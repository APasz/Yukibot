from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from restart_targets import RestartTarget


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid.")
    return value


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid.")
    return value


class RelayNoticeType(StrEnum):
    APP_LIFECYCLE = "app_lifecycle"
    PLAYER_SESSION = "player_session"
    GAME_DEATH = "game_death"
    GAME_PROGRESS = "game_progress"
    GAME_EVENT = "game_event"
    MAINTENANCE = "maintenance"
    BOT_LIFECYCLE = "bot_lifecycle"


class RelayNoticeSource(StrEnum):
    APP_LOG = "app_log"
    APP_MANAGER = "app_manager"
    APP_POLL = "app_poll"
    BOT = "bot"
    DISCORD = "discord"
    WEB = "web"


class RelayNoticeSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AppLifecycleState(StrEnum):
    STARTED = "started"
    STOPPED = "stopped"
    CRASHED = "crashed"


class PlayerSessionAction(StrEnum):
    JOINED = "joined"
    LEFT = "left"


class GameDeathKind(StrEnum):
    PVE = "pve"
    PVP = "pvp"
    UNKNOWN = "unknown"


class GameProgressKind(StrEnum):
    ADVANCEMENT = "advancement"
    GOAL = "goal"
    CHALLENGE = "challenge"
    ACHIEVEMENT = "achievement"
    RESEARCH = "research"
    GENERIC = "generic"


class MaintenanceStage(StrEnum):
    WARNING = "warning"
    EXECUTING = "executing"
    COMPLETED = "completed"


class BotLifecycleStage(StrEnum):
    STARTED = "started"
    STOPPING = "stopping"
    ERROR = "error"


type RelayNoticeBadgeTone = Literal["black", "purple", "red", "warn", "grey"]


@dataclass(frozen=True, slots=True)
class RelayNoticeBadgeSpec:
    text: str
    tone: RelayNoticeBadgeTone

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Relay notice badge text must not be empty.")
        if not self.tone.strip():
            raise ValueError("Relay notice badge tone must not be empty.")


@dataclass(frozen=True, slots=True)
class RelayNoticeEmbedSpec:
    title: str
    description: str

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Relay notice embed title must not be empty.")
        if not self.description.strip():
            raise ValueError("Relay notice embed description must not be empty.")


@dataclass(frozen=True, slots=True)
class AppLifecycleNotice:
    state: AppLifecycleState
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO
    join_address: str | None = None
    detail_lines: tuple[str, ...] = ()
    uptime_seconds: int | None = None
    summary: str | None = None

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.APP_LIFECYCLE

    def __post_init__(self) -> None:
        if self.join_address is not None and not self.join_address.strip():
            raise ValueError("App lifecycle join address must not be blank.")
        if self.uptime_seconds is not None and self.uptime_seconds < 0:
            raise ValueError("App lifecycle uptime must not be negative.")
        if self.summary is not None and not self.summary.strip():
            raise ValueError("App lifecycle summary must not be blank.")
        normalised_lines = tuple(line.strip() for line in self.detail_lines if line.strip())
        object.__setattr__(self, "detail_lines", normalised_lines)

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "state": self.state.value,
            "source": self.source.value,
            "severity": self.severity.value,
            "join_address": self.join_address,
            "detail_lines": list(self.detail_lines),
            "uptime_seconds": self.uptime_seconds,
            "summary": self.summary,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "AppLifecycleNotice":
        raw_state = _required_string(payload, "state")
        raw_source = _required_string(payload, "source")
        raw_severity = _required_string(payload, "severity")
        raw_detail_lines = payload.get("detail_lines", ())
        if not isinstance(raw_detail_lines, list):
            raise ValueError("detail_lines are invalid.")
        detail_lines: list[str] = []
        for raw_line in raw_detail_lines:
            if not isinstance(raw_line, str):
                raise ValueError("detail_lines are invalid.")
            detail_lines.append(raw_line)
        raw_uptime_seconds = payload.get("uptime_seconds")
        if raw_uptime_seconds is not None and (
            isinstance(raw_uptime_seconds, bool) or not isinstance(raw_uptime_seconds, int)
        ):
            raise ValueError("uptime_seconds is invalid.")
        try:
            return cls(
                state=AppLifecycleState(raw_state),
                source=RelayNoticeSource(raw_source),
                severity=RelayNoticeSeverity(raw_severity),
                join_address=_optional_string(payload, "join_address"),
                detail_lines=tuple(detail_lines),
                uptime_seconds=raw_uptime_seconds,
                summary=_optional_string(payload, "summary"),
            )
        except ValueError as xcp:
            raise ValueError("app lifecycle notice is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class PlayerSessionNotice:
    action: PlayerSessionAction
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO
    pack_version: str | None = None
    has_unpublished_pack_changes: bool = False

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.PLAYER_SESSION

    def __post_init__(self) -> None:
        if self.pack_version is not None and not self.pack_version.strip():
            raise ValueError("Player session pack version must not be blank.")
        if self.has_unpublished_pack_changes and self.pack_version is None:
            raise ValueError("Player session unpublished pack changes require a pack version.")

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "action": self.action.value,
            "source": self.source.value,
            "severity": self.severity.value,
            "pack_version": self.pack_version,
            "has_unpublished_pack_changes": self.has_unpublished_pack_changes,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "PlayerSessionNotice":
        raw_has_unpublished_pack_changes = payload.get("has_unpublished_pack_changes", False)
        if not isinstance(raw_has_unpublished_pack_changes, bool):
            raise ValueError("has_unpublished_pack_changes is invalid.")
        try:
            return cls(
                action=PlayerSessionAction(_required_string(payload, "action")),
                source=RelayNoticeSource(_required_string(payload, "source")),
                severity=RelayNoticeSeverity(_required_string(payload, "severity")),
                pack_version=_optional_string(payload, "pack_version"),
                has_unpublished_pack_changes=raw_has_unpublished_pack_changes,
            )
        except ValueError as xcp:
            raise ValueError("player session notice is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class GameDeathNotice:
    death_kind: GameDeathKind
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO
    detail_text: str | None = None

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.GAME_DEATH

    def __post_init__(self) -> None:
        if self.detail_text is not None and not self.detail_text.strip():
            raise ValueError("Game death detail text must not be blank.")

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "death_kind": self.death_kind.value,
            "source": self.source.value,
            "severity": self.severity.value,
            "detail_text": self.detail_text,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GameDeathNotice":
        try:
            return cls(
                death_kind=GameDeathKind(_required_string(payload, "death_kind")),
                source=RelayNoticeSource(_required_string(payload, "source")),
                severity=RelayNoticeSeverity(_required_string(payload, "severity")),
                detail_text=_optional_string(payload, "detail_text"),
            )
        except ValueError as xcp:
            raise ValueError("game death notice is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class GameProgressNotice:
    progress_kind: GameProgressKind
    label: str
    title: str
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.GAME_PROGRESS

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("Game progress label must not be empty.")
        if not self.title.strip():
            raise ValueError("Game progress title must not be empty.")

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "progress_kind": self.progress_kind.value,
            "label": self.label,
            "title": self.title,
            "source": self.source.value,
            "severity": self.severity.value,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GameProgressNotice":
        try:
            return cls(
                progress_kind=GameProgressKind(_required_string(payload, "progress_kind")),
                label=_required_string(payload, "label"),
                title=_required_string(payload, "title"),
                source=RelayNoticeSource(_required_string(payload, "source")),
                severity=RelayNoticeSeverity(_required_string(payload, "severity")),
            )
        except ValueError as xcp:
            raise ValueError("game progress notice is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class GameEventNotice:
    label: str
    detail: str | None
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.GAME_EVENT

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("Game event label must not be empty.")
        if self.detail is not None and not self.detail.strip():
            raise ValueError("Game event detail must not be blank.")

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "label": self.label,
            "detail": self.detail,
            "source": self.source.value,
            "severity": self.severity.value,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GameEventNotice":
        try:
            return cls(
                label=_required_string(payload, "label"),
                detail=_optional_string(payload, "detail"),
                source=RelayNoticeSource(_required_string(payload, "source")),
                severity=RelayNoticeSeverity(_required_string(payload, "severity")),
            )
        except ValueError as xcp:
            raise ValueError("game event notice is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class MaintenanceNotice:
    stage: MaintenanceStage
    target: RestartTarget
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO
    matched_targets: tuple[RestartTarget, ...] = ()
    lead_minutes: int | None = None
    scheduled_time_text: str | None = None
    summary_lines: tuple[str, ...] = ()

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.MAINTENANCE

    def __post_init__(self) -> None:
        if self.lead_minutes is not None and self.lead_minutes < 0:
            raise ValueError("Maintenance notice lead minutes must not be negative.")
        if self.scheduled_time_text is not None and not self.scheduled_time_text.strip():
            raise ValueError("Maintenance notice scheduled time must not be blank.")
        normalised_lines = tuple(line.strip() for line in self.summary_lines if line.strip())
        object.__setattr__(self, "summary_lines", normalised_lines)

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "stage": self.stage.value,
            "target": self.target.value,
            "source": self.source.value,
            "severity": self.severity.value,
            "matched_targets": [target.value for target in self.matched_targets],
            "lead_minutes": self.lead_minutes,
            "scheduled_time_text": self.scheduled_time_text,
            "summary_lines": list(self.summary_lines),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MaintenanceNotice":
        raw_matched_targets = payload.get("matched_targets", ())
        if not isinstance(raw_matched_targets, list):
            raise ValueError("matched_targets are invalid.")
        matched_targets: list[RestartTarget] = []
        for raw_target in raw_matched_targets:
            if not isinstance(raw_target, str):
                raise ValueError("matched_targets are invalid.")
            matched_targets.append(RestartTarget(raw_target))
        raw_lead_minutes = payload.get("lead_minutes")
        if raw_lead_minutes is not None and (
            isinstance(raw_lead_minutes, bool) or not isinstance(raw_lead_minutes, int)
        ):
            raise ValueError("lead_minutes is invalid.")
        raw_summary_lines = payload.get("summary_lines", ())
        if not isinstance(raw_summary_lines, list):
            raise ValueError("summary_lines are invalid.")
        summary_lines: list[str] = []
        for raw_line in raw_summary_lines:
            if not isinstance(raw_line, str):
                raise ValueError("summary_lines are invalid.")
            summary_lines.append(raw_line)
        try:
            return cls(
                stage=MaintenanceStage(_required_string(payload, "stage")),
                target=RestartTarget(_required_string(payload, "target")),
                source=RelayNoticeSource(_required_string(payload, "source")),
                severity=RelayNoticeSeverity(_required_string(payload, "severity")),
                matched_targets=tuple(matched_targets),
                lead_minutes=raw_lead_minutes,
                scheduled_time_text=_optional_string(payload, "scheduled_time_text"),
                summary_lines=tuple(summary_lines),
            )
        except ValueError as xcp:
            raise ValueError("maintenance notice is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class BotLifecycleNotice:
    stage: BotLifecycleStage
    source: RelayNoticeSource
    severity: RelayNoticeSeverity = RelayNoticeSeverity.INFO
    debug_mode: bool = False
    auto_launch_app_names: tuple[str, ...] = ()
    startup_disabled_lines: tuple[str, ...] = ()
    error_lines: tuple[str, ...] = ()
    uptime_seconds: int | None = None
    summary: str | None = None

    @property
    def notice_type(self) -> RelayNoticeType:
        return RelayNoticeType.BOT_LIFECYCLE

    def __post_init__(self) -> None:
        normalised_auto_launch_app_names: list[str] = []
        seen_auto_launch_app_names: set[str] = set()
        for raw_app_name in self.auto_launch_app_names:
            app_name = raw_app_name.strip()
            if not app_name:
                raise ValueError("Bot lifecycle auto-launch app names must not be blank.")
            if app_name in seen_auto_launch_app_names:
                continue
            seen_auto_launch_app_names.add(app_name)
            normalised_auto_launch_app_names.append(app_name)
        object.__setattr__(self, "auto_launch_app_names", tuple(normalised_auto_launch_app_names))
        if self.uptime_seconds is not None and self.uptime_seconds < 0:
            raise ValueError("Bot lifecycle uptime must not be negative.")
        if self.summary is not None and not self.summary.strip():
            raise ValueError("Bot lifecycle summary must not be blank.")
        normalised_disabled_lines = tuple(line.strip() for line in self.startup_disabled_lines if line.strip())
        object.__setattr__(self, "startup_disabled_lines", normalised_disabled_lines)
        normalised_error_lines = tuple(line.strip() for line in self.error_lines if line.strip())
        object.__setattr__(self, "error_lines", normalised_error_lines)

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": self.notice_type.value,
            "stage": self.stage.value,
            "source": self.source.value,
            "severity": self.severity.value,
            "debug_mode": self.debug_mode,
            "auto_launch_app_names": list(self.auto_launch_app_names),
            "startup_disabled_lines": list(self.startup_disabled_lines),
            "error_lines": list(self.error_lines),
            "uptime_seconds": self.uptime_seconds,
            "summary": self.summary,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "BotLifecycleNotice":
        raw_debug_mode = payload.get("debug_mode", False)
        if not isinstance(raw_debug_mode, bool):
            raise ValueError("debug_mode is invalid.")
        raw_uptime_seconds = payload.get("uptime_seconds")
        if raw_uptime_seconds is not None and (
            isinstance(raw_uptime_seconds, bool) or not isinstance(raw_uptime_seconds, int)
        ):
            raise ValueError("uptime_seconds is invalid.")
        raw_startup_disabled_lines = payload.get("startup_disabled_lines", ())
        if not isinstance(raw_startup_disabled_lines, list):
            raise ValueError("startup_disabled_lines are invalid.")
        startup_disabled_lines: list[str] = []
        for raw_line in raw_startup_disabled_lines:
            if not isinstance(raw_line, str):
                raise ValueError("startup_disabled_lines are invalid.")
            startup_disabled_lines.append(raw_line)
        raw_error_lines = payload.get("error_lines", ())
        if not isinstance(raw_error_lines, list):
            raise ValueError("error_lines are invalid.")
        error_lines: list[str] = []
        for raw_line in raw_error_lines:
            if not isinstance(raw_line, str):
                raise ValueError("error_lines are invalid.")
            error_lines.append(raw_line)
        raw_auto_launch_app_names = payload.get("auto_launch_app_names")
        if raw_auto_launch_app_names is None:
            legacy_auto_launch_app_name = _optional_string(payload, "auto_launch_app_name")
            auto_launch_app_names = () if legacy_auto_launch_app_name is None else (legacy_auto_launch_app_name,)
        else:
            if not isinstance(raw_auto_launch_app_names, list):
                raise ValueError("auto_launch_app_names are invalid.")
            auto_launch_app_names = tuple(
                _required_string({"name": raw_name}, "name") for raw_name in raw_auto_launch_app_names
            )
        try:
            return cls(
                stage=BotLifecycleStage(_required_string(payload, "stage")),
                source=RelayNoticeSource(_required_string(payload, "source")),
                severity=RelayNoticeSeverity(_required_string(payload, "severity")),
                debug_mode=raw_debug_mode,
                auto_launch_app_names=auto_launch_app_names,
                startup_disabled_lines=tuple(startup_disabled_lines),
                error_lines=tuple(error_lines),
                uptime_seconds=raw_uptime_seconds,
                summary=_optional_string(payload, "summary"),
            )
        except ValueError as xcp:
            raise ValueError("bot lifecycle notice is invalid.") from xcp


type RelayNotice = (
    AppLifecycleNotice
    | PlayerSessionNotice
    | GameDeathNotice
    | GameProgressNotice
    | GameEventNotice
    | MaintenanceNotice
    | BotLifecycleNotice
)


def relay_notice_to_mapping(notice: RelayNotice) -> dict[str, object]:
    return notice.to_mapping()


def relay_notice_from_mapping(payload: Mapping[str, object]) -> RelayNotice:
    raw_type = _required_string(payload, "type")
    try:
        notice_type = RelayNoticeType(raw_type)
    except ValueError as xcp:
        raise ValueError("relay notice type is invalid.") from xcp
    if notice_type is RelayNoticeType.APP_LIFECYCLE:
        return AppLifecycleNotice.from_mapping(payload)
    if notice_type is RelayNoticeType.PLAYER_SESSION:
        return PlayerSessionNotice.from_mapping(payload)
    if notice_type is RelayNoticeType.GAME_DEATH:
        return GameDeathNotice.from_mapping(payload)
    if notice_type is RelayNoticeType.GAME_PROGRESS:
        return GameProgressNotice.from_mapping(payload)
    if notice_type is RelayNoticeType.GAME_EVENT:
        return GameEventNotice.from_mapping(payload)
    if notice_type is RelayNoticeType.MAINTENANCE:
        return MaintenanceNotice.from_mapping(payload)
    if notice_type is RelayNoticeType.BOT_LIFECYCLE:
        return BotLifecycleNotice.from_mapping(payload)
    raise AssertionError(f"Unhandled relay notice type: {notice_type}")


def _format_duration_seconds(total_seconds: int) -> str:
    clamped_seconds = max(0, total_seconds)
    hours, remainder = divmod(clamped_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    components: list[str] = []
    if hours:
        components.append(f"{hours}h")
    if minutes:
        components.append(f"{minutes}m")
    if seconds or not components:
        components.append(f"{seconds}s")
    return " ".join(components)


def _sentence_case_fragment(text: str) -> str:
    stripped_text = text.strip()
    if not stripped_text:
        raise ValueError("Notice fragment must not be blank.")
    first_character = stripped_text[0]
    return f"{first_character.upper()}{stripped_text[1:]}"


def _render_game_death_embed_description(notice: GameDeathNotice) -> str:
    if notice.detail_text is not None:
        return _sentence_case_fragment(notice.detail_text)
    if notice.death_kind is GameDeathKind.PVP:
        return "Killed by another player"
    return "Died"


def render_pack_text(*, pack_version: str | None, has_unpublished_changes: bool) -> str | None:
    if pack_version is None:
        return None
    pack_text = f"Pack: {pack_version}"
    if has_unpublished_changes:
        return f"{pack_text} [Unpublished Changes]"
    return pack_text


def render_notice_body(notice: RelayNotice, *, app_name: str) -> str:
    if isinstance(notice, PlayerSessionNotice):
        if notice.action is PlayerSessionAction.JOINED:
            return f"joined {app_name}"
        return f"left {app_name}"
    if isinstance(notice, GameDeathNotice):
        if notice.detail_text is not None:
            return notice.detail_text
        if notice.death_kind is GameDeathKind.PVE:
            return "died"
        if notice.death_kind is GameDeathKind.PVP:
            return "killed by another player"
        return "died"
    if isinstance(notice, GameProgressNotice):
        return f"{notice.label}: {notice.title}"
    if isinstance(notice, GameEventNotice):
        if notice.detail is not None:
            return f"{notice.label}: {notice.detail}"
        return notice.label
    if isinstance(notice, MaintenanceNotice):
        if notice.stage is MaintenanceStage.WARNING:
            if notice.lead_minutes is None:
                return "Scheduled maintenance warning."
            return f"Scheduled maintenance: restart in {_format_minutes_short(notice.lead_minutes)}."
        if notice.stage is MaintenanceStage.EXECUTING:
            schedule_value = notice.scheduled_time_text or "unknown time"
            return f"Scheduled maintenance restarting `{notice.target.value}` at `{schedule_value}`."
        schedule_value = notice.scheduled_time_text or "unknown time"
        return f"Scheduled maintenance completed: `{notice.target.value}` at `{schedule_value}`."
    if isinstance(notice, BotLifecycleNotice):
        if notice.stage is BotLifecycleStage.STARTED:
            return "Started: DEBUG" if notice.debug_mode else "Started"
        if notice.stage is BotLifecycleStage.STOPPING:
            if notice.uptime_seconds is not None:
                return f"Shutting Down; uptime: {_format_duration_seconds(notice.uptime_seconds)}"
            return "Shutting Down"
        if notice.summary is not None:
            return f"Error: {notice.summary}"
        return "Error"
    if isinstance(notice, AppLifecycleNotice):
        if notice.state is AppLifecycleState.STARTED:
            return f"{app_name} started"
        if notice.state is AppLifecycleState.STOPPED:
            if notice.uptime_seconds is not None:
                return f"{app_name} stopped after {_format_duration_seconds(notice.uptime_seconds)}"
            return f"{app_name} stopped"
        if notice.summary is not None:
            return f"{app_name} crashed: {notice.summary}"
        if notice.uptime_seconds is not None:
            return f"{app_name} crashed after {_format_duration_seconds(notice.uptime_seconds)}"
        return f"{app_name} crashed"
    raise AssertionError(f"Unhandled relay notice: {type(notice)!r}")


def render_notice_text(notice: RelayNotice, *, author_name: str, app_name: str) -> str:
    body = render_notice_body(notice, app_name=app_name)
    if isinstance(notice, PlayerSessionNotice | GameDeathNotice):
        return f"{author_name} {body}"
    if isinstance(notice, GameProgressNotice):
        if author_name.strip() and author_name.casefold() != "system":
            return f"{author_name}: {body}"
        return body
    return body


def notice_hides_body_content(notice: RelayNotice) -> bool:
    return isinstance(notice, PlayerSessionNotice)


def notice_badge_spec(notice: RelayNotice) -> RelayNoticeBadgeSpec | None:
    if isinstance(notice, PlayerSessionNotice):
        if notice.action is PlayerSessionAction.JOINED:
            return RelayNoticeBadgeSpec(text="Joined", tone="purple")
        return RelayNoticeBadgeSpec(text="Left", tone="grey")
    if isinstance(notice, GameDeathNotice):
        if notice.death_kind is GameDeathKind.PVP:
            return RelayNoticeBadgeSpec(text="PVP Kill", tone="red")
        return RelayNoticeBadgeSpec(text="Death", tone="red")
    if isinstance(notice, GameProgressNotice):
        return RelayNoticeBadgeSpec(text=notice.label, tone="black")
    if isinstance(notice, AppLifecycleNotice):
        if notice.state is AppLifecycleState.STARTED:
            return RelayNoticeBadgeSpec(text="Started", tone="purple")
        if notice.state is AppLifecycleState.STOPPED:
            return RelayNoticeBadgeSpec(text="Ended", tone="grey")
        return RelayNoticeBadgeSpec(text="Crashed", tone="red")
    if isinstance(notice, MaintenanceNotice):
        if notice.stage is MaintenanceStage.WARNING:
            return RelayNoticeBadgeSpec(text="Maintenance", tone="warn")
        if notice.stage is MaintenanceStage.EXECUTING:
            return RelayNoticeBadgeSpec(text="Maintenance", tone="red")
        return RelayNoticeBadgeSpec(text="Maintenance", tone="black")
    if isinstance(notice, BotLifecycleNotice):
        if notice.stage is BotLifecycleStage.STARTED:
            return RelayNoticeBadgeSpec(text="Started", tone="purple")
        if notice.stage is BotLifecycleStage.STOPPING:
            return RelayNoticeBadgeSpec(text="Stopping", tone="grey")
        return RelayNoticeBadgeSpec(text="Error", tone="red")
    if isinstance(notice, GameEventNotice):
        tone = "warn"
        if notice.severity is RelayNoticeSeverity.ERROR:
            tone = "red"
        elif notice.severity is RelayNoticeSeverity.INFO:
            tone = "black"
        return RelayNoticeBadgeSpec(text=notice.label, tone=tone)
    return None


def notice_additional_badge_specs(notice: RelayNotice) -> tuple[RelayNoticeBadgeSpec, ...]:
    del notice
    return ()


def relay_notice_badge_spec_from_label(label: str) -> RelayNoticeBadgeSpec | None:
    text = label.strip()
    if not text:
        return None
    lower = text.casefold()
    if "started" in lower:
        return RelayNoticeBadgeSpec(text=text, tone="purple")
    if "stopped" in lower or "ended" in lower:
        return RelayNoticeBadgeSpec(text=text, tone="grey")
    if "crashed" in lower:
        return RelayNoticeBadgeSpec(text=text, tone="red")
    if "joined" in lower:
        return RelayNoticeBadgeSpec(text=text, tone="purple")
    if "left" in lower:
        return RelayNoticeBadgeSpec(text=text, tone="grey")
    if any(token in lower for token in ("death", "died", "killed")):
        return RelayNoticeBadgeSpec(text=text, tone="red")
    if any(token in lower for token in ("advancement", "challenge", "goal", "research")):
        return RelayNoticeBadgeSpec(text=text, tone="black")
    return RelayNoticeBadgeSpec(text=text, tone="warn")


def notice_embed_spec(notice: RelayNotice, *, app_name: str, author_name: str) -> RelayNoticeEmbedSpec | None:
    if isinstance(notice, PlayerSessionNotice):
        if notice.action is PlayerSessionAction.JOINED:
            return RelayNoticeEmbedSpec(title=app_name, description=f"Joined {author_name}")
        return RelayNoticeEmbedSpec(title=app_name, description=f"Left {author_name}")
    if isinstance(notice, GameDeathNotice):
        return RelayNoticeEmbedSpec(title=app_name, description=_render_game_death_embed_description(notice))
    if isinstance(notice, GameProgressNotice):
        return RelayNoticeEmbedSpec(title=notice.label, description=notice.title)
    if isinstance(notice, GameEventNotice):
        if notice.detail is not None:
            return RelayNoticeEmbedSpec(title=notice.label, description=notice.detail)
        return None
    if isinstance(notice, MaintenanceNotice):
        return RelayNoticeEmbedSpec(title="Maintenance", description=render_notice_body(notice, app_name=app_name))
    if isinstance(notice, BotLifecycleNotice):
        return RelayNoticeEmbedSpec(title="Bot", description=render_system_notice_text(notice))
    if isinstance(notice, AppLifecycleNotice):
        description_lines: list[str] = []
        if notice.state is AppLifecycleState.STARTED:
            if notice.join_address is not None:
                description_lines.append(f"Join: `{notice.join_address}`")
            description_lines.extend(notice.detail_lines)
            if not description_lines:
                description_lines.append("Started.")
            return RelayNoticeEmbedSpec(title=f"{app_name} Started", description="\n".join(description_lines))
        if notice.state is AppLifecycleState.STOPPED:
            if notice.uptime_seconds is not None:
                description_lines.append(f"Uptime: `{_format_duration_seconds(notice.uptime_seconds)}`")
            description_lines.extend(notice.detail_lines)
            if not description_lines:
                description_lines.append("Stopped.")
            return RelayNoticeEmbedSpec(title=f"{app_name} Ended", description="\n".join(description_lines))
        if notice.summary is not None:
            description_lines.append(notice.summary)
        if notice.uptime_seconds is not None:
            description_lines.append(f"Uptime: `{_format_duration_seconds(notice.uptime_seconds)}`")
        description_lines.extend(notice.detail_lines)
        if not description_lines:
            description_lines.append("Crashed.")
        return RelayNoticeEmbedSpec(title=f"{app_name} Crashed", description="\n".join(description_lines))
    return None


def render_system_notice_lines(notice: RelayNotice) -> tuple[str, ...]:
    if isinstance(notice, BotLifecycleNotice):
        if notice.stage is BotLifecycleStage.STARTED:
            lines = ["Started: DEBUG" if notice.debug_mode else "Started"]
            for auto_launch_app_name in notice.auto_launch_app_names:
                lines.append(f"\tAuto-Launch Scheduled: {auto_launch_app_name}")
            lines.extend(notice.startup_disabled_lines)
            lines.extend(notice.error_lines)
            return tuple(lines)
        if notice.stage is BotLifecycleStage.STOPPING:
            return (render_notice_body(notice, app_name=""),)
        return (render_notice_body(notice, app_name=""),)
    if isinstance(notice, MaintenanceNotice):
        lines = [render_notice_body(notice, app_name="")]
        if notice.stage is MaintenanceStage.COMPLETED and notice.matched_targets:
            due_names = ", ".join(target.value for target in notice.matched_targets)
            lines.append(f"Matched targets: `{due_names}`")
        lines.extend(notice.summary_lines)
        return tuple(lines)
    return (render_notice_body(notice, app_name=""),)


def render_system_notice_text(notice: RelayNotice) -> str:
    return "\n".join(render_system_notice_lines(notice))


def _format_minutes_short(minutes: int) -> str:
    return f"{minutes}m"
