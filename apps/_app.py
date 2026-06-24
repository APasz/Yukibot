from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any, Callable, ClassVar, Generic, Protocol, TypeVar, cast

import hikari
import psutil
from hikari.snowflakes import Snowflake

import _errors
import config
from _security import Power_Level
from apps._blueprint_files import AppBlueprintEntry
from apps._config import App_Config, AppVersion, Mod_Config, RelayChannelSource, normalise_app_version
from apps._config_files import (
    AppConfigFile,
    AppConfigFileContent,
    AppConfigFileRoot,
    effective_config_root_read_level,
    list_app_config_files,
    read_app_config_file,
    resolve_app_config_path,
    resolve_app_config_root,
    write_app_config_file,
)
from apps._console import ConsoleAction
from apps._mod import Mod, Mod_Manager
from apps._save_files import AppSaveEntry, AppSaveRoot, list_app_save_files, resolve_app_save_path
from apps._settings import App_Settings, Settings_Manager
from apps._updater import AppUpdateInfo, AppUpdateStatus, Update_Manager
from config import Activity_Manager, Name_Cache

log = logging.getLogger(__name__)

ConfigT = TypeVar("ConfigT", bound=App_Config, default=Any)
AppProviderT = TypeVar("AppProviderT", bound="App[Any]")


class AM_Receiver(Protocol):
    async def send(self, payload: Any) -> None: ...


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
            return "Chat -> Game"
        if self is ChatRelaySupport.OUTBOUND:
            return "Game -> Chat"
        return "Game <-> Chat"

    @property
    def display_value(self) -> str:
        if self is ChatRelaySupport.NONE:
            return "Unsupported"
        if self is ChatRelaySupport.INBOUND:
            return "Chat -> Game"
        if self is ChatRelaySupport.OUTBOUND:
            return "Game -> Chat"
        return "Game <-> Chat"

    @property
    def supports_inbound(self) -> bool:
        return self in {ChatRelaySupport.INBOUND, ChatRelaySupport.BIDIRECTIONAL}

    @property
    def supports_outbound(self) -> bool:
        return self in {ChatRelaySupport.OUTBOUND, ChatRelaySupport.BIDIRECTIONAL}


@dataclass(frozen=True, slots=True)
class RelayAdvancementTerms:
    singular: str = "Advancement"
    plural: str = "Advancements"

    def __post_init__(self) -> None:
        if not self.singular.strip():
            raise ValueError("Relay advancement singular term must not be empty.")
        if not self.plural.strip():
            raise ValueError("Relay advancement plural term must not be empty.")


class AppRuntimeFaultKind(enum.StrEnum):
    CRASH = "crash"


@dataclass(frozen=True, slots=True)
class AppRuntimeFault:
    kind: AppRuntimeFaultKind
    summary: str | None = None

    def __post_init__(self) -> None:
        if self.summary is not None and not self.summary.strip():
            raise ValueError("App runtime fault summary must not be blank.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "AppRuntimeFault":
        raw_kind: object | None = payload.get("kind")
        raw_summary: object | None = payload.get("summary")
        if not isinstance(raw_kind, str):
            raise ValueError("App runtime fault kind is invalid.")
        if raw_summary is not None and not isinstance(raw_summary, str):
            raise ValueError("App runtime fault summary is invalid.")
        try:
            kind: AppRuntimeFaultKind = AppRuntimeFaultKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("App runtime fault kind is invalid.") from xcp
        return cls(kind=kind, summary=raw_summary)

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class AppStdoutTail:
    lines: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class AppActivityProviderMetadata:
    provider_id: str
    label: str

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("App activity provider id must not be empty.")
        if not self.label.strip():
            raise ValueError("App activity provider label must not be empty.")


class AppActivityProvider(ABC, Generic[AppProviderT]):
    metadata: ClassVar[AppActivityProviderMetadata]
    silent: bool = config.SILENT_DEBUG
    prio: int = 50
    activity_field: config.DiscordActivityField | None = None
    activity_scope_name: str | None = None
    task_funcs: tuple[Callable[[], Coroutine[object, object, None]], ...]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls is AppActivityProvider:
            return
        metadata = getattr(cls, "metadata", None)
        if not isinstance(metadata, AppActivityProviderMetadata):
            raise TypeError(f"{cls.__name__} must define AppActivityProviderMetadata in metadata.")

    def __init__(self, app: AppProviderT) -> None:
        self.app: AppProviderT = app
        self.activity_scope_name = getattr(app, "name", None)
        self.task_funcs = tuple(getattr(self, "task_funcs", ()))

    @property
    def provider_id(self) -> str:
        return self.metadata.provider_id

    @property
    def label(self) -> str:
        return self.metadata.label

    @abstractmethod
    async def get(self) -> str | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class AppActivityProviderEntry:
    provider_id: str
    label: str
    enabled: bool
    current_value: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("App activity provider id must not be empty.")
        if not self.label.strip():
            raise ValueError("App activity provider label must not be empty.")
        if self.current_value is not None and not self.current_value.strip():
            raise ValueError("App activity provider current value must not be blank.")


class App(Generic[ConfigT], ABC):
    cfg_cls: type[ConfigT] = cast(type[ConfigT], App_Config)
    bot: hikari.GatewayBot
    cfg: ConfigT
    file_instances: Path
    name: str
    friendly: str
    scope: str
    name_platforms: tuple[str, ...] = ()
    preferred_name_platform: str | None = None
    proc_name: str
    proc_cmd: list[str]
    directory: Path
    dir_log: Path
    server_log: Path | None
    mods: Mod_Manager | None = None
    settings: Settings_Manager | None
    saves = None
    updater: Update_Manager | None = None
    process: subprocess.Popen[Any] | None = None
    file_stdout: Path
    file_errout: Path
    act_err_counts: dict[str, int] = {}
    act_err_threshold: int = 25
    name_cache: Name_Cache = config.Name_Cache()
    am_receiver: "AM_Receiver | None" = None
    chat_relay_outbound: bool = False
    cmd_start: list[str]
    cmd_cwd: Path | None = None
    shell: bool = False
    _stderr_task = None
    _running: bool = False
    chat_channel: hikari.Snowflake | None = None
    chat_channels: tuple[hikari.Snowflake, ...] = ()
    chat_channel_override: hikari.Snowflake | None = None
    chat_channel_overrides: tuple[hikari.Snowflake, ...] = ()
    chat_channel_source: RelayChannelSource = RelayChannelSource.NONE
    activity_manager: Activity_Manager
    providers: list[AppActivityProvider[Any]]
    manage_embed_color: int = 0x96212B
    relay_advancement_terms: RelayAdvancementTerms = RelayAdvancementTerms()
    relay_notice_player_session_supported: bool = False
    relay_notice_player_death_supported: bool = False
    relay_notice_progress_supported: bool = False
    _instance_config_change_handler: Callable[["App"], None] | None = None
    lifecycle_started_at: datetime | None = None
    runtime_fault: AppRuntimeFault | None = None
    config_file_read_level_override: Power_Level | None = None
    config_file_write_level_override: Power_Level | None = None
    save_file_write_level_override: Power_Level | None = None

    def __init__(
        self,
        bot: hikari.GatewayBot,
        activity_manager: Activity_Manager,
        cfg: ConfigT,
        stg: App_Settings | None = None,
        mod_cls: type[Mod] | None = None,
        modcf_cls: type[Mod_Config] | None = None,
    ) -> None:
        if not bot:
            raise ValueError("App missing bot")  # pyright: ignore[reportUnreachable]
        if not cfg:
            raise ValueError("App missing instance configuration")  # pyright: ignore[reportUnreachable]
        self.bot = bot
        self.cfg = cfg
        self.file_instances = cfg.apps_dir / "instances.json"
        self.name = cfg.name
        self.friendly = cfg.friendly_name or cfg.name.title()
        self.scope = cfg.scope
        configured_platforms = tuple(str(platform).strip().lower() for platform in self.name_platforms if str(platform).strip())
        if cfg.steam_update is not None and "steam" not in configured_platforms:
            configured_platforms = (*configured_platforms, "steam")
        self.name_platforms = configured_platforms
        if self.preferred_name_platform is None and cfg.steam_update is not None:
            self.preferred_name_platform = "steam"
        self.directory = cfg.directory
        self.chat_channel = hikari.Snowflake(cfg.chat_channel) if cfg.chat_channel else None
        self.chat_channels = tuple[Snowflake, ...](hikari.Snowflake(channel_id) for channel_id in cfg.chat_channels)
        self.chat_channel_override = hikari.Snowflake(cfg.chat_channel_override) if cfg.chat_channel_override else None
        self.chat_channel_overrides = tuple[Snowflake, ...](
            hikari.Snowflake(channel_id) for channel_id in cfg.chat_channel_overrides
        )
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
        self.lifecycle_started_at = None
        self.runtime_fault = None
        self.proc_name = getattr(self, "proc_name", "")
        self.proc_cmd = getattr(self, "proc_cmd", [])
        self.cmd_start = getattr(self, "cmd_start", [])
        self.config_file_read_level_override = cfg.config_file_read_level_override
        self.config_file_write_level_override = cfg.config_file_write_level_override
        self.save_file_write_level_override = cfg.save_file_write_level_override

        log.debug(f"{__name__} | {self.cmd_start=} @ {self.cmd_cwd=}")

    async def post_init(self) -> None:
        if self.mods:
            await self.mods.load_mods()
        log.debug(f"{self.name}.__post_init__")

    @property
    def chat_relay_support(self) -> ChatRelaySupport:
        has_inbound: bool = self.am_receiver is not None
        has_outbound: bool = self.chat_relay_outbound
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
    def manager_status_lines(self) -> tuple[str, ...]:
        return (
            f"scope: {self.scope}",
            f"version: {self.version_display}",
        )

    @property
    def version_display(self) -> str:
        if self.updater and self.updater.version is not None:
            return self.updater.stringise(self.updater.version)
        if self.cfg.version is not None:
            return self.cfg.version.display_value
        return "none"

    @property
    def instance_config_overrides(self) -> Mapping[str, object]:
        overrides: dict[str, object] = {}
        if self.cfg.friendly_name is not None:
            overrides["friendly_name"] = self.cfg.friendly_name
        if self.cfg.notes is not None:
            overrides["notes"] = self.cfg.notes
        if not self.cfg.lifecycle_notice_started:
            overrides["lifecycle_notice_started"] = self.cfg.lifecycle_notice_started
        if not self.cfg.lifecycle_notice_stopped:
            overrides["lifecycle_notice_stopped"] = self.cfg.lifecycle_notice_stopped
        if not self.cfg.lifecycle_notice_crashed:
            overrides["lifecycle_notice_crashed"] = self.cfg.lifecycle_notice_crashed
        if self.relay_notice_player_session_enabled is False:
            overrides["relay_notice_player_session"] = False
        if self.relay_notice_player_death_enabled is False:
            overrides["relay_notice_player_death"] = False
        if self.relay_notice_progress_enabled is False:
            overrides["relay_notice_progress"] = False
        if self.relay_advancements_enabled is False:
            overrides["relay_advancements"] = False
        if self.cfg.disabled_activity_provider_ids:
            overrides["disabled_activity_provider_ids"] = list(self.cfg.disabled_activity_provider_ids)
        if self.cfg.version is not None:
            overrides["version"] = self.cfg.version.model_dump(mode="json", exclude_none=True)
        if self.cfg.steam_update is not None:
            overrides["steam_update"] = self.cfg.steam_update.model_dump(mode="json", exclude_none=True)
        if self.config_file_read_level_override is not None:
            overrides["config_file_read_level_override"] = self.config_file_read_level_override.name
        if self.config_file_write_level_override is not None:
            overrides["config_file_write_level_override"] = self.config_file_write_level_override.name
        if self.save_file_write_level_override is not None:
            overrides["save_file_write_level_override"] = self.save_file_write_level_override.name
        return overrides

    @property
    def update_info(self) -> AppUpdateInfo | None:
        if self.updater is None:
            return None
        return self.updater.info()

    @property
    def update_status(self) -> AppUpdateStatus | None:
        if self.updater is None:
            return None
        return self.updater.status()

    @property
    def supports_update_tab(self) -> bool:
        return self.update_info is not None

    def detect_installed_version(self) -> AppVersion | None:
        return None

    def apply_version(self, version: AppVersion | str | None, *, persist: bool) -> bool:
        normalised_version = normalise_app_version(version)
        if normalised_version is None or self.cfg.version == normalised_version:
            return False
        self.cfg.version = normalised_version
        if persist:
            self.persist_instance_config_overrides()
        return True

    def set_instance_config_change_handler(self, handler: Callable[["App"], None] | None) -> None:
        self._instance_config_change_handler = handler

    def persist_instance_config_overrides(self) -> None:
        if self._instance_config_change_handler is None:
            return
        self._instance_config_change_handler(self)

    def set_activity_providers(self, providers: Sequence[AppActivityProvider[Any]]) -> None:
        self.providers = list(providers)

    @property
    def activity_providers(self) -> tuple[AppActivityProvider[Any], ...]:
        app_providers = getattr(self, "providers", None)
        if isinstance(app_providers, list | tuple):
            return tuple(cast(AppActivityProvider[Any], provider) for provider in app_providers)
        return ()

    @classmethod
    def activity_provider_id(cls, provider: AppActivityProvider[Any]) -> str:
        return provider.provider_id

    @classmethod
    def activity_provider_label(cls, provider: AppActivityProvider[Any]) -> str:
        return provider.label

    @property
    def disabled_activity_provider_ids(self) -> tuple[str, ...]:
        configured_ids = getattr(self.cfg, "disabled_activity_provider_ids", ())
        known_ids = {self.activity_provider_id(provider).casefold() for provider in self.activity_providers}
        return tuple(
            provider_id
            for provider_id in configured_ids
            if isinstance(provider_id, str) and provider_id.casefold() in known_ids
        )

    def activity_provider_enabled(self, provider: AppActivityProvider[Any]) -> bool:
        provider_id = self.activity_provider_id(provider)
        return provider_id.casefold() not in {item.casefold() for item in self.disabled_activity_provider_ids}

    @property
    def activity_provider_entries(self) -> tuple[AppActivityProviderEntry, ...]:
        return tuple(
            AppActivityProviderEntry(
                provider_id=self.activity_provider_id(provider),
                label=self.activity_provider_label(provider),
                enabled=self.activity_provider_enabled(provider),
            )
            for provider in self.activity_providers
        )

    async def activity_provider_entries_with_values(self) -> tuple[AppActivityProviderEntry, ...]:
        base_entries = self.activity_provider_entries
        if not base_entries or not self.check_running():
            return base_entries
        current_values = await asyncio.gather(
            *(self._activity_provider_current_value(provider) for provider in self.activity_providers)
        )
        return tuple(
            AppActivityProviderEntry(
                provider_id=entry.provider_id,
                label=entry.label,
                enabled=entry.enabled,
                current_value=current_value,
            )
            for entry, current_value in zip(base_entries, current_values, strict=True)
        )

    async def _activity_provider_current_value(self, provider: AppActivityProvider[Any]) -> str | None:
        if not self.activity_provider_enabled(provider):
            return None
        try:
            raw_value = await provider.get()
        except Exception as xcp:
            log.warning(
                "App activity provider value failed: app=%s provider=%s error=%s",
                self.name,
                provider.provider_id,
                xcp,
            )
            return None
        if raw_value is None:
            return None
        current_value = raw_value.strip()
        return current_value or None

    def register_enabled_activity_providers(self) -> None:
        for provider in self.activity_providers:
            if self.activity_provider_enabled(provider):
                self.activity_manager.register(provider)
            else:
                self.activity_manager.deregister(provider)

    def deregister_activity_providers(self) -> None:
        for provider in self.activity_providers:
            self.activity_manager.deregister(provider)

    def apply_disabled_activity_provider_ids(self, provider_ids: tuple[str, ...]) -> None:
        next_disabled_ids = tuple(provider_ids)
        current_entries = {entry.provider_id.casefold(): entry for entry in self.activity_provider_entries}
        unknown_provider_ids = [
            provider_id for provider_id in next_disabled_ids if provider_id.casefold() not in current_entries
        ]
        if unknown_provider_ids:
            raise ValueError(f"Unknown app activity providers: {', '.join(unknown_provider_ids)}.")
        self.cfg.disabled_activity_provider_ids = next_disabled_ids
        if not self.check_running():
            return
        self.register_enabled_activity_providers()

    @property
    def relay_advancements_enabled(self) -> bool | None:
        return None

    @property
    def supports_relay_advancements(self) -> bool:
        return self.relay_advancements_enabled is not None

    def apply_relay_advancements_enabled(self, enabled: bool) -> None:
        raise ValueError(f"{self.friendly} does not support {self.relay_advancement_term.lower()} relay.")

    @property
    def relay_notice_player_session_enabled(self) -> bool | None:
        if not self.relay_notice_player_session_supported:
            return None
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return True
        return cfg.relay_notice_player_session

    def apply_relay_notice_player_session_enabled(self, enabled: bool) -> None:
        if not self.relay_notice_player_session_supported:
            raise ValueError(f"{self.friendly} does not support player session notices.")
        self.cfg.relay_notice_player_session = enabled

    @property
    def relay_notice_player_joined_enabled(self) -> bool | None:
        return self.relay_notice_player_session_enabled

    def apply_relay_notice_player_joined_enabled(self, enabled: bool) -> None:
        self.apply_relay_notice_player_session_enabled(enabled)

    @property
    def relay_notice_player_left_enabled(self) -> bool | None:
        return self.relay_notice_player_session_enabled

    def apply_relay_notice_player_left_enabled(self, enabled: bool) -> None:
        self.apply_relay_notice_player_session_enabled(enabled)

    @property
    def relay_notice_player_death_enabled(self) -> bool | None:
        if not self.relay_notice_player_death_supported:
            return None
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return True
        return cfg.relay_notice_player_death

    def apply_relay_notice_player_death_enabled(self, enabled: bool) -> None:
        if not self.relay_notice_player_death_supported:
            raise ValueError(f"{self.friendly} does not support death notices.")
        self.cfg.relay_notice_player_death = enabled

    @property
    def relay_notice_progress_enabled(self) -> bool | None:
        if not self.relay_notice_progress_supported:
            return None
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return True
        return cfg.relay_notice_progress

    def apply_relay_notice_progress_enabled(self, enabled: bool) -> None:
        if not self.relay_notice_progress_supported:
            raise ValueError(f"{self.friendly} does not support {self.relay_progress_notice_term.lower()} notices.")
        self.cfg.relay_notice_progress = enabled

    @property
    def relay_progress_notice_term(self) -> str:
        return self.relay_advancement_term

    def clear_runtime_fault(self) -> bool:
        if getattr(self, "runtime_fault", None) is None:
            return False
        self.runtime_fault = None
        return True

    def record_runtime_fault(
        self,
        *,
        kind: AppRuntimeFaultKind,
        summary: str | None = None,
    ) -> bool:
        normalised_summary: str | None
        if summary is None:
            normalised_summary = None
        else:
            stripped_summary = summary.strip()
            normalised_summary = stripped_summary or None
        next_fault = AppRuntimeFault(kind=kind, summary=normalised_summary)
        if getattr(self, "runtime_fault", None) == next_fault:
            return False
        self.runtime_fault = next_fault
        return True

    @property
    def relay_advancement_term(self) -> str:
        return self.relay_advancement_terms.singular

    @property
    def relay_advancement_term_plural(self) -> str:
        return self.relay_advancement_terms.plural

    def lifecycle_relay_description_lines(
        self,
        *,
        started: bool,
        uptime: timedelta | None = None,
    ) -> tuple[str, ...]:
        del started, uptime
        return ()

    @property
    def public_map_url(self) -> str | None:
        return None

    @property
    def map_proxy_url(self) -> str | None:
        return self.public_map_url

    @property
    def map_proxy_root_path(self) -> Path | None:
        return None

    @property
    def supports_map(self) -> bool:
        return self.public_map_url is not None

    @property
    def map_annotations_path(self) -> Path:
        return self.directory / ".yukibot" / "map_annotations.json"

    @property
    def map_cache_path(self) -> Path:
        return self.directory / ".yukibot" / "map_cache.json"

    @property
    def has_mod_manager(self) -> Mod_Manager:
        if self.mods:
            return self.mods
        else:
            raise _errors.UnsupportedModManager(self.friendly)

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return ()

    def available_console_actions(self, actions: tuple[ConsoleAction, ...]) -> tuple[ConsoleAction, ...]:
        cfg = getattr(self, "cfg", None)
        app_version = normalise_app_version(getattr(cfg, "version", None))
        return tuple(action for action in actions if action.supports_app_version(app_version))

    @property
    def supports_console_actions(self) -> bool:
        return bool(self.console_actions)

    @property
    def supports_settings(self) -> bool:
        return self.settings is not None

    @property
    def highest_setting_power_level(self) -> Power_Level | None:
        if self.settings is None:
            return None
        return max((setting.power_level for setting in self.settings.app.options), default=None)

    @property
    def config_file_read_level(self) -> Power_Level:
        if self.config_file_read_level_override is not None:
            return self.config_file_read_level_override
        highest_setting_level = self.highest_setting_power_level
        if highest_setting_level is not None:
            return highest_setting_level
        return Power_Level.sudo

    @property
    def config_file_write_level(self) -> Power_Level:
        if self.config_file_write_level_override is not None:
            return self.config_file_write_level_override
        highest_setting_level = self.highest_setting_power_level
        if highest_setting_level is not None:
            return highest_setting_level
        return Power_Level.root

    @property
    def lowest_config_file_read_level(self) -> Power_Level:
        roots = self.config_file_roots
        if not roots:
            return self.config_file_read_level
        return min(
            effective_config_root_read_level(root=root, default=self.config_file_read_level) for root in roots
        )

    def config_file_read_level_for_id(self, file_id: str) -> Power_Level:
        root, _, _ = resolve_app_config_path(self.config_file_roots, file_id)
        return effective_config_root_read_level(root=root, default=self.config_file_read_level)

    def resolve_config_root(self, root_id: str) -> AppConfigFileRoot:
        return resolve_app_config_root(self.config_file_roots, root_id)

    def config_file_read_level_for_root(self, root_id: str) -> Power_Level:
        root = self.resolve_config_root(root_id)
        return effective_config_root_read_level(root=root, default=self.config_file_read_level)

    def settings_save_level(self, actor_user_id: int) -> Power_Level:
        if self.settings is None:
            return Power_Level.user
        return self.settings.required_save_level(actor_user_id)

    def settings_reload_level(self, actor_user_id: int) -> Power_Level:
        if self.settings is None:
            return Power_Level.user
        return self.settings.required_reload_level(actor_user_id)

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        return ()

    @property
    def supports_save_files(self) -> bool:
        return bool(self.save_file_roots)

    def list_save_files(self) -> tuple[AppSaveEntry, ...]:
        return list_app_save_files(self.save_file_roots)

    async def list_save_files_async(self) -> tuple[AppSaveEntry, ...]:
        return self.list_save_files()

    def resolve_save_file(self, file_id: str) -> Path:
        _root, path, _relative_path = resolve_app_save_path(self.save_file_roots, file_id)
        return path

    async def download_save_content(self, file_id: str) -> tuple[str, bytes] | None:
        del file_id
        return None

    @property
    def supports_save_uploads(self) -> bool:
        return False

    @property
    def supports_save_rename(self) -> bool:
        return False

    @property
    def supports_save_delete(self) -> bool:
        return False

    @property
    def save_file_write_level(self) -> Power_Level:
        if self.save_file_write_level_override is not None:
            return self.save_file_write_level_override
        return Power_Level.sudo

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        raise ValueError(f"{self.friendly} does not support save uploads.")

    async def upload_save_file_async(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        return self.upload_save_file(root_id=root_id, upload_name=upload_name, source_path=source_path)

    def relocate_save_file(
        self,
        *,
        save_id: str,
        destination_root_id: str,
        destination_relative_path: str,
    ) -> AppSaveEntry:
        raise ValueError(f"{self.friendly} does not support save relocation.")

    async def relocate_save_file_async(
        self,
        *,
        save_id: str,
        destination_root_id: str,
        destination_relative_path: str,
    ) -> AppSaveEntry:
        return self.relocate_save_file(
            save_id=save_id,
            destination_root_id=destination_root_id,
            destination_relative_path=destination_relative_path,
        )

    def delete_save_file(self, *, file_id: str) -> AppSaveEntry:
        raise ValueError(f"{self.friendly} does not support save deletion.")

    async def delete_save_file_async(self, *, file_id: str) -> AppSaveEntry:
        return self.delete_save_file(file_id=file_id)

    @property
    def supports_blueprints(self) -> bool:
        return False

    @property
    def default_blueprint_session_name(self) -> str | None:
        return None

    def list_blueprint_files(self) -> tuple[AppBlueprintEntry, ...]:
        raise ValueError(f"{self.friendly} does not support blueprint files.")

    def upload_blueprint_file(
        self,
        *,
        session_name: str,
        upload_name: str,
        source_path: Path,
        actor_user_id: int,
        config_upload_name: str | None = None,
        config_source_path: Path | None = None,
    ) -> AppBlueprintEntry:
        raise ValueError(f"{self.friendly} does not support blueprint uploads.")

    def delete_blueprint_file(
        self,
        *,
        file_id: str,
        actor_user_id: int,
        actor_is_sudo: bool,
    ) -> AppBlueprintEntry:
        raise ValueError(f"{self.friendly} does not support blueprint deletion.")

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return ()

    @property
    def supports_config_files(self) -> bool:
        return bool(self.config_file_roots)

    def list_config_files(self) -> tuple[AppConfigFile, ...]:
        return list_app_config_files(self.config_file_roots, default_read_level=self.config_file_read_level)

    def resolve_config_file(self, file_id: str) -> Path:
        _root, path, _relative_path = resolve_app_config_path(self.config_file_roots, file_id)
        return path

    def read_config_file(self, file_id: str) -> AppConfigFileContent:
        return read_app_config_file(self.config_file_roots, file_id, default_read_level=self.config_file_read_level)

    def write_config_file(self, file_id: str, content: str) -> AppConfigFileContent:
        return write_app_config_file(
            self.config_file_roots,
            file_id,
            content,
            default_read_level=self.config_file_read_level,
        )

    def read_stdout_tail(self, *, max_lines: int = 200) -> AppStdoutTail:
        if max_lines < 1:
            raise ValueError("Stdout tail line limit must be at least 1.")
        stdout_path: Path = self.file_stdout
        if not stdout_path.exists():
            return AppStdoutTail(lines=(), truncated=False)
        with stdout_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size: int = handle.tell()
            if file_size <= 0:
                return AppStdoutTail(lines=(), truncated=False)

            chunk_size = 8192
            position = file_size
            buffer = bytearray()
            newline_count = 0
            while position > 0 and newline_count <= max_lines:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                if not chunk:
                    break
                buffer[:0] = chunk
                newline_count = buffer.count(b"\n")

        decoded_lines = deque(
            buffer.decode(config.STR_ENCODE, errors="replace").splitlines(),
            maxlen=max_lines,
        )
        return AppStdoutTail(
            lines=tuple(decoded_lines),
            truncated=position > 0 or newline_count > max_lines,
        )

    @property
    def is_started(self) -> bool:
        return self._running

    @abstractmethod
    async def start(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> bool:
        raise NotImplementedError

    async def kill(self) -> bool:
        self._running = False
        await self._terminate()
        return True

    async def handle_unexpected_stop(self) -> None:
        self._running = False
        process = getattr(self, "process", None)
        if process is not None and process.poll() is not None:
            await self._drain_stderr_task()
            self.process = None

    async def player_count(self) -> tuple[int, int] | None:
        return None

    def connected_player_names(self) -> tuple[str, ...]:
        return ()

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

    @property
    def resolved_cmd_cwd(self) -> Path:
        return self.cmd_cwd or self.directory

    def log_launch_context(self) -> None:
        log.info(
            "Launch config for %s: scope=%s directory=%s cwd=%s cmd_start=%s join_host=%s join_port=%s api_host=%s api_port=%s server_log=%s stdout_log=%s stderr_log=%s",
            self.name,
            self.scope,
            self.directory,
            self.resolved_cmd_cwd,
            self.cmd_start,
            self.cfg.join_host,
            self.cfg.join_port,
            self.cfg.api_host,
            self.cfg.api_port,
            self.server_log,
            self.file_stdout,
            self.file_errout,
        )

    async def _launch_process(self):
        self.log_launch_context()
        try:
            self.process = subprocess.Popen(
                self.cmd_start,
                cwd=self.resolved_cmd_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                start_new_session=True,
                text=True,
                encoding=config.STR_ENCODE,
                shell=self.shell,
            )
        except Exception:
            log.exception(f"Failed to launch: {self.name}")
            raise
        log.info("Launched %s with pid=%s", self.name, self.process.pid)
        self._stderr_task = asyncio.create_task(self._tee(self.process.stderr, self.file_errout, "STDERR"))

    async def _prelaunch_tasks(self):
        self.act_err_counts = {}

    async def _postlaunch_tasks(self): ...

    async def _std_launch(self):
        await self._prelaunch_tasks()
        await self._launch_process()
        await self._postlaunch_tasks()

    async def wait_for_ready_event(
        self,
        ready_event: asyncio.Event,
        *,
        timeout_seconds: float,
        ready_label: str,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not ready_event.is_set():
            if not self.check_running():
                raise RuntimeError(f"{self.name} stopped before reporting {ready_label}.")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"{self.name} did not report {ready_label} within {timeout_seconds:.0f}s.")
            await asyncio.sleep(1)
        log.info("%s reported %s.", self.name, ready_label)

    async def _drain_stderr_task(self, timeout_seconds: float = 1.0) -> None:
        task = self._stderr_task
        if task is None or task.done():
            return

        current_loop = asyncio.get_running_loop()
        task_loop = task.get_loop()
        if task_loop is not current_loop:
            deadline = current_loop.time() + timeout_seconds
            while not task.done():
                if current_loop.time() >= deadline:
                    log.warning("%s stderr reader did not finish in time; cancelling it.", self.name)
                    if not task_loop.is_closed():
                        task_loop.call_soon_threadsafe(task.cancel)
                    return
                await asyncio.sleep(0.05)
            return

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        except asyncio.TimeoutError:
            log.warning(f"{self.name} stderr reader did not finish in time; cancelling it.")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _cancel_background_task(
        self,
        task: asyncio.Task[object] | None,
        *,
        label: str,
        timeout_seconds: float = 1.0,
    ) -> None:
        if task is None or task.done():
            return

        current_loop = asyncio.get_running_loop()
        task_loop = task.get_loop()
        if task_loop is not current_loop:
            deadline = current_loop.time() + timeout_seconds
            if not task_loop.is_closed():
                task_loop.call_soon_threadsafe(task.cancel)
            while not task.done():
                if current_loop.time() >= deadline:
                    log.warning("%s %s did not finish in time after cross-loop cancellation.", self.name, label)
                    return
                await asyncio.sleep(0.05)
            return

        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        except asyncio.TimeoutError:
            log.warning("%s %s did not finish in time after cancellation.", self.name, label)
        except asyncio.CancelledError:
            pass

    async def _terminate(self):
        if self.process is None and not self.proc_name:
            log.info(f"{self.name} already terminated, skipping.")
            return
        process = self.process
        if process is not None:
            log.info(f"Terminating {self.name} via stored process")

            try:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
                await self._drain_stderr_task()
            except Exception as xcp:
                log.exception(f"Termination failed: {xcp}")

            for _ in range(10):
                if process.poll() is not None:
                    break
                await asyncio.sleep(0.3)
            else:
                try:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
                    await self._drain_stderr_task()
                    log.warning(f"{self.name} kill escalation")
                except Exception as xcp:
                    log.exception(f"Kill escalation failed: {xcp}")
            if self.process is process:
                self.process = None

        if not self.proc_name:
            log.warning("No process name specified for process scan")
            return

        await asyncio.to_thread(self._terminate_leftover_processes_sync)

    def _terminate_leftover_processes_sync(self) -> None:
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
                    time.sleep(0.5)

                subprocess.run(["pkill", "-f", self.proc_name], check=False)

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
