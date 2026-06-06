from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

_ANNOTATION_FILE_VERSION = 1
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


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


def _required_number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} is invalid.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{key} is invalid.")
    return number


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


class MapAnnotationShape(StrEnum):
    MARKER = "marker"
    POLYLINE = "polyline"


@dataclass(frozen=True, slots=True)
class MapCoordinate:
    x: float
    z: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.x):
            raise ValueError("Map coordinate x is invalid.")
        if not math.isfinite(self.z):
            raise ValueError("Map coordinate z is invalid.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapCoordinate":
        return cls(
            x=_required_number(payload, "x"),
            z=_required_number(payload, "z"),
        )

    def to_mapping(self) -> dict[str, float]:
        return {
            "x": self.x,
            "z": self.z,
        }


@dataclass(frozen=True, slots=True)
class MapAnnotationDraft:
    world_name: str
    shape: MapAnnotationShape
    label: str
    color_hex: str
    points: tuple[MapCoordinate, ...]

    def __post_init__(self) -> None:
        world_name = self.world_name.strip()
        label = self.label.strip()
        color_hex = self.color_hex.strip()
        if not world_name:
            raise ValueError("Map annotation world_name is invalid.")
        if not label:
            raise ValueError("Map annotation label is invalid.")
        if _HEX_COLOR_RE.fullmatch(color_hex) is None:
            raise ValueError("Map annotation color_hex is invalid.")
        if self.shape is MapAnnotationShape.MARKER and len(self.points) != 1:
            raise ValueError("Map marker annotations require exactly one point.")
        if self.shape is MapAnnotationShape.POLYLINE and len(self.points) < 2:
            raise ValueError("Map polyline annotations require at least two points.")
        object.__setattr__(self, "world_name", world_name)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "color_hex", color_hex.upper())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapAnnotationDraft":
        raw_shape = _required_text(payload, "shape")
        raw_points = _sequence(payload, "points")
        try:
            shape = MapAnnotationShape(raw_shape)
        except ValueError as xcp:
            raise ValueError("shape is invalid.") from xcp
        points = tuple(
            MapCoordinate.from_mapping(cast(Mapping[str, object], raw_point))
            if isinstance(raw_point, Mapping)
            else _raise_invalid_point()
            for raw_point in raw_points
        )
        return cls(
            world_name=_required_text(payload, "world_name"),
            shape=shape,
            label=_required_text(payload, "label"),
            color_hex=_required_text(payload, "color_hex"),
            points=points,
        )


def _raise_invalid_point() -> MapCoordinate:
    raise ValueError("points is invalid.")


@dataclass(frozen=True, slots=True)
class MapAnnotation:
    annotation_id: str
    world_name: str
    shape: MapAnnotationShape
    label: str
    color_hex: str
    points: tuple[MapCoordinate, ...]
    created_at_unix_ms: int
    created_by_user_id: int | None = None
    created_by_name: str | None = None

    def __post_init__(self) -> None:
        annotation_id = self.annotation_id.strip()
        world_name = self.world_name.strip()
        label = self.label.strip()
        color_hex = self.color_hex.strip()
        if not annotation_id:
            raise ValueError("Map annotation annotation_id is invalid.")
        if not world_name:
            raise ValueError("Map annotation world_name is invalid.")
        if not label:
            raise ValueError("Map annotation label is invalid.")
        if _HEX_COLOR_RE.fullmatch(color_hex) is None:
            raise ValueError("Map annotation color_hex is invalid.")
        if self.created_at_unix_ms < 0:
            raise ValueError("Map annotation created_at_unix_ms is invalid.")
        if self.created_by_user_id is not None and self.created_by_user_id < 0:
            raise ValueError("Map annotation created_by_user_id is invalid.")
        if self.shape is MapAnnotationShape.MARKER and len(self.points) != 1:
            raise ValueError("Map marker annotations require exactly one point.")
        if self.shape is MapAnnotationShape.POLYLINE and len(self.points) < 2:
            raise ValueError("Map polyline annotations require at least two points.")
        if self.created_by_name is not None and not self.created_by_name.strip():
            raise ValueError("Map annotation created_by_name is invalid.")
        object.__setattr__(self, "annotation_id", annotation_id)
        object.__setattr__(self, "world_name", world_name)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "color_hex", color_hex.upper())
        if self.created_by_name is not None:
            object.__setattr__(self, "created_by_name", self.created_by_name.strip())

    @classmethod
    def create(
        cls,
        *,
        draft: MapAnnotationDraft,
        created_by_user_id: int | None,
        created_by_name: str | None,
    ) -> "MapAnnotation":
        return cls(
            annotation_id=uuid.uuid4().hex,
            world_name=draft.world_name,
            shape=draft.shape,
            label=draft.label,
            color_hex=draft.color_hex,
            points=draft.points,
            created_at_unix_ms=int(time.time() * 1000),
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapAnnotation":
        raw_shape = _required_text(payload, "shape")
        raw_points = _sequence(payload, "points")
        try:
            shape = MapAnnotationShape(raw_shape)
        except ValueError as xcp:
            raise ValueError("shape is invalid.") from xcp
        points = tuple(
            MapCoordinate.from_mapping(cast(Mapping[str, object], raw_point))
            if isinstance(raw_point, Mapping)
            else _raise_invalid_point()
            for raw_point in raw_points
        )
        created_by_user_id_raw = payload.get("created_by_user_id")
        if created_by_user_id_raw is None:
            created_by_user_id = None
        elif isinstance(created_by_user_id_raw, bool) or not isinstance(created_by_user_id_raw, int):
            raise ValueError("created_by_user_id is invalid.")
        else:
            created_by_user_id = created_by_user_id_raw
        return cls(
            annotation_id=_required_text(payload, "annotation_id"),
            world_name=_required_text(payload, "world_name"),
            shape=shape,
            label=_required_text(payload, "label"),
            color_hex=_required_text(payload, "color_hex"),
            points=points,
            created_at_unix_ms=_required_int(payload, "created_at_unix_ms"),
            created_by_user_id=created_by_user_id,
            created_by_name=_optional_text(payload, "created_by_name"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "annotation_id": self.annotation_id,
            "world_name": self.world_name,
            "shape": self.shape.value,
            "label": self.label,
            "color_hex": self.color_hex,
            "points": [point.to_mapping() for point in self.points],
            "created_at_unix_ms": self.created_at_unix_ms,
            "created_by_user_id": self.created_by_user_id,
            "created_by_name": self.created_by_name,
        }


@dataclass(frozen=True, slots=True)
class MapWorldSummary:
    name: str
    display_name: str
    world_type: str
    order: int

    def __post_init__(self) -> None:
        name = self.name.strip()
        display_name = self.display_name.strip()
        world_type = self.world_type.strip()
        if not name:
            raise ValueError("Map world name is invalid.")
        if not display_name:
            raise ValueError("Map world display_name is invalid.")
        if not world_type:
            raise ValueError("Map world world_type is invalid.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "world_type", world_type)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapWorldSummary":
        return cls(
            name=_required_text(payload, "name"),
            display_name=_required_text(payload, "display_name"),
            world_type=_required_text(payload, "world_type"),
            order=_required_int(payload, "order"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "world_type": self.world_type,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class MapManifest:
    app_name: str
    app_friendly: str
    node_name: str
    public_map_url: str
    icon_base_url: str
    initial_world_name: str
    worlds: tuple[MapWorldSummary, ...]

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("Map manifest app_name is invalid.")
        if not self.app_friendly.strip():
            raise ValueError("Map manifest app_friendly is invalid.")
        if not self.node_name.strip():
            raise ValueError("Map manifest node_name is invalid.")
        if not self.public_map_url.strip():
            raise ValueError("Map manifest public_map_url is invalid.")
        if not self.icon_base_url.strip():
            raise ValueError("Map manifest icon_base_url is invalid.")
        if not self.initial_world_name.strip():
            raise ValueError("Map manifest initial_world_name is invalid.")
        if not self.worlds:
            raise ValueError("Map manifest requires at least one world.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapManifest":
        raw_worlds = _sequence(payload, "worlds")
        worlds = tuple(
            MapWorldSummary.from_mapping(cast(Mapping[str, object], raw_world))
            if isinstance(raw_world, Mapping)
            else _raise_invalid_world()
            for raw_world in raw_worlds
        )
        return cls(
            app_name=_required_text(payload, "app_name"),
            app_friendly=_required_text(payload, "app_friendly"),
            node_name=_required_text(payload, "node_name"),
            public_map_url=_required_text(payload, "public_map_url"),
            icon_base_url=_required_text(payload, "icon_base_url"),
            initial_world_name=_required_text(payload, "initial_world_name"),
            worlds=worlds,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node_name": self.node_name,
            "public_map_url": self.public_map_url,
            "icon_base_url": self.icon_base_url,
            "initial_world_name": self.initial_world_name,
            "worlds": [world.to_mapping() for world in self.worlds],
        }


def _raise_invalid_world() -> MapWorldSummary:
    raise ValueError("worlds is invalid.")


@dataclass(frozen=True, slots=True)
class MapAnnotationList:
    app_name: str
    app_friendly: str
    node_name: str
    annotations: tuple[MapAnnotation, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapAnnotationList":
        raw_annotations = _sequence(payload, "annotations")
        annotations = tuple(
            MapAnnotation.from_mapping(cast(Mapping[str, object], raw_annotation))
            if isinstance(raw_annotation, Mapping)
            else _raise_invalid_annotation()
            for raw_annotation in raw_annotations
        )
        return cls(
            app_name=_required_text(payload, "app_name"),
            app_friendly=_required_text(payload, "app_friendly"),
            node_name=_required_text(payload, "node_name"),
            annotations=annotations,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node_name": self.node_name,
            "annotations": [annotation.to_mapping() for annotation in self.annotations],
        }


def _raise_invalid_annotation() -> MapAnnotation:
    raise ValueError("annotations is invalid.")


@dataclass(frozen=True, slots=True)
class MapAnnotationMutationResult:
    app_name: str
    app_friendly: str
    node_name: str
    message: str
    annotation: MapAnnotation | None = None
    deleted_annotation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("Map annotation mutation result app_name is invalid.")
        if not self.app_friendly.strip():
            raise ValueError("Map annotation mutation result app_friendly is invalid.")
        if not self.node_name.strip():
            raise ValueError("Map annotation mutation result node_name is invalid.")
        if not self.message.strip():
            raise ValueError("Map annotation mutation result message is invalid.")
        if self.annotation is None and self.deleted_annotation_id is None:
            raise ValueError("Map annotation mutation results require an annotation or a deleted annotation id.")
        if self.deleted_annotation_id is not None and not self.deleted_annotation_id.strip():
            raise ValueError("Map annotation mutation result deleted_annotation_id is invalid.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MapAnnotationMutationResult":
        raw_annotation = payload.get("annotation")
        if raw_annotation is not None and not isinstance(raw_annotation, Mapping):
            raise ValueError("annotation is invalid.")
        return cls(
            app_name=_required_text(payload, "app_name"),
            app_friendly=_required_text(payload, "app_friendly"),
            node_name=_required_text(payload, "node_name"),
            message=_required_text(payload, "message"),
            annotation=MapAnnotation.from_mapping(cast(Mapping[str, object], raw_annotation))
            if raw_annotation is not None
            else None,
            deleted_annotation_id=_optional_text(payload, "deleted_annotation_id"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node_name": self.node_name,
            "message": self.message,
            "annotation": self.annotation.to_mapping() if self.annotation is not None else None,
            "deleted_annotation_id": self.deleted_annotation_id,
        }


class AppMapAnnotationStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def list_annotations(self) -> tuple[MapAnnotation, ...]:
        with self._lock:
            return self._load_annotations()

    def create_annotation(
        self,
        *,
        draft: MapAnnotationDraft,
        created_by_user_id: int | None,
        created_by_name: str | None,
    ) -> MapAnnotation:
        with self._lock:
            annotations = list(self._load_annotations())
            annotation = MapAnnotation.create(
                draft=draft,
                created_by_user_id=created_by_user_id,
                created_by_name=created_by_name,
            )
            annotations.append(annotation)
            self._write_annotations(tuple(annotations))
            return annotation

    def delete_annotation(self, annotation_id: str) -> MapAnnotation:
        target_id = annotation_id.strip()
        if not target_id:
            raise ValueError("annotation_id is invalid.")
        with self._lock:
            annotations = list(self._load_annotations())
            for index, annotation in enumerate(annotations):
                if annotation.annotation_id == target_id:
                    removed = annotations.pop(index)
                    self._write_annotations(tuple(annotations))
                    return removed
        raise KeyError(target_id)

    def _load_annotations(self) -> tuple[MapAnnotation, ...]:
        if not self._path.exists():
            return ()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Map annotation store root is invalid.")
        version = payload.get("version")
        if version != _ANNOTATION_FILE_VERSION:
            raise ValueError(f"Unsupported map annotation store version: {version!r}")
        raw_annotations = payload.get("annotations", [])
        if isinstance(raw_annotations, (str, bytes)) or not isinstance(raw_annotations, Sequence):
            raise ValueError("Map annotation store annotations are invalid.")
        annotations = tuple(
            MapAnnotation.from_mapping(cast(Mapping[str, object], raw_annotation))
            if isinstance(raw_annotation, Mapping)
            else _raise_invalid_annotation()
            for raw_annotation in cast(Sequence[object], raw_annotations)
        )
        return tuple(sorted(annotations, key=lambda annotation: (annotation.created_at_unix_ms, annotation.annotation_id)))

    def _write_annotations(self, annotations: tuple[MapAnnotation, ...]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _ANNOTATION_FILE_VERSION,
            "annotations": [annotation.to_mapping() for annotation in annotations],
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._path.parent,
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(self._path)


__all__: tuple[str, ...] = (
    "AppMapAnnotationStore",
    "MapAnnotation",
    "MapAnnotationDraft",
    "MapAnnotationList",
    "MapAnnotationMutationResult",
    "MapAnnotationShape",
    "MapCoordinate",
    "MapManifest",
    "MapWorldSummary",
)
