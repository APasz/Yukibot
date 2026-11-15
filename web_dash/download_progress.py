"""Byte progress for downloads streamed through the portal."""

from __future__ import annotations

import time
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from .backend import ModWebDashboardBackend

DOWNLOAD_TRANSFER_QUERY_PARAMETER = "download_transfer_id"
_PROGRESS_UPDATE_INTERVAL_SECONDS = 0.2


def download_transfer_url(url: str, transfer_id: int) -> str:
    """Attach portal tracking without changing the download's other query parameters."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != DOWNLOAD_TRANSFER_QUERY_PARAMETER]
    query.append((DOWNLOAD_TRANSFER_QUERY_PARAMETER, str(transfer_id)))
    return urlunsplit(parts._replace(query=urlencode(query)))


class DownloadStreamingResponse(StreamingResponse):
    """Track bytes handed to ASGI and close the upstream even when sending is interrupted."""

    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        headers: Mapping[str, str],
        media_type: str | None,
        close_upstream: Callable[[], Awaitable[None]],
        backend: ModWebDashboardBackend,
        transfer_id: int | None,
        total_bytes: int | None,
    ) -> None:
        super().__init__(content, headers=headers, media_type=media_type)
        self._close_upstream = close_upstream
        self._backend = backend
        self._transfer_id = transfer_id
        self._total_bytes = total_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        sent_bytes = 0
        last_percent = -1
        last_update_at = -float("inf")
        sent_complete_body = False

        async def tracked_send(message: Message) -> None:
            nonlocal sent_bytes, last_percent, last_update_at, sent_complete_body
            await send(message)
            if message["type"] != "http.response.body":
                return
            sent_bytes += len(message.get("body", b""))
            if not message.get("more_body", False):
                sent_complete_body = True
                return
            if self._transfer_id is None or not self._total_bytes:
                return
            percent = min(100, sent_bytes * 100 // self._total_bytes)
            now = time.monotonic()
            if percent == last_percent or now - last_update_at < _PROGRESS_UPDATE_INTERVAL_SECONDS:
                return
            self._backend.update_transfer_progress(transfer_id=self._transfer_id, progress_percent=float(percent))
            last_percent = percent
            last_update_at = now

        try:
            await super().__call__(scope, receive, tracked_send)
        finally:
            try:
                await self._close_upstream()
            finally:
                if self._transfer_id is not None:
                    if sent_complete_body:
                        self._backend.complete_transfer(transfer_id=self._transfer_id, detail_text="Transfer sent.")
                    else:
                        self._backend.fail_transfer(
                            transfer_id=self._transfer_id,
                            detail_text="Download interrupted before the transfer finished.",
                        )
