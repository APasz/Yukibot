"""Export managed Undead Legacy clients for Windows and Proton."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from itertools import chain
from ntpath import isreserved
from pathlib import Path, PurePosixPath, PureWindowsPath

from _mod_ops import ArchiveDataEntry, ArchiveEntry, ClientPackValidationError, ModArchiveEntry
from archive_safety import validate_archive_member_layout, validated_archive_member_path
from apps._config import AppVersion
from apps._mod import Mod_Manager
from apps.sevendays.installation import (
    UNDEAD_LEGACY_RELEASE,
    UndeadLegacyManagedFile,
    UndeadLegacyRelease,
    _CORE_DLL,
    _REQUIRED_FILES,
    _file_sha256,
    _is_git_lfs_pointer,
    _managed_path,
    require_undead_legacy_installation,
)


_RUNTIME_DEFAULTS = Path(__file__).with_name("client_runtime")
_LAUNCHER_NAME = "Start Undead Legacy.cmd"
_GUIDE_NAME = "README-Undead-Legacy.txt"


def _runtime_entry(directory: Path, file: UndeadLegacyManagedFile) -> ArchiveEntry:
    # Client defaults come from the pinned release, independently of server settings.
    relative = PurePosixPath(file.path)
    bundled_configuration = relative.parts[:2] == ("BepInEx", "config") or file.path == "doorstop_config.ini"
    root = _RUNTIME_DEFAULTS if bundled_configuration else directory
    pointer = _managed_path(root.resolve(), file.path, allow_symlink=False)
    if not pointer.is_file() or pointer.stat().st_size != file.size_bytes or _file_sha256(pointer) != file.sha256:
        raise ValueError(
            f"Required client loader file is missing or does not match the pinned UL release: {file.path}. "
            "Restore it from the matching official Undead Legacy archive."
        )
    return ArchiveEntry(source_path=pointer, archive_path=PurePosixPath(file.path))


def undead_legacy_client_file_hashes(directory: Path) -> dict[PurePosixPath, str]:
    """Reuse recorded asset digests while the publication hash streams their bytes."""
    try:
        installation = require_undead_legacy_installation(directory)
    except (OSError, ValueError) as error:
        raise ClientPackValidationError(str(error)) from error
    hashes = {
        PurePosixPath(file.path): file.sha256
        for file in installation.managed_files if file.path.startswith("Mods/")
    }
    hashes[PurePosixPath(_CORE_DLL)] = installation.core_sha256
    return hashes


def _client_members(entries: tuple[ArchiveEntry | ArchiveDataEntry, ...]) -> Iterator[tuple[PurePosixPath, bool]]:
    for entry in entries:
        if isinstance(entry, ArchiveDataEntry):
            yield entry.archive_path, False
            continue
        source = entry.source_path
        paths = chain((source,), source.rglob("*")) if source.is_dir() else (source,)
        for path in paths:
            if path.is_symlink():
                raise ValueError(f"Client-pack source cannot be a symbolic link: {path}")
            is_directory = path.is_dir()
            if not is_directory and not path.is_file():
                raise ValueError(f"Client-pack source is missing or unsupported: {path}")
            if path.suffix.casefold() == ".ulm" and path.is_file() and _is_git_lfs_pointer(path):
                raise ValueError(f"Undead Legacy client asset is a Git LFS placeholder: {path}")
            archive_path = (
                entry.archive_path if path == source else
                entry.archive_path_for_relative(PurePosixPath(path.relative_to(source).as_posix()))
            )
            yield archive_path, is_directory


def _validate_client_layout(entries: tuple[ArchiveEntry | ArchiveDataEntry, ...]) -> None:
    def windows_members() -> Iterator[tuple[PurePosixPath, bool]]:
        for path, is_directory in _client_members(entries):
            validated_archive_member_path(path.as_posix(), archive_label="UL Windows client pack")
            if isreserved(path.as_posix()):
                raise ValueError(f"Client-pack path is not supported on Windows: {path}")
            yield path, is_directory

    validate_archive_member_layout(windows_members(), archive_label="UL Windows client pack")


def _windows_launcher(release: UndeadLegacyRelease) -> bytes:
    required_files = (
        "7DaysToDie.exe",
        *(file.path for file in release.windows_runtime_files),
        *(path for path in _REQUIRED_FILES if path.startswith("Mods/")),
    )
    checks = "\n".join(f'    "{PureWindowsPath(path)}"' for path in required_files)
    script = f'''@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 exit /b 1
for %%F in (
{checks}
) do (
    if not exist "%%~F" (
        echo Missing required file: %%~F
        echo Extract the complete client pack beside 7DaysToDie.exe.
        pause
        popd
        exit /b 1
    )
)
start "" /wait "7DaysToDie.exe" -noeac %*
set "GAME_EXIT=%ERRORLEVEL%"
popd
if not "%GAME_EXIT%"=="0" (
    echo Undead Legacy exited with code %GAME_EXIT%. See the game logs for details.
    pause
)
exit /b %GAME_EXIT%
'''
    return script.replace("\n", "\r\n").encode("ascii")


def _setup_guide(release: UndeadLegacyRelease) -> bytes:
    return f'''Undead Legacy {release.version} client pack - Windows and Proton

INSTALLATION
1. In Steam, select the {release.steam_branch} beta branch for 7 Days to Die and
   wait for the installation to finish. This pack requires game {release.game_version}.
2. On Linux, select a Proton version in Steam's Compatibility settings and let
   Steam finish installing the Windows game before extracting this pack.
3. Use Steam's Browse local files option to open the game directory.
4. Extract this ZIP directly into that directory, merging folders and replacing
   pack files when prompted. Mods, BepInEx, winhttp.dll and doorstop_config.ini
   belong beside 7DaysToDie.exe. Do not extract the whole ZIP into Mods.
   The pack includes the complete managed UL overhaul and selected add-ons.
   Extraction does not remove old mods; start with a clean game installation
   if you previously used a different mod pack.

WINDOWS
Keep Steam running and double-click "{_LAUNCHER_NAME}". This starts the client
with EAC disabled and forwards any additional command-line arguments.
Alternatively, launch through Steam, select Show game launcher, disable
EasyAntiCheat in the game launcher, then Save & Run.

LINUX WITH PROTON / STEAM DECK
Use the Windows game through Proton. In Steam > Properties > General > Launch
Options, set:
WINEDLLOVERRIDES="winhttp.dll=n,b" %command%
If you already have launch options, combine them with this setting and keep
one %command% placeholder.
Launch through Steam, select Show game launcher, disable EasyAntiCheat in the
game launcher, then Save & Run. The Windows helper is optional for Windows;
on Proton, use Steam with the DLL override above.
Native Linux clients are not supported by this pack.

CHECKING STARTUP
The main menu should show Undead Legacy {release.version}.
BepInEx writes its loader log to BepInEx/LogOutput.log in the game directory.
Use the matching server release and the add-ons supplied by this pack.

Official release: https://ul.subquake.com/download
Proton loader setup: https://docs.bepinex.dev/master/articles/advanced/steam_interop.html#protonwine
'''.replace("\n", "\r\n").encode("utf-8")


def build_undead_legacy_client_pack_entries(
    directory: Path,
    manager: Mod_Manager,
    entries: tuple[ArchiveEntry, ...],
    *,
    game_version: AppVersion | None,
    release: UndeadLegacyRelease = UNDEAD_LEGACY_RELEASE,
) -> tuple[ArchiveEntry | ArchiveDataEntry, ...]:
    """Add protected overhaul components and a validated game-root runtime payload."""
    try:
        installation = require_undead_legacy_installation(directory)
        if (installation.version, installation.game_version, installation.core_sha256) != (
            release.version, release.game_version, release.core_sha256,
        ):
            raise ValueError("Client-pack export is unavailable for this Undead Legacy release.")
        if game_version is not None and game_version.main is not None and game_version.main != release.game_version:
            raise ValueError(f"This Undead Legacy client pack requires game {release.game_version}.")
        resolved_directory = directory.resolve()
        for relative in _REQUIRED_FILES:
            if not relative.startswith("Mods/"):
                continue
            pointer = _managed_path(resolved_directory, relative, allow_symlink=False)
            if not pointer.is_file() or not pointer.stat().st_size:
                raise ValueError(f"Required Undead Legacy client file is missing or empty: {relative}")
        if _file_sha256(directory / _CORE_DLL) != release.core_sha256:
            raise ValueError("Undead Legacy client core does not match the pinned release.")

        managed_entries: list[ModArchiveEntry] = []
        for name in installation.mod_names:
            mod = manager.get(name)
            expected_path = _managed_path(resolved_directory, Path("Mods") / name, allow_symlink=False)
            if mod.storage_path.resolve() != expected_path.resolve() or not (expected_path / "ModInfo.xml").is_file():
                raise ValueError(f"Managed Undead Legacy component is missing or misplaced: {name}")
            managed_entries.append(ModArchiveEntry.from_mod(mod))
        mod_names = frozenset(installation.mod_names)
        if any(isinstance(entry, ModArchiveEntry) and entry.mod_name in mod_names for entry in entries):
            raise ValueError("Managed Undead Legacy components cannot also be selected as add-ons.")
        output: list[ArchiveEntry | ArchiveDataEntry] = []
        for entry in (*managed_entries, *entries):
            if isinstance(entry, ModArchiveEntry):
                path = PurePosixPath("Mods") / entry.archive_path
            else:
                path = entry.archive_path.relative_to("overrides")
            output.append(replace(entry, archive_path=path))
        output.extend(_runtime_entry(directory, file) for file in release.windows_runtime_files)
        output.extend((
            ArchiveDataEntry(PurePosixPath(_LAUNCHER_NAME), _windows_launcher(release)),
            ArchiveDataEntry(PurePosixPath(_GUIDE_NAME), _setup_guide(release)),
        ))
        result = tuple(output)
        _validate_client_layout(result)
        return result
    except (OSError, ValueError) as error:
        raise ClientPackValidationError(f"Cannot build the Undead Legacy Windows/Proton client pack: {error}") from error
