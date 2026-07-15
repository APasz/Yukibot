from __future__ import annotations

import json
import logging
import os
import threading
from _thread import RLock
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from logging import Logger
from pathlib import Path
from tempfile import NamedTemporaryFile

log: Logger = logging.getLogger(__name__)

PROCESS_RESTART_STATE_PATH: Path = Path("restart_type_sentinel.json")
PENDING_PROCESS_RESTART_KIND_PATH: Path = Path("pending_restart_type_sentinel.json")


class RestartKind(StrEnum):
    EXTERNAL = "external"
    SCHEDULED_BOT = "scheduled_bot"
    SCHEDULED_SYS = "scheduled_sys"
    MANUAL_BOT = "manual_bot"
    MANUAL_SYS = "manual_sys"
    UPDATE_BOT = "update_bot"
    SCHEDULED_VOICE = "scheduled_voice"
    MANUAL_VOICE = "manual_voice"


_PROCESS_RESTART_KINDS: frozenset[RestartKind] = frozenset[RestartKind](
    {
        RestartKind.SCHEDULED_BOT,
        RestartKind.SCHEDULED_SYS,
        RestartKind.MANUAL_BOT,
        RestartKind.MANUAL_SYS,
        RestartKind.UPDATE_BOT,
    }
)
_VOICE_RESTART_KINDS: frozenset[RestartKind] = frozenset[RestartKind](
    {RestartKind.SCHEDULED_VOICE, RestartKind.MANUAL_VOICE}
)


@dataclass(frozen=True, slots=True)
class RestartRecord:
    timestamp: int
    kind: RestartKind

    def __post_init__(self) -> None:
        if self.timestamp <= 0:
            raise ValueError("Restart record timestamp must be positive Unix seconds.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RestartRecord:
        raw_timestamp: object | None = payload.get("timestamp")
        if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, int):
            raise ValueError("Restart record timestamp is invalid.")
        raw_kind: object | None = payload.get("kind")
        if not isinstance(raw_kind, str):
            raise ValueError("Restart record kind is invalid.")
        try:
            kind: RestartKind = RestartKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("Restart record kind is invalid.") from xcp
        return cls(timestamp=raw_timestamp, kind=kind)

    def to_mapping(self) -> dict[str, object]:
        return {"timestamp": self.timestamp, "kind": self.kind.value}


_voice_restart_lock: RLock = threading.RLock()
_voice_restart_record: RestartRecord | None = None


def process_restart_kind(*, scheduled: bool, restart_sys: bool) -> RestartKind:
    if scheduled:
        return RestartKind.SCHEDULED_SYS if restart_sys else RestartKind.SCHEDULED_BOT
    return RestartKind.MANUAL_SYS if restart_sys else RestartKind.MANUAL_BOT


def is_process_restart_kind(kind: RestartKind) -> bool:
    return kind in _PROCESS_RESTART_KINDS


def mark_pending_process_restart(kind: RestartKind) -> None:
    if not is_process_restart_kind(kind):
        raise ValueError(f"{kind.value!r} is not a process restart kind.")
    _write_json(PENDING_PROCESS_RESTART_KIND_PATH, {"kind": kind.value})


def mark_pending_process_restart_if_missing(kind: RestartKind) -> None:
    if PENDING_PROCESS_RESTART_KIND_PATH.exists():
        return
    mark_pending_process_restart(kind)


def record_process_start(started_at: datetime) -> RestartRecord:
    global _voice_restart_record
    timestamp: int = int(started_at.timestamp())
    kind: RestartKind = _consume_pending_process_restart_kind()
    record: RestartRecord = RestartRecord(timestamp=timestamp, kind=kind)
    _write_json(PROCESS_RESTART_STATE_PATH, record.to_mapping())
    with _voice_restart_lock:
        _voice_restart_record = None
    return record


def read_process_restart_record(*, default_timestamp: int) -> RestartRecord:
    payload: dict[str, object] | None = _read_json_object(PROCESS_RESTART_STATE_PATH)
    if payload is None:
        return RestartRecord(timestamp=default_timestamp, kind=RestartKind.EXTERNAL)
    try:
        return RestartRecord.from_mapping(payload)
    except ValueError as xcp:
        log.warning("Ignoring invalid process restart sentinel: %s", xcp)
        return RestartRecord(timestamp=default_timestamp, kind=RestartKind.EXTERNAL)


def record_voice_restart(kind: RestartKind, *, restarted_at: datetime | None = None) -> RestartRecord:
    global _voice_restart_record
    if kind not in _VOICE_RESTART_KINDS:
        raise ValueError(f"{kind.value!r} is not a voice restart kind.")
    record: RestartRecord = RestartRecord(
        timestamp=int((restarted_at or datetime.now().astimezone()).timestamp()), kind=kind
    )
    with _voice_restart_lock:
        _voice_restart_record = record
    return record


def read_voice_restart_record() -> RestartRecord | None:
    with _voice_restart_lock:
        return _voice_restart_record


def _consume_pending_process_restart_kind() -> RestartKind:
    payload: dict[str, object] | None = _read_json_object(PENDING_PROCESS_RESTART_KIND_PATH)
    PENDING_PROCESS_RESTART_KIND_PATH.unlink(missing_ok=True)
    if payload is None:
        return RestartKind.EXTERNAL
    raw_kind: object | None = payload.get("kind")
    if not isinstance(raw_kind, str):
        log.warning("Ignoring invalid pending restart kind sentinel.")
        return RestartKind.EXTERNAL
    try:
        kind: RestartKind = RestartKind(raw_kind)
    except ValueError:
        log.warning("Ignoring unknown pending restart kind sentinel: %r", raw_kind)
        return RestartKind.EXTERNAL
    if not is_process_restart_kind(kind):
        log.warning("Ignoring non-process pending restart kind sentinel: %s", kind.value)
        return RestartKind.EXTERNAL
    return kind


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        raw_payload: str = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as xcp:
        log.warning("Restart sentinel read failed path=%s: %s", path, xcp)
        return None
    try:
        payload: object = json.loads(raw_payload)
    except ValueError as xcp:
        log.warning("Restart sentinel JSON parse failed path=%s: %s", path, xcp)
        return None
    if not isinstance(payload, dict):
        log.warning("Restart sentinel JSON payload is not an object: path=%s", path)
        return None
    return dict[str, object](payload)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(f"{json.dumps(dict[str, object](payload), sort_keys=True)}\n", encoding="utf-8")


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        temporary_file.write(f"{json.dumps(dict[str, object](payload), sort_keys=True)}\n")
        temporary_path = Path(temporary_file.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
