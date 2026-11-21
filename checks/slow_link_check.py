from __future__ import annotations

import asyncio
from contextlib import nullcontext
from collections.abc import AsyncIterator
from dataclasses import replace
import logging
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from fastapi import FastAPI, HTTPException, Request
import httpx
from nicegui.element import Element
import pytest
from starlette.responses import FileResponse, Response

import config
from _security import Power_Level
from _file import File_Utils
from apps._app import App
from node_api.downloads import NodeDownloadStore
from node_api.request_auth import NodeRequestAuth, NodeRequestContext
from node_api.storage_routes import register_storage_routes
from node_api.system import NodeSystemHistory, NodeSystemSample, NodeSystemSummary
from node_auth import NodeAccessGrant, NodeApiScope, issue_node_token
from web_dash.live_updates import CoalescedUiUpdate
from web_dash.download_progress import DownloadStreamingResponse
from web_dash.backend import ModWebDashboardBackend
from web_dash.types import ModWebNotificationTrayItemState
from web_dash.nicegui_protocols import ModWebUi
from web_dash.service import ModWebService
from checks.download_progress_check import _NODE, _USER, _chunks, _receive, _scope
from checks.networking_check import _serve
from aiohttp import web


@pytest.mark.parametrize("inferred", [False, True])
@pytest.mark.parametrize("expired_at_start", [False, True])
def test_save_upload_authorizes_before_receiving_body(inferred: bool, expired_at_start: bool) -> None:
    now = 1400 if expired_at_start else 1000
    body_received = False
    secret = "slow-link-test"
    grant = NodeAccessGrant(subject="web:42", node="test", app="game",
                            scopes=frozenset({NodeApiScope.SAVES_WRITE}), expires_at=1300)
    auth = NodeRequestAuth(node_name=lambda: "test", http_exception=HTTPException, logger=logging.getLogger(__name__))
    result = Mock(save=SimpleNamespace(id="save"), saves=(SimpleNamespace(id="save", root_id="root"),))
    result.to_mapping.return_value = {"ok": True}
    storage = Mock(upload_save_file=AsyncMock(return_value=result), upload_save_files_to_inferred_roots=AsyncMock(return_value=result))
    app_double = Mock(spec=App, save_file_write_level=Power_Level.user)
    app_double.name = "game"
    app_model = cast(App, app_double)
    app = FastAPI()
    register_storage_routes(app, auth=auth, resolve_app=lambda _: app_model, storage=storage, factorio=Mock(),
                            blueprints=Mock(), api_prefix="/api/node", http_exception=HTTPException, traffic_log=Mock())

    async def content() -> AsyncIterator[bytes]:
        nonlocal now, body_received
        body_received = True
        yield b'--test\r\nContent-Disposition: form-data; name="root_id"\r\n\r\nroot\r\n'
        now = 1401
        yield (b'--test\r\nContent-Disposition: form-data; name="upload"; filename="save.zip"\r\n'
               b'Content-Type: application/zip\r\n\r\nsave bytes\r\n--test--\r\n')

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/node/apps/game/saves/upload{'-inferred' if inferred else ''}",
                content=content(), headers={"Content-Type": "multipart/form-data; boundary=test",
                                           "Authorization": f"Bearer {issue_node_token(secret=secret, grant=grant)}"})
        assert response.status_code == (403 if expired_at_start else 200)

    with (patch.object(config, "MOD_WEB_SERVER", replace(config.MOD_WEB_SERVER, token_secret=secret)),
          patch("node_auth.time", SimpleNamespace(time=lambda: now)), patch("node_api.storage_routes.audit_log")):
        asyncio.run(exercise())
    assert body_received is not expired_at_start
    handler = storage.upload_save_files_to_inferred_roots if inferred else storage.upload_save_file
    if expired_at_start:
        handler.assert_not_awaited()
    else:
        handler.assert_awaited_once()
        uploads = handler.call_args.kwargs["uploads"] if inferred else [handler.call_args.kwargs["upload"]]
        assert all(upload.file.closed for upload in uploads)


def _request(*, headers: dict[str, str] | None = None, path: str = "/download", query: bytes = b"") -> Request:
    return Request({"type": "http", "scheme": "http", "server": ("test", 80), "path": path,
                    "query_string": query, "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]})


@pytest.mark.parametrize("status", [200, 206, 416])
def test_proxy_preserves_range_headers_status_and_closes_upstream(status: int) -> None:
    async def exercise() -> None:
        service = ModWebService()
        content_range = "bytes */10" if status == 416 else "bytes 5-9/10"
        upstream = Mock(spec=aiohttp.ClientResponse, status=status, content_length=5,
                        headers={"Content-Length": "5", "Content-Range": content_range, "Accept-Ranges": "bytes", "ETag": '"test"'})
        upstream.content.iter_chunked = lambda _: _chunks(b"56789")
        context = AsyncMock()
        context.__aenter__.return_value = upstream
        session = Mock(spec=aiohttp.ClientSession)
        session.get.return_value = context
        with (patch.object(service, "_remote_token", return_value="token"),
              patch.object(service, "_remote_http_client", AsyncMock(return_value=session))):
            response = await service._remote_stream_response_async(node=_NODE, app_name="game", path="download",
                scopes=(NodeApiScope.SAVES_DOWNLOAD,), user=_USER,
                request=_request(headers={"Range": "bytes=5-", "If-Range": '"test"'}))
            assert response.status_code == status
            assert response.headers["Content-Range"] == content_range
            assert response.headers["Accept-Ranges"] == "bytes"
            assert session.get.call_args.kwargs["headers"]["Range"] == "bytes=5-"
            assert session.get.call_args.kwargs["headers"]["If-Range"] == '"test"'
            await response(_scope(), _receive, AsyncMock())
            context.__aexit__.assert_awaited_once()
    asyncio.run(exercise())


@pytest.mark.parametrize("in_memory", [False, True])
def test_retained_archive_resumes_same_bytes_and_is_scoped_to_owner_and_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_memory: bool) -> None:
    monkeypatch.setattr(config, "DIR_ZIPS", tmp_path)
    store = NodeDownloadStore()
    builds = 0
    owner = NodeRequestContext(grant=None, web_user=_USER)
    app = FastAPI()

    @app.get("/download")
    async def download(request: Request) -> Response:
        async def build() -> Response:
            nonlocal builds
            builds += 1
            body = b"original bytes" if builds == 1 else b"changed bytes"
            if in_memory:
                return Response(body, headers={"Content-Disposition": 'attachment; filename="archive.zip"'})
            path = tmp_path / f"archive-{builds}.zip"
            path.write_bytes(body)
            return FileResponse(path, filename="archive.zip")
        context = owner if request.headers.get("user", "42") == "42" else replace(owner, web_user=replace(_USER, discord_id=99))
        return await store.response(request=request, context=context, build=build)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            initial = await client.get("/download?selection=a")
            headers = {"Range": "bytes=5-", "If-Range": initial.headers["etag"]}
            resumed = await client.get("/download?selection=a", headers=headers)
            assert resumed.status_code == 206
            assert resumed.content == initial.content[5:]
            assert resumed.headers["etag"] == initial.headers["etag"]
            assert builds == 1
            invalid = await client.get("/download?selection=a", headers={**headers, "Range": "bytes=500-"})
            assert invalid.status_code == 416 and invalid.headers["Content-Range"] == f"bytes */{len(initial.content)}"
            assert builds == 1
            await client.get("/download?selection=b", headers=headers)
            await client.get("/download?selection=a", headers={**headers, "user": "99"})
            assert builds == 3
        store.close()
        assert not list(tmp_path.glob("resume-*"))
    asyncio.run(exercise())


def test_retained_archive_expiry_capacity_and_active_response_lifetime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DIR_ZIPS", tmp_path)
    monkeypatch.setattr("node_api.downloads._MAX_ARCHIVES", 1)
    store = NodeDownloadStore()
    owner = NodeRequestContext(grant=None, web_user=_USER)

    async def build(name: str) -> Response:
        path = tmp_path / name
        path.write_bytes(name.encode())
        return FileResponse(path)

    async def consume(response: Response) -> None:
        await response({"type": "http", "method": "GET", "headers": [], "extensions": {}}, _receive, AsyncMock())

    async def exercise() -> None:
        first = await store.response(request=_request(), context=owner, build=lambda: build("first.zip"))
        assert isinstance(first, FileResponse)
        first_path = Path(first.path)
        # Eviction must not unlink a response that has not opened its file yet.
        second = await store.response(request=_request(path="/second"), context=owner, build=lambda: build("second.zip"))
        assert first_path.exists()
        with patch("node_api.downloads.time", SimpleNamespace(monotonic=lambda: float("inf"), time=lambda: float("inf"))):
            await store.cleanup()
        assert first_path.exists()
        await consume(first)
        assert not first_path.exists()
        await consume(second)
        assert not (tmp_path / "second.zip").exists()
        third = await store.response(request=_request(path="/third"), context=owner, build=lambda: build("third.zip"))
        await consume(third)
        with patch("node_api.downloads.time", SimpleNamespace(monotonic=lambda: float("inf"), time=lambda: 0)):
            await store.cleanup()
        assert not list(tmp_path.glob("resume-*"))
    asyncio.run(exercise())


def test_cancelled_live_save_snapshot_removes_its_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DIR_ZIPS", tmp_path)
    store = NodeDownloadStore()
    started, finish = Event(), Event()
    writer = store._write_response_file

    def slow_write(path: Path, body: bytes | memoryview, cancelled: Event) -> None:
        started.set()
        assert finish.wait(5)
        writer(path, body, cancelled)

    async def exercise() -> None:
        task = asyncio.create_task(store.response(request=_request(), context=NodeRequestContext(grant=None, web_user=_USER),
            build=AsyncMock(return_value=Response(b"save bytes", headers={"Content-Disposition": "attachment"}))))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(tmp_path.iterdir())
    with patch.object(store, "_write_response_file", side_effect=slow_write):
        asyncio.run(exercise())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_archive_build_removes_its_unique_output_after_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    monkeypatch.setattr(config, "DIR_ZIPS", tmp_path)
    source = tmp_path / "save.dat"
    source.write_bytes(b"save")
    started, finish, stopped = Event(), Event(), Event()

    def compress(_source: Path, output: Path) -> None:
        started.set()
        try:
            assert finish.wait(5)
            output.write_bytes(b"unfinished archive")
            if not cancelled:
                raise OSError("Compression failed")
        finally:
            stopped.set()

    async def exercise() -> None:
        task = asyncio.create_task(File_Utils.compress(source, "save.zip", unique_output=True))
        try:
            while not started.is_set():
                await asyncio.sleep(0.01)
            if cancelled:
                task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError if cancelled else OSError):
                await task
            assert stopped.is_set()
            assert list(tmp_path.iterdir()) == [source]
        finally:
            finish.set()
    with patch.object(File_Utils, "compress_file", side_effect=compress):
        asyncio.run(exercise())


def test_same_size_and_timestamp_archives_keep_distinct_validators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DIR_ZIPS", tmp_path)
    store = NodeDownloadStore()
    builds = 0
    app = FastAPI()

    @app.get("/download")
    async def download(request: Request) -> Response:
        async def build() -> Response:
            nonlocal builds
            builds += 1
            path = tmp_path / f"archive-{builds}.zip"
            path.write_bytes(b"aaaa" if builds == 1 else b"bbbb")
            os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
            return FileResponse(path)
        return await store.response(request=request, context=NodeRequestContext(grant=None, web_user=_USER), build=build)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.get("/download")
            second = await client.get("/download")
            assert first.headers["etag"] != second.headers["etag"]
            resumed = await client.get("/download", headers={"Range": "bytes=1-", "If-Range": first.headers["etag"]})
            assert resumed.status_code == 206 and resumed.content == b"aaa" and builds == 2
            # HTTP dates cannot distinguish generated revisions created in the same second.
            dated = await client.get("/download", headers={"Range": "bytes=1-", "If-Range": first.headers["last-modified"]})
            assert dated.status_code == 200 and dated.content == b"bbbb"
        store.close()
    asyncio.run(exercise())


def test_resumed_download_reclaims_its_tracker_and_counts_the_existing_offset() -> None:
    backend = ModWebDashboardBackend()
    transfer = backend.start_download_transfers(user_id=42, filenames=("save.zip",), detail_text="Preparing…")[0]
    assert backend.claim_download_transfer(transfer_id=transfer, user_id=42)
    backend.fail_transfer(transfer_id=transfer, detail_text="Interrupted")
    assert not backend.claim_download_transfer(transfer_id=transfer, user_id=99, resume=True)
    assert backend.claim_download_transfer(transfer_id=transfer, user_id=42, resume=True)
    assert not backend.claim_download_transfer(transfer_id=transfer, user_id=42, resume=True)
    response = DownloadStreamingResponse(_chunks(b"56789"), status_code=206,
        headers={"Content-Range": "bytes 5-9/10"}, media_type=None, close_upstream=AsyncMock(),
        backend=backend, transfer_id=transfer, total_bytes=5)
    assert response.initial_progress_percent == 50
    asyncio.run(response(_scope(), _receive, AsyncMock()))
    item = backend.user_transfer_items(user_id=42)[0]
    assert item.state is ModWebNotificationTrayItemState.SUCCESS and item.progress_percent == 100
    backend.clear_user_transfers(user_id=42)


def test_ranges_work_through_a_real_upstream_http_connection(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    data = bytes(range(256)) * 1024
    path.write_bytes(data)

    async def exercise() -> None:
        upstream = web.Application()
        async def archive(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(path)

        upstream.router.add_get("/archive", archive)
        service = ModWebService()
        async with _serve(upstream) as base_url:
            app = FastAPI()

            @app.get("/download")
            async def download(request: Request) -> Response:
                return await service._remote_stream_response_async(
                    node=replace(_NODE, api_base_url=base_url), app_name="game", path="archive",
                    scopes=(NodeApiScope.SAVES_DOWNLOAD,), user=_USER, request=request,
                )

            try:
                with patch.object(service, "_remote_token", return_value="test"):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        initial = await client.get("/download")
                        assert initial.status_code == 200 and initial.content == data
                        resumed = await client.get("/download", headers={"Range": "bytes=131072-", "If-Range": initial.headers["etag"]})
                        assert resumed.status_code == 206 and resumed.content == data[131072:]
                        assert resumed.headers["Content-Range"] == f"bytes 131072-{len(data) - 1}/{len(data)}"
                        invalid = await client.get("/download", headers={"Range": "bytes=1000000-"})
                        assert invalid.status_code == 416
                        assert invalid.headers["Content-Range"] == f"bytes */{len(data)}"
            finally:
                await service._close_remote_http_client()
    asyncio.run(exercise())


def test_ui_updates_coalesce_wait_for_ack_and_pause_when_hidden_or_disconnected() -> None:
    async def exercise() -> None:
        client = SimpleNamespace(has_socket_connection=True, run_javascript=Mock(), layout=nullcontext())
        target = cast(Element, Mock(spec=Element, id=1, client=client))
        bridge = Mock(id=2)
        bridge.classes.return_value = bridge
        ui = Mock(spec=ModWebUi)
        ui.element.return_value = bridge
        apply = Mock()
        updates = CoalescedUiUpdate[int](ui=ui, target=target, apply=apply, interval=0.01)
        callbacks = {call.args[0]: call.args[1] for call in bridge.on.call_args_list}
        visible = callbacks["mod-web-visibility"]
        ack = callbacks["mod-web-update-ack"]
        updates.submit(1)
        await asyncio.sleep(0.02)
        apply.assert_not_called()
        visible(SimpleNamespace(args=True))
        updates.submit(2)
        updates.submit(3)
        await asyncio.sleep(0.02)
        apply.assert_called_once_with(3, True)
        updates.submit(4)
        await asyncio.sleep(0.02)
        apply.assert_called_once()
        ack(SimpleNamespace(args=1))
        await asyncio.sleep(0.02)
        assert apply.call_args.args == (4, False)
        ack(SimpleNamespace(args=2))
        visible(SimpleNamespace(args=False))
        updates.submit(5)
        await asyncio.sleep(0.02)
        assert apply.call_count == 2
        client.has_socket_connection = False
        visible(SimpleNamespace(args=True))
        await asyncio.sleep(0.02)
        assert apply.call_count == 2
        client.has_socket_connection = True
        visible(SimpleNamespace(args=True))
        await asyncio.sleep(0.02)
        assert apply.call_args.args == (5, True)
        updates.close()
        updates.close()
        updates.submit(6)
        await asyncio.sleep(0.02)
        assert apply.call_count == 3
        bridge.delete.assert_called_once()
    asyncio.run(exercise())


def test_frequent_chart_samples_advance_history_across_interval_boundaries() -> None:
    history = NodeSystemHistory(retention_seconds=3600, sample_interval_seconds=10, samples=())
    for captured_at in range(100, 132, 2):
        summary = NodeSystemSummary(cpu_percent=25, ram_percent=50, ram_used_bytes=None, ram_total_bytes=None,
            storage_percent=30, storage_free_bytes=None, storage_total_bytes=None, captured_at_epoch_seconds=captured_at)
        history = ModWebService._append_node_system_history(history, summary)
    assert [sample.captured_at_epoch_seconds for sample in history.samples] == [108, 118, 128, 130]
    assert all(isinstance(sample, NodeSystemSample) for sample in history.samples)


def test_visible_surface_replays_latest_value_after_a_missed_browser_update() -> None:
    async def exercise() -> None:
        client = SimpleNamespace(has_socket_connection=True, run_javascript=Mock(), layout=nullcontext())
        target = cast(Element, Mock(spec=Element, id=1, client=client))
        bridge = Mock(id=2)
        bridge.classes.return_value = bridge
        ui = Mock(spec=ModWebUi)
        ui.element.return_value = bridge
        apply = Mock()
        updates = CoalescedUiUpdate[int](ui=ui, target=target, apply=apply, interval=0.01)
        callbacks = {call.args[0]: call.args[1] for call in bridge.on.call_args_list}
        visible = callbacks["mod-web-visibility"]
        visible(SimpleNamespace(args=True))
        updates.submit(1)
        await asyncio.sleep(0.02)
        callbacks["mod-web-update-ack"](SimpleNamespace(args=1))
        # A target can be detached after the server queues its JavaScript, losing its delta.
        visible(SimpleNamespace(args=False))
        visible(SimpleNamespace(args=True))
        await asyncio.sleep(0.02)
        assert apply.call_count == 2
        assert apply.call_args.args == (1, True)
        updates.close()
    asyncio.run(exercise())
