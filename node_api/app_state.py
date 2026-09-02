"""Shared node API app-state contracts.

This module deliberately contains only behaviour expressed through the common
application contract. Game/mod-specific behaviour remains in apps.*.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, field_validator, model_validator
from pydantic.config import ConfigDict

from _manager import App_Manager, AppDetailsUpdate
from _security import Access_Control, Power_Level
from apps._app import App, AppRuntimeFault, AppVersionSource, ChatRelaySupport
from apps._config import (
    AppTitleFont,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackRelease,
    normalise_activity_provider_ids,
    normalise_app_title_font,
)
from apps._node_api import (
    optional_int,
    optional_string,
    power_level,
    required_bool,
    required_int,
    required_string,
    string_tuple,
)
from apps._updater import AppUpdateInfo, AppUpdateStatus
from .route_contracts import DiscordHealthSnapshot, HttpExceptionFactory
from .system import NodeSystemSummary
from node_auth import NodeApiScope

_DEFAULT_REMOTE_CONFIG_READ_LEVEL = Power_Level.sudo
_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL = Power_Level.root

__all__: tuple[str, ...] = (
    "ClientPackFilePreview",
    "NodeAppActivityProviderEntry",
    "NodeAppEntry",
    "NodeAppFootprintSnapshot",
    "NodeAppMutationAction",
    "NodeAppMutationRequest",
    "NodeAppMutationResult",
    "NodeAppMutationService",
    "NodeAppResourcePointSummary",
    "NodeAppRuntimeSummary",
    "NodeAppStateCache",
    "NodeAppStateSubscriptionService",
    "NodeAppStateStreamEvent",
    "NodeAppTransitionSnapshot",
    "NodeAppTransitionState",
    "NodeStateStreamEvent",
    "NodeStateTopic",
    "required_app_mutation_level",
    "required_app_mutation_scope",
    "_ALL_NODE_STATE_TOPICS",
    "_NodeAppPlayerSnapshot",
    "_NodeLocalAppRuntimeSubscription",
    "_NodeLocalAppRuntimeWatchState",
    "_NodeLocalNodeStateSubscription",
    "_NodeLocalNodeStateWatchState",
    "_TimedAppRuntimeSummary",
    "_TimedNodeAppEntries",
    "_TimedNodeSystemSummary",
)


class NodeAppTransitionState(StrEnum):
    NONE = "none"
    STARTING = "starting"
    STOPPING = "stopping"


class NodeAppMutationAction(StrEnum):
    START = "start"
    STOP = "stop"
    KILL = "kill"
    DELETE = "delete"
    ENABLE = "enable"
    DISABLE = "disable"
    RENAME = "rename"
    UPDATE_DETAILS = "update_details"
    UPDATE = "update"
    VERIFY = "verify"
    SELECT_UPDATE_BRANCH = "select_update_branch"


def required_app_mutation_level(action: NodeAppMutationAction) -> Power_Level:
    if action in {NodeAppMutationAction.START, NodeAppMutationAction.STOP}:
        return Power_Level.user
    if action is NodeAppMutationAction.DELETE:
        return Power_Level.root
    if action in {
        NodeAppMutationAction.KILL,
        NodeAppMutationAction.ENABLE,
        NodeAppMutationAction.DISABLE,
        NodeAppMutationAction.RENAME,
        NodeAppMutationAction.UPDATE_DETAILS,
        NodeAppMutationAction.UPDATE,
        NodeAppMutationAction.VERIFY,
        NodeAppMutationAction.SELECT_UPDATE_BRANCH,
    }:
        return Power_Level.sudo
    raise ValueError(f"Unsupported app mutation action: {action}")


def required_app_mutation_scope(action: NodeAppMutationAction) -> NodeApiScope:
    if action in {NodeAppMutationAction.START, NodeAppMutationAction.STOP}:
        return NodeApiScope.APP_CONTROL
    if action in {
        NodeAppMutationAction.KILL,
        NodeAppMutationAction.DELETE,
        NodeAppMutationAction.ENABLE,
        NodeAppMutationAction.DISABLE,
        NodeAppMutationAction.RENAME,
        NodeAppMutationAction.UPDATE_DETAILS,
        NodeAppMutationAction.UPDATE,
        NodeAppMutationAction.VERIFY,
        NodeAppMutationAction.SELECT_UPDATE_BRANCH,
    }:
        return NodeApiScope.APP_MANAGE
    raise ValueError(f"Unsupported app mutation action: {action}")


class NodeAppMutationRequest(BaseModel):
    action: NodeAppMutationAction
    friendly_name: str | None = None
    title_font_preset: str | None = None
    notes: str | None = None
    lifecycle_notice_started: bool | None = None
    lifecycle_notice_stopped: bool | None = None
    lifecycle_notice_crashed: bool | None = None
    relay_notice_player_session: bool | None = None
    relay_notice_player_death: bool | None = None
    relay_notice_progress: bool | None = None
    relay_advancements_enabled: bool | None = None
    factorio_chat_relay_use_shout: bool | None = None
    rcon_requires_online_players: bool | None = None
    disabled_activity_provider_ids: tuple[str, ...] | None = None
    running_cpu_points: int | None = None
    running_ram_points: int | None = None
    startup_cpu_points: int | None = None
    startup_ram_points: int | None = None
    steam_update_enabled: bool | None = None
    steam_update_selected_branch: str | None = None
    update_branch_id: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("disabled_activity_provider_ids", mode="before")
    @classmethod
    def _validate_disabled_activity_provider_ids(cls, raw: object) -> tuple[str, ...] | None:
        if raw is None:
            return None
        return normalise_activity_provider_ids(raw)

    @model_validator(mode="after")
    def _validate_payload(self) -> "NodeAppMutationRequest":
        if self.action is NodeAppMutationAction.SELECT_UPDATE_BRANCH:
            if self.update_branch_id is None or not self.update_branch_id.strip():
                raise ValueError("Update branch id is required for branch-selection requests.")
            return self
        if self.action is not NodeAppMutationAction.RENAME:
            if self.action is not NodeAppMutationAction.UPDATE_DETAILS:
                return self
            if self.friendly_name is None or not self.friendly_name:
                raise ValueError("Friendly name is required for update-details requests.")
            if self.lifecycle_notice_started is None:
                raise ValueError("Started lifecycle notice flag is required for update-details requests.")
            if self.lifecycle_notice_stopped is None:
                raise ValueError("Stopped lifecycle notice flag is required for update-details requests.")
            if self.lifecycle_notice_crashed is None:
                raise ValueError("Crash lifecycle notice flag is required for update-details requests.")
            if self.running_cpu_points is None:
                raise ValueError("Running CPU points are required for update-details requests.")
            if self.running_ram_points is None:
                raise ValueError("Running RAM points are required for update-details requests.")
            return self
        if self.friendly_name is None or not self.friendly_name:
            raise ValueError("Friendly name is required for rename requests.")
        return self


@dataclass(frozen=True, slots=True)
class NodeAppResourcePointSummary:
    cpu_points_running: int
    cpu_points_startup: int
    ram_points_running: int
    ram_points_startup: int
    startup_defined: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppResourcePointSummary":
        return cls(
            cpu_points_running=required_int(payload, "cpu_points_running"),
            cpu_points_startup=required_int(payload, "cpu_points_startup"),
            ram_points_running=required_int(payload, "ram_points_running"),
            ram_points_startup=required_int(payload, "ram_points_startup"),
            startup_defined=required_bool(payload, "startup_defined") if "startup_defined" in payload else False,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "cpu_points_running": self.cpu_points_running,
            "cpu_points_startup": self.cpu_points_startup,
            "ram_points_running": self.ram_points_running,
            "ram_points_startup": self.ram_points_startup,
            "startup_defined": self.startup_defined,
        }


@dataclass(frozen=True, slots=True)
class NodeAppActivityProviderEntry:
    provider_id: str
    label: str
    enabled: bool
    current_value: str | None = None
    detail_value: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppActivityProviderEntry":
        return cls(
            provider_id=required_string(payload, "provider_id"),
            label=required_string(payload, "label"),
            enabled=required_bool(payload, "enabled"),
            current_value=optional_string(payload, "current_value"),
            detail_value=optional_string(payload, "detail_value"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "enabled": self.enabled,
            "current_value": self.current_value,
            "detail_value": self.detail_value,
        }


@dataclass(frozen=True, slots=True)
class ClientPackFilePreview:
    path: str
    display_name: str
    content_text: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ClientPackFilePreview:
        return cls(
            path=required_string(payload, "path"),
            display_name=required_string(payload, "display_name"),
            content_text=required_string(payload, "content_text"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "path": self.path,
            "display_name": self.display_name,
            "content_text": self.content_text,
        }


@dataclass(frozen=True, slots=True)
class NodeAppEntry:
    name: str
    friendly: str
    node: str
    running: bool
    enabled: bool
    supports_mods: bool
    supports_configs: bool
    scope: str | None = None
    transition_state: NodeAppTransitionState = NodeAppTransitionState.NONE
    player_count: int | None = None
    player_capacity: int | None = None
    connected_player_names: tuple[str, ...] = ()
    supports_saves: bool = False
    supports_save_uploads: bool = False
    supports_save_rename: bool = False
    supports_blueprints: bool = False
    supports_settings: bool = False
    supports_console_actions: bool = False
    supports_chat: bool = False
    supports_updates: bool = False
    supports_sevendays_sandbox_options: bool = False
    client_pack_content_dirty: bool = False
    client_pack_published_version: str | None = None
    client_pack_next_version: str | None = None
    client_pack_published_changelog: str | None = None
    client_pack_releases: tuple[ClientPackRelease, ...] = ()
    client_pack_kubejs_scripts: tuple[ClientPackKubeJsScript, ...] = ()
    client_pack_metadata: ClientPackMetadataConfig | None = None
    client_pack_file_previews: tuple[ClientPackFilePreview, ...] = ()
    client_pack_automated_changelog: str = ""
    runtime_fault: AppRuntimeFault | None = None
    update_info: AppUpdateInfo | None = None
    update_status: AppUpdateStatus | None = None
    config_read_level: Power_Level = _DEFAULT_REMOTE_CONFIG_READ_LEVEL
    config_write_level: Power_Level = _DEFAULT_REMOTE_CONFIG_WRITE_LEVEL
    save_write_level: Power_Level = Power_Level.sudo
    color_hex: str | None = None
    map_url: str | None = None
    join_address: str | None = None
    join_direct_ip_address: str | None = None
    resource_points: NodeAppResourcePointSummary | None = None
    title_font_preset: str = AppTitleFont.AUTO.value
    notes: str | None = field(default=None, kw_only=True)
    lifecycle_notice_started: bool = True
    lifecycle_notice_stopped: bool = True
    lifecycle_notice_crashed: bool = True
    relay_notice_player_session: bool | None = None
    relay_notice_player_death: bool | None = None
    relay_notice_progress: bool | None = None
    relay_notice_progress_label: str | None = None
    relay_advancements_enabled: bool | None = None
    relay_advancement_term: str | None = None
    factorio_chat_relay_use_shout: bool | None = None
    rcon_requires_online_players: bool | None = None
    activity_providers: tuple[NodeAppActivityProviderEntry, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeAppEntry:
        name = payload.get("name")
        friendly = payload.get("friendly")
        notes = payload.get("notes")
        node = payload.get("node")
        running = payload.get("running", False)
        enabled = payload.get("enabled", True)
        supports_mods = payload.get("supports_mods")
        supports_configs = payload.get("supports_configs", False)
        scope = payload.get("scope")
        transition_state = _app_transition_state(payload, "transition_state")
        player_count = optional_int(payload, "player_count")
        player_capacity = optional_int(payload, "player_capacity")
        connected_player_names = string_tuple(payload, "connected_player_names")
        supports_saves = payload.get("supports_saves", False)
        supports_save_uploads = payload.get("supports_save_uploads", False)
        supports_save_rename = payload.get("supports_save_rename", False)
        supports_blueprints = payload.get("supports_blueprints", False)
        supports_settings = payload.get("supports_settings", False)
        supports_console_actions = payload.get("supports_console_actions", False)
        supports_chat = payload.get("supports_chat", False)
        supports_updates = payload.get("supports_updates", False)
        supports_sevendays_sandbox_options = payload.get("supports_sevendays_sandbox_options", False)
        client_pack_content_dirty = payload.get("client_pack_content_dirty", False)
        client_pack_published_version = optional_string(payload, "client_pack_published_version")
        client_pack_next_version = optional_string(payload, "client_pack_next_version")
        client_pack_published_changelog = optional_string(payload, "client_pack_published_changelog")
        raw_client_pack_releases = payload.get("client_pack_releases", ())
        raw_client_pack_kubejs_scripts = payload.get("client_pack_kubejs_scripts", ())
        raw_client_pack_metadata = payload.get("client_pack_metadata")
        raw_client_pack_file_previews = payload.get("client_pack_file_previews", ())
        client_pack_automated_changelog = payload.get("client_pack_automated_changelog", "")
        raw_runtime_fault = payload.get("runtime_fault")
        raw_update_info = payload.get("update_info")
        raw_update_status = payload.get("update_status")
        config_read_level = power_level(payload, "config_read_level", default=_DEFAULT_REMOTE_CONFIG_READ_LEVEL)
        config_write_level = power_level(payload, "config_write_level", default=_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL)
        save_write_level = power_level(payload, "save_write_level", default=Power_Level.sudo)
        color_hex = payload.get("color_hex")
        map_url = payload.get("map_url")
        join_address = optional_string(payload, "join_address")
        join_direct_ip_address = optional_string(payload, "join_direct_ip_address")
        raw_resource_points = payload.get("resource_points")
        raw_title_font_preset = payload.get("title_font_preset", AppTitleFont.AUTO.value)
        lifecycle_notice_started = payload.get("lifecycle_notice_started", True)
        lifecycle_notice_stopped = payload.get("lifecycle_notice_stopped", True)
        lifecycle_notice_crashed = payload.get("lifecycle_notice_crashed", True)
        relay_notice_player_session = payload.get("relay_notice_player_session")
        relay_notice_player_death = payload.get("relay_notice_player_death")
        relay_notice_progress = payload.get("relay_notice_progress")
        relay_notice_progress_label = payload.get("relay_notice_progress_label")
        relay_advancements_enabled = payload.get("relay_advancements_enabled")
        relay_advancement_term = payload.get("relay_advancement_term")
        factorio_chat_relay_use_shout = payload.get("factorio_chat_relay_use_shout")
        rcon_requires_online_players = payload.get("rcon_requires_online_players")
        raw_activity_providers = payload.get("activity_providers", ())
        if not isinstance(name, str) or not name:
            raise ValueError("Node app entry name is invalid.")
        if not isinstance(friendly, str) or not friendly:
            raise ValueError("Node app entry friendly name is invalid.")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("Node app entry notes are invalid.")
        if not isinstance(node, str) or not node:
            raise ValueError("Node app entry node is invalid.")
        if not isinstance(running, bool):
            raise ValueError("Node app entry running is invalid.")
        if not isinstance(enabled, bool):
            raise ValueError("Node app entry enabled is invalid.")
        if not isinstance(supports_mods, bool):
            raise ValueError("Node app entry supports_mods is invalid.")
        if not isinstance(supports_configs, bool):
            raise ValueError("Node app entry supports_configs is invalid.")
        if scope is not None and (not isinstance(scope, str) or not scope.strip()):
            raise ValueError("Node app entry scope is invalid.")
        if not isinstance(supports_saves, bool):
            raise ValueError("Node app entry supports_saves is invalid.")
        if not isinstance(supports_save_uploads, bool):
            raise ValueError("Node app entry supports_save_uploads is invalid.")
        if not isinstance(supports_save_rename, bool):
            raise ValueError("Node app entry supports_save_rename is invalid.")
        if not isinstance(supports_blueprints, bool):
            raise ValueError("Node app entry supports_blueprints is invalid.")
        if not isinstance(supports_settings, bool):
            raise ValueError("Node app entry supports_settings is invalid.")
        if not isinstance(supports_console_actions, bool):
            raise ValueError("Node app entry supports_console_actions is invalid.")
        if not isinstance(supports_chat, bool):
            raise ValueError("Node app entry supports_chat is invalid.")
        if not isinstance(supports_updates, bool):
            raise ValueError("Node app entry supports_updates is invalid.")
        if not isinstance(supports_sevendays_sandbox_options, bool):
            raise ValueError("Node app entry supports_sevendays_sandbox_options is invalid.")
        if not isinstance(client_pack_content_dirty, bool):
            raise ValueError("Node app entry client_pack_content_dirty is invalid.")
        if not isinstance(raw_client_pack_releases, list | tuple) or any(
            not isinstance(release_payload, Mapping) for release_payload in raw_client_pack_releases
        ):
            raise ValueError("Node app entry client_pack_releases is invalid.")
        if not isinstance(raw_client_pack_kubejs_scripts, list | tuple) or any(
            not isinstance(script_payload, Mapping) for script_payload in raw_client_pack_kubejs_scripts
        ):
            raise ValueError("Node app entry client_pack_kubejs_scripts is invalid.")
        client_pack_kubejs_scripts = tuple(
            ClientPackKubeJsScript.model_validate(script_payload) for script_payload in raw_client_pack_kubejs_scripts
        )
        if raw_client_pack_metadata is not None and not isinstance(raw_client_pack_metadata, Mapping):
            raise ValueError("Node app entry client_pack_metadata is invalid.")
        client_pack_metadata = (
            ClientPackMetadataConfig.model_validate(raw_client_pack_metadata)
            if raw_client_pack_metadata is not None
            else None
        )
        if not isinstance(raw_client_pack_file_previews, list | tuple) or any(
            not isinstance(preview_payload, Mapping) for preview_payload in raw_client_pack_file_previews
        ):
            raise ValueError("Node app entry client_pack_file_previews is invalid.")
        if not isinstance(client_pack_automated_changelog, str):
            raise ValueError("Node app entry client_pack_automated_changelog is invalid.")
        client_pack_file_previews = tuple(
            ClientPackFilePreview.from_mapping(cast(Mapping[str, object], preview_payload))
            for preview_payload in raw_client_pack_file_previews
        )
        client_pack_releases = tuple(
            ClientPackRelease.model_validate(release_payload) for release_payload in raw_client_pack_releases
        )
        if not client_pack_releases and (
            client_pack_published_version is not None and client_pack_published_changelog is not None
        ):
            client_pack_releases = (
                ClientPackRelease(
                    version=client_pack_published_version,
                    changelog=client_pack_published_changelog,
                ),
            )
        release_versions = [release.version for release in client_pack_releases]
        if len(release_versions) != len(set(release_versions)):
            raise ValueError("Node app entry client pack release versions must be unique.")
        if raw_runtime_fault is not None and not isinstance(raw_runtime_fault, Mapping):
            raise ValueError("Node app entry runtime_fault is invalid.")
        if raw_update_info is not None and not isinstance(raw_update_info, Mapping):
            raise ValueError("Node app entry update_info is invalid.")
        if raw_update_status is not None and not isinstance(raw_update_status, Mapping):
            raise ValueError("Node app entry update_status is invalid.")
        if raw_resource_points is not None and not isinstance(raw_resource_points, Mapping):
            raise ValueError("Node app entry resource_points is invalid.")
        if color_hex is not None and not isinstance(color_hex, str):
            raise ValueError("Node app entry color_hex is invalid.")
        if map_url is not None and not isinstance(map_url, str):
            raise ValueError("Node app entry map_url is invalid.")
        if join_direct_ip_address is not None and join_address is None:
            raise ValueError("Node app entry direct join address requires a primary join address.")
        if not isinstance(raw_title_font_preset, str):
            raise ValueError("Node app entry title_font_preset is invalid.")
        try:
            title_font_preset = normalise_app_title_font(raw_title_font_preset)
        except (TypeError, ValueError) as xcp:
            raise ValueError("Node app entry title_font_preset is invalid.") from xcp
        if not isinstance(lifecycle_notice_started, bool):
            raise ValueError("Node app entry lifecycle_notice_started is invalid.")
        if not isinstance(lifecycle_notice_stopped, bool):
            raise ValueError("Node app entry lifecycle_notice_stopped is invalid.")
        if not isinstance(lifecycle_notice_crashed, bool):
            raise ValueError("Node app entry lifecycle_notice_crashed is invalid.")
        if relay_notice_player_session is not None and not isinstance(relay_notice_player_session, bool):
            raise ValueError("Node app entry relay_notice_player_session is invalid.")
        if relay_notice_player_death is not None and not isinstance(relay_notice_player_death, bool):
            raise ValueError("Node app entry relay_notice_player_death is invalid.")
        if relay_notice_progress is not None and not isinstance(relay_notice_progress, bool):
            raise ValueError("Node app entry relay_notice_progress is invalid.")
        if relay_notice_progress_label is not None and (
            not isinstance(relay_notice_progress_label, str) or not relay_notice_progress_label.strip()
        ):
            raise ValueError("Node app entry relay_notice_progress_label is invalid.")
        if (relay_notice_progress is None) != (relay_notice_progress_label is None):
            raise ValueError("Node app entry relay progress metadata is inconsistent.")
        if relay_advancements_enabled is not None and not isinstance(relay_advancements_enabled, bool):
            raise ValueError("Node app entry relay_advancements_enabled is invalid.")
        if relay_advancement_term is not None and (
            not isinstance(relay_advancement_term, str) or not relay_advancement_term.strip()
        ):
            raise ValueError("Node app entry relay_advancement_term is invalid.")
        if (relay_advancements_enabled is None) != (relay_advancement_term is None):
            raise ValueError("Node app entry relay advancement metadata is inconsistent.")
        if factorio_chat_relay_use_shout is not None and not isinstance(factorio_chat_relay_use_shout, bool):
            raise ValueError("Node app entry factorio_chat_relay_use_shout is invalid.")
        if rcon_requires_online_players is not None and not isinstance(rcon_requires_online_players, bool):
            raise ValueError("Node app entry rcon_requires_online_players is invalid.")
        if not isinstance(raw_activity_providers, list | tuple):
            raise ValueError("Node app entry activity_providers is invalid.")
        if any(not isinstance(provider_payload, Mapping) for provider_payload in raw_activity_providers):
            raise ValueError("Node app entry activity_providers is invalid.")
        return cls(
            name=name,
            friendly=friendly,
            notes=notes,
            node=node,
            running=running,
            enabled=enabled,
            supports_mods=supports_mods,
            supports_configs=supports_configs,
            scope=scope.strip() if isinstance(scope, str) else None,
            transition_state=transition_state,
            player_count=player_count,
            player_capacity=player_capacity,
            connected_player_names=connected_player_names,
            supports_saves=supports_saves,
            supports_save_uploads=supports_save_uploads,
            supports_save_rename=supports_save_rename,
            supports_blueprints=supports_blueprints,
            supports_settings=supports_settings,
            supports_console_actions=supports_console_actions,
            supports_chat=supports_chat,
            supports_updates=supports_updates,
            supports_sevendays_sandbox_options=supports_sevendays_sandbox_options,
            client_pack_content_dirty=client_pack_content_dirty,
            client_pack_published_version=client_pack_published_version,
            client_pack_next_version=client_pack_next_version,
            client_pack_published_changelog=client_pack_published_changelog,
            client_pack_releases=client_pack_releases,
            client_pack_kubejs_scripts=client_pack_kubejs_scripts,
            client_pack_metadata=client_pack_metadata,
            client_pack_file_previews=client_pack_file_previews,
            client_pack_automated_changelog=client_pack_automated_changelog.strip(),
            runtime_fault=AppRuntimeFault.from_mapping(cast(Mapping[str, object], raw_runtime_fault))
            if raw_runtime_fault is not None
            else None,
            update_info=AppUpdateInfo.from_mapping(cast(Mapping[str, object], raw_update_info))
            if raw_update_info is not None
            else None,
            update_status=AppUpdateStatus.from_mapping(cast(Mapping[str, object], raw_update_status))
            if raw_update_status is not None
            else None,
            config_read_level=config_read_level,
            config_write_level=config_write_level,
            save_write_level=save_write_level,
            color_hex=color_hex,
            map_url=map_url,
            join_address=join_address,
            join_direct_ip_address=join_direct_ip_address,
            resource_points=(
                NodeAppResourcePointSummary.from_mapping(cast(Mapping[str, object], raw_resource_points))
                if raw_resource_points is not None
                else None
            ),
            title_font_preset=title_font_preset,
            lifecycle_notice_started=lifecycle_notice_started,
            lifecycle_notice_stopped=lifecycle_notice_stopped,
            lifecycle_notice_crashed=lifecycle_notice_crashed,
            relay_notice_player_session=relay_notice_player_session,
            relay_notice_player_death=relay_notice_player_death,
            relay_notice_progress=relay_notice_progress,
            relay_notice_progress_label=relay_notice_progress_label,
            relay_advancements_enabled=relay_advancements_enabled,
            relay_advancement_term=relay_advancement_term,
            factorio_chat_relay_use_shout=factorio_chat_relay_use_shout,
            rcon_requires_online_players=rcon_requires_online_players,
            activity_providers=tuple(
                NodeAppActivityProviderEntry.from_mapping(cast(Mapping[str, object], provider_payload))
                for provider_payload in raw_activity_providers
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "friendly": self.friendly,
            "notes": self.notes,
            "node": self.node,
            "running": self.running,
            "enabled": self.enabled,
            "supports_mods": self.supports_mods,
            "supports_configs": self.supports_configs,
            "scope": self.scope,
            "transition_state": self.transition_state.value,
            "player_count": self.player_count,
            "player_capacity": self.player_capacity,
            "connected_player_names": self.connected_player_names,
            "supports_saves": self.supports_saves,
            "supports_save_uploads": self.supports_save_uploads,
            "supports_save_rename": self.supports_save_rename,
            "supports_blueprints": self.supports_blueprints,
            "supports_settings": self.supports_settings,
            "supports_console_actions": self.supports_console_actions,
            "supports_chat": self.supports_chat,
            "supports_updates": self.supports_updates,
            "supports_sevendays_sandbox_options": self.supports_sevendays_sandbox_options,
            "client_pack_content_dirty": self.client_pack_content_dirty,
            "client_pack_published_version": self.client_pack_published_version,
            "client_pack_next_version": self.client_pack_next_version,
            "client_pack_published_changelog": self.client_pack_published_changelog,
            "client_pack_releases": [release.model_dump(mode="json") for release in self.client_pack_releases],
            "client_pack_kubejs_scripts": [
                script.model_dump(mode="json") for script in self.client_pack_kubejs_scripts
            ],
            "client_pack_metadata": (
                self.client_pack_metadata.model_dump(mode="json") if self.client_pack_metadata is not None else None
            ),
            "client_pack_file_previews": [preview.to_mapping() for preview in self.client_pack_file_previews],
            "client_pack_automated_changelog": self.client_pack_automated_changelog,
            "runtime_fault": self.runtime_fault.to_mapping() if self.runtime_fault is not None else None,
            "update_info": self.update_info.to_mapping() if self.update_info is not None else None,
            "update_status": self.update_status.to_mapping() if self.update_status is not None else None,
            "config_read_level": self.config_read_level.name,
            "config_write_level": self.config_write_level.name,
            "save_write_level": self.save_write_level.name,
            "color_hex": self.color_hex,
            "map_url": self.map_url,
            "join_address": self.join_address,
            "join_direct_ip_address": self.join_direct_ip_address,
            "resource_points": self.resource_points.to_mapping() if self.resource_points is not None else None,
            "title_font_preset": self.title_font_preset,
            "lifecycle_notice_started": self.lifecycle_notice_started,
            "lifecycle_notice_stopped": self.lifecycle_notice_stopped,
            "lifecycle_notice_crashed": self.lifecycle_notice_crashed,
            "relay_notice_player_session": self.relay_notice_player_session,
            "relay_notice_player_death": self.relay_notice_player_death,
            "relay_notice_progress": self.relay_notice_progress,
            "relay_notice_progress_label": self.relay_notice_progress_label,
            "relay_advancements_enabled": self.relay_advancements_enabled,
            "relay_advancement_term": self.relay_advancement_term,
            "factorio_chat_relay_use_shout": self.factorio_chat_relay_use_shout,
            "rcon_requires_online_players": self.rcon_requires_online_players,
            "activity_providers": [provider.to_mapping() for provider in self.activity_providers],
        }


def _app_transition_state(
    payload: Mapping[str, object],
    key: str,
    *,
    default: NodeAppTransitionState | None = None,
) -> NodeAppTransitionState:
    resolved_default = NodeAppTransitionState.NONE if default is None else default
    value = payload.get(key)
    if value is None:
        return resolved_default
    if not isinstance(value, str):
        raise ValueError(f"{key} is invalid.")
    try:
        return NodeAppTransitionState(value)
    except ValueError as xcp:
        raise ValueError(f"{key} is invalid.") from xcp


@dataclass(frozen=True, slots=True)
class NodeAppTransitionSnapshot:
    state: NodeAppTransitionState
    requested_at_seconds: float


@dataclass(frozen=True, slots=True)
class _NodeAppPlayerSnapshot:
    player_count: int
    player_capacity: int
    connected_player_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeAppRuntimeSummary:
    running: bool
    enabled: bool
    version: str | None
    player_count: int | None
    player_capacity: int | None
    relay_support: ChatRelaySupport
    storage_percent: int | None
    storage_free_bytes: int | None
    storage_total_bytes: int | None
    version_source: AppVersionSource = AppVersionSource.STARTUP
    footprint_bytes: int | None = None
    runtime_fault: AppRuntimeFault | None = None
    transition_state: NodeAppTransitionState = NodeAppTransitionState.NONE
    connected_player_names: tuple[str, ...] = ()
    activity_providers: tuple[NodeAppActivityProviderEntry, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeAppRuntimeSummary:
        running = payload.get("running")
        enabled = payload.get("enabled")
        version = payload.get("version")
        raw_version_source = payload.get("version_source", AppVersionSource.STARTUP.value)
        transition_state = _app_transition_state(payload, "transition_state")
        player_count = optional_int(payload, "player_count")
        player_capacity = optional_int(payload, "player_capacity")
        connected_player_names = string_tuple(payload, "connected_player_names")
        relay_support = payload.get("relay_support")
        storage_percent = optional_int(payload, "storage_percent")
        storage_free_bytes = optional_int(payload, "storage_free_bytes")
        storage_total_bytes = optional_int(payload, "storage_total_bytes")
        footprint_bytes = optional_int(payload, "footprint_bytes")
        raw_runtime_fault = payload.get("runtime_fault")
        raw_activity_providers = payload.get("activity_providers", ())

        if not isinstance(running, bool):
            raise ValueError("Node app runtime summary running is invalid.")
        if not isinstance(enabled, bool):
            raise ValueError("Node app runtime summary enabled is invalid.")
        if version is not None and not isinstance(version, str):
            raise ValueError("Node app runtime summary version is invalid.")
        if not isinstance(relay_support, str):
            raise ValueError("Node app runtime summary relay_support is invalid.")
        if not isinstance(raw_version_source, str):
            raise ValueError("Node app runtime summary version_source is invalid.")

        try:
            parsed_relay_support = ChatRelaySupport(relay_support)
        except ValueError as xcp:
            raise ValueError("Node app runtime summary relay_support is invalid.") from xcp
        try:
            version_source = AppVersionSource(raw_version_source)
        except ValueError as xcp:
            raise ValueError("Node app runtime summary version_source is invalid.") from xcp
        if raw_runtime_fault is not None and not isinstance(raw_runtime_fault, Mapping):
            raise ValueError("Node app runtime summary runtime_fault is invalid.")
        if not isinstance(raw_activity_providers, list | tuple):
            raise ValueError("Node app runtime summary activity_providers is invalid.")
        if any(not isinstance(provider_payload, Mapping) for provider_payload in raw_activity_providers):
            raise ValueError("Node app runtime summary activity_providers is invalid.")

        return cls(
            running=running,
            enabled=enabled,
            version=version,
            transition_state=transition_state,
            player_count=player_count,
            player_capacity=player_capacity,
            relay_support=parsed_relay_support,
            version_source=version_source,
            storage_percent=storage_percent,
            storage_free_bytes=storage_free_bytes,
            storage_total_bytes=storage_total_bytes,
            footprint_bytes=footprint_bytes,
            runtime_fault=AppRuntimeFault.from_mapping(cast(Mapping[str, object], raw_runtime_fault))
            if raw_runtime_fault is not None
            else None,
            connected_player_names=connected_player_names,
            activity_providers=tuple(
                NodeAppActivityProviderEntry.from_mapping(cast(Mapping[str, object], provider_payload))
                for provider_payload in raw_activity_providers
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "running": self.running,
            "enabled": self.enabled,
            "version": self.version,
            "transition_state": self.transition_state.value,
            "player_count": self.player_count,
            "player_capacity": self.player_capacity,
            "connected_player_names": self.connected_player_names,
            "relay_support": self.relay_support.value,
            "version_source": self.version_source.value,
            "storage_percent": self.storage_percent,
            "storage_free_bytes": self.storage_free_bytes,
            "storage_total_bytes": self.storage_total_bytes,
            "footprint_bytes": self.footprint_bytes,
            "runtime_fault": self.runtime_fault.to_mapping() if self.runtime_fault is not None else None,
            "activity_providers": [provider.to_mapping() for provider in self.activity_providers],
        }


@dataclass(frozen=True, slots=True)
class NodeAppMutationResult:
    app_name: str
    app_friendly: str
    node: str
    action: NodeAppMutationAction
    message: str
    app_stats: NodeAppRuntimeSummary | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppMutationResult":
        app_name = required_string(payload, "app_name")
        app_friendly = required_string(payload, "app_friendly")
        node = required_string(payload, "node")
        message = required_string(payload, "message")
        raw_action = required_string(payload, "action")
        try:
            action = NodeAppMutationAction(raw_action)
        except ValueError as xcp:
            raise ValueError("Node app mutation action is invalid.") from xcp
        raw_app_stats = payload.get("app_stats")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node app mutation app_stats are invalid.")
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            action=action,
            message=message,
            app_stats=NodeAppRuntimeSummary.from_mapping(raw_app_stats) if raw_app_stats is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "action": self.action.value,
            "message": self.message,
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
        }


class NodeAppMutationService:
    """Owns asynchronous app mutations and their visible transition state."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        invalidate_state_caches: Callable[[str], None],
        build_runtime_summary: Callable[[App], Awaitable[NodeAppRuntimeSummary]],
        build_live_runtime_summary: Callable[[App], Awaitable[NodeAppRuntimeSummary]],
        transition_ttl_seconds: float,
    ) -> None:
        if transition_ttl_seconds <= 0:
            raise ValueError("App transition cache TTL must be positive.")
        self._node_name = node_name
        self._invalidate_state_caches = invalidate_state_caches
        self._build_runtime_summary = build_runtime_summary
        self._build_live_runtime_summary = build_live_runtime_summary
        self._transition_ttl_seconds = transition_ttl_seconds
        self._transitions: dict[str, NodeAppTransitionSnapshot] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deleting_app_keys: set[str] = set()
        self._log = logging.getLogger(__name__)

    def cancel_pending(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._transitions.clear()
        for task in tasks:
            task.cancel()

    def transition_state(self, app_name: str) -> NodeAppTransitionState:
        key = app_name.casefold()
        snapshot = self._transitions.get(key)
        if snapshot is None:
            return NodeAppTransitionState.NONE
        task = self._tasks.get(key)
        if task is not None and task.done():
            self._tasks.pop(key, None)
            task = None
        if task is not None:
            return snapshot.state
        if time.monotonic() - snapshot.requested_at_seconds >= self._transition_ttl_seconds:
            self._transitions.pop(key, None)
            return NodeAppTransitionState.NONE
        return snapshot.state

    def remember_transition_state(self, app_name: str, state: NodeAppTransitionState) -> None:
        key = app_name.casefold()
        if state is NodeAppTransitionState.NONE:
            self._transitions.pop(key, None)
            return
        self._transitions[key] = NodeAppTransitionSnapshot(
            state=state,
            requested_at_seconds=time.monotonic(),
        )

    async def mutate(
        self,
        *,
        manager: App_Manager,
        acl: Access_Control,
        app: App,
        action: NodeAppMutationAction,
        actor_user_id: int,
        http_exception: HttpExceptionFactory,
        friendly_name: str | None = None,
        title_font_preset: str | None = None,
        notes: str | None = None,
        lifecycle_notice_started: bool | None = None,
        lifecycle_notice_stopped: bool | None = None,
        lifecycle_notice_crashed: bool | None = None,
        relay_notice_player_session: bool | None = None,
        relay_notice_player_death: bool | None = None,
        relay_notice_progress: bool | None = None,
        relay_advancements_enabled: bool | None = None,
        factorio_chat_relay_use_shout: bool | None = None,
        rcon_requires_online_players: bool | None = None,
        disabled_activity_provider_ids: tuple[str, ...] | None = None,
        running_cpu_points: int | None = None,
        running_ram_points: int | None = None,
        startup_cpu_points: int | None = None,
        startup_ram_points: int | None = None,
        steam_update_enabled: bool | None = None,
        steam_update_selected_branch: str | None = None,
        update_branch_id: str | None = None,
    ) -> NodeAppMutationResult:
        await acl.perm_check(actor_user_id, required_app_mutation_level(action))
        app_key = app.name.casefold()
        if action is not NodeAppMutationAction.DELETE and app_key in self._deleting_app_keys:
            raise http_exception(409, f"Cannot change {app.friendly}; deletion is in progress.")
        if action is NodeAppMutationAction.START:
            blocker = manager.start_blocker(app)
            if blocker is not None:
                raise http_exception(409, blocker.message)
            self._track_task(
                app_name=app.name,
                action=action,
                state=NodeAppTransitionState.STARTING,
                task=asyncio.create_task(self._run_task(manager=manager, app=app, action=action)),
            )
            message = f"Start requested for {app.friendly}."
        elif action is NodeAppMutationAction.STOP:
            self._track_task(
                app_name=app.name,
                action=action,
                state=NodeAppTransitionState.STOPPING,
                task=asyncio.create_task(self._run_task(manager=manager, app=app, action=action)),
            )
            message = f"Stop requested for {app.friendly}."
        elif action is NodeAppMutationAction.KILL:
            self._track_task(
                app_name=app.name,
                action=action,
                state=NodeAppTransitionState.STOPPING,
                task=asyncio.create_task(self._run_task(manager=manager, app=app, action=action)),
            )
            message = f"Kill requested for {app.friendly}."
        elif action is NodeAppMutationAction.DELETE:
            pending_task = self._tasks.get(app_key)
            if pending_task is not None and not pending_task.done():
                raise http_exception(409, f"Cannot delete {app.friendly}; another state change is in progress.")
            if app_key in self._deleting_app_keys:
                raise http_exception(409, f"Cannot delete {app.friendly}; deletion is already in progress.")
            self._deleting_app_keys.add(app_key)
            try:
                await manager.delete_instance(app)
            finally:
                self._deleting_app_keys.discard(app_key)
            message = f"Deleted {app.friendly}."
        elif action is NodeAppMutationAction.ENABLE:
            manager.toggle(app.name, True)
            message = f"Enabled {app.friendly}."
        elif action is NodeAppMutationAction.DISABLE:
            manager.toggle(app.name, False)
            message = f"Disabled {app.friendly}."
        elif action is NodeAppMutationAction.RENAME:
            if friendly_name is None or not friendly_name.strip():
                raise ValueError("Friendly name must not be empty.")
            previous_friendly_name = app.friendly
            next_friendly_name = manager.set_app_friendly_name(app, friendly_name)
            message = (
                f"Friendly name already set to {next_friendly_name}."
                if previous_friendly_name == next_friendly_name
                else f"Renamed {previous_friendly_name} to {next_friendly_name}."
            )
        elif action is NodeAppMutationAction.UPDATE_DETAILS:
            if friendly_name is None or not friendly_name.strip():
                raise ValueError("Friendly name must not be empty.")
            if lifecycle_notice_started is None:
                raise ValueError("Started lifecycle notice flag must not be empty.")
            if lifecycle_notice_stopped is None:
                raise ValueError("Stopped lifecycle notice flag must not be empty.")
            if lifecycle_notice_crashed is None:
                raise ValueError("Crash lifecycle notice flag must not be empty.")
            if running_cpu_points is None:
                raise ValueError("Running CPU points must not be empty.")
            if running_ram_points is None:
                raise ValueError("Running RAM points must not be empty.")
            manager.update_app_details(
                app,
                AppDetailsUpdate(
                    friendly_name=friendly_name,
                    title_font_preset=title_font_preset,
                    notes=notes,
                    lifecycle_notice_started=lifecycle_notice_started,
                    lifecycle_notice_stopped=lifecycle_notice_stopped,
                    lifecycle_notice_crashed=lifecycle_notice_crashed,
                    relay_notice_player_session=relay_notice_player_session,
                    relay_notice_player_death=relay_notice_player_death,
                    relay_notice_progress=relay_notice_progress,
                    relay_advancements_enabled=relay_advancements_enabled,
                    factorio_chat_relay_use_shout=factorio_chat_relay_use_shout,
                    rcon_requires_online_players=rcon_requires_online_players,
                    disabled_activity_provider_ids=disabled_activity_provider_ids,
                    running_cpu_points=running_cpu_points,
                    running_ram_points=running_ram_points,
                    startup_cpu_points=startup_cpu_points,
                    startup_ram_points=startup_ram_points,
                    steam_update_enabled=steam_update_enabled,
                    steam_update_selected_branch=steam_update_selected_branch,
                ),
            )
            message = f"Updated details for {app.friendly}."
        elif action is NodeAppMutationAction.SELECT_UPDATE_BRANCH:
            if app.updater is None:
                raise ValueError(f"{app.friendly} does not support updates.")
            if update_branch_id is None or not update_branch_id.strip():
                raise ValueError("Update branch id must not be empty.")
            self._log.info(
                "Node API selecting update branch: node=%s app=%s branch=%s actor=%s",
                self._node_name(),
                app.name,
                update_branch_id,
                actor_user_id,
            )
            update_info = app.updater.select_branch(update_branch_id)
            message = f"Selected update branch {update_info.selected_branch_label} for {app.friendly}."
        elif action is NodeAppMutationAction.UPDATE:
            if app.updater is None:
                raise ValueError(f"{app.friendly} does not support updates.")
            self._log.info(
                "Node API starting update: node=%s app=%s actor=%s branch=%s",
                self._node_name(),
                app.name,
                actor_user_id,
                app.update_info.selected_branch_id if app.update_info is not None else None,
            )
            message = (await app.updater.start_selected_update()).message
        elif action is NodeAppMutationAction.VERIFY:
            if app.updater is None:
                raise ValueError(f"{app.friendly} does not support verification.")
            self._log.info(
                "Node API starting verify: node=%s app=%s actor=%s branch=%s",
                self._node_name(),
                app.name,
                actor_user_id,
                app.update_info.selected_branch_id if app.update_info is not None else None,
            )
            message = (await app.updater.start_selected_verify()).message
        else:
            raise ValueError(f"Unsupported app mutation action: {action}")

        self._invalidate_state_caches(app.name)
        if action is NodeAppMutationAction.DELETE:
            app_stats = None
        elif action in {
            NodeAppMutationAction.START,
            NodeAppMutationAction.STOP,
            NodeAppMutationAction.KILL,
        }:
            app_stats = await self._build_live_runtime_summary(app)
        else:
            app_stats = await self._build_runtime_summary(app)
        self._log.info(
            "Node API app mutated: node=%s app=%s action=%s actor=%s",
            self._node_name(),
            app.name,
            action.value,
            actor_user_id,
        )
        return NodeAppMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self._node_name(),
            action=action,
            message=message,
            app_stats=app_stats,
        )

    def _track_task(
        self,
        *,
        app_name: str,
        action: NodeAppMutationAction,
        state: NodeAppTransitionState,
        task: asyncio.Task[None],
    ) -> None:
        key = app_name.casefold()
        self.remember_transition_state(app_name, state)
        self._tasks[key] = task
        task.add_done_callback(
            lambda completed_task: self._finish_task(app_name=app_name, action=action, task=completed_task)
        )

    def _finish_task(
        self,
        *,
        app_name: str,
        action: NodeAppMutationAction,
        task: asyncio.Task[None],
    ) -> None:
        key = app_name.casefold()
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self.remember_transition_state(app_name, NodeAppTransitionState.NONE)
        try:
            task.result()
        except asyncio.CancelledError:
            self._log.info(
                "Node API app mutation task cancelled: node=%s app=%s action=%s",
                self._node_name(),
                app_name,
                action,
            )
        except Exception:
            self._log.exception(
                "Node API app mutation failed: node=%s app=%s action=%s",
                self._node_name(),
                app_name,
                action,
            )

    async def _run_task(
        self,
        *,
        manager: App_Manager,
        app: App,
        action: NodeAppMutationAction,
    ) -> None:
        try:
            if action is NodeAppMutationAction.START:
                await manager.launch(app)
                return
            if action is NodeAppMutationAction.STOP:
                await manager.end(app.name)
                return
            if action is NodeAppMutationAction.KILL:
                await manager.kill(app.name)
                return
            raise ValueError(f"Unsupported app runtime mutation action: {action}")
        finally:
            self._invalidate_state_caches(app.name)


@dataclass(frozen=True, slots=True)
class NodeAppStateStreamEvent:
    app_name: str
    is_initial: bool = False
    runtime_changed: bool = False
    system_changed: bool = False
    update_changed: bool = False
    app_stats: NodeAppRuntimeSummary | None = None
    system_summary: NodeSystemSummary | None = None
    update_info: AppUpdateInfo | None = None
    update_status: AppUpdateStatus | None = None

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("Node app state stream event app name is invalid.")
        if not self.is_initial and not self.runtime_changed and not self.system_changed and not self.update_changed:
            raise ValueError("Node app state stream event must signal initial, runtime, system, or update changes.")
        if self.app_stats is not None and not (self.is_initial or self.runtime_changed):
            raise ValueError("Node app state stream event app stats require initial or runtime changes.")
        if self.system_summary is not None and not (self.is_initial or self.system_changed):
            raise ValueError("Node app state stream event system summary requires initial or system changes.")
        if (self.update_info is not None or self.update_status is not None) and not (
            self.is_initial or self.update_changed
        ):
            raise ValueError("Node app state stream event update state requires initial or update changes.")

    @classmethod
    def initial(
        cls,
        *,
        app_name: str,
        app_stats: NodeAppRuntimeSummary,
        system_summary: NodeSystemSummary | None = None,
        update_info: AppUpdateInfo | None = None,
        update_status: AppUpdateStatus | None = None,
    ) -> "NodeAppStateStreamEvent":
        return cls(
            app_name=app_name,
            is_initial=True,
            runtime_changed=True,
            system_changed=system_summary is not None,
            update_changed=update_info is not None or update_status is not None,
            app_stats=app_stats,
            system_summary=system_summary,
            update_info=update_info,
            update_status=update_status,
        )

    @classmethod
    def runtime(
        cls,
        *,
        app_name: str,
        app_stats: NodeAppRuntimeSummary,
        update_info: AppUpdateInfo | None = None,
        update_status: AppUpdateStatus | None = None,
    ) -> "NodeAppStateStreamEvent":
        return cls(
            app_name=app_name,
            runtime_changed=True,
            update_changed=update_info is not None or update_status is not None,
            app_stats=app_stats,
            update_info=update_info,
            update_status=update_status,
        )

    @classmethod
    def system(
        cls,
        *,
        app_name: str,
        system_summary: NodeSystemSummary,
    ) -> "NodeAppStateStreamEvent":
        return cls(app_name=app_name, system_changed=True, system_summary=system_summary)

    @classmethod
    def both(
        cls,
        *,
        app_name: str,
        app_stats: NodeAppRuntimeSummary,
        system_summary: NodeSystemSummary,
        update_info: AppUpdateInfo | None = None,
        update_status: AppUpdateStatus | None = None,
    ) -> "NodeAppStateStreamEvent":
        return cls(
            app_name=app_name,
            runtime_changed=True,
            system_changed=True,
            update_changed=update_info is not None or update_status is not None,
            app_stats=app_stats,
            system_summary=system_summary,
            update_info=update_info,
            update_status=update_status,
        )

    @classmethod
    def update(
        cls,
        *,
        app_name: str,
        update_info: AppUpdateInfo | None,
        update_status: AppUpdateStatus | None,
    ) -> "NodeAppStateStreamEvent":
        return cls(
            app_name=app_name,
            update_changed=True,
            update_info=update_info,
            update_status=update_status,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppStateStreamEvent":
        raw_app_stats = payload.get("app_stats")
        raw_system_summary = payload.get("system_summary")
        raw_update_info = payload.get("update_info")
        raw_update_status = payload.get("update_status")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node app state stream event app stats are invalid.")
        if raw_system_summary is not None and not isinstance(raw_system_summary, Mapping):
            raise ValueError("Node app state stream event system summary is invalid.")
        if raw_update_info is not None and not isinstance(raw_update_info, Mapping):
            raise ValueError("Node app state stream event update info is invalid.")
        if raw_update_status is not None and not isinstance(raw_update_status, Mapping):
            raise ValueError("Node app state stream event update status is invalid.")
        return cls(
            app_name=required_string(payload, "app_name"),
            is_initial=required_bool(payload, "initial"),
            runtime_changed=required_bool(payload, "runtime_changed"),
            system_changed=required_bool(payload, "system_changed"),
            update_changed=required_bool(payload, "update_changed") if "update_changed" in payload else False,
            app_stats=NodeAppRuntimeSummary.from_mapping(raw_app_stats) if raw_app_stats is not None else None,
            system_summary=NodeSystemSummary.from_mapping(raw_system_summary)
            if raw_system_summary is not None
            else None,
            update_info=AppUpdateInfo.from_mapping(raw_update_info) if raw_update_info is not None else None,
            update_status=AppUpdateStatus.from_mapping(raw_update_status) if raw_update_status is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "initial": self.is_initial,
            "runtime_changed": self.runtime_changed,
            "system_changed": self.system_changed,
            "update_changed": self.update_changed,
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
            "system_summary": self.system_summary.to_mapping() if self.system_summary is not None else None,
            "update_info": self.update_info.to_mapping() if self.update_info is not None else None,
            "update_status": self.update_status.to_mapping() if self.update_status is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeStateStreamEvent:
    node_name: str
    is_initial: bool = False
    apps_changed: bool = False
    system_changed: bool = False
    health_changed: bool = False
    app_entries: tuple[NodeAppEntry, ...] | None = None
    system_summary: NodeSystemSummary | None = None
    discord_health: DiscordHealthSnapshot | None = None

    def __post_init__(self) -> None:
        if not self.node_name.strip():
            raise ValueError("Node state stream event node name is invalid.")
        if not self.is_initial and not self.apps_changed and not self.system_changed and not self.health_changed:
            raise ValueError("Node state stream event must signal initial, app, system, or health changes.")
        if self.app_entries is not None and not (self.is_initial or self.apps_changed):
            raise ValueError("Node state stream event app entries require initial or app changes.")
        if self.system_summary is not None and not (self.is_initial or self.system_changed):
            raise ValueError("Node state stream event system summary requires initial or system changes.")
        if self.discord_health is not None and not (self.is_initial or self.health_changed):
            raise ValueError("Node state stream event Discord health requires initial or health changes.")
        if self.app_entries is not None:
            for entry in self.app_entries:
                if entry.node.casefold() != self.node_name.casefold():
                    raise ValueError("Node state stream event app entry node is invalid.")

    @classmethod
    def initial(
        cls,
        *,
        node_name: str,
        app_entries: tuple[NodeAppEntry, ...] | None = None,
        system_summary: NodeSystemSummary | None = None,
        discord_health: DiscordHealthSnapshot | None = None,
    ) -> "NodeStateStreamEvent":
        if app_entries is None and system_summary is None and discord_health is None:
            raise ValueError("Initial node state events require app, system, or Discord health state.")
        return cls(
            node_name=node_name,
            is_initial=True,
            apps_changed=app_entries is not None,
            system_changed=system_summary is not None,
            health_changed=discord_health is not None,
            app_entries=app_entries,
            system_summary=system_summary,
            discord_health=discord_health,
        )

    @classmethod
    def apps(
        cls,
        *,
        node_name: str,
        app_entries: tuple[NodeAppEntry, ...],
    ) -> "NodeStateStreamEvent":
        return cls(node_name=node_name, apps_changed=True, app_entries=app_entries)

    @classmethod
    def system(
        cls,
        *,
        node_name: str,
        system_summary: NodeSystemSummary,
    ) -> "NodeStateStreamEvent":
        return cls(node_name=node_name, system_changed=True, system_summary=system_summary)

    @classmethod
    def health(cls, *, node_name: str, discord_health: DiscordHealthSnapshot | None) -> "NodeStateStreamEvent":
        return cls(node_name=node_name, health_changed=True, discord_health=discord_health)

    @classmethod
    def both(
        cls,
        *,
        node_name: str,
        app_entries: tuple[NodeAppEntry, ...],
        system_summary: NodeSystemSummary,
    ) -> "NodeStateStreamEvent":
        return cls(
            node_name=node_name,
            apps_changed=True,
            system_changed=True,
            app_entries=app_entries,
            system_summary=system_summary,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeStateStreamEvent":
        raw_entries = payload.get("app_entries")
        raw_system_summary = payload.get("system_summary")
        raw_discord_health = payload.get("discord_health")
        if raw_entries is not None and (not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes))):
            raise ValueError("Node state stream event app entries are invalid.")
        if raw_system_summary is not None and not isinstance(raw_system_summary, Mapping):
            raise ValueError("Node state stream event system summary is invalid.")
        if raw_discord_health is not None and not isinstance(raw_discord_health, Mapping):
            raise ValueError("Node state stream event Discord health is invalid.")
        parsed_entries: tuple[NodeAppEntry, ...] | None = None
        if raw_entries is not None:
            parsed_entry_list: list[NodeAppEntry] = []
            for entry in cast(Sequence[object], raw_entries):
                if not isinstance(entry, Mapping):
                    raise ValueError("Node state stream event app entries are invalid.")
                parsed_entry_list.append(NodeAppEntry.from_mapping(cast(Mapping[str, object], entry)))
            parsed_entries = tuple(parsed_entry_list)
        return cls(
            node_name=required_string(payload, "node_name"),
            is_initial=required_bool(payload, "initial"),
            apps_changed=required_bool(payload, "apps_changed"),
            system_changed=required_bool(payload, "system_changed"),
            health_changed=required_bool(payload, "health_changed") if "health_changed" in payload else False,
            app_entries=parsed_entries,
            system_summary=NodeSystemSummary.from_mapping(raw_system_summary)
            if raw_system_summary is not None
            else None,
            discord_health=DiscordHealthSnapshot.from_mapping(raw_discord_health)
            if raw_discord_health is not None
            else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_name": self.node_name,
            "initial": self.is_initial,
            "apps_changed": self.apps_changed,
            "system_changed": self.system_changed,
            "health_changed": self.health_changed,
            "app_entries": [entry.to_mapping() for entry in self.app_entries] if self.app_entries is not None else None,
            "system_summary": self.system_summary.to_mapping() if self.system_summary is not None else None,
            "discord_health": self.discord_health.to_mapping() if self.discord_health is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeAppFootprintSnapshot:
    paths: tuple[Path, ...]
    measured_at_seconds: float
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _NodeLocalAppRuntimeSubscription:
    callback: Callable[[NodeAppStateStreamEvent], None]
    include_update_state: bool = False


@dataclass(slots=True)
class _NodeLocalAppRuntimeWatchState:
    callbacks: dict[str, _NodeLocalAppRuntimeSubscription] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


class NodeStateTopic(StrEnum):
    APPS = "apps"
    SYSTEM = "system"
    HEALTH = "health"


_ALL_NODE_STATE_TOPICS: frozenset[NodeStateTopic] = frozenset(NodeStateTopic)


@dataclass(slots=True)
class _NodeLocalNodeStateSubscription:
    callback: Callable[[NodeStateStreamEvent], None]
    topics: frozenset[NodeStateTopic]
    initial_sent: bool = False


@dataclass(slots=True)
class _NodeLocalNodeStateWatchState:
    subscriptions: dict[str, _NodeLocalNodeStateSubscription] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class _TimedNodeAppEntries:
    captured_at_seconds: float
    entries: tuple[NodeAppEntry, ...]


@dataclass(frozen=True, slots=True)
class _TimedNodeSystemSummary:
    captured_at_seconds: float
    summary: NodeSystemSummary


@dataclass(frozen=True, slots=True)
class _TimedAppRuntimeSummary:
    captured_at_seconds: float
    summary: NodeAppRuntimeSummary


class _RuntimeSummaryBuilder(Protocol):
    def __call__(
        self,
        app: App,
        *,
        include_storage: bool = True,
        include_footprint: bool = True,
    ) -> Awaitable[NodeAppRuntimeSummary]: ...


class NodeAppStateCache:
    """Caches expensive app catalogue and runtime snapshots."""

    def __init__(
        self,
        *,
        app_entry_ttl_seconds: float,
        live_runtime_ttl_seconds: float,
        full_runtime_ttl_seconds: float,
    ) -> None:
        if (
            min(
                app_entry_ttl_seconds,
                live_runtime_ttl_seconds,
                full_runtime_ttl_seconds,
            )
            <= 0
        ):
            raise ValueError("App state cache TTLs must be positive.")
        self._app_entry_ttl_seconds = app_entry_ttl_seconds
        self._live_runtime_ttl_seconds = live_runtime_ttl_seconds
        self._full_runtime_ttl_seconds = full_runtime_ttl_seconds
        self._app_entries: _TimedNodeAppEntries | None = None
        self._app_entries_lock = asyncio.Lock()
        self._live_runtime: dict[str, _TimedAppRuntimeSummary] = {}
        self._live_runtime_locks: dict[str, asyncio.Lock] = {}
        self._full_runtime: dict[str, _TimedAppRuntimeSummary] = {}
        self._full_runtime_locks: dict[str, asyncio.Lock] = {}
        self._log = logging.getLogger(__name__)

    def invalidate(self, app_name: str | None = None) -> None:
        self._app_entries = None
        if app_name is None:
            self._live_runtime.clear()
            self._full_runtime.clear()
            return
        app_key = app_name.casefold()
        self._live_runtime.pop(app_key, None)
        self._full_runtime.pop(app_key, None)

    async def list_entries(
        self,
        *,
        build_entries: Callable[[], Awaitable[tuple[NodeAppEntry, ...]]],
        node_name: str,
    ) -> tuple[NodeAppEntry, ...]:
        now = time.monotonic()
        cached = self._app_entries
        if cached is not None and now - cached.captured_at_seconds < self._app_entry_ttl_seconds:
            return cached.entries
        if cached is not None and self._app_entries_lock.locked():
            return cached.entries
        async with self._app_entries_lock:
            now = time.monotonic()
            cached = self._app_entries
            if cached is not None and now - cached.captured_at_seconds < self._app_entry_ttl_seconds:
                return cached.entries
            try:
                entries = await build_entries()
            except Exception as xcp:
                if cached is None:
                    raise
                self._app_entries = _TimedNodeAppEntries(
                    captured_at_seconds=time.monotonic(),
                    entries=cached.entries,
                )
                self._log.warning(
                    "Node API app entry refresh failed; serving stale entries: node=%s error=%s",
                    node_name,
                    xcp,
                )
                return cached.entries
            self._app_entries = _TimedNodeAppEntries(
                captured_at_seconds=time.monotonic(),
                entries=entries,
            )
            return entries

    async def app_entry(
        self,
        app: App,
        *,
        build_entry: Callable[[App], Awaitable[NodeAppEntry]],
    ) -> NodeAppEntry:
        cached = self._app_entries
        if cached is not None and time.monotonic() - cached.captured_at_seconds < self._app_entry_ttl_seconds:
            app_key = app.name.casefold()
            for entry in cached.entries:
                if entry.name.casefold() == app_key:
                    return entry
        return await build_entry(app)

    async def full_runtime_summary(
        self,
        app: App,
        *,
        build_summary: _RuntimeSummaryBuilder,
    ) -> NodeAppRuntimeSummary:
        app_key = app.name.casefold()
        now = time.monotonic()
        cached = self._full_runtime.get(app_key)
        if cached is not None and now - cached.captured_at_seconds < self._full_runtime_ttl_seconds:
            return cached.summary
        lock = self._full_runtime_locks.setdefault(app_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._full_runtime.get(app_key)
            if cached is not None and now - cached.captured_at_seconds < self._full_runtime_ttl_seconds:
                return cached.summary
            summary = await build_summary(app)
            timed_summary = _TimedAppRuntimeSummary(captured_at_seconds=time.monotonic(), summary=summary)
            self._full_runtime[app_key] = timed_summary
            self._live_runtime[app_key] = timed_summary
            return summary

    async def live_runtime_summary(
        self,
        app: App,
        *,
        build_summary: _RuntimeSummaryBuilder,
    ) -> NodeAppRuntimeSummary:
        app_key = app.name.casefold()
        now = time.monotonic()
        cached = self._live_runtime.get(app_key)
        if cached is not None and now - cached.captured_at_seconds < self._live_runtime_ttl_seconds:
            return cached.summary
        lock = self._live_runtime_locks.setdefault(app_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._live_runtime.get(app_key)
            if cached is not None and now - cached.captured_at_seconds < self._live_runtime_ttl_seconds:
                return cached.summary
            summary = await build_summary(app, include_storage=False, include_footprint=False)
            self._live_runtime[app_key] = _TimedAppRuntimeSummary(
                captured_at_seconds=time.monotonic(),
                summary=summary,
            )
            return summary


class NodeAppStateSubscriptionService:
    """Owns local app and node state subscriptions and their watcher tasks."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        is_shutting_down: Callable[[], bool],
        resolve_app: Callable[[str], App],
        build_live_runtime_summary: Callable[[App], Awaitable[NodeAppRuntimeSummary]],
        list_apps: Callable[[], Awaitable[tuple[NodeAppEntry, ...]]],
        build_system_summary: Callable[[], NodeSystemSummary],
        stream_system_summary: Callable[[NodeSystemSummary], NodeSystemSummary],
        discord_health: Callable[[], DiscordHealthSnapshot | None],
        app_runtime_interval_seconds: float,
        node_state_interval_seconds: float,
    ) -> None:
        if min(app_runtime_interval_seconds, node_state_interval_seconds) <= 0:
            raise ValueError("App state subscription intervals must be positive.")
        self._node_name = node_name
        self._is_shutting_down = is_shutting_down
        self._resolve_app = resolve_app
        self._build_live_runtime_summary = build_live_runtime_summary
        self._list_apps = list_apps
        self._build_system_summary = build_system_summary
        self._stream_system_summary = stream_system_summary
        self._discord_health = discord_health
        self._app_runtime_interval_seconds = app_runtime_interval_seconds
        self._node_state_interval_seconds = node_state_interval_seconds
        self._runtime_watchers: dict[str, _NodeLocalAppRuntimeWatchState] = {}
        self._runtime_watch_lock = threading.RLock()
        self._node_state_watcher = _NodeLocalNodeStateWatchState()
        self._node_state_watch_lock = threading.RLock()
        self._log = logging.getLogger(__name__)

    def close(self) -> None:
        with self._runtime_watch_lock:
            runtime_tasks = tuple(state.task for state in self._runtime_watchers.values() if state.task is not None)
            self._runtime_watchers.clear()
        with self._node_state_watch_lock:
            node_task = self._node_state_watcher.task
            self._node_state_watcher = _NodeLocalNodeStateWatchState()
        for task in runtime_tasks:
            task.cancel()
        if node_task is not None:
            node_task.cancel()

    def subscribe_app_runtime(
        self,
        app_name: str,
        callback: Callable[[NodeAppStateStreamEvent], None],
        *,
        include_update_state: bool = False,
    ) -> Callable[[], None]:
        if not app_name.strip():
            raise ValueError("App name is required for local runtime subscriptions.")
        app_key = app_name.casefold()
        subscription_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        with self._runtime_watch_lock:
            state = self._runtime_watchers.get(app_key)
            if state is None:
                state = _NodeLocalAppRuntimeWatchState()
                self._runtime_watchers[app_key] = state
            state.callbacks[subscription_id] = _NodeLocalAppRuntimeSubscription(
                callback=callback,
                include_update_state=include_update_state,
            )
            if state.task is None or state.task.done():
                state.task = loop.create_task(self._watch_app_runtime(app_name, app_key))
        return lambda: self._unsubscribe_app_runtime(app_key, subscription_id)

    def subscribe_node_state(
        self,
        callback: Callable[[NodeStateStreamEvent], None],
        *,
        topics: frozenset[NodeStateTopic] = _ALL_NODE_STATE_TOPICS,
    ) -> Callable[[], None]:
        if not topics:
            raise ValueError("Local node state subscriptions require at least one topic.")
        subscription_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        with self._node_state_watch_lock:
            self._node_state_watcher.subscriptions[subscription_id] = _NodeLocalNodeStateSubscription(
                callback=callback,
                topics=topics,
            )
            task = self._node_state_watcher.task
            if task is None or task.done():
                self._node_state_watcher.task = loop.create_task(self._watch_node_state())
        return lambda: self._unsubscribe_node_state(subscription_id)

    def _unsubscribe_app_runtime(self, app_key: str, subscription_id: str) -> None:
        task_to_cancel: asyncio.Task[None] | None = None
        with self._runtime_watch_lock:
            state = self._runtime_watchers.get(app_key)
            if state is None:
                return
            state.callbacks.pop(subscription_id, None)
            if state.callbacks:
                return
            task_to_cancel = state.task
            self._runtime_watchers.pop(app_key, None)
        if task_to_cancel is not None and not task_to_cancel.done():
            task_to_cancel.cancel()

    def _unsubscribe_node_state(self, subscription_id: str) -> None:
        task_to_cancel: asyncio.Task[None] | None = None
        with self._node_state_watch_lock:
            self._node_state_watcher.subscriptions.pop(subscription_id, None)
            if self._node_state_watcher.subscriptions:
                return
            task_to_cancel = self._node_state_watcher.task
            self._node_state_watcher.task = None
        if task_to_cancel is not None and not task_to_cancel.done():
            task_to_cancel.cancel()

    async def _watch_app_runtime(self, app_name: str, app_key: str) -> None:
        current_task = asyncio.current_task()
        last_summary: NodeAppRuntimeSummary | None = None
        has_summary = False
        last_update_info: AppUpdateInfo | None = None
        last_update_status: AppUpdateStatus | None = None
        has_update_state = False
        try:
            while not self._is_shutting_down():
                with self._runtime_watch_lock:
                    state = self._runtime_watchers.get(app_key)
                    if state is None or not state.callbacks:
                        return
                    subscriptions = tuple(state.callbacks.values())
                callbacks = tuple(subscription.callback for subscription in subscriptions)
                include_update_state = any(subscription.include_update_state for subscription in subscriptions)
                try:
                    app = self._resolve_app(app_name)
                    summary = await self._build_live_runtime_summary(app)
                    update_info = app.update_info if include_update_state else None
                    update_status = app.update_status if include_update_state else None
                except asyncio.CancelledError:
                    raise
                except Exception as xcp:
                    self._log.warning(
                        "Node API local runtime watch failed: node=%s app=%s error=%s",
                        self._node_name(),
                        app_name,
                        xcp,
                    )
                    await asyncio.sleep(self._app_runtime_interval_seconds)
                    continue
                runtime_changed = (not has_summary) or summary != last_summary
                update_changed = include_update_state and (
                    (not has_update_state) or update_info != last_update_info or update_status != last_update_status
                )
                if (not has_summary) or runtime_changed or update_changed:
                    event_update_info = update_info if update_changed or not has_summary else None
                    event_update_status = update_status if update_changed or not has_summary else None
                    update = (
                        NodeAppStateStreamEvent.initial(
                            app_name=app_name,
                            app_stats=summary,
                            update_info=event_update_info,
                            update_status=event_update_status,
                        )
                        if not has_summary
                        else NodeAppStateStreamEvent(
                            app_name=app_name,
                            runtime_changed=runtime_changed,
                            update_changed=update_changed,
                            app_stats=summary if runtime_changed else None,
                            update_info=event_update_info if update_changed else None,
                            update_status=event_update_status if update_changed else None,
                        )
                    )
                    for callback in callbacks:
                        try:
                            callback(update)
                        except Exception:
                            self._log.exception(
                                "Node API local runtime subscriber callback failed: node=%s app=%s",
                                self._node_name(),
                                app_name,
                            )
                    last_summary = summary
                    has_summary = True
                    if include_update_state:
                        last_update_info = update_info
                        last_update_status = update_status
                        has_update_state = True
                await asyncio.sleep(self._app_runtime_interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            with self._runtime_watch_lock:
                state = self._runtime_watchers.get(app_key)
                if state is not None and state.task is current_task:
                    state.task = None
                if state is not None and not state.callbacks:
                    self._runtime_watchers.pop(app_key, None)

    async def _watch_node_state(self) -> None:
        current_task = asyncio.current_task()
        last_entries: tuple[NodeAppEntry, ...] | None = None
        last_system_summary: NodeSystemSummary | None = None
        last_discord_health: DiscordHealthSnapshot | None = None
        has_state = False
        try:
            while not self._is_shutting_down():
                with self._node_state_watch_lock:
                    subscriptions = tuple(self._node_state_watcher.subscriptions.values())
                    if not subscriptions:
                        return
                try:
                    needs_apps = any(NodeStateTopic.APPS in subscription.topics for subscription in subscriptions)
                    needs_system = any(NodeStateTopic.SYSTEM in subscription.topics for subscription in subscriptions)
                    needs_health = any(NodeStateTopic.HEALTH in subscription.topics for subscription in subscriptions)
                    app_entries = await self._list_apps() if needs_apps else last_entries
                    system_summary = (
                        self._stream_system_summary(self._build_system_summary())
                        if needs_system
                        else last_system_summary
                    )
                    discord_health = self._discord_health() if needs_health else last_discord_health
                except asyncio.CancelledError:
                    raise
                except Exception as xcp:
                    self._log.warning(
                        "Node API local node state watch failed: node=%s error=%s",
                        self._node_name(),
                        xcp,
                    )
                    await asyncio.sleep(self._node_state_interval_seconds)
                    continue
                apps_changed = app_entries is not None and ((not has_state) or app_entries != last_entries)
                system_changed = system_summary is not None and (
                    (not has_state) or system_summary != last_system_summary
                )
                health_changed = (
                    needs_health
                    and discord_health is not None
                    and ((not has_state) or discord_health != last_discord_health)
                )
                for subscription in subscriptions:
                    include_apps = NodeStateTopic.APPS in subscription.topics
                    include_system = NodeStateTopic.SYSTEM in subscription.topics
                    include_health = NodeStateTopic.HEALTH in subscription.topics
                    event: NodeStateStreamEvent | None = None
                    if not subscription.initial_sent:
                        event = NodeStateStreamEvent(
                            node_name=self._node_name(),
                            is_initial=True,
                            apps_changed=include_apps,
                            system_changed=include_system,
                            health_changed=include_health and discord_health is not None,
                            app_entries=app_entries if include_apps else None,
                            system_summary=system_summary if include_system else None,
                            discord_health=discord_health if include_health else None,
                        )
                    else:
                        event_apps_changed = include_apps and apps_changed
                        event_system_changed = include_system and system_changed
                        event_health_changed = include_health and health_changed
                        if event_apps_changed or event_system_changed or event_health_changed:
                            event = NodeStateStreamEvent(
                                node_name=self._node_name(),
                                apps_changed=event_apps_changed,
                                system_changed=event_system_changed,
                                health_changed=event_health_changed,
                                app_entries=app_entries if event_apps_changed else None,
                                system_summary=system_summary if event_system_changed else None,
                                discord_health=discord_health if event_health_changed else None,
                            )
                    if event is None:
                        continue
                    try:
                        subscription.callback(event)
                        subscription.initial_sent = True
                    except Exception:
                        self._log.exception(
                            "Node API local node state subscriber callback failed: node=%s",
                            self._node_name(),
                        )
                if needs_apps:
                    last_entries = app_entries
                if needs_system:
                    last_system_summary = system_summary
                if needs_health:
                    last_discord_health = discord_health
                has_state = True
                await asyncio.sleep(self._node_state_interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            with self._node_state_watch_lock:
                if self._node_state_watcher.task is current_task:
                    self._node_state_watcher.task = None
