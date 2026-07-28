from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import re
import tempfile
import threading
import time
import uuid
from asyncio.events import AbstractEventLoop
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from functools import lru_cache
from html import escape
from logging import Logger
from pathlib import Path, PurePosixPath
from re import Pattern
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    LiteralString,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypeVar,
    assert_never,
    cast,
)
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiohttp
import hikari
import requests
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse
from hikari.impl.gateway_bot import GatewayBot
from hikari.users import OwnUser
from nicegui.elements.button import Button
from nicegui.elements.card import Card
from nicegui.elements.checkbox import Checkbox
from nicegui.elements.codemirror.codemirror import CodeMirror
from nicegui.elements.column import Column
from nicegui.elements.html import Html
from nicegui.elements.input import Input
from nicegui.elements.label import Label
from nicegui.elements.scroll_area import ScrollArea
from nicegui.elements.select import Select
from nicegui.elements.slider import Slider
from nicegui.elements.table import Table
from nicegui.elements.textarea import Textarea
from nicegui.elements.timer import Timer
from nicegui.elements.tooltip import Tooltip
from nicegui.elements.upload import Upload
from requests.models import Response
from starlette.responses import Response as StarletteResponse
from starlette.responses import StreamingResponse

import config
from _authority import AuthorityResource, read_json_object
from _discord import cached_member_role_color, color_int_to_hex
from _manager import App_Manager, ManagedApp, app_scope_from_name
from _security import Access_Control, Power_Level
from _utils import Utilities
from apps._app import App, AppRuntimeFault, AppVersionSource
from apps._config import (
    AppTitleFont,
    BulkLauncherMetadataDiscovery,
    BulkLauncherMetadataEntry,
    BulkLauncherMetadataStatus,
    ClientPackConfig,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackPolicy,
    ClientPackRelease,
    CurseForgeModMetadata,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageDiscovery,
    ModPageLink,
    ModPlacement,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
    SteamUpdateBranch,
    SteamUpdatePreset,
    steam_update_preset_for_scope,
)
from apps._node_api import NodeModUploadSource
from apps._updater import AppUpdateInfo, AppUpdateOperationKind, AppUpdateState, AppUpdateStatus
from apps.factorio.node_api import (
    NodeFactorioGenerationState,
    NodeFactorioMapExchangeString,
    NodeFactorioModSettings,
    NodeModDependencyEntry,
    NodeModDependencyResolutionResult,
    NodeModPortalResolveResult,
    NodeModPortalVersionEntry,
    NodeModPortalVersionList,
    NodeModUpdateCheckResult,
    NodeModUpdateDependency,
    NodeModUpdateDependencyAction,
    NodeModUpdateStatus,
)
from apps.minecraft.node_api import (
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationResult,
    NodeMinecraftRecipeWorkspaceState,
)
from apps.minecraft.pack_export import PackFormat, PackPurpose
from apps.satisfactory.node_api import (
    NodeBlueprintEntry,
    NodeBlueprintFileEntry,
    NodeBlueprintList,
    NodeBlueprintMutationResult,
)
from apps.sevendays.node_api import NodeSevenDaysSandboxOptionsState
from chat_hub import (
    DEFAULT_CHAT_AUTHOR_COLOR_HEX,
    ChatAttachment,
    ChatAuthor,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    ChatHub,
    ChatLink,
    ChatMediaProvider,
    ChatMessageReference,
    ChatReferenceKind,
)
from config import AuthorityEndpoint, BotMetadataModWeb, BotMetadataSnapshot
from maintenance import MAX_RESTART_INTERVAL_MINUTES, MIN_RESTART_INTERVAL_MINUTES, MaintenanceService
from mod_web_auth import ModWebAuthError, ModWebAuthService, ModWebSessionPersistence, ModWebUser
from mod_web_theme import MOD_WEB_ACTION_BASE_CLASSES, BadgeTone, apply_mod_web_theme, mod_web_badge_class
from node_api import (
    ClientPackFilePreview,
    NodeApiService,
    NodeAppActivityProviderEntry,
    NodeAppEntry,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppResourcePointSummary,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeBulkLauncherMetadataApplyResult,
    NodeCapacityMutationResult,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    NodeDiskEntry,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeFontSourceSettingsMutationResult,
    NodeModEntry,
    NodeModList,
    NodeModMutationAction,
    NodeModMutationResult,
    NodeModSummary,
    NodeModUploadBatchResult,
    NodeModUploadResult,
    NodeStateStreamEvent,
    NodeStateTopic,
    required_app_mutation_level,
    required_app_mutation_scope,
    required_mod_mutation_level,
)
from node_api_console import (
    NodeConsoleActionEntry,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleActionParameter,
    NodeConsoleStdoutSnapshot,
    NodeConsoleStdoutStreamEvent,
)
from node_api_files import (
    NodeConfigContent,
    NodeConfigEntry,
    NodeConfigList,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveMutationResult,
)
from node_api_settings import (
    NodeSettingChoice,
    NodeSettingEntry,
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
)
from node_api_relay import RelayTTSQueue
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
from node_auth import NodeAccessGrant, NodeApiScope, issue_node_token
from restart_targets import RestartTarget

__all__: tuple[str, ...] = (
    "AbstractEventLoop",
    "Access_Control",
    "Any",
    "App",
    "App_Manager",
    "AppRuntimeFault",
    "AsyncIterator",
    "AppTitleFont",
    "AppVersionSource",
    "BulkLauncherMetadataDiscovery",
    "BulkLauncherMetadataEntry",
    "BulkLauncherMetadataStatus",
    "SteamUpdateBranch",
    "SteamUpdatePreset",
    "AppUpdateInfo",
    "AppUpdateOperationKind",
    "AppUpdateState",
    "AppUpdateStatus",
    "AuthorityEndpoint",
    "AuthorityResource",
    "Awaitable",
    "BadgeTone",
    "BotMetadataModWeb",
    "BotMetadataSnapshot",
    "Button",
    "Callable",
    "Card",
    "ChatAttachment",
    "ChatAuthor",
    "ChatAuthorKind",
    "ChatEmbed",
    "ChatEndpointId",
    "ChatEndpointKind",
    "ChatEvent",
    "ChatHub",
    "ChatLink",
    "ChatMediaProvider",
    "ChatMessageReference",
    "ChatReferenceKind",
    "ClientPackConfig",
    "ClientPackFilePreview",
    "ClientPackKubeJsScript",
    "ClientPackMetadataConfig",
    "ClientPackPolicy",
    "ClientPackRelease",
    "CurseForgeModMetadata",
    "LauncherMetadataDiscovery",
    "LauncherMetadataResolution",
    "ModPageDiscovery",
    "Checkbox",
    "CodeMirror",
    "Column",
    "Coroutine",
    "DEFAULT_CHAT_AUTHOR_COLOR_HEX",
    "Enum",
    "FileResponse",
    "GatewayBot",
    "Html",
    "Input",
    "Iterable",
    "Label",
    "Literal",
    "LiteralString",
    "Logger",
    "MOD_WEB_ACTION_BASE_CLASSES",
    "ManagedApp",
    "Mapping",
    "ModType",
    "ModDownloadBlockReason",
    "ModMetadataOverrides",
    "ModPageLink",
    "ModPlacement",
    "ModPlatformMetadata",
    "ModrinthModMetadata",
    "ModWebAuthError",
    "ModWebAuthService",
    "ModWebSessionPersistence",
    "ModWebUser",
    "MAX_RESTART_INTERVAL_MINUTES",
    "MaintenanceService",
    "MIN_RESTART_INTERVAL_MINUTES",
    "MutableMapping",
    "NodeAccessGrant",
    "NodeApiScope",
    "NodeApiService",
    "NodeAppActivityProviderEntry",
    "NodeAppEntry",
    "NodeAppResourcePointSummary",
    "NodeAppMutationAction",
    "NodeAppMutationResult",
    "NodeAppRuntimeSummary",
    "NodeAppStateStreamEvent",
    "NodeAppTransitionState",
    "NodeBlueprintEntry",
    "NodeBlueprintFileEntry",
    "NodeBlueprintList",
    "NodeBlueprintMutationResult",
    "NodeBulkLauncherMetadataApplyResult",
    "NodeChatRoomSnapshot",
    "NodeChatStreamEvent",
    "NodeChatStreamEventKind",
    "NodeConfigContent",
    "NodeConfigEntry",
    "NodeConfigList",
    "NodeCapacityMutationResult",
    "NodeDiskEntry",
    "NodeDiskManagementState",
    "NodeDiskSettingsMutationResult",
    "NodeConsoleActionEntry",
    "NodeConsoleActionExecutionResult",
    "NodeConsoleActionList",
    "NodeConsoleActionParameter",
    "NodeConsoleStdoutSnapshot",
    "NodeConsoleStdoutStreamEvent",
    "NodeFontSourceSettingsMutationResult",
    "NodeFactorioGenerationState",
    "NodeFactorioMapExchangeString",
    "NodeFactorioModSettings",
    "NodeMinecraftRecipeMutationAction",
    "NodeMinecraftRecipeMutationResult",
    "NodeMinecraftRecipeWorkspaceState",
    "NodeSevenDaysSandboxOptionsState",
    "NodeModEntry",
    "NodeModList",
    "NodeModDependencyEntry",
    "NodeModDependencyResolutionResult",
    "NodeModMutationAction",
    "NodeModMutationResult",
    "NodeModPortalResolveResult",
    "NodeModPortalVersionEntry",
    "NodeModPortalVersionList",
    "NodeModSummary",
    "NodeModUpdateDependency",
    "NodeModUpdateDependencyAction",
    "NodeModUpdateCheckResult",
    "NodeModUpdateStatus",
    "NodeModUploadBatchResult",
    "NodeModUploadResult",
    "NodeModUploadSource",
    "NodeSaveEntry",
    "NodeSaveList",
    "NodeSaveMutationResult",
    "NodeSettingChoice",
    "NodeSettingEntry",
    "NodeSettingList",
    "NodeSettingMutationResult",
    "NodeSettingsActionResult",
    "NodeStateStreamEvent",
    "NodeSystemHistory",
    "NodeSystemAction",
    "NodeSystemActionHandler",
    "NodeSystemCapabilities",
    "NodeSystemActionResult",
    "NodeRestartRecord",
    "NodeRestartScheduleEntry",
    "NodeRestartScheduleState",
    "NodeRestartState",
    "NodeSystemDiskSummary",
    "NodeSystemSample",
    "NodeSystemLogCatalog",
    "NodeSystemLogEntry",
    "NodeSystemLogTail",
    "NodeStateTopic",
    "NodeSystemSummary",
    "OwnUser",
    "ParamSpec",
    "Path",
    "PackFormat",
    "PackPurpose",
    "Pattern",
    "Power_Level",
    "Protocol",
    "PurePosixPath",
    "RedirectResponse",
    "RelayTTSQueue",
    "Request",
    "RestartTarget",
    "Response",
    "ScrollArea",
    "Select",
    "Slider",
    "StarletteResponse",
    "StreamingResponse",
    "Table",
    "Textarea",
    "TYPE_CHECKING",
    "Timer",
    "Tooltip",
    "TypeAlias",
    "TypeVar",
    "Upload",
    "Utilities",
    "aiohttp",
    "apply_mod_web_theme",
    "app_scope_from_name",
    "assert_never",
    "asyncio",
    "base64",
    "cached_member_role_color",
    "cast",
    "color_int_to_hex",
    "config",
    "dataclass",
    "datetime",
    "escape",
    "field",
    "hashlib",
    "hikari",
    "inspect",
    "issue_node_token",
    "json",
    "logging",
    "lru_cache",
    "mimetypes",
    "mod_web_badge_class",
    "parse_qsl",
    "quote",
    "re",
    "read_json_object",
    "replace",
    "requests",
    "required_app_mutation_level",
    "required_app_mutation_scope",
    "required_mod_mutation_level",
    "steam_update_preset_for_scope",
    "tempfile",
    "threading",
    "time",
    "urlencode",
    "urlsplit",
    "urlunsplit",
    "uuid",
)
