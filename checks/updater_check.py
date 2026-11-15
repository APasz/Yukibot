from __future__ import annotations

import asyncio
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import apps._steam as steam_metadata
import config
from apps._config import App_Config, AppVersion, SteamUpdateBranch, SteamUpdateConfig, SteamUpdatePreset
from apps._steam import (
    STEAM_BRANCH_CACHE_TTL_SECONDS,
    load_steam_update_branches,
    parse_steam_app_info_branches,
    steam_update_preset_for_scope,
)
from apps._updater import (
    AppUpdateBranchState,
    AppUpdateInfo,
    AppUpdateOperationKind,
    AppUpdateProviderKind,
    AppUpdateState,
    SteamAppManifestState,
    SteamCmd_Update_Manager,
    Update_Manager,
    _command_error_text,
    _consume_steamcmd_stream,
    format_steamcmd_output,
    run_steamcmd_command,
)


class _FakeApp:
    def __init__(self, temp_path: Path) -> None:
        self.friendly = "7 Days Alpha"
        self.scope = "sevendays"
        self.directory = temp_path
        self.dir_log = temp_path / "logs"
        self.dir_log.mkdir(parents=True, exist_ok=True)
        self.mods = None
        self.cfg = App_Config(
            name="sevendays_alpha",
            instance_key="alpha",
            friendly_name=self.friendly,
            directory=temp_path,
            apps_dir=temp_path,
            scope="sevendays",
            steam_update=SteamUpdateConfig.model_validate(
                {
                    "app_id": 294420,
                    "branches": [
                        {"branch_id": "public", "label": "Stable"},
                        {"branch_id": "latest_experimental", "label": "Experimental"},
                    ],
                    "selected_branch": "public",
                }
            ),
        )
        self.persisted = 0
        self.applied_versions: list[AppVersion | str | None] = []
        self.running = False

    def persist_instance_config_overrides(self) -> None:
        self.persisted += 1

    def apply_version(self, version: AppVersion | str | None, *, persist: bool) -> bool:
        self.applied_versions.append(version)
        return bool(persist or version is not None)

    def check_running(self) -> bool:
        return self.running

    def detect_installed_version(self) -> AppVersion | None:
        return None


class UpdaterTests(unittest.TestCase):
    def test_steamcmd_output_formats_the_reported_download(self) -> None:
        output = format_steamcmd_output(
            "\x1b[0m Update state (0x61) downloading, progress: 16.83 (2847540397 / 16916605506)\r\n"
        )

        self.assertEqual(output.text, "Downloading game files — 16.8% (2.65 GiB of 15.75 GiB)")
        self.assertEqual(output.phase, "Downloading game files")
        self.assertEqual(output.progress_percent, 16.83)

    def test_steamcmd_output_formats_phases_and_byte_units(self) -> None:
        cases = (
            ("reconfiguring, progress: 0.00 (0 / 0)", "Preparing download — 0.0%", 0.0),
            ("preallocating", "Preparing download", None),
            ("downloading, progress: 50.00 (512 / 1024)", "Downloading game files — 50.0% (512 B of 1.0 KiB)", 50.0),
            ("downloading, progress: 25.00 (1048576 / 4194304)", "Downloading game files — 25.0% (1.0 MiB of 4.0 MiB)", 25.0),
            ("verifying update, progress: 100.00", "Checking installed files — 100.0%", 100.0),
            ("staging, progress: 30.00", "Applying update — 30.0%", 30.0),
            ("waiting for network, progress: 0.00", "Waiting for network — 0.0%", 0.0),
        )
        for state, expected_text, expected_percent in cases:
            with self.subTest(state=state):
                output = format_steamcmd_output(f"Update state (0x61) {state}")
                self.assertEqual(output.text, expected_text)
                self.assertEqual(output.progress_percent, expected_percent)

    def test_steamcmd_output_preserves_errors_and_unrecognized_progress(self) -> None:
        cases = (
            ("\x1b[31mERROR! Disk write failed\x1b[0m\x07", "ERROR! Disk write failed"),
            ("\x1b]0;SteamCMD\x07Warning: retrying", "Warning: retrying"),
            ("\x1b]0;SteamCMD\x1b\\Success! App installed", "Success! App installed"),
            ("\x9b31mERROR! Login failed\x9b0m", "ERROR! Login failed"),
            ("ERROR! Update state (0x61) failed", "ERROR! Update state (0x61) failed"),
            ("Update state (0x61) downloading, progress: 50.00 ERROR! Disk full", "downloading, progress: 50.00 ERROR! Disk full"),
            ("Update state (0x61) downloading, progress: 150.00", "downloading, progress: 150.00"),
            ("Update state (0x61) waiting for connection", "waiting for connection"),
            ("\x1b[0m\x00\x07", ""),
        )
        for line, expected_text in cases:
            with self.subTest(line=line):
                output = format_steamcmd_output(line)
                self.assertEqual(output.text, expected_text)
                self.assertIsNone(output.progress_percent)

    def test_steamcmd_stream_delivers_carriage_return_progress_before_eof(self) -> None:
        async def run() -> None:
            stream = asyncio.StreamReader()
            sink: deque[str] = deque(maxlen=4)
            received: list[tuple[str, str]] = []
            first_line = asyncio.Event()

            def on_output(source: str, line: str) -> None:
                received.append((source, line))
                first_line.set()

            task = asyncio.create_task(
                _consume_steamcmd_stream(stream, source="stdout", sink=sink, on_output=on_output)
            )
            try:
                stream.feed_data(b"\x1b[0mUpdate state (0x61) downloading, progress: 16.83\r")
                await asyncio.wait_for(first_line.wait(), timeout=1)
                self.assertEqual(received, [("stdout", "Update state (0x61) downloading, progress: 16.83")])
                stream.feed_data(b"\nchecking\rapplying\nSuccess! App fully installed.")
                stream.feed_eof()
                await asyncio.wait_for(task, timeout=1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(list(sink), [line for _, line in received])
            self.assertEqual([line for _, line in received][1:], ["checking", "applying", "Success! App fully installed."])

        asyncio.run(run())

    def test_update_status_and_logs_share_friendly_progress_for_update_and_verify(self) -> None:
        for kind in AppUpdateOperationKind:
            with self.subTest(kind=kind), TemporaryDirectory() as temp_dir:
                app = _FakeApp(Path(temp_dir))
                with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                    updater = SteamCmd_Update_Manager(app)
                updater._begin_selected_operation(kind)
                steam_update = app.cfg.steam_update
                assert steam_update is not None
                updater._record_output_line(
                    source="stdout",
                    line="\x1b[0mUpdate state (0x61) downloading, progress: 16.83 (2847540397 / 16916605506)",
                    steam_update=steam_update,
                )
                status = updater.status()
                self.assertEqual(status.summary, "Downloading game files — 16.8% (2.65 GiB of 15.75 GiB)")
                self.assertEqual(status.detail, status.summary)
                self.assertEqual(status.log_lines, (f"stdout: {status.summary}",))
                self.assertEqual(status.progress_percent, 16.83)
                self.assertEqual(status.operation_kind, kind)
                updater._record_output_line(
                    source="stderr", line="\x1b[31mERROR! Disk write failed\x1b[0m", steam_update=steam_update
                )
                self.assertEqual(updater.status().detail, "ERROR! Disk write failed")
                self.assertEqual(updater.status().log_lines[-1], "stderr: ERROR! Disk write failed")

    def test_base_updater_does_not_advertise_verification(self) -> None:
        with TemporaryDirectory() as temp_dir:
            updater = Update_Manager(_FakeApp(Path(temp_dir)))

        self.assertFalse(updater.supports_verify)

    def test_steamcmd_runner_keeps_early_success_when_output_tail_rolls_over(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.returncode = 0
                self.stdout.feed_data(b"Success! App 123 fully installed.\n")
                self.stdout.feed_data(b"\n".join(f"later output {index}".encode() for index in range(8)))
                self.stdout.feed_eof()
                self.stderr.feed_eof()

            async def wait(self) -> int:
                return self.returncode

        async def run() -> None:
            with patch(
                "apps._updater.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=FakeProcess()),
            ):
                completed = await run_steamcmd_command(command=["steamcmd"], cwd=Path("."))
            self.assertTrue(completed)

        asyncio.run(run())

    def test_steamcmd_runner_terminates_when_its_output_sink_fails(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.returncode: int | None = None
                self.terminated = False
                self.wait_calls = 0
                self.stdout.feed_data(b"progress\n")
                self.stdout.feed_eof()
                self.stderr.feed_eof()

            def terminate(self) -> None:
                self.terminated = True

            async def wait(self) -> int:
                self.wait_calls += 1
                return 1

        async def run() -> None:
            process = FakeProcess()

            def reject_output(_: str, __: str) -> None:
                raise RuntimeError("status sink failed")

            with patch(
                "apps._updater.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ):
                with self.assertRaisesRegex(RuntimeError, "status sink failed"):
                    await run_steamcmd_command(command=["steamcmd"], cwd=Path("."), on_output=reject_output)

            self.assertTrue(process.terminated)
            self.assertEqual(process.wait_calls, 1)

        asyncio.run(run())

    def test_steamcmd_runner_requires_an_explicit_success_confirmation(self) -> None:
        async def run(line: str, expected: bool) -> None:
            stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
            stdout.feed_data(line.encode())
            stdout.feed_eof()
            stderr.feed_eof()
            process = Mock(stdout=stdout, stderr=stderr, returncode=0, wait=AsyncMock(return_value=0))
            with patch("apps._updater.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
                result = await run_steamcmd_command(command=["steamcmd"], cwd=Path("."))
            self.assertEqual(result, expected)

        for line, expected in (
            ("ERROR! App was not fully installed.", False),
            ("Warning: missing Success! App confirmation.", False),
            ("  \x1b[0mSuccess! App '123' already up to date.\r", True),
            ("Success! Verified installation of app '123'.", True),
        ):
            with self.subTest(line=line):
                asyncio.run(run(line, expected))

    def test_app_update_info_round_trips_version_branch(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="v3.1.0",
            selected_branch_label="Version 3.1.0 Stable",
            branches=(AppUpdateBranchState(branch_id="v3.1.0", label="Version 3.1.0 Stable", selected=True),),
            supports_verify=True,
        )

        restored = AppUpdateInfo.from_mapping(update_info.to_mapping())

        self.assertEqual(restored, update_info)

    def test_steam_update_config_rejects_unknown_selected_branch(self) -> None:
        with self.assertRaisesRegex(ValueError, "selected steam branch"):
            SteamUpdateConfig.model_validate(
                {
                    "app_id": 294420,
                    "branches": [{"branch_id": "public", "label": "Stable"}],
                    "selected_branch": "latest_experimental",
                }
            )

    def test_steam_update_config_registers_custom_selected_branch(self) -> None:
        update_config = SteamUpdateConfig.model_validate(
            {
                "app_id": 294420,
                "branches": [{"branch_id": "public", "label": "Stable"}],
                "selected_branch": "public",
            }
        )

        next_config = update_config.with_selected_branch(" alpha_21 ", add_if_missing=True)

        self.assertEqual(next_config.selected_branch, "alpha_21")
        self.assertEqual([branch.branch_id for branch in next_config.branches], ["public", "alpha_21"])
        self.assertEqual(next_config.selected_branch_config.display_label, "alpha_21")

    def test_steam_update_preset_defines_only_app_metadata(self) -> None:
        from apps.sevendays import STEAM_APP_ID, STEAM_UPDATE_PRESET

        preset = steam_update_preset_for_scope("sevendays")
        self.assertIsNotNone(preset)
        assert preset is not None
        self.assertIs(preset, STEAM_UPDATE_PRESET)
        self.assertEqual(preset.app_id, STEAM_APP_ID)

        update_config = preset.build_config(selected_branch="v3.1.0")

        self.assertEqual(update_config.selected_branch, "v3.1.0")
        self.assertEqual([branch.branch_id for branch in update_config.branches], ["public", "v3.1.0"])
        self.assertEqual(update_config.selected_branch_config.display_label, "v3.1.0")

    def test_steam_update_preset_requires_one_canonical_branch(self) -> None:
        preset = SteamUpdatePreset(
            app_id=4020,
            default_selected_branch="x86-64",
            required_selected_branch="x86-64",
        )
        legacy_config = SteamUpdateConfig(
            app_id=4020,
            branches=(
                SteamUpdateBranch(branch_id="public", label="Public"),
                SteamUpdateBranch(branch_id="X86-64", label="64-bit binaries"),
            ),
            selected_branch="public",
        )

        constrained_config = preset.normalise_config(legacy_config)

        self.assertEqual(preset.validate_selected_branch("X86-64"), "x86-64")
        self.assertEqual(constrained_config.selected_branch, "x86-64")
        self.assertEqual([branch.branch_id for branch in constrained_config.branches], ["x86-64"])
        self.assertEqual(constrained_config.selected_branch_config.display_label, "64-bit binaries")
        with self.assertRaisesRegex(ValueError, "requires branch"):
            preset.build_config(selected_branch="public")

    def test_steam_update_preset_resolves_satisfactory_app_metadata(self) -> None:
        from apps.satisfactory import STEAM_APP_ID, STEAM_UPDATE_PRESET

        preset = steam_update_preset_for_scope("satisfactory")

        self.assertIsNotNone(preset)
        assert preset is not None
        self.assertIs(preset, STEAM_UPDATE_PRESET)
        self.assertEqual(preset.app_id, STEAM_APP_ID)

    def test_steam_update_preset_resolves_ets_app_metadata(self) -> None:
        from apps.ets import STEAM_APP_ID, STEAM_UPDATE_PRESET

        preset = steam_update_preset_for_scope("ets")

        self.assertIsNotNone(preset)
        assert preset is not None
        self.assertIs(preset, STEAM_UPDATE_PRESET)
        self.assertEqual(preset.app_id, STEAM_APP_ID)
        self.assertEqual(STEAM_APP_ID, 1948160)

    def test_steamcmd_update_manager_select_branch_persists_choice(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            updater = SteamCmd_Update_Manager(app)

            update_info = updater.select_branch("latest_experimental")

        assert app.cfg.steam_update is not None
        self.assertEqual(app.cfg.steam_update.selected_branch, "latest_experimental")
        self.assertEqual(app.persisted, 1)
        self.assertEqual(update_info.provider_kind, AppUpdateProviderKind.STEAMCMD)
        self.assertEqual(update_info.selected_branch_label, "Experimental")
        self.assertTrue(updater.supports_verify)
        self.assertEqual(update_info.supports_verify, updater.supports_verify)

    def test_steamcmd_update_manager_normalises_a_required_branch_before_reporting_info(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            app.friendly = "GMod Alpha"
            app.scope = "gmod"
            app.cfg.steam_update = SteamUpdateConfig(
                app_id=4020,
                branches=(
                    SteamUpdateBranch(branch_id="public", label="Public"),
                    SteamUpdateBranch(branch_id="X86-64", label="64-bit binaries"),
                ),
                selected_branch="public",
            )
            with (
                patch("apps._updater.cached_steam_update_branches", return_value=()),
                patch("apps._updater.steam_update_branch_cache_is_fresh", return_value=True),
            ):
                update_info = SteamCmd_Update_Manager(app).info()

        self.assertEqual(update_info.selected_branch_id, "x86-64")
        self.assertEqual(update_info.selected_branch_label, "64-bit binaries")
        self.assertEqual([branch.branch_id for branch in update_info.branches], ["x86-64"])

    def test_steamcmd_update_manager_lists_version_branches_for_existing_instance(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            updater = SteamCmd_Update_Manager(app)

            with patch(
                "apps._updater.cached_steam_update_branches",
                return_value=(
                    SteamUpdateBranch(branch_id="public", label="Stable"),
                    SteamUpdateBranch(branch_id="v3.1.0", label="Version 3.1.0 Stable"),
                    SteamUpdateBranch(branch_id="alpha21.2", label="Alpha 21.2 Stable"),
                ),
            ):
                update_info = updater.info()

        branches = {branch.branch_id: branch for branch in update_info.branches}
        self.assertEqual(branches["v3.1.0"].label, "Version 3.1.0 Stable")
        self.assertEqual(branches["alpha21.2"].label, "Alpha 21.2 Stable")
        self.assertIn("latest_experimental", branches)
        self.assertEqual(len(branches), 4)

    def test_steamcmd_update_manager_selects_and_persists_version_branch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            updater = SteamCmd_Update_Manager(app)

            with patch(
                "apps._updater.cached_steam_update_branches",
                return_value=(
                    SteamUpdateBranch(branch_id="public", label="Stable"),
                    SteamUpdateBranch(branch_id="latest_experimental", label="Experimental"),
                    SteamUpdateBranch(branch_id="v3.1.0", label="Version 3.1.0 Stable"),
                ),
            ):
                update_info = updater.select_branch("v3.1.0")

        assert app.cfg.steam_update is not None
        self.assertEqual(app.cfg.steam_update.selected_branch, "v3.1.0")
        self.assertEqual(
            [branch.branch_id for branch in app.cfg.steam_update.branches],
            ["public", "latest_experimental", "v3.1.0"],
        )
        self.assertEqual(app.persisted, 1)
        self.assertEqual(update_info.selected_branch_label, "Version 3.1.0 Stable")
        command = updater._steamcmd_command(branch=app.cfg.steam_update.selected_branch_config, validate=False)
        self.assertEqual(command[command.index("-beta") + 1], "v3.1.0")

    def test_steam_app_info_branches_are_parsed_and_cached_for_twelve_hours(self) -> None:
        app_info = '''
Steam> app_info_print 123456
"123456"
{
    "depots"
    {
        "branches"
        {
            "public"
            {
                "buildid" "100"
            }
            "experimental"
            {
                "description" "Experimental Builds"
                "buildid" "101"
            }
        }
    }
}
Steam> quit
'''

        parsed = parse_steam_app_info_branches(app_info, app_id=123456)

        self.assertEqual(
            [(branch.branch_id, branch.label) for branch in parsed],
            [("public", None), ("experimental", "Experimental Builds")],
        )
        self.assertEqual(STEAM_BRANCH_CACHE_TTL_SECONDS, 12 * 60 * 60)

        class _FakeProcess:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return (app_info.encode(), b"")

        async def _exercise_cache() -> tuple[tuple[SteamUpdateBranch, ...], tuple[SteamUpdateBranch, ...], tuple[SteamUpdateBranch, ...], AsyncMock]:
            update_config = SteamUpdateConfig(app_id=123456)
            now = [0.0]
            command_runner = AsyncMock(return_value=_FakeProcess())
            with (
                patch.dict("apps._steam._STEAM_BRANCH_CACHE", {}, clear=True),
                patch.dict("apps._steam._STEAM_BRANCH_FETCHES", {}, clear=True),
                patch("apps._steam.STEAM_BRANCH_CACHE_TTL_SECONDS", 1.0),
                patch("apps._steam.time.monotonic", side_effect=lambda: now[0]),
                patch("apps._steam.asyncio.create_subprocess_exec", command_runner),
            ):
                first = await load_steam_update_branches(
                    update_config,
                    command_prefix=("steamcmd",),
                    working_directory=Path("."),
                )
                now[0] = 0.5
                second = await load_steam_update_branches(
                    update_config,
                    command_prefix=("steamcmd",),
                    working_directory=Path("."),
                )
                now[0] = 1.0
                third = await load_steam_update_branches(
                    update_config,
                    command_prefix=("steamcmd",),
                    working_directory=Path("."),
                )
            return first, second, third, command_runner

        first, second, third, command_runner = asyncio.run(_exercise_cache())

        self.assertEqual(first, parsed)
        self.assertEqual(second, parsed)
        self.assertEqual(third, parsed)
        self.assertEqual(command_runner.await_count, 2)
        self.assertIn("+app_info_update", command_runner.await_args_list[0].args)

    def test_steam_branch_fetch_is_removed_after_all_waiters_cancel(self) -> None:
        async def _exercise() -> None:
            update_config = SteamUpdateConfig(app_id=123456)
            fetch_started = asyncio.Event()
            finish_fetch = asyncio.Event()

            async def _fetch(
                *,
                steam_update: SteamUpdateConfig,
                command_prefix: tuple[str, ...],
                working_directory: Path,
            ) -> tuple[SteamUpdateBranch, ...]:
                del steam_update, command_prefix, working_directory
                fetch_started.set()
                await finish_fetch.wait()
                return (SteamUpdateBranch(branch_id="public"),)

            with (
                patch.dict("apps._steam._STEAM_BRANCH_CACHE", {}, clear=True),
                patch.dict("apps._steam._STEAM_BRANCH_FETCHES", {}, clear=True),
                patch("apps._steam._fetch_steam_update_branches", new=_fetch),
            ):
                waiter = asyncio.create_task(
                    load_steam_update_branches(
                        update_config,
                        command_prefix=("steamcmd",),
                        working_directory=Path("."),
                    )
                )
                await fetch_started.wait()
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter

                finish_fetch.set()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertNotIn(update_config.app_id, steam_metadata._STEAM_BRANCH_FETCHES)

        asyncio.run(_exercise())

    def test_steam_branch_discovery_terminates_on_cancellation(self) -> None:
        class _BlockingProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.communicate_started = asyncio.Event()
                self.terminated = False
                self.wait_calls = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                self.communicate_started.set()
                await asyncio.Event().wait()
                return (b"", b"")

            def terminate(self) -> None:
                self.terminated = True

            async def wait(self) -> int:
                self.wait_calls += 1
                self.returncode = 1
                return self.returncode

        async def _exercise(working_directory: Path) -> _BlockingProcess:
            process = _BlockingProcess()
            with patch(
                "apps._steam.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ):
                discovery = asyncio.create_task(
                    steam_metadata._fetch_steam_update_branches(
                        steam_update=SteamUpdateConfig(app_id=123456),
                        command_prefix=("steamcmd",),
                        working_directory=working_directory,
                    )
                )
                await process.communicate_started.wait()
                discovery.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await discovery
            return process

        with TemporaryDirectory() as temp_dir:
            process = asyncio.run(_exercise(Path(temp_dir) / "steam-info"))

        self.assertTrue(process.terminated)
        self.assertEqual(process.wait_calls, 1)

    def test_steamcmd_command_error_redacts_credentials_in_command_and_output(self) -> None:
        error = _command_error_text(
            command=[
                "steamcmd",
                "+login",
                "account",
                "login-secret",
                "+app_update",
                "294420",
                "-beta",
                "alpha_21",
                "-betapassword",
                "beta-secret",
                "+quit",
            ],
            stdout_text="\x1b[0mUpdate state (0x61) downloading, progress: 16.83 (2847540397 / 16916605506)",
            stderr_text="\x1b[31mERROR! Failed to install app for login-\x1b[0msecret with beta-secret",
        )

        self.assertNotIn("login-secret", error)
        self.assertNotIn("beta-secret", error)
        self.assertIn("******", error)
        self.assertIn("Downloading game files — 16.8% (2.65 GiB of 15.75 GiB)", error)
        self.assertIn("ERROR! Failed to install app", error)
        self.assertNotIn("\x1b", error)
        self.assertNotIn("0x61", error)

    def test_verify_selected_requires_app_to_be_stopped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            app.running = True
            with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                updater = SteamCmd_Update_Manager(app)

            with self.assertRaisesRegex(RuntimeError, "must be stopped"):
                asyncio.run(updater.verify_selected())

    def test_steamcmd_update_manager_uses_global_configuration_script_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            with patch(
                "apps._updater.config.load_bot_configuration",
                return_value=config.BotConfiguration(steamcmd_path="./steamcmd.sh"),
            ):
                updater = SteamCmd_Update_Manager(app)

        self.assertEqual(updater._steamcmd_command_prefix, ("bash", "./steamcmd.sh"))

    def test_start_selected_update_sets_running_status_immediately(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                app = _FakeApp(Path(temp_dir))
                with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                    updater = SteamCmd_Update_Manager(app)
                gate = asyncio.Event()

                async def _pending_run(*, kind: AppUpdateOperationKind, branch: object) -> object:
                    del kind, branch
                    await gate.wait()
                    return object()

                with patch.object(updater, "_run_started_operation", new=AsyncMock(side_effect=_pending_run)):
                    result = await updater.start_selected_update()
                    status = updater.status()
                    self.assertEqual(result.kind, AppUpdateOperationKind.UPDATE)
                    self.assertTrue(status.running)
                    self.assertEqual(status.state, AppUpdateState.RUNNING)
                    self.assertEqual(status.operation_kind, AppUpdateOperationKind.UPDATE)
                    self.assertEqual(status.progress_percent, 0.0)
                    gate.set()
                    active_task = updater._active_task
                    if active_task is not None:
                        await active_task

        asyncio.run(_run())

    def test_info_reads_installed_steam_manifest_build_and_branch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            manifest_dir = app.directory / "steamapps"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest_dir.joinpath("appmanifest_294420.acf").write_text(
                "\n".join(
                    (
                        '"AppState"',
                        "{",
                        '    "appid" "294420"',
                        '    "buildid" "9876543"',
                        '    "UserConfig"',
                        "    {",
                        '        "betakey" "latest_experimental"',
                        "    }",
                        "}",
                    )
                ),
                encoding=config.STR_ENCODE,
            )
            with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                updater = SteamCmd_Update_Manager(app)

            update_info = updater.info()

        self.assertEqual(update_info.installed_build_id, 9876543)
        self.assertEqual(update_info.installed_branch_id, "latest_experimental")

    def test_info_reads_manifest_from_ancestor_steamapps_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_root = root / "steamapps" / "common" / "server"
            app_root.mkdir(parents=True, exist_ok=True)
            app = _FakeApp(app_root)
            root.joinpath("steamapps", "appmanifest_294420.acf").write_text(
                "\n".join(
                    (
                        '"AppState"',
                        "{",
                        '    "appid" "294420"',
                        '    "buildid" "13579"',
                        "}",
                    )
                ),
                encoding=config.STR_ENCODE,
            )
            with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                updater = SteamCmd_Update_Manager(app)

            update_info = updater.info()

        self.assertEqual(update_info.installed_build_id, 13579)
        self.assertEqual(update_info.installed_branch_id, "public")

    def test_safe_read_installed_manifest_logs_once_for_same_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            manifest_dir = app.directory / "steamapps"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest_dir.joinpath("appmanifest_294420.acf").write_text(
                "\n".join(
                    (
                        '"AppState"',
                        "{",
                        '    "appid" "294420"',
                        '    "buildid" "9876543"',
                        "}",
                    )
                ),
                encoding=config.STR_ENCODE,
            )
            with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                updater = SteamCmd_Update_Manager(app)

            with self.assertLogs("apps._updater", level="INFO") as captured:
                updater._safe_read_installed_manifest()
                updater._safe_read_installed_manifest()

        self.assertEqual(
            [
                line
                for line in captured.output
                if "Steam app manifest loaded: app=7 Days Alpha app_id=294420 branch=public build=9876543" in line
            ],
            [
                "INFO:apps._updater:Steam app manifest loaded: app=7 Days Alpha app_id=294420 branch=public build=9876543"
            ],
        )

    def test_update_selected_persists_detected_version_with_steam_manifest_metadata(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                app = _FakeApp(Path(temp_dir))
                manifest_dir = app.directory / "steamapps"
                manifest_dir.mkdir(parents=True, exist_ok=True)
                manifest_dir.joinpath("appmanifest_294420.acf").write_text(
                    "\n".join(
                        (
                            '"AppState"',
                            "{",
                            '    "appid" "294420"',
                            '    "buildid" "24680"',
                            '    "UserConfig"',
                            "    {",
                            '        "betakey" "latest_experimental"',
                            "    }",
                            "}",
                        )
                    ),
                    encoding=config.STR_ENCODE,
                )
                with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                    updater = SteamCmd_Update_Manager(app)
                with (
                    patch.object(updater, "_run_steamcmd", new=AsyncMock(return_value=True)),
                    patch.object(updater, "_detect_installed_version", return_value=AppVersion(main="2.0", build=8)),
                ):
                    result = await updater.update_selected()

                self.assertEqual(
                    app.applied_versions[-1],
                    AppVersion(main="2.0", build=8, steam_build=24680, steam_branch="latest_experimental"),
                )
                self.assertEqual(
                    result.version_text,
                    "2.0:8 [Steam latest_experimental build 24680]",
                )

        asyncio.run(_run())

    def test_manifest_without_a_build_does_not_invalidate_a_build_only_version(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            with patch("apps._updater.config.load_bot_configuration", return_value=config.BotConfiguration()):
                updater = SteamCmd_Update_Manager(app)

            version = AppVersion(steam_branch="public", steam_build=24680)
            resolved = updater._version_with_manifest_data(
                version,
                SteamAppManifestState(app_id=294420, branch_id="latest_experimental"),
            )

        self.assertEqual(resolved, version)
