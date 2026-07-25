from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from _utils import File_Cleaner, Utilities


class FileCleanerTests(unittest.TestCase):
    def test_clear_accepts_mixed_files_and_directories(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            stale_file = base / "stale.txt"
            stale_dir = base / "stale-dir"
            nested_file = stale_dir / "nested.txt"
            fresh_file = base / "fresh.txt"
            stale_file.write_text("stale", encoding="utf-8")
            stale_dir.mkdir()
            nested_file.write_text("nested", encoding="utf-8")
            fresh_file.write_text("fresh", encoding="utf-8")

            stale_at = datetime.now() - timedelta(hours=2)
            stale_timestamp = stale_at.timestamp()
            fresh_timestamp = datetime.now().timestamp()
            stale_file.touch()
            fresh_file.touch()
            stale_dir.touch()
            nested_file.touch()
            for path in (stale_file, stale_dir, nested_file):
                path.touch()
                os.utime(path, (stale_timestamp, stale_timestamp))
            os.utime(fresh_file, (fresh_timestamp, fresh_timestamp))

            remaining = File_Cleaner.clear({stale_file, stale_dir, fresh_file}, timedelta(hours=1))
            self.assertEqual(remaining, {fresh_file})
            self.assertFalse(stale_file.exists())
            self.assertFalse(stale_dir.exists())
            self.assertTrue(fresh_file.exists())


class TimestampFormattingTests(unittest.TestCase):
    def test_timestamp_format_representations_cover_every_available_style(self) -> None:
        styles = {template[-2] for _, template in Utilities.DISCORD_TIMESTAMP_FORMATS}

        self.assertEqual(styles, set(Utilities.DISCORD_TIMESTAMP_STYLE_REPRESENTATIONS))
        self.assertEqual(Utilities.DISCORD_TIMESTAMP_STYLE_REPRESENTATIONS["T"], "HH:MM:SS")
        self.assertEqual(Utilities.DISCORD_TIMESTAMP_STYLE_REPRESENTATIONS["F"], "Day Mon DD YYYY HH:MM")

    def test_exact_and_relative_time_parsers_keep_their_domains_separate(self) -> None:
        self.assertEqual(
            Utilities.parse_exact_time("0", tz=timezone.utc),
            datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(Utilities.parse_exact_time("2h", tz=timezone.utc))
        self.assertIsNone(Utilities.parse_relative_time("0", tz=timezone.utc))
        self.assertIsNotNone(Utilities.parse_relative_time("2h", tz=timezone.utc))

    def test_round_wallclock_rounds_to_nearest_minute(self) -> None:
        timestamp = datetime(2026, 7, 25, 12, 34, 30, tzinfo=timezone.utc)

        self.assertEqual(
            Utilities.round_wallclock(timestamp, "MI"),
            datetime(2026, 7, 25, 12, 35, tzinfo=timezone.utc),
        )

    def test_round_wallclock_rejects_unknown_unit(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown timestamp rounding unit"):
            Utilities.round_wallclock(datetime(2026, 7, 25, tzinfo=timezone.utc), "quarter")

    def test_timezone_selection_options_are_deduplicated_and_context_sensitive(self) -> None:
        default_options = Utilities.timezone_selection_options()
        offset_options = Utilities.timezone_selection_options("+10")
        location_options = Utilities.timezone_selection_options("mel")
        default_values = {option.value for option in default_options}
        offset_values = {option.value for option in offset_options}
        location_values = {option.value for option in location_options}

        self.assertIn("UTC", default_values)
        self.assertIn("Australia/Melbourne", default_values)
        self.assertNotIn("UTC+10:00", default_values)
        self.assertNotIn("Pacific/Pago_Pago", default_values)
        self.assertEqual(len(default_values), len(default_options))
        self.assertEqual(offset_values, {"UTC+10:00"})
        self.assertIn("Australia/Melbourne", location_values)

        melbourne_option = next(
            option for option in location_options if option.value == "Australia/Melbourne"
        )
        self.assertIn(
            "Australia/Melbourne",
            {
                option.value
                for option in Utilities.timezone_selection_options(melbourne_option.timezone_code)
            },
        )

    def test_timezone_search_accepts_partial_iana_paths(self) -> None:
        self.assertIsNone(Utilities.parse_timezone("australia/"))
        self.assertIn(
            "Australia/Melbourne",
            {option.value for option in Utilities.timezone_selection_options("australia/")},
        )


if __name__ == "__main__":
    unittest.main()
