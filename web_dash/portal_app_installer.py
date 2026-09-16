"""Portal-wide orchestration for node-local app-install operations.

The node API remains the authority for executing an install and protecting its
local resources.  This service adds the Portal policy that only one install may
be active across all app nodes at a time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from node_api.app_installer import NodeAppInstallStatus
from node_api.operation_service import NodeOperationView
from node_api.operations import NodeOperationKind, NodeOperationState
from node_auth import NodeApiScope

from .constants import _REMOTE_NODE_OVERVIEW_REQUEST_TIMEOUT_SECONDS, log
from .runtime_imports import asyncio, cast, config
from .service_base import ModWebServiceSupport
from .types import ModWebNodeLink

_PORTAL_APP_INSTALL_RECOVERY_SECONDS = 1.0
PORTAL_APP_INSTALL_CONFLICT_MESSAGE = (
    "Another app install is active, or Portal cannot yet verify that it is safe to start one."
)


class PortalAppInstallConflictError(RuntimeError):
    """Raised when the Portal-wide app-install lease is already held."""

    def __init__(self) -> None:
        super().__init__(PORTAL_APP_INSTALL_CONFLICT_MESSAGE)


@dataclass(frozen=True, slots=True)
class PortalAppInstallOperation:
    """The minimal authoritative operation data needed to recover a lease."""

    node_name: str
    operation_id: str
    state: NodeOperationState

    def __post_init__(self) -> None:
        if not self.node_name.strip():
            raise ValueError("App-install operation node name must not be blank.")
        if not self.operation_id.strip():
            raise ValueError("App-install operation ID must not be blank.")


@dataclass(frozen=True, slots=True)
class PortalAppInstallLease:
    """The in-memory Portal lease for one install, start, or recovery conflict."""

    owner_token: str | None
    node_name: str | None
    operation_id: str | None
    starting: bool

    def __post_init__(self) -> None:
        owner_token = self.owner_token
        if owner_token is not None and not owner_token.strip():
            raise ValueError("App-install lease owner token must not be blank.")
        node_name = self.node_name
        if node_name is not None and not node_name.strip():
            raise ValueError("App-install lease node name must not be blank.")
        operation_id = self.operation_id
        if operation_id is not None and not operation_id.strip():
            raise ValueError("App-install lease operation ID must not be blank.")
        if self.starting:
            if node_name is None or operation_id is not None:
                raise ValueError("A starting app-install lease must have only a target node.")
            return
        if node_name is None:
            if owner_token is not None or operation_id is not None:
                raise ValueError("An unresolved app-install lease cannot identify one operation.")
            return
        if operation_id is None:
            raise ValueError("An active app-install lease must identify its operation.")

    @property
    def unresolved_recovery_conflict(self) -> bool:
        """Whether several installs or unavailable recovery data block new starts."""

        return not self.starting and self.node_name is None


class PortalAppInstallCoordinator:
    """Own the one in-memory Portal-wide app-install lease.

    This intentionally has no distributed lock or durable state.  It derives
    recovery state from the nodes' durable operation records instead.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._lease: PortalAppInstallLease | None = None

    def current(self) -> PortalAppInstallLease | None:
        """Return the current lease snapshot, if installing is globally blocked."""

        with self._lock:
            return self._lease

    def held_by_other(self, *, owner_token: str) -> bool:
        """Return whether another install start or recovery lease holds policy."""

        with self._lock:
            lease = self._lease
            return lease is not None and lease.owner_token != owner_token

    def acquire_start(self, *, owner_token: str, node_name: str) -> None:
        """Atomically reserve the global lease before a node start request."""

        with self._lock:
            if self._lease is not None:
                raise PortalAppInstallConflictError()
            self._lease = PortalAppInstallLease(
                owner_token=owner_token,
                node_name=node_name,
                operation_id=None,
                starting=True,
            )

    def associate_operation(
        self,
        *,
        owner_token: str,
        node_name: str,
        operation_id: str,
    ) -> None:
        """Bind a successfully started node operation to its reserved lease."""

        with self._lock:
            lease = self._lease
            if (
                lease is None
                or not lease.starting
                or lease.owner_token != owner_token
                or lease.node_name is None
                or lease.node_name.casefold() != node_name.casefold()
            ):
                raise RuntimeError("App-install lease was lost before the operation was associated.")
            self._lease = PortalAppInstallLease(
                owner_token=owner_token,
                node_name=node_name,
                operation_id=operation_id,
                starting=False,
            )

    def release_failed_start(self, *, owner_token: str) -> bool:
        """Release a reservation only when its node start request failed."""

        with self._lock:
            lease = self._lease
            if lease is None or not lease.starting or lease.owner_token != owner_token:
                return False
            self._lease = None
            return True

    def observe_operation(
        self,
        *,
        node_name: str,
        operation_id: str,
        state: NodeOperationState,
    ) -> bool:
        """Release a matching lease when its authoritative operation is terminal."""

        if not state.terminal:
            return False
        return self.release_terminal_operation(
            node_name=node_name,
            operation_id=operation_id,
        )

    def release_terminal_operation(self, *, node_name: str, operation_id: str) -> bool:
        """Release a matching lease after an authoritative terminal status."""

        with self._lock:
            lease = self._lease
            if (
                lease is None
                or lease.starting
                or lease.node_name is None
                or lease.operation_id is None
                or lease.node_name.casefold() != node_name.casefold()
                or lease.operation_id != operation_id
            ):
                return False
            self._lease = None
            return True

    def block_until_recovered(self) -> None:
        """Fail closed while Portal cannot obtain an authoritative aggregate."""

        with self._lock:
            if self._lease is None:
                self._lease = PortalAppInstallLease(
                    owner_token=None,
                    node_name=None,
                    operation_id=None,
                    starting=False,
                )

    def mark_start_outcome_unknown(self, *, owner_token: str) -> bool:
        """Keep starts blocked when a completed node request cannot be associated."""

        with self._lock:
            lease = self._lease
            if lease is None or not lease.starting or lease.owner_token != owner_token:
                return False
            self._lease = PortalAppInstallLease(
                owner_token=None,
                node_name=None,
                operation_id=None,
                starting=False,
            )
            return True

    def reconcile_operations(
        self,
        operations: Sequence[PortalAppInstallOperation],
    ) -> tuple[PortalAppInstallOperation, ...]:
        """Rebuild the lease and return any conflicting active install operations."""

        active_operations_by_key: dict[tuple[str, str], PortalAppInstallOperation] = {
            (operation.node_name.casefold(), operation.operation_id): operation
            for operation in operations
            if operation.state.active
        }
        active_operations = tuple(active_operations_by_key.values())
        with self._lock:
            lease = self._lease
            if lease is not None and lease.starting:
                return ()
            if not active_operations:
                self._lease = None
                return ()
            if len(active_operations) == 1:
                operation = active_operations[0]
                owner_token = (
                    lease.owner_token
                    if (
                        lease is not None
                        and lease.node_name is not None
                        and lease.operation_id == operation.operation_id
                        and lease.node_name.casefold() == operation.node_name.casefold()
                    )
                    else None
                )
                self._lease = PortalAppInstallLease(
                    owner_token=owner_token,
                    node_name=operation.node_name,
                    operation_id=operation.operation_id,
                    starting=False,
                )
                return ()
            self._lease = PortalAppInstallLease(
                owner_token=None,
                node_name=None,
                operation_id=None,
                starting=False,
            )
            return tuple(
                sorted(
                    active_operations,
                    key=lambda operation: (
                        operation.node_name.casefold(),
                        operation.operation_id,
                    ),
                )
            )


class ModWebPortalAppInstallerMixin(ModWebServiceSupport):
    """Coordinate Portal app-install starts and recovery outside the wizard UI."""

    _portal_app_install_coordinator: PortalAppInstallCoordinator = cast(
        PortalAppInstallCoordinator,
        cast(object, None),
    )
    _portal_app_install_recovery_task: asyncio.Task[None] | None = None
    _portal_app_install_recovery_lock: asyncio.Lock = cast(
        asyncio.Lock,
        cast(object, None),
    )
    _portal_app_install_multiple_warning_active = False
    _portal_app_install_recovery_error: str | None = None

    def _portal_app_install_lease_held_by_other(self, *, owner_token: str) -> bool:
        return self._portal_app_install_coordinator.held_by_other(owner_token=owner_token)

    async def _start_portal_app_install(
        self,
        *,
        node: ModWebNodeLink,
        owner_token: str | None,
        start_install: Callable[[], Awaitable[NodeAppInstallStatus]],
    ) -> NodeAppInstallStatus:
        """Start one node install while holding the Portal-wide lease."""

        try:
            await self._recover_portal_app_install_lease()
        except Exception as xcp:
            self._portal_app_install_coordinator.block_until_recovered()
            raise PortalAppInstallConflictError() from xcp

        lease_owner_token = owner_token or uuid4().hex
        self._portal_app_install_coordinator.acquire_start(
            owner_token=lease_owner_token,
            node_name=node.node_name,
        )
        try:
            status = await start_install()
        except asyncio.CancelledError:
            # A cancelled HTTP task can still have reached the node. Reconcile
            # instead of freeing the global policy before that outcome is known.
            self._portal_app_install_coordinator.mark_start_outcome_unknown(
                owner_token=lease_owner_token
            )
            raise
        except Exception:
            self._portal_app_install_coordinator.release_failed_start(
                owner_token=lease_owner_token
            )
            raise

        try:
            if status.node.casefold() != node.node_name.casefold():
                raise ValueError("The selected node returned an install for a different node.")
            self._portal_app_install_coordinator.associate_operation(
                owner_token=lease_owner_token,
                node_name=node.node_name,
                operation_id=status.job_id,
            )
            if not status.running:
                self._portal_app_install_coordinator.release_terminal_operation(
                    node_name=node.node_name,
                    operation_id=status.job_id,
                )
        except Exception:
            # The remote node may already own an operation.  Keep the reservation
            # until the next authoritative Operations reconciliation instead of
            # risking another start in the gap.
            self._portal_app_install_coordinator.mark_start_outcome_unknown(
                owner_token=lease_owner_token
            )
            raise
        return status

    def _observe_portal_app_install_operation(self, operation: PortalAppInstallOperation) -> bool:
        """Apply an authoritative operation update to the Portal lease."""

        return self._portal_app_install_coordinator.observe_operation(
            node_name=operation.node_name,
            operation_id=operation.operation_id,
            state=operation.state,
        )

    def _start_portal_app_install_recovery(self) -> None:
        """Start the Portal's durable-operation reconciliation loop once."""

        if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.PORTAL:
            return
        task = self._portal_app_install_recovery_task
        if task is not None and not task.done():
            return
        self._portal_app_install_recovery_task = asyncio.create_task(
            self._run_portal_app_install_recovery(),
            name="portal-app-install-recovery",
        )

    async def _stop_portal_app_install_recovery(self) -> None:
        """Stop the Portal reconciliation loop during dashboard shutdown."""

        task = self._portal_app_install_recovery_task
        self._portal_app_install_recovery_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run_portal_app_install_recovery(self) -> None:
        while not self._shutting_down:
            try:
                await self._recover_portal_app_install_lease()
                self._portal_app_install_recovery_error = None
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                self._portal_app_install_coordinator.block_until_recovered()
                error_detail = f"{type(xcp).__name__}: {xcp}"
                if (
                    not self._shutting_down
                    and self._portal_app_install_recovery_error != error_detail
                ):
                    log.warning(
                        "Portal app-install recovery could not read Operations: %s",
                        xcp,
                    )
                self._portal_app_install_recovery_error = error_detail
            await asyncio.sleep(_PORTAL_APP_INSTALL_RECOVERY_SECONDS)

    async def _recover_portal_app_install_lease(self) -> None:
        """Reconcile the in-memory lease with each app node's Operations records."""

        async with self._portal_app_install_recovery_lock:
            operations = await self._portal_app_install_operations()
            multiple_operations = self._portal_app_install_coordinator.reconcile_operations(
                operations
            )
            if multiple_operations:
                if not self._portal_app_install_multiple_warning_active:
                    log.warning(
                        "Portal found multiple active app installs; blocking new installs until all finish: %s",
                        ", ".join(
                            f"{operation.node_name}/{operation.operation_id}"
                            for operation in multiple_operations
                        ),
                    )
                self._portal_app_install_multiple_warning_active = True
            else:
                self._portal_app_install_multiple_warning_active = False

    async def _portal_app_install_operations(self) -> tuple[PortalAppInstallOperation, ...]:
        """Read complete operation summaries from all install-capable Portal nodes."""

        nodes = tuple(
            node for node in self._node_links() if not self._node_is_portal(node)
        )
        if not nodes:
            return ()
        results = await asyncio.gather(
            *(self._portal_node_app_install_operations(node=node) for node in nodes),
            return_exceptions=True,
        )
        failures = tuple(
            (node, result)
            for node, result in zip(nodes, results, strict=True)
            if isinstance(result, BaseException)
        )
        if failures:
            detail = "; ".join(
                f"{node.node_name}: {type(error).__name__}: {error}"
                for node, error in failures
            )
            raise RuntimeError(f"Could not read app-install Operations from every node: {detail}")
        operations: list[PortalAppInstallOperation] = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            operations.extend(result)
        return tuple(operations)

    async def _portal_node_app_install_operations(
        self,
        *,
        node: ModWebNodeLink,
    ) -> tuple[PortalAppInstallOperation, ...]:
        """Read one node's app-install operations through its generic read API."""

        if node.is_current:
            views = self._node_api.operation_api.list_operations()
        else:
            payload = await self._remote_json_async(
                node=node,
                app_name=None,
                path="/operations",
                scopes=(NodeApiScope.OPERATIONS_READ,),
                user=None,
                timeout=_REMOTE_NODE_OVERVIEW_REQUEST_TIMEOUT_SECONDS,
            )
            views = self._operation_views_from_payload(payload)
        operations: list[PortalAppInstallOperation] = []
        for view in views:
            record = view.record
            if record.kind is not NodeOperationKind.APP_INSTALL:
                continue
            if record.node_name.casefold() != node.node_name.casefold():
                raise ValueError("A node returned an operation for a different node.")
            operations.append(
                PortalAppInstallOperation(
                    node_name=node.node_name,
                    operation_id=record.operation_id,
                    state=record.state,
                )
            )
        return tuple(operations)

    @staticmethod
    def _operation_views_from_payload(
        payload: Mapping[str, object],
    ) -> tuple[NodeOperationView, ...]:
        raw_operations = payload.get("operations")
        if not isinstance(raw_operations, list):
            raise ValueError("Node operation list is invalid.")
        views: list[NodeOperationView] = []
        for raw_operation in cast(list[object], raw_operations):
            if not isinstance(raw_operation, Mapping):
                raise ValueError("Node operation list contains an invalid record.")
            views.append(
                NodeOperationView.from_mapping(cast(Mapping[str, object], raw_operation))
            )
        return tuple(views)


__all__: tuple[str, ...] = (
    "ModWebPortalAppInstallerMixin",
    "PORTAL_APP_INSTALL_CONFLICT_MESSAGE",
    "PortalAppInstallConflictError",
    "PortalAppInstallCoordinator",
    "PortalAppInstallLease",
    "PortalAppInstallOperation",
)
