from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import config
from relay_notices import (
    MaintenanceNotice,
    MaintenanceStage,
    RelayNoticeSeverity,
    RelayNoticeSource,
    render_system_notice_text,
)
from restart_targets import RestartTarget, coalesce_restart_targets

log = logging.getLogger(__name__)
_SCHEDULE_DISABLED_VALUES = frozenset({"", "off", "disabled", "none"})
_SCHEDULE_TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")
_SYSTEM_WARNING_TARGETS = frozenset({RestartTarget.BOT, RestartTarget.SYSTEM})


@dataclass(frozen=True, slots=True)
class ScheduledRestartWarning:
    effective_target: RestartTarget
    matched_targets: tuple[RestartTarget, ...]
    scheduled_for: datetime
    lead_minutes: int


class MaintenanceService:
    _BOT_CONFIGURATION_PATH = Path("configuration.json")

    def __init__(self) -> None:
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH
        self._restart_schedules: dict[RestartTarget, config.PersistedRestartSchedule] = {}
        self._restart_warning = config.PersistedRestartWarning()
        self._warning_slots_sent: set[tuple[RestartTarget, str, int]] = set()
        self.reload()

    @property
    def restart_schedules(self) -> Mapping[RestartTarget, config.PersistedRestartSchedule]:
        return self._restart_schedules

    @property
    def restart_warning(self) -> config.PersistedRestartWarning:
        return self._restart_warning

    @property
    def restart_warning_lead_minutes(self) -> int:
        return self._restart_warning.lead_minutes

    def schedule_for(self, target: RestartTarget) -> config.PersistedRestartSchedule:
        return self._restart_schedules.get(target, config.PersistedRestartSchedule())

    def reload(self) -> bool:
        try:
            bot_config = config.load_bot_configuration(self._bot_configuration_path)
        except (OSError, ValueError) as xcp:
            log.warning(
                "Maintenance config read failed path=%s: %s: %s",
                self._bot_configuration_path,
                type(xcp).__name__,
                xcp,
            )
            return False

        self._restart_schedules = {target: bot_config.maintenance.schedule_for(target) for target in RestartTarget}
        self._restart_warning = bot_config.maintenance.restart_warning
        return True

    def update_restart_schedules(
        self,
        schedules: Mapping[RestartTarget, tuple[int, int] | None],
    ) -> dict[RestartTarget, config.PersistedRestartSchedule]:
        bot_config = self._load_bot_configuration()
        next_schedules = dict(bot_config.maintenance.restart_schedules)
        for target, schedule_time in schedules.items():
            current = next_schedules.get(target, config.PersistedRestartSchedule())
            if schedule_time is None:
                next_schedules[target] = current.model_copy(update={"enabled": False})
                continue
            hour, minute = schedule_time
            next_schedules[target] = current.model_copy(
                update={
                    "enabled": True,
                    "hour": hour,
                    "minute": minute,
                }
            )
        bot_config.maintenance = bot_config.maintenance.model_copy(update={"restart_schedules": next_schedules})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        self.reload()
        return dict(self._restart_schedules)

    def update_restart_warning_minutes(self, lead_minutes: int) -> int:
        warning = config.PersistedRestartWarning(lead_minutes=lead_minutes)
        bot_config = self._load_bot_configuration()
        bot_config.maintenance = bot_config.maintenance.model_copy(update={"restart_warning": warning})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        self.reload()
        return self.restart_warning_lead_minutes

    def due_restart_targets(
        self,
        *,
        now: datetime | None = None,
        available_targets: Sequence[RestartTarget],
    ) -> tuple[RestartTarget, ...]:
        local_now = (now or datetime.now().astimezone()).astimezone()
        return self._scheduled_targets_for_slot(local_now, available_targets=available_targets)

    def due_restart_target(
        self,
        *,
        now: datetime | None = None,
        available_targets: Sequence[RestartTarget],
    ) -> RestartTarget | None:
        return coalesce_restart_targets(self.due_restart_targets(now=now, available_targets=available_targets))

    def mark_triggered(
        self,
        targets: Iterable[RestartTarget],
        *,
        triggered_at: datetime | None = None,
    ) -> None:
        target_list = list(targets)
        if not target_list:
            return

        at = (triggered_at or datetime.now().astimezone()).astimezone()
        bot_config = self._load_bot_configuration()
        next_schedules = dict(bot_config.maintenance.restart_schedules)
        for target in target_list:
            current = next_schedules.get(target, config.PersistedRestartSchedule())
            next_schedules[target] = current.model_copy(update={"last_triggered_at": at})
        bot_config.maintenance = bot_config.maintenance.model_copy(update={"restart_schedules": next_schedules})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        self.reload()

    def due_restart_warnings(
        self,
        *,
        now: datetime | None = None,
        available_targets: Sequence[RestartTarget],
    ) -> tuple[ScheduledRestartWarning, ...]:
        local_now = (now or datetime.now().astimezone()).astimezone()
        warnings: list[ScheduledRestartWarning] = []
        for lead_minutes in self._warning_offsets_minutes():
            scheduled_for = (local_now + timedelta(minutes=lead_minutes)).replace(second=0, microsecond=0)
            matched_targets = self._scheduled_targets_for_slot(
                scheduled_for,
                available_targets=available_targets,
            )
            if not matched_targets:
                continue
            effective_target = coalesce_restart_targets(matched_targets)
            if effective_target is None or effective_target not in _SYSTEM_WARNING_TARGETS:
                continue
            warning_key = (effective_target, scheduled_for.isoformat(), lead_minutes)
            if warning_key in self._warning_slots_sent:
                continue
            self._warning_slots_sent.add(warning_key)
            warnings.append(
                ScheduledRestartWarning(
                    effective_target=effective_target,
                    matched_targets=matched_targets,
                    scheduled_for=scheduled_for,
                    lead_minutes=lead_minutes,
                )
            )
        return tuple(warnings)

    @staticmethod
    def parse_schedule_text(value: str) -> tuple[int, int] | None:
        text = value.strip().lower()
        if text in _SCHEDULE_DISABLED_VALUES:
            return None

        match = _SCHEDULE_TIME_RE.fullmatch(text)
        if match is None:
            raise ValueError("Use HH:MM in 24-hour time or 'off'.")

        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        config.PersistedRestartSchedule(enabled=True, hour=hour, minute=minute)
        return hour, minute

    @staticmethod
    def format_schedule_text(schedule: config.PersistedRestartSchedule) -> str:
        if not schedule.enabled:
            return "off"
        return f"{schedule.hour:02d}:{schedule.minute:02d}"

    @staticmethod
    def format_schedule_input(schedule: config.PersistedRestartSchedule) -> str:
        if not schedule.enabled:
            return ""
        return f"{schedule.hour:02d}:{schedule.minute:02d}"

    @staticmethod
    def parse_warning_minutes_text(value: str) -> int:
        text = value.strip()
        if not text:
            raise ValueError("Use a whole number of minutes from 5 to 180, or 0 to disable.")
        try:
            minutes = int(text)
        except ValueError as xcp:
            raise ValueError("Use a whole number of minutes from 5 to 180, or 0 to disable.") from xcp
        try:
            return config.PersistedRestartWarning(lead_minutes=minutes).lead_minutes
        except ValueError as xcp:
            raise ValueError("Use a whole number of minutes from 5 to 180, or 0 to disable.") from xcp

    @staticmethod
    def format_warning_minutes_input(lead_minutes: int) -> str:
        return str(lead_minutes)

    @staticmethod
    def format_warning_minutes_display(lead_minutes: int) -> str:
        if lead_minutes == 0:
            return "disabled"
        return f"{lead_minutes}m"

    @classmethod
    def build_restart_warning_notice(cls, warning: ScheduledRestartWarning) -> MaintenanceNotice:
        return MaintenanceNotice(
            stage=MaintenanceStage.WARNING,
            target=warning.effective_target,
            source=RelayNoticeSource.BOT,
            severity=RelayNoticeSeverity.WARNING,
            matched_targets=warning.matched_targets,
            lead_minutes=warning.lead_minutes,
            scheduled_time_text=cls._format_scheduled_time_text(warning.scheduled_for),
        )

    @classmethod
    def build_restart_executing_notice(
        cls,
        *,
        effective_target: RestartTarget,
        matched_targets: Sequence[RestartTarget],
        scheduled_for: datetime,
    ) -> MaintenanceNotice:
        return MaintenanceNotice(
            stage=MaintenanceStage.EXECUTING,
            target=effective_target,
            source=RelayNoticeSource.BOT,
            severity=RelayNoticeSeverity.WARNING,
            matched_targets=tuple(matched_targets),
            scheduled_time_text=cls._format_scheduled_time_text(scheduled_for),
        )

    @classmethod
    def build_restart_completed_notice(
        cls,
        *,
        effective_target: RestartTarget,
        matched_targets: Sequence[RestartTarget],
        scheduled_for: datetime,
        summary_lines: Sequence[str] = (),
    ) -> MaintenanceNotice:
        return MaintenanceNotice(
            stage=MaintenanceStage.COMPLETED,
            target=effective_target,
            source=RelayNoticeSource.BOT,
            severity=RelayNoticeSeverity.INFO,
            matched_targets=tuple(matched_targets),
            scheduled_time_text=cls._format_scheduled_time_text(scheduled_for),
            summary_lines=tuple(summary_lines),
        )

    @classmethod
    def format_restart_warning_notice(cls, warning: ScheduledRestartWarning) -> str:
        return render_system_notice_text(cls.build_restart_warning_notice(warning))

    @staticmethod
    def _was_triggered_for_slot(
        schedule: config.PersistedRestartSchedule,
        *,
        now_local: datetime,
    ) -> bool:
        if schedule.last_triggered_at is None:
            return False
        last_local = schedule.last_triggered_at.astimezone(now_local.tzinfo)
        return (
            last_local.date() == now_local.date()
            and last_local.hour == schedule.hour
            and last_local.minute == schedule.minute
        )

    def _load_bot_configuration(self) -> config.BotConfiguration:
        if not self._bot_configuration_path.exists():
            return config.BotConfiguration()
        return config.load_bot_configuration(self._bot_configuration_path)

    def _scheduled_targets_for_slot(
        self,
        slot_local: datetime,
        *,
        available_targets: Sequence[RestartTarget],
    ) -> tuple[RestartTarget, ...]:
        due_targets: list[RestartTarget] = []
        for target in available_targets:
            schedule = self.schedule_for(target)
            if not schedule.enabled:
                continue
            if schedule.hour != slot_local.hour or schedule.minute != slot_local.minute:
                continue
            if self._was_triggered_for_slot(schedule, now_local=slot_local):
                continue
            due_targets.append(target)
        return tuple(due_targets)

    def _warning_offsets_minutes(self) -> tuple[int, ...]:
        offsets = {1}
        if self.restart_warning_lead_minutes > 0:
            offsets.add(self.restart_warning_lead_minutes)
        return tuple(sorted(offsets, reverse=True))

    @staticmethod
    def _format_scheduled_time_text(scheduled_for: datetime) -> str:
        return scheduled_for.astimezone().strftime("%H:%M")
