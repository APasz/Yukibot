"""Node API composition root and backwards-compatible public façade."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

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
import node_api_chat
import node_api_chat_service
import node_api_client_pack
import node_api_files
import node_api_map_service
import node_api_mod
import node_api_mod_service
import node_api_node
import node_api_relay
import node_api_storage_service
import node_api_system
import node_api_system_service
import node_api_node_service
from _async_utils import run_blocking
from _audit import audit_log
from _file import File_Utils
from _manager import App_Manager, app_scope_from_name
from _security import Access_Control, Power_Level
from _sys import Stats_System
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
from chat_hub import ChatEvent
from font_assets import font_assets
from maintenance import MaintenanceService
from map_annotations import (
    MapAnnotationDraft,
    MapAnnotationList,
    MapAnnotationMutationResult,
    MapManifest,
)
from mod_web_auth import ModWebAuthService, ModWebUser
from node_api_app_routes import register_app_routes
from node_api_app_installer_routes import register_app_installer_routes
from node_api_app_installer import (
    NodeAppInstallCatalog,
    NodeAppInstallRequest,
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
)
from node_api_chat import NodeChatRoomSnapshot, NodeWebChatRequest
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
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSaveUploadTransport,
)
from node_api_map_routes import register_map_routes
from node_api_mod_routes import register_mod_routes
from node_api_node import (
    NodeCapacityMutationResult,
    NodeDiscordSettingsMutationResult,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeFontSourceSettingsMutationResult,
)
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
    NodeRestartScheduleState,
    NodeRestartState,
    NodeSystemAction,
    NodeSystemActionHandler,
    NodeSystemActionResult,
    NodeSystemCapabilities,
    NodeSystemHistory,
    NodeSystemLogCatalog,
    NodeSystemLogTail,
    NodeSystemSummary,
)
from node_api_system_routes import register_system_routes
from node_api_upload import NodeApiRequestBodyLimitMiddleware
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


# Backwards-compatible façade exports for response and transport contracts.
NodeAppInstallScopeOption = node_api_app_installer.NodeAppInstallScopeOption
NodeChatEndpointSummary = node_api_chat.NodeChatEndpointSummary
NodeChatStreamEvent = node_api_chat.NodeChatStreamEvent
NodeChatStreamEventKind = node_api_chat.NodeChatStreamEventKind
NodeDiskEntry = node_api_node.NodeDiskEntry
NodeRestartRecord = node_api_system.NodeRestartRecord
NodeRestartScheduleEntry = node_api_system.NodeRestartScheduleEntry
NodeSystemDiskSummary = node_api_system.NodeSystemDiskSummary
NodeSystemLogEntry = node_api_system.NodeSystemLogEntry
NodeSystemSample = node_api_system.NodeSystemSample
NodeSaveEntry = node_api_files.NodeSaveEntry
NodeSaveRootEntry = node_api_files.NodeSaveRootEntry
WebChatRelayPublisher = node_api_chat_service.WebChatRelayPublisher

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
    return isinstance(
        error, RuntimeError
    ) and "cannot schedule new futures after shutdown" in str(error)


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


_BulkMetadataOperationResult = TypeVar("_BulkMetadataOperationResult")


class NodeApiService:
    def __init__(self) -> None:
        self._manager: App_Manager | None = None
        self._discord_health_lock = threading.RLock()
        self._discord_service_state: DiscordServiceState | None = None
        self._discord_health: DiscordHealthSnapshot | None = None
        self._acl: Access_Control | None = None
        self._web_auth: ModWebAuthService | None = None
        self._app_footprint_cache: dict[str, NodeAppFootprintSnapshot] = {}
        self._app_state_cache = node_api_app_state.NodeAppStateCache(
            app_entry_ttl_seconds=_NODE_APP_ENTRY_CACHE_TTL_SECONDS,
            live_runtime_ttl_seconds=_LIVE_APP_RUNTIME_CACHE_TTL_SECONDS,
            full_runtime_ttl_seconds=_FULL_APP_RUNTIME_CACHE_TTL_SECONDS,
        )
        self._system_monitoring = node_api_system_service.NodeSystemMonitoringService(
            node_name=lambda: self.node_name,
            manager=lambda: self._manager,
            stats_factory=lambda: Stats_System(),
            http_exception=_http_exception,
            logger=log,
            summary_cache_ttl_seconds=_NODE_SYSTEM_SUMMARY_CACHE_TTL_SECONDS,
            history_retention_seconds=node_api_system.SYSTEM_HISTORY_RETENTION_SECONDS,
            history_interval_seconds=node_api_system.SYSTEM_HISTORY_INTERVAL_SECONDS,
            max_log_lines=_NODE_SYSTEM_LOG_MAX_LINES,
        )
        self._nodes = node_api_node_service.NodeManagementService(
            node_name=lambda: self.node_name,
            manager=lambda: self._manager,
            require_manager=lambda: self._require_manager(),
            require_acl=lambda: self._require_acl(),
            http_exception=_http_exception,
            stats_factory=lambda: Stats_System(),
            invalidate_state_caches=lambda: self._invalidate_state_caches(),
            refresh_font_assets=lambda *, google_font_urls: (
                font_assets.schedule_startup_refresh(
                    google_font_urls=google_font_urls,
                )
            ),
            audit_log=lambda event, **fields: audit_log(event, **fields),
            process_started_at=lambda: int(psutil.Process().create_time()),
            read_process_restart_record=lambda *, default_timestamp: (
                read_process_restart_record(
                    default_timestamp=default_timestamp,
                )
            ),
            read_voice_restart_record=lambda: read_voice_restart_record(),
            logger=log,
            restart_delay_seconds=_NODE_RESTART_DELAY_SECONDS,
        )
        self._presence_stream_connection_count = 0
        self._presence_stream_connection_lock = threading.Lock()
        self._system_history_task: asyncio.Task[None] | None = None
        self._app_mutations = node_api_app_state.NodeAppMutationService(
            node_name=lambda: self.node_name,
            invalidate_state_caches=lambda app_name: self._invalidate_state_caches(
                app_name=app_name
            ),
            build_runtime_summary=lambda app: self.build_app_runtime_summary(app),
            build_live_runtime_summary=lambda app: self.build_live_app_runtime_summary(
                app
            ),
            transition_ttl_seconds=_APP_TRANSITION_TTL_SECONDS,
        )
        self._app_state_subscriptions = node_api_app_state.NodeAppStateSubscriptionService(
            node_name=lambda: self.node_name,
            is_shutting_down=lambda: self._shutting_down,
            resolve_app=lambda app_name: self._resolve_app(app_name),
            build_live_runtime_summary=lambda app: self.build_live_app_runtime_summary(
                app
            ),
            list_apps=lambda: self.list_apps(),
            build_system_summary=lambda: self.build_system_summary(),
            stream_system_summary=self._stream_system_summary,
            discord_health=lambda: self._discord_service_health()[1],
            app_runtime_interval_seconds=_LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS,
            node_state_interval_seconds=_LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS,
        )
        self._chats = node_api_chat_service.NodeChatService(
            manager=lambda: self._manager,
            http_exception=_http_exception,
            history_limit=_NODE_CHAT_HISTORY_LIMIT,
            build_room_snapshot=lambda app, *, limit: self.build_chat_room_snapshot(
                app,
                limit=limit,
            ),
            build_live_runtime_summary=lambda app: self.build_live_app_runtime_summary(
                app
            ),
            subscribe_local_app_runtime=lambda app_name, callback: (
                self.subscribe_local_app_runtime(
                    app_name,
                    callback,
                )
            ),
            close_websocket=lambda websocket: self._close_websocket_quietly(websocket),
        )
        self._relay_tts = node_api_relay.NodeRelayTTSService(
            node_name=lambda: self.node_name,
            http_exception=_http_exception,
            traffic_logger=traffic_log,
        )
        self._client_packs = node_api_client_pack.NodeClientPackService(
            node_name=lambda: self.node_name,
            invalidate_app_state=lambda app_name: self._invalidate_state_caches(
                app_name=app_name
            ),
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
            invalidate_app_state=lambda app_name: self._invalidate_state_caches(
                app_name=app_name
            ),
            http_exception=_http_exception,
            traffic_log=traffic_log,
        )
        self._satisfactory_blueprints = (
            satisfactory_node_api.SatisfactoryBlueprintService(
                node_name=lambda: self.node_name,
                can_sudo=lambda actor_user_id: (
                    self._acl is not None
                    and self._acl.can(actor_user_id, Power_Level.sudo)
                ),
                require_sudo=lambda actor_user_id: self._require_acl().can(
                    actor_user_id, Power_Level.sudo
                ),
                display_name_for_user=lambda user_id: (
                    config.Name_Cache().cached_display_name(
                        user_id,
                        f"User {user_id}",
                    )
                ),
                http_exception=_http_exception,
                traffic_log=traffic_log,
            )
        )
        self._storage = node_api_storage_service.NodeStorageService(
            node_name=lambda: self.node_name,
            current_acl=lambda: self._acl,
            invalidate_client_pack_content=self._invalidate_client_pack_content,
            http_exception=_http_exception,
            runtime_http_exception=self._runtime_http_exception,
            traffic_log=traffic_log,
        )
        self._maps = node_api_map_service.NodeMapService(
            node_name=lambda: self.node_name,
            http_exception=_http_exception,
            request_get=lambda url, *, params, timeout: requests.get(
                url, params=params, timeout=timeout
            ),
            logger=log,
        )
        self._mod_service = node_api_mod_service.NodeModService(
            node_name=lambda: self.node_name,
            require_acl=self._require_acl,
            build_runtime_summary=lambda app: self.build_cached_app_runtime_summary(
                app
            ),
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

    def _discord_service_health(
        self,
    ) -> tuple[DiscordServiceState | None, DiscordHealthSnapshot | None]:
        with self._discord_health_lock:
            return (self._discord_service_state, self._discord_health)

    def _invalidate_state_caches(self, *, app_name: str | None = None) -> None:
        self._app_state_cache.invalidate(app_name)
        self._system_monitoring.invalidate_summary_cache()

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
        self._nodes.set_system_action_handler(handler)

    def set_maintenance_service(
        self,
        maintenance_service: MaintenanceService,
        available_targets: tuple[RestartTarget, ...],
    ) -> None:
        self._nodes.set_maintenance_service(maintenance_service, available_targets)

    def set_chat_relay_service(self, chat_relay: WebChatRelayPublisher | None) -> None:
        self._chats.set_relay(chat_relay)

    def set_relay_tts_service(self, relay_tts_service: RelayTTSQueue | None) -> None:
        self._relay_tts.set_queue(relay_tts_service)

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
            self._system_history_task = asyncio.get_running_loop().create_task(
                self._sample_system_history()
            )

    async def _sample_system_history(self) -> None:
        try:
            while not self._shutting_down:
                try:
                    self.build_system_summary(force_refresh=True)
                except Exception:
                    log.exception(
                        "Node API system history sample failed: node=%s", self.node_name
                    )
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
        apps = tuple(
            sorted(manager.apps.values(), key=lambda item: item.friendly.casefold())
        )
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
        return await self._app_state_cache.app_entry(
            app, build_entry=lambda app: self._build_live_app_entry(app)
        )

    async def _build_live_app_entry(self, app: App) -> NodeAppEntry:
        player_snapshot = await self._app_player_snapshot(app)
        return self.build_app_entry(
            app,
            transition_state=self._app_mutations.transition_state(app.name),
            player_count=None
            if player_snapshot is None
            else player_snapshot.player_count,
            player_capacity=None
            if player_snapshot is None
            else player_snapshot.player_capacity,
            connected_player_names=()
            if player_snapshot is None
            else player_snapshot.connected_player_names,
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
        relay_notice_player_session = getattr(
            app, "relay_notice_player_session_enabled", None
        )
        relay_notice_player_death = getattr(
            app, "relay_notice_player_death_enabled", None
        )
        relay_notice_progress = getattr(app, "relay_notice_progress_enabled", None)
        relay_advancements_enabled = getattr(app, "relay_advancements_enabled", None)
        rcon_requires_online_players = getattr(
            app, "rcon_requires_online_players_enabled", None
        )
        return NodeAppEntry(
            name=app.name,
            friendly=app.friendly,
            node=self.node_name,
            running=app.check_running(),
            enabled=app.cfg.enabled,
            supports_mods=app.mods is not None,
            supports_configs=app.supports_config_files,
            scope=app_scope
            if isinstance(app_scope, str)
            else app_scope_from_name(app.name),
            transition_state=(
                self._app_mutations.transition_state(app.name)
                if transition_state is None
                else transition_state
            ),
            player_count=player_count,
            player_capacity=player_capacity,
            connected_player_names=connected_player_names,
            supports_saves=app.supports_save_files,
            supports_save_uploads=app.supports_save_uploads,
            supports_save_rename=app.supports_save_rename,
            supports_blueprints=bool(getattr(app, "supports_blueprints", False)),
            supports_settings=app.supports_settings,
            supports_console_actions=bool(
                getattr(app, "supports_console_actions", False)
            ),
            supports_chat=app.supports_chat_relay,
            supports_updates=update_info is not None,
            supports_sevendays_sandbox_options=bool(
                getattr(app, "supports_sevendays_sandbox_options", False)
            ),
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
            title_font_preset=getattr(
                app.cfg, "title_font_preset", AppTitleFont.AUTO.value
            ),
            notes=getattr(app.cfg, "notes", None),
            lifecycle_notice_started=getattr(app.cfg, "lifecycle_notice_started", True),
            lifecycle_notice_stopped=getattr(app.cfg, "lifecycle_notice_stopped", True),
            lifecycle_notice_crashed=getattr(app.cfg, "lifecycle_notice_crashed", True),
            relay_notice_player_session=relay_notice_player_session,
            relay_notice_player_death=relay_notice_player_death,
            relay_notice_progress=relay_notice_progress,
            relay_notice_progress_label=(
                getattr(app, "relay_progress_notice_term", None)
                if relay_notice_progress is not None
                else None
            ),
            relay_advancements_enabled=relay_advancements_enabled,
            relay_advancement_term=(
                getattr(app, "relay_advancement_term", None)
                if relay_advancements_enabled is not None
                else None
            ),
            factorio_chat_relay_use_shout=(
                getattr(app.cfg, "factorio_chat_relay_use_shout", True)
                if app.scope == "factorio"
                else None
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

    def build_minecraft_recipe_workspace_state(
        self, app: App
    ) -> NodeMinecraftRecipeWorkspaceState:
        if not isinstance(app, Minecraft):
            raise _http_exception(
                404, f"App {app.name!r} does not expose Minecraft recipe data."
            )
        return minecraft_node_api.build_minecraft_recipe_workspace_state(app)

    def build_sevendays_sandbox_options_state(
        self, app: App
    ) -> NodeSevenDaysSandboxOptionsState:
        if not isinstance(app, SevenDays):
            raise _http_exception(
                404, f"App {app.name!r} does not expose 7D2D sandbox options."
            )
        return sevendays_node_api.build_sevendays_sandbox_options_state(app)

    def build_minecraft_item_icon_response(self, app: App, *, item_id: str) -> Response:
        if not isinstance(app, Minecraft):
            raise _http_exception(
                404, f"App {app.name!r} does not expose Minecraft recipe item icons."
            )
        try:
            return minecraft_node_api.build_minecraft_item_icon_response(
                app, item_id=item_id
            )
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
            raise _http_exception(
                404, f"App {app.name!r} does not expose Minecraft recipe data."
            )
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
            raise _http_exception(
                500, f"Minecraft recipe mutation failed: {xcp}"
            ) from xcp
        traffic_log.info(
            "Node API Minecraft recipe mutation applied: node=%s app=%s actor=%s action=%s index=%s kind=%s",
            self.node_name,
            app.name,
            actor_user_id,
            mutation_request.action.value,
            mutation_request.mutation_index,
            None
            if mutation_request.mutation is None
            else mutation_request.mutation.to_mapping().get("kind"),
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
            raise ValueError(
                f"App colour must be between 0x000000 and 0xFFFFFF, got {color!r}."
            )
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
        return self._system_monitoring.build_summary(force_refresh=force_refresh)

    def build_system_history(self) -> NodeSystemHistory:
        return self._system_monitoring.build_history()

    def build_system_log_catalog(self) -> NodeSystemLogCatalog:
        return self._system_monitoring.build_log_catalog()

    def build_system_log_tail(
        self, *, log_path: str, max_lines: int = 200
    ) -> NodeSystemLogTail:
        return self._system_monitoring.build_log_tail(
            log_path=log_path, max_lines=max_lines
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
            if any(
                candidate == included or candidate.is_relative_to(included)
                for included in included_paths
            ):
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
        node_api_chat_service.require_chat_relay_app(
            app,
            http_exception=_http_exception,
        )

    def build_chat_room_snapshot(
        self, app: App, *, limit: int = _NODE_CHAT_HISTORY_LIMIT
    ) -> NodeChatRoomSnapshot:
        return self._chats.build_room_snapshot(app, limit=limit)

    async def publish_app_web_chat(
        self,
        *,
        app: App,
        actor_user_id: int,
        chat_request: NodeWebChatRequest,
    ) -> ChatEvent:
        return await self._chats.publish_web_chat(
            app=app,
            actor_user_id=actor_user_id,
            chat_request=chat_request,
        )

    async def publish_app_fake_chat(self, *, app: App, event: ChatEvent) -> ChatEvent:
        return await self._chats.publish_fake_chat(app=app, event=event)

    async def queue_relay_tts(
        self, relay_request: node_api_relay.NodeRelayTTSRequest
    ) -> node_api_relay.NodeRelayTTSResult:
        return await self._relay_tts.queue_request(relay_request)

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
                storage_disk = system_stats.disk_snapshot_for_path(
                    app.directory, refresh=True
                )
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
                footprint_bytes = await run_blocking(
                    self._app_footprint_size_bytes, app
                )
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
        return self._maps.build_manifest(app)

    def _build_map_manifest_result(
        self,
        app: App,
    ) -> tuple[MapManifest, node_api_map_service.NodeMapProxyResponse]:
        return self._maps.build_manifest_result(app)

    def build_map_annotation_list(self, app: App) -> MapAnnotationList:
        return self._maps.build_annotation_list(app)

    def create_map_annotation(
        self,
        app: App,
        draft: MapAnnotationDraft,
        created_by_user_id: int | None,
        created_by_name: str | None,
    ) -> MapAnnotationMutationResult:
        return self._maps.create_annotation(
            app=app,
            draft=draft,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
        )

    def delete_map_annotation(
        self, app: App, annotation_id: str
    ) -> MapAnnotationMutationResult:
        return self._maps.delete_annotation(app=app, annotation_id=annotation_id)

    def _squaremap_proxy_response(
        self,
        app: App,
        relative_path: str,
        raw_query: str = "",
        *,
        allow_stale_on_error: bool = False,
    ) -> node_api_map_service.NodeMapProxyResponse:
        return self._maps.proxy_response(
            app=app,
            relative_path=relative_path,
            raw_query=raw_query,
            allow_stale_on_error=allow_stale_on_error,
        )

    @staticmethod
    def _map_annotation_creator_name(
        app: App, *, actor_user_id: int, user: ModWebUser | None
    ) -> str | None:
        fallback_username = None if user is None else user.username
        return config.Name_Cache().discord_fallback_name(
            actor_user_id,
            fallback_username,
            scope=app.scope,
            fallback_display_name=fallback_username,
        )

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
        return self._app_state_subscriptions.subscribe_node_state(
            callback, topics=topics
        )

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
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION, reason=str(xcp)
            ) from xcp
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
        await self._chats.serve_stream(
            websocket=websocket,
            app=app,
            after_revision=after_revision,
        )

    async def _serve_presence_stream(self, *, websocket: WebSocket) -> None:
        if not self._try_reserve_presence_stream_connection():
            await websocket.close(
                code=status.WS_1013_TRY_AGAIN_LATER,
                reason="Presence stream capacity reached.",
            )
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
            if (
                self._presence_stream_connection_count
                >= _MAX_PRESENCE_STREAM_CONNECTIONS
            ):
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

    async def portal_node_latency_probes_async(
        self,
    ) -> dict[str, PortalNodeLatencyProbe]:
        """Measure the Portal-to-node and node-to-Discord latency for dashboard badges."""

        if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.PORTAL:
            return {}
        targets = self._portal_node_latency_targets()
        measurements = await asyncio.gather(
            *(
                run_blocking(self._measure_node_latency_probe, ping_url)
                for _, ping_url in targets
            )
        )
        return {
            node_name: measurement
            for (node_name, _), measurement in zip(targets, measurements, strict=True)
        }

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
            response = requests.get(
                ping_url, timeout=_PORTAL_NODE_LATENCY_TIMEOUT_SECONDS
            )
            response.raise_for_status()
        except requests.RequestException:
            return PortalNodeLatencyProbe(
                latency_ms=None,
                discord_latency_ms=None,
                discord_service_state=None,
            )
        raw_discord_service_state = response.headers.get(
            NODE_DISCORD_SERVICE_STATE_HEADER
        )
        try:
            discord_service_state = (
                DiscordServiceState(raw_discord_service_state)
                if raw_discord_service_state is not None
                else None
            )
        except ValueError:
            discord_service_state = None
        raw_discord_latency_ms = response.headers.get(
            NODE_DISCORD_HEARTBEAT_LATENCY_HEADER
        )
        try:
            discord_latency_ms = (
                int(raw_discord_latency_ms)
                if raw_discord_latency_ms is not None
                else None
            )
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
                    system_summary=self._stream_system_summary(
                        self.build_system_summary()
                    ),
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
                    merged_event = self._merge_node_state_stream_events(
                        merged_event, update_queue.get_nowait()
                    )
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
                    system_summary=self._stream_system_summary(
                        self.build_system_summary()
                    ),
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
                    merged_event = self._merge_app_state_stream_events(
                        merged_event, update_queue.get_nowait()
                    )
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
            initial_snapshot = self.build_console_stdout_snapshot(
                app=app, max_lines=max_lines
            )
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
                interval_task = asyncio.create_task(
                    asyncio.sleep(_LOCAL_CONSOLE_STDOUT_STREAM_INTERVAL_SECONDS)
                )
                done, _pending = await asyncio.wait(
                    {interval_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    interval_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await interval_task
                    return
                next_snapshot = self.build_console_stdout_snapshot(
                    app=app, max_lines=max_lines
                )
                if next_snapshot != previous_snapshot:
                    appended_lines = self._console_stdout_appended_lines(
                        previous_snapshot, next_snapshot
                    )
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
            raise ValueError(
                "Cannot compare console stdout snapshots for different apps."
            )
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
            raise ValueError(
                "Cannot merge node state stream events for different nodes."
            )
        return NodeStateStreamEvent(
            node_name=first.node_name,
            is_initial=first.is_initial or second.is_initial,
            apps_changed=first.apps_changed or second.apps_changed,
            system_changed=first.system_changed or second.system_changed,
            health_changed=first.health_changed or second.health_changed,
            app_entries=second.app_entries
            if second.app_entries is not None
            else first.app_entries,
            system_summary=second.system_summary
            if second.system_summary is not None
            else first.system_summary,
            discord_health=second.discord_health
            if second.health_changed
            else first.discord_health,
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
            app_stats=second.app_stats
            if second.app_stats is not None
            else first.app_stats,
            system_summary=second.system_summary
            if second.system_summary is not None
            else first.system_summary,
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
            raw_connected_player_names_reader = getattr(
                app, "connected_player_names", None
            )
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

    async def build_app_install_catalog(self) -> NodeAppInstallCatalog:
        self._require_app_installer_available()
        return await self._app_installer.build_catalog(manager=self._require_manager())

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
        return self._nodes.read_app_installer_settings()

    def _require_app_installer_available(self) -> None:
        self._nodes.require_app_installer_available()

    def read_node_capacity(self) -> config.NodeCapacityProfile:
        return self._nodes.read_capacity()

    def read_node_font_sources(self) -> config.NodeFontSourceSettings:
        return self._nodes.read_font_sources()

    def read_node_disk_settings(self) -> NodeDiskManagementState:
        return self._nodes.read_disk_settings()

    def read_discord_settings(self) -> config.DiscordSettings:
        return self._nodes.read_discord_settings()

    async def schedule_system_action(
        self,
        *,
        action: NodeSystemAction,
        auto_restart_running_apps: bool,
        silent: bool,
        actor_user_id: int,
    ) -> NodeSystemActionResult:
        return await self._nodes.schedule_system_action(
            action=action,
            auto_restart_running_apps=auto_restart_running_apps,
            silent=silent,
            actor_user_id=actor_user_id,
        )

    def system_capabilities(self) -> NodeSystemCapabilities:
        return self._nodes.system_capabilities()

    def read_restart_state(self) -> NodeRestartState:
        return self._nodes.read_restart_state()

    def read_restart_schedules(self) -> NodeRestartScheduleState:
        return self._nodes.read_restart_schedules()

    async def update_restart_schedule(
        self,
        *,
        target: RestartTarget,
        interval_minutes: int | None,
        anchor_timestamp: int | None,
        actor_user_id: int,
    ) -> NodeRestartScheduleState:
        return await self._nodes.update_restart_schedule(
            target=target,
            interval_minutes=interval_minutes,
            anchor_timestamp=anchor_timestamp,
            actor_user_id=actor_user_id,
        )

    async def skip_restart_schedule(
        self,
        *,
        target: RestartTarget,
        actor_user_id: int,
    ) -> NodeRestartScheduleState:
        return await self._nodes.skip_restart_schedule(
            target=target,
            actor_user_id=actor_user_id,
        )

    async def mutate_node_capacity(
        self,
        *,
        capacity: config.NodeCapacityProfile,
        actor_user_id: int,
    ) -> NodeCapacityMutationResult:
        return await self._nodes.mutate_capacity(
            capacity=capacity,
            actor_user_id=actor_user_id,
        )

    async def mutate_app_installer_settings(
        self,
        *,
        settings: config.AppInstallerSettings,
        actor_user_id: int,
    ) -> NodeAppInstallerSettingsMutationResult:
        return await self._nodes.mutate_app_installer_settings(
            settings=settings,
            actor_user_id=actor_user_id,
        )

    async def mutate_node_disk_settings(
        self,
        *,
        preferences: config.PersistedDiskPreferences,
        actor_user_id: int,
    ) -> NodeDiskSettingsMutationResult:
        return await self._nodes.mutate_disk_settings(
            preferences=preferences,
            actor_user_id=actor_user_id,
            read_disk_settings=lambda: self.read_node_disk_settings(),
        )

    async def mutate_node_font_sources(
        self,
        *,
        settings: config.NodeFontSourceSettings,
        actor_user_id: int,
    ) -> NodeFontSourceSettingsMutationResult:
        return await self._nodes.mutate_font_sources(
            settings=settings,
            actor_user_id=actor_user_id,
        )

    async def mutate_discord_settings(
        self,
        *,
        settings: config.DiscordSettings,
        actor_user_id: int,
    ) -> NodeDiscordSettingsMutationResult:
        return await self._nodes.mutate_discord_settings(
            settings=settings,
            actor_user_id=actor_user_id,
            read_discord_settings=lambda: self.read_discord_settings(),
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

    def cancel_bulk_metadata_operation(
        self, *, app_name: str, operation_id: uuid.UUID
    ) -> bool:
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
        return await self._mod_service.resolve_mod_link_dependencies(
            app=app, url=url, version=version
        )

    async def list_mod_link_versions(
        self, *, app: App, url: str
    ) -> NodeModPortalVersionList:
        return await self._mod_service.list_mod_link_versions(app=app, url=url)

    async def list_installed_mod_versions(
        self, *, app: App, mod_name: str
    ) -> NodeModPortalVersionList:
        return await self._mod_service.list_installed_mod_versions(
            app=app, mod_name=mod_name
        )

    async def check_mod_update(
        self,
        *,
        app: App,
        mod_name: str,
        version: str | None = None,
    ) -> NodeModUpdateCheckResult:
        return await self._mod_service.check_mod_update(
            app=app, mod_name=mod_name, version=version
        )

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

    def build_config_list(
        self, app: App, *, actor_user_id: int | None = None
    ) -> NodeConfigList:
        return self._storage.build_config_list(app=app, actor_user_id=actor_user_id)

    def read_config_file(self, *, app: App, config_id: str) -> NodeConfigContent:
        return self._storage.read_config_file(app=app, config_id=config_id)

    def write_config_file(
        self, *, app: App, config_id: str, content: str
    ) -> NodeConfigContent:
        return self._storage.write_config_file(
            app=app, config_id=config_id, content=content
        )

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

    def delete_config_file(
        self, *, app: App, config_id: str
    ) -> NodeConfigMutationResult:
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
        return await self._factorio.import_map_exchange_string(
            app=app, import_request=import_request
        )

    async def sync_factorio_generation_from_running_world(
        self, *, app: App
    ) -> NodeFactorioGenerationState:
        return await self._factorio.sync_generation_from_running_world(app=app)

    async def export_factorio_map_exchange_string(
        self, *, app: App
    ) -> NodeFactorioMapExchangeString:
        return await self._factorio.export_map_exchange_string(app=app)

    def factorio_mod_settings_state(self, *, app: App) -> NodeFactorioModSettings:
        return self._factorio.mod_settings_state(app=app)

    def build_factorio_mod_settings_download_response(
        self, *, app: App
    ) -> FileResponse:
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
        return await self._storage.build_save_list(app)

    def build_empty_save_list(self, app: App) -> NodeSaveList:
        return self._storage.build_empty_save_list(app)

    async def build_save_download_response(self, *, app: App, save_id: str) -> Response:
        return await self._storage.build_save_download_response(
            app=app, save_id=save_id
        )

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
        return await self._storage.upload_save_file(
            app=app,
            root_id=root_id,
            upload=upload,
            upload_name=upload_name,
            actor_user_id=actor_user_id,
            upload_transport=upload_transport,
        )

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
        return await self._storage.upload_save_path(
            app=app,
            root_id=root_id,
            source_path=source_path,
            upload_name=upload_name,
            actor_user_id=actor_user_id,
            upload_transport=upload_transport,
        )

    async def rename_save_file(
        self,
        *,
        app: App,
        save_id: str,
        new_name: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        return await self._storage.rename_save_file(
            app=app,
            save_id=save_id,
            new_name=new_name,
            actor_user_id=actor_user_id,
        )

    async def delete_save_file(
        self,
        *,
        app: App,
        save_id: str,
        actor_user_id: int,
    ) -> NodeSaveMutationResult:
        return await self._storage.delete_save_file(
            app=app,
            save_id=save_id,
            actor_user_id=actor_user_id,
        )

    @staticmethod
    def _runtime_http_exception(
        *, app: App, action: str, error: RuntimeError
    ) -> Exception:
        detail = str(error)
        if detail == f"{app.friendly} is not running.":
            return _http_exception(409, detail)
        if detail.endswith("API is unavailable.") or detail.endswith(
            "save API is unavailable."
        ):
            return _http_exception(503, f"{action} failed: {detail}")
        return _http_exception(409, detail)

    def build_blueprint_list(
        self, app: App, *, actor_user_id: int
    ) -> NodeBlueprintList:
        return self._satisfactory_blueprints.build_list(
            app=app, actor_user_id=actor_user_id
        )

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
        return self._app_operations.build_setting_list(
            app=app, actor_user_id=actor_user_id
        )

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

    async def save_settings(
        self, *, app: App, actor_user_id: int
    ) -> NodeSettingsActionResult:
        return await self._app_operations.save_settings(
            app=app, actor_user_id=actor_user_id
        )

    async def reload_settings(
        self, *, app: App, actor_user_id: int
    ) -> NodeSettingsActionResult:
        return await self._app_operations.reload_settings(
            app=app, actor_user_id=actor_user_id
        )

    def build_console_action_list(
        self, *, app: App, actor_user_id: int
    ) -> NodeConsoleActionList:
        return self._app_operations.build_console_action_list(
            app=app, actor_user_id=actor_user_id
        )

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
        return self._app_operations.build_console_stdout_snapshot(
            app=app, max_lines=max_lines
        )

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

    def map_api_url(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> str:
        del subject
        return f"{self._base_url(base_url)}/apps/{quote(app_name, safe='')}/map"

    def list_mods_url(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> str:
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

    def list_configs_url(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> str:
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

    def list_saves_url(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> str:
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

    def list_settings_url(
        self, app_name: str, *, subject: str = "web", base_url: str | None = None
    ) -> str:
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

        if self._require_web_session_access(
            request=request, app_name=app_name, scopes=scopes
        ):
            return None

        if secret is None and (config.INDEV or config.ALLOW_UNAUTH_NODE_API):
            log.debug(
                "Node API auth disabled: node=%s app=%s scopes=%s",
                self.node_name,
                app_name,
                scopes,
            )
            return None

        reason: NodeTokenError = token_error or NodeTokenError(
            "Node API authentication is not configured."
        )
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
            raise _http_exception(
                403, "Mod mutation requires an authenticated Discord user."
            )
        user: ModWebUser | None = self._web_auth.current_user(request)
        if user is None:
            raise _http_exception(
                403, "Mod mutation requires an authenticated Discord user."
            )
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
        if verified_grant is None and (
            self._web_auth is None or not self._web_auth.enabled
        ):
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
        if verified_grant is None and (
            self._web_auth is None or not self._web_auth.enabled
        ):
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
            raise _http_exception(
                403, f"Node token subject cannot act as a web user: {subject}"
            )
        raw_user_id = subject[len(prefix) :].strip()
        if not raw_user_id.isdigit():
            raise _http_exception(
                403, f"Node token subject is invalid for web actions: {subject}"
            )
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
            log.warning(
                "Node API web session auth unavailable because Access_Control is not attached."
            )
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

    def _required_web_level(
        self, *, app_name: str | None, scopes: tuple[NodeApiScope, ...]
    ) -> Power_Level:
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
            log.debug(
                "Node API resolving app: node=%s app=%s", self.node_name, app_name
            )
            return manager.get(app_name)
        except Exception as xcp:
            log.warning(
                "Node API app not found: node=%s app=%s", self.node_name, app_name
            )
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
        if (
            blocker is None
            or not isinstance(friendly_name, str)
            or not friendly_name.strip()
        ):
            return None
        return friendly_name

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
