"""HTTP and WebSocket registration for app chat routes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from _security import Power_Level
from apps._app import App
from .chat import NodeChatInjectionRequest, NodeWebChatRequest
from .chat_service import NodeChatService
from .route_contracts import NodeAuthenticatedRouteService
from node_auth import NodeApiScope

def register_chat_routes(
    nicegui_app: Any,
    *,
    auth: NodeAuthenticatedRouteService,
    resolve_app: Callable[[str], App],
    chat: NodeChatService,
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
        traffic_log.info("Node API chat snapshot request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CHAT_READ,))
        app = resolve_app(app_name)
        return chat.build_room_snapshot(app, limit=limit).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/chat")
    async def _publish_chat(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API chat publish request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CHAT_WRITE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        chat_request = NodeWebChatRequest.model_validate(payload)
        app = resolve_app(app_name)
        return (
            await chat.publish_web_chat(
                app=app,
                actor_user_id=actor_user_id,
                chat_request=chat_request,
            )
        ).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/chat/inject")
    async def _inject_chat_event(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API fake chat injection request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CHAT_INJECT,),
        )
        await auth.require_actor_level(context, Power_Level.root)
        chat_request = NodeChatInjectionRequest.model_validate(payload)
        app = resolve_app(app_name)
        return (await chat.publish_fake_chat(app=app, event=chat_request.to_chat_event())).to_mapping()

    @nicegui_app.websocket(f"{api_prefix}/apps/{{app_name}}/chat/stream")
    async def _chat_stream(
        websocket: WebSocket,
        app_name: str,
        access_token: str | None = None,
        after_revision: int | None = None,
    ) -> None:
        traffic_log.info("Node API chat stream request: node=%s app=%s", auth.node_name, app_name)
        auth.require_websocket_token_access(
            websocket=websocket,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CHAT_READ, NodeApiScope.MODS_READ),
        )
        try:
            app = resolve_app(app_name)
            chat.require_relay_app(app)
        except HTTPException as xcp:
            raise auth.websocket_exception_from_http(xcp) from xcp
        await chat.serve_stream(
            websocket=websocket,
            app=app,
            after_revision=after_revision,
        )
