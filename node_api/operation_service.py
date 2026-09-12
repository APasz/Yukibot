"""Operation API policy and views shared by node HTTP routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from _security import Power_Level
from node_auth import NodeApiScope
from .operations import NodeOperationKind, NodeOperationRecord, NodeOperationService, NodeOperationState


NodeOperationCancellationHandler: TypeAlias = Callable[[str, int], Awaitable[None]]
_CANCELLABLE_OPERATION_STATES: frozenset[NodeOperationState] = frozenset(
    {
        NodeOperationState.QUEUED,
        NodeOperationState.RUNNING,
    }
)


class NodeOperationTargetScope(StrEnum):
    """The resource boundary an operation kind is authorised against."""

    NODE = "node"
    APP = "app"


@dataclass(frozen=True, slots=True)
class NodeOperationKindPolicy:
    """The API visibility and cancellation behaviour for one operation kind."""

    kind: NodeOperationKind
    kind_label: str
    read_scope: NodeApiScope
    cancel_scope: NodeApiScope
    required_level: Power_Level
    target_scope: NodeOperationTargetScope = NodeOperationTargetScope.NODE
    cancellation_handler: NodeOperationCancellationHandler | None = None

    def __post_init__(self) -> None:
        if not self.kind_label.strip():
            raise ValueError("Operation kind label must not be blank.")
        try:
            target_scope = NodeOperationTargetScope(self.target_scope)
        except (TypeError, ValueError) as xcp:
            raise ValueError("Operation target scope is invalid.") from xcp
        object.__setattr__(self, "target_scope", target_scope)

    def app_name_for_request(self, app_name: str | None) -> str | None:
        """Validate and normalize the app boundary supplied by an API request."""

        if self.target_scope is NodeOperationTargetScope.NODE:
            if app_name is not None:
                raise ValueError(
                    f"{self.kind_label} operations are node-scoped and do not accept an app name."
                )
            return None
        if app_name is None:
            raise ValueError(f"{self.kind_label} operations require an app name.")
        if not (normalised_app_name := app_name.strip()):
            raise ValueError("Operation app name must not be blank.")
        return normalised_app_name


@dataclass(frozen=True, slots=True)
class NodeOperationView:
    """A client-facing operation record enriched with its API capabilities."""

    record: NodeOperationRecord
    kind_label: str
    cancellable: bool

    def __post_init__(self) -> None:
        if not self.kind_label.strip():
            raise ValueError("Operation kind label must not be blank.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeOperationView":
        raw_kind_label = payload.get("kind_label")
        raw_cancellable = payload.get("cancellable")
        if not isinstance(raw_kind_label, str) or not raw_kind_label.strip():
            raise ValueError("Operation kind label is invalid.")
        if not isinstance(raw_cancellable, bool):
            raise ValueError("Operation cancellable state is invalid.")
        return cls(
            record=NodeOperationRecord.from_mapping(payload),
            kind_label=raw_kind_label.strip(),
            cancellable=raw_cancellable,
        )

    def to_mapping(self) -> dict[str, object]:
        payload = self.record.to_mapping()
        payload["kind_label"] = self.kind_label
        payload["cancellable"] = self.cancellable
        return payload


class NodeOperationApiService:
    """Projects registered durable operation kinds through one shared API."""

    def __init__(
        self,
        *,
        operations: NodeOperationService,
        policies: Iterable[NodeOperationKindPolicy],
    ) -> None:
        policies_by_kind: dict[NodeOperationKind, NodeOperationKindPolicy] = {}
        for policy in policies:
            if policy.kind in policies_by_kind:
                raise ValueError(
                    f"Operation kind {policy.kind.value} has more than one API policy."
                )
            policies_by_kind[policy.kind] = policy
        if not policies_by_kind:
            raise ValueError("At least one operation API policy is required.")
        self._operations = operations
        self._policies_by_kind = policies_by_kind

    def read_scope_for(
        self,
        *,
        kind: NodeOperationKind | None,
        app_name: str | None = None,
    ) -> NodeApiScope:
        """Return the access scope needed before reading the requested records."""

        return self.policy_for_request(kind=kind, app_name=app_name).read_scope

    def cancel_scope_for(
        self,
        *,
        kind: NodeOperationKind | None,
        app_name: str | None = None,
    ) -> NodeApiScope:
        """Return the access scope needed before requesting cancellation."""

        return self.policy_for_request(kind=kind, app_name=app_name).cancel_scope

    def list_operations(
        self,
        *,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
        limit: int | None = None,
    ) -> tuple[NodeOperationView, ...]:
        """List registered operations newest first without their log bodies."""

        policy = self.policy_for_request(kind=kind, app_name=app_name)
        target_app_name = policy.app_name_for_request(app_name)
        records = self._operations.list_records(
            kind=policy.kind,
            app_name=target_app_name,
            limit=limit,
            include_log_lines=False,
        )
        return tuple(
            self._view_for(record)
            for record in records
            if self._matches_target(
                record=record,
                policy=policy,
                app_name=target_app_name,
            )
        )

    def get_operation(
        self,
        *,
        operation_id: str,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
    ) -> NodeOperationView:
        """Return one registered operation with its retained log lines."""

        policy = self.policy_for_request(kind=kind, app_name=app_name)
        target_app_name = policy.app_name_for_request(app_name)
        record = self._operations.get(operation_id)
        self._require_matching_kind(record=record, kind=policy.kind)
        self._require_matching_target(
            record=record,
            policy=policy,
            app_name=target_app_name,
        )
        return self._view_for(record)

    async def cancel_operation(
        self,
        *,
        operation_id: str,
        actor_user_id: int,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
    ) -> NodeOperationView:
        """Delegate cancellation to the operation kind's safe executor path."""

        policy = self.policy_for_request(kind=kind, app_name=app_name)
        target_app_name = policy.app_name_for_request(app_name)
        record = self._operations.get(operation_id)
        self._require_matching_kind(record=record, kind=policy.kind)
        self._require_matching_target(
            record=record,
            policy=policy,
            app_name=target_app_name,
        )
        handler = policy.cancellation_handler
        if handler is None:
            raise ValueError(f"{policy.kind_label} operations cannot be cancelled.")
        if record.state is NodeOperationState.CANCEL_REQUESTED:
            raise ValueError("Cancellation has already been requested.")
        if record.state.terminal:
            raise ValueError("Operation has already finished.")
        if record.state not in _CANCELLABLE_OPERATION_STATES:
            raise ValueError("Operation is not in a cancellable state.")
        await handler(record.operation_id, actor_user_id)
        record = self._operations.get(record.operation_id)
        return self._view_for(record)

    def policy_for(self, kind: NodeOperationKind) -> NodeOperationKindPolicy:
        """Return the registered API policy for one operation kind."""

        try:
            return self._policies_by_kind[kind]
        except KeyError as xcp:
            raise LookupError(
                f"Operation type {kind.value} is not exposed through the operations API."
            ) from xcp

    def policy_for_request(
        self,
        *,
        kind: NodeOperationKind | None,
        app_name: str | None,
    ) -> NodeOperationKindPolicy:
        """Return the policy applicable before looking up an operation record."""

        if kind is not None:
            policy = self.policy_for(kind)
        elif len(self._policies_by_kind) == 1:
            policy = next(iter(self._policies_by_kind.values()))
        else:
            raise ValueError(
                "Operation kind is required when multiple operation types are exposed."
            )
        policy.app_name_for_request(app_name)
        return policy

    def _view_for(self, record: NodeOperationRecord) -> NodeOperationView:
        policy = self.policy_for(record.kind)
        return NodeOperationView(
            record=record,
            kind_label=policy.kind_label,
            cancellable=(
                policy.cancellation_handler is not None
                and record.state in _CANCELLABLE_OPERATION_STATES
            ),
        )

    @staticmethod
    def _require_matching_kind(
        *,
        record: NodeOperationRecord,
        kind: NodeOperationKind | None,
    ) -> None:
        if kind is not None and record.kind is not kind:
            raise LookupError("Operation was not found.")

    @staticmethod
    def _matches_target(
        *,
        record: NodeOperationRecord,
        policy: NodeOperationKindPolicy,
        app_name: str | None,
    ) -> bool:
        try:
            NodeOperationApiService._require_matching_target(
                record=record,
                policy=policy,
                app_name=app_name,
            )
        except LookupError:
            return False
        return True

    @staticmethod
    def _require_matching_target(
        *,
        record: NodeOperationRecord,
        policy: NodeOperationKindPolicy,
        app_name: str | None,
    ) -> None:
        if policy.target_scope is NodeOperationTargetScope.NODE:
            if record.app_name is not None:
                raise LookupError("Operation was not found.")
            return
        if (
            app_name is None
            or record.app_name is None
            or record.app_name.casefold() != app_name.casefold()
        ):
            raise LookupError("Operation was not found.")


__all__: tuple[str, ...] = (
    "NodeOperationApiService",
    "NodeOperationCancellationHandler",
    "NodeOperationKindPolicy",
    "NodeOperationTargetScope",
    "NodeOperationView",
)
