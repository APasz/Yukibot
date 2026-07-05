from __future__ import annotations

import asyncio
import threading
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

    def test_stop_cancels_foreign_loop_tasks_without_cross_loop_await(self) -> None:
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "server.log"
            log_path.write_text("", encoding="utf-8")

            tailer_ready = threading.Event()
            tailer_holder: dict[str, Tailer] = {}

            def run_tailer_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                async def run_tailer() -> None:
                    tailer = Tailer(lambda: True, log_path)
                    tailer_holder["tailer"] = tailer
                    await tailer.start(set())
                    tailer_ready.set()
                    await asyncio.gather(tailer._read_task, tailer._log_clear_task)

                try:
                    loop.run_until_complete(run_tailer())
                except asyncio.CancelledError:
                    pass
                finally:
                    loop.close()

            thread = threading.Thread(target=run_tailer_loop)
            thread.start()
            self.assertTrue(tailer_ready.wait(timeout=1.0))

            tailer = tailer_holder["tailer"]
            asyncio.run(tailer.stop())

            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())

    @staticmethod
    async def _wait_for_lines(lines: list[str], *, expected_count: int, timeout_seconds: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while len(lines) < expected_count and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if len(lines) < expected_count:
            raise AssertionError(f"Expected {expected_count} tailed lines, received {len(lines)}: {lines}")


if __name__ == "__main__":
    unittest.main()
