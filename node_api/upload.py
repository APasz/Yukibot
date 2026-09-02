"""Shared temporary-file handling for streamed node API uploads."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config


class UploadStream(Protocol):
    """The streamed portion of an uploaded file required by Node API persistence."""

    filename: str | None

    async def read(self, size: int) -> bytes: ...

    async def close(self) -> None: ...


class _RequestBodyTooLargeError(Exception):
    """Raised internally when a streamed Node API request exceeds its limit."""


class NodeApiRequestBodyLimitMiddleware:
    """Reject oversized Node API request bodies before multipart parsing writes them to disk."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        api_prefix: str,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("Node API request size limit must be a positive integer.")
        self._app = app
        self._max_bytes = max_bytes
        self._api_prefix = api_prefix

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not self._should_limit(scope):
            await self._app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self._max_bytes:
            await self._send_too_large_response(send)
            return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    received_bytes += len(body)
                    if received_bytes > self._max_bytes:
                        raise _RequestBodyTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLargeError:
            if not response_started:
                await self._send_too_large_response(send)

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
    ) -> None:
        body = f"Request body exceeds the {self._max_bytes} byte limit.".encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
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
