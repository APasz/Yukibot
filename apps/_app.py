from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import signal
import subprocess
from abc import abstractmethod
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

import hikari
import psutil

import _errors
import config
from apps._config import App_Config, Mod_Config, RelayChannelSource
from apps._mod import Mod, Mod_Manager
from apps._settings import App_Settings, Settings_Manager
from apps._updater import Update_Manager
from config import Activity_Manager

if TYPE_CHECKING:
    from _discord import App_Bound

log = logging.getLogger(__name__)


class AM_Receiver(Protocol):
    async def send(self, payload: App_Bound) -> None: ...


class ChatRelaySupport(enum.StrEnum):
    NONE = "none"
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BIDIRECTIONAL = "bidirectional"

    @property
    def capability_label(self) -> str | None:
        if self is ChatRelaySupport.NONE:
            return None
        if self is ChatRelaySupport.INBOUND:
            return "Chat [In]"
        if self is ChatRelaySupport.OUTBOUND:
            return "Chat [Out]"
        return "Chat"

    @property
    def display_value(self) -> str:
        if self is ChatRelaySupport.NONE:
            return "Unsupported"
        if self is ChatRelaySupport.INBOUND:
            return "Inbound only"
        if self is ChatRelaySupport.OUTBOUND:
            return "Outbound only"
        return "Inbound + Outbound"

    @property
    def supports_inbound(self) -> bool:
        return self in {ChatRelaySupport.INBOUND, ChatRelaySupport.BIDIRECTIONAL}

    @property
    def supports_outbound(self) -> bool:
        return self in {ChatRelaySupport.OUTBOUND, ChatRelaySupport.BIDIRECTIONAL}


class App:
    cfg_cls: type[App_Config] = App_Config
    bot: hikari.GatewayBot
    cfg: App_Config
    name: str
    friendly: str
    scope: str
    proc_name: str
    proc_cmd: list[str]
    directory: Path
    dir_log: Path
    server_log: Path | None
    mods: Mod_Manager | None = None
    settings: Settings_Manager | None
    saves = None
    updater: Update_Manager | None = None
    process: subprocess.Popen | None = None
    file_stdout: Path
    file_errout: Path
    act_err_counts: dict[str, int] = {}
    act_err_threshold = 25
    name_cache = config.Name_Cache()
    am_receiver: "AM_Receiver | None" = None
    chat_relay_outbound: bool = False
    cmd_start: list[str]
    cmd_cwd: Path | None = None
    shell: bool = False
    _stderr_task = None
    _running: bool = False
    chat_channel: hikari.Snowflake | None = None
    chat_channel_override: hikari.Snowflake | None = None
    chat_channel_source: RelayChannelSource = RelayChannelSource.NONE
    activity_manager: Activity_Manager
    providers: list[config.Activity_Provider]
    manage_embed_color: int = 0x96212B

    def __init__(
        self,
        bot: hikari.GatewayBot,
        activity_manager: Activity_Manager,
        cfg: App_Config,
        stg: App_Settings | None = None,
        mod_cls: type[Mod] | None = None,
        modcf_cls: type[Mod_Config] | None = None,
    ):
        if not bot:
            raise ValueError("App missing bot")  # pyright: ignore[reportUnreachable]
        if not cfg:
            raise ValueError("App missing instance configuration")  # pyright: ignore[reportUnreachable]
        self.bot = bot
        self.cfg = cfg
        self.name = cfg.name
        self.friendly = cfg.friendly_name or cfg.name.title()
        self.scope = cfg.scope
        self.directory = cfg.directory
        self.chat_channel = hikari.Snowflake(cfg.chat_channel) if cfg.chat_channel else None
        self.chat_channel_override = hikari.Snowflake(cfg.chat_channel_override) if cfg.chat_channel_override else None
        self.chat_channel_source = cfg.chat_channel_source
        self.server_log = cfg.server_log_file
        self.dir_log = Path(config.DIR_LOG, self.name)
        self.dir_log.mkdir(exist_ok=True, parents=True)
        self.file_stdout = self.dir_log.joinpath("stdout.log")
        self.file_errout = self.dir_log.joinpath("errout.log")

        if mod_cls:
            if modcf_cls:
                self.mods = Mod_Manager(cfg, mod_cls, modcf_cls)
            else:
                self.mods = Mod_Manager(cfg, mod_cls)
        if stg:
            self.settings = Settings_Manager(cfg, stg)
        else:
            self.settings = None
        self.saves = None  # TODO Save_Manager
        self.activity_manager = activity_manager

        self.providers = []

        log.debug(f"{__name__} | {self.cmd_start=} @ {self.cmd_cwd=}")

    async def post_init(self):
        if self.mods:
            await self.mods.load_mods()
        log.debug(f"{self.name}.__post_init__")

    @property
    def chat_relay_support(self) -> ChatRelaySupport:
        has_inbound = self.am_receiver is not None
        has_outbound = self.chat_relay_outbound
        if has_inbound and has_outbound:
            return ChatRelaySupport.BIDIRECTIONAL
        if has_inbound:
            return ChatRelaySupport.INBOUND
        if has_outbound:
            return ChatRelaySupport.OUTBOUND
        return ChatRelaySupport.NONE

    @property
    def supports_chat_relay(self) -> bool:
        return self.chat_relay_support is not ChatRelaySupport.NONE

    @property
    def supports_inbound_chat_relay(self) -> bool:
        return self.chat_relay_support.supports_inbound

    @property
    def supports_outbound_chat_relay(self) -> bool:
        return self.chat_relay_support.supports_outbound

    @property
    def supports_relay_system_notices(self) -> bool:
        return self.supports_inbound_chat_relay

    @property
    def has_mod_manager(self) -> Mod_Manager:
        if self.mods:
            return self.mods
        else:
            raise _errors.UnsupportedModManager(self.friendly)

    @abstractmethod
    async def start(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> bool:
        raise NotImplementedError

    async def player_count(self) -> tuple[int, int] | None:
        return None

    async def _tee(self, stream: IO[str] | None, dest: Path, label: str):
        if not stream:
            return
        with dest.open("w") as f:
            while line := await asyncio.to_thread(stream.readline):
                if not line:
                    break
                f.write(line)
                f.flush()
                if not config.SILENT_DEBUG:
                    log.debug(f"{label}: {line.strip()}")

    async def _launch_process(self):
        try:
            self.process = subprocess.Popen(
                self.cmd_start,
                cwd=self.cmd_cwd or self.directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                start_new_session=True,
                text=True,
                encoding=config.STR_ENCODE,
                shell=self.shell,
            )
            self._stderr_task = asyncio.create_task(self._tee(self.process.stderr, self.file_errout, "STDERR"))
        except Exception:
            log.exception(f"Failed to launch: {self.name}")

    async def _prelaunch_tasks(self):
        self.act_err_counts = {}

    async def _postlaunch_tasks(self): ...

    async def _std_launch(self):
        await self._prelaunch_tasks()
        await self._launch_process()
        await self._postlaunch_tasks()

    async def _drain_stderr_task(self, timeout_seconds: float = 1.0) -> None:
        task = self._stderr_task
        if task is None or task.done():
            return

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        except asyncio.TimeoutError:
            log.warning(f"{self.name} stderr reader did not finish in time; cancelling it.")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _terminate(self):
        if self.process is None and not self.proc_name:
            log.info(f"{self.name} already terminated, skipping.")
            return
        if self.process:
            log.info(f"Terminating {self.name} via stored process")

            try:
                self.process.terminate()
                await asyncio.to_thread(self.process.wait, 5)
                await self._drain_stderr_task()
            except Exception as xcp:
                log.exception(f"Termination failed: {xcp}")

            for _ in range(10):
                if self.process.poll() is not None:
                    break
                await asyncio.sleep(0.3)
            else:
                try:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait, 5)
                    await self._drain_stderr_task()
                    log.warning(f"{self.name} kill escalation")
                except Exception as xcp:
                    log.exception(f"Kill escalation failed: {xcp}")
            self.process = None

        if not self.proc_name:
            log.warning("No process name specified for process scan")
            return

        log.info(f"Scanning for leftover {self.proc_name} processes")
        for proc in psutil.process_iter(attrs=["name", "pid", "cmdline"]):
            try:
                name = proc.info["name"].lower()
                cmdline = proc.info.get("cmdline") or []
                cmdline_strs = [arg.lower() for arg in cmdline]

                if self.proc_name in name and all(
                    cmd_part in arg for arg in cmdline_strs for cmd_part in self.proc_cmd
                ):
                    log.info(f"Force-stopping stray process: {proc.info}")
                    proc.terminate()
                    proc.wait(timeout=10)
                    os.kill(proc.info["pid"], signal.SIGKILL)

                    await asyncio.sleep(0.5)

                subprocess.run(["pkill", "-f", self.proc_name])

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as xcp:
                log.exception(f"Failed to stop {proc.info}: {xcp}")

    def check_running(self) -> bool:
        return bool(self.process) and self.process.poll() is None

    def __str__(self) -> str:
        if self.directory.name != self.scope:
            house = f"{self.directory.name}[{self.scope}]"
        else:
            house = self.scope
        return f"<App {self.name} @ {house} | {self.cfg.enabled_txt}>"

    def __repr__(self) -> str:
        return self.__str__()

    def _simple_str(self) -> str:
        if self.directory.name != self.scope:
            house = f"{self.directory.name}[{self.scope}]"
        else:
            house = self.scope
        return f"<{house}.{self.friendly}>"


# AiviA APasz
