from __future__ import annotations

import logging
import tempfile
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from logging import Logger
from os import stat_result
from pathlib import Path
from typing import Final, Protocol, TypeAlias, TypeVar
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from pydantic.config import ConfigDict

from _file import File_Utils
from _mod_ops import RunningAppModMutationError, require_app_stopped_for_mod_mutation
from _utils import Utilities
from apps._app import App
from apps._config import KnownModPageProvider, ModPlacement, known_mod_page_provider_for_url
from apps._mod import Mod, Mod_Manager
from apps._node_api import NodeModUploadSource, optional_int, optional_string, required_bool, required_string
from apps.factorio import (
    FactorioModPortalCandidate,
    FactorioModPortalCredentials,
    FactorioModPortalDownload,
    FactorioVanillaMod,
    download_factorio_mods_from_portal,
    factorio_mod_portal_credentials_from_server_settings,
    factorio_mod_settings_path,
    factorio_server_settings_path,
    factorio_vanilla_mods,
    list_factorio_mod_portal_release_options,
    parse_factorio_mod_portal_url,
    resolve_factorio_mod_portal_candidates,
)

log: Logger = logging.getLogger(__name__)
_FACTORIO_SCOPE: Final[str] = "factorio"
_UploadBatchResult = TypeVar("_UploadBatchResult", covariant=True)


class FactorioModUploader(Protocol[_UploadBatchResult]):
    def __call__(
        self,
        *,
        app: App,
        upload_sources: Sequence[NodeModUploadSource],
        actor_user_id: int,
        placement: ModPlacement,
    ) -> Awaitable[_UploadBatchResult]: ...


def normalise_factorio_mod_portal_version(raw: str | None) -> str | None:
    if raw is None:
        return None
    version: str = raw.strip()
    if not version:
        return None
    if any(character.isspace() for character in version):
        raise ValueError("Factorio mod portal version must not contain whitespace.")
    return version


class NodeModPortalInstallRequest(BaseModel):
    url: str
    selected_mod_ids: tuple[str, ...] | None = None
    version: str | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("url")
    @classmethod
    def validate_url(cls, raw: str) -> str:
        parse_factorio_mod_portal_url(raw)
        return raw

    @field_validator("selected_mod_ids")
    @classmethod
    def validate_selected_mod_ids(cls, raw: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if raw is None:
            return None
        selected: list[str] = []
        seen: set[str] = set[str]()
        for mod_id in raw:
            if not mod_id.strip():
                raise ValueError("Selected Factorio mod ID must not be empty.")
            parse_factorio_mod_portal_url(f"https://mods.factorio.com/mod/{mod_id}")
            if mod_id in seen:
                continue
            seen.add(mod_id)
            selected.append(mod_id)
        return tuple[str, ...](selected)

    @field_validator("version")
    @classmethod
    def validate_version(cls, raw: str | None) -> str | None:
        return normalise_factorio_mod_portal_version(raw)


class NodeModUpdateRequest(BaseModel):
    version: str | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("version")
    @classmethod
    def validate_version(cls, raw: str | None) -> str | None:
        return normalise_factorio_mod_portal_version(raw)


class NodeModUpdateStatus(StrEnum):
    UPDATE_AVAILABLE = "update_available"
    CURRENT = "current"
    UNKNOWN_CURRENT = "unknown_current"


class NodeModUpdateDependencyAction(StrEnum):
    INSTALL = "install"
    UPDATE = "update"
    CURRENT = "current"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class NodeModDependencyEntry:
    mod_id: str
    title: str
    page_url: str
    version: str
    file_name: str
    parent_mod_ids: tuple[str, ...]
    dependency_mod_ids: tuple[str, ...]
    selected_by_default: bool
    installed: bool
    is_root: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModDependencyEntry":
        raw_parent_mod_ids: object = payload.get("parent_mod_ids", payload.get("required_by", ()))
        if isinstance(raw_parent_mod_ids, str) or not isinstance(raw_parent_mod_ids, Sequence):
            raise ValueError("Node mod dependency parent_mod_ids are invalid.")
        parent_mod_ids: list[str] = []
        for raw_value in raw_parent_mod_ids:
            if not isinstance(raw_value, str):
                raise ValueError("Node mod dependency parent_mod_ids are invalid.")
            parent_mod_ids.append(raw_value)
        raw_dependency_mod_ids: object = payload.get("dependency_mod_ids", ())
        if isinstance(raw_dependency_mod_ids, str) or not isinstance(raw_dependency_mod_ids, Sequence):
            raise ValueError("Node mod dependency dependency_mod_ids are invalid.")
        dependency_mod_ids: list[str] = []
        for raw_value in raw_dependency_mod_ids:
            if not isinstance(raw_value, str):
                raise ValueError("Node mod dependency dependency_mod_ids are invalid.")
            dependency_mod_ids.append(raw_value)
        return cls(
            mod_id=required_string(payload, "mod_id"),
            title=required_string(payload, "title"),
            page_url=required_string(payload, "page_url"),
            version=required_string(payload, "version"),
            file_name=required_string(payload, "file_name"),
            parent_mod_ids=tuple[str, ...](parent_mod_ids),
            dependency_mod_ids=tuple[str, ...](dependency_mod_ids),
            selected_by_default=required_bool(payload, "selected_by_default"),
            installed=required_bool(payload, "installed"),
            is_root=required_bool(payload, "is_root") if "is_root" in payload else False,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mod_id": self.mod_id,
            "title": self.title,
            "page_url": self.page_url,
            "version": self.version,
            "file_name": self.file_name,
            "parent_mod_ids": list[str](self.parent_mod_ids),
            "dependency_mod_ids": list[str](self.dependency_mod_ids),
            "required_by": list[str](self.parent_mod_ids),
            "selected_by_default": self.selected_by_default,
            "installed": self.installed,
            "is_root": self.is_root,
        }

    @property
    def required_by(self) -> tuple[str, ...]:
        return self.parent_mod_ids


NodeModPortalDependencyEntry: TypeAlias = NodeModDependencyEntry


@dataclass(frozen=True, slots=True)
class NodeModDependencyResolutionResult:
    app_name: str
    app_friendly: str
    node: str
    url: str
    root_mod_id: str
    dependencies: tuple[NodeModDependencyEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModDependencyResolutionResult":
        raw_dependencies: object | None = payload.get("dependencies")
        if isinstance(raw_dependencies, str) or not isinstance(raw_dependencies, Sequence):
            raise ValueError("Node mod portal resolve dependencies are invalid.")
        dependencies: list[NodeModDependencyEntry] = []
        for raw_dependency in raw_dependencies:
            if not isinstance(raw_dependency, Mapping):
                raise ValueError("Node mod portal resolve dependencies are invalid.")
            dependencies.append(NodeModDependencyEntry.from_mapping(raw_dependency))
        root_mod_id: str = (
            required_string(payload, "root_mod_id")
            if "root_mod_id" in payload
            else required_string(payload, "requested_mod_id")
        )
        return cls(
            app_name=required_string(payload, "app_name"),
            app_friendly=required_string(payload, "app_friendly"),
            node=required_string(payload, "node"),
            url=required_string(payload, "url"),
            root_mod_id=root_mod_id,
            dependencies=tuple[NodeModDependencyEntry, ...](dependencies),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "url": self.url,
            "root_mod_id": self.root_mod_id,
            "requested_mod_id": self.root_mod_id,
            "dependencies": [dependency.to_mapping() for dependency in self.dependencies],
        }

    @property
    def requested_mod_id(self) -> str:
        return self.root_mod_id


NodeModPortalResolveResult: TypeAlias = NodeModDependencyResolutionResult


@dataclass(frozen=True, slots=True)
class NodeModPortalVersionEntry:
    version: str
    file_name: str
    released_at: str | None
    factorio_version: str | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModPortalVersionEntry":
        return cls(
            version=required_string(payload, "version"),
            file_name=required_string(payload, "file_name"),
            released_at=optional_string(payload, "released_at"),
            factorio_version=optional_string(payload, "factorio_version"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": self.version,
            "file_name": self.file_name,
            "released_at": self.released_at,
            "factorio_version": self.factorio_version,
        }


@dataclass(frozen=True, slots=True)
class NodeModPortalVersionList:
    app_name: str
    app_friendly: str
    node: str
    url: str
    game_version: str | None
    versions: tuple[NodeModPortalVersionEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModPortalVersionList":
        raw_versions: object | None = payload.get("versions")
        if isinstance(raw_versions, str) or not isinstance(raw_versions, Sequence):
            raise ValueError("Node mod portal versions are invalid.")
        versions: list[NodeModPortalVersionEntry] = []
        for raw_version in raw_versions:
            if not isinstance(raw_version, Mapping):
                raise ValueError("Node mod portal versions are invalid.")
            versions.append(NodeModPortalVersionEntry.from_mapping(raw_version))
        return cls(
            app_name=required_string(payload, "app_name"),
            app_friendly=required_string(payload, "app_friendly"),
            node=required_string(payload, "node"),
            url=required_string(payload, "url"),
            game_version=optional_string(payload, "game_version"),
            versions=tuple(versions),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "url": self.url,
            "game_version": self.game_version,
            "versions": [version.to_mapping() for version in self.versions],
        }


@dataclass(frozen=True, slots=True)
class NodeModUpdateDependency:
    mod_id: str
    title: str
    page_url: str
    action: NodeModUpdateDependencyAction
    current_version: str | None
    latest_version: str
    latest_file_name: str
    installed_mod_name: str | None = None
    block_reason: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModUpdateDependency":
        raw_action: str = required_string(payload, "action")
        try:
            action: NodeModUpdateDependencyAction = NodeModUpdateDependencyAction(raw_action)
        except ValueError as xcp:
            raise ValueError("Node mod update dependency action is invalid.") from xcp
        return cls(
            mod_id=required_string(payload, "mod_id"),
            title=required_string(payload, "title"),
            page_url=required_string(payload, "page_url"),
            action=action,
            current_version=optional_string(payload, "current_version"),
            latest_version=required_string(payload, "latest_version"),
            latest_file_name=required_string(payload, "latest_file_name"),
            installed_mod_name=optional_string(payload, "installed_mod_name"),
            block_reason=optional_string(payload, "block_reason"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mod_id": self.mod_id,
            "title": self.title,
            "page_url": self.page_url,
            "action": self.action.value,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "latest_file_name": self.latest_file_name,
            "installed_mod_name": self.installed_mod_name,
            "block_reason": self.block_reason,
        }


@dataclass(frozen=True, slots=True)
class NodeModUpdateCheckResult:
    app_name: str
    app_friendly: str
    node: str
    mod_name: str
    mod_friendly: str
    status: NodeModUpdateStatus
    current_version: str | None
    latest_version: str
    latest_file_name: str
    page_url: str
    message: str
    dependencies: tuple[NodeModUpdateDependency, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModUpdateCheckResult":
        raw_status: str = required_string(payload, "status")
        try:
            status_value: NodeModUpdateStatus = NodeModUpdateStatus(raw_status)
        except ValueError as xcp:
            raise ValueError("Node mod update status is invalid.") from xcp
        raw_dependencies: object = payload.get("dependencies", ())
        if isinstance(raw_dependencies, str) or not isinstance(raw_dependencies, Sequence):
            raise ValueError("Node mod update dependencies are invalid.")
        dependencies: list[NodeModUpdateDependency] = []
        for raw_dependency in raw_dependencies:
            if not isinstance(raw_dependency, Mapping):
                raise ValueError("Node mod update dependencies are invalid.")
            dependencies.append(NodeModUpdateDependency.from_mapping(raw_dependency))
        return cls(
            app_name=required_string(payload, "app_name"),
            app_friendly=required_string(payload, "app_friendly"),
            node=required_string(payload, "node"),
            mod_name=required_string(payload, "mod_name"),
            mod_friendly=required_string(payload, "mod_friendly"),
            status=status_value,
            current_version=optional_string(payload, "current_version"),
            latest_version=required_string(payload, "latest_version"),
            latest_file_name=required_string(payload, "latest_file_name"),
            page_url=required_string(payload, "page_url"),
            message=required_string(payload, "message"),
            dependencies=tuple(dependencies),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "mod_name": self.mod_name,
            "mod_friendly": self.mod_friendly,
            "status": self.status.value,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "latest_file_name": self.latest_file_name,
            "page_url": self.page_url,
            "message": self.message,
            "dependencies": [dependency.to_mapping() for dependency in self.dependencies],
        }


@dataclass(frozen=True, slots=True)
class NodeFactorioModSettings:
    app_name: str
    app_friendly: str
    node: str
    file_exists: bool
    size_bytes: int | None = None
    size_text: str | None = None
    modified_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeFactorioModSettings":
        raw_file_exists: object | None = payload.get("file_exists")
        if not isinstance(raw_file_exists, bool):
            raise ValueError("Factorio mod settings file_exists is invalid.")
        return cls(
            app_name=required_string(payload, "app_name"),
            app_friendly=required_string(payload, "app_friendly"),
            node=required_string(payload, "node"),
            file_exists=raw_file_exists,
            size_bytes=optional_int(payload, "size_bytes"),
            size_text=optional_string(payload, "size_text"),
            modified_at=optional_string(payload, "modified_at"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "file_exists": self.file_exists,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
        }


def build_factorio_mod_settings_state(*, app: App, node_name: str) -> NodeFactorioModSettings:
    pointer: Path = factorio_mod_settings_path(app.directory)
    if not pointer.exists():
        return NodeFactorioModSettings(
            app_name=app.name,
            app_friendly=app.friendly,
            node=node_name,
            file_exists=False,
        )
    if not pointer.is_file():
        raise ValueError(f"Factorio mod settings path is not a file: {pointer}")
    stat: stat_result = pointer.stat()
    return NodeFactorioModSettings(
        app_name=app.name,
        app_friendly=app.friendly,
        node=node_name,
        file_exists=True,
        size_bytes=stat.st_size,
        size_text=Utilities.humanise_bytes(stat.st_size),
        modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
    )


def build_factorio_mod_settings_download_response(*, app: App) -> FileResponse:
    pointer: Path = factorio_mod_settings_path(app.directory)
    if not pointer.exists():
        raise FileNotFoundError("Factorio mod settings file does not exist.")
    if not pointer.is_file():
        raise ValueError(f"Factorio mod settings path is not a file: {pointer}")
    return FileResponse(
        path=pointer,
        filename=pointer.name,
        media_type="application/octet-stream",
    )


def factorio_installed_mod_ids(app: App) -> frozenset[str]:
    return frozenset[str](factorio_installed_mods_by_id(app))


def factorio_vanilla_mods_by_id(app: App) -> Mapping[str, FactorioVanillaMod]:
    try:
        return factorio_vanilla_mods(app.directory / "data")
    except (OSError, ValueError) as xcp:
        log.warning("Failed to inspect Factorio vanilla mods: app=%s error=%s", app.name, xcp)
        return {}


def factorio_installed_mods_by_id(app: App) -> Mapping[str, Mod]:
    if app.mods is None:
        return {}
    installed_mods: dict[str, Mod] = {}
    try:
        mods: list[Mod] = app.has_mod_manager.list_mods()
    except Exception as xcp:
        log.warning("Failed to inspect installed Factorio mods for dependencies: app=%s error=%s", app.name, xcp)
        return {}
    for mod in mods:
        try:
            native_id: str | None = mod.native_metadata_id()
        except Exception:
            native_id = None
        if native_id is not None:
            installed_mods.setdefault(native_id, mod)
        try:
            fallback_id = mod.metadata_fallback_id()
        except Exception:
            fallback_id = None
        if fallback_id is not None:
            installed_mods.setdefault(fallback_id, mod)
    return installed_mods


def factorio_dependency_update_entry(
    *,
    candidate: FactorioModPortalCandidate,
    installed_mod: Mod | None,
    vanilla_mod: FactorioVanillaMod | None = None,
) -> NodeModUpdateDependency:
    if vanilla_mod is not None:
        return NodeModUpdateDependency(
            mod_id=candidate.mod_id,
            title=vanilla_mod.title,
            page_url=candidate.page_url,
            action=NodeModUpdateDependencyAction.CURRENT,
            current_version=vanilla_mod.version,
            latest_version=candidate.version,
            latest_file_name=candidate.file_name,
        )
    if installed_mod is None:
        return NodeModUpdateDependency(
            mod_id=candidate.mod_id,
            title=candidate.title,
            page_url=candidate.page_url,
            action=NodeModUpdateDependencyAction.INSTALL,
            current_version=None,
            latest_version=candidate.version,
            latest_file_name=candidate.file_name,
        )
    if installed_mod.cfg.placement is not ModPlacement.SERVER_ENABLED:
        return NodeModUpdateDependency(
            mod_id=candidate.mod_id,
            title=candidate.title,
            page_url=candidate.page_url,
            action=NodeModUpdateDependencyAction.BLOCKED,
            current_version=installed_mod.version,
            latest_version=candidate.version,
            latest_file_name=candidate.file_name,
            installed_mod_name=installed_mod.name,
            block_reason=f"installed dependency is {installed_mod.cfg.placement.label.lower()}",
        )
    if installed_mod.version == candidate.version:
        action = NodeModUpdateDependencyAction.CURRENT
    else:
        action = NodeModUpdateDependencyAction.UPDATE
    return NodeModUpdateDependency(
        mod_id=candidate.mod_id,
        title=candidate.title,
        page_url=candidate.page_url,
        action=action,
        current_version=installed_mod.version,
        latest_version=candidate.version,
        latest_file_name=candidate.file_name,
        installed_mod_name=installed_mod.name,
    )


def factorio_dependency_update_summary(dependencies: Iterable[NodeModUpdateDependency]) -> str | None:
    install_count = 0
    update_count = 0
    blocked_count = 0
    for dependency in dependencies:
        if dependency.action is NodeModUpdateDependencyAction.INSTALL:
            install_count += 1
        elif dependency.action is NodeModUpdateDependencyAction.UPDATE:
            update_count += 1
        elif dependency.action is NodeModUpdateDependencyAction.BLOCKED:
            blocked_count += 1
    parts: list[str] = []
    if install_count:
        parts.append(f"install {install_count} required dependency{'ies' if install_count != 1 else ''}")
    if update_count:
        parts.append(f"update {update_count} required dependency{'ies' if update_count != 1 else ''}")
    if blocked_count:
        parts.append(f"{blocked_count} required dependency{'ies are' if blocked_count != 1 else ' is'} blocked")
    if not parts:
        return None
    return f"Dependencies: {', '.join(parts)}."


def factorio_mod_update_page_url(mod: Mod) -> str:
    for page in mod.cfg.mod_pages:
        if known_mod_page_provider_for_url(page.url) is KnownModPageProvider.FACTORIO_MODS:
            return page.url
    mod_id: str | None = mod.native_metadata_id()
    if mod_id is None:
        raise ValueError(f"{mod.friendly} does not have a Factorio mod portal identity.")
    return f"https://mods.factorio.com/mod/{quote(mod_id, safe='')}"


@dataclass(frozen=True, slots=True)
class FactorioModUpdateApplyResult:
    old_mod: Mod
    added_mods: tuple[Mod, ...]
    update_check: NodeModUpdateCheckResult
    dependency_actions: tuple[NodeModUpdateDependency, ...]


def _http_exception(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _require_factorio_portal_app(app: App) -> None:
    if app.scope != _FACTORIO_SCOPE:
        raise _http_exception(400, f"{app.friendly} does not support Factorio mod portal links.")


def _require_factorio_update_app(app: App) -> None:
    if app.scope != _FACTORIO_SCOPE:
        raise _http_exception(400, f"{app.friendly} does not support mod updates yet.")
    if app.mods is None:
        raise _http_exception(409, f"{app.friendly} does not support mods.")


def _get_mod_or_404(manager: Mod_Manager, mod_name: str) -> Mod:
    try:
        return manager.get(mod_name)
    except ModuleNotFoundError as xcp:
        raise _http_exception(404, str(xcp)) from xcp


def _factorio_mod_portal_credentials(app: App) -> FactorioModPortalCredentials:
    try:
        return factorio_mod_portal_credentials_from_server_settings(factorio_server_settings_path(app.directory))
    except ValueError as xcp:
        raise _http_exception(400, str(xcp)) from xcp
    except OSError as xcp:
        raise _http_exception(404, f"Factorio server settings could not be read: {xcp}") from xcp


async def install_mod_from_link(
    *,
    app: App,
    url: str,
    actor_user_id: int,
    upload_mod_paths: FactorioModUploader[_UploadBatchResult],
    selected_mod_ids: Sequence[str] | None = None,
    version: str | None = None,
) -> _UploadBatchResult:
    _require_factorio_portal_app(app)
    credentials = _factorio_mod_portal_credentials(app)
    with tempfile.TemporaryDirectory(prefix="yukibot-factorio-mod-link-") as temp_dir:
        try:
            downloads: tuple[FactorioModPortalDownload, ...] = await download_factorio_mods_from_portal(
                page_url=url,
                destination_dir=Path(temp_dir),
                factorio_version=app.detect_installed_version() or app.cfg.version,
                credentials=credentials,
                selected_mod_ids=selected_mod_ids,
                requested_mod_version=version,
            )
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise _http_exception(502, str(xcp)) from xcp
        return await upload_mod_paths(
            app=app,
            upload_sources=tuple(
                NodeModUploadSource(source_path=download.archive_path, upload_name=download.file_name)
                for download in downloads
            ),
            actor_user_id=actor_user_id,
            placement=ModPlacement.SERVER_ENABLED,
        )


async def resolve_mod_link_dependencies(
    *,
    app: App,
    node_name: str,
    url: str,
    version: str | None = None,
) -> NodeModDependencyResolutionResult:
    _require_factorio_portal_app(app)
    try:
        resolution = await resolve_factorio_mod_portal_candidates(
            page_url=url,
            factorio_version=app.detect_installed_version() or app.cfg.version,
            requested_mod_version=version,
        )
    except ValueError as xcp:
        raise _http_exception(400, str(xcp)) from xcp
    except RuntimeError as xcp:
        raise _http_exception(502, str(xcp)) from xcp

    installed_ids = factorio_installed_mod_ids(app)
    vanilla_mods = factorio_vanilla_mods_by_id(app)
    satisfied_ids = installed_ids | frozenset(vanilla_mods)

    def dependency_title(candidate: FactorioModPortalCandidate) -> str:
        vanilla_mod = vanilla_mods.get(candidate.mod_id)
        if vanilla_mod is not None:
            return vanilla_mod.title
        return candidate.title

    return NodeModDependencyResolutionResult(
        app_name=app.name,
        app_friendly=app.friendly,
        node=node_name,
        url=url,
        root_mod_id=resolution.requested_mod_id,
        dependencies=tuple(
            NodeModDependencyEntry(
                mod_id=candidate.mod_id,
                title=dependency_title(candidate),
                page_url=candidate.page_url,
                version=candidate.version,
                file_name=candidate.file_name,
                parent_mod_ids=candidate.required_by,
                dependency_mod_ids=candidate.dependency_ids,
                selected_by_default=(
                    candidate.mod_id == resolution.requested_mod_id or candidate.mod_id not in satisfied_ids
                ),
                installed=candidate.mod_id in satisfied_ids,
                is_root=candidate.mod_id == resolution.requested_mod_id,
            )
            for candidate in resolution.candidates
        ),
    )


async def list_mod_link_versions(*, app: App, node_name: str, url: str) -> NodeModPortalVersionList:
    _require_factorio_portal_app(app)
    return await factorio_mod_versions(app=app, node_name=node_name, url=url)


async def list_installed_mod_versions(
    *,
    app: App,
    node_name: str,
    mod_name: str,
) -> NodeModPortalVersionList:
    _require_factorio_portal_app(app)
    if app.mods is None:
        raise _http_exception(409, f"{app.friendly} does not support mods.")
    manager: Mod_Manager = app.has_mod_manager
    await manager.reload_mods()
    mod = _get_mod_or_404(manager, mod_name)
    return await factorio_mod_versions(app=app, node_name=node_name, url=_factorio_mod_update_page_url_or_400(mod))


async def factorio_mod_versions(*, app: App, node_name: str, url: str) -> NodeModPortalVersionList:
    game_version = app.detect_installed_version() or app.cfg.version
    try:
        versions = await list_factorio_mod_portal_release_options(
            page_url=url,
            factorio_version=game_version,
        )
    except ValueError as xcp:
        raise _http_exception(400, str(xcp)) from xcp
    except RuntimeError as xcp:
        raise _http_exception(502, str(xcp)) from xcp
    return NodeModPortalVersionList(
        app_name=app.name,
        app_friendly=app.friendly,
        node=node_name,
        url=url,
        game_version=None if game_version is None else game_version.main,
        versions=tuple(
            NodeModPortalVersionEntry(
                version=release.version,
                file_name=release.file_name,
                released_at=release.released_at,
                factorio_version=release.factorio_version,
            )
            for release in versions
        ),
    )


async def check_mod_update(
    *,
    app: App,
    node_name: str,
    mod_name: str,
    version: str | None = None,
) -> NodeModUpdateCheckResult:
    _require_factorio_update_app(app)
    manager: Mod_Manager = app.has_mod_manager
    await manager.reload_mods()
    mod = _get_mod_or_404(manager, mod_name)
    return await check_factorio_mod_update(app=app, node_name=node_name, mod=mod, version=version)


async def check_factorio_mod_update(
    *,
    app: App,
    node_name: str,
    mod: Mod,
    version: str | None = None,
) -> NodeModUpdateCheckResult:
    page_url = _factorio_mod_update_page_url_or_400(mod)
    try:
        resolution = await resolve_factorio_mod_portal_candidates(
            page_url=page_url,
            factorio_version=app.detect_installed_version() or app.cfg.version,
            requested_mod_version=version,
        )
    except ValueError as xcp:
        raise _http_exception(400, str(xcp)) from xcp
    except RuntimeError as xcp:
        raise _http_exception(502, str(xcp)) from xcp

    root_candidate = next(
        (candidate for candidate in resolution.candidates if candidate.mod_id == resolution.requested_mod_id),
        None,
    )
    if root_candidate is None:
        raise _http_exception(502, "Factorio mod portal response did not include the requested mod.")
    installed_mods_by_id = factorio_installed_mods_by_id(app)
    vanilla_mods = factorio_vanilla_mods_by_id(app)
    dependency_updates = tuple(
        factorio_dependency_update_entry(
            candidate=candidate,
            installed_mod=installed_mods_by_id.get(candidate.mod_id),
            vanilla_mod=vanilla_mods.get(candidate.mod_id),
        )
        for candidate in resolution.candidates
        if candidate.mod_id != resolution.requested_mod_id
    )

    current_version = mod.version
    if current_version is None:
        update_status = NodeModUpdateStatus.UNKNOWN_CURRENT
        message = f"{mod.friendly}: latest portal version is {root_candidate.version}; local version is unknown."
    elif current_version == root_candidate.version:
        update_status = NodeModUpdateStatus.CURRENT
        message = f"{mod.friendly} is current at {current_version}."
    else:
        update_status = NodeModUpdateStatus.UPDATE_AVAILABLE
        message = f"{mod.friendly}: update available {current_version} -> {root_candidate.version}."
    dependency_message = factorio_dependency_update_summary(dependency_updates)
    if dependency_message is not None:
        message = f"{message} {dependency_message}"

    return NodeModUpdateCheckResult(
        app_name=app.name,
        app_friendly=app.friendly,
        node=node_name,
        mod_name=mod.name,
        mod_friendly=mod.friendly,
        status=update_status,
        current_version=current_version,
        latest_version=root_candidate.version,
        latest_file_name=root_candidate.file_name,
        page_url=root_candidate.page_url,
        message=message,
        dependencies=dependency_updates,
    )


async def update_mod(
    *,
    app: App,
    node_name: str,
    mod_name: str,
    version: str | None = None,
) -> FactorioModUpdateApplyResult:
    _require_factorio_update_app(app)
    added_mods: list[Mod] = []
    dependency_actions: tuple[NodeModUpdateDependency, ...] = ()
    mod: Mod | None = None
    update_check: NodeModUpdateCheckResult | None = None
    try:
        require_app_stopped_for_mod_mutation(app)
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod = _get_mod_or_404(manager, mod_name)
        if mod.cfg.placement is not ModPlacement.SERVER_ENABLED:
            raise _http_exception(409, f"Only enabled mods can be updated: {mod.friendly}.")
        update_check = await check_factorio_mod_update(app=app, node_name=node_name, mod=mod, version=version)
        if update_check.status is not NodeModUpdateStatus.UPDATE_AVAILABLE:
            raise _http_exception(409, update_check.message)
        blocked_dependencies = tuple(
            dependency
            for dependency in update_check.dependencies
            if dependency.action is NodeModUpdateDependencyAction.BLOCKED
        )
        if blocked_dependencies:
            details = ", ".join(
                f"{dependency.title} ({dependency.block_reason or 'blocked'})" for dependency in blocked_dependencies
            )
            raise _http_exception(409, f"Cannot update while required dependencies are blocked: {details}.")
        dependency_actions = tuple(
            dependency
            for dependency in update_check.dependencies
            if dependency.action in {
                NodeModUpdateDependencyAction.INSTALL,
                NodeModUpdateDependencyAction.UPDATE,
            }
        )
        selected_mod_ids = (
            parse_factorio_mod_portal_url(update_check.page_url),
            *(dependency.mod_id for dependency in dependency_actions),
        )
        credentials = _factorio_mod_portal_credentials(app)

        with tempfile.TemporaryDirectory(prefix="yukibot-factorio-mod-update-") as temp_dir:
            temp_path = Path(temp_dir)
            backup_dir = temp_path / "previous"
            backup_dir.mkdir(parents=True, exist_ok=True)
            try:
                downloads: tuple[FactorioModPortalDownload, ...] = await download_factorio_mods_from_portal(
                    page_url=update_check.page_url,
                    destination_dir=temp_path,
                    factorio_version=app.detect_installed_version() or app.cfg.version,
                    credentials=credentials,
                    selected_mod_ids=selected_mod_ids,
                    requested_mod_version=version,
                )
            except ValueError as xcp:
                raise _http_exception(400, str(xcp)) from xcp
            except RuntimeError as xcp:
                raise _http_exception(502, str(xcp)) from xcp
            if len(downloads) != len(selected_mod_ids):
                raise _http_exception(502, "Factorio mod update did not download every selected archive.")
            remove_mods: list[Mod] = [mod]
            for dependency in dependency_actions:
                if dependency.action is not NodeModUpdateDependencyAction.UPDATE:
                    continue
                if dependency.installed_mod_name is None:
                    raise _http_exception(
                        502,
                        f"Factorio dependency update is missing installed mod name: {dependency.mod_id}.",
                    )
                remove_mods.append(_get_mod_or_404(manager, dependency.installed_mod_name))
            for old_mod in remove_mods:
                backup_path = backup_dir / old_mod.name
                File_Utils.copy(old_mod.storage_path, backup_path, True)
            try:
                for old_mod in remove_mods:
                    await manager.remove(old_mod, override_coremod=old_mod.is_protected)
                for download in downloads:
                    added_mods.append(
                        await manager.add(
                            download.archive_path,
                            atomic=True,
                            placement=ModPlacement.SERVER_ENABLED,
                        )
                    )
            except Exception:
                log.exception(
                    "Factorio mod update failed after removing old mods; attempting restore: app=%s mod=%s",
                    app.name,
                    mod.name,
                )
                for added_mod in tuple(added_mods):
                    try:
                        await manager.remove(added_mod, override_coremod=True)
                    except Exception:
                        log.exception(
                            "Factorio mod rollback failed to remove added mod: app=%s mod=%s",
                            app.name,
                            added_mod.name,
                        )
                try:
                    for old_mod in remove_mods:
                        backup_path = backup_dir / old_mod.name
                        if not backup_path.exists():
                            continue
                        await manager.add(
                            backup_path,
                            atomic=True,
                            placement=ModPlacement.SERVER_ENABLED,
                        )
                except Exception:
                    log.exception("Factorio mod restore failed: app=%s mod=%s", app.name, mod.name)
                raise
    except RunningAppModMutationError as xcp:
        raise _http_exception(409, str(xcp)) from xcp
    except HTTPException:
        raise
    except FileNotFoundError as xcp:
        raise _http_exception(404, str(xcp)) from xcp
    except FileExistsError as xcp:
        raise _http_exception(409, str(xcp)) from xcp
    except ValueError as xcp:
        raise _http_exception(400, str(xcp)) from xcp
    except Exception as xcp:
        raise _http_exception(500, f"Mod update failed: {xcp}") from xcp

    assert mod is not None
    assert update_check is not None
    return FactorioModUpdateApplyResult(
        old_mod=mod,
        added_mods=tuple(added_mods),
        update_check=update_check,
        dependency_actions=dependency_actions,
    )


def _factorio_mod_update_page_url_or_400(mod: Mod) -> str:
    try:
        return factorio_mod_update_page_url(mod)
    except ValueError as xcp:
        raise _http_exception(400, str(xcp)) from xcp
