from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._config import (
    App_Config,
    ClientPackConfig,
    ClientPackPolicy,
    Mod_Config,
    ModDownloadBlockReason,
)
from apps._mod import Mod, Mod_Manager
from apps._mod_catalog import (
    LocalModSource,
    ModAction,
    ModCatalog,
    ModInventoryEntry,
    ModReference,
    ModSourceKind,
)


class _FileMod(Mod):
    async def install(self, src: Path, atomic: bool = True) -> None:
        await self._handle_drop(src, atomic)


class _StaticSource:
    kind = ModSourceKind.LOCAL

    def __init__(self, entries: tuple[ModInventoryEntry, ...]) -> None:
        self._entries = entries

    async def refresh(self) -> None:
        return None

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        return self._entries


class ModCatalogTests(unittest.IsolatedAsyncioTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._temp_dir = TemporaryDirectory[str]()
        self.root = Path(self._temp_dir.name)
        self.mods_dir = self.root / "app" / "mods"

    def setUp(self) -> None:
        Mod_Manager._instances.clear()
        self.mods_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        Mod_Manager._instances.clear()
        self._temp_dir.cleanup()

    def _manager(self) -> Mod_Manager:
        return Mod_Manager(
            App_Config(
                name="test_app",
                instance_key="test",
                friendly_name="Test App",
                directory=self.mods_dir.parent,
                apps_dir=self.root,
                mods_dir=self.mods_dir,
                join_host="127.0.0.1",
                scope="test",
            ),
            mod_cls=_FileMod,
            db_path=self.root / "mods.jsonl",
        )

    def test_reference_round_trip_is_opaque_and_preserves_unicode(self) -> None:
        reference = ModReference.local("Café: example.jar")

        parsed = ModReference.from_id(reference.id)

        self.assertEqual(parsed, reference)
        self.assertTrue(reference.id.startswith("local:"))
        self.assertNotIn(reference.source_key, reference.id)

    def test_reference_rejects_invalid_encoded_ids(self) -> None:
        for raw_id in ("", "local", "local:", "unknown:YWJj", "local:***"):
            with self.subTest(raw_id=raw_id):
                with self.assertRaises((TypeError, ValueError)):
                    ModReference.from_id(raw_id)

    def test_catalog_preserves_source_entry_order(self) -> None:
        first = ModInventoryEntry.from_local_mod(
            _FileMod(Mod_Config(name="zeta.jar", directory=self.mods_dir))
        )
        second = ModInventoryEntry.from_local_mod(
            _FileMod(Mod_Config(name="alpha.jar", directory=self.mods_dir))
        )

        entries = ModCatalog((_StaticSource((first, second)),)).list_entries()

        self.assertEqual(tuple(entry.name for entry in entries), ("zeta.jar", "alpha.jar"))

    def test_local_optional_client_mod_does_not_advertise_an_invalid_block_toggle(self) -> None:
        mod = _FileMod(
            Mod_Config(
                name="optional.jar",
                directory=self.mods_dir,
                client_pack=ClientPackConfig(policy=ClientPackPolicy.OPTIONAL),
            )
        )

        entry = ModInventoryEntry.from_local_mod(mod)

        self.assertNotIn(ModAction.TOGGLE_DOWNLOAD_BLOCK, entry.available_actions)

    def test_local_blocked_optional_client_mod_can_advertise_unblocking(self) -> None:
        mod = _FileMod(
            Mod_Config(
                name="blocked-optional.jar",
                directory=self.mods_dir,
                client_pack=ClientPackConfig(policy=ClientPackPolicy.OPTIONAL),
                download_block_reason=ModDownloadBlockReason.OTHER,
            )
        )

        entry = ModInventoryEntry.from_local_mod(mod)

        self.assertIn(ModAction.TOGGLE_DOWNLOAD_BLOCK, entry.available_actions)

    async def test_local_source_exposes_source_neutral_inventory(self) -> None:
        manager = self._manager()
        incoming = self.root / "incoming.jar"
        incoming.write_bytes(b"mod-data")
        await manager.add(incoming)
        catalog = ModCatalog((LocalModSource(manager),))

        await catalog.refresh()
        entries = catalog.list_entries()

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertIs(entry.reference.source, ModSourceKind.LOCAL)
        self.assertEqual(entry.reference.source_key, "incoming.jar")
        self.assertEqual(catalog.get(entry.reference.id), entry)
        self.assertTrue(entry.artifact_available)
        self.assertIn(ModAction.DISABLE, entry.available_actions)
        self.assertIn(ModAction.DELETE, entry.available_actions)
