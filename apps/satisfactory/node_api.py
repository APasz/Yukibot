from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from apps._node_api import (
    optional_string as _optional_string,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
)

@dataclass(frozen=True, slots=True)
class NodeBlueprintFileEntry:
    id: str
    label: str
    relative_path: str
    size_bytes: int
    size_text: str
    modified_at: str
    uploaded_by_display_name: str | None
    can_delete: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintFileEntry":
        uploaded_by_display_name = _optional_string(payload, "uploaded_by_display_name")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=_required_bool(payload, "can_delete"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "uploaded_by_display_name": self.uploaded_by_display_name,
            "can_delete": self.can_delete,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintEntry:
    id: str
    label: str
    session_name: str
    relative_path: str
    size_bytes: int
    size_text: str
    modified_at: str
    uploaded_by_display_name: str | None
    can_delete: bool
    config_file: NodeBlueprintFileEntry | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintEntry":
        uploaded_by_display_name = _optional_string(payload, "uploaded_by_display_name")
        raw_config_file = payload.get("config_file")
        if raw_config_file is not None and not isinstance(raw_config_file, Mapping):
            raise ValueError("Node blueprint entry config_file is invalid.")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            session_name=_required_string(payload, "session_name"),
            relative_path=_required_string(payload, "relative_path"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=_required_bool(payload, "can_delete"),
            config_file=NodeBlueprintFileEntry.from_mapping(raw_config_file) if raw_config_file is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "session_name": self.session_name,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "uploaded_by_display_name": self.uploaded_by_display_name,
            "can_delete": self.can_delete,
            "config_file": self.config_file.to_mapping() if self.config_file is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintList:
    app_name: str
    app_friendly: str
    node: str
    blueprints: tuple[NodeBlueprintEntry, ...]
    default_session_name: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintList":
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        default_session_name = _optional_string(payload, "default_session_name")
        raw_blueprints = payload.get("blueprints")
        if not isinstance(raw_blueprints, Sequence) or isinstance(raw_blueprints, (str, bytes)):
            raise ValueError("Node blueprint list blueprints are invalid.")
        blueprints: list[NodeBlueprintEntry] = []
        for raw_blueprint in raw_blueprints:
            if not isinstance(raw_blueprint, Mapping):
                raise ValueError("Node blueprint list contains an invalid blueprint entry.")
            blueprints.append(NodeBlueprintEntry.from_mapping(raw_blueprint))
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            blueprints=tuple(blueprints),
            default_session_name=default_session_name,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "blueprints": [entry.to_mapping() for entry in self.blueprints],
            "default_session_name": self.default_session_name,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintMutationResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    blueprint: NodeBlueprintEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintMutationResult":
        raw_blueprint = payload.get("blueprint")
        if not isinstance(raw_blueprint, Mapping):
            raise ValueError("Node blueprint mutation result blueprint is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            blueprint=NodeBlueprintEntry.from_mapping(raw_blueprint),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "blueprint": self.blueprint.to_mapping(),
        }
