from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import TracebackType
from typing import Final

STARTUP_LOG_DIRECTORY: Final[Path] = Path("logs")
STARTUP_LOG_FILE: Final[Path] = STARTUP_LOG_DIRECTORY / "Startup.log"
_STARTUP_LOGGER_NAME: Final[str] = "startup"
_HANDLER_NAME: Final[str] = "startup_file"
_LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname).1s %(name)-25s - %(message)s"
_installed: bool = False


def _startup_file_handler() -> logging.FileHandler:
    STARTUP_LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(STARTUP_LOG_FILE, mode="a", encoding="utf-8")
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    return handler


def _startup_logger() -> logging.Logger:
    logger = logging.getLogger(_STARTUP_LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    if not any(handler.get_name() == _HANDLER_NAME for handler in logger.handlers):
        logger.addHandler(_startup_file_handler())
    return logger


def install_startup_exception_logger() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    previous_hook = sys.excepthook

    def _log_uncaught_exception(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            _startup_logger().critical(
                "Unhandled exception during process startup/runtime",
                exc_info=(exc_type, exc_value, exc_traceback),
            )
        previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _log_uncaught_exception
