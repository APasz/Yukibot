"""Chat room snapshots, relay publishing, and websocket streaming for node APIs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from fastapi import WebSocket, WebSocketDisconnect

from _manager import App_Manager
from apps._app import App
from chat_hub import (
    ChatEndpoint,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    ChatHub,
    ChatRoomUpdate,
)
from .app_state import (
    NodeAppRuntimeSummary,
    NodeAppStateSubscriptionService,
)
from .chat import (
    NodeChatEndpointSummary,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    NodeWebChatRequest,
)


class WebChatRelayPublisher(Protocol):
    """Publishes dashboard-originated events to the chat relay."""

    async def publish_web_chat(
        self,
        *,
        room_id: str,
        session_id: str,
        author_display_name: str,
        author_id: str | None,
        discord_user_id: int | None,
        content: str,
        reply_to_event_id: str | None = None,
    ) -> ChatEvent: ...

    async def publish_chat_event(self, *, event: ChatEvent) -> ChatEvent: ...


class LiveAppRuntimeSummaryBuilder(Protocol):
    """Builds the lightweight runtime summary used by chat stream events."""

    def __call__(self, app: App) -> Awaitable[NodeAppRuntimeSummary]: ...


class NodeChatService:
    """Owns chat-room state and live chat subscriptions for a node."""

    def __init__(
        self,
        *,
        http_exception: Callable[[int, str], Exception],
        history_limit: int,
        build_live_runtime_summary: LiveAppRuntimeSummaryBuilder,
        app_runtime_subscriptions: NodeAppStateSubscriptionService,
    ) -> None:
        if history_limit < 1:
            raise ValueError("Chat history limit must be positive.")
        self._manager: App_Manager | None = None
        self._http_exception = http_exception
        self._history_limit = history_limit
        self._build_live_runtime_summary = build_live_runtime_summary
        self._app_runtime_subscriptions = app_runtime_subscriptions
        self._relay: WebChatRelayPublisher | None = None

    def set_manager(self, manager: App_Manager) -> None:
        self._manager = manager

    def set_relay(self, relay: WebChatRelayPublisher | None) -> None:
        self._relay = relay

    def require_relay_app(self, app: App) -> None:
        if not app.supports_chat_relay:
            raise self._http_exception(
                404,
                f"{app.friendly} does not expose a chat relay.",
            )

    def build_room_snapshot(
        self,
        app: App,
        *,
        limit: int,
    ) -> NodeChatRoomSnapshot:
        self.require_relay_app(app)
        bounded_limit = max(0, min(limit, self._history_limit))
        hub = ChatHub()
        endpoint_summaries = self._endpoint_summaries(
            app,
            endpoints=hub.endpoints_for_room(app.name),
        )
        return NodeChatRoomSnapshot(
            room_id=app.name,
            endpoint_count=len(endpoint_summaries),
            events=hub.history(app.name, limit=bounded_limit),
            endpoint_summaries=endpoint_summaries,
            revision=hub.room_revision(app.name),
        )

    async def publish_web_chat(
        self,
        *,
        app: App,
        actor_user_id: int,
        chat_request: NodeWebChatRequest,
    ) -> ChatEvent:
        self.require_relay_app(app)
        relay = self._relay
        if relay is None:
            raise self._http_exception(
                503, "Web chat relay is not available on this node."
            )
        return await relay.publish_web_chat(
            room_id=app.name,
            session_id=chat_request.session_id,
            author_display_name=chat_request.author_display_name,
            author_id=str(actor_user_id),
            discord_user_id=actor_user_id,
            content=chat_request.content,
            reply_to_event_id=chat_request.reply_to_event_id,
        )

    async def publish_fake_chat(self, *, app: App, event: ChatEvent) -> ChatEvent:
        self.require_relay_app(app)
        if event.room_id.casefold() != app.name.casefold():
            raise self._http_exception(
                400,
                "Synthetic chat event room does not match the selected app.",
            )
        relay = self._relay
        if relay is None:
            raise self._http_exception(503, "Chat relay is not available on this node.")
        return await relay.publish_chat_event(event=event)

    async def serve_stream(
        self,
        *,
        websocket: WebSocket,
        app: App,
        after_revision: int | None = None,
    ) -> None:
        await websocket.accept()
        update_queue: asyncio.Queue[NodeChatStreamEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _enqueue_update(event: NodeChatStreamEvent) -> None:
            def _queue_put() -> None:
                update_queue.put_nowait(event)

            try:
                loop.call_soon_threadsafe(_queue_put)
            except RuntimeError:
                return

        def _enqueue_chat_update(update: ChatRoomUpdate) -> None:
            _enqueue_update(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.CHAT_CHANGED,
                    room_id=app.name,
                    snapshot=(
                        self.build_room_snapshot(app, limit=self._history_limit)
                        if update.event is None
                        else None
                    ),
                    events=() if update.event is None else (update.event,),
                    revision=update.revision,
                )
            )

        room_subscription_id = ChatHub().subscribe(app.name, _enqueue_chat_update)
        unsubscribe_runtime = self._app_runtime_subscriptions.subscribe_app_runtime(
            app.name,
            lambda update: _enqueue_update(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.RUNTIME_CHANGED,
                    room_id=app.name,
                    app_stats=update.app_stats,
                )
            ),
        )

        async def _wait_for_disconnect() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return

        async def _send_stream_event(event: NodeChatStreamEvent) -> None:
            app_stats = event.app_stats
            if (
                event.kind
                in {
                    NodeChatStreamEventKind.INITIAL,
                    NodeChatStreamEventKind.RUNTIME_CHANGED,
                }
                and app_stats is None
            ):
                app_stats = await self._build_live_runtime_summary(app)
            await websocket.send_json(
                NodeChatStreamEvent(
                    kind=event.kind,
                    room_id=event.room_id,
                    snapshot=event.snapshot,
                    app_stats=app_stats,
                    events=event.events,
                    revision=event.revision,
                ).to_mapping()
            )

        def _merge_stream_events(
            first: NodeChatStreamEvent,
            second: NodeChatStreamEvent,
        ) -> NodeChatStreamEvent:
            merged_events = (
                second.events
                if second.snapshot is not None
                else first.events + second.events
            )
            return NodeChatStreamEvent(
                kind=(
                    NodeChatStreamEventKind.RUNTIME_CHANGED
                    if NodeChatStreamEventKind.RUNTIME_CHANGED
                    in {first.kind, second.kind}
                    else NodeChatStreamEventKind.CHAT_CHANGED
                ),
                room_id=app.name,
                snapshot=(
                    second.snapshot if second.snapshot is not None else first.snapshot
                ),
                app_stats=(
                    second.app_stats
                    if second.app_stats is not None
                    else first.app_stats
                ),
                events=merged_events,
                revision=max(first.revision, second.revision),
            )

        disconnect_task = asyncio.create_task(_wait_for_disconnect())
        try:
            initial_snapshot = self.build_room_snapshot(
                app,
                limit=self._history_limit,
            )
            await _send_stream_event(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.INITIAL,
                    room_id=app.name,
                    snapshot=(
                        initial_snapshot
                        if after_revision != initial_snapshot.revision
                        else None
                    ),
                    revision=initial_snapshot.revision,
                )
            )
            while True:
                queue_task = asyncio.create_task(update_queue.get())
                done, _pending = await asyncio.wait(
                    {queue_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
                    return
                merged_event = queue_task.result()
                while not update_queue.empty():
                    merged_event = _merge_stream_events(
                        merged_event,
                        update_queue.get_nowait(),
                    )
                await _send_stream_event(merged_event)
        except WebSocketDisconnect:
            return
        finally:
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
            ChatHub().unsubscribe(app.name, room_subscription_id)
            unsubscribe_runtime()
            await self._close_websocket_quietly(websocket)

    @staticmethod
    async def _close_websocket_quietly(websocket: WebSocket) -> None:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()

    def _endpoint_summaries(
        self,
        app: App,
        *,
        endpoints: tuple[ChatEndpoint, ...],
    ) -> tuple[NodeChatEndpointSummary, ...]:
        app_running = app.check_running()
        summaries: list[NodeChatEndpointSummary] = []
        seen_keys: set[str] = set()
        for endpoint in endpoints:
            summary = self._endpoint_summary(app, endpoint, app_running=app_running)
            if summary is None:
                continue
            summary_key, summary_label = summary
            if summary_key in seen_keys:
                continue
            seen_keys.add(summary_key)
            summaries.append(NodeChatEndpointSummary(label=summary_label))
        return tuple(summaries)

    def _endpoint_summary(
        self,
        app: App,
        endpoint: ChatEndpoint,
        *,
        app_running: bool,
    ) -> tuple[str, str] | None:
        endpoint_id = endpoint.id
        if endpoint_id.kind is ChatEndpointKind.APP:
            if not app_running:
                return None
            label = endpoint.label or app.friendly
            return endpoint_id.stable_key, f"Game: {label}"
        if endpoint_id.kind is ChatEndpointKind.DISCORD_CHANNEL:
            return self._discord_endpoint_summary(endpoint)
        if endpoint_id.kind is ChatEndpointKind.DISCORD_TTS:
            label = endpoint.label or endpoint_id.value
            return endpoint_id.stable_key, f"Discord TTS: {label}"
        if endpoint_id.kind is ChatEndpointKind.WEB_SESSION:
            label = endpoint.label or "Dashboard"
            return endpoint_id.stable_key, f"Web: {label}"
        if endpoint_id.kind is ChatEndpointKind.SYSTEM:
            label = endpoint.label or "System"
            return endpoint_id.stable_key, f"System: {label}"
        raise ValueError(f"Unsupported chat endpoint kind: {endpoint_id.kind}")

    def _discord_endpoint_summary(self, endpoint: ChatEndpoint) -> tuple[str, str]:
        endpoint_id = endpoint.id
        channel_id = self._discord_endpoint_channel_id(endpoint_id)
        channel = self._discord_channel_cache_entry(channel_id)
        guild_id = getattr(channel, "guild_id", None)
        if isinstance(guild_id, int | str):
            guild_id_int = int(guild_id)
            guild_name = self._discord_guild_name(guild_id_int)
            guild_label = guild_name or str(guild_id_int)
            return f"discord_guild:{guild_id_int}", f"Discord: {guild_label}"

        channel_name = getattr(channel, "name", None)
        if isinstance(channel_name, str) and channel_name.strip():
            return endpoint_id.stable_key, f"Discord: {channel_name}"
        if endpoint.label is not None and endpoint.label.strip():
            return endpoint_id.stable_key, f"Discord: {endpoint.label}"
        return endpoint_id.stable_key, f"Discord: {endpoint_id.value}"

    @staticmethod
    def _discord_endpoint_channel_id(endpoint_id: ChatEndpointId) -> int | None:
        try:
            return int(endpoint_id.value)
        except TypeError, ValueError:
            return None

    def _discord_channel_cache_entry(self, channel_id: int | None) -> object | None:
        if channel_id is None:
            return None
        manager = self._manager
        bot = getattr(manager, "bot", None) if manager is not None else None
        cache = getattr(bot, "cache", None) if bot is not None else None
        get_guild_channel = (
            getattr(cache, "get_guild_channel", None) if cache is not None else None
        )
        if callable(get_guild_channel):
            return get_guild_channel(channel_id)
        return None

    def _discord_guild_name(self, guild_id: int) -> str | None:
        manager = self._manager
        bot = getattr(manager, "bot", None) if manager is not None else None
        cache = getattr(bot, "cache", None) if bot is not None else None
        get_guild = getattr(cache, "get_guild", None) if cache is not None else None
        guild = get_guild(guild_id) if callable(get_guild) else None
        guild_name = getattr(guild, "name", None)
        if isinstance(guild_name, str) and guild_name.strip():
            return guild_name
        return None
