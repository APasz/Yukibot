from __future__ import annotations

import asyncio
import enum
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

import config
from _mod_ops import (
    ArchiveDataEntry,
    ArchiveEntry,
    ModArchiveEntry,
    WritableArchiveEntry,
    compress_archive_entries,
)
from apps._config import ClientPackKubeJsScript, ClientPackPolicy, ModSide

_KUBEJS_CLIENT_PACK_SCRIPT_DIRECTORIES = ("server_scripts", "startup_scripts")
_NO_EXCLUDED_KUBEJS_SCRIPTS: frozenset[str] = frozenset()
_MODRINTH_PREFLIGHT_CONCURRENCY = 12


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


def discover_client_pack_kubejs_scripts(
    instance_directory: Path,
    *,
    excluded_paths: frozenset[str] = _NO_EXCLUDED_KUBEJS_SCRIPTS,
) -> tuple[ClientPackKubeJsScript, ...]:
    kubejs_directory = instance_directory / "kubejs"
    scripts: list[ClientPackKubeJsScript] = []
    for directory_name in _KUBEJS_CLIENT_PACK_SCRIPT_DIRECTORIES:
        script_directory = kubejs_directory / directory_name
        if not script_directory.is_dir():
            continue
        for script_path in script_directory.rglob("*"):
            if (
                not script_path.is_file()
                or script_path.is_symlink()
                or script_path.name.casefold() == "example.js"
            ):
                continue
            relative_path = script_path.relative_to(kubejs_directory).as_posix()
            scripts.append(
                ClientPackKubeJsScript(
                    relative_path=relative_path,
                    included=relative_path not in excluded_paths,
                )
            )
    return tuple(sorted(scripts, key=lambda script: script.relative_path.casefold()))


def client_pack_kubejs_entries(
    instance_directory: Path,
    *,
    excluded_paths: frozenset[str],
) -> tuple[ArchiveEntry, ...]:
    scripts = discover_client_pack_kubejs_scripts(
        instance_directory,
        excluded_paths=excluded_paths,
    )
    return tuple(
        ArchiveEntry(
            source_path=instance_directory / "kubejs" / script.relative_path,
            archive_path=PurePosixPath("overrides/kubejs") / script.relative_path,
        )
        for script in scripts
        if script.included
    )


def _json_entry(archive_path: str, payload: dict[str, object]) -> ArchiveDataEntry:
    content = json.dumps(payload, ensure_ascii=False, indent=4).encode(config.STR_ENCODE) + b"\n"
    return ArchiveDataEntry(archive_path=PurePosixPath(archive_path), content=content)


def _mod_destination(entry: ModArchiveEntry) -> PurePosixPath:
    return PurePosixPath("mods") / entry.archive_path


def _bundled_mod_entry(entry: ModArchiveEntry, *, override_root: str = "overrides") -> ArchiveEntry:
    return ArchiveEntry(
        source_path=entry.source_path,
        archive_path=PurePosixPath(override_root) / _mod_destination(entry),
    )


def _validate_purpose(entries: tuple[WritableArchiveEntry, ...], purpose: PackPurpose) -> None:
    if purpose is not PackPurpose.SERVER:
        return
    excluded_mods = tuple(
        entry.mod_name
        for entry in entries
        if isinstance(entry, ModArchiveEntry)
        and entry.mod_type.side is ModSide.CLIENT
    )
    if excluded_mods:
        names = ", ".join(excluded_mods)
        raise MinecraftPackExportError(f"Server packs cannot contain client-only mods: {names}")


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


def _modrinth_env(entry: ModArchiveEntry, purpose: PackPurpose) -> dict[str, str]:
    client_requirement = (
        "required" if entry.client_pack_policy is ClientPackPolicy.REQUIRED else "optional"
    )
    match entry.mod_type.side:
        case ModSide.CLIENT:
            return {"client": client_requirement, "server": "unsupported"}
        case ModSide.SERVER:
            if purpose is PackPurpose.CLIENT:
                return {"client": client_requirement, "server": "required"}
            return {"client": "unsupported", "server": "required"}
        case ModSide.BOTH:
            return {"client": client_requirement, "server": "required"}


def _modrinth_entries(
    entries: tuple[WritableArchiveEntry, ...], spec: MinecraftPackSpec
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
        metadata = entry.platforms.modrinth
        missing_fields = tuple(
            field_name
            for field_name, value in (
                ("filename", metadata.filename),
                ("sha1", metadata.sha1),
                ("sha512", metadata.sha512),
                ("size", metadata.size),
            )
            if value is None
        )
        if missing_fields:
            fields = ", ".join(missing_fields)
            raise MinecraftPackExportError(
                f"Modrinth metadata for {entry.mod_name} is missing {fields}; resolve its provider metadata again"
            )
        assert metadata.filename is not None
        assert metadata.sha1 is not None
        assert metadata.sha512 is not None
        assert metadata.size is not None
        manifest_files.append(
            {
                "path": (PurePosixPath("mods") / metadata.filename).as_posix(),
                "hashes": {"sha1": metadata.sha1, "sha512": metadata.sha512},
                "env": _modrinth_env(entry, spec.purpose),
                "downloads": [metadata.download_url],
                "fileSize": metadata.size,
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


async def _preflight_remote_download(
    *,
    mod_name: str,
    download_url: str,
    http: httpx.AsyncClient,
) -> None:
    try:
        async with http.stream(
            "GET",
            download_url,
            headers={"range": "bytes=0-0"},
            follow_redirects=True,
            timeout=30,
        ) as response:
            response.raise_for_status()
    except httpx.HTTPError as xcp:
        raise MinecraftPackExportError(
            f"Remote download for {mod_name} is not reachable: {download_url} ({xcp})"
        ) from xcp


async def _preflight_modrinth_downloads(
    entries: tuple[WritableArchiveEntry, ...],
    *,
    http: httpx.AsyncClient,
) -> None:
    remote_entries = tuple(
        entry
        for entry in entries
        if isinstance(entry, ModArchiveEntry) and entry.platforms.modrinth is not None
    )
    semaphore = asyncio.Semaphore(_MODRINTH_PREFLIGHT_CONCURRENCY)

    async def preflight(entry: ModArchiveEntry) -> None:
        assert entry.platforms.modrinth is not None
        async with semaphore:
            await _preflight_remote_download(
                mod_name=entry.mod_name,
                download_url=entry.platforms.modrinth.download_url,
                http=http,
            )

    await asyncio.gather(*(preflight(entry) for entry in remote_entries))


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
    entries: tuple[WritableArchiveEntry, ...], spec: MinecraftPackSpec
) -> tuple[WritableArchiveEntry, ...]:
    manifest_files: list[dict[str, object]] = []
    archive_entries: list[WritableArchiveEntry] = []
    seen_projects: set[int] = set()
    unsupported_mods: list[str] = []
    for entry in entries:
        if not isinstance(entry, ModArchiveEntry):
            archive_entries.append(entry)
            continue
        metadata = entry.platforms.curseforge
        if metadata is None:
            if entry.bundle_eligible:
                archive_entries.append(_bundled_mod_entry(entry))
            else:
                unsupported_mods.append(entry.mod_name)
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
    if unsupported_mods:
        names = ", ".join(sorted(unsupported_mods, key=str.casefold))
        raise MinecraftPackExportError(
            "CurseForge export cannot include these non-CurseForge mods because bundling is disabled: "
            f"{names}"
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
    if spec.summary:
        manifest["description"] = spec.summary
    return (_json_entry("manifest.json", manifest), *archive_entries)


async def export_minecraft_pack(
    entries: tuple[WritableArchiveEntry, ...],
    spec: MinecraftPackSpec,
    archive_name: str,
    *,
    http: httpx.AsyncClient | None = None,
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
            remote_entries = tuple(
                entry
                for entry in entries
                if isinstance(entry, ModArchiveEntry) and entry.platforms.modrinth is not None
            )
            if remote_entries:
                owned_http = http is None
                client = http or httpx.AsyncClient()
                try:
                    await _preflight_modrinth_downloads(remote_entries, http=client)
                finally:
                    if owned_http:
                        await client.aclose()
        case PackFormat.CURSEFORGE:
            export_entries = _curseforge_entries(entries, spec)
    return await compress_archive_entries(
        export_entries,
        archive_name,
        default_suffix=spec.format.suffix,
    )
