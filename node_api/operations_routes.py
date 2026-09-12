"""HTTP registration for durable node operation records."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import Request, WebSocket

from _audit import audit_log
from _security import Power_Level
from .operation_service import NodeOperationApiService, NodeOperationKindPolicy
from .operations import NodeOperationKind
from .realtime_service import NodeRealtimeService
from .request_auth import NodeRequestContext
from .route_contracts import HttpExceptionFactory
from node_auth import NodeApiScope


class NodeOperationRouteAuth(Protocol):
    """The authentication operations consumed by durable-operation routes."""

    @property
    def node_name(self) -> str: ...

    def require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeRequestContext: ...

    def require_websocket_token_access(
        self,
        *,
        websocket: WebSocket,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeRequestContext: ...

    def require_actor(self, context: NodeRequestContext) -> NodeRequestContext: ...

    async def require_actor_level(
        self,
        context: NodeRequestContext,
        required_level: Power_Level,
    ) -> NodeRequestContext: ...


def register_operation_routes(
    nicegui_app: Any,
    *,
    auth: NodeOperationRouteAuth,
    operation_api: NodeOperationApiService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
    realtime: NodeRealtimeService | None = None,
) -> None:
    """Register the shared operation list, detail, and cancellation endpoints."""

    if realtime is not None:

        @nicegui_app.websocket(f"{api_prefix}/operations/stream")
        async def _operation_stream(
            websocket: WebSocket,
            access_token: str | None = None,
        ) -> None:
            stream_scopes = operation_api.stream_read_scopes()
            traffic_log.info(
                "Node API operation stream request: node=%s scopes=%s",
                auth.node_name,
                ",".join(scope.value for scope in stream_scopes),
            )
            auth.require_websocket_token_access(
                websocket=websocket,
                access_token=access_token,
                app_name=None,
                scopes=stream_scopes,
            )
            await realtime.serve_operation_stream(websocket)

    @nicegui_app.get(f"{api_prefix}/operations")
    async def _list_operations(
        request: Request,
        access_token: str | None = None,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        policy, target_app_name = _policy_for_request(
            operation_api=operation_api,
            kind=kind,
            app_name=app_name,
            http_exception=http_exception,
        )
        traffic_log.info(
            "Node API operation list request: node=%s kind=%s app=%s limit=%s",
            auth.node_name,
            None if kind is None else kind.value,
            target_app_name,
            limit,
        )
        auth.require_access(
            request,
            access_token,
            app_name=target_app_name,
            scopes=(policy.read_scope,),
        )
        try:
            records = operation_api.list_operations(
                kind=kind,
                app_name=target_app_name,
                limit=limit,
            )
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        return {"operations": [record.to_mapping() for record in records]}

    @nicegui_app.get(f"{api_prefix}/operations/{{operation_id}}")
    async def _operation_detail(
        operation_id: str,
        request: Request,
        access_token: str | None = None,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
    ) -> dict[str, object]:
        policy, target_app_name = _policy_for_request(
            operation_api=operation_api,
            kind=kind,
            app_name=app_name,
            http_exception=http_exception,
        )
        traffic_log.info(
            "Node API operation detail request: node=%s operation=%s kind=%s app=%s",
            auth.node_name,
            operation_id,
            None if kind is None else kind.value,
            target_app_name,
        )
        auth.require_access(
            request,
            access_token,
            app_name=target_app_name,
            scopes=(policy.read_scope,),
        )
        try:
            return operation_api.get_operation(
                operation_id=operation_id,
                kind=kind,
                app_name=target_app_name,
            ).to_mapping()
        except LookupError as xcp:
            raise http_exception(404, "Operation was not found.") from xcp

    @nicegui_app.post(f"{api_prefix}/operations/{{operation_id}}/cancel")
    async def _cancel_operation(
        operation_id: str,
        request: Request,
        access_token: str | None = None,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
    ) -> dict[str, object]:
        policy, target_app_name = _policy_for_request(
            operation_api=operation_api,
            kind=kind,
            app_name=app_name,
            http_exception=http_exception,
        )
        traffic_log.info(
            "Node API operation cancellation request: node=%s operation=%s kind=%s app=%s",
            auth.node_name,
            operation_id,
            None if kind is None else kind.value,
            target_app_name,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=target_app_name,
            scopes=(policy.cancel_scope,),
        )
        context = await auth.require_actor_level(context, policy.required_level)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        try:
            view = await operation_api.cancel_operation(
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                kind=kind,
                app_name=target_app_name,
            )
        except LookupError as xcp:
            raise http_exception(404, "Operation was not found.") from xcp
        except ValueError as xcp:
            raise http_exception(409, str(xcp)) from xcp
        policy = operation_api.policy_for(view.record.kind)
        audit_log(
            "operation.cancel_requested",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            operation_id=view.record.operation_id,
            operation_kind=view.record.kind.value,
            required_level=policy.required_level.name,
        )
        return view.to_mapping()


def _policy_for_request(
    *,
    operation_api: NodeOperationApiService,
    kind: NodeOperationKind | None,
    app_name: str | None,
    http_exception: HttpExceptionFactory,
) -> tuple[NodeOperationKindPolicy, str | None]:
    try:
        policy = operation_api.policy_for_request(kind=kind, app_name=app_name)
        return policy, policy.app_name_for_request(app_name)
    except LookupError as xcp:
        raise http_exception(404, "Operation type was not found.") from xcp
    except ValueError as xcp:
        raise http_exception(400, str(xcp)) from xcp


__all__: tuple[str, ...] = ("register_operation_routes",)
