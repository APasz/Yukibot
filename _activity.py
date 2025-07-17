import asyncio
import logging
from datetime import datetime, timedelta, timezone

import hikari

import config
from _sys import Stats_System

log = logging.getLogger(__name__)


class Provider_RAM(config.Activity_Provider):
    def __init__(self, stats: Stats_System):
        self.ram = stats.ram
        self.prio = 0
        super().__init__()

    async def get(self) -> str:
        return f"{self.ram.percent}({self.ram.swap_percent})"


class Provider_CPU(config.Activity_Provider):
    def __init__(self, stats: Stats_System):
        self.cpu = stats.cpu
        self.prio = 2
        super().__init__()

    async def get(self) -> str:
        bangs = "!" * sum(c >= 90 for c in self.cpu.r_per_core)
        return f"{self.cpu.r_total}{bangs}"


class Provider_DISK(config.Activity_Provider):
    def __init__(self, stats: Stats_System):
        self.disk = stats.disk
        self.prio = 80
        super().__init__()

    async def get(self) -> str | None:
        if (percent := self.disk.percent) >= 90:
            return f"disk @ {percent}"
        return None


class Activity_Manager(config.Activity_Manager):
    def __init__(self, bot: hikari.GatewayBot, providers: list[config.Activity_Provider]):
        self.bot = bot
        self.providers = {p.__class__: p for p in providers}
        self.last_update = datetime.now(timezone.utc)
        self.fail_count = 0
        self.state = None
        self.silent = config.SILENT_DEBUG

    def register(self, provider: config.Activity_Provider):
        self.providers[provider.__class__] = provider

    def deregister(self, provider: config.Activity_Provider):
        if provider.__class__ in self.providers:
            del self.providers[provider.__class__]

    @property
    def ordered_providers(self) -> list[config.Activity_Provider]:
        return sorted(self.providers.values(), key=lambda obj: obj.prio)

    async def update(self):
        now = datetime.now(timezone.utc)
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

        statuses = []
        for provider in self.ordered_providers:
            if not config.SILENT_DEBUG:
                log.debug(f"AM.update: provider={provider.__class__}")
            try:
                if status := await provider.get():
                    statuses.append(status)
            except Exception:
                log.exception(f"Provider {provider} failed")

        new_state = " | ".join(statuses)[:127]
        if new_state == self.state:
            return
        self.state = new_state
        if not self.silent:
            log.debug(f"New activity: {statuses}")
        try:
            await self.bot.update_presence(activity=hikari.Activity(name=new_state, type=hikari.ActivityType.CUSTOM))
        except Exception as xcp:
            log.exception(f"BotPresence: {xcp}")


# AiviA APasz
