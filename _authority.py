from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)


class AuthorityResource(StrEnum):
    USERS = "users"
    NAMES = "names"


class NameMutationKind(StrEnum):
    ADD_NAME = "add_name"
    CLEAN_NAMES = "clean_names"
    REMOVE_GAME_ALIAS = "remove_game_alias"
    REMOVE_NAME = "remove_name"
    SET_GAME_ALIAS = "set_game_alias"
    SET_GAME_UUID = "set_game_uuid"
    SET_NAMES = "set_names"
    SET_PLATFORM_ID = "set_platform_id"


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

    def get_json(self, path: str) -> dict[str, Any]:
        response = requests.get(self._url(path), headers=self._headers, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(f"Authority response must be a JSON object, got {type(payload).__name__}")
        return payload

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(self._url(path), json=payload, headers=self._headers, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError(f"Authority response must be a JSON object, got {type(data).__name__}")
        return data


def read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return payload


def write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=4), "utf-8")


def response_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise TypeError("Authority payload data must be a JSON object")
    return data


def append_pending(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, sort_keys=True))
        file.write("\n")


def read_pending(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    pending: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            log.exception(f"Skipping invalid pending authority payload in {path}")
            continue
        if isinstance(payload, dict):
            pending.append(payload)
        else:
            log.warning(f"Skipping non-object pending authority payload in {path}: {type(payload).__name__}")
    return pending
