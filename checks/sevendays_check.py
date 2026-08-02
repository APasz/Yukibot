import asyncio
import unittest
import zipfile
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Never, cast
from unittest.mock import AsyncMock, call, patch

import config
from _discord import Fileish, OutboundRelayFormatter, RelayMessageReferenceKind, RelayOutboundFormatOptions
from _mod_ops import ModArchiveEntry, _write_mod_archive, download_entries
from _security import Power_Level
from _utils import Utilities
from apps._config import (
    App_Config,
    AppVersion,
    Mod_Config,
    ModDownloadBlockReason,
    ModPageLink,
    ModPlacement,
    ModType,
)
from apps._console import ConsoleResponseSource, execute_console_action
from apps._mod import Mod_Manager
from apps.sevendays import (
    Activities as SevenDaysActivities,
)
from apps.sevendays import (
    Matchers,
    Mod_7D2D,
    Receiver,
    SevenDays,
    SevenDaysAdminAddRequest,
    SevenDaysSandboxOption,
    SevenDaysSandboxOptionsSnapshot,
    _candidate_sevendays_logs,
    _discover_sevendays_runtime_log,
    _preferred_sevendays_runtime_log,
    _sevendays_telnet_port,
    detect_sevendays_version,
    extract_sevendays_save_archive,
    inspect_sevendays_save_archive,
    parse_admin_add_value,
    parse_gamestat_value,
)


class _RecordingActivityManager:
    def __init__(self) -> None:
        self.registered: list[object] = []
        self.deregistered: list[object] = []

    def register(self, provider: object) -> None:
        self.registered.append(provider)

    def deregister(self, provider: object) -> None:
        self.deregistered.append(provider)


class _SevenDaysActivityAppStub:
    def __init__(self, activity_manager: _RecordingActivityManager) -> None:
        self.activity_manager = activity_manager
        self.name = "sevendays_alpha"
        self._tail_matchers: set[object] = set()
        self.providers: list[object] = []

    def set_activity_providers(self, providers: Sequence[object]) -> None:
        self.providers = list(providers)

    def register_enabled_activity_providers(self) -> None:
        for provider in self.providers:
            self.activity_manager.register(provider)

    def deregister_activity_providers(self) -> None:
        for provider in self.providers:
            self.activity_manager.deregister(provider)


class SevenDaysGameStatParsingTests(unittest.TestCase):
    def test_builtin_mods_are_not_downloadable(self) -> None:
        mod = Mod_7D2D(Mod_Config(name="0_TFP_Harmony", directory=Path(".")))

        self.assertFalse(mod.downloadable)
        self.assertEqual(mod.cfg.mod_type, ModType.BUILTIN)
        self.assertEqual(mod.cfg.download_block_reason, ModDownloadBlockReason.BUILTIN)

    def test_manager_detects_disabled_mod_by_modinfo_suffix(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            app_dir = apps_dir / "sevendays"
            mods_dir = app_dir / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml.disabled").write_text("<mod />", encoding="utf-8")
            app_cfg = App_Config(
                name="sevendays_alpha",
                instance_key="alpha",
                friendly_name="7D2D",
                directory=app_dir,
                apps_dir=apps_dir,
                mods_dir=mods_dir,
                join_host="127.0.0.1",
                scope="sevendays",
            )
            Mod_Manager._instances.clear()
            manager = Mod_Manager(app_cfg, mod_cls=Mod_7D2D, db_path=root / "mods.jsonl")

            try:
                asyncio.run(manager.reload_mods())
            finally:
                Mod_Manager._instances.clear()

            mod = manager.get("ExampleMod")
            entries = download_entries(
                manager,
                (mod.name,),
                default_enabled_only=False,
            )

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].source_path, mod_dir)

        self.assertFalse(mod.cfg.enabled)
        self.assertEqual(mod.path, mod_dir)

    def test_disable_renames_modinfo_xml_instead_of_the_mod_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            mod_info = mod_dir / "ModInfo.xml"
            mod_info.write_text("<mod />", encoding="utf-8")
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            asyncio.run(mod.disable())

            self.assertTrue(mod_dir.exists())
            self.assertFalse(mod_info.exists())
            self.assertTrue((mod_dir / "ModInfo.xml.disabled").exists())
            self.assertFalse(mod.cfg.enabled)

    def test_client_classification_renames_modinfo_xml_instead_of_the_mod_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            mod_info = mod_dir / "ModInfo.xml"
            mod_info.write_text("<mod />", encoding="utf-8")
            mod = Mod_7D2D(
                Mod_Config(
                    name="ExampleMod",
                    directory=mods_dir,
                    mod_type=ModType.CLIENT,
                )
            )

            mod.sync_metadata()

            self.assertTrue(mod_dir.exists())
            self.assertFalse(mod_info.exists())
            self.assertTrue((mod_dir / "ModInfo.xml.client").exists())
            self.assertFalse((mods_dir / "ExampleMod.client").exists())
            self.assertIs(mod.cfg.placement, ModPlacement.CLIENT_ONLY)
            self.assertEqual(mod.storage_path, mod_dir)

    def test_client_pack_download_strips_modinfo_client_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mods_dir = root / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml.client").write_text("<mod />", encoding="utf-8")
            (mod_dir / "Config" / "settings.xml").parent.mkdir()
            (mod_dir / "Config" / "settings.xml").write_text("<settings />", encoding="utf-8")
            mod = Mod_7D2D(
                Mod_Config(
                    name="ExampleMod",
                    directory=mods_dir,
                    placement=ModPlacement.CLIENT_ONLY,
                    mod_type=ModType.CLIENT,
                )
            )
            entries = (ModArchiveEntry.from_mod(mod),)

            archive_path = root / "client-pack.zip"
            _write_mod_archive(entries, archive_path)

            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())

            self.assertIn("ExampleMod/ModInfo.xml", names)
            self.assertIn("ExampleMod/Config/settings.xml", names)
            self.assertNotIn("ExampleMod/ModInfo.xml.client", names)

    def test_disabled_mod_download_strips_modinfo_disabled_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mods_dir = root / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml.disabled").write_text("<mod />", encoding="utf-8")
            (mod_dir / "Config" / "settings.xml").parent.mkdir()
            (mod_dir / "Config" / "settings.xml").write_text("<settings />", encoding="utf-8")
            mod = Mod_7D2D(
                Mod_Config(
                    name="ExampleMod",
                    directory=mods_dir,
                    placement=ModPlacement.SERVER_DISABLED,
                )
            )
            entries = (ModArchiveEntry.from_mod(mod),)

            archive_path = root / "disabled-mod.zip"
            _write_mod_archive(entries, archive_path)

            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())

            self.assertIn("ExampleMod/ModInfo.xml", names)
            self.assertIn("ExampleMod/Config/settings.xml", names)
            self.assertNotIn("ExampleMod/ModInfo.xml.disabled", names)

    def test_detect_version_reads_modinfo_xml(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8" ?>
<xml>
    <Version value="1.2.3.4" />
</xml>""",
                encoding="utf-8",
            )
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            self.assertEqual(mod.detect_version(), "1.2.3.4")

    def test_detect_friendly_reads_display_name_from_modinfo_xml(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8" ?>
<xml>
    <DisplayName value="Better Loot" />
</xml>""",
                encoding="utf-8",
            )
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            mod.sync_metadata()

            self.assertEqual(mod.friendly, "Better Loot")

    def test_detect_description_reads_description_from_modinfo_xml(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8" ?>
<xml>
    <Description value="Adds better loot to world containers." />
</xml>""",
                encoding="utf-8",
            )
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            self.assertEqual(mod.description, "Adds better loot to world containers.")

    def test_sync_metadata_adds_supported_modinfo_website_as_mod_page(self) -> None:
        cases = (
            ("https://www.nexusmods.com/7daystodie/mods/123", "NexusMods"),
            (
                "https://7daystodiemods.com/mods/craftfromcontainerplus",
                "7D2Dmods",
            ),
        )
        for website, expected_name in cases:
            with self.subTest(website=website), TemporaryDirectory() as tmp:
                mods_dir = Path(tmp) / "Mods"
                mod_dir = mods_dir / "ExampleMod"
                mod_dir.mkdir(parents=True)
                (mod_dir / "ModInfo.xml").write_text(
                    f'<xml><Website value="{website}" /></xml>',
                    encoding="utf-8",
                )
                mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

                mod.sync_metadata()

                self.assertEqual(
                    mod.cfg.mod_pages,
                    (ModPageLink(name=expected_name, url=website),),
                )

    def test_sync_metadata_reads_website_from_disabled_modinfo(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            website = "https://7daystodie.nexusmods.com/mods/456"
            (mod_dir / "ModInfo.xml.disabled").write_text(
                f'<xml><Website value="{website}" /></xml>',
                encoding="utf-8",
            )
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            mod.sync_metadata()

            self.assertFalse(mod.cfg.enabled)
            self.assertEqual(
                mod.cfg.mod_pages,
                (ModPageLink(name="NexusMods", url=website),),
            )

    def test_sync_metadata_preserves_existing_page_for_detected_provider(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml").write_text(
                '<xml><Website value="https://www.nexusmods.com/7daystodie/mods/123" /></xml>',
                encoding="utf-8",
            )
            existing_page = ModPageLink(
                name="NexusMods",
                url="https://www.nexusmods.com/7daystodie/mods/456",
            )
            mod = Mod_7D2D(
                Mod_Config(name="ExampleMod", directory=mods_dir, mod_pages=(existing_page,))
            )

            mod.sync_metadata()

            self.assertEqual(mod.cfg.mod_pages, (existing_page,))

    def test_sync_metadata_ignores_non_project_modinfo_websites(self) -> None:
        websites = (
            "https://www.nexusmods.com/skyrimspecialedition/mods/123",
            "https://www.nexusmods.com/7daystodie/users/123",
            "https://7daystodiemods.com/",
            "https://7daystodiemods.com/about",
            "https://example.com/example-mod",
            "http://www.nexusmods.com/7daystodie/mods/123",
        )
        for website in websites:
            with self.subTest(website=website), TemporaryDirectory() as tmp:
                mods_dir = Path(tmp) / "Mods"
                mod_dir = mods_dir / "ExampleMod"
                mod_dir.mkdir(parents=True)
                (mod_dir / "ModInfo.xml").write_text(
                    f'<xml><Website value="{website}" /></xml>',
                    encoding="utf-8",
                )
                mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

                mod.sync_metadata()

                self.assertEqual(mod.cfg.mod_pages, ())

    def test_detect_sevendays_version_from_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "server_stdout.log"
            log_path.write_text(
                "2025-06-14T09:47:42 0.030 INF Version: V 1.4 (b8) Compatibility Version: V 1.4, Build: LinuxServer 64 Bit\n",
                encoding="utf-8",
            )

            version = detect_sevendays_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="1.4", build=8))

    def test_detect_sevendays_version_accepts_spaced_build_suffix(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "server_stdout.log"
            log_path.write_text(
                "2026-06-26T10:01:00 0.030 INF Version: V 3.0 B259 Compatibility Version: V 3.0\n",
                encoding="utf-8",
            )

            version = detect_sevendays_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="3.0", build=259))

    def test_candidate_sevendays_logs_include_timestamped_output_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir()
            older = log_dir / "output_log__2026-06-17__02-27-35.txt"
            newer = log_dir / "output_log__2026-06-17__03-27-35.txt"
            older.write_text("older", encoding="utf-8")
            newer.write_text("newer", encoding="utf-8")

            candidates = _candidate_sevendays_logs(directory=root, server_log=None)

        self.assertIn(newer, candidates)
        self.assertIn(older, candidates)
        self.assertLess(candidates.index(newer), candidates.index(older))

    def test_candidate_sevendays_logs_include_root_timestamped_output_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_log = root / "output_log__2026-06-27__06-40-00.txt"
            runtime_log.write_text("current", encoding="utf-8")

            candidates = _candidate_sevendays_logs(directory=root, server_log=None)

        self.assertIn(runtime_log, candidates)

    def test_telnet_port_is_read_from_serverconfig(self) -> None:
        with TemporaryDirectory() as tmp:
            serverconfig = Path(tmp) / "serverconfig.xml"
            serverconfig.write_text(
                '<ServerSettings><property name="TelnetEnabled" value="true" />'
                '<property name="TelnetPort" value="18081" /></ServerSettings>',
                encoding="utf-8",
            )

            port = _sevendays_telnet_port(serverconfig)

        self.assertEqual(port, 18081)

    def test_disabled_telnet_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            serverconfig = Path(tmp) / "serverconfig.xml"
            serverconfig.write_text(
                '<ServerSettings><property name="TelnetEnabled" value="false" /></ServerSettings>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be enabled"):
                _sevendays_telnet_port(serverconfig)

    def test_detect_sevendays_version_from_timestamped_output_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir()
            log_path = log_dir / "output_log__2026-06-17__02-27-35.txt"
            log_path.write_text(
                "2026-06-17T02:27:35 0.030 INF Version: V 2.0 (b1) Compatibility Version: V 2.0\n",
                encoding="utf-8",
            )

            version = detect_sevendays_version(directory=root, server_log=None)

        self.assertEqual(version, AppVersion(main="2.0", build=1))

    def test_preferred_sevendays_runtime_log_prefers_launch_created_timestamped_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir()
            older = log_dir / "output_log__2026-06-17__02-27-35.txt"
            newer = log_dir / "output_log__2026-06-17__03-27-35.txt"
            older.write_text("older", encoding="utf-8")
            newer.write_text("newer", encoding="utf-8")

            preferred = _preferred_sevendays_runtime_log(
                directory=root,
                server_log=None,
                previous_timestamped_logs={older},
            )

        self.assertEqual(preferred, newer)

    def test_app_config_parses_save_file_write_level_override(self) -> None:
        cfg = App_Config(
            name="sevendays_alpha",
            instance_key="alpha",
            directory=Path("/srv/7d2d"),
            apps_dir=Path("/srv/apps"),
            scope="sevendays",
            save_file_write_level_override=Power_Level.admin,
        )

        self.assertEqual(cfg.save_file_write_level_override, Power_Level.admin)

    def test_save_file_roots_default_to_managed_userdata_folder_without_a_redirect(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "serverconfig.xml").write_text(
                """<?xml version="1.0"?>
<ServerSettings>
    <property name="GameWorld" value="Navezgane" />
    <property name="GameName" value="AlphaWorld" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir

            roots = app.save_file_roots

            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].path, app_dir / "userdata" / "Saves" / "Navezgane" / "AlphaWorld")
            self.assertTrue(app.supports_save_uploads)

    def test_save_file_roots_use_redirected_userdata_folder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            app_dir.mkdir()
            userdata_dir.mkdir()
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
    <property name="GameWorld" value="Navezgane" />
    <property name="GameName" value="AlphaWorld" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir

            roots = app.save_file_roots

            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].path, userdata_dir / "Saves" / "Navezgane" / "AlphaWorld")
            self.assertTrue(app.supports_save_uploads)

    def test_list_save_files_discovers_redirected_saves_without_current_game_selection(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            alpha_save_dir = userdata_dir / "Saves" / "Navezgane" / "AlphaWorld"
            bravo_save_dir = userdata_dir / "Saves" / "Pregen10k" / "BravoWorld"
            app_dir.mkdir()
            alpha_save_dir.mkdir(parents=True)
            bravo_save_dir.mkdir(parents=True)
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir

            saves = app.list_save_files()

            self.assertEqual(
                tuple((save.root_label, save.label) for save in saves),
                (("Navezgane", "AlphaWorld"), ("Pregen10k", "BravoWorld")),
            )
            self.assertTrue(app.supports_save_uploads)

    def test_delete_save_file_removes_redirected_discovered_save_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Navezgane" / "AlphaWorld"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            (save_dir / "region.dat").write_text("save-data", encoding="utf-8")
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
    <property name="GameWorld" value="Navezgane" />
    <property name="GameName" value="AlphaWorld" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            save_id = next(save.id for save in app.list_save_files() if save.label == "AlphaWorld")
            deleted = app.delete_save_file(file_id=save_id)

            self.assertEqual(deleted.label, "AlphaWorld")
            self.assertFalse(save_dir.exists())

    def test_delete_generated_world_requires_its_saves_to_be_deleted_first(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Wizefoco Mountains" / "AlphaWorld"
            generated_world_dir = userdata_dir / "GeneratedWorlds" / "Wizefoco Mountains"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            generated_world_dir.mkdir(parents=True)
            (save_dir / "main.ttw").write_text("save-data", encoding="utf-8")
            (generated_world_dir / "GenerationInfo.txt").write_text("world-data", encoding="utf-8")
            (app_dir / "serverconfig.xml").write_text(
                f'''<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
''',
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            saves = app.list_save_files()
            save_id = next(save.id for save in saves if save.label == "AlphaWorld")
            world_id = next(save.id for save in saves if save.label == "Wizefoco Mountains")

            with self.assertRaisesRegex(ValueError, "Delete the saves"):
                app.delete_save_file(file_id=world_id)
            app.delete_save_file(file_id=save_id)
            deleted_world = app.delete_save_file(file_id=world_id)

            self.assertEqual(deleted_world.label, "Wizefoco Mountains")
            self.assertFalse(generated_world_dir.exists())

    def test_sevendays_save_archive_inspection_accepts_direct_save_contents(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "Archive.zip"
            destination = root / "extracted"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("main.ttw", "main")
                archive.writestr("ConfigsDump/events.xml", "<events />")

            inspection = extract_sevendays_save_archive(archive_path=archive_path, destination=destination)

            self.assertEqual(inspection.content_prefix, ())
            self.assertIsNone(inspection.game_world)
            self.assertIsNone(inspection.game_name)
            self.assertEqual((destination / "main.ttw").read_text(encoding="utf-8"), "main")
            self.assertFalse((destination / "Archive" / "main.ttw").exists())

    def test_sevendays_save_archive_inspection_accepts_game_folder_layer(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "woabewbies.zip"
            destination = root / "extracted"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("woabewbies/main.ttw", "main")
                archive.writestr("woabewbies/Region/r.0.0.7rg", "region")

            inspection = extract_sevendays_save_archive(archive_path=archive_path, destination=destination)

            self.assertEqual(inspection.content_prefix, ("woabewbies",))
            self.assertIsNone(inspection.game_world)
            self.assertEqual(inspection.game_name, "woabewbies")
            self.assertEqual((destination / "main.ttw").read_text(encoding="utf-8"), "main")
            self.assertFalse((destination / "woabewbies" / "main.ttw").exists())

    def test_sevendays_save_archive_inspection_accepts_world_and_game_folder_layers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "Wizefoco Mountains.zip"
            destination = root / "extracted"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Wizefoco Mountains/woabewbies/main.ttw", "main")
                archive.writestr("Wizefoco Mountains/woabewbies/Player/player.ttp", "player")

            inspection = extract_sevendays_save_archive(archive_path=archive_path, destination=destination)

            self.assertEqual(inspection.content_prefix, ("Wizefoco Mountains", "woabewbies"))
            self.assertEqual(inspection.game_world, "Wizefoco Mountains")
            self.assertEqual(inspection.game_name, "woabewbies")
            self.assertEqual((destination / "main.ttw").read_text(encoding="utf-8"), "main")
            self.assertFalse((destination / "Wizefoco Mountains" / "woabewbies" / "main.ttw").exists())

    def test_sevendays_save_archive_inspection_accepts_portable_generated_world_bundle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "portable-world.zip"
            save_destination = root / "save"
            generated_world_destination = root / "generated-world"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Saves/Wizefoco Mountains/woabewbies/main.ttw", "main")
                archive.writestr("Saves/Wizefoco Mountains/woabewbies/Player/player.ttp", "player")
                archive.writestr("GeneratedWorlds/Wizefoco Mountains/GenerationInfo.txt", "world")
                archive.writestr("GeneratedWorlds/Wizefoco Mountains/prefabs.xml", "<prefabs />")

            inspection = inspect_sevendays_save_archive(archive_path)
            extracted = extract_sevendays_save_archive(
                archive_path=archive_path,
                destination=save_destination,
                generated_world_destination=generated_world_destination,
                inspection=inspection,
            )

            self.assertEqual(extracted.game_world, "Wizefoco Mountains")
            self.assertEqual(extracted.game_name, "woabewbies")
            self.assertEqual(extracted.generated_world, "Wizefoco Mountains")
            self.assertTrue(extracted.includes_generated_world)
            self.assertEqual((save_destination / "main.ttw").read_text(encoding="utf-8"), "main")
            self.assertEqual(
                (generated_world_destination / "GenerationInfo.txt").read_text(encoding="utf-8"), "world"
            )

    def test_sevendays_save_archive_inspection_accepts_generated_world_without_save(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "fresh-world.zip"
            generated_world_destination = root / "generated-world"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Wizefoco Mountains/GenerationInfo.txt", "world")
                archive.writestr("Wizefoco Mountains/prefabs.xml", "<prefabs />")

            inspection = extract_sevendays_save_archive(
                archive_path=archive_path,
                generated_world_destination=generated_world_destination,
            )

            self.assertFalse(inspection.includes_save)
            self.assertEqual(inspection.game_world, "Wizefoco Mountains")
            self.assertIsNone(inspection.game_name)
            self.assertEqual(
                (generated_world_destination / "GenerationInfo.txt").read_text(encoding="utf-8"), "world"
            )

    def test_sevendays_save_archive_does_not_mistake_generated_world_regions_for_a_save(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "fresh-world.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("GeneratedWorlds/Wizefoco Mountains/GenerationInfo.txt", "world")
                archive.writestr("GeneratedWorlds/Wizefoco Mountains/region/r.0.0.7rg", "region")

            inspection = inspect_sevendays_save_archive(archive_path)

            self.assertFalse(inspection.includes_save)
            self.assertTrue(inspection.includes_generated_world)
            self.assertEqual(inspection.game_world, "Wizefoco Mountains")

    def test_sevendays_save_archive_inspection_rejects_multiple_save_roots(self) -> None:
        with TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Alpha/main.ttw", "main")
                archive.writestr("Bravo/main.ttw", "main")

            with self.assertRaisesRegex(ValueError, "multiple save roots"):
                inspect_sevendays_save_archive(archive_path)

    def test_upload_save_file_requires_stopped_server(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Navezgane" / "AlphaWorld"
            archive_path = root / "upload.zip"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("AlphaWorld/main.ttw", "save-data")
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: True

            save_root_id = app.save_file_roots[0].id
            with self.assertRaisesRegex(ValueError, "Stop the server"):
                app.upload_save_file(root_id=save_root_id, upload_name="upload.zip", source_path=archive_path)

    def test_upload_save_file_rejects_replacing_an_existing_save(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Navezgane" / "AlphaWorld"
            archive_path = root / "upload.zip"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            (save_dir / "old.txt").write_text("old", encoding="utf-8")
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Wizefoco Mountains/woabewbies/main.ttw", "main")
                archive.writestr("Wizefoco Mountains/woabewbies/Region/r.0.0.7rg", "region")
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            with self.assertRaisesRegex(ValueError, "must import a new"):
                app.upload_save_file(
                    root_id=app.save_file_roots[0].id,
                    upload_name="upload.zip",
                    source_path=archive_path,
                )

            self.assertEqual((save_dir / "old.txt").read_text(encoding="utf-8"), "old")

    def test_upload_new_save_file_creates_requested_world_and_save_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            archive_path = root / "upload.zip"
            app_dir.mkdir()
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ImportedSave/main.ttw", "save-data")
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            uploaded = app.upload_save_file(
                root_id=SevenDays.new_save_upload_root_id(game_world="RWG", game_name="ImportedSave"),
                upload_name="upload.zip",
                source_path=archive_path,
            )

            self.assertEqual(uploaded.root_label, "RWG")
            self.assertEqual(uploaded.label, "ImportedSave")
            self.assertEqual((userdata_dir / "Saves" / "RWG" / "ImportedSave" / "main.ttw").read_text(), "save-data")

    def test_upload_portable_world_archive_creates_new_save_and_generated_world(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Wizefoco Mountains" / "woabewbies"
            generated_world_dir = userdata_dir / "GeneratedWorlds" / "Wizefoco Mountains"
            archive_path = root / "portable-world.zip"
            app_dir.mkdir()
            (app_dir / "serverconfig.xml").write_text(
                f'''<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
''',
                encoding="utf-8",
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Saves/Wizefoco Mountains/woabewbies/main.ttw", "save-data")
                archive.writestr("GeneratedWorlds/Wizefoco Mountains/GenerationInfo.txt", "world-data")
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            uploaded = app.upload_save_file(
                root_id=SevenDays.new_world_upload_root_id(
                    game_world="Wizefoco Mountains",
                    game_name="woabewbies",
                ),
                upload_name=archive_path.name,
                source_path=archive_path,
            )

            self.assertEqual(uploaded.label, "woabewbies")
            self.assertEqual((save_dir / "main.ttw").read_text(encoding="utf-8"), "save-data")
            self.assertEqual(
                (generated_world_dir / "GenerationInfo.txt").read_text(encoding="utf-8"), "world-data"
            )

    def test_download_generated_world_save_creates_portable_archive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Wizefoco Mountains" / "woabewbies"
            generated_world_dir = userdata_dir / "GeneratedWorlds" / "Wizefoco Mountains"
            archive_path = root / "portable-world.zip"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            generated_world_dir.mkdir(parents=True)
            (save_dir / "main.ttw").write_text("save-data", encoding="utf-8")
            (generated_world_dir / "GenerationInfo.txt").write_text("world-data", encoding="utf-8")
            (app_dir / "serverconfig.xml").write_text(
                f'''<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
''',
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.name = "sevendays_test"
            save_id = app.save_file_roots[0].id + "/woabewbies"

            with patch("apps.sevendays.File_Utils.compress", new=AsyncMock(return_value=archive_path)) as compress:
                filename, downloaded_archive_path = asyncio.run(app.download_save_archive(save_id))

            self.assertEqual(filename, archive_path.name)
            self.assertEqual(downloaded_archive_path, archive_path)
            compress.assert_awaited_once_with(
                (save_dir, generated_world_dir),
                "sevendays_test_Wizefoco Mountains_woabewbies.zip",
                arc_base=userdata_dir,
            )

    def test_upload_generated_world_without_save_leaves_fresh_save_path_empty(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = app_dir / "userdata"
            archive_path = root / "fresh-world.zip"
            app_dir.mkdir()
            (app_dir / "serverconfig.xml").write_text(
                '''<?xml version="1.0"?>
<ServerSettings>
</ServerSettings>
''',
                encoding="utf-8",
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("GeneratedWorlds/Wizefoco Mountains/GenerationInfo.txt", "world-data")
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            uploaded = app.upload_save_file(
                root_id=SevenDays.new_world_upload_root_id(game_world="Wizefoco Mountains"),
                upload_name=archive_path.name,
                source_path=archive_path,
            )

            generated_world_dir = userdata_dir / "GeneratedWorlds" / "Wizefoco Mountains"
            self.assertEqual(uploaded.label, "Wizefoco Mountains")
            self.assertEqual((generated_world_dir / "GenerationInfo.txt").read_text(encoding="utf-8"), "world-data")
            self.assertFalse((userdata_dir / "Saves" / "Wizefoco Mountains" / "FreshSave").exists())
            self.assertIn(
                'name="UserDataFolder" value="userdata"',
                (app_dir / "serverconfig.xml").read_text(encoding="utf-8"),
            )

    def test_upload_new_save_file_rejects_existing_save_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "RWG" / "ImportedSave"
            archive_path = root / "upload.zip"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            (save_dir / "main.ttw").write_text("existing", encoding="utf-8")
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ImportedSave/main.ttw", "replacement")
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                app.upload_save_file(
                    root_id=SevenDays.new_save_upload_root_id(game_world="RWG", game_name="ImportedSave"),
                    upload_name="upload.zip",
                    source_path=archive_path,
                )
            self.assertEqual((save_dir / "main.ttw").read_text(encoding="utf-8"), "existing")

    def test_parse_gamestat_value_returns_none_for_empty_value(self) -> None:
        self.assertIsNone(parse_gamestat_value(""))

    def test_parse_gamestat_value_parses_python_literals(self) -> None:
        self.assertEqual(parse_gamestat_value("42"), 42)
        self.assertEqual(parse_gamestat_value("3.5"), 3.5)
        self.assertEqual(parse_gamestat_value("True"), True)
        self.assertEqual(parse_gamestat_value("'hello'"), "hello")

    def test_parse_gamestat_value_preserves_bare_identifier_as_string(self) -> None:
        self.assertEqual(parse_gamestat_value("XPOnly"), "XPOnly")

    def test_parse_admin_add_value_supports_pipe_separator(self) -> None:
        self.assertEqual(
            parse_admin_add_value("EOS_123456789 | 0"),
            SevenDaysAdminAddRequest(subject="EOS_123456789", permission_level=0),
        )

    def test_parse_admin_add_value_supports_trailing_level(self) -> None:
        self.assertEqual(
            parse_admin_add_value("Alice 100"),
            SevenDaysAdminAddRequest(subject="Alice", permission_level=100),
        )

    def test_parse_admin_add_value_rejects_missing_level(self) -> None:
        with self.assertRaises(ValueError):
            parse_admin_add_value("Alice")


class SevenDaysRelayFormattingTests(unittest.IsolatedAsyncioTestCase):
    def test_outbound_formatter_appends_public_urls_for_attachments(self) -> None:
        payload = SimpleNamespace(
            urls=set(),
            files={Fileish("/tmp/cat.png", "cat.png")},
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        with patch(
            "_discord.Utilities.linkify", return_value=("https://public.example/uploads/cat.png", Path("/tmp/cat.png"))
        ):
            formatted = OutboundRelayFormatter.format_payload(
                cast(Any, payload),
                RelayOutboundFormatOptions(base_content="look"),
            )

        self.assertEqual(formatted, "look https://public.example/uploads/cat.png")


class SevenDaysConsoleActionTests(unittest.IsolatedAsyncioTestCase):
    def _console_app(self, *, version: AppVersion | None = None) -> SevenDays:
        app = cast(SevenDays, object.__new__(SevenDays))
        app.friendly = "7D2D Test"
        app.cfg = SimpleNamespace(version=version)
        app._relay = SimpleNamespace(send=AsyncMock(return_value=True))
        return app

    async def test_raw_console_command_sends_telnet_command(self) -> None:
        app = self._console_app()
        action = next(action for action in app.console_actions if action.key == "raw_command")

        result = await execute_console_action(
            app=app,
            is_running=lambda: True,
            action=action,
            raw_value="mem",
        )

        app._relay.send.assert_awaited_once_with("mem")
        self.assertEqual(result.summary, "7D2D Test: console command sent.")
        self.assertEqual(result.source, ConsoleResponseSource.TELNET)

    async def test_shutdown_sends_saveworld_before_shutdown(self) -> None:
        app = self._console_app()
        action = next(action for action in app.console_actions if action.key == "shutdown")

        result = await execute_console_action(
            app=app,
            is_running=lambda: True,
            action=action,
            raw_value=None,
        )

        self.assertEqual(
            app._relay.send.await_args_list,
            [
                call("saveworld"),
                call("shutdown"),
            ],
        )
        self.assertEqual(result.summary, "7D2D Test: world save and shutdown requested.")
        self.assertEqual(result.source, ConsoleResponseSource.TELNET)

    async def test_getsandboxoptions_sends_telnet_command_for_supported_versions(self) -> None:
        app = self._console_app(version=AppVersion(main="3.0", build=259))
        action = next(action for action in app.console_actions if action.key == "getsandboxoptions")

        result = await execute_console_action(
            app=app,
            is_running=lambda: True,
            action=action,
            raw_value=None,
        )

        app._relay.send.assert_awaited_once_with("getsandboxoptions")
        self.assertEqual(result.summary, "7D2D Test: sandbox options requested.")
        self.assertEqual(result.text, "Sandbox options are written to the 7D2D stdout feed.")
        self.assertEqual(result.source, ConsoleResponseSource.TELNET)

    async def test_startup_sandbox_options_request_is_version_gated(self) -> None:
        unsupported_app = self._console_app(version=AppVersion(main="3.0", build=258))
        await unsupported_app._request_startup_sandbox_options(delay_seconds=0.0, max_attempts=1)
        unsupported_app._relay.send.assert_not_awaited()

        supported_app = self._console_app(version=AppVersion(main="3.0", build=259))
        await supported_app._request_startup_sandbox_options(delay_seconds=0.0, max_attempts=1)
        supported_app._relay.send.assert_awaited_once_with("getsandboxoptions")

    def test_supports_sevendays_sandbox_options_is_version_gated_not_file_gated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = cast(SevenDays, object.__new__(SevenDays))
            app.directory = Path(temp_dir)
            app.cfg = App_Config(
                name="sevendays_alpha",
                instance_key="alpha",
                directory=Path(temp_dir),
                apps_dir=Path(temp_dir),
                scope="sevendays",
                version=AppVersion(main="3.0", build=259),
            )

            self.assertTrue(app.supports_sevendays_sandbox_options)
            self.assertFalse(app.sandbox_options_file_exists)

    async def test_sandbox_options_matcher_persists_parsed_stdout_dump(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = cast(SevenDays, object.__new__(SevenDays))
            app.name = "sevendays_alpha"
            app.directory = Path(temp_dir)
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                directory=Path(temp_dir),
                apps_dir=Path(temp_dir),
                scope="sevendays",
                version=AppVersion(main="3.0", build=259),
            )
            app._tail_matchers = set()
            matcher = Matchers(app)

            await matcher.match_sandbox_options("2026-06-26T10:17:36 2575.058 INF Sandbox Code: AACK")
            await matcher.match_sandbox_options("2026-06-26T10:17:36 2575.058 INF Sandbox Options:")
            await matcher.match_sandbox_options("2026-06-26T10:17:36 2575.058 INF *** GENERAL ***")
            await matcher.match_sandbox_options(
                "2026-06-26T10:17:36 2575.058 INF Option BlockDamage: 10/200% (default: 7/100%)"
            )

            snapshot = app.load_sandbox_options_snapshot()

        self.assertEqual(snapshot.sandbox_code, "AACK")
        self.assertEqual(snapshot.app_version, "3.0:259")
        self.assertEqual(
            snapshot.options,
            (
                SevenDaysSandboxOption(
                    section="General",
                    key="BlockDamage",
                    value_index=10,
                    value_label="200%",
                    default_index=7,
                    default_label="100%",
                ),
            ),
        )

    def test_sandbox_options_snapshot_round_trips_mapping(self) -> None:
        snapshot = SevenDaysSandboxOptionsSnapshot(
            generated_at="2026-06-26T10:17:36",
            sandbox_code="AACK",
            app_version="3.0:259",
            options=(
                SevenDaysSandboxOption(
                    section="General",
                    key="BlockDamage",
                    value_index=10,
                    value_label="200%",
                    default_index=7,
                    default_label="100%",
                ),
            ),
        )

        parsed = SevenDaysSandboxOptionsSnapshot.from_mapping(snapshot.to_mapping())

        self.assertEqual(parsed, snapshot)


class SevenDaysActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sevendays_activities_track_started_tasks_and_deregister_provider(self) -> None:
        activity_manager = _RecordingActivityManager()

        async def background_worker() -> Never:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        app = _SevenDaysActivityAppStub(activity_manager)
        activities = SevenDaysActivities(cast(Any, app))
        provider = activities.providers[0]
        provider.task_funcs = [background_worker]

        await activities.start()

        self.assertEqual(activity_manager.registered, [provider])
        self.assertEqual(len(activities.tasks), 1)

        await activities.stop()

        self.assertEqual(activity_manager.deregistered, [provider])
        self.assertEqual(activities.tasks, set())

    def test_outbound_formatter_uses_percent_encoded_public_urls_for_attachments(self) -> None:
        payload = SimpleNamespace(
            urls=set(),
            files={Fileish("/tmp/cat pic.png", "cat pic.png")},
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        with patch(
            "_discord.Utilities.linkify",
            return_value=("https://public.example/uploads/cat%20pic.png", Path("/tmp/cat pic.png")),
        ):
            formatted = OutboundRelayFormatter.format_payload(
                cast(Any, payload),
                RelayOutboundFormatOptions(base_content="look"),
            )

        self.assertEqual(formatted, "look https://public.example/uploads/cat%20pic.png")

    def test_linkify_percent_encodes_attachment_file_names(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "cat pic.png"
            source.write_text("meow", encoding="utf-8")
            upload_dir = root / "uploads"
            upload_dir.mkdir()

            with (
                patch.object(config, "DIR_UPLOAD", upload_dir),
                patch.object(config, "PUBLIC_UPLOADS_BASE_URL", "https://public.example/uploads/"),
            ):
                link, uploaded = Utilities.linkify(source)

        self.assertEqual(link, "https://public.example/uploads/cat%20pic.png")
        self.assertEqual(uploaded, source)

    def test_outbound_formatter_prefixes_reply_indicator(self) -> None:
        payload = SimpleNamespace(
            urls=set(),
            files=set(),
            reference_kind=RelayMessageReferenceKind.REPLY,
        )

        formatted = OutboundRelayFormatter.format_payload(
            cast(Any, payload),
            RelayOutboundFormatOptions(
                base_content="look",
                reference_renderer=lambda kind: "reply;" if kind is RelayMessageReferenceKind.REPLY else None,
            ),
        )

        self.assertEqual(formatted, "reply; look")

    async def test_receiver_appends_public_urls_for_attachments(self) -> None:
        app = SimpleNamespace(_relay=SimpleNamespace(send=AsyncMock()))
        receiver = Receiver(cast(Any, app))
        payload = SimpleNamespace(
            alias="Erin",
            content_demojised="look",
            urls=set(),
            files={Fileish("/tmp/cat.png", "cat.png")},
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        with patch(
            "_discord.Utilities.linkify", return_value=("https://public.example/uploads/cat.png", Path("/tmp/cat.png"))
        ):
            await receiver.send(cast(Any, payload))

        sent_command = app._relay.send.await_args.args[0]
        self.assertEqual(sent_command, 'say "Erin: look https://public.example/uploads/cat.png"')

    async def test_receiver_prefixes_forward_indicator(self) -> None:
        app = SimpleNamespace(_relay=SimpleNamespace(send=AsyncMock()))
        receiver = Receiver(cast(Any, app))
        payload = SimpleNamespace(
            alias="Erin",
            content_demojised="look",
            urls=set(),
            files=set(),
            reference_kind=RelayMessageReferenceKind.FORWARD,
        )

        await receiver.send(cast(Any, payload))

        sent_command = app._relay.send.await_args.args[0]
        self.assertEqual(sent_command, 'say "Erin: forwarded; look"')

    async def test_receiver_quotes_embedded_double_quotes(self) -> None:
        app = SimpleNamespace(_relay=SimpleNamespace(send=AsyncMock()))
        receiver = Receiver(cast(Any, app))
        payload = SimpleNamespace(
            alias='Erin "Admin"',
            content_demojised='say "hi"',
            urls=set(),
            files=set(),
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        await receiver.send(cast(Any, payload))

        sent_command = app._relay.send.await_args.args[0]
        self.assertEqual(sent_command, 'say "Erin \'Admin\': say \'hi\'"')


class SevenDaysRelayMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_death_relays_player_death(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app.scope = "sevendays"
        app.cfg = SimpleNamespace(relay_notice_player_death=True)
        app._tail_matchers = set()
        app._server_ready = asyncio.Event()
        matcher = Matchers(app)

        with patch("apps.sevendays.DC_Relay.add") as add_mock:
            await matcher.match_death("2026-05-27T22:07:40 8405.351 INF GMSG: Player 'asdblackmea' died")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "asdblackmea")
        self.assertEqual(relayed_message.content, "asdblackmea died")

    async def test_match_ready_sets_server_ready_event(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app.scope = "sevendays"
        app._tail_matchers = set()
        app._server_ready = asyncio.Event()
        matcher = Matchers(app)

        await matcher.match_ready("2026-05-23T11:19:12 8.590 INF StartAsServer")

        self.assertTrue(app._server_ready.is_set())

    async def test_start_waits_for_ready_signal_before_marking_app_running(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app.directory = Path("/tmp/sevendays_demo")
        app.server_log = None
        app.file_stdout = Path("/tmp/sevendays_stdout.log")
        app.process = SimpleNamespace(stdout=object())
        app._server_ready = asyncio.Event()
        app._telnet_startup_error = None
        app._tail_matchers = set()
        app._std_launch = AsyncMock()
        app._configure_telnet_client = AsyncMock()
        app.check_running = lambda: True
        relay_reader = object()
        app._relay = SimpleNamespace(
            setup=AsyncMock(return_value=relay_reader),
            connected_event=asyncio.Event(),
        )
        call_order: list[str] = []

        async def wait_for_ready_event(*args: object, **kwargs: object) -> None:
            self.assertFalse(app._running)
            self.assertEqual(args, (app._server_ready,))
            self.assertEqual(kwargs, {"timeout_seconds": 900.0, "ready_label": "server readiness"})
            call_order.append("ready")

        async def start_players() -> None:
            call_order.append("players")

        async def start_activities() -> None:
            call_order.append("activities")

        app.wait_for_ready_event = wait_for_ready_event
        app._players = SimpleNamespace(start=AsyncMock(side_effect=start_players))
        app._activities = SimpleNamespace(start=AsyncMock(side_effect=start_activities))
        app._running = False

        tailer = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with (
            patch("apps.sevendays.Tailer", return_value=tailer) as tailer_cls,
            patch("apps.sevendays._discover_sevendays_runtime_log", new=AsyncMock(return_value=None)),
            patch("apps.sevendays._ensure_serverconfig_userdata_redirect") as ensure_userdata_redirect,
        ):
            result = await SevenDays.start(app)

        self.assertTrue(result)
        self.assertTrue(app._running)
        self.assertEqual(call_order, ["ready", "players", "activities"])
        app._std_launch.assert_awaited_once()
        ensure_userdata_redirect.assert_not_called()
        app._relay.setup.assert_awaited_once()
        tailer.start.assert_awaited_once_with(app._tail_matchers)
        tailer_cls.assert_called_once()
        tailer_args = tailer_cls.call_args.args
        self.assertEqual(tailer_args[1:], (relay_reader, app.file_stdout))

    async def test_start_prefers_launch_created_runtime_log_file_over_existing_timestamped_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            serverconfig_path = root / "serverconfig.xml"
            serverconfig_path.write_text("<ServerSettings />", encoding="utf-8")
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir(parents=True)
            older_runtime_log = log_dir / "output_log__2026-06-17__02-27-35.txt"
            older_runtime_log.write_text("old\n", encoding="utf-8")
            runtime_log = log_dir / "output_log__2026-06-17__03-27-35.txt"
            app = cast(Any, object.__new__(SevenDays))
            app.name = "sevendays_demo"
            app.directory = root
            app.server_log = None
            app.file_stdout = root / "stdout.log"
            app.process = SimpleNamespace(stdout=object())
            app._server_ready = asyncio.Event()
            app._telnet_startup_error = None
            app._tail_matchers = set()
            app._std_launch = AsyncMock(
                side_effect=lambda: runtime_log.write_text("INF Version: V 2.0 (b1)\n", encoding="utf-8")
            )
            app.check_running = lambda: True
            app._configure_telnet_client = AsyncMock()
            app._relay = SimpleNamespace(
                setup=AsyncMock(return_value=object()),
                connected_event=asyncio.Event(),
            )
            app.wait_for_ready_event = AsyncMock()
            app._players = SimpleNamespace(start=AsyncMock())
            app._activities = SimpleNamespace(start=AsyncMock())
            app._running = False

            tailer = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            with (
                patch("apps.sevendays.Tailer", return_value=tailer) as tailer_cls,
                patch("apps.sevendays.File_Utils.link") as link_mock,
            ):
                result = await SevenDays.start(app)
            redirected_config = serverconfig_path.read_text(encoding="utf-8")

        self.assertTrue(result)
        tailer_cls.assert_called_once()
        self.assertEqual(tailer_cls.call_args.args[1:], (runtime_log, app.file_stdout))
        link_mock.assert_called_once_with(runtime_log, app.file_stdout.with_name(runtime_log.name))
        self.assertIn('name="UserDataFolder" value="userdata"', redirected_config)

    async def test_start_rejects_telnet_bind_failure_and_terminates_process(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_log = root / "output_log__2026-06-27__06-40-00.txt"
            app = cast(Any, object.__new__(SevenDays))
            app.name = "sevendays_demo"
            app.directory = root
            app.server_log = None
            app.file_stdout = root / "stdout.log"
            app._server_ready = asyncio.Event()
            app._telnet_startup_error = None
            app._tail_matchers = set()
            app._configure_telnet_client = AsyncMock()
            app._std_launch = AsyncMock(
                side_effect=lambda: runtime_log.write_text("INF StartAsServer\n", encoding="utf-8")
            )
            app.check_running = lambda: True
            app._relay = SimpleNamespace(setup=AsyncMock(), teardown=AsyncMock())
            app._players = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            app._activities = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            app._running = False
            app._terminate = AsyncMock()

            async def record_telnet_failure(*_args: object, **_kwargs: object) -> None:
                app._telnet_startup_error = "INF Error in Telnet.ctor: Address already in use"

            app.wait_for_ready_event = AsyncMock(side_effect=record_telnet_failure)
            tailer = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            with (
                patch("apps.sevendays.Tailer", return_value=tailer),
                patch("apps.sevendays.File_Utils.link"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Telnet failed to start"):
                    await SevenDays.start(app)

        app._relay.setup.assert_not_awaited()
        app._relay.teardown.assert_awaited_once()
        app._terminate.assert_awaited_once()

    async def test_discover_runtime_log_waits_for_delayed_timestamped_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir(parents=True)
            older_runtime_log = log_dir / "output_log__2026-06-17__02-27-35.txt"
            older_runtime_log.write_text("old\n", encoding="utf-8")
            runtime_log = log_dir / "output_log__2026-06-17__03-27-35.txt"

            async def create_runtime_log(_seconds: float) -> None:
                runtime_log.write_text("INF Version: V 2.0 (b1)\n", encoding="utf-8")

            with patch("apps.sevendays.asyncio.sleep", new=AsyncMock(side_effect=create_runtime_log)):
                discovered = await _discover_sevendays_runtime_log(
                    directory=root,
                    server_log=None,
                    previous_timestamped_logs={older_runtime_log},
                    check_running=lambda: True,
                    timeout_seconds=1.0,
                    poll_seconds=0.0,
                )

        self.assertEqual(discovered, runtime_log)

    async def test_discover_runtime_log_uses_changed_static_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_log = root / "server_stdout.log"
            runtime_log.write_text("old\n", encoding="utf-8")
            previous_signature = runtime_log.stat()
            baseline = {
                runtime_log: (
                    previous_signature.st_dev,
                    previous_signature.st_ino,
                    previous_signature.st_mtime_ns,
                    previous_signature.st_size,
                )
            }

            runtime_log.write_text("current runtime output\n", encoding="utf-8")
            discovered = await _discover_sevendays_runtime_log(
                directory=root,
                server_log=runtime_log,
                previous_log_signatures=baseline,
                check_running=lambda: True,
                timeout_seconds=0.0,
                poll_seconds=0.0,
            )

        self.assertEqual(discovered, runtime_log)

    async def test_discover_runtime_log_does_not_reuse_previous_timestamped_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_log = root / "7DaysToDieServer_Data" / "output_log__2026-06-17__02-27-35.txt"
            runtime_log.parent.mkdir(parents=True)
            runtime_log.write_text("INF Version: V 2.0 (b1)\n", encoding="utf-8")

            discovered = await _discover_sevendays_runtime_log(
                directory=root,
                server_log=None,
                previous_timestamped_logs={runtime_log},
                check_running=lambda: True,
                timeout_seconds=0.0,
                poll_seconds=0.0,
            )

        self.assertIsNone(discovered)

    async def test_stop_terminates_when_telnet_send_and_polling_cleanup_fail(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app._running = True
        app._relay = SimpleNamespace(
            send=AsyncMock(side_effect=BrokenPipeError("closed")),
            teardown=AsyncMock(),
        )
        app._players = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("poll failed")))
        app._activities = SimpleNamespace(stop=AsyncMock())
        app._tail = SimpleNamespace(stop=AsyncMock())
        app._terminate = AsyncMock()

        result = await SevenDays.stop(app)

        self.assertTrue(result)
        app._activities.stop.assert_awaited_once()
        app._tail.stop.assert_awaited_once()
        app._relay.teardown.assert_awaited_once()
        app._terminate.assert_awaited_once()

    async def test_kill_terminates_when_polling_cleanup_fails(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app._running = True
        app._relay = SimpleNamespace(teardown=AsyncMock())
        app._players = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("poll failed")))
        app._activities = SimpleNamespace(stop=AsyncMock())
        app._tail = None
        app._terminate = AsyncMock()

        result = await SevenDays.kill(app)

        self.assertTrue(result)
        app._activities.stop.assert_awaited_once()
        app._relay.teardown.assert_awaited_once()
        app._terminate.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
