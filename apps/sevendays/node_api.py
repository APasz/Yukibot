from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from apps._node_api import JsonValue, optional_string, required_bool, required_string
from apps.sevendays import SevenDays, SevenDaysSandboxOptionsSnapshot


@dataclass(frozen=True, slots=True)
class NodeSevenDaysSandboxOptionsState:
    data_path: str
    file_exists: bool
    payload: dict[str, JsonValue] | None = None
    load_error: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSevenDaysSandboxOptionsState":
        raw_snapshot_payload: object | None = payload.get("payload")
        if raw_snapshot_payload is not None and not isinstance(raw_snapshot_payload, Mapping):
            raise ValueError("Node 7D2D sandbox options payload is invalid.")
        return cls(
            data_path=required_string(payload, "data_path"),
            file_exists=required_bool(payload, "file_exists"),
            payload=None
            if raw_snapshot_payload is None
            else dict[str, JsonValue](cast(Mapping[str, JsonValue], raw_snapshot_payload)),
            load_error=optional_string(payload, "load_error"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "data_path": self.data_path,
            "file_exists": self.file_exists,
            "payload": self.payload,
            "load_error": self.load_error,
        }


def build_sevendays_sandbox_options_state(app: SevenDays) -> NodeSevenDaysSandboxOptionsState:
    data_path = ".yukibot/sandbox_options.json"
    file_exists: bool = app.sandbox_options_file_exists
    if not file_exists:
        return NodeSevenDaysSandboxOptionsState(data_path=data_path, file_exists=False)
    try:
        snapshot: SevenDaysSandboxOptionsSnapshot = app.load_sandbox_options_snapshot()
        snapshot_payload: dict[str, JsonValue] | None = cast(dict[str, JsonValue], snapshot.to_mapping())
        load_error: str | None = None
    except Exception as xcp:
        snapshot_payload = None
        load_error = str(xcp) or type(xcp).__name__
    return NodeSevenDaysSandboxOptionsState(
        data_path=data_path,
        file_exists=file_exists,
        payload=snapshot_payload,
        load_error=load_error,
    )
