"""HTTP registration for mod inventory, mutation, and client-pack routes."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Protocol

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from _audit import audit_log
from _security import Power_Level
from apps._app import App
from apps._config import (
    BulkLauncherMetadataDiscovery,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    ModPageDiscovery,
    ModPlacement,
)
from apps.factorio.node_api import (
    NodeModDependencyResolutionResult,
    NodeModPortalInstallRequest,
    NodeModPortalVersionList,
    NodeModUpdateCheckResult,
    NodeModUpdateRequest,
)
from apps.minecraft.pack_export import PackFormat, PackPurpose
from .mod import (
    NodeBulkLauncherMetadataApplyRequest,
    NodeBulkLauncherMetadataApplyResult,
    NodeBulkLauncherMetadataRequest,
    NodeClientPackConfigUpdateRequest,
    NodeClientPackPublishRequest,
    NodeDownloadRequest,
    NodeModMetadataFetchRequest,
    NodeModMetadataResolveRequest,
    NodeModMutationAction,
    NodeModMutationRequest,
    NodeModMutationResult,
    NodeModNotesUpdateRequest,
    NodeModPageResolveRequest,
    NodeModPropertiesUpdateRequest,
    NodeModUploadBatchResult,
)
from .route_contracts import NodeAuthenticatedRouteService
from node_auth import NodeApiScope


class NodeModRouteService(Protocol):
    """Mod operations exposed through the HTTP API."""

    def _resolve_app(self, app_name: str) -> App: ...

    async def build_mod_download_response(self, *, app: App, request: NodeDownloadRequest) -> FileResponse: ...

    async def upload_mod_files(
        self,
        *,
        app: App,
        uploads: Sequence[UploadFile],
        upload_names: Sequence[str] | None,
        actor_user_id: int,
        placement: ModPlacement,
    ) -> NodeModUploadBatchResult: ...

    async def install_mod_from_link(
        self,
        *,
        app: App,
        url: str,
        actor_user_id: int,
        selected_mod_ids: Sequence[str] | None,
        version: str | None,
    ) -> NodeModUploadBatchResult: ...

    async def resolve_mod_link_dependencies(
        self,
        *,
        app: App,
        url: str,
        version: str | None,
    ) -> NodeModDependencyResolutionResult: ...

    async def list_mod_link_versions(self, *, app: App, url: str) -> NodeModPortalVersionList: ...

    async def mutate_mod(
        self,
        *,
        app: App,
        mod_name: str,
        action: NodeModMutationAction,
        actor_user_id: int,
    ) -> NodeModMutationResult: ...

    async def check_mod_update(
        self,
        *,
        app: App,
        mod_name: str,
        version: str | None,
    ) -> NodeModUpdateCheckResult: ...

    async def list_installed_mod_versions(self, *, app: App, mod_name: str) -> NodeModPortalVersionList: ...

    async def update_mod(
        self,
        *,
        app: App,
        mod_name: str,
        actor_user_id: int,
        version: str | None,
    ) -> NodeModUploadBatchResult: ...

    async def update_mod_properties(
        self,
        *,
        app: App,
        mod_name: str,
        update: NodeModPropertiesUpdateRequest,
        actor_user_id: int,
    ) -> NodeModMutationResult: ...

    async def update_mod_notes(
        self,
        *,
        app: App,
        mod_name: str,
        notes: str | None,
        actor_user_id: int,
    ) -> NodeModMutationResult: ...

    async def fetch_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        fetch_request: NodeModMetadataFetchRequest,
        actor_user_id: int,
    ) -> LauncherMetadataResolution: ...

    async def resolve_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: NodeModMetadataResolveRequest,
        actor_user_id: int,
    ) -> LauncherMetadataDiscovery: ...

    async def find_mod_pages(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: NodeModPageResolveRequest,
        actor_user_id: int,
    ) -> ModPageDiscovery: ...

    async def run_bulk_metadata_operation(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
        action: Callable[[], Awaitable[BaseModel]],
    ) -> BaseModel: ...

    def cancel_bulk_metadata_operation(self, *, app_name: str, operation_id: uuid.UUID) -> bool: ...

    async def discover_bulk_mod_metadata(
        self,
        *,
        app: App,
        discovery_request: NodeBulkLauncherMetadataRequest,
        actor_user_id: int,
    ) -> BulkLauncherMetadataDiscovery: ...

    async def apply_bulk_mod_metadata(
        self,
        *,
        app: App,
        apply_request: NodeBulkLauncherMetadataApplyRequest,
        actor_user_id: int,
    ) -> NodeBulkLauncherMetadataApplyResult: ...

    async def update_client_pack_config(
        self,
        *,
        app: App,
        update: NodeClientPackConfigUpdateRequest,
        actor_user_id: int,
    ) -> dict[str, object]: ...

    async def publish_client_pack_config(
        self,
        *,
        app: App,
        update: NodeClientPackPublishRequest,
        actor_user_id: int,
    ) -> dict[str, object]: ...


def register_mod_routes(
    nicegui_app: Any,
    *,
    service: NodeModRouteService,
    auth: NodeAuthenticatedRouteService,
    api_prefix: str,
    traffic_log: logging.Logger,
) -> None:
    """Register all app-scoped mod and client-pack endpoints."""

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/mods/download")
    async def _download_mods(
        app_name: str,
        request: Request,
        enabled_only: bool = False,
        selected_only: bool = False,
        excluded_only: bool = False,
        client_pack: bool = False,
        pack_purpose: PackPurpose | None = None,
        pack_format: PackFormat = PackFormat.GENERIC_ZIP,
        publish_client_pack: bool = False,
        publish_changelog: str | None = None,
        include_kubejs_scripts: bool = True,
        include_servers_dat: bool = True,
        include_options_txt: bool = True,
        access_token: str | None = None,
    ) -> FileResponse:
        mod_names = tuple(request.query_params.getlist("mod_name"))
        traffic_log.info(
            "Node API mods archive request: node=%s app=%s enabled_only=%s selected_only=%s "
            "excluded_only=%s client_pack=%s purpose=%s format=%s selected=%s",
            auth.node_name,
            app_name,
            enabled_only,
            selected_only,
            excluded_only,
            client_pack,
            pack_purpose,
            pack_format,
            len(mod_names),
        )
        required_scopes = (NodeApiScope.MODS_DOWNLOAD,)
        if pack_purpose in {PackPurpose.SERVER, PackPurpose.ADMIN} or publish_client_pack:
            required_scopes = (NodeApiScope.MODS_DOWNLOAD, NodeApiScope.MODS_WRITE)
        auth.require_access(request, access_token, app_name=app_name, scopes=required_scopes)
        app = service._resolve_app(app_name)
        return await service.build_mod_download_response(
            app=app,
            request=NodeDownloadRequest(
                enabled_only=enabled_only,
                mod_names=mod_names,
                selected_only=selected_only,
                excluded_only=excluded_only,
                client_pack=client_pack,
                pack_purpose=pack_purpose,
                pack_format=pack_format,
                publish_client_pack=publish_client_pack,
                publish_changelog=publish_changelog,
                include_kubejs_scripts=include_kubejs_scripts,
                include_servers_dat=include_servers_dat,
                include_options_txt=include_options_txt,
            ),
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/download")
    async def _download_mod(
        app_name: str,
        mod_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> FileResponse:
        traffic_log.info(
            "Node API single mod request: node=%s app=%s mod=%s",
            auth.node_name,
            app_name,
            mod_name,
        )
        auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        app = service._resolve_app(app_name)
        return await service.build_mod_download_response(
            app=app,
            request=NodeDownloadRequest(mod_name=mod_name),
        )

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/upload")
    async def _upload_mod(
        app_name: str,
        request: Request,
        upload: Annotated[list[UploadFile], File()],
        filename: Annotated[list[str] | None, Form()] = None,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API mod upload request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = service._resolve_app(app_name)
        result = await service.upload_mod_files(
            app=app,
            uploads=upload,
            upload_names=filename,
            actor_user_id=actor_user_id,
            placement=placement,
        )
        audit_log(
            "mod.file_uploaded",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            mod_name=",".join(mod.name for mod in result.mods),
            required_level=Power_Level.user.name,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/install-link")
    async def _install_mod_link(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod link install request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        install_request = NodeModPortalInstallRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = service._resolve_app(app_name)
        result = await service.install_mod_from_link(
            app=app,
            url=install_request.url,
            actor_user_id=actor_user_id,
            selected_mod_ids=install_request.selected_mod_ids,
            version=install_request.version,
        )
        audit_log(
            "mod.link_installed",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            mod_name=",".join(mod.name for mod in result.mods),
            required_level=Power_Level.user.name,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/resolve-link")
    async def _resolve_mod_link(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod link resolve request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        resolve_request = NodeModPortalInstallRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        result = await service.resolve_mod_link_dependencies(
            app=app,
            url=resolve_request.url,
            version=resolve_request.version,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/resolve-link/versions")
    async def _resolve_mod_link_versions(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod link version list request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        resolve_request = NodeModPortalInstallRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        result = await service.list_mod_link_versions(app=app, url=resolve_request.url)
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/mutate")
    async def _mutate_mod(
        app_name: str,
        mod_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod mutation request: node=%s app=%s mod=%s",
            auth.node_name,
            app_name,
            mod_name,
        )
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        mutation_request = NodeModMutationRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = service._resolve_app(app_name)
        result = await service.mutate_mod(
            app=app,
            mod_name=mod_name,
            action=mutation_request.action,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/check-update")
    async def _check_mod_update(
        app_name: str,
        mod_name: str,
        request: Request,
        version: str | None = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod update check request: node=%s app=%s mod=%s",
            auth.node_name,
            app_name,
            mod_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        update_request = NodeModUpdateRequest.model_validate({"version": version})
        app = service._resolve_app(app_name)
        result = await service.check_mod_update(app=app, mod_name=mod_name, version=update_request.version)
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/versions")
    async def _list_mod_versions(
        app_name: str,
        mod_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod version list request: node=%s app=%s mod=%s",
            auth.node_name,
            app_name,
            mod_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
        app = service._resolve_app(app_name)
        result = await service.list_installed_mod_versions(app=app, mod_name=mod_name)
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/update")
    async def _update_mod(
        app_name: str,
        mod_name: str,
        request: Request,
        payload: dict[str, object] | None = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod update request: node=%s app=%s mod=%s",
            auth.node_name,
            app_name,
            mod_name,
        )
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        update_request = NodeModUpdateRequest.model_validate(payload or {})
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        app = service._resolve_app(app_name)
        result = await service.update_mod(
            app=app,
            mod_name=mod_name,
            actor_user_id=actor_user_id,
            version=update_request.version,
        )
        audit_log(
            "mod.updated",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            mod_name=",".join(mod.name for mod in result.mods),
            required_level=Power_Level.user.name,
        )
        return result.to_mapping()

    @nicegui_app.put(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/properties")
    async def _update_mod_properties(
        app_name: str,
        mod_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API mod properties update request: node=%s app=%s mod=%s",
            auth.node_name,
            app_name,
            mod_name,
        )
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        update_request = NodeModPropertiesUpdateRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = await service.update_mod_properties(
            app=service._resolve_app(app_name),
            mod_name=mod_name,
            update=update_request,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()

    @nicegui_app.put(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/notes")
    async def _update_mod_notes(
        app_name: str,
        mod_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        update_request = NodeModNotesUpdateRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = await service.update_mod_notes(
            app=service._resolve_app(app_name),
            mod_name=mod_name,
            notes=update_request.notes,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/launcher-metadata")
    async def _fetch_mod_launcher_metadata(
        app_name: str,
        mod_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        fetch_request = NodeModMetadataFetchRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        resolution = await service.fetch_mod_launcher_metadata(
            app=service._resolve_app(app_name),
            mod_name=mod_name,
            fetch_request=fetch_request,
            actor_user_id=actor_user_id,
        )
        return resolution.model_dump(mode="json")

    @nicegui_app.post(
        f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/launcher-metadata/resolve"
    )
    async def _resolve_mod_launcher_metadata(
        app_name: str,
        mod_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        resolve_request = NodeModMetadataResolveRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        discovery = await service.resolve_mod_launcher_metadata(
            app=service._resolve_app(app_name),
            mod_name=mod_name,
            resolve_request=resolve_request,
            actor_user_id=actor_user_id,
        )
        return discovery.model_dump(mode="json")

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/{{mod_name}}/mod-pages/resolve")
    async def _resolve_mod_pages(
        app_name: str,
        mod_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        resolve_request = NodeModPageResolveRequest.model_validate(payload)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        discovery = await service.find_mod_pages(
            app=service._resolve_app(app_name),
            mod_name=mod_name,
            resolve_request=resolve_request,
            actor_user_id=actor_user_id,
        )
        return discovery.model_dump(mode="json")

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/metadata/discover")
    async def _discover_bulk_mod_metadata(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        discovery_request = NodeBulkLauncherMetadataRequest.model_validate(payload)
        discovery = await service.run_bulk_metadata_operation(
            app_name=app_name,
            operation_id=discovery_request.operation_id,
            action=lambda: service.discover_bulk_mod_metadata(
                app=service._resolve_app(app_name),
                discovery_request=discovery_request,
                actor_user_id=actor_user_id,
            ),
        )
        return discovery.model_dump(mode="json")

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/metadata/apply")
    async def _apply_bulk_mod_metadata(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        apply_request = NodeBulkLauncherMetadataApplyRequest.model_validate(payload)
        result = await service.run_bulk_metadata_operation(
            app_name=app_name,
            operation_id=apply_request.operation_id,
            action=lambda: service.apply_bulk_mod_metadata(
                app=service._resolve_app(app_name),
                apply_request=apply_request,
                actor_user_id=actor_user_id,
            ),
        )
        return result.model_dump(mode="json")

    @nicegui_app.post(
        f"{api_prefix}/apps/{{app_name}}/mods/metadata/{{operation_id}}/cancel"
    )
    async def _cancel_bulk_mod_metadata(
        app_name: str,
        operation_id: uuid.UUID,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_WRITE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        cancelled = service.cancel_bulk_metadata_operation(
            app_name=app_name,
            operation_id=operation_id,
        )
        traffic_log.info(
            "Node API bulk mod metadata cancellation: node=%s app=%s operation=%s "
            "cancelled=%s actor=%s",
            auth.node_name,
            app_name,
            operation_id,
            cancelled,
            actor_user_id,
        )
        return {"operation_id": str(operation_id), "cancelled": cancelled}

    @nicegui_app.put(f"{api_prefix}/apps/{{app_name}}/mods/client-pack-config")
    async def _update_client_pack_config(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API client-pack configuration request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        return await service.update_client_pack_config(
            app=service._resolve_app(app_name),
            update=NodeClientPackConfigUpdateRequest.model_validate(payload),
            actor_user_id=actor_user_id,
        )

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/mods/client-pack-config/publish")
    async def _publish_client_pack_config(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        context = auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        return await service.publish_client_pack_config(
            app=service._resolve_app(app_name),
            update=NodeClientPackPublishRequest.model_validate(payload),
            actor_user_id=actor_user_id,
        )
