"""Chat transport contracts for node API consumers."""

from __future__ import annotations

from importlib import import_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from apps._node_api import (
    optional_int as _optional_int,
    required_int as _required_int,
    required_string as _required_string,
)
from chat_hub import ChatEvent
from pydantic import BaseModel, field_validator, model_validator
from pydantic.config import ConfigDict

class _RuntimeSummary(Protocol):
    def to_mapping(self) -> dict[str, object]: ...


def _runtime_summary_from_mapping(payload: Mapping[str, object]) -> _RuntimeSummary:
    """Resolve the runtime summary after the node API module has initialized."""
    runtime_summary_type = getattr(import_module("node_api"), "NodeAppRuntimeSummary")
    return cast(_RuntimeSummary, runtime_summary_type.from_mapping(payload))


@dataclass(frozen=True, slots=True)
class NodeChatEndpointSummary:
    label: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeChatEndpointSummary":
        return cls(label=_required_string(payload, "label"))

    def to_mapping(self) -> dict[str, object]:
        return {"label": self.label}


@dataclass(frozen=True, slots=True)
class NodeChatRoomSnapshot:
    room_id: str
    endpoint_count: int
    events: tuple[ChatEvent, ...]
    endpoint_summaries: tuple[NodeChatEndpointSummary, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("Chat room snapshot revision must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeChatRoomSnapshot":
        raw_events = payload.get("events")
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise ValueError("events are invalid.")
        events: list[ChatEvent] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                raise ValueError("events are invalid.")
            events.append(ChatEvent.from_mapping(raw_event))
        raw_endpoint_summaries = payload.get("endpoint_summaries", ())
        if not isinstance(raw_endpoint_summaries, Sequence) or isinstance(raw_endpoint_summaries, (str, bytes)):
            raise ValueError("endpoint_summaries are invalid.")
        endpoint_summaries: list[NodeChatEndpointSummary] = []
        for raw_summary in raw_endpoint_summaries:
            if not isinstance(raw_summary, Mapping):
                raise ValueError("endpoint_summaries are invalid.")
            endpoint_summaries.append(NodeChatEndpointSummary.from_mapping(raw_summary))
        return cls(
            room_id=_required_string(payload, "room_id"),
            endpoint_count=_required_int(payload, "endpoint_count"),
            endpoint_summaries=tuple(endpoint_summaries),
            events=tuple(events),
            revision=_optional_int(payload, "revision") or 0,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "endpoint_count": self.endpoint_count,
            "endpoint_summaries": [summary.to_mapping() for summary in self.endpoint_summaries],
            "events": [event.to_mapping() for event in self.events],
            "revision": self.revision,
        }


class NodeChatInjectionRequest(BaseModel):
    """A root-only synthetic chat event requested by the web dashboard."""

    event: dict[str, object]

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_event(self) -> "NodeChatInjectionRequest":
        try:
            ChatEvent.from_mapping(self.event)
        except ValueError as xcp:
            raise ValueError("event is invalid.") from xcp
        return self

    def to_chat_event(self) -> ChatEvent:
        return ChatEvent.from_mapping(self.event)


class NodeChatStreamEventKind(StrEnum):
    INITIAL = "initial"
    CHAT_CHANGED = "chat_changed"
    RUNTIME_CHANGED = "runtime_changed"


@dataclass(frozen=True, slots=True)
class NodeChatStreamEvent:
    kind: NodeChatStreamEventKind
    room_id: str
    snapshot: NodeChatRoomSnapshot | None = None
    app_stats: _RuntimeSummary | None = None
    events: tuple[ChatEvent, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.room_id.strip():
            raise ValueError("Node chat stream event room id is invalid.")
        if self.snapshot is not None and self.snapshot.room_id.casefold() != self.room_id.casefold():
            raise ValueError("Node chat stream event snapshot room id is invalid.")
        if any(event.room_id.casefold() != self.room_id.casefold() for event in self.events):
            raise ValueError("Node chat stream event delta room id is invalid.")
        if self.revision < 0:
            raise ValueError("Node chat stream event revision must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeChatStreamEvent":
        raw_kind = _required_string(payload, "kind")
        try:
            kind = NodeChatStreamEventKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("Node chat stream event kind is invalid.") from xcp
        raw_snapshot = payload.get("snapshot")
        raw_app_stats = payload.get("app_stats")
        raw_events = payload.get("events", ())
        if raw_snapshot is not None and not isinstance(raw_snapshot, Mapping):
            raise ValueError("Node chat stream event snapshot is invalid.")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node chat stream event app_stats are invalid.")
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise ValueError("Node chat stream event deltas are invalid.")
        events: list[ChatEvent] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                raise ValueError("Node chat stream event delta is invalid.")
            events.append(ChatEvent.from_mapping(raw_event))
        return cls(
            kind=kind,
            room_id=_required_string(payload, "room_id"),
            snapshot=NodeChatRoomSnapshot.from_mapping(raw_snapshot) if raw_snapshot is not None else None,
            app_stats=_runtime_summary_from_mapping(raw_app_stats) if raw_app_stats is not None else None,
            events=tuple(events),
            revision=_optional_int(payload, "revision") or 0,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "room_id": self.room_id,
            "snapshot": self.snapshot.to_mapping() if self.snapshot is not None else None,
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
            "events": [event.to_mapping() for event in self.events],
            "revision": self.revision,
        }


class NodeWebChatRequest(BaseModel):
    session_id: str
    author_display_name: str
    content: str
    reply_to_event_id: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("session_id", "author_display_name", "content")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("web chat fields must not be empty.")
        return text

    @field_validator("reply_to_event_id")
    @classmethod
    def _validate_optional_reply_to_event_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("reply_to_event_id must not be empty.")
        return text
