from __future__ import annotations
from abc import abstractmethod
import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path

import hikari
import psutil


import _errors
from apps._settings import App_Settings, Settings_Manager
from config import Activity_Manager
from apps._updater import Update_Manager
import config
from apps._config import App_Config, Mod_Config
from apps._mod import Mod, Mod_Manager

from typing import IO, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from _discord import App_Bound

log = logging.getLogger(__name__)


class AM_Receiver(Protocol):
    async def send(self, payload: App_Bound) -> None: ...


class App:
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
    cmd_start: list[str]
    cmd_cwd: Path | None = None
    shell: bool = False
    _stderr_task = None
    _running: bool = False
    chat_channel: hikari.Snowflake | None = None
    activity_manager: Activity_Manager
    providers: list[config.Activity_Provider]

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
            raise ValueError("App missing bot")
        if not cfg:
            raise ValueError("App missing instance configuration")
        self.bot = bot
        self.cfg = cfg
        self.name = cfg.name
        self.friendly = cfg.friendly_name or cfg.name.title()
        self.scope = cfg.scope
        self.directory = cfg.directory
        self.chat_channel = hikari.Snowflake(cfg.chat_channel) if cfg.chat_channel else None
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

    async def _terminate(self):
        if self.process is None and not self.proc_name:
            log.info(f"{self.name} already terminated, skipping.")
            return
        if self.process:
            log.info(f"Terminating {self.name} via stored process")

            try:
                self.process.terminate()
                self.process.wait(timeout=5)
                if self._stderr_task:
                    await self._stderr_task
            except Exception as xcp:
                log.exception(f"Termination failed: {xcp}")

            for _ in range(10):
                if self.process.poll() is not None:
                    break
                await asyncio.sleep(0.3)
            else:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
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
