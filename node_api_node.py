"""Typed contracts for node-level configuration and management operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import config
import apps._node_api as app_node_api


@dataclass(frozen=True, slots=True)
class NodeCapacityMutationResult:
    node: str
    message: str
    capacity: config.NodeCapacityProfile

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> "NodeCapacityMutationResult":
        raw_capacity = payload.get("capacity")
        if not isinstance(raw_capacity, Mapping):
            raise ValueError("Node capacity mutation capacity is invalid.")
        return cls(
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
            capacity=config.NodeCapacityProfile.model_validate(raw_capacity),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "capacity": self.capacity.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeDiskEntry:
    mountpoint: str
    display_name: str
    is_activity: bool
    is_primary: bool
    is_secondary: bool
    is_bot_disk: bool

    def __post_init__(self) -> None:
        if not self.mountpoint.strip() or not self.display_name.strip():
            raise ValueError("Node disk mountpoint and display name must not be blank.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeDiskEntry":
        return cls(
            mountpoint=app_node_api.required_string(payload, "mountpoint"),
            display_name=app_node_api.required_string(payload, "display_name"),
            is_activity=app_node_api.required_bool(payload, "is_activity"),
            is_primary=app_node_api.required_bool(payload, "is_primary"),
            is_secondary=app_node_api.required_bool(payload, "is_secondary"),
            is_bot_disk=app_node_api.required_bool(payload, "is_bot_disk"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mountpoint": self.mountpoint,
            "display_name": self.display_name,
            "is_activity": self.is_activity,
            "is_primary": self.is_primary,
            "is_secondary": self.is_secondary,
            "is_bot_disk": self.is_bot_disk,
        }


@dataclass(frozen=True, slots=True)
class NodeDiskManagementState:
    node: str
    disks: tuple[NodeDiskEntry, ...]
    preferences: config.PersistedDiskPreferences

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeDiskManagementState":
        raw_disks = payload.get("disks", ())
        if not isinstance(raw_disks, Sequence) or isinstance(raw_disks, (str, bytes)):
            raise ValueError("Node disk management disks are invalid.")
        disks: list[NodeDiskEntry] = []
        for raw_disk in raw_disks:
            if not isinstance(raw_disk, Mapping):
                raise ValueError("Node disk management disks are invalid.")
            disks.append(NodeDiskEntry.from_mapping(raw_disk))
        raw_preferences = payload.get("preferences")
        if not isinstance(raw_preferences, Mapping):
            raise ValueError("Node disk management preferences are invalid.")
        return cls(
            node=app_node_api.required_string(payload, "node"),
            disks=tuple(disks),
            preferences=config.PersistedDiskPreferences.model_validate(raw_preferences),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "disks": [disk.to_mapping() for disk in self.disks],
            "preferences": self.preferences.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeDiskSettingsMutationResult:
    node: str
    message: str
    settings: NodeDiskManagementState

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> "NodeDiskSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("Node disk settings mutation settings are invalid.")
        return cls(
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
            settings=NodeDiskManagementState.from_mapping(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeFontSourceSettingsMutationResult:
    node: str
    message: str
    settings: config.NodeFontSourceSettings

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> "NodeFontSourceSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("Node font source settings mutation settings are invalid.")
        return cls(
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
            settings=config.NodeFontSourceSettings.model_validate(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeDiscordSettingsMutationResult:
    node: str
    message: str
    settings: config.DiscordSettings

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> "NodeDiscordSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("Node Discord settings mutation settings are invalid.")
        return cls(
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
            settings=config.DiscordSettings.model_validate(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.model_dump(mode="json"),
        }
