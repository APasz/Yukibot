from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel
from pydantic.config import ConfigDict

from _security import Power_Level
from apps._node_api import (
    power_level as _power_level,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
)

_DEFAULT_REMOTE_CONFIG_READ_LEVEL = Power_Level.sudo
_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL = Power_Level.root


class NodeSaveUploadTransport(StrEnum):
    """The transport used to submit an app save upload."""

    DIRECT = "direct"
    RELAY = "relay"


@dataclass(frozen=True, slots=True)
class NodeConfigEntry:
    id: str
    label: str
    relative_path: str
    root_id: str
    root_label: str
    kind: str
    read_power_level: Power_Level
    size_bytes: int
    size_text: str
    modified_at: str
    write_power_level: Power_Level = _DEFAULT_REMOTE_CONFIG_WRITE_LEVEL
    can_write: bool = True
    can_delete: bool = False
    write_notice: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigEntry:
        raw_can_write = payload.get("can_write", True)
        raw_can_delete = payload.get("can_delete", False)
        if not isinstance(raw_can_write, bool):
            raise ValueError("Node config entry can_write is invalid.")
        if not isinstance(raw_can_delete, bool):
            raise ValueError("Node config entry can_delete is invalid.")
        write_notice = payload.get("write_notice")
        if write_notice is not None and not isinstance(write_notice, str):
            raise ValueError("Node config entry write_notice is invalid.")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            root_id=_required_string(payload, "root_id"),
            root_label=_required_string(payload, "root_label"),
            kind=_required_string(payload, "kind"),
            read_power_level=_power_level(payload, "read_power_level", default=_DEFAULT_REMOTE_CONFIG_READ_LEVEL),
            write_power_level=_power_level(payload, "write_power_level", default=_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            can_write=raw_can_write,
            can_delete=raw_can_delete,
            write_notice=write_notice,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "root_id": self.root_id,
            "root_label": self.root_label,
            "kind": self.kind,
            "read_power_level": self.read_power_level.name,
            "write_power_level": self.write_power_level.name,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "can_write": self.can_write,
            "can_delete": self.can_delete,
            "write_notice": self.write_notice,
        }


@dataclass(frozen=True, slots=True)
class NodeConfigRootEntry:
    id: str
    label: str
    kind: str
    read_power_level: Power_Level
    write_power_level: Power_Level
    can_create: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigRootEntry:
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            kind=_required_string(payload, "kind"),
            read_power_level=_power_level(payload, "read_power_level", default=_DEFAULT_REMOTE_CONFIG_READ_LEVEL),
            write_power_level=_power_level(payload, "write_power_level", default=_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL),
            can_create=_required_bool(payload, "can_create"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "read_power_level": self.read_power_level.name,
            "write_power_level": self.write_power_level.name,
            "can_create": self.can_create,
        }


@dataclass(frozen=True, slots=True)
class NodeConfigList:
    app_name: str
    app_friendly: str
    node: str
    configs: tuple[NodeConfigEntry, ...]
    roots: tuple[NodeConfigRootEntry, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigList:
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        raw_configs = payload.get("configs")
        if not isinstance(raw_configs, Sequence) or isinstance(raw_configs, (str, bytes)):
            raise ValueError("Node config list configs are invalid.")
        configs: list[NodeConfigEntry] = []
        for raw_config in raw_configs:
            if not isinstance(raw_config, Mapping):
                raise ValueError("Node config list contains an invalid config entry.")
            configs.append(NodeConfigEntry.from_mapping(raw_config))
        raw_roots = payload.get("roots", ())
        if not isinstance(raw_roots, Sequence) or isinstance(raw_roots, (str, bytes)):
            raise ValueError("Node config list roots are invalid.")
        roots: list[NodeConfigRootEntry] = []
        for raw_root in raw_roots:
            if not isinstance(raw_root, Mapping):
                raise ValueError("Node config list contains an invalid root entry.")
            roots.append(NodeConfigRootEntry.from_mapping(raw_root))
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            configs=tuple(configs),
            roots=tuple(roots),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "configs": [entry.to_mapping() for entry in self.configs],
            "roots": [entry.to_mapping() for entry in self.roots],
        }


@dataclass(frozen=True, slots=True)
class NodeConfigContent:
    app_name: str
    app_friendly: str
    node: str
    config: NodeConfigEntry
    content: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigContent:
        raw_config = payload.get("config")
        if not isinstance(raw_config, Mapping):
            raise ValueError("Node config content metadata is invalid.")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("Node config content is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            config=NodeConfigEntry.from_mapping(raw_config),
            content=content,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "config": self.config.to_mapping(),
            "content": self.content,
        }


class NodeConfigWriteRequest(BaseModel):
    content: str

    model_config = ConfigDict(str_strip_whitespace=False)


class NodeConfigCreateRequest(BaseModel):
    root_id: str
    relative_path: str
    content: str = ""

    model_config = ConfigDict(str_strip_whitespace=False)


@dataclass(frozen=True, slots=True)
class NodeConfigMutationResult:
    app_name: str
    app_friendly: str
    node: str
    config_id: str
    message: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigMutationResult:
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            config_id=_required_string(payload, "config_id"),
            message=_required_string(payload, "message"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "config_id": self.config_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class NodeSaveRootEntry:
    id: str
    label: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSaveRootEntry":
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class NodeSaveEntry:
    id: str
    label: str
    relative_path: str
    root_id: str
    root_label: str
    kind: str
    size_bytes: int
    size_text: str
    modified_at: str
    can_delete: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSaveEntry:
        raw_can_delete = payload.get("can_delete", False)
        if not isinstance(raw_can_delete, bool):
            raise ValueError("Node save entry can_delete is invalid.")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            root_id=_required_string(payload, "root_id"),
            root_label=_required_string(payload, "root_label"),
            kind=_required_string(payload, "kind"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            can_delete=raw_can_delete,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "root_id": self.root_id,
            "root_label": self.root_label,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "can_delete": self.can_delete,
        }


@dataclass(frozen=True, slots=True)
class NodeSaveList:
    app_name: str
    app_friendly: str
    node: str
    roots: tuple[NodeSaveRootEntry, ...]
    saves: tuple[NodeSaveEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSaveList:
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        raw_roots = payload.get("roots", ())
        if not isinstance(raw_roots, Sequence) or isinstance(raw_roots, (str, bytes)):
            raise ValueError("Node save list roots are invalid.")
        roots: list[NodeSaveRootEntry] = []
        for raw_root in raw_roots:
            if not isinstance(raw_root, Mapping):
                raise ValueError("Node save list contains an invalid root entry.")
            roots.append(NodeSaveRootEntry.from_mapping(raw_root))
        raw_saves = payload.get("saves")
        if not isinstance(raw_saves, Sequence) or isinstance(raw_saves, (str, bytes)):
            raise ValueError("Node save list saves are invalid.")
        saves: list[NodeSaveEntry] = []
        for raw_save in raw_saves:
            if not isinstance(raw_save, Mapping):
                raise ValueError("Node save list contains an invalid save entry.")
            saves.append(NodeSaveEntry.from_mapping(raw_save))
        return cls(app_name=app_name, app_friendly=app_friendly, node=node, roots=tuple(roots), saves=tuple(saves))

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "roots": [root.to_mapping() for root in self.roots],
            "saves": [entry.to_mapping() for entry in self.saves],
        }


@dataclass(frozen=True, slots=True)
class NodeSaveMutationResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    save: NodeSaveEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSaveMutationResult":
        raw_save = payload.get("save")
        if not isinstance(raw_save, Mapping):
            raise ValueError("Node save mutation result save is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            save=NodeSaveEntry.from_mapping(raw_save),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "save": self.save.to_mapping(),
        }


class NodeSaveRenameRequest(BaseModel):
    new_name: str

    model_config = ConfigDict(str_strip_whitespace=True)
