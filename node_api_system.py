"""System-facing DTOs shared by the node API service and its clients.

Keeping these transport models separate from the HTTP service makes their
validation reusable without coupling callers to the large route coordinator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TypeVar, cast

from apps._node_api import optional_int, optional_string, required_bool, required_int, required_string
from maintenance import MAX_RESTART_INTERVAL_MINUTES, MIN_RESTART_INTERVAL_MINUTES
from restart_state import RestartKind
from restart_targets import RestartTarget

SYSTEM_HISTORY_INTERVAL_SECONDS: int = 10
SYSTEM_HISTORY_RETENTION_SECONDS: int = 60 * 60

_Item = TypeVar("_Item")


def _parsed_tuple(
    payload: Mapping[str, object],
    key: str,
    parser: Callable[[object], _Item],
    *,
    default: Sequence[object] = (),
) -> tuple[_Item, ...]:
    raw_items = payload.get(key, default)
    if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, Sequence):
        raise ValueError(f"{key} is invalid.")
    try:
        return tuple(parser(item) for item in raw_items)
    except (TypeError, ValueError) as xcp:
        raise ValueError(f"{key} is invalid.") from xcp


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a non-empty string.")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string.")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Expected an integer.")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Expected an object.")
    return cast(Mapping[str, object], value)


def _optional_restart_timestamp(payload: Mapping[str, object], key: str, *, label: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Node restart schedule {label} is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class NodeSystemDiskSummary:
    mountpoint: str
    label: str
    percent: int
    free_bytes: int
    total_bytes: int

    def __post_init__(self) -> None:
        if not self.mountpoint.strip() or not self.label.strip():
            raise ValueError("System disk mountpoint and label must not be blank.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemDiskSummary":
        return cls(
            mountpoint=required_string(payload, "mountpoint"),
            label=required_string(payload, "label"),
            percent=required_int(payload, "percent"),
            free_bytes=required_int(payload, "free_bytes"),
            total_bytes=required_int(payload, "total_bytes"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mountpoint": self.mountpoint,
            "label": self.label,
            "percent": self.percent,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemSummary:
    cpu_percent: int | None
    ram_percent: int | None
    ram_used_bytes: int | None
    ram_total_bytes: int | None
    storage_percent: int | None
    storage_free_bytes: int | None
    storage_total_bytes: int | None
    cpu_per_core_percent: tuple[int, ...] = ()
    disks: tuple[NodeSystemDiskSummary, ...] = ()
    bot_uptime_seconds: int | None = None
    uptime_seconds: int | None = None
    cpu_points_available: int | None = None
    cpu_points_capacity: int | None = None
    ram_points_available: int | None = None
    ram_points_capacity: int | None = None
    running_names: tuple[str, ...] = ()
    running_app_ids: tuple[str, ...] = ()
    running_app_scopes: tuple[str, ...] = ()
    start_blocked_app_ids: tuple[str, ...] = ()
    captured_at_epoch_seconds: int | None = None
    deployment_version: str | None = None
    deployment_revision: str | None = None
    deployed_at_epoch_seconds: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("deployment_version", self.deployment_version),
            ("deployment_revision", self.deployment_revision),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must not be blank when provided.")
        if self.deployed_at_epoch_seconds is not None and self.deployed_at_epoch_seconds < 0:
            raise ValueError("deployed_at_epoch_seconds must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemSummary":
        return cls(
            cpu_percent=optional_int(payload, "cpu_percent"),
            ram_percent=optional_int(payload, "ram_percent"),
            ram_used_bytes=optional_int(payload, "ram_used_bytes"),
            ram_total_bytes=optional_int(payload, "ram_total_bytes"),
            storage_percent=optional_int(payload, "storage_percent"),
            storage_free_bytes=optional_int(payload, "storage_free_bytes"),
            storage_total_bytes=optional_int(payload, "storage_total_bytes"),
            cpu_per_core_percent=_parsed_tuple(payload, "cpu_per_core_percent", _integer),
            disks=_parsed_tuple(
                payload,
                "disks",
                lambda value: NodeSystemDiskSummary.from_mapping(_mapping(value)),
            ),
            bot_uptime_seconds=optional_int(payload, "bot_uptime_seconds"),
            uptime_seconds=optional_int(payload, "uptime_seconds"),
            cpu_points_available=optional_int(payload, "cpu_points_available"),
            cpu_points_capacity=optional_int(payload, "cpu_points_capacity"),
            ram_points_available=optional_int(payload, "ram_points_available"),
            ram_points_capacity=optional_int(payload, "ram_points_capacity"),
            running_names=_parsed_tuple(payload, "running_names", _non_empty_string),
            running_app_ids=_parsed_tuple(payload, "running_app_ids", _non_empty_string),
            running_app_scopes=_parsed_tuple(payload, "running_app_scopes", _non_empty_string),
            start_blocked_app_ids=_parsed_tuple(payload, "start_blocked_app_ids", _non_empty_string),
            captured_at_epoch_seconds=optional_int(payload, "captured_at_epoch_seconds"),
            deployment_version=optional_string(payload, "deployment_version"),
            deployment_revision=optional_string(payload, "deployment_revision"),
            deployed_at_epoch_seconds=optional_int(payload, "deployed_at_epoch_seconds"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "ram_used_bytes": self.ram_used_bytes,
            "ram_total_bytes": self.ram_total_bytes,
            "storage_percent": self.storage_percent,
            "storage_free_bytes": self.storage_free_bytes,
            "storage_total_bytes": self.storage_total_bytes,
            "cpu_per_core_percent": list(self.cpu_per_core_percent),
            "disks": [disk.to_mapping() for disk in self.disks],
            "bot_uptime_seconds": self.bot_uptime_seconds,
            "uptime_seconds": self.uptime_seconds,
            "cpu_points_available": self.cpu_points_available,
            "cpu_points_capacity": self.cpu_points_capacity,
            "ram_points_available": self.ram_points_available,
            "ram_points_capacity": self.ram_points_capacity,
            "running_names": list(self.running_names),
            "running_app_ids": list(self.running_app_ids),
            "running_app_scopes": list(self.running_app_scopes),
            "start_blocked_app_ids": list(self.start_blocked_app_ids),
            "captured_at_epoch_seconds": self.captured_at_epoch_seconds,
            "deployment_version": self.deployment_version,
            "deployment_revision": self.deployment_revision,
            "deployed_at_epoch_seconds": self.deployed_at_epoch_seconds,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemSample:
    captured_at_epoch_seconds: int
    cpu_percent: int | None
    ram_percent: int | None
    storage_percent: int | None

    @classmethod
    def from_summary(cls, summary: NodeSystemSummary) -> "NodeSystemSample":
        captured_at = summary.captured_at_epoch_seconds
        if captured_at is None:
            raise ValueError("System summary capture time is required for history samples.")
        return cls(
            captured_at_epoch_seconds=captured_at,
            cpu_percent=summary.cpu_percent,
            ram_percent=summary.ram_percent,
            storage_percent=summary.storage_percent,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemSample":
        captured_at = required_int(payload, "captured_at_epoch_seconds")
        if captured_at < 0:
            raise ValueError("System sample capture time must not be negative.")
        return cls(
            captured_at_epoch_seconds=captured_at,
            cpu_percent=optional_int(payload, "cpu_percent"),
            ram_percent=optional_int(payload, "ram_percent"),
            storage_percent=optional_int(payload, "storage_percent"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "captured_at_epoch_seconds": self.captured_at_epoch_seconds,
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "storage_percent": self.storage_percent,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemHistory:
    retention_seconds: int
    sample_interval_seconds: int
    samples: tuple[NodeSystemSample, ...]

    def __post_init__(self) -> None:
        if self.retention_seconds <= 0 or self.sample_interval_seconds <= 0:
            raise ValueError("System history timing values must be positive.")
        timestamps = tuple(sample.captured_at_epoch_seconds for sample in self.samples)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("System history samples must be chronological.")

    @classmethod
    def empty(cls) -> "NodeSystemHistory":
        return cls(SYSTEM_HISTORY_RETENTION_SECONDS, SYSTEM_HISTORY_INTERVAL_SECONDS, ())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemHistory":
        return cls(
            retention_seconds=required_int(payload, "retention_seconds"),
            sample_interval_seconds=required_int(payload, "sample_interval_seconds"),
            samples=_parsed_tuple(
                payload,
                "samples",
                lambda value: NodeSystemSample.from_mapping(_mapping(value)),
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "retention_seconds": self.retention_seconds,
            "sample_interval_seconds": self.sample_interval_seconds,
            "samples": [sample.to_mapping() for sample in self.samples],
        }


@dataclass(frozen=True, slots=True)
class NodeSystemLogEntry:
    relative_path: str
    size_bytes: int
    modified_at_epoch_seconds: int

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if not self.relative_path.strip() or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("System log path is invalid.")
        if self.size_bytes < 0:
            raise ValueError("System log size must not be negative.")
        if self.modified_at_epoch_seconds < 0:
            raise ValueError("System log modification time must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemLogEntry":
        return cls(
            relative_path=required_string(payload, "relative_path"),
            size_bytes=required_int(payload, "size_bytes"),
            modified_at_epoch_seconds=required_int(payload, "modified_at_epoch_seconds"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "modified_at_epoch_seconds": self.modified_at_epoch_seconds,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemLogCatalog:
    node: str
    entries: tuple[NodeSystemLogEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemLogCatalog":
        return cls(
            node=required_string(payload, "node"),
            entries=_parsed_tuple(
                payload,
                "entries",
                lambda value: NodeSystemLogEntry.from_mapping(_mapping(value)),
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"node": self.node, "entries": [entry.to_mapping() for entry in self.entries]}


@dataclass(frozen=True, slots=True)
class NodeSystemLogTail:
    node: str
    entry: NodeSystemLogEntry
    lines: tuple[str, ...]
    truncated: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemLogTail":
        truncated = payload.get("truncated")
        if not isinstance(truncated, bool):
            raise ValueError("System log tail truncation is invalid.")
        raw_entry = payload.get("entry")
        if not isinstance(raw_entry, Mapping):
            raise ValueError("System log tail entry is invalid.")
        return cls(
            node=required_string(payload, "node"),
            entry=NodeSystemLogEntry.from_mapping(cast(Mapping[str, object], raw_entry)),
            lines=_parsed_tuple(payload, "lines", _string),
            truncated=truncated,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "entry": self.entry.to_mapping(),
            "lines": list(self.lines),
            "truncated": self.truncated,
        }


class NodeSystemAction(StrEnum):
    RESTART_PROCESS = "restart_process"
    REBOOT_HOST = "reboot_host"


@dataclass(frozen=True, slots=True)
class NodeSystemCapabilities:
    actions: tuple[NodeSystemAction, ...]
    supports_app_auto_restart: bool = False
    supports_silent_restart: bool = False
    supports_node_capacity: bool = False
    supports_node_font_sources: bool = False
    supports_node_disk_settings: bool = True
    supports_discord_settings: bool = False

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValueError("Node system capabilities require at least one action.")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("Node system capabilities must not contain duplicate actions.")

    def supports(self, action: NodeSystemAction) -> bool:
        return action in self.actions

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemCapabilities":
        raw_auto_restart = payload.get("supports_app_auto_restart", False)
        raw_silent_restart = payload.get("supports_silent_restart", False)
        raw_node_capacity = payload.get("supports_node_capacity", False)
        raw_node_font_sources = payload.get("supports_node_font_sources", False)
        raw_node_disk_settings = payload.get("supports_node_disk_settings", True)
        raw_discord_settings = payload.get("supports_discord_settings", False)
        if not all(
            isinstance(value, bool)
            for value in (
                raw_auto_restart,
                raw_silent_restart,
                raw_node_capacity,
                raw_node_font_sources,
                raw_node_disk_settings,
                raw_discord_settings,
            )
        ):
            raise ValueError("Node system capability is invalid.")
        try:
            actions = _parsed_tuple(
                payload,
                "actions",
                lambda value: NodeSystemAction(_non_empty_string(value)),
            )
        except ValueError as xcp:
            raise ValueError("Node system capability action is invalid.") from xcp
        return cls(
            actions=actions,
            supports_app_auto_restart=cast(bool, raw_auto_restart),
            supports_silent_restart=cast(bool, raw_silent_restart),
            supports_node_capacity=cast(bool, raw_node_capacity),
            supports_node_font_sources=cast(bool, raw_node_font_sources),
            supports_node_disk_settings=cast(bool, raw_node_disk_settings),
            supports_discord_settings=cast(bool, raw_discord_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "actions": [action.value for action in self.actions],
            "supports_app_auto_restart": self.supports_app_auto_restart,
            "supports_silent_restart": self.supports_silent_restart,
            "supports_node_capacity": self.supports_node_capacity,
            "supports_node_font_sources": self.supports_node_font_sources,
            "supports_node_disk_settings": self.supports_node_disk_settings,
            "supports_discord_settings": self.supports_discord_settings,
        }


type NodeSystemActionHandler = Callable[[NodeSystemAction, bool, bool], None]

SYSTEM_ACTION_LABELS: Mapping[NodeSystemAction, str] = {
    NodeSystemAction.RESTART_PROCESS: "process restart",
    NodeSystemAction.REBOOT_HOST: "host reboot",
}


@dataclass(frozen=True, slots=True)
class NodeSystemActionResult:
    node: str
    action: NodeSystemAction
    message: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSystemActionResult":
        try:
            action = NodeSystemAction(required_string(payload, "action"))
        except ValueError as xcp:
            raise ValueError("Node system action result action is invalid.") from xcp
        return cls(
            node=required_string(payload, "node"),
            action=action,
            message=required_string(payload, "message"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"node": self.node, "action": self.action.value, "message": self.message}


@dataclass(frozen=True, slots=True)
class NodeRestartRecord:
    timestamp: int
    kind: RestartKind

    def __post_init__(self) -> None:
        if self.timestamp <= 0:
            raise ValueError("Node restart record timestamp must be positive Unix seconds.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeRestartRecord":
        try:
            kind = RestartKind(required_string(payload, "kind"))
        except ValueError as xcp:
            raise ValueError("Node restart record kind is invalid.") from xcp
        return cls(timestamp=required_int(payload, "timestamp"), kind=kind)

    def to_mapping(self) -> dict[str, object]:
        return {"timestamp": self.timestamp, "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class NodeRestartState:
    node: str
    process: NodeRestartRecord
    voice: NodeRestartRecord | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeRestartState":
        raw_process = payload.get("process")
        raw_voice = payload.get("voice")
        if not isinstance(raw_process, Mapping):
            raise ValueError("Node process restart record is invalid.")
        if raw_voice is not None and not isinstance(raw_voice, Mapping):
            raise ValueError("Node voice restart record is invalid.")
        return cls(
            node=required_string(payload, "node"),
            process=NodeRestartRecord.from_mapping(cast(Mapping[str, object], raw_process)),
            voice=(
                None if raw_voice is None else NodeRestartRecord.from_mapping(cast(Mapping[str, object], raw_voice))
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "process": self.process.to_mapping(),
            "voice": None if self.voice is None else self.voice.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeRestartScheduleEntry:
    target: RestartTarget
    enabled: bool
    interval_minutes: int
    anchor_timestamp: int | None
    last_triggered_timestamp: int | None
    next_restart_timestamp: int | None
    skipped_through_timestamp: int | None

    def __post_init__(self) -> None:
        if not MIN_RESTART_INTERVAL_MINUTES <= self.interval_minutes <= MAX_RESTART_INTERVAL_MINUTES:
            raise ValueError("Node restart schedule interval is invalid.")
        if self.enabled and (self.anchor_timestamp is None or self.next_restart_timestamp is None):
            raise ValueError("Enabled node restart schedules require anchor and next-restart timestamps.")
        if not self.enabled and self.next_restart_timestamp is not None:
            raise ValueError("Disabled node restart schedules cannot have a next-restart timestamp.")
        for field_name, value in self._timestamps():
            if value is not None and value <= 0:
                raise ValueError(f"Node restart schedule {field_name} must be positive Unix seconds.")

    def _timestamps(self) -> tuple[tuple[str, int | None], ...]:
        return (
            ("anchor_timestamp", self.anchor_timestamp),
            ("last_triggered_timestamp", self.last_triggered_timestamp),
            ("next_restart_timestamp", self.next_restart_timestamp),
            ("skipped_through_timestamp", self.skipped_through_timestamp),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeRestartScheduleEntry":
        try:
            target = RestartTarget(required_string(payload, "target"))
        except ValueError as xcp:
            raise ValueError("Node restart schedule target is invalid.") from xcp
        return cls(
            target=target,
            enabled=required_bool(payload, "enabled"),
            interval_minutes=required_int(payload, "interval_minutes"),
            anchor_timestamp=_optional_restart_timestamp(
                payload,
                "anchor_timestamp",
                label="anchor timestamp",
            ),
            last_triggered_timestamp=_optional_restart_timestamp(
                payload,
                "last_triggered_timestamp",
                label="last triggered timestamp",
            ),
            next_restart_timestamp=_optional_restart_timestamp(
                payload,
                "next_restart_timestamp",
                label="next restart timestamp",
            ),
            skipped_through_timestamp=_optional_restart_timestamp(
                payload,
                "skipped_through_timestamp",
                label="skipped-through timestamp",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "anchor_timestamp": self.anchor_timestamp,
            "last_triggered_timestamp": self.last_triggered_timestamp,
            "next_restart_timestamp": self.next_restart_timestamp,
            "skipped_through_timestamp": self.skipped_through_timestamp,
        }


@dataclass(frozen=True, slots=True)
class NodeRestartScheduleState:
    node: str
    schedules: tuple[NodeRestartScheduleEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeRestartScheduleState":
        return cls(
            node=required_string(payload, "node"),
            schedules=_parsed_tuple(
                payload,
                "schedules",
                lambda value: NodeRestartScheduleEntry.from_mapping(_mapping(value)),
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"node": self.node, "schedules": [schedule.to_mapping() for schedule in self.schedules]}
