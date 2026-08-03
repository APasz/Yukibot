import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from logging import Logger
from pathlib import Path
from typing import Any, Generic, TypeAlias, TypeVar, cast

import hikari

from _audit import audit_log
from _security import Power_Level
from apps._config import App_Config, AppVersion, normalise_app_version

log: Logger = logging.getLogger(__name__)
HideRevealLevel: TypeAlias = Power_Level | None
DraftSettingValue: TypeAlias = object | hikari.UndefinedType
SettingStateValue: TypeAlias = str | bool | int | float


class Setting_Label(StrEnum):
    serv_name = "Server Name"
    serv_desc = "Server Description"
    max_player = "Max Players"
    map_name = "Map"
    motd = "MOTD"
    visibility = "Public"
    password = "Password"
    difficulty = "Difficulty"


class Setting_Group(StrEnum):
    """Extensible base for app-specific setting groups."""

    @property
    def sort_order(self) -> int:
        """Return this group's declaration order within its app-specific enum."""
        return tuple(type(self)).index(self)


T = TypeVar("T", default=object)


def _is_non_negative_int_text(raw_value: str) -> bool:
    return raw_value.isdigit()


def _is_signed_int_text(raw_value: str) -> bool:
    if raw_value.startswith("-"):
        return raw_value[1:].isdigit()
    return raw_value.isdigit()


def _compose_raw_validator(
    primary: Callable[[str], bool],
    secondary: Callable[[str], bool] | None,
) -> Callable[[str], bool]:
    if secondary is None:
        return primary
    return lambda raw_value: primary(raw_value) and secondary(raw_value)


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    value: str
    label: str = ""

    def __init__(self, value: str, label: str = "") -> None:
        if not value:
            raise ValueError("ChoiceOption requires at least value")

        object.__setattr__(self, "value", value)
        object.__setattr__(self, "label", label or value)


@dataclass(frozen=True, slots=True, init=False)
class ChoiceSpec:
    options: tuple[ChoiceOption, ...]
    strict: bool = True

    def __init__(self, *options: ChoiceOption, strict: bool = True) -> None:
        if not options:
            raise ValueError("ChoiceSpec requires at least one option")

        object.__setattr__(self, "options", tuple[ChoiceOption, ...](options))
        object.__setattr__(self, "strict", strict)

    def normalise_input(self, value: str) -> str:
        for option in self.options:
            if option.label == value:
                return option.value
        return value

    def choice_items(self) -> tuple[tuple[str, str], ...]:
        return tuple[tuple[str, str], ...]((option.label, option.value) for option in self.options)

    def raw_values(self) -> frozenset[str]:
        return frozenset[str](option.value for option in self.options)


@dataclass(frozen=True, slots=True)
class ForcedSettingState:
    setting_key: str
    value: SettingStateValue

    def __post_init__(self) -> None:
        if not self.setting_key.strip():
            raise ValueError("ForcedSettingState requires a setting_key.")


@dataclass(frozen=True, slots=True, init=False)
class SettingStateForceRule:
    when_value: SettingStateValue
    forced_states: tuple[ForcedSettingState, ...]

    def __init__(self, when_value: SettingStateValue, *forced_states: ForcedSettingState) -> None:
        if not forced_states:
            raise ValueError("SettingStateForceRule requires at least one forced state.")

        object.__setattr__(self, "when_value", when_value)
        object.__setattr__(self, "forced_states", tuple[ForcedSettingState, ...](forced_states))


class SettingSpec(Generic[T]):
    """Shared parsing, validation, and UI metadata for a setting type."""

    def __init__(
        self,
        python_type: type[T],
        choice_spec: ChoiceSpec | None = None,
        *,
        allow_blank: bool = False,
        is_sensitive: bool = False,
        do_hide: HideRevealLevel = None,
        raw_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.python_type: type[T] = python_type
        self.allow_blank: bool = allow_blank
        self.is_sensitive: bool = is_sensitive
        self.do_hide: HideRevealLevel = do_hide
        self.choice_spec: ChoiceSpec | None = choice_spec
        self.raw_validator: Callable[[str], bool] | None = raw_validator

    @property
    def type_name(self) -> str:
        return getattr(self.python_type, "__name__", type(self.python_type).__name__)

    @property
    def supports_recent_inputs(self) -> bool:
        return self.python_type in {str, int} and (self.choice_spec is None or not self.choice_spec.strict)

    @property
    def strict_choice(self) -> bool:
        return self.choice_spec.strict if self.choice_spec is not None else False

    @property
    def choices(self) -> ChoiceSpec | None:
        return self.choice_spec

    def normalise_input(self, value: str) -> str:
        if self.choice_spec is None:
            return value
        return self.choice_spec.normalise_input(value)

    def choice_items(self) -> tuple[tuple[str, str], ...]:
        if self.choice_spec is None:
            return ()
        return self.choice_spec.choice_items()

    def _parse_input(self, raw_value: str, *, clamp_loaded_range: bool = False) -> T:
        value = self.normalise_input(raw_value)
        if value == "" and self.allow_blank:
            return self.blank_value()
        if self.raw_validator is not None and not self.raw_validator(value):
            raise ValueError(f"`{value}` not valid")
        if self.choice_spec is not None and self.choice_spec.strict and value not in self.choice_spec.raw_values():
            raise IndexError(f"{value} must match provided choices")
        parsed = self.parse(value)
        if clamp_loaded_range:
            parsed = self.coerce_loaded_value(parsed)
        self.validate_value(parsed)
        return parsed

    def parse_input(self, raw_value: str) -> T:
        return self._parse_input(raw_value)

    def parse_loaded_input(self, raw_value: str) -> T:
        return self._parse_input(raw_value, clamp_loaded_range=True)

    def choice_label_for_value(self, value: T | hikari.UndefinedType) -> str | None:
        if self.choice_spec is None or isinstance(value, hikari.UndefinedType):
            return None
        for option in self.choice_spec.options:
            try:
                if self.parse(option.value) == value:
                    return option.label
            except Exception:
                continue
        return None

    def display_value(self, value: T | hikari.UndefinedType) -> str:
        if isinstance(value, hikari.UndefinedType):
            return "undefined"
        label = self.choice_label_for_value(value)
        raw_value = str(value)
        if label is None or label == raw_value:
            return raw_value
        return f"{label} ({raw_value})"

    def serialise_value(self, value: T | hikari.UndefinedType) -> str:
        if isinstance(value, hikari.UndefinedType):
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def blank_value(self) -> T:
        raise ValueError("Blank input is not supported for this setting type.")

    def parse(self, raw_value: str) -> T:
        raise NotImplementedError

    def validate_value(self, value: T) -> None:
        del value

    def coerce_loaded_value(self, value: T) -> T:
        return value


class StringSettingSpec(SettingSpec[str]):
    def __init__(
        self,
        choice_spec: ChoiceSpec | None = None,
        *,
        allow_blank: bool = False,
        is_sensitive: bool = False,
        do_hide: HideRevealLevel = None,
        raw_validator: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(
            str,
            choice_spec,
            allow_blank=allow_blank,
            is_sensitive=is_sensitive,
            do_hide=do_hide,
            raw_validator=raw_validator,
        )

    def blank_value(self) -> str:
        return ""

    def parse(self, raw_value: str) -> str:
        return raw_value


_DEFAULT_BOOL_CHOICE_SPEC = ChoiceSpec(
    ChoiceOption("true", "Enabled"),
    ChoiceOption("false", "Disabled"),
)


class BoolSettingSpec(SettingSpec[bool]):
    def __init__(
        self,
        choice_spec: ChoiceSpec | None = None,
        *,
        is_sensitive: bool = False,
        do_hide: HideRevealLevel = None,
        raw_validator: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(
            bool,
            choice_spec or _DEFAULT_BOOL_CHOICE_SPEC,
            is_sensitive=is_sensitive,
            do_hide=do_hide,
            raw_validator=raw_validator,
        )

    def parse(self, raw_value: str) -> bool:
        lowered = raw_value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{raw_value} is not recognisable bool equivalent")


class IntSettingSpec(SettingSpec[int]):
    def __init__(
        self,
        choice_spec: ChoiceSpec | None = None,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        allow_negative: bool = False,
        is_sensitive: bool = False,
        do_hide: HideRevealLevel = None,
        raw_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.allow_negative = allow_negative
        builtin_validator = _is_signed_int_text if allow_negative else _is_non_negative_int_text
        super().__init__(
            int,
            choice_spec,
            is_sensitive=is_sensitive,
            do_hide=do_hide,
            raw_validator=_compose_raw_validator(builtin_validator, raw_validator),
        )
        self.min_value = min_value
        self.max_value = max_value

    def parse(self, raw_value: str) -> int:
        return int(raw_value)

    def coerce_loaded_value(self, value: int) -> int:
        if self.min_value is not None and value < self.min_value:
            return self.min_value
        if self.max_value is not None and value > self.max_value:
            return self.max_value
        return value

    def validate_value(self, value: int) -> None:
        if self.min_value is not None and value < self.min_value:
            raise ValueError(f"{value} must be at least {self.min_value}")
        if self.max_value is not None and value > self.max_value:
            raise ValueError(f"{value} must be at most {self.max_value}")


class FloatSettingSpec(SettingSpec[float]):
    def __init__(
        self,
        choice_spec: ChoiceSpec | None = None,
        *,
        min_value: float | None = None,
        max_value: float | None = None,
        is_sensitive: bool = False,
        do_hide: HideRevealLevel = None,
        raw_validator: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(
            float,
            choice_spec,
            is_sensitive=is_sensitive,
            do_hide=do_hide,
            raw_validator=raw_validator,
        )
        self.min_value = min_value
        self.max_value = max_value

    def parse(self, raw_value: str) -> float:
        return float(raw_value)

    def validate_value(self, value: float) -> None:
        if self.min_value is not None and value < self.min_value:
            raise ValueError(f"{value} must be at least {self.min_value}")
        if self.max_value is not None and value > self.max_value:
            raise ValueError(f"{value} must be at most {self.max_value}")


class Setting(Generic[T]):
    """Represents a config option with metadata, current value, and a typed spec."""

    label: str
    key: str
    path: tuple[str, ...]
    spec: SettingSpec[T]
    value: T | hikari.UndefinedType
    default: T
    power_level: Power_Level
    group: Setting_Group | None
    desc: str | None
    paragraph: bool
    min_app_version: AppVersion | None
    max_app_version: AppVersion | None
    forced_state_rules: tuple[SettingStateForceRule, ...]
    _recent_inputs: list[str]

    def __init__(
        self,
        value_type: SettingSpec[T],
        label: str | Setting_Label,
        key: str,
        path: Sequence[str],
        *,
        default: T,
        value: T | hikari.UndefinedType = hikari.UNDEFINED,
        power_level: Power_Level = Power_Level.admin,
        group: Setting_Group | None = None,
        desc: str | None = None,
        paragraph: bool = False,
        min_app_version: AppVersion | str | None = None,
        max_app_version: AppVersion | str | None = None,
        forced_state_rules: Sequence[SettingStateForceRule] = (),
    ) -> None:
        self.spec = value_type
        self.path = tuple(path)
        self.key = key
        self.default = self._normalise_value(default)
        if not isinstance(value, hikari.UndefinedType):
            self.value = self._normalise_value(value)
        else:
            self.value = value
        if isinstance(label, Setting_Label):
            label = label.value
        self.label = label.title()
        self.power_level = power_level
        if group is not None and not isinstance(group, Setting_Group):
            raise TypeError("Setting group must be a Setting_Group value or None.")
        self.group = group
        self.desc = desc
        self.paragraph = paragraph
        self.min_app_version = normalise_app_version(min_app_version)
        self.max_app_version = normalise_app_version(max_app_version)
        self.forced_state_rules = tuple(forced_state_rules)
        if (
            self.min_app_version is not None
            and self.max_app_version is not None
            and self.min_app_version.compare_main_and_build(self.max_app_version) > 0
        ):
            raise ValueError("Setting minimum app version must not exceed maximum app version.")
        self._recent_inputs = []

    @property
    def value_type(self) -> type[T]:
        return self.spec.python_type

    def _normalise_value(self, value: T) -> T:
        return self.spec.parse_input(self.spec.serialise_value(value))

    def get(self, data: dict[str, Any]) -> T | hikari.UndefinedType:
        try:
            for key in self.path:
                data = data[key]
            raw_value = data.get(self.key, hikari.UNDEFINED)
        except KeyError:
            log.warning("App setting missing, using default @ %s", "/".join((*self.path, self.key)))
            self.value = self.default
            return self.default
        if isinstance(raw_value, hikari.UndefinedType):
            log.warning("App setting missing, using default @ %s", "/".join((*self.path, self.key)))
            self.value = self.default
            return self.default
        try:
            value = self.spec.parse_loaded_input(self.spec.serialise_value(cast(T, raw_value)))
        except Exception as xcp:
            log.exception("Loading setting value failed: %s > %s", type(raw_value), self.type_name)
            raise ValueError(f"Invalid stored value for {self.label}: {xcp}") from xcp
        self.value = value
        return value

    def set(self, data: dict[str, Any]) -> None:
        for key in self.path:
            data = data.setdefault(key, {})
        data[self.key] = self.value

    def normalise_input(self, value: str) -> str:
        return self.spec.normalise_input(value)

    def choice_items(self) -> tuple[tuple[str, str], ...]:
        return self.spec.choice_items()

    def choice_label_for_value(self, value: T | hikari.UndefinedType | None = None) -> str | None:
        current_value = self.value if value is None else value
        if current_value is None:
            return None
        return self.spec.choice_label_for_value(current_value)

    def display_value(self) -> str:
        return self.spec.display_value(self.value)

    def serialise_value(self) -> str:
        return self.spec.serialise_value(self.value)

    @property
    def recent_inputs(self) -> tuple[str, ...]:
        return tuple(self._recent_inputs)

    @property
    def supports_recent_inputs(self) -> bool:
        return self.spec.supports_recent_inputs and self.do_hide is None

    @property
    def allows_blank_input(self) -> bool:
        return self.spec.allow_blank

    @property
    def is_sensitive(self) -> bool:
        return self.spec.is_sensitive

    @property
    def do_hide(self) -> HideRevealLevel:
        return self.spec.do_hide

    @property
    def strict_choice(self) -> bool:
        return self.spec.strict_choice

    @property
    def choices(self) -> ChoiceSpec | None:
        return self.spec.choices

    @property
    def validator(self) -> Callable[[str], bool] | None:
        return self.spec.raw_validator

    @property
    def type_name(self) -> str:
        return self.spec.type_name

    def supports_app_version(self, app_version: AppVersion | None) -> bool:
        if self.min_app_version is None and self.max_app_version is None:
            return True
        if app_version is None:
            return False
        if self.min_app_version is not None and not app_version.is_at_least(self.min_app_version):
            return False
        if self.max_app_version is not None and not app_version.is_at_most(self.max_app_version):
            return False
        return True

    def _remember_input(self, value: str) -> None:
        if not self.supports_recent_inputs:
            return
        recent_value = value.strip()
        if not recent_value:
            return
        self._recent_inputs = [item for item in self._recent_inputs if item != recent_value]
        self._recent_inputs.insert(0, recent_value)
        del self._recent_inputs[25:]

    def update(self, value: str, *, remember_input: bool = False) -> None:
        try:
            self.value = self.spec.parse_input(value)
        except Exception as xcp:
            log.exception(f"Casting Setting value Failed: {type(value)} > {self.type_name}")
            raise ValueError(f"Invalid value for {self.label}: {xcp}")
        if remember_input:
            self._remember_input(self.serialise_value())

    def load_value(self, value: str) -> None:
        try:
            self.value = self.spec.parse_loaded_input(value)
        except Exception as xcp:
            log.exception("Loading stored setting value failed: %s > %s", type(value), self.type_name)
            raise ValueError(f"Invalid stored value for {self.label}: {xcp}") from xcp

    def __str__(self) -> str:
        return self.label

    def __repr__(self) -> str:
        return self.key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Setting):
            return NotImplemented
        return self.value == other.value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Setting):
            return NotImplemented
        return self._sort_key < other._sort_key

    @property
    def _sort_key(self) -> tuple[int, str, int, str, str]:
        if self.group is None:
            return (0, "", 0, self.label.casefold(), self.key.casefold())
        group_type = type(self.group)
        group_type_id = f"{group_type.__module__}.{group_type.__qualname__}"
        return (1, group_type_id, self.group.sort_order, self.label.casefold(), self.key.casefold())


class App_Settings:
    _lookup: dict[str, Setting[Any]]
    _settings_by_key: dict[str, Setting[Any]]

    def __init__(
        self,
        pointer: Path,
        options: list[Setting[Any]],
        *,
        version_getter: Callable[[], AppVersion | None] | None = None,
    ) -> None:
        self.pointer = pointer
        if not pointer.exists():
            raise FileNotFoundError("App_Settings file missing")
        self._lookup = {}
        self._settings_by_key = {}
        self._version_getter = version_getter

        self._options: list[Setting[Any]] = sorted(options)
        for setting in self._options:
            self._lookup.setdefault(setting.label.lower(), setting)
        for setting in self._options:
            self._lookup[setting.key.lower()] = setting
            self._settings_by_key[setting.key.lower()] = setting
        self._validate_forced_state_rules()
        self.load()

    def set_version_getter(self, version_getter: Callable[[], AppVersion | None] | None) -> None:
        self._version_getter = version_getter

    @property
    def has_version_getter(self) -> bool:
        return self._version_getter is not None

    @property
    def app_version(self) -> AppVersion | None:
        if self._version_getter is None:
            return None
        return self._version_getter()

    @property
    def options(self) -> list[Setting[Any]]:
        app_version = self.app_version
        return [setting for setting in self._options if setting.supports_app_version(app_version)]

    def load(self):
        raise NotImplementedError

    def save(self):
        raise NotImplementedError

    def apply_draft_update(
        self,
        *,
        setting: Setting[Any],
        value: object,
        drafts: dict[str, DraftSettingValue],
    ) -> None:
        if value == setting.value:
            drafts.pop(setting.key, None)
            return
        drafts[setting.key] = value

    def _setting_for_exact_key(self, setting_key: str) -> Setting[Any] | None:
        return self._settings_by_key.get(setting_key.casefold())

    @staticmethod
    def _normalise_declared_setting_value(
        setting: Setting[Any],
        value: SettingStateValue,
        *,
        context: str,
    ) -> object:
        try:
            return setting.spec.parse_input(setting.spec.serialise_value(cast(Any, value)))
        except Exception as xcp:
            raise ValueError(f"{context}: {xcp}") from xcp

    def _validate_forced_state_rules(self) -> None:
        for setting in self._options:
            for rule in setting.forced_state_rules:
                self._normalise_declared_setting_value(
                    setting,
                    rule.when_value,
                    context=f"Invalid forced-state trigger for {setting.key}",
                )
                seen_target_keys: set[str] = set[str]()
                for forced_state in rule.forced_states:
                    target_setting = self._setting_for_exact_key(forced_state.setting_key)
                    if target_setting is None:
                        raise ValueError(
                            f"Forced-state rule for {setting.key} references unknown setting {forced_state.setting_key!r}."
                        )
                    target_key = target_setting.key.casefold()
                    if target_key == setting.key.casefold():
                        raise ValueError(f"Forced-state rule for {setting.key} cannot target itself.")
                    if target_key in seen_target_keys:
                        raise ValueError(
                            f"Forced-state rule for {setting.key} targets {target_setting.key!r} more than once."
                        )
                    seen_target_keys.add(target_key)
                    if setting.power_level < target_setting.power_level:
                        raise ValueError(
                            f"Forced-state rule for {setting.key} cannot target higher-permission setting "
                            f"{target_setting.key}."
                        )
                    self._normalise_declared_setting_value(
                        target_setting,
                        forced_state.value,
                        context=f"Invalid forced-state value for {setting.key} -> {target_setting.key}",
                    )

    def resolve_draft_values(self, drafts: Mapping[str, DraftSettingValue]) -> dict[str, object]:
        resolved: dict[str, object] = {}
        supported_settings = tuple(self.options)
        for setting in supported_settings:
            draft_value = drafts.get(setting.key, hikari.UNDEFINED)
            if isinstance(draft_value, hikari.UndefinedType):
                continue
            resolved[setting.key] = draft_value

        total_rule_count = sum(len(setting.forced_state_rules) for setting in supported_settings)
        max_iterations = max(1, len(supported_settings) * max(1, total_rule_count))

        for _ in range(max_iterations):
            next_resolved = dict(resolved)
            forced_assignments: dict[str, tuple[object, str]] = {}
            for setting in supported_settings:
                effective_value = resolved.get(setting.key, setting.value)
                if isinstance(effective_value, hikari.UndefinedType):
                    continue
                for rule in setting.forced_state_rules:
                    rule_value = self._normalise_declared_setting_value(
                        setting,
                        rule.when_value,
                        context=f"Invalid forced-state trigger for {setting.key}",
                    )
                    if effective_value != rule_value:
                        continue
                    for forced_state in rule.forced_states:
                        target_setting = self.get_setting(forced_state.setting_key)
                        if target_setting is None:
                            continue
                        forced_value = self._normalise_declared_setting_value(
                            target_setting,
                            forced_state.value,
                            context=f"Invalid forced-state value for {setting.key} -> {target_setting.key}",
                        )
                        existing_assignment = forced_assignments.get(target_setting.key)
                        if existing_assignment is not None and existing_assignment[0] != forced_value:
                            raise ValueError(
                                "Conflicting forced-state rules for "
                                f"{target_setting.key}: {existing_assignment[1]} vs {setting.key}."
                            )
                        forced_assignments[target_setting.key] = (forced_value, setting.key)

            for target_key, (forced_value, _) in forced_assignments.items():
                target_setting = self.get_setting(target_key)
                if target_setting is None:
                    continue
                if forced_value == target_setting.value:
                    next_resolved.pop(target_key, None)
                    continue
                next_resolved[target_key] = forced_value

            if next_resolved == resolved:
                return next_resolved
            resolved = next_resolved

        raise ValueError("Forced setting-state rules did not converge.")

    @property
    def friendly_options(self) -> list[str]:
        return [s.label for s in self.options]

    def get_setting(self, ident: str) -> Setting[Any] | None:
        ident = ident.lower()
        setting = self._setting_for_exact_key(ident)
        if setting is None:
            setting = self._lookup.get(ident)
        if setting is None or not setting.supports_app_version(self.app_version):
            return None
        return setting

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
        self._drafts: dict[int, dict[str, object | hikari.UndefinedType]] = {}
        had_version_getter = self.app.has_version_getter
        self.app.set_version_getter(lambda: self.config.version)
        if not had_version_getter:
            self.app.load()

    def _prune_unsupported_drafts(self) -> None:
        supported_keys = {setting.key for setting in self.app.options}
        for actor_key, drafts in tuple(self._drafts.items()):
            for setting_key in tuple(drafts):
                if setting_key in supported_keys:
                    continue
                drafts.pop(setting_key, None)
            if not drafts:
                self._drafts.pop(actor_key, None)

    def _actor_key(self, actor_user_id: int) -> int:
        return int(actor_user_id)

    def _draft_bucket(self, actor_user_id: int) -> dict[str, object | hikari.UndefinedType]:
        actor_key = self._actor_key(actor_user_id)
        return self._drafts.setdefault(actor_key, {})

    def _delete_empty_bucket(self, actor_user_id: int) -> None:
        actor_key = self._actor_key(actor_user_id)
        if not self._drafts.get(actor_key):
            self._drafts.pop(actor_key, None)

    def _prune_redundant_drafts(self) -> None:
        self._prune_unsupported_drafts()
        for actor_key, drafts in tuple(self._drafts.items()):
            resolved_drafts = self.app.resolve_draft_values(drafts)
            for setting in self.app.options:
                draft_value = drafts.get(setting.key, hikari.UNDEFINED)
                if isinstance(draft_value, hikari.UndefinedType):
                    continue
                if resolved_drafts.get(setting.key, setting.value) == setting.value:
                    drafts.pop(setting.key, None)
            if not drafts:
                self._drafts.pop(actor_key, None)

    def has_pending_changes(self, actor_user_id: int) -> bool:
        self._prune_unsupported_drafts()
        return bool(self._resolved_drafts(actor_user_id))

    def pending_change_count(self, actor_user_id: int) -> int:
        self._prune_unsupported_drafts()
        return len(self._resolved_drafts(actor_user_id))

    def has_pending_value(self, actor_user_id: int, setting: Setting[Any]) -> bool:
        self._prune_unsupported_drafts()
        return setting.key in self._resolved_drafts(actor_user_id)

    def pending_change_level(self, actor_user_id: int) -> Power_Level | None:
        self._prune_unsupported_drafts()
        drafts = self._resolved_drafts(actor_user_id)
        if not drafts:
            return None
        highest_level: Power_Level | None = None
        for setting in self.app.options:
            if setting.key not in drafts:
                continue
            if highest_level is None or setting.power_level > highest_level:
                highest_level = setting.power_level
        return highest_level

    def required_save_level(self, actor_user_id: int) -> Power_Level:
        return self.pending_change_level(actor_user_id) or Power_Level.user

    def required_reload_level(self, actor_user_id: int) -> Power_Level:
        return self.pending_change_level(actor_user_id) or Power_Level.user

    def value_for(self, setting: Setting[T], actor_user_id: int) -> T | hikari.UndefinedType:
        self._prune_unsupported_drafts()
        drafts = self._resolved_drafts(actor_user_id)
        draft_value = drafts.get(setting.key, hikari.UNDEFINED)
        if isinstance(draft_value, hikari.UndefinedType):
            return setting.value
        return cast(T, draft_value)

    def _resolved_drafts(self, actor_user_id: int) -> dict[str, object]:
        return self.app.resolve_draft_values(self._drafts.get(self._actor_key(actor_user_id), {}))

    def current_input_value(self, setting: Setting[T], actor_user_id: int) -> str:
        value = self.value_for(setting, actor_user_id)
        label = setting.spec.choice_label_for_value(value)
        if label is not None:
            return label
        if isinstance(value, hikari.UndefinedType):
            return ""
        return str(value)

    def display_value(self, setting: Setting[T], actor_user_id: int) -> str:
        return setting.spec.display_value(self.value_for(setting, actor_user_id))

    def label_text(self, setting: Setting[T], actor_user_id: int) -> str:
        value = self.value_for(setting, actor_user_id)
        choice_label = setting.spec.choice_label_for_value(value)
        if choice_label is not None:
            return choice_label
        return setting.spec.display_value(value)

    def update_setting(
        self,
        actor_user_id: int,
        setting: Setting[T],
        value: str,
        *,
        remember_input: bool = False,
    ) -> None:
        self._prune_unsupported_drafts()
        try:
            parsed_value = setting.spec.parse_input(value)
        except Exception as xcp:
            log.exception(f"Casting Setting value Failed: {type(value)} > {setting.type_name}")
            raise ValueError(f"Invalid value for {setting.label}: {xcp}")
        draft_bucket = self._draft_bucket(actor_user_id)
        self.app.apply_draft_update(setting=setting, value=parsed_value, drafts=draft_bucket)
        self._delete_empty_bucket(actor_user_id)
        if remember_input:
            setting._remember_input(setting.spec.serialise_value(parsed_value))
        audit_log(
            "setting.draft_updated",
            actor_user_id=self._actor_key(actor_user_id),
            app_name=self.config.name,
            setting_key=setting.key,
            setting_label=setting.label,
            power_level=setting.power_level.name,
            has_pending_value=self.has_pending_value(actor_user_id, setting),
            pending_change_count=self.pending_change_count(actor_user_id),
        )

    def load(self, actor_user_id: int | None = None) -> None:
        self.app.load()
        self._prune_redundant_drafts()
        if actor_user_id is None:
            self._drafts.clear()
            return
        draft_count = self.pending_change_count(actor_user_id)
        self._drafts.pop(self._actor_key(actor_user_id), None)
        audit_log(
            "setting.drafts_reloaded",
            actor_user_id=self._actor_key(actor_user_id),
            app_name=self.config.name,
            discarded_draft_count=draft_count,
        )

    def save(self, actor_user_id: int) -> None:
        self._prune_unsupported_drafts()
        actor_key = self._actor_key(actor_user_id)
        drafts = self._resolved_drafts(actor_user_id)
        for setting in self.app.options:
            draft_value = drafts.get(setting.key, hikari.UNDEFINED)
            if isinstance(draft_value, hikari.UndefinedType):
                continue
            setting.value = cast(Any, draft_value)
        self.app.save()
        self._drafts.pop(actor_key, None)
        self._prune_redundant_drafts()
        audit_log(
            "setting.drafts_saved",
            actor_user_id=actor_key,
            app_name=self.config.name,
            saved_setting_keys=sorted(drafts),
            saved_setting_count=len(drafts),
        )


# AiviA APasz
