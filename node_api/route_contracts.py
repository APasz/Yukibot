"""Shared contracts for independently registered node API route domains."""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias, TypeVar

from fastapi import Request

from node_auth import NodeAccessGrant, NodeApiScope


HttpExceptionFactory: TypeAlias = Callable[[int, str], Exception]
_DiscordHealthEnum = TypeVar("_DiscordHealthEnum", bound=enum.StrEnum)
NODE_DISCORD_HEARTBEAT_LATENCY_HEADER: str = "X-Yukibot-Discord-Latency-Ms"
NODE_DISCORD_SERVICE_STATE_HEADER: str = "X-Yukibot-Discord-Service-State"


class DiscordServiceState(enum.StrEnum):
    """Discord command and gateway readiness reported by a bot node."""

    STARTING = "starting"
    COMMANDS_READY = "commands_ready"
    READY = "ready"
    DEGRADED = "degraded"
    GATEWAY_DEGRADED = "gateway_degraded"
    FAILED = "failed"


class DiscordHealthComponentState(enum.StrEnum):
    """Readiness of one independently observed Discord component."""

    UNKNOWN = "unknown"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DiscordHealthSnapshot:
    """Detailed Discord health observation projected to ``service_state`` for clients."""

    service_state: DiscordServiceState
    command_state: DiscordHealthComponentState
    gateway_state: DiscordHealthComponentState
    rest_state: DiscordHealthComponentState
    state_changed_at: datetime
    last_rest_success_at: datetime | None
    last_rest_failure_at: datetime | None
    next_retry_at: datetime | None
    last_error: str | None
    gateway_latency_ms: int | None
    reconnecting_shard_ids: tuple[int, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "DiscordHealthSnapshot":
        def _required_enum(
            field_name: str,
            enum_type: type[_DiscordHealthEnum],
        ) -> _DiscordHealthEnum:
            value = payload.get(field_name)
            if not isinstance(value, str):
                raise ValueError(f"Discord health {field_name} is invalid.")
            try:
                return enum_type(value)
            except ValueError as xcp:
                raise ValueError(f"Discord health {field_name} is invalid.") from xcp

        def _optional_datetime(field_name: str) -> datetime | None:
            value = payload.get(field_name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"Discord health {field_name} is invalid.")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as xcp:
                raise ValueError(f"Discord health {field_name} is invalid.") from xcp
            if parsed.tzinfo is None:
                raise ValueError(f"Discord health {field_name} must include a timezone.")
            return parsed

        raw_latency = payload.get("gateway_latency_ms")
        if raw_latency is not None and (isinstance(raw_latency, bool) or not isinstance(raw_latency, int)):
            raise ValueError("Discord health gateway_latency_ms is invalid.")
        if isinstance(raw_latency, int) and raw_latency < 0:
            raise ValueError("Discord health gateway_latency_ms is invalid.")
        raw_error = payload.get("last_error")
        if raw_error is not None and not isinstance(raw_error, str):
            raise ValueError("Discord health last_error is invalid.")
        raw_reconnecting_shard_ids = payload.get("reconnecting_shard_ids")
        if not isinstance(raw_reconnecting_shard_ids, Sequence) or isinstance(raw_reconnecting_shard_ids, str | bytes):
            raise ValueError("Discord health reconnecting_shard_ids is invalid.")
        reconnecting_shard_ids: list[int] = []
        for raw_shard_id in raw_reconnecting_shard_ids:
            if isinstance(raw_shard_id, bool) or not isinstance(raw_shard_id, int) or raw_shard_id < 0:
                raise ValueError("Discord health reconnecting_shard_ids is invalid.")
            reconnecting_shard_ids.append(raw_shard_id)
        if len(reconnecting_shard_ids) != len(set(reconnecting_shard_ids)):
            raise ValueError("Discord health reconnecting_shard_ids must be unique.")

        state_changed_at = _optional_datetime("state_changed_at")
        if state_changed_at is None:
            raise ValueError("Discord health state_changed_at is invalid.")
        return cls(
            service_state=_required_enum("state", DiscordServiceState),
            command_state=_required_enum("command_state", DiscordHealthComponentState),
            gateway_state=_required_enum("gateway_state", DiscordHealthComponentState),
            rest_state=_required_enum("rest_state", DiscordHealthComponentState),
            state_changed_at=state_changed_at,
            last_rest_success_at=_optional_datetime("last_rest_success_at"),
            last_rest_failure_at=_optional_datetime("last_rest_failure_at"),
            next_retry_at=_optional_datetime("next_retry_at"),
            last_error=raw_error,
            gateway_latency_ms=raw_latency,
            reconnecting_shard_ids=tuple(reconnecting_shard_ids),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "state": self.service_state.value,
            "command_state": self.command_state.value,
            "gateway_state": self.gateway_state.value,
            "rest_state": self.rest_state.value,
            "state_changed_at": self.state_changed_at.isoformat(),
            "last_rest_success_at": (
                self.last_rest_success_at.isoformat() if self.last_rest_success_at is not None else None
            ),
            "last_rest_failure_at": (
                self.last_rest_failure_at.isoformat() if self.last_rest_failure_at is not None else None
            ),
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at is not None else None,
            "last_error": self.last_error,
            "gateway_latency_ms": self.gateway_latency_ms,
            "reconnecting_shard_ids": self.reconnecting_shard_ids,
        }


class MappingResponse(Protocol):
    """A domain response that can be encoded for the HTTP API."""

    def to_mapping(self) -> dict[str, object]: ...


class NodeAuthenticatedRouteService(Protocol):
    """Authentication operations shared by node API route registrars."""

    @property
    def node_name(self) -> str: ...

    def _require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        token_node_names: Sequence[str] | None = None,
    ) -> NodeAccessGrant | None: ...

    def _request_actor_user_id(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        verified_grant: NodeAccessGrant | None = None,
    ) -> int: ...
