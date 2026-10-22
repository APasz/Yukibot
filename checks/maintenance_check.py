from __future__ import annotations

import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from maintenance import MaintenanceService, ScheduledRestartWarning
from relay_notices import MaintenanceStage, RelayNoticeSeverity, render_system_notice_text
from restart_targets import RestartTarget


def _timestamp(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


@contextmanager
def _restart_service(schedule: config.PersistedRestartSchedule) -> Iterator[MaintenanceService]:
    with TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "configuration.json"
        config.save_bot_configuration(
            config_path,
            config.BotConfiguration(
                maintenance=config.PersistedMaintenanceSettings(
                    restart_schedules={RestartTarget.BOT: schedule}
                )
            ),
        )
        with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
            yield MaintenanceService()


class MaintenanceServiceTests(unittest.TestCase):
    def test_due_restart_targets_skip_already_triggered_slot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(
                config_path,
                config.BotConfiguration(
                    maintenance=config.PersistedMaintenanceSettings(
                        restart_schedules={
                            RestartTarget.BOT: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=60,
                                anchor_timestamp=_timestamp("2026-05-27T03:30:00+10:00"),
                                last_triggered_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                            )
                        }
                    )
                ),
            )

            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                due = service.due_restart_targets(
                    now=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
                    available_targets=(RestartTarget.BOT,),
                )

        self.assertEqual(due, ())

    def test_due_restart_target_coalesces_to_broader_restart(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(
                config_path,
                config.BotConfiguration(
                    maintenance=config.PersistedMaintenanceSettings(
                        restart_schedules={
                            RestartTarget.BOT: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=60,
                                anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                            ),
                            RestartTarget.VOICE: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=60,
                                anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                            ),
                        }
                    )
                ),
            )

            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                due = service.due_restart_target(
                    now=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
                    available_targets=(RestartTarget.BOT, RestartTarget.VOICE),
                )

        self.assertIs(due, RestartTarget.BOT)

    def test_update_restart_intervals_persists_disabled_and_enabled_targets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_intervals(
                    {
                        RestartTarget.BOT: 195,
                        RestartTarget.SYSTEM: None,
                    },
                    anchor_timestamp=_timestamp("2026-05-27T03:15:00+10:00"),
                    saved_at_timestamp=_timestamp("2026-05-27T01:00:00+10:00"),
                )
                loaded = config.load_bot_configuration(config_path)

        bot_schedule = loaded.maintenance.schedule_for(RestartTarget.BOT)
        system_schedule = loaded.maintenance.schedule_for(RestartTarget.SYSTEM)
        self.assertTrue(bot_schedule.enabled)
        self.assertEqual(bot_schedule.interval_minutes, 195)
        self.assertEqual(bot_schedule.anchor_timestamp, _timestamp("2026-05-27T03:15:00+10:00"))
        self.assertFalse(system_schedule.enabled)

    def test_update_restart_warning_minutes_persists_configuration(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_warning_minutes(45)
                loaded = config.load_bot_configuration(config_path)

        self.assertEqual(loaded.maintenance.restart_warning.lead_minutes, 45)

    def test_saving_schedule_skips_restarts_within_one_hour(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_intervals(
                    {RestartTarget.BOT: 90},
                    anchor_timestamp=_timestamp("2026-05-27T10:30:00+10:00"),
                    saved_at_timestamp=_timestamp("2026-05-27T10:00:00+10:00"),
                )

                schedule = service.schedule_for(RestartTarget.BOT)
                self.assertEqual(
                    schedule.skipped_through_timestamp,
                    _timestamp("2026-05-27T10:30:00+10:00"),
                )
                self.assertEqual(
                    service.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-05-27T12:00:00+10:00"),
                )

    def test_saving_schedule_keeps_restart_beyond_one_hour(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_intervals(
                    {RestartTarget.BOT: 90},
                    anchor_timestamp=_timestamp("2026-05-27T11:01:00+10:00"),
                    saved_at_timestamp=_timestamp("2026-05-27T10:00:00+10:00"),
                )

                schedule = service.schedule_for(RestartTarget.BOT)
                self.assertIsNone(schedule.skipped_through_timestamp)
                self.assertEqual(
                    service.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-05-27T11:01:00+10:00"),
                )

    def test_saving_schedule_skips_restart_exactly_one_hour_away(self) -> None:
        with _restart_service(config.PersistedRestartSchedule()) as service:
            service.update_restart_intervals(
                {RestartTarget.BOT: 75},
                anchor_timestamp=_timestamp("2026-05-27T11:00:00+10:00"),
                saved_at_timestamp=_timestamp("2026-05-27T10:00:00+10:00"),
            )

            self.assertEqual(
                service.schedule_for(RestartTarget.BOT).skipped_through_timestamp,
                _timestamp("2026-05-27T11:00:00+10:00"),
            )
            self.assertEqual(
                service.next_restart_at(RestartTarget.BOT),
                datetime.fromisoformat("2026-05-27T12:15:00+10:00"),
            )

    def test_future_anchor_defines_interval_phase_without_delaying_shorter_schedule(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                monday_anchor = _timestamp("2026-07-06T04:08:00+00:00")
                service.update_restart_intervals(
                    {RestartTarget.BOT: 24 * 60},
                    anchor_timestamp=monday_anchor,
                    saved_at_timestamp=_timestamp("2026-07-04T12:00:00+00:00"),
                )

                schedule = service.schedule_for(RestartTarget.BOT)
                self.assertEqual(schedule.anchor_timestamp, monday_anchor)
                self.assertEqual(
                    service.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-07-05T04:08:00+00:00"),
                )

                service.mark_triggered(
                    (RestartTarget.BOT,),
                    triggered_at=datetime.fromisoformat("2026-07-05T04:08:45+00:00"),
                )
                self.assertEqual(
                    service.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-07-06T04:08:00+00:00"),
                )

    def test_recurring_interval_becomes_due_with_minute_precision(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_intervals(
                    {RestartTarget.BOT: 75},
                    anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                    saved_at_timestamp=_timestamp("2026-05-27T03:00:00+10:00"),
                )

                self.assertEqual(
                    service.due_restart_targets(
                        now=datetime.fromisoformat("2026-05-27T04:29:00+10:00"),
                        available_targets=(RestartTarget.BOT,),
                    ),
                    (),
                )
                self.assertEqual(
                    service.due_restart_targets(
                        now=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
                        available_targets=(RestartTarget.BOT,),
                    ),
                    (RestartTarget.BOT,),
                )
                service.mark_triggered(
                    (RestartTarget.BOT,),
                    triggered_at=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
                )
                self.assertEqual(
                    service.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-05-27T05:45:00+10:00"),
                )

    def test_late_restarts_keep_anchor_across_reloads(self) -> None:
        anchor = datetime.fromisoformat("2026-10-20T05:02:00+00:00")
        for interval_minutes in (60, 75, 24 * 60, 7 * 24 * 60):
            with (
                self.subTest(interval_minutes=interval_minutes),
                _restart_service(
                    config.PersistedRestartSchedule(
                        enabled=True,
                        interval_minutes=interval_minutes,
                        anchor_timestamp=int(anchor.timestamp()),
                    )
                ) as service,
            ):
                for occurrence, delay_seconds in enumerate((15, 46, 75)):
                    scheduled_for = anchor + timedelta(minutes=occurrence * interval_minutes)
                    triggered_at = scheduled_for + timedelta(seconds=delay_seconds)
                    self.assertEqual(service.next_restart_at(RestartTarget.BOT), scheduled_for)
                    self.assertEqual(
                        service.due_restart_targets(now=triggered_at, available_targets=(RestartTarget.BOT,)),
                        (RestartTarget.BOT,),
                    )
                    service.mark_triggered((RestartTarget.BOT,), triggered_at=triggered_at)
                    self.assertTrue(service.reload())
                    self.assertEqual(
                        service.schedule_for(RestartTarget.BOT).last_triggered_timestamp,
                        int(triggered_at.timestamp()),
                    )
                    self.assertEqual(
                        service.next_restart_at(RestartTarget.BOT),
                        scheduled_for + timedelta(minutes=interval_minutes),
                    )
                    self.assertEqual(
                        service.due_restart_targets(now=triggered_at, available_targets=(RestartTarget.BOT,)),
                        (),
                    )

    def test_existing_drifted_schedules_realign_to_anchor(self) -> None:
        for anchor, last_triggered, skipped_through, expected in (
            ("2026-07-30T05:02:00+00:00", "2026-10-21T05:25:46+00:00", None, "2026-10-22T05:02:00+00:00"),
            ("2026-08-03T05:05:00+00:00", "2026-10-21T05:24:22+00:00", None, "2026-10-22T05:05:00+00:00"),
            (
                "2026-07-30T05:02:00+00:00",
                "2026-10-21T05:25:46+00:00",
                "2026-10-23T05:25:46+00:00",
                "2026-10-24T05:02:00+00:00",
            ),
            (
                "2026-07-30T05:02:00+00:00",
                "2026-10-21T05:25:46+00:00",
                "2026-10-20T05:25:46+00:00",
                "2026-10-22T05:02:00+00:00",
            ),
            ("2026-07-30T05:02:00+00:00", None, "2026-10-23T05:25:46+00:00", "2026-10-24T05:02:00+00:00"),
        ):
            with (
                self.subTest(anchor=anchor, last_triggered=last_triggered, skipped_through=skipped_through),
                _restart_service(
                    config.PersistedRestartSchedule(
                        enabled=True,
                        interval_minutes=24 * 60,
                        anchor_timestamp=_timestamp(anchor),
                        last_triggered_timestamp=None if last_triggered is None else _timestamp(last_triggered),
                        skipped_through_timestamp=None if skipped_through is None else _timestamp(skipped_through),
                    )
                ) as service,
            ):
                self.assertEqual(service.next_restart_at(RestartTarget.BOT), datetime.fromisoformat(expected))

    def test_hourly_restarts_remain_distinct_across_repeated_dst_hour(self) -> None:
        with _restart_service(
            config.PersistedRestartSchedule(
                enabled=True,
                interval_minutes=60,
                anchor_timestamp=_timestamp("2026-04-05T01:30:00+11:00"),
            )
        ) as service:
            for occurrence in (
                "2026-04-05T01:30:00+11:00",
                "2026-04-05T02:30:00+11:00",
                "2026-04-05T02:30:00+10:00",
                "2026-04-05T03:30:00+10:00",
            ):
                with self.subTest(occurrence=occurrence):
                    scheduled_for = datetime.fromisoformat(occurrence)
                    self.assertEqual(service.next_restart_at(RestartTarget.BOT), scheduled_for)
                    self.assertEqual(
                        service.due_restart_targets(now=scheduled_for, available_targets=(RestartTarget.BOT,)),
                        (RestartTarget.BOT,),
                    )
                    triggered_at = scheduled_for + timedelta(seconds=30)
                    service.mark_triggered((RestartTarget.BOT,), triggered_at=triggered_at)
                    self.assertEqual(
                        service.due_restart_targets(now=triggered_at, available_targets=(RestartTarget.BOT,)),
                        (),
                    )

    def test_overdue_restart_runs_once_then_resumes_anchor(self) -> None:
        with _restart_service(
            config.PersistedRestartSchedule(
                enabled=True,
                interval_minutes=24 * 60,
                anchor_timestamp=_timestamp("2026-10-20T05:02:00+00:00"),
            )
        ) as service:
            recovered_at = datetime.fromisoformat("2026-10-23T06:10:00+00:00")
            self.assertEqual(
                service.due_restart_targets(now=recovered_at, available_targets=(RestartTarget.BOT,)),
                (RestartTarget.BOT,),
            )
            service.mark_triggered((RestartTarget.BOT,), triggered_at=recovered_at)
            self.assertTrue(service.reload())
            next_restart = datetime.fromisoformat("2026-10-24T05:02:00+00:00")
            self.assertEqual(service.next_restart_at(RestartTarget.BOT), next_restart)
            for now in (recovered_at, next_restart - timedelta(seconds=1)):
                self.assertEqual(
                    service.due_restart_targets(now=now, available_targets=(RestartTarget.BOT,)),
                    (),
                )
            self.assertEqual(
                service.due_restart_targets(now=next_restart, available_targets=(RestartTarget.BOT,)),
                (RestartTarget.BOT,),
            )

    def test_skips_after_late_restart_keep_anchor(self) -> None:
        with _restart_service(
            config.PersistedRestartSchedule(
                enabled=True,
                interval_minutes=24 * 60,
                anchor_timestamp=_timestamp("2026-10-20T05:02:00+00:00"),
                last_triggered_timestamp=_timestamp("2026-10-20T05:02:45+00:00"),
            )
        ) as service:
            for skipped_at in ("2026-10-21T05:02:00+00:00", "2026-10-22T05:02:00+00:00"):
                skipped = service.skip_next_restart(RestartTarget.BOT)
                self.assertEqual(skipped.skipped_through_timestamp, _timestamp(skipped_at))
                self.assertTrue(service.reload())
            self.assertEqual(
                service.next_restart_at(RestartTarget.BOT),
                datetime.fromisoformat("2026-10-23T05:02:00+00:00"),
            )
            service.mark_triggered(
                (RestartTarget.BOT,),
                triggered_at=datetime.fromisoformat("2026-10-23T05:02:59+00:00"),
            )
            self.assertIsNone(service.schedule_for(RestartTarget.BOT).skipped_through_timestamp)
            self.assertEqual(
                service.next_restart_at(RestartTarget.BOT),
                datetime.fromisoformat("2026-10-24T05:02:00+00:00"),
            )

    def test_warnings_use_anchor_after_late_restart(self) -> None:
        with _restart_service(
            config.PersistedRestartSchedule(
                enabled=True,
                interval_minutes=24 * 60,
                anchor_timestamp=_timestamp("2026-10-20T05:02:00+00:00"),
                last_triggered_timestamp=_timestamp("2026-10-20T05:25:46+00:00"),
            )
        ) as service:
            warnings = service.due_restart_warnings(
                now=datetime.fromisoformat("2026-10-21T05:01:30+00:00"),
                available_targets=(RestartTarget.BOT,),
            )
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0].lead_minutes, 1)
            self.assertEqual(warnings[0].scheduled_for, datetime.fromisoformat("2026-10-21T05:02:00+00:00"))

    def test_skip_next_restart_persists_and_supports_repeated_skips(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_intervals(
                    {RestartTarget.BOT: 75},
                    anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                    saved_at_timestamp=_timestamp("2026-05-27T03:00:00+10:00"),
                )

                first_skip = service.skip_next_restart(RestartTarget.BOT)
                self.assertEqual(
                    first_skip.skipped_through_timestamp,
                    _timestamp("2026-05-27T04:30:00+10:00"),
                )
                self.assertEqual(
                    service.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-05-27T05:45:00+10:00"),
                )

                second_skip = service.skip_next_restart(RestartTarget.BOT)
                self.assertEqual(
                    second_skip.skipped_through_timestamp,
                    _timestamp("2026-05-27T05:45:00+10:00"),
                )
                reloaded = MaintenanceService()
                self.assertEqual(
                    reloaded.next_restart_at(RestartTarget.BOT),
                    datetime.fromisoformat("2026-05-27T07:00:00+10:00"),
                )

                reloaded.mark_triggered(
                    (RestartTarget.BOT,),
                    triggered_at=datetime.fromisoformat("2026-05-27T07:00:00+10:00"),
                )
                self.assertIsNone(reloaded.schedule_for(RestartTarget.BOT).skipped_through_timestamp)

    def test_skip_next_restart_rejects_disabled_schedule(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                with self.assertRaisesRegex(ValueError, "is not enabled"):
                    service.skip_next_restart(RestartTarget.SYSTEM)

    def test_parse_schedule_text_accepts_off_and_minute_interval(self) -> None:
        self.assertIsNone(MaintenanceService.parse_schedule_text("off"))
        self.assertEqual(MaintenanceService.parse_schedule_text("90"), 90)
        with self.assertRaisesRegex(ValueError, "whole minutes"):
            MaintenanceService.parse_schedule_text("4.30")
        with self.assertRaisesRegex(ValueError, "between 60 and 10080"):
            MaintenanceService.parse_schedule_text("59")

    def test_parse_warning_minutes_text_accepts_zero_and_valid_range(self) -> None:
        self.assertEqual(MaintenanceService.parse_warning_minutes_text("0"), 0)
        self.assertEqual(MaintenanceService.parse_warning_minutes_text("15"), 15)
        with self.assertRaisesRegex(ValueError, "0 to disable"):
            MaintenanceService.parse_warning_minutes_text("4")

    def test_build_restart_warning_notice_matches_legacy_text(self) -> None:
        warning = ScheduledRestartWarning(
            effective_target=RestartTarget.SYSTEM,
            matched_targets=(RestartTarget.SYSTEM, RestartTarget.BOT),
            scheduled_for=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
            lead_minutes=15,
        )

        notice = MaintenanceService.build_restart_warning_notice(warning)

        self.assertEqual(notice.stage, MaintenanceStage.WARNING)
        self.assertEqual(notice.severity, RelayNoticeSeverity.WARNING)
        self.assertEqual(render_system_notice_text(notice), "Scheduled maintenance: restart in 15m.")
        self.assertEqual(MaintenanceService.format_restart_warning_notice(warning), "Scheduled maintenance: restart in 15m.")

    def test_build_restart_completed_notice_renders_targets_and_summary(self) -> None:
        notice = MaintenanceService.build_restart_completed_notice(
            effective_target=RestartTarget.VOICE,
            matched_targets=(RestartTarget.VOICE,),
            scheduled_for=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
            summary_lines=("Reloaded voice runtime.", "Reconnected playback."),
        )

        self.assertEqual(notice.stage, MaintenanceStage.COMPLETED)
        self.assertEqual(
            render_system_notice_text(notice),
            (
                "Scheduled maintenance completed: `voice` at `04:30`.\n"
                "Matched targets: `voice`\n"
                "Reloaded voice runtime.\n"
                "Reconnected playback."
            ),
        )

    def test_build_restart_completed_notice_without_summary_lines_renders_plain_notice(self) -> None:
        notice = MaintenanceService.build_restart_completed_notice(
            effective_target=RestartTarget.VOICE,
            matched_targets=(RestartTarget.VOICE,),
            scheduled_for=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
        )

        self.assertEqual(notice.stage, MaintenanceStage.COMPLETED)
        self.assertEqual(
            render_system_notice_text(notice),
            "Scheduled maintenance completed: `voice` at `04:30`.\nMatched targets: `voice`",
        )

    def test_due_restart_warnings_emit_configured_warning_then_one_minute_warning_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(
                config_path,
                config.BotConfiguration(
                    maintenance=config.PersistedMaintenanceSettings(
                        restart_schedules={
                            RestartTarget.SYSTEM: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=60,
                                anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                            ),
                            RestartTarget.VOICE: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=60,
                                anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                            ),
                        },
                        restart_warning=config.PersistedRestartWarning(lead_minutes=15),
                    )
                ),
            )

            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                fifteen_minute_warning = service.due_restart_warnings(
                    now=datetime.fromisoformat("2026-05-27T04:15:00+10:00"),
                    available_targets=(RestartTarget.SYSTEM, RestartTarget.VOICE),
                )
                duplicate_warning = service.due_restart_warnings(
                    now=datetime.fromisoformat("2026-05-27T04:15:00+10:00"),
                    available_targets=(RestartTarget.SYSTEM, RestartTarget.VOICE),
                )
                one_minute_warning = service.due_restart_warnings(
                    now=datetime.fromisoformat("2026-05-27T04:29:00+10:00"),
                    available_targets=(RestartTarget.SYSTEM, RestartTarget.VOICE),
                )

        self.assertEqual(len(fifteen_minute_warning), 1)
        self.assertEqual(fifteen_minute_warning[0].effective_target, RestartTarget.SYSTEM)
        self.assertEqual(fifteen_minute_warning[0].matched_targets, (RestartTarget.SYSTEM, RestartTarget.VOICE))
        self.assertEqual(fifteen_minute_warning[0].lead_minutes, 15)
        self.assertEqual(duplicate_warning, ())
        self.assertEqual(len(one_minute_warning), 1)
        self.assertEqual(one_minute_warning[0].lead_minutes, 1)

    def test_due_restart_warnings_ignore_voice_only_restarts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(
                config_path,
                config.BotConfiguration(
                    maintenance=config.PersistedMaintenanceSettings(
                        restart_schedules={
                            RestartTarget.VOICE: config.PersistedRestartSchedule(
                                enabled=True,
                                interval_minutes=60,
                                anchor_timestamp=_timestamp("2026-05-27T04:30:00+10:00"),
                            ),
                        }
                    )
                ),
            )

            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                warnings = service.due_restart_warnings(
                    now=datetime.fromisoformat("2026-05-27T04:15:00+10:00"),
                    available_targets=(RestartTarget.VOICE,),
                )

        self.assertEqual(warnings, ())


if __name__ == "__main__":
    unittest.main()
