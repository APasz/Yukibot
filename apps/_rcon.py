import asyncio
from collections.abc import Callable
import logging
import traceback
from typing import overload
from factorio_rcon import AsyncRCONClient, RCONConnectError, RCONSendError
from config import Name_Cache
import config


log = logging.getLogger(__name__)


class RconClient:
    _instances: dict[tuple[int, str, int], "RconClient"] = {}

    def __new__(
        cls,
        app_alive: Callable[[], bool],
        port: int,
        pw_env: str = "APP_COMM_PASS",
        /,
        host: str = "localhost",
        max_attempts: int = 30,
    ):
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
        pw_env: str = "APP_COMM_PASS",
        /,
        host: str = "localhost",
        max_attempts: int = 30,
    ):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.app_alive = app_alive

        self._names = Name_Cache()

        self._host = host
        self._port = port
        self._password = config.env_opt(pw_env) or config.env_req("APP_COMM_PASS")
        self._password = self._password.strip("'").strip('"').strip(" ")
        log.debug(f"RCON: {self._password=} | {self._port} | {self._host}")
        self._max_attempts: int = max_attempts
        self._rcon: AsyncRCONClient | None = None
        self._running = False
        self._connected = False

    async def setup(self):
        log.info("RCon.setup")
        self._running = True
        attempts = 0
        while attempts < self._max_attempts:
            if not self._connected:
                log.debug(f"RCon.wait: {attempts=}")
            await asyncio.sleep(3)
            try:
                self._rcon = AsyncRCONClient(self._host, self._port, self._password)
                await self._rcon.connect()
                self._connected = True
                break
            except RCONConnectError:
                log.warning(f"RCon refused, attempt {attempts + 1}/{self._max_attempts}")
                if attempts == self._max_attempts - 1:
                    log.exception(f"RCon refused: {traceback.format_exc()}")
            except Exception as xcp:
                log.exception(f"RCon.connect: {xcp}")
                return None

            attempts += 1

        if self._connected:
            log.info("RCon Connected")
        else:
            raise RuntimeError("Failed to connect to RCon after max attempts")

    async def teardown(self):
        log.info("RCon.teardown")
        if self._rcon:
            await self._rcon.close()
            self._rcon = None
        self._running = False
        self._connected = False
        log.info("RCon Disconnected")

    @property
    def is_connected(self) -> bool:
        return self._rcon is not None and self._running

    @overload
    async def send(self, string: str) -> str | None: ...
    @overload
    async def send(self, string: dict[str, str]) -> dict[str, str | None] | None: ...

    async def send(self, string: str | dict[str, str]) -> str | dict[str, str | None] | None:
        if not self.app_alive():
            return None
        if not self._rcon or not self._connected:
            log.warning(f"RCON not connected: {self._max_attempts} attempts left")
            await self.setup()
            if not self._rcon:
                return None

        if not config.SILENT_DEBUG:
            log.debug(f"Sending RCON command: {string}")
        try:
            if isinstance(string, str):
                data = await self._rcon.send_command(string)
                return data.strip() if data else None
            elif isinstance(string, dict):
                data = await self._rcon.send_commands(string)
                return {k: v.strip() if v else None for k, v in data.items()} if data else None
        except RCONConnectError as xcp:
            log.warning(f"RCON Connection: {xcp}")
            await self.teardown()
            await self.setup()
        except RCONSendError as xcp:
            log.exception(f"RCON Send: {xcp}")
            await self.teardown()
            await self.setup()
        except Exception as xcp:
            log.warning(f"RCON send failed: {xcp} | {type(xcp)}")
            await self.teardown()
            await self.setup()
            return None


# AiviA APasz
