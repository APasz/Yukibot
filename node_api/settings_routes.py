"""HTTP registration for app settings routes."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import Request

from apps._app import App
from .route_contracts import MappingResponse, NodeAuthenticatedRouteService
from .settings import NodeSettingMutationResult, NodeSettingWriteRequest
from node_auth import NodeApiScope


class NodeSettingsRouteService(Protocol):
    """Settings operations required by the settings route registrar."""

    def _resolve_app(self, app_name: str) -> App: ...

    def build_setting_list(self, *, app: App, actor_user_id: int) -> MappingResponse: ...

    async def update_setting(
        self,
        *,
        app: App,
        setting_key: str,
        value: str,
        actor_user_id: int,
    ) -> NodeSettingMutationResult: ...

    async def save_settings(self, *, app: App, actor_user_id: int) -> MappingResponse: ...

    async def reload_settings(self, *, app: App, actor_user_id: int) -> MappingResponse: ...


def register_settings_routes(
    nicegui_app: Any,
    *,
    service: NodeSettingsRouteService,
    auth: NodeAuthenticatedRouteService,
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
        app = service._resolve_app(app_name)
        return service.build_setting_list(app=app, actor_user_id=actor_user_id).to_mapping()

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
        app = service._resolve_app(app_name)
        result: NodeSettingMutationResult = await service.update_setting(
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
        app = service._resolve_app(app_name)
        return (await service.save_settings(app=app, actor_user_id=actor_user_id)).to_mapping()

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
        app = service._resolve_app(app_name)
        return (await service.reload_settings(app=app, actor_user_id=actor_user_id)).to_mapping()
