from __future__ import annotations

import asyncio
import hashlib
import zipfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import hikari

import config
from _file import File_Utils
from _security import Access_Control
from apps._app import App
from apps._config import (
    ClientPackPolicy,
    ModDownloadBlockReason,
    ModPlacement,
    ModPlatformMetadata,
    ModSide,
    ModType,
)
from apps._mod import Mod, Mod_Manager

@dataclass(frozen=True, slots=True)
class ModMutationResult:
    successful: tuple[Mod, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    source_path: Path
    archive_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class ArchiveDataEntry:
    archive_path: PurePosixPath
    content: bytes


type WritableArchiveEntry = ArchiveEntry | ArchiveDataEntry

_ALREADY_COMPRESSED_ARCHIVE_SUFFIXES: frozenset[str] = frozenset({".jar", ".zip", ".mrpack"})


class _HashDigest(Protocol):
    def update(self, content: bytes, /) -> object: ...


@dataclass(frozen=True, slots=True)
class ModArchiveEntry(ArchiveEntry):
    mod_name: str
    placement: ModPlacement
    mod_type: ModType
    client_pack_policy: ClientPackPolicy
    platforms: ModPlatformMetadata
    bundle_eligible: bool

    @classmethod
    def from_mod(cls, mod: Mod) -> ModArchiveEntry:
        archive_path = PurePosixPath(mod.logical_archive_name)
        if archive_path.is_absolute() or len(archive_path.parts) != 1 or archive_path.name in {"", ".", ".."}:
            raise ValueError(f"Invalid logical mod archive name: {mod.logical_archive_name!r}")
        return cls(
            source_path=mod.storage_path,
            archive_path=archive_path,
            mod_name=mod.name,
            placement=mod.cfg.placement,
            mod_type=mod.mod_type,
            client_pack_policy=mod.cfg.client_pack.policy,
            platforms=mod.cfg.platforms,
            bundle_eligible=mod.downloadable,
        )


@dataclass(frozen=True, slots=True)
class ClientPackSelection:
    selected_mod_names: frozenset[str] = frozenset()
    supplied: bool = False


class NonDownloadableModError(RuntimeError):
    def __init__(self, mod: Mod) -> None:
        reason = mod.download_block_label or "not downloadable"
        super().__init__(f"{mod.friendly} is not downloadable ({reason}).")


class ClientPackValidationError(ValueError):
    """The persisted client-pack policy or submitted selection is invalid."""


def client_pack_content_hash(entries: Collection[WritableArchiveEntry], *, format_name: str) -> str:
    digest = hashlib.sha256()
    _update_content_hash(digest, format_name.encode("utf-8"))
    for entry in sorted(entries, key=lambda item: item.archive_path.as_posix().casefold()):
        if isinstance(entry, ArchiveDataEntry):
            _update_content_hash(digest, entry.archive_path.as_posix().encode("utf-8"))
            _update_content_hash(digest, entry.content)
            continue
        source = entry.source_path
        if source.is_dir():
            files = tuple(
                sorted(
                    (path for path in source.rglob("*") if path.is_file()),
                    key=lambda path: path.as_posix(),
                )
            )
        elif source.is_file():
            files = (source,)
        else:
            raise ClientPackValidationError(f"Client-pack entry is missing: {source}")
        for path in files:
            relative = path.relative_to(source) if source.is_dir() else Path(path.name)
            archive_path = entry.archive_path.joinpath(*relative.parts).as_posix()
            _update_content_hash(digest, archive_path.encode("utf-8"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    _update_content_hash(digest, chunk)
    return digest.hexdigest()


def _update_content_hash(digest: _HashDigest, content: bytes) -> None:
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


class RunningAppModMutationError(RuntimeError):
    def __init__(self, app: App) -> None:
        super().__init__(f"{app.friendly} is running; stop it before changing mods.")


def require_app_stopped_for_mod_mutation(app: App) -> None:
    if app.check_running():
        raise RunningAppModMutationError(app)


async def install_attachments(
    manager: Mod_Manager,
    attachments: Collection[hikari.Attachment],
    *,
    atomic: bool,
) -> tuple[Mod, ...]:
    ordered_attachments = tuple(sorted(attachments, key=lambda attachment: attachment.filename.casefold()))
    download_paths = await asyncio.gather(*(File_Utils.download_temp(attachment) for attachment in ordered_attachments))
    installed: list[Mod] = []
    for path in download_paths:
        installed.append(await manager.add(path, atomic=atomic))
    return tuple(installed)


async def refresh_mod_index(manager: Mod_Manager) -> tuple[Mod, ...]:
    await manager.reload_mods()
    return tuple(manager.list_mods())


def download_entries(
    manager: Mod_Manager,
    mod_names: Collection[str] | None = None,
    *,
    default_enabled_only: bool,
    client_pack_only: bool = False,
) -> tuple[ModArchiveEntry, ...]:
    if mod_names is not None:
        resolved_mods = [manager.get(mod_name) for mod_name in mod_names]
        for mod in resolved_mods:
            require_downloadable(mod)
    else:
        resolved_mods = manager.list_mods(True if default_enabled_only else None)
        resolved_mods = [mod for mod in resolved_mods if mod.downloadable]
    if client_pack_only:
        resolved_mods = [mod for mod in resolved_mods if mod.client_pack_eligible]
    return tuple(
        ModArchiveEntry.from_mod(mod)
        for mod in resolved_mods
        if mod.storage_path.exists()
    )


def download_paths(
    manager: Mod_Manager,
    mod_names: Collection[str] | None = None,
    *,
    default_enabled_only: bool,
) -> tuple[Path, ...]:
    """Compatibility view for callers that have not migrated to typed archive entries."""
    return tuple(
        entry.source_path
        for entry in download_entries(
            manager,
            mod_names,
            default_enabled_only=default_enabled_only,
        )
    )


def build_client_pack_entries(
    manager: Mod_Manager,
    selection: ClientPackSelection,
    *,
    client_overrides_dir: Path | None,
) -> tuple[ArchiveEntry, ...]:
    mods = tuple(manager.list_mods())
    selected_names: set[str] = set()
    for selected_name in selection.selected_mod_names:
        selected_mod = manager.get(selected_name)
        selected_names.add(selected_mod.name)

    try:
        client_mods = tuple(mod for mod in mods if mod.client_pack_candidate)
    except ValueError as xcp:
        raise ClientPackValidationError(str(xcp)) from xcp

    eligible_names = {mod.name for mod in client_mods}
    ineligible_selected_names = selected_names.difference(eligible_names)
    if ineligible_selected_names:
        names = ", ".join(sorted(ineligible_selected_names))
        raise ClientPackValidationError(f"Selected mods are not eligible for client packs: {names}")

    for selected_name in selected_names:
        selected_mod = manager.get(selected_name)
        if not selected_mod.downloadable:
            require_downloadable(selected_mod)

    choice_groups: dict[str, list[Mod]] = {}
    selected_mods: list[Mod] = []
    for mod in client_mods:
        client_pack = mod.cfg.client_pack
        if client_pack.policy is ClientPackPolicy.REQUIRED:
            if not mod.downloadable:
                raise ClientPackValidationError(f"Required client-pack mod {mod.name!r} must be downloadable")
            selected_mods.append(mod)
            continue
        if not mod.downloadable:
            raise ClientPackValidationError(f"Client-pack mod {mod.name!r} must be downloadable")
        if client_pack.policy is ClientPackPolicy.OPTIONAL:
            if mod.name in selected_names or (not selection.supplied and client_pack.default_selected):
                selected_mods.append(mod)
            continue
        assert client_pack.choice_group is not None
        choice_groups.setdefault(client_pack.choice_group, []).append(mod)

    for group_name, choices in choice_groups.items():
        if len(choices) < 2:
            raise ClientPackValidationError(f"Client-pack choice group {group_name!r} requires at least two mods")
        defaults = tuple(mod for mod in choices if mod.cfg.client_pack.default_choice)
        if len(defaults) != 1:
            raise ClientPackValidationError(
                f"Client-pack choice group {group_name!r} requires exactly one default; found {len(defaults)}"
            )
        explicitly_selected = tuple(mod for mod in choices if mod.name in selected_names)
        if len(explicitly_selected) > 1:
            selected = ", ".join(mod.name for mod in explicitly_selected)
            raise ClientPackValidationError(
                f"Client-pack choice group {group_name!r} has multiple selections: {selected}"
            )
        selected_mods.append(explicitly_selected[0] if explicitly_selected else defaults[0])

    entries: list[ArchiveEntry] = [ModArchiveEntry.from_mod(mod) for mod in selected_mods]
    if client_overrides_dir is not None:
        overrides_path = client_overrides_dir.resolve()
        if not overrides_path.exists() or not overrides_path.is_dir():
            raise ClientPackValidationError(f"Client overrides directory is missing: {overrides_path}")
        entries.append(ArchiveEntry(source_path=overrides_path, archive_path=PurePosixPath("overrides")))
    return tuple(entries)


def build_server_pack_entries(manager: Mod_Manager) -> tuple[ModArchiveEntry, ...]:
    return tuple(
        ModArchiveEntry.from_mod(mod)
        for mod in manager.list_mods(True)
        if mod.mod_type.side is not ModSide.CLIENT and mod.storage_path.exists()
    )


def build_admin_pack_entries(manager: Mod_Manager) -> tuple[ModArchiveEntry, ...]:
    return tuple(
        ModArchiveEntry.from_mod(mod)
        for mod in manager.list_mods()
        if mod.storage_path.exists()
    )


def _validate_archive_path(archive_path: PurePosixPath) -> None:
    if archive_path.is_absolute() or not archive_path.parts or ".." in archive_path.parts:
        raise ValueError(f"Invalid archive path: {archive_path}")


def _write_mod_archive(entries: Collection[WritableArchiveEntry], archive_path: Path) -> None:
    seen_archive_roots: set[str] = set()
    for entry in entries:
        _validate_archive_path(entry.archive_path)
        archive_root_key = entry.archive_path.as_posix().casefold()
        if archive_root_key in seen_archive_roots:
            raise ValueError(f"Duplicate mod archive path: {entry.archive_path}")
        seen_archive_roots.add(archive_root_key)
        if isinstance(entry, ArchiveDataEntry):
            continue
        File_Utils.ensure_valid_path(entry.source_path)
        if entry.source_path.is_symlink():
            raise ValueError(f"Archive source cannot be a symbolic link: {entry.source_path}")
        if not entry.source_path.is_file() and not entry.source_path.is_dir():
            raise ValueError(f"Unsupported mod archive source: {entry.source_path}")

    written_paths: set[str] = set()

    def reserve_archive_path(pointer: PurePosixPath) -> str:
        rendered = pointer.as_posix()
        key = rendered.casefold().rstrip("/")
        if key in written_paths:
            raise ValueError(f"Duplicate archive member path: {rendered}")
        written_paths.add(key)
        return rendered

    def compression_for(archive_member_path: PurePosixPath) -> int:
        if archive_member_path.suffix.casefold() in _ALREADY_COMPRESSED_ARCHIVE_SUFFIXES:
            return zipfile.ZIP_STORED
        return zipfile.ZIP_DEFLATED

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            if isinstance(entry, ArchiveDataEntry):
                archive.writestr(reserve_archive_path(entry.archive_path), entry.content)
                continue
            source_path = entry.source_path
            if source_path.is_file():
                archive.write(
                    source_path,
                    reserve_archive_path(entry.archive_path),
                    compress_type=compression_for(entry.archive_path),
                )
                continue

            seen_directories: set[tuple[int, int]] = set()
            for root, directories, files in source_path.walk(follow_symlinks=False):
                root_path = Path(root)
                stat = root_path.stat()
                directory_key = (stat.st_dev, stat.st_ino)
                if directory_key in seen_directories:
                    continue
                seen_directories.add(directory_key)
                relative_root = root_path.relative_to(source_path)
                archive_root = entry.archive_path / PurePosixPath(relative_root.as_posix())
                for directory in directories:
                    source_directory = root_path / directory
                    if source_directory.is_symlink():
                        raise ValueError(f"Archive directory cannot contain symbolic links: {source_directory}")
                    directory_path = PurePosixPath((archive_root / directory).as_posix().rstrip("/"))
                    archive.writestr(reserve_archive_path(directory_path) + "/", "")
                for file_name in files:
                    source_file = root_path / file_name
                    if source_file.is_symlink():
                        raise ValueError(f"Archive directory cannot contain symbolic links: {source_file}")
                    archive_file = archive_root / file_name
                    archive.write(
                        source_file,
                        reserve_archive_path(archive_file),
                        compress_type=compression_for(archive_file),
                    )


async def compress_archive_entries(
    entries: Collection[WritableArchiveEntry],
    archive_name: str,
    *,
    default_suffix: str = ".zip",
    overwrite: bool = True,
) -> Path:
    ordered_entries = tuple(entries)
    if not ordered_entries:
        raise ValueError("Mod archive requires at least one entry")
    resolved_name = archive_name if archive_name.endswith(default_suffix) else f"{archive_name}{default_suffix}"
    archive_path = config.DIR_ZIPS / resolved_name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if not overwrite:
            raise FileExistsError(f"Mod archive already exists: {archive_path}")
        archive_path.unlink()
    try:
        await asyncio.to_thread(_write_mod_archive, ordered_entries, archive_path)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


async def compress_mod_archive_entries(
    entries: Collection[WritableArchiveEntry],
    archive_name: str,
    *,
    overwrite: bool = True,
) -> Path:
    return await compress_archive_entries(entries, archive_name, overwrite=overwrite)


def require_downloadable(mod: Mod) -> None:
    if not mod.downloadable:
        raise NonDownloadableModError(mod)


async def toggle_mod(
    manager: Mod_Manager,
    mod_name: str,
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> Mod:
    mod = manager.get(mod_name)
    override_coremod = await _require_coremod_override(acl=acl, actor_user_id=actor_user_id, mod=mod)
    return await manager.toggle(mod, override_coremod=override_coremod)


async def remove_mods(
    manager: Mod_Manager,
    mod_names: Collection[str],
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> ModMutationResult:
    removed: list[Mod] = []
    errors: list[str] = []
    for mod_name in mod_names:
        try:
            mod = manager.get(mod_name)
            override_coremod = await _require_coremod_override(acl=acl, actor_user_id=actor_user_id, mod=mod)
            removed.append(await manager.remove(mod, override_coremod=override_coremod))
        except Exception as xcp:
            errors.append(f"{xcp}: {mod_name}" if mod_name not in str(xcp) else str(xcp))
    return ModMutationResult(successful=tuple(removed), errors=tuple(errors))


async def toggle_coremod(
    manager: Mod_Manager,
    mod_name: str,
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> Mod:
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    mod = manager.get(mod_name)
    if mod.is_builtin:
        raise RuntimeError("Built-in mods cannot be converted to or from coremods.")
    return await manager.set_coremod(mod, not mod.is_coremod_type)


async def toggle_downloadable(
    manager: Mod_Manager,
    mod_name: str,
    *,
    acl: Access_Control,
    actor_user_id: int,
    blocked_reason: ModDownloadBlockReason = ModDownloadBlockReason.OTHER,
) -> Mod:
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    mod = manager.get(mod_name)
    reason = blocked_reason if mod.downloadable else mod.default_download_block_reason()
    return await manager.set_download_block_reason(mod, reason)


async def _require_coremod_override(
    *,
    acl: Access_Control,
    actor_user_id: int,
    mod: Mod,
) -> bool:
    if not mod.is_protected:
        return False
    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    return True
