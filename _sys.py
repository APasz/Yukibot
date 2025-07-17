import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import hikari
import lightbulb
import psutil

import config
from _manager import App_Manager
from config import Singleton

log = logging.getLogger(__name__)


class Stats_CPU:
    def __init__(self):
        self.last_updated: datetime | None = None
        self.total: float = 0.0
        self.per_core: list[float] = []

    def update(self):
        self.total = psutil.cpu_percent(interval=None)
        self.per_core = psutil.cpu_percent(interval=None, percpu=True)
        self.last_updated = datetime.now()

    @property
    def r_total(self) -> int:
        return round(self.total)

    @property
    def r_per_core(self) -> list[int]:
        return [round(c) for c in self.per_core]


class Stats_RAM:
    def __init__(self):
        self.last_updated: datetime | None = None
        self.raw = psutil.virtual_memory()
        self.swap = psutil.swap_memory()

    def update(self):
        self.raw = psutil.virtual_memory()
        self.swap = psutil.swap_memory()
        self.last_updated = datetime.now()

    @property
    def used(self) -> int:
        return self.raw.used

    @property
    def percent(self) -> int:
        return round(self.raw.percent)

    @property
    def swap_percent(self) -> int:
        return round(self.swap.percent)


class Stats_Disk:
    def __init__(self, path: Path = Path.cwd()):
        self.path = path
        self.usage = psutil.disk_usage(str(self.path))

    def update(self):
        self.usage = psutil.disk_usage(str(self.path))

    @property
    def percent(self) -> int:
        return round(self.usage.percent)


class Stats_System(metaclass=Singleton):
    def __init__(self):
        self.cpu = Stats_CPU()
        self.ram = Stats_RAM()
        self.disk = Stats_Disk()

    def update(self):
        self.cpu.update()
        self.ram.update()
        self.disk.update()


async def restart(ctx: lightbulb.Context, bot: hikari.GatewayBot, manager: App_Manager, restart_type: str):
    restart_type = restart_type.strip().lower()
    restart_sys = True if restart_type == "system" else False

    try:
        await manager.end()
    except Exception:
        log.warning("Manager shutdown failed", exc_info=True)

    if me := bot.get_me():
        bot_name = me.display_name
    else:
        bot_name = config.NAME

    await bot.update_presence(
        activity=hikari.Activity(name=f"!!! Restarting {restart_type}", type=hikari.ActivityType.CUSTOM),
        status=hikari.Status.DO_NOT_DISTURB,
    )

    await ctx.respond(f"{bot_name} restarting {restart_type}")
    await asyncio.sleep(0.1)

    try:
        if restart_sys:
            code = os.system("sudo systemctl reboot -i")
            log.info(f"Restart CMD {code=}")
        sys.exit(1)
    except Exception:
        log.exception(f"Failed to reboot {restart_type}")
        await ctx.respond(f"unable to {'restart' if restart_sys else 'crash'}")


# AiviA APasz
