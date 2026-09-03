from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import cast

from fastapi import WebSocket, status

from apps._app import App, ChatRelaySupport
from node_api.app_state import (
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeStateStreamEvent,
    NodeStateTopic,
)
from node_api.console import NodeConsoleStdoutSnapshot
from node_api.realtime_service import NodeRealtimeService
from node_api.system import NodeSystemSummary


class _SubscriptionSource:
    def __init__(self) -> None:
        self.runtime_callbacks: list[Callable[[NodeAppStateStreamEvent], None]] = []
        self.node_callbacks: list[Callable[[NodeStateStreamEvent], None]] = []
        self.runtime_unsubscribe_count = 0
        self.node_unsubscribe_count = 0

    def subscribe_app_runtime(
        self,
        app_name: str,
        callback: Callable[[NodeAppStateStreamEvent], None],
        *,
        include_update_state: bool,
    ) -> Callable[[], None]:
        del app_name, include_update_state
        self.runtime_callbacks.append(callback)

        def _unsubscribe() -> None:
            self.runtime_unsubscribe_count += 1

        return _unsubscribe

    def subscribe_node_state(
        self,
        callback: Callable[[NodeStateStreamEvent], None],
        *,
        topics: frozenset[NodeStateTopic],
    ) -> Callable[[], None]:
        del topics
        self.node_callbacks.append(callback)

        def _unsubscribe() -> None:
            self.node_unsubscribe_count += 1

        return _unsubscribe


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.accepted = asyncio.Event()
        self.initial_sent = asyncio.Event()
        self.disconnect_requested = asyncio.Event()
        self.append_sent = asyncio.Event()
        self.receive_started = asyncio.Event()
        self.sent_payloads: list[object] = []
        self.close_calls: list[tuple[int | None, str | None]] = []
        self.receive_cancelled = False

    async def accept(self) -> None:
        self.accepted.set()

    async def receive(self) -> dict[str, str]:
        self.receive_started.set()
        try:
            await self.disconnect_requested.wait()
        except asyncio.CancelledError:
            self.receive_cancelled = True
            raise
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload: object) -> None:
        self.sent_payloads.append(payload)
        payload_mapping = cast(dict[str, object], payload) if isinstance(payload, dict) else None
        if payload_mapping is not None and payload_mapping.get("kind") == "append":
            self.append_sent.set()
        else:
            self.initial_sent.set()

    async def close(
        self,
        code: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.close_calls.append((code, reason))


class _MessageWebSocket:
    def __init__(self, messages: Sequence[dict[str, str]]) -> None:
        self._messages = iter(messages)
        self.accepted = False
        self.sent_payloads: list[object] = []
        self.close_calls: list[tuple[int | None, str | None]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, str]:
        return next(self._messages)

    async def send_json(self, payload: object) -> None:
        self.sent_payloads.append(payload)

    async def close(
        self,
        code: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.close_calls.append((code, reason))


def _as_websocket(websocket: _BlockingWebSocket | _MessageWebSocket) -> WebSocket:
    return cast(WebSocket, cast(object, websocket))


def _system_summary() -> NodeSystemSummary:
    return NodeSystemSummary(
        cpu_percent=10,
        ram_percent=20,
        ram_used_bytes=2,
        ram_total_bytes=10,
        storage_percent=30,
        storage_free_bytes=20,
        storage_total_bytes=30,
    )


def _runtime_summary() -> NodeAppRuntimeSummary:
    return NodeAppRuntimeSummary(
        running=True,
        enabled=True,
        version="1.21.1",
        player_count=1,
        player_capacity=8,
        relay_support=ChatRelaySupport.NONE,
        storage_percent=None,
        storage_free_bytes=None,
        storage_total_bytes=None,
    )


def _stdout_snapshot(lines: tuple[str, ...]) -> NodeConsoleStdoutSnapshot:
    return NodeConsoleStdoutSnapshot(
        app_name="minecraft_alpha",
        app_friendly="Minecraft Alpha",
        node="erin",
        lines=lines,
        truncated=False,
        running=True,
    )


def _realtime_service(
    subscriptions: _SubscriptionSource,
    *,
    build_console_stdout_snapshot: Callable[[App, int], NodeConsoleStdoutSnapshot] | None = None,
    presence_connection_limit: int = 64,
    presence_message_limit_per_minute: int = 24,
    console_stdout_stream_interval_seconds: float = 0.5,
) -> NodeRealtimeService:
    async def _list_apps() -> tuple[NodeAppEntry, ...]:
        return ()

    async def _build_runtime_summary(_: App) -> NodeAppRuntimeSummary:
        return _runtime_summary()

    return NodeRealtimeService(
        node_name=lambda: "erin",
        discord_service_state=lambda: None,
        discord_heartbeat_latency_ms=lambda: None,
        subscriptions=subscriptions,
        list_apps=_list_apps,
        build_live_app_runtime_summary=_build_runtime_summary,
        build_system_summary=_system_summary,
        discord_health=lambda: None,
        build_console_stdout_snapshot=build_console_stdout_snapshot
        or (lambda _app, _max_lines: _stdout_snapshot(())),
        presence_connection_limit=presence_connection_limit,
        presence_message_limit_per_minute=presence_message_limit_per_minute,
        console_stdout_stream_interval_seconds=console_stdout_stream_interval_seconds,
    )


class NodeRealtimeServiceTests(unittest.TestCase):
    def test_presence_stream_releases_capacity_after_disconnect(self) -> None:
        async def exercise() -> None:
            realtime = _realtime_service(
                _SubscriptionSource(),
                presence_connection_limit=1,
            )
            first = _BlockingWebSocket()
            first_task = asyncio.create_task(
                realtime.serve_presence_stream(_as_websocket(first))
            )
            await asyncio.wait_for(first.accepted.wait(), timeout=0.2)

            rejected = _MessageWebSocket(())
            await realtime.serve_presence_stream(_as_websocket(rejected))

            self.assertEqual(
                rejected.close_calls,
                [(status.WS_1013_TRY_AGAIN_LATER, "Presence stream capacity reached.")],
            )

            first.disconnect_requested.set()
            await first_task

            accepted = _MessageWebSocket(({"type": "websocket.disconnect"},))
            await realtime.serve_presence_stream(_as_websocket(accepted))

            self.assertTrue(accepted.accepted)
            self.assertEqual(first.close_calls, [(None, None)])

        asyncio.run(exercise())

    def test_presence_stream_rate_limit_preserves_policy_close(self) -> None:
        websocket = _MessageWebSocket(
            (
                {"type": "websocket.receive", "text": json.dumps({"sample_id": "one"})},
                {"type": "websocket.receive", "text": json.dumps({"sample_id": "two"})},
            )
        )
        realtime = _realtime_service(
            _SubscriptionSource(),
            presence_message_limit_per_minute=1,
        )

        asyncio.run(realtime.serve_presence_stream(_as_websocket(websocket)))

        self.assertEqual(
            websocket.sent_payloads,
            [{"type": "pong", "node": "erin", "sample_id": "one"}],
        )
        self.assertEqual(
            websocket.close_calls[0],
            (status.WS_1008_POLICY_VIOLATION, "Presence stream message rate exceeded."),
        )

    def test_node_state_stream_disconnect_unsubscribes_and_closes(self) -> None:
        async def exercise() -> None:
            subscriptions = _SubscriptionSource()
            realtime = _realtime_service(subscriptions)
            websocket = _BlockingWebSocket()
            task = asyncio.create_task(
                realtime.serve_node_state_stream(_as_websocket(websocket))
            )

            await asyncio.wait_for(websocket.initial_sent.wait(), timeout=0.2)
            self.assertEqual(len(subscriptions.node_callbacks), 1)
            websocket.disconnect_requested.set()
            await task

            self.assertEqual(subscriptions.node_unsubscribe_count, 1)
            self.assertEqual(websocket.close_calls, [(None, None)])

        asyncio.run(exercise())

    def test_node_state_stream_cancellation_cleans_up_child_waiters(self) -> None:
        async def exercise() -> None:
            subscriptions = _SubscriptionSource()
            realtime = _realtime_service(subscriptions)
            websocket = _BlockingWebSocket()
            task = asyncio.create_task(
                realtime.serve_node_state_stream(_as_websocket(websocket))
            )

            await asyncio.wait_for(websocket.initial_sent.wait(), timeout=0.2)
            await asyncio.wait_for(websocket.receive_started.wait(), timeout=0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertTrue(websocket.receive_cancelled)
            self.assertEqual(subscriptions.node_unsubscribe_count, 1)
            self.assertEqual(websocket.close_calls, [(None, None)])

        asyncio.run(exercise())

    def test_app_state_stream_disconnect_unsubscribes_all_sources(self) -> None:
        async def exercise() -> None:
            subscriptions = _SubscriptionSource()
            realtime = _realtime_service(subscriptions)
            websocket = _BlockingWebSocket()
            app = cast(
                App,
                cast(
                    object,
                    SimpleNamespace(
                        name="minecraft_alpha",
                        update_info=None,
                        update_status=None,
                    ),
                ),
            )
            task = asyncio.create_task(
                realtime.serve_app_state_stream(_as_websocket(websocket), app)
            )

            await asyncio.wait_for(websocket.initial_sent.wait(), timeout=0.2)
            self.assertEqual(len(subscriptions.runtime_callbacks), 1)
            self.assertEqual(len(subscriptions.node_callbacks), 1)
            websocket.disconnect_requested.set()
            await task

            self.assertEqual(subscriptions.runtime_unsubscribe_count, 1)
            self.assertEqual(subscriptions.node_unsubscribe_count, 1)
            self.assertEqual(websocket.close_calls, [(None, None)])

        asyncio.run(exercise())

    def test_console_stdout_stream_sends_deltas_and_closes_on_disconnect(self) -> None:
        async def exercise() -> None:
            snapshots = (_stdout_snapshot(("one",)), _stdout_snapshot(("one", "two")))
            snapshot_index = 0

            def build_snapshot(_: App, _max_lines: int) -> NodeConsoleStdoutSnapshot:
                nonlocal snapshot_index
                snapshot = snapshots[min(snapshot_index, len(snapshots) - 1)]
                snapshot_index += 1
                return snapshot

            realtime = _realtime_service(
                _SubscriptionSource(),
                build_console_stdout_snapshot=build_snapshot,
                console_stdout_stream_interval_seconds=0.001,
            )
            websocket = _BlockingWebSocket()
            app = cast(
                App,
                cast(object, SimpleNamespace(name="minecraft_alpha")),
            )
            task = asyncio.create_task(
                realtime.serve_console_stdout_stream(
                    websocket=_as_websocket(websocket),
                    app=app,
                    max_lines=200,
                )
            )

            await asyncio.wait_for(websocket.append_sent.wait(), timeout=0.2)
            websocket.disconnect_requested.set()
            await task

            self.assertEqual(
                websocket.sent_payloads,
                [
                    {
                        "kind": "initial",
                        "app_name": "minecraft_alpha",
                        "snapshot": _stdout_snapshot(("one",)).to_mapping(),
                        "appended_lines": [],
                        "truncated": False,
                        "running": True,
                    },
                    {
                        "kind": "append",
                        "app_name": "minecraft_alpha",
                        "snapshot": None,
                        "appended_lines": ["two"],
                        "truncated": False,
                        "running": True,
                    },
                ],
            )
            self.assertEqual(websocket.close_calls, [(None, None)])

        asyncio.run(exercise())

    def test_stream_event_mergers_keep_newest_event_values(self) -> None:
        node_event = NodeRealtimeService.merge_node_state_stream_events(
            NodeStateStreamEvent.apps(node_name="erin", app_entries=()),
            NodeStateStreamEvent.health(node_name="ERIN", discord_health=None),
        )
        app_event = NodeRealtimeService.merge_app_state_stream_events(
            NodeAppStateStreamEvent.runtime(
                app_name="minecraft_alpha",
                app_stats=_runtime_summary(),
            ),
            NodeAppStateStreamEvent.update(
                app_name="MINECRAFT_ALPHA",
                update_info=None,
                update_status=None,
            ),
        )

        self.assertTrue(node_event.apps_changed)
        self.assertTrue(node_event.health_changed)
        self.assertEqual(node_event.app_entries, ())
        self.assertTrue(app_event.runtime_changed)
        self.assertTrue(app_event.update_changed)
        self.assertEqual(app_event.app_stats, _runtime_summary())
