from __future__ import annotations

from cmd_voice_common import (
    HFRepoRef,
    PiperPythonVoiceRuntime,
    SpeechContent,
    VoiceJob,
    VoiceRuntimeResetResult,
    group_voice,
    log,
)
from cmd_voice_core import VoiceTTSCoreMixin
from cmd_voice_model import VoiceTTSModelMixin
from cmd_voice_runtime import VoiceTTSRuntimeMixin


class VoiceTTSService(VoiceTTSCoreMixin, VoiceTTSRuntimeMixin, VoiceTTSModelMixin):
    _MAX_SPOKEN_CHARS = 550
    _LOG_PREVIEW_CHARS = 120
    _MAX_BACKLOG_JOBS = 64
    _VARIANT_CLEAR_VALUES = frozenset({"none", "off", "clear", "default"})
    _VOICE_CONNECT_TIMEOUT_SECONDS = 20.0
    _VOICE_CONNECT_FAILURE_COOLDOWN_SECONDS = 20.0
    _VOICE_UDP_DISCOVERY_COOLDOWN_SECONDS = 90.0
    _QUEUE_BATCH_WINDOW_SECONDS = 0.35
    _QUEUE_LATE_JOIN_TAIL_MIN_SECONDS = 0.35
    _QUEUE_LATE_JOIN_TAIL_MAX_SECONDS = 1.25
    _QUEUE_LATE_JOIN_TAIL_RATIO = 0.18
    _MAX_BATCHED_JOBS = 12
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 40
    _MAX_SUBSTITUTION_VALUE_CHARS = 120
    _FUZZY_AUTOCORRECT_MIN_LEN = 4


__all__ = [
    "HFRepoRef",
    "PiperPythonVoiceRuntime",
    "SpeechContent",
    "VoiceJob",
    "VoiceRuntimeResetResult",
    "VoiceTTSService",
    "group_voice",
    "log",
]
# AiviA APasz
