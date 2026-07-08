from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from _utils import File_Cleaner


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


if __name__ == "__main__":
    unittest.main()
