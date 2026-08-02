"""Portal-owned remote node state supervision."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from .runtime_imports import NodeAppEntry, NodeStateStreamEvent, NodeSystemSummary
from .types import ModWebNodeLink


log = logging.getLogger(__name__)


class RemoteNodeAvailability(Enum):
    """The Portal's latest ability to obtain state from a node."""

    CONNECTING = "connecting"
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class RemoteNodeMonitorSnapshot:
    """Latest cached node state, retained while a node reconnects."""

    node: ModWebNodeLink
    availability: RemoteNodeAvailability
    app_entries: tuple[NodeAppEntry, ...] | None = None
    system_summary: NodeSystemSummary | None = None
    last_event: NodeStateStreamEvent | None = None
    last_error: Exception | None = None


RemoteNodeStateListener = Callable[[NodeStateStreamEvent], None]
RemoteNodeOnlineListener = Callable[[], None]
RemoteNodeOfflineListener = Callable[[Exception], None]
RemoteNodeMonitorListener = Callable[
    [RemoteNodeStateListener, RemoteNodeOnlineListener, RemoteNodeOfflineListener], Awaitable[None]
]
RemoteNodeMonitorCallback = Callable[[RemoteNodeMonitorSnapshot], None]


class RemoteNodeMonitor:
    """Keeps one Portal-owned state connection and cache for a remote node."""

    def __init__(self, *, node: ModWebNodeLink, listener: RemoteNodeMonitorListener) -> None:
        self._listener = listener
        self._snapshot = RemoteNodeMonitorSnapshot(node=node, availability=RemoteNodeAvailability.CONNECTING)
        self._callbacks: dict[str, RemoteNodeMonitorCallback] = {}
        self._task: asyncio.Task[None] | None = None
        self._lock = threading.RLock()

    @property
    def snapshot(self) -> RemoteNodeMonitorSnapshot:
        with self._lock:
            return self._snapshot

    def start(self) -> None:
        with self._lock:
            task = self._task
            if task is not None and not task.done():
                return
            self._task = asyncio.create_task(self._run(), name=f"remote-node-monitor:{self._snapshot.node.node_name}")

    def subscribe(self, callback: RemoteNodeMonitorCallback, *, replay: bool = True) -> Callable[[], None]:
        subscription_id = uuid.uuid4().hex
        with self._lock:
            self._callbacks[subscription_id] = callback
            snapshot = self._snapshot
        if replay:
            self._invoke_callback(callback=callback, snapshot=snapshot)

        def _unsubscribe() -> None:
            with self._lock:
                self._callbacks.pop(subscription_id, None)

        return _unsubscribe

    async def close(self) -> None:
        with self._lock:
            task = self._task
            self._task = None
            self._callbacks.clear()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            await self._listener(self._accept_event, self._mark_online, self._mark_offline)
            self._mark_offline(ConnectionError("Remote node state listener stopped."))
        except asyncio.CancelledError:
            raise
        except Exception as xcp:
            log.exception("Remote node monitor stopped unexpectedly: node=%s", self._snapshot.node.node_name)
            self._mark_offline(xcp)

    def _accept_event(self, event: NodeStateStreamEvent) -> None:
        with self._lock:
            current = self._snapshot
            next_snapshot = RemoteNodeMonitorSnapshot(
                node=current.node,
                availability=RemoteNodeAvailability.ONLINE,
                app_entries=event.app_entries if event.app_entries is not None else current.app_entries,
                system_summary=event.system_summary if event.system_summary is not None else current.system_summary,
                last_event=event,
            )
        self._replace_snapshot(next_snapshot)

    def _mark_online(self) -> None:
        with self._lock:
            current = self._snapshot
            if current.availability is RemoteNodeAvailability.ONLINE and current.last_error is None:
                return
            next_snapshot = RemoteNodeMonitorSnapshot(
                node=current.node,
                availability=RemoteNodeAvailability.ONLINE,
                app_entries=current.app_entries,
                system_summary=current.system_summary,
                last_event=current.last_event,
            )
        self._replace_snapshot(next_snapshot)

    def _mark_offline(self, error: Exception) -> None:
        with self._lock:
            current = self._snapshot
            if current.availability is RemoteNodeAvailability.OFFLINE:
                return
            next_snapshot = RemoteNodeMonitorSnapshot(
                node=current.node,
                availability=RemoteNodeAvailability.OFFLINE,
                app_entries=current.app_entries,
                system_summary=current.system_summary,
                last_event=None,
                last_error=error,
            )
        self._replace_snapshot(next_snapshot)

    def _replace_snapshot(self, snapshot: RemoteNodeMonitorSnapshot) -> None:
        with self._lock:
            if snapshot == self._snapshot:
                return
            self._snapshot = snapshot
            callbacks = tuple(self._callbacks.values())
        for callback in callbacks:
            self._invoke_callback(callback=callback, snapshot=snapshot)

    @staticmethod
    def _invoke_callback(*, callback: RemoteNodeMonitorCallback, snapshot: RemoteNodeMonitorSnapshot) -> None:
        try:
            callback(snapshot)
        except Exception:
            log.exception("Remote node monitor subscriber failed: node=%s", snapshot.node.node_name)
