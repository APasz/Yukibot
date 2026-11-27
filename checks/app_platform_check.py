from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import AsyncMock, Mock, patch

import hikari
import pytest

import config
from _manager import App_Manager, AppInstanceCreateRequest, AppStartBlockerKind
from apps._app import App
from apps._platform import AppArchitecture, AppOperatingSystem, AppPlatform, LINUX_ARM64, LINUX_X86_64
from apps._config import App_Config
from apps.sevendays import SevenDays
from config import Activity_Manager
from node_api.app_installer import NodeAppInstallerService, NodeAppInstallRequest
from node_api.operations import NodeOperationService


WINDOWS_X86_64 = AppPlatform(AppOperatingSystem.WINDOWS, AppArchitecture.X86_64)


@contextmanager
def _host(operating_system: str, machine: str = "x86_64") -> Iterator[None]:
    with patch("apps._platform.sys.platform", operating_system), patch("apps._platform.platform.machine", return_value=machine):
        yield


class _Adapter(App[App_Config]):
    async def start(self) -> bool:
        await self._std_launch()
        return True

    async def stop(self) -> bool:
        return True


class _WindowsAdapter(_Adapter):
    supported_platforms: ClassVar[frozenset[AppPlatform]] = frozenset({WINDOWS_X86_64})


class _MixedAdapter(_Adapter):
    supported_platforms: ClassVar[frozenset[AppPlatform]] = frozenset({WINDOWS_X86_64, LINUX_ARM64})


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> App_Manager:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "APP_PATH", tmp_path / "game-data")
    monkeypatch.setattr(config, "ENABLED_DUMP_FILE", tmp_path / "enabled-apps.txt")
    for scope in ("minecraft", "sevendays"):
        directory = tmp_path / "apps" / scope
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
        (directory / "instances.json").write_text(json.dumps({
            "alpha": {"directory": f"/linux-servers/{scope}", "enabled": True},
            "disabled": {"directory": f"/linux-servers/{scope}-disabled", "enabled": False},
        }), encoding="utf-8")
    manager = object.__new__(App_Manager)
    App_Manager.__init__(manager)
    manager.bot = cast(hikari.GatewayBot, Mock())
    manager.activity_manager = cast(Activity_Manager, Mock())
    return manager


@pytest.fixture
def app_config(tmp_path: Path) -> App_Config:
    return App_Config(
        name="test_alpha", scope="test", instance_key="alpha", friendly_name="Test",
        directory=tmp_path / "game", apps_dir=tmp_path / "apps", enabled=True,
    )


@pytest.mark.parametrize("scope", ("ats", "beammp", "ets", "factorio", "gmod", "minecraft", "satisfactory", "sevendays"))
def test_existing_adapters_default_to_linux_x86_64(scope: str) -> None:
    adapter, _ = App_Manager._load_scope_types(scope)
    expected = frozenset({LINUX_X86_64, LINUX_ARM64}) if scope == "factorio" else frozenset({LINUX_X86_64})
    assert adapter.supported_platforms == expected
    with _host("linux"):
        assert adapter.platform_support_error() is None
    with _host("win32"):
        assert "does not support Windows" in (adapter.platform_support_error() or "")


@pytest.mark.parametrize("host", ("win32", "darwin"))
def test_unlisted_hosts_fail_explicitly(host: str) -> None:
    with _host(host), pytest.raises(ValueError, match="supported platforms: Linux/x86_64"):
        _Adapter.require_supported_platform()


@pytest.mark.parametrize("machine, expected", (
    ("x86_64", AppArchitecture.X86_64), ("AMD64", AppArchitecture.X86_64),
    (" x64 ", AppArchitecture.X86_64), ("aarch64", AppArchitecture.ARM64), ("ARM64", AppArchitecture.ARM64),
))
def test_host_detection_normalises_architecture_aliases(machine: str, expected: AppArchitecture) -> None:
    with _host("linux", machine):
        assert AppPlatform.current() == AppPlatform(AppOperatingSystem.LINUX, expected)


@pytest.mark.parametrize("operating_system, machine, error", (
    ("freebsd14", "x86_64", "Unsupported operating system"),
    ("linux", "riscv64", "Unsupported CPU architecture"),
    ("linux", "armv7l", "Unsupported CPU architecture"),
    ("linux", "", "Unsupported CPU architecture"),
))
def test_unknown_host_components_fail_without_a_fallback(operating_system: str, machine: str, error: str) -> None:
    with _host(operating_system, machine):
        with pytest.raises(ValueError, match=error):
            AppPlatform.current()
        with pytest.raises(ValueError, match=error):
            _Adapter.require_supported_platform()
        assert error in (_Adapter.platform_support_error() or "")


@pytest.mark.parametrize("operating_system, architecture", (
    ("linux", AppArchitecture.X86_64), (AppOperatingSystem.LINUX, "arm64"),
))
def test_platform_fields_require_typed_components(operating_system: object, architecture: object) -> None:
    with pytest.raises(TypeError):
        AppPlatform(cast(AppOperatingSystem, operating_system), cast(AppArchitecture, architecture))


@pytest.mark.parametrize("operating_system, machine, supported", (
    ("win32", "AMD64", True), ("linux", "aarch64", True),
    ("win32", "arm64", False), ("linux", "x86_64", False),
))
def test_support_requires_the_declared_pair(operating_system: str, machine: str, supported: bool) -> None:
    with _host(operating_system, machine):
        assert (_MixedAdapter.platform_support_error() is None) is supported


def test_factorio_arm_support_does_not_enable_other_adapters() -> None:
    factorio, _ = App_Manager._load_scope_types("factorio")
    minecraft, _ = App_Manager._load_scope_types("minecraft")
    with _host("linux", "aarch64"):
        assert factorio.require_supported_platform() == LINUX_ARM64
        with pytest.raises(ValueError, match="does not support Linux/arm64"):
            minecraft.require_supported_platform()


def test_an_adapter_can_explicitly_support_another_os() -> None:
    with _host("win32"):
        assert _WindowsAdapter.platform_support_error() is None
        _WindowsAdapter.require_supported_platform()
    with _host("linux"), pytest.raises(ValueError, match="supported platforms: Windows/x86_64"):
        _WindowsAdapter.require_supported_platform()


@pytest.mark.parametrize("declaration", (frozenset(), frozenset({"linux"}), {LINUX_X86_64}))
def test_invalid_platform_declarations_are_rejected(declaration: object) -> None:
    with pytest.raises((TypeError, ValueError), match="supported_platforms"):
        class InvalidAdapter(_Adapter):
            supported_platforms = cast(frozenset[AppPlatform], declaration)


def test_base_initialisation_rejects_an_unsupported_host_before_creating_files(
    manager: App_Manager, app_config: App_Config,
) -> None:
    with _host("win32"), pytest.raises(ValueError, match="does not support Windows"):
        _Adapter(cast(hikari.GatewayBot, manager.bot), cast(Activity_Manager, manager.activity_manager), app_config)
    assert not app_config.directory.exists()


def test_discovery_and_install_recipes_follow_adapter_support(manager: App_Manager) -> None:
    with _host("linux"):
        assert manager.list_create_scopes() == ("minecraft", "sevendays")
        assert tuple(recipe.scope for recipe in manager.list_steam_install_recipes()) == ("sevendays",)
    with _host("win32"):
        assert manager.list_create_scopes() == ()
        assert manager.list_known_scopes() == ()
        assert manager.list_steam_install_recipes() == ()
        with patch.object(SevenDays, "supported_platforms", frozenset({WINDOWS_X86_64})):
            assert manager.list_create_scopes() == ("sevendays",)
            assert tuple(recipe.scope for recipe in manager.list_steam_install_recipes()) == ("sevendays",)
    with _host("linux", "arm64"):
        assert manager.list_create_scopes() == ()
        assert manager.list_steam_install_recipes() == ()


@pytest.mark.parametrize("target", (WINDOWS_X86_64, LINUX_ARM64))
def test_direct_creation_and_defaults_cannot_bypass_platform_support(manager: App_Manager, target: AppPlatform) -> None:
    instances_path = Path("apps/minecraft/instances.json")
    original = instances_path.read_bytes()
    request = AppInstanceCreateRequest(scope="minecraft", instance_key="beta", friendly_name="Test", subfolder="test")
    with patch.object(AppPlatform, "current", return_value=target):
        for create in (manager.prepare_instance_creation, manager.create_instance):
            with pytest.raises(ValueError, match=f"does not support {target.display_name}"):
                create(request)
        with pytest.raises(ValueError, match=f"does not support {target.display_name}"):
            manager.suggest_instance_install_defaults(scope="minecraft")
    assert instances_path.read_bytes() == original
    assert not config.APP_PATH.exists()


@pytest.mark.parametrize("machine", ("x86_64", "arm64"))
def test_startup_skips_unsupported_instances_without_disabling_them(
    manager: App_Manager, caplog: pytest.LogCaptureFixture, machine: str,
) -> None:
    paths = tuple(Path("apps").glob("*/instances.json"))
    original = {path: path.read_bytes() for path in paths}

    async def load() -> None:
        with (
            _host("win32" if machine == "x86_64" else "linux", machine),
            patch.object(manager, "_build_app_config") as build_config,
            patch.object(manager, "_instantiate_app") as instantiate,
            patch.object(manager, "_disable_missing_instance") as disable,
        ):
            await manager.load_apps(cast(hikari.GatewayBot, manager.bot))
        build_config.assert_not_called()
        instantiate.assert_not_called()
        disable.assert_not_called()

    asyncio.run(load())
    assert manager.apps == {}
    assert {path: path.read_bytes() for path in paths} == original
    notices = manager.startup_disabled_notice_lines()
    assert len(notices) == 4
    assert all(line.startswith("Skipped:") and "does not support" in line for line in notices)
    assert "Configuration was not changed" in caplog.text


def test_direct_instance_loading_and_construction_reject_unsupported_hosts(
    manager: App_Manager, app_config: App_Config,
) -> None:
    async def load() -> None:
        with _host("win32"), pytest.raises(ValueError, match="does not support Windows"):
            await manager.load_instance(scope="minecraft", instance_key="alpha")

    asyncio.run(load())
    with (
        _host("win32"),
        patch.object(_Adapter, "__init__", side_effect=AssertionError("Adapter constructor ran")) as constructor,
        pytest.raises(ValueError, match="does not support Windows"),
    ):
        manager._instantiate_app(bot=cast(hikari.GatewayBot, manager.bot), app_cls=_Adapter, cfg=app_config)
    constructor.assert_not_called()


@pytest.mark.parametrize("target", (WINDOWS_X86_64, LINUX_ARM64))
def test_launch_rejects_unsupported_hosts_before_saving_settings_or_starting(
    manager: App_Manager, app_config: App_Config, target: AppPlatform,
) -> None:
    app = object.__new__(_Adapter)
    app.cfg = app_config
    app.settings = Mock()

    async def launch() -> None:
        with patch.object(AppPlatform, "current", return_value=target), patch.object(app, "start", new_callable=AsyncMock) as start:
            blocker = manager.start_blocker(app)
            assert blocker is not None and blocker.kind is AppStartBlockerKind.PLATFORM
            with pytest.raises(RuntimeError, match=f"does not support {target.display_name}"):
                await manager.launch(app)
        start.assert_not_awaited()

    asyncio.run(launch())
    app.settings.app.save.assert_not_called()
    assert manager._pending_start_names == set()


def test_direct_process_launch_is_guarded_before_prelaunch_or_subprocess_work() -> None:
    app = object.__new__(_Adapter)

    async def launch() -> None:
        with (
            _host("win32"),
            patch.object(app, "_prelaunch_tasks", new_callable=AsyncMock) as prelaunch,
            patch("apps._app.subprocess.Popen") as popen,
        ):
            for start in (app._std_launch, app._launch_process):
                with pytest.raises(ValueError, match="does not support Windows"):
                    await start()
        prelaunch.assert_not_awaited()
        popen.assert_not_called()

    asyncio.run(launch())


def test_installer_rejects_unsupported_apps_without_querying_steam_or_creating_a_job(manager: App_Manager) -> None:
    operations = NodeOperationService()
    service = NodeAppInstallerService(
        node_name=lambda: "test", invalidate_state_caches=Mock(), operations=operations,
    )
    acl = Mock()
    acl.perm_check = AsyncMock()
    request = NodeAppInstallRequest(
        scope="sevendays", instance_key="beta", friendly_name="Test", subfolder="test", steam_branch_id="public",
    )

    async def install() -> None:
        with (
            _host("win32"),
            patch.object(service, "_with_discovered_branches", new_callable=AsyncMock) as discover,
            patch.object(operations, "create") as create,
            patch("node_api.app_installer.run_steamcmd_command", new_callable=AsyncMock) as steamcmd,
        ):
            assert (await service.build_catalog(manager=manager)).recipes == ()
            with pytest.raises(ValueError, match="not available for installation"):
                await service.build_recipe(scope="sevendays", manager=manager)
            for install in (service.preflight_install, service.start_install):
                with pytest.raises(ValueError, match="not available for installation"):
                    await install(actor_user_id=1, request=request, manager=manager, acl=acl)
        discover.assert_not_awaited()
        create.assert_not_called()
        steamcmd.assert_not_awaited()

    asyncio.run(install())
    assert not config.APP_PATH.exists()
