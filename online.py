from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import hikari
import lightbulb
from pydantic import BaseModel, ConfigDict, Field, field_validator

import config
from _discord import Distils
from _file import File_Utils
from _security import Access_Control
from config import Name_Cache

log = logging.getLogger(__name__)

group_online = lightbulb.Group("online", "Presence and activity tracking")  # type: ignore

PLATFORMS = ("desktop", "mobile", "web")
STATUS_TYPES = (
    "online-desktop",
    "online-mobile",
    "online-web",
    "dnd-desktop",
    "dnd-mobile",
    "dnd-web",
    "idle-desktop",
    "idle-mobile",
    "idle-web",
    "offline",
)
STATUS_TYPES_SET = set(STATUS_TYPES)

ACTIVITY_TYPES = ("games", "music", "streaming", "other")
ACTIVITY_TYPES_SET = set(ACTIVITY_TYPES)
ONLINE_STATUSES = {"online", "idle", "dnd"}
NICKNAME_MODES = ("online", "offline", "idle", "dnd")
NICKNAME_MODES_SET = set(NICKNAME_MODES)
NICKNAME_PLATFORMS = ("all", "desktop", "mobile", "web")
NICKNAME_PLATFORM_SET = set(NICKNAME_PLATFORMS)
NICKNAME_PLATFORM_PRIORITY = ("mobile", "desktop", "web")
NICKNAME_CHANGE_DELAY = timedelta(seconds=35)
NICKNAME_RETRY_DELAY = timedelta(seconds=90)
DRINK_MODES = {"include", "exclude"}
DRINK_REMINDER_INTERVAL = timedelta(minutes=45)
DRINK_REMINDER_JITTER_MAX = timedelta(minutes=5)
DRINK_REMINDERS = (
    "Hydration check: drink some water",
    "Water break time. Have a sip",
    "Quick reminder: stay hydrated",
    "Hydrate up. You can spare 10 seconds",
    "Pause for water, then continue",
    "Drink check: take a few sips",
    "Your future self thanks you for drinking water",
    "Water ping: refill and sip",
    "Hydration moment: grab water",
    "Sip break detected",
    "Reminder: keep water nearby",
    "Stay sharp, stay hydrated",
    "Hydration check",
    "Drink water",
    "Dink now",
    "Be plant",
    "Go be good and water yourself",
    "Sweetheart drink something :3",
    "Hydration check: drink some water",
    "You've been playing for {duration} it's water break time. Have a sip",
    "Quick reminder; {duration}: stay hydrated",
    "Hydrate up {duration}. You can spare 10 seconds",
    "Pause for water after {duration}, then continue",
    "Drink check {duration}: take a few sips",
    "Your future self says thanks for drinking water for {duration}",
    "Water ping {duration}: refill and sip",
    "Hydration moment {duration}: grab water",
    "Sip break detected after {duration}",
    "Reminder {duration}: keep water nearby",
    "Stay sharp, stay hydrated {duration}",
    "Hydration check after {duration}",
    "Drink water {duration}",
    "Dink now {duration}",
    "Be plant {duration}",
    "Go be good and water yourself, it's been {duration}",
    "Water check-in, {duration}, take a sip",
    "Micro-break: hydrate now",
    "Your hydration timer says {duration}: drink water",
    "Quick sip break. {duration}",
    "Hydration is part of the game plan, you've been playing for {duration}",
    "Take a water pause and keep going",
    "Refill if needed, then take a sip",
    "Stay topped up. Afer {duration} it's water time",
    "One sip now helps later, it's been {duration}",
    "Drink some water and reset your focus",
)
STATUS_EMOJI = {
    "online": "🟢",
    "offline": "⚪",
    "idle": "🟡",
    "dnd": "🔴",
}
UNKNOWN_STATUS_EMOJI = "❓"

PLATFORM_EMOJI = {
    "desktop": "🖥️",
    "mobile": "📱",
    "web": "🕸️",
}
UNKNOWN_PLATFORM_EMOJI = "❔"

# Activity/game names to ignore globally (case-insensitive exact match).
IGNORED_ACTIVITY_NAMES = {
    "wordle",
    "vroid studio",
}

IGNORED_USER_IDS: set[hikari.Snowflake] = set()


class WatchRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    types: set[str] = Field(default_factory=lambda: set(STATUS_TYPES))
    activities: set[str] = Field(default_factory=lambda: set(ACTIVITY_TYPES))
    games_mode: str = "all"
    "all | include | exclude"
    games: set[str] = Field(default_factory=set)
    "casefold game names"
    silent: bool = True
    "Whether notifications should suppress push/ping delivery"
    silent_rules: list[dict[str, object]] = Field(default_factory=list)
    "Per-notification silent overrides; each item may include status_type, activity, or game"

    @field_validator("types", mode="before")
    @classmethod
    def _validate_types(cls, value: object):
        if value is None:
            return set(STATUS_TYPES)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple, set)):
            return {str(v).strip().lower() for v in value if str(v).strip().lower() in STATUS_TYPES_SET}
        return set(STATUS_TYPES)

    @field_validator("activities", mode="before")
    @classmethod
    def _validate_activities(cls, value: object):
        if value is None:
            return set(ACTIVITY_TYPES)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple, set)):
            return {str(v).strip().lower() for v in value if str(v).strip().lower() in ACTIVITY_TYPES_SET}
        return set(ACTIVITY_TYPES)

    @field_validator("games_mode", mode="before")
    @classmethod
    def _validate_games_mode(cls, value: object):
        mode = str(value).strip().lower() if value is not None else "all"
        return mode if mode in {"all", "include", "exclude"} else "all"

    @field_validator("games", mode="before")
    @classmethod
    def _validate_games(cls, value: object):
        if value is None:
            return set()
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple, set)):
            return {str(v).strip().casefold() for v in value if str(v).strip()}
        return set()

    @field_validator("silent_rules", mode="before")
    @classmethod
    def _validate_silent_rules(cls, value: object):
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            return []

        merged: dict[tuple[str | None, str | None, str | None], bool] = {}
        for entry in value:
            if not isinstance(entry, dict):
                continue

            status_raw = entry.get("status_type")
            activity_raw = entry.get("activity")
            game_raw = entry.get("game")
            silent_raw = entry.get("silent", True)

            status_type = str(status_raw).strip().lower() if status_raw is not None else ""
            activity = str(activity_raw).strip().lower() if activity_raw is not None else ""
            game = str(game_raw).strip().casefold() if game_raw is not None else ""

            if status_type and status_type not in STATUS_TYPES_SET:
                continue
            if activity and activity not in ACTIVITY_TYPES_SET:
                continue
            if activity and game:
                continue

            status = status_type or None
            act = activity or None
            gm = game or None
            if status is None and act is None and gm is None:
                continue
            merged[(status, act, gm)] = bool(silent_raw)

        normalised: list[dict[str, object]] = []
        for (status, act, gm), is_silent in merged.items():
            row: dict[str, object] = {"silent": is_silent}
            if status is not None:
                row["status_type"] = status
            if act is not None:
                row["activity"] = act
            if gm is not None:
                row["game"] = gm
            normalised.append(row)
        return normalised

    def to_json(self) -> dict[str, object]:
        data = self.model_dump(mode="json")
        data["types"] = sorted(self.types)
        data["activities"] = sorted(self.activities)
        data["games"] = sorted(self.games)
        data["silent_rules"] = sorted(
            self.silent_rules,
            key=lambda entry: (
                str(entry.get("status_type", "")),
                str(entry.get("activity", "")),
                str(entry.get("game", "")),
            ),
        )
        return data

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> WatchRule:
        return cls.model_validate(raw if isinstance(raw, dict) else {})


class DrinkRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = "include"
    games: set[str] = Field(default_factory=set)

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object):
        mode = str(value).strip().lower() if value is not None else "include"
        return mode if mode in DRINK_MODES else "include"

    @field_validator("games", mode="before")
    @classmethod
    def _validate_games(cls, value: object):
        if value is None:
            return set()
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple, set)):
            return {str(v).strip().casefold() for v in value if str(v).strip()}
        return set()

    def to_json(self) -> dict[str, object]:
        data = self.model_dump(mode="json")
        data["games"] = sorted(self.games)
        return data

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> "DrinkRule":
        return cls.model_validate(raw if isinstance(raw, dict) else {})


@dataclass(slots=True, frozen=True)
class StatusChange:
    key: str
    old: str | None
    new: str | None


@dataclass(slots=True, frozen=True)
class ActivityChange:
    action: str
    kind: str
    name: str


@dataclass(slots=True)
class PresenceSnapshot:
    status: str
    platforms: dict[str, str]
    activities: dict[tuple[str, str], str]
    game_starts: dict[str, datetime | None]
    ignored_activities: set[str]


class Online_Tracker(metaclass=config.Singleton):
    def __init__(self, pointer: Path = Path("online_watch.json")):
        self.pointer = pointer
        self.rules: dict[hikari.Snowflake, dict[hikari.Snowflake, WatchRule]] = {}
        self._watchers_by_target: dict[hikari.Snowflake, set[hikari.Snowflake]] = {}
        self.ignored_user_ids: set[hikari.Snowflake] = set(IGNORED_USER_IDS)
        self.seen_games: set[str] = set()
        self._seen_games_cf: dict[str, str] = {}
        self.seen_games_by_user: dict[hikari.Snowflake, dict[str, str]] = {}
        self.drink_rules: dict[hikari.Snowflake, DrinkRule] = {}
        self.nick_rules: dict[hikari.Snowflake, dict[str, dict[str, str]]] = {}
        self._nick_managed_users: set[hikari.Snowflake] = set()
        self._nick_pending: dict[hikari.Snowflake, str | None] = {}
        self._nick_worker_tasks: dict[hikari.Snowflake, asyncio.Task[None]] = {}
        self._nick_last_change: dict[hikari.Snowflake, datetime] = {}
        self._nick_backoff_until: dict[hikari.Snowflake, datetime] = {}
        self._last_drink_ping: dict[hikari.Snowflake, datetime] = {}
        self._next_drink_ping_at: dict[hikari.Snowflake, datetime] = {}
        self._game_sessions: dict[hikari.Snowflake, dict[str, datetime]] = {}
        self._suppressed_game_stops: dict[hikari.Snowflake, set[str]] = {}
        self._snapshots: dict[hikari.Snowflake, PresenceSnapshot] = {}
        self._dm_channels: dict[hikari.Snowflake, hikari.Snowflake] = {}
        self._dm_backoff_until: dict[hikari.Snowflake, datetime] = {}
        self._recent_notifications: dict[tuple[hikari.Snowflake, str], datetime] = {}
        self._next_recent_cleanup = datetime.now(timezone.utc)
        self.ready_at = datetime.now(timezone.utc) + timedelta(seconds=10)
        self.drink_interval = DRINK_REMINDER_INTERVAL
        self._read()

    def set_ready_delay(self, seconds: int = 10):
        self.ready_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))

    def _read(self):
        if not self.pointer.exists():
            self._dump()
            return
        try:
            raw = json.loads(self.pointer.read_text(config.STR_ENCODE))
        except Exception:
            log.exception("Online_Tracker config read failed, resetting")
            self.rules = {}
            self._watchers_by_target = {}
            self.ignored_user_ids = set(IGNORED_USER_IDS)
            self.seen_games = set()
            self._seen_games_cf = {}
            self.seen_games_by_user = {}
            self.drink_rules = {}
            self.nick_rules = {}
            self._nick_managed_users = set()
            self._dump()
            return

        watchers = raw.get("watchers", {}) if isinstance(raw, dict) else {}
        if not isinstance(watchers, dict):
            watchers = {}

        loaded_rules: dict[hikari.Snowflake, dict[hikari.Snowflake, WatchRule]] = {}
        for watcher_id, entries in watchers.items():
            if not str(watcher_id).isdigit() or not isinstance(entries, dict):
                continue
            watcher = hikari.Snowflake(watcher_id)
            loaded_rules[watcher] = {}
            for target_id, raw_rule in entries.items():
                if not str(target_id).isdigit() or not isinstance(raw_rule, dict):
                    continue
                loaded_rules[watcher][hikari.Snowflake(target_id)] = WatchRule.from_json(raw_rule)
            if not loaded_rules[watcher]:
                loaded_rules.pop(watcher, None)

        self.rules = loaded_rules
        self._rebuild_watch_index()

        ignored_raw: object = []
        if isinstance(raw, dict):
            if "ignored_users" in raw:
                ignored_raw = raw.get("ignored_users", [])
            else:
                ignored_block = raw.get("ignored", {})
                if isinstance(ignored_block, dict):
                    ignored_raw = ignored_block.get("users", [])

        ignored_users = set() if isinstance(raw, dict) and "ignored_users" in raw else set(IGNORED_USER_IDS)
        if isinstance(ignored_raw, list):
            for entry in ignored_raw:
                if user_id := self._parse_snowflake(entry):
                    ignored_users.add(user_id)
        self.ignored_user_ids = ignored_users

        seen = raw.get("seen_games", []) if isinstance(raw, dict) else []
        self.seen_games = {str(v).strip() for v in seen if str(v).strip()}
        self._seen_games_cf = {name.casefold(): name for name in self.seen_games}

        seen_user_raw = raw.get("seen_games_by_user", {}) if isinstance(raw, dict) else {}
        if not isinstance(seen_user_raw, dict):
            seen_user_raw = {}
        seen_user: dict[hikari.Snowflake, dict[str, str]] = {}
        for user_id, games in seen_user_raw.items():
            if not str(user_id).isdigit():
                continue
            uid = hikari.Snowflake(user_id)
            user_games: dict[str, str] = {}
            if isinstance(games, list):
                for game in games:
                    text = str(game).strip()
                    if text:
                        user_games[text.casefold()] = text
            elif isinstance(games, dict):
                for _, game in games.items():
                    text = str(game).strip()
                    if text:
                        user_games[text.casefold()] = text
            if user_games:
                seen_user[uid] = user_games

        self.seen_games_by_user = seen_user
        for user_games in seen_user.values():
            self.seen_games.update(user_games.values())
        self._seen_games_cf = {name.casefold(): name for name in self.seen_games}

        drink_raw = raw.get("drink", {}) if isinstance(raw, dict) else {}
        if not isinstance(drink_raw, dict):
            drink_raw = {}
        loaded_drink: dict[hikari.Snowflake, DrinkRule] = {}
        for user_id, entry in drink_raw.items():
            if not str(user_id).isdigit() or not isinstance(entry, dict):
                continue
            loaded_drink[hikari.Snowflake(user_id)] = DrinkRule.from_json(entry)
        self.drink_rules = loaded_drink

        nick_raw = raw.get("nicknames", {}) if isinstance(raw, dict) else {}
        if not isinstance(nick_raw, dict):
            nick_raw = {}
        loaded_nick: dict[hikari.Snowflake, dict[str, dict[str, str]]] = {}
        for user_id, mode_map in nick_raw.items():
            if not str(user_id).isdigit() or not isinstance(mode_map, dict):
                continue
            uid = hikari.Snowflake(user_id)
            parsed_modes: dict[str, dict[str, str]] = {}
            for mode, raw_platforms in mode_map.items():
                mode_key = str(mode).strip().lower()
                if mode_key not in NICKNAME_MODES_SET or not isinstance(raw_platforms, dict):
                    continue
                parsed_platforms: dict[str, str] = {}
                for platform, nick in raw_platforms.items():
                    platform_key = str(platform).strip().lower()
                    nick_text = str(nick).strip()
                    if platform_key in NICKNAME_PLATFORM_SET and 0 < len(nick_text) <= 32:
                        parsed_platforms[platform_key] = nick_text
                if parsed_platforms:
                    parsed_modes[mode_key] = parsed_platforms
            if parsed_modes:
                loaded_nick[uid] = parsed_modes
        self.nick_rules = loaded_nick
        self._nick_managed_users = set(loaded_nick.keys())

    def _dump(self):
        serial = {
            "watchers": {
                str(watcher): {str(target): rule.to_json() for target, rule in targets.items()}
                for watcher, targets in self.rules.items()
            },
            "ignored_users": [str(uid) for uid in sorted(self.ignored_user_ids)],
            "seen_games": sorted(self.seen_games),
            "seen_games_by_user": {
                str(user_id): sorted(games.values(), key=str.casefold)
                for user_id, games in self.seen_games_by_user.items()
            },
            "drink": {str(user_id): rule.to_json() for user_id, rule in self.drink_rules.items()},
            "nicknames": {
                str(user_id): {
                    mode: {platform: nick for platform, nick in sorted(platforms.items(), key=lambda kv: kv[0])}
                    for mode, platforms in sorted(mode_map.items(), key=lambda kv: kv[0])
                }
                for user_id, mode_map in sorted(self.nick_rules.items(), key=lambda kv: int(kv[0]))
            },
        }
        self.pointer.write_text(json.dumps(serial, sort_keys=True, indent=4), config.STR_ENCODE)

    @staticmethod
    def _norm_game(name: str) -> str:
        return name.strip().casefold()

    @staticmethod
    def _is_ignored_activity_name(name: str) -> bool:
        return name.strip().casefold() in IGNORED_ACTIVITY_NAMES

    @classmethod
    def _is_ignored_activity(cls, kind: str, name: str) -> bool:
        if kind != "games" and name.strip().casefold() == "custom status":
            return True
        return cls._is_ignored_activity_name(name)

    def is_ignored_user(self, user_id: hikari.Snowflake) -> bool:
        return user_id in self.ignored_user_ids

    def toggle_ignored_user(self, user_id: hikari.Snowflake) -> bool:
        if user_id in self.ignored_user_ids:
            self.ignored_user_ids.remove(user_id)
            now_ignored = False
        else:
            self.ignored_user_ids.add(user_id)
            now_ignored = True
        self._dump()
        return now_ignored

    @staticmethod
    def _parse_snowflake(value: object) -> hikari.Snowflake | None:
        if isinstance(value, hikari.Snowflake):
            return value
        if isinstance(value, int):
            return hikari.Snowflake(value)
        if isinstance(value, str) and value.isdigit():
            return hikari.Snowflake(value)
        return None

    def _rebuild_watch_index(self):
        self._watchers_by_target.clear()
        for watcher_id, targets in self.rules.items():
            for target_id in targets.keys():
                self._watchers_by_target.setdefault(target_id, set()).add(watcher_id)

    def ensure_rule(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake) -> tuple[WatchRule, bool]:
        watcher = self.rules.setdefault(watcher_id, {})
        created = target_id not in watcher
        if created:
            watcher[target_id] = WatchRule()
            self._watchers_by_target.setdefault(target_id, set()).add(watcher_id)
            self._dump()
        return watcher[target_id], created

    def get_rule(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake) -> WatchRule | None:
        return self.rules.get(watcher_id, {}).get(target_id)

    def remove_rule(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake) -> bool:
        targets = self.rules.get(watcher_id)
        if not targets or target_id not in targets:
            return False
        del targets[target_id]
        target_watchers = self._watchers_by_target.get(target_id)
        if target_watchers:
            target_watchers.discard(watcher_id)
            if not target_watchers:
                self._watchers_by_target.pop(target_id, None)
        if not targets:
            self.rules.pop(watcher_id, None)
        self._dump()
        return True

    def list_rules(self, watcher_id: hikari.Snowflake) -> dict[hikari.Snowflake, WatchRule]:
        return dict(self.rules.get(watcher_id, {}))

    def list_games(self) -> list[str]:
        return sorted([g for g in self.seen_games if not self._is_ignored_activity_name(g)], key=str.casefold)

    def list_games_for_user(self, user_id: hikari.Snowflake) -> list[str]:
        games = self.seen_games_by_user.get(user_id, {})
        return sorted([g for g in games.values() if not self._is_ignored_activity_name(g)], key=str.casefold)

    def list_rule_games(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake) -> list[str]:
        rule = self.get_rule(watcher_id, target_id)
        if not rule or rule.games_mode == "all":
            return []
        return sorted([g for g in rule.games if not self._is_ignored_activity_name(g)], key=str.casefold)

    def set_rule_silent(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, silent: bool) -> bool:
        rule, _ = self.ensure_rule(watcher_id, target_id)
        if rule.silent == silent:
            return False
        rule.silent = silent
        self._dump()
        return True

    @staticmethod
    def _silent_selector_label(status_type: str | None, activity: str | None, game: str | None) -> str:
        parts: list[str] = []
        if status_type:
            parts.append(f"type:{status_type}")
        if activity:
            parts.append(f"activity:{activity}")
        if game:
            parts.append(f"game:{game}")
        return " + ".join(parts) if parts else "default"

    @staticmethod
    def _silent_rules_to_map(rule: WatchRule) -> dict[tuple[str | None, str | None, str | None], bool]:
        mapped: dict[tuple[str | None, str | None, str | None], bool] = {}
        for entry in rule.silent_rules:
            if not isinstance(entry, dict):
                continue
            status_raw = entry.get("status_type")
            activity_raw = entry.get("activity")
            game_raw = entry.get("game")
            silent_raw = entry.get("silent", True)
            status_type = str(status_raw).strip().lower() if status_raw is not None else ""
            activity = str(activity_raw).strip().lower() if activity_raw is not None else ""
            game = str(game_raw).strip().casefold() if game_raw is not None else ""
            if status_type and status_type not in STATUS_TYPES_SET:
                continue
            if activity and activity not in ACTIVITY_TYPES_SET:
                continue
            if activity and game:
                continue
            status = status_type or None
            act = activity or None
            gm = game or None
            if status is None and act is None and gm is None:
                continue
            mapped[(status, act, gm)] = bool(silent_raw)
        return mapped

    @staticmethod
    def _silent_rules_from_map(
        mapped: dict[tuple[str | None, str | None, str | None], bool],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for (status_type, activity, game), is_silent in sorted(
            mapped.items(),
            key=lambda item: (
                str(item[0][0] or ""),
                str(item[0][1] or ""),
                str(item[0][2] or ""),
            ),
        ):
            row: dict[str, object] = {"silent": is_silent}
            if status_type:
                row["status_type"] = status_type
            if activity:
                row["activity"] = activity
            if game:
                row["game"] = game
            rows.append(row)
        return rows

    def set_rule_silent_filtered(
        self,
        watcher_id: hikari.Snowflake,
        target_id: hikari.Snowflake,
        *,
        status_type: str | None = None,
        activity: str | None = None,
        game: str | None = None,
        silent: bool,
    ) -> list[str]:
        rule, _ = self.ensure_rule(watcher_id, target_id)
        selectors: list[tuple[str | None, str | None, str | None]] = []

        status_norm: str | None = None
        activity_norm: str | None = None
        game_norm: str | None = None
        if status_type:
            status_norm = status_type.strip().lower()
            if status_norm not in STATUS_TYPES_SET:
                raise ValueError(f"Unknown type: {status_type}")
        if activity:
            activity_norm = activity.strip().lower()
            if activity_norm not in ACTIVITY_TYPES_SET:
                raise ValueError(f"Unknown activity: {activity}")
        if game:
            game_norm = self._norm_game(game)
            if not game_norm:
                raise ValueError("game can't be empty")
            if self._is_ignored_activity_name(game_norm):
                raise ValueError(f"Game is ignored: {game}")

        if status_norm:
            if game_norm:
                selectors.append((status_norm, None, game_norm))
            if activity_norm:
                selectors.append((status_norm, activity_norm, None))
            if not game_norm and not activity_norm:
                selectors.append((status_norm, None, None))
        else:
            if game_norm:
                selectors.append((None, None, game_norm))
            if activity_norm:
                selectors.append((None, activity_norm, None))

        if not selectors:
            return []

        mapped = self._silent_rules_to_map(rule)
        changes: list[str] = []
        for selector in selectors:
            if mapped.get(selector) == silent:
                continue
            mapped[selector] = silent
            changes.append(f"silent rule {self._silent_selector_label(*selector)} -> {silent}")
        if not changes:
            return []

        rule.silent_rules = self._silent_rules_from_map(mapped)
        self._dump()
        return changes

    @staticmethod
    def _resolve_silent_candidates(
        overrides: dict[tuple[str | None, str | None, str | None], bool],
        candidates: list[tuple[str | None, str | None, str | None]],
    ) -> bool | None:
        matched = [overrides[candidate] for candidate in candidates if candidate in overrides]
        if not matched:
            return None
        # If same-specificity rules conflict, prefer non-silent.
        return all(matched)

    def resolve_notification_silent(
        self,
        rule: WatchRule,
        *,
        status_types: list[str] | None = None,
        activity_kind: str | None = None,
        game_name: str | None = None,
    ) -> bool:
        overrides = self._silent_rules_to_map(rule)
        if not overrides:
            return rule.silent

        status_list = [s for s in (status_types or []) if s in STATUS_TYPES_SET]
        game_key = game_name.casefold() if game_name else None
        kind = activity_kind if activity_kind in ACTIVITY_TYPES_SET else None

        level3: list[tuple[str | None, str | None, str | None]] = []
        if status_list and game_key:
            level3.extend((status, None, game_key) for status in status_list)
        if status_list and kind:
            level3.extend((status, kind, None) for status in status_list)
        resolved = self._resolve_silent_candidates(overrides, level3)
        if resolved is not None:
            return resolved

        level2 = [(status, None, None) for status in status_list]
        resolved = self._resolve_silent_candidates(overrides, level2)  # pyright: ignore[reportArgumentType]
        if resolved is not None:
            return resolved

        level1: list[tuple[str | None, str | None, str | None]] = []
        if game_key:
            level1.append((None, None, game_key))
        if kind:
            level1.append((None, kind, None))
        resolved = self._resolve_silent_candidates(overrides, level1)
        if resolved is not None:
            return resolved

        return rule.silent

    def export_user_config(
        self,
        watcher_id: hikari.Snowflake,
        target_id: hikari.Snowflake | None = None,
    ) -> dict[str, object]:
        rules = self.list_rules(watcher_id)
        entries: list[dict[str, object]] = []
        for target, rule in sorted(rules.items(), key=lambda kv: int(kv[0])):
            if target_id is not None and target != target_id:
                continue
            entries.append(
                {
                    "user_id": str(target),
                    "types": sorted(rule.types),
                    "activities": sorted(rule.activities),
                    "games_mode": rule.games_mode,
                    "games": sorted([g for g in rule.games if not self._is_ignored_activity_name(g)]),
                    "silent": rule.silent,
                    "silent_rules": rule.silent_rules,
                }
            )

        drink = self.get_drink_rule(watcher_id) or DrinkRule()
        drink_games = sorted([g for g in drink.games if not self._is_ignored_activity_name(g)])
        nick_entries = [
            {
                "nick": nick,
                "mode": mode,
                "platform": platform,
            }
            for mode, platform, nick in self.list_nickname_entries(watcher_id)
        ]

        return {
            "description": "Edit and re-upload this file with /online list file:<attachment> to replace your config",
            "accepted_values": {
                "types": list(STATUS_TYPES),
                "activities": list(ACTIVITY_TYPES),
                "games_mode": ["all", "include", "exclude"],
                "silent": "true|false (default true: suppress push notifications)",
                "silent_rules[]": {
                    "status_type": "optional, one of types",
                    "activity": "optional, one of activities",
                    "game": "optional, case-insensitive game name",
                    "silent": "true|false",
                },
                "drink.mode": ["include", "exclude"],
                "nicknames.mode": list(NICKNAME_MODES),
                "nicknames.platform": list(NICKNAME_PLATFORMS),
            },
            "ignored": {
                "activity_or_game_names": sorted(IGNORED_ACTIVITY_NAMES),
                "users": [str(uid) for uid in sorted(self.ignored_user_ids)],
            },
            "notes": [
                "watches is per target user",
                "Fields omitted in each watch entry fall back to defaults",
                "If games_mode is all, games list is ignored",
                "Uploading replaces your watch/drink/nickname config in one go",
            ],
            "user_editable": "Only values below this line are looked at by the bot",
            "watches": entries,
            "drink": {
                "mode": drink.mode,
                "games": drink_games,
            },
            "nicknames": nick_entries,
        }

    def apply_user_config(self, watcher_id: hikari.Snowflake, payload: dict[str, object] | Any) -> dict[str, int]:
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")

        watches = payload.get("watches", [])
        if not isinstance(watches, list):
            raise ValueError("watches must be a list")
        nicknames = payload.get("nicknames", [])
        if not isinstance(nicknames, list):
            raise ValueError("nicknames must be a list")

        replaced: dict[hikari.Snowflake, WatchRule] = {}
        skipped_ignored_users = 0

        for entry in watches:
            if not isinstance(entry, dict):
                continue
            raw_uid = entry.get("user_id")
            target_id = self._parse_snowflake(raw_uid)
            if not target_id:
                continue
            if self.is_ignored_user(target_id):
                skipped_ignored_users += 1
                continue
            rule = WatchRule.from_json(entry)
            rule.games = {g for g in rule.games if not self._is_ignored_activity_name(g)}
            replaced[target_id] = rule

        if replaced:
            self.rules[watcher_id] = replaced
        else:
            self.rules.pop(watcher_id, None)
        self._rebuild_watch_index()

        drink_raw = payload.get("drink")
        if isinstance(drink_raw, dict):
            drink = DrinkRule.from_json(drink_raw)
            drink.games = {g for g in drink.games if not self._is_ignored_activity_name(g)}
            self.drink_rules[watcher_id] = drink
        else:
            self.drink_rules.pop(watcher_id, None)

        replaced_nicks: dict[str, dict[str, str]] = {}
        for entry in nicknames:
            if not isinstance(entry, dict):
                continue
            try:
                nick = self._norm_nick_text(str(entry.get("nick", "")))
                mode = self._norm_nick_mode(str(entry.get("mode", "")))
                raw_platform = entry.get("platform") if "platform" in entry else None
                platform = self._norm_nick_platform(str(raw_platform) if raw_platform is not None else None)
                if mode == "offline" and platform != "all":
                    continue
            except ValueError:
                continue
            replaced_nicks.setdefault(mode, {})[platform] = nick

        if replaced_nicks:
            self.nick_rules[watcher_id] = replaced_nicks
        else:
            self.nick_rules.pop(watcher_id, None)

        self._dump()
        return {
            "watches": len(replaced),
            "drink_games": len(self.drink_rules.get(watcher_id, DrinkRule()).games),
            "nicknames": sum(len(platforms) for platforms in self.nick_rules.get(watcher_id, {}).values()),
            "skipped_ignored_users": skipped_ignored_users,
        }

    @staticmethod
    def _norm_mode(mode: str | None, default: str = "include") -> str:
        if not mode:
            return default
        mode = mode.strip().lower()
        if mode not in DRINK_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(DRINK_MODES))}")
        return mode

    def _display_game(self, user_id: hikari.Snowflake, game_key: str) -> str:
        if game := self.seen_games_by_user.get(user_id, {}).get(game_key):
            return game
        return self._seen_games_cf.get(game_key, game_key)

    def get_drink_rule(self, user_id: hikari.Snowflake) -> DrinkRule | None:
        return self.drink_rules.get(user_id)

    def toggle_drink_game(
        self,
        user_id: hikari.Snowflake,
        game: str,
        mode: str | None = None,
    ) -> tuple[str, DrinkRule]:
        game_key = self._norm_game(game)
        if not game_key:
            raise ValueError("game can't be empty")
        if self._is_ignored_activity_name(game_key):
            raise ValueError(f"Game is ignored: {game}")

        rule = self.drink_rules.get(user_id, DrinkRule())
        if mode is not None:
            rule.mode = self._norm_mode(mode, default=rule.mode)

        if game_key in rule.games:
            rule.games.remove(game_key)
            action = "removed"
        else:
            rule.games.add(game_key)
            action = "added"

        self.drink_rules[user_id] = rule
        self._dump()
        return action, rule

    @staticmethod
    def _drink_allows(rule: DrinkRule, game_name: str) -> bool:
        key = game_name.casefold()
        if rule.mode == "include":
            return key in rule.games
        return key not in rule.games

    def _next_drink_delay(self) -> timedelta:
        base_seconds = int(self.drink_interval.total_seconds())
        jitter_seconds = int(DRINK_REMINDER_JITTER_MAX.total_seconds())
        if jitter_seconds <= 0:
            return self.drink_interval
        return timedelta(seconds=max(60, base_seconds + random.randint(-jitter_seconds, jitter_seconds)))

    @staticmethod
    def _norm_nick_mode(mode: str) -> str:
        value = mode.strip().lower()
        if value not in NICKNAME_MODES_SET:
            raise ValueError(f"mode must be one of: {', '.join(NICKNAME_MODES)}")
        return value

    @staticmethod
    def _norm_nick_platform(platform: str | None, *, allow_all: bool = True) -> str:
        if not platform:
            return "all"
        value = platform.strip().lower()
        valid = NICKNAME_PLATFORM_SET if allow_all else set(PLATFORMS)
        if value not in valid:
            allowed = ", ".join(NICKNAME_PLATFORMS if allow_all else PLATFORMS)
            raise ValueError(f"platform must be one of: {allowed}")
        return value

    @staticmethod
    def _norm_nick_text(nick: str) -> str:
        value = nick.strip()
        if not value:
            raise ValueError("nick can't be empty")
        if len(value) > 32:
            raise ValueError("nick can't be longer than 32 characters")
        return value

    def set_nick_rule(self, user_id: hikari.Snowflake, nick: str, mode: str, platform: str | None = None) -> bool:
        mode_key = self._norm_nick_mode(mode)
        platform_key = self._norm_nick_platform(platform)
        nick_text = self._norm_nick_text(nick)
        if mode_key == "offline" and platform_key != "all":
            raise ValueError("offline nick rules only support platform=all")

        mode_map = self.nick_rules.setdefault(user_id, {})
        platform_map = mode_map.setdefault(mode_key, {})
        if platform_map.get(platform_key) == nick_text:
            return False
        platform_map[platform_key] = nick_text
        self._dump()
        return True

    def clear_nick_rule(self, user_id: hikari.Snowflake, nick: str, mode: str, platform: str | None = None) -> bool:
        mode_key = self._norm_nick_mode(mode)
        platform_key = self._norm_nick_platform(platform)
        if mode_key == "offline" and platform_key != "all":
            raise ValueError("offline nick rules only support platform=all")

        mode_map = self.nick_rules.get(user_id)
        if not mode_map:
            return False
        platform_map = mode_map.get(mode_key)
        if not platform_map:
            return False

        current = platform_map.get(platform_key)
        if not current:
            return False
        if current.casefold() != self._norm_nick_text(nick).casefold():
            return False

        platform_map.pop(platform_key, None)
        if not platform_map:
            mode_map.pop(mode_key, None)
        if not mode_map:
            self.nick_rules.pop(user_id, None)
        self._dump()
        return True

    def list_nickname_values(self, user_id: hikari.Snowflake) -> list[str]:
        mode_map = self.nick_rules.get(user_id, {})
        values = {nick for platform_map in mode_map.values() for nick in platform_map.values()}
        return sorted(values, key=str.casefold)

    def list_nickname_modes(self, user_id: hikari.Snowflake, nick: str | None = None) -> list[str]:
        mode_map = self.nick_rules.get(user_id, {})
        if not nick:
            return [mode for mode in NICKNAME_MODES if mode in mode_map]
        nick_cf = nick.strip().casefold()
        return [
            mode
            for mode in NICKNAME_MODES
            if mode in mode_map and any(value.casefold() == nick_cf for value in mode_map[mode].values())
        ]

    def list_nickname_platforms(
        self,
        user_id: hikari.Snowflake,
        mode: str | None = None,
        nick: str | None = None,
    ) -> list[str]:
        if not mode:
            return []
        mode_key = mode.strip().lower()
        if mode_key not in NICKNAME_MODES_SET:
            return []
        platform_map = self.nick_rules.get(user_id, {}).get(mode_key, {})
        if not nick:
            return [platform for platform in NICKNAME_PLATFORMS if platform in platform_map]
        nick_cf = nick.strip().casefold()
        return [
            platform
            for platform in NICKNAME_PLATFORMS
            if platform in platform_map and platform_map[platform].casefold() == nick_cf
        ]

    def list_nickname_entries(self, user_id: hikari.Snowflake) -> list[tuple[str, str, str]]:
        entries: list[tuple[str, str, str]] = []
        mode_map = self.nick_rules.get(user_id, {})
        for mode in NICKNAME_MODES:
            platform_map = mode_map.get(mode, {})
            if not platform_map:
                continue
            for platform in NICKNAME_PLATFORMS:
                if nick := platform_map.get(platform):
                    entries.append((mode, platform, nick))
        return entries

    @staticmethod
    def _fmt_nick_clear_label(nick: str, mode: str, platforms: list[str]) -> str:
        return f"{nick} ({mode})[{','.join(platforms)}]"

    def list_nickname_clear_options(self, user_id: hikari.Snowflake) -> list[tuple[str, str]]:
        mode_map = self.nick_rules.get(user_id, {})
        grouped: dict[tuple[str, str], tuple[str, list[str]]] = {}
        for mode in NICKNAME_MODES:
            platform_map = mode_map.get(mode, {})
            if not platform_map:
                continue
            for platform in NICKNAME_PLATFORMS:
                nick = platform_map.get(platform)
                if not nick:
                    continue
                key = (mode, nick.casefold())
                if key not in grouped:
                    grouped[key] = (nick, [])
                grouped[key][1].append(platform)

        out: list[tuple[str, str]] = []
        for mode in NICKNAME_MODES:
            for (entry_mode, _), (nick, platforms) in sorted(
                grouped.items(), key=lambda kv: (kv[1][0].casefold(), kv[0][0])
            ):
                if entry_mode != mode:
                    continue
                label = self._fmt_nick_clear_label(nick, entry_mode, platforms)
                token = json.dumps(
                    {"m": entry_mode, "n": nick, "p": platforms},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                out.append((label, token))
        return out

    @classmethod
    def _parse_nick_clear_token(cls, token: str) -> tuple[str, str, list[str]]:
        try:
            raw = json.loads(token)
        except Exception as xcp:
            raise ValueError("invalid nick_clear option") from xcp
        if not isinstance(raw, dict):
            raise ValueError("invalid nick_clear option")

        mode = cls._norm_nick_mode(str(raw.get("m", "")))
        nick = cls._norm_nick_text(str(raw.get("n", "")))

        platforms_raw = raw.get("p", [])
        if not isinstance(platforms_raw, list):
            raise ValueError("invalid nick_clear option")

        platforms: list[str] = []
        for platform in platforms_raw:
            platform_key = cls._norm_nick_platform(str(platform))
            if platform_key not in platforms:
                platforms.append(platform_key)

        if not platforms:
            platforms = ["all"]
        return mode, nick, platforms

    def describe_nick_clear_token(self, token: str) -> str:
        mode, nick, platforms = self._parse_nick_clear_token(token)
        return self._fmt_nick_clear_label(nick, mode, platforms)

    def clear_nick_by_token(self, user_id: hikari.Snowflake, token: str) -> int:
        mode, nick, platforms = self._parse_nick_clear_token(token)
        mode_map = self.nick_rules.get(user_id)
        if not mode_map:
            return 0
        platform_map = mode_map.get(mode)
        if not platform_map:
            return 0

        nick_cf = nick.casefold()
        removed = 0
        for platform in platforms:
            current = platform_map.get(platform)
            if current and current.casefold() == nick_cf:
                platform_map.pop(platform, None)
                removed += 1

        if removed <= 0:
            return 0

        if not platform_map:
            mode_map.pop(mode, None)
        if not mode_map:
            self.nick_rules.pop(user_id, None)
        self._dump()
        return removed

    def _desired_nickname_for_snapshot(self, user_id: hikari.Snowflake, snapshot: PresenceSnapshot) -> str | None:
        mode_map = self.nick_rules.get(user_id)
        if not mode_map:
            return None
        mode = snapshot.status if snapshot.status in NICKNAME_MODES_SET else "offline"
        platform_map = mode_map.get(mode, {})
        if not platform_map:
            return None

        if mode != "offline":
            for platform in NICKNAME_PLATFORM_PRIORITY:
                if snapshot.platforms.get(platform) == mode and (nick := platform_map.get(platform)):
                    return nick
        return platform_map.get("all")

    def _queue_nickname_update(self, user_id: hikari.Snowflake, desired_nick: str | None, bot: hikari.GatewayBot):
        self._nick_pending[user_id] = desired_nick
        worker = self._nick_worker_tasks.get(user_id)
        if worker and not worker.done():
            return
        self._nick_worker_tasks[user_id] = asyncio.create_task(self._nick_worker(bot, user_id))

    def _maybe_queue_nickname_for_snapshot(
        self,
        user_id: hikari.Snowflake,
        snapshot: PresenceSnapshot,
        bot: hikari.GatewayBot,
        *,
        force_clear: bool = False,
    ) -> bool:
        has_rules = bool(self.nick_rules.get(user_id))
        if not has_rules and not force_clear and user_id not in self._nick_managed_users:
            return False
        desired = self._desired_nickname_for_snapshot(user_id, snapshot) if has_rules else None
        self._queue_nickname_update(user_id, desired, bot)
        return True

    async def refresh_nickname(self, user_id: hikari.Snowflake, bot: hikari.GatewayBot, *, force_clear: bool = False):
        if snapshot := self._snapshots.get(user_id):
            self._maybe_queue_nickname_for_snapshot(user_id, snapshot, bot, force_clear=force_clear)

    async def _nick_worker(self, bot: hikari.GatewayBot, user_id: hikari.Snowflake):
        try:
            while user_id in self._nick_pending:
                now = datetime.now(timezone.utc)
                if now < self.ready_at:
                    await asyncio.sleep(max(0.2, (self.ready_at - now).total_seconds()))
                    continue

                if blocked_until := self._nick_backoff_until.get(user_id):
                    if blocked_until > now:
                        await asyncio.sleep(max(0.2, (blocked_until - now).total_seconds()))
                        continue
                    self._nick_backoff_until.pop(user_id, None)

                if changed_at := self._nick_last_change.get(user_id):
                    wait_seconds = (changed_at + NICKNAME_CHANGE_DELAY - now).total_seconds()
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)
                        continue

                target = self._nick_pending.get(user_id)
                if target is None and user_id not in self._nick_managed_users and not self.nick_rules.get(user_id):
                    self._nick_pending.pop(user_id, None)
                    break

                resolved = await self._apply_nickname(bot, user_id, target)
                if self._nick_pending.get(user_id) != target:
                    continue
                if resolved:
                    self._nick_pending.pop(user_id, None)
        finally:
            self._nick_worker_tasks.pop(user_id, None)

    async def _apply_nickname(
        self,
        bot: hikari.GatewayBot,
        user_id: hikari.Snowflake,
        desired_nick: str | None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        target = desired_nick.strip() if isinstance(desired_nick, str) else None
        target = target or None

        try:
            member = bot.cache.get_member(config.DISCORD_GUILD, user_id)
            if member is None:
                member = await bot.rest.fetch_member(config.DISCORD_GUILD, user_id)
        except hikari.NotFoundError:
            self._nick_managed_users.discard(user_id)
            return True
        except Exception as xcp:
            self._nick_backoff_until[user_id] = now + NICKNAME_RETRY_DELAY
            log.warning(f"Nickname member lookup failed for {user_id}: {xcp}")
            return False

        if member.nickname == target:
            if target is None:
                self._nick_managed_users.discard(user_id)
            else:
                self._nick_managed_users.add(user_id)
            return True

        try:
            await bot.rest.edit_member(config.DISCORD_GUILD, user_id, nickname=target)
            self._nick_last_change[user_id] = now
            self._nick_backoff_until.pop(user_id, None)
            if target is None:
                self._nick_managed_users.discard(user_id)
            else:
                self._nick_managed_users.add(user_id)
            return True
        except hikari.ForbiddenError:
            log.warning(f"Nickname update forbidden for {user_id} in guild {config.DISCORD_GUILD}")
            return True
        except hikari.BadRequestError as xcp:
            log.warning(f"Nickname update rejected for {user_id}: {xcp}")
            return True
        except Exception:
            self._nick_backoff_until[user_id] = now + NICKNAME_RETRY_DELAY
            log.exception(f"Nickname update failed for {user_id}")
            return False

    async def send_drink_reminders(self, bot: hikari.GatewayBot):
        if datetime.now(timezone.utc) < self.ready_at:
            return
        now = datetime.now(timezone.utc)
        for user_id, rule in self.drink_rules.items():
            if self.is_ignored_user(user_id):
                continue
            if rule.mode == "include" and not rule.games:
                continue

            snapshot = self._snapshots.get(user_id)
            if not snapshot:
                continue
            active_games = [name for (kind, _), name in snapshot.activities.items() if kind == "games"]
            if not active_games:
                continue
            matching = [name for name in active_games if self._drink_allows(rule, name)]
            if not matching:
                continue

            due_at = self._next_drink_ping_at.get(user_id)
            if due_at and now < due_at:
                continue

            # bootstrap for existing sessions before jittered schedule is set
            last = self._last_drink_ping.get(user_id)
            if not due_at and last and (now - last) < self.drink_interval:
                continue

            duration = self._reminder_duration_for_games(user_id, matching, now)
            lines = [random.choice(DRINK_REMINDERS).format(duration=duration or "")]
            await self._notify(bot, user_id, lines)
            self._last_drink_ping[user_id] = now
            self._next_drink_ping_at[user_id] = now + self._next_drink_delay()

    def add_type(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, type_name: str) -> bool:
        value = type_name.strip().lower()
        if value not in STATUS_TYPES_SET:
            raise ValueError(f"Unknown type: {type_name}")
        rule, _ = self.ensure_rule(watcher_id, target_id)
        if value in rule.types:
            return False
        rule.types.add(value)
        self._dump()
        return True

    def remove_type(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, type_name: str) -> bool:
        value = type_name.strip().lower()
        if value not in STATUS_TYPES_SET:
            raise ValueError(f"Unknown type: {type_name}")
        rule = self.get_rule(watcher_id, target_id)
        if not rule or value not in rule.types:
            return False
        rule.types.remove(value)
        self._dump()
        return True

    def add_activity(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, activity: str) -> bool:
        value = activity.strip().lower()
        if value not in ACTIVITY_TYPES_SET:
            raise ValueError(f"Unknown activity: {activity}")
        rule, _ = self.ensure_rule(watcher_id, target_id)
        if value in rule.activities:
            return False
        rule.activities.add(value)
        self._dump()
        return True

    def remove_activity(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, activity: str) -> bool:
        value = activity.strip().lower()
        if value not in ACTIVITY_TYPES_SET:
            raise ValueError(f"Unknown activity: {activity}")
        rule = self.get_rule(watcher_id, target_id)
        if not rule or value not in rule.activities:
            return False
        rule.activities.remove(value)
        self._dump()
        return True

    def add_game(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, game: str) -> str:
        game_key = self._norm_game(game)
        if not game_key:
            raise ValueError("game can't be empty")
        if self._is_ignored_activity_name(game_key):
            raise ValueError(f"Game is ignored: {game}")
        rule, _ = self.ensure_rule(watcher_id, target_id)
        if rule.games_mode == "all":
            rule.games_mode = "include"
            rule.games = {game_key}
            self._dump()
            return f"games mode -> include ({game})"
        if rule.games_mode == "include":
            if game_key in rule.games:
                return "no change"
            rule.games.add(game_key)
            self._dump()
            return f"added game include: {game}"
        if game_key not in rule.games:
            return "no change"
        rule.games.remove(game_key)
        if not rule.games:
            rule.games_mode = "all"
        self._dump()
        return f"removed game exclusion: {game}"

    def remove_game(self, watcher_id: hikari.Snowflake, target_id: hikari.Snowflake, game: str) -> str:
        game_key = self._norm_game(game)
        if not game_key:
            raise ValueError("game can't be empty")
        if self._is_ignored_activity_name(game_key):
            raise ValueError(f"Game is ignored: {game}")
        rule = self.get_rule(watcher_id, target_id)
        if not rule:
            return "no watch config"

        if rule.games_mode == "all":
            rule.games_mode = "exclude"
            rule.games = {game_key}
            self._dump()
            return f"games mode -> exclude ({game})"
        if rule.games_mode == "exclude":
            if game_key in rule.games:
                return "no change"
            rule.games.add(game_key)
            self._dump()
            return f"added game exclusion: {game}"
        if game_key not in rule.games:
            return "no change"
        rule.games.remove(game_key)
        self._dump()
        return f"removed game include: {game}"

    @staticmethod
    def _status_name(value: object | None) -> str:
        if value is None:
            return "offline"
        if isinstance(value, str):
            text = value.strip().lower()
        elif hasattr(value, "name"):
            text = str(getattr(value, "name")).strip().lower()
        else:
            text = str(value).split(".")[-1].strip().lower()
        if text in {"do_not_disturb", "do-not-disturb"}:
            return "dnd"
        if text in {"invisible"}:
            return "offline"
        return text or "offline"

    @staticmethod
    def _activity_start_at(activity: object) -> datetime | None:
        timestamps = getattr(activity, "timestamps", None)
        if not timestamps:
            return None
        start = getattr(timestamps, "start", None)
        if not start:
            return None
        if isinstance(start, datetime):
            dt = start
        elif isinstance(start, (int, float)):
            stamp = float(start)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
        else:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _format_duration(delta: timedelta) -> str:
        total = max(0, int(delta.total_seconds()))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def _update_game_sessions(self, user_id: hikari.Snowflake, snapshot: PresenceSnapshot, now: datetime):
        sessions = self._game_sessions.setdefault(user_id, {})
        active = set(snapshot.game_starts.keys())

        for game_key in list(sessions.keys()):
            if game_key not in active:
                sessions.pop(game_key, None)

        for game_key in active:
            if game_key in sessions:
                continue
            sessions[game_key] = snapshot.game_starts.get(game_key) or now

        if not sessions:
            self._game_sessions.pop(user_id, None)

    def _reminder_duration_for_games(
        self,
        user_id: hikari.Snowflake,
        matching_games: list[str],
        now: datetime,
    ) -> str | None:
        sessions = self._game_sessions.get(user_id)
        if not sessions:
            return None
        deltas = []
        for game in matching_games:
            if started := sessions.get(game.casefold()):
                deltas.append(now - started)
        if not deltas:
            return None
        return self._format_duration(max(deltas))

    @staticmethod
    def _activity_kind(activity: object) -> str:
        raw_type = getattr(activity, "type", None)
        name = str(getattr(raw_type, "name", raw_type)).strip().lower()
        if "playing" in name:
            return "games"
        if "listening" in name:
            return "music"
        if "streaming" in name:
            return "streaming"
        return "other"

    def _snapshot_from_presence(self, presence: hikari.MemberPresence | None) -> PresenceSnapshot:
        if not presence:
            return PresenceSnapshot(
                status="offline", platforms={}, activities={}, game_starts={}, ignored_activities=set()
            )

        status = self._status_name(getattr(presence, "visible_status", None) or getattr(presence, "status", None))

        platforms: dict[str, str] = {}
        client_status = getattr(presence, "client_status", None)
        for platform in PLATFORMS:
            value = None
            if isinstance(client_status, dict):
                value = client_status.get(platform)
            elif client_status is not None:
                value = getattr(client_status, platform, None)
            if value is not None:
                platforms[platform] = self._status_name(value)

        activities: dict[tuple[str, str], str] = {}
        game_starts: dict[str, datetime | None] = {}
        ignored_activities: set[str] = set()
        for activity in list(getattr(presence, "activities", []) or []):
            raw_name = (
                getattr(activity, "name", None)
                or getattr(activity, "details", None)
                or getattr(activity, "state", None)
            )
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            activity_name = raw_name.strip()
            kind = self._activity_kind(activity)
            if self._is_ignored_activity(kind, activity_name):
                ignored_activities.add(activity_name.casefold())
                continue
            key = activity_name.casefold()
            activities[(kind, key)] = activity_name
            if kind == "games":
                game_starts[key] = self._activity_start_at(activity)

        return PresenceSnapshot(
            status=status,
            platforms=platforms,
            activities=activities,
            game_starts=game_starts,
            ignored_activities=ignored_activities,
        )

    def _record_seen_games(self, user_id: hikari.Snowflake, snapshot: PresenceSnapshot):
        changed = False
        user_games = self.seen_games_by_user.setdefault(user_id, {})
        for (kind, _), name in snapshot.activities.items():
            if kind != "games":
                continue
            if name not in self.seen_games:
                self.seen_games.add(name)
                self._seen_games_cf[name.casefold()] = name
                changed = True
            key = name.casefold()
            if user_games.get(key) != name:
                user_games[key] = name
                self._seen_games_cf[key] = name
                changed = True
        if changed:
            self._dump()

    def _diff(self, old: PresenceSnapshot, new: PresenceSnapshot) -> tuple[list[StatusChange], list[ActivityChange]]:
        status_changes: list[StatusChange] = []
        if old.status != new.status and new.status == "offline":
            status_changes.append(StatusChange("offline", old.status, new.status))

        for platform in PLATFORMS:
            old_platform = old.platforms.get(platform)
            new_platform = new.platforms.get(platform)
            if old_platform == new_platform:
                continue
            if new_platform in ONLINE_STATUSES:
                status_changes.append(StatusChange(f"{new_platform}-{platform}", old_platform, new_platform))

        old_keys = set(old.activities.keys())
        new_keys = set(new.activities.keys())

        activity_changes = [
            ActivityChange("started", kind, new.activities[(kind, name_key)])
            for kind, name_key in sorted(new_keys - old_keys)
        ]
        activity_changes.extend(
            ActivityChange("stopped", kind, old.activities[(kind, name_key)])
            for kind, name_key in sorted(old_keys - new_keys)
        )

        return status_changes, activity_changes

    def _stabilise_activity_changes(
        self,
        user_id: hikari.Snowflake,
        new_snapshot: PresenceSnapshot,
        changes: list[ActivityChange],
    ) -> list[ActivityChange]:
        if not changes and not self._suppressed_game_stops.get(user_id):
            return changes

        suppressed = self._suppressed_game_stops.setdefault(user_id, set())
        visible_games = {key for (kind, key) in new_snapshot.activities.keys() if kind == "games"}
        stable: list[ActivityChange] = []

        for change in changes:
            if change.kind != "games":
                stable.append(change)
                continue

            game_key = change.name.casefold()
            if change.action == "started":
                if game_key in suppressed:
                    suppressed.discard(game_key)
                    continue
                stable.append(change)
                continue

            if change.action != "stopped":
                stable.append(change)
                continue

            if new_snapshot.status != "offline" and new_snapshot.ignored_activities:
                suppressed.add(game_key)
                continue

            suppressed.discard(game_key)
            stable.append(change)

        if suppressed and (new_snapshot.status == "offline" or not new_snapshot.ignored_activities):
            to_confirm = sorted(suppressed - visible_games)
            for game_key in to_confirm:
                stable.append(ActivityChange("stopped", "games", self._display_game(user_id, game_key)))
                suppressed.discard(game_key)

        if not suppressed:
            self._suppressed_game_stops.pop(user_id, None)

        return stable

    def _watchers_for_target(self, target_id: hikari.Snowflake) -> list[tuple[hikari.Snowflake, WatchRule]]:
        if self.is_ignored_user(target_id):
            return []
        matches: list[tuple[hikari.Snowflake, WatchRule]] = []
        for watcher_id in self._watchers_by_target.get(target_id, set()):
            if self.is_ignored_user(watcher_id):
                continue
            if rule := self.rules.get(watcher_id, {}).get(target_id):
                matches.append((watcher_id, rule))
        return matches

    @staticmethod
    def _status_emoji(status: str | None) -> str:
        if not status:
            return UNKNOWN_STATUS_EMOJI
        return STATUS_EMOJI.get(status.lower(), UNKNOWN_STATUS_EMOJI)

    @staticmethod
    def _platform_emoji(platform: str | None) -> str:
        if not platform:
            return UNKNOWN_PLATFORM_EMOJI
        return PLATFORM_EMOJI.get(platform.lower(), UNKNOWN_PLATFORM_EMOJI)

    @staticmethod
    def _platform_emojis(snapshot: PresenceSnapshot) -> str:
        active = [platform for platform in PLATFORMS if snapshot.platforms.get(platform) in ONLINE_STATUSES]
        if active:
            return "".join(PLATFORM_EMOJI.get(platform, UNKNOWN_PLATFORM_EMOJI) for platform in active)
        known = [platform for platform in PLATFORMS if platform in snapshot.platforms]
        if known:
            return "".join(PLATFORM_EMOJI.get(platform, UNKNOWN_PLATFORM_EMOJI) for platform in known)
        return UNKNOWN_PLATFORM_EMOJI

    @staticmethod
    def _snapshot_status_types(snapshot: PresenceSnapshot) -> list[str]:
        if snapshot.status == "offline":
            return ["offline"]
        keys: list[str] = []
        for platform in PLATFORMS:
            status = snapshot.platforms.get(platform)
            key = f"{status}-{platform}" if status else ""
            if key in STATUS_TYPES_SET:
                keys.append(key)
        return keys

    @staticmethod
    def _game_allowed(rule: WatchRule, game_name: str) -> bool:
        game_key = game_name.casefold()
        if rule.games_mode == "all":
            return True
        if rule.games_mode == "include":
            return game_key in rule.games
        return game_key not in rule.games

    def _fmt_status(self, target_id: hikari.Snowflake, change: StatusChange, snapshot: PresenceSnapshot) -> str:
        del snapshot
        if change.key == "offline":
            status = "offline"
            platform_icon = UNKNOWN_PLATFORM_EMOJI
            label = status
        else:
            status, platform = change.key.split("-", 1)
            platform_icon = self._platform_emoji(platform)
            label = f"{status}-{platform}"
        return f"{self._status_emoji(status)} {platform_icon} <@{target_id}> {label}"

    def _fmt_activity(self, target_id: hikari.Snowflake, change: ActivityChange, snapshot: PresenceSnapshot) -> str:
        status = snapshot.status
        platform = self._platform_emojis(snapshot)
        if change.kind == "games":
            detail = f"{change.action}:{change.name}"
        else:
            detail = f"{change.action}:{change.kind}:{change.name}"
        return f"{self._status_emoji(status)} {platform} <@{target_id}> {detail}"

    async def _resolve_dm_channel(self, bot: hikari.GatewayBot, user_id: hikari.Snowflake) -> hikari.Snowflake:
        if channel_id := self._dm_channels.get(user_id):
            return channel_id
        dm = await bot.rest.create_dm_channel(user_id)
        self._dm_channels[user_id] = dm.id
        return dm.id

    async def _notify(self, bot: hikari.GatewayBot, user_id: hikari.Snowflake, lines: list[str], silent: bool = True):
        if not lines:
            return
        now = datetime.now(timezone.utc)
        payload = "\n".join(lines)

        stamp_key = (user_id, f"{int(silent)}:{payload}")
        previous = self._recent_notifications.get(stamp_key)
        if previous and (now - previous) < timedelta(seconds=3):
            return

        if now >= self._next_recent_cleanup:
            for key, ts in list(self._recent_notifications.items()):
                if (now - ts) > timedelta(minutes=2):
                    self._recent_notifications.pop(key, None)
            self._next_recent_cleanup = now + timedelta(seconds=45)

        if blocked_until := self._dm_backoff_until.get(user_id):
            if now < blocked_until:
                return
            self._dm_backoff_until.pop(user_id, None)
        try:
            channel_id = await self._resolve_dm_channel(bot, user_id)
            await bot.rest.create_message(
                channel_id,
                content=payload,
                flags=hikari.MessageFlag.SUPPRESS_NOTIFICATIONS if silent else hikari.UNDEFINED,
            )
            self._recent_notifications[stamp_key] = now
        except hikari.NotFoundError:
            self._dm_channels.pop(user_id, None)
            channel_id = await self._resolve_dm_channel(bot, user_id)
            await bot.rest.create_message(
                channel_id,
                content=payload,
                flags=hikari.MessageFlag.SUPPRESS_NOTIFICATIONS if silent else hikari.UNDEFINED,
            )
            self._recent_notifications[stamp_key] = now
        except hikari.ForbiddenError:
            log.info(f"Online_Tracker DM blocked for user {user_id}")
        except hikari.BadRequestError as xcp:
            # 40003: opening direct messages too fast
            if getattr(xcp, "code", None) == 40003:
                self._dm_backoff_until[user_id] = now + timedelta(seconds=35)
                log.warning(f"Online_Tracker DM throttled for {user_id}; backing off 35s")
                return
            log.exception(f"Online_Tracker notify bad-request for {user_id}: {xcp}")
        except Exception:
            log.exception(f"Online_Tracker notify failed for {user_id}")

    async def on_presence_update(self, event: hikari.PresenceUpdateEvent, bot: hikari.GatewayBot):
        new_presence = event.presence
        user_id = new_presence.user_id
        if self.is_ignored_user(user_id):
            return
        now = datetime.now(timezone.utc)
        prev_snapshot = self._snapshots.get(user_id)
        event_old = self._snapshot_from_presence(event.old_presence) if event.old_presence else None
        old_snapshot = prev_snapshot or event_old
        new_snapshot = self._snapshot_from_presence(new_presence)

        if prev_snapshot == new_snapshot:
            return

        self._snapshots[user_id] = new_snapshot
        self._update_game_sessions(user_id, new_snapshot, now)
        self._record_seen_games(user_id, new_snapshot)
        self._maybe_queue_nickname_for_snapshot(user_id, new_snapshot, bot)

        if not old_snapshot or now < self.ready_at:
            return

        status_changes, activity_changes = self._diff(old_snapshot, new_snapshot)
        activity_changes = self._stabilise_activity_changes(user_id, new_snapshot, activity_changes)
        if not status_changes and not activity_changes:
            return

        watchers = self._watchers_for_target(user_id)
        if not watchers:
            return

        for watcher_id, rule in watchers:
            silent_lines: list[str] = []
            loud_lines: list[str] = []
            activity_status_types = self._snapshot_status_types(new_snapshot)
            for status_change in status_changes:
                if status_change.key in rule.types:
                    line = self._fmt_status(user_id, status_change, new_snapshot)
                    if self.resolve_notification_silent(rule, status_types=[status_change.key]):
                        silent_lines.append(line)
                    else:
                        loud_lines.append(line)
            for activity_change in activity_changes:
                if activity_change.kind not in rule.activities:
                    continue
                if activity_change.kind == "games" and not self._game_allowed(rule, activity_change.name):
                    continue
                line = self._fmt_activity(user_id, activity_change, new_snapshot)
                silent = self.resolve_notification_silent(
                    rule,
                    status_types=activity_status_types,
                    activity_kind=activity_change.kind,
                    game_name=activity_change.name if activity_change.kind == "games" else None,
                )
                if silent:
                    silent_lines.append(line)
                else:
                    loud_lines.append(line)
            if silent_lines:
                await self._notify(bot, watcher_id, silent_lines, silent=True)
            if loud_lines:
                await self._notify(bot, watcher_id, loud_lines, silent=False)


def _extract_user_id(value: object | None) -> hikari.Snowflake | None:
    if value is None:
        return None
    if isinstance(value, hikari.Snowflake):
        return value
    if isinstance(value, int):
        return hikari.Snowflake(value)
    if hasattr(value, "id"):
        ident = getattr(value, "id")
        if isinstance(ident, (int, hikari.Snowflake)):
            return hikari.Snowflake(ident)
    if isinstance(value, str) and value.isdigit():
        return hikari.Snowflake(value)
    return None


def _target_from_ctx(ctx: lightbulb.AutocompleteContext) -> hikari.Snowflake | None:
    option = ctx.get_option("user")
    if option is None:
        return None
    return _extract_user_id(getattr(option, "value", option))


async def _ctx_defer(ctx: lightbulb.Context):
    await ctx.defer(ephemeral=ctx.guild_id is not None)


async def _ctx_respond(
    ctx: lightbulb.Context,
    content: hikari.UndefinedOr[object] = hikari.UNDEFINED,
    *,
    ephemeral: bool | None = None,
    attachment: hikari.UndefinedOr[hikari.Resourceish] = hikari.UNDEFINED,
):
    if ephemeral is None:
        ephemeral = ctx.guild_id is not None
    await ctx.respond(content, ephemeral=ephemeral, attachment=attachment)


def _rule_summary(rule: WatchRule) -> str:
    status_txt = ", ".join(sorted(rule.types)) if rule.types else "(none)"
    activity_txt = ", ".join(sorted(rule.activities)) if rule.activities else "(none)"
    if rule.games_mode == "all":
        games_txt = "all"
    elif not rule.games:
        games_txt = f"{rule.games_mode} (empty)"
    else:
        games_txt = f"{rule.games_mode}: {', '.join(sorted(rule.games))}"
    silent_rules = len(rule.silent_rules)
    return (
        f"types: {status_txt}\n"
        f"activities: {activity_txt}\n"
        f"games: {games_txt}\n"
        f"default_silent: {rule.silent}\n"
        f"silent_rules: {silent_rules}"
    )


def _drink_summary(tracker: Online_Tracker, user_id: hikari.Snowflake, rule: DrinkRule) -> str:
    games = sorted([tracker._display_game(user_id, game) for game in rule.games], key=str.casefold)
    games_txt = ", ".join(games) if games else "(none)"
    return f"mode: {rule.mode}\ngames: {games_txt}"


def _nickname_summary(tracker: Online_Tracker, user_id: hikari.Snowflake) -> str:
    entries = tracker.list_nickname_entries(user_id)
    if not entries:
        return "nickname rules: (none)"
    lines = [f"{mode}/{platform} -> {nick}" for mode, platform, nick in entries]
    return "nickname rules:\n" + "\n".join(f"- {line}" for line in lines)


async def ac_type_add(ctx: lightbulb.AutocompleteContext):
    await Distils.ac_focused_static(ctx, STATUS_TYPES)


async def ac_type_remove(ctx: lightbulb.AutocompleteContext, tracker: Online_Tracker):
    target_id = _target_from_ctx(ctx)
    if target_id and (rule := tracker.get_rule(ctx.interaction.user.id, target_id)):
        opts = sorted(rule.types)
    else:
        opts = list(STATUS_TYPES)
    await Distils.ac_focused_static(ctx, opts)


async def ac_activity_add(ctx: lightbulb.AutocompleteContext):
    await Distils.ac_focused_static(ctx, ACTIVITY_TYPES)


async def ac_activity_remove(ctx: lightbulb.AutocompleteContext, tracker: Online_Tracker):
    target_id = _target_from_ctx(ctx)
    if target_id and (rule := tracker.get_rule(ctx.interaction.user.id, target_id)):
        opts = sorted(rule.activities)
    else:
        opts = list(ACTIVITY_TYPES)
    await Distils.ac_focused_static(ctx, opts)


async def ac_game_add(ctx: lightbulb.AutocompleteContext, tracker: Online_Tracker):
    await Distils.ac_focused_static(ctx, tracker.list_games())


async def ac_game_remove(ctx: lightbulb.AutocompleteContext, tracker: Online_Tracker):
    target_id = _target_from_ctx(ctx)
    if target_id:
        opts = tracker.list_rule_games(ctx.interaction.user.id, target_id)
    else:
        opts = tracker.list_games()
    await Distils.ac_focused_static(ctx, opts)


async def ac_drink_games(ctx: lightbulb.AutocompleteContext, tracker: Online_Tracker):
    await Distils.ac_focused_static(ctx, tracker.list_games_for_user(ctx.interaction.user.id))


async def ac_nick_clear_entry(ctx: lightbulb.AutocompleteContext, tracker: Online_Tracker):
    if not isinstance(ctx.focused.value, str):
        raise ValueError(f"String go with strings, not {type(ctx.focused.value)}")
    foc_val = ctx.focused.value.lower()
    acb = hikari.impl.AutocompleteChoiceBuilder
    options = tracker.list_nickname_clear_options(ctx.interaction.user.id)
    await ctx.respond([acb(label, token) for label, token in options if foc_val in label.lower()][:25])


@group_online.register
class CMD_OnlineAdd(
    lightbulb.SlashCommand,
    name="add",
    description="Add watch config for a user",
    hooks=[lightbulb.prefab.sliding_window(8, 2, "user")],
):
    user = lightbulb.user("user", "User to watch")  # type: ignore
    status_type = lightbulb.string("type", "Status type", autocomplete=ac_type_add, default=None)  # type: ignore
    game = lightbulb.string("game", "Game filter", autocomplete=ac_game_add, default=None)  # type: ignore
    activity = lightbulb.string("activity", "Activity filter", autocomplete=ac_activity_add, default=None)  # type: ignore
    silent = lightbulb.boolean(
        "silent",
        "Default silence. With type/activity/game this sets a silent selector, not watch filters",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker, names: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        target_id = _extract_user_id(self.user)
        if target_id is None:
            raise ValueError("Invalid target user")
        if tracker.is_ignored_user(target_id):
            raise ValueError("That user is ignored")

        watcher_id = ctx.user.id
        target_name = await names.best_known(target_id, f"<@{target_id}>")

        _, created = tracker.ensure_rule(watcher_id, target_id)
        changes: list[str] = []
        scoped_silent = self.silent is not None and any([self.status_type, self.activity, self.game])

        # When `silent` is provided with selectors, those selectors are treated as
        # silent-rule matchers, not watch-filter edits.
        if not scoped_silent:
            if self.status_type and tracker.add_type(watcher_id, target_id, self.status_type):
                changes.append(f"added type: {self.status_type}")
            if self.activity and tracker.add_activity(watcher_id, target_id, self.activity):
                changes.append(f"added activity: {self.activity}")
            if self.game:
                result = tracker.add_game(watcher_id, target_id, self.game)
                if result != "no change":
                    changes.append(result)
        if self.silent is not None:
            if scoped_silent:
                changes.extend(
                    tracker.set_rule_silent_filtered(
                        watcher_id,
                        target_id,
                        status_type=self.status_type,
                        activity=self.activity,
                        game=self.game,
                        silent=self.silent,
                    )
                )
            elif tracker.set_rule_silent(watcher_id, target_id, self.silent):
                changes.append(f"default silent: {self.silent}")

        if not any([self.status_type, self.activity, self.game, self.silent is not None]):
            await _ctx_respond(ctx, f"Watching {target_name} with default filters")
            return

        if not changes and not created:
            await _ctx_respond(ctx, f"No changes for {target_name}")
            return
        if not changes and created:
            await _ctx_respond(ctx, f"Watching {target_name} with default filters")
            return

        await _ctx_respond(ctx, f"Updated watch for {target_name}\n" + "\n".join(f"- {line}" for line in changes))


@group_online.register
class CMD_OnlineRemove(
    lightbulb.SlashCommand,
    name="remove",
    description="Remove filters from a watch config",
    hooks=[lightbulb.prefab.sliding_window(8, 2, "user")],
):
    user = lightbulb.user("user", "Watched user")  # type: ignore
    status_type = lightbulb.string("type", "Status type", autocomplete=ac_type_remove, default=None)  # type: ignore
    game = lightbulb.string("game", "Game filter", autocomplete=ac_game_remove, default=None)  # type: ignore
    activity = lightbulb.string("activity", "Activity filter", autocomplete=ac_activity_remove, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker, names: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        target_id = _extract_user_id(self.user)
        if target_id is None:
            raise ValueError("Invalid target user")
        if tracker.is_ignored_user(target_id):
            raise ValueError("That user is ignored")

        watcher_id = ctx.user.id
        target_name = await names.best_known(target_id, f"<@{target_id}>")

        rule = tracker.get_rule(watcher_id, target_id)
        if not rule:
            await _ctx_respond(ctx, f"No watch config found for {target_name}")
            return

        if not any([self.status_type, self.activity, self.game]):
            await _ctx_respond(ctx, "No filters passed. Use `/online unwatch` to clear all config for this user")
            return

        changes: list[str] = []
        if self.status_type and tracker.remove_type(watcher_id, target_id, self.status_type):
            changes.append(f"removed type: {self.status_type}")
        if self.activity and tracker.remove_activity(watcher_id, target_id, self.activity):
            changes.append(f"removed activity: {self.activity}")
        if self.game:
            result = tracker.remove_game(watcher_id, target_id, self.game)
            if result not in {"no change", "no watch config"}:
                changes.append(result)

        if not changes:
            await _ctx_respond(ctx, f"No matching filters were set for {target_name}")
            return

        await _ctx_respond(ctx, f"Updated watch for {target_name}\n" + "\n".join(f"- {line}" for line in changes))


@group_online.register
class CMD_OnlineUnwatch(
    lightbulb.SlashCommand,
    name="unwatch",
    description="Remove all watch config for a user",
    hooks=[lightbulb.prefab.sliding_window(8, 2, "user")],
):
    user = lightbulb.user("user", "Watched user", default=None)  # type: ignore
    ignore_me = lightbulb.boolean(
        "ignore_me",
        "Toggle yourself in ignored-users list",
        default=False,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker, names: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        watcher_id = ctx.user.id
        target_id = _extract_user_id(self.user) if self.user else None
        if self.user and target_id is None:
            raise ValueError("Invalid target user")

        if target_id is not None and tracker.is_ignored_user(target_id):
            if not (self.ignore_me and target_id == watcher_id):
                raise ValueError("That user is ignored")

        if target_id is None and not self.ignore_me:
            raise ValueError("Provide a `user` or set `ignore_me: true`")

        lines: list[str] = []
        if target_id is not None:
            target_name = await names.best_known(target_id, f"<@{target_id}>")
            if tracker.remove_rule(watcher_id, target_id):
                lines.append(f"Stopped watching {target_name}")
            else:
                lines.append(f"No watch config found for {target_name}")

        if self.ignore_me:
            now_ignored = tracker.toggle_ignored_user(watcher_id)
            if now_ignored:
                lines.append("You are now ignored by online tracking")
            else:
                lines.append("You are no longer ignored by online tracking")

        await _ctx_respond(ctx, "\n".join(lines))


@group_online.register
class CMD_OnlineDrink(
    lightbulb.SlashCommand,
    name="drink",
    description="Toggle hydration reminders for a game",
    hooks=[lightbulb.prefab.sliding_window(8, 2, "user")],
):
    game = lightbulb.string("game", "Game from your play history", autocomplete=ac_drink_games)  # type: ignore
    mode = lightbulb.string(
        "mode",
        "Include only listed games or exclude listed games",
        choices=[lightbulb.Choice("include", "include"), lightbulb.Choice("exclude", "exclude")],
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)

        action, rule = tracker.toggle_drink_game(ctx.user.id, self.game, self.mode)
        game_name = tracker._display_game(ctx.user.id, tracker._norm_game(self.game))

        await _ctx_respond(ctx, f"Drink reminder {action}: {game_name}\n{_drink_summary(tracker, ctx.user.id, rule)}")


@group_online.register
class CMD_OnlineNickname(
    lightbulb.SlashCommand,
    name="nickname",
    description="Set an auto-nickname rule from your presence mode",
    hooks=[lightbulb.prefab.sliding_window(8, 2, "user")],
):
    nick = lightbulb.string("nick", "Nickname to use in the configured guild")  # type: ignore
    mode = lightbulb.string(
        "mode",
        "Presence mode",
        choices=[
            lightbulb.Choice("online", "online"),
            lightbulb.Choice("offline", "offline"),
            lightbulb.Choice("idle", "idle"),
            lightbulb.Choice("dnd", "dnd"),
        ],
    )
    platform = lightbulb.string(
        "platform",
        "Optional platform override (omit for all platforms)",
        choices=[
            lightbulb.Choice("desktop", "desktop"),
            lightbulb.Choice("mobile", "mobile"),
            lightbulb.Choice("web", "web"),
        ],
        default=None,
    )

    @lightbulb.invoke
    async def invoke(
        self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker, bot: hikari.GatewayBot
    ):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        changed = tracker.set_nick_rule(ctx.user.id, self.nick, self.mode, self.platform)
        await tracker.refresh_nickname(ctx.user.id, bot)

        platform = self.platform or "all"
        if changed:
            await _ctx_respond(
                ctx,
                f"Saved nickname rule: {self.mode}/{platform} -> {self.nick}\n"
                f"{_nickname_summary(tracker, ctx.user.id)}\n"
                f"Changes are throttled to about {int(NICKNAME_CHANGE_DELAY.total_seconds())}s per update",
            )
            return

        await _ctx_respond(
            ctx,
            f"No change: {self.mode}/{platform} already points to `{self.nick}`\n{_nickname_summary(tracker, ctx.user.id)}",
        )


@group_online.register
class CMD_OnlineNickClear(
    lightbulb.SlashCommand,
    name="nick_clear",
    description="Clear an auto-nickname rule",
    hooks=[lightbulb.prefab.sliding_window(8, 2, "user")],
):
    entry = lightbulb.string("entry", "Nickname rule: <nick> (<state>)[<platforms>]", autocomplete=ac_nick_clear_entry)  # type: ignore

    @lightbulb.invoke
    async def invoke(
        self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker, bot: hikari.GatewayBot
    ):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        selected = tracker.describe_nick_clear_token(self.entry)
        removed = tracker.clear_nick_by_token(ctx.user.id, self.entry)
        await tracker.refresh_nickname(ctx.user.id, bot, force_clear=not tracker.nick_rules.get(ctx.user.id))

        if not removed:
            await _ctx_respond(
                ctx,
                f"No matching nickname rule found for `{selected}`\n{_nickname_summary(tracker, ctx.user.id)}",
            )
            return

        await _ctx_respond(
            ctx,
            f"Removed `{selected}` ({removed} entr{'y' if removed == 1 else 'ies'})\n"
            f"{_nickname_summary(tracker, ctx.user.id)}",
        )


@group_online.register
class CMD_OnlineList(
    lightbulb.SlashCommand,
    name="list",
    description="List your online watch config",
    hooks=[lightbulb.prefab.sliding_window(6, 2, "user")],
):
    user = lightbulb.user("user", "Optional target user", default=None)  # type: ignore
    file = lightbulb.attachment(
        "file",
        "Optional JSON config file exported by this command; uploads replace your config",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, tracker: Online_Tracker, names: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        watcher_id = ctx.user.id

        if self.file:
            if self.user:
                raise ValueError("`user` can't be used with `file` import")
            await _ctx_defer(ctx)
            path = await File_Utils.download_temp(self.file)
            try:
                payload = json.loads(path.read_text(config.STR_ENCODE))
            except json.JSONDecodeError as xcp:
                raise ValueError(f"Invalid JSON file: {xcp}") from xcp
            if not isinstance(payload, dict):
                raise ValueError("Invalid JSON file: top-level object expected")
            result = tracker.apply_user_config(watcher_id, payload)
            await _ctx_respond(
                ctx,
                "Online config updated from file\n"
                f"- watches: {result['watches']}\n"
                f"- drink games: {result['drink_games']}\n"
                f"- nicknames: {result['nicknames']}\n"
                f"- skipped ignored users: {result['skipped_ignored_users']}",
            )
            return

        target_id: hikari.Snowflake | None = None
        if self.user:
            target_id = _extract_user_id(self.user)
            if target_id is None:
                raise ValueError("Invalid target user")
            if tracker.is_ignored_user(target_id):
                raise ValueError("That user is ignored")

        exported = tracker.export_user_config(watcher_id, target_id=target_id)
        if target_id:
            target_name = await names.best_known(target_id, f"<@{target_id}>")
            filename = f"online_config_{target_id}.json"
            msg = f"Online config export for {target_name}"
        else:
            filename = "online_config.json"
            msg = "Online config export. Edit and upload with `/online list file:<attachment>` to apply"
        payload = json.dumps(exported, indent=4, sort_keys=False).encode(config.STR_ENCODE)
        await _ctx_respond(ctx, msg, attachment=hikari.Bytes(payload, filename))


# AiviA APasz
