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
from asyncio.events import AbstractEventLoop
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
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
from nicegui.elements.textarea import Textarea
from nicegui.elements.timer import Timer
from nicegui.elements.tooltip import Tooltip
from nicegui.elements.upload import Upload
from requests.models import Response
from starlette.responses import Response as StarletteResponse

import config
from _authority import AuthorityResource, read_json_object
from _discord import cached_member_role_color, color_int_to_hex
from _manager import App_Manager, ManagedApp, app_scope_from_name
from _security import Access_Control, Power_Level
from _utils import Utilities
from apps._app import App, AppRuntimeFault
from apps._config import AppTitleFont, ModType, SteamUpdateBranch, SteamUpdatePreset, steam_update_preset_for_scope
from apps._updater import AppUpdateInfo, AppUpdateOperationKind, AppUpdateState, AppUpdateStatus
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
from mod_web_auth import ModWebAuthError, ModWebAuthService, ModWebUser
from mod_web_theme import MOD_WEB_ACTION_BASE_CLASSES, BadgeTone, apply_mod_web_theme, mod_web_badge_class
from node_api import (
    NodeApiService,
    NodeAppEntry,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppResourcePointSummary,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeBlueprintEntry,
    NodeBlueprintFileEntry,
    NodeBlueprintList,
    NodeBlueprintMutationResult,
    NodeCapacityMutationResult,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    NodeConfigContent,
    NodeConfigEntry,
    NodeConfigList,
    NodeConsoleActionEntry,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleActionParameter,
    NodeConsoleStdoutSnapshot,
    NodeFontSourceSettingsMutationResult,
    NodeModEntry,
    NodeModList,
    NodeModMutationAction,
    NodeModMutationResult,
    NodeModSummary,
    NodeModUploadResult,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSettingChoice,
    NodeSettingEntry,
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
    NodeStateStreamEvent,
    NodeSystemSummary,
    RelayTTSQueue,
    required_app_mutation_level,
    required_app_mutation_scope,
)
from node_auth import NodeAccessGrant, NodeApiScope, issue_node_token

__all__: tuple[str, ...] = (
    "AbstractEventLoop",
    "Access_Control",
    "Any",
    "App",
    "App_Manager",
    "AppRuntimeFault",
    "AppTitleFont",
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
    "ModWebAuthError",
    "ModWebAuthService",
    "ModWebUser",
    "MutableMapping",
    "NodeAccessGrant",
    "NodeApiScope",
    "NodeApiService",
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
    "NodeChatRoomSnapshot",
    "NodeChatStreamEvent",
    "NodeChatStreamEventKind",
    "NodeConfigContent",
    "NodeConfigEntry",
    "NodeConfigList",
    "NodeCapacityMutationResult",
    "NodeConsoleActionEntry",
    "NodeConsoleActionExecutionResult",
    "NodeConsoleActionList",
    "NodeConsoleActionParameter",
    "NodeConsoleStdoutSnapshot",
    "NodeFontSourceSettingsMutationResult",
    "NodeModEntry",
    "NodeModList",
    "NodeModMutationAction",
    "NodeModMutationResult",
    "NodeModSummary",
    "NodeModUploadResult",
    "NodeSaveEntry",
    "NodeSaveList",
    "NodeSaveMutationResult",
    "NodeSettingChoice",
    "NodeSettingEntry",
    "NodeSettingList",
    "NodeSettingMutationResult",
    "NodeSettingsActionResult",
    "NodeStateStreamEvent",
    "NodeSystemSummary",
    "OwnUser",
    "ParamSpec",
    "Path",
    "Pattern",
    "Power_Level",
    "Protocol",
    "PurePosixPath",
    "RedirectResponse",
    "RelayTTSQueue",
    "Request",
    "Response",
    "ScrollArea",
    "Select",
    "StarletteResponse",
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
    "steam_update_preset_for_scope",
    "tempfile",
    "threading",
    "time",
    "urlencode",
    "urlsplit",
    "urlunsplit",
)
