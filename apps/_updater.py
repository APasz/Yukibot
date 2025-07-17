from __future__ import annotations
import logging

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps._app import App

log = logging.getLogger(__name__)


class UnsupportedUpdate(Exception): ...


class Update_Manager:
    version: tuple[int, ...] | None = None

    def __init__(self, app: "App", *, base: bool = False, mods: bool = False) -> None:
        self.app = app
        self.can_base = base
        self.can_mods = mods if app.mods else False

    @staticmethod
    def stringise(version: tuple[int, ...]) -> str:
        return ".".join(map(str, version))

    @staticmethod
    def extract_version(line: str, regex: re.Pattern[str]) -> tuple[int, ...] | None:
        match = regex.search(line)
        ver = match.group(1) if match else None
        return tuple(map(int, ver.split("."))) if ver else None

    async def base(self) -> str | None:
        if not self.can_base:
            raise UnsupportedUpdate("Base updating not supported")

    async def mods(self) -> list[str] | None:
        if not self.can_mods:
            raise UnsupportedUpdate("Mod updating not supported")

# AiviA APasz