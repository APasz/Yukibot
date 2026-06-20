from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._tailer import Tailer


class TailerTests(unittest.IsolatedAsyncioTestCase):
    async def test_restarting_path_tailer_reopens_file_from_start(self) -> None:
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "server.log"
            log_path.write_text("first\n", encoding="utf-8")

            first_lines: list[str] = []

            async def first_matcher(line: str) -> None:
                first_lines.append(line)

            first_tailer = Tailer(lambda: True, log_path)
            await first_tailer.start({first_matcher})
            await self._wait_for_lines(first_lines, expected_count=1)
            await first_tailer.stop()

            log_path.write_text("second\n", encoding="utf-8")
            second_lines: list[str] = []

            async def second_matcher(line: str) -> None:
                second_lines.append(line)

            second_tailer = Tailer(lambda: True, log_path)
            self.assertIsNot(first_tailer, second_tailer)

            await second_tailer.start({second_matcher})
            await self._wait_for_lines(second_lines, expected_count=1)
            await second_tailer.stop()

        self.assertEqual(first_lines, ["first"])
        self.assertEqual(second_lines, ["second"])

    @staticmethod
    async def _wait_for_lines(lines: list[str], *, expected_count: int, timeout_seconds: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while len(lines) < expected_count and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if len(lines) < expected_count:
            raise AssertionError(f"Expected {expected_count} tailed lines, received {len(lines)}: {lines}")


if __name__ == "__main__":
    unittest.main()
