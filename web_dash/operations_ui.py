"""Shared live Operations surfaces for node and Portal system pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from node_api.operation_service import (
    NodeOperationStreamEvent,
    NodeOperationStreamEventKind,
    NodeOperationView,
)
from node_api.operations import NodeOperationState
from node_auth import NodeApiScope

from .links import mod_web_node_system_path
from .nicegui_protocols import ModWebUi, _value_as_bool, _value_as_text
from .operation_stream import ModWebNodeOperationSnapshot
from .remote_node_monitor import RemoteNodeAvailability, RemoteNodeMonitorSnapshot
from .runtime_imports import (
    AbstractEventLoop,
    BadgeTone,
    Callable,
    ModWebUser,
    Power_Level,
    asyncio,
    escape,
    quote,
)
from .service_base import ModWebServiceSupport
from .types import ModWebNodeLink

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.select import Select


_OPERATIONS_FILTER_SELECT_PROPS = (
    "filled square dense hide-bottom-space color=accent options-dark "
    "popup-content-class=mod-setting-menu"
)
_OPERATIONS_FILTER_SELECT_CLASSES = (
    "w-full min-w-0 mod-app-details-field mod-config-select"
)


class ModWebOperationActivityFilter(StrEnum):
    ALL = "all"
    ACTIVE = "active"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class _ModWebOperationDetailKey:
    """The node-local identity of an operation's separately loaded detail."""

    node_key: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class ModWebOperationRow:
    """One non-persistent operation rendered by the shared dashboard component."""

    node: ModWebNodeLink
    operation: NodeOperationView
    availability: RemoteNodeAvailability

    @property
    def stale(self) -> bool:
        return self.availability is not RemoteNodeAvailability.ONLINE

    @property
    def detail_key(self) -> _ModWebOperationDetailKey:
        return _ModWebOperationDetailKey(
            node_key=self.node.node_name.casefold(),
            operation_id=self.operation.record.operation_id,
        )


def operation_detail_path(*, node_name: str, operation_id: str) -> str:
    """Build a direct Operations-tab link for an operation on its authoritative node."""

    return f"{mod_web_node_system_path(node_name)}?{urlencode({'tab': 'operations', 'operation_id': operation_id})}"


class ModWebOperationsMixin(ModWebServiceSupport):
    """Renders and subscribes to operation records without owning their history."""

    @staticmethod
    def _operation_state_icon(state: NodeOperationState) -> str:
        match state:
            case NodeOperationState.QUEUED:
                return "schedule"
            case NodeOperationState.RUNNING:
                return "sync"
            case NodeOperationState.CANCEL_REQUESTED:
                return "hourglass_top"
            case NodeOperationState.SUCCEEDED:
                return "check_circle"
            case NodeOperationState.FAILED:
                return "error"
            case NodeOperationState.CANCELLED:
                return "cancel"
            case NodeOperationState.INTERRUPTED:
                return "warning"

    @staticmethod
    def _operation_state_tone(state: NodeOperationState) -> BadgeTone:
        match state:
            case NodeOperationState.QUEUED | NodeOperationState.RUNNING:
                return "purple"
            case NodeOperationState.CANCEL_REQUESTED:
                return "warn"
            case NodeOperationState.SUCCEEDED:
                return "grey"
            case NodeOperationState.FAILED:
                return "red"
            case NodeOperationState.CANCELLED | NodeOperationState.INTERRUPTED:
                return "warn"

    @staticmethod
    def _operation_state_icon_class(state: NodeOperationState) -> str:
        match state:
            case NodeOperationState.SUCCEEDED:
                return "text-positive"
            case NodeOperationState.FAILED:
                return "text-negative"
            case (
                NodeOperationState.CANCEL_REQUESTED
                | NodeOperationState.CANCELLED
                | NodeOperationState.INTERRUPTED
            ):
                return "text-warning"
            case NodeOperationState.QUEUED | NodeOperationState.RUNNING:
                return "text-purple"

    @staticmethod
    def _operation_state_label(state: NodeOperationState) -> str:
        return state.value.replace("_", " ").title()

    @staticmethod
    def _operation_timestamp(timestamp_unix_ms: int | None) -> str:
        if timestamp_unix_ms is None:
            return "—"
        return (
            datetime.fromtimestamp(timestamp_unix_ms / 1000)
            .astimezone()
            .strftime("%d %b %Y · %H:%M:%S")
        )

    @staticmethod
    def _operation_node_label(node: ModWebNodeLink) -> str:
        return (
            node.label
            if node.label.casefold() == node.node_name.casefold()
            else f"{node.label} · {node.node_name}"
        )

    @staticmethod
    def _operation_source_nodes(
        *,
        node: ModWebNodeLink,
        all_nodes: tuple[ModWebNodeLink, ...],
        portal: bool,
    ) -> tuple[ModWebNodeLink, ...]:
        """Choose authoritative live operation sources for this view."""

        if not portal:
            return (node,)
        return all_nodes

    @staticmethod
    def _operation_rows(
        *,
        nodes: tuple[ModWebNodeLink, ...],
        snapshots_by_node: dict[str, ModWebNodeOperationSnapshot],
        availability_by_node: dict[str, RemoteNodeAvailability],
    ) -> tuple[ModWebOperationRow, ...]:
        """Merge only current in-memory node snapshots, retaining stale rows for visibility."""

        rows: list[ModWebOperationRow] = []
        for node in nodes:
            node_key = node.node_name.casefold()
            snapshot = snapshots_by_node.get(node_key)
            if snapshot is None:
                continue
            availability = availability_by_node.get(
                node_key, RemoteNodeAvailability.CONNECTING
            )
            rows.extend(
                ModWebOperationRow(
                    node=node, operation=operation, availability=availability
                )
                for operation in snapshot.operations
            )
        return tuple(
            sorted(
                rows,
                key=lambda row: (
                    0 if row.operation.record.state.active else 1,
                    -(
                        row.operation.record.created_at_unix_ms
                        if row.operation.record.state.active
                        else (
                            row.operation.record.finished_at_unix_ms
                            or row.operation.record.created_at_unix_ms
                        )
                    ),
                    row.node.node_name.casefold(),
                    row.operation.record.operation_id,
                ),
            )
        )

    @staticmethod
    def _filtered_operation_rows(
        *,
        rows: tuple[ModWebOperationRow, ...],
        node_name: str,
        kind: str,
        app_name: str,
        state: str,
        activity: ModWebOperationActivityFilter,
    ) -> tuple[ModWebOperationRow, ...]:
        """Apply the shared Operations filters in one place for local and Portal views."""

        node_key = node_name.casefold()
        kind_key = kind.casefold()
        app_key = app_name.casefold()
        state_key = state.casefold()
        return tuple(
            row
            for row in rows
            if (not node_key or row.node.node_name.casefold() == node_key)
            and (not kind_key or row.operation.record.kind.value.casefold() == kind_key)
            and (
                not app_key
                or (
                    row.operation.record.app_name is not None
                    and row.operation.record.app_name.casefold() == app_key
                )
            )
            and (
                not state_key
                or row.operation.record.state.value.casefold() == state_key
            )
            and (
                activity is ModWebOperationActivityFilter.ALL
                or (
                    row.operation.record.state.active
                    if activity is ModWebOperationActivityFilter.ACTIVE
                    else row.operation.record.state.terminal
                )
            )
        )

    async def _operation_detail_from_operations_ui(
        self,
        *,
        node: ModWebNodeLink,
        operation: NodeOperationView,
        user: ModWebUser,
    ) -> NodeOperationView:
        """Load one operation's retained detail from its authoritative node route."""

        record = operation.record
        payload = await self._remote_json_async(
            node=node,
            app_name=None,
            path=f"/operations/{quote(record.operation_id, safe='')}",
            scopes=(NodeApiScope.OPERATIONS_READ,),
            user=user,
        )
        detail = NodeOperationView.from_mapping(payload)
        if detail.record.operation_id != record.operation_id:
            raise ValueError("Operation detail response has an unexpected operation ID.")
        return detail

    async def _cancel_operation_from_operations_ui(
        self,
        *,
        node: ModWebNodeLink,
        operation: NodeOperationView,
        user: ModWebUser,
    ) -> NodeOperationView:
        """Use the operation's policy-derived cancellation authorisation metadata."""

        record = operation.record
        cancellation_scope = operation.cancellation_scope
        if cancellation_scope is None:
            raise ValueError("Operation does not expose cancellation authorisation.")
        query: list[tuple[str, str]] = [("kind", record.kind.value)]
        cancellation_app_name = operation.cancellation_app_name
        if cancellation_app_name is not None:
            query.append(("app_name", cancellation_app_name))
        payload = await self._remote_json_async(
            node=node,
            app_name=cancellation_app_name,
            path=(
                f"/operations/{quote(record.operation_id, safe='')}/cancel?"
                f"{urlencode(query)}"
            ),
            scopes=(cancellation_scope,),
            user=user,
            method="POST",
            json_payload={},
        )
        return NodeOperationView.from_mapping(payload)

    @staticmethod
    def _operation_stream_update_is_significant(
        previous: NodeOperationView | None,
        current: NodeOperationView,
    ) -> bool:
        """Ignore retained-log-only changes when deciding whether detail must reload."""

        if previous is None:
            return True
        return previous.without_log_lines() != current.without_log_lines()

    def _render_operations_ui(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        node: ModWebNodeLink,
        initial_operation_id: str | None = None,
    ) -> None:
        """Render the one Operations component used by both node and Portal system pages."""

        portal = self._node_is_portal(node)
        source_nodes = self._operation_source_nodes(
            node=node,
            all_nodes=self._node_links(),
            portal=portal,
        )
        if not source_nodes:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="No operation nodes",
                description="No operation-capable nodes are configured.",
            )
            return

        source_nodes_by_key = {
            candidate.node_name.casefold(): candidate for candidate in source_nodes
        }
        snapshots_by_node: dict[str, ModWebNodeOperationSnapshot] = {}
        availability_by_node: dict[str, RemoteNodeAvailability] = {
            candidate.node_name.casefold(): (
                RemoteNodeAvailability.CONNECTING
                if portal
                else RemoteNodeAvailability.ONLINE
            )
            for candidate in source_nodes
        }
        selected_node_name = ""
        selected_kind = ""
        selected_app_name = ""
        selected_state = ""
        selected_activity = ModWebOperationActivityFilter.ALL
        expanded_detail_keys: set[_ModWebOperationDetailKey] = set()
        detail_by_key: dict[_ModWebOperationDetailKey, NodeOperationView] = {}
        detail_errors_by_key: dict[_ModWebOperationDetailKey, str] = {}
        detail_refresh_generations: dict[_ModWebOperationDetailKey, int] = {}
        detail_loads_in_progress: set[_ModWebOperationDetailKey] = set()
        detail_load_tasks: set[asyncio.Task[None]] = set()
        initial_operation_expanded = False
        page_closed = False
        loop: AbstractEventLoop = asyncio.get_running_loop()
        unsubscribers: list[Callable[[], None]] = []

        with ui.column().classes("w-full gap-4"):
            with ui.card().classes("mod-card w-full"):
                with ui.column().classes("w-full gap-3 p-4"):
                    with ui.row().classes(
                        "w-full items-center justify-between gap-3 flex-wrap"
                    ):
                        ui.label("Operations").classes(
                            "text-lg font-black mod-title-small"
                        )
                        if portal:
                            self._badge(ui=ui, text="Portal live merge", tone="purple")

                    filter_grid_classes = (
                        "w-full grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5"
                        if portal
                        else "w-full grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4"
                    )
                    with ui.element("div").classes(filter_grid_classes):
                        node_select: Select | None = None
                        if portal:
                            node_select = (
                                ui.select(
                                    {
                                        "": "All nodes",
                                        **{
                                            candidate.node_name: self._operation_node_label(
                                                candidate
                                            )
                                            for candidate in source_nodes
                                        },
                                    },
                                    value="",
                                    label="Node",
                                )
                                .props(_OPERATIONS_FILTER_SELECT_PROPS)
                                .classes(_OPERATIONS_FILTER_SELECT_CLASSES)
                            )
                        kind_select: Select = (
                            ui.select(
                                {"": "All kinds"},
                                value="",
                                label="Kind",
                            )
                            .props(_OPERATIONS_FILTER_SELECT_PROPS)
                            .classes(_OPERATIONS_FILTER_SELECT_CLASSES)
                        )
                        app_select: Select = (
                            ui.select(
                                {"": "All apps"},
                                value="",
                                label="App",
                            )
                            .props(_OPERATIONS_FILTER_SELECT_PROPS)
                            .classes(_OPERATIONS_FILTER_SELECT_CLASSES)
                        )
                        state_select: Select = (
                            ui.select(
                                {
                                    "": "All states",
                                    **{
                                        candidate.value: self._operation_state_label(
                                            candidate
                                        )
                                        for candidate in NodeOperationState
                                    },
                                },
                                value="",
                                label="State",
                            )
                            .props(_OPERATIONS_FILTER_SELECT_PROPS)
                            .classes(_OPERATIONS_FILTER_SELECT_CLASSES)
                        )
                        activity_select: Select = (
                            ui.select(
                                {
                                    ModWebOperationActivityFilter.ALL.value: "Active + completed",
                                    ModWebOperationActivityFilter.ACTIVE.value: "Active only",
                                    ModWebOperationActivityFilter.COMPLETED.value: "Completed only",
                                },
                                value=ModWebOperationActivityFilter.ALL.value,
                                label="Show",
                            )
                            .props(_OPERATIONS_FILTER_SELECT_PROPS)
                            .classes(_OPERATIONS_FILTER_SELECT_CLASSES)
                        )

                    @ui.refreshable
                    def _render_node_availability() -> None:
                        if not portal:
                            return
                        stale_nodes = tuple(
                            candidate
                            for candidate in source_nodes
                            if availability_by_node.get(candidate.node_name.casefold())
                            is not RemoteNodeAvailability.ONLINE
                        )
                        if not stale_nodes:
                            return
                        with ui.row().classes("w-full gap-2 flex-wrap items-center"):
                            for candidate in stale_nodes:
                                availability = availability_by_node.get(
                                    candidate.node_name.casefold(),
                                    RemoteNodeAvailability.CONNECTING,
                                )
                                status_text = (
                                    "Connecting / stale"
                                    if availability is RemoteNodeAvailability.CONNECTING
                                    else "Unavailable / stale"
                                )
                                self._badge(
                                    ui=ui,
                                    text=f"{candidate.label}: {status_text}",
                                    tone="warn"
                                    if availability is RemoteNodeAvailability.CONNECTING
                                    else "red",
                                )

                    @ui.refreshable
                    def _render_operation_rows() -> None:
                        nonlocal initial_operation_expanded
                        rows = self._filtered_operation_rows(
                            rows=self._operation_rows(
                                nodes=source_nodes,
                                snapshots_by_node=snapshots_by_node,
                                availability_by_node=availability_by_node,
                            ),
                            node_name=selected_node_name,
                            kind=selected_kind,
                            app_name=selected_app_name,
                            state=selected_state,
                            activity=selected_activity,
                        )
                        if not rows:
                            ui.label(
                                "No operations match the current filters."
                            ).classes("mod-subtitle text-sm py-3")
                            return
                        for row in rows:
                            detail_key = row.detail_key
                            if (
                                not initial_operation_expanded
                                and initial_operation_id is not None
                                and row.operation.record.operation_id
                                == initial_operation_id
                            ):
                                initial_operation_expanded = True
                                expanded_detail_keys.add(detail_key)
                            if (
                                detail_key in expanded_detail_keys
                                and detail_key not in detail_by_key
                                and detail_key not in detail_errors_by_key
                            ):
                                _start_operation_detail_load(detail_key)
                            self._render_operation_row(
                                ui=ui,
                                row=row,
                                user=user,
                                show_node=portal,
                                detail_operation=detail_by_key.get(detail_key),
                                details_open=detail_key in expanded_detail_keys,
                                details_loading=(
                                    detail_key in detail_loads_in_progress
                                ),
                                detail_error=detail_errors_by_key.get(detail_key),
                                on_details_open_changed=(
                                    lambda is_open, current_row=row: (
                                        _set_operation_details_open(
                                            row=current_row,
                                            is_open=is_open,
                                        )
                                    )
                                ),
                                on_cancelled=lambda operation, row_node=row.node: (
                                    _apply_cancelled_operation(
                                        node=row_node,
                                        operation=operation,
                                    )
                                ),
                            )

                    def _refresh_filter_options() -> None:
                        operations = tuple(
                            row.operation
                            for row in self._operation_rows(
                                nodes=source_nodes,
                                snapshots_by_node=snapshots_by_node,
                                availability_by_node=availability_by_node,
                            )
                        )
                        self._set_select_options(
                            kind_select,
                            {
                                "": "All kinds",
                                **{
                                    operation.record.kind.value: operation.kind_label
                                    for operation in operations
                                },
                            },
                        )
                        self._set_select_options(
                            app_select,
                            {
                                "": "All apps",
                                **{
                                    operation.record.app_name: operation.record.app_name
                                    for operation in operations
                                    if operation.record.app_name is not None
                                },
                            },
                        )

                    def _refresh_rows() -> None:
                        _refresh_filter_options()
                        _render_node_availability.refresh()
                        _render_operation_rows.refresh()

                    def _operation_row_for_detail_key(
                        detail_key: _ModWebOperationDetailKey,
                    ) -> ModWebOperationRow | None:
                        detail_node = source_nodes_by_key.get(detail_key.node_key)
                        snapshot = snapshots_by_node.get(detail_key.node_key)
                        if detail_node is None or snapshot is None:
                            return None
                        for operation in snapshot.operations:
                            if operation.record.operation_id == detail_key.operation_id:
                                return ModWebOperationRow(
                                    node=detail_node,
                                    operation=operation,
                                    availability=availability_by_node.get(
                                        detail_key.node_key,
                                        RemoteNodeAvailability.CONNECTING,
                                    ),
                                )
                        return None

                    def _discard_operation_detail(
                        detail_key: _ModWebOperationDetailKey,
                    ) -> None:
                        expanded_detail_keys.discard(detail_key)
                        detail_by_key.pop(detail_key, None)
                        detail_errors_by_key.pop(detail_key, None)
                        if detail_key in detail_loads_in_progress:
                            detail_refresh_generations[detail_key] = (
                                detail_refresh_generations.get(detail_key, 0) + 1
                            )
                        else:
                            detail_refresh_generations.pop(detail_key, None)

                    def _invalidate_operation_detail(
                        detail_key: _ModWebOperationDetailKey,
                    ) -> None:
                        if (
                            detail_key not in expanded_detail_keys
                            and detail_key not in detail_by_key
                            and detail_key not in detail_errors_by_key
                            and detail_key not in detail_loads_in_progress
                        ):
                            return
                        detail_by_key.pop(detail_key, None)
                        detail_errors_by_key.pop(detail_key, None)
                        detail_refresh_generations[detail_key] = (
                            detail_refresh_generations.get(detail_key, 0) + 1
                        )
                        if detail_key in expanded_detail_keys:
                            _start_operation_detail_load(detail_key)

                    async def _load_operation_detail(
                        detail_key: _ModWebOperationDetailKey,
                    ) -> None:
                        try:
                            while (
                                not page_closed
                                and detail_key in expanded_detail_keys
                            ):
                                row = _operation_row_for_detail_key(detail_key)
                                if row is None:
                                    return
                                generation = detail_refresh_generations.get(
                                    detail_key,
                                    0,
                                )
                                try:
                                    detail = (
                                        await self._operation_detail_from_operations_ui(
                                            node=row.node,
                                            operation=row.operation,
                                            user=user,
                                        )
                                    )
                                except asyncio.CancelledError:
                                    raise
                                except Exception as xcp:
                                    if generation != detail_refresh_generations.get(
                                        detail_key,
                                        0,
                                    ):
                                        continue
                                    detail_errors_by_key[detail_key] = (
                                        str(xcp) or type(xcp).__name__
                                    )
                                    return
                                if generation != detail_refresh_generations.get(
                                    detail_key,
                                    0,
                                ):
                                    continue
                                if (
                                    page_closed
                                    or detail_key not in expanded_detail_keys
                                ):
                                    return
                                detail_by_key[detail_key] = detail
                                detail_errors_by_key.pop(detail_key, None)
                                return
                        finally:
                            detail_loads_in_progress.discard(detail_key)
                            if (
                                detail_key not in expanded_detail_keys
                                and detail_key not in detail_by_key
                            ):
                                detail_refresh_generations.pop(detail_key, None)
                            if not page_closed:
                                _refresh_rows()

                    def _forget_operation_detail_task(
                        task: asyncio.Task[None],
                    ) -> None:
                        detail_load_tasks.discard(task)

                    def _start_operation_detail_load(
                        detail_key: _ModWebOperationDetailKey,
                    ) -> None:
                        if (
                            page_closed
                            or detail_key not in expanded_detail_keys
                            or detail_key in detail_loads_in_progress
                        ):
                            return
                        detail_loads_in_progress.add(detail_key)
                        task = asyncio.create_task(
                            _load_operation_detail(detail_key),
                            name=(
                                "mod-web-operation-detail:"
                                f"{detail_key.node_key}:{detail_key.operation_id}"
                            ),
                        )
                        detail_load_tasks.add(task)
                        task.add_done_callback(_forget_operation_detail_task)

                    def _set_operation_details_open(
                        *,
                        row: ModWebOperationRow,
                        is_open: bool,
                    ) -> None:
                        if page_closed:
                            return
                        detail_key = row.detail_key
                        if is_open == (detail_key in expanded_detail_keys):
                            return
                        if is_open:
                            expanded_detail_keys.add(detail_key)
                            _invalidate_operation_detail(detail_key)
                        else:
                            _discard_operation_detail(detail_key)
                        _refresh_rows()

                    def _set_node_filter(event: object) -> None:
                        nonlocal selected_node_name
                        selected_node_name = _value_as_text(event).strip()
                        _refresh_rows()

                    def _set_kind_filter(event: object) -> None:
                        nonlocal selected_kind
                        selected_kind = _value_as_text(event).strip()
                        _refresh_rows()

                    def _set_app_filter(event: object) -> None:
                        nonlocal selected_app_name
                        selected_app_name = _value_as_text(event).strip()
                        _refresh_rows()

                    def _set_state_filter(event: object) -> None:
                        nonlocal selected_state
                        selected_state = _value_as_text(event).strip()
                        _refresh_rows()

                    def _set_activity_filter(event: object) -> None:
                        nonlocal selected_activity
                        try:
                            selected_activity = ModWebOperationActivityFilter(
                                _value_as_text(event).strip()
                            )
                        except ValueError:
                            selected_activity = ModWebOperationActivityFilter.ALL
                        _refresh_rows()

                    if node_select is not None:
                        node_select.on("update:model-value", _set_node_filter)
                    kind_select.on("update:model-value", _set_kind_filter)
                    app_select.on("update:model-value", _set_app_filter)
                    state_select.on("update:model-value", _set_state_filter)
                    activity_select.on("update:model-value", _set_activity_filter)

                    def _apply_snapshot(snapshot: ModWebNodeOperationSnapshot) -> None:
                        if page_closed:
                            return
                        node_key = snapshot.node_name.casefold()
                        previous_snapshot = snapshots_by_node.get(node_key)
                        previous_operations_by_id = (
                            {}
                            if previous_snapshot is None
                            else {
                                operation.record.operation_id: operation
                                for operation in previous_snapshot.operations
                            }
                        )
                        current_operation_ids = {
                            operation.record.operation_id
                            for operation in snapshot.operations
                        }
                        snapshots_by_node[node_key] = snapshot
                        availability_by_node[node_key] = (
                            RemoteNodeAvailability.ONLINE
                        )
                        for operation in snapshot.operations:
                            detail_key = _ModWebOperationDetailKey(
                                node_key=node_key,
                                operation_id=operation.record.operation_id,
                            )
                            if self._operation_stream_update_is_significant(
                                previous_operations_by_id.get(
                                    operation.record.operation_id
                                ),
                                operation,
                            ):
                                _invalidate_operation_detail(detail_key)
                        for operation_id in previous_operations_by_id.keys() - current_operation_ids:
                            _discard_operation_detail(
                                _ModWebOperationDetailKey(
                                    node_key=node_key,
                                    operation_id=operation_id,
                                )
                            )
                        _refresh_rows()

                    def _handle_snapshot(snapshot: ModWebNodeOperationSnapshot) -> None:
                        loop.call_soon_threadsafe(lambda: _apply_snapshot(snapshot))

                    def _apply_monitor_snapshot(
                        snapshot: RemoteNodeMonitorSnapshot,
                    ) -> None:
                        if page_closed:
                            return
                        availability_by_node[snapshot.node.node_name.casefold()] = (
                            snapshot.availability
                        )
                        _refresh_rows()

                    def _handle_monitor_snapshot(
                        snapshot: RemoteNodeMonitorSnapshot,
                    ) -> None:
                        loop.call_soon_threadsafe(
                            lambda: _apply_monitor_snapshot(snapshot)
                        )

                    def _apply_cancelled_operation(
                        *,
                        node: ModWebNodeLink,
                        operation: NodeOperationView,
                    ) -> None:
                        node_key = node.node_name.casefold()
                        current_snapshot = snapshots_by_node.get(node_key)
                        event = NodeOperationStreamEvent(
                            kind=NodeOperationStreamEventKind.UPDATED,
                            node_name=node.node_name,
                            operation=operation,
                        )
                        _apply_snapshot(
                            ModWebNodeOperationSnapshot.apply_event(
                                current_snapshot,
                                event,
                            )
                        )

                    for source_node in source_nodes:
                        unsubscribers.append(
                            self._create_remote_operation_subscription(
                                node=source_node,
                                user=user,
                                on_update=_handle_snapshot,
                            )
                        )
                        if portal:
                            unsubscribers.append(
                                self._subscribe_remote_node_monitor(
                                    node=source_node,
                                    on_update=_handle_monitor_snapshot,
                                )
                            )
                    _refresh_filter_options()
                    _render_node_availability()
                    _render_operation_rows()

        def _cleanup() -> None:
            nonlocal page_closed
            page_closed = True
            for task in tuple(detail_load_tasks):
                task.cancel()
            for unsubscribe in unsubscribers:
                unsubscribe()

        self._register_client_cleanup(ui=ui, cleanup=_cleanup)

    def _render_operation_row(
        self,
        *,
        ui: ModWebUi,
        row: ModWebOperationRow,
        user: ModWebUser,
        show_node: bool,
        detail_operation: NodeOperationView | None,
        details_open: bool,
        details_loading: bool,
        detail_error: str | None,
        on_details_open_changed: Callable[[bool], None],
        on_cancelled: Callable[[NodeOperationView], None],
    ) -> None:
        """Render one live summary with detail and logs loaded only on expansion."""

        operation = row.operation
        record = operation.record
        state_tone = self._operation_state_tone(record.state)
        with ui.card().classes("mod-card w-full"):
            with ui.column().classes("w-full gap-3 p-4"):
                with ui.row().classes(
                    "w-full items-start justify-between gap-3 flex-wrap"
                ):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.icon(self._operation_state_icon(record.state)).classes(
                            f"{self._operation_state_icon_class(record.state)} text-xl"
                        )
                        ui.label(self._operation_state_label(record.state)).classes(
                            self._badge_class_name(tone=state_tone)
                        )
                        ui.label(operation.kind_label).classes(
                            self._badge_class_name(tone="grey")
                        )
                        if row.stale:
                            self._badge(ui=ui, text="Stale node data", tone="warn")
                    if operation.cancellable:

                        async def _cancel() -> None:
                            try:
                                updated = (
                                    await self._cancel_operation_from_operations_ui(
                                        node=row.node,
                                        operation=operation,
                                        user=user,
                                    )
                                )
                            except Exception as xcp:
                                ui.notify(
                                    f"Could not cancel operation: {xcp}",
                                    type="negative",
                                )
                                return
                            on_cancelled(updated)
                            ui.notify("Cancellation requested.", type="info")

                        ui.button("Cancel", on_click=_cancel).classes(
                            "mod-list-button danger"
                        )
                with ui.column().classes("gap-1"):
                    ui.label(record.subject).classes("font-bold mod-title-small")
                    with ui.row().classes("gap-2 flex-wrap items-center"):
                        if record.app_name is not None:
                            ui.label(record.app_name).classes("mod-subtitle text-xs")
                        if show_node:
                            ui.label(
                                f"Node: {self._operation_node_label(row.node)}"
                            ).classes("mod-subtitle text-xs")
                        if record.phase is not None:
                            ui.label(f"Phase: {record.phase}").classes(
                                "mod-subtitle text-xs"
                            )
                ui.label(record.summary).classes("text-sm")
                if record.progress_percent is not None:
                    with ui.element("div").classes(
                        "w-full rounded overflow-hidden bg-slate-700 h-2"
                    ):
                        ui.element("div").classes("h-full bg-accent").style(
                            f"width: {record.progress_percent:.1f}%"
                        )
                    ui.label(f"{record.progress_percent:.1f}%").classes(
                        "mod-subtitle text-xs"
                    )
                with ui.row().classes(
                    "w-full gap-x-4 gap-y-1 flex-wrap mod-subtitle text-xs"
                ):
                    ui.label(
                        "Requester: "
                        + (
                            str(record.requested_by_user_id)
                            if record.requested_by_user_id is not None
                            else "System"
                        )
                    )
                    ui.label(
                        f"Created: {self._operation_timestamp(record.created_at_unix_ms)}"
                    )
                    if record.started_at_unix_ms is not None:
                        ui.label(
                            f"Started: {self._operation_timestamp(record.started_at_unix_ms)}"
                        )
                    if record.finished_at_unix_ms is not None:
                        ui.label(
                            f"Finished: {self._operation_timestamp(record.finished_at_unix_ms)}"
                        )
                details: Element = ui.element("details").classes(
                    "w-full mod-operation-details"
                )
                if details_open:
                    details.props("open")

                def _details_toggled(event: object) -> None:
                    on_details_open_changed(_value_as_bool(event))

                details.on(
                    "toggle",
                    _details_toggled,
                    js_handler="(event) => emit(event.target.open)",
                )
                with details:
                    with ui.element("summary").classes(
                        "cursor-pointer text-sm font-medium"
                    ):
                        ui.label("Details and logs")
                    with ui.column().classes("w-full gap-2 pt-2"):
                        if details_loading:
                            ui.label("Loading details and retained logs…").classes(
                                "mod-subtitle text-sm"
                            )
                        elif detail_error is not None:
                            ui.label(
                                f"Could not load details and retained logs: {detail_error}"
                            ).classes("text-negative text-sm")
                        elif detail_operation is None:
                            ui.label("Loading details and retained logs…").classes(
                                "mod-subtitle text-sm"
                            )
                        else:
                            detail_record = detail_operation.record
                            if detail_record.detail is not None:
                                ui.label(detail_record.detail).classes(
                                    "text-sm whitespace-pre-wrap"
                                )
                            if detail_record.log_lines:
                                ui.html(
                                    '<pre class="mod-operation-log">'
                                    f"{escape(chr(10).join(detail_record.log_lines))}"
                                    "</pre>"
                                ).classes("w-full")
                            elif detail_record.detail is None:
                                ui.label("No additional detail or retained logs.").classes(
                                    "mod-subtitle text-sm"
                                )
                            else:
                                ui.label("No retained log lines.").classes(
                                    "mod-subtitle text-sm"
                                )

    def _render_app_active_operation_summary(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        node_name: str,
        app_name: str,
    ) -> None:
        """Show contextual app work from the same shared node operation websocket."""

        if not self._user_has_level(user, Power_Level.sudo):
            return
        node = self._remote_node_link(node_name)
        current_snapshot: ModWebNodeOperationSnapshot | None = None
        page_closed = False
        loop: AbstractEventLoop = asyncio.get_running_loop()

        @ui.refreshable
        def _render_summary() -> None:
            if current_snapshot is None:
                return
            active_operations = tuple(
                operation
                for operation in current_snapshot.operations
                if operation.record.state.active
                and operation.record.app_name is not None
                and operation.record.app_name.casefold() == app_name.casefold()
            )
            if not active_operations:
                return
            with ui.card().classes("mod-card w-full mod-active-operation-summary"):
                with ui.column().classes("w-full gap-2 p-3"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.icon("sync").classes("text-purple")
                        ui.label(
                            f"{len(active_operations)} active operation"
                            + ("" if len(active_operations) == 1 else "s")
                        ).classes("font-bold text-sm")
                    for operation in active_operations:
                        with ui.row().classes(
                            "w-full items-center justify-between gap-3 flex-wrap"
                        ):
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                ui.label(operation.kind_label).classes(
                                    self._badge_class_name(tone="grey")
                                )
                                ui.label(operation.record.summary).classes(
                                    "mod-subtitle text-xs"
                                )
                            ui.link(
                                "View operation",
                                operation_detail_path(
                                    node_name=node.node_name,
                                    operation_id=operation.record.operation_id,
                                ),
                            ).classes("mod-list-button secondary text-sm")

        def _apply(snapshot: ModWebNodeOperationSnapshot) -> None:
            nonlocal current_snapshot
            if page_closed:
                return
            current_snapshot = snapshot
            _render_summary.refresh()

        def _handle(snapshot: ModWebNodeOperationSnapshot) -> None:
            loop.call_soon_threadsafe(lambda: _apply(snapshot))

        _render_summary()
        unsubscribe = self._create_remote_operation_subscription(
            node=node,
            user=user,
            on_update=_handle,
        )

        def _cleanup() -> None:
            nonlocal page_closed
            page_closed = True
            unsubscribe()

        self._register_client_cleanup(ui=ui, cleanup=_cleanup)

    @staticmethod
    def _set_select_options(select: "Select", options: dict[str, str]) -> None:
        """Update dynamic filter options while remaining friendly to lightweight UI fakes."""

        set_options = getattr(select, "set_options", None)
        if callable(set_options):
            set_options(options)
            return
        setattr(select, "options", options)
        update = getattr(select, "update", None)
        if callable(update):
            update()


__all__: tuple[str, ...] = (
    "ModWebOperationActivityFilter",
    "ModWebOperationRow",
    "ModWebOperationsMixin",
    "operation_detail_path",
)
