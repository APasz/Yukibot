"""HTTP and WebSocket registration for app console routes."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import HTTPException, Request, WebSocket, WebSocketException, status

from _audit import audit_log
from apps._app import App
from apps._console import ConsoleAction
from .console import NodeConsoleActionExecuteRequest, NodeConsoleActionExecutionResult
from .route_contracts import HttpExceptionFactory, MappingResponse, NodeAuthenticatedRouteService
from node_auth import NodeApiScope


class NodeConsoleRouteService(Protocol):
    """Console operations required by the console route registrar."""

    def _resolve_app(self, app_name: str) -> App: ...

    def _resolve_console_action(self, app: App, action_key: str) -> ConsoleAction: ...

    async def _serve_console_stdout_stream(self, *, websocket: WebSocket, app: App, max_lines: int) -> None: ...

    def build_console_action_list(self, *, app: App, actor_user_id: int) -> MappingResponse: ...

    async def execute_console_action(
        self,
        *,
        app: App,
        action_key: str,
        raw_value: str | None,
        actor_user_id: int,
    ) -> NodeConsoleActionExecutionResult: ...

    async def read_console_stdout(self, *, app: App, actor_user_id: int, max_lines: int) -> MappingResponse: ...


def register_console_routes(
    nicegui_app: Any,
    *,
    service: NodeConsoleRouteService,
    auth: NodeAuthenticatedRouteService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register per-app console action and stdout endpoints."""
    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/console-actions")
    async def _list_console_actions(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API console action list request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_CONTROL,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = service._resolve_app(app_name)
        return service.build_console_action_list(app=app, actor_user_id=actor_user_id).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/console-actions/{{action_key}}")
    async def _execute_console_action_route(
        app_name: str,
        action_key: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API console action execute request: node=%s app=%s action=%s",
            auth.node_name,
            app_name,
            action_key,
        )
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_CONTROL,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        execute_request: NodeConsoleActionExecuteRequest = NodeConsoleActionExecuteRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        result = await service.execute_console_action(
            app=app,
            action_key=action_key,
            raw_value=execute_request.value,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "app.console_action_executed",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            console_action_key=action_key,
            required_level=service._resolve_console_action(app, action_key).power_level.name,
            success=result.success,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/console/stdout")
    async def _read_console_stdout_route(
        app_name: str,
        request: Request,
        access_token: str | None = None,
        max_lines: int = 200,
    ) -> dict[str, object]:
        traffic_log.info("Node API console stdout request: node=%s app=%s", auth.node_name, app_name)
        if max_lines < 1 or max_lines > 500:
            raise http_exception(400, "Console stdout line limit must be between 1 and 500.")
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_CONTROL,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = service._resolve_app(app_name)
        result = await service.read_console_stdout(app=app, actor_user_id=actor_user_id, max_lines=max_lines)
        return result.to_mapping()

    @nicegui_app.websocket(f"{api_prefix}/apps/{{app_name}}/console/stdout/stream")
    async def _console_stdout_stream(
        websocket: WebSocket,
        app_name: str,
        access_token: str | None = None,
        max_lines: int = 200,
    ) -> None:
        traffic_log.info("Node API console stdout stream request: node=%s app=%s", auth.node_name, app_name)
        if max_lines < 1 or max_lines > 500:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid stdout line limit.")
        auth.require_websocket_token_access(
            websocket=websocket,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.APP_CONTROL,),
        )
        try:
            app = service._resolve_app(app_name)
        except HTTPException as xcp:
            raise auth.websocket_exception_from_http(xcp) from xcp
        await service._serve_console_stdout_stream(websocket=websocket, app=app, max_lines=max_lines)
