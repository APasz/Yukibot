import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._app import App, AppStdoutTail


class AppLogTailTests(unittest.TestCase):
    def test_truncation_counts_lines_with_or_without_a_final_newline(self) -> None:
        for suffix in ("", "\n", "\r\n"):
            with self.subTest(suffix=suffix), TemporaryDirectory() as tmp:
                path = Path(tmp) / "output.log"
                path.write_bytes(f"first\nsecond{suffix}".encode())
                self.assertEqual(
                    App._read_log_tail(path, max_lines=1),
                    AppStdoutTail(lines=("second",), truncated=True),
                )
                self.assertEqual(
                    App._read_log_tail(path, max_lines=2),
                    AppStdoutTail(lines=("first", "second"), truncated=False),
                )

    def test_large_utf8_logs_preserve_the_last_complete_lines_across_chunks(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.log"
            last_line = "🚚" * 3000
            path.write_text("old\n" * 5000 + last_line + "\nlast", encoding="utf-8")
            self.assertEqual(
                App._read_log_tail(path, max_lines=2),
                AppStdoutTail(lines=(last_line, "last"), truncated=True),
            )

    def test_empty_and_missing_logs_return_an_empty_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.log"
            self.assertEqual(App._read_log_tail(path, max_lines=2), AppStdoutTail(lines=(), truncated=False))
            path.touch()
            self.assertEqual(App._read_log_tail(path, max_lines=2), AppStdoutTail(lines=(), truncated=False))

    def test_invalid_line_limits_fail_even_when_the_log_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.log"
            for limit in (0, -1):
                with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "at least 1"):
                    App._read_log_tail(path, max_lines=limit)


if __name__ == "__main__":
    unittest.main()
