from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, cast
from urllib.parse import quote, unquote, urlparse

import emoji
import hikari
import hikariwave
import lightbulb
import requests

import config
from _security import Access_Control

log = logging.getLogger(__name__)

group_voice = lightbulb.Group("voice", "Voice commands and TTS")  # type: ignore

VOICE_USERS_FILE = Path("voice_users.json")
VOICE_CORRECTIONS_FILE = Path("voice_corrections.json")
DISCORD_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
EMOJI_TAG_RE = re.compile(r":[a-z0-9_+\-]+:", re.IGNORECASE)
TOKEN_RE = re.compile(r":[a-z0-9_+\-]+:|[^\s]+", re.IGNORECASE)
SUBSTITUTION_TOKEN_RE = re.compile(r"^([^\w]*)([\w'-]+)([^\w]*)$")
VOICE_LINE_RE = re.compile(r"^\s*\d+\s+(\S+)\s+")
VARIANT_FILE_RE = re.compile(r"!v/(.+?)(?:\s{2,}|\s*$)")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
HUGGINGFACE_HOSTS = frozenset({"huggingface.co", "www.huggingface.co"})


def _patch_hikariwave_cache_state_update_bug():
    """Work around hikari-wave 0.7.0a1 cache bug calling __member_update without new_channel_id."""
    try:
        from hikariwave.impl import cache as hw_cache
    except Exception as xcp:
        log.warning(f"TTS workaround skipped: couldn't import hikariwave cache module: {xcp}")
        return

    state_name = "_Cache__state_update"
    member_name = "_Cache__member_update"
    state_update = getattr(hw_cache.Cache, state_name, None)
    member_update = getattr(hw_cache.Cache, member_name, None)
    if not callable(state_update) or not callable(member_update):
        return

    if "new_channel_id" not in inspect.signature(member_update).parameters:
        return
    if getattr(state_update, "__name__", "") == "_patched_state_update":
        return

    async def _patched_state_update(self, event: hikari.VoiceStateUpdateEvent) -> None:
        state: hikari.VoiceState = event.state
        member: hikari.Member | None = state.member

        me = self._client._bot.get_me()
        if (me and state.user_id == me.id) or not member:
            return

        old_channel_id: hikari.Snowflake | None = self._members.get(member.id)
        new_channel_id: hikari.Snowflake | None = state.channel_id

        if not old_channel_id and new_channel_id:
            await self._Cache__member_join(member, state, new_channel_id)
        elif old_channel_id and new_channel_id and old_channel_id != new_channel_id:
            await self._Cache__member_move(member, old_channel_id, new_channel_id)
        elif old_channel_id and not new_channel_id:
            await self._Cache__member_leave(member, old_channel_id)
        elif old_channel_id and new_channel_id and old_channel_id == new_channel_id:
            await self._Cache__member_update(member, state, new_channel_id)

    setattr(hw_cache.Cache, state_name, _patched_state_update)
    log.warning("Applied hikari-wave cache state-update workaround")


def _patch_hikariwave_udp_discovery_timeout():
    """Retry UDP IP discovery to avoid transient 3s timeout failures during voice connect."""
    try:
        from hikariwave.networking.server import VoiceServer
    except Exception as xcp:
        log.warning(f"TTS workaround skipped: couldn't import hikariwave voice server module: {xcp}")
        return

    discover_name = "_discover_ip"
    discover_ip_obj = getattr(VoiceServer, discover_name, None)
    if not callable(discover_ip_obj):
        return
    if getattr(discover_ip_obj, "__name__", "") == "_patched_discover_ip":
        return
    discover_ip = cast(Callable[[object], Awaitable[tuple[str, int]]], discover_ip_obj)

    async def _patched_discover_ip(self):
        last_timeout: asyncio.TimeoutError | None = None
        for attempt in range(1, 4):
            try:
                return await discover_ip(self)
            except asyncio.TimeoutError as xcp:
                last_timeout = xcp
                log.warning(
                    f"TTS voice UDP discovery timeout attempt={attempt}/3 "
                    f"ip={getattr(self, '_ip', None)!r} port={getattr(self, '_port', None)!r}"
                )
                udp = getattr(self, "_udp", None)
                if udp:
                    with contextlib.suppress(Exception):
                        udp.close()
                    setattr(self, "_udp", None)
                if attempt < 3:
                    await asyncio.sleep(0.25 * attempt)

        if last_timeout:
            raise last_timeout
        raise asyncio.TimeoutError("Voice UDP discovery timed out.")

    setattr(VoiceServer, discover_name, _patched_discover_ip)
    log.warning("Applied hikari-wave UDP discovery timeout workaround")


_patch_hikariwave_cache_state_update_bug()
_patch_hikariwave_udp_discovery_timeout()


@dataclass(slots=True, frozen=True)
class VoiceJob:
    guild_id: hikari.Snowflake
    message_id: hikari.Snowflake
    text: str
    voice: str
    variant: str | None


@dataclass(slots=True, frozen=True)
class HFRepoRef:
    repo_id: str
    revision: str
    onnx_file: str | None = None


@dataclass(slots=True)
class UserVoiceSettings:
    enabled: bool = False
    voice: str | None = None
    variant: str | None = None
    substitutions: dict[str, str] = field(default_factory=dict)


class VoiceTTSService:
    _MAX_SPOKEN_CHARS = 550
    _LOG_PREVIEW_CHARS = 120
    _VARIANT_CLEAR_VALUES = frozenset({"none", "off", "clear", "default"})
    _VOICE_CONNECT_TIMEOUT_SECONDS = 20.0
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 40
    _MAX_SUBSTITUTION_VALUE_CHARS = 120

    def __init__(self, bot: hikari.GatewayBot):
        self.bot = bot
        self.voice_channel = config.VOICE_CHANNEL
        self.tts_channel = config.TTS_CHANNEL

        self._voice_client = hikariwave.VoiceClient(bot)
        self._queue: asyncio.Queue[VoiceJob] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._engine_kind, self._engine = self._resolve_local_tts_engine()
        self._piper_data_dir = config.TTS_PIPER_DATA_DIR
        self._piper_config_path = config.TTS_PIPER_CONFIG
        self._users_path = VOICE_USERS_FILE
        self._corrections_path = VOICE_CORRECTIONS_FILE
        self._user_settings: dict[int, UserVoiceSettings] = {}
        self.voice = config.TTS_VOICE
        if self._engine_kind == "piper":
            self.voice = self._initial_piper_voice()
        self.variant = self._normalise_variant(config.TTS_VARIANT)
        self._available_voices: list[str] = []
        self._available_variants: list[str] = []
        self._piper_config_cache: dict[str, tuple[int, dict[str, object] | None]] = {}
        self._common_text_corrections = self._load_common_text_corrections()
        self._load_user_settings()

        self._enabled = bool(self.voice_channel and self.tts_channel)
        if not self._enabled:
            log.warning("Voice TTS disabled: VOICE_CHANNEL and TTS_CHANNEL must be configured")
        elif not self._engine:
            requested = config.TTS_ENGINE or "auto"
            log.warning(
                f"Voice TTS disabled: local TTS executable not found for {requested=!r} (espeak-ng/espeak/piper)"
            )
        elif self._engine_kind == "piper" and not self._piper_model_path(self.voice):
            model_hint = f"voice={self.voice!r} data_dir={self._piper_data_dir!r}"
            log.warning(
                "Voice TTS Piper model could not be resolved; "
                f"set TTS_PIPER_MODEL/TTS_VOICE and TTS_PIPER_DATA_DIR if needed ({model_hint})"
            )

    async def setup(self):
        if self._worker_task:
            return
        self._worker_task = asyncio.create_task(self._worker_loop(), name="voice-tts-worker")
        await self._validate_voice()
        await self._validate_variant()
        if self._enabled:
            users = self.listening_users()
            target_users = ",".join(str(uid) for uid in users) if users else "none"
            log.info(
                f"Voice TTS ready: {self.tts_channel=} {self.voice_channel=} target_users={target_users} "
                f"voice={self.voice} variant={self.variant or 'none'} engine={self._engine_display()}"
            )

    async def close(self):
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        await self._voice_client.close()

    def get_connection(self, guild_id: hikari.Snowflakeish) -> hikariwave.VoiceConnection | None:
        return self._voice_client.get_connection(guild_id=guild_id)

    def active_voice_connection(self) -> hikariwave.VoiceConnection | None:
        if not self.voice_channel:
            return None
        return self._voice_client.get_connection(channel_id=hikari.Snowflake(self.voice_channel))

    def _target_voice_channel_id(self) -> hikari.Snowflake | None:
        if not self.voice_channel:
            return None
        return hikari.Snowflake(self.voice_channel)

    def _target_voice_listener_count(self, guild_id: hikari.Snowflakeish) -> int:
        channel_id = self._target_voice_channel_id()
        if channel_id is None:
            return 0

        me = self.bot.get_me()
        voice_states = self.bot.cache.get_voice_states_view_for_channel(guild_id, channel_id)
        if not me:
            return len(voice_states)
        return sum(1 for user_id in voice_states if user_id != me.id)

    def active_voice_guild_id(self) -> hikari.Snowflake | None:
        if conn := self.active_voice_connection():
            return hikari.Snowflake(conn.guild_id)
        return None

    @staticmethod
    def _connection_state_name(connection: hikariwave.VoiceConnection) -> str:
        state = getattr(connection, "_state", None)
        if state is None:
            return "unknown"
        name = getattr(state, "name", None)
        return str(name) if name else str(state)

    @classmethod
    def _connection_is_ready(cls, connection: hikariwave.VoiceConnection) -> bool:
        state_name = cls._connection_state_name(connection).upper()
        if state_name == "CONNECTED":
            return True
        if state_name in {"CONNECTING", "DISCONNECTING", "DISCONNECTED"}:
            return False
        ready = getattr(connection, "_ready", None)
        return bool(ready.is_set()) if isinstance(ready, asyncio.Event) else False

    async def _reset_voice_connection(
        self,
        guild_id: hikari.Snowflake,
        target_channel: hikari.Snowflake,
        *,
        verify: bool = False,
    ) -> bool:
        ok = True

        try:
            await self._voice_client.disconnect(guild_id=guild_id)
        except Exception as xcp:
            ok = False
            log.warning(f"TTS voice reset disconnect-by-guild failed {guild_id=}: {type(xcp).__name__}: {xcp}")

        try:
            await self._voice_client.disconnect(channel_id=target_channel)
        except Exception as xcp:
            ok = False
            log.warning(
                f"TTS voice reset disconnect-by-channel failed channel={target_channel}: {type(xcp).__name__}: {xcp}"
            )

        try:
            await self.bot.update_voice_state(guild_id, None)
        except Exception as xcp:
            ok = False
            log.warning(f"TTS voice reset update_voice_state failed {guild_id=}: {type(xcp).__name__}: {xcp}")

        if not verify:
            return ok

        me = self.bot.get_me()
        cached_state = self.bot.cache.get_voice_state(guild_id, me.id) if me else None
        has_active_connection = self._voice_client.get_connection(guild_id=guild_id) is not None
        still_in_target = bool(cached_state and cached_state.channel_id == target_channel)
        return ok and not has_active_connection and not still_in_target

    async def on_voice_state_update(self, event: hikari.VoiceStateUpdateEvent):
        if not self.voice_channel:
            return

        me = self.bot.get_me()
        if me and event.state.user_id == me.id:
            return

        target_channel = hikari.Snowflake(self.voice_channel)
        old_channel_id = event.old_state.channel_id if event.old_state else None
        new_channel_id = event.state.channel_id

        # Trigger only when someone leaves/moves away from the configured channel.
        if old_channel_id != target_channel or new_channel_id == target_channel:
            return

        connection = self.active_voice_connection()
        if not connection or connection.channel_id != target_channel:
            return

        voice_states = self.bot.cache.get_voice_states_view_for_channel(event.guild_id, target_channel)
        occupants = [uid for uid in voice_states if not me or uid != me.id]
        if new_channel_id != target_channel:
            occupants = [uid for uid in occupants if uid != event.state.user_id]
        if occupants:
            return

        disconnected = await self._reset_voice_connection(
            hikari.Snowflake(connection.guild_id),
            target_channel,
            verify=True,
        )
        if disconnected:
            log.info(
                f"TTS auto-disconnect channel={target_channel} reason=channel_empty "
                f"trigger_user={event.state.user_id} guild={connection.guild_id}"
            )
        else:
            log.warning(
                f"TTS auto-disconnect may have failed channel={target_channel} reason=channel_empty "
                f"trigger_user={event.state.user_id} guild={connection.guild_id}"
            )

    def listening_users(self) -> list[int]:
        return sorted(uid for uid, settings in self._user_settings.items() if settings.enabled)

    def is_user_listening(self, user_id: hikari.Snowflakeish) -> bool:
        return bool(self._user_settings.get(int(user_id), UserVoiceSettings()).enabled)

    def set_user_listening(self, user_id: hikari.Snowflakeish, enabled: bool) -> bool:
        uid = int(user_id)
        settings = self._user_settings.get(uid, UserVoiceSettings())
        settings.enabled = enabled
        if enabled and settings.voice is None:
            settings.voice = self.voice
        if enabled and settings.variant is None and self.variant is not None:
            settings.variant = self.variant
        self._user_settings[uid] = settings
        self._save_user_settings()
        return settings.enabled

    def user_voice_variant(self, user_id: hikari.Snowflakeish) -> tuple[str, str | None]:
        settings = self._user_settings.get(int(user_id))
        if not settings:
            return self.voice, self.variant

        selected_voice = settings.voice or self.voice
        if self._engine_kind == "piper" and not self._piper_model_path(selected_voice):
            selected_voice = self.voice

        return selected_voice, settings.variant

    def user_voice_variant_for_say(self, user_id: hikari.Snowflakeish) -> tuple[str, str | None]:
        settings = self._user_settings.get(int(user_id))
        if not settings or not settings.enabled:
            return self.voice, self.variant

        selected_voice = settings.voice or self.voice
        if self._engine_kind == "piper" and not self._piper_model_path(selected_voice):
            selected_voice = self.voice
        return selected_voice, settings.variant

    def user_text_substitutions(self, user_id: hikari.Snowflakeish) -> dict[str, str]:
        settings = self._user_settings.get(int(user_id))
        if not settings or not settings.substitutions:
            return {}
        return dict(sorted(settings.substitutions.items()))

    def base_text_substitutions(self) -> dict[str, str]:
        if not self._common_text_corrections:
            return {}
        return dict(sorted(self._common_text_corrections.items()))

    @staticmethod
    def _match_case_insensitive(options: list[str], requested: str) -> str | None:
        requested_lower = requested.lower()
        return next((option for option in options if option.lower() == requested_lower), None)

    async def _resolve_requested_voice(self, voice: str) -> str:
        requested_voice = voice.strip()
        if not requested_voice:
            raise ValueError("voice must not be empty")

        voices = await self.available_voices(force_refresh=True)
        if not voices:
            return requested_voice

        if match := self._match_case_insensitive(voices, requested_voice):
            return match

        if self._engine_kind == "piper" and self._piper_model_path(requested_voice):
            return requested_voice

        raise LookupError(f"Unknown voice: {requested_voice}")

    async def _resolve_requested_variant(self, voice: str, variant: str) -> str | None:
        requested_variant = self._normalise_variant(variant, allow_empty=False)
        if requested_variant is None:
            return None

        variants = await self._available_variants_for_voice(voice, force_refresh=True)
        if not variants:
            return requested_variant

        if match := self._match_case_insensitive(variants, requested_variant):
            return match

        raise LookupError(f"Unknown variant: {requested_variant}")

    async def _revalidate_variant_for_voice(self, voice: str, variant: str | None) -> str | None:
        if variant is None:
            return None

        variants = await self._available_variants_for_voice(voice, force_refresh=True)
        if variants:
            return self._match_case_insensitive(variants, variant)

        if self._engine_kind == "piper":
            return None
        return variant

    async def _resolve_voice_variant_selection(
        self,
        current_voice: str,
        current_variant: str | None,
        *,
        voice: str | None = None,
        variant: str | None = None,
    ) -> tuple[str, str | None]:
        next_voice = current_voice
        next_variant = current_variant
        voice_changed = False

        if voice is not None:
            next_voice = await self._resolve_requested_voice(voice)
            voice_changed = next_voice.lower() != current_voice.lower()

        if variant is not None:
            next_variant = await self._resolve_requested_variant(next_voice, variant)
        elif voice_changed and next_variant is not None:
            next_variant = await self._revalidate_variant_for_voice(next_voice, next_variant)

        return next_voice, next_variant

    def set_user_text_substitution(
        self, user_id: hikari.Snowflakeish, source: str, target: str
    ) -> tuple[str, str, bool]:
        uid = int(user_id)
        key = self._normalise_substitution_key(source)
        value = self._normalise_substitution_value(target)

        settings = self._user_settings.get(uid, UserVoiceSettings())
        substitutions = dict(settings.substitutions)
        existed = key in substitutions
        if not existed and len(substitutions) >= self._MAX_SUBSTITUTIONS_PER_USER:
            raise ValueError(f"You can store up to {self._MAX_SUBSTITUTIONS_PER_USER} substitutions.")

        substitutions[key] = value
        settings.substitutions = dict(sorted(substitutions.items()))
        self._user_settings[uid] = settings
        self._save_user_settings()
        return key, value, existed

    def remove_user_text_substitution(self, user_id: hikari.Snowflakeish, source: str) -> tuple[str, bool]:
        uid = int(user_id)
        key = self._normalise_substitution_key(source)
        settings = self._user_settings.get(uid)
        if not settings or key not in settings.substitutions:
            return key, False

        substitutions = dict(settings.substitutions)
        del substitutions[key]
        settings.substitutions = substitutions
        self._user_settings[uid] = settings
        self._save_user_settings()
        return key, True

    async def set_user_voice_variant(
        self,
        user_id: hikari.Snowflakeish,
        voice: str | None = None,
        variant: str | None = None,
    ) -> tuple[str, str | None]:
        uid = int(user_id)
        current_voice, current_variant = self.user_voice_variant(uid)
        next_voice, next_variant = await self._resolve_voice_variant_selection(
            current_voice,
            current_variant,
            voice=voice,
            variant=variant,
        )

        settings = self._user_settings.get(uid, UserVoiceSettings())
        settings.voice = next_voice
        settings.variant = next_variant
        self._user_settings[uid] = settings
        self._save_user_settings()
        return next_voice, next_variant

    async def available_variants_for_voice(self, voice: str, force_refresh: bool = False) -> list[str]:
        return await self._available_variants_for_voice(voice, force_refresh=force_refresh)

    def _load_user_settings(self):
        self._user_settings.clear()
        if not self._users_path.exists():
            return

        try:
            raw = json.loads(self._users_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(f"TTS user settings read failed path={self._users_path!s}: {type(xcp).__name__}: {xcp}")
            return

        users_raw = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(users_raw, dict):
            return

        for user_id, values in users_raw.items():
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                continue

            if not isinstance(values, dict):
                continue

            enabled = bool(values.get("enabled"))
            voice = values.get("voice")
            variant = values.get("variant")
            substitutions_raw = values.get("substitutions")

            selected_voice = voice.strip() if isinstance(voice, str) and voice.strip() else None
            selected_variant: str | None = None
            if isinstance(variant, str):
                try:
                    selected_variant = self._normalise_variant(variant)
                except ValueError:
                    selected_variant = None

            substitutions: dict[str, str] = {}
            if isinstance(substitutions_raw, dict):
                for source, target in substitutions_raw.items():
                    if not isinstance(source, str) or not isinstance(target, str):
                        continue
                    try:
                        key = self._normalise_substitution_key(source)
                        value = self._normalise_substitution_value(target)
                    except ValueError:
                        continue
                    substitutions[key] = value

            self._user_settings[uid] = UserVoiceSettings(
                enabled=enabled,
                voice=selected_voice,
                variant=selected_variant,
                substitutions=dict(sorted(substitutions.items())),
            )

    def _save_user_settings(self):
        users: dict[str, dict[str, object]] = {}
        for uid, settings in self._user_settings.items():
            users[str(uid)] = {
                "enabled": settings.enabled,
                "voice": settings.voice,
                "variant": settings.variant,
                "substitutions": dict(sorted(settings.substitutions.items())),
            }

        payload = {"users": users}
        try:
            self._users_path.write_text(json.dumps(payload, indent=2), config.STR_ENCODE)
        except OSError as xcp:
            log.warning(f"TTS user settings write failed path={self._users_path!s}: {type(xcp).__name__}: {xcp}")

    def _load_common_text_corrections(self) -> dict[str, str]:
        if not self._corrections_path.exists():
            log.warning(f"TTS correction file not found path={self._corrections_path!s}; typo correction disabled")
            return {}

        try:
            raw = json.loads(self._corrections_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(f"TTS correction file read failed path={self._corrections_path!s}: {type(xcp).__name__}: {xcp}")
            return {}

        if not isinstance(raw, dict):
            log.warning(
                f"TTS correction file invalid path={self._corrections_path!s}: expected a JSON object map of key->value"
            )
            return {}

        corrections: dict[str, str] = {}
        invalid = 0
        for source, target in raw.items():
            if not isinstance(source, str) or not isinstance(target, str):
                invalid += 1
                continue
            try:
                key = self._normalise_substitution_key(source)
                value = self._normalise_substitution_value(target)
            except ValueError:
                invalid += 1
                continue
            corrections[key] = value

        if invalid:
            log.warning(
                f"TTS correction file loaded with skipped entries path={self._corrections_path!s}: "
                f"loaded={len(corrections)} skipped={invalid}"
            )
        else:
            log.info(f"TTS correction file loaded path={self._corrections_path!s}: loaded={len(corrections)}")

        return corrections

    async def available_voices(self, force_refresh: bool = False) -> list[str]:
        if self._available_voices and not force_refresh:
            return self._available_voices
        if not self._engine:
            self._available_voices = []
            return self._available_voices

        if self._engine_kind == "piper":
            self._available_voices = self._piper_available_voices()
            return self._available_voices

        process = await asyncio.create_subprocess_exec(
            self._engine,
            "--voices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode != 0:
            error = err.decode(config.STR_ENCODE, "replace").strip() if err else "unknown error"
            log.warning(f"TTS voices lookup failed: code={process.returncode}; {error}")
            self._available_voices = []
            return self._available_voices

        raw = out.decode(config.STR_ENCODE, "replace")
        voices = sorted({m.group(1) for m in [VOICE_LINE_RE.match(line) for line in raw.splitlines()] if m})
        english = [v for v in voices if v.lower().startswith("en")]
        self._available_voices = english if english else voices
        return self._available_voices

    async def available_variants(self, force_refresh: bool = False) -> list[str]:
        if self._available_variants and not force_refresh:
            return self._available_variants
        if not self._engine:
            self._available_variants = []
            return self._available_variants

        if self._engine_kind == "piper":
            self._available_variants = self._piper_available_variants(self.voice)
            return self._available_variants

        process = await asyncio.create_subprocess_exec(
            self._engine,
            "--voices=variant",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode != 0:
            error = err.decode(config.STR_ENCODE, "replace").strip() if err else "unknown error"
            log.warning(f"TTS variants lookup failed: code={process.returncode}; {error}")
            self._available_variants = []
            return self._available_variants

        raw = out.decode(config.STR_ENCODE, "replace")
        variants = sorted({m.group(1).strip() for line in raw.splitlines() if (m := VARIANT_FILE_RE.search(line))})
        self._available_variants = variants
        return self._available_variants

    async def _available_variants_for_voice(self, voice: str, force_refresh: bool = False) -> list[str]:
        if self._engine_kind != "piper":
            return await self.available_variants(force_refresh=force_refresh)

        if voice == self.voice and self._available_variants and not force_refresh:
            return self._available_variants

        variants = self._piper_available_variants(voice)
        if voice == self.voice:
            self._available_variants = variants
        return variants

    async def set_voice(self, voice: str) -> str:
        selected, _ = await self.set_voice_variant(voice=voice)
        return selected

    async def set_variant(self, variant: str) -> str | None:
        _, selected = await self.set_voice_variant(variant=variant)
        return selected

    async def set_voice_variant(
        self,
        voice: str | None = None,
        variant: str | None = None,
    ) -> tuple[str, str | None]:
        next_voice, next_variant = await self._resolve_voice_variant_selection(
            self.voice,
            self.variant,
            voice=voice,
            variant=variant,
        )

        prev_voice = self.voice
        prev_variant = self.variant

        self.voice = next_voice
        self.variant = next_variant

        if prev_voice != self.voice:
            self._available_variants = []
            log.info(f"TTS voice update old={prev_voice} new={self.voice}")
        if prev_variant != self.variant:
            log.info(f"TTS variant update old={prev_variant or 'none'} new={self.variant or 'none'}")

        return self.voice, self.variant

    def _invalidate_piper_runtime_cache(self):
        self._available_voices = []
        self._available_variants = []
        self._piper_config_cache.clear()

    async def scan_piper_models_from_hf(self, url: str) -> tuple[HFRepoRef, list[str]]:
        if self._engine_kind != "piper":
            raise RuntimeError("Model add/remove commands are only available for Piper TTS.")

        repo_ref = self._hf_parse_repo_url(url)
        files = await asyncio.to_thread(self._hf_repo_files, repo_ref.repo_id, repo_ref.revision)
        candidates = await asyncio.to_thread(self._hf_find_piper_candidates, repo_ref.repo_id, repo_ref.revision, files)

        if repo_ref.onnx_file:
            selected = repo_ref.onnx_file.lower()
            match = next((path for path in candidates if path.lower() == selected), None)
            if not match:
                raise LookupError(
                    "The `.onnx` file in that URL is not Piper-compatible (missing/invalid `.onnx.json` Piper config)."
                )
            return repo_ref, [match]

        return repo_ref, candidates

    async def add_piper_model_from_hf(self, repo_ref: HFRepoRef, onnx_file: str) -> tuple[str, bool]:
        if self._engine_kind != "piper":
            raise RuntimeError("Model add/remove commands are only available for Piper TTS.")

        selected_file = onnx_file.strip()
        if not selected_file:
            raise ValueError("onnx_file must not be empty")

        model_url = self._hf_resolve_download_url(repo_ref.repo_id, repo_ref.revision, selected_file)
        config_url = self._hf_resolve_download_url(repo_ref.repo_id, repo_ref.revision, f"{selected_file}.json")

        target_dir = self._piper_custom_write_dir()
        target_model = target_dir / Path(selected_file).name
        target_config = Path(f"{target_model}.json")
        if target_model.exists():
            raise FileExistsError(f"Model `{target_model.stem}` already exists.")

        await asyncio.to_thread(self._download_file, model_url, target_model, False)
        config_downloaded = False
        try:
            config_downloaded = await asyncio.to_thread(self._download_file, config_url, target_config, True)
        except Exception as xcp:
            log.warning(f"TTS Piper model config download failed model={target_model.stem!r}: {xcp}")

        self._invalidate_piper_runtime_cache()
        return target_model.stem, config_downloaded

    async def delete_piper_model(self, model: str) -> str:
        if self._engine_kind != "piper":
            raise RuntimeError("Model add/remove commands are only available for Piper TTS.")

        requested = model.strip()
        if not requested:
            raise ValueError("model must not be empty")

        model_path = self._piper_custom_model_path(requested)
        if not model_path:
            raise LookupError(f"Unknown model: {requested}")

        config_path = Path(f"{model_path}.json")
        try:
            model_path.unlink()
        except OSError as xcp:
            raise RuntimeError(f"Failed to delete model file: {xcp}") from xcp

        if config_path.exists():
            with contextlib.suppress(OSError):
                config_path.unlink()

        was_active = self.voice.lower() == model_path.stem.lower()
        self._invalidate_piper_runtime_cache()

        if was_active:
            await self._validate_voice()
            await self._validate_variant()

        return model_path.stem

    def available_custom_voices(self) -> list[str]:
        return sorted({model.stem for model in self._piper_custom_models()})

    async def _validate_voice(self):
        voices = await self.available_voices(force_refresh=True)
        if not voices:
            return
        if any(self.voice.lower() == v.lower() for v in voices):
            self.voice = next(v for v in voices if self.voice.lower() == v.lower())
            return

        fallback = "en-us" if "en-us" in voices else voices[0]
        log.warning(f"TTS voice '{self.voice}' unavailable; using '{fallback}'")
        self.voice = fallback

    async def _validate_variant(self):
        if not self.variant:
            return

        variants = await self._available_variants_for_voice(self.voice, force_refresh=True)
        if not variants:
            return

        variant = self.variant
        if any(variant.lower() == v.lower() for v in variants):
            self.variant = next(v for v in variants if variant.lower() == v.lower())
            return

        log.warning(f"TTS variant '{self.variant}' unavailable; disabling variant")
        self.variant = None

    def _queue_preflight_reason(
        self,
        guild_id: hikari.Snowflakeish,
        *,
        require_enabled: bool = False,
        require_worker: bool = False,
    ) -> str | None:
        if require_enabled and not self._enabled:
            return "service_disabled"
        if not self._engine:
            return "no_local_tts_engine"
        if not self.voice_channel:
            return "voice_channel_unconfigured"
        if require_worker and (not self._worker_task or self._worker_task.done()):
            return "worker_not_running"
        if self._target_voice_listener_count(guild_id) == 0:
            return "voice_channel_empty"
        return None

    @staticmethod
    def _queue_preflight_error(reason: str) -> str:
        if reason == "no_local_tts_engine":
            return "TTS engine is unavailable on the bot host."
        if reason == "voice_channel_unconfigured":
            return "VOICE_CHANNEL is not configured."
        if reason == "worker_not_running":
            return "Voice TTS worker is not running."
        if reason == "voice_channel_empty":
            return "No one is in the configured voice channel."
        if reason == "service_disabled":
            return "Voice TTS is disabled."
        return "TTS is unavailable."

    async def on_message(self, event: hikari.GuildMessageCreateEvent):
        if event.channel_id != self.tts_channel:
            return

        raw = (event.content or "").strip()
        preview = self._preview(raw)
        base_log = (
            f"TTS message {event.message_id=} {event.guild_id=} {event.channel_id=} {event.author_id=} "
            f"attachments={len(event.message.attachments)} preview={preview!r}"
        )

        if not event.is_human:
            log.info(f"{base_log} said=no reason=not_human")
            return
        if not self.is_user_listening(event.author_id):
            log.info(f"{base_log} said=no reason=wrong_user")
            return
        if not event.guild_id:
            log.info(f"{base_log} said=no reason=no_guild")
            return
        if raw.startswith(config.CHAT_IGNORE):
            log.info(f"{base_log} said=no reason=chat_ignore_prefix")
            return
        if reason := self._queue_preflight_reason(event.guild_id, require_enabled=True):
            log.info(f"{base_log} said=no reason={reason}")
            return

        spoken = self._normalise_for_speech(raw, event)
        if not spoken:
            log.info(f"{base_log} said=no reason=empty_after_normalise")
            return

        selected_voice, selected_variant = self.user_voice_variant(event.author_id)
        voice_spec = self._voice_spec(selected_voice, selected_variant)
        self._queue.put_nowait(
            VoiceJob(
                hikari.Snowflake(event.guild_id),
                event.message_id,
                spoken,
                selected_voice,
                selected_variant,
            )
        )
        log.info(
            f"{base_log} said=queued reason=accepted queue_size={self._queue.qsize()} "
            f"voice={voice_spec} spoken={self._preview(spoken)!r}"
        )

    def queue_say(
        self,
        guild_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        user_id: hikari.Snowflakeish | None = None,
    ) -> tuple[str, int]:
        if reason := self._queue_preflight_reason(guild_id, require_worker=True):
            raise RuntimeError(self._queue_preflight_error(reason))

        spoken = self._normalise_for_speech(text, user_id=user_id)
        if not spoken:
            raise ValueError("No speakable text after normalisation.")

        selected_voice, selected_variant = (
            self.user_voice_variant_for_say(user_id) if user_id else (self.voice, self.variant)
        )

        guild = hikari.Snowflake(guild_id)
        message = hikari.Snowflake(message_id)
        voice_spec = self._voice_spec(selected_voice, selected_variant)
        self._queue.put_nowait(VoiceJob(guild, message, spoken, selected_voice, selected_variant))
        queue_size = self._queue.qsize()
        log.info(
            f"TTS command queued guild={guild} message_id={message} "
            f"queue_size={queue_size} voice={voice_spec} spoken={self._preview(spoken)!r}"
        )
        return spoken, queue_size

    async def _worker_loop(self):
        while True:
            job = await self._queue.get()
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(f"Voice TTS failed for {job.message_id=}")
            finally:
                self._queue.task_done()

    async def _process_job(self, job: VoiceJob):
        connection = await self._ensure_connection(job.guild_id)
        if not connection:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=no_voice_connection")
            return

        self._recover_stale_player_task(connection, job.message_id)

        audio = await self._synth_text(job.text, job.voice, job.variant)
        if not audio:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=tts_synth_empty")
            return

        source = hikariwave.BufferAudioSource(audio, name=f"tts-{job.message_id}")
        result = await connection.player.add_queue(source)
        if not result.success:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=player_rejected detail={result.reason}")
            return

        log.info(
            f"TTS job queued-to-player {job.message_id=} queue_len={connection.player.queue} "
            f"voice={self._voice_spec(job.voice, job.variant)}"
        )
        finished = await self._wait_for_completion(job, connection, source)
        if finished:
            log.info(f"TTS job completed {job.message_id=} said=yes")
        else:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=playback_timeout_or_reset")

    async def _ensure_connection(self, guild_id: hikari.Snowflake) -> hikariwave.VoiceConnection | None:
        if not self.voice_channel:
            return None

        target_channel = hikari.Snowflake(self.voice_channel)
        connection = self._voice_client.get_connection(guild_id=guild_id)
        listeners = self._target_voice_listener_count(guild_id)

        if connection and connection.channel_id == target_channel:
            if listeners == 0:
                log.info(f"TTS voice not ready {guild_id=} channel={target_channel} mode=disconnect_empty_channel")
                with contextlib.suppress(Exception):
                    await self._voice_client.disconnect(channel_id=target_channel)
                return None
            state_name = self._connection_state_name(connection)
            if not self._connection_is_ready(connection):
                log.warning(
                    f"TTS stale voice connection {guild_id=} channel={target_channel} mode=reset state={state_name}"
                )
                await self._reset_voice_connection(guild_id, target_channel)
                connection = None
            else:
                log.info(f"TTS voice ready {guild_id=} channel={target_channel} mode=reuse state={state_name}")
                return connection

        if listeners == 0:
            log.info(f"TTS voice skip connect {guild_id=} channel={target_channel} reason=channel_empty")
            return None

        if connection:
            try:
                log.info(
                    f"TTS moving voice {guild_id=} from={connection.channel_id} to={target_channel} "
                    f"mode=move timeout={self._VOICE_CONNECT_TIMEOUT_SECONDS:.1f}s"
                )
                moved = await asyncio.wait_for(
                    self._voice_client.move(target_channel, guild_id=guild_id, deaf=True),
                    timeout=self._VOICE_CONNECT_TIMEOUT_SECONDS,
                )
                if self._connection_is_ready(moved):
                    log.info(
                        f"TTS voice moved {guild_id=} channel={target_channel} "
                        f"state={self._connection_state_name(moved)}"
                    )
                    return moved
                log.warning(
                    f"TTS voice move returned unready connection {guild_id=} channel={target_channel} "
                    f"state={self._connection_state_name(moved)}; resetting"
                )
            except asyncio.TimeoutError:
                log.warning(
                    f"TTS move attempt timed out {guild_id=} channel={target_channel} "
                    f"timeout={self._VOICE_CONNECT_TIMEOUT_SECONDS:.1f}s; resetting"
                )
            except Exception as xcp:
                log.warning(f"TTS move attempt failed {guild_id=}: {type(xcp).__name__}: {xcp}")
            await self._reset_voice_connection(guild_id, target_channel)
            await asyncio.sleep(0.35)

        me = self.bot.get_me()
        if me and (state := self.bot.cache.get_voice_state(guild_id, me.id)):
            if state.channel_id:
                log.warning(f"TTS stale voice-state detected {guild_id=} cached_channel={state.channel_id}; resetting")
                with contextlib.suppress(Exception):
                    await self.bot.update_voice_state(guild_id, None)
                await asyncio.sleep(0.35)

        last_xcp: Exception | None = None
        for attempt in range(1, 4):
            try:
                log.info(
                    f"TTS connecting voice {guild_id=} channel={target_channel} attempt={attempt} "
                    f"timeout={self._VOICE_CONNECT_TIMEOUT_SECONDS:.1f}s"
                )
                connected = await asyncio.wait_for(
                    self._voice_client.connect(guild_id, target_channel, deaf=True),
                    timeout=self._VOICE_CONNECT_TIMEOUT_SECONDS,
                )
                if not self._connection_is_ready(connected):
                    raise RuntimeError(
                        f"Voice connection entered unexpected state={self._connection_state_name(connected)}"
                    )
                return connected
            except asyncio.TimeoutError as xcp:
                last_xcp = xcp
                log.warning(
                    f"TTS connect attempt timed out {guild_id=} attempt={attempt} "
                    f"timeout={self._VOICE_CONNECT_TIMEOUT_SECONDS:.1f}s"
                )
            except Exception as xcp:
                last_xcp = xcp
                log.warning(f"TTS connect attempt failed {guild_id=} attempt={attempt}: {type(xcp).__name__}: {xcp}")
            await self._reset_voice_connection(guild_id, target_channel)
            await asyncio.sleep(0.45)

        if last_xcp:
            log.error(f"TTS unable to establish voice connection {guild_id=}: {type(last_xcp).__name__}: {last_xcp}")
        return None

    async def _synth_text(self, text: str, voice: str, variant: str | None) -> bytes:
        if not self._engine:
            return b""

        if self._engine_kind == "piper":
            return await self._synth_text_piper(text, voice, variant)

        voice_spec = self._voice_spec(voice, variant)
        process = await asyncio.create_subprocess_exec(
            self._engine,
            "--stdout",
            "-v",
            voice_spec,
            "-s",
            "165",
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode != 0:
            error = err.decode(config.STR_ENCODE, "replace").strip() if err else "unknown error"
            log.warning(f"TTS synth failed voice={voice_spec}: code={process.returncode}; {error}")
            return b""
        return out

    async def _synth_text_piper(self, text: str, voice: str, variant: str | None) -> bytes:
        if not self._engine:
            return b""

        speaker_id = self._piper_speaker_id(voice, variant)
        model_path = self._piper_model_path(voice)
        model_arg = str(model_path) if model_path else voice
        command = [self._engine, "--model", model_arg, "--output_file", "-"]
        if self._piper_config_path:
            command.extend(["--config", self._piper_config_path])
        elif not model_path:
            for data_dir in self._piper_model_search_dirs():
                command.extend(["--data-dir", str(data_dir)])
        if speaker_id is not None:
            command.extend(["--speaker", str(speaker_id)])

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = (text.strip() + "\n").encode(config.STR_ENCODE, "replace")
        out, err = await process.communicate(payload)
        if process.returncode != 0:
            error = err.decode(config.STR_ENCODE, "replace").strip() if err else "unknown error"
            log.warning(
                f"TTS synth failed voice={self._voice_spec(voice, variant)} speaker={speaker_id} "
                f"engine=piper code={process.returncode}; {error}"
            )
            return b""
        return out

    async def _wait_for_completion(
        self,
        job: VoiceJob,
        connection: hikariwave.VoiceConnection,
        source: hikariwave.AudioSource,
    ) -> bool:
        timeout = self._playback_timeout_seconds(job.text)

        def pred(e):
            return e.guild_id == job.guild_id and e.audio is source

        try:
            await self.bot.wait_for(hikariwave.AudioBeginEvent, 5.0, pred)
            log.info(f"TTS audio begin {job.message_id=} timeout={timeout:.1f}s")
        except asyncio.TimeoutError:
            log.warning(f"TTS audio begin timeout {job.message_id=} continuing wait_for_end")

        try:
            await self.bot.wait_for(hikariwave.AudioEndEvent, timeout, pred)
            return True
        except asyncio.TimeoutError:
            state = getattr(connection.player.state, "name", str(connection.player.state))
            log.warning(
                f"TTS audio end timeout {job.message_id=} state={state} elapsed={connection.player.elapsed:.2f}s "
                f"queue_len={len(connection.player.queue)} timeout={timeout:.1f}s"
            )
            with contextlib.suppress(Exception):
                await connection.player.stop()
            with contextlib.suppress(Exception):
                await self._voice_client.disconnect(guild_id=job.guild_id)
            await asyncio.sleep(0.35)
            return False

    def _recover_stale_player_task(self, connection: hikariwave.VoiceConnection, message_id: hikari.Snowflake):
        player = connection.player
        task = getattr(player, "_player_task", None)
        if not task:
            return
        if not task.done():
            return

        cancelled = task.cancelled()
        error: Exception | None = None
        if not cancelled:
            with contextlib.suppress(Exception):
                error = task.exception()

        setattr(player, "_player_task", None)
        if error:
            log.warning(
                f"TTS recovered stale player task {message_id=} cancelled={cancelled} error={type(error).__name__}: {error}"
            )
        else:
            log.warning(f"TTS recovered stale player task {message_id=} cancelled={cancelled}")

    def _normalise_for_speech(
        self,
        content: str,
        event: hikari.GuildMessageCreateEvent | None = None,
        user_id: hikari.Snowflakeish | None = None,
    ) -> str:
        text = content.strip()
        if not text:
            return ""

        substitutions: dict[str, str] = {}
        source_user: int | None = None
        if event is not None:
            source_user = int(event.author_id)
        elif user_id is not None:
            source_user = int(user_id)
        if source_user is not None:
            settings = self._user_settings.get(source_user)
            if settings and settings.substitutions:
                substitutions = settings.substitutions

        if event:
            text = self._replace_mentions_with_names(text, event)
        else:
            text = USER_MENTION_RE.sub(" user ", text)
            text = CHANNEL_MENTION_RE.sub(" channel ", text)
        text = text.replace("-#", " ")
        text = URL_RE.sub(" ", text)
        text = DISCORD_CUSTOM_EMOJI_RE.sub(lambda m: f":{m.group(1)}:", text)
        text = emoji.demojize(text, language="en")

        spoken = []
        repeat_tag: str | None = None
        repeat_count = 0

        def flush_repeat():
            nonlocal repeat_tag
            nonlocal repeat_count
            if not repeat_tag:
                return
            label = self._emoji_tag_to_words(repeat_tag)
            if repeat_count > 1:
                spoken.append(f"{label} x{repeat_count}")
            else:
                spoken.append(label)
            repeat_tag = None
            repeat_count = 0

        for token in TOKEN_RE.findall(text):
            if EMOJI_TAG_RE.fullmatch(token):
                token = token.lower()
                if token == repeat_tag:
                    repeat_count += 1
                else:
                    flush_repeat()
                    repeat_tag = token
                    repeat_count = 1
                continue

            flush_repeat()
            if substitutions:
                token = self._apply_substitution_token(token, substitutions)
            token = self._apply_common_typo_correction(token)
            spoken.append(token)

        flush_repeat()

        text = " ".join(spoken)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""

        if len(text) > self._MAX_SPOKEN_CHARS:
            text = text[: self._MAX_SPOKEN_CHARS].rstrip()
        return text

    @staticmethod
    def _apply_substitution_token(token: str, substitutions: dict[str, str]) -> str:
        match = SUBSTITUTION_TOKEN_RE.fullmatch(token)
        if not match:
            return token

        lead, core, tail = match.groups()
        replacement = substitutions.get(core.lower())
        if replacement is None:
            return token
        return f"{lead}{replacement}{tail}"

    def _apply_common_typo_correction(self, token: str) -> str:
        match = SUBSTITUTION_TOKEN_RE.fullmatch(token)
        if not match:
            return token

        lead, core, tail = match.groups()
        replacement = self._common_text_corrections.get(core.lower())
        if replacement is None:
            return token
        return f"{lead}{self._match_token_case(replacement, core)}{tail}"

    @staticmethod
    def _match_token_case(replacement: str, original: str) -> str:
        if not original:
            return replacement
        if original.isupper():
            return replacement.upper()
        if len(original) > 1 and original[0].isupper() and original[1:].islower():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    def _replace_mentions_with_names(self, text: str, event: hikari.GuildMessageCreateEvent) -> str:
        message = event.message
        member_mentions = message.get_member_mentions()
        user_mentions = message.user_mentions
        channel_mentions = message.channel_mentions

        def user_name(match: re.Match[str]) -> str:
            user_id = hikari.Snowflake(int(match.group(1)))
            name: str | None = None

            if member_mentions is not hikari.UNDEFINED and (member := member_mentions.get(user_id)):
                name = member.display_name
            elif user_mentions is not hikari.UNDEFINED and (user := user_mentions.get(user_id)):
                name = user.display_name or user.username
            elif event.guild_id and (member := self.bot.cache.get_member(event.guild_id, user_id)):
                name = member.display_name
            elif user := self.bot.cache.get_user(user_id):
                name = user.display_name or user.username

            return f" {name or 'user'} "

        def channel_name(match: re.Match[str]) -> str:
            channel_id = hikari.Snowflake(int(match.group(1)))
            name: str | None = None

            if channel_mentions is not hikari.UNDEFINED and (channel := channel_mentions.get(channel_id)):
                name = getattr(channel, "name", None)
            elif channel := self.bot.cache.get_guild_channel(channel_id):
                name = getattr(channel, "name", None)

            return f" {name or 'channel'} "

        text = USER_MENTION_RE.sub(user_name, text)
        text = CHANNEL_MENTION_RE.sub(channel_name, text)
        return text

    @staticmethod
    def _emoji_tag_to_words(tag: str) -> str:
        name = tag.strip(":").replace("_", " ").replace("-", " ")
        return name.strip() or "emoji"

    @staticmethod
    def _resolve_local_tts_engine() -> tuple[str, str | None]:
        preferred = (config.TTS_ENGINE or "auto").strip().lower()
        if preferred in {"", "auto"}:
            if piper_path := shutil.which("piper"):
                return "piper", piper_path
            if espeak_path := VoiceTTSService._resolve_espeak_engine():
                return "espeak", espeak_path
            return "auto", None

        if preferred in {"espeak", "espeak-ng", "espeak_ng"}:
            return "espeak", VoiceTTSService._resolve_espeak_engine()

        if preferred == "piper":
            return "piper", shutil.which("piper")

        log.warning(f"Unknown TTS_ENGINE={preferred!r}; falling back to auto")
        if piper_path := shutil.which("piper"):
            return "piper", piper_path
        if espeak_path := VoiceTTSService._resolve_espeak_engine():
            return "espeak", espeak_path
        return "auto", None

    @staticmethod
    def _resolve_espeak_engine() -> str | None:
        for executable in ("espeak-ng", "espeak"):
            if path := shutil.which(executable):
                return path
        return None

    def _engine_display(self) -> str:
        if not self._engine:
            return "none"
        return f"{self._engine_kind}:{self._engine}"

    def _initial_piper_voice(self) -> str:
        if model := config.TTS_PIPER_MODEL:
            return model

        configured_voice = (config.TTS_VOICE or "").strip()
        if configured_voice and self._piper_model_path(configured_voice):
            return configured_voice

        if discovered := self._piper_discover_models():
            return discovered[0].stem

        return configured_voice or "en-gb-x-rp"

    def _piper_available_voices(self) -> list[str]:
        voices: set[str] = set()
        for model in self._piper_discover_models():
            voices.add(model.stem)

        if model := config.TTS_PIPER_MODEL:
            voices.add(model)

        if self.voice and self._piper_model_path(self.voice):
            voices.add(self.voice)

        return sorted(voices)

    def _piper_discover_models(self) -> list[Path]:
        models: list[Path] = []
        seen: set[str] = set()
        for data_dir in self._piper_model_search_dirs():
            for model in sorted(data_dir.glob("*.onnx")):
                resolved = str(model.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                models.append(model)
        return models

    def _piper_custom_write_dir(self) -> Path:
        for path in self._piper_custom_model_dirs(include_missing=True):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            if path.exists() and path.is_dir():
                return path
        raise OSError("Unable to create a writable custom Piper model directory.")

    def _piper_custom_models(self) -> list[Path]:
        models: list[Path] = []
        seen: set[str] = set()
        for data_dir in self._piper_custom_model_dirs():
            if not data_dir.exists() or not data_dir.is_dir():
                continue
            for model in sorted(data_dir.glob("*.onnx")):
                resolved = str(model.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                models.append(model)
        return models

    def _piper_custom_model_path(self, model: str) -> Path | None:
        needle = model.strip().lower()
        if not needle:
            return None

        for path in self._piper_custom_models():
            if path.stem.lower() == needle or path.name.lower() == needle:
                return path
        return None

    def _piper_custom_model_dirs(self, include_missing: bool = False) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()

        def add_dir(path: Path):
            if not include_missing and (not path.exists() or not path.is_dir()):
                return
            resolved = str(path.resolve())
            if resolved in seen:
                return
            seen.add(resolved)
            dirs.append(path)

        if self._piper_data_dir:
            configured = Path(self._piper_data_dir).expanduser()
            add_dir(configured / "custom")

        bot_dir = Path(__file__).resolve().parent
        add_dir(bot_dir / "voices" / "piper" / "custom")

        cwd = Path.cwd()
        add_dir(cwd / "voices" / "piper" / "custom")

        return dirs

    def _piper_model_search_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()

        def add_dir(path: Path):
            if not path.exists() or not path.is_dir():
                return
            resolved = str(path.resolve())
            if resolved in seen:
                return
            seen.add(resolved)
            dirs.append(path)

        if self._piper_data_dir:
            configured = Path(self._piper_data_dir).expanduser()
            add_dir(configured)
            add_dir(configured / "custom")

        bot_dir = Path(__file__).resolve().parent
        bot_voices_dir = bot_dir / "voices" / "piper"
        add_dir(bot_voices_dir / "custom")
        add_dir(bot_voices_dir)

        bot_voice_dir = bot_dir / "voice" / "piper"
        add_dir(bot_voice_dir)

        add_dir(bot_dir)

        cwd = Path.cwd()
        cwd_voices_dir = cwd / "voices" / "piper"
        add_dir(cwd_voices_dir / "custom")
        add_dir(cwd_voices_dir)

        cwd_voice_dir = cwd / "voice" / "piper"
        add_dir(cwd_voice_dir)

        add_dir(cwd)

        return dirs

    def _piper_available_variants(self, voice: str) -> list[str]:
        raw = self._piper_load_config(voice)
        if not raw:
            return []

        speaker_map = raw.get("speaker_id_map")
        if isinstance(speaker_map, dict) and speaker_map:
            variants = {str(name).strip() for name in speaker_map if str(name).strip()}
            for sid in speaker_map.values():
                try:
                    variants.add(str(int(sid)))
                except (TypeError, ValueError):
                    continue
            variants = sorted(variants)
            if variants:
                return variants

        num_speakers = raw.get("num_speakers")
        if isinstance(num_speakers, int) and num_speakers > 1:
            return [str(i) for i in range(num_speakers)]

        return []

    @staticmethod
    def _variant_gender_hint(value: str) -> str | None:
        tokens = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
        if not tokens:
            return None

        if tokens & {"female", "woman", "girl", "fem", "f"}:
            return "female"
        if tokens & {"male", "man", "boy", "masc", "m"}:
            return "male"
        if tokens & {"neutral", "nonbinary", "non-binary", "nb", "androgynous"}:
            return "neutral"
        return None

    def _piper_variant_details(self, voice: str) -> dict[str, str]:
        raw = self._piper_load_config(voice)
        if not raw:
            return {}

        speaker_map = raw.get("speaker_id_map")
        if not isinstance(speaker_map, dict) or not speaker_map:
            return {}

        name_to_id: dict[str, int] = {}
        id_to_name: dict[int, str] = {}
        id_to_gender: dict[int, str] = {}

        for raw_name, raw_id in speaker_map.items():
            name = str(raw_name).strip()
            if not name:
                continue

            try:
                speaker_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            name_to_id[name] = speaker_id
            id_to_name.setdefault(speaker_id, name)

            gender = self._variant_gender_hint(name)
            if gender and speaker_id not in id_to_gender:
                id_to_gender[speaker_id] = gender

        details: dict[str, str] = {}
        for name, speaker_id in name_to_id.items():
            parts = [f"id {speaker_id}"]
            gender = self._variant_gender_hint(name)
            if gender:
                parts.append(gender)
            details[name] = "; ".join(parts)

        for speaker_id, name in id_to_name.items():
            parts = [f"name {name}"]
            gender = id_to_gender.get(speaker_id)
            if gender:
                parts.append(gender)
            details[str(speaker_id)] = "; ".join(parts)

        return details

    @staticmethod
    def _variant_choice_label(variant: str, detail: str | None) -> str:
        if not detail:
            return variant[:100]

        max_len = 100
        label = f"{variant} ({detail})"
        if len(label) <= max_len:
            return label

        available = max_len - len(variant) - 3
        if available <= 3:
            return variant[:max_len]

        clipped = detail[: available - 3].rstrip()
        return f"{variant} ({clipped}...)"

    def variant_autocomplete_choices(self, voice: str, variants: list[str], needle: str = ""):
        detail_map = self._piper_variant_details(voice) if self._engine_kind == "piper" else {}
        acb = hikari.impl.AutocompleteChoiceBuilder
        choices = []

        for variant in variants:
            detail = "disable variant" if variant == "none" else detail_map.get(variant)
            search_blob = f"{variant} {detail or ''}".lower()
            if needle and needle not in search_blob:
                continue
            label = self._variant_choice_label(variant, detail)
            choices.append(acb(label, variant))

        return choices[:25]

    def _piper_speaker_id(self, voice: str, variant: str | None) -> int | None:
        if not variant:
            return None

        value = variant.strip()
        if value.isdigit():
            return int(value)

        raw = self._piper_load_config(voice)
        if not raw:
            log.warning(f"TTS Piper speaker map unavailable for voice={voice!r}; using default speaker")
            return None

        speaker_map = raw.get("speaker_id_map")
        if not isinstance(speaker_map, dict):
            log.warning(f"TTS Piper voice has no named speakers for voice={voice!r}; using default speaker")
            return None

        match = next((sid for name, sid in speaker_map.items() if str(name).lower() == value.lower()), None)
        if match is None:
            log.warning(f"TTS Piper unknown speaker={variant!r} for voice={voice!r}; using default speaker")
            return None

        try:
            return int(match)
        except (TypeError, ValueError):
            log.warning(f"TTS Piper invalid speaker id for speaker={variant!r} voice={voice!r}; using default speaker")
            return None

    def _piper_load_config(self, voice: str) -> dict[str, object] | None:
        config_path = self._piper_config_file(voice)
        if not config_path:
            return None

        try:
            mtime_ns = config_path.stat().st_mtime_ns
            cache_key = str(config_path.resolve())
        except OSError as xcp:
            log.warning(f"TTS Piper config stat failed path={config_path!s}: {type(xcp).__name__}: {xcp}")
            return None

        cached = self._piper_config_cache.get(cache_key)
        if cached and cached[0] == mtime_ns:
            return cached[1]

        try:
            raw = json.loads(config_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(f"TTS Piper config read failed path={config_path!s}: {type(xcp).__name__}: {xcp}")
            self._piper_config_cache[cache_key] = (mtime_ns, None)
            return None

        if not isinstance(raw, dict):
            log.warning(f"TTS Piper config invalid path={config_path!s}: expected JSON object")
            self._piper_config_cache[cache_key] = (mtime_ns, None)
            return None

        self._piper_config_cache[cache_key] = (mtime_ns, raw)
        return raw

    def _piper_config_file(self, voice: str) -> Path | None:
        if self._piper_config_path:
            configured = Path(self._piper_config_path).expanduser()
            if configured.exists():
                return configured
            log.warning(f"TTS Piper config missing: {configured!s}")
            return None

        model_path = self._piper_model_path(voice)
        if not model_path:
            return None

        inferred = Path(f"{model_path}.json")
        if inferred.exists():
            return inferred

        return None

    def _piper_model_path(self, voice: str) -> Path | None:
        value = voice.strip()
        if not value:
            return None

        direct = Path(value).expanduser()
        if direct.exists() and direct.is_file():
            return direct
        if direct.suffix != ".onnx":
            with_suffix = direct.with_suffix(".onnx")
            if with_suffix.exists() and with_suffix.is_file():
                return with_suffix

        filename = direct.name if direct.suffix == ".onnx" else f"{direct.name}.onnx"
        for data_dir in self._piper_model_search_dirs():
            candidate = data_dir / filename
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _hf_parse_repo_url(url: str) -> HFRepoRef:
        value = url.strip()
        if not value:
            raise ValueError("url must not be empty")

        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only HTTP/HTTPS Hugging Face URLs are supported.")
        if parsed.netloc.lower() not in HUGGINGFACE_HOSTS:
            raise ValueError("Only huggingface.co model links are supported.")

        parts = [segment for segment in parsed.path.split("/") if segment]
        if len(parts) < 2:
            raise ValueError("Expected a URL like https://huggingface.co/<owner>/<repo>")

        repo_id = f"{parts[0]}/{parts[1]}"
        revision = "main"
        onnx_file: str | None = None

        if len(parts) >= 3 and parts[2] in {"blob", "resolve", "tree"}:
            if len(parts) < 4:
                raise ValueError("Invalid Hugging Face URL.")
            revision = unquote(parts[3])
            if parts[2] in {"blob", "resolve"}:
                if len(parts) < 5:
                    raise ValueError("Model file URL must include a file path.")
                onnx_file = unquote("/".join(parts[4:]))

        if onnx_file and not onnx_file.lower().endswith(".onnx"):
            raise ValueError("Model file URL must point to a `.onnx` file.")

        return HFRepoRef(repo_id=repo_id, revision=revision, onnx_file=onnx_file)

    @staticmethod
    def _hf_repo_files(repo_id: str, revision: str) -> list[str]:
        api_url = f"https://huggingface.co/api/models/{repo_id}"
        try:
            with requests.get(api_url, params={"revision": revision}, timeout=30) as response:
                if response.status_code == 404:
                    raise LookupError(f"Hugging Face repository `{repo_id}` not found.")

                try:
                    response.raise_for_status()
                except requests.HTTPError as xcp:
                    raise RuntimeError(f"Hugging Face API error: {response.status_code}") from xcp

                try:
                    payload = response.json()
                except ValueError as xcp:
                    raise RuntimeError("Hugging Face API returned invalid JSON.") from xcp
        except requests.RequestException as xcp:
            raise RuntimeError(f"Failed to query Hugging Face API: {xcp}") from xcp

        siblings = payload.get("siblings")
        if not isinstance(siblings, list):
            return []

        files: list[str] = []
        for entry in siblings:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("rfilename")
            if isinstance(filename, str) and filename.strip():
                files.append(filename.strip())
        return files

    @classmethod
    def _hf_find_piper_candidates(cls, repo_id: str, revision: str, files: list[str]) -> list[str]:
        file_map: dict[str, str] = {}
        for value in files:
            clean = value.strip()
            if not clean:
                continue
            file_map.setdefault(clean.lower(), clean)

        onnx_files = sorted({path for key, path in file_map.items() if key.endswith(".onnx")}, key=str.lower)
        candidates: list[str] = []

        for onnx_file in onnx_files:
            config_file = file_map.get(f"{onnx_file}.json".lower())
            if not config_file:
                continue

            raw = cls._hf_load_json_file(repo_id, revision, config_file)
            if not raw:
                continue

            if cls._hf_is_piper_model_config(raw):
                candidates.append(onnx_file)

        return candidates

    @classmethod
    def _hf_load_json_file(cls, repo_id: str, revision: str, path: str) -> dict[str, object] | None:
        url = cls._hf_resolve_download_url(repo_id, revision, path)
        try:
            with requests.get(url, timeout=30) as response:
                if response.status_code == 404:
                    return None

                try:
                    response.raise_for_status()
                except requests.HTTPError as xcp:
                    raise RuntimeError(
                        f"Hugging Face file request failed for `{path}`: {response.status_code}"
                    ) from xcp

                try:
                    payload = response.json()
                except ValueError:
                    return None
        except requests.RequestException as xcp:
            raise RuntimeError(f"Failed to read `{path}` from Hugging Face: {xcp}") from xcp
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _hf_is_piper_model_config(raw: dict[str, object]) -> bool:
        audio = raw.get("audio")
        inference = raw.get("inference")
        phoneme_type = raw.get("phoneme_type")
        phoneme_id_map = raw.get("phoneme_id_map")
        sample_rate = audio.get("sample_rate") if isinstance(audio, dict) else None

        return bool(
            isinstance(audio, dict)
            and isinstance(sample_rate, (int, float))
            and isinstance(inference, dict)
            and isinstance(phoneme_type, str)
            and phoneme_type.strip()
            and isinstance(phoneme_id_map, dict)
            and phoneme_id_map
        )

    @staticmethod
    def _hf_resolve_download_url(repo_id: str, revision: str, path: str) -> str:
        quoted_revision = quote(revision, safe="/")
        quoted_path = quote(path, safe="/")
        return f"https://huggingface.co/{repo_id}/resolve/{quoted_revision}/{quoted_path}"

    @staticmethod
    def _download_file(url: str, target: Path, optional: bool) -> bool:
        partial = Path(f"{target}.part")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                if response.status_code == 404 and optional:
                    return False
                if response.status_code == 404:
                    raise LookupError(f"File not found: {url}")
                response.raise_for_status()
                with partial.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            stream.write(chunk)
            partial.replace(target)
            return True
        except Exception:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise

    @staticmethod
    def _voice_spec(voice: str, variant: str | None) -> str:
        if not variant:
            return voice
        return f"{voice}+{variant}"

    @classmethod
    def _normalise_variant(cls, variant: str | None, allow_empty: bool = True) -> str | None:
        if variant is None:
            return None

        value = variant.strip()
        if value.startswith("+"):
            value = value[1:].strip()

        if not value:
            if allow_empty:
                return None
            raise ValueError("variant must not be empty")

        if value.lower() in cls._VARIANT_CLEAR_VALUES:
            return None

        return value

    @classmethod
    def _normalise_substitution_key(cls, source: str) -> str:
        key = source.strip().lower()
        if not key:
            raise ValueError("source must not be empty")
        if len(key) > cls._MAX_SUBSTITUTION_KEY_CHARS:
            raise ValueError(f"source is too long (max {cls._MAX_SUBSTITUTION_KEY_CHARS} chars)")
        if not re.fullmatch(r"[a-z0-9][a-z0-9'_-]*", key):
            raise ValueError("source may only include letters, numbers, apostrophes, underscores, and hyphens")
        return key

    @classmethod
    def _normalise_substitution_value(cls, target: str) -> str:
        value = target.strip()
        if not value:
            raise ValueError("target must not be empty")
        if len(value) > cls._MAX_SUBSTITUTION_VALUE_CHARS:
            raise ValueError(f"target is too long (max {cls._MAX_SUBSTITUTION_VALUE_CHARS} chars)")
        return value

    def _preview(self, text: str) -> str:
        if len(text) <= self._LOG_PREVIEW_CHARS:
            return text
        return text[: self._LOG_PREVIEW_CHARS].rstrip() + "..."

    @staticmethod
    def _playback_timeout_seconds(text: str) -> float:
        words = max(1, len(text.split()))
        # At ~165 wpm this leaves generous headroom for connect/encode jitter.
        return min(120.0, max(10.0, words * 0.7 + 8.0))


async def ac_tts_voices(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return
    needle = ctx.focused.value.strip().lower()
    voices = await voice_tts.available_voices()
    if needle:
        voices = [voice for voice in voices if needle in voice.lower()]
    await ctx.respond(voices[:25])


async def ac_tts_variants(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    voice_opt = ctx.get_option("voice")
    selected_voice: str | None = None
    if voice_opt and isinstance(voice_opt.value, str):
        selected_voice = voice_opt.value.strip() or None
    if not selected_voice:
        selected_voice, _ = voice_tts.user_voice_variant(ctx.interaction.user.id)

    voices = await voice_tts.available_voices()
    if voices:
        match = next((voice for voice in voices if voice.lower() == selected_voice.lower()), None)
        if match:
            selected_voice = match

    variants = ["none", *await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)]
    await ctx.respond(voice_tts.variant_autocomplete_choices(selected_voice, variants, needle))


async def ac_tts_custom_models(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    models = voice_tts.available_custom_voices()
    if needle:
        models = [model for model in models if needle in model.lower()]
    await ctx.respond(models[:25])


async def ac_tts_substitution_sources(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    sources = list(voice_tts.user_text_substitutions(ctx.interaction.user.id))
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


@group_voice.register
class CMD_VoiceSay(
    lightbulb.SlashCommand,
    name="say",
    description="Queue TTS text from any channel",
):
    text = lightbulb.string("text", "What the bot should say")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        guild_id = hikari.Snowflake(ctx.guild_id) if ctx.guild_id else voice_tts.active_voice_guild_id()
        if guild_id is None:
            await ctx.respond(
                "In DMs, `/voice say` works only while I am already connected to the configured voice channel."
            )
            log.info(f"Voice cmd say rejected no_active_connection user={ctx.user.id} guild={ctx.guild_id}")
            return

        log.info(
            f"Voice cmd say invoked user={ctx.user.id} guild={ctx.guild_id} "
            f"resolved_guild={guild_id} text={voice_tts._preview(self.text)!r}"
        )

        try:
            spoken, queue_len = voice_tts.queue_say(guild_id, ctx.interaction.id, self.text, user_id=ctx.user.id)
        except (RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd say rejected user={ctx.user.id} reason={xcp}")
            return

        selected_voice, selected_variant = voice_tts.user_voice_variant_for_say(ctx.user.id)
        voice_spec = voice_tts._voice_spec(selected_voice, selected_variant)
        await ctx.respond(
            "\n".join(
                [
                    f"says `{voice_tts._preview(spoken)}`",
                ]
            )
        )
        log.info(
            f"Voice cmd say success user={ctx.user.id} guild={ctx.guild_id} resolved_guild={guild_id} "
            f"queue_size={queue_len} voice={voice_spec} spoken={voice_tts._preview(spoken)!r}"
        )


@group_voice.register
class CMD_VoiceSet(
    lightbulb.SlashCommand,
    name="set",
    description="Get or set your TTS voice and variant",
):
    _VARIANT_PREVIEW_LIMIT = 15

    voice = lightbulb.string(
        "voice",
        "Voice id (leave empty to view current)",
        autocomplete=ac_tts_voices,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    variant = lightbulb.string(
        "variant",
        "Variant id (optional; use `none` to disable)",
        autocomplete=ac_tts_variants,  # pyright: ignore[reportArgumentType]
        default=None,
    )

    @classmethod
    def _variant_preview(cls, variants: list[str]) -> str:
        if not variants:
            return "`none`"
        shown = ", ".join(f"`{variant}`" for variant in variants[: cls._VARIANT_PREVIEW_LIMIT])
        if len(variants) > cls._VARIANT_PREVIEW_LIMIT:
            shown += f", ... (+{len(variants) - cls._VARIANT_PREVIEW_LIMIT} more)"
        return shown

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_voice, current_variant = voice_tts.user_voice_variant(ctx.user.id)
        is_listening = voice_tts.is_user_listening(ctx.user.id)
        log.info(
            f"Voice cmd set invoked by user={ctx.user.id} requested_voice={self.voice!r} requested_variant={self.variant!r} "
            f"current_voice={current_voice!r} current_variant={current_variant!r} listening={is_listening}"
        )

        if not self.voice and not self.variant:
            guild_id = hikari.Snowflake(ctx.guild_id or config.DISCORD_GUILD)
            conn = voice_tts.get_connection(guild_id)
            voices = await voice_tts.available_voices()
            variants = await voice_tts.available_variants_for_voice(current_voice, force_refresh=True)
            conn_chan = f"<#{conn.channel_id}>" if conn else "not connected"
            await ctx.respond(
                "\n".join(
                    [
                        f"listen: `{'enabled' if is_listening else 'disabled'}`",
                        f"voice: `{current_voice}`",
                        f"variant: `{current_variant or 'none'}`",
                        f"engine: `{voice_tts._engine_display()}`",
                        f"connected: {conn_chan}",
                        f"available voices: `{len(voices)}` (use autocomplete on `voice` option)",
                        f"available variants for `{current_voice}`: `{len(variants)}` (use autocomplete on `variant` option)",
                        f"variants: {self._variant_preview(variants)}",
                    ]
                )
            )
            log.info(
                f"Voice cmd set status user={ctx.user.id} current_voice={current_voice!r} "
                f"current_variant={current_variant!r} listening={is_listening} voices={len(voices)} variants={len(variants)}"
            )
            return

        try:
            selected_voice, selected_variant = await voice_tts.set_user_voice_variant(
                ctx.user.id,
                voice=self.voice,
                variant=self.variant,
            )
        except LookupError as xcp:
            message = str(xcp)
            if message.startswith("Unknown variant:"):
                await ctx.respond(f"Unknown variant `{self.variant}`. Use the `variant` autocomplete.")
                log.info(
                    f"Voice cmd set rejected unknown variant user={ctx.user.id} requested_variant={self.variant!r}"
                )
                return

            await ctx.respond(f"Unknown voice `{self.voice}`. Use the `voice` autocomplete.")
            log.info(f"Voice cmd set rejected unknown voice user={ctx.user.id} requested_voice={self.voice!r}")
            return
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd set rejected invalid user={ctx.user.id} requested_voice={self.voice!r} "
                f"requested_variant={self.variant!r} reason={xcp}"
            )
            return

        variants = await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)
        await ctx.respond(
            "\n".join(
                [
                    f"TTS voice: `{selected_voice}`",
                    f"TTS variant: `{selected_variant or 'none'}`",
                    f"listen: `{'enabled' if voice_tts.is_user_listening(ctx.user.id) else 'disabled'}`",
                    f"available variants for `{selected_voice}`: `{len(variants)}`",
                    f"variants: {self._variant_preview(variants)}",
                    "Applies to your user only. Use `/voice listen enabled:true` to read your messages.",
                ]
            )
        )
        log.info(
            f"Voice cmd set success user={ctx.user.id} selected_voice={selected_voice!r} "
            f"selected_variant={selected_variant!r}"
        )


@group_voice.register
class CMD_VoiceList(
    lightbulb.SlashCommand,
    name="list",
    description="List variants for a voice",
):
    _MAX_MESSAGE_CHARS = 1850

    voice = lightbulb.string(
        "voice",
        "Voice id to list variants for (defaults to your current voice)",
        autocomplete=ac_tts_voices,  # pyright: ignore[reportArgumentType]
        default=None,
    )

    @classmethod
    def _chunk_variant_messages(cls, voice: str, variants: list[str]) -> list[str]:
        header = [f"voice: `{voice}`", f"available variants: `{len(variants)}`"]
        if not variants:
            return ["\n".join([*header, "variants: `none`"])]

        messages: list[str] = []
        current = "\n".join([*header, "variants:"])
        for variant in variants:
            line = f"`{variant}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join([f"voice: `{voice}`", "variants (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_voice, _ = voice_tts.user_voice_variant(ctx.user.id)
        requested_voice = (self.voice or current_voice).strip()

        if not requested_voice:
            await ctx.respond("voice must not be empty")
            log.info(f"Voice cmd list rejected empty_voice user={ctx.user.id}")
            return

        voices = await voice_tts.available_voices(force_refresh=True)
        selected_voice = requested_voice
        if voices:
            match = next((voice for voice in voices if voice.lower() == requested_voice.lower()), None)
            if not match:
                if voice_tts._engine_kind != "piper" or not voice_tts._piper_model_path(requested_voice):
                    await ctx.respond(f"Unknown voice `{requested_voice}`. Use the `voice` autocomplete.")
                    log.info(
                        f"Voice cmd list rejected unknown_voice user={ctx.user.id} requested_voice={requested_voice!r}"
                    )
                    return
            else:
                selected_voice = match

        variants = await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)
        messages = self._chunk_variant_messages(selected_voice, variants)
        for message in messages:
            await ctx.respond(message)

        log.info(
            f"Voice cmd list success user={ctx.user.id} selected_voice={selected_voice!r} variants={len(variants)}"
        )


@group_voice.register
class CMD_VoiceListen(
    lightbulb.SlashCommand,
    name="listen",
    description="Enable or disable TTS listening for your messages",
):
    enabled = lightbulb.boolean(
        "enabled",
        "Enable or disable listening (leave empty to view current state)",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_state = voice_tts.is_user_listening(ctx.user.id)
        current_voice, current_variant = voice_tts.user_voice_variant(ctx.user.id)

        if self.enabled is None:
            await ctx.respond(
                "\n".join(
                    [
                        f"listen: `{'enabled' if current_state else 'disabled'}`",
                        f"voice: `{current_voice}`",
                        f"variant: `{current_variant or 'none'}`",
                        "Use `/voice listen enabled:true` to enable reading your messages.",
                    ]
                )
            )
            log.info(
                f"Voice cmd listen status user={ctx.user.id} enabled={current_state} "
                f"voice={current_voice!r} variant={current_variant!r}"
            )
            return

        updated_state = voice_tts.set_user_listening(ctx.user.id, self.enabled)
        updated_voice, updated_variant = voice_tts.user_voice_variant(ctx.user.id)
        await ctx.respond(
            "\n".join(
                [
                    f"listen: `{'enabled' if updated_state else 'disabled'}`",
                    f"voice: `{updated_voice}`",
                    f"variant: `{updated_variant or 'none'}`",
                ]
            )
        )
        log.info(
            f"Voice cmd listen success user={ctx.user.id} old_enabled={current_state} new_enabled={updated_state} "
            f"voice={updated_voice!r} variant={updated_variant!r}"
        )


@group_voice.register
class CMD_VoiceSub(
    lightbulb.SlashCommand,
    name="sub",
    description="Manage your TTS text substitutions",
):
    _MAX_MESSAGE_CHARS = 1850

    source = lightbulb.string(
        "source",
        "Word to replace (leave empty to list)",
        autocomplete=ac_tts_substitution_sources,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    target = lightbulb.string(
        "target",
        "Replacement text (omit to remove source)",
        default=None,
    )

    @classmethod
    def _chunk_substitution_messages(cls, substitutions: dict[str, str]) -> list[str]:
        header = [f"substitutions: `{len(substitutions)}`"]
        if not substitutions:
            return ["\n".join([*header, "No substitutions set. Example: `/voice sub source:im target:I'm`"])]

        messages: list[str] = []
        current = "\n".join([*header, "source -> target:"])
        for source, target in substitutions.items():
            line = f"`{source}` -> `{target}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join(["substitutions (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @staticmethod
    def _build_substitution_text_file(substitutions: dict[str, str]) -> bytes:
        lines = [f"base substitutions: {len(substitutions)}", ""]
        if substitutions:
            lines.extend(f"{source} -> {target}" for source, target in substitutions.items())
        else:
            lines.append("(none)")
        text = "\n".join(lines) + "\n"
        return text.encode(config.STR_ENCODE, "replace")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        source = self.source.strip() if isinstance(self.source, str) else ""
        target = self.target.strip() if isinstance(self.target, str) else None

        log.info(
            f"Voice cmd sub invoked user={ctx.user.id} source={source!r} target={voice_tts._preview(target or '')!r}"
        )

        if not source and target is not None:
            await ctx.respond("source is required when target is provided")
            log.info(f"Voice cmd sub rejected missing_source user={ctx.user.id}")
            return

        if not source:
            substitutions = voice_tts.user_text_substitutions(ctx.user.id)
            for message in self._chunk_substitution_messages(substitutions):
                await ctx.respond(message)
            base_substitutions = voice_tts.base_text_substitutions()
            base_file = hikari.Bytes(
                self._build_substitution_text_file(base_substitutions),
                "voice_base_substitutions.txt",
            )
            await ctx.respond(
                f"Attached base substitutions file (`{len(base_substitutions)}` entries).",
                attachment=base_file,
            )
            log.info(f"Voice cmd sub list user={ctx.user.id} count={len(substitutions)}")
            return

        if target is None:
            try:
                source_key, removed = voice_tts.remove_user_text_substitution(ctx.user.id, source)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(f"Voice cmd sub rejected remove user={ctx.user.id} source={source!r} reason={xcp}")
                return

            if removed:
                await ctx.respond(f"Removed substitution: `{source_key}`")
            else:
                await ctx.respond(f"No substitution set for `{source_key}`.")
            log.info(f"Voice cmd sub remove user={ctx.user.id} source={source_key!r} removed={removed}")
            return

        try:
            source_key, replacement, existed = voice_tts.set_user_text_substitution(ctx.user.id, source, target)
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd sub rejected set user={ctx.user.id} source={source!r} "
                f"target={voice_tts._preview(target)!r} reason={xcp}"
            )
            return

        action = "Updated" if existed else "Added"
        await ctx.respond(f"{action} substitution: `{source_key}` -> `{replacement}`")
        log.info(
            f"Voice cmd sub set user={ctx.user.id} source={source_key!r} replacement={voice_tts._preview(replacement)!r} "
            f"updated={existed}"
        )


@group_voice.register
class CMD_VoiceAddModel(
    lightbulb.SlashCommand,
    name="addmodel",
    description="Add a custom Piper model from a Hugging Face URL",
):
    _SELECT_TIMEOUT_SECONDS = 90.0
    _SELECT_MAX_OPTIONS = 25

    url = lightbulb.string("url", "Hugging Face repo or .onnx file URL")

    @staticmethod
    def _component_text(value: str, limit: int = 100) -> str:
        text = value.strip() or "-"
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[: limit - 3].rstrip() + "..."

    async def _select_candidate(
        self,
        ctx: lightbulb.Context,
        bot: hikari.GatewayBot,
        repo_ref: HFRepoRef,
        candidates: list[str],
    ) -> str | None:
        if len(candidates) > self._SELECT_MAX_OPTIONS:
            await ctx.respond(
                "\n".join(
                    [
                        f"Found `{len(candidates)}` Piper-compatible files in `{repo_ref.repo_id}`.",
                        "Discord select menus support up to 25 options.",
                        "Use a direct file URL (`.../blob/<rev>/<path>.onnx`) to pick one explicitly.",
                    ]
                )
            )
            return None

        custom_id = f"voice-addmodel:{ctx.user.id}:{ctx.interaction.id}"
        row = hikari.impl.MessageActionRowBuilder()
        menu = row.add_text_menu(custom_id, placeholder="Choose a Piper model file", min_values=1, max_values=1)

        for idx, path in enumerate(candidates):
            label = self._component_text(Path(path).name)
            description = self._component_text(path if "/" in path else f"repo:{repo_ref.repo_id}")
            menu.add_option(label, str(idx), description=description)

        response_id = hikari.Snowflake(
            await ctx.respond(
                "\n".join(
                    [
                        f"Found `{len(candidates)}` Piper-compatible model files in `{repo_ref.repo_id}`.",
                        "Select which one to install:",
                    ]
                ),
                components=[row],
                ephemeral=True,
            )
        )

        def pred(event: hikari.InteractionCreateEvent) -> bool:
            interaction = event.interaction
            if not isinstance(interaction, hikari.ComponentInteraction):
                return False
            if interaction.custom_id != custom_id:
                return False
            if interaction.user.id != ctx.user.id:
                return False
            return bool(interaction.message and interaction.message.id == response_id)

        try:
            event = await bot.wait_for(hikari.InteractionCreateEvent, self._SELECT_TIMEOUT_SECONDS, pred)
        except asyncio.TimeoutError:
            await ctx.edit_response(
                response_id,
                "Model selection timed out. Run `/voice addmodel` again.",
                components=[],
            )
            return None

        interaction = event.interaction
        if not isinstance(interaction, hikari.ComponentInteraction) or not interaction.values:
            await ctx.edit_response(response_id, "No model selected.", components=[])
            return None

        choice = interaction.values[0]
        if not choice.isdigit():
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                "Invalid selection. Run `/voice addmodel` again.",
                components=[],
            )
            return None

        index = int(choice)
        if index < 0 or index >= len(candidates):
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                "Selection out of range. Run `/voice addmodel` again.",
                components=[],
            )
            return None

        selected_file = candidates[index]
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            f"Selected `{Path(selected_file).name}`. Downloading model...",
            components=[],
        )
        return selected_file

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        voice_tts: VoiceTTSService,
        bot: hikari.GatewayBot,
    ):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await ctx.defer()
        log.info(f"Voice cmd addmodel invoked user={ctx.user.id} url={self.url!r}")

        try:
            repo_ref, candidates = await voice_tts.scan_piper_models_from_hf(self.url)
        except (LookupError, RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd addmodel rejected user={ctx.user.id} reason={xcp}")
            return

        if not candidates:
            await ctx.respond(
                "\n".join(
                    [
                        f"No Piper-compatible models found in `{repo_ref.repo_id}` (revision `{repo_ref.revision}`).",
                        "Expected `.onnx` files with matching Piper `.onnx.json` configs.",
                    ]
                )
            )
            log.info(
                f"Voice cmd addmodel rejected no_candidates user={ctx.user.id} repo={repo_ref.repo_id!r} "
                f"revision={repo_ref.revision!r}"
            )
            return

        selected_file = candidates[0]
        if len(candidates) > 1:
            selected_file = await self._select_candidate(ctx, bot, repo_ref, candidates)
            if not selected_file:
                log.info(
                    f"Voice cmd addmodel cancelled selection user={ctx.user.id} repo={repo_ref.repo_id!r} "
                    f"candidates={len(candidates)}"
                )
                return

        try:
            model_name, has_config = await voice_tts.add_piper_model_from_hf(repo_ref, selected_file)
        except FileExistsError as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd addmodel rejected exists user={ctx.user.id} reason={xcp}")
            return
        except (LookupError, RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd addmodel rejected user={ctx.user.id} reason={xcp}")
            return
        except requests.RequestException as xcp:
            await ctx.respond(f"Failed to download model: {xcp}")
            log.warning(f"Voice cmd addmodel network failure user={ctx.user.id}: {xcp}")
            return

        await ctx.respond(
            "\n".join(
                [
                    f"Added TTS model: `{model_name}`",
                    f"model config: `{'downloaded' if has_config else 'not found'}`",
                    f"Use `/voice set voice:{model_name}` to switch to it.",
                ]
            )
        )
        log.info(
            f"Voice cmd addmodel success user={ctx.user.id} model={model_name!r} config={has_config} "
            f"repo={repo_ref.repo_id!r} file={selected_file!r}"
        )


@group_voice.register
class CMD_VoiceDeleteModel(
    lightbulb.SlashCommand,
    name="delmodel",
    description="Delete a custom Piper model",
):
    model = lightbulb.string(
        "model",
        "Model name to delete",
        autocomplete=ac_tts_custom_models,  # pyright: ignore[reportArgumentType]
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"Voice cmd delmodel invoked user={ctx.user.id} model={self.model!r}")

        try:
            removed = await voice_tts.delete_piper_model(self.model)
        except LookupError:
            await ctx.respond(f"Unknown model `{self.model}`. Use the `model` autocomplete.")
            log.info(f"Voice cmd delmodel rejected missing user={ctx.user.id} model={self.model!r}")
            return
        except (RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd delmodel rejected user={ctx.user.id} reason={xcp}")
            return

        await ctx.respond(f"Deleted TTS model `{removed}`.")
        log.info(f"Voice cmd delmodel success user={ctx.user.id} model={removed!r}")
