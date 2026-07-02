from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import config
from _mod_ops import (
    ArchiveDataEntry,
    ArchiveEntry,
    ModArchiveEntry,
    WritableArchiveEntry,
    compress_archive_entries,
)
from apps._config import ClientPackPolicy, ModSide


class PackPurpose(enum.StrEnum):
    CLIENT = "client"
    SERVER = "server"
    ADMIN = "admin"


class PackFormat(enum.StrEnum):
    GENERIC_ZIP = "generic_zip"
    MODRINTH = "mrpack"
    CURSEFORGE = "curseforge"

    @property
    def suffix(self) -> str:
        return ".mrpack" if self is PackFormat.MODRINTH else ".zip"


class MinecraftPackExportError(ValueError):
    """The selected entries or Minecraft metadata cannot produce the requested format."""


@dataclass(frozen=True, slots=True)
class MinecraftPackSpec:
    purpose: PackPurpose
    format: PackFormat
    name: str
    version_id: str
    minecraft_version: str
    loader: str | None = None
    loader_version: str | None = None
    author: str = "Yukibot"
    summary: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("name", self.name),
            ("version_id", self.version_id),
            ("minecraft_version", self.minecraft_version),
            ("author", self.author),
        ):
            if not value.strip():
                raise MinecraftPackExportError(f"Minecraft pack {field_name} must not be empty")
        if (
            self.format is not PackFormat.GENERIC_ZIP
            and self.loader not in {None, "vanilla"}
            and not self.loader_version
        ):
            raise MinecraftPackExportError("Minecraft loader version is required for launcher pack exports")


def _json_entry(archive_path: str, payload: dict[str, object]) -> ArchiveDataEntry:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode(config.STR_ENCODE) + b"\n"
    return ArchiveDataEntry(archive_path=PurePosixPath(archive_path), content=content)


def _mod_destination(entry: ModArchiveEntry) -> PurePosixPath:
    return PurePosixPath("mods") / entry.archive_path


def _bundled_mod_entry(entry: ModArchiveEntry, *, override_root: str = "overrides") -> ArchiveEntry:
    return ArchiveEntry(
        source_path=entry.source_path,
        archive_path=PurePosixPath(override_root) / _mod_destination(entry),
    )


def _validate_purpose(entries: tuple[ArchiveEntry, ...], purpose: PackPurpose) -> None:
    if purpose is PackPurpose.ADMIN:
        return
    excluded_mods = tuple(
        entry.mod_name
        for entry in entries
        if isinstance(entry, ModArchiveEntry)
        and (
            purpose is PackPurpose.CLIENT and entry.mod_type.side is ModSide.SERVER
            or purpose is PackPurpose.SERVER and entry.mod_type.side is ModSide.CLIENT
        )
    )
    if excluded_mods:
        names = ", ".join(excluded_mods)
        excluded_side = "server-only" if purpose is PackPurpose.CLIENT else "client-only"
        raise MinecraftPackExportError(f"{purpose.value.title()} packs cannot contain {excluded_side} mods: {names}")


def _modrinth_dependencies(spec: MinecraftPackSpec) -> dict[str, str]:
    dependencies = {"minecraft": spec.minecraft_version}
    loader = spec.loader
    if loader is None or loader == "vanilla":
        return dependencies
    loader_keys = {
        "forge": "forge",
        "neoforge": "neoforge",
        "fabric": "fabric-loader",
        "legacy_fabric": "fabric-loader",
        "quilt": "quilt-loader",
    }
    try:
        dependency_key = loader_keys[loader]
    except KeyError as xcp:
        raise MinecraftPackExportError(f"Unsupported Modrinth loader: {spec.loader}") from xcp
    assert spec.loader_version is not None
    dependencies[dependency_key] = spec.loader_version
    return dependencies


def _modrinth_env(entry: ModArchiveEntry) -> dict[str, str]:
    client_requirement = (
        "required" if entry.client_pack_policy is ClientPackPolicy.REQUIRED else "optional"
    )
    match entry.mod_type.side:
        case ModSide.CLIENT:
            return {"client": client_requirement, "server": "unsupported"}
        case ModSide.SERVER:
            return {"client": "unsupported", "server": "required"}
        case ModSide.BOTH:
            return {"client": client_requirement, "server": "required"}


def _file_hashes(path: Path) -> dict[str, str]:
    sha1 = hashlib.sha1(usedforsecurity=False)
    sha512 = hashlib.sha512()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            sha1.update(chunk)
            sha512.update(chunk)
    return {"sha1": sha1.hexdigest(), "sha512": sha512.hexdigest()}


def _modrinth_download_url(entry: ModArchiveEntry) -> str:
    metadata = entry.platforms.modrinth
    if metadata is None:
        raise MinecraftPackExportError(f"Modrinth metadata is missing for {entry.mod_name}")
    return metadata.download_url


def _modrinth_entries(
    entries: tuple[ArchiveEntry, ...], spec: MinecraftPackSpec
) -> tuple[WritableArchiveEntry, ...]:
    manifest_files: list[dict[str, object]] = []
    archive_entries: list[WritableArchiveEntry] = []
    bundled_override_root = "server-overrides" if spec.purpose is PackPurpose.SERVER else "overrides"
    for entry in entries:
        if not isinstance(entry, ModArchiveEntry):
            archive_entries.append(entry)
            continue
        if entry.platforms.modrinth is None:
            archive_entries.append(_bundled_mod_entry(entry, override_root=bundled_override_root))
            continue
        if not entry.source_path.is_file():
            raise MinecraftPackExportError(f"Modrinth manifest mod must be a file: {entry.mod_name}")
        manifest_files.append(
            {
                "path": _mod_destination(entry).as_posix(),
                "hashes": _file_hashes(entry.source_path),
                "env": _modrinth_env(entry),
                "downloads": [_modrinth_download_url(entry)],
                "fileSize": entry.source_path.stat().st_size,
            }
        )
    manifest: dict[str, object] = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": spec.version_id,
        "name": spec.name,
        "files": manifest_files,
        "dependencies": _modrinth_dependencies(spec),
    }
    if spec.summary:
        manifest["summary"] = spec.summary
    return (_json_entry("modrinth.index.json", manifest), *archive_entries)


def _curseforge_loader_id(spec: MinecraftPackSpec) -> str | None:
    loader = spec.loader
    if loader is None or loader == "vanilla":
        return None
    loader_names = {
        "forge": "forge",
        "neoforge": "neoforge",
        "fabric": "fabric",
        "legacy_fabric": "fabric",
        "quilt": "quilt",
    }
    try:
        loader_name = loader_names[loader]
    except KeyError as xcp:
        raise MinecraftPackExportError(f"Unsupported CurseForge loader: {spec.loader}") from xcp
    assert spec.loader_version is not None
    return f"{loader_name}-{spec.loader_version}"


def _curseforge_entries(
    entries: tuple[ArchiveEntry, ...], spec: MinecraftPackSpec
) -> tuple[WritableArchiveEntry, ...]:
    manifest_files: list[dict[str, object]] = []
    archive_entries: list[WritableArchiveEntry] = []
    seen_projects: set[int] = set()
    for entry in entries:
        if not isinstance(entry, ModArchiveEntry):
            archive_entries.append(entry)
            continue
        metadata = entry.platforms.curseforge
        if metadata is None:
            archive_entries.append(_bundled_mod_entry(entry))
            continue
        if metadata.project_id in seen_projects:
            raise MinecraftPackExportError(
                f"CurseForge project {metadata.project_id} is referenced more than once"
            )
        seen_projects.add(metadata.project_id)
        manifest_files.append(
            {
                "projectID": metadata.project_id,
                "fileID": metadata.file_id,
                "required": (
                    spec.purpose is not PackPurpose.CLIENT
                    or entry.client_pack_policy is ClientPackPolicy.REQUIRED
                ),
            }
        )
    loader_id = _curseforge_loader_id(spec)
    mod_loaders: list[dict[str, object]] = []
    if loader_id is not None:
        mod_loaders.append({"id": loader_id, "primary": True})
    manifest: dict[str, object] = {
        "minecraft": {
            "version": spec.minecraft_version,
            "modLoaders": mod_loaders,
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": spec.name,
        "version": spec.version_id,
        "author": spec.author,
        "files": manifest_files,
        "overrides": "overrides",
    }
    return (_json_entry("manifest.json", manifest), *archive_entries)


async def export_minecraft_pack(
    entries: tuple[ArchiveEntry, ...],
    spec: MinecraftPackSpec,
    archive_name: str,
) -> Path:
    if not entries:
        raise MinecraftPackExportError("Minecraft pack requires at least one entry")
    _validate_purpose(entries, spec.purpose)
    export_entries: tuple[WritableArchiveEntry, ...]
    match spec.format:
        case PackFormat.GENERIC_ZIP:
            export_entries = entries
        case PackFormat.MODRINTH:
            export_entries = _modrinth_entries(entries, spec)
        case PackFormat.CURSEFORGE:
            export_entries = _curseforge_entries(entries, spec)
    return await compress_archive_entries(
        export_entries,
        archive_name,
        default_suffix=spec.format.suffix,
    )
