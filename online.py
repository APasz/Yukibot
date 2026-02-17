from __future__ import annotations

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

ACTIVITY_TYPES = ("games", "music", "streaming", "custom", "other")
ACTIVITY_TYPES_SET = set(ACTIVITY_TYPES)
ONLINE_STATUSES = {"online", "idle", "dnd"}
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
    "Hydration check: drink some water.",
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
}

# Default Discord user IDs to ignore globally.
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

    def to_json(self) -> dict[str, object]:
        data = self.model_dump(mode="json")
        data["types"] = sorted(self.types)
        data["activities"] = sorted(self.activities)
        data["games"] = sorted(self.games)
        return data

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> "WatchRule":
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
        self._last_drink_ping: dict[hikari.Snowflake, datetime] = {}
        self._next_drink_ping_at: dict[hikari.Snowflake, datetime] = {}
        self._game_sessions: dict[hikari.Snowflake, dict[str, datetime]] = {}
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
        del kind  # reserved for future per-kind ignore tuning
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
                }
            )

        drink = self.get_drink_rule(watcher_id) or DrinkRule()
        drink_games = sorted([g for g in drink.games if not self._is_ignored_activity_name(g)])

        return {
            "description": "Edit and re-upload this file with /online list file:<attachment> to replace your config",
            "accepted_values": {
                "types": list(STATUS_TYPES),
                "activities": list(ACTIVITY_TYPES),
                "games_mode": ["all", "include", "exclude"],
                "silent": "true|false (default true: suppress push notifications)",
                "drink.mode": ["include", "exclude"],
            },
            "ignored": {
                "activity_or_game_names": sorted(IGNORED_ACTIVITY_NAMES),
                "users": [str(uid) for uid in sorted(self.ignored_user_ids)],
            },
            "notes": [
                "watches is per target user.",
                "Fields omitted in each watch entry fall back to defaults",
                "If games_mode is all, games list is ignored",
                "Uploading replaces your watch/drink config in one go",
            ],
            "user_editable": "Only values below this line are looked at by the bot",
            "watches": entries,
            "drink": {
                "mode": drink.mode,
                "games": drink_games,
            },
        }

    def apply_user_config(self, watcher_id: hikari.Snowflake, payload: dict[str, object] | Any) -> dict[str, int]:
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")

        watches = payload.get("watches", [])
        if not isinstance(watches, list):
            raise ValueError("watches must be a list")

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

        self._dump()
        return {
            "watches": len(replaced),
            "drink_games": len(self.drink_rules.get(watcher_id, DrinkRule()).games),
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
        if "custom" in name:
            return "custom"
        return "other"

    def _snapshot_from_presence(self, presence: hikari.MemberPresence | None) -> PresenceSnapshot:
        if not presence:
            return PresenceSnapshot(status="offline", platforms={}, activities={}, game_starts={})

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
                continue
            key = activity_name.casefold()
            activities[(kind, key)] = activity_name
            if kind == "games":
                game_starts[key] = self._activity_start_at(activity)

        return PresenceSnapshot(status=status, platforms=platforms, activities=activities, game_starts=game_starts)

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
    def _preferred_platform(snapshot: PresenceSnapshot) -> str | None:
        for platform in PLATFORMS:
            if snapshot.platforms.get(platform) in ONLINE_STATUSES:
                return platform
        if snapshot.platforms:
            return next(iter(snapshot.platforms.keys()))
        return None

    @staticmethod
    def _game_allowed(rule: WatchRule, game_name: str) -> bool:
        game_key = game_name.casefold()
        if rule.games_mode == "all":
            return True
        if rule.games_mode == "include":
            return game_key in rule.games
        return game_key not in rule.games

    def _fmt_status(self, target_id: hikari.Snowflake, change: StatusChange) -> str:
        if change.key == "offline":
            status = "offline"
            platform = None
        else:
            status, platform = change.key.split("-", 1)
        return f"{self._status_emoji(status)} {self._platform_emoji(platform)} <@{target_id}> {status}"

    def _fmt_activity(self, target_id: hikari.Snowflake, change: ActivityChange, snapshot: PresenceSnapshot) -> str:
        status = snapshot.status
        platform = self._preferred_platform(snapshot)
        if change.kind == "games":
            detail = f"{change.action}:{change.name}"
        else:
            detail = f"{change.action}:{change.kind}:{change.name}"
        return f"{self._status_emoji(status)} {self._platform_emoji(platform)} <@{target_id}> {detail}"

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

        # Guard against duplicate event fanout (e.g. same presence update across multiple guild contexts)
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
            # stale DM cache, refresh once
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

        if not old_snapshot or now < self.ready_at:
            return

        status_changes, activity_changes = self._diff(old_snapshot, new_snapshot)
        if not status_changes and not activity_changes:
            return

        watchers = self._watchers_for_target(user_id)
        if not watchers:
            return

        for watcher_id, rule in watchers:
            lines: list[str] = []
            for status_change in status_changes:
                if status_change.key in rule.types:
                    lines.append(self._fmt_status(user_id, status_change))
            for activity_change in activity_changes:
                if activity_change.kind not in rule.activities:
                    continue
                if activity_change.kind == "games" and not self._game_allowed(rule, activity_change.name):
                    continue
                lines.append(self._fmt_activity(user_id, activity_change, new_snapshot))
            if lines:
                await self._notify(bot, watcher_id, lines, silent=rule.silent)


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


def _rule_summary(rule: WatchRule) -> str:
    status_txt = ", ".join(sorted(rule.types)) if rule.types else "(none)"
    activity_txt = ", ".join(sorted(rule.activities)) if rule.activities else "(none)"
    if rule.games_mode == "all":
        games_txt = "all"
    elif not rule.games:
        games_txt = f"{rule.games_mode} (empty)"
    else:
        games_txt = f"{rule.games_mode}: {', '.join(sorted(rule.games))}"
    return f"types: {status_txt}\nactivities: {activity_txt}\ngames: {games_txt}\nsilent: {rule.silent}"


def _drink_summary(tracker: Online_Tracker, user_id: hikari.Snowflake, rule: DrinkRule) -> str:
    games = sorted([tracker._display_game(user_id, game) for game in rule.games], key=str.casefold)
    games_txt = ", ".join(games) if games else "(none)"
    return f"mode: {rule.mode}\ngames: {games_txt}"


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
        "Suppress push notifications for this watch target (default=true)",
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

        if self.status_type and tracker.add_type(watcher_id, target_id, self.status_type):
            changes.append(f"added type: {self.status_type}")
        if self.activity and tracker.add_activity(watcher_id, target_id, self.activity):
            changes.append(f"added activity: {self.activity}")
        if self.game:
            result = tracker.add_game(watcher_id, target_id, self.game)
            if result != "no change":
                changes.append(result)
        if self.silent is not None and tracker.set_rule_silent(watcher_id, target_id, self.silent):
            changes.append(f"silent: {self.silent}")

        if not any([self.status_type, self.activity, self.game, self.silent is not None]):
            await ctx.respond(f"Watching {target_name} with default filters")
            return

        if not changes and not created:
            await ctx.respond(f"No changes for {target_name}")
            return
        if not changes and created:
            await ctx.respond(f"Watching {target_name} with default filters")
            return

        await ctx.respond(f"Updated watch for {target_name}\n" + "\n".join(f"- {line}" for line in changes))


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
            await ctx.respond(f"No watch config found for {target_name}")
            return

        if not any([self.status_type, self.activity, self.game]):
            await ctx.respond("No filters passed. Use `/online unwatch` to clear all config for this user.")
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
            await ctx.respond(f"No matching filters were set for {target_name}")
            return

        await ctx.respond(f"Updated watch for {target_name}\n" + "\n".join(f"- {line}" for line in changes))


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

        await ctx.respond("\n".join(lines))


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

        await ctx.respond(f"Drink reminder {action}: {game_name}\n{_drink_summary(tracker, ctx.user.id, rule)}")


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
            await ctx.defer()
            path = await File_Utils.download_temp(self.file)
            try:
                payload = json.loads(path.read_text(config.STR_ENCODE))
            except json.JSONDecodeError as xcp:
                raise ValueError(f"Invalid JSON file: {xcp}") from xcp
            if not isinstance(payload, dict):
                raise ValueError("Invalid JSON file: top-level object expected")
            result = tracker.apply_user_config(watcher_id, payload)
            await ctx.respond(
                "Online config updated from file\n"
                f"- watches: {result['watches']}\n"
                f"- drink games: {result['drink_games']}\n"
                f"- skipped ignored users: {result['skipped_ignored_users']}"
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
            msg = "Online config export. Edit and upload with `/online list file:<attachment>` to apply."
        payload = json.dumps(exported, indent=4, sort_keys=False).encode(config.STR_ENCODE)
        await ctx.respond(msg, attachment=hikari.Bytes(payload, filename))


# AiviA APasz
