"""Shared temporary-file handling for streamed node API uploads."""

from __future__ import annotations

import tempfile
import asyncio
import logging
from time import perf_counter
from pathlib import Path, PurePosixPath
from typing import Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config
from _async_utils import run_blocking
from _file import InsufficientDiskSpaceError, require_free_disk_space

_MULTIPART_OVERHEAD_BYTES = 1024**2
log = logging.getLogger(__name__)


def is_mod_upload_path(path: str, api_prefix: str) -> bool:
    prefix = f"{api_prefix.rstrip('/')}/apps/"
    if not path.startswith(prefix):
        return False
    parts = path.removeprefix(prefix).split("/")
    return len(parts) in {3, 4} and bool(parts[0]) and parts[1:] in (["mods", "upload"], ["mods", "upload", "inspect"])


class UploadStream(Protocol):
    """The streamed portion of an uploaded file required by Node API persistence."""

    filename: str | None

    async def read(self, size: int) -> bytes: ...

    async def close(self) -> None: ...


class _RequestBodyTooLargeError(Exception):
    """Raised internally when a streamed Node API request exceeds its limit."""


class _UploadStorageFullError(Exception):
    """Abort reception before exhausting the temporary filesystem."""


class NodeApiRequestBodyLimitMiddleware:
    """Reject oversized Node API request bodies before multipart parsing writes them to disk."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        api_prefix: str,
        mod_max_bytes: int | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("Node API request size limit must be a positive integer.")
        self._app = app
        self._max_bytes = max_bytes
        self._api_prefix = api_prefix
        if mod_max_bytes is not None and (isinstance(mod_max_bytes, bool) or not isinstance(mod_max_bytes, int) or mod_max_bytes <= 0):
            raise ValueError("Mod upload size limit must be a positive integer.")
        self._mod_max_bytes = mod_max_bytes
        self._mod_upload_lock = asyncio.Lock()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not self._should_limit(scope):
            await self._app(scope, receive, send)
            return

        mod_upload = self._mod_max_bytes is not None and is_mod_upload_path(scope["path"], self._api_prefix)
        limit = self._mod_max_bytes + _MULTIPART_OVERHEAD_BYTES if mod_upload and self._mod_max_bytes is not None else self._max_bytes
        if mod_upload and self._mod_upload_lock.locked():
            await self._send_error(send, 429, "Another mod upload is being received. Please retry shortly.")
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > limit:
            await self._send_too_large_response(send, limit)
            return
        if mod_upload:
            await self._mod_upload_lock.acquire()
            try:
                await run_blocking(require_free_disk_space, Path(tempfile.gettempdir()), max(content_length or 0, 0))
            except InsufficientDiskSpaceError as xcp:
                self._mod_upload_lock.release()
                await self._send_error(send, 507, str(xcp))
                return
            except BaseException:
                self._mod_upload_lock.release()
                raise

        received_bytes = 0
        started_at = perf_counter()
        last_chunk_at = started_at
        largest_chunk_gap = 0.0
        received_chunks = 0
        response_status: int | None = None
        next_space_check = 64 * 1024**2
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, next_space_check, last_chunk_at, largest_chunk_gap, received_chunks
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    now = perf_counter()
                    largest_chunk_gap = max(largest_chunk_gap, now - last_chunk_at)
                    last_chunk_at = now
                    received_chunks += 1
                    received_bytes += len(body)
                    if received_bytes > limit:
                        raise _RequestBodyTooLargeError
                    if mod_upload and received_bytes >= next_space_check:
                        try:
                            await run_blocking(require_free_disk_space, Path(tempfile.gettempdir()), 0)
                        except InsufficientDiskSpaceError as xcp:
                            raise _UploadStorageFullError(str(xcp)) from xcp
                        next_space_check = received_bytes + 64 * 1024**2
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started, response_status
            if message.get("type") == "http.response.start":
                response_started = True
                response_status = message["status"]
            await send(message)

        try:
            await self._app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLargeError:
            if not response_started:
                await self._send_too_large_response(tracked_send, limit)
        except _UploadStorageFullError as xcp:
            if not response_started:
                await self._send_error(tracked_send, 507, str(xcp))
        finally:
            if mod_upload:
                self._mod_upload_lock.release()
            if any(name.lower() == b"content-type" and value.lower().startswith(b"multipart/form-data")
                   for name, value in scope.get("headers", [])):
                receive_duration = last_chunk_at - started_at
                log.info(
                    "Upload request finished: path=%s status=%s bytes=%s expected_bytes=%s chunks=%s "
                    "receive_seconds=%.3f request_seconds=%.3f bytes_per_second=%.0f max_chunk_gap_seconds=%.3f",
                    scope["path"], response_status, received_bytes, content_length, received_chunks, receive_duration,
                    perf_counter() - started_at, received_bytes / receive_duration if receive_duration > 0 else 0,
                    largest_chunk_gap,
                )

    def _should_limit(self, scope: Scope) -> bool:
        return (
            scope.get("type") == "http"
            and scope.get("method") in {"POST", "PUT", "PATCH"}
            and isinstance(scope.get("path"), str)
            and scope["path"].startswith(self._api_prefix)
        )

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() != b"content-length":
                continue
            try:
                return int(raw_value)
            except (TypeError, ValueError):
                return None
        return None

    async def _send_too_large_response(
        self,
        send: Send,
        max_bytes: int,
    ) -> None:
        await self._send_error(send, 413, f"Request body exceeds the {max_bytes} byte limit.")

    @staticmethod
    async def _send_error(send: Send, status: int, detail: str) -> None:
        body = detail.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def validated_upload_filename(filename: str, *, kind: str) -> str:
    """Return a safe single-component upload filename."""
    resolved = filename.strip()
    if not resolved:
        raise ValueError(f"{kind} upload filename is required.")
    if (
        resolved in {".", ".."}
        or PurePosixPath(resolved).name != resolved
        or "\\" in resolved
    ):
        raise ValueError(f"{kind} upload filename must not include directories.")
    return resolved


async def persist_upload_to_temp(upload: UploadStream, *, max_bytes: int | None = None) -> Path:
    """Write an upload to a temporary file and close its request stream."""
    limit = config.NODE_API_UPLOAD_MAX_BYTES if max_bytes is None else max_bytes
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("Upload size limit must be a positive integer.")
    suffix = Path(upload.filename or "upload").suffix
    temp_path: Path | None = None
    uploaded_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(
            prefix="yukibot-upload-", suffix=suffix, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            while chunk := await upload.read(1024 * 1024):
                uploaded_bytes += len(chunk)
                if uploaded_bytes > limit:
                    raise ValueError(f"Upload exceeds the {limit} byte limit.")
                handle.write(chunk)
            return temp_path
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
