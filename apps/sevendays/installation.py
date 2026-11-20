"""Provision fresh Undead Legacy instances from pinned official release archives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
from apps._config import AppVersion
from apps._steam import parse_steam_keyvalues_mapping
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
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_CORE_DLL = "Mods/UndeadLegacy/UndeadLegacy.dll"
_MONO_RUNTIME_DIRECTORY = Path("7DaysToDieServer_Data/MonoBleedingEdge")
_MONO_CONFIG = _MONO_RUNTIME_DIRECTORY / "etc/mono/config"
_RUNTIME_CONFIGURATION_FILES = frozenset({_MONO_CONFIG.as_posix(), UNDEAD_LEGACY_LAUNCHER, "serverconfig.xml"})
_MONO_LIBRARY_TARGETS = {
    "dl": "libdl.so.2",
    "System.Native": "libmono-native.so",
    "System.Net.Security.Native": "libmono-native.so",
}
_REQUIRED_GAME_FILES = (
    Path("7DaysToDieServer.x86_64"),
    Path("UnityPlayer.so"),
    _MONO_CONFIG,
    _MONO_RUNTIME_DIRECTORY / "x86_64/libmonobdwgc-2.0.so",
    _MONO_RUNTIME_DIRECTORY / "x86_64/libmono-native.so",
    _MONO_RUNTIME_DIRECTORY / "x86_64/libMonoPosixHelper.so",
)
_REQUIRED_FILES = (
    "Mods/UndeadLegacy/ModInfo.xml",
    _CORE_DLL,
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


class UndeadLegacyManagedFile(BaseModel):
    path: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        if path not in _REQUIRED_FILES:
            raise ValueError("Unknown managed Undead Legacy file.")
        return path


class UndeadLegacyInstallation(BaseModel):
    """Portable provenance for one completed, managed overhaul installation."""

    profile: Literal["undead_legacy"] = "undead_legacy"
    version: str = Field(min_length=1)
    game_version: str = Field(min_length=1)
    core_sha256: str = Field(pattern=_SHA256_PATTERN)
    mod_names: tuple[str, ...] = Field(min_length=1)
    managed_files: tuple[UndeadLegacyManagedFile, ...] = ()

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

    @field_validator("managed_files")
    @classmethod
    def validate_managed_files(cls, files: tuple[UndeadLegacyManagedFile, ...]) -> tuple[UndeadLegacyManagedFile, ...]:
        if files and {file.path for file in files} != set(_REQUIRED_FILES):
            raise ValueError("Managed fingerprints must cover all required Undead Legacy files.")
        if len({file.path for file in files}) != len(files):
            raise ValueError("Managed Undead Legacy fingerprints must be unique.")
        return files


@dataclass(frozen=True, slots=True)
class UndeadLegacyHealthIssue:
    message: str
    repairable: bool
    package_file: Path | None = None


def undead_legacy_download_cache_directory(*, root: Path, node_name: str) -> Path:
    node_key = hashlib.sha256(node_name.encode("utf-8")).hexdigest()[:16]
    return root / "app-install-downloads" / node_key


def _file_sha256(pointer: Path) -> str:
    with pointer.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _is_git_lfs_pointer(pointer: Path) -> bool:
    with pointer.open("rb") as source:
        return source.read(128).startswith(b"version https://git-lfs.github.com/spec/v1")


def _managed_path(resolved_directory: Path, relative: str | Path, *, allow_symlink: bool = True) -> Path:
    pointer = resolved_directory / relative
    if not pointer.resolve().is_relative_to(resolved_directory):
        raise ValueError(f"Managed path escapes the installation: {relative}")
    if not allow_symlink and pointer.is_symlink():
        raise ValueError(f"Managed configuration must not be a symbolic link: {relative}")
    return pointer


def _managed_file_fingerprints(directory: Path) -> tuple[UndeadLegacyManagedFile, ...]:
    files: list[UndeadLegacyManagedFile] = []
    for relative in _REQUIRED_FILES:
        pointer = directory / relative
        digest = _file_sha256(pointer)
        files.append(UndeadLegacyManagedFile(path=relative, size_bytes=pointer.stat().st_size, sha256=digest))
    return tuple(files)


def load_undead_legacy_installation(directory: Path) -> UndeadLegacyInstallation | None:
    pointer = _managed_path(directory.resolve(), UNDEAD_LEGACY_MANIFEST)
    if not pointer.exists() and not pointer.is_symlink():
        return None
    return UndeadLegacyInstallation.model_validate_json(pointer.read_text(encoding="utf-8"))


def inspect_undead_legacy_installation(
    directory: Path,
    *,
    game_version: AppVersion | None,
    deep: bool = False,
) -> tuple[UndeadLegacyHealthIssue, ...]:
    """Check managed files and launch requirements without changing the installation."""
    installation = load_undead_legacy_installation(directory)
    if installation is None:
        raise ValueError("This instance is not a managed Undead Legacy installation.")
    issues: list[UndeadLegacyHealthIssue] = []
    resolved_directory = directory.resolve()
    for relative in (*_REQUIRED_GAME_FILES, *_REQUIRED_FILES, UNDEAD_LEGACY_LAUNCHER, "serverconfig.xml"):
        try:
            _managed_path(resolved_directory, relative, allow_symlink=str(relative) not in _RUNTIME_CONFIGURATION_FILES)
        except ValueError as error:
            issues.append(UndeadLegacyHealthIssue(str(error), False))
    if issues:
        return tuple(issues)
    manifest = directory / "steamapps/appmanifest_294420.acf"
    if manifest.is_file():
        try:
            state = parse_steam_keyvalues_mapping(
                manifest.read_text(encoding="utf-8"), source_label="7D2D Steam manifest"
            ).get("AppState")
            if not isinstance(state, Mapping):
                raise ValueError("Missing AppState.")
            user_config = state.get("MountedConfig", state.get("UserConfig"))
            branch = (
                next(
                    (
                        value
                        for key, value in user_config.items()
                        if isinstance(key, str) and key.casefold() == "betakey"
                    ),
                    "public",
                )
                if isinstance(user_config, Mapping)
                else "public"
            )
            if not isinstance(branch, str) or not branch.strip():
                raise ValueError("Invalid game branch.")
            if branch != f"v{installation.game_version}":
                issues.append(
                    UndeadLegacyHealthIssue(
                        f"Base game branch is {branch!r}; this UL release requires v{installation.game_version}. Restore the matching game runtime.",
                        False,
                    )
                )
        except (OSError, ValueError) as error:
            issues.append(UndeadLegacyHealthIssue(f"Cannot read the base game Steam manifest: {error}", False))
    elif game_version is None or game_version.main is None:
        issues.append(
            UndeadLegacyHealthIssue(
                "Cannot verify the base game version: no Steam manifest or detected game version.",
                False,
            )
        )
    if game_version is not None and game_version.main is not None and game_version.main != installation.game_version:
        issues.append(
            UndeadLegacyHealthIssue(
                f"Detected game {game_version.display_value}; this UL release requires {installation.game_version}.",
                False,
            )
        )

    fingerprints = {file.path: file for file in installation.managed_files}
    if deep and not fingerprints:
        issues.append(
            UndeadLegacyHealthIssue(
                "Per-file fingerprints are unavailable for this older installation. Run Repair Undead Legacy to establish them.",
                True,
            )
        )
    required_files = (
        *((relative, False) for relative in _REQUIRED_GAME_FILES),
        *((Path(relative), True) for relative in _REQUIRED_FILES),
    )
    for relative, repairable in required_files:
        pointer = directory / relative
        if not pointer.is_file() or pointer.stat().st_size == 0:
            issues.append(UndeadLegacyHealthIssue(
                f"Missing or empty required file: {relative}", repairable, relative if repairable else None,
            ))
            continue
        if pointer.suffix == ".ulm" and _is_git_lfs_pointer(pointer):
            issues.append(UndeadLegacyHealthIssue(f"Asset is a Git LFS placeholder: {relative}", True, relative))
            continue
        fingerprint = fingerprints.get(str(relative))
        if fingerprint is not None and pointer.stat().st_size != fingerprint.size_bytes:
            issues.append(UndeadLegacyHealthIssue(f"Required file has an unexpected size: {relative}", True, relative))
            continue
        expected_hash = (
            installation.core_sha256
            if str(relative) == _CORE_DLL
            else (fingerprint.sha256 if deep and fingerprint is not None else None)
        )
        if expected_hash is not None:
            digest = _file_sha256(pointer)
            if digest != expected_hash:
                issues.append(
                    UndeadLegacyHealthIssue(
                        f"Required file does not match the installed release: {relative}",
                        True,
                        relative,
                    )
                )

    mono_config = directory / _MONO_CONFIG
    if mono_config.is_file():
        try:
            root = ET.parse(mono_config).getroot()
            for library, target in _MONO_LIBRARY_TARGETS.items():
                maps = [node for node in root.findall("dllmap") if node.get("dll") == library]
                if not maps or any(node.get("target") != target for node in maps):
                    issues.append(UndeadLegacyHealthIssue(f"Mono library mapping needs repair: {library}", True))
            if mono_config.read_bytes().startswith(b"<?xml"):
                issues.append(UndeadLegacyHealthIssue("Mono configuration has an incompatible XML declaration.", True))
        except (OSError, ET.ParseError) as error:
            issues.append(
                UndeadLegacyHealthIssue(
                    f"Cannot read Mono configuration; restore the matching base game runtime: {error}",
                    False,
                )
            )
    launcher = directory / UNDEAD_LEGACY_LAUNCHER
    if (
        not launcher.is_file()
        or launcher.read_bytes() != _LAUNCHER.encode("utf-8")
        or not launcher.stat().st_mode & stat.S_IXUSR
    ):
        issues.append(UndeadLegacyHealthIssue("The managed Undead Legacy launcher is missing or modified.", True))
    try:
        serverconfig = ET.parse(directory / "serverconfig.xml").getroot()
        eac_values = [node.get("value") for node in serverconfig.findall("property") if node.get("name") == "EACEnabled"]
        if len(eac_values) != 1 or eac_values[0] is None or eac_values[0].casefold() != "false":
            issues.append(UndeadLegacyHealthIssue("Undead Legacy requires one EACEnabled property set to false.", True))
    except (OSError, ET.ParseError) as error:
        issues.append(UndeadLegacyHealthIssue(f"Cannot read serverconfig.xml: {error}", False))
    return tuple(issues)


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


def _overhaul_member_target(path: PurePosixPath, prefix: tuple[str, ...]) -> PurePosixPath | None:
    if path.parts[: len(prefix)] != prefix:
        raise ValueError(f"Archive member is outside the overhaul package: {path}")
    parts = path.parts[len(prefix) :]
    if not parts:
        return None
    relative = PurePosixPath(*parts)
    if parts[0] in _DOCUMENTS and len(parts) == 1:
        return PurePosixPath(".yukibot", "undead-legacy-docs", parts[0])
    if parts[0] not in _PACKAGE_ROOTS and relative.as_posix() not in _SUPPORT_FILES:
        raise ValueError(f"Unsupported overhaul package member: {path}")
    return relative


def _extract_overhaul_archives(
    archives: tuple[Path, ...],
    destination: Path,
    cancelled: Event,
    *,
    selected_files: frozenset[str] | None = None,
) -> None:
    extracted_bytes = 0
    resolved_destination = destination.resolve()
    reserved_paths: dict[str, PurePosixPath] = {}
    for archive_path in archives:
        entries = validated_zip_entries(
            archive_path,
            archive_label="Undead Legacy archive",
            limits=_ARCHIVE_LIMITS,
            allow_windows_separators=True,
        )
        prefix = _archive_prefix(tuple(path for _, path in entries))
        archive_bytes = sum(member.file_size for member, _ in entries)
        extracted_bytes += archive_bytes
        if extracted_bytes > _ARCHIVE_LIMITS.extracted_bytes:
            raise ValueError("The combined overhaul archives exceed the 16 GiB extracted-size limit.")
        selected_bytes = sum(
            member.file_size
            for member, path in entries
            if (relative := _overhaul_member_target(path, prefix)) is not None
            and (selected_files is None or relative.as_posix() in selected_files)
        )
        if shutil.disk_usage(destination).free < selected_bytes:
            raise ValueError("Insufficient space to extract Undead Legacy.")
        with zipfile.ZipFile(archive_path) as archive:
            for member, path in entries:
                if cancelled.is_set():
                    raise InterruptedError("Overhaul installation cancelled.")
                relative = _overhaul_member_target(path, prefix)
                if relative is None:
                    continue
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(f"Overhaul archives must not contain symbolic links: {path}")
                for index in range(1, len(relative.parts) + 1):
                    component = PurePosixPath(*relative.parts[:index])
                    previous = reserved_paths.setdefault(component.as_posix().casefold(), component)
                    if previous != component:
                        raise ValueError(f"Overhaul archive members differ only by case: {path}")
                if selected_files is not None and relative.as_posix() not in selected_files:
                    continue
                target = destination.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(resolved_destination):
                    raise ValueError(f"Overhaul package member escapes the app directory: {path}")
                try:
                    if member.filename.endswith(("/", "\\")):
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    existing = target.exists()
                    with (
                        archive.open(member) as source,
                        target.open("rb" if existing else "xb") as output,
                    ):
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


def _validate_overhaul_files(directory: Path, release: UndeadLegacyRelease) -> tuple[UndeadLegacyManagedFile, ...]:
    for relative in _REQUIRED_FILES:
        pointer = directory / relative
        if not pointer.is_file() or pointer.stat().st_size == 0:
            raise ValueError(f"The overhaul package is incomplete: missing {relative}")
        if pointer.suffix == ".ulm" and _is_git_lfs_pointer(pointer):
            raise ValueError(
                f"The archive contains a Git LFS pointer instead of the required asset: {relative}"
            )
    files = _managed_file_fingerprints(directory)
    core_hash = next(file.sha256 for file in files if file.path == _CORE_DLL)
    if core_hash != release.core_sha256:
        raise ValueError(f"The overhaul archive does not match supported Undead Legacy {release.version}.")
    return files


def _prepare_overhaul_files(directory: Path, archives: tuple[Path, ...], release: UndeadLegacyRelease, cancelled: Event) -> None:
    _extract_overhaul_archives(archives, directory, cancelled)
    files = _validate_overhaul_files(directory, release)
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
        version=release.version, game_version=release.game_version, core_sha256=release.core_sha256,
        mod_names=tuple(mod_names), managed_files=files,
    )
    manifest_path = directory / UNDEAD_LEGACY_MANIFEST
    manifest_path.parent.mkdir(exist_ok=True)
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _configure_undead_legacy_runtime(directory: Path, *, repair: bool = False) -> None:
    """Resolve bundled native libraries before Doorstop starts managed code."""

    resolved_directory = directory.resolve()
    launcher = _managed_path(resolved_directory, UNDEAD_LEGACY_LAUNCHER, allow_symlink=False)
    for relative in _REQUIRED_GAME_FILES:
        pointer = _managed_path(resolved_directory, relative, allow_symlink=relative != _MONO_CONFIG)
        if not pointer.is_file() or pointer.stat().st_size == 0:
            raise ValueError(f"The 7D2D game runtime is incomplete: missing or empty {relative}")
    mono_config = directory / _MONO_CONFIG
    mono_tree = ET.parse(mono_config)
    for library, target in _MONO_LIBRARY_TARGETS.items():
        maps = [node for node in mono_tree.getroot().findall("dllmap") if node.get("dll") == library]
        if not maps:
            attributes = {"dll": library, "target": target}
            if library != "dl":
                attributes["os"] = "!windows"
            ET.SubElement(mono_tree.getroot(), "dllmap", attributes)
        for node in maps:
            allowed_targets = {target} if library == "dl" else {target, f"$mono_libdir/{target}"}
            if not repair and node.get("target") not in allowed_targets:
                raise ValueError(f"The installed Mono configuration has an incompatible {library} mapping.")
            # Doorstop loads before Unity establishes Mono's library root.
            node.set("target", target)
    # Unity's Mono ignores mappings when this file has an XML declaration.
    mono_tree.write(mono_config, encoding="utf-8", xml_declaration=False)
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


def _repair_runtime_configuration(directory: Path) -> None:
    serverconfig = _managed_path(directory.resolve(), "serverconfig.xml", allow_symlink=False)
    _configure_undead_legacy_runtime(directory, repair=True)
    tree = ET.parse(serverconfig)
    nodes = [node for node in tree.getroot().findall("property") if node.get("name") == "EACEnabled"]
    if not nodes:
        nodes = [ET.SubElement(tree.getroot(), "property", {"name": "EACEnabled"})]
    nodes[0].set("value", "false")
    for node in nodes[1:]:
        tree.getroot().remove(node)
    tree.write(serverconfig, encoding="utf-8", xml_declaration=True)


def _repair_overhaul_files(
    directory: Path,
    archives: tuple[Path, ...],
    installation: UndeadLegacyInstallation,
    release: UndeadLegacyRelease,
    cancelled: Event,
) -> None:
    with tempfile.TemporaryDirectory(prefix="ul-repair-", dir=directory / ".yukibot") as temporary:
        staging = Path(temporary)
        _extract_overhaul_archives(archives, staging, cancelled, selected_files=frozenset(_REQUIRED_FILES))
        files = _validate_overhaul_files(staging, release)
        resolved_directory = directory.resolve()
        for file in files:
            if cancelled.is_set():
                raise InterruptedError("Undead Legacy repair cancelled.")
            target = _managed_path(resolved_directory, file.path)
            if target.is_file() and _file_sha256(target) == file.sha256:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            (staging / file.path).replace(target)
        _repair_runtime_configuration(directory)
        repaired = installation.model_copy(update={"managed_files": files})
        staged_manifest = staging / "installation.json"
        staged_manifest.write_text(repaired.model_dump_json(indent=2) + "\n", encoding="utf-8")
        staged_manifest.replace(directory / UNDEAD_LEGACY_MANIFEST)


async def repair_undead_legacy_installation(
    directory: Path,
    *,
    game_version: AppVersion | None,
    cache_directory: Path,
    report: Callable[[str], None],
    release: UndeadLegacyRelease = UNDEAD_LEGACY_RELEASE,
) -> None:
    """Restore this release's managed files; preserve saves, mod configs, and add-ons."""
    installation = load_undead_legacy_installation(directory)
    if installation is None:
        raise ValueError("This instance is not a managed Undead Legacy installation.")
    if (installation.version, installation.game_version, installation.core_sha256) != (
        release.version,
        release.game_version,
        release.core_sha256,
    ):
        raise ValueError("Repair is unavailable for this UL release; it will not install a different version.")
    issues = await run_blocking(
        inspect_undead_legacy_installation,
        directory,
        game_version=game_version,
        deep=True,
    )
    unrepairable = tuple(issue.message for issue in issues if not issue.repairable)
    if unrepairable:
        raise ValueError("Repair requires attention to the base installation first: " + " ".join(unrepairable))
    if not issues and installation.managed_files:
        report("Undead Legacy is healthy; no files need repair.")
        return
    if installation.managed_files and not any(issue.package_file is not None for issue in issues):
        report("Repairing Undead Legacy launch configuration.")
        await run_blocking_cancellable(Event(), _repair_runtime_configuration, directory)
    else:
        await run_blocking(cleanup_install_downloads, cache_directory)
        archives: list[Path] = []
        for url in release.archive_urls:
            archives.append(await _download_archive(url, cache_directory, report))
        report("Restoring managed Undead Legacy files.")
        cancelled = Event()
        await run_blocking_cancellable(
            cancelled,
            _repair_overhaul_files,
            directory,
            tuple(archives),
            installation,
            release,
            cancelled,
        )
    remaining = await run_blocking(inspect_undead_legacy_installation, directory, game_version=game_version)
    if remaining:
        raise ValueError("Repair incomplete: " + " ".join(issue.message for issue in remaining))
    report("Undead Legacy repair completed.")
