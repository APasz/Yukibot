"""Read the game's rendered map tiles directly from the selected save."""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from map_annotations import MapCoordinate

_INDEX = r"-?[0-9]{1,10}"
_TILE_INDEX = re.compile(_INDEX)
_TILE_NAME = re.compile(rf"({_INDEX})_({_INDEX})\.png")
_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_MAP_DISCOVERY_INTERVAL_SECONDS = 2.0
_MAX_METADATA_BYTES = 16 * 1024
log = logging.getLogger(__name__)

type _FileSignature = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class SevenDaysMap:
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
            level = _contained_path(root, root / str(zoom))
            if not level.is_dir():
                continue
            with os.scandir(level) as columns:
                for column in columns:
                    column_index = _parse_index(column.name)
                    if column_index is None or not column.is_dir():
                        continue
                    column_path = _contained_path(root, Path(column.path))
                    with os.scandir(column_path) as tiles:
                        for entry in tiles:
                            name = Path(entry.name)
                            tile_index = _parse_index(name.stem)
                            if name.suffix != ".png" or tile_index is None:
                                continue
                            tile = _contained_path(root, Path(entry.path))
                            if not _has_tile_data(tile, tile_size):
                                continue
                            scale = tile_size * (1 << (max_zoom - zoom))
                            return cls(
                                root=root,
                                world_id=world_id,
                                display_name=f"{save.parent.name} / {save.name}",
                                tile_size=tile_size,
                                max_zoom=max_zoom,
                                initial_zoom=zoom,
                                center=MapCoordinate(
                                    x=(column_index + 0.5) * scale, z=(tile_index + 0.5) * scale,
                                ),
                                sample_tile=tile,
                            )
        return None

    def read_tile(self, *, zoom: int, tile_name: str) -> bytes:
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
            content = stream.read(_maximum_tile_bytes(self.tile_size) + 1)
        if not _complete_tile(content[:33], content[-12:], len(content), self.tile_size):
            raise OSError("7D2D map tile is incomplete or invalid; waiting for it to be rendered again.")
        return content


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
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


class SevenDaysMapCache:
    """Coalesce discovery across concurrent requests without caching tile contents."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identity: tuple[Path, str] | None = None
        self._metadata_signature: _FileSignature | None = None
        self._sample_signature: _FileSignature | None = None
        self._map: SevenDaysMap | None = None
        self._expires_at = 0.0

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
