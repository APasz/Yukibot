from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import psutil
import requests
from fastapi import (
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
from starlette.middleware.cors import CORSMiddleware

import apps._node_api as app_node_api
import apps.factorio.node_api as factorio_node_api
import apps.minecraft.node_api as minecraft_node_api
import apps.satisfactory.node_api as satisfactory_node_api
import apps.sevendays.node_api as sevendays_node_api
import config
import node_api_app_operations
import node_api_app_installer
import node_api_app_state
import node_api_client_pack
import node_api_mod
import node_api_mod_service
import node_api_relay
import node_api_storage_service
import node_api_system
from _async_utils import run_blocking
from _audit import audit_log
from _file import File_Utils
from _manager import App_Manager, app_scope_from_name
from _security import Access_Control, Power_Level
from _sys import Stats_System, StatsDiskSnapshot, StatsSystemSnapshot
from _utils import Utilities
from apps._app import App
from apps._config import (
    AppTitleFont,
    BulkLauncherMetadataDiscovery,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    ModPageDiscovery,
    ModPlacement,
)
from apps._console import ConsoleAction
from apps._save_files import AppSaveEntry, AppSaveEntryKind
from apps.factorio.node_api import (
    NodeFactorioGenerationState,
    NodeFactorioGenerationUpdateRequest,
    NodeFactorioMapExchangeImportRequest,
    NodeFactorioMapExchangeString,
    NodeFactorioModSettings,
)
from apps.minecraft import (
    Minecraft,
    MinecraftRecipeMutation,
)
from apps.minecraft.node_api import (
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationRequest,
    NodeMinecraftRecipeMutationResult,
    NodeMinecraftRecipeWorkspaceState,
)
from apps.satisfactory.node_api import (
    NodeBlueprintList,
    NodeBlueprintMutationResult,
)
from apps.sevendays import SevenDays
from apps.sevendays.node_api import (
    NodeSevenDaysSandboxOptionsState,
)
from chat_hub import (
    ChatEndpoint,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    ChatHub,
    ChatRoomUpdate,
)
from deployment_metadata import DeploymentMetadata
from font_assets import font_assets
from maintenance import MaintenanceService
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
from node_api_app_routes import register_app_routes
from node_api_app_installer_routes import register_app_installer_routes
from node_api_app_installer import (
    NodeAppInstallCatalog,
    NodeAppInstallRequest,
    NodeAppInstallScopeOption,
    NodeAppInstallStatus,
    NodeAppInstallerSettingsMutationResult,
    NodeAppInstallerSettingsState,
)
from node_api_app_state import (
    _ALL_NODE_STATE_TOPICS,
    NodeAppActivityProviderEntry,
    NodeAppEntry,
    NodeAppFootprintSnapshot,
    NodeAppResourcePointSummary,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeStateStreamEvent,
    NodeStateTopic,
    _NodeAppPlayerSnapshot,
    _TimedNodeSystemSummary,
)
from node_api_chat import (
    NodeChatEndpointSummary,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    NodeWebChatRequest,
)
from node_api_chat_routes import register_chat_routes
from node_api_console import (
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleStdoutSnapshot,
    NodeConsoleStdoutStreamEvent,
    NodeConsoleStdoutStreamEventKind,
)
from node_api_console_routes import register_console_routes
from node_api_core_routes import register_core_routes
from node_api_files import (
    NodeConfigContent,
    NodeConfigList,
    NodeConfigMutationResult,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSaveRootEntry,
    NodeSaveUploadTransport,
)
from node_api_map_routes import register_map_routes
from node_api_mod_routes import register_mod_routes
from node_api_node_routes import register_node_management_routes
from node_api_relay import (
    RelayTTSQueue,
)
from node_api_route_contracts import (
    NODE_DISCORD_HEARTBEAT_LATENCY_HEADER,
    NODE_DISCORD_SERVICE_STATE_HEADER,
    DiscordHealthSnapshot,
    DiscordServiceState,
)
from node_api_settings import (
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
)
from node_api_settings_routes import register_settings_routes
from node_api_storage_routes import register_storage_routes
from node_api_system import (
    NodeRestartRecord,
    NodeRestartScheduleEntry,
    NodeRestartScheduleState,
    NodeRestartState,
    NodeSystemAction,
    NodeSystemActionHandler,
    NodeSystemActionResult,
    NodeSystemCapabilities,
    NodeSystemDiskSummary,
    NodeSystemHistory,
    NodeSystemLogCatalog,
    NodeSystemLogEntry,
    NodeSystemLogTail,
    NodeSystemSample,
    NodeSystemSummary,
)
from node_api_system_routes import register_system_routes
from node_api_upload import NodeApiRequestBodyLimitMiddleware, persist_upload_to_temp
from node_auth import (
    NodeAccessGrant,
    NodeApiScope,
    NodeTokenError,
    issue_node_token,
    verify_node_token,
)
from restart_state import (
    read_process_restart_record,
    read_voice_restart_record,
)
from restart_targets import RestartTarget

if TYPE_CHECKING:
    from _manager import App_Manager
    from apps.factorio.node_api import (
        NodeModDependencyResolutionResult,
        NodeModPortalVersionList,
        NodeModUpdateCheckResult,
    )

_NODE_API_PREFIX = "/api/node"
_NODE_TOKEN_TTL_SECONDS = 15 * 60
_NODE_RESTART_DELAY_SECONDS = 0.25
_APP_PLAYER_COUNT_TIMEOUT_SECONDS = 1.5
_PORTAL_NODE_LATENCY_TIMEOUT_SECONDS = 4.0
_APP_FOOTPRINT_CACHE_TTL_SECONDS = 60.0
_APP_TRANSITION_TTL_SECONDS = 15.0
_NODE_APP_ENTRY_CACHE_TTL_SECONDS = 5.0
_NODE_SYSTEM_SUMMARY_CACHE_TTL_SECONDS = 1.0
_LIVE_APP_RUNTIME_CACHE_TTL_SECONDS = 0.5
_FULL_APP_RUNTIME_CACHE_TTL_SECONDS = 2.0
_LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS = 0.75
_LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS = 2.0
_NODE_SYSTEM_HISTORY_MAX_SAMPLES = node_api_system.SYSTEM_HISTORY_RETENTION_SECONDS // int(
    node_api_system.SYSTEM_HISTORY_INTERVAL_SECONDS
)
_NODE_SYSTEM_LOG_MAX_LINES = 500
_MAX_PRESENCE_STREAM_CONNECTIONS = 64
_MAX_PRESENCE_STREAM_MESSAGES_PER_MINUTE = 24


@dataclass(frozen=True, slots=True)
class PortalNodeLatencyProbe:
    """Portal's current HTTP and Discord latency observations for one node."""

    latency_ms: int | None
    discord_latency_ms: int | None
    discord_service_state: DiscordServiceState | None


_LOCAL_CONSOLE_STDOUT_STREAM_INTERVAL_SECONDS = 0.5
_NODE_CHAT_HISTORY_LIMIT = 100
_SQUAREMAP_REQUEST_TIMEOUT_SECONDS = 10.0
_NODE_API_SCOPE_WEB_LEVELS: dict[NodeApiScope, Power_Level] = {
    NodeApiScope.APPS_READ: Power_Level.visitor,
    NodeApiScope.MAP_READ: Power_Level.visitor,
    NodeApiScope.MAP_WRITE: Power_Level.user,
    NodeApiScope.CHAT_READ: Power_Level.visitor,
    NodeApiScope.CHAT_WRITE: Power_Level.visitor,
    NodeApiScope.CHAT_INJECT: Power_Level.root,
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


def _is_executor_shutdown_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and "cannot schedule new futures after shutdown" in str(error)


def _normalised_auth_url(raw: str) -> str:
    text = raw.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.rstrip("/").casefold()
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


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
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
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
            mountpoint=app_node_api.required_string(payload, "mountpoint"),
            display_name=app_node_api.required_string(payload, "display_name"),
            is_activity=app_node_api.required_bool(payload, "is_activity"),
            is_primary=app_node_api.required_bool(payload, "is_primary"),
            is_secondary=app_node_api.required_bool(payload, "is_secondary"),
            is_bot_disk=app_node_api.required_bool(payload, "is_bot_disk"),
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
            node=app_node_api.required_string(payload, "node"),
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
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
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
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
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
            node=app_node_api.required_string(payload, "node"),
            message=app_node_api.required_string(payload, "message"),
            settings=config.DiscordSettings.model_validate(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class _SquaremapProxyResponse:
    content: bytes
    media_type: str | None
    headers: tuple[tuple[str, str], ...] = ()
    is_stale: bool = False
    cache_updated_at_unix_ms: int | None = None


_BulkMetadataOperationResult = TypeVar("_BulkMetadataOperationResult")


class NodeApiService:
    def __init__(self) -> None:
        self._manager: App_Manager | None = None
        self._discord_health_lock = threading.RLock()
        self._discord_service_state: DiscordServiceState | None = None
        self._discord_health: DiscordHealthSnapshot | None = None
        self._chat_relay: WebChatRelayPublisher | None = None
        self._relay_tts_service: RelayTTSQueue | None = None
        self._acl: Access_Control | None = None
        self._web_auth: ModWebAuthService | None = None
        self._system_action_handler: NodeSystemActionHandler | None = None
        self._maintenance_service: MaintenanceService | None = None
        self._maintenance_restart_targets: tuple[RestartTarget, ...] = ()
        self._pending_system_action: NodeSystemAction | None = None
        self._system_action_lock = threading.RLock()
        self._app_footprint_cache: dict[str, NodeAppFootprintSnapshot] = {}
        self._app_state_cache = node_api_app_state.NodeAppStateCache(
            app_entry_ttl_seconds=_NODE_APP_ENTRY_CACHE_TTL_SECONDS,
            live_runtime_ttl_seconds=_LIVE_APP_RUNTIME_CACHE_TTL_SECONDS,
            full_runtime_ttl_seconds=_FULL_APP_RUNTIME_CACHE_TTL_SECONDS,
        )
        self._system_summary_cache: _TimedNodeSystemSummary | None = None
        self._system_summary_cache_lock = threading.RLock()
        self._system_history: deque[NodeSystemSample] = deque(maxlen=_NODE_SYSTEM_HISTORY_MAX_SAMPLES)
        self._system_history_lock = threading.RLock()
        self._presence_stream_connection_count = 0
        self._presence_stream_connection_lock = threading.Lock()
        self._system_history_task: asyncio.Task[None] | None = None
        self._app_mutations = node_api_app_state.NodeAppMutationService(
            node_name=lambda: self.node_name,
            invalidate_state_caches=lambda app_name: self._invalidate_state_caches(app_name=app_name),
            build_runtime_summary=lambda app: self.build_app_runtime_summary(app),
            build_live_runtime_summary=lambda app: self.build_live_app_runtime_summary(app),
            transition_ttl_seconds=_APP_TRANSITION_TTL_SECONDS,
        )
        self._app_state_subscriptions = node_api_app_state.NodeAppStateSubscriptionService(
            node_name=lambda: self.node_name,
            is_shutting_down=lambda: self._shutting_down,
            resolve_app=lambda app_name: self._resolve_app(app_name),
            build_live_runtime_summary=lambda app: self.build_live_app_runtime_summary(app),
            list_apps=lambda: self.list_apps(),
            build_system_summary=lambda: self.build_system_summary(),
            stream_system_summary=self._stream_system_summary,
            discord_health=lambda: self._discord_service_health()[1],
            app_runtime_interval_seconds=_LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS,
            node_state_interval_seconds=_LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS,
        )
        self._client_packs = node_api_client_pack.NodeClientPackService(
            node_name=lambda: self.node_name,
            invalidate_app_state=lambda app_name: self._invalidate_state_caches(app_name=app_name),
            invalidate_mod_inventory=self._invalidate_mod_inventory,
        )
        self._app_operations = node_api_app_operations.NodeAppOperationsService(
            node_name=lambda: self.node_name,
            require_acl=self._require_acl,
            http_exception=_http_exception,
            runtime_http_exception=self._runtime_http_exception,
            traffic_log=traffic_log,
        )
        self._app_installer = node_api_app_installer.NodeAppInstallerService(
            node_name=lambda: self.node_name,
            invalidate_state_caches=self._invalidate_state_caches,
            scope_policy=lambda: self._require_manager().app_installer_settings(),
        )
        self._factorio = factorio_node_api.FactorioNodeApiService(
            node_name=lambda: self.node_name,
            invalidate_app_state=lambda app_name: self._invalidate_state_caches(app_name=app_name),
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )
        self._satisfactory_blueprints = satisfactory_node_api.SatisfactoryBlueprintService(
            node_name=lambda: self.node_name,
            can_sudo=lambda actor_user_id: self._acl is not None and self._acl.can(actor_user_id, Power_Level.sudo),
            require_sudo=lambda actor_user_id: self._require_acl().can(actor_user_id, Power_Level.sudo),
            display_name_for_user=lambda user_id: config.Name_Cache().cached_display_name(
                user_id,
                f"User {user_id}",
            ),
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )
        self._storage = node_api_storage_service.NodeStorageService(
            node_name=lambda: self.node_name,
            current_acl=lambda: self._acl,
            invalidate_client_pack_content=self._invalidate_client_pack_content,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )
        self._mod_service = node_api_mod_service.NodeModService(
            node_name=lambda: self.node_name,
            require_acl=self._require_acl,
            build_runtime_summary=lambda app: self.build_cached_app_runtime_summary(app),
            invalidate_client_pack_content=self._invalidate_client_pack_content,
            invalidate_mod_inventory=self._invalidate_mod_inventory,
            upload_mod_paths=self._upload_mod_paths_for_mod_service,
        )
        self._routes_registered = False
        self._shutting_down = False

    @property
    def node_name(self) -> str:
        return config.MOD_WEB_SERVER.node_name

    @property
    def api_base_url(self) -> str:
        return config.MOD_WEB_SERVER.node_api_base_url

    def set_manager(self, manager: App_Manager) -> None:
        self._manager = manager
        self._invalidate_state_caches()

    def set_discord_service_state(self, state: DiscordServiceState | None) -> None:
        """Record command-service availability without affecting node liveness."""

        with self._discord_health_lock:
            self._discord_service_state = state
            self._discord_health = None

    def set_discord_health(self, health: DiscordHealthSnapshot) -> None:
        """Record a detailed Discord health observation for diagnostic clients."""

        with self._discord_health_lock:
            self._discord_service_state = health.service_state
            self._discord_health = health

    def _discord_service_health(self) -> tuple[DiscordServiceState | None, DiscordHealthSnapshot | None]:
        with self._discord_health_lock:
            return (self._discord_service_state, self._discord_health)

    def _invalidate_state_caches(self, *, app_name: str | None = None) -> None:
        self._app_state_cache.invalidate(app_name)
        with self._system_summary_cache_lock:
            self._system_summary_cache = None

    def _invalidate_mod_inventory(self, app_name: str) -> None:
        self._mod_service.invalidate_inventory(app_name)

    async def _upload_mod_paths_for_mod_service(
        self,
        *,
        app: App,
        upload_sources: Sequence[app_node_api.NodeModUploadSource],
        actor_user_id: int,
        placement: ModPlacement,
    ) -> node_api_mod.NodeModUploadBatchResult:
        return await self.upload_mod_paths(
            app=app,
            upload_sources=upload_sources,
            actor_user_id=actor_user_id,
            placement=placement,
        )

    def set_acl(self, acl: Access_Control) -> None:
        self._acl = acl

    def set_web_auth(self, web_auth: ModWebAuthService) -> None:
        self._web_auth = web_auth

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
        self._app_mutations.cancel_pending()
        self._app_installer.cancel_pending()
        self._app_state_subscriptions.close()
        if history_task is not None:
            history_task.cancel()

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
                await asyncio.sleep(node_api_system.SYSTEM_HISTORY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    def register_routes(self, nicegui_app: Any) -> None:
        if self._routes_registered:
            return

        nicegui_app.add_middleware(
            NodeApiRequestBodyLimitMiddleware,
            max_bytes=config.NODE_API_UPLOAD_MAX_BYTES,
            api_prefix=_NODE_API_PREFIX,
        )
        nicegui_app.add_middleware(
            CORSMiddleware,
            allow_origins=("*",),
            allow_methods=("GET", "POST"),
            allow_headers=("Authorization",),
            expose_headers=(
                NODE_DISCORD_HEARTBEAT_LATENCY_HEADER,
                NODE_DISCORD_SERVICE_STATE_HEADER,
            ),
        )

        register_core_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            traffic_log=traffic_log,
        )

        register_system_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            max_log_lines=_NODE_SYSTEM_LOG_MAX_LINES,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )

        register_node_management_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )

        register_chat_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            history_limit=_NODE_CHAT_HISTORY_LIMIT,
            traffic_log=traffic_log,
        )

        register_app_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )

        register_app_installer_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )

        register_map_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            traffic_log=traffic_log,
        )

        register_mod_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            traffic_log=traffic_log,
        )

        register_storage_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )

        register_settings_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            traffic_log=traffic_log,
        )

        register_console_routes(
            nicegui_app,
            service=self,
            api_prefix=_NODE_API_PREFIX,
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )

        @nicegui_app.get(f"{_NODE_API_PREFIX}/{{missing_path:path}}")
        async def _missing_node_api_route(missing_path: str) -> dict[str, object]:
            if self._should_log_missing_route_warning(missing_path):
                log.warning(
                    "Node API route not found: /%s/%s",
                    _NODE_API_PREFIX.strip("/"),
                    missing_path,
                )
            raise _http_exception(
                404,
                f"Unknown node API route: /{_NODE_API_PREFIX.strip('/')}/{missing_path}",
            )

        self._routes_registered = True

    async def list_apps(self) -> tuple[NodeAppEntry, ...]:
        return await self._app_state_cache.list_entries(
            build_entries=lambda: self._build_app_entries(),
            node_name=self.node_name,
        )

    async def _build_app_entries(self) -> tuple[NodeAppEntry, ...]:
        manager = self._manager
        if manager is None:
            if config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL:
                return ()
            raise _http_exception(503, "App manager is not available yet.")
        apps = tuple(sorted(manager.apps.values(), key=lambda item: item.friendly.casefold()))
        pending_entries = await asyncio.gather(
            *(self._build_live_app_entry(app) for app in apps),
            return_exceptions=True,
        )
        entries: list[NodeAppEntry] = []
        for app, entry_or_error in zip(apps, pending_entries, strict=True):
            if manager.apps.get(app.name) is not app:
                continue
            if isinstance(entry_or_error, BaseException):
                raise entry_or_error
            entries.append(entry_or_error)
        return tuple(entries)

    async def build_live_app_entry(self, app: App) -> NodeAppEntry:
        return await self._app_state_cache.app_entry(app, build_entry=lambda app: self._build_live_app_entry(app))

    async def _build_live_app_entry(self, app: App) -> NodeAppEntry:
        player_snapshot = await self._app_player_snapshot(app)
        return self.build_app_entry(
            app,
            transition_state=self._app_mutations.transition_state(app.name),
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
                self._app_mutations.transition_state(app.name) if transition_state is None else transition_state
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
            client_pack_kubejs_scripts=self._client_packs.kubejs_scripts(app),
            client_pack_metadata=self._client_packs.metadata(app),
            client_pack_file_previews=self._client_packs.file_previews(app),
            client_pack_automated_changelog=self._client_packs.automated_changelog(app),
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
        return minecraft_node_api.build_minecraft_recipe_workspace_state(app)

    def build_sevendays_sandbox_options_state(self, app: App) -> NodeSevenDaysSandboxOptionsState:
        if not isinstance(app, SevenDays):
            raise _http_exception(404, f"App {app.name!r} does not expose 7D2D sandbox options.")
        return sevendays_node_api.build_sevendays_sandbox_options_state(app)

    def build_minecraft_item_icon_response(self, app: App, *, item_id: str) -> Response:
        if not isinstance(app, Minecraft):
            raise _http_exception(404, f"App {app.name!r} does not expose Minecraft recipe item icons.")
        try:
            return minecraft_node_api.build_minecraft_item_icon_response(app, item_id=item_id)
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp

    @staticmethod
    def minecraft_item_icon_placeholder_svg(item_id: str) -> str:
        return minecraft_node_api.minecraft_item_icon_placeholder_svg(item_id)

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
            minecraft_node_api.apply_minecraft_recipe_mutation(
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

    @staticmethod
    def app_color_hex(color: int | None) -> str | None:
        if color is None:
            return None
        if color < 0 or color > 0xFFFFFF:
            raise ValueError(f"App colour must be between 0x000000 and 0xFFFFFF, got {color!r}.")
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
                elif elapsed < node_api_system.SYSTEM_HISTORY_INTERVAL_SECONDS:
                    return
            self._system_history.append(sample)

    def build_system_history(self) -> NodeSystemHistory:
        with self._system_history_lock:
            samples = tuple(self._system_history)
        return NodeSystemHistory(
            retention_seconds=node_api_system.SYSTEM_HISTORY_RETENTION_SECONDS,
            sample_interval_seconds=int(node_api_system.SYSTEM_HISTORY_INTERVAL_SECONDS),
            samples=samples,
        )

    @staticmethod
    def _read_log_tail(*, path: Path, max_lines: int) -> tuple[tuple[str, ...], bool]:
        if max_lines < 1:
            raise ValueError("Log tail line limit must be at least 1.")
        with path.open("rb") as handle:
            handle.seek(0, 2)
            file_size = handle.tell()
            if file_size <= 0:
                return (), False

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

        lines = tuple(
            deque(
                buffer.decode(config.STR_ENCODE, errors="replace").splitlines(),
                maxlen=max_lines,
            )
        )
        return lines, position > 0 or newline_count > max_lines

    @staticmethod
    def _system_log_entries_with_paths() -> tuple[tuple[NodeSystemLogEntry, Path], ...]:
        log_root = config.DIR_LOG.resolve(strict=False)
        if not log_root.exists():
            return ()
        if not log_root.is_dir():
            raise RuntimeError(f"System log directory is not a directory: {log_root}")

        candidates = sorted(
            (candidate for candidate in log_root.rglob("*") if candidate.is_file()),
            key=lambda candidate: (
                len(candidate.relative_to(log_root).parts),
                candidate.as_posix().casefold(),
            ),
        )
        entries_by_target: dict[Path, tuple[NodeSystemLogEntry, Path]] = {}
        for candidate in candidates:
            relative_path = candidate.relative_to(log_root).as_posix()
            try:
                target_path = candidate.resolve(strict=True)
                if not target_path.is_relative_to(log_root):
                    log.warning("Skipping system log outside log directory: path=%s", candidate)
                    continue
                stat = target_path.stat()
            except OSError as xcp:
                log.warning("Skipping unreadable system log: path=%s error=%s", candidate, xcp)
                continue
            entries_by_target.setdefault(
                target_path,
                (
                    NodeSystemLogEntry(
                        relative_path=relative_path,
                        size_bytes=stat.st_size,
                        modified_at_epoch_seconds=max(0, int(stat.st_mtime)),
                    ),
                    target_path,
                ),
            )
        return tuple(
            sorted(
                entries_by_target.values(),
                key=lambda item: item[0].relative_path.casefold(),
            )
        )

    def build_system_log_catalog(self) -> NodeSystemLogCatalog:
        return NodeSystemLogCatalog(
            node=self.node_name,
            entries=tuple(entry for entry, _path in self._system_log_entries_with_paths()),
        )

    def build_system_log_tail(self, *, log_path: str, max_lines: int = 200) -> NodeSystemLogTail:
        if max_lines < 1 or max_lines > _NODE_SYSTEM_LOG_MAX_LINES:
            raise ValueError(f"System log line limit must be between 1 and {_NODE_SYSTEM_LOG_MAX_LINES}.")
        path_by_relative_path = {
            entry.relative_path: (entry, path) for entry, path in self._system_log_entries_with_paths()
        }
        resolved = path_by_relative_path.get(log_path)
        if resolved is None:
            raise _http_exception(404, "Unknown system log.")
        entry, path = resolved
        try:
            lines, truncated = self._read_log_tail(path=path, max_lines=max_lines)
        except OSError as xcp:
            raise _http_exception(500, f"System log read failed: {xcp}") from xcp
        return NodeSystemLogTail(
            node=self.node_name,
            entry=entry,
            lines=lines,
            truncated=truncated,
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
        deployment_metadata: DeploymentMetadata | None = config.MOD_WEB_DEPLOYMENT_METADATA
        deployment_version: str | None = (
            deployment_metadata.version
            if deployment_metadata is not None
            else "indev"
            if config.INDEV
            else None
        )
        deployment_revision: str | None = (
            config.MOD_WEB_BUILD_SHA if deployment_metadata is None else deployment_metadata.revision
        )
        deployed_at_epoch_seconds: int | None = (
            None if deployment_metadata is None else int(deployment_metadata.deployed_at.timestamp())
        )

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
            log.warning(
                "Node API bot uptime probe failed: node=%s error=%s",
                self.node_name,
                xcp,
            )
        try:
            uptime_seconds = max(0, int(time.time() - psutil.boot_time()))
        except Exception as xcp:
            log.warning("Node API uptime probe failed: node=%s error=%s", self.node_name, xcp)
        if self._manager is not None:
            try:
                capacity = self._manager.node_capacity()
                usage = self._manager.active_resource_point_usage()
            except Exception as xcp:
                log.warning(
                    "Node API resource point summary failed: node=%s error=%s",
                    self.node_name,
                    xcp,
                )
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
                for app in sorted(
                    self._manager.apps.values(),
                    key=lambda item: item.friendly.casefold(),
                )
                if app.check_running()
            )
            running_names = tuple(app_friendly for _app_name, app_friendly, _app_scope in running_apps)
            running_app_ids = tuple(app_name for app_name, _app_friendly, _app_scope in running_apps)
            running_app_scopes = tuple(app_scope for _app_name, _app_friendly, app_scope in running_apps)
            start_blocked_app_ids = tuple(
                app.name
                for app in sorted(
                    self._manager.apps.values(),
                    key=lambda item: item.friendly.casefold(),
                )
                if not app.check_running()
                and self._manager.start_blocker(app, include_current_activity=False) is not None
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
            deployment_version=deployment_version,
            deployment_revision=deployment_revision,
            deployed_at_epoch_seconds=deployed_at_epoch_seconds,
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
        except TypeError, ValueError:
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

    async def publish_app_fake_chat(self, *, app: App, event: ChatEvent) -> ChatEvent:
        self._require_chat_relay_app(app)
        if event.room_id.casefold() != app.name.casefold():
            raise _http_exception(400, "Synthetic chat event room does not match the selected app.")
        if self._chat_relay is None:
            raise _http_exception(503, "Chat relay is not available on this node.")
        return await self._chat_relay.publish_chat_event(event=event)

    async def queue_relay_tts(
        self, relay_request: node_api_relay.NodeRelayTTSRequest
    ) -> node_api_relay.NodeRelayTTSResult:
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
            return node_api_relay.NodeRelayTTSResult(queued=False, reason=reason)

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
        return node_api_relay.NodeRelayTTSResult(queued=True, spoken=spoken, queue_size=queue_size)

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
        transition_state = self._app_mutations.transition_state(app.name)

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
                log.warning(
                    "Node API storage stats failed: node=%s app=%s error=%s",
                    self.node_name,
                    app.name,
                    xcp,
                )
            else:
                if storage_disk is not None:
                    storage_percent = storage_disk.percent
                    storage_free_bytes = storage_disk.free_bytes
                    storage_total_bytes = storage_disk.total_bytes
        if include_footprint and not config.IS_SHUTTINGDOWN and not self._shutting_down:
            try:
                footprint_bytes = await run_blocking(self._app_footprint_size_bytes, app)
            except Exception as xcp:
                if not (config.IS_SHUTTINGDOWN and _is_executor_shutdown_error(xcp)):
                    log.warning(
                        "Node API footprint stats failed: node=%s app=%s error=%s",
                        self.node_name,
                        app.name,
                        xcp,
                    )
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
            log.warning(
                "Node API activity provider snapshot failed: node=%s app=%s error=%s",
                self.node_name,
                app.name,
                xcp,
            )

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
            version_source=app.version_source,
            storage_percent=storage_percent,
            storage_free_bytes=storage_free_bytes,
            storage_total_bytes=storage_total_bytes,
            footprint_bytes=footprint_bytes,
            runtime_fault=getattr(app, "runtime_fault", None),
            connected_player_names=connected_player_names,
            activity_providers=activity_providers,
        )

    async def _build_runtime_summary_for_state_cache(
        self,
        app: App,
        *,
        include_storage: bool = True,
        include_footprint: bool = True,
    ) -> NodeAppRuntimeSummary:
        if include_storage and include_footprint:
            return await self.build_app_runtime_summary(app)
        return await self.build_app_runtime_summary(
            app,
            include_storage=include_storage,
            include_footprint=include_footprint,
        )

    async def build_cached_app_runtime_summary(self, app: App) -> NodeAppRuntimeSummary:
        return await self._app_state_cache.full_runtime_summary(
            app,
            build_summary=self._build_runtime_summary_for_state_cache,
        )

    async def build_live_app_runtime_summary(self, app: App) -> NodeAppRuntimeSummary:
        return await self._app_state_cache.live_runtime_summary(
            app,
            build_summary=self._build_runtime_summary_for_state_cache,
        )

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
    def _squaremap_passthrough_headers(
        response: requests.Response,
    ) -> tuple[tuple[str, str], ...]:
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

    def _remember_squaremap_cache_entry(
        self, app: App, relative_path: str, proxy_response: _SquaremapProxyResponse
    ) -> None:
        if not self._should_cache_squaremap_path(relative_path):
            return
        try:
            content_text = proxy_response.content.decode("utf-8")
        except UnicodeDecodeError:
            log.warning(
                "Skipping map cache write for %s because %s was not UTF-8 JSON.",
                app.friendly,
                relative_path,
            )
            return
        try:
            self._map_json_cache_store(app).save_entry(
                relative_path=relative_path,
                content=content_text,
                media_type=proxy_response.media_type,
                headers=proxy_response.headers,
            )
        except ValueError:
            log.exception(
                "Failed to update map cache for %s at %s",
                app.friendly,
                app.map_cache_path,
            )

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
    def _squaremap_world_summaries(
        payload: Mapping[str, object],
    ) -> tuple[MapWorldSummary, ...]:
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
        return tuple(
            sorted(
                worlds,
                key=lambda world: (
                    world.order,
                    world.display_name.casefold(),
                    world.name.casefold(),
                ),
            )
        )

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
        return self._app_state_subscriptions.subscribe_app_runtime(
            app_name,
            callback,
            include_update_state=include_update_state,
        )

    def subscribe_local_node_state(
        self,
        callback: Callable[[NodeStateStreamEvent], None],
        *,
        topics: frozenset[NodeStateTopic] = _ALL_NODE_STATE_TOPICS,
    ) -> Callable[[], None]:
        return self._app_state_subscriptions.subscribe_node_state(callback, topics=topics)

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
            if event.kind in {
                NodeChatStreamEventKind.INITIAL,
                NodeChatStreamEventKind.RUNTIME_CHANGED,
            }:
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
        if not self._try_reserve_presence_stream_connection():
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="Presence stream capacity reached.")
            return
        await websocket.accept()
        message_times: deque[float] = deque()
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                now = time.monotonic()
                while message_times and now - message_times[0] >= 60.0:
                    message_times.popleft()
                if len(message_times) >= _MAX_PRESENCE_STREAM_MESSAGES_PER_MINUTE:
                    await websocket.close(
                        code=status.WS_1008_POLICY_VIOLATION,
                        reason="Presence stream message rate exceeded.",
                    )
                    return
                message_times.append(now)
                sample_id: str | None = None
                payload: Mapping[str, object] | None = None
                payload_text = message.get("text")
                if isinstance(payload_text, str) and payload_text:
                    try:
                        raw_payload = json.loads(payload_text)
                    except ValueError:
                        raw_payload = None
                    if isinstance(raw_payload, Mapping):
                        payload = cast(Mapping[str, object], raw_payload)
                        raw_sample_id = payload.get("sample_id")
                        if raw_sample_id is not None:
                            sample_id = str(raw_sample_id)
                response: dict[str, object] = {
                    "type": "pong",
                    "node": self.node_name,
                    "sample_id": sample_id,
                }
                discord_service_state, _ = self._discord_service_health()
                if discord_service_state is not None:
                    response["discord_service_state"] = discord_service_state.value
                discord_latency_ms = self._discord_heartbeat_latency_ms()
                if discord_latency_ms is not None:
                    response["discord_latency_ms"] = discord_latency_ms
                await websocket.send_json(response)
        except WebSocketDisconnect:
            return
        finally:
            await self._close_websocket_quietly(websocket)
            self._release_presence_stream_connection()

    def _try_reserve_presence_stream_connection(self) -> bool:
        with self._presence_stream_connection_lock:
            if self._presence_stream_connection_count >= _MAX_PRESENCE_STREAM_CONNECTIONS:
                return False
            self._presence_stream_connection_count += 1
            return True

    def _release_presence_stream_connection(self) -> None:
        with self._presence_stream_connection_lock:
            if self._presence_stream_connection_count <= 0:
                raise RuntimeError("Presence stream connection count underflow.")
            self._presence_stream_connection_count -= 1

    def _discord_heartbeat_latency_ms(self) -> int | None:
        if config.ACTIVE_BOT_PROFILE.name not in {
            config.BotProfileName.YUKI,
            config.BotProfileName.ERIN,
        }:
            return None
        manager = self._manager
        if manager is None or manager.bot is None:
            return None
        latency_seconds = manager.bot.heartbeat_latency
        if not math.isfinite(latency_seconds):
            return None
        return round(latency_seconds * 1000)

    def _node_ping_headers(self) -> dict[str, str]:
        """Return non-sensitive node liveness metadata for the public ping route."""

        headers: dict[str, str] = {}
        discord_service_state, _ = self._discord_service_health()
        if discord_service_state is not None:
            headers[NODE_DISCORD_SERVICE_STATE_HEADER] = discord_service_state.value
        discord_latency_ms = self._discord_heartbeat_latency_ms()
        if discord_latency_ms is not None:
            headers[NODE_DISCORD_HEARTBEAT_LATENCY_HEADER] = str(discord_latency_ms)
        return headers

    async def portal_node_latency_probes_async(self) -> dict[str, PortalNodeLatencyProbe]:
        """Measure the Portal-to-node and node-to-Discord latency for dashboard badges."""

        if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.PORTAL:
            return {}
        targets = self._portal_node_latency_targets()
        measurements = await asyncio.gather(
            *(run_blocking(self._measure_node_latency_probe, ping_url) for _, ping_url in targets)
        )
        return {node_name: measurement for (node_name, _), measurement in zip(targets, measurements, strict=True)}

    async def portal_node_latencies_async(self) -> dict[str, int | None]:
        probes = await self.portal_node_latency_probes_async()
        return {node_name: probe.latency_ms for node_name, probe in probes.items()}

    @staticmethod
    def _portal_node_latency_targets() -> tuple[tuple[str, str], ...]:
        targets: dict[str, tuple[str, str]] = {}
        for snapshot in config.load_known_bot_snapshots():
            if snapshot.profile.bot_profile not in {
                config.BotProfileName.YUKI,
                config.BotProfileName.ERIN,
            }:
                continue
            mod_web = snapshot.features.mod_web
            if mod_web is None:
                continue
            node_name = mod_web.node_name
            targets.setdefault(
                node_name.casefold(),
                (node_name, f"{mod_web.node_api_base_url.rstrip('/')}/ping"),
            )
        return tuple(targets.values())

    @staticmethod
    def _measure_node_latency_probe(ping_url: str) -> PortalNodeLatencyProbe:
        started_at = time.perf_counter()
        try:
            response = requests.get(ping_url, timeout=_PORTAL_NODE_LATENCY_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException:
            return PortalNodeLatencyProbe(
                latency_ms=None,
                discord_latency_ms=None,
                discord_service_state=None,
            )
        raw_discord_service_state = response.headers.get(NODE_DISCORD_SERVICE_STATE_HEADER)
        try:
            discord_service_state = (
                DiscordServiceState(raw_discord_service_state) if raw_discord_service_state is not None else None
            )
        except ValueError:
            discord_service_state = None
        raw_discord_latency_ms = response.headers.get(NODE_DISCORD_HEARTBEAT_LATENCY_HEADER)
        try:
            discord_latency_ms = int(raw_discord_latency_ms) if raw_discord_latency_ms is not None else None
        except ValueError:
            discord_latency_ms = None
        if discord_latency_ms is not None and discord_latency_ms < 0:
            discord_latency_ms = None
        return PortalNodeLatencyProbe(
            latency_ms=max(1, round((time.perf_counter() - started_at) * 1000)),
            discord_latency_ms=discord_latency_ms,
            discord_service_state=discord_service_state,
        )

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
                    discord_health=self._discord_service_health()[1],
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
            health_changed=first.health_changed or second.health_changed,
            app_entries=second.app_entries if second.app_entries is not None else first.app_entries,
            system_summary=second.system_summary if second.system_summary is not None else first.system_summary,
            discord_health=second.discord_health if second.health_changed else first.discord_health,
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
            log.debug(
                "Node API player count timed out: node=%s app=%s",
                self.node_name,
                app.name,
            )
        except Exception as xcp:
            log.warning(
                "Node API player count failed: node=%s app=%s error=%s",
                self.node_name,
                app.name,
                xcp,
            )
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
        action: node_api_app_state.NodeAppMutationAction,
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
    ) -> node_api_app_state.NodeAppMutationResult:
        result = await self._app_mutations.mutate(
            manager=self._require_manager(),
            acl=self._require_acl(),
            app=app,
            action=action,
            actor_user_id=actor_user_id,
            http_exception=_http_exception,
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
            update_branch_id=update_branch_id,
        )
        if action is node_api_app_state.NodeAppMutationAction.DELETE:
            self._invalidate_mod_inventory(app.name)
            audit_log(
                "node.app.deleted",
                actor_user_id=actor_user_id,
                node=self.node_name,
                app_name=app.name,
                scope=app.scope,
            )
        return result

    def build_app_install_catalog(self) -> NodeAppInstallCatalog:
        self._require_app_installer_available()
        return self._app_installer.build_catalog(manager=self._require_manager())

    async def start_app_install(
        self,
        *,
        request: NodeAppInstallRequest,
        actor_user_id: int,
    ) -> NodeAppInstallStatus:
        self._require_app_installer_available()
        return await self._app_installer.start_install(
            manager=self._require_manager(),
            acl=self._require_acl(),
            actor_user_id=actor_user_id,
            request=request,
        )

    def app_install_status(self, *, job_id: str) -> NodeAppInstallStatus:
        self._require_app_installer_available()
        return self._app_installer.install_status(job_id=job_id)

    def read_app_installer_settings(self) -> NodeAppInstallerSettingsState:
        if not self.system_capabilities().supports_app_installer_settings:
            raise _http_exception(400, "App installer settings are unavailable on this node.")
        manager = self._require_manager()
        return NodeAppInstallerSettingsState(
            node=self.node_name,
            settings=manager.app_installer_settings(),
            available_apps=tuple(
                NodeAppInstallScopeOption(scope=recipe.scope, label=recipe.label)
                for recipe in manager.list_steam_install_recipes()
            ),
        )

    def _require_app_installer_available(self) -> None:
        if config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL:
            raise _http_exception(400, "App installation is unavailable on portal nodes.")

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
                    is_primary=(primary_disk is not None and disk.mountpoint_text == primary_disk.mountpoint_text),
                    is_secondary=(
                        secondary_disk is not None and disk.mountpoint_text == secondary_disk.mountpoint_text
                    ),
                    is_bot_disk=(bot_disk is not None and disk.mountpoint_text == bot_disk.mountpoint_text),
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
            raise _http_exception(400, f"Node action {action.value!r} is unavailable on this node.")
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
                log.exception(
                    "Node system action dispatch failed: node=%s action=%s",
                    self.node_name,
                    action.value,
                )
                return

        asyncio.get_running_loop().call_later(_NODE_RESTART_DELAY_SECONDS, _dispatch)
        action_label = node_api_system.SYSTEM_ACTION_LABELS[action]
        return NodeSystemActionResult(
            node=self.node_name,
            action=action,
            message=f"Scheduled {action_label} for {self.node_name}.",
        )

    def system_capabilities(self) -> NodeSystemCapabilities:
        manager = self._manager
        is_portal = config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.PORTAL
        supports_app_auto_restart = not is_portal and manager is not None
        supports_silent_restart = not is_portal and manager is not None and manager.bot is not None
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
        if not maintenance.reload():
            raise _http_exception(503, "Restart scheduling configuration is temporarily unavailable.")
        return self._restart_schedule_state(maintenance)

    def _restart_schedule_state(self, maintenance: MaintenanceService) -> NodeRestartScheduleState:
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
        return self._restart_schedule_state(maintenance)

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
        return self._restart_schedule_state(maintenance)

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

    async def mutate_app_installer_settings(
        self,
        *,
        settings: config.AppInstallerSettings,
        actor_user_id: int,
    ) -> NodeAppInstallerSettingsMutationResult:
        await self._require_acl().perm_check(actor_user_id, Power_Level.root)
        if not self.system_capabilities().supports_app_installer_settings:
            raise _http_exception(400, "App installer settings are unavailable on this node.")
        updated_settings = self._require_manager().set_app_installer_settings(settings)
        audit_log(
            "node.app_installer_settings.updated",
            actor_user_id=actor_user_id,
            node=self.node_name,
            allowed_scopes=updated_settings.allowed_scopes,
        )
        return NodeAppInstallerSettingsMutationResult(
            node=self.node_name,
            message=f"Updated app install settings for {self.node_name}.",
            settings=updated_settings,
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
        current_settings = self.read_discord_settings()
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

    async def build_mod_download_response(
        self,
        *,
        app: App,
        request: node_api_mod.NodeDownloadRequest,
    ) -> FileResponse:
        return await self._client_packs.build_mod_download_response(
            app=app,
            request=request,
            http_exception=_http_exception,
        )

    async def run_bulk_metadata_operation(
        self,
        *,
        app_name: str,
        operation_id: uuid.UUID,
        action: Callable[[], Awaitable[_BulkMetadataOperationResult]],
    ) -> _BulkMetadataOperationResult:
        return await self._mod_service.run_bulk_metadata_operation(
            app_name=app_name,
            operation_id=operation_id,
            action=action,
        )

    def cancel_bulk_metadata_operation(self, *, app_name: str, operation_id: uuid.UUID) -> bool:
        return self._mod_service.cancel_bulk_metadata_operation(
            app_name=app_name,
            operation_id=operation_id,
        )

    async def build_mod_list(self, app: App) -> node_api_mod.NodeModList:
        return await self._mod_service.build_mod_list(app)

    async def upload_mod_file(
        self,
        *,
        app: App,
        upload: UploadFile,
        upload_name: str | None,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> node_api_mod.NodeModUploadResult:
        return await self._mod_service.upload_mod_file(
            app=app,
            upload=upload,
            upload_name=upload_name,
            actor_user_id=actor_user_id,
            placement=placement,
        )

    async def upload_mod_files(
        self,
        *,
        app: App,
        uploads: Sequence[UploadFile],
        upload_names: Sequence[str] | None,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> node_api_mod.NodeModUploadBatchResult:
        return await self._mod_service.upload_mod_files(
            app=app,
            uploads=uploads,
            upload_names=upload_names,
            actor_user_id=actor_user_id,
            placement=placement,
        )

    async def upload_mod_path(
        self,
        *,
        app: App,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> node_api_mod.NodeModUploadResult:
        return await self._mod_service.upload_mod_path(
            app=app,
            source_path=source_path,
            upload_name=upload_name,
            actor_user_id=actor_user_id,
            placement=placement,
        )

    async def install_mod_from_link(
        self,
        *,
        app: App,
        url: str,
        actor_user_id: int,
        selected_mod_ids: Sequence[str] | None = None,
        version: str | None = None,
    ) -> node_api_mod.NodeModUploadBatchResult:
        return await self._mod_service.install_mod_from_link(
            app=app,
            url=url,
            actor_user_id=actor_user_id,
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
        return await self._mod_service.resolve_mod_link_dependencies(app=app, url=url, version=version)

    async def list_mod_link_versions(self, *, app: App, url: str) -> NodeModPortalVersionList:
        return await self._mod_service.list_mod_link_versions(app=app, url=url)

    async def list_installed_mod_versions(self, *, app: App, mod_name: str) -> NodeModPortalVersionList:
        return await self._mod_service.list_installed_mod_versions(app=app, mod_name=mod_name)

    async def check_mod_update(
        self,
        *,
        app: App,
        mod_name: str,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        return await self._mod_service.check_mod_update(app=app, mod_name=mod_name, version=version)

    async def update_mod(
        self,
        *,
        app: App,
        mod_name: str,
        actor_user_id: int,
        version: str | None = None,
    ) -> node_api_mod.NodeModUploadBatchResult:
        return await self._mod_service.update_mod(
            app=app,
            mod_name=mod_name,
            actor_user_id=actor_user_id,
            version=version,
        )

    async def mutate_mod(
        self,
        *,
        app: App,
        mod_name: str,
        action: node_api_mod.NodeModMutationAction,
        actor_user_id: int,
    ) -> node_api_mod.NodeModMutationResult:
        return await self._mod_service.mutate_mod(
            app=app,
            mod_name=mod_name,
            action=action,
            actor_user_id=actor_user_id,
        )

    async def find_mod_pages(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: node_api_mod.NodeModPageResolveRequest,
        actor_user_id: int,
    ) -> ModPageDiscovery:
        return await self._mod_service.find_mod_pages(
            app=app,
            mod_name=mod_name,
            resolve_request=resolve_request,
            actor_user_id=actor_user_id,
        )

    async def discover_bulk_mod_metadata(
        self,
        *,
        app: App,
        discovery_request: node_api_mod.NodeBulkLauncherMetadataRequest,
        actor_user_id: int,
    ) -> BulkLauncherMetadataDiscovery:
        return await self._mod_service.discover_bulk_mod_metadata(
            app=app,
            discovery_request=discovery_request,
            actor_user_id=actor_user_id,
        )

    async def apply_bulk_mod_metadata(
        self,
        *,
        app: App,
        apply_request: node_api_mod.NodeBulkLauncherMetadataApplyRequest,
        actor_user_id: int,
    ) -> node_api_mod.NodeBulkLauncherMetadataApplyResult:
        return await self._mod_service.apply_bulk_mod_metadata(
            app=app,
            apply_request=apply_request,
            actor_user_id=actor_user_id,
        )

    async def resolve_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        resolve_request: node_api_mod.NodeModMetadataResolveRequest,
        actor_user_id: int,
    ) -> LauncherMetadataDiscovery:
        return await self._mod_service.resolve_mod_launcher_metadata(
            app=app,
            mod_name=mod_name,
            resolve_request=resolve_request,
            actor_user_id=actor_user_id,
        )

    async def fetch_mod_launcher_metadata(
        self,
        *,
        app: App,
        mod_name: str,
        fetch_request: node_api_mod.NodeModMetadataFetchRequest,
        actor_user_id: int,
    ) -> LauncherMetadataResolution:
        return await self._mod_service.fetch_mod_launcher_metadata(
            app=app,
            mod_name=mod_name,
            fetch_request=fetch_request,
            actor_user_id=actor_user_id,
        )

    async def update_mod_properties(
        self,
        *,
        app: App,
        mod_name: str,
        update: node_api_mod.NodeModPropertiesUpdateRequest,
        actor_user_id: int,
    ) -> node_api_mod.NodeModMutationResult:
        return await self._mod_service.update_mod_properties(
            app=app,
            mod_name=mod_name,
            update=update,
            actor_user_id=actor_user_id,
        )

    async def update_mod_notes(
        self,
        *,
        app: App,
        mod_name: str,
        notes: str | None,
        actor_user_id: int,
    ) -> node_api_mod.NodeModMutationResult:
        return await self._mod_service.update_mod_notes(
            app=app,
            mod_name=mod_name,
            notes=notes,
            actor_user_id=actor_user_id,
        )

    async def upload_mod_paths(
        self,
        *,
        app: App,
        upload_sources: Sequence[app_node_api.NodeModUploadSource],
        actor_user_id: int,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> node_api_mod.NodeModUploadBatchResult:
        return await self._mod_service.upload_mod_paths(
            app=app,
            upload_sources=upload_sources,
            actor_user_id=actor_user_id,
            placement=placement,
        )

    def build_config_list(self, app: App, *, actor_user_id: int | None = None) -> NodeConfigList:
        return self._storage.build_config_list(app=app, actor_user_id=actor_user_id)

    def read_config_file(self, *, app: App, config_id: str) -> NodeConfigContent:
        return self._storage.read_config_file(app=app, config_id=config_id)

    def write_config_file(self, *, app: App, config_id: str, content: str) -> NodeConfigContent:
        return self._storage.write_config_file(app=app, config_id=config_id, content=content)

    def create_config_file(
        self,
        *,
        app: App,
        root_id: str,
        relative_path: str,
        content: str,
    ) -> NodeConfigContent:
        return self._storage.create_config_file(
            app=app,
            root_id=root_id,
            relative_path=relative_path,
            content=content,
        )

    def delete_config_file(self, *, app: App, config_id: str) -> NodeConfigMutationResult:
        return self._storage.delete_config_file(app=app, config_id=config_id)

    def factorio_generation_state(self, *, app: App) -> NodeFactorioGenerationState:
        return self._factorio.generation_state(app=app)

    def update_factorio_generation(
        self,
        *,
        app: App,
        update: NodeFactorioGenerationUpdateRequest,
    ) -> NodeFactorioGenerationState:
        return self._factorio.update_generation(app=app, update=update)

    async def import_factorio_map_exchange_string(
        self,
        *,
        app: App,
        import_request: NodeFactorioMapExchangeImportRequest,
    ) -> NodeFactorioGenerationState:
        return await self._factorio.import_map_exchange_string(app=app, import_request=import_request)

    async def sync_factorio_generation_from_running_world(self, *, app: App) -> NodeFactorioGenerationState:
        return await self._factorio.sync_generation_from_running_world(app=app)

    async def export_factorio_map_exchange_string(self, *, app: App) -> NodeFactorioMapExchangeString:
        return await self._factorio.export_map_exchange_string(app=app)

    def factorio_mod_settings_state(self, *, app: App) -> NodeFactorioModSettings:
        return self._factorio.mod_settings_state(app=app)

    def build_factorio_mod_settings_download_response(self, *, app: App) -> FileResponse:
        return self._factorio.mod_settings_download_response(app=app)

    async def upload_factorio_mod_settings(
        self,
        *,
        app: App,
        upload: UploadFile,
        upload_name: str,
        actor_user_id: int,
    ) -> NodeFactorioModSettings:
        del actor_user_id
        return await self._factorio.upload_mod_settings(
            app=app,
            upload=upload,
            upload_name=upload_name,
        )

    def delete_factorio_mod_settings(self, *, app: App) -> NodeFactorioModSettings:
        return self._factorio.delete_mod_settings(app=app)

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
        return await self._storage.build_config_root_download_response(
            app=app,
            root_id=root_id,
            actor_user_id=actor_user_id,
        )

    async def build_save_list(self, app: App) -> NodeSaveList:
        saves: tuple[AppSaveEntry, ...] = await app.list_save_files_async()
        save_can_delete: bool = bool(getattr(app, "supports_save_delete", False))
        traffic_log.info(
            "Node API built save list: node=%s app=%s saves=%s",
            self.node_name,
            app.name,
            len(saves),
        )
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
            custom_archive: tuple[str, Path] | None = await app.download_save_archive(save_id)
        except FileNotFoundError as xcp:
            raise _http_exception(404, str(xcp)) from xcp
        except ValueError as xcp:
            raise _http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise self._runtime_http_exception(app=app, action="Save download", error=xcp) from xcp
        except Exception as xcp:
            raise _http_exception(500, f"Save download failed: {xcp}") from xcp
        if custom_archive is not None:
            filename, archive_path = custom_archive
            if not archive_path.is_file():
                raise _http_exception(404, f"Save archive does not exist: {archive_path.name}")
            traffic_log.info(
                "Node API sending custom save archive: node=%s app=%s save=%s archive=%s",
                self.node_name,
                app.name,
                save_id,
                archive_path,
            )
            return FileResponse(path=archive_path, filename=filename)

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
            traffic_log.info(
                "Node API sending save file: node=%s app=%s path=%s",
                self.node_name,
                app.name,
                save_path,
            )
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
        upload_transport: NodeSaveUploadTransport = NodeSaveUploadTransport.DIRECT,
    ) -> NodeSaveMutationResult:
        if not app.supports_save_uploads:
            raise _http_exception(409, f"{app.friendly} does not support save uploads.")
        resolved_upload_name: str = (upload_name or upload.filename or "").strip()
        if not resolved_upload_name:
            raise _http_exception(400, "Save upload filename is required.")

        temp_path: Path = await persist_upload_to_temp(upload)
        try:
            return await self.upload_save_path(
                app=app,
                root_id=root_id,
                source_path=temp_path,
                upload_name=resolved_upload_name,
                actor_user_id=actor_user_id,
                upload_transport=upload_transport,
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
        upload_transport: NodeSaveUploadTransport = NodeSaveUploadTransport.DIRECT,
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
            "Node API save uploaded: node=%s app=%s root=%s save=%s actor=%s transport=%s",
            self.node_name,
            app.name,
            root_id,
            updated.id,
            actor_user_id,
            upload_transport.value,
        )
        return NodeSaveMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node=self.node_name,
            message=f"Uploaded save `{updated.label}` for {app.friendly}.",
            save=self._save_entry(updated, can_delete=save_can_delete),
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
        return self._satisfactory_blueprints.build_list(app=app, actor_user_id=actor_user_id)

    def build_empty_blueprint_list(self, app: App) -> NodeBlueprintList:
        return self._satisfactory_blueprints.build_empty_list(app=app)

    async def upload_blueprint_files(
        self,
        *,
        app: App,
        session_name: str,
        uploads: list[UploadFile],
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult:
        return await self._satisfactory_blueprints.upload_files(
            app=app,
            session_name=session_name,
            uploads=uploads,
            actor_user_id=actor_user_id,
        )

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
        return self._satisfactory_blueprints.upload_path(
            app=app,
            session_name=session_name,
            source_path=source_path,
            upload_name=upload_name,
            actor_user_id=actor_user_id,
            config_source_path=config_source_path,
            config_upload_name=config_upload_name,
        )

    def delete_blueprint_file(
        self,
        *,
        app: App,
        blueprint_id: str,
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult:
        return self._satisfactory_blueprints.delete_file(
            app=app,
            blueprint_id=blueprint_id,
            actor_user_id=actor_user_id,
        )

    def build_setting_list(self, *, app: App, actor_user_id: int) -> NodeSettingList:
        return self._app_operations.build_setting_list(app=app, actor_user_id=actor_user_id)

    async def update_setting(
        self,
        *,
        app: App,
        setting_key: str,
        value: str,
        actor_user_id: int,
    ) -> NodeSettingMutationResult:
        return await self._app_operations.update_setting(
            app=app,
            setting_key=setting_key,
            value=value,
            actor_user_id=actor_user_id,
        )

    async def save_settings(self, *, app: App, actor_user_id: int) -> NodeSettingsActionResult:
        return await self._app_operations.save_settings(app=app, actor_user_id=actor_user_id)

    async def reload_settings(self, *, app: App, actor_user_id: int) -> NodeSettingsActionResult:
        return await self._app_operations.reload_settings(app=app, actor_user_id=actor_user_id)

    def build_console_action_list(self, *, app: App, actor_user_id: int) -> NodeConsoleActionList:
        return self._app_operations.build_console_action_list(app=app, actor_user_id=actor_user_id)

    async def read_console_stdout(
        self,
        *,
        app: App,
        actor_user_id: int,
        max_lines: int = 200,
    ) -> NodeConsoleStdoutSnapshot:
        return await self._app_operations.read_console_stdout(
            app=app,
            actor_user_id=actor_user_id,
            max_lines=max_lines,
        )

    def build_console_stdout_snapshot(
        self,
        *,
        app: App,
        max_lines: int = 200,
    ) -> NodeConsoleStdoutSnapshot:
        return self._app_operations.build_console_stdout_snapshot(app=app, max_lines=max_lines)

    async def execute_console_action(
        self,
        *,
        app: App,
        action_key: str,
        raw_value: str | None,
        actor_user_id: int,
    ) -> NodeConsoleActionExecutionResult:
        return await self._app_operations.execute_console_action(
            app=app,
            action_key=action_key,
            raw_value=raw_value,
            actor_user_id=actor_user_id,
        )

    async def update_client_pack_config(
        self,
        *,
        app: App,
        update: node_api_mod.NodeClientPackConfigUpdateRequest,
        actor_user_id: int,
    ) -> dict[str, object]:
        return await self._client_packs.update_config(
            app=app,
            update=update,
            actor_user_id=actor_user_id,
            acl=self._require_acl(),
            http_exception=_http_exception,
        )

    async def publish_client_pack_config(
        self,
        *,
        app: App,
        update: node_api_mod.NodeClientPackPublishRequest,
        actor_user_id: int,
    ) -> dict[str, object]:
        return await self._client_packs.publish_config(
            app=app,
            update=update,
            actor_user_id=actor_user_id,
            acl=self._require_acl(),
            http_exception=_http_exception,
        )

    def _resolve_console_action(self, app: App, action_key: str) -> ConsoleAction:
        return self._app_operations.resolve_console_action(app, action_key)

    def apps_url(self, *, subject: str = "web", base_url: str | None = None) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps"

    def ping_url(self, *, base_url: str | None = None) -> str:
        return f"{self._base_url(base_url)}/ping"

    def presence_stream_url(self, *, base_url: str | None = None) -> str:
        return f"{self._base_url(base_url)}/presence/stream"

    def map_api_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/map"

    def list_mods_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods"

    def mod_download_url(
        self,
        app_name: str,
        *,
        enabled_only: bool,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        del subject
        query: dict[str, str] = {"enabled_only": str(enabled_only).lower()}
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods/download?{urlencode(query)}"

    def mod_download_form(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> node_api_mod.NodeModDownloadForm:
        token: str | None = self.issue_access_token(
            subject=subject,
            app_name=app_name,
            scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        return node_api_mod.NodeModDownloadForm(
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
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/mods/{quote(mod_name, safe='')}/download"

    def list_configs_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/configs"

    def config_file_url(
        self,
        app_name: str,
        config_id: str,
        *,
        subject: str = "web",
        writable: bool = False,
        base_url: str | None = None,
    ) -> str:
        del subject, writable
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/configs/{quote(config_id, safe='/')}"

    def config_root_download_url(
        self,
        app_name: str,
        root_id: str,
        *,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/configs/roots/{quote(root_id, safe='')}/download"

    def list_saves_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/saves"

    def save_download_url(
        self,
        app_name: str,
        save_id: str,
        *,
        subject: str = "web",
        base_url: str | None = None,
    ) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/saves/{quote(save_id, safe='/')}/download"

    def list_settings_url(self, app_name: str, *, subject: str = "web", base_url: str | None = None) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/settings"

    def setting_url(
        self,
        app_name: str,
        setting_key: str,
        *,
        subject: str = "web",
        writable: bool = False,
        base_url: str | None = None,
    ) -> str:
        del subject, writable
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/settings/{quote(setting_key, safe='')}"

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
        token_node_names: Sequence[str] | None = None,
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
                    node_names=token_node_names,
                )
            except NodeTokenError as xcp:
                token_error = xcp
            else:
                log.debug(
                    "Node API token access accepted: node=%s app=%s scopes=%s",
                    self.node_name,
                    app_name,
                    scopes,
                )
                return grant

        if self._require_web_session_access(request=request, app_name=app_name, scopes=scopes):
            return None

        if secret is None and (config.INDEV or config.ALLOW_UNAUTH_NODE_API):
            log.debug(
                "Node API auth disabled: node=%s app=%s scopes=%s",
                self.node_name,
                app_name,
                scopes,
            )
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
        node_names: Sequence[str] | None = None,
    ) -> NodeAccessGrant:
        secret: str | None = config.MOD_WEB_SERVER.token_secret
        if secret is None:
            raise NodeTokenError("Node token secret is not configured.")
        token: str = self._request_token(request, access_token)
        resolved_node_names = node_names or (self.node_name,)
        token_error: NodeTokenError | None = None
        for node_name in resolved_node_names:
            try:
                return verify_node_token(
                    secret=secret,
                    token=token,
                    node=node_name,
                    app=app_name,
                    required_scopes=scopes,
                )
            except NodeTokenError as xcp:
                token_error = xcp
        raise token_error or NodeTokenError("Node token node target is not configured.")

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
        try:
            await self._acl.perm_check(actor_user_id, required_level)
        except PermissionError as xcp:
            raise _http_exception(403, str(xcp)) from xcp

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
                raise _http_exception(
                    403,
                    f"Node API scope cannot be granted by a web session: {scope.value}.",
                )
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

    def _running_blocker_name(self, app: App) -> str | None:
        manager = self._require_manager()
        blocker = manager.start_blocker(app, include_current_activity=False)
        friendly_name = blocker.blocking_app_friendly if blocker is not None else None
        if blocker is None or not isinstance(friendly_name, str) or not friendly_name.strip():
            return None
        return friendly_name

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
    def _request_token(request: Any, _access_token: str | None) -> str:
        auth_header = request.headers.get("authorization", "")
        scheme, _, token = auth_header.partition(" ")
        if scheme.casefold() == "bearer" and token:
            return token.strip()
        return ""

    def _base_url(self, base_url: str | None) -> str:
        return (base_url or self.api_base_url).rstrip("/")


def _http_exception(status_code: int, detail: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=detail)
