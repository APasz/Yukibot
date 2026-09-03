"""Authentication and request-actor resolution for Node API routes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from fastapi import HTTPException, Request, WebSocket, WebSocketException, status

import config
from _security import Access_Control, Power_Level
from mod_web_auth import ModWebAuthService, ModWebUser
from node_auth import (
    NodeAccessGrant,
    NodeApiScope,
    NodeTokenError,
    issue_node_token,
    verify_node_token,
)


NodeHttpExceptionFactory = Callable[[int, str], Exception]


@dataclass(frozen=True, slots=True)
class NodeRequestContext:
    """Verified identity data available to one Node API request."""

    grant: NodeAccessGrant | None
    actor_user_id: int | None = None
    web_user: ModWebUser | None = None

    def require_actor_user_id(self) -> int:
        """Return the resolved actor, failing loudly for a context without one."""

        actor_user_id = self.actor_user_id
        if actor_user_id is None:
            raise RuntimeError("Node request context does not include an actor user.")
        return actor_user_id


class NodeRequestAuth:
    """Authenticate HTTP and WebSocket Node API requests and resolve their actor."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        http_exception: NodeHttpExceptionFactory,
        logger: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._http_exception = http_exception
        self._logger = logger
        self._acl: Access_Control | None = None
        self._web_auth: ModWebAuthService | None = None

    @property
    def node_name(self) -> str:
        """The node for which requests are being authorised."""

        return self._node_name()

    def set_acl(self, acl: Access_Control) -> None:
        """Attach the Discord permission source used for web-session access."""

        self._acl = acl

    def set_web_auth(self, web_auth: ModWebAuthService) -> None:
        """Attach the Mod Web session provider used for browser access."""

        self._web_auth = web_auth

    def issue_access_token(
        self,
        *,
        subject: str,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        ttl_seconds: int,
    ) -> str | None:
        """Issue a short-lived signed Node API token when token auth is configured."""

        secret = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            return None
        self._logger.debug(
            "Issuing node API access token: node=%s app=%s scopes=%s subject=%s ttl_seconds=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
            subject,
            ttl_seconds,
        )
        return issue_node_token(
            secret=secret,
            grant=NodeAccessGrant(
                subject=subject,
                node=self.node_name,
                app=app_name,
                scopes=frozenset(scopes),
                expires_at=int(time.time()) + ttl_seconds,
            ),
        )

    def require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        token_node_names: Sequence[str] | None = None,
    ) -> NodeRequestContext:
        """Verify a token or authorised browser session for an HTTP request."""

        secret = config.MOD_WEB_SERVER.token_secret
        token_error: NodeTokenError | None = None
        if secret is not None:
            try:
                grant = self._verified_token_grant(
                    request=request,
                    access_token=access_token,
                    app_name=app_name,
                    scopes=scopes,
                    node_names=token_node_names,
                )
            except NodeTokenError as xcp:
                token_error = xcp
            else:
                self._logger.debug(
                    "Node API token access accepted: node=%s app=%s scopes=%s",
                    self.node_name,
                    app_name,
                    scopes,
                )
                return NodeRequestContext(grant=grant)

        session_user = self._require_web_session_access(
            request=request,
            app_name=app_name,
            scopes=scopes,
        )
        if session_user is not None:
            return NodeRequestContext(grant=None, web_user=session_user)

        if secret is None and (config.INDEV or config.ALLOW_UNAUTH_NODE_API):
            self._logger.debug(
                "Node API auth disabled: node=%s app=%s scopes=%s",
                self.node_name,
                app_name,
                scopes,
            )
            return NodeRequestContext(grant=None)

        reason = token_error or NodeTokenError("Node API authentication is not configured.")
        self._logger.warning(
            "Node API access rejected: node=%s app=%s scopes=%s reason=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
            reason,
        )
        raise self._http_exception(403, str(reason)) from token_error

    def with_current_web_user(
        self,
        context: NodeRequestContext,
        request: Request,
    ) -> NodeRequestContext:
        """Return a context enriched with the active Mod Web user, if any."""

        if context.web_user is not None:
            return context
        web_user = self._current_web_user(request)
        return context if web_user is None else replace(context, web_user=web_user)

    def require_actor(self, context: NodeRequestContext) -> NodeRequestContext:
        """Resolve the Discord actor required for a mutating request."""

        if context.actor_user_id is not None:
            return context
        grant = context.grant
        if grant is not None:
            return replace(
                context,
                actor_user_id=self._actor_user_id_from_subject(grant.subject),
            )
        web_user = context.web_user
        if web_user is None:
            raise self._http_exception(
                403, "Mod mutation requires an authenticated Discord user."
            )
        return replace(context, actor_user_id=web_user.discord_id)

    def resolve_actor_if_available(self, context: NodeRequestContext) -> NodeRequestContext:
        """Resolve the actor only when the active auth mode makes one available."""

        if self._acl is None:
            return context
        if context.grant is None and (
            self._web_auth is None or not self._web_auth.enabled
        ):
            return context
        return self.require_actor(context)

    async def require_actor_level(
        self,
        context: NodeRequestContext,
        required_level: Power_Level,
    ) -> NodeRequestContext:
        """Apply an actor-level check when request authentication has an actor."""

        acl = self._acl
        if acl is None:
            return context
        if context.grant is None and (
            self._web_auth is None or not self._web_auth.enabled
        ):
            return context
        resolved_context = self.require_actor(context)
        try:
            await acl.perm_check(
                resolved_context.require_actor_user_id(), required_level
            )
        except PermissionError as xcp:
            raise self._http_exception(403, str(xcp)) from xcp
        return resolved_context

    def require_websocket_token_access(
        self,
        *,
        websocket: WebSocket,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeRequestContext:
        """Verify token-only access for a Node API WebSocket route."""

        secret = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise WebSocketException(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="Node token secret is not configured.",
            )
        token = self._request_token(websocket, access_token)
        try:
            grant = verify_node_token(
                secret=secret,
                token=token,
                node=self.node_name,
                app=app_name,
                required_scopes=scopes,
            )
        except NodeTokenError as xcp:
            self._logger.warning(
                "Node API websocket access rejected: node=%s app=%s scopes=%s reason=%s",
                self.node_name,
                app_name,
                ",".join(scope.value for scope in scopes),
                xcp,
            )
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=str(xcp),
            ) from xcp
        self._logger.debug(
            "Node API websocket token access accepted: node=%s app=%s scopes=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
        )
        return NodeRequestContext(grant=grant)

    @staticmethod
    def websocket_exception_from_http(error: HTTPException) -> WebSocketException:
        """Map HTTP route failures to stable WebSocket close semantics."""

        if error.status_code in {400, 401, 403, 404, 409}:
            code = status.WS_1008_POLICY_VIOLATION
        else:
            code = status.WS_1011_INTERNAL_ERROR
        return WebSocketException(code=code, reason=str(error.detail))

    def required_web_level(
        self,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> Power_Level:
        """Return the least privileged web role that grants all requested scopes."""

        del app_name
        if not scopes:
            raise self._http_exception(403, "Node API access requires at least one scope.")
        required_levels: list[Power_Level] = []
        for scope in scopes:
            level = _NODE_API_SCOPE_WEB_LEVELS.get(scope)
            if level is None:
                raise self._http_exception(
                    403,
                    f"Node API scope cannot be granted by a web session: {scope.value}.",
                )
            required_levels.append(level)
        return max(required_levels)

    def _verified_token_grant(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        node_names: Sequence[str] | None = None,
    ) -> NodeAccessGrant:
        secret = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise NodeTokenError("Node token secret is not configured.")
        token = self._request_token(request, access_token)
        resolved_node_names = node_names or (self.node_name,)
        token_error: NodeTokenError | None = None
        for node_name in resolved_node_names:
            try:
                return verify_node_token(
                    secret=secret,
                    token=token,
                    node=node_name,
                    app=app_name,
                    required_scopes=scopes,
                )
            except NodeTokenError as xcp:
                token_error = xcp
        raise token_error or NodeTokenError("Node token node target is not configured.")

    def _actor_user_id_from_subject(self, subject: str) -> int:
        prefix = "web:"
        if not subject.startswith(prefix):
            raise self._http_exception(
                403, f"Node token subject cannot act as a web user: {subject}"
            )
        raw_user_id = subject[len(prefix) :].strip()
        if not raw_user_id.isdigit():
            raise self._http_exception(
                403, f"Node token subject is invalid for web actions: {subject}"
            )
        return int(raw_user_id)

    def _current_web_user(self, request: Request) -> ModWebUser | None:
        web_auth = self._web_auth
        return None if web_auth is None else web_auth.current_user(request)

    def _require_web_session_access(
        self,
        *,
        request: Request,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> ModWebUser | None:
        web_auth = self._web_auth
        if web_auth is None or not web_auth.enabled:
            return None

        acl = self._acl
        if acl is None:
            self._logger.warning(
                "Node API web session auth unavailable because Access_Control is not attached."
            )
            raise self._http_exception(503, "Mod web permissions are not available.")

        required_level = self.required_web_level(app_name=app_name, scopes=scopes)
        web_user = web_auth.current_user(request)
        if web_user is None:
            raise self._http_exception(401, "Discord login is required.")
        if not acl.can(web_user.discord_id, required_level):
            raise self._http_exception(
                403,
                "Insufficient level: "
                f"{acl.level_of(web_user.discord_id).name.title()} < {required_level.name.title()}",
            )
        self._logger.debug(
            "Node API web session access accepted: node=%s app=%s scopes=%s user_id=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
            web_user.discord_id,
        )
        return web_user

    @staticmethod
    def _request_token(request: Request | WebSocket, _access_token: str | None) -> str:
        auth_header = request.headers.get("authorization", "")
        scheme, _, token = auth_header.partition(" ")
        if scheme.casefold() == "bearer" and token:
            return token.strip()
        return ""


_NODE_API_SCOPE_WEB_LEVELS: dict[NodeApiScope, Power_Level] = {
    NodeApiScope.APPS_READ: Power_Level.visitor,
    NodeApiScope.MAP_READ: Power_Level.visitor,
    NodeApiScope.MAP_WRITE: Power_Level.user,
    NodeApiScope.CHAT_READ: Power_Level.visitor,
    NodeApiScope.CHAT_WRITE: Power_Level.visitor,
    NodeApiScope.CHAT_INJECT: Power_Level.root,
    NodeApiScope.MODS_READ: Power_Level.visitor,
    NodeApiScope.MODS_DOWNLOAD: Power_Level.user,
    NodeApiScope.MODS_WRITE: Power_Level.user,
    NodeApiScope.CONFIGS_READ: Power_Level.visitor,
    NodeApiScope.CONFIGS_WRITE: Power_Level.sudo,
    NodeApiScope.SAVES_READ: Power_Level.user,
    NodeApiScope.SAVES_DOWNLOAD: Power_Level.user,
    NodeApiScope.SAVES_WRITE: Power_Level.user,
    NodeApiScope.BLUEPRINTS_READ: Power_Level.user,
    NodeApiScope.BLUEPRINTS_WRITE: Power_Level.user,
    NodeApiScope.SETTINGS_READ: Power_Level.user,
    NodeApiScope.SETTINGS_WRITE: Power_Level.user,
    NodeApiScope.FILES_READ: Power_Level.user,
    NodeApiScope.FILES_DOWNLOAD: Power_Level.user,
    NodeApiScope.FILES_UPLOAD: Power_Level.user,
    NodeApiScope.APP_CONTROL: Power_Level.user,
    NodeApiScope.APP_MANAGE: Power_Level.sudo,
    NodeApiScope.NODE_OPERATE: Power_Level.sudo,
    NodeApiScope.NODE_MANAGE: Power_Level.root,
}
__all__: tuple[str, ...] = ("NodeRequestAuth", "NodeRequestContext")
