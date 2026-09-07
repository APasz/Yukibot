import asyncio
import signal
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import AsyncMock, patch

from apps._config import AppVersion
from apps.ets import (
    ETS,
    ETS_DEFAULT_CONNECTION_PORT,
    ETS_Settings,
    STEAM_GAME_APP_ID,
    configure_ets_server_ports,
    detect_ets_version,
    ets_launch_environment,
    ets_server_config_path,
    ets_server_package_paths,
    missing_ets_server_package_names,
    prepare_ets_server_installation,
    resolve_ets_connection_port,
)


class ETSSettingsTests(unittest.TestCase):
    def test_server_config_settings_load_and_save_all_supported_values(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text(
                "\n".join(
                    (
                        "SiiNunit",
                        "{",
                        "server_config : Server {",
                        ' lobby_name: "Old // Lobby" // session name',
                        ' description: "Original description"',
                        ' welcome_message: "Welcome to \\"ETS\\""',
                        ' password: "secret"',
                        " max_players: 8",
                        " max_vehicles_total: 100",
                        " max_ai_vehicles_player: 50",
                        " max_ai_vehicles_player_spawn: 45",
                        " player_damage: true",
                        " traffic: false # preserve this comment",
                        " hide_in_company: true",
                        " hide_colliding: false",
                        " force_speed_limiter: true",
                        " mods_optioning: true",
                        " service_no_collision: true",
                        " in_menu_ghosting: true",
                        " name_tags: false",
                        "}",
                        "}",
                    )
                ),
                encoding="utf-8",
            )

            settings = ETS_Settings(config_path)
            settings_by_key = {setting.key: setting for setting in settings.options}

            self.assertSetEqual(
                set(settings_by_key),
                {
                    "lobby_name",
                    "description",
                    "welcome_message",
                    "password",
                    "max_players",
                    "max_vehicles_total",
                    "max_ai_vehicles_player",
                    "max_ai_vehicles_player_spawn",
                    "player_damage",
                    "traffic",
                    "hide_in_company",
                    "hide_colliding",
                    "force_speed_limiter",
                    "mods_optioning",
                    "service_no_collision",
                    "in_menu_ghosting",
                    "name_tags",
                },
            )
            self.assertEqual(settings_by_key["lobby_name"].value, "Old // Lobby")
            self.assertEqual(settings_by_key["description"].value, "Original description")
            self.assertEqual(settings_by_key["welcome_message"].value, 'Welcome to "ETS"')
            self.assertEqual(settings_by_key["password"].value, "secret")
            self.assertEqual(settings_by_key["max_players"].value, 8)
            self.assertEqual(settings_by_key["max_vehicles_total"].value, 100)
            self.assertEqual(settings_by_key["max_ai_vehicles_player"].value, 50)
            self.assertEqual(settings_by_key["max_ai_vehicles_player_spawn"].value, 45)
            self.assertTrue(settings_by_key["player_damage"].value)
            self.assertFalse(settings_by_key["traffic"].value)
            self.assertTrue(settings_by_key["hide_in_company"].value)
            self.assertFalse(settings_by_key["hide_colliding"].value)
            self.assertTrue(settings_by_key["force_speed_limiter"].value)
            self.assertTrue(settings_by_key["mods_optioning"].value)
            self.assertTrue(settings_by_key["service_no_collision"].value)
            self.assertTrue(settings_by_key["in_menu_ghosting"].value)
            self.assertFalse(settings_by_key["name_tags"].value)

            settings_by_key["lobby_name"].update('New "Road" // Lobby')
            settings_by_key["max_ai_vehicles_player"].update("51")
            settings_by_key["max_ai_vehicles_player_spawn"].update("52")
            settings_by_key["traffic"].update("true")
            settings.save()

            saved = config_path.read_text(encoding="utf-8")

        self.assertIn('lobby_name: "New \\"Road\\" // Lobby" // session name', saved)
        self.assertIn("max_ai_vehicles_player: 51", saved)
        self.assertIn("max_ai_vehicles_player_spawn: 52", saved)
        self.assertIn("traffic: true # preserve this comment", saved)

    def test_server_config_settings_enforce_documented_limits(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text('lobby_name: "Lobby"\n', encoding="utf-8")
            settings = ETS_Settings(config_path)
            settings_by_key = {setting.key: setting for setting in settings.options}

            with self.assertRaisesRegex(ValueError, "63 characters"):
                settings_by_key["lobby_name"].update("x" * 64)
            with self.assertRaisesRegex(ValueError, "127 characters"):
                settings_by_key["welcome_message"].update("x" * 128)
            with self.assertRaisesRegex(ValueError, "at most 8"):
                settings_by_key["max_players"].update("9")
            with self.assertRaisesRegex(ValueError, "at least 1"):
                settings_by_key["max_players"].update("0")

    def test_server_config_settings_accept_legacy_bare_text_and_canonicalise_it_on_save(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text(
                "lobby_name: Unquoted Lobby\npassword: \n",
                encoding="utf-8",
            )

            settings = ETS_Settings(config_path)
            settings_by_key = {setting.key: setting for setting in settings.options}

            self.assertEqual(settings_by_key["lobby_name"].value, "Unquoted Lobby")
            self.assertEqual(settings_by_key["password"].value, "")
            settings.save()
            saved = config_path.read_text(encoding="utf-8")

        self.assertIn('lobby_name: "Unquoted Lobby"', saved)
        self.assertIn('password: ""', saved)

    def test_server_config_settings_use_declared_defaults_for_missing_optional_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text("traffic: false\n", encoding="utf-8")

            settings = ETS_Settings(config_path)
            settings_by_key = {setting.key: setting for setting in settings.options}

        self.assertEqual(settings_by_key["lobby_name"].value, "")
        self.assertEqual(settings_by_key["max_players"].value, 8)
        self.assertFalse(settings_by_key["traffic"].value)

    def test_server_config_settings_reject_malformed_quoted_text_and_duplicate_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text('lobby_name: "Unterminated\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "quoted text value"):
                ETS_Settings(config_path)

            config_path.write_text("traffic: true\ntraffic: false\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "multiple traffic entries"):
                ETS_Settings(config_path)

            config_path.write_text("/*\ntraffic: false\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unterminated block comment"):
                ETS_Settings(config_path)

    def test_server_config_settings_ignore_block_comments_and_preserve_inline_comments(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text(
                "\n".join(
                    (
                        "/*",
                        " traffic: false",
                        "*/",
                        "traffic: true /* current value */",
                    )
                ),
                encoding="utf-8",
            )
            settings = ETS_Settings(config_path)
            traffic = next(setting for setting in settings.options if setting.key == "traffic")

            self.assertTrue(traffic.value)
            traffic.update("false")
            settings.save()
            saved = config_path.read_text(encoding="utf-8")

        self.assertIn(" traffic: false\n", saved)
        self.assertIn("traffic: false /* current value */", saved)


class ETSGameServerLoginTokenTests(unittest.TestCase):
    @staticmethod
    def _app(directory: Path) -> ETS:
        app = object.__new__(ETS)
        app.directory = directory
        return app

    def test_login_token_status_uses_the_ets2_game_app_id_without_exposing_the_token(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                f"server_logon_token: {token} // persistent server account\n",
                encoding="utf-8",
            )

            status = self._app(root).steam_game_server_login_token_status

        self.assertEqual(status.game_app_id, STEAM_GAME_APP_ID)
        self.assertEqual(STEAM_GAME_APP_ID, 227300)
        self.assertTrue(status.configured)
        self.assertNotIn(token, repr(status))
        self.assertNotIn(token, str(status.to_mapping()))

    def test_login_token_set_and_clear_preserve_comments_and_use_the_documented_sii_form(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "\n".join(
                    (
                        "/* server_logon_token: ignored-token */",
                        'server_logon_token: "" // persistent server account',
                    )
                ),
                encoding="utf-8",
            )
            app = self._app(root)

            self.assertFalse(app.steam_game_server_login_token_status.configured)
            app.set_steam_game_server_login_token(f"  {token}  ")
            configured = config_path.read_text(encoding="utf-8")
            self.assertTrue(app.steam_game_server_login_token_status.configured)

            app.set_steam_game_server_login_token(None)
            cleared = config_path.read_text(encoding="utf-8")
            self.assertFalse(app.steam_game_server_login_token_status.configured)

        self.assertIn("/* server_logon_token: ignored-token */", configured)
        self.assertIn(f"server_logon_token: {token} // persistent server account", configured)
        self.assertIn('server_logon_token: "" // persistent server account', cleared)

    def test_login_token_quotes_values_that_could_be_read_as_sii_comments(self) -> None:
        token = "A0B1#C2D3"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text('server_logon_token: ""\n', encoding="utf-8")
            app = self._app(root)

            app.set_steam_game_server_login_token(token)
            saved = config_path.read_text(encoding="utf-8")
            self.assertTrue(app.steam_game_server_login_token_status.configured)

        self.assertIn(f'server_logon_token: "{token}"', saved)
        self.assertNotIn(f"server_logon_token: {token}\n", saved)

    def test_login_token_duplicate_config_error_does_not_leak_stored_tokens(self) -> None:
        stored_token = "LEAKEDTOKEN"
        replacement_token = "REPLACEMENTTOKEN"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "\n".join(
                    (
                        f"server_logon_token: {stored_token}",
                        "server_logon_token: OTHERSTOREDTOKEN",
                    )
                ),
                encoding="utf-8",
            )
            app = self._app(root)

            with self.assertRaises(ValueError) as raised:
                app.set_steam_game_server_login_token(replacement_token)

        self.assertIn("multiple server_logon_token entries", str(raised.exception))
        self.assertNotIn(stored_token, str(raised.exception))
        self.assertNotIn(replacement_token, str(raised.exception))

    def test_login_token_can_replace_a_malformed_stored_value(self) -> None:
        replacement_token = "REPLACEMENTTOKEN"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text('server_logon_token: "unterminated\n', encoding="utf-8")
            app = self._app(root)

            self.assertTrue(app.steam_game_server_login_token_status.configured)
            app.set_steam_game_server_login_token(replacement_token)
            saved = config_path.read_text(encoding="utf-8")

        self.assertEqual(saved, f"server_logon_token: {replacement_token}\n")


class ETSVersionDetectionTests(unittest.TestCase):
    def test_detect_ets_version_prefers_game_version_line(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "home_data" / "Euro Truck Simulator 2"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "server.log.txt"
            log_path.write_text(
                "\n".join(
                    (
                        "00:00:00.002 : [ufs] Loaded pack set version 1.55.0.3 created at 1749652225",
                        "00:00:02.039 : [MP] Game version: 1.55s",
                    )
                ),
                encoding="utf-8",
            )

            version = detect_ets_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="1.55s"))

    def test_detect_ets_version_falls_back_to_pack_set_version(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "home_data" / "Euro Truck Simulator 2"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "server.log.txt"
            log_path.write_text(
                "00:00:00.002 : [ufs] Loaded pack set version 1.55.0.3 created at 1749652225\n",
                encoding="utf-8",
            )

            version = detect_ets_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="1.55.0.3"))

    def test_ets_launch_environment_uses_an_isolated_data_home(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            environment = ets_launch_environment(root)

        self.assertEqual(environment, {"XDG_DATA_HOME": str(root / "home_data")})

    def test_connection_port_uses_the_default_and_reserves_the_query_port(self) -> None:
        self.assertEqual(resolve_ets_connection_port(None), ETS_DEFAULT_CONNECTION_PORT)
        self.assertEqual(resolve_ets_connection_port(31000), 31000)
        with self.assertRaisesRegex(TypeError, "integer"):
            resolve_ets_connection_port(True)
        with self.assertRaisesRegex(TypeError, "integer"):
            resolve_ets_connection_port(cast(int, "31000"))
        with self.assertRaisesRegex(ValueError, "65534"):
            resolve_ets_connection_port(65535)

    def test_prepare_existing_server_config_patches_the_connection_and_query_ports(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "\n".join(
                    (
                        "SiiNunit",
                        "{",
                        "server_config : Server {",
                        " connection_dedicated_port: 27015 // game-server port",
                        " query_dedicated_port: 27016 // query-server port",
                        "}",
                        "}",
                    )
                ),
                encoding="utf-8",
            )

            async def _prepare() -> None:
                with patch("apps.ets.asyncio.create_subprocess_exec", new=AsyncMock()) as launch:
                    await prepare_ets_server_installation(directory=root, connection_port=31000)
                launch.assert_not_awaited()

            asyncio.run(_prepare())

            data = config_path.read_text(encoding="utf-8")

        self.assertIn("connection_dedicated_port: 31000 // game-server port", data)
        self.assertIn("query_dedicated_port: 31001 // query-server port", data)

    def test_prepare_fresh_server_config_launches_with_the_isolated_data_home(self) -> None:
        class _InitialisationProcess:
            def __init__(self) -> None:
                self.pid = 4321
                self.returncode: int | None = None

            async def wait(self) -> int:
                self.returncode = 0
                return self.returncode

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            launch_directory = root / "bin" / "linux_x64"
            launch_directory.mkdir(parents=True)
            (launch_directory / "server_launch.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            config_path = ets_server_config_path(root)
            process = _InitialisationProcess()

            async def _launch(*command: str, **kwargs: object) -> _InitialisationProcess:
                self.assertEqual(command, ("bash", "server_launch.sh"))
                self.assertEqual(kwargs["cwd"], str(launch_directory))
                environment_raw = kwargs["env"]
                self.assertIsInstance(environment_raw, dict)
                environment = cast(dict[str, str], environment_raw)
                self.assertEqual(environment["XDG_DATA_HOME"], str(root / "home_data"))
                self.assertTrue(kwargs["start_new_session"])
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    "connection_dedicated_port: 27015\nquery_dedicated_port: 27016\n",
                    encoding="utf-8",
                )
                return process

            async def _prepare() -> None:
                with (
                    patch("apps.ets.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_launch)),
                    patch("apps.ets.os.killpg") as killpg,
                ):
                    await prepare_ets_server_installation(directory=root, connection_port=32000)
                killpg.assert_called_once_with(process.pid, signal.SIGTERM)

            asyncio.run(_prepare())
            data = config_path.read_text(encoding="utf-8")

        self.assertIn("connection_dedicated_port: 32000", data)
        self.assertIn("query_dedicated_port: 32001", data)

    def test_prepare_fresh_server_config_stops_child_processes_after_the_launcher_exits(self) -> None:
        class _InitialisationProcess:
            pid = 4321
            returncode = 0

            async def wait(self) -> int:
                return self.returncode

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            launch_directory = root / "bin" / "linux_x64"
            launch_directory.mkdir(parents=True)
            (launch_directory / "server_launch.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            config_path = ets_server_config_path(root)

            async def _launch(*command: str, **kwargs: object) -> _InitialisationProcess:
                del command, kwargs
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    "connection_dedicated_port: 27015\nquery_dedicated_port: 27016\n",
                    encoding="utf-8",
                )
                return _InitialisationProcess()

            async def _prepare() -> None:
                with (
                    patch("apps.ets.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_launch)),
                    patch("apps.ets.os.killpg") as killpg,
                ):
                    await prepare_ets_server_installation(directory=root, connection_port=None)
                killpg.assert_called_once_with(4321, signal.SIGTERM)

            asyncio.run(_prepare())

    def test_configure_ports_rejects_missing_dedicated_port_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text("connection_dedicated_port: 27015\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "query_dedicated_port"):
                configure_ets_server_ports(config_path=config_path, connection_port=27015)

    def test_configure_ports_ignores_block_comments_and_preserves_inline_comments(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "server_config.sii"
            config_path.write_text(
                "\n".join(
                    (
                        "/*",
                        " connection_dedicated_port: 27015",
                        " query_dedicated_port: 27016",
                        "*/",
                        "connection_dedicated_port: 27015 /* game server */",
                        "query_dedicated_port: 27016 /* query server */",
                    )
                ),
                encoding="utf-8",
            )

            configure_ets_server_ports(config_path=config_path, connection_port=31000)
            saved = config_path.read_text(encoding="utf-8")

        self.assertIn(" connection_dedicated_port: 27015\n", saved)
        self.assertIn(" query_dedicated_port: 27016\n", saved)
        self.assertIn("connection_dedicated_port: 31000 /* game server */", saved)
        self.assertIn("query_dedicated_port: 31001 /* query server */", saved)

    def test_ets_termination_targets_its_process_group(self) -> None:
        class _Process:
            def __init__(self) -> None:
                self.pid = 4321
                self.returncode: int | None = None
                self.terminate_calls = 0

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.returncode = 0
                return self.returncode

            def poll(self) -> int | None:
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        with TemporaryDirectory() as tmp:
            app = object.__new__(ETS)
            app.name = "ets_alpha"
            app.proc_name = "eurotrucks2_server"
            app.proc_cmd = [app.proc_name]
            app.cmd_cwd = Path(tmp) / "bin" / "linux_x64"
            process = _Process()
            app.process = cast(subprocess.Popen[str], cast(object, process))
            app._stderr_task = None

            async def _terminate() -> None:
                with (
                    patch("apps._app.os.killpg") as killpg,
                    patch("apps._app.psutil.process_iter", return_value=()),
                ):
                    await app._terminate()
                killpg.assert_called_once_with(4321, signal.SIGTERM)

            asyncio.run(_terminate())

        self.assertIsNone(app.process)
        self.assertEqual(process.terminate_calls, 0)

    def test_ets_leftover_cleanup_is_scoped_to_its_launch_directory(self) -> None:
        class _Process:
            def __init__(self, *, pid: int, cwd: Path) -> None:
                self.info = {
                    "name": "eurotrucks2_server",
                    "pid": pid,
                    "cmdline": ["./eurotrucks2_server"],
                    "cwd": str(cwd),
                }
                self.terminate_calls = 0

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: float) -> None:
                del timeout

            def kill(self) -> None:
                raise AssertionError("Unexpected process kill")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            own_launch_directory = root / "alpha" / "bin" / "linux_x64"
            other_launch_directory = root / "bravo" / "bin" / "linux_x64"
            own_process = _Process(pid=123, cwd=own_launch_directory)
            other_process = _Process(pid=456, cwd=other_launch_directory)
            app = object.__new__(ETS)
            app.proc_name = "eurotrucks2_server"
            app.proc_cmd = [app.proc_name]
            app.cmd_cwd = own_launch_directory

            with patch("apps._app.psutil.process_iter", return_value=(own_process, other_process)):
                app._terminate_leftover_processes_sync()

        self.assertEqual(own_process.terminate_calls, 1)
        self.assertEqual(other_process.terminate_calls, 0)

    def test_package_upload_replaces_only_the_matching_server_package(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "exported.sii"
            source_path.write_bytes(b"package configuration")
            app = object.__new__(ETS)
            app.directory = root
            app.process = None

            uploaded = app.upload_save_file(
                root_id="server-packages-config",
                upload_name="server_packages.sii",
                source_path=source_path,
            )
            package_config_path, package_data_path = ets_server_package_paths(root)

            self.assertEqual(uploaded.id, "server-packages-config/server_packages.sii")
            self.assertEqual(package_config_path.read_bytes(), b"package configuration")
            self.assertFalse(package_data_path.exists())
            self.assertEqual(missing_ets_server_package_names(root), ("server_packages.dat",))
            with self.assertRaisesRegex(ValueError, "server_packages.sii"):
                app.upload_save_file(
                    root_id="server-packages-config",
                    upload_name="wrong-name.sii",
                    source_path=source_path,
                )

    def test_server_package_upload_destination_is_inferred_from_its_filename(self) -> None:
        with TemporaryDirectory() as tmp:
            app = object.__new__(ETS)
            app.directory = Path(tmp)

            self.assertEqual(
                app.resolve_save_upload_root_id("server_packages.sii"),
                "server-packages-config",
            )
            self.assertEqual(
                app.resolve_save_upload_root_id("server_packages.dat"),
                "server-packages-data",
            )
            with self.assertRaisesRegex(ValueError, "must be named"):
                app.resolve_save_upload_root_id("other.sii")
            with self.assertRaisesRegex(ValueError, "directories"):
                app.resolve_save_upload_root_id("exports/server_packages.sii")

    def test_server_package_upload_validation_rejects_empty_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "empty.sii"
            source_path.touch()
            app = object.__new__(ETS)
            app.directory = root
            app.process = None

            with self.assertRaisesRegex(ValueError, "must not be empty"):
                app.validate_save_upload_file(
                    root_id="server-packages-config",
                    upload_name="server_packages.sii",
                    source_path=source_path,
                )

    def test_start_blocks_until_both_exported_server_packages_are_present(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ets_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text("server_config : Server {}\n", encoding="utf-8")
            app = object.__new__(ETS)
            app.directory = root
            app.process = None

            async def _start() -> None:
                with self.assertRaisesRegex(RuntimeError, "server_packages.sii, server_packages.dat"):
                    await app.start()

            asyncio.run(_start())


if __name__ == "__main__":
    unittest.main()
