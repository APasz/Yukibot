from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _mod_ops import (
    ClientPackSelection,
    ClientPackValidationError,
    ModArchiveEntry,
    NonDownloadableModError,
    build_client_pack_entries,
    build_admin_pack_entries,
    build_server_pack_entries,
    download_entries,
    download_paths,
)
from apps._config import (
    App_Config,
    ClientPackConfig,
    ClientPackPolicy,
    CurseForgeModMetadata,
    ModClassificationOverride,
    Mod_Config,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPlacement,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
)
from apps._mod import Mod, Mod_Manager


class _FileMod(Mod):
    async def install(self, src: Path, atomic: bool = True) -> None:
        await self._handle_drop(src, atomic)


class _DetectedServerMod(_FileMod):
    def default_mod_type(self) -> ModType:
        return ModType.SERVER

    def default_download_block_reason(self) -> ModDownloadBlockReason | None:
        return ModDownloadBlockReason.SERVER_ONLY


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

    def _build_manager(
        self,
        *,
        mod_cls: type[Mod] = _FileMod,
        client_mods_dir: Path | None = None,
    ) -> Mod_Manager:
        app_cfg = App_Config(
            name="test_app",
            instance_key="test",
            friendly_name="Test App",
            directory=self.app_dir,
            apps_dir=self.apps_dir,
            mods_dir=self.mods_dir,
            client_mods_dir=client_mods_dir,
            join_host="127.0.0.1",
            scope="test",
        )
        return Mod_Manager(app_cfg, mod_cls=mod_cls, db_path=self.db_path)

    def _write_source_file(self, name: str = "example.zip") -> Path:
        source_dir = self.temp_path / "incoming"
        source_dir.mkdir(exist_ok=True)
        pointer = source_dir / name
        pointer.write_text("payload")
        return pointer

    def _insert_existing_mod(
        self,
        manager: Mod_Manager,
        name: str,
        *,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
        mod_type: ModType = ModType.REGULAR,
        download_block_reason: ModDownloadBlockReason | None = None,
    ) -> Mod:
        mod = _FileMod(
            Mod_Config(
                name=name,
                directory=self.mods_dir,
                placement=placement,
                mod_type=mod_type,
                download_block_reason=download_block_reason,
            )
        )
        mod.storage_path.write_text("payload", encoding="utf-8")
        manager.index[mod.name] = mod
        manager._rebuild_lookup()
        return mod

    async def _build_client_pack_manager(self) -> Mod_Manager:
        manager = self._build_manager()
        required = await manager.add(self._write_source_file("required.zip"))
        client = await manager.add(
            self._write_source_file("client.zip"),
            placement=ModPlacement.CLIENT_ONLY,
        )
        optional_default = await manager.add(self._write_source_file("optional-default.zip"))
        optional_explicit = await manager.add(self._write_source_file("optional-explicit.zip"))
        choice_default = await manager.add(self._write_source_file("choice-default.zip"))
        choice_explicit = await manager.add(self._write_source_file("choice-explicit.zip"))
        required.cfg.client_pack = ClientPackConfig(policy=ClientPackPolicy.REQUIRED)
        client.cfg.client_pack = ClientPackConfig(policy=ClientPackPolicy.REQUIRED)
        optional_default.cfg.client_pack = ClientPackConfig(policy=ClientPackPolicy.OPTIONAL)
        optional_explicit.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.OPTIONAL,
            default_selected=False,
        )
        choice_default.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="renderer",
            default_choice=True,
        )
        choice_explicit.cfg.client_pack = ClientPackConfig(
            policy=ClientPackPolicy.ALTERNATIVE,
            choice_group="renderer",
        )
        await manager.save_mods()
        return manager

    async def test_add_persists_mod_and_lookup(self) -> None:
        manager = self._build_manager()

        added = await manager.add(self._write_source_file())

        self.assertEqual(added.name, "example.zip")
        self.assertTrue(added.cfg.enabled)
        self.assertTrue(added.path.exists())
        self.assertEqual(manager.get("example.zip").name, "example.zip")
        self.assertIn('"name":"example.zip"', self.db_path.read_text())

    def test_optional_client_pack_defaults_preserve_legacy_inclusion(self) -> None:
        legacy = ClientPackConfig.model_validate({"policy": "optional"})
        explicit_opt_out = ClientPackConfig(
            policy=ClientPackPolicy.OPTIONAL,
            default_selected=False,
        )

        self.assertTrue(legacy.default_selected)
        self.assertFalse(explicit_opt_out.default_selected)

    def test_client_pack_inclusion_is_explicitly_typed(self) -> None:
        self.assertTrue(ClientPackConfig().included_in_client)
        self.assertFalse(ClientPackConfig(included_in_client=False).included_in_client)

    def test_client_pack_inclusion_defaults_from_mod_type(self) -> None:
        regular = Mod_Config(name="regular.zip", directory=self.mods_dir)
        server = Mod_Config(name="server.zip", directory=self.mods_dir, mod_type=ModType.SERVER)
        explicitly_included_server = Mod_Config(
            name="included-server.zip",
            directory=self.mods_dir,
            mod_type=ModType.SERVER,
            client_pack=ClientPackConfig(included_in_client=True),
        )

        self.assertTrue(regular.client_pack.included_in_client)
        self.assertFalse(server.client_pack.included_in_client)
        self.assertTrue(explicitly_included_server.client_pack.included_in_client)

    def test_alternative_client_pack_group_id_rejects_whitespace(self) -> None:
        for group_id in ("visual options", " visual-options", "visual-options ", "visual\toptions"):
            with self.subTest(group_id=group_id), self.assertRaisesRegex(ValueError, "cannot contain whitespace"):
                ClientPackConfig(
                    policy=ClientPackPolicy.ALTERNATIVE,
                    choice_group=group_id,
                )

    async def test_toggle_and_reload_preserve_disabled_mod_identity(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        toggled = await manager.toggle("example.zip")

        self.assertFalse(toggled.cfg.enabled)
        self.assertIs(toggled.cfg.placement, ModPlacement.SERVER_DISABLED)
        self.assertEqual(toggled.path.name, "example.zip.disabled")
        self.assertTrue(toggled.path.exists())
        self.assertEqual(manager.list_names(False), ["example.zip"])

        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertFalse(reloaded.cfg.enabled)
        self.assertIs(reloaded.cfg.placement, ModPlacement.SERVER_DISABLED)
        self.assertEqual(reloaded.path.name, "example.zip.disabled")
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
        self.assertTrue((self.mods_dir / "example.zip.disabled").exists())

    async def test_legacy_enabled_field_derives_persisted_placement(self) -> None:
        (self.mods_dir / "legacy.zip.disabled").write_text("payload", encoding="utf-8")
        self.db_path.write_text(
            '{"name":"legacy.zip","directory":"%s","enabled":false}' % self.mods_dir,
            encoding="utf-8",
        )
        manager = self._build_manager()

        await manager.reload_mods()

        mod = manager.get("legacy.zip")
        self.assertIs(mod.cfg.placement, ModPlacement.SERVER_DISABLED)
        self.assertFalse(mod.cfg.enabled)
        self.assertIn('"placement":"server_disabled"', self.db_path.read_text(encoding="utf-8"))

    async def test_legacy_replaced_suffix_file_is_migrated(self) -> None:
        (self.mods_dir / "legacy.disabled").write_text("payload", encoding="utf-8")
        self.db_path.write_text(
            '{"name":"legacy.zip","directory":"%s","enabled":false}' % self.mods_dir,
            encoding="utf-8",
        )
        manager = self._build_manager()

        await manager.reload_mods()

        mod = manager.get("legacy.zip")
        self.assertEqual(mod.path, self.mods_dir / "legacy.zip.disabled")
        self.assertFalse((self.mods_dir / "legacy.disabled").exists())

    async def test_discovers_client_only_mod_with_appended_marker(self) -> None:
        client_path = self.mods_dir / "client.zip.client"
        client_path.write_text("payload", encoding="utf-8")
        manager = self._build_manager()

        await manager.reload_mods()

        mod = manager.get("client.zip")
        self.assertIs(mod.cfg.placement, ModPlacement.CLIENT_ONLY)
        self.assertFalse(mod.cfg.enabled)
        self.assertEqual(mod.storage_path, client_path)
        self.assertNotIn(mod, manager.list_mods(True))
        self.assertNotIn(mod, manager.list_mods(False))

        with self.assertRaisesRegex(ValueError, "no server enabled state"):
            await manager.set_enabled(mod, True)

    async def test_discovers_client_only_mod_in_separate_client_folder(self) -> None:
        client_mods_dir = self.app_dir / "client-mods"
        client_mods_dir.mkdir()
        client_path = client_mods_dir / "client.zip"
        client_path.write_text("payload", encoding="utf-8")
        manager = self._build_manager(client_mods_dir=client_mods_dir)

        await manager.reload_mods()

        mod = manager.get("client.zip")
        self.assertIs(mod.cfg.placement, ModPlacement.CLIENT_ONLY)
        self.assertIs(mod.mod_type, ModType.CLIENT)
        self.assertEqual(mod.storage_path, client_path)

    async def test_conflicting_physical_representations_fail_discovery(self) -> None:
        (self.mods_dir / "duplicate.zip").write_text("enabled", encoding="utf-8")
        (self.mods_dir / "duplicate.zip.disabled").write_text("disabled", encoding="utf-8")
        (self.mods_dir / "duplicate.zip.client").write_text("client", encoding="utf-8")
        manager = self._build_manager()

        with self.assertRaisesRegex(RuntimeError, "conflicting physical representations"):
            await manager.reload_mods()

    async def test_server_and_separate_client_representations_conflict(self) -> None:
        client_mods_dir = self.app_dir / "client-mods"
        client_mods_dir.mkdir()
        (self.mods_dir / "duplicate.zip").write_text("enabled", encoding="utf-8")
        (client_mods_dir / "duplicate.zip").write_text("client", encoding="utf-8")
        manager = self._build_manager(client_mods_dir=client_mods_dir)

        with self.assertRaisesRegex(RuntimeError, "conflicting physical representations"):
            await manager.reload_mods()

    async def test_add_and_remove_client_only_mod_in_separate_folder(self) -> None:
        client_mods_dir = self.app_dir / "client-mods"
        manager = self._build_manager(client_mods_dir=client_mods_dir)

        added = await manager.add(
            self._write_source_file("client.zip"),
            placement=ModPlacement.CLIENT_ONLY,
        )

        self.assertIs(added.cfg.placement, ModPlacement.CLIENT_ONLY)
        self.assertIs(added.mod_type, ModType.CLIENT)
        self.assertEqual(added.storage_path, client_mods_dir / "client.zip")
        self.assertTrue(added.storage_path.exists())

        removed = await manager.remove(added)

        self.assertFalse(removed.storage_path.exists())
        self.assertEqual(manager.index, {})

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

        updated = await manager.set_download_block_reason("example.zip", ModDownloadBlockReason.OTHER)

        self.assertFalse(updated.downloadable)
        self.assertEqual(updated.download_block_label, "Not downloadable")

        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertFalse(reloaded.downloadable)
        self.assertEqual(reloaded.cfg.download_block_reason, ModDownloadBlockReason.OTHER)

    async def test_platform_metadata_persists_across_reload(self) -> None:
        manager = self._build_manager()
        mod = await manager.add(self._write_source_file())
        mod.cfg.platforms = ModPlatformMetadata(
            modrinth=ModrinthModMetadata(
                page_url="https://modrinth.com/mod/example/version/version-id",
                project_id="project-slug",
                version_id="version-id",
                download_url="https://cdn.modrinth.com/data/project/version/example.zip",
            ),
            curseforge=CurseForgeModMetadata(
                page_url="https://www.curseforge.com/minecraft/mc-mods/example/files/456",
                project_id=123,
                file_id=456,
            ),
        )

        await manager.save_mods()
        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertEqual(reloaded.cfg.platforms.modrinth.project_id, "project-slug")
        self.assertEqual(reloaded.cfg.platforms.curseforge.file_id, 456)

    async def test_update_properties_persists_classification_and_metadata_overrides(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        updated = await manager.update_properties(
            "example.zip",
            mod_type=ModType.CLIENT,
            download_block_reason=ModDownloadBlockReason.ARTIFACT,
            metadata_overrides=ModMetadataOverrides(
                friendly_name="Example Client Mod",
                version="2.4.0",
                origin="curated",
            ),
        )

        self.assertEqual(updated.mod_type, ModType.CLIENT)
        self.assertEqual(updated.friendly, "Example Client Mod")
        self.assertEqual(updated.version, "2.4.0")
        self.assertEqual(updated.origin, "curated")
        self.assertIs(manager.get("Example Client Mod"), updated)

        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertEqual(reloaded.mod_type, ModType.CLIENT)
        self.assertEqual(reloaded.download_block_reason, ModDownloadBlockReason.ARTIFACT)
        self.assertEqual(reloaded.cfg.metadata_overrides.friendly_name, "Example Client Mod")
        self.assertEqual(reloaded.version, "2.4.0")
        self.assertEqual(reloaded.origin, "curated")

    async def test_update_properties_persists_optional_client_pack_policy(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file())

        await manager.update_properties(
            "example.zip",
            mod_type=ModType.CLIENT,
            download_block_reason=None,
            metadata_overrides=ModMetadataOverrides(),
            client_pack=ClientPackConfig(
                policy=ClientPackPolicy.OPTIONAL,
                default_selected=False,
            ),
        )
        await manager.reload_mods()

        client_pack = manager.get("example.zip").cfg.client_pack
        self.assertIs(client_pack.policy, ClientPackPolicy.OPTIONAL)
        self.assertFalse(client_pack.default_selected)

    async def test_update_client_pack_configs_persists_all_updates_in_one_operation(self) -> None:
        manager = self._build_manager()
        await manager.add(self._write_source_file("alpha.zip"))
        await manager.add(self._write_source_file("beta.zip"))
        updates = {
            "alpha.zip": ClientPackConfig(policy=ClientPackPolicy.OPTIONAL, default_selected=True),
            "beta.zip": ClientPackConfig(policy=ClientPackPolicy.REQUIRED),
        }

        updated = await manager.update_client_pack_configs(updates)
        await manager.reload_mods()

        self.assertEqual(tuple(mod.name for mod in updated), ("alpha.zip", "beta.zip"))
        self.assertEqual(manager.get("alpha.zip").cfg.client_pack, updates["alpha.zip"])
        self.assertEqual(manager.get("beta.zip").cfg.client_pack, updates["beta.zip"])

    async def test_explicit_regular_classification_overrides_detected_server_type(self) -> None:
        manager = self._build_manager(mod_cls=_DetectedServerMod)
        detected = await manager.add(self._write_source_file())
        self.assertEqual(detected.mod_type, ModType.SERVER)

        await manager.update_properties(
            detected,
            mod_type=ModType.REGULAR,
            download_block_reason=None,
            metadata_overrides=ModMetadataOverrides(),
        )
        await manager.reload_mods()

        reloaded = manager.get("example.zip")
        self.assertEqual(reloaded.mod_type, ModType.REGULAR)
        self.assertIsNone(reloaded.download_block_reason)

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

    async def test_client_pack_uses_configured_defaults_without_submitted_selection(self) -> None:
        manager = await self._build_client_pack_manager()

        entries = build_client_pack_entries(
            manager,
            ClientPackSelection(),
            client_overrides_dir=None,
        )

        self.assertEqual(
            {entry.archive_path.as_posix() for entry in entries},
            {"required.zip", "client.zip", "optional-default.zip", "choice-default.zip"},
        )

    def test_client_pack_candidate_matrix(self) -> None:
        manager = self._build_manager()
        shared = self._insert_existing_mod(manager, "shared.zip")
        client_side = self._insert_existing_mod(manager, "client-side.zip", mod_type=ModType.CLIENT)
        client_only = self._insert_existing_mod(
            manager,
            "client-only.zip",
            placement=ModPlacement.CLIENT_ONLY,
            mod_type=ModType.CLIENT,
        )
        disabled = self._insert_existing_mod(
            manager,
            "disabled.zip",
            placement=ModPlacement.SERVER_DISABLED,
        )
        server = self._insert_existing_mod(manager, "server.zip", mod_type=ModType.SERVER)

        entries = build_client_pack_entries(
            manager,
            ClientPackSelection(),
            client_overrides_dir=None,
        )

        self.assertEqual(
            {entry.archive_path.as_posix() for entry in entries},
            {shared.name, client_side.name, client_only.name},
        )
        self.assertTrue(shared.client_pack_eligible)
        self.assertTrue(client_side.client_pack_eligible)
        self.assertTrue(client_only.client_pack_eligible)
        self.assertFalse(disabled.client_pack_eligible)
        self.assertFalse(server.client_pack_eligible)

    async def test_server_mod_is_directly_downloadable_and_can_be_included_in_client(self) -> None:
        manager = self._build_manager(mod_cls=_DetectedServerMod)
        server = await manager.add(self._write_source_file("server.zip"))

        self.assertIs(server.mod_type, ModType.SERVER)
        self.assertTrue(server.downloadable)
        self.assertFalse(server.cfg.client_pack.included_in_client)
        self.assertFalse(server.client_pack_eligible)
        self.assertEqual(
            download_paths(manager, (server.name,), default_enabled_only=False),
            (server.storage_path,),
        )

        server.cfg.client_pack.included_in_client = True
        self.assertTrue(server.client_pack_eligible)
        self.assertEqual(
            tuple(
                entry.mod_name
                for entry in build_client_pack_entries(
                    manager,
                    ClientPackSelection(),
                    client_overrides_dir=None,
                )
                if isinstance(entry, ModArchiveEntry)
            ),
            (server.name,),
        )

    def test_client_pack_rejects_explicitly_selected_disabled_mod(self) -> None:
        manager = self._build_manager()
        disabled = self._insert_existing_mod(
            manager,
            "disabled.zip",
            placement=ModPlacement.SERVER_DISABLED,
        )

        with self.assertRaisesRegex(ClientPackValidationError, "not eligible.*disabled.zip"):
            build_client_pack_entries(
                manager,
                ClientPackSelection(
                    selected_mod_names=frozenset({disabled.name}),
                    supplied=True,
                ),
                client_overrides_dir=None,
            )

    def test_client_pack_rejects_explicitly_selected_server_only_mod(self) -> None:
        manager = self._build_manager()
        server = self._insert_existing_mod(
            manager,
            "server.zip",
            mod_type=ModType.SERVER,
            download_block_reason=ModDownloadBlockReason.SERVER_ONLY,
        )

        with self.assertRaisesRegex(ClientPackValidationError, "not eligible.*server.zip"):
            build_client_pack_entries(
                manager,
                ClientPackSelection(
                    selected_mod_names=frozenset({server.name}),
                    supplied=True,
                ),
                client_overrides_dir=None,
            )

    def test_client_only_mod_requires_client_side_classification(self) -> None:
        manager = self._build_manager()
        client = self._insert_existing_mod(
            manager,
            "client.zip",
            placement=ModPlacement.CLIENT_ONLY,
            mod_type=ModType.REGULAR,
        )

        with self.assertRaisesRegex(ValueError, "requires client-side classification"):
            manager.validate_client_pack_configuration()
        with self.assertRaisesRegex(ValueError, "requires client-side classification"):
            _ = client.client_pack_eligible
        with self.assertRaisesRegex(ClientPackValidationError, "requires client-side classification"):
            build_client_pack_entries(
                manager,
                ClientPackSelection(),
                client_overrides_dir=None,
            )

    async def test_client_pack_uses_explicit_optional_and_alternative_selection(self) -> None:
        manager = await self._build_client_pack_manager()

        entries = build_client_pack_entries(
            manager,
            ClientPackSelection(
                selected_mod_names=frozenset({"optional-explicit.zip", "choice-explicit.zip"}),
                supplied=True,
            ),
            client_overrides_dir=None,
        )

        self.assertEqual(
            {entry.archive_path.as_posix() for entry in entries},
            {"required.zip", "client.zip", "optional-explicit.zip", "choice-explicit.zip"},
        )

    async def test_client_pack_rejects_multiple_explicit_alternatives(self) -> None:
        manager = await self._build_client_pack_manager()

        with self.assertRaisesRegex(ClientPackValidationError, "multiple selections"):
            build_client_pack_entries(
                manager,
                ClientPackSelection(
                    selected_mod_names=frozenset({"choice-default.zip", "choice-explicit.zip"}),
                    supplied=True,
                ),
                client_overrides_dir=None,
            )

    async def test_client_pack_does_not_bypass_download_block_for_required_file(self) -> None:
        manager = self._build_manager()
        blocked = await manager.add(self._write_source_file("blocked.zip"))
        blocked.cfg.classification_override = ModClassificationOverride(
            mod_type=ModType.CLIENT,
            download_block_reason=ModDownloadBlockReason.ARTIFACT,
        )

        with self.assertRaisesRegex(ClientPackValidationError, "must be downloadable"):
            build_client_pack_entries(
                manager,
                ClientPackSelection(),
                client_overrides_dir=None,
            )

    async def test_client_pack_rejects_non_downloadable_required_file_without_bundle_policy(self) -> None:
        manager = self._build_manager()
        blocked = await manager.add(self._write_source_file("blocked.zip"))
        blocked.cfg.classification_override = ModClassificationOverride(
            mod_type=ModType.CLIENT,
            download_block_reason=ModDownloadBlockReason.ARTIFACT,
        )

        with self.assertRaisesRegex(ClientPackValidationError, "must be downloadable"):
            build_client_pack_entries(
                manager,
                ClientPackSelection(),
                client_overrides_dir=None,
            )

    async def test_server_and_admin_pack_selection_respects_purpose(self) -> None:
        manager = self._build_manager()
        shared = await manager.add(self._write_source_file("shared.zip"))
        disabled = await manager.add(self._write_source_file("disabled.zip"))
        await manager.set_enabled(disabled, False)
        client = await manager.add(
            self._write_source_file("client.zip"),
            placement=ModPlacement.CLIENT_ONLY,
        )
        server = await manager.add(self._write_source_file("server.zip"))
        server.cfg.classification_override = ModClassificationOverride(
            mod_type=ModType.SERVER,
            download_block_reason=ModDownloadBlockReason.SERVER_ONLY,
        )

        server_entries = build_server_pack_entries(manager)
        admin_entries = build_admin_pack_entries(manager)

        self.assertEqual({entry.mod_name for entry in server_entries}, {shared.name, server.name})
        self.assertEqual(
            {entry.mod_name for entry in admin_entries},
            {shared.name, disabled.name, client.name, server.name},
        )

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
        await manager.set_download_block_reason(blocked, ModDownloadBlockReason.OTHER)

        paths = download_paths(manager, default_enabled_only=False)

        self.assertEqual(paths, (downloadable.path,))

        entries = download_entries(manager, default_enabled_only=False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].source_path, downloadable.storage_path)
        self.assertEqual(entries[0].archive_path.as_posix(), downloadable.logical_archive_name)
        self.assertEqual(entries[0].mod_name, downloadable.name)
        self.assertIs(entries[0].placement, ModPlacement.SERVER_ENABLED)
        self.assertIs(entries[0].mod_type, ModType.REGULAR)
        self.assertIs(entries[0].client_pack_policy, ClientPackPolicy.REQUIRED)

    async def test_download_paths_reject_blocked_direct_download(self) -> None:
        manager = self._build_manager()
        blocked = await manager.add(self._write_source_file("server-only.zip"))
        await manager.set_download_block_reason(blocked, ModDownloadBlockReason.OTHER)

        with self.assertRaises(NonDownloadableModError):
            download_paths(manager, (blocked.name,), default_enabled_only=False)


if __name__ == "__main__":
    unittest.main()
