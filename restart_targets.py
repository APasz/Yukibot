from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final


class RestartTarget(StrEnum):
    BOT = "bot"
    VOICE = "voice"
    SYSTEM = "system"
    PORTAL = "portal"


PORTAL_SYSTEMD_UNIT: Final[str] = "yukiportal.service"


def coalesce_restart_targets(targets: Iterable[RestartTarget]) -> RestartTarget | None:
    target_set: set[RestartTarget] = set[RestartTarget](targets)
    for target in (RestartTarget.SYSTEM, RestartTarget.BOT, RestartTarget.VOICE, RestartTarget.PORTAL):
        if target in target_set:
            return target
    return None
