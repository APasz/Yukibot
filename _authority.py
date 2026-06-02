from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias, cast

import requests

log = logging.getLogger(__name__)
JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    mapping = cast(Mapping[object, object], value)
    result: dict[str, object] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{label} keys must be strings")
        result[key] = item
    return result


class AuthorityResource(StrEnum):
    BOTS = "bots"
    USERS = "users"
    NAMES = "names"


class NameMutationKind(StrEnum):
    ADD_NAME = "add_name"
    CLEAN_NAMES = "clean_names"
    REMOVE_GAME_ALIAS = "remove_game_alias"
    REMOVE_NAME = "remove_name"
    SET_DISPLAY_OVERRIDE = "set_display_override"
    SET_GAME_ALIAS = "set_game_alias"
    SET_GAME_UUID = "set_game_uuid"
    SET_NAMES = "set_names"
    SET_PLATFORM_ID = "set_platform_id"
    UPSERT_MANUAL_USER = "upsert_manual_user"


@dataclass(frozen=True, slots=True)
class AuthorityClient:
    base_url: str
    token: str
    timeout: float

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get_json(self, path: str) -> dict[str, object]:
        response = requests.get(self._url(path), headers=self._headers, timeout=self.timeout)
        response.raise_for_status()
        return _json_object(cast(object, response.json()), label="Authority response")

    def post_json(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        response = requests.post(
            self._url(path),
            json=cast(Mapping[str, JsonValue], dict(payload)),
            headers=self._headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return _json_object(cast(object, response.json()), label="Authority response")


def read_json_object(path: Path) -> dict[str, object]:
    return _json_object(cast(object, json.loads(path.read_text("utf-8"))), label=str(path))


def write_json_object(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=4), "utf-8")


def response_data(payload: Mapping[str, object]) -> dict[str, object]:
    data = payload.get("data", dict(payload))
    return _json_object(data, label="Authority payload data")


def append_pending(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, sort_keys=True))
        file.write("\n")


def read_pending(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []

    pending: list[dict[str, object]] = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = cast(object, json.loads(line))
        except json.JSONDecodeError:
            log.exception(f"Skipping invalid pending authority payload in {path}")
            continue
        try:
            pending.append(_json_object(payload, label=f"pending authority payload in {path}"))
        except TypeError:
            log.warning(f"Skipping non-object pending authority payload in {path}: {type(payload).__name__}")
    return pending
