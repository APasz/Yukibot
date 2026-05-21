from __future__ import annotations

import unittest

from apps._settings import Setting, Setting_Label
from cmd_app import AppManageMode, AppManageState, _state_from_value, _state_value


class SettingTests(unittest.TestCase):
    def test_choice_labels_are_accepted_and_displayed(self) -> None:
        setting = Setting(
            bool,
            Setting_Label.visibility,
            "public",
            ["visibility"],
            choices={"Public": "true", "Private": "false"},
        )

        setting.update("Public")

        self.assertIs(setting.value, True)
        self.assertEqual(setting.choice_label_for_value(), "Public")
        self.assertEqual(setting.display_value(), "Public (True)")

    def test_settings_state_round_trips(self) -> None:
        state = AppManageState(
            mode=AppManageMode.SETTINGS,
            page=2,
            app_name="factorio",
            selected_setting_key="map",
        )

        encoded = _state_value(state)

        self.assertEqual(_state_from_value(encoded, 2), state)

    def test_settings_state_requires_app_name(self) -> None:
        encoded = _state_value(AppManageState(mode=AppManageMode.SETTINGS, page=0))

        self.assertIsNone(_state_from_value(encoded, 0))

    def test_setting_choices_state_requires_selected_setting_key(self) -> None:
        encoded = _state_value(
            AppManageState(
                mode=AppManageMode.SETTING_CHOICES,
                page=0,
                app_name="factorio",
            )
        )

        self.assertIsNone(_state_from_value(encoded, 0))


if __name__ == "__main__":
    unittest.main()
