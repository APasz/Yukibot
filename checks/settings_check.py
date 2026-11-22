from __future__ import annotations

import json
import tomllib
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _security import Power_Level
from apps._config import App_Config, AppVersion
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    ForcedSettingState,
    IntSettingSpec,
    Setting,
    Setting_Group,
    Setting_Label,
    SettingStateForceRule,
    Settings_Manager,
    StringSettingSpec,
)
from apps.factorio import Factorio_Settings
from apps.minecraft import Minecraft_Settings
from apps.sevendays import SevenDays_Settings
from apps.sevendays.installation import UNDEAD_LEGACY_MANIFEST, UNDEAD_LEGACY_RELEASE, UndeadLegacyInstallation
from apps.sevendays.overhaul_settings import SevenDaysSettingGroup
from cmd_app import AppManageMode, AppManageState, _state_from_value, _state_value


class _TestSettingGroup(Setting_Group):
    network = "Network"
    proxy = "Proxy"


def _write_sevendays_xml_files(
    root: Path,
    *,
    server_properties: Mapping[str, str],
    trader_biomes: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    trader_biome_map = {
        "trader_rekt": "forest",
        "trader_jen": "burntforest",
        "trader_bob": "desert",
        "trader_hugh": "snow",
        "trader_joel": "wasteland",
    }
    if trader_biomes is not None:
        trader_biome_map.update(trader_biomes)

    server_pointer = root / "serverconfig.xml"
    server_xml = "\n".join(
        f'    <property name="{name}" value="{value}"/>' for name, value in server_properties.items()
    )
    server_pointer.write_text(
        f'<?xml version="1.0"?>\n<ServerSettings>\n{server_xml}\n</ServerSettings>\n',
        encoding="utf-8",
    )

    rwgmixer_pointer = root / "Data" / "Config" / "rwgmixer.xml"
    rwgmixer_pointer.parent.mkdir(parents=True, exist_ok=True)
    rwgmixer_xml = "\n".join(
        (
            f'    <prefab_spawn_adjust partial_name="{partial_name}" biomeTags="{biome}" '
            'bias="20" min_count="2" max_count="4"/>'
        )
        for partial_name, biome in trader_biome_map.items()
    )
    rwgmixer_pointer.write_text(
        f'<?xml version="1.0"?>\n<rwgmixer>\n{rwgmixer_xml}\n</rwgmixer>\n',
        encoding="utf-8",
    )
    return server_pointer, rwgmixer_pointer


def _sevendays_settings_manager(settings: SevenDays_Settings) -> Settings_Manager:
    root = settings.pointer.parent
    return Settings_Manager(
        App_Config(name="sevendays_alpha", instance_key="alpha", directory=root, apps_dir=root, scope="sevendays"),
        settings,
    )


def _read_sevendays_properties(pointer: Path) -> dict[str, str]:
    return {
        element.attrib["name"]: element.attrib["value"]
        for element in ET.parse(pointer).getroot().findall("property")
    }


def _write_computercraft_config(root: Path, *, level_name: str = "world") -> Path:
    pointer = root / level_name / "serverconfig" / "computercraft-server.toml"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        "\n".join((
            "[http]",
            "enabled = true",
            "websocket_enabled = true",
            "",
            "[http.proxy]",
            'type = "HTTP"',
            'host = "" # Proxy hostname',
            "port = 8080",
            "",
            "[[http.rules]]",
            'host = "$private"',
            'action = "deny"',
            "",
            "[[http.rules]]",
            'host = "*"',
            'action = "allow"',
            "",
            "[peripheral]",
            "command_block_enabled = false",
            "modem_range = 64",
            "modem_high_altitude_range = 384",
            "modem_range_during_storm = 64",
            "modem_high_altitude_range_during_storm = 384",
            'unmanaged_setting = "preserved"',
        )),
        encoding="utf-8",
    )
    return pointer


def _factorio_settings_with_instance_config(root: Path) -> tuple[Factorio_Settings, App_Config, Path]:
    pointer = root / "data" / "server-settings.json"
    pointer.parent.mkdir()
    pointer.write_text("{}", encoding="utf-8")
    cfg = App_Config(name="factorio_alpha", instance_key="alpha", directory=root, apps_dir=root, scope="factorio")
    instances = root / "instances.json"
    instances.write_text(json.dumps({"alpha": {"enabled": True}, "other": {"enabled": False}}))
    return Factorio_Settings(pointer, saves_directory=root / "saves", cfg=cfg), cfg, instances


class SettingTests(unittest.TestCase):
    def test_required_text_rejects_blank_direct_updates_without_losing_loaded_value(self) -> None:
        setting = Setting(StringSettingSpec(), "Name", "name", [], default="Name")
        setting.load_value("Loaded")
        for blank in ("", " ", "\n"):
            with self.subTest(blank=blank), self.assertRaisesRegex(ValueError, "must not be empty"):
                setting.update(blank)
            self.assertEqual(setting.value, "Loaded")

    def test_invalid_setting_logs_identify_the_key_without_exposing_the_value(self) -> None:
        setting = Setting(IntSettingSpec(is_sensitive=True), "Secret", "secret", [], default=0)
        for parse in (setting.update, setting.load_value):
            with self.subTest(parse=parse), self.assertLogs("apps._settings", level="WARNING") as captured:
                with self.assertRaises(ValueError):
                    parse("private-invalid-value")
            self.assertIn("key=secret", " ".join(captured.output))
            self.assertNotIn("private-invalid-value", " ".join(captured.output))

    def test_settings_manager_rejects_blank_required_text_without_losing_the_previous_draft(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer, _ = _write_sevendays_xml_files(Path(temporary), server_properties={"ServerName": "Before"})
            settings = SevenDays_Settings(pointer)
            manager = _sevendays_settings_manager(settings)
            name = settings.get_setting("ServerName")
            assert name is not None
            manager.update_setting(42, name, "Pending")

            for blank in ("", " ", "\n"):
                with self.subTest(blank=blank), self.assertRaisesRegex(ValueError, "must not be empty"):
                    manager.update_setting(42, name, blank)

            self.assertEqual(name.value, "Before")
            self.assertEqual(manager.current_input_value(name, 42), "Pending")

    def test_unloaded_settings_cannot_be_serialised_or_persisted(self) -> None:
        setting = Setting(IntSettingSpec(), "Count", "count", [], default=10)
        with self.assertRaisesRegex(ValueError, "not been loaded"):
            setting.serialise_value()
        data: dict[str, object] = {}
        with self.assertRaisesRegex(ValueError, "not been loaded"):
            setting.set(data)
        self.assertEqual(data, {})

    def test_failed_reload_preserves_loaded_values_and_user_drafts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer = root / "server.properties"
            pointer.write_text("max-players=20\nwhite-list=false\nmotd=Old\n")
            settings = Minecraft_Settings(pointer)
            manager = Settings_Manager(
                App_Config(name="minecraft_alpha", instance_key="alpha", directory=root, apps_dir=root, scope="minecraft"),
                settings,
            )
            maximum, motd = settings.get_setting("max-players"), settings.get_setting("motd")
            assert maximum is not None and motd is not None
            manager.update_setting(42, motd, "Pending")
            pointer.write_text("max-players=99\nwhite-list=invalid\nmotd=New\n")

            with self.assertRaises(ValueError):
                manager.load(42)

            self.assertEqual(maximum.value, 20)
            self.assertEqual(motd.value, "Old")
            self.assertEqual(manager.current_input_value(motd, 42), "Pending")

    def test_missing_minecraft_properties_reset_to_defaults_and_can_be_saved(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer = Path(temporary) / "server.properties"
            pointer.write_text("motd=Custom\nmax-players=8\n")
            settings = Minecraft_Settings(pointer)
            pointer.write_text("# Properties removed externally\n")

            settings.load()

            maximum = settings.get_setting("max-players")
            assert maximum is not None
            self.assertEqual(maximum.value, maximum.default)
            maximum.update("42")
            settings.save()
            self.assertIn("max-players=42\n", pointer.read_text())
            self.assertIn("# Properties removed externally", pointer.read_text())

    def test_missing_sevendays_properties_reset_to_defaults_and_can_be_saved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer, _ = _write_sevendays_xml_files(root, server_properties={"GameDifficulty": "4"})
            settings = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.6"))
            _write_sevendays_xml_files(root, server_properties={"GameName": "MyGame"})

            settings.load()

            difficulty = settings.get_setting("GameDifficulty")
            assert difficulty is not None
            self.assertEqual(difficulty.value, difficulty.default)
            difficulty.update("5")
            settings.save()
            persisted = {node.get("name"): node.get("value") for node in ET.parse(pointer).getroot()}
            self.assertEqual(persisted["GameDifficulty"], "5")
            self.assertNotIn("TraderRektBiome", persisted)

    def test_duplicate_managed_properties_fail_on_load_and_save(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            minecraft_pointer = root / "server.properties"
            minecraft_pointer.write_text("max-players=20\n")
            minecraft = Minecraft_Settings(minecraft_pointer)
            sevendays_pointer, _ = _write_sevendays_xml_files(root, server_properties={"GameName": "MyGame"})
            sevendays = SevenDays_Settings(sevendays_pointer)
            minecraft_pointer.write_text("max-players=20\nmax-players=30\n")
            tree = ET.parse(sevendays_pointer)
            ET.SubElement(tree.getroot(), "property", name="GameName", value="Another")
            tree.write(sevendays_pointer)

            for settings in (minecraft, sevendays):
                original = settings.pointer.read_bytes()
                for operation in (settings.load, settings.save):
                    with self.subTest(app=type(settings).__name__, operation=operation.__name__):
                        with self.assertRaisesRegex(ValueError, "Duplicate"):
                            operation()
                        self.assertEqual(settings.pointer.read_bytes(), original)

    def test_sevendays_file_replacement_failure_rolls_back_both_config_files(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer, mixer = _write_sevendays_xml_files(Path(temporary), server_properties={"ServerName": "Before"})
            settings = SevenDays_Settings(pointer)
            name = settings.get_setting("ServerName")
            assert name is not None
            name.update("After")
            original_server, original_mixer = pointer.read_bytes(), mixer.read_bytes()
            original_replace = Path.replace

            def replace(source: Path, target: Path) -> Path:
                if target == mixer:
                    raise OSError("write failed")
                return original_replace(source, target)

            with patch.object(Path, "replace", new=replace), self.assertRaisesRegex(OSError, "write failed"):
                settings.save()

            self.assertEqual(pointer.read_bytes(), original_server)
            self.assertEqual(mixer.read_bytes(), original_mixer)

    def test_choice_labels_are_accepted_and_displayed(self) -> None:
        setting: Setting[bool] = Setting[bool](
            BoolSettingSpec(ChoiceSpec(ChoiceOption("true", "Public"), ChoiceOption("false", "Private"))),
            Setting_Label.visibility,
            "public",
            ["visibility"],
            default=False,
        )

        setting.update("Public")

        self.assertIs(setting.value, True)
        self.assertEqual(setting.choice_label_for_value(), "Public")
        self.assertEqual(setting.display_value(), "Public (True)")

    def test_bool_setting_serialises_to_lowercase_true_false(self) -> None:
        setting: Setting[bool] = Setting[bool](
            BoolSettingSpec(), Setting_Label.visibility, "public", ["visibility"], default=False
        )

        setting.update("true")
        self.assertEqual(setting.serialise_value(), "true")

        setting.update("false")
        self.assertEqual(setting.serialise_value(), "false")

    def test_bool_settings_default_to_enabled_disabled_choices(self) -> None:
        setting: Setting[bool] = Setting[bool](BoolSettingSpec(), "Auto Pause", "FG.DSAutoPause", [], default=False)

        self.assertEqual(
            setting.choice_items(),
            (("Enabled", "true"), ("Disabled", "false")),
        )

    def test_setting_groups_sort_before_setting_labels(self) -> None:
        class _GroupedSettings(App_Settings):
            def load(self) -> None:
                return None

            def save(self) -> None:
                return None

        with TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "settings.json"
            pointer.write_text("{}", encoding="utf-8")
            settings = _GroupedSettings(
                pointer,
                [
                    Setting(StringSettingSpec(), "Zulu", "ungrouped", [], default=""),
                    Setting(
                        StringSettingSpec(),
                        "Zulu",
                        "http-zulu",
                        [],
                        default="",
                        group=_TestSettingGroup.network,
                    ),
                    Setting(
                        StringSettingSpec(),
                        "Alpha",
                        "http-alpha",
                        [],
                        default="",
                        group=_TestSettingGroup.network,
                    ),
                    Setting(
                        StringSettingSpec(),
                        "Alpha",
                        "proxy-alpha",
                        [],
                        default="",
                        group=_TestSettingGroup.proxy,
                    ),
                ],
            )

        self.assertEqual(
            [setting.key for setting in settings.options],
            ["ungrouped", "http-alpha", "http-zulu", "proxy-alpha"],
        )

    def test_spec_based_setting_exposes_sensitive_and_blank_metadata(self) -> None:
        setting: Setting[str] = Setting[str](
            StringSettingSpec(allow_blank=True, is_sensitive=True),
            "Admin Password",
            "admin-password",
            [],
            default="",
        )

        self.assertTrue(setting.allows_blank_input)
        self.assertTrue(setting.is_sensitive)
        self.assertIsNone(setting.do_hide)
        setting.update("")
        self.assertEqual(setting.value, "")

    def test_setting_can_opt_into_hidden_value_policy_separately_from_sensitivity(self) -> None:
        setting: Setting[str] = Setting[str](
            StringSettingSpec(
                allow_blank=True,
                is_sensitive=True,
                do_hide=Power_Level.sudo,
            ),
            "Admin Password",
            "admin-password",
            [],
            default="",
        )

        self.assertTrue(setting.is_sensitive)
        self.assertEqual(setting.do_hide, Power_Level.sudo)

    def test_spec_based_bool_setting_supports_custom_choice_labels(self) -> None:
        setting: Setting[bool] = Setting[bool](
            BoolSettingSpec(ChoiceSpec(ChoiceOption("true", "Public"), ChoiceOption("false", "Private"))),
            Setting_Label.visibility,
            "public",
            [],
            default=False,
        )

        setting.update("Private")
        self.assertIs(setting.value, False)
        self.assertEqual(setting.choice_label_for_value(), "Private")

    def test_recent_inputs_track_recency_for_freeform_string_settings(self) -> None:
        setting = Setting(StringSettingSpec(allow_blank=True), Setting_Label.motd, "motd", [], default="")

        setting.update("alpha", remember_input=True)
        setting.update("beta", remember_input=True)
        setting.update("alpha", remember_input=True)

        self.assertEqual(setting.recent_inputs, ("alpha", "beta"))

    def test_recent_inputs_keep_last_twenty_five_values(self) -> None:
        setting = Setting(IntSettingSpec(), "View Distance", "view-distance", [], default=10)

        for value in range(30):
            setting.update(str(value), remember_input=True)

        self.assertEqual(len(setting.recent_inputs), 25)
        self.assertEqual(setting.recent_inputs[0], "29")
        self.assertEqual(setting.recent_inputs[-1], "5")

    def test_hidden_settings_do_not_track_recent_inputs(self) -> None:
        setting = Setting(
            StringSettingSpec(allow_blank=True, do_hide=Power_Level.admin),
            "Admin Token",
            "admin-token",
            [],
            default="",
        )

        setting.update("alpha", remember_input=True)

        self.assertFalse(setting.supports_recent_inputs)
        self.assertEqual(setting.recent_inputs, ())

    def test_int_setting_spec_rejects_negative_values_by_default(self) -> None:
        setting = Setting(IntSettingSpec(), "Spawn Protection", "spawn-protection", [], default=16)

        with self.assertRaisesRegex(ValueError, "Invalid value"):
            setting.update("-1")

    def test_int_setting_spec_can_allow_negative_values(self) -> None:
        setting = Setting(
            IntSettingSpec(allow_negative=True),
            "Offset",
            "offset",
            [],
            default=0,
        )

        setting.update("-1")

        self.assertEqual(setting.value, -1)

    def test_setting_default_is_normalised_and_exposed(self) -> None:
        setting = Setting(
            BoolSettingSpec(),
            "Whitelist",
            "white-list",
            [],
            default=True,
        )

        self.assertIs(setting.default, True)

    def test_freeform_string_settings_allow_blank_input_without_extra_flag(self) -> None:
        setting = Setting(StringSettingSpec(allow_blank=True), "Level Seed", "level-seed", [], default="")

        self.assertTrue(setting.allows_blank_input)
        setting.update("")
        self.assertEqual(setting.value, "")

    def test_settings_default_to_non_paragraph_and_can_opt_in(self) -> None:
        default_setting = Setting(StringSettingSpec(allow_blank=True), "Server Name", "server-name", [], default="")
        paragraph_setting = Setting(
            StringSettingSpec(allow_blank=True),
            Setting_Label.motd,
            "motd",
            [],
            default="",
            paragraph=True,
        )

        self.assertFalse(default_setting.paragraph)
        self.assertTrue(paragraph_setting.paragraph)

    def test_string_settings_do_not_allow_blank_input_by_default(self) -> None:
        setting = Setting(StringSettingSpec(), "Level Name", "level-name", [], default="world")

        self.assertFalse(setting.allows_blank_input)

    def test_setting_get_normalises_loaded_values(self) -> None:
        setting = Setting(BoolSettingSpec(), "Whitelist", "white-list", [], default=False)

        value = setting.get({"white-list": "true"})

        self.assertIs(value, True)
        self.assertIs(setting.value, True)

    def test_setting_get_uses_default_when_value_missing(self) -> None:
        setting = Setting(IntSettingSpec(), "Max Players", "max_players", [], default=8)

        value = setting.get({})

        self.assertEqual(value, 8)
        self.assertEqual(setting.value, 8)

    def test_setting_get_clamps_loaded_int_values_to_declared_range(self) -> None:
        min_clamped_setting = Setting(
            IntSettingSpec(min_value=2, max_value=8),
            "Max Players",
            "max_players",
            [],
            default=4,
        )
        max_clamped_setting = Setting(
            IntSettingSpec(min_value=2, max_value=8),
            "Max Players",
            "max_players",
            [],
            default=4,
        )

        min_loaded_value = min_clamped_setting.get({"max_players": "1"})
        max_loaded_value = max_clamped_setting.get({"max_players": "9"})

        self.assertEqual(min_loaded_value, 2)
        self.assertEqual(min_clamped_setting.value, 2)
        self.assertEqual(max_loaded_value, 8)
        self.assertEqual(max_clamped_setting.value, 8)

    def test_setting_load_value_clamps_loaded_int_values_to_declared_range(self) -> None:
        min_clamped_setting = Setting(
            IntSettingSpec(min_value=2, max_value=8),
            "Max Players",
            "max_players",
            [],
            default=4,
        )
        max_clamped_setting = Setting(
            IntSettingSpec(min_value=2, max_value=8),
            "Max Players",
            "max_players",
            [],
            default=4,
        )

        min_clamped_setting.load_value("1")
        max_clamped_setting.load_value("9")

        self.assertEqual(min_clamped_setting.value, 2)
        self.assertEqual(max_clamped_setting.value, 8)

    def test_setting_can_enforce_inclusive_app_version_bounds(self) -> None:
        setting = Setting(
            StringSettingSpec(allow_blank=True),
            "Gameplay Flag",
            "gameplay-flag",
            [],
            default="",
            min_app_version=AppVersion(main="1.1.0", build=100),
            max_app_version=AppVersion(main="1.1.0", build=200),
        )

        self.assertFalse(setting.supports_app_version(None))
        self.assertFalse(setting.supports_app_version(AppVersion(main="1.1.0", build=99)))
        self.assertTrue(setting.supports_app_version(AppVersion(main="1.1.0", build=100)))
        self.assertTrue(setting.supports_app_version(AppVersion(main="1.1.0", build=150)))
        self.assertFalse(setting.supports_app_version(AppVersion(main="1.1.0", build=201)))

    def test_app_settings_filter_unsupported_settings_by_current_version(self) -> None:
        class _VersionAwareSettings(App_Settings):
            def load(self) -> None:
                return None

            def save(self) -> None:
                return None

        with TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "settings.json"
            pointer.write_text("{}", encoding="utf-8")
            always_setting = Setting(StringSettingSpec(allow_blank=True), "Always", "always", [], default="")
            gated_setting = Setting(
                StringSettingSpec(allow_blank=True),
                "Experimental",
                "experimental",
                [],
                default="",
                min_app_version=AppVersion(main="1.1.0", build=100),
            )
            settings = _VersionAwareSettings(
                pointer,
                [always_setting, gated_setting],
                version_getter=lambda: AppVersion(main="1.1.0", build=150),
            )

            self.assertEqual([setting.key for setting in settings.options], ["always", "experimental"])
            self.assertIs(settings.get_setting("experimental"), gated_setting)

            settings.set_version_getter(lambda: AppVersion(main="1.0.9"))

            self.assertEqual([setting.key for setting in settings.options], ["always"])
            self.assertIsNone(settings.get_setting("experimental"))

    def test_setting_bounds_allow_a_build_minimum_and_main_version_maximum(self) -> None:
        setting = Setting(
            BoolSettingSpec(), "Feature", "feature", [], default=False,
            min_app_version=AppVersion(main="2.6", build=10), max_app_version=AppVersion(main="2.6"),
        )

        self.assertFalse(setting.supports_app_version(AppVersion(main="2.6", build=9)))
        self.assertTrue(setting.supports_app_version(AppVersion(main="2.6", build=14)))
        self.assertFalse(setting.supports_app_version(AppVersion(main="3.0")))

    def test_settings_manager_discards_drafts_for_now_unsupported_settings(self) -> None:
        class _VersionAwareSettings(App_Settings):
            def load(self) -> None:
                return None

            def save(self) -> None:
                return None

        with TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "settings.json"
            pointer.write_text("{}", encoding="utf-8")
            config = App_Config(
                name="dummy",
                instance_key="alpha",
                directory=Path(temp_dir),
                apps_dir=Path(temp_dir),
                scope="dummy",
                version=AppVersion(main="1.1.0", build=150),
            )
            gated_setting = Setting(
                StringSettingSpec(allow_blank=True),
                "Experimental",
                "experimental",
                [],
                default="",
                min_app_version=AppVersion(main="1.1.0", build=100),
            )
            settings = _VersionAwareSettings(pointer, [gated_setting])
            manager = Settings_Manager(config, settings)

            manager.update_setting(actor_user_id=42, setting=gated_setting, value="enabled")
            self.assertEqual(manager.pending_change_count(42), 1)

            config.version = AppVersion(main="1.0.9")

            self.assertEqual(manager.pending_change_count(42), 0)
            with self.assertRaisesRegex(ValueError, "no longer available"):
                manager.update_setting(actor_user_id=42, setting=gated_setting, value="enabled")
            self.assertEqual(manager.pending_change_count(42), 0)

    def test_settings_manager_applies_forced_setting_states(self) -> None:
        class _JsonSettings(App_Settings):
            def load(self) -> None:
                data = json.loads(self.pointer.read_text(encoding="utf-8"))
                for setting in self.options:
                    setting.get(data)

            def save(self) -> None:
                data: dict[str, object] = {}
                for setting in self.options:
                    setting.set(data)
                self.pointer.write_text(json.dumps(data, indent=4), encoding="utf-8")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pointer = root / "settings.json"
            pointer.write_text(
                json.dumps(
                    {
                        "visibility": {"public": False},
                        "require_user_verification": False,
                    },
                    indent=4,
                ),
                encoding="utf-8",
            )
            public_setting = Setting(
                BoolSettingSpec(ChoiceSpec(ChoiceOption("true", "Public"), ChoiceOption("false", "Private"))),
                Setting_Label.visibility,
                "public",
                ["visibility"],
                default=False,
                forced_state_rules=(
                    SettingStateForceRule(
                        True,
                        ForcedSettingState("require_user_verification", True),
                    ),
                ),
            )
            verification_setting = Setting(
                BoolSettingSpec(),
                "Require User Verification",
                "require_user_verification",
                [],
                default=False,
            )
            settings = _JsonSettings(pointer, [public_setting, verification_setting])
            manager = Settings_Manager(
                App_Config(
                    name="dummy",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="dummy",
                ),
                settings,
            )

            manager.update_setting(actor_user_id=42, setting=public_setting, value="Public")

            self.assertEqual(manager.pending_change_count(42), 2)
            self.assertTrue(manager.has_pending_value(42, public_setting))
            self.assertTrue(manager.has_pending_value(42, verification_setting))
            self.assertIs(manager.value_for(verification_setting, 42), True)

            manager.save(42)

            saved = json.loads(pointer.read_text(encoding="utf-8"))
            self.assertIs(saved["visibility"]["public"], True)
            self.assertIs(saved["require_user_verification"], True)

    def test_forced_setting_state_is_removed_when_trigger_is_cleared(self) -> None:
        class _JsonSettings(App_Settings):
            def load(self) -> None:
                data = json.loads(self.pointer.read_text(encoding="utf-8"))
                for setting in self.options:
                    setting.get(data)

            def save(self) -> None:
                return None

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pointer = root / "settings.json"
            pointer.write_text(
                json.dumps(
                    {
                        "visibility": {"public": False},
                        "require_user_verification": False,
                    },
                    indent=4,
                ),
                encoding="utf-8",
            )
            public_setting = Setting(
                BoolSettingSpec(ChoiceSpec(ChoiceOption("true", "Public"), ChoiceOption("false", "Private"))),
                Setting_Label.visibility,
                "public",
                ["visibility"],
                default=False,
                forced_state_rules=(
                    SettingStateForceRule(
                        True,
                        ForcedSettingState("require_user_verification", True),
                    ),
                ),
            )
            verification_setting = Setting(
                BoolSettingSpec(),
                "Require User Verification",
                "require_user_verification",
                [],
                default=False,
            )
            settings = _JsonSettings(pointer, [public_setting, verification_setting])
            manager = Settings_Manager(
                App_Config(
                    name="dummy",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="dummy",
                ),
                settings,
            )

            manager.update_setting(actor_user_id=42, setting=public_setting, value="Public")
            manager.update_setting(actor_user_id=42, setting=public_setting, value="Private")

            self.assertEqual(manager.pending_change_count(42), 0)
            self.assertFalse(manager.has_pending_value(42, public_setting))
            self.assertFalse(manager.has_pending_value(42, verification_setting))
            self.assertIs(manager.value_for(verification_setting, 42), False)

    def test_app_settings_exact_key_lookup_precedes_label_alias(self) -> None:
        class _LookupSettings(App_Settings):
            def load(self) -> None:
                return None

            def save(self) -> None:
                return None

        with TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "settings.json"
            pointer.write_text("{}", encoding="utf-8")
            exact_key_setting = Setting(
                StringSettingSpec(allow_blank=True),
                "Factorio Password",
                "password",
                [],
                default="",
                power_level=Power_Level.root,
            )
            label_alias_setting = Setting(
                StringSettingSpec(allow_blank=True),
                "Password",
                "game_password",
                [],
                default="",
                power_level=Power_Level.sudo,
            )

            settings = _LookupSettings(pointer, [exact_key_setting, label_alias_setting])

            self.assertIs(settings.get_setting("password"), exact_key_setting)
            self.assertIs(settings.get_setting("game_password"), label_alias_setting)

    def test_minecraft_settings_round_trip_extended_server_properties(self) -> None:
        with TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "server.properties"
            pointer.write_text(
                "\n".join(
                    [
                        "allow-flight=true",
                        "enable-command-block=false",
                        "force-gamemode=false",
                        "gamemode=survival",
                        "level-name=world",
                        "level-seed=",
                        "max-players=10",
                        "motd=A Minecraft Server",
                        "white-list=false",
                        "enforce-whitelist=false",
                        "pvp=true",
                        "spawn-protection=16",
                        "view-distance=16",
                        "difficulty=easy",
                    ]
                ),
                encoding="utf-8",
            )

            settings = Minecraft_Settings(pointer)
            for key, value in {
                "allow-flight": "false",
                "enable-command-block": "true",
                "force-gamemode": "true",
                "gamemode": "creative",
                "level-name": "skyblock",
                "level-seed": "8675309",
                "white-list": "true",
                "enforce-whitelist": "true",
                "pvp": "false",
                "view-distance": "12",
                "spawn-protection": "0",
            }.items():
                setting = settings.get_setting(key)
                if setting is None:
                    raise AssertionError(f"Missing setting {key}")
                setting.update(value)

            settings.save()

            saved = pointer.read_text(encoding="utf-8")
            self.assertIn("allow-flight=false", saved)
            self.assertIn("enable-command-block=true", saved)
            self.assertIn("force-gamemode=true", saved)
            self.assertIn("gamemode=creative", saved)
            self.assertIn("level-name=skyblock", saved)
            self.assertIn("level-seed=8675309", saved)
            self.assertIn("white-list=true", saved)
            self.assertIn("enforce-whitelist=true", saved)
            self.assertIn("pvp=false", saved)
            self.assertIn("view-distance=12", saved)
            self.assertIn("spawn-protection=0", saved)

    def test_minecraft_settings_round_trip_computercraft_server_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pointer = root / "server.properties"
            pointer.write_text("level-name=world\n", encoding="utf-8")
            computer_craft_pointer = _write_computercraft_config(root)

            settings = Minecraft_Settings(pointer)
            updates = {
                "computercraft.http.enabled": "false",
                "computercraft.http.websocket_enabled": "false",
                "computercraft.http.proxy.type": "SOCKS5",
                "computercraft.http.proxy.host": "proxy.example.test",
                "computercraft.http.proxy.port": "1080",
                "computercraft.http.rules.0.host": "10.0.0.0/8",
                "computercraft.http.rules.0.action": "allow",
                "computercraft.http.rules.1.host": "*.example.test",
                "computercraft.http.rules.1.action": "deny",
                "computercraft.peripheral.command_block_enabled": "true",
                "computercraft.peripheral.modem_range": "128",
                "computercraft.peripheral.modem_high_altitude_range": "512",
                "computercraft.peripheral.modem_range_during_storm": "96",
                "computercraft.peripheral.modem_high_altitude_range_during_storm": "256",
            }
            for key, value in updates.items():
                setting = settings.get_setting(key)
                if setting is None:
                    raise AssertionError(f"Missing setting {key}")
                setting.update(value)

            original_properties = pointer.read_bytes()
            original_computercraft = computer_craft_pointer.read_bytes()
            original_replace = Path.replace

            def replace(source: Path, target: Path) -> Path:
                if target == pointer:
                    raise OSError("write failed")
                return original_replace(source, target)

            with patch.object(Path, "replace", new=replace), self.assertRaisesRegex(OSError, "write failed"):
                settings.save()
            self.assertEqual(pointer.read_bytes(), original_properties)
            self.assertEqual(computer_craft_pointer.read_bytes(), original_computercraft)

            settings.save()

            saved_source = computer_craft_pointer.read_text(encoding="utf-8")
            saved = tomllib.loads(saved_source)
            self.assertEqual(
                saved,
                {
                    "http": {
                        "enabled": False,
                        "websocket_enabled": False,
                        "proxy": {
                            "type": "SOCKS5",
                            "host": "proxy.example.test",
                            "port": 1080,
                        },
                        "rules": [
                            {"host": "10.0.0.0/8", "action": "allow"},
                            {"host": "*.example.test", "action": "deny"},
                        ],
                    },
                    "peripheral": {
                        "command_block_enabled": True,
                        "modem_range": 128,
                        "modem_high_altitude_range": 512,
                        "modem_range_during_storm": 96,
                        "modem_high_altitude_range_during_storm": 256,
                        "unmanaged_setting": "preserved",
                    },
                },
            )
            self.assertIn('# Proxy hostname', saved_source)

    def test_minecraft_world_switch_requires_reload_before_computercraft_edits(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer = root / "server.properties"
            pointer.write_text("level-name=world\n")
            old_config = _write_computercraft_config(root)
            new_config = _write_computercraft_config(root, level_name="new-world")
            original_config = old_config.read_bytes()
            settings = Minecraft_Settings(pointer)
            manager = Settings_Manager(
                App_Config(name="minecraft_alpha", instance_key="alpha", directory=root, apps_dir=root, scope="minecraft"),
                settings,
            )
            level_name = settings.get_setting("level-name")
            http_enabled = settings.get_setting("computercraft.http.enabled")
            assert level_name is not None and http_enabled is not None
            manager.update_setting(42, level_name, "new-world")
            manager.update_setting(42, http_enabled, "false")

            with self.assertRaisesRegex(ValueError, "reload settings before editing ComputerCraft"):
                manager.save(42)

            self.assertEqual(pointer.read_text(), "level-name=world\n")
            self.assertEqual(old_config.read_bytes(), original_config)
            self.assertEqual(new_config.read_bytes(), original_config)
            self.assertEqual(manager.pending_change_count(42), 2)
            self.assertIs(http_enabled.value, True)

            manager.update_setting(42, http_enabled, "true")
            manager.save(42)
            self.assertIn("level-name=new-world\n", pointer.read_text())
            self.assertIsNone(settings.get_setting(http_enabled.key))
            with self.assertRaisesRegex(ValueError, "no longer available"):
                manager.update_setting(42, http_enabled, "false")

            manager.load(42)
            reloaded_http = settings.get_setting(http_enabled.key)
            assert reloaded_http is not None
            manager.update_setting(42, reloaded_http, "false")
            manager.save(42)

            self.assertIs(tomllib.loads(new_config.read_text())["http"]["enabled"], False)
            self.assertEqual(old_config.read_bytes(), original_config)

    def test_computercraft_uses_the_exact_world_folder_name(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer = root / "server.properties"
            pointer.write_text("level-name=world \\u0020\n")
            computer_craft_pointer = _write_computercraft_config(root, level_name="world  ")
            settings = Minecraft_Settings(pointer)
            http_enabled = settings.get_setting("computercraft.http.enabled")
            assert http_enabled is not None
            http_enabled.update("false")

            settings.save()

            self.assertIs(tomllib.loads(computer_craft_pointer.read_text())["http"]["enabled"], False)

    def test_minecraft_settings_hide_computercraft_controls_without_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            pointer = Path(temp_dir) / "server.properties"
            pointer.write_text("level-name=world\n", encoding="utf-8")

            settings = Minecraft_Settings(pointer)

            self.assertIsNone(settings.get_setting("computercraft.http.enabled"))
            self.assertNotIn(
                "computercraft.http.enabled",
                {setting.key for setting in settings.options},
            )

    def test_sevendays_settings_register_and_save_extended_server_config(self) -> None:
        property_values = {
            "ServerName": "Mr and Mrs Cream",
            "ServerDescription": "A 7 Days to Die server",
            "ServerPassword": "hugs",
            "ServerVisibility": "2",
            "ServerMaxWorldTransferSpeedKiBs": "1200",
            "ServerMaxPlayerCount": "3",
            "ServerReservedSlots": "0",
            "ServerAdminSlots": "0",
            "PlayerSafeZoneLevel": "5",
            "PlayerSafeZoneHours": "5",
            "BuildCreate": "false",
            "BedrollDeadZoneSize": "15",
            "BedrollExpiryTime": "45",
            "AllowSpawnNearFriend": "2",
            "MaxSpawnedZombies": "64",
            "MaxSpawnedAnimals": "50",
            "ServerMaxAllowedViewDistance": "12",
            "LandClaimCount": "5",
            "LandClaimSize": "41",
            "LandClaimDeadZone": "30",
            "LandClaimExpiryTime": "7",
            "LandClaimDecayMode": "0",
            "LandClaimOnlineDurabilityModifier": "4",
            "LandClaimOfflineDurabilityModifier": "4",
            "LandClaimOfflineDelay": "0",
            "EACEnabled": "true",
            "GameWorld": "East Yebixi Mountains",
            "WorldGenSeed": "asdf",
            "WorldGenSize": "6144",
            "GameName": "7 Days To Dai With Nai",
            "GameDifficulty": "1",
            "BlockDamagePlayer": "100",
            "BlockDamageAI": "100",
            "BlockDamageAIBM": "100",
            "XPMultiplier": "110",
            "DayNightLength": "90",
            "DayLightLength": "18",
            "BiomeProgression": "true",
            "StormFreq": "100",
            "DeathPenalty": "1",
            "DropOnDeath": "1",
            "DropOnQuit": "0",
            "CameraRestrictionMode": "0",
            "JarRefund": "0",
            "EnemyDifficulty": "0",
            "ZombieFeralSense": "0",
            "ZombieMove": "0",
            "ZombieMoveNight": "3",
            "ZombieFeralMove": "3",
            "ZombieBMMove": "3",
            "AISmellMode": "3",
            "BloodMoonFrequency": "10",
            "BloodMoonRange": "0",
            "BloodMoonWarning": "6",
            "LootAbundance": "100",
            "LootRespawnDays": "7",
            "AirDropFrequency": "72",
            "AirDropMarker": "true",
            "PartySharedKillRange": "200",
            "PlayerKillingMode": "3",
            "QuestProgressionDailyLimit": "4",
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            settings: SevenDays_Settings = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.6"))

            for key in property_values:
                self.assertIsNotNone(settings.get_setting(key), key)

            max_players = settings.get_setting("ServerMaxPlayerCount")
            biome_progression = settings.get_setting("BiomeProgression")
            visibility = settings.get_setting("ServerVisibility")
            world_gen_size = settings.get_setting("WorldGenSize")
            region = settings.get_setting("Region")
            eac_enabled = settings.get_setting("EACEnabled")
            spawn_near_friend = settings.get_setting("AllowSpawnNearFriend")
            land_claim_decay_mode = settings.get_setting("LandClaimDecayMode")
            view_distance = settings.get_setting("ServerMaxAllowedViewDistance")
            if (
                max_players is None
                or biome_progression is None
                or visibility is None
                or world_gen_size is None
                or region is None
                or eac_enabled is None
                or spawn_near_friend is None
                or land_claim_decay_mode is None
                or view_distance is None
            ):
                raise AssertionError("Missing expected 7D2D setting")

            self.assertEqual(max_players.value, 3)
            self.assertIs(biome_progression.value, True)
            self.assertIs(eac_enabled.value, True)
            self.assertEqual(visibility.choice_label_for_value(), "Public")
            self.assertEqual(spawn_near_friend.choice_label_for_value(), "Forest Only")
            self.assertEqual(land_claim_decay_mode.choice_label_for_value(), "Slow (Linear)")
            self.assertEqual(view_distance.value, 12)
            self.assertEqual(world_gen_size.choice_label_for_value(), "Small")
            self.assertEqual(region.desc, "Server browser region.")
            self.assertEqual(world_gen_size.desc, "RWG world size in blocks, including the advanced generator's presets.")
            self.assertEqual(eac_enabled.desc, "Require Easy Anti-Cheat for connecting clients.")

            for key, value in {
                "ServerDescription": "Private run",
                "ServerVisibility": "Friends",
                "ServerMaxWorldTransferSpeedKiBs": "1024",
                "ServerMaxPlayerCount": "32",
                "ServerReservedSlots": "12",
                "PlayerSafeZoneLevel": "8",
                "PlayerSafeZoneHours": "6",
                "BuildCreate": "true",
                "BedrollDeadZoneSize": "20",
                "BedrollExpiryTime": "30",
                "AllowSpawnNearFriend": "Always",
                "MaxSpawnedZombies": "80",
                "MaxSpawnedAnimals": "60",
                "ServerMaxAllowedViewDistance": "10",
                "LandClaimCount": "6",
                "LandClaimSize": "51",
                "LandClaimDeadZone": "40",
                "LandClaimExpiryTime": "10",
                "LandClaimDecayMode": "None Until Expired",
                "LandClaimOnlineDurabilityModifier": "5",
                "LandClaimOfflineDurabilityModifier": "6",
                "LandClaimOfflineDelay": "15",
                "EACEnabled": "false",
                "BiomeProgression": "false",
                "JarRefund": "60",
                "AirDropMarker": "false",
                "WorldGenSize": "Medium",
                "QuestProgressionDailyLimit": "6",
            }.items():
                setting = settings.get_setting(key)
                if setting is None:
                    raise AssertionError(f"Missing setting {key}")
                setting.update(value)

            trader_rekt = settings.get_setting("TraderRektBiome")
            trader_jen = settings.get_setting("TraderJenBiome")
            if trader_rekt is None or trader_jen is None:
                raise AssertionError("Missing trader biome settings")
            trader_rekt.update("burntforest")
            trader_jen.update("forest")

            settings.save()

            saved: str = pointer.read_text(encoding="utf-8")
            self.assertIn('name="ServerDescription" value="Private run"', saved)
            self.assertIn('name="ServerVisibility" value="1"', saved)
            self.assertIn('name="ServerMaxWorldTransferSpeedKiBs" value="1024"', saved)
            self.assertIn('name="ServerMaxPlayerCount" value="32"', saved)
            self.assertIn('name="ServerReservedSlots" value="12"', saved)
            self.assertIn('name="PlayerSafeZoneLevel" value="8"', saved)
            self.assertIn('name="PlayerSafeZoneHours" value="6"', saved)
            self.assertIn('name="BuildCreate" value="true"', saved)
            self.assertIn('name="BedrollDeadZoneSize" value="20"', saved)
            self.assertIn('name="BedrollExpiryTime" value="30"', saved)
            self.assertIn('name="AllowSpawnNearFriend" value="1"', saved)
            self.assertIn('name="MaxSpawnedZombies" value="80"', saved)
            self.assertIn('name="MaxSpawnedAnimals" value="60"', saved)
            self.assertIn('name="ServerMaxAllowedViewDistance" value="10"', saved)
            self.assertIn('name="LandClaimCount" value="6"', saved)
            self.assertIn('name="LandClaimSize" value="51"', saved)
            self.assertIn('name="LandClaimDeadZone" value="40"', saved)
            self.assertIn('name="LandClaimExpiryTime" value="10"', saved)
            self.assertIn('name="LandClaimDecayMode" value="2"', saved)
            self.assertIn('name="LandClaimOnlineDurabilityModifier" value="5"', saved)
            self.assertIn('name="LandClaimOfflineDurabilityModifier" value="6"', saved)
            self.assertIn('name="LandClaimOfflineDelay" value="15"', saved)
            self.assertIn('name="EACEnabled" value="false"', saved)
            self.assertIn('name="BiomeProgression" value="false"', saved)
            self.assertIn('name="JarRefund" value="60"', saved)
            self.assertIn('name="AirDropMarker" value="false"', saved)
            self.assertIn('name="WorldGenSize" value="8192"', saved)
            self.assertIn('name="QuestProgressionDailyLimit" value="6"', saved)
            rwgmixer_saved = (Path(temp_dir) / "Data" / "Config" / "rwgmixer.xml").read_text(encoding="utf-8")
            self.assertIn('partial_name="trader_rekt" biomeTags="burntforest"', rwgmixer_saved)
            self.assertIn('partial_name="trader_jen" biomeTags="forest"', rwgmixer_saved)

    def test_sevendays_settings_reject_invalid_restricted_values(self) -> None:
        property_values = {
            "ServerMaxWorldTransferSpeedKiBs": "1200",
            "ServerMaxPlayerCount": "8",
            "ServerReservedSlots": "0",
            "WorldGenSize": "6144",
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            settings = SevenDays_Settings(pointer)
            transfer_speed = settings.get_setting("ServerMaxWorldTransferSpeedKiBs")
            world_gen_size = settings.get_setting("WorldGenSize")
            max_players = settings.get_setting("ServerMaxPlayerCount")
            if transfer_speed is None or world_gen_size is None or max_players is None:
                raise AssertionError("Missing expected 7D2D setting")

            transfer_speed.update("2049")
            self.assertEqual(transfer_speed.value, 2049)
            with self.assertRaises(ValueError):
                transfer_speed.update("-1")

            with self.assertRaisesRegex(ValueError, "must match provided choices"):
                world_gen_size.update("7000")

            max_players.update("64")
            self.assertEqual(max_players.value, 64)

    def test_sevendays_settings_force_userdata_redirect_when_missing(self) -> None:
        property_values = {
            "ServerName": "Redirect Test",
            "GameWorld": "RWG",
            "GameName": "World",
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            SevenDays_Settings(pointer)

            saved = pointer.read_text(encoding="utf-8")
            expected_userdata_path = (pointer.parent / "userdata").resolve()
            self.assertIn(f'name="UserDataFolder" value="{expected_userdata_path}"', saved)

    def test_sevendays_game_name_allows_supported_characters_in_fresh_worlds(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            root = Path(temp_dir)
            pointer, _ = _write_sevendays_xml_files(
                root, server_properties={"GameWorld": "RWG", "GameName": "MyGame"}
            )
            (root / "userdata" / "GeneratedWorlds" / "Kotavu County").mkdir(parents=True)
            settings = SevenDays_Settings(pointer)
            manager = _sevendays_settings_manager(settings)
            game_world = settings.get_setting("GameWorld")
            game_name = settings.get_setting("GameName")
            if game_world is None or game_name is None:
                raise AssertionError("Missing expected 7D2D world save settings")

            for selection in ("new-rwg", "world:Kotavu%20County"):
                for value in ("The Leg", "AZaz09._-", "0", "  The Leg  "):
                    with self.subTest(selection=selection, value=value):
                        manager.update_setting(actor_user_id=42, setting=game_world, value=selection)
                        manager.update_setting(actor_user_id=42, setting=game_name, value=value)
                        manager.save(42)
                        stored_name = ET.parse(pointer).find("./property[@name='GameName']")
                        if stored_name is None:
                            raise AssertionError("Missing saved GameName property")
                        self.assertEqual(stored_name.get("value"), value.strip())
                        self.assertEqual(manager.current_input_value(game_name, 42), value.strip())

    def test_sevendays_game_name_rejects_invalid_drafts_without_changing_files(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            root = Path(temp_dir)
            pointer, rwgmixer_pointer = _write_sevendays_xml_files(
                root, server_properties={"GameWorld": "RWG", "GameName": "MyGame"}
            )
            settings = SevenDays_Settings(pointer)
            manager = _sevendays_settings_manager(settings)
            game_name = settings.get_setting("GameName")
            if game_name is None:
                raise AssertionError("Missing expected 7D2D GameName setting")
            original_server = pointer.read_bytes()
            original_mixer = rwgmixer_pointer.read_bytes()

            invalid_names = (
                "The Leg!", "Save/name", "Save\\name", "Save:name", "Save?", "café", "Save🔥",
                "Save\tName", "Save\nName", "Save\x00Name", "\tSave", "Save\n", "", "   ", ".", "..", ".Save",
            )
            for value in invalid_names:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "Save name \\(GameName\\)"):
                        manager.update_setting(actor_user_id=42, setting=game_name, value=value)
                    self.assertEqual(manager.pending_change_count(42), 0)
                    self.assertEqual(game_name.value, "MyGame")
                    self.assertEqual(pointer.read_bytes(), original_server)
                    self.assertEqual(rwgmixer_pointer.read_bytes(), original_mixer)

    def test_sevendays_settings_insert_missing_world_save_properties(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(
                Path(temp_dir), server_properties={"ServerName": "Defaults", "UnmanagedProperty": "preserved"}
            )
            settings = SevenDays_Settings(pointer)

            settings.save()

            stored = {node.get("name"): node.get("value") for node in ET.parse(pointer).getroot()}
            self.assertEqual(stored["GameName"], "MyGame")
            self.assertEqual(stored["GameWorld"], "Navezgane")
            self.assertEqual(stored["UnmanagedProperty"], "preserved")

    def test_sevendays_settings_validate_both_config_files_before_writing(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            pointer, mixer_pointer = _write_sevendays_xml_files(
                Path(temp_dir), server_properties={"GameWorld": "RWG", "GameName": "MyGame"}
            )
            settings = SevenDays_Settings(pointer)
            game_name = settings.get_setting("GameName")
            if game_name is None:
                raise AssertionError("Missing expected 7D2D GameName setting")
            game_name.update("NextSave")
            server_tree = ET.parse(pointer)
            for node in server_tree.getroot():
                if node.get("name") == "UserDataFolder":
                    node.set("value", "/external/userdata")
            server_tree.write(pointer, encoding="utf-8")
            mixer_pointer.write_text("<rwgmixer />", encoding="utf-8")
            original_server = pointer.read_bytes()
            original_mixer = mixer_pointer.read_bytes()

            with self.assertRaisesRegex(ValueError, "missing prefab_spawn_adjust"):
                settings.save()

            self.assertEqual(pointer.read_bytes(), original_server)
            self.assertEqual(mixer_pointer.read_bytes(), original_mixer)

    def test_sevendays_settings_exclude_invalid_discovered_save_names(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            root = Path(temp_dir)
            pointer, _ = _write_sevendays_xml_files(
                root, server_properties={"GameWorld": "Kotavu County", "GameName": "GoodSave"}
            )
            for name in ("GoodSave", "The Leg!"):
                (root / "userdata" / "Saves" / "Kotavu County" / name).mkdir(parents=True)
            settings = SevenDays_Settings(pointer)
            game_world = settings.get_setting("GameWorld")
            if game_world is None:
                raise AssertionError("Missing expected 7D2D GameWorld setting")

            labels = [label for label, _ in game_world.choice_items()]

            self.assertIn("Kotavu County / GoodSave", labels)
            self.assertNotIn("Kotavu County / The Leg!", labels)

    def test_sevendays_settings_reject_invalid_stored_game_names(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            for value in ("", "   ", "The Leg!", "MyGame&#10;"):
                with self.subTest(value=value):
                    pointer, _ = _write_sevendays_xml_files(
                        Path(temp_dir), server_properties={"GameWorld": "RWG", "GameName": value}
                    )
                    with self.assertRaisesRegex(ValueError, "GameName"):
                        SevenDays_Settings(pointer)

    def test_sevendays_game_name_is_revalidated_before_saving_any_world_selection(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            pointer, rwgmixer_pointer = _write_sevendays_xml_files(
                Path(temp_dir), server_properties={"GameWorld": "RWG", "GameName": "MyGame"}
            )
            settings = SevenDays_Settings(pointer)
            game_world = settings.get_setting("GameWorld")
            game_name = settings.get_setting("GameName")
            if game_world is None or game_name is None:
                raise AssertionError("Missing expected 7D2D world save settings")
            original_server = pointer.read_bytes()
            original_mixer = rwgmixer_pointer.read_bytes()

            for selection in ("new-rwg", "world:Kotavu%20County", "save:Kotavu%20County/The%20Leg%21"):
                with self.subTest(selection=selection):
                    game_world.value = selection
                    game_name.value = "MyGame" if selection.startswith("save:") else "The Leg!"
                    with self.assertRaisesRegex(ValueError, "may only contain A-Z"):
                        settings.save()
                    self.assertEqual(pointer.read_bytes(), original_server)
                    self.assertEqual(rwgmixer_pointer.read_bytes(), original_mixer)

    def test_sevendays_settings_force_userdata_redirect_when_present_with_other_value(self) -> None:
        property_values = {
            "UserDataFolder": "/var/lib/7d2d",
            "ServerName": "Redirect Test",
            "GameWorld": "RWG",
            "GameName": "World",
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            settings = SevenDays_Settings(pointer)
            settings.save()

            saved = pointer.read_text(encoding="utf-8")
            expected_userdata_path = (pointer.parent / "userdata").resolve()
            self.assertIn(f'name="UserDataFolder" value="{expected_userdata_path}"', saved)
            self.assertNotIn('name="UserDataFolder" value="/var/lib/7d2d"', saved)

    def test_sevendays_world_save_selection_uses_discovered_pairs_and_fresh_generation(self) -> None:
        property_values = {
            "UserDataFolder": "userdata",
            "ServerName": "Choice Test",
            "GameWorld": "Navezgane",
            "GameName": "CurrentSave",
        }

        with TemporaryDirectory[str]() as temp_dir:
            root = Path(temp_dir)
            pointer, _ = _write_sevendays_xml_files(root, server_properties=property_values)
            (root / "userdata" / "Saves" / "Navezgane" / "CurrentSave").mkdir(parents=True)
            (root / "userdata" / "Saves" / "Wizefoco Mountains" / "ImportedSave").mkdir(parents=True)
            (root / "userdata" / "GeneratedWorlds" / "Wizefoco Mountains").mkdir(parents=True)
            (root / "userdata" / "Saves" / ".HiddenWorld" / "HiddenSave").mkdir(parents=True)
            (root / "userdata" / "Saves" / "Navezgane" / ".HiddenSave").mkdir(parents=True)

            settings = SevenDays_Settings(pointer)
            listed_settings = {setting.key: setting for setting in settings.options}
            game_world = listed_settings["GameWorld"]
            game_name = listed_settings["GameName"]

            self.assertTrue(game_world.strict_choice)
            self.assertFalse(game_name.strict_choice)
            self.assertEqual(
                game_world.choice_items(),
                (
                    ("Generate Fresh Random World", "new-rwg"),
                    ("Wizefoco Mountains / Fresh Characters", "world:Wizefoco%20Mountains"),
                    ("Navezgane / CurrentSave", "save:Navezgane/CurrentSave"),
                    ("Wizefoco Mountains / ImportedSave", "save:Wizefoco%20Mountains/ImportedSave"),
                ),
            )
            self.assertEqual(game_world.value, "save:Navezgane/CurrentSave")
            self.assertEqual(game_name.value, "CurrentSave")

            manager = Settings_Manager(
                App_Config(
                    name="sevendays_alpha",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="sevendays",
                ),
                settings,
            )
            manager.update_setting(actor_user_id=42, setting=game_world, value="Wizefoco Mountains / ImportedSave")
            self.assertEqual(manager.current_input_value(game_name, 42), "ImportedSave")
            manager.save(42)
            switched = pointer.read_text(encoding="utf-8")
            self.assertIn('name="GameWorld" value="Wizefoco Mountains"', switched)
            self.assertIn('name="GameName" value="ImportedSave"', switched)

            manager.update_setting(actor_user_id=42, setting=game_world, value="Generate Fresh Random World")
            manager.update_setting(actor_user_id=42, setting=game_name, value="FreshSave")
            manager.save(42)
            generated = pointer.read_text(encoding="utf-8")
            self.assertIn('name="GameWorld" value="RWG"', generated)
            self.assertIn('name="GameName" value="FreshSave"', generated)

            manager.update_setting(actor_user_id=42, setting=game_world, value="Wizefoco Mountains / Fresh Characters")
            manager.update_setting(actor_user_id=42, setting=game_name, value="FreshCharacters")
            manager.save(42)
            fresh_characters = pointer.read_text(encoding="utf-8")
            self.assertIn('name="GameWorld" value="Wizefoco Mountains"', fresh_characters)
            self.assertIn('name="GameName" value="FreshCharacters"', fresh_characters)

    def test_sevendays_quest_daily_limit_saves_and_reloads_unlimited(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer, _ = _write_sevendays_xml_files(
                Path(temporary), server_properties={"QuestProgressionDailyLimit": "4"},
            )
            settings = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.6"))
            manager = _sevendays_settings_manager(settings)
            manager.config.version = AppVersion(main="2.6")
            limit = settings.get_setting("QuestProgressionDailyLimit")
            assert limit is not None
            self.assertEqual(limit.default, 4)
            self.assertIn(("Unlimited", "-1"), limit.choice_items())
            self.assertFalse(limit.strict_choice)

            for value, expected in (("Unlimited", -1), ("-1", -1), ("1", 1), ("10", 10), ("11", 11)):
                with self.subTest(value=value):
                    manager.update_setting(42, limit, value)
                    manager.save(42)
                    persisted = _read_sevendays_properties(pointer)
                    self.assertEqual(persisted[limit.key], str(expected))
                    settings.load()
                    self.assertEqual(limit.value, expected)
                    if expected == -1:
                        self.assertEqual(limit.display_value(), "Unlimited (-1)")

    def test_sevendays_quest_daily_limit_rejects_invalid_negative_values(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer, _ = _write_sevendays_xml_files(
                Path(temporary), server_properties={"QuestProgressionDailyLimit": "4"},
            )
            settings = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.6"))
            limit = settings.get_setting("QuestProgressionDailyLimit")
            assert limit is not None

            for parse in (limit.update, limit.load_value):
                for value in ("-2", "-10", "1.5", "invalid"):
                    with self.subTest(parse=parse, value=value), self.assertRaises(ValueError):
                        parse(value)
                    self.assertEqual(limit.value, 4)

    def test_sevendays_settings_preserve_loaded_transfer_speed(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer, _ = _write_sevendays_xml_files(
                Path(temporary), server_properties={"ServerMaxWorldTransferSpeedKiBs": "4096"},
            )
            settings = SevenDays_Settings(pointer)
            transfer_speed = settings.get_setting("ServerMaxWorldTransferSpeedKiBs")
            assert transfer_speed is not None
            self.assertEqual(transfer_speed.value, 4096)
            settings.save()
            self.assertEqual(_read_sevendays_properties(pointer)[transfer_speed.key], "4096")

            for value in ("0", "2049", "8192"):
                with self.subTest(value=value):
                    transfer_speed.update(value)
                    settings.save()
                    self.assertEqual(_read_sevendays_properties(pointer)[transfer_speed.key], value)
                    settings.load()
                    self.assertEqual(transfer_speed.value, int(value))

    def test_sevendays_settings_save_advanced_world_sizes(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer, _ = _write_sevendays_xml_files(Path(temporary), server_properties={"WorldGenSize": "4096"})
            settings = SevenDays_Settings(pointer)
            world_size = settings.get_setting("WorldGenSize")
            assert world_size is not None
            self.assertEqual(world_size.value, 4096)

            for value in ("2048", "3072", "5120", "7168", "9216"):
                with self.subTest(value=value):
                    world_size.update(value)
                    settings.save()
                    persisted = _read_sevendays_properties(pointer)
                    self.assertEqual(persisted[world_size.key], value)
                    settings.load()
                    self.assertEqual(world_size.value, int(value))
            with self.assertRaises(ValueError):
                world_size.update("12288")

    def test_sevendays_settings_blank_camera_restriction_keeps_default(self) -> None:
        property_values = {
            "CameraRestrictionMode": "",
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            settings = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="3.0"))
            camera_restriction = settings.get_setting("CameraRestrictionMode")
            if camera_restriction is None:
                raise AssertionError("Missing camera restriction setting")

            self.assertEqual(camera_restriction.value, 0)

            settings.save()

            saved = pointer.read_text(encoding="utf-8")
            self.assertIn('name="CameraRestrictionMode" value="0"', saved)

    def test_sevendays_save_loads_settings_when_the_version_becomes_supported(self) -> None:
        properties = {"BiomeProgression": "false", "GameDifficulty": "4", "XPMultiplier": "150"}
        for initial_version in (None, AppVersion(main="3.0")):
            with self.subTest(initial_version=initial_version), TemporaryDirectory() as temp:
                pointer, _ = _write_sevendays_xml_files(Path(temp), server_properties=properties)
                version = initial_version
                settings = SevenDays_Settings(pointer, version_getter=lambda: version)
                self.assertIsNone(settings.get_setting("BiomeProgression"))

                version = AppVersion(main="2.6")
                settings.save()

                persisted = {node.get("name"): node.get("value") for node in ET.parse(pointer).getroot()}
                for key, value in properties.items():
                    self.assertEqual(persisted[key], value, key)

    def test_sevendays_save_rejects_invalid_newly_supported_settings_before_writing(self) -> None:
        with TemporaryDirectory() as temp:
            pointer, rwgmixer = _write_sevendays_xml_files(
                Path(temp), server_properties={"XPMultiplier": "150", "BiomeProgression": "invalid"},
            )
            version = AppVersion(main="3.0")
            settings = SevenDays_Settings(pointer, version_getter=lambda: version)
            manager = _sevendays_settings_manager(settings)
            original_config = pointer.read_bytes()
            original_rwgmixer = rwgmixer.read_bytes()

            manager.config.version = AppVersion(main="2.6")
            xp_multiplier = settings.get_setting("XPMultiplier")
            assert xp_multiplier is not None
            previous_value = xp_multiplier.value
            with self.assertRaisesRegex(ValueError, "Biome Progression"):
                manager.save(42)

            self.assertEqual(pointer.read_bytes(), original_config)
            self.assertEqual(rwgmixer.read_bytes(), original_rwgmixer)
            self.assertIs(xp_multiplier.value, previous_value)

    def test_sevendays_sandbox_code_requires_version_three(self) -> None:
        property_values = {
            "SandboxCode": "AAAJABJACJADJARFBNC",
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            settings_v2 = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.0"))
            settings_v3 = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="3.0"))

            self.assertIsNone(settings_v2.get_setting("SandboxCode"))
            sandbox_code = settings_v3.get_setting("SandboxCode")
            if sandbox_code is None:
                raise AssertionError("Missing expected 7D2D sandbox code setting")
            self.assertEqual(sandbox_code.value, "AAAJABJACJADJARFBNC")

            with self.assertRaisesRegex(ValueError, "Invalid value for Sandbox Code"):
                sandbox_code.update("")

    def test_sevendays_trader_biomes_swap_without_overlap(self) -> None:
        property_values = {
            "ServerName": "Biomes",
            "ServerDescription": "Biomes",
            "ServerPassword": "",
            "ServerVisibility": "2",
            "ServerMaxWorldTransferSpeedKiBs": "512",
            "ServerMaxPlayerCount": "8",
            "ServerReservedSlots": "0",
            "ServerAdminSlots": "0",
            "GameWorld": "RWG",
            "WorldGenSeed": "seed",
            "WorldGenSize": "6144",
            "GameName": "World",
            "GameDifficulty": "1",
            "BlockDamagePlayer": "100",
            "BlockDamageAI": "100",
            "BlockDamageAIBM": "100",
            "XPMultiplier": "100",
            "DayNightLength": "60",
            "DayLightLength": "18",
            "BiomeProgression": "true",
            "WebDashboardEnabled": "false",
            "StormFreq": "100",
            "DeathPenalty": "1",
            "DropOnDeath": "1",
            "DropOnQuit": "0",
            "CameraRestrictionMode": "0",
            "JarRefund": "60",
            "EnemyDifficulty": "0",
            "ZombieFeralSense": "0",
            "ZombieMove": "0",
            "ZombieMoveNight": "3",
            "ZombieFeralMove": "3",
            "ZombieBMMove": "3",
            "AISmellMode": "3",
            "BloodMoonFrequency": "7",
            "BloodMoonRange": "0",
            "BloodMoonWarning": "8",
            "LootAbundance": "100",
            "LootRespawnDays": "7",
            "AirDropFrequency": "72",
            "AirDropMarker": "true",
            "PartySharedKillRange": "100",
            "PlayerKillingMode": "3",
            "QuestProgressionDailyLimit": "4",
        }

        with TemporaryDirectory[str]() as temp_dir:
            root = Path(temp_dir)
            pointer, _ = _write_sevendays_xml_files(root, server_properties=property_values)
            settings = SevenDays_Settings(pointer)
            manager = Settings_Manager(
                App_Config(
                    name="sevendays_alpha",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="sevendays",
                ),
                settings,
            )
            rekt = settings.get_setting("TraderRektBiome")
            jen = settings.get_setting("TraderJenBiome")
            if rekt is None or jen is None:
                raise AssertionError("Missing trader biome settings")

            manager.update_setting(actor_user_id=42, setting=rekt, value="Burnt Forest")

            self.assertEqual(manager.current_input_value(rekt, 42), "Burnt Forest")
            self.assertEqual(manager.current_input_value(jen, 42), "Forest")

            manager.save(42)

            saved_rwgmixer = (root / "Data" / "Config" / "rwgmixer.xml").read_text(encoding="utf-8")
            self.assertIn('partial_name="trader_rekt" biomeTags="burntforest"', saved_rwgmixer)
            self.assertIn('partial_name="trader_jen" biomeTags="forest"', saved_rwgmixer)

    def test_factorio_settings_register_comments_and_root_only_fields(self) -> None:
        visibility_comment: list[str] = [
            "public: Game will be published on the official Factorio matching server",
            "lan: Game will be broadcast on LAN",
        ]
        credentials_comment = "Your factorio.com login credentials. Required for games with visibility public"
        username_comment = "Comment based username description that should be ignored."
        username_description = "Factorio account username used for mod portal authentication."
        token_comment = "Authentication token. May be used instead of 'password' above."
        payload: dict[str, str | int | list[str] | dict[str, bool] | bool] = {
            "name": "Rail Society 2 test",
            "description": "Rail Harder",
            "_comment_max_players": "Maximum number of players allowed, admins can join even a full server. 0 means unlimited.",
            "max_players": 5,
            "_comment_visibility": visibility_comment,
            "visibility": {"public": False, "lan": True},
            "game_password": "hugs",
            "_comment_require_user_verification": (
                "When set to true, the server will only allow clients that have a valid Factorio.com account"
            ),
            "require_user_verification": False,
            "_comment_max_upload_in_kilobytes_per_second": "optional, default value is 0. 0 means unlimited.",
            "max_upload_in_kilobytes_per_second": 0,
            "_comment_max_upload_slots": "optional, default value is 5. 0 means unlimited.",
            "max_upload_slots": 5,
            "_comment_minimum_latency_in_ticks": (
                "optional one tick is 16ms in default speed, default value is 0. 0 means no minimum."
            ),
            "minimum_latency_in_ticks": 0,
            "_comment_max_heartbeats_per_second": (
                "Network tick rate. Maximum rate game updates packets are sent at before bundling them together."
            ),
            "max_heartbeats_per_second": 60,
            "_comment_autosave_interval": "Autosave interval in minutes",
            "autosave_interval": 10,
            "_comment_ignore_player_limit_for_returning_players": (
                "Players that played on this map already can join even when the max player limit was reached."
            ),
            "ignore_player_limit_for_returning_players": False,
            "_comment_afk_autokick_interval": "How many minutes until someone is kicked when doing nothing, 0 for never.",
            "afk_autokick_interval": 0,
            "_comment_auto_pause": "Whether should the server be paused when no players are present.",
            "auto_pause": True,
            "_comment_auto_pause_when_players_connect": (
                "Whether should the server be paused when someone is connecting to the server."
            ),
            "auto_pause_when_players_connect": False,
            "only_admins_can_pause_the_game": True,
            "_comment_autosave_only_on_server": (
                "Whether autosaves should be saved only on server or also on all connected clients. Default is true."
            ),
            "autosave_only_on_server": True,
            "_comment_credentials": credentials_comment,
            "_comment_username": username_comment,
            "username": "APasz",
            "password": "",
            "_comment_token": token_comment,
            "token": "c44859afb3b45ac346debd70b0d2da",
            "_comment_non_blocking_saving": "Highly experimental feature, enable only at your own risk of losing your saves.",
            "non_blocking_saving": True,
        }

        with TemporaryDirectory[str]() as temp_dir:
            pointer: Path = Path(temp_dir) / "server-settings.json"
            pointer.write_text(json.dumps(payload, indent=4), encoding="utf-8")

            settings: Factorio_Settings = Factorio_Settings(pointer)

            for key in (
                "require_user_verification",
                "max_upload_in_kilobytes_per_second",
                "max_upload_slots",
                "minimum_latency_in_ticks",
                "max_heartbeats_per_second",
                "autosave_interval",
                "ignore_player_limit_for_returning_players",
                "afk_autokick_interval",
                "auto_pause",
                "auto_pause_when_players_connect",
                "only_admins_can_pause_the_game",
                "autosave_only_on_server",
                "username",
                "password",
                "token",
                "non_blocking_saving",
            ):
                self.assertIsNotNone(settings.get_setting(key), key)

            visibility = settings.get_setting("public")
            username = settings.get_setting("username")
            password = settings.get_setting("password")
            token = settings.get_setting("token")
            heartbeats = settings.get_setting("max_heartbeats_per_second")
            verification = settings.get_setting("require_user_verification")
            autosave_server_only = settings.get_setting("autosave_only_on_server")
            if any(
                item is None
                for item in (visibility, username, password, token, heartbeats, verification, autosave_server_only)
            ):
                raise AssertionError("Missing expected Factorio setting")
            assert visibility is not None
            assert username is not None
            assert password is not None
            assert token is not None
            assert heartbeats is not None
            assert verification is not None
            assert autosave_server_only is not None

            self.assertEqual(visibility.desc, visibility_comment[0] + " " + visibility_comment[1])
            self.assertEqual(username.desc, username_description)
            self.assertEqual(password.desc, credentials_comment)
            self.assertEqual(token.desc, token_comment)
            self.assertEqual(username.power_level.name, "root")
            self.assertEqual(password.power_level.name, "root")
            self.assertEqual(token.power_level.name, "root")
            self.assertEqual(heartbeats.value, 60)
            self.assertIs(verification.value, False)
            self.assertIs(autosave_server_only.value, True)

            for key, value in {
                "public": "Public",
                "require_user_verification": "true",
                "max_upload_slots": "9",
                "max_heartbeats_per_second": "120",
                "auto_pause": "false",
                "username": "RailAdmin",
                "token": "new-token",
                "non_blocking_saving": "true",
            }.items():
                setting = settings.get_setting(key)
                if setting is None:
                    raise AssertionError(f"Missing setting {key}")
                setting.update(value)

            settings.save()

            saved = json.loads(pointer.read_text(encoding="utf-8"))
            self.assertIs(saved["visibility"]["public"], True)
            self.assertIs(saved["require_user_verification"], True)
            self.assertEqual(saved["max_upload_slots"], 9)
            self.assertEqual(saved["max_heartbeats_per_second"], 120)
            self.assertIs(saved["auto_pause"], False)
            self.assertEqual(saved["username"], "RailAdmin")
            self.assertEqual(saved["token"], "new-token")
            self.assertIs(saved["non_blocking_saving"], True)
            self.assertEqual(saved["_comment_token"], payload["_comment_token"])

    def test_factorio_settings_selects_a_discovered_save_without_modifying_server_settings(self) -> None:
        with TemporaryDirectory[str]() as temp_dir:
            root = Path(temp_dir)
            saves_directory = root / "saves"
            saves_directory.mkdir()
            (saves_directory / "Alpha.zip").write_bytes(b"save")
            settings, cfg, instances = _factorio_settings_with_instance_config(root)
            selection = settings.get_setting("factorio_save_selection")
            if selection is None:
                raise AssertionError("Missing Factorio save selection setting")

            self.assertEqual(
                selection.spec.choice_items(),
                (("Latest Save", "latest"), ("Create Fresh World", "fresh"), ("Alpha.zip", "save:Alpha.zip")),
            )
            selection.update("Alpha.zip")
            settings.save()

            self.assertEqual(cfg.factorio_save_file, "Alpha.zip")
            self.assertFalse(cfg.factorio_create_fresh_world)
            self.assertNotIn("factorio_save_selection", json.loads(settings.pointer.read_text(encoding="utf-8")))

            fresh_save_name = settings.get_setting("factorio_fresh_save_file")
            if fresh_save_name is None:
                raise AssertionError("Missing Factorio fresh save name setting")
            selection.update("Create Fresh World")
            fresh_save_name.update("Orbit")
            settings.save()

            self.assertEqual(cfg.factorio_save_file, "Orbit.zip")
            self.assertTrue(cfg.factorio_create_fresh_world)
            self.assertEqual(cfg.factorio_fresh_save_file, "Orbit")
            self.assertEqual(json.loads(instances.read_text()), {
                "alpha": {
                    "enabled": True, "factorio_save_file": "Orbit.zip", "factorio_create_fresh_world": True,
                    "factorio_fresh_save_file": "Orbit",
                },
                "other": {"enabled": False},
            })

    def test_failed_factorio_world_selection_preserves_both_files_config_and_drafts(self) -> None:
        original_replace = Path.replace
        for fail_replacement in (False, True):
            with self.subTest(fail_replacement=fail_replacement), TemporaryDirectory() as temporary:
                settings, cfg, instances = _factorio_settings_with_instance_config(Path(temporary))
                manager = Settings_Manager(cfg, settings)
                for key, value in {
                    "name": "Changed server", "factorio_save_selection": "Create Fresh World",
                    "factorio_fresh_save_file": "Orbit",
                }.items():
                    setting = settings.get_setting(key)
                    assert setting is not None
                    manager.update_setting(42, setting, value)
                if not fail_replacement:
                    instances.write_text("{}")
                original_server, original_instances = settings.pointer.read_bytes(), instances.read_bytes()

                def replace(source: Path, target: Path) -> Path:
                    if fail_replacement and target == instances:
                        raise OSError("write failed")
                    return original_replace(source, target)

                with patch.object(Path, "replace", new=replace), self.assertRaises(
                    OSError if fail_replacement else ValueError,
                ):
                    manager.save(42)

                self.assertEqual(settings.pointer.read_bytes(), original_server)
                self.assertEqual(instances.read_bytes(), original_instances)
                self.assertIsNone(cfg.factorio_save_file)
                self.assertFalse(cfg.factorio_create_fresh_world)
                self.assertEqual(cfg.factorio_fresh_save_file, "New World.zip")
                self.assertEqual(manager.pending_change_count(42), 3)

    def test_settings_state_round_trips(self) -> None:
        state: AppManageState = AppManageState(
            mode=AppManageMode.SETTINGS,
            page=2,
            app_name="factorio",
            selected_setting_index=4,
        )

        encoded: str = _state_value(state)

        self.assertEqual(_state_from_value(encoded, 2), state)

    def test_settings_state_requires_app_name(self) -> None:
        encoded: str = _state_value(AppManageState(mode=AppManageMode.SETTINGS, page=0))

        self.assertIsNone(_state_from_value(encoded, 0))

    def test_setting_choices_state_requires_selected_setting_index(self) -> None:
        encoded: str = _state_value(
            AppManageState(
                mode=AppManageMode.SETTING_CHOICES,
                page=0,
                app_name="factorio",
            )
        )

        self.assertIsNone(_state_from_value(encoded, 0))


class UndeadLegacySettingTests(unittest.TestCase):
    @staticmethod
    def _settings(root: Path, **properties: str) -> SevenDays_Settings:
        pointer, _ = _write_sevendays_xml_files(
            root,
            server_properties={
                "GameWorld": "Navezgane",
                "GameName": "Review",
                **properties,
            },
        )
        manifest = root / UNDEAD_LEGACY_MANIFEST
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text(
            UndeadLegacyInstallation(
                version=UNDEAD_LEGACY_RELEASE.version,
                game_version=UNDEAD_LEGACY_RELEASE.game_version,
                core_sha256=UNDEAD_LEGACY_RELEASE.core_sha256,
                mod_names=("UndeadLegacy",),
            ).model_dump_json()
        )
        return SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.6"))

    def test_ul_gameplay_settings_survive_build_and_main_only_version_reports(self) -> None:
        properties = {
            "BiomeProgression": "false",
            "GameDifficulty": "4",
            "DayNightLength": "90",
            "XPMultiplier": "150",
            "AirDropMarker": "false",
            "ServerWebsiteURL": "",
            "QuestProgressionDailyLimit": "-1",
            "DuskTime": "24",
            "WorldGenSize": "14336",
        }
        with TemporaryDirectory() as temp:
            pointer = self._settings(Path(temp), **properties).pointer
            version = AppVersion(main="2.6", build=14)
            settings = SevenDays_Settings(pointer, version_getter=lambda: version)
            settings.save()
            version = AppVersion(main="2.6")
            settings.save()

            persisted = {node.get("name"): node.get("value") for node in ET.parse(pointer).getroot()}
            for key, value in properties.items():
                self.assertEqual(persisted[key], value, key)

    def test_ul_built_version_loads_and_repairs_blank_gameplay_preferences(self) -> None:
        with TemporaryDirectory() as temp:
            pointer = self._settings(Path(temp), BiomeProgression="", GameDifficulty="").pointer
            settings = SevenDays_Settings(pointer, version_getter=lambda: AppVersion(main="2.6", build=14))
            biome_progression = settings.get_setting("BiomeProgression")
            difficulty = settings.get_setting("GameDifficulty")
            self.assertIsNotNone(biome_progression)
            self.assertIsNotNone(difficulty)
            assert biome_progression is not None and difficulty is not None
            self.assertIs(biome_progression.value, True)
            self.assertEqual(difficulty.value, 1)

            settings.save()

            persisted = {node.get("name"): node.get("value") for node in ET.parse(pointer).getroot()}
            self.assertEqual(persisted["BiomeProgression"], "true")
            self.assertEqual(persisted["GameDifficulty"], "1")

    def test_ul_group_is_only_available_for_managed_overhaul_instances(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._settings(root)
            grouped = [
                setting
                for setting in settings.settings_page_options
                if setting.group is SevenDaysSettingGroup.UNDEAD_LEGACY
            ]
            self.assertEqual(len(grouped), 18)
            self.assertEqual({setting.type_name for setting in grouped}, {"int", "bool"})
            (root / UNDEAD_LEGACY_MANIFEST).unlink()
            vanilla = SevenDays_Settings(settings.pointer, version_getter=lambda: AppVersion(main="2.6"))
            self.assertIsNone(vanilla.get_setting("ActionSkillXPMultiplier"))

    def test_save_inserts_ul_preferences_and_preserves_existing_configuration(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp), CustomPreference="untouched", OverrideBrightness="-1")
            manager = _sevendays_settings_manager(settings)
            for key, value in (
                ("ActionSkillXPMultiplier", "125"),
                ("AIHeatMapScoutsEnabled", "false"),
                ("OverrideBrightness", "-1"),
            ):
                setting = settings.get_setting(key)
                assert setting is not None
                manager.update_setting(42, setting, value)
            manager.save(42)
            persisted = {node.get("name"): node.get("value") for node in ET.parse(settings.pointer).getroot()}
            self.assertEqual(persisted["ActionSkillXPMultiplier"], "125")
            self.assertEqual(persisted["AIHeatMapScoutsEnabled"], "false")
            self.assertEqual(persisted["OverrideBrightness"], "-1")
            self.assertEqual(persisted["CustomPreference"], "untouched")
            manager.load()
            setting = settings.get_setting("ActionSkillXPMultiplier")
            assert setting is not None
            self.assertEqual(setting.value, 125)

    def test_ul_settings_save_larger_world_sizes_and_midnight_dusk(self) -> None:
        with TemporaryDirectory() as temporary:
            settings = self._settings(Path(temporary))
            manager = _sevendays_settings_manager(settings)
            world_size = settings.get_setting("WorldGenSize")
            dusk = settings.get_setting("DuskTime")
            assert world_size is not None and dusk is not None
            manager.update_setting(42, dusk, "24")

            for value in ("11264", "12288", "13312", "14336", "15360"):
                with self.subTest(value=value):
                    manager.update_setting(42, world_size, value)
                    manager.save(42)
                    persisted = _read_sevendays_properties(settings.pointer)
                    self.assertEqual(persisted[world_size.key], value)
                    self.assertEqual(persisted[dusk.key], "24")
                    manager.load()
                    self.assertEqual(world_size.value, int(value))
                    self.assertEqual(dusk.value, 24)

    def test_wandering_horde_limit_is_checked_before_accepting_a_draft(self) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp))
            manager = _sevendays_settings_manager(settings)
            count = settings.get_setting("WanderingHordeEnemyCount")
            random_range = settings.get_setting("WanderingHordeEnemyRange")
            assert count is not None and random_range is not None
            manager.update_setting(42, count, "54")
            with self.assertRaisesRegex(ValueError, "must not exceed 64"):
                manager.update_setting(42, random_range, "11")
            self.assertFalse(manager.has_pending_value(42, random_range))
            manager.update_setting(42, random_range, "10")
            manager.save(42)
            self.assertEqual(count.value, 54)

    def test_unchanged_ul_defaults_are_not_written_to_the_server_config(self) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp))
            settings.save()
            stored_keys = {node.get("name") for node in ET.parse(settings.pointer).getroot()}
            ul_keys = {setting.key for setting in settings.options if setting.group is SevenDaysSettingGroup.UNDEAD_LEGACY}
            self.assertFalse(stored_keys & ul_keys)

    def test_conflicting_user_drafts_do_not_change_saved_values_after_a_failed_save(self) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp))
            manager = _sevendays_settings_manager(settings)
            count = settings.get_setting("WanderingHordeEnemyCount")
            random_range = settings.get_setting("WanderingHordeEnemyRange")
            assert count is not None and random_range is not None
            manager.update_setting(42, count, "54")
            manager.update_setting(43, random_range, "54")
            manager.save(42)
            saved = settings.pointer.read_bytes()
            with self.assertRaisesRegex(ValueError, "must not exceed 64"):
                manager.save(43)
            self.assertEqual((count.value, random_range.value), (54, 10))
            self.assertEqual(settings.pointer.read_bytes(), saved)
            self.assertTrue(manager.has_pending_value(43, random_range))
            manager.update_setting(43, random_range, "8")
            manager.save(43)
            self.assertEqual((count.value, random_range.value), (54, 8))

    def test_ul_bounds_and_integer_preferences_reject_invalid_values(self) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp))
            for key, value in (
                ("RecipeFilter", "3"),
                ("DawnTime", "24"),
                ("DuskTime", "25"),
                ("POITierLootScale", "101"),
                ("WanderingHordeFrequency", "0"),
                ("OverrideBrightness", "1.5"),
                ("OverrideBrightness", "-2"),
            ):
                with self.subTest(key=key, value=value):
                    setting = settings.get_setting(key)
                    assert setting is not None
                    with self.assertRaises((ValueError, IndexError)):
                        setting.spec.parse_input(value)

    def test_reload_restores_default_when_a_ul_property_is_removed(self) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp), ActionSkillXPMultiplier="150")
            setting = settings.get_setting("ActionSkillXPMultiplier")
            assert setting is not None
            self.assertEqual(setting.value, 150)
            tree = ET.parse(settings.pointer)
            for node in tuple(tree.getroot()):
                if node.get("name") == "ActionSkillXPMultiplier":
                    tree.getroot().remove(node)
            tree.write(settings.pointer)
            settings.load()
            self.assertEqual(setting.value, 100)

    def test_duplicate_ul_properties_fail_with_a_clear_error(self) -> None:
        with TemporaryDirectory() as temp:
            settings = self._settings(Path(temp), ActionSkillXPMultiplier="150")
            tree = ET.parse(settings.pointer)
            ET.SubElement(tree.getroot(), "property", {"name": "ActionSkillXPMultiplier", "value": "100"})
            tree.write(settings.pointer)
            with self.assertRaisesRegex(ValueError, "Duplicate Undead Legacy setting"):
                settings.load()


if __name__ == "__main__":
    unittest.main()
