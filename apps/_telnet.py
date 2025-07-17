from collections.abc import Callable, Sequence
import logging
import asyncio

import config


log = logging.getLogger(__name__)


class TelnetClient:
    _instances: dict[tuple[int, str, int], "TelnetClient"] = {}

    def __new__(cls, app_alive: Callable[[], bool], port: int, /, host: str = "localhost", **kwargs):
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
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.app_alive = app_alive
        self._host = host
        self._port = port
        self._max_attempts: int = max_attempts

        self._reader: None | asyncio.StreamReader = None
        self._writer: None | asyncio.StreamWriter = None
        self._setup_lock: asyncio.Lock = asyncio.Lock()
        self.connected_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

        self._prefix = prefix
        self._suffix = suffix

    async def setup(self) -> asyncio.StreamReader:
        async with self._setup_lock:
            self.connected_event.set()
            if self._reader:
                return self._reader

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

    async def teardown(self):
        log.info("Telnet.teardown")
        self.connected_event.clear()
        if not self._running:
            return
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
        self._reader = None
        self._running = False
        log.info("Telnet Disconnected")

    async def send(self, string: str | Sequence[str]) -> bool | None:
        if not self.is_connected:
            if not self.app_alive():
                log.debug("Telnet.send: App.Alive=False")
                return None
            await self.setup()
            if not self.is_connected:
                return False

        def str_fmt(value: str) -> bytes:
            return f"{self._prefix}{value}{self._suffix}".encode(config.STR_ENCODE)

        try:
            if not config.SILENT_DEBUG:
                log.debug(f"Sending Telnet command: {string!r}")
            if not self._writer:
                raise ConnectionError("Telnet.write: Not Connected")
            if isinstance(string, str):
                self._writer.write(str_fmt(string))
            else:
                for cmd in string:
                    self._writer.write(str_fmt(cmd))
            await self._writer.drain()
            return True
        except Exception as xcp:
            log.warning(f"Telnet send failed: {xcp}")
            await self.teardown()
            return False

    @property
    def is_connected(self) -> bool:
        return self._reader is not None and self._writer is not None and self._running

    @property
    def reader(self) -> asyncio.StreamReader | None:
        return self._reader

    def __repr__(self):
        return f"<{__class__} {self._host}:{self._port} connected={self.is_connected}>"


# AiviA APasz
