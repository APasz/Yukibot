import asyncio
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from apps._config import App_Config, AppVersion, Mod_Config
from apps._mod import Mod_Manager
from apps.factorio import Factorio, Matchers, Mod_Factorio, detect_factorio_version


class FactorioVersionDetectionTests(unittest.TestCase):
    def test_delete_save_file_removes_save_archive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saves_dir = root / "saves"
            save_path = saves_dir / "alpha.zip"
            saves_dir.mkdir()
            save_path.write_bytes(b"save-data")
            app = cast(Any, object.__new__(Factorio))
            app.directory = root
            app.check_running = lambda: False

            deleted = app.delete_save_file(file_id="saves/alpha.zip")

            self.assertEqual(deleted.id, "saves/alpha.zip")
            self.assertFalse(save_path.exists())

    def test_detect_factorio_version_from_local_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "factorio-current.log").write_text(
                "   0.000 2025-01-01 00:00:00; Factorio 1.1.107 (build 12345, linux64, headless)\n",
                encoding="utf-8",
            )

            version = detect_factorio_version(directory=root)

        self.assertEqual(version, AppVersion(main="1.1.107"))

    def test_manager_ignores_factorio_metadata_files_and_uses_mod_list_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            app_dir = apps_dir / "factorio"
            mods_dir = app_dir / "mods"
            mods_dir.mkdir(parents=True)
            self._write_factorio_mod_archive(mods_dir / "example_1.0.0.zip", mod_name="example")
            (mods_dir / "mod-settings.dat").write_bytes(b"\x00")
            (mods_dir / "mod-list.json").write_text(
                json.dumps({"mods": [{"name": "example", "enabled": False}]}, indent=2),
                encoding="utf-8",
            )
            app_cfg = App_Config(
                name="factorio_alpha",
                instance_key="alpha",
                friendly_name="Factorio",
                directory=app_dir,
                apps_dir=apps_dir,
                mods_dir=mods_dir,
                join_host="127.0.0.1",
                scope="factorio",
            )
            Mod_Manager._instances.clear()
            manager = Mod_Manager(app_cfg, mod_cls=Mod_Factorio, db_path=root / "mods.jsonl")

            try:
                asyncio.run(manager.reload_mods())
            finally:
                Mod_Manager._instances.clear()

            self.assertEqual(manager.list_names(), ["example_1.0.0.zip"])
            self.assertFalse(manager.get("example_1.0.0.zip").cfg.enabled)
            self.assertEqual(manager.get("example_1.0.0.zip").cfg.version, "1.0.0")

    def test_toggle_updates_factorio_mod_list_without_renaming_archive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mods_dir = root / "mods"
            mods_dir.mkdir()
            archive_path = mods_dir / "example_1.0.0.zip"
            self._write_factorio_mod_archive(archive_path, mod_name="example")
            mod_list_path = mods_dir / "mod-list.json"
            mod_list_path.write_text(
                json.dumps({"mods": [{"name": "example", "enabled": True}]}, indent=2),
                encoding="utf-8",
            )
            mod = Mod_Factorio(Mod_Config(name=archive_path.name, directory=mods_dir))

            asyncio.run(mod.disable())

            payload = json.loads(mod_list_path.read_text(encoding="utf-8"))
            self.assertTrue(archive_path.exists())
            self.assertFalse((mods_dir / "example_1.0.0.disabled").exists())
            self.assertEqual(payload["mods"], [{"name": "example", "enabled": False}])
            self.assertFalse(mod.cfg.enabled)
            self.assertEqual(mod.detect_version(), "1.0.0")

    def test_detects_human_friendly_name_from_mod_id(self) -> None:
        mod = Mod_Factorio(Mod_Config(name="circuit-network-selector-wire-icons_1.0.0.zip", directory=Path(".")))

        mod.sync_metadata()

        self.assertEqual(mod.friendly, "Circuit Network Selector Wire Icons")

    def test_reload_removes_stale_factorio_metadata_entries_from_db(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            app_dir = apps_dir / "factorio"
            mods_dir = app_dir / "mods"
            mods_dir.mkdir(parents=True)
            self._write_factorio_mod_archive(mods_dir / "example_1.0.0.zip", mod_name="example")
            (mods_dir / "mod-settings.dat").write_bytes(b"\x00")
            (mods_dir / "mod-list.json").write_text(
                json.dumps({"mods": [{"name": "example", "enabled": True}]}, indent=2),
                encoding="utf-8",
            )
            db_path = root / "mods.jsonl"
            db_path.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "name": "example_1.0.0.zip",
                                "directory": str(mods_dir),
                                "enabled": True,
                                "version": None,
                                "origin": "manual",
                                "coremod": False,
                                "download_block_reason": None,
                            }
                        ),
                        json.dumps(
                            {
                                "name": "mod-list.json",
                                "directory": str(mods_dir),
                                "enabled": True,
                                "version": None,
                                "origin": "manual",
                                "coremod": False,
                                "download_block_reason": None,
                            }
                        ),
                    )
                ),
                encoding="utf-8",
            )
            app_cfg = App_Config(
                name="factorio_alpha",
                instance_key="alpha",
                friendly_name="Factorio",
                directory=app_dir,
                apps_dir=apps_dir,
                mods_dir=mods_dir,
                join_host="127.0.0.1",
                scope="factorio",
            )
            Mod_Manager._instances.clear()
            manager = Mod_Manager(app_cfg, mod_cls=Mod_Factorio, db_path=db_path)

            try:
                asyncio.run(manager.reload_mods())
            finally:
                Mod_Manager._instances.clear()

            self.assertEqual(manager.list_names(), ["example_1.0.0.zip"])
            self.assertNotIn("mod-list.json", db_path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_factorio_mod_archive(pointer: Path, *, mod_name: str) -> None:
        with zipfile.ZipFile(pointer, "w") as archive:
            archive.writestr(f"{mod_name}_1.0.0/info.json", json.dumps({"name": mod_name, "version": "1.0.0"}))


class FactorioRelayMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_research_relays_finished_notice(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_research(
                "891.725 Script @__events-logger__/events/research.lua:23: [RESEARCH FINISHED] electronics 1"
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Research: Electronics 1")
        self.assertIsNotNone(relayed_message.relay_embed)
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Research")
        self.assertEqual(relayed_message.relay_embed.description, "Electronics 1")


if __name__ == "__main__":
    unittest.main()
