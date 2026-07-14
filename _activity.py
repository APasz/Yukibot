import math
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from logging import Logger
from pathlib import Path
from typing import LiteralString, override

import hikari
from hikari.impl.gateway_bot import GatewayBot

import config
from _sys import Stats_CPU, Stats_Disk, Stats_RAM, Stats_System

log: Logger = logging.getLogger(name=__name__)


class Provider_RAM(config.Activity_Provider):
    activity_field = config.DiscordActivityField.RAM

    def __init__(self, stats: Stats_System) -> None:
        self.ram: Stats_RAM = stats.ram
        self.prio: int = 0
        super().__init__()

    @override
    async def get(self) -> str:
        return f"{self.ram.percent}({self.ram.swap_percent})"


class Provider_CPU(config.Activity_Provider):
    activity_field = config.DiscordActivityField.CPU

    def __init__(self, stats: Stats_System) -> None:
        self.cpu: Stats_CPU = stats.cpu
        self.prio: int = 2
        super().__init__()

    @override
    async def get(self) -> str:
        bangs: LiteralString = "!" * sum(c >= 90 for c in self.cpu.r_per_core)
        return f"{self.cpu.r_total}{bangs}"


class Provider_DISK(config.Activity_Provider):
    activity_field = config.DiscordActivityField.DISK_ALERT

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
    def __init__(
        self,
        bot: hikari.GatewayBot,
        providers: list[config.Activity_Provider],
        *,
        activity_settings: config.DiscordActivitySettings | None = None,
    ) -> None:
        self.bot: GatewayBot = bot
        self.providers: dict[type[config.Activity_Provider], config.Activity_Provider] = {
            p.__class__: p for p in providers
        }
        self.last_update: datetime | None = None
        self.fail_count: int = 0
        self.state: str | None = None
        self.silent: bool = config.SILENT_DEBUG
        self._rotation_unit_index: int = 0
        self._rotation_target_name_provider: Callable[[], str | None] | None = None
        self.activity_settings: config.DiscordActivitySettings = (
            self._load_activity_settings() if activity_settings is None else activity_settings
        )

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

    @staticmethod
    def _load_activity_settings() -> config.DiscordActivitySettings:
        try:
            return config.load_bot_configuration(Path("configuration.json")).discord_settings.activity
        except Exception as xcp:
            log.warning("Activity settings load failed; using defaults: %s", xcp)
            return config.DiscordActivitySettings()

    @staticmethod
    def _provider_activity_field(provider: config.Activity_Provider) -> config.DiscordActivityField | None:
        field = provider.activity_field
        if isinstance(field, config.DiscordActivityField):
            return field
        return None

    def _build_state(
        self,
        *,
        field_statuses: dict[config.DiscordActivityField, str],
        unmapped_statuses: list[str],
    ) -> str:
        ordered_statuses = [
            field_statuses[field]
            for field in self.activity_settings.fields
            if field in field_statuses and field_statuses[field].strip()
        ]
        ordered_statuses.extend(status for status in unmapped_statuses if status.strip())
        if ordered_statuses:
            core_text = self.activity_settings.separator.join(ordered_statuses)
            return f"{self.activity_settings.prefix}{core_text}{self.activity_settings.suffix}"[:127]
        return self.activity_settings.fallback_text[:127]

    @override
    def set_activity_settings(self, settings: config.DiscordActivitySettings) -> None:
        self.activity_settings = settings

    @override
    def set_rotation_target_name_provider(self, provider: Callable[[], str | None] | None) -> None:
        self._rotation_target_name_provider = provider

    @override
    def current_rotation_target_name(self) -> str | None:
        if self._rotation_target_name_provider is None:
            return None
        target_name = self._rotation_target_name_provider()
        if target_name is None:
            return None
        normalised_target_name = target_name.strip()
        return normalised_target_name or None

    def _provider_matches_rotation_target(self, provider: config.Activity_Provider) -> bool:
        scope_name = provider.activity_scope_name
        if scope_name is None:
            return True
        normalised_scope_name = scope_name.strip()
        if not normalised_scope_name:
            return True
        target_name = self.current_rotation_target_name()
        return target_name is not None and target_name.casefold() == normalised_scope_name.casefold()

    @override
    async def refresh(self) -> None:
        self.last_update = None
        await self.update()

    @property
    def refresh_interval_seconds(self) -> int:
        return self.activity_settings.refresh_interval_seconds

    @override
    def current_rotation_slot(self, app_count: int) -> tuple[int, bool]:
        if app_count <= 0:
            raise ValueError("app_count must be positive.")

        units_per_app = self.activity_settings.units_per_app
        cycle_unit_index = self._rotation_unit_index % (app_count * units_per_app)
        app_index, unit_index = divmod(cycle_unit_index, units_per_app)
        alt_units = math.ceil(units_per_app * (self.activity_settings.alt_text_percentage / 100))
        show_alt_text = alt_units > 0 and unit_index >= units_per_app - alt_units
        return (app_index, show_alt_text)

    async def update(self) -> None:
        if config.IS_RESTARTING:
            return
        now: datetime = datetime.now(tz=timezone.utc)
        if self.last_update is not None and now - self.last_update < timedelta(seconds=self.refresh_interval_seconds):
            return

        self.last_update = now

        field_statuses: dict[config.DiscordActivityField, str] = {}
        unmapped_statuses: list[str] = []
        for provider in self.ordered_providers:
            if not config.SILENT_DEBUG:
                log.debug(f"AM.update: provider={provider.__class__}")
            try:
                if not self._provider_matches_rotation_target(provider):
                    if not config.SILENT_DEBUG:
                        log.debug(
                            "AM.update: provider=%s skipped for rotation target %s",
                            provider.__class__.__name__,
                            self.current_rotation_target_name(),
                        )
                    continue
                if status := await provider.get():
                    if not config.SILENT_DEBUG:
                        log.debug("AM.update: provider=%s status=%s", provider.__class__.__name__, status)
                    activity_field = self._provider_activity_field(provider)
                    if activity_field is None:
                        unmapped_statuses.append(status)
                    else:
                        field_statuses[activity_field] = status
            except Exception:
                log.exception(f"Provider {provider} failed")

        self._rotation_unit_index += 1
        new_state = self._build_state(field_statuses=field_statuses, unmapped_statuses=unmapped_statuses)
        if new_state == self.state:
            return
        self.state = new_state
        if not self.silent:
            log.debug("New activity: mapped=%s unmapped=%s", field_statuses, unmapped_statuses)
        try:
            if not self.silent:
                log.debug("AM.update_presence: %s", new_state)
            await self.bot.update_presence(activity=hikari.Activity(name=new_state, type=hikari.ActivityType.CUSTOM))
        except Exception as xcp:
            log.exception(f"BotPresence: {xcp}")


# AiviA APasz
