from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import struct
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TypeVar, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import hikari
import psutil
import requests
from fastapi import (
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import FileResponse
from modmux.models import Provider
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.config import ConfigDict
from starlette.middleware.cors import CORSMiddleware

import config
from _audit import audit_log
from _authority import AuthorityResource, read_json_object
from _file import File_Utils
from _manager import App_Manager, AppDetailsUpdate, app_scope_from_name
from _mod_ops import (
    ArchiveDataEntry,
    ArchiveEntry,
    ClientPackSelection,
    ClientPackValidationError,
    ModArchiveEntry,
    NonDownloadableModError,
    RunningAppModMutationError,
    build_admin_pack_entries,
    build_client_pack_entries,
    build_server_pack_entries,
    client_pack_content_hash,
    compress_mod_archive_entries,
    require_app_stopped_for_mod_mutation,
    require_downloadable,
)
from _mod_ops import (
    download_entries as build_mod_download_entries,
)
from _security import Access_Control, Power_Level
from _sys import Stats_System, StatsDiskSnapshot, StatsSystemSnapshot
from _utils import Utilities
from apps._app import App, AppRuntimeFault, AppStdoutTail, ChatRelaySupport
from apps._blueprint_files import (
    AppBlueprintEntry,
    AppBlueprintFileEntry,
    AppBlueprintFileType,
    BlueprintUploadPair,
    blueprint_file_type_from_name,
    classify_blueprint_upload_filenames,
)
from apps._config import (
    CLIENT_PACK_CHANGELOG_MAX_LENGTH,
    AppTitleFont,
    BulkLauncherMetadataDiscovery,
    BulkLauncherMetadataEntry,
    BulkLauncherMetadataStatus,
    ClientPackConfig,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackModSnapshot,
    ClientPackRelease,
    KnownModPageProvider,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    LauncherProviderUrls,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageDiscovery,
    ModPageLink,
    ModPlacement,
    ModPlatformMetadata,
    ModType,
    is_client_pack_candidate,
    known_mod_page_provider_for_url,
    normalise_activity_provider_ids,
    normalise_app_title_font,
    normalise_client_pack_changelog,
)
from apps._config_files import AppConfigFile, AppConfigFileContent, AppConfigFileRoot
from apps._console import (
    ConsoleAction,
    ConsoleActionParameter,
    ConsoleActionResult,
    ConsoleResponseSource,
    execute_console_action,
)
from apps._launcher_metadata import (
    BulkLauncherMetadataTarget,
    discover_bulk_launcher_metadata,
    discover_launcher_metadata,
    discover_mod_pages,
    resolve_launcher_metadata,
    resolve_launcher_metadata_resolution,
)
from apps._mod import Mod, Mod_Manager
from apps._node_api import (
    JsonValue,
    NodeModUploadSource,
    optional_int as _optional_int,
    optional_string as _optional_string,
    power_level as _power_level,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
    required_text as _required_text,
    string_tuple as _string_tuple,
)
from apps._save_files import AppSaveEntry, AppSaveEntryKind
from apps._settings import Setting, Settings_Manager
from apps._updater import AppUpdateInfo, AppUpdateStatus
from apps.factorio import (
    FactorioModPortalCandidate,
    FactorioVanillaMod,
    factorio_mod_settings_path,
)
from apps.factorio.node_api import (
    FactorioModUpdateApplyResult,
    NodeFactorioModSettings,
    NodeModDependencyEntry,
    NodeModDependencyResolutionResult,
    NodeModPortalDependencyEntry,
    NodeModPortalInstallRequest,
    NodeModPortalResolveResult,
    NodeModPortalVersionEntry,
    NodeModPortalVersionList,
    NodeModUpdateCheckResult,
    NodeModUpdateDependency,
    NodeModUpdateDependencyAction,
    NodeModUpdateRequest,
    NodeModUpdateStatus,
    build_factorio_mod_settings_download_response as _build_factorio_mod_settings_download_response,
    build_factorio_mod_settings_state as _build_factorio_mod_settings_state,
    check_factorio_mod_update as _check_factorio_mod_update,
    check_mod_update as _check_factorio_mod_update_by_name,
    factorio_dependency_update_entry as _factorio_dependency_update_entry,
    factorio_dependency_update_summary as _factorio_dependency_update_summary,
    factorio_installed_mods_by_id as _factorio_installed_mods_by_id,
    factorio_mod_update_page_url as _factorio_mod_update_page_url,
    factorio_vanilla_mods_by_id as _factorio_vanilla_mods,
    factorio_mod_versions as _factorio_mod_versions,
    install_mod_from_link as _install_factorio_mod_from_link,
    list_installed_mod_versions as _list_installed_factorio_mod_versions,
    list_mod_link_versions as _list_factorio_mod_link_versions,
    resolve_mod_link_dependencies as _resolve_factorio_mod_link_dependencies,
    update_mod as _update_factorio_mod,
)
from apps.minecraft import (
    Minecraft,
    MinecraftRecipeMutation,
)
from apps.minecraft.node_api import (
    NodeMinecraftItemRegistryState,
    NodeMinecraftRecipeBookState,
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationRequest,
    NodeMinecraftRecipeMutationResult,
    NodeMinecraftRecipeWorkspaceState,
    apply_minecraft_recipe_mutation as _apply_minecraft_recipe_mutation,
    build_minecraft_item_icon_response as _build_minecraft_item_icon_response,
    build_minecraft_recipe_workspace_state as _build_minecraft_recipe_workspace_state,
    minecraft_item_icon_placeholder_svg as _minecraft_item_icon_placeholder_svg,
)
from apps.minecraft.pack_export import (
    MinecraftPackExportError,
    MinecraftPackSpec,
    PackFormat,
    PackPurpose,
    client_pack_kubejs_entries,
    discover_client_pack_kubejs_scripts,
    export_minecraft_pack,
)
from apps.sevendays import SevenDays
from apps.sevendays.node_api import (
    NodeSevenDaysSandboxOptionsState,
    build_sevendays_sandbox_options_state as _build_sevendays_sandbox_options_state,
)
from chat_hub import ChatEndpoint, ChatEndpointId, ChatEndpointKind, ChatEvent, ChatHub, ChatRoomUpdate
from font_assets import font_assets
from maintenance import MAX_RESTART_INTERVAL_MINUTES, MIN_RESTART_INTERVAL_MINUTES, MaintenanceService
from map_annotations import (
    AppMapAnnotationStore,
    MapAnnotationDraft,
    MapAnnotationList,
    MapAnnotationMutationResult,
    MapManifest,
    MapWorldSummary,
)
from map_cache import AppMapJsonCacheStore, MapJsonCacheEntry
from mod_web_auth import ModWebAuthService, ModWebUser
from node_auth import NodeAccessGrant, NodeApiScope, NodeTokenError, issue_node_token, verify_node_token
from restart_state import (
    RestartKind,
    mark_pending_process_restart,
    read_process_restart_record,
    read_voice_restart_record,
)
from restart_targets import RestartTarget

if TYPE_CHECKING:
    from _manager import App_Manager

_NODE_API_PREFIX = "/api/node"
_NODE_TOKEN_TTL_SECONDS = 15 * 60
_NODE_RESTART_DELAY_SECONDS = 0.25
_RELAY_TTS_FORWARD_TTL_SECONDS = 60
_APP_PLAYER_COUNT_TIMEOUT_SECONDS = 1.5
_APP_FOOTPRINT_CACHE_TTL_SECONDS = 60.0
_APP_TRANSITION_TTL_SECONDS = 15.0
_NODE_APP_ENTRY_CACHE_TTL_SECONDS = 5.0
_NODE_SYSTEM_SUMMARY_CACHE_TTL_SECONDS = 1.0
_LIVE_APP_RUNTIME_CACHE_TTL_SECONDS = 0.5
_FULL_APP_RUNTIME_CACHE_TTL_SECONDS = 2.0
_MOD_INVENTORY_CACHE_TTL_SECONDS = 5.0
_BULK_METADATA_DISCOVERY_CACHE_TTL_SECONDS = 60.0 * 60.0
_BULK_METADATA_DISCOVERY_CACHE_MAX_ENTRIES = 64
_LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS = 0.75
_LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS = 2.0
_NODE_SYSTEM_HISTORY_INTERVAL_SECONDS = 10.0
_NODE_SYSTEM_HISTORY_RETENTION_SECONDS = 60 * 60
_NODE_SYSTEM_HISTORY_MAX_SAMPLES = (
    _NODE_SYSTEM_HISTORY_RETENTION_SECONDS // int(_NODE_SYSTEM_HISTORY_INTERVAL_SECONDS)
)
_LOCAL_CONSOLE_STDOUT_STREAM_INTERVAL_SECONDS = 0.5
_NODE_CHAT_HISTORY_LIMIT = 100
_SQUAREMAP_REQUEST_TIMEOUT_SECONDS = 10.0
_MAP_SOURCE_HEADER_NAME = "X-Yukibot-Map-Source"
_MAP_CACHE_UPDATED_AT_HEADER_NAME = "X-Yukibot-Map-Cache-Updated-At"
_DEFAULT_REMOTE_CONFIG_READ_LEVEL = Power_Level.sudo
_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL = Power_Level.root
_NODE_API_SCOPE_WEB_LEVELS: dict[NodeApiScope, Power_Level] = {
    NodeApiScope.APPS_READ: Power_Level.visitor,
    NodeApiScope.MAP_READ: Power_Level.visitor,
    NodeApiScope.MAP_WRITE: Power_Level.user,
    NodeApiScope.CHAT_READ: Power_Level.visitor,
    NodeApiScope.CHAT_WRITE: Power_Level.visitor,
    NodeApiScope.MODS_READ: Power_Level.visitor,
    NodeApiScope.MODS_DOWNLOAD: Power_Level.user,
    NodeApiScope.MODS_WRITE: Power_Level.user,
    NodeApiScope.CONFIGS_READ: Power_Level.visitor,
    NodeApiScope.CONFIGS_WRITE: Power_Level.sudo,
    NodeApiScope.SAVES_READ: Power_Level.user,
    NodeApiScope.SAVES_DOWNLOAD: Power_Level.user,
    NodeApiScope.SAVES_WRITE: Power_Level.user,
    NodeApiScope.BLUEPRINTS_READ: Power_Level.user,
    NodeApiScope.BLUEPRINTS_WRITE: Power_Level.user,
    NodeApiScope.SETTINGS_READ: Power_Level.user,
    NodeApiScope.SETTINGS_WRITE: Power_Level.user,
    NodeApiScope.FILES_READ: Power_Level.user,
    NodeApiScope.FILES_DOWNLOAD: Power_Level.user,
    NodeApiScope.FILES_UPLOAD: Power_Level.user,
    NodeApiScope.APP_CONTROL: Power_Level.user,
    NodeApiScope.APP_MANAGE: Power_Level.sudo,
    NodeApiScope.NODE_OPERATE: Power_Level.sudo,
    NodeApiScope.NODE_MANAGE: Power_Level.root,
}
log = logging.getLogger(__name__)
traffic_log = logging.getLogger(config.LOGGER_TRAFFIC)

# Keep moved DTO imports visible from node_api during this staged refactor.
_NODE_API_COMPAT_EXPORTS: tuple[type[object], ...] = (
    NodeMinecraftItemRegistryState,
    NodeMinecraftRecipeBookState,
    NodeModPortalDependencyEntry,
    NodeModPortalResolveResult,
)

def _is_executor_shutdown_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and "cannot schedule new futures after shutdown" in str(error)


def _app_transition_state(
    payload: Mapping[str, object],
    key: str,
    *,
    default: "NodeAppTransitionState | None" = None,
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


class RelayTTSQueue(Protocol):
    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]: ...


class WebChatRelayPublisher(Protocol):
    async def publish_web_chat(
        self,
        *,
        room_id: str,
        session_id: str,
        author_display_name: str,
        author_id: str | None,
        discord_user_id: int | None,
        content: str,
        reply_to_event_id: str | None = None,
    ) -> ChatEvent: ...

    async def publish_chat_event(self, *, event: ChatEvent) -> ChatEvent: ...


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
            startup_defined=_required_bool(payload, "startup_defined")
            if "startup_defined" in payload
            else False,
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
            not isinstance(release_payload, Mapping)
            for release_payload in raw_client_pack_releases
        ):
            raise ValueError("Node app entry client_pack_releases is invalid.")
        if not isinstance(raw_client_pack_kubejs_scripts, list | tuple) or any(
            not isinstance(script_payload, Mapping)
            for script_payload in raw_client_pack_kubejs_scripts
        ):
            raise ValueError("Node app entry client_pack_kubejs_scripts is invalid.")
        client_pack_kubejs_scripts = tuple(
            ClientPackKubeJsScript.model_validate(script_payload)
            for script_payload in raw_client_pack_kubejs_scripts
        )
        if raw_client_pack_metadata is not None and not isinstance(raw_client_pack_metadata, Mapping):
            raise ValueError("Node app entry client_pack_metadata is invalid.")
        client_pack_metadata = (
            ClientPackMetadataConfig.model_validate(raw_client_pack_metadata)
            if raw_client_pack_metadata is not None
            else None
        )
        if not isinstance(raw_client_pack_file_previews, list | tuple) or any(
            not isinstance(preview_payload, Mapping)
            for preview_payload in raw_client_pack_file_previews
        ):
            raise ValueError("Node app entry client_pack_file_previews is invalid.")
        if not isinstance(client_pack_automated_changelog, str):
            raise ValueError("Node app entry client_pack_automated_changelog is invalid.")
        client_pack_file_previews = tuple(
            ClientPackFilePreview.from_mapping(cast(Mapping[str, object], preview_payload))
            for preview_payload in raw_client_pack_file_previews
        )
        client_pack_releases = tuple(
            ClientPackRelease.model_validate(release_payload)
            for release_payload in raw_client_pack_releases
        )
        if not client_pack_releases and (
            client_pack_published_version is not None
            and client_pack_published_changelog is not None
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
            "client_pack_releases": [
                release.model_dump(mode="json") for release in self.client_pack_releases
            ],
            "client_pack_kubejs_scripts": [
                script.model_dump(mode="json") for script in self.client_pack_kubejs_scripts
            ],
            "client_pack_metadata": (
                self.client_pack_metadata.model_dump(mode="json")
                if self.client_pack_metadata is not None
                else None
            ),
            "client_pack_file_previews": [
                preview.to_mapping() for preview in self.client_pack_file_previews
            ],
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


@dataclass(frozen=True, slots=True)
class NodeModSummary:
    total_count: int
    enabled_count: int
    disabled_count: int
    coremod_count: int
    downloadable_count: int
    non_downloadable_count: int
    client_only_count: int = 0
    client_pack_eligible_count: int = 0

    @property
    def server_enabled_count(self) -> int:
        return self.enabled_count

    @property
    def server_disabled_count(self) -> int:
        return self.disabled_count

    @property
    def server_loadable_count(self) -> int:
        return self.server_enabled_count + self.server_disabled_count

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModSummary:
        values: dict[str, int] = {}
        for key in (
            "total_count",
            "enabled_count",
            "disabled_count",
            "coremod_count",
            "downloadable_count",
            "non_downloadable_count",
        ):
            value: object | None = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Node mod summary {key} is invalid.")
            values[key] = value
        raw_client_only_count: object = payload.get("client_only_count", 0)
        if isinstance(raw_client_only_count, bool) or not isinstance(raw_client_only_count, int):
            raise ValueError("Node mod summary client_only_count is invalid.")
        raw_client_pack_eligible_count: object = payload.get(
            "client_pack_eligible_count",
            values["downloadable_count"],
        )
        if isinstance(raw_client_pack_eligible_count, bool) or not isinstance(raw_client_pack_eligible_count, int):
            raise ValueError("Node mod summary client_pack_eligible_count is invalid.")
        return cls(
            **values,
            client_only_count=raw_client_only_count,
            client_pack_eligible_count=raw_client_pack_eligible_count,
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "total_count": self.total_count,
            "enabled_count": self.enabled_count,
            "disabled_count": self.disabled_count,
            "coremod_count": self.coremod_count,
            "downloadable_count": self.downloadable_count,
            "non_downloadable_count": self.non_downloadable_count,
            "server_enabled_count": self.server_enabled_count,
            "server_disabled_count": self.server_disabled_count,
            "server_loadable_count": self.server_loadable_count,
            "client_only_count": self.client_only_count,
            "client_pack_eligible_count": self.client_pack_eligible_count,
        }


@dataclass(frozen=True, slots=True)
class NodeModEntry:
    name: str
    friendly: str
    enabled: bool
    mod_type: ModType
    coremod: bool
    downloadable: bool
    download_block_reason: str | None
    download_block_label: str | None
    origin: str
    version: str | None
    added: str
    size_bytes: int
    size_text: str
    placement: ModPlacement
    server_loadable: bool
    client_pack_eligible: bool
    archive_name: str
    source_path: str
    description: str | None = None
    client_path: str | None = None
    mod_pages: tuple[ModPageLink, ...] = ()
    metadata_overrides: ModMetadataOverrides = field(default_factory=ModMetadataOverrides)
    client_pack: ClientPackConfig = field(default_factory=ClientPackConfig)
    platforms: ModPlatformMetadata = field(default_factory=ModPlatformMetadata)

    @property
    def added_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.added)
        except ValueError as xcp:
            raise ValueError(f"Node mod {self.name!r} has an invalid added timestamp: {self.added!r}") from xcp

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModEntry:
        name: str = _required_string(payload, "name")
        friendly: str = _required_string(payload, "friendly")
        client_path: str | None = _optional_string(payload, "client_path")
        enabled: bool = _required_bool(payload, "enabled")
        coremod: bool = _required_bool(payload, "coremod")
        raw_mod_type: str | None = _optional_string(payload, "mod_type")
        downloadable: bool = _required_bool(payload, "downloadable")
        download_block_reason: str | None = _optional_string(payload, "download_block_reason")
        origin: str = _required_string(payload, "origin")
        added: str = _required_string(payload, "added")
        size_bytes: int = _required_int(payload, "size_bytes")
        size_text: str = _required_string(payload, "size_text")
        raw_client_pack: object | None = payload.get("client_pack")
        if raw_client_pack is not None and not isinstance(raw_client_pack, Mapping):
            raise ValueError("Node mod client_pack is invalid.")
        raw_metadata_overrides: object | None = payload.get("metadata_overrides")
        if raw_metadata_overrides is not None and not isinstance(raw_metadata_overrides, Mapping):
            raise ValueError("Node mod metadata overrides are invalid.")
        raw_mod_pages: object = payload.get("mod_pages", ())
        if not isinstance(raw_mod_pages, (list, tuple)):
            raise ValueError("Node mod pages are invalid.")
        raw_platforms: object | None = payload.get("platforms")
        if raw_platforms is not None and not isinstance(raw_platforms, Mapping):
            raise ValueError("Node mod platform metadata is invalid.")
        if raw_mod_type is not None:
            mod_type: ModType = ModType(raw_mod_type)
        elif download_block_reason == ModDownloadBlockReason.BUILTIN.value:
            mod_type = ModType.BUILTIN
        elif coremod:
            mod_type = ModType.COREMOD
        else:
            mod_type = ModType.REGULAR
        client_pack_payload: dict[Any, object] = {} if raw_client_pack is None else dict(raw_client_pack)
        client_pack_payload.setdefault(
            "included_in_client",
            mod_type.included_in_client_by_default,
        )
        client_pack: ClientPackConfig = ClientPackConfig.model_validate(client_pack_payload)
        raw_placement: str | None = _optional_string(payload, "placement")
        placement: ModPlacement = (
            (ModPlacement.SERVER_ENABLED if enabled else ModPlacement.SERVER_DISABLED)
            if raw_placement is None
            else ModPlacement(raw_placement)
        )
        if raw_placement is not None and enabled is not placement.enabled:
            raise ValueError("Node mod enabled state conflicts with placement.")
        raw_server_loadable: object | None = payload.get("server_loadable")
        server_loadable: bool = (
            placement.server_loadable if raw_server_loadable is None else _required_bool(payload, "server_loadable")
        )
        if server_loadable is not placement.server_loadable:
            raise ValueError("Node mod server_loadable conflicts with placement.")
        raw_client_pack_eligible: object | None = payload.get("client_pack_eligible")
        expected_client_pack_eligible: bool = (
            is_client_pack_candidate(placement, mod_type.side) and client_pack.included_in_client and downloadable
        )
        client_pack_eligible: bool = (
            expected_client_pack_eligible
            if raw_client_pack_eligible is None
            else _required_bool(payload, "client_pack_eligible")
        )
        if client_pack_eligible is not expected_client_pack_eligible:
            raise ValueError("Node mod client_pack_eligible conflicts with classification.")
        return cls(
            name=name,
            friendly=friendly,
            client_path=client_path,
            enabled=enabled,
            mod_type=mod_type,
            coremod=coremod,
            downloadable=downloadable,
            download_block_reason=download_block_reason,
            download_block_label=_optional_string(payload, "download_block_label"),
            origin=origin,
            version=_optional_string(payload, "version"),
            added=added,
            size_bytes=size_bytes,
            size_text=size_text,
            placement=placement,
            server_loadable=server_loadable,
            client_pack_eligible=client_pack_eligible,
            archive_name=_optional_string(payload, "archive_name") or name,
            source_path=_optional_string(payload, "source_path") or client_path or name,
            description=_optional_string(payload, "description"),
            mod_pages=tuple(
                ModPageLink.model_validate(page)
                for page in cast(list[object] | tuple[object, ...], raw_mod_pages)
            ),
            metadata_overrides=(
                ModMetadataOverrides()
                if raw_metadata_overrides is None
                else ModMetadataOverrides.model_validate(dict(raw_metadata_overrides))
            ),
            client_pack=client_pack,
            platforms=(
                ModPlatformMetadata()
                if raw_platforms is None
                else ModPlatformMetadata.model_validate(dict(raw_platforms))
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "friendly": self.friendly,
            "client_path": self.client_path,
            "enabled": self.enabled,
            "mod_type": self.mod_type.value,
            "coremod": self.coremod,
            "downloadable": self.downloadable,
            "download_block_reason": self.download_block_reason,
            "download_block_label": self.download_block_label,
            "origin": self.origin,
            "version": self.version,
            "added": self.added,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "placement": self.placement.value,
            "server_loadable": self.server_loadable,
            "client_pack_eligible": self.client_pack_eligible,
            "archive_name": self.archive_name,
            "source_path": self.source_path,
            "description": self.description,
            "mod_pages": [page.model_dump(mode="json") for page in self.mod_pages],
            "metadata_overrides": self.metadata_overrides.model_dump(mode="json"),
            "client_pack": self.client_pack.model_dump(mode="json"),
            "platforms": self.platforms.model_dump(mode="json"),
        }


class NodeModMutationAction(StrEnum):
    ENABLE = "enable"
    DISABLE = "disable"
    TOGGLE_COREMOD = "toggle_coremod"
    TOGGLE_DOWNLOAD_BLOCK = "toggle_download_block"
    UPDATE_PROPERTIES = "update_properties"
    DELETE = "delete"


def _get_mod_or_404(manager: Mod_Manager, mod_name: str) -> Mod:
    try:
        return manager.get(mod_name)
    except ModuleNotFoundError as xcp:
        raise _http_exception(404, str(xcp)) from xcp


def required_mod_mutation_level(
    action: NodeModMutationAction,
    *,
    is_protected: bool = False,
) -> Power_Level:
    if action in {NodeModMutationAction.ENABLE, NodeModMutationAction.DISABLE}:
        return Power_Level.sudo if is_protected else Power_Level.admin
    if action in {
        NodeModMutationAction.TOGGLE_COREMOD,
        NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK,
        NodeModMutationAction.UPDATE_PROPERTIES,
        NodeModMutationAction.DELETE,
    }:
        return Power_Level.sudo
    raise ValueError(f"Unsupported mod mutation action: {action}")


class NodeModMutationRequest(BaseModel):
    action: NodeModMutationAction


class NodeModPropertiesUpdateRequest(BaseModel):
    mod_type: ModType
    download_block_reason: ModDownloadBlockReason | None
    metadata_overrides: ModMetadataOverrides
    mod_pages: tuple[ModPageLink, ...] | None = None
    client_pack: ClientPackConfig | None = None
    launcher_urls: LauncherProviderUrls = Field(default_factory=LauncherProviderUrls)


class NodeLauncherProviderSelectionRequest(BaseModel):
    providers: tuple[Provider, ...] | None = None

    model_config = ConfigDict(extra="forbid")


class NodeModMetadataFetchRequest(NodeLauncherProviderSelectionRequest):
    launcher_urls: LauncherProviderUrls


class NodeModMetadataResolveRequest(NodeLauncherProviderSelectionRequest):
    mod_pages: tuple[ModPageLink, ...]
    existing_launcher_urls: LauncherProviderUrls = Field(default_factory=LauncherProviderUrls)


class NodeModPageResolveRequest(NodeLauncherProviderSelectionRequest):
    mod_pages: tuple[ModPageLink, ...]


class NodeBulkLauncherMetadataRequest(BaseModel):
    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    mod_names: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("mod_names", mode="before")
    @classmethod
    def validate_mod_names(cls, raw: object) -> object:
        if not isinstance(raw, (list, tuple)):
            raise TypeError("bulk launcher metadata mod names must be a list")
        return raw

    @model_validator(mode="after")
    def validate_unique_mod_names(self) -> NodeBulkLauncherMetadataRequest:
        if any(not name for name in self.mod_names):
            raise ValueError("bulk launcher metadata mod names must not be blank")
        if len(self.mod_names) != len(set(self.mod_names)):
            raise ValueError("bulk launcher metadata mod names must be unique")
        return self


class NodeBulkLauncherMetadataApplyRequest(NodeBulkLauncherMetadataRequest):
    discovery_operation_id: uuid.UUID
    apply_suggested_type_mod_names: tuple[str, ...] = ()

    @field_validator("apply_suggested_type_mod_names", mode="before")
    @classmethod
    def validate_apply_suggested_type_mod_names(cls, raw: object) -> object:
        if not isinstance(raw, (list, tuple)):
            raise TypeError("bulk launcher metadata type selections must be a list")
        return raw

    @model_validator(mode="after")
    def validate_type_selections(self) -> NodeBulkLauncherMetadataApplyRequest:
        selected_names = self.apply_suggested_type_mod_names
        if any(not name for name in selected_names):
            raise ValueError("bulk launcher metadata type selection names must not be blank")
        if len(selected_names) != len(set(selected_names)):
            raise ValueError("bulk launcher metadata type selection names must be unique")
        if not set(selected_names).issubset(self.mod_names):
            raise ValueError("bulk launcher metadata type selections must be selected for apply")
        return self


class NodeBulkLauncherMetadataApplyResult(BaseModel):
    discovery: BulkLauncherMetadataDiscovery
    applied_mod_names: tuple[str, ...] = ()
    applied_type_mod_names: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeClientPackModConfigUpdate(BaseModel):
    mod_name: str = Field(min_length=1)
    client_pack: ClientPackConfig


class NodeClientPackConfigUpdateRequest(BaseModel):
    mods: tuple[NodeClientPackModConfigUpdate, ...]
    kubejs_scripts: tuple[ClientPackKubeJsScript, ...] | None = None
    metadata: ClientPackMetadataConfig | None = None

    @model_validator(mode="after")
    def validate_unique_mod_names(self) -> NodeClientPackConfigUpdateRequest:
        mod_names = tuple(update.mod_name for update in self.mods)
        if len(mod_names) != len(set(mod_names)):
            raise ValueError("client-pack configuration contains duplicate mod names")
        if self.kubejs_scripts is not None:
            script_paths = tuple(script.relative_path for script in self.kubejs_scripts)
            if len(script_paths) != len(set(script_paths)):
                raise ValueError("client-pack configuration contains duplicate KubeJS script paths")
        return self


class NodeClientPackPublishRequest(BaseModel):
    changelog: str = Field(min_length=1, max_length=CLIENT_PACK_CHANGELOG_MAX_LENGTH)

    @field_validator("changelog", mode="before")
    @classmethod
    def validate_changelog(cls, value: object) -> str:
        changelog = normalise_client_pack_changelog(value, required=True)
        assert changelog is not None
        return changelog


@dataclass(frozen=True, slots=True)
class NodeModMutationResult:
    app_name: str
    app_friendly: str
    node: str
    mod_name: str
    action: NodeModMutationAction
    message: str
    mod: NodeModEntry | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModMutationResult":
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        mod_name = _required_string(payload, "mod_name")
        message = _required_string(payload, "message")
        raw_action = _required_string(payload, "action")
        try:
            action = NodeModMutationAction(raw_action)
        except ValueError as xcp:
            raise ValueError("Node mod mutation action is invalid.") from xcp
        raw_mod = payload.get("mod")
        if raw_mod is not None and not isinstance(raw_mod, Mapping):
            raise ValueError("Node mod mutation mod is invalid.")
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            mod_name=mod_name,
            action=action,
            message=message,
            mod=NodeModEntry.from_mapping(raw_mod) if raw_mod is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "mod_name": self.mod_name,
            "action": self.action.value,
            "message": self.message,
            "mod": self.mod.to_mapping() if self.mod is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeModUploadResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    mod: NodeModEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModUploadResult":
        raw_mod = payload.get("mod")
        if not isinstance(raw_mod, Mapping):
            raise ValueError("Node mod upload mod is invalid.")
        mod_payload = cast(Mapping[str, object], raw_mod)
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            mod=NodeModEntry.from_mapping(mod_payload),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "mod": self.mod.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeModUploadBatchResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    mods: tuple[NodeModEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeModUploadBatchResult":
        raw_mods = payload.get("mods")
        if isinstance(raw_mods, str) or not isinstance(raw_mods, Sequence):
            raise ValueError("Node mod upload mods are invalid.")
        mods: list[NodeModEntry] = []
        for raw_mod in raw_mods:
            if not isinstance(raw_mod, Mapping):
                raise ValueError("Node mod upload mods are invalid.")
            mods.append(NodeModEntry.from_mapping(cast(Mapping[str, object], raw_mod)))
        if not mods:
            raise ValueError("Node mod upload mods are invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            mods=tuple(mods),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "mods": [mod.to_mapping() for mod in self.mods],
        }


@dataclass(frozen=True, slots=True)
class _ResolvedModUploadFile:
    upload: UploadFile
    upload_name: str


class NodeAppMutationAction(StrEnum):
    START = "start"
    STOP = "stop"
    KILL = "kill"
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
class NodeCapacityMutationResult:
    node: str
    message: str
    capacity: config.NodeCapacityProfile

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeCapacityMutationResult":
        raw_capacity = payload.get("capacity")
        if not isinstance(raw_capacity, Mapping):
            raise ValueError("Node capacity mutation capacity is invalid.")
        return cls(
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            capacity=config.NodeCapacityProfile.model_validate(raw_capacity),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "capacity": self.capacity.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeDiskEntry:
    mountpoint: str
    display_name: str
    is_activity: bool
    is_primary: bool
    is_secondary: bool
    is_bot_disk: bool

    def __post_init__(self) -> None:
        if not self.mountpoint.strip() or not self.display_name.strip():
            raise ValueError("Node disk mountpoint and display name must not be blank.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeDiskEntry":
        return cls(
            mountpoint=_required_string(payload, "mountpoint"),
            display_name=_required_string(payload, "display_name"),
            is_activity=_required_bool(payload, "is_activity"),
            is_primary=_required_bool(payload, "is_primary"),
            is_secondary=_required_bool(payload, "is_secondary"),
            is_bot_disk=_required_bool(payload, "is_bot_disk"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mountpoint": self.mountpoint,
            "display_name": self.display_name,
            "is_activity": self.is_activity,
            "is_primary": self.is_primary,
            "is_secondary": self.is_secondary,
            "is_bot_disk": self.is_bot_disk,
        }


@dataclass(frozen=True, slots=True)
class NodeDiskManagementState:
    node: str
    disks: tuple[NodeDiskEntry, ...]
    preferences: config.PersistedDiskPreferences

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeDiskManagementState":
        raw_disks = payload.get("disks", ())
        if not isinstance(raw_disks, Sequence) or isinstance(raw_disks, (str, bytes)):
            raise ValueError("Node disk management disks are invalid.")
        disks: list[NodeDiskEntry] = []
        for raw_disk in raw_disks:
            if not isinstance(raw_disk, Mapping):
                raise ValueError("Node disk management disks are invalid.")
            disks.append(NodeDiskEntry.from_mapping(raw_disk))
        raw_preferences = payload.get("preferences")
        if not isinstance(raw_preferences, Mapping):
            raise ValueError("Node disk management preferences are invalid.")
        return cls(
            node=_required_string(payload, "node"),
            disks=tuple(disks),
            preferences=config.PersistedDiskPreferences.model_validate(raw_preferences),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "disks": [disk.to_mapping() for disk in self.disks],
            "preferences": self.preferences.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeDiskSettingsMutationResult:
    node: str
    message: str
    settings: NodeDiskManagementState

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeDiskSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("Node disk settings mutation settings are invalid.")
        return cls(
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            settings=NodeDiskManagementState.from_mapping(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeFontSourceSettingsMutationResult:
    node: str
    message: str
    settings: config.NodeFontSourceSettings

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeFontSourceSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("Node font source settings mutation settings are invalid.")
        return cls(
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            settings=config.NodeFontSourceSettings.model_validate(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeDiscordSettingsMutationResult:
    node: str
    message: str
    settings: config.DiscordSettings

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeDiscordSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("Node Discord settings mutation settings are invalid.")
        return cls(
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            settings=config.DiscordSettings.model_validate(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.model_dump(mode="json"),
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
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        message = _required_string(payload, "message")
        raw_action = _required_string(payload, "action")
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
class _SquaremapProxyResponse:
    content: bytes
    media_type: str | None
    headers: tuple[tuple[str, str], ...] = ()
    is_stale: bool = False
    cache_updated_at_unix_ms: int | None = None


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

        try:
            parsed_relay_support = ChatRelaySupport(relay_support)
        except ValueError as xcp:
            raise ValueError("Node app runtime summary relay_support is invalid.") from xcp
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
        if raw_entries is not None and (
            not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes))
        ):
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


@dataclass(frozen=True, slots=True)
class _TimedModInventory:
    captured_at_seconds: float
    summary: NodeModSummary
    mods: tuple[NodeModEntry, ...]


@dataclass(frozen=True, slots=True)
class _CachedBulkMetadataDiscovery:
    captured_at_seconds: float
    discovery: BulkLauncherMetadataDiscovery


@dataclass(frozen=True, slots=True)
class NodeSystemDiskSummary:
    mountpoint: str
    label: str
    percent: int
    free_bytes: int
    total_bytes: int

    def __post_init__(self) -> None:
        if not self.mountpoint.strip() or not self.label.strip():
            raise ValueError("System disk mountpoint and label must not be blank.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSystemDiskSummary:
        return cls(
            mountpoint=_required_string(payload, "mountpoint"),
            label=_required_string(payload, "label"),
            percent=_required_int(payload, "percent"),
            free_bytes=_required_int(payload, "free_bytes"),
            total_bytes=_required_int(payload, "total_bytes"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mountpoint": self.mountpoint,
            "label": self.label,
            "percent": self.percent,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemSummary:
    cpu_percent: int | None
    ram_percent: int | None
    ram_used_bytes: int | None
    ram_total_bytes: int | None
    storage_percent: int | None
    storage_free_bytes: int | None
    storage_total_bytes: int | None
    cpu_per_core_percent: tuple[int, ...] = ()
    disks: tuple[NodeSystemDiskSummary, ...] = ()
    bot_uptime_seconds: int | None = None
    uptime_seconds: int | None = None
    cpu_points_available: int | None = None
    cpu_points_capacity: int | None = None
    ram_points_available: int | None = None
    ram_points_capacity: int | None = None
    running_names: tuple[str, ...] = ()
    running_app_ids: tuple[str, ...] = ()
    running_app_scopes: tuple[str, ...] = ()
    start_blocked_app_ids: tuple[str, ...] = ()
    captured_at_epoch_seconds: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSystemSummary:
        raw_cpu_per_core = payload.get("cpu_per_core_percent", ())
        if not isinstance(raw_cpu_per_core, Sequence) or isinstance(raw_cpu_per_core, (str, bytes)):
            raise ValueError("cpu_per_core_percent is invalid.")
        cpu_per_core_percent: list[int] = []
        for raw_percent in raw_cpu_per_core:
            if isinstance(raw_percent, bool) or not isinstance(raw_percent, int):
                raise ValueError("cpu_per_core_percent is invalid.")
            cpu_per_core_percent.append(raw_percent)
        raw_disks = payload.get("disks", ())
        if not isinstance(raw_disks, Sequence) or isinstance(raw_disks, (str, bytes)):
            raise ValueError("disks is invalid.")
        disks: list[NodeSystemDiskSummary] = []
        for raw_disk in raw_disks:
            if not isinstance(raw_disk, Mapping):
                raise ValueError("disks is invalid.")
            disks.append(NodeSystemDiskSummary.from_mapping(raw_disk))
        raw_running_names = payload.get("running_names", ())
        if not isinstance(raw_running_names, Sequence) or isinstance(raw_running_names, (str, bytes)):
            raise ValueError("running_names is invalid.")
        running_names: list[str] = []
        for raw_name in raw_running_names:
            if not isinstance(raw_name, str) or not raw_name:
                raise ValueError("running_names is invalid.")
            running_names.append(raw_name)
        raw_running_app_ids = payload.get("running_app_ids", ())
        if not isinstance(raw_running_app_ids, Sequence) or isinstance(raw_running_app_ids, (str, bytes)):
            raise ValueError("running_app_ids is invalid.")
        running_app_ids: list[str] = []
        for raw_app_id in raw_running_app_ids:
            if not isinstance(raw_app_id, str) or not raw_app_id:
                raise ValueError("running_app_ids is invalid.")
            running_app_ids.append(raw_app_id)
        raw_running_app_scopes = payload.get("running_app_scopes", ())
        if not isinstance(raw_running_app_scopes, Sequence) or isinstance(raw_running_app_scopes, (str, bytes)):
            raise ValueError("running_app_scopes is invalid.")
        running_app_scopes: list[str] = []
        for raw_scope in raw_running_app_scopes:
            if not isinstance(raw_scope, str) or not raw_scope:
                raise ValueError("running_app_scopes is invalid.")
            running_app_scopes.append(raw_scope)
        raw_start_blocked_app_ids = payload.get("start_blocked_app_ids", ())
        if not isinstance(raw_start_blocked_app_ids, Sequence) or isinstance(raw_start_blocked_app_ids, (str, bytes)):
            raise ValueError("start_blocked_app_ids is invalid.")
        start_blocked_app_ids: list[str] = []
        for raw_app_id in raw_start_blocked_app_ids:
            if not isinstance(raw_app_id, str) or not raw_app_id:
                raise ValueError("start_blocked_app_ids is invalid.")
            start_blocked_app_ids.append(raw_app_id)
        return cls(
            cpu_percent=_optional_int(payload, "cpu_percent"),
            ram_percent=_optional_int(payload, "ram_percent"),
            ram_used_bytes=_optional_int(payload, "ram_used_bytes"),
            ram_total_bytes=_optional_int(payload, "ram_total_bytes"),
            storage_percent=_optional_int(payload, "storage_percent"),
            storage_free_bytes=_optional_int(payload, "storage_free_bytes"),
            storage_total_bytes=_optional_int(payload, "storage_total_bytes"),
            cpu_per_core_percent=tuple(cpu_per_core_percent),
            disks=tuple(disks),
            bot_uptime_seconds=_optional_int(payload, "bot_uptime_seconds"),
            uptime_seconds=_optional_int(payload, "uptime_seconds"),
            cpu_points_available=_optional_int(payload, "cpu_points_available"),
            cpu_points_capacity=_optional_int(payload, "cpu_points_capacity"),
            ram_points_available=_optional_int(payload, "ram_points_available"),
            ram_points_capacity=_optional_int(payload, "ram_points_capacity"),
            running_names=tuple(running_names),
            running_app_ids=tuple(running_app_ids),
            running_app_scopes=tuple(running_app_scopes),
            start_blocked_app_ids=tuple(start_blocked_app_ids),
            captured_at_epoch_seconds=_optional_int(payload, "captured_at_epoch_seconds"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "ram_used_bytes": self.ram_used_bytes,
            "ram_total_bytes": self.ram_total_bytes,
            "storage_percent": self.storage_percent,
            "storage_free_bytes": self.storage_free_bytes,
            "storage_total_bytes": self.storage_total_bytes,
            "cpu_per_core_percent": list(self.cpu_per_core_percent),
            "disks": [disk.to_mapping() for disk in self.disks],
            "bot_uptime_seconds": self.bot_uptime_seconds,
            "uptime_seconds": self.uptime_seconds,
            "cpu_points_available": self.cpu_points_available,
            "cpu_points_capacity": self.cpu_points_capacity,
            "ram_points_available": self.ram_points_available,
            "ram_points_capacity": self.ram_points_capacity,
            "running_names": list(self.running_names),
            "running_app_ids": list(self.running_app_ids),
            "running_app_scopes": list(self.running_app_scopes),
            "start_blocked_app_ids": list(self.start_blocked_app_ids),
            "captured_at_epoch_seconds": self.captured_at_epoch_seconds,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemSample:
    captured_at_epoch_seconds: int
    cpu_percent: int | None
    ram_percent: int | None
    storage_percent: int | None

    @classmethod
    def from_summary(cls, summary: NodeSystemSummary) -> NodeSystemSample:
        captured_at = summary.captured_at_epoch_seconds
        if captured_at is None:
            raise ValueError("System summary capture time is required for history samples.")
        return cls(
            captured_at_epoch_seconds=captured_at,
            cpu_percent=summary.cpu_percent,
            ram_percent=summary.ram_percent,
            storage_percent=summary.storage_percent,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSystemSample:
        captured_at = _required_int(payload, "captured_at_epoch_seconds")
        if captured_at < 0:
            raise ValueError("System sample capture time must not be negative.")
        return cls(
            captured_at_epoch_seconds=captured_at,
            cpu_percent=_optional_int(payload, "cpu_percent"),
            ram_percent=_optional_int(payload, "ram_percent"),
            storage_percent=_optional_int(payload, "storage_percent"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "captured_at_epoch_seconds": self.captured_at_epoch_seconds,
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "storage_percent": self.storage_percent,
        }


@dataclass(frozen=True, slots=True)
class NodeSystemHistory:
    retention_seconds: int
    sample_interval_seconds: int
    samples: tuple[NodeSystemSample, ...]

    def __post_init__(self) -> None:
        if self.retention_seconds <= 0 or self.sample_interval_seconds <= 0:
            raise ValueError("System history timing values must be positive.")
        timestamps = tuple(sample.captured_at_epoch_seconds for sample in self.samples)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("System history samples must be chronological.")

    @classmethod
    def empty(cls) -> NodeSystemHistory:
        return cls(
            retention_seconds=_NODE_SYSTEM_HISTORY_RETENTION_SECONDS,
            sample_interval_seconds=int(_NODE_SYSTEM_HISTORY_INTERVAL_SECONDS),
            samples=(),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSystemHistory:
        raw_samples = payload.get("samples")
        if not isinstance(raw_samples, Sequence) or isinstance(raw_samples, (str, bytes)):
            raise ValueError("System history samples are invalid.")
        samples: list[NodeSystemSample] = []
        for raw_sample in raw_samples:
            if not isinstance(raw_sample, Mapping):
                raise ValueError("System history sample is invalid.")
            samples.append(NodeSystemSample.from_mapping(raw_sample))
        return cls(
            retention_seconds=_required_int(payload, "retention_seconds"),
            sample_interval_seconds=_required_int(payload, "sample_interval_seconds"),
            samples=tuple(samples),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "retention_seconds": self.retention_seconds,
            "sample_interval_seconds": self.sample_interval_seconds,
            "samples": [sample.to_mapping() for sample in self.samples],
        }


class NodeSystemAction(StrEnum):
    RESTART_PROCESS = "restart_process"
    REBOOT_HOST = "reboot_host"
    RESTART_PORTAL = "restart_portal"


type NodeSystemActionHandler = Callable[[NodeSystemAction, bool, bool], None]


_NODE_SYSTEM_ACTION_LABELS: dict[NodeSystemAction, str] = {
    NodeSystemAction.RESTART_PROCESS: "process restart",
    NodeSystemAction.REBOOT_HOST: "host reboot",
    NodeSystemAction.RESTART_PORTAL: "Portal restart",
}


@dataclass(frozen=True, slots=True)
class NodeSystemActionResult:
    node: str
    action: NodeSystemAction
    message: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSystemActionResult:
        raw_action = _required_string(payload, "action")
        try:
            action = NodeSystemAction(raw_action)
        except ValueError as xcp:
            raise ValueError("Node system action result action is invalid.") from xcp
        return cls(
            node=_required_string(payload, "node"),
            action=action,
            message=_required_string(payload, "message"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "action": self.action.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class NodeRestartRecord:
    timestamp: int
    kind: RestartKind

    def __post_init__(self) -> None:
        if self.timestamp <= 0:
            raise ValueError("Node restart record timestamp must be positive Unix seconds.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeRestartRecord:
        raw_kind = _required_string(payload, "kind")
        try:
            kind = RestartKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("Node restart record kind is invalid.") from xcp
        return cls(timestamp=_required_int(payload, "timestamp"), kind=kind)

    def to_mapping(self) -> dict[str, object]:
        return {"timestamp": self.timestamp, "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class NodeRestartState:
    node: str
    process: NodeRestartRecord
    voice: NodeRestartRecord | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeRestartState:
        raw_process = payload.get("process")
        if not isinstance(raw_process, Mapping):
            raise ValueError("Node process restart record is invalid.")
        raw_voice = payload.get("voice")
        if raw_voice is not None and not isinstance(raw_voice, Mapping):
            raise ValueError("Node voice restart record is invalid.")
        return cls(
            node=_required_string(payload, "node"),
            process=NodeRestartRecord.from_mapping(raw_process),
            voice=None if raw_voice is None else NodeRestartRecord.from_mapping(raw_voice),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "process": self.process.to_mapping(),
            "voice": None if self.voice is None else self.voice.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeRestartScheduleEntry:
    target: RestartTarget
    enabled: bool
    interval_minutes: int
    anchor_timestamp: int | None
    last_triggered_timestamp: int | None
    next_restart_timestamp: int | None
    skipped_through_timestamp: int | None

    def __post_init__(self) -> None:
        if not MIN_RESTART_INTERVAL_MINUTES <= self.interval_minutes <= MAX_RESTART_INTERVAL_MINUTES:
            raise ValueError("Node restart schedule interval is invalid.")
        if self.enabled and (self.anchor_timestamp is None or self.next_restart_timestamp is None):
            raise ValueError("Enabled node restart schedules require anchor and next-restart timestamps.")
        if not self.enabled and self.next_restart_timestamp is not None:
            raise ValueError("Disabled node restart schedules cannot have a next-restart timestamp.")
        for field_name, value in (
            ("anchor_timestamp", self.anchor_timestamp),
            ("last_triggered_timestamp", self.last_triggered_timestamp),
            ("next_restart_timestamp", self.next_restart_timestamp),
            ("skipped_through_timestamp", self.skipped_through_timestamp),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"Node restart schedule {field_name} must be positive Unix seconds.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeRestartScheduleEntry:
        raw_target = _required_string(payload, "target")
        try:
            target = RestartTarget(raw_target)
        except ValueError as xcp:
            raise ValueError("Node restart schedule target is invalid.") from xcp
        raw_anchor_timestamp = payload.get("anchor_timestamp")
        raw_last_triggered_timestamp = payload.get("last_triggered_timestamp")
        raw_next_restart_timestamp = payload.get("next_restart_timestamp")
        raw_skipped_through_timestamp = payload.get("skipped_through_timestamp")
        if isinstance(raw_anchor_timestamp, bool) or (
            raw_anchor_timestamp is not None and not isinstance(raw_anchor_timestamp, int)
        ):
            raise ValueError("Node restart schedule anchor timestamp is invalid.")
        if isinstance(raw_last_triggered_timestamp, bool) or (
            raw_last_triggered_timestamp is not None and not isinstance(raw_last_triggered_timestamp, int)
        ):
            raise ValueError("Node restart schedule last triggered timestamp is invalid.")
        if isinstance(raw_next_restart_timestamp, bool) or (
            raw_next_restart_timestamp is not None and not isinstance(raw_next_restart_timestamp, int)
        ):
            raise ValueError("Node restart schedule next restart timestamp is invalid.")
        if isinstance(raw_skipped_through_timestamp, bool) or (
            raw_skipped_through_timestamp is not None and not isinstance(raw_skipped_through_timestamp, int)
        ):
            raise ValueError("Node restart schedule skipped-through timestamp is invalid.")
        return cls(
            target=target,
            enabled=_required_bool(payload, "enabled"),
            interval_minutes=_required_int(payload, "interval_minutes"),
            anchor_timestamp=raw_anchor_timestamp,
            last_triggered_timestamp=raw_last_triggered_timestamp,
            next_restart_timestamp=raw_next_restart_timestamp,
            skipped_through_timestamp=raw_skipped_through_timestamp,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "anchor_timestamp": self.anchor_timestamp,
            "last_triggered_timestamp": self.last_triggered_timestamp,
            "next_restart_timestamp": self.next_restart_timestamp,
            "skipped_through_timestamp": self.skipped_through_timestamp,
        }


@dataclass(frozen=True, slots=True)
class NodeRestartScheduleState:
    node: str
    schedules: tuple[NodeRestartScheduleEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeRestartScheduleState:
        raw_schedules = payload.get("schedules")
        if not isinstance(raw_schedules, Sequence) or isinstance(raw_schedules, (str, bytes)):
            raise ValueError("Node restart schedules are invalid.")
        schedules: list[NodeRestartScheduleEntry] = []
        for raw_schedule in raw_schedules:
            if not isinstance(raw_schedule, Mapping):
                raise ValueError("Node restart schedule is invalid.")
            schedules.append(NodeRestartScheduleEntry.from_mapping(raw_schedule))
        return cls(node=_required_string(payload, "node"), schedules=tuple(schedules))

    def to_mapping(self) -> dict[str, object]:
        return {"node": self.node, "schedules": [schedule.to_mapping() for schedule in self.schedules]}


@dataclass(frozen=True, slots=True)
class NodeModList:
    app_name: str
    app_friendly: str
    node: str
    summary: NodeModSummary
    mods: tuple[NodeModEntry, ...]
    app_stats: NodeAppRuntimeSummary | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModList:
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        raw_summary = payload.get("summary")
        raw_app_stats = payload.get("app_stats")
        if not isinstance(raw_summary, Mapping):
            raise ValueError("Node mod list summary is invalid.")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node mod list app_stats are invalid.")
        raw_mods = payload.get("mods")
        if not isinstance(raw_mods, Sequence) or isinstance(raw_mods, (str, bytes)):
            raise ValueError("Node mod list mods are invalid.")
        mods: list[NodeModEntry] = []
        for raw_mod in raw_mods:
            if not isinstance(raw_mod, Mapping):
                raise ValueError("Node mod list contains an invalid mod entry.")
            mods.append(NodeModEntry.from_mapping(raw_mod))
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            summary=NodeModSummary.from_mapping(raw_summary),
            mods=tuple(mods),
            app_stats=NodeAppRuntimeSummary.from_mapping(raw_app_stats) if raw_app_stats is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "summary": self.summary.to_mapping(),
            "mods": [mod.to_mapping() for mod in self.mods],
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeConfigEntry:
    id: str
    label: str
    relative_path: str
    root_id: str
    root_label: str
    kind: str
    read_power_level: Power_Level
    size_bytes: int
    size_text: str
    modified_at: str
    write_power_level: Power_Level = _DEFAULT_REMOTE_CONFIG_WRITE_LEVEL

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigEntry:
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            root_id=_required_string(payload, "root_id"),
            root_label=_required_string(payload, "root_label"),
            kind=_required_string(payload, "kind"),
            read_power_level=_power_level(payload, "read_power_level", default=_DEFAULT_REMOTE_CONFIG_READ_LEVEL),
            write_power_level=_power_level(payload, "write_power_level", default=_DEFAULT_REMOTE_CONFIG_WRITE_LEVEL),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "root_id": self.root_id,
            "root_label": self.root_label,
            "kind": self.kind,
            "read_power_level": self.read_power_level.name,
            "write_power_level": self.write_power_level.name,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True, slots=True)
class NodeConfigList:
    app_name: str
    app_friendly: str
    node: str
    configs: tuple[NodeConfigEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigList:
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        raw_configs = payload.get("configs")
        if not isinstance(raw_configs, Sequence) or isinstance(raw_configs, (str, bytes)):
            raise ValueError("Node config list configs are invalid.")
        configs: list[NodeConfigEntry] = []
        for raw_config in raw_configs:
            if not isinstance(raw_config, Mapping):
                raise ValueError("Node config list contains an invalid config entry.")
            configs.append(NodeConfigEntry.from_mapping(raw_config))
        return cls(app_name=app_name, app_friendly=app_friendly, node=node, configs=tuple(configs))

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "configs": [entry.to_mapping() for entry in self.configs],
        }


@dataclass(frozen=True, slots=True)
class NodeConfigContent:
    app_name: str
    app_friendly: str
    node: str
    config: NodeConfigEntry
    content: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeConfigContent:
        raw_config = payload.get("config")
        if not isinstance(raw_config, Mapping):
            raise ValueError("Node config content metadata is invalid.")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("Node config content is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            config=NodeConfigEntry.from_mapping(raw_config),
            content=content,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "config": self.config.to_mapping(),
            "content": self.content,
        }


class NodeConfigWriteRequest(BaseModel):
    content: str

    model_config = ConfigDict(str_strip_whitespace=False)


@dataclass(frozen=True, slots=True)
class NodeSaveRootEntry:
    id: str
    label: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSaveRootEntry":
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class NodeSaveEntry:
    id: str
    label: str
    relative_path: str
    root_id: str
    root_label: str
    kind: str
    size_bytes: int
    size_text: str
    modified_at: str
    can_delete: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSaveEntry:
        raw_can_delete = payload.get("can_delete", False)
        if not isinstance(raw_can_delete, bool):
            raise ValueError("Node save entry can_delete is invalid.")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            root_id=_required_string(payload, "root_id"),
            root_label=_required_string(payload, "root_label"),
            kind=_required_string(payload, "kind"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            can_delete=raw_can_delete,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "root_id": self.root_id,
            "root_label": self.root_label,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "can_delete": self.can_delete,
        }


@dataclass(frozen=True, slots=True)
class NodeSaveList:
    app_name: str
    app_friendly: str
    node: str
    roots: tuple[NodeSaveRootEntry, ...]
    saves: tuple[NodeSaveEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSaveList:
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        raw_roots = payload.get("roots", ())
        if not isinstance(raw_roots, Sequence) or isinstance(raw_roots, (str, bytes)):
            raise ValueError("Node save list roots are invalid.")
        roots: list[NodeSaveRootEntry] = []
        for raw_root in raw_roots:
            if not isinstance(raw_root, Mapping):
                raise ValueError("Node save list contains an invalid root entry.")
            roots.append(NodeSaveRootEntry.from_mapping(raw_root))
        raw_saves = payload.get("saves")
        if not isinstance(raw_saves, Sequence) or isinstance(raw_saves, (str, bytes)):
            raise ValueError("Node save list saves are invalid.")
        saves: list[NodeSaveEntry] = []
        for raw_save in raw_saves:
            if not isinstance(raw_save, Mapping):
                raise ValueError("Node save list contains an invalid save entry.")
            saves.append(NodeSaveEntry.from_mapping(raw_save))
        return cls(app_name=app_name, app_friendly=app_friendly, node=node, roots=tuple(roots), saves=tuple(saves))

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "roots": [root.to_mapping() for root in self.roots],
            "saves": [entry.to_mapping() for entry in self.saves],
        }


@dataclass(frozen=True, slots=True)
class NodeSaveMutationResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    save: NodeSaveEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeSaveMutationResult":
        raw_save = payload.get("save")
        if not isinstance(raw_save, Mapping):
            raise ValueError("Node save mutation result save is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            save=NodeSaveEntry.from_mapping(raw_save),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "save": self.save.to_mapping(),
        }


class NodeSaveRenameRequest(BaseModel):
    new_name: str

    model_config = ConfigDict(str_strip_whitespace=True)


@dataclass(frozen=True, slots=True)
class NodeBlueprintFileEntry:
    id: str
    label: str
    relative_path: str
    size_bytes: int
    size_text: str
    modified_at: str
    uploaded_by_display_name: str | None
    can_delete: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintFileEntry":
        uploaded_by_display_name = _optional_string(payload, "uploaded_by_display_name")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=_required_bool(payload, "can_delete"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "uploaded_by_display_name": self.uploaded_by_display_name,
            "can_delete": self.can_delete,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintEntry:
    id: str
    label: str
    session_name: str
    relative_path: str
    size_bytes: int
    size_text: str
    modified_at: str
    uploaded_by_display_name: str | None
    can_delete: bool
    config_file: NodeBlueprintFileEntry | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintEntry":
        uploaded_by_display_name = _optional_string(payload, "uploaded_by_display_name")
        raw_config_file = payload.get("config_file")
        if raw_config_file is not None and not isinstance(raw_config_file, Mapping):
            raise ValueError("Node blueprint entry config_file is invalid.")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            session_name=_required_string(payload, "session_name"),
            relative_path=_required_string(payload, "relative_path"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=_required_bool(payload, "can_delete"),
            config_file=NodeBlueprintFileEntry.from_mapping(raw_config_file) if raw_config_file is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "session_name": self.session_name,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "uploaded_by_display_name": self.uploaded_by_display_name,
            "can_delete": self.can_delete,
            "config_file": self.config_file.to_mapping() if self.config_file is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintList:
    app_name: str
    app_friendly: str
    node: str
    blueprints: tuple[NodeBlueprintEntry, ...]
    default_session_name: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintList":
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        default_session_name = _optional_string(payload, "default_session_name")
        raw_blueprints = payload.get("blueprints")
        if not isinstance(raw_blueprints, Sequence) or isinstance(raw_blueprints, (str, bytes)):
            raise ValueError("Node blueprint list blueprints are invalid.")
        blueprints: list[NodeBlueprintEntry] = []
        for raw_blueprint in raw_blueprints:
            if not isinstance(raw_blueprint, Mapping):
                raise ValueError("Node blueprint list contains an invalid blueprint entry.")
            blueprints.append(NodeBlueprintEntry.from_mapping(raw_blueprint))
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            blueprints=tuple(blueprints),
            default_session_name=default_session_name,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "blueprints": [entry.to_mapping() for entry in self.blueprints],
            "default_session_name": self.default_session_name,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintMutationResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    blueprint: NodeBlueprintEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintMutationResult":
        raw_blueprint = payload.get("blueprint")
        if not isinstance(raw_blueprint, Mapping):
            raise ValueError("Node blueprint mutation result blueprint is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            blueprint=NodeBlueprintEntry.from_mapping(raw_blueprint),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "blueprint": self.blueprint.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeSettingChoice:
    label: str
    raw_value: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingChoice:
        return cls(
            label=_required_string(payload, "label"),
            raw_value=_required_string(payload, "raw_value"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "label": self.label,
            "raw_value": self.raw_value,
        }


@dataclass(frozen=True, slots=True)
class NodeSettingEntry:
    key: str
    label: str
    type_name: str
    permission_level: str
    permission_level_name: str
    default_text: str
    description: str | None
    paragraph: bool
    is_sensitive: bool
    value_text: str
    revealed_value_text: str
    current_input_value: str
    has_pending_value: bool
    can_edit: bool
    value_is_hidden: bool
    can_reveal_hidden_text: bool
    allows_text_input: bool
    allows_blank_input: bool
    strict_choice: bool
    choices: tuple[NodeSettingChoice, ...]
    recent_inputs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingEntry:
        raw_choices = payload.get("choices")
        if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
            raise ValueError("Node setting entry choices are invalid.")
        choices: list[NodeSettingChoice] = []
        for raw_choice in raw_choices:
            if not isinstance(raw_choice, Mapping):
                raise ValueError("Node setting entry contained an invalid choice.")
            choices.append(NodeSettingChoice.from_mapping(raw_choice))

        raw_recent_inputs = payload.get("recent_inputs")
        if not isinstance(raw_recent_inputs, Sequence) or isinstance(raw_recent_inputs, (str, bytes)):
            raise ValueError("Node setting entry recent inputs are invalid.")
        recent_inputs: list[str] = []
        for raw_recent_input in raw_recent_inputs:
            if not isinstance(raw_recent_input, str):
                raise ValueError("Node setting entry contained an invalid recent input.")
            recent_inputs.append(raw_recent_input)

        permission_level = _required_string(payload, "permission_level")
        permission_level_name = payload.get("permission_level_name")
        if not isinstance(permission_level_name, str) or not permission_level_name:
            parsed_permission_level = Access_Control.parse_level(permission_level)
            permission_level_name = (
                parsed_permission_level.name if parsed_permission_level is not None else permission_level
            )
        has_pending_value = payload.get("has_pending_value", False)
        if not isinstance(has_pending_value, bool):
            raise ValueError("Node setting entry has_pending_value is invalid.")

        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            type_name=_required_string(payload, "type_name"),
            permission_level=permission_level,
            permission_level_name=permission_level_name,
            default_text=_required_text(payload, "default_text"),
            description=_optional_string(payload, "description"),
            paragraph=_required_bool(payload, "paragraph"),
            is_sensitive=_required_bool(payload, "is_sensitive"),
            value_text=_required_text(payload, "value_text"),
            revealed_value_text=_required_text(payload, "revealed_value_text"),
            current_input_value=_required_text(payload, "current_input_value"),
            has_pending_value=has_pending_value,
            can_edit=_required_bool(payload, "can_edit"),
            value_is_hidden=_required_bool(payload, "value_is_hidden"),
            can_reveal_hidden_text=_required_bool(payload, "can_reveal_hidden_text"),
            allows_text_input=_required_bool(payload, "allows_text_input"),
            allows_blank_input=_required_bool(payload, "allows_blank_input"),
            strict_choice=_required_bool(payload, "strict_choice"),
            choices=tuple(choices),
            recent_inputs=tuple(recent_inputs),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "type_name": self.type_name,
            "permission_level": self.permission_level,
            "permission_level_name": self.permission_level_name,
            "default_text": self.default_text,
            "description": self.description,
            "paragraph": self.paragraph,
            "is_sensitive": self.is_sensitive,
            "value_text": self.value_text,
            "revealed_value_text": self.revealed_value_text,
            "current_input_value": self.current_input_value,
            "has_pending_value": self.has_pending_value,
            "can_edit": self.can_edit,
            "value_is_hidden": self.value_is_hidden,
            "can_reveal_hidden_text": self.can_reveal_hidden_text,
            "allows_text_input": self.allows_text_input,
            "allows_blank_input": self.allows_blank_input,
            "strict_choice": self.strict_choice,
            "choices": [choice.to_mapping() for choice in self.choices],
            "recent_inputs": list(self.recent_inputs),
        }


@dataclass(frozen=True, slots=True)
class NodeSettingList:
    app_name: str
    app_friendly: str
    node: str
    editable_count: int
    restricted_count: int
    has_pending_changes: bool
    pending_change_count: int
    required_save_level_name: str
    required_reload_level_name: str
    settings: tuple[NodeSettingEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingList:
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Sequence) or isinstance(raw_settings, (str, bytes)):
            raise ValueError("Node setting list settings are invalid.")
        settings: list[NodeSettingEntry] = []
        for raw_setting in raw_settings:
            if not isinstance(raw_setting, Mapping):
                raise ValueError("Node setting list contained an invalid setting entry.")
            settings.append(NodeSettingEntry.from_mapping(raw_setting))
        has_pending_changes = payload.get("has_pending_changes", False)
        if not isinstance(has_pending_changes, bool):
            raise ValueError("Node setting list has_pending_changes is invalid.")
        pending_change_count = payload.get("pending_change_count", 0)
        if isinstance(pending_change_count, bool) or not isinstance(pending_change_count, int):
            raise ValueError("Node setting list pending_change_count is invalid.")
        required_save_level_name = payload.get("required_save_level_name", Power_Level.user.name)
        if not isinstance(required_save_level_name, str) or not required_save_level_name:
            raise ValueError("Node setting list required_save_level_name is invalid.")
        required_reload_level_name = payload.get("required_reload_level_name", Power_Level.user.name)
        if not isinstance(required_reload_level_name, str) or not required_reload_level_name:
            raise ValueError("Node setting list required_reload_level_name is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            editable_count=_required_int(payload, "editable_count"),
            restricted_count=_required_int(payload, "restricted_count"),
            has_pending_changes=has_pending_changes,
            pending_change_count=pending_change_count,
            required_save_level_name=required_save_level_name,
            required_reload_level_name=required_reload_level_name,
            settings=tuple(settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "editable_count": self.editable_count,
            "restricted_count": self.restricted_count,
            "has_pending_changes": self.has_pending_changes,
            "pending_change_count": self.pending_change_count,
            "required_save_level_name": self.required_save_level_name,
            "required_reload_level_name": self.required_reload_level_name,
            "settings": [setting.to_mapping() for setting in self.settings],
        }


@dataclass(frozen=True, slots=True)
class NodeSettingMutationResult:
    app_name: str
    app_friendly: str
    node: str
    setting_key: str
    message: str
    setting: NodeSettingEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingMutationResult:
        raw_setting = payload.get("setting")
        if not isinstance(raw_setting, Mapping):
            raise ValueError("Node setting mutation result setting is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            setting_key=_required_string(payload, "setting_key"),
            message=_required_string(payload, "message"),
            setting=NodeSettingEntry.from_mapping(raw_setting),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "setting_key": self.setting_key,
            "message": self.message,
            "setting": self.setting.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeSettingsActionResult:
    app_name: str
    app_friendly: str
    node: str
    message: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeSettingsActionResult:
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleActionParameter:
    key: str
    label: str
    value_type_name: str
    description: str | None
    max_length: int
    multiline: bool
    strict_choice: bool
    allows_text_input: bool
    choices: tuple[NodeSettingChoice, ...]
    recent_inputs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionParameter":
        raw_choices = payload.get("choices")
        raw_recent_inputs = payload.get("recent_inputs")
        if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
            raise ValueError("Node console action parameter choices are invalid.")
        if not isinstance(raw_recent_inputs, Sequence) or isinstance(raw_recent_inputs, (str, bytes)):
            raise ValueError("Node console action parameter recent_inputs are invalid.")
        choices: list[NodeSettingChoice] = []
        for raw_choice in raw_choices:
            if not isinstance(raw_choice, Mapping):
                raise ValueError("Node console action parameter contains an invalid choice.")
            choices.append(NodeSettingChoice.from_mapping(raw_choice))
        recent_inputs: list[str] = []
        for raw_recent_input in raw_recent_inputs:
            if not isinstance(raw_recent_input, str):
                raise ValueError("Node console action parameter contains an invalid recent input.")
            recent_inputs.append(raw_recent_input)
        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            value_type_name=_required_string(payload, "value_type_name"),
            description=_optional_string(payload, "description"),
            max_length=_required_int(payload, "max_length"),
            multiline=_required_bool(payload, "multiline"),
            strict_choice=_required_bool(payload, "strict_choice"),
            allows_text_input=_required_bool(payload, "allows_text_input"),
            choices=tuple(choices),
            recent_inputs=tuple(recent_inputs),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "value_type_name": self.value_type_name,
            "description": self.description,
            "max_length": self.max_length,
            "multiline": self.multiline,
            "strict_choice": self.strict_choice,
            "allows_text_input": self.allows_text_input,
            "choices": [choice.to_mapping() for choice in self.choices],
            "recent_inputs": list(self.recent_inputs),
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleActionEntry:
    key: str
    label: str
    description: str
    power_level_name: str
    power_level_label: str
    requires_running: bool
    can_run: bool
    parameter: NodeConsoleActionParameter | None
    runtime_running: bool | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionEntry":
        raw_parameter = payload.get("parameter")
        if raw_parameter is not None and not isinstance(raw_parameter, Mapping):
            raise ValueError("Node console action entry parameter is invalid.")
        raw_runtime_running = payload.get("runtime_running")
        if raw_runtime_running is not None and not isinstance(raw_runtime_running, bool):
            raise ValueError("Node console action entry runtime_running is invalid.")
        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            description=_required_string(payload, "description"),
            power_level_name=_required_string(payload, "power_level_name"),
            power_level_label=_required_string(payload, "power_level_label"),
            requires_running=_required_bool(payload, "requires_running"),
            can_run=_required_bool(payload, "can_run"),
            parameter=(
                NodeConsoleActionParameter.from_mapping(raw_parameter)
                if raw_parameter is not None
                else None
            ),
            runtime_running=raw_runtime_running,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "power_level_name": self.power_level_name,
            "power_level_label": self.power_level_label,
            "requires_running": self.requires_running,
            "can_run": self.can_run,
            "parameter": self.parameter.to_mapping() if self.parameter is not None else None,
            "runtime_running": self.runtime_running,
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleActionList:
    app_name: str
    app_friendly: str
    node: str
    actions: tuple[NodeConsoleActionEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionList":
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, Sequence) or isinstance(raw_actions, (str, bytes)):
            raise ValueError("Node console action list actions are invalid.")
        actions: list[NodeConsoleActionEntry] = []
        for raw_action in raw_actions:
            if not isinstance(raw_action, Mapping):
                raise ValueError("Node console action list contains an invalid action.")
            actions.append(NodeConsoleActionEntry.from_mapping(raw_action))
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            actions=tuple(actions),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "actions": [action.to_mapping() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class NodeConsoleStdoutSnapshot:
    app_name: str
    app_friendly: str
    node: str
    lines: tuple[str, ...]
    truncated: bool
    running: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleStdoutSnapshot":
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)):
            raise ValueError("Node console stdout snapshot lines are invalid.")
        lines: list[str] = []
        for raw_line in raw_lines:
            if not isinstance(raw_line, str):
                raise ValueError("Node console stdout snapshot contains an invalid line.")
            lines.append(raw_line)
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            lines=tuple(lines),
            truncated=_required_bool(payload, "truncated"),
            running=_required_bool(payload, "running"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "running": self.running,
        }


class NodeConsoleStdoutStreamEventKind(StrEnum):
    INITIAL = "initial"
    APPEND = "append"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class NodeConsoleStdoutStreamEvent:
    kind: NodeConsoleStdoutStreamEventKind
    app_name: str
    snapshot: NodeConsoleStdoutSnapshot | None = None
    appended_lines: tuple[str, ...] = ()
    truncated: bool = False
    running: bool = False

    def __post_init__(self) -> None:
        if not self.app_name.strip():
            raise ValueError("Console stdout stream app name must not be empty.")
        if self.kind in {NodeConsoleStdoutStreamEventKind.INITIAL, NodeConsoleStdoutStreamEventKind.RESET}:
            if self.snapshot is None or self.snapshot.app_name.casefold() != self.app_name.casefold():
                raise ValueError("Console stdout stream snapshots are invalid.")
            if self.appended_lines:
                raise ValueError("Console stdout snapshot events cannot append lines.")
        elif self.snapshot is not None:
            raise ValueError("Console stdout append events cannot contain snapshots.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleStdoutStreamEvent":
        try:
            kind = NodeConsoleStdoutStreamEventKind(_required_string(payload, "kind"))
        except ValueError as xcp:
            raise ValueError("Console stdout stream event kind is invalid.") from xcp
        raw_snapshot = payload.get("snapshot")
        if raw_snapshot is not None and not isinstance(raw_snapshot, Mapping):
            raise ValueError("Console stdout stream snapshot is invalid.")
        raw_appended_lines = payload.get("appended_lines", ())
        if not isinstance(raw_appended_lines, Sequence) or isinstance(raw_appended_lines, (str, bytes)):
            raise ValueError("Console stdout appended lines are invalid.")
        appended_lines = tuple(raw_appended_lines)
        if any(not isinstance(line, str) for line in appended_lines):
            raise ValueError("Console stdout appended lines are invalid.")
        return cls(
            kind=kind,
            app_name=_required_string(payload, "app_name"),
            snapshot=NodeConsoleStdoutSnapshot.from_mapping(raw_snapshot) if raw_snapshot is not None else None,
            appended_lines=cast(tuple[str, ...], appended_lines),
            truncated=_required_bool(payload, "truncated"),
            running=_required_bool(payload, "running"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "app_name": self.app_name,
            "snapshot": self.snapshot.to_mapping() if self.snapshot is not None else None,
            "appended_lines": list(self.appended_lines),
            "truncated": self.truncated,
            "running": self.running,
        }

    def apply(
        self,
        previous: NodeConsoleStdoutSnapshot | None,
        *,
        max_lines: int,
    ) -> NodeConsoleStdoutSnapshot:
        if max_lines <= 0:
            raise ValueError("Console stdout stream max lines must be positive.")
        if self.snapshot is not None:
            return self.snapshot
        if previous is None:
            raise ValueError("Console stdout append event requires an initial snapshot.")
        lines = (*previous.lines, *self.appended_lines)
        return NodeConsoleStdoutSnapshot(
            app_name=previous.app_name,
            app_friendly=previous.app_friendly,
            node=previous.node,
            lines=tuple(lines[-max_lines:]),
            truncated=self.truncated,
            running=self.running,
        )


class NodeConsoleActionExecuteRequest(BaseModel):
    value: str | None = None


@dataclass(frozen=True, slots=True)
class NodeConsoleActionExecutionResult:
    app_name: str
    app_friendly: str
    node: str
    action_key: str
    summary: str
    success: bool
    text: str | None
    source: ConsoleResponseSource

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeConsoleActionExecutionResult":
        raw_source = _required_string(payload, "source")
        try:
            source = ConsoleResponseSource(raw_source)
        except ValueError as xcp:
            raise ValueError("Node console action execution result source is invalid.") from xcp
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            action_key=_required_string(payload, "action_key"),
            summary=_required_string(payload, "summary"),
            success=_required_bool(payload, "success"),
            text=_optional_string(payload, "text"),
            source=source,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "action_key": self.action_key,
            "summary": self.summary,
            "success": self.success,
            "text": self.text,
            "source": self.source.value,
        }


@dataclass(frozen=True, slots=True)
class NodeChatEndpointSummary:
    label: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeChatEndpointSummary":
        return cls(label=_required_string(payload, "label"))

    def to_mapping(self) -> dict[str, object]:
        return {"label": self.label}


@dataclass(frozen=True, slots=True)
class NodeChatRoomSnapshot:
    room_id: str
    endpoint_count: int
    events: tuple[ChatEvent, ...]
    endpoint_summaries: tuple[NodeChatEndpointSummary, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("Chat room snapshot revision must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeChatRoomSnapshot":
        raw_events = payload.get("events")
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise ValueError("events are invalid.")
        events: list[ChatEvent] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                raise ValueError("events are invalid.")
            events.append(ChatEvent.from_mapping(raw_event))
        raw_endpoint_summaries = payload.get("endpoint_summaries", ())
        if not isinstance(raw_endpoint_summaries, Sequence) or isinstance(raw_endpoint_summaries, (str, bytes)):
            raise ValueError("endpoint_summaries are invalid.")
        endpoint_summaries: list[NodeChatEndpointSummary] = []
        for raw_summary in raw_endpoint_summaries:
            if not isinstance(raw_summary, Mapping):
                raise ValueError("endpoint_summaries are invalid.")
            endpoint_summaries.append(NodeChatEndpointSummary.from_mapping(raw_summary))
        return cls(
            room_id=_required_string(payload, "room_id"),
            endpoint_count=_required_int(payload, "endpoint_count"),
            endpoint_summaries=tuple(endpoint_summaries),
            events=tuple(events),
            revision=_optional_int(payload, "revision") or 0,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "endpoint_count": self.endpoint_count,
            "endpoint_summaries": [summary.to_mapping() for summary in self.endpoint_summaries],
            "events": [event.to_mapping() for event in self.events],
            "revision": self.revision,
        }


class NodeChatStreamEventKind(StrEnum):
    INITIAL = "initial"
    CHAT_CHANGED = "chat_changed"
    RUNTIME_CHANGED = "runtime_changed"


@dataclass(frozen=True, slots=True)
class NodeChatStreamEvent:
    kind: NodeChatStreamEventKind
    room_id: str
    snapshot: NodeChatRoomSnapshot | None = None
    app_stats: NodeAppRuntimeSummary | None = None
    events: tuple[ChatEvent, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.room_id.strip():
            raise ValueError("Node chat stream event room id is invalid.")
        if self.snapshot is not None and self.snapshot.room_id.casefold() != self.room_id.casefold():
            raise ValueError("Node chat stream event snapshot room id is invalid.")
        if any(event.room_id.casefold() != self.room_id.casefold() for event in self.events):
            raise ValueError("Node chat stream event delta room id is invalid.")
        if self.revision < 0:
            raise ValueError("Node chat stream event revision must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeChatStreamEvent":
        raw_kind = _required_string(payload, "kind")
        try:
            kind = NodeChatStreamEventKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("Node chat stream event kind is invalid.") from xcp
        raw_snapshot = payload.get("snapshot")
        raw_app_stats = payload.get("app_stats")
        raw_events = payload.get("events", ())
        if raw_snapshot is not None and not isinstance(raw_snapshot, Mapping):
            raise ValueError("Node chat stream event snapshot is invalid.")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node chat stream event app_stats are invalid.")
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise ValueError("Node chat stream event deltas are invalid.")
        events: list[ChatEvent] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                raise ValueError("Node chat stream event delta is invalid.")
            events.append(ChatEvent.from_mapping(raw_event))
        return cls(
            kind=kind,
            room_id=_required_string(payload, "room_id"),
            snapshot=NodeChatRoomSnapshot.from_mapping(raw_snapshot) if raw_snapshot is not None else None,
            app_stats=NodeAppRuntimeSummary.from_mapping(raw_app_stats) if raw_app_stats is not None else None,
            events=tuple(events),
            revision=_optional_int(payload, "revision") or 0,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "room_id": self.room_id,
            "snapshot": self.snapshot.to_mapping() if self.snapshot is not None else None,
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
            "events": [event.to_mapping() for event in self.events],
            "revision": self.revision,
        }


class NodeSettingWriteRequest(BaseModel):
    value: str

    model_config = ConfigDict(str_strip_whitespace=False)


class NodeWebChatRequest(BaseModel):
    session_id: str
    author_display_name: str
    content: str
    reply_to_event_id: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("session_id", "author_display_name", "content")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("web chat fields must not be empty.")
        return text

    @field_validator("reply_to_event_id")
    @classmethod
    def _validate_optional_reply_to_event_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("reply_to_event_id must not be empty.")
        return text


@dataclass(frozen=True, slots=True)
class NodeDownloadRequest:
    enabled_only: bool = False
    mod_name: str | None = None
    mod_names: tuple[str, ...] = ()
    selected_only: bool = False
    excluded_only: bool = False
    client_pack: bool = False
    pack_purpose: PackPurpose | None = None
    pack_format: PackFormat = PackFormat.GENERIC_ZIP
    publish_client_pack: bool = False
    publish_changelog: str | None = None
    include_kubejs_scripts: bool = True
    include_servers_dat: bool = True
    include_options_txt: bool = True

    @property
    def resolved_pack_purpose(self) -> PackPurpose | None:
        if self.pack_purpose is not None:
            return self.pack_purpose
        if self.client_pack or self.pack_format is not PackFormat.GENERIC_ZIP:
            return PackPurpose.CLIENT
        return None


@dataclass(frozen=True, slots=True)
class NodeDownloadFile:
    path: Path
    filename: str
    is_archive: bool


@dataclass(frozen=True, slots=True)
class NodeModDownloadForm:
    action_url: str
    access_token: str | None


class NodeRelayTTSRequest(BaseModel):
    guild_id: int
    channel_id: int
    message_id: int
    text: str
    user_id: int | None = None
    source_app: str
    player_name: str

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("guild_id", "channel_id", "message_id", "user_id", mode="before")
    @classmethod
    def _validate_optional_snowflake_int(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError("relay TTS snowflake fields must not be booleans.")
        if not isinstance(value, (int, str, hikari.Snowflake)):
            raise TypeError("relay TTS snowflake fields must be Discord snowflakes.")
        return int(hikari.Snowflake(value))

    @field_validator("text", "source_app", "player_name")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("relay TTS fields must not be empty.")
        return text


@dataclass(frozen=True, slots=True)
class NodeRelayTTSResult:
    queued: bool
    spoken: str | None = None
    queue_size: int | None = None
    reason: str | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "queued": self.queued,
            "spoken": self.spoken,
            "queue_size": self.queue_size,
            "reason": self.reason,
        }


class RemoteRelayTTSForwarder:
    _BOT_CONFIGURATION_PATH = Path("configuration.json")
    _TARGET_PROFILE = config.BotProfileName.YUKI

    def __init__(self) -> None:
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH

    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None:
        del guild_id
        return None

    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]:
        return await self.queue_discord_relay_message(
            guild_id,
            channel_id,
            message_id,
            text,
            user_id=user_id,
            source_app=config.MOD_WEB_SERVER.node_name,
            player_name=str(user_id) if user_id is not None else "unlinked",
        )

    async def queue_discord_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
        source_app: str,
        player_name: str,
    ) -> tuple[str, int]:
        secret = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise RuntimeError("Node relay TTS token secret is not configured.")

        target_snapshot = self._resolve_target_snapshot()
        mod_web = target_snapshot.features.mod_web
        if mod_web is None:
            raise RuntimeError("Target voice node does not expose a node API endpoint.")

        payload = NodeRelayTTSRequest(
            guild_id=int(hikari.Snowflake(guild_id)),
            channel_id=int(hikari.Snowflake(channel_id)),
            message_id=int(hikari.Snowflake(message_id)),
            text=text,
            user_id=int(hikari.Snowflake(user_id)) if user_id is not None else None,
            source_app=source_app,
            player_name=player_name,
        )
        token = issue_node_token(
            secret=secret,
            grant=NodeAccessGrant(
                subject=f"relay-tts:{config.MOD_WEB_SERVER.node_name}",
                node=mod_web.node_name,
                app=None,
                scopes=frozenset({NodeApiScope.RELAY_TTS}),
                expires_at=int(time.time()) + _RELAY_TTS_FORWARD_TTL_SECONDS,
            ),
        )
        response = await asyncio.to_thread(
            self._post_relay_tts,
            mod_web.node_api_base_url.rstrip("/") + "/relay/tts",
            token,
            cast(Mapping[str, JsonValue], payload.model_dump(mode="json")),
        )
        queued = bool(response.get("queued"))
        if not queued:
            reason = str(response.get("reason") or "Relay TTS request was not queued.")
            raise RuntimeError(reason)
        spoken = response.get("spoken")
        queue_size = response.get("queue_size")
        if not isinstance(spoken, str) or not isinstance(queue_size, int):
            raise RuntimeError("Relay TTS response from target node was invalid.")
        return spoken, queue_size

    def _resolve_target_snapshot(self) -> config.BotMetadataSnapshot:
        registry = self._load_known_bot_registry()
        for snapshot in registry.values():
            if snapshot.profile.bot_profile is self._TARGET_PROFILE:
                return snapshot
        raise RuntimeError(f"No known bot metadata entry exists for target profile {self._TARGET_PROFILE.value!r}.")

    def _load_known_bot_registry(self) -> dict[str, config.BotMetadataSnapshot]:
        snapshots: dict[str, config.BotMetadataSnapshot] = {}
        if self._bot_configuration_path.exists():
            try:
                bot_config = config.load_bot_configuration(self._bot_configuration_path)
            except (OSError, ValueError) as xcp:
                log.warning("Relay TTS target lookup failed to read %s: %s", self._bot_configuration_path, xcp)
            else:
                snapshots.update(bot_config.known_bots)

        if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.REMOTE:
            cache_path = config.authority_cache_path(AuthorityResource.BOTS)
            if cache_path.exists():
                try:
                    raw = read_json_object(cache_path)
                    snapshots.update(
                        {
                            bot_id: config.BotMetadataSnapshot.model_validate(snapshot)
                            for bot_id, snapshot in raw.items()
                        }
                    )
                except (OSError, ValueError, TypeError) as xcp:
                    log.warning("Relay TTS target lookup failed to read bot registry cache %s: %s", cache_path, xcp)

        return snapshots

    @staticmethod
    def _post_relay_tts(url: str, token: str, payload: Mapping[str, JsonValue]) -> dict[str, object]:
        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
        except requests.RequestException as xcp:
            raise RuntimeError(f"Relay TTS request failed: {type(xcp).__name__}: {xcp}") from xcp
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else response.text
            raise RuntimeError(f"Relay TTS request rejected by target node: {detail}")
        if not isinstance(body, dict):
            raise RuntimeError("Relay TTS response from target node was not a JSON object.")
        return body


_BulkMetadataOperationResult = TypeVar("_BulkMetadataOperationResult")


class NodeApiService:
    def __init__(self) -> None:
        self._manager: App_Manager | None = None
        self._chat_relay: WebChatRelayPublisher | None = None
        self._relay_tts_service: RelayTTSQueue | None = None
        self._acl: Access_Control | None = None
        self._web_auth: ModWebAuthService | None = None
        self._process_restart_handler: Callable[[], None] | None = None
        self._system_action_handler: NodeSystemActionHandler | None = None
        self._maintenance_service: MaintenanceService | None = None
        self._maintenance_restart_targets: tuple[RestartTarget, ...] = ()
        self._pending_system_action: NodeSystemAction | None = None
        self._system_action_lock = threading.RLock()
        self._app_footprint_cache: dict[str, NodeAppFootprintSnapshot] = {}
        self._app_transition_cache: dict[str, NodeAppTransitionSnapshot] = {}
        self._app_entries_cache: _TimedNodeAppEntries | None = None
        self._app_entries_cache_lock = asyncio.Lock()
        self._system_summary_cache: _TimedNodeSystemSummary | None = None
        self._system_summary_cache_lock = threading.RLock()
        self._system_history: deque[NodeSystemSample] = deque(maxlen=_NODE_SYSTEM_HISTORY_MAX_SAMPLES)
        self._system_history_lock = threading.RLock()
        self._system_history_task: asyncio.Task[None] | None = None
        self._live_runtime_cache: dict[str, _TimedAppRuntimeSummary] = {}
        self._live_runtime_cache_locks: dict[str, asyncio.Lock] = {}
        self._full_runtime_cache: dict[str, _TimedAppRuntimeSummary] = {}
        self._full_runtime_cache_locks: dict[str, asyncio.Lock] = {}
        self._mod_inventory_cache: dict[str, _TimedModInventory] = {}
        self._mod_inventory_cache_locks: dict[str, asyncio.Lock] = {}
        self._app_mutation_tasks: dict[str, asyncio.Task[None]] = {}
        self._bulk_metadata_tasks: dict[tuple[str, uuid.UUID], asyncio.Task[object]] = {}
        self._bulk_metadata_discoveries: dict[
            tuple[str, uuid.UUID], _CachedBulkMetadataDiscovery
        ] = {}
        self._local_runtime_watchers: dict[str, _NodeLocalAppRuntimeWatchState] = {}
        self._local_runtime_watch_lock = threading.RLock()
        self._local_node_state_watcher = _NodeLocalNodeStateWatchState()
        self._local_node_state_watch_lock = threading.RLock()
        self._routes_registered = False
        self._shutting_down = False

    async def _run_bulk_metadata_operation(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
        action: Callable[[], Awaitable[_BulkMetadataOperationResult]],
    ) -> _BulkMetadataOperationResult:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Bulk metadata operation is not running in an asyncio task.")
        key = (app_name, operation_id)
        existing = self._bulk_metadata_tasks.get(key)
        if existing is not None and not existing.done():
            raise _http_exception(409, f"Bulk metadata operation {operation_id} is already running.")
        self._bulk_metadata_tasks[key] = cast(asyncio.Task[object], task)
        log.info(
            "Bulk mod metadata operation started: node=%s app=%s operation=%s",
            self.node_name,
            app_name,
            operation_id,
        )
        try:
            return await action()
        except asyncio.CancelledError:
            log.info(
                "Bulk mod metadata operation cancelled: node=%s app=%s operation=%s",
                self.node_name,
                app_name,
                operation_id,
            )
            raise
        finally:
            if self._bulk_metadata_tasks.get(key) is task:
                self._bulk_metadata_tasks.pop(key, None)

    def _cancel_bulk_metadata_operation(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
    ) -> bool:
        task = self._bulk_metadata_tasks.get((app_name, operation_id))
        if task is None or task.done():
            return False
        task.cancel()
        log.info(
            "Bulk mod metadata cancellation requested: node=%s app=%s operation=%s",
            self.node_name,
            app_name,
            operation_id,
        )
        return True

    def _cache_bulk_metadata_discovery(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
        discovery: BulkLauncherMetadataDiscovery,
    ) -> None:
        now = time.monotonic()
        expired_keys = tuple(
            key
            for key, cached in self._bulk_metadata_discoveries.items()
            if now - cached.captured_at_seconds
            >= _BULK_METADATA_DISCOVERY_CACHE_TTL_SECONDS
        )
        for key in expired_keys:
            self._bulk_metadata_discoveries.pop(key, None)
        cache_key = (app_name, operation_id)
        if (
            cache_key not in self._bulk_metadata_discoveries
            and len(self._bulk_metadata_discoveries)
            >= _BULK_METADATA_DISCOVERY_CACHE_MAX_ENTRIES
        ):
            oldest_key = min(
                self._bulk_metadata_discoveries,
                key=lambda key: self._bulk_metadata_discoveries[key].captured_at_seconds,
            )
            self._bulk_metadata_discoveries.pop(oldest_key, None)
        self._bulk_metadata_discoveries[cache_key] = (
            _CachedBulkMetadataDiscovery(
                captured_at_seconds=now,
                discovery=discovery,
            )
        )

    def _cached_bulk_metadata_discovery(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
    ) -> BulkLauncherMetadataDiscovery:
        key = (app_name, operation_id)
        cached = self._bulk_metadata_discoveries.get(key)
        if cached is None:
            raise _http_exception(409, "Bulk metadata discovery is unavailable; run discovery again.")
        if (
            time.monotonic() - cached.captured_at_seconds
            >= _BULK_METADATA_DISCOVERY_CACHE_TTL_SECONDS
        ):
            self._bulk_metadata_discoveries.pop(key, None)
            raise _http_exception(409, "Bulk metadata discovery expired; run discovery again.")
        return cached.discovery

    @property
    def node_name(self) -> str:
        return config.MOD_WEB_SERVER.node_name

    @property
    def api_base_url(self) -> str:
        return config.MOD_WEB_SERVER.node_api_base_url

    def set_manager(self, manager: App_Manager) -> None:
        self._manager = manager
        self._invalidate_state_caches()

    def _invalidate_state_caches(self, *, app_name: str | None = None) -> None:
        self._app_entries_cache = None
        with self._system_summary_cache_lock:
            self._system_summary_cache = None
        if app_name is None:
            self._live_runtime_cache.clear()
            self._full_runtime_cache.clear()
        else:
            self._live_runtime_cache.pop(app_name.casefold(), None)
            self._full_runtime_cache.pop(app_name.casefold(), None)

    def _invalidate_mod_inventory(self, app_name: str) -> None:
        self._mod_inventory_cache.pop(app_name.casefold(), None)

    def set_acl(self, acl: Access_Control) -> None:
        self._acl = acl

    def set_web_auth(self, web_auth: ModWebAuthService) -> None:
        self._web_auth = web_auth

    def set_process_restart_handler(self, handler: Callable[[], None]) -> None:
        self._process_restart_handler = handler

    def set_system_action_handler(self, handler: NodeSystemActionHandler) -> None:
        self._system_action_handler = handler

    def set_maintenance_service(
        self,
        maintenance_service: MaintenanceService,
        available_targets: tuple[RestartTarget, ...],
    ) -> None:
        self._maintenance_service = maintenance_service
        self._maintenance_restart_targets = available_targets

    def set_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._chat_relay = chat_relay

    def set_relay_tts_service(self, relay_tts_service: RelayTTSQueue | None) -> None:
        self._relay_tts_service = relay_tts_service

    def begin_shutdown(self) -> None:
        self._shutting_down = True
        history_task = self._system_history_task
        self._system_history_task = None
        app_mutation_tasks = tuple(self._app_mutation_tasks.values())
        self._app_mutation_tasks.clear()
        with self._local_runtime_watch_lock:
            tasks = tuple(state.task for state in self._local_runtime_watchers.values() if state.task is not None)
            self._local_runtime_watchers.clear()
        with self._local_node_state_watch_lock:
            node_task = self._local_node_state_watcher.task
            self._local_node_state_watcher = _NodeLocalNodeStateWatchState()
        for task in app_mutation_tasks:
            task.cancel()
        if history_task is not None:
            history_task.cancel()
        for task in tasks:
            task.cancel()
        if node_task is not None:
            node_task.cancel()

    def start_background_tasks(self) -> None:
        self._shutting_down = False
        task = self._system_history_task
        if task is None or task.done():
            self._system_history_task = asyncio.get_running_loop().create_task(self._sample_system_history())

    async def _sample_system_history(self) -> None:
        try:
            while not self._shutting_down:
                try:
                    self.build_system_summary(force_refresh=True)
                except Exception:
                    log.exception("Node API system history sample failed: node=%s", self.node_name)
                await asyncio.sleep(_NODE_SYSTEM_HISTORY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    def register_routes(self, nicegui_app: Any) -> None:
        if self._routes_registered:
            return

        nicegui_app.add_middleware(
            CORSMiddleware,
            allow_origins=("*",),
            allow_methods=("POST",),
            allow_headers=("Authorization",),
        )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps")
        async def _list_apps(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API apps request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,))
            return {"node": self.node_name, "apps": [entry.to_mapping() for entry in await self.list_apps()]}

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}")
        async def _app_summary(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API app summary request: node=%s app=%s", self.node_name, app_name)
            self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APPS_READ,),
            )
            app = self._resolve_app(app_name)
            return (await self.build_live_app_entry(app)).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/ping")
        async def _ping() -> Response:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @nicegui_app.post(f"{_NODE_API_PREFIX}/restart")
        async def _restart_node(request: Request, access_token: str | None = None) -> Response:
            traffic_log.info("Node API restart request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.PORTAL:
                raise _http_exception(404, "Process restart is only available on the Portal node.")
            if self._process_restart_handler is None:
                raise _http_exception(503, "Portal process restart handler is unavailable.")
            restart_kind = await self._portal_process_restart_kind(request)
            mark_pending_process_restart(restart_kind)
            asyncio.get_running_loop().call_later(
                _NODE_RESTART_DELAY_SECONDS,
                self._process_restart_handler,
            )
            return Response(status_code=status.HTTP_202_ACCEPTED)

        @nicegui_app.websocket(f"{_NODE_API_PREFIX}/presence/stream")
        async def _presence_stream(websocket: WebSocket) -> None:
            traffic_log.info("Node API presence stream request: node=%s", self.node_name)
            await self._serve_presence_stream(websocket=websocket)

        @nicegui_app.get(f"{_NODE_API_PREFIX}/system")
        async def _system_summary(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API system summary request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,))
            return self.build_system_summary().to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/system/history")
        async def _system_history(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API system history request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,))
            return self.build_system_history().to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/system/restart-state")
        async def _restart_state(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API restart state request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
            return self.read_restart_state().to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/system/actions")
        async def _system_action(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API system action request: node=%s", self.node_name)
            grant = self._require_access(
                request,
                access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_OPERATE,),
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_OPERATE,),
                verified_grant=grant,
            )
            raw_action = payload.get("action")
            if not isinstance(raw_action, str):
                raise _http_exception(400, "Node system action is invalid.")
            try:
                action = NodeSystemAction(raw_action)
            except ValueError as xcp:
                raise _http_exception(400, "Unknown node system action.") from xcp
            auto_restart_running_apps = payload.get("auto_restart_running_apps", True)
            if not isinstance(auto_restart_running_apps, bool):
                raise _http_exception(400, "Node system action auto-restart option must be boolean.")
            silent = payload.get("silent", False)
            if not isinstance(silent, bool):
                raise _http_exception(400, "Node system action silent option must be boolean.")
            result = await self.schedule_system_action(
                action=action,
                auto_restart_running_apps=auto_restart_running_apps,
                silent=silent,
                actor_user_id=actor_user_id,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/system/restart-schedules")
        async def _restart_schedules(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API restart schedule request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
            return self.read_restart_schedules().to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/system/restart-schedules")
        async def _update_restart_schedule(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API restart schedule update request: node=%s", self.node_name)
            grant = self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_OPERATE,),
                verified_grant=grant,
            )
            raw_target = payload.get("target")
            if not isinstance(raw_target, str):
                raise _http_exception(400, "Restart schedule target is invalid.")
            try:
                target = RestartTarget(raw_target)
            except ValueError as xcp:
                raise _http_exception(400, "Unknown restart schedule target.") from xcp
            raw_interval = payload.get("interval_minutes")
            if isinstance(raw_interval, bool) or (raw_interval is not None and not isinstance(raw_interval, int)):
                raise _http_exception(400, "Restart schedule interval is invalid.")
            raw_anchor_timestamp = payload.get("anchor_timestamp")
            if isinstance(raw_anchor_timestamp, bool) or (
                raw_anchor_timestamp is not None and not isinstance(raw_anchor_timestamp, int)
            ):
                raise _http_exception(400, "Restart schedule anchor timestamp is invalid.")
            result = await self.update_restart_schedule(
                target=target,
                interval_minutes=raw_interval,
                anchor_timestamp=raw_anchor_timestamp,
                actor_user_id=actor_user_id,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/system/restart-schedules/{{target_name}}/skip")
        async def _skip_restart_schedule(
            target_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API restart schedule skip request: node=%s target=%s",
                self.node_name,
                target_name,
            )
            grant = self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_OPERATE,),
                verified_grant=grant,
            )
            try:
                target = RestartTarget(target_name)
            except ValueError as xcp:
                raise _http_exception(400, "Unknown restart schedule target.") from xcp
            return (await self.skip_restart_schedule(target=target, actor_user_id=actor_user_id)).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/node-capacity")
        async def _node_capacity(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API node capacity request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            return self.read_node_capacity().model_dump(mode="json")

        @nicegui_app.post(f"{_NODE_API_PREFIX}/node-capacity")
        async def _update_node_capacity(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API node capacity update request: node=%s", self.node_name)
            grant = self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_MANAGE,),
                verified_grant=grant,
            )
            capacity = config.NodeCapacityProfile.model_validate(payload)
            result = await self.mutate_node_capacity(capacity=capacity, actor_user_id=actor_user_id)
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/node-disk-settings")
        async def _node_disk_settings(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API node disk settings request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            return self.read_node_disk_settings().to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/node-disk-settings")
        async def _update_node_disk_settings(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API node disk settings update request: node=%s", self.node_name)
            grant = self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_MANAGE,),
                verified_grant=grant,
            )
            preferences = config.PersistedDiskPreferences.model_validate(payload)
            try:
                result = await self.mutate_node_disk_settings(
                    preferences=preferences,
                    actor_user_id=actor_user_id,
                )
            except ValueError as xcp:
                raise _http_exception(400, str(xcp)) from xcp
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/node-font-sources")
        async def _node_font_sources(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API node font sources request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
            return self.read_node_font_sources().model_dump(mode="json")

        @nicegui_app.post(f"{_NODE_API_PREFIX}/node-font-sources")
        async def _update_node_font_sources(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API node font sources update request: node=%s", self.node_name)
            grant = self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_OPERATE,),
                verified_grant=grant,
            )
            settings = config.NodeFontSourceSettings.model_validate(payload)
            result = await self.mutate_node_font_sources(settings=settings, actor_user_id=actor_user_id)
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/discord-settings")
        async def _discord_settings(request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API Discord settings request: node=%s", self.node_name)
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            return self.read_discord_settings().model_dump(mode="json")

        @nicegui_app.post(f"{_NODE_API_PREFIX}/discord-settings")
        async def _update_discord_settings(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API Discord settings update request: node=%s", self.node_name)
            grant = self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_MANAGE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.NODE_MANAGE,),
                verified_grant=grant,
            )
            settings = config.DiscordSettings.model_validate(payload)
            result = await self.mutate_discord_settings(settings=settings, actor_user_id=actor_user_id)
            return result.to_mapping()

        @nicegui_app.websocket(f"{_NODE_API_PREFIX}/state/stream")
        async def _node_state_stream(
            websocket: WebSocket,
            access_token: str | None = None,
        ) -> None:
            traffic_log.info("Node API node state stream request: node=%s", self.node_name)
            self._require_websocket_token_access(
                websocket=websocket,
                access_token=access_token,
                app_name=None,
                scopes=(NodeApiScope.APPS_READ,),
            )
            await self._serve_node_state_stream(websocket=websocket)

        @nicegui_app.post(f"{_NODE_API_PREFIX}/relay/tts")
        async def _queue_relay_tts(
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            self._require_access(request, access_token, app_name=None, scopes=(NodeApiScope.RELAY_TTS,))
            relay_request = NodeRelayTTSRequest.model_validate(payload)
            result = await self.queue_relay_tts(relay_request)
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/chat")
        async def _chat_snapshot(
            app_name: str,
            request: Request,
            limit: int = _NODE_CHAT_HISTORY_LIMIT,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API chat snapshot request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CHAT_READ,))
            app = self._resolve_app(app_name)
            return self.build_chat_room_snapshot(app, limit=limit).to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/chat")
        async def _publish_chat(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API chat publish request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CHAT_WRITE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CHAT_WRITE,),
                verified_grant=grant,
            )
            chat_request = NodeWebChatRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            return (
                await self.publish_app_web_chat(
                    app=app,
                    actor_user_id=actor_user_id,
                    chat_request=chat_request,
                )
            ).to_mapping()

        @nicegui_app.websocket(f"{_NODE_API_PREFIX}/apps/{{app_name}}/chat/stream")
        async def _chat_stream(
            websocket: WebSocket,
            app_name: str,
            access_token: str | None = None,
            after_revision: int | None = None,
        ) -> None:
            traffic_log.info("Node API chat stream request: node=%s app=%s", self.node_name, app_name)
            self._require_websocket_token_access(
                websocket=websocket,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CHAT_READ, NodeApiScope.MODS_READ),
            )
            try:
                app = self._resolve_app(app_name)
                self._require_chat_relay_app(app)
            except HTTPException as xcp:
                raise self._websocket_exception_from_http(xcp) from xcp
            await self._serve_chat_stream(
                websocket=websocket,
                app=app,
                after_revision=after_revision,
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods")
        async def _list_mods(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API mods list request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            app = self._resolve_app(app_name)
            return (await self.build_mod_list(app)).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/runtime")
        async def _runtime_summary(
            app_name: str, request: Request, access_token: str | None = None
        ) -> dict[str, object]:
            traffic_log.info("Node API runtime summary request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            app = self._resolve_app(app_name)
            return (await self.build_cached_app_runtime_summary(app)).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/sevendays/sandbox-options")
        async def _sevendays_sandbox_options(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API 7D2D sandbox options request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            app = self._resolve_app(app_name)
            return self.build_sevendays_sandbox_options_state(app).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/minecraft/recipes")
        async def _minecraft_recipe_workspace(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API Minecraft recipe workspace request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            app = self._resolve_app(app_name)
            return self.build_minecraft_recipe_workspace_state(app).to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/minecraft/recipes/mutations")
        async def _mutate_minecraft_recipe(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API Minecraft recipe mutation request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_MANAGE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APP_MANAGE,),
                verified_grant=grant,
            )
            try:
                mutation_request = NodeMinecraftRecipeMutationRequest.from_mapping(payload)
            except ValueError as xcp:
                raise _http_exception(400, str(xcp)) from xcp
            app = self._resolve_app(app_name)
            result = await self.mutate_minecraft_recipe_book(
                app=app, mutation_request=mutation_request, actor_user_id=actor_user_id
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/minecraft/recipes/item-icon")
        async def _minecraft_recipe_item_icon(
            app_name: str,
            item_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info(
                "Node API Minecraft recipe item icon request: node=%s app=%s item=%s",
                self.node_name,
                app_name,
                item_id,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            app = self._resolve_app(app_name)
            return await asyncio.to_thread(self.build_minecraft_item_icon_response, app, item_id=item_id)

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/manifest")
        async def _map_manifest(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info("Node API map manifest request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            manifest, source = await asyncio.to_thread(self._build_map_manifest_result, app)
            return Response(
                content=json.dumps(manifest.to_mapping()),
                media_type="application/json",
                headers=self._squaremap_response_headers(source),
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/annotations")
        async def _map_annotations(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API map annotation list request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            annotations = await asyncio.to_thread(self.build_map_annotation_list, app)
            return annotations.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/annotations")
        async def _create_map_annotation(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API map annotation create request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_WRITE,))
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MAP_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            draft = MapAnnotationDraft.from_mapping(payload)
            user: ModWebUser | None = None if self._web_auth is None else self._web_auth.current_user(request)
            created_by_name = self._map_annotation_creator_name(app, actor_user_id=actor_user_id, user=user)
            result = await asyncio.to_thread(
                self.create_map_annotation,
                app,
                draft,
                actor_user_id,
                created_by_name,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/annotations/{{annotation_id}}/delete")
        async def _delete_map_annotation(
            app_name: str,
            annotation_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API map annotation delete request: node=%s app=%s annotation_id=%s",
                self.node_name,
                app_name,
                annotation_id,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_WRITE,))
            app = self._resolve_app(app_name)
            result = await asyncio.to_thread(self.delete_map_annotation, app, annotation_id)
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/players")
        async def _map_players(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info("Node API map players request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            proxy_response = await asyncio.to_thread(
                self._squaremap_proxy_response,
                app,
                "tiles/players.json",
                request.url.query,
            )
            return Response(
                content=proxy_response.content,
                media_type=proxy_response.media_type,
                headers=self._squaremap_response_headers(proxy_response),
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/worlds/{{world_name}}/settings")
        async def _map_world_settings(
            app_name: str,
            world_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info(
                "Node API map world settings request: node=%s app=%s world=%s",
                self.node_name,
                app_name,
                world_name,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            proxy_response = await asyncio.to_thread(
                self._squaremap_proxy_response,
                app,
                f"tiles/{quote(world_name, safe='')}/settings.json",
                request.url.query,
                allow_stale_on_error=True,
            )
            return Response(
                content=proxy_response.content,
                media_type=proxy_response.media_type,
                headers=self._squaremap_response_headers(proxy_response),
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/worlds/{{world_name}}/markers")
        async def _map_world_markers(
            app_name: str,
            world_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info(
                "Node API map world markers request: node=%s app=%s world=%s",
                self.node_name,
                app_name,
                world_name,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            proxy_response = await asyncio.to_thread(
                self._squaremap_proxy_response,
                app,
                f"tiles/{quote(world_name, safe='')}/markers.json",
                request.url.query,
                allow_stale_on_error=True,
            )
            return Response(
                content=proxy_response.content,
                media_type=proxy_response.media_type,
                headers=self._squaremap_response_headers(proxy_response),
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/worlds/{{world_name}}/tiles/{{z}}/{{tile_name}}")
        async def _map_world_tile(
            app_name: str,
            world_name: str,
            z: int,
            tile_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info(
                "Node API map tile request: node=%s app=%s world=%s z=%s tile=%s",
                self.node_name,
                app_name,
                world_name,
                z,
                tile_name,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            proxy_response = await asyncio.to_thread(
                self._squaremap_proxy_response,
                app,
                f"tiles/{quote(world_name, safe='')}/{z}/{quote(tile_name, safe='')}",
                request.url.query,
            )
            return Response(
                content=proxy_response.content,
                media_type=proxy_response.media_type,
                headers=self._squaremap_response_headers(proxy_response),
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/map/assets/{{asset_path:path}}")
        async def _map_asset(
            app_name: str,
            asset_path: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info(
                "Node API map asset request: node=%s app=%s asset=%s",
                self.node_name,
                app_name,
                asset_path,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
            app = self._resolve_app(app_name)
            proxy_response = await asyncio.to_thread(
                self._squaremap_proxy_response,
                app,
                f"images/{quote(asset_path, safe='/')}",
                request.url.query,
            )
            return Response(
                content=proxy_response.content,
                media_type=proxy_response.media_type,
                headers=self._squaremap_response_headers(proxy_response),
            )

        @nicegui_app.websocket(f"{_NODE_API_PREFIX}/apps/{{app_name}}/state/stream")
        async def _app_state_stream(
            websocket: WebSocket,
            app_name: str,
            access_token: str | None = None,
        ) -> None:
            traffic_log.info("Node API app state stream request: node=%s app=%s", self.node_name, app_name)
            self._require_websocket_token_access(
                websocket=websocket,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APPS_READ, NodeApiScope.MODS_READ),
            )
            try:
                app = self._resolve_app(app_name)
            except HTTPException as xcp:
                raise self._websocket_exception_from_http(xcp) from xcp
            await self._serve_app_state_stream(websocket=websocket, app=app)

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mutate")
        async def _mutate_app(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API app mutation request: node=%s app=%s", self.node_name, app_name)
            mutation_request = NodeAppMutationRequest.model_validate(payload)
            required_scope = required_app_mutation_scope(mutation_request.action)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(required_scope,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(required_scope,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            result = await self.mutate_app(
                app=app,
                action=mutation_request.action,
                actor_user_id=actor_user_id,
                friendly_name=mutation_request.friendly_name,
                title_font_preset=mutation_request.title_font_preset,
                notes=mutation_request.notes,
                lifecycle_notice_started=mutation_request.lifecycle_notice_started,
                lifecycle_notice_stopped=mutation_request.lifecycle_notice_stopped,
                lifecycle_notice_crashed=mutation_request.lifecycle_notice_crashed,
                relay_notice_player_session=mutation_request.relay_notice_player_session,
                relay_notice_player_death=mutation_request.relay_notice_player_death,
                relay_notice_progress=mutation_request.relay_notice_progress,
                relay_advancements_enabled=mutation_request.relay_advancements_enabled,
                factorio_chat_relay_use_shout=mutation_request.factorio_chat_relay_use_shout,
                rcon_requires_online_players=mutation_request.rcon_requires_online_players,
                disabled_activity_provider_ids=mutation_request.disabled_activity_provider_ids,
                running_cpu_points=mutation_request.running_cpu_points,
                running_ram_points=mutation_request.running_ram_points,
                startup_cpu_points=mutation_request.startup_cpu_points,
                startup_ram_points=mutation_request.startup_ram_points,
                steam_update_enabled=mutation_request.steam_update_enabled,
                steam_update_selected_branch=mutation_request.steam_update_selected_branch,
                update_branch_id=mutation_request.update_branch_id,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/download")
        async def _download_mods(
            app_name: str,
            request: Request,
            enabled_only: bool = False,
            selected_only: bool = False,
            excluded_only: bool = False,
            client_pack: bool = False,
            pack_purpose: PackPurpose | None = None,
            pack_format: PackFormat = PackFormat.GENERIC_ZIP,
            publish_client_pack: bool = False,
            include_kubejs_scripts: bool = True,
            include_servers_dat: bool = True,
            include_options_txt: bool = True,
            access_token: str | None = None,
        ) -> FileResponse:
            mod_names = tuple(request.query_params.getlist("mod_name"))
            traffic_log.info(
                "Node API mods archive request: node=%s app=%s enabled_only=%s selected_only=%s "
                "excluded_only=%s client_pack=%s purpose=%s format=%s selected=%s",
                self.node_name,
                app_name,
                enabled_only,
                selected_only,
                excluded_only,
                client_pack,
                pack_purpose,
                pack_format,
                len(mod_names),
            )
            required_scopes = (NodeApiScope.MODS_DOWNLOAD,)
            if pack_purpose in {PackPurpose.SERVER, PackPurpose.ADMIN} or publish_client_pack:
                required_scopes = (NodeApiScope.MODS_DOWNLOAD, NodeApiScope.MODS_WRITE)
            self._require_access(request, access_token, app_name=app_name, scopes=required_scopes)
            app = self._resolve_app(app_name)
            return await self.build_mod_download_response(
                app=app,
                request=NodeDownloadRequest(
                    enabled_only=enabled_only,
                    mod_names=mod_names,
                    selected_only=selected_only,
                    excluded_only=excluded_only,
                    client_pack=client_pack,
                    pack_purpose=pack_purpose,
                    pack_format=pack_format,
                    publish_client_pack=publish_client_pack,
                    include_kubejs_scripts=include_kubejs_scripts,
                    include_servers_dat=include_servers_dat,
                    include_options_txt=include_options_txt,
                ),
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/download")
        async def _download_mod(
            app_name: str,
            mod_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> FileResponse:
            traffic_log.info("Node API single mod request: node=%s app=%s mod=%s", self.node_name, app_name, mod_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_DOWNLOAD,))
            app = self._resolve_app(app_name)
            return await self.build_mod_download_response(
                app=app,
                request=NodeDownloadRequest(mod_name=mod_name),
            )

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/upload")
        async def _upload_mod(
            app_name: str,
            request: Request,
            upload: Annotated[list[UploadFile], File()],
            filename: Annotated[list[str] | None, Form()] = None,
            placement: ModPlacement = ModPlacement.SERVER_ENABLED,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API mod upload request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            result = await self.upload_mod_files(
                app=app,
                uploads=upload,
                upload_names=filename,
                actor_user_id=actor_user_id,
                placement=placement,
            )
            audit_log(
                "mod.file_uploaded",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                mod_name=",".join(mod.name for mod in result.mods),
                required_level=Power_Level.user.name,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/install-link")
        async def _install_mod_link(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API mod link install request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            install_request = NodeModPortalInstallRequest.model_validate(payload)
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            result = await self.install_mod_from_link(
                app=app,
                url=install_request.url,
                actor_user_id=actor_user_id,
                selected_mod_ids=install_request.selected_mod_ids,
                version=install_request.version,
            )
            audit_log(
                "mod.link_installed",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                mod_name=",".join(mod.name for mod in result.mods),
                required_level=Power_Level.user.name,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/resolve-link")
        async def _resolve_mod_link(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API mod link resolve request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            resolve_request = NodeModPortalInstallRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            result = await self.resolve_mod_link_dependencies(
                app=app,
                url=resolve_request.url,
                version=resolve_request.version,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/resolve-link/versions")
        async def _resolve_mod_link_versions(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API mod link version list request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            resolve_request = NodeModPortalInstallRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            result = await self.list_mod_link_versions(app=app, url=resolve_request.url)
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/mutate")
        async def _mutate_mod(
            app_name: str,
            mod_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API mod mutation request: node=%s app=%s mod=%s", self.node_name, app_name, mod_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            mutation_request = NodeModMutationRequest.model_validate(payload)
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            result = await self.mutate_mod(
                app=app,
                mod_name=mod_name,
                action=mutation_request.action,
                actor_user_id=actor_user_id,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/check-update")
        async def _check_mod_update(
            app_name: str,
            mod_name: str,
            request: Request,
            version: str | None = None,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API mod update check request: node=%s app=%s mod=%s",
                self.node_name,
                app_name,
                mod_name,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            update_request = NodeModUpdateRequest.model_validate({"version": version})
            app = self._resolve_app(app_name)
            result = await self.check_mod_update(app=app, mod_name=mod_name, version=update_request.version)
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/versions")
        async def _list_mod_versions(
            app_name: str,
            mod_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API mod version list request: node=%s app=%s mod=%s",
                self.node_name,
                app_name,
                mod_name,
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_READ,))
            app = self._resolve_app(app_name)
            result = await self.list_installed_mod_versions(app=app, mod_name=mod_name)
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/update")
        async def _update_mod(
            app_name: str,
            mod_name: str,
            request: Request,
            payload: dict[str, object] | None = None,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API mod update request: node=%s app=%s mod=%s",
                self.node_name,
                app_name,
                mod_name,
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            update_request = NodeModUpdateRequest.model_validate(payload or {})
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            result = await self.update_mod(
                app=app,
                mod_name=mod_name,
                actor_user_id=actor_user_id,
                version=update_request.version,
            )
            audit_log(
                "mod.updated",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                mod_name=",".join(mod.name for mod in result.mods),
                required_level=Power_Level.user.name,
            )
            return result.to_mapping()

        @nicegui_app.put(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/properties")
        async def _update_mod_properties(
            app_name: str,
            mod_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API mod properties update request: node=%s app=%s mod=%s",
                self.node_name,
                app_name,
                mod_name,
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            update_request = NodeModPropertiesUpdateRequest.model_validate(payload)
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            result = await self.update_mod_properties(
                app=self._resolve_app(app_name),
                mod_name=mod_name,
                update=update_request,
                actor_user_id=actor_user_id,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/launcher-metadata")
        async def _fetch_mod_launcher_metadata(
            app_name: str,
            mod_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
            )
            fetch_request = NodeModMetadataFetchRequest.model_validate(payload)
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            resolution = await self.fetch_mod_launcher_metadata(
                app=self._resolve_app(app_name),
                mod_name=mod_name,
                fetch_request=fetch_request,
                actor_user_id=actor_user_id,
            )
            return resolution.model_dump(mode="json")

        @nicegui_app.post(
            f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/launcher-metadata/resolve"
        )
        async def _resolve_mod_launcher_metadata(
            app_name: str,
            mod_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
            )
            resolve_request = NodeModMetadataResolveRequest.model_validate(payload)
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            discovery = await self.resolve_mod_launcher_metadata(
                app=self._resolve_app(app_name),
                mod_name=mod_name,
                resolve_request=resolve_request,
                actor_user_id=actor_user_id,
            )
            return discovery.model_dump(mode="json")

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/{{mod_name}}/mod-pages/resolve")
        async def _resolve_mod_pages(
            app_name: str,
            mod_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
            )
            resolve_request = NodeModPageResolveRequest.model_validate(payload)
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            discovery = await self.find_mod_pages(
                app=self._resolve_app(app_name),
                mod_name=mod_name,
                resolve_request=resolve_request,
                actor_user_id=actor_user_id,
            )
            return discovery.model_dump(mode="json")

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/metadata/discover")
        async def _discover_bulk_mod_metadata(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            discovery_request = NodeBulkLauncherMetadataRequest.model_validate(payload)
            discovery = await self._run_bulk_metadata_operation(
                app_name=app_name,
                operation_id=discovery_request.operation_id,
                action=lambda: self.discover_bulk_mod_metadata(
                    app=self._resolve_app(app_name),
                    discovery_request=discovery_request,
                    actor_user_id=actor_user_id,
                ),
            )
            return discovery.model_dump(mode="json")

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/metadata/apply")
        async def _apply_bulk_mod_metadata(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            apply_request = NodeBulkLauncherMetadataApplyRequest.model_validate(payload)
            result = await self._run_bulk_metadata_operation(
                app_name=app_name,
                operation_id=apply_request.operation_id,
                action=lambda: self.apply_bulk_mod_metadata(
                    app=self._resolve_app(app_name),
                    apply_request=apply_request,
                    actor_user_id=actor_user_id,
                ),
            )
            return result.model_dump(mode="json")

        @nicegui_app.post(
            f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/metadata/{{operation_id}}/cancel"
        )
        async def _cancel_bulk_mod_metadata(
            app_name: str,
            operation_id: uuid.UUID,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(
                request,
                access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            cancelled = self._cancel_bulk_metadata_operation(
                app_name=app_name,
                operation_id=operation_id,
            )
            traffic_log.info(
                "Node API bulk mod metadata cancellation: node=%s app=%s operation=%s "
                "cancelled=%s actor=%s",
                self.node_name,
                app_name,
                operation_id,
                cancelled,
                actor_user_id,
            )
            return {"operation_id": str(operation_id), "cancelled": cancelled}

        @nicegui_app.put(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/client-pack-config")
        async def _update_client_pack_config(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API client-pack configuration request: node=%s app=%s",
                self.node_name,
                app_name,
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            return await self.update_client_pack_config(
                app=self._resolve_app(app_name),
                update=NodeClientPackConfigUpdateRequest.model_validate(payload),
                actor_user_id=actor_user_id,
            )

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/mods/client-pack-config/publish")
        async def _publish_client_pack_config(
            app_name: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MODS_WRITE,))
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.MODS_WRITE,),
                verified_grant=grant,
            )
            return await self.publish_client_pack_config(
                app=self._resolve_app(app_name),
                update=NodeClientPackPublishRequest.model_validate(payload),
                actor_user_id=actor_user_id,
            )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/configs")
        async def _list_configs(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API config list request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
            app = self._resolve_app(app_name)
            actor_user_id = self._request_actor_user_id_if_available(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_READ,),
                verified_grant=grant,
            )
            return self.build_config_list(app, actor_user_id=actor_user_id).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/factorio/mod-settings")
        async def _factorio_mod_settings_state(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API Factorio mod settings state request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_READ,),
                required_level=app.config_file_read_level,
                verified_grant=grant,
            )
            return self.factorio_mod_settings_state(app=app).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/factorio/mod-settings/download")
        async def _download_factorio_mod_settings(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> FileResponse:
            traffic_log.info("Node API Factorio mod settings download request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_READ,),
                required_level=app.config_file_read_level,
                verified_grant=grant,
            )
            return self.build_factorio_mod_settings_download_response(app=app)

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/factorio/mod-settings/upload")
        async def _upload_factorio_mod_settings(
            app_name: str,
            request: Request,
            upload: Annotated[UploadFile, File()],
            filename: Annotated[str | None, Form()] = None,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API Factorio mod settings upload request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_WRITE,),
                required_level=app.config_file_write_level,
                verified_grant=grant,
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_WRITE,),
                verified_grant=grant,
            )
            result = await self.upload_factorio_mod_settings(
                app=app,
                upload=upload,
                upload_name=filename or upload.filename or "",
                actor_user_id=actor_user_id,
            )
            audit_log(
                "factorio.mod_settings_uploaded",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                required_level=app.config_file_write_level.name,
            )
            return result.to_mapping()

        @nicegui_app.delete(f"{_NODE_API_PREFIX}/apps/{{app_name}}/factorio/mod-settings")
        async def _delete_factorio_mod_settings(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API Factorio mod settings delete request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_WRITE,),
                required_level=app.config_file_write_level,
                verified_grant=grant,
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_WRITE,),
                verified_grant=grant,
            )
            result = self.delete_factorio_mod_settings(app=app)
            audit_log(
                "factorio.mod_settings_deleted",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                required_level=app.config_file_write_level.name,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/configs/roots/{{root_id}}/download")
        async def _download_config_root(
            app_name: str,
            root_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> FileResponse:
            traffic_log.info(
                "Node API config root download request: node=%s app=%s root=%s",
                self.node_name,
                app_name,
                root_id,
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
            app = self._resolve_app(app_name)
            try:
                required_level = app.config_file_read_level_for_root(root_id)
            except ValueError as xcp:
                raise _http_exception(400, str(xcp)) from xcp
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_READ,),
                required_level=required_level,
                verified_grant=grant,
            )
            actor_user_id = self._request_actor_user_id_if_available(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_READ,),
                verified_grant=grant,
            )
            return await self.build_config_root_download_response(app=app, root_id=root_id, actor_user_id=actor_user_id)

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/configs/{{config_id:path}}")
        async def _read_config(
            app_name: str,
            config_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API config read request: node=%s app=%s config=%s", self.node_name, app_name, config_id
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_READ,))
            app = self._resolve_app(app_name)
            try:
                required_level = app.config_file_read_level_for_id(config_id)
            except ValueError as xcp:
                raise _http_exception(400, str(xcp)) from xcp
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_READ,),
                required_level=required_level,
                verified_grant=grant,
            )
            return self.read_config_file(app=app, config_id=config_id).to_mapping()

        @nicegui_app.put(f"{_NODE_API_PREFIX}/apps/{{app_name}}/configs/{{config_id:path}}")
        async def _write_config(
            app_name: str,
            config_id: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API config write request: node=%s app=%s config=%s", self.node_name, app_name, config_id
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.CONFIGS_WRITE,))
            write_request = NodeConfigWriteRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            try:
                required_level = app.config_file_write_level_for_id(config_id)
            except ValueError as xcp:
                raise _http_exception(400, str(xcp)) from xcp
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_WRITE,),
                required_level=required_level,
                verified_grant=grant,
            )
            actor_user_id = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.CONFIGS_WRITE,),
                verified_grant=grant,
            )
            result = self.write_config_file(app=app, config_id=config_id, content=write_request.content)
            audit_log(
                "config.file_written",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                config_id=config_id,
                required_level=required_level.name,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/saves")
        async def _list_saves(app_name: str, request: Request, access_token: str | None = None) -> dict[str, object]:
            traffic_log.info("Node API save list request: node=%s app=%s", self.node_name, app_name)
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_READ,))
            app = self._resolve_app(app_name)
            return (await self.build_save_list(app)).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/saves/{{save_id:path}}/download")
        async def _download_save(
            app_name: str,
            save_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> Response:
            traffic_log.info(
                "Node API save download request: node=%s app=%s save=%s", self.node_name, app_name, save_id
            )
            self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_DOWNLOAD,))
            app = self._resolve_app(app_name)
            return await self.build_save_download_response(app=app, save_id=save_id)

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/saves/upload")
        async def _upload_save(
            app_name: str,
            request: Request,
            root_id: Annotated[str, Form()],
            upload: Annotated[UploadFile, File()],
            filename: Annotated[str | None, Form()] = None,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API save upload request: node=%s app=%s root=%s", self.node_name, app_name, root_id)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_WRITE,)
            )
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SAVES_WRITE,),
                required_level=app.save_file_write_level,
                verified_grant=grant,
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SAVES_WRITE,),
                verified_grant=grant,
            )
            result: NodeSaveMutationResult = await self.upload_save_file(
                app=app,
                root_id=root_id,
                upload=upload,
                upload_name=filename,
                actor_user_id=actor_user_id,
            )
            audit_log(
                "save.file_uploaded",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                save_id=result.save.id,
                root_id=root_id,
                required_level=app.save_file_write_level.name,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/saves/{{save_id:path}}/rename")
        async def _rename_save(
            app_name: str,
            save_id: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API save rename request: node=%s app=%s save=%s", self.node_name, app_name, save_id)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_WRITE,)
            )
            rename_request: NodeSaveRenameRequest = NodeSaveRenameRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SAVES_WRITE,),
                required_level=app.save_file_write_level,
                verified_grant=grant,
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SAVES_WRITE,),
                verified_grant=grant,
            )
            result: NodeSaveMutationResult = await self.rename_save_file(
                app=app,
                save_id=save_id,
                new_name=rename_request.new_name,
                actor_user_id=actor_user_id,
            )
            audit_log(
                "save.file_renamed",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                save_id=save_id,
                destination_save_id=result.save.id,
                required_level=app.save_file_write_level.name,
            )
            return result.to_mapping()

        @nicegui_app.delete(f"{_NODE_API_PREFIX}/apps/{{app_name}}/saves/{{save_id:path}}")
        async def _delete_save(
            app_name: str,
            save_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API save delete request: node=%s app=%s save=%s", self.node_name, app_name, save_id)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SAVES_WRITE,)
            )
            app = self._resolve_app(app_name)
            await self._require_actor_level_for_request(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SAVES_WRITE,),
                required_level=app.save_file_write_level,
                verified_grant=grant,
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SAVES_WRITE,),
                verified_grant=grant,
            )
            result: NodeSaveMutationResult = await self.delete_save_file(
                app=app,
                save_id=save_id,
                actor_user_id=actor_user_id,
            )
            audit_log(
                "save.file_deleted",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                save_id=save_id,
                required_level=app.save_file_write_level.name,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/blueprints")
        async def _list_blueprints(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API blueprint list request: node=%s app=%s", self.node_name, app_name)
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.BLUEPRINTS_READ,))
            app = self._resolve_app(app_name)
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.BLUEPRINTS_READ,),
                verified_grant=grant,
            )
            return self.build_blueprint_list(app, actor_user_id=actor_user_id).to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/blueprints/upload")
        async def _upload_blueprint(
            app_name: str,
            request: Request,
            session_name: Annotated[str, Form()],
            upload: Annotated[list[UploadFile], File()],
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API blueprint upload request: node=%s app=%s session=%s",
                self.node_name,
                app_name,
                session_name,
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.BLUEPRINTS_WRITE,))
            app = self._resolve_app(app_name)
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
                verified_grant=grant,
            )
            result: NodeBlueprintMutationResult = await self.upload_blueprint_files(
                app=app,
                session_name=session_name,
                uploads=upload,
                actor_user_id=actor_user_id,
            )
            audit_log(
                "blueprint.file_uploaded",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                blueprint_id=result.blueprint.id,
                session_name=session_name,
            )
            return result.to_mapping()

        @nicegui_app.delete(f"{_NODE_API_PREFIX}/apps/{{app_name}}/blueprints/{{blueprint_id:path}}")
        async def _delete_blueprint(
            app_name: str,
            blueprint_id: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API blueprint delete request: node=%s app=%s blueprint=%s",
                self.node_name,
                app_name,
                blueprint_id,
            )
            grant = self._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.BLUEPRINTS_WRITE,))
            app = self._resolve_app(app_name)
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.BLUEPRINTS_WRITE,),
                verified_grant=grant,
            )
            result = self.delete_blueprint_file(
                app=app,
                blueprint_id=blueprint_id,
                actor_user_id=actor_user_id,
            )
            audit_log(
                "blueprint.file_deleted",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                blueprint_id=blueprint_id,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/settings")
        async def _list_settings(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API setting list request: node=%s app=%s", self.node_name, app_name)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_READ,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SETTINGS_READ,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            return self.build_setting_list(app=app, actor_user_id=actor_user_id).to_mapping()

        @nicegui_app.put(f"{_NODE_API_PREFIX}/apps/{{app_name}}/settings/{{setting_key}}")
        async def _write_setting(
            app_name: str,
            setting_key: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API setting write request: node=%s app=%s setting=%s", self.node_name, app_name, setting_key
            )
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_WRITE,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SETTINGS_WRITE,),
                verified_grant=grant,
            )
            write_request: NodeSettingWriteRequest = NodeSettingWriteRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            result: NodeSettingMutationResult = await self.update_setting(
                app=app,
                setting_key=setting_key,
                value=write_request.value,
                actor_user_id=actor_user_id,
            )
            return result.to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/settings/save")
        async def _save_settings(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API settings save request: node=%s app=%s", self.node_name, app_name)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_WRITE,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SETTINGS_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            return (await self.save_settings(app=app, actor_user_id=actor_user_id)).to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/settings/reload")
        async def _reload_settings(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API settings reload request: node=%s app=%s", self.node_name, app_name)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.SETTINGS_WRITE,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.SETTINGS_WRITE,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            return (await self.reload_settings(app=app, actor_user_id=actor_user_id)).to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/console-actions")
        async def _list_console_actions(
            app_name: str,
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info("Node API console action list request: node=%s app=%s", self.node_name, app_name)
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_CONTROL,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APP_CONTROL,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            return self.build_console_action_list(app=app, actor_user_id=actor_user_id).to_mapping()

        @nicegui_app.post(f"{_NODE_API_PREFIX}/apps/{{app_name}}/console-actions/{{action_key}}")
        async def _execute_console_action_route(
            app_name: str,
            action_key: str,
            payload: dict[str, object],
            request: Request,
            access_token: str | None = None,
        ) -> dict[str, object]:
            traffic_log.info(
                "Node API console action execute request: node=%s app=%s action=%s",
                self.node_name,
                app_name,
                action_key,
            )
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_CONTROL,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APP_CONTROL,),
                verified_grant=grant,
            )
            execute_request: NodeConsoleActionExecuteRequest = NodeConsoleActionExecuteRequest.model_validate(payload)
            app = self._resolve_app(app_name)
            result = await self.execute_console_action(
                app=app,
                action_key=action_key,
                raw_value=execute_request.value,
                actor_user_id=actor_user_id,
            )
            audit_log(
                "app.console_action_executed",
                actor_user_id=actor_user_id,
                node_name=self.node_name,
                app_name=app.name,
                console_action_key=action_key,
                required_level=self._resolve_console_action(app, action_key).power_level.name,
                success=result.success,
            )
            return result.to_mapping()

        @nicegui_app.get(f"{_NODE_API_PREFIX}/apps/{{app_name}}/console/stdout")
        async def _read_console_stdout_route(
            app_name: str,
            request: Request,
            access_token: str | None = None,
            max_lines: int = 200,
        ) -> dict[str, object]:
            traffic_log.info("Node API console stdout request: node=%s app=%s", self.node_name, app_name)
            if max_lines < 1 or max_lines > 500:
                raise _http_exception(400, "Console stdout line limit must be between 1 and 500.")
            grant: NodeAccessGrant | None = self._require_access(
                request, access_token, app_name=app_name, scopes=(NodeApiScope.APP_CONTROL,)
            )
            actor_user_id: int = self._request_actor_user_id(
                request=request,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APP_CONTROL,),
                verified_grant=grant,
            )
            app = self._resolve_app(app_name)
            result = await self.read_console_stdout(app=app, actor_user_id=actor_user_id, max_lines=max_lines)
            return result.to_mapping()

        @nicegui_app.websocket(f"{_NODE_API_PREFIX}/apps/{{app_name}}/console/stdout/stream")
        async def _console_stdout_stream(
            websocket: WebSocket,
            app_name: str,
            access_token: str | None = None,
            max_lines: int = 200,
        ) -> None:
            traffic_log.info("Node API console stdout stream request: node=%s app=%s", self.node_name, app_name)
            if max_lines < 1 or max_lines > 500:
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid stdout line limit.")
            self._require_websocket_token_access(
                websocket=websocket,
                access_token=access_token,
                app_name=app_name,
                scopes=(NodeApiScope.APP_CONTROL,),
            )
            try:
                app = self._resolve_app(app_name)
            except HTTPException as xcp:
                raise self._websocket_exception_from_http(xcp) from xcp
            await self._serve_console_stdout_stream(websocket=websocket, app=app, max_lines=max_lines)

        @nicegui_app.get(f"{_NODE_API_PREFIX}/{{missing_path:path}}")
        async def _missing_node_api_route(missing_path: str) -> dict[str, object]:
            if self._should_log_missing_route_warning(missing_path):
                log.warning("Node API route not found: /%s/%s", _NODE_API_PREFIX.strip("/"), missing_path)
            raise _http_exception(404, f"Unknown node API route: /{_NODE_API_PREFIX.strip('/')}/{missing_path}")

        self._routes_registered = True

    async def list_apps(self) -> tuple[NodeAppEntry, ...]:
        now = time.monotonic()
        cached = self._app_entries_cache
        if cached is not None and now - cached.captured_at_seconds < _NODE_APP_ENTRY_CACHE_TTL_SECONDS:
            return cached.entries
        if cached is not None and self._app_entries_cache_lock.locked():
            return cached.entries
        async with self._app_entries_cache_lock:
            now = time.monotonic()
            cached = self._app_entries_cache
            if cached is not None and now - cached.captured_at_seconds < _NODE_APP_ENTRY_CACHE_TTL_SECONDS:
                return cached.entries
            try:
                entries = await self._build_app_entries()
            except Exception as xcp:
                if cached is None:
                    raise
                self._app_entries_cache = _TimedNodeAppEntries(
                    captured_at_seconds=time.monotonic(),
                    entries=cached.entries,
                )
                log.warning(
                    "Node API app entry refresh failed; serving stale entries: node=%s error=%s",
                    self.node_name,
                    xcp,
                )
                return cached.entries
            self._app_entries_cache = _TimedNodeAppEntries(
                captured_at_seconds=time.monotonic(),
                entries=entries,
            )
            return entries

    async def _build_app_entries(self) -> tuple[NodeAppEntry, ...]:
        manager: App_Manager = self._require_manager()
        apps = tuple(sorted(manager.apps.values(), key=lambda item: item.friendly.casefold()))
        return tuple(await asyncio.gather(*(self._build_live_app_entry(app) for app in apps)))

    async def build_live_app_entry(self, app: App) -> NodeAppEntry:
        now = time.monotonic()
        cached = self._app_entries_cache
        if cached is not None and now - cached.captured_at_seconds < _NODE_APP_ENTRY_CACHE_TTL_SECONDS:
            app_key = app.name.casefold()
            for entry in cached.entries:
                if entry.name.casefold() == app_key:
                    return entry
        return await self._build_live_app_entry(app)

    async def _build_live_app_entry(self, app: App) -> NodeAppEntry:
        player_snapshot = await self._app_player_snapshot(app)
        return self.build_app_entry(
            app,
            transition_state=self._cached_app_transition_state(app.name),
            player_count=None if player_snapshot is None else player_snapshot.player_count,
            player_capacity=None if player_snapshot is None else player_snapshot.player_capacity,
            connected_player_names=() if player_snapshot is None else player_snapshot.connected_player_names,
        )

    def build_app_entry(
        self,
        app: App,
        *,
        transition_state: NodeAppTransitionState | None = None,
        player_count: int | None = None,
        player_capacity: int | None = None,
        connected_player_names: tuple[str, ...] = (),
    ) -> NodeAppEntry:
        app_scope = getattr(app, "scope", None)
        update_info = getattr(app, "update_info", None)
        update_status = getattr(app, "update_status", None)
        raw_activity_provider_entries = getattr(app, "activity_provider_entries", ())
        activity_provider_entries = (
            tuple(raw_activity_provider_entries)
            if isinstance(raw_activity_provider_entries, list | tuple)
            else ()
        )
        relay_notice_player_session = getattr(app, "relay_notice_player_session_enabled", None)
        relay_notice_player_death = getattr(app, "relay_notice_player_death_enabled", None)
        relay_notice_progress = getattr(app, "relay_notice_progress_enabled", None)
        relay_advancements_enabled = getattr(app, "relay_advancements_enabled", None)
        rcon_requires_online_players = getattr(app, "rcon_requires_online_players_enabled", None)
        return NodeAppEntry(
            name=app.name,
            friendly=app.friendly,
            node=self.node_name,
            running=app.check_running(),
            enabled=app.cfg.enabled,
            supports_mods=app.mods is not None,
            supports_configs=app.supports_config_files,
            scope=app_scope if isinstance(app_scope, str) else app_scope_from_name(app.name),
            transition_state=(
                self._cached_app_transition_state(app.name) if transition_state is None else transition_state
            ),
            player_count=player_count,
            player_capacity=player_capacity,
            connected_player_names=connected_player_names,
            supports_saves=app.supports_save_files,
            supports_save_uploads=app.supports_save_uploads,
            supports_save_rename=app.supports_save_rename,
            supports_blueprints=bool(getattr(app, "supports_blueprints", False)),
            supports_settings=app.supports_settings,
            supports_console_actions=bool(getattr(app, "supports_console_actions", False)),
            supports_chat=app.supports_chat_relay,
            supports_updates=update_info is not None,
            supports_sevendays_sandbox_options=bool(getattr(app, "supports_sevendays_sandbox_options", False)),
            client_pack_content_dirty=app.cfg.client_pack_content_dirty,
            client_pack_published_version=app.cfg.client_pack_published_version,
            client_pack_next_version=app.next_client_pack_version,
            client_pack_published_changelog=app.cfg.client_pack_published_changelog,
            client_pack_releases=app.client_pack_releases,
            client_pack_kubejs_scripts=self._client_pack_kubejs_scripts(app),
            client_pack_metadata=self._client_pack_metadata(app),
            client_pack_file_previews=self._client_pack_file_previews(app),
            client_pack_automated_changelog=self._client_pack_automated_changelog(app),
            runtime_fault=getattr(app, "runtime_fault", None),
            update_info=update_info,
            update_status=update_status,
            config_read_level=app.lowest_config_file_read_level,
            config_write_level=app.config_file_write_level,
            save_write_level=app.save_file_write_level,
            color_hex=self.app_color_hex(app.manage_embed_color),
            map_url=app.public_map_url,
            join_address=app.cfg.join_address,
            join_direct_ip_address=app.cfg.join_direct_ip_address,
            resource_points=self._app_resource_point_summary(app),
            title_font_preset=getattr(app.cfg, "title_font_preset", AppTitleFont.AUTO.value),
            notes=getattr(app.cfg, "notes", None),
            lifecycle_notice_started=getattr(app.cfg, "lifecycle_notice_started", True),
            lifecycle_notice_stopped=getattr(app.cfg, "lifecycle_notice_stopped", True),
            lifecycle_notice_crashed=getattr(app.cfg, "lifecycle_notice_crashed", True),
            relay_notice_player_session=relay_notice_player_session,
            relay_notice_player_death=relay_notice_player_death,
            relay_notice_progress=relay_notice_progress,
            relay_notice_progress_label=(
                getattr(app, "relay_progress_notice_term", None) if relay_notice_progress is not None else None
            ),
            relay_advancements_enabled=relay_advancements_enabled,
            relay_advancement_term=(
                getattr(app, "relay_advancement_term", None) if relay_advancements_enabled is not None else None
            ),
            factorio_chat_relay_use_shout=(
                getattr(app.cfg, "factorio_chat_relay_use_shout", True) if app.scope == "factorio" else None
            ),
            rcon_requires_online_players=rcon_requires_online_players,
            activity_providers=tuple(
                NodeAppActivityProviderEntry(
                    provider_id=entry.provider_id,
                    label=entry.label,
                    enabled=entry.enabled,
                    current_value=entry.current_value,
                    detail_value=entry.detail_value,
                )
                for entry in activity_provider_entries
            ),
        )

    def build_minecraft_recipe_workspace_state(self, app: App) -> NodeMinecraftRecipeWorkspaceState:
        if not isinstance(app, Minecraft):
            raise _http_exception(404, f"App {app.name!r} does not expose Minecraft recipe data.")
        return _build_minecraft_recipe_workspace_state(app)

    def build_sevendays_sandbox_options_state(self, app: App) -> NodeSevenDaysSandboxOptionsState:
        if not isinstance(app, SevenDays):
            raise _http_exception(404, f"App {app.name!r} does not expose 7D2D sandbox options.")
        return _build_sevendays_sandbox_options_state(app)

    def build_minecraft_item_icon_response(self, app: App, *, item_id: str) -> Response:
        if not isinstance(app, Minecraft):
            raise _http_exception(404, f"App {app.name!r} does not expose Minecraft recipe item icons.")
        try:
            return _build_minecraft_item_icon_response(app, item_id=item_id)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

    @staticmethod
    def minecraft_item_icon_placeholder_svg(item_id: str) -> str:
        return _minecraft_item_icon_placeholder_svg(item_id)

    async def append_minecraft_recipe_mutation(
        self,
        *,
        app: App,
        mutation: MinecraftRecipeMutation,
        actor_user_id: int,
    ) -> NodeMinecraftRecipeMutationResult:
        mutation_request = NodeMinecraftRecipeMutationRequest(
            action=NodeMinecraftRecipeMutationAction.ADD,
            mutation=mutation,
        )
        return await self.mutate_minecraft_recipe_book(
            app=app,
            mutation_request=mutation_request,
            actor_user_id=actor_user_id,
        )

    async def mutate_minecraft_recipe_book(
        self,
        *,
        app: App,
        mutation_request: NodeMinecraftRecipeMutationRequest,
        actor_user_id: int,
    ) -> NodeMinecraftRecipeMutationResult:
        if not isinstance(app, Minecraft):
            raise _http_exception(404, f"App {app.name!r} does not expose Minecraft recipe data.")
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        try:
            _apply_minecraft_recipe_mutation(
                app=app,
                mutation_request=mutation_request,
                actor_user_id=actor_user_id,
            )
        except IndexError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Minecraft recipe mutation failed: {xcp}") from xcp
        traffic_log.info(
            "Node API Minecraft recipe mutation applied: node=%s app=%s actor=%s action=%s index=%s kind=%s",
            self.node_name,
            app.name,
            actor_user_id,
            mutation_request.action.value,
            mutation_request.mutation_index,
            None if mutation_request.mutation is None else mutation_request.mutation.to_mapping().get("kind"),
        )
        return NodeMinecraftRecipeMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"Saved Minecraft recipe change for {app.friendly}.",
            workspace=self.build_minecraft_recipe_workspace_state(app),
        )

    def _cached_app_transition_state(self, app_name: str) -> NodeAppTransitionState:
        key = app_name.casefold()
        snapshot = self._app_transition_cache.get(key)
        if snapshot is None:
            return NodeAppTransitionState.NONE
        task = self._app_mutation_tasks.get(key)
        if task is not None and task.done():
            self._app_mutation_tasks.pop(key, None)
            task = None
        if task is not None:
            return snapshot.state
        if time.monotonic() - snapshot.requested_at_seconds >= _APP_TRANSITION_TTL_SECONDS:
            self._app_transition_cache.pop(key, None)
            return NodeAppTransitionState.NONE
        return snapshot.state

    def _remember_app_transition_state(self, app_name: str, state: NodeAppTransitionState) -> None:
        key = app_name.casefold()
        if state is NodeAppTransitionState.NONE:
            self._app_transition_cache.pop(key, None)
            return
        self._app_transition_cache[key] = NodeAppTransitionSnapshot(
            state=state,
            requested_at_seconds=time.monotonic(),
        )

    def _track_app_mutation_task(
        self,
        *,
        app_name: str,
        action: NodeAppMutationAction,
        state: NodeAppTransitionState,
        task: asyncio.Task[None],
    ) -> None:
        key = app_name.casefold()
        self._remember_app_transition_state(app_name, state)
        self._app_mutation_tasks[key] = task

        def _finalise_task(completed_task: asyncio.Task[None]) -> None:
            self._finish_app_mutation_task(app_name=app_name, action=action, task=completed_task)

        task.add_done_callback(_finalise_task)

    def _finish_app_mutation_task(
        self,
        *,
        app_name: str,
        action: NodeAppMutationAction,
        task: asyncio.Task[None],
    ) -> None:
        key = app_name.casefold()
        tracked_task = self._app_mutation_tasks.get(key)
        if tracked_task is task:
            self._app_mutation_tasks.pop(key, None)
            self._remember_app_transition_state(app_name, NodeAppTransitionState.NONE)
        try:
            task.result()
        except asyncio.CancelledError:
            log.info("Node API app mutation task cancelled: node=%s app=%s action=%s", self.node_name, app_name, action)
        except Exception:
            log.exception("Node API app mutation failed: node=%s app=%s action=%s", self.node_name, app_name, action)

    async def _run_app_mutation_task(
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
            self._invalidate_state_caches(app_name=app.name)

    @staticmethod
    def app_color_hex(color: int | None) -> str | None:
        if color is None:
            return None
        if color < 0 or color > 0xFFFFFF:
            raise ValueError(f"App color must be between 0x000000 and 0xFFFFFF, got {color!r}.")
        return f"#{color:06X}"

    @staticmethod
    def _app_resource_point_summary(app: App) -> NodeAppResourcePointSummary | None:
        resource_points = getattr(getattr(app, "cfg", None), "resource_points", None)
        running_points = getattr(resource_points, "running", None)
        startup_points = getattr(resource_points, "startup_points", None)
        if running_points is None or startup_points is None:
            return None
        return NodeAppResourcePointSummary(
            cpu_points_running=getattr(running_points, "cpu_points", 0),
            cpu_points_startup=getattr(startup_points, "cpu_points", 0),
            ram_points_running=getattr(running_points, "ram_points", 0),
            ram_points_startup=getattr(startup_points, "ram_points", 0),
            startup_defined=getattr(resource_points, "startup", None) is not None,
        )

    def build_system_summary(self, *, force_refresh: bool = False) -> NodeSystemSummary:
        with self._system_summary_cache_lock:
            now = time.monotonic()
            cached = self._system_summary_cache
            if (
                not force_refresh
                and cached is not None
                and now - cached.captured_at_seconds < _NODE_SYSTEM_SUMMARY_CACHE_TTL_SECONDS
            ):
                return cached.summary
            summary = self._build_system_summary_uncached()
            self._record_system_sample(summary)
            self._system_summary_cache = _TimedNodeSystemSummary(
                captured_at_seconds=time.monotonic(),
                summary=summary,
            )
            return summary

    def _record_system_sample(self, summary: NodeSystemSummary) -> None:
        sample = NodeSystemSample.from_summary(summary)
        with self._system_history_lock:
            if self._system_history:
                previous = self._system_history[-1]
                elapsed = sample.captured_at_epoch_seconds - previous.captured_at_epoch_seconds
                if elapsed < 0:
                    self._system_history.clear()
                elif elapsed < _NODE_SYSTEM_HISTORY_INTERVAL_SECONDS:
                    return
            self._system_history.append(sample)

    def build_system_history(self) -> NodeSystemHistory:
        with self._system_history_lock:
            samples = tuple(self._system_history)
        return NodeSystemHistory(
            retention_seconds=_NODE_SYSTEM_HISTORY_RETENTION_SECONDS,
            sample_interval_seconds=int(_NODE_SYSTEM_HISTORY_INTERVAL_SECONDS),
            samples=samples,
        )

    def _build_system_summary_uncached(self) -> NodeSystemSummary:
        cpu_percent: int | None = None
        cpu_per_core_percent: tuple[int, ...] = ()
        ram_percent: int | None = None
        ram_used_bytes: int | None = None
        ram_total_bytes: int | None = None
        storage_percent: int | None = None
        storage_free_bytes: int | None = None
        storage_total_bytes: int | None = None
        disks: tuple[NodeSystemDiskSummary, ...] = ()
        bot_uptime_seconds: int | None = None
        uptime_seconds: int | None = None
        cpu_points_available: int | None = None
        cpu_points_capacity: int | None = None
        ram_points_available: int | None = None
        ram_points_capacity: int | None = None
        running_names: tuple[str, ...] = ()
        running_app_ids: tuple[str, ...] = ()
        running_app_scopes: tuple[str, ...] = ()
        start_blocked_app_ids: tuple[str, ...] = ()

        try:
            system_stats: Stats_System = Stats_System()
            snapshot: StatsSystemSnapshot = system_stats.system_snapshot(refresh=True)
        except Exception as xcp:
            log.warning("Node API system stats failed: node=%s error=%s", self.node_name, xcp)
        else:
            cpu_percent = snapshot.cpu_percent
            cpu_per_core_percent = snapshot.cpu_per_core_percent
            ram_percent = snapshot.ram_percent
            ram_used_bytes = snapshot.ram_used_bytes
            ram_total_bytes = snapshot.ram_total_bytes
            primary_disk: StatsDiskSnapshot | None = snapshot.primary_disk
            if primary_disk is not None:
                storage_percent = primary_disk.percent
                storage_free_bytes = primary_disk.free_bytes
                storage_total_bytes = primary_disk.total_bytes
            disks = tuple(
                NodeSystemDiskSummary(
                    mountpoint=disk.mountpoint_text,
                    label=disk.display_name,
                    percent=disk.percent,
                    free_bytes=disk.free_bytes,
                    total_bytes=disk.total_bytes,
                )
                for disk in snapshot.disks
            )
        try:
            bot_uptime_seconds = max(0, int(time.time() - psutil.Process().create_time()))
        except Exception as xcp:
            log.warning("Node API bot uptime probe failed: node=%s error=%s", self.node_name, xcp)
        try:
            uptime_seconds = max(0, int(time.time() - psutil.boot_time()))
        except Exception as xcp:
            log.warning("Node API uptime probe failed: node=%s error=%s", self.node_name, xcp)
        if self._manager is not None:
            try:
                capacity = self._manager.node_capacity()
                usage = self._manager.active_resource_point_usage()
            except Exception as xcp:
                log.warning("Node API resource point summary failed: node=%s error=%s", self.node_name, xcp)
            else:
                cpu_points_capacity = capacity.cpu_points_available
                ram_points_capacity = capacity.ram_points_available
                cpu_points_available = max(0, cpu_points_capacity - usage.cpu_points)
                ram_points_available = max(0, ram_points_capacity - usage.ram_points)
            running_apps = tuple(
                (
                    app.name,
                    app.friendly,
                    app.scope if isinstance(getattr(app, "scope", None), str) else app_scope_from_name(app.name) or "",
                )
                for app in sorted(self._manager.apps.values(), key=lambda item: item.friendly.casefold())
                if app.check_running()
            )
            running_names = tuple(app_friendly for _app_name, app_friendly, _app_scope in running_apps)
            running_app_ids = tuple(app_name for app_name, _app_friendly, _app_scope in running_apps)
            running_app_scopes = tuple(app_scope for _app_name, _app_friendly, app_scope in running_apps)
            start_blocked_app_ids = tuple(
                app.name
                for app in sorted(self._manager.apps.values(), key=lambda item: item.friendly.casefold())
                if not app.check_running() and self._manager.start_blocker(app, include_current_activity=False) is not None
            )

        return NodeSystemSummary(
            cpu_percent=cpu_percent,
            ram_percent=ram_percent,
            ram_used_bytes=ram_used_bytes,
            ram_total_bytes=ram_total_bytes,
            storage_percent=storage_percent,
            storage_free_bytes=storage_free_bytes,
            storage_total_bytes=storage_total_bytes,
            cpu_per_core_percent=cpu_per_core_percent,
            disks=disks,
            bot_uptime_seconds=bot_uptime_seconds,
            uptime_seconds=uptime_seconds,
            cpu_points_available=cpu_points_available,
            cpu_points_capacity=cpu_points_capacity,
            ram_points_available=ram_points_available,
            ram_points_capacity=ram_points_capacity,
            running_names=running_names,
            running_app_ids=running_app_ids,
            running_app_scopes=running_app_scopes,
            start_blocked_app_ids=start_blocked_app_ids,
            captured_at_epoch_seconds=int(time.time()),
        )

    @staticmethod
    def _app_footprint_paths(app: App) -> tuple[Path, ...]:
        candidates: list[Path] = [app.directory]
        for optional_path in (
            app.cfg.mods_dir,
            app.cfg.client_mods_dir,
            app.cfg.client_overrides_dir,
            app.cfg.settings_pointer,
            app.cfg.server_log_file,
        ):
            if optional_path is not None:
                candidates.append(optional_path)
        candidates.extend(root.path for root in app.config_file_roots)

        resolved_candidates = sorted(
            {candidate.resolve(strict=False) for candidate in candidates},
            key=lambda path: (len(path.parts), str(path).casefold()),
        )
        included_paths: list[Path] = []
        for candidate in resolved_candidates:
            if any(candidate == included or candidate.is_relative_to(included) for included in included_paths):
                continue
            included_paths.append(candidate)
        return tuple(included_paths)

    @staticmethod
    def _calculate_app_footprint_size_bytes(paths: tuple[Path, ...]) -> int:
        return sum(File_Utils.pointer_size(path) for path in paths)

    def _app_footprint_size_bytes(self, app: App) -> int:
        paths = self._app_footprint_paths(app)
        now = time.time()
        cached = self._app_footprint_cache.get(app.name)
        if (
            cached is not None
            and cached.paths == paths
            and now - cached.measured_at_seconds < _APP_FOOTPRINT_CACHE_TTL_SECONDS
        ):
            return cached.size_bytes

        size_bytes = self._calculate_app_footprint_size_bytes(paths)
        self._app_footprint_cache[app.name] = NodeAppFootprintSnapshot(
            paths=paths,
            measured_at_seconds=now,
            size_bytes=size_bytes,
        )
        return size_bytes

    @staticmethod
    def _require_chat_relay_app(app: App) -> None:
        if not app.supports_chat_relay:
            raise _http_exception(404, f"{app.friendly} does not expose a chat relay.")

    def build_chat_room_snapshot(self, app: App, *, limit: int = _NODE_CHAT_HISTORY_LIMIT) -> NodeChatRoomSnapshot:
        self._require_chat_relay_app(app)
        bounded_limit = max(0, min(limit, _NODE_CHAT_HISTORY_LIMIT))
        hub = ChatHub()
        endpoint_summaries = self._chat_endpoint_summaries(app, endpoints=hub.endpoints_for_room(app.name))
        return NodeChatRoomSnapshot(
            room_id=app.name,
            endpoint_count=len(endpoint_summaries),
            events=hub.history(app.name, limit=bounded_limit),
            endpoint_summaries=endpoint_summaries,
            revision=hub.room_revision(app.name),
        )

    def _chat_endpoint_summaries(
        self,
        app: App,
        *,
        endpoints: tuple[ChatEndpoint, ...],
    ) -> tuple[NodeChatEndpointSummary, ...]:
        app_running = app.check_running()
        summaries: list[NodeChatEndpointSummary] = []
        seen_keys: set[str] = set()
        for endpoint in endpoints:
            summary = self._chat_endpoint_summary(app, endpoint, app_running=app_running)
            if summary is None:
                continue
            summary_key, summary_label = summary
            if summary_key in seen_keys:
                continue
            seen_keys.add(summary_key)
            summaries.append(NodeChatEndpointSummary(label=summary_label))
        return tuple(summaries)

    def _chat_endpoint_summary(
        self,
        app: App,
        endpoint: ChatEndpoint,
        *,
        app_running: bool,
    ) -> tuple[str, str] | None:
        endpoint_id = endpoint.id
        if endpoint_id.kind is ChatEndpointKind.APP:
            if not app_running:
                return None
            label = endpoint.label or app.friendly
            return endpoint_id.stable_key, f"Game: {label}"
        if endpoint_id.kind is ChatEndpointKind.DISCORD_CHANNEL:
            return self._discord_endpoint_summary(endpoint)
        if endpoint_id.kind is ChatEndpointKind.DISCORD_TTS:
            label = endpoint.label or endpoint_id.value
            return endpoint_id.stable_key, f"Discord TTS: {label}"
        if endpoint_id.kind is ChatEndpointKind.WEB_SESSION:
            label = endpoint.label or "Dashboard"
            return endpoint_id.stable_key, f"Web: {label}"
        if endpoint_id.kind is ChatEndpointKind.SYSTEM:
            label = endpoint.label or "System"
            return endpoint_id.stable_key, f"System: {label}"
        raise ValueError(f"Unsupported chat endpoint kind: {endpoint_id.kind}")

    def _discord_endpoint_summary(self, endpoint: ChatEndpoint) -> tuple[str, str]:
        endpoint_id = endpoint.id
        channel_id = self._discord_endpoint_channel_id(endpoint_id)
        channel = self._discord_channel_cache_entry(channel_id)
        guild_id = getattr(channel, "guild_id", None)
        if isinstance(guild_id, int | str):
            guild_id_int = int(guild_id)
            guild_name = self._discord_guild_name(guild_id_int)
            guild_label = guild_name or str(guild_id_int)
            return f"discord_guild:{guild_id_int}", f"Discord: {guild_label}"

        channel_name = getattr(channel, "name", None)
        if isinstance(channel_name, str) and channel_name.strip():
            return endpoint_id.stable_key, f"Discord: {channel_name}"
        if endpoint.label is not None and endpoint.label.strip():
            return endpoint_id.stable_key, f"Discord: {endpoint.label}"
        return endpoint_id.stable_key, f"Discord: {endpoint_id.value}"

    @staticmethod
    def _discord_endpoint_channel_id(endpoint_id: ChatEndpointId) -> int | None:
        try:
            return int(endpoint_id.value)
        except (TypeError, ValueError):
            return None

    def _discord_channel_cache_entry(self, channel_id: int | None) -> object | None:
        if channel_id is None:
            return None
        manager = self._manager
        bot = getattr(manager, "bot", None) if manager is not None else None
        cache = getattr(bot, "cache", None) if bot is not None else None
        get_guild_channel = getattr(cache, "get_guild_channel", None) if cache is not None else None
        if callable(get_guild_channel):
            return get_guild_channel(channel_id)
        return None

    def _discord_guild_name(self, guild_id: int) -> str | None:
        manager = self._manager
        bot = getattr(manager, "bot", None) if manager is not None else None
        cache = getattr(bot, "cache", None) if bot is not None else None
        get_guild = getattr(cache, "get_guild", None) if cache is not None else None
        guild = get_guild(guild_id) if callable(get_guild) else None
        guild_name = getattr(guild, "name", None)
        if isinstance(guild_name, str) and guild_name.strip():
            return guild_name
        return None

    async def publish_app_web_chat(
        self,
        *,
        app: App,
        actor_user_id: int,
        chat_request: NodeWebChatRequest,
    ) -> ChatEvent:
        self._require_chat_relay_app(app)
        if self._chat_relay is None:
            raise _http_exception(503, "Web chat relay is not available on this node.")
        return await self._chat_relay.publish_web_chat(
            room_id=app.name,
            session_id=chat_request.session_id,
            author_display_name=chat_request.author_display_name,
            author_id=str(actor_user_id),
            discord_user_id=actor_user_id,
            content=chat_request.content,
            reply_to_event_id=chat_request.reply_to_event_id,
        )

    async def queue_relay_tts(self, relay_request: NodeRelayTTSRequest) -> NodeRelayTTSResult:
        if self._relay_tts_service is None:
            log.warning(
                "Node API relay TTS unavailable: node=%s source_app=%s player=%s",
                self.node_name,
                relay_request.source_app,
                relay_request.player_name,
            )
            raise _http_exception(503, "Relay TTS is not available on this node.")

        try:
            spoken, queue_size = await self._relay_tts_service.queue_relay_message(
                relay_request.guild_id,
                relay_request.channel_id,
                relay_request.message_id,
                relay_request.text,
                user_id=relay_request.user_id,
            )
        except (RuntimeError, ValueError) as xcp:
            reason = str(xcp)
            traffic_log.info(
                "Node API relay TTS skipped: node=%s source_app=%s player=%s guild=%s channel=%s message_id=%s reason=%s",
                self.node_name,
                relay_request.source_app,
                relay_request.player_name,
                relay_request.guild_id,
                relay_request.channel_id,
                relay_request.message_id,
                reason,
            )
            return NodeRelayTTSResult(queued=False, reason=reason)

        traffic_log.info(
            "Node API relay TTS queued: node=%s source_app=%s player=%s guild=%s channel=%s message_id=%s queue_size=%s",
            self.node_name,
            relay_request.source_app,
            relay_request.player_name,
            relay_request.guild_id,
            relay_request.channel_id,
            relay_request.message_id,
            queue_size,
        )
        return NodeRelayTTSResult(queued=True, spoken=spoken, queue_size=queue_size)

    async def build_mod_list(self, app: App) -> NodeModList:
        inventory, app_stats = await asyncio.gather(
            self._cached_mod_inventory(app),
            self.build_cached_app_runtime_summary(app),
        )
        traffic_log.info(
            "Node API built mod list: node=%s app=%s mods=%s",
            self.node_name,
            app.name,
            len(inventory.mods),
        )
        return NodeModList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            summary=inventory.summary,
            mods=inventory.mods,
            app_stats=app_stats,
        )

    async def _cached_mod_inventory(self, app: App) -> _TimedModInventory:
        app_key = app.name.casefold()
        now = time.monotonic()
        cached = self._mod_inventory_cache.get(app_key)
        if cached is not None and now - cached.captured_at_seconds < _MOD_INVENTORY_CACHE_TTL_SECONDS:
            return cached
        lock = self._mod_inventory_cache_locks.setdefault(app_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._mod_inventory_cache.get(app_key)
            if cached is not None and now - cached.captured_at_seconds < _MOD_INVENTORY_CACHE_TTL_SECONDS:
                return cached
            await app.has_mod_manager.reload_mods()
            mods = tuple(app.has_mod_manager.list_mods())
            inventory = _TimedModInventory(
                captured_at_seconds=time.monotonic(),
                summary=NodeModSummary(
                    total_count=len(mods),
                    enabled_count=sum(
                        1 for mod in mods if mod.cfg.placement is ModPlacement.SERVER_ENABLED
                    ),
                    disabled_count=sum(
                        1 for mod in mods if mod.cfg.placement is ModPlacement.SERVER_DISABLED
                    ),
                    coremod_count=sum(1 for mod in mods if mod.counts_as_coremod),
                    downloadable_count=sum(1 for mod in mods if mod.downloadable),
                    non_downloadable_count=sum(1 for mod in mods if not mod.downloadable),
                    client_only_count=sum(
                        1 for mod in mods if mod.cfg.placement is ModPlacement.CLIENT_ONLY
                    ),
                    client_pack_eligible_count=sum(1 for mod in mods if mod.client_pack_eligible),
                ),
                mods=tuple(self._mod_entry(mod) for mod in mods),
            )
            self._mod_inventory_cache[app_key] = inventory
            return inventory

    async def build_app_runtime_summary(
        self,
        app: App,
        *,
        include_storage: bool = True,
        include_footprint: bool = True,
    ) -> NodeAppRuntimeSummary:
        player_count: int | None = None
        player_capacity: int | None = None
        connected_player_names: tuple[str, ...] = ()
        player_snapshot = await self._app_player_snapshot(app)
        if player_snapshot is not None:
            player_count = player_snapshot.player_count
            player_capacity = player_snapshot.player_capacity
            connected_player_names = player_snapshot.connected_player_names
        transition_state = self._cached_app_transition_state(app.name)

        storage_percent: int | None = None
        storage_free_bytes: int | None = None
        storage_total_bytes: int | None = None
        footprint_bytes: int | None = None
        activity_providers: tuple[NodeAppActivityProviderEntry, ...] = ()
        if include_storage:
            try:
                system_stats = Stats_System()
                storage_disk = system_stats.disk_snapshot_for_path(app.directory, refresh=True)
            except Exception as xcp:
                log.warning("Node API storage stats failed: node=%s app=%s error=%s", self.node_name, app.name, xcp)
            else:
                if storage_disk is not None:
                    storage_percent = storage_disk.percent
                    storage_free_bytes = storage_disk.free_bytes
                    storage_total_bytes = storage_disk.total_bytes
        if include_footprint and not config.IS_SHUTTINGDOWN and not self._shutting_down:
            try:
                footprint_bytes = await asyncio.to_thread(self._app_footprint_size_bytes, app)
            except Exception as xcp:
                if not (config.IS_SHUTTINGDOWN and _is_executor_shutdown_error(xcp)):
                    log.warning("Node API footprint stats failed: node=%s app=%s error=%s", self.node_name, app.name, xcp)
        try:
            activity_providers = tuple(
                NodeAppActivityProviderEntry(
                    provider_id=entry.provider_id,
                    label=entry.label,
                    enabled=entry.enabled,
                    current_value=entry.current_value,
                    detail_value=entry.detail_value,
                )
                for entry in await app.activity_provider_entries_with_values()
            )
        except Exception as xcp:
            log.warning("Node API activity provider snapshot failed: node=%s app=%s error=%s", self.node_name, app.name, xcp)

        version = app.version_display
        if version == "none":
            version = None

        return NodeAppRuntimeSummary(
            running=app.check_running(),
            enabled=app.cfg.enabled,
            version=version,
            transition_state=transition_state,
            player_count=player_count,
            player_capacity=player_capacity,
            relay_support=app.chat_relay_support,
            storage_percent=storage_percent,
            storage_free_bytes=storage_free_bytes,
            storage_total_bytes=storage_total_bytes,
            footprint_bytes=footprint_bytes,
            runtime_fault=getattr(app, "runtime_fault", None),
            connected_player_names=connected_player_names,
            activity_providers=activity_providers,
        )

    async def build_cached_app_runtime_summary(self, app: App) -> NodeAppRuntimeSummary:
        app_key = app.name.casefold()
        now = time.monotonic()
        cached = self._full_runtime_cache.get(app_key)
        if cached is not None and now - cached.captured_at_seconds < _FULL_APP_RUNTIME_CACHE_TTL_SECONDS:
            return cached.summary
        lock = self._full_runtime_cache_locks.setdefault(app_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._full_runtime_cache.get(app_key)
            if cached is not None and now - cached.captured_at_seconds < _FULL_APP_RUNTIME_CACHE_TTL_SECONDS:
                return cached.summary
            summary = await self.build_app_runtime_summary(app)
            timed_summary = _TimedAppRuntimeSummary(
                captured_at_seconds=time.monotonic(),
                summary=summary,
            )
            self._full_runtime_cache[app_key] = timed_summary
            self._live_runtime_cache[app_key] = timed_summary
            return summary

    async def build_live_app_runtime_summary(self, app: App) -> NodeAppRuntimeSummary:
        app_key = app.name.casefold()
        now = time.monotonic()
        cached = self._live_runtime_cache.get(app_key)
        if cached is not None and now - cached.captured_at_seconds < _LIVE_APP_RUNTIME_CACHE_TTL_SECONDS:
            return cached.summary
        lock = self._live_runtime_cache_locks.setdefault(app_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._live_runtime_cache.get(app_key)
            if cached is not None and now - cached.captured_at_seconds < _LIVE_APP_RUNTIME_CACHE_TTL_SECONDS:
                return cached.summary
            summary = await self.build_app_runtime_summary(app, include_storage=False, include_footprint=False)
            self._live_runtime_cache[app_key] = _TimedAppRuntimeSummary(
                captured_at_seconds=time.monotonic(),
                summary=summary,
            )
            return summary

    def build_map_manifest(self, app: App) -> MapManifest:
        manifest, _ = self._build_map_manifest_result(app)
        return manifest

    def _build_map_manifest_result(self, app: App) -> tuple[MapManifest, _SquaremapProxyResponse]:
        public_map_url: str = self._require_map_app(app)
        settings_response = self._squaremap_proxy_response(app, "tiles/settings.json", allow_stale_on_error=True)
        settings = self._squaremap_json_object_from_response(settings_response, "tiles/settings.json")
        worlds = self._squaremap_world_summaries(settings)
        if not worlds:
            raise _http_exception(502, f"Squaremap did not expose any worlds for {app.friendly}.")
        initial_world_name = self._squaremap_initial_world_name(public_map_url)
        known_world_names = {world.name.casefold() for world in worlds}
        if initial_world_name is None or initial_world_name.casefold() not in known_world_names:
            initial_world_name = worlds[0].name
        return MapManifest(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self.node_name,
            public_map_url=public_map_url,
            icon_base_url="./assets",
            initial_world_name=initial_world_name,
            worlds=worlds,
        ), settings_response

    def build_map_annotation_list(self, app: App) -> MapAnnotationList:
        self._require_map_app(app)
        annotations = self._map_annotation_store(app).list_annotations()
        return MapAnnotationList(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self.node_name,
            annotations=annotations,
        )

    def create_map_annotation(
        self,
        app: App,
        draft: MapAnnotationDraft,
        created_by_user_id: int | None,
        created_by_name: str | None,
    ) -> MapAnnotationMutationResult:
        self._require_map_app(app)
        annotation = self._map_annotation_store(app).create_annotation(
            draft=draft,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
        )
        return MapAnnotationMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self.node_name,
            message=f"Created map annotation {annotation.annotation_id}.",
            annotation=annotation,
        )

    def delete_map_annotation(self, app: App, annotation_id: str) -> MapAnnotationMutationResult:
        self._require_map_app(app)
        try:
            removed = self._map_annotation_store(app).delete_annotation(annotation_id)
        except KeyError as xcp:
            raise _http_exception(404, f"Unknown map annotation: {annotation_id}") from xcp
        return MapAnnotationMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self.node_name,
            message=f"Deleted map annotation {removed.annotation_id}.",
            deleted_annotation_id=removed.annotation_id,
        )

    @staticmethod
    def _map_annotation_store(app: App) -> AppMapAnnotationStore:
        return AppMapAnnotationStore(app.map_annotations_path)

    def _require_map_app(self, app: App) -> str:
        public_map_url = app.public_map_url
        if public_map_url is None:
            raise _http_exception(404, f"{app.friendly} does not expose a public map.")
        return public_map_url

    @staticmethod
    def _squaremap_response_headers(proxy_response: _SquaremapProxyResponse) -> dict[str, str]:
        headers = dict(proxy_response.headers)
        headers[_MAP_SOURCE_HEADER_NAME] = "stale" if proxy_response.is_stale else "live"
        if proxy_response.cache_updated_at_unix_ms is not None:
            headers[_MAP_CACHE_UPDATED_AT_HEADER_NAME] = str(proxy_response.cache_updated_at_unix_ms)
        return headers

    def _squaremap_root_url(self, app: App) -> str:
        map_proxy_url = app.map_proxy_url
        if map_proxy_url is None:
            map_proxy_url = self._require_map_app(app)
        parsed = urlsplit(map_proxy_url)
        root_path = parsed.path.rstrip("/") + "/"
        return urlunsplit((parsed.scheme, parsed.netloc, root_path, "", ""))

    @staticmethod
    def _squaremap_initial_world_name(public_map_url: str) -> str | None:
        parsed = urlsplit(public_map_url)
        world_values = parse_qs(parsed.query, keep_blank_values=True).get("world", ())
        for candidate in reversed(world_values):
            world_name = candidate.strip()
            if world_name:
                return world_name
        return None

    @staticmethod
    def _squaremap_passthrough_headers(response: requests.Response) -> tuple[tuple[str, str], ...]:
        allowed_names = ("Cache-Control", "ETag", "Last-Modified", "Expires")
        headers: list[tuple[str, str]] = []
        for name in allowed_names:
            value = response.headers.get(name)
            if value:
                headers.append((name, value))
        return tuple(headers)

    def _squaremap_proxy_response(
        self,
        app: App,
        relative_path: str,
        raw_query: str = "",
        *,
        allow_stale_on_error: bool = False,
    ) -> _SquaremapProxyResponse:
        normalized_path = relative_path.lstrip("/")
        local_response = self._squaremap_local_proxy_response(app, normalized_path)
        if local_response is not None:
            return local_response
        url = f"{self._squaremap_root_url(app)}{normalized_path}"
        params = parse_qs(raw_query, keep_blank_values=True) if raw_query else None
        should_log_failure = not normalized_path.casefold().endswith(".png")
        try:
            response = requests.get(url, params=params, timeout=_SQUAREMAP_REQUEST_TIMEOUT_SECONDS)
        except requests.Timeout as xcp:
            cached_response = self._squaremap_cached_response(app, normalized_path) if allow_stale_on_error else None
            if should_log_failure:
                log.warning(
                    "Squaremap request timed out: app=%s url=%s query=%s stale_cache=%s",
                    app.name,
                    url,
                    raw_query,
                    cached_response is not None,
                )
            if cached_response is not None:
                return cached_response
            raise _http_exception(504, f"Squaremap request timed out: {relative_path}") from xcp
        except requests.RequestException as xcp:
            cached_response = self._squaremap_cached_response(app, normalized_path) if allow_stale_on_error else None
            if should_log_failure:
                log.warning(
                    "Squaremap request failed: app=%s url=%s query=%s stale_cache=%s error=%s: %s",
                    app.name,
                    url,
                    raw_query,
                    cached_response is not None,
                    type(xcp).__name__,
                    xcp,
                )
            if cached_response is not None:
                return cached_response
            raise _http_exception(502, f"Squaremap request failed: {type(xcp).__name__}: {xcp}") from xcp
        if response.status_code >= 400:
            if response.status_code != 404 and allow_stale_on_error:
                cached_response = self._squaremap_cached_response(app, normalized_path)
                if cached_response is not None:
                    if should_log_failure:
                        log.warning(
                            "Squaremap returned HTTP %s: app=%s url=%s query=%s stale_cache=%s",
                            response.status_code,
                            app.name,
                            url,
                            raw_query,
                            True,
                        )
                    return cached_response
            if should_log_failure:
                log.warning(
                    "Squaremap returned HTTP %s: app=%s url=%s query=%s stale_cache=%s",
                    response.status_code,
                    app.name,
                    url,
                    raw_query,
                    False,
                )
            status_code = 404 if response.status_code == 404 else 502
            raise _http_exception(
                status_code,
                f"Squaremap returned HTTP {response.status_code} for {relative_path}.",
            )
        proxy_response = _SquaremapProxyResponse(
            content=response.content,
            media_type=response.headers.get("Content-Type"),
            headers=self._squaremap_passthrough_headers(response),
        )
        self._remember_squaremap_cache_entry(app, normalized_path, proxy_response)
        return proxy_response

    def _squaremap_local_proxy_response(self, app: App, relative_path: str) -> _SquaremapProxyResponse | None:
        root_path: Path | None = app.map_proxy_root_path
        if root_path is None:
            return None
        resolved_root = root_path.resolve()
        resolved_path = (resolved_root / relative_path).resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise _http_exception(400, f"Invalid Squaremap path: {relative_path}") from None
        if not resolved_path.is_file():
            return None
        media_type, _ = mimetypes.guess_type(resolved_path.name)
        return _SquaremapProxyResponse(
            content=resolved_path.read_bytes(),
            media_type=media_type,
            headers=(),
        )

    @staticmethod
    def _map_annotation_creator_name(app: App, *, actor_user_id: int, user: ModWebUser | None) -> str | None:
        fallback_username = None if user is None else user.username
        return config.Name_Cache().discord_fallback_name(
            actor_user_id,
            fallback_username,
            scope=app.scope,
            fallback_display_name=fallback_username,
        )

    def _squaremap_cached_response(self, app: App, relative_path: str) -> _SquaremapProxyResponse | None:
        cache_entry = self._squaremap_cache_entry(app, relative_path)
        if cache_entry is None:
            return None
        return _SquaremapProxyResponse(
            content=cache_entry.content.encode("utf-8"),
            media_type=cache_entry.media_type,
            headers=cache_entry.header_pairs,
            is_stale=True,
            cache_updated_at_unix_ms=cache_entry.updated_at_unix_ms,
        )

    def _squaremap_cache_entry(self, app: App, relative_path: str) -> MapJsonCacheEntry | None:
        try:
            return self._map_json_cache_store(app).load_entry(relative_path)
        except ValueError:
            log.exception("Map cache for %s is invalid at %s", app.friendly, app.map_cache_path)
            return None

    def _remember_squaremap_cache_entry(self, app: App, relative_path: str, proxy_response: _SquaremapProxyResponse) -> None:
        if not self._should_cache_squaremap_path(relative_path):
            return
        try:
            content_text = proxy_response.content.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("Skipping map cache write for %s because %s was not UTF-8 JSON.", app.friendly, relative_path)
            return
        try:
            self._map_json_cache_store(app).save_entry(
                relative_path=relative_path,
                content=content_text,
                media_type=proxy_response.media_type,
                headers=proxy_response.headers,
            )
        except ValueError:
            log.exception("Failed to update map cache for %s at %s", app.friendly, app.map_cache_path)

    @staticmethod
    def _should_cache_squaremap_path(relative_path: str) -> bool:
        normalized_path = relative_path.lstrip("/")
        if normalized_path == "tiles/settings.json":
            return True
        if not normalized_path.startswith("tiles/"):
            return False
        return normalized_path.endswith("/settings.json") or normalized_path.endswith("/markers.json")

    @staticmethod
    def _map_json_cache_store(app: App) -> AppMapJsonCacheStore:
        return AppMapJsonCacheStore(app.map_cache_path)

    def _squaremap_json_object(self, app: App, relative_path: str) -> Mapping[str, object]:
        proxy_response = self._squaremap_proxy_response(app, relative_path)
        return self._squaremap_json_object_from_response(proxy_response, relative_path)

    @staticmethod
    def _squaremap_json_object_from_response(
        proxy_response: _SquaremapProxyResponse,
        relative_path: str,
    ) -> Mapping[str, object]:
        try:
            payload = cast(object, json.loads(proxy_response.content.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as xcp:
            raise _http_exception(502, f"Squaremap returned invalid JSON for {relative_path}.") from xcp
        if not isinstance(payload, Mapping):
            raise _http_exception(502, f"Squaremap returned an invalid JSON object for {relative_path}.")
        return cast(Mapping[str, object], payload)

    @staticmethod
    def _squaremap_world_summaries(payload: Mapping[str, object]) -> tuple[MapWorldSummary, ...]:
        raw_worlds = payload.get("worlds")
        if isinstance(raw_worlds, (str, bytes)) or not isinstance(raw_worlds, Sequence):
            raise _http_exception(502, "Squaremap world list is invalid.")
        worlds: list[MapWorldSummary] = []
        for index, raw_world in enumerate(raw_worlds):
            if not isinstance(raw_world, Mapping):
                raise _http_exception(502, "Squaremap world entry is invalid.")
            name = NodeApiService._mapping_text(raw_world, ("name",))
            worlds.append(
                MapWorldSummary(
                    name=name,
                    display_name=NodeApiService._mapping_text(raw_world, ("display_name", "title", "name")),
                    world_type=NodeApiService._mapping_text(raw_world, ("type",), default="normal"),
                    order=NodeApiService._mapping_int(raw_world, "order", default=index),
                )
            )
        return tuple(sorted(worlds, key=lambda world: (world.order, world.display_name.casefold(), world.name.casefold())))

    @staticmethod
    def _mapping_text(
        payload: Mapping[str, object],
        keys: tuple[str, ...],
        *,
        default: str | None = None,
    ) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        if default is not None:
            return default
        joined_keys = ", ".join(keys)
        raise _http_exception(502, f"Squaremap payload is missing required text fields: {joined_keys}.")

    @staticmethod
    def _mapping_int(payload: Mapping[str, object], key: str, *, default: int) -> int:
        value = payload.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise _http_exception(502, f"Squaremap field {key!r} is invalid.")
        return value

    def subscribe_local_app_runtime(
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
        with self._local_runtime_watch_lock:
            state = self._local_runtime_watchers.get(app_key)
            if state is None:
                state = _NodeLocalAppRuntimeWatchState()
                self._local_runtime_watchers[app_key] = state
            state.callbacks[subscription_id] = _NodeLocalAppRuntimeSubscription(
                callback=callback,
                include_update_state=include_update_state,
            )
            if state.task is None or state.task.done():
                state.task = loop.create_task(self._watch_local_app_runtime(app_name, app_key))

        def _unsubscribe() -> None:
            self._unsubscribe_local_app_runtime(app_key, subscription_id)

        return _unsubscribe

    def subscribe_local_node_state(
        self,
        callback: Callable[[NodeStateStreamEvent], None],
        *,
        topics: frozenset[NodeStateTopic] = _ALL_NODE_STATE_TOPICS,
    ) -> Callable[[], None]:
        if not topics:
            raise ValueError("Local node state subscriptions require at least one topic.")
        subscription_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        with self._local_node_state_watch_lock:
            self._local_node_state_watcher.subscriptions[subscription_id] = _NodeLocalNodeStateSubscription(
                callback=callback,
                topics=topics,
            )
            task = self._local_node_state_watcher.task
            if task is None or task.done():
                self._local_node_state_watcher.task = loop.create_task(self._watch_local_node_state())

        def _unsubscribe() -> None:
            self._unsubscribe_local_node_state(subscription_id)

        return _unsubscribe

    def _unsubscribe_local_app_runtime(self, app_key: str, subscription_id: str) -> None:
        task_to_cancel: asyncio.Task[None] | None = None
        with self._local_runtime_watch_lock:
            state = self._local_runtime_watchers.get(app_key)
            if state is None:
                return
            state.callbacks.pop(subscription_id, None)
            if state.callbacks:
                return
            task_to_cancel = state.task
            self._local_runtime_watchers.pop(app_key, None)
        if task_to_cancel is not None and not task_to_cancel.done():
            task_to_cancel.cancel()

    def _unsubscribe_local_node_state(self, subscription_id: str) -> None:
        task_to_cancel: asyncio.Task[None] | None = None
        with self._local_node_state_watch_lock:
            self._local_node_state_watcher.subscriptions.pop(subscription_id, None)
            if self._local_node_state_watcher.subscriptions:
                return
            task_to_cancel = self._local_node_state_watcher.task
            self._local_node_state_watcher.task = None
        if task_to_cancel is not None and not task_to_cancel.done():
            task_to_cancel.cancel()

    async def _watch_local_app_runtime(self, app_name: str, app_key: str) -> None:
        current_task = asyncio.current_task()
        last_summary: NodeAppRuntimeSummary | None = None
        has_summary = False
        last_update_info: AppUpdateInfo | None = None
        last_update_status: AppUpdateStatus | None = None
        has_update_state = False
        try:
            while not self._shutting_down:
                with self._local_runtime_watch_lock:
                    state = self._local_runtime_watchers.get(app_key)
                    if state is None or not state.callbacks:
                        return
                    subscriptions = tuple(state.callbacks.values())
                callbacks = tuple(subscription.callback for subscription in subscriptions)
                include_update_state = any(subscription.include_update_state for subscription in subscriptions)
                try:
                    app = self._resolve_app(app_name)
                    summary = await self.build_live_app_runtime_summary(app)
                    update_info = app.update_info if include_update_state else None
                    update_status = app.update_status if include_update_state else None
                except asyncio.CancelledError:
                    raise
                except Exception as xcp:
                    log.warning(
                        "Node API local runtime watch failed: node=%s app=%s error=%s",
                        self.node_name,
                        app_name,
                        xcp,
                    )
                    await asyncio.sleep(_LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS)
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
                            log.exception(
                                "Node API local runtime subscriber callback failed: node=%s app=%s",
                                self.node_name,
                                app_name,
                            )
                    last_summary = summary
                    has_summary = True
                    if include_update_state:
                        last_update_info = update_info
                        last_update_status = update_status
                        has_update_state = True
                await asyncio.sleep(_LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            with self._local_runtime_watch_lock:
                state = self._local_runtime_watchers.get(app_key)
                if state is not None and state.task is current_task:
                    state.task = None
                if state is not None and not state.callbacks:
                    self._local_runtime_watchers.pop(app_key, None)

    async def _watch_local_node_state(self) -> None:
        current_task = asyncio.current_task()
        last_entries: tuple[NodeAppEntry, ...] | None = None
        last_system_summary: NodeSystemSummary | None = None
        has_state = False
        try:
            while not self._shutting_down:
                with self._local_node_state_watch_lock:
                    subscriptions = tuple(self._local_node_state_watcher.subscriptions.values())
                    if not subscriptions:
                        return
                try:
                    needs_apps = any(NodeStateTopic.APPS in subscription.topics for subscription in subscriptions)
                    needs_system = any(NodeStateTopic.SYSTEM in subscription.topics for subscription in subscriptions)
                    app_entries = await self.list_apps() if needs_apps else last_entries
                    system_summary = (
                        self._stream_system_summary(self.build_system_summary())
                        if needs_system
                        else last_system_summary
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as xcp:
                    log.warning("Node API local node state watch failed: node=%s error=%s", self.node_name, xcp)
                    await asyncio.sleep(_LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS)
                    continue

                apps_changed = app_entries is not None and ((not has_state) or app_entries != last_entries)
                system_changed = system_summary is not None and (
                    (not has_state) or system_summary != last_system_summary
                )
                for subscription in subscriptions:
                    include_apps = NodeStateTopic.APPS in subscription.topics
                    include_system = NodeStateTopic.SYSTEM in subscription.topics
                    event: NodeStateStreamEvent | None = None
                    if not subscription.initial_sent:
                        event = NodeStateStreamEvent.initial(
                            node_name=self.node_name,
                            app_entries=app_entries if include_apps else None,
                            system_summary=system_summary if include_system else None,
                        )
                    elif include_apps and apps_changed and include_system and system_changed:
                        if app_entries is None or system_summary is None:
                            raise RuntimeError("Combined node state update is incomplete.")
                        event = NodeStateStreamEvent.both(
                            node_name=self.node_name,
                            app_entries=app_entries,
                            system_summary=system_summary,
                        )
                    elif include_apps and apps_changed:
                        if app_entries is None:
                            raise RuntimeError("App node state update is incomplete.")
                        event = NodeStateStreamEvent.apps(node_name=self.node_name, app_entries=app_entries)
                    elif include_system and system_changed:
                        if system_summary is None:
                            raise RuntimeError("System node state update is incomplete.")
                        event = NodeStateStreamEvent.system(
                            node_name=self.node_name,
                            system_summary=system_summary,
                        )
                    if event is None:
                        continue
                    try:
                        subscription.callback(event)
                        subscription.initial_sent = True
                    except Exception:
                        log.exception("Node API local node state subscriber callback failed: node=%s", self.node_name)
                if needs_apps:
                    last_entries = app_entries
                if needs_system:
                    last_system_summary = system_summary
                has_state = True

                await asyncio.sleep(_LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            with self._local_node_state_watch_lock:
                if self._local_node_state_watcher.task is current_task:
                    self._local_node_state_watcher.task = None

    @staticmethod
    def _stream_system_summary(summary: NodeSystemSummary) -> NodeSystemSummary:
        def _minute_bucket(seconds: int | None) -> int | None:
            return None if seconds is None else (seconds // 60) * 60

        return replace(
            summary,
            bot_uptime_seconds=_minute_bucket(summary.bot_uptime_seconds),
            uptime_seconds=_minute_bucket(summary.uptime_seconds),
        )

    def _require_websocket_token_access(
        self,
        *,
        websocket: WebSocket,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeAccessGrant:
        secret = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise WebSocketException(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="Node token secret is not configured.",
            )
        token = self._request_token(websocket, access_token)
        try:
            grant = verify_node_token(
                secret=secret,
                token=token,
                node=self.node_name,
                app=app_name,
                required_scopes=scopes,
            )
        except NodeTokenError as xcp:
            log.warning(
                "Node API websocket access rejected: node=%s app=%s scopes=%s reason=%s",
                self.node_name,
                app_name,
                ",".join(scope.value for scope in scopes),
                xcp,
            )
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=str(xcp)) from xcp
        log.debug(
            "Node API websocket token access accepted: node=%s app=%s scopes=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
        )
        return grant

    @staticmethod
    def _websocket_exception_from_http(error: HTTPException) -> WebSocketException:
        if error.status_code in {400, 401, 403, 404, 409}:
            code = status.WS_1008_POLICY_VIOLATION
        else:
            code = status.WS_1011_INTERNAL_ERROR
        return WebSocketException(code=code, reason=str(error.detail))

    async def _serve_chat_stream(
        self,
        *,
        websocket: WebSocket,
        app: App,
        after_revision: int | None = None,
    ) -> None:
        await websocket.accept()
        update_queue: asyncio.Queue[NodeChatStreamEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _enqueue_update(event: NodeChatStreamEvent) -> None:
            def _queue_put() -> None:
                update_queue.put_nowait(event)

            try:
                loop.call_soon_threadsafe(_queue_put)
            except RuntimeError:
                return

        def _enqueue_chat_update(update: ChatRoomUpdate) -> None:
            _enqueue_update(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.CHAT_CHANGED,
                    room_id=app.name,
                    snapshot=(
                        self.build_chat_room_snapshot(app, limit=_NODE_CHAT_HISTORY_LIMIT)
                        if update.event is None
                        else None
                    ),
                    events=() if update.event is None else (update.event,),
                    revision=update.revision,
                )
            )

        room_subscription_id = ChatHub().subscribe(app.name, _enqueue_chat_update)
        unsubscribe_runtime = self.subscribe_local_app_runtime(
            app.name,
            lambda update: _enqueue_update(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.RUNTIME_CHANGED,
                    room_id=app.name,
                    app_stats=update.app_stats,
                )
            ),
        )

        async def _wait_for_disconnect() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return

        async def _send_stream_event(event: NodeChatStreamEvent) -> None:
            app_stats = event.app_stats
            if event.kind in {NodeChatStreamEventKind.INITIAL, NodeChatStreamEventKind.RUNTIME_CHANGED}:
                if app_stats is None:
                    app_stats = await self.build_live_app_runtime_summary(app)
            await websocket.send_json(
                NodeChatStreamEvent(
                    kind=event.kind,
                    room_id=event.room_id,
                    snapshot=event.snapshot,
                    app_stats=app_stats,
                    events=event.events,
                    revision=event.revision,
                ).to_mapping()
            )

        def _merge_stream_events(first: NodeChatStreamEvent, second: NodeChatStreamEvent) -> NodeChatStreamEvent:
            merged_events = second.events if second.snapshot is not None else first.events + second.events
            return NodeChatStreamEvent(
                kind=(
                    NodeChatStreamEventKind.RUNTIME_CHANGED
                    if NodeChatStreamEventKind.RUNTIME_CHANGED in {first.kind, second.kind}
                    else NodeChatStreamEventKind.CHAT_CHANGED
                ),
                room_id=app.name,
                snapshot=second.snapshot if second.snapshot is not None else first.snapshot,
                app_stats=second.app_stats if second.app_stats is not None else first.app_stats,
                events=merged_events,
                revision=max(first.revision, second.revision),
            )

        disconnect_task = asyncio.create_task(_wait_for_disconnect())
        try:
            initial_snapshot = self.build_chat_room_snapshot(app, limit=_NODE_CHAT_HISTORY_LIMIT)
            await _send_stream_event(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.INITIAL,
                    room_id=app.name,
                    snapshot=initial_snapshot if after_revision != initial_snapshot.revision else None,
                    revision=initial_snapshot.revision,
                )
            )
            while True:
                queue_task = asyncio.create_task(update_queue.get())
                done, _pending = await asyncio.wait(
                    {queue_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
                    return
                merged_event = queue_task.result()
                while not update_queue.empty():
                    merged_event = _merge_stream_events(merged_event, update_queue.get_nowait())
                await _send_stream_event(merged_event)
        except WebSocketDisconnect:
            return
        finally:
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
            ChatHub().unsubscribe(app.name, room_subscription_id)
            unsubscribe_runtime()
            await self._close_websocket_quietly(websocket)

    async def _serve_presence_stream(self, *, websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                sample_id: str | None = None
                payload_text = message.get("text")
                if isinstance(payload_text, str) and payload_text:
                    try:
                        payload = json.loads(payload_text)
                    except ValueError:
                        payload = None
                    if isinstance(payload, Mapping):
                        raw_sample_id = payload.get("sample_id")
                        if raw_sample_id is not None:
                            sample_id = str(raw_sample_id)
                await websocket.send_json({"type": "pong", "node": self.node_name, "sample_id": sample_id})
        except WebSocketDisconnect:
            return
        finally:
            await self._close_websocket_quietly(websocket)

    async def _serve_node_state_stream(self, *, websocket: WebSocket) -> None:
        await websocket.accept()
        update_queue: asyncio.Queue[NodeStateStreamEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        skip_initial = True

        def _enqueue_update(event: NodeStateStreamEvent) -> None:
            nonlocal skip_initial
            if skip_initial and event.is_initial:
                skip_initial = False
                return

            def _queue_put() -> None:
                update_queue.put_nowait(event)

            try:
                loop.call_soon_threadsafe(_queue_put)
            except RuntimeError:
                return

        unsubscribe = self.subscribe_local_node_state(_enqueue_update)

        async def _wait_for_disconnect() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return

        async def _send_stream_event(event: NodeStateStreamEvent) -> None:
            await websocket.send_json(event.to_mapping())

        disconnect_task = asyncio.create_task(_wait_for_disconnect())
        try:
            await _send_stream_event(
                NodeStateStreamEvent.initial(
                    node_name=self.node_name,
                    app_entries=await self.list_apps(),
                    system_summary=self._stream_system_summary(self.build_system_summary()),
                )
            )
            while True:
                queue_task = asyncio.create_task(update_queue.get())
                done, _pending = await asyncio.wait(
                    {queue_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
                    return
                merged_event = queue_task.result()
                while not update_queue.empty():
                    merged_event = self._merge_node_state_stream_events(merged_event, update_queue.get_nowait())
                await _send_stream_event(merged_event)
        except WebSocketDisconnect:
            return
        finally:
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
            unsubscribe()
            await self._close_websocket_quietly(websocket)

    async def _serve_app_state_stream(self, *, websocket: WebSocket, app: App) -> None:
        await websocket.accept()
        update_queue: asyncio.Queue[NodeAppStateStreamEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        skip_runtime_initial = True
        skip_node_initial = True

        def _enqueue_runtime_update(event: NodeAppStateStreamEvent) -> None:
            nonlocal skip_runtime_initial
            if skip_runtime_initial and event.is_initial:
                skip_runtime_initial = False
                return

            def _queue_put() -> None:
                update_queue.put_nowait(event)

            try:
                loop.call_soon_threadsafe(_queue_put)
            except RuntimeError:
                return

        def _enqueue_node_update(event: NodeStateStreamEvent) -> None:
            nonlocal skip_node_initial
            if skip_node_initial and event.is_initial:
                skip_node_initial = False
                return
            system_summary = event.system_summary
            if system_summary is None:
                return

            def _queue_put() -> None:
                update_queue.put_nowait(
                    NodeAppStateStreamEvent.system(
                        app_name=app.name,
                        system_summary=system_summary,
                    )
                )

            try:
                loop.call_soon_threadsafe(_queue_put)
            except RuntimeError:
                return

        unsubscribe_runtime = self.subscribe_local_app_runtime(
            app.name,
            _enqueue_runtime_update,
            include_update_state=True,
        )
        unsubscribe_node = self.subscribe_local_node_state(
            _enqueue_node_update,
            topics=frozenset({NodeStateTopic.SYSTEM}),
        )

        async def _wait_for_disconnect() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return

        async def _send_stream_event(event: NodeAppStateStreamEvent) -> None:
            await websocket.send_json(event.to_mapping())

        disconnect_task = asyncio.create_task(_wait_for_disconnect())
        try:
            await _send_stream_event(
                NodeAppStateStreamEvent.initial(
                    app_name=app.name,
                    app_stats=await self.build_live_app_runtime_summary(app),
                    system_summary=self._stream_system_summary(self.build_system_summary()),
                    update_info=app.update_info,
                    update_status=app.update_status,
                )
            )
            while True:
                queue_task = asyncio.create_task(update_queue.get())
                done, _pending = await asyncio.wait(
                    {queue_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
                    return
                merged_event = queue_task.result()
                while not update_queue.empty():
                    merged_event = self._merge_app_state_stream_events(merged_event, update_queue.get_nowait())
                await _send_stream_event(merged_event)
        except WebSocketDisconnect:
            return
        finally:
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
            unsubscribe_runtime()
            unsubscribe_node()
            await self._close_websocket_quietly(websocket)

    async def _serve_console_stdout_stream(
        self,
        *,
        websocket: WebSocket,
        app: App,
        max_lines: int,
    ) -> None:
        await websocket.accept()

        async def _wait_for_disconnect() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return

        async def _send_event(event: NodeConsoleStdoutStreamEvent) -> None:
            await websocket.send_json(event.to_mapping())

        disconnect_task = asyncio.create_task(_wait_for_disconnect())
        previous_snapshot: NodeConsoleStdoutSnapshot | None = None
        try:
            initial_snapshot = self.build_console_stdout_snapshot(app=app, max_lines=max_lines)
            await _send_event(
                NodeConsoleStdoutStreamEvent(
                    kind=NodeConsoleStdoutStreamEventKind.INITIAL,
                    app_name=app.name,
                    snapshot=initial_snapshot,
                    truncated=initial_snapshot.truncated,
                    running=initial_snapshot.running,
                )
            )
            previous_snapshot = initial_snapshot
            while True:
                interval_task = asyncio.create_task(asyncio.sleep(_LOCAL_CONSOLE_STDOUT_STREAM_INTERVAL_SECONDS))
                done, _pending = await asyncio.wait(
                    {interval_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    interval_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await interval_task
                    return
                next_snapshot = self.build_console_stdout_snapshot(app=app, max_lines=max_lines)
                if next_snapshot != previous_snapshot:
                    appended_lines = self._console_stdout_appended_lines(previous_snapshot, next_snapshot)
                    if appended_lines is None:
                        event = NodeConsoleStdoutStreamEvent(
                            kind=NodeConsoleStdoutStreamEventKind.RESET,
                            app_name=app.name,
                            snapshot=next_snapshot,
                            truncated=next_snapshot.truncated,
                            running=next_snapshot.running,
                        )
                    else:
                        event = NodeConsoleStdoutStreamEvent(
                            kind=NodeConsoleStdoutStreamEventKind.APPEND,
                            app_name=app.name,
                            appended_lines=appended_lines,
                            truncated=next_snapshot.truncated,
                            running=next_snapshot.running,
                        )
                    await _send_event(event)
                    previous_snapshot = next_snapshot
        except WebSocketDisconnect:
            return
        finally:
            disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
            await self._close_websocket_quietly(websocket)

    @staticmethod
    def _console_stdout_appended_lines(
        previous: NodeConsoleStdoutSnapshot,
        updated: NodeConsoleStdoutSnapshot,
    ) -> tuple[str, ...] | None:
        if previous.app_name.casefold() != updated.app_name.casefold():
            raise ValueError("Cannot compare console stdout snapshots for different apps.")
        if not previous.lines:
            return updated.lines
        max_overlap = min(len(previous.lines), len(updated.lines))
        for overlap in range(max_overlap, 0, -1):
            if previous.lines[-overlap:] == updated.lines[:overlap]:
                return updated.lines[overlap:]
        return None

    @staticmethod
    async def _close_websocket_quietly(websocket: WebSocket) -> None:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()

    @staticmethod
    def _merge_node_state_stream_events(
        first: NodeStateStreamEvent,
        second: NodeStateStreamEvent,
    ) -> NodeStateStreamEvent:
        if first.node_name.casefold() != second.node_name.casefold():
            raise ValueError("Cannot merge node state stream events for different nodes.")
        return NodeStateStreamEvent(
            node_name=first.node_name,
            is_initial=first.is_initial or second.is_initial,
            apps_changed=first.apps_changed or second.apps_changed,
            system_changed=first.system_changed or second.system_changed,
            app_entries=second.app_entries if second.app_entries is not None else first.app_entries,
            system_summary=second.system_summary if second.system_summary is not None else first.system_summary,
        )

    @staticmethod
    def _merge_app_state_stream_events(
        first: NodeAppStateStreamEvent,
        second: NodeAppStateStreamEvent,
    ) -> NodeAppStateStreamEvent:
        if first.app_name.casefold() != second.app_name.casefold():
            raise ValueError("Cannot merge app state stream events for different apps.")
        return NodeAppStateStreamEvent(
            app_name=first.app_name,
            is_initial=first.is_initial or second.is_initial,
            runtime_changed=first.runtime_changed or second.runtime_changed,
            system_changed=first.system_changed or second.system_changed,
            update_changed=first.update_changed or second.update_changed,
            app_stats=second.app_stats if second.app_stats is not None else first.app_stats,
            system_summary=second.system_summary if second.system_summary is not None else first.system_summary,
            update_info=second.update_info
            if second.update_info is not None or second.update_changed
            else first.update_info,
            update_status=(
                second.update_status
                if second.update_status is not None or second.update_changed
                else first.update_status
            ),
        )

    async def _app_player_snapshot(self, app: App) -> _NodeAppPlayerSnapshot | None:
        if not app.check_running():
            return None
        try:
            count_snapshot: tuple[int, int] | None = await asyncio.wait_for(
                app.player_count(), timeout=_APP_PLAYER_COUNT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            log.debug("Node API player count timed out: node=%s app=%s", self.node_name, app.name)
        except Exception as xcp:
            log.warning("Node API player count failed: node=%s app=%s error=%s", self.node_name, app.name, xcp)
        else:
            if count_snapshot is None:
                return None
            player_count, player_capacity = count_snapshot
            connected_player_names: tuple[str, ...] = ()
            raw_connected_player_names_reader = getattr(app, "connected_player_names", None)
            if callable(raw_connected_player_names_reader):
                connected_player_names_reader: Callable[[], tuple[str, ...]] = cast(
                    Callable[[], tuple[str, ...]],
                    raw_connected_player_names_reader,
                )
                try:
                    connected_player_names = connected_player_names_reader()
                except Exception as xcp:
                    log.warning(
                        "Node API connected player names failed: node=%s app=%s error=%s",
                        self.node_name,
                        app.name,
                        xcp,
                    )
            return _NodeAppPlayerSnapshot(
                player_count=player_count,
                player_capacity=player_capacity,
                connected_player_names=connected_player_names,
            )
        return None

    async def mutate_app(
        self,
        *,
        app: App,
        action: NodeAppMutationAction,
        actor_user_id: int,
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
        await self._require_acl().perm_check(actor_user_id, required_app_mutation_level(action))
        manager: App_Manager = self._require_manager()
        if action is NodeAppMutationAction.START:
            blocker = manager.start_blocker(app)
            if blocker is not None:
                raise _http_exception(409, blocker.message)
            self._track_app_mutation_task(
                app_name=app.name,
                action=action,
                state=NodeAppTransitionState.STARTING,
                task=asyncio.create_task(self._run_app_mutation_task(manager=manager, app=app, action=action)),
            )
            message = f"Start requested for {app.friendly}."
        elif action is NodeAppMutationAction.STOP:
            self._track_app_mutation_task(
                app_name=app.name,
                action=action,
                state=NodeAppTransitionState.STOPPING,
                task=asyncio.create_task(self._run_app_mutation_task(manager=manager, app=app, action=action)),
            )
            message = f"Stop requested for {app.friendly}."
        elif action is NodeAppMutationAction.KILL:
            self._track_app_mutation_task(
                app_name=app.name,
                action=action,
                state=NodeAppTransitionState.STOPPING,
                task=asyncio.create_task(self._run_app_mutation_task(manager=manager, app=app, action=action)),
            )
            message = f"Kill requested for {app.friendly}."
        elif action is NodeAppMutationAction.ENABLE:
            manager.toggle(app.name, True)
            message = f"Enabled {app.friendly}."
        elif action is NodeAppMutationAction.DISABLE:
            manager.toggle(app.name, False)
            message: str = f"Disabled {app.friendly}."
        elif action is NodeAppMutationAction.RENAME:
            previous_friendly_name: str = app.friendly
            if friendly_name is None or not friendly_name.strip():
                raise ValueError("Friendly name must not be empty.")
            next_friendly_name = manager.set_app_friendly_name(app, friendly_name)
            if previous_friendly_name == next_friendly_name:
                message = f"Friendly name already set to {next_friendly_name}."
            else:
                message = f"Renamed {previous_friendly_name} to {next_friendly_name}."
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
            log.info(
                "Node API selecting update branch: node=%s app=%s branch=%s actor=%s",
                self.node_name,
                app.name,
                update_branch_id,
                actor_user_id,
            )
            update_info = app.updater.select_branch(update_branch_id)
            message = f"Selected update branch {update_info.selected_branch_label} for {app.friendly}."
        elif action is NodeAppMutationAction.UPDATE:
            if app.updater is None:
                raise ValueError(f"{app.friendly} does not support updates.")
            log.info(
                "Node API starting update: node=%s app=%s actor=%s branch=%s",
                self.node_name,
                app.name,
                actor_user_id,
                app.update_info.selected_branch_id if app.update_info is not None else None,
            )
            result = await app.updater.start_selected_update()
            message = result.message
        elif action is NodeAppMutationAction.VERIFY:
            if app.updater is None:
                raise ValueError(f"{app.friendly} does not support verification.")
            log.info(
                "Node API starting verify: node=%s app=%s actor=%s branch=%s",
                self.node_name,
                app.name,
                actor_user_id,
                app.update_info.selected_branch_id if app.update_info is not None else None,
            )
            result = await app.updater.start_selected_verify()
            message = result.message
        else:
            raise ValueError(f"Unsupported app mutation action: {action}")

        self._invalidate_state_caches(app_name=app.name)
        app_stats: NodeAppRuntimeSummary
        if action in {NodeAppMutationAction.START, NodeAppMutationAction.STOP, NodeAppMutationAction.KILL}:
            app_stats = await self.build_live_app_runtime_summary(app)
        else:
            app_stats = await self.build_app_runtime_summary(app)
        traffic_log.info(
            "Node API app mutated: node=%s app=%s action=%s actor=%s",
            self.node_name,
            app.name,
            action.value,
            actor_user_id,
        )
        return NodeAppMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            action=action,
            message=message,
            app_stats=app_stats,
        )

    def read_node_capacity(self) -> config.NodeCapacityProfile:
        manager = self._require_manager()
        return manager.node_capacity()

    def read_node_font_sources(self) -> config.NodeFontSourceSettings:
        manager = self._require_manager()
        return manager.node_font_sources()

    def read_node_disk_settings(self) -> NodeDiskManagementState:
        stats = Stats_System()
        stats.refresh_disk_inventory()
        activity_mountpoints = {disk.mountpoint_text for disk in stats.activity_disks}
        primary_disk = stats.primary_disk
        secondary_disk = stats.secondary_disk
        bot_disk = stats.bot_disk
        return NodeDiskManagementState(
            node=self.node_name,
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
        manager = self._require_manager()
        return manager.discord_settings()

    async def _portal_process_restart_kind(self, request: Request) -> RestartKind:
        try:
            payload: object = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, Mapping):
            raise _http_exception(400, "Portal restart payload is invalid.")
        raw_kind = payload.get("restart_kind", RestartKind.MANUAL_BOT.value)
        if not isinstance(raw_kind, str):
            raise _http_exception(400, "Portal restart kind is invalid.")
        try:
            restart_kind = RestartKind(raw_kind)
        except ValueError as xcp:
            raise _http_exception(400, "Portal restart kind is invalid.") from xcp
        if restart_kind not in {RestartKind.SCHEDULED_BOT, RestartKind.MANUAL_BOT}:
            raise _http_exception(400, "Portal restart kind must be scheduled_bot or manual_bot.")
        return restart_kind

    async def schedule_system_action(
        self,
        *,
        action: NodeSystemAction,
        auto_restart_running_apps: bool,
        silent: bool,
        actor_user_id: int,
    ) -> NodeSystemActionResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        if (
            action is NodeSystemAction.RESTART_PORTAL
            and config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.YUKI
        ):
            raise _http_exception(400, "Portal restart is only available on the Yuki node.")
        handler = self._system_action_handler
        if handler is None:
            raise _http_exception(503, "Node system actions are unavailable on this node.")
        with self._system_action_lock:
            if self._pending_system_action is not None:
                raise _http_exception(
                    409,
                    f"Node system action {self._pending_system_action.value!r} is already pending.",
                )
            self._pending_system_action = action

        audit_log(
            "node.system_action.scheduled",
            actor_user_id=actor_user_id,
            node=self.node_name,
            action=action.value,
        )

        def _dispatch() -> None:
            try:
                handler(action, auto_restart_running_apps, silent)
            except Exception:
                with self._system_action_lock:
                    self._pending_system_action = None
                log.exception("Node system action dispatch failed: node=%s action=%s", self.node_name, action.value)
                return
            if action is NodeSystemAction.RESTART_PORTAL:
                with self._system_action_lock:
                    self._pending_system_action = None

        asyncio.get_running_loop().call_later(_NODE_RESTART_DELAY_SECONDS, _dispatch)
        action_label = _NODE_SYSTEM_ACTION_LABELS[action]
        return NodeSystemActionResult(
            node=self.node_name,
            action=action,
            message=f"Scheduled {action_label} for {self.node_name}.",
        )

    def read_restart_state(self) -> NodeRestartState:
        process_start_timestamp = int(psutil.Process().create_time())
        process_record = read_process_restart_record(default_timestamp=process_start_timestamp)
        voice_record = read_voice_restart_record()
        return NodeRestartState(
            node=self.node_name,
            process=NodeRestartRecord(timestamp=process_record.timestamp, kind=process_record.kind),
            voice=(
                None
                if voice_record is None
                else NodeRestartRecord(timestamp=voice_record.timestamp, kind=voice_record.kind)
            ),
        )

    def read_restart_schedules(self) -> NodeRestartScheduleState:
        maintenance = self._maintenance_service
        if maintenance is None:
            raise _http_exception(503, "Restart scheduling is unavailable on this node.")
        maintenance.reload()
        return NodeRestartScheduleState(
            node=self.node_name,
            schedules=tuple(
                NodeRestartScheduleEntry(
                    target=target,
                    enabled=(schedule := maintenance.schedule_for(target)).enabled,
                    interval_minutes=schedule.interval_minutes,
                    anchor_timestamp=schedule.anchor_timestamp,
                    last_triggered_timestamp=schedule.last_triggered_timestamp,
                    next_restart_timestamp=(
                        int(next_restart.timestamp())
                        if (next_restart := maintenance.next_restart_at(target)) is not None
                        else None
                    ),
                    skipped_through_timestamp=schedule.skipped_through_timestamp,
                )
                for target in self._maintenance_restart_targets
            ),
        )

    async def update_restart_schedule(
        self,
        *,
        target: RestartTarget,
        interval_minutes: int | None,
        anchor_timestamp: int | None,
        actor_user_id: int,
    ) -> NodeRestartScheduleState:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        maintenance = self._maintenance_service
        if maintenance is None:
            raise _http_exception(503, "Restart scheduling is unavailable on this node.")
        if target not in self._maintenance_restart_targets:
            raise _http_exception(400, f"Restart target {target.value!r} is unavailable on this node.")
        if interval_minutes is not None and anchor_timestamp is None:
            raise _http_exception(400, "Enabled restart schedules require an anchor timestamp.")
        try:
            updated_schedules = maintenance.update_restart_intervals(
                {target: interval_minutes},
                anchor_timestamp=anchor_timestamp,
            )
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        updated_schedule = updated_schedules[target]
        audit_log(
            "node.restart_schedule.updated",
            actor_user_id=actor_user_id,
            node=self.node_name,
            target=target.value,
            interval_minutes=interval_minutes,
            anchor_timestamp=anchor_timestamp,
            automatically_skipped_through_timestamp=updated_schedule.skipped_through_timestamp,
        )
        return self.read_restart_schedules()

    async def skip_restart_schedule(
        self,
        *,
        target: RestartTarget,
        actor_user_id: int,
    ) -> NodeRestartScheduleState:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        maintenance = self._maintenance_service
        if maintenance is None:
            raise _http_exception(503, "Restart scheduling is unavailable on this node.")
        if target not in self._maintenance_restart_targets:
            raise _http_exception(400, f"Restart target {target.value!r} is unavailable on this node.")
        try:
            schedule = maintenance.skip_next_restart(target)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        audit_log(
            "node.restart_schedule.skipped",
            actor_user_id=actor_user_id,
            node=self.node_name,
            target=target.value,
            skipped_through_timestamp=schedule.skipped_through_timestamp,
        )
        return self.read_restart_schedules()

    async def mutate_node_capacity(
        self,
        *,
        capacity: config.NodeCapacityProfile,
        actor_user_id: int,
    ) -> NodeCapacityMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.root)
        manager = self._require_manager()
        updated_capacity = manager.set_node_capacity(capacity)
        self._invalidate_state_caches()
        return NodeCapacityMutationResult(
            node=self.node_name,
            message=f"Updated node capacity for {self.node_name}.",
            capacity=updated_capacity,
        )

    async def mutate_node_disk_settings(
        self,
        *,
        preferences: config.PersistedDiskPreferences,
        actor_user_id: int,
    ) -> NodeDiskSettingsMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.root)
        updated_preferences = Stats_System().set_disk_preferences(preferences)
        self._invalidate_state_caches()
        audit_log(
            "node.disk_settings.updated",
            actor_user_id=actor_user_id,
            node=self.node_name,
            activity_mounts=updated_preferences.activity_mounts,
            primary_mount=updated_preferences.primary_mount,
            secondary_mount=updated_preferences.secondary_mount,
            label_mountpoints=sorted(updated_preferences.labels),
        )
        return NodeDiskSettingsMutationResult(
            node=self.node_name,
            message=f"Updated node disk settings for {self.node_name}.",
            settings=self.read_node_disk_settings(),
        )

    async def mutate_node_font_sources(
        self,
        *,
        settings: config.NodeFontSourceSettings,
        actor_user_id: int,
    ) -> NodeFontSourceSettingsMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.sudo)
        manager = self._require_manager()
        updated_settings = manager.set_node_font_sources(settings)
        font_assets.schedule_startup_refresh(google_font_urls=updated_settings.google_font_urls)
        return NodeFontSourceSettingsMutationResult(
            node=self.node_name,
            message=f"Updated node font sources for {self.node_name}.",
            settings=updated_settings,
        )

    async def mutate_discord_settings(
        self,
        *,
        settings: config.DiscordSettings,
        actor_user_id: int,
    ) -> NodeDiscordSettingsMutationResult:
        manager = self._require_manager()
        current_settings = manager.discord_settings()
        required_level = (
            Power_Level.root
            if current_settings.activity.refresh_interval_seconds != settings.activity.refresh_interval_seconds
            else Power_Level.sudo
        )
        await self._require_acl().perm_check(actor_user_id, required_level)
        updated_settings = manager.set_discord_settings(settings)
        if manager.activity_manager is not None:
            await manager.activity_manager.refresh()
        return NodeDiscordSettingsMutationResult(
            node=self.node_name,
            message=f"Updated Discord settings for {self.node_name}.",
            settings=updated_settings,
        )

    async def build_mod_download_response(self, *, app: App, request: NodeDownloadRequest) -> FileResponse:
        await app.has_mod_manager.reload_mods()

        capabilities = app.mod_capabilities
        pack_purpose = request.resolved_pack_purpose
        if pack_purpose is PackPurpose.CLIENT:
            if not capabilities.supports_client_pack:
                raise _http_exception(400, f"{app.friendly} does not support client pack generation.")
            if app.cfg.client_pack_content_dirty and not request.publish_client_pack:
                raise _http_exception(409, "Client pack configuration has unpublished changes.")
            if request.publish_client_pack and not (request.publish_changelog or "").strip():
                raise _http_exception(
                    400,
                    "Client pack publication requires a changelog.",
                )
            if request.pack_format is not PackFormat.GENERIC_ZIP and not capabilities.supports_launcher_formats:
                raise _http_exception(400, f"{app.friendly} does not support launcher pack formats.")
        elif pack_purpose is None and not capabilities.supports_raw_download:
            raise _http_exception(400, f"{app.friendly} does not support raw mod downloads.")

        if request.mod_name is not None:
            mod: Mod = app.has_mod_manager.get(request.mod_name)
            try:
                require_downloadable(mod)
            except NonDownloadableModError as xcp:
                log.warning("Node API blocked single mod download: app=%s mod=%s reason=%s", app.name, mod.name, xcp)
                raise _http_exception(403, str(xcp)) from xcp
            download: NodeDownloadFile = await self._single_mod_download_file(app=app, mod=mod)
            traffic_log.info(
                "Node API sending single mod: app=%s mod=%s path=%s archive=%s",
                app.name,
                mod.name,
                download.path,
                download.is_archive,
            )
            return FileResponse(path=download.path, filename=download.filename)

        selected_mod_names: tuple[str, ...] | None = (
            request.mod_names if request.selected_only or request.mod_names else None
        )
        if request.excluded_only:
            if not request.selected_only:
                raise _http_exception(400, "Excluded mod selection requires selected-only mode.")
            if pack_purpose is not None:
                raise _http_exception(400, "Excluded mod selection is only supported for raw mod downloads.")
            excluded_names = frozenset(request.mod_names)
            for excluded_name in excluded_names:
                try:
                    require_downloadable(app.has_mod_manager.get(excluded_name))
                except (KeyError, ModuleNotFoundError, NonDownloadableModError) as xcp:
                    raise _http_exception(400, f"Invalid excluded mod selection: {excluded_name}") from xcp
            selected_mod_names = tuple(
                mod.name
                for mod in app.has_mod_manager.list_mods()
                if mod.downloadable and mod.name not in excluded_names
            )
        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...]
        try:
            if pack_purpose is PackPurpose.CLIENT:
                entries = self._client_pack_entries(
                    app=app,
                    selection=ClientPackSelection(
                        selected_mod_names=frozenset(selected_mod_names or ()),
                        supplied=request.selected_only or bool(request.mod_names),
                    ),
                    include_kubejs_scripts=request.include_kubejs_scripts,
                    include_servers_dat=request.include_servers_dat,
                    include_options_txt=request.include_options_txt,
                )
            elif pack_purpose is PackPurpose.SERVER:
                entries = build_server_pack_entries(app.has_mod_manager)
            elif pack_purpose is PackPurpose.ADMIN:
                entries = build_admin_pack_entries(app.has_mod_manager)
            else:
                entries = build_mod_download_entries(
                    app.has_mod_manager,
                    selected_mod_names,
                    default_enabled_only=request.enabled_only,
                )
        except NonDownloadableModError as xcp:
            raise _http_exception(403, str(xcp)) from xcp
        except ClientPackValidationError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except ModuleNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        if not entries:
            detail: str = self._empty_archive_detail(request)
            log.warning(
                "Node API archive request had no paths: app=%s enabled_only=%s selected=%s",
                app.name,
                request.enabled_only,
                len(selected_mod_names) if selected_mod_names is not None else 0,
            )
            raise _http_exception(404, detail)

        generated_pack_version: str | None = None
        if pack_purpose is PackPurpose.CLIENT:
            published_entries = self._client_pack_entries(
                app=app,
                selection=ClientPackSelection(),
                include_kubejs_scripts=True,
            )
            current_hash = await self._client_pack_content_hash(app=app, entries=published_entries)
            if app.cfg.client_pack_current_hash != current_hash:
                app.record_client_pack_content_hash(current_hash)
            if request.publish_client_pack:
                generated_pack_version = app.publish_client_pack(
                    current_hash,
                    changelog=request.publish_changelog or "",
                    mods=self._default_client_pack_mod_snapshots(app),
                )
                self._invalidate_state_caches(app_name=app.name)
            elif app.cfg.client_pack_published_hash != current_hash:
                raise _http_exception(409, "Client pack content has changed; publish or regenerate it before download.")
            elif app.cfg.client_pack_published_version is None:
                raise _http_exception(409, "Client pack version metadata is missing; publish the client pack again.")
            else:
                generated_pack_version = app.cfg.client_pack_published_version

        archive_name = self._archive_name(
            app=app,
            entries=entries,
            request=request,
            client_pack_version=generated_pack_version,
        )
        if pack_purpose is not None:
            version = app.cfg.version
            if version is None and request.pack_format is not PackFormat.GENERIC_ZIP:
                raise _http_exception(400, "Minecraft version metadata is required for launcher pack exports.")
            if version is None:
                archive_path = await compress_mod_archive_entries(entries, archive_name)
            else:
                client_pack_metadata = (
                    self._client_pack_metadata(app)
                    if pack_purpose is PackPurpose.CLIENT
                    else None
                )
                try:
                    archive_path = await export_minecraft_pack(
                        entries,
                        MinecraftPackSpec(
                            purpose=pack_purpose,
                            format=request.pack_format,
                            name=(client_pack_metadata.name if client_pack_metadata is not None else app.friendly),
                            version_id=generated_pack_version or version.main,
                            minecraft_version=version.main,
                            loader=version.loader,
                            loader_version=version.framework,
                            author=getattr(app.cfg, "pack_author", "Yukibot"),
                            summary=(
                                client_pack_metadata.description or None
                                if client_pack_metadata is not None
                                else app.cfg.notes
                            ),
                        ),
                        archive_name,
                    )
                except MinecraftPackExportError as xcp:
                    raise _http_exception(400, str(xcp)) from xcp
        else:
            archive_path = await compress_mod_archive_entries(entries, archive_name)
        traffic_log.info(
            "Node API sending mod archive: app=%s enabled_only=%s selected=%s entries=%s archive=%s",
            app.name,
            request.enabled_only,
            len(selected_mod_names) if selected_mod_names is not None else 0,
            len(entries),
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_path.name)

    async def _client_pack_content_hash(
        self,
        *,
        app: App,
        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...],
    ) -> str:
        version = app.cfg.version
        metadata = self._client_pack_metadata(app)
        hash_context_payload: dict[str, object] = {
            "app_version": None if version is None else version.model_dump(mode="json", exclude_none=True),
            "name": metadata.name if metadata is not None else app.friendly,
            "summary": metadata.description if metadata is not None else app.cfg.notes,
        }
        if metadata is not None and app.cfg.client_pack_metadata is not None:
            hash_context_payload["filename_template"] = metadata.filename_template
            hash_context_payload["include_servers_dat"] = metadata.include_servers_dat
            hash_context_payload["include_options_txt"] = metadata.include_options_txt
        hash_context = json.dumps(hash_context_payload, sort_keys=True)
        return await asyncio.to_thread(client_pack_content_hash, entries, format_name=hash_context)

    @staticmethod
    def _client_overrides_dir_for_pack(app: App) -> Path | None:
        if not app.mod_capabilities.include_client_overrides:
            return None
        configured_dir = app.cfg.client_overrides_dir
        if configured_dir is not None:
            resolved_configured_dir = configured_dir.resolve()
            if resolved_configured_dir.is_dir():
                return resolved_configured_dir

        fallback_dir = (app.directory / ".yukibot" / "client-overrides").resolve()
        fallback_existed = fallback_dir.is_dir()
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
        except OSError as xcp:
            log.exception(
                "Failed to create client overrides directory: app=%s path=%s",
                app.name,
                fallback_dir,
            )
            raise ClientPackValidationError(
                f"Client overrides directory could not be created: {fallback_dir}"
            ) from xcp
        if not fallback_existed:
            log.warning(
                "Created fallback client overrides directory: app=%s configured_path=%s path=%s",
                app.name,
                configured_dir,
                fallback_dir,
            )
        return fallback_dir

    @staticmethod
    def _client_pack_kubejs_scripts(app: App) -> tuple[ClientPackKubeJsScript, ...]:
        if not isinstance(app, Minecraft):
            return ()
        return discover_client_pack_kubejs_scripts(
            app.directory,
            excluded_paths=frozenset(app.cfg.client_pack_excluded_kubejs_scripts),
        )

    @staticmethod
    def _client_pack_metadata(app: App) -> ClientPackMetadataConfig | None:
        if not isinstance(app, Minecraft):
            return None
        configured_metadata = app.cfg.client_pack_metadata
        if configured_metadata is not None:
            return configured_metadata
        return ClientPackMetadataConfig(
            name=app.friendly,
            description=app.cfg.notes or "",
        )

    @staticmethod
    def _client_pack_preview_overrides_dir(app: App) -> Path | None:
        if not app.mod_capabilities.include_client_overrides:
            return None
        configured_dir = app.cfg.client_overrides_dir
        if configured_dir is not None and configured_dir.is_dir():
            return configured_dir.resolve()
        fallback_dir = app.directory / ".yukibot" / "client-overrides"
        if fallback_dir.is_dir():
            return fallback_dir.resolve()
        return None

    @staticmethod
    def _client_pack_options_txt_preview(app: App) -> str:
        overrides_dir = NodeApiService._client_pack_preview_overrides_dir(app)
        if overrides_dir is None:
            return "No client overrides directory exists, so overrides/options.txt will not be added."
        options_path = overrides_dir / "options.txt"
        if not options_path.is_file():
            return f"No options.txt file exists at {options_path}."
        try:
            return options_path.read_text(encoding="utf-8", errors="replace")
        except OSError as xcp:
            return f"Could not read {options_path}: {xcp}"

    def _client_pack_file_previews(self, app: App) -> tuple[ClientPackFilePreview, ...]:
        if not isinstance(app, Minecraft):
            return ()
        server_address = app.cfg.join_direct_ip_address or app.cfg.join_address
        if server_address is None:
            servers_dat_preview = "No join address is configured, so overrides/servers.dat will not be generated."
        else:
            server_name = self._minecraft_servers_dat_server_name(self._client_pack_node_label())
            servers_dat_preview = (
                "Minecraft servers.dat entry\n"
                f"name={server_name}\n"
                f"ip={server_address}\n"
            )
        return (
            ClientPackFilePreview(
                path="overrides/servers.dat",
                display_name="servers.dat",
                content_text=servers_dat_preview,
            ),
            ClientPackFilePreview(
                path="overrides/options.txt",
                display_name="options.txt",
                content_text=self._client_pack_options_txt_preview(app),
            ),
        )

    def _default_client_pack_mod_snapshots(self, app: App) -> tuple[ClientPackModSnapshot, ...]:
        entries = self._client_pack_entries(
            selection=ClientPackSelection(),
            app=app,
            include_kubejs_scripts=False,
            include_servers_dat=False,
            include_options_txt=False,
        )
        snapshots: list[ClientPackModSnapshot] = []
        for entry in entries:
            if not isinstance(entry, ModArchiveEntry):
                continue
            mod = app.has_mod_manager.get(entry.mod_name)
            snapshots.append(
                ClientPackModSnapshot(
                    name=mod.name,
                    friendly=mod.friendly,
                    version=mod.version,
                )
            )
        return tuple(sorted(snapshots, key=lambda mod: mod.friendly.casefold()))

    @staticmethod
    def _client_pack_mod_snapshot_label(snapshot: ClientPackModSnapshot) -> str:
        if snapshot.version is None:
            return snapshot.friendly
        return f"{snapshot.friendly} ({snapshot.version})"

    @staticmethod
    def _unique_client_pack_mods_by_friendly(
        snapshots: Sequence[ClientPackModSnapshot],
    ) -> dict[str, ClientPackModSnapshot]:
        friendly_counts: dict[str, int] = {}
        for snapshot in snapshots:
            key = snapshot.friendly.casefold()
            friendly_counts[key] = friendly_counts.get(key, 0) + 1
        return {
            snapshot.friendly.casefold(): snapshot
            for snapshot in snapshots
            if friendly_counts[snapshot.friendly.casefold()] == 1
        }

    @classmethod
    def _client_pack_mod_update_label(cls, before: ClientPackModSnapshot, after: ClientPackModSnapshot) -> str:
        changes: list[str] = []
        if before.version != after.version:
            changes.append(f"{before.version or 'unknown'} -> {after.version or 'unknown'}")
        if before.name != after.name:
            changes.append(f"file {before.name} -> {after.name}")
        if before.friendly != after.friendly:
            changes.append(f"name {before.friendly} -> {after.friendly}")
        if not changes:
            return cls._client_pack_mod_snapshot_label(after)
        return f"{after.friendly}: {'; '.join(changes)}"

    @classmethod
    def _client_pack_automated_changelog_text(
        cls,
        *,
        current: tuple[ClientPackModSnapshot, ...],
        published: tuple[ClientPackModSnapshot, ...],
        has_published_pack: bool,
    ) -> str:
        if not current and not published:
            return "No automated client-pack changes detected."

        current_by_name = {snapshot.name.casefold(): snapshot for snapshot in current}
        published_by_name = {snapshot.name.casefold(): snapshot for snapshot in published}
        matched_pairs: list[tuple[ClientPackModSnapshot, ClientPackModSnapshot]] = []
        current_unmatched_keys = set(current_by_name)
        published_unmatched_keys = set(published_by_name)

        for key in sorted(current_unmatched_keys & published_unmatched_keys):
            matched_pairs.append((published_by_name[key], current_by_name[key]))
            current_unmatched_keys.remove(key)
            published_unmatched_keys.remove(key)

        current_unmatched = tuple(current_by_name[key] for key in current_unmatched_keys)
        published_unmatched = tuple(published_by_name[key] for key in published_unmatched_keys)
        current_by_friendly = cls._unique_client_pack_mods_by_friendly(current_unmatched)
        published_by_friendly = cls._unique_client_pack_mods_by_friendly(published_unmatched)

        for friendly_key in sorted(current_by_friendly.keys() & published_by_friendly.keys()):
            before = published_by_friendly[friendly_key]
            after = current_by_friendly[friendly_key]
            matched_pairs.append((before, after))
            current_unmatched_keys.remove(after.name.casefold())
            published_unmatched_keys.remove(before.name.casefold())

        added = tuple(
            current_by_name[key]
            for key in sorted(current_unmatched_keys, key=lambda item: current_by_name[item].friendly.casefold())
        )
        removed = tuple(
            published_by_name[key]
            for key in sorted(published_unmatched_keys, key=lambda item: published_by_name[item].friendly.casefold())
        )
        updated = tuple(
            (before, after)
            for before, after in sorted(matched_pairs, key=lambda item: item[1].friendly.casefold())
            if before.version != after.version or before.name != after.name or before.friendly != after.friendly
        )

        lines: list[str] = []
        if not published and not has_published_pack:
            lines.append("Initial client pack contents:")
            lines.extend(f"- {cls._client_pack_mod_snapshot_label(snapshot)}" for snapshot in current)
            return "\n".join(lines)
        if not published and has_published_pack:
            lines.append("Published mod snapshot will be tracked after the next publish.")
            lines.append("Current default client pack contents:")
            lines.extend(f"- {cls._client_pack_mod_snapshot_label(snapshot)}" for snapshot in current)
            return "\n".join(lines)

        if added:
            lines.append("Added mods:")
            lines.extend(f"- {cls._client_pack_mod_snapshot_label(snapshot)}" for snapshot in added)
        if removed:
            if lines:
                lines.append("")
            lines.append("Removed mods:")
            lines.extend(f"- {cls._client_pack_mod_snapshot_label(snapshot)}" for snapshot in removed)
        if updated:
            if lines:
                lines.append("")
            lines.append("Updated mods:")
            lines.extend(
                f"- {cls._client_pack_mod_update_label(before, after)}"
                for before, after in updated
            )
        return "\n".join(lines) if lines else "No automated client-pack changes detected."

    def _client_pack_automated_changelog(self, app: App) -> str:
        if not app.mod_capabilities.supports_client_pack:
            return ""
        try:
            current = self._default_client_pack_mod_snapshots(app)
        except Exception as xcp:
            log.warning("Client-pack automated changelog failed: app=%s error=%s", app.name, xcp)
            return f"Automated client-pack changelog is unavailable: {xcp}"
        return self._client_pack_automated_changelog_text(
            current=current,
            published=app.cfg.client_pack_published_mods,
            has_published_pack=app.cfg.client_pack_published_hash is not None,
        )

    @staticmethod
    def _normalised_client_pack_node_label(node_name: str) -> str:
        text = node_name.strip()
        if not text:
            return "Node"
        if text.casefold() == text:
            return text.title()
        return text

    def _client_pack_node_label(self) -> str:
        node_key = self.node_name.casefold()
        for snapshot in self._known_bot_snapshots():
            mod_web = snapshot.features.mod_web
            if mod_web is None or mod_web.node_name.casefold() != node_key:
                continue
            if snapshot.profile.label:
                return snapshot.profile.label
        if node_key == config.ACTIVE_BOT_PROFILE.name.value.casefold():
            return config.ACTIVE_BOT_PROFILE.name.value.title()
        return self._normalised_client_pack_node_label(self.node_name)

    @staticmethod
    def _known_bot_snapshots() -> tuple[config.BotMetadataSnapshot, ...]:
        snapshots: list[config.BotMetadataSnapshot] = []
        try:
            snapshots.extend(config.load_bot_configuration(Path("configuration.json")).known_bots.values())
        except Exception as xcp:
            log.warning("Node API failed to load local bot registry: %s", xcp)

        cache_path = config.authority_cache_path(AuthorityResource.BOTS)
        if cache_path.exists():
            try:
                raw_cache = read_json_object(cache_path)
                snapshots.extend(
                    config.BotMetadataSnapshot.model_validate(snapshot)
                    for snapshot in raw_cache.values()
                    if isinstance(snapshot, dict)
                )
            except Exception as xcp:
                log.warning("Node API failed to load cached bot registry: %s", xcp)

        unique: dict[str, config.BotMetadataSnapshot] = {}
        for snapshot in snapshots:
            unique[snapshot.profile.id] = snapshot
        return tuple(unique.values())

    @staticmethod
    def _minecraft_servers_dat_server_name(node_label: str) -> str:
        base = "".join(character for character in node_label.strip() if character.isalnum())
        if not base:
            base = "Node"
        return f"{base}Server"

    @staticmethod
    def _minecraft_servers_dat_content(*, server_name: str, server_address: str) -> bytes:
        def tag_name(name: str) -> bytes:
            encoded = name.encode("utf-8")
            return struct.pack(">H", len(encoded)) + encoded

        def string_tag(name: str, value: str) -> bytes:
            encoded = value.encode("utf-8")
            return b"\x08" + tag_name(name) + struct.pack(">H", len(encoded)) + encoded

        server_compound = (
            string_tag("name", server_name)
            + string_tag("ip", server_address)
            + b"\x00"
        )
        servers_list = b"\x09" + tag_name("servers") + b"\x0a" + struct.pack(">i", 1) + server_compound
        return b"\x0a" + tag_name("") + servers_list + b"\x00"

    def _minecraft_client_pack_extra_entries(
        self,
        *,
        app: Minecraft,
        metadata: ClientPackMetadataConfig,
    ) -> tuple[ArchiveDataEntry, ...]:
        if not metadata.include_servers_dat:
            return ()
        server_address = app.cfg.join_direct_ip_address or app.cfg.join_address
        if server_address is None:
            log.warning("Skipping generated servers.dat because %s has no join address.", app.name)
            return ()
        return (
            ArchiveDataEntry(
                archive_path=PurePosixPath("overrides/servers.dat"),
                content=self._minecraft_servers_dat_content(
                    server_name=self._minecraft_servers_dat_server_name(self._client_pack_node_label()),
                    server_address=server_address,
                ),
            ),
        )

    def _client_override_entries_for_pack(
        self,
        *,
        app: App,
        metadata: ClientPackMetadataConfig | None,
    ) -> tuple[ArchiveEntry, ...]:
        overrides_dir = self._client_overrides_dir_for_pack(app)
        if overrides_dir is None:
            return ()
        overrides_path = overrides_dir.resolve()
        if not overrides_path.exists() or not overrides_path.is_dir():
            raise ClientPackValidationError(f"Client overrides directory is missing: {overrides_path}")

        excluded_paths: frozenset[PurePosixPath] = frozenset(
            {PurePosixPath("options.txt")}
            if metadata is not None and not metadata.include_options_txt
            else ()
        )
        entries: list[ArchiveEntry] = []
        override_files = tuple(
            sorted(
                (path for path in overrides_path.rglob("*") if path.is_file()),
                key=lambda path: path.as_posix(),
            )
        )
        for file_path in override_files:
            relative_path = PurePosixPath(file_path.relative_to(overrides_path).as_posix())
            if relative_path in excluded_paths:
                continue
            entries.append(
                ArchiveEntry(
                    source_path=file_path,
                    archive_path=PurePosixPath("overrides") / relative_path,
                )
            )
        return tuple(entries)

    def _client_pack_entries(
        self,
        selection: ClientPackSelection,
        *,
        app: App,
        include_kubejs_scripts: bool,
        include_servers_dat: bool | None = None,
        include_options_txt: bool | None = None,
    ) -> tuple[ArchiveEntry | ArchiveDataEntry, ...]:
        metadata = self._client_pack_metadata(app)
        if metadata is not None and (include_servers_dat is not None or include_options_txt is not None):
            metadata = metadata.model_copy(
                update={
                    "include_servers_dat": metadata.include_servers_dat
                    if include_servers_dat is None
                    else include_servers_dat,
                    "include_options_txt": metadata.include_options_txt
                    if include_options_txt is None
                    else include_options_txt,
                }
            )
        entries = build_client_pack_entries(
            app.has_mod_manager,
            selection,
            client_overrides_dir=None,
        )
        entries = (
            *entries,
            *self._client_override_entries_for_pack(app=app, metadata=metadata),
        )
        if not isinstance(app, Minecraft):
            return entries
        if metadata is not None:
            entries = (
                *entries,
                *self._minecraft_client_pack_extra_entries(app=app, metadata=metadata),
            )
        if not include_kubejs_scripts:
            return entries
        return (
            *entries,
            *client_pack_kubejs_entries(
                app.directory,
                excluded_paths=frozenset(app.cfg.client_pack_excluded_kubejs_scripts),
            ),
        )

    def build_config_list(self, app: App, *, actor_user_id: int | None = None) -> NodeConfigList:
        configs: tuple[AppConfigFile, ...] = app.list_config_files()
        if actor_user_id is not None and self._acl is not None:
            configs = tuple[AppConfigFile, ...](
                config_file for config_file in configs if self._acl.can(actor_user_id, config_file.read_power_level)
            )
        traffic_log.info(
            "Node API built config list: node=%s app=%s configs=%s", self.node_name, app.name, len(configs)
        )
        return NodeConfigList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            configs=tuple[NodeConfigEntry, ...](self._config_entry(config_file) for config_file in configs),
        )

    def read_config_file(self, *, app: App, config_id: str) -> NodeConfigContent:
        try:
            content: AppConfigFileContent = app.read_config_file(config_id)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        return self._config_content(app=app, content=content)

    def write_config_file(self, *, app: App, config_id: str, content: str) -> NodeConfigContent:
        try:
            updated: AppConfigFileContent = app.write_config_file(config_id, content)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        traffic_log.info("Node API wrote config file: node=%s app=%s config=%s", self.node_name, app.name, config_id)
        self._invalidate_client_pack_content(app)
        return self._config_content(app=app, content=updated)

    def _require_factorio_app(self, app: App) -> None:
        if app.scope != config.AppScopes.factorio.value:
            raise _http_exception(400, f"{app.friendly} does not support Factorio mod settings.")

    def factorio_mod_settings_state(self, *, app: App) -> NodeFactorioModSettings:
        self._require_factorio_app(app)
        try:
            return _build_factorio_mod_settings_state(app=app, node_name=self.node_name)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

    def build_factorio_mod_settings_download_response(self, *, app: App) -> FileResponse:
        self._require_factorio_app(app)
        try:
            return _build_factorio_mod_settings_download_response(app=app)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

    async def upload_factorio_mod_settings(
        self,
        *,
        app: App,
        upload: UploadFile,
        upload_name: str,
        actor_user_id: int,
    ) -> NodeFactorioModSettings:
        del actor_user_id
        self._require_factorio_app(app)
        resolved_upload_name = self._validated_upload_filename(upload_name, kind="Factorio mod settings")
        if resolved_upload_name != "mod-settings.dat":
            raise _http_exception(400, "Factorio mod settings upload must be named mod-settings.dat.")
        temp_path = await self._persist_upload_to_temp(upload)
        try:
            target = factorio_mod_settings_path(app.directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(File_Utils.copy, temp_path, target, True)
        finally:
            temp_path.unlink(missing_ok=True)
        traffic_log.info("Node API uploaded Factorio mod settings: node=%s app=%s", self.node_name, app.name)
        self._invalidate_state_caches(app_name=app.name)
        return self.factorio_mod_settings_state(app=app)

    def delete_factorio_mod_settings(self, *, app: App) -> NodeFactorioModSettings:
        self._require_factorio_app(app)
        pointer = factorio_mod_settings_path(app.directory)
        if pointer.exists() and not pointer.is_file():
            raise _http_exception(400, f"Factorio mod settings path is not a file: {pointer}")
        File_Utils.remove(pointer, silent=True, resolve=False)
        traffic_log.info("Node API deleted Factorio mod settings: node=%s app=%s", self.node_name, app.name)
        self._invalidate_state_caches(app_name=app.name)
        return self.factorio_mod_settings_state(app=app)

    def _invalidate_client_pack_content(self, app: App) -> None:
        app.invalidate_client_pack_content()
        self._invalidate_state_caches(app_name=app.name)

    async def build_config_root_download_response(
        self,
        *,
        app: App,
        root_id: str,
        actor_user_id: int | None = None,
    ) -> FileResponse:
        try:
            root: AppConfigFileRoot = app.resolve_config_root(root_id)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

        if actor_user_id is not None and self._acl is not None and not self._acl.can(
            actor_user_id, app.config_file_read_level_for_root(root_id)
        ):
            raise _http_exception(403, f"Insufficient level for config root: {root.label}")

        root_path: Path = root.resolved_path
        if not root_path.exists():
            raise _http_exception(404, f"Config root does not exist: {root.label}")
        if not root_path.is_file() and not root_path.is_dir():
            raise _http_exception(404, f"Config root is unsupported: {root.label}")

        visible_configs: tuple[AppConfigFile, ...] = tuple[AppConfigFile, ...](
            config_file for config_file in app.list_config_files() if config_file.root_id == root_id
        )
        if actor_user_id is not None and self._acl is not None:
            visible_configs = tuple[AppConfigFile, ...](
                config_file
                for config_file in visible_configs
                if self._acl.can(actor_user_id, config_file.read_power_level)
            )
        if not visible_configs:
            raise _http_exception(404, f"No downloadable config files found in root: {root.label}")
        if root_path.is_file():
            traffic_log.info("Node API sending config file root: node=%s app=%s root=%s", self.node_name, app.name, root_id)
            return FileResponse(path=root_path, filename=root_path.name)

        paths: tuple[Path, ...] = tuple[Path, ...](
            app.resolve_config_file(config_file.id) for config_file in visible_configs
        )
        archive_path: Path = await File_Utils.compress(
            paths, self._config_root_archive_name(app=app, root=root), arc_base=root_path
        )
        traffic_log.info(
            "Node API sending config root archive: node=%s app=%s root=%s files=%s archive=%s",
            self.node_name,
            app.name,
            root_id,
            len(paths),
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_path.name)

    async def build_save_list(self, app: App) -> NodeSaveList:
        saves: tuple[AppSaveEntry, ...] = await app.list_save_files_async()
        save_can_delete: bool = bool(getattr(app, "supports_save_delete", False))
        traffic_log.info("Node API built save list: node=%s app=%s saves=%s", self.node_name, app.name, len(saves))
        return replace(
            self.build_empty_save_list(app),
            saves=tuple[NodeSaveEntry, ...](self._save_entry(save, can_delete=save_can_delete) for save in saves),
        )

    def build_empty_save_list(self, app: App) -> NodeSaveList:
        return NodeSaveList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            roots=tuple[NodeSaveRootEntry, ...](
                NodeSaveRootEntry(id=root.id, label=root.label) for root in app.save_file_roots
            ),
            saves=(),
        )

    async def build_save_download_response(self, *, app: App, save_id: str) -> Response:
        try:
            custom_download: tuple[str, bytes] | None = await app.download_save_content(save_id)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Save download", error=xcp) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Save download failed: {xcp}") from xcp
        if custom_download is not None:
            filename, content = custom_download
            traffic_log.info(
                "Node API sending save content: node=%s app=%s save=%s filename=%s bytes=%s",
                self.node_name,
                app.name,
                save_id,
                filename,
                len(content),
            )
            return Response(
                content=content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        try:
            save_path: Path = app.resolve_save_file(save_id)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

        if not save_path.exists():
            raise _http_exception(404, f"Save file does not exist: {save_path.name}")
        if save_path.is_file():
            traffic_log.info("Node API sending save file: node=%s app=%s path=%s", self.node_name, app.name, save_path)
            return FileResponse(path=save_path, filename=save_path.name)
        if not save_path.is_dir():
            raise _http_exception(404, f"Save path is unsupported: {save_path.name}")

        archive_path: Path = await File_Utils.compress(
            save_path,
            self._save_archive_name(app=app, save_path=save_path),
            arc_base=save_path.parent,
        )
        traffic_log.info(
            "Node API sending save archive: node=%s app=%s path=%s archive=%s",
            self.node_name,
            app.name,
            save_path,
            archive_path,
        )
        return FileResponse(path=archive_path, filename=archive_path.name)

    async def upload_save_file(
        self,
        *,
        app: App,
        root_id: str,
        upload: UploadFile,
        upload_name: str | None,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_uploads:
            raise _http_exception(409, f"{app.friendly} does not support save uploads.")
        resolved_upload_name: str = (upload_name or upload.filename or "").strip()
        if not resolved_upload_name:
            raise _http_exception(400, "Save upload filename is required.")

        temp_path: Path = await self._persist_upload_to_temp(upload)
        try:
            return await self.upload_save_path(
                app=app,
                root_id=root_id,
                source_path=temp_path,
                upload_name=resolved_upload_name,
                actor_user_id=actor_user_id,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def upload_save_path(
        self,
        *,
        app: App,
        root_id: str,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_uploads:
            raise _http_exception(409, f"{app.friendly} does not support save uploads.")
        save_can_delete: bool = bool(getattr(app, "supports_save_delete", False))
        try:
            updated: AppSaveEntry = await app.upload_save_file_async(
                root_id=root_id, upload_name=upload_name, source_path=source_path
            )
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Save upload", error=xcp) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Save upload failed: {xcp}") from xcp

        traffic_log.info(
            "Node API save uploaded: node=%s app=%s root=%s save=%s actor=%s",
            self.node_name,
            app.name,
            root_id,
            updated.id,
            actor_user_id,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"Uploaded save `{updated.label}` for {app.friendly}.",
            save=self._save_entry(updated, can_delete=save_can_delete),
        )

    async def upload_mod_file(
        self,
        *,
        app: App,
        upload: UploadFile,
        upload_name: str | None,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> NodeModUploadResult:
        result = await self.upload_mod_files(
            app=app,
            uploads=[upload],
            upload_names=None if upload_name is None else [upload_name],
            actor_user_id=actor_user_id,
            placement=placement,
        )
        return self._single_mod_upload_result(result)

    async def upload_mod_files(
        self,
        *,
        app: App,
        uploads: Sequence[UploadFile],
        upload_names: Sequence[str] | None,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> NodeModUploadBatchResult:
        upload_sources = self._resolve_mod_upload_requests(uploads=uploads, upload_names=upload_names)
        temp_paths: list[Path] = []
        try:
            resolved_sources: list[NodeModUploadSource] = []
            for upload_request in upload_sources:
                temp_path = await self._persist_upload_to_temp(upload_request.upload)
                temp_paths.append(temp_path)
                resolved_sources.append(
                    NodeModUploadSource(
                        source_path=temp_path,
                        upload_name=upload_request.upload_name,
                    )
                )
            return await self.upload_mod_paths(
                app=app,
                upload_sources=resolved_sources,
                actor_user_id=actor_user_id,
                placement=placement,
            )
        finally:
            for temp_path in temp_paths:
                temp_path.unlink(missing_ok=True)

    async def upload_mod_path(
        self,
        *,
        app: App,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> NodeModUploadResult:
        result = await self.upload_mod_paths(
            app=app,
            upload_sources=[NodeModUploadSource(source_path=source_path, upload_name=upload_name)],
            actor_user_id=actor_user_id,
            placement=placement,
        )
        return self._single_mod_upload_result(result)

    async def install_mod_from_link(
        self,
        *,
        app: App,
        url: str,
        actor_user_id: int,
        selected_mod_ids: Sequence[str] | None = None,
        version: str | None = None,
    ) -> NodeModUploadBatchResult:
        return await _install_factorio_mod_from_link(
            app=app,
            url=url,
            actor_user_id=actor_user_id,
            upload_mod_paths=self.upload_mod_paths,
            selected_mod_ids=selected_mod_ids,
            version=version,
        )

    async def resolve_mod_link_dependencies(
        self,
        *,
        app: App,
        url: str,
        version: str | None = None,
    ) -> NodeModDependencyResolutionResult:
        return await _resolve_factorio_mod_link_dependencies(
            app=app,
            node_name=self.node_name,
            url=url,
            version=version,
        )

    async def list_mod_link_versions(
        self,
        *,
        app: App,
        url: str,
    ) -> NodeModPortalVersionList:
        return await _list_factorio_mod_link_versions(app=app, node_name=self.node_name, url=url)

    async def list_installed_mod_versions(
        self,
        *,
        app: App,
        mod_name: str,
    ) -> NodeModPortalVersionList:
        return await _list_installed_factorio_mod_versions(app=app, node_name=self.node_name, mod_name=mod_name)

    async def _factorio_mod_versions(self, *, app: App, url: str) -> NodeModPortalVersionList:
        return await _factorio_mod_versions(app=app, node_name=self.node_name, url=url)

    @staticmethod
    def _factorio_installed_mod_ids(app: App) -> frozenset[str]:
        return frozenset(_factorio_installed_mods_by_id(app))

    @staticmethod
    def _factorio_vanilla_mods(app: App) -> Mapping[str, FactorioVanillaMod]:
        return _factorio_vanilla_mods(app)

    @staticmethod
    def _factorio_installed_mods_by_id(app: App) -> Mapping[str, Mod]:
        return _factorio_installed_mods_by_id(app)

    async def check_mod_update(
        self,
        *,
        app: App,
        mod_name: str,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        return await _check_factorio_mod_update_by_name(
            app=app,
            node_name=self.node_name,
            mod_name=mod_name,
            version=version,
        )

    async def update_mod(
        self,
        *,
        app: App,
        mod_name: str,
        actor_user_id: int,
        version: str | None = None,
    ) -> NodeModUploadBatchResult:
        update_result: FactorioModUpdateApplyResult = await _update_factorio_mod(
            app=app,
            node_name=self.node_name,
            mod_name=mod_name,
            version=version,
        )
        old_mod = update_result.old_mod
        added_mods = update_result.added_mods
        update_check = update_result.update_check
        dependency_actions = update_result.dependency_actions

        traffic_log.info(
            "Node API mod updated: node=%s app=%s old_mod=%s new_mod=%s actor=%s",
            self.node_name,
            app.name,
            old_mod.name,
            ",".join(updated_mod.name for updated_mod in added_mods),
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        updated_entries = tuple(self._mod_entry(updated_mod) for updated_mod in added_mods)
        dependency_change_count = len(dependency_actions)
        dependency_suffix = (
            ""
            if dependency_change_count == 0
            else f" Updated {dependency_change_count} required dependenc{'ies' if dependency_change_count != 1 else 'y'}."
        )
        return NodeModUploadBatchResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=(
                f"Updated `{old_mod.friendly}` from {update_check.current_version} "
                f"to {update_check.latest_version}.{dependency_suffix}"
            ),
            mods=updated_entries,
        )

    async def _check_factorio_mod_update(
        self,
        *,
        app: App,
        mod: Mod,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        return await _check_factorio_mod_update(
            app=app,
            node_name=self.node_name,
            mod=mod,
            version=version,
        )


    @staticmethod
    def _factorio_dependency_update_entry(
        *,
        candidate: FactorioModPortalCandidate,
        installed_mod: Mod | None,
        vanilla_mod: FactorioVanillaMod | None = None,
    ) -> NodeModUpdateDependency:
        return _factorio_dependency_update_entry(
            candidate=candidate,
            installed_mod=installed_mod,
            vanilla_mod=vanilla_mod,
        )

    @staticmethod
    def _factorio_dependency_update_summary(
        dependencies: Iterable[NodeModUpdateDependency],
    ) -> str | None:
        return _factorio_dependency_update_summary(dependencies)

    @staticmethod
    def _factorio_mod_update_page_url(mod: Mod) -> str:
        try:
            return _factorio_mod_update_page_url(mod)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp


    async def upload_mod_paths(
        self,
        *,
        app: App,
        upload_sources: Sequence[NodeModUploadSource],
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> NodeModUploadBatchResult:
        if app.mods is None:
            raise _http_exception(409, f"{app.friendly} does not support mods.")
        resolved_upload_sources: tuple[NodeModUploadSource, ...] = self._validated_mod_upload_sources(upload_sources)
        try:
            require_app_stopped_for_mod_mutation(app)
            manager: Mod_Manager = app.has_mod_manager
            await manager.reload_mods()
            uploaded_mods: list[Mod] = []
            with tempfile.TemporaryDirectory(prefix="yukibot-mod-upload-") as temp_dir:
                for upload_source in resolved_upload_sources:
                    staged_path: Path = Path(temp_dir) / upload_source.upload_name
                    await asyncio.to_thread(File_Utils.copy, upload_source.source_path, staged_path, True)
                    uploaded_mods.append(
                        await manager.add(staged_path, atomic=True, placement=placement)
                    )
        except RunningAppModMutationError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Mod upload failed: {xcp}") from xcp

        traffic_log.info(
            "Node API mods uploaded: node=%s app=%s mods=%s actor=%s",
            self.node_name,
            app.name,
            ",".join(mod.name for mod in uploaded_mods),
            actor_user_id,
        )
        mod_entries: tuple[NodeModEntry, ...] = tuple(self._mod_entry(uploaded_mod) for uploaded_mod in uploaded_mods)
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        return NodeModUploadBatchResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=self._mod_upload_message(app=app, mods=mod_entries),
            mods=mod_entries,
        )

    async def rename_save_file(
        self,
        *,
        app: App,
        save_id: str,
        new_name: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_rename:
            raise _http_exception(409, f"{app.friendly} does not support save renaming.")
        save_can_delete: bool = bool(getattr(app, "supports_save_delete", False))
        resolved_name: str = new_name.strip()
        if not resolved_name:
            raise _http_exception(400, "Save name must not be empty.")

        try:
            current_save: AppSaveEntry = next(save for save in await app.list_save_files_async() if save.id == save_id)
        except StopIteration as xcp:
            raise _http_exception(404, f"Unknown save file: {save_id}") from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Save rename", error=xcp) from xcp

        destination_relative_path: str = PurePosixPath(current_save.relative_path).with_name(resolved_name).as_posix()
        try:
            updated: AppSaveEntry = await app.relocate_save_file_async(
                save_id=save_id,
                destination_root_id=current_save.root_id,
                destination_relative_path=destination_relative_path,
            )
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Save rename", error=xcp) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Save rename failed: {xcp}") from xcp

        traffic_log.info(
            "Node API save renamed: node=%s app=%s save=%s renamed_to=%s actor=%s",
            self.node_name,
            app.name,
            save_id,
            updated.id,
            actor_user_id,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"Renamed save to `{updated.label}` for {app.friendly}.",
            save=self._save_entry(updated, can_delete=save_can_delete),
        )

    async def delete_save_file(
        self,
        *,
        app: App,
        save_id: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_delete:
            raise _http_exception(409, f"{app.friendly} does not support save deletion.")
        save_can_delete: bool = bool(getattr(app, "supports_save_delete", False))
        try:
            deleted: AppSaveEntry = await app.delete_save_file_async(file_id=save_id)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Save delete", error=xcp) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Save delete failed: {xcp}") from xcp

        traffic_log.info(
            "Node API save deleted: node=%s app=%s save=%s actor=%s",
            self.node_name,
            app.name,
            save_id,
            actor_user_id,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"Deleted save `{deleted.label}` from {app.friendly}.",
            save=self._save_entry(deleted, can_delete=save_can_delete),
        )

    @staticmethod
    def _runtime_http_exception(*, app: App, action: str, error: RuntimeError) -> Exception:
        detail = str(error)
        if detail == f"{app.friendly} is not running.":
            return _http_exception(409, detail)
        if detail.endswith("API is unavailable.") or detail.endswith("save API is unavailable."):
            return _http_exception(503, f"{action} failed: {detail}")
        return _http_exception(409, detail)

    def build_blueprint_list(self, app: App, *, actor_user_id: int) -> NodeBlueprintList:
        if not app.supports_blueprints:
            raise _http_exception(409, f"{app.friendly} does not support blueprint files.")
        blueprints: tuple[AppBlueprintEntry, ...] = app.list_blueprint_files()
        traffic_log.info(
            "Node API built blueprint list: node=%s app=%s blueprints=%s",
            self.node_name,
            app.name,
            len(blueprints),
        )
        return replace(
            self.build_empty_blueprint_list(app),
            blueprints=tuple[NodeBlueprintEntry, ...](
                self._blueprint_entry(blueprint_file, actor_user_id=actor_user_id) for blueprint_file in blueprints
            ),
        )

    def build_empty_blueprint_list(self, app: App) -> NodeBlueprintList:
        if not app.supports_blueprints:
            raise _http_exception(409, f"{app.friendly} does not support blueprint files.")
        return NodeBlueprintList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            blueprints=(),
            default_session_name=app.default_blueprint_session_name,
        )

    async def upload_blueprint_files(
        self,
        *,
        app: App,
        session_name: str,
        uploads: list[UploadFile],
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult:
        if not app.supports_blueprints:
            raise _http_exception(409, f"{app.friendly} does not support blueprint uploads.")
        resolved_names: list[str] = []
        for upload in uploads:
            raw_upload_name: str = upload.filename or ""
            if raw_upload_name != raw_upload_name.strip():
                raise _http_exception(400, "Blueprint filenames must not start or end with spaces.")
            resolved_names.append(self._validated_upload_filename(raw_upload_name, kind="Blueprint"))
        try:
            upload_pair: BlueprintUploadPair = classify_blueprint_upload_filenames(resolved_names)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

        temp_paths: dict[str, Path] = {}
        try:
            for upload, resolved_name in zip(uploads, resolved_names, strict=True):
                temp_paths[resolved_name] = await self._persist_upload_to_temp(upload)
            config_source_path: Path | None = None
            if upload_pair.config_filename is not None:
                config_source_path = temp_paths[upload_pair.config_filename]
            return self.upload_blueprint_path(
                app=app,
                session_name=session_name,
                source_path=temp_paths[upload_pair.module_filename],
                upload_name=upload_pair.module_filename,
                actor_user_id=actor_user_id,
                config_source_path=config_source_path,
                config_upload_name=upload_pair.config_filename,
            )
        finally:
            for temp_path in temp_paths.values():
                temp_path.unlink(missing_ok=True)

    def upload_blueprint_path(
        self,
        *,
        app: App,
        session_name: str,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
        config_source_path: Path | None = None,
        config_upload_name: str | None = None,
    ) -> NodeBlueprintMutationResult:
        if not app.supports_blueprints:
            raise _http_exception(409, f"{app.friendly} does not support blueprint uploads.")
        if upload_name != upload_name.strip():
            raise _http_exception(400, "Blueprint filenames must not start or end with spaces.")
        if config_upload_name is not None and config_upload_name != config_upload_name.strip():
            raise _http_exception(400, "Blueprint config filenames must not start or end with spaces.")
        resolved_upload_names: list[str] = [self._validated_upload_filename(upload_name, kind="Blueprint")]
        if config_upload_name is not None:
            resolved_upload_names.append(self._validated_upload_filename(config_upload_name, kind="Blueprint"))
        try:
            upload_pair: BlueprintUploadPair = classify_blueprint_upload_filenames(resolved_upload_names)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        try:
            uploaded: AppBlueprintEntry = app.upload_blueprint_file(
                session_name=session_name,
                upload_name=upload_pair.module_filename,
                source_path=source_path,
                actor_user_id=actor_user_id,
                config_upload_name=upload_pair.config_filename,
                config_source_path=config_source_path,
            )
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Blueprint upload failed: {xcp}") from xcp

        traffic_log.info(
            "Node API blueprint uploaded: node=%s app=%s blueprint=%s actor=%s",
            self.node_name,
            app.name,
            uploaded.id,
            actor_user_id,
        )
        message: str = f"Uploaded blueprint `{uploaded.label}` for {app.friendly}."
        if upload_pair.config_filename is not None:
            message = (
                f"Uploaded blueprint `{uploaded.label}` with config `{upload_pair.config_filename}` for {app.friendly}."
            )
        return NodeBlueprintMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=message,
            blueprint=self._blueprint_entry(uploaded, actor_user_id=actor_user_id),
        )

    def delete_blueprint_file(
        self,
        *,
        app: App,
        blueprint_id: str,
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult:
        if not app.supports_blueprints:
            raise _http_exception(409, f"{app.friendly} does not support blueprint deletion.")
        actor_is_sudo: bool = self._require_acl().can(actor_user_id, Power_Level.sudo)
        try:
            deleted: AppBlueprintEntry = app.delete_blueprint_file(
                file_id=blueprint_id,
                actor_user_id=actor_user_id,
                actor_is_sudo=actor_is_sudo,
            )
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except PermissionError as xcp:
            raise _http_exception(403, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Blueprint delete failed: {xcp}") from xcp

        delete_message: str = f"Deleted blueprint `{deleted.label}` from {app.friendly}."
        try:
            deleted_file_type = blueprint_file_type_from_name(PurePosixPath(blueprint_id).name)
        except ValueError:
            deleted_file_type = AppBlueprintFileType.MODULE
        if deleted_file_type is AppBlueprintFileType.CONFIG:
            delete_message = f"Deleted blueprint config `{PurePosixPath(blueprint_id).name}` from {app.friendly}."
        elif deleted.config_file is not None:
            delete_message = f"Deleted blueprint `{deleted.label}` and its matching config from {app.friendly}."

        traffic_log.info(
            "Node API blueprint deleted: node=%s app=%s blueprint=%s actor=%s",
            self.node_name,
            app.name,
            blueprint_id,
            actor_user_id,
        )
        return NodeBlueprintMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=delete_message,
            blueprint=self._blueprint_entry(deleted, actor_user_id=actor_user_id),
        )

    def build_setting_list(self, *, app: App, actor_user_id: int) -> NodeSettingList:
        settings: tuple[Setting[object], ...] = self._settings_for_app(app)
        acl: Access_Control = self._require_acl()
        settings_manager: Settings_Manager = self._require_settings_manager(app)
        entries: tuple[NodeSettingEntry, ...] = tuple[NodeSettingEntry, ...](
            self._setting_entry(setting, acl=acl, actor_user_id=actor_user_id, settings_manager=settings_manager)
            for setting in settings
        )
        editable_count: int = sum(1 for setting in settings if acl.can(actor_user_id, setting.power_level))
        return NodeSettingList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            editable_count=editable_count,
            restricted_count=len(settings) - editable_count,
            has_pending_changes=settings_manager.has_pending_changes(actor_user_id),
            pending_change_count=settings_manager.pending_change_count(actor_user_id),
            required_save_level_name=settings_manager.required_save_level(actor_user_id).name,
            required_reload_level_name=settings_manager.required_reload_level(actor_user_id).name,
            settings=entries,
        )

    async def update_setting(
        self,
        *,
        app: App,
        setting_key: str,
        value: str,
        actor_user_id: int,
    ) -> NodeSettingMutationResult:
        setting: Setting[object] = self._resolve_setting(app=app, setting_key=setting_key)
        await self._require_acl().perm_check(actor_user_id, setting.power_level)
        settings_manager: Settings_Manager = self._require_settings_manager(app)

        resolved_value: str = value.strip()
        if not resolved_value and not setting.allows_blank_input:
            raise _http_exception(400, "Setting value must not be empty.")

        try:
            settings_manager.update_setting(actor_user_id, setting, resolved_value, remember_input=True)
        except (IndexError, ValueError) as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Setting update failed: {xcp}") from xcp

        traffic_log.info(
            "Node API setting updated: node=%s app=%s setting=%s actor=%s",
            self.node_name,
            app.name,
            setting.key,
            actor_user_id,
        )
        return NodeSettingMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            setting_key=setting.key,
            message=(
                f"{app.friendly} setting `{setting.label}` updated: "
                f"{settings_manager.display_value(setting, actor_user_id)}. "
                "Settings are saved on launch or via Save Settings."
            ),
            setting=self._setting_entry(
                setting,
                acl=self._require_acl(),
                actor_user_id=actor_user_id,
                settings_manager=settings_manager,
            ),
        )

    async def save_settings(self, *, app: App, actor_user_id: int) -> NodeSettingsActionResult:
        settings_manager: Settings_Manager = self._require_settings_manager(app)
        await self._require_acl().perm_check(actor_user_id, settings_manager.required_save_level(actor_user_id))
        try:
            settings_manager.save(actor_user_id)
        except Exception as xcp:
            raise _http_exception(500, f"Settings save failed: {xcp}") from xcp
        traffic_log.info("Node API settings saved: node=%s app=%s actor=%s", self.node_name, app.name, actor_user_id)
        return NodeSettingsActionResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"Saved settings for {app.friendly}.",
        )

    async def reload_settings(self, *, app: App, actor_user_id: int) -> NodeSettingsActionResult:
        settings_manager: Settings_Manager = self._require_settings_manager(app)
        await self._require_acl().perm_check(actor_user_id, settings_manager.required_reload_level(actor_user_id))
        try:
            settings_manager.load(actor_user_id)
        except Exception as xcp:
            raise _http_exception(500, f"Settings reload failed: {xcp}") from xcp
        traffic_log.info("Node API settings reloaded: node=%s app=%s actor=%s", self.node_name, app.name, actor_user_id)
        return NodeSettingsActionResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"{app.friendly} settings reloaded from disk.",
        )

    def build_console_action_list(self, *, app: App, actor_user_id: int) -> NodeConsoleActionList:
        acl: Access_Control = self._require_acl()
        runtime_running: bool = app.check_running()
        return NodeConsoleActionList(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            actions=tuple[NodeConsoleActionEntry, ...](
                self._console_action_entry(
                    action=action,
                    actor_user_id=actor_user_id,
                    acl=acl,
                    runtime_running=runtime_running,
                )
                for action in app.console_actions
            ),
        )

    async def read_console_stdout(
        self,
        *,
        app: App,
        actor_user_id: int,
        max_lines: int = 200,
    ) -> NodeConsoleStdoutSnapshot:
        await self._require_acl().perm_check(actor_user_id, Power_Level.user)
        return self.build_console_stdout_snapshot(app=app, max_lines=max_lines)

    def build_console_stdout_snapshot(
        self,
        *,
        app: App,
        max_lines: int = 200,
    ) -> NodeConsoleStdoutSnapshot:
        try:
            stdout_tail: AppStdoutTail = app.read_stdout_tail(max_lines=max_lines)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Console stdout read failed: {xcp}") from xcp
        return NodeConsoleStdoutSnapshot(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            lines=stdout_tail.lines,
            truncated=stdout_tail.truncated,
            running=app.check_running(),
        )

    async def execute_console_action(
        self,
        *,
        app: App,
        action_key: str,
        raw_value: str | None,
        actor_user_id: int,
    ) -> NodeConsoleActionExecutionResult:
        action: ConsoleAction = self._resolve_console_action(app, action_key)
        await self._require_acl().perm_check(actor_user_id, action.power_level)
        try:
            result: ConsoleActionResult = await execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value=raw_value,
            )
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Console action", error=xcp) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Console action failed: {xcp}") from xcp

        traffic_log.info(
            "Node API console action executed: node=%s app=%s action=%s actor=%s success=%s",
            self.node_name,
            app.name,
            action.key,
            actor_user_id,
            result.success,
        )
        return NodeConsoleActionExecutionResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            action_key=action.key,
            summary=result.summary,
            success=result.success,
            text=result.text,
            source=result.source,
        )

    async def mutate_mod(
        self,
        *,
        app: App,
        mod_name: str,
        action: NodeModMutationAction,
        actor_user_id: int,
    ) -> NodeModMutationResult:
        try:
            manager: Mod_Manager = app.has_mod_manager
            await manager.reload_mods()
            mod: Mod = _get_mod_or_404(manager, mod_name)
            override_protected_mod: bool = mod.is_protected and action in {
                NodeModMutationAction.ENABLE,
                NodeModMutationAction.DISABLE,
                NodeModMutationAction.DELETE,
            }
            await self._require_acl().perm_check(
                actor_user_id,
                required_mod_mutation_level(action, is_protected=override_protected_mod),
            )
            result_message: str
            result_mod_entry: NodeModEntry | None

            if action is NodeModMutationAction.ENABLE:
                if not mod.server_loadable:
                    raise _http_exception(409, f"Client-only mod cannot be enabled on the server: {mod.name}")
                require_app_stopped_for_mod_mutation(app)
                updated_mod: Mod = await manager.set_enabled(
                    mod,
                    True,
                    override_coremod=override_protected_mod,
                )
                result_message = f"Enabled {updated_mod.friendly}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is NodeModMutationAction.DISABLE:
                if not mod.server_loadable:
                    raise _http_exception(409, f"Client-only mod cannot be disabled on the server: {mod.name}")
                require_app_stopped_for_mod_mutation(app)
                updated_mod = await manager.set_enabled(
                    mod,
                    False,
                    override_coremod=override_protected_mod,
                )
                result_message = f"Disabled {updated_mod.friendly}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is NodeModMutationAction.TOGGLE_COREMOD:
                if mod.is_builtin:
                    raise _http_exception(409, "Built-in mods cannot be converted to or from coremods.")
                updated_mod = await manager.set_coremod(mod, not mod.is_coremod_type)
                coremod_text: Literal["enabled", "disabled"] = (
                    "enabled" if updated_mod.is_coremod_type else "disabled"
                )
                result_message = f"Coremod {coremod_text} for {updated_mod.friendly}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK:
                reason: ModDownloadBlockReason | None = (
                    ModDownloadBlockReason.OTHER if mod.downloadable else mod.default_download_block_reason()
                )
                updated_mod = await manager.set_download_block_reason(mod, reason)
                blocked_text: Literal["blocked from download", "download-enabled"] = (
                    "blocked from download" if updated_mod.download_block_label is not None else "download-enabled"
                )
                result_message = f"{updated_mod.friendly} is now {blocked_text}."
                result_mod_entry = self._mod_entry(updated_mod)
            elif action is NodeModMutationAction.DELETE:
                require_app_stopped_for_mod_mutation(app)
                await manager.remove(
                    mod,
                    override_coremod=override_protected_mod,
                )
                result_message = f"Deleted {mod.friendly}."
                result_mod_entry = None
            else:
                raise ValueError(f"Unsupported mod mutation action: {action}")
        except RunningAppModMutationError as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        traffic_log.info(
            "Node API mod mutated: node=%s app=%s mod=%s action=%s actor=%s",
            self.node_name,
            app.name,
            mod.name,
            action.value,
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        return NodeModMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            mod_name=mod.name,
            action=action,
            message=result_message,
            mod=result_mod_entry,
        )

    async def find_mod_pages(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: NodeModPageResolveRequest,
        actor_user_id: int,
    ) -> ModPageDiscovery:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        version = app.cfg.version
        try:
            return await discover_mod_pages(
                scope=app.scope,
                existing_mod_pages=resolve_request.mod_pages,
                local_path=mod.storage_path,
                local_filename=mod.name,
                friendly_name=mod.friendly,
                detected_version=mod.version,
                game_version=None if version is None else version.main,
                loader=None if version is None else version.loader,
                providers=resolve_request.providers,
            )
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

    @staticmethod
    def _bulk_launcher_metadata_targets(
        *,
        manager: Mod_Manager,
        discovery_request: NodeBulkLauncherMetadataRequest,
    ) -> tuple[BulkLauncherMetadataTarget, ...]:
        mods = (
            tuple(_get_mod_or_404(manager, mod_name) for mod_name in discovery_request.mod_names)
            if discovery_request.mod_names
            else tuple(manager.list_mods())
        )
        return tuple(
            BulkLauncherMetadataTarget(
                mod_name=mod.name,
                friendly_name=mod.friendly,
                local_path=mod.storage_path,
                existing_mod_pages=mod.cfg.mod_pages,
                existing_platforms=mod.cfg.platforms,
            )
            for mod in mods
            if not mod.is_builtin
            and (
                mod.cfg.platforms.modrinth is None
                or mod.cfg.platforms.curseforge is None
                or (
                    mod.cfg.platforms.modrinth is not None
                    and not any(
                        known_mod_page_provider_for_url(page.url)
                        is KnownModPageProvider.MODRINTH
                        for page in mod.cfg.mod_pages
                    )
                )
                or (
                    mod.cfg.platforms.curseforge is not None
                    and mod.cfg.platforms.curseforge.page_url is not None
                    and not any(
                        known_mod_page_provider_for_url(page.url)
                        is KnownModPageProvider.CURSEFORGE
                        for page in mod.cfg.mod_pages
                    )
                )
            )
        )

    async def discover_bulk_mod_metadata(
        self,
        *,
        app: App,
        discovery_request: NodeBulkLauncherMetadataRequest,
        actor_user_id: int,
    ) -> BulkLauncherMetadataDiscovery:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        await self._require_acl().perm_check(
            actor_user_id,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        targets = self._bulk_launcher_metadata_targets(
            manager=manager,
            discovery_request=discovery_request,
        )
        started_at = time.monotonic()
        log.info(
            "Bulk mod metadata discovery scanning: node=%s app=%s operation=%s targets=%s",
            self.node_name,
            app.name,
            discovery_request.operation_id,
            len(targets),
        )
        try:
            discovery = await discover_bulk_launcher_metadata(scope=app.scope, targets=targets)
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp
        exact_count = len(discovery.exact_entries)
        log.info(
            "Bulk mod metadata discovery completed: node=%s app=%s operation=%s exact=%s "
            "unmatched=%s provider_errors=%s elapsed=%.2fs",
            self.node_name,
            app.name,
            discovery_request.operation_id,
            exact_count,
            len(discovery.entries) - exact_count,
            len(discovery.provider_errors),
            time.monotonic() - started_at,
        )
        self._cache_bulk_metadata_discovery(
            app_name=app.name,
            operation_id=discovery_request.operation_id,
            discovery=discovery,
        )
        return discovery

    async def apply_bulk_mod_metadata(
        self,
        *,
        app: App,
        apply_request: NodeBulkLauncherMetadataApplyRequest,
        actor_user_id: int,
    ) -> NodeBulkLauncherMetadataApplyResult:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        await self._require_acl().perm_check(
            actor_user_id,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        started_at = time.monotonic()
        type_selection_names = frozenset(apply_request.apply_suggested_type_mod_names)
        discovery = self._cached_bulk_metadata_discovery(
            app_name=app.name,
            operation_id=apply_request.discovery_operation_id,
        )
        entries_by_name = {entry.mod_name: entry for entry in discovery.entries}
        selected_entries: list[BulkLauncherMetadataEntry] = []
        for mod_name in apply_request.mod_names:
            entry = entries_by_name.get(mod_name)
            if entry is None or entry.status is not BulkLauncherMetadataStatus.EXACT:
                raise _http_exception(
                    409,
                    "Bulk metadata apply selections must be exact matches from the cached discovery.",
                )
            selected_entries.append(entry)
        invalid_type_selection_names = tuple(
            entry.mod_name
            for entry in selected_entries
            if entry.mod_name in type_selection_names
            and (
                entry.suggested_mod_type is None
                or entry.suggested_mod_type is ModType.REGULAR
            )
        )
        if invalid_type_selection_names:
            raise _http_exception(
                409,
                "Bulk metadata type selections require cached non-Regular suggestions: "
                + ", ".join(invalid_type_selection_names),
            )
        log.info(
            "Bulk mod metadata apply using cached discovery: node=%s app=%s operation=%s "
            "discovery_operation=%s targets=%s type_selections=%s",
            self.node_name,
            app.name,
            apply_request.operation_id,
            apply_request.discovery_operation_id,
            len(selected_entries),
            len(type_selection_names),
        )
        applied_mod_names: list[str] = []
        applied_type_mod_names: list[str] = []
        try:
            for entry in selected_entries:
                apply_suggested_mod_type = entry.mod_name in type_selection_names
                await manager.apply_discovered_launcher_metadata(
                    entry.mod_name,
                    entry,
                    apply_suggested_mod_type=apply_suggested_mod_type,
                )
                applied_mod_names.append(entry.mod_name)
                if apply_suggested_mod_type:
                    applied_type_mod_names.append(entry.mod_name)
        except asyncio.CancelledError:
            log.warning(
                "Bulk mod metadata apply cancelled: node=%s app=%s operation=%s "
                "applied_before_cancel=%s elapsed=%.2fs",
                self.node_name,
                app.name,
                apply_request.operation_id,
                len(applied_mod_names),
                time.monotonic() - started_at,
            )
            raise
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        if applied_mod_names:
            self._invalidate_client_pack_content(app)
            self._invalidate_mod_inventory(app.name)
        traffic_log.info(
            "Node API bulk mod metadata applied: node=%s app=%s count=%s actor=%s",
            self.node_name,
            app.name,
            len(applied_mod_names),
            actor_user_id,
        )
        log.info(
            "Bulk mod metadata apply completed: node=%s app=%s operation=%s applied=%s "
            "types_updated=%s elapsed=%.2fs",
            self.node_name,
            app.name,
            apply_request.operation_id,
            len(applied_mod_names),
            len(applied_type_mod_names),
            time.monotonic() - started_at,
        )
        self._bulk_metadata_discoveries.pop(
            (app.name, apply_request.discovery_operation_id),
            None,
        )
        return NodeBulkLauncherMetadataApplyResult(
            discovery=discovery,
            applied_mod_names=tuple(applied_mod_names),
            applied_type_mod_names=tuple(applied_type_mod_names),
        )

    async def resolve_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: NodeModMetadataResolveRequest,
        actor_user_id: int,
    ) -> LauncherMetadataDiscovery:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        version = app.cfg.version
        try:
            return await discover_launcher_metadata(
                scope=app.scope,
                mod_pages=resolve_request.mod_pages,
                existing_urls=resolve_request.existing_launcher_urls,
                local_path=mod.storage_path,
                local_filename=mod.name,
                game_version=None if version is None else version.main,
                loader=None if version is None else version.loader,
                providers=resolve_request.providers,
            )
        except (OSError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

    async def fetch_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        fetch_request: NodeModMetadataFetchRequest,
        actor_user_id: int,
    ) -> LauncherMetadataResolution:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        try:
            return await resolve_launcher_metadata_resolution(
                scope=app.scope,
                urls=fetch_request.launcher_urls,
                local_filename=mod.name,
                local_path=mod.storage_path,
                providers=fetch_request.providers,
            )
        except ValueError as xcp:
            raise _http_exception(409, str(xcp)) from xcp

    async def update_mod_properties(
        self,
        *,
        app: App,
        mod_name: str,
        update: NodeModPropertiesUpdateRequest,
        actor_user_id: int,
    ) -> NodeModMutationResult:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        mod: Mod = _get_mod_or_404(manager, mod_name)
        await self._require_acl().perm_check(
            actor_user_id,
            required_mod_mutation_level(NodeModMutationAction.UPDATE_PROPERTIES),
        )
        if mod.is_builtin:
            raise _http_exception(409, "Built-in mod properties cannot be changed.")
        try:
            platforms = await resolve_launcher_metadata(
                scope=app.scope,
                urls=update.launcher_urls,
                local_filename=mod.name,
                local_path=mod.storage_path,
            )
            updated_mod = await manager.update_properties(
                mod,
                mod_type=update.mod_type,
                download_block_reason=update.download_block_reason,
                metadata_overrides=update.metadata_overrides,
                mod_pages=update.mod_pages,
                client_pack=update.client_pack or mod.cfg.client_pack,
                platforms=platforms,
            )
        except ValueError as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        traffic_log.info(
            "Node API mod properties updated: node=%s app=%s mod=%s actor=%s",
            self.node_name,
            app.name,
            mod.name,
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        self._invalidate_mod_inventory(app.name)
        return NodeModMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            mod_name=mod.name,
            action=NodeModMutationAction.UPDATE_PROPERTIES,
            message=f"Updated properties for {updated_mod.friendly}.",
            mod=self._mod_entry(updated_mod),
        )

    async def update_client_pack_config(
        self,
        *,
        app: App,
        update: NodeClientPackConfigUpdateRequest,
        actor_user_id: int,
    ) -> dict[str, object]:
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        await self._require_acl().perm_check(actor_user_id, Power_Level.admin)
        excluded_kubejs_scripts: tuple[str, ...] | None = None
        if update.metadata is not None and not isinstance(app, Minecraft):
            raise _http_exception(409, "Client-pack metadata is only supported for Minecraft apps.")
        if update.kubejs_scripts is not None:
            if not isinstance(app, Minecraft):
                raise _http_exception(409, "KubeJS client-pack scripts are only supported for Minecraft apps.")
            discovered_scripts = self._client_pack_kubejs_scripts(app)
            discovered_paths = {script.relative_path for script in discovered_scripts}
            submitted_paths = {script.relative_path for script in update.kubejs_scripts}
            if submitted_paths != discovered_paths:
                raise _http_exception(
                    409,
                    "KubeJS scripts changed since the client-pack configuration was loaded; reload and try again.",
                )
            excluded_kubejs_scripts = tuple(
                sorted(
                    (script.relative_path for script in update.kubejs_scripts if not script.included),
                    key=str.casefold,
                )
            )
        try:
            updated_mods = await manager.update_client_pack_configs(
                {item.mod_name: item.client_pack for item in update.mods}
            )
        except (ModuleNotFoundError, ValueError) as xcp:
            raise _http_exception(409, str(xcp)) from xcp

        if excluded_kubejs_scripts is not None:
            app.cfg.client_pack_excluded_kubejs_scripts = tuple(
                excluded_kubejs_scripts
            )
        if update.metadata is not None:
            app.cfg.client_pack_metadata = update.metadata

        traffic_log.info(
            "Node API client-pack configuration updated: node=%s app=%s mods=%s actor=%s",
            self.node_name,
            app.name,
            len(updated_mods),
            actor_user_id,
        )
        self._invalidate_client_pack_content(app)
        app.persist_instance_config_overrides()
        self._invalidate_mod_inventory(app.name)
        return {
            "app_name": app.name,
            "updated_count": len(updated_mods),
            "message": f"Updated client-pack configuration for {len(updated_mods)} mods.",
        }

    async def publish_client_pack_config(
        self,
        *,
        app: App,
        update: NodeClientPackPublishRequest,
        actor_user_id: int,
    ) -> dict[str, object]:
        await self._require_acl().perm_check(actor_user_id, Power_Level.admin)
        manager: Mod_Manager = app.has_mod_manager
        await manager.reload_mods()
        try:
            entries = self._client_pack_entries(
                app=app,
                selection=ClientPackSelection(),
                include_kubejs_scripts=True,
            )
        except (ClientPackValidationError, NonDownloadableModError, ModuleNotFoundError) as xcp:
            log.warning(
                "Client-pack configuration publish rejected: app=%s actor=%s error=%s",
                app.name,
                actor_user_id,
                xcp,
            )
            raise _http_exception(409, str(xcp)) from xcp
        if not entries:
            log.warning(
                "Client-pack configuration publish rejected because the pack is empty: app=%s actor=%s",
                app.name,
                actor_user_id,
            )
            raise _http_exception(409, "The default client pack contains no mods.")
        content_hash = await self._client_pack_content_hash(app=app, entries=entries)
        version = app.publish_client_pack(
            content_hash,
            changelog=update.changelog,
            mods=self._default_client_pack_mod_snapshots(app),
        )
        self._invalidate_state_caches(app_name=app.name)
        return {
            "app_name": app.name,
            "published_version": version,
            "message": f"Published client pack version {version}.",
        }

    def _resolve_console_action(self, app: App, action_key: str) -> ConsoleAction:
        if not app.supports_console_actions:
            raise _http_exception(404, f"{app.friendly} does not support console actions.")
        normalised_key: str = action_key.strip().casefold()
        if not normalised_key:
            raise _http_exception(400, "Console action key must not be empty.")
        for action in app.console_actions:
            if action.key.casefold() == normalised_key:
                return action
        raise _http_exception(404, f"Unknown console action: {action_key}")

    def _console_action_entry(
        self,
        *,
        action: ConsoleAction,
        actor_user_id: int,
        acl: Access_Control,
        runtime_running: bool,
    ) -> NodeConsoleActionEntry:
        parameter: ConsoleActionParameter[object] | None = action.parameter
        can_run: bool = acl.can(actor_user_id, action.power_level)
        return NodeConsoleActionEntry(
            key=action.key,
            label=action.label,
            description=action.description,
            power_level_name=action.power_level.name,
            power_level_label=action.power_level.name.title(),
            requires_running=action.requires_running,
            can_run=can_run,
            runtime_running=runtime_running,
            parameter=(
                self._console_action_parameter_entry(parameter, include_recent_inputs=can_run)
                if parameter is not None
                else None
            ),
        )

    @staticmethod
    def _console_action_parameter_entry(
        parameter: ConsoleActionParameter[object],
        *,
        include_recent_inputs: bool,
    ) -> NodeConsoleActionParameter:
        resolved_parameter: ConsoleActionParameter[object] = parameter
        return NodeConsoleActionParameter(
            key=resolved_parameter.key,
            label=resolved_parameter.label,
            value_type_name=resolved_parameter.value_type_name,
            description=resolved_parameter.desc,
            max_length=resolved_parameter.max_length,
            multiline=resolved_parameter.multiline,
            strict_choice=resolved_parameter.strict_choice,
            allows_text_input=resolved_parameter.choice_spec is None or not resolved_parameter.strict_choice,
            choices=tuple[NodeSettingChoice, ...](
                NodeSettingChoice(label=label, raw_value=raw_value)
                for label, raw_value in resolved_parameter.choice_items()
            ),
            recent_inputs=resolved_parameter.recent_inputs if include_recent_inputs else (),
        )

    async def _single_mod_download_file(self, *, app: App, mod: Mod) -> NodeDownloadFile:
        if not mod.path.exists():
            log.warning("Node API single mod missing: app=%s mod=%s path=%s", app.name, mod.name, mod.path)
            raise _http_exception(404, f"Mod file is missing: {mod.name}")
        if mod.path.is_file():
            return NodeDownloadFile(path=mod.path, filename=mod.name, is_archive=False)
        if mod.path.is_dir():
            archive_name: str = self._single_mod_archive_name(app=app, mod=mod)
            archive_path: Path = await compress_mod_archive_entries(
                (ModArchiveEntry.from_mod(mod),),
                archive_name,
            )
            traffic_log.info(
                "Node API zipped directory mod: app=%s mod=%s source=%s archive=%s",
                app.name,
                mod.name,
                mod.path,
                archive_path,
            )
            return NodeDownloadFile(path=archive_path, filename=archive_path.name, is_archive=True)

        log.warning("Node API single mod path is unsupported: app=%s mod=%s path=%s", app.name, mod.name, mod.path)
        raise _http_exception(404, f"Mod path is neither a file nor a directory: {mod.name}")

    def apps_url(self, *, subject: str = "web", base_url: str | None = None) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=None,
            scopes=(NodeApiScope.APPS_READ,),
        )
        return self._with_access_token(f"{self._base_url(base_url)}/apps", token)

    def ping_url(self, *, base_url: str | None = None) -> str:
        return f"{self._base_url(base_url)}/ping"

    def presence_stream_url(self, *, base_url: str | None = None) -> str:
        return f"{self._base_url(base_url)}/presence/stream"

    def map_api_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.MAP_READ,),
        )
        return self._with_access_token(f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/map", token)

    def list_mods_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_READ,),
        )
        return self._with_access_token(f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods", token)

    def mod_download_url(
        self,
        app_name: str,
        *,
        enabled_only: bool,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        query: dict[str, str] = {"enabled_only": str(enabled_only).lower()}
        return self._with_access_token(
            f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods/download?{urlencode(query)}",
            token,
        )

    def mod_download_form(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> NodeModDownloadForm:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        return NodeModDownloadForm(
            action_url=f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods/download",
            access_token=token,
        )

    def single_mod_download_url(
        self,
        app_name: str,
        mod_name: str,
        *,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        return self._with_access_token(
            f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods/{quote(mod_name, safe='')}/download",
            token,
        )

    def list_configs_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        return self._with_access_token(f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/configs", token)

    def config_file_url(
        self,
        app_name: str,
        config_id: str,
        *,
        subject: str = "web",
        writable: bool = False,
        base_url: str | None = None,
    ) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_WRITE if writable else NodeApiScope.CONFIGS_READ,),
        )
        return self._with_access_token(
            f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/configs/{quote(config_id, safe='/')}",
            token,
        )

    def config_root_download_url(
        self,
        app_name: str,
        root_id: str,
        *,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )
        return self._with_access_token(
            f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/configs/roots/{quote(root_id, safe='')}/download",
            token,
        )

    def list_saves_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_READ,),
        )
        return self._with_access_token(f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/saves", token)

    def save_download_url(
        self,
        app_name: str,
        save_id: str,
        *,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.SAVES_DOWNLOAD,),
        )
        return self._with_access_token(
            f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/saves/{quote(save_id, safe='/')}/download",
            token,
        )

    def list_settings_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.SETTINGS_READ,),
        )
        return self._with_access_token(f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/settings", token)

    def setting_url(
        self,
        app_name: str,
        setting_key: str,
        *,
        subject: str = "web",
        writable: bool = False,
        base_url: str | None = None,
    ) -> str:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.SETTINGS_WRITE if writable else NodeApiScope.SETTINGS_READ,),
        )
        return self._with_access_token(
            f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/settings/{quote(setting_key, safe='')}",
            token,
        )

    def issue_access_token(
        self,
        *,
        subject: str,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        ttl_seconds: int = _NODE_TOKEN_TTL_SECONDS,
    ) -> str | None:
        secret: str | None = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            return None
        log.debug(
            "Issuing node API access token: node=%s app=%s scopes=%s subject=%s ttl_seconds=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
            subject,
            ttl_seconds,
        )
        return issue_node_token(
            secret=secret,
            grant=NodeAccessGrant(
                subject=subject,
                node=self.node_name,
                app=app_name,
                scopes=frozenset(scopes),
                expires_at=int(time.time()) + ttl_seconds,
            ),
        )

    def _require_access(
        self,
        request: Request,
        access_token: str | None,
        *,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeAccessGrant | None:
        secret: str | None = config.MOD_WEB_SERVER.token_secret
        token_error: NodeTokenError | None = None
        if secret is not None:
            try:
                grant: NodeAccessGrant = self._verified_token_grant(
                    request=request,
                    access_token=access_token,
                    app_name=app_name,
                    scopes=scopes,
                )
            except NodeTokenError as xcp:
                token_error = xcp
            else:
                log.debug("Node API token access accepted: node=%s app=%s scopes=%s", self.node_name, app_name, scopes)
                return grant

        if self._require_web_session_access(request=request, app_name=app_name, scopes=scopes):
            return None

        if secret is None and (config.INDEV or config.ALLOW_UNAUTH_NODE_API):
            log.debug("Node API auth disabled: node=%s app=%s scopes=%s", self.node_name, app_name, scopes)
            return None

        reason: NodeTokenError = token_error or NodeTokenError("Node API authentication is not configured.")
        log.warning(
            "Node API access rejected: node=%s app=%s scopes=%s reason=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
            reason,
        )
        raise _http_exception(403, str(reason)) from token_error

    def _verified_token_grant(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> NodeAccessGrant:
        secret: str | None = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise NodeTokenError("Node token secret is not configured.")
        token: str = self._request_token(request, access_token)
        return verify_node_token(
            secret=secret,
            token=token,
            node=self.node_name,
            app=app_name,
            required_scopes=scopes,
        )

    def _request_actor_user_id(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        verified_grant: NodeAccessGrant | None = None,
    ) -> int:
        grant: NodeAccessGrant | None = verified_grant
        if grant is None and config.MOD_WEB_SERVER.token_secret is not None:
            try:
                grant = self._verified_token_grant(
                    request=request,
                    access_token=access_token,
                    app_name=app_name,
                    scopes=scopes,
                )
            except NodeTokenError:
                grant = None

        if grant is not None:
            return self._actor_user_id_from_subject(grant.subject)

        if self._web_auth is None:
            raise _http_exception(403, "Mod mutation requires an authenticated Discord user.")
        user: ModWebUser | None = self._web_auth.current_user(request)
        if user is None:
            raise _http_exception(403, "Mod mutation requires an authenticated Discord user.")
        return user.discord_id

    def _request_actor_user_id_if_available(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        verified_grant: NodeAccessGrant | None = None,
    ) -> int | None:
        if self._acl is None:
            return None
        if verified_grant is None and (self._web_auth is None or not self._web_auth.enabled):
            return None
        return self._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=scopes,
            verified_grant=verified_grant,
        )

    async def _require_actor_level_for_request(
        self,
        *,
        request: Request,
        access_token: str | None,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
        required_level: Power_Level,
        verified_grant: NodeAccessGrant | None = None,
    ) -> None:
        if self._acl is None:
            return
        if verified_grant is None and (self._web_auth is None or not self._web_auth.enabled):
            return
        actor_user_id = self._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=scopes,
            verified_grant=verified_grant,
        )
        await self._acl.perm_check(actor_user_id, required_level)

    @staticmethod
    def _actor_user_id_from_subject(subject: str) -> int:
        prefix = "web:"
        if not subject.startswith(prefix):
            raise _http_exception(403, f"Node token subject cannot act as a web user: {subject}")
        raw_user_id = subject[len(prefix) :].strip()
        if not raw_user_id.isdigit():
            raise _http_exception(403, f"Node token subject is invalid for web actions: {subject}")
        return int(raw_user_id)

    def _require_web_session_access(
        self,
        *,
        request: Request,
        app_name: str | None,
        scopes: tuple[NodeApiScope, ...],
    ) -> bool:
        if self._web_auth is None or not self._web_auth.enabled:
            return False

        if self._acl is None:
            log.warning("Node API web session auth unavailable because Access_Control is not attached.")
            raise _http_exception(503, "Mod web permissions are not available.")

        required_level = self._required_web_level(app_name=app_name, scopes=scopes)
        user = self._web_auth.current_user(request)
        if user is None:
            raise _http_exception(401, "Discord login is required.")
        if not self._acl.can(user.discord_id, required_level):
            raise _http_exception(
                403,
                f"Insufficient level: {self._acl.level_of(user.discord_id).name.title()} < {required_level.name.title()}",
            )
        log.debug(
            "Node API web session access accepted: node=%s app=%s scopes=%s user_id=%s",
            self.node_name,
            app_name,
            ",".join(scope.value for scope in scopes),
            user.discord_id,
        )
        return True

    def _required_web_level(self, *, app_name: str | None, scopes: tuple[NodeApiScope, ...]) -> Power_Level:
        if not scopes:
            raise _http_exception(403, "Node API access requires at least one scope.")
        required_levels: list[Power_Level] = []
        for scope in scopes:
            level = _NODE_API_SCOPE_WEB_LEVELS.get(scope)
            if level is None:
                raise _http_exception(403, f"Node API scope cannot be granted by a web session: {scope.value}.")
            required_levels.append(level)
        return max(required_levels)

    def _resolve_app(self, app_name: str) -> App:
        manager = self._require_manager()
        try:
            log.debug("Node API resolving app: node=%s app=%s", self.node_name, app_name)
            return manager.get(app_name)
        except Exception as xcp:
            log.warning("Node API app not found: node=%s app=%s", self.node_name, app_name)
            raise _http_exception(404, f"Unknown app: {app_name}") from xcp

    def _should_log_missing_route_warning(self, missing_path: str) -> bool:
        app_name = self._missing_route_app_name(missing_path)
        if app_name is None or self._manager is None:
            return True
        app = self._manager.apps.get(app_name)
        if app is None:
            return True
        return app.check_running()

    @staticmethod
    def _missing_route_app_name(missing_path: str) -> str | None:
        parts = tuple(part for part in missing_path.split("/") if part)
        if len(parts) < 2 or parts[0] != "apps":
            return None
        return parts[1]

    def _require_manager(self) -> App_Manager:
        if self._manager is None:
            raise _http_exception(503, "App manager is not available yet.")
        return self._manager

    def _require_acl(self) -> Access_Control:
        if self._acl is None:
            raise _http_exception(503, "Mod web permissions are not available.")
        return self._acl

    @staticmethod
    def _validated_upload_filename(filename: str, *, kind: str) -> str:
        resolved = filename.strip()
        if not resolved:
            raise _http_exception(400, f"{kind} upload filename is required.")
        if resolved in {".", ".."} or PurePosixPath(resolved).name != resolved or "\\" in resolved:
            raise _http_exception(400, f"{kind} upload filename must not include directories.")
        return resolved

    def _resolve_mod_upload_requests(
        self,
        *,
        uploads: Sequence[UploadFile],
        upload_names: Sequence[str] | None,
    ) -> tuple[_ResolvedModUploadFile, ...]:
        if not uploads:
            raise _http_exception(400, "At least one mod upload is required.")
        if upload_names is not None and len(upload_names) != len(uploads):
            raise _http_exception(400, "Mod upload filenames must match the number of uploads.")
        resolved_uploads: list[_ResolvedModUploadFile] = []
        for index, upload in enumerate(uploads):
            resolved_upload_name = self._validated_upload_filename(
                (upload.filename or "") if upload_names is None else upload_names[index],
                kind="Mod",
            )
            resolved_uploads.append(
                _ResolvedModUploadFile(
                    upload=upload,
                    upload_name=resolved_upload_name,
                )
            )
        return tuple(resolved_uploads)

    def _validated_mod_upload_sources(
        self,
        upload_sources: Sequence[NodeModUploadSource],
    ) -> tuple[NodeModUploadSource, ...]:
        if not upload_sources:
            raise _http_exception(400, "At least one mod upload is required.")
        resolved_sources: list[NodeModUploadSource] = []
        for upload_source in upload_sources:
            resolved_sources.append(
                NodeModUploadSource(
                    source_path=upload_source.source_path,
                    upload_name=self._validated_upload_filename(upload_source.upload_name, kind="Mod"),
                )
            )
        return tuple(resolved_sources)

    @staticmethod
    def _mod_upload_message(*, app: App, mods: Sequence[NodeModEntry]) -> str:
        if len(mods) == 1:
            return f"Uploaded mod `{mods[0].friendly}` for {app.friendly}."
        return f"Uploaded {len(mods)} mods for {app.friendly}."

    @staticmethod
    def _single_mod_upload_result(result: NodeModUploadBatchResult) -> NodeModUploadResult:
        if len(result.mods) != 1:
            raise ValueError("Exactly one uploaded mod is required.")
        return NodeModUploadResult(
            app_name=result.app_name,
            app_friendly=result.app_friendly,
            node=result.node,
            message=result.message,
            mod=result.mods[0],
        )

    @staticmethod
    async def _persist_upload_to_temp(upload: UploadFile) -> Path:
        suffix = Path(upload.filename or "save-upload").suffix
        with tempfile.NamedTemporaryFile(prefix="yukibot-save-", suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            while chunk := await upload.read(1024 * 1024):
                handle.write(chunk)
        await upload.close()
        return temp_path

    def _running_blocker_name(self, app: App) -> str | None:
        manager = self._require_manager()
        blocker = manager.start_blocker(app, include_current_activity=False)
        friendly_name = blocker.blocking_app_friendly if blocker is not None else None
        if blocker is None or not isinstance(friendly_name, str) or not friendly_name.strip():
            return None
        return friendly_name

    @staticmethod
    def _settings_for_app(app: App) -> tuple[Setting[object], ...]:
        settings_manager = NodeApiService._require_settings_manager(app)
        return tuple(cast(Sequence[Setting[object]], settings_manager.app.options))

    @staticmethod
    def _require_settings_manager(app: App) -> Settings_Manager:
        settings_manager = app.settings
        if settings_manager is None:
            raise _http_exception(404, f"{app.friendly} does not support settings.")
        return settings_manager

    def _resolve_setting(self, *, app: App, setting_key: str) -> Setting[object]:
        setting = self._setting_lookup(app).get(setting_key.casefold())
        if setting is None:
            raise _http_exception(404, f"Unknown setting: {setting_key}")
        return setting

    @classmethod
    def _setting_lookup(cls, app: App) -> dict[str, Setting[object]]:
        lookup: dict[str, Setting[object]] = {}
        for setting in cls._settings_for_app(app):
            lookup[setting.key.casefold()] = setting
        return lookup

    @staticmethod
    def _setting_type_name(setting: Setting[object]) -> str:
        return setting.type_name

    @staticmethod
    def _setting_current_input_value(
        setting: Setting[object],
        *,
        can_edit: bool,
        settings_manager: Settings_Manager,
        actor_user_id: int,
    ) -> str:
        if setting.do_hide is not None and (not can_edit or setting.value_type is not bool):
            return ""
        return settings_manager.current_input_value(setting, actor_user_id)

    @staticmethod
    def _setting_recent_inputs(setting: Setting[object]) -> tuple[str, ...]:
        if not setting.supports_recent_inputs:
            return ()
        return setting.recent_inputs

    @staticmethod
    def _setting_allows_text_input(setting: Setting[object]) -> bool:
        return not setting.choices or not setting.strict_choice

    @staticmethod
    def _setting_label_text(
        setting: Setting[object],
        value: object,
    ) -> str:
        choice_label = setting.spec.choice_label_for_value(value)
        if choice_label is not None:
            return choice_label
        return setting.spec.display_value(value)

    @classmethod
    def _setting_default_text(cls, setting: Setting[object]) -> str:
        if setting.do_hide is not None:
            return ""
        return cls._setting_label_text(setting, setting.default)

    @classmethod
    def _setting_value_text(
        cls,
        setting: Setting[object],
        *,
        can_reveal: bool,
        settings_manager: Settings_Manager,
        actor_user_id: int,
    ) -> str:
        if setting.do_hide is None:
            return settings_manager.label_text(setting, actor_user_id)
        if setting.is_sensitive:
            return "REDACTED"
        if can_reveal:
            return "Hidden"
        return f"Hidden (requires {setting.do_hide.name.title()})"

    @classmethod
    def _setting_revealed_value_text(
        cls,
        setting: Setting[object],
        *,
        can_reveal: bool,
        settings_manager: Settings_Manager,
        actor_user_id: int,
    ) -> str:
        if setting.do_hide is None or not can_reveal:
            return ""
        return settings_manager.label_text(setting, actor_user_id)

    def _setting_entry(
        self,
        setting: Setting[object],
        *,
        acl: Access_Control,
        actor_user_id: int,
        settings_manager: Settings_Manager,
    ) -> NodeSettingEntry:
        can_edit = acl.can(actor_user_id, setting.power_level)
        reveal_level = setting.do_hide
        can_reveal = reveal_level is not None and acl.can(actor_user_id, reveal_level)
        value_is_hidden = reveal_level is not None
        choices = tuple(
            NodeSettingChoice(label=label, raw_value=raw_value) for label, raw_value in setting.choice_items()
        )
        return NodeSettingEntry(
            key=setting.key,
            label=setting.label,
            type_name=self._setting_type_name(setting),
            permission_level=setting.power_level.name.title(),
            permission_level_name=setting.power_level.name,
            default_text=self._setting_default_text(setting),
            description=setting.desc,
            paragraph=setting.paragraph,
            is_sensitive=setting.is_sensitive,
            value_text=self._setting_value_text(
                setting,
                can_reveal=can_reveal,
                settings_manager=settings_manager,
                actor_user_id=actor_user_id,
            ),
            revealed_value_text=self._setting_revealed_value_text(
                setting,
                can_reveal=can_reveal,
                settings_manager=settings_manager,
                actor_user_id=actor_user_id,
            ),
            current_input_value=self._setting_current_input_value(
                setting,
                can_edit=can_edit,
                settings_manager=settings_manager,
                actor_user_id=actor_user_id,
            ),
            has_pending_value=settings_manager.has_pending_value(actor_user_id, setting),
            can_edit=can_edit,
            value_is_hidden=value_is_hidden,
            can_reveal_hidden_text=can_reveal,
            allows_text_input=self._setting_allows_text_input(setting),
            allows_blank_input=setting.allows_blank_input,
            strict_choice=setting.strict_choice,
            choices=choices,
            recent_inputs=self._setting_recent_inputs(setting) if can_edit else (),
        )

    @staticmethod
    def _mod_entry(mod: Mod) -> NodeModEntry:
        size_bytes = File_Utils.pointer_size(mod.path)
        return NodeModEntry(
            name=mod.name,
            friendly=mod.friendly,
            client_path=str(mod.client_path),
            enabled=mod.cfg.enabled,
            placement=mod.cfg.placement,
            server_loadable=mod.server_loadable,
            client_pack_eligible=mod.client_pack_eligible,
            archive_name=mod.logical_archive_name,
            source_path=str(mod.storage_path),
            description=mod.description,
            mod_type=mod.mod_type,
            coremod=mod.is_coremod_type,
            downloadable=mod.downloadable,
            download_block_reason=mod.download_block_reason.value
            if mod.download_block_reason is not None
            else None,
            download_block_label=mod.download_block_label,
            origin=mod.origin,
            version=mod.version,
            added=mod.added.isoformat(sep=" ", timespec="seconds"),
            size_bytes=size_bytes,
            size_text=Utilities.humanise_bytes(size_bytes),
            mod_pages=mod.cfg.mod_pages,
            metadata_overrides=mod.cfg.metadata_overrides,
            client_pack=mod.cfg.client_pack,
            platforms=mod.cfg.platforms,
        )

    @staticmethod
    def _config_entry(config_file: AppConfigFile) -> NodeConfigEntry:
        return NodeConfigEntry(
            id=config_file.id,
            label=config_file.label,
            relative_path=config_file.relative_path,
            root_id=config_file.root_id,
            root_label=config_file.root_label,
            kind=config_file.kind.value,
            read_power_level=config_file.read_power_level,
            write_power_level=config_file.write_power_level,
            size_bytes=config_file.size_bytes,
            size_text=Utilities.humanise_bytes(config_file.size_bytes),
            modified_at=config_file.modified_at.isoformat(sep=" ", timespec="seconds"),
        )

    def _config_content(self, *, app: App, content: AppConfigFileContent) -> NodeConfigContent:
        return NodeConfigContent(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            config=self._config_entry(content.file),
            content=content.content,
        )

    @staticmethod
    def _save_entry(save_file: AppSaveEntry, *, can_delete: bool = False) -> NodeSaveEntry:
        size_bytes = save_file.size_bytes
        if save_file.kind is AppSaveEntryKind.DIRECTORY:
            size_text = "Directory"
        else:
            size_text = Utilities.humanise_bytes(size_bytes)
        return NodeSaveEntry(
            id=save_file.id,
            label=save_file.label,
            relative_path=save_file.relative_path,
            root_id=save_file.root_id,
            root_label=save_file.root_label,
            kind=save_file.kind.value,
            size_bytes=size_bytes,
            size_text=size_text,
            modified_at=save_file.modified_at.isoformat(sep=" ", timespec="seconds"),
            can_delete=can_delete,
        )

    def _blueprint_file_entry(
        self,
        blueprint_file: AppBlueprintFileEntry,
        *,
        actor_user_id: int,
    ) -> NodeBlueprintFileEntry:
        uploaded_by_user_id: int | None = blueprint_file.uploaded_by_user_id
        uploaded_by_display_name: str | None = None
        if uploaded_by_user_id is not None:
            uploaded_by_display_name = config.Name_Cache().cached_display_name(
                uploaded_by_user_id,
                f"User {uploaded_by_user_id}",
            )
        can_delete: bool = uploaded_by_user_id == actor_user_id
        if not can_delete and self._acl is not None:
            can_delete = self._acl.can(actor_user_id, Power_Level.sudo)
        return NodeBlueprintFileEntry(
            id=blueprint_file.id,
            label=blueprint_file.label,
            relative_path=blueprint_file.relative_path,
            size_bytes=blueprint_file.size_bytes,
            size_text=Utilities.humanise_bytes(blueprint_file.size_bytes),
            modified_at=blueprint_file.modified_at.isoformat(sep=" ", timespec="seconds"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=can_delete,
        )

    def _blueprint_entry(self, blueprint_file: AppBlueprintEntry, *, actor_user_id: int) -> NodeBlueprintEntry:
        main_file = self._blueprint_file_entry(
            AppBlueprintFileEntry(
                id=blueprint_file.id,
                label=blueprint_file.label,
                relative_path=blueprint_file.relative_path,
                size_bytes=blueprint_file.size_bytes,
                modified_at=blueprint_file.modified_at,
                uploaded_by_user_id=blueprint_file.uploaded_by_user_id,
            ),
            actor_user_id=actor_user_id,
        )
        config_file = (
            self._blueprint_file_entry(blueprint_file.config_file, actor_user_id=actor_user_id)
            if blueprint_file.config_file is not None
            else None
        )
        return NodeBlueprintEntry(
            id=main_file.id,
            label=main_file.label,
            session_name=blueprint_file.session_name,
            relative_path=main_file.relative_path,
            size_bytes=main_file.size_bytes,
            size_text=main_file.size_text,
            modified_at=main_file.modified_at,
            uploaded_by_display_name=main_file.uploaded_by_display_name,
            can_delete=main_file.can_delete and (config_file is None or config_file.can_delete),
            config_file=config_file,
        )

    def _archive_name(
        self,
        *,
        app: App,
        entries: tuple[ArchiveEntry | ArchiveDataEntry, ...],
        request: NodeDownloadRequest,
        client_pack_version: str | None = None,
    ) -> str:
        if request.resolved_pack_purpose is PackPurpose.CLIENT:
            metadata = self._client_pack_metadata(app)
            version = app.cfg.version
            if metadata is not None and version is not None:
                pack_version = (
                    client_pack_version
                    or app.cfg.client_pack_published_version
                    or version.main
                )
                format_name = {
                    PackFormat.MODRINTH: "modrinth",
                    PackFormat.CURSEFORGE: "curseforge",
                    PackFormat.GENERIC_ZIP: "generic",
                }[request.pack_format]
                stem = metadata.filename_stem(
                    app_name=app.name,
                    version=pack_version,
                    minecraft_version=version.main,
                    format_name=format_name,
                )
                return f"{stem}{request.pack_format.suffix}"
        if request.resolved_pack_purpose is not None:
            suffix = f"{request.resolved_pack_purpose.value}_pack"
        elif request.selected_only or request.mod_names:
            suffix = "selected_mods" if len(entries) != 1 else "selected_mod"
        elif request.enabled_only:
            suffix = "enabled_mods" if len(entries) != 1 else "enabled_mod"
        else:
            suffix = "mods"
        app_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in app.friendly.strip())
        base_name = app_name.strip("_") or app.name
        if request.resolved_pack_purpose is not None and request.pack_format is not PackFormat.GENERIC_ZIP:
            suffix = f"{suffix}_{request.pack_format.value}"
        return f"{base_name}_{suffix}{request.pack_format.suffix}"

    @staticmethod
    def _empty_archive_detail(request: NodeDownloadRequest) -> str:
        if request.resolved_pack_purpose is PackPurpose.CLIENT:
            return "No client-pack-eligible mods found."
        if request.resolved_pack_purpose is PackPurpose.SERVER:
            return "No enabled server-pack mods found."
        if request.resolved_pack_purpose is PackPurpose.ADMIN:
            return "No admin-pack mods found."
        if request.selected_only or request.mod_names:
            return "No selected downloadable mods found."
        if request.enabled_only:
            return "No enabled downloadable mods found."
        return "No downloadable mods found."

    @staticmethod
    def _single_mod_archive_name(*, app: App, mod: Mod) -> str:
        app_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in app.friendly.strip())
        mod_name = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in mod.friendly.strip())
        base_app_name = app_name.strip("_") or app.name
        base_mod_name = mod_name.strip("_") or mod.name
        return f"{base_app_name}_{base_mod_name}.zip"

    @staticmethod
    def _save_archive_name(*, app: App, save_path: Path) -> str:
        app_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in app.friendly.strip())
        save_name = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_" for char in save_path.name.strip()
        )
        base_app_name = app_name.strip("_") or app.name
        base_save_name = save_name.strip("_") or save_path.name
        return f"{base_app_name}_{base_save_name}.zip"

    @staticmethod
    def _config_root_archive_name(*, app: App, root: AppConfigFileRoot) -> str:
        app_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in app.friendly.strip())
        root_name = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in root.id.strip())
        base_app_name = app_name.strip("_") or app.name
        base_root_name = root_name.strip("_") or root.id
        return f"{base_app_name}_{base_root_name}_configs.zip"

    @staticmethod
    def _request_token(request: Any, access_token: str | None) -> str:
        if access_token:
            return access_token

        auth_header = request.headers.get("authorization", "")
        scheme, _, token = auth_header.partition(" ")
        if scheme.casefold() == "bearer" and token:
            return token.strip()
        return ""

    @staticmethod
    def _with_access_token(url: str, token: str | None) -> str:
        if token is None:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode({'access_token': token})}"

    def _base_url(self, base_url: str | None) -> str:
        return (base_url or self.api_base_url).rstrip("/")


def _http_exception(status_code: int, detail: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=detail)
