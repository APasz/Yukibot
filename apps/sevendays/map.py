"""Read the game's rendered map tiles directly from the selected save."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from map_annotations import MapCoordinate

_INDEX = r"-?[0-9]{1,10}"
_TILE_INDEX = re.compile(_INDEX)
_TILE_NAME = re.compile(rf"({_INDEX})_({_INDEX})\.png")
_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_MAP_DISCOVERY_INTERVAL_SECONDS = 2.0
MAP_TILE_UPDATE_INTERVAL_SECONDS = 15.0
_MAX_METADATA_BYTES = 16 * 1024
_MAX_VIEWPORT_TILES = 4096
log = logging.getLogger(__name__)

type _FileSignature = tuple[int, int, int, int]


def _signature(info: os.stat_result) -> _FileSignature:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _revision(signature: _FileSignature) -> str:
    return "-".join(f"{part:x}" for part in signature)


def matches_etag(validator: str | None, etag: str) -> bool:
    """Apply weak comparison for authenticated conditional GET requests."""
    return validator is not None and any(
        item.strip().removeprefix("W/") in {"*", etag} for item in validator.split(",")
    )


@dataclass(frozen=True, slots=True)
class MapTileRange:
    zoom: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.zoom, self.min_x, self.max_x, self.min_y, self.max_y)):
            raise ValueError("Map tile coordinates must be integers.")
        if not 0 <= self.zoom <= 16 or self.min_x > self.max_x or self.min_y > self.max_y:
            raise ValueError("Invalid map tile range.")
        if (self.max_x - self.min_x + 1) * (self.max_y - self.min_y + 1) > _MAX_VIEWPORT_TILES:
            raise ValueError(f"Map tile range exceeds {_MAX_VIEWPORT_TILES} tiles.")
        if any(not -(1 << 31) <= value < (1 << 31) for value in (self.min_x, self.max_x, self.min_y, self.max_y)):
            raise ValueError("Map tile coordinates are outside the supported range.")


@dataclass(frozen=True, slots=True)
class MapTileIndex:
    """Validated tile revisions; PNG contents remain on disk."""

    tiles: dict[tuple[int, int, int], _FileSignature] = field(default_factory=dict)
    bounds: tuple[MapCoordinate, MapCoordinate] | None = None

    def viewport_response(self, area: MapTileRange) -> tuple[bytes, str]:
        tiles = [
            {"x": x, "y": y, "revision": _revision(signature)}
            for x in range(area.min_x, area.max_x + 1)
            for y in range(area.min_y, area.max_y + 1)
            if (signature := self.tiles.get((area.zoom, x, y))) is not None
        ]
        content = json.dumps({
            "zoom": area.zoom, "tiles": tiles,
            "bounds": [point.to_mapping() for point in self.bounds] if self.bounds is not None else None,
        }, separators=(",", ":")).encode()
        return content, f'"{hashlib.sha256(content).hexdigest()}"'


@dataclass(frozen=True, slots=True)
class SevenDaysMap:
    save: Path
    root: Path
    world_id: str
    display_name: str
    tile_size: int
    max_zoom: int
    initial_zoom: int
    center: MapCoordinate
    sample_tile: Path

    @classmethod
    def load(cls, *, save: Path, world_id: str) -> SevenDaysMap | None:
        save = save.resolve()
        root = (save / "map").resolve()
        if not root.is_dir():
            return None
        if not root.is_relative_to(save):
            raise ValueError("7D2D map directory is outside the selected save.")
        metadata = _contained_path(root, root / "mapinfo.json")
        if not metadata.is_file():
            return None
        with metadata.open("rb") as stream:
            content = stream.read(_MAX_METADATA_BYTES + 1)
        if len(content) > _MAX_METADATA_BYTES:
            raise ValueError("7D2D mapinfo.json exceeds the metadata size limit.")
        raw = cast(object, json.loads(content.decode("utf-8-sig")))
        if not isinstance(raw, dict):
            raise ValueError("7D2D mapinfo.json must be an object.")
        payload = cast(dict[str, object], raw)
        tile_size = payload.get("blockSize")
        max_zoom = payload.get("maxZoom")
        if type(tile_size) is not int or not 16 <= tile_size <= 2048 or tile_size & (tile_size - 1):
            raise ValueError("7D2D map blockSize must be a power of two between 16 and 2048.")
        if type(max_zoom) is not int or not 0 <= max_zoom <= 16:
            raise ValueError("7D2D map maxZoom must be between 0 and 16.")
        preferred_zoom = max(0, max_zoom - 2)
        zoom_levels = (preferred_zoom, *(zoom for zoom in range(max_zoom, -1, -1) if zoom != preferred_zoom))
        for zoom in zoom_levels:
            scale = tile_size * (1 << (max_zoom - zoom))
            for column, row, tile in _tiles_at_zoom(root, zoom):
                if not _has_tile_data(tile, tile_size):
                    continue
                return cls(
                    save=save,
                    root=root,
                    world_id=world_id,
                    display_name=f"{save.parent.name} / {save.name}",
                    tile_size=tile_size,
                    max_zoom=max_zoom,
                    initial_zoom=zoom,
                    center=MapCoordinate(x=(column + 0.5) * scale, z=(row + 0.5) * scale),
                    sample_tile=tile,
                )
        return None

    def read_tile(self, *, zoom: int, tile_name: str) -> bytes:
        content, _ = self.read_tile_result(zoom=zoom, tile_name=tile_name)
        assert content is not None
        return content

    def read_tile_result(
        self, *, zoom: int, tile_name: str, if_none_match: str | None = None,
    ) -> tuple[bytes | None, str]:
        match = _TILE_NAME.fullmatch(tile_name)
        if match is None or not 0 <= zoom <= self.max_zoom:
            raise ValueError("Invalid 7D2D map tile coordinates.")
        x, y = map(_parse_index, match.groups())
        if x is None or y is None:
            raise ValueError("Invalid 7D2D map tile coordinates.")
        # The game numbers rows northward; Leaflet numbers them southward.
        path = _contained_path(self.root, self.root / str(zoom) / str(x) / f"{-y - 1}.png")
        if not path.is_file():
            raise FileNotFoundError("This map tile has not been rendered.")
        with path.open("rb") as stream:
            signature = _signature(os.fstat(stream.fileno()))
            etag = f'"{_revision(signature)}"'
            if matches_etag(if_none_match, etag):
                return None, etag
            content = stream.read(_maximum_tile_bytes(self.tile_size) + 1)
            if signature != _signature(os.fstat(stream.fileno())):
                raise OSError("7D2D map tile changed while being read; retry shortly.")
        if not _complete_tile(content[:33], content[-12:], len(content), self.tile_size):
            raise OSError("7D2D map tile is incomplete or invalid; waiting for it to be rendered again.")
        return content, etag

    def tile_index(self, previous: MapTileIndex | None = None) -> MapTileIndex:
        tiles: dict[tuple[int, int, int], _FileSignature] = {}
        min_x = min_z = float("inf")
        max_x = max_z = float("-inf")
        bounds_zoom = -1
        for zoom in range(self.max_zoom + 1):
            scale = self.tile_size * (1 << (self.max_zoom - zoom))
            for x, row, path in _tiles_at_zoom(self.root, zoom):
                signature = _file_signature(path)
                key = (zoom, x, -row - 1)
                if signature is None:
                    continue
                if previous is None or previous.tiles.get(key) != signature:
                    if not _has_tile_data(path, self.tile_size):
                        signature = previous.tiles.get(key) if previous is not None else None
                        if signature is None:
                            continue
                tiles[key] = signature
                if bounds_zoom != zoom:
                    min_x = min_z = float("inf")
                    max_x = max_z = float("-inf")
                    bounds_zoom = zoom
                min_x, min_z = min(min_x, x * scale), min(min_z, row * scale)
                max_x, max_z = max(max_x, (x + 1) * scale), max(max_z, (row + 1) * scale)
        bounds = (MapCoordinate(x=min_x, z=min_z), MapCoordinate(x=max_x, z=max_z)) if tiles else None
        return MapTileIndex(tiles=tiles, bounds=bounds)


def _tiles_at_zoom(root: Path, zoom: int) -> Iterator[tuple[int, int, Path]]:
    """Enumerate rendered files, resolving directories once and checking file symlinks."""
    level = _contained_path(root, root / str(zoom))
    try:
        columns = os.scandir(level)
    except (FileNotFoundError, NotADirectoryError):
        return
    with columns:
        for column in columns:
            x = _parse_index(column.name)
            if x is None or not column.is_dir():
                continue
            column_path = _contained_path(root, Path(column.path))
            try:
                entries = os.scandir(column_path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            with entries:
                for entry in entries:
                    if not entry.name.endswith(".png"):
                        continue
                    row = _parse_index(entry.name[:-4])
                    if row is None or not entry.is_file():
                        continue
                    path = Path(entry.path)
                    if entry.is_symlink():
                        path = _contained_path(root, path)
                    yield x, row, path


def _contained_path(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("7D2D map path is outside the rendered map directory.")
    return resolved


def _parse_index(value: str) -> int | None:
    if _TILE_INDEX.fullmatch(value) is None:
        return None
    index = int(value)
    return index if -(1 << 31) <= index < (1 << 31) else None


def _maximum_tile_bytes(tile_size: int) -> int:
    # Allow uncompressed 16-bit RGBA data plus PNG/zlib framing overhead.
    return tile_size * tile_size * 8 + 65536


def _complete_tile(header: bytes, tail: bytes, size: int, tile_size: int) -> bool:
    return (
        57 <= size <= _maximum_tile_bytes(tile_size)
        and len(header) == 33
        and header.startswith(_PNG_HEADER)
        and struct.unpack(">II", header[16:24]) == (tile_size, tile_size)
        and zlib.crc32(header[12:29]) == struct.unpack(">I", header[29:33])[0]
        and tail == _PNG_END
    )


def _has_tile_data(path: Path, tile_size: int) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            header = stream.read(33)
            size = stream.seek(0, os.SEEK_END)
            if size < 12:
                return False
            stream.seek(-12, os.SEEK_END)
            return _complete_tile(header, stream.read(12), size, tile_size)
    except FileNotFoundError:
        return False


def _file_signature(path: Path) -> _FileSignature | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return _signature(info)


class SevenDaysMapCache:
    """Coalesce discovery across concurrent requests without caching tile contents."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._identity: tuple[Path, str] | None = None
        self._metadata_signature: _FileSignature | None = None
        self._sample_signature: _FileSignature | None = None
        self._map: SevenDaysMap | None = None
        self._expires_at = 0.0
        self._index_identity: tuple[Path, str, int, int] | None = None
        self._index = MapTileIndex()
        self._index_expires_at = 0.0

    def tile_index(self, rendered: SevenDaysMap) -> MapTileIndex:
        identity = (rendered.root, rendered.world_id, rendered.tile_size, rendered.max_zoom)
        with self._index_lock:
            now = time.monotonic()
            if self._index_identity != identity or now >= self._index_expires_at:
                previous = self._index if self._index_identity == identity else None
                self._index = rendered.tile_index(previous)
                self._index_identity = identity
                self._index_expires_at = time.monotonic() + MAP_TILE_UPDATE_INTERVAL_SECONDS
            return self._index

    def load(self, *, save: Path, world_id: str) -> SevenDaysMap | None:
        identity = (save, world_id)
        with self._lock:
            now = time.monotonic()
            metadata_signature: _FileSignature | None = None
            try:
                metadata_signature = _file_signature(save / "map" / "mapinfo.json")
                sample_signature = (
                    _file_signature(self._map.sample_tile)
                    if identity == self._identity and self._map is not None else None
                )
                if (
                    identity == self._identity and now < self._expires_at
                    and metadata_signature == self._metadata_signature and sample_signature == self._sample_signature
                ):
                    return self._map
                self._map = SevenDaysMap.load(save=save, world_id=world_id)
                self._metadata_signature = metadata_signature
                self._sample_signature = _file_signature(self._map.sample_tile) if self._map is not None else None
            except (OSError, ValueError) as xcp:
                log.warning("7D2D rendered map is unavailable in %s: %s", save, xcp)
                self._map = None
                self._metadata_signature = metadata_signature
                self._sample_signature = None
            self._identity = identity
            self._expires_at = now + _MAP_DISCOVERY_INTERVAL_SECONDS
            return self._map
