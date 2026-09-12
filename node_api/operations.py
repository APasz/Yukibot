"""Durable node-local operation records and execution coordination.

This module deliberately records no request payloads. Operation payloads can
contain credentials, while an operation's identity, lifecycle, progress, and
sanitised log output are safe and useful to retain for operators.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import cast


_OPERATION_LOG_LINE_LIMIT = 100
_OPERATION_RESULT_MAX_BYTES = 5 * 1024 * 1024
_INTERRUPTED_SUMMARY = "Interrupted by a node restart."
_CANCEL_REQUESTED_SUMMARY = "Cancellation requested."
_CANCELLED_SUMMARY = "Operation cancelled."
_UNREPORTED_COMPLETION_SUMMARY = "Operation ended without reporting a result."
_UNEXPECTED_FAILURE_SUMMARY = "Operation task failed unexpectedly."
_DATABASE_SCHEMA_VERSION = 2

_SCHEMA_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS node_operations (
        operation_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        node_name TEXT NOT NULL,
        subject TEXT NOT NULL,
        requested_by_user_id INTEGER,
        state TEXT NOT NULL,
        summary TEXT NOT NULL,
        phase TEXT,
        result_reference TEXT,
        detail TEXT,
        progress_percent REAL,
        created_at_unix_ms INTEGER NOT NULL,
        started_at_unix_ms INTEGER,
        finished_at_unix_ms INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_operation_logs (
        operation_id TEXT NOT NULL REFERENCES node_operations(operation_id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,
        line TEXT NOT NULL,
        PRIMARY KEY (operation_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_operation_resources (
        resource_key TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL REFERENCES node_operations(operation_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS node_operations_created_index
        ON node_operations (created_at_unix_ms DESC, operation_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS node_operations_kind_index
        ON node_operations (kind, created_at_unix_ms DESC, operation_id DESC)
    """,
)

_SCHEMA_V1_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "node_operations",
        (
            "operation_id",
            "kind",
            "node_name",
            "subject",
            "requested_by_user_id",
            "state",
            "summary",
            "phase",
            "result_reference",
            "detail",
            "progress_percent",
            "created_at_unix_ms",
            "started_at_unix_ms",
            "finished_at_unix_ms",
        ),
    ),
    ("node_operation_logs", ("operation_id", "sequence", "line")),
    ("node_operation_resources", ("resource_key", "operation_id")),
)

_SCHEMA_V2_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "node_operations",
        (
            "operation_id",
            "kind",
            "node_name",
            "subject",
            "requested_by_user_id",
            "state",
            "summary",
            "phase",
            "result_reference",
            "detail",
            "progress_percent",
            "created_at_unix_ms",
            "started_at_unix_ms",
            "finished_at_unix_ms",
            "app_name",
        ),
    ),
    ("node_operation_logs", ("operation_id", "sequence", "line")),
    ("node_operation_resources", ("resource_key", "operation_id")),
    (
        "node_operation_results",
        ("operation_id", "payload_json", "expires_at_unix_ms"),
    ),
)

_SCHEMA_COLUMNS_BY_VERSION: dict[int, tuple[tuple[str, tuple[str, ...]], ...]] = {
    1: _SCHEMA_V1_COLUMNS,
    2: _SCHEMA_V2_COLUMNS,
}

log = logging.getLogger(__name__)


class NodeOperationKind(StrEnum):
    """A durable operation type understood by the local node."""

    APP_INSTALL = "app_install"
    MOD_METADATA_DISCOVERY = "mod_metadata_discovery"
    MOD_METADATA_APPLY = "mod_metadata_apply"


class NodeOperationState(StrEnum):
    """The lifecycle states shared by durable node operations."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def active(self) -> bool:
        return self in {
            NodeOperationState.QUEUED,
            NodeOperationState.RUNNING,
            NodeOperationState.CANCEL_REQUESTED,
        }

    @property
    def terminal(self) -> bool:
        return not self.active


class NodeOperationChangeKind(StrEnum):
    """The durable-record change that a live operation consumer should apply."""

    CREATED = "created"
    UPDATED = "updated"
    FINISHED = "finished"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class NodeOperationProgress:
    """The current visible progress for an active operation."""

    summary: str
    phase: str | None = None
    detail: str | None = None
    progress_percent: float | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("Operation progress summary must not be blank.")
        if self.phase is not None and not self.phase.strip():
            raise ValueError(
                "Operation progress phase must not be blank when provided."
            )
        if self.detail is not None and not self.detail.strip():
            raise ValueError(
                "Operation progress detail must not be blank when provided."
            )
        if (
            self.progress_percent is not None
            and not 0.0 <= self.progress_percent <= 100.0
        ):
            raise ValueError("Operation progress must be between 0 and 100 percent.")


@dataclass(frozen=True, slots=True)
class NodeOperationRecord:
    """A retained, client-safe snapshot of one operation's lifecycle."""

    operation_id: str
    kind: NodeOperationKind
    node_name: str
    subject: str
    state: NodeOperationState
    summary: str
    requested_by_user_id: int | None
    app_name: str | None = None
    phase: str | None = None
    result_reference: str | None = None
    detail: str | None = None
    progress_percent: float | None = None
    log_lines: tuple[str, ...] = ()
    created_at_unix_ms: int = 0
    started_at_unix_ms: int | None = None
    finished_at_unix_ms: int | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("Operation ID", self.operation_id),
            ("Operation node", self.node_name),
            ("Operation subject", self.subject),
            ("Operation summary", self.summary),
        ):
            if not value.strip():
                raise ValueError(f"{label} must not be blank.")
        if self.requested_by_user_id is not None and self.requested_by_user_id <= 0:
            raise ValueError("Operation requester ID must be positive when provided.")
        for label, value in (
            ("Operation app name", self.app_name),
            ("Operation phase", self.phase),
            ("Operation result reference", self.result_reference),
            ("Operation detail", self.detail),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{label} must not be blank when provided.")
        if (
            self.progress_percent is not None
            and not 0.0 <= self.progress_percent <= 100.0
        ):
            raise ValueError("Operation progress must be between 0 and 100 percent.")
        if self.created_at_unix_ms <= 0:
            raise ValueError(
                "Operation creation time must be positive Unix milliseconds."
            )
        for label, value in (
            ("Operation start time", self.started_at_unix_ms),
            ("Operation finish time", self.finished_at_unix_ms),
        ):
            if value is not None and value <= 0:
                raise ValueError(
                    f"{label} must be positive Unix milliseconds when provided."
                )
        if self.state.terminal and self.finished_at_unix_ms is None:
            raise ValueError("Terminal operations require a finish time.")
        if self.state.active and self.finished_at_unix_ms is not None:
            raise ValueError("Active operations cannot have a finish time.")
        if len(self.log_lines) > _OPERATION_LOG_LINE_LIMIT:
            raise ValueError("Operation log history exceeds its retention limit.")
        if any(not line.strip() for line in self.log_lines):
            raise ValueError("Operation log lines must not be blank.")

    @property
    def active(self) -> bool:
        return self.state.active

    def without_log_lines(self) -> "NodeOperationRecord":
        """Return this record's complete summary without its retained log body."""

        if not self.log_lines:
            return self
        return replace(self, log_lines=())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeOperationRecord":
        """Decode one client-safe operation record from an API response."""

        raw_kind = _mapping_required_text(payload, "kind", label="Operation kind")
        raw_state = _mapping_required_text(payload, "state", label="Operation state")
        try:
            kind = NodeOperationKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("Operation kind is invalid.") from xcp
        try:
            state = NodeOperationState(raw_state)
        except ValueError as xcp:
            raise ValueError("Operation state is invalid.") from xcp
        return cls(
            operation_id=_mapping_required_text(
                payload, "operation_id", label="Operation ID"
            ),
            kind=kind,
            node_name=_mapping_required_text(
                payload, "node_name", label="Operation node"
            ),
            subject=_mapping_required_text(
                payload, "subject", label="Operation subject"
            ),
            state=state,
            summary=_mapping_required_text(
                payload, "summary", label="Operation summary"
            ),
            requested_by_user_id=_mapping_optional_int(
                payload.get("requested_by_user_id"),
                label="Operation requester ID",
            ),
            app_name=_mapping_optional_text(
                payload.get("app_name"), label="Operation app name"
            ),
            phase=_mapping_optional_text(
                payload.get("phase"), label="Operation phase"
            ),
            result_reference=_mapping_optional_text(
                payload.get("result_reference"), label="Operation result reference"
            ),
            detail=_mapping_optional_text(
                payload.get("detail"), label="Operation detail"
            ),
            progress_percent=_mapping_optional_progress(
                payload.get("progress_percent")
            ),
            log_lines=_mapping_log_lines(payload.get("log_lines", ())),
            created_at_unix_ms=_mapping_required_int(
                payload, "created_at_unix_ms", label="Operation creation time"
            ),
            started_at_unix_ms=_mapping_optional_int(
                payload.get("started_at_unix_ms"), label="Operation start time"
            ),
            finished_at_unix_ms=_mapping_optional_int(
                payload.get("finished_at_unix_ms"), label="Operation finish time"
            ),
        )

    def to_mapping(self, *, include_log_lines: bool = True) -> dict[str, object]:
        """Encode this record, optionally omitting its retained log body."""

        payload: dict[str, object] = {
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "node_name": self.node_name,
            "subject": self.subject,
            "state": self.state.value,
            "summary": self.summary,
            "requested_by_user_id": self.requested_by_user_id,
            "app_name": self.app_name,
            "phase": self.phase,
            "result_reference": self.result_reference,
            "detail": self.detail,
            "progress_percent": self.progress_percent,
            "created_at_unix_ms": self.created_at_unix_ms,
            "started_at_unix_ms": self.started_at_unix_ms,
            "finished_at_unix_ms": self.finished_at_unix_ms,
        }
        if include_log_lines:
            payload["log_lines"] = list(self.log_lines)
        return payload


@dataclass(frozen=True, slots=True)
class NodeOperationChange:
    """A complete durable operation snapshot following one lifecycle mutation."""

    kind: NodeOperationChangeKind
    record: NodeOperationRecord
    immediate: bool


@dataclass(frozen=True, slots=True)
class NodeOperationResult:
    """A type-owned, client-safe JSON artifact retained for an operation."""

    payload_json: str
    expires_at_unix_ms: int | None = None

    def __post_init__(self) -> None:
        payload_json = _required_text(
            self.payload_json, label="Operation result payload"
        )
        if len(payload_json.encode("utf-8")) > _OPERATION_RESULT_MAX_BYTES:
            raise ValueError("Operation result payload exceeds its retention limit.")
        try:
            decoded = json.loads(payload_json)
        except json.JSONDecodeError as xcp:
            raise ValueError("Operation result payload must be valid JSON.") from xcp
        if not isinstance(decoded, dict):
            raise ValueError("Operation result payload must be a JSON object.")
        if (
            self.expires_at_unix_ms is not None
            and self.expires_at_unix_ms <= 0
        ):
            raise ValueError(
                "Operation result expiration must be positive Unix milliseconds when provided."
            )


class NodeOperationResourceConflict(RuntimeError):
    """Raised when an active operation already owns a required resource."""

    def __init__(self, resource_key: str) -> None:
        self.resource_key = resource_key
        super().__init__(f"Operation resource is already busy: {resource_key}")


class NodeOperationSchemaVersionError(RuntimeError):
    """Raised when an operation database was created by a newer application."""

    def __init__(self, *, database_version: int, supported_version: int) -> None:
        self.database_version = database_version
        self.supported_version = supported_version
        super().__init__(
            "Operation database schema version "
            f"{database_version} is newer than supported version {supported_version}."
        )


class NodeOperationService:
    """Owns durable operation state, resource reservations, and active tasks.

    Passing ``database_path`` enables persistent records. Omitting it provides
    an in-memory instance useful for narrow unit-level compositions. The node
    API composition root always supplies a database path.
    """

    def __init__(
        self,
        *,
        database_path: Path | None = None,
        completed_history_limit: int = 100,
        now_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        if completed_history_limit < 1:
            raise ValueError("Completed operation history limit must be positive.")
        self._database_path = database_path
        self._database: sqlite3.Connection | None = None
        self._completed_history_limit = completed_history_limit
        self._now_unix_ms = now_unix_ms or _unix_ms_now
        self._lock = threading.RLock()
        self._records: dict[str, NodeOperationRecord] = {}
        self._results: dict[str, NodeOperationResult] = {}
        self._resource_owners: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[object]] = {}
        self._hard_cancelled_tasks: dict[str, asyncio.Task[object]] = {}
        self._change_subscribers: dict[str, Callable[[NodeOperationChange], None]] = {}
        self._pending_changes: deque[tuple[NodeOperationChange, tuple[str, ...]]] = deque()
        self._dispatching_changes = False

    def subscribe_changes(
        self,
        callback: Callable[[NodeOperationChange], None],
    ) -> Callable[[], None]:
        """Subscribe to complete operation records as their durable state changes."""

        with self._lock:
            return self._subscribe_changes_locked(callback)

    def subscribe_changes_with_snapshot(
        self,
        callback: Callable[[NodeOperationChange], None],
        *,
        include_log_lines: bool = True,
    ) -> tuple[tuple[NodeOperationRecord, ...], Callable[[], None]]:
        """Atomically subscribe and return a record snapshot with optional log bodies."""

        with self._lock:
            self._ensure_database_locked()
            snapshot = self._list_all_records_locked(
                include_log_lines=include_log_lines
            )
            unsubscribe = self._subscribe_changes_locked(callback)
        return snapshot, unsubscribe

    def create(
        self,
        *,
        kind: NodeOperationKind,
        node_name: str,
        subject: str,
        requested_by_user_id: int | None,
        progress: NodeOperationProgress,
        app_name: str | None = None,
        resource_keys: Sequence[str] = (),
    ) -> NodeOperationRecord:
        """Create and reserve the resources for a queued operation."""

        operation_id = uuid.uuid4().hex
        now = self._now_unix_ms()
        record = NodeOperationRecord(
            operation_id=operation_id,
            kind=kind,
            node_name=node_name,
            subject=subject,
            state=NodeOperationState.QUEUED,
            summary=progress.summary,
            requested_by_user_id=requested_by_user_id,
            app_name=_optional_text(app_name, label="Operation app name"),
            phase=progress.phase,
            detail=progress.detail,
            progress_percent=progress.progress_percent,
            created_at_unix_ms=now,
        )
        normalised_resource_keys = _normalise_resource_keys(resource_keys)
        with self._lock:
            self._ensure_database_locked()
            if self._database is None:
                self._create_memory_locked(record, normalised_resource_keys)
            else:
                self._create_database_locked(record, normalised_resource_keys)
            self._queue_change_locked(
                NodeOperationChange(
                    kind=NodeOperationChangeKind.CREATED,
                    record=record,
                    immediate=True,
                )
            )
        self._dispatch_changes()
        return record

    def get(
        self,
        operation_id: str,
        *,
        include_log_lines: bool = True,
    ) -> NodeOperationRecord:
        """Return an operation snapshot, optionally without retained log lines."""

        with self._lock:
            self._ensure_database_locked()
            return self._record_locked(
                operation_id,
                include_log_lines=include_log_lines,
            )

    def list_records(
        self,
        *,
        kind: NodeOperationKind | None = None,
        app_name: str | None = None,
        limit: int | None = None,
        include_log_lines: bool = True,
    ) -> tuple[NodeOperationRecord, ...]:
        """List retained records newest first, optionally without retained log lines."""

        if limit is not None and limit < 1:
            raise ValueError("Operation list limit must be positive when provided.")
        normalised_app_name = _optional_text(app_name, label="Operation app name")
        with self._lock:
            self._ensure_database_locked()
            if self._database is None:
                ordered = sorted(
                    (
                        record
                        for record in self._records.values()
                        if kind is None or record.kind is kind
                        if normalised_app_name is None
                        or (
                            record.app_name is not None
                            and record.app_name.casefold()
                            == normalised_app_name.casefold()
                        )
                    ),
                    key=lambda record: (record.created_at_unix_ms, record.operation_id),
                    reverse=True,
                )
                selected = ordered if limit is None else ordered[:limit]
                if include_log_lines:
                    return tuple(selected)
                return tuple(record.without_log_lines() for record in selected)
            return self._list_database_records_locked(
                kind=kind,
                app_name=normalised_app_name,
                limit=limit,
                include_log_lines=include_log_lines,
            )

    def _list_all_records_locked(
        self,
        *,
        include_log_lines: bool,
    ) -> tuple[NodeOperationRecord, ...]:
        if self._database is None:
            records = tuple(
                sorted(
                    self._records.values(),
                    key=lambda record: (record.created_at_unix_ms, record.operation_id),
                    reverse=True,
                )
            )
            if include_log_lines:
                return records
            return tuple(record.without_log_lines() for record in records)
        return self._list_database_records_locked(
            kind=None,
            app_name=None,
            limit=None,
            include_log_lines=include_log_lines,
        )

    def begin(
        self, *, operation_id: str, progress: NodeOperationProgress
    ) -> NodeOperationRecord:
        """Mark a queued operation as running, unless cancellation won the race."""

        with self._lock:
            self._ensure_database_locked()
            current = self._record_locked(operation_id)
            if (
                current.state is NodeOperationState.CANCEL_REQUESTED
                or current.state.terminal
            ):
                return current
            if current.state is not NodeOperationState.QUEUED:
                raise RuntimeError(
                    f"Operation {operation_id} cannot begin from {current.state.value}."
                )
            updated = replace(
                current,
                state=NodeOperationState.RUNNING,
                summary=progress.summary,
                phase=progress.phase,
                detail=progress.detail,
                progress_percent=progress.progress_percent,
                started_at_unix_ms=self._now_unix_ms(),
            )
            self._write_record_locked(updated)
            self._queue_change_locked(
                NodeOperationChange(
                    kind=NodeOperationChangeKind.UPDATED,
                    record=updated,
                    immediate=True,
                )
            )
        self._dispatch_changes()
        return updated

    def update_active(
        self,
        *,
        operation_id: str,
        progress: NodeOperationProgress,
        log_line: str | None = None,
    ) -> NodeOperationRecord:
        """Replace visible progress while retaining the operation's active state."""

        with self._lock:
            self._ensure_database_locked()
            current = self._record_locked(operation_id)
            if current.state.terminal:
                return current
            if current.state is NodeOperationState.QUEUED:
                raise RuntimeError(f"Operation {operation_id} has not started.")
            updated = replace(
                current,
                summary=progress.summary,
                phase=progress.phase,
                detail=progress.detail,
                progress_percent=progress.progress_percent,
            )
            self._write_record_locked(updated, log_line=log_line)
            updated = self._record_locked(operation_id)
            self._queue_change_locked(
                NodeOperationChange(
                    kind=NodeOperationChangeKind.UPDATED,
                    record=updated,
                    immediate=False,
                )
            )
        self._dispatch_changes()
        return updated

    def append_log(self, *, operation_id: str, log_line: str) -> NodeOperationRecord:
        """Append a sanitised log line without changing the operation state."""

        with self._lock:
            self._ensure_database_locked()
            current = self._record_locked(operation_id)
            if current.state.terminal:
                return current
            self._write_record_locked(current, log_line=log_line)
            updated = self._record_locked(operation_id)
            self._queue_change_locked(
                NodeOperationChange(
                    kind=NodeOperationChangeKind.UPDATED,
                    record=updated,
                    immediate=False,
                )
            )
        self._dispatch_changes()
        return updated

    def finish(
        self,
        *,
        operation_id: str,
        state: NodeOperationState,
        summary: str,
        detail: str | None = None,
        result_reference: str | None = None,
        progress_percent: float | None = None,
        result_payload_json: str | None = None,
        result_ttl_seconds: int | None = None,
    ) -> NodeOperationRecord:
        """Finish an operation and release its reserved resources."""

        if not state.terminal:
            raise ValueError("Only terminal operation states can finish an operation.")
        progress = NodeOperationProgress(
            summary=summary,
            detail=detail,
            progress_percent=progress_percent,
        )
        with self._lock:
            self._ensure_database_locked()
            current = self._record_locked(operation_id)
            if current.state.terminal:
                return current
            result = self._result_for_finish(
                state=state,
                result_payload_json=result_payload_json,
                result_ttl_seconds=result_ttl_seconds,
            )
            updated = replace(
                current,
                state=state,
                summary=progress.summary,
                phase=None,
                detail=progress.detail,
                result_reference=_optional_text(
                    result_reference, label="Operation result reference"
                ),
                progress_percent=progress.progress_percent,
                finished_at_unix_ms=self._now_unix_ms(),
            )
            removed_records = self._finish_record_locked(updated, operation_result=result)
            self._queue_finished_changes_locked(updated, removed_records)
        self._dispatch_changes()
        return updated

    def get_result(self, *, operation_id: str) -> NodeOperationResult:
        """Return a non-expired type-owned artifact for a retained operation."""

        with self._lock:
            self._ensure_database_locked()
            normalised_id = _required_text(operation_id, label="Operation ID")
            self._record_locked(normalised_id, include_log_lines=False)
            result = self._result_locked(normalised_id)
            if result is None:
                raise LookupError("Operation result was not found.")
            if (
                result.expires_at_unix_ms is not None
                and result.expires_at_unix_ms <= self._now_unix_ms()
            ):
                self._delete_result_locked(normalised_id)
                raise LookupError("Operation result was not found.")
            return result

    def request_cancellation(self, *, operation_id: str) -> NodeOperationRecord:
        """Record a cooperative cancellation request without interrupting a task."""

        with self._lock:
            self._ensure_database_locked()
            updated, changed = self._request_cancellation_locked(operation_id)
            if changed:
                self._queue_change_locked(
                    NodeOperationChange(
                        kind=NodeOperationChangeKind.UPDATED,
                        record=updated,
                        immediate=True,
                    )
                )
        if changed:
            self._dispatch_changes()
        return updated

    def hard_cancel(self, *, operation_id: str) -> NodeOperationRecord:
        """Request cancellation and interrupt the tracked task once when safe.

        Callers must only use this for executors whose cancellation handler can
        safely restore or preserve their externally visible state.
        """

        task_to_cancel: asyncio.Task[object] | None = None
        with self._lock:
            self._ensure_database_locked()
            updated, changed = self._request_cancellation_locked(operation_id)
            if changed:
                self._queue_change_locked(
                    NodeOperationChange(
                        kind=NodeOperationChangeKind.UPDATED,
                        record=updated,
                        immediate=True,
                    )
                )
            task = self._tasks.get(operation_id)
            if (
                not updated.state.terminal
                and task is not None
                and not task.done()
                and self._hard_cancelled_tasks.get(operation_id) is not task
            ):
                self._hard_cancelled_tasks[operation_id] = task
                task_to_cancel = task
        if task_to_cancel is not None and not self._schedule_hard_cancellation(
            operation_id=operation_id,
            task=task_to_cancel,
        ):
            with self._lock:
                if self._hard_cancelled_tasks.get(operation_id) is task_to_cancel:
                    self._hard_cancelled_tasks.pop(operation_id, None)
        if changed:
            self._dispatch_changes()
        return updated

    def cancellation_requested(self, *, operation_id: str) -> bool:
        """Return whether a handler should stop at its next safe boundary."""

        return self.get(operation_id).state is NodeOperationState.CANCEL_REQUESTED

    def track_task(self, *, operation_id: str, task: asyncio.Task[object]) -> None:
        """Associate an executor task with a durable operation.

        The done callback closes lifecycle gaps such as a task being cancelled
        before its coroutine body has started.
        """

        with self._lock:
            self._ensure_database_locked()
            record = self._record_locked(operation_id)
            if record.state.terminal:
                raise RuntimeError(f"Operation {operation_id} is already finished.")
            if operation_id in self._tasks:
                raise RuntimeError(
                    f"Operation {operation_id} already has an executor task."
                )
            self._tasks[operation_id] = task
        task.add_done_callback(
            lambda completed: self._finish_tracked_task(operation_id, completed)
        )

    def cancel_pending(self, *, kind: NodeOperationKind | None = None) -> None:
        """Request cancellation for locally tracked active operations of one kind."""

        self._cancel_pending(kind=kind, hard=False)

    def hard_cancel_pending(self, *, kind: NodeOperationKind | None = None) -> None:
        """Hard-cancel locally tracked operations when their executor permits it."""

        self._cancel_pending(kind=kind, hard=True)

    def _cancel_pending(self, *, kind: NodeOperationKind | None, hard: bool) -> None:
        with self._lock:
            self._ensure_database_locked()
            operation_ids: list[str] = []
            for operation_id in self._tasks:
                try:
                    record = self._record_locked(operation_id)
                except LookupError:
                    continue
                if kind is None or record.kind is kind:
                    operation_ids.append(operation_id)
        for operation_id in operation_ids:
            try:
                if hard:
                    self.hard_cancel(operation_id=operation_id)
                else:
                    self.request_cancellation(operation_id=operation_id)
            except LookupError:
                continue

    def _finish_tracked_task(
        self, operation_id: str, task: asyncio.Task[object]
    ) -> None:
        finished_record: NodeOperationRecord
        removed_records: tuple[NodeOperationRecord, ...] = ()
        with self._lock:
            if self._tasks.get(operation_id) is task:
                self._tasks.pop(operation_id, None)
            if self._hard_cancelled_tasks.get(operation_id) is task:
                self._hard_cancelled_tasks.pop(operation_id, None)
            try:
                record = self._record_locked(operation_id)
            except LookupError:
                return
            if record.state.terminal:
                if not task.cancelled():
                    task.exception()
                return

            if task.cancelled():
                finished_record, removed_records = self._finish_cancelled_record_locked(record)
            else:
                try:
                    error = task.exception()
                except asyncio.CancelledError:
                    error = None
                if error is None:
                    if record.state is NodeOperationState.CANCEL_REQUESTED:
                        finished_record, removed_records = self._finish_cancelled_record_locked(record)
                    else:
                        finished_record = replace(
                            record,
                            state=NodeOperationState.FAILED,
                            summary=_UNREPORTED_COMPLETION_SUMMARY,
                            phase=None,
                            finished_at_unix_ms=self._now_unix_ms(),
                        )
                        removed_records = self._finish_record_locked(finished_record)
                else:
                    log.exception(
                        "Node operation task failed without reporting its result: operation=%s kind=%s",
                        operation_id,
                        record.kind.value,
                        exc_info=error,
                    )
                    finished_record = replace(
                        record,
                        state=NodeOperationState.FAILED,
                        summary=_UNEXPECTED_FAILURE_SUMMARY,
                        phase=None,
                        finished_at_unix_ms=self._now_unix_ms(),
                    )
                    removed_records = self._finish_record_locked(finished_record)
            self._queue_finished_changes_locked(finished_record, removed_records)
        self._dispatch_changes()

    def _finish_cancelled_record_locked(
        self,
        record: NodeOperationRecord,
    ) -> tuple[NodeOperationRecord, tuple[NodeOperationRecord, ...]]:
        updated = replace(
            record,
            state=NodeOperationState.CANCELLED,
            summary=_CANCELLED_SUMMARY,
            phase=None,
            finished_at_unix_ms=self._now_unix_ms(),
        )
        return updated, self._finish_record_locked(updated)

    def _schedule_hard_cancellation(
        self,
        *,
        operation_id: str,
        task: asyncio.Task[object],
    ) -> bool:
        if task.done():
            return False
        try:
            task.get_loop().call_soon_threadsafe(
                self._cancel_tracked_task_if_active,
                operation_id,
                task,
            )
        except RuntimeError:
            return False
        return True

    def _cancel_tracked_task_if_active(
        self,
        operation_id: str,
        task: asyncio.Task[object],
    ) -> None:
        with self._lock:
            if self._hard_cancelled_tasks.get(operation_id) is not task:
                return
            try:
                record = self._record_locked(operation_id)
            except LookupError:
                self._hard_cancelled_tasks.pop(operation_id, None)
                return
            if (
                self._tasks.get(operation_id) is not task
                or task.done()
                or record.state.terminal
            ):
                self._hard_cancelled_tasks.pop(operation_id, None)
                return
            task.cancel()

    def _request_cancellation_locked(
        self,
        operation_id: str,
    ) -> tuple[NodeOperationRecord, bool]:
        current = self._record_locked(operation_id)
        if (
            current.state.terminal
            or current.state is NodeOperationState.CANCEL_REQUESTED
        ):
            return current, False
        updated = replace(
            current,
            state=NodeOperationState.CANCEL_REQUESTED,
            summary=_CANCEL_REQUESTED_SUMMARY,
        )
        self._write_record_locked(updated)
        return updated, True

    def _subscribe_changes_locked(
        self,
        callback: Callable[[NodeOperationChange], None],
    ) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("Operation change callback must be callable.")
        subscription_id = uuid.uuid4().hex
        self._change_subscribers[subscription_id] = callback

        def _unsubscribe() -> None:
            with self._lock:
                self._change_subscribers.pop(subscription_id, None)

        return _unsubscribe

    def _queue_change_locked(self, change: NodeOperationChange) -> None:
        """Queue one complete record change in durable mutation order."""

        self._pending_changes.append((change, tuple(self._change_subscribers)))

    def _queue_finished_changes_locked(
        self,
        record: NodeOperationRecord,
        removed_records: tuple[NodeOperationRecord, ...],
    ) -> None:
        self._queue_change_locked(
            NodeOperationChange(
                kind=NodeOperationChangeKind.FINISHED,
                record=record,
                immediate=True,
            )
        )
        for removed_record in removed_records:
            self._queue_change_locked(
                NodeOperationChange(
                    kind=NodeOperationChangeKind.REMOVED,
                    record=removed_record,
                    immediate=True,
                )
            )

    def _dispatch_changes(self) -> None:
        """Deliver queued changes serially without holding the durable-state lock."""

        with self._lock:
            if self._dispatching_changes:
                return
            self._dispatching_changes = True

        try:
            while True:
                with self._lock:
                    if not self._pending_changes:
                        self._dispatching_changes = False
                        return
                    change, subscription_ids = self._pending_changes.popleft()
                    callbacks = tuple(
                        callback
                        for subscription_id in subscription_ids
                        if (
                            callback := self._change_subscribers.get(subscription_id)
                        ) is not None
                    )
                for callback in callbacks:
                    try:
                        callback(change)
                    except Exception:
                        log.exception(
                            "Node operation change subscriber failed: operation=%s kind=%s",
                            change.record.operation_id,
                            change.kind.value,
                        )
        except BaseException:
            with self._lock:
                self._dispatching_changes = False
                retry_dispatch = bool(self._pending_changes)
            if retry_dispatch:
                self._dispatch_changes()
            raise

    def _ensure_database_locked(self) -> None:
        if self._database_path is None or self._database is not None:
            return
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(
            self._database_path,
            check_same_thread=False,
            isolation_level=None,
        )
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA busy_timeout = 5000")
            self._database = database
            self._migrate_database_locked()
            recovered = self._recover_active_database_records_locked()
        except BaseException:
            self._database = None
            database.close()
            raise
        if recovered:
            log.warning(
                "Recovered interrupted node operations: path=%s count=%s",
                self._database_path,
                recovered,
            )

    def _migrate_database_locked(self) -> None:
        database = self._require_database_locked()
        version = self._database_schema_version_locked()
        if version > _DATABASE_SCHEMA_VERSION:
            raise NodeOperationSchemaVersionError(
                database_version=version,
                supported_version=_DATABASE_SCHEMA_VERSION,
            )
        while version < _DATABASE_SCHEMA_VERSION:
            next_version = version + 1
            with self._database_transaction_locked():
                self._apply_database_migration_locked(
                    from_version=version,
                    to_version=next_version,
                )
                database.execute(f"PRAGMA user_version = {next_version}")
            version = next_version
        self._validate_database_schema_locked(version=version)

    def _database_schema_version_locked(self) -> int:
        database = self._require_database_locked()
        row = database.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise RuntimeError("Operation database schema version is unavailable.")
        version = cast(object, row[0])
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise RuntimeError("Operation database schema version is invalid.")
        return version

    def _apply_database_migration_locked(
        self,
        *,
        from_version: int,
        to_version: int,
    ) -> None:
        if from_version == 0 and to_version == 1:
            self._create_schema_v1_locked()
            self._validate_database_schema_locked(version=to_version)
            return
        if from_version == 1 and to_version == 2:
            self._migrate_schema_v1_to_v2_locked()
            self._validate_database_schema_locked(version=to_version)
            return
        raise RuntimeError(
            "Operation database migration path is unsupported: "
            f"{from_version} to {to_version}."
        )

    def _create_schema_v1_locked(self) -> None:
        database = self._require_database_locked()
        for statement in _SCHEMA_V1_STATEMENTS:
            database.execute(statement)

    def _migrate_schema_v1_to_v2_locked(self) -> None:
        database = self._require_database_locked()
        database.execute("ALTER TABLE node_operations ADD COLUMN app_name TEXT")
        database.execute(
            """
            CREATE TABLE node_operation_results (
                operation_id TEXT PRIMARY KEY REFERENCES node_operations(operation_id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                expires_at_unix_ms INTEGER
            )
            """
        )
        database.execute(
            """
            CREATE INDEX node_operations_app_kind_index
                ON node_operations (app_name, kind, created_at_unix_ms DESC, operation_id DESC)
            """
        )

    def _validate_database_schema_locked(self, *, version: int) -> None:
        try:
            expected_schema = _SCHEMA_COLUMNS_BY_VERSION[version]
        except KeyError as xcp:
            raise RuntimeError(
                f"Operation database schema version {version} is unsupported."
            ) from xcp
        database = self._require_database_locked()
        for table_name, expected_columns in expected_schema:
            rows = database.execute(f"PRAGMA table_info({table_name})").fetchall()
            actual_columns = tuple(cast(str, row["name"]) for row in rows)
            if actual_columns != expected_columns:
                raise RuntimeError(
                    f"Operation database table {table_name} does not match schema version {version}."
                )

    def _recover_active_database_records_locked(self) -> int:
        database = self._require_database_locked()
        active_values = tuple(
            state.value for state in NodeOperationState if state.active
        )
        placeholders = ", ".join("?" for _ in active_values)
        rows = database.execute(
            f"SELECT operation_id FROM node_operations WHERE state IN ({placeholders})",
            active_values,
        ).fetchall()
        if not rows:
            return 0
        now = self._now_unix_ms()
        operation_ids = tuple(cast(str, row["operation_id"]) for row in rows)
        with self._database_transaction_locked():
            database.execute(
                f"""
                UPDATE node_operations
                SET state = ?, summary = ?, phase = NULL, finished_at_unix_ms = ?
                WHERE state IN ({placeholders})
                """,
                (
                    NodeOperationState.INTERRUPTED.value,
                    _INTERRUPTED_SUMMARY,
                    now,
                    *active_values,
                ),
            )
            resource_placeholders = ", ".join("?" for _ in operation_ids)
            database.execute(
                f"DELETE FROM node_operation_resources WHERE operation_id IN ({resource_placeholders})",
                operation_ids,
            )
            self._prune_database_completed_locked()
        return len(operation_ids)

    def _create_memory_locked(
        self,
        record: NodeOperationRecord,
        resource_keys: tuple[str, ...],
    ) -> None:
        conflict = next(
            (
                resource_key
                for resource_key in resource_keys
                if resource_key in self._resource_owners
            ),
            None,
        )
        if conflict is not None:
            raise NodeOperationResourceConflict(conflict)
        self._records[record.operation_id] = record
        for resource_key in resource_keys:
            self._resource_owners[resource_key] = record.operation_id

    def _create_database_locked(
        self,
        record: NodeOperationRecord,
        resource_keys: tuple[str, ...],
    ) -> None:
        database = self._require_database_locked()
        with self._database_transaction_locked():
            conflict = self._database_resource_conflict_locked(resource_keys)
            if conflict is not None:
                raise NodeOperationResourceConflict(conflict)
            database.execute(
                """
                INSERT INTO node_operations (
                    operation_id, kind, node_name, subject, requested_by_user_id,
                    state, summary, phase, result_reference, detail, progress_percent,
                    created_at_unix_ms, started_at_unix_ms, finished_at_unix_ms, app_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _record_values(record),
            )
            database.executemany(
                "INSERT INTO node_operation_resources (resource_key, operation_id) VALUES (?, ?)",
                ((resource_key, record.operation_id) for resource_key in resource_keys),
            )

    def _database_resource_conflict_locked(
        self,
        resource_keys: tuple[str, ...],
    ) -> str | None:
        if not resource_keys:
            return None
        database = self._require_database_locked()
        placeholders = ", ".join("?" for _ in resource_keys)
        row = database.execute(
            f"""
            SELECT resource_key
            FROM node_operation_resources
            WHERE resource_key IN ({placeholders})
            ORDER BY resource_key
            LIMIT 1
            """,
            resource_keys,
        ).fetchone()
        return None if row is None else cast(str, row["resource_key"])

    def _record_locked(
        self,
        operation_id: str,
        *,
        include_log_lines: bool = True,
    ) -> NodeOperationRecord:
        normalised_id = _required_text(operation_id, label="Operation ID")
        if self._database is None:
            try:
                record = self._records[normalised_id]
            except KeyError as xcp:
                raise LookupError("Operation was not found.") from xcp
            return record if include_log_lines else record.without_log_lines()
        database = self._require_database_locked()
        row = database.execute(
            "SELECT * FROM node_operations WHERE operation_id = ?",
            (normalised_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Operation was not found.")
        return self._database_row_to_record_locked(
            row,
            include_log_lines=include_log_lines,
        )

    def _list_database_records_locked(
        self,
        *,
        kind: NodeOperationKind | None,
        app_name: str | None,
        limit: int | None,
        include_log_lines: bool,
    ) -> tuple[NodeOperationRecord, ...]:
        database = self._require_database_locked()
        query = "SELECT * FROM node_operations"
        arguments: list[object] = []
        conditions: list[str] = []
        if kind is not None:
            conditions.append("kind = ?")
            arguments.append(kind.value)
        if app_name is not None:
            conditions.append("app_name COLLATE NOCASE = ?")
            arguments.append(app_name)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at_unix_ms DESC, operation_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            arguments.append(limit)
        rows = database.execute(query, arguments).fetchall()
        return tuple(
            self._database_row_to_record_locked(
                row,
                include_log_lines=include_log_lines,
            )
            for row in rows
        )

    def _database_row_to_record_locked(
        self,
        row: sqlite3.Row,
        *,
        include_log_lines: bool = True,
    ) -> NodeOperationRecord:
        database = self._require_database_locked()
        operation_id = cast(str, row["operation_id"])
        log_lines: tuple[str, ...] = ()
        if include_log_lines:
            log_rows = database.execute(
                """
                SELECT line FROM node_operation_logs
                WHERE operation_id = ?
                ORDER BY sequence ASC
                """,
                (operation_id,),
            ).fetchall()
            log_lines = tuple(cast(str, log_row["line"]) for log_row in log_rows)
        try:
            kind = NodeOperationKind(cast(str, row["kind"]))
            state = NodeOperationState(cast(str, row["state"]))
        except ValueError as xcp:
            raise RuntimeError(
                f"Persisted operation {operation_id} has an invalid lifecycle value."
            ) from xcp
        return NodeOperationRecord(
            operation_id=operation_id,
            kind=kind,
            node_name=cast(str, row["node_name"]),
            subject=cast(str, row["subject"]),
            state=state,
            summary=cast(str, row["summary"]),
            requested_by_user_id=_database_optional_int(row["requested_by_user_id"]),
            app_name=_database_optional_text(row["app_name"]),
            phase=_database_optional_text(row["phase"]),
            result_reference=_database_optional_text(row["result_reference"]),
            detail=_database_optional_text(row["detail"]),
            progress_percent=_database_optional_float(row["progress_percent"]),
            log_lines=log_lines,
            created_at_unix_ms=_database_required_int(row["created_at_unix_ms"]),
            started_at_unix_ms=_database_optional_int(row["started_at_unix_ms"]),
            finished_at_unix_ms=_database_optional_int(row["finished_at_unix_ms"]),
        )

    def _write_record_locked(
        self,
        record: NodeOperationRecord,
        *,
        log_line: str | None = None,
    ) -> None:
        validated_log_line = _optional_text(log_line, label="Operation log line")
        if self._database is None:
            current = self._record_locked(record.operation_id)
            if current.operation_id != record.operation_id:
                raise RuntimeError("Operation record identity changed unexpectedly.")
            if validated_log_line is not None:
                record = replace(
                    record,
                    log_lines=(*record.log_lines, validated_log_line)[
                        -_OPERATION_LOG_LINE_LIMIT:
                    ],
                )
            self._records[record.operation_id] = record
            return

        database = self._require_database_locked()
        with self._database_transaction_locked():
            result = database.execute(
                """
                UPDATE node_operations
                SET state = ?, summary = ?, phase = ?, result_reference = ?, detail = ?,
                    progress_percent = ?, started_at_unix_ms = ?, finished_at_unix_ms = ?
                WHERE operation_id = ?
                """,
                (
                    record.state.value,
                    record.summary,
                    record.phase,
                    record.result_reference,
                    record.detail,
                    record.progress_percent,
                    record.started_at_unix_ms,
                    record.finished_at_unix_ms,
                    record.operation_id,
                ),
            )
            if result.rowcount != 1:
                raise LookupError("Operation was not found.")
            if validated_log_line is not None:
                self._append_database_log_locked(
                    record.operation_id, validated_log_line
                )

    def _finish_record_locked(
        self,
        record: NodeOperationRecord,
        *,
        operation_result: NodeOperationResult | None = None,
    ) -> tuple[NodeOperationRecord, ...]:
        if not record.state.terminal:
            raise ValueError("Only terminal records can be finished.")
        if self._database is None:
            self._records[record.operation_id] = record
            if operation_result is not None:
                self._results[record.operation_id] = operation_result
            self._release_memory_resources_locked(record.operation_id)
            return self._prune_memory_completed_locked()

        database = self._require_database_locked()
        with self._database_transaction_locked():
            write_result = database.execute(
                """
                UPDATE node_operations
                SET state = ?, summary = ?, phase = ?, result_reference = ?, detail = ?,
                    progress_percent = ?, started_at_unix_ms = ?, finished_at_unix_ms = ?
                WHERE operation_id = ?
                """,
                (
                    record.state.value,
                    record.summary,
                    record.phase,
                    record.result_reference,
                    record.detail,
                    record.progress_percent,
                    record.started_at_unix_ms,
                    record.finished_at_unix_ms,
                    record.operation_id,
                ),
            )
            if write_result.rowcount != 1:
                raise LookupError("Operation was not found.")
            if operation_result is not None:
                database.execute(
                    """
                    INSERT INTO node_operation_results (
                        operation_id, payload_json, expires_at_unix_ms
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        expires_at_unix_ms = excluded.expires_at_unix_ms
                    """,
                    (
                        record.operation_id,
                        operation_result.payload_json,
                        operation_result.expires_at_unix_ms,
                    ),
                )
            database.execute(
                "DELETE FROM node_operation_resources WHERE operation_id = ?",
                (record.operation_id,),
            )
            removed_records = self._prune_database_completed_locked()
        return removed_records

    def _append_database_log_locked(self, operation_id: str, line: str) -> None:
        database = self._require_database_locked()
        row = database.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS maximum FROM node_operation_logs WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Unable to allocate an operation log sequence.")
        sequence = _database_required_int(row["maximum"]) + 1
        database.execute(
            "INSERT INTO node_operation_logs (operation_id, sequence, line) VALUES (?, ?, ?)",
            (operation_id, sequence, line),
        )
        oldest_retained_sequence = sequence - _OPERATION_LOG_LINE_LIMIT
        if oldest_retained_sequence > 0:
            database.execute(
                "DELETE FROM node_operation_logs WHERE operation_id = ? AND sequence <= ?",
                (operation_id, oldest_retained_sequence),
            )

    def _result_for_finish(
        self,
        *,
        state: NodeOperationState,
        result_payload_json: str | None,
        result_ttl_seconds: int | None,
    ) -> NodeOperationResult | None:
        if result_payload_json is None:
            if result_ttl_seconds is not None:
                raise ValueError(
                    "Operation result retention requires an operation result payload."
                )
            return None
        if state is not NodeOperationState.SUCCEEDED:
            raise ValueError("Only successful operations can retain a result artifact.")
        if result_ttl_seconds is not None:
            if (
                isinstance(result_ttl_seconds, bool)
                or not isinstance(result_ttl_seconds, int)
                or result_ttl_seconds < 1
            ):
                raise ValueError(
                    "Operation result retention must be a positive number of seconds."
                )
        expires_at_unix_ms = (
            None
            if result_ttl_seconds is None
            else self._now_unix_ms() + (result_ttl_seconds * 1000)
        )
        return NodeOperationResult(
            payload_json=result_payload_json,
            expires_at_unix_ms=expires_at_unix_ms,
        )

    def _result_locked(self, operation_id: str) -> NodeOperationResult | None:
        if self._database is None:
            return self._results.get(operation_id)
        database = self._require_database_locked()
        row = database.execute(
            """
            SELECT payload_json, expires_at_unix_ms
            FROM node_operation_results
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return NodeOperationResult(
                payload_json=_database_required_text(row["payload_json"]),
                expires_at_unix_ms=_database_optional_int(row["expires_at_unix_ms"]),
            )
        except ValueError as xcp:
            raise RuntimeError(
                f"Persisted operation {operation_id} has an invalid result artifact."
            ) from xcp

    def _delete_result_locked(self, operation_id: str) -> None:
        if self._database is None:
            self._results.pop(operation_id, None)
            return
        database = self._require_database_locked()
        with self._database_transaction_locked():
            database.execute(
                "DELETE FROM node_operation_results WHERE operation_id = ?",
                (operation_id,),
            )

    def _release_memory_resources_locked(self, operation_id: str) -> None:
        released_keys = tuple(
            resource_key
            for resource_key, owner_operation_id in self._resource_owners.items()
            if owner_operation_id == operation_id
        )
        for resource_key in released_keys:
            self._resource_owners.pop(resource_key, None)

    def _prune_memory_completed_locked(self) -> tuple[NodeOperationRecord, ...]:
        now_unix_ms = self._now_unix_ms()
        expired_result_ids = tuple(
            operation_id
            for operation_id, result in self._results.items()
            if (
                result.expires_at_unix_ms is not None
                and result.expires_at_unix_ms <= now_unix_ms
            )
        )
        for operation_id in expired_result_ids:
            self._results.pop(operation_id, None)
        completed = sorted(
            (
                (position, record)
                for position, record in enumerate(self._records.values())
                if record.state.terminal
            ),
            key=lambda item: (
                item[1].finished_at_unix_ms
                if item[1].finished_at_unix_ms is not None
                else 0,
                item[0],
            ),
        )
        removed_records: list[NodeOperationRecord] = []
        for _position, record in completed[
            : max(0, len(completed) - self._completed_history_limit)
        ]:
            self._records.pop(record.operation_id, None)
            self._results.pop(record.operation_id, None)
            removed_records.append(record)
        return tuple(removed_records)

    def _prune_database_completed_locked(self) -> tuple[NodeOperationRecord, ...]:
        database = self._require_database_locked()
        database.execute(
            """
            DELETE FROM node_operation_results
            WHERE expires_at_unix_ms IS NOT NULL AND expires_at_unix_ms <= ?
            """,
            (self._now_unix_ms(),),
        )
        terminal_values = tuple(
            state.value for state in NodeOperationState if state.terminal
        )
        placeholders = ", ".join("?" for _ in terminal_values)
        rows = database.execute(
            f"""
            SELECT operation_id FROM node_operations
            WHERE state IN ({placeholders})
            ORDER BY finished_at_unix_ms ASC, rowid ASC
            """,
            terminal_values,
        ).fetchall()
        excess = len(rows) - self._completed_history_limit
        if excess <= 0:
            return ()
        operation_ids = tuple(cast(str, row["operation_id"]) for row in rows[:excess])
        removed_records = tuple(
            self._record_locked(operation_id, include_log_lines=True)
            for operation_id in operation_ids
        )
        delete_placeholders = ", ".join("?" for _ in operation_ids)
        database.execute(
            f"DELETE FROM node_operations WHERE operation_id IN ({delete_placeholders})",
            operation_ids,
        )
        return removed_records

    @contextmanager
    def _database_transaction_locked(self) -> Generator[None, None, None]:
        database = self._require_database_locked()
        database.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            database.execute("ROLLBACK")
            raise
        else:
            database.execute("COMMIT")

    def _require_database_locked(self) -> sqlite3.Connection:
        database = self._database
        if database is None:
            raise RuntimeError("Operation database is not configured.")
        return database


def _record_values(record: NodeOperationRecord) -> tuple[object, ...]:
    return (
        record.operation_id,
        record.kind.value,
        record.node_name,
        record.subject,
        record.requested_by_user_id,
        record.state.value,
        record.summary,
        record.phase,
        record.result_reference,
        record.detail,
        record.progress_percent,
        record.created_at_unix_ms,
        record.started_at_unix_ms,
        record.finished_at_unix_ms,
        record.app_name,
    )


def _normalise_resource_keys(resource_keys: Sequence[str]) -> tuple[str, ...]:
    normalised: list[str] = []
    seen: set[str] = set()
    for raw_key in resource_keys:
        key = _required_text(raw_key, label="Operation resource key")
        if key not in seen:
            normalised.append(key)
            seen.add(key)
    return tuple(normalised)


def _required_text(value: str, *, label: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must not be blank.")
    return text


def _optional_text(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must not be blank when provided.")
    return text


def _mapping_required_text(
    payload: Mapping[str, object],
    key: str,
    *,
    label: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid.")
    return _required_text(value, label=label)


def _mapping_optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid.")
    return _optional_text(value, label=label)


def _mapping_required_int(
    payload: Mapping[str, object],
    key: str,
    *,
    label: str,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is invalid.")
    return value


def _mapping_optional_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is invalid.")
    return value


def _mapping_optional_progress(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Operation progress is invalid.")
    return float(value)


def _mapping_log_lines(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError("Operation log lines are invalid.")
    lines: list[str] = []
    for line in value:
        if not isinstance(line, str):
            raise ValueError("Operation log lines are invalid.")
        lines.append(line)
    return tuple(lines)


def _database_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Persisted operation text field is invalid.")
    return value


def _database_required_text(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Persisted operation text field is invalid.")
    return value


def _database_required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("Persisted operation integer field is invalid.")
    return value


def _database_optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _database_required_int(value)


def _database_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError("Persisted operation progress field is invalid.")
    return float(value)


def _unix_ms_now() -> int:
    return int(time.time() * 1000)


__all__: tuple[str, ...] = (
    "NodeOperationChange",
    "NodeOperationChangeKind",
    "NodeOperationKind",
    "NodeOperationProgress",
    "NodeOperationRecord",
    "NodeOperationResult",
    "NodeOperationResourceConflict",
    "NodeOperationSchemaVersionError",
    "NodeOperationService",
    "NodeOperationState",
)
