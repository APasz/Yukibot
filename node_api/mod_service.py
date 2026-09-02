"""Mod inventory, mutation, upload, and metadata operations for the node API."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

from fastapi import HTTPException, UploadFile

import apps._node_api as app_node_api
import apps.factorio.node_api as factorio_node_api
import config
from . import mod as mod_contracts
from _async_utils import run_blocking
from _file import File_Utils
from _mod_ops import RunningAppModMutationError, require_app_stopped_for_mod_mutation
from _security import Access_Control, Power_Level
from _utils import Utilities
from apps._app import App
from apps._config import (
    BulkLauncherMetadataDiscovery,
    BulkLauncherMetadataEntry,
    BulkLauncherMetadataStatus,
    KnownModPageProvider,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    ModDownloadBlockReason,
    ModPageDiscovery,
    ModPlacement,
    ModType,
    known_mod_page_provider_for_url,
)
from apps._launcher_metadata import (
    BulkLauncherMetadataTarget,
    discover_bulk_launcher_metadata,
    discover_launcher_metadata,
    discover_mod_pages,
    resolve_launcher_metadata,
    resolve_launcher_metadata_resolution,
)
from apps._mod import Mod, Mod_Manager
from apps.factorio import FactorioModPortalCandidate, FactorioVanillaMod
from apps.factorio.node_api import (
    FactorioModUpdateApplyResult,
    NodeModDependencyResolutionResult,
    NodeModPortalVersionList,
    NodeModUpdateCheckResult,
    NodeModUpdateDependency,
)
from .app_state import NodeAppRuntimeSummary
from .upload import persist_upload_to_temp, validated_upload_filename

_MOD_INVENTORY_CACHE_TTL_SECONDS = 5.0
_BULK_METADATA_DISCOVERY_CACHE_TTL_SECONDS = 60.0 * 60.0
_BULK_METADATA_DISCOVERY_CACHE_MAX_ENTRIES = 64

log = logging.getLogger(__name__)
traffic_log = logging.getLogger(config.LOGGER_TRAFFIC)

_BulkMetadataOperationResult = TypeVar("_BulkMetadataOperationResult")


class ModUploadPaths(Protocol):
    async def __call__(
        self,
        *,
        app: App,
        upload_sources: Sequence[app_node_api.NodeModUploadSource],
        actor_user_id: int,
        placement: ModPlacement,
    ) -> mod_contracts.NodeModUploadBatchResult: ...


def _http_exception(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _get_mod_or_404(manager: Mod_Manager, mod_name: str) -> Mod:
    try:
        return manager.get(mod_name)
    except ModuleNotFoundError as xcp:
        raise _http_exception(404, str(xcp)) from xcp


class NodeModService:
    """Owns node-side mod inventory, mutations, uploads, and metadata workflows."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        require_acl: Callable[[], Access_Control],
        build_runtime_summary: Callable[[App], Awaitable[NodeAppRuntimeSummary]],
        invalidate_client_pack_content: Callable[[App], None],
        invalidate_mod_inventory: Callable[[str], None],
        upload_mod_paths: ModUploadPaths,
    ) -> None:
        self._node_name = node_name
        self._require_acl = require_acl
        self._build_runtime_summary = build_runtime_summary
        self._invalidate_client_pack_content = invalidate_client_pack_content
        self._invalidate_mod_inventory = invalidate_mod_inventory
        self._upload_mod_paths_callback = upload_mod_paths
        self._inventory_cache: dict[str, mod_contracts.TimedModInventory] = {}
        self._inventory_cache_locks: dict[str, asyncio.Lock] = {}
        self._bulk_metadata_tasks: dict[tuple[str, uuid.UUID], asyncio.Task[object]] = {}
        self._bulk_metadata_discoveries: dict[tuple[str, uuid.UUID], mod_contracts.CachedBulkMetadataDiscovery] = {}

    def invalidate_inventory(self, app_name: str) -> None:
        self._inventory_cache.pop(app_name.casefold(), None)

    async def run_bulk_metadata_operation(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
        action: Callable[[], Awaitable[_BulkMetadataOperationResult]],
    ) -> _BulkMetadataOperationResult:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Bulk metadata operation is not running in an asyncio task.")
        key = (app_name, operation_id)
        existing = self._bulk_metadata_tasks.get(key)
        if existing is not None and not existing.done():
            raise _http_exception(409, f"Bulk metadata operation {operation_id} is already running.")
        self._bulk_metadata_tasks[key] = cast(asyncio.Task[object], task)
        log.info(
            "Bulk mod metadata operation started: node=%s app=%s operation=%s",
            self._node_name(),
            app_name,
            operation_id,
        )
        try:
            return await action()
        except asyncio.CancelledError:
            log.info(
                "Bulk mod metadata operation cancelled: node=%s app=%s operation=%s",
                self._node_name(),
                app_name,
                operation_id,
            )
            raise
        finally:
            if self._bulk_metadata_tasks.get(key) is task:
                self._bulk_metadata_tasks.pop(key, None)

    def cancel_bulk_metadata_operation(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
    ) -> bool:
        task = self._bulk_metadata_tasks.get((app_name, operation_id))
        if task is None or task.done():
            return False
        task.cancel()
        log.info(
            "Bulk mod metadata cancellation requested: node=%s app=%s operation=%s",
            self._node_name(),
            app_name,
            operation_id,
        )
        return True

    def _cache_bulk_metadata_discovery(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
        discovery: BulkLauncherMetadataDiscovery,
    ) -> None:
        now = time.monotonic()
        expired_keys = tuple(
            key
            for key, cached in self._bulk_metadata_discoveries.items()
            if now - cached.captured_at_seconds >= _BULK_METADATA_DISCOVERY_CACHE_TTL_SECONDS
        )
        for key in expired_keys:
            self._bulk_metadata_discoveries.pop(key, None)
        cache_key = (app_name, operation_id)
        if (
            cache_key not in self._bulk_metadata_discoveries
            and len(self._bulk_metadata_discoveries) >= _BULK_METADATA_DISCOVERY_CACHE_MAX_ENTRIES
        ):
            oldest_key = min(
                self._bulk_metadata_discoveries,
                key=lambda key: self._bulk_metadata_discoveries[key].captured_at_seconds,
            )
            self._bulk_metadata_discoveries.pop(oldest_key, None)
        self._bulk_metadata_discoveries[cache_key] = mod_contracts.CachedBulkMetadataDiscovery(
            captured_at_seconds=now,
            discovery=discovery,
        )

    def _cached_bulk_metadata_discovery(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
    ) -> BulkLauncherMetadataDiscovery:
        key = (app_name, operation_id)
        cached = self._bulk_metadata_discoveries.get(key)
        if cached is None:
            raise _http_exception(409, "Bulk metadata discovery is unavailable; run discovery again.")
        if time.monotonic() - cached.captured_at_seconds >= _BULK_METADATA_DISCOVERY_CACHE_TTL_SECONDS:
            self._bulk_metadata_discoveries.pop(key, None)
            raise _http_exception(409, "Bulk metadata discovery expired; run discovery again.")
        return cached.discovery

    async def build_mod_list(self, app: App) -> mod_contracts.NodeModList:
        inventory, app_stats = await asyncio.gather(
            self._cached_mod_inventory(app),
            self._build_runtime_summary(app),
        )
        traffic_log.info(
            "Node API built mod list: node=%s app=%s mods=%s",
            self._node_name(),
            app.name,
            len(inventory.mods),
        )
        return mod_contracts.NodeModList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            summary=inventory.summary,
            mods=inventory.mods,
            app_stats=app_stats,
        )

    async def _cached_mod_inventory(self, app: App) -> mod_contracts.TimedModInventory:
        app_key = app.name.casefold()
        now = time.monotonic()
        cached = self._inventory_cache.get(app_key)
        if cached is not None and now - cached.captured_at_seconds < _MOD_INVENTORY_CACHE_TTL_SECONDS:
            return cached
        lock = self._inventory_cache_locks.setdefault(app_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._inventory_cache.get(app_key)
            if cached is not None and now - cached.captured_at_seconds < _MOD_INVENTORY_CACHE_TTL_SECONDS:
                return cached
            await app.has_mod_manager.reload_mods()
            mods = tuple(app.has_mod_manager.list_mods())
            inventory = mod_contracts.TimedModInventory(
                captured_at_seconds=time.monotonic(),
                summary=mod_contracts.NodeModSummary(
                    total_count=len(mods),
                    enabled_count=sum(1 for mod in mods if mod.cfg.placement is ModPlacement.SERVER_ENABLED),
                    disabled_count=sum(1 for mod in mods if mod.cfg.placement is ModPlacement.SERVER_DISABLED),
                    coremod_count=sum(1 for mod in mods if mod.counts_as_coremod),
                    downloadable_count=sum(1 for mod in mods if mod.downloadable),
                    non_downloadable_count=sum(1 for mod in mods if not mod.downloadable),
                    client_only_count=sum(1 for mod in mods if mod.cfg.placement is ModPlacement.CLIENT_ONLY),
                    client_pack_eligible_count=sum(1 for mod in mods if mod.client_pack_eligible),
                ),
                mods=tuple(self._mod_entry(mod) for mod in mods),
            )
            self._inventory_cache[app_key] = inventory
            return inventory

    async def upload_mod_file(
        self,
        *,
        app: App,
        upload: UploadFile,
        upload_name: str | None,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> mod_contracts.NodeModUploadResult:
        result = await self.upload_mod_files(
            app=app,
            uploads=[upload],
            upload_names=None if upload_name is None else [upload_name],
            actor_user_id=actor_user_id,
            placement=placement,
        )
        return self._single_mod_upload_result(result)

    async def upload_mod_files(
        self,
        *,
        app: App,
        uploads: Sequence[UploadFile],
        upload_names: Sequence[str] | None,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> mod_contracts.NodeModUploadBatchResult:
        upload_sources = self._resolve_mod_upload_requests(uploads=uploads, upload_names=upload_names)
        temp_paths: list[Path] = []
        try:
            resolved_sources: list[app_node_api.NodeModUploadSource] = []
            for upload_request in upload_sources:
                temp_path = await persist_upload_to_temp(upload_request.upload)
                temp_paths.append(temp_path)
                resolved_sources.append(
                    app_node_api.NodeModUploadSource(
                        source_path=temp_path,
                        upload_name=upload_request.upload_name,
                    )
                )
            return await self.upload_mod_paths(
                app=app,
                upload_sources=resolved_sources,
                actor_user_id=actor_user_id,
                placement=placement,
            )
        finally:
            for temp_path in temp_paths:
                temp_path.unlink(missing_ok=True)

    async def upload_mod_path(
        self,
        *,
        app: App,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> mod_contracts.NodeModUploadResult:
        result = await self.upload_mod_paths(
            app=app,
            upload_sources=[app_node_api.NodeModUploadSource(source_path=source_path, upload_name=upload_name)],
            actor_user_id=actor_user_id,
            placement=placement,
        )
        return self._single_mod_upload_result(result)

    async def install_mod_from_link(
        self,
        *,
        app: App,
        url: str,
        actor_user_id: int,
        selected_mod_ids: Sequence[str] | None = None,
        version: str | None = None,
    ) -> mod_contracts.NodeModUploadBatchResult:
        return await factorio_node_api.install_mod_from_link(
            app=app,
            url=url,
            actor_user_id=actor_user_id,
            upload_mod_paths=self._upload_mod_paths_callback,
            selected_mod_ids=selected_mod_ids,
            version=version,
        )

    async def resolve_mod_link_dependencies(
        self,
        *,
        app: App,
        url: str,
        version: str | None = None,
    ) -> NodeModDependencyResolutionResult:
        return await factorio_node_api.resolve_mod_link_dependencies(
            app=app,
            node_name=self._node_name(),
            url=url,
            version=version,
        )

    async def list_mod_link_versions(
        self,
        *,
        app: App,
        url: str,
    ) -> NodeModPortalVersionList:
        return await factorio_node_api.list_mod_link_versions(app=app, node_name=self._node_name(), url=url)

    async def list_installed_mod_versions(
        self,
        *,
        app: App,
        mod_name: str,
    ) -> NodeModPortalVersionList:
        return await factorio_node_api.list_installed_mod_versions(
            app=app,
            node_name=self._node_name(),
            mod_name=mod_name,
        )

    async def _factorio_mod_versions(self, *, app: App, url: str) -> NodeModPortalVersionList:
        return await factorio_node_api.factorio_mod_versions(app=app, node_name=self._node_name(), url=url)

    @staticmethod
    def _factorio_installed_mod_ids(app: App) -> frozenset[str]:
        return frozenset(factorio_node_api.factorio_installed_mods_by_id(app))

    @staticmethod
    def _factorio_vanilla_mods(app: App) -> Mapping[str, FactorioVanillaMod]:
        return factorio_node_api.factorio_vanilla_mods_by_id(app)

    @staticmethod
    def _factorio_installed_mods_by_id(app: App) -> Mapping[str, Mod]:
        return factorio_node_api.factorio_installed_mods_by_id(app)

    async def check_mod_update(
        self,
        *,
        app: App,
        mod_name: str,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        return await factorio_node_api.check_mod_update(
            app=app,
            node_name=self._node_name(),
            mod_name=mod_name,
            version=version,
        )

    async def update_mod(
        self,
        *,
        app: App,
        mod_name: str,
        actor_user_id: int,
        version: str | None = None,
    ) -> mod_contracts.NodeModUploadBatchResult:
        update_result: FactorioModUpdateApplyResult = await factorio_node_api.update_mod(
            app=app,
            node_name=self._node_name(),
            mod_name=mod_name,
            version=version,
        )
        old_mod = update_result.old_mod
        added_mods = update_result.added_mods
        update_check = update_result.update_check
        dependency_actions = update_result.dependency_actions

        traffic_log.info(
            "Node API mod updated: node=%s app=%s old_mod=%s new_mod=%s actor=%s",
            self._node_name(),
            app.name,
            old_mod.name,
            ",".join(updated_mod.name for updated_mod in added_mods),
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        updated_entries = tuple(self._mod_entry(updated_mod) for updated_mod in added_mods)
        dependency_change_count = len(dependency_actions)
        dependency_suffix = (
            ""
            if dependency_change_count == 0
            else f" Updated {dependency_change_count} required dependenc{'ies' if dependency_change_count != 1 else 'y'}."
        )
        return mod_contracts.NodeModUploadBatchResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=(
                f"Updated `{old_mod.friendly}` from {update_check.current_version} "
                f"to {update_check.latest_version}.{dependency_suffix}"
            ),
            mods=updated_entries,
        )

    async def _check_factorio_mod_update(
        self,
        *,
        app: App,
        mod: Mod,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        return await factorio_node_api.check_factorio_mod_update(
            app=app,
            node_name=self._node_name(),
            mod=mod,
            version=version,
        )

    @staticmethod
    def _factorio_dependency_update_entry(
        *,
        candidate: FactorioModPortalCandidate,
        installed_mod: Mod | None,
        vanilla_mod: FactorioVanillaMod | None = None,
    ) -> NodeModUpdateDependency:
        return factorio_node_api.factorio_dependency_update_entry(
            candidate=candidate,
            installed_mod=installed_mod,
            vanilla_mod=vanilla_mod,
        )

    @staticmethod
    def _factorio_dependency_update_summary(
        dependencies: Iterable[NodeModUpdateDependency],
    ) -> str | None:
        return factorio_node_api.factorio_dependency_update_summary(dependencies)

    @staticmethod
    def _factorio_mod_update_page_url(mod: Mod) -> str:
        try:
            return factorio_node_api.factorio_mod_update_page_url(mod)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

    async def upload_mod_paths(
        self,
        *,
        app: App,
        upload_sources: Sequence[app_node_api.NodeModUploadSource],
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> mod_contracts.NodeModUploadBatchResult:
        if app.mods is None:
            raise _http_exception(409, f"{app.friendly} does not support mods.")
        resolved_upload_sources: tuple[app_node_api.NodeModUploadSource, ...] = self._validated_mod_upload_sources(
            upload_sources
        )
        try:
            require_app_stopped_for_mod_mutation(app)
            manager: Mod_Manager = app.has_mod_manager
            await manager.reload_mods()
            uploaded_mods: list[Mod] = []
            with tempfile.TemporaryDirectory(prefix="yukibot-mod-upload-") as temp_dir:
                for upload_source in resolved_upload_sources:
                    staged_path: Path = Path(temp_dir) / upload_source.upload_name
                    await run_blocking(File_Utils.copy, upload_source.source_path, staged_path, True)
                    uploaded_mods.append(await manager.add(staged_path, atomic=True, placement=placement))
        except RunningAppModMutationError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Mod upload failed: {xcp}") from xcp

        traffic_log.info(
            "Node API mods uploaded: node=%s app=%s mods=%s actor=%s",
            self._node_name(),
            app.name,
            ",".join(mod.name for mod in uploaded_mods),
            actor_user_id,
        )
        mod_entries: tuple[mod_contracts.NodeModEntry, ...] = tuple(
            self._mod_entry(uploaded_mod) for uploaded_mod in uploaded_mods
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        return mod_contracts.NodeModUploadBatchResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            message=self._mod_upload_message(app=app, mods=mod_entries),
            mods=mod_entries,
        )

    async def mutate_mod(
        self,
        *,
        app: App,
        mod_name: str,
        action: mod_contracts.NodeModMutationAction,
        actor_user_id: int,
    ) -> mod_contracts.NodeModMutationResult:
        try:
            manager: Mod_Manager = app.has_mod_manager
            await manager.reload_mods()
            mod: Mod = _get_mod_or_404(manager, mod_name)
            override_protected_mod: bool = mod.is_protected and action in {
                mod_contracts.NodeModMutationAction.ENABLE,
                mod_contracts.NodeModMutationAction.DISABLE,
                mod_contracts.NodeModMutationAction.DELETE,
            }
            await self._require_acl().perm_check(
                actor_user_id,
                mod_contracts.required_mod_mutation_level(action, is_protected=override_protected_mod),
            )
            result_message: str
            result_mod_entry: mod_contracts.NodeModEntry | None

            if action is mod_contracts.NodeModMutationAction.ENABLE:
                if not mod.server_loadable:
                    raise _http_exception(
                        409,
                        f"Client-only mod cannot be enabled on the server: {mod.name}",
                    )
                require_app_stopped_for_mod_mutation(app)
                updated_mod: Mod = await manager.set_enabled(
                    mod,
                    True,
                    override_coremod=override_protected_mod,
                )
                result_message = f"Enabled {updated_mod.friendly}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is mod_contracts.NodeModMutationAction.DISABLE:
                if not mod.server_loadable:
                    raise _http_exception(
                        409,
                        f"Client-only mod cannot be disabled on the server: {mod.name}",
                    )
                require_app_stopped_for_mod_mutation(app)
                updated_mod = await manager.set_enabled(
                    mod,
                    False,
                    override_coremod=override_protected_mod,
                )
                result_message = f"Disabled {updated_mod.friendly}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is mod_contracts.NodeModMutationAction.TOGGLE_COREMOD:
                if mod.is_builtin:
                    raise _http_exception(409, "Built-in mods cannot be converted to or from coremods.")
                updated_mod = await manager.set_coremod(mod, not mod.is_coremod_type)
                coremod_text: Literal["enabled", "disabled"] = "enabled" if updated_mod.is_coremod_type else "disabled"
                result_message = f"Coremod {coremod_text} for {updated_mod.friendly}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is mod_contracts.NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK:
                reason: ModDownloadBlockReason | None = (
                    ModDownloadBlockReason.OTHER if mod.downloadable else mod.default_download_block_reason()
                )
                updated_mod = await manager.set_download_block_reason(mod, reason)
                blocked_text: Literal["blocked from download", "download-enabled"] = (
                    "blocked from download" if updated_mod.download_block_label is not None else "download-enabled"
                )
                result_message = f"{updated_mod.friendly} is now {blocked_text}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is mod_contracts.NodeModMutationAction.DELETE:
                require_app_stopped_for_mod_mutation(app)
                await manager.remove(
                    mod,
                    override_coremod=override_protected_mod,
                )
                result_message = f"Deleted {mod.friendly}."
                result_mod_entry = None
            else:
                raise ValueError(f"Unsupported mod mutation action: {action}")
        except RunningAppModMutationError as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        traffic_log.info(
            "Node API mod mutated: node=%s app=%s mod=%s action=%s actor=%s",
            self._node_name(),
            app.name,
            mod.name,
            action.value,
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        return mod_contracts.NodeModMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            mod_name=mod.name,
            action=action,
            message=result_message,
            mod=result_mod_entry,
        )

    async def find_mod_pages(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: mod_contracts.NodeModPageResolveRequest,
        actor_user_id: int,
    ) -> ModPageDiscovery:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            mod_contracts.required_mod_mutation_level(mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES),
        )
        version = app.cfg.version
        try:
            return await discover_mod_pages(
                scope=app.scope,
                existing_mod_pages=resolve_request.mod_pages,
                local_path=mod.storage_path,
                local_filename=mod.name,
                friendly_name=mod.friendly,
                detected_version=mod.version,
                game_version=None if version is None else version.main,
                loader=None if version is None else version.loader,
                providers=resolve_request.providers,
            )
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

    @staticmethod
    def _bulk_launcher_metadata_targets(
        *,
        manager: Mod_Manager,
        discovery_request: mod_contracts.NodeBulkLauncherMetadataRequest,
    ) -> tuple[BulkLauncherMetadataTarget, ...]:
        mods = (
            tuple(_get_mod_or_404(manager, mod_name) for mod_name in discovery_request.mod_names)
            if discovery_request.mod_names
            else tuple(manager.list_mods())
        )
        return tuple(
            BulkLauncherMetadataTarget(
                mod_name=mod.name,
                friendly_name=mod.friendly,
                local_path=mod.storage_path,
                existing_mod_pages=mod.cfg.mod_pages,
                existing_platforms=mod.cfg.platforms,
            )
            for mod in mods
            if not mod.is_builtin
            and (
                mod.cfg.platforms.modrinth is None
                or mod.cfg.platforms.curseforge is None
                or (
                    mod.cfg.platforms.modrinth is not None
                    and not any(
                        known_mod_page_provider_for_url(page.url) is KnownModPageProvider.MODRINTH
                        for page in mod.cfg.mod_pages
                    )
                )
                or (
                    mod.cfg.platforms.curseforge is not None
                    and mod.cfg.platforms.curseforge.page_url is not None
                    and not any(
                        known_mod_page_provider_for_url(page.url) is KnownModPageProvider.CURSEFORGE
                        for page in mod.cfg.mod_pages
                    )
                )
            )
        )

    async def discover_bulk_mod_metadata(
        self,
        *,
        app: App,
        discovery_request: mod_contracts.NodeBulkLauncherMetadataRequest,
        actor_user_id: int,
    ) -> BulkLauncherMetadataDiscovery:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        await self._require_acl().perm_check(
            actor_user_id,
            mod_contracts.required_mod_mutation_level(mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES),
        )
        targets = self._bulk_launcher_metadata_targets(
            manager=manager,
            discovery_request=discovery_request,
        )
        started_at = time.monotonic()
        log.info(
            "Bulk mod metadata discovery scanning: node=%s app=%s operation=%s targets=%s",
            self._node_name(),
            app.name,
            discovery_request.operation_id,
            len(targets),
        )
        try:
            discovery = await discover_bulk_launcher_metadata(scope=app.scope, targets=targets)
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        exact_count = len(discovery.exact_entries)
        log.info(
            "Bulk mod metadata discovery completed: node=%s app=%s operation=%s exact=%s "
            "unmatched=%s provider_errors=%s elapsed=%.2fs",
            self._node_name(),
            app.name,
            discovery_request.operation_id,
            exact_count,
            len(discovery.entries) - exact_count,
            len(discovery.provider_errors),
            time.monotonic() - started_at,
        )
        self._cache_bulk_metadata_discovery(
            app_name=app.name,
            operation_id=discovery_request.operation_id,
            discovery=discovery,
        )
        return discovery

    async def apply_bulk_mod_metadata(
        self,
        *,
        app: App,
        apply_request: mod_contracts.NodeBulkLauncherMetadataApplyRequest,
        actor_user_id: int,
    ) -> mod_contracts.NodeBulkLauncherMetadataApplyResult:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        await self._require_acl().perm_check(
            actor_user_id,
            mod_contracts.required_mod_mutation_level(mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES),
        )
        started_at = time.monotonic()
        type_selection_names = frozenset(apply_request.apply_suggested_type_mod_names)
        discovery = self._cached_bulk_metadata_discovery(
            app_name=app.name,
            operation_id=apply_request.discovery_operation_id,
        )
        entries_by_name = {entry.mod_name: entry for entry in discovery.entries}
        selected_entries: list[BulkLauncherMetadataEntry] = []
        for mod_name in apply_request.mod_names:
            entry = entries_by_name.get(mod_name)
            if entry is None or entry.status is not BulkLauncherMetadataStatus.EXACT:
                raise _http_exception(
                    409,
                    "Bulk metadata apply selections must be exact matches from the cached discovery.",
                )
            selected_entries.append(entry)
        invalid_type_selection_names = tuple(
            entry.mod_name
            for entry in selected_entries
            if entry.mod_name in type_selection_names
            and (entry.suggested_mod_type is None or entry.suggested_mod_type is ModType.REGULAR)
        )
        if invalid_type_selection_names:
            raise _http_exception(
                409,
                "Bulk metadata type selections require cached non-Regular suggestions: "
                + ", ".join(invalid_type_selection_names),
            )
        log.info(
            "Bulk mod metadata apply using cached discovery: node=%s app=%s operation=%s "
            "discovery_operation=%s targets=%s type_selections=%s",
            self._node_name(),
            app.name,
            apply_request.operation_id,
            apply_request.discovery_operation_id,
            len(selected_entries),
            len(type_selection_names),
        )
        applied_mod_names: list[str] = []
        applied_type_mod_names: list[str] = []
        try:
            for entry in selected_entries:
                apply_suggested_mod_type = entry.mod_name in type_selection_names
                await manager.apply_discovered_launcher_metadata(
                    entry.mod_name,
                    entry,
                    apply_suggested_mod_type=apply_suggested_mod_type,
                )
                applied_mod_names.append(entry.mod_name)
                if apply_suggested_mod_type:
                    applied_type_mod_names.append(entry.mod_name)
        except asyncio.CancelledError:
            log.warning(
                "Bulk mod metadata apply cancelled: node=%s app=%s operation=%s applied_before_cancel=%s elapsed=%.2fs",
                self._node_name(),
                app.name,
                apply_request.operation_id,
                len(applied_mod_names),
                time.monotonic() - started_at,
            )
            raise
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        if applied_mod_names:
            self._invalidate_client_pack_content(app)
            self._invalidate_mod_inventory(app.name)
        traffic_log.info(
            "Node API bulk mod metadata applied: node=%s app=%s count=%s actor=%s",
            self._node_name(),
            app.name,
            len(applied_mod_names),
            actor_user_id,
        )
        log.info(
            "Bulk mod metadata apply completed: node=%s app=%s operation=%s applied=%s types_updated=%s elapsed=%.2fs",
            self._node_name(),
            app.name,
            apply_request.operation_id,
            len(applied_mod_names),
            len(applied_type_mod_names),
            time.monotonic() - started_at,
        )
        self._bulk_metadata_discoveries.pop(
            (app.name, apply_request.discovery_operation_id),
            None,
        )
        return mod_contracts.NodeBulkLauncherMetadataApplyResult(
            discovery=discovery,
            applied_mod_names=tuple(applied_mod_names),
            applied_type_mod_names=tuple(applied_type_mod_names),
        )

    async def resolve_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: mod_contracts.NodeModMetadataResolveRequest,
        actor_user_id: int,
    ) -> LauncherMetadataDiscovery:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            mod_contracts.required_mod_mutation_level(mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES),
        )
        version = app.cfg.version
        try:
            return await discover_launcher_metadata(
                scope=app.scope,
                mod_pages=resolve_request.mod_pages,
                existing_urls=resolve_request.existing_launcher_urls,
                local_path=mod.storage_path,
                local_filename=mod.name,
                game_version=None if version is None else version.main,
                loader=None if version is None else version.loader,
                providers=resolve_request.providers,
            )
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

    async def fetch_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        fetch_request: mod_contracts.NodeModMetadataFetchRequest,
        actor_user_id: int,
    ) -> LauncherMetadataResolution:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            mod_contracts.required_mod_mutation_level(mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES),
        )
        try:
            return await resolve_launcher_metadata_resolution(
                scope=app.scope,
                urls=fetch_request.launcher_urls,
                local_filename=mod.name,
                local_path=mod.storage_path,
                providers=fetch_request.providers,
            )
        except ValueError as xcp:
            raise _http_exception(409, str(xcp)) from xcp

    async def update_mod_properties(
        self,
        *,
        app: App,
        mod_name: str,
        update: mod_contracts.NodeModPropertiesUpdateRequest,
        actor_user_id: int,
    ) -> mod_contracts.NodeModMutationResult:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            mod_contracts.required_mod_mutation_level(mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES),
        )
        if mod.is_builtin:
            raise _http_exception(409, "Built-in mod properties cannot be changed.")
        try:
            platforms = await resolve_launcher_metadata(
                scope=app.scope,
                urls=update.launcher_urls,
                local_filename=mod.name,
                local_path=mod.storage_path,
            )
            updated_mod = await manager.update_properties(
                mod,
                mod_type=update.mod_type,
                download_block_reason=update.download_block_reason,
                metadata_overrides=update.metadata_overrides,
                mod_pages=update.mod_pages,
                client_pack=update.client_pack or mod.cfg.client_pack,
                platforms=platforms,
            )
        except ValueError as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        traffic_log.info(
            "Node API mod properties updated: node=%s app=%s mod=%s actor=%s",
            self._node_name(),
            app.name,
            mod.name,
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        return mod_contracts.NodeModMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            mod_name=mod.name,
            action=mod_contracts.NodeModMutationAction.UPDATE_PROPERTIES,
            message=f"Updated properties for {updated_mod.friendly}.",
            mod=self._mod_entry(updated_mod),
        )

    async def update_mod_notes(
        self,
        *,
        app: App,
        mod_name: str,
        notes: str | None,
        actor_user_id: int,
    ) -> mod_contracts.NodeModMutationResult:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(actor_user_id, Power_Level.admin)
        try:
            updated_mod = await manager.update_notes(mod, notes=notes)
        except ValueError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        self._invalidate_mod_inventory(app.name)
        return mod_contracts.NodeModMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            mod_name=mod.name,
            action=mod_contracts.NodeModMutationAction.UPDATE_NOTES,
            message=f"Updated notes for {updated_mod.friendly}.",
            mod=self._mod_entry(updated_mod),
        )

    def _resolve_mod_upload_requests(
        self,
        *,
        uploads: Sequence[UploadFile],
        upload_names: Sequence[str] | None,
    ) -> tuple[mod_contracts.ResolvedModUploadFile, ...]:
        if not uploads:
            raise _http_exception(400, "At least one mod upload is required.")
        if upload_names is not None and len(upload_names) != len(uploads):
            raise _http_exception(400, "Mod upload filenames must match the number of uploads.")
        resolved_uploads: list[mod_contracts.ResolvedModUploadFile] = []
        for index, upload in enumerate(uploads):
            resolved_upload_name = self._validated_upload_filename(
                (upload.filename or "") if upload_names is None else upload_names[index],
                kind="Mod",
            )
            resolved_uploads.append(
                mod_contracts.ResolvedModUploadFile(
                    upload=upload,
                    upload_name=resolved_upload_name,
                )
            )
        return tuple(resolved_uploads)

    @staticmethod
    def _validated_upload_filename(filename: str, *, kind: str) -> str:
        try:
            return validated_upload_filename(filename, kind=kind)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

    def _validated_mod_upload_sources(
        self,
        upload_sources: Sequence[app_node_api.NodeModUploadSource],
    ) -> tuple[app_node_api.NodeModUploadSource, ...]:
        if not upload_sources:
            raise _http_exception(400, "At least one mod upload is required.")
        resolved_sources: list[app_node_api.NodeModUploadSource] = []
        for upload_source in upload_sources:
            resolved_sources.append(
                app_node_api.NodeModUploadSource(
                    source_path=upload_source.source_path,
                    upload_name=self._validated_upload_filename(upload_source.upload_name, kind="Mod"),
                )
            )
        return tuple(resolved_sources)

    @staticmethod
    def _mod_upload_message(*, app: App, mods: Sequence[mod_contracts.NodeModEntry]) -> str:
        if len(mods) == 1:
            return f"Uploaded mod `{mods[0].friendly}` for {app.friendly}."
        return f"Uploaded {len(mods)} mods for {app.friendly}."

    @staticmethod
    def _single_mod_upload_result(
        result: mod_contracts.NodeModUploadBatchResult,
    ) -> mod_contracts.NodeModUploadResult:
        if len(result.mods) != 1:
            raise ValueError("Exactly one uploaded mod is required.")
        return mod_contracts.NodeModUploadResult(
            app_name=result.app_name,
            app_friendly=result.app_friendly,
            node=result.node,
            message=result.message,
            mod=result.mods[0],
        )

    @staticmethod
    def _mod_entry(mod: Mod) -> mod_contracts.NodeModEntry:
        size_bytes = File_Utils.pointer_size(mod.path)
        return mod_contracts.NodeModEntry(
            name=mod.name,
            friendly=mod.friendly,
            client_path=str(mod.client_path),
            enabled=mod.cfg.enabled,
            placement=mod.cfg.placement,
            server_loadable=mod.server_loadable,
            client_pack_eligible=mod.client_pack_eligible,
            archive_name=mod.logical_archive_name,
            source_path=str(mod.storage_path),
            description=mod.description,
            notes=mod.cfg.notes,
            mod_type=mod.mod_type,
            coremod=mod.is_coremod_type,
            downloadable=mod.downloadable,
            download_block_reason=mod.download_block_reason.value if mod.download_block_reason is not None else None,
            download_block_label=mod.download_block_label,
            origin=mod.origin,
            version=mod.version,
            added=mod.added.isoformat(sep=" ", timespec="seconds"),
            size_bytes=size_bytes,
            size_text=Utilities.humanise_bytes(size_bytes),
            mod_pages=mod.cfg.mod_pages,
            metadata_overrides=mod.cfg.metadata_overrides,
            client_pack=mod.cfg.client_pack,
            platforms=mod.cfg.platforms,
        )
