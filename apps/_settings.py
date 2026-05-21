import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

import hikari

from _security import Power_Level
from apps._config import App_Config

log = logging.getLogger(__name__)


class Setting_Label(StrEnum):
    serv_name = "Server Name"
    serv_desc = "Server Description"
    max_player = "Max Players"
    map_name = "Map"
    motd = "MOTD"
    visibility = "Public"
    password = "Password"
    difficulty = "Difficulty"


T = TypeVar("T")


class Setting(Generic[T]):
    """Represents a config option with metadata, casting, and validation"""

    value_type: Callable[[Any], T]
    label: str
    key: str
    path: list[str]
    value: T | hikari.UndefinedType
    choices: list[str] | dict[str, str]
    strict_choice: bool
    validator: Callable[[str], bool] | None
    power_level: Power_Level
    desc: str | None

    def __init__(
        self,
        value_type: Callable[[Any], T],
        label: str | Setting_Label,
        key: str,
        path: list[str],
        value: T | hikari.UndefinedType = hikari.UNDEFINED,
        *,
        choices: list[str] | dict[str, str] | None = None,
        strict_choice: bool = True,
        validator: Callable[[str], bool] | None = None,
        power_level: Power_Level = Power_Level.guest,
        desc: str | None = None,
    ):
        if not callable(value_type):
            raise TypeError("Type must be a callable that casts input to the expected type")  # pyright: ignore[reportUnreachable]
        self.value_type = value_type
        self.path = path
        self.key = key
        if not isinstance(value, hikari.UndefinedType):
            self.update(str(value))
        else:
            self.value = value
        self.choices = choices or []
        if isinstance(label, Setting_Label):
            label = label.value
        self.strict_choice = strict_choice
        self.label = label.title()
        self.validator = validator
        self.power_level = power_level
        self.desc = desc

    def get(self, data: dict) -> T | hikari.UndefinedType:
        try:
            for key in self.path:
                data = data[key]
            value = data.get(self.key, hikari.UNDEFINED)
        except KeyError:
            log.error(f"App Setting not found @ {'/'.join(self.path + [self.key])}")
            value = hikari.UNDEFINED
        self.value = value
        return value

    def set(self, data: dict):
        for key in self.path:
            data = data.setdefault(key, {})
        data[self.key] = self.value

    def normalise_input(self, value: str) -> str:
        if isinstance(self.choices, dict):
            return self.choices.get(value, value)
        return value

    def choice_items(self) -> tuple[tuple[str, str], ...]:
        if isinstance(self.choices, dict):
            return tuple(self.choices.items())
        return tuple((choice, choice) for choice in self.choices)

    def choice_label_for_value(self, value: T | hikari.UndefinedType | None = None) -> str | None:
        current_value = self.value if value is None else value
        if isinstance(current_value, hikari.UndefinedType):
            return None

        for label, raw_value in self.choice_items():
            try:
                if self._cast_value(raw_value) == current_value:
                    return label
            except Exception:
                continue
        return None

    def display_value(self) -> str:
        if isinstance(self.value, hikari.UndefinedType):
            return "undefined"

        label = self.choice_label_for_value(self.value)
        raw_value = str(self.value)
        if label is None or label == raw_value:
            return raw_value
        return f"{label} ({raw_value})"

    def _cast_value(self, value: str) -> T:
        if self.value_type is bool:
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return cast(T, True)
            if lowered in {"0", "false", "no", "off"}:
                return cast(T, False)
            raise ValueError(f"{value} is not recognisable bool equivalent")
        return self.value_type(value)

    def update(self, value: str):
        value = self.normalise_input(value)
        if self.validator:
            if not self.validator(value):
                raise ValueError(f"`{value}` not valid")
        if self.choices and self.strict_choice:
            if value not in self.choices.values() if isinstance(self.choices, dict) else value not in self.choices:
                raise IndexError(f"{value} must match provided choices")
        try:
            self.value = self._cast_value(value)
        except Exception as xcp:
            log.exception(f"Casting Setting value Failed: {type(value)} > {self.value_type}")
            raise ValueError(f"Invalid value for {self.label}: {xcp}")

    def __str__(self) -> str:
        return self.label

    def __repr__(self) -> str:
        return self.key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Setting):
            raise TypeError(f"'==' not supported between instances of '{type(self)}' and '{type(other)}'")
        return self.value == other.value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Setting):
            return NotImplemented
        return (
            self.label.lower() < other.label.lower()
            if self.label != other.label
            else self.key.lower() < other.key.lower()
        )


class App_Settings:
    _lookup: dict[str, Setting]

    def __init__(self, pointer: Path, options: list[Setting]) -> None:
        self.pointer = pointer
        if not pointer.exists():
            raise FileNotFoundError("App_Settings file missing")
        self._lookup = {}

        self.options: list[Setting] = sorted(options)
        for setting in options:
            self._lookup[setting.label.lower()] = setting
            self._lookup[setting.key.lower()] = setting
        self.load()

    def load(self):
        raise NotImplementedError

    def save(self):
        raise NotImplementedError

    @property
    def friendly_options(self) -> list[str]:
        return [s.label for s in self.options]

    def get_setting(self, ident: str) -> Setting | None:
        ident = ident.lower()
        if ident not in self._lookup:
            return None
        return self._lookup[ident]

    @property
    def max_player(self) -> int | None:
        setting = self.get_setting(Setting_Label.max_player)
        if setting and isinstance(setting.value, int):
            return setting.value
        return None

    @property
    def server_name(self) -> str | None:
        setting = self.get_setting(Setting_Label.serv_name)
        if setting and isinstance(setting.value, str):
            return setting.value
        return None


class Settings_Manager:
    def __init__(self, config: App_Config, settings: App_Settings) -> None:
        self.config = config
        self.app = settings


# AiviA APasz
