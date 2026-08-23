from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import apps._steam as steam_metadata
import config
from apps._config import App_Config, AppVersion, SteamUpdateBranch, SteamUpdateConfig
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
    SteamCmd_Update_Manager,
    _command_error_text,
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


class UpdaterTests(unittest.TestCase):
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
        self.assertEqual([branch.branch_id for branch in update_config.branches], ["latest_experimental", "v3.1.0"])
        self.assertEqual(update_config.selected_branch_config.display_label, "v3.1.0")

    def test_steam_update_preset_resolves_satisfactory_app_metadata(self) -> None:
        from apps.satisfactory import STEAM_APP_ID, STEAM_UPDATE_PRESET

        preset = steam_update_preset_for_scope("satisfactory")

        self.assertIsNotNone(preset)
        assert preset is not None
        self.assertIs(preset, STEAM_UPDATE_PRESET)
        self.assertEqual(preset.app_id, STEAM_APP_ID)

    def test_steamcmd_update_manager_select_branch_persists_choice(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeApp(Path(temp_dir))
            updater = SteamCmd_Update_Manager(app)

            update_info = updater.select_branch("latest_experimental")

        self.assertEqual(app.cfg.steam_update.selected_branch, "latest_experimental")  # type: ignore[union-attr]
        self.assertEqual(app.persisted, 1)
        self.assertEqual(update_info.provider_kind, AppUpdateProviderKind.STEAMCMD)
        self.assertEqual(update_info.selected_branch_label, "Experimental")

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
            stdout_text="",
            stderr_text="ERROR! Failed to install app for login-secret with beta-secret",
        )

        self.assertNotIn("login-secret", error)
        self.assertNotIn("beta-secret", error)
        self.assertIn("******", error)

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
