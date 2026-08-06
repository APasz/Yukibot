"""ComputerCraft client assets for Yukibot update mirrors."""

from __future__ import annotations

from pathlib import Path
from typing import Final

COMPUTERCRAFT_MIRROR_STATE_ROOT: Final[str] = "/.yukibot_mirrors"
COMPUTERCRAFT_MIRROR_STARTUP_DISPATCHER_PATH: Final[str] = f"{COMPUTERCRAFT_MIRROR_STATE_ROOT}/_startup.lua"

_STATE_ROOT_TOKEN: Final[str] = "__YUKIBOT_MIRROR_STATE_ROOT__"
_INSTALLER_TEMPLATE_PATH: Final[Path] = Path(__file__).with_name("computercraft_mirror_installer.lua")


def _load_installer() -> str:
    try:
        template = _INSTALLER_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as xcp:
        raise RuntimeError(f"Unable to load ComputerCraft mirror installer: {_INSTALLER_TEMPLATE_PATH}") from xcp
    if template.count(_STATE_ROOT_TOKEN) != 1:
        raise RuntimeError("ComputerCraft mirror installer must contain exactly one state-root placeholder.")
    return template.replace(_STATE_ROOT_TOKEN, COMPUTERCRAFT_MIRROR_STATE_ROOT)


COMPUTERCRAFT_MIRROR_INSTALLER: Final[str] = _load_installer()
