from __future__ import annotations

import json
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_MAP_CACHE_FILE_VERSION = 1


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is invalid.")
    text = value.strip()
    if not text:
        raise ValueError(f"{key} is invalid.")
    return text


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} is invalid.")
    text = value.strip()
    return text or None


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} is invalid.")
    return value


def _sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = payload.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{key} is invalid.")
    return cast(Sequence[object], value)


def _normalize_relative_path(relative_path: str) -> str:
    normalized = relative_path.strip().strip("/")
    if not normalized:
        raise ValueError("relative_path is invalid.")
    return normalized


@dataclass(frozen=True, slots=True)
class MapCacheHeader:
    name: str
    value: str

    def __post_init__(self) -> None:
        name = self.name.strip()
        value = self.value.strip()
        if not name:
            raise ValueError("Map cache header name is invalid.")
        if not value:
            raise ValueError("Map cache header value is invalid.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "value", value)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapCacheHeader":
        return cls(
            name=_required_text(payload, "name"),
            value=_required_text(payload, "value"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class MapJsonCacheEntry:
    relative_path: str
    content: str
    media_type: str | None
    headers: tuple[MapCacheHeader, ...]
    updated_at_unix_ms: int

    def __post_init__(self) -> None:
        normalized_path = _normalize_relative_path(self.relative_path)
        if self.content == "":
            raise ValueError("Map cache content is invalid.")
        if self.updated_at_unix_ms <= 0:
            raise ValueError("Map cache updated_at_unix_ms is invalid.")
        object.__setattr__(self, "relative_path", normalized_path)
        object.__setattr__(self, "media_type", _optional_text({"media_type": self.media_type}, "media_type"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapJsonCacheEntry":
        raw_headers = _sequence(payload, "headers")
        return cls(
            relative_path=_required_text(payload, "relative_path"),
            content=_required_text(payload, "content"),
            media_type=_optional_text(payload, "media_type"),
            headers=tuple(
                MapCacheHeader.from_mapping(cast(Mapping[str, object], raw_header))
                if isinstance(raw_header, Mapping)
                else _raise_invalid_header()
                for raw_header in raw_headers
            ),
            updated_at_unix_ms=_required_int(payload, "updated_at_unix_ms"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "content": self.content,
            "media_type": self.media_type,
            "headers": [header.to_mapping() for header in self.headers],
            "updated_at_unix_ms": self.updated_at_unix_ms,
        }

    @property
    def header_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple((header.name, header.value) for header in self.headers)


def _raise_invalid_header() -> MapCacheHeader:
    raise ValueError("headers is invalid.")


@dataclass(frozen=True, slots=True)
class MapJsonCacheDocument:
    entries: tuple[MapJsonCacheEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapJsonCacheDocument":
        version = payload.get("version")
        if version != _MAP_CACHE_FILE_VERSION:
            raise ValueError(f"Unsupported map cache version: {version!r}")
        raw_entries = _sequence(payload, "entries")
        return cls(
            entries=tuple(
                MapJsonCacheEntry.from_mapping(cast(Mapping[str, object], raw_entry))
                if isinstance(raw_entry, Mapping)
                else _raise_invalid_entry()
                for raw_entry in raw_entries
            )
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": _MAP_CACHE_FILE_VERSION,
            "entries": [entry.to_mapping() for entry in self.entries],
        }


def _raise_invalid_entry() -> MapJsonCacheEntry:
    raise ValueError("entries is invalid.")


class AppMapJsonCacheStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load_entry(self, relative_path: str) -> MapJsonCacheEntry | None:
        normalized_path = _normalize_relative_path(relative_path)
        with self._lock:
            document = self._load_document()
        for entry in document.entries:
            if entry.relative_path == normalized_path:
                return entry
        return None

    def save_entry(
        self,
        *,
        relative_path: str,
        content: str,
        media_type: str | None,
        headers: tuple[tuple[str, str], ...],
    ) -> MapJsonCacheEntry:
        normalized_path = _normalize_relative_path(relative_path)
        entry = MapJsonCacheEntry(
            relative_path=normalized_path,
            content=content,
            media_type=media_type,
            headers=tuple(MapCacheHeader(name=name, value=value) for name, value in headers),
            updated_at_unix_ms=int(time.time() * 1000),
        )
        with self._lock:
            document = self._load_document()
            updated_entries = {
                cached_entry.relative_path: cached_entry
                for cached_entry in document.entries
                if cached_entry.relative_path != normalized_path
            }
            updated_entries[normalized_path] = entry
            self._write_document(
                MapJsonCacheDocument(
                    entries=tuple(updated_entries[key] for key in sorted(updated_entries)),
                )
            )
        return entry

    def _load_document(self) -> MapJsonCacheDocument:
        if not self._path.exists():
            return MapJsonCacheDocument(entries=())
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Map cache root is invalid.")
        return MapJsonCacheDocument.from_mapping(cast(Mapping[str, object], payload))

    def _write_document(self, document: MapJsonCacheDocument) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._path.parent,
            delete=False,
        ) as handle:
            json.dump(document.to_mapping(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(self._path)


__all__: tuple[str, ...] = (
    "AppMapJsonCacheStore",
    "MapCacheHeader",
    "MapJsonCacheEntry",
)
