"""HTTP registration for node-management routes."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import Request

import config
from node_api_app_installer import NodeAppInstallerSettingsMutationResult, NodeAppInstallerSettingsState
from node_api_route_contracts import HttpExceptionFactory, MappingResponse, NodeAuthenticatedRouteService
from node_auth import NodeApiScope


class NodeManagementRouteService(NodeAuthenticatedRouteService, Protocol):
    """Node-management operations exposed through the HTTP API."""

    def read_node_capacity(self) -> config.NodeCapacityProfile: ...

    def read_app_installer_settings(self) -> NodeAppInstallerSettingsState: ...

    def read_node_font_sources(self) -> config.NodeFontSourceSettings: ...

    def read_node_disk_settings(self) -> MappingResponse: ...

    def read_discord_settings(self) -> config.DiscordSettings: ...

    async def mutate_node_capacity(
        self,
        *,
        capacity: config.NodeCapacityProfile,
        actor_user_id: int,
    ) -> MappingResponse: ...

    async def mutate_app_installer_settings(
        self,
        *,
        settings: config.AppInstallerSettings,
        actor_user_id: int,
    ) -> NodeAppInstallerSettingsMutationResult: ...

    async def mutate_node_disk_settings(
        self,
        *,
        preferences: config.PersistedDiskPreferences,
        actor_user_id: int,
    ) -> MappingResponse: ...

    async def mutate_node_font_sources(
        self,
        *,
        settings: config.NodeFontSourceSettings,
        actor_user_id: int,
    ) -> MappingResponse: ...

    async def mutate_discord_settings(
        self,
        *,
        settings: config.DiscordSettings,
        actor_user_id: int,
    ) -> MappingResponse: ...


def register_node_management_routes(
    nicegui_app: Any,
    *,
    service: NodeManagementRouteService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register capacity and node-level settings endpoints."""

    @nicegui_app.get(f"{api_prefix}/node-capacity")
    async def _node_capacity(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API node capacity request: node=%s", service.node_name)
        service._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
        return service.read_node_capacity().model_dump(mode="json")

    @nicegui_app.post(f"{api_prefix}/node-capacity")
    async def _update_node_capacity(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API node capacity update request: node=%s", service.node_name)
        actor_user_id = _actor_user_id(service, request=request, access_token=access_token, scope=NodeApiScope.NODE_MANAGE)
        capacity = config.NodeCapacityProfile.model_validate(payload)
        return (await service.mutate_node_capacity(capacity=capacity, actor_user_id=actor_user_id)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/app-installer-settings")
    async def _app_installer_settings(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API app installer settings request: node=%s", service.node_name)
        service._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
        return service.read_app_installer_settings().to_mapping()

    @nicegui_app.post(f"{api_prefix}/app-installer-settings")
    async def _update_app_installer_settings(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API app installer settings update request: node=%s", service.node_name)
        actor_user_id = _actor_user_id(service, request=request, access_token=access_token, scope=NodeApiScope.NODE_MANAGE)
        settings = config.AppInstallerSettings.model_validate(payload)
        return (await service.mutate_app_installer_settings(settings=settings, actor_user_id=actor_user_id)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/node-disk-settings")
    async def _node_disk_settings(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API node disk settings request: node=%s", service.node_name)
        service._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
        return service.read_node_disk_settings().to_mapping()

    @nicegui_app.post(f"{api_prefix}/node-disk-settings")
    async def _update_node_disk_settings(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API node disk settings update request: node=%s", service.node_name)
        actor_user_id = _actor_user_id(service, request=request, access_token=access_token, scope=NodeApiScope.NODE_MANAGE)
        preferences = config.PersistedDiskPreferences.model_validate(payload)
        try:
            result = await service.mutate_node_disk_settings(preferences=preferences, actor_user_id=actor_user_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/node-font-sources")
    async def _node_font_sources(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API node font sources request: node=%s", service.node_name)
        service._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return service.read_node_font_sources().model_dump(mode="json")

    @nicegui_app.post(f"{api_prefix}/node-font-sources")
    async def _update_node_font_sources(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API node font sources update request: node=%s", service.node_name)
        actor_user_id = _actor_user_id(service, request=request, access_token=access_token, scope=NodeApiScope.NODE_OPERATE)
        settings = config.NodeFontSourceSettings.model_validate(payload)
        return (await service.mutate_node_font_sources(settings=settings, actor_user_id=actor_user_id)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/discord-settings")
    async def _discord_settings(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API Discord settings request: node=%s", service.node_name)
        service._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return service.read_discord_settings().model_dump(mode="json")

    @nicegui_app.post(f"{api_prefix}/discord-settings")
    async def _update_discord_settings(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Discord settings update request: node=%s", service.node_name)
        actor_user_id = _actor_user_id(service, request=request, access_token=access_token, scope=NodeApiScope.NODE_OPERATE)
        settings = config.DiscordSettings.model_validate(payload)
        return (await service.mutate_discord_settings(settings=settings, actor_user_id=actor_user_id)).to_mapping()


def _actor_user_id(
    service: NodeManagementRouteService,
    *,
    request: Request,
    access_token: str | None,
    scope: NodeApiScope,
) -> int:
    grant = service._require_access(request, access_token, app_name=None, scopes=(scope,))
    return service._request_actor_user_id(
        request=request,
        access_token=access_token,
        app_name=None,
        scopes=(scope,),
        verified_grant=grant,
    )
