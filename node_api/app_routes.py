"""HTTP registration for app summaries, runtime state, and app control."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import Response

from _async_utils import run_blocking
from apps._app import App
from apps.minecraft.node_api import NodeMinecraftRecipeMutationRequest
from .app_game_service import NodeAppGameService
from .app_state import (
    NodeAppMutationService,
    NodeAppMutationRequest,
    NodeAppRuntimeSummary,
    required_app_mutation_scope,
)
from .mod_service import NodeModService
from .realtime_service import NodeRealtimeService
from .route_contracts import (
    HttpExceptionFactory,
    NodeAuthenticatedRouteService,
)
from node_auth import NodeApiScope


def register_app_routes(
    nicegui_app: Any,
    *,
    auth: NodeAuthenticatedRouteService,
    resolve_app: Callable[[str], App],
    mod_service: NodeModService,
    build_cached_runtime_summary: Callable[[App], Awaitable[NodeAppRuntimeSummary]],
    games: NodeAppGameService,
    mutations: NodeAppMutationService,
    realtime: NodeRealtimeService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register app catalogue extensions, runtime, and app-control endpoints."""

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/mods")
    async def _list_mods(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API mods list request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        app = resolve_app(app_name)
        return (await mod_service.build_mod_list(app)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/runtime")
    async def _runtime_summary(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API runtime summary request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        app = resolve_app(app_name)
        return (await build_cached_runtime_summary(app)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/sevendays/sandbox-options")
    async def _sevendays_sandbox_options(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API 7D2D sandbox options request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        app = resolve_app(app_name)
        return games.build_sevendays_sandbox_options_state(app).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/minecraft/recipes")
    async def _minecraft_recipe_workspace(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API Minecraft recipe workspace request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        app = resolve_app(app_name)
        return games.build_minecraft_recipe_workspace_state(app).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/minecraft/recipes/mutations")
    async def _mutate_minecraft_recipe(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API Minecraft recipe mutation request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.APP_MANAGE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        try:
            mutation_request = NodeMinecraftRecipeMutationRequest.from_mapping(payload)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        app = resolve_app(app_name)
        result = await games.mutate_minecraft_recipe_book(
            app=app,
            mutation_request=mutation_request,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/minecraft/recipes/item-icon")
    async def _minecraft_recipe_item_icon(
        app_name: str,
        item_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API Minecraft recipe item icon request: node=%s app=%s item=%s",
            auth.node_name,
            app_name,
            item_id,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        app = resolve_app(app_name)
        return await run_blocking(games.build_minecraft_item_icon_response, app, item_id=item_id)

    @nicegui_app.websocket(f"{api_prefix}/apps/{{app_name}}/state/stream")
    async def _app_state_stream(
        websocket: WebSocket,
        app_name: str,
        access_token: str | None = None,
    ) -> None:
        traffic_log.info(
            "Node API app state stream request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        auth.require_websocket_token_access(
            websocket=websocket,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.APPS_READ, NodeApiScope.MODS_READ),
        )
        try:
            app = resolve_app(app_name)
        except HTTPException as xcp:
            raise auth.websocket_exception_from_http(xcp) from xcp
        await realtime.serve_app_state_stream(websocket, app)

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mutate")
    async def _mutate_app(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API app mutation request: node=%s app=%s", auth.node_name, app_name)
        mutation_request: NodeAppMutationRequest = NodeAppMutationRequest.model_validate(payload)
        required_scope = required_app_mutation_scope(mutation_request.action)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(required_scope,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = resolve_app(app_name)
        result = await mutations.mutate(
            app=app,
            action=mutation_request.action,
            actor_user_id=actor_user_id,
            friendly_name=mutation_request.friendly_name,
            title_font_preset=mutation_request.title_font_preset,
            notes=mutation_request.notes,
            lifecycle_notice_started=mutation_request.lifecycle_notice_started,
            lifecycle_notice_stopped=mutation_request.lifecycle_notice_stopped,
            lifecycle_notice_crashed=mutation_request.lifecycle_notice_crashed,
            relay_notice_player_session=mutation_request.relay_notice_player_session,
            relay_notice_player_death=mutation_request.relay_notice_player_death,
            relay_notice_progress=mutation_request.relay_notice_progress,
            relay_advancements_enabled=mutation_request.relay_advancements_enabled,
            factorio_chat_relay_use_shout=mutation_request.factorio_chat_relay_use_shout,
            rcon_requires_online_players=mutation_request.rcon_requires_online_players,
            disabled_activity_provider_ids=mutation_request.disabled_activity_provider_ids,
            running_cpu_points=mutation_request.running_cpu_points,
            running_ram_points=mutation_request.running_ram_points,
            startup_cpu_points=mutation_request.startup_cpu_points,
            startup_ram_points=mutation_request.startup_ram_points,
            steam_update_enabled=mutation_request.steam_update_enabled,
            steam_update_selected_branch=mutation_request.steam_update_selected_branch,
            update_branch_id=mutation_request.update_branch_id,
        )
        return result.to_mapping()
