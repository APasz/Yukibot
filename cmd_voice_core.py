from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import sys
from collections.abc import Awaitable, Iterable, Mapping
from pathlib import Path
from string import Formatter
from typing import TYPE_CHECKING, Callable, ClassVar, TypeVar
from urllib.parse import unquote, urlparse

import hikari
import hikariwave
import lightbulb
from lightbulb import Choice
from modmux import Muxer, SteamCreds
from modmux.models import ProviderCreds
from modmux.modmux_errors import ModMuxError
from modmux.providers.curseforge import CurseforgeCreds
from modmux.providers.modio import ModioCreds
from modmux.providers.modrinth import ModrinthCreds
from modmux.providers.nexusmods import NexusCreds
from modmux.providers.wube import WubeCreds
from pydantic import SecretStr

import config
from _async_utils import run_blocking
from cmd_voice_common import (
    MAX_TTS_VOICES,
    VARIANT_FILE_RE,
    VOICE_CORRECTIONS_FILE,
    VOICE_LINE_RE,
    VOICE_LINK_RULES_FILE,
    VOICE_TARGET_LABELS_FILE,
    VOICE_USERS_FILE,
    HFRepoRef,
    PiperPythonVoiceRuntime,
    PronunciationFormat,
    PronunciationOverride,
    TextCorrectionCatalog,
    TextSubstitutionRule,
    UserVoiceSettings,
    VoiceConnectBackoff,
    VoiceJob,
    VoiceLinkRegexRuleSpec,
    VoiceLinkRule,
    VoiceLinkRuleDraft,
    VoiceLinkRuleMode,
    VoiceLinkRules,
    VoiceLinkSimpleRuleSpec,
    VoiceRuntimeResetResult,
    log,
)
from voice_common import VoiceUdpDiscoveryNetworkError, VoiceUdpDiscoveryTimeoutError, cached_voice_channel_occupants

tts_log = logging.getLogger(config.LOGGER_TTS)


class VoiceTTSCoreMixin:
    _TEXT_CORRECTION_CATEGORY_ALIASES: ClassVar[dict[str, str]] = {
        "slang": "slang",
        "typo": "typos",
        "typos": "typos",
    }
    _MAX_AVAILABLE_VOICES: ClassVar[int] = MAX_TTS_VOICES
    _BOT_CONFIGURATION_PATH: ClassVar[Path] = Path("configuration.json")
    _VOICE_TARGETS_CONFIG_KEY: ClassVar[str] = "voice_targets"

    if TYPE_CHECKING:
        bot: hikari.GatewayBot
        _voice_client: hikariwave.VoiceClient
        _backlog_job_count: int
        _engine_kind: str
        _engine: str | None
        _piper_data_dir: str | None
        _piper_config_path: str | None
        _users_path: Path
        _corrections_path: Path
        _voice_target_labels_path: Path
        _voice_link_rules_path: Path
        _bot_configuration_path: Path
        voice: str
        variant: str | None
        _text_corrections: TextCorrectionCatalog
        _voice_link_rules: VoiceLinkRules
        _voice_link_rules_mtime_ns: int | None
        _mod_link_name_cache: dict[str, str | None]
        _modmux: Muxer | None
        _voice_target_name_cache: dict[hikari.Snowflake, str]
        _voice_target_choices_dirty: bool
        _enabled: bool
        _VOICE_CONNECT_FAILURE_COOLDOWN_SECONDS: ClassVar[float]
        _VOICE_UDP_DISCOVERY_COOLDOWN_SECONDS: ClassVar[float]
        _MAX_SUBSTITUTIONS_PER_USER: ClassVar[int]

        @staticmethod
        def _resolve_local_tts_engine() -> tuple[str, str | None]: ...

        @staticmethod
        def _resolve_piper_python_loader() -> Callable[[str, str | None], PiperPythonVoiceRuntime] | None: ...

        def _initial_piper_voice(self) -> str: ...

        @classmethod
        def _normalise_variant(cls, variant: str | None, allow_empty: bool = True) -> str | None: ...

        @classmethod
        def _normalise_substitution_key(cls, source: str, *, case_sensitive: bool = False) -> str: ...

        @classmethod
        def _normalise_substitution_value(cls, target: str) -> str: ...
        @staticmethod
        def _normalise_pronunciation_format(value: PronunciationFormat | str) -> PronunciationFormat: ...
        def voice_supports_ipa_pronunciations(self, voice: str) -> bool: ...

        def _piper_model_path(self, voice: str) -> Path | None: ...
        def _piper_available_voices(self) -> list[str]: ...
        def _piper_available_variants(self, voice: str) -> list[str]: ...
        def _piper_custom_write_dir(self) -> Path: ...
        def _piper_custom_model_path(self, model: str) -> Path | None: ...
        def _piper_custom_models(self) -> list[Path]: ...

        @staticmethod
        def _hf_parse_repo_url(url: str) -> HFRepoRef: ...

        @staticmethod
        def _hf_repo_files(repo_id: str, revision: str) -> list[str]: ...

        @classmethod
        def _hf_find_piper_candidates(cls, repo_id: str, revision: str, files: list[str]) -> list[str]: ...

        @classmethod
        def _hf_is_piper_file_candidate(cls, repo_id: str, revision: str, onnx_file: str) -> bool: ...

        @staticmethod
        def _hf_resolve_download_url(repo_id: str, revision: str, path: str) -> str: ...

        @staticmethod
        def _download_file(url: str, target: Path, optional: bool) -> bool: ...

        def _drop_queued_jobs(self) -> int: ...
        def _engine_display(self) -> str: ...

    def __init__(self, bot: hikari.GatewayBot, voice_client: hikariwave.VoiceClient):
        self.bot = bot
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH
        self._voice_targets = self._load_voice_targets()

        self._voice_client = voice_client
        self._music_active_channel_provider: Callable[[hikari.Snowflake], hikari.Snowflake | None] | None = None
        self._music_duck_handler: (
            Callable[
                [hikari.Snowflake, hikari.Snowflake, bytes],
                Awaitable[tuple[hikariwave.VoiceConnection, hikariwave.AudioSource] | None],
            ]
            | None
        ) = None
        self._queue: asyncio.Queue[VoiceJob] = asyncio.Queue()
        self._backlog_job_count = 0
        self._worker_task: asyncio.Task[None] | None = None
        self._engine_kind, self._engine = self._resolve_local_tts_engine()
        self._piper_python_loader = self._resolve_piper_python_loader()
        self._piper_python_voice_cache: dict[str, PiperPythonVoiceRuntime] = {}
        self._piper_data_dir = config.TTS_PIPER_DATA_DIR
        self._piper_config_path = config.TTS_PIPER_CONFIG
        self._users_path = VOICE_USERS_FILE
        self._corrections_path = VOICE_CORRECTIONS_FILE
        self._voice_target_labels_path = VOICE_TARGET_LABELS_FILE
        self._voice_link_rules_path = VOICE_LINK_RULES_FILE
        self._user_settings: dict[int, UserVoiceSettings] = {}
        self._voice_connect_backoff: dict[hikari.Snowflake, VoiceConnectBackoff] = {}
        self.voice = config.TTS_VOICE
        if self._engine_kind == "piper":
            self.voice = self._initial_piper_voice()
        self.variant = self._normalise_variant(config.TTS_VARIANT)
        self._available_voices: list[str] = []
        self._available_variants: list[str] = []
        self._piper_config_cache: dict[str, tuple[int, dict[str, object] | None]] = {}
        self._text_corrections = self._load_text_corrections()
        self._voice_link_rules = self._load_voice_link_rules()
        self._voice_link_rules_mtime_ns = self._path_mtime_ns(self._voice_link_rules_path)
        self._mod_link_name_cache = {}
        self._modmux = None
        self._load_user_settings()
        self._voice_target_name_cache = self._load_voice_target_name_cache()
        self._voice_target_choices_dirty = True

        self._enabled = bool(self._voice_targets)
        if not self._enabled:
            log.warning("Voice TTS disabled: configure VOICE_TARGETS or the legacy VOICE_CHANNEL/TTS_CHANNEL pair")
        elif not self._engine:
            requested = config.TTS_ENGINE or "auto"
            log.warning(f"Voice TTS disabled: local TTS engine not found for {requested=!r} (espeak-ng/espeak/piper)")
        elif self._engine_kind == "piper" and not self._piper_model_path(self.voice):
            model_hint = f"voice={self.voice!r} data_dir={self._piper_data_dir!r}"
            log.warning(
                "Voice TTS Piper model could not be resolved; "
                f"set TTS_PIPER_MODEL/TTS_VOICE and TTS_PIPER_DATA_DIR if needed ({model_hint})"
            )

    async def setup(self, client: lightbulb.Client | None = None):
        if self._worker_task:
            return
        self._worker_task = asyncio.create_task(self._worker_loop(), name="voice-tts-worker")  # pyright: ignore[reportAttributeAccessIssue]
        if not shutil.which("ffmpeg"):
            log.warning("Voice playback may fail: ffmpeg is not available in PATH.")
        await self._validate_voice()
        await self._validate_variant()
        if client:
            await self.sync_voice_target_choices(client, reason="startup")
        if self._enabled:
            users = self.listening_users()
            target_users = ",".join(str(uid) for uid in users) if users else "none"
            target_summary = ", ".join(
                (
                    f"{target.guild_id}:tts={int(target.primary_tts_channel)}"
                    + (":on" if target.primary_tts_listen_enabled else ":off")
                    + (
                        f",{int(target.secondary_tts_channel)}"
                        f"{':on' if target.secondary_tts_listen_enabled else ':off'}"
                        if target.secondary_tts_channel is not None
                        else ""
                    )
                    + f"/voice={target.voice_channel}"
                )
                for target in sorted(self._voice_targets.values(), key=lambda item: int(item.guild_id))
            )
            log.info(
                f"Voice TTS ready: targets=[{target_summary}] target_users={target_users} "
                f"voice={self.voice} variant={self.variant or 'none'} engine={self._engine_display()}"
            )

    async def close(self):
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        self._drop_queued_jobs()
        if self._modmux is not None:
            await self._modmux.aclose()
            self._modmux = None

    async def reset_runtime(
        self,
        extra_guild_ids: Iterable[hikari.Snowflakeish] = (),
    ) -> VoiceRuntimeResetResult:
        guild_ids = {hikari.Snowflake(guild_id) for guild_id in extra_guild_ids}
        guild_ids.update(self.configured_voice_guild_ids())
        guild_ids.update(hikari.Snowflake(connection.guild_id) for connection in self.active_voice_connections())

        outstanding_job_count = self._backlog_job_count
        active_connection_count = sum(1 for guild_id in guild_ids if self.get_connection(guild_id) is not None)
        targeted_guild_count = len(guild_ids)
        backoff_count = len(self._voice_connect_backoff)

        await self.close()
        self._voice_connect_backoff.clear()

        for guild_id in sorted(guild_ids, key=int):
            connection = self.get_connection(guild_id)
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.player.clear_queue()
                with contextlib.suppress(Exception):
                    await connection.player.stop()

            with contextlib.suppress(Exception):
                await self._voice_client.disconnect(guild_id=guild_id)
            with contextlib.suppress(Exception):
                await self.bot.update_voice_state(guild_id, None)

        await self.setup()
        worker_restarted = self._worker_task is not None and not self._worker_task.done()

        result = VoiceRuntimeResetResult(
            outstanding_job_count=outstanding_job_count,
            active_connection_count=active_connection_count,
            targeted_guild_count=targeted_guild_count,
            backoff_count=backoff_count,
            worker_restarted=worker_restarted,
        )

        log.warning(
            "TTS runtime reset outstanding_jobs=%s active_connections=%s targeted_guilds=%s backoffs=%s worker=%s",
            result.outstanding_job_count,
            result.active_connection_count,
            result.targeted_guild_count,
            result.backoff_count,
            result.worker_restarted,
        )
        return result

    def get_connection(self, guild_id: hikari.Snowflakeish) -> hikariwave.VoiceConnection | None:
        return self._voice_client.get_connection(guild_id=guild_id)

    def set_music_active_channel_provider(
        self,
        provider: Callable[[hikari.Snowflake], hikari.Snowflake | None] | None,
    ) -> None:
        self._music_active_channel_provider = provider

    def set_music_duck_handler(
        self,
        handler: Callable[
            [hikari.Snowflake, hikari.Snowflake, bytes],
            Awaitable[tuple[hikariwave.VoiceConnection, hikariwave.AudioSource] | None],
        ]
        | None,
    ) -> None:
        self._music_duck_handler = handler

    def _active_music_channel(self, guild_id: hikari.Snowflakeish) -> hikari.Snowflake | None:
        if not self._music_active_channel_provider:
            return None
        return self._music_active_channel_provider(hikari.Snowflake(guild_id))

    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None:
        return self._voice_targets.get(hikari.Snowflake(guild_id))

    def voice_targets(self) -> Mapping[hikari.Snowflake, config.VoiceTargetConfig]:
        return dict(self._voice_targets)

    def _load_voice_targets(self) -> dict[hikari.Snowflake, config.VoiceTargetConfig]:
        if not self._bot_configuration_path.exists():
            return dict(config.VOICE_TARGETS)

        try:
            bot_config = config.load_bot_configuration(self._bot_configuration_path)
            payload = {
                guild_id: target.model_dump(mode="json")
                for guild_id, target in bot_config.voice_targets.items()
            }
            return config.parse_voice_targets_payload(
                payload,
                source=f"configuration.json.{self._VOICE_TARGETS_CONFIG_KEY}",
            )
        except (OSError, ValueError) as xcp:
            log.warning(
                f"TTS voice-target config read failed path={self._bot_configuration_path!s}: "
                f"{type(xcp).__name__}: {xcp}"
            )
            return dict(config.VOICE_TARGETS)

    def _save_voice_targets(self) -> None:
        voice_targets = {
            str(guild_id): config.PersistedVoiceTarget(
                voice_channel=int(target.voice_channel),
                primary_tts_channel=int(target.primary_tts_channel),
                primary_tts_listen_enabled=target.primary_tts_listen_enabled,
                secondary_tts_channel=(
                    int(target.secondary_tts_channel) if target.secondary_tts_channel is not None else None
                ),
                secondary_tts_listen_enabled=target.secondary_tts_listen_enabled,
                relay_tts_enabled=target.relay_tts_enabled,
            )
            for guild_id, target in sorted(self._voice_targets.items(), key=lambda item: int(item[0]))
        }
        bot_config = config.BotConfiguration()
        if self._bot_configuration_path.exists():
            try:
                bot_config = config.load_bot_configuration(self._bot_configuration_path)
            except (OSError, ValueError) as xcp:
                log.warning(
                    f"TTS bot configuration read failed path={self._bot_configuration_path!s}: "
                    f"{type(xcp).__name__}: {xcp}"
                )
        bot_config.voice_targets = voice_targets
        config.save_bot_configuration(self._bot_configuration_path, bot_config)

    def set_voice_target_config(
        self,
        guild_id: hikari.Snowflakeish,
        *,
        voice_channel: hikari.Snowflakeish,
        primary_tts_channel: hikari.Snowflakeish,
        primary_tts_listen_enabled: bool | None = None,
        secondary_tts_channel: hikari.Snowflakeish | None = None,
        secondary_tts_listen_enabled: bool | None = None,
        relay_tts_enabled: bool | None = None,
    ) -> config.VoiceTargetConfig:
        guild = hikari.Snowflake(guild_id)
        existing = self._voice_targets.get(guild)
        primary_tts = hikari.Snowflake(primary_tts_channel)
        secondary_tts = hikari.Snowflake(secondary_tts_channel) if secondary_tts_channel is not None else None
        if secondary_tts is not None and secondary_tts == primary_tts:
            raise ValueError("Secondary TTS channel must differ from the primary TTS channel.")
        resolved_primary_listen_enabled = (
            primary_tts_listen_enabled
            if primary_tts_listen_enabled is not None
            else existing.primary_tts_listen_enabled
            if existing is not None
            else True
        )
        resolved_secondary_listen_enabled = (
            False
            if secondary_tts is None
            else (
                secondary_tts_listen_enabled
                if secondary_tts_listen_enabled is not None
                else (
                    existing.secondary_tts_listen_enabled
                    if existing is not None and existing.secondary_tts_channel == secondary_tts
                    else True
                )
            )
        )
        target = config.VoiceTargetConfig(
            guild_id=guild,
            voice_channel=hikari.Snowflake(voice_channel),
            primary_tts_channel=primary_tts,
            primary_tts_listen_enabled=resolved_primary_listen_enabled,
            secondary_tts_channel=secondary_tts,
            secondary_tts_listen_enabled=resolved_secondary_listen_enabled,
            relay_tts_enabled=(
                relay_tts_enabled
                if relay_tts_enabled is not None
                else existing.relay_tts_enabled
                if existing
                else False
            ),
        )
        self._voice_targets[target.guild_id] = target
        self._enabled = bool(self._voice_targets)
        self._voice_target_choices_dirty = True
        if self._voice_target_name_cache.pop(target.guild_id, None) is not None:
            self._save_voice_target_name_cache()
        self._save_voice_targets()
        return target

    def set_voice_target_relay_tts_enabled(
        self,
        guild_id: hikari.Snowflakeish,
        enabled: bool,
    ) -> config.VoiceTargetConfig:
        target = self.voice_target(guild_id)
        if target is None:
            raise LookupError("Unknown voice target.")
        return self.set_voice_target_config(
            guild_id,
            voice_channel=target.voice_channel,
            primary_tts_channel=target.primary_tts_channel,
            primary_tts_listen_enabled=target.primary_tts_listen_enabled,
            secondary_tts_channel=target.secondary_tts_channel,
            secondary_tts_listen_enabled=target.secondary_tts_listen_enabled,
            relay_tts_enabled=enabled,
        )

    def set_voice_target_tts_listen_enabled(
        self,
        guild_id: hikari.Snowflakeish,
        role: config.VoiceTargetTtsChannelRole,
        enabled: bool,
    ) -> config.VoiceTargetConfig:
        target = self.voice_target(guild_id)
        if target is None:
            raise LookupError("Unknown voice target.")
        if role is config.VoiceTargetTtsChannelRole.SECONDARY and target.secondary_tts_channel is None:
            raise LookupError("Secondary TTS channel is not configured.")
        return self.set_voice_target_config(
            guild_id,
            voice_channel=target.voice_channel,
            primary_tts_channel=target.primary_tts_channel,
            primary_tts_listen_enabled=enabled
            if role is config.VoiceTargetTtsChannelRole.PRIMARY
            else target.primary_tts_listen_enabled,
            secondary_tts_channel=target.secondary_tts_channel,
            secondary_tts_listen_enabled=(
                enabled if role is config.VoiceTargetTtsChannelRole.SECONDARY else target.secondary_tts_listen_enabled
            ),
            relay_tts_enabled=target.relay_tts_enabled,
        )

    def remove_voice_target_config(self, guild_id: hikari.Snowflakeish) -> bool:
        guild = hikari.Snowflake(guild_id)
        removed = self._voice_targets.pop(guild, None)
        if removed is None:
            return False
        self._enabled = bool(self._voice_targets)
        self._voice_target_choices_dirty = True
        if self._voice_target_name_cache.pop(guild, None) is not None:
            self._save_voice_target_name_cache()
        self._save_voice_targets()
        return True

    def _load_voice_target_name_cache(self) -> dict[hikari.Snowflake, str]:
        if not self._voice_target_labels_path.exists():
            return {}

        try:
            raw = json.loads(self._voice_target_labels_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(
                f"TTS voice-target label cache read failed path={self._voice_target_labels_path!s}: "
                f"{type(xcp).__name__}: {xcp}"
            )
            return {}

        if not isinstance(raw, dict):
            return {}

        labels: dict[hikari.Snowflake, str] = {}
        for guild_key, label in raw.items():
            if not isinstance(label, str) or not label.strip():
                continue
            try:
                guild_id = hikari.Snowflake(str(guild_key).strip())
            except ValueError:
                continue
            if guild_id not in self._voice_targets:
                continue
            labels[guild_id] = label.strip()
        return labels

    def _save_voice_target_name_cache(self) -> None:
        payload = {
            str(guild_id): self._voice_target_name_cache[guild_id]
            for guild_id in self.configured_voice_guild_ids()
            if guild_id in self._voice_target_name_cache
        }
        try:
            self._voice_target_labels_path.write_text(json.dumps(payload, indent=4), config.STR_ENCODE)
        except OSError as xcp:
            log.warning(
                f"TTS voice-target label cache write failed path={self._voice_target_labels_path!s}: "
                f"{type(xcp).__name__}: {xcp}"
            )

    def _set_voice_target_label(self, guild_id: hikari.Snowflakeish, label: str) -> bool:
        guild = hikari.Snowflake(guild_id)
        clean = label.strip()
        if not clean:
            return False
        if self._voice_target_name_cache.get(guild) == clean:
            return False
        self._voice_target_name_cache[guild] = clean
        self._save_voice_target_name_cache()
        return True

    def configured_voice_guild_ids(self) -> list[hikari.Snowflake]:
        guild_ids = sorted(self._voice_targets, key=int)
        primary = hikari.Snowflake(config.DISCORD_GUILD)
        if primary in self._voice_targets:
            guild_ids.remove(primary)
            guild_ids.insert(0, primary)
        return guild_ids

    def primary_voice_guild_id(self) -> hikari.Snowflake | None:
        primary = hikari.Snowflake(config.DISCORD_GUILD)
        if primary in self._voice_targets:
            return primary
        guild_ids = self.configured_voice_guild_ids()
        return guild_ids[0] if guild_ids else None

    def resolve_voice_target_selection(self, value: str | None) -> hikari.Snowflake | None:
        if value is None:
            return self.primary_voice_guild_id()

        selected = value.strip()
        if not selected:
            return self.primary_voice_guild_id()

        try:
            guild_id = hikari.Snowflake(selected)
        except ValueError as xcp:
            raise LookupError("Unknown voice target.") from xcp

        if guild_id not in self._voice_targets:
            raise LookupError("Unknown voice target.")
        return guild_id

    async def describe_voice_target(self, guild_id: hikari.Snowflakeish) -> str:
        guild = hikari.Snowflake(guild_id)
        if cached := self._voice_target_name_cache.get(guild):
            return cached

        target = self.voice_target(guild)
        if not target:
            raise LookupError(f"Unknown voice target guild: {guild}")

        guild_name: str | None = None
        channel_name: str | None = None

        guild_obj = self.bot.cache.get_guild(guild)
        if guild_obj:
            guild_name = guild_obj.name

        channel_obj = self.bot.cache.get_guild_channel(target.voice_channel)
        if channel_obj:
            channel_name = getattr(channel_obj, "name", None)

        if guild_name is None:
            with contextlib.suppress(Exception):
                guild_obj = await self.bot.rest.fetch_guild(guild)
                guild_name = guild_obj.name

        if channel_name is None:
            with contextlib.suppress(Exception):
                channel_obj = await self.bot.rest.fetch_channel(target.voice_channel)
                channel_name = getattr(channel_obj, "name", None)

        label = f"{guild_name or guild} [{channel_name or target.voice_channel}]"
        self._set_voice_target_label(guild, label)
        return label

    async def voice_target_choice_list(self) -> list[Choice[str]]:
        choices: list[Choice[str]] = []
        for guild_id in self.configured_voice_guild_ids():
            label = await self.describe_voice_target(guild_id)
            choices.append(Choice(self._voice_target_choice_label(label), str(guild_id)))
        return choices

    @staticmethod
    def _voice_target_choice_label(label: str) -> str:
        if len(label) <= 100:
            return label
        return label[:97].rstrip() + "..."

    async def refresh_voice_target_choices(self) -> bool:
        command_cls = getattr(sys.modules.get("cmd_voice"), "CMD_VoiceSay", None)
        if command_cls is None:
            raise RuntimeError("CMD_VoiceSay is not loaded")

        option_data = command_cls._command_data.options["target"]
        choices = await self.voice_target_choice_list()
        current = option_data.choices
        current_pairs = [] if current is hikari.UNDEFINED else [(choice.name, choice.value) for choice in current]
        next_pairs = [(choice.name, choice.value) for choice in choices]
        if current_pairs == next_pairs and not option_data.autocomplete:
            return False

        option_data.choices = choices
        option_data.autocomplete = False
        option_data.autocomplete_provider = hikari.UNDEFINED
        self._voice_target_choices_dirty = True
        return True

    async def sync_voice_target_choices(self, client: lightbulb.Client, *, reason: str) -> bool:
        changed = await self.refresh_voice_target_choices()
        if not changed and not self._voice_target_choices_dirty:
            return False

        try:
            await client.sync_application_commands()
        except Exception as xcp:
            log.warning(f"TTS voice-target command sync failed reason={reason}: {type(xcp).__name__}: {xcp}")
            return False

        self._voice_target_choices_dirty = False
        log.info(f"TTS voice-target command sync complete reason={reason}")
        return True

    async def on_guild_available(self, guild: hikari.GatewayGuild, client: lightbulb.Client) -> bool:
        target = self.voice_target(guild.id)
        if not target:
            return False

        channel = guild.get_channels().get(target.voice_channel)
        channel_name = getattr(channel, "name", None) if channel else None
        label = f"{guild.name} [{channel_name or target.voice_channel}]"
        changed = self._set_voice_target_label(guild.id, label)
        if not changed:
            return False

        return await self.sync_voice_target_choices(client, reason=f"guild_available:{guild.id}")

    def active_voice_connections(self) -> list[hikariwave.VoiceConnection]:
        connections: list[hikariwave.VoiceConnection] = []
        for guild_id in sorted(self._voice_targets, key=int):
            if connection := self._voice_client.get_connection(guild_id=guild_id):
                connections.append(connection)
        return connections

    def active_voice_connection(self, guild_id: hikari.Snowflakeish | None = None) -> hikariwave.VoiceConnection | None:
        if guild_id is not None:
            return self.get_connection(guild_id)

        connections = self.active_voice_connections()
        if len(connections) != 1:
            return None
        return connections[0]

    async def notify_connected_tts_channels(self, content: str) -> int:
        sent_count = 0
        sent_channels: set[hikari.Snowflake] = set()
        for connection in self.active_voice_connections():
            target = self.voice_target(connection.guild_id)
            if target is None:
                continue
            channel_id = target.tts_channel
            if channel_id in sent_channels:
                continue
            try:
                await self.bot.rest.create_message(
                    channel_id,
                    content,
                    flags=hikari.MessageFlag.SUPPRESS_NOTIFICATIONS,
                )
            except Exception:
                log.exception(
                    "Failed to send TTS system notice guild=%s channel=%s",
                    connection.guild_id,
                    int(channel_id),
                )
                continue
            sent_channels.add(channel_id)
            sent_count += 1
        return sent_count

    def _target_voice_channel_id(self, guild_id: hikari.Snowflakeish) -> hikari.Snowflake | None:
        if not (target := self.voice_target(guild_id)):
            return None
        return target.voice_channel

    def _target_voice_listener_count(self, guild_id: hikari.Snowflakeish) -> int:
        channel_id = self._target_voice_channel_id(guild_id)
        if channel_id is None:
            return 0

        return len(cached_voice_channel_occupants(self.bot, guild_id, channel_id))

    def _clear_voice_connect_backoff(self, guild_id: hikari.Snowflakeish) -> None:
        self._voice_connect_backoff.pop(hikari.Snowflake(guild_id), None)

    def _active_voice_connect_backoff(
        self,
        guild_id: hikari.Snowflakeish,
        listener_count: int,
    ) -> VoiceConnectBackoff | None:
        guild = hikari.Snowflake(guild_id)
        backoff = self._voice_connect_backoff.get(guild)
        if backoff is None:
            return None
        if listener_count != backoff.listener_count:
            self._voice_connect_backoff.pop(guild, None)
            return None

        remaining = backoff.retry_at_monotonic - asyncio.get_running_loop().time()
        if remaining <= 0:
            self._voice_connect_backoff.pop(guild, None)
            return None
        return backoff

    def _voice_connect_failure_reason(self, error: Exception) -> tuple[str, float]:
        if isinstance(error, VoiceUdpDiscoveryTimeoutError):
            return "udp_discovery_timeout", self._VOICE_UDP_DISCOVERY_COOLDOWN_SECONDS
        if isinstance(error, VoiceUdpDiscoveryNetworkError):
            return "udp_discovery_network_error", self._VOICE_UDP_DISCOVERY_COOLDOWN_SECONDS
        if isinstance(error, asyncio.TimeoutError):
            return "connect_timeout", self._VOICE_CONNECT_FAILURE_COOLDOWN_SECONDS
        return "connect_failed", self._VOICE_CONNECT_FAILURE_COOLDOWN_SECONDS

    def _record_voice_connect_failure(
        self,
        guild_id: hikari.Snowflakeish,
        listener_count: int,
        error: Exception,
    ) -> None:
        guild = hikari.Snowflake(guild_id)
        reason, cooldown = self._voice_connect_failure_reason(error)
        detail = f"{type(error).__name__}: {error}".strip()
        self._voice_connect_backoff[guild] = VoiceConnectBackoff(
            retry_at_monotonic=asyncio.get_running_loop().time() + cooldown,
            listener_count=listener_count,
            reason=reason,
            detail=detail,
        )
        log.warning(
            f"TTS voice connect backoff scheduled {guild_id=} reason={reason} "
            f"cooldown={cooldown:.1f}s listeners={listener_count} detail={detail}"
        )

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
        if not (target := self.voice_target(event.guild_id)):
            return

        me = self.bot.get_me()
        if me and event.state.user_id == me.id:
            return

        target_channel = target.voice_channel
        old_channel_id = event.old_state.channel_id if event.old_state else None
        new_channel_id = event.state.channel_id

        # Trigger only when someone leaves/moves away from the configured channel.
        if old_channel_id != target_channel or new_channel_id == target_channel:
            return

        connection = self.get_connection(event.guild_id)
        if not connection or connection.channel_id != target_channel:
            return

        occupants = cached_voice_channel_occupants(
            self.bot,
            event.guild_id,
            target_channel,
            exclude_user_id=event.state.user_id if new_channel_id != target_channel else None,
        )
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

    def user_autocorrect_enabled(self, user_id: hikari.Snowflakeish) -> bool:
        return self._user_settings.get(int(user_id), UserVoiceSettings()).autocorrect

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

    def set_user_autocorrect(self, user_id: hikari.Snowflakeish, enabled: bool) -> bool:
        uid = int(user_id)
        settings = self._user_settings.get(uid, UserVoiceSettings())
        settings.autocorrect = enabled
        self._user_settings[uid] = settings
        self._save_user_settings()
        return settings.autocorrect

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

    @staticmethod
    def _substitution_sort_key(item: tuple[str, TextSubstitutionRule]) -> tuple[str, int, str]:
        key, rule = item
        return (key.lower(), 0 if not rule.case_sensitive else 1, key)

    @classmethod
    def _sorted_substitution_map(
        cls,
        substitutions: dict[str, TextSubstitutionRule],
    ) -> dict[str, TextSubstitutionRule]:
        return dict(sorted(substitutions.items(), key=cls._substitution_sort_key))

    @staticmethod
    def _resolve_substitution_storage_key(
        source: str,
        substitutions: dict[str, TextSubstitutionRule],
    ) -> str | None:
        exact = source.strip()
        if exact in substitutions:
            return exact

        lowered = exact.lower()
        rule = substitutions.get(lowered)
        if rule is not None and not rule.case_sensitive:
            return lowered
        return None

    def user_mention_overrides(self, user_id: hikari.Snowflakeish) -> dict[int, str]:
        settings = self._user_settings.get(int(user_id))
        if not settings or not settings.mention_overrides:
            return {}
        return dict(sorted(settings.mention_overrides.items()))

    def global_mention_overrides(self) -> dict[int, str]:
        if not self._text_corrections.mention_overrides:
            return {}
        return dict(sorted(self._text_corrections.mention_overrides.items()))

    def user_text_substitutions(self, user_id: hikari.Snowflakeish) -> dict[str, TextSubstitutionRule]:
        settings = self._user_settings.get(int(user_id))
        if not settings or not settings.substitutions:
            return {}
        return self._sorted_substitution_map(settings.substitutions)

    def user_pronunciations(
        self,
        user_id: hikari.Snowflakeish,
        voice: str | None = None,
    ) -> dict[str, PronunciationOverride]:
        selected_voice = self._normalise_pronunciation_voice(voice) if voice is not None else self.user_voice_variant(user_id)[0]
        merged = self.global_pronunciations(selected_voice)
        overrides = self.user_pronunciation_overrides(user_id, selected_voice)
        merged.update(overrides)
        return dict(sorted(merged.items()))

    def user_pronunciation_overrides(
        self,
        user_id: hikari.Snowflakeish,
        voice: str | None = None,
    ) -> dict[str, PronunciationOverride]:
        settings = self._user_settings.get(int(user_id))
        if not settings or not settings.pronunciations:
            return {}
        selected_voice = self._normalise_pronunciation_voice(voice) if voice is not None else self.user_voice_variant(user_id)[0]
        entries = settings.pronunciations.get(selected_voice)
        if not entries:
            return {}
        return dict(sorted(entries.items()))

    def global_pronunciations(self, voice: str | None = None) -> dict[str, PronunciationOverride]:
        selected_voice = self._normalise_pronunciation_voice(voice) if voice is not None else self.voice
        entries = self._text_corrections.pronunciations.get(selected_voice)
        if not entries:
            return {}
        return dict(sorted(entries.items()))

    def all_global_pronunciations(self) -> dict[str, dict[str, PronunciationOverride]]:
        if not self._text_corrections.pronunciations:
            return {}
        return {
            voice: dict(sorted(entries.items()))
            for voice, entries in sorted(self._text_corrections.pronunciations.items())
        }

    def base_text_substitutions(self) -> dict[str, TextSubstitutionRule]:
        combined = dict(self._text_corrections.slang)
        combined.update(self._text_corrections.typos)
        if not combined:
            return {}
        return self._sorted_substitution_map(combined)

    def global_text_substitutions(self, category: str | None = None) -> dict[str, TextSubstitutionRule]:
        if category is None:
            return self.base_text_substitutions()

        normalised = self._normalise_text_correction_category(category)
        corrections = self._text_correction_map(normalised)
        return self._sorted_substitution_map(corrections)

    def set_user_mention_override(
        self,
        user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
        spoken_name: str,
    ) -> tuple[int, str, bool]:
        uid = int(user_id)
        target_uid = int(target_user_id)
        value = self._normalise_substitution_value(spoken_name)

        settings = self._user_settings.get(uid, UserVoiceSettings())
        mention_overrides = dict(settings.mention_overrides)
        existed = target_uid in mention_overrides
        if not existed and len(mention_overrides) >= self._MAX_SUBSTITUTIONS_PER_USER:
            raise ValueError(f"You can store up to {self._MAX_SUBSTITUTIONS_PER_USER} mention overrides.")

        mention_overrides[target_uid] = value
        settings.mention_overrides = dict(sorted(mention_overrides.items()))
        self._user_settings[uid] = settings
        self._save_user_settings()
        return target_uid, value, existed

    def remove_user_mention_override(
        self,
        user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
    ) -> tuple[int, bool]:
        uid = int(user_id)
        target_uid = int(target_user_id)
        settings = self._user_settings.get(uid)
        if not settings or target_uid not in settings.mention_overrides:
            return target_uid, False

        mention_overrides = dict(settings.mention_overrides)
        del mention_overrides[target_uid]
        settings.mention_overrides = mention_overrides
        self._user_settings[uid] = settings
        self._save_user_settings()
        return target_uid, True

    def set_global_mention_override(
        self,
        target_user_id: hikari.Snowflakeish,
        spoken_name: str,
    ) -> tuple[int, str, bool]:
        target_uid = int(target_user_id)
        value = self._normalise_substitution_value(spoken_name)
        mention_overrides = dict(self._text_corrections.mention_overrides)
        existed = target_uid in mention_overrides
        mention_overrides[target_uid] = value
        self._replace_text_corrections(
            slang=dict(self._text_corrections.slang),
            typos=dict(self._text_corrections.typos),
            pronunciations=self.all_global_pronunciations(),
            mention_overrides=mention_overrides,
            protected=set(self._text_corrections.protected),
        )
        self._save_text_corrections()
        return target_uid, value, existed

    def remove_global_mention_override(self, target_user_id: hikari.Snowflakeish) -> tuple[int, bool]:
        target_uid = int(target_user_id)
        mention_overrides = dict(self._text_corrections.mention_overrides)
        removed = mention_overrides.pop(target_uid, None) is not None
        if removed:
            self._replace_text_corrections(
                slang=dict(self._text_corrections.slang),
                typos=dict(self._text_corrections.typos),
                pronunciations=self.all_global_pronunciations(),
                mention_overrides=mention_overrides,
                protected=set(self._text_corrections.protected),
            )
            self._save_text_corrections()
        return target_uid, removed

    def set_global_text_substitution(
        self,
        category: str,
        source: str,
        target: str,
        *,
        case_sensitive: bool = False,
    ) -> tuple[str, str, TextSubstitutionRule, bool]:
        normalised = self._normalise_text_correction_category(category)
        key = self._normalise_text_correction_key(source, case_sensitive=case_sensitive)
        value = self._normalise_substitution_value(target)
        rule = TextSubstitutionRule(source=key, target=value, case_sensitive=case_sensitive)

        slang = dict(self._text_corrections.slang)
        typos = dict(self._text_corrections.typos)
        target_map = slang if normalised == "slang" else typos
        other_map = typos if normalised == "slang" else slang

        existed = key in target_map
        target_map[key] = rule
        other_map.pop(key, None)
        self._replace_text_corrections(
            slang=self._sorted_substitution_map(slang),
            typos=self._sorted_substitution_map(typos),
            pronunciations=self.all_global_pronunciations(),
            mention_overrides=dict(self._text_corrections.mention_overrides),
            protected=set(self._text_corrections.protected),
        )
        self._save_text_corrections()
        return normalised, key, rule, existed

    def remove_global_text_substitution(self, category: str, source: str) -> tuple[str, str, bool]:
        normalised = self._normalise_text_correction_category(category)

        slang = dict(self._text_corrections.slang)
        typos = dict(self._text_corrections.typos)
        corrections = slang if normalised == "slang" else typos
        key = self._resolve_substitution_storage_key(source, corrections)
        if key is None:
            try:
                key = self._normalise_text_correction_key(source)
            except ValueError:
                key = source.strip()
        removed = corrections.pop(key, None) is not None

        if removed:
            self._replace_text_corrections(
                slang=self._sorted_substitution_map(slang),
                typos=self._sorted_substitution_map(typos),
                pronunciations=self.all_global_pronunciations(),
                mention_overrides=dict(self._text_corrections.mention_overrides),
                protected=set(self._text_corrections.protected),
            )
            self._save_text_corrections()
        return normalised, key, removed

    def global_protected_text_tokens(self) -> list[str]:
        return sorted(self._text_corrections.protected)

    def add_global_protected_text_token(self, source: str) -> tuple[str, bool]:
        key = self._normalise_substitution_key(source)
        protected = set(self._text_corrections.protected)
        existed = key in protected
        protected.add(key)
        self._replace_text_corrections(
            slang=dict(self._text_corrections.slang),
            typos=dict(self._text_corrections.typos),
            pronunciations=self.all_global_pronunciations(),
            mention_overrides=dict(self._text_corrections.mention_overrides),
            protected=protected,
        )
        self._save_text_corrections()
        return key, existed

    def remove_global_protected_text_token(self, source: str) -> tuple[str, bool]:
        key = self._normalise_substitution_key(source)
        protected = set(self._text_corrections.protected)
        removed = key in protected
        protected.discard(key)
        if removed:
            self._replace_text_corrections(
                slang=dict(self._text_corrections.slang),
                typos=dict(self._text_corrections.typos),
                pronunciations=self.all_global_pronunciations(),
                mention_overrides=dict(self._text_corrections.mention_overrides),
                protected=protected,
            )
            self._save_text_corrections()
        return key, removed

    @staticmethod
    def _match_case_insensitive(options: list[str], requested: str) -> str | None:
        requested_lower = requested.lower()
        return next((option for option in options if option.lower() == requested_lower), None)

    def _limit_available_voices(self, voices: list[str], *, preferred: str | None = None) -> list[str]:
        if len(voices) <= self._MAX_AVAILABLE_VOICES:
            return voices

        limited = list(voices[: self._MAX_AVAILABLE_VOICES])
        if preferred:
            match = self._match_case_insensitive(voices, preferred)
            if match and not any(voice.lower() == match.lower() for voice in limited):
                limited[-1] = match
                limited.sort(key=str.lower)
        return limited

    async def _resolve_requested_voice(self, voice: str) -> str:
        requested_voice = voice.strip()
        if not requested_voice:
            raise ValueError("voice must not be empty")

        voices = await self.available_voices(force_refresh=True)
        if not voices:
            return requested_voice

        if match := self._match_case_insensitive(voices, requested_voice):
            return match

        raise LookupError(f"Unknown voice: {requested_voice}")

    async def _resolve_requested_variant(self, voice: str, variant: str) -> str | None:
        requested_variant = self._normalise_variant(variant, allow_empty=False)
        if requested_variant is None:
            return None

        variants = await self._available_variants_for_voice(voice, force_refresh=True)
        if not variants:
            if self._engine_kind == "piper":
                raise LookupError(f"Voice has no variants: {voice}")
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
        self,
        user_id: hikari.Snowflakeish,
        source: str,
        target: str,
        *,
        case_sensitive: bool = False,
    ) -> tuple[str, TextSubstitutionRule, bool]:
        uid = int(user_id)
        key = self._normalise_substitution_key(source, case_sensitive=case_sensitive)
        value = self._normalise_substitution_value(target)
        rule = TextSubstitutionRule(source=key, target=value, case_sensitive=case_sensitive)

        settings = self._user_settings.get(uid, UserVoiceSettings())
        substitutions = dict(settings.substitutions)
        existed = key in substitutions
        if not existed and len(substitutions) >= self._MAX_SUBSTITUTIONS_PER_USER:
            raise ValueError(f"You can store up to {self._MAX_SUBSTITUTIONS_PER_USER} substitutions.")

        substitutions[key] = rule
        settings.substitutions = self._sorted_substitution_map(substitutions)
        self._user_settings[uid] = settings
        self._save_user_settings()
        return key, rule, existed

    def set_user_pronunciation(
        self,
        user_id: hikari.Snowflakeish,
        voice: str,
        source: str,
        target: str,
        format: PronunciationFormat | str,
    ) -> tuple[str, PronunciationOverride, bool]:
        uid = int(user_id)
        voice_key = self._normalise_pronunciation_voice(voice)
        key = self._normalise_substitution_key(source)
        value = PronunciationOverride(
            format=self._normalise_pronunciation_format(format),
            value=self._normalise_substitution_value(target),
        )
        self._validate_pronunciation_override(voice_key, value)

        settings = self._user_settings.get(uid, UserVoiceSettings())
        pronunciations = dict(settings.pronunciations)
        voice_pronunciations = dict(pronunciations.get(voice_key, {}))
        existed = key in voice_pronunciations
        if not existed and len(voice_pronunciations) >= self._MAX_SUBSTITUTIONS_PER_USER:
            raise ValueError(f"You can store up to {self._MAX_SUBSTITUTIONS_PER_USER} pronunciations.")

        voice_pronunciations[key] = value
        pronunciations[voice_key] = dict(sorted(voice_pronunciations.items()))
        settings.pronunciations = dict(sorted(pronunciations.items()))
        self._user_settings[uid] = settings
        self._save_user_settings()
        return key, value, existed

    def remove_user_pronunciation(self, user_id: hikari.Snowflakeish, voice: str, source: str) -> tuple[str, bool]:
        uid = int(user_id)
        voice_key = self._normalise_pronunciation_voice(voice)
        key = self._normalise_substitution_key(source)
        settings = self._user_settings.get(uid)
        if not settings:
            return key, False

        pronunciations = dict(settings.pronunciations)
        voice_pronunciations = dict(pronunciations.get(voice_key, {}))
        if key not in voice_pronunciations:
            return key, False

        del voice_pronunciations[key]
        if voice_pronunciations:
            pronunciations[voice_key] = dict(sorted(voice_pronunciations.items()))
        else:
            pronunciations.pop(voice_key, None)
        settings.pronunciations = pronunciations
        self._user_settings[uid] = settings
        self._save_user_settings()
        return key, True

    def set_global_pronunciation(
        self,
        voice: str,
        source: str,
        target: str,
        format: PronunciationFormat | str,
    ) -> tuple[str, str, PronunciationOverride, bool]:
        voice_key = self._normalise_pronunciation_voice(voice)
        key = self._normalise_substitution_key(source)
        value = PronunciationOverride(
            format=self._normalise_pronunciation_format(format),
            value=self._normalise_substitution_value(target),
        )
        self._validate_pronunciation_override(voice_key, value)

        pronunciations = {
            loaded_voice: dict(entries)
            for loaded_voice, entries in self._text_corrections.pronunciations.items()
        }
        voice_pronunciations = dict(pronunciations.get(voice_key, {}))
        existed = key in voice_pronunciations
        voice_pronunciations[key] = value
        pronunciations[voice_key] = dict(sorted(voice_pronunciations.items()))
        self._replace_text_corrections(
            slang=dict(self._text_corrections.slang),
            typos=dict(self._text_corrections.typos),
            pronunciations=pronunciations,
            mention_overrides=dict(self._text_corrections.mention_overrides),
            protected=set(self._text_corrections.protected),
        )
        self._save_text_corrections()
        return voice_key, key, value, existed

    def remove_global_pronunciation(self, voice: str, source: str) -> tuple[str, str, bool]:
        voice_key = self._normalise_pronunciation_voice(voice)
        key = self._normalise_substitution_key(source)
        pronunciations = {
            loaded_voice: dict(entries)
            for loaded_voice, entries in self._text_corrections.pronunciations.items()
        }
        voice_pronunciations = dict(pronunciations.get(voice_key, {}))
        if key not in voice_pronunciations:
            return voice_key, key, False

        del voice_pronunciations[key]
        if voice_pronunciations:
            pronunciations[voice_key] = dict(sorted(voice_pronunciations.items()))
        else:
            pronunciations.pop(voice_key, None)
        self._replace_text_corrections(
            slang=dict(self._text_corrections.slang),
            typos=dict(self._text_corrections.typos),
            pronunciations=pronunciations,
            mention_overrides=dict(self._text_corrections.mention_overrides),
            protected=set(self._text_corrections.protected),
        )
        self._save_text_corrections()
        return voice_key, key, True

    @staticmethod
    def _normalise_pronunciation_voice(voice: str) -> str:
        value = voice.strip()
        if not value:
            raise ValueError("voice must not be empty")
        return value

    def _validate_pronunciation_override(self, voice: str, value: PronunciationOverride) -> None:
        if value.format is PronunciationFormat.IPA and not self.voice_supports_ipa_pronunciations(voice):
            raise ValueError(f"IPA pronunciations are not supported for voice `{voice}`.")

    def _load_pronunciation_entry(self, raw: object) -> PronunciationOverride | None:
        if isinstance(raw, str):
            try:
                value = self._normalise_substitution_value(raw)
            except ValueError:
                return None
            return PronunciationOverride(format=PronunciationFormat.TEXT, value=value)

        if not isinstance(raw, dict):
            return None

        format_raw = raw.get("format", raw.get("type"))
        value_raw = raw.get("value")
        if not isinstance(format_raw, str) or not isinstance(value_raw, str):
            return None

        try:
            format_value = self._normalise_pronunciation_format(format_raw)
            value = self._normalise_substitution_value(value_raw)
        except ValueError:
            return None
        return PronunciationOverride(format=format_value, value=value)

    def _load_pronunciation_entries(self, raw: object) -> dict[str, PronunciationOverride]:
        if not isinstance(raw, dict):
            return {}

        pronunciations: dict[str, PronunciationOverride] = {}
        for source, target in raw.items():
            if not isinstance(source, str):
                continue
            entry = self._load_pronunciation_entry(target)
            if entry is None:
                continue
            try:
                key = self._normalise_substitution_key(source)
            except ValueError:
                continue
            pronunciations[key] = entry

        return dict(sorted(pronunciations.items()))

    def _load_pronunciation_voice_map(
        self,
        raw: object,
        *,
        legacy_voice: str | None = None,
    ) -> dict[str, dict[str, PronunciationOverride]]:
        if not isinstance(raw, dict):
            return {}

        pronunciations: dict[str, dict[str, PronunciationOverride]] = {}
        if legacy_voice is not None and raw and all(not isinstance(target, dict) for target in raw.values()):
            legacy_entries = self._load_pronunciation_entries(raw)
            if legacy_entries:
                pronunciations[legacy_voice] = legacy_entries
            return dict(sorted(pronunciations.items()))

        for pronunciation_voice, voice_entries_raw in raw.items():
            if not isinstance(pronunciation_voice, str):
                continue
            try:
                voice_key = self._normalise_pronunciation_voice(pronunciation_voice)
            except ValueError:
                continue
            voice_entries = self._load_pronunciation_entries(voice_entries_raw)
            if voice_entries:
                pronunciations[voice_key] = voice_entries

        return dict(sorted(pronunciations.items()))

    def remove_user_text_substitution(self, user_id: hikari.Snowflakeish, source: str) -> tuple[str, bool]:
        uid = int(user_id)
        settings = self._user_settings.get(uid)
        if not settings:
            try:
                key = self._normalise_substitution_key(source)
            except ValueError:
                key = source.strip()
            return key, False

        key = self._resolve_substitution_storage_key(source, settings.substitutions)
        if key is None:
            try:
                key = self._normalise_substitution_key(source)
            except ValueError:
                key = source.strip()
            return key, False

        substitutions = dict(settings.substitutions)
        del substitutions[key]
        settings.substitutions = substitutions
        self._user_settings[uid] = settings
        self._save_user_settings()
        return key, True

    def _load_substitution_rule(
        self,
        source: str,
        raw: object,
        *,
        allow_symbols: bool,
    ) -> tuple[str, TextSubstitutionRule] | None:
        case_sensitive = False
        target_raw = raw
        if isinstance(raw, dict):
            target_raw = raw.get("value")
            case_sensitive = bool(raw.get("case_sensitive"))

        if not isinstance(target_raw, str):
            return None

        try:
            key = (
                self._normalise_text_correction_key(source, case_sensitive=case_sensitive)
                if allow_symbols
                else self._normalise_substitution_key(source, case_sensitive=case_sensitive)
            )
            value = self._normalise_substitution_value(target_raw)
        except ValueError:
            return None
        return key, TextSubstitutionRule(source=key, target=value, case_sensitive=case_sensitive)

    def _load_substitution_map(
        self,
        raw: object,
        *,
        allow_symbols: bool,
    ) -> dict[str, TextSubstitutionRule]:
        if not isinstance(raw, dict):
            return {}

        substitutions: dict[str, TextSubstitutionRule] = {}
        for source, target in raw.items():
            if not isinstance(source, str):
                continue
            loaded = self._load_substitution_rule(source, target, allow_symbols=allow_symbols)
            if loaded is None:
                continue
            key, rule = loaded
            substitutions[key] = rule
        return self._sorted_substitution_map(substitutions)

    def _load_mention_override_map(self, raw: object) -> dict[int, str]:
        if not isinstance(raw, dict):
            return {}

        mention_overrides: dict[int, str] = {}
        for user_id, spoken_name in raw.items():
            if not isinstance(spoken_name, str):
                continue
            try:
                target_uid = int(str(user_id).strip())
                mention_overrides[target_uid] = self._normalise_substitution_value(spoken_name)
            except (TypeError, ValueError):
                continue
        return dict(sorted(mention_overrides.items()))

    @staticmethod
    def _serialise_substitution_map(substitutions: dict[str, TextSubstitutionRule]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for source, rule in substitutions.items():
            payload[source] = (
                {
                    "value": rule.target,
                    "case_sensitive": True,
                }
                if rule.case_sensitive
                else rule.target
            )
        return payload

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
            autocorrect = bool(values.get("autocorrect", True))
            voice = values.get("voice")
            variant = values.get("variant")
            pronunciations_raw = values.get("pronunciations")
            mention_overrides_raw = values.get("mention_overrides")
            substitutions_raw = values.get("substitutions")

            selected_voice = voice.strip() if isinstance(voice, str) and voice.strip() else None
            selected_variant: str | None = None
            if isinstance(variant, str):
                try:
                    selected_variant = self._normalise_variant(variant)
                except ValueError:
                    selected_variant = None

            legacy_voice = selected_voice or self.voice
            pronunciations = self._load_pronunciation_voice_map(pronunciations_raw, legacy_voice=legacy_voice)
            mention_overrides = self._load_mention_override_map(mention_overrides_raw)
            substitutions = self._load_substitution_map(substitutions_raw, allow_symbols=False)

            self._user_settings[uid] = UserVoiceSettings(
                enabled=enabled,
                autocorrect=autocorrect,
                voice=selected_voice,
                variant=selected_variant,
                pronunciations=dict(sorted(pronunciations.items())),
                mention_overrides=mention_overrides,
                substitutions=substitutions,
            )

    def _save_user_settings(self):
        users: dict[str, dict[str, object]] = {}
        for uid, settings in self._user_settings.items():
            users[str(uid)] = {
                "enabled": settings.enabled,
                "autocorrect": settings.autocorrect,
                "voice": settings.voice,
                "variant": settings.variant,
                "pronunciations": {
                    voice: {
                        source: {
                            "format": entry.format.value,
                            "value": entry.value,
                        }
                        for source, entry in sorted(entries.items())
                    }
                    for voice, entries in sorted(settings.pronunciations.items())
                },
                "mention_overrides": {
                    str(target_uid): spoken_name
                    for target_uid, spoken_name in sorted(settings.mention_overrides.items())
                },
                "substitutions": self._serialise_substitution_map(settings.substitutions),
            }

        payload = {"users": users}
        try:
            self._users_path.write_text(json.dumps(payload, indent=4), config.STR_ENCODE)
        except OSError as xcp:
            log.warning(f"TTS user settings write failed path={self._users_path!s}: {type(xcp).__name__}: {xcp}")

    def _load_text_corrections(self) -> TextCorrectionCatalog:
        if not self._corrections_path.exists():
            log.warning(f"TTS correction file not found path={self._corrections_path!s}; typo correction disabled")
            return TextCorrectionCatalog()

        try:
            raw = json.loads(self._corrections_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(f"TTS correction file read failed path={self._corrections_path!s}: {type(xcp).__name__}: {xcp}")
            return TextCorrectionCatalog()

        if not isinstance(raw, dict):
            log.warning(f"TTS correction file invalid path={self._corrections_path!s}: expected a JSON object")
            return TextCorrectionCatalog()

        slang_raw: object = {}
        typos_raw: object = {}
        pronunciations_raw: object = {}
        mention_overrides_raw: object = {}
        protected_raw: object = ()
        legacy_mode = all(isinstance(target, str) for target in raw.values())
        if legacy_mode:
            typos_raw = raw
        else:
            slang_raw = raw.get("slang", {})
            typos_raw = raw.get("typos", {})
            pronunciations_raw = raw.get("pronunciations", {})
            mention_overrides_raw = raw.get("mention_overrides", {})
            protected_raw = raw.get("protected", ())

        slang = self._load_substitution_map(slang_raw, allow_symbols=True)
        typos = self._load_substitution_map(typos_raw, allow_symbols=True)
        pronunciations = self._load_pronunciation_voice_map(pronunciations_raw)
        mention_overrides = self._load_mention_override_map(mention_overrides_raw)
        protected = self._load_protected_text_tokens(protected_raw)
        fuzzy_targets = self._build_fuzzy_targets((slang, typos), protected)
        skipped = 0
        if isinstance(slang_raw, dict):
            skipped += len(slang_raw) - len(slang)
        if isinstance(typos_raw, dict):
            skipped += len(typos_raw) - len(typos)
        if isinstance(pronunciations_raw, dict):
            skipped += sum(
                1
                for voice, entries in pronunciations_raw.items()
                if not isinstance(voice, str) or not isinstance(entries, dict)
            )
        if isinstance(mention_overrides_raw, dict):
            skipped += len(mention_overrides_raw) - len(mention_overrides)
        if isinstance(protected_raw, list):
            skipped += len(protected_raw) - len(protected)

        catalog = TextCorrectionCatalog(
            slang=slang,
            typos=typos,
            pronunciations=pronunciations,
            mention_overrides=mention_overrides,
            protected=frozenset(sorted(protected)),
            fuzzy_targets=fuzzy_targets,
        )
        mode = "legacy-map" if legacy_mode else "sectioned"
        log_message = (
            f"TTS correction file loaded path={self._corrections_path!s}: mode={mode} "
            f"slang={len(catalog.slang)} typos={len(catalog.typos)} pronunciations={len(catalog.pronunciations)} "
            f"mentions={len(catalog.mention_overrides)} protected={len(catalog.protected)} "
            f"fuzzy_targets={len(catalog.fuzzy_targets)}"
        )
        if skipped:
            log.warning(f"{log_message} skipped={skipped}")
        else:
            tts_log.info(log_message)
        return catalog

    def _load_protected_text_tokens(self, raw: object) -> set[str]:
        if not isinstance(raw, list):
            return set()

        protected: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            try:
                protected.add(self._normalise_substitution_key(item))
            except ValueError:
                continue
        return protected

    @staticmethod
    def _build_fuzzy_targets(
        correction_maps: Iterable[dict[str, TextSubstitutionRule]],
        protected: set[str],
    ) -> tuple[str, ...]:
        candidates: set[str] = set()
        for corrections in correction_maps:
            for rule in corrections.values():
                target_word = rule.target.strip().lower()
                if re.fullmatch(r"[a-z]+", target_word):
                    candidates.add(target_word)
        candidates.difference_update(protected)
        return tuple(sorted(candidates))

    @classmethod
    def _normalise_text_correction_category(cls, category: str) -> str:
        key = category.strip().lower()
        normalised = cls._TEXT_CORRECTION_CATEGORY_ALIASES.get(key)
        if normalised is None:
            raise ValueError("category must be `slang` or `typo`")
        return normalised

    def _normalise_text_correction_key(self, source: str, *, case_sensitive: bool = False) -> str:
        key = source.strip()
        if not key:
            raise ValueError("source must not be empty")
        if not case_sensitive:
            key = key.lower()
        max_chars = getattr(self, "_MAX_SUBSTITUTION_KEY_CHARS", 40)
        if len(key) > max_chars:
            raise ValueError(f"source is too long (max {max_chars} chars)")
        if any(char.isspace() for char in key):
            raise ValueError("source must be a single token without whitespace")
        return key

    def _text_correction_map(self, category: str) -> dict[str, TextSubstitutionRule]:
        if category == "slang":
            return self._text_corrections.slang
        if category == "typos":
            return self._text_corrections.typos
        raise ValueError("category must be `slang` or `typo`")

    def _replace_text_corrections(
        self,
        *,
        slang: dict[str, TextSubstitutionRule],
        typos: dict[str, TextSubstitutionRule],
        pronunciations: dict[str, dict[str, PronunciationOverride]],
        mention_overrides: dict[int, str],
        protected: set[str],
    ) -> None:
        self._text_corrections = TextCorrectionCatalog(
            slang=self._sorted_substitution_map(slang),
            typos=self._sorted_substitution_map(typos),
            pronunciations={
                voice: dict(sorted(entries.items()))
                for voice, entries in sorted(pronunciations.items())
            },
            mention_overrides=dict(sorted(mention_overrides.items())),
            protected=frozenset(sorted(protected)),
            fuzzy_targets=self._build_fuzzy_targets((slang, typos), protected),
        )

    def _save_text_corrections(self) -> None:
        payload = {
            "slang": self._serialise_substitution_map(self._text_corrections.slang),
            "typos": self._serialise_substitution_map(self._text_corrections.typos),
            "pronunciations": {
                voice: {
                    source: {
                        "format": entry.format.value,
                        "value": entry.value,
                    }
                    for source, entry in sorted(entries.items())
                }
                for voice, entries in sorted(self._text_corrections.pronunciations.items())
            },
            "mention_overrides": {
                str(target_uid): spoken_name
                for target_uid, spoken_name in sorted(self._text_corrections.mention_overrides.items())
            },
            "protected": sorted(self._text_corrections.protected),
        }
        try:
            self._corrections_path.write_text(
                json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
                config.STR_ENCODE,
            )
        except OSError as xcp:
            log.warning(
                f"TTS correction file write failed path={self._corrections_path!s}: {type(xcp).__name__}: {xcp}"
            )

    @staticmethod
    def _path_mtime_ns(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def _refresh_voice_link_rules_if_needed(self) -> None:
        current_mtime_ns = self._path_mtime_ns(self._voice_link_rules_path)
        if (
            current_mtime_ns == self._voice_link_rules_mtime_ns
            and current_mtime_ns is not None
        ):
            return

        self._voice_link_rules = self._load_voice_link_rules()
        self._voice_link_rules_mtime_ns = self._path_mtime_ns(self._voice_link_rules_path)

    def _load_voice_link_rules(self) -> VoiceLinkRules:
        self._ensure_voice_link_rules_file()
        if not self._voice_link_rules_path.exists():
            return VoiceLinkRules()

        try:
            raw = json.loads(self._voice_link_rules_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(
                f"TTS link rule file read failed path={self._voice_link_rules_path!s}: {type(xcp).__name__}: {xcp}"
            )
            return VoiceLinkRules()

        if not isinstance(raw, dict):
            log.warning(f"TTS link rule file invalid path={self._voice_link_rules_path!s}: expected a JSON object")
            return VoiceLinkRules()

        host_labels = self._load_voice_link_host_labels(raw.get("hosts"))
        rules = self._load_voice_link_rule_list(raw.get("rules"))
        tts_log.info(
            f"TTS link rule file loaded path={self._voice_link_rules_path!s}: "
            f"hosts={len(host_labels)} rules={len(rules)}"
        )
        return VoiceLinkRules(host_labels=host_labels, rules=rules)

    def _ensure_voice_link_rules_file(self) -> None:
        if self._voice_link_rules_path.exists():
            return

        payload = {
            "hosts": {
                "giphy.com": "gif",
                "klipy.com": "gif",
                "tenor.com": "gif",
                "www.giphy.com": "gif",
                "www.klipy.com": "gif",
                "www.tenor.com": "gif",
            },
            "rules": [
                {
                    "host": "store.steampowered.com",
                    "path_regex": r"^/(?:agecheck/)?app/\d+/(?P<title>[^/?#]+)",
                    "template": "link steam store {title_norm}",
                }
            ],
        }
        try:
            self._voice_link_rules_path.write_text(
                json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
                config.STR_ENCODE,
            )
        except OSError as xcp:
            log.warning(
                f"TTS link rule file write failed path={self._voice_link_rules_path!s}: {type(xcp).__name__}: {xcp}"
            )

    def _modmux_client(self) -> Muxer:
        if self._modmux is not None:
            return self._modmux

        creds = self._modmux_creds_from_env()
        self._modmux = Muxer(creds=creds)
        return self._modmux

    async def _mod_link_name(self, url: str) -> str | None:
        if url in self._mod_link_name_cache:
            return self._mod_link_name_cache[url]

        try:
            mux = self._modmux_client()
            mod = await mux.get_mod_from_url(url)
        except (ModMuxError, ValueError) as xcp:
            log.debug(f"TTS mod link lookup skipped url={url!r}: {type(xcp).__name__}: {xcp}")
            self._mod_link_name_cache[url] = None
            return None

        name = str(mod.name).strip()
        result = name or None
        self._mod_link_name_cache[url] = result
        return result

    def _invalidate_mod_link_name_cache(self) -> None:
        self._mod_link_name_cache.clear()

    def _modmux_creds_from_env(self) -> list[ProviderCreds]:
        creds: list[ProviderCreds] = []

        if secret := self._env_secret("MODRINTH_API_KEY"):
            creds.append(ModrinthCreds(api_key=secret))
        if secret := self._env_secret("CURSEFORGE_API_KEY"):
            creds.append(CurseforgeCreds(api_key=secret))
        if secret := self._env_secret("NEXUSMODS_API_KEY"):
            creds.append(NexusCreds(token=secret))
        if secret := self._env_secret("WUBE_API_KEY"):
            creds.append(WubeCreds(api_key=secret))

        modio_api_key = self._env_secret("MODIO_API_KEY")
        modio_user_id = self._env_secret("MODIO_USER_ID")
        if modio_api_key and modio_user_id:
            creds.append(ModioCreds(api_key=modio_api_key, user_id=modio_user_id))
        elif modio_api_key or modio_user_id:
            log.warning("TTS mod.io link lookup disabled: both MODIO_API_KEY and MODIO_USER_ID are required")

        if secret := self._env_secret("STEAM_WEB_API_KEY"):
            creds.append(SteamCreds(api_key=secret))

        return creds

    @staticmethod
    def _env_secret(name: str) -> SecretStr | None:
        value = (config.env_opt(name) or "").strip()
        return SecretStr(value) if value else None

    def _load_voice_link_host_labels(self, raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}

        host_labels: dict[str, str] = {}
        for host, label in raw.items():
            if not isinstance(host, str) or not isinstance(label, str):
                continue
            normalised_host = host.strip().lower()
            normalised_label = label.strip()
            if not normalised_host or not normalised_label:
                continue
            host_labels[normalised_host] = normalised_label
        return dict(sorted(host_labels.items()))

    def _load_voice_link_rule_list(self, raw: object) -> tuple[VoiceLinkRule, ...]:
        if not isinstance(raw, list):
            return ()

        loaded_rules: list[VoiceLinkRule] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                loaded_rules.append(self._load_voice_link_rule(item))
            except ValueError as xcp:
                log.warning(f"TTS link rule invalid path={self._voice_link_rules_path!s}: {xcp}")

        return tuple(loaded_rules)

    def voice_link_host_labels(self) -> dict[str, str]:
        self._refresh_voice_link_rules_if_needed()
        return dict(self._voice_link_rules.host_labels)

    def voice_link_rules(self) -> tuple[VoiceLinkRule, ...]:
        self._refresh_voice_link_rules_if_needed()
        return self._voice_link_rules.rules

    def set_voice_link_host_label(self, host: str, label: str) -> tuple[str, str, bool]:
        self._refresh_voice_link_rules_if_needed()
        host_key = self._normalise_voice_link_host(host)
        label_value = self._normalise_voice_link_label(label)
        host_labels = dict(self._voice_link_rules.host_labels)
        existed = host_key in host_labels
        host_labels[host_key] = label_value
        self._replace_voice_link_rules(host_labels=host_labels, rules=self._voice_link_rules.rules)
        self._save_voice_link_rules()
        return host_key, label_value, existed

    def remove_voice_link_host_label(self, host: str) -> tuple[str, bool]:
        self._refresh_voice_link_rules_if_needed()
        host_key = self._normalise_voice_link_host(host)
        host_labels = dict(self._voice_link_rules.host_labels)
        removed = host_labels.pop(host_key, None) is not None
        if removed:
            self._replace_voice_link_rules(host_labels=host_labels, rules=self._voice_link_rules.rules)
            self._save_voice_link_rules()
        return host_key, removed

    def add_voice_link_rule(
        self,
        host: str,
        path_regex: str,
        template: str,
        *,
        mode: VoiceLinkRuleMode | str = VoiceLinkRuleMode.REGEX,
        example_url: str | None = None,
    ) -> tuple[int, VoiceLinkRule]:
        self._refresh_voice_link_rules_if_needed()
        rule = self._build_voice_link_rule_from_input(
            host=host,
            path_value=path_regex,
            template=template,
            mode=mode,
            example_url=example_url,
        )
        rules = [*self._voice_link_rules.rules, rule]
        self._replace_voice_link_rules(host_labels=self._voice_link_rules.host_labels, rules=rules)
        self._save_voice_link_rules()
        return len(rules), rule

    def update_voice_link_rule(
        self,
        index: int,
        *,
        host: str | None = None,
        path_regex: str | None = None,
        template: str | None = None,
        mode: VoiceLinkRuleMode | str | None = None,
        example_url: str | None = None,
    ) -> tuple[int, VoiceLinkRule]:
        self._refresh_voice_link_rules_if_needed()
        position = self._normalise_voice_link_rule_index(index)
        existing = self._voice_link_rules.rules[position - 1]
        rule = self._build_voice_link_rule_from_input(
            host=host if host is not None else existing.host,
            path_value=path_regex if path_regex is not None else existing.input_pattern,
            template=template if template is not None else existing.template,
            mode=mode if mode is not None else existing.mode,
            example_url=example_url if example_url is not None else existing.example_url,
        )
        rules = list(self._voice_link_rules.rules)
        rules[position - 1] = rule
        self._replace_voice_link_rules(host_labels=self._voice_link_rules.host_labels, rules=rules)
        self._save_voice_link_rules()
        return position, rule

    def remove_voice_link_rule(self, index: int) -> tuple[int, VoiceLinkRule]:
        self._refresh_voice_link_rules_if_needed()
        position = self._normalise_voice_link_rule_index(index)
        rules = list(self._voice_link_rules.rules)
        removed = rules.pop(position - 1)
        self._replace_voice_link_rules(host_labels=self._voice_link_rules.host_labels, rules=rules)
        self._save_voice_link_rules()
        return position, removed

    def _replace_voice_link_rules(
        self,
        *,
        host_labels: dict[str, str],
        rules: Iterable[VoiceLinkRule],
    ) -> None:
        self._voice_link_rules = VoiceLinkRules(
            host_labels=dict(sorted(host_labels.items())),
            rules=tuple(rules),
        )

    def _save_voice_link_rules(self) -> None:
        payload = {
            "hosts": dict(sorted(self._voice_link_rules.host_labels.items())),
            "rules": [self._serialise_voice_link_rule(rule) for rule in self._voice_link_rules.rules],
        }
        try:
            self._voice_link_rules_path.write_text(
                json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
                config.STR_ENCODE,
            )
        except OSError as xcp:
            log.warning(
                f"TTS link rule file write failed path={self._voice_link_rules_path!s}: {type(xcp).__name__}: {xcp}"
            )
            return

        self._voice_link_rules_mtime_ns = self._path_mtime_ns(self._voice_link_rules_path)

    def preview_voice_link_rule(self, rule: VoiceLinkRule, url: str | None = None) -> str | None:
        preview_url = self._normalise_voice_link_example_url(rule.example_url if url is None else url)
        if preview_url is None:
            return None

        parsed = urlparse(preview_url if "://" in preview_url else f"https://{preview_url}")
        hostname = parsed.hostname
        if hostname is None:
            return None
        if rule.host not in self._voice_link_host_candidates(hostname):
            return None
        match = rule.path_pattern.search(parsed.path)
        if match is None:
            return None
        return self._render_link_rule_template(rule.template, rule.host, match)

    def build_voice_link_rule_draft(self, example_url: str, mode: VoiceLinkRuleMode | str) -> VoiceLinkRuleDraft:
        example_url_value = self._normalise_voice_link_example_url(example_url)
        if example_url_value is None:
            raise ValueError("Example URL must look like `https://example.com/path`.")

        mode_value = self._normalise_voice_link_mode(mode)
        host = self._host_from_voice_link_example_url(example_url_value)
        if host is None:
            raise ValueError("Example URL must include a hostname.")
        path_shape = self._derive_voice_link_shape_from_example_url(example_url_value)
        compiled_path_regex = self._compile_voice_link_shape(path_shape)
        input_pattern = path_shape if mode_value is VoiceLinkRuleMode.SIMPLE else compiled_path_regex
        path_pattern = re.compile(compiled_path_regex)
        template = self._default_voice_link_rule_template(path_pattern)
        return VoiceLinkRuleDraft(
            mode=mode_value,
            host=host,
            example_url=example_url_value,
            input_pattern=input_pattern,
            compiled_path_regex=compiled_path_regex,
            template=template,
        )

    def _load_voice_link_rule(self, item: dict[str, object]) -> VoiceLinkRule:
        host = item.get("host")
        template = item.get("template")
        if not isinstance(host, str) or not isinstance(template, str):
            raise ValueError("rule is missing a host or template string")

        mode = self._load_voice_link_rule_mode(item)
        example_url = item.get("example_url")
        if isinstance(example_url, str):
            try:
                example_url = self._normalise_voice_link_example_url(example_url)
            except ValueError:
                example_url = None
        else:
            example_url = None

        if mode is VoiceLinkRuleMode.SIMPLE:
            path_shape = item.get("path_shape")
            if not isinstance(path_shape, str):
                raise ValueError("simple link rule is missing `path_shape`")
            spec = VoiceLinkSimpleRuleSpec(
                host=self._normalise_voice_link_host(host),
                path_shape=self._normalise_voice_link_shape(path_shape),
                template=self._normalise_voice_link_template(template),
                example_url=example_url,
            )
            return self._compile_voice_link_rule(spec)

        path_regex = item.get("path_regex")
        if not isinstance(path_regex, str):
            raise ValueError("regex link rule is missing `path_regex`")
        spec = VoiceLinkRegexRuleSpec(
            host=self._normalise_voice_link_host(host),
            path_regex=self._normalise_voice_link_regex(path_regex),
            template=self._normalise_voice_link_template(template),
            example_url=example_url,
        )
        return self._compile_voice_link_rule(spec)

    def _load_voice_link_rule_mode(self, item: dict[str, object]) -> VoiceLinkRuleMode:
        raw_mode = item.get("mode")
        if isinstance(raw_mode, str):
            try:
                return self._normalise_voice_link_mode(raw_mode)
            except ValueError:
                pass
        if isinstance(item.get("path_shape"), str):
            return VoiceLinkRuleMode.SIMPLE
        return VoiceLinkRuleMode.REGEX

    def _build_voice_link_rule_from_input(
        self,
        *,
        host: str,
        path_value: str,
        template: str,
        mode: VoiceLinkRuleMode | str,
        example_url: str | None = None,
    ) -> VoiceLinkRule:
        mode_value = self._normalise_voice_link_mode(mode)
        example_url_value = self._normalise_voice_link_example_url(example_url)
        derived_host = self._host_from_voice_link_example_url(example_url_value)
        host_value = self._normalise_voice_link_host(host if host.strip() else (derived_host or ""))
        template_value = self._normalise_voice_link_template(template)
        if mode_value is VoiceLinkRuleMode.SIMPLE:
            path_shape = path_value.strip() or self._derive_voice_link_shape_from_example_url(example_url_value)
            spec: VoiceLinkRegexRuleSpec | VoiceLinkSimpleRuleSpec = VoiceLinkSimpleRuleSpec(
                host=host_value,
                path_shape=self._normalise_voice_link_shape(path_shape),
                template=template_value,
                example_url=example_url_value,
            )
        else:
            spec = VoiceLinkRegexRuleSpec(
                host=host_value,
                path_regex=self._normalise_voice_link_regex(path_value),
                template=template_value,
                example_url=example_url_value,
            )
        rule = self._compile_voice_link_rule(spec)
        if example_url_value is not None and self.preview_voice_link_rule(rule, example_url_value) is None:
            raise ValueError("example_url does not match the rule")
        return rule

    def _compile_voice_link_rule(self, spec: VoiceLinkRegexRuleSpec | VoiceLinkSimpleRuleSpec) -> VoiceLinkRule:
        if isinstance(spec, VoiceLinkSimpleRuleSpec):
            regex_value = self._compile_voice_link_shape(spec.path_shape)
        else:
            regex_value = spec.path_regex
        try:
            path_pattern = re.compile(regex_value)
        except re.error as xcp:
            raise ValueError(f"path_regex is invalid: {xcp}") from xcp
        self._validate_voice_link_template(spec.template, path_pattern)
        return VoiceLinkRule(spec=spec, compiled_path_regex=regex_value, path_pattern=path_pattern)

    @staticmethod
    def _default_voice_link_rule_template(path_pattern: re.Pattern[str]) -> str:
        group_names = tuple(path_pattern.groupindex)
        if "title" in path_pattern.groupindex:
            return "link {host} {title_norm}"
        if group_names:
            return f"link {{host}} {{{group_names[0]}_norm}}"
        return "link {host}"

    @staticmethod
    def _normalise_voice_link_label(label: str) -> str:
        value = re.sub(r"\s+", " ", label).strip()
        if not value:
            raise ValueError("label must not be empty")
        return value

    @staticmethod
    def _normalise_voice_link_regex(path_regex: str) -> str:
        value = path_regex.strip()
        if not value:
            raise ValueError("path_regex must not be empty")
        return value

    @staticmethod
    def _normalise_voice_link_mode(mode: VoiceLinkRuleMode | str) -> VoiceLinkRuleMode:
        if isinstance(mode, VoiceLinkRuleMode):
            return mode
        value = mode.strip().lower()
        aliases = {
            "s": VoiceLinkRuleMode.SIMPLE,
            "simple": VoiceLinkRuleMode.SIMPLE,
            "r": VoiceLinkRuleMode.REGEX,
            "regex": VoiceLinkRuleMode.REGEX,
        }
        if value in aliases:
            return aliases[value]
        raise ValueError("Mode must be `simple` or `regex`.")

    @staticmethod
    def _normalise_voice_link_example_url(example_url: str | None) -> str | None:
        if example_url is None:
            return None
        value = example_url.strip()
        if not value:
            return None
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.hostname is None:
            raise ValueError("Example URL must look like `https://example.com/path`.")
        if parsed.path in ("", "/"):
            raise ValueError("Example URL must include a path.")
        return value

    @staticmethod
    def _normalise_voice_link_shape(path_shape: str) -> str:
        value = path_shape.strip()
        if not value:
            raise ValueError("path_shape must not be empty")
        if any(char.isspace() for char in value):
            raise ValueError("path_shape must not contain whitespace")
        if not value.startswith("/"):
            raise ValueError("path_shape must start with `/`")
        if value != "/" and value.endswith("/"):
            value = value.rstrip("/")
        return value

    @staticmethod
    def _normalise_voice_link_template(template: str) -> str:
        value = re.sub(r"\s+", " ", template).strip()
        if not value:
            raise ValueError("template must not be empty")
        return value

    def _normalise_voice_link_host(self, host: str) -> str:
        value = host.strip().lower()
        if not value:
            raise ValueError("host must not be empty")
        if any(char.isspace() for char in value):
            raise ValueError("host must not contain whitespace")

        parsed = urlparse(value if "://" in value else f"https://{value}")
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError("host must be a hostname like `example.com`")
        if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("host must not include a path, query, or fragment")
        return hostname.lower()

    def _normalise_voice_link_rule_index(self, index: int) -> int:
        self._refresh_voice_link_rules_if_needed()
        if index <= 0:
            raise ValueError("index must be 1 or greater")
        if index > len(self._voice_link_rules.rules):
            raise ValueError(f"index must be between 1 and {len(self._voice_link_rules.rules)}")
        return index

    @staticmethod
    def _voice_link_host_candidates(hostname: str) -> tuple[str, ...]:
        normalised_host = hostname.lower()
        if normalised_host.startswith("www."):
            return (normalised_host, normalised_host.removeprefix("www."))
        return (normalised_host,)

    @classmethod
    def _render_link_rule_template(cls, template: str, hostname: str, match: re.Match[str]) -> str | None:
        values: dict[str, str] = {"host": cls._spoken_link_host(hostname)}
        for key, value in match.groupdict().items():
            decoded = unquote(value).strip() if value is not None else ""
            normalised = cls._normalise_link_template_value(decoded)
            values[key] = decoded
            values[f"{key}_norm"] = normalised
            values[f"{key}_words"] = normalised

        try:
            rendered = template.format_map(values)
        except KeyError, ValueError:
            return None
        return re.sub(r"\s+", " ", rendered).strip() or None

    @staticmethod
    def _normalise_link_template_value(value: str) -> str:
        cleaned = value.strip().strip("/")
        cleaned = re.sub(r"[_\-]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @classmethod
    def _spoken_link_host(cls, hostname: str) -> str:
        normalised_host = hostname.lower()
        if normalised_host.startswith("www."):
            normalised_host = normalised_host.removeprefix("www.")
        aliases = getattr(cls, "_SPOKEN_LINK_HOST_ALIASES", {})
        alias = aliases.get(normalised_host)
        if alias is not None:
            return alias
        if normalised_host.endswith(".com"):
            root = normalised_host[: -len(".com")]
            if "." not in root:
                return root
        return normalised_host

    @staticmethod
    def _host_from_voice_link_example_url(example_url: str | None) -> str | None:
        if example_url is None:
            return None
        parsed = urlparse(example_url if "://" in example_url else f"https://{example_url}")
        return parsed.hostname.lower() if parsed.hostname is not None else None

    @staticmethod
    def _derive_voice_link_shape_from_example_url(example_url: str | None) -> str:
        if example_url is None:
            raise ValueError("Path pattern must not be empty.")
        parsed = urlparse(example_url if "://" in example_url else f"https://{example_url}")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            raise ValueError("Example URL must include a path to derive a simple rule.")

        derived_segments: list[str] = []
        used_names: set[str] = set()
        id_count = 0
        for index, segment in enumerate(segments):
            decoded = unquote(segment)
            if re.fullmatch(r"\d+", decoded):
                id_count += 1
                name = "id" if id_count == 1 else f"id{id_count}"
                used_names.add(name)
                derived_segments.append(f"{{{name}}}")
                continue
            if index == len(segments) - 1:
                suffix = 1
                name = "title"
                while name in used_names:
                    suffix += 1
                    name = f"title{suffix}"
                used_names.add(name)
                derived_segments.append(f"{{{name}}}")
                continue
            derived_segments.append(segment)
        return "/" + "/".join(derived_segments)

    @staticmethod
    def _compile_voice_link_shape(path_shape: str) -> str:
        value = VoiceTTSCoreMixin._normalise_voice_link_shape(path_shape)
        if value == "/":
            return r"^/$"

        placeholder_re = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
        parts: list[str] = ["^"]
        used_names: set[str] = set()
        cursor = 0
        for match in placeholder_re.finditer(value):
            literal = value[cursor : match.start()]
            if "{" in literal or "}" in literal:
                raise ValueError("path_shape contains invalid braces")
            parts.append(re.escape(literal))
            name = match.group(1)
            if name in used_names:
                raise ValueError(f"path_shape capture `{name}` is duplicated")
            used_names.add(name)
            parts.append(rf"(?P<{name}>[^/?#]+)")
            cursor = match.end()
        tail = value[cursor:]
        if "{" in tail or "}" in tail:
            raise ValueError("path_shape contains invalid braces")
        parts.append(re.escape(tail))
        parts.append(r"/?$")
        return "".join(parts)

    @staticmethod
    def _serialise_voice_link_rule(rule: VoiceLinkRule) -> dict[str, str]:
        payload: dict[str, str] = {
            "host": rule.host,
            "template": rule.template,
            "mode": rule.mode.value,
        }
        if isinstance(rule.spec, VoiceLinkSimpleRuleSpec):
            payload["path_shape"] = rule.spec.path_shape
        else:
            payload["path_regex"] = rule.spec.path_regex
        if rule.example_url:
            payload["example_url"] = rule.example_url
        return payload

    @staticmethod
    def _validate_voice_link_template(template: str, path_pattern: re.Pattern[str]) -> None:
        allowed_fields = {"host"}
        for group_name in path_pattern.groupindex:
            allowed_fields.add(group_name)
            allowed_fields.add(f"{group_name}_norm")
            allowed_fields.add(f"{group_name}_words")

        for _, field_name, format_spec, conversion in Formatter().parse(template):
            if field_name is None:
                continue
            if not field_name:
                raise ValueError("template must use named fields like `{title_norm}`")
            if conversion is not None or format_spec:
                raise ValueError("template format conversions/specifiers are not supported")
            if field_name not in allowed_fields:
                allowed_list = ", ".join(sorted(allowed_fields))
                raise ValueError(f"template field `{field_name}` is invalid; allowed: {allowed_list}")

    async def available_voices(self, force_refresh: bool = False) -> list[str]:
        if self._available_voices and not force_refresh:
            return self._available_voices
        if not self._engine:
            self._available_voices = []
            return self._available_voices

        if self._engine_kind == "piper":
            self._available_voices = self._limit_available_voices(
                self._piper_available_voices(),
                preferred=self.voice,
            )
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
        self._available_voices = self._limit_available_voices(english if english else voices, preferred=self.voice)
        return self._available_voices

    @staticmethod
    async def _run_blocking_io(
        func: Callable[..., BlockingReturnT],
        /,
        *args: object,
    ) -> BlockingReturnT:
        return await run_blocking(func, *args)

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
        self._piper_python_voice_cache.clear()

    def _piper_python_runtime_ready(self, voice: str | None = None) -> bool:
        if self._engine_kind != "piper" or self._engine != "python":
            return True
        target_voice = self.voice if voice is None else voice
        return self._piper_model_path(target_voice) is not None

    async def scan_piper_models_from_hf(self, url: str) -> tuple[HFRepoRef, list[str]]:
        if self._engine_kind != "piper":
            raise RuntimeError("Model add/remove commands are only available for Piper TTS.")

        repo_ref = self._hf_parse_repo_url(url)
        tts_log.info(
            f"TTS HF scan start repo={repo_ref.repo_id!r} revision={repo_ref.revision!r} "
            f"direct_file={repo_ref.onnx_file!r}"
        )

        if repo_ref.onnx_file:
            is_candidate = await self._run_blocking_io(
                self._hf_is_piper_file_candidate,
                repo_ref.repo_id,
                repo_ref.revision,
                repo_ref.onnx_file,
            )
            if not is_candidate:
                tts_log.info(
                    f"TTS HF scan rejected repo={repo_ref.repo_id!r} revision={repo_ref.revision!r} "
                    f"file={repo_ref.onnx_file!r}"
                )
                raise LookupError(
                    "The `.onnx` file in that URL is not Piper-compatible (missing/invalid `.onnx.json` Piper config)."
                )
            tts_log.info(
                f"TTS HF scan accepted repo={repo_ref.repo_id!r} revision={repo_ref.revision!r} "
                f"file={repo_ref.onnx_file!r}"
            )
            return repo_ref, [repo_ref.onnx_file]

        files = await self._run_blocking_io(self._hf_repo_files, repo_ref.repo_id, repo_ref.revision)
        candidates = await self._run_blocking_io(self._hf_find_piper_candidates, repo_ref.repo_id, repo_ref.revision, files)
        tts_log.info(
            f"TTS HF scan complete repo={repo_ref.repo_id!r} revision={repo_ref.revision!r} "
            f"repo_files={len(files)} candidates={len(candidates)}"
        )

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
        if len(self._piper_available_voices()) >= self._MAX_AVAILABLE_VOICES:
            raise ValueError(
                f"Voice limit reached: at most {self._MAX_AVAILABLE_VOICES} total voices are supported, "
                "including built-in voices. Remove a voice before adding another."
            )

        log.info(
            f"TTS HF model download start repo={repo_ref.repo_id!r} revision={repo_ref.revision!r} "
            f"file={selected_file!r} target={str(target_model)!r}"
        )
        await self._run_blocking_io(self._download_file, model_url, target_model, False)
        config_downloaded = False
        try:
            config_downloaded = await self._run_blocking_io(self._download_file, config_url, target_config, True)
        except Exception as xcp:
            log.warning(f"TTS Piper model config download failed model={target_model.stem!r}: {xcp}")

        self._invalidate_piper_runtime_cache()
        log.info(
            f"TTS HF model download complete model={target_model.stem!r} config_downloaded={config_downloaded}"
        )
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
            if self._engine_kind == "piper":
                log.warning(f"TTS variant '{self.variant}' unavailable for voice '{self.voice}'; disabling variant")
                self.variant = None
            return

        variant = self.variant
        if any(variant.lower() == v.lower() for v in variants):
            self.variant = next(v for v in variants if variant.lower() == v.lower())
            return

        log.warning(f"TTS variant '{self.variant}' unavailable; disabling variant")
        self.variant = None


# AiviA APasz
    BlockingReturnT = TypeVar("BlockingReturnT")
