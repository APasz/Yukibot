import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence

import config

log = logging.getLogger(__name__)


class TelnetClient:
    _instances: dict[tuple[int, str, int], "TelnetClient"] = {}

    def __new__(
        cls,
        app_alive: Callable[[], bool],
        port: int,
        /,
        host: str = "localhost",
        max_attempts: int = 20,
        prefix: str = "",
        suffix: str = "\n",
    ) -> "TelnetClient":
        del max_attempts, prefix, suffix
        key = id(app_alive), host, port
        if key in cls._instances:
            return cls._instances[key]
        instance = super().__new__(cls)
        cls._instances[key] = instance
        return instance

    def __init__(
        self,
        app_alive: Callable[[], bool],
        port: int,
        /,
        host: str = "localhost",
        max_attempts: int = 20,
        prefix: str = "",
        suffix: str = "\n",
    ):
        if getattr(self, "_initialised", False):
            return
        self._initialised = True

        self.app_alive = app_alive
        self._host = host
        self._port = port
        self._max_attempts: int = max_attempts

        self._reader: None | asyncio.StreamReader = None
        self._writer: None | asyncio.StreamWriter = None
        self._setup_lock: asyncio.Lock = asyncio.Lock()
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self.connected_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

        self._prefix = prefix
        self._suffix = suffix

    async def setup(self) -> asyncio.StreamReader:
        async with self._setup_lock:
            if self.is_connected and self._reader is not None:
                return self._reader

            await self._teardown_locked()

            log.info(f"Telnet.setup @ {self._host}:{self._port}")

            attempts = 0
            while attempts < self._max_attempts:
                if not self.app_alive():
                    log.debug(f"Telnet.wait: {attempts=}")
                    await asyncio.sleep(1)
                    continue

                try:
                    self._reader, self._writer = await asyncio.wait_for(
                        asyncio.open_connection(self._host, self._port), timeout=10
                    )
                    self._running = True
                    self.connected_event.set()
                    log.info("Telnet Connected")
                    return self._reader
                except ConnectionRefusedError:
                    log.warning(f"Telnet refused, attempt {attempts + 1}/{self._max_attempts}")
                except Exception as xcp:
                    log.exception(f"Telnet.connect failed: {xcp}")
                    break

                attempts += 1
                await asyncio.sleep(1)

            raise RuntimeError("Failed to connect to Telnet after max attempts")

    async def _teardown_locked(self) -> None:
        log.info("Telnet.teardown")
        self.connected_event.clear()
        writer = self._writer
        self._writer = None
        self._reader = None
        self._running = False
        if writer is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionError, OSError, RuntimeError):
                writer.close()
                await writer.wait_closed()
        log.info("Telnet Disconnected")

    async def teardown(self) -> None:
        async with self._setup_lock:
            await self._teardown_locked()

    async def send(self, string: str | Sequence[str]) -> bool | None:
        def str_fmt(value: str) -> bytes:
            return f"{self._prefix}{value}{self._suffix}".encode(config.STR_ENCODE)

        async with self._send_lock:
            for attempt in range(2):
                if not self.is_connected:
                    if not self.app_alive():
                        log.debug("Telnet.send: App.Alive=False")
                        return None
                    try:
                        await self.setup()
                    except Exception as xcp:
                        log.warning("Telnet reconnect failed: %s", xcp)
                        return False

                try:
                    if not config.SILENT_DEBUG:
                        log.debug(f"Sending Telnet command: {string!r}")
                    writer = self._writer
                    if writer is None:
                        raise ConnectionError("Telnet.write: Not Connected")
                    if isinstance(string, str):
                        writer.write(str_fmt(string))
                    else:
                        for cmd in string:
                            writer.write(str_fmt(cmd))
                    await writer.drain()
                    return True
                except Exception as xcp:
                    log.warning("Telnet send failed (attempt %s/2): %s", attempt + 1, xcp)
                    await self.teardown()
            return False

    @property
    def is_connected(self) -> bool:
        reader = self._reader
        writer = self._writer
        return (
            reader is not None
            and writer is not None
            and self._running
            and not reader.at_eof()
            and not writer.is_closing()
        )

    @property
    def reader(self) -> asyncio.StreamReader | None:
        return self._reader

    def __repr__(self):
        return f"<{__class__} {self._host}:{self._port} connected={self.is_connected}>"


# AiviA APasz
