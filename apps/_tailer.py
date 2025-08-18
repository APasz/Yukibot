import asyncio
from collections.abc import Awaitable, Callable
import inspect
from io import TextIOWrapper
import logging
from pathlib import Path
from typing import IO, TextIO, BinaryIO
from asyncio import StreamReader

import config

log = logging.getLogger(__name__)


class Tailer:
    _instances: dict[str, "Tailer"] = {}
    _pointer_path = _pointer_text = _pointer_stream = _pointer_binary = None

    def __new__(
        cls,
        app_alive: Callable[[], bool | Awaitable[bool | asyncio.Event]],
        pointer: Path | StreamReader | TextIO | BinaryIO | IO,
        output: Path | None = None,
    ):
        if isinstance(pointer, Path):
            key = f"fileio:{str(pointer.resolve())}"
        elif isinstance(pointer, StreamReader):
            key = f"stream:{id(pointer)}"
        elif isinstance(pointer, TextIO):
            key = f"textio:{id(pointer)}"
        elif isinstance(pointer, BinaryIO):
            key = f"binyio:{id(pointer)}"
        elif hasattr(pointer, "name"):
            key = f"uknown:{id(pointer.name)}"
        else:
            raise ValueError("Unable to make pointer unique")

        if key in cls._instances:
            return cls._instances[key]
        instance = super().__new__(cls)
        cls._instances[key] = instance
        return instance

    def __init__(
        self,
        app_alive: Callable[[], bool | Awaitable[bool] | Awaitable[asyncio.Event]],
        pointer: Path | StreamReader | TextIO | BinaryIO | IO,
        output: Path | None = None,
    ):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        if not callable(app_alive):
            raise TypeError("Tailer.app_alive must be a callable that returns a bool | Awaitable[bool] | Event")
        self.app_alive = app_alive

        if isinstance(pointer, Path):
            log.error(f"Tail Pointer: Path={pointer}")
            self._pointer_path = pointer
        elif isinstance(pointer, TextIO | TextIOWrapper):
            log.error(f"Tail Pointer: TextIO={pointer}")
            self._pointer_text = pointer
        elif isinstance(pointer, BinaryIO):
            log.error(f"Tail Pointer: BinaryIO={pointer}")
            self._pointer_binary = pointer
        elif isinstance(pointer, StreamReader):
            log.error(f"Tail Pointer: Stream={pointer}")
            self._pointer_stream = pointer
        else:
            log.error(f"Tail Pointer Invalid: {pointer}")

        self._read_task: asyncio.Task | None = None
        self._log_clear_task: asyncio.Task | None = None
        self._matchers: dict[str, Callable[[str], Awaitable[None]]] = {}

        self._log: dict[int, str] = {}

        self.reader: TextIO | None = None
        self.breader: BinaryIO | None = None
        self.sreader: StreamReader | None = None

        self.output = output if isinstance(output, Path) else None

        self._running: bool = False

    async def start(self, matchers: set):
        log.info(f"{__name__}.start")
        for matcher in matchers:
            self.register_matcher(matcher)

        result = self.app_alive()

        if isinstance(result, asyncio.Event):
            log.info(f"{__name__}.wait: Event")
            await result.wait()
        elif inspect.isawaitable(result):
            value = await result
            if isinstance(value, asyncio.Event):
                log.info(f"{__name__}.wait: Async")
                await value.wait()
            elif value is True:
                pass
            else:
                while not value:
                    log.info(f"{__name__}.wait: Sync1")
                    await asyncio.sleep(1)
                    value = await self.app_alive()  # type: ignore
        else:
            while not result:
                log.info(f"{__name__}.wait: Sync2")
                await asyncio.sleep(1)
                result = self.app_alive()

        if not self._read_task or self._read_task.done():
            self._read_task = asyncio.create_task(self._reader_loop())
        if not self._log_clear_task or self._log_clear_task.done():
            self._log_clear_task = asyncio.create_task(self._log_cleaner())
        self._running = True

    async def stop(self):
        log.info(f"{__name__}.stop")
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if self._log_clear_task and not self._log_clear_task.done():
            self._log_clear_task.cancel()
        self._running = False

    def recent_lines(self, count: int = 50) -> list[str]:
        return [self._log[i] for i in sorted(self._log.keys())[-count:]]

    def specific_lines(self, start: int = 0, end: int = 50) -> list[str]:
        keys = sorted(self._log.keys())
        start = max(0, start)
        end = min(len(keys), end)
        return [self._log[i] for i in keys[start:end]]

    async def _get_reader(self):
        if self.sreader or self.breader or self.reader:
            return self.sreader or self.breader or self.reader
        if self._pointer_stream:
            self.sreader = self._pointer_stream
        if self._pointer_binary and not self.breader:
            self.breader = self._pointer_binary
        if self._pointer_path and not self.reader:
            self.reader = self._pointer_path.open("r")
        if self._pointer_text and not self.reader:
            self.reader = self._pointer_text
        log.debug(f"Reader: {'R' if self.reader else 'B' if self.breader else 'S' if self.sreader else 'ERR'}")
        return self.sreader or self.breader or self.reader

    async def _log_cleaner(self):
        MAX_LOG = 5000
        PRUNE_CHUNK = 1000
        while True:
            await asyncio.sleep(60)
            if len(self._log) < MAX_LOG:
                continue
            keys = sorted(self._log.keys())
            to_delete = keys[:PRUNE_CHUNK]
            for k in to_delete:
                self._log.pop(k, None)

    async def _reader_loop(self):
        count = 0
        stdout = self.output.open("w") if self.output else None
        try:
            while True:
                await asyncio.sleep(0.01)
                if not self.reader and not self.breader and not self.sreader:
                    log.debug("GET READER")
                    await asyncio.sleep(1)
                    await self._get_reader()
                    continue

                line = None

                if self.sreader:
                    raw_line = await self.sreader.readline()
                    line = raw_line.decode(config.STR_ENCODE, "replace").rstrip("\r\n")

                elif self.breader:
                    raw = await asyncio.to_thread(self.breader.readline)
                    if raw:
                        line = raw.decode(config.STR_ENCODE, "replace")

                elif self.reader:
                    line = await asyncio.to_thread(self.reader.readline)

                if line is None:
                    await asyncio.sleep(0.1)
                    if self.breader and getattr(self.breader, "closed", False):
                        self.breader = None
                    if self.reader and getattr(self.reader, "closed", False):
                        self.reader = None
                    continue
                elif not line:
                    continue

                line = line.strip(" \r\n\t")
                if not config.SILENT_DEBUG:
                    log.debug(f"Tailer.{count}.{line=}")
                self._log[count] = line
                count += 1

                if stdout and stdout.writable:
                    stdout.write(f"{line}\n")

                for func, matcher in self._matchers.items():
                    if not config.SILENT_DEBUG:
                        log.debug(f"Running Matcher: {func}")
                    await matcher(line)

        except Exception:
            log.exception("Error in Tailer reader loop")
            if stdout:
                stdout.close()
            await asyncio.sleep(2)
        finally:
            if stdout:
                stdout.close()

    @property
    def reader_type(self) -> str:
        if self._pointer_stream:
            return "stream"
        if self.breader:
            return "binary"
        if self.reader:
            return "text"
        return "unknown"

    def register_matcher(self, func: Callable[[str], Awaitable[None]]):
        name = f"{func.__module__}.{func.__name__}"
        if name in self._matchers:
            log.warning(f"Matcher {name} already registered — overwriting")
        self._matchers[name] = func

    def unregister_matcher(self, func: Callable):
        name = f"{func.__module__}.{func.__name__}"
        log.warning(f"Matcher {name} deregistered")
        self._matchers.pop(name, None)


# AiviA APasz
