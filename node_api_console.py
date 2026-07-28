from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from pydantic import BaseModel

from apps._console import ConsoleResponseSource
from apps._node_api import (
    optional_string as _optional_string,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
)
from node_api_settings import NodeSettingChoice

@dataclass(frozen=True, slots=True)
class NodeConsoleActionParameter:
    key: str
    label: str
    value_type_name: str
    description: str | None
    max_length: int
    multiline: bool
    strict_choice: bool
    allows_text_input: bool
    choices: tuple[NodeSettingChoice, ...]
    recent_inputs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionParameter":
        raw_choices = payload.get("choices")
        raw_recent_inputs = payload.get("recent_inputs")
        if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
            raise ValueError("Node console action parameter choices are invalid.")
        if not isinstance(raw_recent_inputs, Sequence) or isinstance(raw_recent_inputs, (str, bytes)):
            raise ValueError("Node console action parameter recent_inputs are invalid.")
        choices: list[NodeSettingChoice] = []
        for raw_choice in raw_choices:
            if not isinstance(raw_choice, Mapping):
                raise ValueError("Node console action parameter contains an invalid choice.")
            choices.append(NodeSettingChoice.from_mapping(raw_choice))
        recent_inputs: list[str] = []
        for raw_recent_input in raw_recent_inputs:
            if not isinstance(raw_recent_input, str):
                raise ValueError("Node console action parameter contains an invalid recent input.")
            recent_inputs.append(raw_recent_input)
        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            value_type_name=_required_string(payload, "value_type_name"),
            description=_optional_string(payload, "description"),
            max_length=_required_int(payload, "max_length"),
            multiline=_required_bool(payload, "multiline"),
            strict_choice=_required_bool(payload, "strict_choice"),
            allows_text_input=_required_bool(payload, "allows_text_input"),
            choices=tuple(choices),
            recent_inputs=tuple(recent_inputs),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "value_type_name": self.value_type_name,
            "description": self.description,
            "max_length": self.max_length,
            "multiline": self.multiline,
            "strict_choice": self.strict_choice,
            "allows_text_input": self.allows_text_input,
            "choices": [choice.to_mapping() for choice in self.choices],
            "recent_inputs": list(self.recent_inputs),
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleActionEntry:
    key: str
    label: str
    description: str
    power_level_name: str
    power_level_label: str
    requires_running: bool
    can_run: bool
    parameter: NodeConsoleActionParameter | None
    runtime_running: bool | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionEntry":
        raw_parameter = payload.get("parameter")
        if raw_parameter is not None and not isinstance(raw_parameter, Mapping):
            raise ValueError("Node console action entry parameter is invalid.")
        raw_runtime_running = payload.get("runtime_running")
        if raw_runtime_running is not None and not isinstance(raw_runtime_running, bool):
            raise ValueError("Node console action entry runtime_running is invalid.")
        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            description=_required_string(payload, "description"),
            power_level_name=_required_string(payload, "power_level_name"),
            power_level_label=_required_string(payload, "power_level_label"),
            requires_running=_required_bool(payload, "requires_running"),
            can_run=_required_bool(payload, "can_run"),
            parameter=(
                NodeConsoleActionParameter.from_mapping(raw_parameter)
                if raw_parameter is not None
                else None
            ),
            runtime_running=raw_runtime_running,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "power_level_name": self.power_level_name,
            "power_level_label": self.power_level_label,
            "requires_running": self.requires_running,
            "can_run": self.can_run,
            "parameter": self.parameter.to_mapping() if self.parameter is not None else None,
            "runtime_running": self.runtime_running,
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleActionList:
    app_name: str
    app_friendly: str
    node: str
    actions: tuple[NodeConsoleActionEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionList":
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, Sequence) or isinstance(raw_actions, (str, bytes)):
            raise ValueError("Node console action list actions are invalid.")
        actions: list[NodeConsoleActionEntry] = []
        for raw_action in raw_actions:
            if not isinstance(raw_action, Mapping):
                raise ValueError("Node console action list contains an invalid action.")
            actions.append(NodeConsoleActionEntry.from_mapping(raw_action))
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            actions=tuple(actions),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "actions": [action.to_mapping() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleStdoutSnapshot:
    app_name: str
    app_friendly: str
    node: str
    lines: tuple[str, ...]
    truncated: bool
    running: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleStdoutSnapshot":
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)):
            raise ValueError("Node console stdout snapshot lines are invalid.")
        lines: list[str] = []
        for raw_line in raw_lines:
            if not isinstance(raw_line, str):
                raise ValueError("Node console stdout snapshot contains an invalid line.")
            lines.append(raw_line)
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            lines=tuple(lines),
            truncated=_required_bool(payload, "truncated"),
            running=_required_bool(payload, "running"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "running": self.running,
        }


class NodeConsoleStdoutStreamEventKind(StrEnum):
    INITIAL = "initial"
    APPEND = "append"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class NodeConsoleStdoutStreamEvent:
    kind: NodeConsoleStdoutStreamEventKind
    app_name: str
    snapshot: NodeConsoleStdoutSnapshot | None = None
    appended_lines: tuple[str, ...] = ()
    truncated: bool = False
    running: bool = False

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("Console stdout stream app name must not be empty.")
        if self.kind in {NodeConsoleStdoutStreamEventKind.INITIAL, NodeConsoleStdoutStreamEventKind.RESET}:
            if self.snapshot is None or self.snapshot.app_name.casefold() != self.app_name.casefold():
                raise ValueError("Console stdout stream snapshots are invalid.")
            if self.appended_lines:
                raise ValueError("Console stdout snapshot events cannot append lines.")
        elif self.snapshot is not None:
            raise ValueError("Console stdout append events cannot contain snapshots.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleStdoutStreamEvent":
        try:
            kind = NodeConsoleStdoutStreamEventKind(_required_string(payload, "kind"))
        except ValueError as xcp:
            raise ValueError("Console stdout stream event kind is invalid.") from xcp
        raw_snapshot = payload.get("snapshot")
        if raw_snapshot is not None and not isinstance(raw_snapshot, Mapping):
            raise ValueError("Console stdout stream snapshot is invalid.")
        raw_appended_lines = payload.get("appended_lines", ())
        if not isinstance(raw_appended_lines, Sequence) or isinstance(raw_appended_lines, (str, bytes)):
            raise ValueError("Console stdout appended lines are invalid.")
        appended_lines = tuple(raw_appended_lines)
        if any(not isinstance(line, str) for line in appended_lines):
            raise ValueError("Console stdout appended lines are invalid.")
        return cls(
            kind=kind,
            app_name=_required_string(payload, "app_name"),
            snapshot=NodeConsoleStdoutSnapshot.from_mapping(raw_snapshot) if raw_snapshot is not None else None,
            appended_lines=cast(tuple[str, ...], appended_lines),
            truncated=_required_bool(payload, "truncated"),
            running=_required_bool(payload, "running"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "app_name": self.app_name,
            "snapshot": self.snapshot.to_mapping() if self.snapshot is not None else None,
            "appended_lines": list(self.appended_lines),
            "truncated": self.truncated,
            "running": self.running,
        }

    def apply(
        self,
        previous: NodeConsoleStdoutSnapshot | None,
        *,
        max_lines: int,
    ) -> NodeConsoleStdoutSnapshot:
        if max_lines <= 0:
            raise ValueError("Console stdout stream max lines must be positive.")
        if self.snapshot is not None:
            return self.snapshot
        if previous is None:
            raise ValueError("Console stdout append event requires an initial snapshot.")
        lines = (*previous.lines, *self.appended_lines)
        return NodeConsoleStdoutSnapshot(
            app_name=previous.app_name,
            app_friendly=previous.app_friendly,
            node=previous.node,
            lines=tuple(lines[-max_lines:]),
            truncated=self.truncated,
            running=self.running,
        )


class NodeConsoleActionExecuteRequest(BaseModel):
    value: str | None = None


@dataclass(frozen=True, slots=True)
class NodeConsoleActionExecutionResult:
    app_name: str
    app_friendly: str
    node: str
    action_key: str
    summary: str
    success: bool
    text: str | None
    source: ConsoleResponseSource

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionExecutionResult":
        raw_source = _required_string(payload, "source")
        try:
            source = ConsoleResponseSource(raw_source)
        except ValueError as xcp:
            raise ValueError("Node console action execution result source is invalid.") from xcp
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            action_key=_required_string(payload, "action_key"),
            summary=_required_string(payload, "summary"),
            success=_required_bool(payload, "success"),
            text=_optional_string(payload, "text"),
            source=source,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "action_key": self.action_key,
            "summary": self.summary,
            "success": self.success,
            "text": self.text,
            "source": self.source.value,
        }
