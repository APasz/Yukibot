"""HTTP registration for core node and app-discovery routes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from fastapi import Request, WebSocket, status
from fastapi.responses import Response

from apps._app import App
from .realtime_service import NodeRealtimeService
from .relay import NodeRelayTTSRequest, NodeRelayTTSService
from .route_contracts import MappingResponse, NodeAuthenticatedRouteService
from node_auth import NodeApiScope


def register_core_routes(
    nicegui_app: Any,
    *,
    auth: NodeAuthenticatedRouteService,
    list_apps: Callable[[], Awaitable[Sequence[MappingResponse]]],
    resolve_app: Callable[[str], App],
    build_live_app_entry: Callable[[App], Awaitable[MappingResponse]],
    node_ping_headers: Callable[[], Mapping[str, str]],
    realtime: NodeRealtimeService,
    relay_tts: NodeRelayTTSService,
    api_prefix: str,
    traffic_log: logging.Logger,
) -> None:
    """Register app discovery, presence, node-state, and relay endpoints."""

    @nicegui_app.get(f"{api_prefix}/apps")
    async def _list_apps(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API apps request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,))
        return {"node": auth.node_name, "apps": [entry.to_mapping() for entry in await list_apps()]}

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}")
    async def _app_summary(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API app summary request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.APPS_READ,))
        return (await build_live_app_entry(resolve_app(app_name))).to_mapping()

    @nicegui_app.get(f"{api_prefix}/ping")
    async def _ping() -> Response:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=node_ping_headers())

    @nicegui_app.websocket(f"{api_prefix}/presence/stream")
    async def _presence_stream(websocket: WebSocket) -> None:
        traffic_log.info("Node API presence stream request: node=%s", auth.node_name)
        await realtime.serve_presence_stream(websocket)

    @nicegui_app.websocket(f"{api_prefix}/state/stream")
    async def _node_state_stream(
        websocket: WebSocket,
        access_token: str | None = None,
    ) -> None:
        traffic_log.info("Node API node state stream request: node=%s", auth.node_name)
        auth.require_websocket_token_access(
            websocket=websocket,
            access_token=access_token,
            app_name=None,
            scopes=(NodeApiScope.APPS_READ,),
        )
        await realtime.serve_node_state_stream(websocket)

    @nicegui_app.post(f"{api_prefix}/relay/tts")
    async def _queue_relay_tts(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.RELAY_TTS,))
        relay_request = NodeRelayTTSRequest.model_validate(payload)
        return (await relay_tts.queue_request(relay_request)).to_mapping()
