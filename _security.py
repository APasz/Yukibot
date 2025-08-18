from enum import IntEnum
import json
import logging
from pathlib import Path
from typing import NoReturn, overload

import config

log = logging.getLogger(__name__)


class Power_Level(IntEnum):
    guest = 0
    user = 1
    sudo = 2
    root = 3


class Access_Control:
    LvL = Power_Level

    def __init__(self, pointer: Path = Path("users.json")):
        self._roles: dict[int, Power_Level] = {}
        self._guests_enabled = getattr(config, "GUESTS_ALLOWED", True)

        raw: dict[str, list[int | str]] = {}
        if not pointer.exists():
            log.error(f"Permissions file not found @ {pointer}")
        else:
            try:
                raw = json.loads(pointer.read_text(config.STR_ENCODE))
                if not isinstance(raw, dict):
                    raise TypeError("Top-level JSON must be an object {level: [ids...]}")
            except Exception as e:
                log.exception(f"Failed to load {pointer}: {e}")
                return

        problems: set[str] = set()
        name_map: dict[str, Power_Level] = {lvl.name.casefold(): lvl for lvl in Power_Level}

        def _to_level(value: int | str) -> Power_Level | None:
            if isinstance(value, str):
                string = value.casefold()
                if string in name_map:
                    return name_map[string]
                try:
                    return Power_Level(int(value))
                except Exception:
                    return None
            if isinstance(value, int):
                try:
                    return Power_Level(value)
                except ValueError:
                    return None
            return None

        def _to_user_id(ident: int | str) -> int | None:
            if isinstance(ident, int):
                return ident
            if isinstance(ident, str):
                ident = ident.strip()
                if ident.isdigit():
                    return int(ident)
            return None

        for lvl_key, ids in raw.items():
            lvl = _to_level(lvl_key)
            if lvl is None:
                problems.add(f"Unknown level {lvl_key!r}: skipping group")
                continue
            if not isinstance(ids, list):
                problems.add(f"Level {lvl_key!r} should map to a list, got {type(ids).__name__}: skipping")
                continue

            for entry in ids:
                uid = _to_user_id(entry)
                if uid is None:
                    problems.add(f"Bad user id {entry!r} under {lvl_key!r}: skipping")
                    continue

                prev = self._roles.get(uid)
                if prev is None or lvl > prev:
                    if prev is not None and lvl != prev:
                        problems.add(f"User {uid} listed at {prev.name} and {lvl.name}: taking highest ({lvl.name})")
                    self._roles[uid] = lvl

        for p in problems:
            log.warning(p)

    def level_of(self, user_id: int) -> Power_Level:
        return self._roles.get(int(user_id), Power_Level.guest)

    def can(self, user_id: int, required: Power_Level) -> bool:
        if required == Power_Level.guest and not self._guests_enabled:
            return False
        return self.level_of(user_id) >= required

    @overload
    async def perm_check(self, user_id: int, required: Power_Level, *, silent: bool = False) -> NoReturn: ...

    @overload
    async def perm_check(self, user_id: int, required: Power_Level, *, silent: bool = True) -> bool: ...

    async def perm_check(self, user_id: int, required: Power_Level, *, silent: bool = False):
        ok = self.can(user_id, required)
        if silent:
            return ok
        if not ok:
            raise PermissionError(
                f"Insufficient level: {self.level_of(user_id).name.title()} < {required.name.title()}"
            )


# AiviA APasz
