from __future__ import annotations

from .runtime_imports import (
    App,
    Callable,
    ModWebUser,
    NodeApiScope,
    NodeAppStateStreamEvent,
    NodeStateStreamEvent,
    aiohttp,
    asyncio,
    cast,
    quote,
    urlsplit,
    urlunsplit,
)
from .constants import (
    _REMOTE_CHAT_STREAM_HEARTBEAT_SECONDS,
    _REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS,
    _REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
    log,
)
from .json_helpers import _json_object_from_text
from .types import ModWebNodeLink

from .service_base import ModWebServiceSupport

class ModWebStreamsMixin(ModWebServiceSupport):
    def _subscribe_local_app_state(
        self,
        *,
        app: App,
        on_update: Callable[[NodeAppStateStreamEvent], None],
    ) -> Callable[[], None]:
        unsubscribe_runtime = self._node_api.subscribe_local_app_runtime(
            app.name,
            lambda event: on_update(event) if not event.is_initial else None,
        )
        unsubscribe_node = self._node_api.subscribe_local_node_state(
            lambda event: (
                on_update(NodeAppStateStreamEvent.system(app_name=app.name, system_summary=event.system_summary))
                if (not event.is_initial and event.system_summary is not None)
                else None
            )
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
        stream_task = asyncio.create_task(
            self._remote_app_state_stream_listener(
                node=node,
                app_name=app_name,
                user=user,
                on_update=on_update,
            )
        )

        def _unsubscribe() -> None:
            stream_task.cancel()

        return _unsubscribe

    def _create_remote_node_state_subscription(
        self,
        *,
        node: ModWebNodeLink,
        user: ModWebUser,
        on_update: Callable[[NodeStateStreamEvent], None],
    ) -> Callable[[], None]:
        stream_task = asyncio.create_task(
            self._remote_node_state_stream_listener(
                node=node,
                user=user,
                on_update=on_update,
            )
        )

        def _unsubscribe() -> None:
            stream_task.cancel()

        return _unsubscribe

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
                timeout = aiohttp.ClientTimeout(
                    total=None,
                    connect=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
                    sock_connect=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
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
                log.warning(
                    "Remote app state stream failed: node=%s app=%s error=%s",
                    node.node_name,
                    app_name,
                    xcp,
                )
            await asyncio.sleep(_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS)

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
                timeout = aiohttp.ClientTimeout(
                    total=None,
                    connect=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
                    sock_connect=_REMOTE_NODE_REQUEST_TIMEOUT_SECONDS,
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
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
                log.warning(
                    "Remote node state stream failed: node=%s error=%s",
                    node.node_name,
                    xcp,
                )
            await asyncio.sleep(_REMOTE_CHAT_STREAM_RECONNECT_DELAY_SECONDS)

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
    def _remote_websocket_url(*, node: ModWebNodeLink, path: str) -> str:
        api_url = urlsplit(node.api_base_url.rstrip("/"))
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
