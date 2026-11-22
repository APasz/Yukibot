"""Bounded, short-lived browser transfers served by the selected node itself."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from .network_models import (
    BANDWIDTH_LIMITS,
    NetworkBandwidthGrant,
    NetworkBandwidthDuration,
    NetworkBandwidthLimits,
    NetworkBandwidthReceipt,
    NetworkBandwidthSize,
)

PORTAL_BANDWIDTH_PREFIX: Final[str] = "/api/network/bandwidth/transfers"

_MIN_SESSION_SECONDS: Final[float] = 30.0
_SESSION_GRACE_SECONDS: Final[float] = 10.0
_COOLDOWN_SECONDS: Final[float] = 60.0
_MAX_SESSIONS: Final[int] = 2
_CHUNK_BYTES: Final[int] = 64 * 1024
_SLOT_WAIT_RETRIES: Final[int] = 25
_SLOT_WAIT_SECONDS: Final[float] = 0.01
_TRANSFER_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store, no-transform",
    "Content-Encoding": "identity",
    "X-Accel-Buffering": "no",
    "Referrer-Policy": "no-referrer",
}


@dataclass(slots=True)
class _TransferSession:
    origin: str
    expires_at: float
    limits: NetworkBandwidthLimits
    download_remaining: int
    upload_remaining: int
    active_transfers: int = 0
    closed: bool = False


class NetworkBandwidthService:
    """Issue opaque transfer-only credentials with shared quotas and expiry."""

    def __init__(
        self,
        *,
        limits: NetworkBandwidthLimits = BANDWIDTH_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _TransferSession] = {}
        self._cooldowns: dict[str, float] = {}
        self._payload = secrets.token_bytes(_CHUNK_BYTES)

    def issue(
        self,
        *,
        actor_id: str,
        origin: str,
        size_bytes: NetworkBandwidthSize | None = None,
        duration_ms: NetworkBandwidthDuration | None = None,
    ) -> NetworkBandwidthGrant:
        limits = self.limits
        max_bytes = (
            limits.max_bytes_per_direction if size_bytes is None else size_bytes.value
        )
        transfer_duration = (
            limits.transfer_duration_ms if duration_ms is None else duration_ms.value
        )
        if (
            max_bytes != limits.max_bytes_per_direction
            or transfer_duration != limits.transfer_duration_ms
        ):
            limits = limits.model_copy(
                update={
                    "max_bytes_per_direction": max_bytes,
                    "transfer_duration_ms": transfer_duration,
                }
            )
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError as error:
            raise HTTPException(422, "A valid browser origin is required.") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or origin != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise HTTPException(422, "A valid browser origin is required.")
        if not actor_id:
            raise ValueError("A bandwidth test needs an authenticated actor.")
        with self._lock:
            now = self._clock()
            for token, session in tuple(self._sessions.items()):
                if session.expires_at <= now:
                    session.closed = True
                    del self._sessions[token]
            self._cooldowns = {
                actor: deadline
                for actor, deadline in self._cooldowns.items()
                if deadline > now
            }
            if actor_id in self._cooldowns:
                raise HTTPException(
                    429, "Wait a minute before testing this node again."
                )
            if len(self._sessions) >= _MAX_SESSIONS:
                raise HTTPException(409, "This node is already being tested.")
            token = secrets.token_urlsafe(32)
            self._sessions[token] = _TransferSession(
                origin=origin,
                expires_at=now
                + max(
                    _MIN_SESSION_SECONDS,
                    (limits.idle_duration_ms + 2 * limits.transfer_duration_ms) / 1000
                    + _SESSION_GRACE_SECONDS,
                ),
                limits=limits,
                download_remaining=limits.max_bytes_per_direction,
                upload_remaining=limits.max_bytes_per_direction,
            )
            self._cooldowns[actor_id] = now + _COOLDOWN_SECONDS
        return NetworkBandwidthGrant(token=token, limits=limits)

    def _resolve(self, request: Request) -> _TransferSession:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "A bandwidth test credential is required.")
        session = self._sessions.get(token)
        if session is None or session.closed or session.expires_at <= self._clock():
            raise HTTPException(401, "Bandwidth test expired. Start a new test.")
        origin = request.headers.get("origin")
        if origin is not None and origin != session.origin:
            raise HTTPException(403, "Bandwidth test belongs to another origin.")
        return session

    def acquire(
        self, request: Request, *, download_bytes: int = 0, upload_bytes: int = 0
    ) -> tuple[_TransferSession, int]:
        with self._lock:
            session = self._resolve(request)
            if session.active_transfers >= session.limits.parallel_requests:
                raise HTTPException(429, "Too many simultaneous bandwidth transfers.")
            if max(download_bytes, upload_bytes) > session.limits.max_request_bytes:
                raise HTTPException(
                    413, "Bandwidth request exceeds the transfer limit."
                )
            count = min(download_bytes, session.download_remaining)
            if download_bytes and count == 0:
                raise HTTPException(409, "Bandwidth download allowance exhausted.")
            if upload_bytes > session.upload_remaining:
                raise HTTPException(409, "Bandwidth upload allowance exhausted.")
            session.download_remaining -= count
            session.upload_remaining -= upload_bytes
            session.active_transfers += 1
            return session, count

    def release(self, session: _TransferSession) -> None:
        with self._lock:
            session.active_transfers -= 1

    async def reserve_transfer(
        self, request: Request, *, download_bytes: int = 0, upload_bytes: int = 0
    ) -> tuple[_TransferSession, int]:
        """Allow completed streams to release their slot before rejecting a refill."""
        retries = 0
        while True:
            try:
                return self.acquire(
                    request, download_bytes=download_bytes, upload_bytes=upload_bytes
                )
            except HTTPException as error:
                if error.status_code != 429 or retries >= _SLOT_WAIT_RETRIES:
                    raise
            retries += 1
            await asyncio.sleep(_SLOT_WAIT_SECONDS)

    def finish(self, request: Request) -> None:
        with self._lock:
            session = self._resolve(request)
            session.closed = True
            self._sessions = {
                token: item
                for token, item in self._sessions.items()
                if item is not session
            }

    def _active(self, session: _TransferSession) -> bool:
        return not session.closed and session.expires_at > self._clock()

    def transfer_timeout(self, session: _TransferSession) -> float:
        """Bound blocked reads and writes by the phase and credential lifetime."""
        return max(
            0.001,
            min(
                session.limits.transfer_duration_ms / 1000 + 2,
                session.expires_at - self._clock(),
            ),
        )

    async def download(
        self, session: _TransferSession, count: int
    ) -> AsyncIterator[bytes]:
        remaining = count
        while remaining and self._active(session):
            size = min(remaining, len(self._payload))
            yield self._payload if size == len(self._payload) else self._payload[:size]
            remaining -= size
            await asyncio.sleep(0)

    async def upload(self, request: Request) -> NetworkBandwidthReceipt:
        raw_length = request.headers.get("content-length", "")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise HTTPException(411, "A known upload size is required.")
        if len(raw_length) > len(str(self.limits.max_request_bytes)):
            raise HTTPException(413, "Bandwidth request exceeds the transfer limit.")
        expected = int(raw_length)
        if expected <= 0:
            raise HTTPException(411, "A known upload size is required.")
        session, _ = await self.reserve_transfer(request, upload_bytes=expected)
        received = 0
        try:
            try:
                async with asyncio.timeout(self.transfer_timeout(session)):
                    async for chunk in request.stream():
                        received += len(chunk)
                        with self._lock:
                            if not self._active(session):
                                raise HTTPException(401, "Bandwidth test expired.")
                            if received > expected:
                                raise HTTPException(
                                    413, "Bandwidth upload exceeds its declared size."
                                )
            except TimeoutError as error:
                raise HTTPException(408, "Bandwidth upload timed out.") from error
            except ClientDisconnect as error:
                raise HTTPException(400, "Bandwidth upload cancelled.") from error
            if received != expected:
                raise HTTPException(400, "Bandwidth upload ended early.")
            return NetworkBandwidthReceipt(received_bytes=received)
        finally:
            self.release(session)


class _DownloadResponse(StreamingResponse):
    """Release the slot even if disconnect occurs before the iterator starts."""

    def __init__(
        self, service: NetworkBandwidthService, session: _TransferSession, count: int
    ) -> None:
        self._service = service
        self._session = session
        super().__init__(
            service.download(session, count),
            media_type="application/octet-stream",
            headers={**_TRANSFER_HEADERS, "Content-Length": str(count)},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with asyncio.timeout(self._service.transfer_timeout(self._session)):
                await super().__call__(scope, receive, send)
        except TimeoutError as error:
            raise ClientDisconnect() from error
        finally:
            self._service.release(self._session)


def create_bandwidth_transfer_router(
    service: NetworkBandwidthService, *, prefix: str = "/api/node/bandwidth"
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["bandwidth-transfers"])

    @router.get("/download")
    async def download(
        request: Request,
        size: Annotated[int, Query(ge=1, le=BANDWIDTH_LIMITS.max_request_bytes)],
    ) -> StreamingResponse:
        session, count = await service.reserve_transfer(request, download_bytes=size)
        return _DownloadResponse(service, session, count)

    @router.post("/upload")
    async def upload(request: Request, response: Response) -> NetworkBandwidthReceipt:
        response.headers.update(_TRANSFER_HEADERS)
        return await service.upload(request)

    @router.post("/finish", status_code=204)
    async def finish(request: Request) -> Response:
        service.finish(request)
        return Response(status_code=204, headers=_TRANSFER_HEADERS)

    return router
