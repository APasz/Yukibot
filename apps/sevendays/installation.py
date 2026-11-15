"""Provision fresh Undead Legacy instances from pinned official release archives."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from threading import Event
import time
from typing import Literal
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
import zipfile

import aiofiles
import aiohttp
from pydantic import BaseModel, ConfigDict, Field, field_validator

from _async_utils import run_blocking, run_blocking_cancellable
from archive_safety import ZipArchiveLimits, validated_archive_member_path, validated_zip_entries
from safe_http import PublicAddressResolver, validate_public_http_url


@dataclass(frozen=True, slots=True)
class UndeadLegacyRelease:
    version: str
    game_version: str
    steam_branch: str
    archive_urls: tuple[str, ...]
    core_sha256: str


UNDEAD_LEGACY_RELEASE = UndeadLegacyRelease(
    version="2.7.40",
    game_version="2.6",
    steam_branch="v2.6",
    archive_urls=(
        "https://gitlab.com/subquakesgroup/UndeadLegacyExperimentalPart1/-/archive/"
        "2a188174cf9b6be70cdb9864cb6ea74f917a38b5/UndeadLegacyExperimentalPart1.zip",
        "https://gitlab.com/subquakesgroup/UndeadLegacyExperimentalPart2/-/archive/"
        "39e80938f70ba253aedb2c71c70d6ecf6886a7b3/UndeadLegacyExperimentalPart2.zip",
    ),
    core_sha256="69446cfa36e04513cb435abce3b6f5561c2bd25e1278317893ffb993e8edf511",
)
INSTALL_DOWNLOAD_RETENTION_SECONDS = 72 * 60 * 60
UNDEAD_LEGACY_MANIFEST = Path(".yukibot") / "undead-legacy.json"
UNDEAD_LEGACY_LAUNCHER = "start_undead_legacy.sh"
# Official prefab voxel files compress beyond the default 100:1 upload limit.
_ARCHIVE_LIMITS = ZipArchiveLimits(file_bytes=4 * 1024**3, extracted_bytes=16 * 1024**3, compression_ratio=256)
_DOWNLOAD_MAX_BYTES = _ARCHIVE_LIMITS.extracted_bytes
_COPY_CHUNK_BYTES = 1024 * 1024
_CACHE_FILENAME = re.compile(r"[a-f0-9]{64}(?:-[a-z0-9_]+)?\.(?:zip|part)\Z")
_PACKAGE_ROOTS = frozenset({"Mods", "BepInEx", "doorstop_libs"})
_SUPPORT_FILES = frozenset({"doorstop_config.ini", "run_bepinex.sh", "run_bepinex_server.sh", "winhttp.dll"})
_DOCUMENTS = frozenset({"README.md", "LICENSE", ".gitattributes", ".gitignore"})
_MONO_RUNTIME_DIRECTORY = Path("7DaysToDieServer_Data/MonoBleedingEdge")
_REQUIRED_GAME_FILES = (
    Path("7DaysToDieServer.x86_64"),
    Path("UnityPlayer.so"),
    _MONO_RUNTIME_DIRECTORY / "etc/mono/config",
    _MONO_RUNTIME_DIRECTORY / "x86_64/libmonobdwgc-2.0.so",
    _MONO_RUNTIME_DIRECTORY / "x86_64/libmono-native.so",
    _MONO_RUNTIME_DIRECTORY / "x86_64/libMonoPosixHelper.so",
)
_REQUIRED_FILES = (
    "Mods/UndeadLegacy/ModInfo.xml",
    "Mods/UndeadLegacy/UndeadLegacy.dll",
    "Mods/UndeadLegacy/Resources/Subquake_Items.ulm",
    "Mods/UndeadLegacy/Resources/Subquake_Core.ulm",
    "Mods/UndeadLegacy/Resources/Subquake_Vehicles.ulm",
    "Mods/UndeadLegacy/Resources/Subquake_Power.ulm",
    "BepInEx/core/BepInEx.Preloader.dll",
    "BepInEx/core/0Harmony.dll",
    "BepInEx/core/0Harmony20.dll",
    "BepInEx/core/BepInEx.dll",
    "BepInEx/core/BepInEx.Harmony.dll",
    "BepInEx/core/HarmonyXInterop.dll",
    "BepInEx/core/Mono.Cecil.dll",
    "BepInEx/core/Mono.Cecil.Mdb.dll",
    "BepInEx/core/Mono.Cecil.Pdb.dll",
    "BepInEx/core/Mono.Cecil.Rocks.dll",
    "BepInEx/core/MonoMod.RuntimeDetour.dll",
    "BepInEx/core/MonoMod.Utils.dll",
    "BepInEx/patchers/BepInEx.MultiFolderLoader.dll",
    "doorstop_libs/libdoorstop_x64.so",
    "doorstop_config.ini",
    "run_bepinex.sh",
)
_LAUNCHER = f'''#!/bin/sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$APP_DIR"
MONO_DIR="$APP_DIR/{_MONO_RUNTIME_DIRECTORY.as_posix()}"
export MONO_CONFIG="$MONO_DIR/etc/mono/config"
export MONO_CFG_DIR="$MONO_DIR/etc"
export LD_LIBRARY_PATH="$MONO_DIR/x86_64${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
exec sh "$APP_DIR/run_bepinex.sh" "$APP_DIR/7DaysToDieServer.x86_64" \\
    "-configfile=$APP_DIR/serverconfig.xml" "-UserDataFolder=$APP_DIR/userdata" \\
    -logfile "$APP_DIR/server_stdout.log" -quit -batchmode -noeac -nographics -dedicated "$@"
'''


class UndeadLegacyInstallation(BaseModel):
    """Portable provenance for one completed, managed overhaul installation."""

    profile: Literal["undead_legacy"] = "undead_legacy"
    version: str = Field(min_length=1)
    game_version: str = Field(min_length=1)
    core_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mod_names: tuple[str, ...] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("mod_names")
    @classmethod
    def validate_mod_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if "UndeadLegacy" not in names or len({name.casefold() for name in names}) != len(names):
            raise ValueError("The installation must identify its UndeadLegacy core and unique component folders.")
        for name in names:
            if validated_archive_member_path(name, archive_label="Overhaul component").parts != (name,):
                raise ValueError("Overhaul components must be single folder names.")
        return names


def load_undead_legacy_installation(directory: Path) -> UndeadLegacyInstallation | None:
    pointer = directory / UNDEAD_LEGACY_MANIFEST
    if not pointer.exists():
        return None
    return UndeadLegacyInstallation.model_validate_json(pointer.read_text(encoding="utf-8"))


def sevendays_content_userdata_root(*, directory: Path, userdata_root: Path) -> Path:
    """Locate vanilla or overhaul saves beneath the configured userdata root."""
    if load_undead_legacy_installation(directory) is not None:
        return userdata_root / "UndeadLegacy"
    return userdata_root


def cleanup_install_downloads(cache_directory: Path, *, now: float | None = None) -> None:
    """Expire only this installer's cached archives and interrupted downloads."""
    if not cache_directory.is_dir():
        return
    cutoff = (time.time() if now is None else now) - INSTALL_DOWNLOAD_RETENTION_SECONDS
    for pointer in cache_directory.iterdir():
        if not _CACHE_FILENAME.fullmatch(pointer.name) or pointer.is_symlink():
            continue
        with suppress(FileNotFoundError):
            if pointer.is_file() and pointer.stat().st_mtime <= cutoff:
                pointer.unlink()


def validate_overhaul_archive_source(source: str | None) -> Path | None:
    if source is None or not source.strip():
        return None
    pointer = Path(source).expanduser()
    if not pointer.is_absolute() or not pointer.is_file():
        raise ValueError("The overhaul archive must be an existing ZIP file at an absolute path on the selected node.")
    if not zipfile.is_zipfile(pointer):
        raise ValueError("The overhaul archive is not a ZIP file.")
    if pointer.stat().st_size > _DOWNLOAD_MAX_BYTES:
        raise ValueError("The overhaul archive exceeds the 16 GiB size limit.")
    return pointer


async def _download_archive(url: str, cache_directory: Path, report: Callable[[str], None]) -> Path:
    cache_directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    destination = cache_directory / f"{key}.zip"
    if destination.is_file() and not destination.is_symlink():
        if await run_blocking(zipfile.is_zipfile, destination):
            destination.touch()
            report("Reusing a cached Undead Legacy archive.")
            return destination
        destination.unlink()
    with tempfile.NamedTemporaryFile(prefix=f"{key}-", suffix=".part", dir=cache_directory, delete=False) as handle:
        partial = Path(handle.name)
    try:
        connector = aiohttp.TCPConnector(resolver=PublicAddressResolver())
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            current_url = url
            for _ in range(10):
                validate_public_http_url(current_url)
                async with session.get(current_url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if location is None:
                            raise ValueError("The overhaul download redirected without a destination.")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    if response.content_length is not None:
                        if response.content_length > _DOWNLOAD_MAX_BYTES:
                            raise ValueError("The overhaul download exceeds the 16 GiB size limit.")
                        if shutil.disk_usage(cache_directory).free < response.content_length:
                            raise ValueError("Insufficient space for the overhaul download cache.")
                    downloaded = 0
                    last_report = time.monotonic()
                    async with aiofiles.open(partial, "wb") as output:
                        async for chunk in response.content.iter_chunked(_COPY_CHUNK_BYTES):
                            downloaded += len(chunk)
                            if downloaded > _DOWNLOAD_MAX_BYTES:
                                raise ValueError("The overhaul download exceeds the 16 GiB size limit.")
                            await output.write(chunk)
                            if time.monotonic() - last_report >= 5:
                                report(f"Downloading Undead Legacy: {downloaded / 1024**3:.1f} GiB received.")
                                last_report = time.monotonic()
                    break
            else:
                raise ValueError("The overhaul download redirected too many times.")
        if not await run_blocking(zipfile.is_zipfile, partial):
            raise ValueError("The official overhaul download did not return a ZIP archive.")
        partial.replace(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


def _archive_prefix(paths: tuple[PurePosixPath, ...]) -> tuple[str, ...]:
    prefixes = {
        path.parts[:index]
        for path in paths
        for index, part in enumerate(path.parts[:-1])
        if part == "Mods" and path.parts[index + 1] == "UndeadLegacy"
    }
    if len(prefixes) != 1:
        raise ValueError("The archive must contain one Undead Legacy package, optionally inside a wrapper folder.")
    return prefixes.pop()


def _extract_overhaul_archives(archives: tuple[Path, ...], destination: Path, cancelled: Event) -> None:
    extracted_bytes = 0
    resolved_destination = destination.resolve()
    reserved_paths: dict[str, PurePosixPath] = {}
    for archive_path in archives:
        entries = validated_zip_entries(
            archive_path, archive_label="Undead Legacy archive", limits=_ARCHIVE_LIMITS,
            allow_windows_separators=True,
        )
        prefix = _archive_prefix(tuple(path for _, path in entries))
        archive_bytes = sum(member.file_size for member, _ in entries)
        extracted_bytes += archive_bytes
        if extracted_bytes > _ARCHIVE_LIMITS.extracted_bytes:
            raise ValueError("The combined overhaul archives exceed the 16 GiB extracted-size limit.")
        if shutil.disk_usage(destination).free < archive_bytes:
            raise ValueError("Insufficient space to extract Undead Legacy.")
        with zipfile.ZipFile(archive_path) as archive:
            for member, path in entries:
                if cancelled.is_set():
                    raise InterruptedError("Overhaul installation cancelled.")
                if path.parts[:len(prefix)] != prefix:
                    raise ValueError(f"Archive member is outside the overhaul package: {path}")
                parts = path.parts[len(prefix):]
                if not parts:
                    continue
                relative = PurePosixPath(*parts)
                if parts[0] in _DOCUMENTS and len(parts) == 1:
                    relative = PurePosixPath(".yukibot", "undead-legacy-docs", parts[0])
                elif parts[0] not in _PACKAGE_ROOTS and relative.as_posix() not in _SUPPORT_FILES:
                    raise ValueError(f"Unsupported overhaul package member: {path}")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(f"Overhaul archives must not contain symbolic links: {path}")
                for index in range(1, len(relative.parts) + 1):
                    component = PurePosixPath(*relative.parts[:index])
                    previous = reserved_paths.setdefault(component.as_posix().casefold(), component)
                    if previous != component:
                        raise ValueError(f"Overhaul archive members differ only by case: {path}")
                target = destination.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(resolved_destination):
                    raise ValueError(f"Overhaul package member escapes the app directory: {path}")
                try:
                    if member.filename.endswith(("/", "\\")):
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    existing = target.exists()
                    with archive.open(member) as source, target.open("rb" if existing else "xb") as output:
                        while chunk := source.read(_COPY_CHUNK_BYTES):
                            if cancelled.is_set():
                                raise InterruptedError("Overhaul installation cancelled.")
                            if existing:
                                if output.read(len(chunk)) != chunk:
                                    raise ValueError(f"Overhaul archives contain conflicting files: {path}")
                            else:
                                output.write(chunk)
                        if existing and output.read(1):
                            raise ValueError(f"Overhaul archives contain conflicting files: {path}")
                except (FileExistsError, IsADirectoryError, NotADirectoryError) as xcp:
                    raise ValueError(f"Overhaul package conflicts with an installed path: {path}") from xcp


def _prepare_overhaul_files(directory: Path, archives: tuple[Path, ...], release: UndeadLegacyRelease, cancelled: Event) -> None:
    _extract_overhaul_archives(archives, directory, cancelled)
    for relative in _REQUIRED_FILES:
        pointer = directory / relative
        if not pointer.is_file() or pointer.stat().st_size == 0:
            raise ValueError(f"The overhaul package is incomplete: missing {relative}")
        if pointer.suffix == ".ulm":
            with pointer.open("rb") as asset:
                if asset.read(128).startswith(b"version https://git-lfs.github.com/spec/v1"):
                    raise ValueError(f"The archive contains a Git LFS pointer instead of the required asset: {relative}")
    with (directory / "Mods/UndeadLegacy/UndeadLegacy.dll").open("rb") as core:
        core_hash = hashlib.file_digest(core, "sha256").hexdigest()
    if core_hash != release.core_sha256:
        raise ValueError(f"The overhaul archive does not match supported Undead Legacy {release.version}.")
    mod_names: list[str] = []
    for mod_directory in sorted((directory / "Mods").iterdir()):
        if mod_directory.is_dir() and mod_directory.name.startswith("UndeadLegacy"):
            info = mod_directory / "ModInfo.xml"
            if not info.is_file():
                raise ValueError(f"The overhaul mod is missing ModInfo.xml: {mod_directory.name}")
            ET.parse(info)
            mod_names.append(mod_directory.name)
    config_path = directory / "serverconfig.xml"
    tree = ET.parse(config_path)
    for key, value in (("EACEnabled", "false"), ("UserDataFolder", "userdata")):
        node = next((node for node in tree.getroot().findall("property") if node.get("name") == key), None)
        if node is None:
            node = ET.SubElement(tree.getroot(), "property", {"name": key})
        node.set("value", value)
    tree.write(config_path, encoding="utf-8", xml_declaration=True)
    (directory / "userdata").mkdir(exist_ok=True)
    _configure_undead_legacy_runtime(directory)
    manifest = UndeadLegacyInstallation(
        version=release.version, game_version=release.game_version, core_sha256=core_hash,
        mod_names=tuple(mod_names),
    )
    manifest_path = directory / UNDEAD_LEGACY_MANIFEST
    manifest_path.parent.mkdir(exist_ok=True)
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _configure_undead_legacy_runtime(directory: Path) -> None:
    """Resolve bundled native libraries before Doorstop starts managed code."""

    for relative in _REQUIRED_GAME_FILES:
        pointer = directory / relative
        if not pointer.is_file() or pointer.stat().st_size == 0:
            raise ValueError(f"The 7D2D game runtime is incomplete: missing or empty {relative}")
    mono_config = directory / _MONO_RUNTIME_DIRECTORY / "etc/mono/config"
    mono_tree = ET.parse(mono_config)
    library_targets = {
        "dl": "libdl.so.2",
        "System.Native": "libmono-native.so",
        "System.Net.Security.Native": "libmono-native.so",
    }
    for library, target in library_targets.items():
        maps = [node for node in mono_tree.getroot().findall("dllmap") if node.get("dll") == library]
        if not maps:
            attributes = {"dll": library, "target": target}
            if library != "dl":
                attributes["os"] = "!windows"
            ET.SubElement(mono_tree.getroot(), "dllmap", attributes)
        for node in maps:
            allowed_targets = {target} if library == "dl" else {target, f"$mono_libdir/{target}"}
            if node.get("target") not in allowed_targets:
                raise ValueError(f"The installed Mono configuration has an incompatible {library} mapping.")
            # Doorstop loads before Unity establishes Mono's library root.
            node.set("target", target)
    # Unity's Mono ignores mappings when this file has an XML declaration.
    mono_tree.write(mono_config, encoding="utf-8", xml_declaration=False)
    launcher = directory / UNDEAD_LEGACY_LAUNCHER
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)


async def prepare_undead_legacy_install(
    directory: Path,
    *,
    cache_directory: Path,
    archive_source: Path | None,
    report: Callable[[str], None],
    release: UndeadLegacyRelease = UNDEAD_LEGACY_RELEASE,
) -> None:
    await run_blocking(cleanup_install_downloads, cache_directory)
    if archive_source is None:
        downloaded: list[Path] = []
        for url in release.archive_urls:
            report(f"Downloading Undead Legacy {release.version}.")
            downloaded.append(await _download_archive(url, cache_directory, report))
        archives = tuple(downloaded)
    else:
        report("Using the supplied Undead Legacy archive.")
        archives = (archive_source,)
    report("Extracting and configuring Undead Legacy.")
    cancelled = Event()
    await run_blocking_cancellable(cancelled, _prepare_overhaul_files, directory, archives, release, cancelled)
