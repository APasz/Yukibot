from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from _security import Access_Control, Power_Level

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class NodeModUploadSource:
    source_path: Path
    upload_name: str


def required_string(payload: Mapping[str, object], key: str) -> str:
    value: object | None = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid.")
    return value


def required_text(payload: Mapping[str, object], key: str) -> str:
    value: object | None = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is invalid.")
    return value


def optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value: object | None = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid.")
    return value


def string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value: object = payload.get(key, ())
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{key} is invalid.")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} is invalid.")
        items.append(item)
    return tuple[str, ...](items)


def power_level(payload: Mapping[str, object], key: str, *, default: Power_Level) -> Power_Level:
    value: object = payload.get(key, default.name)
    if isinstance(value, bool):
        raise ValueError(f"{key} is invalid.")
    if isinstance(value, (str, int)):
        parsed: Power_Level | None = Access_Control.parse_level(value)
        if parsed is not None:
            return parsed
        raise ValueError(f"{key} is invalid.")
    raise ValueError(f"{key} is invalid.")


def required_bool(payload: Mapping[str, object], key: str) -> bool:
    value: object | None = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} is invalid.")
    return value


def required_int(payload: Mapping[str, object], key: str) -> int:
    value: object | None = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} is invalid.")
    return value


def optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value: object | None = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} is invalid.")
    return value
