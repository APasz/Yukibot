from __future__ import annotations

import asyncio
import unittest
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from node_api.operations import (
    NodeOperationKind,
    NodeOperationProgress,
    NodeOperationResourceConflict,
    NodeOperationService,
    NodeOperationState,
)


class NodeOperationsCheck(unittest.TestCase):
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

    def test_cancelling_a_tracked_task_finishes_the_operation(self) -> None:
        async def _run() -> NodeOperationState:
            operations = NodeOperationService()
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )

            async def _wait_forever() -> None:
                await asyncio.Event().wait()

            task = asyncio.create_task(_wait_forever())
            operations.track_task(operation_id=operation.operation_id, task=task)
            operations.request_cancellation(operation_id=operation.operation_id)
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            return operations.get(operation.operation_id).state

        self.assertEqual(asyncio.run(_run()), NodeOperationState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
