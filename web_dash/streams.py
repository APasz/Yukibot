from __future__ import annotations

from .constants import (
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    _REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
    _REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS,
    log,
)
from .json_helpers import _json_object_from_text
from .runtime_imports import (
    App,
    AppUpdateInfo,
    AppUpdateStatus,
    Callable,
    ModWebUser,
    NodeApiScope,
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeConsoleStdoutSnapshot,
    NodeConsoleStdoutStreamEvent,
    NodeStateStreamEvent,
    NodeStateTopic,
    NodeSystemSummary,
    aiohttp,
    asyncio,
    cast,
    config,
    quote,
    urlsplit,
    urlunsplit,
)
from .service_base import ModWebServiceSupport
from .stream_broker import ConsoleStreamKey, RemoteAppStreamKey, RemoteNodeStreamKey
from .types import ModWebNodeLink

_LOCAL_CONSOLE_STDOUT_SUBSCRIPTION_INTERVAL_SECONDS = 0.5


class ModWebStreamsMixin(ModWebServiceSupport):
    @staticmethod
    def _remote_websocket_stream_is_unsupported(xcp: Exception) -> bool:
        return isinstance(xcp, aiohttp.WSServerHandshakeError) and xcp.status in {400, 404, 405, 426}

    def _subscribe_local_app_state(
        self,
        *,
        app: App,
        on_update: Callable[[NodeAppStateStreamEvent], None],
    ) -> Callable[[], None]:
        unsubscribe_runtime = self._node_api.subscribe_local_app_runtime(
            app.name,
            lambda event: on_update(event) if not event.is_initial else None,
            include_update_state=True,
        )
        unsubscribe_node = self._node_api.subscribe_local_node_state(
            lambda event: (
                on_update(NodeAppStateStreamEvent.system(app_name=app.name, system_summary=event.system_summary))
                if (not event.is_initial and event.system_summary is not None)
                else None
            ),
            topics=frozenset({NodeStateTopic.SYSTEM}),
        )

        def _unsubscribe() -> None:
            unsubscribe_runtime()
            unsubscribe_node()

        return _unsubscribe

    def _create_remote_app_state_subscription(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
        on_update: Callable[[NodeAppStateStreamEvent], None],
    ) -> Callable[[], None]:
        key = RemoteAppStreamKey(node=node, app_name=app_name.casefold())
        return self._remote_app_state_broker.subscribe(
            key=key,
            callback=on_update,
            listener_factory=lambda publish: self._remote_app_state_stream_listener(
                node=node,
                app_name=app_name,
                user=user,
                on_update=publish,
            ),
        )

    def _create_remote_node_state_subscription(
        self,
        *,
        node: ModWebNodeLink,
        user: ModWebUser,
        on_update: Callable[[NodeStateStreamEvent], None],
    ) -> Callable[[], None]:
        key = RemoteNodeStreamKey(node=node)
        return self._remote_node_state_broker.subscribe(
            key=key,
            callback=on_update,
            listener_factory=lambda publish: self._remote_node_state_stream_listener(
                node=node,
                user=user,
                on_update=publish,
            ),
        )

    def _subscribe_local_app_console_stdout(
        self,
        *,
        app: App,
        max_lines: int,
        on_update: Callable[[NodeConsoleStdoutSnapshot], None],
    ) -> Callable[[], None]:
        key = ConsoleStreamKey(node=None, app_name=app.name.casefold(), max_lines=max_lines)
        return self._console_stdout_broker.subscribe(
            key=key,
            callback=on_update,
            listener_factory=lambda publish: self._local_app_console_stdout_listener(
                app=app,
                max_lines=max_lines,
                on_update=publish,
            ),
        )

    def _create_remote_console_stdout_subscription(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        max_lines: int,
        user: ModWebUser,
        on_update: Callable[[NodeConsoleStdoutSnapshot], None],
    ) -> Callable[[], None]:
        key = ConsoleStreamKey(node=node, app_name=app_name.casefold(), max_lines=max_lines)
        return self._console_stdout_broker.subscribe(
            key=key,
            callback=on_update,
            listener_factory=lambda publish: self._remote_console_stdout_stream_listener(
                node=node,
                app_name=app_name,
                max_lines=max_lines,
                user=user,
                on_update=publish,
            ),
        )

    async def _remote_app_state_stream_listener(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
        on_update: Callable[[NodeAppStateStreamEvent], None],
    ) -> None:
        while True:
            try:
                token = self._remote_token(
                    node=node,
                    app_name=app_name,
                    scopes=(NodeApiScope.APPS_READ, NodeApiScope.MODS_READ),
                    user=user,
                )
                session = await self._remote_http_client()
                async with session.ws_connect(
                    self._remote_app_state_stream_url(node=node, app_name=app_name),
                    headers={"Authorization": f"Bearer {token}"},
                    heartbeat=_REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
                ) as websocket:
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload_text: object = cast(object, message.data)
                            payload = _json_object_from_text(
                                payload_text,
                                context="Remote app state stream message",
                            )
                            event = NodeAppStateStreamEvent.from_mapping(payload)
                            if event.app_name.casefold() != app_name.casefold():
                                raise RuntimeError(
                                    "Remote app state stream app mismatch: "
                                    f"expected={app_name!r} got={event.app_name!r}"
                                )
                            on_update(event)
                            continue
                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        }:
                            break
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"Remote app state stream websocket error: {websocket.exception()}")
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                if self._remote_websocket_stream_is_unsupported(xcp):
                    log.warning(
                        "Remote app state stream websocket unsupported: node=%s app=%s status=%s; falling back to polling",
                        node.node_name,
                        app_name,
                        getattr(xcp, "status", None),
                    )
                    return await self._remote_app_state_polling_listener(
                        node=node,
                        app_name=app_name,
                        user=user,
                        on_update=on_update,
                    )
                log.warning(
                    "Remote app state stream failed: node=%s app=%s error=%s",
                    node.node_name,
                    app_name,
                    xcp,
                )
            await asyncio.sleep(_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS)

    async def _local_app_console_stdout_listener(
        self,
        *,
        app: App,
        max_lines: int,
        on_update: Callable[[NodeConsoleStdoutSnapshot], None],
    ) -> None:
        previous_snapshot: NodeConsoleStdoutSnapshot | None = None
        while True:
            next_snapshot = self._node_api.build_console_stdout_snapshot(app=app, max_lines=max_lines)
            if next_snapshot != previous_snapshot:
                on_update(next_snapshot)
                previous_snapshot = next_snapshot
            await asyncio.sleep(_LOCAL_CONSOLE_STDOUT_SUBSCRIPTION_INTERVAL_SECONDS)

    async def _remote_console_stdout_stream_listener(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        max_lines: int,
        user: ModWebUser,
        on_update: Callable[[NodeConsoleStdoutSnapshot], None],
    ) -> None:
        try:
            on_update(
                await self._remote_console_stdout_async(
                    node,
                    app_name,
                    max_lines=max_lines,
                    user=user,
                )
            )
        except Exception as xcp:
            log.warning(
                "Remote console stdout initial snapshot failed: node=%s app=%s error=%s",
                node.node_name,
                app_name,
                xcp,
            )
        while True:
            try:
                token = self._remote_token(
                    node=node,
                    app_name=app_name,
                    scopes=(NodeApiScope.APP_CONTROL,),
                    user=user,
                )
                session = await self._remote_http_client()
                async with session.ws_connect(
                    self._remote_console_stdout_stream_url(node=node, app_name=app_name, max_lines=max_lines),
                    headers={"Authorization": f"Bearer {token}"},
                    heartbeat=_REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
                ) as websocket:
                    current_snapshot: NodeConsoleStdoutSnapshot | None = None
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload_text: object = cast(object, message.data)
                            payload = _json_object_from_text(
                                payload_text,
                                context="Remote console stdout stream message",
                            )
                            if "kind" in payload:
                                stream_event = NodeConsoleStdoutStreamEvent.from_mapping(payload)
                                snapshot = stream_event.apply(current_snapshot, max_lines=max_lines)
                            else:
                                snapshot = NodeConsoleStdoutSnapshot.from_mapping(payload)
                            if snapshot.app_name.casefold() != app_name.casefold():
                                raise RuntimeError(
                                    "Remote console stdout stream app mismatch: "
                                    f"expected={app_name!r} got={snapshot.app_name!r}"
                                )
                            current_snapshot = snapshot
                            on_update(snapshot)
                            continue
                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        }:
                            break
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"Remote console stdout websocket error: {websocket.exception()}")
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                if self._remote_websocket_stream_is_unsupported(xcp):
                    log.warning(
                        "Remote console stdout websocket unsupported: node=%s app=%s status=%s; falling back to polling",
                        node.node_name,
                        app_name,
                        getattr(xcp, "status", None),
                    )
                    return await self._remote_console_stdout_polling_listener(
                        node=node,
                        app_name=app_name,
                        max_lines=max_lines,
                        user=user,
                        on_update=on_update,
                    )
                log.warning(
                    "Remote console stdout stream failed: node=%s app=%s error=%s",
                    node.node_name,
                    app_name,
                    xcp,
                )
            await asyncio.sleep(_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS)

    async def _remote_console_stdout_polling_listener(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        max_lines: int,
        user: ModWebUser,
        on_update: Callable[[NodeConsoleStdoutSnapshot], None],
    ) -> None:
        previous_snapshot: NodeConsoleStdoutSnapshot | None = None
        while True:
            try:
                next_snapshot = await self._remote_console_stdout_async(
                    node,
                    app_name,
                    max_lines=max_lines,
                    user=user,
                )
                if next_snapshot != previous_snapshot:
                    on_update(next_snapshot)
                    previous_snapshot = next_snapshot
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                log.warning(
                    "Remote console stdout polling failed: node=%s app=%s error=%s",
                    node.node_name,
                    app_name,
                    xcp,
                )
            await asyncio.sleep(_APP_RUNTIME_REFRESH_INTERVAL_SECONDS)

    async def _remote_app_state_polling_listener(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        user: ModWebUser,
        on_update: Callable[[NodeAppStateStreamEvent], None],
    ) -> None:
        previous_app_stats: NodeAppRuntimeSummary | None = None
        previous_system_summary: NodeSystemSummary | None = None
        previous_update_info: AppUpdateInfo | None = None
        previous_update_status: AppUpdateStatus | None = None
        while True:
            try:
                app_entry, app_stats, system_summary = await asyncio.gather(
                    self._remote_app_entry_async(node, app_name, user),
                    self._remote_app_runtime_summary_async(node, app_name, user),
                    self._remote_node_system_summary_or_none_async(
                        node,
                        user,
                        error_context="Remote app state polling system summary failed",
                    ),
                )
                event = self._remote_polled_app_state_event(
                    app_name=app_name,
                    app_stats=app_stats,
                    system_summary=system_summary,
                    previous_app_stats=previous_app_stats,
                    previous_system_summary=previous_system_summary,
                    update_info=app_entry.update_info,
                    update_status=app_entry.update_status,
                    previous_update_info=previous_update_info,
                    previous_update_status=previous_update_status,
                )
                previous_app_stats = app_stats
                if system_summary is not None:
                    previous_system_summary = system_summary
                previous_update_info = app_entry.update_info
                previous_update_status = app_entry.update_status
                if event is not None:
                    on_update(event)
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                log.warning(
                    "Remote app state polling failed: node=%s app=%s error=%s",
                    node.node_name,
                    app_name,
                    xcp,
                )
            await asyncio.sleep(_APP_RUNTIME_REFRESH_INTERVAL_SECONDS)

    async def _remote_node_state_stream_listener(
        self,
        *,
        node: ModWebNodeLink,
        user: ModWebUser,
        on_update: Callable[[NodeStateStreamEvent], None],
    ) -> None:
        while True:
            try:
                token = self._remote_token(
                    node=node,
                    app_name=None,
                    scopes=(NodeApiScope.APPS_READ,),
                    user=user,
                )
                session = await self._remote_http_client()
                async with session.ws_connect(
                    self._remote_node_state_stream_url(node=node),
                    headers={"Authorization": f"Bearer {token}"},
                    heartbeat=_REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
                ) as websocket:
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload_text: object = cast(object, message.data)
                            payload = _json_object_from_text(
                                payload_text,
                                context="Remote node state stream message",
                            )
                            event = NodeStateStreamEvent.from_mapping(payload)
                            if event.node_name.casefold() != node.node_name.casefold():
                                raise RuntimeError(
                                    "Remote node state stream node mismatch: "
                                    f"expected={node.node_name!r} got={event.node_name!r}"
                                )
                            on_update(event)
                            continue
                        if message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        }:
                            break
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"Remote node state stream websocket error: {websocket.exception()}")
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                if self._remote_websocket_stream_is_unsupported(xcp):
                    log.warning(
                        "Remote node state stream websocket unsupported: node=%s status=%s; falling back to polling",
                        node.node_name,
                        getattr(xcp, "status", None),
                    )
                    return await self._remote_node_state_polling_listener(
                        node=node,
                        user=user,
                        on_update=on_update,
                    )
                log_method = log.info if self._remote_node_error_is_transient(xcp) else log.warning
                log_method(
                    "Remote node state stream disconnected; retrying: node=%s error=%s",
                    node.node_name,
                    xcp,
                )
            await asyncio.sleep(_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS)

    async def _remote_node_state_polling_listener(
        self,
        *,
        node: ModWebNodeLink,
        user: ModWebUser,
        on_update: Callable[[NodeStateStreamEvent], None],
    ) -> None:
        previous_app_entries: tuple[NodeAppEntry, ...] | None = None
        previous_system_summary: NodeSystemSummary | None = None
        while True:
            try:
                app_entries, system_summary = await asyncio.gather(
                    self._remote_apps_async(node, user),
                    self._remote_node_system_summary_or_none_async(
                        node,
                        user,
                        error_context="Remote node state polling system summary failed",
                    ),
                )
                event = self._remote_polled_node_state_event(
                    node_name=node.node_name,
                    app_entries=app_entries,
                    system_summary=system_summary,
                    previous_app_entries=previous_app_entries,
                    previous_system_summary=previous_system_summary,
                )
                previous_app_entries = app_entries
                if system_summary is not None:
                    previous_system_summary = system_summary
                if event is not None:
                    on_update(event)
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                log.warning(
                    "Remote node state polling failed: node=%s error=%s",
                    node.node_name,
                    xcp,
                )
            await asyncio.sleep(_APP_RUNTIME_REFRESH_INTERVAL_SECONDS)

    @staticmethod
    def _remote_polled_app_state_event(
        *,
        app_name: str,
        app_stats: NodeAppRuntimeSummary,
        system_summary: NodeSystemSummary | None,
        previous_app_stats: NodeAppRuntimeSummary | None,
        previous_system_summary: NodeSystemSummary | None,
        update_info: AppUpdateInfo | None,
        update_status: AppUpdateStatus | None,
        previous_update_info: AppUpdateInfo | None,
        previous_update_status: AppUpdateStatus | None,
    ) -> NodeAppStateStreamEvent | None:
        runtime_changed = previous_app_stats != app_stats
        system_changed = system_summary is not None and previous_system_summary != system_summary
        update_changed = previous_update_info != update_info or previous_update_status != update_status
        if not runtime_changed and not system_changed and not update_changed:
            return None
        return NodeAppStateStreamEvent(
            app_name=app_name,
            runtime_changed=runtime_changed,
            system_changed=system_changed,
            update_changed=update_changed,
            app_stats=app_stats if runtime_changed else None,
            system_summary=system_summary if system_changed else None,
            update_info=update_info if update_changed else None,
            update_status=update_status if update_changed else None,
        )

    @staticmethod
    def _remote_polled_node_state_event(
        *,
        node_name: str,
        app_entries: tuple[NodeAppEntry, ...],
        system_summary: NodeSystemSummary | None,
        previous_app_entries: tuple[NodeAppEntry, ...] | None,
        previous_system_summary: NodeSystemSummary | None,
    ) -> NodeStateStreamEvent | None:
        apps_changed = previous_app_entries != app_entries
        system_changed = system_summary is not None and previous_system_summary != system_summary
        if apps_changed and system_summary is not None and system_changed:
            return NodeStateStreamEvent.both(
                node_name=node_name,
                app_entries=app_entries,
                system_summary=system_summary,
            )
        if apps_changed:
            return NodeStateStreamEvent.apps(node_name=node_name, app_entries=app_entries)
        if system_summary is not None and system_changed:
            return NodeStateStreamEvent.system(node_name=node_name, system_summary=system_summary)
        return None

    @staticmethod
    def _remote_node_state_stream_url(*, node: ModWebNodeLink) -> str:
        return ModWebStreamsMixin._remote_websocket_url(node=node, path="/state/stream")

    @staticmethod
    def _remote_app_state_stream_url(*, node: ModWebNodeLink, app_name: str) -> str:
        return ModWebStreamsMixin._remote_websocket_url(
            node=node,
            path=f"/apps/{quote(app_name, safe='')}/state/stream",
        )

    @staticmethod
    def _remote_console_stdout_stream_url(*, node: ModWebNodeLink, app_name: str, max_lines: int) -> str:
        return ModWebStreamsMixin._remote_websocket_url(
            node=node,
            path=f"/apps/{quote(app_name, safe='')}/console/stdout/stream?max_lines={max_lines}",
        )

    @staticmethod
    def _remote_websocket_url(*, node: ModWebNodeLink, path: str) -> str:
        resolved_api_base_url = node.api_base_url.rstrip("/")
        api_url = urlsplit(resolved_api_base_url)
        if not api_url.scheme and resolved_api_base_url.startswith("/"):
            api_url = urlsplit(f"{config.MOD_WEB_SERVER.public_base_url.rstrip('/')}{resolved_api_base_url}")
        if api_url.scheme == "https":
            websocket_scheme = "wss"
        elif api_url.scheme == "http":
            websocket_scheme = "ws"
        else:
            raise RuntimeError(f"Unsupported remote node API scheme for websocket stream: {api_url.scheme!r}")
        if not api_url.netloc:
            raise RuntimeError("Remote node API base URL must be absolute for websocket streams.")
        stream_path = f"{api_url.path.rstrip('/')}{path}"
        return urlunsplit((websocket_scheme, api_url.netloc, stream_path, "", ""))
