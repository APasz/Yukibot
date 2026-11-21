import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import config
from apps._config import App_Config
from apps._mod_catalog import ModAction, ModCatalog, ModSourceKind
from apps._scs_truck_simulator import (
    ATS_PROFILE,
    ETS2_PROFILE,
    ScsServerPackageModSource,
    ScsTruckSimulatorProfile,
    scs_server_config_path,
    scs_server_package_paths,
)
from apps.ats import ATS
from apps.ets import ETS
from node_api.mod import NodeModEntry
from node_api.mod_service import NodeModService

_PACKAGE_FIXTURE = Path(__file__).parent / "fixtures" / "scs" / "server_packages.sii"


class ScsModTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory[str]()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)

    def _source(
        self, profile: ScsTruckSimulatorProfile = ETS2_PROFILE
    ) -> ScsServerPackageModSource:
        config_path = scs_server_config_path(self.root, profile=profile)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "server_config : Server {\n mods_optioning: true\n}\n", encoding="utf-8"
        )
        package_path, _ = scs_server_package_paths(self.root, profile=profile)
        package_path.write_bytes(_PACKAGE_FIXTURE.read_bytes())
        return ScsServerPackageModSource(self.root, profile=profile)

    async def test_both_games_expose_exported_mods_with_links_and_client_requirements(
        self,
    ) -> None:
        for profile in (ETS2_PROFILE, ATS_PROFILE):
            with self.subTest(game=profile.abbreviation):
                source = self._source(profile)
                catalog = ModCatalog((source,))
                await catalog.refresh()
                required, manual, optional = catalog.list_entries()
                self.assertTrue(source.status.healthy)
                self.assertEqual(required.friendly, "No Damage/Hasarsız")
                self.assertIn("truncated", required.description or "")
                self.assertEqual(manual.friendly, "ProMods Map Package")
                self.assertFalse(manual.mod_pages)
                self.assertEqual(optional.friendly, "Doubles Anywhere")
                self.assertIn("Export order: 3", optional.description or "")
                self.assertTrue(required.client_required)
                self.assertTrue(manual.client_required)
                self.assertFalse(optional.client_required)
                self.assertEqual(
                    required.mod_pages[0].url,
                    "https://steamcommunity.com/sharedfiles/filedetails/?id=3298337822",
                )
                for entry in catalog.list_entries():
                    self.assertIs(
                        entry.reference.source, ModSourceKind.SCS_SERVER_PACKAGES
                    )
                    self.assertFalse(entry.available_actions)
                    self.assertFalse(entry.downloadable)
                    self.assertFalse(entry.client_pack_eligible)
                    self.assertIsNone(entry.artifact)
                    node_entry = NodeModService._inventory_entry_to_node_entry(entry)
                    round_trip = NodeModEntry.from_mapping(node_entry.to_mapping())
                    self.assertEqual(round_trip.id, node_entry.id)
                    self.assertEqual(round_trip.client_required, entry.client_required)
                    self.assertFalse(round_trip.download_is_policy_blocked)
                    self.assertTrue(
                        all(
                            not round_trip.supports_action(action)
                            for action in ModAction
                        )
                    )

    async def test_optional_mods_require_server_option_and_refresh_when_it_changes(
        self,
    ) -> None:
        source = self._source()
        config_path = scs_server_config_path(self.root, profile=ETS2_PROFILE)
        for setting in (
            "mods_optioning: false",
            "",
            "mods_optioning: true // allow optional mods",
        ):
            with self.subTest(setting=setting):
                config_path.write_text(setting + "\n", encoding="utf-8")
                await source.refresh()
                self.assertTrue(source.status.healthy)
                self.assertEqual(
                    source.list_entries()[-1].client_required,
                    not setting.startswith("mods_optioning: true"),
                )

    async def test_missing_or_invalid_replacement_clears_old_requirements_and_recovers(
        self,
    ) -> None:
        source = self._source()
        catalog = ModCatalog((source,))
        package_path, _ = scs_server_package_paths(self.root, profile=ETS2_PROFILE)
        for replacement in (None, b"", b"SiiNunit\n{\n", b"\xff"):
            with self.subTest(replacement=replacement):
                package_path.write_bytes(_PACKAGE_FIXTURE.read_bytes())
                await catalog.refresh()
                self.assertEqual(len(catalog.list_entries()), 3)
                if replacement is None:
                    package_path.unlink()
                else:
                    package_path.write_bytes(replacement)
                await catalog.refresh()
                self.assertFalse(catalog.source_statuses[0].healthy)
                self.assertTrue(catalog.source_statuses[0].warning)
                self.assertFalse(catalog.source_statuses[0].using_cached_entries)
                self.assertEqual(catalog.list_entries(), ())

    async def test_invalid_mod_references_fields_and_counts_report_source_warning(
        self,
    ) -> None:
        source = self._source()
        package_path, _ = scs_server_package_paths(self.root, profile=ETS2_PROFILE)
        fixture = _PACKAGE_FIXTURE.read_text(encoding="utf-8")
        replacements = (
            ("mod_list: 3", "mod_list: 4"),
            ("mod_list[1]: _nameless.manual", "mod_list[1]: _nameless.missing"),
            ("mod_list[1]: _nameless.manual", "mod_list[1]: _nameless.required"),
            ("optional_mod: true", "optional_mod: maybe"),
            ("mod_id: 1113405579", "mod_id: zero"),
            ("full_name: true", "full_name: true\n full_name: false"),
            ("version: 1", "version: 2"),
            ('mod_name: "Doubles Anywhere"', 'mod_name: "Bad\\q"'),
            ('mod_name: "Doubles Anywhere"', 'mod_name: "Broken\\xc4"'),
            ('mod_name: "Doubles Anywhere"', 'mod_name: "Bad\\xff name"'),
            ('mod_name: "Doubles Anywhere"', 'mod_name: "   "'),
            ('package_name: "promods-eu-map-v280"', 'package_name: "   "'),
            ('package_name: "promods-eu-map-v280"', 'package_name: "broken\\xc4"'),
            (
                "server_packages_info : _nameless.info",
                "server_packages_info : _nameless.manual",
            ),
            (
                "server_mod_detail : _nameless.manual",
                "server_mod_detail : _nameless.required",
            ),
        )
        for old, new in replacements:
            with self.subTest(new=new):
                package_path.write_text(fixture.replace(old, new), encoding="utf-8")
                await source.refresh()
                self.assertFalse(source.status.healthy)
                self.assertEqual(source.list_entries(), ())

    async def test_truncated_utf8_display_name_is_allowed_only_at_the_end(self) -> None:
        source = self._source()
        package_path, _ = scs_server_package_paths(self.root, profile=ETS2_PROFILE)
        fixture = _PACKAGE_FIXTURE.read_text(encoding="utf-8")
        for name, valid in (
            (r"Truncated\xc4", True),
            (r"Broken\xff", False),
            (r"Bad\xc4 text", False),
        ):
            with self.subTest(name=name):
                package_path.write_text(
                    fixture.replace(r"No Damage/Hasars\xc4\xb1z", name),
                    encoding="utf-8",
                )
                await source.refresh()
                self.assertEqual(source.status.healthy, valid)
                if valid:
                    self.assertEqual(
                        source.list_entries()[0].friendly,
                        "Truncated\N{REPLACEMENT CHARACTER}",
                    )
                else:
                    self.assertEqual(source.list_entries(), ())

    async def test_package_strings_support_escaped_quotes_backslashes_and_comments(
        self,
    ) -> None:
        source = self._source()
        package_path, _ = scs_server_package_paths(self.root, profile=ETS2_PROFILE)
        fixture = _PACKAGE_FIXTURE.read_text(encoding="utf-8")
        package_path.write_text(
            "\ufeff/* Export header */\n"
            + fixture.replace(
                'mod_name: "Doubles Anywhere"',
                r'mod_name: "A \"quoted\" \\ name # // /* */" // trailing comment',
            ),
            encoding="utf-8",
        )
        await source.refresh()
        self.assertTrue(source.status.healthy)
        self.assertEqual(
            source.list_entries()[-1].friendly, 'A "quoted" \\ name # // /* */'
        )

    async def test_missing_server_config_reports_the_missing_config_without_requesting_package_uploads(
        self,
    ) -> None:
        source = self._source()
        config_path = scs_server_config_path(self.root, profile=ETS2_PROFILE)
        config_path.unlink()
        await source.refresh()
        self.assertFalse(source.status.healthy)
        self.assertIn("server_config.sii", source.status.warning or "")
        self.assertNotIn("Upload server_packages", source.status.warning or "")

    async def test_invalid_optional_mod_setting_clears_previous_requirements(
        self,
    ) -> None:
        source = self._source()
        await source.refresh()
        config_path = scs_server_config_path(self.root, profile=ETS2_PROFILE)
        config_path.write_text("mods_optioning: maybe\n", encoding="utf-8")
        await source.refresh()
        self.assertFalse(source.status.healthy)
        self.assertIn("mods_optioning", source.status.warning or "")
        self.assertEqual(source.list_entries(), ())

    async def test_unmodded_export_is_healthy_and_ignores_dlc_lists(self) -> None:
        source = self._source()
        package_path, _ = scs_server_package_paths(self.root, profile=ETS2_PROFILE)
        package_path.write_text(
            "SiiNunit\n{\nserver_packages_info : info {\n version: 1\n mod_list: 0\n"
            " dlc_essential_list: 1\n dlc_essential_list[0]: 123\n}\n}\n",
            encoding="utf-8",
        )
        await source.refresh()
        self.assertTrue(source.status.healthy)
        self.assertEqual(source.list_entries(), ())

    async def test_both_apps_register_the_read_only_source_without_a_local_mod_manager(
        self,
    ) -> None:
        for app_type in (ETS, ATS):
            with self.subTest(game=app_type.__name__):
                self._source(app_type.profile)
                cfg = App_Config(
                    name=f"{app_type.profile.scope}_test",
                    instance_key="test",
                    directory=self.root,
                    apps_dir=self.root,
                    scope=app_type.profile.scope,
                )
                with patch.object(config, "DIR_LOG", self.root / "logs"):
                    app = app_type(Mock(), Mock(), cfg)
                await app.post_init()
                self.assertIsNone(app.mods)
                self.assertEqual(len(app.has_mod_catalog.list_entries()), 3)
