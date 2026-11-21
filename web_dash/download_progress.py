"""Byte progress for downloads streamed through the portal."""

from __future__ import annotations

import time
import logging
import re
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from .backend import ModWebDashboardBackend

DOWNLOAD_TRANSFER_QUERY_PARAMETER = "download_transfer_id"
_PROGRESS_UPDATE_INTERVAL_SECONDS = 0.2
log = logging.getLogger(__name__)


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
        status_code: int = 200,
    ) -> None:
        super().__init__(content, headers=headers, media_type=media_type, status_code=status_code)
        self._close_upstream = close_upstream
        self._backend = backend
        self._transfer_id = transfer_id
        self._total_bytes = total_bytes
        self._progress_offset = 0
        self._progress_total = total_bytes
        if self.status_code == 206 and (match := re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", self.headers.get("content-range", ""))):
            start, end, total = (int(part) for part in match.groups())
            if not 0 <= start <= end < total:
                raise ValueError("Download response contains an invalid byte range.")
            self._progress_offset, self._progress_total = start, total

    @property
    def initial_progress_percent(self) -> float | None:
        return None if not self._progress_total else self._progress_offset * 100.0 / self._progress_total

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        sent_bytes = 0
        last_percent = -1
        last_update_at = -float("inf")
        sent_complete_body = False
        started_at = time.perf_counter()

        async def tracked_send(message: Message) -> None:
            nonlocal sent_bytes, last_percent, last_update_at, sent_complete_body
            await send(message)
            if message["type"] != "http.response.body":
                return
            sent_bytes += len(message.get("body", b""))
            if not message.get("more_body", False):
                sent_complete_body = True
                return
            if self._transfer_id is None or not self._progress_total:
                return
            percent = min(100, (self._progress_offset + sent_bytes) * 100 // self._progress_total)
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
                    if sent_complete_body and 200 <= self.status_code < 300:
                        self._backend.complete_transfer(transfer_id=self._transfer_id, detail_text="Transfer sent.")
                    else:
                        self._backend.fail_transfer(
                            transfer_id=self._transfer_id,
                            detail_text=("Requested byte range is unavailable. Restart the download." if self.status_code == 416
                                         else "Download interrupted before the transfer finished."),
                        )
                elapsed = time.perf_counter() - started_at
                log.info(
                    "Download stream finished: transfer=%s status=%s complete=%s bytes=%s expected_bytes=%s "
                    "duration_seconds=%.3f bytes_per_second=%.0f partial=%s",
                    self._transfer_id, self.status_code, sent_complete_body, sent_bytes, self._total_bytes,
                    elapsed, sent_bytes / elapsed if elapsed > 0 else 0, self.status_code == 206,
                )
