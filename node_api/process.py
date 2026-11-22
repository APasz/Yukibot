"""Process telemetry shared by node and browser diagnostics APIs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeAppProcessSummary(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )

    state: Literal["running", "stopped", "unavailable"]
    captured_at_ms: int = Field(ge=0)
    pid: int | None = Field(default=None, gt=0)
    started_at_ms: int | None = Field(default=None, ge=0)
    process_count: int | None = Field(default=None, ge=1)
    cpu_percent: float | None = Field(default=None, ge=0)
    memory_rss_bytes: int | None = Field(default=None, ge=0)
    thread_count: int | None = Field(default=None, ge=0)
    read_bytes_per_second: float | None = Field(default=None, ge=0)
    write_bytes_per_second: float | None = Field(default=None, ge=0)
