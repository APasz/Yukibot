from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast

from apps._app import AppPortClaim, NetworkProtocol
from apps.ats import ATS, ats_server_config_path
from apps.beammp import BeamMP, BeamMPNetworkPorts, _beammp_network_ports, _beammp_server_port
from apps.factorio import Factorio
from apps.minecraft import Minecraft, MinecraftServerPropertiesSnapshot
from apps.satisfactory import Satisfactory
from apps.sevendays import SevenDays


class AppPortClaimTests(unittest.TestCase):
    def test_rejects_invalid_port_claim_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "protocol"):
            AppPortClaim(protocol=cast(NetworkProtocol, object()), port=25565, purpose="game server")
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            AppPortClaim(protocol=NetworkProtocol.TCP, port=0, purpose="game server")
        with self.assertRaisesRegex(ValueError, "must not be blank"):
            AppPortClaim(protocol=NetworkProtocol.TCP, port=25565, purpose=" ")

    def test_factorio_claims_its_game_and_rcon_ports(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.cfg = SimpleNamespace(join_port=34198)

        self.assertEqual(
            app.listening_port_claims,
            (
                AppPortClaim(protocol=NetworkProtocol.UDP, port=34198, purpose="game server"),
                AppPortClaim(protocol=NetworkProtocol.TCP, port=27015, purpose="RCON"),
            ),
        )

    def test_satisfactory_claims_its_game_and_reliable_messaging_ports(self) -> None:
        app = cast(Any, object.__new__(Satisfactory))
        app.cfg = SimpleNamespace(join_port=7788, reliable_messaging_port=8899)

        self.assertEqual(
            app.listening_port_claims,
            (
                AppPortClaim(protocol=NetworkProtocol.TCP, port=7788, purpose="game server and HTTPS API"),
                AppPortClaim(protocol=NetworkProtocol.UDP, port=7788, purpose="game server and HTTPS API"),
                AppPortClaim(protocol=NetworkProtocol.TCP, port=8899, purpose="reliable messaging"),
            ),
        )

    def test_scs_claims_connection_and_query_port_pairs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config_path = ats_server_config_path(directory)
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                "connection_dedicated_port: 32000\nquery_dedicated_port: 32002\n",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(ATS))
            app.directory = directory
            app.cfg = SimpleNamespace(join_port=32000)

            self.assertEqual(
                app.listening_port_claims,
                (
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=32000, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=32000, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=32002, purpose="server query"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=32002, purpose="server query"),
                ),
            )

    def test_beammp_claims_the_port_from_its_server_configuration(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config_path = directory / "ServerConfig.toml"
            config_path.write_text(
                "[General]\nPort = 30815\n\n[HTTP]\nHTTPServerEnabled = true\nHTTPServerPort = 18080\n",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(BeamMP))
            app.directory = directory
            app.cfg = SimpleNamespace(join_port=30814)

            self.assertEqual(_beammp_server_port(config_path, fallback=30814), 30815)
            self.assertEqual(
                app.launch_environment(),
                {
                    "BEAMMP_PORT": "30815",
                    "BEAMMP_PROVIDER_PORT_ENV": "",
                    "BEAMMP_PROVIDER_DISABLE_CONFIG": "0",
                },
            )
            self.assertEqual(
                app.listening_port_claims,
                (
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=30815, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=30815, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=18080, purpose="HTTP server"),
                ),
            )

    def test_beammp_reads_an_http_listener_without_a_general_table(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ServerConfig.toml"
            config_path.write_text(
                "[HTTP]\nHTTPServerEnabled = true\nHTTPServerPort = 18080\n",
                encoding="utf-8",
            )

            self.assertEqual(
                _beammp_network_ports(config_path, fallback=None),
                BeamMPNetworkPorts(game_port=30814, http_port=18080),
            )

    def test_beammp_rejects_a_non_file_server_configuration(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ServerConfig.toml"
            config_path.mkdir()

            with self.assertRaisesRegex(ValueError, "server config is not a file"):
                _beammp_network_ports(config_path, fallback=30814)

    def test_minecraft_claims_configured_game_query_rcon_and_squaremap_ports(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "server.properties").write_text(
                "server-port=25566\n"
                "enable-query=true\n"
                "query.port=25567\n"
                "enable-rcon=true\n"
                "rcon.port=25576\n"
                "management-server-enabled=true\n"
                "management-server-port=25577\n",
                encoding="utf-8",
            )
            squaremap_config = directory / "config" / "squaremap" / "config.yml"
            squaremap_config.parent.mkdir(parents=True)
            squaremap_config.write_text(
                "settings:\n  internal-webserver:\n    enabled: true\n    port: 18080\n",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(Minecraft))
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: (SimpleNamespace(name="squaremap-forge-mc1.20.1-1.2.0.jar"),)
            )
            app._server_properties = MinecraftServerPropertiesSnapshot(
                enable_rcon=False,
                rcon_port=None,
                rcon_password=None,
                max_players=None,
                server_port=25565,
            )

            self.assertEqual(
                app.listening_port_claims,
                (
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=25566, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=25567, purpose="server query"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=25576, purpose="RCON"),
                    AppPortClaim(
                        protocol=NetworkProtocol.TCP,
                        port=25577,
                        purpose="Minecraft Server Management Protocol",
                    ),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=18080, purpose="Squaremap web server"),
                ),
            )

    def test_minecraft_requires_a_fixed_management_server_port(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "server.properties").write_text(
                "management-server-enabled=true\nmanagement-server-port=0\n",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(Minecraft))
            app.directory = directory
            app.mods = SimpleNamespace(list_mods=lambda state=None: ())

            with self.assertRaisesRegex(ValueError, "requires a fixed management-server-port"):
                _ = app.listening_port_claims

    def test_sevendays_claims_game_and_telnet_ports(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "serverconfig.xml").write_text(
                '<ServerSettings><property name="TelnetEnabled" value="true" />'
                '<property name="TelnetPort" value="18081" />'
                '<property name="WebDashboardEnabled" value="true" />'
                '<property name="WebDashboardPort" value="18080" /></ServerSettings>',
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = directory
            app.cfg = SimpleNamespace(join_port=26901)

            self.assertEqual(
                app.listening_port_claims,
                (
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=26901, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26901, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26902, purpose="game networking"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26903, purpose="game networking"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26904, purpose="game networking"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=18081, purpose="Telnet"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=18080, purpose="web dashboard"),
                ),
            )

    def test_sevendays_claims_legacy_control_panel_port(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "serverconfig.xml").write_text(
                '<ServerSettings><property name="TelnetEnabled" value="true" />'
                '<property name="ControlPanelEnabled" value="true" />'
                '<property name="ControlPanelPort" value="18080" /></ServerSettings>',
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = directory
            app.cfg = SimpleNamespace(join_port=26900)

            self.assertEqual(
                app.listening_port_claims,
                (
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=26900, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26900, purpose="game server"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26901, purpose="game networking"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26902, purpose="game networking"),
                    AppPortClaim(protocol=NetworkProtocol.UDP, port=26903, purpose="game networking"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=8081, purpose="Telnet"),
                    AppPortClaim(protocol=NetworkProtocol.TCP, port=18080, purpose="web dashboard"),
                ),
            )

    def test_sevendays_reuses_last_known_server_config_while_file_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            serverconfig_path = directory / "serverconfig.xml"
            userdata_path = directory / "userdata"
            save_path = userdata_path / "Saves" / "Navezgane" / "Alpha"
            save_path.mkdir(parents=True)

            def write_serverconfig(*, telnet_port: int, user_data_path: Path) -> None:
                serverconfig_path.write_text(
                    "<ServerSettings>"
                    '<property name="TelnetEnabled" value="true" />'
                    f'<property name="TelnetPort" value="{telnet_port}" />'
                    f'<property name="UserDataFolder" value="{user_data_path}" />'
                    "</ServerSettings>",
                    encoding="utf-8",
                )

            write_serverconfig(telnet_port=18081, user_data_path=userdata_path)
            app = cast(Any, object.__new__(SevenDays))
            app.directory = directory
            app.cfg = SimpleNamespace(join_port=26901)

            known_claims = app.listening_port_claims
            known_save_roots = app.save_file_roots
            app.updater = SimpleNamespace(status=lambda: SimpleNamespace(running=True))
            serverconfig_path.unlink()

            self.assertEqual(app.listening_port_claims, known_claims)
            self.assertEqual(app.save_file_roots, known_save_roots)
            self.assertTrue(app.supports_save_uploads)

            app.updater = None
            with self.assertRaises(FileNotFoundError):
                _ = app.listening_port_claims
            with self.assertRaises(FileNotFoundError):
                _ = app.save_file_roots

            restored_userdata_path = directory / "restored-userdata"
            write_serverconfig(telnet_port=18082, user_data_path=restored_userdata_path)

            self.assertIn(
                AppPortClaim(protocol=NetworkProtocol.TCP, port=18082, purpose="Telnet"),
                app.listening_port_claims,
            )
            self.assertEqual(app._userdata_root_path(), restored_userdata_path)

    def test_sevendays_requires_server_config_before_first_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = cast(Any, object.__new__(SevenDays))
            app.directory = Path(temp_dir)
            app.cfg = SimpleNamespace(join_port=26901)

            with self.assertRaises(FileNotFoundError):
                _ = app.listening_port_claims


if __name__ == "__main__":
    unittest.main()
