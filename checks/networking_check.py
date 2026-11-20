from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Barrier
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
import requests
from aiohttp import web

import config
from _authority import AuthorityClient
from apps._app import App
from http_sessions import ThreadLocalHttpSessions
from mod_web_auth import ModWebUser
from node_api.map_service import NodeMapService
from node_api.route_contracts import PORTAL_NODE_LATENCY_REFRESH_INTERVAL_SECONDS
from node_api.service import NodeApiService, PortalNodeLatencyProbe
from node_auth import NodeApiScope
from web_dash.service import ModWebService
from web_dash.types import ModWebNodeLink


@asynccontextmanager
async def _serve(application: web.Application) -> AsyncGenerator[str, None]:
    runner = web.AppRunner(application, shutdown_timeout=1.0)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = cast(tuple[str, int], runner.addresses[0])[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def test_http_requests_keep_capacity_when_websocket_pool_is_full() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        async for _message in websocket:
            pass
        return websocket

    async def ping(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def exercise() -> None:
        application = web.Application()
        application.router.add_get("/ws", websocket_handler)
        application.router.add_get("/ping", ping)
        service = ModWebService()
        sockets: list[aiohttp.ClientWebSocketResponse] = []
        async with _serve(application) as base_url:
            try:
                streams = await service._remote_websocket_client()
                http = await service._remote_http_client()
                assert streams is await service._remote_websocket_client()
                assert http is await service._remote_http_client()
                assert streams.connector is not http.connector
                assert streams.connector is not None
                for _ in range(streams.connector.limit_per_host):
                    sockets.append(await streams.ws_connect(f"{base_url}/ws"))
                async with http.get(f"{base_url}/ping", timeout=aiohttp.ClientTimeout(total=1.0)) as response:
                    assert await response.json() == {"ok": True}
            finally:
                await asyncio.gather(*(socket.close() for socket in sockets))
                await service._close_remote_http_client()
            assert http.closed and streams.closed

    asyncio.run(exercise())


def test_async_clients_are_replaced_together_after_an_event_loop_change() -> None:
    service = ModWebService()

    async def open_clients() -> tuple[aiohttp.ClientSession, aiohttp.ClientSession]:
        return await service._remote_http_client(), await service._remote_websocket_client()

    old_http, old_streams = asyncio.run(open_clients())

    async def replace_clients() -> None:
        try:
            http, streams = await open_clients()
            assert old_http.closed and old_streams.closed
            assert http is not old_http and streams is not old_streams
            assert not http.closed and not streams.closed
        finally:
            await service._close_remote_http_client()

    asyncio.run(replace_clients())


def test_foreign_running_loop_cannot_close_the_active_http_clients() -> None:
    async def exercise() -> None:
        service = ModWebService()
        http = await service._remote_http_client()

        async def foreign_loop() -> None:
            client = await service._remote_websocket_client()
            await client.close()

        try:
            with pytest.raises(RuntimeError, match="another running event loop"):
                await asyncio.to_thread(asyncio.run, foreign_loop())
            assert not http.closed
            assert await service._remote_http_client() is http
        finally:
            await service._close_remote_http_client()

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan"), (1.0, 0.0), (0.0, 1.0)])
@pytest.mark.parametrize("streaming", [False, True])
def test_invalid_timeouts_cannot_disable_request_limits(
    timeout: float | tuple[float, float], streaming: bool,
) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        ModWebService._aiohttp_client_timeout(timeout, streaming=streaming)


def test_json_deadline_expires_while_response_keeps_sending_data() -> None:
    async def trickle(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        try:
            await response.write(b'{"value":"')
            for _ in range(20):
                await asyncio.sleep(0.02)
                await response.write(b"x")
            await response.write(b'"}')
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    async def exercise() -> None:
        application = web.Application()
        application.router.add_post("/trickle", trickle)
        service = ModWebService()
        async with _serve(application) as base_url:
            node = ModWebNodeLink(
                node_name="test", label="Test", url="/test", api_base_url=base_url,
                api_url="/test", is_current=False,
            )
            user = ModWebUser(discord_id=42, username="test", global_name=None, avatar_hash=None)
            try:
                with patch.object(service, "_remote_token", return_value="test"):
                    with pytest.raises(RuntimeError, match="Remote node request failed"):
                        await service._remote_json_async(
                            node=node, app_name=None, path="/trickle", scopes=(NodeApiScope.APPS_READ,),
                            user=user, method="POST", timeout=0.15,
                        )
                session = await service._remote_http_client()
                async with session.post(
                    f"{base_url}/trickle", timeout=service._aiohttp_client_timeout(0.15, streaming=True),
                ) as response:
                    assert await response.json() == {"value": "x" * 20}
            finally:
                await service._close_remote_http_client()

    asyncio.run(exercise())


def test_sync_sessions_are_thread_local_and_invalidated_on_close() -> None:
    sessions = ThreadLocalHttpSessions()
    barrier = Barrier(2)

    def session_in_worker() -> requests.Session:
        session = sessions.session()
        barrier.wait(timeout=2.0)
        assert sessions.session() is session
        return session

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(session_in_worker)
            second = workers.submit(session_in_worker)
            assert first.result(timeout=3.0) is not second.result(timeout=3.0)
        with ThreadPoolExecutor(max_workers=1) as worker:
            original = worker.submit(sessions.session).result(timeout=2.0)
            with patch.object(original, "close", wraps=original.close) as close:
                sessions.close()
                sessions.close()
                close.assert_called_once_with()
                assert worker.submit(sessions.session).result(timeout=2.0) is not original
    finally:
        sessions.close()


def test_authority_and_map_calls_reuse_connections_and_preserve_request_data() -> None:
    peers: dict[str, set[tuple[str, int]]] = {"authority": set(), "map": set()}

    async def handler(request: web.Request) -> web.Response:
        assert request.transport is not None
        peer = cast(tuple[str, int], request.transport.get_extra_info("peername"))
        kind = "map" if request.path.endswith(".png") else "authority"
        peers[kind].add(peer)
        if kind == "map":
            return web.Response(body=b"tile", content_type="image/png")
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = await request.json() if request.method == "POST" else {"ok": True}
        return web.json_response({"data": payload})

    async def exercise() -> None:
        application = web.Application()
        application.router.add_route("*", "/{path:.*}", handler)
        sessions = ThreadLocalHttpSessions()
        maps = NodeMapService(
            node_name=lambda: "test", http_exception=lambda _status, detail: RuntimeError(detail),
            logger=logging.getLogger(__name__),
        )
        async with _serve(application) as base_url:
            def requests_in_worker() -> None:
                for _ in range(5):
                    client = AuthorityClient(base_url=base_url, token="test-token", timeout=1.0)
                    assert client.get_json("/get") == {"data": {"ok": True}}
                    assert client.post_json("/post", {"value": 42}) == {"data": {"value": 42}}
                app = cast(App, Mock(
                    spec=App, public_map_url=base_url, map_proxy_url=base_url, map_proxy_root_path=None,
                ))
                for _ in range(10):
                    response = maps.proxy_response(app=app, relative_path="tiles/world/0/tile.png")
                    assert response.content == b"tile"
                    assert response.media_type == "image/png"

            try:
                with patch("_authority._http_sessions", sessions):
                    await asyncio.to_thread(requests_in_worker)
            finally:
                sessions.close()
                maps.close()
        assert {kind: len(connections) for kind, connections in peers.items()} == {"authority": 1, "map": 1}

    asyncio.run(exercise())


@pytest.mark.parametrize(("status", "body", "error"), [
    (500, b'{"detail":"unavailable"}', requests.HTTPError),
    (200, b"invalid JSON", requests.exceptions.JSONDecodeError),
    (200, b"[]", TypeError),
])
def test_authority_closes_responses_when_http_or_json_validation_fails(
    status: int, body: bytes, error: type[Exception],
) -> None:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response._content_consumed = True
    session = Mock(spec=requests.Session)
    session.get.return_value = response
    client = AuthorityClient(base_url="https://authority.example", token="test-token", timeout=1.0)
    with (
        patch("_authority._http_sessions.session", return_value=session),
        patch.object(response, "close", wraps=response.close) as close,
    ):
        with pytest.raises(error):
            client.get_json("/test")
        close.assert_called_once_with()


@pytest.fixture
def portal_service(monkeypatch: pytest.MonkeyPatch) -> NodeApiService:
    monkeypatch.setattr(config, "ACTIVE_BOT_PROFILE", config.BOT_PROFILES[config.BotProfileName.PORTAL])
    service = NodeApiService()
    monkeypatch.setattr(service, "_portal_node_latency_targets", lambda: (("yuki", "https://node.example/ping"),))
    return service


def test_probe_callers_share_inflight_work_despite_one_cancellation(portal_service: NodeApiService) -> None:
    async def exercise() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        expected = {"yuki": PortalNodeLatencyProbe(12, 34, None)}

        async def measure(_targets: tuple[tuple[str, str], ...]) -> dict[str, PortalNodeLatencyProbe]:
            entered.set()
            await release.wait()
            return expected

        with patch.object(portal_service, "_measure_portal_node_latencies", side_effect=measure) as probe:
            first = asyncio.create_task(portal_service.portal_node_latency_probes_async())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            others = [asyncio.create_task(portal_service.portal_node_latency_probes_async()) for _ in range(8)]
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert await asyncio.gather(*others) == [expected] * len(others)
            probe.assert_awaited_once()
            result = await portal_service.portal_node_latency_probes_async()
            result.clear()
            assert await portal_service.portal_node_latency_probes_async() == expected

    asyncio.run(exercise())


def test_probe_cache_expires_and_invalidates_changed_targets(portal_service: NodeApiService) -> None:
    now = 100.0
    first = {"yuki": PortalNodeLatencyProbe(12, 34, None)}
    second = {"yuki": PortalNodeLatencyProbe(None, None, None)}
    changed = (("yuki", "https://replacement.example/ping"),)
    with (
        patch("node_api.service.time", SimpleNamespace(monotonic=lambda: now)),
        patch.object(portal_service, "_measure_portal_node_latencies", new=AsyncMock(side_effect=[first, second, first])) as probe,
    ):
        assert asyncio.run(portal_service.portal_node_latency_probes_async()) == first
        assert asyncio.run(portal_service.portal_node_latency_probes_async()) == first
        now += PORTAL_NODE_LATENCY_REFRESH_INTERVAL_SECONDS
        assert asyncio.run(portal_service.portal_node_latency_probes_async()) == second
        assert asyncio.run(portal_service.portal_node_latency_probes_async()) == second
        with patch.object(portal_service, "_portal_node_latency_targets", return_value=changed):
            assert asyncio.run(portal_service.portal_node_latency_probes_async()) == first
        assert probe.await_count == 3
        assert probe.await_args is not None and probe.await_args.args == (changed,)


def test_failed_probe_batches_can_be_retried(portal_service: NodeApiService) -> None:
    expected = {"yuki": PortalNodeLatencyProbe(12, 34, None)}
    with patch.object(
        portal_service, "_measure_portal_node_latencies", new=AsyncMock(side_effect=[RuntimeError("failed"), expected]),
    ) as probe:
        with pytest.raises(RuntimeError, match="failed"):
            asyncio.run(portal_service.portal_node_latency_probes_async())
        assert asyncio.run(portal_service.portal_node_latency_probes_async()) == expected
        assert probe.await_count == 2


def test_shutdown_cancels_shared_probes_and_closes_map_sessions(portal_service: NodeApiService) -> None:
    async def exercise() -> None:
        entered = asyncio.Event()

        async def measure(_targets: tuple[tuple[str, str], ...]) -> dict[str, PortalNodeLatencyProbe]:
            entered.set()
            await asyncio.Event().wait()
            return {}

        with (
            patch.object(portal_service, "_measure_portal_node_latencies", side_effect=measure),
            patch.object(portal_service.maps, "close") as close_maps,
        ):
            caller = asyncio.create_task(portal_service.portal_node_latency_probes_async())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            portal_service.begin_shutdown()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=1.0)
            assert await portal_service.portal_node_latency_probes_async() == {}
            close_maps.assert_called_once_with()

    asyncio.run(exercise())


def test_shutdown_cancels_probe_batches_superseded_by_changed_targets(portal_service: NodeApiService) -> None:
    async def exercise() -> None:
        entered: asyncio.Queue[None] = asyncio.Queue()
        release = asyncio.Event()

        async def measure(_targets: tuple[tuple[str, str], ...]) -> dict[str, PortalNodeLatencyProbe]:
            entered.put_nowait(None)
            await release.wait()
            return {}

        callers: list[asyncio.Task[dict[str, PortalNodeLatencyProbe]]] = []
        with patch.object(portal_service, "_measure_portal_node_latencies", side_effect=measure):
            try:
                callers.append(asyncio.create_task(portal_service.portal_node_latency_probes_async()))
                await asyncio.wait_for(entered.get(), timeout=1.0)
                with patch.object(
                    portal_service, "_portal_node_latency_targets",
                    return_value=(("yuki", "https://replacement.example/ping"),),
                ):
                    callers.append(asyncio.create_task(portal_service.portal_node_latency_probes_async()))
                    await asyncio.wait_for(entered.get(), timeout=1.0)
                portal_service.begin_shutdown()
                done, pending = await asyncio.wait(callers, timeout=0.2)
                assert not pending
                assert all(task.cancelled() for task in done)
            finally:
                release.set()
                await asyncio.gather(*callers, return_exceptions=True)

    asyncio.run(exercise())


def test_dashboard_server_shutdown_closes_its_backend_resources() -> None:
    async def exercise() -> None:
        service = ModWebService()
        with patch.object(service._node_api.maps, "close") as close_maps:
            await service._on_shutdown()
        assert service._shutting_down
        close_maps.assert_called_once_with()

    asyncio.run(exercise())


@pytest.mark.parametrize("error", [None, requests.HTTPError("unavailable")])
def test_latency_probes_skip_response_bodies_and_close_responses(error: requests.HTTPError | None) -> None:
    response = Mock(spec=requests.Response)
    response.headers = {"X-Yukibot-Discord-Latency-Ms": "42"}
    response.raise_for_status.side_effect = error
    url = "https://node.example/api/node/ping"
    with patch("node_api.service.requests.get", return_value=response) as get:
        probe = NodeApiService._measure_node_latency_probe(url)
    get.assert_called_once_with(url, timeout=4.0, stream=True)
    response.close.assert_called_once_with()
    assert probe.discord_latency_ms == (42 if error is None else None)
