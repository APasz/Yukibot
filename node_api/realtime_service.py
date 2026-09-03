"""WebSocket transport orchestration for node API realtime endpoints."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Protocol, TypeVar, cast

from fastapi import WebSocket, WebSocketDisconnect, status

from apps._app import App
from .app_state import (
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeStateStreamEvent,
    NodeStateTopic,
)
from .console import (
    NodeConsoleStdoutSnapshot,
    NodeConsoleStdoutStreamEvent,
    NodeConsoleStdoutStreamEventKind,
)
from .route_contracts import DiscordHealthSnapshot, DiscordServiceState
from .system import NodeSystemSummary


_MAX_PRESENCE_STREAM_CONNECTIONS = 64
_MAX_PRESENCE_STREAM_MESSAGES_PER_MINUTE = 24
_CONSOLE_STDOUT_STREAM_INTERVAL_SECONDS = 0.5
_TaskResult = TypeVar("_TaskResult")


class _JsonStreamEvent(Protocol):
    """A stream event that can be sent as a websocket JSON payload."""

    def to_mapping(self) -> dict[str, object]: ...


_StreamEvent = TypeVar("_StreamEvent", bound=_JsonStreamEvent)


class NodeStateSubscriptionSource(Protocol):
    """The narrow state-subscription surface required by realtime transport."""

    def subscribe_app_runtime(
        self,
        app_name: str,
        callback: Callable[[NodeAppStateStreamEvent], None],
        *,
        include_update_state: bool,
    ) -> Callable[[], None]: ...

    def subscribe_node_state(
        self,
        callback: Callable[[NodeStateStreamEvent], None],
        *,
        topics: frozenset[NodeStateTopic],
    ) -> Callable[[], None]: ...


class NodeRealtimeService:
    """Coordinates websocket transport around existing node state providers."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        discord_service_state: Callable[[], DiscordServiceState | None],
        discord_heartbeat_latency_ms: Callable[[], int | None],
        subscriptions: NodeStateSubscriptionSource,
        list_apps: Callable[[], Awaitable[tuple[NodeAppEntry, ...]]],
        build_live_app_runtime_summary: Callable[[App], Awaitable[NodeAppRuntimeSummary]],
        build_system_summary: Callable[[], NodeSystemSummary],
        discord_health: Callable[[], DiscordHealthSnapshot | None],
        build_console_stdout_snapshot: Callable[[App, int], NodeConsoleStdoutSnapshot],
        presence_connection_limit: int = _MAX_PRESENCE_STREAM_CONNECTIONS,
        presence_message_limit_per_minute: int = _MAX_PRESENCE_STREAM_MESSAGES_PER_MINUTE,
        console_stdout_stream_interval_seconds: float = _CONSOLE_STDOUT_STREAM_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if presence_connection_limit < 1:
            raise ValueError("Presence stream connection limit must be positive.")
        if presence_message_limit_per_minute < 1:
            raise ValueError("Presence stream message limit must be positive.")
        if console_stdout_stream_interval_seconds <= 0:
            raise ValueError("Console stdout stream interval must be positive.")
        self._node_name = node_name
        self._discord_service_state = discord_service_state
        self._discord_heartbeat_latency_ms = discord_heartbeat_latency_ms
        self._subscriptions = subscriptions
        self._list_apps = list_apps
        self._build_live_app_runtime_summary = build_live_app_runtime_summary
        self._build_system_summary = build_system_summary
        self._discord_health = discord_health
        self._build_console_stdout_snapshot = build_console_stdout_snapshot
        self._presence_connection_limit = presence_connection_limit
        self._presence_message_limit_per_minute = presence_message_limit_per_minute
        self._console_stdout_stream_interval_seconds = console_stdout_stream_interval_seconds
        self._monotonic = monotonic
        self._presence_stream_connection_count = 0
        self._presence_stream_connection_lock = threading.Lock()

    async def serve_presence_stream(self, websocket: WebSocket) -> None:
        """Respond to lightweight presence probes without exposing diagnostics."""

        if not self._try_reserve_presence_stream_connection():
            await websocket.close(
                code=status.WS_1013_TRY_AGAIN_LATER,
                reason="Presence stream capacity reached.",
            )
            return

        message_times: deque[float] = deque()
        try:
            await websocket.accept()
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                now = self._monotonic()
                while message_times and now - message_times[0] >= 60.0:
                    message_times.popleft()
                if len(message_times) >= self._presence_message_limit_per_minute:
                    await websocket.close(
                        code=status.WS_1008_POLICY_VIOLATION,
                        reason="Presence stream message rate exceeded.",
                    )
                    return
                message_times.append(now)
                sample_id: str | None = None
                payload_text = message.get("text")
                if isinstance(payload_text, str) and payload_text:
                    try:
                        raw_payload = cast(object, json.loads(payload_text))
                    except ValueError:
                        raw_payload = None
                    if isinstance(raw_payload, Mapping):
                        payload = cast(Mapping[str, object], raw_payload)
                        raw_sample_id = payload.get("sample_id")
                        if raw_sample_id is not None:
                            sample_id = str(raw_sample_id)
                response: dict[str, object] = {
                    "type": "pong",
                    "node": self._node_name(),
                    "sample_id": sample_id,
                }
                discord_service_state = self._discord_service_state()
                if discord_service_state is not None:
                    response["discord_service_state"] = discord_service_state.value
                discord_latency_ms = self._discord_heartbeat_latency_ms()
                if discord_latency_ms is not None:
                    response["discord_latency_ms"] = discord_latency_ms
                await websocket.send_json(response)
        except WebSocketDisconnect:
            return
        finally:
            await self._close_websocket_quietly(websocket)
            self._release_presence_stream_connection()

    async def serve_node_state_stream(self, websocket: WebSocket) -> None:
        """Send node state updates until the client disconnects."""

        await websocket.accept()
        update_queue: asyncio.Queue[NodeStateStreamEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        skip_initial = True

        def _enqueue_update(event: NodeStateStreamEvent) -> None:
            nonlocal skip_initial
            if skip_initial and event.is_initial:
                skip_initial = False
                return
            self._enqueue_stream_event(loop, update_queue, event)

        unsubscribe = self._subscriptions.subscribe_node_state(
            _enqueue_update,
            topics=frozenset(NodeStateTopic),
        )
        disconnect_task = asyncio.create_task(self._wait_for_disconnect(websocket))
        try:
            initial_event = NodeStateStreamEvent.initial(
                node_name=self._node_name(),
                app_entries=await self._list_apps(),
                system_summary=self.stream_system_summary(self._build_system_summary()),
                discord_health=self._discord_health(),
            )
            await self._serve_queued_stream(
                websocket=websocket,
                update_queue=update_queue,
                disconnect_task=disconnect_task,
                initial_event=initial_event,
                merge_events=self.merge_node_state_stream_events,
            )
        except WebSocketDisconnect:
            return
        finally:
            await self._cancel_task(disconnect_task)
            unsubscribe()
            await self._close_websocket_quietly(websocket)

    async def serve_app_state_stream(self, websocket: WebSocket, app: App) -> None:
        """Send selected-app runtime and host-system updates until disconnect."""

        await websocket.accept()
        update_queue: asyncio.Queue[NodeAppStateStreamEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        skip_runtime_initial = True
        skip_node_initial = True

        def _enqueue_runtime_update(event: NodeAppStateStreamEvent) -> None:
            nonlocal skip_runtime_initial
            if skip_runtime_initial and event.is_initial:
                skip_runtime_initial = False
                return
            self._enqueue_stream_event(loop, update_queue, event)

        def _enqueue_node_update(event: NodeStateStreamEvent) -> None:
            nonlocal skip_node_initial
            if skip_node_initial and event.is_initial:
                skip_node_initial = False
                return
            system_summary = event.system_summary
            if system_summary is None:
                return
            self._enqueue_stream_event(
                loop,
                update_queue,
                NodeAppStateStreamEvent.system(
                    app_name=app.name,
                    system_summary=system_summary,
                ),
            )

        unsubscribe_runtime = self._subscriptions.subscribe_app_runtime(
            app.name,
            _enqueue_runtime_update,
            include_update_state=True,
        )
        unsubscribe_node = self._subscriptions.subscribe_node_state(
            _enqueue_node_update,
            topics=frozenset({NodeStateTopic.SYSTEM}),
        )
        disconnect_task = asyncio.create_task(self._wait_for_disconnect(websocket))
        try:
            initial_event = NodeAppStateStreamEvent.initial(
                app_name=app.name,
                app_stats=await self._build_live_app_runtime_summary(app),
                system_summary=self.stream_system_summary(self._build_system_summary()),
                update_info=app.update_info,
                update_status=app.update_status,
            )
            await self._serve_queued_stream(
                websocket=websocket,
                update_queue=update_queue,
                disconnect_task=disconnect_task,
                initial_event=initial_event,
                merge_events=self.merge_app_state_stream_events,
            )
        except WebSocketDisconnect:
            return
        finally:
            await self._cancel_task(disconnect_task)
            unsubscribe_runtime()
            unsubscribe_node()
            await self._close_websocket_quietly(websocket)

    async def serve_console_stdout_stream(
        self,
        *,
        websocket: WebSocket,
        app: App,
        max_lines: int,
    ) -> None:
        """Stream a console tail, sending deltas when the rolling tail changes."""

        await websocket.accept()
        disconnect_task = asyncio.create_task(self._wait_for_disconnect(websocket))
        previous_snapshot: NodeConsoleStdoutSnapshot | None = None
        try:
            initial_snapshot = self._build_console_stdout_snapshot(app, max_lines)
            await websocket.send_json(
                NodeConsoleStdoutStreamEvent(
                    kind=NodeConsoleStdoutStreamEventKind.INITIAL,
                    app_name=app.name,
                    snapshot=initial_snapshot,
                    truncated=initial_snapshot.truncated,
                    running=initial_snapshot.running,
                ).to_mapping()
            )
            previous_snapshot = initial_snapshot
            while True:
                if not await self._wait_for_interval_or_disconnect(disconnect_task):
                    return
                next_snapshot = self._build_console_stdout_snapshot(app, max_lines)
                if next_snapshot == previous_snapshot:
                    continue
                appended_lines = self.console_stdout_appended_lines(
                    previous_snapshot,
                    next_snapshot,
                )
                if appended_lines is None:
                    event = NodeConsoleStdoutStreamEvent(
                        kind=NodeConsoleStdoutStreamEventKind.RESET,
                        app_name=app.name,
                        snapshot=next_snapshot,
                        truncated=next_snapshot.truncated,
                        running=next_snapshot.running,
                    )
                else:
                    event = NodeConsoleStdoutStreamEvent(
                        kind=NodeConsoleStdoutStreamEventKind.APPEND,
                        app_name=app.name,
                        appended_lines=appended_lines,
                        truncated=next_snapshot.truncated,
                        running=next_snapshot.running,
                    )
                await websocket.send_json(event.to_mapping())
                previous_snapshot = next_snapshot
        except WebSocketDisconnect:
            return
        finally:
            await self._cancel_task(disconnect_task)
            await self._close_websocket_quietly(websocket)

    @staticmethod
    def stream_system_summary(summary: NodeSystemSummary) -> NodeSystemSummary:
        """Reduce uptime precision for a stable, low-churn stream payload."""

        def _minute_bucket(seconds: int | None) -> int | None:
            return None if seconds is None else (seconds // 60) * 60

        return replace(
            summary,
            bot_uptime_seconds=_minute_bucket(summary.bot_uptime_seconds),
            uptime_seconds=_minute_bucket(summary.uptime_seconds),
        )

    @staticmethod
    def merge_node_state_stream_events(
        first: NodeStateStreamEvent,
        second: NodeStateStreamEvent,
    ) -> NodeStateStreamEvent:
        """Coalesce queued node updates while retaining the newest field values."""

        if first.node_name.casefold() != second.node_name.casefold():
            raise ValueError("Cannot merge node state stream events for different nodes.")
        return NodeStateStreamEvent(
            node_name=first.node_name,
            is_initial=first.is_initial or second.is_initial,
            apps_changed=first.apps_changed or second.apps_changed,
            system_changed=first.system_changed or second.system_changed,
            health_changed=first.health_changed or second.health_changed,
            app_entries=second.app_entries
            if second.app_entries is not None
            else first.app_entries,
            system_summary=second.system_summary
            if second.system_summary is not None
            else first.system_summary,
            discord_health=second.discord_health
            if second.health_changed
            else first.discord_health,
        )

    @staticmethod
    def merge_app_state_stream_events(
        first: NodeAppStateStreamEvent,
        second: NodeAppStateStreamEvent,
    ) -> NodeAppStateStreamEvent:
        """Coalesce queued app updates while retaining the newest field values."""

        if first.app_name.casefold() != second.app_name.casefold():
            raise ValueError("Cannot merge app state stream events for different apps.")
        return NodeAppStateStreamEvent(
            app_name=first.app_name,
            is_initial=first.is_initial or second.is_initial,
            runtime_changed=first.runtime_changed or second.runtime_changed,
            system_changed=first.system_changed or second.system_changed,
            update_changed=first.update_changed or second.update_changed,
            app_stats=second.app_stats
            if second.app_stats is not None
            else first.app_stats,
            system_summary=second.system_summary
            if second.system_summary is not None
            else first.system_summary,
            update_info=second.update_info
            if second.update_info is not None or second.update_changed
            else first.update_info,
            update_status=(
                second.update_status
                if second.update_status is not None or second.update_changed
                else first.update_status
            ),
        )

    @staticmethod
    def console_stdout_appended_lines(
        previous: NodeConsoleStdoutSnapshot,
        updated: NodeConsoleStdoutSnapshot,
    ) -> tuple[str, ...] | None:
        """Return new tail lines, or ``None`` when a full snapshot is required."""

        if previous.app_name.casefold() != updated.app_name.casefold():
            raise ValueError("Cannot compare console stdout snapshots for different apps.")
        if not previous.lines:
            return updated.lines
        max_overlap = min(len(previous.lines), len(updated.lines))
        for overlap in range(max_overlap, 0, -1):
            if previous.lines[-overlap:] == updated.lines[:overlap]:
                return updated.lines[overlap:]
        return None

    def _try_reserve_presence_stream_connection(self) -> bool:
        with self._presence_stream_connection_lock:
            if self._presence_stream_connection_count >= self._presence_connection_limit:
                return False
            self._presence_stream_connection_count += 1
            return True

    def _release_presence_stream_connection(self) -> None:
        with self._presence_stream_connection_lock:
            if self._presence_stream_connection_count <= 0:
                raise RuntimeError("Presence stream connection count underflow.")
            self._presence_stream_connection_count -= 1

    async def _wait_for_interval_or_disconnect(
        self,
        disconnect_task: asyncio.Task[None],
    ) -> bool:
        interval_task = asyncio.create_task(
            asyncio.sleep(self._console_stdout_stream_interval_seconds)
        )
        try:
            done, _pending = await asyncio.wait(
                {interval_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            return disconnect_task not in done
        finally:
            await self._cancel_task(interval_task)

    async def _serve_queued_stream(
        self,
        *,
        websocket: WebSocket,
        update_queue: asyncio.Queue[_StreamEvent],
        disconnect_task: asyncio.Task[None],
        initial_event: _StreamEvent,
        merge_events: Callable[[_StreamEvent, _StreamEvent], _StreamEvent],
    ) -> None:
        await websocket.send_json(initial_event.to_mapping())
        while True:
            merged_event = await self._next_queued_event(
                update_queue,
                disconnect_task,
            )
            if merged_event is None:
                return
            while not update_queue.empty():
                merged_event = merge_events(merged_event, update_queue.get_nowait())
            await websocket.send_json(merged_event.to_mapping())

    @staticmethod
    def _enqueue_stream_event(
        loop: asyncio.AbstractEventLoop,
        update_queue: asyncio.Queue[_StreamEvent],
        event: _StreamEvent,
    ) -> None:
        try:
            loop.call_soon_threadsafe(update_queue.put_nowait, event)
        except RuntimeError:
            return

    @staticmethod
    async def _next_queued_event(
        update_queue: asyncio.Queue[_StreamEvent],
        disconnect_task: asyncio.Task[None],
    ) -> _StreamEvent | None:
        queue_task = asyncio.create_task(update_queue.get())
        try:
            done, _pending = await asyncio.wait(
                {queue_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                return None
            return queue_task.result()
        finally:
            await NodeRealtimeService._cancel_task(queue_task)

    @staticmethod
    async def _wait_for_disconnect(websocket: WebSocket) -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return

    @staticmethod
    async def _cancel_task(task: asyncio.Task[_TaskResult]) -> None:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    async def _close_websocket_quietly(websocket: WebSocket) -> None:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()
