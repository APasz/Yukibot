from __future__ import annotations

import enum
import hashlib
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import requests
from diskcache import Cache
from fastapi import Request
from starlette.responses import RedirectResponse, Response

import config
from _async_utils import run_blocking
from _audit import audit_log
from _security import Access_Control, Power_Level

log: Logger = logging.getLogger(__name__)

_DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
_DISCORD_ME_URL = "https://discord.com/api/v10/users/@me"
_SESSION_COOKIE_NAME = "yukibot_mod_web_session"
_OAUTH_STATE_COOKIE_NAME = "yukibot_mod_web_oauth_state"
_BYPASS_SUPPRESS_COOKIE_NAME = "yukibot_mod_web_bypass_suppressed"
_OAUTH_STATE_TTL_SECONDS = 10 * 60
_BROWSER_SESSION_TTL_SECONDS = 16 * 60 * 60
_REMEMBERED_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_DISCORD_REQUEST_TIMEOUT_SECONDS = 10.0
_CACHE_PAYLOAD_VERSION = 1
_SESSION_MEMORY_CACHE_LIMIT = 256
_SESSION_CACHE_KEY_PREFIX = "session:"
_OAUTH_STATE_CACHE_KEY_PREFIX = "oauth_state:"
_DEV_BYPASS_ACCOUNT_NAMES: dict[Power_Level, str] = {
    Power_Level.guest: "Dev Guest",
    Power_Level.visitor: "Dev Visitor",
    Power_Level.user: "Dev User",
    Power_Level.admin: "Dev Admin",
    Power_Level.sudo: "Dev Sudo",
    Power_Level.root: "Dev Root",
}
_DEV_BYPASS_DISPLAY_NAMES: dict[Power_Level, str] = {
    Power_Level.guest: "Tourist",
    Power_Level.visitor: "Lost Peeper",
    Power_Level.user: "Jane Doe",
    Power_Level.admin: "Agent Smith",
    Power_Level.sudo: "Finch",
    Power_Level.root: "Unimatrix 01",
}


class ModWebAuthError(ValueError):
    pass


class ModWebSessionPersistence(enum.StrEnum):
    BROWSER_SESSION = "browser_session"
    REMEMBERED = "remembered"

    @classmethod
    def from_remembered(cls, remembered: bool) -> ModWebSessionPersistence:
        return cls.REMEMBERED if remembered else cls.BROWSER_SESSION

    @property
    def ttl_seconds(self) -> int:
        if self is ModWebSessionPersistence.BROWSER_SESSION:
            return _BROWSER_SESSION_TTL_SECONDS
        return _REMEMBERED_SESSION_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class ModWebUser:
    discord_id: int
    username: str
    global_name: str | None
    avatar_hash: str | None

    @property
    def display_name(self) -> str:
        return self.global_name or self.username


@dataclass(frozen=True, slots=True)
class ModWebSession:
    session_id: str
    user: ModWebUser
    persistence: ModWebSessionPersistence
    created_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class PendingOAuthState:
    state: str
    next_path: str
    persistence: ModWebSessionPersistence
    expires_at: int


class ModWebAuthService:
    def __init__(self, auth_config: config.ModWebAuthConfig | None = None) -> None:
        self._config = auth_config or config.MOD_WEB_AUTH
        cache_directory: Path | None = self._config.session_cache_directory
        self._cache = Cache(directory=None if cache_directory is None else str(cache_directory))
        self._session_memory_cache: OrderedDict[str, ModWebSession] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def bypass_enabled(self) -> bool:
        return self._config.bypass_enabled

    @property
    def bypass_user(self) -> ModWebUser:
        return self.dev_bypass_user(Power_Level.user)

    @property
    def bypass_levels(self) -> tuple[Power_Level, ...]:
        return (
            Power_Level.guest,
            Power_Level.visitor,
            Power_Level.user,
            Power_Level.admin,
            Power_Level.sudo,
            Power_Level.root,
        )

    def dev_bypass_user(self, level: Power_Level) -> ModWebUser:
        return ModWebUser(
            discord_id=Access_Control.dev_bypass_user_id(level),
            username=_DEV_BYPASS_ACCOUNT_NAMES[level],
            global_name=_DEV_BYPASS_DISPLAY_NAMES[level],
            avatar_hash=None,
        )

    @property
    def redirect_url(self) -> str:
        return self._config.redirect_url

    def login_redirect(
        self,
        *,
        next_path: str = "/",
        persistence: ModWebSessionPersistence = ModWebSessionPersistence.BROWSER_SESSION,
    ) -> RedirectResponse:
        if self.bypass_enabled:
            return self.dev_login_response(level=Power_Level.user, next_path=next_path, persistence=persistence)
        self._require_configured()
        state = secrets.token_urlsafe(32)
        pending = PendingOAuthState(
            state=state,
            next_path=self._safe_next_path(next_path),
            persistence=persistence,
            expires_at=self._now() + _OAUTH_STATE_TTL_SECONDS,
        )
        self._store_pending_state(pending)

        authorize_url = self._authorize_url(state)
        log.info(
            "Mod web login redirect created: redirect_uri=%s next_path=%s persistence=%s secure_cookies=%s",
            self._config.redirect_url,
            pending.next_path,
            pending.persistence.value,
            self._secure_cookies(),
        )
        response = RedirectResponse(authorize_url)
        response.set_cookie(
            _OAUTH_STATE_COOKIE_NAME,
            state,
            max_age=_OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=self._secure_cookies(),
            samesite="lax",
        )
        return response

    async def callback_response(self, *, request: Request, code: str | None, state: str | None) -> RedirectResponse:
        self._require_configured()
        if not code:
            raise ModWebAuthError("Discord OAuth callback did not include a code.")
        log.info(
            "Mod web OAuth callback received: path=%s query=%s redirect_uri=%s",
            request.url.path,
            request.url.query,
            self._config.redirect_url,
        )
        pending = self._consume_state(request=request, state=state)
        token = await run_blocking(self._exchange_code, code)
        user = await run_blocking(self._fetch_user, token)

        response = RedirectResponse(pending.next_path)
        self._set_session_cookie(response, self._create_session(user, persistence=pending.persistence))
        response.delete_cookie(_OAUTH_STATE_COOKIE_NAME)
        response.delete_cookie(_BYPASS_SUPPRESS_COOKIE_NAME)
        log.info("Mod web login accepted: user_id=%s username=%s", user.discord_id, user.username)
        audit_log(
            "security.mod_web_login",
            user_id=user.discord_id,
            username=user.username,
            auth_kind="discord_oauth",
            persistence=pending.persistence.value,
        )
        return response

    def logout_response(self) -> RedirectResponse:
        response = RedirectResponse("/")
        response.delete_cookie(_SESSION_COOKIE_NAME)
        response.delete_cookie(_OAUTH_STATE_COOKIE_NAME)
        response.delete_cookie(_BYPASS_SUPPRESS_COOKIE_NAME)
        return response

    def logout_request(self, request: Request) -> None:
        session_id = request.cookies.get(_SESSION_COOKIE_NAME)
        if not session_id:
            return
        self._session_memory_cache.pop(session_id, None)
        self._cache.delete(self._session_cache_key(session_id), retry=True)

    def current_session(self, request: Request) -> ModWebSession | None:
        session_id = request.cookies.get(_SESSION_COOKIE_NAME)
        if not session_id:
            return None
        now = self._now()
        cached_session = self._session_memory_cache.get(session_id)
        if cached_session is not None:
            if cached_session.expires_at > now:
                self._session_memory_cache.move_to_end(session_id)
                return cached_session
            self._session_memory_cache.pop(session_id, None)
        cache_key = self._session_cache_key(session_id)
        raw: object = cast(object, self._cache.get(cache_key, retry=True))
        if raw is None:
            return None
        session = self._session_from_cache_payload(raw, session_id=session_id)
        if session.expires_at <= now:
            self._cache.delete(cache_key, retry=True)
            return None
        self._remember_session(session)
        return session

    def current_user(self, request: Request) -> ModWebUser | None:
        session = self.current_session(request)
        return session.user if session is not None else None

    def _remember_session(self, session: ModWebSession) -> None:
        self._session_memory_cache[session.session_id] = session
        self._session_memory_cache.move_to_end(session.session_id)
        while len(self._session_memory_cache) > _SESSION_MEMORY_CACHE_LIMIT:
            self._session_memory_cache.popitem(last=False)

    def dev_login_response(
        self,
        *,
        level: Power_Level,
        next_path: str = "/",
        persistence: ModWebSessionPersistence = ModWebSessionPersistence.BROWSER_SESSION,
    ) -> RedirectResponse:
        if not self.bypass_enabled:
            raise ModWebAuthError("Dev bypass login is not enabled.")
        user = self.dev_bypass_user(level)
        response = RedirectResponse(self._safe_next_path(next_path))
        self._set_session_cookie(response, self._create_session(user, persistence=persistence))
        response.delete_cookie(_BYPASS_SUPPRESS_COOKIE_NAME)
        audit_log(
            "security.mod_web_login",
            user_id=user.discord_id,
            username=user.username,
            auth_kind="dev_bypass",
            level=level.name,
            persistence=persistence.value,
        )
        return response

    def _authorize_url(self, state: str) -> str:
        client_id = self._config.discord_client_id
        if client_id is None:
            raise ModWebAuthError("Discord OAuth client ID is not configured.")
        return f"{_DISCORD_AUTHORIZE_URL}?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": self._config.redirect_url,
                "response_type": "code",
                "scope": "identify",
                "state": state,
            }
        )

    def _exchange_code(self, code: str) -> str:
        client_id = self._config.discord_client_id
        client_secret = self._config.discord_client_secret
        if client_id is None or client_secret is None:
            raise ModWebAuthError("Discord OAuth credentials are not configured.")
        try:
            response = requests.post(
                _DISCORD_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._config.redirect_url,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_DISCORD_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as xcp:
            raise ModWebAuthError(f"Discord OAuth token exchange failed: {type(xcp).__name__}: {xcp}") from xcp

        payload = self._json_object(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ModWebAuthError("Discord OAuth token response did not include an access token.")
        return access_token

    def _fetch_user(self, access_token: str) -> ModWebUser:
        try:
            response = requests.get(
                _DISCORD_ME_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_DISCORD_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as xcp:
            raise ModWebAuthError(f"Discord user lookup failed: {type(xcp).__name__}: {xcp}") from xcp

        payload = self._json_object(response)
        raw_id = payload.get("id")
        username = payload.get("username")
        global_name = payload.get("global_name")
        avatar_hash = payload.get("avatar")
        if not isinstance(raw_id, str) or not raw_id.isdecimal():
            raise ModWebAuthError("Discord user response did not include a valid user id.")
        if not isinstance(username, str) or not username:
            raise ModWebAuthError("Discord user response did not include a username.")
        return ModWebUser(
            discord_id=int(raw_id),
            username=username,
            global_name=global_name if isinstance(global_name, str) and global_name else None,
            avatar_hash=avatar_hash if isinstance(avatar_hash, str) and avatar_hash else None,
        )

    def _consume_state(self, *, request: Request, state: str | None) -> PendingOAuthState:
        if not state:
            raise ModWebAuthError("Discord OAuth callback did not include a state.")
        cookie_state = request.cookies.get(_OAUTH_STATE_COOKIE_NAME)
        if cookie_state != state:
            raise ModWebAuthError("Discord OAuth state did not match this browser session.")
        raw: object = cast(
            object,
            self._cache.pop(self._oauth_state_cache_key(state), default=None, retry=True),
        )
        if raw is None:
            raise ModWebAuthError("Discord OAuth state expired or was not started by this server.")
        pending = self._pending_state_from_cache_payload(raw, state=state)
        if pending.expires_at <= self._now():
            raise ModWebAuthError("Discord OAuth state expired or was not started by this server.")
        return pending

    def _create_session(
        self,
        user: ModWebUser,
        *,
        persistence: ModWebSessionPersistence = ModWebSessionPersistence.BROWSER_SESSION,
    ) -> ModWebSession:
        now = self._now()
        session = ModWebSession(
            session_id=secrets.token_urlsafe(32),
            user=user,
            persistence=persistence,
            created_at=now,
            expires_at=now + persistence.ttl_seconds,
        )
        stored = self._cache.set(
            self._session_cache_key(session.session_id),
            self._session_cache_payload(session),
            expire=persistence.ttl_seconds,
            retry=True,
        )
        if not stored:
            raise RuntimeError("Failed to persist the mod web session.")
        self._remember_session(session)
        return session

    def _set_session_cookie(self, response: Response, session: ModWebSession) -> None:
        if session.persistence is ModWebSessionPersistence.REMEMBERED:
            response.set_cookie(
                _SESSION_COOKIE_NAME,
                session.session_id,
                max_age=max(0, session.expires_at - self._now()),
                httponly=True,
                secure=self._secure_cookies(),
                samesite="lax",
            )
            return
        response.set_cookie(
            _SESSION_COOKIE_NAME,
            session.session_id,
            httponly=True,
            secure=self._secure_cookies(),
            samesite="lax",
        )

    def close(self) -> None:
        self._session_memory_cache.clear()
        self._cache.close()

    def _store_pending_state(self, pending: PendingOAuthState) -> None:
        stored = self._cache.set(
            self._oauth_state_cache_key(pending.state),
            self._pending_state_cache_payload(pending),
            expire=_OAUTH_STATE_TTL_SECONDS,
            retry=True,
        )
        if not stored:
            raise RuntimeError("Failed to persist the Discord OAuth state.")

    @staticmethod
    def _session_cache_payload(session: ModWebSession) -> dict[str, object]:
        return {
            "version": _CACHE_PAYLOAD_VERSION,
            "discord_id": session.user.discord_id,
            "username": session.user.username,
            "global_name": session.user.global_name,
            "avatar_hash": session.user.avatar_hash,
            "persistence": session.persistence.value,
            "created_at": session.created_at,
            "expires_at": session.expires_at,
        }

    @staticmethod
    def _pending_state_cache_payload(pending: PendingOAuthState) -> dict[str, object]:
        return {
            "version": _CACHE_PAYLOAD_VERSION,
            "next_path": pending.next_path,
            "persistence": pending.persistence.value,
            "expires_at": pending.expires_at,
        }

    @classmethod
    def _session_from_cache_payload(cls, raw: object, *, session_id: str) -> ModWebSession:
        payload = cls._cache_payload_mapping(raw, kind="session")
        cls._validate_cache_payload_version(payload, kind="session")
        return ModWebSession(
            session_id=session_id,
            user=ModWebUser(
                discord_id=cls._cache_payload_int(payload, "discord_id", kind="session"),
                username=cls._cache_payload_string(payload, "username", kind="session"),
                global_name=cls._cache_payload_optional_string(payload, "global_name", kind="session"),
                avatar_hash=cls._cache_payload_optional_string(payload, "avatar_hash", kind="session"),
            ),
            persistence=cls._cache_payload_persistence(payload, kind="session"),
            created_at=cls._cache_payload_int(payload, "created_at", kind="session"),
            expires_at=cls._cache_payload_int(payload, "expires_at", kind="session"),
        )

    @classmethod
    def _pending_state_from_cache_payload(cls, raw: object, *, state: str) -> PendingOAuthState:
        payload = cls._cache_payload_mapping(raw, kind="OAuth state")
        cls._validate_cache_payload_version(payload, kind="OAuth state")
        return PendingOAuthState(
            state=state,
            next_path=cls._safe_next_path(cls._cache_payload_string(payload, "next_path", kind="OAuth state")),
            persistence=cls._cache_payload_persistence(payload, kind="OAuth state"),
            expires_at=cls._cache_payload_int(payload, "expires_at", kind="OAuth state"),
        )

    @staticmethod
    def _cache_payload_mapping(raw: object, *, kind: str) -> Mapping[str, object]:
        if not isinstance(raw, Mapping):
            raise ModWebAuthError(f"Persisted mod web {kind} must be an object.")
        return cast(Mapping[str, object], raw)

    @classmethod
    def _validate_cache_payload_version(cls, payload: Mapping[str, object], *, kind: str) -> None:
        version = cls._cache_payload_int(payload, "version", kind=kind)
        if version != _CACHE_PAYLOAD_VERSION:
            raise ModWebAuthError(f"Persisted mod web {kind} has unsupported version {version}.")

    @staticmethod
    def _cache_payload_int(payload: Mapping[str, object], field: str, *, kind: str) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ModWebAuthError(f"Persisted mod web {kind} field {field!r} must be an integer.")
        return value

    @staticmethod
    def _cache_payload_string(payload: Mapping[str, object], field: str, *, kind: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ModWebAuthError(f"Persisted mod web {kind} field {field!r} must be a non-empty string.")
        return value

    @staticmethod
    def _cache_payload_optional_string(
        payload: Mapping[str, object], field: str, *, kind: str
    ) -> str | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ModWebAuthError(f"Persisted mod web {kind} field {field!r} must be null or a non-empty string.")
        return value

    @classmethod
    def _cache_payload_persistence(
        cls, payload: Mapping[str, object], *, kind: str
    ) -> ModWebSessionPersistence:
        value = cls._cache_payload_string(payload, "persistence", kind=kind)
        try:
            return ModWebSessionPersistence(value)
        except ValueError as xcp:
            raise ModWebAuthError(f"Persisted mod web {kind} has unknown persistence {value!r}.") from xcp

    @staticmethod
    def _session_cache_key(session_id: str) -> str:
        return _SESSION_CACHE_KEY_PREFIX + hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _oauth_state_cache_key(state: str) -> str:
        return _OAUTH_STATE_CACHE_KEY_PREFIX + hashlib.sha256(state.encode("utf-8")).hexdigest()

    def _require_configured(self) -> None:
        if not self.enabled or self._config.discord_client_id is None or self._config.discord_client_secret is None:
            raise ModWebAuthError("Discord OAuth is not configured for the mod web UI.")

    def _secure_cookies(self) -> bool:
        return self._config.redirect_url.startswith("https://")

    @staticmethod
    def _bypass_suppressed(request: Request) -> bool:
        return request.cookies.get(_BYPASS_SUPPRESS_COOKIE_NAME) == "1"

    @staticmethod
    def _safe_next_path(next_path: str) -> str:
        if next_path.startswith("/") and not next_path.startswith("//"):
            return next_path
        return "/"

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _json_object(response: requests.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as xcp:
            raise ModWebAuthError("Discord returned invalid JSON.") from xcp
        if not isinstance(payload, dict):
            raise ModWebAuthError(f"Discord response must be a JSON object, got {type(payload).__name__}.")
        return cast(dict[str, object], payload)
