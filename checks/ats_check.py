import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._config import AppVersion
from apps.ats import (
    ATS,
    ATS_DEFAULT_CONNECTION_PORT,
    ATS_Settings,
    STEAM_APP_ID,
    STEAM_GAME_APP_ID,
    STEAM_UPDATE_PRESET,
    ats_server_config_path,
    ats_server_log_path,
    ats_server_package_paths,
    configure_ats_server_ports,
    detect_ats_version,
    missing_ats_server_package_names,
    resolve_ats_connection_port,
)


class AmericanTruckSimulatorTests(unittest.TestCase):
    @staticmethod
    def _app(directory: Path) -> ATS:
        app = object.__new__(ATS)
        app.directory = directory
        app.process = None
        return app

    def test_profile_uses_ats_paths_process_and_steam_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            self.assertEqual(ats_server_config_path(root), root / "home_data" / "American Truck Simulator" / "server_config.sii")
            self.assertEqual(ats_server_log_path(root), root / "home_data" / "American Truck Simulator" / "server.log.txt")
            self.assertEqual(
                ats_server_package_paths(root),
                (
                    root / "home_data" / "American Truck Simulator" / "server_packages.sii",
                    root / "home_data" / "American Truck Simulator" / "server_packages.dat",
                ),
            )

        self.assertEqual(ATS.profile.process_name, "amtrucks_server")
        self.assertEqual(ATS_DEFAULT_CONNECTION_PORT, 27015)
        self.assertEqual(STEAM_GAME_APP_ID, 270880)
        self.assertEqual(STEAM_APP_ID, 2239530)
        self.assertEqual(STEAM_UPDATE_PRESET.app_id, STEAM_APP_ID)

    def test_login_token_status_uses_the_ats_game_app_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ats_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text("server_logon_token: TOKEN\n", encoding="utf-8")

            status = self._app(root).steam_game_server_login_token_status

        self.assertEqual(status.game_app_id, 270880)
        self.assertTrue(status.configured)

    def test_settings_and_package_uploads_use_the_ats_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ats_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text('lobby_name: "Old Lobby"\nmax_players: 8\n', encoding="utf-8")
            settings = ATS_Settings(config_path)
            max_players = next(setting for setting in settings.options if setting.key == "max_players")
            source_path = root / "server_packages.sii"
            source_path.write_bytes(b"package config")
            app = self._app(root)

            settings.load()
            self.assertEqual(max_players.desc, "Maximum players in the session. ATS supports up to 8.")
            uploaded = app.upload_save_file(
                root_id="server-packages-config",
                upload_name="server_packages.sii",
                source_path=source_path,
            )

            self.assertEqual(uploaded.id, "server-packages-config/server_packages.sii")
            self.assertEqual(missing_ats_server_package_names(root), ("server_packages.dat",))
            with self.assertRaisesRegex(ValueError, "ATS server package uploads must be named"):
                app.resolve_save_upload_root_id("invalid.sii")

    def test_configuration_and_version_detection_use_the_ats_server_home(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ats_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "connection_dedicated_port: 27015\nquery_dedicated_port: 27016\n",
                encoding="utf-8",
            )
            configure_ats_server_ports(config_path=config_path, connection_port=32000)
            ats_server_log_path(root).write_text("[MP] Game version: 1.56s\n", encoding="utf-8")

            version = detect_ats_version(directory=root, server_log=None)
            saved_config = config_path.read_text(encoding="utf-8")

        self.assertIn("connection_dedicated_port: 32000", saved_config)
        self.assertEqual(version, AppVersion(main="1.56s"))

    def test_connection_port_validation_and_startup_message_identify_ats(self) -> None:
        self.assertEqual(resolve_ats_connection_port(None), 27015)
        with self.assertRaisesRegex(ValueError, "ATS connection port"):
            resolve_ats_connection_port(65535)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = ats_server_config_path(root)
            config_path.parent.mkdir(parents=True)
            config_path.write_text("server_config : Server {}\n", encoding="utf-8")
            app = self._app(root)

            async def _start() -> None:
                with self.assertRaisesRegex(RuntimeError, "ATS requires exported server packages"):
                    await app.start()

            asyncio.run(_start())


if __name__ == "__main__":
    unittest.main()
