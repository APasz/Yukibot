from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import config
import main
from _manager import App_Manager
from apps._app import App
from apps._config import App_Config


class _FakeApp(App[App_Config]):
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


def _build_fake_app(*, friendly: str) -> _FakeApp:
    app = object.__new__(_FakeApp)
    app.friendly = friendly
    return app


class _SelectionManager(App_Manager):
    def __init__(
        self,
        *,
        auto_start_app_name: str | None,
        app: App[App_Config] | None,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self._auto_start_app_name = auto_start_app_name
        self._app = app
        self._error = error

    def consume_restart_auto_start_app(self) -> str | None:
        if self._error is not None:
            raise self._error
        return self._auto_start_app_name

    def get(self, name: str) -> App[App_Config]:
        if self._app is None:
            raise LookupError(name)
        return self._app


class _LaunchManager(App_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.launched: list[App[App_Config]] = []

    async def launch(self, name: str | App[App_Config]) -> None:
        if isinstance(name, str):
            raise AssertionError("launch helper expected an app instance")
        self.launched.append(name)


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
        manager = _SelectionManager(auto_start_app_name="minecraft_alpha", app=auto_app)

        selection = main._consume_restart_auto_launch_selection(manager)

        self.assertIs(selection.app, auto_app)
        self.assertIsNone(selection.error_text)
        self.assertEqual(selection.started_notice_line, "\tAuto-Launch Scheduled: Minecraft Alpha")

    def test_consume_restart_auto_launch_selection_returns_error_text(self) -> None:
        manager = _SelectionManager(
            auto_start_app_name="minecraft_alpha",
            app=None,
            error=LookupError("unknown app"),
        )

        selection = main._consume_restart_auto_launch_selection(manager)

        self.assertIsNone(selection.app)
        self.assertEqual(selection.error_text, "unknown app")
        self.assertIsNone(selection.started_notice_line)

    async def test_launch_restart_auto_app_waits_then_launches(self) -> None:
        auto_app = _build_fake_app(friendly="Minecraft Alpha")
        manager = _LaunchManager()

        with patch("main.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            await main._launch_restart_auto_app(manager, auto_app, delay_seconds=3.5)

        sleep_mock.assert_awaited_once_with(3.5)
        self.assertEqual(manager.launched, [auto_app])

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
