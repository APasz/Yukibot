"""HTTP registration for configs, Factorio files, saves, and blueprints."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Protocol

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response

from _audit import audit_log
from _security import Power_Level
from apps._app import App
from apps.factorio.node_api import (
    NodeFactorioGenerationUpdateRequest,
    NodeFactorioMapExchangeImportRequest,
)
from apps.satisfactory.node_api import NodeBlueprintMutationResult
from .files import (
    NodeConfigCreateRequest,
    NodeConfigWriteRequest,
    NodeSaveMutationResult,
    NodeSaveRenameRequest,
    NodeSaveUploadTransport,
)
from .storage_service import NodeStorageService
from .route_contracts import HttpExceptionFactory, MappingResponse, NodeAuthenticatedRouteService
from node_auth import NodeApiScope


class NodeStorageRouteContext(Protocol):
    """App-specific operations required by storage routes."""

    def _resolve_app(self, app_name: str) -> App: ...

    def factorio_generation_state(self, *, app: App) -> MappingResponse: ...

    def update_factorio_generation(
        self,
        *,
        app: App,
        update: NodeFactorioGenerationUpdateRequest,
    ) -> MappingResponse: ...

    async def import_factorio_map_exchange_string(
        self,
        *,
        app: App,
        import_request: NodeFactorioMapExchangeImportRequest,
    ) -> MappingResponse: ...

    async def sync_factorio_generation_from_running_world(self, *, app: App) -> MappingResponse: ...

    async def export_factorio_map_exchange_string(self, *, app: App) -> MappingResponse: ...

    def factorio_mod_settings_state(self, *, app: App) -> MappingResponse: ...

    def build_factorio_mod_settings_download_response(self, *, app: App) -> FileResponse: ...

    async def upload_factorio_mod_settings(
        self,
        *,
        app: App,
        upload: UploadFile,
        upload_name: str,
        actor_user_id: int,
    ) -> MappingResponse: ...

    def delete_factorio_mod_settings(self, *, app: App) -> MappingResponse: ...

    def build_blueprint_list(self, app: App, *, actor_user_id: int) -> MappingResponse: ...

    async def upload_blueprint_files(
        self,
        *,
        app: App,
        session_name: str,
        uploads: list[UploadFile],
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult: ...

    def delete_blueprint_file(
        self,
        *,
        app: App,
        blueprint_id: str,
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult: ...


FACTORIO_MOD_SETTINGS_ACCESS_LEVEL = Power_Level.sudo
FACTORIO_GENERATION_ACCESS_LEVEL = Power_Level.sudo


def register_storage_routes(
    nicegui_app: Any,
    *,
    service: NodeStorageRouteContext,
    auth: NodeAuthenticatedRouteService,
    storage: NodeStorageService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register all app-scoped storage and file-management endpoints."""
    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/configs")
    async def _list_configs(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API config list request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        actor_user_id = auth.resolve_actor_if_available(context).actor_user_id
        return storage.build_config_list(app=app, actor_user_id=actor_user_id).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/configs")
    async def _create_config(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API config create request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        create_request = NodeConfigCreateRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_write_level_for_root(create_request.root_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        context = await auth.require_actor_level(
            context,
            required_level,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = storage.create_config_file(
            app=app,
            root_id=create_request.root_id,
            relative_path=create_request.relative_path,
            content=create_request.content,
        )
        audit_log(
            "config.file_created",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            config_id=result.config.id,
            required_level=required_level.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings")
    async def _factorio_mod_settings_state(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio mod settings state request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        await auth.require_actor_level(context, FACTORIO_MOD_SETTINGS_ACCESS_LEVEL)
        return service.factorio_mod_settings_state(app=app).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/generation")
    async def _factorio_generation_state(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio generation state request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        await auth.require_actor_level(context, FACTORIO_GENERATION_ACCESS_LEVEL)
        return service.factorio_generation_state(app=app).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/factorio/generation")
    async def _update_factorio_generation(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio generation update request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            FACTORIO_GENERATION_ACCESS_LEVEL,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = service.update_factorio_generation(
            app=app,
            update=NodeFactorioGenerationUpdateRequest.model_validate(payload),
        )
        audit_log(
            "factorio.generation_updated",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL.name,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/factorio/generation/map-exchange-string")
    async def _import_factorio_map_exchange_string(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio map exchange import request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            FACTORIO_GENERATION_ACCESS_LEVEL,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = await service.import_factorio_map_exchange_string(
            app=app,
            import_request=NodeFactorioMapExchangeImportRequest.model_validate(payload),
        )
        audit_log(
            "factorio.map_exchange_imported",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/generation/map-exchange-string")
    async def _export_factorio_map_exchange_string(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio map exchange export request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        await auth.require_actor_level(context, FACTORIO_GENERATION_ACCESS_LEVEL)
        return (await service.export_factorio_map_exchange_string(app=app)).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/factorio/generation/running-world")
    async def _sync_factorio_generation_from_running_world(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API Factorio running-world generation sync request: node=%s app=%s",
            auth.node_name,
            app_name,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            FACTORIO_GENERATION_ACCESS_LEVEL,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = await service.sync_factorio_generation_from_running_world(app=app)
        audit_log(
            "factorio.running_world_generation_synced",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings/download")
    async def _download_factorio_mod_settings(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> FileResponse:
        traffic_log.info("Node API Factorio mod settings download request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        await auth.require_actor_level(context, FACTORIO_MOD_SETTINGS_ACCESS_LEVEL)
        return service.build_factorio_mod_settings_download_response(app=app)

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings/upload")
    async def _upload_factorio_mod_settings(
        app_name: str,
        request: Request,
        upload: Annotated[UploadFile, File()],
        filename: Annotated[str | None, Form()] = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio mod settings upload request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = await service.upload_factorio_mod_settings(
            app=app,
            upload=upload,
            upload_name=filename or upload.filename or "",
            actor_user_id=actor_user_id,
        )
        audit_log(
            "factorio.mod_settings_uploaded",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            required_level=FACTORIO_MOD_SETTINGS_ACCESS_LEVEL.name,
        )
        return result.to_mapping()

    @nicegui_app.delete(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings")
    async def _delete_factorio_mod_settings(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio mod settings delete request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = service.delete_factorio_mod_settings(app=app)
        audit_log(
            "factorio.mod_settings_deleted",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            required_level=FACTORIO_MOD_SETTINGS_ACCESS_LEVEL.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/configs/roots/{{root_id}}/download")
    async def _download_config_root(
        app_name: str,
        root_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> FileResponse:
        traffic_log.info(
            "Node API config root download request: node=%s app=%s root=%s",
            auth.node_name,
            app_name,
            root_id,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_read_level_for_root(root_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        context = await auth.require_actor_level(
            context,
            required_level,
        )
        actor_user_id = auth.resolve_actor_if_available(context).actor_user_id
        return await storage.build_config_root_download_response(
            app=app,
            root_id=root_id,
            actor_user_id=actor_user_id,
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/configs/{{config_id:path}}")
    async def _read_config(
        app_name: str,
        config_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API config read request: node=%s app=%s config=%s", auth.node_name, app_name, config_id
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_read_level_for_id(config_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        await auth.require_actor_level(context, required_level)
        return storage.read_config_file(app=app, config_id=config_id).to_mapping()

    @nicegui_app.put(f"{api_prefix}/apps/{{app_name}}/configs/{{config_id:path}}")
    async def _write_config(
        app_name: str,
        config_id: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API config write request: node=%s app=%s config=%s", auth.node_name, app_name, config_id
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        write_request = NodeConfigWriteRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_write_level_for_id(config_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        context = await auth.require_actor_level(context, required_level)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = storage.write_config_file(
            app=app,
            config_id=config_id,
            content=write_request.content,
        )
        audit_log(
            "config.file_written",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            config_id=config_id,
            required_level=required_level.name,
        )
        return result.to_mapping()

    @nicegui_app.delete(f"{api_prefix}/apps/{{app_name}}/configs/{{config_id:path}}")
    async def _delete_config(
        app_name: str,
        config_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API config delete request: node=%s app=%s config=%s", auth.node_name, app_name, config_id
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
        )
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_write_level_for_id(config_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        context = await auth.require_actor_level(context, required_level)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = storage.delete_config_file(app=app, config_id=config_id)
        audit_log(
            "config.file_deleted",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            config_id=result.config_id,
            required_level=required_level.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/saves")
    async def _list_saves(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API save list request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_READ,))
        app = service._resolve_app(app_name)
        return (await storage.build_save_list(app)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/saves/{{save_id:path}}/download")
    async def _download_save(
        app_name: str,
        save_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API save download request: node=%s app=%s save=%s", auth.node_name, app_name, save_id
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_DOWNLOAD,))
        app = service._resolve_app(app_name)
        return await storage.build_save_download_response(app=app, save_id=save_id)

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/saves/upload")
    async def _upload_save(
        app_name: str,
        request: Request,
        root_id: Annotated[str, Form()],
        upload: Annotated[UploadFile, File()],
        filename: Annotated[str | None, Form()] = None,
        upload_transport: Annotated[NodeSaveUploadTransport, Form()] = NodeSaveUploadTransport.DIRECT,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API save upload request: node=%s app=%s root=%s transport=%s",
            auth.node_name,
            app_name,
            root_id,
            upload_transport.value,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            app.save_file_write_level,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result: NodeSaveMutationResult = await storage.upload_save_file(
            app=app,
            root_id=root_id,
            upload=upload,
            upload_name=filename,
            actor_user_id=actor_user_id,
            upload_transport=upload_transport,
        )
        audit_log(
            "save.file_uploaded",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            save_id=result.save.id,
            root_id=root_id,
            required_level=app.save_file_write_level.name,
            upload_transport=upload_transport.value,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/saves/{{save_id:path}}/rename")
    async def _rename_save(
        app_name: str,
        save_id: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API save rename request: node=%s app=%s save=%s", auth.node_name, app_name, save_id)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
        )
        rename_request: NodeSaveRenameRequest = NodeSaveRenameRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            app.save_file_write_level,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result: NodeSaveMutationResult = await storage.rename_save_file(
            app=app,
            save_id=save_id,
            new_name=rename_request.new_name,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "save.file_renamed",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            save_id=save_id,
            destination_save_id=result.save.id,
            required_level=app.save_file_write_level.name,
        )
        return result.to_mapping()

    @nicegui_app.delete(f"{api_prefix}/apps/{{app_name}}/saves/{{save_id:path}}")
    async def _delete_save(
        app_name: str,
        save_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API save delete request: node=%s app=%s save=%s", auth.node_name, app_name, save_id)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
        )
        app = service._resolve_app(app_name)
        context = await auth.require_actor_level(
            context,
            app.save_file_write_level,
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result: NodeSaveMutationResult = await storage.delete_save_file(
            app=app,
            save_id=save_id,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "save.file_deleted",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            save_id=save_id,
            required_level=app.save_file_write_level.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/blueprints")
    async def _list_blueprints(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API blueprint list request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_READ,),
        )
        app = service._resolve_app(app_name)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        return service.build_blueprint_list(app, actor_user_id=actor_user_id).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/blueprints/upload")
    async def _upload_blueprint(
        app_name: str,
        request: Request,
        session_name: Annotated[str, Form()],
        upload: Annotated[list[UploadFile], File()],
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API blueprint upload request: node=%s app=%s session=%s",
            auth.node_name,
            app_name,
            session_name,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
        )
        app = service._resolve_app(app_name)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result: NodeBlueprintMutationResult = await service.upload_blueprint_files(
            app=app,
            session_name=session_name,
            uploads=upload,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "blueprint.file_uploaded",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            blueprint_id=result.blueprint.id,
            session_name=session_name,
        )
        return result.to_mapping()

    @nicegui_app.delete(f"{api_prefix}/apps/{{app_name}}/blueprints/{{blueprint_id:path}}")
    async def _delete_blueprint(
        app_name: str,
        blueprint_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API blueprint delete request: node=%s app=%s blueprint=%s",
            auth.node_name,
            app_name,
            blueprint_id,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
        )
        app = service._resolve_app(app_name)
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        result = service.delete_blueprint_file(
            app=app,
            blueprint_id=blueprint_id,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "blueprint.file_deleted",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_name=app.name,
            blueprint_id=blueprint_id,
        )
        return result.to_mapping()
