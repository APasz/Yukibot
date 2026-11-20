from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from threading import Event
import time
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, Mock
import xml.etree.ElementTree as ET
import zipfile

import pytest

import config
from _manager import AppDetailsUpdate, App_Manager
from _mod_ops import ArchiveDataEntry, ClientPackSelection, ClientPackValidationError
from archive_safety import ZipArchiveLimits, validated_zip_entries
from apps._config import AppVersion, App_Config, Mod_Config, ModClassificationOverride, ModDownloadBlockReason, ModPlacement, ModType, SteamUpdateConfig
from apps.sevendays import Mod_7D2D, SevenDays, SevenDays_Settings
from apps.sevendays import installation
from apps.sevendays import client_pack
from node_api import client_pack as node_client_pack
from node_api.client_pack import NodeClientPackService
from node_api.mod import NodeDownloadRequest


_CORE = b"supported overhaul core"
_RELEASE = replace(installation.UNDEAD_LEGACY_RELEASE, core_sha256=hashlib.sha256(_CORE).hexdigest())


@pytest.fixture
def package_files() -> dict[str, bytes]:
    return {
        "Mods/UndeadLegacy/ModInfo.xml": b'<xml><Name value="UndeadLegacy"/><Version value="2.7.01"/></xml>',
        "Mods/UndeadLegacy/UndeadLegacy.dll": _CORE,
        "Mods/UndeadLegacy/Resources/Subquake_Items.ulm": b"items",
        "Mods/UndeadLegacy/Resources/Subquake_Core.ulm": b"core",
        "Mods/UndeadLegacy/Resources/Subquake_Vehicles.ulm": b"vehicles",
        "Mods/UndeadLegacy/Resources/Subquake_Power.ulm": b"power",
        "BepInEx/core/BepInEx.Preloader.dll": b"preloader",
        "BepInEx/core/0Harmony.dll": b"harmony",
        "BepInEx/core/0Harmony20.dll": b"harmony 2",
        "BepInEx/core/BepInEx.dll": b"bepinex",
        "BepInEx/core/BepInEx.Harmony.dll": b"bepinex harmony",
        "BepInEx/core/HarmonyXInterop.dll": b"interop",
        "BepInEx/core/Mono.Cecil.dll": b"cecil",
        "BepInEx/core/Mono.Cecil.Mdb.dll": b"mdb",
        "BepInEx/core/Mono.Cecil.Pdb.dll": b"pdb",
        "BepInEx/core/Mono.Cecil.Rocks.dll": b"rocks",
        "BepInEx/core/MonoMod.RuntimeDetour.dll": b"detour",
        "BepInEx/core/MonoMod.Utils.dll": b"utils",
        "BepInEx/patchers/BepInEx.MultiFolderLoader.dll": b"folder loader",
        "doorstop_libs/libdoorstop_x64.so": b"doorstop",
        "doorstop_config.ini": b"[MultiFolderLoader]\nbaseDir=Mods/\n",
        "run_bepinex.sh": b'#!/bin/sh\nprintf "%s\\n" "$@"\n',
        "README.md": b"overhaul instructions",
    }


@pytest.fixture
def app_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "app"
    directory.mkdir()
    (directory / "serverconfig.xml").write_text(
        '<ServerSettings><property name="EACEnabled" value="true"/></ServerSettings>', encoding="utf-8",
    )
    mono = directory / "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config"
    mono.parent.mkdir(parents=True)
    mono.write_text('<configuration><dllmap dll="libc" target="libc.so.6"/></configuration>', encoding="utf-8")
    for relative in installation._REQUIRED_GAME_FILES:
        pointer = directory / relative
        if not pointer.exists():
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_bytes(b"game runtime")
    (directory / "README.md").write_text("game instructions", encoding="utf-8")
    rwgmixer = directory / "Data/Config/rwgmixer.xml"
    rwgmixer.parent.mkdir(parents=True)
    traders = {"rekt": "forest", "jen": "burntforest", "bob": "desert", "hugh": "snow", "joel": "wasteland"}
    rwgmixer.write_text(
        "<rwgmixer>" + "".join(f'<prefab_spawn_adjust partial_name="trader_{name}" biomeTags="{biome}"/>' for name, biome in traders.items()) + "</rwgmixer>",
        encoding="utf-8",
    )
    return directory


def _write_zip(path: Path, files: dict[str, bytes], *, prefix: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in files.items():
            archive.writestr(prefix + name, contents)
    return path


def test_full_install_creates_portable_profile(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
) -> None:
    archive = _write_zip(tmp_path / "overhaul.zip", package_files)
    installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())

    manifest = installation.load_undead_legacy_installation(app_directory)
    assert manifest is not None
    assert manifest.version == "2.7.40"
    assert manifest.mod_names == ("UndeadLegacy",)
    assert str(tmp_path) not in manifest.model_dump_json()
    settings = ET.parse(app_directory / "serverconfig.xml").getroot()
    assert {node.get("name"): node.get("value") for node in settings} == {
        "EACEnabled": "false", "UserDataFolder": "userdata",
    }
    mono = ET.parse(app_directory / "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config").getroot()
    assert not (app_directory / "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config").read_bytes().startswith(b"<?xml")
    assert any(node.attrib == {"dll": "dl", "target": "libdl.so.2"} for node in mono)
    assert {node.get("dll"): node.get("target") for node in mono if node.get("dll") in {"System.Native", "System.Net.Security.Native"}} == {
        "System.Native": "libmono-native.so", "System.Net.Security.Native": "libmono-native.so",
    }
    assert (app_directory / "README.md").read_text() == "game instructions"
    assert (app_directory / ".yukibot/undead-legacy-docs/README.md").read_bytes() == package_files["README.md"]
    assert (app_directory / "userdata").is_dir()


def test_compressed_prefabs_can_exceed_default_upload_ratio(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
) -> None:
    prefab_path = "Mods/UndeadLegacy/Prefabs/POIs/barn_01.tts"
    prefab = bytes(range(256)) * 512
    archive_path = _write_zip(tmp_path / "overhaul.zip", package_files)
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefab_path, prefab)
        member = archive.getinfo(prefab_path)
        assert 100 < member.file_size / member.compress_size < 256

    with pytest.raises(ValueError, match="compression ratio"):
        validated_zip_entries(archive_path, archive_label="Upload", limits=ZipArchiveLimits())
    installation._prepare_overhaul_files(app_directory, (archive_path,), _RELEASE, Event())
    assert (app_directory / prefab_path).read_bytes() == prefab
    assert installation.load_undead_legacy_installation(app_directory) is not None


def test_overhaul_still_rejects_excessive_prefab_compression(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
) -> None:
    archive_path = _write_zip(tmp_path / "overhaul.zip", package_files)
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Mods/UndeadLegacy/Prefabs/POIs/excessive.tts", b"\0" * 256_000)
    with pytest.raises(ValueError, match="compression ratio"):
        installation._prepare_overhaul_files(app_directory, (archive_path,), _RELEASE, Event())
    assert installation.load_undead_legacy_installation(app_directory) is None


def test_split_downloads_accept_wrappers_and_asset_only_second_part(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
) -> None:
    assets = {name: value for name, value in package_files.items() if name.endswith(".ulm")}
    code = {name: value for name, value in package_files.items() if name not in assets}
    archives = (
        _write_zip(tmp_path / "code.zip", code, prefix="Part1-commit/"),
        _write_zip(tmp_path / "assets.zip", assets, prefix="Part2-other-commit/"),
    )
    installation._prepare_overhaul_files(app_directory, archives, _RELEASE, Event())
    assert installation.load_undead_legacy_installation(app_directory) is not None


def test_official_install_downloads_both_parts_before_preparing(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = {name: value for name, value in package_files.items() if name.endswith(".ulm")}
    code = {name: value for name, value in package_files.items() if name not in assets}
    archives = iter((_write_zip(tmp_path / "code.zip", code), _write_zip(tmp_path / "assets.zip", assets)))
    requested: list[str] = []

    async def download(url: str, cache_directory: Path, report: object) -> Path:
        del cache_directory, report
        requested.append(url)
        return next(archives)

    monkeypatch.setattr(installation, "_download_archive", download)
    asyncio.run(installation.prepare_undead_legacy_install(
        app_directory, cache_directory=tmp_path / "cache", archive_source=None, report=lambda _: None, release=_RELEASE,
    ))
    assert tuple(requested) == _RELEASE.archive_urls
    assert installation.load_undead_legacy_installation(app_directory) is not None


@pytest.mark.parametrize("failure", ["missing_asset", "missing_loader", "wrong_release", "lfs_pointer"])
def test_invalid_package_never_marks_install_complete(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes], failure: str,
) -> None:
    if failure == "missing_asset":
        del package_files["Mods/UndeadLegacy/Resources/Subquake_Items.ulm"]
    elif failure == "missing_loader":
        del package_files["BepInEx/core/BepInEx.dll"]
    elif failure == "wrong_release":
        package_files["Mods/UndeadLegacy/UndeadLegacy.dll"] = b"unsupported release"
    else:
        package_files["Mods/UndeadLegacy/Resources/Subquake_Items.ulm"] = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n"
    archive = _write_zip(tmp_path / "bad.zip", package_files)
    with pytest.raises(ValueError):
        installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    assert installation.load_undead_legacy_installation(app_directory) is None


@pytest.mark.parametrize("member", ["..\\outside.txt", "C:\\outside.txt", "7DaysToDieServer.x86_64", "Mods/undeadlegacy/alias.txt"])
def test_unsafe_or_conflicting_members_are_rejected(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes], member: str,
) -> None:
    package_files[member] = b"unsafe"
    archive = _write_zip(tmp_path / "bad.zip", package_files)
    with pytest.raises(ValueError):
        installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("relative", [
    "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config",
    "7DaysToDieServer_Data/MonoBleedingEdge/x86_64/libmono-native.so",
    "7DaysToDieServer.x86_64",
])
@pytest.mark.parametrize("empty", [False, True])
def test_incomplete_game_runtime_never_marks_install_complete(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes], relative: str, empty: bool,
) -> None:
    pointer = app_directory / relative
    if empty:
        pointer.write_bytes(b"")
    else:
        pointer.unlink()
    archive = _write_zip(tmp_path / "overhaul.zip", package_files)
    with pytest.raises(ValueError, match="game runtime is incomplete"):
        installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    assert installation.load_undead_legacy_installation(app_directory) is None
    assert not (app_directory / installation.UNDEAD_LEGACY_LAUNCHER).exists()


def test_archive_symlinks_are_rejected(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
) -> None:
    archive = _write_zip(tmp_path / "symlink.zip", package_files)
    with zipfile.ZipFile(archive, "a") as output:
        member = zipfile.ZipInfo("Mods/UndeadLegacy/link")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(member, "../../outside")
    with pytest.raises(ValueError, match="symbolic links"):
        installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())


@pytest.mark.parametrize("existing_library_path", (None, "/existing library path"))
def test_portable_launcher_preserves_arguments_after_relocation(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
    existing_library_path: str | None,
) -> None:
    package_files["run_bepinex.sh"] = (
        b'#!/bin/sh\nprintf "%s\\n" "$MONO_CONFIG" "$MONO_CFG_DIR" "$LD_LIBRARY_PATH" >&2\nprintf "%s\\n" "$@"\n'
    )
    archive = _write_zip(tmp_path / "overhaul.zip", package_files)
    installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    relocated = tmp_path / "moved server with spaces"
    app_directory.rename(relocated)
    environment = os.environ.copy()
    environment["MONO_CONFIG"] = "/another-install/mono/config"
    if existing_library_path is None:
        environment.pop("LD_LIBRARY_PATH", None)
    else:
        environment["LD_LIBRARY_PATH"] = existing_library_path
    result = subprocess.run(
        [str(relocated / installation.UNDEAD_LEGACY_LAUNCHER), "-ServerName=Example With Spaces"],
        cwd=tmp_path, check=True, capture_output=True, text=True, env=environment,
    )
    arguments = result.stdout.splitlines()
    assert arguments[0] == str(relocated / "7DaysToDieServer.x86_64")
    assert f"-configfile={relocated}/serverconfig.xml" in arguments
    assert f"-UserDataFolder={relocated}/userdata" in arguments
    assert "-noeac" in arguments
    assert arguments[-1] == "-ServerName=Example With Spaces"
    mono = relocated / "7DaysToDieServer_Data/MonoBleedingEdge"
    assert result.stderr.splitlines() == [
        str(mono / "etc/mono/config"), str(mono / "etc"),
        str(mono / "x86_64") + (f":{existing_library_path}" if existing_library_path else ""),
    ]


def test_runtime_configuration_repairs_default_native_mappings_idempotently(app_directory: Path) -> None:
    mono_config = app_directory / "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config"
    mono_config.write_text(
        '<configuration><dllmap dll="System.Native" target="$mono_libdir/libmono-native.so" os="!windows"/>'
        '<dllmap dll="System.Net.Security.Native" target="$mono_libdir/libmono-native.so" os="!windows"/></configuration>',
        encoding="utf-8",
    )
    installation._configure_undead_legacy_runtime(app_directory)
    first = mono_config.read_bytes()
    assert not first.startswith(b"<?xml")
    installation._configure_undead_legacy_runtime(app_directory)
    assert mono_config.read_bytes() == first
    root = ET.parse(mono_config).getroot()
    assert {node.get("dll"): node.get("target") for node in root} == {
        "System.Native": "libmono-native.so", "System.Net.Security.Native": "libmono-native.so", "dl": "libdl.so.2",
    }


def test_expiry_removes_only_owned_downloads_at_72_hours(tmp_path: Path) -> None:
    now = time.time()
    expired = tmp_path / ("a" * 64 + ".zip")
    partial = tmp_path / ("b" * 64 + "-interrupted.part")
    recent = tmp_path / ("c" * 64 + ".zip")
    unrelated = tmp_path / "unrelated.zip"
    for path in (expired, partial, recent, unrelated):
        path.write_bytes(b"cache")
        os.utime(path, (now - 72 * 60 * 60, now - 72 * 60 * 60))
    os.utime(recent, (now - 72 * 60 * 60 + 1, now - 72 * 60 * 60 + 1))
    link = tmp_path / ("d" * 64 + ".zip")
    link.symlink_to(unrelated)

    installation.cleanup_install_downloads(tmp_path, now=now)
    assert not expired.exists()
    assert not partial.exists()
    assert recent.exists() and unrelated.exists() and link.is_symlink()


def test_cached_download_is_reused_and_retention_renewed(tmp_path: Path) -> None:
    url = "https://example.com/pinned.zip"
    archive = _write_zip(tmp_path / (hashlib.sha256(url.encode()).hexdigest() + ".zip"), {"file": b"cached"})
    os.utime(archive, (1, 1))
    messages: list[str] = []
    result = asyncio.run(installation._download_archive(url, tmp_path, messages.append))
    assert result == archive
    assert archive.stat().st_mtime > 1
    assert messages == ["Reusing a cached Undead Legacy archive."]


@pytest.mark.parametrize("outcome", ["complete", "interrupted", "private_redirect", "invalid_zip"])
def test_streamed_download_publishes_only_complete_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["complete", "interrupted", "private_redirect", "invalid_zip"],
) -> None:
    payload = _write_zip(tmp_path / "source.zip", {"asset": b"downloaded"}).read_bytes()
    if outcome == "invalid_zip":
        payload = b"not a ZIP archive"

    async def chunks(size: int) -> AsyncIterator[bytes]:
        del size
        yield payload[:10]
        if outcome == "interrupted":
            raise RuntimeError("connection interrupted")
        yield payload[10:]

    response = Mock(status=200, content_length=len(payload))
    response.content.iter_chunked = chunks
    if outcome == "private_redirect":
        response.status = 302
        response.headers = {"Location": "http://127.0.0.1/private.zip"}
    session = MagicMock()
    session.__aenter__.return_value = session
    session.get.return_value.__aenter__.return_value = response
    monkeypatch.setattr(installation.aiohttp, "ClientSession", Mock(return_value=session))
    monkeypatch.setattr(installation.aiohttp, "TCPConnector", Mock())
    cache = tmp_path / "cache"
    if outcome == "complete":
        downloaded = asyncio.run(installation._download_archive("https://example.com/pinned.zip", cache, lambda _: None))
        assert downloaded.read_bytes() == payload
        assert list(cache.iterdir()) == [downloaded]
    else:
        with pytest.raises(RuntimeError if outcome == "interrupted" else ValueError):
            asyncio.run(installation._download_archive("https://example.com/pinned.zip", cache, lambda _: None))
        assert list(cache.iterdir()) == []


def test_cancellation_waits_for_extraction_to_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, stopped = Event(), Event()

    def extract(directory: Path, archives: tuple[Path, ...], release: installation.UndeadLegacyRelease, cancelled: Event) -> None:
        del directory, archives, release
        started.set()
        assert cancelled.wait(timeout=5)
        stopped.set()
        raise InterruptedError("cancelled")

    monkeypatch.setattr(installation, "_prepare_overhaul_files", extract)

    async def run() -> None:
        task = asyncio.create_task(installation.prepare_undead_legacy_install(
            tmp_path, cache_directory=tmp_path / "cache", archive_source=tmp_path / "source.zip", report=lambda _: None,
        ))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(run())


def test_repeated_cancellation_cannot_release_a_running_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, cancellation_seen, release_worker, stopped = Event(), Event(), Event(), Event()

    def extract(directory: Path, archives: tuple[Path, ...], release: installation.UndeadLegacyRelease, cancelled: Event) -> None:
        del directory, archives, release
        started.set()
        assert cancelled.wait(timeout=5)
        cancellation_seen.set()
        assert release_worker.wait(timeout=5)
        stopped.set()
        raise InterruptedError("cancelled")

    monkeypatch.setattr(installation, "_prepare_overhaul_files", extract)

    async def wait_for(event: Event) -> None:
        async with asyncio.timeout(3):
            while not event.is_set():
                await asyncio.sleep(0.005)

    async def run() -> None:
        task = asyncio.create_task(installation.prepare_undead_legacy_install(
            tmp_path, cache_directory=tmp_path / "cache", archive_source=tmp_path / "source.zip", report=lambda _: None,
        ))
        try:
            await wait_for(started)
            task.cancel()
            await wait_for(cancellation_seen)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        finally:
            release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(run())


def test_managed_components_are_protected_and_saves_use_overhaul_folder(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes],
) -> None:
    archive = _write_zip(tmp_path / "overhaul.zip", package_files)
    installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    mod = Mod_7D2D(Mod_Config(name="UndeadLegacy", directory=app_directory / "Mods"))
    assert mod.cfg.mod_type is ModType.BUILTIN
    assert not mod.downloadable
    assert mod.detect_version() == _RELEASE.version
    mod.cfg.classification_override = ModClassificationOverride(mod_type=ModType.CLIENT)
    mod.sync_metadata()
    assert mod.mod_type is ModType.BUILTIN
    assert mod.download_block_reason is ModDownloadBlockReason.BUILTIN
    assert mod.cfg.placement is ModPlacement.SERVER_ENABLED
    assert not mod.downloadable
    with pytest.raises(ValueError, match="managed by the instance"):
        mod.move_to_placement(ModPlacement.SERVER_DISABLED)
    for action in (mod.uninstall, mod._disable_file, mod._enable_file):
        with pytest.raises(ValueError, match="managed by the instance"):
            asyncio.run(action(override_coremod=True))
    with pytest.raises(ValueError, match="managed by the instance"):
        asyncio.run(mod.install(archive))
    assert (app_directory / "Mods/UndeadLegacy/ModInfo.xml").exists()
    assert SevenDays_Settings(app_directory / "serverconfig.xml")._settings_userdata_root_path() == app_directory / "userdata/UndeadLegacy"
    assert installation.sevendays_content_userdata_root(directory=tmp_path, userdata_root=tmp_path / "userdata") == tmp_path / "userdata"


def test_loaded_overhaul_uses_its_launcher_and_disables_steam_updater(
    tmp_path: Path, app_directory: Path, package_files: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_zip(tmp_path / "overhaul.zip", package_files)
    installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    monkeypatch.setattr(config, "DIR_LOG", tmp_path / "logs")
    app_config = App_Config(
        name="sevendays_alpha", instance_key="alpha", directory=app_directory, apps_dir=tmp_path / "apps",
        mods_dir=app_directory / "Mods", scope="sevendays", join_host="127.0.0.1",
        steam_update=SteamUpdateConfig(app_id=294420),
    )
    updater = Mock()
    monkeypatch.setattr("apps.sevendays.SteamCmd_Update_Manager", updater)
    app = SevenDays(Mock(), Mock(), app_config)
    assert app.cmd_start == ["sh", installation.UNDEAD_LEGACY_LAUNCHER]
    assert app.updater is None
    assert not app.steam_update_supported
    manager = object.__new__(App_Manager)
    manager._sync_app_steam_updater(app=app, previous_steam_update=None, next_steam_update=app_config.steam_update)
    assert app.updater is None
    details = AppDetailsUpdate(
        friendly_name="UL", notes=None, lifecycle_notice_started=True, lifecycle_notice_stopped=True,
        lifecycle_notice_crashed=True, running_cpu_points=1, running_ram_points=1,
        startup_cpu_points=None, startup_ram_points=None,
    )
    assert manager._resolve_next_steam_update_config(app=app, details=details) == app_config.steam_update
    with pytest.raises(ValueError, match="unavailable for this installation profile"):
        manager._resolve_next_steam_update_config(app=app, details=replace(details, steam_update_enabled=True))
    updater.assert_not_called()
    assert app._userdata_root_path() == app_directory / "userdata/UndeadLegacy"


@pytest.mark.parametrize("source", ["relative.zip", "/missing/archive.zip"])
def test_archive_source_requires_existing_absolute_zip(source: str) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        installation.validate_overhaul_archive_source(source)


@pytest.mark.parametrize("names", [("Other",), ("UndeadLegacy", "undeadlegacy"), ("UndeadLegacy", "nested/component"), ("UndeadLegacy", "..")])
def test_invalid_profile_component_names_are_rejected(names: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        installation.UndeadLegacyInstallation(
            version=_RELEASE.version, game_version=_RELEASE.game_version,
            core_sha256=_RELEASE.core_sha256, mod_names=names,
        )


@pytest.mark.parametrize("path", [".", "../outside", "BepInEx/../../outside", "unmanaged.txt"])
def test_managed_file_paths_fail_with_validation_errors(path: str) -> None:
    with pytest.raises(ValueError):
        installation.UndeadLegacyManagedFile(path=path, size_bytes=1, sha256="0" * 64)


@pytest.fixture
def installed_overhaul_archive(tmp_path: Path, app_directory: Path, package_files: dict[str, bytes]) -> Path:
    archive = _write_zip(tmp_path / "installed-overhaul.zip", package_files)
    installation._prepare_overhaul_files(app_directory, (archive,), _RELEASE, Event())
    return archive


@pytest.fixture
def overhaul_app(
    installed_overhaul_archive: Path,
    app_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SevenDays:
    monkeypatch.setattr(config, "DIR_LOG", tmp_path / "logs")
    cfg = App_Config(
        name="sevendays_ul_review",
        instance_key="ul_review",
        scope="sevendays",
        directory=app_directory,
        apps_dir=tmp_path / "apps",
        mods_dir=app_directory / "Mods",
        version=AppVersion(main="2.6"),
    )
    return SevenDays(Mock(), Mock(), cfg)


def test_health_checks_fingerprints_assets_loader_and_game_version(
    app_directory: Path,
    installed_overhaul_archive: Path,
) -> None:
    version = AppVersion(main="2.6")
    assert not installation.inspect_undead_legacy_installation(app_directory, game_version=version, deep=True)
    asset = app_directory / "Mods/UndeadLegacy/Resources/Subquake_Items.ulm"
    asset.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    loader = app_directory / "BepInEx/core/BepInEx.dll"
    loader.write_bytes(b"x" * loader.stat().st_size)
    issues = installation.inspect_undead_legacy_installation(app_directory, game_version=version, deep=True)
    assert any("Git LFS" in issue.message and issue.repairable for issue in issues)
    assert any("BepInEx.dll" in issue.message and issue.repairable for issue in issues)
    issues = installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="3.4"))
    assert any("requires 2.6" in issue.message and not issue.repairable for issue in issues)


def test_health_uses_the_installed_steam_branch_instead_of_trusting_old_logs(
    app_directory: Path,
    installed_overhaul_archive: Path,
) -> None:
    manifest = app_directory / "steamapps/appmanifest_294420.acf"
    manifest.parent.mkdir()
    manifest.write_text('"AppState" { "UserConfig" { "betakey" "public" } }')
    issues = installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"))
    assert any("branch is 'public'" in issue.message and not issue.repairable for issue in issues)
    manifest.write_text('"AppState" { "UserConfig" { "betakey" "v2.6" } }')
    assert not installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"))
    assert not installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(steam_build=123))
    manifest.write_text('"AppState" { "UserConfig" { "BetaKey" "public" } "MountedConfig" { "BetaKey" "v2.6" } }')
    assert not installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"))
    manifest.write_text('"AppState" { "UserConfig" { "BetaKey" "v2.6" } "MountedConfig" { "BetaKey" "public" } }')
    assert installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"))


def test_repair_restores_only_managed_files_and_keeps_worlds_and_gameplay_configuration(
    app_directory: Path,
    installed_overhaul_archive: Path,
    package_files: dict[str, bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = app_directory / "Mods/UndeadLegacy/Resources/Subquake_Items.ulm"
    asset.write_bytes(b"broken")
    preserved = {
        "Mods/UndeadLegacy/Config/custom.xml": b"custom UL configuration",
        "Mods/MyAddon/ModInfo.xml": b"add-on",
        "userdata/UndeadLegacy/Saves/World/Save/main.ttw": b"world progress",
    }
    for relative, content in preserved.items():
        pointer = app_directory / relative
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_bytes(content)
    config_path = app_directory / "serverconfig.xml"
    tree = ET.parse(config_path)
    ET.SubElement(tree.getroot(), "property", {"name": "ActionSkillXPMultiplier", "value": "175"})
    tree.write(config_path)
    (app_directory / installation.UNDEAD_LEGACY_LAUNCHER).unlink()
    download = AsyncMock(return_value=installed_overhaul_archive)
    monkeypatch.setattr(installation, "_download_archive", download)
    asyncio.run(
        installation.repair_undead_legacy_installation(
            app_directory,
            game_version=AppVersion(main="2.6"),
            cache_directory=tmp_path / "cache",
            report=lambda _: None,
            release=_RELEASE,
        )
    )
    assert download.await_count == len(_RELEASE.archive_urls)
    assert asset.read_bytes() == package_files["Mods/UndeadLegacy/Resources/Subquake_Items.ulm"]
    for relative, content in preserved.items():
        assert (app_directory / relative).read_bytes() == content
    assert (
        next(
            node.get("value")
            for node in ET.parse(config_path).getroot()
            if node.get("name") == "ActionSkillXPMultiplier"
        )
        == "175"
    )
    assert not installation.inspect_undead_legacy_installation(
        app_directory, game_version=AppVersion(main="2.6"), deep=True
    )


def test_repair_does_not_download_for_healthy_or_incompatible_installations(
    app_directory: Path,
    installed_overhaul_archive: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download = AsyncMock()
    monkeypatch.setattr(installation, "_download_archive", download)
    arguments = dict(cache_directory=tmp_path / "cache", report=lambda _: None, release=_RELEASE)
    asyncio.run(
        installation.repair_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"), **arguments)
    )
    with pytest.raises(ValueError, match="requires attention"):
        asyncio.run(
            installation.repair_undead_legacy_installation(
                app_directory, game_version=AppVersion(main="3.4"), **arguments
            )
        )
    with pytest.raises(ValueError, match="different version"):
        asyncio.run(
            installation.repair_undead_legacy_installation(
                app_directory,
                game_version=AppVersion(main="2.6"),
                cache_directory=tmp_path / "cache",
                report=lambda _: None,
                release=replace(_RELEASE, version="2.8"),
            )
        )
    download.assert_not_awaited()


def test_bad_repair_archive_leaves_the_existing_installation_untouched(
    app_directory: Path,
    installed_overhaul_archive: Path,
    package_files: dict[str, bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = app_directory / "Mods/UndeadLegacy/Resources/Subquake_Items.ulm"
    asset.write_bytes(b"broken")
    before = (app_directory / installation.UNDEAD_LEGACY_MANIFEST).read_bytes()
    files = {**package_files, "Mods/UndeadLegacy/UndeadLegacy.dll": b"wrong release"}
    archive = _write_zip(tmp_path / "bad-repair.zip", files)
    monkeypatch.setattr(installation, "_download_archive", AsyncMock(return_value=archive))
    with pytest.raises(ValueError, match="supported Undead Legacy"):
        asyncio.run(
            installation.repair_undead_legacy_installation(
                app_directory,
                game_version=AppVersion(main="2.6"),
                cache_directory=tmp_path / "cache",
                report=lambda _: None,
                release=_RELEASE,
            )
        )
    assert asset.read_bytes() == b"broken"
    assert (app_directory / installation.UNDEAD_LEGACY_MANIFEST).read_bytes() == before


def test_runtime_configuration_repair_does_not_download_archives(
    app_directory: Path, installed_overhaul_archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (app_directory / installation.UNDEAD_LEGACY_LAUNCHER).write_bytes(b"\xffcorrupt launcher")
    mono = app_directory / "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config"
    tree = ET.parse(mono)
    for node in tree.getroot().findall("dllmap"):
        if node.get("dll") == "dl":
            node.set("target", "wrong-library")
    tree.write(mono, xml_declaration=True, encoding="utf-8")
    serverconfig = app_directory / "serverconfig.xml"
    tree = ET.parse(serverconfig)
    ET.SubElement(tree.getroot(), "property", {"name": "EACEnabled", "value": "true"})
    tree.write(serverconfig)
    before = (app_directory / installation.UNDEAD_LEGACY_MANIFEST).read_bytes()
    download = AsyncMock()
    monkeypatch.setattr(installation, "_download_archive", download)
    asyncio.run(installation.repair_undead_legacy_installation(
        app_directory, game_version=AppVersion(main="2.6"), cache_directory=tmp_path / "cache", report=lambda _: None,
        release=_RELEASE,
    ))
    download.assert_not_awaited()
    assert (app_directory / installation.UNDEAD_LEGACY_MANIFEST).read_bytes() == before
    assert not installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"))


@pytest.mark.parametrize("relative", [
    installation.UNDEAD_LEGACY_LAUNCHER,
    "serverconfig.xml",
    "7DaysToDieServer_Data/MonoBleedingEdge/etc/mono/config",
])
def test_repair_refuses_paths_outside_the_installation(
    app_directory: Path, installed_overhaul_archive: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    pointer = app_directory / relative
    outside = tmp_path / "outside"
    original = pointer.read_bytes()
    outside.write_bytes(original)
    pointer.unlink()
    pointer.symlink_to(outside)
    download = AsyncMock()
    monkeypatch.setattr(installation, "_download_archive", download)
    issues = installation.inspect_undead_legacy_installation(app_directory, game_version=AppVersion(main="2.6"))
    assert any("escapes the installation" in issue.message and not issue.repairable for issue in issues)
    with pytest.raises(ValueError, match="requires attention"):
        asyncio.run(installation.repair_undead_legacy_installation(
            app_directory, game_version=AppVersion(main="2.6"), cache_directory=tmp_path / "cache",
            report=lambda _: None, release=_RELEASE,
        ))
    assert outside.read_bytes() == original
    download.assert_not_awaited()


def test_configuration_repair_cancellation_waits_for_the_worker_before_unlocking(
    overhaul_app: SevenDays, app_directory: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (app_directory / installation.UNDEAD_LEGACY_LAUNCHER).unlink()
    started = Event()
    release_worker = Event()
    stopped = Event()

    def repair_runtime(directory: Path) -> None:
        started.set()
        try:
            assert release_worker.wait(3)
        finally:
            stopped.set()

    async def repair(directory: Path, **kwargs: object) -> None:
        await installation.repair_undead_legacy_installation(
            directory, game_version=overhaul_app.cfg.version, cache_directory=app_directory / "cache",
            report=lambda _: None, release=_RELEASE,
        )

    monkeypatch.setattr(installation, "_repair_runtime_configuration", repair_runtime)
    monkeypatch.setattr("apps.sevendays.repair_undead_legacy_installation", repair)
    monkeypatch.setattr(overhaul_app, "check_running", lambda: False)
    action = next(action for action in overhaul_app.console_actions if action.key == "repair_undead_legacy")

    async def exercise() -> None:
        task = asyncio.create_task(action.execute(overhaul_app, None))
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done()
            assert overhaul_app._maintenance_lock.locked()
            with pytest.raises(RuntimeError, match="maintenance is already"):
                await overhaul_app.start()
            settings = overhaul_app.settings
            assert settings is not None
            serverconfig = app_directory / "serverconfig.xml"
            before = serverconfig.read_bytes()
            with pytest.raises(RuntimeError, match="maintenance is already"):
                settings.save(42)
            assert serverconfig.read_bytes() == before
        finally:
            release_worker.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set()
        assert not overhaul_app._maintenance_lock.locked()

    asyncio.run(exercise())


def test_repair_does_not_overwrite_configuration_through_a_launcher_symlink(
    app_directory: Path, installed_overhaul_archive: Path, tmp_path: Path,
) -> None:
    launcher = app_directory / installation.UNDEAD_LEGACY_LAUNCHER
    launcher.unlink()
    launcher.symlink_to("serverconfig.xml")
    serverconfig = app_directory / "serverconfig.xml"
    before = serverconfig.read_bytes()
    with pytest.raises(ValueError, match="must not be a symbolic link"):
        asyncio.run(installation.repair_undead_legacy_installation(
            app_directory, game_version=AppVersion(main="2.6"), cache_directory=tmp_path / "cache",
            report=lambda _: None, release=_RELEASE,
        ))
    assert serverconfig.read_bytes() == before


def test_version_display_preserves_the_base_version_for_feature_gates(
    overhaul_app: SevenDays,
) -> None:
    assert overhaul_app.version_display == "UL 2.7.40 · Game 2.6"
    assert overhaul_app.cfg.version == AppVersion(main="2.6")
    assert {action.key for action in overhaul_app.console_actions} >= {
        "check_undead_legacy",
        "repair_undead_legacy",
    }
    overhaul_app.cfg.version = None
    assert overhaul_app.version_display == "UL 2.7.40 · Game Unknown"


def test_preflight_rejects_a_broken_ul_installation_before_launch(
    overhaul_app: SevenDays,
    app_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (app_directory / "Mods/UndeadLegacy/UndeadLegacy.dll").unlink()
    launch = AsyncMock()
    monkeypatch.setattr(overhaul_app, "_std_launch", launch)
    with pytest.raises(RuntimeError, match="preflight failed"):
        asyncio.run(overhaul_app.start())
    launch.assert_not_awaited()


def test_repair_refuses_running_servers_and_prevents_concurrent_launch(
    overhaul_app: SevenDays,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = next(action for action in overhaul_app.console_actions if action.key == "repair_undead_legacy")
    monkeypatch.setattr(overhaul_app, "check_running", lambda: True)
    with pytest.raises(RuntimeError, match="Stop the server"):
        asyncio.run(action.execute(overhaul_app, None))
    monkeypatch.setattr(overhaul_app, "check_running", lambda: False)

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def repair(*args: object, **kwargs: object) -> None:
            started.set()
            await release.wait()

        monkeypatch.setattr("apps.sevendays.repair_undead_legacy_installation", repair)
        task = asyncio.create_task(action.execute(overhaul_app, None))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            with pytest.raises(RuntimeError, match="maintenance is already"):
                await overhaul_app.start()
        finally:
            release.set()
            await task
        assert not overhaul_app._maintenance_lock.locked()

    asyncio.run(exercise())


@pytest.fixture
def client_release(package_files: dict[str, bytes]) -> installation.UndeadLegacyRelease:
    package_files["winhttp.dll"] = b"windows doorstop"
    files: list[installation.UndeadLegacyManagedFile] = []
    for file in _RELEASE.windows_runtime_files:
        if Path(file.path).suffix in {".cfg", ".ini"}:
            package_files[file.path] = (client_pack._RUNTIME_DEFAULTS / file.path).read_bytes()
        content = package_files[file.path]
        files.append(file.model_copy(update={"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}))
    return replace(_RELEASE, windows_runtime_files=tuple(files))


@pytest.fixture
def client_pack_app(
    client_release: installation.UndeadLegacyRelease,
    overhaul_app: SevenDays,
    monkeypatch: pytest.MonkeyPatch,
) -> SevenDays:
    mods_directory = overhaul_app.directory / "Mods"
    mods = {"UndeadLegacy": Mod_7D2D(Mod_Config(name="UndeadLegacy", directory=mods_directory))}
    for name, placement in (("add-on", ModPlacement.SERVER_ENABLED), ("client-addon", ModPlacement.CLIENT_ONLY)):
        folder = mods_directory / name
        folder.mkdir()
        filename = "ModInfo.xml.client" if placement is ModPlacement.CLIENT_ONLY else "ModInfo.xml"
        (folder / filename).write_text(f'<xml><Name value="{name}"/></xml>', encoding="utf-8")
        (folder / "addon.dll").write_bytes(b"add-on code")
        mods[name] = Mod_7D2D(Mod_Config(name=name, directory=mods_directory, placement=placement))
    manager = Mock()
    manager.list_mods.return_value = tuple(mods.values())
    manager.get.side_effect = mods.__getitem__
    manager.reload_mods = AsyncMock()
    overhaul_app.mods = manager
    overrides = overhaul_app.directory / "client-overrides"
    (overrides / "Config").mkdir(parents=True)
    (overrides / "Config/client.ini").write_text("client options", encoding="utf-8")
    overhaul_app.cfg.client_overrides_dir = overrides
    monkeypatch.setattr(
        node_client_pack,
        "build_undead_legacy_client_pack_entries",
        partial(client_pack.build_undead_legacy_client_pack_entries, release=client_release),
    )
    return overhaul_app


@pytest.fixture
def client_pack_service() -> NodeClientPackService:
    return NodeClientPackService(
        node_name=lambda: "test",
        require_acl=Mock(),
        http_exception=lambda status, detail: RuntimeError(f"{status}: {detail}"),
        invalidate_app_state=lambda _name: None,
        invalidate_mod_inventory=lambda _name: None,
    )


def test_ul_client_archive_is_a_complete_windows_game_root_overlay(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
    client_release: installation.UndeadLegacyRelease,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for relative in ("BepInEx/LogOutput.log", "BepInEx/cache/private.cache", "BepInEx/plugins/server.dll"):
        pointer = client_pack_app.directory / relative
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_bytes(b"server-only generated data")
    (client_pack_app.directory / "BepInEx/config/BepInEx.cfg").write_bytes(b"custom server settings")
    (client_pack_app.directory / "doorstop_config.ini").write_bytes(b"custom server loader settings")
    monkeypatch.setattr(config, "DIR_ZIPS", tmp_path / "zips")
    request = NodeDownloadRequest(
        client_pack=True, publish_client_pack=True, publish_changelog="Complete Windows and Proton client.",
    )
    response = asyncio.run(client_pack_service.build_mod_download_response(app=client_pack_app, request=request))
    with zipfile.ZipFile(response.path) as archive:
        files = {name for name in archive.namelist() if not name.endswith("/")}
        assert {
            "Mods/UndeadLegacy/UndeadLegacy.dll",
            "Mods/UndeadLegacy/Resources/Subquake_Items.ulm",
            "Mods/add-on/ModInfo.xml",
            "Mods/client-addon/ModInfo.xml",
            "Mods/client-addon/addon.dll",
            "Config/client.ini",
            "Start Undead Legacy.cmd",
            "README-Undead-Legacy.txt",
            *(file.path for file in client_release.windows_runtime_files),
        }.issubset(files)
        assert files == {
            str(path.relative_to(client_pack_app.directory))
            for path in (client_pack_app.directory / "Mods").rglob("*")
            if path.is_file() and path.name != "ModInfo.xml.client"
        } | {
            "Mods/client-addon/ModInfo.xml", "Config/client.ini",
            "Start Undead Legacy.cmd", "README-Undead-Legacy.txt",
            *(file.path for file in client_release.windows_runtime_files),
        }
        assert archive.read("BepInEx/config/BepInEx.cfg") == (client_pack._RUNTIME_DEFAULTS / "BepInEx/config/BepInEx.cfg").read_bytes()
        assert archive.read("doorstop_config.ini") == (client_pack._RUNTIME_DEFAULTS / "doorstop_config.ini").read_bytes()
        guide = archive.read("README-Undead-Legacy.txt").decode()
        assert 'WINEDLLOVERRIDES="winhttp.dll=n,b" %command%' in guide
        assert client_release.version in guide and client_release.steam_branch in guide
    core = client_pack_app.has_mod_manager.get("UndeadLegacy")
    assert not core.downloadable and core.is_protected
    assert not core.cfg.client_pack.included_in_client
    assert "UndeadLegacy" in {mod.name for mod in client_pack_app.cfg.client_pack_published_mods}


@pytest.mark.parametrize("relative,damage", [
    ("winhttp.dll", "missing"),
    ("BepInEx/core/BepInEx.dll", "modified"),
    ("Mods/UndeadLegacy/UndeadLegacy.dll", "modified"),
    ("Mods/UndeadLegacy/Resources/Subquake_Items.ulm", "lfs"),
    ("Mods/UndeadLegacy/Resources/Subquake_Power.ulm", "missing"),
])
def test_ul_client_export_rejects_missing_or_invalid_required_files(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
    relative: str,
    damage: Literal["missing", "lfs", "modified"],
) -> None:
    target = client_pack_app.directory / relative
    if damage == "missing":
        target.unlink()
    elif damage == "lfs":
        target.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    else:
        target.write_bytes(b"x" * target.stat().st_size)
    with pytest.raises(ClientPackValidationError):
        client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)


def test_ul_client_export_rejects_loader_symlinks(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
    tmp_path: Path,
) -> None:
    target = client_pack_app.directory / "winhttp.dll"
    outside = tmp_path / "outside-loader.dll"
    target.rename(outside)
    target.symlink_to(outside)
    with pytest.raises(ClientPackValidationError, match="escapes|symbolic link"):
        client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)


def test_ul_client_overrides_cannot_replace_managed_runtime_files(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
) -> None:
    overrides = client_pack_app.cfg.client_overrides_dir
    assert overrides is not None
    (overrides / "WINHTTP.DLL").write_bytes(b"conflicting override")
    with pytest.raises(ClientPackValidationError, match="[Dd]uplicate"):
        entries = client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)
        asyncio.run(client_pack_service.content_hash(app=client_pack_app, entries=entries))


def test_ul_client_asset_digest_is_checked_during_publication_hashing(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
) -> None:
    target = client_pack_app.directory / "Mods/UndeadLegacy/Resources/Subquake_Items.ulm"
    target.write_bytes(b"x" * target.stat().st_size)
    entries = client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)
    with pytest.raises(ClientPackValidationError, match="Subquake_Items.ulm"):
        asyncio.run(client_pack_service.content_hash(app=client_pack_app, entries=entries))


def test_ul_client_export_requires_the_known_managed_manifest(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
) -> None:
    (client_pack_app.directory / installation.UNDEAD_LEGACY_MANIFEST).unlink()
    with pytest.raises(ClientPackValidationError, match="managed Undead Legacy"):
        client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)


@pytest.mark.parametrize("relative", ["BepInEx/core", "bepinex/custom.ini", "CON.txt", "winhttp.dll.", "bad:stream"])
def test_ul_client_export_rejects_windows_path_conflicts(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
    relative: str,
) -> None:
    overrides = client_pack_app.cfg.client_overrides_dir
    assert overrides is not None
    target = overrides / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"invalid Windows overlay")
    with pytest.raises(ClientPackValidationError):
        entries = client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)
        asyncio.run(client_pack_service.content_hash(app=client_pack_app, entries=entries))


def test_ul_client_changelog_does_not_read_runtime_binaries(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_hash(_path: Path) -> str:
        pytest.fail("Metadata previews must not hash client runtime files.")
    monkeypatch.setattr(client_pack, "_file_sha256", unexpected_hash)
    snapshots = client_pack_service.default_mod_snapshots(client_pack_app)
    assert {mod.name for mod in snapshots} == {"UndeadLegacy", "add-on", "client-addon"}


def test_ul_client_hash_tracks_runtime_and_generated_launch_support(
    client_pack_app: SevenDays,
    client_pack_service: NodeClientPackService,
) -> None:
    entries = client_pack_service.entries(ClientPackSelection(), app=client_pack_app, include_kubejs_scripts=False)
    original = asyncio.run(client_pack_service.content_hash(app=client_pack_app, entries=entries))
    for path in ("Start Undead Legacy.cmd", "README-Undead-Legacy.txt"):
        changed = tuple(
            replace(entry, content=entry.content + b"changed")
            if isinstance(entry, ArchiveDataEntry) and entry.archive_path == PurePosixPath(path) else entry
            for entry in entries
        )
        assert asyncio.run(client_pack_service.content_hash(app=client_pack_app, entries=changed)) != original
    loader = client_pack_app.directory / "winhttp.dll"
    loader.write_bytes(b"x" * loader.stat().st_size)
    assert asyncio.run(client_pack_service.content_hash(app=client_pack_app, entries=entries)) != original


def test_bundled_ul_client_configuration_matches_pinned_official_release() -> None:
    for file in installation.UNDEAD_LEGACY_RELEASE.windows_runtime_files:
        if Path(file.path).suffix not in {".cfg", ".ini"}:
            continue
        content = (client_pack._RUNTIME_DEFAULTS / file.path).read_bytes()
        assert len(content) == file.size_bytes
        assert hashlib.sha256(content).hexdigest() == file.sha256


def test_vanilla_sevendays_client_layout_is_unchanged(
    tmp_path: Path,
    client_pack_service: NodeClientPackService,
) -> None:
    manager = Mock()
    manager.list_mods.return_value = ()
    app = Mock(spec=SevenDays)
    app.directory = tmp_path
    app.has_mod_manager = manager
    app.undead_legacy_installation = None
    app.mod_capabilities.include_client_overrides = False
    app.cfg = App_Config(
        name="sevendays_vanilla", instance_key="vanilla", scope="sevendays", directory=tmp_path,
        apps_dir=tmp_path, version=AppVersion(main="2.6"),
    )
    assert client_pack_service.entries(ClientPackSelection(), app=app, include_kubejs_scripts=False) == ()
