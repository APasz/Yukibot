from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, cast

from _security import Power_Level
from apps._config import AppVersion, normalise_app_version
from apps._settings import ChoiceSpec

T_co = TypeVar("T_co", covariant=True)


class ConsoleResponseSource(enum.StrEnum):
    NONE = "none"
    API = "api"
    RCON = "rcon"
    TELNET = "telnet"


@dataclass(frozen=True, slots=True)
class ConsoleActionResult:
    summary: str
    success: bool = True
    text: str | None = None
    source: ConsoleResponseSource = ConsoleResponseSource.NONE


ConsoleExecutor = Callable[[object, object | None], Awaitable[ConsoleActionResult]]
ConsoleActionAuthorizer = Callable[["ConsoleAction"], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ConsoleActionParameter(Generic[T_co]):
    key: str
    label: str
    value_type: Callable[[Any], T_co]
    choices: ChoiceSpec | None = None
    validator: Callable[[str], bool] | None = None
    desc: str | None = None
    max_length: int = 200
    multiline: bool = False
    _recent_inputs: list[str] = field(default_factory=list, init=False, repr=False, compare=False)

    @property
    def choice_spec(self) -> ChoiceSpec | None:
        return self.choices

    @property
    def strict_choice(self) -> bool:
        return self.choice_spec.strict if self.choice_spec is not None else False

    def normalise_input(self, value: str) -> str:
        if self.choice_spec is None:
            return value
        return self.choice_spec.normalise_input(value)

    def choice_items(self) -> tuple[tuple[str, str], ...]:
        if self.choice_spec is None:
            return ()
        return self.choice_spec.choice_items()

    def _cast_value(self, value: str) -> T_co:
        if self.value_type is bool:
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return cast(T_co, True)
            if lowered in {"0", "false", "no", "off"}:
                return cast(T_co, False)
            raise ValueError(f"{value} is not recognisable bool equivalent")
        return self.value_type(value)

    def parse(self, raw_value: str) -> T_co:
        value = self.normalise_input(raw_value.strip())
        if self.validator is not None and not self.validator(value):
            raise ValueError(f"`{value}` not valid")
        if self.choice_spec is not None and self.strict_choice:
            if value not in self.choice_spec.raw_values():
                raise ValueError(f"{value} must match provided choices")
        return self._cast_value(value)

    @property
    def recent_inputs(self) -> tuple[str, ...]:
        return tuple(self._recent_inputs)

    @property
    def supports_recent_inputs(self) -> bool:
        return self.value_type in {str, int} and (self.choice_spec is None or not self.strict_choice)

    @property
    def value_type_name(self) -> str:
        return getattr(self.value_type, "__name__", type(self.value_type).__name__)

    def remember_input(self, raw_value: str) -> None:
        if not self.supports_recent_inputs:
            return
        value = self.normalise_input(raw_value.strip())
        if not value:
            return
        self._recent_inputs[:] = [item for item in self._recent_inputs if item != value]
        self._recent_inputs.insert(0, value)
        del self._recent_inputs[25:]

    def display_value(self, value: object) -> str:
        for label, raw_value in self.choice_items():
            try:
                if self._cast_value(raw_value) == value:
                    if label == str(value):
                        return str(value)
                    return f"{label} ({value})"
            except Exception:
                continue
        return str(value)


@dataclass(frozen=True, slots=True)
class ConsoleAction:
    key: str
    label: str
    description: str
    power_level: Power_Level
    execute: ConsoleExecutor
    parameter: ConsoleActionParameter[object] | None = None
    requires_running: bool = True
    transport: ConsoleResponseSource = ConsoleResponseSource.NONE
    min_app_version: AppVersion | str | None = None
    max_app_version: AppVersion | str | None = None

    def __post_init__(self) -> None:
        min_app_version = normalise_app_version(self.min_app_version)
        max_app_version = normalise_app_version(self.max_app_version)
        object.__setattr__(self, "min_app_version", min_app_version)
        object.__setattr__(self, "max_app_version", max_app_version)
        if (
            min_app_version is not None
            and max_app_version is not None
            and min_app_version.compare_main_and_build(max_app_version) > 0
        ):
            raise ValueError("Console action minimum app version must not exceed maximum app version.")

    def supports_app_version(self, app_version: AppVersion | None) -> bool:
        if isinstance(self.min_app_version, str) or isinstance(self.max_app_version, str):
            raise RuntimeError("Console action app version bounds must be normalised during initialisation.")
        if self.min_app_version is None and self.max_app_version is None:
            return True
        if app_version is None:
            return False
        min_app_version = self.min_app_version
        if min_app_version is not None and not app_version.is_at_least(min_app_version):
            return False
        max_app_version = self.max_app_version
        if max_app_version is not None and not app_version.is_at_most(max_app_version):
            return False
        return True


async def execute_console_action(
    *,
    app: object,
    is_running: Callable[[], bool],
    action: ConsoleAction,
    raw_value: str | None,
) -> ConsoleActionResult:
    if action.requires_running and not is_running():
        app_friendly = getattr(app, "friendly", "App")
        raise RuntimeError(f"{app_friendly} is not running.")
    parameter = action.parameter
    parsed_value: object | None = None
    if parameter is not None:
        if raw_value is None or not raw_value.strip():
            raise ValueError(f"{parameter.label} must not be empty.")
        parsed_value = parameter.parse(raw_value)
        parameter.remember_input(raw_value)
    ensure_console_action_allowed = getattr(app, "ensure_console_action_allowed", None)
    if ensure_console_action_allowed is not None:
        if not callable(ensure_console_action_allowed):
            raise TypeError("Console action authorizer must be callable.")
        await cast(ConsoleActionAuthorizer, ensure_console_action_allowed)(action)
    return await action.execute(app, parsed_value)
