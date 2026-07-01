from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from maintenance import MaintenanceService, ScheduledRestartWarning
from relay_notices import MaintenanceStage, RelayNoticeSeverity, render_system_notice_text
from restart_targets import RestartTarget


def _timestamp(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


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
                    service.next_restart_at(
                        RestartTarget.BOT,
                        now=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
                    ),
                    datetime.fromisoformat("2026-05-27T05:45:00+10:00"),
                )

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
