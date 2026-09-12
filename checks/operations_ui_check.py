from __future__ import annotations

import asyncio
import unittest

from mod_web_auth import ModWebUser
from node_api.operation_service import (
    NodeOperationStreamEvent,
    NodeOperationStreamEventKind,
    NodeOperationView,
)
from node_api.operations import (
    NodeOperationKind,
    NodeOperationRecord,
    NodeOperationState,
)
from node_auth import NodeApiScope
from web_dash.app_installer import ModWebAppInstallerMixin
from web_dash.operation_stream import ModWebNodeOperationSnapshot
from web_dash.operations_ui import (
    ModWebOperationActivityFilter,
    ModWebOperationsMixin,
    operation_detail_path,
)
from web_dash.remote_node_monitor import RemoteNodeAvailability
from web_dash.types import ModWebNodeLink


def _node(name: str) -> ModWebNodeLink:
    return ModWebNodeLink(
        node_name=name,
        label=name.title(),
        url=f"/mod-web/nodes/{name}/system",
        api_base_url=f"https://{name}.example.test/api/node",
        api_url=f"/api/node/{name}",
        is_current=False,
    )


def _operation(
    *,
    operation_id: str,
    node_name: str,
    state: NodeOperationState,
    created_at_unix_ms: int,
    finished_at_unix_ms: int | None = None,
    kind: NodeOperationKind = NodeOperationKind.APP_INSTALL,
    app_name: str | None = None,
    summary: str = "Working.",
    detail: str | None = None,
    log_lines: tuple[str, ...] = (),
) -> NodeOperationView:
    return NodeOperationView(
        record=NodeOperationRecord(
            operation_id=operation_id,
            kind=kind,
            node_name=node_name,
            subject=f"Subject {operation_id}",
            state=state,
            summary=summary,
            requested_by_user_id=42,
            app_name=app_name,
            detail=detail,
            log_lines=log_lines,
            created_at_unix_ms=created_at_unix_ms,
            finished_at_unix_ms=(finished_at_unix_ms if state.terminal else None),
        ),
        kind_label=(
            "App install" if kind is NodeOperationKind.APP_INSTALL else "Mod metadata"
        ),
        cancellable=state in {NodeOperationState.QUEUED, NodeOperationState.RUNNING},
    )


class _OperationRequestHarness(ModWebOperationsMixin):
    def __init__(self, response: NodeOperationView) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response

    async def _remote_json_async(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self._response.to_mapping()


class _AppInstallerRequestHarness(ModWebAppInstallerMixin):
    def __init__(self, responses: tuple[dict[str, object], ...]) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = iter(responses)

    async def _remote_json_async(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return next(self._responses)


class OperationsUiCheck(unittest.TestCase):
    def test_portal_aggregation_merges_local_and_remote_authoritative_node_snapshots(
        self,
    ) -> None:
        alpha = _node("alpha")
        beta = _node("beta")
        portal = _node("portal")
        portal_sources = ModWebOperationsMixin._operation_source_nodes(
            node=portal,
            all_nodes=(portal, alpha, beta),
            portal=True,
        )
        self.assertEqual(portal_sources, (portal, alpha, beta))
        self.assertEqual(
            ModWebOperationsMixin._operation_source_nodes(
                node=alpha,
                all_nodes=(portal, alpha, beta),
                portal=False,
            ),
            (alpha,),
        )
        portal_active = _operation(
            operation_id="portal-active",
            node_name="portal",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=40,
        )
        completed = _operation(
            operation_id="completed",
            node_name="alpha",
            state=NodeOperationState.SUCCEEDED,
            created_at_unix_ms=10,
            finished_at_unix_ms=90,
        )
        alpha_active = _operation(
            operation_id="alpha-active",
            node_name="alpha",
            state=NodeOperationState.QUEUED,
            created_at_unix_ms=30,
        )
        beta_active = _operation(
            operation_id="beta-active",
            node_name="beta",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=20,
            kind=NodeOperationKind.MOD_METADATA_APPLY,
            app_name="minecraft_alpha",
        )
        rows = ModWebOperationsMixin._operation_rows(
            nodes=portal_sources,
            snapshots_by_node={
                "portal": ModWebNodeOperationSnapshot(
                    node_name="portal",
                    operations=(portal_active,),
                ),
                "alpha": ModWebNodeOperationSnapshot(
                    node_name="alpha",
                    operations=(completed, alpha_active),
                ),
                "beta": ModWebNodeOperationSnapshot(
                    node_name="beta",
                    operations=(beta_active,),
                ),
            },
            availability_by_node={
                "portal": RemoteNodeAvailability.ONLINE,
                "alpha": RemoteNodeAvailability.ONLINE,
                "beta": RemoteNodeAvailability.ONLINE,
            },
        )

        self.assertEqual(
            tuple(row.operation.record.operation_id for row in rows),
            ("portal-active", "alpha-active", "beta-active", "completed"),
        )
        self.assertEqual(
            tuple(
                row.operation.record.operation_id
                for row in ModWebOperationsMixin._filtered_operation_rows(
                    rows=rows,
                    node_name="beta",
                    kind=NodeOperationKind.MOD_METADATA_APPLY.value,
                    app_name="MINECRAFT_ALPHA",
                    state=NodeOperationState.RUNNING.value,
                    activity=ModWebOperationActivityFilter.ACTIVE,
                )
            ),
            ("beta-active",),
        )
        self.assertEqual(
            tuple(
                row.operation.record.operation_id
                for row in ModWebOperationsMixin._filtered_operation_rows(
                    rows=rows,
                    node_name="",
                    kind="",
                    app_name="",
                    state="",
                    activity=ModWebOperationActivityFilter.COMPLETED,
                )
            ),
            ("completed",),
        )

    def test_portal_aggregation_marks_only_offline_node_data_stale(
        self,
    ) -> None:
        portal = _node("portal")
        alpha = _node("alpha")
        beta = _node("beta")
        portal_operation = _operation(
            operation_id="portal-active",
            node_name="portal",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=15,
        )
        alpha_operation = _operation(
            operation_id="alpha-active",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
        )
        beta_operation = _operation(
            operation_id="beta-failed",
            node_name="beta",
            state=NodeOperationState.FAILED,
            created_at_unix_ms=20,
            finished_at_unix_ms=30,
            summary="Upload failed.",
        )
        rows = ModWebOperationsMixin._operation_rows(
            nodes=(portal, alpha, beta),
            snapshots_by_node={
                "portal": ModWebNodeOperationSnapshot(
                    node_name="portal",
                    operations=(portal_operation,),
                ),
                "alpha": ModWebNodeOperationSnapshot(
                    node_name="alpha",
                    operations=(alpha_operation,),
                ),
                "beta": ModWebNodeOperationSnapshot(
                    node_name="beta",
                    operations=(beta_operation,),
                ),
            },
            availability_by_node={
                "portal": RemoteNodeAvailability.ONLINE,
                "alpha": RemoteNodeAvailability.ONLINE,
                "beta": RemoteNodeAvailability.OFFLINE,
            },
        )

        rows_by_id = {row.operation.record.operation_id: row for row in rows}
        self.assertEqual(
            set(rows_by_id),
            {"portal-active", "alpha-active", "beta-failed"},
        )
        self.assertFalse(rows_by_id["portal-active"].stale)
        self.assertFalse(rows_by_id["alpha-active"].stale)
        self.assertTrue(rows_by_id["beta-failed"].stale)

    def test_fresh_snapshot_replaces_stale_stream_data_and_complete_summary_updates_replace_records(
        self,
    ) -> None:
        first = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
            summary="Downloading.",
        )
        second = _operation(
            operation_id="second",
            node_name="alpha",
            state=NodeOperationState.SUCCEEDED,
            created_at_unix_ms=20,
            finished_at_unix_ms=30,
        )
        updated_first = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
            summary="Extracting.",
            detail="Unpacking files.",
            log_lines=("stdout: Extracting.",),
        )
        snapshot = ModWebNodeOperationSnapshot.apply_event(
            None,
            NodeOperationStreamEvent(
                kind=NodeOperationStreamEventKind.SNAPSHOT,
                node_name="alpha",
                operations=(first, second),
            ),
        )
        snapshot = ModWebNodeOperationSnapshot.apply_event(
            snapshot,
            NodeOperationStreamEvent(
                kind=NodeOperationStreamEventKind.UPDATED,
                node_name="alpha",
                operation=updated_first,
            ),
        )
        self.assertEqual(
            next(
                operation
                for operation in snapshot.operations
                if operation.record.operation_id == "first"
            ).record.log_lines,
            (),
        )

        reconnected = ModWebNodeOperationSnapshot.apply_event(
            snapshot,
            NodeOperationStreamEvent(
                kind=NodeOperationStreamEventKind.SNAPSHOT,
                node_name="alpha",
                operations=(second,),
            ),
        )
        self.assertEqual(
            tuple(
                operation.record.operation_id for operation in reconnected.operations
            ),
            ("second",),
        )

    def test_operation_stream_rejects_updates_before_an_authoritative_snapshot(
        self,
    ) -> None:
        operation = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
        )

        with self.assertRaisesRegex(ValueError, "authoritative snapshot"):
            ModWebNodeOperationSnapshot.apply_event(
                None,
                NodeOperationStreamEvent(
                    kind=NodeOperationStreamEventKind.UPDATED,
                    node_name="alpha",
                    operation=operation,
                ),
            )

    def test_operation_stream_summary_omits_retained_log_lines(self) -> None:
        operation = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
            log_lines=("stdout: Extracting.",),
        )

        snapshot = NodeOperationStreamEvent(
            kind=NodeOperationStreamEventKind.SNAPSHOT,
            node_name="alpha",
            operations=(operation,),
        )
        event = NodeOperationStreamEvent(
            kind=NodeOperationStreamEventKind.UPDATED,
            node_name="alpha",
            operation=operation,
        )

        self.assertEqual(snapshot.operations[0].record.log_lines, ())
        self.assertEqual(event.operation.record.log_lines if event.operation else (), ())
        snapshot_payload = snapshot.to_mapping()
        snapshot_operation = snapshot_payload["operations"]
        self.assertIsInstance(snapshot_operation, list)
        assert isinstance(snapshot_operation, list)
        self.assertTrue(snapshot_operation)
        snapshot_operation_view = snapshot_operation[0]
        self.assertIsInstance(snapshot_operation_view, dict)
        assert isinstance(snapshot_operation_view, dict)
        self.assertNotIn("log_lines", snapshot_operation_view)
        payload = event.to_mapping()
        operation_payload = payload["operation"]
        self.assertIsInstance(operation_payload, dict)
        assert isinstance(operation_payload, dict)
        self.assertNotIn("log_lines", operation_payload)

    def test_significant_stream_updates_ignore_retained_log_only_changes(self) -> None:
        previous = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
            summary="Downloading.",
            log_lines=("stdout: One.",),
        )
        log_only_change = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
            summary="Downloading.",
            log_lines=("stdout: Two.",),
        )
        progress_change = _operation(
            operation_id="first",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
            summary="Extracting.",
        )

        self.assertFalse(
            ModWebOperationsMixin._operation_stream_update_is_significant(
                previous,
                log_only_change,
            )
        )
        self.assertTrue(
            ModWebOperationsMixin._operation_stream_update_is_significant(
                previous,
                progress_change,
            )
        )

    def test_detail_load_uses_the_authoritative_operation_endpoint(self) -> None:
        node = _node("alpha")
        operation = _operation(
            operation_id="metadata",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=20,
            kind=NodeOperationKind.MOD_METADATA_APPLY,
            app_name="minecraft_alpha",
        )
        detail = _operation(
            operation_id="metadata",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=20,
            kind=NodeOperationKind.MOD_METADATA_APPLY,
            app_name="minecraft_alpha",
            detail="Unpacking files.",
            log_lines=("stdout: Extracting.",),
        )
        user = ModWebUser(
            discord_id=42,
            username="operator",
            global_name=None,
            avatar_hash=None,
        )
        harness = _OperationRequestHarness(detail)

        loaded = asyncio.run(
            ModWebOperationsMixin._operation_detail_from_operations_ui(
                harness,
                node=node,
                operation=operation,
                user=user,
            )
        )

        self.assertEqual(loaded, detail)
        self.assertEqual(
            {
                key: value
                for key, value in harness.calls[0].items()
                if key != "user"
            },
            {
                "node": node,
                "app_name": "minecraft_alpha",
                "path": (
                    "/operations/metadata?"
                    "kind=mod_metadata_apply&app_name=minecraft_alpha"
                ),
                "scopes": (NodeApiScope.MODS_WRITE,),
            },
        )
        self.assertIs(harness.calls[0]["user"], user)

    def test_cancellation_uses_existing_kind_specific_scope_and_target(self) -> None:
        node = _node("alpha")
        install = _operation(
            operation_id="install",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=10,
        )
        metadata = _operation(
            operation_id="metadata",
            node_name="alpha",
            state=NodeOperationState.RUNNING,
            created_at_unix_ms=20,
            kind=NodeOperationKind.MOD_METADATA_APPLY,
            app_name="minecraft_alpha",
        )
        user = ModWebUser(
            discord_id=42,
            username="operator",
            global_name=None,
            avatar_hash=None,
        )

        install_harness = _OperationRequestHarness(install)
        install_result = asyncio.run(
            ModWebOperationsMixin._cancel_operation_from_operations_ui(
                install_harness,
                node=node,
                operation=install,
                user=user,
            )
        )
        metadata_harness = _OperationRequestHarness(metadata)
        metadata_result = asyncio.run(
            ModWebOperationsMixin._cancel_operation_from_operations_ui(
                metadata_harness,
                node=node,
                operation=metadata,
                user=user,
            )
        )

        self.assertEqual(install_result, install)
        self.assertEqual(metadata_result, metadata)
        self.assertEqual(len(install_harness.calls), 1)
        self.assertEqual(
            {
                key: value
                for key, value in install_harness.calls[0].items()
                if key != "user"
            },
            {
                "node": node,
                "app_name": None,
                "path": "/operations/install/cancel?kind=app_install",
                "scopes": (NodeApiScope.APP_MANAGE,),
                "method": "POST",
                "json_payload": {},
            },
        )
        self.assertIs(install_harness.calls[0]["user"], user)
        self.assertEqual(len(metadata_harness.calls), 1)
        self.assertEqual(
            {
                key: value
                for key, value in metadata_harness.calls[0].items()
                if key != "user"
            },
            {
                "node": node,
                "app_name": "minecraft_alpha",
                "path": "/operations/metadata/cancel?kind=mod_metadata_apply&app_name=minecraft_alpha",
                "scopes": (NodeApiScope.MODS_WRITE,),
                "method": "POST",
                "json_payload": {},
            },
        )
        self.assertIs(metadata_harness.calls[0]["user"], user)
        self.assertEqual(
            operation_detail_path(node_name="alpha node", operation_id="operation 1"),
            "/mod-web/nodes/alpha%20node/system?tab=operations&operation_id=operation+1",
        )

    def test_app_install_cancellation_reloads_authoritative_operation_detail(self) -> None:
        node = _node("alpha")
        operation = _operation(
            operation_id="install",
            node_name="alpha",
            state=NodeOperationState.CANCEL_REQUESTED,
            created_at_unix_ms=10,
            summary="Cancellation requested.",
        )
        detail = _operation(
            operation_id="install",
            node_name="alpha",
            state=NodeOperationState.CANCEL_REQUESTED,
            created_at_unix_ms=10,
            summary="Cancellation requested.",
            detail="Stopping the installer.",
            log_lines=("stdout: Downloading.",),
        )
        user = ModWebUser(
            discord_id=42,
            username="operator",
            global_name=None,
            avatar_hash=None,
        )
        harness = _AppInstallerRequestHarness(
            (operation.to_mapping(include_log_lines=False), detail.to_mapping())
        )

        status = asyncio.run(
            harness._cancel_app_install(node=node, job_id="install", user=user)
        )

        self.assertEqual(status.detail, "Stopping the installer.")
        self.assertEqual(status.log_lines, ("stdout: Downloading.",))
        self.assertEqual(
            [
                {key: value for key, value in call.items() if key != "user"}
                for call in harness.calls
            ],
            [
                {
                    "node": node,
                    "app_name": None,
                    "path": "/operations/install/cancel?kind=app_install",
                    "scopes": (NodeApiScope.APP_MANAGE,),
                    "method": "POST",
                    "json_payload": {},
                },
                {
                    "node": node,
                    "app_name": None,
                    "path": "/operations/install?kind=app_install",
                    "scopes": (NodeApiScope.APP_MANAGE,),
                },
            ],
        )
        self.assertTrue(all(call["user"] is user for call in harness.calls))


if __name__ == "__main__":
    unittest.main()
