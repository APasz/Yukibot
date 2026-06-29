from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _mod_ops import NonDownloadableModError, download_paths
from apps._config import (
    App_Config,
    ClientPackConfig,
    ClientPackPolicy,
    ModDownloadBlockReason,
    ModType,
)
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
            join_host="127.0.0.1",
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

    async def test_set_enabled_is_idempotent_when_mod_is_already_enabled(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        updated = await manager.set_enabled("example.zip", True)

        self.assertTrue(updated.cfg.enabled)
        self.assertTrue((self.mods_dir / "example.zip").exists())

    async def test_set_enabled_is_idempotent_when_mod_is_already_disabled(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())
        await manager.set_enabled("example.zip", False)

        updated = await manager.set_enabled("example.zip", False)

        self.assertFalse(updated.cfg.enabled)
        self.assertTrue((self.mods_dir / "example.disabled").exists())

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
        self.assertEqual(reloaded.cfg.mod_type, ModType.COREMOD)

    async def test_legacy_builtin_block_reason_migrates_to_builtin_mod_type(self) -> None:
        (self.mods_dir / "builtin.zip").write_text("payload", encoding="utf-8")
        self.db_path.write_text(
            (
                '{"name":"builtin.zip","directory":"%s","enabled":true,"version":null,"origin":"manual",'
                '"coremod":false,"download_block_reason":"builtin"}'
            )
            % self.mods_dir,
            encoding="utf-8",
        )
        manager = self._build_manager()

        await manager.reload_mods()

        reloaded = manager.get("builtin.zip")
        self.assertEqual(reloaded.cfg.mod_type, ModType.BUILTIN)

    async def test_legacy_server_only_block_reason_stays_regular_mod_type(self) -> None:
        (self.mods_dir / "server-only.zip").write_text("payload", encoding="utf-8")
        self.db_path.write_text(
            (
                '{"name":"server-only.zip","directory":"%s","enabled":true,"version":null,"origin":"manual",'
                '"coremod":false,"download_block_reason":"server_only"}'
            )
            % self.mods_dir,
            encoding="utf-8",
        )
        manager = self._build_manager()

        await manager.reload_mods()

        reloaded = manager.get("server-only.zip")
        self.assertEqual(reloaded.cfg.mod_type, ModType.REGULAR)

    async def test_set_download_block_reason_persists_across_reload(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        updated = await manager.set_download_block_reason("example.zip", ModDownloadBlockReason.SERVER_ONLY)

        self.assertFalse(updated.downloadable)
        self.assertEqual(updated.download_block_label, "Server only")

        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertFalse(reloaded.downloadable)
        self.assertEqual(reloaded.cfg.download_block_reason, ModDownloadBlockReason.SERVER_ONLY)

    async def test_client_pack_choice_group_persists_when_valid(self) -> None:
        manager = self._build_manager()
        first = await manager.add(self._write_source_file("first.zip"))
        second = await manager.add(self._write_source_file("second.zip"))
        first.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="map_renderer",
            default_choice=True,
        )
        second.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="map_renderer",
        )

        await manager.save_mods()
        await manager.reload_mods()

        self.assertEqual(manager.get("first.zip").cfg.client_pack.choice_group, "map_renderer")
        self.assertTrue(manager.get("first.zip").cfg.client_pack.default_choice)

    async def test_client_pack_choice_group_requires_one_default(self) -> None:
        manager = self._build_manager()
        first = await manager.add(self._write_source_file("first.zip"))
        second = await manager.add(self._write_source_file("second.zip"))
        first.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="map_renderer",
        )
        second.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="map_renderer",
        )

        with self.assertRaisesRegex(ValueError, "exactly one default"):
            await manager.save_mods()

    async def test_remove_rejects_breaking_a_client_pack_choice_group_before_deleting_file(self) -> None:
        manager = self._build_manager()
        first = await manager.add(self._write_source_file("first.zip"))
        second = await manager.add(self._write_source_file("second.zip"))
        first.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="map_renderer",
            default_choice=True,
        )
        second.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="map_renderer",
        )
        await manager.save_mods()

        with self.assertRaisesRegex(ValueError, "at least two mods"):
            await manager.remove(second)

        self.assertTrue(second.path.exists())
        self.assertIs(manager.get(second.name), second)

    async def test_download_paths_skip_blocked_mods_for_batch_downloads(self) -> None:
        manager = self._build_manager()
        downloadable = await manager.add(self._write_source_file("downloadable.zip"))
        blocked = await manager.add(self._write_source_file("server-only.zip"))
        await manager.set_download_block_reason(blocked, ModDownloadBlockReason.SERVER_ONLY)

        paths = download_paths(manager, default_enabled_only=False)

        self.assertEqual(paths, (downloadable.path,))

    async def test_download_paths_reject_blocked_direct_download(self) -> None:
        manager = self._build_manager()
        blocked = await manager.add(self._write_source_file("server-only.zip"))
        await manager.set_download_block_reason(blocked, ModDownloadBlockReason.SERVER_ONLY)

        with self.assertRaises(NonDownloadableModError):
            download_paths(manager, (blocked.name,), default_enabled_only=False)


if __name__ == "__main__":
    unittest.main()
