from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

from _manager import AppInstallInput, App_Manager
from apps._app import AppPortClaim, NetworkProtocol
from apps._config import App_Config, AppVersion, SteamUpdateBranch
from apps._updater import SteamCmd_Update_Manager
from apps._steam import STEAM_GAME_SERVER_LOGIN_TOKEN_MANAGEMENT_URL
from apps.gmod import (
    GMOD_DEFAULT_GAMEMODE,
    GMOD_DEFAULT_MAX_PLAYERS,
    GMOD_DEFAULT_PORT,
    GMOD_DEFAULT_STARTUP_MAP,
    GMOD_MANAGE_EMBED_COLOR,
    STEAM_APP_ID,
    STEAM_GAME_APP_ID,
    STEAM_UPDATE_PRESET,
    Gmod,
    Gmod_Settings,
    _read_gmod_game_server_login_token,
    ensure_gmod_managed_files,
    gmod_game_server_login_token_path,
    gmod_server_config_path,
    gmod_settings_path,
    gmod_start_command,
    resolve_gmod_game_port,
)
from node_api.app_installer import NodeAppInstallInputKind, NodeAppInstallRequest, NodeAppInstallerService
from node_api.service import NodeApiService


def _write_gmod_steam_manifest(directory: Path, *, build_id: int, branch_id: str = "public") -> None:
    manifest_path = directory / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    user_config: tuple[str, ...] = ()
    if branch_id != "public":
        user_config = (
            '    "UserConfig"',
            "    {",
            f'        "betakey" "{branch_id}"',
            "    }",
        )
    manifest_path.write_text(
        "\n".join(
            (
                '"AppState"',
                "{",
                f'    "appid" "{STEAM_APP_ID}"',
                f'    "buildid" "{build_id}"',
                *user_config,
                "}",
            )
        ),
        encoding="utf-8",
    )


class GmodSettingsTests(unittest.TestCase):
    def test_default_launch_settings_are_persisted_with_sensible_values(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)

            settings = Gmod_Settings(gmod_settings_path(directory))

            self.assertEqual(settings.startup_map, GMOD_DEFAULT_STARTUP_MAP)
            self.assertEqual(settings.gamemode, GMOD_DEFAULT_GAMEMODE)
            self.assertEqual(settings.max_players, GMOD_DEFAULT_MAX_PLAYERS)
            self.assertTrue(gmod_server_config_path(directory).is_file())

    def test_custom_launch_settings_drive_the_generated_command(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            settings = Gmod_Settings(gmod_settings_path(directory))
            settings_by_key = {setting.key: setting for setting in settings.options}
            settings_by_key["startup_map"].update("gm_flatgrass")
            settings_by_key["gamemode"].update("darkrp")
            settings_by_key["max_players"].update("32")
            settings.save()

            reloaded = Gmod_Settings(gmod_settings_path(directory))
            command = gmod_start_command(
                port=27030,
                max_players=reloaded.max_players,
                gamemode=reloaded.gamemode,
                startup_map=reloaded.startup_map,
                game_server_login_token=token,
            )

        self.assertEqual(
            command,
            [
                "./srcds_run",
                "-game",
                "garrysmod",
                "+port",
                "27030",
                "+maxplayers",
                "32",
                "+gamemode",
                "darkrp",
                "+map",
                "gm_flatgrass",
                "+sv_setsteamaccount",
                token,
            ],
        )

    def test_launch_settings_reject_unsafe_map_and_gamemode_names(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            settings = Gmod_Settings(gmod_settings_path(directory))
            settings_by_key = {setting.key: setting for setting in settings.options}

            with self.assertRaisesRegex(ValueError, "not valid"):
                settings_by_key["startup_map"].update("gm construct")
            with self.assertRaisesRegex(ValueError, "not valid"):
                settings_by_key["gamemode"].update("sandbox; quit")


class GmodIntegrationTests(unittest.TestCase):
    @staticmethod
    def _config(
        directory: Path,
        *,
        port: int | None = None,
        steam_update: bool = True,
        version: AppVersion | None = None,
    ) -> App_Config:
        return App_Config(
            name="gmod_alpha",
            instance_key="alpha",
            friendly_name="GMod Alpha",
            directory=directory,
            apps_dir=directory / "apps" / "gmod",
            join_port=port,
            scope="gmod",
            version=version,
            steam_update=STEAM_UPDATE_PRESET.build_config() if steam_update else None,
        )

    @classmethod
    def _app(
        cls,
        directory: Path,
        *,
        port: int | None = None,
        version: AppVersion | None = None,
    ) -> Gmod:
        with patch("apps._updater.resolve_steamcmd_command_prefix", return_value=("steamcmd",)):
            return Gmod(Mock(), Mock(), cls._config(directory, port=port, version=version))

    def test_fresh_install_loads_and_displays_the_steam_manifest_build(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        request = NodeAppInstallerService._create_request(
            NodeAppInstallRequest(
                scope="gmod",
                instance_key="alpha",
                friendly_name="GMod Alpha",
                subfolder="gmod-alpha",
                steam_branch_id="public",
                inputs={AppInstallInput.GAME_SERVER_LOGIN_TOKEN: token},
            )
        )
        self.assertIsNone(request.initial_version)
        self.assertTrue(request.clear_template_version)

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_gmod_steam_manifest(directory, build_id=1234567)

            app = self._app(directory)
            with patch("apps._updater.steam_update_branch_cache_is_fresh", return_value=True):
                update_info = app.update_info

        self.assertEqual(app.cfg.version, AppVersion(steam_branch="public", steam_build=1234567))
        self.assertEqual(app.version_display, "Steam public build 1234567")
        self.assertNotIn("0.0", app.version_display)
        assert update_info is not None
        self.assertEqual(update_info.installed_build_id, 1234567)
        self.assertEqual(update_info.installed_branch_id, "public")

    def test_legacy_placeholder_is_not_reported_without_a_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(
                Path(temporary_directory),
                version=AppVersion(main="0.0", steam_branch="public"),
            )

        self.assertEqual(app.version_display, "none")

    def test_update_and_verify_refresh_the_displayed_steam_build(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_steam_manifest(directory, build_id=100)
                app = self._app(directory, version=AppVersion(main="0.0"))
                assert isinstance(app.updater, SteamCmd_Update_Manager)
                self.assertEqual(app.version_display, "Steam public build 100")
                builds = iter((200, 300))

                async def _run_steamcmd(*, branch: SteamUpdateBranch, validate: bool) -> bool:
                    del branch, validate
                    _write_gmod_steam_manifest(directory, build_id=next(builds))
                    return True

                with patch.object(app.updater, "_run_steamcmd", new=_run_steamcmd):
                    update_result = await app.updater.update_selected()
                    self.assertEqual(app.version_display, "Steam public build 200")
                    verify_result = await app.updater.verify_selected()

                with patch("apps._updater.steam_update_branch_cache_is_fresh", return_value=True):
                    update_info = app.update_info
                self.assertEqual(update_result.version_text, "Steam public build 200")
                self.assertEqual(verify_result.version_text, "Steam public build 300")
                self.assertEqual(app.cfg.version, AppVersion(steam_branch="public", steam_build=300))
                self.assertEqual(app.version_display, "Steam public build 300")
                self.assertNotIn("0.0", app.version_display)
                assert update_info is not None
                self.assertEqual(update_info.installed_build_id, 300)
                self.assertEqual(update_info.installed_branch_id, "public")

        asyncio.run(_run())

    def test_port_claims_reserve_only_the_configured_udp_game_port(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory), port=27031)

            claims = app.listening_port_claims

        self.assertEqual(
            claims,
            (
                AppPortClaim(
                    protocol=NetworkProtocol.UDP,
                    port=27031,
                    purpose="game server",
                ),
            ),
        )
        self.assertEqual(resolve_gmod_game_port(None), GMOD_DEFAULT_PORT)
        with self.assertRaisesRegex(TypeError, "integer"):
            resolve_gmod_game_port(True)

    def test_manage_embed_color_uses_gmod_blue(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))

        self.assertEqual(GMOD_MANAGE_EMBED_COLOR, 0x1194F0)
        self.assertEqual(app.manage_embed_color, GMOD_MANAGE_EMBED_COLOR)

    def test_gslt_is_write_only_and_redacted_from_config_and_runtime_output(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app.set_steam_game_server_login_token(f"  {token}  ")
            token_path = gmod_game_server_login_token_path(directory)

            status = app.steam_game_server_login_token_status
            self.assertTrue(status.configured)
            self.assertEqual(status.game_app_id, STEAM_GAME_APP_ID)
            self.assertEqual(_read_gmod_game_server_login_token(directory), token)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(token, repr(status))
            self.assertNotIn(token, str(status.to_mapping()))

            server_config = gmod_server_config_path(directory)
            server_config.write_text(
                f'hostname "GMod Alpha"; sv_setsteamaccount {token}; sv_lan 0\n',
                encoding="utf-8",
            )
            displayed = app.read_config_file("server/server.cfg")
            displayed_config = displayed.content
            self.assertNotIn(token, displayed_config)
            self.assertEqual(
                displayed_config,
                'hostname "GMod Alpha"; sv_setsteamaccount [REDACTED]; sv_lan 0\n',
            )
            self.assertIsNotNone(displayed.warning)
            assert displayed.warning is not None
            self.assertIn("legacy/manual", displayed.warning)
            self.assertIn("Remove it", displayed.warning)
            self.assertIn("Yukibot", displayed.warning)
            with self.assertRaisesRegex(ValueError, "managed"):
                app.write_config_file("server/server.cfg", f"hostname GMod; sv_setsteamaccount {token}\n")

            app._launch_token = token
            app.cmd_start = app._launch_command(token)
            with patch("apps.gmod.log.info") as launch_log:
                app.log_launch_context()
            launch_text = str(launch_log.call_args)
            self.assertNotIn(token, launch_text)
            self.assertIn("[REDACTED]", launch_text)

            output_path = directory / "redacted-output.log"
            asyncio.run(app._tee(io.StringIO(f"token={token}\n"), output_path, "STDOUT"))
            output = output_path.read_text(encoding="utf-8")
            self.assertNotIn(token, output)
            self.assertIn("[REDACTED]", output)

            app._clear_launch_token()
            self.assertNotIn(token, app.cmd_start)
            self.assertIsNone(app._launch_token)

            app.set_steam_game_server_login_token(None)
            self.assertFalse(app.steam_game_server_login_token_status.configured)
            self.assertFalse(token_path.exists())

    def test_legacy_server_config_redacts_quoted_inline_token_without_hiding_commands(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(
                f'hostname "Foo"; sv_setsteamaccount "{token}"; sv_lan 0\n',
                encoding="utf-8",
            )

            displayed = app.read_config_file("server/server.cfg")

        self.assertEqual(
            displayed.content,
            'hostname "Foo"; sv_setsteamaccount "[REDACTED]"; sv_lan 0\n',
        )
        self.assertNotIn(token, displayed.content)
        self.assertIsNotNone(displayed.warning)

    def test_legacy_server_config_download_is_redacted(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(
                f'hostname "Foo"; sv_setsteamaccount {token}; sv_lan 0\n',
                encoding="utf-8",
            )

            response = asyncio.run(
                NodeApiService().storage.build_config_root_download_response(app=app, root_id="server")
            )

        downloaded_content = bytes(response.body).decode("utf-8")
        self.assertEqual(
            downloaded_content,
            'hostname "Foo"; sv_setsteamaccount [REDACTED]; sv_lan 0\n',
        )
        self.assertNotIn(token, downloaded_content)
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="server.cfg"',
        )

    def test_steam_updater_uses_gmod_dedicated_server_app_id_and_verify_flow(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            self.assertIsInstance(app.updater, SteamCmd_Update_Manager)
            assert isinstance(app.updater, SteamCmd_Update_Manager)
            self.assertTrue(app.updater.supports_verify)
            steam_update = app.cfg.steam_update
            assert steam_update is not None
            command = app.updater._steamcmd_command(
                branch=steam_update.selected_branch_config,
                validate=True,
            )

        self.assertEqual(STEAM_APP_ID, 4020)
        self.assertEqual(steam_update.login.username, "anonymous")
        self.assertEqual(steam_update.selected_branch, "public")
        self.assertEqual(
            command,
            [
                "steamcmd",
                "+force_install_dir",
                str(directory),
                "+login",
                "anonymous",
                "+app_update",
                "4020",
                "-beta",
                "public",
                "validate",
                "+quit",
            ],
        )

    def test_launch_failure_does_not_surface_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))
            app.set_steam_game_server_login_token(token)

            async def _start() -> RuntimeError:
                with (
                    patch.object(app, "_std_launch", new=AsyncMock(side_effect=RuntimeError(token))),
                    patch.object(app, "_drain_stderr_task", new=AsyncMock()),
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        await app.start()
                return raised.exception

            error = asyncio.run(_start())

        self.assertEqual(str(error), "Garry's Mod could not be launched.")
        self.assertNotIn(token, app.cmd_start)
        self.assertIsNone(app._launch_token)

    def test_process_detection_only_matches_this_instance_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            expected = tuple(part.casefold() for part in app.proc_cmd)

            matches = app._matches_leftover_process(
                command_line=("srcds_linux", "-game", "garrysmod"),
                expected_command_parts=expected,
                process_cwd=str(directory),
            )
            different_directory = app._matches_leftover_process(
                command_line=("srcds_linux", "-game", "garrysmod"),
                expected_command_parts=expected,
                process_cwd=str(directory / "other"),
            )

        self.assertTrue(matches)
        self.assertFalse(different_directory)

    def test_leftover_process_cleanup_does_not_log_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _Process:
            def __init__(self, directory: Path) -> None:
                self.info = {
                    "name": "srcds_linux",
                    "pid": 123,
                    "cmdline": ["srcds_linux", "-game", "garrysmod", "+sv_setsteamaccount", token],
                    "cwd": str(directory),
                }
                self.terminate_calls = 0

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: float) -> None:
                del timeout

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = object.__new__(Gmod)
            app.proc_name = "srcds_linux"
            app.proc_cmd = ["srcds_linux", "-game", "garrysmod"]
            app.directory = directory
            process = _Process(directory)

            with patch("apps._app.psutil.process_iter", return_value=(process,)), patch(
                "apps._app.log.info"
            ) as info_log:
                app._terminate_leftover_processes_sync()

        self.assertEqual(process.terminate_calls, 1)
        self.assertNotIn(token, str(info_log.call_args_list))

    def test_stored_process_cleanup_does_not_log_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _Process:
            pid = 123

            @staticmethod
            def wait(timeout: float) -> None:
                del timeout
                raise RuntimeError(f"GMod process failure: {token}")

            @staticmethod
            def poll() -> int:
                return 0

        app = object.__new__(Gmod)
        app.name = "gmod_alpha"
        app.process = cast(subprocess.Popen[str], cast(object, _Process()))
        app.proc_name = ""
        app._stderr_task = None

        with (
            patch("apps._app.os.killpg") as kill_process_group,
            patch("apps._app.log.warning") as warning_log,
            patch("apps._app.log.exception") as exception_log,
        ):
            asyncio.run(app._terminate())

        kill_process_group.assert_called_once()
        self.assertIsNone(app.process)
        self.assertNotIn(token, str(warning_log.call_args_list))
        self.assertNotIn(token, str(exception_log.call_args_list))

    def test_stdout_drain_cancels_a_stalled_task(self) -> None:
        async def _drain() -> bool:
            app = object.__new__(Gmod)
            app.name = "gmod_alpha"
            task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))
            app._stdout_task = task
            await app._drain_stdout_task(timeout_seconds=0.05)
            return task.cancelled()

        self.assertTrue(asyncio.run(_drain()))


class GmodInstallerTests(unittest.TestCase):
    def test_steam_install_recipe_requires_and_redacts_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        manager = object.__new__(App_Manager)
        original_directory = Path.cwd()
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            scope_path = directory / "apps" / "gmod"
            scope_path.mkdir(parents=True)
            (scope_path / "__init__.py").write_text("", encoding="utf-8")
            os.chdir(directory)
            try:
                recipes = manager.list_steam_install_recipes()
                self.assertEqual(len(recipes), 1)
                recipe = recipes[0]
                self.assertEqual(recipe.scope, "gmod")
                self.assertEqual(recipe.label, "Garry's Mod")
                self.assertEqual(recipe.default_port, GMOD_DEFAULT_PORT)
                self.assertEqual(recipe.steam_update.app_id, STEAM_APP_ID)
                self.assertEqual(recipe.inputs, (AppInstallInput.GAME_SERVER_LOGIN_TOKEN,))
                self.assertEqual(recipe.game_server_login_token_app_id, STEAM_GAME_APP_ID)
                self.assertNotEqual(recipe.game_server_login_token_app_id, recipe.steam_update.app_id)
                self.assertIsNotNone(recipe.post_steam_install)

                catalog_recipe = NodeAppInstallerService._catalog_recipe(recipe)
                install_field = catalog_recipe.fields[0]
                self.assertEqual(install_field.key, AppInstallInput.GAME_SERVER_LOGIN_TOKEN.value)
                self.assertEqual(install_field.kind, NodeAppInstallInputKind.PASSWORD)
                self.assertEqual(install_field.game_server_login_token_app_id, STEAM_GAME_APP_ID)
                self.assertEqual(install_field.action_label, "Manage tokens on Steam")
                self.assertEqual(install_field.action_url, STEAM_GAME_SERVER_LOGIN_TOKEN_MANAGEMENT_URL)
                self.assertNotIn(token, str(catalog_recipe.to_mapping()))

                request = NodeAppInstallRequest(
                    scope="gmod",
                    instance_key="alpha",
                    friendly_name="GMod Alpha",
                    subfolder="gmod-alpha",
                    port=27031,
                    steam_branch_id="public",
                    inputs={AppInstallInput.GAME_SERVER_LOGIN_TOKEN: token},
                )
                self.assertNotIn(token, repr(request))
                create_request = NodeAppInstallerService._create_request(request)
                self.assertEqual(create_request.steam_game_server_login_token, token)
                self.assertNotIn(token, repr(create_request))
                with self.assertRaisesRegex(ValueError, "Login Token is required"):
                    manager.prepare_instance_creation(replace(create_request, steam_game_server_login_token=None))
                redacted_detail = NodeAppInstallerService._redact_install_detail(
                    detail=f"SteamCMD printed {token}",
                    steam_update=recipe.steam_update,
                    secret_values=(token,),
                )
                self.assertNotIn(token, redacted_detail)

                staging_directory = directory / "staging"
                staging_directory.mkdir()
                (staging_directory / "srcds_run").write_text("#!/bin/sh\n", encoding="utf-8")
                post_processor = recipe.post_steam_install
                assert post_processor is not None
                asyncio.run(post_processor(staging_directory, create_request))
                self.assertEqual(_read_gmod_game_server_login_token(staging_directory), token)
                self.assertTrue(gmod_server_config_path(staging_directory).is_file())

                manager.create_instance(create_request)
                instance_payload = json.loads((scope_path / "instances.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(original_directory)

        self.assertEqual(instance_payload["alpha"]["join_port"], 27031)
        self.assertEqual(instance_payload["alpha"]["steam_update"]["app_id"], STEAM_APP_ID)
        self.assertNotIn(token, json.dumps(instance_payload))
