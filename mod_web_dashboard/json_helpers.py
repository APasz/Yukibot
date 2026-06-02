from __future__ import annotations

from .runtime_imports import Iterable, Mapping, cast, json

type JsonObject = dict[str, object]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


def _json_object(value: object, *, context: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{context} must be a JSON object, got {type(value).__name__}.")
    payload: JsonObject = {}
    mapping = cast(Mapping[object, object], value)
    for key, entry_value in mapping.items():
        if not isinstance(key, str):
            raise RuntimeError(f"{context} must use string JSON object keys.")
        payload[key] = entry_value
    return payload


def _json_object_from_text(value: object, *, context: str) -> JsonObject:
    if not isinstance(value, str | bytes | bytearray):
        raise RuntimeError(f"{context} must be a text JSON payload.")
    try:
        payload: object = cast(object, json.loads(value))
    except ValueError as xcp:
        raise RuntimeError(f"{context} returned invalid JSON.") from xcp
    return _json_object(payload, context=context)


def _json_request_object(payload: Mapping[str, object] | None) -> dict[str, JsonValue]:
    if payload is None:
        return {}
    return {key: _json_value(value) for key, value in payload.items()}


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        result: dict[str, JsonValue] = {}
        for key, entry_value in mapping.items():
            if not isinstance(key, str):
                raise TypeError("JSON request object keys must be strings.")
            result[key] = _json_value(entry_value)
        return result
    if isinstance(value, list | tuple):
        return [_json_value(entry_value) for entry_value in cast(Iterable[object], value)]
    raise TypeError(f"Unsupported JSON request value type: {type(value).__name__}.")


__all__: tuple[str, ...] = (
    "JsonObject",
    "JsonValue",
    "_json_object",
    "_json_object_from_text",
    "_json_request_object",
    "_json_value",
)
