import asyncio
import io
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import hikari

from _discord import DC_Bound
from _async_utils import run_blocking
from _manager import App_Manager
from apps._app import AppRuntimeFault, AppRuntimeFaultCode, AppRuntimeFaultKind
from apps._config import App_Config
from apps._scs_truck_simulator import (
    ScsTruckSimulator,
    ScsTruckSimulatorMatchers,
    scs_server_config_path,
    scs_server_log_path,
    scs_server_package_paths,
)
from apps.ats import ATS
from apps.ets import ETS
from apps._tailer import Tailer
from relay_notices import AppLifecycleNotice, AppLifecycleState


class ScsTruckSimulatorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _app(app_type: type[ScsTruckSimulator], directory: Path) -> ScsTruckSimulator:
        app = object.__new__(app_type)
        app.directory = directory
        app.scope = app.profile.scope
        app.name = f"{app.profile.scope}_test"
        app.friendly = app.profile.abbreviation
        app.proc_name = app.profile.process_name
        app.proc_cmd = [app.proc_name]
        app.cfg = App_Config(
            name=app.name,
            instance_key="test",
            directory=directory,
            apps_dir=directory,
            scope=app.profile.scope,
        )
        app.chat_channel = hikari.Snowflake(123)
        app.manage_embed_color = app.profile.manage_embed_color
        app.server_log = scs_server_log_path(directory, profile=app.profile)
        app.file_stdout = directory / "stdout.log"
        app.file_errout = directory / "errout.log"
        app._tail = None
        app._tail_matchers = set()
        app._stderr_task = None
        app._stderr_capture_started = False
        config_path = scs_server_config_path(directory, profile=app.profile)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("server_config : Server {}\n", encoding="utf-8")
        for path in scs_server_package_paths(directory, profile=app.profile):
            path.write_bytes(b"exported package")
        return app

    @staticmethod
    def _set_process(app: ScsTruckSimulator, *, exit_code: int | None = None) -> Mock:
        process = Mock(spec=subprocess.Popen)
        process.poll.return_value = exit_code
        app.process = cast(subprocess.Popen[str], process)
        return process

    async def test_failed_launches_do_not_multiply_player_events_after_restart(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                log_path = app.server_log
                assert log_path is not None
                ready = asyncio.Event()
                finished = asyncio.Event()
                player_lines: list[str] = []

                async def match(line: str) -> None:
                    if line == "ready":
                        ready.set()
                    elif "client_id" in line:
                        player_lines.append(line)
                        if "disconnected" in line:
                            finished.set()

                async def launch() -> None:
                    ready.clear()
                    log_path.write_text("ready\n", encoding="utf-8")
                    self._set_process(app)

                app._tail_matchers.add(match)
                ScsTruckSimulatorMatchers(app)
                with (
                    patch.object(app, "_std_launch", new=AsyncMock(side_effect=launch)),
                    patch("apps._scs_truck_simulator.DC_Relay.add") as relay,
                ):
                    try:
                        for _ in range(3):
                            await app.start()
                            await asyncio.wait_for(ready.wait(), timeout=2)
                            previous_tail = app._tail
                            assert previous_tail is not None
                            assert isinstance(app.process, Mock)
                            app.process.poll.return_value = 1
                            await app.handle_unexpected_stop()
                            self.assertFalse(previous_tail.is_running)
                            self.assertIsNone(previous_tail.reader)
                            self.assertIsNone(app._tail)
                            self.assertIsNone(app.process)
                            self.assertEqual(app.consume_unexpected_stop_exit_code(), 1)

                        await app.start()
                        await asyncio.wait_for(ready.wait(), timeout=2)
                        events = (
                            "00:02:49.426 : [MP] APasz connected, client_id = 10",
                            "00:03:30.323 : [MP] APasz disconnected, client_id = 10",
                        )
                        with log_path.open("a", encoding="utf-8") as handle:
                            handle.write("\n".join(events) + "\n")
                        await asyncio.wait_for(finished.wait(), timeout=2)
                        self.assertEqual(player_lines, list(events))
                        self.assertEqual(app.file_stdout.read_text().splitlines(), ["ready", *events])
                        self.assertEqual(relay.call_count, 2)
                    finally:
                        if isinstance(app.process, Mock):
                            app.process.poll.return_value = 0
                        await app.handle_unexpected_stop()

    async def test_start_cleans_up_a_previous_reader_before_launching(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                log_path = app.server_log
                assert log_path is not None
                log_path.write_text("", encoding="utf-8")
                with (
                    patch.object(app, "_std_launch", new=AsyncMock(side_effect=lambda: self._set_process(app))) as launch,
                ):
                    try:
                        await app.start()
                        previous_tail = app._tail
                        assert previous_tail is not None

                        async def relaunch() -> None:
                            self.assertFalse(previous_tail.is_running)
                            self.assertIsNone(app._tail)
                            self.assertIsNone(app.runtime_fault)
                            self._set_process(app)

                        app.runtime_fault = AppRuntimeFault(kind=AppRuntimeFaultKind.UNEXPECTED_EXIT, summary="Old fault")
                        launch.side_effect = relaunch
                        await app.start()
                        self.assertIsNot(app._tail, previous_tail)
                    finally:
                        await app._stop_tailer()

    async def test_expired_gslt_clean_exit_emits_one_actionable_notice_after_stderr_drains(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                log_path = app.server_log
                assert log_path is not None
                log_path.write_text("", encoding="utf-8")
                manager = object.__new__(App_Manager)

                async def launch() -> None:
                    self._set_process(app)
                    app._stderr_task = asyncio.create_task(
                        app._tee(
                            io.StringIO("*** ERROR *** : [MP] Error: Steam log on failed - code 106 .\n"),
                            app.file_errout,
                            "STDERR",
                        )
                    )

                with (
                    patch.object(app, "_std_launch", new=AsyncMock(side_effect=launch)),
                    patch("_manager.DC_Relay.add") as relay,
                ):
                    await app.start()
                    assert isinstance(app.process, Mock)
                    app.process.poll.return_value = 0
                    stopped_at = datetime.now(timezone.utc)
                    app.lifecycle_started_at = stopped_at - timedelta(seconds=10)
                    with patch("_manager.datetime") as manager_clock:
                        manager_clock.now.return_value = stopped_at
                        await manager._handle_inactive_app(app)
                        await manager._handle_inactive_app(app)

                relay.assert_called_once()
                message = cast(DC_Bound, relay.call_args.args[0])
                assert message.relay_embed is not None
                self.assertEqual(message.relay_embed.title, f"{app.friendly} Stopped Unexpectedly")
                self.assertEqual(
                    message.relay_embed.description,
                    "The Steam Game Server Login Token (GSLT) has expired.\n"
                    "Uptime: `10s`\n"
                    f"Next step: Regenerate the GSLT for {app.profile.abbreviation} "
                    f"(App ID {app.profile.steam_game_app_id}), "
                    "replace it in Properties → Steam Game Server Login Token, then restart.",
                )
                assert isinstance(message.notice, AppLifecycleNotice)
                self.assertIs(message.notice.state, AppLifecycleState.UNEXPECTED_STOP)
                assert app.runtime_fault is not None
                self.assertIs(app.runtime_fault.kind, AppRuntimeFaultKind.UNEXPECTED_EXIT)
                self.assertIs(app.runtime_fault.code, AppRuntimeFaultCode.STEAM_GAME_SERVER_LOGIN_TOKEN_EXPIRED)
                self.assertIsNone(app._tail)

    async def test_expiry_diagnosis_requires_current_launch_output_and_the_exact_steam_error(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                expired_message = "[MP] Error: Steam log on failed - code 106 .\n"
                app.file_errout.write_text(expired_message, encoding="utf-8")
                self.assertIsNone(app.diagnose_unexpected_stop())
                app._stderr_capture_started = True
                app._stderr_task = asyncio.create_task(asyncio.sleep(0))
                self.assertIsNone(app.diagnose_unexpected_stop())
                await app._stderr_task
                self.assertIsNotNone(app.diagnose_unexpected_stop())
                for line in (
                    "[MP] Error: Steam log on failed - code 6 .\n",
                    "[MP] Error: Steam log on failed - code 1060 .\n",
                    "[MP] [Chat] APasz: Steam log on failed - code 106 .\n",
                    "[MP] [Chat] APasz: [MP] Error: Steam log on failed - code 106 .\n",
                    "[MP] Session running.\n",
                ):
                    app.file_errout.write_text(line, encoding="utf-8")
                    self.assertIsNone(app.diagnose_unexpected_stop())

                app.file_errout.write_text(expired_message, encoding="utf-8")
                with patch.object(app, "_std_launch", new=AsyncMock(side_effect=RuntimeError("launch failed"))):
                    with self.assertRaisesRegex(RuntimeError, "launch failed"):
                        await app.start()
                self.assertIsNone(app.diagnose_unexpected_stop())

    async def test_failed_stderr_capture_does_not_reuse_a_previous_token_error(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                app.file_errout.write_text("[MP] Error: Steam log on failed - code 106 .\n", encoding="utf-8")
                app._stderr_capture_started = True

                async def failed_capture() -> None:
                    raise PermissionError("cannot replace stderr log")

                app._stderr_task = asyncio.create_task(failed_capture())
                with self.assertRaisesRegex(PermissionError, "cannot replace stderr log"):
                    await app._stderr_task
                with self.assertRaisesRegex(PermissionError, "cannot replace stderr log"):
                    app.diagnose_unexpected_stop()

                app._stderr_task = asyncio.create_task(asyncio.sleep(0))
                app._stderr_task.cancel()
                try:
                    await app._stderr_task
                except asyncio.CancelledError:
                    pass
                self.assertIsNone(app.diagnose_unexpected_stop())

    async def test_an_early_process_exit_fails_startup_without_hanging(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                with patch.object(
                    app, "_std_launch", new=AsyncMock(side_effect=lambda: self._set_process(app, exit_code=1))
                ):
                    with self.assertRaisesRegex(RuntimeError, "stopped before startup completed"):
                        await asyncio.wait_for(app.start(), timeout=1)
                self.assertIsNone(app.process)
                self.assertIsNone(app._tail)
                self.assertFalse(app.is_started)
                self.assertEqual(app.consume_unexpected_stop_exit_code(), 1)

    async def test_exit_between_launch_and_tailer_start_fails_without_hanging(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))

                async def launch() -> None:
                    process = self._set_process(app)
                    process_states = iter((None, 0))
                    process.poll.side_effect = lambda: next(process_states, 0)

                with patch.object(app, "_std_launch", new=AsyncMock(side_effect=launch)):
                    with self.assertRaisesRegex(RuntimeError, "inactive app"):
                        await asyncio.wait_for(app.start(), timeout=1)
                self.assertIsNone(app.process)
                self.assertIsNone(app._tail)
                self.assertFalse(app.is_started)

    async def test_tailer_start_failure_or_cancellation_terminates_the_launched_process(self) -> None:
        for app_type in (ETS, ATS):
            for error_type in (RuntimeError, asyncio.CancelledError):
                with self.subTest(app=app_type.__name__, error=error_type.__name__), TemporaryDirectory() as tmp:
                    app = self._app(app_type, Path(tmp))

                    async def terminate() -> None:
                        app.process = None

                    with (
                        patch.object(app, "_std_launch", new=AsyncMock(side_effect=lambda: self._set_process(app))),
                        patch.object(Tailer, "start", new=AsyncMock(side_effect=error_type("tailer start failed"))),
                        patch.object(app, "_terminate", new=AsyncMock(side_effect=terminate)) as terminate_process,
                    ):
                        with self.assertRaises(error_type):
                            await app.start()
                    terminate_process.assert_awaited_once()
                    self.assertIsNone(app.process)
                    self.assertIsNone(app._tail)
                    self.assertFalse(app.is_started)

    async def test_invalid_log_configuration_does_not_launch_a_process(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                app.server_log = None
                with patch.object(app, "_std_launch", new=AsyncMock()) as launch:
                    with self.assertRaisesRegex(RuntimeError, "requires a server log path"):
                        await app.start()
                launch.assert_not_awaited()

    async def test_failed_tailer_cleanup_still_finalises_the_process_and_stderr(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                self._set_process(app, exit_code=1)
                app._running = True
                tailer = Mock(spec=Tailer)
                tailer.stop = AsyncMock(side_effect=RuntimeError("tailer cleanup failed"))
                app._tail = cast(Tailer, tailer)
                app._stderr_task = asyncio.create_task(app._tee(io.StringIO("last error\n"), app.file_errout, "STDERR"))

                with self.assertRaisesRegex(RuntimeError, "tailer cleanup failed"):
                    await app.handle_unexpected_stop()
                self.assertIsNone(app.process)
                self.assertFalse(app.is_started)
                self.assertEqual(app.file_errout.read_text(), "last error\n")

                for shutdown in (app.stop, app.kill):
                    with patch.object(app, "_terminate", new=AsyncMock()) as terminate:
                        with self.assertRaisesRegex(RuntimeError, "tailer cleanup failed"):
                            await shutdown()
                    terminate.assert_awaited_once()

    async def test_file_based_logging_does_not_block_on_unread_stdout(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                app.cmd_cwd = app.directory
                app.cmd_start = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1048576); sys.stdout.flush()"]
                await app._launch_process()
                process = app.process
                assert process is not None
                with process:
                    try:
                        self.assertEqual(await run_blocking(process.wait, timeout=2), 0)
                        self.assertIsNone(process.stdout)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        await run_blocking(process.wait, timeout=2)
                        await app.handle_unexpected_stop()

    async def test_player_notices_preserve_names_and_ignore_chat_duplicates(self) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(app=app_type.__name__), TemporaryDirectory() as tmp:
                app = self._app(app_type, Path(tmp))
                matchers = ScsTruckSimulatorMatchers(app)
                player = "APasz [Truck Driver] 🚚"
                with patch("apps._scs_truck_simulator.DC_Relay.add") as relay:
                    for action in ("connected", "disconnected"):
                        await matchers.match_transient(f"00:03:30.323 : [MP] [Chat] {player} {action}")
                        await matchers.match_transient(f"00:03:30.323 : [MP] {player} {action}, client_id = 10")
                    await matchers.match_transient("[MP] [Chat] fake connected, client_id = 10")
                self.assertEqual(relay.call_count, 2)
                self.assertEqual([call.args[0].player for call in relay.call_args_list], [player, player])


if __name__ == "__main__":
    unittest.main()
