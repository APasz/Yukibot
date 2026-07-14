from __future__ import annotations

import asyncio
import json
import struct
import unittest
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast, get_type_hints
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import hikari
import requests
from fastapi import FastAPI, HTTPException, Request, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from modmux.models import Provider

import config
from _manager import AppStartBlocker, AppStartBlockerKind
from _mod_ops import ArchiveDataEntry, ClientPackSelection
from _security import Access_Control, Power_Level
from apps._app import (
    App,
    AppActivityProvider,
    AppActivityProviderMetadata,
    AppRuntimeFault,
    AppRuntimeFaultKind,
    AppVersionSource,
    ChatRelaySupport,
)
from apps._config import (
    App_Config,
    AppTitleFont,
    AppVersion,
    BulkLauncherMetadataDiscovery,
    BulkLauncherMetadataEntry,
    BulkLauncherMetadataStatus,
    ClientPackConfig,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackModSnapshot,
    ClientPackPolicy,
    ClientPackRelease,
    LauncherMetadataDiscovery,
    LauncherMetadataResolution,
    LauncherProviderUrls,
    Mod_Config,
    ModClassificationOverride,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageDiscovery,
    ModPageLink,
    ModPlacement,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
)
from apps._config_files import AppConfigFile, AppConfigFileContent, AppConfigFileKind, AppConfigFileRoot
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult, ConsoleResponseSource
from apps._mod import Mod
from apps._save_files import AppSaveEntry, AppSaveEntryKind, AppSaveRoot, AppSaveRootMode
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Settings_Manager,
    StringSettingSpec,
)
from apps._updater import (
    AppUpdateBranchState,
    AppUpdateInfo,
    AppUpdateOperationKind,
    AppUpdateOperationResult,
    AppUpdateProviderKind,
    AppUpdateState,
    AppUpdateStatus,
    Update_Manager,
)
from apps.factorio import (
    FactorioModPortalCandidate,
    FactorioModPortalDownload,
    FactorioModPortalReleaseOption,
    FactorioModPortalResolution,
)
from apps.minecraft import (
    Minecraft,
    MinecraftItemRegistrySnapshot,
    MinecraftRecipeBook,
    MinecraftRecipeIngredient,
    MinecraftRecipeItemStack,
    MinecraftShapelessRecipe,
)
from apps.minecraft.pack_export import PackFormat, PackPurpose
from apps.satisfactory import Satisfactory, SatisfactoryBlueprintOwnershipStore, SatisfactoryServerState
from apps.sevendays import SevenDays, SevenDaysSandboxOption, SevenDaysSandboxOptionsSnapshot
from chat_hub import ChatAuthor, ChatAuthorKind, ChatEndpoint, ChatEndpointId, ChatEvent, ChatHub
from maintenance import MaintenanceService
from map_annotations import MapAnnotationDraft
from node_api import (
    NodeApiService,
    NodeAppActivityProviderEntry,
    NodeAppEntry,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeBlueprintList,
    NodeBlueprintMutationResult,
    NodeBulkLauncherMetadataApplyRequest,
    NodeBulkLauncherMetadataRequest,
    NodeCapacityMutationResult,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    NodeClientPackConfigUpdateRequest,
    NodeClientPackModConfigUpdate,
    NodeClientPackPublishRequest,
    NodeConfigList,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleStdoutSnapshot,
    NodeConsoleStdoutStreamEvent,
    NodeConsoleStdoutStreamEventKind,
    NodeDiscordSettingsMutationResult,
    NodeDiskEntry,
    NodeDiskManagementState,
    NodeDiskSettingsMutationResult,
    NodeDownloadRequest,
    NodeFactorioModSettings,
    NodeFontSourceSettingsMutationResult,
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationRequest,
    NodeMinecraftRecipeMutationResult,
    NodeModEntry,
    NodeModList,
    NodeModMetadataFetchRequest,
    NodeModMetadataResolveRequest,
    NodeModMutationAction,
    NodeModMutationResult,
    NodeModPageResolveRequest,
    NodeModPortalInstallRequest,
    NodeModPortalResolveResult,
    NodeModPortalVersionEntry,
    NodeModPortalVersionList,
    NodeModPropertiesUpdateRequest,
    NodeModUpdateDependency,
    NodeModUpdateDependencyAction,
    NodeModUpdateCheckResult,
    NodeModUpdateRequest,
    NodeModUpdateStatus,
    NodeModUploadBatchResult,
    NodeModUploadResult,
    NodeModUploadSource,
    NodeRelayTTSRequest,
    NodeRestartRecord,
    NodeRestartScheduleState,
    NodeRestartState,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSettingEntry,
    NodeSettingList,
    NodeSevenDaysSandboxOptionsState,
    NodeStateStreamEvent,
    NodeStateTopic,
    NodeSystemAction,
    NodeSystemDiskSummary,
    NodeSystemHistory,
    NodeSystemSample,
    NodeSystemSummary,
    NodeWebChatRequest,
    RemoteRelayTTSForwarder,
    required_app_mutation_level,
    required_app_mutation_scope,
    required_mod_mutation_level,
)
from node_auth import NodeApiScope, verify_node_token
from restart_targets import RestartTarget
from restart_state import RestartKind, RestartRecord


class _DummyApp(App[Any]):
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


class _ConsoleActionApp(_DummyApp):
    _console_actions: tuple[ConsoleAction, ...] = ()

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return self.available_console_actions(self._console_actions)


class _TestMod(Mod):
    async def install(self, src: Path, atomic: bool = True) -> None:
        del src, atomic


@dataclass(frozen=True, slots=True)
class _ServersDatEntry:
    name: str
    ip: str


def _read_nbt_string(content: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from(">H", content, offset)[0]
    offset += 2
    value = content[offset:offset + length].decode("utf-8")
    return value, offset + length


def _parse_servers_dat(content: bytes) -> tuple[_ServersDatEntry, ...]:
    offset = 0
    if content[offset] != 0x0A:
        raise AssertionError("servers.dat root tag must be a compound")
    offset += 1
    root_name, offset = _read_nbt_string(content, offset)
    if root_name:
        raise AssertionError("servers.dat root compound name must be empty")
    if content[offset] != 0x09:
        raise AssertionError("servers.dat must contain a servers list tag")
    offset += 1
    list_name, offset = _read_nbt_string(content, offset)
    if list_name != "servers":
        raise AssertionError(f"servers.dat list name was {list_name!r}, expected 'servers'")
    list_type = content[offset]
    offset += 1
    if list_type != 0x0A:
        raise AssertionError("servers.dat servers list must contain compounds")
    entry_count = struct.unpack_from(">i", content, offset)[0]
    offset += 4
    entries: list[_ServersDatEntry] = []
    for _ in range(entry_count):
        name: str | None = None
        ip: str | None = None
        while True:
            tag_type = content[offset]
            offset += 1
            if tag_type == 0x00:
                break
            tag_name, offset = _read_nbt_string(content, offset)
            if tag_type == 0x08:
                value, offset = _read_nbt_string(content, offset)
                if tag_name == "name":
                    name = value
                elif tag_name == "ip":
                    ip = value
                continue
            if tag_type == 0x01:
                offset += 1
                continue
            raise AssertionError(f"servers.dat contained unsupported tag type {tag_type} for {tag_name!r}")
        if name is None or ip is None:
            raise AssertionError("servers.dat entry must contain name and ip")
        entries.append(_ServersDatEntry(name=name, ip=ip))
    if content[offset] != 0x00:
        raise AssertionError("servers.dat root compound must end after the servers list")
    offset += 1
    if offset != len(content):
        raise AssertionError("servers.dat contained trailing bytes")
    return tuple(entries)


class _DummyReceiver:
    async def send(self, payload: object) -> None:
        del payload


class _DummySettingsApp(App_Settings):
    def __init__(self, pointer: Path, options: list[Setting[Any]]) -> None:
        self.saved = False
        self.loaded = False
        super().__init__(pointer, options)

    def load(self):
        self.loaded = True
        for setting in self.options:
            if isinstance(setting.value, hikari.UndefinedType):
                if setting.value_type is int:
                    setting.value = setting.value_type(0)
                elif setting.value_type is bool:
                    setting.value = setting.value_type(False)
                else:
                    setting.value = setting.value_type("loaded")

    def save(self):
        self.saved = True


def _build_app(mod_manager: object) -> _DummyApp:
    app = object.__new__(_DummyApp)
    app.name = "minecraft_alpha"
    app.friendly = "Minecraft Alpha"
    app.scope = "minecraft"
    app.directory = Path(".")
    app.mods = cast(Any, mod_manager)
    app.settings = None
    app.runtime_fault = None
    app.cfg = App_Config(
        name=app.name,
        instance_key="alpha",
        friendly_name=app.friendly,
        directory=Path("."),
        apps_dir=Path("."),
        scope=app.scope,
    )
    app.check_running = Mock(return_value=False)  # type: ignore[method-assign]
    return app


def _build_console_action_app(
    *,
    actions: tuple[ConsoleAction, ...],
    running: bool = False,
) -> _ConsoleActionApp:
    app = object.__new__(_ConsoleActionApp)
    app.name = "minecraft_alpha"
    app.friendly = "Minecraft Alpha"
    app.scope = "minecraft"
    app.directory = Path(".")
    app.mods = None
    app.settings = None
    app.runtime_fault = None
    app.cfg = App_Config(
        name=app.name,
        instance_key="alpha",
        friendly_name=app.friendly,
        directory=Path("."),
        apps_dir=Path("."),
        scope=app.scope,
    )
    app.check_running = Mock(return_value=running)  # type: ignore[method-assign]
    app._console_actions = actions
    app.chat_relay_outbound = False
    app.am_receiver = None
    app.manage_embed_color = 0x22C55E
    app.updater = None
    app.process = None
    app.config_file_read_level_override = None
    app.config_file_write_level_override = None
    app.save_file_write_level_override = None
    return app


def _build_blueprint_app(temp_path: Path) -> Satisfactory:
    app = object.__new__(Satisfactory)
    app.name = "satisfactory_alpha"
    app.friendly = "Satisfactory Alpha"
    app.dir_log = temp_path / "app-log"
    app.dir_log.mkdir(parents=True, exist_ok=True)
    cast(Any, app)._blueprint_root_override = temp_path / "blueprints"
    cast(Any, app)._blueprint_ownership_store = SatisfactoryBlueprintOwnershipStore(
        app.dir_log / "satisfactory-blueprints.json"
    )
    return app


def _attach_settings(app: _DummyApp, settings_app: App_Settings) -> None:
    app.settings = Settings_Manager(app.cfg, settings_app)


def _string_setting(
    label: str,
    key: str,
    *,
    value: str,
    choice_spec: ChoiceSpec | None,
    power_level: Power_Level,
    is_sensitive: bool = False,
    do_hide: Power_Level | None = None,
    paragraph: bool = False,
) -> Setting[str]:
    spec = StringSettingSpec(choice_spec, allow_blank=True, is_sensitive=is_sensitive, do_hide=do_hide)
    setting = Setting(
        spec,
        label,
        key,
        [],
        default=spec.parse_input(value),
        power_level=power_level,
        paragraph=paragraph,
    )
    setting.update(value)
    return setting


def _bool_setting(
    label: str,
    key: str,
    *,
    value: str,
    choice_spec: ChoiceSpec | None,
    power_level: Power_Level,
    do_hide: Power_Level | None = None,
) -> Setting[bool]:
    spec = BoolSettingSpec(choice_spec, do_hide=do_hide)
    setting = Setting(
        spec,
        label,
        key,
        [],
        default=spec.parse_input(value),
        power_level=power_level,
    )
    setting.update(value)
    return setting


def _int_setting(
    label: str,
    key: str,
    *,
    value: str,
    choice_spec: ChoiceSpec | None,
    power_level: Power_Level,
    do_hide: Power_Level | None = None,
) -> Setting[int]:
    spec = IntSettingSpec(choice_spec, do_hide=do_hide)
    setting = Setting(
        spec,
        label,
        key,
        [],
        default=spec.parse_input(value),
        power_level=power_level,
    )
    setting.update(value)
    return setting


def _setting(
    value_type: type[str] | type[bool] | type[int],
    label: str,
    key: str,
    *,
    value: str,
    choice_spec: ChoiceSpec | None = None,
    power_level: Power_Level = Power_Level.guest,
    is_sensitive: bool = False,
    do_hide: Power_Level | None = None,
) -> Setting[Any]:
    if value_type is str:
        return _string_setting(
            label,
            key,
            value=value,
            choice_spec=choice_spec,
            power_level=power_level,
            is_sensitive=is_sensitive,
            do_hide=do_hide,
        )
    if value_type is bool:
        return _bool_setting(
            label,
            key,
            value=value,
            choice_spec=choice_spec,
            power_level=power_level,
            do_hide=do_hide,
        )
    if value_type is int:
        return _int_setting(
            label,
            key,
            value=value,
            choice_spec=choice_spec,
            power_level=power_level,
            do_hide=do_hide,
        )
    raise TypeError(f"Unsupported test setting value type: {value_type!r}")


class NodeApiTests(unittest.TestCase):
    @staticmethod
    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/node/apps",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
            }
        )

    def test_node_api_fails_closed_when_authentication_is_not_configured(self) -> None:
        server = replace(config.MOD_WEB_SERVER, token_secret=None)
        with (
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(config, "INDEV", False),
            patch.object(config, "ALLOW_UNAUTH_NODE_API", False),
            self.assertRaisesRegex(
                HTTPException,
                "Node API authentication is not configured",
            ) as raised,
        ):
            NodeApiService()._require_access(
                self._request(),
                None,
                app_name=None,
                scopes=(NodeApiScope.APPS_READ,),
            )

        self.assertEqual(raised.exception.status_code, 403)

    def test_node_api_allows_unauthenticated_access_in_development(self) -> None:
        server = replace(config.MOD_WEB_SERVER, token_secret=None)
        with (
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(config, "INDEV", True),
            patch.object(config, "ALLOW_UNAUTH_NODE_API", False),
        ):
            grant = NodeApiService()._require_access(
                self._request(),
                None,
                app_name=None,
                scopes=(NodeApiScope.APPS_READ,),
            )

        self.assertIsNone(grant)

    def test_node_api_allows_explicit_unauthenticated_access(self) -> None:
        server = replace(config.MOD_WEB_SERVER, token_secret=None)
        with (
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(config, "INDEV", False),
            patch.object(config, "ALLOW_UNAUTH_NODE_API", True),
        ):
            grant = NodeApiService()._require_access(
                self._request(),
                None,
                app_name=None,
                scopes=(NodeApiScope.APPS_READ,),
            )

        self.assertIsNone(grant)

    def test_app_color_hex_formats_embed_color(self) -> None:
        self.assertEqual(NodeApiService.app_color_hex(0x22C55E), "#22C55E")
        self.assertIsNone(NodeApiService.app_color_hex(None))

    def test_app_mutation_start_requires_user_scope_and_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.START), NodeApiScope.APP_CONTROL)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.START), Power_Level.user)

    def test_app_mutation_enable_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.ENABLE), NodeApiScope.APP_MANAGE)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.ENABLE), Power_Level.sudo)

    def test_app_mutation_kill_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.KILL), NodeApiScope.APP_MANAGE)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.KILL), Power_Level.sudo)

    def test_app_mutation_rename_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.RENAME), NodeApiScope.APP_MANAGE)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.RENAME), Power_Level.sudo)

    def test_app_mutation_update_details_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.UPDATE_DETAILS), NodeApiScope.APP_MANAGE)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.UPDATE_DETAILS), Power_Level.sudo)

    def test_app_mutation_update_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.UPDATE), NodeApiScope.APP_MANAGE)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.UPDATE), Power_Level.sudo)

    def test_app_mutation_verify_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(required_app_mutation_scope(NodeAppMutationAction.VERIFY), NodeApiScope.APP_MANAGE)
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.VERIFY), Power_Level.sudo)

    def test_app_mutation_branch_select_requires_manage_scope_and_sudo_level(self) -> None:
        self.assertEqual(
            required_app_mutation_scope(NodeAppMutationAction.SELECT_UPDATE_BRANCH),
            NodeApiScope.APP_MANAGE,
        )
        self.assertEqual(required_app_mutation_level(NodeAppMutationAction.SELECT_UPDATE_BRANCH), Power_Level.sudo)

    def test_mod_download_requires_user_but_config_read_allows_visitors(self) -> None:
        service = NodeApiService()

        self.assertEqual(
            service._required_web_level(app_name="minecraft_alpha", scopes=(NodeApiScope.MODS_DOWNLOAD,)),
            Power_Level.user,
        )
        self.assertEqual(
            service._required_web_level(app_name="minecraft_alpha", scopes=(NodeApiScope.CONFIGS_READ,)),
            Power_Level.visitor,
        )

    def test_node_operation_scope_requires_sudo_while_management_requires_root(self) -> None:
        service = NodeApiService()

        self.assertEqual(
            service._required_web_level(app_name=None, scopes=(NodeApiScope.NODE_OPERATE,)),
            Power_Level.sudo,
        )
        self.assertEqual(
            service._required_web_level(app_name=None, scopes=(NodeApiScope.NODE_MANAGE,)),
            Power_Level.root,
        )

    def test_build_console_action_list_includes_parameter_metadata_and_permissions(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="message",
            label="Message",
            value_type=str,
            desc="Broadcast a message to every player.",
            max_length=64,
            multiline=True,
        )
        parameter.remember_input("Hello world")
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.sudo,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
            parameter=parameter,
        )
        app = _build_console_action_app(actions=(action,))
        acl = Mock()
        acl.can = Mock(return_value=False)
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionList = service.build_console_action_list(app=app, actor_user_id=42)

        self.assertEqual(result.app_name, app.name)
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0].power_level_name, Power_Level.sudo.name)
        self.assertEqual(result.actions[0].power_level_label, Power_Level.sudo.name.title())
        self.assertFalse(result.actions[0].can_run)
        self.assertFalse(result.actions[0].runtime_running)
        self.assertIsNotNone(result.actions[0].parameter)
        assert result.actions[0].parameter is not None
        self.assertEqual(result.actions[0].parameter.value_type_name, "str")
        self.assertEqual(result.actions[0].parameter.description, "Broadcast a message to every player.")
        self.assertEqual(result.actions[0].parameter.recent_inputs, ())

    def test_build_console_action_list_marks_running_runtime(self) -> None:
        action = ConsoleAction(
            key="save_all",
            label="Save All",
            description="Flush world state to disk.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
        )
        app = _build_console_action_app(actions=(action,), running=True)
        acl = Mock()
        acl.can = Mock(return_value=True)
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result = service.build_console_action_list(app=app, actor_user_id=42)

        self.assertTrue(result.actions[0].runtime_running)

    def test_build_console_action_list_includes_recent_inputs_for_authorised_users(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="message",
            label="Message",
            value_type=str,
        )
        parameter.remember_input("Hello world")
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
            parameter=parameter,
        )
        app = _build_console_action_app(actions=(action,))
        acl = Mock()
        acl.can = Mock(return_value=True)
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionList = service.build_console_action_list(app=app, actor_user_id=42)

        assert result.actions[0].parameter is not None
        self.assertEqual(result.actions[0].parameter.recent_inputs, ("Hello world",))

    def test_build_console_action_list_omits_actions_outside_current_app_version(self) -> None:
        gated_action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
            min_app_version=AppVersion(main="1.1.0", build=100),
        )
        app = _build_console_action_app(actions=(gated_action,))
        app.cfg.version = AppVersion(main="1.1.0", build=99)
        acl = Mock()
        acl.can = Mock(return_value=True)
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionList = service.build_console_action_list(app=app, actor_user_id=42)

        self.assertEqual(result.actions, ())
        self.assertFalse(app.supports_console_actions)

    def test_build_console_action_list_includes_actions_at_matching_app_version(self) -> None:
        gated_action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
            min_app_version=AppVersion(main="1.1.0", build=100),
            max_app_version=AppVersion(main="1.1.0", build=200),
        )
        app = _build_console_action_app(actions=(gated_action,))
        app.cfg.version = AppVersion(main="1.1.0", build=100)
        acl = Mock()
        acl.can = Mock(return_value=True)
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionList = service.build_console_action_list(app=app, actor_user_id=42)

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0].key, "say")
        self.assertTrue(app.supports_console_actions)

    def test_execute_console_action_returns_structured_result_and_tracks_recent_inputs(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="message",
            label="Message",
            value_type=str,
        )
        execute = AsyncMock(
            return_value=ConsoleActionResult(
                summary="Minecraft Alpha: broadcast sent.",
                text="[Server] hi",
                source=ConsoleResponseSource.RCON,
            )
        )
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=execute,
            parameter=parameter,
        )
        app = _build_console_action_app(actions=(action,), running=True)
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionExecutionResult = asyncio.run(
            service.execute_console_action(app=app, action_key="say", raw_value="Hello", actor_user_id=42)
        )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.user)
        execute.assert_awaited_once_with(app, "Hello")
        self.assertEqual(parameter.recent_inputs, ("Hello",))
        self.assertEqual(result.action_key, "say")
        self.assertEqual(result.summary, "Minecraft Alpha: broadcast sent.")
        self.assertEqual(result.text, "[Server] hi")
        self.assertEqual(result.source, ConsoleResponseSource.RCON)

    def test_execute_console_action_skips_player_gate_for_manual_rcon_action(self) -> None:
        execute = AsyncMock(return_value=ConsoleActionResult(summary="ok"))
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=execute,
            transport=ConsoleResponseSource.RCON,
        )
        app = _build_console_action_app(actions=(action,), running=True)
        app.rcon_requires_online_players_default = True
        app.player_count = AsyncMock(return_value=(0, 20))  # type: ignore[method-assign]
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionExecutionResult = asyncio.run(
            service.execute_console_action(app=app, action_key="say", raw_value=None, actor_user_id=42)
        )

        self.assertTrue(result.success)
        app.player_count.assert_not_awaited()
        execute.assert_awaited_once_with(app, None)

    def test_execute_console_action_skips_player_gate_when_rcon_override_is_disabled(self) -> None:
        execute = AsyncMock(return_value=ConsoleActionResult(summary="ok"))
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=execute,
            transport=ConsoleResponseSource.RCON,
        )
        app = _build_console_action_app(actions=(action,), running=True)
        app.rcon_requires_online_players_default = True
        app.cfg.rcon_requires_online_players = False
        app.player_count = AsyncMock(return_value=(0, 20))  # type: ignore[method-assign]
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        result: NodeConsoleActionExecutionResult = asyncio.run(
            service.execute_console_action(app=app, action_key="say", raw_value=None, actor_user_id=42)
        )

        self.assertTrue(result.success)
        app.player_count.assert_not_awaited()
        execute.assert_awaited_once_with(app, None)

    def test_execute_console_action_requires_running_app_when_action_demands_it(self) -> None:
        action = ConsoleAction(
            key="save_all",
            label="Save All",
            description="Flush world state to disk.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
        )
        app = _build_console_action_app(actions=(action,), running=False)
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                service.execute_console_action(app=app, action_key="save_all", raw_value=None, actor_user_id=42)
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(str(raised.exception.detail), "Minecraft Alpha is not running.")

    def test_execute_console_action_maps_runtime_api_unavailable_to_503(self) -> None:
        action = ConsoleAction(
            key="save_all",
            label="Save All",
            description="Flush world state to disk.",
            power_level=Power_Level.user,
            execute=AsyncMock(side_effect=RuntimeError("Minecraft Alpha API is unavailable.")),
        )
        app = _build_console_action_app(actions=(action,), running=True)
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                service.execute_console_action(app=app, action_key="save_all", raw_value=None, actor_user_id=42)
            )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(str(raised.exception.detail), "Console action failed: Minecraft Alpha API is unavailable.")

    def test_read_console_stdout_returns_recent_tail_and_truncation_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            stdout_path = Path(temp_dir) / "stdout.log"
            stdout_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            app = _build_console_action_app(actions=(), running=True)
            app.file_stdout = stdout_path
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            result: NodeConsoleStdoutSnapshot = asyncio.run(
                service.read_console_stdout(app=app, actor_user_id=42, max_lines=2)
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.user)
        self.assertEqual(result.app_name, "minecraft_alpha")
        self.assertEqual(result.lines, ("two", "three"))
        self.assertTrue(result.truncated)
        self.assertTrue(result.running)

    def test_list_apps_marks_console_action_support(self) -> None:
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
        )
        app = _build_console_action_app(actions=(action,), running=False)
        manager = Mock()
        manager.apps = {app.name: app}
        service = NodeApiService()
        service.set_manager(cast(Any, manager))

        with patch.object(service, "_app_player_snapshot", new=AsyncMock(return_value=None)):
            entries = asyncio.run(service.list_apps())

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].supports_console_actions)

    def test_build_minecraft_recipe_workspace_state_reads_managed_recipe_and_registry_data(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            recipe_book_path = directory / ".yukibot" / "recipes.json"
            recipe_book_path.parent.mkdir(parents=True)
            recipe_book_path.write_text(
                json.dumps(
                    MinecraftRecipeBook(
                        mutations=(),
                    ).to_mapping()
                ),
                encoding="utf-8",
            )
            item_registry_path = directory / ".yukibot" / "registries" / "items.json"
            item_registry_path.parent.mkdir(parents=True)
            item_registry_path.write_text(
                json.dumps(
                    MinecraftItemRegistrySnapshot(
                        generated_at_epoch_ms=1234567890,
                        item_ids=("minecraft:stone",),
                        block_item_ids=("minecraft:stone",),
                        item_types_classified=True,
                    ).to_mapping()
                ),
                encoding="utf-8",
            )
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            service = NodeApiService()

            workspace_state = service.build_minecraft_recipe_workspace_state(cast(App, app))

        self.assertEqual(workspace_state.recipe_book.data_path, ".yukibot/recipes.json")
        self.assertEqual(workspace_state.recipe_book.script_path, "kubejs/server_scripts/yuki_recipes.js")
        self.assertIsNotNone(workspace_state.recipe_book.payload)
        self.assertEqual(workspace_state.item_registry.data_path, ".yukibot/registries/items.json")
        self.assertTrue(workspace_state.item_registry.file_exists)
        self.assertIsNotNone(workspace_state.item_registry.payload)
        self.assertEqual(
            cast(dict[str, object], workspace_state.item_registry.payload).get("item_ids"),
            ["minecraft:stone"],
        )
        self.assertEqual(
            cast(dict[str, object], workspace_state.item_registry.payload).get("block_item_ids"),
            ["minecraft:stone"],
        )

    def test_build_sevendays_sandbox_options_state_reads_persisted_dataset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(SevenDays)
            app.name = "sevendays_alpha"
            app.directory = directory
            app.save_sandbox_options_snapshot(
                SevenDaysSandboxOptionsSnapshot(
                    generated_at="2026-06-26T10:17:36",
                    sandbox_code="AACK",
                    app_version="3.0:259",
                    options=(
                        SevenDaysSandboxOption(
                            section="General",
                            key="BlockDamage",
                            value_index=10,
                            value_label="200%",
                            default_index=7,
                            default_label="100%",
                        ),
                    ),
                )
            )

            state = NodeApiService().build_sevendays_sandbox_options_state(cast(App, app))

        self.assertEqual(state.data_path, ".yukibot/sandbox_options.json")
        self.assertTrue(state.file_exists)
        self.assertIsNotNone(state.payload)
        assert state.payload is not None
        self.assertEqual(state.payload["sandbox_code"], "AACK")

    def test_node_sevendays_sandbox_options_state_round_trips_mapping(self) -> None:
        state = NodeSevenDaysSandboxOptionsState(
            data_path=".yukibot/sandbox_options.json",
            file_exists=True,
            payload={
                "schema_version": 1,
                "generated_at": "2026-06-26T10:17:36",
                "sandbox_code": "AACK",
                "app_version": "3.0:259",
                "options": (),
            },
        )

        parsed = NodeSevenDaysSandboxOptionsState.from_mapping(state.to_mapping())

        self.assertEqual(parsed, state)

    def test_build_minecraft_item_icon_response_returns_png_file_when_icon_exists(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            asset_path = directory / "kubejs" / "assets" / "minecraft" / "textures" / "item" / "dirt.png"
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(b"png-bits")
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(Any, SimpleNamespace(list_mods=lambda state=None: []))

            response = NodeApiService().build_minecraft_item_icon_response(cast(App, app), item_id="minecraft:dirt")

        self.assertIsInstance(response, FileResponse)
        assert isinstance(response, FileResponse)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(Path(response.path), directory / ".yukibot" / "assets" / "item_icons" / "minecraft" / "dirt.png")

    def test_build_minecraft_item_icon_response_returns_svg_placeholder_when_icon_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(Any, SimpleNamespace(list_mods=lambda state=None: []))

            response = NodeApiService().build_minecraft_item_icon_response(cast(App, app), item_id="minecraft:dirt")

        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertIn("<svg", response.body.decode("utf-8"))

    def test_append_minecraft_recipe_mutation_persists_and_returns_workspace_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.directory = directory
            app.mods = cast(Any, SimpleNamespace(list_mods=lambda state=None: []))
            service = NodeApiService()
            acl = Access_Control()
            acl._roles[0] = Power_Level.sudo
            service.set_acl(acl)

            with patch.object(config.Name_Cache(), "get_game_alias", return_value="YukiPlayer"):
                result = asyncio.run(
                    service.append_minecraft_recipe_mutation(
                        app=cast(App, app),
                        mutation=MinecraftShapelessRecipe(
                            output=MinecraftRecipeItemStack("minecraft:gravel"),
                            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint"),),
                            recipe_id="client:ignored",
                        ),
                        actor_user_id=0,
                    )
                )

        self.assertIsInstance(result, NodeMinecraftRecipeMutationResult)
        self.assertEqual(result.app_name, "minecraft_alpha")
        self.assertEqual(result.workspace.recipe_book.data_path, ".yukibot/recipes.json")
        self.assertIsNotNone(result.workspace.recipe_book.payload)
        recipe_book = MinecraftRecipeBook.from_mapping(cast(dict[str, object], result.workspace.recipe_book.payload))
        self.assertEqual(len(recipe_book.mutations), 1)
        self.assertEqual(recipe_book.mutations[0].to_mapping()["id"], "yukibot:yukiplayer/minecraft/gravel")

    def test_append_minecraft_recipe_mutation_requires_linked_minecraft_username(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.directory = Path(temp_dir)
            app.mods = cast(Any, SimpleNamespace(list_mods=lambda state=None: []))
            service = NodeApiService()
            acl = Access_Control()
            acl._roles[0] = Power_Level.sudo
            service.set_acl(acl)

            with patch.object(config.Name_Cache(), "get_game_alias", return_value=None):
                with self.assertRaisesRegex(HTTPException, "Link a Minecraft username"):
                    asyncio.run(
                        service.append_minecraft_recipe_mutation(
                            app=cast(App, app),
                            mutation=MinecraftShapelessRecipe(
                                output=MinecraftRecipeItemStack("minecraft:gravel"),
                                ingredients=(MinecraftRecipeIngredient.item("minecraft:flint"),),
                            ),
                            actor_user_id=0,
                        )
                    )

            self.assertEqual(app.load_kubejs_recipe_book().mutations, ())

    def test_mutate_minecraft_recipe_book_replaces_and_deletes_by_index(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.directory = directory
            app.mods = cast(Any, SimpleNamespace(list_mods=lambda state=None: []))
            app.append_kubejs_recipe_mutation(
                MinecraftShapelessRecipe(
                    output=MinecraftRecipeItemStack("minecraft:gravel"),
                    ingredients=(MinecraftRecipeIngredient.item("minecraft:flint"),),
                    recipe_id="kubejs:flint_to_gravel",
                )
            )
            service = NodeApiService()
            acl = Access_Control()
            acl._roles[0] = Power_Level.sudo
            service.set_acl(acl)

            with patch.object(config.Name_Cache(), "get_game_alias", return_value="YukiPlayer"):
                replace_result = asyncio.run(
                    service.mutate_minecraft_recipe_book(
                        app=cast(App, app),
                        mutation_request=NodeMinecraftRecipeMutationRequest(
                            action=NodeMinecraftRecipeMutationAction.REPLACE,
                            mutation_index=0,
                            mutation=MinecraftShapelessRecipe(
                                output=MinecraftRecipeItemStack("minecraft:sand"),
                                ingredients=(MinecraftRecipeIngredient.item("minecraft:gravel"),),
                                recipe_id="client:ignored",
                            ),
                        ),
                        actor_user_id=0,
                    )
                )
            delete_result = asyncio.run(
                service.mutate_minecraft_recipe_book(
                    app=cast(App, app),
                    mutation_request=NodeMinecraftRecipeMutationRequest(
                        action=NodeMinecraftRecipeMutationAction.DELETE,
                        mutation_index=0,
                    ),
                    actor_user_id=0,
                )
            )

        replaced_recipe_book = MinecraftRecipeBook.from_mapping(cast(dict[str, object], replace_result.workspace.recipe_book.payload))
        deleted_recipe_book = MinecraftRecipeBook.from_mapping(cast(dict[str, object], delete_result.workspace.recipe_book.payload))
        self.assertEqual(replaced_recipe_book.mutations[0].to_mapping()["id"], "yukibot:yukiplayer/minecraft/sand")
        self.assertEqual(deleted_recipe_book.mutations, ())

    def test_single_mod_download_url_is_signed_and_escaped(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.single_mod_download_url("minecraft alpha", "Some Mod+1.0.jar", subject="42")

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/mods/Some%20Mod%2B1.0.jar/download")
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )
        self.assertEqual(grant.subject, "42")

    def test_ping_url_uses_base_path_without_token(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            self.assertEqual(service.ping_url(), "https://erin.example/api/node/ping")
            self.assertEqual(service.ping_url(base_url="/api/node"), "/api/node/ping")

    def test_presence_stream_url_uses_base_path_without_token(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            self.assertEqual(service.presence_stream_url(), "https://erin.example/api/node/presence/stream")
            self.assertEqual(service.presence_stream_url(base_url="/api/node"), "/api/node/presence/stream")

    def test_map_api_url_is_signed_and_escaped(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.map_api_url("minecraft alpha", subject="42", base_url="/api/node")

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/map")
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.MAP_READ,),
        )
        self.assertEqual(grant.subject, "42")

    def test_mod_download_url_omits_token_when_secret_is_unset(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_api_base_url="https://erin.example/api/node",
            token_secret=None,
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.mod_download_url("minecraft_alpha", enabled_only=True)

        self.assertEqual(url, "https://erin.example/api/node/apps/minecraft_alpha/mods/download?enabled_only=true")

    def test_mod_download_url_can_use_same_origin_base_path(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_api_base_url="https://erin.example/api/node",
            token_secret=None,
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.mod_download_url("minecraft_alpha", enabled_only=True, base_url="/api/node")

        self.assertEqual(url, "/api/node/apps/minecraft_alpha/mods/download?enabled_only=true")

    def test_mod_download_form_uses_same_origin_base_path_and_token(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            form = service.mod_download_form("minecraft alpha", base_url="/api/node")

        self.assertEqual(form.action_url, "/api/node/apps/minecraft%20alpha/mods/download")
        self.assertIsNotNone(form.access_token)

    def test_config_file_url_is_signed_and_escaped(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.config_file_url("minecraft alpha", "mod-configs/Foo Bar/config.toml", subject="42")

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/configs/mod-configs/Foo%20Bar/config.toml")
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.CONFIGS_READ,),
        )
        self.assertEqual(grant.subject, "42")

    def test_config_root_download_url_is_signed_and_escaped(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.config_root_download_url("minecraft alpha", "mod-configs", subject="42")

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/configs/roots/mod-configs/download")
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.CONFIGS_READ,),
        )
        self.assertEqual(grant.subject, "42")

    def test_settings_url_is_signed_and_escaped(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.list_settings_url("minecraft alpha", subject="42")

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/settings")
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.SETTINGS_READ,),
        )
        self.assertEqual(grant.subject, "42")

    def test_save_download_url_is_signed_and_escaped(self) -> None:
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            node_api_base_url="https://erin.example/api/node",
            token_secret="secret",
        )
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            url = service.save_download_url("minecraft alpha", "world/Current World", subject="42")

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/saves/world/Current%20World/download")
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.SAVES_DOWNLOAD,),
        )
        self.assertEqual(grant.subject, "42")

    def test_registered_routes_expose_real_fastapi_request_annotations(self) -> None:
        handlers: dict[str, object] = {}

        class _RouteCollector:
            def add_middleware(self, middleware_class: type[object], **options: object) -> None:
                del middleware_class, options

            def get(self, path: str):
                def _decorator(handler: object) -> object:
                    handlers[path] = handler
                    return handler

                return _decorator

            def post(self, path: str):
                def _decorator(handler: object) -> object:
                    handlers[path] = handler
                    return handler

                return _decorator

            def put(self, path: str):
                def _decorator(handler: object) -> object:
                    handlers[path] = handler
                    return handler

                return _decorator

            def delete(self, path: str):
                def _decorator(handler: object) -> object:
                    handlers[path] = handler
                    return handler

                return _decorator

            def websocket(self, path: str):
                def _decorator(handler: object) -> object:
                    handlers[path] = handler
                    return handler

                return _decorator

        service = NodeApiService()
        service.register_routes(_RouteCollector())

        route = handlers["/api/node/apps/{app_name}/mods/{mod_name}/download"]
        hints = get_type_hints(route)
        self.assertIs(hints["request"], Request)
        self.assertIn("/api/node/ping", handlers)
        self.assertIn("/api/node/restart", handlers)
        self.assertIn("/api/node/presence/stream", handlers)
        self.assertIn("/api/node/apps/{app_name}/chat/stream", handlers)

    def test_node_api_allows_authorized_cross_origin_uploads(self) -> None:
        app = FastAPI()
        NodeApiService().register_routes(app)

        response = TestClient(app).options(
            "/api/node/apps/minecraft_alpha/mods/upload",
            headers={
                "Origin": "https://portal.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertIn("Authorization", response.headers["access-control-allow-headers"])

    def test_mod_mutation_result_round_trips_mapping(self) -> None:
        result = NodeModMutationResult(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="erin",
            mod_name="example.jar",
            action=NodeModMutationAction.ENABLE,
            message="Enabled Example.",
            mod=None,
        )

        mapped = result.to_mapping()
        restored = NodeModMutationResult.from_mapping(mapped)

        self.assertEqual(restored, result)

    def test_mod_update_check_result_round_trips_mapping(self) -> None:
        result = NodeModUpdateCheckResult(
            app_name="factorio_lab",
            app_friendly="Factorio Lab",
            node="erin",
            mod_name="root_1.0.0.zip",
            mod_friendly="Root Mod",
            status=NodeModUpdateStatus.UPDATE_AVAILABLE,
            current_version="1.0.0",
            latest_version="1.1.0",
            latest_file_name="root_1.1.0.zip",
            page_url="https://mods.factorio.com/mod/root",
            message="Root Mod: update available 1.0.0 -> 1.1.0.",
            dependencies=(
                NodeModUpdateDependency(
                    mod_id="dependency",
                    title="Dependency",
                    page_url="https://mods.factorio.com/mod/dependency",
                    action=NodeModUpdateDependencyAction.UPDATE,
                    current_version="1.0.0",
                    latest_version="1.2.0",
                    latest_file_name="dependency_1.2.0.zip",
                    installed_mod_name="dependency_1.0.0.zip",
                ),
            ),
        )

        mapped = result.to_mapping()
        restored = NodeModUpdateCheckResult.from_mapping(mapped)

        self.assertEqual(restored, result)

    def test_app_mutation_result_round_trips_mapping(self) -> None:
        result = NodeAppMutationResult(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="erin",
            action=NodeAppMutationAction.START,
            message="Started Minecraft Alpha.",
            app_stats=None,
        )

        mapped = result.to_mapping()
        restored = NodeAppMutationResult.from_mapping(mapped)

        self.assertEqual(restored, result)

    def test_chat_stream_event_round_trips_mapping(self) -> None:
        snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=1,
            events=(),
            revision=4,
        )
        app_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=2,
            player_capacity=8,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            version_source=AppVersionSource.INSTALLED_FILES,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            transition_state=NodeAppTransitionState.NONE,
            connected_player_names=("Yoko", "Bea"),
        )
        delta = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="hello",
        )
        event = NodeChatStreamEvent(
            kind=NodeChatStreamEventKind.RUNTIME_CHANGED,
            room_id="minecraft_alpha",
            snapshot=snapshot,
            app_stats=app_stats,
            events=(delta,),
            revision=5,
        )

        mapped = event.to_mapping()
        restored = NodeChatStreamEvent.from_mapping(mapped)

        self.assertEqual(mapped["app_stats"]["version_source"], AppVersionSource.INSTALLED_FILES.value)
        self.assertEqual(restored, event)

    def test_app_runtime_summary_defaults_version_source_to_startup(self) -> None:
        restored = NodeAppRuntimeSummary.from_mapping(
            {
                "running": True,
                "enabled": True,
                "version": "1.20.1",
                "player_count": None,
                "player_capacity": None,
                "relay_support": ChatRelaySupport.NONE.value,
                "storage_percent": None,
                "storage_free_bytes": None,
                "storage_total_bytes": None,
            }
        )

        self.assertIs(restored.version_source, AppVersionSource.STARTUP)

    def test_console_stdout_append_event_round_trips_and_applies(self) -> None:
        initial = NodeConsoleStdoutSnapshot(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="erin",
            lines=("first", "second"),
            truncated=False,
            running=True,
        )
        event = NodeConsoleStdoutStreamEvent(
            kind=NodeConsoleStdoutStreamEventKind.APPEND,
            app_name=initial.app_name,
            appended_lines=("third", "fourth"),
            truncated=True,
            running=True,
        )

        restored = NodeConsoleStdoutStreamEvent.from_mapping(event.to_mapping())
        updated = restored.apply(initial, max_lines=3)

        self.assertEqual(restored, event)
        self.assertEqual(updated.lines, ("second", "third", "fourth"))
        self.assertTrue(updated.truncated)

    def test_console_stdout_overlap_detects_rolling_tail(self) -> None:
        previous = NodeConsoleStdoutSnapshot(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="erin",
            lines=("first", "second", "third"),
            truncated=False,
            running=True,
        )
        updated = replace(previous, lines=("second", "third", "fourth"), truncated=True)

        appended = NodeApiService._console_stdout_appended_lines(previous, updated)

        self.assertEqual(appended, ("fourth",))

    def test_app_state_stream_event_round_trips_mapping(self) -> None:
        app_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=2,
            player_capacity=8,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            runtime_fault=AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH, summary="Failed to start the minecraft server"
            ),
            transition_state=NodeAppTransitionState.NONE,
            connected_player_names=("Yoko", "Bea"),
        )
        system_summary = NodeSystemSummary(
            cpu_percent=20,
            ram_percent=30,
            ram_used_bytes=3,
            ram_total_bytes=10,
            storage_percent=40,
            storage_free_bytes=20,
            storage_total_bytes=30,
            running_names=("Minecraft Alpha",),
            running_app_ids=("minecraft_alpha",),
        )
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
        )
        update_status = AppUpdateStatus(
            state=AppUpdateState.RUNNING,
            summary="Downloading",
            operation_kind=AppUpdateOperationKind.UPDATE,
            progress_percent=42.5,
        )
        event = NodeAppStateStreamEvent.initial(
            app_name="minecraft_alpha",
            app_stats=app_stats,
            system_summary=system_summary,
            update_info=update_info,
            update_status=update_status,
        )

        mapped = event.to_mapping()
        restored = NodeAppStateStreamEvent.from_mapping(mapped)

        self.assertEqual(restored, event)

    def test_node_state_stream_event_round_trips_mapping(self) -> None:
        app_entry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="erin",
            running=True,
            enabled=True,
            supports_mods=True,
            supports_configs=True,
            transition_state=NodeAppTransitionState.NONE,
            player_count=2,
            player_capacity=8,
            connected_player_names=("Yoko", "Bea"),
            supports_saves=True,
            supports_save_uploads=True,
            supports_save_rename=True,
            supports_settings=True,
            supports_chat=True,
            runtime_fault=AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH, summary="Failed to start the minecraft server"
            ),
            color_hex="#336699",
        )
        system_summary = NodeSystemSummary(
            cpu_percent=20,
            ram_percent=30,
            ram_used_bytes=3,
            ram_total_bytes=10,
            storage_percent=40,
            storage_free_bytes=20,
            storage_total_bytes=30,
            running_names=("Minecraft Alpha",),
            running_app_ids=("minecraft_alpha",),
        )
        event = NodeStateStreamEvent.initial(
            node_name="erin",
            app_entries=(app_entry,),
            system_summary=system_summary,
        )

        mapped = event.to_mapping()
        restored = NodeStateStreamEvent.from_mapping(mapped)

        self.assertEqual(restored, event)

    def test_websocket_exception_from_http_maps_policy_and_internal_failures(self) -> None:
        policy_error = NodeApiService._websocket_exception_from_http(HTTPException(status_code=404, detail="Missing"))
        internal_error = NodeApiService._websocket_exception_from_http(HTTPException(status_code=503, detail="Busy"))

        self.assertEqual(policy_error.code, 1008)
        self.assertEqual(policy_error.reason, "Missing")
        self.assertEqual(internal_error.code, 1011)
        self.assertEqual(internal_error.reason, "Busy")

    def test_setting_entry_round_trips_blank_text_fields(self) -> None:
        result = NodeSettingEntry(
            key="level-seed",
            label="Level Seed",
            type_name="str",
            permission_level="User",
            permission_level_name="user",
            default_text="",
            description=None,
            paragraph=True,
            is_sensitive=False,
            value_text="",
            revealed_value_text="",
            current_input_value="",
            has_pending_value=False,
            can_edit=True,
            value_is_hidden=False,
            can_reveal_hidden_text=False,
            allows_text_input=True,
            allows_blank_input=True,
            strict_choice=False,
            choices=(),
            recent_inputs=(),
        )

        mapped = result.to_mapping()
        restored = NodeSettingEntry.from_mapping(mapped)

        self.assertEqual(restored, result)

    def test_build_setting_list_hides_restricted_values(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app: _DummyApp = _build_app(Mock())
            settings_app: _DummySettingsApp = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        str,
                        "Server Name",
                        "server_name",
                        value="Alpha",
                        power_level=Power_Level.user,
                    ),
                    _setting(
                        str,
                        "Admin Password",
                        "admin_password",
                        value="secret",
                        power_level=Power_Level.sudo,
                        is_sensitive=True,
                        do_hide=Power_Level.sudo,
                    ),
                ],
            )
            server_name: Setting[object] | None = settings_app.get_setting("server_name")
            assert server_name is not None
            server_name.update("Beta", remember_input=True)
            _attach_settings(app, settings_app)

            users_pointer: Path = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            service: NodeApiService = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            result: NodeSettingList = service.build_setting_list(app=app, actor_user_id=42)

        self.assertIsInstance(result, NodeSettingList)
        self.assertEqual(result.editable_count, 1)
        self.assertEqual(result.restricted_count, 1)
        self.assertEqual(result.settings[0].label, "Admin Password")
        self.assertTrue(result.settings[0].value_is_hidden)
        self.assertFalse(result.settings[0].can_reveal_hidden_text)
        self.assertEqual(result.settings[0].default_text, "")
        self.assertEqual(result.settings[0].value_text, "REDACTED")
        self.assertEqual(result.settings[0].revealed_value_text, "")
        self.assertEqual(result.settings[0].current_input_value, "")
        self.assertTrue(result.settings[0].allows_blank_input)
        self.assertTrue(result.settings[1].allows_blank_input)
        self.assertEqual(result.settings[1].default_text, "Alpha")
        self.assertEqual(result.settings[1].current_input_value, "Beta")
        self.assertEqual(result.settings[1].recent_inputs, ("Beta",))

    def test_build_setting_list_shows_restricted_values_when_hide_policy_is_disabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        str,
                        "Admin Password",
                        "admin_password",
                        value="secret",
                        power_level=Power_Level.sudo,
                        is_sensitive=True,
                    ),
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            result = service.build_setting_list(app=app, actor_user_id=42)

        self.assertFalse(result.settings[0].can_edit)
        self.assertFalse(result.settings[0].value_is_hidden)
        self.assertFalse(result.settings[0].can_reveal_hidden_text)
        self.assertEqual(result.settings[0].value_text, "secret")
        self.assertEqual(result.settings[0].default_text, "secret")
        self.assertEqual(result.settings[0].revealed_value_text, "")
        self.assertEqual(result.settings[0].current_input_value, "secret")

    def test_build_setting_list_keeps_hidden_values_obfuscated_for_privileged_users(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        str,
                        "Admin Password",
                        "admin_password",
                        value="secret",
                        power_level=Power_Level.user,
                        is_sensitive=True,
                        do_hide=Power_Level.user,
                    ),
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            result = service.build_setting_list(app=app, actor_user_id=42)

        self.assertTrue(result.settings[0].can_edit)
        self.assertTrue(result.settings[0].value_is_hidden)
        self.assertTrue(result.settings[0].can_reveal_hidden_text)
        self.assertEqual(result.settings[0].value_text, "REDACTED")
        self.assertEqual(result.settings[0].revealed_value_text, "secret")
        self.assertEqual(result.settings[0].default_text, "")
        self.assertEqual(result.settings[0].current_input_value, "")

    def test_build_setting_list_supports_reveal_threshold_below_edit_threshold(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        str,
                        "World Seed",
                        "world_seed",
                        value="alpha-seed",
                        power_level=Power_Level.sudo,
                        do_hide=Power_Level.user,
                    ),
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            result = service.build_setting_list(app=app, actor_user_id=42)

        self.assertFalse(result.settings[0].can_edit)
        self.assertTrue(result.settings[0].value_is_hidden)
        self.assertTrue(result.settings[0].can_reveal_hidden_text)
        self.assertEqual(result.settings[0].value_text, "Hidden")
        self.assertEqual(result.settings[0].revealed_value_text, "alpha-seed")
        self.assertEqual(result.settings[0].current_input_value, "")

    def test_build_setting_list_prefers_choice_labels_for_value_and_default_text(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        bool,
                        "Auto Pause",
                        "auto_pause",
                        value="true",
                        power_level=Power_Level.user,
                    ),
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            result = service.build_setting_list(app=app, actor_user_id=42)

        self.assertEqual(result.settings[0].value_text, "Enabled")
        self.assertEqual(result.settings[0].default_text, "Enabled")

    def test_build_setting_list_isolates_pending_drafts_per_user(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        str,
                        "Server Name",
                        "server_name",
                        value="Alpha",
                        power_level=Power_Level.user,
                    ),
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42, 84]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            asyncio.run(service.update_setting(app=app, setting_key="server_name", value="Beta", actor_user_id=42))

            actor_a_view = service.build_setting_list(app=app, actor_user_id=42)
            actor_b_view = service.build_setting_list(app=app, actor_user_id=84)

        self.assertTrue(actor_a_view.has_pending_changes)
        self.assertEqual(actor_a_view.pending_change_count, 1)
        self.assertEqual(actor_a_view.required_save_level_name, "user")
        self.assertEqual(actor_a_view.settings[0].current_input_value, "Beta")
        self.assertTrue(actor_a_view.settings[0].has_pending_value)
        self.assertFalse(actor_b_view.has_pending_changes)
        self.assertEqual(actor_b_view.pending_change_count, 0)
        self.assertEqual(actor_b_view.settings[0].current_input_value, "Alpha")
        self.assertFalse(actor_b_view.settings[0].has_pending_value)

    def test_save_settings_persists_only_callers_drafts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        str,
                        "Server Name",
                        "server_name",
                        value="Alpha",
                        power_level=Power_Level.user,
                    ),
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42, 84]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            asyncio.run(service.update_setting(app=app, setting_key="server_name", value="Beta", actor_user_id=42))
            asyncio.run(service.update_setting(app=app, setting_key="server_name", value="Gamma", actor_user_id=84))
            asyncio.run(service.save_settings(app=app, actor_user_id=42))

            actor_a_view = service.build_setting_list(app=app, actor_user_id=42)
            actor_b_view = service.build_setting_list(app=app, actor_user_id=84)

        self.assertEqual(actor_a_view.settings[0].current_input_value, "Beta")
        self.assertFalse(actor_a_view.has_pending_changes)
        self.assertEqual(actor_b_view.settings[0].current_input_value, "Gamma")
        self.assertTrue(actor_b_view.has_pending_changes)

    def test_build_save_list_uses_app_save_entries(self) -> None:
        class _SaveListApp(_DummyApp):
            _save_file_roots: tuple[AppSaveRoot, ...]

            @property
            def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
                return self._save_file_roots

            @property
            def supports_save_delete(self) -> bool:
                return True

        app = cast(_SaveListApp, object.__new__(_SaveListApp))
        app.name = "minecraft_alpha"
        app.friendly = "Minecraft Alpha"
        app._save_file_roots = (
            AppSaveRoot(
                id="world",
                label="Current World",
                path=Path("/tmp/world"),
                mode=AppSaveRootMode.SELF,
                include_files=False,
                include_directories=True,
            ),
        )
        app.list_save_files = Mock(
            return_value=(
                AppSaveEntry(
                    id="world/world",
                    label="world",
                    relative_path="world",
                    root_id="world",
                    root_label="Current World",
                    kind=AppSaveEntryKind.DIRECTORY,
                    size_bytes=0,
                    modified_at=datetime(2026, 5, 28, 12, 0, 0),
                ),
            )
        )
        result = asyncio.run(NodeApiService().build_save_list(app))

        self.assertIsInstance(result, NodeSaveList)
        self.assertEqual(result.roots[0].id, "world")
        self.assertEqual(result.saves[0].kind, "directory")
        self.assertEqual(result.saves[0].size_text, "Directory")
        self.assertTrue(result.saves[0].can_delete)

    def test_build_save_download_response_returns_existing_save_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_path = root / "world.zip"
            save_path.write_bytes(b"save-data")
            app = _build_app(Mock())
            app.resolve_save_file = Mock(return_value=save_path)  # type: ignore[method-assign]

            response = asyncio.run(NodeApiService().build_save_download_response(app=app, save_id="saves/world.zip"))

        self.assertEqual(Path(response.path), save_path)
        self.assertEqual(response.filename, "world.zip")

    def test_build_save_download_response_maps_not_running_to_409(self) -> None:
        app = SimpleNamespace(
            name="satisfactory_alpha",
            friendly="Satisfactory",
            download_save_content=AsyncMock(side_effect=RuntimeError("Satisfactory is not running.")),
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(NodeApiService().build_save_download_response(app=cast(Any, app), save_id="saves/world.sav"))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(str(raised.exception.detail), "Satisfactory is not running.")

    def test_upload_save_path_returns_save_mutation_result(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "incoming.zip"
            source.write_bytes(b"zip-data")
            uploaded = AppSaveEntry(
                id="saves/incoming.zip",
                label="incoming.zip",
                relative_path="incoming.zip",
                root_id="saves",
                root_label="Saves",
                kind=AppSaveEntryKind.FILE,
                size_bytes=8,
                modified_at=datetime(2026, 5, 30, 12, 0, 0),
            )
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                supports_save_uploads=True,
                upload_save_file_async=AsyncMock(return_value=uploaded),
            )

            result = asyncio.run(
                NodeApiService().upload_save_path(
                    app=cast(Any, app),
                    root_id="saves",
                    source_path=source,
                    upload_name="incoming.zip",
                    actor_user_id=42,
                )
            )

        self.assertIsInstance(result, NodeSaveMutationResult)
        self.assertEqual(result.save.id, "saves/incoming.zip")
        self.assertIn("Uploaded save", result.message)

    def test_upload_save_path_maps_runtime_api_unavailable_to_503(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "incoming.sav"
            source.write_bytes(b"save-data")
            app = SimpleNamespace(
                name="satisfactory_alpha",
                friendly="Satisfactory",
                supports_save_uploads=True,
                upload_save_file_async=AsyncMock(side_effect=RuntimeError("Satisfactory save API is unavailable.")),
            )

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    NodeApiService().upload_save_path(
                        app=cast(Any, app),
                        root_id="saves",
                        source_path=source,
                        upload_name="incoming.sav",
                        actor_user_id=42,
                    )
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(str(raised.exception.detail), "Save upload failed: Satisfactory save API is unavailable.")

    def test_build_blueprint_list_marks_delete_permission_from_owner_and_sudo(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_blueprint_app(root)
            source = root / "module.sbp"
            source.write_text("module", encoding="utf-8")
            config_source = root / "module.sbpcfg"
            config_source.write_text("config", encoding="utf-8")
            app.upload_blueprint_file(
                session_name="Session Alpha",
                upload_name="module.sbp",
                source_path=source,
                actor_user_id=101,
                config_upload_name="module.sbpcfg",
                config_source_path=config_source,
            )

            service = NodeApiService()
            acl = Mock()
            acl.can = Mock(side_effect=lambda user_id, level: user_id == 999 and level == Power_Level.sudo)
            service.set_acl(cast(Any, acl))

            owner_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=101)
            sudo_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=999)
            other_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=202)

        self.assertTrue(owner_list.blueprints[0].can_delete)
        self.assertTrue(sudo_list.blueprints[0].can_delete)
        self.assertFalse(other_list.blueprints[0].can_delete)
        self.assertEqual(owner_list.blueprints[0].uploaded_by_display_name, "User 101")
        self.assertIsNotNone(owner_list.blueprints[0].config_file)
        config_file = owner_list.blueprints[0].config_file
        assert config_file is not None
        self.assertEqual(config_file.uploaded_by_display_name, "User 101")

    def test_build_blueprint_list_includes_default_session_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_blueprint_app(root)
            cast(Any, app)._players = SimpleNamespace(
                state=SatisfactoryServerState(active_session_name="Session Current")
            )

            result: NodeBlueprintList = NodeApiService().build_blueprint_list(app, actor_user_id=101)
            round_trip = NodeBlueprintList.from_mapping(result.to_mapping())

        self.assertEqual(result.default_session_name, "Shared")
        self.assertEqual(round_trip.default_session_name, "Shared")

    def test_build_blueprint_list_blocks_main_delete_when_config_has_different_owner(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_blueprint_app(root)
            module_path = root / "blueprints-shared" / "Shared" / "module.sbp"
            config_path = root / "blueprints-shared" / "Shared" / "module.sbpcfg"
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.write_text("module", encoding="utf-8")
            config_path.write_text("config", encoding="utf-8")
            app._blueprint_ownership_store.record_upload(relative_path="Shared/module.sbp", actor_user_id=101)
            app._blueprint_ownership_store.record_upload(relative_path="Shared/module.sbpcfg", actor_user_id=202)

            service = NodeApiService()
            acl = Mock()
            acl.can = Mock(side_effect=lambda user_id, level: user_id == 999 and level == Power_Level.sudo)
            service.set_acl(cast(Any, acl))

            owner_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=101)
            sudo_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=999)

        self.assertFalse(owner_list.blueprints[0].can_delete)
        self.assertIsNotNone(owner_list.blueprints[0].config_file)
        config_file = owner_list.blueprints[0].config_file
        assert config_file is not None
        self.assertFalse(config_file.can_delete)
        self.assertTrue(sudo_list.blueprints[0].can_delete)

    def test_build_blueprint_list_tolerates_legacy_blueprint_filenames(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_blueprint_app(root)
            legacy_path = root / "blueprints-shared" / "Shared" / "Legacy .sbp"
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text("module", encoding="utf-8")

            service = NodeApiService()
            acl = Mock()
            acl.can = Mock(side_effect=lambda user_id, level: user_id == 999 and level == Power_Level.sudo)
            service.set_acl(cast(Any, acl))

            user_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=101)
            sudo_list: NodeBlueprintList = service.build_blueprint_list(app, actor_user_id=999)

        self.assertEqual(user_list.blueprints[0].relative_path, "Shared/Legacy .sbp")
        self.assertFalse(user_list.blueprints[0].can_delete)
        self.assertTrue(sudo_list.blueprints[0].can_delete)

    def test_upload_blueprint_path_rejects_config_only_upload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_blueprint_app(root)
            config_source = root / "module.sbpcfg"
            config_source.write_text("config", encoding="utf-8")

            with self.assertRaises(HTTPException) as raised:
                NodeApiService().upload_blueprint_path(
                    app=app,
                    session_name="Session Alpha",
                    source_path=config_source,
                    upload_name="module.sbpcfg",
                    actor_user_id=101,
                )

        self.assertEqual(raised.exception.status_code, 400)

    def test_upload_and_delete_blueprint_path_return_mutation_results(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_blueprint_app(root)
            source = root / "module.sbp"
            source.write_text("module", encoding="utf-8")
            config_source = root / "module.sbpcfg"
            config_source.write_text("config", encoding="utf-8")
            service = NodeApiService()
            acl = Mock()
            acl.can = Mock(side_effect=lambda user_id, level: user_id == 999 and level == Power_Level.sudo)
            service.set_acl(cast(Any, acl))

            uploaded: NodeBlueprintMutationResult = service.upload_blueprint_path(
                app=app,
                session_name="Session Alpha",
                source_path=source,
                upload_name="module.sbp",
                actor_user_id=101,
                config_source_path=config_source,
                config_upload_name="module.sbpcfg",
            )

            with self.assertRaises(Exception) as raised:
                service.delete_blueprint_file(
                    app=app,
                    blueprint_id=uploaded.blueprint.id,
                    actor_user_id=202,
                )

            deleted: NodeBlueprintMutationResult = service.delete_blueprint_file(
                app=app,
                blueprint_id=uploaded.blueprint.id,
                actor_user_id=999,
            )

        self.assertIsInstance(uploaded, NodeBlueprintMutationResult)
        self.assertEqual(uploaded.blueprint.relative_path, "Shared/module.sbp")
        self.assertIsNotNone(uploaded.blueprint.config_file)
        self.assertIn("Uploaded blueprint", uploaded.message)
        self.assertIn("with config", uploaded.message)
        self.assertEqual(getattr(raised.exception, "status_code"), 403)
        self.assertIsInstance(deleted, NodeBlueprintMutationResult)
        self.assertEqual(deleted.blueprint.id, uploaded.blueprint.id)
        self.assertIn("Deleted blueprint", deleted.message)

    def test_upload_mod_path_preserves_upload_filename_for_manager(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "node-upload.tmp"
            source.write_bytes(b"mod-data")
            mods_dir = root / "mods"
            mods_dir.mkdir()
            installed_path = mods_dir / "CoolMod.jar"
            installed_path.write_bytes(b"mod-data")
            installed = _TestMod(Mod_Config(name=installed_path.name, directory=mods_dir))
            manager = Mock()
            manager.reload_mods = AsyncMock()

            async def add_mod(
                path: Path,
                *,
                atomic: bool = True,
                placement: ModPlacement = ModPlacement.SERVER_ENABLED,
            ) -> Mod:
                self.assertTrue(atomic)
                self.assertIs(placement, ModPlacement.SERVER_ENABLED)
                self.assertEqual(path.name, "CoolMod.jar")
                self.assertTrue(path.exists())
                return installed

            manager.add = AsyncMock(side_effect=add_mod)
            app = _build_app(manager)

            result = asyncio.run(
                NodeApiService().upload_mod_path(
                    app=app,
                    source_path=source,
                    upload_name="CoolMod.jar",
                    actor_user_id=42,
                )
            )

        self.assertIsInstance(result, NodeModUploadResult)
        self.assertEqual(result.mod.name, "CoolMod.jar")
        self.assertIn("Uploaded mod", result.message)
        manager.reload_mods.assert_awaited_once()
        manager.add.assert_awaited_once()

    def test_upload_mod_paths_supports_multiple_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_source = root / "node-upload-1.tmp"
            second_source = root / "node-upload-2.tmp"
            first_source.write_bytes(b"mod-data-1")
            second_source.write_bytes(b"mod-data-2")
            mods_dir = root / "mods"
            mods_dir.mkdir()
            first_installed_path = mods_dir / "CoolMod.jar"
            second_installed_path = mods_dir / "AddonPack.zip"
            first_installed_path.write_bytes(b"mod-data-1")
            second_installed_path.write_bytes(b"mod-data-2")
            installed_mods = {
                "CoolMod.jar": _TestMod(Mod_Config(name=first_installed_path.name, directory=mods_dir)),
                "AddonPack.zip": _TestMod(Mod_Config(name=second_installed_path.name, directory=mods_dir)),
            }
            manager = Mock()
            manager.reload_mods = AsyncMock()
            added_names: list[str] = []

            async def add_mod(
                path: Path,
                *,
                atomic: bool = True,
                placement: ModPlacement = ModPlacement.SERVER_ENABLED,
            ) -> Mod:
                self.assertTrue(atomic)
                self.assertIs(placement, ModPlacement.SERVER_ENABLED)
                self.assertTrue(path.exists())
                added_names.append(path.name)
                return installed_mods[path.name]

            manager.add = AsyncMock(side_effect=add_mod)
            app = _build_app(manager)

            result = asyncio.run(
                NodeApiService().upload_mod_paths(
                    app=app,
                    upload_sources=(
                        NodeModUploadSource(source_path=first_source, upload_name="CoolMod.jar"),
                        NodeModUploadSource(source_path=second_source, upload_name="AddonPack.zip"),
                    ),
                    actor_user_id=42,
                )
            )

        self.assertIsInstance(result, NodeModUploadBatchResult)
        self.assertEqual(tuple(mod.name for mod in result.mods), ("CoolMod.jar", "AddonPack.zip"))
        self.assertIn("Uploaded 2 mods", result.message)
        self.assertEqual(added_names, ["CoolMod.jar", "AddonPack.zip"])
        manager.reload_mods.assert_awaited_once()
        self.assertEqual(manager.add.await_count, 2)

    def test_mod_portal_install_request_deduplicates_selected_mod_ids(self) -> None:
        request = NodeModPortalInstallRequest.model_validate(
            {
                "url": "https://mods.factorio.com/mod/root",
                "selected_mod_ids": ["root", "dep-one", "dep-one"],
                "version": " 1.2.3 ",
            }
        )

        self.assertEqual(request.selected_mod_ids, ("root", "dep-one"))
        self.assertEqual(request.version, "1.2.3")

    def test_mod_update_request_normalises_blank_version(self) -> None:
        request = NodeModUpdateRequest.model_validate({"version": " "})

        self.assertIsNone(request.version)

    def test_mod_portal_version_list_round_trips_mapping(self) -> None:
        versions = NodeModPortalVersionList.from_mapping(
            {
                "app_name": "factorio_lab",
                "app_friendly": "Factorio Lab",
                "node": "erin",
                "url": "https://mods.factorio.com/mod/root",
                "game_version": "2.0.68",
                "versions": [
                    {
                        "version": "2.1.0",
                        "file_name": "root_2.1.0.zip",
                        "released_at": "2026-02-01T00:00:00.000000Z",
                        "factorio_version": "2.0",
                    }
                ],
            }
        )

        self.assertEqual(
            versions.versions,
            (
                NodeModPortalVersionEntry(
                    version="2.1.0",
                    file_name="root_2.1.0.zip",
                    released_at="2026-02-01T00:00:00.000000Z",
                    factorio_version="2.0",
                ),
            ),
        )
        self.assertEqual(versions.to_mapping()["game_version"], "2.0.68")

    def test_install_mod_from_link_passes_requested_factorio_version(self) -> None:
        manager = Mock()
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            scope="factorio",
            directory=Path("."),
            mods=manager,
            has_mod_manager=manager,
            cfg=App_Config(
                name="factorio_lab",
                instance_key="lab",
                friendly_name="Factorio Lab",
                directory=Path("."),
                apps_dir=Path("."),
                scope="factorio",
                version=AppVersion(main="2.0.68"),
            ),
            detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
        )

        async def fake_download(
            *,
            page_url: str,
            destination_dir: Path,
            factorio_version: AppVersion | None,
            credentials: object,
            selected_mod_ids: tuple[str, ...] | None,
            requested_mod_version: str | None,
        ) -> tuple[FactorioModPortalDownload, ...]:
            del credentials, selected_mod_ids
            archive_path = destination_dir / "root_1.2.3.zip"
            archive_path.write_bytes(b"downloaded")
            self.assertEqual(page_url, "https://mods.factorio.com/mod/root")
            self.assertEqual(factorio_version, AppVersion(main="2.0.68"))
            self.assertEqual(requested_mod_version, "1.2.3")
            return (
                FactorioModPortalDownload(
                    mod_id="root",
                    page_url=page_url,
                    file_name="root_1.2.3.zip",
                    version="1.2.3",
                    archive_path=archive_path,
                ),
            )

        expected = NodeModUploadBatchResult(
            app_name="factorio_lab",
            app_friendly="Factorio Lab",
            node="erin",
            message="Uploaded 1 mod.",
            mods=(),
        )
        service = NodeApiService()
        with (
            patch("apps.factorio.node_api.factorio_mod_portal_credentials_from_server_settings", return_value=Mock()),
            patch("apps.factorio.node_api.download_factorio_mods_from_portal", new=AsyncMock(side_effect=fake_download)),
            patch.object(service, "upload_mod_paths", new=AsyncMock(return_value=expected)) as upload,
        ):
            result = asyncio.run(
                service.install_mod_from_link(
                    app=cast(App[Any], cast(object, app)),
                    url="https://mods.factorio.com/mod/root",
                    actor_user_id=42,
                    version="1.2.3",
                )
            )

        self.assertEqual(result, expected)
        upload.assert_awaited_once()

    def test_list_installed_mod_versions_uses_current_factorio_version(self) -> None:
        mod = Mock()
        mod.name = "root_1.0.0.zip"
        mod.friendly = "Root Mod"
        mod.cfg = Mod_Config(
            name="root_1.0.0.zip",
            directory=Path("."),
            mod_pages=(ModPageLink(name="Factorio Mods", url="https://mods.factorio.com/mod/root"),),
        )
        manager = Mock()
        manager.reload_mods = AsyncMock()
        manager.get.return_value = mod
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            scope="factorio",
            directory=Path("."),
            mods=manager,
            has_mod_manager=manager,
            cfg=App_Config(
                name="factorio_lab",
                instance_key="lab",
                friendly_name="Factorio Lab",
                directory=Path("."),
                apps_dir=Path("."),
                scope="factorio",
                version=AppVersion(main="1.1.107"),
            ),
            detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
        )

        async def fake_versions(
            *,
            page_url: str,
            factorio_version: AppVersion | None,
        ) -> tuple[FactorioModPortalReleaseOption, ...]:
            self.assertEqual(page_url, "https://mods.factorio.com/mod/root")
            self.assertEqual(factorio_version, AppVersion(main="2.0.68"))
            return (
                FactorioModPortalReleaseOption(
                    version="2.1.0",
                    file_name="root_2.1.0.zip",
                    released_at="2026-02-01T00:00:00.000000Z",
                    factorio_version="2.0",
                ),
            )

        with patch("apps.factorio.node_api.list_factorio_mod_portal_release_options", new=AsyncMock(side_effect=fake_versions)):
            result = asyncio.run(
                NodeApiService().list_installed_mod_versions(
                    app=cast(App[Any], cast(object, app)),
                    mod_name="root_1.0.0.zip",
                )
            )

        manager.reload_mods.assert_awaited_once()
        manager.get.assert_called_once_with("root_1.0.0.zip")
        self.assertEqual(result.game_version, "2.0.68")
        self.assertEqual(tuple(version.version for version in result.versions), ("2.1.0",))

    def test_resolve_mod_link_dependencies_marks_installed_dependencies(self) -> None:
        installed_mod = Mock()
        installed_mod.native_metadata_id.return_value = "dep-installed"
        manager = Mock()
        manager.list_mods.return_value = (installed_mod,)
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            scope="factorio",
            directory=Path("."),
            cfg=App_Config(
                name="factorio_lab",
                instance_key="lab",
                friendly_name="Factorio Lab",
                directory=Path("."),
                apps_dir=Path("."),
                scope="factorio",
                version=AppVersion(main="2.0.68"),
            ),
            mods=manager,
            has_mod_manager=manager,
            detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
        )
        resolution = FactorioModPortalResolution(
            requested_mod_id="root",
            candidates=(
                FactorioModPortalCandidate(
                    mod_id="root",
                    title="Root Mod",
                    page_url="https://mods.factorio.com/mod/root",
                    file_name="root_1.0.0.zip",
                    version="1.0.0",
                    required_by=(),
                    dependency_ids=("dep-new", "dep-installed"),
                ),
                FactorioModPortalCandidate(
                    mod_id="dep-new",
                    title="New Dependency",
                    page_url="https://mods.factorio.com/mod/dep-new",
                    file_name="dep-new_1.0.0.zip",
                    version="1.0.0",
                    required_by=("root",),
                    dependency_ids=(),
                ),
                FactorioModPortalCandidate(
                    mod_id="dep-installed",
                    title="Installed Dependency",
                    page_url="https://mods.factorio.com/mod/dep-installed",
                    file_name="dep-installed_1.0.0.zip",
                    version="1.0.0",
                    required_by=("root",),
                    dependency_ids=(),
                ),
            ),
        )

        with patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)):
            result = asyncio.run(
                NodeApiService().resolve_mod_link_dependencies(
                    app=cast(App[Any], cast(object, app)),
                    url="https://mods.factorio.com/mod/root",
                )
            )

        self.assertIsInstance(result, NodeModPortalResolveResult)
        self.assertEqual(tuple(entry.mod_id for entry in result.dependencies), ("root", "dep-new", "dep-installed"))
        self.assertEqual(tuple(entry.selected_by_default for entry in result.dependencies), (True, True, False))
        self.assertEqual(tuple(entry.installed for entry in result.dependencies), (False, False, True))
        self.assertTrue(result.dependencies[0].is_root)
        self.assertEqual(result.dependencies[0].dependency_mod_ids, ("dep-new", "dep-installed"))
        self.assertEqual(result.dependencies[1].parent_mod_ids, ("root",))

    def test_resolve_mod_link_dependencies_marks_vanilla_dependencies_installed(self) -> None:
        manager = Mock()
        manager.list_mods.return_value = ()
        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            vanilla_dir = root_dir / "data" / "space-age"
            vanilla_dir.mkdir(parents=True)
            (vanilla_dir / "info.json").write_text(
                json.dumps({"name": "space-age", "title": "Space Age", "version": "2.1.9"}),
                encoding="utf-8",
            )
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=root_dir,
                cfg=App_Config(
                    name="factorio_lab",
                    instance_key="lab",
                    friendly_name="Factorio Lab",
                    directory=root_dir,
                    apps_dir=root_dir,
                    scope="factorio",
                    version=AppVersion(main="2.1.9"),
                ),
                mods=manager,
                has_mod_manager=manager,
                detect_installed_version=Mock(return_value=AppVersion(main="2.1.9")),
            )
            resolution = FactorioModPortalResolution(
                requested_mod_id="root",
                candidates=(
                    FactorioModPortalCandidate(
                        mod_id="root",
                        title="Root Mod",
                        page_url="https://mods.factorio.com/mod/root",
                        file_name="root_1.0.0.zip",
                        version="1.0.0",
                        required_by=(),
                        dependency_ids=("space-age",),
                    ),
                    FactorioModPortalCandidate(
                        mod_id="space-age",
                        title="[reserved]",
                        page_url="https://mods.factorio.com/mod/space-age",
                        file_name="space-age_2.1.9.zip",
                        version="2.1.9",
                        required_by=("root",),
                        dependency_ids=(),
                    ),
                ),
            )

            with patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)):
                result = asyncio.run(
                    NodeApiService().resolve_mod_link_dependencies(
                        app=cast(App[Any], cast(object, app)),
                        url="https://mods.factorio.com/mod/root",
                    )
                )

        self.assertEqual(tuple(entry.mod_id for entry in result.dependencies), ("root", "space-age"))
        self.assertEqual(result.dependencies[1].title, "Space Age")
        self.assertFalse(result.dependencies[1].selected_by_default)
        self.assertTrue(result.dependencies[1].installed)

    def test_check_mod_update_reports_available_factorio_release(self) -> None:
        class _FactorioTestMod(_TestMod):
            def native_metadata_id(self) -> str:
                return "root"

        mod = _FactorioTestMod(
            Mod_Config(
                name="root_1.0.0.zip",
                directory=Path("."),
                version="1.0.0",
            ),
            nice_name="Root Mod",
        )
        manager = Mock()
        manager.reload_mods = AsyncMock()
        manager.get.return_value = mod
        manager.list_mods.return_value = (mod,)
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            scope="factorio",
            directory=Path("."),
            mods=manager,
            has_mod_manager=manager,
            cfg=App_Config(
                name="factorio_lab",
                instance_key="lab",
                friendly_name="Factorio Lab",
                directory=Path("."),
                apps_dir=Path("."),
                scope="factorio",
                version=AppVersion(main="2.0.68"),
            ),
            detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
        )
        resolution = FactorioModPortalResolution(
            requested_mod_id="root",
            candidates=(
                FactorioModPortalCandidate(
                    mod_id="root",
                    title="Root Mod",
                    page_url="https://mods.factorio.com/mod/root",
                    file_name="root_1.1.0.zip",
                    version="1.1.0",
                    required_by=(),
                    dependency_ids=(),
                ),
            ),
        )

        with patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)) as resolve:
            result = asyncio.run(
                NodeApiService().check_mod_update(
                    app=cast(App[Any], cast(object, app)),
                    mod_name="root_1.0.0.zip",
                    version="1.1.0",
                )
            )

        manager.reload_mods.assert_awaited_once()
        manager.get.assert_called_once_with("root_1.0.0.zip")
        resolve.assert_awaited_once_with(
            page_url="https://mods.factorio.com/mod/root",
            factorio_version=AppVersion(main="2.0.68"),
            requested_mod_version="1.1.0",
        )
        self.assertEqual(result.status, NodeModUpdateStatus.UPDATE_AVAILABLE)
        self.assertEqual(result.current_version, "1.0.0")
        self.assertEqual(result.latest_version, "1.1.0")
        self.assertIn("1.0.0 -> 1.1.0", result.message)

    def test_check_mod_update_treats_vanilla_factorio_dependency_as_current(self) -> None:
        class _FactorioTestMod(_TestMod):
            def native_metadata_id(self) -> str:
                return "root"

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            vanilla_dir = root_dir / "data" / "space-age"
            vanilla_dir.mkdir(parents=True)
            (vanilla_dir / "info.json").write_text(
                json.dumps({"name": "space-age", "title": "Space Age", "version": "2.1.9"}),
                encoding="utf-8",
            )
            mod = _FactorioTestMod(
                Mod_Config(
                    name="root_1.0.0.zip",
                    directory=root_dir / "mods",
                    version="1.0.0",
                ),
                nice_name="Root Mod",
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            manager.list_mods.return_value = (mod,)
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=root_dir,
                mods=manager,
                has_mod_manager=manager,
                cfg=App_Config(
                    name="factorio_lab",
                    instance_key="lab",
                    friendly_name="Factorio Lab",
                    directory=root_dir,
                    apps_dir=root_dir,
                    scope="factorio",
                    version=AppVersion(main="2.1.9"),
                ),
                detect_installed_version=Mock(return_value=AppVersion(main="2.1.9")),
            )
            resolution = FactorioModPortalResolution(
                requested_mod_id="root",
                candidates=(
                    FactorioModPortalCandidate(
                        mod_id="root",
                        title="Root Mod",
                        page_url="https://mods.factorio.com/mod/root",
                        file_name="root_1.1.0.zip",
                        version="1.1.0",
                        required_by=(),
                        dependency_ids=("space-age",),
                    ),
                    FactorioModPortalCandidate(
                        mod_id="space-age",
                        title="[reserved]",
                        page_url="https://mods.factorio.com/mod/space-age",
                        file_name="space-age_2.1.9.zip",
                        version="2.1.9",
                        required_by=("root",),
                        dependency_ids=(),
                    ),
                ),
            )

            with patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)):
                result = asyncio.run(
                    NodeApiService().check_mod_update(
                        app=cast(App[Any], cast(object, app)),
                        mod_name="root_1.0.0.zip",
                    )
                )

        self.assertEqual(tuple(dependency.mod_id for dependency in result.dependencies), ("space-age",))
        self.assertEqual(result.dependencies[0].title, "Space Age")
        self.assertEqual(result.dependencies[0].action, NodeModUpdateDependencyAction.CURRENT)
        self.assertEqual(result.dependencies[0].current_version, "2.1.9")

    def test_update_mod_replaces_enabled_factorio_mod_with_latest_release(self) -> None:
        class _FactorioTestMod(_TestMod):
            def native_metadata_id(self) -> str:
                return "root"

        with TemporaryDirectory() as temp_dir:
            mods_dir = Path(temp_dir) / "mods"
            mods_dir.mkdir()
            old_path = mods_dir / "root_1.0.0.zip"
            old_path.write_bytes(b"old")
            latest_path = mods_dir / "root_1.1.0.zip"
            latest_path.write_bytes(b"new")
            old_mod = _FactorioTestMod(
                Mod_Config(
                    name=old_path.name,
                    directory=mods_dir,
                    version="1.0.0",
                ),
                nice_name="Root Mod",
            )
            updated_mod = _FactorioTestMod(
                Mod_Config(
                    name=latest_path.name,
                    directory=mods_dir,
                    version="1.1.0",
                ),
                nice_name="Root Mod",
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = old_mod
            manager.list_mods.return_value = (old_mod,)
            manager.remove = AsyncMock(return_value=old_mod)
            manager.add = AsyncMock(return_value=updated_mod)
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=Path("."),
                mods=manager,
                has_mod_manager=manager,
                cfg=App_Config(
                    name="factorio_lab",
                    instance_key="lab",
                    friendly_name="Factorio Lab",
                    directory=Path("."),
                    apps_dir=Path("."),
                    scope="factorio",
                    version=AppVersion(main="2.0.68"),
                ),
                detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
                check_running=Mock(return_value=False),
                invalidate_client_pack_content=Mock(),
            )
            resolution = FactorioModPortalResolution(
                requested_mod_id="root",
                candidates=(
                    FactorioModPortalCandidate(
                        mod_id="root",
                        title="Root Mod",
                        page_url="https://mods.factorio.com/mod/root",
                        file_name="root_1.1.0.zip",
                        version="1.1.0",
                        required_by=(),
                        dependency_ids=(),
                    ),
                ),
            )

            async def fake_download(
                *,
                page_url: str,
                destination_dir: Path,
                factorio_version: AppVersion | None,
                credentials: object,
                selected_mod_ids: tuple[str, ...],
                requested_mod_version: str | None,
            ) -> tuple[FactorioModPortalDownload, ...]:
                del credentials
                archive_path = destination_dir / "root_1.1.0.zip"
                archive_path.write_bytes(b"downloaded")
                self.assertEqual(page_url, "https://mods.factorio.com/mod/root")
                self.assertEqual(factorio_version, AppVersion(main="2.0.68"))
                self.assertEqual(selected_mod_ids, ("root",))
                self.assertEqual(requested_mod_version, "1.1.0")
                return (
                    FactorioModPortalDownload(
                        mod_id="root",
                        page_url=page_url,
                        file_name="root_1.1.0.zip",
                        version="1.1.0",
                        archive_path=archive_path,
                    ),
                )

            with (
                patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)),
                patch("apps.factorio.node_api.factorio_mod_portal_credentials_from_server_settings", return_value=Mock()),
                patch("apps.factorio.node_api.download_factorio_mods_from_portal", new=AsyncMock(side_effect=fake_download)),
            ):
                result = asyncio.run(
                    NodeApiService().update_mod(
                        app=cast(App[Any], cast(object, app)),
                        mod_name=old_path.name,
                        actor_user_id=42,
                        version="1.1.0",
                    )
                )

            manager.remove.assert_awaited_once_with(old_mod, override_coremod=False)
            add_kwargs = manager.add.await_args.kwargs
            self.assertEqual(add_kwargs["placement"], ModPlacement.SERVER_ENABLED)
            self.assertTrue(str(manager.add.await_args.args[0]).endswith("root_1.1.0.zip"))
            self.assertEqual(tuple(mod.name for mod in result.mods), ("root_1.1.0.zip",))
            self.assertIn("1.0.0 to 1.1.0", result.message)

    def test_update_mod_installs_and_updates_required_factorio_dependencies(self) -> None:
        class _FactorioTestMod(_TestMod):
            def __init__(self, cfg: Mod_Config, *, native_id: str, nice_name: str) -> None:
                super().__init__(cfg, nice_name=nice_name)
                self._native_id = native_id

            def native_metadata_id(self) -> str:
                return self._native_id

        with TemporaryDirectory() as temp_dir:
            mods_dir = Path(temp_dir) / "mods"
            mods_dir.mkdir()
            old_root_path = mods_dir / "root_1.0.0.zip"
            old_root_path.write_bytes(b"old-root")
            old_dependency_path = mods_dir / "dep-old_1.0.0.zip"
            old_dependency_path.write_bytes(b"old-dependency")
            old_root = _FactorioTestMod(
                Mod_Config(
                    name=old_root_path.name,
                    directory=mods_dir,
                    version="1.0.0",
                ),
                native_id="root",
                nice_name="Root Mod",
            )
            old_dependency = _FactorioTestMod(
                Mod_Config(
                    name=old_dependency_path.name,
                    directory=mods_dir,
                    version="1.0.0",
                ),
                native_id="dep-old",
                nice_name="Old Dependency",
            )
            updated_root = _FactorioTestMod(
                Mod_Config(
                    name="root_1.1.0.zip",
                    directory=mods_dir,
                    version="1.1.0",
                ),
                native_id="root",
                nice_name="Root Mod",
            )
            new_dependency = _FactorioTestMod(
                Mod_Config(
                    name="dep-new_1.0.0.zip",
                    directory=mods_dir,
                    version="1.0.0",
                ),
                native_id="dep-new",
                nice_name="New Dependency",
            )
            updated_dependency = _FactorioTestMod(
                Mod_Config(
                    name="dep-old_2.0.0.zip",
                    directory=mods_dir,
                    version="2.0.0",
                ),
                native_id="dep-old",
                nice_name="Old Dependency",
            )
            installed_by_name = {
                old_root.name: old_root,
                old_dependency.name: old_dependency,
            }
            added_by_name = {
                updated_root.name: updated_root,
                new_dependency.name: new_dependency,
                updated_dependency.name: updated_dependency,
            }
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.side_effect = lambda name: installed_by_name.get(name)
            manager.list_mods.return_value = (old_root, old_dependency)
            manager.remove = AsyncMock()

            async def add_mod(
                archive_path: Path,
                *,
                atomic: bool,
                placement: ModPlacement,
            ) -> _FactorioTestMod:
                self.assertTrue(atomic)
                self.assertIs(placement, ModPlacement.SERVER_ENABLED)
                return added_by_name[archive_path.name]

            manager.add = AsyncMock(side_effect=add_mod)
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=Path("."),
                mods=manager,
                has_mod_manager=manager,
                cfg=App_Config(
                    name="factorio_lab",
                    instance_key="lab",
                    friendly_name="Factorio Lab",
                    directory=Path("."),
                    apps_dir=Path("."),
                    scope="factorio",
                    version=AppVersion(main="2.0.68"),
                ),
                detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
                check_running=Mock(return_value=False),
                invalidate_client_pack_content=Mock(),
            )
            resolution = FactorioModPortalResolution(
                requested_mod_id="root",
                candidates=(
                    FactorioModPortalCandidate(
                        mod_id="root",
                        title="Root Mod",
                        page_url="https://mods.factorio.com/mod/root",
                        file_name="root_1.1.0.zip",
                        version="1.1.0",
                        required_by=(),
                        dependency_ids=("dep-new", "dep-old"),
                    ),
                    FactorioModPortalCandidate(
                        mod_id="dep-new",
                        title="New Dependency",
                        page_url="https://mods.factorio.com/mod/dep-new",
                        file_name="dep-new_1.0.0.zip",
                        version="1.0.0",
                        required_by=("root",),
                        dependency_ids=(),
                    ),
                    FactorioModPortalCandidate(
                        mod_id="dep-old",
                        title="Old Dependency",
                        page_url="https://mods.factorio.com/mod/dep-old",
                        file_name="dep-old_2.0.0.zip",
                        version="2.0.0",
                        required_by=("root",),
                        dependency_ids=(),
                    ),
                ),
            )

            async def fake_download(
                *,
                page_url: str,
                destination_dir: Path,
                factorio_version: AppVersion | None,
                credentials: object,
                selected_mod_ids: tuple[str, ...],
                requested_mod_version: str | None,
            ) -> tuple[FactorioModPortalDownload, ...]:
                del credentials
                self.assertEqual(page_url, "https://mods.factorio.com/mod/root")
                self.assertEqual(factorio_version, AppVersion(main="2.0.68"))
                self.assertEqual(selected_mod_ids, ("root", "dep-new", "dep-old"))
                self.assertIsNone(requested_mod_version)
                file_names_by_id = {
                    "root": "root_1.1.0.zip",
                    "dep-new": "dep-new_1.0.0.zip",
                    "dep-old": "dep-old_2.0.0.zip",
                }
                versions_by_id = {
                    "root": "1.1.0",
                    "dep-new": "1.0.0",
                    "dep-old": "2.0.0",
                }
                downloads: list[FactorioModPortalDownload] = []
                for mod_id in selected_mod_ids:
                    archive_path = destination_dir / file_names_by_id[mod_id]
                    archive_path.write_bytes(b"downloaded")
                    downloads.append(
                        FactorioModPortalDownload(
                            mod_id=mod_id,
                            page_url=f"https://mods.factorio.com/mod/{mod_id}",
                            file_name=file_names_by_id[mod_id],
                            version=versions_by_id[mod_id],
                            archive_path=archive_path,
                        )
                    )
                return tuple(downloads)

            with (
                patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)),
                patch("apps.factorio.node_api.factorio_mod_portal_credentials_from_server_settings", return_value=Mock()),
                patch("apps.factorio.node_api.download_factorio_mods_from_portal", new=AsyncMock(side_effect=fake_download)),
            ):
                result = asyncio.run(
                    NodeApiService().update_mod(
                        app=cast(App[Any], cast(object, app)),
                        mod_name=old_root_path.name,
                        actor_user_id=42,
                    )
                )

            self.assertEqual([await_call.args[0] for await_call in manager.remove.await_args_list], [old_root, old_dependency])
            self.assertEqual(manager.add.await_count, 3)
            self.assertEqual(
                tuple(mod.name for mod in result.mods),
                ("root_1.1.0.zip", "dep-new_1.0.0.zip", "dep-old_2.0.0.zip"),
            )
            self.assertIn("Updated 2 required dependencies", result.message)

    def test_update_mod_rejects_blocked_factorio_dependency(self) -> None:
        class _FactorioTestMod(_TestMod):
            def __init__(self, cfg: Mod_Config, *, native_id: str, nice_name: str) -> None:
                super().__init__(cfg, nice_name=nice_name)
                self._native_id = native_id

            def native_metadata_id(self) -> str:
                return self._native_id

        old_root = _FactorioTestMod(
            Mod_Config(
                name="root_1.0.0.zip",
                directory=Path("."),
                version="1.0.0",
            ),
            native_id="root",
            nice_name="Root Mod",
        )
        disabled_dependency = _FactorioTestMod(
            Mod_Config(
                name="dep_1.0.0.zip",
                directory=Path("."),
                placement=ModPlacement.SERVER_DISABLED,
                version="1.0.0",
            ),
            native_id="dep",
            nice_name="Dependency",
        )
        manager = Mock()
        manager.reload_mods = AsyncMock()
        manager.get.return_value = old_root
        manager.list_mods.return_value = (old_root, disabled_dependency)
        manager.remove = AsyncMock()
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            scope="factorio",
            directory=Path("."),
            mods=manager,
            has_mod_manager=manager,
            cfg=App_Config(
                name="factorio_lab",
                instance_key="lab",
                friendly_name="Factorio Lab",
                directory=Path("."),
                apps_dir=Path("."),
                scope="factorio",
                version=AppVersion(main="2.0.68"),
            ),
            detect_installed_version=Mock(return_value=AppVersion(main="2.0.68")),
            check_running=Mock(return_value=False),
        )
        resolution = FactorioModPortalResolution(
            requested_mod_id="root",
            candidates=(
                FactorioModPortalCandidate(
                    mod_id="root",
                    title="Root Mod",
                    page_url="https://mods.factorio.com/mod/root",
                    file_name="root_1.1.0.zip",
                    version="1.1.0",
                    required_by=(),
                    dependency_ids=("dep",),
                ),
                FactorioModPortalCandidate(
                    mod_id="dep",
                    title="Dependency",
                    page_url="https://mods.factorio.com/mod/dep",
                    file_name="dep_1.0.0.zip",
                    version="1.0.0",
                    required_by=("root",),
                    dependency_ids=(),
                ),
            ),
        )

        with (
            patch("apps.factorio.node_api.resolve_factorio_mod_portal_candidates", new=AsyncMock(return_value=resolution)),
            patch("apps.factorio.node_api.factorio_mod_portal_credentials_from_server_settings") as credentials,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    NodeApiService().update_mod(
                        app=cast(App[Any], cast(object, app)),
                        mod_name="root_1.0.0.zip",
                        actor_user_id=42,
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("required dependencies are blocked", str(raised.exception.detail))
        manager.remove.assert_not_awaited()
        credentials.assert_not_called()

    def test_update_mod_rejects_disabled_factorio_mods(self) -> None:
        mod = _TestMod(
            Mod_Config(
                name="root_1.0.0.zip",
                directory=Path("."),
                placement=ModPlacement.SERVER_DISABLED,
                version="1.0.0",
            ),
            nice_name="Root Mod",
        )
        manager = Mock()
        manager.reload_mods = AsyncMock()
        manager.get.return_value = mod
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            scope="factorio",
            mods=manager,
            has_mod_manager=manager,
            cfg=App_Config(
                name="factorio_lab",
                instance_key="lab",
                friendly_name="Factorio Lab",
                directory=Path("."),
                apps_dir=Path("."),
                scope="factorio",
            ),
            check_running=Mock(return_value=False),
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                NodeApiService().update_mod(
                    app=cast(App[Any], cast(object, app)),
                    mod_name="root_1.0.0.zip",
                    actor_user_id=42,
                )
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Only enabled mods", str(raised.exception.detail))
        manager.reload_mods.assert_awaited_once()

    def test_upload_mod_path_passes_client_only_placement_to_manager(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "upload.tmp"
            source.write_bytes(b"client-mod")
            mods_dir = root / "mods"
            mods_dir.mkdir()
            installed_path = mods_dir / "ClientMod.jar.client"
            installed_path.write_bytes(b"client-mod")
            installed = _TestMod(
                Mod_Config(
                    name="ClientMod.jar",
                    directory=mods_dir,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.add = AsyncMock(return_value=installed)
            app = _build_app(manager)

            result = asyncio.run(
                NodeApiService().upload_mod_path(
                    app=app,
                    source_path=source,
                    upload_name="ClientMod.jar",
                    actor_user_id=42,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )

        self.assertIs(result.mod.placement, ModPlacement.CLIENT_ONLY)
        manager.add.assert_awaited_once()
        self.assertIs(manager.add.await_args.kwargs["placement"], ModPlacement.CLIENT_ONLY)

    def test_upload_mod_path_rejects_directory_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "node-upload.tmp"
            source.write_bytes(b"mod-data")
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.add = AsyncMock()
            app = _build_app(manager)

            with self.assertRaises(Exception) as raised:
                asyncio.run(
                    NodeApiService().upload_mod_path(
                        app=app,
                        source_path=source,
                        upload_name="../CoolMod.jar",
                        actor_user_id=42,
                    )
                )

        self.assertEqual(getattr(raised.exception, "status_code"), 400)
        manager.add.assert_not_awaited()

    def test_upload_mod_path_rejects_parent_directory_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "node-upload.tmp"
            source.write_bytes(b"mod-data")
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.add = AsyncMock()
            app = _build_app(manager)

            with self.assertRaises(Exception) as raised:
                asyncio.run(
                    NodeApiService().upload_mod_path(
                        app=app,
                        source_path=source,
                        upload_name="..",
                        actor_user_id=42,
                    )
                )

        self.assertEqual(getattr(raised.exception, "status_code"), 400)
        manager.add.assert_not_awaited()

    def test_rename_save_file_returns_save_mutation_result(self) -> None:
        current = AppSaveEntry(
            id="saves/old.zip",
            label="old.zip",
            relative_path="old.zip",
            root_id="saves",
            root_label="Saves",
            kind=AppSaveEntryKind.FILE,
            size_bytes=8,
            modified_at=datetime(2026, 5, 30, 12, 0, 0),
        )
        renamed = AppSaveEntry(
            id="saves/new.zip",
            label="new.zip",
            relative_path="new.zip",
            root_id="saves",
            root_label="Saves",
            kind=AppSaveEntryKind.FILE,
            size_bytes=8,
            modified_at=datetime(2026, 5, 30, 12, 1, 0),
        )
        app = SimpleNamespace(
            name="factorio_lab",
            friendly="Factorio Lab",
            supports_save_rename=True,
            list_save_files_async=AsyncMock(return_value=(current,)),
            relocate_save_file_async=AsyncMock(return_value=renamed),
        )

        result = asyncio.run(
            NodeApiService().rename_save_file(
                app=cast(Any, app),
                save_id="saves/old.zip",
                new_name="new.zip",
                actor_user_id=42,
            )
        )

        self.assertIsInstance(result, NodeSaveMutationResult)
        self.assertEqual(result.save.id, "saves/new.zip")
        app.relocate_save_file_async.assert_awaited_once_with(
            save_id="saves/old.zip",
            destination_root_id="saves",
            destination_relative_path="new.zip",
        )

    def test_delete_save_file_returns_save_mutation_result(self) -> None:
        deleted = AppSaveEntry(
            id="world/world",
            label="world",
            relative_path="world",
            root_id="world",
            root_label="Current World",
            kind=AppSaveEntryKind.DIRECTORY,
            size_bytes=0,
            modified_at=datetime(2026, 6, 16, 12, 0, 0),
        )
        app = SimpleNamespace(
            name="sevendays_alpha",
            friendly="7D2D Alpha",
            supports_save_delete=True,
            delete_save_file_async=AsyncMock(return_value=deleted),
        )

        result = asyncio.run(
            NodeApiService().delete_save_file(
                app=cast(Any, app),
                save_id="world/world",
                actor_user_id=42,
            )
        )

        self.assertIsInstance(result, NodeSaveMutationResult)
        self.assertEqual(result.save.id, "world/world")
        self.assertTrue(result.save.can_delete)
        app.delete_save_file_async.assert_awaited_once_with(file_id="world/world")

    def test_update_setting_and_save_settings_use_attached_settings_manager(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        int,
                        "Max Players",
                        "max_players",
                        value="8",
                        choice_spec=ChoiceSpec(ChoiceOption("8"), ChoiceOption("12"), ChoiceOption("16")),
                    )
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            acl = Access_Control(users_pointer)
            service = NodeApiService()
            service.set_acl(acl)

            update_result = asyncio.run(
                service.update_setting(app=app, setting_key="max_players", value="12", actor_user_id=42)
            )
            save_result = asyncio.run(service.save_settings(app=app, actor_user_id=42))
            settings_app.loaded = False
            reload_result = asyncio.run(service.reload_settings(app=app, actor_user_id=42))

        self.assertEqual(update_result.setting.value_text, "12")
        self.assertTrue(settings_app.saved)
        self.assertTrue(settings_app.loaded)
        self.assertEqual(save_result.message, "Saved settings for Minecraft Alpha.")
        self.assertEqual(reload_result.message, "Minecraft Alpha settings reloaded from disk.")

    def test_save_settings_only_requires_callers_pending_setting_level(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        int,
                        "Admin Slots",
                        "admin_slots",
                        value="4",
                        power_level=Power_Level.sudo,
                    )
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42], "sudo": [84]}', encoding="utf-8")
            acl = Access_Control(users_pointer)
            service = NodeApiService()
            service.set_acl(acl)

            asyncio.run(service.update_setting(app=app, setting_key="admin_slots", value="6", actor_user_id=84))

            save_result = asyncio.run(service.save_settings(app=app, actor_user_id=42))

            sudo_save_result = asyncio.run(service.save_settings(app=app, actor_user_id=84))

        self.assertTrue(settings_app.saved)
        self.assertEqual(save_result.message, "Saved settings for Minecraft Alpha.")
        self.assertEqual(sudo_save_result.message, "Saved settings for Minecraft Alpha.")

    def test_reload_settings_only_requires_callers_pending_setting_level(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [
                    _setting(
                        int,
                        "Admin Slots",
                        "admin_slots",
                        value="4",
                        power_level=Power_Level.sudo,
                    )
                ],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42], "sudo": [84]}', encoding="utf-8")
            acl = Access_Control(users_pointer)
            service = NodeApiService()
            service.set_acl(acl)

            asyncio.run(service.update_setting(app=app, setting_key="admin_slots", value="6", actor_user_id=84))

            reload_result = asyncio.run(service.reload_settings(app=app, actor_user_id=42))

            sudo_reload_result = asyncio.run(service.reload_settings(app=app, actor_user_id=84))

        self.assertTrue(settings_app.loaded)
        self.assertEqual(reload_result.message, "Minecraft Alpha settings reloaded from disk.")
        self.assertEqual(sudo_reload_result.message, "Minecraft Alpha settings reloaded from disk.")

    def test_node_app_entry_uses_safe_config_level_defaults(self) -> None:
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "supports_configs": True,
            }
        )

        self.assertEqual(entry.config_read_level, Power_Level.sudo)
        self.assertEqual(entry.config_write_level, Power_Level.root)
        self.assertEqual(entry.save_write_level, Power_Level.sudo)
        self.assertTrue(entry.enabled)
        self.assertFalse(entry.supports_chat)
        self.assertIsNone(entry.map_url)
        self.assertIsNone(entry.join_address)
        self.assertIsNone(entry.join_direct_ip_address)

    def test_node_app_entry_accepts_canonical_power_level_parser_inputs(self) -> None:
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "supports_configs": True,
                "config_read_level": "admins",
                "config_write_level": 3,
            }
        )

        self.assertEqual(entry.config_read_level, Power_Level.admin)
        self.assertEqual(entry.config_write_level, Power_Level.root)
        self.assertEqual(entry.save_write_level, Power_Level.sudo)
        self.assertTrue(entry.enabled)

    def test_node_app_entry_round_trips_map_url(self) -> None:
        map_url = "https://example.invalid/squaremap/?world=minecraft_overworld"
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "supports_configs": True,
                "map_url": map_url,
            }
        )

        self.assertEqual(entry.map_url, map_url)
        self.assertEqual(entry.to_mapping()["map_url"], map_url)

    def test_node_app_entry_round_trips_client_pack_kubejs_scripts(self) -> None:
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "client_pack_kubejs_scripts": [
                    {"relative_path": "server_scripts/events.js", "included": True},
                    {"relative_path": "startup_scripts/registry.js", "included": False},
                ],
            }
        )

        self.assertEqual(
            entry.client_pack_kubejs_scripts,
            (
                ClientPackKubeJsScript(
                    relative_path="server_scripts/events.js",
                    included=True,
                ),
                ClientPackKubeJsScript(
                    relative_path="startup_scripts/registry.js",
                    included=False,
                ),
            ),
        )
        self.assertEqual(
            entry.to_mapping()["client_pack_kubejs_scripts"],
            [
                {"relative_path": "server_scripts/events.js", "included": True},
                {"relative_path": "startup_scripts/registry.js", "included": False},
            ],
        )

    def test_node_app_entry_round_trips_client_pack_metadata(self) -> None:
        metadata = ClientPackMetadataConfig(
            name="Example Pack",
            description="Example description",
            filename_template="{pack_name}-{version}",
        )
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "client_pack_metadata": metadata.model_dump(mode="json"),
            }
        )

        self.assertEqual(entry.client_pack_metadata, metadata)
        self.assertEqual(
            entry.to_mapping()["client_pack_metadata"],
            metadata.model_dump(mode="json"),
        )

    def test_node_app_entry_round_trips_client_pack_automated_changelog(self) -> None:
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "client_pack_automated_changelog": "Added mods:\n- Sodium (0.5.11)",
            }
        )

        self.assertEqual(entry.client_pack_automated_changelog, "Added mods:\n- Sodium (0.5.11)")
        self.assertEqual(
            entry.to_mapping()["client_pack_automated_changelog"],
            "Added mods:\n- Sodium (0.5.11)",
        )

    def test_client_pack_automated_changelog_reports_added_removed_and_updated_mods(self) -> None:
        text = NodeApiService._client_pack_automated_changelog_text(
            current=(
                ClientPackModSnapshot(name="alpha.jar", friendly="Alpha", version="2.0.0"),
                ClientPackModSnapshot(name="gamma.jar", friendly="Gamma", version=None),
            ),
            published=(
                ClientPackModSnapshot(name="alpha.jar", friendly="Alpha", version="1.0.0"),
                ClientPackModSnapshot(name="beta.jar", friendly="Beta", version="1.0.0"),
            ),
            has_published_pack=True,
        )

        self.assertEqual(
            text,
            "Added mods:\n"
            "- Gamma\n\n"
            "Removed mods:\n"
            "- Beta (1.0.0)\n\n"
            "Updated mods:\n"
            "- Alpha: 1.0.0 -> 2.0.0",
        )

    def test_client_pack_automated_changelog_treats_disabled_mod_as_removed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "enabled.jar").write_bytes(b"enabled")
            (root / "disabled.jar.disabled").write_bytes(b"disabled")
            enabled = _TestMod(
                Mod_Config(
                    name="enabled.jar",
                    directory=root,
                    metadata_overrides=ModMetadataOverrides(friendly_name="Enabled Mod", version="1.0.0"),
                )
            )
            disabled = _TestMod(
                Mod_Config(
                    name="disabled.jar",
                    directory=root,
                    placement=ModPlacement.SERVER_DISABLED,
                    metadata_overrides=ModMetadataOverrides(friendly_name="Disabled Mod", version="1.0.0"),
                )
            )
            manager = Mock()
            manager.list_mods.return_value = (enabled, disabled)
            manager.get.side_effect = {enabled.name: enabled, disabled.name: disabled}.__getitem__
            app = _build_app(manager)
            app.directory = root
            app.cfg.directory = root
            app.cfg.apps_dir = root
            service = NodeApiService()

            current = service._default_client_pack_mod_snapshots(app)

        text = NodeApiService._client_pack_automated_changelog_text(
            current=current,
            published=(
                ClientPackModSnapshot(name="enabled.jar", friendly="Enabled Mod", version="1.0.0"),
                ClientPackModSnapshot(name="disabled.jar", friendly="Disabled Mod", version="1.0.0"),
            ),
            has_published_pack=True,
        )

        self.assertEqual(current, (ClientPackModSnapshot(name="enabled.jar", friendly="Enabled Mod", version="1.0.0"),))
        self.assertEqual(text, "Removed mods:\n- Disabled Mod (1.0.0)")

    def test_client_pack_automated_changelog_matches_versioned_filename_changes_by_friendly_name(self) -> None:
        text = NodeApiService._client_pack_automated_changelog_text(
            current=(
                ClientPackModSnapshot(name="alpha-2.0.0.jar", friendly="Alpha", version="2.0.0"),
                ClientPackModSnapshot(name="gamma.jar", friendly="Gamma", version=None),
            ),
            published=(
                ClientPackModSnapshot(name="alpha-1.0.0.jar", friendly="Alpha", version="1.0.0"),
                ClientPackModSnapshot(name="beta.jar", friendly="Beta", version="1.0.0"),
            ),
            has_published_pack=True,
        )

        self.assertEqual(
            text,
            "Added mods:\n"
            "- Gamma\n\n"
            "Removed mods:\n"
            "- Beta (1.0.0)\n\n"
            "Updated mods:\n"
            "- Alpha: 1.0.0 -> 2.0.0; file alpha-1.0.0.jar -> alpha-2.0.0.jar",
        )

    def test_node_app_entry_round_trips_join_addresses(self) -> None:
        entry = NodeAppEntry.from_mapping(
            {
                "name": "minecraft_alpha",
                "friendly": "Minecraft Alpha",
                "node": "erin",
                "supports_mods": True,
                "supports_configs": True,
                "join_address": "play.example.test:25565",
                "join_direct_ip_address": "203.0.113.10:25565",
            }
        )

        self.assertEqual(entry.join_address, "play.example.test:25565")
        self.assertEqual(entry.join_direct_ip_address, "203.0.113.10:25565")
        self.assertEqual(entry.to_mapping()["join_address"], "play.example.test:25565")
        self.assertEqual(entry.to_mapping()["join_direct_ip_address"], "203.0.113.10:25565")

    def test_missing_route_app_name_parses_app_paths(self) -> None:
        self.assertEqual(
            NodeApiService._missing_route_app_name("apps/minecraft_alpha/map/assets/icon/registered/spawn.png"),
            "minecraft_alpha",
        )
        self.assertIsNone(NodeApiService._missing_route_app_name("ping"))

    def test_missing_route_warning_is_suppressed_for_stopped_apps(self) -> None:
        app = _build_app(Mock())
        service = NodeApiService()
        service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app})))

        self.assertFalse(service._should_log_missing_route_warning("apps/minecraft_alpha/map/assets/spawn.png"))

    def test_missing_route_warning_is_kept_for_running_apps(self) -> None:
        app = _build_app(Mock())
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        service = NodeApiService()
        service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app})))

        self.assertTrue(service._should_log_missing_route_warning("apps/minecraft_alpha/map/assets/spawn.png"))

    def test_list_apps_includes_chat_support_flag(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_overworld"

        app = _build_app(Mock())
        app.__class__ = _MappedApp
        app.am_receiver = _DummyReceiver()
        app.chat_relay_outbound = True
        service = NodeApiService()
        service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app})))

        entries = asyncio.run(service.list_apps())

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].enabled)
        self.assertTrue(entries[0].supports_chat)
        self.assertEqual(entries[0].scope, "minecraft")
        self.assertEqual(entries[0].map_url, "https://example.invalid/squaremap/?world=minecraft_overworld")

    def test_list_apps_includes_update_info(self) -> None:
        app = _build_app(Mock())
        app.updater = Mock()
        app.updater.info.return_value = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            app_id=294420,
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
        )
        app.updater.status.return_value = AppUpdateStatus(
            state=AppUpdateState.RUNNING,
            summary="Downloading",
            operation_kind=AppUpdateOperationKind.UPDATE,
            progress_percent=42.5,
        )
        service = NodeApiService()
        service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app})))

        entries = asyncio.run(service.list_apps())

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].supports_updates)
        self.assertIsNotNone(entries[0].update_info)
        self.assertEqual(entries[0].update_info.selected_branch_label, "Stable")  # type: ignore[union-attr]
        self.assertIsNotNone(entries[0].update_status)
        self.assertEqual(entries[0].update_status.progress_percent, 42.5)  # type: ignore[union-attr]

    def test_build_app_entry_captures_dashboard_metadata(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_overworld"

        app = _build_app(Mock())
        app.__class__ = _MappedApp
        app.cfg.title_font_preset = AppTitleFont.MINECRAFT_TEN.value
        app.cfg.notes = "Keep two slots reserved for staff."
        app.cfg.lifecycle_notice_started = False
        app.cfg.lifecycle_notice_stopped = True
        app.cfg.lifecycle_notice_crashed = False
        app.cfg.client_pack_content_dirty = True
        app.cfg.client_pack_published_changelog = "Added client performance fixes."
        app.cfg.join_host = "play.example.test"
        app.cfg.join_port = 25565
        app.cfg.resource_points = SimpleNamespace(
            running=SimpleNamespace(cpu_points=3, ram_points=6),
            startup_points=SimpleNamespace(cpu_points=5, ram_points=8),
            startup=True,
        )
        app.chat_relay_outbound = True
        app.am_receiver = _DummyReceiver()

        with (
            patch.object(config, "PUBLIC_ADDR", "play.example.test"),
            patch.object(config, "PUBLIC_IP", "203.0.113.10"),
        ):
            entry = NodeApiService().build_app_entry(
                app,
                transition_state=NodeAppTransitionState.STARTING,
                player_count=2,
                player_capacity=8,
                connected_player_names=("Alice", "Bob"),
            )

        self.assertEqual(entry.name, app.name)
        self.assertEqual(entry.node, config.MOD_WEB_SERVER.node_name)
        self.assertEqual(entry.transition_state, NodeAppTransitionState.STARTING)
        self.assertEqual(entry.player_count, 2)
        self.assertEqual(entry.player_capacity, 8)
        self.assertEqual(entry.connected_player_names, ("Alice", "Bob"))
        self.assertTrue(entry.supports_chat)
        self.assertEqual(entry.map_url, "https://example.invalid/squaremap/?world=minecraft_overworld")
        self.assertEqual(entry.join_address, "play.example.test:25565")
        self.assertEqual(entry.join_direct_ip_address, "203.0.113.10:25565")
        self.assertEqual(entry.title_font_preset, AppTitleFont.MINECRAFT_TEN.value)
        self.assertEqual(entry.notes, "Keep two slots reserved for staff.")
        self.assertFalse(entry.lifecycle_notice_started)
        self.assertTrue(entry.lifecycle_notice_stopped)
        self.assertFalse(entry.lifecycle_notice_crashed)
        self.assertTrue(entry.client_pack_content_dirty)
        self.assertEqual(entry.client_pack_published_changelog, "Added client performance fixes.")
        self.assertIsNone(entry.relay_notice_player_session)
        self.assertIsNone(entry.relay_notice_player_death)
        self.assertIsNone(entry.relay_notice_progress)
        self.assertIsNone(entry.relay_notice_progress_label)
        self.assertIsNone(entry.relay_advancements_enabled)
        self.assertIsNone(entry.relay_advancement_term)
        self.assertIsNotNone(entry.resource_points)
        self.assertEqual(entry.resource_points.cpu_points_running if entry.resource_points is not None else None, 3)
        self.assertEqual(entry.resource_points.cpu_points_startup if entry.resource_points is not None else None, 5)

    def test_build_app_entry_captures_relay_advancement_metadata(self) -> None:
        class _RelayApp(_DummyApp):
            @property
            def relay_advancements_enabled(self) -> bool | None:
                return bool(getattr(self, "_relay_advancements_enabled_state", False))

            @property
            def relay_advancement_term(self) -> str:
                return "Advancement"

        app = _build_app(Mock())
        app.__class__ = _RelayApp
        app._relay_advancements_enabled_state = False

        entry = NodeApiService().build_app_entry(app)

        self.assertFalse(entry.relay_advancements_enabled)
        self.assertEqual(entry.relay_advancement_term, "Advancement")

    def test_build_app_entry_captures_rcon_player_gate_metadata(self) -> None:
        action = ConsoleAction(
            key="say",
            label="Say",
            description="Broadcast to all players.",
            power_level=Power_Level.user,
            execute=AsyncMock(return_value=ConsoleActionResult(summary="ok")),
            transport=ConsoleResponseSource.RCON,
        )
        app = _build_console_action_app(actions=(action,))
        app.rcon_requires_online_players_default = True

        entry = NodeApiService().build_app_entry(app)

        self.assertTrue(entry.rcon_requires_online_players)

    def test_build_app_entry_captures_activity_provider_metadata(self) -> None:
        class _ActivityProvider(AppActivityProvider):
            metadata = AppActivityProviderMetadata(provider_id="day", label="Day Counter")

            async def get(self) -> str | None:
                return None

        app = _build_app(Mock())
        app.set_activity_providers((_ActivityProvider(app),))
        app.cfg.disabled_activity_provider_ids = ("day",)

        entry = NodeApiService().build_app_entry(app)

        self.assertEqual(len(entry.activity_providers), 1)
        self.assertEqual(entry.activity_providers[0].provider_id, "day")
        self.assertEqual(entry.activity_providers[0].label, "Day Counter")
        self.assertFalse(entry.activity_providers[0].enabled)

    def test_app_activity_provider_requires_explicit_metadata(self) -> None:
        with self.assertRaises(TypeError):

            class _InvalidActivityProvider(AppActivityProvider):
                async def get(self) -> str | None:
                    return None

    def test_build_app_entry_captures_generic_relay_notice_metadata(self) -> None:
        class _RelayNoticeApp(_DummyApp):
            relay_notice_player_session_supported = True
            relay_notice_player_death_supported = True
            relay_notice_progress_supported = True

            @property
            def relay_progress_notice_term(self) -> str:
                return "Research"

        app = _build_app(Mock())
        app.__class__ = _RelayNoticeApp
        app.cfg.relay_notice_player_session = False
        app.cfg.relay_notice_player_death = False
        app.cfg.relay_notice_progress = False

        entry = NodeApiService().build_app_entry(app)

        self.assertFalse(entry.relay_notice_player_session)
        self.assertFalse(entry.relay_notice_player_death)
        self.assertFalse(entry.relay_notice_progress)
        self.assertEqual(entry.relay_notice_progress_label, "Research")

    def test_build_map_manifest_uses_squaremap_settings_and_initial_world(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_nether"

            @property
            def map_proxy_url(self) -> str | None:
                return "http://localhost:8080"

        app = _build_app(Mock())
        app.__class__ = _MappedApp
        response = Mock(status_code=200)
        response.content = (
            b'{"worlds":[{"name":"minecraft_overworld","display_name":"Overworld","type":"normal","order":2},'
            b'{"name":"minecraft_nether","display_name":"Nether","type":"nether","order":1}]}'
        )
        response.headers = {"Content-Type": "application/json"}

        with patch("node_api.requests.get", return_value=response) as get_mock:
            manifest = NodeApiService().build_map_manifest(app)

        self.assertEqual(manifest.initial_world_name, "minecraft_nether")
        self.assertEqual([world.name for world in manifest.worlds], ["minecraft_nether", "minecraft_overworld"])
        self.assertEqual(manifest.icon_base_url, "./assets")
        self.assertEqual(get_mock.call_args.args[0], "http://localhost:8080/tiles/settings.json")

    def test_map_annotation_creator_name_prefers_minecraft_alias(self) -> None:
        app = _build_app(Mock())
        user = SimpleNamespace(username="discord_user")
        relay_display_name = Mock(return_value="Yoko")

        with patch(
            "node_api.config.Name_Cache",
            return_value=SimpleNamespace(discord_fallback_name=relay_display_name),
        ):
            created_by_name = NodeApiService._map_annotation_creator_name(app, actor_user_id=42, user=cast(Any, user))

        self.assertEqual(created_by_name, "Yoko")
        relay_display_name.assert_called_once_with(
            42,
            "discord_user",
            scope=app.scope,
            fallback_display_name="discord_user",
        )

    def test_map_annotation_creator_name_falls_back_to_discord_username(self) -> None:
        app = _build_app(Mock())
        user = SimpleNamespace(username="discord_user")

        with patch(
            "node_api.config.Name_Cache",
            return_value=SimpleNamespace(discord_fallback_name=Mock(return_value="discord_user")),
        ):
            created_by_name = NodeApiService._map_annotation_creator_name(app, actor_user_id=42, user=cast(Any, user))

        self.assertEqual(created_by_name, "discord_user")

    def test_build_map_manifest_reads_local_squaremap_root_when_available(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_nether"

            @property
            def map_proxy_root_path(self) -> Path | None:
                return self.directory

        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir)
            tiles_path = root_path / "tiles"
            tiles_path.mkdir(parents=True, exist_ok=True)
            (tiles_path / "settings.json").write_text(
                json.dumps(
                    {
                        "worlds": [
                            {
                                "name": "minecraft_overworld",
                                "display_name": "Overworld",
                                "type": "normal",
                                "order": 2,
                            },
                            {
                                "name": "minecraft_nether",
                                "display_name": "Nether",
                                "type": "nether",
                                "order": 1,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            app = _build_app(Mock())
            app.__class__ = _MappedApp
            app.directory = root_path
            with patch("node_api.requests.get") as get_mock:
                manifest = NodeApiService().build_map_manifest(app)

        self.assertEqual(manifest.initial_world_name, "minecraft_nether")
        self.assertEqual([world.name for world in manifest.worlds], ["minecraft_nether", "minecraft_overworld"])
        get_mock.assert_not_called()

    def test_squaremap_proxy_response_reads_local_asset_when_available(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def map_proxy_root_path(self) -> Path | None:
                return self.directory

        with TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir)
            asset_path = root_path / "images" / "icon" / "registered"
            asset_path.mkdir(parents=True, exist_ok=True)
            expected_content = b"png-bits"
            (asset_path / "squaremap-spawn_icon.png").write_bytes(expected_content)
            app = _build_app(Mock())
            app.__class__ = _MappedApp
            app.directory = root_path
            with patch("node_api.requests.get") as get_mock:
                response = NodeApiService()._squaremap_proxy_response(
                    app,
                    "images/icon/registered/squaremap-spawn_icon.png",
                )

        self.assertEqual(response.content, expected_content)
        self.assertEqual(response.media_type, "image/png")
        get_mock.assert_not_called()

    def test_build_map_manifest_uses_cached_squaremap_settings_when_upstream_is_unavailable(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_nether"

        live_response = Mock(status_code=200)
        live_response.content = (
            b'{"worlds":[{"name":"minecraft_overworld","display_name":"Overworld","type":"normal","order":2},'
            b'{"name":"minecraft_nether","display_name":"Nether","type":"nether","order":1}]}'
        )
        live_response.headers = {"Content-Type": "application/json"}

        with TemporaryDirectory() as temp_dir:
            app = _build_app(Mock())
            app.__class__ = _MappedApp
            app.directory = Path(temp_dir)
            service = NodeApiService()
            with patch("node_api.requests.get", return_value=live_response):
                service.build_map_manifest(app)
            with patch("node_api.requests.get", side_effect=requests.ConnectionError("offline")):
                cached_manifest = service.build_map_manifest(app)

        self.assertEqual(cached_manifest.initial_world_name, "minecraft_nether")
        self.assertEqual([world.name for world in cached_manifest.worlds], ["minecraft_nether", "minecraft_overworld"])

    def test_squaremap_proxy_response_uses_cached_world_settings_when_upstream_is_unavailable(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_overworld"

        live_response = Mock(status_code=200)
        live_response.content = (
            b'{"spawn":{"x":0,"z":0},"zoom":{"def":1,"max":4,"extra":2},"player_tracker":{"enabled":true}}'
        )
        live_response.headers = {"Content-Type": "application/json"}

        with TemporaryDirectory() as temp_dir:
            app = _build_app(Mock())
            app.__class__ = _MappedApp
            app.directory = Path(temp_dir)
            service = NodeApiService()
            with patch("node_api.requests.get", return_value=live_response):
                service._squaremap_proxy_response(
                    app,
                    "tiles/minecraft_overworld/settings.json",
                    allow_stale_on_error=True,
                )
            with patch("node_api.requests.get", side_effect=requests.Timeout("slow")):
                cached_response = service._squaremap_proxy_response(
                    app,
                    "tiles/minecraft_overworld/settings.json",
                    allow_stale_on_error=True,
                )

        self.assertTrue(cached_response.is_stale)
        self.assertEqual(
            json.loads(cached_response.content.decode("utf-8")),
            {"spawn": {"x": 0, "z": 0}, "zoom": {"def": 1, "max": 4, "extra": 2}, "player_tracker": {"enabled": True}},
        )
        self.assertIsNotNone(cached_response.cache_updated_at_unix_ms)

    def test_map_annotation_round_trip_uses_shared_store(self) -> None:
        class _MappedApp(_DummyApp):
            @property
            def public_map_url(self) -> str | None:
                return "https://example.invalid/squaremap/?world=minecraft_overworld"

        with TemporaryDirectory() as temp_dir:
            app = _build_app(Mock())
            app.__class__ = _MappedApp
            app.directory = Path(temp_dir)
            service = NodeApiService()
            draft = MapAnnotationDraft.from_mapping(
                {
                    "world_name": "minecraft_overworld",
                    "shape": "marker",
                    "label": "Home Base",
                    "color_hex": "#22C55E",
                    "points": [{"x": 12, "z": -48}],
                }
            )

            created = service.create_map_annotation(app, draft, 42, "Taylor")
            listed = service.build_map_annotation_list(app)
            self.assertIsNotNone(created.annotation)
            assert created.annotation is not None
            deleted = service.delete_map_annotation(app, created.annotation.annotation_id)
            after_delete = service.build_map_annotation_list(app)

        self.assertEqual(created.annotation.label, "Home Base")
        self.assertEqual(created.annotation.created_by_name, "Taylor")
        self.assertEqual(len(listed.annotations), 1)
        self.assertEqual(deleted.deleted_annotation_id, created.annotation.annotation_id)
        self.assertEqual(after_delete.annotations, ())

    def test_list_apps_includes_player_counts_for_running_apps(self) -> None:
        app = _build_app(Mock())
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        app.player_count = AsyncMock(return_value=(3, 20))  # type: ignore[method-assign]
        service = NodeApiService()
        service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app})))

        entries = asyncio.run(service.list_apps())

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].player_count, 3)
        self.assertEqual(entries[0].player_capacity, 20)

    def test_list_apps_includes_cached_transition_state(self) -> None:
        app = _build_app(Mock())
        service = NodeApiService()
        service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app})))
        service._remember_app_transition_state(app.name, NodeAppTransitionState.STARTING)

        entries = asyncio.run(service.list_apps())

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].transition_state, NodeAppTransitionState.STARTING)

    def test_build_chat_room_snapshot_returns_limited_history(self) -> None:
        app = _build_app(Mock())
        app.am_receiver = _DummyReceiver()
        app.chat_relay_outbound = True
        hub = ChatHub()
        hub.clear_room(app.name)
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), app.friendly))
        first_event = ChatEvent(
            room_id=app.name,
            source=ChatEndpointId.app(app.name),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="one",
        )
        second_event = ChatEvent(
            room_id=app.name,
            source=ChatEndpointId.app(app.name),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="two",
        )
        hub.publish(first_event)
        hub.publish(second_event)
        service = NodeApiService()

        try:
            snapshot = service.build_chat_room_snapshot(app, limit=1)
        finally:
            hub.clear_room(app.name)

        self.assertEqual(snapshot.room_id, app.name)
        self.assertEqual(snapshot.endpoint_count, 0)
        self.assertEqual(snapshot.endpoint_summaries, ())
        self.assertEqual(snapshot.events, (second_event,))

    def test_node_web_chat_request_accepts_reply_target(self) -> None:
        request = NodeWebChatRequest.model_validate(
            {
                "session_id": "session-1",
                "author_display_name": "Tester",
                "content": "hello",
                "reply_to_event_id": "event-1",
            }
        )

        self.assertEqual(request.reply_to_event_id, "event-1")

    def test_node_web_chat_request_rejects_blank_reply_target(self) -> None:
        with self.assertRaises(ValueError):
            NodeWebChatRequest.model_validate(
                {
                    "session_id": "session-1",
                    "author_display_name": "Tester",
                    "content": "hello",
                    "reply_to_event_id": "   ",
                }
            )

    def test_serve_chat_stream_ignores_disconnect_while_closing_websocket(self) -> None:
        class _DisconnectingWebSocket:
            async def accept(self) -> None:
                return None

            async def receive(self) -> dict[str, str]:
                return {"type": "websocket.disconnect"}

            async def send_json(self, payload: object) -> None:
                del payload

            async def close(self) -> None:
                raise WebSocketDisconnect(code=1006)

        service = NodeApiService()
        service.build_chat_room_snapshot = Mock(
            return_value=NodeChatRoomSnapshot(room_id="minecraft_alpha", endpoint_count=0, events=())
        )  # type: ignore[method-assign]
        service.build_live_app_runtime_summary = AsyncMock(return_value=None)  # type: ignore[method-assign]
        service.subscribe_local_app_runtime = Mock(return_value=Mock())  # type: ignore[method-assign]
        app = _build_app(Mock())

        asyncio.run(service._serve_chat_stream(websocket=cast(Any, _DisconnectingWebSocket()), app=app))

    def test_serve_presence_stream_returns_pong_with_sample_id(self) -> None:
        sent_payloads: list[object] = []

        class _PresenceWebSocket:
            def __init__(self) -> None:
                self._messages = iter(
                    (
                        {"type": "websocket.receive", "text": json.dumps({"type": "ping", "sample_id": "sample-1"})},
                        {"type": "websocket.disconnect"},
                    )
                )

            async def accept(self) -> None:
                return None

            async def receive(self) -> dict[str, str]:
                return next(self._messages)

            async def send_json(self, payload: object) -> None:
                sent_payloads.append(payload)

            async def close(self) -> None:
                return None

        service = NodeApiService()

        asyncio.run(service._serve_presence_stream(websocket=cast(Any, _PresenceWebSocket())))

        self.assertEqual(
            sent_payloads,
            [{"type": "pong", "node": service.node_name, "sample_id": "sample-1"}],
        )

    def test_serve_presence_stream_ignores_disconnect_while_closing_websocket(self) -> None:
        class _DisconnectingWebSocket:
            async def accept(self) -> None:
                return None

            async def receive(self) -> dict[str, str]:
                return {"type": "websocket.disconnect"}

            async def send_json(self, payload: object) -> None:
                del payload

            async def close(self) -> None:
                raise WebSocketDisconnect(code=1006)

        service = NodeApiService()

        asyncio.run(service._serve_presence_stream(websocket=cast(Any, _DisconnectingWebSocket())))

    def test_build_chat_room_snapshot_counts_discord_guilds_as_separate_endpoints(self) -> None:
        app = _build_app(Mock())
        app.am_receiver = _DummyReceiver()
        app.chat_relay_outbound = True
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        hub = ChatHub()
        hub.clear_room(app.name)
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), app.friendly))
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.discord_channel("100"), "Discord 100"))
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.discord_channel("101"), "Discord 101"))
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.discord_channel("200"), "Discord 200"))
        service = NodeApiService()
        service.set_manager(
            cast(
                Any,
                SimpleNamespace(
                    bot=SimpleNamespace(
                        cache=SimpleNamespace(
                            get_guild_channel=lambda channel_id: {
                                100: SimpleNamespace(guild_id=1, name="relay-a"),
                                101: SimpleNamespace(guild_id=1, name="relay-b"),
                                200: SimpleNamespace(guild_id=2, name="relay-c"),
                            }.get(int(channel_id)),
                            get_guild=lambda guild_id: {
                                1: SimpleNamespace(name="Friends"),
                                2: SimpleNamespace(name="Builders"),
                            }.get(int(guild_id)),
                        )
                    )
                ),
            )
        )

        try:
            snapshot = service.build_chat_room_snapshot(app, limit=1)
        finally:
            hub.clear_room(app.name)

        self.assertEqual(snapshot.endpoint_count, 3)
        self.assertEqual(
            [summary.label for summary in snapshot.endpoint_summaries],
            [f"Game: {app.friendly}", "Discord: Friends", "Discord: Builders"],
        )

    def test_build_chat_room_snapshot_omits_game_endpoint_when_app_is_stopped(self) -> None:
        app = _build_app(Mock())
        app.am_receiver = _DummyReceiver()
        app.chat_relay_outbound = True
        hub = ChatHub()
        hub.clear_room(app.name)
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), app.friendly))
        hub.bind(app.name, ChatEndpoint(ChatEndpointId.discord_channel("100"), "Discord 100"))
        service = NodeApiService()
        service.set_manager(
            cast(
                Any,
                SimpleNamespace(
                    bot=SimpleNamespace(
                        cache=SimpleNamespace(
                            get_guild_channel=lambda channel_id: {
                                100: SimpleNamespace(guild_id=1, name="relay-a"),
                            }.get(int(channel_id)),
                            get_guild=lambda guild_id: {
                                1: SimpleNamespace(name="Friends"),
                            }.get(int(guild_id)),
                        )
                    )
                ),
            )
        )

        try:
            snapshot = service.build_chat_room_snapshot(app, limit=1)
        finally:
            hub.clear_room(app.name)

        self.assertEqual(snapshot.endpoint_count, 1)
        self.assertEqual([summary.label for summary in snapshot.endpoint_summaries], ["Discord: Friends"])

    def test_update_setting_allows_blank_string_for_freeform_text_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_pointer = root / "settings.json"
            settings_pointer.write_text("{}", encoding="utf-8")
            app = _build_app(Mock())
            settings_app = _DummySettingsApp(
                settings_pointer,
                [_setting(str, "Level Seed", "level-seed", value="8675309")],
            )
            _attach_settings(app, settings_app)

            users_pointer = root / "users.json"
            users_pointer.write_text('{"user": [42]}', encoding="utf-8")
            service = NodeApiService()
            service.set_acl(Access_Control(users_pointer))

            update_result = asyncio.run(
                service.update_setting(app=app, setting_key="level-seed", value="", actor_user_id=42)
            )

        self.assertEqual(update_result.setting.value_text, "")

    def test_actor_user_id_from_subject_requires_web_prefix(self) -> None:
        self.assertEqual(NodeApiService._actor_user_id_from_subject("web:42"), 42)
        with self.assertRaises(Exception) as raised:
            NodeApiService._actor_user_id_from_subject("relay-tts:erin")

        self.assertEqual(getattr(raised.exception, "status_code"), 403)

    def test_required_mod_mutation_level_allows_admin_toggle_for_regular_mods(self) -> None:
        self.assertEqual(required_mod_mutation_level(NodeModMutationAction.ENABLE), Power_Level.admin)
        self.assertEqual(required_mod_mutation_level(NodeModMutationAction.DISABLE), Power_Level.admin)
        self.assertEqual(
            required_mod_mutation_level(NodeModMutationAction.ENABLE, is_protected=True),
            Power_Level.sudo,
        )
        self.assertEqual(required_mod_mutation_level(NodeModMutationAction.DELETE), Power_Level.sudo)

    def test_mod_entry_includes_explicit_client_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mods_dir = root / "mods"
            mods_dir.mkdir()
            mod_path = mods_dir / "example.jar"
            mod_path.write_bytes(b"mod-data")
            client_mod_path = root / "client-mods" / "example.jar"
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=mods_dir, client_path=client_mod_path))

            entry = NodeApiService._mod_entry(mod)

        self.assertEqual(entry.client_path, str(client_mod_path))
        self.assertEqual(NodeModEntry.from_mapping(entry.to_mapping()).client_path, str(client_mod_path))

    def test_mod_entry_includes_launcher_description(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mods_dir = root / "mods"
            mods_dir.mkdir()
            mod_path = mods_dir / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(
                    name=mod_path.name,
                    directory=mods_dir,
                    platforms=ModPlatformMetadata(
                        modrinth=ModrinthModMetadata(
                            page_url="https://modrinth.com/mod/example/version/abc123",
                            project_id="example-project",
                            version_id="abc123",
                            download_url=("https://cdn.modrinth.com/data/example-project/versions/abc123/example.jar"),
                            description="Example launcher description.",
                            filename="example.jar",
                        )
                    ),
                )
            )

            entry = NodeApiService._mod_entry(mod)

        self.assertEqual(entry.description, "Example launcher description.")
        self.assertEqual(NodeModEntry.from_mapping(entry.to_mapping()).description, entry.description)

    def test_mod_entry_includes_local_mod_description(self) -> None:
        class _DescriptionMod(_TestMod):
            def detect_description(self) -> str | None:
                return "Example local description."

        with TemporaryDirectory() as temp_dir:
            mods_dir = Path(temp_dir)
            mod_path = mods_dir / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _DescriptionMod(Mod_Config(name="example.jar", directory=mods_dir))

            entry = NodeApiService._mod_entry(mod)

        self.assertEqual(entry.description, "Example local description.")
        self.assertEqual(NodeModEntry.from_mapping(entry.to_mapping()).description, entry.description)

    def test_mod_entry_exposes_client_only_placement_and_artifact_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mods_dir = root / "mods"
            mods_dir.mkdir()
            client_path = mods_dir / "example.jar.client"
            client_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(
                    name="example.jar",
                    directory=mods_dir,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )

            entry = NodeApiService._mod_entry(mod)

        self.assertIs(entry.placement, ModPlacement.CLIENT_ONLY)
        self.assertFalse(entry.server_loadable)
        self.assertTrue(entry.client_pack_eligible)
        self.assertEqual(entry.archive_name, "example.jar")
        self.assertEqual(entry.source_path, str(client_path))
        self.assertEqual(NodeModEntry.from_mapping(entry.to_mapping()), entry)

    def test_mod_entry_marks_server_disabled_mod_as_client_pack_ineligible(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mods_dir = Path(temp_dir)
            disabled_path = mods_dir / "example.jar.disabled"
            disabled_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(
                    name="example.jar",
                    directory=mods_dir,
                    placement=ModPlacement.SERVER_DISABLED,
                )
            )

            entry = NodeApiService._mod_entry(mod)

        self.assertFalse(entry.client_pack_eligible)
        self.assertFalse(NodeModEntry.from_mapping(entry.to_mapping()).client_pack_eligible)

    def test_mutate_mod_enable_requires_admin_for_regular_mods(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name="example.jar", directory=Path(temp_dir), enabled=False))
            updated = _TestMod(Mod_Config(name="example.jar", directory=Path(temp_dir), enabled=True))
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            manager.set_enabled = AsyncMock(return_value=updated)
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            result = asyncio.run(
                service.mutate_mod(
                    app=app,
                    mod_name="example.jar",
                    action=NodeModMutationAction.ENABLE,
                    actor_user_id=42,
                )
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.admin)
        manager.set_enabled.assert_awaited_once_with(mod, True, override_coremod=False)
        self.assertEqual(result.action, NodeModMutationAction.ENABLE)
        self.assertEqual(result.message, "Enabled example.jar.")

    def test_mutate_mod_enable_updates_manager_with_coremod_override(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(name="example.jar", directory=Path(temp_dir), enabled=False, mod_type=ModType.COREMOD)
            )
            updated = _TestMod(
                Mod_Config(name="example.jar", directory=Path(temp_dir), enabled=True, mod_type=ModType.COREMOD)
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            manager.set_enabled = AsyncMock(return_value=updated)
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))

            result = asyncio.run(
                service.mutate_mod(
                    app=app,
                    mod_name="example.jar",
                    action=NodeModMutationAction.ENABLE,
                    actor_user_id=42,
                )
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        manager.set_enabled.assert_awaited_once_with(mod, True, override_coremod=True)
        self.assertEqual(result.action, NodeModMutationAction.ENABLE)
        self.assertEqual(result.message, "Enabled example.jar.")

    def test_mutate_mod_enable_rejects_client_only_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod = _TestMod(
                Mod_Config(
                    name="client.jar",
                    directory=Path(temp_dir),
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            manager.set_enabled = AsyncMock()
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))

            with self.assertRaises(Exception) as raised:
                asyncio.run(
                    service.mutate_mod(
                        app=app,
                        mod_name=mod.name,
                        action=NodeModMutationAction.ENABLE,
                        actor_user_id=42,
                    )
                )

        self.assertEqual(getattr(raised.exception, "status_code"), 409)
        manager.set_enabled.assert_not_awaited()

    def test_mutate_mod_toggle_coremod_rejects_builtin_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "builtin.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(
                    name=mod_path.name,
                    directory=Path(temp_dir),
                    download_block_reason=ModDownloadBlockReason.BUILTIN,
                )
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))

            with self.assertRaises(Exception) as raised:
                asyncio.run(
                    service.mutate_mod(
                        app=app,
                        mod_name=mod.name,
                        action=NodeModMutationAction.TOGGLE_COREMOD,
                        actor_user_id=42,
                    )
                )

        self.assertEqual(getattr(raised.exception, "status_code"), 409)

    def test_mutate_mod_enable_blocks_when_target_app_is_running(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name="example.jar", directory=Path(temp_dir), enabled=False))
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            manager.set_enabled = AsyncMock()
            app = _build_app(manager)
            app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))

            with self.assertRaises(Exception) as raised:
                asyncio.run(
                    service.mutate_mod(
                        app=app,
                        mod_name="example.jar",
                        action=NodeModMutationAction.ENABLE,
                        actor_user_id=42,
                    )
                )

        self.assertEqual(getattr(raised.exception, "status_code"), 409)
        manager.set_enabled.assert_not_awaited()

    def test_update_mod_properties_persists_typed_values_with_sudo_access(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=Path(temp_dir)))
            overrides = ModMetadataOverrides(
                friendly_name="Example Override",
                version="3.0.0",
                origin="curated",
            )
            client_pack = ClientPackConfig(
                policy=ClientPackPolicy.OPTIONAL,
                default_selected=False,
            )
            mod_pages = (
                ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
            )
            updated = _TestMod(
                Mod_Config(
                    name=mod_path.name,
                    directory=Path(temp_dir),
                    mod_type=ModType.CLIENT,
                    download_block_reason=ModDownloadBlockReason.ARTIFACT,
                    mod_pages=mod_pages,
                    metadata_overrides=overrides,
                )
            )
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            manager.update_properties = AsyncMock(return_value=updated)
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))

            result = asyncio.run(
                service.update_mod_properties(
                    app=app,
                    mod_name=mod.name,
                    update=NodeModPropertiesUpdateRequest(
                        mod_type=ModType.CLIENT,
                        download_block_reason=ModDownloadBlockReason.ARTIFACT,
                        metadata_overrides=overrides,
                        mod_pages=mod_pages,
                        client_pack=client_pack,
                        launcher_urls=LauncherProviderUrls(),
                    ),
                    actor_user_id=42,
                )
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        manager.update_properties.assert_awaited_once_with(
            mod,
            mod_type=ModType.CLIENT,
            download_block_reason=ModDownloadBlockReason.ARTIFACT,
            metadata_overrides=overrides,
            mod_pages=mod_pages,
            client_pack=client_pack,
            platforms=ModPlatformMetadata(),
        )
        self.assertEqual(result.action, NodeModMutationAction.UPDATE_PROPERTIES)
        self.assertIsNotNone(result.mod)
        assert result.mod is not None
        self.assertEqual(result.mod.friendly, "Example Override")
        self.assertEqual(result.mod.version, "3.0.0")
        self.assertEqual(result.mod.metadata_overrides, overrides)
        self.assertEqual(result.mod.mod_pages, mod_pages)

    def test_fetch_mod_launcher_metadata_resolves_without_updating_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod = _TestMod(Mod_Config(name="client.jar", directory=Path(temp_dir)))
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            launcher_urls = LauncherProviderUrls(
                modrinth="https://modrinth.com/mod/client-mod/version/client-version"
            )
            expected = LauncherMetadataResolution(
                suggested_mod_type=ModType.CLIENT,
                suggestion_provider=Provider.MODRINTH,
            )

            with patch(
                "node_api.resolve_launcher_metadata_resolution",
                new=AsyncMock(return_value=expected),
            ) as resolve_metadata:
                result = asyncio.run(
                    service.fetch_mod_launcher_metadata(
                        app=app,
                        mod_name=mod.name,
                        fetch_request=NodeModMetadataFetchRequest(
                            launcher_urls=launcher_urls,
                            providers=(Provider.MODRINTH,),
                        ),
                        actor_user_id=42,
                    )
                )

        self.assertEqual(result, expected)
        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        resolve_metadata.assert_awaited_once_with(
            scope=app.scope,
            urls=launcher_urls,
            local_filename=mod.name,
            local_path=mod.storage_path,
            providers=(Provider.MODRINTH,),
        )
        manager.update_properties.assert_not_called()

    def test_fetch_mod_launcher_metadata_returns_404_when_mod_disappears(self) -> None:
        manager = Mock()
        manager.reload_mods = AsyncMock()
        manager.get.side_effect = ModuleNotFoundError("No such Mod: removed.jar")
        app = _build_app(manager)
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        with self.assertRaises(Exception) as raised:
            asyncio.run(
                service.fetch_mod_launcher_metadata(
                    app=app,
                    mod_name="removed.jar",
                    fetch_request=NodeModMetadataFetchRequest(
                        launcher_urls=LauncherProviderUrls()
                    ),
                    actor_user_id=42,
                )
            )

        self.assertEqual(getattr(raised.exception, "status_code"), 404)
        self.assertEqual(getattr(raised.exception, "detail"), "No such Mod: removed.jar")
        manager.reload_mods.assert_awaited_once_with()
        acl.perm_check.assert_not_awaited()

    def test_resolve_mod_launcher_metadata_uses_effective_pages_without_mutating_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=Path(temp_dir)))
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            app = _build_app(manager)
            app.cfg.version = AppVersion(main="1.20.1", loader="forge")
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            mod_pages = (
                ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
            )
            existing_urls = LauncherProviderUrls(
                curseforge="https://www.curseforge.com/minecraft/mc-mods/example/files/123"
            )
            expected = LauncherMetadataDiscovery()

            with patch(
                "node_api.discover_launcher_metadata",
                new=AsyncMock(return_value=expected),
            ) as discover_metadata:
                result = asyncio.run(
                    service.resolve_mod_launcher_metadata(
                        app=app,
                        mod_name=mod.name,
                        resolve_request=NodeModMetadataResolveRequest(
                            mod_pages=mod_pages,
                            existing_launcher_urls=existing_urls,
                            providers=(Provider.MODRINTH,),
                        ),
                        actor_user_id=42,
                    )
                )

        self.assertEqual(result, expected)
        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        discover_metadata.assert_awaited_once_with(
            scope=app.scope,
            mod_pages=mod_pages,
            existing_urls=existing_urls,
            local_path=mod.storage_path,
            local_filename=mod.name,
            game_version="1.20.1",
            loader="forge",
            providers=(Provider.MODRINTH,),
        )
        manager.update_properties.assert_not_called()

    def test_find_mod_pages_uses_local_mod_data_without_mutating_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=Path(temp_dir)))
            mod.friendly = "Example Mod"
            mod.cfg.version = "2.0.0"
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.return_value = mod
            app = _build_app(manager)
            app.cfg.version = AppVersion(main="1.20.1", loader="forge")
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            mod_pages = (
                ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
            )
            expected = ModPageDiscovery()

            with patch(
                "node_api.discover_mod_pages",
                new=AsyncMock(return_value=expected),
            ) as discover_pages:
                result = asyncio.run(
                    service.find_mod_pages(
                        app=app,
                        mod_name=mod.name,
                        resolve_request=NodeModPageResolveRequest(
                            mod_pages=mod_pages,
                            providers=(Provider.CURSEFORGE,),
                        ),
                        actor_user_id=42,
                    )
                )

        self.assertEqual(result, expected)
        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        discover_pages.assert_awaited_once_with(
            scope=app.scope,
            existing_mod_pages=mod_pages,
            local_path=mod.storage_path,
            local_filename=mod.name,
            friendly_name="Example Mod",
            detected_version="2.0.0",
            game_version="1.20.1",
            loader="forge",
            providers=(Provider.CURSEFORGE,),
        )
        manager.update_properties.assert_not_called()

    def test_bulk_metadata_discovery_uses_all_non_builtin_mods_without_mutating(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=Path(temp_dir)))
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.list_mods.return_value = [mod]
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            expected = BulkLauncherMetadataDiscovery()

            with patch(
                "node_api.discover_bulk_launcher_metadata",
                new=AsyncMock(return_value=expected),
            ) as discover_metadata:
                result = asyncio.run(
                    service.discover_bulk_mod_metadata(
                        app=app,
                        discovery_request=NodeBulkLauncherMetadataRequest(),
                        actor_user_id=42,
                    )
                )

        self.assertEqual(result, expected)
        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        targets = discover_metadata.await_args.kwargs["targets"]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].mod_name, mod.name)
        manager.apply_discovered_launcher_metadata.assert_not_called()

    def test_bulk_metadata_apply_uses_cached_exact_matches_without_provider_requests(self) -> None:
        with TemporaryDirectory() as temp_dir:
            exact_path = Path(temp_dir) / "exact.jar"
            unmatched_path = Path(temp_dir) / "unmatched.jar"
            exact_path.write_bytes(b"exact")
            unmatched_path.write_bytes(b"unmatched")
            exact_mod = _TestMod(Mod_Config(name=exact_path.name, directory=Path(temp_dir)))
            unmatched_mod = _TestMod(Mod_Config(name=unmatched_path.name, directory=Path(temp_dir)))
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.get.side_effect = lambda name: {
                exact_mod.name: exact_mod,
                unmatched_mod.name: unmatched_mod,
            }[name]
            manager.apply_discovered_launcher_metadata = AsyncMock(return_value=exact_mod)
            app = _build_app(manager)
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            exact_entry = BulkLauncherMetadataEntry(
                mod_name=exact_mod.name,
                friendly_name="Exact",
                status=BulkLauncherMetadataStatus.EXACT,
                platforms=ModPlatformMetadata(
                    modrinth=ModrinthModMetadata(
                        page_url="https://modrinth.com/mod/exact/version/version-id",
                        project_id="project-id",
                        version_id="version-id",
                        download_url="https://cdn.modrinth.com/exact.jar",
                    )
                ),
                suggested_mod_type=ModType.SERVER,
                matched_providers=(Provider.MODRINTH,),
            )
            unmatched_entry = BulkLauncherMetadataEntry(
                mod_name=unmatched_mod.name,
                friendly_name="Unmatched",
                status=BulkLauncherMetadataStatus.UNMATCHED,
            )
            discovery = BulkLauncherMetadataDiscovery(entries=(exact_entry, unmatched_entry))
            discovery_operation_id = uuid.UUID("c50f39cb-acde-441f-ab92-3fd507c7b294")
            service._cache_bulk_metadata_discovery(
                app_name=app.name,
                operation_id=discovery_operation_id,
                discovery=discovery,
            )

            with (
                patch(
                    "node_api.discover_bulk_launcher_metadata",
                    new=AsyncMock(),
                ) as discover_metadata,
                patch.object(service, "_invalidate_client_pack_content"),
                patch.object(service, "_invalidate_mod_inventory"),
            ):
                result = asyncio.run(
                    service.apply_bulk_mod_metadata(
                        app=app,
                        apply_request=NodeBulkLauncherMetadataApplyRequest(
                            discovery_operation_id=discovery_operation_id,
                            mod_names=(exact_mod.name,),
                            apply_suggested_type_mod_names=(exact_mod.name,),
                        ),
                        actor_user_id=42,
                    )
                )

        self.assertEqual(result.applied_mod_names, (exact_mod.name,))
        self.assertEqual(result.applied_type_mod_names, (exact_mod.name,))
        discover_metadata.assert_not_awaited()
        manager.apply_discovered_launcher_metadata.assert_awaited_once_with(
            exact_mod.name,
            exact_entry,
            apply_suggested_mod_type=True,
        )

    def test_bulk_metadata_apply_type_selections_must_be_selected_mods(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be selected for apply"):
            NodeBulkLauncherMetadataApplyRequest(
                discovery_operation_id=uuid.UUID("c50f39cb-acde-441f-ab92-3fd507c7b295"),
                mod_names=("metadata-only.jar",),
                apply_suggested_type_mod_names=("type-only.jar",),
            )

    def test_bulk_metadata_operation_can_be_cancelled_by_operation_id(self) -> None:
        service = NodeApiService()
        operation_id = uuid.UUID("c50f39cb-acde-441f-ab92-3fd507c7b293")

        async def exercise() -> None:
            action_started = asyncio.Event()

            async def action() -> BulkLauncherMetadataDiscovery:
                action_started.set()
                await asyncio.Event().wait()
                return BulkLauncherMetadataDiscovery()

            operation_task = asyncio.create_task(
                service._run_bulk_metadata_operation(
                    app_name="minecraft_alpha",
                    operation_id=operation_id,
                    action=action,
                )
            )
            await action_started.wait()

            self.assertTrue(
                service._cancel_bulk_metadata_operation(
                    app_name="minecraft_alpha",
                    operation_id=operation_id,
                )
            )
            with self.assertRaises(asyncio.CancelledError):
                await operation_task
            self.assertFalse(
                service._cancel_bulk_metadata_operation(
                    app_name="minecraft_alpha",
                    operation_id=operation_id,
                )
            )

        asyncio.run(exercise())

    def test_update_client_pack_config_uses_admin_access_and_single_bulk_write(self) -> None:
        with TemporaryDirectory() as temp_dir:
            alpha = _TestMod(Mod_Config(name="alpha.jar", directory=Path(temp_dir)))
            beta = _TestMod(Mod_Config(name="beta.jar", directory=Path(temp_dir)))
            alpha_config = ClientPackConfig(policy=ClientPackPolicy.OPTIONAL, default_selected=True)
            beta_config = ClientPackConfig(policy=ClientPackPolicy.REQUIRED)
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.update_client_pack_configs = AsyncMock(return_value=(alpha, beta))
            app = _build_app(manager)
            app.cfg.client_pack_current_hash = "a" * 64
            app.cfg.client_pack_published_hash = "a" * 64
            app.cfg.client_pack_published_version = "2026-07-03"
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))
            service._app_entries_cache = cast(Any, object())
            persisted_overrides: list[dict[str, object]] = []
            app.set_instance_config_change_handler(
                lambda changed_app: persisted_overrides.append(dict(changed_app.instance_config_overrides))
            )

            result = asyncio.run(
                service.update_client_pack_config(
                    app=app,
                    update=NodeClientPackConfigUpdateRequest(
                        mods=(
                            NodeClientPackModConfigUpdate(mod_name=alpha.name, client_pack=alpha_config),
                            NodeClientPackModConfigUpdate(mod_name=beta.name, client_pack=beta_config),
                        )
                    ),
                    actor_user_id=42,
                )
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.admin)
        manager.update_client_pack_configs.assert_awaited_once_with(
            {alpha.name: alpha_config, beta.name: beta_config}
        )
        self.assertEqual(result["updated_count"], 2)
        self.assertTrue(app.cfg.client_pack_content_dirty)
        self.assertEqual(app.cfg.client_pack_published_version, "2026-07-03")
        self.assertTrue(persisted_overrides[-1]["client_pack_content_dirty"])
        self.assertIsNone(service._app_entries_cache)

    def test_update_client_pack_config_persists_minecraft_kubejs_exclusions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            instance_directory = Path(temp_dir)
            script_directory = instance_directory / "kubejs" / "server_scripts"
            script_directory.mkdir(parents=True)
            (script_directory / "events.js").write_text("events", encoding="utf-8")
            manager = Mock()
            manager.reload_mods = AsyncMock()
            manager.update_client_pack_configs = AsyncMock(return_value=())
            app = Mock(spec=Minecraft)
            app.name = "minecraft_alpha"
            app.directory = instance_directory
            app.has_mod_manager = manager
            app.cfg = App_Config(
                name="minecraft_alpha",
                instance_key="alpha",
                friendly_name="Minecraft Alpha",
                directory=instance_directory,
                apps_dir=instance_directory,
                mods_dir=None,
                scope="minecraft",
            )
            acl = Mock()
            acl.perm_check = AsyncMock()
            service = NodeApiService()
            service.set_acl(cast(Any, acl))

            result = asyncio.run(
                service.update_client_pack_config(
                    app=cast(App, app),
                    update=NodeClientPackConfigUpdateRequest(
                        mods=(),
                        metadata=ClientPackMetadataConfig(
                            name="Example Pack",
                            description="Example description",
                            filename_template="{pack_name}-{version}",
                            include_servers_dat=False,
                            include_options_txt=False,
                        ),
                        kubejs_scripts=(
                            ClientPackKubeJsScript(
                                relative_path="server_scripts/events.js",
                                included=False,
                            ),
                        ),
                    ),
                    actor_user_id=42,
                )
            )

        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(
            app.cfg.client_pack_excluded_kubejs_scripts,
            ("server_scripts/events.js",),
        )
        self.assertEqual(app.cfg.client_pack_metadata.name, "Example Pack")
        self.assertFalse(app.cfg.client_pack_metadata.include_servers_dat)
        self.assertFalse(app.cfg.client_pack_metadata.include_options_txt)
        app.invalidate_client_pack_content.assert_called_once_with()
        app.persist_instance_config_overrides.assert_called_once_with()

    def test_minecraft_client_pack_entries_generate_servers_dat_and_filter_options_txt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overrides = root / "client-overrides"
            overrides.mkdir()
            (overrides / "options.txt").write_text("client options", encoding="utf-8")
            (overrides / "config.toml").write_text("client config", encoding="utf-8")
            (root / "required.jar").write_bytes(b"required")
            required = _TestMod(Mod_Config(name="required.jar", directory=root))
            manager = Mock()
            manager.list_mods.return_value = (required,)
            manager.get.side_effect = {required.name: required}.__getitem__
            app = Mock(spec=Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.directory = root
            app.has_mod_manager = manager
            app.mod_capabilities = SimpleNamespace(include_client_overrides=True)
            app.cfg = App_Config(
                name="minecraft_alpha",
                instance_key="alpha",
                friendly_name="Minecraft Alpha",
                directory=root,
                apps_dir=root,
                mods_dir=None,
                client_overrides_dir=overrides,
                scope="minecraft",
                join_host="play.example.test",
                join_port=25565,
                client_pack_metadata=ClientPackMetadataConfig(
                    name="Example Pack",
                    description="Example description",
                    include_servers_dat=True,
                    include_options_txt=False,
                ),
            )
            service = NodeApiService()

            with (
                patch.object(NodeApiService, "node_name", new=property(lambda _service: "erin")),
                patch.object(NodeApiService, "_known_bot_snapshots", return_value=()),
            ):
                entries = service._client_pack_entries(
                    ClientPackSelection(),
                    app=cast(App, app),
                    include_kubejs_scripts=True,
                )

        archive_paths = {entry.archive_path.as_posix(): entry for entry in entries}
        self.assertIn("required.jar", archive_paths)
        self.assertIn("overrides/config.toml", archive_paths)
        self.assertNotIn("overrides/options.txt", archive_paths)
        self.assertIn("overrides/servers.dat", archive_paths)
        servers_dat = archive_paths["overrides/servers.dat"]
        self.assertIsInstance(servers_dat, ArchiveDataEntry)
        assert isinstance(servers_dat, ArchiveDataEntry)
        self.assertEqual(
            _parse_servers_dat(servers_dat.content),
            (_ServersDatEntry(name="ErinServer", ip="play.example.test:25565"),),
        )

    def test_minecraft_client_pack_entries_prefer_direct_join_address_for_servers_dat(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "required.jar").write_bytes(b"required")
            required = _TestMod(Mod_Config(name="required.jar", directory=root))
            manager = Mock()
            manager.list_mods.return_value = (required,)
            manager.get.side_effect = {required.name: required}.__getitem__
            app = Mock(spec=Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.directory = root
            app.has_mod_manager = manager
            app.mod_capabilities = SimpleNamespace(include_client_overrides=True)
            app.cfg = App_Config(
                name="minecraft_alpha",
                instance_key="alpha",
                friendly_name="Minecraft Alpha",
                directory=root,
                apps_dir=root,
                mods_dir=None,
                scope="minecraft",
                join_host="play.example.test",
                join_port=25565,
                client_pack_metadata=ClientPackMetadataConfig(
                    name="Example Pack",
                    description="Example description",
                    include_servers_dat=True,
                ),
            )
            service = NodeApiService()

            with (
                patch.object(NodeApiService, "node_name", new=property(lambda _service: "erin")),
                patch.object(NodeApiService, "_known_bot_snapshots", return_value=()),
                patch.object(config, "PUBLIC_ADDR", "play.example.test"),
                patch.object(config, "PUBLIC_IP", "203.0.113.10"),
            ):
                entries = service._client_pack_entries(
                    ClientPackSelection(),
                    app=cast(App, app),
                    include_kubejs_scripts=False,
                )

        archive_paths = {entry.archive_path.as_posix(): entry for entry in entries}
        servers_dat = archive_paths["overrides/servers.dat"]
        self.assertIsInstance(servers_dat, ArchiveDataEntry)
        assert isinstance(servers_dat, ArchiveDataEntry)
        self.assertEqual(
            _parse_servers_dat(servers_dat.content),
            (_ServersDatEntry(name="ErinServer", ip="203.0.113.10:25565"),),
        )

    def test_minecraft_client_pack_entries_honor_download_file_overrides(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overrides = root / "client-overrides"
            overrides.mkdir()
            (overrides / "options.txt").write_text("client options", encoding="utf-8")
            (root / "required.jar").write_bytes(b"required")
            required = _TestMod(Mod_Config(name="required.jar", directory=root))
            manager = Mock()
            manager.list_mods.return_value = (required,)
            manager.get.side_effect = {required.name: required}.__getitem__
            app = Mock(spec=Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.directory = root
            app.has_mod_manager = manager
            app.mod_capabilities = SimpleNamespace(include_client_overrides=True)
            app.cfg = App_Config(
                name="minecraft_alpha",
                instance_key="alpha",
                friendly_name="Minecraft Alpha",
                directory=root,
                apps_dir=root,
                mods_dir=None,
                client_overrides_dir=overrides,
                scope="minecraft",
                join_host="play.example.test",
                join_port=25565,
                client_pack_metadata=ClientPackMetadataConfig(
                    name="Example Pack",
                    description="Example description",
                    include_servers_dat=True,
                    include_options_txt=True,
                ),
            )

            entries = NodeApiService()._client_pack_entries(
                ClientPackSelection(),
                app=cast(App, app),
                include_kubejs_scripts=True,
                include_servers_dat=False,
                include_options_txt=False,
            )

        archive_paths = {entry.archive_path.as_posix() for entry in entries}
        self.assertIn("required.jar", archive_paths)
        self.assertNotIn("overrides/options.txt", archive_paths)
        self.assertNotIn("overrides/servers.dat", archive_paths)

    def test_client_pack_server_name_uses_node_label_when_available(self) -> None:
        service = NodeApiService()
        snapshot = SimpleNamespace(
            profile=SimpleNamespace(label="Yoko"),
            features=SimpleNamespace(mod_web=SimpleNamespace(node_name="yuki")),
        )

        with (
            patch.object(NodeApiService, "node_name", new=property(lambda _service: "yuki")),
            patch.object(NodeApiService, "_known_bot_snapshots", return_value=(snapshot,)),
        ):
            server_name = service._minecraft_servers_dat_server_name(service._client_pack_node_label())

        self.assertEqual(server_name, "YokoServer")

    def test_client_pack_archive_name_uses_configured_metadata_template(self) -> None:
        app = Mock(spec=Minecraft)
        app.name = "minecraft_alpha"
        app.friendly = "Minecraft Alpha"
        app.cfg = App_Config(
            name="minecraft_alpha",
            instance_key="alpha",
            friendly_name="Minecraft Alpha",
            directory=Path("/tmp/minecraft-alpha"),
            apps_dir=Path("/tmp"),
            mods_dir=None,
            scope="minecraft",
            version=AppVersion(main="1.21.1", loader="fabric", framework="0.16.10"),
            client_pack_metadata=ClientPackMetadataConfig(
                name="Example Pack",
                description="Example description",
                filename_template="{pack_name}-{version}",
            ),
        )

        archive_name = NodeApiService()._archive_name(
            app=cast(App, app),
            entries=(),
            request=NodeDownloadRequest(
                pack_purpose=PackPurpose.CLIENT,
                pack_format=PackFormat.MODRINTH,
            ),
            client_pack_version="2026-07-04.2",
        )

        self.assertEqual(archive_name, "Example_Pack-2026-07-04.2.mrpack")

    def test_client_pack_changes_are_dirty_before_first_publication(self) -> None:
        app = _build_app(Mock())
        persisted_overrides: list[dict[str, object]] = []
        app.set_instance_config_change_handler(
            lambda changed_app: persisted_overrides.append(dict(changed_app.instance_config_overrides))
        )

        app.invalidate_client_pack_content()

        self.assertTrue(app.cfg.client_pack_content_dirty)
        self.assertTrue(persisted_overrides[-1]["client_pack_content_dirty"])

    def test_publish_client_pack_requires_changelog(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a changelog"):
            NodeClientPackPublishRequest(changelog="   ")

    def test_publish_client_pack_config_saves_default_pack_without_downloading(self) -> None:
        manager = Mock()
        manager.reload_mods = AsyncMock()
        app = _build_app(manager)
        acl = Mock()
        acl.perm_check = AsyncMock()
        service = NodeApiService()
        service.set_acl(cast(Any, acl))

        with TemporaryDirectory() as temp_dir:
            app.directory = Path(temp_dir)
            with (
                patch("node_api.build_client_pack_entries", return_value=(Mock(),)),
                patch.object(
                    service,
                    "_client_pack_content_hash",
                    new=AsyncMock(return_value="a" * 64),
                ),
                patch.object(
                    service,
                    "_default_client_pack_mod_snapshots",
                    return_value=(
                        ClientPackModSnapshot(name="alpha.jar", friendly="Alpha", version="1.0.0"),
                    ),
                ),
                patch("apps._app.next_client_pack_version", return_value="2026-07-04"),
            ):
                result = asyncio.run(
                    service.publish_client_pack_config(
                        app=app,
                        update=NodeClientPackPublishRequest(changelog="Initial release."),
                        actor_user_id=42,
                    )
                )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.admin)
        self.assertEqual(result["published_version"], "2026-07-04")
        self.assertEqual(app.cfg.client_pack_published_hash, "a" * 64)
        self.assertEqual(app.cfg.client_pack_published_changelog, "Initial release.")
        self.assertEqual(
            app.cfg.client_pack_releases,
            (ClientPackRelease(version="2026-07-04", changelog="Initial release."),),
        )
        self.assertEqual(
            app.cfg.client_pack_published_mods,
            (ClientPackModSnapshot(name="alpha.jar", friendly="Alpha", version="1.0.0"),),
        )
        self.assertFalse(app.cfg.client_pack_content_dirty)

    def test_publish_client_pack_keeps_version_when_content_hash_is_unchanged(self) -> None:
        app = _build_app(Mock())
        app.cfg.client_pack_published_hash = "a" * 64
        app.cfg.client_pack_published_version = "2026-07-04"
        app.cfg.client_pack_content_dirty = True

        with patch("apps._app.next_client_pack_version") as next_version:
            published_version = app.publish_client_pack(
                "a" * 64,
                changelog="Clarified release notes.",
            )

        next_version.assert_not_called()
        self.assertEqual(published_version, "2026-07-04")
        self.assertEqual(app.cfg.client_pack_published_changelog, "Clarified release notes.")
        self.assertEqual(
            app.cfg.client_pack_releases,
            (ClientPackRelease(version="2026-07-04", changelog="Clarified release notes."),),
        )
        self.assertFalse(app.cfg.client_pack_content_dirty)

    def test_publish_client_pack_rejects_duplicate_mod_snapshots_before_persisting(self) -> None:
        app = _build_app(Mock())
        app.cfg.client_pack_content_dirty = True

        with self.assertRaisesRegex(ValueError, "duplicate.jar"):
            app.publish_client_pack(
                "a" * 64,
                changelog="Initial release.",
                mods=(
                    ClientPackModSnapshot(name="duplicate.jar", friendly="Duplicate"),
                    ClientPackModSnapshot(name="duplicate.jar", friendly="Duplicate"),
                ),
            )

        self.assertEqual(app.cfg.client_pack_published_mods, ())
        self.assertTrue(app.cfg.client_pack_content_dirty)

    def test_publish_client_pack_retains_previous_release_changelogs(self) -> None:
        app = _build_app(Mock())
        app.cfg.client_pack_published_hash = "a" * 64
        app.cfg.client_pack_published_version = "2026-07-03"
        app.cfg.client_pack_published_changelog = "Initial release."

        with patch("apps._app.next_client_pack_version", return_value="2026-07-04"):
            app.publish_client_pack("b" * 64, changelog="Added renderer options.")

        self.assertEqual(
            app.cfg.client_pack_releases,
            (
                ClientPackRelease(version="2026-07-03", changelog="Initial release."),
                ClientPackRelease(version="2026-07-04", changelog="Added renderer options."),
            ),
        )
        self.assertEqual(
            app.instance_config_overrides["client_pack_releases"],
            [
                {"version": "2026-07-03", "changelog": "Initial release."},
                {"version": "2026-07-04", "changelog": "Added renderer options."},
            ],
        )

    def test_client_pack_releases_reconciles_current_release_into_incomplete_history(self) -> None:
        app = _build_app(Mock())
        app.cfg.client_pack_published_version = "2026-07-04.3"
        app.cfg.client_pack_published_changelog = "Current changes."
        app.cfg.client_pack_releases = (
            ClientPackRelease(version="2026-07-04", changelog="Initial release."),
        )

        self.assertEqual(
            app.client_pack_releases,
            (
                ClientPackRelease(version="2026-07-04", changelog="Initial release."),
                ClientPackRelease(version="2026-07-04.3", changelog="Current changes."),
            ),
        )
        self.assertEqual(
            app.instance_config_overrides["client_pack_releases"],
            [
                {"version": "2026-07-04", "changelog": "Initial release."},
                {"version": "2026-07-04.3", "changelog": "Current changes."},
            ],
        )

    def test_missing_client_overrides_directory_creates_logged_yukibot_fallback(self) -> None:
        app = _build_app(Mock())
        with TemporaryDirectory() as temp_dir:
            app.directory = Path(temp_dir)
            app.cfg.client_overrides_dir = Path(temp_dir) / "client-overrides"

            with self.assertLogs("node_api", level="WARNING") as captured:
                resolved = NodeApiService._client_overrides_dir_for_pack(app)

            self.assertEqual(resolved, Path(temp_dir) / ".yukibot" / "client-overrides")
            self.assertTrue(resolved.is_dir())
        self.assertIn("Created fallback client overrides directory", "\n".join(captured.output))

    def test_mutate_app_start_blocks_when_another_app_is_running(self) -> None:
        service = NodeApiService()
        manager = Mock()
        manager.start_blocker = Mock(
            return_value=AppStartBlocker(
                kind=AppStartBlockerKind.SAME_SCOPE,
                message="Cannot start Minecraft Alpha; Factorio Lab is already running for scope `minecraft`.",
                blocking_app_name="factorio_lab",
                blocking_app_friendly="Factorio Lab",
            )
        )
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))
        app = _build_app(Mock())

        with self.assertRaises(Exception) as raised:
            asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.START,
                    actor_user_id=42,
                )
            )

        self.assertEqual(getattr(raised.exception, "status_code"), 409)

    def test_mutate_app_disable_updates_enabled_state(self) -> None:
        app = _build_app(Mock())
        app.cfg.enabled = True
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.toggle = Mock()
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=False,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            result = asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.DISABLE,
                    actor_user_id=42,
                )
            )

        manager.toggle.assert_called_once_with(app.name, False)
        self.assertEqual(result.action, NodeAppMutationAction.DISABLE)

    def test_mutate_app_rename_updates_friendly_name(self) -> None:
        app = _build_app(Mock())
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.set_app_friendly_name = Mock(
            side_effect=lambda current_app, friendly_name: (
                setattr(current_app, "friendly", friendly_name) or friendly_name
            )
        )
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            result = asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.RENAME,
                    actor_user_id=42,
                    friendly_name="Demo Alpha",
                )
            )

        manager.set_app_friendly_name.assert_called_once_with(app, "Demo Alpha")
        self.assertEqual(result.action, NodeAppMutationAction.RENAME)
        self.assertEqual(result.app_friendly, "Demo Alpha")
        self.assertEqual(result.message, "Renamed Minecraft Alpha to Demo Alpha.")

    def test_mutate_app_update_details_persists_notes_and_notice_flags(self) -> None:
        app = _build_app(Mock())
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)

        def _update_details(current_app: _DummyApp, details: object) -> str:
            setattr(current_app, "friendly", "Demo Alpha")
            current_app.cfg.notes = "Main shard"
            current_app.cfg.lifecycle_notice_started = False
            current_app.cfg.lifecycle_notice_stopped = True
            current_app.cfg.lifecycle_notice_crashed = False
            return "Demo Alpha"

        manager.update_app_details = Mock(side_effect=_update_details)
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            result = asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    actor_user_id=42,
                    friendly_name="Demo Alpha",
                    title_font_preset=AppTitleFont.MINECRAFT_TEN.value,
                    notes="Main shard",
                    lifecycle_notice_started=False,
                    lifecycle_notice_stopped=True,
                    lifecycle_notice_crashed=False,
                    rcon_requires_online_players=False,
                    disabled_activity_provider_ids=("day",),
                    running_cpu_points=3,
                    running_ram_points=7,
                    startup_cpu_points=None,
                    startup_ram_points=None,
                    steam_update_enabled=True,
                    steam_update_selected_branch="latest_experimental",
                )
            )

        manager.update_app_details.assert_called_once()
        details = manager.update_app_details.call_args.args[1]
        self.assertEqual(details.title_font_preset, AppTitleFont.MINECRAFT_TEN.value)
        self.assertEqual(details.running_cpu_points, 3)
        self.assertEqual(details.running_ram_points, 7)
        self.assertIsNone(details.startup_cpu_points)
        self.assertIsNone(details.startup_ram_points)
        self.assertFalse(details.rcon_requires_online_players)
        self.assertEqual(details.disabled_activity_provider_ids, ("day",))
        self.assertTrue(details.steam_update_enabled)
        self.assertEqual(details.steam_update_selected_branch, "latest_experimental")
        self.assertEqual(result.action, NodeAppMutationAction.UPDATE_DETAILS)
        self.assertEqual(result.app_friendly, "Demo Alpha")
        self.assertEqual(result.message, "Updated details for Demo Alpha.")

    def test_mutate_app_update_details_passes_relay_advancement_toggle(self) -> None:
        class _RelayApp(_DummyApp):
            @property
            def relay_advancements_enabled(self) -> bool | None:
                return bool(getattr(self, "_relay_advancements_enabled_state", False))

            @property
            def relay_advancement_term(self) -> str:
                return "Advancement"

            def apply_relay_advancements_enabled(self, enabled: bool) -> None:
                self._relay_advancements_enabled_state = enabled

        app = _build_app(Mock())
        app.__class__ = _RelayApp
        app._relay_advancements_enabled_state = True
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.update_app_details = Mock(return_value="Demo Alpha")
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    actor_user_id=42,
                    friendly_name="Demo Alpha",
                    notes="Main shard",
                    lifecycle_notice_started=False,
                    lifecycle_notice_stopped=True,
                    lifecycle_notice_crashed=False,
                    relay_advancements_enabled=False,
                    running_cpu_points=3,
                    running_ram_points=7,
                    startup_cpu_points=None,
                    startup_ram_points=None,
                )
            )

        manager.update_app_details.assert_called_once()
        details = manager.update_app_details.call_args.args[1]
        self.assertFalse(details.relay_advancements_enabled)

    def test_mutate_app_update_details_passes_generic_relay_notice_toggles(self) -> None:
        class _RelayNoticeApp(_DummyApp):
            relay_notice_player_session_supported = True
            relay_notice_player_death_supported = True
            relay_notice_progress_supported = True

            @property
            def relay_progress_notice_term(self) -> str:
                return "Research"

        app = _build_app(Mock())
        app.__class__ = _RelayNoticeApp
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.update_app_details = Mock(return_value="Demo Alpha")
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    actor_user_id=42,
                    friendly_name="Demo Alpha",
                    notes="Main shard",
                    lifecycle_notice_started=False,
                    lifecycle_notice_stopped=True,
                    lifecycle_notice_crashed=False,
                    relay_notice_player_session=False,
                    relay_notice_player_death=False,
                    relay_notice_progress=False,
                    running_cpu_points=3,
                    running_ram_points=7,
                    startup_cpu_points=None,
                    startup_ram_points=None,
                )
            )

        manager.update_app_details.assert_called_once()
        details = manager.update_app_details.call_args.args[1]
        self.assertFalse(details.relay_notice_player_session)
        self.assertFalse(details.relay_notice_player_death)
        self.assertFalse(details.relay_notice_progress)

    def test_mutate_app_update_details_allows_single_resource_startup_override(self) -> None:
        app = _build_app(Mock())
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.update_app_details = Mock(return_value="Demo Alpha")
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    actor_user_id=42,
                    friendly_name="Demo Alpha",
                    notes="Main shard",
                    lifecycle_notice_started=False,
                    lifecycle_notice_stopped=True,
                    lifecycle_notice_crashed=False,
                    running_cpu_points=3,
                    running_ram_points=7,
                    startup_cpu_points=None,
                    startup_ram_points=9,
                )
            )

        manager.update_app_details.assert_called_once()
        details = manager.update_app_details.call_args.args[1]
        self.assertIsNone(details.startup_cpu_points)
        self.assertEqual(details.startup_ram_points, 9)

    def test_mutate_app_select_update_branch_uses_updater(self) -> None:
        app = _build_app(Mock())
        app.updater = Mock()
        app.updater.select_branch.return_value = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            app_id=294420,
            selected_branch_id="latest_experimental",
            selected_branch_label="Experimental",
            branches=(
                AppUpdateBranchState(branch_id="public", label="Stable", selected=False),
                AppUpdateBranchState(branch_id="latest_experimental", label="Experimental", selected=True),
            ),
            supports_verify=True,
        )
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            result = asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.SELECT_UPDATE_BRANCH,
                    actor_user_id=42,
                    update_branch_id="latest_experimental",
                )
            )

        app.updater.select_branch.assert_called_once_with("latest_experimental")
        self.assertEqual(result.action, NodeAppMutationAction.SELECT_UPDATE_BRANCH)
        self.assertEqual(result.message, "Selected update branch Experimental for Minecraft Alpha.")

    def test_mutate_app_update_uses_updater_result(self) -> None:
        app = _build_app(Mock())
        app.updater = Mock()
        app.updater.start_selected_update = AsyncMock(
            return_value=AppUpdateOperationResult(
                kind=AppUpdateOperationKind.UPDATE,
                message="Started update for Minecraft Alpha on Steam branch Stable.",
                selected_branch_id="public",
                selected_branch_label="Stable",
            )
        )
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            result = asyncio.run(
                service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.UPDATE,
                    actor_user_id=42,
                )
            )

        app.updater.start_selected_update.assert_awaited_once()
        self.assertEqual(result.action, NodeAppMutationAction.UPDATE)
        self.assertEqual(result.message, "Started update for Minecraft Alpha on Steam branch Stable.")

    def test_mutate_node_capacity_requires_root_and_returns_result(self) -> None:
        manager = Mock()
        manager.set_node_capacity = Mock(
            return_value=config.NodeCapacityProfile(
                cpu_points_total=8,
                ram_points_total=12,
                cpu_points_reserved=2,
                ram_points_reserved=3,
            )
        )
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        result = asyncio.run(
            service.mutate_node_capacity(
                capacity=config.NodeCapacityProfile(
                    cpu_points_total=8,
                    ram_points_total=12,
                    cpu_points_reserved=2,
                    ram_points_reserved=3,
                ),
                actor_user_id=42,
            )
        )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.root)
        manager.set_node_capacity.assert_called_once()
        self.assertEqual(
            result,
            NodeCapacityMutationResult(
                node=config.MOD_WEB_SERVER.node_name,
                message=f"Updated node capacity for {config.MOD_WEB_SERVER.node_name}.",
                capacity=config.NodeCapacityProfile(
                    cpu_points_total=8,
                    ram_points_total=12,
                    cpu_points_reserved=2,
                    ram_points_reserved=3,
                ),
            ),
        )

    def test_mutate_node_font_sources_requires_sudo_and_refreshes_assets(self) -> None:
        manager = Mock()
        manager.set_node_font_sources = Mock(
            return_value=config.NodeFontSourceSettings(
                google_font_urls=("https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap",)
            )
        )
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch("node_api.font_assets.schedule_startup_refresh") as schedule_refresh:
            result = asyncio.run(
                service.mutate_node_font_sources(
                    settings=config.NodeFontSourceSettings(
                        google_font_urls=("https://fonts.google.com/specimen/Black+Ops+One",)
                    ),
                    actor_user_id=42,
                )
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        manager.set_node_font_sources.assert_called_once()
        schedule_refresh.assert_called_once_with(
            google_font_urls=("https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap",)
        )
        self.assertEqual(
            result,
            NodeFontSourceSettingsMutationResult(
                node=config.MOD_WEB_SERVER.node_name,
                message=f"Updated node font sources for {config.MOD_WEB_SERVER.node_name}.",
                settings=config.NodeFontSourceSettings(
                    google_font_urls=("https://fonts.googleapis.com/css2?family=Black+Ops+One&display=swap",)
                ),
            ),
        )

    def test_mutate_node_disk_settings_requires_root_and_returns_secondary_disk_state(self) -> None:
        preferences = config.PersistedDiskPreferences(
            activity_mounts=["/mnt/data"],
            labels={"/mnt/data": "Data"},
            primary_mount="/mnt/data",
            secondary_mount="/mnt/backups",
        )
        settings = NodeDiskManagementState(
            node=config.MOD_WEB_SERVER.node_name,
            disks=(
                NodeDiskEntry(
                    mountpoint="/mnt/data",
                    display_name="Data",
                    is_activity=True,
                    is_primary=True,
                    is_secondary=False,
                    is_bot_disk=True,
                ),
                NodeDiskEntry(
                    mountpoint="/mnt/backups",
                    display_name="Backups",
                    is_activity=False,
                    is_primary=False,
                    is_secondary=True,
                    is_bot_disk=False,
                ),
            ),
            preferences=preferences,
        )
        stats = Mock()
        stats.set_disk_preferences = Mock(return_value=preferences)
        service = NodeApiService()
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with (
            patch("node_api.Stats_System", return_value=stats),
            patch.object(service, "read_node_disk_settings", return_value=settings),
        ):
            result = asyncio.run(
                service.mutate_node_disk_settings(
                    preferences=preferences,
                    actor_user_id=42,
                )
            )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.root)
        stats.set_disk_preferences.assert_called_once_with(preferences)
        self.assertEqual(
            result,
            NodeDiskSettingsMutationResult(
                node=config.MOD_WEB_SERVER.node_name,
                message=f"Updated node disk settings for {config.MOD_WEB_SERVER.node_name}.",
                settings=settings,
            ),
        )
        self.assertEqual(NodeDiskSettingsMutationResult.from_mapping(result.to_mapping()), result)

    def test_mutate_discord_settings_requires_sudo_and_refreshes_activity(self) -> None:
        manager = Mock()
        manager.discord_settings = Mock(
            return_value=config.DiscordSettings(
                activity=config.DiscordActivitySettings(
                    fallback_text="Watching over Erin",
                    refresh_interval_seconds=3,
                    fields=(config.DiscordActivityField.APP,),
                )
            )
        )
        manager.set_discord_settings = Mock(
            return_value=config.DiscordSettings(
                activity=config.DiscordActivitySettings(
                    fallback_text="Watching over Erin",
                    refresh_interval_seconds=3,
                    fields=(config.DiscordActivityField.APP,),
                )
            )
        )
        manager.activity_manager = SimpleNamespace(refresh=AsyncMock())
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))
        settings = config.DiscordSettings(
            activity=config.DiscordActivitySettings(
                fallback_text="Watching over Erin",
                refresh_interval_seconds=3,
                fields=(config.DiscordActivityField.APP,),
            )
        )

        result = asyncio.run(
            service.mutate_discord_settings(
                settings=settings,
                actor_user_id=42,
            )
        )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
        manager.set_discord_settings.assert_called_once_with(settings)
        manager.activity_manager.refresh.assert_awaited_once()
        self.assertEqual(
            result,
            NodeDiscordSettingsMutationResult(
                node=config.MOD_WEB_SERVER.node_name,
                message=f"Updated Discord settings for {config.MOD_WEB_SERVER.node_name}.",
                settings=settings,
            ),
        )

    def test_mutate_discord_settings_requires_root_when_refresh_interval_changes(self) -> None:
        manager = Mock()
        manager.discord_settings = Mock(
            return_value=config.DiscordSettings(
                activity=config.DiscordActivitySettings(
                    refresh_interval_seconds=3,
                )
            )
        )
        manager.set_discord_settings = Mock(
            return_value=config.DiscordSettings(
                activity=config.DiscordActivitySettings(
                    refresh_interval_seconds=5,
                )
            )
        )
        manager.activity_manager = SimpleNamespace(refresh=AsyncMock())
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        asyncio.run(
            service.mutate_discord_settings(
                settings=config.DiscordSettings(
                    activity=config.DiscordActivitySettings(
                        refresh_interval_seconds=5,
                    )
                ),
                actor_user_id=42,
            )
        )

        acl.perm_check.assert_awaited_once_with(42, Power_Level.root)

    def test_mutate_app_start_returns_before_launch_finishes(self) -> None:
        app = _build_app(Mock())
        app.cfg.enabled = True
        launch_started = asyncio.Event()
        allow_launch_finish = asyncio.Event()

        async def _launch(_: object) -> None:
            launch_started.set()
            await allow_launch_finish.wait()

        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.launch = AsyncMock(side_effect=_launch)
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        runtime_summary = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version=None,
            transition_state=NodeAppTransitionState.STARTING,
            player_count=None,
            player_capacity=None,
            relay_support=app.chat_relay_support,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )

        async def _run_test() -> NodeAppMutationResult:
            with patch.object(service, "build_live_app_runtime_summary", new=AsyncMock(return_value=runtime_summary)):
                result = await service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.START,
                    actor_user_id=42,
                )
            await asyncio.sleep(0)
            self.assertTrue(launch_started.is_set())
            self.assertFalse(allow_launch_finish.is_set())
            self.assertEqual(result.message, "Start requested for Minecraft Alpha.")
            self.assertEqual(result.app_stats, runtime_summary)
            pending_task = service._app_mutation_tasks.get(app.name.casefold())
            self.assertIsNotNone(pending_task)
            allow_launch_finish.set()
            if pending_task is not None:
                await pending_task
            return result

        result = asyncio.run(_run_test())

        manager.launch.assert_awaited_once_with(app)
        self.assertEqual(result.action, NodeAppMutationAction.START)
        self.assertNotIn(app.name.casefold(), service._app_mutation_tasks)

    def test_mutate_app_stop_uses_manager_end_and_live_summary(self) -> None:
        app = _build_app(Mock())
        app.cfg.enabled = True
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.end = AsyncMock(return_value={app.name.title()})
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))
        runtime_summary = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version=None,
            transition_state=NodeAppTransitionState.STOPPING,
            player_count=None,
            player_capacity=None,
            relay_support=app.chat_relay_support,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )

        with (
            patch.object(service, "build_live_app_runtime_summary", new=AsyncMock(return_value=runtime_summary)),
            patch.object(service, "build_app_runtime_summary", new=AsyncMock()) as full_summary_mock,
        ):
            async def _run_test() -> NodeAppMutationResult:
                result = await service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.STOP,
                    actor_user_id=42,
                )
                pending_task = service._app_mutation_tasks.get(app.name.casefold())
                self.assertIsNotNone(pending_task)
                if pending_task is not None:
                    await pending_task
                return result

            result = asyncio.run(_run_test())

        manager.end.assert_awaited_once_with(app.name)
        full_summary_mock.assert_not_awaited()
        self.assertEqual(result.action, NodeAppMutationAction.STOP)
        self.assertEqual(result.message, "Stop requested for Minecraft Alpha.")
        self.assertEqual(result.app_stats, runtime_summary)

    def test_mutate_app_kill_uses_manager_kill(self) -> None:
        app = _build_app(Mock())
        app.cfg.enabled = True
        manager = Mock()
        manager.start_blocker = Mock(return_value=None)
        manager.kill = AsyncMock(return_value={app.name.title()})
        service = NodeApiService()
        service.set_manager(cast(Any, manager))
        acl = Mock()
        acl.perm_check = AsyncMock()
        service.set_acl(cast(Any, acl))

        with patch.object(
            service,
            "build_live_app_runtime_summary",
            new=AsyncMock(
                return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=app.chat_relay_support,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )
            ),
        ):
            async def _run_test() -> NodeAppMutationResult:
                result = await service.mutate_app(
                    app=app,
                    action=NodeAppMutationAction.KILL,
                    actor_user_id=42,
                )
                pending_task = service._app_mutation_tasks.get(app.name.casefold())
                self.assertIsNotNone(pending_task)
                if pending_task is not None:
                    await pending_task
                return result

            result = asyncio.run(_run_test())

        manager.kill.assert_awaited_once_with(app.name)
        self.assertEqual(result.action, NodeAppMutationAction.KILL)
        self.assertEqual(result.message, "Kill requested for Minecraft Alpha.")

    def test_build_mod_list_uses_local_mod_manager(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "example.jar"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=mod_path.parent))
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = [mod]
            app = _build_app(mod_manager)
            app.player_count = AsyncMock(return_value=(2, 16))  # type: ignore[method-assign]
            app.chat_relay_outbound = True
            app.am_receiver = _DummyReceiver()
            updater = Update_Manager(app)
            updater.version = (1, 2, 3)
            app.updater = updater
            app.process = Mock()
            app.process.poll.return_value = None
            app.check_running = Mock(return_value=True)  # type: ignore[method-assign]

            server = replace(config.MOD_WEB_SERVER, node_name="erin")
            fake_disk = SimpleNamespace(
                percent=42,
                free_bytes=20 * 1024**3,
                total_bytes=50 * 1024**3,
            )
            fake_stats = Mock()
            fake_stats.disk_snapshot_for_path.return_value = fake_disk
            with (
                patch.object(config, "MOD_WEB_SERVER", server),
                patch("node_api.Stats_System", return_value=fake_stats),
                patch.object(NodeApiService, "_app_footprint_size_bytes", return_value=8),
            ):
                service = NodeApiService()
                async def build_twice() -> tuple[NodeModList, NodeModList]:
                    return await service.build_mod_list(app), await service.build_mod_list(app)

                model, second_model = asyncio.run(build_twice())

        self.assertEqual(model.node, "erin")
        self.assertEqual(second_model, model)
        mod_manager.reload_mods.assert_awaited_once_with()
        self.assertEqual(model.summary.total_count, 1)
        self.assertEqual(model.summary.enabled_count, 1)
        self.assertEqual(model.summary.server_enabled_count, 1)
        self.assertEqual(model.summary.server_disabled_count, 0)
        self.assertEqual(model.summary.server_loadable_count, 1)
        self.assertEqual(model.summary.client_only_count, 0)
        self.assertEqual(model.summary.client_pack_eligible_count, 1)
        self.assertEqual(model.summary.downloadable_count, 1)
        self.assertEqual(model.summary.non_downloadable_count, 0)
        self.assertEqual(model.mods[0].name, "example.jar")
        self.assertIs(model.mods[0].placement, ModPlacement.SERVER_ENABLED)
        self.assertTrue(model.mods[0].server_loadable)
        self.assertTrue(model.mods[0].client_pack_eligible)
        self.assertEqual(model.mods[0].archive_name, "example.jar")
        self.assertEqual(model.mods[0].source_path, str(mod_path))
        self.assertTrue(model.mods[0].downloadable)
        self.assertEqual(model.mods[0].size_bytes, 8)
        self.assertIsNotNone(model.app_stats)
        assert model.app_stats is not None
        self.assertTrue(model.app_stats.running)
        self.assertTrue(model.app_stats.enabled)
        self.assertEqual(model.app_stats.version, "1.2.3")
        self.assertEqual(model.app_stats.player_count, 2)
        self.assertEqual(model.app_stats.player_capacity, 16)
        self.assertEqual(model.app_stats.relay_support.value, "bidirectional")
        self.assertEqual(model.app_stats.storage_percent, 42)
        self.assertEqual(model.app_stats.footprint_bytes, 8)

    def test_cached_runtime_summary_single_flights_concurrent_requests(self) -> None:
        app = _build_app(Mock())
        summary = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.0.0",
            player_count=1,
            player_capacity=8,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=20,
            storage_free_bytes=80,
            storage_total_bytes=100,
            footprint_bytes=42,
        )
        service = NodeApiService()

        async def exercise() -> tuple[NodeAppRuntimeSummary, NodeAppRuntimeSummary]:
            with patch.object(
                service,
                "build_app_runtime_summary",
                new=AsyncMock(return_value=summary),
            ) as build_summary:
                first, second = await asyncio.gather(
                    service.build_cached_app_runtime_summary(app),
                    service.build_cached_app_runtime_summary(app),
                )
                build_summary.assert_awaited_once_with(app)
                return first, second

        first, second = asyncio.run(exercise())

        self.assertEqual(first, summary)
        self.assertEqual(second, summary)

    def test_app_entry_cache_serves_stale_snapshot_when_refresh_fails(self) -> None:
        service = NodeApiService()

        async def exercise() -> tuple[tuple[NodeAppEntry, ...], tuple[NodeAppEntry, ...]]:
            with (
                patch.object(
                    service,
                    "_build_app_entries",
                    new=AsyncMock(side_effect=((), RuntimeError("temporary failure"))),
                ) as build_entries,
                patch("node_api._NODE_APP_ENTRY_CACHE_TTL_SECONDS", 0),
            ):
                first = await service.list_apps()
                second = await service.list_apps()
                self.assertEqual(build_entries.await_count, 2)
                return first, second

        first, second = asyncio.run(exercise())

        self.assertEqual(first, ())
        self.assertEqual(second, ())

    def test_build_app_runtime_summary_reports_total_app_footprint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            mods_dir = app_dir / "mods"
            client_overrides_dir = app_dir / "client-overrides"
            app_dir.mkdir()
            mods_dir.mkdir()
            client_overrides_dir.mkdir()
            (app_dir / "base.bin").write_bytes(b"base")
            (mods_dir / "example.jar").write_bytes(b"module")
            (client_overrides_dir / "options.txt").write_bytes(b"12345")
            settings_pointer = root / "settings.ini"
            settings_pointer.write_bytes(b"cfg")
            server_log_file = root / "server.log"
            server_log_file.write_bytes(b"log!")

            app = _build_app(Mock())
            app.directory = app_dir
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=app_dir,
                apps_dir=root,
                mods_dir=mods_dir,
                client_overrides_dir=client_overrides_dir,
                settings_pointer=settings_pointer,
                server_log_file=server_log_file,
                scope=app.scope,
            )
            app.chat_relay_outbound = False
            app.am_receiver = None
            app.updater = None

            fake_disk = SimpleNamespace(
                percent=55,
                free_bytes=100 * 1024**3,
                total_bytes=200 * 1024**3,
            )
            fake_stats = Mock()
            fake_stats.disk_snapshot_for_path.return_value = fake_disk
            with patch("node_api.Stats_System", return_value=fake_stats):
                summary = asyncio.run(NodeApiService().build_app_runtime_summary(app))

        self.assertEqual(summary.footprint_bytes, 22)
        self.assertEqual(summary.storage_percent, 55)

    def test_build_live_app_runtime_summary_skips_storage_and_footprint(self) -> None:
        app = _build_app(Mock())
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        app.player_count = AsyncMock(return_value=(2, 12))  # type: ignore[method-assign]
        app.chat_relay_outbound = True
        app.am_receiver = _DummyReceiver()
        service = NodeApiService()

        with (
            patch("node_api.Stats_System") as stats_system,
            patch.object(service, "_app_footprint_size_bytes") as footprint_size,
        ):
            summary = asyncio.run(service.build_live_app_runtime_summary(app))

        stats_system.assert_not_called()
        footprint_size.assert_not_called()
        self.assertEqual(summary.player_count, 2)
        self.assertEqual(summary.player_capacity, 12)
        self.assertIsNone(summary.storage_percent)
        self.assertIsNone(summary.footprint_bytes)

    def test_build_live_app_runtime_summary_preserves_runtime_fault(self) -> None:
        app = _build_app(Mock())
        app.runtime_fault = AppRuntimeFault(
            kind=AppRuntimeFaultKind.CRASH,
            summary="Failed to start the minecraft server",
        )
        service = NodeApiService()

        summary = asyncio.run(service.build_live_app_runtime_summary(app))

        self.assertEqual(summary.runtime_fault, app.runtime_fault)

    def test_build_live_app_runtime_summary_includes_activity_provider_values(self) -> None:
        class _ActivityProvider(AppActivityProvider):
            metadata = AppActivityProviderMetadata(provider_id="day", label="Day Counter")

            async def get(self) -> str | None:
                return "D2"

        app = _build_app(Mock())
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        app.set_activity_providers((_ActivityProvider(app),))
        service = NodeApiService()

        summary = asyncio.run(service.build_live_app_runtime_summary(app))

        self.assertEqual(
            summary.activity_providers,
            (
                NodeAppActivityProviderEntry(
                    provider_id="day",
                    label="Day Counter",
                    enabled=True,
                    current_value="D2",
                ),
            ),
        )

    def test_subscribe_local_app_runtime_notifies_initial_and_changed_state(self) -> None:
        async def exercise() -> None:
            app = _build_app(Mock())
            service = NodeApiService()
            service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app}, get=Mock(return_value=app))))
            first_summary = NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                footprint_bytes=None,
                transition_state=NodeAppTransitionState.NONE,
            )
            second_summary = replace(
                first_summary,
                running=True,
                player_count=1,
                player_capacity=12,
                relay_support=ChatRelaySupport.BIDIRECTIONAL,
            )
            summaries = iter((first_summary, second_summary, second_summary))
            notifications: list[NodeAppStateStreamEvent] = []
            second_notification = asyncio.Event()

            async def build_live_summary(_: App) -> NodeAppRuntimeSummary:
                return next(summaries)

            def on_update(update: NodeAppStateStreamEvent) -> None:
                notifications.append(update)
                if len(notifications) >= 2:
                    second_notification.set()

            with (
                patch.object(service, "build_live_app_runtime_summary", side_effect=build_live_summary),
                patch("node_api._LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS", 0.01),
            ):
                unsubscribe = service.subscribe_local_app_runtime(app.name, on_update)
                try:
                    await asyncio.wait_for(second_notification.wait(), timeout=0.2)
                finally:
                    unsubscribe()
                    await asyncio.sleep(0)

            self.assertEqual(
                notifications,
                [
                    NodeAppStateStreamEvent.initial(app_name=app.name, app_stats=first_summary),
                    NodeAppStateStreamEvent.runtime(app_name=app.name, app_stats=second_summary),
                ],
            )

        asyncio.run(exercise())

    def test_subscribe_local_app_runtime_can_emit_update_only_changes(self) -> None:
        async def exercise() -> None:
            app = _build_app(Mock())
            service = NodeApiService()
            service.set_manager(cast(Any, SimpleNamespace(apps={app.name: app}, get=Mock(return_value=app))))
            runtime_summary = NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="1.21.1",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                footprint_bytes=None,
                transition_state=NodeAppTransitionState.NONE,
            )
            update_info = AppUpdateInfo(
                provider_kind=AppUpdateProviderKind.STEAMCMD,
                provider_label="SteamCMD",
                selected_branch_id="public",
                selected_branch_label="Stable",
                branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
                supports_verify=True,
            )
            idle_status = AppUpdateStatus(state=AppUpdateState.IDLE, summary="Ready")
            running_status = AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary="Downloading",
                operation_kind=AppUpdateOperationKind.UPDATE,
                progress_percent=12.5,
            )
            app.updater = Mock()
            app.updater.info = Mock(return_value=update_info)
            app.updater.status = Mock(side_effect=(idle_status, running_status, running_status))
            notifications: list[NodeAppStateStreamEvent] = []
            second_notification = asyncio.Event()

            async def build_live_summary(_: App) -> NodeAppRuntimeSummary:
                return runtime_summary

            def on_update(update: NodeAppStateStreamEvent) -> None:
                notifications.append(update)
                if len(notifications) >= 2:
                    second_notification.set()

            with (
                patch.object(service, "build_live_app_runtime_summary", side_effect=build_live_summary),
                patch("node_api._LOCAL_APP_RUNTIME_SUBSCRIPTION_INTERVAL_SECONDS", 0.01),
            ):
                unsubscribe = service.subscribe_local_app_runtime(
                    app.name,
                    on_update,
                    include_update_state=True,
                )
                try:
                    await asyncio.wait_for(second_notification.wait(), timeout=0.2)
                finally:
                    unsubscribe()
                    await asyncio.sleep(0)

            self.assertEqual(
                notifications,
                [
                    NodeAppStateStreamEvent.initial(
                        app_name=app.name,
                        app_stats=runtime_summary,
                        update_info=update_info,
                        update_status=idle_status,
                    ),
                    NodeAppStateStreamEvent.update(
                        app_name=app.name,
                        update_info=update_info,
                        update_status=running_status,
                    ),
                ],
            )

        asyncio.run(exercise())

    def test_subscribe_local_node_state_notifies_initial_and_changed_state(self) -> None:
        async def exercise() -> None:
            node_name = config.MOD_WEB_SERVER.node_name
            app_entry = NodeAppEntry(
                name="minecraft_alpha",
                friendly="Minecraft Alpha",
                node=node_name,
                running=True,
                enabled=True,
                supports_mods=True,
                supports_configs=True,
                transition_state=NodeAppTransitionState.NONE,
                player_count=1,
                player_capacity=12,
                supports_saves=True,
                supports_save_uploads=True,
                supports_save_rename=True,
                supports_settings=True,
                supports_chat=True,
                color_hex="#336699",
            )
            first_summary = NodeSystemSummary(
                cpu_percent=10,
                ram_percent=20,
                ram_used_bytes=2,
                ram_total_bytes=10,
                storage_percent=30,
                storage_free_bytes=20,
                storage_total_bytes=30,
                running_names=(),
            )
            second_summary = replace(first_summary, cpu_percent=35, running_names=("Minecraft Alpha",))
            service = NodeApiService()
            app_entries = iter(((app_entry,), (app_entry,), (app_entry,)))
            summaries = iter((first_summary, second_summary, second_summary))
            notifications: list[NodeStateStreamEvent] = []
            second_notification = asyncio.Event()

            async def list_apps() -> tuple[NodeAppEntry, ...]:
                return next(app_entries)

            def build_system_summary() -> NodeSystemSummary:
                return next(summaries)

            def on_update(update: NodeStateStreamEvent) -> None:
                notifications.append(update)
                if len(notifications) >= 2:
                    second_notification.set()

            service.list_apps = list_apps  # type: ignore[method-assign]
            service.build_system_summary = build_system_summary  # type: ignore[method-assign]

            with patch("node_api._LOCAL_NODE_STATE_SUBSCRIPTION_INTERVAL_SECONDS", 0.01):
                unsubscribe = service.subscribe_local_node_state(on_update)
                try:
                    await asyncio.wait_for(second_notification.wait(), timeout=0.2)
                finally:
                    unsubscribe()
                    await asyncio.sleep(0)

            self.assertEqual(
                notifications,
                [
                    NodeStateStreamEvent.initial(
                        node_name=node_name,
                        app_entries=(app_entry,),
                        system_summary=first_summary,
                    ),
                    NodeStateStreamEvent.system(
                        node_name=node_name,
                        system_summary=second_summary,
                    ),
                ],
            )

        asyncio.run(exercise())

    def test_local_node_system_subscription_does_not_build_app_entries(self) -> None:
        async def exercise() -> None:
            summary = NodeSystemSummary(
                cpu_percent=10,
                ram_percent=20,
                ram_used_bytes=2,
                ram_total_bytes=10,
                storage_percent=30,
                storage_free_bytes=20,
                storage_total_bytes=30,
            )
            service = NodeApiService()
            received = asyncio.Event()

            def on_update(_event: NodeStateStreamEvent) -> None:
                received.set()

            with (
                patch.object(
                    service,
                    "list_apps",
                    new=AsyncMock(side_effect=AssertionError("App entries should not be built")),
                ) as list_apps,
                patch.object(service, "build_system_summary", return_value=summary),
            ):
                unsubscribe = service.subscribe_local_node_state(
                    on_update,
                    topics=frozenset({NodeStateTopic.SYSTEM}),
                )
                try:
                    await asyncio.wait_for(received.wait(), timeout=0.2)
                finally:
                    unsubscribe()
                    await asyncio.sleep(0)

                list_apps.assert_not_awaited()

        asyncio.run(exercise())

    def test_app_footprint_size_bytes_uses_cache_for_stable_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _build_app(Mock())
            app.directory = root
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=root,
                apps_dir=root,
                scope=app.scope,
            )

            service = NodeApiService()
            with (
                patch.object(service, "_calculate_app_footprint_size_bytes", side_effect=[123, 456]) as calculate_size,
                patch("node_api.time.time", side_effect=[100.0, 120.0]),
            ):
                first = service._app_footprint_size_bytes(app)
                second = service._app_footprint_size_bytes(app)

        self.assertEqual(first, 123)
        self.assertEqual(second, 123)
        calculate_size.assert_called_once()

    def test_build_mod_list_marks_blocked_downloads(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "builtin"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(
                    name=mod_path.name,
                    directory=mod_path.parent,
                    download_block_reason=ModDownloadBlockReason.BUILTIN,
                )
            )
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = [mod]
            app = _build_app(mod_manager)

            service = NodeApiService()
            model = asyncio.run(service.build_mod_list(app))

        self.assertEqual(model.summary.downloadable_count, 0)
        self.assertEqual(model.summary.non_downloadable_count, 1)
        self.assertFalse(model.mods[0].downloadable)
        self.assertEqual(model.mods[0].mod_type, ModType.BUILTIN)
        self.assertEqual(model.mods[0].download_block_reason, "builtin")
        self.assertEqual(model.mods[0].download_block_label, "Built-in")

    def test_build_mod_list_counts_builtin_mods_as_coremods(self) -> None:
        with TemporaryDirectory() as temp_dir:
            builtin_path = Path(temp_dir) / "builtin"
            builtin_path.write_bytes(b"builtin")
            regular_path = Path(temp_dir) / "regular"
            regular_path.write_bytes(b"regular")
            builtin_mod = _TestMod(
                Mod_Config(
                    name=builtin_path.name,
                    directory=builtin_path.parent,
                    download_block_reason=ModDownloadBlockReason.BUILTIN,
                )
            )
            regular_mod = _TestMod(Mod_Config(name=regular_path.name, directory=regular_path.parent))
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = [builtin_mod, regular_mod]
            app = _build_app(mod_manager)

            service = NodeApiService()
            model = asyncio.run(service.build_mod_list(app))

        self.assertEqual(model.summary.coremod_count, 1)

    def test_build_system_summary_uses_primary_disk(self) -> None:
        fake_primary_disk = SimpleNamespace(
            mountpoint_text="/",
            display_name="System",
            percent=55,
            free_bytes=100 * 1024**3,
            total_bytes=200 * 1024**3,
        )
        fake_stats = Mock()
        fake_stats.system_snapshot.return_value = SimpleNamespace(
            cpu_percent=22,
            cpu_per_core_percent=(11, 33),
            ram_percent=44,
            ram_used_bytes=8 * 1024**3,
            ram_total_bytes=16 * 1024**3,
            primary_disk=fake_primary_disk,
            disks=(fake_primary_disk,),
        )

        with (
            patch("node_api.Stats_System", return_value=fake_stats),
            patch("node_api.time.time", return_value=10_000),
            patch("node_api.psutil.Process") as process_cls,
            patch("node_api.psutil.boot_time", return_value=6_400),
        ):
            process_cls.return_value.create_time.return_value = 9_100
            summary = NodeApiService().build_system_summary()

        self.assertEqual(
            summary,
            NodeSystemSummary(
                cpu_percent=22,
                ram_percent=44,
                ram_used_bytes=8 * 1024**3,
                ram_total_bytes=16 * 1024**3,
                storage_percent=55,
                storage_free_bytes=100 * 1024**3,
                storage_total_bytes=200 * 1024**3,
                cpu_per_core_percent=(11, 33),
                disks=(
                    NodeSystemDiskSummary(
                        mountpoint="/",
                        label="System",
                        percent=55,
                        free_bytes=100 * 1024**3,
                        total_bytes=200 * 1024**3,
                    ),
                ),
                bot_uptime_seconds=900,
                uptime_seconds=3_600,
                running_names=(),
                captured_at_epoch_seconds=10_000,
            ),
        )

    def test_system_history_round_trip_preserves_typed_samples(self) -> None:
        history = NodeSystemHistory(
            retention_seconds=3600,
            sample_interval_seconds=10,
            samples=(
                NodeSystemSample(
                    captured_at_epoch_seconds=100,
                    cpu_percent=20,
                    ram_percent=30,
                    storage_percent=40,
                ),
            ),
        )

        self.assertEqual(NodeSystemHistory.from_mapping(history.to_mapping()), history)

    def test_schedule_system_action_requires_sudo_and_dispatches_once(self) -> None:
        async def exercise() -> None:
            service = NodeApiService()
            acl = AsyncMock()
            handler = Mock()
            service.set_acl(cast(Access_Control, acl))
            service.set_system_action_handler(handler)
            loop = asyncio.get_running_loop()
            with patch.object(loop, "call_later") as call_later:
                result = await service.schedule_system_action(
                    action=NodeSystemAction.RESTART_PROCESS,
                    auto_restart_running_apps=False,
                    silent=True,
                    actor_user_id=42,
                )

            acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
            self.assertEqual(result.action, NodeSystemAction.RESTART_PROCESS)
            dispatch = call_later.call_args.args[1]
            dispatch()
            handler.assert_called_once_with(NodeSystemAction.RESTART_PROCESS, False, True)

            with self.assertRaises(HTTPException) as raised:
                await service.schedule_system_action(
                    action=NodeSystemAction.REBOOT_HOST,
                    auto_restart_running_apps=True,
                    silent=False,
                    actor_user_id=42,
                )
            self.assertEqual(raised.exception.status_code, 409)

        asyncio.run(exercise())

    def test_schedule_portal_action_is_available_on_yuki(self) -> None:
        async def exercise() -> None:
            service = NodeApiService()
            acl = AsyncMock()
            handler = Mock()
            service.set_acl(cast(Access_Control, acl))
            service.set_system_action_handler(handler)
            loop = asyncio.get_running_loop()
            with patch.object(loop, "call_later") as call_later:
                result = await service.schedule_system_action(
                    action=NodeSystemAction.RESTART_PORTAL,
                    auto_restart_running_apps=True,
                    silent=False,
                    actor_user_id=42,
                )

            self.assertEqual(result.message, f"Scheduled Portal restart for {service.node_name}.")
            call_later.call_args.args[1]()
            handler.assert_called_once_with(NodeSystemAction.RESTART_PORTAL, True, False)
            self.assertIsNone(service._pending_system_action)  # type: ignore[attr-defined]

        asyncio.run(exercise())

    def test_restart_schedule_state_round_trip_and_sudo_update(self) -> None:
        async def exercise() -> None:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "configuration.json"
                with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                    maintenance = MaintenanceService()
                    maintenance.update_restart_intervals(
                        {RestartTarget.BOT: 90},
                        anchor_timestamp=int(datetime.fromisoformat("2026-07-01T10:00:00+10:00").timestamp()),
                        saved_at_timestamp=int(datetime.fromisoformat("2026-07-01T08:00:00+10:00").timestamp()),
                    )
                    triggered_at = datetime.fromisoformat("2026-07-01T10:30:00+10:00")
                    maintenance.mark_triggered((RestartTarget.BOT,), triggered_at=triggered_at)
                    service = NodeApiService()
                    acl = AsyncMock()
                    service.set_acl(cast(Access_Control, acl))
                    service.set_maintenance_service(
                        maintenance,
                        (RestartTarget.BOT, RestartTarget.SYSTEM),
                    )

                    state = service.read_restart_schedules()
                    self.assertEqual(NodeRestartScheduleState.from_mapping(state.to_mapping()), state)
                    self.assertEqual([entry.target for entry in state.schedules], [RestartTarget.BOT, RestartTarget.SYSTEM])
                    self.assertTrue(state.schedules[0].enabled)
                    self.assertEqual(state.schedules[0].interval_minutes, 90)
                    self.assertEqual(state.schedules[0].last_triggered_timestamp, int(triggered_at.timestamp()))

                    updated = await service.update_restart_schedule(
                        target=RestartTarget.SYSTEM,
                        interval_minutes=7 * 24 * 60,
                        anchor_timestamp=int(datetime.fromisoformat("2026-07-02T10:00:00+10:00").timestamp()),
                        actor_user_id=42,
                    )

                    acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
                    system_schedule = next(entry for entry in updated.schedules if entry.target is RestartTarget.SYSTEM)
                    self.assertTrue(system_schedule.enabled)
                    self.assertEqual(system_schedule.interval_minutes, 7 * 24 * 60)

        asyncio.run(exercise())

    def test_restart_state_uses_process_sentinel_and_optional_voice_record(self) -> None:
        service = NodeApiService()
        with (
            patch("node_api.psutil.Process") as process_cls,
            patch(
                "node_api.read_process_restart_record",
                return_value=RestartRecord(timestamp=1_782_909_000, kind=RestartKind.MANUAL_SYS),
            ) as read_process,
            patch(
                "node_api.read_voice_restart_record",
                return_value=RestartRecord(timestamp=1_782_912_600, kind=RestartKind.MANUAL_VOICE),
            ),
        ):
            process_cls.return_value.create_time.return_value = 1_782_800_000
            state = service.read_restart_state()

        read_process.assert_called_once_with(default_timestamp=1_782_800_000)
        self.assertEqual(
            state,
            NodeRestartState(
                node=service.node_name,
                process=NodeRestartRecord(timestamp=1_782_909_000, kind=RestartKind.MANUAL_SYS),
                voice=NodeRestartRecord(timestamp=1_782_912_600, kind=RestartKind.MANUAL_VOICE),
            ),
        )
        self.assertEqual(NodeRestartState.from_mapping(state.to_mapping()), state)

    def test_skip_restart_schedule_requires_sudo_and_advances_next_restart(self) -> None:
        async def exercise() -> None:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "configuration.json"
                with patch.object(MaintenanceService, "_BOT_CONFIGURATION_PATH", config_path):
                    maintenance = MaintenanceService()
                    anchor_timestamp = int(datetime.fromisoformat("2026-07-01T10:00:00+10:00").timestamp())
                    maintenance.update_restart_intervals(
                        {RestartTarget.BOT: 90},
                        anchor_timestamp=anchor_timestamp,
                        saved_at_timestamp=int(datetime.fromisoformat("2026-07-01T08:00:00+10:00").timestamp()),
                    )
                    service = NodeApiService()
                    acl = AsyncMock()
                    service.set_acl(cast(Access_Control, acl))
                    service.set_maintenance_service(maintenance, (RestartTarget.BOT,))

                    updated = await service.skip_restart_schedule(
                        target=RestartTarget.BOT,
                        actor_user_id=42,
                    )

                    acl.perm_check.assert_awaited_once_with(42, Power_Level.sudo)
                    entry = updated.schedules[0]
                    self.assertEqual(entry.skipped_through_timestamp, anchor_timestamp)
                    self.assertEqual(entry.next_restart_timestamp, anchor_timestamp + 90 * 60)
                    self.assertEqual(NodeRestartScheduleState.from_mapping(updated.to_mapping()), updated)

        asyncio.run(exercise())

    def test_build_system_summary_lists_running_app_names(self) -> None:
        fake_stats = Mock()
        fake_stats.system_snapshot.return_value = SimpleNamespace(
            cpu_percent=12,
            cpu_per_core_percent=(10, 14),
            ram_percent=30,
            ram_used_bytes=2 * 1024**3,
            ram_total_bytes=8 * 1024**3,
            primary_disk=None,
            disks=(),
        )
        service = NodeApiService()
        service.set_manager(
            cast(
                Any,
                SimpleNamespace(
                    apps={
                        "minecraft_alpha": SimpleNamespace(
                            name="minecraft_alpha",
                            friendly="Minecraft Alpha",
                            check_running=lambda: True,
                        ),
                        "factorio_lab": SimpleNamespace(
                            name="factorio_lab",
                            friendly="Factorio Lab",
                            check_running=lambda: True,
                        ),
                        "beammp_test": SimpleNamespace(
                            name="beammp_test",
                            friendly="BeamMP Test",
                            check_running=lambda: False,
                        ),
                    },
                    start_blocker=lambda app, include_current_activity=False: (
                        AppStartBlocker(
                            kind=AppStartBlockerKind.CPU_POINTS,
                            message=f"Cannot start {app.friendly}; insufficient CPU points.",
                        )
                        if app.name == "beammp_test"
                        else None
                    ),
                ),
            )
        )

        with (
            patch("node_api.Stats_System", return_value=fake_stats),
            patch("node_api.time.time", return_value=12_000),
            patch("node_api.psutil.Process") as process_cls,
            patch("node_api.psutil.boot_time", return_value=10_200),
        ):
            process_cls.return_value.create_time.return_value = 11_700
            summary = service.build_system_summary()

        self.assertEqual(summary.running_names, ("Factorio Lab", "Minecraft Alpha"))
        self.assertEqual(summary.running_app_ids, ("factorio_lab", "minecraft_alpha"))
        self.assertEqual(summary.running_app_scopes, ("factorio", "minecraft"))
        self.assertEqual(summary.start_blocked_app_ids, ("beammp_test",))
        self.assertEqual(summary.bot_uptime_seconds, 300)
        self.assertEqual(summary.uptime_seconds, 1_800)

    def test_build_config_list_uses_app_config_files(self) -> None:
        app = _build_app(Mock())
        config_file = AppConfigFile(
            id="server/server.properties",
            label="server.properties",
            relative_path="server.properties",
            root_id="server",
            root_label="Server Properties",
            kind=AppConfigFileKind.GAME,
            read_power_level=Power_Level.user,
            write_power_level=Power_Level.sudo,
            size_bytes=14,
            modified_at=datetime(2026, 5, 26, 12, 0, 0),
        )
        app.list_config_files = Mock(return_value=(config_file,))  # type: ignore[method-assign]

        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        with patch.object(config, "MOD_WEB_SERVER", server):
            service = NodeApiService()
            model = service.build_config_list(app)

        self.assertIsInstance(model, NodeConfigList)
        self.assertEqual(model.node, "erin")
        self.assertEqual(model.configs[0].id, "server/server.properties")
        self.assertEqual(model.configs[0].kind, "game")
        self.assertEqual(model.configs[0].read_power_level, Power_Level.user)
        self.assertEqual(model.configs[0].write_power_level, Power_Level.sudo)
        self.assertIn("B", model.configs[0].size_text)

    def test_build_config_list_filters_entries_above_actor_level(self) -> None:
        app = _build_app(Mock())
        public_config = AppConfigFile(
            id="public/visitor.toml",
            label="visitor.toml",
            relative_path="visitor.toml",
            root_id="public",
            root_label="Public Configs",
            kind=AppConfigFileKind.MOD,
            read_power_level=Power_Level.visitor,
            write_power_level=Power_Level.sudo,
            size_bytes=12,
            modified_at=datetime(2026, 5, 26, 12, 0, 0),
        )
        private_config = AppConfigFile(
            id="private/admin.toml",
            label="admin.toml",
            relative_path="admin.toml",
            root_id="private",
            root_label="Private Configs",
            kind=AppConfigFileKind.GAME,
            read_power_level=Power_Level.user,
            write_power_level=Power_Level.root,
            size_bytes=13,
            modified_at=datetime(2026, 5, 26, 12, 1, 0),
        )
        app.list_config_files = Mock(return_value=(public_config, private_config))  # type: ignore[method-assign]

        service = NodeApiService()
        acl = Access_Control(pointer=Path("missing-users.json"))
        acl._roles = {42: Power_Level.visitor}  # type: ignore[attr-defined]
        service.set_acl(acl)

        model = service.build_config_list(app, actor_user_id=42)

        self.assertEqual([entry.id for entry in model.configs], ["public/visitor.toml"])

    def test_read_config_file_wraps_app_content(self) -> None:
        app = _build_app(Mock())
        config_file = AppConfigFile(
            id="server/server.properties",
            label="server.properties",
            relative_path="server.properties",
            root_id="server",
            root_label="Server Properties",
            kind=AppConfigFileKind.GAME,
            read_power_level=Power_Level.user,
            write_power_level=Power_Level.sudo,
            size_bytes=11,
            modified_at=datetime(2026, 5, 26, 12, 0, 0),
        )
        app.read_config_file = Mock(return_value=AppConfigFileContent(file=config_file, content="motd=hello\n"))  # type: ignore[method-assign]

        service = NodeApiService()
        content = service.read_config_file(app=app, config_id="server/server.properties")

        self.assertEqual(content.app_name, "minecraft_alpha")
        self.assertEqual(content.content, "motd=hello\n")
        self.assertEqual(content.config.relative_path, "server.properties")

    def test_config_root_download_creates_archive_from_visible_root_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            root_path = temp_path / "config"
            root_path.mkdir()
            first_path = root_path / "alpha.toml"
            first_path.write_text("enabled=true\n", encoding="utf-8")
            nested_path = root_path / "nested"
            nested_path.mkdir()
            second_path = nested_path / "beta.json"
            second_path.write_text("{}", encoding="utf-8")
            archive_path = temp_path / "Minecraft_Alpha_mod-configs_configs.zip"

            class _ConfigDownloadApp(_DummyApp):
                @property
                def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
                    return (
                        AppConfigFileRoot(
                            id="mod-configs",
                            label="Mod Configs",
                            path=root_path,
                            kind=AppConfigFileKind.MOD,
                            read_power_level_override=Power_Level.visitor,
                        ),
                    )

            app = _build_app(Mock())
            app.__class__ = _ConfigDownloadApp

            with patch("node_api.File_Utils.compress", new=AsyncMock(return_value=archive_path)) as compress:
                service = NodeApiService()
                response = asyncio.run(
                    service.build_config_root_download_response(app=app, root_id="mod-configs", actor_user_id=None)
                )

        self.assertEqual(Path(response.path), archive_path)
        compress.assert_awaited_once_with(
            (first_path, second_path),
            "Minecraft_Alpha_mod-configs_configs.zip",
            arc_base=root_path,
        )

    def test_factorio_mod_settings_state_reports_missing_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=Path(temp_dir),
            )

            state = NodeApiService().factorio_mod_settings_state(app=cast(App[Any], app))

        self.assertIsInstance(state, NodeFactorioModSettings)
        self.assertFalse(state.file_exists)
        self.assertIsNone(state.size_bytes)

    def test_factorio_mod_settings_state_reports_existing_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            settings_path = app_dir / "mods" / "mod-settings.dat"
            settings_path.parent.mkdir()
            settings_path.write_bytes(b"factorio-settings")
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=app_dir,
            )

            state = NodeApiService().factorio_mod_settings_state(app=cast(App[Any], app))

        self.assertTrue(state.file_exists)
        self.assertEqual(state.size_bytes, len(b"factorio-settings"))
        self.assertIsNotNone(state.size_text)
        self.assertIsNotNone(state.modified_at)

    def test_delete_factorio_mod_settings_removes_file_and_reports_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            settings_path = app_dir / "mods" / "mod-settings.dat"
            settings_path.parent.mkdir()
            settings_path.write_bytes(b"factorio-settings")
            app = SimpleNamespace(
                name="factorio_lab",
                friendly="Factorio Lab",
                scope="factorio",
                directory=app_dir,
            )

            state = NodeApiService().delete_factorio_mod_settings(app=cast(App[Any], app))

            self.assertFalse(settings_path.exists())

        self.assertFalse(state.file_exists)

    def test_list_apps_uses_lowest_config_root_read_level(self) -> None:
        app = _build_app(Mock())
        app.manage_embed_color = 0x22C55E
        app.config_file_read_level_override = Power_Level.user
        app.config_file_write_level_override = Power_Level.sudo
        app.save_file_write_level_override = Power_Level.user

        class _ConfigRootsApp(_DummyApp):
            @property
            def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
                return (
                    AppConfigFileRoot(
                        id="public",
                        label="Public Configs",
                        path=Path("/tmp/public"),
                        kind=AppConfigFileKind.MOD,
                        read_power_level_override=Power_Level.visitor,
                    ),
                    AppConfigFileRoot(
                        id="private",
                        label="Private Configs",
                        path=Path("/tmp/private"),
                        kind=AppConfigFileKind.GAME,
                    ),
                )

        app.__class__ = _ConfigRootsApp
        manager = cast(Any, SimpleNamespace(apps={app.name: app}))

        service = NodeApiService()
        service.set_manager(manager)

        entries = asyncio.run(service.list_apps())

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].config_read_level, Power_Level.visitor)

    def test_single_mod_directory_download_is_zipped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mod_path = temp_path / "DirectoryMod"
            mod_path.mkdir()
            (mod_path / "mod.txt").write_text("mod-data", encoding="utf-8")
            archive_path = temp_path / "zips" / "Minecraft_Alpha_DirectoryMod.zip"
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=mod_path.parent))
            app = _build_app(Mock(get=Mock(return_value=mod), reload_mods=AsyncMock()))

            with patch("node_api.compress_mod_archive_entries", new=AsyncMock(return_value=archive_path)) as compress:
                service = NodeApiService()
                download = asyncio.run(service._single_mod_download_file(app=app, mod=mod))

        self.assertTrue(download.is_archive)
        self.assertEqual(download.filename, "Minecraft_Alpha_DirectoryMod.zip")
        self.assertEqual(download.path, archive_path)
        archive_entries, archive_name = compress.await_args.args
        self.assertEqual(tuple(entry.source_path for entry in archive_entries), (mod_path,))
        self.assertEqual(tuple(entry.archive_path.as_posix() for entry in archive_entries), ("DirectoryMod",))
        self.assertEqual(archive_name, "Minecraft_Alpha_DirectoryMod.zip")

    def test_single_disabled_file_mod_download_uses_logical_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            disabled_path = temp_path / "disabled.jar.disabled"
            disabled_path.write_bytes(b"disabled")
            mod = _TestMod(
                Mod_Config(
                    name="disabled.jar",
                    directory=temp_path,
                    placement=ModPlacement.SERVER_DISABLED,
                )
            )
            app = _build_app(Mock(get=Mock(return_value=mod), reload_mods=AsyncMock()))

            download = asyncio.run(NodeApiService()._single_mod_download_file(app=app, mod=mod))

        self.assertFalse(download.is_archive)
        self.assertEqual(download.path, disabled_path)
        self.assertEqual(download.filename, "disabled.jar")

    def test_selected_mod_download_creates_archive_from_selected_mods(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            first_path = temp_path / "first.jar"
            second_path = temp_path / "second.jar"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            archive_path = temp_path / "Minecraft_Alpha_selected_mods.zip"
            first = _TestMod(Mod_Config(name=first_path.name, directory=first_path.parent))
            second = _TestMod(Mod_Config(name=second_path.name, directory=second_path.parent))
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = {"first.jar": first, "second.jar": second}.__getitem__
            app = _build_app(mod_manager)

            with patch("node_api.compress_mod_archive_entries", new=AsyncMock(return_value=archive_path)) as compress:
                service = NodeApiService()
                response = asyncio.run(
                    service.build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(mod_names=("first.jar", "second.jar")),
                    )
                )

        self.assertEqual(Path(response.path), archive_path)
        archive_entries, archive_name = compress.await_args.args
        self.assertEqual(tuple(entry.source_path for entry in archive_entries), (first_path, second_path))
        self.assertEqual(
            tuple(entry.archive_path.as_posix() for entry in archive_entries),
            ("first.jar", "second.jar"),
        )
        self.assertEqual(archive_name, "Minecraft_Alpha_selected_mods.zip")

    def test_excluded_mod_selection_archives_the_complement(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mods = tuple(
                _TestMod(Mod_Config(name=f"{name}.jar", directory=temp_path))
                for name in ("first", "second", "third")
            )
            for mod in mods:
                mod.storage_path.write_bytes(mod.name.encode())
            archive_path = temp_path / "Minecraft_Alpha_selected_mods.zip"
            mods_by_name = {mod.name: mod for mod in mods}
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = mods_by_name.__getitem__
            mod_manager.list_mods.return_value = mods
            app = _build_app(mod_manager)

            with patch("node_api.compress_mod_archive_entries", new=AsyncMock(return_value=archive_path)) as compress:
                response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(
                            mod_names=("second.jar",),
                            selected_only=True,
                            excluded_only=True,
                        ),
                    )
                )

        self.assertEqual(Path(response.path), archive_path)
        archive_entries, _archive_name = compress.await_args.args
        self.assertEqual(
            tuple(entry.archive_path.as_posix() for entry in archive_entries),
            ("first.jar", "third.jar"),
        )

    def test_single_directory_mod_archive_keeps_mod_folder_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            zips_path.mkdir()
            mod_path = temp_path / "DirectoryMod"
            mod_path.mkdir()
            (mod_path / "mod.txt").write_text("mod-data", encoding="utf-8")
            mod = _TestMod(Mod_Config(name=mod_path.name, directory=mod_path.parent))
            app = _build_app(Mock(get=Mock(return_value=mod), reload_mods=AsyncMock()))

            with patch.object(config, "DIR_ZIPS", zips_path):
                service = NodeApiService()
                download = asyncio.run(service._single_mod_download_file(app=app, mod=mod))
                with zipfile.ZipFile(download.path) as archive:
                    self.assertEqual(sorted(archive.namelist()), ["DirectoryMod/mod.txt"])

    def test_selected_directory_mod_download_preserves_each_folder_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            zips_path.mkdir()
            first_path = temp_path / "mod_folder_A"
            second_path = temp_path / "mod_folder_B"
            first_path.mkdir()
            second_path.mkdir()
            (first_path / "config.json").write_text("{}", encoding="utf-8")
            nested_path = second_path / "Resources"
            nested_path.mkdir()
            (nested_path / "payload.xml").write_text("<xml />", encoding="utf-8")
            first = _TestMod(Mod_Config(name=first_path.name, directory=first_path.parent))
            second = _TestMod(Mod_Config(name=second_path.name, directory=second_path.parent))
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = {first.name: first, second.name: second}.__getitem__
            app = _build_app(mod_manager)

            with patch.object(config, "DIR_ZIPS", zips_path):
                service = NodeApiService()
                response = asyncio.run(
                    service.build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(mod_names=(first.name, second.name)),
                    )
                )
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(
                        sorted(archive.namelist()),
                        ["mod_folder_A/config.json", "mod_folder_B/Resources/", "mod_folder_B/Resources/payload.xml"],
                    )

    def test_selected_mixed_mod_download_preserves_file_and_folder_layout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            zips_path.mkdir()
            file_path = temp_path / "alpha.jar"
            file_path.write_bytes(b"jar")
            folder_path = temp_path / "mod_folder_B"
            folder_path.mkdir()
            (folder_path / "ModInfo.xml").write_text("<mod />", encoding="utf-8")
            file_mod = _TestMod(Mod_Config(name=file_path.name, directory=file_path.parent))
            folder_mod = _TestMod(Mod_Config(name=folder_path.name, directory=folder_path.parent))
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = {file_mod.name: file_mod, folder_mod.name: folder_mod}.__getitem__
            app = _build_app(mod_manager)

            with patch.object(config, "DIR_ZIPS", zips_path):
                service = NodeApiService()
                response = asyncio.run(
                    service.build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(mod_names=(file_mod.name, folder_mod.name)),
                    )
                )
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(sorted(archive.namelist()), ["alpha.jar", "mod_folder_B/ModInfo.xml"])

    def test_marker_backed_mods_archive_with_logical_names(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            disabled_path = temp_path / "disabled.jar.disabled"
            client_path = temp_path / "client.jar.client"
            disabled_path.write_bytes(b"disabled")
            client_path.write_bytes(b"client")
            disabled = _TestMod(
                Mod_Config(
                    name="disabled.jar",
                    directory=temp_path,
                    placement=ModPlacement.SERVER_DISABLED,
                )
            )
            client = _TestMod(
                Mod_Config(
                    name="client.jar",
                    directory=temp_path,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = {disabled.name: disabled, client.name: client}.__getitem__
            app = _build_app(mod_manager)

            with patch.object(config, "DIR_ZIPS", zips_path):
                response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(mod_names=(disabled.name, client.name)),
                    )
                )
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(sorted(archive.namelist()), ["client.jar", "disabled.jar"])

    def test_client_pack_archive_includes_downloadable_server_only_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            regular_path = temp_path / "regular.jar"
            client_path = temp_path / "client.jar.client"
            server_path = temp_path / "server.jar"
            for pointer in (regular_path, client_path, server_path):
                pointer.write_bytes(pointer.name.encode())
            regular = _TestMod(Mod_Config(name="regular.jar", directory=temp_path))
            client = _TestMod(
                Mod_Config(
                    name="client.jar",
                    directory=temp_path,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )
            server = _TestMod(
                Mod_Config(
                    name="server.jar",
                    directory=temp_path,
                    classification_override=ModClassificationOverride(
                        mod_type=ModType.SERVER,
                        download_block_reason=None,
                    ),
                )
            )
            mods = {mod.name: mod for mod in (regular, client, server)}
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = mods.__getitem__
            mod_manager.list_mods.return_value = tuple(mods.values())
            app = _build_app(mod_manager)

            with patch.object(config, "DIR_ZIPS", zips_path):
                response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(
                            client_pack=True,
                            publish_client_pack=True,
                            publish_changelog="Initial client pack.",
                        ),
                    )
                )
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(
                        sorted(archive.namelist()),
                        ["client.jar", "regular.jar", "server.jar"],
                    )

                selected_response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(
                            mod_names=(server.name,),
                            selected_only=True,
                            client_pack=True,
                        ),
                    )
                )
                with zipfile.ZipFile(Path(selected_response.path)) as archive:
                    self.assertIn("server.jar", archive.namelist())

    def test_client_pack_archive_includes_client_overrides(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            overrides_path = temp_path / "client-overrides"
            overrides_path.mkdir()
            (overrides_path / "options.txt").write_text("client settings", encoding="utf-8")
            (temp_path / "required.jar").write_bytes(b"required")
            (temp_path / "client.jar.client").write_bytes(b"client")
            required = _TestMod(Mod_Config(name="required.jar", directory=temp_path))
            client = _TestMod(
                Mod_Config(
                    name="client.jar",
                    directory=temp_path,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )
            mods = {mod.name: mod for mod in (required, client)}
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.get.side_effect = mods.__getitem__
            mod_manager.list_mods.return_value = tuple(mods.values())
            app = _build_app(mod_manager)
            app.cfg.client_overrides_dir = overrides_path

            with patch.object(config, "DIR_ZIPS", zips_path):
                response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(
                            client_pack=True,
                            publish_client_pack=True,
                            publish_changelog="Include client overrides.",
                        ),
                    )
                )
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(
                        set(archive.namelist()),
                        {"required.jar", "client.jar", "overrides/options.txt"},
                    )

    def test_client_pack_archive_supports_modrinth_format(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            (temp_path / "client.jar.client").write_bytes(b"client")
            client = _TestMod(
                Mod_Config(
                    name="client.jar",
                    directory=temp_path,
                    placement=ModPlacement.CLIENT_ONLY,
                )
            )
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = (client,)
            app = _build_app(mod_manager)
            app.cfg.version = AppVersion(
                main="1.21.1",
                loader="fabric",
                framework="0.16.10",
            )

            with patch.object(config, "DIR_ZIPS", zips_path):
                response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(
                            client_pack=True,
                            pack_format=PackFormat.MODRINTH,
                            publish_client_pack=True,
                            publish_changelog="Publish Modrinth pack.",
                        ),
                    )
                )
                self.assertTrue(str(response.path).endswith(".mrpack"))
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(
                        set(archive.namelist()),
                        {"modrinth.index.json", "overrides/mods/client.jar"},
                    )
                    index = json.loads(archive.read("modrinth.index.json"))
                    self.assertRegex(index["versionId"], r"^\d{4}-\d{2}-\d{2}(?:\.\d+)?$")

    def test_client_pack_download_blocks_after_published_content_changes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            mod_path = temp_path / "client.jar.client"
            mod_path.write_bytes(b"published")
            client = _TestMod(
                Mod_Config(name="client.jar", directory=temp_path, placement=ModPlacement.CLIENT_ONLY)
            )
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = (client,)
            app = _build_app(mod_manager)
            service = NodeApiService()

            with patch.object(config, "DIR_ZIPS", zips_path):
                asyncio.run(
                    service.build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(
                            client_pack=True,
                            publish_client_pack=True,
                            publish_changelog="Publish client pack.",
                        ),
                    )
                )
                mod_path.write_bytes(b"changed")
                with self.assertRaisesRegex(HTTPException, "publish or regenerate") as raised:
                    asyncio.run(
                        service.build_mod_download_response(
                            app=app,
                            request=NodeDownloadRequest(client_pack=True),
                        )
                    )

            self.assertEqual(raised.exception.status_code, 409)

    def test_client_pack_download_blocks_while_configuration_is_dirty_even_when_hash_matches(self) -> None:
        manager = Mock()
        manager.reload_mods = AsyncMock()
        app = _build_app(manager)
        app.cfg.client_pack_current_hash = "a" * 64
        app.cfg.client_pack_published_hash = "a" * 64
        app.cfg.client_pack_published_version = "2026-07-04"
        app.cfg.client_pack_content_dirty = True

        with self.assertRaisesRegex(HTTPException, "unpublished changes") as raised:
            asyncio.run(
                NodeApiService().build_mod_download_response(
                    app=app,
                    request=NodeDownloadRequest(client_pack=True),
                )
            )

        self.assertEqual(raised.exception.status_code, 409)

    def test_factorio_rejects_client_pack_generation(self) -> None:
        mod_manager = Mock()
        mod_manager.reload_mods = AsyncMock()
        app = _build_app(mod_manager)
        app.scope = "factorio"
        app.cfg.scope = "factorio"

        with self.assertRaisesRegex(HTTPException, "does not support client pack"):
            asyncio.run(
                NodeApiService().build_mod_download_response(
                    app=app,
                    request=NodeDownloadRequest(client_pack=True),
                )
            )

    def test_server_pack_includes_enabled_non_downloadable_server_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zips_path = temp_path / "zips"
            (temp_path / "server.jar").write_bytes(b"server")
            server = _TestMod(
                Mod_Config(
                    name="server.jar",
                    directory=temp_path,
                    classification_override=ModClassificationOverride(
                        mod_type=ModType.SERVER,
                        download_block_reason=ModDownloadBlockReason.SERVER_ONLY,
                    ),
                )
            )
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = (server,)
            app = _build_app(mod_manager)

            with patch.object(config, "DIR_ZIPS", zips_path):
                response = asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(pack_purpose=PackPurpose.SERVER),
                    )
                )
                with zipfile.ZipFile(Path(response.path)) as archive:
                    self.assertEqual(archive.namelist(), ["server.jar"])

    def test_client_pack_archive_rejects_non_downloadable_required_mod(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "blocked.jar").write_bytes(b"blocked")
            blocked = _TestMod(
                Mod_Config(
                    name="blocked.jar",
                    directory=temp_path,
                    classification_override=ModClassificationOverride(
                        mod_type=ModType.CLIENT,
                        download_block_reason=ModDownloadBlockReason.ARTIFACT,
                    ),
                )
            )
            mod_manager = Mock()
            mod_manager.reload_mods = AsyncMock()
            mod_manager.list_mods.return_value = (blocked,)
            app = _build_app(mod_manager)

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    NodeApiService().build_mod_download_response(
                        app=app,
                        request=NodeDownloadRequest(client_pack=True),
                    )
                )

            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("must be downloadable", str(raised.exception.detail))

    def test_empty_selected_mod_download_returns_404(self) -> None:
        mod_manager = Mock()
        mod_manager.reload_mods = AsyncMock()
        app = _build_app(mod_manager)

        service = NodeApiService()
        with self.assertRaises(Exception) as raised:
            asyncio.run(
                service.build_mod_download_response(
                    app=app,
                    request=NodeDownloadRequest(selected_only=True),
                )
            )

        self.assertEqual(getattr(raised.exception, "status_code"), 404)
        self.assertEqual(getattr(raised.exception, "detail"), "No selected downloadable mods found.")
        mod_manager.get.assert_not_called()

    def test_blocked_single_mod_download_returns_403(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mod_path = Path(temp_dir) / "builtin"
            mod_path.write_bytes(b"mod-data")
            mod = _TestMod(
                Mod_Config(
                    name=mod_path.name,
                    directory=mod_path.parent,
                    download_block_reason=ModDownloadBlockReason.BUILTIN,
                )
            )
            app = _build_app(Mock(get=Mock(return_value=mod), reload_mods=AsyncMock()))

            service = NodeApiService()
        with self.assertRaises(Exception) as raised:
            asyncio.run(service.build_mod_download_response(app=app, request=NodeDownloadRequest(mod_name=mod.name)))

        self.assertEqual(getattr(raised.exception, "status_code"), 403)

    def test_queue_relay_tts_returns_skipped_reason_for_runtime_error(self) -> None:
        service = NodeApiService()
        relay_tts = Mock()
        relay_tts.queue_relay_message = AsyncMock(side_effect=RuntimeError("Relay author is not listening to TTS."))
        service.set_relay_tts_service(cast(Any, relay_tts))

        result = asyncio.run(
            service.queue_relay_tts(
                NodeRelayTTSRequest(
                    guild_id=123,
                    channel_id=456,
                    message_id=789,
                    text="Stone Age",
                    user_id=42,
                    source_app="minecraft_survival",
                    player_name="Alice",
                )
            )
        )

        self.assertFalse(result.queued)
        self.assertEqual(result.reason, "Relay author is not listening to TTS.")

    def test_remote_relay_tts_forwarder_posts_signed_request_for_yuki(self) -> None:
        yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="123456789012345678",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="https://yuki.example",
                    node_api_base_url="https://yuki.example/api/node",
                )
            ),
        )
        bot_config = config.BotConfiguration(KnownBots={yuki_snapshot.profile.id: yuki_snapshot})
        server = replace(
            config.MOD_WEB_SERVER,
            node_name="erin",
            token_secret="secret",
        )

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(config_path, bot_config)
            forwarder = RemoteRelayTTSForwarder()
            forwarder._bot_configuration_path = config_path

            response = {"queued": True, "spoken": "Stone Age", "queue_size": 3}
            requests_response = Mock(status_code=200)
            requests_response.json.return_value = response

            with (
                patch.object(config, "MOD_WEB_SERVER", server),
                patch("node_api.requests.post", return_value=requests_response) as post_mock,
            ):
                spoken, queue_size = asyncio.run(
                    forwarder.queue_discord_relay_message(
                        123,
                        456,
                        789,
                        "Stone Age",
                        user_id=42,
                        source_app="minecraft_survival",
                        player_name="Alice",
                    )
                )

        self.assertEqual(spoken, "Stone Age")
        self.assertEqual(queue_size, 3)
        post_kwargs = post_mock.call_args.kwargs
        self.assertEqual(post_mock.call_args.args[0], "https://yuki.example/api/node/relay/tts")
        self.assertEqual(
            post_kwargs["json"],
            {
                "guild_id": 123,
                "channel_id": 456,
                "message_id": 789,
                "text": "Stone Age",
                "user_id": 42,
                "source_app": "minecraft_survival",
                "player_name": "Alice",
            },
        )
        token = post_kwargs["headers"]["Authorization"].split(" ", 1)[1]
        grant = verify_node_token(
            secret="secret",
            token=token,
            node="yuki",
            app=None,
            required_scopes=(NodeApiScope.RELAY_TTS,),
        )
        self.assertEqual(grant.subject, "relay-tts:erin")


if __name__ == "__main__":
    unittest.main()
