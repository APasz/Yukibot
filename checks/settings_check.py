from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

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
    Setting_Label,
    SettingStateForceRule,
    Settings_Manager,
    StringSettingSpec,
)
from apps.factorio import Factorio_Settings
from apps.minecraft import Minecraft_Settings
from apps.sevendays import SevenDays_Settings
from cmd_app import AppManageMode, AppManageState, _state_from_value, _state_value


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


class SettingTests(unittest.TestCase):
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
            self.assertEqual(world_gen_size.desc, "Supported RWG world size preset.")
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

            with self.assertRaisesRegex(ValueError, "2048"):
                transfer_speed.update("2049")

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
            self.assertIn('name="UserDataFolder" value="userdata"', saved)

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
            self.assertIn('name="UserDataFolder" value="userdata"', saved)
            self.assertNotIn('name="UserDataFolder" value="/var/lib/7d2d"', saved)

    def test_sevendays_settings_clamp_loaded_transfer_speed_without_startup_error(self) -> None:
        property_values = {
            "ServerName": "Clamp Test",
            "ServerDescription": "Clamp Test",
            "ServerPassword": "",
            "ServerVisibility": "2",
            "ServerMaxWorldTransferSpeedKiBs": "4096",
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
            pointer, _ = _write_sevendays_xml_files(Path(temp_dir), server_properties=property_values)

            settings = SevenDays_Settings(pointer)
            transfer_speed = settings.get_setting("ServerMaxWorldTransferSpeedKiBs")
            if transfer_speed is None:
                raise AssertionError("Missing transfer speed setting")

            self.assertEqual(transfer_speed.value, 2048)

            settings.save()

            saved = pointer.read_text(encoding="utf-8")
            self.assertIn('name="ServerMaxWorldTransferSpeedKiBs" value="2048"', saved)

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


if __name__ == "__main__":
    unittest.main()
