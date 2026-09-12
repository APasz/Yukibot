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
from node_api.operation_service import (
    NodeOperationApiService,
    NodeOperationKindPolicy,
    NodeOperationTargetScope,
    NodeOperationView,
)
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
        self.level_requests: list[Power_Level] = []

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

    async def require_actor_level(
        self,
        context: NodeRequestContext,
        required_level: Power_Level,
    ) -> NodeRequestContext:
        self.level_requests.append(required_level)
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
            app_name="demo-alpha",
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

    def test_persisted_result_artifact_retains_its_app_target_and_expires(self) -> None:
        now_unix_ms = [1_000]
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            operations = NodeOperationService(
                database_path=database_path,
                now_unix_ms=lambda: now_unix_ms[0],
            )
            operation = operations.create(
                kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
                node_name="node-a",
                subject="Minecraft Alpha",
                requested_by_user_id=42,
                app_name="minecraft_alpha",
                progress=NodeOperationProgress(summary="Queued."),
            )
            operations.begin(
                operation_id=operation.operation_id,
                progress=NodeOperationProgress(summary="Scanning metadata."),
            )
            operations.finish(
                operation_id=operation.operation_id,
                state=NodeOperationState.SUCCEEDED,
                summary="Metadata discovery is ready.",
                result_payload_json='{"entries":[]}',
                result_ttl_seconds=1,
            )

            reopened = NodeOperationService(
                database_path=database_path,
                now_unix_ms=lambda: now_unix_ms[0],
            )
            record = reopened.get(operation.operation_id)
            records_for_app = reopened.list_records(
                kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
                app_name="MINECRAFT_ALPHA",
            )
            result = reopened.get_result(operation_id=operation.operation_id)
            now_unix_ms[0] = 2_000
            with self.assertRaises(LookupError):
                reopened.get_result(operation_id=operation.operation_id)

        self.assertEqual(record.app_name, "minecraft_alpha")
        self.assertEqual(records_for_app, (record,))
        self.assertEqual(result.payload_json, '{"entries":[]}')
        self.assertEqual(result.expires_at_unix_ms, 2_000)

    def test_app_scoped_operation_api_requires_the_matching_app_target(self) -> None:
        operations = NodeOperationService()
        operation = operations.create(
            kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
            node_name="node-a",
            subject="Minecraft Alpha",
            requested_by_user_id=42,
            app_name="minecraft_alpha",
            progress=NodeOperationProgress(summary="Queued."),
        )
        operation_api = NodeOperationApiService(
            operations=operations,
            policies=(
                NodeOperationKindPolicy(
                    kind=NodeOperationKind.APP_INSTALL,
                    kind_label="App install",
                    read_scope=NodeApiScope.APP_MANAGE,
                    cancel_scope=NodeApiScope.APP_MANAGE,
                    required_level=Power_Level.sudo,
                ),
                NodeOperationKindPolicy(
                    kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
                    kind_label="Mod metadata discovery",
                    read_scope=NodeApiScope.MODS_WRITE,
                    cancel_scope=NodeApiScope.MODS_WRITE,
                    required_level=Power_Level.sudo,
                    target_scope=NodeOperationTargetScope.APP,
                ),
            ),
        )

        view = operation_api.get_operation(
            operation_id=operation.operation_id,
            kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
            app_name="minecraft_alpha",
        )

        self.assertEqual(view.record, operation)
        self.assertEqual(
            operation_api.list_operations(
                kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
                app_name="MINECRAFT_ALPHA",
            ),
            (view,),
        )
        with self.assertRaises(LookupError):
            operation_api.get_operation(
                operation_id=operation.operation_id,
                kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
                app_name="factorio_alpha",
            )
        with self.assertRaisesRegex(ValueError, "require an app name"):
            operation_api.get_operation(
                operation_id=operation.operation_id,
                kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
            )
        with self.assertRaisesRegex(ValueError, "Operation kind is required"):
            operation_api.list_operations()

    def test_single_policy_only_returns_its_registered_operation_kind(self) -> None:
        operations = NodeOperationService()
        install = operations.create(
            kind=NodeOperationKind.APP_INSTALL,
            node_name="node-a",
            subject="demo",
            requested_by_user_id=42,
            progress=NodeOperationProgress(summary="Queued."),
        )
        metadata = operations.create(
            kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
            node_name="node-a",
            subject="Minecraft Alpha",
            requested_by_user_id=42,
            app_name="minecraft_alpha",
            progress=NodeOperationProgress(summary="Queued."),
        )
        operation_api = NodeOperationApiService(
            operations=operations,
            policies=(
                NodeOperationKindPolicy(
                    kind=NodeOperationKind.APP_INSTALL,
                    kind_label="App install",
                    read_scope=NodeApiScope.APP_MANAGE,
                    cancel_scope=NodeApiScope.APP_MANAGE,
                    required_level=Power_Level.sudo,
                ),
            ),
        )

        listed = operation_api.list_operations()

        self.assertEqual(tuple(view.record.operation_id for view in listed), (install.operation_id,))
        with self.assertRaises(LookupError):
            operation_api.get_operation(operation_id=metadata.operation_id)

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
        self.assertEqual(auth.level_requests, [Power_Level.sudo] * 3)
        audit.assert_called_once_with(
            "operation.cancel_requested",
            actor_user_id=42,
            node_name="node-a",
            operation_id=operation.operation_id,
            operation_kind=NodeOperationKind.APP_INSTALL.value,
            required_level=Power_Level.sudo.name,
        )

    def test_app_scoped_operation_routes_authorize_and_enforce_the_app_target(self) -> None:
        operations = NodeOperationService()
        operation = operations.create(
            kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
            node_name="node-a",
            subject="Minecraft Alpha",
            requested_by_user_id=42,
            app_name="minecraft_alpha",
            progress=NodeOperationProgress(summary="Queued."),
        )
        cancellation_requests: list[tuple[str, int]] = []

        async def _cancel(operation_id: str, actor_user_id: int) -> None:
            cancellation_requests.append((operation_id, actor_user_id))
            operations.request_cancellation(operation_id=operation_id)

        operation_api = NodeOperationApiService(
            operations=operations,
            policies=(
                NodeOperationKindPolicy(
                    kind=NodeOperationKind.MOD_METADATA_DISCOVERY,
                    kind_label="Mod metadata discovery",
                    read_scope=NodeApiScope.MODS_WRITE,
                    cancel_scope=NodeApiScope.MODS_WRITE,
                    required_level=Power_Level.sudo,
                    target_scope=NodeOperationTargetScope.APP,
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

        async def _request_routes() -> tuple[Response, Response, Response, Response]:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                query = "kind=mod_metadata_discovery&app_name=minecraft_alpha"
                list_response = await client.get(f"/api/operations?{query}")
                detail_response = await client.get(
                    f"/api/operations/{operation.operation_id}?{query}"
                )
                wrong_target_response = await client.get(
                    f"/api/operations/{operation.operation_id}?"
                    "kind=mod_metadata_discovery&app_name=factorio_alpha"
                )
                cancel_response = await client.post(
                    f"/api/operations/{operation.operation_id}/cancel?{query}",
                    json={},
                )
                return (
                    list_response,
                    detail_response,
                    wrong_target_response,
                    cancel_response,
                )

        with patch("node_api.operations_routes.audit_log"):
            (
                list_response,
                detail_response,
                wrong_target_response,
                cancel_response,
            ) = asyncio.run(_request_routes())

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(wrong_target_response.status_code, 404)
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancellation_requests, [(operation.operation_id, 42)])
        self.assertEqual(
            auth.access_requests,
            [
                ("minecraft_alpha", (NodeApiScope.MODS_WRITE,)),
                ("minecraft_alpha", (NodeApiScope.MODS_WRITE,)),
                ("factorio_alpha", (NodeApiScope.MODS_WRITE,)),
                ("minecraft_alpha", (NodeApiScope.MODS_WRITE,)),
            ],
        )
        self.assertEqual(auth.level_requests, [Power_Level.sudo])

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

    def test_fresh_database_creation_uses_current_schema_version(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            operations = NodeOperationService(database_path=database_path)
            self.assertEqual(operations.list_records(), ())

            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(version, (2,))
        self.assertEqual(
            {row[0] for row in table_rows},
            {
                "node_operations",
                "node_operation_logs",
                "node_operation_resources",
                "node_operation_results",
            },
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

        self.assertEqual(version, (2,))

    def test_version_zero_database_is_upgraded_to_current_schema_version(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
                database.execute("INSERT INTO legacy_marker (value) VALUES ('kept')")
                database.execute("PRAGMA user_version = 0")

            operations = NodeOperationService(database_path=database_path)
            operation = operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name="node-a",
                subject="demo",
                requested_by_user_id=42,
                progress=NodeOperationProgress(summary="Queued."),
            )
            self.assertEqual(operations.get(operation.operation_id).state, NodeOperationState.QUEUED)
            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                marker = database.execute("SELECT value FROM legacy_marker").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(version, (2,))
        self.assertEqual(marker, ("kept",))
        self.assertTrue(
            {
                "node_operations",
                "node_operation_logs",
                "node_operation_resources",
                "node_operation_results",
            }
            <= {row[0] for row in table_rows}
        )

    def test_version_one_database_upgrades_existing_records_to_version_two(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "operations.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.execute(
                    """
                    CREATE TABLE node_operations (
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
                    """
                )
                database.execute(
                    """
                    CREATE TABLE node_operation_logs (
                        operation_id TEXT NOT NULL REFERENCES node_operations(operation_id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        line TEXT NOT NULL,
                        PRIMARY KEY (operation_id, sequence)
                    )
                    """
                )
                database.execute(
                    """
                    CREATE TABLE node_operation_resources (
                        resource_key TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL REFERENCES node_operations(operation_id) ON DELETE CASCADE
                    )
                    """
                )
                database.execute(
                    """
                    INSERT INTO node_operations (
                        operation_id, kind, node_name, subject, requested_by_user_id,
                        state, summary, phase, result_reference, detail, progress_percent,
                        created_at_unix_ms, started_at_unix_ms, finished_at_unix_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-operation",
                        NodeOperationKind.APP_INSTALL.value,
                        "node-a",
                        "demo",
                        42,
                        NodeOperationState.SUCCEEDED.value,
                        "Installed.",
                        None,
                        "demo_alpha",
                        None,
                        100.0,
                        1,
                        1,
                        2,
                    ),
                )
                database.execute("PRAGMA user_version = 1")

            operations = NodeOperationService(database_path=database_path)
            record = operations.get("legacy-operation")
            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                columns = tuple(
                    row[1]
                    for row in database.execute("PRAGMA table_info(node_operations)")
                )

        self.assertEqual(record.app_name, None)
        self.assertEqual(version, (2,))
        self.assertIn("app_name", columns)

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
                database.execute("PRAGMA user_version = 3")

            with self.assertRaises(NodeOperationSchemaVersionError) as raised:
                NodeOperationService(database_path=database_path).list_records()

            with sqlite3.connect(database_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()
                table_rows = database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()

        self.assertEqual(raised.exception.database_version, 3)
        self.assertEqual(raised.exception.supported_version, 2)
        self.assertEqual(version, (3,))
        self.assertEqual(table_rows, [("future_marker",)])


if __name__ == "__main__":
    unittest.main()
