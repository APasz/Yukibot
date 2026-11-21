"""Retain generated archives for conditional HTTP download retries."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import Request
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.responses import FileResponse, Response
from starlette.types import Receive, Scope, Send

import config
from _async_utils import run_blocking, run_blocking_cancellable
from _file import require_free_disk_space
from .request_auth import NodeRequestContext

_RETENTION_SECONDS = 24 * 60 * 60
_MAX_ARCHIVES = 64
_MAX_ARCHIVE_BYTES = 32 * 1024**3
log = logging.getLogger(__name__)


@dataclass(slots=True)
class _RetainedArchive:
    response: FileResponse
    expires_at: float
    size: int
    readers: int = 0
    retired: bool = False


class _RetainedFileResponse(FileResponse):
    """Keep a revision alive until ASGI finishes or disconnects, including before opening it."""

    def __init__(self, archive: _RetainedArchive, release: Callable[[], None], *, initial: bool) -> None:
        response = archive.response
        super().__init__(response.path, filename=response.filename, media_type=response.media_type,
                         headers=dict(response.headers), stat_result=response.stat_result,
                         background=response.background if initial else None)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()

    def _should_use_range(self, http_if_range: str) -> bool:
        # Generated revisions can share a modification date despite containing different bytes.
        return http_if_range == self.headers["etag"]


class NodeDownloadStore:
    """Keep immutable generated files scoped to their authenticated owner and request."""

    def __init__(self) -> None:
        self._archives: dict[tuple[str, str, str], _RetainedArchive] = {}
        self._lock = RLock()
        self._retired: dict[Path, _RetainedArchive] = {}
        self._closed = False
        self._last_orphan_cleanup: float | None = None

    def close(self) -> None:
        """Remove retained files when their node service shuts down."""
        with self._lock:
            self._closed = True
            for archive in self._archives.values():
                self._retire(archive)
            self._archives.clear()

    def _retire(self, archive: _RetainedArchive) -> None:
        archive.retired = True
        if archive.readers == 0:
            Path(archive.response.path).unlink(missing_ok=True)
        else:
            self._retired[Path(archive.response.path)] = archive

    def _lease(self, archive: _RetainedArchive, *, initial: bool = False) -> FileResponse:
        archive.readers += 1

        def release() -> None:
            with self._lock:
                archive.readers -= 1
                if archive.retired and archive.readers == 0:
                    Path(archive.response.path).unlink(missing_ok=True)
                    self._retired.pop(Path(archive.response.path), None)

        return _RetainedFileResponse(archive, release, initial=initial)

    async def cleanup(self) -> None:
        """Expire revisions even without new downloads, including files left by a crashed process."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            protected = frozenset(Path(archive.response.path) for archive in self._archives.values()) | frozenset(self._retired)
        if self._last_orphan_cleanup is None or now - self._last_orphan_cleanup >= 600:
            self._last_orphan_cleanup = now
            await run_blocking(self._cleanup_orphans, config.DIR_ZIPS, time.time() - _RETENTION_SECONDS, frozenset(protected))

    @staticmethod
    def _cleanup_orphans(directory: Path, cutoff: float, protected: frozenset[Path]) -> None:
        for path in directory.glob("resume-*"):
            if path in protected or re.fullmatch(r"resume-[0-9a-f]{32}(?:\.[^/]+)?", path.name) is None:
                continue
            try:
                if path.is_file() and path.stat().st_ctime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue

    @staticmethod
    def _resource(request: Request) -> str:
        query = [(name, value) for name, value in request.query_params.multi_items()
                 if name not in {"access_token", "download_transfer_id"}]
        return request.url.path + "?" + urlencode(query)

    @staticmethod
    def _owner(context: NodeRequestContext) -> str:
        if context.grant is not None:
            return context.grant.subject
        if context.web_user is not None:
            return f"web:{context.web_user.discord_id}"
        return "local"

    def _prune(self, now: float, *, required_bytes: int | None = None) -> None:
        total_bytes = self._retained_bytes()
        for key, archive in tuple(self._archives.items()):
            has_capacity = required_bytes is None or (
                len(self._archives) + len(self._retired) < _MAX_ARCHIVES and total_bytes + required_bytes <= _MAX_ARCHIVE_BYTES
            )
            if archive.expires_at > now and has_capacity:
                continue
            self._archives.pop(key)
            self._retire(archive)
            if archive.readers == 0:
                total_bytes -= archive.size

    def _retained_bytes(self) -> int:
        return sum(archive.size for archive in self._archives.values()) + sum(archive.size for archive in self._retired.values())

    @staticmethod
    def _write_response_file(path: Path, body: bytes | memoryview, cancelled: Event) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        require_free_disk_space(path.parent, len(body))
        view = memoryview(body)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            for offset in range(0, len(view), 1024**2):
                if cancelled.is_set():
                    raise InterruptedError("Download snapshot was cancelled.")
                handle.write(view[offset:offset + 1024**2])

    def _uncached_response(self, response: FileResponse, size: int) -> FileResponse:
        return self._lease(_RetainedArchive(response=response, expires_at=0, size=size, retired=True), initial=True)

    async def response(
        self, *, request: Request, context: NodeRequestContext, build: Callable[[], Awaitable[Response]],
    ) -> Response:
        """Reuse a retained revision only after the route has checked current permissions."""
        now = time.monotonic()
        owner, resource = self._owner(context), self._resource(request)
        validator = request.headers.get("if-range") if "range" in request.headers else None
        with self._lock:
            self._prune(now)
            if validator is not None:
                cached = self._archives.get((owner, resource, validator))
                if cached is not None and Path(cached.response.path).is_file():
                    log.info("Download archive resumed: path=%s bytes=%s retained_seconds=%.1f",
                             request.url.path, cached.size, _RETENTION_SECONDS - (cached.expires_at - now))
                    return self._lease(cached)
        response = await build()
        if (type(response) is Response and response.status_code == 200
                and response.headers.get("content-disposition", "").startswith("attachment")
                and len(response.body) <= _MAX_ARCHIVE_BYTES):
            # Live save APIs return bytes; spool once so their retries can use the same range machinery.
            path = config.DIR_ZIPS / f"download-{uuid4().hex}.bin"
            cancelled = Event()
            try:
                await run_blocking_cancellable(cancelled, self._write_response_file, path, response.body, cancelled)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            background = BackgroundTasks([BackgroundTask(path.unlink, missing_ok=True)])
            if response.background is not None:
                background.tasks.append(response.background)
            response = FileResponse(path, headers=dict(response.headers), media_type=response.media_type, background=background)
        if not isinstance(response, FileResponse):
            return response
        source = Path(response.path)
        # App-owned live files continue to use FileResponse's If-Range validation.
        if not source.resolve().is_relative_to(config.DIR_ZIPS.resolve()):
            return response
        stat = source.stat()
        response.set_stat_headers(stat)
        response.stat_result = stat
        download_id = uuid4().hex
        # Equal sizes and filesystem timestamps do not identify equal generated bytes.
        response.headers["ETag"] = f'"{download_id}"'
        if stat.st_size > _MAX_ARCHIVE_BYTES:
            log.warning("Download archive exceeds resume cache capacity: path=%s bytes=%s", request.url.path, stat.st_size)
            return self._uncached_response(response, stat.st_size)
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return self._uncached_response(response, stat.st_size)
            self._prune(now, required_bytes=stat.st_size)
            if (len(self._archives) + len(self._retired) >= _MAX_ARCHIVES
                    or self._retained_bytes() + stat.st_size > _MAX_ARCHIVE_BYTES):
                log.info("Download resume cache occupied by active readers: path=%s bytes=%s", request.url.path, stat.st_size)
                return self._uncached_response(response, stat.st_size)
            retained_path = source.with_name(f"resume-{download_id}{source.suffix}")
            source.rename(retained_path)
            response.path = retained_path
            key = owner, resource, response.headers["etag"]
            previous = self._archives.pop(key, None)
            if previous is not None:
                self._retire(previous)
            archive = _RetainedArchive(response=response, expires_at=now + _RETENTION_SECONDS, size=stat.st_size)
            self._archives[key] = archive
            retained_response = self._lease(archive, initial=True)
        log.info("Download archive retained: path=%s bytes=%s retention_seconds=%s", request.url.path, stat.st_size, _RETENTION_SECONDS)
        return retained_response
