from __future__ import annotations

import asyncio
import contextlib
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._tailer import Tailer


class TailerTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacing_stream_reader_preserves_output_and_cached_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "stdout.log"
            first = asyncio.StreamReader()
            second = asyncio.StreamReader()
            lines: list[str] = []

            async def match(line: str) -> None:
                lines.append(line)

            tailer = Tailer(lambda: True, first, output)
            try:
                await tailer.start({match})
                first.feed_data(b"before reconnect\n")
                await self._wait_for_lines(lines, expected_count=1)
                await tailer.replace_stream_reader(second)
                first.feed_data(b"obsolete reader\n")
                second.feed_data(b"after reconnect\n")
                await self._wait_for_lines(lines, expected_count=2)
                task = tailer._read_task
                await tailer.replace_stream_reader(second)
                self.assertIs(tailer._read_task, task)

                assert task is not None
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                self.assertFalse(tailer.is_running)
                await tailer.replace_stream_reader(second)
                second.feed_data(b"after recovery\n")
                await self._wait_for_lines(lines, expected_count=3)
                self.assertTrue(tailer.is_running)
                self.assertEqual(tailer.recent_lines(), lines)
                self.assertEqual(output.read_text(), "before reconnect\nafter reconnect\nafter recovery\n")
            finally:
                await tailer.stop()
            self.assertFalse(tailer.is_running)

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
