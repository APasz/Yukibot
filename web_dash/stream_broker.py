from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar, cast

from .types import ModWebNodeLink


log = logging.getLogger(__name__)

StreamKey = TypeVar("StreamKey")
StreamEvent = TypeVar("StreamEvent")
StreamListenerFactory = Callable[[Callable[[StreamEvent], None]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RemoteNodeStreamKey:
    """A per-node server-side stream used by the system monitoring page."""

    node: ModWebNodeLink


@dataclass(frozen=True, slots=True)
class RemoteAppStreamKey:
    node: ModWebNodeLink
    app_name: str


@dataclass(frozen=True, slots=True)
class RemoteChatStreamKey:
    node: ModWebNodeLink
    app_name: str


@dataclass(frozen=True, slots=True)
class ConsoleStreamKey:
    node: ModWebNodeLink | None
    app_name: str
    max_lines: int


@dataclass(slots=True)
class _SharedStreamEntry(Generic[StreamEvent]):
    callbacks: dict[str, Callable[[StreamEvent], None]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    latest_event: StreamEvent | None = None
    has_latest_event: bool = False


class SharedAsyncStreamBroker(Generic[StreamKey, StreamEvent]):
    """Shares one long-running upstream listener between local subscribers."""

    def __init__(self, *, reconnect_delay_seconds: float = 1.0) -> None:
        if reconnect_delay_seconds < 0:
            raise ValueError("Stream broker reconnect delay must not be negative.")
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._entries: dict[StreamKey, _SharedStreamEntry[StreamEvent]] = {}
        self._lock = threading.RLock()
        self._closed = False

    def subscribe(
        self,
        *,
        key: StreamKey,
        callback: Callable[[StreamEvent], None],
        listener_factory: StreamListenerFactory[StreamEvent],
        replay_latest: bool = False,
    ) -> Callable[[], None]:
        subscription_id = uuid.uuid4().hex
        latest_event: StreamEvent | None = None
        has_latest_event = False
        with self._lock:
            if self._closed:
                raise RuntimeError("Stream broker is closed.")
            entry = self._entries.get(key)
            if entry is None:
                entry = _SharedStreamEntry[StreamEvent]()
                self._entries[key] = entry
            entry.callbacks[subscription_id] = callback
            if replay_latest and entry.has_latest_event:
                latest_event = entry.latest_event
                has_latest_event = True
            if entry.task is None or entry.task.done():
                entry.task = asyncio.create_task(
                    self._run_listener(key=key, listener_factory=listener_factory),
                    name=f"shared-stream:{key!r}",
                )

        if has_latest_event:
            self._invoke_callback(key=key, callback=callback, event=cast(StreamEvent, latest_event))

        def _unsubscribe() -> None:
            self._unsubscribe(key=key, subscription_id=subscription_id)

        return _unsubscribe

    def subscriber_count(self, key: StreamKey) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return 0 if entry is None else len(entry.callbacks)

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            tasks = tuple(entry.task for entry in self._entries.values() if entry.task is not None)
            self._entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _unsubscribe(self, *, key: StreamKey, subscription_id: str) -> None:
        task: asyncio.Task[None] | None = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.callbacks.pop(subscription_id, None)
            if entry.callbacks:
                return
            task = entry.task
            self._entries.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_listener(
        self,
        *,
        key: StreamKey,
        listener_factory: StreamListenerFactory[StreamEvent],
    ) -> None:
        current_task = asyncio.current_task()
        try:
            while self.subscriber_count(key) > 0:
                try:
                    await listener_factory(lambda event: self._publish(key=key, event=event))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Shared stream listener failed: key=%r", key)
                if self.subscriber_count(key) > 0:
                    await asyncio.sleep(self._reconnect_delay_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.task is current_task:
                    entry.task = None

    def _publish(self, *, key: StreamKey, event: StreamEvent) -> None:
        with self._lock:
            entry = self._entries.get(key)
            callbacks = () if entry is None else tuple(entry.callbacks.values())
            if entry is not None:
                entry.latest_event = event
                entry.has_latest_event = True
        for callback in callbacks:
            self._invoke_callback(key=key, callback=callback, event=event)

    @staticmethod
    def _invoke_callback(
        *,
        key: StreamKey,
        callback: Callable[[StreamEvent], None],
        event: StreamEvent,
    ) -> None:
        try:
            callback(event)
        except Exception:
            log.exception("Shared stream subscriber failed: key=%r", key)
