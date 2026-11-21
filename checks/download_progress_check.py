from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.types import Message, Scope, Send

from mod_web_auth import ModWebUser
from node_auth import NodeApiScope
from web_dash.backend import ModWebDashboardBackend
from web_dash.download_progress import DOWNLOAD_TRANSFER_QUERY_PARAMETER, DownloadStreamingResponse, download_transfer_url
from web_dash.nicegui_protocols import ModWebFastApiApp, ModWebRouteUi, ModWebUi
from web_dash.service import ModWebService
from web_dash.status_notifications import ModWebStatusNotificationsMixin
from web_dash.types import ModWebBasePageModel, ModWebNodeLink, ModWebNotificationTrayItemState


@pytest.fixture
def backend() -> Iterator[ModWebDashboardBackend]:
    value = ModWebDashboardBackend()
    yield value
    value.clear_user_transfers(user_id=42)
    value.clear_user_transfers(user_id=99)


def _start_download(backend: ModWebDashboardBackend) -> int:
    return backend.start_download_transfers(user_id=42, filenames=("mod.zip",), detail_text="Preparing archive…")[0]


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _receive() -> Message:
    await asyncio.Event().wait()
    raise AssertionError("Unexpected receive completion")


def _scope(spec_version: str = "2.4") -> Scope:
    return {"type": "http", "asgi": {"spec_version": spec_version}}


_USER = ModWebUser(discord_id=42, username="review", global_name=None, avatar_hash=None)
_NODE = ModWebNodeLink(
    node_name="review", label="Review", url="/mod-web/nodes/review", api_base_url="https://node.example/api/node",
    api_url="/api/node-proxy/review/apps", is_current=False,
)


@pytest.fixture
def service(backend: ModWebDashboardBackend) -> ModWebService:
    value = ModWebService()
    value._backend = backend
    return value


def _request(transfer_id: int) -> Request:
    return Request({"type": "http", "headers": [], "query_string": f"{DOWNLOAD_TRANSFER_QUERY_PARAMETER}={transfer_id}".encode()})


def test_download_url_keeps_repeated_queries_blanks_and_fragment() -> None:
    url = download_transfer_url("/download?mod_name=a%20b&mod_name=c&empty=&download_transfer_id=1#details", 42)
    parsed = urlsplit(url)
    assert parsed.path == "/download"
    assert parsed.fragment == "details"
    assert parse_qs(parsed.query, keep_blank_values=True) == {
        "mod_name": ["a b", "c"], "empty": [""], DOWNLOAD_TRANSFER_QUERY_PARAMETER: ["42"],
    }


def test_download_requires_its_owner_and_claims_only_once(backend: ModWebDashboardBackend) -> None:
    transfer_id = _start_download(backend)
    assert backend.user_transfer_items(user_id=42)[0].progress_percent is None
    assert not backend.claim_download_transfer(transfer_id=transfer_id, user_id=99)
    assert backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    assert not backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    backend._timeout_pending_download(transfer_id=transfer_id)
    assert backend.user_transfer_items(user_id=42)[0].state is ModWebNotificationTrayItemState.ACTIVE
    backend.complete_transfer(transfer_id=transfer_id, detail_text="Transfer sent.")
    assert not backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    upload_id = backend.start_upload_transfers(user_id=42, filenames=("other.zip",), detail_text="Uploading…")[0]
    assert not backend.claim_download_transfer(transfer_id=upload_id, user_id=42)
    assert not backend.claim_download_transfer(transfer_id=99999, user_id=42)


def test_download_that_never_starts_releases_its_slot(backend: ModWebDashboardBackend) -> None:
    transfer_id = _start_download(backend)
    backend._timeout_pending_download(transfer_id=transfer_id)
    item = backend.user_transfer_items(user_id=42)[0]
    assert item.state is ModWebNotificationTrayItemState.ERROR
    assert item.detail_text and "did not start" in item.detail_text
    assert backend.user_active_transfer_slots(user_id=42) == 0
    assert not backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)


@pytest.mark.parametrize(("path", "parameters", "read_timeout"), [
    ("mods/download", {}, 600.0),
    ("configs/roots/{root_id}/download", {"root_id": "config"}, 600.0),
    ("saves/{save_id:path}/download", {"save_id": "world"}, 600.0),
    ("mods/{mod_name}/download", {"mod_name": "mod"}, 600.0),
    ("factorio/mod-settings/download", {}, 600.0),
    ("map/players", {}, 15.0),
])
def test_only_download_routes_use_the_long_preparation_timeout(
    service: ModWebService, path: str, parameters: dict[str, str], read_timeout: float,
) -> None:
    routes: dict[str, Callable[..., Awaitable[object]]] = {}

    def register_get(path: str) -> Callable[[Callable[..., Awaitable[object]]], Callable[..., Awaitable[object]]]:
        def register(handler: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
            routes[path] = handler
            return handler

        return register

    app = Mock(spec=ModWebFastApiApp, exception_handlers={})
    app.get.side_effect = register_get
    upstream = Mock(spec=aiohttp.ClientResponse, status=200, headers={}, content_length=0)
    upstream.content.iter_chunked = lambda _size: _chunks()
    context = AsyncMock()
    context.__aenter__.return_value = upstream
    session = Mock(spec=aiohttp.ClientSession)
    session.get.return_value = context

    async def exercise() -> None:
        response = cast(Response, await routes[f"/api/node-proxy/{{node_name}}/apps/{{app_name}}/{path}"](
            node_name="review", app_name="game", request=_request(1), **parameters,
        ))
        await response(_scope(), _receive, AsyncMock())

    with (
        patch.object(service._backend, "register_node_api_routes"),
        patch.object(service, "_require_http_user", return_value=_USER),
        patch.object(service, "_remote_node_link", return_value=_NODE),
        patch.object(service, "_remote_token", return_value="review"),
        patch.object(service, "_remote_http_client", AsyncMock(return_value=session)),
    ):
        service._register_routes(nicegui_app=cast(ModWebFastApiApp, app), ui=cast(ModWebRouteUi, Mock()))
        asyncio.run(exercise())
    timeout = cast(aiohttp.ClientTimeout, session.get.call_args.kwargs["timeout"])
    assert timeout.connect == timeout.sock_connect == 15.0
    assert timeout.sock_read == read_timeout
    assert timeout.total is None
    context.__aexit__.assert_awaited_once()


def test_stream_progress_counts_sent_bytes_and_throttles_updates(backend: ModWebDashboardBackend) -> None:
    transfer_id = _start_download(backend)
    assert backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    backend.update_transfer_progress(transfer_id=transfer_id, progress_percent=0)
    close = AsyncMock()
    response = DownloadStreamingResponse(
        _chunks(b"aaaa", b"bbbbbb", b"cccccccccc"), headers={}, media_type=None, close_upstream=close,
        backend=backend, transfer_id=transfer_id, total_bytes=20,
    )
    progress_before_send: list[float | None] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            item = backend.user_transfer_items(user_id=42)[0]
            assert item.state is ModWebNotificationTrayItemState.ACTIVE
            progress_before_send.append(item.progress_percent)

    with patch("web_dash.download_progress.time", SimpleNamespace(monotonic=Mock(side_effect=[0.0, 0.1, 0.3]), perf_counter=lambda: 0.0)):
        asyncio.run(response(_scope(), _receive, send))
    assert progress_before_send == [0, 20, 20, 100]
    close.assert_awaited_once()
    item = backend.user_transfer_items(user_id=42)[0]
    assert item.state is ModWebNotificationTrayItemState.SUCCESS
    assert item.progress_percent == 100
    assert item.detail_text == "Transfer sent."
    assert ModWebStatusNotificationsMixin._transfer_overlay_detail((item,)) == "Download · mod.zip · Transfer sent."


@pytest.mark.parametrize("total_bytes", [None, 0])
def test_unknown_length_and_empty_downloads_finish_normally(
    backend: ModWebDashboardBackend, total_bytes: int | None,
) -> None:
    transfer_id = _start_download(backend)
    assert backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    close = AsyncMock()
    response = DownloadStreamingResponse(
        _chunks(*(b"data",) if total_bytes is None else ()), headers={}, media_type=None, close_upstream=close,
        backend=backend, transfer_id=transfer_id, total_bytes=total_bytes,
    )

    async def send(_message: Message) -> None:
        assert backend.user_transfer_items(user_id=42)[0].progress_percent is None

    asyncio.run(response(_scope(), _receive, send))
    assert backend.user_transfer_items(user_id=42)[0].state is ModWebNotificationTrayItemState.SUCCESS
    close.assert_awaited_once()


@pytest.mark.parametrize("failure", ["upstream", "send", "cancel", "headers", "final_send"])
def test_interrupted_downloads_fail_and_close_without_affecting_uploads(
    backend: ModWebDashboardBackend, failure: Literal["upstream", "send", "cancel", "headers", "final_send"],
) -> None:
    upload_id = backend.start_upload_transfers(user_id=42, filenames=("upload.zip",), detail_text="Uploading…")[0]
    transfer_id = _start_download(backend)
    assert backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    close = AsyncMock()

    async def content() -> AsyncIterator[bytes]:
        yield b"data"
        if failure == "upstream":
            raise RuntimeError("Upstream connection lost")

    async def send(message: Message) -> None:
        if failure == "headers" and message["type"] == "http.response.start":
            raise OSError("Browser connection lost")
        if message["type"] != "http.response.body":
            return
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "send" or (failure == "final_send" and not message.get("more_body")):
            raise OSError("Browser connection lost")

    response = DownloadStreamingResponse(
        content(), headers={}, media_type=None, close_upstream=close, backend=backend,
        transfer_id=transfer_id, total_bytes=4,
    )
    expected_error = asyncio.CancelledError if failure == "cancel" else RuntimeError if failure == "upstream" else ClientDisconnect
    with pytest.raises(expected_error):
        asyncio.run(response(_scope(), _receive, send))
    close.assert_awaited_once()
    assert backend.user_active_transfer_slots(user_id=42) == 1
    items = backend.user_transfer_items(user_id=42)
    assert next(item for item in items if item.label == "mod.zip").state is ModWebNotificationTrayItemState.ERROR
    assert next(item for item in items if item.label == "upload.zip").state is ModWebNotificationTrayItemState.ACTIVE
    backend.update_transfer_progress(transfer_id=upload_id, progress_percent=55)
    assert next(item for item in backend.user_transfer_items(user_id=42) if item.label == "upload.zip").progress_percent == 55


def test_older_asgi_disconnect_also_fails_and_closes(backend: ModWebDashboardBackend) -> None:
    transfer_id = _start_download(backend)
    assert backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    close = AsyncMock()

    async def content() -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b"unreachable"

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    send: Send = AsyncMock()
    response = DownloadStreamingResponse(
        content(), headers={}, media_type=None, close_upstream=close, backend=backend, transfer_id=transfer_id, total_bytes=10,
    )
    asyncio.run(response(_scope("2.0"), receive, send))
    close.assert_awaited_once()
    assert backend.user_transfer_items(user_id=42)[0].state is ModWebNotificationTrayItemState.ERROR


@pytest.mark.parametrize("failure", ["connection", "http", "token", "cancel"])
def test_preparation_failures_release_the_transfer_and_close_received_responses(
    backend: ModWebDashboardBackend, service: ModWebService, failure: Literal["connection", "http", "token", "cancel"],
) -> None:
    transfer_id = _start_download(backend)
    upstream = Mock(spec=aiohttp.ClientResponse, status=503)
    upstream.text = AsyncMock(return_value='{"detail": "node unavailable"}')
    context = AsyncMock()
    context.__aenter__.return_value = upstream
    if failure == "connection":
        context.__aenter__.side_effect = aiohttp.ClientConnectionError("connection failed")
    elif failure == "cancel":
        context.__aenter__.side_effect = asyncio.CancelledError
    session = Mock(spec=aiohttp.ClientSession)
    session.get.return_value = context
    with (
        patch.object(service, "_remote_http_client", AsyncMock(return_value=session)),
        patch.object(service, "_remote_token", side_effect=RuntimeError("no signing secret") if failure == "token" else None,
                     return_value="review"),
        pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError),
    ):
        asyncio.run(service._remote_stream_response_async(
            node=_NODE, app_name="game", path="/apps/game/mods/mod/download", user=_USER,
            scopes=(NodeApiScope.MODS_DOWNLOAD,), request=_request(transfer_id),
        ))
    assert backend.user_active_transfer_slots(user_id=42) == 0
    assert backend.user_transfer_items(user_id=42)[0].state is ModWebNotificationTrayItemState.ERROR
    if failure == "http":
        context.__aexit__.assert_awaited_once()
    else:
        context.__aexit__.assert_not_awaited()


def test_browser_download_is_linked_to_the_reserved_transfer(
    backend: ModWebDashboardBackend, service: ModWebService,
) -> None:
    ui = Mock(spec=ModWebUi)
    ui.download = Mock()
    model = Mock(spec=ModWebBasePageModel, node_name="review", app_color_hex=None)
    with patch.object(service, "_node_role_color_hex", return_value=None):
        asyncio.run(service._start_download(
            ui=cast(ModWebUi, ui), user=_USER, model=cast(ModWebBasePageModel, model),
            url="/api/node-proxy/review/apps/game/mods/download?mod_name=one&mod_name=two", message="Preparing archive…",
            filenames=("bundle.zip",),
        ))
    query = parse_qs(urlsplit(ui.download.call_args.args[0]).query)
    assert query["mod_name"] == ["one", "two"]
    transfer_id = int(query[DOWNLOAD_TRANSFER_QUERY_PARAMETER][0])
    assert backend.claim_download_transfer(transfer_id=transfer_id, user_id=42)
    assert backend.user_transfer_items(user_id=42)[0].label == "bundle.zip"
