"""HTTP and WebSocket registration for app chat routes."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import HTTPException, Request, WebSocket, WebSocketException

from apps._app import App
from node_api_chat import NodeWebChatRequest
from node_api_route_contracts import MappingResponse, NodeAuthenticatedRouteService
from node_auth import NodeApiScope


class NodeChatRouteService(NodeAuthenticatedRouteService, Protocol):
    """Chat operations exposed through the HTTP API."""

    def _resolve_app(self, app_name: str) -> App: ...

    def build_chat_room_snapshot(self, app: App, *, limit: int) -> MappingResponse: ...

    async def publish_app_web_chat(
        self,
        *,
        app: App,
        actor_user_id: int,
        chat_request: NodeWebChatRequest,
    ) -> MappingResponse: ...

    def _require_websocket_token_access(
        self,
        *,
        websocket: WebSocket,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> object: ...

    def _require_chat_relay_app(self, app: App) -> None: ...

    def _websocket_exception_from_http(self, error: HTTPException) -> WebSocketException: ...

    async def _serve_chat_stream(
        self,
        *,
        websocket: WebSocket,
        app: App,
        after_revision: int | None,
    ) -> None: ...


def register_chat_routes(
    nicegui_app: Any,
    *,
    service: NodeChatRouteService,
    api_prefix: str,
    history_limit: int,
    traffic_log: logging.Logger,
) -> None:
    """Register chat snapshots, publishing, and streaming endpoints."""

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/chat")
    async def _chat_snapshot(
        app_name: str,
        request: Request,
        limit: int = history_limit,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API chat snapshot request: node=%s app=%s", service.node_name, app_name)
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CHAT_READ,))
        app = service._resolve_app(app_name)
        return service.build_chat_room_snapshot(app, limit=limit).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/chat")
    async def _publish_chat(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API chat publish request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CHAT_WRITE,))
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CHAT_WRITE,),
            verified_grant=grant,
        )
        chat_request = NodeWebChatRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        return (
            await service.publish_app_web_chat(
                app=app,
                actor_user_id=actor_user_id,
                chat_request=chat_request,
            )
        ).to_mapping()

    @nicegui_app.websocket(f"{api_prefix}/apps/{{app_name}}/chat/stream")
    async def _chat_stream(
        websocket: WebSocket,
        app_name: str,
        access_token: str | None = None,
        after_revision: int | None = None,
    ) -> None:
        traffic_log.info("Node API chat stream request: node=%s app=%s", service.node_name, app_name)
        service._require_websocket_token_access(
            websocket=websocket,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CHAT_READ, NodeApiScope.MODS_READ),
        )
        try:
            app = service._resolve_app(app_name)
            service._require_chat_relay_app(app)
        except HTTPException as xcp:
            raise service._websocket_exception_from_http(xcp) from xcp
        await service._serve_chat_stream(
            websocket=websocket,
            app=app,
            after_revision=after_revision,
        )
