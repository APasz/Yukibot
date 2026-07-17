from __future__ import annotations

import json
import logging
import os
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

LOG_SESSION_COUNT: Final[int] = 3
LOG_SESSION_ID: Final[str] = uuid.uuid4().hex


class HumanLogFormatter(logging.Formatter):
    """Format operator logs while omitting tracebacks explicitly marked as routine."""

    _SUPPRESS_TRACEBACK_ATTRIBUTE: Final[str] = "_yukibot_suppress_traceback"

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, self._SUPPRESS_TRACEBACK_ATTRIBUTE, False):
            return super().format(record)

        original_exc_info = record.exc_info
        original_exc_text = record.exc_text
        record.exc_info = None
        record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info = original_exc_info
            record.exc_text = original_exc_text


class SessionRotatingFileHandler(logging.FileHandler):
    """Keep the current session log and a fixed number of prior sessions."""

    def __init__(
        self,
        filename: str | os.PathLike[str],
        mode: str = "w",
        encoding: str | None = None,
        delay: bool = False,
        errors: str | None = None,
        session_count: int = LOG_SESSION_COUNT,
        legacy_path: str | os.PathLike[str] | None = None,
        current_link_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if mode != "w":
            raise ValueError("SessionRotatingFileHandler only supports write mode.")
        if session_count < 1:
            raise ValueError("session_count must be at least 1.")

        file_path = Path(filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_log(
            legacy_path=None if legacy_path is None else Path(legacy_path),
            file_path=file_path,
            session_count=session_count,
        )
        self._rotate_session_logs(file_path, session_count=session_count)
        if current_link_path is not None:
            self._create_current_log_link(link_path=Path(current_link_path), file_path=file_path)
        super().__init__(file_path, mode=mode, encoding=encoding, delay=delay, errors=errors)

    @staticmethod
    def _archive_path(file_path: Path, session_index: int) -> Path:
        return file_path.with_name(f"{file_path.stem}.{session_index}{file_path.suffix}")

    @classmethod
    def _rotate_session_logs(cls, file_path: Path, *, session_count: int) -> None:
        for session_index in range(session_count - 1, 1, -1):
            previous_path = cls._archive_path(file_path, session_index - 1)
            if not previous_path.exists():
                continue
            try:
                previous_path.replace(cls._archive_path(file_path, session_index))
            except FileNotFoundError:
                # Multiple bot profiles can start together and rotate the shared log directory.
                # Another process may have already moved this archive after we checked it.
                continue

        if session_count > 1 and file_path.exists():
            try:
                file_path.replace(cls._archive_path(file_path, 1))
            except FileNotFoundError:
                # The active file can likewise be moved by a concurrent profile startup.
                pass

    @staticmethod
    def _migrate_legacy_log(*, legacy_path: Path | None, file_path: Path, session_count: int) -> None:
        if legacy_path is None:
            return

        for session_index in range(session_count - 1, 0, -1):
            legacy_archive_path = SessionRotatingFileHandler._archive_path(legacy_path, session_index)
            if not legacy_archive_path.exists():
                continue
            archive_path = SessionRotatingFileHandler._archive_path(file_path, session_index)
            if archive_path.exists():
                legacy_archive_path.unlink()
            else:
                legacy_archive_path.replace(archive_path)

        if legacy_path.is_symlink() or not legacy_path.exists():
            return
        if file_path.exists():
            raise RuntimeError(f"Cannot migrate {legacy_path}: {file_path} already exists.")
        legacy_path.replace(file_path)

    @staticmethod
    def _create_current_log_link(*, link_path: Path, file_path: Path) -> None:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.is_symlink():
            try:
                link_path.unlink()
            except FileNotFoundError:
                # Another profile can replace this shared current-log link between
                # the symlink check and its removal.
                pass
        elif link_path.exists():
            raise RuntimeError(f"Cannot create current-log link: {link_path} already exists.")
        link_path.symlink_to(os.path.relpath(file_path, link_path.parent))


class MachineJsonFormatter(logging.Formatter):
    """Serialize log records as UTC JSON Lines with session context."""

    def __init__(self, *, node_name: str | None = None, bot_profile: str | None = None) -> None:
        super().__init__()
        self._node_name = node_name
        self._bot_profile = bot_profile

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "session_id": LOG_SESSION_ID,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "source": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
            "process_id": record.process,
            "thread_name": record.threadName,
        }
        if self._node_name is not None:
            event["node_name"] = self._node_name
        if self._bot_profile is not None:
            event["bot_profile"] = self._bot_profile

        exception = self._format_exception(record)
        if exception is not None:
            event["exception"] = exception
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _format_exception(record: logging.LogRecord) -> dict[str, str] | None:
        exc_info = record.exc_info
        if not isinstance(exc_info, tuple):
            return None

        exception_type, exception, exception_traceback = exc_info
        if exception_type is None or exception is None:
            return None
        return {
            "type": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception),
            "traceback": "".join(
                traceback.format_exception(exception_type, exception, exception_traceback)
            ).rstrip(),
        }
