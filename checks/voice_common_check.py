from __future__ import annotations

import asyncio
import contextlib
import unittest
from collections.abc import Callable, Coroutine
from typing import Any, cast

from hikariwave.connection import ConnectionState, VoiceConnection

from voice_common import VoiceUdpDiscoveryTimeoutError, _await_voice_connect_ready


class _AsyncTaskFactory:
    def create(self, coroutine: Coroutine[Any, Any, None], *, name: str | None = None) -> asyncio.Task[None]:
        return asyncio.create_task(coroutine, name=name)


class _FakeClient:
    def __init__(self) -> None:
        self._tasks = _AsyncTaskFactory()


class _FakeServer:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class _FakeGateway:
    def __init__(self, listener_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._listener_factory = listener_factory
        self._task_listen: asyncio.Task[None] | None = None
        self.connected_urls: list[str] = []
        self.disconnect_calls = 0

    async def connect(self, url: str) -> None:
        self.connected_urls.append(url)
        self._task_listen = asyncio.create_task(self._listener_factory(), name="gateway-listener")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self._task_listen is None:
            return
        if not self._task_listen.done():
            self._task_listen.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task_listen


class _FakeVoiceConnection:
    def __init__(self, listener_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._lock = asyncio.Lock()
        self._state = ConnectionState.DISCONNECTED
        self._ready = asyncio.Event()
        self._client = _FakeClient()
        self._endpoint = "wss://voice.example.test"
        self._server = _FakeServer()
        self._gateway = _FakeGateway(listener_factory)
        self._report_task: asyncio.Task[None] | None = None
        self._VoiceConnection__loop_reports = self._loop_reports

    async def _loop_reports(self) -> None:
        await asyncio.Future()


class VoiceCommonPatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_await_voice_connect_ready_propagates_listener_failure(self) -> None:
        ready = asyncio.Event()

        async def failing_listener() -> None:
            await asyncio.sleep(0)
            raise VoiceUdpDiscoveryTimeoutError(ip="35.213.98.127", port=50006, attempts=3)

        listener_task = asyncio.create_task(failing_listener(), name="gateway-listener")

        with self.assertRaises(VoiceUdpDiscoveryTimeoutError):
            await _await_voice_connect_ready(ready, listener_task)

    async def test_patched_connect_raises_listener_error_and_cleans_up(self) -> None:
        async def failing_listener() -> None:
            await asyncio.sleep(0)
            raise VoiceUdpDiscoveryTimeoutError(ip="35.213.98.127", port=50006, attempts=3)

        connection = _FakeVoiceConnection(failing_listener)

        with self.assertRaises(VoiceUdpDiscoveryTimeoutError):
            await asyncio.wait_for(
                VoiceConnection._connect(cast(VoiceConnection, cast(object, connection))),
                timeout=1.0,
            )

        self.assertEqual(connection._state, ConnectionState.DISCONNECTED)
        self.assertEqual(connection._server.disconnect_calls, 1)
        self.assertEqual(connection._gateway.disconnect_calls, 1)
        self.assertIsNone(connection._report_task)
