import asyncio
import contextlib
import enum
import inspect
import logging
import os
from asyncio import StreamReader
from collections.abc import Awaitable, Callable
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from typing import IO, Any

import config

log = logging.getLogger(__name__)

type AppAliveResult = bool | asyncio.Event | Awaitable[bool | asyncio.Event]
type TailPointer = Path | StreamReader | TextIOBase | BufferedIOBase | IO[Any]


class TailReaderKind(enum.StrEnum):
    PATH = "path"
    STREAM = "stream"
    TEXT = "text"
    BINARY = "binary"


class Tailer:
    def __init__(
        self,
        app_alive: Callable[[], AppAliveResult],
        pointer: TailPointer,
        output: Path | None = None,
    ):
        if not callable(app_alive):
            raise TypeError("Tailer.app_alive must be a callable that returns a bool | Awaitable[bool] | Event")  # pyright: ignore[reportUnreachable]
        self.app_alive = app_alive

        self._pointer_path: Path | None = None
        self._pointer_text: TextIOBase | None = None
        self._pointer_binary: BufferedIOBase | None = None
        self._pointer_stream: StreamReader | None = None

        if isinstance(pointer, Path):
            self._pointer_kind = TailReaderKind.PATH
            self._pointer_path = pointer
        elif isinstance(pointer, StreamReader):
            self._pointer_kind = TailReaderKind.STREAM
            self._pointer_stream = pointer
        elif isinstance(pointer, TextIOBase):
            self._pointer_kind = TailReaderKind.TEXT
            self._pointer_text = pointer
        elif isinstance(pointer, BufferedIOBase):
            self._pointer_kind = TailReaderKind.BINARY
            self._pointer_binary = pointer
        else:
            raise TypeError(f"Unsupported tail pointer: {type(pointer)!r}")

        self._read_task: asyncio.Task[None] | None = None
        self._log_clear_task: asyncio.Task[None] | None = None
        self._matchers: dict[str, Callable[[str], Awaitable[None]]] = {}

        self._log: dict[int, str] = {}
        self._next_log_index: int = 0

        self.reader: TextIOBase | None = None
        self.breader: BufferedIOBase | None = None
        self.sreader: StreamReader | None = None

        self.output = output if isinstance(output, Path) else None

        self._running: bool = False

    async def _resolve_app_alive(self) -> bool | asyncio.Event:
        result = self.app_alive()
        if inspect.isawaitable(result):
            return await result
        return result

    async def start(self, matchers: set[Callable[[str], Awaitable[None]]]) -> None:
        log.info(f"{__name__}.start")
        self._prepare_for_start()
        for matcher in matchers:
            self.register_matcher(matcher)

        result = await self._resolve_app_alive()

        if isinstance(result, asyncio.Event):
            log.info(f"{__name__}.wait: Event")
            await result.wait()
        else:
            while not result:
                log.info(f"{__name__}.wait: Sync2")
                await asyncio.sleep(1)
                result = await self._resolve_app_alive()
                if isinstance(result, asyncio.Event):
                    await result.wait()
                    break

        if not self._read_task or self._read_task.done():
            self._read_task = asyncio.create_task(self._reader_loop())
        if not self._log_clear_task or self._log_clear_task.done():
            self._log_clear_task = asyncio.create_task(self._log_cleaner())
        self._running = True

    async def stop(self) -> None:
        log.info(f"{__name__}.stop")
        for task in (self._read_task, self._log_clear_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._read_task, self._log_clear_task):
            if task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._read_task = None
        self._log_clear_task = None
        self._running = False
        self._close_readers()

    def recent_lines(self, count: int = 50) -> list[str]:
        return [self._log[i] for i in sorted(self._log.keys())[-count:]]

    def specific_lines(self, start: int = 0, end: int = 50) -> list[str]:
        keys = sorted(self._log.keys())
        start = max(0, start)
        end = min(len(keys), end)
        return [self._log[i] for i in keys[start:end]]

    def _prepare_for_start(self) -> None:
        self._log.clear()
        self._next_log_index = 0
        if self._pointer_kind is TailReaderKind.PATH:
            self._close_readers()
        else:
            self._discard_closed_readers()

    def _close_readers(self) -> None:
        if self.reader is not None:
            with contextlib.suppress(OSError, ValueError):
                self.reader.close()
            self.reader = None
        if self.breader is not None:
            with contextlib.suppress(OSError, ValueError):
                self.breader.close()
            self.breader = None
        self.sreader = None

    def _discard_closed_readers(self) -> None:
        if self.reader is not None and self.reader.closed:
            self.reader = None
        if self.breader is not None and self.breader.closed:
            self.breader = None

    def _refresh_path_reader_if_needed(self) -> None:
        if self._pointer_kind is not TailReaderKind.PATH or self._pointer_path is None or self.reader is None:
            return
        try:
            path_stat = self._pointer_path.stat()
            reader_stat = os.fstat(self.reader.fileno())
            reader_position = self.reader.tell()
        except (FileNotFoundError, OSError, ValueError):
            return

        path_replaced = (reader_stat.st_dev, reader_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        path_truncated = reader_position > path_stat.st_size
        if not path_replaced and not path_truncated:
            return

        log.info(
            "Tailer reopening path after file change: path=%s replaced=%s truncated=%s",
            self._pointer_path,
            path_replaced,
            path_truncated,
        )
        with contextlib.suppress(OSError, ValueError):
            self.reader.close()
        self.reader = None

    async def _get_reader(self) -> StreamReader | BufferedIOBase | TextIOBase | None:
        if self.sreader or self.breader or self.reader:
            return self.sreader or self.breader or self.reader
        if self._pointer_stream is not None:
            self.sreader = self._pointer_stream
        elif self._pointer_binary is not None:
            self.breader = self._pointer_binary
        elif self._pointer_path is not None:
            try:
                self.reader = self._pointer_path.open("r", encoding=config.STR_ENCODE, errors="replace")
            except OSError as xcp:
                log.debug("Tailer failed to open path %s: %s", self._pointer_path, xcp)
                return None
        elif self._pointer_text is not None:
            self.reader = self._pointer_text
        log.debug(f"Reader: {'R' if self.reader else 'B' if self.breader else 'S' if self.sreader else 'ERR'}")
        return self.sreader or self.breader or self.reader

    async def _log_cleaner(self) -> None:
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
        stdout: TextIOBase | None = None
        if self.output:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            stdout = self.output.open("w", encoding=config.STR_ENCODE, buffering=1)
        try:
            while True:
                await asyncio.sleep(0.01)
                if not self.reader and not self.breader and not self.sreader:
                    await asyncio.sleep(0.1)
                    await self._get_reader()
                    continue

                line: str | None = None

                if self.sreader:
                    raw_line = await self.sreader.readline()
                    if raw_line:
                        line = raw_line.decode(config.STR_ENCODE, "replace").rstrip("\r\n")
                    else:
                        line = ""

                elif self.breader:
                    raw = await asyncio.to_thread(self.breader.readline)
                    if raw:
                        line = raw.decode(config.STR_ENCODE, "replace")
                    else:
                        line = ""

                elif self.reader:
                    line = await asyncio.to_thread(self.reader.readline)

                if line is None:
                    await asyncio.sleep(0.1)
                    self._discard_closed_readers()
                    continue
                if not line:
                    self._discard_closed_readers()
                    self._refresh_path_reader_if_needed()
                    await asyncio.sleep(0.1)
                    continue

                line = line.strip(" \r\n\t")
                if not config.SILENT_DEBUG:
                    log.debug(f"Tailer.{self._next_log_index}.{line=}")
                self._log[self._next_log_index] = line
                self._next_log_index += 1

                if stdout and stdout.writable():
                    stdout.write(f"{line}\n")
                    stdout.flush()

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
        return self._pointer_kind.value

    def register_matcher(self, func: Callable[[str], Awaitable[None]]):
        name = f"{func.__module__}.{func.__qualname__}"
        if name in self._matchers:
            log.warning(f"Matcher {name} already registered — overwriting")
        self._matchers[name] = func

    def unregister_matcher(self, func: Callable[[str], Awaitable[None]]) -> None:
        name = f"{func.__module__}.{func.__qualname__}"
        log.warning(f"Matcher {name} deregistered")
        self._matchers.pop(name, None)


# AiviA APasz
