"""Durable operation lifecycle for app update and verification work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from _security import Access_Control, Power_Level
from apps._app import App
from apps._updater import (
    AppUpdateOperationKind,
    AppUpdateOperationResult,
    AppUpdateState,
    AppUpdateStatus,
    Update_Manager,
)
from .operations import (
    NodeOperationKind,
    NodeOperationProgress,
    NodeOperationRecord,
    NodeOperationResourceConflict,
    NodeOperationService,
    NodeOperationState,
)
from .route_contracts import HttpExceptionFactory


_UPDATE_PROGRESS_POLL_SECONDS = 0.25

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AppUpdateOperationSpec:
    """The durable presentation for one updater-domain operation kind."""

    updater_kind: AppUpdateOperationKind
    operation_kind: NodeOperationKind
    label: str

    @property
    def phase(self) -> str:
        return self.updater_kind.value


@dataclass(slots=True)
class _AppUpdateProgressCursor:
    """The updater snapshot already examined for progress and log deltas."""

    status: AppUpdateStatus | None = None
    log_lines: tuple[str, ...] = ()


_APP_UPDATE_OPERATION_SPECS: tuple[_AppUpdateOperationSpec, ...] = (
    _AppUpdateOperationSpec(
        updater_kind=AppUpdateOperationKind.UPDATE,
        operation_kind=NodeOperationKind.APP_UPDATE,
        label="Update",
    ),
    _AppUpdateOperationSpec(
        updater_kind=AppUpdateOperationKind.VERIFY,
        operation_kind=NodeOperationKind.APP_VERIFY,
        label="Verify",
    ),
)


class _AppUpdateCancellationRequested(Exception):
    """Raised before updater work begins when an operation was cancelled."""


class NodeAppUpdateOperationService:
    """Runs updater domain calls inside durable, app-scoped operations.

    Updaters retain responsibility for selecting a branch, invoking providers,
    and producing their domain result/status. This service only projects that
    work into operation lifecycle, retained progress, and history.
    """

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        require_acl: Callable[[], Access_Control],
        http_exception: HttpExceptionFactory,
        invalidate_state_caches: Callable[[str], None],
        operations: NodeOperationService,
        progress_poll_seconds: float = _UPDATE_PROGRESS_POLL_SECONDS,
    ) -> None:
        if progress_poll_seconds <= 0:
            raise ValueError("App update operation progress poll interval must be positive.")
        self._node_name = node_name
        self._require_acl = require_acl
        self._http_exception = http_exception
        self._invalidate_state_caches = invalidate_state_caches
        self._operations = operations
        self._progress_poll_seconds = progress_poll_seconds

    async def start(
        self,
        *,
        app: App,
        updater_kind: AppUpdateOperationKind,
        actor_user_id: int,
        availability_check: Callable[[], None] | None = None,
    ) -> NodeOperationRecord:
        """Create and begin observing one app update or verification operation.

        ``availability_check`` runs after authorisation and immediately before
        the resource reservation. It lets the caller close races with
        app-lifecycle work that is owned outside this service.
        """

        spec = self._spec_for_updater_kind(updater_kind)
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        if availability_check is not None:
            availability_check()
        self._require_updater(app, spec=spec)
        if app.check_running():
            raise self._http_exception(
                409,
                f"{app.friendly} must be stopped before {spec.phase}.",
            )
        try:
            operation = self._operations.create(
                kind=spec.operation_kind,
                node_name=self._node_name(),
                subject=app.friendly,
                requested_by_user_id=actor_user_id,
                app_name=app.name,
                progress=NodeOperationProgress(
                    summary=f"{spec.label} queued.",
                    phase="queued",
                    detail=self._selected_branch_detail(app),
                    progress_percent=0.0,
                ),
                resource_keys=(self.app_update_resource_key(app.name),),
            )
        except NodeOperationResourceConflict as xcp:
            raise self._http_exception(
                409,
                f"Another update or verification is already running for {app.friendly}.",
            ) from xcp

        task: asyncio.Task[None] | None = None
        try:
            task = asyncio.create_task(
                self._run_operation(
                    operation_id=operation.operation_id,
                    app=app,
                    spec=spec,
                ),
                name=f"app-{spec.phase}-{operation.operation_id}",
            )
            self._operations.track_task(
                operation_id=operation.operation_id,
                task=task,
            )
        except Exception as xcp:
            if task is not None:
                task.cancel()
            self._operations.finish(
                operation_id=operation.operation_id,
                state=NodeOperationState.FAILED,
                summary=f"{spec.label} could not be started.",
                detail=type(xcp).__name__,
            )
            raise
        return operation

    async def cancel(
        self,
        *,
        operation_id: str,
        actor_user_id: int,
    ) -> None:
        """Request cancellation without interrupting an updater mid-mutation."""

        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        try:
            operation = self._operations.get(operation_id, include_log_lines=False)
        except LookupError as xcp:
            raise LookupError("App update operation was not found.") from xcp
        self._spec_for_operation_kind(operation.kind)
        self._operations.request_cancellation(operation_id=operation.operation_id)

    def cancel_pending(self) -> None:
        """Request cooperative shutdown of app update operations."""

        self._operations.cancel_pending(kind=NodeOperationKind.APP_UPDATE)
        self._operations.cancel_pending(kind=NodeOperationKind.APP_VERIFY)

    def ensure_branch_selection_available(self, app: App) -> None:
        """Keep a selected branch stable from operation creation through completion."""

        self.ensure_no_active_update_operation(
            app,
            message=(
                f"Cannot change the update branch while {app.friendly} is "
                "updating or verifying."
            ),
        )

    def ensure_update_configuration_available(self, app: App) -> None:
        """Keep provider settings stable from reservation through completion."""

        self.ensure_no_active_update_operation(
            app,
            message=(
                f"Cannot change update settings while {app.friendly} is "
                "updating or verifying."
            ),
        )

    def has_active_update_operation(self, app: App) -> bool:
        """Return whether update or verify currently reserves this app."""

        return self._has_active_update_operation(app)

    def ensure_no_active_update_operation(self, app: App, *, message: str) -> None:
        """Raise a conflict when app work would overlap update or verification."""

        if self._has_active_update_operation(app):
            raise self._http_exception(409, message)

    def _has_active_update_operation(self, app: App) -> bool:
        for operation_kind in (
            NodeOperationKind.APP_UPDATE,
            NodeOperationKind.APP_VERIFY,
        ):
            active_operations = self._operations.list_records(
                kind=operation_kind,
                app_name=app.name,
                include_log_lines=False,
            )
            if any(operation.state.active for operation in active_operations):
                return True
        return False

    @staticmethod
    def app_update_resource_key(app_name: str) -> str:
        """Return the shared resource held by both update and verify work."""

        normalised_name = app_name.strip()
        if not normalised_name:
            raise ValueError("App update resource app name must not be blank.")
        return f"app-update:{normalised_name.casefold()}"

    async def _run_operation(
        self,
        *,
        operation_id: str,
        app: App,
        spec: _AppUpdateOperationSpec,
    ) -> None:
        updater: Update_Manager | None = None
        progress_cursor = _AppUpdateProgressCursor()
        try:
            updater = self._require_updater(app, spec=spec)
            operation = self._operations.begin(
                operation_id=operation_id,
                progress=NodeOperationProgress(
                    summary=f"Starting {spec.phase}.",
                    phase=spec.phase,
                    detail=self._selected_branch_detail(app),
                    progress_percent=0.0,
                ),
            )
            if operation.state is not NodeOperationState.RUNNING:
                if operation.state is NodeOperationState.CANCEL_REQUESTED:
                    self._finish_cancelled_before_start(
                        operation_id=operation_id,
                        spec=spec,
                    )
                return
            self._raise_if_cancellation_requested(operation_id=operation_id)
            result, updater_status = await self._run_updater_with_progress(
                operation_id=operation_id,
                updater=updater,
                spec=spec,
                progress_cursor=progress_cursor,
            )
            self._finish_success(
                operation_id=operation_id,
                spec=spec,
                result=result,
                updater_status=updater_status,
            )
        except _AppUpdateCancellationRequested:
            self._finish_cancelled_before_start(
                operation_id=operation_id,
                spec=spec,
            )
        except asyncio.CancelledError:
            self._operations.finish(
                operation_id=operation_id,
                state=NodeOperationState.INTERRUPTED,
                summary=f"{spec.label} interrupted.",
                detail="The updater reached a safe boundary before the node stopped.",
            )
            raise
        except Exception as xcp:
            updater_status: AppUpdateStatus | None = None
            if updater is not None:
                try:
                    updater_status, _ = self._sync_updater_status(
                        operation_id=operation_id,
                        updater=updater,
                        spec=spec,
                        previous_status=progress_cursor.status,
                        previous_log_lines=progress_cursor.log_lines,
                    )
                except Exception as status_xcp:
                    log.warning(
                        "Could not collect updater status while failing operation: "
                        "node=%s app=%s operation=%s error_type=%s",
                        self._node_name(),
                        app.name,
                        operation_id,
                        type(status_xcp).__name__,
                    )
            self._operations.finish(
                operation_id=operation_id,
                state=NodeOperationState.FAILED,
                summary=f"{spec.label} failed for {app.friendly}.",
                detail=self._failure_detail(updater_status=updater_status, error=xcp),
            )
            log.warning(
                "App update operation failed: node=%s app=%s operation=%s kind=%s error_type=%s",
                self._node_name(),
                app.name,
                operation_id,
                spec.operation_kind.value,
                type(xcp).__name__,
            )
        finally:
            self._invalidate_app_state(app.name)

    async def _run_updater_with_progress(
        self,
        *,
        operation_id: str,
        updater: Update_Manager,
        spec: _AppUpdateOperationSpec,
        progress_cursor: _AppUpdateProgressCursor,
    ) -> tuple[AppUpdateOperationResult, AppUpdateStatus | None]:
        previous_status = self._read_updater_status(updater)
        progress_cursor.status = previous_status
        progress_cursor.log_lines = (
            () if previous_status is None else previous_status.log_lines
        )
        updater_task: asyncio.Task[AppUpdateOperationResult] = asyncio.create_task(
            self._invoke_updater(
                operation_id=operation_id,
                updater=updater,
                spec=spec,
            ),
            name=f"updater-{spec.phase}-{operation_id}",
        )
        try:
            while not updater_task.done():
                progress_cursor.status, progress_cursor.log_lines = self._sync_updater_status(
                    operation_id=operation_id,
                    updater=updater,
                    spec=spec,
                    previous_status=progress_cursor.status,
                    previous_log_lines=progress_cursor.log_lines,
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(updater_task),
                        timeout=self._progress_poll_seconds,
                    )
                except asyncio.TimeoutError:
                    continue
            result = await updater_task
        except asyncio.CancelledError:
            await self._wait_for_updater_safe_boundary(updater_task)
            raise
        except Exception:
            await self._wait_for_updater_safe_boundary(updater_task)
            raise
        progress_cursor.status, progress_cursor.log_lines = self._sync_updater_status(
            operation_id=operation_id,
            updater=updater,
            spec=spec,
            previous_status=progress_cursor.status,
            previous_log_lines=progress_cursor.log_lines,
        )
        return result, progress_cursor.status

    @staticmethod
    async def _wait_for_updater_safe_boundary(
        updater_task: asyncio.Task[AppUpdateOperationResult],
    ) -> None:
        """Do not release the operation resource while invoked updater work remains."""

        while not updater_task.done():
            try:
                await asyncio.shield(updater_task)
            except asyncio.CancelledError:
                if updater_task.done():
                    break
            except Exception:
                break
        if updater_task.done() and not updater_task.cancelled():
            updater_task.exception()

    async def _invoke_updater(
        self,
        *,
        operation_id: str,
        updater: Update_Manager,
        spec: _AppUpdateOperationSpec,
    ) -> AppUpdateOperationResult:
        self._raise_if_cancellation_requested(operation_id=operation_id)
        if spec.updater_kind is AppUpdateOperationKind.UPDATE:
            return await updater.update_selected()
        if spec.updater_kind is AppUpdateOperationKind.VERIFY:
            return await updater.verify_selected()
        raise RuntimeError(f"Unsupported updater operation kind: {spec.updater_kind.value}")

    @staticmethod
    def _read_updater_status(updater: Update_Manager) -> AppUpdateStatus | None:
        status = updater.status()
        if status is None:
            return None
        if not isinstance(status, AppUpdateStatus):
            raise TypeError("App updater status must be AppUpdateStatus or None.")
        return status

    def _sync_updater_status(
        self,
        *,
        operation_id: str,
        updater: Update_Manager,
        spec: _AppUpdateOperationSpec,
        previous_status: AppUpdateStatus | None,
        previous_log_lines: tuple[str, ...],
    ) -> tuple[AppUpdateStatus | None, tuple[str, ...]]:
        status = self._read_updater_status(updater)
        if status is None:
            return previous_status, previous_log_lines
        current_log_lines = status.log_lines
        appended_log_lines = tuple(
            log_line
            for log_line in self._appended_log_lines(
                previous_log_lines=previous_log_lines,
                current_log_lines=current_log_lines,
            )
            if log_line.strip()
        )
        if status == previous_status and not appended_log_lines:
            return status, current_log_lines
        try:
            operation = self._operations.get(operation_id, include_log_lines=False)
        except LookupError:
            return status, current_log_lines
        if not operation.state.active:
            return status, current_log_lines
        if operation.state is NodeOperationState.CANCEL_REQUESTED:
            self._append_log_lines(
                operation_id=operation_id,
                log_lines=appended_log_lines,
            )
            return status, current_log_lines
        if status.state is AppUpdateState.RUNNING:
            progress = NodeOperationProgress(
                summary=status.summary,
                phase=spec.phase,
                detail=status.detail,
                progress_percent=status.progress_percent,
            )
            if appended_log_lines:
                self._operations.update_active(
                    operation_id=operation_id,
                    progress=progress,
                    log_line=appended_log_lines[0],
                )
                self._append_log_lines(
                    operation_id=operation_id,
                    log_lines=appended_log_lines[1:],
                )
            else:
                self._operations.update_active(
                    operation_id=operation_id,
                    progress=progress,
                )
        else:
            self._append_log_lines(
                operation_id=operation_id,
                log_lines=appended_log_lines,
            )
        return status, current_log_lines

    def _finish_success(
        self,
        *,
        operation_id: str,
        spec: _AppUpdateOperationSpec,
        result: AppUpdateOperationResult,
        updater_status: AppUpdateStatus | None,
    ) -> None:
        if result.kind is not spec.updater_kind:
            raise RuntimeError(
                "Updater returned a result for a different operation kind."
            )
        detail = None if updater_status is None else updater_status.detail
        if self._operations.cancellation_requested(operation_id=operation_id):
            cancellation_detail = (
                "Cancellation was requested while the updater was active; "
                "it completed at its next safe boundary."
            )
            detail = (
                cancellation_detail
                if detail is None
                else f"{detail} {cancellation_detail}"
            )
        self._operations.finish(
            operation_id=operation_id,
            state=NodeOperationState.SUCCEEDED,
            summary=result.message,
            detail=detail,
            result_reference=(
                result.version_text.strip()
                if result.version_text is not None and result.version_text.strip()
                else None
            ),
            progress_percent=100.0,
        )

    def _finish_cancelled_before_start(
        self,
        *,
        operation_id: str,
        spec: _AppUpdateOperationSpec,
    ) -> None:
        self._operations.finish(
            operation_id=operation_id,
            state=NodeOperationState.CANCELLED,
            summary=f"{spec.label} stopped before it started.",
        )

    def _raise_if_cancellation_requested(self, *, operation_id: str) -> None:
        if self._operations.cancellation_requested(operation_id=operation_id):
            raise _AppUpdateCancellationRequested

    def _invalidate_app_state(self, app_name: str) -> None:
        try:
            self._invalidate_state_caches(app_name)
        except Exception:
            log.exception(
                "Could not invalidate app state after update operation: node=%s app=%s",
                self._node_name(),
                app_name,
            )

    @staticmethod
    def _appended_log_lines(
        *,
        previous_log_lines: tuple[str, ...],
        current_log_lines: tuple[str, ...],
    ) -> tuple[str, ...]:
        if current_log_lines[: len(previous_log_lines)] == previous_log_lines:
            return current_log_lines[len(previous_log_lines) :]
        maximum_overlap = min(len(previous_log_lines), len(current_log_lines))
        for overlap_length in range(maximum_overlap, 0, -1):
            if previous_log_lines[-overlap_length:] == current_log_lines[:overlap_length]:
                return current_log_lines[overlap_length:]
        return current_log_lines

    def _append_log_lines(
        self,
        *,
        operation_id: str,
        log_lines: tuple[str, ...],
    ) -> None:
        for log_line in log_lines:
            self._operations.append_log(
                operation_id=operation_id,
                log_line=log_line,
            )

    @staticmethod
    def _failure_detail(
        *,
        updater_status: AppUpdateStatus | None,
        error: Exception,
    ) -> str:
        if updater_status is not None and updater_status.detail is not None:
            return updater_status.detail
        return type(error).__name__

    @staticmethod
    def _require_updater(
        app: App,
        *,
        spec: _AppUpdateOperationSpec,
    ) -> Update_Manager:
        updater = app.updater
        if updater is None:
            capability = (
                "verification"
                if spec.updater_kind is AppUpdateOperationKind.VERIFY
                else "updates"
            )
            raise ValueError(f"{app.friendly} does not support {capability}.")
        return updater

    @staticmethod
    def _selected_branch_detail(app: App) -> str | None:
        update_info = app.update_info
        if update_info is None:
            return None
        return (
            f"Target branch: {update_info.selected_branch_label} "
            f"({update_info.selected_branch_id})."
        )

    @staticmethod
    def _spec_for_updater_kind(
        updater_kind: AppUpdateOperationKind,
    ) -> _AppUpdateOperationSpec:
        for spec in _APP_UPDATE_OPERATION_SPECS:
            if spec.updater_kind is updater_kind:
                return spec
        raise ValueError(f"Unsupported updater operation kind: {updater_kind.value}")

    @staticmethod
    def _spec_for_operation_kind(
        operation_kind: NodeOperationKind,
    ) -> _AppUpdateOperationSpec:
        for spec in _APP_UPDATE_OPERATION_SPECS:
            if spec.operation_kind is operation_kind:
                return spec
        raise LookupError("App update operation was not found.")


__all__: tuple[str, ...] = ("NodeAppUpdateOperationService",)
