from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from aiohttp import web

import config
from _authority_server import AuthorityServer


class _FakeJsonRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def json(self) -> dict[str, object]:
        return self._payload


def _response_payload(response: object) -> dict[str, object]:
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        raise AssertionError("response body must be bytes")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("response payload must be a JSON object")
    return payload


class AuthorityServerBotRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_bots_sync_persists_snapshot_to_known_bots(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            snapshot = config.BotMetadataSnapshot(
                profile=config.BotMetadataProfile(
                    id="1350601198637551659",
                    label="Erin",
                    bot_profile=config.BotProfileName.ERIN,
                ),
                features=config.BotMetadataFeatures(
                    oauth=config.PersistedOAuthLinks(guild="https://example.com/guild")
                ),
            )

            with patch.object(AuthorityServer, "_BOT_CONFIGURATION_PATH", path):
                server = AuthorityServer(cast(config.Name_Cache, cast(object, SimpleNamespace())))
                response = await server._handle_bots_sync(
                    cast(web.Request, cast(object, _FakeJsonRequest({"data": snapshot.model_dump(mode="json")})))
                )

            payload = _response_payload(response)
            saved = config.load_bot_configuration(path)

        self.assertTrue(payload["ok"])
        self.assertIn("1350601198637551659", saved.known_bots)
        self.assertEqual(saved.known_bots["1350601198637551659"], snapshot)

    async def test_handle_bots_returns_known_bot_registry(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.json"
            snapshot = config.BotMetadataSnapshot(
                profile=config.BotMetadataProfile(
                    id="1350601198637551659",
                    label="Erin",
                ),
                features=config.BotMetadataFeatures(oauth=config.PersistedOAuthLinks(guild=None)),
            )
            config.save_bot_configuration(
                path,
                config.BotConfiguration(KnownBots={"1350601198637551659": snapshot}),
            )

            with patch.object(AuthorityServer, "_BOT_CONFIGURATION_PATH", path):
                server = AuthorityServer(cast(config.Name_Cache, cast(object, SimpleNamespace())))
                response = await server._handle_bots(cast(web.Request, cast(object, SimpleNamespace())))

            payload = _response_payload(response)
            raw_data = payload.get("data")

        self.assertIsInstance(raw_data, dict)
        assert isinstance(raw_data, dict)
        snapshot_payload = raw_data["1350601198637551659"]
        snapshot = config.BotMetadataSnapshot.model_validate(snapshot_payload)

        self.assertEqual(snapshot.profile.label, "Erin")
        self.assertEqual(snapshot.features.oauth, config.PersistedOAuthLinks(guild=None))


class AuthorityServerUserSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_settings_replace_round_trips_through_authority(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_settings.json"
            server = AuthorityServer(cast(config.Name_Cache, cast(object, SimpleNamespace())))
            user_settings_payload = {
                "version": 1,
                "users": {"42": {"appearance": {"color_scheme": "current", "primary_color_hex": None}}},
            }

            with patch.object(config, "USER_SETTINGS", path):
                initial_response = await server._handle_user_settings(cast(web.Request, cast(object, SimpleNamespace())))
                replace_response = await server._handle_user_settings_replace(
                    cast(web.Request, cast(object, _FakeJsonRequest({"data": user_settings_payload})))
                )
                loaded_response = await server._handle_user_settings(cast(web.Request, cast(object, SimpleNamespace())))

            initial_payload = _response_payload(initial_response)
            replace_payload = _response_payload(replace_response)
            loaded_payload = _response_payload(loaded_response)

        self.assertEqual(initial_payload, {"data": {}})
        self.assertTrue(replace_payload["ok"])
        self.assertEqual(loaded_payload, {"data": user_settings_payload})

    async def test_user_settings_mutation_preserves_other_users(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_settings.json"
            initial_payload = {
                "version": 1,
                "users": {"42": {"web_chat": {"use_24_hour_time": True}}},
            }
            path.write_text(json.dumps(initial_payload), encoding="utf-8")
            server = AuthorityServer(cast(config.Name_Cache, cast(object, SimpleNamespace())))

            with patch.object(config, "USER_SETTINGS", path):
                response = await server._handle_user_settings_mutate(
                    cast(
                        web.Request,
                        cast(
                            object,
                            _FakeJsonRequest(
                                {
                                    "user_id": 99,
                                    "settings": {"web_chat": {"use_24_hour_time": False}},
                                }
                            ),
                        ),
                    )
                )

            saved_payload = json.loads(path.read_text(encoding="utf-8"))

        response_payload = _response_payload(response)
        self.assertTrue(response_payload["ok"])
        self.assertEqual(saved_payload["users"]["42"], initial_payload["users"]["42"])
        self.assertEqual(saved_payload["users"]["99"], {"web_chat": {"use_24_hour_time": False}})


if __name__ == "__main__":
    unittest.main()
