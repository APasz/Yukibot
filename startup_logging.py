from __future__ import annotations

import logging
import os
import sys
from logging import Logger
from pathlib import Path
from types import TracebackType
from typing import Final

from logging_support import MachineJsonFormatter, SessionRotatingFileHandler

STARTUP_LOG_DIRECTORY: Final[Path] = Path("logs")
STARTUP_USER_LOG_DIRECTORY: Final[Path] = STARTUP_LOG_DIRECTORY / "_user"
STARTUP_MACHINE_LOG_DIRECTORY: Final[Path] = STARTUP_LOG_DIRECTORY / "_machine"
STARTUP_LOG_FILE: Final[Path] = STARTUP_USER_LOG_DIRECTORY / "Startup.log"
STARTUP_MACHINE_LOG_FILE: Final[Path] = STARTUP_MACHINE_LOG_DIRECTORY / "Startup.jsonl"
STARTUP_LOG_LINK: Final[Path] = STARTUP_LOG_DIRECTORY / "Startup.log"
_LEGACY_STARTUP_MACHINE_LOG_FILE: Final[Path] = STARTUP_LOG_DIRECTORY / "Startup.jsonl"
_STARTUP_LOGGER_NAME: Final[str] = "startup"
_HANDLER_NAME: Final[str] = "startup_file"
_MACHINE_HANDLER_NAME: Final[str] = "startup_machine_file"
_LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname).1s %(name)-25s - %(message)s"
_installed: bool = False


def _startup_file_handler() -> SessionRotatingFileHandler:
    STARTUP_LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    handler: SessionRotatingFileHandler = SessionRotatingFileHandler(
        STARTUP_LOG_FILE,
        encoding="utf-8",
        legacy_path=STARTUP_LOG_LINK,
        current_link_path=STARTUP_LOG_LINK,
    )
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    return handler


def _startup_machine_file_handler() -> SessionRotatingFileHandler:
    STARTUP_LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    handler: SessionRotatingFileHandler = SessionRotatingFileHandler(
        STARTUP_MACHINE_LOG_FILE,
        encoding="utf-8",
        legacy_path=_LEGACY_STARTUP_MACHINE_LOG_FILE,
    )
    handler.set_name(_MACHINE_HANDLER_NAME)
    handler.setFormatter(
        MachineJsonFormatter(
            node_name=os.environ.get("NODE_NAME"),
            bot_profile=os.environ.get("BOT_PROFILE"),
        )
    )
    return handler


def _startup_logger() -> logging.Logger:
    logger: Logger = logging.getLogger(_STARTUP_LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    if not any(handler.get_name() == _HANDLER_NAME for handler in logger.handlers):
        logger.addHandler(_startup_file_handler())
    if not any(handler.get_name() == _MACHINE_HANDLER_NAME for handler in logger.handlers):
        logger.addHandler(_startup_machine_file_handler())
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
