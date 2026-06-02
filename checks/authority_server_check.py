from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

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
                server = AuthorityServer(cast(config.Name_Cache, SimpleNamespace()))
                response = await server._handle_bots_sync(
                    _FakeJsonRequest({"data": snapshot.model_dump(mode="json")})  # type: ignore[arg-type]
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
                server = AuthorityServer(cast(config.Name_Cache, SimpleNamespace()))
                response = await server._handle_bots(SimpleNamespace())  # type: ignore[arg-type]

            payload = _response_payload(response)
            raw_data = payload.get("data")

        self.assertIsInstance(raw_data, dict)
        assert isinstance(raw_data, dict)
        snapshot_payload = raw_data["1350601198637551659"]
        snapshot = config.BotMetadataSnapshot.model_validate(snapshot_payload)

        self.assertEqual(snapshot.profile.label, "Erin")
        self.assertEqual(snapshot.features.oauth, config.PersistedOAuthLinks(guild=None))


if __name__ == "__main__":
    unittest.main()
