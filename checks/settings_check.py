from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _security import Power_Level
from apps._settings import (
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Setting_Label,
    StringSettingSpec,
)
from apps.factorio import Factorio_Settings
from apps.minecraft import Minecraft_Settings
from apps.sevendays import SevenDays_Settings
from cmd_app import AppManageMode, AppManageState, _state_from_value, _state_value


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
            "ServerMaxWorldTransferSpeedKiBs": "2048",
            "ServerMaxPlayerCount": "3",
            "ServerReservedSlots": "0",
            "ServerAdminSlots": "0",
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
            pointer = Path(temp_dir) / "serverconfig.xml"
            properties_xml = "\n".join(
                f'    <property name="{name}" value="{value}"/>' for name, value in property_values.items()
            )
            pointer.write_text(
                f'<?xml version="1.0"?>\n<ServerSettings>\n{properties_xml}\n</ServerSettings>\n',
                encoding="utf-8",
            )

            settings: SevenDays_Settings = SevenDays_Settings(pointer)

            for key in property_values:
                self.assertIsNotNone(settings.get_setting(key), key)

            max_players = settings.get_setting("ServerMaxPlayerCount")
            biome_progression = settings.get_setting("BiomeProgression")
            visibility = settings.get_setting("ServerVisibility")
            if max_players is None or biome_progression is None or visibility is None:
                raise AssertionError("Missing expected 7D2D setting")

            self.assertEqual(max_players.value, 3)
            self.assertIs(biome_progression.value, True)
            self.assertEqual(visibility.choice_label_for_value(), "Public")

            for key, value in {
                "ServerDescription": "Private run",
                "ServerVisibility": "Friends",
                "ServerMaxWorldTransferSpeedKiBs": "1024",
                "BiomeProgression": "false",
                "JarRefund": "60",
                "AirDropMarker": "false",
                "QuestProgressionDailyLimit": "6",
            }.items():
                setting = settings.get_setting(key)
                if setting is None:
                    raise AssertionError(f"Missing setting {key}")
                setting.update(value)

            settings.save()

            saved: str = pointer.read_text(encoding="utf-8")
            self.assertIn('name="ServerDescription" value="Private run"', saved)
            self.assertIn('name="ServerVisibility" value="1"', saved)
            self.assertIn('name="ServerMaxWorldTransferSpeedKiBs" value="1024"', saved)
            self.assertIn('name="BiomeProgression" value="false"', saved)
            self.assertIn('name="JarRefund" value="60"', saved)
            self.assertIn('name="AirDropMarker" value="false"', saved)
            self.assertIn('name="QuestProgressionDailyLimit" value="6"', saved)

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
