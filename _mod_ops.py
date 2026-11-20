from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import zipfile
from collections import Counter
from collections.abc import Collection, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Never, Protocol

import hikari

import config
from _async_utils import run_blocking_cancellable
from _file import File_Utils
from _security import Access_Control
from archive_safety import ArchiveMemberLayoutValidator, check_archive_cancelled, validated_archive_member_path
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
class ArchivePathRewrite:
    source_relative_path: PurePosixPath
    archive_relative_path: PurePosixPath

    def __post_init__(self) -> None:
        _validate_archive_relative_path(self.source_relative_path)
        _validate_archive_relative_path(self.archive_relative_path)


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    source_path: Path
    archive_path: PurePosixPath
    path_rewrites: tuple[ArchivePathRewrite, ...] = field(default=(), kw_only=True)

    def archive_path_for_relative(self, relative_path: PurePosixPath) -> PurePosixPath:
        _validate_archive_relative_path(relative_path)
        for rewrite in self.path_rewrites:
            if rewrite.source_relative_path == relative_path:
                return self.archive_path / rewrite.archive_relative_path
        return self.archive_path / relative_path


@dataclass(frozen=True, slots=True)
class ArchiveDataEntry:
    archive_path: PurePosixPath
    content: bytes


type WritableArchiveEntry = ArchiveEntry | ArchiveDataEntry

_ALREADY_COMPRESSED_ARCHIVE_SUFFIXES: frozenset[str] = frozenset({".jar", ".zip", ".mrpack"})
# Chunk framing is part of the persisted client-pack hash format.
_CLIENT_PACK_HASH_CHUNK_BYTES = 1024**2
_ARCHIVE_COPY_CHUNK_BYTES = _CLIENT_PACK_HASH_CHUNK_BYTES
_ZIP_MEMBER_NAME_MAX_BYTES = (1 << 16) - 1


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
            path_rewrites=tuple(
                ArchivePathRewrite(source_relative_path=source, archive_relative_path=destination)
                for source, destination in mod.download_archive_path_rewrites()
            ),
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


def _client_pack_manifest_metadata(entry: ModArchiveEntry) -> dict[str, object]:
    curseforge = entry.platforms.curseforge
    modrinth = entry.platforms.modrinth
    return {
        "bundle_eligible": entry.bundle_eligible,
        "client_pack_policy": entry.client_pack_policy.value,
        "mod_type": entry.mod_type.value,
        "curseforge": (
            None
            if curseforge is None
            else curseforge.model_dump(mode="json", include={"project_id", "file_id"})
        ),
        "modrinth": (
            None
            if modrinth is None
            else modrinth.model_dump(
                mode="json",
                include={"filename", "sha1", "sha512", "size", "download_url"},
            )
        ),
    }


def _client_pack_hash(
    entries: Collection[WritableArchiveEntry],
    *,
    format_name: str,
    hash_file_contents: bool,
    expected_file_hashes: Mapping[PurePosixPath, str] | None = None,
    cancelled: Event | None = None,
) -> str:
    digest = hashlib.sha256()
    if not hash_file_contents:
        _update_content_hash(digest, b"client-pack-source-fingerprint-v1")
    _update_content_hash(digest, format_name.encode("utf-8"))
    seen_archive_roots: set[str] = set()
    layout = ArchiveMemberLayoutValidator(archive_label="Client-pack archive")
    expected_hashes: Mapping[PurePosixPath, str] = expected_file_hashes if expected_file_hashes is not None else {}
    unverified_paths = set(expected_hashes)

    def reserve_archive_path(archive_path: PurePosixPath, *, is_directory: bool = False) -> None:
        try:
            layout.add(archive_path, is_directory=is_directory)
        except ValueError as xcp:
            raise ClientPackValidationError(str(xcp)) from xcp

    for entry in sorted(entries, key=lambda item: item.archive_path.as_posix().casefold()):
        check_archive_cancelled(cancelled)
        try:
            _validate_archive_path(entry.archive_path)
        except ValueError as xcp:
            raise ClientPackValidationError(str(xcp)) from xcp
        archive_root_key = entry.archive_path.as_posix().casefold()
        if archive_root_key in seen_archive_roots:
            raise ClientPackValidationError(f"Duplicate client-pack archive path: {entry.archive_path}")
        seen_archive_roots.add(archive_root_key)
        if isinstance(entry, ArchiveDataEntry):
            reserve_archive_path(entry.archive_path)
            _update_content_hash(digest, b"data")
            _update_content_hash(digest, entry.archive_path.as_posix().encode("utf-8"))
            _update_content_hash(digest, entry.content)
            continue
        if isinstance(entry, ModArchiveEntry):
            _update_content_hash(
                digest,
                json.dumps(
                    _client_pack_manifest_metadata(entry),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        source = entry.source_path
        if source.is_symlink():
            raise ClientPackValidationError(f"Client-pack entry cannot be a symbolic link: {source}")
        source_is_directory = source.is_dir()
        if source_is_directory:
            if not hash_file_contents:
                _update_source_fingerprint(digest, source)
            directories: list[Path] = []
            files: list[Path] = []
            for root, directory_names, file_names in source.walk(on_error=_raise_archive_scan_error):
                check_archive_cancelled(cancelled)
                for name in directory_names:
                    path = root / name
                    if path.is_symlink():
                        raise ClientPackValidationError(f"Client-pack entry cannot contain symbolic links: {path}")
                    directories.append(path)
                for name in file_names:
                    path = root / name
                    if path.is_symlink():
                        raise ClientPackValidationError(f"Client-pack entry cannot contain symbolic links: {path}")
                    if not path.is_file():
                        raise ClientPackValidationError(f"Unsupported client-pack source: {path}")
                    files.append(path)
            for path in sorted(directories, key=lambda item: item.as_posix()):
                relative = PurePosixPath(path.relative_to(source).as_posix())
                archive_path = entry.archive_path / relative
                reserve_archive_path(archive_path, is_directory=True)
                _update_content_hash(digest, b"directory")
                _update_content_hash(digest, archive_path.as_posix().encode("utf-8"))
                if not hash_file_contents:
                    _update_source_fingerprint(digest, path)
            files.sort(key=lambda path: path.as_posix())
        elif source.is_file():
            files = [source]
        else:
            raise ClientPackValidationError(f"Client-pack entry is missing: {source}")
        for path in files:
            check_archive_cancelled(cancelled)
            archive_path = (
                entry.archive_path_for_relative(PurePosixPath(path.relative_to(source).as_posix()))
                if source_is_directory
                else entry.archive_path
            )
            reserve_archive_path(archive_path)
            _update_content_hash(digest, b"file")
            _update_content_hash(digest, archive_path.as_posix().encode("utf-8"))
            expected_hash = expected_hashes.get(archive_path)
            if not hash_file_contents:
                _update_source_fingerprint(digest, path)
                _update_content_hash(digest, json.dumps(expected_hash).encode("utf-8"))
                unverified_paths.discard(archive_path)
                continue
            file_digest = hashlib.sha256() if expected_hash is not None else None
            with path.open("rb") as handle:
                while chunk := handle.read(_CLIENT_PACK_HASH_CHUNK_BYTES):
                    check_archive_cancelled(cancelled)
                    _update_content_hash(digest, chunk)
                    if file_digest is not None:
                        file_digest.update(chunk)
            if file_digest is not None:
                if file_digest.hexdigest() != expected_hash:
                    raise ClientPackValidationError(f"Client-pack file does not match the installed release: {archive_path}")
                unverified_paths.remove(archive_path)
    if unverified_paths:
        missing = ", ".join(str(path) for path in sorted(unverified_paths))
        raise ClientPackValidationError(f"Required client-pack files are missing: {missing}")
    return digest.hexdigest()


def client_pack_content_hash(
    entries: Collection[WritableArchiveEntry],
    *,
    format_name: str,
    expected_file_hashes: Mapping[PurePosixPath, str] | None = None,
    cancelled: Event | None = None,
) -> str:
    return _client_pack_hash(
        entries, format_name=format_name, hash_file_contents=True, expected_file_hashes=expected_file_hashes,
        cancelled=cancelled,
    )


def client_pack_source_fingerprint(
    entries: Collection[WritableArchiveEntry],
    *,
    format_name: str,
    expected_file_hashes: Mapping[PurePosixPath, str] | None = None,
    cancelled: Event | None = None,
) -> str:
    """Identify an unchanged pack using its metadata and filesystem signatures."""
    return _client_pack_hash(
        entries, format_name=format_name, hash_file_contents=False, expected_file_hashes=expected_file_hashes,
        cancelled=cancelled,
    )


def _update_source_fingerprint(digest: _HashDigest, path: Path) -> None:
    stat = path.stat()
    signature = (
        path.absolute().as_posix(), stat.st_dev, stat.st_ino, stat.st_mode,
        stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    )
    _update_content_hash(digest, json.dumps(signature, separators=(",", ":")).encode("utf-8"))


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

    selected_name_counts = Counter(mod.name.casefold() for mod in selected_mods)
    duplicate_names = sorted(
        {mod.name for mod in selected_mods if selected_name_counts[mod.name.casefold()] > 1},
        key=str.casefold,
    )
    if duplicate_names:
        names = ", ".join(duplicate_names)
        raise ClientPackValidationError(f"Client pack contains duplicate mods: {names}")

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
    validated_archive_member_path(archive_path.as_posix(), archive_label="Archive")


def _validate_archive_relative_path(relative_path: PurePosixPath) -> None:
    validated_archive_member_path(relative_path.as_posix(), archive_label="Archive relative")


def _raise_archive_scan_error(error: OSError) -> Never:
    """Prevent unreadable directories from producing incomplete packs."""
    raise error


def _process_archive_entries(
    entries: Collection[WritableArchiveEntry], *, archive_path: Path | None, cancelled: Event | None = None,
) -> None:
    """Validate members and optionally stream them into a ZIP using the same rules."""
    seen_archive_roots: set[str] = set()
    for entry in entries:
        check_archive_cancelled(cancelled)
        _validate_archive_path(entry.archive_path)
        archive_root_key = entry.archive_path.as_posix().casefold()
        if archive_root_key in seen_archive_roots:
            raise ValueError(f"Duplicate mod archive path: {entry.archive_path}")
        seen_archive_roots.add(archive_root_key)
        if isinstance(entry, ArchiveDataEntry):
            continue
        File_Utils.ensure_valid_path(entry.source_path)
        if entry.source_path.is_symlink():
            raise ValueError(f"Archive source cannot use symbolic links: {entry.source_path}")
        if not entry.source_path.is_file() and not entry.source_path.is_dir():
            raise ValueError(f"Unsupported mod archive source: {entry.source_path}")

    layout = ArchiveMemberLayoutValidator(archive_label="Archive")

    def reserve_archive_path(pointer: PurePosixPath, *, is_directory: bool = False) -> str:
        layout.add(pointer, is_directory=is_directory)
        rendered = pointer.as_posix() + ("/" if is_directory else "")
        if len(rendered.encode("utf-8")) > _ZIP_MEMBER_NAME_MAX_BYTES:
            raise ValueError("Archive member path exceeds the ZIP filename size limit.")
        return rendered

    def compression_for(archive_member_path: PurePosixPath) -> int:
        if archive_member_path.suffix.casefold() in _ALREADY_COMPRESSED_ARCHIVE_SUFFIXES:
            return zipfile.ZIP_STORED
        return zipfile.ZIP_DEFLATED

    with (
        zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED)
        if archive_path is not None else nullcontext()
    ) as archive:
        def process_file(source: Path, member_path: PurePosixPath) -> None:
            if source.is_symlink():
                raise ValueError(f"Archive source cannot use symbolic links: {source}")
            if not source.is_file():
                raise ValueError(f"Unsupported mod archive source: {source}")
            name = reserve_archive_path(member_path)
            info = zipfile.ZipInfo.from_file(source, name)
            if info.date_time[0] > 2107:
                raise ValueError("ZIP timestamps must be between 1980 and 2107.")
            if archive is not None:
                info.compress_type = compression_for(member_path)
                with source.open("rb") as source_file, archive.open(info, "w") as destination:
                    while chunk := source_file.read(_ARCHIVE_COPY_CHUNK_BYTES):
                        check_archive_cancelled(cancelled)
                        destination.write(chunk)

        for entry in entries:
            check_archive_cancelled(cancelled)
            if isinstance(entry, ArchiveDataEntry):
                member_path = reserve_archive_path(entry.archive_path)
                if archive is not None:
                    archive.writestr(member_path, entry.content)
                continue
            source_path = entry.source_path
            if source_path.is_file():
                process_file(source_path, entry.archive_path)
                continue

            seen_directories: set[tuple[int, int]] = set()
            for root, directories, files in source_path.walk(on_error=_raise_archive_scan_error):
                check_archive_cancelled(cancelled)
                root_path = root
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
                    directory_path = archive_root / directory
                    member_path = reserve_archive_path(directory_path, is_directory=True)
                    if archive is not None:
                        archive.writestr(member_path, "")
                for file_name in files:
                    source_file = root_path / file_name
                    relative_file = PurePosixPath(source_file.relative_to(source_path).as_posix())
                    archive_file = entry.archive_path_for_relative(relative_file)
                    process_file(source_file, archive_file)


def validate_archive_entries(
    entries: Collection[WritableArchiveEntry], *, cancelled: Event | None = None,
) -> None:
    """Apply archive layout and source validation without copying file contents."""
    _process_archive_entries(entries, archive_path=None, cancelled=cancelled)


def _write_mod_archive(
    entries: Collection[WritableArchiveEntry], archive_path: Path, *, cancelled: Event | None = None,
) -> None:
    _process_archive_entries(entries, archive_path=archive_path, cancelled=cancelled)


async def compress_archive_entries(
    entries: Collection[WritableArchiveEntry],
    archive_name: str,
    *,
    default_suffix: str = ".zip",
    overwrite: bool = True,
    unique_output: bool = False,
) -> Path:
    ordered_entries = tuple(entries)
    if not ordered_entries:
        raise ValueError("Mod archive requires at least one entry")
    resolved_name = archive_name if archive_name.endswith(default_suffix) else f"{archive_name}{default_suffix}"
    archive_path = config.DIR_ZIPS / resolved_name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if unique_output:
        archive_path = archive_path.with_name(
            f"{archive_path.stem}-{uuid.uuid4().hex}{archive_path.suffix}"
        )
        overwrite = False
    if archive_path.exists():
        if not overwrite:
            raise FileExistsError(f"Mod archive already exists: {archive_path}")
        archive_path.unlink()
    try:
        cancelled = Event()
        await run_blocking_cancellable(cancelled, _write_mod_archive, ordered_entries, archive_path, cancelled=cancelled)
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


async def compress_mod_archive_entries(
    entries: Collection[WritableArchiveEntry],
    archive_name: str,
    *,
    overwrite: bool = True,
    unique_output: bool = False,
) -> Path:
    return await compress_archive_entries(
        entries,
        archive_name,
        overwrite=overwrite,
        unique_output=unique_output,
    )


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
