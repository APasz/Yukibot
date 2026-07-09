from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from _security import Power_Level
from apps._config_files import (
    AppConfigFileKind,
    AppConfigFileRoot,
    list_app_config_files,
    read_app_config_file,
    resolve_app_config_path,
    write_app_config_file,
)
from apps.beammp import BeamMP
from apps.ets import ETS
from apps.factorio import Factorio
from apps.minecraft import Minecraft
from apps.sevendays import SevenDays


class ConfigFileTests(unittest.TestCase):
    def test_lists_reads_and_writes_allowed_config_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir) / "config"
            root_path.mkdir()
            config_path = root_path / "example.toml"
            config_path.write_text("enabled=true\n", encoding="utf-8")
            (root_path / "ignored.bin").write_bytes(b"\x00\x01")
            roots = (
                AppConfigFileRoot(
                    id="mods",
                    label="Mod Configs",
                    path=root_path,
                    kind=AppConfigFileKind.MOD,
                ),
            )

            files = list_app_config_files(
                roots,
                default_read_level=Power_Level.user,
                default_write_level=Power_Level.sudo,
            )
            loaded = read_app_config_file(
                roots,
                "mods/example.toml",
                default_read_level=Power_Level.user,
                default_write_level=Power_Level.sudo,
            )
            saved = write_app_config_file(
                roots,
                "mods/example.toml",
                "enabled=false\n",
                default_read_level=Power_Level.user,
                default_write_level=Power_Level.sudo,
            )

        self.assertEqual([file.id for file in files], ["mods/example.toml"])
        self.assertEqual(files[0].read_power_level, Power_Level.user)
        self.assertEqual(files[0].write_power_level, Power_Level.sudo)
        self.assertEqual(loaded.content, "enabled=true\n")
        self.assertEqual(saved.content, "enabled=false\n")

    def test_rejects_paths_that_escape_config_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir) / "config"
            root_path.mkdir()
            roots = (
                AppConfigFileRoot(
                    id="mods",
                    label="Mod Configs",
                    path=root_path,
                    kind=AppConfigFileKind.MOD,
                ),
            )

            with self.assertRaisesRegex(ValueError, "invalid segment"):
                resolve_app_config_path(roots, "mods/../server.properties")

    def test_list_skips_symlinks_that_escape_config_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            root_path = temp_path / "config"
            root_path.mkdir()
            outside = temp_path / "outside.toml"
            outside.write_text("secret=true\n", encoding="utf-8")
            (root_path / "outside.toml").symlink_to(outside)
            roots = (
                AppConfigFileRoot(
                    id="mods",
                    label="Mod Configs",
                    path=root_path,
                    kind=AppConfigFileKind.MOD,
                ),
            )

            files = list_app_config_files(
                roots,
                default_read_level=Power_Level.user,
                default_write_level=Power_Level.sudo,
            )

        self.assertEqual(files, ())

    def test_root_power_level_overrides_are_applied_to_file_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir) / "config"
            root_path.mkdir()
            config_path = root_path / "visitor.toml"
            config_path.write_text("enabled=true\n", encoding="utf-8")
            roots = (
                AppConfigFileRoot(
                    id="mods",
                    label="Mod Configs",
                    path=root_path,
                    kind=AppConfigFileKind.MOD,
                    read_power_level_override=Power_Level.visitor,
                    write_power_level_override=Power_Level.admin,
                ),
            )

            files = list_app_config_files(
                roots,
                default_read_level=Power_Level.user,
                default_write_level=Power_Level.sudo,
            )
            loaded = read_app_config_file(
                roots,
                "mods/visitor.toml",
                default_read_level=Power_Level.user,
                default_write_level=Power_Level.sudo,
            )

        self.assertEqual(files[0].read_power_level, Power_Level.visitor)
        self.assertEqual(files[0].write_power_level, Power_Level.admin)
        self.assertEqual(loaded.file.read_power_level, Power_Level.visitor)
        self.assertEqual(loaded.file.write_power_level, Power_Level.admin)

    def test_minecraft_exposes_server_properties_mod_config_and_world_serverconfig_roots(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = object.__new__(Minecraft)
            app.directory = Path(temp_dir)
            app._file_settings = Path(temp_dir) / "server.properties"
            app.settings = None

            roots = app.config_file_roots

        self.assertEqual([root.id for root in roots], ["server", "mod-configs", "server-config"])
        self.assertEqual(roots[0].kind, AppConfigFileKind.GAME)
        self.assertEqual(roots[1].kind, AppConfigFileKind.MOD)
        self.assertEqual(roots[2].kind, AppConfigFileKind.GAME)
        self.assertEqual(roots[2].path, Path(temp_dir) / "world" / "serverconfig")

    def test_other_apps_expose_primary_config_roots(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            beammp = object.__new__(BeamMP)
            beammp.directory = root / "beammp"
            ets = object.__new__(ETS)
            ets.directory = root / "ets"
            factorio = object.__new__(Factorio)
            factorio.directory = root / "factorio"
            sevendays = object.__new__(SevenDays)
            sevendays.directory = root / "sevendays"

            beammp_roots = beammp.config_file_roots
            ets_roots = ets.config_file_roots
            factorio_roots = factorio.config_file_roots
            sevendays_roots = sevendays.config_file_roots

        self.assertEqual([(root.id, root.path.name) for root in beammp_roots], [("server", "ServerConfig.toml")])
        self.assertEqual([(root.id, root.path.name) for root in ets_roots], [("server", "server_config.sii")])
        self.assertEqual(
            [(root.id, root.path.name) for root in factorio_roots],
            [
                ("server", "server-settings.json"),
                ("map-settings", "map-settings.json"),
                ("map-gen-settings", "map-gen-settings.json"),
            ],
        )
        self.assertIsNone(factorio_roots[0].write_power_level_override)
        self.assertIsNone(factorio_roots[0].read_power_level_override)
        self.assertEqual(factorio_roots[1].read_power_level_override, Power_Level.sudo)
        self.assertEqual(factorio_roots[1].write_power_level_override, Power_Level.sudo)
        self.assertEqual(factorio_roots[2].read_power_level_override, Power_Level.sudo)
        self.assertEqual(factorio_roots[2].write_power_level_override, Power_Level.sudo)
        self.assertEqual(
            [(root.id, root.path.name) for root in sevendays_roots],
            [("server", "serverconfig.xml"), ("rwg-mixer", "rwgmixer.xml")],
        )

    def test_factorio_lowest_config_read_level_uses_sudo_map_root_overrides(self) -> None:
        app = object.__new__(Factorio)
        app.directory = Path("/srv/factorio")
        app.settings = SimpleNamespace(
            app=SimpleNamespace(
                options=(
                    SimpleNamespace(power_level=Power_Level.root),
                )
            )
        )

        self.assertEqual(app.config_file_read_level, Power_Level.root)
        self.assertEqual(app.lowest_config_file_read_level, Power_Level.sudo)
        self.assertEqual(app.config_file_read_level_for_root("server"), Power_Level.root)
        self.assertEqual(app.config_file_read_level_for_root("map-settings"), Power_Level.sudo)
        self.assertEqual(app.config_file_read_level_for_root("map-gen-settings"), Power_Level.sudo)


if __name__ == "__main__":
    unittest.main()
