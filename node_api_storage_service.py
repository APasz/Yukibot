"""App-agnostic configuration storage operations for the node API."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from fastapi import UploadFile
from fastapi.responses import FileResponse, Response

from _file import File_Utils
from _security import Access_Control
from _utils import Utilities
from apps._app import App
from apps._config_files import AppConfigFile, AppConfigFileContent, AppConfigFileRoot
from apps._save_files import AppSaveEntry, AppSaveEntryKind
from node_api_files import (
    NodeConfigContent,
    NodeConfigEntry,
    NodeConfigList,
    NodeConfigMutationResult,
    NodeConfigRootEntry,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSaveRootEntry,
    NodeSaveUploadTransport,
)
from node_api_upload import persist_upload_to_temp


class RuntimeHttpExceptionFactory(Protocol):
    """Translate app runtime failures to an HTTP-compatible exception."""

    def __call__(self, *, app: App, action: str, error: RuntimeError) -> Exception: ...


class NodeStorageService:
    """Owns app-agnostic configuration and save storage operations."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        current_acl: Callable[[], Access_Control | None],
        invalidate_client_pack_content: Callable[[App], None],
        http_exception: Callable[[int, str], Exception],
        runtime_http_exception: RuntimeHttpExceptionFactory,
        traffic_log: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._current_acl = current_acl
        self._invalidate_client_pack_content = invalidate_client_pack_content
        self._http_exception = http_exception
        self._runtime_http_exception = runtime_http_exception
        self._traffic_log = traffic_log

    def build_config_list(
        self, *, app: App, actor_user_id: int | None = None
    ) -> NodeConfigList:
        configs = self._visible_config_files(app=app, actor_user_id=actor_user_id)
        self._traffic_log.info(
            "Node API built config list: node=%s app=%s configs=%s",
            self._node_name(),
            app.name,
            len(configs),
        )
        return NodeConfigList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            configs=tuple(self._config_entry(config_file) for config_file in configs),
            roots=tuple(
                self._config_root_entry(app=app, root=root)
                for root in self._visible_config_roots(
                    app=app, actor_user_id=actor_user_id
                )
            ),
        )

    def read_config_file(self, *, app: App, config_id: str) -> NodeConfigContent:
        try:
            content = app.read_config_file(config_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        return self._config_content(app=app, content=content)

    def write_config_file(
        self, *, app: App, config_id: str, content: str
    ) -> NodeConfigContent:
        try:
            updated = app.write_config_file(config_id, content)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        self._traffic_log.info(
            "Node API wrote config file: node=%s app=%s config=%s",
            self._node_name(),
            app.name,
            config_id,
        )
        self._invalidate_client_pack_content(app)
        return self._config_content(app=app, content=updated)

    def create_config_file(
        self,
        *,
        app: App,
        root_id: str,
        relative_path: str,
        content: str,
    ) -> NodeConfigContent:
        try:
            created = app.create_config_file(
                root_id=root_id, relative_path=relative_path, content=content
            )
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        self._traffic_log.info(
            "Node API created config file: node=%s app=%s config=%s",
            self._node_name(),
            app.name,
            created.file.id,
        )
        self._invalidate_client_pack_content(app)
        return self._config_content(app=app, content=created)

    def delete_config_file(
        self, *, app: App, config_id: str
    ) -> NodeConfigMutationResult:
        try:
            deleted = app.delete_config_file(config_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        self._traffic_log.info(
            "Node API deleted config file: node=%s app=%s config=%s",
            self._node_name(),
            app.name,
            deleted.id,
        )
        self._invalidate_client_pack_content(app)
        return NodeConfigMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            config_id=deleted.id,
            message=f"Deleted {deleted.root_label} / {deleted.relative_path}.",
        )

    async def build_config_root_download_response(
        self,
        *,
        app: App,
        root_id: str,
        actor_user_id: int | None = None,
    ) -> FileResponse:
        try:
            root = app.resolve_config_root(root_id)
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp

        acl = self._current_acl()
        if (
            actor_user_id is not None
            and acl is not None
            and not acl.can(actor_user_id, app.config_file_read_level_for_root(root_id))
        ):
            raise self._http_exception(
                403, f"Insufficient level for config root: {root.label}"
            )

        root_path = root.resolved_path
        if not root_path.exists():
            raise self._http_exception(404, f"Config root does not exist: {root.label}")
        if not root_path.is_file() and not root_path.is_dir():
            raise self._http_exception(404, f"Config root is unsupported: {root.label}")

        visible_configs = tuple(
            config_file
            for config_file in self._visible_config_files(
                app=app, actor_user_id=actor_user_id
            )
            if config_file.root_id == root_id
        )
        if not visible_configs:
            raise self._http_exception(
                404, f"No downloadable config files found in root: {root.label}"
            )
        if root_path.is_file():
            self._traffic_log.info(
                "Node API sending config file root: node=%s app=%s root=%s",
                self._node_name(),
                app.name,
                root_id,
            )
            return FileResponse(path=root_path, filename=root_path.name)

        paths = tuple(
            app.resolve_config_file(config_file.id) for config_file in visible_configs
        )
        archive_path = await File_Utils.compress(
            paths,
            self._config_root_archive_name(app=app, root=root),
            arc_base=root_path,
        )
        self._traffic_log.info(
            "Node API sending config root archive: node=%s app=%s root=%s files=%s archive=%s",
            self._node_name(),
            app.name,
            root_id,
            len(paths),
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_path.name)

    async def build_save_list(self, app: App) -> NodeSaveList:
        saves = await app.list_save_files_async()
        save_can_delete = bool(getattr(app, "supports_save_delete", False))
        self._traffic_log.info(
            "Node API built save list: node=%s app=%s saves=%s",
            self._node_name(),
            app.name,
            len(saves),
        )
        return replace(
            self.build_empty_save_list(app),
            saves=tuple(
                self._save_entry(save, can_delete=save_can_delete) for save in saves
            ),
        )

    def build_empty_save_list(self, app: App) -> NodeSaveList:
        return NodeSaveList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            roots=tuple(
                NodeSaveRootEntry(id=root.id, label=root.label)
                for root in app.save_file_roots
            ),
            saves=(),
        )

    async def build_save_download_response(self, *, app: App, save_id: str) -> Response:
        try:
            custom_archive = await app.download_save_archive(save_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Save download", error=xcp
            ) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Save download failed: {xcp}") from xcp
        if custom_archive is not None:
            filename, archive_path = custom_archive
            if not archive_path.is_file():
                raise self._http_exception(
                    404, f"Save archive does not exist: {archive_path.name}"
                )
            self._traffic_log.info(
                "Node API sending custom save archive: node=%s app=%s save=%s archive=%s",
                self._node_name(),
                app.name,
                save_id,
                archive_path,
            )
            return FileResponse(path=archive_path, filename=filename)

        try:
            custom_download = await app.download_save_content(save_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Save download", error=xcp
            ) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Save download failed: {xcp}") from xcp
        if custom_download is not None:
            filename, content = custom_download
            self._traffic_log.info(
                "Node API sending save content: node=%s app=%s save=%s filename=%s bytes=%s",
                self._node_name(),
                app.name,
                save_id,
                filename,
                len(content),
            )
            return Response(
                content=content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        try:
            save_path = app.resolve_save_file(save_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp

        if not save_path.exists():
            raise self._http_exception(
                404, f"Save file does not exist: {save_path.name}"
            )
        if save_path.is_file():
            self._traffic_log.info(
                "Node API sending save file: node=%s app=%s path=%s",
                self._node_name(),
                app.name,
                save_path,
            )
            return FileResponse(path=save_path, filename=save_path.name)
        if not save_path.is_dir():
            raise self._http_exception(
                404, f"Save path is unsupported: {save_path.name}"
            )

        archive_path = await File_Utils.compress(
            save_path,
            self._save_archive_name(app=app, save_path=save_path),
            arc_base=save_path.parent,
        )
        self._traffic_log.info(
            "Node API sending save archive: node=%s app=%s path=%s archive=%s",
            self._node_name(),
            app.name,
            save_path,
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_path.name)

    async def upload_save_file(
        self,
        *,
        app: App,
        root_id: str,
        upload: UploadFile,
        upload_name: str | None,
        actor_user_id: int,
        upload_transport: NodeSaveUploadTransport = NodeSaveUploadTransport.DIRECT,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_uploads:
            raise self._http_exception(
                409, f"{app.friendly} does not support save uploads."
            )
        resolved_upload_name = (upload_name or upload.filename or "").strip()
        if not resolved_upload_name:
            raise self._http_exception(400, "Save upload filename is required.")

        temp_path = await persist_upload_to_temp(upload)
        try:
            return await self.upload_save_path(
                app=app,
                root_id=root_id,
                source_path=temp_path,
                upload_name=resolved_upload_name,
                actor_user_id=actor_user_id,
                upload_transport=upload_transport,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def upload_save_path(
        self,
        *,
        app: App,
        root_id: str,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
        upload_transport: NodeSaveUploadTransport = NodeSaveUploadTransport.DIRECT,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_uploads:
            raise self._http_exception(
                409, f"{app.friendly} does not support save uploads."
            )
        save_can_delete = bool(getattr(app, "supports_save_delete", False))
        try:
            updated = await app.upload_save_file_async(
                root_id=root_id,
                upload_name=upload_name,
                source_path=source_path,
            )
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise self._http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Save upload", error=xcp
            ) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Save upload failed: {xcp}") from xcp

        self._traffic_log.info(
            "Node API save uploaded: node=%s app=%s root=%s save=%s actor=%s transport=%s",
            self._node_name(),
            app.name,
            root_id,
            updated.id,
            actor_user_id,
            upload_transport.value,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=f"Uploaded save `{updated.label}` for {app.friendly}.",
            save=self._save_entry(updated, can_delete=save_can_delete),
        )

    async def rename_save_file(
        self,
        *,
        app: App,
        save_id: str,
        new_name: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_rename:
            raise self._http_exception(
                409, f"{app.friendly} does not support save renaming."
            )
        save_can_delete = bool(getattr(app, "supports_save_delete", False))
        resolved_name = new_name.strip()
        if not resolved_name:
            raise self._http_exception(400, "Save name must not be empty.")

        try:
            current_save = next(
                save for save in await app.list_save_files_async() if save.id == save_id
            )
        except StopIteration as xcp:
            raise self._http_exception(404, f"Unknown save file: {save_id}") from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Save rename", error=xcp
            ) from xcp

        destination_relative_path = (
            PurePosixPath(current_save.relative_path)
            .with_name(resolved_name)
            .as_posix()
        )
        try:
            updated = await app.relocate_save_file_async(
                save_id=save_id,
                destination_root_id=current_save.root_id,
                destination_relative_path=destination_relative_path,
            )
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise self._http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Save rename", error=xcp
            ) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Save rename failed: {xcp}") from xcp

        self._traffic_log.info(
            "Node API save renamed: node=%s app=%s save=%s renamed_to=%s actor=%s",
            self._node_name(),
            app.name,
            save_id,
            updated.id,
            actor_user_id,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=f"Renamed save to `{updated.label}` for {app.friendly}.",
            save=self._save_entry(updated, can_delete=save_can_delete),
        )

    async def delete_save_file(
        self,
        *,
        app: App,
        save_id: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_delete:
            raise self._http_exception(
                409, f"{app.friendly} does not support save deletion."
            )
        save_can_delete = bool(getattr(app, "supports_save_delete", False))
        try:
            deleted = await app.delete_save_file_async(file_id=save_id)
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(
                app=app, action="Save delete", error=xcp
            ) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Save delete failed: {xcp}") from xcp

        self._traffic_log.info(
            "Node API save deleted: node=%s app=%s save=%s actor=%s",
            self._node_name(),
            app.name,
            save_id,
            actor_user_id,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=f"Deleted save `{deleted.label}` from {app.friendly}.",
            save=self._save_entry(deleted, can_delete=save_can_delete),
        )

    def _visible_config_files(
        self, *, app: App, actor_user_id: int | None
    ) -> tuple[AppConfigFile, ...]:
        configs = app.list_config_files()
        acl = self._current_acl()
        if actor_user_id is None or acl is None:
            return configs
        return tuple(
            config_file
            for config_file in configs
            if acl.can(actor_user_id, config_file.read_power_level)
        )

    def _visible_config_roots(
        self, *, app: App, actor_user_id: int | None
    ) -> tuple[AppConfigFileRoot, ...]:
        roots = app.config_file_roots
        acl = self._current_acl()
        if actor_user_id is None or acl is None:
            return roots
        return tuple(
            root
            for root in roots
            if acl.can(actor_user_id, app.config_file_read_level_for_root(root.id))
        )

    @staticmethod
    def _config_entry(config_file: AppConfigFile) -> NodeConfigEntry:
        return NodeConfigEntry(
            id=config_file.id,
            label=config_file.label,
            relative_path=config_file.relative_path,
            root_id=config_file.root_id,
            root_label=config_file.root_label,
            kind=config_file.kind.value,
            read_power_level=config_file.read_power_level,
            write_power_level=config_file.write_power_level,
            size_bytes=config_file.size_bytes,
            size_text=Utilities.humanise_bytes(config_file.size_bytes),
            modified_at=config_file.modified_at.isoformat(sep=" ", timespec="seconds"),
            can_write=config_file.can_write,
            can_delete=config_file.can_delete,
            write_notice=config_file.write_notice,
        )

    @staticmethod
    def _config_root_entry(*, app: App, root: AppConfigFileRoot) -> NodeConfigRootEntry:
        return NodeConfigRootEntry(
            id=root.id,
            label=root.label,
            kind=root.kind.value,
            read_power_level=app.config_file_read_level_for_root(root.id),
            write_power_level=app.config_file_write_level_for_root(root.id),
            can_create=root.allow_file_creation,
        )

    def _config_content(
        self, *, app: App, content: AppConfigFileContent
    ) -> NodeConfigContent:
        return NodeConfigContent(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            config=self._config_entry(content.file),
            content=content.content,
        )

    @staticmethod
    def _config_root_archive_name(*, app: App, root: AppConfigFileRoot) -> str:
        app_name = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in app.friendly.strip()
        )
        root_name = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in root.id.strip()
        )
        return f"{app_name.strip('_') or app.name}_{root_name.strip('_') or root.id}_configs.zip"

    @staticmethod
    def _save_entry(
        save_file: AppSaveEntry, *, can_delete: bool = False
    ) -> NodeSaveEntry:
        size_bytes = save_file.size_bytes
        size_text = (
            "Directory"
            if save_file.kind is AppSaveEntryKind.DIRECTORY
            else Utilities.humanise_bytes(size_bytes)
        )
        return NodeSaveEntry(
            id=save_file.id,
            label=save_file.label,
            relative_path=save_file.relative_path,
            root_id=save_file.root_id,
            root_label=save_file.root_label,
            kind=save_file.kind.value,
            size_bytes=size_bytes,
            size_text=size_text,
            modified_at=save_file.modified_at.isoformat(sep=" ", timespec="seconds"),
            can_delete=can_delete,
        )

    @staticmethod
    def _save_archive_name(*, app: App, save_path: Path) -> str:
        app_name = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in app.friendly.strip()
        )
        save_name = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in save_path.name.strip()
        )
        base_app_name = app_name.strip("_") or app.name
        base_save_name = save_name.strip("_") or save_path.name
        return f"{base_app_name}_{base_save_name}.zip"
