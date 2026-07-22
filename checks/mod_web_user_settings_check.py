from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from web_dash.user_settings import (
    ModWebAppearanceSettings,
    ModWebChatSettings,
    ModWebColorScheme,
    ModWebUserSettings,
    ModWebUserSettingsStore,
)


class ModWebUserSettingsStoreTests(unittest.TestCase):
    def test_settings_round_trip_by_discord_user_id(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_settings.json"
            store = ModWebUserSettingsStore(path)
            settings = ModWebUserSettings(
                appearance=ModWebAppearanceSettings(
                    color_scheme=ModWebColorScheme.DARK,
                    primary_color_hex="#22c55e",
                    positive_color_hex="#16a34a",
                    warning_color_hex="#facc15",
                    negative_color_hex="#ef4444",
                    info_color_hex="#0ea5e9",
                ),
                web_chat=ModWebChatSettings(use_24_hour_time=False),
            )

            with patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.LOCAL):
                self.assertEqual(store.get(user_id=42), ModWebUserSettings())
                self.assertTrue(store.set(user_id=42, settings=settings))
                self.assertFalse(store.set(user_id=42, settings=settings))
                restored = ModWebUserSettingsStore(path).get(user_id=42)

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(restored, settings)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["users"]["42"]["appearance"]["primary_color_hex"], "#22C55E")
        self.assertEqual(payload["users"]["42"]["appearance"]["positive_color_hex"], "#16A34A")
        self.assertEqual(payload["users"]["42"]["appearance"]["warning_color_hex"], "#FACC15")
        self.assertEqual(payload["users"]["42"]["appearance"]["negative_color_hex"], "#EF4444")
        self.assertEqual(payload["users"]["42"]["appearance"]["info_color_hex"], "#0EA5E9")

    def test_store_rejects_invalid_user_ids_and_payloads(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_settings.json"
            path.write_text('{"version": 1, "users": {"0": {}}}', encoding="utf-8")
            store = ModWebUserSettingsStore(path)

            with patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.LOCAL):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    store.get(user_id=0)
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    store.get(user_id=42)

    def test_appearance_settings_reject_invalid_primary_colour(self) -> None:
        with self.assertRaisesRegex(ValueError, "six-digit"):
            ModWebAppearanceSettings(warning_color_hex="#1234")

    def test_remote_store_uses_atomic_single_user_mutation(self) -> None:
        settings = ModWebUserSettings(web_chat=ModWebChatSettings(use_24_hour_time=False))
        response_payload = {
            "version": 1,
            "users": {
                "42": settings.model_dump(mode="json"),
                "99": ModWebUserSettings().model_dump(mode="json"),
            },
        }
        store = ModWebUserSettingsStore()

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "load_authority_json", return_value={"version": 1, "users": {}}),
            patch.object(config, "mutate_remote_user_settings", return_value=response_payload) as mutate,
        ):
            self.assertTrue(store.set(user_id=42, settings=settings))

        mutate.assert_called_once_with(user_id=42, settings=settings.model_dump(mode="json"))
        self.assertEqual(store.get(user_id=99), ModWebUserSettings())


if __name__ == "__main__":
    unittest.main()
