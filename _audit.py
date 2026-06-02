from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TypeAlias, cast

import config

_log = logging.getLogger(config.LOGGER_AUDIT)
_tenor_log = logging.getLogger(config.LOGGER_TENOR)

AuditPayloadValue: TypeAlias = (
    str | int | float | bool | None | list["AuditPayloadValue"] | dict[str, "AuditPayloadValue"]
)


def _normalise_value(value: object) -> AuditPayloadValue:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        mapping_value = cast(Mapping[object, object], value)
        return {str(key): _normalise_value(item) for key, item in mapping_value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        iterable_value = cast(tuple[object, ...] | list[object] | set[object] | frozenset[object], value)
        return [_normalise_value(item) for item in iterable_value]
    return str(value)


def audit_log(event: str, /, **fields: object) -> None:
    payload: dict[str, AuditPayloadValue] = {"event": event}
    payload.update({key: _normalise_value(value) for key, value in fields.items()})
    _log.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def tenor_log(event: str, /, **fields: object) -> None:
    payload: dict[str, AuditPayloadValue] = {"event": event}
    payload.update({key: _normalise_value(value) for key, value in fields.items()})
    _tenor_log.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
