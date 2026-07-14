from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType

import startup_logging


class StartupLoggingTests(unittest.TestCase):
    def test_uncaught_exception_hook_writes_startup_log(self) -> None:
        previous_hook = sys.excepthook
        logger = logging.getLogger("startup")
        previous_handlers = list(logger.handlers)
        for handler in previous_handlers:
            logger.removeHandler(handler)

        captured_errors: list[str] = []

        def capture_previous_hook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_traceback: TracebackType | None,
        ) -> None:
            del exc_type, exc_traceback
            captured_errors.append(str(exc_value))

        original_cwd = Path.cwd()
        startup_logging._installed = False
        try:
            with TemporaryDirectory() as temp_dir:
                os.chdir(temp_dir)
                sys.excepthook = capture_previous_hook
                startup_logging.install_startup_exception_logger()

                error = ModuleNotFoundError("No module named 'apps._node_api'")
                sys.excepthook(type(error), error, error.__traceback__)

                log_text = (Path(temp_dir) / startup_logging.STARTUP_LOG_FILE).read_text(encoding="utf-8")
                self.assertIn("Unhandled exception during process startup/runtime", log_text)
                self.assertIn("ModuleNotFoundError: No module named 'apps._node_api'", log_text)
                self.assertEqual(["No module named 'apps._node_api'"], captured_errors)
        finally:
            os.chdir(original_cwd)
            sys.excepthook = previous_hook
            startup_logging._installed = False
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in previous_handlers:
                logger.addHandler(handler)


if __name__ == "__main__":
    unittest.main()
