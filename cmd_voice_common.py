from __future__ import annotations

import asyncio
import enum
import logging
import re
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import hikari
import hikariwave
import lightbulb

log = logging.getLogger(__name__)

group_voice = lightbulb.Group("voice", "Voice commands and TTS")  # type: ignore

VOICE_USERS_FILE = Path("voice_users.json")
VOICE_CORRECTIONS_FILE = Path("voice_corrections.json")
VOICE_TARGET_LABELS_FILE = Path("voice_target_labels.json")
VOICE_LINK_RULES_FILE = Path("voice_link_rules.json")
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
MAX_TTS_VOICES = 25


@dataclass(slots=True, frozen=True)
class VoiceJob:
    guild_id: hikari.Snowflake
    message_id: hikari.Snowflake
    speech: SpeechContent
    voice: str
    variant: str | None


@dataclass(slots=True)
class ActivePlayback:
    jobs: tuple[VoiceJob, ...]
    text: str
    connection: hikariwave.VoiceConnection
    source: hikariwave.AudioSource
    begin_timeout_seconds: float
    timeout_seconds: float
    expected_duration_seconds: float | None
    started_at: float | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    monitor_task: asyncio.Task[None] | None = None


class PlaybackWaitResult(enum.StrEnum):
    JOB = "job"
    DONE = "done"
    TIMEOUT = "timeout"


class SpeechTokenKind(enum.StrEnum):
    TEXT = "text"
    EMOJI = "emoji"


@dataclass(slots=True, frozen=True)
class SpeechToken:
    text: str
    kind: SpeechTokenKind
    repeat_count: int = 1

    def render(self) -> str:
        if self.kind is SpeechTokenKind.EMOJI and self.repeat_count > 1:
            return f"{self.text} x{self.repeat_count}"
        return self.text

    def rendered_len(self) -> int:
        return len(self.render())

    def can_merge_emoji_repeat(self, other: SpeechToken) -> bool:
        return self.kind is SpeechTokenKind.EMOJI and other.kind is SpeechTokenKind.EMOJI and self.text == other.text

    def merge_emoji_repeat(self, other: SpeechToken) -> SpeechToken:
        if not self.can_merge_emoji_repeat(other):
            raise ValueError("Speech tokens are not merge-compatible emoji repeats.")
        return SpeechToken(self.text, self.kind, self.repeat_count + other.repeat_count)


@dataclass(slots=True, frozen=True)
class SpeechContent:
    tokens: tuple[SpeechToken, ...]

    def __bool__(self) -> bool:
        return bool(self.tokens)

    def render(self) -> str:
        return " ".join(token.render() for token in self.tokens if token.text)

    def rendered_len(self) -> int:
        return len(self.render())

    def starts_with_emoji(self) -> bool:
        return bool(self.tokens and self.tokens[0].kind is SpeechTokenKind.EMOJI)

    def ends_with_emoji(self) -> bool:
        return bool(self.tokens and self.tokens[-1].kind is SpeechTokenKind.EMOJI)

    def first_token(self) -> SpeechToken | None:
        return self.tokens[0] if self.tokens else None

    def last_token(self) -> SpeechToken | None:
        return self.tokens[-1] if self.tokens else None


@dataclass(slots=True)
class PiperPythonVoiceRuntime:
    raw_voice: Any
    synthesis_config_factory: Callable[..., Any] | None = None

    def synthesize_to_wav(self, text: str, wav_file: wave.Wave_write, speaker_id: int | None = None) -> None:
        synthesize_wav = getattr(self.raw_voice, "synthesize_wav", None)
        syn_config = self._synthesis_config(speaker_id)
        if callable(synthesize_wav):
            try:
                if syn_config is None:
                    synthesize_wav(text, wav_file)
                else:
                    synthesize_wav(text, wav_file, syn_config=syn_config)
                return
            except TypeError:
                pass

        synthesize = getattr(self.raw_voice, "synthesize", None)
        if callable(synthesize):
            try:
                maybe_chunks = synthesize(text, syn_config=syn_config)
            except TypeError:
                maybe_chunks = synthesize(text)

            chunks = cast(Iterable[Any], maybe_chunks)
            first_chunk = True
            for chunk in chunks:
                sample_rate = getattr(chunk, "sample_rate", None)
                sample_width = getattr(chunk, "sample_width", None)
                sample_channels = getattr(chunk, "sample_channels", None)
                audio_bytes = getattr(chunk, "audio_int16_bytes", None)
                if not isinstance(audio_bytes, (bytes, bytearray, memoryview)):
                    raise RuntimeError("Piper Python synth chunk has no readable PCM bytes.")
                if first_chunk:
                    if not isinstance(sample_rate, int) or sample_rate <= 0:
                        raise RuntimeError("Piper Python synth chunk has no valid sample rate.")
                    if not isinstance(sample_width, int) or sample_width <= 0:
                        raise RuntimeError("Piper Python synth chunk has no valid sample width.")
                    if not isinstance(sample_channels, int) or sample_channels <= 0:
                        raise RuntimeError("Piper Python synth chunk has no valid channel count.")
                    wav_file.setframerate(sample_rate)
                    wav_file.setsampwidth(sample_width)
                    wav_file.setnchannels(sample_channels)
                    first_chunk = False
                wav_file.writeframes(bytes(audio_bytes))

            if not first_chunk:
                return

        synthesize_stream_raw = getattr(self.raw_voice, "synthesize_stream_raw", None)
        if callable(synthesize_stream_raw):
            sample_rate = self.sample_rate()
            if sample_rate is None:
                raise RuntimeError("Piper Python voice has no readable sample rate for raw streaming output.")
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            raw_chunks = cast(Iterable[bytes | bytearray | memoryview], synthesize_stream_raw(text))
            for chunk in raw_chunks:
                wav_file.writeframes(bytes(chunk))
            return

        raise RuntimeError("Unsupported Piper Python voice API.")

    def sample_rate(self) -> int | None:
        config = getattr(self.raw_voice, "config", None)
        sample_rate = getattr(config, "sample_rate", None)
        return int(sample_rate) if isinstance(sample_rate, (int, float)) and sample_rate > 0 else None

    def _synthesis_config(self, speaker_id: int | None) -> Any | None:
        if speaker_id is None or self.synthesis_config_factory is None:
            return None
        return self.synthesis_config_factory(speaker_id=speaker_id)


@dataclass(slots=True, frozen=True)
class HFRepoRef:
    repo_id: str
    revision: str
    onnx_file: str | None = None


class PronunciationFormat(enum.StrEnum):
    TEXT = "text"
    IPA = "ipa"


@dataclass(slots=True, frozen=True)
class PronunciationOverride:
    format: PronunciationFormat
    value: str


@dataclass(slots=True, frozen=True)
class TextSubstitutionRule:
    source: str
    target: str
    case_sensitive: bool = False


@dataclass(slots=True)
class UserVoiceSettings:
    enabled: bool = False
    autocorrect: bool = True
    voice: str | None = None
    variant: str | None = None
    pronunciations: dict[str, dict[str, PronunciationOverride]] = field(default_factory=dict)
    mention_overrides: dict[int, str] = field(default_factory=dict)
    substitutions: dict[str, TextSubstitutionRule] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class TextCorrectionCatalog:
    slang: dict[str, TextSubstitutionRule] = field(default_factory=dict)
    typos: dict[str, TextSubstitutionRule] = field(default_factory=dict)
    pronunciations: dict[str, dict[str, PronunciationOverride]] = field(default_factory=dict)
    mention_overrides: dict[int, str] = field(default_factory=dict)
    protected: frozenset[str] = field(default_factory=frozenset)
    fuzzy_targets: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class VoiceConnectBackoff:
    retry_at_monotonic: float
    listener_count: int
    reason: str
    detail: str


class VoiceLinkRuleMode(enum.StrEnum):
    SIMPLE = "simple"
    REGEX = "regex"


@dataclass(slots=True, frozen=True)
class VoiceLinkRuleDraft:
    mode: VoiceLinkRuleMode
    host: str
    example_url: str
    input_pattern: str
    compiled_path_regex: str
    template: str


@dataclass(slots=True, frozen=True)
class VoiceLinkRegexRuleSpec:
    host: str
    path_regex: str
    template: str
    example_url: str | None = None


@dataclass(slots=True, frozen=True)
class VoiceLinkSimpleRuleSpec:
    host: str
    path_shape: str
    template: str
    example_url: str | None = None


@dataclass(slots=True, frozen=True)
class VoiceLinkRule:
    spec: VoiceLinkRegexRuleSpec | VoiceLinkSimpleRuleSpec
    compiled_path_regex: str
    path_pattern: re.Pattern[str]

    @property
    def host(self) -> str:
        return self.spec.host

    @property
    def template(self) -> str:
        return self.spec.template

    @property
    def example_url(self) -> str | None:
        return self.spec.example_url

    @property
    def mode(self) -> VoiceLinkRuleMode:
        if isinstance(self.spec, VoiceLinkSimpleRuleSpec):
            return VoiceLinkRuleMode.SIMPLE
        return VoiceLinkRuleMode.REGEX

    @property
    def path_regex(self) -> str:
        return self.compiled_path_regex

    @property
    def path_shape(self) -> str | None:
        if isinstance(self.spec, VoiceLinkSimpleRuleSpec):
            return self.spec.path_shape
        return None

    @property
    def input_pattern(self) -> str:
        if isinstance(self.spec, VoiceLinkSimpleRuleSpec):
            return self.spec.path_shape
        return self.spec.path_regex


@dataclass(slots=True, frozen=True, init=False)
class VoiceLinkRules:
    host_labels: dict[str, str]
    rules: tuple[VoiceLinkRule, ...]

    def __init__(
        self,
        host_labels: dict[str, str] | None = None,
        rules: tuple[VoiceLinkRule, ...] = (),
    ) -> None:
        object.__setattr__(self, "host_labels", {} if host_labels is None else host_labels)
        object.__setattr__(self, "rules", rules)


@dataclass(slots=True, frozen=True)
class VoiceRuntimeResetResult:
    outstanding_job_count: int
    active_connection_count: int
    targeted_guild_count: int
    backoff_count: int
    worker_restarted: bool


class VoiceTTSService:
    _MAX_SPOKEN_CHARS = 550
    _LOG_PREVIEW_CHARS = 120
    _MAX_BACKLOG_JOBS = 64
    _VARIANT_CLEAR_VALUES = frozenset({"none", "off", "clear", "default"})
    _VOICE_CONNECT_TIMEOUT_SECONDS = 20.0
    _VOICE_CONNECT_FAILURE_COOLDOWN_SECONDS = 20.0


# AiviA APasz
