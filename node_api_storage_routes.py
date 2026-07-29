# pyright: reportImportCycles=false
"""HTTP registration for configs, Factorio files, saves, and blueprints."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response

from _audit import audit_log
from node_api_files import NodeConfigWriteRequest, NodeSaveRenameRequest, NodeSaveUploadTransport
from node_api_route_contracts import HttpExceptionFactory
from node_auth import NodeAccessGrant, NodeApiScope

if TYPE_CHECKING:
    from node_api import NodeApiService


def register_storage_routes(
    nicegui_app: Any,
    *,
    service: NodeApiService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register all app-scoped storage and file-management endpoints."""
    from node_api import (
        FACTORIO_GENERATION_ACCESS_LEVEL,
        FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
        NodeBlueprintMutationResult,
        NodeFactorioGenerationUpdateRequest,
        NodeFactorioMapExchangeImportRequest,
        NodeSaveMutationResult,
    )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/configs")
    async def _list_configs(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API config list request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        actor_user_id = service._request_actor_user_id_if_available(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            verified_grant=grant,
        )
        return service.build_config_list(app, actor_user_id=actor_user_id).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings")
    async def _factorio_mod_settings_state(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio mod settings state request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            required_level=FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
            verified_grant=grant,
        )
        return service.factorio_mod_settings_state(app=app).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/generation")
    async def _factorio_generation_state(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio generation state request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL,
            verified_grant=grant,
        )
        return service.factorio_generation_state(app=app).to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/factorio/generation")
    async def _update_factorio_generation(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio generation update request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL,
            verified_grant=grant,
        )
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            verified_grant=grant,
        )
        result = service.update_factorio_generation(
            app=app,
            update=NodeFactorioGenerationUpdateRequest.model_validate(payload),
        )
        audit_log(
            "factorio.generation_updated",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
        traffic_log.info("Node API Factorio map exchange import request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL,
            verified_grant=grant,
        )
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            verified_grant=grant,
        )
        result = await service.import_factorio_map_exchange_string(
            app=app,
            import_request=NodeFactorioMapExchangeImportRequest.model_validate(payload),
        )
        audit_log(
            "factorio.map_exchange_imported",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
        traffic_log.info("Node API Factorio map exchange export request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            required_level=FACTORIO_GENERATION_ACCESS_LEVEL,
            verified_grant=grant,
        )
        return (await service.export_factorio_map_exchange_string(app=app)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings/download")
    async def _download_factorio_mod_settings(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> FileResponse:
        traffic_log.info("Node API Factorio mod settings download request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            required_level=FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
            verified_grant=grant,
        )
        return service.build_factorio_mod_settings_download_response(app=app)

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/factorio/mod-settings/upload")
    async def _upload_factorio_mod_settings(
        app_name: str,
        request: Request,
        upload: Annotated[UploadFile, File()],
        filename: Annotated[str | None, Form()] = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API Factorio mod settings upload request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            required_level=FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
            verified_grant=grant,
        )
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            verified_grant=grant,
        )
        result = await service.upload_factorio_mod_settings(
            app=app,
            upload=upload,
            upload_name=filename or upload.filename or "",
            actor_user_id=actor_user_id,
        )
        audit_log(
            "factorio.mod_settings_uploaded",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
        traffic_log.info("Node API Factorio mod settings delete request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            required_level=FACTORIO_MOD_SETTINGS_ACCESS_LEVEL,
            verified_grant=grant,
        )
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            verified_grant=grant,
        )
        result = service.delete_factorio_mod_settings(app=app)
        audit_log(
            "factorio.mod_settings_deleted",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
            service.node_name,
            app_name,
            root_id,
        )
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_read_level_for_root(root_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            required_level=required_level,
            verified_grant=grant,
        )
        actor_user_id = service._request_actor_user_id_if_available(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            verified_grant=grant,
        )
        return await service.build_config_root_download_response(app=app, root_id=root_id, actor_user_id=actor_user_id)

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/configs/{{config_id:path}}")
    async def _read_config(
        app_name: str,
        config_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API config read request: node=%s app=%s config=%s", service.node_name, app_name, config_id
        )
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_read_level_for_id(config_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
            required_level=required_level,
            verified_grant=grant,
        )
        return service.read_config_file(app=app, config_id=config_id).to_mapping()

    @nicegui_app.put(f"{api_prefix}/apps/{{app_name}}/configs/{{config_id:path}}")
    async def _write_config(
        app_name: str,
        config_id: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API config write request: node=%s app=%s config=%s", service.node_name, app_name, config_id
        )
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
        write_request = NodeConfigWriteRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        try:
            required_level = app.config_file_write_level_for_id(config_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            required_level=required_level,
            verified_grant=grant,
        )
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE,),
            verified_grant=grant,
        )
        result = service.write_config_file(app=app, config_id=config_id, content=write_request.content)
        audit_log(
            "config.file_written",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
            app_name=app.name,
            config_id=config_id,
            required_level=required_level.name,
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/saves")
    async def _list_saves(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API save list request: node=%s app=%s", service.node_name, app_name)
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_READ,))
        app = service._resolve_app(app_name)
        return (await service.build_save_list(app)).to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/saves/{{save_id:path}}/download")
    async def _download_save(
        app_name: str,
        save_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API save download request: node=%s app=%s save=%s", service.node_name, app_name, save_id
        )
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_DOWNLOAD,))
        app = service._resolve_app(app_name)
        return await service.build_save_download_response(app=app, save_id=save_id)

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
            service.node_name,
            app_name,
            root_id,
            upload_transport.value,
        )
        grant: NodeAccessGrant | None = service._require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_WRITE,)
        )
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            required_level=app.save_file_write_level,
            verified_grant=grant,
        )
        actor_user_id: int = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            verified_grant=grant,
        )
        result: NodeSaveMutationResult = await service.upload_save_file(
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
            node_name=service.node_name,
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
        traffic_log.info("Node API save rename request: node=%s app=%s save=%s", service.node_name, app_name, save_id)
        grant: NodeAccessGrant | None = service._require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_WRITE,)
        )
        rename_request: NodeSaveRenameRequest = NodeSaveRenameRequest.model_validate(payload)
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            required_level=app.save_file_write_level,
            verified_grant=grant,
        )
        actor_user_id: int = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            verified_grant=grant,
        )
        result: NodeSaveMutationResult = await service.rename_save_file(
            app=app,
            save_id=save_id,
            new_name=rename_request.new_name,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "save.file_renamed",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
        traffic_log.info("Node API save delete request: node=%s app=%s save=%s", service.node_name, app_name, save_id)
        grant: NodeAccessGrant | None = service._require_access(
            request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_WRITE,)
        )
        app = service._resolve_app(app_name)
        await service._require_actor_level_for_request(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            required_level=app.save_file_write_level,
            verified_grant=grant,
        )
        actor_user_id: int = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_WRITE,),
            verified_grant=grant,
        )
        result: NodeSaveMutationResult = await service.delete_save_file(
            app=app,
            save_id=save_id,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "save.file_deleted",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
        traffic_log.info("Node API blueprint list request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.BLUEPRINTS_READ,))
        app = service._resolve_app(app_name)
        actor_user_id: int = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_READ,),
            verified_grant=grant,
        )
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
            service.node_name,
            app_name,
            session_name,
        )
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.BLUEPRINTS_WRITE,))
        app = service._resolve_app(app_name)
        actor_user_id: int = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
            verified_grant=grant,
        )
        result: NodeBlueprintMutationResult = await service.upload_blueprint_files(
            app=app,
            session_name=session_name,
            uploads=upload,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "blueprint.file_uploaded",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
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
            service.node_name,
            app_name,
            blueprint_id,
        )
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.BLUEPRINTS_WRITE,))
        app = service._resolve_app(app_name)
        actor_user_id: int = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
            verified_grant=grant,
        )
        result = service.delete_blueprint_file(
            app=app,
            blueprint_id=blueprint_id,
            actor_user_id=actor_user_id,
        )
        audit_log(
            "blueprint.file_deleted",
            actor_user_id=actor_user_id,
            node_name=service.node_name,
            app_name=app.name,
            blueprint_id=blueprint_id,
        )
        return result.to_mapping()
