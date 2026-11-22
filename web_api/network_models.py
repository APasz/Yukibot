"""Transport contracts shared by the diagnostics API and generated TypeScript."""

from __future__ import annotations

from enum import IntEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from node_api.process import NodeAppProcessSummary

type Percent = Annotated[int, Field(ge=0, le=100)]
type NetworkNodeId = Annotated[str, Field(min_length=1, max_length=128)]
type NetworkAppName = Annotated[str, Field(min_length=1, max_length=256)]


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


class NetworkBandwidthSize(IntEnum):
    MIB_32 = 32 * 1024**2
    MIB_64 = 64 * 1024**2
    MIB_128 = 128 * 1024**2
    MIB_256 = 256 * 1024**2
    MIB_512 = 512 * 1024**2
    GIB_1 = 1024**3


class NetworkBandwidthDuration(IntEnum):
    SECONDS_5 = 5000
    SECONDS_10 = 10000
    SECONDS_20 = 20000


DEFAULT_BANDWIDTH_SIZE: Final = NetworkBandwidthSize.MIB_32
DEFAULT_BANDWIDTH_DURATION: Final = NetworkBandwidthDuration.SECONDS_5
MAX_BANDWIDTH_BYTES: Final = max(NetworkBandwidthSize).value
MAX_BANDWIDTH_REQUEST_BYTES: Final = 2 * 1024**2


class NetworkBandwidthLimits(NetworkModel):
    idle_duration_ms: int = Field(gt=0)
    transfer_duration_ms: int = Field(gt=0)
    probe_interval_ms: int = Field(gt=0)
    max_bytes_per_direction: int = Field(gt=0)
    max_request_bytes: int = Field(gt=0, le=MAX_BANDWIDTH_REQUEST_BYTES)
    parallel_requests: int = Field(gt=0, le=4)
    max_tests_per_recording: int = Field(gt=0, le=10)

    @model_validator(mode="after")
    def validate_request_budget(self) -> Self:
        if self.max_request_bytes > self.max_bytes_per_direction:
            raise ValueError("A transfer cannot exceed the test's data allowance.")
        return self


BANDWIDTH_LIMITS = NetworkBandwidthLimits(
    idle_duration_ms=3000,
    transfer_duration_ms=DEFAULT_BANDWIDTH_DURATION.value,
    probe_interval_ms=500,
    max_bytes_per_direction=DEFAULT_BANDWIDTH_SIZE.value,
    max_request_bytes=MAX_BANDWIDTH_REQUEST_BYTES,
    parallel_requests=4,
    max_tests_per_recording=5,
)


class NetworkBandwidthGrant(NetworkModel):
    token: str = Field(min_length=32, max_length=128)
    limits: NetworkBandwidthLimits


class NetworkBandwidthEndpoint(NetworkModel):
    target_id: NetworkNodeId
    url: str = Field(min_length=1, max_length=2048)
    grant: NetworkBandwidthGrant


class NetworkBandwidthSelection(NetworkModel):
    size_bytes: NetworkBandwidthSize = Field(
        default=DEFAULT_BANDWIDTH_SIZE, strict=False
    )
    duration_ms: NetworkBandwidthDuration = Field(
        default=DEFAULT_BANDWIDTH_DURATION, strict=False
    )


class NetworkBandwidthSessionRequest(NetworkBandwidthSelection):
    origin: str = Field(min_length=1, max_length=2048)


class NetworkBandwidthReceipt(NetworkModel):
    received_bytes: int = Field(ge=0, le=BANDWIDTH_LIMITS.max_request_bytes)


type NetworkBandwidthPhaseName = Literal["idle", "download", "upload"]


class NetworkBandwidthPhase(NetworkModel):
    phase: NetworkBandwidthPhaseName
    offset_ms: int = Field(ge=0, le=NETWORK_LIMITS.max_duration_ms)
    end_ms: int = Field(ge=0, le=NETWORK_LIMITS.max_duration_ms)
    transferred_bytes: int = Field(ge=0, le=MAX_BANDWIDTH_BYTES)
    ended_by: Literal["duration", "byte_limit", "cancelled", "error"]

    @model_validator(mode="after")
    def validate_phase(self) -> Self:
        if self.end_ms < self.offset_ms:
            raise ValueError("A bandwidth phase cannot end before it starts.")
        if self.phase == "idle" and self.transferred_bytes:
            raise ValueError("An idle phase cannot transfer test data.")
        return self


class NetworkTarget(NetworkModel):
    id: NetworkNodeId
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
    bandwidth_limits: NetworkBandwidthLimits = BANDWIDTH_LIMITS
    bandwidth_sizes: tuple[NetworkBandwidthSize, ...] = Field(
        default=tuple(NetworkBandwidthSize), min_length=1
    )
    bandwidth_durations: tuple[NetworkBandwidthDuration, ...] = Field(
        default=tuple(NetworkBandwidthDuration), min_length=1
    )


class NetworkMonitoredApp(NetworkModel):
    target_id: NetworkNodeId
    node_label: str = Field(min_length=1, max_length=128)
    app_name: NetworkAppName
    label: str = Field(min_length=1, max_length=256)


class NetworkAppList(NetworkModel):
    apps: list[NetworkMonitoredApp]
    unavailable_nodes: list[str]


class NetworkAppContext(NetworkModel):
    app: NetworkMonitoredApp
    process: NodeAppProcessSummary
    player_count: int | None = Field(default=None, ge=0)
    player_capacity: int | None = Field(default=None, ge=0)


class ProbeBase(NetworkModel):
    target_id: NetworkNodeId
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


class NetworkBandwidthTest(NetworkModel):
    target: NetworkTarget
    max_bytes_per_direction: int = Field(
        default=DEFAULT_BANDWIDTH_SIZE.value, gt=0, le=MAX_BANDWIDTH_BYTES
    )
    transfer_duration_ms: int = Field(
        default=DEFAULT_BANDWIDTH_DURATION.value,
        gt=0,
        le=max(NetworkBandwidthDuration).value,
    )
    started_at_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0, le=NETWORK_LIMITS.max_duration_ms)
    probes: list[NetworkProbe] = Field(max_length=100)
    phases: list[NetworkBandwidthPhase] = Field(min_length=1, max_length=3)
    state: Literal["completed", "cancelled", "failed"]
    error: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_phases(self) -> Self:
        expected = ("idle", "download", "upload")
        previous_end = 0
        for index, phase in enumerate(self.phases):
            if phase.transferred_bytes > self.max_bytes_per_direction:
                raise ValueError(
                    "Bandwidth phases cannot exceed the selected data cap."
                )
            if phase.phase != expected[index] or phase.offset_ms < previous_end:
                raise ValueError("Bandwidth phases must be ordered and not overlap.")
            if index < len(self.phases) - 1 and phase.ended_by not in {
                "duration",
                "byte_limit",
            }:
                raise ValueError("Only the last bandwidth phase can be interrupted.")
            previous_end = phase.end_ms
        if self.state == "completed" and (
            len(self.phases) != len(expected)
            or any(
                phase.ended_by not in {"duration", "byte_limit"}
                for phase in self.phases
            )
        ):
            raise ValueError("A completed bandwidth test must finish all phases.")
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("Only failed bandwidth tests must include an error.")
        last_reason = self.phases[-1].ended_by
        if (self.state == "failed") != (last_reason == "error") or (
            (self.state == "cancelled") != (last_reason == "cancelled")
        ):
            raise ValueError("The final phase must match the bandwidth test state.")
        if self.phases[-1].end_ms > self.duration_ms:
            raise ValueError("Bandwidth phases must fall within the test.")
        for probe in self.probes:
            if probe.target_id != self.target.id or probe.offset_ms > self.duration_ms:
                raise ValueError("Bandwidth probes must belong to this test.")
        return self


class NetworkRecordedBandwidthTest(NetworkModel):
    offset_ms: int = Field(ge=0, le=NETWORK_LIMITS.max_duration_ms)
    test: NetworkBandwidthTest


class NetworkNodeContext(NetworkModel):
    target_id: NetworkNodeId
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
    app: NetworkAppContext | None = None


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
    monitored_app: NetworkMonitoredApp | None = None
    bandwidth_tests: list[NetworkRecordedBandwidthTest] = Field(
        default_factory=list, max_length=BANDWIDTH_LIMITS.max_tests_per_recording
    )

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
        for item in self.contexts:
            app = None if item.snapshot is None else item.snapshot.app
            if app is not None and (
                self.monitored_app is None
                or app.app.target_id != self.monitored_app.target_id
                or app.app.app_name != self.monitored_app.app_name
            ):
                raise ValueError(
                    "Process observations must belong to the monitored app."
                )
        for test in self.bandwidth_tests:
            if test.offset_ms + test.test.duration_ms > self.duration_ms:
                raise ValueError("Bandwidth tests must fall within the recording.")
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
