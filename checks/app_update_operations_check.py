from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from fastapi import HTTPException

from _security import Access_Control, Power_Level
from apps._app import App
from apps._updater import (
    AppUpdateOperationKind,
    AppUpdateOperationResult,
    AppUpdateState,
    AppUpdateStatus,
)
from node_api.app_update_service import NodeAppUpdateOperationService
from node_api.operation_service import (
    NodeOperationApiService,
    NodeOperationKindPolicy,
    NodeOperationTargetScope,
)
from node_api.operations import (
    NodeOperationKind,
    NodeOperationService,
    NodeOperationState,
)
from node_auth import NodeApiScope


class _UpdateProbe:
    def __init__(
        self,
        *,
        initial_status: AppUpdateStatus | None = None,
        supports_verify: bool = True,
    ) -> None:
        self.started = asyncio.Event()
        self.complete = asyncio.Event()
        self.update_calls = 0
        self.verify_calls = 0
        self.supports_verify = supports_verify
        self._status = (
            AppUpdateStatus(state=AppUpdateState.IDLE, summary="Ready")
            if initial_status is None
            else initial_status
        )

    def status(self) -> AppUpdateStatus:
        return self._status

    async def update_selected(self) -> AppUpdateOperationResult:
        self.update_calls += 1
        self._status = AppUpdateStatus(
            state=AppUpdateState.RUNNING,
            summary="Downloading update.",
            operation_kind=AppUpdateOperationKind.UPDATE,
            progress_percent=50.0,
            detail="Downloading package 1 of 2.",
            log_lines=("stdout: Downloading package 1 of 2.",),
            log_cursor=1,
        )
        self.started.set()
        await self.complete.wait()
        self._status = AppUpdateStatus(
            state=AppUpdateState.SUCCEEDED,
            summary="Updated Minecraft Alpha.",
            operation_kind=AppUpdateOperationKind.UPDATE,
            progress_percent=100.0,
            log_lines=("stdout: Downloading package 1 of 2.", "stdout: Update complete."),
            log_cursor=2,
        )
        return AppUpdateOperationResult(
            kind=AppUpdateOperationKind.UPDATE,
            message="Updated Minecraft Alpha.",
            version_text="1.2.3",
        )

    async def verify_selected(self) -> AppUpdateOperationResult:
        self.verify_calls += 1
        return AppUpdateOperationResult(
            kind=AppUpdateOperationKind.VERIFY,
            message="Verified Minecraft Alpha.",
        )


class NodeAppUpdateOperationsCheck(unittest.TestCase):
    def test_update_and_verify_share_a_resource_and_retain_updater_progress(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            invalidated_app_names: list[str] = []
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=invalidated_app_names.append,
                operations=operations,
                progress_poll_seconds=0.001,
            )
            updater = _UpdateProbe()
            app = self._app(updater)

            update = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.UPDATE,
                actor_user_id=42,
            )
            await updater.started.wait()
            await asyncio.sleep(0.01)
            running = operations.get(update.operation_id)

            self.assertEqual(running.kind, NodeOperationKind.APP_UPDATE)
            self.assertEqual(running.app_name, app.name)
            self.assertEqual(running.state, NodeOperationState.RUNNING)
            self.assertEqual(running.progress_percent, 50.0)
            self.assertEqual(
                running.log_lines,
                ("stdout: Downloading package 1 of 2.",),
            )
            with self.assertRaises(HTTPException) as raised:
                await service.start(
                    app=app,
                    updater_kind=AppUpdateOperationKind.VERIFY,
                    actor_user_id=42,
                )
            self.assertEqual(raised.exception.status_code, 409)

            service.cancel_pending()
            self.assertEqual(
                operations.get(update.operation_id).state,
                NodeOperationState.CANCEL_REQUESTED,
            )
            self.assertFalse(updater.complete.is_set())
            updater.complete.set()
            await self._wait_for_terminal(operations, update.operation_id)
            completed = operations.get(update.operation_id)

            self.assertEqual(completed.state, NodeOperationState.SUCCEEDED)
            self.assertEqual(completed.summary, "Updated Minecraft Alpha.")
            self.assertIn("safe boundary", completed.detail or "")
            self.assertEqual(
                completed.log_lines,
                (
                    "stdout: Downloading package 1 of 2.",
                    "stdout: Update complete.",
                ),
            )
            self.assertEqual(completed.result_reference, "1.2.3")
            self.assertIn(app.name, invalidated_app_names)

            verify = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.VERIFY,
                actor_user_id=42,
            )
            await self._wait_for_terminal(operations, verify.operation_id)
            completed_verify = operations.get(verify.operation_id)
            self.assertEqual(completed_verify.kind, NodeOperationKind.APP_VERIFY)
            self.assertEqual(completed_verify.state, NodeOperationState.SUCCEEDED)
            self.assertEqual(updater.update_calls, 1)
            self.assertEqual(updater.verify_calls, 1)
            self.assertEqual(acl.perm_check.await_count, 3)

        asyncio.run(_run())

    def test_branch_selection_stays_reserved_while_cancellation_is_pending(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
            )
            updater = _UpdateProbe()
            app = self._app(updater)

            update = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.UPDATE,
                actor_user_id=42,
            )
            await updater.started.wait()
            service.cancel_pending()
            with self.assertRaises(HTTPException) as raised:
                service.ensure_branch_selection_available(app)
            self.assertEqual(raised.exception.status_code, 409)
            with self.assertRaises(HTTPException) as raised:
                service.ensure_update_configuration_available(app)
            self.assertEqual(raised.exception.status_code, 409)
            with self.assertRaises(HTTPException) as raised:
                service.ensure_no_active_update_operation(
                    app,
                    message="App lifecycle changes are unavailable.",
                )
            self.assertEqual(raised.exception.status_code, 409)

            updater.complete.set()
            await self._wait_for_terminal(operations, update.operation_id)
            service.ensure_branch_selection_available(app)
            service.ensure_update_configuration_available(app)
            service.ensure_no_active_update_operation(
                app,
                message="App lifecycle changes are unavailable.",
            )

        asyncio.run(_run())

    def test_start_rejects_an_app_that_is_already_running(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
            )
            updater = _UpdateProbe()
            app = self._app(updater, running=True)

            with self.assertRaises(HTTPException) as raised:
                await service.start(
                    app=app,
                    updater_kind=AppUpdateOperationKind.UPDATE,
                    actor_user_id=42,
                )

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(operations.list_records(), ())
            self.assertEqual(updater.update_calls, 0)

        asyncio.run(_run())

    def test_verify_without_capability_creates_no_durable_operation(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
            )
            updater = _UpdateProbe(supports_verify=False)
            app = self._app(updater)

            with self.assertRaisesRegex(ValueError, "does not support verification"):
                await service.start(
                    app=app,
                    updater_kind=AppUpdateOperationKind.VERIFY,
                    actor_user_id=42,
                )

            self.assertEqual(operations.list_records(), ())
            self.assertEqual(updater.verify_calls, 0)

        asyncio.run(_run())

    def test_queued_update_can_be_cancelled_before_updater_mutation(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
            )
            updater = _UpdateProbe()
            app = self._app(updater)

            update = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.UPDATE,
                actor_user_id=42,
            )
            await service.cancel(operation_id=update.operation_id, actor_user_id=42)
            self.assertEqual(
                operations.get(update.operation_id).state,
                NodeOperationState.CANCEL_REQUESTED,
            )

            await self._wait_for_terminal(operations, update.operation_id)
            self.assertEqual(
                operations.get(update.operation_id).state,
                NodeOperationState.CANCELLED,
            )
            self.assertEqual(updater.update_calls, 0)

        asyncio.run(_run())

    def test_running_updater_operation_is_not_cancellable(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
                progress_poll_seconds=0.001,
            )
            updater = _UpdateProbe()
            app = self._app(updater)

            async def _cancel(operation_id: str, actor_user_id: int) -> None:
                await service.cancel(
                    operation_id=operation_id,
                    actor_user_id=actor_user_id,
                )

            operation_api = NodeOperationApiService(
                operations=operations,
                policies=(
                    NodeOperationKindPolicy(
                        kind=NodeOperationKind.APP_UPDATE,
                        kind_label="App update",
                        read_scope=NodeApiScope.APP_MANAGE,
                        cancel_scope=NodeApiScope.APP_MANAGE,
                        required_level=Power_Level.sudo,
                        target_scope=NodeOperationTargetScope.APP,
                        cancellable_states=frozenset({NodeOperationState.QUEUED}),
                        cancellation_handler=_cancel,
                    ),
                ),
            )
            update = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.UPDATE,
                actor_user_id=42,
            )
            await updater.started.wait()

            running = operation_api.get_operation(
                operation_id=update.operation_id,
                kind=NodeOperationKind.APP_UPDATE,
                app_name=app.name,
            )
            self.assertFalse(running.cancellable)
            with self.assertRaisesRegex(ValueError, "not in a cancellable state"):
                await operation_api.cancel_operation(
                    operation_id=update.operation_id,
                    actor_user_id=42,
                    kind=NodeOperationKind.APP_UPDATE,
                    app_name=app.name,
                )
            self.assertEqual(
                operations.get(update.operation_id).state,
                NodeOperationState.RUNNING,
            )
            self.assertFalse(updater.complete.is_set())

            updater.complete.set()
            await self._wait_for_terminal(operations, update.operation_id)

        asyncio.run(_run())

    def test_external_task_cancellation_waits_for_the_updater_safe_boundary(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
                progress_poll_seconds=0.001,
            )
            updater = _UpdateProbe()
            app = self._app(updater)

            update = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.UPDATE,
                actor_user_id=42,
            )
            await updater.started.wait()

            # This is not an update cancellation path. It models an external
            # task interruption after updater work has already begun.
            operations.hard_cancel(operation_id=update.operation_id)
            await asyncio.sleep(0)
            self.assertEqual(
                operations.get(update.operation_id).state,
                NodeOperationState.CANCEL_REQUESTED,
            )
            with self.assertRaises(HTTPException) as raised:
                await service.start(
                    app=app,
                    updater_kind=AppUpdateOperationKind.VERIFY,
                    actor_user_id=42,
                )
            self.assertEqual(raised.exception.status_code, 409)

            updater.complete.set()
            await self._wait_for_terminal(operations, update.operation_id)
            self.assertEqual(
                operations.get(update.operation_id).state,
                NodeOperationState.INTERRUPTED,
            )

            verify = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.VERIFY,
                actor_user_id=42,
            )
            await self._wait_for_terminal(operations, verify.operation_id)
            self.assertEqual(
                operations.get(verify.operation_id).state,
                NodeOperationState.SUCCEEDED,
            )

        asyncio.run(_run())

    def test_appended_log_lines_uses_the_cursor_across_tail_rollover(self) -> None:
        appended = NodeAppUpdateOperationService._appended_log_lines(
            previous_log_cursor=3,
            status=AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary="Downloading.",
                log_lines=("line 2", "line 3", "line 4"),
                log_cursor=4,
            ),
        )

        self.assertEqual(appended, ("line 4",))

    def test_appended_log_lines_preserves_repeated_lines_with_a_cursor(self) -> None:
        appended = NodeAppUpdateOperationService._appended_log_lines(
            previous_log_cursor=2,
            status=AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary="Downloading.",
                log_lines=("stdout: retry", "stdout: retry", "stdout: retry", "stdout: retry"),
                log_cursor=4,
            ),
        )

        self.assertEqual(appended, ("stdout: retry", "stdout: retry"))

    def test_update_history_excludes_the_updater_previous_operation_log_tail(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            acl = SimpleNamespace(perm_check=AsyncMock())
            service = NodeAppUpdateOperationService(
                node_name=lambda: "node-a",
                require_acl=lambda: cast(Access_Control, cast(object, acl)),
                http_exception=lambda status, detail: HTTPException(status, detail),
                invalidate_state_caches=lambda _app_name: None,
                operations=operations,
                progress_poll_seconds=0.001,
            )
            updater = _UpdateProbe(
                initial_status=AppUpdateStatus(
                    state=AppUpdateState.SUCCEEDED,
                    summary="Earlier update completed.",
                    operation_kind=AppUpdateOperationKind.UPDATE,
                    log_lines=("stdout: Earlier update complete.",),
                )
            )
            app = self._app(updater)

            update = await service.start(
                app=app,
                updater_kind=AppUpdateOperationKind.UPDATE,
                actor_user_id=42,
            )
            await updater.started.wait()
            await asyncio.sleep(0.01)

            self.assertEqual(
                operations.get(update.operation_id).log_lines,
                ("stdout: Downloading package 1 of 2.",),
            )

            updater.complete.set()
            await self._wait_for_terminal(operations, update.operation_id)
            self.assertEqual(
                operations.get(update.operation_id).log_lines,
                (
                    "stdout: Downloading package 1 of 2.",
                    "stdout: Update complete.",
                ),
            )

        asyncio.run(_run())

    @staticmethod
    async def _wait_for_terminal(
        operations: NodeOperationService,
        operation_id: str,
    ) -> None:
        for _ in range(100):
            if operations.get(operation_id).state.terminal:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("App update operation did not finish.")

    @staticmethod
    def _app(updater: _UpdateProbe, *, running: bool = False) -> App:
        return cast(
            App,
            cast(
                object,
                SimpleNamespace(
                    name="minecraft_alpha",
                    friendly="Minecraft Alpha",
                    updater=updater,
                    update_info=None,
                    check_running=lambda: running,
                ),
            ),
        )
