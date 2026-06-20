from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

import config
from _manager import App_Manager
from _security import Access_Control
from node_api import NodeApiService, RelayTTSQueue
from web_dash.nicegui_protocols import WebChatRelayPublisher

log = logging.getLogger(__name__)
_NODE_API_STARTUP_TIMEOUT_SECONDS = 15.0


class NodeApiHttpService:
    def __init__(self) -> None:
        self._node_api = NodeApiService()
        self._startup_lock = asyncio.Lock()
        self._startup_signal = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._startup_error: Exception | None = None
        self._started = False

    @property
    def enabled(self) -> bool:
        return config.NODE_API_SERVER is not None

    def set_relay_tts_service(self, relay_tts_service: RelayTTSQueue | None) -> None:
        self._node_api.set_relay_tts_service(relay_tts_service)

    def set_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._node_api.set_chat_relay_service(chat_relay)

    async def start(self, manager: App_Manager, *, acl: Access_Control | None = None) -> None:
        if not self.enabled:
            return

        self._node_api.set_manager(manager)
        if acl is not None:
            self._node_api.set_acl(acl)
        await self._ensure_started()

    async def stop(self) -> None:
        self._node_api.begin_shutdown()
        server = self._server
        if server is not None:
            server.should_exit = True
            server.force_exit = True
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 5.0)
        self._server = None
        self._server_thread = None
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            server_config = config.NODE_API_SERVER
            if server_config is None:
                return

            @asynccontextmanager
            async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
                self._startup_signal.set()
                log.info("Node API startup event received")
                yield

            app = FastAPI(lifespan=_lifespan)
            self._node_api.register_routes(app)
            uvicorn_config = uvicorn.Config(
                app,
                host=server_config.host,
                port=server_config.port,
                log_config=None,
                access_log=False,
            )
            server = uvicorn.Server(uvicorn_config)
            self._server = server
            self._startup_signal.clear()
            self._startup_error = None
            self._server_thread = threading.Thread(
                target=self._run_server,
                args=(server,),
                name="node-api",
                daemon=True,
            )
            self._server_thread.start()

            started = await asyncio.to_thread(self._startup_signal.wait, _NODE_API_STARTUP_TIMEOUT_SECONDS)
            if not started:
                raise TimeoutError("Timed out while starting the node API server.")
            if self._startup_error is not None:
                raise RuntimeError(f"Node API server failed to start: {self._startup_error}") from self._startup_error
            self._started = True
            log.info(
                "Node API server started: bind=%s:%s public=%s",
                server_config.host,
                server_config.port,
                server_config.node_api_base_url,
            )

    def _run_server(self, server: uvicorn.Server) -> None:
        try:
            server.run()
        except Exception as xcp:
            self._startup_error = xcp
            log.exception("Node API server failed")
            self._startup_signal.set()
