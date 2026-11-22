from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from node_api.bandwidth_routes import register_bandwidth_routes
from node_api.request_auth import NodeRequestAuth, NodeRequestContext
from node_auth import NodeApiScope
from web_api.network_bandwidth import (
    NetworkBandwidthService,
    create_bandwidth_transfer_router,
)
from web_api.network_models import (
    BANDWIDTH_LIMITS,
    NetworkBandwidthSize,
    NetworkBandwidthDuration,
)

ORIGIN = "https://portal.invalid"


def request(token: str, *, origin: str = ORIGIN) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"origin", origin.encode()),
            ],
        }
    )


def test_transfer_credentials_expire_and_cannot_be_used_by_another_origin() -> None:
    now = [0.0]
    service = NetworkBandwidthService(clock=lambda: now[0])
    grant = service.issue(actor_id="one", origin=ORIGIN)
    with pytest.raises(HTTPException) as denied:
        service.acquire(
            request(grant.token, origin="https://other.invalid"), download_bytes=1
        )
    assert denied.value.status_code == 403
    now[0] = 31
    with pytest.raises(HTTPException) as expired:
        service.acquire(request(grant.token), download_bytes=1)
    assert expired.value.status_code == 401


@pytest.mark.parametrize("duration", list(NetworkBandwidthDuration))
def test_session_lifetime_covers_both_selected_phases_and_still_expires(
    duration: NetworkBandwidthDuration,
) -> None:
    now = [0.0]
    service = NetworkBandwidthService(clock=lambda: now[0])
    grant = service.issue(actor_id="one", origin=ORIGIN, duration_ms=duration)
    assert grant.limits.transfer_duration_ms == duration.value
    now[0] = (grant.limits.idle_duration_ms + 2 * duration.value) / 1000 + 1
    session, _ = service.acquire(request(grant.token), upload_bytes=1)
    service.release(session)
    now[0] = 90
    with pytest.raises(HTTPException) as expired:
        service.acquire(request(grant.token), upload_bytes=1)
    assert expired.value.status_code == 401


def test_long_upload_uses_its_session_duration_after_the_old_expiry() -> None:
    now = [0.0]
    service = NetworkBandwidthService(clock=lambda: now[0])
    grant = service.issue(
        actor_id="one", origin=ORIGIN, duration_ms=NetworkBandwidthDuration.SECONDS_20
    )
    now[0] = 31
    req = request(grant.token)
    req.scope["headers"].append((b"content-length", b"1"))

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x", "more_body": False}

    with patch(
        "web_api.network_bandwidth.asyncio.timeout", wraps=asyncio.timeout
    ) as timeout:
        receipt = asyncio.run(service.upload(Request(req.scope, receive)))
    assert receipt.received_bytes == 1
    assert timeout.call_args.args[0] >= NetworkBandwidthDuration.SECONDS_20.value / 1000


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://[",
        "https://:",
        "https://host:bad",
        "https://user:pass@host",
        "https://host/path",
        "https://host/",
    ],
)
def test_invalid_origins_are_rejected(origin: str) -> None:
    with pytest.raises(HTTPException) as denied:
        NetworkBandwidthService().issue(actor_id="one", origin=origin)
    assert denied.value.status_code == 422


def test_sessions_are_capped_and_finishing_does_not_bypass_cooldown() -> None:
    service = NetworkBandwidthService()
    first = service.issue(actor_id="one", origin=ORIGIN)
    service.issue(actor_id="two", origin=ORIGIN)
    with pytest.raises(HTTPException) as full:
        service.issue(actor_id="three", origin=ORIGIN)
    assert full.value.status_code == 409
    service.finish(request(first.token))
    service.issue(actor_id="three", origin=ORIGIN)
    with pytest.raises(HTTPException) as cooldown:
        service.issue(actor_id="one", origin=ORIGIN)
    assert cooldown.value.status_code == 429


def test_parallel_requests_reserve_shared_quotas_before_streaming() -> None:
    limits = BANDWIDTH_LIMITS.model_copy(
        update={
            "max_request_bytes": 8,
            "max_bytes_per_direction": 10,
            "parallel_requests": 2,
        }
    )
    service = NetworkBandwidthService(limits=limits)
    token = service.issue(actor_id="one", origin=ORIGIN).token
    session, first = service.acquire(request(token), download_bytes=8)
    _, second = service.acquire(request(token), download_bytes=8)
    assert (first, second) == (8, 2)
    with pytest.raises(HTTPException) as busy:
        service.acquire(request(token), upload_bytes=1)
    assert busy.value.status_code == 429
    service.release(session)
    with pytest.raises(HTTPException) as exhausted:
        service.acquire(request(token), download_bytes=1)
    assert exhausted.value.status_code == 409
    service.release(session)
    service.acquire(request(token), upload_bytes=8)
    service.release(session)
    with pytest.raises(HTTPException) as upload_exhausted:
        service.acquire(request(token), upload_bytes=3)
    assert upload_exhausted.value.status_code == 409


@pytest.mark.parametrize("size", list(NetworkBandwidthSize))
def test_selected_size_controls_both_server_quotas_without_changing_default_limits(
    size: NetworkBandwidthSize,
) -> None:
    service = NetworkBandwidthService()
    grant = service.issue(actor_id="one", origin=ORIGIN, size_bytes=size)
    assert grant.limits.max_bytes_per_direction == size.value
    assert service.limits == BANDWIDTH_LIMITS
    for _ in range(size.value // grant.limits.max_request_bytes):
        session, count = service.acquire(
            request(grant.token), download_bytes=grant.limits.max_request_bytes
        )
        assert count == grant.limits.max_request_bytes
        service.release(session)
    with pytest.raises(HTTPException) as exhausted:
        service.acquire(request(grant.token), download_bytes=1)
    assert exhausted.value.status_code == 409
    for _ in range(size.value // grant.limits.max_request_bytes):
        session, _ = service.acquire(
            request(grant.token), upload_bytes=grant.limits.max_request_bytes
        )
        service.release(session)
    with pytest.raises(HTTPException) as upload_exhausted:
        service.acquire(request(grant.token), upload_bytes=1)
    assert upload_exhausted.value.status_code == 409


def test_a_refill_waits_for_stream_cleanup_without_reserving_or_replaying_data() -> (
    None
):
    service = NetworkBandwidthService(
        limits=BANDWIDTH_LIMITS.model_copy(update={"parallel_requests": 1})
    )
    grant = service.issue(actor_id="one", origin=ORIGIN)
    first, _ = service.acquire(request(grant.token), download_bytes=1)

    async def refill() -> None:
        waiting = asyncio.create_task(
            service.reserve_transfer(request(grant.token), upload_bytes=2)
        )
        await asyncio.sleep(0.02)
        assert not waiting.done()
        assert first.upload_remaining == grant.limits.max_bytes_per_direction
        service.release(first)
        session, _ = await waiting
        assert session.upload_remaining == grant.limits.max_bytes_per_direction - 2
        service.release(session)

    asyncio.run(refill())


def test_a_stuck_stream_does_not_leave_refills_waiting_indefinitely() -> None:
    service = NetworkBandwidthService(
        limits=BANDWIDTH_LIMITS.model_copy(update={"parallel_requests": 1})
    )
    grant = service.issue(actor_id="one", origin=ORIGIN)
    first, _ = service.acquire(request(grant.token), download_bytes=1)
    with pytest.raises(HTTPException) as busy:
        asyncio.run(service.reserve_transfer(request(grant.token), upload_bytes=1))
    assert busy.value.status_code == 429
    assert first.upload_remaining == grant.limits.max_bytes_per_direction
    service.release(first)


def test_browser_transfers_are_direct_bounded_uncompressed_and_cors_enabled() -> None:
    service = NetworkBandwidthService(
        limits=BANDWIDTH_LIMITS.model_copy(
            update={"max_request_bytes": 8, "max_bytes_per_direction": 10}
        )
    )
    application = FastAPI()
    application.add_middleware(GZipMiddleware, minimum_size=1)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization"],
        expose_headers=["Content-Encoding"],
    )
    application.include_router(create_bandwidth_transfer_router(service))
    token = service.issue(actor_id="one", origin=ORIGIN).token
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    with TestClient(application) as client:
        assert client.get("/api/node/bandwidth/download?size=8").status_code == 401
        assert (
            client.get(f"/api/node/bandwidth/download?size=8&token={token}").status_code
            == 401
        )
        preflight = client.options(
            "/api/node/bandwidth/upload",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        result = client.get("/api/node/bandwidth/download?size=8", headers=headers)
        assert result.status_code == 200 and len(result.content) == 8
        assert result.headers["content-encoding"] == "identity"
        assert result.headers["access-control-allow-origin"] == "*"
        assert "no-store" in result.headers["cache-control"]
        assert (
            len(
                client.get(
                    "/api/node/bandwidth/download?size=8", headers=headers
                ).content
            )
            == 2
        )
        assert (
            client.get(
                "/api/node/bandwidth/download?size=1", headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/node/bandwidth/upload", headers=headers, content=b"123456789"
            ).status_code
            == 413
        )
        result = client.post(
            "/api/node/bandwidth/upload", headers=headers, content=b"12345678"
        )
        assert result.status_code == 200 and result.json() == {"received_bytes": 8}
        assert (
            client.post(
                "/api/node/bandwidth/upload", headers=headers, content=b"123"
            ).status_code
            == 409
        )
        assert (
            client.post("/api/node/bandwidth/finish", headers=headers).status_code
            == 204
        )
        assert (
            client.post(
                "/api/node/bandwidth/upload", headers=headers, content=b"1"
            ).status_code
            == 401
        )


@pytest.mark.parametrize(
    "length,body,status",
    [
        ("0", b"", 411),
        ("-1", b"x", 411),
        ("9" * 5000, b"x", 413),
        ("2", b"x", 400),
        ("1", b"xx", 413),
    ],
)
def test_upload_counts_actual_bytes_and_rejects_invalid_lengths(
    length: str, body: bytes, status: int
) -> None:
    service = NetworkBandwidthService()
    token = service.issue(actor_id="one", origin=ORIGIN).token
    req = request(token)
    req.scope["headers"].append((b"content-length", length.encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request(req.scope, receive)
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(service.upload(req))
    assert invalid.value.status_code == status


def test_node_session_issuer_requires_an_actor_and_read_scope() -> None:
    service = NetworkBandwidthService()
    auth = Mock(spec=NodeRequestAuth)
    context = NodeRequestContext(grant=None, actor_user_id=123)
    auth.require_access.return_value = context
    auth.require_actor.return_value = context
    application = FastAPI()
    register_bandwidth_routes(
        application, auth=auth, service=service, api_prefix="/api/node"
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/node/bandwidth/sessions",
            json={
                "origin": ORIGIN,
                "size_bytes": NetworkBandwidthSize.MIB_64.value,
                "duration_ms": NetworkBandwidthDuration.SECONDS_20.value,
            },
        )
        assert response.status_code == 200
        assert (
            response.json()["limits"]["max_bytes_per_direction"]
            == NetworkBandwidthSize.MIB_64.value
        )
        assert response.headers["cache-control"] == "no-store"
        assert (
            response.json()["limits"]["transfer_duration_ms"]
            == NetworkBandwidthDuration.SECONDS_20.value
        )
        assert auth.require_access.call_args.kwargs["scopes"] == (
            NodeApiScope.APPS_READ,
        )
        auth.require_actor.assert_called_once_with(context)
        auth.require_actor.side_effect = HTTPException(403, "Actor required")
        assert (
            client.post(
                "/api/node/bandwidth/sessions", json={"origin": ORIGIN}
            ).status_code
            == 403
        )


def test_download_disconnect_before_streaming_releases_the_transfer_slot() -> None:
    service = NetworkBandwidthService(
        limits=BANDWIDTH_LIMITS.model_copy(update={"parallel_requests": 1})
    )
    token = service.issue(actor_id="one", origin=ORIGIN).token
    application = FastAPI()
    application.include_router(create_bandwidth_transfer_router(service))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/api/node/bandwidth/download",
        "query_string": b"size=1",
        "scheme": "https",
        "server": ("node.invalid", 443),
        "headers": request(token).scope["headers"],
    }

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise OSError("Disconnected before response headers")

    with pytest.raises(ClientDisconnect):
        asyncio.run(application(scope, receive, send))
    session, _ = service.acquire(request(token), download_bytes=1)
    service.release(session)


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_a_stalled_download_expires_and_releases_its_transfer_slot(
    spec_version: str,
) -> None:
    now = [0.0]
    service = NetworkBandwidthService(
        limits=BANDWIDTH_LIMITS.model_copy(update={"parallel_requests": 1}),
        clock=lambda: now[0],
    )
    token = service.issue(actor_id="one", origin=ORIGIN).token
    now[0] = 29.999
    application = FastAPI()
    application.include_router(create_bandwidth_transfer_router(service))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "method": "GET",
        "path": "/api/node/bandwidth/download",
        "query_string": b"size=1",
        "scheme": "https",
        "server": ("node.invalid", 443),
        "headers": request(token).scope["headers"],
    }

    async def receive() -> Message:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    async def download() -> None:
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(application(scope, receive, send), timeout=1)

    asyncio.run(download())
    session, _ = service.acquire(request(token), download_bytes=1)
    service.release(session)


def test_upload_timeout_releases_the_slot_without_restoring_its_byte_reservation() -> (
    None
):
    now = [0.0]
    service = NetworkBandwidthService(
        limits=BANDWIDTH_LIMITS.model_copy(
            update={
                "parallel_requests": 1,
                "max_request_bytes": 2,
                "max_bytes_per_direction": 2,
            }
        ),
        clock=lambda: now[0],
    )
    token = service.issue(actor_id="one", origin=ORIGIN).token
    now[0] = 29.999
    req = request(token)
    req.scope["headers"].append((b"content-length", b"1"))

    async def receive() -> Message:
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"x", "more_body": False}

    with pytest.raises(HTTPException) as expired:
        asyncio.run(service.upload(Request(req.scope, receive)))
    assert expired.value.status_code == 408
    session, _ = service.acquire(request(token), upload_bytes=1)
    service.release(session)
    with pytest.raises(HTTPException) as exhausted:
        service.acquire(request(token), upload_bytes=1)
    assert exhausted.value.status_code == 409
