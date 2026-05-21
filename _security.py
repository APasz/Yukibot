import logging
from enum import IntEnum
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import NoReturn, overload

import config

log = logging.getLogger(__name__)


class Power_Level(IntEnum):
    guest = 0
    user = 10
    admin = 20
    sudo = 30
    root = 40


class Access_Control:
    LvL = Power_Level
    _LEVEL_ALIASES: dict[str, Power_Level] = {
        "guest": Power_Level.guest,
        "guests": Power_Level.guest,
        "user": Power_Level.user,
        "users": Power_Level.user,
        "admin": Power_Level.admin,
        "admins": Power_Level.admin,
        "sudo": Power_Level.sudo,
        "sudoers": Power_Level.sudo,
        "root": Power_Level.root,
        "roots": Power_Level.root,
    }
    _LEGACY_NUMERIC_LEVELS: dict[int, Power_Level] = {
        0: Power_Level.guest,
        1: Power_Level.user,
        2: Power_Level.sudo,
        3: Power_Level.root,
    }
    _ORDERED_LEVELS: tuple[Power_Level, ...] = (
        Power_Level.guest,
        Power_Level.user,
        Power_Level.admin,
        Power_Level.sudo,
        Power_Level.root,
    )
    _WRITABLE_LEVELS: tuple[Power_Level, ...] = (
        Power_Level.user,
        Power_Level.admin,
        Power_Level.sudo,
        Power_Level.root,
    )

    def __init__(self, pointer: Path = Path("users.json")):
        self.pointer = pointer
        self._roles: dict[int, Power_Level] = {}
        self._guests_enabled = getattr(config, "GUESTS_ALLOWED", True)
        self.reload()

    @classmethod
    def _to_level(cls, value: int | str) -> Power_Level | None:
        if isinstance(value, str):
            string = value.casefold()
            name_map: dict[str, Power_Level] = {lvl.name.casefold(): lvl for lvl in Power_Level}
            name_map.update(cls._LEVEL_ALIASES)
            if string in name_map:
                return name_map[string]
            try:
                numeric = int(value)
            except Exception:
                return None
            try:
                return Power_Level(numeric)
            except ValueError:
                return cls._LEGACY_NUMERIC_LEVELS.get(numeric)
        try:
            return Power_Level(value)
        except ValueError:
            return cls._LEGACY_NUMERIC_LEVELS.get(value)

    @staticmethod
    def _to_user_id(ident: int | str) -> int | None:
        if isinstance(ident, int):
            return ident
        if isinstance(ident, str):
            ident = ident.strip()
            if ident.isdigit():
                return int(ident)
        return None

    @classmethod
    def _parse_roles(cls, raw: object) -> tuple[dict[int, Power_Level], tuple[str, ...]]:
        if not isinstance(raw, dict):
            raise TypeError(f"Authority users payload must be a JSON object, got {type(raw).__name__}")

        problems: set[str] = set()
        roles: dict[int, Power_Level] = {}
        for lvl_key, ids in raw.items():
            lvl = cls._to_level(lvl_key)
            if lvl is None:
                problems.add(f"Unknown level {lvl_key!r}: skipping group")
                continue
            if not isinstance(ids, list):
                problems.add(f"Level {lvl_key!r} should map to a list, got {type(ids).__name__}: skipping")
                continue

            for entry in ids:
                uid = cls._to_user_id(entry)
                if uid is None:
                    problems.add(f"Bad user id {entry!r} under {lvl_key!r}: skipping")
                    continue

                prev = roles.get(uid)
                if prev is None or lvl > prev:
                    if prev is not None and lvl != prev:
                        problems.add(f"User {uid} listed at {prev.name} and {lvl.name}: taking highest ({lvl.name})")
                    roles[uid] = lvl

        return roles, tuple(sorted(problems))

    @classmethod
    def _serializable_roles(cls, roles: dict[int, Power_Level]) -> dict[str, list[int]]:
        grouped: dict[str, list[int]] = {level.name: [] for level in cls._WRITABLE_LEVELS}
        for user_id, level in sorted(roles.items()):
            if level in cls._WRITABLE_LEVELS:
                grouped[level.name].append(user_id)
        return grouped

    @classmethod
    def _highest_manageable_level(cls, actor_level: Power_Level) -> Power_Level | None:
        if actor_level >= Power_Level.root:
            return Power_Level.sudo
        if actor_level >= Power_Level.sudo:
            return Power_Level.admin
        if actor_level >= Power_Level.admin:
            return Power_Level.user
        return None

    @classmethod
    def _next_level(cls, level: Power_Level) -> Power_Level | None:
        try:
            index = cls._ORDERED_LEVELS.index(level)
        except ValueError:
            return None
        if index + 1 >= len(cls._ORDERED_LEVELS):
            return None
        return cls._ORDERED_LEVELS[index + 1]

    @classmethod
    def _prev_level(cls, level: Power_Level) -> Power_Level | None:
        try:
            index = cls._ORDERED_LEVELS.index(level)
        except ValueError:
            return None
        if index == 0:
            return None
        return cls._ORDERED_LEVELS[index - 1]

    def reload(self) -> bool:
        raw: object = {}
        try:
            raw = config.load_authority_json(config.AuthorityResource.USERS, self.pointer)
        except Exception as e:
            log.exception(f"Failed to load permissions from authority/source {self.pointer}: {e}")
            return False

        roles, problems = self._parse_roles(raw)
        for p in problems:
            log.warning(p)
        self._roles = roles
        return True

    def level_of(self, user_id: int) -> Power_Level:
        return self._roles.get(int(user_id), Power_Level.guest)

    def can(self, user_id: int, required: Power_Level) -> bool:
        usr_lvl = self.level_of(user_id)
        if not self._guests_enabled and usr_lvl == Power_Level.guest:
            return False
        return usr_lvl >= required

    def serializable(self) -> Mapping[str, list[int]]:
        return self._serializable_roles(self._roles)

    def explicit_roles(self) -> dict[int, Power_Level]:
        return dict(self._roles)

    def highest_manageable_level(self, actor_user_id: int) -> Power_Level | None:
        return self._highest_manageable_level(self.level_of(actor_user_id))

    def can_manage_target(self, actor_user_id: int, target_user_id: int) -> bool:
        if int(actor_user_id) == int(target_user_id):
            return False

        actor_level = self.level_of(actor_user_id)
        highest_manageable = self._highest_manageable_level(actor_level)
        if highest_manageable is None:
            return False

        current_level = self.level_of(target_user_id)
        return current_level <= highest_manageable and current_level < actor_level

    def next_promoted_level(self, actor_user_id: int, target_user_id: int) -> Power_Level | None:
        actor_level = self.level_of(actor_user_id)
        highest_manageable = self._highest_manageable_level(actor_level)
        if highest_manageable is None or int(actor_user_id) == int(target_user_id):
            return None

        current_level = self.level_of(target_user_id)
        next_level = self._next_level(current_level)
        if next_level is None or next_level > highest_manageable:
            return None
        if current_level >= actor_level:
            return None
        return next_level

    def next_demoted_level(self, actor_user_id: int, target_user_id: int) -> Power_Level | None:
        actor_level = self.level_of(actor_user_id)
        highest_manageable = self._highest_manageable_level(actor_level)
        if highest_manageable is None or int(actor_user_id) == int(target_user_id):
            return None

        current_level = self.level_of(target_user_id)
        previous_level = self._prev_level(current_level)
        if previous_level is None or current_level > highest_manageable:
            return None
        if current_level >= actor_level:
            return None
        return previous_level

    def promote(self, actor_user_id: int, target_user_id: int) -> Power_Level:
        actor_level = self.level_of(actor_user_id)
        highest_manageable = self._highest_manageable_level(actor_level)
        if highest_manageable is None:
            raise PermissionError("You are not allowed to promote users.")

        current_level = self.level_of(target_user_id)
        next_level = self._next_level(current_level)
        if next_level is None:
            raise ValueError("That user is already at the highest level.")
        if next_level > highest_manageable:
            raise PermissionError(f"You can only promote users up to {highest_manageable.name.title()}.")
        if int(actor_user_id) == int(target_user_id):
            raise PermissionError("You cannot change your own privilege level.")
        if current_level >= actor_level:
            raise PermissionError("You cannot change a user with your own level or higher.")

        self._write_level(target_user_id, next_level)
        return next_level

    def demote(self, actor_user_id: int, target_user_id: int) -> Power_Level:
        actor_level = self.level_of(actor_user_id)
        highest_manageable = self._highest_manageable_level(actor_level)
        if highest_manageable is None:
            raise PermissionError("You are not allowed to demote users.")

        current_level = self.level_of(target_user_id)
        previous_level = self._prev_level(current_level)
        if previous_level is None:
            raise ValueError("Guest users cannot be demoted.")
        if current_level > highest_manageable:
            raise PermissionError(f"You can only demote users up to {highest_manageable.name.title()}.")
        if int(actor_user_id) == int(target_user_id):
            raise PermissionError("You cannot change your own privilege level.")
        if current_level >= actor_level:
            raise PermissionError("You cannot change a user with your own level or higher.")

        self._write_level(target_user_id, previous_level)
        return previous_level

    def demote_to_guest_many(self, actor_user_id: int, target_user_ids: Iterable[int]) -> tuple[int, ...]:
        actor_level = self.level_of(actor_user_id)
        highest_manageable = self._highest_manageable_level(actor_level)
        if highest_manageable is None:
            raise PermissionError("You are not allowed to demote users.")

        unique_target_ids = tuple(sorted({int(user_id) for user_id in target_user_ids}))
        if not unique_target_ids:
            return ()

        removable: list[int] = []
        for target_user_id in unique_target_ids:
            current_level = self.level_of(target_user_id)
            if current_level is Power_Level.guest:
                continue
            if target_user_id == int(actor_user_id):
                continue
            if current_level > highest_manageable:
                continue
            if current_level >= actor_level:
                continue
            removable.append(target_user_id)

        if not removable:
            return ()

        next_roles = dict(self._roles)
        for target_user_id in removable:
            next_roles.pop(target_user_id, None)

        self._write_roles(next_roles)
        return tuple(removable)

    def _write_level(self, target_user_id: int, level: Power_Level) -> None:
        next_roles = dict(self._roles)
        if level is Power_Level.guest:
            next_roles.pop(int(target_user_id), None)
        else:
            next_roles[int(target_user_id)] = level

        self._write_roles(next_roles)

    def _write_roles(self, roles: dict[int, Power_Level]) -> None:
        payload = self._serializable_roles(roles)
        saved = config.save_authority_json(config.AuthorityResource.USERS, self.pointer, payload)
        parsed_roles, problems = self._parse_roles(saved)
        for problem in problems:
            log.warning(problem)
        self._roles = parsed_roles

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
