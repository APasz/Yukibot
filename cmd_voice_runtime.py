from __future__ import annotations

# pyright: reportUninitializedInstanceVariable=false

import asyncio
import contextlib
import io
import logging
import re
import wave
from collections import deque
from collections.abc import Awaitable, Iterable, Mapping, Sized
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ClassVar, cast
from urllib.parse import unquote, urlparse

import emoji
import hikari
import hikariwave
from hikari import messages as hikari_messages

import config
from _async_utils import run_blocking
from cmd_voice_common import (
    CHANNEL_MENTION_RE,
    DISCORD_CUSTOM_EMOJI_RE,
    EMOJI_TAG_RE,
    SUBSTITUTION_TOKEN_RE,
    TOKEN_RE,
    URL_RE,
    USER_MENTION_RE,
    ActivePlayback,
    PiperPythonVoiceRuntime,
    PlaybackWaitResult,
    PronunciationFormat,
    PronunciationOverride,
    SpeechContent,
    SpeechToken,
    SpeechTokenKind,
    TextCorrectionCatalog,
    TextSubstitutionRule,
    UserVoiceSettings,
    VoiceConnectBackoff,
    VoiceJob,
    VoiceLinkRules,
    log,
)
from voice_common import (
    VoiceUdpDiscoveryNetworkError,
    VoiceUdpDiscoveryTimeoutError,
    wav_audio_duration_seconds,
)

tts_log = logging.getLogger(config.LOGGER_TTS)

DISCORD_TIMESTAMP_RE = re.compile(r"<t:\d+(?::[A-Za-z])?>")
DISCORD_HEADING_RE = re.compile(r"^(#{1,3}|-#)\s+(.*\S)\s*$", re.MULTILINE)
DISCORD_CODE_BLOCK_RE = re.compile(r"```(?:[^\s`]+\n)?(.*?)```", re.DOTALL)
DISCORD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
DISCORD_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
DISCORD_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
DISCORD_UNDERLINE_RE = re.compile(r"__(.+?)__", re.DOTALL)
DISCORD_TRIPLE_STAR_RE = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
DISCORD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
DISCORD_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)", re.DOTALL)
DISCORD_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])", re.DOTALL)
NUMERIC_VALUE_RE = r"\d+(?:\.\d+)?"
COMPACT_UNIT_RE = re.compile(rf"^([^\w.]*)({NUMERIC_VALUE_RE})(km|m|s|h|d)([^\w]*)$", re.IGNORECASE)
SLASH_RATIO_RE = re.compile(rf"^([^\w.]*)({NUMERIC_VALUE_RE})/({NUMERIC_VALUE_RE})([^\w]*)$")
CURRENCY_AMOUNT_RE = re.compile(
    rf"(?<!\w)(?:(?P<prefix>[$£€Є¥₩₹])\s*(?P<prefix_value>{NUMERIC_VALUE_RE})|(?P<suffix_value>{NUMERIC_VALUE_RE})\s*(?P<suffix>[$£€Є¥₩₹]))(?!\w)"
)
WITH_SHORTHAND_RE = re.compile(r"^([^\w]*)(w/|w/o)([^\w]*)$", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class SpokenQuantityForms:
    singular: str
    plural: str


@dataclass(slots=True, frozen=True)
class SpeechNormalisationContext:
    source_user_id: int | None
    selected_voice: str
    pronunciations: dict[str, PronunciationOverride]
    substitutions: dict[str, TextSubstitutionRule]
    fuzzy_autocorrect_enabled: bool


class VoiceTTSRuntimeMixin:
    _SPOKEN_LINK_HOST_ALIASES: ClassVar[dict[str, str]] = {
        "youtu.be": "youtube",
        "youtube.com": "youtube",
    }
    _COMPACT_UNIT_WORDS: ClassVar[dict[str, SpokenQuantityForms]] = {
        "km": SpokenQuantityForms("kilometer", "kilometers"),
        "m": SpokenQuantityForms("minute", "minutes"),
        "s": SpokenQuantityForms("second", "seconds"),
        "h": SpokenQuantityForms("hour", "hours"),
        "d": SpokenQuantityForms("day", "days"),
    }
    _CURRENCY_SYMBOL_WORDS: ClassVar[dict[str, SpokenQuantityForms]] = {
        "$": SpokenQuantityForms("dollar", "dollars"),
        "£": SpokenQuantityForms("pound", "pounds"),
        "€": SpokenQuantityForms("euro", "euros"),
        "Є": SpokenQuantityForms("euro", "euros"),
        "¥": SpokenQuantityForms("yen", "yen"),
        "₩": SpokenQuantityForms("won", "won"),
        "₹": SpokenQuantityForms("rupee", "rupees"),
    }
    _REPLY_PREFIX: ClassVar[str] = "is reply..."
    _FORWARD_PREFIX: ClassVar[str] = "is forwarded..."

    if TYPE_CHECKING:
        bot: hikari.GatewayBot
        _voice_client: hikariwave.VoiceClient
        _music_duck_handler: (
            Callable[
                [hikari.Snowflake, hikari.Snowflake, bytes],
                Awaitable[tuple[hikariwave.VoiceConnection, hikariwave.AudioSource] | None],
            ]
            | None
        )
        _queue: asyncio.Queue[VoiceJob]
        _backlog_job_count: int
        _worker_task: asyncio.Task[None] | None
        _engine_kind: str
        _engine: str | None
        _piper_python_loader: Callable[[str, str | None], PiperPythonVoiceRuntime] | None
        _piper_config_path: str | None
        _user_settings: dict[int, UserVoiceSettings]
        _text_corrections: TextCorrectionCatalog
        _voice_link_rules: VoiceLinkRules
        _enabled: bool
        voice: str
        variant: str | None
        _MAX_BACKLOG_JOBS: ClassVar[int]
        _MAX_SPOKEN_CHARS: ClassVar[int]
        _FUZZY_AUTOCORRECT_MIN_LEN: ClassVar[int]
        _VOICE_CONNECT_TIMEOUT_SECONDS: ClassVar[float]
        _QUEUE_BATCH_WINDOW_SECONDS: ClassVar[float]
        _QUEUE_LATE_JOIN_TAIL_MIN_SECONDS: ClassVar[float]
        _QUEUE_LATE_JOIN_TAIL_MAX_SECONDS: ClassVar[float]
        _QUEUE_LATE_JOIN_TAIL_RATIO: ClassVar[float]
        _MAX_BATCHED_JOBS: ClassVar[int]

        def _piper_python_runtime_ready(self, voice: str | None = None) -> bool: ...
        def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None: ...
        def _target_voice_listener_count(self, guild_id: hikari.Snowflakeish) -> int: ...
        def _active_voice_connect_backoff(
            self, guild_id: hikari.Snowflakeish, listener_count: int
        ) -> VoiceConnectBackoff | None: ...
        def is_user_listening(self, user_id: hikari.Snowflakeish) -> bool: ...
        def user_autocorrect_enabled(self, user_id: hikari.Snowflakeish) -> bool: ...
        def user_voice_variant(self, user_id: hikari.Snowflakeish) -> tuple[str, str | None]: ...
        def user_voice_variant_for_say(self, user_id: hikari.Snowflakeish) -> tuple[str, str | None]: ...
        def user_pronunciations(
            self,
            user_id: hikari.Snowflakeish,
            voice: str | None = None,
        ) -> dict[str, PronunciationOverride]: ...
        def voice_supports_ipa_pronunciations(self, voice: str) -> bool: ...
        def _preview(self, text: str) -> str: ...

        @staticmethod
        def _voice_spec(voice: str, variant: str | None) -> str: ...

        def _active_music_channel(self, guild_id: hikari.Snowflakeish) -> hikari.Snowflake | None: ...

        @staticmethod
        def _playback_timeout_seconds(text: str) -> float: ...

        @staticmethod
        def _connection_state_name(connection: hikariwave.VoiceConnection) -> str: ...

        @classmethod
        def _connection_is_ready(cls, connection: hikariwave.VoiceConnection) -> bool: ...

        def _clear_voice_connect_backoff(self, guild_id: hikari.Snowflakeish) -> None: ...
        async def _reset_voice_connection(
            self, guild_id: hikari.Snowflake, target_channel: hikari.Snowflake, *, verify: bool = False
        ) -> bool:
            raise NotImplementedError

        def _record_voice_connect_failure(
            self, guild_id: hikari.Snowflakeish, listener_count: int, error: Exception
        ) -> None: ...
        def _piper_model_path(self, voice: str) -> Path | None: ...
        def _piper_speaker_id(self, voice: str, variant: str | None) -> int | None: ...
        def _piper_model_search_dirs(self) -> list[Path]: ...
        def _piper_python_voice(self, voice: str) -> PiperPythonVoiceRuntime | None: ...
        def _refresh_voice_link_rules_if_needed(self) -> None: ...
        async def _mod_link_name(self, url: str) -> str | None: ...

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
        if not self._piper_python_runtime_ready():
            return "voice_unavailable"
        if not self.voice_target(guild_id):
            return "guild_unconfigured"
        if require_worker and (not self._worker_task or self._worker_task.done()):
            return "worker_not_running"
        listener_count = self._target_voice_listener_count(guild_id)
        if listener_count == 0:
            return "voice_channel_empty"
        if self._active_voice_connect_backoff(guild_id, listener_count):
            return "voice_connect_backoff"
        return None

    @staticmethod
    def _queue_preflight_error(reason: str) -> str:
        if reason == "no_local_tts_engine":
            return "TTS engine is unavailable on the bot host."
        if reason == "voice_unavailable":
            return "The configured Piper voice model is unavailable on the bot host."
        if reason == "guild_unconfigured":
            return "Voice TTS is not configured for this server."
        if reason == "worker_not_running":
            return "Voice TTS worker is not running."
        if reason == "voice_channel_empty":
            return "No one is in the configured voice channel."
        if reason == "voice_connect_backoff":
            return "Voice connect is temporarily cooling down after a recent failure."
        if reason == "service_disabled":
            return "Voice TTS is disabled."
        return "TTS is unavailable."

    def _try_enqueue_job(self, job: VoiceJob) -> int | None:
        if self._backlog_job_count >= self._MAX_BACKLOG_JOBS:
            return None
        self._queue.put_nowait(job)
        self._backlog_job_count += 1
        return self._backlog_job_count

    def _drop_queued_jobs(self) -> int:
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
                dropped += 1

        self._backlog_job_count = max(0, self._backlog_job_count - dropped)
        if dropped or self._backlog_job_count:
            self._backlog_job_count = 0
        return dropped

    async def on_message(self, event: hikari.GuildMessageCreateEvent):
        if not event.guild_id:
            return

        target = self.voice_target(event.guild_id)
        if not target or not target.has_listening_tts_channel(event.channel_id):
            return

        content = (event.content or "").strip()
        if content.startswith(config.CHAT_IGNORE):
            preview = self._preview(content)
            tts_log.info(
                f"TTS message {event.message_id=} {event.guild_id=} {event.channel_id=} {event.author_id=} "
                f"attachments={len(event.message.attachments)} preview={preview!r} said=no reason=chat_ignore_prefix"
            )
            return

        raw = self._message_speech_input(
            content,
            attachment_count=len(event.message.attachments),
            sticker_count=len(event.message.stickers),
            sticker_names=self._sticker_speech_fragments(event.message.stickers),
            is_reply=self._is_reply_message(event.message),
            is_forward=self._is_forward_message(event.message),
            forwarded_content=self._forwarded_snapshot_speech_input(event.message, event.author_id, event.guild_id),
        )
        preview = self._preview(raw)
        base_log = (
            f"TTS message {event.message_id=} {event.guild_id=} {event.channel_id=} {event.author_id=} "
            f"attachments={len(event.message.attachments)} preview={preview!r}"
        )

        if not event.is_human:
            tts_log.info(f"{base_log} said=no reason=not_human")
            return
        if not self.is_user_listening(event.author_id):
            tts_log.info(f"{base_log} said=no reason=wrong_user")
            return
        if reason := self._queue_preflight_reason(event.guild_id, require_enabled=True):
            tts_log.info(f"{base_log} said=no reason={reason}")
            return

        spoken = await self._normalise_for_speech_async(raw, event)
        if not spoken:
            tts_log.info(f"{base_log} said=no reason=empty_after_normalise")
            return

        selected_voice, selected_variant = self.user_voice_variant(event.author_id)
        voice_spec = self._voice_spec(selected_voice, selected_variant)
        queue_size = self._try_enqueue_job(
            VoiceJob(
                hikari.Snowflake(event.guild_id),
                event.message_id,
                spoken,
                selected_voice,
                selected_variant,
            )
        )
        if queue_size is None:
            log.warning(f"{base_log} said=no reason=queue_full backlog={self._backlog_job_count}")
            return
        tts_log.info(
            f"{base_log} said=queued reason=accepted queue_size={queue_size} "
            f"voice={voice_spec} spoken={self._preview(spoken.render())!r}"
        )

    @classmethod
    def _is_reply_message(cls, message: hikari.Message) -> bool:
        return message.type is hikari.MessageType.REPLY

    @staticmethod
    def _is_forward_message(message: hikari.Message) -> bool:
        reference = message.message_reference
        return (
            reference is not None
            and getattr(reference, "type", None) is hikari_messages.MessageReferenceType.FORWARD
        )

    @classmethod
    def _message_speech_input(
        cls,
        raw_content: str,
        *,
        attachment_count: int,
        sticker_count: int = 0,
        sticker_names: Iterable[str] = (),
        is_reply: bool = False,
        is_forward: bool = False,
        forwarded_content: str = "",
    ) -> str:
        content = cls._compose_message_speech_body(
            raw_content,
            attachment_count=attachment_count,
            sticker_count=sticker_count,
            sticker_names=sticker_names,
        )
        forwarded = forwarded_content.strip()
        if forwarded:
            content = f"{content}... {forwarded}" if content else forwarded

        prefixes: list[str] = []
        if is_forward:
            prefixes.append(cls._FORWARD_PREFIX)
        if is_reply:
            prefixes.append(cls._REPLY_PREFIX)

        if prefixes:
            prefix_text = " ".join(prefixes)
            if content:
                return f"{prefix_text} {content}"
            return prefix_text
        return content

    @classmethod
    def _compose_message_speech_body(
        cls,
        raw_content: str,
        *,
        attachment_count: int,
        sticker_count: int = 0,
        sticker_names: Iterable[str] = (),
        include_extras_with_content: bool = False,
    ) -> str:
        content = raw_content.strip()
        extras = cls._message_extra_speech(
            attachment_count=attachment_count,
            sticker_count=sticker_count,
            sticker_names=sticker_names,
        )
        if content:
            if extras and include_extras_with_content:
                return f"{content}, {extras}"
            return content
        return extras

    @staticmethod
    def _message_extra_speech(
        *,
        attachment_count: int,
        sticker_count: int,
        sticker_names: Iterable[str] = (),
    ) -> str:
        extras: list[str] = []
        if attachment_count > 0:
            extras.append("attachment" if attachment_count == 1 else f"{attachment_count} attachments")
        resolved_sticker_names = tuple(name for name in sticker_names if name)
        extras.extend(resolved_sticker_names)
        remaining_sticker_count = max(0, sticker_count - len(resolved_sticker_names))
        if remaining_sticker_count > 0:
            extras.append("sticker" if remaining_sticker_count == 1 else f"{remaining_sticker_count} stickers")
        return ", ".join(extras)

    @staticmethod
    def _collection_size(value: object) -> int:
        if value is None or value is hikari.UNDEFINED:
            return 0
        try:
            return len(cast(Sized, value))
        except TypeError:
            return 0

    @classmethod
    def _sticker_speech_fragments(cls, stickers: object) -> tuple[str, ...]:
        if stickers is None or stickers is hikari.UNDEFINED:
            return ()
        try:
            sticker_items = tuple(cast(Iterable[object], stickers))
        except TypeError:
            return ()

        fragments: list[str] = []
        for sticker in sticker_items:
            raw_name = getattr(sticker, "name", None)
            if not isinstance(raw_name, str):
                continue
            fragment = cls._sticker_name_to_speech_fragment(raw_name)
            if fragment:
                fragments.append(fragment)
        return tuple(fragments)

    @staticmethod
    def _sticker_name_to_speech_fragment(raw_name: str) -> str | None:
        cleaned_name = re.sub(r"\s+", " ", raw_name).strip()
        if not cleaned_name:
            return None

        tag_name = re.sub(r"[^a-z0-9_+\-]+", "_", cleaned_name.casefold())
        tag_name = re.sub(r"_+", "_", tag_name).strip("_")
        if tag_name:
            return f":{tag_name}:"
        return cleaned_name

    def _forwarded_snapshot_speech_input(
        self,
        message: hikari.Message,
        source_user_id: hikari.Snowflakeish,
        guild_id: hikari.Snowflake | None,
    ) -> str:
        snapshots = getattr(message, "message_snapshots", hikari.UNDEFINED)
        if snapshots is None or snapshots is hikari.UNDEFINED:
            return ""
        try:
            snapshot_items = tuple(cast(Iterable[object], snapshots))
        except TypeError:
            return ""

        rendered_snapshots: list[str] = []
        for snapshot in snapshot_items:
            content = self._replace_mentions_with_context(
                getattr(snapshot, "content", "") or "",
                source_user_id=int(source_user_id),
                guild_id=guild_id,
                user_mentions=cast(
                    Mapping[hikari.Snowflake, object] | hikari.UndefinedType,
                    getattr(snapshot, "user_mentions", hikari.UNDEFINED),
                ),
            )
            rendered = self._compose_message_speech_body(
                content,
                attachment_count=self._collection_size(getattr(snapshot, "attachments", None)),
                sticker_count=self._collection_size(getattr(snapshot, "stickers", None)),
                sticker_names=self._sticker_speech_fragments(getattr(snapshot, "stickers", None)),
                include_extras_with_content=True,
            )
            if rendered:
                rendered_snapshots.append(rendered)
        return "... ".join(rendered_snapshots)

    async def queue_say(
        self,
        guild_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        user_id: hikari.Snowflakeish | None = None,
    ) -> tuple[str, int]:
        if reason := self._queue_preflight_reason(guild_id, require_worker=True):
            raise RuntimeError(self._queue_preflight_error(reason))

        spoken = await self._normalise_for_speech_async(text, user_id=user_id)
        if not spoken:
            raise ValueError("No speakable text after normalisation.")

        selected_voice, selected_variant = (
            self.user_voice_variant_for_say(user_id) if user_id else (self.voice, self.variant)
        )

        guild = hikari.Snowflake(guild_id)
        message = hikari.Snowflake(message_id)
        voice_spec = self._voice_spec(selected_voice, selected_variant)
        queue_size = self._try_enqueue_job(VoiceJob(guild, message, spoken, selected_voice, selected_variant))
        if queue_size is None:
            raise RuntimeError("Voice TTS backlog is full. Try again once the queue drains.")
        tts_log.info(
            f"TTS command queued guild={guild} message_id={message} "
            f"queue_size={queue_size} voice={voice_spec} spoken={self._preview(spoken.render())!r}"
        )
        return spoken.render(), queue_size

    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]:
        guild = hikari.Snowflake(guild_id)
        channel = hikari.Snowflake(channel_id)
        if user_id is None:
            raise RuntimeError("Relay author is not linked to a Discord user.")
        target = self.voice_target(guild)
        if target is None:
            raise RuntimeError("Voice TTS is not configured for this server.")
        if not target.has_listening_tts_channel(channel):
            raise RuntimeError("Relay message was not posted in an active TTS channel.")
        if not target.relay_tts_enabled:
            raise RuntimeError("Relay TTS is disabled for this server.")
        if not self.is_user_listening(user_id):
            raise RuntimeError("Relay author is not listening to TTS.")
        if reason := self._queue_preflight_reason(guild, require_enabled=True, require_worker=True):
            raise RuntimeError(self._queue_preflight_error(reason))

        spoken = await self._normalise_for_speech_async(text, user_id=user_id)
        if not spoken:
            raise ValueError("No speakable relay text after normalisation.")

        selected_voice, selected_variant = self.user_voice_variant(user_id)
        message = hikari.Snowflake(message_id)
        voice_spec = self._voice_spec(selected_voice, selected_variant)
        queue_size = self._try_enqueue_job(VoiceJob(guild, message, spoken, selected_voice, selected_variant))
        if queue_size is None:
            raise RuntimeError("Voice TTS backlog is full. Try again once the queue drains.")
        tts_log.info(
            f"TTS relay queued guild={guild} channel={channel} message_id={message} user_id={int(user_id)} "
            f"queue_size={queue_size} voice={voice_spec} spoken={self._preview(spoken.render())!r}"
        )
        return spoken.render(), queue_size

    async def _normalise_for_speech_async(
        self,
        content: str,
        event: hikari.GuildMessageCreateEvent | None = None,
        user_id: hikari.Snowflakeish | None = None,
    ) -> SpeechContent:
        text = content.strip()
        if not text:
            return SpeechContent(())

        context = self._speech_normalisation_context(event=event, user_id=user_id)
        self._refresh_voice_link_rules_if_needed()
        text = await self._replace_links_async(text, substitutions=context.substitutions)
        return self._normalise_for_speech(text, event=event, user_id=user_id, links_resolved=True)

    async def _replace_links_async(
        self,
        text: str,
        *,
        substitutions: Mapping[str, TextSubstitutionRule] | None = None,
    ) -> str:
        parts: list[str] = []
        cursor = 0
        for match in URL_RE.finditer(text):
            parts.append(text[cursor : match.start()])
            parts.append(await self._replace_link_async(match.group(0), substitutions=substitutions))
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts)

    async def _replace_link_async(
        self,
        raw_url: str,
        *,
        substitutions: Mapping[str, TextSubstitutionRule] | None = None,
    ) -> str:
        trimmed_url = raw_url.rstrip(".,!?;:")
        if not trimmed_url:
            return " "
        if substituted := self._url_substitution_target(trimmed_url, substitutions=substitutions):
            return f" {substituted} "

        parsed = urlparse(trimmed_url if "://" in trimmed_url else f"https://{trimmed_url}")
        hostname = parsed.hostname
        if not hostname:
            return " link "

        mod_name = await self._mod_link_name(trimmed_url)
        if mod_name:
            return f" mod {mod_name} "

        spoken = self._describe_link(hostname, parsed.path)
        return f" {spoken} "

    async def _worker_loop(self):
        pending_jobs: deque[VoiceJob] = deque()
        current_playback: ActivePlayback | None = None
        queued_playback: ActivePlayback | None = None
        inflight_jobs: tuple[VoiceJob, ...] = ()

        try:
            while True:
                if current_playback is None:
                    job = pending_jobs.popleft() if pending_jobs else await self._queue.get()
                    batch = [job]
                    inflight_jobs = (job,)
                    try:
                        batch = await self._collect_batch(job, pending_jobs)
                        inflight_jobs = tuple(batch)
                        current_playback = await self._enqueue_job_batch(batch)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(f"Voice TTS failed for message_id={job.message_id} batch_size={len(batch)}")
                    if current_playback is None:
                        self._mark_jobs_done(batch)
                    inflight_jobs = ()
                    continue

                if queued_playback is None:
                    await self._buffer_jobs_until_tail(current_playback, pending_jobs)
                    if current_playback.done_event.is_set():
                        await self._finalize_playback(current_playback)
                        current_playback = None
                        continue

                    if not pending_jobs:
                        await self._buffer_jobs_until_ready_or_done(current_playback, pending_jobs)
                        if current_playback.done_event.is_set():
                            await self._finalize_playback(current_playback)
                            current_playback = None
                            continue
                        if not pending_jobs:
                            continue

                    job = pending_jobs.popleft()
                    batch = [job]
                    inflight_jobs = (job,)
                    try:
                        batch = await self._collect_batch(job, pending_jobs)
                        inflight_jobs = tuple(batch)
                        queued_playback = await self._enqueue_job_batch(batch, predecessor=current_playback)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(f"Voice TTS failed for message_id={job.message_id} batch_size={len(batch)}")
                    if queued_playback is None:
                        self._mark_jobs_done(batch)
                    inflight_jobs = ()
                    continue

                await self._buffer_jobs_until_done(current_playback, pending_jobs)
                await self._finalize_playback(current_playback)
                current_playback = queued_playback
                queued_playback = None
        except asyncio.CancelledError:
            if inflight_jobs:
                self._mark_jobs_done(inflight_jobs)
                inflight_jobs = ()
            if pending_jobs:
                self._mark_jobs_done(pending_jobs)
                pending_jobs.clear()
            for playback in (current_playback, queued_playback):
                if playback and playback.monitor_task:
                    playback.monitor_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await playback.monitor_task
                    self._mark_jobs_done(playback.jobs)
            raise

    async def _collect_batch(self, first_job: VoiceJob, pending_jobs: deque[VoiceJob]) -> list[VoiceJob]:
        batch = [first_job]
        deadline = asyncio.get_running_loop().time() + self._QUEUE_BATCH_WINDOW_SECONDS
        batch_len = first_job.speech.rendered_len()

        while len(batch) < self._MAX_BATCHED_JOBS:
            if pending_jobs:
                next_job = pending_jobs[0]
                fetched_from_queue = False
            else:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    next_job = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                fetched_from_queue = True

            if not self._jobs_can_batch(first_job, next_job):
                if fetched_from_queue:
                    pending_jobs.appendleft(next_job)
                break

            next_len = batch_len + self._batched_message_additional_len(batch[-1].speech, next_job.speech)
            if next_len > self._MAX_SPOKEN_CHARS:
                if fetched_from_queue:
                    pending_jobs.appendleft(next_job)
                break

            if not fetched_from_queue:
                pending_jobs.popleft()
            batch.append(next_job)
            batch_len = next_len

        return batch

    @staticmethod
    def _jobs_can_batch(left: VoiceJob, right: VoiceJob) -> bool:
        return left.guild_id == right.guild_id and left.voice == right.voice and left.variant == right.variant

    async def _enqueue_job_batch(
        self,
        jobs: list[VoiceJob],
        predecessor: ActivePlayback | None = None,
    ) -> ActivePlayback | None:
        job = jobs[0]
        connection = await self._ensure_connection(job.guild_id)
        if not connection:
            log.warning(f"TTS job dropped {job.message_id=} batch_size={len(jobs)} said=no reason=no_voice_connection")
            return

        self._recover_stale_player_task(connection, job.message_id)

        text = self._render_batched_speech(job.speech for job in jobs)
        audio = await self._synth_text(text, job.voice, job.variant)
        if not audio:
            log.warning(f"TTS job dropped {job.message_id=} batch_size={len(jobs)} said=no reason=tts_synth_empty")
            return None

        source: hikariwave.AudioSource
        music_channel = self._active_music_channel(job.guild_id)
        if self._music_duck_handler:
            ducked_playback = await self._music_duck_handler(job.guild_id, job.message_id, audio)
            if ducked_playback is not None:
                connection, source = ducked_playback
                playback = ActivePlayback(
                    jobs=tuple(jobs),
                    text=text,
                    connection=connection,
                    source=source,
                    begin_timeout_seconds=self._playback_begin_timeout_seconds(predecessor),
                    timeout_seconds=self._playback_timeout_seconds(text),
                    expected_duration_seconds=wav_audio_duration_seconds(audio),
                )
                playback.monitor_task = asyncio.create_task(
                    self._monitor_playback(playback),
                    name=f"voice-tts-playback-{job.message_id}",
                )
                tts_log.info(
                    f"TTS job ducked-to-player {job.message_id=} batch_size={len(jobs)} "
                    f"voice={self._voice_spec(job.voice, job.variant)} spoken={self._preview(text)!r}"
                )
                return playback
            if music_channel is not None and connection.channel_id == music_channel:
                log.warning(
                    f"TTS job dropped {job.message_id=} batch_size={len(jobs)} said=no reason=music_duck_unavailable"
                )
                return None

        source = hikariwave.BufferAudioSource(audio, name=f"tts-{job.message_id}")
        result = await connection.player.add_queue(source)
        if not result.success:
            log.warning(
                f"TTS job dropped {job.message_id=} batch_size={len(jobs)} "
                f"said=no reason=player_rejected detail={result.reason}"
            )
            return None

        playback = ActivePlayback(
            jobs=tuple(jobs),
            text=text,
            connection=connection,
            source=source,
            begin_timeout_seconds=self._playback_begin_timeout_seconds(predecessor),
            timeout_seconds=self._playback_timeout_seconds(text),
            expected_duration_seconds=wav_audio_duration_seconds(audio),
        )
        playback.monitor_task = asyncio.create_task(
            self._monitor_playback(playback),
            name=f"voice-tts-playback-{job.message_id}",
        )

        tts_log.info(
            f"TTS job queued-to-player {job.message_id=} queue_len={connection.player.queue} "
            f"batch_size={len(jobs)} voice={self._voice_spec(job.voice, job.variant)} "
            f"spoken={self._preview(text)!r}"
        )
        return playback

    async def _buffer_jobs_until_tail(self, playback: ActivePlayback, pending_jobs: deque[VoiceJob]) -> None:
        while not playback.done_event.is_set():
            timeout = self._time_until_late_join(playback)
            if timeout is not None and timeout <= 0:
                return

            result, job = await self._await_job_or_playback(playback, timeout)
            if result is PlaybackWaitResult.JOB and job is not None:
                pending_jobs.append(job)
                continue
            if result is PlaybackWaitResult.DONE:
                return
            if result is PlaybackWaitResult.TIMEOUT:
                return

    async def _buffer_jobs_until_ready_or_done(self, playback: ActivePlayback, pending_jobs: deque[VoiceJob]) -> None:
        while not playback.done_event.is_set() and not pending_jobs:
            result, job = await self._await_job_or_playback(playback)
            if result is PlaybackWaitResult.JOB and job is not None:
                pending_jobs.append(job)
                return
            if result is PlaybackWaitResult.DONE:
                return

    async def _buffer_jobs_until_done(self, playback: ActivePlayback, pending_jobs: deque[VoiceJob]) -> None:
        while not playback.done_event.is_set():
            result, job = await self._await_job_or_playback(playback)
            if result is PlaybackWaitResult.JOB and job is not None:
                pending_jobs.append(job)
                continue
            return

    async def _await_job_or_playback(
        self,
        playback: ActivePlayback,
        timeout: float | None = None,
    ) -> tuple[PlaybackWaitResult, VoiceJob | None]:
        queue_task = asyncio.create_task(self._queue.get())
        done_task = asyncio.create_task(playback.done_event.wait())
        timeout_task: asyncio.Task[None] | None = None
        tasks: set[asyncio.Task[object]] = {
            cast(asyncio.Task[object], queue_task),
            cast(asyncio.Task[object], done_task),
        }
        if timeout is not None:
            timeout_task = asyncio.create_task(asyncio.sleep(timeout))
            tasks.add(cast(asyncio.Task[object], timeout_task))

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            recovered_job: VoiceJob | None = None
            if queue_task.done() and not queue_task.cancelled():
                with contextlib.suppress(Exception):
                    recovered_job = queue_task.result()

            for task in tasks:
                if task is not queue_task or recovered_job is None:
                    task.cancel()
            for task in tasks:
                if task is queue_task and recovered_job is not None:
                    continue
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            if recovered_job is not None:
                self._queue.put_nowait(recovered_job)
            raise
        except Exception:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if queue_task in done:
            return PlaybackWaitResult.JOB, queue_task.result()
        if done_task in done:
            return PlaybackWaitResult.DONE, None
        return PlaybackWaitResult.TIMEOUT, None

    async def _finalize_playback(self, playback: ActivePlayback) -> None:
        if playback.monitor_task:
            await playback.monitor_task
        self._mark_jobs_done(playback.jobs)

    def _mark_jobs_done(self, jobs: Iterable[VoiceJob]) -> None:
        completed = 0
        for _ in jobs:
            self._queue.task_done()
            completed += 1
        self._backlog_job_count = max(0, self._backlog_job_count - completed)

    def _time_until_late_join(self, playback: ActivePlayback) -> float | None:
        if playback.started_at is None or playback.expected_duration_seconds is None:
            return None

        late_join_tail = self._late_join_tail_seconds(playback.expected_duration_seconds)
        deadline = playback.started_at + max(playback.expected_duration_seconds - late_join_tail, 0.0)
        return deadline - asyncio.get_running_loop().time()

    @classmethod
    def _late_join_tail_seconds(cls, duration_seconds: float) -> float:
        return min(
            cls._QUEUE_LATE_JOIN_TAIL_MAX_SECONDS,
            max(cls._QUEUE_LATE_JOIN_TAIL_MIN_SECONDS, duration_seconds * cls._QUEUE_LATE_JOIN_TAIL_RATIO),
        )

    def _playback_begin_timeout_seconds(self, predecessor: ActivePlayback | None) -> float:
        if predecessor is None:
            return 5.0
        return max(5.0, self._remaining_playback_seconds(predecessor) + 5.0)

    def _remaining_playback_seconds(self, playback: ActivePlayback) -> float:
        if playback.started_at is not None and playback.expected_duration_seconds is not None:
            elapsed = asyncio.get_running_loop().time() - playback.started_at
            return max(playback.expected_duration_seconds - elapsed, 0.0)
        if playback.expected_duration_seconds is not None:
            return playback.expected_duration_seconds
        return playback.timeout_seconds

    async def _ensure_connection(self, guild_id: hikari.Snowflake) -> hikariwave.VoiceConnection | None:
        target = self.voice_target(guild_id)
        if not target:
            return None

        target_channel = target.voice_channel
        connection = self._voice_client.get_connection(guild_id=guild_id)
        listeners = self._target_voice_listener_count(guild_id)
        music_channel = self._active_music_channel(guild_id)

        if music_channel is not None and music_channel != target_channel:
            tts_log.info(
                f"TTS voice skip connect {guild_id=} channel={target_channel} "
                f"reason=music_active_other_channel active_channel={music_channel}"
            )
            return None

        if connection and connection.channel_id == target_channel:
            state_name = self._connection_state_name(connection)
            if self._connection_is_ready(connection):
                self._clear_voice_connect_backoff(guild_id)
                if listeners == 0:
                    tts_log.info(
                        f"TTS voice not ready {guild_id=} channel={target_channel} mode=disconnect_empty_channel"
                    )
                    with contextlib.suppress(Exception):
                        await self._voice_client.disconnect(channel_id=target_channel)
                    return None
                tts_log.info(f"TTS voice ready {guild_id=} channel={target_channel} mode=reuse state={state_name}")
                return connection

        if listeners == 0:
            tts_log.info(f"TTS voice skip connect {guild_id=} channel={target_channel} reason=channel_empty")
            return None

        active_backoff = self._active_voice_connect_backoff(guild_id, listeners)
        if active_backoff is not None:
            remaining = active_backoff.retry_at_monotonic - asyncio.get_running_loop().time()
            tts_log.info(
                f"TTS voice skip connect {guild_id=} channel={target_channel} "
                f"reason={active_backoff.reason} cooldown_remaining={remaining:.1f}s "
                f"detail={active_backoff.detail}"
            )
            return None

        if connection and connection.channel_id == target_channel:
            state_name = self._connection_state_name(connection)
            log.warning(
                f"TTS stale voice connection {guild_id=} channel={target_channel} mode=reset state={state_name}"
            )
            await self._reset_voice_connection(guild_id, target_channel)
            connection = None

        if connection:
            try:
                tts_log.info(
                    f"TTS moving voice {guild_id=} from={connection.channel_id} to={target_channel} "
                    f"mode=move timeout={self._VOICE_CONNECT_TIMEOUT_SECONDS:.1f}s"
                )
                moved = await asyncio.wait_for(
                    self._voice_client.move(target_channel, guild_id=guild_id, deaf=True),
                    timeout=self._VOICE_CONNECT_TIMEOUT_SECONDS,
                )
                if self._connection_is_ready(moved):
                    self._clear_voice_connect_backoff(guild_id)
                    tts_log.info(
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
                tts_log.info(
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
                self._clear_voice_connect_backoff(guild_id)
                return connected
            except VoiceUdpDiscoveryTimeoutError as xcp:
                last_xcp = xcp
                log.warning(
                    f"TTS connect attempt failed {guild_id=} attempt={attempt} "
                    f"reason=udp_discovery_timeout detail={xcp}"
                )
            except VoiceUdpDiscoveryNetworkError as xcp:
                last_xcp = xcp
                log.warning(
                    f"TTS connect attempt failed {guild_id=} attempt={attempt} "
                    f"reason=udp_discovery_network_error detail={xcp}"
                )
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
            self._record_voice_connect_failure(guild_id, listeners, last_xcp)
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

        model_path = self._piper_model_path(voice)
        if self._piper_python_loader and model_path:
            return await run_blocking(self._synth_text_piper_python, text, voice, variant)
        if self._engine == "python":
            log.warning(f"TTS synth failed voice={self._voice_spec(voice, variant)} reason=no_piper_model")
            return b""

        speaker_id = self._piper_speaker_id(voice, variant)
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

    def _synth_text_piper_python(self, text: str, voice: str, variant: str | None) -> bytes:
        speaker_id = self._piper_speaker_id(voice, variant)
        voice_runtime = self._piper_python_voice(voice)
        if voice_runtime is None:
            return b""

        buffer = io.BytesIO()
        wav_file: wave.Wave_write | None = None
        try:
            wav_file = wave.open(buffer, "wb")
            voice_runtime.synthesize_to_wav(text, wav_file, speaker_id=speaker_id)
        except Exception as xcp:
            log.warning(
                f"TTS Piper python synth failed voice={self._voice_spec(voice, variant)} "
                f"speaker={speaker_id}: {type(xcp).__name__}: {xcp}"
            )
            return b""
        finally:
            if wav_file is not None:
                with contextlib.suppress(Exception):
                    wav_file.close()
        return buffer.getvalue()

    async def _monitor_playback(self, playback: ActivePlayback) -> None:
        job = playback.jobs[0]

        def pred(e):
            return e.guild_id == job.guild_id and e.audio is playback.source

        try:
            begin_timed_out = False
            try:
                await self.bot.wait_for(hikariwave.AudioBeginEvent, playback.begin_timeout_seconds, pred)
                playback.started_at = asyncio.get_running_loop().time()
                tts_log.info(f"TTS audio begin {job.message_id=} timeout={playback.timeout_seconds:.1f}s")
            except asyncio.TimeoutError:
                begin_timed_out = True
                log.warning(f"TTS audio begin timeout {job.message_id=} continuing wait_for_end")

            try:
                end_timeout = playback.timeout_seconds + (playback.begin_timeout_seconds if begin_timed_out else 0.0)
                await self.bot.wait_for(hikariwave.AudioEndEvent, end_timeout, pred)
                tts_log.info(f"TTS job completed {job.message_id=} batch_size={len(playback.jobs)} said=yes")
            except asyncio.TimeoutError:
                state = getattr(playback.connection.player.state, "name", str(playback.connection.player.state))
                log.warning(
                    f"TTS audio end timeout {job.message_id=} state={state} "
                    f"elapsed={playback.connection.player.elapsed:.2f}s "
                    f"queue_len={len(playback.connection.player.queue)} timeout={playback.timeout_seconds:.1f}s"
                )
                with contextlib.suppress(Exception):
                    await playback.connection.player.stop()
                with contextlib.suppress(Exception):
                    await self._voice_client.disconnect(guild_id=job.guild_id)
                await asyncio.sleep(0.35)
                log.warning(
                    f"TTS job dropped {job.message_id=} batch_size={len(playback.jobs)} "
                    "said=no reason=playback_timeout_or_reset"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                f"TTS playback monitor failed for message_id={job.message_id} batch_size={len(playback.jobs)}"
            )
        finally:
            playback.done_event.set()

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
        *,
        links_resolved: bool = False,
    ) -> SpeechContent:
        text = content.strip()
        if not text:
            return SpeechContent(())

        context = self._speech_normalisation_context(event=event, user_id=user_id)
        selected_voice = context.selected_voice
        pronunciations = context.pronunciations
        substitutions = context.substitutions
        fuzzy_autocorrect_enabled = context.fuzzy_autocorrect_enabled

        if event:
            text = self._replace_mentions_with_names(text, event)
        else:
            text = USER_MENTION_RE.sub(" user ", text)
            text = CHANNEL_MENTION_RE.sub(" channel ", text)
        if not links_resolved:
            self._refresh_voice_link_rules_if_needed()
            text = URL_RE.sub(lambda match: self._replace_link(match.group(0), substitutions=substitutions), text)
        text = self._replace_discord_formatting(text)
        text = DISCORD_CUSTOM_EMOJI_RE.sub(lambda m: f":{m.group(1)}:", text)
        text = emoji.demojize(text, language="en")
        text = self._expand_currency_speech_text(text)

        spoken_tokens: list[SpeechToken] = []
        repeat_tag: str | None = None
        repeat_count = 0

        def flush_repeat():
            nonlocal repeat_tag
            nonlocal repeat_count
            if not repeat_tag:
                return
            label = self._emoji_tag_to_words(repeat_tag, selected_voice, pronunciations, substitutions)
            spoken_tokens.append(SpeechToken(label, SpeechTokenKind.EMOJI, repeat_count))
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
            if pronunciations:
                token = self._apply_pronunciation_token(token, selected_voice, pronunciations)
            if substitutions:
                token = self._apply_substitution_token(token, substitutions)
            token = self._expand_compact_speech_token(token)
            token = self._apply_exact_correction_token(token, self._text_corrections.slang)
            token = self._apply_exact_correction_token(token, self._text_corrections.typos)
            token = self._expand_common_shorthand_token(token)
            if fuzzy_autocorrect_enabled:
                token = self._apply_fuzzy_typo_correction(token)
            clean = re.sub(r"\s+", " ", token).strip()
            if clean:
                spoken_tokens.append(SpeechToken(clean, SpeechTokenKind.TEXT))

        flush_repeat()

        tokens = self._truncate_speech_tokens(spoken_tokens)
        if not tokens:
            return SpeechContent(())
        return SpeechContent(tokens)

    def _speech_normalisation_context(
        self,
        *,
        event: hikari.GuildMessageCreateEvent | None,
        user_id: hikari.Snowflakeish | None,
    ) -> SpeechNormalisationContext:
        source_user_id: int | None = None
        if event is not None:
            source_user_id = int(event.author_id)
        elif user_id is not None:
            source_user_id = int(user_id)

        if source_user_id is None:
            return SpeechNormalisationContext(
                source_user_id=None,
                selected_voice=self.voice,
                pronunciations={},
                substitutions={},
                fuzzy_autocorrect_enabled=True,
            )

        selected_voice, _ = self.user_voice_variant(source_user_id)
        settings = self._user_settings.get(source_user_id)
        return SpeechNormalisationContext(
            source_user_id=source_user_id,
            selected_voice=selected_voice,
            pronunciations=self.user_pronunciations(source_user_id, selected_voice),
            substitutions={} if settings is None else settings.substitutions,
            fuzzy_autocorrect_enabled=True if settings is None else settings.autocorrect,
        )

    @classmethod
    def _expand_compact_speech_token(cls, token: str) -> str:
        if match := COMPACT_UNIT_RE.fullmatch(token):
            lead, raw_value, raw_unit, tail = match.groups()
            forms = cls._COMPACT_UNIT_WORDS[raw_unit.lower()]
            return f"{lead}{raw_value} {cls._quantity_label(raw_value, forms)}{tail}"

        if match := SLASH_RATIO_RE.fullmatch(token):
            lead, left, right, tail = match.groups()
            return f"{lead}{left} out of {right}{tail}"

        return token

    @classmethod
    def _expand_currency_speech_text(cls, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            symbol = match.group("prefix") or match.group("suffix")
            raw_value = match.group("prefix_value") or match.group("suffix_value")
            if symbol is None or raw_value is None:
                return match.group(0)
            forms = cls._CURRENCY_SYMBOL_WORDS.get(symbol)
            if forms is None:
                return match.group(0)
            return f"{raw_value} {cls._quantity_label(raw_value, forms)}"

        return CURRENCY_AMOUNT_RE.sub(replace, text)

    @staticmethod
    def _quantity_label(raw_value: str, forms: SpokenQuantityForms) -> str:
        try:
            numeric_value = Decimal(raw_value)
        except InvalidOperation:
            return forms.plural
        return forms.singular if numeric_value == 1 else forms.plural

    @staticmethod
    def _expand_common_shorthand_token(token: str) -> str:
        if match := WITH_SHORTHAND_RE.fullmatch(token):
            lead, raw_core, tail = match.groups()
            core = raw_core.lower()
            replacement = "with" if core == "w/" else "without"
            return f"{lead}{replacement}{tail}"
        return token

    @classmethod
    def _replace_discord_formatting(cls, text: str) -> str:
        text = DISCORD_TIMESTAMP_RE.sub(" time code ", text)
        text = DISCORD_HEADING_RE.sub(lambda match: cls._replace_heading(match), text)
        text = DISCORD_CODE_BLOCK_RE.sub(lambda match: cls._markdown_replacement("code block", match), text)
        text = DISCORD_INLINE_CODE_RE.sub(lambda match: cls._markdown_replacement("code", match), text)

        replacements: tuple[tuple[re.Pattern[str], str], ...] = (
            (DISCORD_SPOILER_RE, "spoiler"),
            (DISCORD_STRIKETHROUGH_RE, "strikethrough"),
            (DISCORD_UNDERLINE_RE, "underline"),
            (DISCORD_TRIPLE_STAR_RE, "bold italic"),
            (DISCORD_BOLD_RE, "bold"),
            (DISCORD_ITALIC_STAR_RE, "italic"),
            (DISCORD_ITALIC_UNDERSCORE_RE, "italic"),
        )
        for pattern, label in replacements:
            text = pattern.sub(
                lambda match, replacement_label=label: cls._markdown_replacement(replacement_label, match), text
            )
        return text

    @staticmethod
    def _markdown_replacement(label: str, match: re.Match[str]) -> str:
        inner = re.sub(r"\s+", " ", match.group(1)).strip()
        if not inner:
            return " "
        return f" {label} {inner} "

    @staticmethod
    def _replace_heading(match: re.Match[str]) -> str:
        marker = match.group(1)
        body = re.sub(r"\s+", " ", match.group(2)).strip()
        if not body:
            return " "
        label = {
            "-#": "subtext",
            "#": "heading",
            "##": "heading 2",
            "###": "heading 3",
        }[marker]
        return f" {label} {body} "

    def _replace_link(
        self,
        raw_url: str,
        *,
        substitutions: Mapping[str, TextSubstitutionRule] | None = None,
    ) -> str:
        trimmed_url = raw_url.rstrip(".,!?;:")
        if not trimmed_url:
            return " "
        if substituted := self._url_substitution_target(trimmed_url, substitutions=substitutions):
            return f" {substituted} "

        parsed = urlparse(trimmed_url if "://" in trimmed_url else f"https://{trimmed_url}")
        hostname = parsed.hostname
        if not hostname:
            return " link "

        spoken = self._describe_link(hostname, parsed.path)
        return f" {spoken} "

    def _url_substitution_target(
        self,
        url: str,
        *,
        substitutions: Mapping[str, TextSubstitutionRule] | None = None,
    ) -> str | None:
        substitution_sources: tuple[Mapping[str, TextSubstitutionRule], ...] = tuple(
            source
            for source in (
                substitutions,
                self._text_corrections.slang,
                self._text_corrections.typos,
            )
            if source
        )
        for source_map in substitution_sources:
            rule = self._lookup_substitution_rule(url, source_map)
            if rule is not None:
                return rule.target
        return None

    def _describe_link(self, hostname: str, path: str) -> str:
        host_candidates = self._link_host_candidates(hostname)
        for host_candidate in host_candidates:
            for rule in self._voice_link_rules.rules:
                if rule.host != host_candidate:
                    continue
                match = rule.path_pattern.search(path)
                if match is None:
                    continue
                rendered = self._render_link_rule_template(rule.template, host_candidate, match)
                if rendered:
                    return rendered

        for host_candidate in host_candidates:
            host_label = self._voice_link_rules.host_labels.get(host_candidate)
            if host_label:
                return host_label
        return f"link {self._spoken_link_host(host_candidates[0])}"

    @staticmethod
    def _link_host_candidates(hostname: str) -> tuple[str, ...]:
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
        except (KeyError, ValueError):
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
        alias = cls._SPOKEN_LINK_HOST_ALIASES.get(normalised_host)
        if alias is not None:
            return alias
        if normalised_host.endswith(".com"):
            root = normalised_host[: -len(".com")]
            if "." not in root:
                return root
        return normalised_host

    def _truncate_speech_tokens(self, tokens: list[SpeechToken]) -> tuple[SpeechToken, ...]:
        trimmed: list[SpeechToken] = []
        total_len = 0

        for token in tokens:
            if not token.text:
                continue

            separator_len = 1 if trimmed else 0
            next_len = total_len + separator_len + token.rendered_len()
            if next_len <= self._MAX_SPOKEN_CHARS:
                trimmed.append(token)
                total_len = next_len
                continue

            if token.kind is SpeechTokenKind.EMOJI:
                break

            remaining = self._MAX_SPOKEN_CHARS - total_len - separator_len
            if remaining <= 0:
                break

            clipped = token.text[:remaining].rstrip()
            if clipped:
                trimmed.append(SpeechToken(clipped, token.kind, token.repeat_count))
            break

        return tuple(trimmed)

    @staticmethod
    def _batched_message_separator(previous: SpeechContent, current: SpeechContent) -> str:
        return " " if previous.ends_with_emoji() and current.starts_with_emoji() else "\n"

    @classmethod
    def _batched_message_additional_len(cls, previous: SpeechContent, current: SpeechContent) -> int:
        separator = cls._batched_message_separator(previous, current)
        previous_last = previous.last_token()
        current_first = current.first_token()
        if (
            separator == " "
            and previous_last is not None
            and current_first is not None
            and previous_last.can_merge_emoji_repeat(current_first)
        ):
            added_len = previous_last.merge_emoji_repeat(current_first).rendered_len() - previous_last.rendered_len()
            for token in current.tokens[1:]:
                if token.text:
                    added_len += 1 + token.rendered_len()
            return added_len

        return len(separator) + current.rendered_len()

    @classmethod
    def _render_batched_speech(cls, contents: Iterable[SpeechContent]) -> str:
        rendered: list[str] = []
        previous: SpeechContent | None = None
        last_token: SpeechToken | None = None

        for raw_content in contents:
            if not raw_content:
                continue
            current_tokens = [token for token in raw_content.tokens if token.text]
            if not current_tokens:
                continue

            if previous is None:
                rendered.append(current_tokens[0].render())
                last_token = current_tokens[0]
                start_index = 1
            else:
                separator = cls._batched_message_separator(previous, raw_content)
                first_token = current_tokens[0]
                if separator == " " and last_token is not None and last_token.can_merge_emoji_repeat(first_token):
                    last_token = last_token.merge_emoji_repeat(first_token)
                    rendered[-1] = last_token.render()
                    start_index = 1
                else:
                    rendered.extend((separator, first_token.render()))
                    last_token = first_token
                    start_index = 1

            for token in current_tokens[start_index:]:
                rendered.extend((" ", token.render()))
                last_token = token

            previous = raw_content

        return "".join(rendered)

    @staticmethod
    def _lookup_substitution_rule(
        source: str,
        substitutions: Mapping[str, TextSubstitutionRule],
    ) -> TextSubstitutionRule | None:
        direct = substitutions.get(source)
        if direct is not None and direct.case_sensitive:
            return direct

        lowered = substitutions.get(source.lower())
        if lowered is not None and not lowered.case_sensitive:
            return lowered

        if direct is not None and not direct.case_sensitive:
            return direct
        return None

    @classmethod
    def _apply_substitution_token(cls, token: str, substitutions: dict[str, TextSubstitutionRule]) -> str:
        match = SUBSTITUTION_TOKEN_RE.fullmatch(token)
        if not match:
            return token

        lead, core, tail = match.groups()
        rule = cls._lookup_substitution_rule(core, substitutions)
        if rule is None:
            return token
        return f"{lead}{rule.target}{tail}"

    def _apply_pronunciation_token(
        self,
        token: str,
        voice: str,
        pronunciations: dict[str, PronunciationOverride],
    ) -> str:
        match = SUBSTITUTION_TOKEN_RE.fullmatch(token)
        if not match:
            return token

        lead, core, tail = match.groups()
        replacement = pronunciations.get(core.lower())
        if replacement is None:
            return token
        return f"{lead}{self._render_pronunciation_override(voice, replacement)}{tail}"

    def _apply_exact_correction_token(self, token: str, corrections: dict[str, TextSubstitutionRule]) -> str:
        direct_rule = self._lookup_substitution_rule(token, corrections)
        if direct_rule is not None:
            return direct_rule.target if direct_rule.case_sensitive else self._match_token_case(direct_rule.target, token)

        match = SUBSTITUTION_TOKEN_RE.fullmatch(token)
        if not match:
            return token

        lead, core, tail = match.groups()
        rule = self._lookup_substitution_rule(core, corrections)
        if rule is None:
            return token
        replacement = rule.target if rule.case_sensitive else self._match_token_case(rule.target, core)
        return f"{lead}{replacement}{tail}"

    def _apply_fuzzy_typo_correction(self, token: str) -> str:
        match = SUBSTITUTION_TOKEN_RE.fullmatch(token)
        if not match:
            return token

        lead, core, tail = match.groups()
        candidate = self._best_fuzzy_typo_candidate(core)
        if candidate is None:
            return token
        log.debug(f"TTS fuzzy autocorrect token={core!r} replacement={candidate!r}")
        return f"{lead}{candidate}{tail}"

    def _best_fuzzy_typo_candidate(self, token: str) -> str | None:
        if not self._is_fuzzy_autocorrect_candidate(token):
            return None

        lower = token.lower()
        if lower in self._text_corrections.protected:
            return None

        best_candidate: str | None = None
        best_distance: int | None = None
        ambiguous = False
        max_distance = 1 if len(lower) <= 6 else 2

        for candidate in self._text_corrections.fuzzy_targets:
            if candidate == lower or abs(len(candidate) - len(lower)) > max_distance:
                continue

            distance = self._bounded_edit_distance(lower, candidate, max_distance)
            if distance is None:
                continue
            if best_distance is None or distance < best_distance:
                best_candidate = candidate
                best_distance = distance
                ambiguous = False
                continue
            if distance == best_distance:
                ambiguous = True

        if ambiguous or best_candidate is None:
            return None
        return best_candidate

    @classmethod
    def _is_fuzzy_autocorrect_candidate(cls, token: str) -> bool:
        return len(token) >= cls._FUZZY_AUTOCORRECT_MIN_LEN and token.isalpha() and token.islower()

    @staticmethod
    def _bounded_edit_distance(source: str, target: str, limit: int) -> int | None:
        if source == target:
            return 0
        if not source or not target:
            distance = max(len(source), len(target))
            return distance if distance <= limit else None
        if abs(len(source) - len(target)) > limit:
            return None

        previous = list(range(len(target) + 1))
        for source_index, source_char in enumerate(source, start=1):
            current = [source_index]
            row_min = current[0]
            for target_index, target_char in enumerate(target, start=1):
                substitution_cost = 0 if source_char == target_char else 1
                current_value = min(
                    previous[target_index] + 1,
                    current[target_index - 1] + 1,
                    previous[target_index - 1] + substitution_cost,
                )
                current.append(current_value)
                row_min = min(row_min, current_value)
            if row_min > limit:
                return None
            previous = current

        distance = previous[-1]
        return distance if distance <= limit else None

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
        return self._replace_mentions_with_context(
            text,
            source_user_id=int(event.author_id),
            guild_id=event.guild_id,
            member_mentions=message.get_member_mentions(),
            user_mentions=message.user_mentions,
            channel_mentions=message.channel_mentions,
        )

    def _replace_mentions_with_context(
        self,
        text: str,
        *,
        source_user_id: int,
        guild_id: hikari.Snowflake | None,
        member_mentions: Mapping[hikari.Snowflake, object] | hikari.UndefinedType = hikari.UNDEFINED,
        user_mentions: Mapping[hikari.Snowflake, object] | hikari.UndefinedType = hikari.UNDEFINED,
        channel_mentions: Mapping[hikari.Snowflake, object] | hikari.UndefinedType = hikari.UNDEFINED,
    ) -> str:
        user_mention_overrides = self._user_settings.get(source_user_id, UserVoiceSettings()).mention_overrides
        global_mention_overrides = self._text_corrections.mention_overrides

        def resolved_username(member: object | None = None, user: object | None = None) -> str | None:
            if member is not None:
                name = getattr(member, "username", None)
                if isinstance(name, str) and name:
                    return name
                member_user = getattr(member, "user", None)
                nested_name = getattr(member_user, "username", None)
                if isinstance(nested_name, str) and nested_name:
                    return nested_name
            if user is not None:
                name = getattr(user, "username", None)
                if isinstance(name, str) and name:
                    return name
            return None

        def user_name(match: re.Match[str]) -> str:
            user_id = hikari.Snowflake(int(match.group(1)))
            display_name: str | None = None
            username: str | None = None

            if member_mentions is not hikari.UNDEFINED and (member := member_mentions.get(user_id)):
                display_name = getattr(member, "display_name", None)
                username = resolved_username(member=member)
            elif user_mentions is not hikari.UNDEFINED and (user := user_mentions.get(user_id)):
                username = resolved_username(user=user)
                display_name = getattr(user, "display_name", None) or username
            elif guild_id is not None and (member := self.bot.cache.get_member(guild_id, user_id)):
                display_name = member.display_name
                username = resolved_username(member=member)
            elif user := self.bot.cache.get_user(user_id):
                username = resolved_username(user=user)
                display_name = user.display_name or username

            override = user_mention_overrides.get(int(user_id))
            if override is not None:
                return f" {override} "

            if display_name is not None and username is not None and display_name == username:
                global_override = global_mention_overrides.get(int(user_id))
                if global_override is not None:
                    return f" {global_override} "

            return f" {display_name or username or 'user'} "

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

    def _emoji_tag_to_words(
        self,
        tag: str,
        voice: str,
        pronunciations: dict[str, PronunciationOverride] | None = None,
        substitutions: dict[str, TextSubstitutionRule] | None = None,
    ) -> str:
        raw_name = tag.strip(":").strip().lower()
        for key in self._emoji_tag_substitution_keys(raw_name):
            pronunciation = pronunciations.get(key) if pronunciations else None
            replacement = self._render_pronunciation_override(voice, pronunciation) if pronunciation else None
            if replacement is None:
                substitution = self._lookup_substitution_rule(key, substitutions) if substitutions else None
                replacement = substitution.target if substitution is not None else None
            if replacement is None:
                substitution = self._lookup_substitution_rule(key, self._text_corrections.slang)
                replacement = substitution.target if substitution is not None else None
            if replacement is None:
                substitution = self._lookup_substitution_rule(key, self._text_corrections.typos)
                replacement = substitution.target if substitution is not None else None
            if replacement is not None:
                return replacement

        name = raw_name.replace("_", " ").replace("-", " ")
        return name.strip() or "emoji"

    def _render_pronunciation_override(self, voice: str, entry: PronunciationOverride) -> str:
        if entry.format is PronunciationFormat.TEXT:
            return entry.value
        if self.voice_supports_ipa_pronunciations(voice):
            return f"[[{entry.value}]]"
        log.warning(
            f"TTS ignored unsupported IPA pronunciation voice={voice!r} value={self._preview(entry.value)!r}"
        )
        return entry.value

    @staticmethod
    def _emoji_tag_substitution_keys(raw_name: str) -> tuple[str, ...]:
        keys = [raw_name]
        underscore = raw_name.replace("-", "_")
        hyphen = raw_name.replace("_", "-")
        if underscore not in keys:
            keys.append(underscore)
        if hyphen not in keys:
            keys.append(hyphen)
        return tuple(keys)


# AiviA APasz
