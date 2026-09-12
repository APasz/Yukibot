from __future__ import annotations

import asyncio
import logging
import sqlite3
import unittest
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient, Response

from _security import Power_Level
from node_api.operation_service import NodeOperationApiService, NodeOperationKindPolicy, NodeOperationView
from node_api.operations import (
    NodeOperationKind,
    NodeOperationProgress,
    NodeOperationRecord,
    NodeOperationResourceConflict,
    NodeOperationSchemaVersionError,
    NodeOperationService,
    NodeOperationState,
)
from node_api.operations_routes import register_operation_routes
from node_api.request_auth import NodeRequestContext
from node_auth import NodeApiScope


class _RouteAuth:
    node_name = "node-a"

    def __init__(self) -> None:
        self.access_requests: list[tuple[str | None, tuple[NodeApiScope, ...]]] = []

    def require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeRequestContext:
        del request, access_token
        self.access_requests.append((app_name, scopes))
        return NodeRequestContext(grant=None, actor_user_id=42)

    @staticmethod
    def require_actor(context: NodeRequestContext) -> NodeRequestContext:
        return context


class NodeOperationsCheck(unittest.TestCase):
    def test_operation_record_round_trips_through_api_mapping(self) -> None:
        record = NodeOperationRecord(
            operation_id="operation-1",
            kind=NodeOperationKind.APP_INSTALL,
            node_name="node-a",
            subject="demo",
            state=NodeOperationState.SUCCEEDED,
            summary="Installed.",
            requested_by_user_id=42,
            result_reference="demo_alpha",
            detail="Loaded successfully.",
            progress_percent=100.0,
            log_lines=("stdout: Installed.",),
            created_at_unix_ms=100,
            started_at_unix_ms=110,
            finished_at_unix_ms=120,
        )

        self.assertEqual(NodeOperationRecord.from_mapping(record.to_mapping()), record)

    def test_persisted_operation_retains_sanitised_progress_and_history(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            operations = NodeOperationService(database_path=database_path)
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )
            operations.begin(
                operation_id=operation.operation_id,
                progress=NodeOperationProgress(
                    summary="Installing.",
                    phase="installing",
                    progress_percent=12.5,
                ),
            )
            operations.update_active(
                operation_id=operation.operation_id,
                progress=NodeOperationProgress(
                    summary="Downloading.",
                    phase="installing",
                    detail="Downloading content.",
                    progress_percent=50.0,
                ),
                log_line="stdout: Downloading content.",
            )
            operations.finish(
                operation_id=operation.operation_id,
                state=NodeOperationState.SUCCEEDED,
                summary="Installed.",
                result_reference="demo_alpha",
                progress_percent=100.0,
            )

            reopened = NodeOperationService(database_path=database_path)
            record = reopened.get(operation.operation_id)

        self.assertEqual(record.state, NodeOperationState.SUCCEEDED)
        self.assertEqual(record.result_reference, "demo_alpha")
        self.assertEqual(record.log_lines, ("stdout: Downloading content.",))
        self.assertEqual(record.progress_percent, 100.0)

    def test_list_records_can_omit_log_lines_for_operation_summaries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            operations = NodeOperationService(
                database_path=Path(temp_dir) / "operations.sqlite3"
            )
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )
            operations.begin(
                operation_id=operation.operation_id,
                progress=NodeOperationProgress(summary="Installing."),
            )
            operations.update_active(
                operation_id=operation.operation_id,
                progress=NodeOperationProgress(summary="Downloading."),
                log_line="stdout: Downloading content.",
            )

            summary = operations.list_records(include_log_lines=False)
            detail = operations.get(operation.operation_id)

        self.assertEqual(summary[0].log_lines, ())
        self.assertEqual(detail.log_lines, ("stdout: Downloading content.",))

    def test_operation_routes_expose_registered_records_and_safe_cancellation(self) -> None:
        operations = NodeOperationService()
        operation = operations.create(
            kind=NodeOperationKind.APP_INSTALL,
            node_name="node-a",
            subject="demo",
            requested_by_user_id=42,
            progress=NodeOperationProgress(summary="Queued."),
        )
        operations.begin(
            operation_id=operation.operation_id,
            progress=NodeOperationProgress(summary="Installing."),
        )
        operations.update_active(
            operation_id=operation.operation_id,
            progress=NodeOperationProgress(summary="Downloading."),
            log_line="stdout: Downloading content.",
        )
        cancel_requests: list[tuple[str, int]] = []

        async def _cancel(operation_id: str, actor_user_id: int) -> None:
            cancel_requests.append((operation_id, actor_user_id))
            operations.request_cancellation(operation_id=operation_id)

        operation_api = NodeOperationApiService(
            operations=operations,
            policies=(
                NodeOperationKindPolicy(
                    kind=NodeOperationKind.APP_INSTALL,
                    kind_label="App install",
                    read_scope=NodeApiScope.APP_MANAGE,
                    cancel_scope=NodeApiScope.APP_MANAGE,
                    required_level=Power_Level.sudo,
                    cancellation_handler=_cancel,
                ),
            ),
        )
        app = FastAPI()
        auth = _RouteAuth()
        register_operation_routes(
            app,
            auth=auth,
            operation_api=operation_api,
            api_prefix="/api",
            http_exception=lambda status_code, detail: HTTPException(
                status_code=status_code,
                detail=detail,
            ),
            traffic_log=logging.getLogger(__name__),
        )

        async def _request_routes() -> tuple[Response, Response, Response, Response, Response]:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                list_response = await client.get("/api/operations?kind=app_install")
                detail_response = await client.get(
                    f"/api/operations/{operation.operation_id}"
                )
                cancel_response = await client.post(
                    f"/api/operations/{operation.operation_id}/cancel",
                    json={},
                )
                repeated_cancel_response = await client.post(
                    f"/api/operations/{operation.operation_id}/cancel",
                    json={},
                )
                operations.finish(
                    operation_id=operation.operation_id,
                    state=NodeOperationState.CANCELLED,
                    summary="Install stopped.",
                )
                finished_cancel_response = await client.post(
                    f"/api/operations/{operation.operation_id}/cancel",
                    json={},
                )
                return (
                    list_response,
                    detail_response,
                    cancel_response,
                    repeated_cancel_response,
                    finished_cancel_response,
                )

        with patch("node_api.operations_routes.audit_log") as audit:
            (
                list_response,
                detail_response,
                cancel_response,
                repeated_cancel_response,
                finished_cancel_response,
            ) = asyncio.run(_request_routes())

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(repeated_cancel_response.status_code, 409)
        self.assertEqual(finished_cancel_response.status_code, 409)
        self.assertEqual(
            repeated_cancel_response.json(),
            {"detail": "Cancellation has already been requested."},
        )
        self.assertEqual(
            finished_cancel_response.json(),
            {"detail": "Operation has already finished."},
        )
        list_view = NodeOperationView.from_mapping(list_response.json()["operations"][0])
        detail_view = NodeOperationView.from_mapping(detail_response.json())
        cancelled_view = NodeOperationView.from_mapping(cancel_response.json())
        self.assertEqual(list_view.record.log_lines, ())
        self.assertEqual(
            detail_view.record.log_lines,
            ("stdout: Downloading content.",),
        )
        self.assertTrue(list_view.cancellable)
        self.assertFalse(cancelled_view.cancellable)
        self.assertEqual(cancelled_view.record.state, NodeOperationState.CANCEL_REQUESTED)
        self.assertEqual(cancel_requests, [(operation.operation_id, 42)])
        self.assertEqual(
            auth.access_requests,
            [(None, (NodeApiScope.APP_MANAGE,))] * 5,
        )
        audit.assert_called_once_with(
            "operation.cancel_requested",
            actor_user_id=42,
            node_name="node-a",
            operation_id=operation.operation_id,
            operation_kind=NodeOperationKind.APP_INSTALL.value,
            required_level=Power_Level.sudo.name,
        )

    def test_reopening_marks_active_operations_interrupted_and_releases_resources(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            first = NodeOperationService(database_path=database_path)
            operation = first.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )
            first.begin(
                operation_id=operation.operation_id,
                progress=NodeOperationProgress(
                    summary="Installing.", phase="installing"
                ),
            )

            reopened = NodeOperationService(database_path=database_path)
            recovered = reopened.get(operation.operation_id)
            successor = reopened.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )

        self.assertEqual(recovered.state, NodeOperationState.INTERRUPTED)
        self.assertFalse(recovered.active)
        self.assertNotEqual(successor.operation_id, operation.operation_id)

    def test_active_resource_conflicts_until_terminal_completion(self) -> None:
        operations = NodeOperationService()
        operation = operations.create(
            kind=NodeOperationKind.APP_INSTALL,
            node_name="node-a",
            subject="demo",
            requested_by_user_id=42,
            progress=NodeOperationProgress(summary="Queued."),
            resource_keys=("app-install-directory:/apps/demo",),
        )

        with self.assertRaises(NodeOperationResourceConflict):
            operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )

        operations.finish(
            operation_id=operation.operation_id,
            state=NodeOperationState.CANCELLED,
            summary="Install stopped.",
        )
        successor = operations.create(
            kind=NodeOperationKind.APP_INSTALL,
            node_name="node-a",
            subject="demo",
            requested_by_user_id=42,
            progress=NodeOperationProgress(summary="Queued."),
            resource_keys=("app-install-directory:/apps/demo",),
        )

        self.assertNotEqual(successor.operation_id, operation.operation_id)

    def test_database_history_pruning_keeps_recent_records_when_times_tie(self) -> None:
        with TemporaryDirectory() as temp_dir:
            operations = NodeOperationService(
                database_path=Path(temp_dir) / "operations.sqlite3",
                completed_history_limit=2,
                now_unix_ms=lambda: 1,
            )
            operation_ids: list[str] = []
            for index in range(3):
                operation = operations.create(
                    kind=NodeOperationKind.APP_INSTALL,
                    node_name="node-a",
                    subject="demo",
                    requested_by_user_id=42,
                    progress=NodeOperationProgress(summary="Queued."),
                    resource_keys=(f"app-install-directory:/apps/demo-{index}",),
                )
                operation_ids.append(operation.operation_id)
                operations.finish(
                    operation_id=operation.operation_id,
                    state=NodeOperationState.SUCCEEDED,
                    summary="Installed.",
                )

            with self.assertRaises(LookupError):
                operations.get(operation_ids[0])
            self.assertEqual(
                {record.operation_id for record in operations.list_records()},
                set(operation_ids[1:]),
            )

    def test_request_cancellation_is_cooperative_and_retains_resources(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )
            executor_started = asyncio.Event()
            allow_executor_to_finish = asyncio.Event()

            async def _execute() -> None:
                operations.begin(
                    operation_id=operation.operation_id,
                    progress=NodeOperationProgress(summary="Installing."),
                )
                executor_started.set()
                await allow_executor_to_finish.wait()
                if operations.cancellation_requested(operation_id=operation.operation_id):
                    operations.finish(
                        operation_id=operation.operation_id,
                        state=NodeOperationState.CANCELLED,
                        summary="Install stopped.",
                    )
                    return
                operations.finish(
                    operation_id=operation.operation_id,
                    state=NodeOperationState.SUCCEEDED,
                    summary="Installed.",
                )

            task = asyncio.create_task(_execute())
            operations.track_task(operation_id=operation.operation_id, task=task)
            await executor_started.wait()
            operations.request_cancellation(operation_id=operation.operation_id)
            self.assertFalse(task.done())
            self.assertTrue(
                operations.cancellation_requested(operation_id=operation.operation_id)
            )
            with self.assertRaises(NodeOperationResourceConflict):
                operations.create(
                    kind=NodeOperationKind.APP_INSTALL,
                    node_name="node-a",
                    subject="demo",
                    requested_by_user_id=42,
                    progress=NodeOperationProgress(summary="Queued."),
                    resource_keys=("app-install-directory:/apps/demo",),
                )

            allow_executor_to_finish.set()
            await task
            self.assertEqual(
                operations.get(operation.operation_id).state,
                NodeOperationState.CANCELLED,
            )
            successor = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )
            self.assertNotEqual(successor.operation_id, operation.operation_id)

        asyncio.run(_run())

    def test_hard_cancellation_interrupts_a_tracked_task_once(self) -> None:
        async def _run() -> None:
            operations = NodeOperationService()
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )
            executor_started = asyncio.Event()
            executor_cleaned_up = asyncio.Event()

            async def _execute() -> None:
                operations.begin(
                    operation_id=operation.operation_id,
                    progress=NodeOperationProgress(summary="Installing."),
                )
                executor_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    executor_cleaned_up.set()

            task = asyncio.create_task(_execute())
            operations.track_task(operation_id=operation.operation_id, task=task)
            await executor_started.wait()
            requested = operations.hard_cancel(operation_id=operation.operation_id)
            self.assertEqual(requested.state, NodeOperationState.CANCEL_REQUESTED)
            with self.assertRaises(NodeOperationResourceConflict):
                operations.create(
                    kind=NodeOperationKind.APP_INSTALL,
                    node_name="node-a",
                    subject="demo",
                    requested_by_user_id=42,
                    progress=NodeOperationProgress(summary="Queued."),
                    resource_keys=("app-install-directory:/apps/demo",),
                )

            with suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertTrue(executor_cleaned_up.is_set())
            self.assertEqual(
                operations.get(operation.operation_id).state,
                NodeOperationState.CANCELLED,
            )
            successor = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=("app-install-directory:/apps/demo",),
            )
            self.assertNotEqual(successor.operation_id, operation.operation_id)

        asyncio.run(_run())

    def test_terminal_completion_wins_a_hard_cancellation_race(self) -> None:
        async def _run() -> NodeOperationState:
            operations = NodeOperationService()
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )
            executor_started = asyncio.Event()
            allow_executor_to_finish = asyncio.Event()

            async def _execute() -> None:
                operations.begin(
                    operation_id=operation.operation_id,
                    progress=NodeOperationProgress(summary="Installing."),
                )
                executor_started.set()
                await allow_executor_to_finish.wait()

            task = asyncio.create_task(_execute())
            operations.track_task(operation_id=operation.operation_id, task=task)
            await executor_started.wait()
            operations.hard_cancel(operation_id=operation.operation_id)
            operations.finish(
                operation_id=operation.operation_id,
                state=NodeOperationState.SUCCEEDED,
                summary="Installed.",
            )
            await asyncio.sleep(0)
            self.assertFalse(task.done())

            allow_executor_to_finish.set()
            await task
            return operations.get(operation.operation_id).state

        self.assertEqual(asyncio.run(_run()), NodeOperationState.SUCCEEDED)

    def test_cancellation_before_executor_start_is_finished_cooperatively(self) -> None:
        async def _run() -> NodeOperationState:
            operations = NodeOperationService()
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )

            async def _execute() -> None:
                operations.begin(
                    operation_id=operation.operation_id,
                    progress=NodeOperationProgress(summary="Installing."),
                )
                if operations.cancellation_requested(operation_id=operation.operation_id):
                    operations.finish(
                        operation_id=operation.operation_id,
                        state=NodeOperationState.CANCELLED,
                        summary="Install stopped.",
                    )

            task = asyncio.create_task(_execute())
            operations.track_task(operation_id=operation.operation_id, task=task)
            operations.request_cancellation(operation_id=operation.operation_id)
            await task
            return operations.get(operation.operation_id).state

        self.assertEqual(asyncio.run(_run()), NodeOperationState.CANCELLED)

    def test_fresh_database_creation_uses_schema_version_one(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            operations = NodeOperationService(database_path=database_path)
            self.assertEqual(operations.list_records(), ())

            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(version, (1,))
        self.assertEqual(
            {row[0] for row in table_rows},
            {"node_operations", "node_operation_logs", "node_operation_resources"},
        )

    def test_reopening_current_schema_preserves_its_version(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            first = NodeOperationService(database_path=database_path)
            operation = first.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )
            first.finish(
                operation_id=operation.operation_id,
                state=NodeOperationState.SUCCEEDED,
                summary="Installed.",
            )

            reopened = NodeOperationService(database_path=database_path)
            self.assertEqual(
                reopened.get(operation.operation_id).state,
                NodeOperationState.SUCCEEDED,
            )
            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()

        self.assertEqual(version, (1,))

    def test_version_zero_database_is_upgraded_to_version_one(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            legacy_operations = NodeOperationService(database_path=database_path)
            operation = legacy_operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )
            legacy_operations.finish(
                operation_id=operation.operation_id,
                state=NodeOperationState.SUCCEEDED,
                summary="Installed.",
            )
            with sqlite3.connect(database_path) as database:
                database.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
                database.execute("INSERT INTO legacy_marker (value) VALUES ('kept')")
                database.execute("PRAGMA user_version = 0")

            operations = NodeOperationService(database_path=database_path)
            self.assertEqual(
                operations.get(operation.operation_id).state,
                NodeOperationState.SUCCEEDED,
            )
            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                marker = database.execute("SELECT value FROM legacy_marker").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(version, (1,))
        self.assertEqual(marker, ("kept",))
        self.assertTrue(
            {"node_operations", "node_operation_logs", "node_operation_resources"}
            <= {row[0] for row in table_rows}
        )

    def test_failed_database_migration_rolls_back(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.execute(
                    "CREATE TABLE node_operations (operation_id TEXT PRIMARY KEY)"
                )
                database.execute("PRAGMA user_version = 0")

            with self.assertRaises(sqlite3.OperationalError):
                NodeOperationService(database_path=database_path).list_records()

            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(version, (0,))
        self.assertEqual(table_rows, [("node_operations",)])

    def test_newer_database_schema_version_is_rejected_without_changes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.execute("CREATE TABLE future_marker (value TEXT NOT NULL)")
                database.execute("PRAGMA user_version = 2")

            with self.assertRaises(NodeOperationSchemaVersionError) as raised:
                NodeOperationService(database_path=database_path).list_records()

            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(raised.exception.database_version, 2)
        self.assertEqual(raised.exception.supported_version, 1)
        self.assertEqual(version, (2,))
        self.assertEqual(table_rows, [("future_marker",)])


if __name__ == "__main__":
    unittest.main()
