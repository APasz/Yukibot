from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import subprocess
from threading import Event
import time
from typing import Literal
from unittest.mock import MagicMock, Mock
import xml.etree.ElementTree as ET
import zipfile

import pytest

import config
from _manager import AppDetailsUpdate, App_Manager
from archive_safety import ZipArchiveLimits, validated_zip_entries
from apps._config import App_Config, Mod_Config, ModClassificationOverride, ModDownloadBlockReason, ModPlacement, ModType, SteamUpdateConfig
from apps.sevendays import Mod_7D2D, SevenDays, SevenDays_Settings
from apps.sevendays import installation


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
