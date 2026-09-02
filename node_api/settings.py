from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic.config import ConfigDict

from _security import Access_Control, Power_Level
from apps._node_api import (
    optional_string as _optional_string,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
    required_text as _required_text,
)


class NodeSettingWriteRequest(BaseModel):
    value: str

    model_config = ConfigDict(str_strip_whitespace=False)


@dataclass(frozen=True, slots=True)
class NodeSettingChoice:
    label: str
    raw_value: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingChoice:
        return cls(
            label=_required_string(payload, "label"),
            raw_value=_required_string(payload, "raw_value"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "label": self.label,
            "raw_value": self.raw_value,
        }


@dataclass(frozen=True, slots=True)
class NodeSettingEntry:
    key: str
    label: str
    type_name: str
    permission_level: str
    permission_level_name: str
    default_text: str
    description: str | None
    paragraph: bool
    is_sensitive: bool
    value_text: str
    revealed_value_text: str
    current_input_value: str
    has_pending_value: bool
    can_edit: bool
    value_is_hidden: bool
    can_reveal_hidden_text: bool
    allows_text_input: bool
    allows_blank_input: bool
    strict_choice: bool
    choices: tuple[NodeSettingChoice, ...]
    recent_inputs: tuple[str, ...]
    group_id: str | None = None
    group_label: str | None = None

    def __post_init__(self) -> None:
        if (self.group_id is None) != (self.group_label is None):
            raise ValueError("Node setting group id and label must either both be set or both be absent.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingEntry:
        raw_choices = payload.get("choices")
        if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
            raise ValueError("Node setting entry choices are invalid.")
        choices: list[NodeSettingChoice] = []
        for raw_choice in raw_choices:
            if not isinstance(raw_choice, Mapping):
                raise ValueError("Node setting entry contained an invalid choice.")
            choices.append(NodeSettingChoice.from_mapping(raw_choice))

        raw_recent_inputs = payload.get("recent_inputs")
        if not isinstance(raw_recent_inputs, Sequence) or isinstance(raw_recent_inputs, (str, bytes)):
            raise ValueError("Node setting entry recent inputs are invalid.")
        recent_inputs: list[str] = []
        for raw_recent_input in raw_recent_inputs:
            if not isinstance(raw_recent_input, str):
                raise ValueError("Node setting entry contained an invalid recent input.")
            recent_inputs.append(raw_recent_input)

        permission_level = _required_string(payload, "permission_level")
        permission_level_name = payload.get("permission_level_name")
        if not isinstance(permission_level_name, str) or not permission_level_name:
            parsed_permission_level = Access_Control.parse_level(permission_level)
            permission_level_name = (
                parsed_permission_level.name if parsed_permission_level is not None else permission_level
            )
        has_pending_value = payload.get("has_pending_value", False)
        if not isinstance(has_pending_value, bool):
            raise ValueError("Node setting entry has_pending_value is invalid.")
        group_id = _optional_string(payload, "group_id")
        group_label = _optional_string(payload, "group_label")

        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            type_name=_required_string(payload, "type_name"),
            permission_level=permission_level,
            permission_level_name=permission_level_name,
            default_text=_required_text(payload, "default_text"),
            description=_optional_string(payload, "description"),
            paragraph=_required_bool(payload, "paragraph"),
            is_sensitive=_required_bool(payload, "is_sensitive"),
            value_text=_required_text(payload, "value_text"),
            revealed_value_text=_required_text(payload, "revealed_value_text"),
            current_input_value=_required_text(payload, "current_input_value"),
            has_pending_value=has_pending_value,
            can_edit=_required_bool(payload, "can_edit"),
            value_is_hidden=_required_bool(payload, "value_is_hidden"),
            can_reveal_hidden_text=_required_bool(payload, "can_reveal_hidden_text"),
            allows_text_input=_required_bool(payload, "allows_text_input"),
            allows_blank_input=_required_bool(payload, "allows_blank_input"),
            strict_choice=_required_bool(payload, "strict_choice"),
            choices=tuple(choices),
            recent_inputs=tuple(recent_inputs),
            group_id=group_id,
            group_label=group_label,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "type_name": self.type_name,
            "permission_level": self.permission_level,
            "permission_level_name": self.permission_level_name,
            "default_text": self.default_text,
            "description": self.description,
            "paragraph": self.paragraph,
            "is_sensitive": self.is_sensitive,
            "value_text": self.value_text,
            "revealed_value_text": self.revealed_value_text,
            "current_input_value": self.current_input_value,
            "has_pending_value": self.has_pending_value,
            "can_edit": self.can_edit,
            "value_is_hidden": self.value_is_hidden,
            "can_reveal_hidden_text": self.can_reveal_hidden_text,
            "allows_text_input": self.allows_text_input,
            "allows_blank_input": self.allows_blank_input,
            "strict_choice": self.strict_choice,
            "choices": [choice.to_mapping() for choice in self.choices],
            "recent_inputs": list(self.recent_inputs),
            "group_id": self.group_id,
            "group_label": self.group_label,
        }


@dataclass(frozen=True, slots=True)
class NodeSettingList:
    app_name: str
    app_friendly: str
    node: str
    editable_count: int
    restricted_count: int
    has_pending_changes: bool
    pending_change_count: int
    required_save_level_name: str
    required_reload_level_name: str
    settings: tuple[NodeSettingEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingList:
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Sequence) or isinstance(raw_settings, (str, bytes)):
            raise ValueError("Node setting list settings are invalid.")
        settings: list[NodeSettingEntry] = []
        for raw_setting in raw_settings:
            if not isinstance(raw_setting, Mapping):
                raise ValueError("Node setting list contained an invalid setting entry.")
            settings.append(NodeSettingEntry.from_mapping(raw_setting))
        has_pending_changes = payload.get("has_pending_changes", False)
        if not isinstance(has_pending_changes, bool):
            raise ValueError("Node setting list has_pending_changes is invalid.")
        pending_change_count = payload.get("pending_change_count", 0)
        if isinstance(pending_change_count, bool) or not isinstance(pending_change_count, int):
            raise ValueError("Node setting list pending_change_count is invalid.")
        required_save_level_name = payload.get("required_save_level_name", Power_Level.user.name)
        if not isinstance(required_save_level_name, str) or not required_save_level_name:
            raise ValueError("Node setting list required_save_level_name is invalid.")
        required_reload_level_name = payload.get("required_reload_level_name", Power_Level.user.name)
        if not isinstance(required_reload_level_name, str) or not required_reload_level_name:
            raise ValueError("Node setting list required_reload_level_name is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            editable_count=_required_int(payload, "editable_count"),
            restricted_count=_required_int(payload, "restricted_count"),
            has_pending_changes=has_pending_changes,
            pending_change_count=pending_change_count,
            required_save_level_name=required_save_level_name,
            required_reload_level_name=required_reload_level_name,
            settings=tuple(settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "editable_count": self.editable_count,
            "restricted_count": self.restricted_count,
            "has_pending_changes": self.has_pending_changes,
            "pending_change_count": self.pending_change_count,
            "required_save_level_name": self.required_save_level_name,
            "required_reload_level_name": self.required_reload_level_name,
            "settings": [setting.to_mapping() for setting in self.settings],
        }


@dataclass(frozen=True, slots=True)
class NodeSettingMutationResult:
    app_name: str
    app_friendly: str
    node: str
    setting_key: str
    message: str
    setting: NodeSettingEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingMutationResult:
        raw_setting = payload.get("setting")
        if not isinstance(raw_setting, Mapping):
            raise ValueError("Node setting mutation result setting is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            setting_key=_required_string(payload, "setting_key"),
            message=_required_string(payload, "message"),
            setting=NodeSettingEntry.from_mapping(raw_setting),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "setting_key": self.setting_key,
            "message": self.message,
            "setting": self.setting.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeSettingsActionResult:
    app_name: str
    app_friendly: str
    node: str
    message: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingsActionResult:
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
        }
