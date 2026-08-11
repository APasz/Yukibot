"""Client-pack assembly, publication, and configuration for node API apps."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from fastapi.responses import FileResponse

import config
from _async_utils import run_blocking
from _authority import AuthorityResource, read_json_object
from _mod_ops import (
    ArchiveDataEntry,
    ArchiveEntry,
    ClientPackSelection,
    ClientPackValidationError,
    ModArchiveEntry,
    NonDownloadableModError,
    build_admin_pack_entries,
    build_client_pack_entries,
    build_server_pack_entries,
    client_pack_content_hash,
    compress_mod_archive_entries,
    require_downloadable,
)
from _mod_ops import (
    download_entries as build_mod_download_entries,
)
from _security import Access_Control, Power_Level
from apps._app import App
from apps._config import (
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackModSnapshot,
)
from apps._mod import Mod
from apps.minecraft import Minecraft
from apps.minecraft.pack_export import (
    MinecraftPackExportError,
    MinecraftPackSpec,
    PackFormat,
    PackPurpose,
    client_pack_kubejs_entries,
    discover_client_pack_kubejs_scripts,
    export_minecraft_pack,
)
from node_api_app_state import ClientPackFilePreview
from node_api_mod import (
    NodeClientPackConfigUpdateRequest,
    NodeClientPackPublishRequest,
    NodeDownloadFile,
    NodeDownloadRequest,
)
from node_api_route_contracts import HttpExceptionFactory


class NodeClientPackService:
    """Owns client-pack configuration plus mod archive assembly and downloads."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        invalidate_app_state: Callable[[str], None],
        invalidate_mod_inventory: Callable[[str], None],
    ) -> None:
        self._node_name = node_name
        self._invalidate_app_state = invalidate_app_state
        self._invalidate_mod_inventory = invalidate_mod_inventory
        self._client_pack_locks: dict[str, asyncio.Lock] = {}
        self._log = logging.getLogger(__name__)

    def _client_pack_lock(self, app_name: str) -> asyncio.Lock:
        return self._client_pack_locks.setdefault(app_name, asyncio.Lock())

    async def content_hash(
        self,
        *,
        app: App,
        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...],
    ) -> str:
        version = app.cfg.version
        metadata = self.metadata(app)
        hash_context_payload: dict[str, object] = {
            "app_version": None if version is None else version.model_dump(mode="json", exclude_none=True),
            "name": metadata.name if metadata is not None else app.friendly,
            "summary": metadata.description if metadata is not None else app.cfg.notes,
        }
        if isinstance(app, Minecraft):
            hash_context_payload["author"] = getattr(app.cfg, "pack_author", "Yukibot")
        if metadata is not None and app.cfg.client_pack_metadata is not None:
            hash_context_payload["filename_template"] = metadata.filename_template
            hash_context_payload["include_servers_dat"] = metadata.include_servers_dat
            hash_context_payload["include_options_txt"] = metadata.include_options_txt
        return await run_blocking(
            client_pack_content_hash,
            entries,
            format_name=json.dumps(hash_context_payload, sort_keys=True),
        )

    @staticmethod
    def overrides_dir(app: App) -> Path | None:
        if not app.mod_capabilities.include_client_overrides:
            return None
        configured_dir = app.cfg.client_overrides_dir
        if configured_dir is not None:
            resolved_configured_dir = configured_dir.resolve()
            if resolved_configured_dir.is_dir():
                return resolved_configured_dir

        fallback_dir = (app.directory / ".yukibot" / "client-overrides").resolve()
        fallback_existed = fallback_dir.is_dir()
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
        except OSError as xcp:
            raise ClientPackValidationError(f"Client overrides directory could not be created: {fallback_dir}") from xcp
        if not fallback_existed:
            logging.getLogger(__name__).warning(
                "Created fallback client overrides directory: app=%s configured_path=%s path=%s",
                app.name,
                configured_dir,
                fallback_dir,
            )
        return fallback_dir

    @staticmethod
    def kubejs_scripts(app: App) -> tuple[ClientPackKubeJsScript, ...]:
        if not isinstance(app, Minecraft):
            return ()
        return discover_client_pack_kubejs_scripts(
            app.directory,
            excluded_paths=frozenset(app.cfg.client_pack_excluded_kubejs_scripts),
        )

    @staticmethod
    def metadata(app: App) -> ClientPackMetadataConfig | None:
        if not isinstance(app, Minecraft):
            return None
        if app.cfg.client_pack_metadata is not None:
            return app.cfg.client_pack_metadata
        return ClientPackMetadataConfig(name=app.friendly, description=app.cfg.notes or "")

    @staticmethod
    def _preview_overrides_dir(app: App) -> Path | None:
        if not app.mod_capabilities.include_client_overrides:
            return None
        configured_dir = app.cfg.client_overrides_dir
        if configured_dir is not None and configured_dir.is_dir():
            return configured_dir.resolve()
        fallback_dir = app.directory / ".yukibot" / "client-overrides"
        return fallback_dir.resolve() if fallback_dir.is_dir() else None

    @classmethod
    def _options_txt_preview(cls, app: App) -> str:
        overrides_dir = cls._preview_overrides_dir(app)
        if overrides_dir is None:
            return "No client overrides directory exists, so overrides/options.txt will not be added."
        options_path = overrides_dir / "options.txt"
        if not options_path.is_file():
            return f"No options.txt file exists at {options_path}."
        try:
            return options_path.read_text(encoding="utf-8", errors="replace")
        except OSError as xcp:
            return f"Could not read {options_path}: {xcp}"

    def file_previews(self, app: App) -> tuple[ClientPackFilePreview, ...]:
        if not isinstance(app, Minecraft):
            return ()
        server_address = app.cfg.join_direct_ip_address or app.cfg.join_address
        if server_address is None:
            servers_dat_preview = "No join address is configured, so overrides/servers.dat will not be generated."
        else:
            server_name = self._minecraft_servers_dat_server_name(self._node_label())
            servers_dat_preview = f"Minecraft servers.dat entry\nname={server_name}\nip={server_address}\n"
        return (
            ClientPackFilePreview(
                path="overrides/servers.dat",
                display_name="servers.dat",
                content_text=servers_dat_preview,
            ),
            ClientPackFilePreview(
                path="overrides/options.txt",
                display_name="options.txt",
                content_text=self._options_txt_preview(app),
            ),
        )

    def entries(
        self,
        selection: ClientPackSelection,
        *,
        app: App,
        include_kubejs_scripts: bool,
        include_servers_dat: bool | None = None,
        include_options_txt: bool | None = None,
    ) -> tuple[ArchiveEntry | ArchiveDataEntry, ...]:
        metadata = self.metadata(app)
        if metadata is not None and (include_servers_dat is not None or include_options_txt is not None):
            metadata = metadata.model_copy(
                update={
                    "include_servers_dat": metadata.include_servers_dat
                    if include_servers_dat is None
                    else include_servers_dat,
                    "include_options_txt": metadata.include_options_txt
                    if include_options_txt is None
                    else include_options_txt,
                }
            )
        entries = (
            *build_client_pack_entries(app.has_mod_manager, selection, client_overrides_dir=None),
            *self._override_entries(app=app, metadata=metadata),
        )
        if not isinstance(app, Minecraft):
            return entries
        if metadata is not None:
            entries = (
                *entries,
                *self._minecraft_extra_entries(app=app, metadata=metadata),
            )
        if not include_kubejs_scripts:
            return entries
        return (
            *entries,
            *client_pack_kubejs_entries(
                app.directory,
                excluded_paths=frozenset(app.cfg.client_pack_excluded_kubejs_scripts),
            ),
        )

    def default_mod_snapshots(self, app: App) -> tuple[ClientPackModSnapshot, ...]:
        entries = self.entries(
            ClientPackSelection(),
            app=app,
            include_kubejs_scripts=False,
            include_servers_dat=False,
            include_options_txt=False,
        )
        snapshots: list[ClientPackModSnapshot] = []
        for entry in entries:
            if isinstance(entry, ModArchiveEntry):
                mod = app.has_mod_manager.get(entry.mod_name)
                snapshots.append(ClientPackModSnapshot(name=mod.name, friendly=mod.friendly, version=mod.version))
        return tuple(sorted(snapshots, key=lambda mod: mod.friendly.casefold()))

    def automated_changelog(self, app: App) -> str:
        if not app.mod_capabilities.supports_client_pack:
            return ""
        try:
            current = self.default_mod_snapshots(app)
        except Exception as xcp:
            self._log.warning("Client-pack automated changelog failed: app=%s error=%s", app.name, xcp)
            return f"Automated client-pack changelog is unavailable: {xcp}"
        return self._automated_changelog_text(
            current=current,
            published=app.cfg.client_pack_published_mods,
            has_published_pack=app.cfg.client_pack_published_hash is not None,
        )

    async def update_config(
        self,
        *,
        app: App,
        update: NodeClientPackConfigUpdateRequest,
        actor_user_id: int,
        acl: Access_Control,
        http_exception: HttpExceptionFactory,
    ) -> dict[str, object]:
        if not app.mod_capabilities.supports_client_pack:
            raise http_exception(400, f"{app.friendly} does not support client pack generation.")
        manager = app.has_mod_manager
        await manager.reload_mods()
        await acl.perm_check(actor_user_id, Power_Level.admin)
        async with self._client_pack_lock(app.name):
            excluded_kubejs_scripts: tuple[str, ...] | None = None
            if update.metadata is not None and not isinstance(app, Minecraft):
                raise http_exception(409, "Client-pack metadata is only supported for Minecraft apps.")
            if update.kubejs_scripts is not None:
                if not isinstance(app, Minecraft):
                    raise http_exception(
                        409,
                        "KubeJS client-pack scripts are only supported for Minecraft apps.",
                    )
                discovered_paths = {script.relative_path for script in self.kubejs_scripts(app)}
                submitted_paths = {script.relative_path for script in update.kubejs_scripts}
                if submitted_paths != discovered_paths:
                    raise http_exception(
                        409,
                        "KubeJS scripts changed since the client-pack configuration was loaded; reload and try again.",
                    )
                excluded_kubejs_scripts = tuple(
                    sorted(
                        (script.relative_path for script in update.kubejs_scripts if not script.included),
                        key=str.casefold,
                    )
                )
            try:
                updated_mods = await manager.update_client_pack_configs(
                    {item.mod_name: item.client_pack for item in update.mods}
                )
            except (ModuleNotFoundError, ValueError) as xcp:
                raise http_exception(409, str(xcp)) from xcp
            if excluded_kubejs_scripts is not None:
                app.cfg.client_pack_excluded_kubejs_scripts = excluded_kubejs_scripts
            if update.metadata is not None:
                app.cfg.client_pack_metadata = update.metadata
            app.invalidate_client_pack_content()
            app.persist_instance_config_overrides()
        self._invalidate_app_state(app.name)
        self._invalidate_mod_inventory(app.name)
        self._log.info(
            "Node API client-pack configuration updated: node=%s app=%s mods=%s actor=%s",
            self._node_name(),
            app.name,
            len(updated_mods),
            actor_user_id,
        )
        return {
            "app_name": app.name,
            "updated_count": len(updated_mods),
            "message": f"Updated client-pack configuration for {len(updated_mods)} mods.",
        }

    async def publish_config(
        self,
        *,
        app: App,
        update: NodeClientPackPublishRequest,
        actor_user_id: int,
        acl: Access_Control,
        http_exception: HttpExceptionFactory,
    ) -> dict[str, object]:
        if not app.mod_capabilities.supports_client_pack:
            raise http_exception(400, f"{app.friendly} does not support client pack generation.")
        await acl.perm_check(actor_user_id, Power_Level.admin)
        async with self._client_pack_lock(app.name):
            await app.has_mod_manager.reload_mods()
            try:
                entries = self.entries(ClientPackSelection(), app=app, include_kubejs_scripts=True)
                content_hash = await self.content_hash(app=app, entries=entries)
                snapshots = self.default_mod_snapshots(app)
            except (
                ClientPackValidationError,
                ModuleNotFoundError,
                NonDownloadableModError,
            ) as xcp:
                self._log.warning(
                    "Client-pack publication rejected: app=%s actor=%s error=%s",
                    app.name,
                    actor_user_id,
                    xcp,
                )
                raise http_exception(409, str(xcp)) from xcp
            if not entries:
                raise http_exception(409, "The default client pack contains no mods.")
            try:
                await self._preflight_curseforge_client_pack(app=app, entries=entries)
            except (MinecraftPackExportError, ValueError) as xcp:
                self._log.warning(
                    "Client-pack CurseForge preflight rejected publication: app=%s actor=%s error=%s",
                    app.name,
                    actor_user_id,
                    xcp,
                )
                raise http_exception(409, f"CurseForge client-pack preflight failed: {xcp}") from xcp
            version = app.publish_client_pack(
                content_hash,
                changelog=update.changelog,
                mods=snapshots,
            )
        self._invalidate_app_state(app.name)
        return {
            "app_name": app.name,
            "published_version": version,
            "message": f"Published client pack version {version}.",
        }

    async def _preflight_curseforge_client_pack(
        self,
        *,
        app: App,
        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...],
    ) -> None:
        if not isinstance(app, Minecraft):
            return
        archive_path: Path | None = None
        try:
            archive_path = await export_minecraft_pack(
                entries,
                self._minecraft_pack_spec(
                    app=app,
                    purpose=PackPurpose.CLIENT,
                    pack_format=PackFormat.CURSEFORGE,
                    version_id=app.cfg.client_pack_published_version or app.next_client_pack_version,
                ),
                f"{app.name}-curseforge-preflight",
                unique_output=True,
            )
        finally:
            if archive_path is not None:
                archive_path.unlink(missing_ok=True)

    def _minecraft_pack_spec(
        self,
        *,
        app: App,
        purpose: PackPurpose,
        pack_format: PackFormat,
        version_id: str,
    ) -> MinecraftPackSpec:
        version = app.cfg.version
        if version is None:
            raise MinecraftPackExportError("Minecraft version metadata is required for launcher pack exports.")
        client_pack_metadata = self.metadata(app) if purpose is PackPurpose.CLIENT else None
        return MinecraftPackSpec(
            purpose=purpose,
            format=pack_format,
            name=(client_pack_metadata.name if client_pack_metadata is not None else app.friendly),
            version_id=version_id,
            minecraft_version=version.main,
            loader=version.loader,
            loader_version=version.framework,
            author=getattr(app.cfg, "pack_author", "Yukibot"),
            summary=(
                client_pack_metadata.description or None
                if client_pack_metadata is not None
                else app.cfg.notes
            ),
        )

    async def single_mod_download_file(
        self,
        *,
        app: App,
        mod: Mod,
        http_exception: HttpExceptionFactory,
    ) -> NodeDownloadFile:
        if not mod.path.exists():
            self._log.warning(
                "Node API single mod missing: app=%s mod=%s path=%s",
                app.name,
                mod.name,
                mod.path,
            )
            raise http_exception(404, f"Mod file is missing: {mod.name}")
        if mod.path.is_file():
            return NodeDownloadFile(path=mod.path, filename=mod.name, is_archive=False)
        if mod.path.is_dir():
            archive_path = await compress_mod_archive_entries(
                (ModArchiveEntry.from_mod(mod),),
                self.single_mod_archive_name(app=app, mod=mod),
                unique_output=True,
            )
            self._log.info(
                "Node API zipped directory mod: app=%s mod=%s source=%s archive=%s",
                app.name,
                mod.name,
                mod.path,
                archive_path,
            )
            return NodeDownloadFile(
                path=archive_path,
                filename=self.single_mod_archive_name(app=app, mod=mod),
                is_archive=True,
            )
        self._log.warning(
            "Node API single mod path is unsupported: app=%s mod=%s path=%s",
            app.name,
            mod.name,
            mod.path,
        )
        raise http_exception(404, f"Mod path is neither a file nor a directory: {mod.name}")

    async def build_mod_download_response(
        self,
        *,
        app: App,
        request: NodeDownloadRequest,
        http_exception: HttpExceptionFactory,
        _publish_lock_held: bool = False,
    ) -> FileResponse:
        if request.publish_client_pack and not _publish_lock_held:
            async with self._client_pack_lock(app.name):
                return await self.build_mod_download_response(
                    app=app,
                    request=request,
                    http_exception=http_exception,
                    _publish_lock_held=True,
                )
        await app.has_mod_manager.reload_mods()

        capabilities = app.mod_capabilities
        pack_purpose = request.resolved_pack_purpose
        if request.publish_client_pack and pack_purpose is not PackPurpose.CLIENT:
            raise http_exception(400, "Client-pack publication requires a client-pack request.")
        if pack_purpose is PackPurpose.CLIENT:
            if not capabilities.supports_client_pack:
                raise http_exception(400, f"{app.friendly} does not support client pack generation.")
            if app.cfg.client_pack_content_dirty and not request.publish_client_pack:
                raise http_exception(409, "Client pack configuration has unpublished changes.")
            if request.publish_client_pack and not (request.publish_changelog or "").strip():
                raise http_exception(400, "Client pack publication requires a changelog.")
            if request.pack_format is not PackFormat.GENERIC_ZIP and not capabilities.supports_launcher_formats:
                raise http_exception(400, f"{app.friendly} does not support launcher pack formats.")
        elif pack_purpose is None and not capabilities.supports_raw_download:
            raise http_exception(400, f"{app.friendly} does not support raw mod downloads.")

        if request.mod_name is not None:
            mod = app.has_mod_manager.get(request.mod_name)
            try:
                require_downloadable(mod)
            except NonDownloadableModError as xcp:
                self._log.warning(
                    "Node API blocked single mod download: app=%s mod=%s reason=%s",
                    app.name,
                    mod.name,
                    xcp,
                )
                raise http_exception(403, str(xcp)) from xcp
            download = await self.single_mod_download_file(
                app=app,
                mod=mod,
                http_exception=http_exception,
            )
            self._log.info(
                "Node API sending single mod: app=%s mod=%s path=%s archive=%s",
                app.name,
                mod.name,
                download.path,
                download.is_archive,
            )
            return FileResponse(path=download.path, filename=download.filename)

        selected_mod_names: tuple[str, ...] | None = (
            request.mod_names if request.selected_only or request.mod_names else None
        )
        if request.excluded_only:
            if not request.selected_only:
                raise http_exception(400, "Excluded mod selection requires selected-only mode.")
            if pack_purpose is not None:
                raise http_exception(
                    400,
                    "Excluded mod selection is only supported for raw mod downloads.",
                )
            excluded_names = frozenset(request.mod_names)
            for excluded_name in excluded_names:
                try:
                    require_downloadable(app.has_mod_manager.get(excluded_name))
                except (KeyError, ModuleNotFoundError, NonDownloadableModError) as xcp:
                    raise http_exception(400, f"Invalid excluded mod selection: {excluded_name}") from xcp
            selected_mod_names = tuple(
                mod.name
                for mod in app.has_mod_manager.list_mods()
                if mod.downloadable and mod.name not in excluded_names
            )

        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...]
        try:
            if pack_purpose is PackPurpose.CLIENT:
                entries = self.entries(
                    app=app,
                    selection=ClientPackSelection(
                        selected_mod_names=frozenset(selected_mod_names or ()),
                        supplied=request.selected_only or bool(request.mod_names),
                    ),
                    include_kubejs_scripts=request.include_kubejs_scripts,
                    include_servers_dat=request.include_servers_dat,
                    include_options_txt=request.include_options_txt,
                )
            elif pack_purpose is PackPurpose.SERVER:
                entries = build_server_pack_entries(app.has_mod_manager)
            elif pack_purpose is PackPurpose.ADMIN:
                entries = build_admin_pack_entries(app.has_mod_manager)
            else:
                entries = build_mod_download_entries(
                    app.has_mod_manager,
                    selected_mod_names,
                    default_enabled_only=request.enabled_only,
                )
        except NonDownloadableModError as xcp:
            raise http_exception(403, str(xcp)) from xcp
        except ClientPackValidationError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        except ModuleNotFoundError as xcp:
            raise http_exception(404, str(xcp)) from xcp
        if not entries:
            detail = self.empty_archive_detail(request)
            self._log.warning(
                "Node API archive request had no paths: app=%s enabled_only=%s selected=%s",
                app.name,
                request.enabled_only,
                len(selected_mod_names) if selected_mod_names is not None else 0,
            )
            raise http_exception(404, detail)

        current_hash: str | None = None
        generated_pack_version: str | None = None
        publication_mods: tuple[ClientPackModSnapshot, ...] | None = None
        publication_changelog: str | None = None
        if pack_purpose is PackPurpose.CLIENT:
            try:
                published_entries = self.entries(
                    app=app,
                    selection=ClientPackSelection(),
                    include_kubejs_scripts=True,
                )
                current_hash = await self.content_hash(app=app, entries=published_entries)
            except ClientPackValidationError as xcp:
                raise http_exception(400, str(xcp)) from xcp
            if app.cfg.client_pack_current_hash != current_hash:
                app.record_client_pack_content_hash(current_hash)
            if request.publish_client_pack:
                publication_mods = self.default_mod_snapshots(app)
                publication_changelog = request.publish_changelog or ""
                if (
                    app.cfg.client_pack_published_hash == current_hash
                    and app.cfg.client_pack_published_version is not None
                ):
                    generated_pack_version = app.cfg.client_pack_published_version
                else:
                    generated_pack_version = app.next_client_pack_version
            elif app.cfg.client_pack_published_hash != current_hash:
                raise http_exception(
                    409,
                    "Client pack content has changed; publish or regenerate it before download.",
                )
            elif app.cfg.client_pack_published_version is None:
                raise http_exception(
                    409,
                    "Client pack version metadata is missing; publish the client pack again.",
                )
            else:
                generated_pack_version = app.cfg.client_pack_published_version

        archive_name = self.archive_name(
            app=app,
            entries=entries,
            request=request,
            client_pack_version=generated_pack_version,
        )
        try:
            if pack_purpose is not None:
                version = app.cfg.version
                if version is None and request.pack_format is not PackFormat.GENERIC_ZIP:
                    raise http_exception(
                        400,
                        "Minecraft version metadata is required for launcher pack exports.",
                    )
                if version is None:
                    archive_path = await compress_mod_archive_entries(
                        entries,
                        archive_name,
                        unique_output=True,
                    )
                else:
                    archive_path = await export_minecraft_pack(
                        entries,
                        self._minecraft_pack_spec(
                            app=app,
                            purpose=pack_purpose,
                            pack_format=request.pack_format,
                            version_id=generated_pack_version or version.main,
                        ),
                        archive_name,
                        unique_output=True,
                    )
            else:
                archive_path = await compress_mod_archive_entries(
                    entries,
                    archive_name,
                    unique_output=True,
                )
        except (MinecraftPackExportError, ValueError) as xcp:
            raise http_exception(400, str(xcp)) from xcp
        if publication_changelog is not None:
            assert publication_mods is not None
            assert current_hash is not None
            try:
                published_version = app.publish_client_pack(
                    current_hash,
                    changelog=publication_changelog,
                    mods=publication_mods,
                )
            except ValueError as xcp:
                archive_path.unlink(missing_ok=True)
                raise http_exception(400, str(xcp)) from xcp
            if published_version != generated_pack_version:
                archive_path.unlink(missing_ok=True)
                raise RuntimeError("Client-pack publication version changed while its archive was being built.")
            self._invalidate_app_state(app.name)
        self._log.info(
            "Node API sending mod archive: app=%s enabled_only=%s selected=%s entries=%s archive=%s",
            app.name,
            request.enabled_only,
            len(selected_mod_names) if selected_mod_names is not None else 0,
            len(entries),
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_name)

    def archive_name(
        self,
        *,
        app: App,
        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...],
        request: NodeDownloadRequest,
        client_pack_version: str | None = None,
    ) -> str:
        if request.resolved_pack_purpose is PackPurpose.CLIENT:
            metadata = self.metadata(app)
            version = app.cfg.version
            if metadata is not None and version is not None:
                pack_version = client_pack_version or app.cfg.client_pack_published_version or version.main
                format_name = {
                    PackFormat.MODRINTH: "modrinth",
                    PackFormat.CURSEFORGE: "curseforge",
                    PackFormat.GENERIC_ZIP: "generic",
                }[request.pack_format]
                stem = metadata.filename_stem(
                    app_name=app.name,
                    version=pack_version,
                    minecraft_version=version.main,
                    format_name=format_name,
                )
                return f"{stem}{request.pack_format.suffix}"
        if request.resolved_pack_purpose is not None:
            suffix = f"{request.resolved_pack_purpose.value}_pack"
        elif request.selected_only or request.mod_names:
            suffix = "selected_mods" if len(entries) != 1 else "selected_mod"
        elif request.enabled_only:
            suffix = "enabled_mods" if len(entries) != 1 else "enabled_mod"
        else:
            suffix = "mods"
        app_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in app.friendly.strip())
        base_name = app_name.strip("_") or app.name
        if request.resolved_pack_purpose is not None and request.pack_format is not PackFormat.GENERIC_ZIP:
            suffix = f"{suffix}_{request.pack_format.value}"
        return f"{base_name}_{suffix}{request.pack_format.suffix}"

    @staticmethod
    def empty_archive_detail(request: NodeDownloadRequest) -> str:
        if request.resolved_pack_purpose is PackPurpose.CLIENT:
            return "No client-pack-eligible mods found."
        if request.resolved_pack_purpose is PackPurpose.SERVER:
            return "No enabled server-pack mods found."
        if request.resolved_pack_purpose is PackPurpose.ADMIN:
            return "No admin-pack mods found."
        if request.selected_only or request.mod_names:
            return "No selected downloadable mods found."
        if request.enabled_only:
            return "No enabled downloadable mods found."
        return "No downloadable mods found."

    @staticmethod
    def single_mod_archive_name(*, app: App, mod: Mod) -> str:
        app_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in app.friendly.strip())
        mod_name = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in mod.friendly.strip())
        base_app_name = app_name.strip("_") or app.name
        base_mod_name = mod_name.strip("_") or mod.name
        return f"{base_app_name}_{base_mod_name}.zip"

    def _override_entries(
        self,
        *,
        app: App,
        metadata: ClientPackMetadataConfig | None,
    ) -> tuple[ArchiveEntry, ...]:
        overrides_dir = self.overrides_dir(app)
        if overrides_dir is None:
            return ()
        if not overrides_dir.is_dir():
            raise ClientPackValidationError(f"Client overrides directory is missing: {overrides_dir}")
        excluded_paths = frozenset(
            {PurePosixPath("options.txt")} if metadata is not None and not metadata.include_options_txt else ()
        )
        return tuple(
            ArchiveEntry(
                source_path=file_path,
                archive_path=PurePosixPath("overrides")
                / PurePosixPath(file_path.relative_to(overrides_dir).as_posix()),
            )
            for file_path in sorted(
                (path for path in overrides_dir.rglob("*") if path.is_file()),
                key=lambda path: path.as_posix(),
            )
            if PurePosixPath(file_path.relative_to(overrides_dir).as_posix()) not in excluded_paths
        )

    def _minecraft_extra_entries(
        self,
        *,
        app: Minecraft,
        metadata: ClientPackMetadataConfig,
    ) -> tuple[ArchiveDataEntry, ...]:
        if not metadata.include_servers_dat:
            return ()
        server_address = app.cfg.join_direct_ip_address or app.cfg.join_address
        if server_address is None:
            self._log.warning(
                "Skipping generated servers.dat because %s has no join address.",
                app.name,
            )
            return ()
        return (
            ArchiveDataEntry(
                archive_path=PurePosixPath("overrides/servers.dat"),
                content=self._minecraft_servers_dat_content(
                    server_name=self._minecraft_servers_dat_server_name(self._node_label()),
                    server_address=server_address,
                ),
            ),
        )

    def _node_label(self) -> str:
        node_name = self._node_name()
        node_key = node_name.casefold()
        for snapshot in self._known_bot_snapshots():
            mod_web = snapshot.features.mod_web
            if mod_web is not None and mod_web.node_name.casefold() == node_key and snapshot.profile.label:
                return snapshot.profile.label
        if node_key == config.ACTIVE_BOT_PROFILE.name.value.casefold():
            return config.ACTIVE_BOT_PROFILE.name.value.title()
        return self._normalised_node_label(node_name)

    @staticmethod
    def _normalised_node_label(node_name: str) -> str:
        text = node_name.strip()
        return "Node" if not text else text.title() if text.casefold() == text else text

    def _known_bot_snapshots(self) -> tuple[config.BotMetadataSnapshot, ...]:
        snapshots: list[config.BotMetadataSnapshot] = []
        try:
            snapshots.extend(config.load_bot_configuration(Path("configuration.json")).known_bots.values())
        except Exception as xcp:
            self._log.warning("Node API failed to load local bot registry: %s", xcp)
        cache_path = config.authority_cache_path(AuthorityResource.BOTS)
        if cache_path.exists():
            try:
                raw_cache = read_json_object(cache_path)
                snapshots.extend(
                    config.BotMetadataSnapshot.model_validate(snapshot)
                    for snapshot in raw_cache.values()
                    if isinstance(snapshot, dict)
                )
            except Exception as xcp:
                self._log.warning("Node API failed to load cached bot registry: %s", xcp)
        return tuple({snapshot.profile.id: snapshot for snapshot in snapshots}.values())

    @staticmethod
    def _minecraft_servers_dat_server_name(node_label: str) -> str:
        base = "".join(character for character in node_label.strip() if character.isalnum()) or "Node"
        return f"{base}Server"

    @staticmethod
    def _minecraft_servers_dat_content(*, server_name: str, server_address: str) -> bytes:
        def tag_name(name: str) -> bytes:
            encoded = name.encode("utf-8")
            return struct.pack(">H", len(encoded)) + encoded

        def string_tag(name: str, value: str) -> bytes:
            encoded = value.encode("utf-8")
            return b"\x08" + tag_name(name) + struct.pack(">H", len(encoded)) + encoded

        server_compound = string_tag("name", server_name) + string_tag("ip", server_address) + b"\x00"
        servers_list = b"\x09" + tag_name("servers") + b"\x0a" + struct.pack(">i", 1) + server_compound
        return b"\x0a" + tag_name("") + servers_list + b"\x00"

    @classmethod
    def _automated_changelog_text(
        cls,
        *,
        current: tuple[ClientPackModSnapshot, ...],
        published: tuple[ClientPackModSnapshot, ...],
        has_published_pack: bool,
    ) -> str:
        if not current and not published:
            return "No automated client-pack changes detected."
        current_by_name = {snapshot.name.casefold(): snapshot for snapshot in current}
        published_by_name = {snapshot.name.casefold(): snapshot for snapshot in published}
        matched_pairs: list[tuple[ClientPackModSnapshot, ClientPackModSnapshot]] = []
        current_unmatched_keys = set(current_by_name)
        published_unmatched_keys = set(published_by_name)
        for key in sorted(current_unmatched_keys & published_unmatched_keys):
            matched_pairs.append((published_by_name[key], current_by_name[key]))
            current_unmatched_keys.remove(key)
            published_unmatched_keys.remove(key)

        current_unmatched = tuple(current_by_name[key] for key in current_unmatched_keys)
        published_unmatched = tuple(published_by_name[key] for key in published_unmatched_keys)
        current_by_friendly = cls._unique_by_friendly(current_unmatched)
        published_by_friendly = cls._unique_by_friendly(published_unmatched)
        for friendly_key in sorted(current_by_friendly.keys() & published_by_friendly.keys()):
            before = published_by_friendly[friendly_key]
            after = current_by_friendly[friendly_key]
            matched_pairs.append((before, after))
            current_unmatched_keys.remove(after.name.casefold())
            published_unmatched_keys.remove(before.name.casefold())

        added = tuple(
            current_by_name[key]
            for key in sorted(
                current_unmatched_keys,
                key=lambda item: current_by_name[item].friendly.casefold(),
            )
        )
        removed = tuple(
            published_by_name[key]
            for key in sorted(
                published_unmatched_keys,
                key=lambda item: published_by_name[item].friendly.casefold(),
            )
        )
        updated = tuple(
            (before, after)
            for before, after in sorted(matched_pairs, key=lambda item: item[1].friendly.casefold())
            if before.version != after.version or before.name != after.name or before.friendly != after.friendly
        )

        lines: list[str] = []
        if not published and not has_published_pack:
            lines.append("Initial client pack contents:")
            lines.extend(f"- {cls._snapshot_label(snapshot)}" for snapshot in current)
            return "\n".join(lines)
        if not published and has_published_pack:
            lines.append("Published mod snapshot will be tracked after the next publish.")
            lines.append("Current default client pack contents:")
            lines.extend(f"- {cls._snapshot_label(snapshot)}" for snapshot in current)
            return "\n".join(lines)
        if added:
            lines.append("Added mods:")
            lines.extend(f"- {cls._snapshot_label(snapshot)}" for snapshot in added)
        if removed:
            if lines:
                lines.append("")
            lines.append("Removed mods:")
            lines.extend(f"- {cls._snapshot_label(snapshot)}" for snapshot in removed)
        if updated:
            if lines:
                lines.append("")
            lines.append("Updated mods:")
            lines.extend(f"- {cls._update_label(before, after)}" for before, after in updated)
        return "\n".join(lines) if lines else "No automated client-pack changes detected."

    @staticmethod
    def _unique_by_friendly(
        snapshots: Sequence[ClientPackModSnapshot],
    ) -> dict[str, ClientPackModSnapshot]:
        friendly_counts: dict[str, int] = {}
        for snapshot in snapshots:
            key = snapshot.friendly.casefold()
            friendly_counts[key] = friendly_counts.get(key, 0) + 1
        return {
            snapshot.friendly.casefold(): snapshot
            for snapshot in snapshots
            if friendly_counts[snapshot.friendly.casefold()] == 1
        }

    @staticmethod
    def _snapshot_label(snapshot: ClientPackModSnapshot) -> str:
        return snapshot.friendly if snapshot.version is None else f"{snapshot.friendly} ({snapshot.version})"

    @classmethod
    def _update_label(cls, before: ClientPackModSnapshot, after: ClientPackModSnapshot) -> str:
        changes: list[str] = []
        if before.version != after.version:
            changes.append(f"{before.version or 'unknown'} -> {after.version or 'unknown'}")
        if before.name != after.name:
            changes.append(f"file {before.name} -> {after.name}")
        if before.friendly != after.friendly:
            changes.append(f"name {before.friendly} -> {after.friendly}")
        return cls._snapshot_label(after) if not changes else f"{after.friendly}: {'; '.join(changes)}"
