import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import TypeVar, overload

from factorio_rcon import AsyncRCONClient, RCONConnectError, RCONSendError

import config
from config import Name_Cache

log = logging.getLogger(__name__)
_T = TypeVar("_T")


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
        label: str | None = None,
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
        label: str | None = None,
    ):
        if getattr(self, "_initialised", False):
            return
        self._initialised = True
        self.app_alive = app_alive

        self._names = Name_Cache()

        self._host = host
        self._port = port
        self._password_env = pw_env
        self._label = label or f"{host}:{port}"
        self._password = config.env_opt(pw_env) or config.env_req("APP_COMM_PASS")
        self._password = self._password.strip("'").strip('"').strip(" ")
        self._max_attempts: int = max_attempts
        self._rcon: AsyncRCONClient | None = None
        self._running = False
        self._connected = False
        self._command_lock = asyncio.Lock()
        self._owner_loop: asyncio.AbstractEventLoop | None = self._current_running_loop()
        log.info(
            "RCON client configured: label=%s host=%s port=%s password_env=%s password_state=%s max_attempts=%s",
            self._label,
            self._host,
            self._port,
            self._password_env,
            self._describe_password_state(),
            self._max_attempts,
        )

    def _describe_password_state(self) -> str:
        if not self._password:
            return "empty"
        return f"set(len={len(self._password)})"

    @staticmethod
    def _current_running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _resolve_owner_loop(self) -> asyncio.AbstractEventLoop:
        owner_loop = getattr(self, "_owner_loop", None)
        if owner_loop is None or owner_loop.is_closed():
            owner_loop = asyncio.get_running_loop()
            self._owner_loop = owner_loop
        return owner_loop

    async def _run_on_owner_loop(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        owner_loop = self._resolve_owner_loop()
        current_loop = asyncio.get_running_loop()
        if owner_loop is current_loop:
            return await operation()

        async def _await_operation() -> _T:
            return await operation()

        future = asyncio.run_coroutine_threadsafe(_await_operation(), owner_loop)
        return await asyncio.wrap_future(future)

    async def setup(self):
        async def _setup_on_owner_loop() -> None:
            async with self._command_lock:
                await self._setup_locked()

        await self._run_on_owner_loop(_setup_on_owner_loop)

    async def _setup_locked(self) -> None:
        if self._rcon is not None and self._connected and self._running:
            return
        log.info("RCon.setup label=%s host=%s port=%s", self._label, self._host, self._port)
        self._running = True
        attempts = 0
        while attempts < self._max_attempts:
            if not self._connected:
                log.debug(
                    "RCon.wait label=%s attempts=%s host=%s port=%s", self._label, attempts, self._host, self._port
                )
            await asyncio.sleep(3)
            try:
                self._rcon = AsyncRCONClient(self._host, self._port, self._password)
                await self._rcon.connect()
                self._connected = True
                break
            except RCONConnectError:
                log.warning(
                    "RCon refused for %s at %s:%s, attempt %s/%s",
                    self._label,
                    self._host,
                    self._port,
                    attempts + 1,
                    self._max_attempts,
                )
                if attempts == self._max_attempts - 1:
                    log.exception(f"RCon refused: {traceback.format_exc()}")
            except Exception as xcp:
                log.exception("RCon.connect failed for %s at %s:%s: %s", self._label, self._host, self._port, xcp)
                return None

            attempts += 1

        if self._connected:
            log.info("RCon Connected: label=%s host=%s port=%s", self._label, self._host, self._port)
        else:
            raise RuntimeError(
                f"Failed to connect to RCon after max attempts: {self._label} @ {self._host}:{self._port}"
            )

    async def teardown(self):
        async def _teardown_on_owner_loop() -> None:
            async with self._command_lock:
                await self._teardown_locked()

        await self._run_on_owner_loop(_teardown_on_owner_loop)

    async def _teardown_locked(self) -> None:
        log.info("RCon.teardown label=%s host=%s port=%s", self._label, self._host, self._port)
        if self._rcon:
            await self._rcon.close()
            self._rcon = None
        self._running = False
        self._connected = False
        log.info("RCon Disconnected: label=%s host=%s port=%s", self._label, self._host, self._port)

    async def _ensure_connected_locked(self) -> bool:
        if self._rcon is not None and self._connected:
            return True
        log.warning(
            "RCON not connected for %s, reconnecting to %s:%s with max_attempts=%s",
            self._label,
            self._host,
            self._port,
            self._max_attempts,
        )
        await self._setup_locked()
        return self._rcon is not None and self._connected

    @property
    def is_connected(self) -> bool:
        return self._rcon is not None and self._running and self._connected

    @overload
    async def send(self, string: str, *, reconnect_on_failure: bool = True) -> str | None: ...
    @overload
    async def send(
        self, string: dict[str, str], *, reconnect_on_failure: bool = True
    ) -> dict[str, str | None] | None: ...

    async def send(
        self, string: str | dict[str, str], *, reconnect_on_failure: bool = True
    ) -> str | dict[str, str | None] | None:
        if not self.app_alive():
            return None

        async def _send_on_owner_loop() -> str | dict[str, str | None] | None:
            async with self._command_lock:
                if not await self._ensure_connected_locked():
                    return None
                if self._rcon is None:
                    raise RuntimeError(f"RCON connection for {self._label} is missing after successful setup")

                if not config.SILENT_DEBUG:
                    log.debug("Sending RCON command for %s: %s", self._label, string)
                try:
                    if isinstance(string, str):
                        data = await self._rcon.send_command(string)
                        result = data.strip() if data else None
                    else:
                        data = await self._rcon.send_commands(string)
                        result = {k: v.strip() if v else None for k, v in data.items()} if data else None
                    if not config.SILENT_DEBUG:
                        log.debug("RCON result for %s: %r", self._label, result)
                    return result
                except RCONConnectError as xcp:
                    log.warning(f"RCON Connection: {xcp}")
                    await self._teardown_locked()
                    if reconnect_on_failure and self._running and self.app_alive():
                        await self._setup_locked()
                except RCONSendError as xcp:
                    log.exception(f"RCON Send: {xcp}")
                    await self._teardown_locked()
                    if reconnect_on_failure and self._running and self.app_alive():
                        await self._setup_locked()
                except Exception as xcp:
                    log.warning(f"RCON send failed: {xcp} | {type(xcp)}")
                    await self._teardown_locked()
                    if reconnect_on_failure and self._running and self.app_alive():
                        await self._setup_locked()
                    return None
            return None

        return await self._run_on_owner_loop(_send_on_owner_loop)


# AiviA APasz
