"""HTTP registration for app settings routes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request

from apps._app import App
from .app_operations import NodeAppOperationsService
from .route_contracts import NodeAuthenticatedRouteService
from .settings import NodeSettingMutationResult, NodeSettingWriteRequest
from node_auth import NodeApiScope


def register_settings_routes(
    nicegui_app: Any,
    *,
    auth: NodeAuthenticatedRouteService,
    resolve_app: Callable[[str], App],
    operations: NodeAppOperationsService,
    api_prefix: str,
    traffic_log: logging.Logger,
) -> None:
    """Register per-app setting read, write, save, and reload endpoints."""
    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/settings")
    async def _list_settings(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API setting list request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_READ,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = resolve_app(app_name)
        return operations.build_setting_list(app=app, actor_user_id=actor_user_id).to_mapping()

    @nicegui_app.put(f"{api_prefix}/apps/{{app_name}}/settings/{{setting_key}}")
    async def _write_setting(
        app_name: str,
        setting_key: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API setting write request: node=%s app=%s setting=%s", auth.node_name, app_name, setting_key
        )
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_WRITE,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        write_request: NodeSettingWriteRequest = NodeSettingWriteRequest.model_validate(payload)
        app = resolve_app(app_name)
        result: NodeSettingMutationResult = await operations.update_setting(
            app=app,
            setting_key=setting_key,
            value=write_request.value,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/settings/save")
    async def _save_settings(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API settings save request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_WRITE,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = resolve_app(app_name)
        return (await operations.save_settings(app=app, actor_user_id=actor_user_id)).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/settings/reload")
    async def _reload_settings(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API settings reload request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_WRITE,)
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = resolve_app(app_name)
        return (await operations.reload_settings(app=app, actor_user_id=actor_user_id)).to_mapping()
