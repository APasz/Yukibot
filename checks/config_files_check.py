from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from _security import Power_Level
from apps._config_files import (
    AppConfigFileKind,
    AppConfigFileRoot,
    create_app_config_file,
    delete_app_config_file,
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

    def test_managed_config_root_creates_and_deletes_unmanaged_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir) / "kubejs" / "server_scripts"
            roots = (
                AppConfigFileRoot(
                    id="kubejs-server-scripts",
                    label="KubeJS Server Scripts",
                    path=root_path,
                    kind=AppConfigFileKind.MOD,
                    suffixes=frozenset({".js"}),
                    allow_file_creation=True,
                    allow_file_deletion=True,
                    protected_relative_paths=frozenset({"yuki_recipes.js"}),
                    write_notice="Reload the app to apply this config file.",
                ),
            )

            created = create_app_config_file(
                roots,
                "kubejs-server-scripts",
                "custom/example.js",
                "ServerEvents.recipes(() => {})\n",
                default_read_level=Power_Level.sudo,
                default_write_level=Power_Level.sudo,
            )
            deleted = delete_app_config_file(
                roots,
                "kubejs-server-scripts/custom/example.js",
                default_read_level=Power_Level.sudo,
                default_write_level=Power_Level.sudo,
            )
            file_exists_after_delete = (root_path / "custom" / "example.js").exists()

        self.assertEqual(created.file.relative_path, "custom/example.js")
        self.assertTrue(created.file.can_write)
        self.assertTrue(created.file.can_delete)
        self.assertEqual(created.file.write_notice, "Reload the app to apply this config file.")
        self.assertEqual(deleted.id, "kubejs-server-scripts/custom/example.js")
        self.assertFalse(file_exists_after_delete)

    def test_managed_config_root_protects_reserved_files_and_rejects_invalid_creates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir) / "kubejs" / "server_scripts"
            root_path.mkdir(parents=True)
            reserved_path = root_path / "yuki_recipes.js"
            reserved_path.write_text("managed\n", encoding="utf-8")
            roots = (
                AppConfigFileRoot(
                    id="kubejs-server-scripts",
                    label="KubeJS Server Scripts",
                    path=root_path,
                    kind=AppConfigFileKind.MOD,
                    suffixes=frozenset({".js"}),
                    allow_file_creation=True,
                    allow_file_deletion=True,
                    protected_relative_paths=frozenset({"yuki_recipes.js"}),
                ),
            )

            listed = list_app_config_files(
                roots,
                default_read_level=Power_Level.sudo,
                default_write_level=Power_Level.sudo,
            )
            with self.assertRaisesRegex(ValueError, "cannot be modified"):
                write_app_config_file(
                    roots,
                    "kubejs-server-scripts/yuki_recipes.js",
                    "edited\n",
                    default_read_level=Power_Level.sudo,
                    default_write_level=Power_Level.sudo,
                )
            with self.assertRaisesRegex(ValueError, "cannot be deleted"):
                delete_app_config_file(
                    roots,
                    "kubejs-server-scripts/yuki_recipes.js",
                    default_read_level=Power_Level.sudo,
                    default_write_level=Power_Level.sudo,
                )
            with self.assertRaisesRegex(ValueError, "cannot be created"):
                create_app_config_file(
                    roots,
                    "kubejs-server-scripts",
                    "yuki_recipes.js",
                    "",
                    default_read_level=Power_Level.sudo,
                    default_write_level=Power_Level.sudo,
                )
            with self.assertRaisesRegex(ValueError, "suffix is not allowed"):
                create_app_config_file(
                    roots,
                    "kubejs-server-scripts",
                    "not-a-script.json",
                    "{}",
                    default_read_level=Power_Level.sudo,
                    default_write_level=Power_Level.sudo,
                )
            reserved_content = reserved_path.read_text(encoding="utf-8")

        self.assertFalse(listed[0].can_write)
        self.assertFalse(listed[0].can_delete)
        self.assertEqual(reserved_content, "managed\n")

    def test_non_recursive_config_root_rejects_nested_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir) / "config"
            root_path.mkdir()
            roots = (
                AppConfigFileRoot(
                    id="server",
                    label="Server Properties",
                    path=root_path,
                    kind=AppConfigFileKind.GAME,
                    recursive=False,
                ),
            )

            with self.assertRaisesRegex(ValueError, "does not allow nested"):
                create_app_config_file(
                    roots,
                    "server",
                    "nested/server.toml",
                    "enabled=true\n",
                    default_read_level=Power_Level.user,
                    default_write_level=Power_Level.sudo,
                )

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

    def test_minecraft_exposes_sudo_managed_kubejs_script_roots_when_kubejs_is_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = object.__new__(Minecraft)
            app.directory = Path(temp_dir)
            app._file_settings = Path(temp_dir) / "server.properties"
            app.settings = None
            app.config_file_read_level_override = None
            app.config_file_write_level_override = None
            setattr(
                app,
                "mods",
                SimpleNamespace(list_mods=lambda _enabled_only: [SimpleNamespace(name="kubejs-forge-1.0.0.jar")]),
            )

            roots = app.config_file_roots
            startup_script_path = next(root for root in roots if root.id == "kubejs-startup-scripts").path / (
                "yuki_item_registry.js"
            )
            startup_script_path.parent.mkdir(parents=True)
            startup_script_path.write_text("managed\n", encoding="utf-8")
            scripts = app.list_config_files()

        kubejs_roots = roots[3:]
        managed_startup_script = next(script for script in scripts if script.id == "kubejs-startup-scripts/yuki_item_registry.js")
        self.assertEqual(
            [root.id for root in kubejs_roots],
            ["kubejs-server-scripts", "kubejs-startup-scripts", "kubejs-client-scripts"],
        )
        self.assertTrue(all(root.allow_file_creation for root in kubejs_roots))
        self.assertTrue(all(root.allow_file_deletion for root in kubejs_roots))
        self.assertTrue(all(root.read_power_level_override is Power_Level.sudo for root in kubejs_roots))
        self.assertTrue(all(root.write_power_level_override is Power_Level.sudo for root in kubejs_roots))
        self.assertEqual(
            [root.write_notice for root in kubejs_roots],
            [
                "KubeJS server script saved. Run /kubejs reload server_scripts or restart Minecraft.",
                "KubeJS startup script saved. Restart Minecraft to apply it.",
                "KubeJS client script saved. Restart clients to apply it.",
            ],
        )
        self.assertTrue(
            all(
                root.protected_relative_paths
                == frozenset({"yuki_log.js", "yuki_recipes.js", "yuki_item_registry.js"})
                for root in kubejs_roots
            )
        )
        self.assertFalse(managed_startup_script.can_write)
        self.assertFalse(managed_startup_script.can_delete)

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
