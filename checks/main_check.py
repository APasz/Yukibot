from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import config
import main
from _manager import AppStartBlocker, AppStartBlockerKind, App_Manager
from apps._app import App
from apps._config import AppResourcePointProfile, App_Config
from mirror_service import MirrorProject
from relay_notices import BotLifecycleStage, RelayNoticeSeverity, render_system_notice_text


class _FakeApp(App[App_Config]):
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


def _build_fake_app(*, friendly: str) -> _FakeApp:
    app = object.__new__(_FakeApp)
    app.name = friendly.casefold().replace(" ", "_")
    app.friendly = friendly
    app.scope = app.name.partition("_")[0] or app.name
    app.cfg = App_Config(
        name=app.name,
        instance_key=app.name,
        friendly_name=app.friendly,
        directory=Path("."),
        apps_dir=Path("."),
        scope=app.scope,
    )
    app.cfg.resource_points = AppResourcePointProfile(
        running=config.ResourcePointSet(),
        startup=None,
    )
    app._is_running = False
    app.check_running = lambda app=app: cast(bool, getattr(app, "_is_running", False))  # type: ignore[method-assign]
    return app


def _build_resource_app(
    *,
    friendly: str,
    scope: str,
    running_cpu_points: int,
    startup_cpu_points: int,
    running_ram_points: int = 0,
    startup_ram_points: int | None = None,
) -> _FakeApp:
    app = _build_fake_app(friendly=friendly)
    app.scope = scope
    app.cfg = App_Config(
        name=app.name,
        instance_key=app.name,
        friendly_name=app.friendly,
        directory=Path("."),
        apps_dir=Path("."),
        scope=scope,
    )
    app.cfg.resource_points = AppResourcePointProfile(
        running=config.ResourcePointSet(cpu_points=running_cpu_points, ram_points=running_ram_points),
        startup=config.ResourcePointSet(
            cpu_points=startup_cpu_points,
            ram_points=running_ram_points if startup_ram_points is None else startup_ram_points,
        ),
    )
    app._is_running = False
    app.check_running = lambda app=app: cast(bool, getattr(app, "_is_running", False))  # type: ignore[method-assign]
    return app


class _SelectionManager(App_Manager):
    def __init__(
        self,
        *,
        auto_start_app_names: tuple[str, ...],
        app: App[App_Config] | None,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self._auto_start_app_names = auto_start_app_names
        self._app = app
        self._error = error

    def consume_restart_auto_start_apps(self) -> tuple[str, ...]:
        if self._error is not None:
            raise self._error
        return self._auto_start_app_names

    def get(self, name: str) -> App[App_Config]:
        if self._app is None:
            raise LookupError(name)
        return self._app


class _LaunchManager(App_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.launched: list[App[App_Config]] = []
        self._blockers_by_name: dict[str, AppStartBlocker | None] = {}
        self._errors_by_name: dict[str, Exception] = {}

    def set_blocker(self, app: App[App_Config], blocker: AppStartBlocker | None) -> None:
        self._blockers_by_name[app.name] = blocker

    def set_launch_error(self, app: App[App_Config], error: Exception) -> None:
        self._errors_by_name[app.name] = error

    def start_blocker(
        self,
        app: App[App_Config],
        *,
        include_current_activity: bool = True,
    ) -> AppStartBlocker | None:
        if app.name in self._blockers_by_name:
            return self._blockers_by_name[app.name]
        return super().start_blocker(app, include_current_activity=include_current_activity)

    async def launch(self, name: str | App[App_Config]) -> None:
        if isinstance(name, str):
            raise AssertionError("launch helper expected an app instance")
        error = self._errors_by_name.get(name.name)
        if error is not None:
            raise error
        self.launched.append(name)
        setattr(name, "_is_running", True)


@dataclass(frozen=True, slots=True)
class _FakeDisk:
    percent: int
    mountpoint_text: str


class _FakeStats:
    def __init__(self, *, disk: _FakeDisk | None = None) -> None:
        self._disk = disk
        self.paths: list[Path] = []

    def disk_for_path(self, path: Path) -> _FakeDisk | None:
        self.paths.append(path)
        return self._disk


class _FakeCleaner:
    def __init__(self, folders_to_clear: dict[Path, timedelta]) -> None:
        self.folders_to_clear = folders_to_clear
        self.calls: list[tuple[set[Path], timedelta]] = []

    def clear(self, paths: set[Path], threshold: timedelta | None = None) -> set[Path]:
        if threshold is None:
            raise AssertionError("cleanup helper should always pass a threshold")
        self.calls.append((paths, threshold))
        return set()


class MainHelpersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        config.Singleton._instances.pop(_SelectionManager, None)
        config.Singleton._instances.pop(_LaunchManager, None)

    def test_consume_restart_auto_launch_selection_returns_scheduled_notice(self) -> None:
        auto_app = _build_fake_app(friendly="Minecraft Alpha")
        manager = _SelectionManager(auto_start_app_names=("minecraft_alpha",), app=auto_app)

        selection = main._consume_restart_auto_launch_selection(manager)

        self.assertEqual(selection.apps, (auto_app,))
        self.assertEqual(selection.error_lines, ())
        self.assertEqual(selection.started_notice_lines, ("\tAuto-Launch Scheduled: Minecraft Alpha",))

    def test_consume_restart_auto_launch_selection_returns_error_text(self) -> None:
        manager = _SelectionManager(
            auto_start_app_names=("minecraft_alpha",),
            app=None,
            error=LookupError("unknown app"),
        )

        selection = main._consume_restart_auto_launch_selection(manager)

        self.assertEqual(selection.apps, ())
        self.assertEqual(selection.error_lines, ("unknown app",))
        self.assertEqual(selection.started_notice_lines, ())

    def test_build_startup_notice_renders_existing_lines(self) -> None:
        auto_app = _build_fake_app(friendly="Minecraft Alpha")
        auto_launch = main.RestartAutoLaunchSelection(apps=(auto_app,))

        with patch.object(config, "IS_DEBUG", False):
            notice = main._build_startup_notice(
                auto_launch=auto_launch,
                startup_disabled_lines=("Auto-disabled: BeamMP (missing file)",),
                error_lines=("unknown app",),
            )

        self.assertEqual(notice.stage, BotLifecycleStage.STARTED)
        self.assertEqual(notice.severity, RelayNoticeSeverity.WARNING)
        self.assertEqual(
            render_system_notice_text(notice),
            "Started\n\tAuto-Launch Scheduled: Minecraft Alpha\nAuto-disabled: BeamMP (missing file)\nunknown app",
        )

    def test_build_shutdown_notice_renders_uptime(self) -> None:
        notice = main._build_shutdown_notice(
            started_at=datetime.fromisoformat("2026-06-05T12:00:00"),
            now=datetime.fromisoformat("2026-06-05T13:02:03"),
        )

        self.assertEqual(notice.stage, BotLifecycleStage.STOPPING)
        self.assertEqual(render_system_notice_text(notice), "Shutting Down; uptime: 1h 2m 3s")

    def test_build_bot_error_notice_renders_error_summary(self) -> None:
        notice = main._build_bot_error_notice("launcher failed")

        self.assertEqual(notice.stage, BotLifecycleStage.ERROR)
        self.assertEqual(notice.severity, RelayNoticeSeverity.ERROR)
        self.assertEqual(render_system_notice_text(notice), "Error: launcher failed")

    async def test_launch_restart_auto_app_waits_then_launches(self) -> None:
        auto_app = _build_fake_app(friendly="Minecraft Alpha")
        manager = _LaunchManager()

        with patch("main.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            await main._launch_restart_auto_apps(manager, (auto_app,), delay_seconds=3.5)

        sleep_mock.assert_awaited_once_with(3.5)
        self.assertEqual(manager.launched, [auto_app])

    async def test_launch_restart_auto_apps_skips_existing_running_app_and_continues(self) -> None:
        running_app = _build_fake_app(friendly="Minecraft Alpha")
        queued_app = _build_fake_app(friendly="Factorio Lab")
        manager = _LaunchManager()
        manager.set_blocker(
            running_app,
            AppStartBlocker(
                kind=AppStartBlockerKind.ALREADY_RUNNING,
                message=f"{running_app.friendly} is already running.",
            ),
        )

        await main._launch_restart_auto_apps(manager, (running_app, queued_app), delay_seconds=0.0)

        self.assertEqual(manager.launched, [queued_app])

    async def test_launch_restart_auto_apps_tries_later_apps_before_raising_blockers(self) -> None:
        blocked_app = _build_fake_app(friendly="Minecraft Alpha")
        launchable_app = _build_fake_app(friendly="Factorio Lab")
        manager = _LaunchManager()
        manager.set_blocker(
            blocked_app,
            AppStartBlocker(
                kind=AppStartBlockerKind.CPU_POINTS,
                message="Cannot start Minecraft Alpha; node `test` has insufficient CPU points (required 5, available 0).",
                required_points=5,
                available_points=0,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "Cannot start Minecraft Alpha; node `test` has insufficient CPU points"):
            await main._launch_restart_auto_apps(manager, (blocked_app, launchable_app), delay_seconds=0.0)

        self.assertEqual(manager.launched, [launchable_app])

    async def test_launch_restart_auto_apps_uses_feasible_resource_order(self) -> None:
        app_a = _build_resource_app(
            friendly="App Alpha",
            scope="alpha",
            running_cpu_points=2,
            startup_cpu_points=3,
        )
        app_b = _build_resource_app(
            friendly="App Beta",
            scope="beta",
            running_cpu_points=2,
            startup_cpu_points=4,
        )
        manager = _LaunchManager()
        manager.apps = {app_a.name: app_a, app_b.name: app_b}
        manager._load_bot_configuration = Mock(
            return_value=config.BotConfiguration(
                node_capacity=config.NodeCapacityProfile(
                    cpu_points_total=5,
                    ram_points_total=5,
                    cpu_points_reserved=0,
                    ram_points_reserved=0,
                )
            )
        )

        await main._launch_restart_auto_apps(manager, (app_a, app_b), delay_seconds=0.0)

        self.assertEqual(manager.launched, [app_b, app_a])

    def test_clear_managed_files_once_skips_when_service_disabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            cleaner = _FakeCleaner({folder: timedelta(hours=1)})
            stats = _FakeStats()
            profile = config.BotProfileConfig(
                name=config.BotProfileName.YUKI,
                command_groups=(),
                services=frozenset(),
            )

            main._clear_managed_files_once(cleaner, stats, profile=profile)

        self.assertEqual(cleaner.calls, [])
        self.assertEqual(stats.paths, [])

    def test_clear_managed_files_once_uses_configured_thresholds(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            uploads = base / "uploads"
            zips = base / "zips"
            uploads.mkdir()
            zips.mkdir()
            upload_file = uploads / "a.txt"
            zip_file = zips / "b.zip"
            upload_file.write_text("upload", encoding="utf-8")
            zip_file.write_text("zip", encoding="utf-8")
            cleaner = _FakeCleaner(
                {
                    uploads: timedelta(hours=1),
                    zips: timedelta(hours=2),
                }
            )
            stats = _FakeStats()

            main._clear_managed_files_once(cleaner, stats, profile=config.BOT_PROFILES[config.BotProfileName.YUKI])

        self.assertEqual(
            cleaner.calls,
            [
                ({upload_file}, timedelta(hours=1)),
                ({zip_file}, timedelta(hours=2)),
            ],
        )
        self.assertEqual(stats.paths, [uploads, zips])

    async def test_refresh_portal_remote_state_reloads_acl_and_bot_registry(self) -> None:
        acl = Mock()

        async def _run_in_thread(func, *args):
            return func(*args)

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch("main.run_blocking", side_effect=_run_in_thread) as run_blocking_mock,
            patch.object(config, "fetch_remote_bot_registry") as fetch_remote_bot_registry,
        ):
            await main._refresh_portal_remote_state(acl)

        self.assertEqual(
            run_blocking_mock.call_args_list,
            [call(acl.reload), call(fetch_remote_bot_registry)],
        )
        acl.reload.assert_called_once_with()
        fetch_remote_bot_registry.assert_called_once_with()

    async def test_refresh_portal_remote_state_skips_when_not_remote(self) -> None:
        acl = Mock()

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.LOCAL),
            patch("main.run_blocking", new=AsyncMock()) as run_blocking_mock,
        ):
            await main._refresh_portal_remote_state(acl)

        run_blocking_mock.assert_not_awaited()

    async def test_portal_mirror_sync_loop_checks_one_due_project_per_tick(self) -> None:
        stop_event = asyncio.Event()
        result = main.MirrorAutoSyncResult(
            project_id="example",
            outcome=main.MirrorAutoSyncOutcome.UNCHANGED,
            project=cast(MirrorProject, Mock(status_detail="Revision is already published.")),
        )
        mirrors = Mock()
        mirrors.sync_next_due_git_project.return_value = result

        async def run_in_thread(function: Callable[[], object]) -> object:
            self.assertIs(function, mirrors.sync_next_due_git_project)
            stop_event.set()
            return function()

        with patch("main.run_blocking", side_effect=run_in_thread):
            await main._portal_mirror_sync_loop(
                mirrors=cast(main.MirrorService, mirrors),
                stop_event=stop_event,
                interval_seconds=0.01,
            )

        mirrors.sync_next_due_git_project.assert_called_once_with()

    async def test_portal_process_action_exits_main_process_with_failure(self) -> None:
        restart_handler: Callable[[main.NodeSystemAction, bool, bool], None] = Mock()
        mod_web = Mock()

        def _set_restart_handler(handler: Callable[[main.NodeSystemAction, bool, bool], None]) -> None:
            nonlocal restart_handler
            restart_handler = handler

        async def _start(*, acl: object) -> None:
            del acl
            restart_handler(main.NodeSystemAction.RESTART_PROCESS, True, False)

        mod_web.set_system_action_handler.side_effect = _set_restart_handler
        mod_web.start = AsyncMock(side_effect=_start)

        with (
            patch("main.Access_Control", return_value=Mock()),
            patch("main.ModWebService", return_value=mod_web),
            patch("main.mark_pending_process_restart_if_missing"),
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.LOCAL),
            patch.object(config, "IS_RESTARTING", False),
        ):
            with self.assertRaises(SystemExit) as raised:
                await main._run_portal()

            self.assertEqual(raised.exception.code, 1)
            self.assertTrue(config.IS_RESTARTING)

        mod_web.begin_shutdown.assert_called_once_with()

    def test_clear_managed_files_once_shortens_threshold_when_debug_disk_is_full(self) -> None:
        with TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            target = folder / "stale.txt"
            target.write_text("stale", encoding="utf-8")
            cleaner = _FakeCleaner({folder: timedelta(hours=1)})
            stats = _FakeStats(disk=_FakeDisk(percent=91, mountpoint_text="/opt/yukibot"))

            with patch.object(config, "IS_DEBUG", True):
                main._clear_managed_files_once(cleaner, stats, profile=config.BOT_PROFILES[config.BotProfileName.YUKI])

        self.assertEqual(cleaner.calls, [({target}, timedelta(seconds=1))])
        self.assertEqual(stats.paths, [folder])
