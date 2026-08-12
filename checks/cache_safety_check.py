from __future__ import annotations

import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cache_safety import prepare_private_cache_directory


class CacheSafetyTests(unittest.TestCase):
    def test_private_cache_directory_is_created_with_owner_only_permissions(self) -> None:
        with TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "cache"

            prepared_directory = prepare_private_cache_directory(cache_directory)

            self.assertEqual(prepared_directory, str(cache_directory))
            self.assertEqual(cache_directory.stat().st_mode & 0o777, 0o700)

    def test_group_or_world_writable_cache_directory_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            cache_directory = Path(directory) / "cache"
            cache_directory.mkdir(mode=0o700)
            cache_directory.chmod(stat.S_IRWXU | stat.S_IWGRP)

            with self.assertRaisesRegex(ValueError, "group- or world-writable"):
                prepare_private_cache_directory(cache_directory)


if __name__ == "__main__":
    unittest.main()
