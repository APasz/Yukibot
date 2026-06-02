from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from maintenance import MaintenanceService
from restart_targets import RestartTarget


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
                                hour=4,
                                minute=30,
                                last_triggered_at=datetime.fromisoformat("2026-05-27T04:30:00+10:00"),
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
                            RestartTarget.BOT: config.PersistedRestartSchedule(enabled=True, hour=4, minute=30),
                            RestartTarget.VOICE: config.PersistedRestartSchedule(enabled=True, hour=4, minute=30),
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

    def test_update_restart_schedules_persists_disabled_and_enabled_targets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_schedules(
                    {
                        RestartTarget.BOT: (3, 15),
                        RestartTarget.SYSTEM: None,
                    }
                )
                loaded = config.load_bot_configuration(config_path)

        bot_schedule = loaded.maintenance.schedule_for(RestartTarget.BOT)
        system_schedule = loaded.maintenance.schedule_for(RestartTarget.SYSTEM)
        self.assertTrue(bot_schedule.enabled)
        self.assertEqual((bot_schedule.hour, bot_schedule.minute), (3, 15))
        self.assertFalse(system_schedule.enabled)

    def test_update_restart_warning_minutes_persists_configuration(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                service = MaintenanceService()
                service.update_restart_warning_minutes(45)
                loaded = config.load_bot_configuration(config_path)

        self.assertEqual(loaded.maintenance.restart_warning.lead_minutes, 45)

    def test_parse_schedule_text_accepts_off_and_hh_mm(self) -> None:
        self.assertIsNone(MaintenanceService.parse_schedule_text("off"))
        self.assertEqual(MaintenanceService.parse_schedule_text("04:30"), (4, 30))
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            MaintenanceService.parse_schedule_text("4.30")

    def test_parse_warning_minutes_text_accepts_zero_and_valid_range(self) -> None:
        self.assertEqual(MaintenanceService.parse_warning_minutes_text("0"), 0)
        self.assertEqual(MaintenanceService.parse_warning_minutes_text("15"), 15)
        with self.assertRaisesRegex(ValueError, "0 to disable"):
            MaintenanceService.parse_warning_minutes_text("4")

    def test_due_restart_warnings_emit_configured_warning_then_one_minute_warning_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(
                config_path,
                config.BotConfiguration(
                    maintenance=config.PersistedMaintenanceSettings(
                        restart_schedules={
                            RestartTarget.SYSTEM: config.PersistedRestartSchedule(enabled=True, hour=4, minute=30),
                            RestartTarget.VOICE: config.PersistedRestartSchedule(enabled=True, hour=4, minute=30),
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
                            RestartTarget.VOICE: config.PersistedRestartSchedule(enabled=True, hour=4, minute=30),
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
