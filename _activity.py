import asyncio
import logging
from datetime import datetime, timedelta, timezone
from logging import Logger
from typing import LiteralString, override

import hikari
from hikari.impl.gateway_bot import GatewayBot

import config
from _sys import Stats_CPU, Stats_Disk, Stats_RAM, Stats_System

log: Logger = logging.getLogger(name=__name__)


class Provider_RAM(config.Activity_Provider):
    def __init__(self, stats: Stats_System) -> None:
        self.ram: Stats_RAM = stats.ram
        self.prio: int = 0
        super().__init__()

    @override
    async def get(self) -> str:
        return f"{self.ram.percent}({self.ram.swap_percent})"


class Provider_CPU(config.Activity_Provider):
    def __init__(self, stats: Stats_System) -> None:
        self.cpu: Stats_CPU = stats.cpu
        self.prio: int = 2
        super().__init__()

    @override
    async def get(self) -> str:
        bangs: LiteralString = "!" * sum(c >= 90 for c in self.cpu.r_per_core)
        return f"{self.cpu.r_total}{bangs}"


class Provider_DISK(config.Activity_Provider):
    def __init__(self, stats: Stats_System) -> None:
        self.stats: Stats_System = stats
        self.prio: int = 80
        super().__init__()

    @override
    async def get(self) -> str | None:
        disks: tuple[Stats_Disk, ...] = self.stats.activity_disks
        if not disks:
            return None

        hottest_disk: Stats_Disk = max(disks, key=lambda disk: disk.percent)
        if hottest_disk.percent >= 90:
            return f"{hottest_disk.display_name} @ {hottest_disk.percent}"
        return None


class Activity_Manager(config.Activity_Manager):
    def __init__(self, bot: hikari.GatewayBot, providers: list[config.Activity_Provider]) -> None:
        self.bot: GatewayBot = bot
        self.providers: dict[type[config.Activity_Provider], config.Activity_Provider] = {
            p.__class__: p for p in providers
        }
        self.last_update: datetime = datetime.now(tz=timezone.utc)
        self.fail_count: int = 0
        self.state: str | None = None
        self.silent: bool = config.SILENT_DEBUG

    @override
    def register(self, provider: config.Activity_Provider) -> None:
        self.providers[provider.__class__] = provider

    @override
    def deregister(self, provider: config.Activity_Provider) -> None:
        if provider.__class__ in self.providers:
            del self.providers[provider.__class__]

    @property
    def ordered_providers(self) -> list[config.Activity_Provider]:
        return sorted(self.providers.values(), key=lambda obj: obj.prio)

    async def update(self) -> None:
        if config.IS_RESTARTING:
            return
        now: datetime = datetime.now(tz=timezone.utc)
        if now - self.last_update < timedelta(seconds=2):
            self.fail_count += 1
            log.error("Task going too fast, probably broken.")
            if self.fail_count >= 5:
                log.warning("Too many failures. Sleeping for recovery...")
                await asyncio.sleep(30)
                self.fail_count = 0
            else:
                await asyncio.sleep(5)
            return

        self.last_update = now

        statuses: list[str] = []
        for provider in self.ordered_providers:
            if not config.SILENT_DEBUG:
                log.debug(f"AM.update: provider={provider.__class__}")
            try:
                if status := await provider.get():
                    if not config.SILENT_DEBUG:
                        log.debug("AM.update: provider=%s status=%s", provider.__class__.__name__, status)
                    statuses.append(status)
            except Exception:
                log.exception(f"Provider {provider} failed")

        new_state: str = " | ".join(statuses)[:127]
        if new_state == self.state:
            return
        self.state = new_state
        if not self.silent:
            log.debug(f"New activity: {statuses}")
        try:
            if not self.silent:
                log.debug("AM.update_presence: %s", new_state)
            await self.bot.update_presence(activity=hikari.Activity(name=new_state, type=hikari.ActivityType.CUSTOM))
        except Exception as xcp:
            log.exception(f"BotPresence: {xcp}")


# AiviA APasz
