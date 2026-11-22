"""Transport contracts shared by the diagnostics API and generated TypeScript."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

type Percent = Annotated[int, Field(ge=0, le=100)]


class NetworkModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class NetworkLimits(NetworkModel):
    interval_ms: int
    timeout_ms: int
    context_interval_ms: int
    max_duration_ms: int
    max_targets: int
    retention_days: int


NETWORK_LIMITS = NetworkLimits(
    interval_ms=2000,
    timeout_ms=5000,
    context_interval_ms=10000,
    max_duration_ms=1800000,
    max_targets=16,
    retention_days=7,
)
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_REPORTS_PER_USER = 20
MAX_STORED_REPORTS = 200


class NetworkTarget(NetworkModel):
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    kind: Literal["portal", "node"]


class NetworkIdentity(NetworkModel):
    user_id: str
    display_name: str
    is_admin: bool


class NetworkConfiguration(NetworkModel):
    identity: NetworkIdentity
    targets: list[NetworkTarget] = Field(max_length=NETWORK_LIMITS.max_targets)
    limits: NetworkLimits


class ProbeBase(NetworkModel):
    target_id: str = Field(min_length=1, max_length=128)
    offset_ms: int = Field(
        ge=0, le=NETWORK_LIMITS.max_duration_ms + NETWORK_LIMITS.timeout_ms
    )
    hidden: bool
    browser_delay_ms: int = Field(ge=0, le=86400000)


class SuccessfulProbe(ProbeBase):
    status: Literal["ok"]
    rtt_ms: float = Field(ge=0, le=NETWORK_LIMITS.timeout_ms)


class FailedProbe(ProbeBase):
    status: Literal["timeout", "network_error", "http_error"]
    http_status: int | None = Field(default=None, ge=100, le=599)

    @model_validator(mode="after")
    def validate_http_status(self) -> Self:
        if (self.status == "http_error") != (self.http_status is not None):
            raise ValueError("Only HTTP errors must include an HTTP status.")
        if self.http_status is not None and self.http_status < 300:
            raise ValueError(
                "An HTTP error cannot contain a successful or informational status."
            )
        return self


class DelayedProbe(ProbeBase):
    status: Literal["browser_delayed"]


type NetworkProbe = Annotated[
    SuccessfulProbe | FailedProbe | DelayedProbe, Field(discriminator="status")
]


class NetworkNodeContext(NetworkModel):
    target_id: str = Field(min_length=1, max_length=128)
    availability: Literal["online", "connecting", "offline"]
    captured_at_ms: int | None = Field(default=None, ge=0)
    cpu_percent: Percent | None = None
    cpu_per_core_percent: list[Percent] = Field(default_factory=list, max_length=4096)
    ram_percent: Percent | None = None
    portal_latency_ms: int | None = Field(default=None, ge=0)
    portal_probe_captured_at_ms: int | None = Field(default=None, ge=0)


class NetworkContext(NetworkModel):
    observed_at_ms: int = Field(ge=0)
    nodes: list[NetworkNodeContext] = Field(max_length=NETWORK_LIMITS.max_targets)


class RecordedContext(NetworkModel):
    offset_ms: int = Field(
        ge=0, le=NETWORK_LIMITS.max_duration_ms + NETWORK_LIMITS.timeout_ms
    )
    snapshot: NetworkContext | None
    error: Literal["unavailable"] | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if (self.snapshot is None) != (self.error is not None):
            raise ValueError(
                "A context sample must contain either a snapshot or an error."
            )
        return self


class NetworkMarker(NetworkModel):
    offset_ms: int = Field(ge=0, le=NETWORK_LIMITS.max_duration_ms)
    label: str = Field(min_length=1, max_length=200)


class NetworkReportInput(NetworkModel):
    schema_version: Literal[1] = 1
    id: UUID = Field(strict=False)
    started_at_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0, le=NETWORK_LIMITS.max_duration_ms)
    title: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)
    targets: list[NetworkTarget] = Field(
        min_length=1, max_length=NETWORK_LIMITS.max_targets
    )
    probes: list[NetworkProbe] = Field(max_length=15000)
    contexts: list[RecordedContext] = Field(max_length=1000)
    markers: list[NetworkMarker] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        target_ids = {target.id for target in self.targets}
        if len(target_ids) != len(self.targets):
            raise ValueError("Probe target identifiers must be unique.")
        for probe in self.probes:
            if probe.target_id not in target_ids:
                raise ValueError("A probe refers to an unknown target.")
            if probe.offset_ms > self.duration_ms:
                raise ValueError("Probe timestamps must fall within the recording.")
        for item in [*self.contexts, *self.markers]:
            if item.offset_ms > self.duration_ms:
                raise ValueError("Event timestamps must fall within the recording.")
        return self


class NetworkReportSummary(NetworkModel):
    id: UUID
    owner_id: str
    owner_name: str
    saved_at_ms: int
    started_at_ms: int
    duration_ms: int
    title: str
    probe_count: int


class NetworkReport(NetworkModel):
    summary: NetworkReportSummary
    recording: NetworkReportInput


class NetworkReportList(NetworkModel):
    reports: list[NetworkReportSummary]
