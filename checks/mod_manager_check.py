from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._config import App_Config
from apps._mod import Mod, Mod_Manager


class _FileMod(Mod):
    async def install(self, src: Path, atomic: bool = True) -> None:
        await self._handle_drop(src, atomic)


class ModManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        Mod_Manager._instances.clear()
        self._temp_dir = TemporaryDirectory()
        self.temp_path = Path(self._temp_dir.name)
        self.apps_dir = self.temp_path / "apps"
        self.app_dir = self.apps_dir / "test"
        self.mods_dir = self.app_dir / "mods"
        self.mods_dir.mkdir(parents=True)
        self.db_path = self.temp_path / "mods.jsonl"

    def tearDown(self) -> None:
        Mod_Manager._instances.clear()
        self._temp_dir.cleanup()

    def _build_manager(self) -> Mod_Manager:
        app_cfg = App_Config(
            name="test_app",
            instance_key="test",
            friendly_name="Test App",
            directory=self.app_dir,
            apps_dir=self.apps_dir,
            mods_dir=self.mods_dir,
            address="127.0.0.1",
            scope="test",
        )
        return Mod_Manager(app_cfg, mod_cls=_FileMod, db_path=self.db_path)

    def _write_source_file(self, name: str = "example.zip") -> Path:
        source_dir = self.temp_path / "incoming"
        source_dir.mkdir(exist_ok=True)
        pointer = source_dir / name
        pointer.write_text("payload")
        return pointer

    async def test_add_persists_mod_and_lookup(self) -> None:
        manager = self._build_manager()

        added = await manager.add(self._write_source_file())

        self.assertEqual(added.name, "example.zip")
        self.assertTrue(added.cfg.enabled)
        self.assertTrue(added.path.exists())
        self.assertEqual(manager.get("example.zip").name, "example.zip")
        self.assertIn('"name":"example.zip"', self.db_path.read_text())

    async def test_toggle_and_reload_preserve_disabled_mod_identity(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        toggled = await manager.toggle("example.zip")

        self.assertFalse(toggled.cfg.enabled)
        self.assertEqual(toggled.path.name, "example.disabled")
        self.assertTrue(toggled.path.exists())
        self.assertEqual(manager.list_names(False), ["example.zip"])

        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertFalse(reloaded.cfg.enabled)
        self.assertEqual(reloaded.path.name, "example.disabled")
        self.assertEqual(manager.list_names(False), ["example.zip"])

    async def test_remove_updates_index_and_db(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        removed = await manager.remove("example.zip")

        self.assertEqual(removed.name, "example.zip")
        self.assertFalse(removed.path.exists())
        self.assertEqual(manager.index, {})
        self.assertEqual(self.db_path.read_text(), "")

    async def test_set_coremod_persists_across_reload(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        updated = await manager.set_coremod("example.zip", True)

        self.assertTrue(updated.cfg.coremod)

        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertTrue(reloaded.cfg.coremod)


if __name__ == "__main__":
    unittest.main()
