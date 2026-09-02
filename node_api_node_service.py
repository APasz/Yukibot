"""Node-level administration, restart scheduling, and system action dispatch."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Protocol

import config
from _manager import App_Manager
from _security import Access_Control, Power_Level
from _sys import Stats_System
from maintenance import MaintenanceService
from node_api_app_installer import (
    NodeAppInstallScopeOption,
    NodeAppInstallerSettingsMutationResult,
    NodeAppInstallerSettingsState,
)
from node_api_node import (
    NodeCapacityMutationResult,
    NodeDiscordSettingsMutationResult,
    NodeDiskEntry,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeFontSourceSettingsMutationResult,
)
from node_api_system import (
    SYSTEM_ACTION_LABELS,
    NodeRestartRecord,
    NodeRestartScheduleEntry,
    NodeRestartScheduleState,
    NodeRestartState,
    NodeSystemAction,
    NodeSystemActionHandler,
    NodeSystemActionResult,
    NodeSystemCapabilities,
)
from restart_state import RestartRecord
from restart_targets import RestartTarget


class NodeAuditLogger(Protocol):
    """Writes an audit event for a node-management change."""

    def __call__(self, event: str, /, **fields: object) -> None: ...


class ProcessRestartRecordReader(Protocol):
    """Reads a process restart record with a fallback start timestamp."""

    def __call__(self, *, default_timestamp: int) -> RestartRecord: ...


class FontAssetRefresher(Protocol):
    """Schedules a refresh after Google font source settings change."""

    def __call__(self, *, google_font_urls: tuple[str, ...]) -> None: ...


class NodeManagementService:
    """Owns mutable node-management state and privileged operations."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        manager: Callable[[], App_Manager | None],
        require_manager: Callable[[], App_Manager],
        require_acl: Callable[[], Access_Control],
        http_exception: Callable[[int, str], Exception],
        stats_factory: Callable[[], Stats_System],
        invalidate_state_caches: Callable[[], None],
        refresh_font_assets: FontAssetRefresher,
        audit_log: NodeAuditLogger,
        process_started_at: Callable[[], int],
        read_process_restart_record: ProcessRestartRecordReader,
        read_voice_restart_record: Callable[[], RestartRecord | None],
        logger: logging.Logger,
        restart_delay_seconds: float,
    ) -> None:
        if restart_delay_seconds < 0:
            raise ValueError("Node system action delay must not be negative.")
        self._node_name = node_name
        self._manager = manager
        self._require_manager = require_manager
        self._require_acl = require_acl
        self._http_exception = http_exception
        self._stats_factory = stats_factory
        self._invalidate_state_caches = invalidate_state_caches
        self._refresh_font_assets = refresh_font_assets
        self._audit_log = audit_log
        self._process_started_at = process_started_at
        self._read_process_restart_record = read_process_restart_record
        self._read_voice_restart_record = read_voice_restart_record
        self._log = logger
        self._restart_delay_seconds = restart_delay_seconds
        self._system_action_handler: NodeSystemActionHandler | None = None
        self._maintenance_service: MaintenanceService | None = None
        self._maintenance_restart_targets: tuple[RestartTarget, ...] = ()
        self._pending_system_action: NodeSystemAction | None = None
        self._system_action_lock = threading.RLock()

    def set_system_action_handler(self, handler: NodeSystemActionHandler) -> None:
        self._system_action_handler = handler

    def set_maintenance_service(
        self,
        maintenance_service: MaintenanceService,
        available_targets: tuple[RestartTarget, ...],
    ) -> None:
        self._maintenance_service = maintenance_service
        self._maintenance_restart_targets = available_targets

    def require_app_installer_available(self) -> None:
        if config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL:
            raise self._http_exception(
                400,
                "App installation is unavailable on portal nodes.",
            )

    def read_app_installer_settings(self) -> NodeAppInstallerSettingsState:
        if not self.system_capabilities().supports_app_installer_settings:
            raise self._http_exception(
                400,
                "App installer settings are unavailable on this node.",
            )
        manager = self._require_manager()
        return NodeAppInstallerSettingsState(
            node=self._node_name(),
            settings=manager.app_installer_settings(),
            available_apps=tuple(
                NodeAppInstallScopeOption(scope=recipe.scope, label=recipe.label)
                for recipe in manager.list_steam_install_recipes()
            ),
        )

    def read_capacity(self) -> config.NodeCapacityProfile:
        return self._require_manager().node_capacity()

    def read_font_sources(self) -> config.NodeFontSourceSettings:
        return self._require_manager().node_font_sources()

    def read_disk_settings(self) -> NodeDiskManagementState:
        stats = self._stats_factory()
        stats.refresh_disk_inventory()
        activity_mountpoints = {disk.mountpoint_text for disk in stats.activity_disks}
        primary_disk = stats.primary_disk
        secondary_disk = stats.secondary_disk
        bot_disk = stats.bot_disk
        return NodeDiskManagementState(
            node=self._node_name(),
            disks=tuple(
                NodeDiskEntry(
                    mountpoint=disk.mountpoint_text,
                    display_name=disk.display_name,
                    is_activity=disk.mountpoint_text in activity_mountpoints,
                    is_primary=(
                        primary_disk is not None
                        and disk.mountpoint_text == primary_disk.mountpoint_text
                    ),
                    is_secondary=(
                        secondary_disk is not None
                        and disk.mountpoint_text == secondary_disk.mountpoint_text
                    ),
                    is_bot_disk=(
                        bot_disk is not None
                        and disk.mountpoint_text == bot_disk.mountpoint_text
                    ),
                )
                for disk in stats.disks
            ),
            preferences=stats.disk_preferences,
        )

    def read_discord_settings(self) -> config.DiscordSettings:
        return self._require_manager().discord_settings()

    async def schedule_system_action(
        self,
        *,
        action: NodeSystemAction,
        auto_restart_running_apps: bool,
        silent: bool,
        actor_user_id: int,
    ) -> NodeSystemActionResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        if not self.system_capabilities().supports(action):
            raise self._http_exception(
                400,
                f"Node action {action.value!r} is unavailable on this node.",
            )
        handler = self._system_action_handler
        if handler is None:
            raise self._http_exception(
                503,
                "Node system actions are unavailable on this node.",
            )
        with self._system_action_lock:
            if self._pending_system_action is not None:
                raise self._http_exception(
                    409,
                    f"Node system action {self._pending_system_action.value!r} is already pending.",
                )
            self._pending_system_action = action

        self._audit_log(
            "node.system_action.scheduled",
            actor_user_id=actor_user_id,
            node=self._node_name(),
            action=action.value,
        )

        def _dispatch() -> None:
            try:
                handler(action, auto_restart_running_apps, silent)
            except Exception:
                with self._system_action_lock:
                    self._pending_system_action = None
                self._log.exception(
                    "Node system action dispatch failed: node=%s action=%s",
                    self._node_name(),
                    action.value,
                )

        asyncio.get_running_loop().call_later(self._restart_delay_seconds, _dispatch)
        action_label = SYSTEM_ACTION_LABELS[action]
        return NodeSystemActionResult(
            node=self._node_name(),
            action=action,
            message=f"Scheduled {action_label} for {self._node_name()}.",
        )

    def system_capabilities(self) -> NodeSystemCapabilities:
        manager = self._manager()
        is_portal = config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL
        supports_app_auto_restart = not is_portal and manager is not None
        supports_silent_restart = (
            not is_portal and manager is not None and manager.bot is not None
        )
        supports_node_capacity = manager is not None
        supports_node_font_sources = manager is not None
        supports_discord_settings = manager is not None and manager.bot is not None
        supports_app_installer_settings = not is_portal and manager is not None
        if is_portal:
            return NodeSystemCapabilities(
                actions=(NodeSystemAction.RESTART_PROCESS,),
                supports_app_auto_restart=supports_app_auto_restart,
                supports_silent_restart=supports_silent_restart,
                supports_node_capacity=supports_node_capacity,
                supports_node_font_sources=supports_node_font_sources,
                supports_discord_settings=supports_discord_settings,
                supports_app_installer_settings=supports_app_installer_settings,
            )
        return NodeSystemCapabilities(
            actions=(NodeSystemAction.RESTART_PROCESS, NodeSystemAction.REBOOT_HOST),
            supports_app_auto_restart=supports_app_auto_restart,
            supports_silent_restart=supports_silent_restart,
            supports_node_capacity=supports_node_capacity,
            supports_node_font_sources=supports_node_font_sources,
            supports_discord_settings=supports_discord_settings,
            supports_app_installer_settings=supports_app_installer_settings,
        )

    def read_restart_state(self) -> NodeRestartState:
        process_record = self._read_process_restart_record(
            default_timestamp=self._process_started_at()
        )
        voice_record = self._read_voice_restart_record()
        return NodeRestartState(
            node=self._node_name(),
            process=NodeRestartRecord(
                timestamp=process_record.timestamp,
                kind=process_record.kind,
            ),
            voice=(
                None
                if voice_record is None
                else NodeRestartRecord(
                    timestamp=voice_record.timestamp,
                    kind=voice_record.kind,
                )
            ),
        )

    def read_restart_schedules(self) -> NodeRestartScheduleState:
        maintenance = self._maintenance_service
        if maintenance is None:
            raise self._http_exception(
                503,
                "Restart scheduling is unavailable on this node.",
            )
        if not maintenance.reload():
            raise self._http_exception(
                503,
                "Restart scheduling configuration is temporarily unavailable.",
            )
        return self._restart_schedule_state(maintenance)

    async def update_restart_schedule(
        self,
        *,
        target: RestartTarget,
        interval_minutes: int | None,
        anchor_timestamp: int | None,
        actor_user_id: int,
    ) -> NodeRestartScheduleState:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        maintenance = self._require_maintenance_service()
        self._require_restart_target(target)
        if interval_minutes is not None and anchor_timestamp is None:
            raise self._http_exception(
                400,
                "Enabled restart schedules require an anchor timestamp.",
            )
        try:
            updated_schedules = maintenance.update_restart_intervals(
                {target: interval_minutes},
                anchor_timestamp=anchor_timestamp,
            )
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        updated_schedule = updated_schedules[target]
        self._audit_log(
            "node.restart_schedule.updated",
            actor_user_id=actor_user_id,
            node=self._node_name(),
            target=target.value,
            interval_minutes=interval_minutes,
            anchor_timestamp=anchor_timestamp,
            automatically_skipped_through_timestamp=updated_schedule.skipped_through_timestamp,
        )
        return self._restart_schedule_state(maintenance)

    async def skip_restart_schedule(
        self,
        *,
        target: RestartTarget,
        actor_user_id: int,
    ) -> NodeRestartScheduleState:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        maintenance = self._require_maintenance_service()
        self._require_restart_target(target)
        try:
            schedule = maintenance.skip_next_restart(target)
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        self._audit_log(
            "node.restart_schedule.skipped",
            actor_user_id=actor_user_id,
            node=self._node_name(),
            target=target.value,
            skipped_through_timestamp=schedule.skipped_through_timestamp,
        )
        return self._restart_schedule_state(maintenance)

    async def mutate_capacity(
        self,
        *,
        capacity: config.NodeCapacityProfile,
        actor_user_id: int,
    ) -> NodeCapacityMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.root)
        updated_capacity = self._require_manager().set_node_capacity(capacity)
        self._invalidate_state_caches()
        return NodeCapacityMutationResult(
            node=self._node_name(),
            message=f"Updated node capacity for {self._node_name()}.",
            capacity=updated_capacity,
        )

    async def mutate_app_installer_settings(
        self,
        *,
        settings: config.AppInstallerSettings,
        actor_user_id: int,
    ) -> NodeAppInstallerSettingsMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.root)
        if not self.system_capabilities().supports_app_installer_settings:
            raise self._http_exception(
                400,
                "App installer settings are unavailable on this node.",
            )
        updated_settings = self._require_manager().set_app_installer_settings(settings)
        self._audit_log(
            "node.app_installer_settings.updated",
            actor_user_id=actor_user_id,
            node=self._node_name(),
            allowed_scopes=updated_settings.allowed_scopes,
        )
        return NodeAppInstallerSettingsMutationResult(
            node=self._node_name(),
            message=f"Updated app install settings for {self._node_name()}.",
            settings=updated_settings,
        )

    async def mutate_disk_settings(
        self,
        *,
        preferences: config.PersistedDiskPreferences,
        actor_user_id: int,
        read_disk_settings: Callable[[], NodeDiskManagementState],
    ) -> NodeDiskSettingsMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.root)
        updated_preferences = self._stats_factory().set_disk_preferences(preferences)
        self._invalidate_state_caches()
        self._audit_log(
            "node.disk_settings.updated",
            actor_user_id=actor_user_id,
            node=self._node_name(),
            activity_mounts=updated_preferences.activity_mounts,
            primary_mount=updated_preferences.primary_mount,
            secondary_mount=updated_preferences.secondary_mount,
            label_mountpoints=sorted(updated_preferences.labels),
        )
        return NodeDiskSettingsMutationResult(
            node=self._node_name(),
            message=f"Updated node disk settings for {self._node_name()}.",
            settings=read_disk_settings(),
        )

    async def mutate_font_sources(
        self,
        *,
        settings: config.NodeFontSourceSettings,
        actor_user_id: int,
    ) -> NodeFontSourceSettingsMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        updated_settings = self._require_manager().set_node_font_sources(settings)
        self._refresh_font_assets(
            google_font_urls=updated_settings.google_font_urls,
        )
        return NodeFontSourceSettingsMutationResult(
            node=self._node_name(),
            message=f"Updated node font sources for {self._node_name()}.",
            settings=updated_settings,
        )

    async def mutate_discord_settings(
        self,
        *,
        settings: config.DiscordSettings,
        actor_user_id: int,
        read_discord_settings: Callable[[], config.DiscordSettings],
    ) -> NodeDiscordSettingsMutationResult:
        manager = self._require_manager()
        current_settings = read_discord_settings()
        required_level = (
            Power_Level.root
            if current_settings.activity.refresh_interval_seconds
            != settings.activity.refresh_interval_seconds
            else Power_Level.sudo
        )
        await self._require_acl().perm_check(actor_user_id, required_level)
        updated_settings = manager.set_discord_settings(settings)
        if manager.activity_manager is not None:
            await manager.activity_manager.refresh()
        return NodeDiscordSettingsMutationResult(
            node=self._node_name(),
            message=f"Updated Discord settings for {self._node_name()}.",
            settings=updated_settings,
        )

    def _require_maintenance_service(self) -> MaintenanceService:
        maintenance = self._maintenance_service
        if maintenance is None:
            raise self._http_exception(
                503,
                "Restart scheduling is unavailable on this node.",
            )
        return maintenance

    def _require_restart_target(self, target: RestartTarget) -> None:
        if target not in self._maintenance_restart_targets:
            raise self._http_exception(
                400,
                f"Restart target {target.value!r} is unavailable on this node.",
            )

    def _restart_schedule_state(
        self,
        maintenance: MaintenanceService,
    ) -> NodeRestartScheduleState:
        return NodeRestartScheduleState(
            node=self._node_name(),
            schedules=tuple(
                NodeRestartScheduleEntry(
                    target=target,
                    enabled=(schedule := maintenance.schedule_for(target)).enabled,
                    interval_minutes=schedule.interval_minutes,
                    anchor_timestamp=schedule.anchor_timestamp,
                    last_triggered_timestamp=schedule.last_triggered_timestamp,
                    next_restart_timestamp=(
                        int(next_restart.timestamp())
                        if (next_restart := maintenance.next_restart_at(target))
                        is not None
                        else None
                    ),
                    skipped_through_timestamp=schedule.skipped_through_timestamp,
                )
                for target in self._maintenance_restart_targets
            ),
        )
