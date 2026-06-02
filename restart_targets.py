from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class RestartTarget(StrEnum):
    BOT = "bot"
    VOICE = "voice"
    SYSTEM = "system"


def coalesce_restart_targets(targets: Iterable[RestartTarget]) -> RestartTarget | None:
    target_set = set(targets)
    for target in (RestartTarget.SYSTEM, RestartTarget.BOT, RestartTarget.VOICE):
        if target in target_set:
            return target
    return None
