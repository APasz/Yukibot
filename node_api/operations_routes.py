"""HTTP registration for durable node operation records."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import Request

from _audit import audit_log
from .operation_service import NodeOperationApiService
from .operations import NodeOperationKind
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

    def require_actor(self, context: NodeRequestContext) -> NodeRequestContext: ...


def register_operation_routes(
    nicegui_app: Any,
    *,
    auth: NodeOperationRouteAuth,
    operation_api: NodeOperationApiService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register the shared operation list, detail, and cancellation endpoints."""

    @nicegui_app.get(f"{api_prefix}/operations")
    async def _list_operations(
        request: Request,
        access_token: str | None = None,
        kind: NodeOperationKind | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        scope = _scope_for_request(
            operation_api=operation_api,
            kind=kind,
            cancellation=False,
            http_exception=http_exception,
        )
        traffic_log.info(
            "Node API operation list request: node=%s kind=%s limit=%s",
            auth.node_name,
            None if kind is None else kind.value,
            limit,
        )
        auth.require_access(request, access_token, app_name=None, scopes=(scope,))
        try:
            records = operation_api.list_operations(kind=kind, limit=limit)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        return {"operations": [record.to_mapping() for record in records]}

    @nicegui_app.get(f"{api_prefix}/operations/{{operation_id}}")
    async def _operation_detail(
        operation_id: str,
        request: Request,
        access_token: str | None = None,
        kind: NodeOperationKind | None = None,
    ) -> dict[str, object]:
        scope = _scope_for_request(
            operation_api=operation_api,
            kind=kind,
            cancellation=False,
            http_exception=http_exception,
        )
        traffic_log.info(
            "Node API operation detail request: node=%s operation=%s kind=%s",
            auth.node_name,
            operation_id,
            None if kind is None else kind.value,
        )
        auth.require_access(request, access_token, app_name=None, scopes=(scope,))
        try:
            return operation_api.get_operation(
                operation_id=operation_id,
                kind=kind,
            ).to_mapping()
        except LookupError as xcp:
            raise http_exception(404, "Operation was not found.") from xcp

    @nicegui_app.post(f"{api_prefix}/operations/{{operation_id}}/cancel")
    async def _cancel_operation(
        operation_id: str,
        request: Request,
        access_token: str | None = None,
        kind: NodeOperationKind | None = None,
    ) -> dict[str, object]:
        scope = _scope_for_request(
            operation_api=operation_api,
            kind=kind,
            cancellation=True,
            http_exception=http_exception,
        )
        traffic_log.info(
            "Node API operation cancellation request: node=%s operation=%s kind=%s",
            auth.node_name,
            operation_id,
            None if kind is None else kind.value,
        )
        context = auth.require_access(request, access_token, app_name=None, scopes=(scope,))
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        try:
            view = await operation_api.cancel_operation(
                operation_id=operation_id,
                actor_user_id=actor_user_id,
                kind=kind,
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


def _scope_for_request(
    *,
    operation_api: NodeOperationApiService,
    kind: NodeOperationKind | None,
    cancellation: bool,
    http_exception: HttpExceptionFactory,
) -> NodeApiScope:
    try:
        if cancellation:
            return operation_api.cancel_scope_for(kind=kind)
        return operation_api.read_scope_for(kind=kind)
    except LookupError as xcp:
        raise http_exception(404, "Operation type was not found.") from xcp
    except ValueError as xcp:
        raise http_exception(400, str(xcp)) from xcp


__all__: tuple[str, ...] = ("register_operation_routes",)
