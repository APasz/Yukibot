"""Shared node API app-state contracts.

This module deliberately contains only behaviour expressed through the common
application contract. Game- and mod-specific behaviour remains in apps.*.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from _security import Power_Level
from apps._app import AppRuntimeFault, AppVersionSource, ChatRelaySupport
from apps._config import (
    AppTitleFont,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackRelease,
    normalise_app_title_font,
)
from apps._node_api import (
    optional_int as _optional_int,
    optional_string as _optional_string,
    power_level as _power_level,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
    string_tuple as _string_tuple,
)
from apps._updater import AppUpdateInfo, AppUpdateStatus
from node_api_system import NodeSystemSummary

_DEFAULT_REMOTE_CONFIG_READ_LEVEL = Power_Level.sudo
_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL = Power_Level.root

__all__: tuple[str, ...] = (
    "ClientPackFilePreview",
    "NodeAppActivityProviderEntry",
    "NodeAppEntry",
    "NodeAppFootprintSnapshot",
    "NodeAppResourcePointSummary",
    "NodeAppRuntimeSummary",
    "NodeAppStateStreamEvent",
    "NodeAppTransitionSnapshot",
    "NodeAppTransitionState",
    "NodeStateStreamEvent",
    "NodeStateTopic",
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
            cpu_points_running=_required_int(payload, "cpu_points_running"),
            cpu_points_startup=_required_int(payload, "cpu_points_startup"),
            ram_points_running=_required_int(payload, "ram_points_running"),
            ram_points_startup=_required_int(payload, "ram_points_startup"),
            startup_defined=_required_bool(payload, "startup_defined") if "startup_defined" in payload else False,
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
            provider_id=_required_string(payload, "provider_id"),
            label=_required_string(payload, "label"),
            enabled=_required_bool(payload, "enabled"),
            current_value=_optional_string(payload, "current_value"),
            detail_value=_optional_string(payload, "detail_value"),
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
            path=_required_string(payload, "path"),
            display_name=_required_string(payload, "display_name"),
            content_text=_required_string(payload, "content_text"),
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
        player_count = _optional_int(payload, "player_count")
        player_capacity = _optional_int(payload, "player_capacity")
        connected_player_names = _string_tuple(payload, "connected_player_names")
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
        client_pack_published_version = _optional_string(payload, "client_pack_published_version")
        client_pack_next_version = _optional_string(payload, "client_pack_next_version")
        client_pack_published_changelog = _optional_string(payload, "client_pack_published_changelog")
        raw_client_pack_releases = payload.get("client_pack_releases", ())
        raw_client_pack_kubejs_scripts = payload.get("client_pack_kubejs_scripts", ())
        raw_client_pack_metadata = payload.get("client_pack_metadata")
        raw_client_pack_file_previews = payload.get("client_pack_file_previews", ())
        client_pack_automated_changelog = payload.get("client_pack_automated_changelog", "")
        raw_runtime_fault = payload.get("runtime_fault")
        raw_update_info = payload.get("update_info")
        raw_update_status = payload.get("update_status")
        config_read_level = _power_level(payload, "config_read_level", default=_DEFAULT_REMOTE_CONFIG_READ_LEVEL)
        config_write_level = _power_level(payload, "config_write_level", default=_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL)
        save_write_level = _power_level(payload, "save_write_level", default=Power_Level.sudo)
        color_hex = payload.get("color_hex")
        map_url = payload.get("map_url")
        join_address = _optional_string(payload, "join_address")
        join_direct_ip_address = _optional_string(payload, "join_direct_ip_address")
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
        player_count = _optional_int(payload, "player_count")
        player_capacity = _optional_int(payload, "player_capacity")
        connected_player_names = _string_tuple(payload, "connected_player_names")
        relay_support = payload.get("relay_support")
        storage_percent = _optional_int(payload, "storage_percent")
        storage_free_bytes = _optional_int(payload, "storage_free_bytes")
        storage_total_bytes = _optional_int(payload, "storage_total_bytes")
        footprint_bytes = _optional_int(payload, "footprint_bytes")
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
        return cls(app_name=app_name, update_changed=True, update_info=update_info, update_status=update_status)

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
            app_name=_required_string(payload, "app_name"),
            is_initial=_required_bool(payload, "initial"),
            runtime_changed=_required_bool(payload, "runtime_changed"),
            system_changed=_required_bool(payload, "system_changed"),
            update_changed=_required_bool(payload, "update_changed") if "update_changed" in payload else False,
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
    app_entries: tuple[NodeAppEntry, ...] | None = None
    system_summary: NodeSystemSummary | None = None

    def __post_init__(self) -> None:
        if not self.node_name.strip():
            raise ValueError("Node state stream event node name is invalid.")
        if not self.is_initial and not self.apps_changed and not self.system_changed:
            raise ValueError("Node state stream event must signal initial, app, or system changes.")
        if self.app_entries is not None and not (self.is_initial or self.apps_changed):
            raise ValueError("Node state stream event app entries require initial or app changes.")
        if self.system_summary is not None and not (self.is_initial or self.system_changed):
            raise ValueError("Node state stream event system summary requires initial or system changes.")
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
    ) -> "NodeStateStreamEvent":
        if app_entries is None and system_summary is None:
            raise ValueError("Initial node state events require app or system state.")
        return cls(
            node_name=node_name,
            is_initial=True,
            apps_changed=app_entries is not None,
            system_changed=system_summary is not None,
            app_entries=app_entries,
            system_summary=system_summary,
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
        if raw_entries is not None and (not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes))):
            raise ValueError("Node state stream event app entries are invalid.")
        if raw_system_summary is not None and not isinstance(raw_system_summary, Mapping):
            raise ValueError("Node state stream event system summary is invalid.")
        parsed_entries: tuple[NodeAppEntry, ...] | None = None
        if raw_entries is not None:
            parsed_entry_list: list[NodeAppEntry] = []
            for entry in cast(Sequence[object], raw_entries):
                if not isinstance(entry, Mapping):
                    raise ValueError("Node state stream event app entries are invalid.")
                parsed_entry_list.append(NodeAppEntry.from_mapping(cast(Mapping[str, object], entry)))
            parsed_entries = tuple(parsed_entry_list)
        return cls(
            node_name=_required_string(payload, "node_name"),
            is_initial=_required_bool(payload, "initial"),
            apps_changed=_required_bool(payload, "apps_changed"),
            system_changed=_required_bool(payload, "system_changed"),
            app_entries=parsed_entries,
            system_summary=NodeSystemSummary.from_mapping(raw_system_summary)
            if raw_system_summary is not None
            else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_name": self.node_name,
            "initial": self.is_initial,
            "apps_changed": self.apps_changed,
            "system_changed": self.system_changed,
            "app_entries": [entry.to_mapping() for entry in self.app_entries] if self.app_entries is not None else None,
            "system_summary": self.system_summary.to_mapping() if self.system_summary is not None else None,
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
