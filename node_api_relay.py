"""Relay text-to-speech transport contracts and remote forwarding."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import hikari
import requests
from pydantic import BaseModel, field_validator
from pydantic.config import ConfigDict

import config
from _async_utils import run_blocking
from _authority import AuthorityResource, read_json_object
from apps._node_api import JsonValue
from node_auth import NodeAccessGrant, NodeApiScope, issue_node_token

log = logging.getLogger(__name__)
_RELAY_TTS_FORWARD_TTL_SECONDS = 60

class RelayTTSQueue(Protocol):
    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]: ...


class NodeRelayTTSRequest(BaseModel):
    guild_id: int
    channel_id: int
    message_id: int
    text: str
    user_id: int | None = None
    source_app: str
    player_name: str

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("guild_id", "channel_id", "message_id", "user_id", mode="before")
    @classmethod
    def _validate_optional_snowflake_int(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError("relay TTS snowflake fields must not be booleans.")
        if not isinstance(value, (int, str, hikari.Snowflake)):
            raise TypeError("relay TTS snowflake fields must be Discord snowflakes.")
        return int(hikari.Snowflake(value))

    @field_validator("text", "source_app", "player_name")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("relay TTS fields must not be empty.")
        return text


@dataclass(frozen=True, slots=True)
class NodeRelayTTSResult:
    queued: bool
    spoken: str | None = None
    queue_size: int | None = None
    reason: str | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "queued": self.queued,
            "spoken": self.spoken,
            "queue_size": self.queue_size,
            "reason": self.reason,
        }


class RemoteRelayTTSForwarder:
    _BOT_CONFIGURATION_PATH = Path("configuration.json")
    _TARGET_PROFILE = config.BotProfileName.YUKI

    def __init__(self) -> None:
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH

    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None:
        del guild_id
        return None

    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]:
        return await self.queue_discord_relay_message(
            guild_id,
            channel_id,
            message_id,
            text,
            user_id=user_id,
            source_app=config.MOD_WEB_SERVER.node_name,
            player_name=str(user_id) if user_id is not None else "unlinked",
        )

    async def queue_discord_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
        source_app: str,
        player_name: str,
    ) -> tuple[str, int]:
        secret = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise RuntimeError("Node relay TTS token secret is not configured.")

        target_snapshot = self._resolve_target_snapshot()
        mod_web = target_snapshot.features.mod_web
        if mod_web is None:
            raise RuntimeError("Target voice node does not expose a node API endpoint.")

        payload = NodeRelayTTSRequest(
            guild_id=int(hikari.Snowflake(guild_id)),
            channel_id=int(hikari.Snowflake(channel_id)),
            message_id=int(hikari.Snowflake(message_id)),
            text=text,
            user_id=int(hikari.Snowflake(user_id)) if user_id is not None else None,
            source_app=source_app,
            player_name=player_name,
        )
        token = issue_node_token(
            secret=secret,
            grant=NodeAccessGrant(
                subject=f"relay-tts:{config.MOD_WEB_SERVER.node_name}",
                node=mod_web.node_name,
                app=None,
                scopes=frozenset({NodeApiScope.RELAY_TTS}),
                expires_at=int(time.time()) + _RELAY_TTS_FORWARD_TTL_SECONDS,
            ),
        )
        response = await run_blocking(
            self._post_relay_tts,
            mod_web.node_api_base_url.rstrip("/") + "/relay/tts",
            token,
            cast(Mapping[str, JsonValue], payload.model_dump(mode="json")),
        )
        queued = bool(response.get("queued"))
        if not queued:
            reason = str(response.get("reason") or "Relay TTS request was not queued.")
            raise RuntimeError(reason)
        spoken = response.get("spoken")
        queue_size = response.get("queue_size")
        if not isinstance(spoken, str) or not isinstance(queue_size, int):
            raise RuntimeError("Relay TTS response from target node was invalid.")
        return spoken, queue_size

    def _resolve_target_snapshot(self) -> config.BotMetadataSnapshot:
        registry = self._load_known_bot_registry()
        for snapshot in registry.values():
            if snapshot.profile.bot_profile is self._TARGET_PROFILE:
                return snapshot
        raise RuntimeError(f"No known bot metadata entry exists for target profile {self._TARGET_PROFILE.value!r}.")

    def _load_known_bot_registry(self) -> dict[str, config.BotMetadataSnapshot]:
        snapshots: dict[str, config.BotMetadataSnapshot] = {}
        if self._bot_configuration_path.exists():
            try:
                bot_config = config.load_bot_configuration(self._bot_configuration_path)
            except (OSError, ValueError) as xcp:
                log.warning("Relay TTS target lookup failed to read %s: %s", self._bot_configuration_path, xcp)
            else:
                snapshots.update(bot_config.known_bots)

        if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.REMOTE:
            cache_path = config.authority_cache_path(AuthorityResource.BOTS)
            if cache_path.exists():
                try:
                    raw = read_json_object(cache_path)
                    snapshots.update(
                        {
                            bot_id: config.BotMetadataSnapshot.model_validate(snapshot)
                            for bot_id, snapshot in raw.items()
                        }
                    )
                except (OSError, ValueError, TypeError) as xcp:
                    log.warning("Relay TTS target lookup failed to read bot registry cache %s: %s", cache_path, xcp)

        return snapshots

    @staticmethod
    def _post_relay_tts(url: str, token: str, payload: Mapping[str, JsonValue]) -> dict[str, object]:
        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
        except requests.RequestException as xcp:
            raise RuntimeError(f"Relay TTS request failed: {type(xcp).__name__}: {xcp}") from xcp
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else response.text
            raise RuntimeError(f"Relay TTS request rejected by target node: {detail}")
        if not isinstance(body, dict):
            raise RuntimeError("Relay TTS response from target node was not a JSON object.")
        return body
