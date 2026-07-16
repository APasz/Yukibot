from __future__ import annotations

import asyncio
import base64
import json
import unittest
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, cast
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import requests
from aiohttp.client_reqrep import RequestInfo
from modmux.models import Provider
from multidict import CIMultiDict, CIMultiDictProxy
from nicegui.elements.link import Link
from yarl import URL

import config
from _minecraft_heads import minecraft_dev_bypass_head_data_uri
from _security import Access_Control, Power_Level
from apps._app import AppRuntimeFault, AppRuntimeFaultKind, AppVersionSource, ChatRelaySupport
from apps._config import (
    AppTitleFont,
    BulkLauncherMetadataDiscovery,
    ClientPackConfig,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    ClientPackPolicy,
    ClientPackRelease,
    LauncherMetadataCandidate,
    LauncherMetadataDiscovery,
    LauncherMetadataMatchReason,
    LauncherMetadataProviderCandidates,
    LauncherMetadataResolution,
    LauncherProviderUrls,
    ModDistributionMode,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageCandidate,
    ModPageDiscovery,
    ModPageLink,
    ModPageMatchConfidence,
    ModPageMatchReason,
    ModPageProviderCandidates,
    ModPlacement,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
    is_client_pack_candidate,
)
from apps._updater import (
    AppUpdateBranchState,
    AppUpdateInfo,
    AppUpdateOperationKind,
    AppUpdateProviderKind,
    AppUpdateState,
    AppUpdateStatus,
)
from apps.minecraft import (
    Minecraft,
    MinecraftCookingRecipe,
    MinecraftItemRegistrySnapshot,
    MinecraftRecipeBook,
    MinecraftRecipeIngredient,
    MinecraftRecipeItemStack,
    MinecraftRecipeKind,
    MinecraftRecipeRemoval,
    MinecraftRecipeRemovalFilter,
    MinecraftShapedRecipe,
    MinecraftShapelessRecipe,
)
from apps.minecraft.pack_export import PackFormat, PackPurpose
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
    ChatRoomUpdate,
)
from config import BotConfiguration, BotMetadataSnapshot, ModWebServerConfig
from font_assets import FontAssetEntry, font_assets
from mod_web_auth import ModWebUser
from node_api import (
    ClientPackFilePreview,
    ConsoleResponseSource,
    NodeAppActivityProviderEntry,
    NodeAppEntry,
    NodeAppMutationAction,
    NodeAppResourcePointSummary,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeBlueprintEntry,
    NodeBlueprintList,
    NodeBulkLauncherMetadataApplyResult,
    NodeChatEndpointSummary,
    NodeChatRoomSnapshot,
    NodeChatStreamEvent,
    NodeChatStreamEventKind,
    NodeConfigEntry,
    NodeConfigList,
    NodeConsoleActionEntry,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleActionParameter,
    NodeDiskEntry,
    NodeDiskManagementState,
    NodeMinecraftItemRegistryState,
    NodeMinecraftRecipeBookState,
    NodeMinecraftRecipeMutationAction,
    NodeMinecraftRecipeMutationResult,
    NodeMinecraftRecipeWorkspaceState,
    NodeModEntry,
    NodeModList,
    NodeModMutationAction,
    NodeModMutationResult,
    NodeModSummary,
    NodeModUploadBatchResult,
    NodeRestartRecord,
    NodeRestartScheduleState,
    NodeRestartState,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveRootEntry,
    NodeSettingChoice,
    NodeSettingEntry,
    NodeSettingList,
    NodeStateStreamEvent,
    NodeSystemAction,
    NodeSystemCapabilities,
    NodeSystemDiskSummary,
    NodeSystemHistory,
    NodeSystemSample,
    NodeSystemSummary,
)
from node_auth import NodeApiScope, verify_node_token
from relay_notices import (
    AppLifecycleNotice,
    AppLifecycleState,
    GameDeathKind,
    GameDeathNotice,
    GameProgressKind,
    GameProgressNotice,
    MaintenanceNotice,
    MaintenanceStage,
    PlayerSessionAction,
    PlayerSessionNotice,
    RelayNoticeSource,
)
from restart_state import RestartKind
from web_dash.app_page import (
    _MinecraftRecipeDragPayload,
    _MinecraftRecipeEditorIngredientKind,
    _MinecraftRecipeEditorIngredientState,
    _MinecraftRecipeEditorOperation,
    _MinecraftRecipeEditorSelection,
    _MinecraftRecipeEditorState,
)
from web_dash.assets import AssetContentEncoding, CacheableTextAsset, extract_html_tag_contents
from web_dash.backend import ModWebDashboardBackend
from web_dash.constants import (
    _APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
    _PORTAL_HEALTH_PATH,
    _REMOTE_NODE_OVERVIEW_REQUEST_TIMEOUT_SECONDS,
)
from web_dash.home import (
    _format_restart_hours_input,
    _format_restart_state_line,
    _format_restart_timestamp,
    _ModWebNodeDiskChoice,
    _parse_restart_hours_input,
    _restart_anchor_timestamp,
    _restart_interval_from_parts,
    _restart_interval_parts,
    _RestartWeekday,
)
from web_dash.links import current_node_app_url, mod_web_node_system_path
from web_dash.nicegui_protocols import ModWebUi
from web_dash.routes import _ModWebGZipMiddleware
from web_dash.service import ModWebService
from web_dash.stream_broker import SharedAsyncStreamBroker
from web_dash.types import (
    ModDownloadKind,
    ModWebAppLink,
    ModWebAppSectionKind,
    ModWebAppTabContext,
    ModWebAppTabDefinition,
    ModWebAppTabLoadResult,
    ModWebBasePageModel,
    ModWebConfigEditorShape,
    ModWebDirectUploadTarget,
    ModWebFileSortOrder,
    ModWebHomeNodeSummary,
    ModWebMinecraftItemRegistrySummary,
    ModWebMinecraftRecipeBookSummary,
    ModWebModlistFormat,
    ModWebModSortOrder,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebNotificationTrayItemKind,
    ModWebNotificationTrayItemState,
    ModWebOverviewPageModel,
    ModWebPageLoadWarning,
    ModWebPageModel,
    ModWebSearchOption,
    ModWebSettingControlKind,
    ModWebSevenDaysSandboxOptionEntry,
    ModWebSevenDaysSandboxOptionsSummary,
    ModWebTitleStat,
    RemoteNodeCircuitOpenError,
    _ModWebAppCardBadgeSpec,
    _ModWebBadgeSpec,
    _ModWebChatComposeRequest,
    _ModWebChatPanelConfig,
    _ModWebChatPanelSignal,
    _ModWebChatSurfaceConfig,
    _ModWebFakeChatMessageMode,
    _ModWebFakeChatPreviewState,
    _ModWebLinkSpec,
    _ModWebLoginAdministrator,
    _ModWebNodePresenceBadgeSpec,
    _ModWebNotificationPreviewSpec,
    _ModWebNotificationTrayItem,
    _ModWebTabActionSpec,
)

if TYPE_CHECKING:
    from _manager import App_Manager


def _manager_stub(**attrs: object) -> "App_Manager":
    return cast("App_Manager", cast(object, SimpleNamespace(**attrs)))


def _request_info(url: str) -> RequestInfo:
    parsed_url = URL(url)
    return RequestInfo(
        url=parsed_url,
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict[str]()),
        real_url=parsed_url,
    )


@dataclass(frozen=True, slots=True)
class _FakeRole:
    color: int
    position: int


class _FakeRoleMember:
    def __init__(self, role_color: int, *, roles: tuple[_FakeRole, ...] | None = None) -> None:
        self._roles: tuple[_FakeRole, ...] | tuple[_FakeRole] = roles or (_FakeRole(color=role_color, position=1),)
        self._top_role: _FakeRole = max(self._roles, key=lambda role: role.position)

    def get_top_role(self) -> _FakeRole:
        return self._top_role

    def get_roles(self) -> tuple[_FakeRole, ...]:
        return self._roles


class _FakeRoleCache:
    def __init__(
        self,
        member: _FakeRoleMember | None,
        *,
        members_by_user_id: dict[int, _FakeRoleMember] | None = None,
    ) -> None:
        self._member: _FakeRoleMember | None = member
        self._members_by_user_id: dict[int, _FakeRoleMember] = members_by_user_id or {}

    def get_member(self, guild_id: int, user_id: int) -> _FakeRoleMember | None:
        del guild_id
        return self._members_by_user_id.get(user_id, self._member)


class _FakeRoleBot:
    def __init__(
        self,
        member: _FakeRoleMember | None,
        *,
        user_id: int = 123456789,
        members_by_user_id: dict[int, _FakeRoleMember] | None = None,
    ) -> None:
        self.cache: _FakeRoleCache = _FakeRoleCache(member, members_by_user_id=members_by_user_id)
        self._me: SimpleNamespace = SimpleNamespace(id=user_id)

    def get_me(self) -> SimpleNamespace:
        return self._me


class _FakeQueryParams:
    def __init__(self, values_by_key: dict[str, tuple[str, ...]]) -> None:
        self._values_by_key = values_by_key

    def get(self, key: str, default: str | None = None) -> str | None:
        values = self._values_by_key.get(key)
        if not values:
            return default
        return values[0]

    def getlist(self, key: str) -> list[str]:
        return list(self._values_by_key.get(key, ()))


class _FakeCleanupClient:
    def __init__(self, *, deleted: bool = False) -> None:
        self.delete_handlers: list[Callable[..., object]] = []
        self._deleted = deleted

    def on_delete(self, handler: Callable[..., object]) -> None:
        self.delete_handlers.append(handler)


class _FakeCleanupOwner:
    def __init__(self) -> None:
        self.delete_call_count = 0

    def _handle_delete(self) -> None:
        self.delete_call_count += 1


@dataclass(frozen=True, slots=True)
class _FakeCleanupSlot:
    parent: _FakeCleanupOwner


class _FakeCleanupTimer:
    def __init__(self, parent_slot: _FakeCleanupSlot) -> None:
        self.parent_slot = parent_slot
        self._deleted = False
        self.cancel_calls: list[bool] = []
        self.raise_deleted_parent_slot = False

    def cancel(self, *, with_current_invocation: bool = False) -> None:
        self.cancel_calls.append(with_current_invocation)

    def _get_context(self) -> AbstractContextManager[None]:
        if self.raise_deleted_parent_slot:
            raise RuntimeError("The parent slot of the element has been deleted.")
        return nullcontext()


class _FakeTabbedSectionContainer:
    class_value: str | None = None
    added_style: str | None = None
    removed_style: str | None = None

    def classes(self, value: str) -> "_FakeTabbedSectionContainer":
        self.class_value = value
        return self

    def style(self, *, add: str | None = None, remove: str | None = None) -> "_FakeTabbedSectionContainer":
        self.added_style = add
        self.removed_style = remove
        return self

    def clear(self) -> None:
        return None

    def __enter__(self) -> "_FakeTabbedSectionContainer":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        del exc_type, exc, traceback
        return False


class _FakeTabbedSectionUi:
    def __init__(self, *, client: object | None = None) -> None:
        self.context = SimpleNamespace(client=client) if client is not None else None
        self.tab_change_handler: Callable[[object], object] | None = None
        self.navigate = SimpleNamespace(to=Mock())
        self.javascript_calls: list[str] = []

    def column(self) -> _FakeTabbedSectionContainer:
        return _FakeTabbedSectionContainer()

    def row(self) -> _FakeTabbedSectionContainer:
        return _FakeTabbedSectionContainer()

    def element(self, tag: str) -> _FakeTabbedSectionContainer:
        del tag
        return _FakeTabbedSectionContainer()

    def label(self, text: str) -> _FakeTabbedSectionContainer:
        del text
        return _FakeTabbedSectionContainer()

    def tabs(self, *, value: str, on_change: object) -> _FakeTabbedSectionContainer:
        del value
        self.tab_change_handler = cast(Callable[[object], object], on_change)
        return _FakeTabbedSectionContainer()

    def tab(self, tab_id: str, *, label: str, icon: str | None = None) -> object:
        del tab_id, label, icon
        return object()

    def tab_panels(self, tabs: object, *, value: str, animated: bool) -> _FakeTabbedSectionContainer:
        del tabs, value, animated
        return _FakeTabbedSectionContainer()

    def tab_panel(self, tab: object) -> _FakeTabbedSectionContainer:
        del tab
        return _FakeTabbedSectionContainer()

    def run_javascript(self, script: str, *, timeout: float = 1.0) -> None:
        del timeout
        self.javascript_calls.append(script)


class ModWebTests(unittest.TestCase):
    def test_portal_recovery_script_targets_health_endpoint(self) -> None:
        script = ModWebService._portal_recovery_head_html()

        self.assertIn(_PORTAL_HEALTH_PATH, script)
        self.assertIn("window.modWebPortalRecovery", script)
        self.assertIn("visibilitychange", script)
        self.assertIn("window.location.reload()", script)

    def test_guarded_reload_uses_browser_recovery_helper(self) -> None:
        class FakeNavigate:
            def __init__(self) -> None:
                self.reload_calls = 0

            def reload(self) -> None:
                self.reload_calls += 1

        class FakeUi:
            def __init__(self) -> None:
                self.navigate = FakeNavigate()
                self.javascript_calls: list[tuple[str, float]] = []

            def run_javascript(self, code: str, *, timeout: float = 1.0) -> object:
                self.javascript_calls.append((code, timeout))
                return None

        ui = FakeUi()

        ModWebService._guarded_reload(ui=cast(ModWebUi, ui), reason="Refreshing after save")

        self.assertEqual(ui.navigate.reload_calls, 0)
        self.assertEqual(len(ui.javascript_calls), 1)
        code, timeout = ui.javascript_calls[0]
        self.assertEqual(timeout, 0.1)
        self.assertIn("modWebPortalRecovery.reload", code)
        self.assertIn("Refreshing after save", code)

    def test_guarded_reload_falls_back_to_nicegui_reload(self) -> None:
        class FakeNavigate:
            def __init__(self) -> None:
                self.reload_calls = 0

            def reload(self) -> None:
                self.reload_calls += 1

        class FakeUi:
            def __init__(self) -> None:
                self.navigate = FakeNavigate()

            def run_javascript(self, code: str, *, timeout: float = 1.0) -> object:
                del code, timeout
                raise RuntimeError("client disconnected")

        ui = FakeUi()

        ModWebService._guarded_reload(ui=cast(ModWebUi, ui))

        self.assertEqual(ui.navigate.reload_calls, 1)

    def test_loading_button_wraps_action_and_clears_after_success(self) -> None:
        button = Mock()

        async def action() -> None:
            button.props.assert_called_once_with("loading")

        asyncio.run(
            ModWebService._run_with_loading_button(
                button=cast(Any, button),
                action=action,
            )
        )

        button.props.assert_has_calls([call("loading"), call(remove="loading")])

    def test_loading_button_clears_after_failure(self) -> None:
        button = Mock()

        async def action() -> None:
            raise RuntimeError("fetch failed")

        with self.assertRaisesRegex(RuntimeError, "fetch failed"):
            asyncio.run(
                ModWebService._run_with_loading_button(
                    button=cast(Any, button),
                    action=action,
                )
            )

        button.props.assert_has_calls([call("loading"), call(remove="loading")])

    def test_metadata_detection_automatically_accepts_one_exact_mod_page(self) -> None:
        page = ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example")
        candidate = ModPageCandidate(
            provider=Provider.MODRINTH,
            page=page,
            project_id="example",
            title="Example",
            confidence=ModPageMatchConfidence.EXACT,
            match_reasons=(ModPageMatchReason.FILE_HASH,),
        )
        possible_candidate = candidate.model_copy(
            update={
                "page": ModPageLink(
                    name="Modrinth",
                    url="https://modrinth.com/mod/similar-example",
                ),
                "project_id": "similar-example",
                "confidence": ModPageMatchConfidence.POSSIBLE,
                "match_reasons": (ModPageMatchReason.NAME,),
            }
        )
        discovery = ModPageDiscovery(
            providers=(
                ModPageProviderCandidates(
                    provider=Provider.MODRINTH,
                    candidates=(candidate, possible_candidate),
                ),
            )
        )

        self.assertEqual(ModWebService._automatic_mod_pages(discovery), (page,))

    def test_metadata_detection_requires_confirmation_for_possible_mod_page(self) -> None:
        candidate = ModPageCandidate(
            provider=Provider.MODRINTH,
            page=ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
            project_id="example",
            title="Example",
            confidence=ModPageMatchConfidence.POSSIBLE,
            match_reasons=(ModPageMatchReason.NAME,),
        )
        discovery = ModPageDiscovery(
            providers=(
                ModPageProviderCandidates(
                    provider=Provider.MODRINTH,
                    candidates=(candidate,),
                ),
            )
        )

        self.assertIsNone(ModWebService._automatic_mod_pages(discovery))

    def test_metadata_detection_automatically_accepts_one_launcher_file(self) -> None:
        candidate = LauncherMetadataCandidate(
            provider=Provider.MODRINTH,
            project_page_url="https://modrinth.com/mod/example",
            file_page_url="https://modrinth.com/mod/example/version/1.0.0",
            version="1.0.0",
            filename="example.jar",
            match_reasons=(LauncherMetadataMatchReason.SHA1,),
        )
        discovery = LauncherMetadataDiscovery(
            providers=(
                LauncherMetadataProviderCandidates(
                    provider=Provider.MODRINTH,
                    project_page_url=candidate.project_page_url,
                    candidates=(candidate,),
                ),
            )
        )

        self.assertEqual(
            ModWebService._automatic_launcher_urls(discovery),
            {Provider.MODRINTH: candidate.file_page_url},
        )

    def test_metadata_detection_accepts_explicit_modrinth_file_when_curseforge_fails(self) -> None:
        candidate = LauncherMetadataCandidate(
            provider=Provider.MODRINTH,
            project_page_url="https://modrinth.com/mod/mouse-tweaks",
            file_page_url="https://modrinth.com/mod/mouse-tweaks/version/7JVXOe3K",
            version="1.20.1-2.25.1-forge",
            filename="MouseTweaks-forge-mc1.20.1-2.25.1.jar",
            match_reasons=(
                LauncherMetadataMatchReason.EXPLICIT_FILE_PAGE,
                LauncherMetadataMatchReason.FILENAME,
            ),
        )
        discovery = LauncherMetadataDiscovery(
            providers=(
                LauncherMetadataProviderCandidates(
                    provider=Provider.MODRINTH,
                    project_page_url=candidate.project_page_url,
                    candidates=(candidate,),
                ),
                LauncherMetadataProviderCandidates(
                    provider=Provider.CURSEFORGE,
                    project_page_url=(
                        "https://www.curseforge.com/minecraft/mc-mods/mouse-tweaks"
                    ),
                    error="CurseForge unavailable",
                ),
            )
        )

        self.assertEqual(
            ModWebService._automatic_launcher_urls(discovery),
            {Provider.MODRINTH: candidate.file_page_url},
        )

    def test_metadata_detection_requires_confirmation_for_multiple_launcher_files(self) -> None:
        first_candidate = LauncherMetadataCandidate(
            provider=Provider.MODRINTH,
            project_page_url="https://modrinth.com/mod/example",
            file_page_url="https://modrinth.com/mod/example/version/1.0.0",
            version="1.0.0",
            filename="example.jar",
            match_reasons=(LauncherMetadataMatchReason.FILENAME,),
        )
        second_candidate = first_candidate.model_copy(
            update={
                "file_page_url": "https://modrinth.com/mod/example/version/1.0.1",
                "version": "1.0.1",
            }
        )
        discovery = LauncherMetadataDiscovery(
            providers=(
                LauncherMetadataProviderCandidates(
                    provider=Provider.MODRINTH,
                    project_page_url=first_candidate.project_page_url,
                    candidates=(first_candidate, second_candidate),
                ),
            )
        )

        self.assertIsNone(ModWebService._automatic_launcher_urls(discovery))

    def test_metadata_detection_requires_confirmation_for_filename_only_match(self) -> None:
        candidate = LauncherMetadataCandidate(
            provider=Provider.MODRINTH,
            project_page_url="https://modrinth.com/mod/example",
            file_page_url="https://modrinth.com/mod/example/version/1.0.0",
            version="1.0.0",
            filename="example.jar",
            match_reasons=(LauncherMetadataMatchReason.FILENAME,),
        )
        discovery = LauncherMetadataDiscovery(
            providers=(
                LauncherMetadataProviderCandidates(
                    provider=Provider.MODRINTH,
                    project_page_url=candidate.project_page_url,
                    candidates=(candidate,),
                ),
            )
        )

        self.assertIsNone(ModWebService._automatic_launcher_urls(discovery))

    def test_metadata_detection_still_discovers_modrinth_when_curseforge_is_present(self) -> None:
        launcher_urls = LauncherProviderUrls(
            curseforge=(
                "https://www.curseforge.com/minecraft/mc-mods/"
                "yungs-better-dungeons/files/12345"
            )
        )
        mod_pages = (
            ModPageLink(
                name="CurseForge",
                url=(
                    "https://www.curseforge.com/minecraft/mc-mods/"
                    "yungs-better-dungeons"
                ),
            ),
        )

        missing_providers = ModWebService._launcher_providers_missing_mod_pages(
            providers=(Provider.MODRINTH, Provider.CURSEFORGE),
            launcher_urls=launcher_urls,
            mod_pages=mod_pages,
        )

        self.assertEqual(missing_providers, frozenset({Provider.MODRINTH}))

    def test_optional_client_pack_policy_defaults_to_selected_on_transition(self) -> None:
        self.assertTrue(
            ModWebService._client_pack_default_selected_after_policy_change(
                previous_policy=ClientPackPolicy.REQUIRED,
                selected_policy=ClientPackPolicy.OPTIONAL,
                current_value=False,
            )
        )

    def test_optional_client_pack_policy_preserves_saved_selection(self) -> None:
        self.assertFalse(
            ModWebService._client_pack_default_selected_after_policy_change(
                previous_policy=ClientPackPolicy.OPTIONAL,
                selected_policy=ClientPackPolicy.OPTIONAL,
                current_value=False,
            )
        )

    def test_cacheable_text_asset_selects_best_supported_encoding(self) -> None:
        asset = CacheableTextAsset.build(text="repeated content " * 20, media_type="text/plain")

        brotli_body = asset.select_content("gzip;q=0.4, br;q=0.9")
        gzip_body = asset.select_content("br;q=0, gzip")
        plain_body = asset.select_content("identity")

        self.assertEqual(brotli_body.encoding, AssetContentEncoding.BROTLI)
        self.assertEqual(gzip_body.encoding, AssetContentEncoding.GZIP)
        self.assertIsNone(plain_body.encoding)
        self.assertEqual(plain_body.content, asset.content)

    def test_extract_html_tag_contents_combines_matching_blocks(self) -> None:
        html = "<style>.first { color: red; }</style><style>.second { color: blue; }</style>"

        extracted = extract_html_tag_contents(html, tag_name="style")

        self.assertEqual(extracted, ".first { color: red; }\n.second { color: blue; }")

    def test_shared_stream_broker_reuses_listener_and_replays_latest_event(self) -> None:
        async def exercise() -> None:
            broker = SharedAsyncStreamBroker[str, int](reconnect_delay_seconds=0)
            listener_started = asyncio.Event()
            listener_cancelled = asyncio.Event()
            listener_calls = 0
            first_events: list[int] = []
            second_events: list[int] = []

            async def listener(publish: Callable[[int], None]) -> None:
                nonlocal listener_calls
                listener_calls += 1
                publish(7)
                listener_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    listener_cancelled.set()

            unsubscribe_first = broker.subscribe(
                key="node",
                callback=first_events.append,
                listener_factory=listener,
            )
            await asyncio.wait_for(listener_started.wait(), timeout=0.2)
            unsubscribe_second = broker.subscribe(
                key="node",
                callback=second_events.append,
                listener_factory=listener,
                replay_latest=True,
            )

            self.assertEqual(listener_calls, 1)
            self.assertEqual(first_events, [7])
            self.assertEqual(second_events, [7])
            self.assertEqual(broker.subscriber_count("node"), 2)

            unsubscribe_first()
            self.assertEqual(broker.subscriber_count("node"), 1)
            unsubscribe_second()
            await asyncio.wait_for(listener_cancelled.wait(), timeout=0.2)
            self.assertEqual(broker.subscriber_count("node"), 0)
            await broker.close()

        asyncio.run(exercise())

    @staticmethod
    def _config_entry(
        *,
        root_id: str,
        root_label: str,
        relative_path: str,
        kind: str = "game",
        read_power_level: Power_Level = Power_Level.user,
        size_bytes: int = 123,
        modified_at: str = "2026-05-27 12:00:00",
    ) -> NodeConfigEntry:
        return NodeConfigEntry(
            id=f"{root_id}/{relative_path}",
            label=Path(relative_path).name,
            relative_path=relative_path,
            root_id=root_id,
            root_label=root_label,
            kind=kind,
            read_power_level=read_power_level,
            size_bytes=size_bytes,
            size_text=f"{size_bytes}B",
            modified_at=modified_at,
        )

    @staticmethod
    def _setting_entry(
        *,
        key: str,
        label: str,
        type_name: str,
        permission_level: str = "Admin",
        permission_level_name: str | None = None,
        default_text: str = "",
        description: str | None = None,
        paragraph: bool = False,
        is_sensitive: bool = False,
        value_text: str = "",
        revealed_value_text: str = "",
        current_input_value: str = "",
        has_pending_value: bool = False,
        can_edit: bool = True,
        value_is_hidden: bool = False,
        can_reveal_hidden_text: bool = False,
        allows_text_input: bool = True,
        allows_blank_input: bool = False,
        strict_choice: bool = False,
        choices: tuple[NodeSettingChoice, ...] = (),
        recent_inputs: tuple[str, ...] = (),
    ) -> NodeSettingEntry:
        resolved_permission_level_name: str = permission_level_name or permission_level.casefold()
        return NodeSettingEntry(
            key=key,
            label=label,
            type_name=type_name,
            permission_level=permission_level,
            permission_level_name=resolved_permission_level_name,
            default_text=default_text,
            description=description,
            paragraph=paragraph,
            is_sensitive=is_sensitive,
            value_text=value_text,
            revealed_value_text=revealed_value_text,
            current_input_value=current_input_value,
            has_pending_value=has_pending_value,
            can_edit=can_edit,
            value_is_hidden=value_is_hidden,
            can_reveal_hidden_text=can_reveal_hidden_text,
            allows_text_input=allows_text_input,
            allows_blank_input=allows_blank_input,
            strict_choice=strict_choice,
            choices=choices,
            recent_inputs=recent_inputs,
        )

    @staticmethod
    def _mod_list(
        *,
        app_name: str = "minecraft_alpha",
        mods: tuple[NodeModEntry, ...] = (),
    ) -> NodeModList:
        return NodeModList(
            app_name=app_name,
            app_friendly="Minecraft Alpha",
            node="yuki",
            summary=NodeModSummary(
                total_count=len(mods),
                enabled_count=sum(1 for mod in mods if mod.enabled),
                disabled_count=sum(1 for mod in mods if not mod.enabled),
                coremod_count=sum(1 for mod in mods if mod.coremod),
                downloadable_count=sum(1 for mod in mods if mod.downloadable),
                non_downloadable_count=sum(1 for mod in mods if not mod.downloadable),
            ),
            mods=mods,
            app_stats=None,
        )

    def test_notification_tray_item_validates_progress_range(self) -> None:
        item = _ModWebNotificationTrayItem(
            kind=ModWebNotificationTrayItemKind.UPLOAD,
            state=ModWebNotificationTrayItemState.ACTIVE,
            label=" Uploading ",
            detail_text=" Waiting ",
            progress_percent=42.5,
            node_color_hex=" #ff0000 ",
            app_color_hex=" #00ffff ",
            blink=True,
        )

        self.assertEqual(item.label, "Uploading")
        self.assertEqual(item.detail_text, "Waiting")
        self.assertEqual(item.progress_percent, 42.5)
        self.assertEqual(item.node_color_hex, "#ff0000")
        self.assertEqual(item.app_color_hex, "#00ffff")
        self.assertTrue(item.blink)

        with self.assertRaises(ValueError):
            _ModWebNotificationTrayItem(
                kind=ModWebNotificationTrayItemKind.DOWNLOAD,
                state=ModWebNotificationTrayItemState.ERROR,
                label="Broken",
                progress_percent=101.0,
            )

    def test_dashboard_backend_limits_active_transfers_per_user(self) -> None:
        backend = ModWebDashboardBackend()

        transfer_ids = backend.start_upload_transfers(
            user_id=42,
            filenames=("alpha.jar", "beta.jar", "gamma.jar"),
            detail_text="Uploading mods.",
            node_color_hex="#ff0000",
            app_color_hex="#00ffff",
        )

        self.assertEqual(len(transfer_ids), 3)
        self.assertEqual(backend.user_active_transfer_slots(user_id=42), 3)
        self.assertEqual(len(backend.user_transfer_items(user_id=42)), 3)
        self.assertEqual(backend.user_transfer_items(user_id=42)[0].node_color_hex, "#ff0000")
        self.assertEqual(backend.user_transfer_items(user_id=42)[0].app_color_hex, "#00ffff")

        with self.assertRaises(RuntimeError):
            backend.start_download_transfers(
                user_id=42,
                filenames=("delta.jar",),
                detail_text="Preparing download.",
            )

        backend.complete_transfer(transfer_id=transfer_ids[0], detail_text="Uploaded.")

        resumed_transfer_ids = backend.start_download_transfers(
            user_id=42,
            filenames=("delta.jar",),
            detail_text="Preparing download.",
        )

        self.assertEqual(len(resumed_transfer_ids), 1)

    def test_dashboard_backend_notifies_transfer_subscribers_on_transfer_changes(self) -> None:
        backend = ModWebDashboardBackend()
        notifications: list[str] = []

        def _record_notification() -> None:
            notifications.append("changed")

        unsubscribe = backend.subscribe_user_transfers(user_id=42, subscriber=_record_notification)

        transfer_id = backend.start_upload_transfers(
            user_id=42,
            filenames=("alpha.jar",),
            detail_text="Uploading mods.",
        )[0]
        backend.update_transfer_progress(
            transfer_id=transfer_id,
            progress_percent=55.0,
            detail_text="Receiving mod payload.",
        )
        backend.complete_transfer(transfer_id=transfer_id, detail_text="Installed.")
        backend.clear_user_transfers(user_id=42)

        self.assertEqual(len(notifications), 4)

        unsubscribe()
        backend.start_upload_transfers(
            user_id=42,
            filenames=("beta.jar",),
            detail_text="Uploading mods.",
        )
        self.assertEqual(len(notifications), 4)

    def test_home_navigation_warns_while_upload_is_active(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        navigated_to: list[str] = []
        notifications: list[tuple[str, str | None]] = []
        ui = cast(
            ModWebUi,
            cast(
                object,
                SimpleNamespace(
                    navigate=SimpleNamespace(to=navigated_to.append),
                    notify=lambda message, type=None: notifications.append((message, type)),
                ),
            ),
        )
        transfer_id = service._backend.start_upload_transfers(
            user_id=user.discord_id,
            filenames=("Mod upload",),
            detail_text="Sending mods directly.",
        )[0]

        self.assertFalse(service._navigate_home(ui=ui, user=user))
        self.assertEqual(navigated_to, [])
        self.assertEqual(notifications[0][1], "warning")

        service._backend.fail_transfer(transfer_id=transfer_id, detail_text="Interrupted.")

        self.assertTrue(service._navigate_home(ui=ui, user=user))
        self.assertEqual(navigated_to, [service.index_path()])

    def test_persist_uploaded_file_for_transfer_updates_backend_progress(self) -> None:
        service = ModWebService()
        transfer_id = service._backend.start_upload_transfers(
            user_id=42,
            filenames=("alpha.jar",),
            detail_text="Staging mods.",
        )[0]

        class _FakeUploadFile:
            name = "alpha.jar"
            content_type = "application/java-archive"

            def __init__(self, content: bytes) -> None:
                self._content = content

            async def read(self) -> bytes:
                return self._content

            async def text(self, encoding: str = "utf-8") -> str:
                return self._content.decode(encoding)

            def iterate(self, *, chunk_size: int = 1024 * 1024):
                async def _iterate():
                    for offset in range(0, len(self._content), chunk_size):
                        yield self._content[offset : offset + chunk_size]

                return _iterate()

            async def save(self, path: str | Path) -> None:
                Path(path).write_bytes(self._content)

            def size(self) -> int:
                return len(self._content)

        async def _run_upload() -> Path:
            return await service._persist_uploaded_file_for_transfer(
                upload_file=_FakeUploadFile(b"mod-payload" * 32),
                transfer_id=transfer_id,
                active_detail_text="Receiving mods for Minecraft Alpha.",
            )

        temp_path = asyncio.run(_run_upload())
        self.addCleanup(temp_path.unlink, missing_ok=True)

        item = service._backend.user_transfer_items(user_id=42)[0]

        self.assertEqual(temp_path.read_bytes(), b"mod-payload" * 32)
        self.assertEqual(item.detail_text, "Receiving mods for Minecraft Alpha.")
        self.assertEqual(item.progress_percent, 72.0)

    def test_wait_for_upload_transfer_capacity_respects_transfer_limit(self) -> None:
        service = ModWebService()
        transfer_ids = service._backend.start_upload_transfers(
            user_id=42,
            filenames=("alpha.jar", "beta.jar"),
            detail_text="Staging mods.",
        )

        available_slots = asyncio.run(
            service._wait_for_upload_transfer_capacity(
                user_id=42,
                requested_slots=4,
            )
        )

        self.assertEqual(available_slots, 1)

        for transfer_id in transfer_ids:
            service._backend.complete_transfer(transfer_id=transfer_id, detail_text="Installed.")

    def test_upload_mods_batches_files_when_drop_exceeds_transfer_limit(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=0,
                    enabled_count=0,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=0,
                    non_downloadable_count=0,
                ),
                mods=(),
                app_stats=None,
            ),
            supports_chat=False,
            chat_url=None,
            map_url=None,
            can_write_map_annotations=False,
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )
        user = ModWebUser(discord_id=42, username="finch", global_name="Finch", avatar_hash=None)
        service._user_has_level = lambda *_args, **_kwargs: True  # type: ignore[method-assign]

        class _FakeUploadFile:
            content_type = "application/java-archive"

            def __init__(self, name: str) -> None:
                self.name = name

            async def read(self) -> bytes:
                return b""

            async def text(self, encoding: str = "utf-8") -> str:
                del encoding
                return ""

            def iterate(self, *, chunk_size: int = 1024 * 1024):
                del chunk_size

                async def _iterate():
                    if False:
                        yield b""

                return _iterate()

            async def save(self, path: str | Path) -> None:
                Path(path).write_bytes(b"")

            def size(self) -> int:
                return 1

        upload_files = tuple(
            _FakeUploadFile(name)
            for name in ("alpha.jar", "beta.jar", "gamma.jar", "delta.jar")
        )
        observed_batches: list[tuple[str, ...]] = []

        async def _fake_wait_for_upload_transfer_capacity(*, user_id: int, requested_slots: int) -> int:
            del user_id, requested_slots
            return 3 if not observed_batches else 1

        async def _fake_upload_mod_batch(
            *,
            model: ModWebPageModel,
            upload_files: tuple[_FakeUploadFile, ...],
            user: ModWebUser,
        ) -> NodeModUploadBatchResult:
            del user
            batch_names = tuple(upload_file.name for upload_file in upload_files)
            observed_batches.append(batch_names)
            uploaded_mods = tuple(self._mod_entry(name=name) for name in batch_names)
            return NodeModUploadBatchResult(
                app_name=model.app_name,
                app_friendly=model.app_friendly,
                node=model.node_name,
                message=f"Uploaded {len(uploaded_mods)} mods for {model.app_friendly}.",
                mods=uploaded_mods,
            )

        service._wait_for_upload_transfer_capacity = _fake_wait_for_upload_transfer_capacity  # type: ignore[method-assign]
        service._upload_mod_batch = _fake_upload_mod_batch  # type: ignore[method-assign]

        result = asyncio.run(
            service._upload_mods(
                model=model,
                upload_files=upload_files,
                user=user,
            )
        )

        self.assertEqual(
            observed_batches,
            [
                ("alpha.jar", "beta.jar", "gamma.jar"),
                ("delta.jar",),
            ],
        )
        self.assertEqual(tuple(mod.name for mod in result.mods), tuple(upload_file.name for upload_file in upload_files))
        self.assertEqual(result.message, "Uploaded 4 mods for Minecraft Alpha.")

    @staticmethod
    def _mod_entry(
        *,
        name: str,
        friendly: str | None = None,
        description: str | None = None,
        enabled: bool = True,
        mod_type: ModType = ModType.REGULAR,
        coremod: bool = False,
        downloadable: bool = True,
        download_block_reason: str | None = None,
        download_block_label: str | None = None,
        origin: str = "manual",
        version: str | None = "1.0.0",
        added: str = "2026-06-04T20:00:00",
        size_bytes: int = 128,
        size_text: str = "128B",
        client_pack: ClientPackConfig | None = None,
        placement: ModPlacement | None = None,
    ) -> NodeModEntry:
        resolved_placement = placement or (
            ModPlacement.SERVER_ENABLED if enabled else ModPlacement.SERVER_DISABLED
        )
        resolved_client_pack = client_pack or ClientPackConfig(
            included_in_client=mod_type.included_in_client_by_default
        )
        return NodeModEntry(
            name=name,
            friendly=friendly or name,
            description=description,
            enabled=enabled,
            mod_type=mod_type,
            coremod=coremod,
            downloadable=downloadable,
            download_block_reason=download_block_reason,
            download_block_label=download_block_label,
            origin=origin,
            version=version,
            added=added,
            size_bytes=size_bytes,
            size_text=size_text,
            placement=resolved_placement,
            server_loadable=resolved_placement.server_loadable,
            client_pack_eligible=is_client_pack_candidate(resolved_placement, mod_type.side)
            and resolved_client_pack.included_in_client
            and downloadable,
            archive_name=name,
            source_path=f"/mods/{name}",
            client_pack=resolved_client_pack,
        )

    def _render_mod_info_dialog_labels(self, entry: NodeModEntry) -> list[str]:
        class FakeContainer:
            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def classes(self, value: str) -> "FakeContainer":
                del value
                return self

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

            def clear(self) -> None:
                return None

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text

            def classes(self, value: str) -> "FakeLabel":
                del value
                return self

        class FakeLink:
            def props(self, value: str) -> "FakeLink":
                del value
                return self

            def classes(self, value: str) -> "FakeLink":
                del value
                return self

        class FakeButton:
            def classes(self, value: str) -> "FakeButton":
                del value
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.labels: list[FakeLabel] = []

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def grid(self, *, columns: int) -> FakeContainer:
                del columns
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def link(self, text: str, target: str, *, new_tab: bool) -> FakeLink:
                del text, target, new_tab
                return FakeLink()

            def button(self, text: str, on_click: object | None = None) -> FakeButton:
                del text, on_click
                return FakeButton()

        service = ModWebService()
        ui = FakeUi()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=1,
                    enabled_count=1,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=1,
                    non_downloadable_count=0,
                ),
                mods=(entry,),
                app_stats=None,
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={entry.name: f"/mods/download/{entry.name}"},
            app_scope="minecraft",
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=False),
            patch.object(service, "_available_mod_actions", return_value=()),
        ):
            service._render_mod_info_dialog(
                ui=cast(Any, ui),
                entry=entry,
                model=model,
                user=user,
            )

        return [label.text for label in ui.labels]

    def test_render_mod_info_dialog_shows_description_below_mod_pages_when_available(self) -> None:
        labels = self._render_mod_info_dialog_labels(
            replace(
                self._mod_entry(
                    name="alpha-fabric.jar",
                    friendly="Alpha Fabric",
                    description="Client-side rendering and HUD improvements.",
                ),
                mod_pages=(ModPageLink(name="Modrinth", url="https://modrinth.com/mod/alpha-fabric"),),
                platforms=ModPlatformMetadata(
                    modrinth=ModrinthModMetadata(
                        page_url="https://modrinth.com/mod/alpha-fabric/version/abc123",
                        project_id="project-alpha",
                        version_id="abc123",
                        download_url=("https://cdn.modrinth.com/data/project-alpha/versions/abc123/alpha-fabric.jar"),
                        description="Client-side rendering and HUD improvements.",
                        filename="alpha-fabric.jar",
                        sha1="0123456789abcdef0123456789abcdef01234567",
                        sha512="0" * 128,
                        size=123,
                    )
                ),
            )
        )

        self.assertLess(labels.index("Mod pages"), labels.index("Description"))
        self.assertIn("Client-side rendering and HUD improvements.", labels)

    def test_render_mod_info_dialog_skips_description_section_when_unavailable(self) -> None:
        labels = self._render_mod_info_dialog_labels(
            replace(
                self._mod_entry(
                    name="beta-forge.jar",
                    friendly="Beta Forge",
                ),
                mod_pages=(ModPageLink(name="Modrinth", url="https://modrinth.com/mod/beta-forge"),),
            )
        )

        self.assertNotIn("Description", labels)

    @staticmethod
    def _save_list(*, app_name: str = "minecraft_alpha") -> NodeSaveList:
        return NodeSaveList(
            app_name=app_name,
            app_friendly="Minecraft Alpha",
            node="yuki",
            roots=(),
            saves=(),
        )

    @staticmethod
    def _blueprint_list(*, app_name: str = "minecraft_alpha") -> NodeBlueprintList:
        return NodeBlueprintList(
            app_name=app_name,
            app_friendly="Minecraft Alpha",
            node="yuki",
            default_session_name="Session Alpha",
            blueprints=(
                NodeBlueprintEntry(
                    id="Session Alpha/Assembler.sbp",
                    label="Assembler.sbp",
                    session_name="Session Alpha",
                    relative_path="Session Alpha/Assembler.sbp",
                    size_bytes=128,
                    size_text="128B",
                    modified_at="2026-06-04 20:00:00",
                    uploaded_by_display_name="User 42",
                    can_delete=True,
                ),
            ),
        )

    @staticmethod
    def _setting_list(*, app_name: str = "minecraft_alpha") -> NodeSettingList:
        return NodeSettingList(
            app_name=app_name,
            app_friendly="Minecraft Alpha",
            node="yuki",
            editable_count=0,
            restricted_count=0,
            has_pending_changes=False,
            pending_change_count=0,
            required_save_level_name=Power_Level.user.name,
            required_reload_level_name=Power_Level.user.name,
            settings=(),
        )

    @staticmethod
    def _console_action_list(*, app_name: str = "minecraft_alpha") -> NodeConsoleActionList:
        return NodeConsoleActionList(
            app_name=app_name,
            app_friendly="Minecraft Alpha",
            node="yuki",
            actions=(),
        )

    @staticmethod
    def _overview_model_with_config_and_chat() -> ModWebOverviewPageModel:
        return ModWebOverviewPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            supports_chat=True,
            chat_url="/mod-web/chat/minecraft_alpha",
        )

    @staticmethod
    def _chat_surface_with_map() -> _ModWebChatSurfaceConfig:
        initial_snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=1,
            events=(),
            endpoint_summaries=(NodeChatEndpointSummary(label="Game: Minecraft Alpha"),),
        )
        return _ModWebChatSurfaceConfig(
            panel=_ModWebChatPanelConfig(
                initial_snapshot=initial_snapshot,
                refresh_snapshot=AsyncMock(return_value=initial_snapshot),
                send_message=None,
            ),
            node_name="yuki",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            app_stats=None,
            popout_url="/mod-web/chat/minecraft_alpha",
            map_url="https://example.invalid/squaremap/?world=minecraft_overworld",
        )

    def test_build_home_node_stat_specs_groups_metrics_per_node(self) -> None:
        node_stats = ModWebService()._build_home_node_stat_specs(
            (
                ModWebHomeNodeSummary(
                    node=ModWebNodeLink(
                        node_name="yuki",
                        label="Yuki",
                        url="/",
                        api_base_url="/api/node",
                        api_url="/api/node/apps",
                        is_current=True,
                    ),
                    app_count=3,
                    system_summary=NodeSystemSummary(
                        cpu_percent=22,
                        ram_percent=44,
                        ram_used_bytes=8 * 1024**3,
                        ram_total_bytes=16 * 1024**3,
                        storage_percent=55,
                        storage_free_bytes=100 * 1024**3,
                        storage_total_bytes=200 * 1024**3,
                        bot_uptime_seconds=2 * 60 * 60,
                        uptime_seconds=24 * 60 * 60,
                        running_names=("Factorio Lab", "Minecraft Alpha"),
                    ),
                ),
                ModWebHomeNodeSummary(
                    node=ModWebNodeLink(
                        node_name="erin",
                        label="Erin",
                        url="/mod-web/nodes/erin",
                        api_base_url="https://erin.example/api/node",
                        api_url="/api/node-proxy/erin/apps",
                        is_current=False,
                    ),
                    app_count=2,
                    system_summary=NodeSystemSummary(
                        cpu_percent=91,
                        ram_percent=19,
                        ram_used_bytes=719 * 1024**2,
                        ram_total_bytes=3700 * 1024**2,
                        storage_percent=99,
                        storage_free_bytes=1 * 1024**3,
                        storage_total_bytes=786 * 1024**3 // 10,
                        bot_uptime_seconds=30,
                        running_names=(),
                    ),
                ),
            )
        )

        self.assertEqual([stat.node_label for stat in node_stats], ["Yuki", "Erin"])
        self.assertEqual(node_stats[0].status_text, "2 Running")
        self.assertEqual(node_stats[0].status_tone, "purple")
        self.assertEqual(node_stats[0].card_tone, "purple")
        self.assertEqual(
            [(metric.label, metric.icon, metric.value, metric.tone) for metric in node_stats[0].metrics],
            [
                ("CPU", "speed", "22%", "grey"),
                ("RAM", "memory", "44%", "purple"),
                ("Disk", "storage", "55%", "purple"),
                ("Bot Uptime", "smart_toy", "2h 0m", "black"),
            ],
        )
        self.assertIsNone(node_stats[0].node_subtitle)
        self.assertEqual(node_stats[0].running_text, "Factorio Lab, Minecraft Alpha")
        self.assertIsNone(node_stats[0].running_tooltip)
        self.assertIsNone(node_stats[1].status_text)
        self.assertEqual(node_stats[1].status_tone, "black")
        self.assertEqual(node_stats[1].card_tone, "black")
        self.assertEqual(node_stats[1].running_text, "Nothin Running")
        self.assertEqual(node_stats[1].running_tone, "grey")
        self.assertEqual(node_stats[1].metrics[2].value, "99%")
        self.assertEqual(node_stats[1].metrics[2].tone, "red")
        self.assertEqual(node_stats[1].metrics[3].value, "<1m")

    def test_node_display_subtitle_omits_case_only_duplicates(self) -> None:
        subtitle: str | None = ModWebService._node_display_subtitle(label="Erin", node_name="erin")

        self.assertIsNone(subtitle)

    def test_node_display_subtitle_keeps_distinct_node_name(self) -> None:
        subtitle: str | None = ModWebService._node_display_subtitle(label="Production", node_name="erin-prod")

        self.assertEqual(subtitle, "erin-prod")

    def test_home_node_nickname_has_priority_over_node_label(self) -> None:
        service = ModWebService()
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=False,
        )
        snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="900",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(),
        )

        live_member = SimpleNamespace(nickname="Katoku Test")
        live_bot = SimpleNamespace(
            cache=SimpleNamespace(get_member=Mock(return_value=live_member)),
            get_me=Mock(return_value=SimpleNamespace(id=900)),
        )
        with (
            patch.object(service, "_known_bot_snapshot_for_node", return_value=snapshot),
            patch.object(service, "_mod_web_bot", return_value=live_bot),
            patch.object(config, "Name_Cache") as name_cache,
        ):
            display_label = service._home_node_display_label(node)

        self.assertEqual(display_label, "Katoku Test")
        name_cache.assert_not_called()
        self.assertEqual(
            service._home_node_display_subtitle(node=node, display_label=display_label),
            "Yuki",
        )

    def test_app_start_blocked_remote_uses_app_name_identity(self) -> None:
        self.assertTrue(
            ModWebService._app_start_blocked_remote(
                app_name="minecraft_alpha",
                app_stats=None,
                start_blocked_app_ids=("minecraft_alpha",),
            )
        )
        self.assertFalse(
            ModWebService._app_start_blocked_remote(
                app_name="minecraft_alpha",
                app_stats=NodeAppRuntimeSummary(
                    running=True,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=ChatRelaySupport.NONE,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                ),
                start_blocked_app_ids=("minecraft_alpha",),
            )
        )
        self.assertFalse(
            ModWebService._app_start_blocked_remote(
                app_name="minecraft_alpha",
                app_stats=None,
                start_blocked_app_ids=("factorio_lab",),
            )
        )

    def test_node_status_badge_marks_current_node_alive(self) -> None:
        section: ModWebNodeAppSection = ModWebNodeAppSection(
            node=ModWebNodeLink(
                node_name="yuki",
                label="Yuki",
                url="/",
                api_base_url="/api/node",
                api_url="/api/node/apps",
                is_current=True,
            ),
            app_links=(),
        )

        self.assertEqual(ModWebService._node_status_badge_text(section), "Yuki: Alive")
        self.assertEqual(ModWebService._node_status_badge_tone(section), "black")

    def test_node_status_badge_marks_unavailable_node_down(self) -> None:
        section: ModWebNodeAppSection = ModWebNodeAppSection(
            node=ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            ),
            app_links=(),
            error="timeout",
        )

        self.assertEqual(ModWebService._node_status_badge_text(section), "Erin: Down")
        self.assertEqual(ModWebService._node_status_badge_tone(section), "red")

    def test_node_status_badge_marks_simulated_node_down(self) -> None:
        section: ModWebNodeAppSection = ModWebNodeAppSection(
            node=ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            ),
            app_links=(),
            error="This node is being simulated as unavailable in dev mode.",
            is_simulated_down=True,
        )

        self.assertEqual(ModWebService._node_status_badge_text(section), "Erin: Simulated Down")
        self.assertEqual(ModWebService._node_status_badge_tone(section), "warn")

    def test_home_node_latency_badges_javascript_embeds_presence_stream_urls(self) -> None:
        script = ModWebService._home_node_latency_badges_javascript(
            (
                _ModWebNodePresenceBadgeSpec(
                    node_name="yuki",
                    badge_element_id=100,
                    text_element_id=101,
                    node_label="Yuki",
                    pending_text="Yuki: ...",
                    alive_text="Yuki: Alive",
                    down_text="Yuki: Down",
                    presence_stream_url="/api/node/presence/stream",
                    pending_class_name="badge-pending",
                    healthy_class_name="badge-healthy",
                    unhealthy_class_name="badge-down",
                    show_latency=True,
                    tooltip_mode="discord",
                ),
                _ModWebNodePresenceBadgeSpec(
                    node_name="erin",
                    badge_element_id=200,
                    text_element_id=202,
                    node_label="Erin",
                    pending_text="Erin: Down",
                    alive_text="Erin: Alive",
                    down_text="Erin: Down",
                    presence_stream_url=None,
                    pending_class_name="badge-down",
                    healthy_class_name="badge-healthy",
                    unhealthy_class_name="badge-down",
                    show_latency=False,
                    tooltip_mode="portal",
                ),
            )
        )

        self.assertIn('"badge_element_id":100', script)
        self.assertIn('"text_element_id":101', script)
        self.assertIn('"node_label":"Yuki"', script)
        self.assertIn('"pending_text":"Yuki: ..."', script)
        self.assertIn('"alive_text":"Yuki: Alive"', script)
        self.assertIn('"down_text":"Yuki: Down"', script)
        self.assertIn('"presence_stream_url":"/api/node/presence/stream"', script)
        self.assertIn('"presence_stream_url":null', script)
        self.assertIn('"show_latency":true', script)
        self.assertIn('"show_latency":false', script)
        self.assertIn('"tooltip_mode":"discord"', script)
        self.assertIn('"tooltip_mode":"portal"', script)
        self.assertIn("modWebHomeNodeLatency", script)
        self.assertIn("new WebSocket", script)
        self.assertIn("sample_id", script)
        self.assertIn("type: 'ping'", script)
        self.assertIn("latencyRefreshIntervalMs", script)
        self.assertIn("latencyTimeoutMs", script)
        self.assertIn("reconnectDelayMs", script)
        self.assertIn("socket.addEventListener('message'", script)
        self.assertIn("renderBadge(spec, spec.pending_text, spec.pending_class_name);", script)
        self.assertIn("const confirmPresence = async (nodeName) => {", script)
        self.assertIn("if (payload.node !== nodeName) {", script)
        self.assertIn("socket.close();", script)
        self.assertIn("${spec.node_label}: ${latencyTextValue}", script)
        self.assertIn("textElement.textContent = text;", script)
        self.assertNotIn("_mod_web_latency_probe", script)
        self.assertNotIn("fetch(", script)
        self.assertIn("bootstrapProbeCount = 4", script)
        self.assertIn("bootstrapProbeDelayMs = 850", script)
        self.assertIn("summariseLatencyMeasurements", script)
        self.assertIn("connectionsByNode", script)
        self.assertIn("pendingSamples", script)
        self.assertIn("Discord: ${formatTooltipLatency(connection.discordLatencyMs)}", script)
        self.assertIn("Portal → ${target.node_label}", script)
        self.assertIn(r"\nDiscord: ${formatTooltipLatency(connection.discordLatencyMs)}", script)
        self.assertIn(r".join('\n')", script)
        self.assertIn("type: 'node_latencies'", script)
        self.assertIn("payload.type === 'node_latencies'", script)

    def test_custom_node_names_preserve_portal_and_discord_badge_tooltips(self) -> None:
        portal_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="900",
                label="Portal",
                bot_profile=config.BotProfileName.PORTAL,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="helios",
                    public_base_url="https://portal.example",
                    node_api_base_url="https://portal.example/api/node",
                )
            ),
        )
        yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="901",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="wakusei",
                    public_base_url="https://yuki.example",
                    node_api_base_url="https://yuki.example/api/node",
                )
            ),
        )
        service = ModWebService()
        portal_node = ModWebNodeLink(
            node_name="helios",
            label="Portal",
            url="/mod-web/nodes/helios",
            api_base_url="https://portal.example/api/node",
            api_url="/api/node-proxy/helios/apps",
            is_current=False,
        )
        yuki_node = ModWebNodeLink(
            node_name="wakusei",
            label="Yuki",
            url="/mod-web/nodes/wakusei",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/wakusei/apps",
            is_current=False,
        )

        with patch.object(service, "_known_bot_snapshots", return_value=(portal_snapshot, yuki_snapshot)):
            self.assertTrue(service._node_is_portal(portal_node))
            self.assertTrue(service._node_has_discord_bot(yuki_node))
            self.assertEqual(
                service._home_node_badge_tooltip_mode(ModWebNodeAppSection(node=portal_node, app_links=())),
                "portal",
            )
            self.assertEqual(
                service._home_node_badge_tooltip_mode(ModWebNodeAppSection(node=yuki_node, app_links=())),
                "discord",
            )

    def test_node_capability_badges_report_supported_features(self) -> None:
        badges = ModWebService._node_capability_badges(
            app_links=(
                ModWebAppLink(
                    name="minecraft_alpha",
                    friendly="Minecraft Alpha",
                    node_name="yuki",
                    running=True,
                    enabled=True,
                    color_hex=None,
                    supports_mods=True,
                    supports_configs=True,
                    supports_saves=False,
                    supports_settings=False,
                    url="/mod-web/mods/minecraft_alpha",
                    api_url=None,
                    configs_api_url=None,
                    supports_console_actions=True,
                ),
                ModWebAppLink(
                    name="factorio_beta",
                    friendly="Factorio Beta",
                    node_name="yuki",
                    running=False,
                    enabled=True,
                    color_hex=None,
                    supports_mods=False,
                    supports_configs=False,
                    supports_saves=True,
                    supports_settings=False,
                    url="/mod-web/mods/factorio_beta",
                    api_url=None,
                    configs_api_url=None,
                ),
            ),
            unavailable_count=1,
        )

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="1 Modly", tone="purple"),
                _ModWebBadgeSpec(text="1 Savely", tone="black"),
                _ModWebBadgeSpec(text="1 Configy", tone="black"),
                _ModWebBadgeSpec(text="1 Consolely", tone="black"),
                _ModWebBadgeSpec(text="0 Chatty", tone="purple"),
                _ModWebBadgeSpec(text="1 unavailable", tone="red"),
            ),
        )

    def test_interactive_badge_uses_badge_link_for_urls(self) -> None:
        service = ModWebService()
        ui = cast(Any, object())
        badge_element = cast(Any, object())

        with (
            patch.object(ModWebService, "_badge_link", return_value=badge_element) as render_badge_link,
            patch.object(ModWebService, "_attach_text_tooltip") as attach_text_tooltip,
            patch.object(ModWebService, "_badge") as render_badge,
        ):
            returned_badge = service._interactive_badge(
                ui=ui,
                text="Erin: Down",
                tone="red",
                url="/mod-web?dev_node_down=erin",
                tooltip_text="Simulate this node going down.",
                extra_classes="mod-node-status-badge",
            )

        self.assertIs(returned_badge, badge_element)
        render_badge_link.assert_called_once_with(
            ui=ui,
            text="Erin: Down",
            tone="red",
            url="/mod-web?dev_node_down=erin",
            extra_classes="mod-node-status-badge",
            tooltip_text="Simulate this node going down.",
        )
        render_badge.assert_not_called()
        attach_text_tooltip.assert_not_called()

    def test_primary_guild_bot_role_color_hex_uses_cached_top_role_color(self) -> None:
        service: ModWebService = ModWebService()
        service._manager = _manager_stub(bot=_FakeRoleBot(_FakeRoleMember(0x7C3AED)))

        self.assertEqual(service._primary_guild_bot_role_color_hex(), "#7c3aed")

    def test_primary_guild_bot_role_color_hex_skips_uncoloured_top_role(self) -> None:
        service: ModWebService = ModWebService()
        service._manager = _manager_stub(
            bot=_FakeRoleBot(
                _FakeRoleMember(
                    0,
                    roles=(
                        _FakeRole(color=0, position=20),
                        _FakeRole(color=0x22C55E, position=10),
                    ),
                )
            )
        )

        self.assertEqual(service._primary_guild_bot_role_color_hex(), "#22c55e")

    def test_primary_guild_bot_role_color_hex_falls_back_to_app_bot(self) -> None:
        service: ModWebService = ModWebService()
        service._manager = _manager_stub(
            bot=None,
            apps={"factorio": SimpleNamespace(bot=_FakeRoleBot(_FakeRoleMember(0xDC6B0F)))},
        )

        self.assertEqual(service._primary_guild_bot_role_color_hex(), "#dc6b0f")

    def test_node_role_color_hex_uses_represented_remote_node_bot_member(self) -> None:
        service: ModWebService = ModWebService()
        service._manager = _manager_stub(
            bot=_FakeRoleBot(
                None,
                members_by_user_id={1350601198637551659: _FakeRoleMember(0xDC2626)},
            ),
            apps={},
        )
        remote_snapshot: BotMetadataSnapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="1350601198637551659",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="http://erin.example:3180",
                    node_api_base_url="http://erin.example:3180/api/node",
                )
            ),
        )

        with patch.object(ModWebService, "_known_bot_snapshots", return_value=(remote_snapshot,)):
            self.assertEqual(service._node_role_color_hex(node_name="erin"), "#dc2626")

    def test_node_role_color_hex_falls_back_to_snapshot_presentation(self) -> None:
        service: ModWebService = ModWebService()
        service._manager = _manager_stub(bot=None, apps={})
        remote_snapshot: BotMetadataSnapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="1350601198637551659",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="http://erin.example:3180",
                    node_api_base_url="http://erin.example:3180/api/node",
                ),
                presentation=config.BotMetadataPresentation(
                    avatar_uri="https://cdn.example.com/erin.png",
                    accent_color_hex="#dc2626",
                ),
            ),
        )

        with patch.object(ModWebService, "_known_bot_snapshots", return_value=(remote_snapshot,)):
            self.assertEqual(service._node_role_color_hex(node_name="erin"), "#dc2626")

    def test_node_bot_avatar_uri_falls_back_to_snapshot_presentation(self) -> None:
        service: ModWebService = ModWebService()
        service._manager = _manager_stub(bot=None, apps={})
        remote_snapshot: BotMetadataSnapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="1350601198637551659",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="http://erin.example:3180",
                    node_api_base_url="http://erin.example:3180/api/node",
                ),
                presentation=config.BotMetadataPresentation(
                    avatar_uri="https://cdn.example.com/erin.png",
                    accent_color_hex="#dc2626",
                ),
            ),
        )

        with patch.object(ModWebService, "_known_bot_snapshots", return_value=(remote_snapshot,)):
            self.assertEqual(service._node_bot_avatar_uri(node_name="erin"), "https://cdn.example.com/erin.png")

    def test_node_badge_style_colours_badge_surface_and_text(self) -> None:
        style: str = ModWebService._node_badge_style("#dc6b0f")

        self.assertEqual(style, "border-color: #dc6b0f !important;")
        self.assertNotIn("background:", style)

    def test_probe_node_status_requires_ping_success_status(self) -> None:
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        class ResponseContext:
            async def __aenter__(self) -> SimpleNamespace:
                return SimpleNamespace(status=401)

            async def __aexit__(self, *_args: object) -> None:
                return None

        session = Mock()
        session.get.return_value = ResponseContext()
        with patch.object(ModWebService, "_remote_http_client", new=AsyncMock(return_value=session)):
            status = asyncio.run(ModWebService()._probe_node_status_async(node))

        self.assertEqual(status, ModWebNodeStatus(node=node, alive=False, detail="Unexpected HTTP 401"))

    def test_probe_node_status_uses_presence_timeout(self) -> None:
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        class ResponseContext:
            async def __aenter__(self) -> SimpleNamespace:
                return SimpleNamespace(status=200)

            async def __aexit__(self, *_args: object) -> None:
                return None

        session = Mock()
        session.get.return_value = ResponseContext()
        with patch.object(ModWebService, "_remote_http_client", new=AsyncMock(return_value=session)):
            asyncio.run(ModWebService()._probe_node_status_async(node))

        session.get.assert_called_once()
        call_kwargs = session.get.call_args.kwargs
        self.assertEqual(session.get.call_args.args, ("https://erin.example/api/node/ping",))
        self.assertIsNone(call_kwargs["timeout"].total)
        self.assertEqual(call_kwargs["timeout"].connect, 2.0)
        self.assertEqual(call_kwargs["timeout"].sock_read, 4.0)

    def test_probe_node_status_marks_request_failure_down(self) -> None:
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )

        session = Mock()
        session.get.side_effect = aiohttp.ClientConnectionError("timeout")
        with patch.object(ModWebService, "_remote_http_client", new=AsyncMock(return_value=session)):
            status = asyncio.run(ModWebService()._probe_node_status_async(node))

        self.assertFalse(status.alive)
        self.assertEqual(status.node, node)
        self.assertEqual(status.detail, "timeout")

    def test_login_node_statuses_short_circuit_simulated_remote_nodes(self) -> None:
        service: ModWebService = ModWebService()
        local_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        remote_node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )

        with (
            patch.object(ModWebService, "_node_links", return_value=(local_node, remote_node)),
            patch.object(ModWebService, "_probe_node_status_async", new=AsyncMock()) as probe_node_status,
        ):
            probe_node_status.side_effect = [ModWebNodeStatus(node=local_node, alive=True)]
            statuses = asyncio.run(service._login_node_statuses_async(simulated_down_node_names=("erin",)))

        self.assertEqual(
            statuses,
            (
                ModWebNodeStatus(node=local_node, alive=True),
                ModWebNodeStatus(
                    node=remote_node,
                    alive=False,
                    detail="This node is being simulated as unavailable in dev mode.",
                    is_simulated_down=True,
                ),
            ),
        )
        probe_node_status.assert_awaited_once_with(local_node)

    def test_login_node_statuses_probe_nodes_concurrently(self) -> None:
        async def exercise() -> tuple[ModWebNodeStatus, ...]:
            service = ModWebService()
            first_node = ModWebNodeLink(
                node_name="yuki",
                label="Yuki",
                url="/",
                api_base_url="/api/node",
                api_url="/api/node/apps",
                is_current=True,
            )
            second_node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            started_nodes: list[ModWebNodeLink] = []
            all_started = asyncio.Event()

            async def fake_probe(node: ModWebNodeLink) -> ModWebNodeStatus:
                started_nodes.append(node)
                if len(started_nodes) == 2:
                    all_started.set()
                await asyncio.wait_for(all_started.wait(), timeout=0.2)
                return ModWebNodeStatus(node=node, alive=True)

            with (
                patch.object(ModWebService, "_node_links", return_value=(first_node, second_node)),
                patch.object(ModWebService, "_probe_node_status_async", side_effect=fake_probe),
            ):
                statuses = await service._login_node_statuses_async()

            self.assertEqual(started_nodes, [first_node, second_node])
            return statuses

        statuses = asyncio.run(exercise())

        self.assertEqual([status.node.node_name for status in statuses], ["yuki", "erin"])

    def test_friendly_remote_node_error_text_hides_connection_details(self) -> None:
        try:
            raise RuntimeError("Remote node request failed: url=https://erin.example/api/node/apps") from (
                requests.ConnectionError("Connection refused")
            )
        except RuntimeError as xcp:
            message = ModWebService._friendly_remote_node_error_text(xcp)

        self.assertEqual(
            message,
            "This node is unreachable right now. It may be offline or still waking up.",
        )

    def test_friendly_remote_node_error_text_hides_timeout_details(self) -> None:
        try:
            raise RuntimeError("Remote node request failed: url=https://erin.example/api/node/apps") from (
                requests.Timeout("Read timed out")
            )
        except RuntimeError as xcp:
            message = ModWebService._friendly_remote_node_error_text(xcp)

        self.assertEqual(
            message,
            "This node is taking too long to respond. It may be offline or still waking up.",
        )

    def test_remote_save_delete_uses_delete_request(self) -> None:
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        payload = {
            "app_name": "sevendays_alpha",
            "app_friendly": "7D2D Alpha",
            "node": "erin",
            "message": "Deleted save `AlphaWorld` for 7D2D Alpha.",
            "save": {
                "id": "save-abcd1234/AlphaWorld",
                "label": "AlphaWorld",
                "relative_path": "AlphaWorld",
                "root_id": "save-abcd1234",
                "root_label": "Navezgane",
                "kind": "directory",
                "size_bytes": 0,
                "size_text": "0 B",
                "modified_at": "2026-06-17 12:00:00",
                "can_delete": True,
            },
        }

        with patch.object(
            ModWebService,
            "_remote_json_async",
            new=AsyncMock(return_value=payload),
        ) as remote_json:
            result = asyncio.run(
                ModWebService()._remote_save_delete_async(
                    node,
                    "sevendays_alpha",
                    "save-abcd1234/AlphaWorld",
                    user,
                )
            )

        self.assertEqual(result.save.label, "AlphaWorld")
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="sevendays_alpha",
            path="/apps/sevendays_alpha/saves/save-abcd1234/AlphaWorld",
            scopes=(NodeApiScope.SAVES_WRITE,),
            user=user,
            method="DELETE",
        )

    def test_remote_node_system_action_sends_auto_restart_option(self) -> None:
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        payload = {
            "node": "erin",
            "action": NodeSystemAction.REBOOT_HOST.value,
            "message": "Scheduled host reboot for erin.",
        }

        with patch.object(
            ModWebService,
            "_remote_json_async",
            new=AsyncMock(return_value=payload),
        ) as remote_json:
            result = asyncio.run(
                ModWebService()._remote_node_system_action_async(
                    node,
                    NodeSystemAction.REBOOT_HOST,
                    False,
                    True,
                    user,
                )
            )

        self.assertEqual(result.action, NodeSystemAction.REBOOT_HOST)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name=None,
            path="/system/actions",
            scopes=(NodeApiScope.NODE_OPERATE,),
            user=user,
            method="POST",
            json_payload={
                "action": NodeSystemAction.REBOOT_HOST.value,
                "auto_restart_running_apps": False,
                "silent": True,
            },
        )

    def test_remote_app_mutation_sends_rcon_online_player_gate(self) -> None:
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        payload = {
            "app_name": "factorio_alpha",
            "app_friendly": "Factorio Alpha",
            "node": "erin",
            "action": NodeAppMutationAction.UPDATE_DETAILS.value,
            "message": "Updated details for Factorio Alpha.",
            "app_stats": None,
        }

        with patch.object(
            ModWebService,
            "_remote_json_async",
            new=AsyncMock(return_value=payload),
        ) as remote_json:
            result = asyncio.run(
                ModWebService()._remote_app_mutation_async(
                    node,
                    "factorio_alpha",
                    NodeAppMutationAction.UPDATE_DETAILS,
                    user,
                    rcon_requires_online_players=False,
                )
            )

        self.assertEqual(result.action, NodeAppMutationAction.UPDATE_DETAILS)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="factorio_alpha",
            path="/apps/factorio_alpha/mutate",
            scopes=(NodeApiScope.APP_MANAGE,),
            user=user,
            method="POST",
            json_payload={
                "action": "update_details",
                "rcon_requires_online_players": False,
                "running_cpu_points": None,
                "running_ram_points": None,
                "startup_cpu_points": None,
                "startup_ram_points": None,
                "steam_update_enabled": None,
                "steam_update_selected_branch": None,
            },
            timeout=15.0,
        )

    def test_mutate_app_wrapper_forwards_rcon_online_player_gate(self) -> None:
        service = ModWebService()
        model = cast(
            ModWebBasePageModel,
            cast(
                object,
                SimpleNamespace(
                    node_name="erin",
                    app_name="factorio_alpha",
                ),
            ),
        )
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        expected_result = object()

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_app_mutation_async",
                new=AsyncMock(return_value=expected_result),
            ) as remote_mutation,
        ):
            result = asyncio.run(
                service._mutate_app(
                    model=model,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    user=user,
                    rcon_requires_online_players=False,
                )
            )

        self.assertIs(result, expected_result)
        remote_mutation.assert_awaited_once()
        self.assertIs(remote_mutation.call_args.kwargs["node"], node)
        self.assertEqual(remote_mutation.call_args.kwargs["app_name"], "factorio_alpha")
        self.assertIs(remote_mutation.call_args.kwargs["action"], NodeAppMutationAction.UPDATE_DETAILS)
        self.assertFalse(remote_mutation.call_args.kwargs["rcon_requires_online_players"])

    def test_login_node_status_badge_marks_current_node_alive(self) -> None:
        status = ModWebNodeStatus(
            node=ModWebNodeLink(
                node_name="yuki",
                label="Yuki",
                url="/",
                api_base_url="/api/node",
                api_url="/api/node/apps",
                is_current=True,
            ),
            alive=True,
        )

        self.assertEqual(ModWebService._login_node_status_badge_text(status), "Yuki: Alive")
        self.assertEqual(ModWebService._login_node_status_badge_tone(status), "black")

    def test_login_node_status_badge_marks_simulated_node_down(self) -> None:
        status = ModWebNodeStatus(
            node=ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            ),
            alive=False,
            detail="This node is being simulated as unavailable in dev mode.",
            is_simulated_down=True,
        )

        self.assertEqual(ModWebService._login_node_status_badge_text(status), "Erin: Simulated Down")
        self.assertEqual(ModWebService._login_node_status_badge_tone(status), "warn")

    def test_login_node_statuses_can_simulate_current_node_down(self) -> None:
        service = ModWebService()
        local_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )

        with (
            patch.object(ModWebService, "_node_links", return_value=(local_node,)),
            patch.object(ModWebService, "_probe_node_status_async", new=AsyncMock()) as probe_node_status,
        ):
            statuses = asyncio.run(service._login_node_statuses_async(simulated_down_node_names=("yuki",)))

        self.assertEqual(
            statuses,
            (
                ModWebNodeStatus(
                    node=local_node,
                    alive=False,
                    detail="This node is being simulated as unavailable in dev mode.",
                    is_simulated_down=True,
                ),
            ),
        )
        probe_node_status.assert_not_awaited()

    def test_build_system_title_stats_formats_cpu_ram_and_storage(self) -> None:
        title_stats: tuple[ModWebTitleStat, ...] = ModWebService._build_system_title_stats(
            NodeSystemSummary(
                cpu_percent=31,
                ram_percent=44,
                ram_used_bytes=8 * 1024**3,
                ram_total_bytes=16 * 1024**3,
                storage_percent=55,
                storage_free_bytes=100 * 1024**3,
                storage_total_bytes=200 * 1024**3,
                cpu_per_core_percent=(20, 42),
                disks=(
                    NodeSystemDiskSummary(
                        mountpoint="/",
                        label="System",
                        percent=55,
                        free_bytes=100 * 1024**3,
                        total_bytes=200 * 1024**3,
                    ),
                    NodeSystemDiskSummary(
                        mountpoint="/mnt/data",
                        label="Data",
                        percent=65,
                        free_bytes=350 * 1024**3,
                        total_bytes=1000 * 1024**3,
                    ),
                ),
            )
        )

        self.assertEqual(
            [(stat.label, stat.tone) for stat in title_stats],
            [
                ("CPU", "grey"),
                ("RAM", "purple"),
                ("Storage", "purple"),
            ],
        )
        self.assertEqual(title_stats[0].value, "31%")
        self.assertIn("8.0GiB / 16.0GiB", title_stats[1].value)
        self.assertIn("100.0GiB / 200.0GiB", title_stats[2].value)

    def test_node_system_path_quotes_node_name(self) -> None:
        self.assertEqual(mod_web_node_system_path("node alpha"), "/mod-web/nodes/node%20alpha/system")

    def test_node_system_scope_badges_deduplicate_installed_app_scopes(self) -> None:
        entries = (
            NodeAppEntry(
                name="factorio_lab",
                friendly="Factorio Lab",
                node="erin",
                running=True,
                enabled=True,
                supports_mods=True,
                supports_configs=True,
            ),
            NodeAppEntry(
                name="minecraft_alpha",
                friendly="Minecraft Alpha",
                node="erin",
                running=False,
                enabled=False,
                supports_mods=True,
                supports_configs=True,
            ),
            NodeAppEntry(
                name="minecraft_beta",
                friendly="Minecraft Beta",
                node="erin",
                running=True,
                enabled=True,
                supports_mods=True,
                supports_configs=True,
            ),
            NodeAppEntry(
                name="custom_worker",
                friendly="Custom Worker",
                node="erin",
                running=False,
                enabled=True,
                supports_mods=False,
                supports_configs=False,
                scope="custom_scope",
            ),
        )

        badges = ModWebService._node_system_scope_badges(entries)

        self.assertEqual(
            [(badge.text, badge.tone) for badge in badges],
            [("Minecraft", "purple"), ("Factorio", "purple"), ("Custom Scope", "grey")],
        )

    def test_node_system_uptime_badges_show_system_and_bot_uptimes(self) -> None:
        badges = ModWebService._node_system_uptime_badges(
            NodeSystemSummary(
                cpu_percent=None,
                ram_percent=None,
                ram_used_bytes=None,
                ram_total_bytes=None,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                bot_uptime_seconds=3661,
                uptime_seconds=90061,
            )
        )

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="1d 1h 1m", tone="black", icon="dns", tooltip_text="System uptime"),
                _ModWebBadgeSpec(text="1h 1m", tone="black", icon="smart_toy", tooltip_text="Yukibot uptime"),
            ),
        )

    def test_node_system_operational_badges_match_app_hero_edge_order(self) -> None:
        badges = ModWebService._node_system_operational_badges(
            NodeSystemSummary(
                cpu_percent=None,
                ram_percent=None,
                ram_used_bytes=None,
                ram_total_bytes=None,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                bot_uptime_seconds=3661,
                uptime_seconds=90061,
                cpu_points_available=6,
                cpu_points_capacity=12,
                ram_points_available=2,
                ram_points_capacity=8,
            )
        )

        self.assertEqual(
            [(badge.text, badge.icon, badge.tooltip_text) for badge in badges],
            [
                ("1d 1h 1m", "dns", "System uptime"),
                ("1h 1m", "smart_toy", "Yukibot uptime"),
                ("6/12", "speed", "CPU"),
                ("2/8", "memory", "RAM"),
            ],
        )

    def test_node_system_operational_badges_omit_resource_points_when_unavailable(self) -> None:
        badges = ModWebService._node_system_operational_badges(
            NodeSystemSummary(
                cpu_percent=None,
                ram_percent=None,
                ram_used_bytes=None,
                ram_total_bytes=None,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                bot_uptime_seconds=3661,
                uptime_seconds=90061,
                cpu_points_available=6,
                cpu_points_capacity=12,
                ram_points_available=2,
                ram_points_capacity=8,
            ),
            include_resource_points=False,
        )

        self.assertEqual(
            [(badge.text, badge.icon, badge.tooltip_text) for badge in badges],
            [
                ("1d 1h 1m", "dns", "System uptime"),
                ("1h 1m", "smart_toy", "Yukibot uptime"),
            ],
        )

    def test_node_system_load_trend_and_warnings_report_actionable_signals(self) -> None:
        history = NodeSystemHistory(
            retention_seconds=3600,
            sample_interval_seconds=60,
            samples=(
                NodeSystemSample(captured_at_epoch_seconds=0, cpu_percent=42, ram_percent=55, storage_percent=60),
                NodeSystemSample(captured_at_epoch_seconds=600, cpu_percent=78, ram_percent=58, storage_percent=60),
            ),
        )
        summary = NodeSystemSummary(
            cpu_percent=92,
            ram_percent=78,
            ram_used_bytes=None,
            ram_total_bytes=None,
            storage_percent=91,
            storage_free_bytes=None,
            storage_total_bytes=None,
            cpu_points_available=0,
            cpu_points_capacity=8,
            ram_points_available=2,
            ram_points_capacity=12,
            start_blocked_app_ids=("minecraft",),
        )

        trend_badges = ModWebService._node_system_load_trend_badges(history)
        warning_badges = ModWebService._node_system_warning_badges(summary)

        self.assertEqual(
            [(badge.text, badge.tone) for badge in trend_badges], [("CPU +36pp", "red"), ("RAM +3pp", "black")]
        )
        self.assertEqual(
            [(badge.text, badge.tone) for badge in warning_badges],
            [
                ("CPU 92%", "red"),
                ("RAM 78%", "warn"),
                ("Storage 91%", "red"),
                ("CPU capacity 0/8", "red"),
                ("RAM capacity 2/12", "warn"),
                ("1 app start blocked", "warn"),
            ],
        )

    def test_build_node_system_stats_formats_live_summary(self) -> None:
        stats = ModWebService._build_node_system_stats(
            NodeSystemSummary(
                cpu_percent=31,
                ram_percent=44,
                ram_used_bytes=8 * 1024**3,
                ram_total_bytes=16 * 1024**3,
                storage_percent=55,
                storage_free_bytes=100 * 1024**3,
                storage_total_bytes=200 * 1024**3,
                cpu_per_core_percent=(20, 42),
                disks=(
                    NodeSystemDiskSummary(
                        mountpoint="/",
                        label="System",
                        percent=55,
                        free_bytes=100 * 1024**3,
                        total_bytes=200 * 1024**3,
                    ),
                    NodeSystemDiskSummary(
                        mountpoint="/mnt/data",
                        label="Data",
                        percent=65,
                        free_bytes=350 * 1024**3,
                        total_bytes=1000 * 1024**3,
                    ),
                ),
                bot_uptime_seconds=3661,
                uptime_seconds=90061,
                cpu_points_available=3,
                cpu_points_capacity=8,
                ram_points_available=5,
                ram_points_capacity=12,
                running_names=("Factorio Lab", "Minecraft Alpha"),
            )
        )

        self.assertEqual(
            [stat.label for stat in stats],
            ["CPU", "RAM & Storage"],
        )
        self.assertEqual([stat.tone for stat in stats], ["black", "black"])
        self.assertTrue(stats[0].show_label)
        self.assertFalse(stats[1].show_label)
        self.assertEqual(
            [(line.label, line.value) for line in stats[0].lines],
            [("Total", "31%"), ("Core 1", "20%"), ("Core 2", "42%")],
        )
        self.assertEqual([line.tone for line in stats[0].lines], ["grey", "grey", "purple"])
        self.assertEqual(
            [(line.label, line.is_section) for line in stats[1].lines],
            [("RAM", True), ("Storage", True), ("System", False), ("Data", False)],
        )
        self.assertEqual(stats[1].lines[0].value, "44% · 8.0GiB / 16.0GiB")
        self.assertEqual(stats[1].lines[1].value, "2 configured disks")
        self.assertEqual(
            [line.tone for line in stats[1].lines],
            ["purple", None, "purple", "purple"],
        )

    def test_node_bot_avatar_markup_supports_system_hero_class(self) -> None:
        service = ModWebService()
        with patch.object(service, "_node_bot_avatar_uri", return_value="https://cdn.example.com/yuki.png"):
            markup = service._node_bot_avatar_markup(
                node_name="yuki",
                display_name="Yuki",
                extra_class="mod-system-hero-avatar",
            )

        self.assertIn('class="mod-user-avatar mod-system-hero-avatar"', markup)
        self.assertIn('alt="Yuki bot avatar"', markup)

    def test_restart_interval_controls_round_trip_minute_precision(self) -> None:
        self.assertEqual(_restart_interval_parts(3_077), (2, 3, 17))
        self.assertEqual(_restart_interval_from_parts(days=2, hours=3, minutes=17), 3_077)
        self.assertEqual(_format_restart_hours_input(3, 0), "3")
        self.assertEqual(_format_restart_hours_input(3, 7), "3:07")
        self.assertEqual(_parse_restart_hours_input("3"), (3, 0))
        self.assertEqual(_parse_restart_hours_input("03:17"), (3, 17))
        with self.assertRaisesRegex(ValueError, "H or H:MM"):
            _parse_restart_hours_input("3.5")
        with self.assertRaisesRegex(ValueError, "0–59 minutes"):
            _parse_restart_hours_input("3:60")
        with self.assertRaisesRegex(ValueError, "between 1 hour and 1 week"):
            _restart_interval_from_parts(days=0, hours=0, minutes=59)

    def test_restart_anchor_and_display_use_named_timezones(self) -> None:
        timestamp = _restart_anchor_timestamp(
            _RestartWeekday.WEDNESDAY,
            "12:30",
            "UTC",
            now_timestamp=1_782_820_800,
        )

        self.assertEqual(timestamp, 1_782_909_000)
        self.assertEqual(
            _format_restart_timestamp(timestamp, "Australia/Melbourne"),
            "Wed, 01 Jul 2026 · 22:30 AEST",
        )

    def test_restart_state_line_formats_explicit_restart_kind(self) -> None:
        self.assertEqual(
            _format_restart_state_line(
                "Bot",
                1_782_909_000,
                RestartKind.MANUAL_SYS.value,
                "Australia/Melbourne",
            ),
            "Bot: Wed, 01 Jul 2026 · 22:30 AEST [manual_sys]",
        )

    def test_restart_anchor_rejects_nonexistent_dst_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            _restart_anchor_timestamp(
                _RestartWeekday.SUNDAY,
                "02:30",
                "Australia/Melbourne",
                now_timestamp=1_790_776_800,
            )

    def test_restart_anchor_uses_next_week_when_day_time_has_passed(self) -> None:
        timestamp = _restart_anchor_timestamp(
            _RestartWeekday.WEDNESDAY,
            "11:30",
            "UTC",
            now_timestamp=1_782_909_000,
        )

        self.assertEqual(timestamp, 1_783_510_200)

    def test_node_system_history_appends_by_sample_interval_and_renders_svg(self) -> None:
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
        updated = ModWebService._append_node_system_history(
            history,
            NodeSystemSummary(
                cpu_percent=25,
                ram_percent=35,
                ram_used_bytes=None,
                ram_total_bytes=None,
                storage_percent=45,
                storage_free_bytes=None,
                storage_total_bytes=None,
                captured_at_epoch_seconds=110,
            ),
        )

        self.assertEqual(len(updated.samples), 2)
        markup = ModWebService._node_system_history_svg(updated)
        self.assertIn('aria-label="CPU, RAM, and storage usage over the last hour"', markup)
        self.assertIn('class="mod-system-chart-line" pathLength="1"', markup)
        self.assertNotIn("mod-system-chart-line-enter", markup)
        self.assertIn("#a78bfa", markup)
        self.assertIn("#38bdf8", markup)

        animated_markup = ModWebService._node_system_history_svg(updated, animate=True)
        self.assertIn(
            'class="mod-system-chart-line mod-system-chart-line-enter" pathLength="1"',
            animated_markup,
        )

    def test_render_node_system_page_requires_sudo_and_builds_live_page(self) -> None:
        async def exercise() -> None:
            service = ModWebService()
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
            summary = NodeSystemSummary(
                cpu_percent=20,
                ram_percent=30,
                ram_used_bytes=3,
                ram_total_bytes=10,
                storage_percent=40,
                storage_free_bytes=6,
                storage_total_bytes=10,
            )
            authorised_user = AsyncMock(return_value=user)
            render_dashboard = Mock()
            history = NodeSystemHistory.empty()
            restart_schedules = NodeRestartScheduleState(node="erin", schedules=())
            restart_state = NodeRestartState(
                node="erin",
                process=NodeRestartRecord(timestamp=1_782_909_000, kind=RestartKind.MANUAL_BOT),
            )
            system_capabilities = NodeSystemCapabilities(
                actions=(NodeSystemAction.RESTART_PROCESS, NodeSystemAction.REBOOT_HOST),
            )
            capacity = config.NodeCapacityProfile(
                cpu_points_total=12,
                ram_points_total=24,
                cpu_points_reserved=2,
                ram_points_reserved=4,
            )
            font_sources = config.NodeFontSourceSettings(
                google_font_urls=("https://fonts.google.com/specimen/Inter",)
            )
            disk_settings = NodeDiskManagementState(
                node="erin",
                disks=(
                    NodeDiskEntry(
                        mountpoint="/mnt/data",
                        display_name="Data",
                        is_activity=True,
                        is_primary=True,
                        is_secondary=False,
                        is_bot_disk=True,
                    ),
                ),
                preferences=config.PersistedDiskPreferences(),
            )
            ui = cast(ModWebUi, cast(object, SimpleNamespace()))
            request = cast(
                Any,
                SimpleNamespace(
                    query_params=SimpleNamespace(getlist=Mock(return_value=[])),
                    url=SimpleNamespace(path="/mod-web/nodes/erin/system", query=""),
                ),
            )
            with (
                patch.object(service, "_authorised_page_user", new=authorised_user),
                patch.object(service, "_remote_node_link", return_value=node),
                patch.object(
                    service,
                    "_probe_node_status_async",
                    new=AsyncMock(return_value=ModWebNodeStatus(node=node, alive=True, detail="HTTP 204")),
                ),
                patch.object(service, "_remote_node_system_summary_async", new=AsyncMock(return_value=summary)),
                patch.object(service, "_remote_node_system_history_async", new=AsyncMock(return_value=history)),
                patch.object(service, "_remote_apps_async", new=AsyncMock(return_value=())),
                patch.object(service, "_user_has_level", return_value=True),
                patch.object(
                    service,
                    "_remote_restart_schedules_async",
                    new=AsyncMock(return_value=restart_schedules),
                ),
                patch.object(
                    service,
                    "_remote_restart_state_async",
                    new=AsyncMock(return_value=restart_state),
                ),
                patch.object(
                    service,
                    "_remote_node_system_capabilities_async",
                    new=AsyncMock(return_value=system_capabilities),
                ),
                patch.object(service, "_node_capacity", new=AsyncMock(return_value=capacity)),
                patch.object(service, "_node_font_sources", new=AsyncMock(return_value=font_sources)),
                patch.object(service, "_node_disk_settings", new=AsyncMock(return_value=disk_settings)),
                patch.object(service, "_render_node_system_dashboard", new=render_dashboard),
            ):
                await service._render_node_system_page(
                    ui=ui,
                    node_name="erin",
                    request=request,
                )

            authorised_user.assert_awaited_once_with(
                ui=ui,
                request=request,
                required_level=Power_Level.sudo,
            )
            self.assertEqual(render_dashboard.call_args.kwargs["node"], node)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_system_summary"], summary)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_system_history"], history)
            self.assertEqual(
                render_dashboard.call_args.kwargs["initial_node_status"],
                ModWebNodeStatus(node=node, alive=True, detail="HTTP 204"),
            )
            self.assertEqual(render_dashboard.call_args.kwargs["initial_app_entries"], ())
            self.assertEqual(render_dashboard.call_args.kwargs["initial_restart_schedules"], restart_schedules)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_restart_state"], restart_state)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_system_capabilities"], system_capabilities)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_node_capacity"], capacity)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_node_font_sources"], font_sources)
            self.assertEqual(render_dashboard.call_args.kwargs["initial_node_disk_settings"], disk_settings)
            self.assertEqual(render_dashboard.call_args.kwargs["current_url"], "/mod-web/nodes/erin/system")

        asyncio.run(exercise())

    def test_build_node_disk_preferences_preserves_unknown_labels_and_supports_secondary_disk(self) -> None:
        initial_settings = NodeDiskManagementState(
            node="erin",
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
                    is_activity=True,
                    is_primary=False,
                    is_secondary=True,
                    is_bot_disk=False,
                ),
            ),
            preferences=config.PersistedDiskPreferences(
                labels={"/mnt/data": "Fast", "/mnt/offline": "Offline"},
                secondary_mount="/mnt/backups",
            ),
        )

        preferences = ModWebService._build_node_disk_preferences(
            initial_settings=initial_settings,
            selected_activity_mounts=("/mnt/backups",),
            primary_choice="/mnt/backups",
            secondary_choice=_ModWebNodeDiskChoice.NO_SECONDARY.value,
            label_values={"/mnt/data": "", "/mnt/backups": "Archive"},
        )

        self.assertEqual(preferences.activity_mounts, ["/mnt/backups"])
        self.assertEqual(preferences.primary_mount, "/mnt/backups")
        self.assertIsNone(preferences.secondary_mount)
        self.assertEqual(
            preferences.labels,
            {"/mnt/offline": "Offline", "/mnt/backups": "Archive"},
        )

    def test_render_node_system_page_reconnects_after_transient_node_restart(self) -> None:
        async def exercise() -> None:
            service = ModWebService()
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
            connection_error = RuntimeError("Remote node request failed")
            connection_error.__cause__ = aiohttp.ClientConnectionError("node restarting")
            timer = object()
            navigate_to = Mock()
            timer_factory = Mock(return_value=timer)
            ui = cast(
                ModWebUi,
                cast(object, SimpleNamespace(timer=timer_factory, navigate=SimpleNamespace(to=navigate_to))),
            )
            request = cast(Any, SimpleNamespace())
            render_unavailable = Mock()
            register_timer_cleanup = Mock()
            with (
                patch.object(service, "_authorised_page_user", new=AsyncMock(return_value=user)),
                patch.object(service, "_remote_node_link", return_value=node),
                patch.object(
                    service,
                    "_remote_node_system_summary_async",
                    new=AsyncMock(side_effect=connection_error),
                ),
                patch.object(service, "_remote_node_system_history_async", new=AsyncMock(side_effect=connection_error)),
                patch.object(service, "_remote_apps_async", new=AsyncMock(side_effect=connection_error)),
                patch.object(service, "_remote_restart_schedules_async", new=AsyncMock(side_effect=connection_error)),
                patch.object(service, "_remote_restart_state_async", new=AsyncMock(side_effect=connection_error)),
                patch.object(service, "_user_has_level", return_value=False),
                patch.object(service, "_render_remote_node_unavailable_page", new=render_unavailable),
                patch.object(
                    service,
                    "_probe_node_status_async",
                    new=AsyncMock(return_value=ModWebNodeStatus(node=node, alive=True, detail="HTTP 204")),
                ) as probe,
                patch.object(service, "_register_timer_cleanup", new=register_timer_cleanup),
            ):
                await service._render_node_system_page(ui=ui, node_name="erin", request=request)
                reconnect = timer_factory.call_args.args[1]
                await reconnect()

            render_unavailable.assert_called_once_with(
                ui=ui,
                node_name="erin",
                exception=connection_error,
                retry_url="/mod-web/nodes/erin/system",
            )
            self.assertEqual(probe.await_args_list, [call(node), call(node, log_failures=False)])
            navigate_to.assert_called_once_with("/mod-web/nodes/erin/system")
            register_timer_cleanup.assert_called_once_with(ui=ui, timer=timer)

        asyncio.run(exercise())

    def test_remote_node_connection_errors_are_transient(self) -> None:
        wrapped = RuntimeError("Remote node request failed")
        wrapped.__cause__ = aiohttp.ClientConnectionError("node restarting")

        self.assertTrue(ModWebService._remote_node_error_is_transient(wrapped))
        self.assertEqual(
            ModWebService._friendly_remote_node_error_text(wrapped),
            "This node is unreachable right now. It may be offline or still waking up.",
        )
        self.assertFalse(ModWebService._remote_node_error_is_transient(ValueError("invalid payload")))

    def test_app_page_logs_transient_node_restart_without_traceback(self) -> None:
        async def exercise() -> None:
            service = ModWebService()
            node = ModWebNodeLink(
                node_name="yuki",
                label="Yuki",
                url="/mod-web/nodes/yuki",
                api_base_url="http://127.0.0.1:8082/api/node",
                api_url="/api/node-proxy/yuki/apps",
                is_current=True,
            )
            user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
            connection_error = RuntimeError("Remote node request failed")
            connection_error.__cause__ = aiohttp.ClientConnectionError("node restarting")
            ui = cast(ModWebUi, cast(object, SimpleNamespace()))
            render_error = Mock()

            with (
                patch.object(service, "_authorised_page_user", new=AsyncMock(return_value=user)),
                patch.object(service, "_remote_node_link", return_value=node),
                patch.object(service, "_remote_app_entry_async", new=AsyncMock(side_effect=connection_error)),
                patch.object(service, "_render_error_page", new=render_error),
                patch("web_dash.page_handlers.log.info") as log_info,
                patch("web_dash.page_handlers.log.exception") as log_exception,
            ):
                await service._render_node_mods_page(
                    ui=ui,
                    node_name="yuki",
                    app_name="minecraft_all_fabric",
                    request=cast(Any, SimpleNamespace()),
                )

            log_info.assert_called_once_with(
                "Remote mod web app temporarily unavailable: node=%s app=%s reason=%s",
                "yuki",
                "minecraft_all_fabric",
                "This node is unreachable right now. It may be offline or still waking up.",
            )
            log_exception.assert_not_called()
            render_error.assert_called_once_with(
                ui=ui,
                title="Page unavailable",
                detail="This node is unreachable right now. It may be offline or still waking up.",
                app_name="minecraft_all_fabric",
            )

        asyncio.run(exercise())

    def test_format_uptime_seconds_compacts_duration(self) -> None:
        self.assertEqual(ModWebService._format_uptime_seconds(59), "<1m")
        self.assertEqual(ModWebService._format_uptime_seconds(3661), "1h 1m")
        self.assertEqual(ModWebService._format_uptime_seconds(90061), "1d 1h 1m")

    def test_format_update_timestamp_uses_utc(self) -> None:
        self.assertEqual(
            ModWebService._format_update_timestamp(0),
            "1970-01-01 00:00:00 UTC",
        )

    def test_format_update_duration_compacts_running_and_completed_attempts(self) -> None:
        self.assertEqual(
            ModWebService._format_update_duration(started_at_unix_ms=0, finished_at_unix_ms=59_000),
            "59s",
        )
        self.assertEqual(
            ModWebService._format_update_duration(started_at_unix_ms=0, finished_at_unix_ms=125_000),
            "2m 5s",
        )
        self.assertEqual(
            ModWebService._format_update_duration(started_at_unix_ms=0, finished_at_unix_ms=3_661_000),
            "1h 1m",
        )

    def test_prefer_newer_update_status_keeps_running_state_over_older_idle_snapshot(self) -> None:
        current_status = AppUpdateStatus(
            state=AppUpdateState.RUNNING,
            summary="Downloading",
            operation_kind=AppUpdateOperationKind.UPDATE,
            progress_percent=24.0,
            started_at_unix_ms=2_000,
        )
        next_status = AppUpdateStatus(
            state=AppUpdateState.IDLE,
            summary="Ready",
        )

        preferred = ModWebService._prefer_newer_update_status(current_status, next_status)

        self.assertEqual(preferred, current_status)

    def test_resolve_update_target_branch_id_falls_back_when_selection_is_stale(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(
                AppUpdateBranchState(branch_id="public", label="Stable", selected=True),
                AppUpdateBranchState(branch_id="latest_experimental", label="Experimental", selected=False),
            ),
            supports_verify=True,
        )

        resolved_branch_id = ModWebService._resolve_update_target_branch_id(update_info, "removed_branch")

        self.assertEqual(resolved_branch_id, "public")

    def test_pending_update_target_branch_id_returns_pending_branch(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(
                AppUpdateBranchState(branch_id="public", label="Stable", selected=True),
                AppUpdateBranchState(branch_id="latest_experimental", label="Experimental", selected=False),
            ),
            supports_verify=True,
        )

        pending_branch_id = ModWebService._pending_update_target_branch_id(update_info, "latest_experimental")

        self.assertEqual(pending_branch_id, "latest_experimental")

    def test_pending_update_target_branch_id_returns_none_when_branch_is_current(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
        )

        pending_branch_id = ModWebService._pending_update_target_branch_id(update_info, "public")

        self.assertIsNone(pending_branch_id)

    def test_update_branch_display_text_includes_label_and_branch_id(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(
                AppUpdateBranchState(branch_id="public", label="Stable", selected=True),
                AppUpdateBranchState(branch_id="latest_experimental", label="Experimental", selected=False),
            ),
            supports_verify=True,
        )

        branch_text = ModWebService._update_branch_display_text(update_info, "latest_experimental")

        self.assertEqual(branch_text, "Experimental (latest_experimental)")

    def test_details_steam_update_preset_resolves_scope_default(self) -> None:
        preset = ModWebService._details_steam_update_preset("sevendays_alpha")

        self.assertIsNotNone(preset)
        assert preset is not None
        self.assertEqual(preset.app_id, 294420)
        self.assertEqual(preset.default_selected_branch, "latest_experimental")

    def test_details_steam_update_branch_options_fall_back_to_scope_preset(self) -> None:
        options = ModWebService._details_steam_update_branch_options(
            app_name="satisfactory_alpha",
            update_info=None,
        )

        self.assertEqual(
            options,
            {
                "public": "Stable (public)",
                "experimental": "Experimental (experimental)",
            },
        )

    def test_pending_update_target_display_text_reports_no_change_when_current(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
        )

        pending_text = ModWebService._pending_update_target_display_text(update_info, "public")

        self.assertEqual(pending_text, "No pending change")

    def test_update_progress_text_reports_unavailable_when_percent_missing(self) -> None:
        self.assertEqual(
            ModWebService._update_progress_text(
                AppUpdateStatus(
                    state=AppUpdateState.RUNNING,
                    summary="Downloading",
                    operation_kind=AppUpdateOperationKind.UPDATE,
                )
            ),
            "Progress unavailable",
        )

    def test_update_action_block_reason_reports_verify_provider_limit(self) -> None:
        self.assertEqual(
            ModWebService._update_action_block_reason(
                action=NodeAppMutationAction.VERIFY,
                can_manage_updates=True,
                app_running=False,
                update_running=False,
                supports_verify=False,
            ),
            "Verification is not available for this update provider.",
        )

    def test_update_action_block_reason_prioritises_running_operation(self) -> None:
        self.assertEqual(
            ModWebService._update_action_block_reason(
                action=NodeAppMutationAction.UPDATE,
                can_manage_updates=True,
                app_running=True,
                update_running=True,
                supports_verify=True,
            ),
            "Another update operation is already running.",
        )

    def test_update_install_alignment_badge_reports_matching_manifest_branch(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
            installed_branch_id="public",
        )

        badge = ModWebService._update_install_alignment_badge(update_info)

        self.assertEqual(badge, _ModWebBadgeSpec(text="Installed matches configured target", tone="black"))

    def test_update_install_alignment_badge_reports_branch_drift(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(
                AppUpdateBranchState(branch_id="public", label="Stable", selected=True),
                AppUpdateBranchState(branch_id="latest_experimental", label="Experimental", selected=False),
            ),
            supports_verify=True,
            installed_branch_id="latest_experimental",
        )

        badge = ModWebService._update_install_alignment_badge(update_info)

        self.assertEqual(badge, _ModWebBadgeSpec(text="Installed differs from configured target", tone="purple"))

    def test_update_section_view_signature_ignores_irrelevant_runtime_fields(self) -> None:
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
        )
        update_status = AppUpdateStatus(state=AppUpdateState.IDLE, summary="Ready")
        base_stats = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version="1.2.3",
            player_count=0,
            player_capacity=20,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            connected_player_names=(),
        )
        changed_stats = replace(
            base_stats,
            player_count=4,
            player_capacity=24,
            connected_player_names=("One", "Two"),
        )
        base_model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=base_stats,
            app_start_blocked=False,
            settings=None,
            update_info=update_info,
            update_status=update_status,
        )
        changed_model = replace(base_model, app_stats=changed_stats)

        self.assertEqual(
            ModWebService._update_section_view_signature(base_model),
            ModWebService._update_section_view_signature(changed_model),
        )

    def test_update_section_view_signature_tracks_running_and_version(self) -> None:
        base_stats = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version="1.2.3",
            player_count=0,
            player_capacity=20,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            connected_player_names=(),
        )
        changed_stats = replace(base_stats, running=True, version="1.2.4")

        self.assertNotEqual(
            ModWebService._update_section_runtime_signature(base_stats),
            ModWebService._update_section_runtime_signature(changed_stats),
        )

    def test_dry_update_preview_statuses_cover_running_success_and_failure_states(self) -> None:
        statuses = ModWebService._dry_update_preview_statuses()

        self.assertEqual(len(statuses), 5)
        self.assertEqual(statuses[0].state, AppUpdateState.RUNNING)
        self.assertEqual(statuses[0].operation_kind, AppUpdateOperationKind.UPDATE)
        self.assertEqual(statuses[1].state, AppUpdateState.SUCCEEDED)
        self.assertEqual(statuses[2].state, AppUpdateState.FAILED)
        self.assertEqual(statuses[3].operation_kind, AppUpdateOperationKind.VERIFY)
        self.assertEqual(statuses[4].operation_kind, AppUpdateOperationKind.VERIFY)

    def test_user_level_helpers_reflect_acl_level(self) -> None:
        with TemporaryDirectory[str]() as tmp:
            pointer: Path = Path(tmp) / "users.json"
            pointer.write_text('{"sudo": [42]}')
            service: ModWebService = ModWebService()
            service.set_acl(Access_Control(pointer))

        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        self.assertEqual(service._user_level(user), Power_Level.sudo)
        self.assertEqual(service._user_level_label(user), "Sudo")
        self.assertEqual(service._user_level_tone(user), "red")

    def test_user_avatar_uri_prefers_discord_profile_avatar(self) -> None:
        user = ModWebUser(discord_id=42, username="tester", global_name="Tester", avatar_hash="abc123")

        avatar_uri = ModWebService()._user_avatar_uri(user)

        self.assertEqual(avatar_uri, "https://cdn.discordapp.com/avatars/42/abc123.png?size=128")

    def test_user_avatar_uri_falls_back_to_power_level_icon(self) -> None:
        with TemporaryDirectory[str]() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text('{"sudo": [42]}')
            service = ModWebService()
            service.set_acl(Access_Control(pointer))

        user = ModWebUser(discord_id=42, username="tester", global_name="Tester", avatar_hash=None)

        avatar_uri = service._user_avatar_uri(user)

        self.assertTrue(avatar_uri.startswith("data:image/png;base64,"))

    def test_user_avatar_uri_falls_back_to_inline_svg_when_level_icon_is_missing(self) -> None:
        with TemporaryDirectory[str]() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text('{"admin": [42]}')
            service = ModWebService()
            service.set_acl(Access_Control(pointer))

        user = ModWebUser(discord_id=42, username="tester", global_name="Tester", avatar_hash=None)

        with patch("web_dash.avatars._user_avatar_icon_data_uri", return_value=None):
            avatar_uri = service._user_avatar_uri(user)

        self.assertTrue(avatar_uri.startswith("data:image/svg+xml;base64,"))
        encoded_svg = avatar_uri.removeprefix("data:image/svg+xml;base64,")
        svg_markup = base64.b64decode(encoded_svg).decode("utf-8")
        self.assertIn("<svg", svg_markup)
        self.assertIn('aria-label="Admin avatar fallback"', svg_markup)

    def test_page_unavailable_icon_markup_loads_svg_resource(self) -> None:
        svg_markup = ModWebService._page_unavailable_icon_markup()

        self.assertIn("<svg", svg_markup)
        self.assertIn('viewBox="0 0 80 80"', svg_markup)
        self.assertIn("M54 18h11l5 5v14H54Z", svg_markup)

    def test_error_page_icon_markup_uses_chat_specific_icon(self) -> None:
        svg_markup = ModWebService()._error_page_icon_markup("Chat unavailable")

        self.assertIn("<svg", svg_markup)
        self.assertIn('viewBox="0 0 80 80"', svg_markup)
        self.assertIn("M55.5 23h10.5", svg_markup)

    def test_error_page_icon_markup_falls_back_to_generic_icon(self) -> None:
        svg_markup = ModWebService()._error_page_icon_markup("Page unavailable")

        self.assertIn("<svg", svg_markup)
        self.assertIn('viewBox="0 0 80 80"', svg_markup)
        self.assertIn("M24 30 30 14l8 12", svg_markup)

    def test_user_avatar_markup_renders_avatar_image_tag(self) -> None:
        markup = ModWebService._user_avatar_markup(
            avatar_uri="https://cdn.discordapp.com/avatars/42/abc123.png?size=128",
            display_name="Tester",
        )

        self.assertIn('<img class="mod-user-avatar"', markup)
        self.assertIn('src="https://cdn.discordapp.com/avatars/42/abc123.png?size=128"', markup)
        self.assertIn('alt="Tester avatar"', markup)

    def test_badge_avatar_markup_escapes_image_attributes(self) -> None:
        markup = ModWebService._badge_avatar_markup(
            avatar_uri='https://example.invalid/avatar.png?size=32&variant="square"',
            display_name='Admin <Alice> "Root"',
        )

        self.assertIn(
            'src="https://example.invalid/avatar.png?size=32&amp;variant=&quot;square&quot;"',
            markup,
        )
        self.assertIn('alt="Admin &lt;Alice&gt; &quot;Root&quot; avatar"', markup)
        self.assertIn('loading="lazy"', markup)

    def test_run_server_preserves_existing_logging_configuration(self) -> None:
        class FakeUi:
            run_kwargs: dict[str, object] | None = None

            def run(self, **kwargs: object) -> None:
                self.run_kwargs = kwargs

        ui: FakeUi = FakeUi()
        ModWebService()._run_server(object(), ui)

        self.assertIsNotNone(ui.run_kwargs)
        assert ui.run_kwargs is not None
        self.assertIn("log_config", ui.run_kwargs)
        self.assertIsNone(ui.run_kwargs["log_config"])

    def test_external_chat_link_opens_in_new_tab(self) -> None:
        class FakeLink:
            props_value: str | None = None

            def props(self, value: str) -> "FakeLink":
                self.props_value = value
                return self

        class FakeUi:
            label: str | None = None
            url: str | None = None
            link_object: FakeLink = FakeLink()

            def link(self, label: str, url: str) -> FakeLink:
                self.label = label
                self.url = url
                return self.link_object

        ui: FakeUi = FakeUi()

        link: Link = ModWebService._external_chat_link(
            ui=cast(ModWebUi, cast(object, ui)),
            label="cat.png",
            url="https://example.invalid/cat.png",
        )

        self.assertIs(link, ui.link_object)
        self.assertEqual(ui.label, "cat.png")
        self.assertEqual(ui.url, "https://example.invalid/cat.png")
        self.assertEqual(ui.link_object.props_value, 'target="_blank" rel="noopener noreferrer"')

    def test_action_link_can_open_in_new_tab(self) -> None:
        class FakeLink:
            class_value: str | None = None
            props_value: str | None = None
            js_handler: str | None = None

            def classes(self, value: str) -> "FakeLink":
                self.class_value = value
                return self

            def props(self, value: str) -> "FakeLink":
                self.props_value = value
                return self

            def on(self, event: str, *, js_handler: str) -> "FakeLink":
                assert event == "click"
                self.js_handler = js_handler
                return self

        class FakeUi:
            label: str | None = None
            url: str | None = None
            link_object: FakeLink = FakeLink()

            def link(self, label: str, url: str) -> FakeLink:
                self.label = label
                self.url = url
                return self.link_object

        ui: FakeUi = FakeUi()

        ModWebService._action_link(
            ui=cast(ModWebUi, cast(object, ui)),
            label="Chat",
            url="/mod-web/chat/minecraft_alpha",
            compact=True,
            extra_classes="mod-toolbar-chat-button",
            stop_propagation=True,
            new_tab=True,
        )

        self.assertEqual(ui.label, "Chat")
        self.assertEqual(ui.url, "/mod-web/chat/minecraft_alpha")
        self.assertEqual(ui.link_object.props_value, 'target="_blank" rel="noopener noreferrer"')
        self.assertEqual(ui.link_object.js_handler, "(event) => event.stopPropagation()")

    def test_badge_link_can_open_shift_click_target(self) -> None:
        class FakeLink:
            class_value: str | None = None
            props_value: str | None = None
            js_handler: str | None = None

            def classes(self, value: str) -> "FakeLink":
                self.class_value = value
                return self

            def props(self, value: str) -> "FakeLink":
                self.props_value = value
                return self

            def on(self, event: str, *, js_handler: str) -> "FakeLink":
                assert event == "click"
                self.js_handler = js_handler
                return self

        class FakeUi:
            label: str | None = None
            url: str | None = None
            link_object: FakeLink = FakeLink()

            def link(self, label: str, url: str) -> FakeLink:
                self.label = label
                self.url = url
                return self.link_object

        ui: FakeUi = FakeUi()

        ModWebService._badge_link(
            ui=cast(ModWebUi, cast(object, ui)),
            text="Chat",
            tone="purple",
            url="/mod-web/mods/minecraft_alpha?tab=chat",
            shift_url="/mod-web/chat/minecraft_alpha",
            stop_propagation=True,
        )

        self.assertEqual(ui.label, "Chat")
        self.assertEqual(ui.url, "/mod-web/mods/minecraft_alpha?tab=chat")
        self.assertIsNotNone(ui.link_object.js_handler)
        assert ui.link_object.js_handler is not None
        self.assertIn("event.shiftKey", ui.link_object.js_handler)
        self.assertIn('window.open("/mod-web/chat/minecraft_alpha"', ui.link_object.js_handler)
        self.assertIn("event.stopPropagation();", ui.link_object.js_handler)

    def test_badge_link_can_open_in_new_tab(self) -> None:
        class FakeLink:
            class_value: str | None = None
            props_value: str | None = None

            def classes(self, value: str) -> "FakeLink":
                self.class_value = value
                return self

            def props(self, value: str) -> "FakeLink":
                self.props_value = value
                return self

        class FakeUi:
            label: str | None = None
            url: str | None = None
            link_object: FakeLink = FakeLink()

            def link(self, label: str, url: str) -> FakeLink:
                self.label = label
                self.url = url
                return self.link_object

        ui: FakeUi = FakeUi()

        ModWebService._badge_link(
            ui=cast(ModWebUi, cast(object, ui)),
            text="Map",
            tone="purple",
            url="https://example.invalid/squaremap/",
            new_tab=True,
        )

        self.assertEqual(ui.label, "Map")
        self.assertEqual(ui.url, "https://example.invalid/squaremap/")
        self.assertEqual(ui.link_object.props_value, 'target="_blank" rel="noopener noreferrer"')

    def test_node_links_include_current_and_known_mod_web_nodes(self) -> None:
        remote_snapshot: BotMetadataSnapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="123456789012345678",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="http://erin.example:3180",
                    node_api_base_url="http://erin.example:3180/api/node",
                )
            ),
        )
        bot_config: BotConfiguration = config.BotConfiguration(KnownBots={remote_snapshot.profile.id: remote_snapshot})
        server: ModWebServerConfig = replace(
            config.MOD_WEB_SERVER,
            node_name="yuki",
            public_base_url="http://yuki.example:3180",
            node_api_base_url="http://yuki.example:3180/api/node",
            token_secret=None,
        )

        with TemporaryDirectory[str]() as temp_dir:
            missing_cache: Path = Path(temp_dir) / "bots.json"
            with (
                patch.object(config, "MOD_WEB_SERVER", server),
                patch.object(config, "load_bot_configuration", return_value=bot_config),
                patch.object(config, "authority_cache_path", return_value=missing_cache),
            ):
                links: tuple[ModWebNodeLink, ...] = ModWebService()._node_links()

        self.assertEqual([link.node_name for link in links], ["yuki", "erin"])
        self.assertEqual(links[0].label, "Yuki")
        self.assertTrue(links[0].is_current)
        self.assertEqual(links[0].url, "/mod-web/nodes/yuki/system")
        self.assertEqual(links[0].latency_probe_url, "http://yuki.example:3180/api/node/ping")
        self.assertEqual(links[0].presence_stream_url, "http://yuki.example:3180/api/node/presence/stream")
        self.assertEqual(links[1].url, "/mod-web/nodes/erin/system")
        self.assertEqual(links[1].api_base_url, "http://erin.example:3180/api/node")
        self.assertEqual(links[1].api_url, "/api/node-proxy/erin/apps")
        self.assertEqual(links[1].latency_probe_url, "http://erin.example:3180/api/node/ping")
        self.assertEqual(links[1].presence_stream_url, "http://erin.example:3180/api/node/presence/stream")

    def test_node_links_ignore_registry_nodes_without_dashboard_profiles(self) -> None:
        profiled_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="123456789012345678",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="http://erin.example:3180",
                    node_api_base_url="http://erin.example:3180/api/node",
                )
            ),
        )
        unprofiled_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="223456789012345678",
                label="Kousei",
                bot_profile=None,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="kousei",
                    public_base_url="http://kousei.example:3180",
                    node_api_base_url="http://kousei.example:3180/api/node",
                )
            ),
        )

        with patch.object(ModWebService, "_known_bot_snapshots", return_value=(profiled_snapshot, unprofiled_snapshot)):
            links = ModWebService()._node_links()

        self.assertEqual([link.node_name for link in links if not link.is_current], ["erin"])

    def test_portal_default_node_name_ignores_unprofiled_registry_nodes(self) -> None:
        unprofiled_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="223456789012345678",
                label="Kousei",
                bot_profile=None,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="kousei",
                    public_base_url="http://kousei.example:3180",
                    node_api_base_url="http://kousei.example:3180/api/node",
                )
            ),
        )

        with (
            patch.object(config, "env_opt", return_value=None),
            patch.object(ModWebService, "_known_bot_snapshots", return_value=(unprofiled_snapshot,)),
        ):
            self.assertIsNone(ModWebService()._portal_default_node_name())

    def test_portal_node_links_include_current_portal_node(self) -> None:
        yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="764270771350142976",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="https://wakusei.apasz.com",
                    node_api_base_url="https://wakusei.apasz.com/api/node",
                )
            ),
        )
        erin_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="123456789012345678",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="https://erin.example",
                    node_api_base_url="https://erin.example/api/node",
                )
            ),
        )
        bot_config = config.BotConfiguration(
            KnownBots={
                yuki_snapshot.profile.id: yuki_snapshot,
                erin_snapshot.profile.id: erin_snapshot,
            }
        )
        portal_profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]
        server = replace(config.MOD_WEB_SERVER, node_name="portal")

        with TemporaryDirectory[str]() as temp_dir:
            missing_cache = Path(temp_dir) / "bots.json"
            with (
                patch.object(config, "ACTIVE_BOT_PROFILE", portal_profile),
                patch.object(config, "MOD_WEB_SERVER", server),
                patch.object(config, "load_bot_configuration", return_value=bot_config),
                patch.object(config, "authority_cache_path", return_value=missing_cache),
            ):
                links = ModWebService()._node_links()

        self.assertEqual([link.node_name for link in links], ["portal", "yuki", "erin"])
        self.assertTrue(links[0].is_current)

    def test_portal_node_links_include_portal(self) -> None:
        portal_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="987654321098765432",
                label="Portal",
                bot_profile=config.BotProfileName.PORTAL,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="portal",
                    public_base_url="https://portal.example",
                    node_api_base_url="https://portal.example/api/node",
                )
            ),
        )
        portal_profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]
        server = replace(config.MOD_WEB_SERVER, node_name="portal")

        with (
            patch.object(config, "ACTIVE_BOT_PROFILE", portal_profile),
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_known_bot_snapshots", return_value=(portal_snapshot,)),
        ):
            links = ModWebService()._node_links()

        self.assertEqual([link.node_name for link in links], ["portal"])
        self.assertEqual(links[0].label, "Portal")
        self.assertEqual(links[0].api_base_url, config.LOCAL_NODE_API_BASE_URL)
        self.assertEqual(links[0].latency_probe_url, f"{config.LOCAL_NODE_API_BASE_URL}/ping")
        self.assertEqual(links[0].presence_stream_url, f"{config.LOCAL_NODE_API_BASE_URL}/presence/stream")

    def test_portal_node_links_prefer_dev_cluster_env_over_stale_snapshots(self) -> None:
        stale_yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="764270771350142976",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="http://124.187.226.10",
                    node_api_base_url="http://124.187.226.10/api/node",
                )
            ),
        )
        stale_erin_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="123456789012345678",
                label="Erin",
                bot_profile=config.BotProfileName.ERIN,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="erin",
                    public_base_url="http://127.0.0.1:3181",
                    node_api_base_url="http://127.0.0.1:3181/api/node",
                )
            ),
        )
        bot_config = config.BotConfiguration(
            KnownBots={
                stale_yuki_snapshot.profile.id: stale_yuki_snapshot,
                stale_erin_snapshot.profile.id: stale_erin_snapshot,
            }
        )
        portal_profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]
        server = replace(config.MOD_WEB_SERVER, node_name="portal", public_base_url="http://127.0.0.1:3180")
        dev_cluster_payload = json.dumps(
            [
                {
                    "node_name": "yuki",
                    "label": "Yuki",
                    "node_api_public_base_url": "http://127.0.0.1:8082",
                },
                {
                    "node_name": "erin",
                    "label": "Erin",
                    "node_api_public_base_url": "http://127.0.0.1:8083",
                },
            ]
        )

        with TemporaryDirectory[str]() as temp_dir:
            missing_cache = Path(temp_dir) / "bots.json"
            with (
                patch.object(config, "ACTIVE_BOT_PROFILE", portal_profile),
                patch.object(config, "MOD_WEB_SERVER", server),
                patch.object(config, "load_bot_configuration", return_value=bot_config),
                patch.object(config, "authority_cache_path", return_value=missing_cache),
                patch.object(config, "INDEV", True),
                patch.object(config, "env_opt", side_effect=lambda name: dev_cluster_payload if name == "DEV_CLUSTER_NODE_LINKS_JSON" else None),
            ):
                links = ModWebService()._node_links()

        self.assertEqual([link.node_name for link in links], ["portal", "yuki", "erin"])
        self.assertEqual(links[1].api_base_url, "http://127.0.0.1:8082/api/node")
        self.assertEqual(links[2].api_base_url, "http://127.0.0.1:8083/api/node")
        self.assertEqual(links[1].latency_probe_url, "http://127.0.0.1:8082/api/node/ping")
        self.assertEqual(links[2].latency_probe_url, "http://127.0.0.1:8083/api/node/ping")
        self.assertEqual(links[1].presence_stream_url, "http://127.0.0.1:8082/api/node/presence/stream")
        self.assertEqual(links[2].presence_stream_url, "http://127.0.0.1:8083/api/node/presence/stream")

    def test_portal_default_node_name_prefers_dev_cluster_yuki_link(self) -> None:
        dev_cluster_payload = json.dumps(
            [
                {
                    "node_name": "erin",
                    "label": "Erin",
                    "node_api_public_base_url": "http://127.0.0.1:8083",
                },
                {
                    "node_name": "yuki",
                    "label": "Yuki",
                    "node_api_public_base_url": "http://127.0.0.1:8082",
                },
            ]
        )

        with (
            patch.object(config, "INDEV", True),
            patch.object(
                config,
                "env_opt",
                side_effect=lambda name: dev_cluster_payload if name == "DEV_CLUSTER_NODE_LINKS_JSON" else None,
            ),
        ):
            self.assertEqual(ModWebService()._portal_default_node_name(), "yuki")

    def test_mod_web_gzip_middleware_skips_binary_proxy_paths(self) -> None:
        self.assertTrue(
            _ModWebGZipMiddleware._should_skip_compression(
                "/api/node-proxy/yuki/apps/minecraft_alpha/map/worlds/world/tiles/0/0_0.png"
            )
        )
        self.assertTrue(
            _ModWebGZipMiddleware._should_skip_compression(
                "/api/node/apps/minecraft_alpha/mods/download"
            )
        )
        self.assertTrue(_ModWebGZipMiddleware._should_skip_compression("/mod-web/assets/fonts/test.woff2"))
        self.assertFalse(_ModWebGZipMiddleware._should_skip_compression("/mod-web/assets/theme.css"))

    def test_app_links_include_dedicated_chat_link_for_local_chat_relay_apps(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        entry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="yuki",
            running=False,
            enabled=True,
            supports_mods=False,
            supports_configs=False,
            supports_chat=True,
            color_hex="#22C55E",
        )

        with patch.object(ModWebService, "_remote_apps_async", AsyncMock(return_value=(entry,))):
            links: tuple[ModWebAppLink, ...] = asyncio.run(service._app_links(user))

        self.assertEqual(len(links), 1)
        self.assertTrue(links[0].enabled)
        self.assertEqual(links[0].color_hex, "#22C55E")
        self.assertTrue(links[0].supports_chat)
        self.assertEqual(links[0].chat_url, "/mod-web/nodes/yuki/chat/minecraft_alpha")
        self.assertIsNone(links[0].player_count)

    def test_app_links_include_player_count_for_running_local_apps(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        entry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="yuki",
            running=True,
            enabled=True,
            supports_mods=False,
            supports_configs=False,
            supports_chat=True,
            color_hex="#22C55E",
            player_count=4,
            player_capacity=12,
        )

        with patch.object(ModWebService, "_remote_apps_async", AsyncMock(return_value=(entry,))):
            links: tuple[ModWebAppLink, ...] = asyncio.run(service._app_links(user))

        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].player_count, 4)
        self.assertEqual(links[0].player_capacity, 12)

    def test_app_links_resolve_missing_remote_app_color_from_scope(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        entry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="yuki",
            running=False,
            enabled=True,
            supports_mods=False,
            supports_configs=False,
            supports_chat=True,
            color_hex=None,
            scope="minecraft",
        )

        with patch.object(ModWebService, "_remote_apps_async", AsyncMock(return_value=(entry,))):
            links: tuple[ModWebAppLink, ...] = asyncio.run(service._app_links(user))

        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].color_hex, "#22C55E")

    def test_remote_apps_from_payload_replaces_generic_default_app_color(self) -> None:
        payload: dict[str, object] = {
            "apps": [
                {
                    "name": "satisfactory_prime",
                    "friendly": "Satisfactory Prime",
                    "node": "erin",
                    "running": False,
                    "enabled": True,
                    "supports_mods": False,
                    "supports_configs": False,
                    "supports_chat": False,
                    "scope": "satisfactory",
                    "color_hex": "#96212B",
                }
            ]
        }

        entries = ModWebService._remote_apps_from_payload(payload)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].color_hex, "#F59E0B")

    def test_remote_app_links_include_dedicated_chat_link_for_remote_chat_relay_apps(self) -> None:
        service: ModWebService = ModWebService()
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        entry: NodeAppEntry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="erin",
            running=False,
            enabled=False,
            supports_mods=True,
            supports_configs=False,
            supports_console_actions=True,
            supports_chat=True,
            color_hex="#DC2626",
        )

        with patch.object(ModWebService, "_remote_apps_async", AsyncMock(return_value=(entry,))) as remote_apps:
            links: tuple[ModWebAppLink, ...] = asyncio.run(service._remote_app_links(node, user))

        self.assertEqual(len(links), 1)
        self.assertFalse(links[0].enabled)
        self.assertEqual(links[0].color_hex, "#DC2626")
        self.assertTrue(links[0].supports_console_actions)
        self.assertTrue(links[0].supports_chat)
        self.assertEqual(links[0].chat_url, "/mod-web/nodes/erin/chat/minecraft_alpha")
        self.assertIsNone(links[0].player_count)
        remote_apps.assert_awaited_once_with(
            node,
            user,
            timeout=_REMOTE_NODE_OVERVIEW_REQUEST_TIMEOUT_SECONDS,
        )

    def test_remote_apps_uses_standard_request_timeout(self) -> None:
        service: ModWebService = ModWebService()
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with patch.object(
            ModWebService,
            "_remote_json_async",
            new=AsyncMock(return_value={"apps": []}),
        ) as remote_json:
            self.assertEqual(asyncio.run(service._remote_apps_async(node, user)), ())

        remote_json.assert_awaited_once_with(
            node=node,
            app_name=None,
            path="/apps",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
            timeout=15.0,
        )

    def test_remote_app_entry_uses_app_specific_endpoint(self) -> None:
        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        expected = NodeAppEntry(
            name="sevendays_alpha",
            friendly="7D2D Alpha",
            node="erin",
            running=True,
            enabled=True,
            supports_mods=True,
            supports_configs=True,
            client_pack_published_version="2026-07-04",
            client_pack_published_changelog="Added client performance fixes.",
            client_pack_releases=(
                ClientPackRelease(
                    version="2026-07-04",
                    changelog="Added client performance fixes.",
                ),
            ),
        )
        legacy_payload = expected.to_mapping()
        del legacy_payload["client_pack_releases"]

        with patch.object(
            service,
            "_remote_json_async",
            new=AsyncMock(return_value=legacy_payload),
        ) as remote_json:
            entry = asyncio.run(service._remote_app_entry_async(node, expected.name, user))

        self.assertEqual(entry, replace(expected, color_hex="#B91C1C"))
        remote_json.assert_awaited_once_with(
            node=node,
            app_name=expected.name,
            path="/apps/sevendays_alpha",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
        )

    def test_remote_json_async_retries_transient_get_timeout(self) -> None:
        class TimeoutContext:
            async def __aenter__(self) -> object:
                raise aiohttp.SocketTimeoutError("slow response")

            async def __aexit__(self, *_args: object) -> None:
                return None

        class SuccessResponse:
            status = 200

            async def json(self) -> dict[str, object]:
                return {"apps": []}

        class SuccessContext:
            async def __aenter__(self) -> SuccessResponse:
                return SuccessResponse()

            async def __aexit__(self, *_args: object) -> None:
                return None

        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        session = Mock()
        session.get.side_effect = (TimeoutContext(), SuccessContext())

        with (
            patch.object(service, "_remote_token", return_value="test-token"),
            patch.object(service, "_remote_http_client", new=AsyncMock(return_value=session)),
            patch("web_dash.models.asyncio.sleep", new=AsyncMock()) as retry_sleep,
        ):
            payload = asyncio.run(
                service._remote_json_async(
                    node=node,
                    app_name=None,
                    path="/apps",
                    scopes=(NodeApiScope.APPS_READ,),
                    user=user,
                )
            )

        self.assertEqual(payload, {"apps": []})
        self.assertEqual(session.get.call_count, 2)
        retry_sleep.assert_awaited_once()

    def test_remote_json_async_opens_circuit_after_exhausted_get_timeout(self) -> None:
        class TimeoutContext:
            async def __aenter__(self) -> object:
                raise aiohttp.SocketTimeoutError("slow response")

            async def __aexit__(self, *_args: object) -> None:
                return None

        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        session = Mock()
        session.get.side_effect = (TimeoutContext(), TimeoutContext())

        with (
            patch.object(service, "_remote_token", return_value="test-token"),
            patch.object(service, "_remote_http_client", new=AsyncMock(return_value=session)),
            patch("web_dash.models.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "Remote node request failed"):
                asyncio.run(
                    service._remote_json_async(
                        node=node,
                        app_name=None,
                        path="/apps",
                        scopes=(NodeApiScope.APPS_READ,),
                        user=user,
                    )
                )
            with self.assertRaises(RemoteNodeCircuitOpenError):
                asyncio.run(
                    service._remote_json_async(
                        node=node,
                        app_name=None,
                        path="/apps",
                        scopes=(NodeApiScope.APPS_READ,),
                        user=user,
                    )
                )

        self.assertEqual(session.get.call_count, 2)

    def test_remote_node_system_summary_or_none_async_returns_none_on_failure(self) -> None:
        async def exercise() -> None:
            service = ModWebService()
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            user = cast(Any, SimpleNamespace(discord_id=42))
            service._remote_node_system_summary_async = AsyncMock(side_effect=RuntimeError("down"))  # type: ignore[method-assign]

            summary = await service._remote_node_system_summary_or_none_async(
                node,
                user,
                error_context="Remote mod web node system summary failed",
            )

            self.assertIsNone(summary)

        asyncio.run(exercise())

    def test_remote_page_model_uses_same_origin_api_urls_for_current_node(self) -> None:
        service = ModWebService()
        current_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        mods = NodeModList(
            app_name="minecraft alpha",
            app_friendly="Minecraft Alpha",
            node="yuki",
            summary=NodeModSummary(
                total_count=1,
                enabled_count=1,
                disabled_count=0,
                coremod_count=0,
                downloadable_count=1,
                non_downloadable_count=0,
            ),
            mods=(self._mod_entry(name="Some Mod+1.0.jar"),),
            app_stats=None,
        )

        model = service._remote_page_model(
            node=current_node,
            mods=mods,
            app_scope="minecraft",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=True,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            blueprints=None,
            settings=None,
            console_actions=None,
            map_url="https://example.invalid/map",
            can_write_map_annotations=True,
            supports_chat=True,
            supports_updates=False,
            chat_url="/mod-web/chat/minecraft_alpha",
            update_info=None,
            update_status=None,
            app_start_blocked=False,
            app_color_hex="#22C55E",
            resource_points=None,
            app_title_font_preset=AppTitleFont.AUTO.value,
            app_notes=None,
            join_address="play.example.test:25565",
            join_direct_ip_address="203.0.113.10:25565",
            lifecycle_notice_started=True,
            lifecycle_notice_stopped=True,
            lifecycle_notice_crashed=True,
        )

        self.assertEqual(model.map_api_url, "/api/node/apps/minecraft%20alpha/map")
        self.assertEqual(model.join_address, "play.example.test:25565")
        self.assertEqual(model.join_direct_ip_address, "203.0.113.10:25565")
        self.assertEqual(model.download_all_url, "/api/node/apps/minecraft%20alpha/mods/download?enabled_only=false")
        self.assertEqual(
            model.download_enabled_url,
            "/api/node/apps/minecraft%20alpha/mods/download?enabled_only=true",
        )
        self.assertEqual(
            model.mod_download_urls["Some Mod+1.0.jar"],
            "/api/node/apps/minecraft%20alpha/mods/Some%20Mod%2B1.0.jar/download",
        )

    def test_build_local_app_page_data_uses_mod_list_when_app_entry_supports_mods(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        app = cast(Any, SimpleNamespace(name="minecraft_alpha"))
        app_entry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="yuki",
            running=True,
            enabled=True,
            supports_mods=True,
            supports_configs=False,
        )
        mod_list = NodeModList(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="yuki",
            summary=NodeModSummary(
                total_count=1,
                enabled_count=1,
                disabled_count=0,
                coremod_count=0,
                downloadable_count=1,
                non_downloadable_count=0,
            ),
            mods=(self._mod_entry(name="server.jar"),),
            app_stats=NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version=None,
                player_count=1,
                player_capacity=8,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
        )

        with (
            patch.object(service._node_api, "build_app_entry", return_value=app_entry),
            patch.object(service._node_api, "build_mod_list", new=AsyncMock(return_value=mod_list)) as build_mod_list,
            patch.object(
                service._node_api,
                "build_app_runtime_summary",
                new=AsyncMock(return_value=NodeAppRuntimeSummary(
                    running=False,
                    enabled=True,
                    version=None,
                    player_count=None,
                    player_capacity=None,
                    relay_support=ChatRelaySupport.NONE,
                    storage_percent=None,
                    storage_free_bytes=None,
                    storage_total_bytes=None,
                )),
            ) as build_app_runtime_summary,
        ):
            page_data = asyncio.run(service._build_local_app_page_data(app, user=user))

        self.assertIs(page_data.app_entry, app_entry)
        self.assertIs(page_data.mods, mod_list)
        self.assertEqual(page_data.app_stats, mod_list.app_stats)
        build_mod_list.assert_awaited_once_with(app)
        build_app_runtime_summary.assert_not_awaited()

    def test_build_local_app_page_data_uses_runtime_summary_when_app_entry_has_no_mods(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        app = cast(Any, SimpleNamespace(name="factorio_alpha"))
        app_entry = NodeAppEntry(
            name="factorio_alpha",
            friendly="Factorio Alpha",
            node="yuki",
            running=False,
            enabled=True,
            supports_mods=False,
            supports_configs=False,
        )
        runtime_summary = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version=None,
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )

        with (
            patch.object(service._node_api, "build_app_entry", return_value=app_entry),
            patch.object(service._node_api, "build_mod_list", new=AsyncMock()) as build_mod_list,
            patch.object(
                service._node_api,
                "build_app_runtime_summary",
                new=AsyncMock(return_value=runtime_summary),
            ) as build_app_runtime_summary,
        ):
            page_data = asyncio.run(service._build_local_app_page_data(app, user=user))

        self.assertIs(page_data.app_entry, app_entry)
        self.assertIsNone(page_data.mods)
        self.assertEqual(page_data.app_stats, runtime_summary)
        build_mod_list.assert_not_awaited()
        build_app_runtime_summary.assert_awaited_once_with(app)

    def test_build_local_app_page_data_awaits_save_list_for_save_support(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        app = cast(Any, SimpleNamespace(name="satisfactory_alpha"))
        app_entry = NodeAppEntry(
            name="satisfactory_alpha",
            friendly="Satisfactory Alpha",
            node="yuki",
            running=True,
            enabled=True,
            supports_mods=False,
            supports_configs=False,
            supports_saves=True,
        )
        save_list = self._save_list(app_name="satisfactory_alpha")
        runtime_summary = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version=None,
            player_count=2,
            player_capacity=8,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service._node_api, "build_app_entry", return_value=app_entry),
            patch.object(service._node_api, "build_save_list", new=AsyncMock(return_value=save_list)) as build_save_list,
            patch.object(service._node_api, "build_mod_list", new=AsyncMock()) as build_mod_list,
            patch.object(
                service._node_api,
                "build_app_runtime_summary",
                new=AsyncMock(return_value=runtime_summary),
            ) as build_app_runtime_summary,
        ):
            page_data = asyncio.run(service._build_local_app_page_data(app, user=user))

        self.assertIs(page_data.saves, save_list)
        build_save_list.assert_awaited_once_with(app)
        build_mod_list.assert_not_awaited()
        build_app_runtime_summary.assert_awaited_once_with(app)

    def test_build_local_app_page_data_keeps_page_available_when_save_list_fails(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        app = cast(Any, SimpleNamespace(name="satisfactory_alpha"))
        app_entry = NodeAppEntry(
            name="satisfactory_alpha",
            friendly="Satisfactory Alpha",
            node="yuki",
            running=True,
            enabled=True,
            supports_mods=False,
            supports_configs=False,
            supports_saves=True,
            supports_blueprints=True,
        )
        runtime_summary = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version=None,
            player_count=2,
            player_capacity=8,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )
        empty_save_list = self._save_list(app_name="satisfactory_alpha")
        empty_blueprint_list = NodeBlueprintList(
            app_name="satisfactory_alpha",
            app_friendly="Satisfactory Alpha",
            node="yuki",
            blueprints=(),
            default_session_name="Session Alpha",
        )

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service._node_api, "build_app_entry", return_value=app_entry),
            patch.object(
                service._node_api,
                "build_save_list",
                new=AsyncMock(side_effect=RuntimeError("Satisfactory API is unavailable.")),
            ) as build_save_list,
            patch.object(service._node_api, "build_empty_save_list", return_value=empty_save_list),
            patch.object(
                service._node_api,
                "build_blueprint_list",
                side_effect=RuntimeError("Blueprint directory is unavailable."),
            ) as build_blueprint_list,
            patch.object(
                service._node_api,
                "build_empty_blueprint_list",
                return_value=empty_blueprint_list,
            ),
            patch.object(service._node_api, "build_mod_list", new=AsyncMock()) as build_mod_list,
            patch.object(
                service._node_api,
                "build_app_runtime_summary",
                new=AsyncMock(return_value=runtime_summary),
            ) as build_app_runtime_summary,
        ):
            page_data = asyncio.run(service._build_local_app_page_data(app, user=user))

        self.assertIs(page_data.saves, empty_save_list)
        self.assertIs(page_data.blueprints, empty_blueprint_list)
        self.assertEqual(page_data.app_stats, runtime_summary)
        self.assertEqual(
            page_data.load_warnings,
            (
                ModWebPageLoadWarning(title="Saves unavailable", detail="Satisfactory API is unavailable."),
                ModWebPageLoadWarning(title="Blueprints unavailable", detail="Blueprint directory is unavailable."),
            ),
        )
        build_save_list.assert_awaited_once_with(app)
        build_blueprint_list.assert_called_once_with(app, actor_user_id=user.discord_id)
        build_mod_list.assert_not_awaited()
        build_app_runtime_summary.assert_awaited_once_with(app)

    def test_safe_remote_optional_page_section_returns_fallback_and_warning(self) -> None:
        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example.invalid",
            api_url="/mod-web/api/nodes/erin/apps",
            is_current=False,
            latency_probe_url="https://erin.example.invalid/ping",
            presence_stream_url="https://erin.example.invalid/presence/stream",
        )
        load_warnings: list[ModWebPageLoadWarning] = []

        async def unavailable_section() -> None:
            raise RuntimeError("Satisfactory API is unavailable.")

        result = asyncio.run(
            service._safe_remote_optional_page_section(
                node=node,
                app_name="satisfactory_alpha",
                section_label="Saves",
                fallback=None,
                load_warnings=load_warnings,
                operation=unavailable_section,
            )
        )

        self.assertIsNone(result)
        self.assertEqual(
            load_warnings,
            [ModWebPageLoadWarning(title="Saves unavailable", detail="Satisfactory API is unavailable.")],
        )

    def test_home_app_sections_format_remote_failures_for_people(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        local_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        remote_node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )

        try:
            raise RuntimeError("Remote node request failed: url=https://erin.example/api/node/apps") from (
                requests.ConnectionError("Connection refused")
            )
        except RuntimeError as xcp:
            remote_failure = xcp

        with (
            patch.object(ModWebService, "_node_links", return_value=(local_node, remote_node)),
            patch.object(ModWebService, "_remote_app_links", new=AsyncMock(side_effect=((), remote_failure))),
        ):
            sections = asyncio.run(service._home_app_sections(user))

        self.assertEqual(len(sections), 2)
        self.assertIsNone(sections[0].error)
        self.assertEqual(
            sections[1].error,
            "This node is unreachable right now. It may be offline or still waking up.",
        )

    def test_home_app_sections_format_local_failures_for_people(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        local_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )

        try:
            raise RuntimeError("Local node app listing failed")
        except RuntimeError as xcp:
            local_failure = xcp

        with (
            patch.object(ModWebService, "_node_links", return_value=(local_node,)),
            patch.object(ModWebService, "_remote_app_links", new=AsyncMock(side_effect=local_failure)),
        ):
            sections = asyncio.run(service._home_app_sections(user))

        self.assertEqual(
            sections,
            (
                ModWebNodeAppSection(
                    node=local_node,
                    app_links=(),
                    error="Yuki could not talk to this node right now. Refresh to try again in a moment.",
                ),
            ),
        )

    def test_home_app_sections_short_circuit_simulated_remote_nodes(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        local_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        remote_node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )

        with (
            patch.object(ModWebService, "_node_links", return_value=(local_node, remote_node)),
            patch.object(ModWebService, "_remote_app_links", new=AsyncMock()) as remote_app_links,
        ):
            sections = asyncio.run(service._home_app_sections(user, simulated_down_node_names=("erin",)))

        self.assertEqual(len(sections), 2)
        self.assertEqual(
            sections[1],
            ModWebNodeAppSection(
                node=remote_node,
                app_links=(),
                error="This node is being simulated as unavailable in dev mode.",
                is_simulated_down=True,
            ),
        )
        remote_app_links.assert_awaited_once_with(
            local_node,
            user,
            timeout=_REMOTE_NODE_OVERVIEW_REQUEST_TIMEOUT_SECONDS,
        )

    def test_home_app_sections_short_circuit_simulated_current_node(self) -> None:
        service: ModWebService = ModWebService()
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        local_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )

        with (
            patch.object(ModWebService, "_node_links", return_value=(local_node,)),
            patch.object(ModWebService, "_remote_app_links", new=AsyncMock()) as remote_app_links,
        ):
            sections = asyncio.run(service._home_app_sections(user, simulated_down_node_names=("yuki",)))

        self.assertEqual(
            sections,
            (
                ModWebNodeAppSection(
                    node=local_node,
                    app_links=(),
                    error="This node is being simulated as unavailable in dev mode.",
                    is_simulated_down=True,
                ),
            ),
        )
        remote_app_links.assert_not_called()

    def test_home_app_card_target_uses_app_page_for_visitors(self) -> None:
        service: ModWebService = ModWebService()
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text('{"visitor": [42]}')
            service.set_acl(Access_Control(pointer))

        app: ModWebAppLink = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            supports_chat=True,
            chat_url="/mod-web/chat/minecraft_alpha",
        )
        user: ModWebUser = ModWebUser(discord_id=42, username="visitor", global_name=None, avatar_hash=None)

        target = service._home_app_card_target(app=app, user=user, show_api_actions=False)

        self.assertEqual(target, "/mod-web/mods/minecraft_alpha")

    def test_render_home_page_sections_wraps_nodes_in_section_grid(self) -> None:
        class FakeContainer:
            def __init__(self, *, kind: str, ui: "FakeUi") -> None:
                self.kind = kind
                self.ui = ui
                self.class_value: str | None = None
                self.style_value: str | None = None
                self.events: list[str] = []
                self.props_values: list[str] = []

            def classes(self, value: str) -> "FakeContainer":
                self.class_value = value
                return self

            def style(self, value: str) -> "FakeContainer":
                self.style_value = value
                return self

            def props(self, value: str) -> "FakeContainer":
                self.props_values.append(value)
                return self

            def on(
                self,
                event_name: str,
                handler: object,
                *,
                js_handler: str | None = None,
            ) -> "FakeContainer":
                del handler, js_handler
                self.events.append(event_name)
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text
                self.class_value: str | None = None
                self.style_value: str | None = None

            def classes(self, value: str) -> "FakeLabel":
                self.class_value = value
                return self

            def style(self, value: str) -> "FakeLabel":
                self.style_value = value
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.elements: list[FakeContainer] = []
                self.cards: list[FakeContainer] = []
                self.labels: list[FakeLabel] = []
                self.html_fragments: list[str] = []
                self.navigate = SimpleNamespace(to=lambda target_url: target_url)

            def column(self) -> FakeContainer:
                container = FakeContainer(kind="column", ui=self)
                self.elements.append(container)
                return container

            def row(self) -> FakeContainer:
                container = FakeContainer(kind="row", ui=self)
                self.elements.append(container)
                return container

            def element(self, tag: str) -> FakeContainer:
                container = FakeContainer(kind=tag, ui=self)
                self.elements.append(container)
                return container

            def icon(self, name: str) -> FakeContainer:
                container = FakeContainer(kind=f"icon:{name}", ui=self)
                self.elements.append(container)
                return container

            def card(self) -> FakeContainer:
                card = FakeContainer(kind="card", ui=self)
                self.cards.append(card)
                self.elements.append(card)
                return card

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def html(self, content: str) -> FakeContainer:
                self.html_fragments.append(content)
                container = FakeContainer(kind="html", ui=self)
                self.elements.append(container)
                return container

            def tooltip(self, text: str) -> None:
                del text

        service = ModWebService()
        ui = FakeUi()
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/node/yuki",
            api_base_url="https://example.invalid/api",
            api_url="https://example.invalid/api/node/apps",
            is_current=True,
        )
        apps = (
            ModWebAppLink(
                name="minecraft_alpha",
                friendly="Minecraft Alpha",
                node_name="yuki",
                running=False,
                enabled=True,
                color_hex="#22C55E",
                supports_mods=True,
                supports_configs=False,
                supports_saves=False,
                supports_settings=False,
                url="/mod-web/mods/minecraft_alpha",
                api_url=None,
                configs_api_url=None,
            ),
            ModWebAppLink(
                name="factorio_alpha",
                friendly="Factorio Alpha",
                node_name="yuki",
                running=True,
                enabled=True,
                color_hex="#F97316",
                supports_mods=False,
                supports_configs=True,
                supports_saves=True,
                supports_settings=False,
                url="/mod-web/mods/factorio_alpha",
                api_url=None,
                configs_api_url="/api/node/apps/factorio_alpha/configs",
            ),
        )
        section = ModWebNodeAppSection(node=node, app_links=apps)
        node_summary = ModWebHomeNodeSummary(
            node=node,
            app_count=len(apps),
            system_summary=NodeSystemSummary(
                cpu_percent=20,
                ram_percent=30,
                ram_used_bytes=3,
                ram_total_bytes=10,
                storage_percent=40,
                storage_free_bytes=20,
                storage_total_bytes=30,
                cpu_points_available=4,
                cpu_points_capacity=6,
                ram_points_available=5,
                ram_points_capacity=8,
            ),
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with patch.object(ModWebService, "_render_app_card_content") as render_app_card_content:
            service._render_home_page_sections(
                ui=cast(ModWebUi, cast(object, ui)),
                sections=(section,),
                node_summaries=(node_summary,),
                user=user,
                show_api_actions=False,
            )

        self.assertIn(
            "mod-home-section-grid w-full", [element.class_value for element in ui.elements if element.kind == "div"]
        )
        self.assertIn(
            "mod-home-section w-full gap-3",
            [element.class_value for element in ui.elements if element.kind == "column"],
        )
        self.assertEqual(len(ui.cards), 2)
        self.assertTrue(all("role=link tabindex=0" in card.props_values for card in ui.cards))
        self.assertTrue(all({"click", "keydown.enter", "keydown.space"}.issubset(card.events) for card in ui.cards))
        self.assertEqual(render_app_card_content.call_count, 2)
        self.assertIn("4/6", [label.text for label in ui.labels])
        self.assertIn("5/8", [label.text for label in ui.labels])
        self.assertIn("icon:speed", [element.kind for element in ui.elements])
        self.assertIn("icon:memory", [element.kind for element in ui.elements])
        self.assertEqual(len(ui.html_fragments), 1)
        self.assertIn('class="mod-user-avatar mod-home-section-avatar"', ui.html_fragments[0])

    def test_node_resource_point_badges_show_available_capacity(self) -> None:
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        badges = ModWebService._node_resource_point_badges(
            ModWebHomeNodeSummary(
                node=node,
                app_count=3,
                system_summary=NodeSystemSummary(
                    cpu_percent=20,
                    ram_percent=30,
                    ram_used_bytes=3,
                    ram_total_bytes=10,
                    storage_percent=40,
                    storage_free_bytes=20,
                    storage_total_bytes=30,
                    cpu_points_available=6,
                    cpu_points_capacity=12,
                    ram_points_available=2,
                    ram_points_capacity=8,
                ),
            )
        )

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="6/12", tone="black", icon="speed", tooltip_text="CPU"),
                _ModWebBadgeSpec(text="2/8", tone="warn", icon="memory", tooltip_text="RAM"),
            ),
        )

    def test_app_resource_point_badges_show_startup_override_only_when_different(self) -> None:
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            resource_points=NodeAppResourcePointSummary(
                cpu_points_running=3,
                cpu_points_startup=5,
                ram_points_running=8,
                ram_points_startup=8,
            ),
        )

        badges = ModWebService._app_resource_point_badges(model)

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(
                    text="3 (5)",
                    tone="black",
                    icon="speed",
                    tooltip_text="CPU points required for running (starting)",
                ),
                _ModWebBadgeSpec(
                    text="8",
                    tone="black",
                    icon="memory",
                    tooltip_text="RAM points required for running",
                ),
            ),
        )

    def test_app_card_badges_follow_standard_order(self) -> None:
        service = ModWebService()
        app: ModWebAppLink = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=True,
            supports_saves=True,
            supports_settings=True,
            url="/mod-web/mods/minecraft_alpha",
            api_url="/api/node/apps/minecraft_alpha/mods",
            configs_api_url="/api/node/apps/minecraft_alpha/configs",
            saves_api_url="/api/node/apps/minecraft_alpha/saves",
            settings_api_url="/api/node/apps/minecraft_alpha/settings",
            supports_console_actions=True,
            supports_chat=True,
            chat_url="/mod-web/chat/minecraft_alpha",
        )

        badges = service._app_card_badges(app)

        self.assertEqual(
            [badge.text for badge in badges],
            ["Saves", "Configs", "Settings", "Console", "Mods", "Chat"],
        )
        self.assertEqual(
            [badge.tone for badge in badges],
            ["grey", "grey", "grey", "grey", "purple", "purple"],
        )
        self.assertEqual(
            [badge.tab_id for badge in badges],
            [
                "saves",
                "configs",
                "settings",
                "console",
                "mods",
                "chat",
            ],
        )

    def test_hidden_app_tabs_can_contribute_home_app_card_badges(self) -> None:
        class HiddenTabBadgeService(ModWebService):
            def _additional_app_tab_definitions(
                self,
                *,
                context: ModWebAppTabContext,
                is_detail_page: bool,
            ) -> tuple[ModWebAppTabDefinition, ...]:
                del is_detail_page
                if context.app_name != "minecraft_alpha":
                    return ()
                return (
                    ModWebAppTabDefinition.custom(
                        tab_id="map",
                        label="Map",
                        page_order=650,
                        app_card_order=650,
                        app_card_tone="purple",
                        show_on_app_card=False,
                        render_handler_name="_render_map_tab",
                        app_card_badge_handler_name="_map_app_card_badges",
                    ),
                )

            @staticmethod
            def _render_map_tab(
                *,
                ui: ModWebUi,
                model: ModWebBasePageModel,
                user: ModWebUser,
                tab: ModWebAppTabDefinition,
            ) -> None:
                del ui, model, user, tab
                return None

            @staticmethod
            def _map_app_card_badges(
                *,
                app: ModWebAppLink,
                tab: ModWebAppTabDefinition,
            ) -> tuple[_ModWebAppCardBadgeSpec, ...]:
                del app
                return (_ModWebAppCardBadgeSpec(text="Live Map", tone="purple", tab_id=tab.tab_id),)

        service = HiddenTabBadgeService()
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url="/api/node/apps/minecraft_alpha/mods",
            configs_api_url=None,
        )

        badges = service._app_card_badges(app)

        self.assertEqual([badge.text for badge in badges], ["Mods", "Live Map"])
        self.assertEqual([badge.tab_id for badge in badges], ["mods", "map"])

    def test_blueprint_hidden_tab_contributes_home_app_card_badge(self) -> None:
        service = ModWebService()
        app = ModWebAppLink(
            name="satisfactory_alpha",
            friendly="Satisfactory Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#F59E0B",
            supports_mods=False,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/satisfactory_alpha",
            api_url=None,
            configs_api_url=None,
            supports_blueprints=True,
        )

        badges = service._app_card_badges(app)

        self.assertEqual([badge.text for badge in badges], ["Blueprints"])
        self.assertEqual([badge.tab_id for badge in badges], ["blueprints"])
        self.assertEqual([badge.tone for badge in badges], ["grey"])

    def test_app_card_badge_target_preserves_app_list_query_and_sets_requested_tab(self) -> None:
        service: ModWebService = ModWebService()
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=True,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha?view=compact",
            api_url=None,
            configs_api_url=None,
            supports_chat=False,
            chat_url=None,
        )

        target = service._app_card_badge_target(
            app=app,
            badge=_ModWebAppCardBadgeSpec(
                text="Configs",
                tone="black",
                tab_id="configs",
            ),
            show_api_actions=True,
        )

        self.assertEqual(target, "/mod-web/mods/minecraft_alpha?view=compact&dev_api=1&tab=configs")

    def test_app_card_badge_target_routes_chat_badges_to_the_chat_tab(self) -> None:
        service: ModWebService = ModWebService()
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            supports_chat=True,
            chat_url="/mod-web/chat/minecraft_alpha",
        )

        target = service._app_card_badge_target(
            app=app,
            badge=_ModWebAppCardBadgeSpec(
                text="Chat",
                tone="purple",
                tab_id="chat",
            ),
            show_api_actions=False,
        )

        self.assertEqual(target, "/mod-web/mods/minecraft_alpha?tab=chat")

    def test_app_link_with_runtime_updates_dynamic_fields(self) -> None:
        service = ModWebService()
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=False,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            player_count=None,
            player_capacity=None,
            supports_chat=False,
            chat_url=None,
        )

        updated = service._app_link_with_runtime(
            app,
            NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version="1.21.1",
                player_count=5,
                player_capacity=20,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STARTING,
                connected_player_names=("Yoko", "Bea"),
            ),
        )

        self.assertTrue(updated.enabled)
        self.assertEqual(updated.transition_state, NodeAppTransitionState.STARTING)
        self.assertEqual(updated.player_count, 5)
        self.assertEqual(updated.player_capacity, 20)
        self.assertEqual(updated.connected_player_names, ("Yoko", "Bea"))
        self.assertEqual(updated.url, app.url)

    def test_app_card_link_classes_uses_starting_state_class(self) -> None:
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            transition_state=NodeAppTransitionState.STARTING,
            supports_chat=False,
            chat_url=None,
        )

        classes = ModWebService()._app_card_link_classes(app)

        self.assertIn("mod-app-card-starting", classes)
        self.assertNotIn("mod-app-card-running", classes)
        self.assertNotIn("mod-app-card-stopping", classes)

    def test_app_card_link_classes_uses_running_state_class_when_stable(self) -> None:
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=True,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            transition_state=NodeAppTransitionState.NONE,
            supports_chat=False,
            chat_url=None,
        )

        classes = ModWebService()._app_card_link_classes(app)

        self.assertIn("mod-app-card-running", classes)
        self.assertNotIn("mod-app-card-starting", classes)
        self.assertNotIn("mod-app-card-stopping", classes)

    def test_app_card_link_classes_use_crash_rail_when_runtime_fault_exists(self) -> None:
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            runtime_fault=AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH,
                summary="Failed to start the minecraft server",
            ),
            supports_chat=False,
            chat_url=None,
        )

        self.assertIn("mod-app-card-crashed", ModWebService()._app_card_link_classes(app))

    def test_app_card_link_classes_uses_stopping_state_class(self) -> None:
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=True,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/mods/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            transition_state=NodeAppTransitionState.STOPPING,
            supports_chat=False,
            chat_url=None,
        )

        classes = ModWebService()._app_card_link_classes(app)

        self.assertIn("mod-app-card-stopping", classes)
        self.assertNotIn("mod-app-card-starting", classes)
        self.assertNotIn("mod-app-card-running", classes)

    def test_app_hero_card_classes_use_starting_state_class(self) -> None:
        classes = ModWebService()._app_hero_card_classes(
            NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="1.21.1",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STARTING,
            )
        )

        self.assertIn("mod-app-hero-starting", classes)
        self.assertNotIn("mod-app-hero-running", classes)
        self.assertNotIn("mod-app-hero-stopping", classes)

    def test_app_hero_card_classes_use_running_state_class_when_stable(self) -> None:
        classes = ModWebService()._app_hero_card_classes(
            NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version="1.21.1",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.NONE,
            )
        )

        self.assertIn("mod-app-hero-running", classes)
        self.assertNotIn("mod-app-hero-starting", classes)
        self.assertNotIn("mod-app-hero-stopping", classes)

    def test_app_hero_card_classes_use_stopping_state_class(self) -> None:
        classes = ModWebService()._app_hero_card_classes(
            NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version="1.21.1",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STOPPING,
            )
        )

        self.assertIn("mod-app-hero-stopping", classes)
        self.assertNotIn("mod-app-hero-starting", classes)
        self.assertNotIn("mod-app-hero-running", classes)

    def test_render_live_app_hero_runtime_renders_initial_content_before_refresh_updates(self) -> None:
        class FakeContainer:
            def classes(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(
                self, value: str | None = None, *, add: str | None = None, remove: str | None = None
            ) -> "FakeContainer":
                del value, add, remove
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text
                self.class_value: str | None = None
                self.style_values: list[tuple[str, str]] = []

            def __enter__(self) -> "FakeLabel":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

            def classes(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
                replace: str | None = None,
            ) -> "FakeLabel":
                del remove
                self.class_value = replace if replace is not None else add if add is not None else value
                return self

            def set_text(self, text: str) -> None:
                self.text = text

            def style(
                self, value: str | None = None, *, add: str | None = None, remove: str | None = None
            ) -> "FakeLabel":
                if add is not None:
                    self.style_values.append(("add", add))
                if remove is not None:
                    self.style_values.append(("remove", remove))
                del value
                return self

            def update(self) -> None:
                return None

        class FakeTooltip:
            def __enter__(self) -> "FakeTooltip":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

            def update(self) -> None:
                return None

        class FakeHtml:
            def __init__(self, content: str) -> None:
                self.content = content
                self.class_value: str | None = None
                self.style_values: list[tuple[str, str]] = []

            def __enter__(self) -> "FakeHtml":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeHtml":
                self.class_value = replace if replace is not None else value
                return self

            def style(
                self, value: str | None = None, *, add: str | None = None, remove: str | None = None
            ) -> "FakeHtml":
                if add is not None:
                    self.style_values.append(("add", add))
                if remove is not None:
                    self.style_values.append(("remove", remove))
                del value
                return self

            def set_content(self, content: str) -> None:
                self.content = content

            def update(self) -> None:
                return None

        class FakeCard:
            replaced_classes: str | None = None

            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeCard":
                del value
                self.replaced_classes = replace
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.label_texts: list[str] = []
                self.labels: list[FakeLabel] = []
                self.html_contents: list[str] = []
                self.html_elements: list[FakeHtml] = []

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def label(self, text: str) -> FakeLabel:
                self.label_texts.append(text)
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def tooltip(self) -> FakeTooltip:
                return FakeTooltip()

            def html(self, content: str) -> FakeHtml:
                self.html_contents.append(content)
                html = FakeHtml(content)
                self.html_elements.append(html)
                return html

        service = ModWebService()
        ui = FakeUi()
        hero_card = FakeCard()
        activity_providers = (
            NodeAppActivityProviderEntry(provider_id="day", label="Day Counter", enabled=True),
        )
        initial_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.21.1",
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            transition_state=NodeAppTransitionState.NONE,
            activity_providers=(
                NodeAppActivityProviderEntry(provider_id="day", label="Day Counter", enabled=True, current_value="D2"),
            ),
        )
        updated_stats = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version="1.21.1",
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            transition_state=NodeAppTransitionState.STARTING,
            activity_providers=(
                NodeAppActivityProviderEntry(provider_id="day", label="Day Counter", enabled=True, current_value="D3"),
            ),
        )

        apply_runtime = service._render_live_app_hero_runtime(
            ui=cast(ModWebUi, cast(object, ui)),
            hero_card=cast(Any, hero_card),
            app_name="minecraft_alpha",
            title="Minecraft Alpha",
            title_font_preset=AppTitleFont.DEFAULT,
            join_address="play.example.test:25565",
            join_direct_ip_address="203.0.113.10:25565",
            activity_providers=activity_providers,
            initial_app_stats=initial_stats,
        )

        self.assertEqual(hero_card.replaced_classes, "mod-card mod-card-hero w-full mod-app-hero-running")
        self.assertEqual(ui.label_texts[:2], ["Minecraft Alpha", "Running"])
        self.assertEqual(
            ui.label_texts,
            [
                "Minecraft Alpha",
                "Running",
                "play.example.test:25565",
                "203.0.113.10:25565",
                "",
            ],
        )
        self.assertEqual(ui.labels[2].class_value, "mod-app-hero-join-address")
        self.assertEqual(ui.labels[3].class_value, "mod-app-hero-join-address-direct")
        self.assertEqual(ui.html_contents, ["", "Day 2", "Day Counter<br>Current day: 2"])

        apply_runtime(updated_stats)

        self.assertEqual(hero_card.replaced_classes, "mod-card mod-card-hero w-full mod-app-hero-starting")
        self.assertEqual(
            ui.label_texts,
            [
                "Minecraft Alpha",
                "Running",
                "play.example.test:25565",
                "203.0.113.10:25565",
                "",
            ],
        )
        self.assertEqual(ui.html_elements[1].content, "Day 2")
        self.assertEqual(ui.html_elements[2].content, "")
        self.assertEqual(ui.labels[1].text, "Starting")

    def test_render_page_disables_hero_runtime_polling_when_live_app_updates_are_subscribed(self) -> None:
        class FakeContainer:
            def classes(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(self, value: str) -> "FakeContainer":
                del value
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeUi:
            def column(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        model = cast(
            ModWebPageModel,
            cast(
                object,
                SimpleNamespace(
                    supports_chat=False,
                    app_stats=None,
                    app_color_hex="#22C55E",
                    app_name="minecraft_alpha",
                    app_friendly="Minecraft Alpha",
                    app_title_font_preset=AppTitleFont.DEFAULT,
                    join_address=None,
                    join_direct_ip_address=None,
                    activity_providers=(),
                    node_name="yuki",
                ),
            ),
        )
        ui = FakeUi()
        subscribe_updates = Mock(return_value=lambda: None)

        with (
            patch.object(ModWebService, "_apply_theme"),
            patch.object(ModWebService, "_render_user_header"),
            patch.object(
                ModWebService,
                "_render_app_hero_corner_badges",
                return_value=SimpleNamespace(apply_node_summary=lambda *_args: None, apply_app_stats=lambda *_args: None),
            ),
            patch.object(ModWebService, "_app_page_hero_badges", return_value=()),
            patch.object(ModWebService, "_render_live_app_hero_runtime", return_value=lambda *_args: None) as render_hero,
            patch.object(
                ModWebService,
                "_render_global_app_toolbar",
                return_value=SimpleNamespace(apply_runtime_model=None),
            ),
            patch.object(ModWebService, "_page_tabs", return_value=()),
            patch.object(ModWebService, "_render_tabbed_page_sections", return_value=None),
            patch.object(ModWebService, "_register_client_cleanup"),
            patch("web_dash.app_page.asyncio.get_running_loop", return_value=cast(Any, object())),
        ):
            service._render_page(
                ui=cast(ModWebUi, cast(object, ui)),
                model=model,
                user=user,
                current_url="/mod-web/mods/minecraft_alpha",
                refresh_async_app_stats=AsyncMock(return_value=None),
                refresh_async_runtime_model=AsyncMock(return_value=model),
                subscribe_app_state_updates=subscribe_updates,
            )

        self.assertIsNone(render_hero.call_args.kwargs["refresh_async_app_stats"])
        subscribe_updates.assert_called_once()

    def test_app_card_badges_omit_no_mods_placeholder(self) -> None:
        service = ModWebService()
        app = ModWebAppLink(
            name="factorio",
            friendly="Factorio",
            node_name="erin",
            running=False,
            enabled=False,
            color_hex="#DC2626",
            supports_mods=False,
            supports_configs=True,
            supports_saves=True,
            supports_settings=False,
            url="/mod-web/nodes/erin/mods/factorio",
            api_url=None,
            configs_api_url="/api/node-proxy/erin/apps/factorio/configs",
            saves_api_url="/api/node-proxy/erin/apps/factorio/saves",
            settings_api_url=None,
            supports_chat=False,
            chat_url=None,
        )

        badges = service._app_card_badges(app)

        self.assertEqual([badge.text for badge in badges], ["Saves", "Configs"])
        self.assertNotIn("no mods", [badge.text.casefold() for badge in badges])

    def test_app_card_api_actions_group_supported_json_links(self) -> None:
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=True,
            supports_configs=True,
            supports_saves=True,
            supports_settings=True,
            url="/mod-web/mods/minecraft_alpha",
            api_url="/api/node/apps/minecraft_alpha/mods",
            configs_api_url="/api/node/apps/minecraft_alpha/configs",
            saves_api_url="/api/node/apps/minecraft_alpha/saves",
            settings_api_url="/api/node/apps/minecraft_alpha/settings",
            supports_chat=False,
            chat_url=None,
        )

        actions = ModWebService._app_card_api_actions(app)

        self.assertEqual([action.label for action in actions], ["Mods", "Configs", "Saves", "Settings"])

    def test_app_list_view_url_preserves_existing_query_and_toggle(self) -> None:
        enabled_url = ModWebService._app_list_view_url(
            "/mod-web/nodes/erin/chat/factorio?view=compact",
            show_api_actions=True,
        )
        disabled_url = ModWebService._app_list_view_url(enabled_url, show_api_actions=False)

        self.assertEqual(enabled_url, "/mod-web/nodes/erin/chat/factorio?view=compact&dev_api=1")
        self.assertEqual(disabled_url, "/mod-web/nodes/erin/chat/factorio?view=compact")

    def test_page_tab_url_preserves_existing_query_and_replaces_tab(self) -> None:
        updated_url = ModWebService._page_tab_url(
            "/mod-web/mods/minecraft_alpha?view=compact&tab=saves&search=alpha&dev_api=1",
            tab_id="configs",
        )
        same_tab_url = ModWebService._page_tab_url(
            "/mod-web/mods/minecraft_alpha?tab=saves&search=alpha",
            tab_id="saves",
        )

        self.assertEqual(updated_url, "/mod-web/mods/minecraft_alpha?view=compact&dev_api=1&tab=configs")
        self.assertEqual(same_tab_url, "/mod-web/mods/minecraft_alpha?search=alpha&tab=saves")

    def test_page_search_query_round_trips_through_browser_url(self) -> None:
        ui = Mock()

        search_query = ModWebService._initial_page_search_query(
            "/mod-web/mods/minecraft_alpha?tab=mods&search=alpha+fabric"
        )
        ModWebService._replace_browser_search_query(ui=cast(ModWebUi, ui), search_query=search_query)

        self.assertEqual(search_query, "alpha fabric")
        javascript = cast(str, ui.run_javascript.call_args.args[0])
        self.assertIn('url.searchParams.set("search", value)', javascript)
        self.assertIn('const value = "alpha fabric"', javascript)

    def test_empty_page_search_query_removes_browser_url_parameter(self) -> None:
        ui = Mock()

        ModWebService._replace_browser_search_query(ui=cast(ModWebUi, ui), search_query="  ")

        javascript = cast(str, ui.run_javascript.call_args.args[0])
        self.assertIn('url.searchParams.delete("search")', javascript)

    def test_page_mod_sort_order_round_trips_through_browser_url(self) -> None:
        ui = Mock()

        sort_order = ModWebService._initial_page_mod_sort_order(
            "/mod-web/mods/minecraft_alpha?tab=mods&mod_sort=size_descending"
        )
        ModWebService._replace_browser_mod_sort_order(ui=cast(ModWebUi, ui), order=sort_order)

        self.assertIs(sort_order, ModWebModSortOrder.SIZE_DESCENDING)
        javascript = cast(str, ui.run_javascript.call_args.args[0])
        self.assertIn('url.searchParams.set("mod_sort", value)', javascript)
        self.assertIn('const value = "size_descending"', javascript)
        self.assertIs(
            ModWebService._initial_page_mod_sort_order(
                "/mod-web/mods/minecraft_alpha?tab=mods&mod_sort=invalid"
            ),
            ModWebModSortOrder.NEWEST,
        )

    def test_toggle_simulated_down_node_url_preserves_existing_query(self) -> None:
        service = ModWebService()
        current_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        erin_node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        kousei_node = ModWebNodeLink(
            node_name="kousei",
            label="Kousei",
            url="/mod-web/nodes/kousei",
            api_base_url="https://kousei.example/api/node",
            api_url="/api/node-proxy/kousei/apps",
            is_current=False,
        )

        with patch.object(ModWebService, "_node_links", return_value=(current_node, erin_node, kousei_node)):
            enabled_url = service._toggle_simulated_down_node_url(
                current_url="/mod-web?view=compact&dev_api=1&dev_node_down=erin",
                node_name="kousei",
                simulated_down_node_names=("erin",),
            )
            disabled_url = service._toggle_simulated_down_node_url(
                current_url=enabled_url,
                node_name="erin",
                simulated_down_node_names=("erin", "kousei"),
            )

        self.assertEqual(
            enabled_url,
            "/mod-web?view=compact&dev_api=1&dev_node_down=erin&dev_node_down=kousei",
        )
        self.assertEqual(disabled_url, "/mod-web?view=compact&dev_api=1&dev_node_down=kousei")

    def test_simulated_down_node_names_include_current_and_ignore_unknown_nodes(self) -> None:
        service = ModWebService()
        current_node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/",
            api_base_url="/api/node",
            api_url="/api/node/apps",
            is_current=True,
        )
        erin_node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        kousei_node = ModWebNodeLink(
            node_name="kousei",
            label="Kousei",
            url="/mod-web/nodes/kousei",
            api_base_url="https://kousei.example/api/node",
            api_url="/api/node-proxy/kousei/apps",
            is_current=False,
        )
        request = SimpleNamespace(
            query_params=_FakeQueryParams(
                {"dev_node_down": (" ERIN ", "yuki", "unknown", "kousei")}
            )
        )

        with (
            patch.object(config, "INDEV", True),
            patch.object(ModWebService, "_node_links", return_value=(current_node, erin_node, kousei_node)),
        ):
            simulated_down_node_names = service._simulated_down_node_names(cast(Any, request))

        self.assertEqual(simulated_down_node_names, ("yuki", "erin", "kousei"))

    def test_chat_event_time_markup_uses_client_local_time_element(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="hello",
            created_at=123.0,
        )

        markup = ModWebService._chat_event_time_markup(event)

        self.assertIn('class="mod-chat-client-time"', markup)
        self.assertIn('data-mod-chat-unix="123"', markup)
        self.assertIn('data-mod-chat-time-style="T"', markup)
        self.assertIn(">00:02:03 UTC<", markup)

    def test_chat_author_color_uses_default_without_role_color(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="hello",
        )

        self.assertEqual(ModWebService._chat_author_color_hex(event), DEFAULT_CHAT_AUTHOR_COLOR_HEX)

    def test_chat_author_color_preserves_explicit_role_color(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Yoko", color_hex="#336699"),
            content="hello",
        )

        self.assertEqual(ModWebService._chat_author_color_hex(event), "#336699")

    def test_chat_author_avatar_uri_uses_safe_http_url(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(
                kind=ChatAuthorKind.GAME_PLAYER,
                display_name="Yoko",
                avatar_uri="https://mc-heads.net/avatar/Yoko/32",
            ),
            content="hello",
        )

        self.assertEqual(ModWebService._chat_author_avatar_uri(event), "https://mc-heads.net/avatar/Yoko/32")

    def test_chat_author_avatar_uri_accepts_safe_data_image_uri(self) -> None:
        avatar_uri = minecraft_dev_bypass_head_data_uri(Access_Control.dev_bypass_user_id(Power_Level.user))
        assert avatar_uri is not None
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(
                kind=ChatAuthorKind.WEB_USER,
                display_name="Tester",
                avatar_uri=avatar_uri,
            ),
            content="hello",
        )

        self.assertEqual(ModWebService._chat_author_avatar_uri(event), avatar_uri)

    def test_chat_author_avatar_uri_rejects_non_http_url(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(
                kind=ChatAuthorKind.GAME_PLAYER,
                display_name="Yoko",
                avatar_uri="javascript:alert(1)",
            ),
            content="hello",
        )

        self.assertIsNone(ModWebService._chat_author_avatar_uri(event))

    def test_chat_author_avatar_uri_rejects_non_image_data_uri(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(
                kind=ChatAuthorKind.WEB_USER,
                display_name="Tester",
                avatar_uri="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            ),
            content="hello",
        )

        self.assertIsNone(ModWebService._chat_author_avatar_uri(event))

    def test_chat_event_content_renders_typed_notice_for_web_chat(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", friendly="Minecraft Alpha"),
                }
            )
        )
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        self.assertEqual(service._chat_event_content(event), "Yoko joined Minecraft Alpha")

    def test_chat_event_content_omits_client_pack_details_for_web_chat(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", friendly="Minecraft Alpha"),
                }
            )
        )
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
                pack_version="2026-07-04",
                has_unpublished_pack_changes=True,
            ),
        )

        self.assertEqual(service._chat_event_content(event), "Yoko joined Minecraft Alpha")

    def test_chat_event_content_prefers_embed_description_for_web_chat(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Advancement: Stone Age",
            embed=ChatEmbed(title="Advancement", description="Stone Age", color=0x336699),
        )

        self.assertEqual(service._chat_event_content(event), "Stone Age")

    def test_user_can_use_fake_chat_preview_requires_root_level(self) -> None:
        service = ModWebService()
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text('{"root": [42], "user": [7]}')
            service.set_acl(Access_Control(pointer))

        root_user = ModWebUser(discord_id=42, username="rooty", global_name=None, avatar_hash=None)
        normal_user = ModWebUser(discord_id=7, username="norm", global_name=None, avatar_hash=None)

        self.assertTrue(service._user_can_use_fake_chat_preview(root_user))
        self.assertFalse(service._user_can_use_fake_chat_preview(normal_user))

    def test_fake_chat_preview_app_options_include_managed_apps_and_bound_rooms_in_friendly_order(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "zeta": SimpleNamespace(name="zeta", friendly="Zeta", supports_chat_relay=False),
                    "alpha": SimpleNamespace(name="alpha", friendly="Alpha", supports_chat_relay=True),
                    "beta": SimpleNamespace(name="beta", friendly="beta", supports_chat_relay=True),
                }
            )
        )

        with patch.object(ChatHub(), "bound_room_ids", return_value=("guest_lobby",)):
            options = service._fake_chat_preview_app_options()

        self.assertEqual(
            options,
            {
                "Alpha (alpha)": "alpha",
                "beta (beta)": "beta",
                "guest_lobby": "guest_lobby",
                "Zeta (zeta)": "zeta",
            },
        )

    def test_fake_chat_preview_send_target_options_prefer_bound_rooms(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "alpha": SimpleNamespace(name="alpha", friendly="Alpha", supports_chat_relay=True),
                    "zeta": SimpleNamespace(name="zeta", friendly="Zeta", supports_chat_relay=False),
                }
            )
        )

        with patch.object(ChatHub(), "bound_room_ids", return_value=("alpha", "guest_lobby")):
            options = service._fake_chat_preview_send_target_options()

        self.assertEqual(
            options,
            {
                "Alpha (alpha)": "alpha",
                "guest_lobby": "guest_lobby",
            },
        )

    def test_fake_chat_preview_send_target_options_fall_back_to_all_managed_apps_when_none_relay(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "zeta": SimpleNamespace(name="zeta", friendly="Zeta", supports_chat_relay=False),
                    "alpha": SimpleNamespace(name="alpha", friendly="Alpha", supports_chat_relay=False),
                }
            )
        )

        with patch.object(ChatHub(), "bound_room_ids", return_value=()):
            options = service._fake_chat_preview_send_target_options()

        self.assertEqual(
            options,
            {
                "Alpha (alpha)": "alpha",
                "Zeta (zeta)": "zeta",
            },
        )

    def test_build_fake_chat_preview_event_creates_join_notice_event(self) -> None:
        service = ModWebService()
        state = _ModWebFakeChatPreviewState(
            app_name="minecraft_alpha",
            source_kind=ChatEndpointKind.APP,
            author_kind=ChatAuthorKind.GAME_PLAYER,
            author_name="Yoko",
            message_mode=_ModWebFakeChatMessageMode.JOIN,
        )

        event = service._build_fake_chat_preview_event(state)

        self.assertEqual(event.room_id, "minecraft_alpha")
        self.assertEqual(event.source, ChatEndpointId.app("minecraft_alpha"))
        self.assertEqual(event.author.kind, ChatAuthorKind.GAME_PLAYER)
        self.assertEqual(event.author.display_name, "Yoko")
        self.assertEqual(event.content, "Yoko joined minecraft_alpha")
        self.assertIsNone(event.embed)
        self.assertIsInstance(event.notice, PlayerSessionNotice)
        assert isinstance(event.notice, PlayerSessionNotice)
        self.assertIs(event.notice.action, PlayerSessionAction.JOINED)

    def test_build_fake_chat_preview_event_creates_embed_with_app_color_and_source_kind(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "factorio_lab": SimpleNamespace(
                        name="factorio_lab",
                        friendly="Factorio Lab",
                        manage_embed_color=0xDC6B0F,
                    )
                }
            )
        )
        state = _ModWebFakeChatPreviewState(
            app_name="factorio_lab",
            source_kind=ChatEndpointKind.DISCORD_CHANNEL,
            author_kind=ChatAuthorKind.DISCORD_USER,
            author_name="Operator",
            message_mode=_ModWebFakeChatMessageMode.EMBED,
            content_text="",
            embed_title="Research",
            embed_description="Automation",
            source_label="Bridge",
        )

        event = service._build_fake_chat_preview_event(state)

        self.assertEqual(event.room_id, "factorio_lab")
        self.assertEqual(event.source, ChatEndpointId.discord_channel("preview"))
        self.assertEqual(event.author.kind, ChatAuthorKind.DISCORD_USER)
        self.assertEqual(event.author.display_name, "Operator")
        self.assertEqual(event.content, "Research: Automation")
        self.assertEqual(event.source_label, "Bridge")
        self.assertIsNotNone(event.embed)
        assert event.embed is not None
        self.assertEqual(event.embed.title, "Research")
        self.assertEqual(event.embed.description, "Automation")
        self.assertEqual(event.embed.color, 0xDC6B0F)

    def test_build_fake_chat_preview_event_for_room_overrides_room_and_app_source(self) -> None:
        service = ModWebService()
        state = _ModWebFakeChatPreviewState(
            app_name="minecraft_alpha",
            source_kind=ChatEndpointKind.APP,
            author_kind=ChatAuthorKind.GAME_PLAYER,
            author_name="Yoko",
            message_mode=_ModWebFakeChatMessageMode.JOIN,
        )

        event = service._build_fake_chat_preview_event_for_room(state, room_id="factorio_lab")

        self.assertEqual(event.room_id, "factorio_lab")
        self.assertEqual(event.source, ChatEndpointId.app("factorio_lab"))
        self.assertEqual(event.content, "Yoko joined factorio_lab")

    def test_build_fake_chat_preview_event_creates_advancement_notice_event(self) -> None:
        service = ModWebService()
        state = _ModWebFakeChatPreviewState(
            app_name="minecraft_alpha",
            source_kind=ChatEndpointKind.APP,
            author_kind=ChatAuthorKind.GAME_PLAYER,
            author_name="Yoko",
            message_mode=_ModWebFakeChatMessageMode.ADVANCEMENT,
            embed_title="Advancement",
            embed_description="Stone Age",
        )

        event = service._build_fake_chat_preview_event(state)

        self.assertEqual(event.content, "Yoko: Advancement: Stone Age")
        self.assertIsInstance(event.notice, GameProgressNotice)
        assert isinstance(event.notice, GameProgressNotice)
        self.assertIs(event.notice.progress_kind, GameProgressKind.ADVANCEMENT)
        self.assertEqual(event.notice.title, "Stone Age")

    def test_build_fake_chat_preview_event_creates_app_started_notice_with_friendly_app_name(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "factorio_lab": SimpleNamespace(
                        name="factorio_lab",
                        friendly="Factorio Lab",
                        supports_chat_relay=True,
                    )
                }
            )
        )
        state = _ModWebFakeChatPreviewState(
            app_name="factorio_lab",
            source_kind=ChatEndpointKind.SYSTEM,
            author_kind=ChatAuthorKind.SYSTEM,
            author_name="System",
            message_mode=_ModWebFakeChatMessageMode.APP_STARTED,
            detail_text="127.0.0.1:34197",
            embed_description="Mods synced\nAutosave restored",
        )

        event = service._build_fake_chat_preview_event(state)

        self.assertEqual(event.content, "Factorio Lab started")
        self.assertIsInstance(event.notice, AppLifecycleNotice)
        assert isinstance(event.notice, AppLifecycleNotice)
        self.assertIs(event.notice.state, AppLifecycleState.STARTED)
        self.assertEqual(event.notice.join_address, "127.0.0.1:34197")
        self.assertEqual(event.notice.detail_lines, ("Mods synced", "Autosave restored"))

    def test_build_fake_chat_preview_event_includes_reference_media_and_author_decoration(self) -> None:
        service = ModWebService()
        state = _ModWebFakeChatPreviewState(
            app_name="minecraft_alpha",
            source_kind=ChatEndpointKind.WEB_SESSION,
            author_kind=ChatAuthorKind.WEB_USER,
            author_name="Avery",
            author_color_hex="#336699",
            author_avatar_uri="https://cdn.example.com/avatar.png",
            message_mode=_ModWebFakeChatMessageMode.TEXT,
            content_text="look at this",
            reference_kind=ChatReferenceKind.REPLY,
            reference_author_name="Taylor",
            reference_content="earlier message",
            link_url="https://cdn.example.com/cat.png",
            link_label="cat.png",
            attachment_url="https://cdn.example.com/clip.mp4",
            attachment_name="clip.mp4",
        )

        event = service._build_fake_chat_preview_event(state)

        self.assertEqual(event.author.color_hex, "#336699")
        self.assertEqual(event.author.avatar_uri, "https://cdn.example.com/avatar.png")
        self.assertIs(event.reference_kind, ChatReferenceKind.REPLY)
        self.assertEqual(event.reference, ChatMessageReference("Taylor", "earlier message"))
        self.assertEqual(
            event.links,
            (
                ChatLink(
                    url="https://cdn.example.com/cat.png",
                    label="cat.png",
                    is_media=True,
                    extension=".png",
                    provider=ChatMediaProvider.DIRECT,
                ),
            ),
        )
        self.assertEqual(
            event.attachments,
            (ChatAttachment(uri="https://cdn.example.com/clip.mp4", name="clip.mp4"),),
        )

    def test_build_fake_chat_preview_event_creates_maintenance_warning_notice(self) -> None:
        service = ModWebService()
        state = _ModWebFakeChatPreviewState(
            app_name="minecraft_alpha",
            source_kind=ChatEndpointKind.SYSTEM,
            author_kind=ChatAuthorKind.SYSTEM,
            author_name="System",
            message_mode=_ModWebFakeChatMessageMode.MAINTENANCE_WARNING,
            detail_text="30",
            embed_description="Drain players first",
        )

        event = service._build_fake_chat_preview_event(state)

        self.assertEqual(event.content, "Scheduled maintenance: restart in 30m.")
        self.assertIsInstance(event.notice, MaintenanceNotice)
        assert isinstance(event.notice, MaintenanceNotice)
        self.assertIs(event.notice.stage, MaintenanceStage.WARNING)
        self.assertEqual(event.notice.lead_minutes, 30)
        self.assertEqual(event.notice.summary_lines, ("Drain players first",))

    def test_fake_chat_select_props_use_dark_popup_menu_class(self) -> None:
        self.assertEqual(
            ModWebService._fake_chat_select_props(clearable=True),
            "filled square dense clearable hide-bottom-space color=accent options-dense popup-content-class=mod-fake-chat-menu",
        )
        self.assertEqual(
            ModWebService._fake_chat_select_props(clearable=False),
            "filled square dense hide-bottom-space color=accent options-dense popup-content-class=mod-fake-chat-menu",
        )

    def test_framework_http_error_config_formats_server_error_page(self) -> None:
        service = ModWebService()

        config = service._framework_http_error_config(status_code=500, exception=RuntimeError("boom"))

        self.assertEqual(config.title, "Server error")
        self.assertEqual(config.badge_text, "500")
        self.assertEqual(config.badge_tone, "red")
        self.assertEqual(config.accent_color_hex, "#dc2626")
        self.assertEqual(config.detail_label, "Exception")
        self.assertEqual(config.detail_text, "RuntimeError: boom")
        self.assertIsNotNone(config.icon_markup)
        assert config.icon_markup is not None
        self.assertIn("<svg", config.icon_markup)
        self.assertEqual(len(config.actions), 1)
        self.assertEqual(config.actions[0].label, "Home")
        self.assertEqual(config.actions[0].url, service.index_path())

    def test_access_denied_icon_markup_loads_svg_resource(self) -> None:
        svg_markup = ModWebService._access_denied_icon_markup()

        self.assertIn("<svg", svg_markup)
        self.assertIn('viewBox="0 0 80 80"', svg_markup)
        self.assertIn("M60 18.5 65 20.6", svg_markup)

    def test_framework_http_error_config_formats_not_found_page(self) -> None:
        from fastapi import HTTPException

        service = ModWebService()

        config = service._framework_http_error_config(status_code=404, exception=HTTPException(404, "Missing page"))

        self.assertEqual(config.title, "Page not found")
        self.assertEqual(config.badge_text, "404")
        self.assertEqual(config.badge_tone, "grey")
        self.assertEqual(config.accent_color_hex, "#71717a")
        self.assertEqual(config.detail_label, "Details")
        self.assertEqual(config.detail_text, "Missing page")
        self.assertIsNotNone(config.icon_markup)

    def test_framework_http_error_config_formats_api_conflict_page(self) -> None:
        from fastapi import HTTPException

        service = ModWebService()
        config = service._framework_http_error_config(
            status_code=409,
            exception=HTTPException(
                409,
                "Client pack content has changed; publish or regenerate it before download.",
            ),
        )

        self.assertEqual(config.title, "Request could not be completed")
        self.assertEqual(config.badge_text, "409")
        self.assertEqual(config.badge_tone, "warn")
        self.assertEqual(config.detail_label, "Details")
        self.assertEqual(
            config.detail_text,
            "Client pack content has changed; publish or regenerate it before download.",
        )

    def test_framework_http_error_config_formats_api_service_error_page(self) -> None:
        from fastapi import HTTPException

        service = ModWebService()
        config = service._framework_http_error_config(
            status_code=503,
            exception=HTTPException(503, "Node is still starting."),
        )

        self.assertEqual(config.title, "Service unavailable")
        self.assertEqual(config.badge_text, "503")
        self.assertEqual(config.badge_tone, "red")
        self.assertEqual(config.detail_label, "Details")
        self.assertEqual(config.detail_text, "Node is still starting.")

    def test_framework_http_error_config_formats_request_validation_page(self) -> None:
        from fastapi.exceptions import RequestValidationError

        service = ModWebService()
        config = service._framework_http_error_config(
            status_code=422,
            exception=RequestValidationError(
                [
                    {
                        "type": "enum",
                        "loc": ("query", "pack_format"),
                        "msg": "Input should be a supported pack format",
                        "input": "invalid",
                    }
                ]
            ),
        )

        self.assertEqual(config.title, "Request could not be completed")
        self.assertEqual(config.badge_text, "422")
        self.assertEqual(
            config.detail_text,
            "query.pack_format: Input should be a supported pack format",
        )

    def test_framework_http_error_config_formats_redirect_loop_page(self) -> None:
        service = ModWebService()

        config = service._framework_http_error_config(
            status_code=310,
            exception=RuntimeError("Remote mod web attempted to redirect this request back to the same URL."),
        )

        self.assertEqual(config.title, "Too many redirects")
        self.assertEqual(config.badge_text, "310")
        self.assertEqual(config.badge_tone, "warn")
        self.assertEqual(config.accent_color_hex, "#f59e0b")
        self.assertEqual(config.detail_label, "Details")
        self.assertEqual(
            config.detail_text,
            "RuntimeError: Remote mod web attempted to redirect this request back to the same URL.",
        )
        self.assertIsNotNone(config.icon_markup)

    def test_dev_error_preview_actions_list_expected_preview_routes(self) -> None:
        actions = ModWebService._dev_error_preview_actions()

        self.assertEqual(
            actions,
            (
                _ModWebLinkSpec(label="Access Denied", url="/mod-web/dev/error/access-denied"),
                _ModWebLinkSpec(label="Sign-in Unavailable", url="/mod-web/dev/error/sign-in-unavailable"),
                _ModWebLinkSpec(label="OAuth Failure", url="/mod-web/dev/error/oauth-failure"),
                _ModWebLinkSpec(label="Page Unavailable", url="/mod-web/dev/error/page-unavailable"),
                _ModWebLinkSpec(label="Chat Unavailable", url="/mod-web/dev/error/chat-unavailable"),
                _ModWebLinkSpec(label="Node Unavailable", url="/mod-web/dev/error/node-unavailable"),
                _ModWebLinkSpec(label="Remote JSON Invalid", url="/mod-web/dev/error/remote-json-invalid"),
                _ModWebLinkSpec(label="Remote Timeout", url="/mod-web/dev/error/remote-timeout"),
                _ModWebLinkSpec(label="Remote Rejected", url="/mod-web/dev/error/remote-rejected"),
                _ModWebLinkSpec(label="Redirect Loop 310", url="/mod-web/dev/error/redirect-loop"),
                _ModWebLinkSpec(label="Framework 404", url="/mod-web/dev/error/framework-404"),
                _ModWebLinkSpec(label="Framework 500", url="/mod-web/dev/error/framework-500"),
                _ModWebLinkSpec(label="NiceGUI Exception", url="/mod-web/dev/error/nicegui-exception"),
                _ModWebLinkSpec(label="Refresh Shutdown", url="/mod-web/dev/error/refresh-shutdown"),
                _ModWebLinkSpec(label="Config Fail Toasts", url="/mod-web/dev/error/config-failure"),
                _ModWebLinkSpec(label="Chat Stream WS", url="/mod-web/dev/error/chat-stream-websocket"),
            ),
        )

    def test_oauth_failure_page_config_offers_retry_and_home_actions(self) -> None:
        page_config = ModWebService()._oauth_failure_page_config("OAuth state expired.")

        self.assertEqual(page_config.title, "Discord sign-in failed")
        self.assertEqual(page_config.badge_tone, "red")
        self.assertEqual(page_config.detail_text, "OAuth state expired.")
        self.assertEqual(
            page_config.actions,
            (
                _ModWebLinkSpec(label="Try Again", url="/auth/login?next_path=%2F"),
                _ModWebLinkSpec(label="Home", url="/"),
            ),
        )

    def test_login_administrators_lists_admin_and_above_with_cached_names(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(
                json.dumps(
                    {
                        "visitor": [1],
                        "user": [2],
                        "admin": [3],
                        "sudo": [4],
                        "root": [5, 792857784508219404],
                    }
                ),
                encoding="utf-8",
            )
            acl = Access_Control(pointer)

        display_names = {
            3: "Admin Alice",
            4: "Sudo Sam",
            5: "Root Riley",
            792857784508219404: "AiviA",
        }
        name_cache = Mock()
        name_cache.discord_avatar_hash.side_effect = lambda user_id: f"avatar-{user_id}"
        name_cache.web_display_name.side_effect = (
            lambda user_id, default: display_names.get(user_id, default)
        )
        service = ModWebService()
        service.set_acl(acl)

        with patch("web_dash.status.config.Name_Cache", return_value=name_cache):
            administrators = service._login_administrators()

        self.assertEqual([administrator.user_id for administrator in administrators], [5, 4, 3])
        self.assertNotIn(792857784508219404, [administrator.user_id for administrator in administrators])
        self.assertEqual(
            [administrator.display_name for administrator in administrators],
            ["Root Riley", "Sudo Sam", "Admin Alice"],
        )
        self.assertEqual(
            [administrator.avatar_hash for administrator in administrators],
            ["avatar-5", "avatar-4", "avatar-3"],
        )

    def test_login_administrator_rows_use_descending_power_order(self) -> None:
        self.assertEqual(
            ModWebService._login_administrator_levels(),
            (Power_Level.root, Power_Level.sudo, Power_Level.admin),
        )

    def test_login_administrator_avatar_requires_cached_discord_avatar(self) -> None:
        without_avatar = _ModWebLoginAdministrator(
            user_id=42,
            display_name="Admin Alice",
            level=Power_Level.admin,
            avatar_hash=None,
        )
        with_avatar = _ModWebLoginAdministrator(
            user_id=42,
            display_name="Admin Alice",
            level=Power_Level.admin,
            avatar_hash="avatar-123",
        )

        self.assertIsNone(ModWebService._login_administrator_avatar_uri(without_avatar))
        self.assertEqual(
            ModWebService._login_administrator_avatar_uri(with_avatar),
            "https://cdn.discordapp.com/avatars/42/avatar-123.png?size=128",
        )

    def test_login_information_actions_include_source_about_and_optional_build(self) -> None:
        with patch.object(config, "MOD_WEB_BUILD_SHA", "abcdef1234567890"):
            actions = ModWebService._login_information_actions()

        self.assertEqual([action.label for action in actions], ["About", "GitHub", "Build abcdef1"])
        self.assertEqual(actions[0].url, "/auth/about")
        self.assertEqual(actions[1].url, "https://github.com/APasz/Yukibot")
        self.assertEqual(
            actions[2].url,
            "https://github.com/APasz/Yukibot/commit/abcdef1234567890",
        )
        self.assertFalse(actions[0].new_tab)
        self.assertTrue(actions[1].new_tab)
        self.assertTrue(actions[2].new_tab)

    def test_login_information_actions_omit_unconfigured_build(self) -> None:
        with patch.object(config, "MOD_WEB_BUILD_SHA", None):
            actions = ModWebService._login_information_actions()

        self.assertEqual([action.label for action in actions], ["About", "GitHub"])

    def test_about_page_config_credits_project_history(self) -> None:
        page_config = ModWebService()._about_page_config()

        self.assertEqual(page_config.title, "About Yukibot")
        self.assertIn("NaiTechie", page_config.detail_text or "")
        self.assertIn("AiviA", page_config.detail_text or "")
        self.assertIn("APasz", page_config.detail_text or "")
        self.assertIn("rgba(167, 139, 250", page_config.icon_markup or "")
        self.assertEqual([action.label for action in page_config.actions], ["GitHub", "Home"])
        self.assertTrue(page_config.actions[0].new_tab)

    def test_about_supported_apps_follow_configured_app_scopes(self) -> None:
        self.assertEqual(
            ModWebService._about_supported_app_names(),
            (
                "Minecraft",
                "7 Days to Die",
                "BeamMP",
                "Euro Truck Simulator 2",
                "Factorio",
                "Satisfactory",
            ),
        )

    def test_dev_notification_preview_actions_cover_every_notification_type(self) -> None:
        actions = ModWebService._dev_notification_preview_actions()

        self.assertEqual(
            actions,
            (
                _ModWebNotificationPreviewSpec(
                    label="Positive Toast",
                    message="Positive notification preview.",
                    notification_type="positive",
                ),
                _ModWebNotificationPreviewSpec(
                    label="Negative Toast",
                    message="Negative notification preview.",
                    notification_type="negative",
                ),
                _ModWebNotificationPreviewSpec(
                    label="Warning Toast",
                    message="Warning notification preview.",
                    notification_type="warning",
                ),
                _ModWebNotificationPreviewSpec(
                    label="Info Toast",
                    message="Info notification preview.",
                    notification_type="info",
                ),
                _ModWebNotificationPreviewSpec(
                    label="Ongoing Toast",
                    message="Ongoing notification preview.",
                    notification_type="ongoing",
                ),
                _ModWebNotificationPreviewSpec(
                    label="Grouped Duplicate",
                    message="Intentional grouped duplicate preview.",
                    notification_type="info",
                    repeat_count=2,
                ),
                _ModWebNotificationPreviewSpec(
                    label="Long Multiline",
                    message=(
                        "Long multiline notification preview for checking wrapping, spacing, and readability "
                        "when a toast contains more detail than usual."
                    ),
                    notification_type="warning",
                    multi_line=True,
                ),
                _ModWebNotificationPreviewSpec(
                    label="Persistent Dismissible",
                    message="Persistent notification preview; dismiss it with the button.",
                    notification_type="ongoing",
                    close_button="Dismiss",
                    timeout_milliseconds=0,
                ),
            ),
        )

        self.assertEqual(
            {action.notification_type for action in actions},
            {"positive", "negative", "warning", "info", "ongoing"},
        )

    def test_notification_preview_spec_rejects_invalid_repeat_count_and_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "repeat count must be positive"):
            _ModWebNotificationPreviewSpec(
                label="Invalid",
                message="Invalid",
                notification_type="info",
                repeat_count=0,
            )
        with self.assertRaisesRegex(ValueError, "timeout must not be negative"):
            _ModWebNotificationPreviewSpec(
                label="Invalid",
                message="Invalid",
                notification_type="info",
                timeout_milliseconds=-1,
            )

    def test_login_dev_preview_card_buttons_emit_configured_notifications(self) -> None:
        class FakeElement:
            def __enter__(self) -> "FakeElement":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

            def classes(self, value: str) -> "FakeElement":
                del value
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.button_handlers: dict[str, Callable[[object | None], None]] = {}
                self.notifications: list[dict[str, object]] = []

            def card(self) -> FakeElement:
                return FakeElement()

            def column(self) -> FakeElement:
                return FakeElement()

            def row(self) -> FakeElement:
                return FakeElement()

            def label(self, text: str) -> FakeElement:
                del text
                return FakeElement()

            def button(
                self,
                label: str,
                *,
                on_click: Callable[[object | None], None],
            ) -> FakeElement:
                self.button_handlers[label] = on_click
                return FakeElement()

            def notify(
                self,
                message: str,
                *,
                close_button: bool | str = False,
                multi_line: bool = False,
                type: str | None = None,
                timeout: int | None = None,
            ) -> None:
                self.notifications.append(
                    {
                        "message": message,
                        "close_button": close_button,
                        "multi_line": multi_line,
                        "type": type,
                        "timeout": timeout,
                    }
                )

        service = ModWebService()
        ui = FakeUi()
        with patch.object(service, "_action_link"):
            service._render_login_dev_error_preview_card(ui=cast(ModWebUi, cast(object, ui)))

        ui.button_handlers["Grouped Duplicate"](None)
        self.assertEqual(len(ui.notifications), 2)
        self.assertEqual(ui.notifications[0], ui.notifications[1])
        self.assertEqual(ui.notifications[0]["timeout"], _APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS)

        ui.button_handlers["Long Multiline"](None)
        self.assertIs(ui.notifications[-1]["multi_line"], True)

        ui.button_handlers["Persistent Dismissible"](None)
        self.assertEqual(ui.notifications[-1]["close_button"], "Dismiss")
        self.assertEqual(ui.notifications[-1]["timeout"], 0)

    def test_exception_detail_text_formats_class_and_message(self) -> None:
        self.assertEqual(ModWebService._exception_detail_text(RuntimeError("boom")), "RuntimeError: boom")
        self.assertEqual(ModWebService._exception_detail_text(ValueError()), "ValueError")

    def test_should_render_framework_error_page_only_for_html_navigation_requests(self) -> None:
        self.assertTrue(
            ModWebService._should_render_framework_error_page(
                method="GET",
                path="/mod-web/missing",
                accept_header="text/html,application/xhtml+xml",
            )
        )
        self.assertTrue(
            ModWebService._should_render_framework_error_page(
                method="GET",
                path="/api/node/apps",
                accept_header="text/html,application/xhtml+xml",
            )
        )
        self.assertFalse(
            ModWebService._should_render_framework_error_page(
                method="GET",
                path="/api/node/apps",
                accept_header="application/json",
            )
        )
        self.assertFalse(
            ModWebService._should_render_framework_error_page(
                method="GET",
                path="/favicon.ico",
                accept_header="image/avif,image/webp,image/*,*/*;q=0.8",
            )
        )
        self.assertFalse(
            ModWebService._should_render_framework_error_page(
                method="POST",
                path="/mod-web/missing",
                accept_header="text/html,application/xhtml+xml",
            )
        )

    def test_chat_event_display_content_hides_join_and_leave_body_text(self) -> None:
        service = ModWebService()
        join_event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
        )
        leave_event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko left Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.LEFT,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        self.assertEqual(service._chat_event_display_content(join_event), "")
        self.assertEqual(service._chat_event_display_content(leave_event), "")

    def test_chat_event_display_content_keeps_typed_death_body_text(self) -> None:
        service = ModWebService()
        death_event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko died to Skeleton",
            notice=GameDeathNotice(
                death_kind=GameDeathKind.PVE,
                detail_text="died to Skeleton",
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        self.assertEqual(service._chat_event_display_content(death_event), "Yoko died to Skeleton")

    def test_chat_event_copy_text_prefers_event_body_content(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Hello from chat",
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Ken", content="Referenced text"),
        )

        self.assertEqual(service._chat_event_copy_text(event), "Hello from chat")

    def test_chat_event_copy_text_falls_back_to_reference_content(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="   ",
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Ken", content="Referenced text"),
        )

        self.assertEqual(service._chat_event_copy_text(event), "Referenced text")

    def test_chat_markup_html_supports_discord_style_formatting(self) -> None:
        markup = ModWebService._chat_markup_html("**bold** _italic_ __underline__ ~~strike~~ ||spoiler||\n> quoted")

        self.assertIn("<strong>bold</strong>", markup)
        self.assertIn("<em>italic</em>", markup)
        self.assertIn("<u>underline</u>", markup)
        self.assertIn("<s>strike</s>", markup)
        self.assertIn('<span class="mod-chat-spoiler" tabindex="0">spoiler</span>', markup)
        self.assertIn('<blockquote class="mod-chat-quote">quoted</blockquote>', markup)

    def test_chat_markup_html_renders_inline_markdown_links(self) -> None:
        markup = ModWebService._chat_markup_html("look [Cat](https://cdn.example.com/cat.png)")

        self.assertIn(
            '<a href="https://cdn.example.com/cat.png" target="_blank" rel="noopener noreferrer">Cat</a>',
            markup,
        )
        self.assertNotIn("[Cat](https://cdn.example.com/cat.png)", markup)

    def test_chat_markup_html_autolinks_raw_urls(self) -> None:
        markup = ModWebService._chat_markup_html("look https://cdn.example.com/cat.png.")

        self.assertIn(
            '<a href="https://cdn.example.com/cat.png" target="_blank" rel="noopener noreferrer">'
            "https://cdn.example.com/cat.png</a>.",
            markup,
        )

    def test_chat_markup_html_preserves_code_and_escapes_html(self) -> None:
        markup = ModWebService._chat_markup_html("`<b>safe</b>`\n```\n**literal**\n```")

        self.assertIn('<code class="mod-chat-inline-code">&lt;b&gt;safe&lt;/b&gt;</code>', markup)
        self.assertIn('<pre class="mod-chat-code-block"><code>**literal**</code></pre>', markup)
        self.assertNotIn("<strong>literal</strong>", markup)

    def test_chat_markup_html_does_not_render_unsafe_markdown_links(self) -> None:
        markup = ModWebService._chat_markup_html("[Danger](javascript:alert(1))")

        self.assertIn("[Danger](javascript:alert(1))", markup)
        self.assertNotIn("<a ", markup)

    def test_chat_markup_html_respects_escaped_markdown_and_quote_prefixes(self) -> None:
        markup = ModWebService._chat_markup_html(r"\*\*literal\*\* \> not a quote")

        self.assertIn("**literal** &gt; not a quote", markup)
        self.assertNotIn("<strong>", markup)
        self.assertNotIn("<blockquote", markup)

    def test_chat_markup_html_supports_discord_multiline_quote_blocks(self) -> None:
        markup = ModWebService._chat_markup_html(">>> quoted\nstill quoted")

        self.assertIn('<blockquote class="mod-chat-quote">quoted<br>still quoted</blockquote>', markup)

    def test_chat_markup_html_supports_discord_headers_and_subtext(self) -> None:
        markup = ModWebService._chat_markup_html("# Big\n## Mid\n### Small\n-# Fine print")

        self.assertIn('<div class="mod-chat-markup-heading mod-chat-markup-heading-1">Big</div>', markup)
        self.assertIn('<div class="mod-chat-markup-heading mod-chat-markup-heading-2">Mid</div>', markup)
        self.assertIn('<div class="mod-chat-markup-heading mod-chat-markup-heading-3">Small</div>', markup)
        self.assertIn('<div class="mod-chat-markup-subtext">Fine print</div>', markup)

    def test_chat_markup_html_only_treats_headers_and_subtext_at_start_of_line(self) -> None:
        markup = ModWebService._chat_markup_html("plain # not a header\nplain -# not subtext")

        self.assertNotIn("mod-chat-markup-heading", markup)
        self.assertNotIn("mod-chat-markup-subtext", markup)
        self.assertIn("plain # not a header", markup)
        self.assertIn("plain -# not subtext", markup)

    def test_chat_markup_html_mixes_headers_subtext_quotes_and_normal_lines(self) -> None:
        markup = ModWebService._chat_markup_html("# Title\nbody\n-# note\n> quote")

        self.assertIn('<div class="mod-chat-markup-heading mod-chat-markup-heading-1">Title</div>', markup)
        self.assertIn('<div class="mod-chat-markup-block">body</div>', markup)
        self.assertIn('<div class="mod-chat-markup-subtext">note</div>', markup)
        self.assertIn('<blockquote class="mod-chat-quote">quote</blockquote>', markup)

    def test_chat_markup_html_supports_discord_unordered_and_ordered_lists(self) -> None:
        markup = ModWebService._chat_markup_html("- one\n* two\n1. first\n2. second")

        self.assertIn(
            '<ul class="mod-chat-markup-list mod-chat-markup-list-unordered"><li>one</li><li>two</li></ul>', markup
        )
        self.assertIn(
            '<ol class="mod-chat-markup-list mod-chat-markup-list-ordered"><li>first</li><li>second</li></ol>', markup
        )

    def test_chat_markup_html_supports_nested_discord_lists_with_two_space_indent(self) -> None:
        markup = ModWebService._chat_markup_html("- top\n  - child\n  3. third\n- next")

        self.assertIn(
            '<ul class="mod-chat-markup-list mod-chat-markup-list-unordered"><li>top'
            '<ul class="mod-chat-markup-list mod-chat-markup-list-unordered"><li>child</li></ul>'
            '<ol class="mod-chat-markup-list mod-chat-markup-list-ordered" start="3"><li>third</li></ol>'
            "</li><li>next</li></ul>",
            markup,
        )

    def test_chat_markup_html_only_treats_even_indented_list_markers_as_nested_lists(self) -> None:
        markup = ModWebService._chat_markup_html(" - not nested\n   - still plain")

        self.assertNotIn("mod-chat-markup-list", markup)
        self.assertIn(" - not nested", markup)
        self.assertIn("   - still plain", markup)

    def test_chat_markup_html_supports_language_tagged_code_blocks(self) -> None:
        markup = ModWebService._chat_markup_html("```py\n**literal**\n```")

        self.assertIn('<pre class="mod-chat-code-block"><code>**literal**</code></pre>', markup)
        self.assertNotIn("<strong>literal</strong>", markup)

    def test_chat_markup_html_applies_text_transform_after_code_placeholders(self) -> None:
        markup = ModWebService._chat_markup_html(
            "hi <@42> `keep <@43>`",
            text_transform=lambda text: text.replace("<@42>", "@Alice").replace("<@43>", "@Bob"),
        )

        self.assertIn("@Alice", markup)
        self.assertIn("keep &lt;@43&gt;", markup)
        self.assertNotIn("@Bob", markup)

    def test_resolve_chat_markup_mentions_uses_name_cache_for_discord_ids(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", scope="minecraft"),
                }
            )
        )
        web_mention_name = Mock(side_effect=lambda user_id, **_: f"user-{user_id}")

        with patch(
            "web_dash.chat.config.Name_Cache",
            return_value=SimpleNamespace(web_mention_name=web_mention_name),
        ):
            resolved = service._resolve_chat_markup_mentions(
                "hi <@42> and @43",
                room_id="minecraft_alpha",
                preferred_guild_id=99,
            )

        self.assertEqual(resolved, "hi @user-42 and @user-43")
        self.assertEqual(
            web_mention_name.call_args_list,
            [
                call(42, scope="minecraft", platforms=(), preferred_platform=None, default="42"),
                call(43, scope="minecraft", platforms=(), preferred_platform=None, default="43"),
            ],
        )

    def test_resolve_chat_markup_mentions_resolves_channel_and_role_entities(self) -> None:
        service = ModWebService()
        service._manager = _manager_stub(
            apps={},
            bot=SimpleNamespace(
                cache=SimpleNamespace(
                    get_guild_channel=lambda channel_id: (
                        SimpleNamespace(name="relay-main") if int(channel_id) == 123 else None
                    ),
                    get_role=lambda role_id: SimpleNamespace(name="Raiders") if int(role_id) == 456 else None,
                )
            ),
        )

        resolved = service._resolve_chat_markup_mentions(
            "see <#123> ping <@&456> at <t:123:T>",
            room_id="minecraft_alpha",
            preferred_guild_id=99,
        )

        self.assertEqual(resolved, "see #relay-main ping @Raiders at <t:123:T>")

    def test_chat_markup_html_renders_discord_timestamps_as_client_local_time_elements(self) -> None:
        service = ModWebService()

        with patch("web_dash.chat.time.time", return_value=90.0):
            markup = service._chat_markup_html(
                "started <t:120:R> at <t:123:T>",
                text_transform=lambda text: service._resolve_chat_markup_mentions(
                    text,
                    room_id="minecraft_alpha",
                    preferred_guild_id=None,
                ),
            )

        self.assertIn('class="mod-chat-client-time"', markup)
        self.assertIn('data-mod-chat-unix="120"', markup)
        self.assertIn('data-mod-chat-time-style="R"', markup)
        self.assertIn(">in 30 seconds<", markup)
        self.assertIn('data-mod-chat-unix="123"', markup)
        self.assertIn('data-mod-chat-time-style="T"', markup)
        self.assertIn(">00:02:03 UTC<", markup)

    def test_resolve_chat_markup_mentions_preserves_relative_timestamp_tokens_for_markup_rendering(self) -> None:
        service = ModWebService()

        resolved = service._resolve_chat_markup_mentions(
            "started <t:120:R> ended <t:30:R>",
            room_id="minecraft_alpha",
            preferred_guild_id=None,
        )

        self.assertEqual(resolved, "started <t:120:R> ended <t:30:R>")

    def test_chat_client_script_localizes_timestamps_in_browser(self) -> None:
        script = ModWebService._chat_client_script()

        self.assertIn("Intl.DateTimeFormat", script)
        self.assertIn("mod-chat-client-time", script)
        self.assertIn("MutationObserver", script)
        self.assertIn("use24HourTime: true", script)
        self.assertIn("window.modWebPreferences", script)
        self.assertIn("hour12: false", script)
        self.assertIn("hourCycle: 'h23'", script)
        self.assertIn("setInterval(() => localizeTimes(document), 30000)", script)

    def test_chat_markup_html_preserves_inline_discord_timestamps_inside_code(self) -> None:
        markup = ModWebService._chat_markup_html("`<t:123:T>` outside <t:123:T>")

        self.assertIn("&lt;t:123:T&gt;", markup)
        self.assertIn('data-mod-chat-unix="123"', markup)
        self.assertEqual(markup.count('data-mod-chat-unix="123"'), 1)

    def test_chat_markup_html_renders_relative_timestamp_fallback_text(self) -> None:
        service = ModWebService()

        with patch("web_dash.chat.time.time", return_value=90.0):
            markup = service._chat_markup_html(
                "started <t:120:R> ended <t:30:R>",
                text_transform=lambda text: service._resolve_chat_markup_mentions(
                    text,
                    room_id="minecraft_alpha",
                    preferred_guild_id=None,
                ),
            )

        self.assertIn(">in 30 seconds<", markup)
        self.assertIn(">1 minute ago<", markup)

    def test_chat_event_author_display_name_resolves_raw_discord_mentions(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", scope="minecraft"),
                }
            )
        )
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="<@42>", discord_user_id=42),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
            source_guild_id=99,
        )
        web_display_name = Mock(return_value="Yoko")

        with patch(
            "web_dash.chat.config.Name_Cache",
            return_value=SimpleNamespace(web_display_name=web_display_name),
        ):
            resolved = service._chat_event_author_display_name(event)

        self.assertEqual(resolved, "Yoko")
        web_display_name.assert_called_once_with(
            42,
            "42",
            scope="minecraft",
            platforms=(),
            preferred_platform=None,
        )

    def test_chat_reference_label_resolves_raw_discord_mentions(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", scope="minecraft"),
                }
            )
        )
        web_display_name = Mock(return_value="Yoko")

        with patch(
            "web_dash.chat.config.Name_Cache",
            return_value=SimpleNamespace(web_display_name=web_display_name),
        ):
            label = service._chat_reference_label(
                ChatReferenceKind.REPLY,
                ChatMessageReference("<@42>", "Joined the game"),
                room_id="minecraft_alpha",
                preferred_guild_id=99,
            )

        self.assertEqual(label, "Replying to Yoko")
        web_display_name.assert_called_once_with(
            42,
            "42",
            scope="minecraft",
            platforms=(),
            preferred_platform=None,
        )

    def test_chat_event_badges_include_join_notice_badge(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        self.assertEqual(ModWebService._chat_event_badges(event), (_ModWebBadgeSpec(text="Joined", tone="purple"),))

    def test_chat_event_badges_omit_client_pack_badge_for_join_notice(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
                pack_version="2026-07-04",
                has_unpublished_pack_changes=True,
            ),
        )

        self.assertEqual(
            ModWebService._chat_event_badges(event),
            (_ModWebBadgeSpec(text="Joined", tone="purple"),),
        )

    def test_chat_event_badges_prefer_typed_notice_over_embed_title(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="Stopped",
            notice=AppLifecycleNotice(
                state=AppLifecycleState.STOPPED,
                source=RelayNoticeSource.APP_MANAGER,
            ),
            embed=ChatEmbed(title="Minecraft Alpha Ended", description="Uptime: `1h 2m 3s`", color=0x336699),
        )

        self.assertEqual(
            ModWebService._chat_event_badges(event),
            (_ModWebBadgeSpec(text="Ended", tone="grey"),),
        )

    def test_chat_event_badges_include_embed_title_badge(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="Research: Electronics 1",
            embed=ChatEmbed(title="Research", description="Electronics 1", color=0x336699),
        )

        self.assertEqual(
            ModWebService._chat_event_badges(event),
            (_ModWebBadgeSpec(text="Research", tone="black"),),
        )

    def test_chat_event_badges_treat_ended_embed_titles_as_stopped(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="Stopped",
            embed=ChatEmbed(title="Minecraft Alpha Ended", description="Uptime: `1h 2m 3s`", color=0x336699),
        )

        self.assertEqual(
            ModWebService._chat_event_badges(event),
            (_ModWebBadgeSpec(text="Minecraft Alpha Ended", tone="grey"),),
        )

    def test_chat_event_badges_treat_crashed_embed_titles_as_crash_notices(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="Crashed",
            embed=ChatEmbed(title="Minecraft Alpha Crashed", description="Out of memory", color=0x336699),
        )

        self.assertEqual(
            ModWebService._chat_event_badges(event),
            (_ModWebBadgeSpec(text="Minecraft Alpha Crashed", tone="red"),),
        )

    def test_discord_chat_source_label_uses_guild_name_for_single_guild_channel(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Yoko"),
            content="hello",
            source_guild_id=1,
            source_channel_id=123,
            source_label="relay-main",
        )
        service._manager = _manager_stub(
            apps={"minecraft_alpha": SimpleNamespace(chat_channels=(123, 789))},
            bot=SimpleNamespace(
                cache=SimpleNamespace(
                    get_guild=lambda guild_id: SimpleNamespace(name="Friends") if int(guild_id) == 1 else None,
                    get_guild_channel=lambda channel_id: {
                        123: SimpleNamespace(guild_id=1, name="relay-main"),
                        789: SimpleNamespace(guild_id=2, name="other-guild"),
                    }.get(int(channel_id)),
                )
            ),
        )

        self.assertEqual(service._chat_event_source_label(event), "Friends")

    def test_discord_chat_source_label_includes_channel_when_guild_has_multiple_room_channels(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Yoko"),
            content="hello",
            source_guild_id=1,
            source_channel_id=123,
            source_label="relay-main",
        )
        service._manager = cast(
            Any,
            SimpleNamespace(
                apps={"minecraft_alpha": SimpleNamespace(chat_channels=(123, 456))},
                bot=SimpleNamespace(
                    cache=SimpleNamespace(
                        get_guild=lambda guild_id: SimpleNamespace(name="Friends") if int(guild_id) == 1 else None,
                        get_guild_channel=lambda channel_id: {
                            123: SimpleNamespace(guild_id=1, name="relay-main"),
                            456: SimpleNamespace(guild_id=1, name="relay-side"),
                        }.get(int(channel_id)),
                    )
                ),
            ),
        )

        self.assertEqual(service._chat_event_source_label(event), "Friends.relay-main")

    def test_discord_chat_source_label_uses_serialized_guild_name_without_manager_context(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Yoko"),
            content="hello",
            source_guild_id=1,
            source_guild_name="Friends",
            source_channel_id=123,
            source_label="relay-main",
        )

        self.assertEqual(service._chat_event_source_label(event), "Friends")

    def test_discord_chat_source_label_falls_back_to_channel_name_without_manager_context(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Yoko"),
            content="hello",
            source_channel_id=123,
            source_label="relay-main",
        )

        self.assertEqual(service._chat_event_source_label(event), "relay-main")

    def test_discord_chat_source_label_uses_discord_fallback_when_guild_id_has_no_name(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Yoko"),
            content="hello",
            source_guild_id=1,
            source_channel_id=123,
            source_label="relay-main",
        )

        self.assertEqual(service._chat_event_source_label(event), "Discord")

    def test_app_chat_source_label_uses_game_badge_text(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="hello",
        )

        self.assertEqual(service._chat_event_source_label(event), "GAME")

    def test_app_chat_source_label_uses_source_instance_name_in_other_room(self) -> None:
        service = ModWebService()
        service._manager = _manager_stub(
            apps={
                "sevendays_1": SimpleNamespace(name="sevendays_1", friendly="7D2D-1"),
                "sevendays_2": SimpleNamespace(name="sevendays_2", friendly="7D2D-2"),
            }
        )
        event = ChatEvent(
            room_id="sevendays_2",
            source=ChatEndpointId.app("sevendays_1"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="hello",
        )

        self.assertEqual(service._chat_event_source_label(event, room_id="sevendays_2"), "7D2D-1")

    def test_app_chat_source_label_falls_back_to_source_room_id_in_other_room_without_manager_context(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="sevendays_2",
            source=ChatEndpointId.app("sevendays_1"),
            author=ChatAuthor(kind=ChatAuthorKind.GAME_PLAYER, display_name="Yoko"),
            content="hello",
        )

        self.assertEqual(service._chat_event_source_label(event, room_id="sevendays_2"), "sevendays_1")

    def test_web_chat_source_label_uses_web_badge_text(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Yoko"),
            content="hello",
        )

        self.assertEqual(service._chat_event_source_label(event), "WEB")

    def test_system_chat_source_label_overrides_app_source(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="Research: Electronics 1",
        )

        self.assertEqual(service._chat_event_source_label(event), "SYSTEM")
        self.assertEqual(service._chat_event_tone(event), "warn")
        self.assertEqual(service._chat_event_source_class(event), "system")

    def test_chat_media_preview_embeds_image_links(self) -> None:
        preview = ModWebService._chat_media_preview_from_link(
            ChatLink(url="https://example.invalid/cat.gif", label="cat gif", media_type="image/gif", is_media=True)
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        markup = ModWebService._chat_media_embed_markup(preview)
        self.assertIn('<a class="mod-chat-media-link" href="https://example.invalid/cat.gif"', markup)
        self.assertIn('target="_blank"', markup)
        self.assertIn('rel="noopener noreferrer"', markup)
        self.assertIn('<img class="mod-chat-media-image"', markup)
        self.assertIn('src="https://example.invalid/cat.gif"', markup)
        self.assertIn('<span class="mod-chat-media-caption">cat gif</span>', markup)

    def test_chat_media_preview_embeds_video_attachments_by_extension(self) -> None:
        preview = ModWebService._chat_media_preview_from_attachment(
            ChatAttachment(
                uri="/tmp/upload",
                source_url="https://cdn.example.invalid/clip",
                name="clip.webm",
            )
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview.kind, "video")

    def test_chat_event_display_content_hides_previewed_sticker_fallback_text(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="APasz"),
            content="sticker; Wave",
            attachments=(
                ChatAttachment(
                    uri="https://media.discordapp.net/stickers/123.png",
                    source_url="https://media.discordapp.net/stickers/123.png",
                    name="Wave.png",
                ),
            ),
        )

        self.assertEqual(service._chat_event_display_content(event), "")
        self.assertEqual(service._chat_event_copy_text(event), "sticker; Wave")

    def test_chat_event_display_content_hides_previewed_media_link_url(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="Mea"),
            content="https://klipy.com/gifs/frieren-anime-54",
            links=(
                ChatLink(
                    url="https://klipy.com/gifs/frieren-anime-54",
                    media_type="image/gif",
                    is_media=True,
                ),
            ),
        )

        self.assertEqual(service._chat_event_display_content(event), "")
        self.assertEqual(service._chat_event_copy_text(event), "https://klipy.com/gifs/frieren-anime-54")

    def test_chat_event_display_content_hides_rendered_link_url(self) -> None:
        service = ModWebService()
        event = ChatEvent(
            room_id="factorio_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.DISCORD_USER, display_name="CoffeeCreamTV"),
            content="https://mods.factorio.com/mod/factorio-rate-calculator-tooltip",
            links=(
                ChatLink(
                    url="https://mods.factorio.com/mod/factorio-rate-calculator-tooltip",
                    label="https://mods.factorio.com/mod/factorio-rate-calculator-tooltip",
                    is_media=False,
                ),
            ),
        )

        self.assertEqual(service._chat_event_display_content(event), "")
        self.assertEqual(
            service._chat_event_copy_text(event),
            "https://mods.factorio.com/mod/factorio-rate-calculator-tooltip",
        )

    def test_chat_media_preview_rejects_non_http_urls(self) -> None:
        preview = ModWebService._chat_media_preview_from_link(
            ChatLink(url="javascript:alert(1)", label="bad", media_type="image/png", is_media=True)
        )

        self.assertIsNone(preview)

    def test_chat_media_embed_markup_escapes_labels(self) -> None:
        preview = ModWebService._chat_media_preview_from_link(
            ChatLink(
                url="https://example.invalid/cat.png",
                label='cat "onerror"',
                media_type="image/png",
                is_media=True,
            )
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        markup = ModWebService._chat_media_embed_markup(preview)
        self.assertIn("cat &quot;onerror&quot;", markup)
        self.assertIn('<span class="mod-chat-media-caption">cat &quot;onerror&quot;</span>', markup)
        self.assertNotIn('cat "onerror"', markup)

    def test_chat_app_status_badge_reflects_runtime_state(self) -> None:
        running_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=2,
            player_capacity=20,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )
        stopped_stats = replace(running_stats, running=False, player_count=None, player_capacity=None)

        self.assertEqual(ModWebService._chat_app_status_badge(running_stats), ("Running", "grey"))
        self.assertEqual(ModWebService._chat_app_status_badge(stopped_stats), ("Stopped", "grey"))
        self.assertEqual(ModWebService._chat_app_status_badge(None), ("Status unknown", "warn"))

    def test_chat_app_status_badge_uses_crash_state(self) -> None:
        crashed_stats = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version="1.20.1",
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            runtime_fault=AppRuntimeFault(kind=AppRuntimeFaultKind.CRASH, summary="Failed to start the minecraft server"),
        )

        self.assertEqual(ModWebService._chat_app_status_badge(crashed_stats), ("Crashed", "red"))

    def test_player_count_snapshot_text_requires_complete_snapshot(self) -> None:
        self.assertEqual(
            ModWebService._player_count_snapshot_text(player_count=3, player_capacity=20),
            "3 / 20",
        )
        self.assertEqual(
            ModWebService._player_count_snapshot_text(player_count=3, player_capacity=-1),
            "3 / ∞",
        )
        self.assertIsNone(ModWebService._player_count_snapshot_text(player_count=3, player_capacity=None))
        self.assertIsNone(ModWebService._player_count_snapshot_text(player_count=None, player_capacity=20))

    def test_chat_player_count_badge_reflects_runtime_snapshot(self) -> None:
        active_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version=None,
            player_count=3,
            player_capacity=20,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )
        empty_stats = replace(active_stats, player_count=0)
        missing_stats = replace(active_stats, player_capacity=None)

        self.assertEqual(
            ModWebService._chat_player_count_badge(active_stats),
            _ModWebBadgeSpec(text="3 / 20", tone="purple"),
        )
        self.assertEqual(
            ModWebService._chat_player_count_badge(replace(active_stats, player_capacity=-1)),
            _ModWebBadgeSpec(text="3 / ∞", tone="purple"),
        )
        self.assertEqual(
            ModWebService._chat_player_count_badge(empty_stats),
            _ModWebBadgeSpec(text="0 / 20", tone="grey"),
        )
        self.assertIsNone(ModWebService._chat_player_count_badge(missing_stats))
        self.assertIsNone(ModWebService._chat_player_count_badge(None))

    def test_player_count_tooltip_html_lists_connected_players(self) -> None:
        service = ModWebService()

        self.assertEqual(
            service._player_count_tooltip_html(
                connected_player_names=("Yoko", "Bea", "Casey"),
                fallback_text="3 / 20",
            ),
            "Yoko<br>Bea<br>Casey",
        )
        self.assertEqual(
            service._player_count_tooltip_html(
                connected_player_names=("", "  "),
                fallback_text="3 / 20",
            ),
            "3 / 20",
        )
        self.assertIsNone(service._player_count_tooltip_html(connected_player_names=()))

    def test_local_chat_panel_config_subscribes_to_room_and_runtime_updates(self) -> None:
        service = ModWebService()
        initial_snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=1,
            events=(),
            endpoint_summaries=(NodeChatEndpointSummary(label="Game: Minecraft Alpha"),),
        )
        runtime_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=3,
            player_capacity=12,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            transition_state=NodeAppTransitionState.NONE,
        )
        service._local_chat_snapshot = Mock(return_value=initial_snapshot)  # type: ignore[method-assign]
        runtime_unsubscribe = Mock()
        service._node_api.subscribe_local_app_runtime = Mock(return_value=runtime_unsubscribe)  # type: ignore[method-assign]

        panel = service._local_chat_panel_config(
            room_id="minecraft_alpha",
            session_id="session-1",
            user=cast(Any, SimpleNamespace(discord_id=42, display_name="Tester")),
            app_scope="minecraft",
        )

        on_update = Mock()
        with (
            patch.object(ChatHub(), "subscribe", return_value="room-subscription") as subscribe_mock,
            patch.object(ChatHub(), "unsubscribe") as unsubscribe_mock,
        ):
            assert panel.subscribe_updates is not None
            unsubscribe = panel.subscribe_updates(on_update)
            room_callback = subscribe_mock.call_args.args[1]
            runtime_callback = service._node_api.subscribe_local_app_runtime.call_args.args[1]
            room_callback(ChatRoomUpdate(room_id="minecraft_alpha"))
            runtime_callback(NodeAppStateStreamEvent.runtime(app_name="minecraft_alpha", app_stats=runtime_stats))
            unsubscribe()

        subscribe_mock.assert_called_once()
        service._node_api.subscribe_local_app_runtime.assert_called_once()
        self.assertEqual(
            on_update.call_args_list,
            [
                call(_ModWebChatPanelSignal.chat()),
                call(_ModWebChatPanelSignal.runtime(app_stats=runtime_stats)),
            ],
        )
        unsubscribe_mock.assert_called_once_with("minecraft_alpha", "room-subscription")
        runtime_unsubscribe.assert_called_once_with()

    def test_local_chat_panel_config_send_message_uses_scoped_web_display_name(self) -> None:
        service = ModWebService()
        service._local_chat_snapshot = Mock(
            return_value=NodeChatRoomSnapshot(
                room_id="minecraft_alpha",
                endpoint_count=0,
                events=(),
                endpoint_summaries=(),
            )
        )  # type: ignore[method-assign]
        service._chat_relay = cast(
            Any,
            SimpleNamespace(
                publish_web_chat=AsyncMock(
                    return_value=ChatEvent(
                        room_id="minecraft_alpha",
                        source=ChatEndpointId.web_session("session-1"),
                        author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="AliceGame"),
                        content="hello",
                    )
                )
            ),
        )

        panel = service._local_chat_panel_config(
            room_id="minecraft_alpha",
            session_id="session-1",
            user=cast(Any, SimpleNamespace(discord_id=42, display_name="Tester")),
            app_scope="minecraft",
        )

        with patch(
            "web_dash.service.config.Name_Cache",
            return_value=SimpleNamespace(web_display_name=Mock(return_value="AliceGame")),
        ):
            send_message = panel.send_message
            assert send_message is not None

            async def _send_message() -> None:
                await send_message(
                    _ModWebChatComposeRequest(
                        content="hello",
                        reply_to_event_id=None,
                    )
                )

            asyncio.run(_send_message())

        service._chat_relay.publish_web_chat.assert_awaited_once()
        self.assertEqual(
            service._chat_relay.publish_web_chat.await_args.kwargs["author_display_name"],
            "AliceGame",
        )

    def test_local_chat_panel_config_can_skip_runtime_updates(self) -> None:
        service = ModWebService()
        service._local_chat_snapshot = Mock(
            return_value=NodeChatRoomSnapshot(
                room_id="minecraft_alpha",
                endpoint_count=0,
                events=(),
                endpoint_summaries=(),
            )
        )  # type: ignore[method-assign]
        service._node_api.subscribe_local_app_runtime = Mock()  # type: ignore[method-assign]

        panel = service._local_chat_panel_config(
            room_id="minecraft_alpha",
            session_id="session-1",
            user=cast(Any, SimpleNamespace(discord_id=42, display_name="Tester")),
            app_scope="minecraft",
            include_runtime_updates=False,
        )

        on_update = Mock()
        with (
            patch.object(ChatHub(), "subscribe", return_value="room-subscription") as subscribe_mock,
            patch.object(ChatHub(), "unsubscribe") as unsubscribe_mock,
        ):
            assert panel.subscribe_updates is not None
            unsubscribe = panel.subscribe_updates(on_update)
            room_callback = subscribe_mock.call_args.args[1]
            room_callback(ChatRoomUpdate(room_id="minecraft_alpha"))
            unsubscribe()

        self.assertEqual(on_update.call_args_list, [call(_ModWebChatPanelSignal.chat())])
        service._node_api.subscribe_local_app_runtime.assert_not_called()
        unsubscribe_mock.assert_called_once_with("minecraft_alpha", "room-subscription")

    def test_render_chat_section_reuses_panel_without_embedded_header_chrome(self) -> None:
        class FakeColumn:
            class_value: str | None = None

            def classes(self, value: str) -> "FakeColumn":
                self.class_value = value
                return self

            def __enter__(self) -> "FakeColumn":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeUi:
            def column(self) -> FakeColumn:
                return FakeColumn()

        service = ModWebService()
        runtime_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=3,
            player_capacity=12,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            transition_state=NodeAppTransitionState.NONE,
        )
        initial_snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=1,
            events=(),
            endpoint_summaries=(NodeChatEndpointSummary(label="Game: Minecraft Alpha"),),
        )
        panel = _ModWebChatPanelConfig(
            initial_snapshot=initial_snapshot,
            refresh_snapshot=AsyncMock(return_value=initial_snapshot),
            send_message=None,
        )
        chat_surface = _ModWebChatSurfaceConfig(
            panel=panel,
            node_name="yuki",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            app_stats=runtime_stats,
            hero_badges=(_ModWebBadgeSpec(text="Relay", tone="purple"),),
            refresh_app_stats=AsyncMock(return_value=runtime_stats),
            popout_url="/mod-web/chat/minecraft_alpha",
        )
        endpoint_count_label = cast(Any, object())
        endpoint_count_tooltip = cast(Any, object())
        endpoint_count_tooltip_content = cast(Any, object())
        apply_runtime_stats = Mock()
        ui = FakeUi()

        with patch.object(ModWebService, "_render_chat_panel", return_value=apply_runtime_stats) as render_chat_panel:
            apply_runtime_model = service._render_chat_section(
                ui=cast(ModWebUi, cast(object, ui)),
                chat_surface=chat_surface,
                endpoint_count_label=endpoint_count_label,
                endpoint_count_tooltip=endpoint_count_tooltip,
                endpoint_count_tooltip_content=endpoint_count_tooltip_content,
            )

        call_kwargs = render_chat_panel.call_args.kwargs
        self.assertEqual(call_kwargs["ui"], ui)
        self.assertEqual(call_kwargs["chat_panel"], panel)
        self.assertEqual(call_kwargs["app_friendly"], "Minecraft Alpha")
        self.assertEqual(call_kwargs["app_stats"], runtime_stats)
        self.assertIsNone(call_kwargs["refresh_app_stats"])
        self.assertFalse(call_kwargs["show_header"])
        self.assertTrue(call_kwargs["embedded"])
        self.assertIs(call_kwargs["endpoint_count_label"], endpoint_count_label)
        self.assertIs(call_kwargs["endpoint_count_tooltip"], endpoint_count_tooltip)
        self.assertIs(call_kwargs["endpoint_count_tooltip_content"], endpoint_count_tooltip_content)
        self.assertNotIn("header_badges", call_kwargs)
        self.assertNotIn("popout_url", call_kwargs)

        apply_runtime_model(cast(ModWebBasePageModel, cast(object, SimpleNamespace(app_stats=runtime_stats))))

        apply_runtime_stats.assert_called_once_with(runtime_stats)

    def test_render_chat_page_card_includes_map_badge_link(self) -> None:
        class FakeContainer:
            class_value: str | None = None
            style_value: str | None = None

            def classes(self, value: str) -> "FakeContainer":
                self.class_value = value
                return self

            def style(self, value: str) -> "FakeContainer":
                self.style_value = value
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text
                self.class_value: str | None = None

            def classes(self, value: str) -> "FakeLabel":
                self.class_value = value
                return self

        class FakeUi:
            def card(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def label(self, text: str) -> FakeLabel:
                return FakeLabel(text)

        service = ModWebService()
        runtime_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=3,
            player_capacity=12,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            transition_state=NodeAppTransitionState.NONE,
        )
        initial_snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=1,
            events=(),
            endpoint_summaries=(NodeChatEndpointSummary(label="Game: Minecraft Alpha"),),
        )
        chat_surface = _ModWebChatSurfaceConfig(
            panel=_ModWebChatPanelConfig(
                initial_snapshot=initial_snapshot,
                refresh_snapshot=AsyncMock(return_value=initial_snapshot),
                send_message=None,
            ),
            node_name="yuki",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            app_stats=runtime_stats,
            hero_badges=(_ModWebBadgeSpec(text="Relay", tone="purple"),),
            refresh_app_stats=AsyncMock(return_value=runtime_stats),
            popout_url="/mod-web/chat/minecraft_alpha",
            map_url="https://example.invalid/squaremap/?world=minecraft_overworld",
        )
        ui = FakeUi()

        with (
            patch.object(ModWebService, "_render_app_node_badge") as render_app_node_badge,
            patch.object(
                ModWebService,
                "_render_chat_endpoint_badge",
                return_value=(cast(Any, object()), cast(Any, object()), cast(Any, object())),
            ) as render_chat_endpoint_badge,
            patch.object(ModWebService, "_badge", return_value=cast(Any, object())) as render_badge,
            patch.object(
                ModWebService,
                "_attach_html_tooltip",
                return_value=(cast(Any, object()), cast(Any, object())),
            ) as attach_html_tooltip,
            patch.object(ModWebService, "_set_optional_badge_state") as set_optional_badge_state,
            patch.object(ModWebService, "_badge_link") as render_badge_link,
            patch.object(ModWebService, "_render_chat_panel") as render_chat_panel,
        ):
            service._render_chat_page_card(ui=cast(ModWebUi, cast(object, ui)), chat_surface=chat_surface)

        render_app_node_badge.assert_called_once()
        render_chat_endpoint_badge.assert_called_once()
        self.assertGreaterEqual(render_badge.call_count, 3)
        attach_html_tooltip.assert_called_once()
        set_optional_badge_state.assert_called_once()
        render_badge_link.assert_called_once_with(
            ui=ui,
            text="Map",
            tone="purple",
            url="https://example.invalid/squaremap/?world=minecraft_overworld",
            new_tab=True,
        )
        render_chat_panel.assert_called_once()

    def test_render_flat_tab_header_omits_redundant_title_and_keeps_copy_only(self) -> None:
        class FakeContainer:
            def __init__(self) -> None:
                self.class_value: str | None = None

            def classes(self, value: str) -> "FakeContainer":
                self.class_value = value
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text: str = text
                self.class_value: str | None = None

            def classes(self, value: str) -> "FakeLabel":
                self.class_value = value
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.labels: list[FakeLabel] = []

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

        service = ModWebService()
        ui = FakeUi()

        description_label, secondary_label = service._render_flat_tab_header(
            ui=cast(ModWebUi, cast(object, ui)),
            title="Mods",
            description="Browse the indexed mod inventory.",
            secondary_description="Metadata is loaded from the selected file.",
        )

        self.assertEqual(
            [label.text for label in ui.labels],
            [
                "Browse the indexed mod inventory.",
                "Metadata is loaded from the selected file.",
            ],
        )
        self.assertEqual(description_label, ui.labels[0])
        self.assertEqual(secondary_label, ui.labels[1])
        self.assertEqual(ui.labels[0].class_value, "mod-subtitle text-sm w-full")
        self.assertEqual(ui.labels[1].class_value, "mod-subtitle text-xs w-full")

    def test_page_section_badges_provide_mods_chrome_without_body_header_badges(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_scope="minecraft",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=6,
                    enabled_count=5,
                    disabled_count=1,
                    coremod_count=2,
                    downloadable_count=4,
                    non_downloadable_count=2,
                ),
                mods=(),
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            client_pack_published_version="2026-07-04",
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        tab = service._page_tabs(model)[0]

        badges = service._page_section_badges(
            model=model,
            user=user,
            tab=tab,
        )

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="6 mods", tone="black"),
                _ModWebBadgeSpec(text="pack 2026-07-04", tone="grey"),
                _ModWebBadgeSpec(text="2 blocked", tone="warn"),
                _ModWebBadgeSpec(text="4 downloadable", tone="purple"),
                _ModWebBadgeSpec(text="2 coremods", tone="red"),
            ),
        )

    def test_mods_section_badges_omit_pack_version_for_apps_without_client_packs(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="factorio_alpha",
            app_friendly="Factorio Alpha",
            app_scope="factorio",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="factorio_alpha",
                app_friendly="Factorio Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="factorio_alpha",
                app_friendly="Factorio Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=1,
                    enabled_count=1,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=1,
                    non_downloadable_count=0,
                ),
                mods=(),
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            client_pack_published_version="2026-07-04",
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        tab = service._page_tabs(model)[0]

        self.assertEqual(
            service._page_section_badges(model=model, user=user, tab=tab),
            (
                _ModWebBadgeSpec(text="1 mod", tone="black"),
                _ModWebBadgeSpec(text="1 downloadable", tone="purple"),
            ),
        )

    def test_section_badge_rows_stagger_badges_from_the_right(self) -> None:
        badges = (
            _ModWebBadgeSpec(text="6 mods", tone="black"),
            _ModWebBadgeSpec(text="2 blocked", tone="warn"),
            _ModWebBadgeSpec(text="4 downloadable", tone="purple"),
            _ModWebBadgeSpec(text="2 coremods", tone="red"),
        )

        rows = ModWebService._section_badge_rows(badges)

        self.assertEqual(
            rows,
            (
                (
                    _ModWebBadgeSpec(text="6 mods", tone="black"),
                    _ModWebBadgeSpec(text="4 downloadable", tone="purple"),
                ),
                (
                    _ModWebBadgeSpec(text="2 blocked", tone="warn"),
                    _ModWebBadgeSpec(text="2 coremods", tone="red"),
                ),
            ),
        )

    def test_settings_section_badges_prioritise_editable_summary_on_the_top_row(self) -> None:
        service = ModWebService()
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=NodeSettingList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                editable_count=1,
                restricted_count=1,
                has_pending_changes=True,
                pending_change_count=1,
                required_save_level_name=Power_Level.user.name,
                required_reload_level_name=Power_Level.user.name,
                settings=(
                    self._setting_entry(
                        key="pvp",
                        label="PVP",
                        type_name="bool",
                        value_text="true",
                        default_text="true",
                        description="Whether players can hurt each other.",
                        can_edit=True,
                    ),
                    self._setting_entry(
                        key="difficulty",
                        label="Difficulty",
                        type_name="str",
                        value_text="hard",
                        default_text="normal",
                        description="Current world difficulty.",
                        can_edit=False,
                    ),
                ),
            ),
            console_actions=None,
        )

        badges = service._settings_section_badges(model=model)

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="2 settings", tone="black"),
                _ModWebBadgeSpec(text="1 drafts", tone="grey"),
                _ModWebBadgeSpec(text="1 editable", tone="purple"),
                _ModWebBadgeSpec(text="1 restricted", tone="warn"),
            ),
        )
        self.assertEqual(
            service._section_badge_rows(badges),
            (
                (
                    _ModWebBadgeSpec(text="2 settings", tone="black"),
                    _ModWebBadgeSpec(text="1 editable", tone="purple"),
                ),
                (
                    _ModWebBadgeSpec(text="1 drafts", tone="grey"),
                    _ModWebBadgeSpec(text="1 restricted", tone="warn"),
                ),
            ),
        )

    def test_remote_chat_stream_signal_maps_event_kinds(self) -> None:
        snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=1,
            events=(),
            endpoint_summaries=(NodeChatEndpointSummary(label="Game: Minecraft Alpha"),),
        )
        app_stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.1",
            player_count=3,
            player_capacity=12,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            footprint_bytes=None,
            transition_state=NodeAppTransitionState.NONE,
        )
        self.assertEqual(
            ModWebService._remote_chat_stream_signal(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.INITIAL,
                    room_id="minecraft_alpha",
                    snapshot=snapshot,
                    app_stats=app_stats,
                )
            ),
            _ModWebChatPanelSignal.both(snapshot=snapshot, app_stats=app_stats),
        )
        delta = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="hello",
        )
        self.assertEqual(
            ModWebService._remote_chat_stream_signal(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.CHAT_CHANGED,
                    room_id="minecraft_alpha",
                    events=(delta,),
                )
            ),
            _ModWebChatPanelSignal.chat(events=(delta,)),
        )
        self.assertEqual(
            ModWebService._remote_chat_stream_signal(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.CHAT_CHANGED,
                    room_id="minecraft_alpha",
                    snapshot=snapshot,
                )
            ),
            _ModWebChatPanelSignal.chat(snapshot=snapshot),
        )
        self.assertIsNone(
            ModWebService._remote_chat_stream_signal(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.RUNTIME_CHANGED,
                    room_id="minecraft_alpha",
                    app_stats=app_stats,
                ),
                include_runtime_updates=False,
            )
        )
        self.assertEqual(
            ModWebService._remote_chat_stream_signal(
                NodeChatStreamEvent(
                    kind=NodeChatStreamEventKind.RUNTIME_CHANGED,
                    room_id="minecraft_alpha",
                    snapshot=snapshot,
                    app_stats=app_stats,
                )
            ),
            _ModWebChatPanelSignal.both(snapshot=snapshot, app_stats=app_stats),
        )

    def test_remote_chat_stream_listener_emits_typed_updates(self) -> None:
        class _FakeWebSocket:
            def __init__(self, messages: list[object]) -> None:
                self.messages = messages

            async def __aenter__(self) -> "_FakeWebSocket":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def __aiter__(self) -> "_FakeWebSocket":
                return self

            async def __anext__(self) -> object:
                if not self.messages:
                    await asyncio.Event().wait()
                return self.messages.pop(0)

            def exception(self) -> None:
                return None

        class _FakeClientSession:
            def __init__(self, websocket: _FakeWebSocket) -> None:
                self.websocket = websocket
                self.ws_connect_calls: list[tuple[str, dict[str, str], float]] = []

            async def __aenter__(self) -> "_FakeClientSession":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def ws_connect(self, url: str, *, headers: dict[str, str], heartbeat: float) -> _FakeWebSocket:
                self.ws_connect_calls.append((url, headers, heartbeat))
                return self.websocket

        async def exercise() -> None:
            service = ModWebService()
            service._remote_token = Mock(return_value="stream-token")  # type: ignore[method-assign]
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            snapshot = NodeChatRoomSnapshot(
                room_id="minecraft_alpha",
                endpoint_count=1,
                events=(),
                endpoint_summaries=(NodeChatEndpointSummary(label="Game: Minecraft Alpha"),),
            )
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
                transition_state=NodeAppTransitionState.NONE,
            )
            websocket = _FakeWebSocket(
                [
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            NodeChatStreamEvent(
                                kind=NodeChatStreamEventKind.INITIAL,
                                room_id="minecraft_alpha",
                                snapshot=snapshot,
                                app_stats=app_stats,
                            ).to_mapping()
                        ),
                    ),
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            NodeChatStreamEvent(
                                kind=NodeChatStreamEventKind.RUNTIME_CHANGED,
                                room_id="minecraft_alpha",
                                snapshot=snapshot,
                                app_stats=app_stats,
                            ).to_mapping()
                        ),
                    ),
                ]
            )
            session = _FakeClientSession(websocket)
            updates: list[_ModWebChatPanelSignal] = []
            received_two_updates = asyncio.Event()

            def on_update(signal: _ModWebChatPanelSignal) -> None:
                updates.append(signal)
                if len(updates) >= 2:
                    received_two_updates.set()

            with patch("web_dash.chat.aiohttp.ClientSession", return_value=session):
                task = asyncio.create_task(
                    service._remote_chat_stream_listener(
                        node=node,
                        app_name="minecraft_alpha",
                        user=cast(Any, SimpleNamespace(discord_id=42)),
                        on_update=on_update,
                    )
                )
                try:
                    await asyncio.wait_for(received_two_updates.wait(), timeout=0.2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            self.assertEqual(
                updates,
                [
                    _ModWebChatPanelSignal.both(snapshot=snapshot, app_stats=app_stats),
                    _ModWebChatPanelSignal.both(snapshot=snapshot, app_stats=app_stats),
                ],
            )
            self.assertEqual(
                session.ws_connect_calls,
                [
                    (
                        "wss://erin.example/api/node/apps/minecraft_alpha/chat/stream",
                        {"Authorization": "Bearer stream-token"},
                        30.0,
                    )
                ],
            )

        asyncio.run(exercise())

    def test_remote_chat_stream_listener_stops_retrying_after_unsupported_websocket(self) -> None:
        class _FailingClientSession:
            def __init__(self) -> None:
                self.ws_connect_calls: list[tuple[str, dict[str, str], float]] = []

            async def __aenter__(self) -> "_FailingClientSession":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def ws_connect(self, url: str, *, headers: dict[str, str], heartbeat: float) -> object:
                self.ws_connect_calls.append((url, headers, heartbeat))
                raise aiohttp.WSServerHandshakeError(
                    _request_info(url),
                    (),
                    status=404,
                    message="Not Found",
                    headers=None,
                )

        async def exercise() -> None:
            service = ModWebService()
            service._remote_token = Mock(return_value="stream-token")  # type: ignore[method-assign]
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            session = _FailingClientSession()
            updates: list[_ModWebChatPanelSignal] = []

            with patch("web_dash.chat.aiohttp.ClientSession", return_value=session):
                await asyncio.wait_for(
                    service._remote_chat_stream_listener(
                        node=node,
                        app_name="minecraft_alpha",
                        user=cast(Any, SimpleNamespace(discord_id=42)),
                        on_update=updates.append,
                    ),
                    timeout=0.2,
                )

            self.assertEqual(updates, [])
            self.assertEqual(
                session.ws_connect_calls,
                [
                    (
                        "wss://erin.example/api/node/apps/minecraft_alpha/chat/stream",
                        {"Authorization": "Bearer stream-token"},
                        30.0,
                    )
                ],
            )

        asyncio.run(exercise())

    def test_remote_app_state_stream_listener_emits_updates(self) -> None:
        class _FakeWebSocket:
            def __init__(self, messages: list[object]) -> None:
                self.messages = messages

            async def __aenter__(self) -> "_FakeWebSocket":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def __aiter__(self) -> "_FakeWebSocket":
                return self

            async def __anext__(self) -> object:
                if not self.messages:
                    await asyncio.Event().wait()
                return self.messages.pop(0)

            def exception(self) -> None:
                return None

        class _FakeClientSession:
            def __init__(self, websocket: _FakeWebSocket) -> None:
                self.websocket = websocket
                self.ws_connect_calls: list[tuple[str, dict[str, str], float]] = []

            async def __aenter__(self) -> "_FakeClientSession":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def ws_connect(self, url: str, *, headers: dict[str, str], heartbeat: float) -> _FakeWebSocket:
                self.ws_connect_calls.append((url, headers, heartbeat))
                return self.websocket

        async def exercise() -> None:
            service = ModWebService()
            service._remote_token = Mock(return_value="stream-token")  # type: ignore[method-assign]
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
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
                transition_state=NodeAppTransitionState.NONE,
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
            )
            websocket = _FakeWebSocket(
                [
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            NodeAppStateStreamEvent.initial(
                                app_name="minecraft_alpha",
                                app_stats=app_stats,
                                system_summary=system_summary,
                            ).to_mapping()
                        ),
                    ),
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            NodeAppStateStreamEvent.runtime(
                                app_name="minecraft_alpha",
                                app_stats=app_stats,
                            ).to_mapping()
                        ),
                    ),
                ]
            )
            session = _FakeClientSession(websocket)
            updates: list[NodeAppStateStreamEvent] = []
            update_seen = asyncio.Event()

            def on_update(event: NodeAppStateStreamEvent) -> None:
                updates.append(event)
                if len(updates) >= 2:
                    update_seen.set()

            with patch("web_dash.streams.aiohttp.ClientSession", return_value=session):
                task = asyncio.create_task(
                    service._remote_app_state_stream_listener(
                        node=node,
                        app_name="minecraft_alpha",
                        user=cast(Any, SimpleNamespace(discord_id=42)),
                        on_update=on_update,
                    )
                )
                try:
                    await asyncio.wait_for(update_seen.wait(), timeout=0.2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            self.assertEqual(
                updates,
                [
                    NodeAppStateStreamEvent.initial(
                        app_name="minecraft_alpha",
                        app_stats=app_stats,
                        system_summary=system_summary,
                    ),
                    NodeAppStateStreamEvent.runtime(
                        app_name="minecraft_alpha",
                        app_stats=app_stats,
                    ),
                ],
            )
            self.assertEqual(
                session.ws_connect_calls,
                [
                    (
                        "wss://erin.example/api/node/apps/minecraft_alpha/state/stream",
                        {"Authorization": "Bearer stream-token"},
                        30.0,
                    )
                ],
            )

        asyncio.run(exercise())

    def test_remote_polled_app_state_event_keeps_runtime_updates_when_system_summary_is_unavailable(self) -> None:
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
            transition_state=NodeAppTransitionState.NONE,
        )

        event = ModWebService._remote_polled_app_state_event(
            app_name="minecraft_alpha",
            app_stats=app_stats,
            system_summary=None,
            previous_app_stats=None,
            update_info=None,
            update_status=None,
            previous_system_summary=NodeSystemSummary(
                cpu_percent=20,
                ram_percent=30,
                ram_used_bytes=3,
                ram_total_bytes=10,
                storage_percent=40,
                storage_free_bytes=20,
                storage_total_bytes=30,
            ),
            previous_update_info=None,
            previous_update_status=None,
        )

        self.assertEqual(
            event,
            NodeAppStateStreamEvent.runtime(
                app_name="minecraft_alpha",
                app_stats=app_stats,
            ),
        )

    def test_remote_app_state_stream_listener_falls_back_to_polling_after_unsupported_websocket(self) -> None:
        class _FailingClientSession:
            def __init__(self) -> None:
                self.ws_connect_calls: list[tuple[str, dict[str, str], float]] = []

            async def __aenter__(self) -> "_FailingClientSession":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def ws_connect(self, url: str, *, headers: dict[str, str], heartbeat: float) -> object:
                self.ws_connect_calls.append((url, headers, heartbeat))
                raise aiohttp.WSServerHandshakeError(
                    _request_info(url),
                    (),
                    status=404,
                    message="Not Found",
                    headers=None,
                )

        async def exercise() -> None:
            service = ModWebService()
            service._remote_token = Mock(return_value="stream-token")  # type: ignore[method-assign]
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
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
                transition_state=NodeAppTransitionState.NONE,
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
            )
            service._remote_app_runtime_summary_async = AsyncMock(return_value=app_stats)  # type: ignore[method-assign]
            service._remote_app_entry_async = AsyncMock(  # type: ignore[method-assign]
                return_value=SimpleNamespace(update_info=None, update_status=None)
            )
            service._remote_node_system_summary_or_none_async = AsyncMock(return_value=system_summary)  # type: ignore[method-assign]
            session = _FailingClientSession()
            updates: list[NodeAppStateStreamEvent] = []
            update_seen = asyncio.Event()

            def on_update(event: NodeAppStateStreamEvent) -> None:
                updates.append(event)
                update_seen.set()

            with (
                patch("web_dash.streams.aiohttp.ClientSession", return_value=session),
                patch("web_dash.streams._APP_RUNTIME_REFRESH_INTERVAL_SECONDS", 0.01),
            ):
                task = asyncio.create_task(
                    service._remote_app_state_stream_listener(
                        node=node,
                        app_name="minecraft_alpha",
                        user=cast(Any, SimpleNamespace(discord_id=42)),
                        on_update=on_update,
                    )
                )
                try:
                    await asyncio.wait_for(update_seen.wait(), timeout=0.2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            self.assertEqual(
                updates,
                [
                    NodeAppStateStreamEvent.both(
                        app_name="minecraft_alpha",
                        app_stats=app_stats,
                        system_summary=system_summary,
                    )
                ],
            )
            self.assertEqual(
                session.ws_connect_calls,
                [
                    (
                        "wss://erin.example/api/node/apps/minecraft_alpha/state/stream",
                        {"Authorization": "Bearer stream-token"},
                        30.0,
                    )
                ],
            )

        asyncio.run(exercise())

    def test_remote_node_state_stream_listener_emits_updates(self) -> None:
        class _FakeWebSocket:
            def __init__(self, messages: list[object]) -> None:
                self.messages = messages

            async def __aenter__(self) -> "_FakeWebSocket":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def __aiter__(self) -> "_FakeWebSocket":
                return self

            async def __anext__(self) -> object:
                if not self.messages:
                    await asyncio.Event().wait()
                return self.messages.pop(0)

            def exception(self) -> None:
                return None

        class _FakeClientSession:
            def __init__(self, websocket: _FakeWebSocket) -> None:
                self.websocket = websocket
                self.ws_connect_calls: list[tuple[str, dict[str, str], float]] = []

            async def __aenter__(self) -> "_FakeClientSession":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def ws_connect(self, url: str, *, headers: dict[str, str], heartbeat: float) -> _FakeWebSocket:
                self.ws_connect_calls.append((url, headers, heartbeat))
                return self.websocket

        async def exercise() -> None:
            service = ModWebService()
            service._remote_token = Mock(return_value="stream-token")  # type: ignore[method-assign]
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            app_entry = NodeAppEntry(
                name="minecraft_alpha",
                friendly="Minecraft Alpha",
                node="erin",
                running=True,
                enabled=True,
                supports_mods=True,
                supports_configs=True,
                transition_state=NodeAppTransitionState.NONE,
                player_count=1,
                player_capacity=8,
                supports_saves=True,
                supports_save_uploads=True,
                supports_save_rename=True,
                supports_settings=True,
                supports_chat=True,
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
            )
            websocket = _FakeWebSocket(
                [
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            NodeStateStreamEvent.initial(
                                node_name="erin",
                                app_entries=(app_entry,),
                                system_summary=system_summary,
                            ).to_mapping()
                        ),
                    ),
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            NodeStateStreamEvent.apps(
                                node_name="erin",
                                app_entries=(app_entry,),
                            ).to_mapping()
                        ),
                    ),
                ]
            )
            session = _FakeClientSession(websocket)
            updates: list[NodeStateStreamEvent] = []
            update_seen = asyncio.Event()

            def on_update(event: NodeStateStreamEvent) -> None:
                updates.append(event)
                if len(updates) >= 2:
                    update_seen.set()

            with patch("web_dash.streams.aiohttp.ClientSession", return_value=session):
                task = asyncio.create_task(
                    service._remote_node_state_stream_listener(
                        node=node,
                        user=cast(Any, SimpleNamespace(discord_id=42)),
                        on_update=on_update,
                    )
                )
                try:
                    await asyncio.wait_for(update_seen.wait(), timeout=0.2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            self.assertEqual(
                updates,
                [
                    NodeStateStreamEvent.initial(
                        node_name="erin",
                        app_entries=(app_entry,),
                        system_summary=system_summary,
                    ),
                    NodeStateStreamEvent.apps(
                        node_name="erin",
                        app_entries=(app_entry,),
                    ),
                ],
            )
            self.assertEqual(
                session.ws_connect_calls,
                [
                    (
                        "wss://erin.example/api/node/state/stream",
                        {"Authorization": "Bearer stream-token"},
                        30.0,
                    )
                ],
            )

        asyncio.run(exercise())

    def test_remote_polled_node_state_event_keeps_app_updates_when_system_summary_is_unavailable(self) -> None:
        app_entry = NodeAppEntry(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node="erin",
            running=True,
            enabled=True,
            supports_mods=True,
            supports_configs=True,
        )

        event = ModWebService._remote_polled_node_state_event(
            node_name="erin",
            app_entries=(app_entry,),
            system_summary=None,
            previous_app_entries=None,
            previous_system_summary=NodeSystemSummary(
                cpu_percent=20,
                ram_percent=30,
                ram_used_bytes=3,
                ram_total_bytes=10,
                storage_percent=40,
                storage_free_bytes=20,
                storage_total_bytes=30,
            ),
        )

        self.assertEqual(
            event,
            NodeStateStreamEvent.apps(
                node_name="erin",
                app_entries=(app_entry,),
            ),
        )

    def test_remote_node_state_stream_listener_falls_back_to_polling_after_unsupported_websocket(self) -> None:
        class _FailingClientSession:
            def __init__(self) -> None:
                self.ws_connect_calls: list[tuple[str, dict[str, str], float]] = []

            async def __aenter__(self) -> "_FailingClientSession":
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def ws_connect(self, url: str, *, headers: dict[str, str], heartbeat: float) -> object:
                self.ws_connect_calls.append((url, headers, heartbeat))
                raise aiohttp.WSServerHandshakeError(
                    _request_info(url),
                    (),
                    status=404,
                    message="Not Found",
                    headers=None,
                )

        async def exercise() -> None:
            service = ModWebService()
            service._remote_token = Mock(return_value="stream-token")  # type: ignore[method-assign]
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            app_entry = NodeAppEntry(
                name="minecraft_alpha",
                friendly="Minecraft Alpha",
                node="erin",
                running=True,
                enabled=True,
                supports_mods=True,
                supports_configs=True,
                transition_state=NodeAppTransitionState.NONE,
                player_count=1,
                player_capacity=8,
                supports_saves=True,
                supports_save_uploads=True,
                supports_save_rename=True,
                supports_settings=True,
                supports_chat=True,
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
            )
            service._remote_apps_async = AsyncMock(return_value=(app_entry,))  # type: ignore[method-assign]
            service._remote_node_system_summary_or_none_async = AsyncMock(return_value=system_summary)  # type: ignore[method-assign]
            session = _FailingClientSession()
            updates: list[NodeStateStreamEvent] = []
            update_seen = asyncio.Event()

            def on_update(event: NodeStateStreamEvent) -> None:
                updates.append(event)
                update_seen.set()

            with (
                patch("web_dash.streams.aiohttp.ClientSession", return_value=session),
                patch("web_dash.streams._APP_RUNTIME_REFRESH_INTERVAL_SECONDS", 0.01),
            ):
                task = asyncio.create_task(
                    service._remote_node_state_stream_listener(
                        node=node,
                        user=cast(Any, SimpleNamespace(discord_id=42)),
                        on_update=on_update,
                    )
                )
                try:
                    await asyncio.wait_for(update_seen.wait(), timeout=0.2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            self.assertEqual(
                updates,
                [
                    NodeStateStreamEvent.both(
                        node_name="erin",
                        app_entries=(app_entry,),
                        system_summary=system_summary,
                    )
                ],
            )
            self.assertEqual(
                session.ws_connect_calls,
                [
                    (
                        "wss://erin.example/api/node/state/stream",
                        {"Authorization": "Bearer stream-token"},
                        30.0,
                    )
                ],
            )

        asyncio.run(exercise())

    def test_chat_stream_fallback_loop_polls_while_stream_is_unhealthy(self) -> None:
        async def exercise() -> None:
            updates: list[_ModWebChatPanelSignal] = []
            wakeup = asyncio.Event()
            stream_healthy = False
            update_seen = asyncio.Event()

            def on_update(signal: _ModWebChatPanelSignal) -> None:
                updates.append(signal)
                update_seen.set()

            task = asyncio.create_task(
                ModWebService._chat_stream_fallback_loop(
                    fallback_signal=_ModWebChatPanelSignal.both(),
                    is_stream_healthy=lambda: stream_healthy,
                    on_update=on_update,
                    wakeup=wakeup,
                    refresh_interval_seconds=0.01,
                )
            )
            try:
                await asyncio.wait_for(update_seen.wait(), timeout=0.2)
                self.assertEqual(updates, [_ModWebChatPanelSignal.both()])
                stream_healthy = True
                wakeup.set()
                await asyncio.sleep(0.03)
                self.assertEqual(len(updates), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(exercise())

    def test_set_badge_state_replaces_badge_text_tone_and_visibility(self) -> None:
        label = Mock()

        ModWebService._set_badge_state(label, "Running", "purple")

        label.set_text.assert_called_once_with("Running")
        label.classes.assert_called_once_with(replace="mod-badge purple")
        label.style.assert_called_once_with(remove="display: none;")

    def test_set_optional_badge_state_hides_badge_when_no_snapshot_exists(self) -> None:
        label = Mock()

        ModWebService._set_optional_badge_state(label, None)

        label.style.assert_called_once_with(add="display: none;")
        label.set_text.assert_not_called()
        label.classes.assert_not_called()

    def test_chat_endpoint_count_tooltip_lists_endpoint_summaries(self) -> None:
        snapshot = NodeChatRoomSnapshot(
            room_id="minecraft_alpha",
            endpoint_count=3,
            events=(),
            endpoint_summaries=(
                NodeChatEndpointSummary(label="Game: Minecraft Alpha"),
                NodeChatEndpointSummary(label="Discord: Friends"),
                NodeChatEndpointSummary(label="Discord: Builders"),
            ),
        )

        self.assertEqual(ModWebService._chat_endpoint_count_text(snapshot), "3 endpoints")
        self.assertEqual(
            ModWebService._chat_endpoint_count_tooltip(snapshot),
            "Game: Minecraft Alpha<br>Discord: Friends<br>Discord: Builders",
        )

    def test_chat_event_groups_merge_consecutive_messages_from_same_author_and_source(self) -> None:
        base_source = ChatEndpointId.web_session("session-1")
        first = ChatEvent(
            room_id="minecraft_alpha",
            source=base_source,
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Tester"),
            content="one",
            created_at=100.0,
        )
        second = ChatEvent(
            room_id="minecraft_alpha",
            source=base_source,
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Tester"),
            content="two",
            created_at=160.0,
        )
        third = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Tester"),
            content="three",
            created_at=180.0,
        )

        groups = ModWebService._chat_event_groups((first, second, third))

        self.assertEqual([len(group.events) for group in groups], [2, 1])
        self.assertEqual(groups[0].head_event, first)
        self.assertEqual(groups[0].events, (first, second))

    def test_chat_event_groups_keep_system_and_notice_events_separate(self) -> None:
        first = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
            created_at=100.0,
        )
        second = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(kind=ChatAuthorKind.SYSTEM, display_name="System"),
            content="Yoko left Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.LEFT,
                source=RelayNoticeSource.APP_LOG,
            ),
            created_at=110.0,
        )

        groups = ModWebService._chat_event_groups((first, second))

        self.assertEqual([len(group.events) for group in groups], [1, 1])

    def test_chat_history_append_count_handles_trimmed_history(self) -> None:
        appended_count = ModWebService._chat_history_append_count(("a", "b", "c"), ("b", "c", "d", "e"))

        self.assertEqual(appended_count, 2)

    def test_chat_client_script_tracks_pre_refresh_scroll_state_and_media(self) -> None:
        script = ModWebService._chat_client_script()

        self.assertIn("beforeRefresh", script)
        self.assertIn("attachMediaListeners", script)
        self.assertIn("openMediaLinkInNewTab", script)
        self.assertIn("a.mod-chat-media-link[href]", script)
        self.assertIn("window.open(link.href, '_blank', 'noopener,noreferrer')", script)
        self.assertIn("observeTimelineMutations", script)
        self.assertIn("jumpStateByTimeline", script)
        self.assertIn("clearScheduledJump", script)
        self.assertIn("hiddenMessageCount", script)
        self.assertIn("mod-chat-entry-live", script)
        self.assertIn("mod-chat-unread-live", script)
        self.assertIn("modChatWasPinned", script)
        self.assertIn("modChatHiddenCount", script)
        self.assertIn("autoScrollHiddenMessageLimit = 3", script)
        self.assertIn("shouldAutoScrollAfterRefresh", script)
        self.assertIn("timeline._modChatMutationObserver", script)
        self.assertIn("observer.observe(timeline, {childList: true, subtree: true})", script)
        self.assertIn("loadedmetadata", script)
        self.assertIn("ResizeObserver", script)
        self.assertIn("setTimeout(settle, 320)", script)

    def test_chat_reference_label_reflects_reference_kind(self) -> None:
        service = ModWebService()
        reference = ChatMessageReference(author_display_name="Yoko", content="hello")

        self.assertEqual(
            service._chat_reference_label(
                ChatReferenceKind.REPLY,
                reference,
                room_id="minecraft_alpha",
                preferred_guild_id=None,
            ),
            "Replying to Yoko",
        )
        self.assertEqual(
            service._chat_reference_label(
                ChatReferenceKind.FORWARD,
                reference,
                room_id="minecraft_alpha",
                preferred_guild_id=None,
            ),
            "Forwarded from Yoko",
        )

    def test_remote_download_url_points_at_owning_node_with_scoped_token(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="yuki", token_secret="shared-secret")
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with patch.object(config, "MOD_WEB_SERVER", server):
            url = ModWebService()._remote_download_url(
                node=node,
                app_name="minecraft alpha",
                path="/apps/minecraft%20alpha/mods/download",
                query={"enabled_only": "true", "mod_name": ["one.jar", "two.jar"]},
                user=user,
            )

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]
        grant = verify_node_token(
            secret="shared-secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.MODS_DOWNLOAD,),
        )

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "erin.example")
        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/mods/download")
        self.assertEqual(query["enabled_only"], ["true"])
        self.assertEqual(query["mod_name"], ["one.jar", "two.jar"])
        self.assertEqual(grant.subject, "web:42")

    def test_direct_upload_targets_point_at_node_with_scoped_tokens(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="yuki", token_secret="shared-secret")
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="minecraft alpha", app_friendly="Minecraft Alpha", node="erin", configs=()),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with (
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_remote_node_link", return_value=node),
        ):
            service = ModWebService()
            mod_target = service._direct_mod_upload_target(model=model, user=user)
            save_target = service._direct_save_upload_target(model=model, user=user)

        mod_grant = verify_node_token(
            secret="shared-secret",
            token=mod_target.authorization_header.removeprefix("Bearer "),
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.MODS_WRITE,),
        )
        save_grant = verify_node_token(
            secret="shared-secret",
            token=save_target.authorization_header.removeprefix("Bearer "),
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.SAVES_WRITE,),
        )

        self.assertEqual(
            mod_target.url,
            "https://erin.example/api/node/apps/minecraft%20alpha/mods/upload?placement=server_enabled",
        )
        self.assertEqual(
            save_target.url,
            "https://erin.example/api/node/apps/minecraft%20alpha/saves/upload",
        )
        self.assertEqual(mod_grant.subject, "web:42")
        self.assertEqual(save_grant.subject, "web:42")

    def test_download_query_preserves_pack_parameters(self) -> None:
        query = ModWebService._download_query(
            enabled_only=False,
            selected_only=True,
            mod_names=("client.jar",),
            client_pack=True,
            pack_purpose=PackPurpose.CLIENT,
            pack_format=PackFormat.MODRINTH,
        )

        self.assertEqual(
            query,
            {
                "enabled_only": "false",
                "selected_only": "true",
                "client_pack": "true",
                "pack_purpose": "client",
                "pack_format": "mrpack",
                "include_kubejs_scripts": "true",
                "include_servers_dat": "true",
                "include_options_txt": "true",
                "mod_name": ["client.jar"],
            },
        )

    def test_download_query_can_exclude_generated_client_files_from_client_pack(self) -> None:
        query = ModWebService._download_query(
            enabled_only=False,
            selected_only=True,
            mod_names=("client.jar",),
            pack_purpose=PackPurpose.CLIENT,
            include_kubejs_scripts=False,
            include_servers_dat=False,
            include_options_txt=False,
        )

        self.assertEqual(query["include_kubejs_scripts"], "false")
        self.assertEqual(query["include_servers_dat"], "false")
        self.assertEqual(query["include_options_txt"], "false")

    def test_download_query_supports_compact_excluded_mod_selection(self) -> None:
        query = ModWebService._download_query(
            enabled_only=False,
            selected_only=True,
            excluded_only=True,
            mod_names=("not-selected.jar",),
        )

        self.assertEqual(
            query,
            {
                "enabled_only": "false",
                "selected_only": "true",
                "excluded_only": "true",
                "mod_name": ["not-selected.jar"],
            },
        )

    def test_server_and_admin_pack_downloads_require_sudo(self) -> None:
        self.assertIs(ModWebService._mod_download_required_level(None), Power_Level.visitor)
        self.assertIs(ModWebService._mod_download_required_level(PackPurpose.CLIENT), Power_Level.visitor)
        self.assertIs(ModWebService._mod_download_required_level(PackPurpose.SERVER), Power_Level.sudo)
        self.assertIs(ModWebService._mod_download_required_level(PackPurpose.ADMIN), Power_Level.sudo)

    def test_config_root_download_url_uses_configs_read_scope(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="yuki", token_secret="shared-secret")
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.visitor,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="minecraft alpha", app_friendly="Minecraft Alpha", node="erin", configs=()),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with (
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_remote_node_link", return_value=node),
        ):
            url = ModWebService()._config_root_download_url(model=model, root_id="mod-configs", user=user)

        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        token = query["access_token"][0]
        grant = verify_node_token(
            secret="shared-secret",
            token=token,
            node="erin",
            app="minecraft alpha",
            required_scopes=(NodeApiScope.CONFIGS_READ,),
        )

        self.assertEqual(parsed.path, "/api/node/apps/minecraft%20alpha/configs/roots/mod-configs/download")
        self.assertEqual(grant.subject, "web:42")

    def test_download_feedback_message_formats_selected_downloads(self) -> None:
        message = ModWebService._download_feedback_message(
            kind=ModDownloadKind.SELECTED,
            app_friendly="Minecraft Alpha",
            selected_count=2,
        )

        self.assertEqual(message, "Preparing download for 2 selected mods from Minecraft Alpha.")

    def test_download_feedback_message_requires_single_mod_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "mod_friendly"):
            ModWebService._download_feedback_message(
                kind=ModDownloadKind.SINGLE,
                app_friendly="Minecraft Alpha",
            )

    def test_client_pack_download_feedback_is_specific(self) -> None:
        self.assertEqual(
            ModWebService._download_feedback_message(
                kind=ModDownloadKind.CLIENT_PACK,
                app_friendly="Minecraft Alpha",
            ),
            "Preparing client pack for Minecraft Alpha.",
        )

    def test_mod_download_row_builds_detail_dialog_only_when_first_opened(self) -> None:
        service = ModWebService()
        ui = MagicMock()
        row = MagicMock()
        ui.row.return_value.classes.return_value = row
        dialog = MagicMock()
        entry = self._mod_entry(name="alpha.jar", friendly="Alpha")
        model = cast(ModWebPageModel, object())
        user = cast(ModWebUser, object())

        with patch.object(service, "_render_mod_info_dialog", return_value=dialog) as render_dialog:
            service._render_mod_download_row(
                ui=cast(ModWebUi, ui),
                entry=entry,
                download_url=None,
                on_change=Mock(),
                can_select=True,
                app_friendly="Minecraft Alpha",
                model=model,
                user=user,
            )

            render_dialog.assert_not_called()
            open_dialog = row.on.call_args_list[0].args[1]
            open_dialog(None)
            open_dialog(None)

        render_dialog.assert_called_once_with(
            ui=ui,
            entry=entry,
            model=model,
            user=user,
        )
        self.assertEqual(dialog.open.call_count, 2)

    def test_mod_download_row_client_mod_uses_default_row_border_classes(self) -> None:
        service = ModWebService()
        ui = MagicMock()
        row = MagicMock()
        ui.row.return_value.classes.return_value = row
        entry = self._mod_entry(
            name="alpha-client.jar",
            friendly="Alpha Client",
            mod_type=ModType.CLIENT,
            placement=ModPlacement.CLIENT_ONLY,
        )

        service._render_mod_download_row(
            ui=cast(ModWebUi, ui),
            entry=entry,
            download_url=None,
            on_change=Mock(),
            can_select=True,
            app_friendly="Minecraft Alpha",
            model=cast(ModWebPageModel, object()),
            user=cast(ModWebUser, object()),
        )

        row_classes = ui.row.return_value.classes.call_args_list[0].args[0]
        self.assertNotIn("mod-row-client-only", row_classes)
        self.assertIn("mod-row-clickable", row_classes)

    def test_large_mod_list_uses_virtual_scroll_table(self) -> None:
        service = ModWebService()
        mods = tuple(
            self._mod_entry(name=f"mod-{index}.jar", friendly=f"Mod {index}")
            for index in range(50)
        )
        model = ModWebPageModel(
            node_name="yuki",
            app_name="factorio_alpha",
            app_friendly="Factorio Alpha",
            app_color_hex=None,
            app_scope="factorio",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="factorio_alpha",
                app_friendly="Factorio Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=self._mod_list(app_name="factorio_alpha", mods=mods),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={mod.name: f"/mods/{mod.name}" for mod in mods},
        )
        ui = MagicMock()
        ui.refreshable.side_effect = lambda function: function
        select = MagicMock()
        select.props.return_value.classes.return_value = select
        select.value = ModWebModlistFormat.PLAINTEXT.value
        ui.select.return_value = select
        table = MagicMock()
        table.props.return_value.classes.return_value = table
        table.rows = []
        table.selected = []
        ui.table.return_value = table
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        dialog = MagicMock()

        with (
            patch.object(service, "_user_has_level", return_value=False),
            patch.object(service, "_render_flat_tab_header"),
            patch.object(service, "_render_mod_info_dialog", return_value=dialog) as render_mod_dialog,
            patch.object(
                service,
                "_render_mod_toolbar",
                return_value=SimpleNamespace(
                    selection_button=None,
                    download_button=None,
                    delete_control=None,
                    result_count_label=None,
                    metadata_status_button=None,
                ),
            ),
            patch.object(service, "_render_mod_download_row") as render_standard_row,
        ):
            service._render_mods_section(ui=cast(ModWebUi, ui), model=model, user=user)
            click_handler = table.on.call_args.args[1]
            asyncio.run(click_handler(SimpleNamespace(args={"action": "details", "name": mods[0].name})))

        ui.table.assert_called_once()
        self.assertEqual(len(ui.table.call_args.kwargs["rows"]), 50)
        self.assertIn("virtual-scroll", table.props.call_args.args[0])
        self.assertIn("hide-bottom", table.props.call_args.args[0])
        virtual_row_template = table.add_slot.call_args.args[1]
        self.assertIn("['mod-row', 'mod-row-clickable', props.row.state_class]", virtual_row_template)
        self.assertIn(':data-mod-name="props.row.name"', virtual_row_template)
        self.assertIn("data-mod-download", virtual_row_template)
        self.assertNotIn("$parent.$emit", virtual_row_template)
        self.assertIn('class="mod-pill size"', virtual_row_template)
        self.assertIn("'mod-setting-badge', 'mod-mod-type-badge'", virtual_row_template)
        table.on.assert_called_once()
        self.assertEqual(table.on.call_args.args[0], "click")
        render_mod_dialog.assert_called_once_with(ui=ui, entry=mods[0], model=model, user=user)
        dialog.open.assert_called_once()
        render_standard_row.assert_not_called()
        scroll_javascript = cast(str, ui.run_javascript.call_args.args[0])
        self.assertIn('window.sessionStorage.getItem(storageKey)', scroll_javascript)
        self.assertIn('window.sessionStorage.setItem(storageKey, String(position))', scroll_javascript)
        self.assertIn('mod-web:mods-scroll:yuki:factorio_alpha', scroll_javascript)

    def test_large_mod_list_client_mod_rows_use_default_state_class(self) -> None:
        service = ModWebService()
        mods = tuple(
            self._mod_entry(
                name=f"client-mod-{index}.jar",
                friendly=f"Client Mod {index}",
                mod_type=ModType.CLIENT,
                placement=ModPlacement.CLIENT_ONLY,
            )
            for index in range(50)
        )
        model = ModWebPageModel(
            node_name="yuki",
            app_name="factorio_alpha",
            app_friendly="Factorio Alpha",
            app_color_hex=None,
            app_scope="factorio",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="factorio_alpha",
                app_friendly="Factorio Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=self._mod_list(app_name="factorio_alpha", mods=mods),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={mod.name: f"/mods/{mod.name}" for mod in mods},
        )
        ui = MagicMock()
        ui.refreshable.side_effect = lambda function: function
        select = MagicMock()
        select.props.return_value.classes.return_value = select
        select.value = ModWebModlistFormat.PLAINTEXT.value
        ui.select.return_value = select
        table = MagicMock()
        table.props.return_value.classes.return_value = table
        table.rows = []
        table.selected = []
        ui.table.return_value = table
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=False),
            patch.object(service, "_render_flat_tab_header"),
            patch.object(
                service,
                "_render_mod_toolbar",
                return_value=SimpleNamespace(
                    selection_button=None,
                    download_button=None,
                    delete_control=None,
                    result_count_label=None,
                    metadata_status_button=None,
                ),
            ),
            patch.object(service, "_render_mod_download_row"),
        ):
            service._render_mods_section(ui=cast(ModWebUi, ui), model=model, user=user)

        rows = cast(list[dict[str, object]], ui.table.call_args.kwargs["rows"])
        self.assertTrue(rows)
        self.assertTrue(all(row["state_class"] == "" for row in rows))

    def test_filter_mod_entries_matches_name_version_and_state_tokens(self) -> None:
        service = ModWebService()
        mods = (
            self._mod_entry(name="alpha-fabric.jar", friendly="Alpha Fabric", version="2.1.0"),
            self._mod_entry(
                name="beta-core.jar",
                friendly="Beta Core",
                enabled=False,
                coremod=True,
                mod_type=ModType.COREMOD,
                downloadable=False,
                download_block_reason="builtin",
                download_block_label="Built-in",
            ),
        )
        options = service._mod_options(mods)

        self.assertEqual(
            service._filter_mod_entries(mods=mods, options=options, search_query="fabric 2.1"),
            (mods[0],),
        )
        self.assertEqual(
            service._filter_mod_entries(mods=mods, options=options, search_query="beta disabled blocked"),
            (mods[1],),
        )
        self.assertEqual(service._filter_mod_entries(mods=mods, options=options, search_query="missing"), ())

    def test_sort_mod_entries_defaults_can_use_newest_first_and_support_all_orders(self) -> None:
        service = ModWebService()
        alpha = self._mod_entry(
            name="alpha.jar",
            friendly="Alpha",
            added="2026-06-01 12:00:00",
            size_bytes=50,
            mod_type=ModType.CLIENT,
        )
        beta = self._mod_entry(
            name="beta.jar",
            friendly="Beta",
            added="2026-06-03 12:00:00",
            size_bytes=10,
            mod_type=ModType.REGULAR,
        )
        gamma = self._mod_entry(
            name="gamma.jar",
            friendly="Gamma",
            added="2026-06-02 12:00:00",
            size_bytes=100,
            mod_type=ModType.SERVER,
        )
        mods = (alpha, beta, gamma)

        self.assertEqual(
            service._sort_mod_entries(mods, ModWebModSortOrder.NEWEST),
            (beta, gamma, alpha),
        )
        self.assertEqual(
            service._sort_mod_entries(mods, ModWebModSortOrder.OLDEST),
            (alpha, gamma, beta),
        )
        self.assertEqual(
            service._sort_mod_entries(mods, ModWebModSortOrder.NAME_DESCENDING),
            (gamma, beta, alpha),
        )
        self.assertEqual(
            service._sort_mod_entries(mods, ModWebModSortOrder.SIZE_DESCENDING),
            (gamma, alpha, beta),
        )
        self.assertEqual(
            service._sort_mod_entries(mods, ModWebModSortOrder.TYPE),
            (beta, alpha, gamma),
        )

    def test_sort_file_entries_supports_modified_name_and_size_orders(self) -> None:
        service = ModWebService()
        alpha = NodeSaveEntry(
            id="worlds/alpha.zip",
            label="Alpha.zip",
            relative_path="alpha.zip",
            root_id="worlds",
            root_label="Worlds",
            kind="file",
            size_bytes=50,
            size_text="50B",
            modified_at="2026-06-01 12:00:00",
        )
        beta = NodeSaveEntry(
            id="worlds/beta.zip",
            label="Beta.zip",
            relative_path="beta.zip",
            root_id="worlds",
            root_label="Worlds",
            kind="file",
            size_bytes=10,
            size_text="10B",
            modified_at="2026-06-03 12:00:00",
        )
        gamma = NodeSaveEntry(
            id="worlds/gamma.zip",
            label="Gamma.zip",
            relative_path="gamma.zip",
            root_id="worlds",
            root_label="Worlds",
            kind="file",
            size_bytes=100,
            size_text="100B",
            modified_at="2026-06-02 12:00:00",
        )
        entries = (alpha, beta, gamma)

        self.assertEqual(
            service._sort_file_entries(entries, ModWebFileSortOrder.LATEST_MODIFIED),
            (beta, gamma, alpha),
        )
        self.assertEqual(
            service._sort_file_entries(entries, ModWebFileSortOrder.OLDEST_MODIFIED),
            (alpha, gamma, beta),
        )
        self.assertEqual(
            service._sort_file_entries(entries, ModWebFileSortOrder.NAME_DESCENDING),
            (gamma, beta, alpha),
        )
        self.assertEqual(
            service._sort_file_entries(entries, ModWebFileSortOrder.SIZE_DESCENDING),
            (gamma, alpha, beta),
        )

    def test_render_blueprints_editor_defaults_to_alphabetical_sort(self) -> None:
        class FakeRefreshable:
            def __init__(self, function: Callable[[str], None]) -> None:
                self._function = function

            def __call__(self, search_query: str) -> None:
                self._function(search_query)

            def refresh(self, search_query: str) -> None:
                self._function(search_query)

        alpha = NodeBlueprintEntry(
            id="Session Alpha/Alpha.sbp",
            label="Alpha.sbp",
            session_name="Session Alpha",
            relative_path="Session Alpha/Alpha.sbp",
            size_bytes=10,
            size_text="10B",
            modified_at="2026-06-01 12:00:00",
            uploaded_by_display_name=None,
            can_delete=True,
        )
        zulu = NodeBlueprintEntry(
            id="Session Alpha/Zulu.sbp",
            label="Zulu.sbp",
            session_name="Session Alpha",
            relative_path="Session Alpha/Zulu.sbp",
            size_bytes=20,
            size_text="20B",
            modified_at="2026-06-03 12:00:00",
            uploaded_by_display_name=None,
            can_delete=True,
        )
        model = cast(
            ModWebBasePageModel,
            cast(
                object,
                SimpleNamespace(
                    app_friendly="Satisfactory",
                    search_query="",
                    blueprints=NodeBlueprintList(
                        app_name="satisfactory",
                        app_friendly="Satisfactory",
                        node="yuki",
                        default_session_name="Session Alpha",
                        blueprints=(zulu, alpha),
                    ),
                ),
            ),
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        ui = MagicMock()
        ui.refreshable.side_effect = lambda function: FakeRefreshable(function)
        rendered_blueprint_names: list[str] = []
        service = ModWebService()

        with (
            patch.object(ModWebService, "_render_flat_tab_header", side_effect=lambda **kwargs: None),
            patch.object(
                ModWebService,
                "_render_blueprint_tile",
                side_effect=lambda **kwargs: rendered_blueprint_names.append(kwargs["blueprint"].label),
            ),
        ):
            service._render_blueprints_editor(ui=cast(ModWebUi, ui), model=model, user=user)

        self.assertEqual(rendered_blueprint_names, ["Alpha.sbp", "Zulu.sbp"])
        self.assertNotIn("label", ui.select.call_args.kwargs)
        self.assertEqual(ui.select.call_args.kwargs["value"], ModWebFileSortOrder.NAME_ASCENDING.value)

    def test_resolve_client_pack_mod_names_includes_required_optional_and_one_choice(self) -> None:
        required = self._mod_entry(name="required.jar")
        optional = self._mod_entry(
            name="optional.jar",
            client_pack=ClientPackConfig(policy=ClientPackPolicy.OPTIONAL),
        )
        default_choice = self._mod_entry(
            name="default.jar",
            client_pack=ClientPackConfig(
                policy=ClientPackPolicy.ALTERNATIVE,
                choice_group="renderer",
                default_choice=True,
            ),
        )
        other_choice = self._mod_entry(
            name="other.jar",
            client_pack=ClientPackConfig(
                policy=ClientPackPolicy.ALTERNATIVE,
                choice_group="renderer",
            ),
        )

        self.assertEqual(
            ModWebService._resolve_client_pack_mod_names(
                mods=(required, optional, default_choice, other_choice),
                optional_names=frozenset({optional.name}),
                choice_names={"renderer": other_choice.name},
            ),
            (required.name, optional.name, other_choice.name),
        )

        with self.assertRaisesRegex(ValueError, "Every client-pack choice group"):
            ModWebService._resolve_client_pack_mod_names(
                mods=(required, default_choice, other_choice),
                optional_names=frozenset(),
                choice_names={},
            )

    def test_client_pack_formats_only_offer_launcher_exports_for_minecraft(self) -> None:
        self.assertEqual(
            ModWebService._client_pack_format_options(None),
            {PackFormat.GENERIC_ZIP.value: "Generic ZIP"},
        )
        self.assertEqual(
            ModWebService._client_pack_format_options("minecraft"),
            {
                PackFormat.MODRINTH.value: "Modrinth (.mrpack)",
                PackFormat.CURSEFORGE.value: "CurseForge ZIP",
                PackFormat.GENERIC_ZIP.value: "Generic ZIP",
            },
        )
        self.assertEqual(
            tuple(ModWebService._client_pack_format_options("minecraft")),
            (
                PackFormat.MODRINTH.value,
                PackFormat.CURSEFORGE.value,
                PackFormat.GENERIC_ZIP.value,
            ),
        )
        self.assertIs(
            ModWebService._default_client_pack_format("minecraft"),
            PackFormat.MODRINTH,
        )
        self.assertIs(
            ModWebService._default_client_pack_format(None),
            PackFormat.GENERIC_ZIP,
        )

    def test_client_pack_kubejs_toggle_requires_an_included_minecraft_script(self) -> None:
        included_script = ClientPackKubeJsScript(
            relative_path="server_scripts/events.js",
            included=True,
        )
        excluded_script = ClientPackKubeJsScript(
            relative_path="startup_scripts/registry.js",
            included=False,
        )

        self.assertTrue(
            ModWebService._show_client_pack_kubejs_toggle(
                "minecraft",
                (excluded_script, included_script),
            )
        )
        self.assertFalse(
            ModWebService._show_client_pack_kubejs_toggle(
                "minecraft",
                (excluded_script,),
            )
        )
        self.assertFalse(ModWebService._show_client_pack_kubejs_toggle("minecraft", ()))
        self.assertFalse(
            ModWebService._show_client_pack_kubejs_toggle(
                "sevendays",
                (included_script,),
            )
        )

    def test_node_mod_entry_mapping_preserves_client_pack_policy(self) -> None:
        entry = self._mod_entry(
            name="optional.jar",
            client_pack=ClientPackConfig(policy=ClientPackPolicy.OPTIONAL),
        )

        restored = NodeModEntry.from_mapping(entry.to_mapping())

        self.assertEqual(restored, entry)

    def test_render_mods_section_adds_search_box_and_filters_visible_rows(self) -> None:
        class FakeContainer:
            def __init__(self) -> None:
                self.class_value: str | None = None
                self.visible = True

            def classes(self, value: str) -> "FakeContainer":
                self.class_value = value
                return self

            def set_visibility(self, visible: bool) -> None:
                self.visible = visible

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeButton:
            def __init__(
                self,
                text: str = "",
                on_click: Callable[[], object] | None = None,
            ) -> None:
                self.text: str = text
                self.on_click = on_click
                self.enabled: bool = True
                self.class_value: str | None = None
                self.props_value: str | None = None

            def __enter__(self) -> "FakeButton":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def classes(self, value: str) -> "FakeButton":
                self.class_value = value
                return self

            def props(
                self,
                value: str | None = None,
                *,
                remove: str | None = None,
            ) -> "FakeButton":
                self.props_value = value if remove is None else f"remove:{remove}"
                return self

            def on(
                self,
                event_name: str,
                handler: Callable[[object], None] | None = None,
                *,
                js_handler: str | None = None,
            ) -> "FakeButton":
                del event_name, handler, js_handler
                return self

            def set_text(self, value: str) -> None:
                self.text = value

            def set_enabled(self, enabled: bool) -> None:
                self.enabled = enabled

            def set_visibility(self, visible: bool) -> None:
                self.visible = visible

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text
                self.class_value: str | None = None

            def classes(self, value: str) -> "FakeLabel":
                self.class_value = value
                return self

            def set_text(self, value: str) -> None:
                self.text = value

        class FakeInput:
            def __init__(self, *, placeholder: str | None = None, value: object = None) -> None:
                self.placeholder = placeholder
                self.value = value
                self.class_value: str | None = None
                self.props_value: str | None = None
                self.handlers: dict[str, Callable[[object], None]] = {}
                self.visible = True

            def props(self, value: str) -> "FakeInput":
                self.props_value = value
                return self

            def classes(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
            ) -> "FakeInput":
                if value is not None:
                    self.class_value = value
                if add is not None:
                    self.class_value = f"{self.class_value or ''} {add}".strip()
                if remove is not None and self.class_value is not None:
                    self.class_value = self.class_value.replace(remove, "").strip()
                return self

            def on(self, event_name: str, handler: Callable[[object], None]) -> "FakeInput":
                self.handlers[event_name] = handler
                return self

            def on_change(self, handler: Callable[[object], None]) -> "FakeInput":
                self.handlers["change"] = handler
                return self

            def set_visibility(self, visible: bool) -> None:
                self.visible = visible

            def set_value(self, value: object) -> None:
                self.value = value

        class FakeUpload:
            def __init__(self) -> None:
                self.props: dict[str, object] = {}
                self.handlers: dict[str, Callable[[], None]] = {}

            def classes(self, value: str) -> "FakeUpload":
                del value
                return self

            def add_slot(self, name: str) -> FakeContainer:
                del name
                return FakeContainer()

            def disable(self) -> None:
                return None

            def enable(self) -> None:
                return None

            def on(self, event_name: str, handler: Callable[[], None], *, args: list[object]) -> "FakeUpload":
                self.handlers[event_name] = handler
                self.props[f"{event_name}-args"] = args
                return self

            def run_method(self, method_name: str) -> None:
                self.props["last-method"] = method_name

        class FakeDialog(FakeContainer):
            def __init__(self) -> None:
                super().__init__()
                self.opened = False

            def open(self) -> None:
                self.opened = True

            def close(self) -> None:
                return None

        class FakeRefreshable:
            def __init__(self, func: Callable[..., None]) -> None:
                self._func = func

            def __call__(self, *args: object) -> None:
                self._func(*args)

            def refresh(self, *args: object) -> None:
                self._func(*args)

        class FakeUi:
            def __init__(self) -> None:
                self.labels: list[FakeLabel] = []
                self.inputs: list[FakeInput] = []
                self.textareas: list[FakeInput] = []
                self.config_search_inputs: list[FakeInput] = []
                self.buttons: list[FakeButton] = []
                self.menu_items: list[FakeButton] = []
                self.policy_select_labels: list[object] = []
                self.policy_selects: list[FakeInput] = []
                self.checkboxes: dict[str, FakeInput] = {}
                self.group_inputs: list[FakeInput] = []
                self.rows: list[FakeContainer] = []
                self.dialogs: list[FakeDialog] = []
                self.tooltips: list[str] = []
                self.render_events: list[str] = []
                self.javascript_calls: list[str] = []
                self.sort_change_handler: Callable[[object], None] | None = None
                self.modlist_format_select: FakeInput | None = None
                self.navigate = SimpleNamespace(reload=lambda: None)

            def refreshable(self, func: Callable[..., None]) -> FakeRefreshable:
                return FakeRefreshable(func)

            def card(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                row = FakeContainer()
                self.rows.append(row)
                return row

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                dialog = FakeDialog()
                self.dialogs.append(dialog)
                return dialog

            def menu(self) -> FakeContainer:
                return FakeContainer()

            def menu_item(self, *args: object, **kwargs: object) -> FakeButton:
                item = FakeButton(
                    str(args[0]) if args else "",
                    on_click=cast(Callable[[], object] | None, kwargs.get("on_click")),
                )
                self.menu_items.append(item)
                return item

            def upload(self, *args: object, **kwargs: object) -> FakeUpload:
                del args, kwargs
                return FakeUpload()

            def timer(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                return object()

            def button(self, *args: object, **kwargs: object) -> FakeButton:
                text = str(args[0]) if args else ""
                button = FakeButton(
                    text,
                    on_click=cast(Callable[[], object] | None, kwargs.get("on_click")),
                )
                self.buttons.append(button)
                self.render_events.append(f"button:{text}")
                return button

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                self.render_events.append(f"label:{text}")
                return label

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                control = FakeInput(
                    placeholder=cast(str | None, kwargs.get("placeholder")),
                    value=kwargs.get("value"),
                )
                if control.placeholder == "Search mods":
                    self.inputs.append(control)
                elif control.placeholder == "Search pack mods":
                    self.config_search_inputs.append(control)
                if args and args[0] == "Group ID":
                    self.group_inputs.append(control)
                return control

            def textarea(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(
                    placeholder=cast(str | None, kwargs.get("placeholder")),
                    value=kwargs.get("value"),
                )
                on_change = cast(Callable[[object], None] | None, kwargs.get("on_change"))
                if on_change is not None:
                    control.on_change(on_change)
                self.textareas.append(control)
                return control

            def checkbox(self, *args: object, **kwargs: object) -> FakeInput:
                control = FakeInput(value=kwargs.get("value"))
                if args:
                    self.checkboxes[str(args[0])] = control
                return control

            def tooltip(self, text: str) -> None:
                self.tooltips.append(text)

            def select(self, *args: object, **kwargs: object) -> FakeInput:
                if kwargs.get("label") == "Format":
                    control = FakeInput(value=kwargs.get("value"))
                    self.modlist_format_select = control
                    return control
                if args and isinstance(args[0], dict) and set(args[0].values()) == {
                    "Required",
                    "Optional",
                    "Alternative",
                }:
                    self.policy_select_labels.append(kwargs.get("label"))
                    control = FakeInput(value=kwargs.get("value"))
                    self.policy_selects.append(control)
                    return control
                if args and isinstance(args[0], dict) and set(args[0].values()) == {
                    order.label for order in ModWebModSortOrder
                }:
                    self.sort_change_handler = cast(
                        Callable[[object], None] | None,
                        kwargs.get("on_change"),
                    )
                return FakeInput(value=kwargs.get("value"))

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

            def add_head_html(self, html: str) -> None:
                del html
                return None

            def run_javascript(self, script: str, *, timeout: float = 1.0) -> None:
                del timeout
                self.javascript_calls.append(script)

        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            app_scope="minecraft",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=2,
                    enabled_count=2,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=2,
                    non_downloadable_count=0,
                ),
                mods=(
                    self._mod_entry(
                        name="alpha-fabric.jar",
                        friendly="Alpha Fabric",
                        mod_type=ModType.CLIENT,
                        client_pack=ClientPackConfig(
                            policy=ClientPackPolicy.OPTIONAL,
                            default_selected=True,
                        ),
                    ),
                    self._mod_entry(name="beta-forge.jar", friendly="Beta Forge"),
                ),
                app_stats=None,
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={
                "alpha-fabric.jar": "/mods/download/alpha-fabric.jar",
                "beta-forge.jar": "/mods/download/beta-forge.jar",
            },
            client_pack_content_dirty=True,
            client_pack_published_version="2026-07-03",
            client_pack_next_version="2026-07-04",
            client_pack_changelog="Shared draft notes.",
            client_pack_published_changelog=(
                "Added client performance fixes.\nUpdated the default renderer."
            ),
            client_pack_releases=(
                ClientPackRelease(
                    version="2026-07-02",
                    changelog="Initial client pack.",
                ),
                ClientPackRelease(
                    version="2026-07-03",
                    changelog="Added client performance fixes.\nUpdated the default renderer.",
                ),
            ),
            client_pack_kubejs_scripts=(
                ClientPackKubeJsScript(
                    relative_path="server_scripts/events.js",
                    included=True,
                ),
                ClientPackKubeJsScript(
                    relative_path="startup_scripts/registry.js",
                    included=False,
                ),
            ),
            client_pack_metadata=ClientPackMetadataConfig(
                name="Example Pack",
                description="Example description",
                filename_template="{pack_name}-{version}",
            ),
            client_pack_file_previews=(
                ClientPackFilePreview(
                    path="overrides/servers.dat",
                    display_name="servers.dat",
                    content_text="Minecraft servers.dat entry\nname=YokoServer\nip=play.example.test:25565\n",
                ),
                ClientPackFilePreview(
                    path="overrides/options.txt",
                    display_name="options.txt",
                    content_text="autoJump:false\n",
                ),
            ),
            client_pack_automated_changelog="Added mods:\n- Alpha Fabric (1.0.0)",
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        ui = FakeUi()
        rendered_mod_names: list[str] = []
        remote_json = AsyncMock(return_value={"changelog": "Persisted after Save."})

        async def wait_for_metadata_cancellation(**_kwargs: object) -> BulkLauncherMetadataDiscovery:
            await asyncio.Event().wait()
            return BulkLauncherMetadataDiscovery()

        discover_bulk_metadata = AsyncMock(side_effect=wait_for_metadata_cancellation)
        cancel_bulk_metadata = AsyncMock(return_value=True)
        set_changelog_draft = Mock()
        get_changelog_draft = Mock(side_effect=[None, "Fresh shared draft."])

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_json_async", new=remote_json),
            patch.object(
                service,
                "_discover_bulk_mod_metadata",
                new=discover_bulk_metadata,
            ),
            patch.object(
                service,
                "_cancel_bulk_mod_metadata",
                new=cancel_bulk_metadata,
            ),
            patch.object(
                service._backend,
                "set_client_pack_changelog_draft",
                new=set_changelog_draft,
            ),
            patch.object(
                service._backend,
                "client_pack_changelog_draft",
                new=get_changelog_draft,
            ),
            patch.object(
                ModWebService,
                "_render_mod_download_row",
                side_effect=lambda **kwargs: rendered_mod_names.append(kwargs["entry"].name) or None,
            ),
        ):
            service._render_mods_section(ui=cast(ModWebUi, cast(object, ui)), model=model, user=user)

            self.assertEqual(ui.config_search_inputs, [])
            self.assertNotIn("Include configured KubeJS scripts", ui.checkboxes)
            configure_button = next(
                item for item in ui.menu_items if item.text == "Configure <!>"
            )
            self.assertIsNotNone(configure_button.on_click)
            assert configure_button.on_click is not None
            configure_button.on_click()
            client_pack_button = next(button for button in ui.buttons if button.text == "Client Pack")
            self.assertIsNotNone(client_pack_button.on_click)
            assert client_pack_button.on_click is not None
            client_pack_button.on_click()
            for dialog in ui.dialogs:
                dialog.opened = 0

            changelog_handler = ui.textareas[-2].handlers["change"]
            changelog_handler(SimpleNamespace(value="Persisted after Save."))
            ui.checkboxes["server_scripts/events.js"].value = False
            save_button = next(button for button in ui.buttons if button.text == "Save")
            self.assertIsNotNone(save_button.on_click)
            assert save_button.on_click is not None
            asyncio.run(cast(Any, save_button.on_click()))
            self.assertEqual(save_button.props_value, "remove:loading")
            remote_json.assert_awaited_once()
            self.assertEqual(
                remote_json.await_args.kwargs["json_payload"].get("changelog"),
                None,
            )
            self.assertEqual(
                remote_json.await_args.kwargs["json_payload"]["kubejs_scripts"],
                [
                    {"relative_path": "server_scripts/events.js", "included": False},
                    {"relative_path": "startup_scripts/registry.js", "included": False},
                ],
            )
            self.assertEqual(
                remote_json.await_args.kwargs["json_payload"]["metadata"],
                {
                    "name": "Example Pack",
                    "description": "Example description",
                    "filename_template": "{pack_name}-{version}",
                    "include_servers_dat": True,
                    "include_options_txt": True,
                },
            )
            self.assertTrue(ui.checkboxes["Include configured KubeJS scripts"].value)
            self.assertTrue(ui.checkboxes["Include servers.dat"].value)
            self.assertTrue(ui.checkboxes["Include options.txt"].value)
            self.assertEqual(ui.tooltips.count("View servers.dat"), 2)
            self.assertEqual(ui.tooltips.count("View options.txt"), 2)
            view_buttons = [
                button for button in ui.buttons if button.props_value is not None and "aria-label=View" in button.props_value
            ]
            self.assertEqual(len(view_buttons), 4)
            self.assertIsNotNone(view_buttons[0].on_click)
            assert view_buttons[0].on_click is not None
            view_buttons[0].on_click()
            self.assertTrue(ui.dialogs[-1].opened)
            self.assertEqual(
                ui.textareas[-1].value,
                "Minecraft servers.dat entry\nname=YokoServer\nip=play.example.test:25565\n",
            )
            ui.dialogs[-1].opened = False
            set_changelog_draft.assert_called_once_with(
                node_name="yuki",
                app_name="minecraft_alpha",
                changelog="Persisted after Save.",
            )
            changelog_handler(SimpleNamespace(value="Shared draft notes."))
            remote_json.reset_mock()
            publish_button = next(button for button in ui.buttons if button.text == "Publish")
            self.assertIsNotNone(publish_button.on_click)
            assert publish_button.on_click is not None
            asyncio.run(cast(Any, publish_button.on_click()))
            publish_calls = [
                call_args
                for call_args in remote_json.await_args_list
                if call_args.kwargs["method"] == "POST"
            ]
            self.assertEqual(len(publish_calls), 1)
            self.assertEqual(
                publish_calls[0].kwargs["json_payload"]["changelog"],
                "Shared draft notes.\n\nAdded mods:\n- Alpha Fabric (1.0.0)",
            )

            self.assertEqual([control.placeholder for control in ui.inputs], ["Search mods"])
            self.assertEqual(
                [control.placeholder for control in ui.config_search_inputs],
                ["Search pack mods"],
            )
            self.assertEqual(ui.policy_select_labels, [None, None])
            config_rows = [
                row
                for row in ui.rows
                if row.class_value is not None and "mod-client-pack-config-option" in row.class_value
            ]
            config_layout_rows = [
                row
                for row in ui.rows
                if row.class_value is not None and "mod-client-pack-config-layout" in row.class_value
            ]
            self.assertEqual(len(config_layout_rows), 1)
            self.assertEqual(len(config_rows), 2)
            ui.config_search_inputs[0].handlers["update:model-value"](SimpleNamespace(args="beta"))
            self.assertEqual([row.visible for row in config_rows], [False, True])
            ui.policy_selects[0].value = ClientPackPolicy.ALTERNATIVE.value
            ui.policy_selects[0].handlers["update:model-value"](SimpleNamespace(args=None))
            ui.group_inputs[0].handlers["update:model-value"](SimpleNamespace(args="renderer"))
            self.assertIn("Default alternatives", [label.text for label in ui.labels])
            ui.group_inputs[0].handlers["update:model-value"](SimpleNamespace(args="bad group"))
            self.assertIn("mod-client-pack-config-invalid", ui.group_inputs[0].class_value or "")
            ui.group_inputs[0].handlers["update:model-value"](SimpleNamespace(args="renderer_2"))
            self.assertNotIn("mod-client-pack-config-invalid", ui.group_inputs[0].class_value or "")
            self.assertEqual(
                ui.inputs[0].class_value,
                "mod-config-search mod-settings-search mod-mods-toolbar-search",
            )
            self.assertEqual(rendered_mod_names, ["alpha-fabric.jar", "beta-forge.jar"])
            result_count_label = next(
                label for label in ui.labels if label.class_value == "mod-mods-toolbar-result-count"
            )
            self.assertEqual(result_count_label.text, "2 mods")
            toolbar_text = [
                button.text
                for button in ui.buttons
                if button.class_value is not None and "mod-toolbar-button" in button.class_value
            ]
            self.assertNotIn("Enabled ZIP", toolbar_text)
            self.assertIn("Client Pack", toolbar_text)
            self.assertIn("Modlist", toolbar_text)
            self.assertNotIn("Configure <!>", toolbar_text)
            self.assertNotIn("Upload", toolbar_text)
            self.assertNotIn("Delete", toolbar_text)
            self.assertNotIn("Find Metadata", toolbar_text)
            self.assertIn("Metadata: Running", toolbar_text)
            self.assertIn("Clear", toolbar_text)
            self.assertIn("Download All/2", toolbar_text)
            self.assertEqual(
                toolbar_text[toolbar_text.index("Clear") : toolbar_text.index("Clear") + 2],
                ["Clear", "Download All/2"],
            )
            self.assertEqual(
                [item.text for item in ui.menu_items],
                ["Client Pack", "Modlist", "Upload", "Configure <!>", "Find Metadata", "Delete"],
            )
            mobile_menu_items = [
                item
                for item in ui.menu_items
                if item.text in {"Client Pack", "Modlist"}
            ]
            self.assertTrue(
                all("mod-toolbar-menu-mobile-only" in (item.class_value or "") for item in mobile_menu_items)
            )
            initial_metadata_status = next(
                button for button in ui.buttons if button.text == "Metadata: Running"
            )
            self.assertFalse(initial_metadata_status.visible)
            find_metadata_item = next(
                item for item in ui.menu_items if item.text == "Find Metadata"
            )
            self.assertIsNotNone(find_metadata_item.on_click)
            assert find_metadata_item.on_click is not None

            async def click_find_metadata() -> None:
                find_metadata_item.on_click()
                await asyncio.sleep(0)
                running_status = next(
                    button for button in ui.buttons if button.text == "Metadata: Scanning…"
                )
                self.assertTrue(running_status.visible)
                self.assertTrue(running_status.enabled)
                self.assertIsNotNone(running_status.on_click)
                assert running_status.on_click is not None
                running_status.on_click()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            asyncio.run(click_find_metadata())
            metadata_status_button = next(
                button for button in ui.buttons if button.text == "Metadata: Cancelled"
            )
            self.assertIn("mod-toolbar-status-button", metadata_status_button.class_value or "")
            self.assertTrue(metadata_status_button.visible)
            self.assertFalse(metadata_status_button.enabled)
            discovery_operation_id = discover_bulk_metadata.await_args.kwargs["operation_id"]
            cancel_bulk_metadata.assert_awaited_once_with(
                model=model,
                operation_id=discovery_operation_id,
                user=user,
            )
            self.assertIsNotNone(ui.modlist_format_select)
            assert ui.modlist_format_select is not None
            self.assertFalse(ui.checkboxes["Disabled"].value)
            self.assertFalse(ui.checkboxes["Built-in"].value)
            self.assertTrue(ui.checkboxes["Client"].value)
            ui.modlist_format_select.value = ModWebModlistFormat.JSON.value
            ui.checkboxes["Filename"].value = True
            ui.modlist_format_select.handlers["update:model-value"](SimpleNamespace(args=None))
            preview_labels = [
                label for label in ui.labels if label.class_value == "mod-modlist-preview w-full"
            ]
            self.assertEqual(
                json.loads(preview_labels[-1].text),
                [
                    {
                        "name": "Alpha Fabric",
                        "version": "1.0.0",
                        "filename": "alpha-fabric.jar",
                        "optional": True,
                    },
                    {"name": "Beta Forge", "version": "1.0.0", "filename": "beta-forge.jar"},
                ],
            )
            copy_button = next(button for button in ui.buttons if button.text == "Copy")
            self.assertIsNotNone(copy_button.on_click)
            assert copy_button.on_click is not None
            copy_button.on_click()
            self.assertIn("navigator.clipboard.writeText", ui.javascript_calls[-1])
            ui.javascript_calls.clear()
            menu_button = next(
                button
                for button in ui.buttons
                if button.class_value is not None and "mod-toolbar-menu-button" in button.class_value
            )
            self.assertIn("icon=menu", menu_button.props_value or "")
            selection_button = next(button for button in ui.buttons if button.text == "Clear")
            self.assertIn("mod-toolbar-selection-button", selection_button.class_value or "")
            all_button_text = [button.text for button in ui.buttons]
            self.assertNotIn("Publish & Download", all_button_text)
            self.assertIn("Changes", all_button_text)
            changes_button_index = all_button_text.index("Changes")
            self.assertEqual(
                all_button_text[changes_button_index - 1 : changes_button_index + 2],
                ["Cancel", "Changes", "Download"],
            )
            changes_button = ui.buttons[changes_button_index]
            self.assertIsNotNone(changes_button.on_click)
            assert changes_button.on_click is not None
            changes_button.on_click()
            self.assertEqual(sum(dialog.opened for dialog in ui.dialogs), 1)
            self.assertEqual(all_button_text.count("Save"), 1)
            self.assertEqual(all_button_text.count("Publish"), 1)
            self.assertEqual(
                [control.placeholder for control in ui.textareas],
                [None, "Describe client-pack changes in this release…", None, None],
            )
            self.assertEqual(
                [control.value for control in ui.textareas],
                [
                    "Example description",
                    "Shared draft notes.",
                    "Added mods:\n- Alpha Fabric (1.0.0)",
                    "Minecraft servers.dat entry\nname=YokoServer\nip=play.example.test:25565\n",
                ],
            )
            self.assertIn("rows=2", ui.textareas[0].props_value or "")
            self.assertIn("stack-label", ui.textareas[1].props_value or "")
            self.assertIn("rows=3", ui.textareas[1].props_value or "")
            self.assertNotIn("debounce=", ui.textareas[1].props_value or "")
            self.assertIn("change", ui.textareas[1].handlers)
            self.assertIn("readonly", ui.textareas[2].props_value or "")
            self.assertIn("rows=6", ui.textareas[2].props_value or "")
            label_texts = [label.text for label in ui.labels]
            self.assertIn("Mods", label_texts)
            self.assertIn(
                "Draft notes are shared when this configuration is saved.",
                label_texts,
            )
            self.assertIn("Publish reasons", label_texts)
            self.assertIn(
                "Saved client-pack configuration changes are waiting to be published.",
                label_texts,
            )
            self.assertIn("Added mods: Alpha Fabric (1.0.0)", label_texts)
            configure_button = next(
                item for item in ui.menu_items if item.text == "Configure <!>"
            )
            self.assertIsNotNone(configure_button.on_click)
            assert configure_button.on_click is not None
            configure_button.on_click()
            self.assertIn("Fresh shared draft.", [control.value for control in ui.textareas])
            changelog_labels = [
                label.text
                for label in ui.labels
                if label.class_value == "mod-client-pack-changelog-content"
            ]
            self.assertEqual(
                changelog_labels,
                [
                    "Added client performance fixes.\nUpdated the default renderer.",
                    "Initial client pack.",
                ],
            )
            release_version_labels = [
                text for text in label_texts if text in {"2026-07-02", "2026-07-03"}
            ]
            self.assertEqual(release_version_labels[-2:], ["2026-07-03", "2026-07-02"])
            self.assertIn("2026-07-03", label_texts)
            self.assertIn("2026-07-04", label_texts)
            self.assertLess(label_texts.index("Pack format"), label_texts.index("Optional mods"))
            self.assertLess(label_texts.index("Optional mods"), label_texts.index("Required"))
            self.assertEqual(label_texts.count("Required"), 1)
            client_pack_download_button = next(button for button in ui.buttons if button.text == "Download")
            self.assertFalse(client_pack_download_button.enabled)
            self.assertIn(
                "This client pack has unpublished changes. Publish them before downloading.",
                label_texts,
            )
            self.assertLess(
                ui.render_events.index("button:Download"),
                ui.render_events.index("label:Required"),
            )

            sort_change_handler = ui.sort_change_handler
            self.assertIsNotNone(sort_change_handler)
            assert sort_change_handler is not None
            sort_change_handler(SimpleNamespace(value=ModWebModSortOrder.TYPE.value))
            self.assertEqual(rendered_mod_names[-2:], ["beta-forge.jar", "alpha-fabric.jar"])
            self.assertIn('const value = "type"', ui.javascript_calls[-1])
            self.assertIn('url.searchParams.set("mod_sort", value)', ui.javascript_calls[-1])
            rendered_mod_names.clear()
            ui.javascript_calls.clear()

            self.assertNotIn("update:model-value", ui.inputs[0].handlers)
            self.assertIn("clear", ui.inputs[0].handlers)
            search_handler = ui.inputs[0].handlers["keydown.enter"]
            ui.inputs[0].value = "beta"
            self.assertEqual(ui.javascript_calls, [])
            search_handler()
            self.assertIn('const value = "beta"', ui.javascript_calls[-1])
            self.assertEqual(rendered_mod_names, ["beta-forge.jar"])
            self.assertEqual(result_count_label.text, "1 of 2 mods")

            ui.inputs[0].value = "missing"
            search_handler()
            self.assertEqual(result_count_label.text, "0 of 2 mods")

            rendered_mod_names.clear()
            ui.inputs[0].handlers["clear"]()
            self.assertEqual(ui.inputs[0].value, "")
            self.assertEqual(rendered_mod_names, ["beta-forge.jar", "alpha-fabric.jar"])
            self.assertIn('url.searchParams.delete("search")', ui.javascript_calls[-1])

        fallback_model = replace(
            model,
            client_pack_file_previews=(),
            join_address="play.example.test:25565",
            join_direct_ip_address="203.0.113.10:25565",
        )
        fallback_ui = FakeUi()
        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service._backend, "client_pack_changelog_draft", return_value=None),
            patch.object(ModWebService, "_render_mod_download_row", return_value=None),
        ):
            service._render_mods_section(
                ui=cast(ModWebUi, cast(object, fallback_ui)),
                model=fallback_model,
                user=user,
            )
            configure_button = next(
                item for item in fallback_ui.menu_items if item.text == "Configure <!>"
            )
            self.assertIsNotNone(configure_button.on_click)
            assert configure_button.on_click is not None
            configure_button.on_click()
            client_pack_button = next(button for button in fallback_ui.buttons if button.text == "Client Pack")
            self.assertIsNotNone(client_pack_button.on_click)
            assert client_pack_button.on_click is not None
            client_pack_button.on_click()
            fallback_view_button = next(
                button
                for button in fallback_ui.buttons
                if button.props_value is not None and "aria-label=View servers.dat" in button.props_value
            )
            self.assertIsNotNone(fallback_view_button.on_click)
            assert fallback_view_button.on_click is not None
            fallback_view_button.on_click()
            self.assertEqual(
                fallback_ui.textareas[-1].value,
                "Minecraft servers.dat entry\nname=YukiServer\nip=203.0.113.10:25565\n",
            )

        self.assertIn("No mods match that search.", [label.text for label in ui.labels])

    def test_render_mods_section_shows_sudo_delete_toolbar_and_selectable_blocked_mods(self) -> None:
        class FakeContainer:
            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def classes(self, value: str) -> "FakeContainer":
                del value
                return self

        class FakeButton:
            def __init__(self, text: str = "") -> None:
                self.text: str = text
                self.class_value: str | None = None
                self.enabled: bool = True

            def classes(self, value: str) -> "FakeButton":
                self.class_value = value
                return self

            def on(
                self,
                event_name: str,
                handler: Callable[[object], None] | None = None,
                *,
                js_handler: str | None = None,
            ) -> "FakeButton":
                del event_name, handler, js_handler
                return self

            def set_text(self, value: str) -> None:
                self.text = value

            def set_enabled(self, enabled: bool) -> None:
                self.enabled = enabled

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text

            def classes(self, value: str) -> "FakeLabel":
                del value
                return self

            def set_text(self, value: str) -> None:
                self.text = value

        class FakeInput:
            def __init__(self, value: object = None) -> None:
                self.value: object = value

            def props(self, value: str) -> "FakeInput":
                del value
                return self

            def classes(self, value: str) -> "FakeInput":
                del value
                return self

            def on(self, event_name: str, handler: Callable[[object], None]) -> "FakeInput":
                del event_name, handler
                return self

        class FakeUpload:
            def __init__(self) -> None:
                self.props: dict[str, object] = {}
                self.handlers: dict[str, Callable[[], None]] = {}

            def classes(self, value: str) -> "FakeUpload":
                del value
                return self

            def add_slot(self, name: str) -> FakeContainer:
                del name
                return FakeContainer()

            def disable(self) -> None:
                return None

            def enable(self) -> None:
                return None

            def on(self, event_name: str, handler: Callable[[], None], *, args: list[object]) -> "FakeUpload":
                self.handlers[event_name] = handler
                self.props[f"{event_name}-args"] = args
                return self

            def run_method(self, method_name: str) -> None:
                self.props["last-method"] = method_name

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeRefreshable:
            def __init__(self, func: Callable[..., None]) -> None:
                self._func = func

            def __call__(self, *args: object) -> None:
                self._func(*args)

            def refresh(self, *args: object) -> None:
                self._func(*args)

        class FakeUi:
            def __init__(self) -> None:
                self.buttons: list[FakeButton] = []
                self.cleanups: list[Callable[..., object]] = []
                self.context = SimpleNamespace(
                    client=SimpleNamespace(on_delete=lambda cleanup: self.cleanups.append(cleanup))
                )
                self.navigate = SimpleNamespace(reload=lambda: None)
                self.upload_control = FakeUpload()
                self.upload_kwargs: dict[str, object] = {}

            def refreshable(self, func: Callable[..., None]) -> FakeRefreshable:
                return FakeRefreshable(func)

            def card(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def upload(self, *args: object, **kwargs: object) -> FakeUpload:
                del args
                self.upload_kwargs = kwargs
                return self.upload_control

            def timer(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                return object()

            def button(self, *args: object, **kwargs: object) -> FakeButton:
                del kwargs
                text = str(args[0]) if args else ""
                button = FakeButton(text)
                self.buttons.append(button)
                return button

            def label(self, text: str) -> FakeLabel:
                return FakeLabel(text)

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args, kwargs
                return FakeInput()

            def select(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                return FakeInput(kwargs.get("value"))

            def checkbox(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                return FakeInput(kwargs.get("value"))

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

            def add_head_html(self, html: str) -> None:
                del html
                return None

        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=3,
                    enabled_count=3,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=2,
                    non_downloadable_count=1,
                ),
                mods=(
                    self._mod_entry(name="downloadable.jar", friendly="Downloadable Mod"),
                    self._mod_entry(
                        name="server-only.jar",
                        friendly="Server Only Mod",
                        mod_type=ModType.SERVER,
                    ),
                    self._mod_entry(
                        name="builtin.zip",
                        friendly="Built In Mod",
                        downloadable=False,
                        mod_type=ModType.BUILTIN,
                        download_block_label="Built in",
                    ),
                ),
                app_stats=None,
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={
                "downloadable.jar": "/mods/download/downloadable.jar",
                "server-only.jar": "/mods/download/server-only.jar",
            },
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        ui = FakeUi()
        can_select_by_mod: dict[str, bool] = {}

        def _capture_row(**kwargs: object) -> None:
            entry = cast(NodeModEntry, kwargs["entry"])
            can_select_by_mod[entry.name] = cast(bool, kwargs["can_select"])
            return None

        with patch.object(
            service,
            "_user_has_level",
            side_effect=lambda _user, level: level in {Power_Level.user, Power_Level.sudo},
        ):
            with (
                patch.object(
                    ModWebService,
                    "_render_mod_download_row",
                    side_effect=_capture_row,
                ),
                patch.object(
                    service,
                    "_direct_mod_upload_target",
                    return_value=ModWebDirectUploadTarget(
                        url="https://node.example/api/node/apps/minecraft_alpha/mods/upload",
                        authorization_header="Bearer direct-token",
                    ),
                ),
            ):
                service._render_mods_section(ui=cast(ModWebUi, cast(object, ui)), model=model, user=user)

        toolbar_text = [
            button.text for button in ui.buttons if button.class_value is not None and "mod-toolbar-button" in button.class_value
        ]
        self.assertTrue(any(text.startswith("Delete") for text in toolbar_text))
        self.assertNotIn("on_multi_upload", ui.upload_kwargs)
        self.assertEqual(
            ui.upload_control.props["url"],
            "https://node.example/api/node/apps/minecraft_alpha/mods/upload",
        )
        self.assertEqual(
            ui.upload_control.props["headers"],
            [{"name": "Authorization", "value": "Bearer direct-token"}],
        )
        self.assertEqual(ui.upload_control.props["field-name"], "upload")
        self.assertTrue(ui.upload_control.props["batch"])
        self.assertEqual(set(ui.upload_control.handlers), {"start", "uploaded", "failed", "rejected"})
        ui.upload_control.handlers["start"]()
        active_transfer = service._backend.user_transfer_items(user_id=user.discord_id)[0]
        self.assertEqual(active_transfer.label, "Mod upload")
        self.assertIs(active_transfer.state, ModWebNotificationTrayItemState.ACTIVE)
        ui.upload_control.handlers["uploaded"]()
        completed_transfer = service._backend.user_transfer_items(user_id=user.discord_id)[0]
        self.assertIs(completed_transfer.state, ModWebNotificationTrayItemState.SUCCESS)
        ui.upload_control.handlers["start"]()
        ui.cleanups[0]()
        interrupted_transfer = service._backend.user_transfer_items(user_id=user.discord_id)[0]
        self.assertIs(interrupted_transfer.state, ModWebNotificationTrayItemState.ERROR)
        self.assertIn("interrupted", interrupted_transfer.detail_text or "")
        self.assertEqual(
            can_select_by_mod,
            {
                "downloadable.jar": True,
                "server-only.jar": True,
                "builtin.zip": False,
            },
        )

    def test_available_mod_actions_allow_admin_enable_disable_without_sudo_actions(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="admin", global_name=None, avatar_hash=None)
        regular_entry = self._mod_entry(name="alpha.jar", enabled=True)
        coremod_entry = self._mod_entry(name="core.jar", enabled=False, mod_type=ModType.COREMOD, coremod=True)

        with patch.object(
            service,
            "_user_has_level",
            side_effect=lambda _user, level: level in {Power_Level.user, Power_Level.admin},
        ):
            regular_actions = service._available_mod_actions(user=user, entry=regular_entry)
            coremod_actions = service._available_mod_actions(user=user, entry=coremod_entry)

        self.assertEqual(regular_actions, (NodeModMutationAction.DISABLE,))
        self.assertEqual(coremod_actions, ())

    def test_client_only_mod_actions_exclude_server_enable_disable(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)
        entry = self._mod_entry(
            name="client.jar",
            enabled=False,
            mod_type=ModType.CLIENT,
            placement=ModPlacement.CLIENT_ONLY,
        )

        with patch.object(service, "_user_has_level", return_value=True):
            actions = service._available_mod_actions(user=user, entry=entry)

        self.assertEqual(actions, (NodeModMutationAction.DELETE,))

    def test_mutate_mod_allows_admin_enable_for_regular_mod(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=1,
                    enabled_count=0,
                    disabled_count=1,
                    coremod_count=0,
                    downloadable_count=1,
                    non_downloadable_count=0,
                ),
                mods=(self._mod_entry(name="alpha.jar", enabled=False),),
                app_stats=None,
            ),
            supports_chat=False,
            chat_url=None,
            map_url=None,
            can_write_map_annotations=False,
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={"alpha.jar": "/mods/download/alpha.jar"},
        )
        user = ModWebUser(discord_id=42, username="admin", global_name=None, avatar_hash=None)
        expected_result = NodeModMutationResult(
            app_name=model.app_name,
            app_friendly=model.app_friendly,
            node=model.node_name,
            mod_name="alpha.jar",
            action=NodeModMutationAction.ENABLE,
            message="Enabled alpha.jar.",
            mod=self._mod_entry(name="alpha.jar", enabled=True),
        )
        with patch.object(
            service,
            "_user_has_level",
            side_effect=lambda _user, level: level in {Power_Level.user, Power_Level.admin},
        ), patch.object(
            service,
            "_remote_mod_mutation_async",
            new=AsyncMock(return_value=expected_result),
        ) as remote_mod_mutation:
            result = asyncio.run(
                service._mutate_mod(
                    model=model,
                    mod_name="alpha.jar",
                    action=NodeModMutationAction.ENABLE,
                    user=user,
                )
            )

        remote_mod_mutation.assert_awaited_once()
        self.assertEqual(result, expected_result)

    def test_mutate_mod_rejects_admin_delete_without_sudo(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=1,
                    enabled_count=1,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=1,
                    non_downloadable_count=0,
                ),
                mods=(self._mod_entry(name="alpha.jar"),),
                app_stats=None,
            ),
            supports_chat=False,
            chat_url=None,
            map_url=None,
            can_write_map_annotations=False,
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={"alpha.jar": "/mods/download/alpha.jar"},
        )
        user = ModWebUser(discord_id=42, username="admin", global_name=None, avatar_hash=None)

        with patch.object(
            service,
            "_user_has_level",
            side_effect=lambda _user, level: level in {Power_Level.user, Power_Level.admin},
        ):
            with self.assertRaises(PermissionError) as raised:
                asyncio.run(
                    service._mutate_mod(
                        model=model,
                        mod_name="alpha.jar",
                        action=NodeModMutationAction.DELETE,
                        user=user,
                    )
                )

        self.assertEqual(str(raised.exception), "Sudo access is required for this mod action.")

    def test_update_mod_properties_sends_typed_classification_and_overrides(self) -> None:
        service = ModWebService()
        entry = self._mod_entry(name="alpha.jar")
        overrides = ModMetadataOverrides(
            friendly_name="Alpha Override",
            version="2.0.0",
            origin="curated",
        )
        client_pack = ClientPackConfig(
            policy=ClientPackPolicy.OPTIONAL,
            default_selected=False,
        )
        mod_pages = (
            ModPageLink(name="Modrinth", url="https://modrinth.com/mod/alpha"),
        )
        updated_entry = replace(
            entry,
            friendly="Alpha Override",
            mod_type=ModType.CLIENT,
            downloadable=False,
            client_pack_eligible=False,
            download_block_reason=ModDownloadBlockReason.ARTIFACT.value,
            download_block_label=ModDownloadBlockReason.ARTIFACT.label,
            version="2.0.0",
            origin="curated",
            mod_pages=mod_pages,
            metadata_overrides=overrides,
            client_pack=client_pack,
        )
        expected_result = NodeModMutationResult(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="yuki",
            mod_name=entry.name,
            action=NodeModMutationAction.UPDATE_PROPERTIES,
            message="Updated properties for Alpha Override.",
            mod=updated_entry,
        )
        model = cast(
            ModWebPageModel,
            cast(
                object,
                SimpleNamespace(
                    node_name="yuki",
                    app_name="minecraft_alpha",
                ),
            ),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(return_value=expected_result.to_mapping()),
            ) as remote_json,
        ):
            result = asyncio.run(
                service._update_mod_properties(
                    model=model,
                    entry=entry,
                    mod_type=ModType.CLIENT,
                    download_block_reason=ModDownloadBlockReason.ARTIFACT,
                    metadata_overrides=overrides,
                    mod_pages=mod_pages,
                    client_pack=client_pack,
                    launcher_urls=LauncherProviderUrls(
                        modrinth="https://modrinth.com/mod/alpha/version/alpha-2.0.0",
                    ),
                    user=user,
                )
            )

        self.assertEqual(result, expected_result)
        self.assertIs(result.mod.client_pack.policy, ClientPackPolicy.OPTIONAL)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path="/apps/minecraft_alpha/mods/alpha.jar/properties",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="PUT",
            json_payload={
                "mod_type": "client",
                "download_block_reason": "artifact",
                "metadata_overrides": {
                    "friendly_name": "Alpha Override",
                    "version": "2.0.0",
                    "origin": "curated",
                    "added": None,
                },
                "mod_pages": [
                    {"name": "Modrinth", "url": "https://modrinth.com/mod/alpha"},
                ],
                "client_pack": {
                    "included_in_client": True,
                    "policy": "optional",
                    "choice_group": None,
                    "default_choice": False,
                    "default_selected": False,
                },
                "launcher_urls": {
                    "modrinth": "https://modrinth.com/mod/alpha/version/alpha-2.0.0",
                    "curseforge": None,
                    "curseforge_reference": None,
                },
            },
        )

    def test_fetch_mod_launcher_metadata_uses_non_mutating_resolver_endpoint(self) -> None:
        service = ModWebService()
        entry = self._mod_entry(name="alpha.jar")
        launcher_urls = LauncherProviderUrls(
            modrinth="https://modrinth.com/mod/alpha/version/alpha-2.0.0"
        )
        expected = LauncherMetadataResolution(
            suggested_mod_type=ModType.CLIENT,
            suggestion_provider=Provider.MODRINTH,
        )
        model = cast(
            ModWebPageModel,
            cast(object, SimpleNamespace(node_name="yuki", app_name="minecraft_alpha")),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(return_value=expected.model_dump(mode="json")),
            ) as remote_json,
        ):
            result = asyncio.run(
                service._fetch_mod_launcher_metadata(
                    model=model,
                    entry=entry,
                    launcher_urls=launcher_urls,
                    providers=(Provider.MODRINTH,),
                    user=user,
                )
            )

        self.assertEqual(result, expected)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path="/apps/minecraft_alpha/mods/alpha.jar/launcher-metadata",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "launcher_urls": launcher_urls.model_dump(mode="json"),
                "providers": [Provider.MODRINTH.value],
            },
        )

    def test_resolve_mod_launcher_metadata_uses_effective_mod_page_values(self) -> None:
        service = ModWebService()
        entry = self._mod_entry(name="alpha.jar")
        mod_pages = (
            ModPageLink(name="Modrinth", url="https://modrinth.com/mod/alpha"),
        )
        existing_urls = LauncherProviderUrls(
            curseforge="https://www.curseforge.com/minecraft/mc-mods/alpha/files/123"
        )
        expected = LauncherMetadataDiscovery()
        model = cast(
            ModWebPageModel,
            cast(object, SimpleNamespace(node_name="yuki", app_name="minecraft_alpha")),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(return_value=expected.model_dump(mode="json")),
            ) as remote_json,
        ):
            result = asyncio.run(
                service._resolve_mod_launcher_metadata(
                    model=model,
                    entry=entry,
                    mod_pages=mod_pages,
                    existing_launcher_urls=existing_urls,
                    providers=(Provider.MODRINTH,),
                    user=user,
                )
            )

        self.assertEqual(result, expected)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path="/apps/minecraft_alpha/mods/alpha.jar/launcher-metadata/resolve",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "mod_pages": [page.model_dump(mode="json") for page in mod_pages],
                "existing_launcher_urls": existing_urls.model_dump(mode="json"),
                "providers": [Provider.MODRINTH.value],
            },
        )

    def test_find_mod_pages_uses_effective_mod_page_values(self) -> None:
        service = ModWebService()
        entry = self._mod_entry(name="alpha.jar")
        mod_pages = (
            ModPageLink(name="Modrinth", url="https://modrinth.com/mod/alpha"),
        )
        expected = ModPageDiscovery()
        model = cast(
            ModWebPageModel,
            cast(object, SimpleNamespace(node_name="yuki", app_name="minecraft_alpha")),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(return_value=expected.model_dump(mode="json")),
            ) as remote_json,
        ):
            result = asyncio.run(
                service._find_mod_pages(
                    model=model,
                    entry=entry,
                    mod_pages=mod_pages,
                    providers=(Provider.CURSEFORGE,),
                    user=user,
                )
            )

        self.assertEqual(result, expected)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path="/apps/minecraft_alpha/mods/alpha.jar/mod-pages/resolve",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "mod_pages": [page.model_dump(mode="json") for page in mod_pages],
                "providers": [Provider.CURSEFORGE.value],
            },
        )

    def test_bulk_metadata_discovery_uses_bulk_node_endpoint(self) -> None:
        service = ModWebService()
        operation_id = "c50f39cb-acde-441f-ab92-3fd507c7b290"
        expected = BulkLauncherMetadataDiscovery()
        model = cast(
            ModWebPageModel,
            cast(object, SimpleNamespace(node_name="yuki", app_name="minecraft_alpha")),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(return_value=expected.model_dump(mode="json")),
            ) as remote_json,
        ):
            result = asyncio.run(
                service._discover_bulk_mod_metadata(
                    model=model,
                    operation_id=operation_id,
                    user=user,
                )
            )

        self.assertEqual(result, expected)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path="/apps/minecraft_alpha/mods/metadata/discover",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={"operation_id": operation_id, "mod_names": []},
            timeout=600.0,
        )

    def test_bulk_metadata_apply_sends_selected_mod_names_and_type_opt_ins(self) -> None:
        service = ModWebService()
        operation_id = "c50f39cb-acde-441f-ab92-3fd507c7b291"
        discovery_operation_id = "c50f39cb-acde-441f-ab92-3fd507c7b290"
        expected = NodeBulkLauncherMetadataApplyResult(
            discovery=BulkLauncherMetadataDiscovery(),
            applied_mod_names=("alpha.jar",),
        )
        model = cast(
            ModWebPageModel,
            cast(object, SimpleNamespace(node_name="yuki", app_name="minecraft_alpha")),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(return_value=expected.model_dump(mode="json")),
            ) as remote_json,
        ):
            result = asyncio.run(
                service._apply_bulk_mod_metadata(
                    model=model,
                    operation_id=operation_id,
                    discovery_operation_id=discovery_operation_id,
                    mod_names=("alpha.jar",),
                    apply_suggested_type_mod_names=("alpha.jar",),
                    user=user,
                )
            )

        self.assertEqual(result, expected)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path="/apps/minecraft_alpha/mods/metadata/apply",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={
                "operation_id": operation_id,
                "discovery_operation_id": discovery_operation_id,
                "mod_names": ["alpha.jar"],
                "apply_suggested_type_mod_names": ["alpha.jar"],
            },
            timeout=600.0,
        )

    def test_bulk_metadata_cancel_uses_operation_endpoint(self) -> None:
        service = ModWebService()
        operation_id = "c50f39cb-acde-441f-ab92-3fd507c7b292"
        model = cast(
            ModWebPageModel,
            cast(object, SimpleNamespace(node_name="yuki", app_name="minecraft_alpha")),
        )
        node = ModWebNodeLink(
            node_name="yuki",
            label="Yuki",
            url="/mod-web/nodes/yuki",
            api_base_url="https://yuki.example/api/node",
            api_url="/api/node-proxy/yuki/apps",
            is_current=True,
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(
                service,
                "_remote_json_async",
                new=AsyncMock(
                    return_value={"operation_id": operation_id, "cancelled": True}
                ),
            ) as remote_json,
        ):
            cancelled = asyncio.run(
                service._cancel_bulk_mod_metadata(
                    model=model,
                    operation_id=operation_id,
                    user=user,
                )
            )

        self.assertTrue(cancelled)
        remote_json.assert_awaited_once_with(
            node=node,
            app_name="minecraft_alpha",
            path=f"/apps/minecraft_alpha/mods/metadata/{operation_id}/cancel",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={},
        )

    def test_render_saves_editor_uses_direct_upload_and_settings_search_styling(self) -> None:
        class FakeContainer:
            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                del exc_type, exc, tb
                return False

            def classes(self, value: str) -> "FakeContainer":
                del value
                return self

            def props(self, value: str) -> "FakeContainer":
                del value
                return self

        class FakeLabel:
            def __init__(self, text: str) -> None:
                self.text = text

            def classes(self, value: str) -> "FakeLabel":
                del value
                return self

        class FakeInput:
            def __init__(self, *, placeholder: str | None = None, value: object = None) -> None:
                self.placeholder = placeholder
                self.value = value
                self.class_value: str | None = None
                self.handlers: dict[str, Callable[..., None]] = {}

            def props(self, value: str) -> "FakeInput":
                del value
                return self

            def classes(self, value: str) -> "FakeInput":
                self.class_value = value
                return self

            def on(self, event_name: str, handler: Callable[..., None]) -> "FakeInput":
                self.handlers[event_name] = handler
                return self

        class FakeUpload:
            def __init__(self) -> None:
                self.props: dict[str, object] = {}
                self.handlers: dict[str, Callable[[], None]] = {}

            def classes(self, value: str) -> "FakeUpload":
                del value
                return self

            def on(self, event_name: str, handler: Callable[[], None], *, args: list[object]) -> "FakeUpload":
                self.handlers[event_name] = handler
                self.props[f"{event_name}-args"] = args
                return self

        class FakeButton:
            def classes(self, value: str) -> "FakeButton":
                del value
                return self

        class FakeDialog(FakeContainer):
            def __init__(self) -> None:
                self.closed = False

            def open(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        class FakeRefreshable:
            def __init__(self, func: Callable[[str], None]) -> None:
                self._func = func

            def __call__(self, search_query: str) -> None:
                self._func(search_query)

            def refresh(self, search_query: str) -> None:
                self._func(search_query)

        class FakeUi:
            def __init__(self) -> None:
                self.inputs: list[FakeInput] = []
                self.labels: list[FakeLabel] = []
                self.selects: list[FakeInput] = []
                self.notifications: list[tuple[str, str | None]] = []
                self.reload_called = False
                self.navigate = SimpleNamespace(reload=self._reload)
                self.upload_control = FakeUpload()
                self.upload_kwargs: dict[str, object] = {}

            def _reload(self) -> None:
                self.reload_called = True

            def refreshable(self, func: Callable[[str], None]) -> FakeRefreshable:
                return FakeRefreshable(func)

            def card(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def element(self, *args: object, **kwargs: object) -> FakeContainer:
                del args, kwargs
                return FakeContainer()

            def upload(self, *args: object, **kwargs: object) -> FakeUpload:
                del args
                self.upload_kwargs = kwargs
                return self.upload_control

            def timer(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                return object()

            def button(self, *args: object, **kwargs: object) -> FakeButton:
                del args, kwargs
                return FakeButton()

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(
                    placeholder=cast(str | None, kwargs.get("placeholder")),
                    value=kwargs.get("value"),
                )
                self.inputs.append(control)
                return control

            def select(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(value=kwargs.get("value"))
                on_change = kwargs.get("on_change")
                if callable(on_change):
                    control.handlers["change"] = on_change
                self.selects.append(control)
                return control

            def notify(
                self,
                message: str,
                *,
                type: str | None = None,
                multi_line: bool = False,
            ) -> None:
                del multi_line
                self.notifications.append((message, type))

            def run_javascript(self, script: str) -> None:
                del script
                return None

        service = ModWebService()
        model = cast(
            ModWebPageModel,
            cast(
                object,
                SimpleNamespace(
                    app_friendly="Factorio",
                    app_color_hex="#DC6B0F",
                    search_query="",
                    node_name="yuki",
                    app_name="factorio",
                    supports_save_uploads=True,
                    supports_save_rename=False,
                    save_write_level=Power_Level.user,
                    saves=NodeSaveList(
                        app_name="factorio",
                        app_friendly="Factorio",
                        node="yuki",
                        roots=(
                            NodeSaveRootEntry(id="worlds", label="Worlds"),
                            NodeSaveRootEntry(id="archives", label="Archives"),
                        ),
                        saves=(
                            NodeSaveEntry(
                                id="worlds/alpha.zip",
                                label="alpha.zip",
                                relative_path="alpha.zip",
                                root_id="worlds",
                                root_label="Worlds",
                                kind="file",
                                size_bytes=128,
                                size_text="128B",
                                modified_at="2026-06-06 10:00:00",
                            ),
                            NodeSaveEntry(
                                id="worlds/beta.zip",
                                label="beta.zip",
                                relative_path="beta.zip",
                                root_id="worlds",
                                root_label="Worlds",
                                kind="file",
                                size_bytes=256,
                                size_text="256B",
                                modified_at="2026-06-06 11:00:00",
                            ),
                        ),
                    ),
                ),
            ),
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        ui = FakeUi()
        rendered_save_names: list[str] = []

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(
                service,
                "_direct_save_upload_target",
                return_value=ModWebDirectUploadTarget(
                    url="https://node.example/api/node/apps/factorio/saves/upload",
                    authorization_header="Bearer save-token",
                ),
            ),
            patch.object(ModWebService, "_render_flat_tab_header", side_effect=lambda **kwargs: None),
            patch.object(
                ModWebService,
                "_render_save_tile",
                side_effect=lambda **kwargs: rendered_save_names.append(kwargs["save"].label) or None,
            ),
        ):
            service._render_saves_editor(ui=cast(ModWebUi, cast(object, ui)), model=model, user=user)

            self.assertEqual([control.placeholder for control in ui.inputs], ["Search saves"])
            self.assertEqual(ui.inputs[0].class_value, "mod-config-search mod-settings-search")
            self.assertEqual(rendered_save_names, ["beta.zip", "alpha.zip"])
            self.assertEqual(ui.selects[1].value, ModWebFileSortOrder.LATEST_MODIFIED.value)

            ui.selects[1].handlers["change"](SimpleNamespace(value=ModWebFileSortOrder.NAME_ASCENDING.value))
            self.assertEqual(rendered_save_names[-2:], ["alpha.zip", "beta.zip"])

            self.assertNotIn("update:model-value", ui.inputs[0].handlers)
            search_handler = ui.inputs[0].handlers["keydown.enter"]
            ui.inputs[0].value = "beta"
            search_handler()
            self.assertEqual(rendered_save_names[-1:], ["beta.zip"])

            ui.inputs[0].value = "missing"
            search_handler()

            self.assertNotIn("on_upload", ui.upload_kwargs)
            self.assertEqual(
                ui.upload_control.props["url"],
                "https://node.example/api/node/apps/factorio/saves/upload",
            )
            self.assertEqual(
                ui.upload_control.props["headers"],
                [{"name": "Authorization", "value": "Bearer save-token"}],
            )
            self.assertEqual(
                ui.upload_control.props["form-fields"],
                [{"name": "root_id", "value": "worlds"}],
            )
            self.assertEqual(
                set(ui.upload_control.handlers),
                {"start", "uploaded", "failed", "rejected"},
            )

            ui.selects[0].value = "archives"
            ui.selects[0].handlers["update:model-value"]()
            self.assertEqual(
                ui.upload_control.props["form-fields"],
                [{"name": "root_id", "value": "archives"}],
            )

            ui.upload_control.handlers["start"]()
            active_transfer = service._backend.user_transfer_items(user_id=user.discord_id)[0]
            self.assertEqual(active_transfer.label, "Save upload")
            self.assertIs(active_transfer.state, ModWebNotificationTrayItemState.ACTIVE)
            ui.upload_control.handlers["failed"]()
            failed_transfer = service._backend.user_transfer_items(user_id=user.discord_id)[0]
            self.assertIs(failed_transfer.state, ModWebNotificationTrayItemState.ERROR)
            ui.upload_control.handlers["rejected"]()
            self.assertEqual([tone for _message, tone in ui.notifications], ["info", "negative", "warning"])
            self.assertIn("Upload acknowledged", ui.notifications[0][0])
            self.assertIn("out of temporary space", ui.notifications[1][0])

        self.assertIn("No saves match that search.", [label.text for label in ui.labels])

    def test_remote_node_ui_redirects_to_portal_home_page(self) -> None:
        yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="1350601198637551659",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="https://wakusei.apasz.com",
                    node_api_base_url="https://wakusei.apasz.com/api/node",
                )
            ),
        )
        bot_config = config.BotConfiguration(KnownBots={yuki_snapshot.profile.id: yuki_snapshot})
        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/", query=""))

        with TemporaryDirectory() as temp_dir:
            missing_cache = Path(temp_dir) / "bots.json"
            with (
                patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
                patch.object(config, "MOD_WEB_SERVER", server),
                patch.object(config, "load_bot_configuration", return_value=bot_config),
                patch.object(config, "authority_cache_path", return_value=missing_cache),
            ):
                response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.headers["location"], "https://wakusei.apasz.com/")

    def test_current_node_app_url_targets_node_specific_route(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin", public_base_url="https://portal.example")

        with patch.object(config, "MOD_WEB_SERVER", server):
            self.assertEqual(
                current_node_app_url("minecraft_survival"),
                "https://portal.example/mod-web/nodes/erin/mods/minecraft_survival",
            )

    def test_remote_node_app_page_redirects_to_yuki_portal_app_page(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        request = SimpleNamespace(
            method="GET", url=SimpleNamespace(path="/mod-web/mods/minecraft_survival", query="tab=mods")
        )

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_portal_base_url", return_value="https://wakusei.apasz.com"),
        ):
            response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(
            response.headers["location"],
            "https://wakusei.apasz.com/mod-web/nodes/erin/mods/minecraft_survival?tab=mods",
        )

    def test_remote_node_node_page_redirect_preserves_requested_node_path(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/mod-web/nodes/yuki/mods/minecraft_survival", query="tab=mods"),
        )

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_portal_base_url", return_value="https://wakusei.apasz.com"),
        ):
            response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(
            response.headers["location"],
            "https://wakusei.apasz.com/mod-web/nodes/yuki/mods/minecraft_survival?tab=mods",
        )

    def test_portal_profile_redirects_local_alias_app_page_to_default_remote_node(self) -> None:
        yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="1350601198637551659",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="https://wakusei.apasz.com",
                    node_api_base_url="https://wakusei.apasz.com/api/node",
                )
            ),
        )
        bot_config = config.BotConfiguration(KnownBots={yuki_snapshot.profile.id: yuki_snapshot})
        portal_profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]
        server = replace(config.MOD_WEB_SERVER, node_name="portal")
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/mods/minecraft_survival", query="tab=mods"))

        with TemporaryDirectory() as temp_dir:
            missing_cache = Path(temp_dir) / "bots.json"
            with (
                patch.object(config, "ACTIVE_BOT_PROFILE", portal_profile),
                patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
                patch.object(config, "MOD_WEB_SERVER", server),
                patch.object(config, "load_bot_configuration", return_value=bot_config),
                patch.object(config, "authority_cache_path", return_value=missing_cache),
            ):
                response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.headers["location"], "/mod-web/nodes/yuki/mods/minecraft_survival?tab=mods")

    def test_remote_node_chat_page_redirects_to_yuki_portal_app_chat_page(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/mod-web/chat/minecraft_survival", query=""))

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_portal_base_url", return_value="https://wakusei.apasz.com"),
        ):
            response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(
            response.headers["location"],
            "https://wakusei.apasz.com/mod-web/nodes/erin/chat/minecraft_survival",
        )

    def test_portal_base_url_prefers_portal_snapshot_over_yuki_snapshot(self) -> None:
        yuki_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="1350601198637551659",
                label="Yuki",
                bot_profile=config.BotProfileName.YUKI,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="https://wakusei.apasz.com",
                    node_api_base_url="https://wakusei.apasz.com/api/node",
                )
            ),
        )
        portal_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="764270771350142976",
                label="Portal",
                bot_profile=config.BotProfileName.PORTAL,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="portal",
                    public_base_url="https://portal.example",
                    node_api_base_url="https://portal.example/api/node",
                )
            ),
        )

        with patch.object(ModWebService, "_known_bot_snapshots", return_value=(yuki_snapshot, portal_snapshot)):
            self.assertEqual(ModWebService()._portal_base_url(), "https://portal.example")

    def test_remote_node_api_is_not_redirected(self) -> None:
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/node/apps", query=""))

        with patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE):
            response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNone(response)

    def test_portal_profile_does_not_redirect_portal_owned_routes(self) -> None:
        portal_profile = config.BOT_PROFILES[config.BotProfileName.PORTAL]
        server = replace(config.MOD_WEB_SERVER, node_name="portal")
        cases: tuple[tuple[str, str], ...] = (
            ("/auth/login", "next_path=%2F"),
            ("/mod-web/nodes/yuki", ""),
            ("/mod-web/nodes/erin/mods/minecraft_survival", "tab=mods"),
            ("/mod-web/dev/error/page-unavailable", ""),
            ("/mod-web/assets/fonts/test.woff2", ""),
        )

        with (
            patch.object(config, "ACTIVE_BOT_PROFILE", portal_profile),
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "MOD_WEB_SERVER", server),
        ):
            for path, query in cases:
                with self.subTest(path=path, query=query):
                    request = SimpleNamespace(method="GET", url=SimpleNamespace(path=path, query=query))
                    response = ModWebService()._remote_portal_redirect(cast(Any, request))
                    self.assertIsNone(response)

    def test_app_hero_runtime_details_use_status_text_and_badges(self) -> None:
        stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.4",
            player_count=3,
            player_capacity=20,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=58,
            storage_free_bytes=120 * 1024**3,
            storage_total_bytes=256 * 1024**3,
            footprint_bytes=12 * 1024**3,
        )

        details = ModWebService()._app_hero_runtime_details(stats)

        self.assertEqual(
            details.relay_badge,
            _ModWebBadgeSpec(text="Game <-> Chat", tone="grey", tooltip_text="Chat bridge support"),
        )
        self.assertEqual(
            details.version_badge,
            _ModWebBadgeSpec(
                text="1.20.4",
                tone="black",
                tooltip_text="Game version updated upon start",
            ),
        )
        self.assertEqual(details.player_count_badge, _ModWebBadgeSpec(text="3 / 20", tone="purple"))
        self.assertEqual(details.status_text, "Running")
        self.assertEqual(details.status_tone, "purple")

    def test_app_hero_runtime_details_show_live_version_tooltip_for_installed_file_source(self) -> None:
        stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="2.0.72",
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.NONE,
            version_source=AppVersionSource.INSTALLED_FILES,
            storage_percent=58,
            storage_free_bytes=120 * 1024**3,
            storage_total_bytes=256 * 1024**3,
        )

        details = ModWebService()._app_hero_runtime_details(stats)

        self.assertEqual(
            details.version_badge,
            _ModWebBadgeSpec(
                text="2.0.72",
                tone="black",
                tooltip_text="Game version updated live",
            ),
        )

    def test_app_hero_runtime_details_render_unlimited_player_capacity(self) -> None:
        stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.4",
            player_count=3,
            player_capacity=-1,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=58,
            storage_free_bytes=120 * 1024**3,
            storage_total_bytes=256 * 1024**3,
            footprint_bytes=12 * 1024**3,
        )

        details = ModWebService()._app_hero_runtime_details(stats)

        self.assertEqual(details.player_count_badge, _ModWebBadgeSpec(text="3 / ∞", tone="purple"))

    def test_app_hero_runtime_details_prefer_transition_status(self) -> None:
        stats = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.20.4",
            player_count=0,
            player_capacity=20,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=58,
            storage_free_bytes=120 * 1024**3,
            storage_total_bytes=256 * 1024**3,
            footprint_bytes=12 * 1024**3,
            transition_state=NodeAppTransitionState.STARTING,
        )

        details = ModWebService()._app_hero_runtime_details(stats)

        self.assertEqual(details.status_text, "Starting")
        self.assertEqual(details.status_tone, "purple")
        self.assertEqual(details.player_count_badge, _ModWebBadgeSpec(text="0 / 20", tone="grey"))

    def test_app_hero_runtime_details_show_crashed_status(self) -> None:
        stats = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version="1.20.4",
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.BIDIRECTIONAL,
            storage_percent=58,
            storage_free_bytes=120 * 1024**3,
            storage_total_bytes=256 * 1024**3,
            footprint_bytes=12 * 1024**3,
            runtime_fault=AppRuntimeFault(kind=AppRuntimeFaultKind.CRASH, summary="Failed to start the minecraft server"),
        )

        details = ModWebService()._app_hero_runtime_details(stats)

        self.assertEqual(details.status_text, "Crashed")
        self.assertEqual(details.status_tone, "red")

    def test_app_page_hero_badges_keep_only_resource_point_badges(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=True,
            supports_save_rename=True,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(self._config_entry(root_id="public", root_label="Public Configs", relative_path="server.properties"),),
            ),
            saves=NodeSaveList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                roots=(),
                saves=(),
            ),
            app_stats=None,
            app_start_blocked=False,
            settings=NodeSettingList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                editable_count=0,
                restricted_count=0,
                has_pending_changes=False,
                pending_change_count=0,
                required_save_level_name=Power_Level.user.name,
                required_reload_level_name=Power_Level.user.name,
                settings=(),
            ),
            console_actions=NodeConsoleActionList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                actions=(),
            ),
            resource_points=NodeAppResourcePointSummary(
                cpu_points_running=3,
                cpu_points_startup=5,
                ram_points_running=8,
                ram_points_startup=8,
            ),
            mods=NodeModList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                summary=NodeModSummary(
                    total_count=4,
                    enabled_count=4,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=4,
                    non_downloadable_count=0,
                ),
                mods=(),
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )

        badges = service._app_page_hero_badges(model)

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(
                    text="3 (5)",
                    tone="black",
                    icon="speed",
                    tooltip_text="CPU points required for running (starting)",
                ),
                _ModWebBadgeSpec(
                    text="8",
                    tone="black",
                    icon="memory",
                    tooltip_text="RAM points required for running",
                ),
            ),
        )

    def test_visible_app_activity_provider_badges_only_include_enabled_providers_with_values(self) -> None:
        model = ModWebBasePageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            activity_providers=(
                NodeAppActivityProviderEntry(provider_id="players", label="Player Count", enabled=True),
                NodeAppActivityProviderEntry(provider_id="day", label="Day Counter", enabled=False),
                NodeAppActivityProviderEntry(provider_id="stage", label="Stage", enabled=True),
            ),
        )
        runtime_summary = NodeAppRuntimeSummary(
            running=True,
            enabled=True,
            version="1.21.1",
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
            activity_providers=(
                NodeAppActivityProviderEntry(provider_id="players", label="Player Count", enabled=True, current_value="3/20"),
                NodeAppActivityProviderEntry(provider_id="day", label="Day Counter", enabled=False, current_value="D2"),
                NodeAppActivityProviderEntry(provider_id="stage", label="Stage", enabled=True, current_value="T3"),
            ),
        )

        badges = ModWebService._visible_app_activity_provider_badges(
            app_stats=runtime_summary,
            activity_providers=ModWebService._enabled_app_activity_providers(model),
        )

        self.assertEqual(
            badges,
            (
                "Player Count: 3/20",
                "Tier 3",
            ),
        )

    def test_app_activity_provider_badge_markup_formats_stage_value(self) -> None:
        self.assertEqual(
            ModWebService._app_activity_provider_badge_markup(
                provider_id="stage",
                label="Stage",
                current_value="T3: Schematic 3-2",
            ),
            "Tier 3: Schematic 3-2",
        )

    def test_app_activity_provider_badge_markup_uses_raw_map_age_value(self) -> None:
        self.assertEqual(
            ModWebService._app_activity_provider_badge_markup(
                provider_id="map_age",
                label="Map Age",
                current_value="D0/H04",
            ),
            "D0/H04",
        )

    def test_app_activity_provider_badge_markup_formats_sevendays_time_and_blood_moon(self) -> None:
        self.assertEqual(
            ModWebService._app_activity_provider_badge_markup(
                provider_id="time",
                label="Game Time",
                current_value="D14/H07",
            ),
            "Day 14 Hour 7",
        )
        self.assertEqual(
            ModWebService._app_activity_provider_badge_markup(
                provider_id="time",
                label="Game Time",
                current_value="!D14/H21",
            ),
            'Day <span class="mod-app-activity-alert">14</span> Hour 21',
        )

    def test_app_activity_provider_tooltips_explain_values_and_player_names(self) -> None:
        service = ModWebService()

        self.assertEqual(
            service._app_activity_provider_tooltip_html(
                provider=NodeAppActivityProviderEntry(
                    provider_id="time",
                    label="Game Time",
                    enabled=True,
                    current_value="!D14/H21",
                )
            ),
            "Game Time<br>Day: 14<br>Hour: 21<br>Blood moon active",
        )
        self.assertEqual(
            service._app_activity_provider_tooltip_html(
                provider=NodeAppActivityProviderEntry(
                    provider_id="players",
                    label="Player Count",
                    enabled=True,
                    current_value="2/20",
                ),
                connected_player_names=("Yoko", "Bea"),
            ),
            "Player Count<br>Current value: 2/20<br>Connected players:<br>Yoko<br>Bea",
        )
        self.assertEqual(
            service._app_activity_provider_tooltip_html(
                provider=NodeAppActivityProviderEntry(
                    provider_id="evolution",
                    label="Evolution",
                    enabled=True,
                    current_value="37.5%",
                    detail_value="Nauvis: 37.5%\nGleba: 12.5%",
                )
            ),
            "Evolution<br>Nauvis: 37.5%<br>Gleba: 12.5%",
        )

    def test_app_title_font_style_resolves_auto_preset_from_app_scope(self) -> None:
        minecraft_style = ModWebService._app_title_font_style(
            app_name="minecraft_alpha",
            title_font_preset=AppTitleFont.AUTO.value,
        )
        default_style = ModWebService._app_title_font_style(
            app_name="custom_alpha",
            title_font_preset=AppTitleFont.AUTO.value,
        )

        self.assertIsNotNone(minecraft_style)
        assert minecraft_style is not None
        self.assertIn("Press Start 2P", minecraft_style)
        self.assertIsNone(default_style)

    def test_app_title_font_style_for_custom_font_forces_normal_weight(self) -> None:
        custom_style = ModWebService._app_title_font_style(
            app_name="minecraft_alpha",
            title_font_preset="Black Ops One",
        )

        self.assertIsNotNone(custom_style)
        assert custom_style is not None
        self.assertIn('font-family: "Black Ops One"', custom_style)
        self.assertIn("font-weight: 400", custom_style)

    def test_app_title_font_options_include_matching_custom_fonts(self) -> None:
        original_entries = font_assets.entries
        try:
            font_assets._entries = (
                FontAssetEntry(
                    family_name="Minecraft Ten",
                    scope="minecraft",
                    source_path=Path("resources/fonts/minecraft/minecraft.ttf"),
                    woff_path=Path("resources/fonts/minecraft/minecraft.woff"),
                    woff2_path=Path("resources/fonts/minecraft/minecraft.woff2"),
                ),
                FontAssetEntry(
                    family_name="Official Block",
                    scope="minecraft",
                    source_path=Path("resources/fonts/minecraft/official.ttf"),
                    woff_path=Path("resources/fonts/minecraft/official.woff"),
                    woff2_path=Path("resources/fonts/minecraft/official.woff2"),
                ),
                FontAssetEntry(
                    family_name="Factorio Header",
                    scope="factorio",
                    source_path=Path("resources/fonts/factorio/header.ttf"),
                    woff_path=Path("resources/fonts/factorio/header.woff"),
                    woff2_path=Path("resources/fonts/factorio/header.woff2"),
                ),
            )
            options = ModWebService._app_title_font_options(app_name="minecraft_alpha")
        finally:
            font_assets._entries = original_entries

        self.assertEqual(options["Official Block"], "Official Block")
        self.assertNotIn("Factorio Header", options)

    def test_app_page_node_badge_tone_marks_remote_summary_failures_as_red(self) -> None:
        self.assertEqual(
            ModWebService()._app_page_node_badge_tone(
                node_name="erin",
                system_summary=None,
            ),
            "red",
        )
        self.assertEqual(
            ModWebService()._app_page_node_badge_tone(
                node_name="yuki",
                system_summary=None,
            ),
            "red",
        )

    def test_app_page_node_presence_badge_spec_uses_black_for_alive_and_red_for_down(self) -> None:
        badge_element = cast(Any, SimpleNamespace(id=17))

        spec = ModWebService._app_page_node_presence_badge_spec(
            node_name="erin",
            badge_element=badge_element,
            presence_stream_url="/mod-web/node-presence/erin",
        )

        self.assertIsNotNone(spec)
        if spec is None:
            self.fail("Expected a node presence badge spec.")
        self.assertEqual(spec.pending_text, "erin")
        self.assertEqual(spec.alive_text, "erin")
        self.assertEqual(spec.down_text, "erin")
        self.assertEqual(
            spec.healthy_class_name,
            "mod-badge black mod-app-corner-badge mod-app-node-badge",
        )
        self.assertEqual(
            spec.unhealthy_class_name,
            "mod-badge red mod-app-corner-badge mod-app-node-badge",
        )

    def test_register_timer_cleanup_cancels_timer_when_owner_is_deleted(self) -> None:
        service = ModWebService()
        owner = _FakeCleanupOwner()
        timer = _FakeCleanupTimer(_FakeCleanupSlot(parent=owner))
        ui = SimpleNamespace(context=SimpleNamespace(client=_FakeCleanupClient()))

        service._register_timer_cleanup(ui=cast(ModWebUi, cast(object, ui)), timer=timer)
        owner._handle_delete()

        self.assertEqual(timer.cancel_calls, [True])
        self.assertTrue(timer._deleted)
        self.assertEqual(owner.delete_call_count, 1)

    def test_register_timer_cleanup_converts_deleted_parent_slot_to_shutdown(self) -> None:
        service = ModWebService()
        owner = _FakeCleanupOwner()
        timer = _FakeCleanupTimer(_FakeCleanupSlot(parent=owner))
        ui = SimpleNamespace(context=SimpleNamespace(client=_FakeCleanupClient()))

        service._register_timer_cleanup(ui=cast(ModWebUi, cast(object, ui)), timer=timer)
        timer.raise_deleted_parent_slot = True

        context = timer._get_context()
        with context:
            pass

        self.assertEqual(timer.cancel_calls, [True])
        self.assertTrue(timer._deleted)

    def test_ui_client_is_alive_rejects_deleted_nicegui_client(self) -> None:
        service = ModWebService()
        deleted_ui = SimpleNamespace(context=SimpleNamespace(client=_FakeCleanupClient(deleted=True)))
        live_ui = SimpleNamespace(context=SimpleNamespace(client=_FakeCleanupClient()))

        self.assertFalse(service._ui_client_is_alive(ui=cast(ModWebUi, cast(object, deleted_ui))))
        self.assertTrue(service._ui_client_is_alive(ui=cast(ModWebUi, cast(object, live_ui))))

    def test_config_options_use_root_and_relative_path_labels(self) -> None:
        configs = (
            self._config_entry(root_id="server", root_label="Server Properties", relative_path="server.properties"),
            self._config_entry(
                root_id="mods",
                root_label="Mod Configs",
                relative_path="almostunified/debug.json",
                kind="mod",
                size_bytes=456,
                modified_at="2026-05-27 12:01:00",
            ),
        )

        options = ModWebService._config_options(configs)

        self.assertEqual(
            options,
            (
                ModWebSearchOption(
                    option_id="server/server.properties",
                    label="Server Properties / server.properties",
                    search_text="server properties server.properties server.properties",
                ),
                ModWebSearchOption(
                    option_id="mods/almostunified/debug.json",
                    label="Mod Configs / almostunified/debug.json",
                    search_text="mod configs almostunified/debug.json debug.json",
                ),
            ),
        )

    def test_config_file_options_use_relative_path_labels(self) -> None:
        configs = (
            self._config_entry(root_id="mods", root_label="Mod Configs", relative_path="almostunified/debug.json"),
            self._config_entry(
                root_id="mods",
                root_label="Mod Configs",
                relative_path="almostunified/materials.json",
                size_bytes=456,
                modified_at="2026-05-27 12:01:00",
            ),
        )

        options = ModWebService._config_file_options(configs)

        self.assertEqual(
            options,
            (
                ModWebSearchOption(
                    option_id="mods/almostunified/debug.json",
                    label="almostunified/debug.json",
                    search_text="mod configs almostunified/debug.json debug.json",
                ),
                ModWebSearchOption(
                    option_id="mods/almostunified/materials.json",
                    label="almostunified/materials.json",
                    search_text="mod configs almostunified/materials.json materials.json",
                ),
            ),
        )

    def test_config_editor_language_detects_known_formats(self) -> None:
        self.assertEqual(
            ModWebService._config_editor_language(
                self._config_entry(root_id="server", root_label="Server", relative_path="server.properties")
            ),
            "Properties files",
        )
        self.assertEqual(
            ModWebService._config_editor_language(
                self._config_entry(root_id="mods", root_label="Mods", relative_path="config/settings.toml")
            ),
            "TOML",
        )
        self.assertEqual(
            ModWebService._config_editor_language(
                self._config_entry(root_id="mods", root_label="Mods", relative_path="kubejs/data.json")
            ),
            "JSON",
        )
        self.assertEqual(
            ModWebService._config_editor_language(
                self._config_entry(root_id="server", root_label="Server", relative_path="serverconfig.xml")
            ),
            "XML",
        )
        self.assertEqual(
            ModWebService._config_editor_language(
                self._config_entry(root_id="server", root_label="Server", relative_path="docker/Dockerfile")
            ),
            "Dockerfile",
        )

    def test_config_editor_language_returns_none_for_unknown_formats(self) -> None:
        self.assertIsNone(
            ModWebService._config_editor_language(
                self._config_entry(root_id="server", root_label="Server", relative_path="server_config.sii")
            )
        )

    def test_filtered_config_options_keeps_selected_item_while_showing_matches(self) -> None:
        config_options = (
            ModWebSearchOption(
                option_id="server/server.properties",
                label="Server Properties / server.properties",
                search_text="server properties server.properties server.properties",
            ),
            ModWebSearchOption(
                option_id="mods/almostunified/debug.json",
                label="Mod Configs / almostunified/debug.json",
                search_text="mod configs almostunified/debug.json debug.json",
            ),
            ModWebSearchOption(
                option_id="mods/almostunified/materials.json",
                label="Mod Configs / almostunified/materials.json",
                search_text="mod configs almostunified/materials.json materials.json",
            ),
        )

        filtered = ModWebService._filtered_search_options(
            options=config_options,
            search_query="almostunified",
            selected_id="server/server.properties",
        )

        self.assertEqual(
            filtered,
            {
                "server/server.properties": "Server Properties / server.properties",
                "mods/almostunified/debug.json": "Mod Configs / almostunified/debug.json",
                "mods/almostunified/materials.json": "Mod Configs / almostunified/materials.json",
            },
        )

    def test_setting_control_kind_uses_switch_for_boolean_entries(self) -> None:
        setting = self._setting_entry(
            key="auto_pause",
            label="Auto Pause",
            type_name="bool",
            value_text="Enabled",
            current_input_value="Enabled",
            strict_choice=True,
            allows_text_input=False,
            choices=(
                NodeSettingChoice(label="Enabled", raw_value="true"),
                NodeSettingChoice(label="Disabled", raw_value="false"),
            ),
        )

        self.assertEqual(ModWebService._setting_control_kind(setting), ModWebSettingControlKind.BOOLEAN_SWITCH)
        self.assertTrue(ModWebService._setting_switch_value(setting))
        self.assertEqual(ModWebService._setting_boolean_submit_value(setting, False), "false")

    def test_setting_control_kind_uses_select_for_strict_non_boolean_choices(self) -> None:
        setting = self._setting_entry(
            key="network_quality",
            label="Network Quality",
            type_name="str",
            value_text="Medium",
            current_input_value="Medium",
            strict_choice=True,
            allows_text_input=False,
            choices=(
                NodeSettingChoice(label="Low", raw_value="low"),
                NodeSettingChoice(label="Medium", raw_value="medium"),
                NodeSettingChoice(label="High", raw_value="high"),
            ),
        )

        self.assertEqual(ModWebService._setting_control_kind(setting), ModWebSettingControlKind.CHOICE_SELECT)

    def test_setting_control_kind_uses_editable_select_for_non_strict_choices(self) -> None:
        setting = self._setting_entry(
            key="GameWorld",
            label="Game World",
            type_name="str",
            value_text="Navezgane",
            current_input_value="Navezgane",
            strict_choice=False,
            allows_text_input=True,
            choices=(
                NodeSettingChoice(label="Navezgane", raw_value="Navezgane"),
                NodeSettingChoice(label="Wizefoco Mountains", raw_value="Wizefoco Mountains"),
            ),
        )

        self.assertEqual(ModWebService._setting_control_kind(setting), ModWebSettingControlKind.EDITABLE_CHOICE_SELECT)

    def test_setting_control_kind_keeps_binary_integer_choices_as_dropdowns(self) -> None:
        setting = self._setting_entry(
            key="enemy_difficulty",
            label="Enemy Difficulty",
            type_name="int",
            value_text="Normal",
            current_input_value="Normal",
            strict_choice=True,
            allows_text_input=False,
            choices=(
                NodeSettingChoice(label="Normal", raw_value="0"),
                NodeSettingChoice(label="Feral", raw_value="1"),
            ),
        )

        self.assertEqual(ModWebService._setting_control_kind(setting), ModWebSettingControlKind.CHOICE_SELECT)

    def test_filter_setting_entries_matches_search_text(self) -> None:
        settings = (
            self._setting_entry(
                key="server_name",
                label="Server Name",
                type_name="str",
                permission_level="Admin",
                value_text="Alpha",
                current_input_value="Alpha",
            ),
            self._setting_entry(
                key="max_players",
                label="Max Players",
                type_name="int",
                permission_level="User",
                value_text="20",
                current_input_value="20",
            ),
        )

        filtered = ModWebService._filter_setting_entries(
            settings=settings,
            options=ModWebService._setting_options(settings),
            search_query="players max",
        )

        self.assertEqual(filtered, (settings[1],))

    def test_filter_setting_entries_ignores_setting_metadata_not_shown_on_card(self) -> None:
        settings = (
            self._setting_entry(
                key="server_name",
                label="Server Name",
                type_name="str",
                permission_level="Admin",
                description="Shown in browser listings.",
                value_text="Alpha",
                current_input_value="Alpha",
            ),
        )

        filtered = ModWebService._filter_setting_entries(
            settings=settings,
            options=ModWebService._setting_options(settings),
            search_query="admin browser",
        )

        self.assertEqual(filtered, ())

    def test_setting_text_validation_message_rejects_blank_values(self) -> None:
        setting = self._setting_entry(
            key="server_name",
            label="Server Name",
            type_name="str",
        )

        self.assertEqual(ModWebService._setting_text_validation_message(setting, "   "), "Value required.")

    def test_setting_text_validation_message_allows_blank_when_setting_metadata_allows_it(self) -> None:
        setting = self._setting_entry(
            key="level-seed",
            label="Level Seed",
            type_name="str",
            allows_blank_input=True,
        )

        self.assertIsNone(ModWebService._setting_text_validation_message(setting, "   "))

    def test_setting_permission_badge_tone_reflects_edit_access(self) -> None:
        editable = self._setting_entry(
            key="server_name",
            label="Server Name",
            type_name="str",
            can_edit=True,
        )
        locked = self._setting_entry(
            key="server_name",
            label="Server Name",
            type_name="str",
            can_edit=False,
        )

        self.assertEqual(ModWebService._setting_permission_badge_tone(editable), "grey")
        self.assertEqual(ModWebService._setting_permission_badge_tone(locked), "warn")

    def test_hidden_setting_display_text_keeps_redacted_for_sensitive_main_value(self) -> None:
        setting = self._setting_entry(
            key="admin_password",
            label="Admin Password",
            type_name="str",
            is_sensitive=True,
            value_text="REDACTED",
            can_edit=False,
            value_is_hidden=True,
        )

        self.assertEqual(ModWebService._hidden_setting_display_text(setting), "REDACTED")

    def test_hidden_setting_display_text_obfuscates_non_sensitive_hidden_values(self) -> None:
        setting = self._setting_entry(
            key="admin_slots",
            label="Admin Slots",
            type_name="int",
            permission_level="Sudo",
            value_text="Hidden (requires Sudo)",
            can_edit=False,
            value_is_hidden=True,
        )

        self.assertEqual(ModWebService._hidden_setting_display_text(setting), "RWTE A?#2 ZFJW")
        self.assertEqual(ModWebService._hidden_setting_display_text(setting, variant=1), "PMLQ QYD$ VB#C")
        self.assertEqual(ModWebService._hidden_setting_display_text(setting, variant=2), "LH#F $MEB FBN*")

    def test_hidden_setting_cycle_texts_mutate_obfuscated_value_incrementally(self) -> None:
        setting = self._setting_entry(
            key="admin_slots",
            label="Admin Slots",
            type_name="int",
            permission_level="Sudo",
            value_text="Hidden (requires Sudo)",
            can_edit=False,
            value_is_hidden=True,
        )

        cycle_texts = ModWebService._hidden_setting_cycle_texts(setting)

        self.assertEqual(len(cycle_texts), 4)
        self.assertEqual(cycle_texts[0], ModWebService._hidden_setting_display_text(setting))
        for previous_text, next_text in zip(cycle_texts, cycle_texts[1:]):
            self.assertEqual((previous_text[4], previous_text[9]), (" ", " "))
            self.assertEqual((next_text[4], next_text[9]), (" ", " "))
            difference_count = sum(
                previous_char != next_char
                for previous_char, next_char in zip(previous_text, next_text, strict=True)
                if previous_char != " "
            )
            self.assertIn(difference_count, (1, 2))

    def test_hero_badge_class_helpers_support_dashboard_fill_layout(self) -> None:
        self.assertEqual(ModWebService._hero_badges_classes(), "mod-corner-badges")
        self.assertEqual(ModWebService._hero_badges_classes(wide=True), "mod-corner-badges mod-corner-badges-wide")
        self.assertEqual(ModWebService._hero_badge_row_classes(), "mod-corner-badge-row")
        self.assertEqual(
            ModWebService._hero_badge_row_classes(fill=True),
            "mod-corner-badge-row mod-corner-badge-row-fill",
        )

    def test_setting_secret_style_is_deterministic_and_setting_specific(self) -> None:
        first_setting = self._setting_entry(key="admin_password", label="Admin Password", type_name="str")
        second_setting = self._setting_entry(key="server_password", label="Server Password", type_name="str")

        first_style = ModWebService._setting_secret_style(first_setting)

        self.assertEqual(first_style, ModWebService._setting_secret_style(first_setting))
        self.assertNotEqual(first_style, ModWebService._setting_secret_style(second_setting))
        self.assertIn("--mod-setting-secret-cycle-duration:", first_style)
        self.assertIn("--mod-setting-secret-flicker-duration:", first_style)
        self.assertIn("--mod-setting-secret-shift-delay-b:", first_style)

    def test_render_setting_meta_value_uses_cycle_markup_for_hidden_non_sensitive_values(self) -> None:
        class _FakeHtmlUi:
            def __init__(self) -> None:
                self.html_fragments: list[str] = []

            def html(self, content: str = "", *args: Any, **kwargs: Any) -> Any:
                self.html_fragments.append(content)
                return None

            def label(self, text: str = "", *args: Any, **kwargs: Any) -> Any:
                raise AssertionError(f"Unexpected label render for hidden setting: {text!r}")

        setting = self._setting_entry(
            key="admin_slots",
            label="Admin Slots",
            type_name="int",
            permission_level="Sudo",
            value_text="Hidden (requires Sudo)",
            can_edit=False,
            value_is_hidden=True,
        )
        ui = _FakeHtmlUi()

        ModWebService._render_setting_meta_value(ui=cast(Any, ui), setting=setting)

        self.assertEqual(len(ui.html_fragments), 1)
        markup = ui.html_fragments[0]
        self.assertIn("mod-setting-meta-secret-cycle", markup)
        self.assertIn("mod-setting-meta-secret-cycle-token", markup)
        self.assertIn('data-text="RWTE A?#2 ZFJW"', markup)
        self.assertNotIn("REDACTED", markup)

    def test_render_setting_meta_value_adds_hover_reveal_for_privileged_hidden_values(self) -> None:
        class _FakeHtmlUi:
            def __init__(self) -> None:
                self.html_fragments: list[str] = []

            def html(self, content: str = "", *args: Any, **kwargs: Any) -> Any:
                self.html_fragments.append(content)
                return None

            def label(self, text: str = "", *args: Any, **kwargs: Any) -> Any:
                raise AssertionError(f"Unexpected label render for hidden setting: {text!r}")

        setting = self._setting_entry(
            key="admin_password",
            label="Admin Password",
            type_name="str",
            is_sensitive=True,
            value_text="REDACTED",
            revealed_value_text="secret",
            can_edit=True,
            value_is_hidden=True,
            can_reveal_hidden_text=True,
        )
        ui = _FakeHtmlUi()

        ModWebService._render_setting_meta_value(ui=cast(Any, ui), setting=setting)

        self.assertEqual(len(ui.html_fragments), 1)
        markup = ui.html_fragments[0]
        self.assertIn("mod-setting-meta-secret-revealable", markup)
        self.assertIn('tabindex="0"', markup)
        self.assertIn("mod-setting-meta-secret-reveal", markup)
        self.assertIn("mod-setting-meta-secret-reveal-token", markup)
        self.assertIn(">secret</span>", markup)
        self.assertIn(">REDACTED</span>", markup)

    def test_setting_text_input_props_use_compact_square_field_contract(self) -> None:
        int_setting = self._setting_entry(key="slots", label="Slots", type_name="int")
        text_setting = self._setting_entry(key="motd", label="MOTD", type_name="str")
        hidden_setting = self._setting_entry(
            key="token",
            label="Token",
            type_name="str",
            value_is_hidden=True,
        )

        self.assertEqual(
            ModWebService._setting_text_input_props(int_setting),
            "filled square dense clearable hide-bottom-space color=accent type=number inputmode=numeric step=1",
        )
        self.assertEqual(
            ModWebService._setting_text_input_props(text_setting),
            "filled square dense clearable hide-bottom-space color=accent",
        )
        self.assertEqual(
            ModWebService._setting_text_input_props(hidden_setting),
            "filled square dense clearable hide-bottom-space color=accent type=password autocomplete=off",
        )

    def test_setting_paragraph_props_use_full_height_textarea_contract(self) -> None:
        text_setting = self._setting_entry(key="motd", label="MOTD", type_name="str", paragraph=True)
        hidden_setting = self._setting_entry(
            key="token",
            label="Token",
            type_name="str",
            paragraph=True,
            value_is_hidden=True,
        )

        self.assertEqual(
            ModWebService._setting_paragraph_props(text_setting),
            "filled square hide-bottom-space color=accent "
            "spellcheck=false autocorrect=off autocapitalize=off "
            "rows=2 input-style=height:100%;min-height:100%;max-height:100%;resize:none",
        )
        self.assertEqual(
            ModWebService._setting_paragraph_props(hidden_setting),
            "filled square hide-bottom-space color=accent "
            "type=password autocomplete=off spellcheck=false autocorrect=off autocapitalize=off "
            "rows=2 input-style=height:100%;min-height:100%;max-height:100%;resize:none",
        )

    def test_hidden_text_settings_do_not_initialise_blank_drafts(self) -> None:
        hidden_setting = self._setting_entry(
            key="admin_password",
            label="Admin Password",
            type_name="str",
            can_edit=True,
            value_is_hidden=True,
        )
        visible_setting = self._setting_entry(
            key="motd",
            label="MOTD",
            type_name="str",
            can_edit=True,
            value_is_hidden=False,
        )

        self.assertFalse(ModWebService._should_initialise_text_setting_draft(hidden_setting))
        self.assertTrue(ModWebService._should_initialise_text_setting_draft(visible_setting))

    def test_setting_select_props_use_compact_popup_theme(self) -> None:
        self.assertEqual(
            ModWebService._setting_select_props(),
            "filled square dense clearable hide-bottom-space color=accent options-dense popup-content-class=mod-setting-menu",
        )

    def test_setting_choice_select_props_remove_clearable_for_strict_dropdowns(self) -> None:
        self.assertEqual(
            ModWebService._setting_choice_select_props(),
            "filled square dense hide-bottom-space color=accent options-dense popup-content-class=mod-setting-menu",
        )

    def test_setting_editable_choice_select_props_allow_free_text(self) -> None:
        self.assertEqual(
            ModWebService._setting_editable_choice_select_props(),
            "filled square dense clearable hide-bottom-space color=accent options-dense "
            "popup-content-class=mod-setting-menu use-input new-value-mode=add input-debounce=0",
        )

    def test_setting_aux_select_props_adds_integrated_prefix(self) -> None:
        self.assertEqual(
            ModWebService._setting_aux_select_props(prefix="Preset"),
            "filled square dense clearable hide-bottom-space color=accent options-dense popup-content-class=mod-setting-menu prefix=Preset",
        )
        self.assertEqual(
            ModWebService._setting_aux_select_props(prefix="Recent"),
            "filled square dense clearable hide-bottom-space color=accent options-dense popup-content-class=mod-setting-menu prefix=Recent",
        )

    def test_config_select_props_use_notepad_popup_theme(self) -> None:
        self.assertEqual(
            ModWebService._config_select_props(clearable=False),
            "outlined options-dense popup-content-class=mod-notepad-menu",
        )
        self.assertEqual(
            ModWebService._config_select_props(clearable=True),
            "outlined clearable options-dense popup-content-class=mod-notepad-menu",
        )

    def test_setting_control_surface_classes_reflect_editability(self) -> None:
        self.assertEqual(ModWebService._setting_control_surface_classes(can_edit=True), "mod-setting-control-surface")
        self.assertEqual(
            ModWebService._setting_control_surface_classes(can_edit=False),
            "mod-setting-control-surface locked",
        )

    def test_setting_text_validation_message_rejects_non_integer_text_for_int_fields(self) -> None:
        setting = self._setting_entry(
            key="max_players",
            label="Max Players",
            type_name="int",
        )

        self.assertEqual(
            ModWebService._setting_text_validation_message(setting, "12.5"),
            "Enter a whole number.",
        )
        self.assertIsNone(ModWebService._setting_text_validation_message(setting, "12"))

    def test_setting_text_draft_validation_ignores_unchanged_invalid_text(self) -> None:
        setting = self._setting_entry(
            key="server_name",
            label="Server Name",
            type_name="str",
            current_input_value="",
            allows_blank_input=False,
        )

        self.assertIsNone(
            ModWebService._setting_text_draft_validation_message(
                setting=setting,
                value="",
                draft_values={},
            )
        )
        self.assertEqual(
            ModWebService._setting_text_draft_validation_message(
                setting=setting,
                value="",
                draft_values={"server_name": ""},
            ),
            "Value required.",
        )

    def test_setting_text_input_value_prefers_event_payload_over_control_state(self) -> None:
        input_control = SimpleNamespace(value="0")
        event = SimpleNamespace(args="6")

        self.assertEqual(
            ModWebService._setting_text_input_value(input_control=cast(Any, input_control), event=cast(Any, event)),
            "6",
        )
        self.assertEqual(
            ModWebService._setting_text_input_value(input_control=cast(Any, input_control)),
            "0",
        )

    def test_apply_linked_setting_drafts_swaps_trader_biome_values(self) -> None:
        rekt = self._setting_entry(
            key="TraderRektBiome",
            label="Trader Rekt Biome",
            type_name="str",
            current_input_value="Forest",
            strict_choice=True,
        )
        jen = self._setting_entry(
            key="TraderJenBiome",
            label="Trader Jen Biome",
            type_name="str",
            current_input_value="Burnt Forest",
            strict_choice=True,
        )
        drafts: dict[str, bool | str] = {}

        ModWebService._set_setting_draft_value(
            setting=rekt,
            value="Burnt Forest",
            draft_values=drafts,
        )
        refreshed = ModWebService._apply_linked_setting_drafts(
            settings=(rekt, jen),
            setting=rekt,
            previous_value="Forest",
            next_value="Burnt Forest",
            draft_values=drafts,
        )

        self.assertTrue(refreshed)
        self.assertEqual(drafts, {"TraderRektBiome": "Burnt Forest", "TraderJenBiome": "Forest"})

    def test_config_root_options_group_by_root(self) -> None:
        configs = (
            self._config_entry(root_id="server", root_label="Server Properties", relative_path="server.properties"),
            self._config_entry(
                root_id="server-config",
                root_label="Server Config",
                relative_path="serverconfig.xml",
                size_bytes=456,
                modified_at="2026-05-27 12:01:00",
            ),
        )

        grouped = ModWebService._configs_by_root(configs)
        options = ModWebService._config_root_options(grouped)

        self.assertEqual(
            options,
            (
                ModWebSearchOption(
                    option_id="server",
                    label="Server Properties",
                    search_text="server properties server",
                ),
                ModWebSearchOption(
                    option_id="server-config",
                    label="Server Config",
                    search_text="server config server-config",
                ),
            ),
        )

    def test_config_single_file_root_options_use_root_labels(self) -> None:
        configs = (
            self._config_entry(root_id="server", root_label="Server Properties", relative_path="server.properties"),
            self._config_entry(
                root_id="server-config",
                root_label="Server Config",
                relative_path="serverconfig.xml",
                size_bytes=456,
                modified_at="2026-05-27 12:01:00",
            ),
        )

        options = ModWebService._config_single_file_root_options(ModWebService._configs_by_root(configs))

        self.assertEqual(
            options,
            (
                ModWebSearchOption(
                    option_id="server/server.properties",
                    label="Server Properties",
                    search_text="server properties server server.properties server.properties",
                ),
                ModWebSearchOption(
                    option_id="server-config/serverconfig.xml",
                    label="Server Config",
                    search_text="server config server-config serverconfig.xml serverconfig.xml",
                ),
            ),
        )

    def test_config_editor_layout_identifies_single_file(self) -> None:
        layout = ModWebService._config_editor_layout(
            (self._config_entry(root_id="server", root_label="Server Config", relative_path="serverconfig.xml"),)
        )

        self.assertEqual(layout.shape, ModWebConfigEditorShape.SINGLE_FILE)
        self.assertFalse(layout.shows_file_selector)
        self.assertFalse(layout.shows_root_selector)

    def test_config_editor_layout_identifies_single_folder_multi_file(self) -> None:
        layout = ModWebService._config_editor_layout(
            (
                self._config_entry(root_id="mods", root_label="Mod Configs", relative_path="a.json"),
                self._config_entry(root_id="mods", root_label="Mod Configs", relative_path="b.json"),
            )
        )

        self.assertEqual(layout.shape, ModWebConfigEditorShape.SINGLE_FOLDER_MULTI_FILE)
        self.assertEqual(layout.primary_selector_label, "File")

    def test_config_editor_layout_identifies_multi_folder_single_file(self) -> None:
        layout = ModWebService._config_editor_layout(
            (
                self._config_entry(root_id="server", root_label="Server Properties", relative_path="server.properties"),
                self._config_entry(
                    root_id="server-config",
                    root_label="Server Config",
                    relative_path="serverconfig.xml",
                ),
            )
        )

        self.assertEqual(layout.shape, ModWebConfigEditorShape.MULTI_FOLDER_SINGLE_FILE)
        self.assertEqual(layout.primary_selector_label, "Config Area")
        self.assertFalse(layout.shows_root_selector)
        self.assertTrue(layout.shows_file_selector)

    def test_config_editor_layout_identifies_multi_folder_multi_file(self) -> None:
        layout = ModWebService._config_editor_layout(
            (
                self._config_entry(root_id="mods", root_label="Mod Configs", relative_path="a.json"),
                self._config_entry(root_id="mods", root_label="Mod Configs", relative_path="b.json"),
                self._config_entry(root_id="server", root_label="Server Properties", relative_path="server.properties"),
            )
        )

        self.assertEqual(layout.shape, ModWebConfigEditorShape.MULTI_FOLDER_MULTI_FILE)
        self.assertTrue(layout.shows_root_selector)
        self.assertEqual(layout.primary_selector_label, "File")

    def test_save_options_use_root_and_relative_path_labels(self) -> None:
        saves = (
            NodeSaveEntry(
                id="world/world",
                label="world",
                relative_path="world",
                root_id="world",
                root_label="Current World",
                kind="directory",
                size_bytes=0,
                size_text="Directory",
                modified_at="2026-05-28 12:00:00",
            ),
        )

        options = ModWebService._save_options(saves)

        self.assertEqual(
            options,
            (
                ModWebSearchOption(
                    option_id="world/world",
                    label="Current World / world",
                    search_text="current world world world directory",
                ),
            ),
        )

    def test_filter_save_entries_matches_search_query(self) -> None:
        saves = (
            NodeSaveEntry(
                id="world/world",
                label="world",
                relative_path="world",
                root_id="world",
                root_label="Current World",
                kind="directory",
                size_bytes=0,
                size_text="Directory",
                modified_at="2026-05-28 12:00:00",
            ),
            NodeSaveEntry(
                id="backups/world-02.zip",
                label="world-02.zip",
                relative_path="backups/world-02.zip",
                root_id="backups",
                root_label="Backups",
                kind="file",
                size_bytes=123,
                size_text="123B",
                modified_at="2026-05-29 12:00:00",
            ),
        )

        options = ModWebService._save_options(saves)

        self.assertEqual(
            ModWebService._filter_save_entries(saves=saves, options=options, search_query="backup"),
            (saves[1],),
        )
        self.assertEqual(
            ModWebService._filter_save_entries(saves=saves, options=options, search_query=""),
            saves,
        )

    def test_save_size_badge_is_hidden_when_it_duplicates_kind(self) -> None:
        directory_entry = NodeSaveEntry(
            id="world/world",
            label="world",
            relative_path="world",
            root_id="world",
            root_label="Current World",
            kind="directory",
            size_bytes=0,
            size_text="Directory",
            modified_at="2026-05-28 12:00:00",
        )
        file_entry = NodeSaveEntry(
            id="backups/world-02.zip",
            label="world-02.zip",
            relative_path="backups/world-02.zip",
            root_id="backups",
            root_label="Backups",
            kind="file",
            size_bytes=123,
            size_text="123B",
            modified_at="2026-05-29 12:00:00",
        )

        self.assertFalse(ModWebService._save_shows_size_badge(directory_entry))
        self.assertTrue(ModWebService._save_shows_size_badge(file_entry))

    def test_save_tile_omits_redundant_root_and_directory_badges(self) -> None:
        class FakeElement:
            class_value: str | None = None

            def classes(self, value: str) -> "FakeElement":
                self.class_value = value
                return self

            def props(self, value: str) -> "FakeElement":
                del value
                return self

            def on(self, event: str, handler: object) -> "FakeElement":
                del event, handler
                return self

            def __enter__(self) -> "FakeElement":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeLabel(FakeElement):
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeUi:
            def __init__(self) -> None:
                self.labels: list[FakeLabel] = []

            def card(self) -> FakeElement:
                return FakeElement()

            def column(self) -> FakeElement:
                return FakeElement()

            def row(self) -> FakeElement:
                return FakeElement()

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def button(self, text: str, *, on_click: object) -> FakeElement:
                del text, on_click
                return FakeElement()

        service = ModWebService()
        ui = FakeUi()
        save = NodeSaveEntry(
            id="navezgane/woabewbies",
            label="woabewbies",
            relative_path="woabewbies",
            root_id="navezgane",
            root_label="Navezgane",
            kind="directory",
            size_bytes=0,
            size_text="Directory",
            modified_at="2026-07-14 11:55:50",
        )
        model = cast(
            ModWebBasePageModel,
            cast(
                object,
                SimpleNamespace(
                    app_friendly="7 Days to Die",
                    supports_save_rename=False,
                ),
            ),
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        service._render_save_tile(
            ui=cast(ModWebUi, cast(object, ui)),
            model=model,
            user=user,
            save=save,
            root_count=2,
            can_write=True,
        )

        labels = [label.text for label in ui.labels]
        self.assertIn("woabewbies", labels)
        self.assertIn("Navezgane / woabewbies", labels)
        self.assertIn("Modified 2026-07-14 11:55:50", labels)
        self.assertNotIn("Navezgane", labels)
        self.assertNotIn("Directory", labels)

    def test_builtin_mod_detection_uses_block_reason(self) -> None:
        builtin_entry = SimpleNamespace(mod_type=ModType.BUILTIN)
        normal_entry = SimpleNamespace(mod_type=ModType.SERVER)

        self.assertTrue(ModWebService._is_builtin_mod(cast(Any, builtin_entry)))
        self.assertFalse(ModWebService._is_builtin_mod(cast(Any, normal_entry)))

    def test_mod_type_badges_use_short_labels_and_distinct_power_badge_tones(self) -> None:
        self.assertEqual(ModType.REGULAR.label, "Regular")
        self.assertEqual(ModType.SERVER.label, "Server")
        self.assertEqual(ModType.CLIENT.label, "Client")
        self.assertEqual(ModWebService._mod_type_badge_tone(ModType.REGULAR), "grey")
        self.assertEqual(ModWebService._mod_type_badge_tone(ModType.SERVER), "warn")
        self.assertEqual(ModWebService._mod_type_badge_tone(ModType.CLIENT), "purple")

    def test_mod_detail_summaries_combine_derived_distribution_fields(self) -> None:
        entry = self._mod_entry(name="example.jar")
        self.assertEqual(ModWebService._mod_download_summary(entry), "Available")
        self.assertEqual(ModWebService._mod_client_pack_summary(entry), "Required")

        blocked = replace(
            entry,
            downloadable=False,
            download_block_label="Artifact",
            client_pack_eligible=False,
        )
        self.assertEqual(ModWebService._mod_download_summary(blocked), "Blocked — Artifact")
        self.assertEqual(
            ModWebService._mod_client_pack_summary(blocked),
            "Excluded — File download blocked",
        )

        optional = replace(
            entry,
            client_pack=ClientPackConfig(
                policy=ClientPackPolicy.OPTIONAL,
                default_selected=True,
            ),
        )
        self.assertEqual(
            ModWebService._mod_client_pack_summary(optional),
            "Optional — included by default",
        )

        alternative = replace(
            entry,
            client_pack=ClientPackConfig(
                policy=ClientPackPolicy.ALTERNATIVE,
                choice_group="minimap",
                default_choice=True,
            ),
        )
        self.assertEqual(
            ModWebService._mod_client_pack_summary(alternative),
            "Alternative — minimap (default)",
        )

        excluded = replace(
            entry,
            client_pack=ClientPackConfig(included_in_client=False),
            client_pack_eligible=False,
        )
        self.assertEqual(ModWebService._mod_client_pack_summary(excluded), "Not included")

        server_only = replace(
            entry,
            mod_type=ModType.SERVER,
            client_pack_eligible=True,
        )
        self.assertEqual(
            ModWebService._mod_client_pack_summary(server_only),
            "Required",
        )

        builtin = replace(
            entry,
            mod_type=ModType.BUILTIN,
            downloadable=False,
            client_pack_eligible=False,
        )
        self.assertEqual(
            ModWebService._mod_client_pack_summary(builtin),
            "Excluded — Built-in",
        )

    def test_selection_toggle_label_switches_between_select_all_and_clear(self) -> None:
        self.assertEqual(ModWebService._selection_toggle_label(selected_count=0), "Select All")
        self.assertEqual(ModWebService._selection_toggle_label(selected_count=2), "Clear")

    def test_render_modlist_supports_all_formats_and_field_options(self) -> None:
        alpha = self._mod_entry(
            name="alpha.jar",
            friendly="Alpha | Tools",
            version="1.2.0",
        )
        beta = self._mod_entry(
            name="beta.jar",
            friendly="Beta",
            version=None,
        )
        mods = (beta, alpha)

        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.PLAINTEXT,
                include_version=True,
                include_filename=True,
            ),
            "Minecraft Alpha [2026-07-04]\n\n"
            "Alpha | Tools [1.2.0] (alpha.jar)\n"
            "Beta [Unknown] (beta.jar)",
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.PLAINTEXT,
                include_version=False,
                include_filename=True,
            ),
            "Minecraft Alpha [2026-07-04]\n\nAlpha | Tools (alpha.jar)\nBeta (beta.jar)",
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.PLAINTEXT,
                include_version=False,
                include_filename=False,
            ),
            "Minecraft Alpha [2026-07-04]\n\nAlpha | Tools\nBeta",
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Factorio Alpha",
                pack_version=None,
                output_format=ModWebModlistFormat.PLAINTEXT,
                include_version=False,
                include_filename=False,
                include_pack_version=False,
            ),
            "Factorio Alpha\n\nAlpha | Tools\nBeta",
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version=None,
                output_format=ModWebModlistFormat.PLAINTEXT,
                include_version=False,
                include_filename=False,
            ),
            "Minecraft Alpha [Unpublished]\n\nAlpha | Tools\nBeta",
        )
        self.assertEqual(
            json.loads(
                ModWebService._render_modlist(
                    mods,
                    instance_name="Minecraft Alpha",
                    pack_version="2026-07-04",
                    output_format=ModWebModlistFormat.JSON,
                    include_version=False,
                    include_filename=True,
                )
            ),
            [
                {"name": "Alpha | Tools", "filename": "alpha.jar"},
                {"name": "Beta", "filename": "beta.jar"},
            ],
        )
        jsonl = ModWebService._render_modlist(
            mods,
            instance_name="Minecraft Alpha",
            pack_version="2026-07-04",
            output_format=ModWebModlistFormat.JSONL,
            include_version=True,
            include_filename=False,
        )
        self.assertEqual(
            [json.loads(line) for line in jsonl.splitlines()],
            [
                {"name": "Alpha | Tools", "version": "1.2.0"},
                {"name": "Beta", "version": None},
            ],
        )
        self.assertEqual(
            ModWebService._render_modlist(
                (
                    self._mod_entry(
                        name="quoted.jar",
                        friendly='Comma, "Quoted"',
                        version="2.0.0",
                    ),
                ),
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.CSV,
                include_version=True,
                include_filename=True,
            ),
            'name,version,filename\n"Comma, ""Quoted""",2.0.0,quoted.jar',
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.MARKDOWN_GFM,
                include_version=True,
                include_filename=False,
            ),
            "# Minecraft Alpha [2026-07-04]\n\n"
            "| Name | Version |\n"
            "| --- | --- |\n"
            "| Alpha \\| Tools | 1.2.0 |\n"
            "| Beta | Unknown |",
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.MARKDOWN_COMMONMARK,
                include_version=True,
                include_filename=True,
            ),
            "# Minecraft Alpha [2026-07-04]\n\n"
            "- Alpha | Tools [1.2.0] (alpha.jar)\n"
            "- Beta [Unknown] (beta.jar)",
        )
        self.assertEqual(
            ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=ModWebModlistFormat.DISCORD,
                include_version=True,
                include_filename=True,
            ),
            "**Minecraft Alpha [2026-07-04]**\n\n"
            "- **Alpha \\| Tools** [`1.2.0`] (`alpha.jar`)\n"
            "- **Beta** [`Unknown`] (`beta.jar`)",
        )
        self.assertEqual(
            [output_format.label for output_format in ModWebModlistFormat],
            [
                "Plaintext",
                "Discord",
                "JSON",
                "JSONL",
                "Markdown [GitHub/GFM]",
                "Markdown [CommonMark]",
                "CSV",
            ],
        )

    def test_render_modlist_filters_categories_and_marks_optional_client_mods(self) -> None:
        regular = self._mod_entry(name="regular.jar", friendly="Regular")
        disabled = self._mod_entry(
            name="disabled.jar",
            friendly="Disabled",
            enabled=False,
            placement=ModPlacement.SERVER_DISABLED,
        )
        builtin = self._mod_entry(
            name="builtin.jar",
            friendly="Built-in",
            mod_type=ModType.BUILTIN,
        )
        optional_client = self._mod_entry(
            name="client.jar",
            friendly="Client Optional",
            enabled=False,
            mod_type=ModType.CLIENT,
            placement=ModPlacement.CLIENT_ONLY,
            client_pack=ClientPackConfig(policy=ClientPackPolicy.OPTIONAL),
        )
        mods = (regular, disabled, builtin, optional_client)

        def render(
            output_format: ModWebModlistFormat,
            *,
            include_disabled: bool = False,
            include_builtin: bool = False,
            include_client: bool = True,
        ) -> str:
            return ModWebService._render_modlist(
                mods,
                instance_name="Minecraft Alpha",
                pack_version="2026-07-04",
                output_format=output_format,
                include_version=False,
                include_filename=False,
                include_disabled=include_disabled,
                include_builtin=include_builtin,
                include_client=include_client,
            )

        self.assertEqual(
            render(ModWebModlistFormat.PLAINTEXT),
            "Minecraft Alpha [2026-07-04]\n\nClient Optional [Optional]\nRegular",
        )
        self.assertEqual(
            render(ModWebModlistFormat.PLAINTEXT, include_client=False),
            "Minecraft Alpha [2026-07-04]\n\nRegular",
        )
        self.assertEqual(
            render(
                ModWebModlistFormat.PLAINTEXT,
                include_disabled=True,
                include_builtin=True,
                include_client=True,
            ),
            "Minecraft Alpha [2026-07-04]\n\n"
            "Built-in\nClient Optional [Optional]\nDisabled\nRegular",
        )
        self.assertEqual(
            json.loads(render(ModWebModlistFormat.JSON)),
            [{"name": "Client Optional", "optional": True}, {"name": "Regular"}],
        )
        self.assertIn("Client Optional [Optional]", render(ModWebModlistFormat.MARKDOWN_GFM))
        self.assertIn("Client Optional [Optional]", render(ModWebModlistFormat.MARKDOWN_COMMONMARK))
        self.assertIn("**Client Optional** [Optional]", render(ModWebModlistFormat.DISCORD))
        self.assertEqual(
            render(ModWebModlistFormat.CSV),
            "name,optional\nClient Optional,True\nRegular,",
        )

    def test_mod_result_count_label_distinguishes_filtered_and_total_counts(self) -> None:
        self.assertEqual(ModWebService._mod_result_count_label(visible_count=7, total_count=7), "7 mods")
        self.assertEqual(ModWebService._mod_result_count_label(visible_count=1, total_count=1), "1 mod")
        self.assertEqual(ModWebService._mod_result_count_label(visible_count=2, total_count=7), "2 of 7 mods")
        with self.assertRaisesRegex(ValueError, "0 <= visible_count <= total_count"):
            ModWebService._mod_result_count_label(visible_count=8, total_count=7)

    def test_mods_card_description_only_renders_for_empty_inventory(self) -> None:
        self.assertEqual(
            ModWebService._mods_card_description(
                NodeModSummary(
                    total_count=0,
                    enabled_count=0,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=0,
                    non_downloadable_count=0,
                )
            ),
            "No mods are currently indexed.",
        )
        self.assertEqual(
            ModWebService._mods_card_description(
                NodeModSummary(
                    total_count=3,
                    enabled_count=3,
                    disabled_count=0,
                    coremod_count=1,
                    downloadable_count=0,
                    non_downloadable_count=3,
                )
            ),
            None,
        )
        self.assertEqual(
            ModWebService._mods_card_description(
                NodeModSummary(
                    total_count=4,
                    enabled_count=3,
                    disabled_count=1,
                    coremod_count=1,
                    downloadable_count=2,
                    non_downloadable_count=2,
                )
            ),
            None,
        )

    def test_mods_header_badges_surface_download_and_block_summary(self) -> None:
        badges = ModWebService._mods_header_badges(
            NodeModSummary(
                total_count=4,
                enabled_count=3,
                disabled_count=1,
                coremod_count=1,
                downloadable_count=2,
                non_downloadable_count=2,
            ),
            client_pack_version="2026-07-04",
        )

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="4 mods", tone="black"),
                _ModWebBadgeSpec(text="pack 2026-07-04", tone="grey"),
                _ModWebBadgeSpec(text="2 blocked", tone="warn"),
                _ModWebBadgeSpec(text="2 downloadable", tone="purple"),
                _ModWebBadgeSpec(text="1 coremod", tone="red"),
            ),
        )

    def test_app_page_hero_mod_badge_uses_compact_enabled_over_total_format(self) -> None:
        badge = ModWebService._app_page_hero_mod_badge(
            NodeModSummary(
                total_count=4,
                enabled_count=3,
                disabled_count=1,
                coremod_count=1,
                downloadable_count=2,
                non_downloadable_count=2,
            )
        )

        self.assertEqual(badge, _ModWebBadgeSpec(text="3/4 Mods", tone="black"))

    def test_app_page_hero_mod_badge_uses_total_only_when_all_mods_are_enabled(self) -> None:
        badge = ModWebService._app_page_hero_mod_badge(
            NodeModSummary(
                total_count=4,
                enabled_count=4,
                disabled_count=0,
                coremod_count=1,
                downloadable_count=2,
                non_downloadable_count=2,
            )
        )

        self.assertEqual(badge, _ModWebBadgeSpec(text="4 Mods", tone="black"))

    def test_download_selection_label_uses_all_for_none_or_full_selection(self) -> None:
        self.assertEqual(
            ModWebService._download_selection_label(selected_count=0, downloadable_count=7),
            "Download All/7",
        )
        self.assertEqual(
            ModWebService._download_selection_label(selected_count=7, downloadable_count=7),
            "Download All/7",
        )
        self.assertEqual(
            ModWebService._download_selection_label(selected_count=3, downloadable_count=7),
            "Download 3/7",
        )

    def test_server_mod_is_selected_for_direct_download_by_default(self) -> None:
        regular = self._mod_entry(name="regular.jar")
        server = self._mod_entry(name="server.jar", mod_type=ModType.SERVER)
        builtin = self._mod_entry(
            name="builtin.jar",
            mod_type=ModType.BUILTIN,
            downloadable=False,
        )

        selected_names = ModWebService._default_mod_download_names(
            (regular, server, builtin),
            ModDistributionMode.MINECRAFT_LAUNCHER_PACK,
        )

        self.assertEqual(selected_names, frozenset({regular.name, server.name}))

    def test_hero_card_style_uses_app_color_when_available(self) -> None:
        self.assertEqual(
            ModWebService._hero_card_style("#22C55E"),
            (
                "--mod-hero-border: #22C55E; "
                "--mod-hero-border-glow: rgba(34, 197, 94, 0.18); "
                "--mod-hero-border-fade: var(--mod-border);"
            ),
        )
        self.assertEqual(ModWebService._hero_card_style(None), "")

    def test_hex_color_to_rgba_rejects_invalid_hex_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "Expected #rrggbb color"):
            ModWebService._hex_color_to_rgba("22C55E", alpha=0.18)

    def test_page_tabs_use_capability_order_for_mod_pages(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=True,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=self._save_list(),
            app_stats=None,
            app_start_blocked=False,
            settings=self._setting_list(),
            console_actions=self._console_action_list(),
            mods=self._mod_list(),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )

        tabs = service._page_tabs(model)

        self.assertEqual(
            [tab.tab_id for tab in tabs],
            ["mods", "configs", "settings", "saves", "console"],
        )
        self.assertEqual(
            [tab.icon for tab in tabs],
            ["extension", "description", "tune", "save", "terminal"],
        )

    def test_page_tabs_include_hidden_minecraft_recipes_when_enabled_kubejs_exists(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=True,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=self._save_list(),
            app_stats=None,
            app_start_blocked=False,
            settings=self._setting_list(),
            console_actions=self._console_action_list(),
            mods=self._mod_list(
                mods=(
                    self._mod_entry(name="kubejs-forge-2001.6.5-build.26.jar"),
                    self._mod_entry(name="kubejs-create-1.0.0.jar"),
                )
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )

        tabs = service._page_tabs(model)
        recipes_tab = next(tab for tab in tabs if tab.tab_id == "recipes")

        self.assertEqual(
            [tab.tab_id for tab in tabs],
            ["mods", "configs", "settings", "saves", "recipes", "console"],
        )
        self.assertFalse(recipes_tab.show_on_app_card)

    def test_page_tabs_omit_minecraft_recipes_without_enabled_kubejs(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            mods=self._mod_list(
                mods=(self._mod_entry(name="kubejs-forge-2001.6.5-build.26.jar", enabled=False),)
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "console"])

    def test_page_tabs_include_hidden_sevendays_sandbox_when_dataset_exists(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="sevendays_alpha",
            app_friendly="7D2D Alpha",
            app_color_hex="#B91C1C",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="sevendays_alpha", app_friendly="7D2D Alpha", node="yuki", configs=()),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="3.0:259",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            mods=self._mod_list(app_name="sevendays_alpha", mods=()),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            sevendays_sandbox_options=ModWebSevenDaysSandboxOptionsSummary(
                data_path=".yukibot/sandbox_options.json",
                file_exists=True,
                options=(
                    ModWebSevenDaysSandboxOptionEntry(
                        section="General",
                        key="BlockDamage",
                        value_index=10,
                        value_label="200%",
                        default_index=7,
                        default_label="100%",
                    ),
                ),
            ),
        )

        tabs = service._page_tabs(model)
        sandbox_tab = next(tab for tab in tabs if tab.tab_id == "sandbox")

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "sandbox", "console"])
        self.assertFalse(sandbox_tab.show_on_app_card)

    def test_page_tabs_include_sevendays_sandbox_for_supported_version_without_dataset(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="sevendays_alpha",
            app_friendly="7D2D Alpha",
            app_color_hex="#B91C1C",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="sevendays_alpha", app_friendly="7D2D Alpha", node="yuki", configs=()),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="3.0:259",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            mods=self._mod_list(app_name="sevendays_alpha", mods=()),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            sevendays_sandbox_options=ModWebSevenDaysSandboxOptionsSummary(
                data_path=".yukibot/sandbox_options.json",
                file_exists=False,
            ),
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "sandbox", "console"])

    def test_page_tabs_include_sevendays_sandbox_when_only_snapshot_version_is_available(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="sevendays_alpha",
            app_friendly="7D2D Alpha",
            app_color_hex="#B91C1C",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="sevendays_alpha", app_friendly="7D2D Alpha", node="yuki", configs=()),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            mods=self._mod_list(app_name="sevendays_alpha", mods=()),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            sevendays_sandbox_options=ModWebSevenDaysSandboxOptionsSummary(
                data_path=".yukibot/sandbox_options.json",
                file_exists=True,
                app_version="3.0:259",
            ),
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "sandbox", "console"])

    def test_page_tabs_include_sevendays_sandbox_when_scope_is_explicit(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="sevendays1",
            app_friendly="7D2D-1",
            app_color_hex="#B91C1C",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="sevendays1", app_friendly="7D2D-1", node="yuki", configs=()),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="3.0.0:259",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            app_scope="sevendays",
            mods=self._mod_list(app_name="sevendays1", mods=()),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            sevendays_sandbox_options=ModWebSevenDaysSandboxOptionsSummary(
                data_path=".yukibot/sandbox_options.json",
                file_exists=True,
                app_version="3.0.0:259",
            ),
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "sandbox", "console"])

    def test_page_tabs_omit_sevendays_sandbox_below_supported_build(self) -> None:
        service = ModWebService()
        model = ModWebPageModel(
            node_name="yuki",
            app_name="sevendays_alpha",
            app_friendly="7D2D Alpha",
            app_color_hex="#B91C1C",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(app_name="sevendays_alpha", app_friendly="7D2D Alpha", node="yuki", configs=()),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="3.0:258",
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            mods=self._mod_list(app_name="sevendays_alpha", mods=()),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
            sevendays_sandbox_options=ModWebSevenDaysSandboxOptionsSummary(
                data_path=".yukibot/sandbox_options.json",
                file_exists=True,
            ),
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "console"])

    def test_sevendays_sandbox_options_markup_groups_options(self) -> None:
        markup = ModWebService._sevendays_sandbox_options_markup(
            ModWebSevenDaysSandboxOptionsSummary(
                data_path=".yukibot/sandbox_options.json",
                file_exists=True,
                options=(
                    ModWebSevenDaysSandboxOptionEntry(
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

        self.assertIn("General", markup)
        self.assertIn("BlockDamage", markup)
        self.assertIn("10/200%", markup)

    def test_minecraft_recipe_summary_reads_persisted_recipe_book(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.save_kubejs_recipe_book(
                MinecraftRecipeBook(
                    mutations=(
                        MinecraftShapelessRecipe(
                            output=MinecraftRecipeItemStack("minecraft:gravel"),
                            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
                            recipe_id="kubejs:flint_to_gravel",
                        ),
                        MinecraftRecipeRemoval(
                            MinecraftRecipeRemovalFilter(recipe_id="minecraft:stick"),
                        ),
                    )
                )
            )

            summary = ModWebService()._minecraft_recipe_summary(cast(Any, app))

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary.data_path, ".yukibot/recipes.json")
            self.assertEqual(summary.script_path, "kubejs/server_scripts/yuki_recipes.js")
            self.assertEqual([entry.kind_label for entry in summary.entries], ["Shapeless", "Remove"])
            self.assertEqual(summary.entries[0].title, "minecraft:gravel")
            self.assertEqual(summary.entries[0].recipe_id, "kubejs:flint_to_gravel")
            self.assertEqual(summary.entries[1].title, "minecraft:stick")

    def test_minecraft_item_registry_summary_reads_persisted_item_registry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            item_registry_path = directory / ".yukibot" / "registries" / "items.json"
            item_registry_path.parent.mkdir(parents=True)
            item_registry_path.write_text(
                json.dumps(
                    MinecraftItemRegistrySnapshot(
                        generated_at_epoch_ms=1234567890,
                        item_ids=("minecraft:dirt", "minecraft:stone"),
                        block_item_ids=("minecraft:stone",),
                        item_types_classified=True,
                    ).to_mapping()
                ),
                encoding="utf-8",
            )

            summary = ModWebService()._minecraft_item_registry_summary(cast(Any, app))

            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary.data_path, ".yukibot/registries/items.json")
            self.assertTrue(summary.file_exists)
            self.assertEqual(summary.generated_at_epoch_ms, 1234567890)
            self.assertEqual(summary.item_ids, ("minecraft:dirt", "minecraft:stone"))
            self.assertEqual(summary.block_item_ids, ("minecraft:stone",))
            self.assertTrue(summary.item_types_classified)

    def test_minecraft_item_browser_filters_by_namespace_and_item_type(self) -> None:
        entries = ModWebService._minecraft_browser_entries(
            ("minecraft:stone", "minecraft:stick", "create:cogwheel"),
            block_item_ids=("minecraft:stone",),
        )

        create_entries = ModWebService._filtered_minecraft_browser_entries(
            entries,
            "",
            namespace="create",
        )
        block_entries = ModWebService._filtered_minecraft_browser_entries(
            entries,
            "",
            item_type=entries[0].item_type,
        )
        item_entries = ModWebService._filtered_minecraft_browser_entries(
            entries,
            "",
            item_type=entries[1].item_type,
        )

        self.assertEqual([entry.item_id for entry in create_entries], ["create:cogwheel"])
        self.assertEqual([entry.item_id for entry in block_entries], ["minecraft:stone"])
        self.assertEqual(
            [entry.item_id for entry in item_entries],
            ["minecraft:stick", "create:cogwheel"],
        )

    def test_minecraft_recipes_body_markup_handles_empty_error_and_entries(self) -> None:
        unavailable_markup = ModWebService._minecraft_recipes_body_markup(None)
        empty_markup = ModWebService._minecraft_recipes_body_markup(
            ModWebMinecraftRecipeBookSummary(
                data_path=".yukibot/recipes.json",
                script_path="kubejs/server_scripts/yuki_recipes.js",
            )
        )
        error_markup = ModWebService._minecraft_recipes_body_markup(
            ModWebMinecraftRecipeBookSummary(
                data_path=".yukibot/recipes.json",
                script_path="kubejs/server_scripts/yuki_recipes.js",
                load_error="<broken>",
            )
        )
        entry_markup = ModWebService._minecraft_recipes_body_markup(
            ModWebMinecraftRecipeBookSummary(
                data_path=".yukibot/recipes.json",
                script_path="kubejs/server_scripts/yuki_recipes.js",
                entries=(
                    ModWebService._minecraft_recipe_entry(
                        MinecraftShapelessRecipe(
                            output=MinecraftRecipeItemStack("minecraft:gravel"),
                            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
                            recipe_id="kubejs:flint_to_gravel",
                        )
                    ),
                ),
            )
        )

        self.assertIn("not available", unavailable_markup)
        self.assertIn("No managed recipes yet", empty_markup)
        self.assertIn("&lt;broken&gt;", error_markup)
        self.assertIn("minecraft:gravel", entry_markup)
        self.assertIn("kubejs:flint_to_gravel", entry_markup)

    def test_minecraft_item_registry_markup_handles_missing_error_and_entries(self) -> None:
        unavailable_markup = ModWebService._minecraft_item_registry_markup(None)
        error_markup = ModWebService._minecraft_item_registry_markup(
            ModWebMinecraftItemRegistrySummary(
                data_path=".yukibot/registries/items.json",
                file_exists=True,
                load_error="<broken>",
            )
        )
        loaded_markup = ModWebService._minecraft_item_registry_markup(
            ModWebMinecraftItemRegistrySummary(
                data_path=".yukibot/registries/items.json",
                file_exists=True,
                generated_at_epoch_ms=1234567890,
                item_ids=("minecraft:dirt", "minecraft:stone"),
            )
        )

        self.assertIn("not available", unavailable_markup)
        self.assertIn("&lt;broken&gt;", error_markup)
        self.assertIn("2 items", loaded_markup)
        self.assertIn("1234567890", loaded_markup)

    def test_minecraft_known_item_ids_ignores_missing_and_error_summaries(self) -> None:
        self.assertEqual(ModWebService._minecraft_known_item_ids(None), ())
        self.assertEqual(
            ModWebService._minecraft_known_item_ids(
                ModWebMinecraftItemRegistrySummary(
                    data_path=".yukibot/registries/items.json",
                    load_error="broken",
                )
            ),
            (),
        )
        self.assertEqual(
            ModWebService._minecraft_known_item_ids(
                ModWebMinecraftItemRegistrySummary(
                    data_path=".yukibot/registries/items.json",
                    file_exists=True,
                    item_ids=("minecraft:dirt", "minecraft:stone"),
                )
            ),
            ("minecraft:dirt", "minecraft:stone"),
        )

    def test_minecraft_item_icon_markup_layers_lazy_image_over_csp_safe_fallback(self) -> None:
        markup = ModWebService._minecraft_item_icon_markup(
            item_icon_api_url="/api/node-proxy/yuki/apps/minecraft_alpha/minecraft/recipes/item-icon",
            item_id="minecraft:dirt",
            alt_text="Dirt",
        )

        self.assertIn("mod-recipe-icon-stack", markup)
        self.assertIn("mod-recipe-icon-fallback", markup)
        self.assertIn('loading="lazy"', markup)
        self.assertNotIn("onerror=", markup)

    def test_minecraft_item_icon_remote_path_encodes_app_and_item_ids(self) -> None:
        path = ModWebService._minecraft_item_icon_remote_path(
            app_name="minecraft alpha",
            item_id="minecraft:dark oak_planks",
        )

        self.assertEqual(
            path,
            "/apps/minecraft%20alpha/minecraft/recipes/item-icon?item_id=minecraft%3Adark+oak_planks",
        )

    def test_minecraft_recipe_editor_supports_cooking_extras(self) -> None:
        editor_state = _MinecraftRecipeEditorState(
            operation=_MinecraftRecipeEditorOperation.ADD,
            kind=MinecraftRecipeKind.SMELTING,
            recipe_id="kubejs:smelt_gravel",
            output_item_id="minecraft:gravel",
            output_count_text="2",
            cooking_input_ingredient=_MinecraftRecipeEditorIngredientState.item("minecraft:cobblestone"),
            cooking_experience_text="0.35",
            cooking_time_ticks_text="160",
            selected_slot=_MinecraftRecipeEditorSelection.cooking_input(),
        )

        mutation = ModWebService._minecraft_recipe_mutation_from_editor(editor_state)

        self.assertIsInstance(mutation, MinecraftCookingRecipe)
        assert isinstance(mutation, MinecraftCookingRecipe)
        self.assertEqual(mutation.experience, 0.35)
        self.assertEqual(mutation.cooking_time_ticks, 160)

    def test_minecraft_recipe_editor_preserves_counts_and_accepts_multi_digit_output_count(self) -> None:
        original_mutation = MinecraftShapelessRecipe(
            output=MinecraftRecipeItemStack("minecraft:gravel", count=12),
            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
            recipe_id="yukibot:yuki/minecraft/gravel",
        )
        editor_state = _MinecraftRecipeEditorState()

        ModWebService._load_minecraft_recipe_editor_state(editor_state, original_mutation, mutation_index=0)
        rebuilt_mutation = ModWebService._minecraft_recipe_mutation_from_editor(editor_state)

        self.assertEqual(editor_state.output_count_text, "12")
        self.assertEqual(rebuilt_mutation.to_mapping(), original_mutation.to_mapping())

    def test_minecraft_recipe_editor_supports_removal_filters_and_loading(self) -> None:
        mutation = MinecraftRecipeRemoval(
            filter=MinecraftRecipeRemovalFilter(
                recipe_id="minecraft:iron_ingot_from_blasting",
                output=MinecraftRecipeIngredient.item("minecraft:iron_ingot"),
                input=MinecraftRecipeIngredient.tag("c:iron_ores"),
                recipe_type=MinecraftRecipeKind.BLASTING,
                mod_id="minecraft",
            )
        )
        editor_state = _MinecraftRecipeEditorState()

        ModWebService._load_minecraft_recipe_editor_state(editor_state, mutation, mutation_index=4)

        self.assertEqual(editor_state.operation, _MinecraftRecipeEditorOperation.REMOVE)
        self.assertEqual(editor_state.editing_recipe_index, 4)
        self.assertEqual(editor_state.removal_recipe_id, "minecraft:iron_ingot_from_blasting")
        self.assertEqual(editor_state.removal_output_filter.editor_text, "minecraft:iron_ingot")
        self.assertEqual(editor_state.removal_input_filter.editor_text, "#c:iron_ores")
        self.assertEqual(editor_state.removal_recipe_type_text, MinecraftRecipeKind.BLASTING.value)
        self.assertEqual(editor_state.removal_mod_id, "minecraft")

        rebuilt_mutation = ModWebService._minecraft_recipe_mutation_from_editor(editor_state)

        self.assertEqual(rebuilt_mutation.to_mapping(), mutation.to_mapping())

    def test_minecraft_recipe_editor_supports_tag_ingredients(self) -> None:
        editor_state = _MinecraftRecipeEditorState(
            operation=_MinecraftRecipeEditorOperation.ADD,
            kind=MinecraftRecipeKind.SHAPED,
            recipe_id="kubejs:tagged_recipe",
            output_item_id="minecraft:stick",
            output_count_text="1",
            shaped_ingredients=[
                _MinecraftRecipeEditorIngredientState.tag("minecraft:planks"),
                _MinecraftRecipeEditorIngredientState.empty(),
                _MinecraftRecipeEditorIngredientState.empty(),
                _MinecraftRecipeEditorIngredientState.empty(),
                _MinecraftRecipeEditorIngredientState.item("minecraft:coal"),
                _MinecraftRecipeEditorIngredientState.empty(),
                _MinecraftRecipeEditorIngredientState.empty(),
                _MinecraftRecipeEditorIngredientState.empty(),
                _MinecraftRecipeEditorIngredientState.empty(),
            ],
            selected_slot=_MinecraftRecipeEditorSelection.shaped(0),
        )

        mutation = ModWebService._minecraft_recipe_mutation_from_editor(editor_state)

        self.assertIsInstance(mutation, MinecraftShapedRecipe)
        assert isinstance(mutation, MinecraftShapedRecipe)
        self.assertEqual(mutation.pattern, ("A ", " B"))
        self.assertEqual(mutation.key["A"].kind.value, "tag")
        self.assertEqual(mutation.key["A"].resource_id, "minecraft:planks")

    def test_minecraft_recipe_drag_payload_can_fill_item_and_tag_slots(self) -> None:
        editor_state = _MinecraftRecipeEditorState(
            selected_slot=_MinecraftRecipeEditorSelection.output(),
        )

        ModWebService._apply_minecraft_recipe_drag_payload_to_selection(
            editor_state,
            selection=_MinecraftRecipeEditorSelection.shapeless(2),
            payload=_MinecraftRecipeDragPayload(
                kind=_MinecraftRecipeEditorIngredientKind.TAG,
                resource_id="c:ingots/iron",
            ),
        )
        ModWebService._apply_minecraft_recipe_drag_payload_to_selection(
            editor_state,
            selection=_MinecraftRecipeEditorSelection.output(),
            payload=_MinecraftRecipeDragPayload(
                kind=_MinecraftRecipeEditorIngredientKind.ITEM,
                resource_id="minecraft:iron_ingot",
            ),
        )

        self.assertEqual(editor_state.shapeless_ingredients[2].editor_text, "#c:ingots/iron")
        self.assertEqual(editor_state.output_item_id, "minecraft:iron_ingot")

    def test_minecraft_recipe_drag_payload_rejects_tag_outputs(self) -> None:
        editor_state = _MinecraftRecipeEditorState()

        with self.assertRaisesRegex(ValueError, "Recipe outputs must be concrete items."):
            ModWebService._apply_minecraft_recipe_drag_payload_to_selection(
                editor_state,
                selection=_MinecraftRecipeEditorSelection.output(),
                payload=_MinecraftRecipeDragPayload(
                    kind=_MinecraftRecipeEditorIngredientKind.TAG,
                    resource_id="c:ingots/iron",
                ),
            )

    def test_remote_minecraft_recipe_summaries_parse_remote_workspace_payload(self) -> None:
        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        workspace_state = NodeMinecraftRecipeWorkspaceState(
            recipe_book=NodeMinecraftRecipeBookState(
                data_path=".yukibot/recipes.json",
                script_path="kubejs/server_scripts/yuki_recipes.js",
                payload=MinecraftRecipeBook(
                    mutations=(
                        MinecraftShapelessRecipe(
                            output=MinecraftRecipeItemStack("minecraft:gravel"),
                            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
                            recipe_id="kubejs:flint_to_gravel",
                        ),
                    )
                ).to_mapping(),
            ),
            item_registry=NodeMinecraftItemRegistryState(
                data_path=".yukibot/registries/items.json",
                file_exists=True,
                payload=MinecraftItemRegistrySnapshot(
                    generated_at_epoch_ms=1234567890,
                    item_ids=("minecraft:dirt", "minecraft:stone"),
                    block_item_ids=("minecraft:stone",),
                    item_types_classified=True,
                ).to_mapping(),
            ),
        )
        service._remote_json_async = AsyncMock(return_value=workspace_state.to_mapping())  # type: ignore[method-assign]

        recipe_summary, item_registry_summary = asyncio.run(
            service._remote_minecraft_recipe_summaries_async(
                node,
                "minecraft_alpha",
                user,
            )
        )

        self.assertEqual(recipe_summary.data_path, ".yukibot/recipes.json")
        self.assertEqual(recipe_summary.script_path, "kubejs/server_scripts/yuki_recipes.js")
        self.assertEqual(recipe_summary.entries[0].recipe_id, "kubejs:flint_to_gravel")
        self.assertEqual(item_registry_summary.data_path, ".yukibot/registries/items.json")
        self.assertTrue(item_registry_summary.file_exists)
        self.assertEqual(item_registry_summary.item_ids, ("minecraft:dirt", "minecraft:stone"))
        self.assertEqual(item_registry_summary.block_item_ids, ("minecraft:stone",))
        self.assertTrue(item_registry_summary.item_types_classified)

    def test_lazy_app_tab_loader_fetches_only_requested_section(self) -> None:
        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        app_entry = NodeAppEntry(
            name="factorio_alpha",
            friendly="Factorio Alpha",
            node="erin",
            running=False,
            enabled=True,
            supports_mods=False,
            supports_configs=True,
        )
        empty_configs = NodeConfigList(
            app_name="factorio_alpha",
            app_friendly="Factorio Alpha",
            node="erin",
            configs=(),
        )
        loaded_configs = replace(
            empty_configs,
            configs=(
                self._config_entry(
                    root_id="server",
                    root_label="Server",
                    relative_path="server.properties",
                ),
            ),
        )
        model_without_tabs = ModWebOverviewPageModel(
            node_name="erin",
            app_name="factorio_alpha",
            app_friendly="Factorio Alpha",
            app_color_hex=None,
            supports_configs=True,
            config_read_level=Power_Level.visitor,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=empty_configs,
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
        )
        model = replace(model_without_tabs, tabs=service._page_tabs(model_without_tabs))

        with (
            patch.object(service, "_user_has_level", return_value=True),
            patch.object(service, "_remote_config_list_async", AsyncMock(return_value=loaded_configs)) as load,
        ):
            result = asyncio.run(
                service._load_remote_app_tab(
                    tab_id="configs",
                    model=model,
                    node=node,
                    app_entry=app_entry,
                    app_name="factorio_alpha",
                    request=cast(Any, SimpleNamespace()),
                    user=user,
                )
            )

        self.assertEqual(result.model.configs, loaded_configs)
        self.assertIsNone(result.chat_surface)
        load.assert_awaited_once_with(node, "factorio_alpha", user)

    def test_append_minecraft_recipe_mutation_posts_to_node_api(self) -> None:
        service = ModWebService()
        node = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        mutation = MinecraftShapelessRecipe(
            output=MinecraftRecipeItemStack("minecraft:gravel"),
            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint"),),
            recipe_id="kubejs:flint_to_gravel",
        )
        payload = NodeMinecraftRecipeMutationResult(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="erin",
            message="Saved Minecraft recipe change for Minecraft Alpha.",
            workspace=NodeMinecraftRecipeWorkspaceState(
                recipe_book=NodeMinecraftRecipeBookState(
                    data_path=".yukibot/recipes.json",
                    script_path="kubejs/server_scripts/yuki_recipes.js",
                    payload=MinecraftRecipeBook(mutations=(mutation,)).to_mapping(),
                ),
                item_registry=NodeMinecraftItemRegistryState(
                    data_path=".yukibot/registries/items.json",
                    file_exists=True,
                    payload=MinecraftItemRegistrySnapshot.empty().to_mapping(),
                ),
            ),
        ).to_mapping()
        acl = Access_Control()
        acl._roles[42] = Power_Level.sudo
        service.set_acl(acl)
        service._remote_node_link = Mock(return_value=node)  # type: ignore[method-assign]
        service._remote_json_async = AsyncMock(return_value=payload)  # type: ignore[method-assign]

        result = asyncio.run(
            service._append_minecraft_recipe_mutation(
                model=cast(
                    ModWebPageModel,
                    cast(
                        object,
                        SimpleNamespace(
                            node_name="erin",
                            app_name="minecraft_alpha",
                        ),
                    ),
                ),
                mutation=mutation,
                user=user,
            )
        )

        self.assertEqual(result.message, "Saved Minecraft recipe change for Minecraft Alpha.")
        service._remote_json_async.assert_awaited_once()  # type: ignore[attr-defined]
        call_kwargs = service._remote_json_async.call_args.kwargs  # type: ignore[attr-defined]
        self.assertEqual(call_kwargs["node"], node)
        self.assertEqual(call_kwargs["app_name"], "minecraft_alpha")
        self.assertEqual(call_kwargs["path"], "/apps/minecraft_alpha/minecraft/recipes/mutations")
        self.assertEqual(call_kwargs["scopes"], (NodeApiScope.APP_MANAGE,))
        self.assertEqual(call_kwargs["user"], user)
        self.assertEqual(call_kwargs["method"], "POST")
        self.assertEqual(
            call_kwargs["json_payload"],
            {"action": NodeMinecraftRecipeMutationAction.ADD.value, "mutation": mutation.to_mapping()},
        )

    def test_render_node_mods_page_passes_minecraft_recipe_summaries_to_remote_page_model(self) -> None:
        async def exercise() -> None:
            service = ModWebService()
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
            app_entry = NodeAppEntry(
                name="minecraft_alpha",
                friendly="Minecraft Alpha",
                node="erin",
                running=False,
                enabled=True,
                supports_mods=True,
                supports_configs=False,
                scope="minecraft",
            )
            mods = self._mod_list(
                app_name="minecraft_alpha",
                mods=(self._mod_entry(name="kubejs-forge-2001.6.5-build.26.jar"),),
            )
            recipe_summary = ModWebMinecraftRecipeBookSummary(
                data_path=".yukibot/recipes.json",
                script_path="kubejs/server_scripts/yuki_recipes.js",
            )
            item_registry_summary = ModWebMinecraftItemRegistrySummary(
                data_path=".yukibot/registries/items.json",
                file_exists=True,
                item_ids=("minecraft:stone",),
            )
            render_page = Mock()

            service._authorised_page_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
            service._remote_node_link = Mock(return_value=node)  # type: ignore[method-assign]
            service._remote_app_entry_async = AsyncMock(return_value=app_entry)  # type: ignore[method-assign]
            service._remote_mod_list_async = AsyncMock(return_value=mods)  # type: ignore[method-assign]
            service._remote_node_system_summary_or_none_async = AsyncMock(return_value=None)  # type: ignore[method-assign]
            service._remote_minecraft_recipe_summaries_async = AsyncMock(  # type: ignore[method-assign]
                return_value=(recipe_summary, item_registry_summary)
            )
            service._render_page = render_page  # type: ignore[method-assign]
            service._request_path = Mock(return_value="/mod-web/nodes/erin/mods/minecraft_alpha")  # type: ignore[method-assign]

            await service._render_node_mods_page(
                ui=cast(ModWebUi, cast(object, SimpleNamespace())),
                node_name="erin",
                app_name="minecraft_alpha",
                request=cast(Any, SimpleNamespace(query_params={"tab": "recipes"})),
            )

            model = cast(ModWebPageModel, render_page.call_args.kwargs["model"])
            self.assertEqual(model.minecraft_recipes, recipe_summary)
            self.assertEqual(model.minecraft_item_registry, item_registry_summary)

        asyncio.run(exercise())

    def test_render_node_mods_page_only_loads_requested_config_section(self) -> None:
        async def exercise() -> None:
            service = ModWebService()
            node = ModWebNodeLink(
                node_name="erin",
                label="Erin",
                url="/mod-web/nodes/erin",
                api_base_url="https://erin.example/api/node",
                api_url="/api/node-proxy/erin/apps",
                is_current=False,
            )
            user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
            app_entry = NodeAppEntry(
                name="factorio_alpha",
                friendly="Factorio Alpha",
                node="erin",
                running=True,
                enabled=True,
                supports_mods=True,
                supports_configs=True,
                scope="factorio",
                config_read_level=Power_Level.visitor,
            )
            configs = NodeConfigList(
                app_name="factorio_alpha",
                app_friendly="Factorio Alpha",
                node="erin",
                configs=(),
            )
            app_stats = NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version="2.0.0",
                player_count=0,
                player_capacity=8,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                footprint_bytes=None,
            )
            render_page = Mock()
            remote_mod_list = AsyncMock(side_effect=AssertionError("Mods should be deferred"))
            remote_config_list = AsyncMock(return_value=configs)
            with (
                patch.object(service, "_authorised_page_user", new=AsyncMock(return_value=user)),
                patch.object(service, "_user_has_level", return_value=True),
                patch.object(service, "_remote_node_link", return_value=node),
                patch.object(service, "_remote_app_entry_async", new=AsyncMock(return_value=app_entry)),
                patch.object(service, "_remote_mod_list_async", new=remote_mod_list),
                patch.object(service, "_remote_config_list_async", new=remote_config_list),
                patch.object(
                    service,
                    "_remote_app_runtime_summary_async",
                    new=AsyncMock(return_value=app_stats),
                ),
                patch.object(
                    service,
                    "_remote_node_system_summary_or_none_async",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(service, "_render_page", new=render_page),
                patch.object(
                    service,
                    "_request_path",
                    return_value="/mod-web/nodes/erin/mods/factorio_alpha?tab=configs",
                ),
            ):
                await service._render_node_mods_page(
                    ui=cast(ModWebUi, cast(object, SimpleNamespace())),
                    node_name="erin",
                    app_name="factorio_alpha",
                    request=cast(Any, SimpleNamespace(query_params={"tab": "configs"})),
                )

            remote_mod_list.assert_not_awaited()
            remote_config_list.assert_awaited_once_with(node, "factorio_alpha", user)
            model = cast(ModWebPageModel, render_page.call_args.kwargs["model"])
            self.assertEqual(model.configs, configs)
            self.assertEqual(model.mods.mods, ())
            self.assertEqual(model.app_stats, app_stats)

        asyncio.run(exercise())

    def test_minecraft_recipe_form_builds_shapeless_mutation(self) -> None:
        mutation = ModWebService._minecraft_recipe_mutation_from_form(
            kind_value="shapeless",
            recipe_id="kubejs:flint_to_gravel",
            output_item="minecraft:gravel",
            output_count="1",
            ingredients_text="3x minecraft:flint",
            pattern_text="",
            key_text="",
        )

        self.assertIsInstance(mutation, MinecraftShapelessRecipe)
        assert isinstance(mutation, MinecraftShapelessRecipe)
        self.assertEqual(mutation.output.kubejs_value, "minecraft:gravel")
        self.assertEqual(tuple(ingredient.kubejs_value for ingredient in mutation.ingredients), ("3x minecraft:flint",))
        self.assertEqual(mutation.recipe_id, "kubejs:flint_to_gravel")

    def test_minecraft_recipe_form_builds_shaped_mutation(self) -> None:
        mutation = ModWebService._minecraft_recipe_mutation_from_form(
            kind_value="shaped",
            recipe_id="",
            output_item="minecraft:blast_furnace",
            output_count="1",
            ingredients_text="",
            pattern_text="III\nIFI\nSSS",
            key_text="I=minecraft:iron_ingot\nF=minecraft:furnace\nS=minecraft:smooth_stone",
        )

        self.assertEqual(mutation.render_kubejs(), (
            'event.shaped("minecraft:blast_furnace", ["III", "IFI", "SSS"], '
            '{"F": "minecraft:furnace", "I": "minecraft:iron_ingot", "S": "minecraft:smooth_stone"})'
        ))

    def test_minecraft_recipe_form_rejects_invalid_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integers"):
            ModWebService._minecraft_recipe_mutation_from_form(
                kind_value="shapeless",
                recipe_id="",
                output_item="minecraft:gravel",
                output_count="one",
                ingredients_text="minecraft:flint",
                pattern_text="",
                key_text="",
            )

    def test_render_minecraft_recipes_section_is_read_only_for_remote_user(self) -> None:
        class FakeContextElement:
            def __enter__(self) -> "FakeContextElement":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            def classes(self, value: str) -> "FakeContextElement":
                del value
                return self

        class FakeHtmlElement:
            def classes(self, value: str) -> "FakeHtmlElement":
                del value
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.html_fragments: list[str] = []

            def html(self, content: str) -> FakeHtmlElement:
                self.html_fragments.append(content)
                return FakeHtmlElement()

            def tabs(self, *args: object, **kwargs: object) -> FakeContextElement:
                del args, kwargs
                return FakeContextElement()

            def tab(self, *args: object, **kwargs: object) -> FakeContextElement:
                del args, kwargs
                return FakeContextElement()

            def tab_panels(self, *args: object, **kwargs: object) -> FakeContextElement:
                del args, kwargs
                return FakeContextElement()

            def tab_panel(self, *args: object, **kwargs: object) -> FakeContextElement:
                del args, kwargs
                return FakeContextElement()

            def element(self, *args: object, **kwargs: object) -> FakeContextElement:
                del args, kwargs
                return FakeContextElement()

        service = ModWebService()
        ui = FakeUi()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        model = ModWebPageModel(
            node_name="erin",
            app_name="minecraft_all_fabric",
            app_friendly="Minecraft All Fabric",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_all_fabric",
                app_friendly="Minecraft All Fabric",
                node="erin",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            mods=self._mod_list(
                app_name="minecraft_all_fabric",
                mods=(self._mod_entry(name="kubejs-fabric-2001.6.5-build.26.jar"),),
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )
        tab = ModWebAppTabDefinition.custom(
            tab_id="recipes",
            label="Recipes",
            page_order=425,
            app_card_order=675,
            app_card_tone="black",
            render_handler_name="_render_minecraft_recipes_section",
        )

        service._render_minecraft_recipes_section(ui=cast(ModWebUi, cast(object, ui)), model=model, user=user, tab=tab)

        markup = "\n".join(ui.html_fragments)
        self.assertIn("Recipe book data is not available for this node yet.", markup)
        self.assertIn("read-only for non-sudo accounts", markup)

    def test_page_tabs_include_map_when_same_origin_map_api_is_available(self) -> None:
        service = ModWebService()
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(),
            map_url="https://example.invalid/squaremap/?world=minecraft_overworld",
            map_api_url="/api/node/apps/minecraft_alpha/map",
            can_write_map_annotations=True,
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["map", "console"])

    def test_map_client_assets_are_vendored_locally(self) -> None:
        assets_html = ModWebService._map_client_assets_html()
        stylesheet = ModWebService._map_client_stylesheet()
        script = ModWebService._map_client_script()

        self.assertNotIn("https://unpkg.com", assets_html)
        self.assertIn("Leaflet", assets_html)
        self.assertIn(".leaflet-container", stylesheet)
        self.assertIn("window.modWebMap", script)
        self.assertNotIn("<style>", stylesheet)
        self.assertNotIn("<script>", script)

    def test_render_map_section_places_controls_inside_shared_tab_toolbar(self) -> None:
        class FakeHtmlElement:
            def __init__(self) -> None:
                self.class_names: list[str] = []

            def classes(self, value: str) -> "FakeHtmlElement":
                self.class_names.append(value)
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.head_html: list[str] = []
                self.html_fragments: list[str] = []
                self.javascript_calls: list[tuple[str, float | None]] = []
                self.html_elements: list[FakeHtmlElement] = []

            def add_head_html(self, content: str) -> None:
                self.head_html.append(content)

            def html(self, content: str) -> FakeHtmlElement:
                self.html_fragments.append(content)
                element = FakeHtmlElement()
                self.html_elements.append(element)
                return element

            def run_javascript(self, script: str, *, timeout: float | None = None) -> None:
                self.javascript_calls.append((script, timeout))

        service = ModWebService()
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            map_url="https://example.invalid/squaremap/?world=minecraft_overworld",
            map_api_url="/api/node/apps/minecraft_alpha/map",
            can_write_map_annotations=True,
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        tab = ModWebAppTabDefinition.custom(
            tab_id="map",
            label="Map",
            page_order=350,
            app_card_order=150,
            app_card_tone="purple",
            render_handler_name="_render_map_section",
        )
        ui = FakeUi()

        service._render_map_section(ui=cast(ModWebUi, cast(object, ui)), model=model, user=user, tab=tab)

        self.assertEqual(len(ui.html_fragments), 1)
        self.assertEqual(len(ui.html_elements), 1)
        self.assertEqual(ui.html_elements[0].class_names, ["w-full"])
        self.assertEqual(len(ui.head_html), 1)
        self.assertIn('/mod-web/assets/map.css?v=', ui.head_html[0])
        self.assertIn('/mod-web/assets/map.js?v=', ui.head_html[0])
        self.assertNotIn("Leaflet", ui.head_html[0])
        markup = ui.html_fragments[0]
        self.assertIn("mod-tab-toolbar mod-tab-toolbar-surface mod-map-toolbar", markup)
        self.assertIn('class="mod-map-toolbar-main"', markup)
        self.assertIn('class="mod-map-mode mod-subtitle">Loading map…</div>', markup)
        self.assertIn('class="mod-map-status mod-subtitle">Loading map data…</div>', markup)
        self.assertIn("mod-tab-toolbar-actions mod-map-toolbar-actions", markup)
        self.assertNotIn("mod-map-toolset", markup)
        self.assertIn('class="mod-map-toolbar-group mod-map-toolbar-group-dimension"', markup)
        self.assertIn('class="mod-map-toolbar-group mod-map-toolbar-group-tools"', markup)
        self.assertIn('class="mod-map-toolbar-pair mod-map-toolbar-pair-dimension"', markup)
        self.assertIn('class="mod-map-toolbar-pair mod-map-toolbar-pair-tools"', markup)
        self.assertIn("mod-toolbar-button mod-map-button", markup)
        self.assertIn('class="mod-map-label-prompt"', markup)
        self.assertIn('class="mod-map-canvas-frame"', markup)
        self.assertIn('aria-label="Annotation label"', markup)
        self.assertNotIn(">Cancel</button>", markup)
        self.assertNotIn(">Pan</button>", markup)
        self.assertNotIn(">Finish</button>", markup)
        self.assertNotIn('id="mod-map-yuki-minecraft-alpha-finish"', markup)
        self.assertNotIn('id="mod-map-yuki-minecraft-alpha-cancel"', markup)
        self.assertIn('aria-label="Dimension"', markup)

    def test_map_client_refresh_keeps_existing_layers_visible_until_replacements_are_ready(self) -> None:
        assets_html = ModWebService._map_client_assets_html()

        self.assertIn('replaceLayerGroup(state, "annotationLayer", annotationResult.value);', assets_html)
        self.assertIn("nextTileLayer.setOpacity(state.tileLayer ? 0 : 1);", assets_html)
        self.assertIn('nextTileLayer.once("load", finish);', assets_html)
        self.assertIn("const requestAnnotationLabel = async (state, labelKind, anchorPoint = null) => {", assets_html)
        self.assertNotIn("window.prompt(", assets_html)
        self.assertIn("state.labelPrompt.hidden = false;", assets_html)
        self.assertIn("background: rgba(0, 0, 0, 0.96);", assets_html)
        self.assertIn("placeLabelPrompt(state, anchorPoint);", assets_html)
        self.assertIn("const placeLabelPrompt = (state, anchorPoint = null) => {", assets_html)
        self.assertIn("transform: translate(-50%, calc(-100% - 0.85rem));", assets_html)
        self.assertIn("width: 100%;", assets_html)
        self.assertIn("const resolveLabelPrompt = (state, value, { focusMap = true } = {}) => {", assets_html)
        self.assertIn("const cancelPendingAnnotation = (state, { focusMap = true } = {}) => {", assets_html)
        self.assertIn('document.addEventListener("pointerdown", state.documentPointerDownListener, true);', assets_html)
        self.assertIn('document.addEventListener("contextmenu", state.documentContextMenuListener, true);', assets_html)
        self.assertIn('cancelPendingAnnotation(state, { focusMap: false });', assets_html)
        self.assertIn('state.labelPromptInput.placeholder = `Enter a label for this ${labelKind}`;', assets_html)
        self.assertIn('void createMarkerAnnotation(state, rawPoint, event.containerPoint).catch((error) =>', assets_html)
        self.assertIn('void finishLineAnnotation(state, event.containerPoint).catch((error) =>', assets_html)
        self.assertNotIn("state.labelPromptCancel?.addEventListener(", assets_html)
        self.assertIn('if (state.tool === "marker") {', assets_html)
        self.assertIn("doubleClickZoom: false,", assets_html)
        self.assertIn("if (event.originalEvent?.detail && event.originalEvent.detail > 1) {", assets_html)
        self.assertIn('state.map.on("dblclick", (event) => {', assets_html)
        self.assertIn('state.map.on("contextmenu", (event) => {', assets_html)
        self.assertIn('setToolState(state, "pan");', assets_html)
        self.assertIn('toggle.disabled = !enabled;', assets_html)
        self.assertIn('toggleLabel.dataset.disabled = enabled ? "false" : "true";', assets_html)
        self.assertIn(".mod-map-toggle[data-disabled=\"true\"]", assets_html)
        self.assertIn("width: 100%;", assets_html)
        self.assertIn("gap: 0.55rem;", assets_html)
        self.assertIn("justify-content: space-between;", assets_html)
        self.assertIn("text-align: right;", assets_html)
        self.assertIn("justify-content: center;", assets_html)
        self.assertIn("aspect-ratio: 1 / 1;", assets_html)
        self.assertIn("border-radius: 0;", assets_html)
        self.assertIn('const defaultWorldName = state.worldByName.has("minecraft_overworld")', assets_html)
        self.assertIn('typeof layer.bringToFront === "function"', assets_html)
        self.assertIn('void fetch(state.config.clientErrorUrl, {', assets_html)
        self.assertIn('console.error("[mod-map]", context, error);', assets_html)
        self.assertIn("const apiSubpathUrl = (state, baseSuffix, relativePath) => {", assets_html)
        self.assertIn('iconUrl: apiSubpathUrl(', assets_html)
        self.assertIn('if (markersLoaded && markersLoaded.source === MAP_SOURCE_STALE) {', assets_html)
        self.assertIn("if (forceTiles) {", assets_html)
        self.assertIn("await refreshTiles(state, { force: true });", assets_html)
        self.assertNotIn("await refreshTiles(state, { force: forceTiles });", assets_html)
        self.assertIn("const isOfflineError = isSquaremapOfflineError(detail);", assets_html)
        self.assertIn('setStatus(state, "Map data is unavailable.", "error");', assets_html)

    def test_app_link_tabs_include_map_when_public_map_exists(self) -> None:
        service = ModWebService()
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#22C55E",
            supports_mods=False,
            supports_configs=False,
            supports_saves=False,
            supports_settings=False,
            url="/mod-web/apps/minecraft_alpha",
            api_url=None,
            configs_api_url=None,
            map_url="https://example.invalid/squaremap/?world=minecraft_overworld",
        )

        tabs = service._app_link_tabs(app)

        self.assertEqual([tab.tab_id for tab in tabs], ["map"])

    def test_page_tabs_include_blueprints_for_user_visible_satisfactory_pages(self) -> None:
        service = ModWebService()
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="satisfactory_alpha",
            app_friendly="Satisfactory Alpha",
            app_color_hex="#F59E0B",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="satisfactory_alpha",
                app_friendly="Satisfactory Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=self._console_action_list(app_name="satisfactory_alpha"),
            blueprints=self._blueprint_list(app_name="satisfactory_alpha"),
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["blueprints", "console"])

    def test_page_tabs_omit_mods_for_overview_pages(self) -> None:
        service = ModWebService()
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="factorio",
            app_friendly="Factorio",
            app_color_hex="#DC2626",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.admin,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="factorio",
                app_friendly="Factorio",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=self._setting_list(app_name="factorio"),
            console_actions=self._console_action_list(app_name="factorio"),
        )

        tabs = service._page_tabs(model)

        self.assertEqual(
            [tab.tab_id for tab in tabs],
            ["configs", "settings", "console"],
        )

    def test_page_tabs_include_chat_when_supported(self) -> None:
        service = ModWebService()
        model = ModWebOverviewPageModel(
            node_name="yuki",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="yuki",
                configs=(),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            console_actions=None,
            supports_chat=True,
            chat_url="/mod-web/chat/minecraft_alpha",
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["chat"])

    def test_initial_page_tab_id_uses_requested_available_tab(self) -> None:
        tab_id = ModWebService._initial_page_tab_id(
            current_url="/mod-web/mods/minecraft_alpha?tab=saves",
            tabs=(
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.MODS),
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.CONFIGS),
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.SAVES),
            ),
        )

        self.assertEqual(tab_id, "saves")

    def test_initial_page_tab_id_falls_back_to_first_section(self) -> None:
        unavailable_tab = ModWebService._initial_page_tab_id(
            current_url="/mod-web/mods/factorio?tab=mods",
            tabs=(
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.CONFIGS),
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.SETTINGS),
            ),
        )
        invalid_tab = ModWebService._initial_page_tab_id(
            current_url="/mod-web/mods/factorio?tab=unknown",
            tabs=(
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.CONFIGS),
                ModWebService._builtin_tab_definition(ModWebAppSectionKind.SETTINGS),
            ),
        )

        self.assertEqual(unavailable_tab, "configs")
        self.assertEqual(invalid_tab, "configs")

    def test_precomputed_tabs_reject_case_insensitive_duplicate_ids(self) -> None:
        service = ModWebService()
        tabs = (
            ModWebAppTabDefinition.custom(
                tab_id="Map",
                label="Map",
                page_order=100,
                app_card_order=100,
                app_card_tone="purple",
                render_handler_name="_render_map_tab",
            ),
            ModWebAppTabDefinition.custom(
                tab_id=" map ",
                label="Map Overview",
                page_order=200,
                app_card_order=200,
                app_card_tone="black",
                render_handler_name="_render_map_overview_tab",
            ),
        )
        model = replace(self._overview_model_with_config_and_chat(), tabs=tabs)

        with self.assertRaisesRegex(ValueError, "Duplicate app tab id: map"):
            service._page_tabs(model)

    def test_additional_app_tabs_render_custom_handlers_on_detail_pages(self) -> None:
        class HiddenTabService(ModWebService):
            def __init__(self) -> None:
                super().__init__()
                self.rendered_tab_ids: list[str] = []

            def _additional_app_tab_definitions(
                self,
                *,
                context: ModWebAppTabContext,
                is_detail_page: bool,
            ) -> tuple[ModWebAppTabDefinition, ...]:
                if context.app_name != "minecraft_alpha" or not is_detail_page:
                    return ()
                return (
                    ModWebAppTabDefinition.custom(
                        tab_id="map",
                        label="Map",
                        page_order=650,
                        app_card_order=650,
                        app_card_tone="purple",
                        render_handler_name="_render_map_tab",
                        badge_handler_name="_map_tab_badges",
                        action_handler_name="_map_tab_actions",
                    ),
                )

            def _render_map_tab(
                self,
                *,
                ui: ModWebUi,
                model: ModWebBasePageModel,
                user: ModWebUser,
                tab: ModWebAppTabDefinition,
            ) -> None:
                del ui, model, user
                self.rendered_tab_ids.append(tab.tab_id)
                return None

            @staticmethod
            def _map_tab_badges(
                *,
                model: ModWebBasePageModel,
                user: ModWebUser,
                tab: ModWebAppTabDefinition,
            ) -> tuple[_ModWebBadgeSpec, ...]:
                del model, user, tab
                return (_ModWebBadgeSpec(text="Squaremap", tone="purple"),)

            @staticmethod
            def _map_tab_actions(
                *,
                model: ModWebBasePageModel,
                user: ModWebUser,
                tab: ModWebAppTabDefinition,
            ) -> tuple[_ModWebTabActionSpec, ...]:
                del model, user, tab
                return (_ModWebTabActionSpec(label="Open Map", url="/maps/minecraft_alpha", new_tab=True),)

        service = HiddenTabService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        model = self._overview_model_with_config_and_chat()

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["configs", "chat", "map"])
        map_tab = tabs[-1]
        self.assertEqual(
            service._page_section_badges(model=model, user=user, tab=map_tab),
            (_ModWebBadgeSpec(text="Squaremap", tone="purple"),),
        )
        self.assertEqual(
            service._page_tab_actions(model=model, user=user, tab=map_tab, chat_surface=None),
            (_ModWebTabActionSpec(label="Open Map", url="/maps/minecraft_alpha", new_tab=True),),
        )
        self.assertIsNone(
            service._render_page_section(
                ui=cast(ModWebUi, cast(object, SimpleNamespace())),
                model=model,
                user=user,
                tab=map_tab,
                chat_surface=None,
            )
        )
        self.assertEqual(service.rendered_tab_ids, ["map"])

    def test_render_tabbed_page_sections_includes_chat_map_badge_link(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        model = self._overview_model_with_config_and_chat()
        chat_surface = self._chat_surface_with_map()
        ui = _FakeTabbedSectionUi()
        tabs = service._page_tabs(model)
        load_tab = AsyncMock(return_value=ModWebAppTabLoadResult(model=model))

        with (
            patch.object(
                ModWebService,
                "_render_chat_endpoint_badge",
                return_value=(cast(Any, object()), cast(Any, object()), cast(Any, object())),
            ) as render_chat_endpoint_badge,
            patch.object(ModWebService, "_badge_link") as render_badge_link,
            patch.object(ModWebService, "_action_link") as render_action_link,
            patch.object(ModWebService, "_render_page_section", return_value=None) as render_page_section,
        ):
            result = service._render_tabbed_page_sections(
                ui=cast(ModWebUi, cast(object, ui)),
                model=model,
                user=user,
                current_url="/mod-web/apps/minecraft_alpha?tab=chat",
                tabs=tabs,
                chat_surface=chat_surface,
                load_tab=load_tab,
            )

            self.assertIsNotNone(ui.tab_change_handler)
            assert ui.tab_change_handler is not None
            asyncio.run(cast(Any, ui.tab_change_handler)(SimpleNamespace(value="configs")))

        self.assertIsNone(result)
        render_chat_endpoint_badge.assert_called_once()
        render_badge_link.assert_called_once_with(
            ui=ui,
            text="Map",
            tone="purple",
            url="https://example.invalid/squaremap/?world=minecraft_overworld",
            new_tab=True,
        )
        render_action_link.assert_called_once()
        self.assertEqual(render_page_section.call_count, 2)
        load_tab.assert_awaited_once_with("configs")
        ui.navigate.to.assert_not_called()
        self.assertTrue(any("history.replaceState" in script for script in ui.javascript_calls))

    def test_render_tabbed_page_sections_skips_failed_lazy_tab_fallback_after_client_delete(self) -> None:
        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        model = self._overview_model_with_config_and_chat()
        chat_surface = self._chat_surface_with_map()
        client = _FakeCleanupClient()
        ui = _FakeTabbedSectionUi(client=client)
        tabs = service._page_tabs(model)

        async def load_deleted_tab(tab_id: str) -> ModWebAppTabLoadResult:
            del tab_id
            client._deleted = True
            raise RuntimeError("client closed")

        load_tab = AsyncMock(side_effect=load_deleted_tab)

        with (
            patch.object(
                ModWebService,
                "_render_chat_endpoint_badge",
                return_value=(cast(Any, object()), cast(Any, object()), cast(Any, object())),
            ),
            patch.object(ModWebService, "_badge_link"),
            patch.object(ModWebService, "_action_link"),
            patch.object(ModWebService, "_render_page_section", return_value=None),
            patch.object(ModWebService, "_render_flat_tab_empty_state") as render_empty_state,
        ):
            service._render_tabbed_page_sections(
                ui=cast(ModWebUi, cast(object, ui)),
                model=model,
                user=user,
                current_url="/mod-web/apps/minecraft_alpha?tab=chat",
                tabs=tabs,
                chat_surface=chat_surface,
                load_tab=load_tab,
            )

            self.assertIsNotNone(ui.tab_change_handler)
            assert ui.tab_change_handler is not None
            asyncio.run(cast(Any, ui.tab_change_handler)(SimpleNamespace(value="configs")))

        load_tab.assert_awaited_once_with("configs")
        render_empty_state.assert_not_called()

    def test_save_card_description_prefers_supported_actions(self) -> None:
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=True,
            supports_save_rename=False,
            save_write_level=Power_Level.sudo,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
        )

        self.assertEqual(
            ModWebService._save_card_description(model=model, save_count=2),
            "Download the current save or upload a replacement.",
        )
        self.assertEqual(
            ModWebService._save_card_description(model=model, save_count=0),
            "No saves are currently available. Upload one to seed this app.",
        )

    def test_console_card_description_uses_action_count(self) -> None:
        self.assertEqual(
            ModWebService._console_card_description(action_count=1),
            "Run the single curated console action exposed by this app.",
        )
        self.assertEqual(
            ModWebService._console_card_description(action_count=4),
            "Run any of the 4 curated console actions exposed by this app.",
        )

    def test_console_action_count_badge_text_pluralizes(self) -> None:
        self.assertEqual(ModWebService._console_action_count_badge_text(action_count=1), "1 console action")
        self.assertEqual(ModWebService._console_action_count_badge_text(action_count=3), "3 console actions")

    def test_console_action_input_props_support_multiline_and_numeric_values(self) -> None:
        multiline_parameter = NodeConsoleActionParameter(
            key="message",
            label="Message",
            value_type_name="str",
            description="Broadcast message",
            max_length=200,
            multiline=True,
            strict_choice=False,
            allows_text_input=True,
            choices=(),
            recent_inputs=(),
        )
        numeric_parameter = NodeConsoleActionParameter(
            key="count",
            label="Count",
            value_type_name="int",
            description=None,
            max_length=10,
            multiline=False,
            strict_choice=False,
            allows_text_input=True,
            choices=(),
            recent_inputs=(),
        )

        self.assertIn("type=textarea", ModWebService._console_action_input_props(multiline_parameter))
        self.assertIn("type=number", ModWebService._console_action_input_props(numeric_parameter))

    def test_console_action_runtime_helpers_reflect_runtime_availability(self) -> None:
        action = NodeConsoleActionEntry(
            key="save_all",
            label="Save All",
            description="Flush world state to disk.",
            power_level_name=Power_Level.user.name,
            power_level_label=Power_Level.user.name.title(),
            requires_running=True,
            can_run=True,
            parameter=None,
        )
        stopped_stats = NodeAppRuntimeSummary(
            running=False,
            enabled=True,
            version=None,
            player_count=None,
            player_capacity=None,
            relay_support=ChatRelaySupport.NONE,
            storage_percent=None,
            storage_free_bytes=None,
            storage_total_bytes=None,
        )
        starting_stats = replace(stopped_stats, transition_state=NodeAppTransitionState.STARTING)
        disabled_stats = replace(stopped_stats, enabled=False)
        offline_safe_action = replace(action, requires_running=False)

        self.assertEqual(
            ModWebService._console_action_runtime_badge(action=action, app_stats=stopped_stats),
            _ModWebBadgeSpec(text="Stopped", tone="warn"),
        )
        self.assertFalse(ModWebService._console_action_can_execute(action=action, app_stats=stopped_stats))
        self.assertEqual(
            ModWebService._console_action_status_text(
                action=action,
                app_friendly="Minecraft Alpha",
                app_stats=stopped_stats,
            ),
            "Minecraft Alpha must be running before this action can be used.",
        )
        live_action = replace(action, runtime_running=True)
        self.assertEqual(
            ModWebService._console_action_runtime_badge(action=live_action, app_stats=stopped_stats),
            _ModWebBadgeSpec(text="Running", tone="grey"),
        )
        self.assertTrue(ModWebService._console_action_can_execute(action=live_action, app_stats=stopped_stats))
        self.assertEqual(
            ModWebService._console_action_status_text(
                action=live_action,
                app_friendly="Minecraft Alpha",
                app_stats=stopped_stats,
            ),
            "Ready.",
        )
        self.assertEqual(
            ModWebService._console_action_runtime_badge(action=action, app_stats=starting_stats),
            _ModWebBadgeSpec(text="Starting", tone="purple"),
        )
        self.assertFalse(ModWebService._console_action_can_execute(action=action, app_stats=starting_stats))
        crashed_stats = replace(
            stopped_stats,
            runtime_fault=AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH,
                summary="Failed to start the minecraft server",
            ),
        )
        self.assertEqual(
            ModWebService._console_action_runtime_badge(action=action, app_stats=crashed_stats),
            _ModWebBadgeSpec(text="Crashed", tone="red"),
        )
        self.assertEqual(
            ModWebService._console_action_status_text(
                action=action,
                app_friendly="Minecraft Alpha",
                app_stats=crashed_stats,
            ),
            "Minecraft Alpha crashed. Restart it before using this action.",
        )
        self.assertEqual(
            ModWebService._console_action_runtime_badge(action=action, app_stats=disabled_stats),
            _ModWebBadgeSpec(text="Disabled", tone="red"),
        )
        self.assertIsNone(
            ModWebService._console_action_runtime_badge(action=offline_safe_action, app_stats=stopped_stats)
        )
        self.assertTrue(
            ModWebService._console_action_can_execute(action=offline_safe_action, app_stats=stopped_stats)
        )

    def test_app_card_runtime_badge_uses_crash_state(self) -> None:
        app = ModWebAppLink(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            node_name="yuki",
            running=False,
            enabled=True,
            color_hex="#336699",
            supports_mods=True,
            supports_configs=True,
            supports_saves=True,
            supports_settings=True,
            url="/mod-web/apps/minecraft_alpha",
            api_url="/api/node/apps/minecraft_alpha/mods",
            configs_api_url="/api/node/apps/minecraft_alpha/configs",
            runtime_fault=AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH,
                summary="Failed to start the minecraft server",
            ),
        )

        self.assertEqual(ModWebService._app_card_runtime_badge(app), _ModWebBadgeSpec(text="Crashed", tone="red"))

    def test_console_action_result_for_selection_hides_other_action_feedback(self) -> None:
        result = NodeConsoleActionExecutionResult(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="yuki",
            action_key="save_all",
            summary="Minecraft Alpha: save requested.",
            success=True,
            text="[Server] Saved the game",
            source=ConsoleResponseSource.RCON,
        )

        self.assertIsNone(
            ModWebService._console_action_result_for_selection(
                selected_action_key="say",
                last_result_action_key="save_all",
                last_result=result,
            )
        )
        self.assertEqual(
            ModWebService._console_action_result_for_selection(
                selected_action_key="save_all",
                last_result_action_key="save_all",
                last_result=result,
            ),
            result,
        )

    def test_save_detail_path_text_hides_redundant_single_root_labels(self) -> None:
        entry = NodeSaveEntry(
            id="world/world",
            label="world",
            relative_path="world",
            root_id="world",
            root_label="Current World",
            kind="directory",
            size_bytes=0,
            size_text="Directory",
            modified_at="2026-05-28 12:00:00",
        )
        nested_entry = NodeSaveEntry(
            id="world/backups/world",
            label="world",
            relative_path="backups/world",
            root_id="world",
            root_label="Current World",
            kind="directory",
            size_bytes=0,
            size_text="Directory",
            modified_at="2026-05-28 12:00:00",
        )

        self.assertIsNone(ModWebService._save_detail_path_text(save=entry, root_count=1))
        self.assertEqual(ModWebService._save_detail_path_text(save=nested_entry, root_count=1), "backups/world")
        self.assertEqual(
            ModWebService._save_detail_path_text(save=entry, root_count=2),
            "Current World / world",
        )

    def test_normalise_blueprint_title_drops_known_suffixes(self) -> None:
        blueprint = NodeBlueprintEntry(
            id="Session Alpha/Assembler.sbp",
            label="Assembler.sbp",
            session_name="Session Alpha",
            relative_path="Session Alpha/Assembler.sbp",
            size_bytes=128,
            size_text="128B",
            modified_at="2026-06-04 20:00:00",
            uploaded_by_display_name="User 42",
            can_delete=True,
        )
        config_label = "Assembler.sbpcfg"
        legacy = NodeBlueprintEntry(
            id="Session Alpha/Legacy Blueprint",
            label="Legacy Blueprint",
            session_name="Session Alpha",
            relative_path="Session Alpha/Legacy Blueprint",
            size_bytes=128,
            size_text="128B",
            modified_at="2026-06-04 20:00:00",
            uploaded_by_display_name="User 42",
            can_delete=True,
        )

        self.assertEqual(ModWebService._blueprint_card_title(blueprint), "Assembler")
        self.assertEqual(ModWebService._normalise_blueprint_title(config_label), "Assembler")
        self.assertEqual(ModWebService._normalise_blueprint_title(legacy.label), "Legacy Blueprint")

    def test_app_start_stop_label_shows_blocked_when_other_app_is_running(self) -> None:
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=True,
            settings=None,
        )

        self.assertEqual(ModWebService._app_start_stop_label(model), "Blocked")
        self.assertIsNone(ModWebService._app_start_stop_action(model))

    def test_app_start_stop_label_shows_starting_during_start_transition(self) -> None:
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STARTING,
            ),
            app_start_blocked=False,
            settings=None,
        )

        self.assertEqual(ModWebService._app_start_stop_label(model), "Starting")
        self.assertIsNone(ModWebService._app_start_stop_action(model))
        self.assertTrue(ModWebService._app_start_stop_disabled(model))

    def test_app_start_stop_label_shows_stopping_during_stop_transition(self) -> None:
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STOPPING,
            ),
            app_start_blocked=False,
            settings=None,
        )

        self.assertEqual(ModWebService._app_start_stop_label(model), "Stopping")
        self.assertIsNone(ModWebService._app_start_stop_action(model))
        self.assertTrue(ModWebService._app_start_stop_disabled(model))

    def test_app_kill_disabled_only_when_app_is_not_active(self) -> None:
        stopped_model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.NONE,
            ),
            app_start_blocked=False,
            settings=None,
        )
        starting_model = replace(
            stopped_model,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STARTING,
            ),
        )
        running_model = replace(
            stopped_model,
            app_stats=NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STOPPING,
            ),
        )

        self.assertTrue(ModWebService._app_kill_disabled(stopped_model))
        self.assertFalse(ModWebService._app_kill_disabled(starting_model))
        self.assertFalse(ModWebService._app_kill_disabled(running_model))

    def test_model_with_runtime_state_updates_app_stats_and_blocked_flag(self) -> None:
        service = ModWebService()
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
        )

        updated = service._model_with_runtime_state(
            model,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version="1.21.1",
                player_count=0,
                player_capacity=20,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.STOPPING,
            ),
            app_start_blocked=True,
        )

        self.assertTrue(updated.app_start_blocked)
        self.assertIsNotNone(updated.app_stats)
        updated_app_stats = updated.app_stats
        assert updated_app_stats is not None
        self.assertEqual(updated_app_stats.transition_state, NodeAppTransitionState.STOPPING)
        self.assertEqual(updated_app_stats.player_capacity, 20)

    def test_apply_live_app_state_update_updates_update_state_without_runtime_change(self) -> None:
        service = ModWebService()
        update_info = AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            selected_branch_id="public",
            selected_branch_label="Stable",
            branches=(AppUpdateBranchState(branch_id="public", label="Stable", selected=True),),
            supports_verify=True,
        )
        initial_status = AppUpdateStatus(state=AppUpdateState.IDLE, summary="Ready")
        next_status = AppUpdateStatus(
            state=AppUpdateState.RUNNING,
            summary="Downloading",
            operation_kind=AppUpdateOperationKind.UPDATE,
            progress_percent=5.0,
        )
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
            update_info=update_info,
            update_status=initial_status,
        )

        updated_model, _ = service._apply_live_app_state_update(
            model=model,
            event=NodeAppStateStreamEvent.update(
                app_name="minecraft_alpha",
                update_info=update_info,
                update_status=next_status,
            ),
            last_system_summary=None,
        )

        self.assertEqual(updated_model.update_info, update_info)
        self.assertEqual(updated_model.update_status, next_status)

    def test_app_action_pending_feedback_messages_cover_runtime_and_update_actions(self) -> None:
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.START), "Starting...")
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.STOP), "Stopping...")
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.KILL), "Killing...")
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.UPDATE), "Updating...")
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.VERIFY), "Verifying...")
        self.assertEqual(
            ModWebService._app_action_pending_message(NodeAppMutationAction.START, "Minecraft Alpha"),
            "Start requested for Minecraft Alpha.",
        )
        self.assertEqual(
            ModWebService._app_action_pending_message(NodeAppMutationAction.STOP, "Minecraft Alpha"),
            "Stop requested for Minecraft Alpha.",
        )
        self.assertEqual(
            ModWebService._app_action_pending_message(NodeAppMutationAction.KILL, "Minecraft Alpha"),
            "Kill requested for Minecraft Alpha.",
        )
        self.assertEqual(
            ModWebService._app_action_pending_message(NodeAppMutationAction.UPDATE, "Minecraft Alpha"),
            "Update requested for Minecraft Alpha.",
        )
        self.assertEqual(
            ModWebService._app_action_pending_message(NodeAppMutationAction.VERIFY, "Minecraft Alpha"),
            "Verify requested for Minecraft Alpha.",
        )

    def test_app_action_completion_message_suppresses_duplicate_pending_notification(self) -> None:
        pending_message = "Start requested for Minecraft Alpha."

        self.assertIsNone(
            ModWebService._app_action_completion_message(
                pending_message=pending_message,
                result_message=pending_message,
            )
        )
        self.assertEqual(
            ModWebService._app_action_completion_message(
                pending_message=pending_message,
                result_message="Minecraft Alpha started.",
            ),
            "Minecraft Alpha started.",
        )

    def test_render_global_app_toolbar_exposes_details_dialog_for_sudo_users(self) -> None:
        class FakeContainer:
            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeContainer":
                del value, replace
                return self

            def style(self, value: str) -> "FakeContainer":
                del value
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeButton:
            def __init__(self, text: str, on_click: Callable[[], object] | None = None) -> None:
                self.text = text
                self.on_click = on_click
                self.class_value: str | None = None

            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeButton":
                self.class_value = replace if replace is not None else value
                return self

            def set_text(self, text: str) -> None:
                self.text = text

            def disable(self) -> None:
                return None

            def enable(self) -> None:
                return None

        class FakeInput:
            def __init__(self, value: object) -> None:
                self.value = value
                self.class_value: str | None = None
                self.props_value: str | None = None

            def props(self, value: str) -> "FakeInput":
                self.props_value = value
                return self

            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeInput":
                self.class_value = replace if replace is not None else value
                return self

        class FakeSelect(FakeInput):
            def __init__(self, options: object, value: object, label: str) -> None:
                super().__init__(value)
                self.options = options
                self.label = label

        class FakeCheckbox:
            def __init__(self, label: str, value: object) -> None:
                self.label = label
                self.value = value
                self.class_value: str | None = None

            def props(self, value: str) -> "FakeCheckbox":
                del value
                return self

            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeCheckbox":
                self.class_value = replace if replace is not None else value
                return self

        class FakeUi:
            def __init__(self) -> None:
                self.buttons: list[FakeButton] = []
                self.inputs: list[FakeInput] = []
                self.selects: list[FakeSelect] = []
                self.checkboxes: list[FakeCheckbox] = []
                self.navigate = SimpleNamespace(reload=lambda: None)

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def button(self, text: str = "", **kwargs: object) -> FakeButton:
                button = FakeButton(text, cast(Callable[[], object] | None, kwargs.get("on_click")))
                self.buttons.append(button)
                return button

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(kwargs.get("value"))
                self.inputs.append(control)
                return control

            def select(self, options: object, *, value: object, label: str, **kwargs: object) -> FakeSelect:
                del kwargs
                control = FakeSelect(options, value, label)
                self.selects.append(control)
                return control

            def checkbox(self, label: str, *, value: object = False, **kwargs: object) -> FakeCheckbox:
                del kwargs
                control = FakeCheckbox(label, value)
                self.checkboxes.append(control)
                return control

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

        service = ModWebService()
        ui = FakeUi()
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=False,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="erin",
                configs=(),
            ),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=False,
            settings=None,
            app_notes="Main shard",
            lifecycle_notice_started=False,
            lifecycle_notice_stopped=True,
            lifecycle_notice_crashed=False,
            relay_notice_player_session=False,
            relay_notice_player_death=False,
            relay_notice_progress=False,
            relay_notice_progress_label="Research",
            relay_advancements_enabled=False,
            relay_advancement_term="Advancement",
            activity_providers=(
                NodeAppActivityProviderEntry(provider_id="day", label="Day Counter", enabled=False),
            ),
        )
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)

        with patch.object(service, "_user_has_level", return_value=True):
            service._render_global_app_toolbar(
                ui=cast(ModWebUi, cast(object, ui)),
                model=model,
                user=user,
                refresh_async_runtime_model=None,
            )

        self.assertIn("Properties", [button.text for button in ui.buttons])
        self.assertEqual(ui.inputs, [])
        properties_button = next(button for button in ui.buttons if button.text == "Properties")
        self.assertIsNotNone(properties_button.on_click)
        assert properties_button.on_click is not None
        properties_button.on_click()
        self.assertIn("Disable", [button.text for button in ui.buttons])
        self.assertEqual(
            [control.value for control in ui.inputs],
            ["Minecraft Alpha", "Main shard", "0", "0", "", ""],
        )
        self.assertEqual(
            [(control.label, control.value) for control in ui.selects],
            [("Title font [Minecraft Ten]", AppTitleFont.AUTO.value)],
        )
        title_font_options = ui.selects[0].options
        self.assertEqual(len(title_font_options), len(AppTitleFont))
        self.assertEqual(title_font_options["auto"], "Auto (by game)")
        self.assertEqual(title_font_options["arial"], "Arial")
        self.assertEqual(title_font_options["helvetica_neue"], "Helvetica Neue")
        self.assertEqual(title_font_options["minecraft_ten"], "Minecraft Ten")
        self.assertEqual(title_font_options["roboto"], "Roboto")
        self.assertEqual(title_font_options["source_sans_3"], "Source Sans 3")
        self.assertEqual(ui.inputs[0].class_value, "mod-app-details-field")
        self.assertIsNotNone(ui.inputs[0].props_value)
        props_value = ui.inputs[0].props_value
        assert props_value is not None
        self.assertIn("maxlength=80", props_value)
        self.assertEqual(ui.selects[0].props_value, "filled square dense hide-bottom-space color=accent options-dark")
        self.assertEqual(ui.inputs[1].class_value, "mod-app-details-field mod-app-details-notes")
        self.assertEqual(
            [(control.label, control.value) for control in ui.checkboxes],
            [
                ("Started", False),
                ("Stopped", True),
                ("Crash", False),
                ("Player Join/Leave", False),
                ("Death", False),
                ("Research", False),
                ("Advancement", False),
                ("Day Counter", False),
            ],
        )
        self.assertEqual(
            [control.class_value for control in ui.checkboxes],
            [
                "mod-app-details-toggle",
                "mod-app-details-toggle",
                "mod-app-details-toggle",
                "mod-app-details-toggle",
                "mod-app-details-toggle",
                "mod-app-details-toggle",
                "mod-app-details-toggle",
                "mod-app-details-toggle",
            ],
        )

    def test_render_user_header_exposes_discord_settings_button_for_sudo_users(self) -> None:
        class FakeContainer:
            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeContainer":
                del value, replace
                return self

            def props(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
            ) -> "FakeContainer":
                del value, add, remove
                return self

            def on(self, event: str, handler: object | None = None, *, js_handler: str | None = None) -> "FakeContainer":
                del event, handler, js_handler
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeButton(FakeContainer):
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeInput(FakeContainer):
            def __init__(self, value: object) -> None:
                self.value = value
                self.disabled = False

            def disable(self) -> None:
                self.disabled = True

        class FakeUi:
            def __init__(self) -> None:
                self.buttons: list[FakeButton] = []
                self.inputs: list[FakeInput] = []
                self.navigate = SimpleNamespace(reload=lambda: None)

            def row(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def html(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def button(self, text: str = "", **kwargs: object) -> FakeButton:
                del kwargs
                button = FakeButton(text)
                self.buttons.append(button)
                return button

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(kwargs.get("value"))
                self.inputs.append(control)
                return control

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

        service = ModWebService()
        service.set_manager(cast(Any, Mock()))
        ui = FakeUi()
        user = ModWebUser(discord_id=42, username="sudo", global_name=None, avatar_hash=None)
        service._node_api.read_discord_settings = Mock(
            return_value=config.DiscordSettings(
                activity=config.DiscordActivitySettings(
                    fallback_text="Watching over Erin",
                    refresh_interval_seconds=3,
                    units_per_app=2,
                    alt_text_percentage=50,
                    fields=(
                        config.DiscordActivityField.APP,
                        config.DiscordActivityField.PLAYERS,
                    ),
                )
            )
        )

        with (
            patch.object(service, "_action_link", side_effect=lambda **kwargs: None),
            patch.object(service, "_badge", side_effect=lambda **kwargs: FakeContainer()),
            patch.object(service, "_web_display_name", return_value="Finch"),
            patch.object(service, "_user_avatar_uri", return_value="https://example.com/avatar.png"),
            patch.object(service, "_user_level_label", return_value="Dev Sudo"),
            patch.object(service, "_user_level_tone", return_value="red"),
            patch.object(service, "_user_can_use_fake_chat_preview", return_value=False),
            patch.object(service, "_user_has_level", side_effect=lambda _user, level: level is Power_Level.sudo),
        ):
            service._render_user_header(ui=cast(ModWebUi, cast(object, ui)), user=user)

        self.assertIn("Alias", [button.text for button in ui.buttons])
        self.assertIn("Discord", [button.text for button in ui.buttons])
        self.assertEqual(
            [control.value for control in ui.inputs],
            [
                "Watching over Erin",
                "3",
                "2",
                "50",
                "",
                " | ",
                "",
                "app, players",
            ],
        )
        self.assertTrue(ui.inputs[1].disabled)

    def test_render_user_header_keeps_utility_menu_for_unprivileged_users(self) -> None:
        class FakeContainer:
            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeContainer":
                del value, replace
                return self

            def props(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
            ) -> "FakeContainer":
                del value, add, remove
                return self

            def on(self, event: str, handler: object | None = None, *, js_handler: str | None = None) -> "FakeContainer":
                del event, handler, js_handler
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeButton(FakeContainer):
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeUi:
            def __init__(self) -> None:
                self.buttons: list[FakeButton] = []
                self.navigate = SimpleNamespace(reload=lambda: None)

            def row(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def html(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def button(self, text: str = "", **kwargs: object) -> FakeButton:
                del kwargs
                button = FakeButton(text)
                self.buttons.append(button)
                return button

        service = ModWebService()
        ui = FakeUi()
        user = ModWebUser(discord_id=42, username="visitor", global_name=None, avatar_hash=None)

        with (
            patch.object(config, "INDEV", False),
            patch.object(service, "_badge", side_effect=lambda **kwargs: FakeContainer()),
            patch.object(service, "_web_display_name", return_value="Visitor"),
            patch.object(service, "_user_avatar_uri", return_value="https://example.com/avatar.png"),
            patch.object(service, "_user_level_label", return_value="Visitor"),
            patch.object(service, "_user_level_tone", return_value="grey"),
            patch.object(service, "_user_can_manage_discord_settings", return_value=False),
            patch.object(service, "_user_can_use_fake_chat_preview", return_value=False),
        ):
            service._render_user_header(ui=cast(ModWebUi, cast(object, ui)), user=user)

        self.assertIn("Alias", [button.text for button in ui.buttons])
        self.assertIn("Log out", [button.text for button in ui.buttons])

    def test_render_user_header_menu_branch_includes_alias_item(self) -> None:
        class FakeContainer:
            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeContainer":
                del value, replace
                return self

            def props(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
            ) -> "FakeContainer":
                del value, add, remove
                return self

            def on(self, event: str, handler: object | None = None, *, js_handler: str | None = None) -> "FakeContainer":
                del event, handler, js_handler
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeButton(FakeContainer):
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeUi:
            def __init__(self) -> None:
                self.menu_items: list[FakeButton] = []
                self.navigate = SimpleNamespace(reload=lambda: None, to=lambda *_args, **_kwargs: None)

            def row(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def menu(self) -> FakeContainer:
                return FakeContainer()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def html(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def button(self, text: str = "", **kwargs: object) -> FakeButton:
                del text, kwargs
                return FakeButton("")

            def menu_item(self, text: str, **kwargs: object) -> FakeButton:
                del kwargs
                item = FakeButton(text)
                self.menu_items.append(item)
                return item

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

        service = ModWebService()
        ui = FakeUi()
        user = ModWebUser(discord_id=42, username="sudo", global_name="Finch", avatar_hash=None)

        with (
            patch.object(service, "_user_can_manage_discord_settings", return_value=False),
            patch.object(service, "_user_can_use_fake_chat_preview", return_value=False),
            patch.object(service, "_user_has_level", side_effect=lambda _user, level: level is Power_Level.sudo),
        ):
            service._render_user_utility_launcher(ui=cast(ModWebUi, cast(object, ui)), user=user)

        self.assertEqual(
            [item.text for item in ui.menu_items],
            ["Sim Upload", "Sim Download", "Clear Transfers", "Alias", "Log out"],
        )

    def test_alias_target_label_does_not_duplicate_unknown_discord_id(self) -> None:
        cache = object.__new__(config.Name_Cache)
        cache.by_id = {}
        user = ModWebUser(discord_id=42, username=None, global_name=None, avatar_hash=None)

        label = ModWebService._alias_target_label(name_cache=cache, user_id=42, viewer=user)

        self.assertEqual(label, "42")

    def test_alias_known_scopes_includes_configured_app_scopes_without_manager(self) -> None:
        service = ModWebService()

        scopes = service._alias_known_scopes()

        self.assertEqual(scopes, tuple(sorted((scope.value for scope in config.AppScopes), key=str.casefold)))

    def test_persist_alias_dialog_draft_saves_factorio_and_minecraft_aliases(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {42: config.UserNames(games={"beammp": ("RoadRunner", None)})}
            cache.by_alias = {}
            cache.by_platform_id = {}
            draft = cast(
                Any,
                SimpleNamespace(
                    display_name="Portal Finch",
                    app_aliases={
                        "beammp": "RoadRunner",
                        "factorio": "Factory Finch",
                        "minecraft": "Miner Finch",
                    },
                    steam_id="76561198000000001",
                    minecraft_uuid="123e4567-e89b-12d3-a456-426614174000",
                ),
            )

            with patch.object(ModWebService, "_sync_name_cache_with_authority_if_remote") as sync_mock:
                changed_fields = ModWebService._persist_alias_dialog_draft(
                    name_cache=cache,
                    target_user_id=42,
                    draft=draft,
                    scopes=("beammp", "factorio", "minecraft"),
                )

            payload = json.loads(cache.pointer.read_text(encoding="utf-8"))

            self.assertEqual(
                changed_fields,
                (
                    "display name",
                    "Factorio alias",
                    "Minecraft alias",
                    "Steam ID",
                    "Minecraft UUID",
                ),
            )
            self.assertEqual(cache.get_display_override(42), "Portal Finch")
            self.assertEqual(cache.get_game_alias(42, "beammp"), "RoadRunner")
            self.assertEqual(cache.get_game_alias(42, "factorio"), "Factory Finch")
            self.assertEqual(cache.get_game_alias(42, "minecraft"), "Miner Finch")
            self.assertEqual(cache.get_platform_id(42, "steam"), "76561198000000001")
            self.assertEqual(cache.get_game_uuid(42, "minecraft"), "123e4567-e89b-12d3-a456-426614174000")
            sync_mock.assert_called_once_with(name_cache=cache)
            self.assertEqual(payload["42"]["display_overrides"], {"value": "Portal Finch"})
            self.assertEqual(
                payload["42"]["games"],
                {
                    "beammp": ["RoadRunner", None],
                    "factorio": ["Factory Finch", None],
                    "minecraft": ["Miner Finch", "123e4567-e89b-12d3-a456-426614174000"],
                },
            )
            self.assertEqual(payload["42"]["platform_ids"], {"steam": "76561198000000001"})

    def test_sync_name_cache_with_authority_if_remote_flushes_and_refreshes(self) -> None:
        cache = SimpleNamespace(
            flush_pending_mutations=Mock(return_value=2),
            refresh_from_authority=Mock(return_value=True),
        )

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "authority_pending_names_path", return_value=Path("/tmp/pending-names.jsonl")),
            patch.object(Path, "exists", return_value=False),
        ):
            ModWebService._sync_name_cache_with_authority_if_remote(name_cache=cast(Any, cache))

        cache.flush_pending_mutations.assert_called_once_with()
        cache.refresh_from_authority.assert_called_once_with()

    def test_sync_name_cache_with_authority_if_remote_async_uses_sync_helper(self) -> None:
        cache = SimpleNamespace()

        with patch.object(ModWebService, "_sync_name_cache_with_authority_if_remote") as sync_mock:
            asyncio.run(ModWebService._sync_name_cache_with_authority_if_remote_async(name_cache=cast(Any, cache)))

        sync_mock.assert_called_once_with(name_cache=cache)

    def test_sync_name_cache_with_authority_if_remote_fails_when_pending_mutations_remain(self) -> None:
        cache = SimpleNamespace(
            flush_pending_mutations=Mock(return_value=1),
            refresh_from_authority=Mock(return_value=True),
        )

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "authority_pending_names_path", return_value=Path("/tmp/pending-names.jsonl")),
            patch.object(Path, "exists", return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "pending name mutations remain queued"):
                ModWebService._sync_name_cache_with_authority_if_remote(name_cache=cast(Any, cache))

    def test_alias_panel_user_switcher_is_disabled_without_sudo(self) -> None:
        class FakeContainer:
            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeContainer":
                del value, replace
                return self

            def props(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
            ) -> "FakeContainer":
                del value, add, remove
                return self

            def on(self, event: str, handler: object | None = None, *, js_handler: str | None = None) -> "FakeContainer":
                del event, handler, js_handler
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeButton(FakeContainer):
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeInput(FakeContainer):
            def __init__(self, value: object) -> None:
                self.value = value
                self.disabled = False

            def disable(self) -> None:
                self.disabled = True

        class FakeSelect(FakeInput):
            def __init__(self, options: object, value: object, label: str) -> None:
                super().__init__(value)
                self.options = options
                self.label = label

        class FakeUi:
            def __init__(self) -> None:
                self.buttons: list[FakeButton] = []
                self.inputs: list[FakeInput] = []
                self.selects: list[FakeSelect] = []
                self.navigate = SimpleNamespace(reload=lambda: None)

            def row(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def html(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def button(self, text: str = "", **kwargs: object) -> FakeButton:
                del kwargs
                button = FakeButton(text)
                self.buttons.append(button)
                return button

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(kwargs.get("value"))
                self.inputs.append(control)
                return control

            def select(self, options: object, *, value: object, label: str, **kwargs: object) -> FakeSelect:
                del kwargs
                control = FakeSelect(options, value, label)
                self.selects.append(control)
                return control

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

        service = ModWebService()
        service.set_manager(cast(Any, SimpleNamespace(apps={}, list_known_scopes=lambda: ("factorio", "minecraft"))))
        ui = FakeUi()
        user = ModWebUser(discord_id=42, username="visitor", global_name="Visitor", avatar_hash=None)
        cache = object.__new__(config.Name_Cache)
        cache.pointer = Path("discord_names.json")
        cache.by_id = {7: config.UserNames(account="other_user", global_name="Other User")}
        cache.by_alias = {}
        cache.by_platform_id = {}

        with patch.object(config, "Name_Cache", return_value=cache):
            with patch.object(service, "_user_has_level", return_value=False):
                open_panel = service._build_alias_panel(ui=cast(ModWebUi, cast(object, ui)), user=user)
                open_panel()

        self.assertEqual(len(ui.selects), 1)
        self.assertEqual(ui.selects[0].label, "User")
        self.assertTrue(ui.selects[0].disabled)
        self.assertEqual(ui.selects[0].value, "42")

    def test_alias_panel_user_switcher_is_enabled_for_sudo(self) -> None:
        class FakeContainer:
            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeContainer":
                del value, replace
                return self

            def props(self, value: str) -> "FakeContainer":
                del value
                return self

            def style(
                self,
                value: str | None = None,
                *,
                add: str | None = None,
                remove: str | None = None,
            ) -> "FakeContainer":
                del value, add, remove
                return self

            def on(self, event: str, handler: object | None = None, *, js_handler: str | None = None) -> "FakeContainer":
                del event, handler, js_handler
                return self

            def __enter__(self) -> "FakeContainer":
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object | None,
            ) -> bool:
                del exc_type, exc, traceback
                return False

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeButton(FakeContainer):
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeInput(FakeContainer):
            def __init__(self, value: object) -> None:
                self.value = value
                self.disabled = False

            def disable(self) -> None:
                self.disabled = True

        class FakeSelect(FakeInput):
            def __init__(self, options: object, value: object, label: str) -> None:
                super().__init__(value)
                self.options = options
                self.label = label

        class FakeUi:
            def __init__(self) -> None:
                self.buttons: list[FakeButton] = []
                self.inputs: list[FakeInput] = []
                self.selects: list[FakeSelect] = []
                self.navigate = SimpleNamespace(reload=lambda: None)

            def row(self) -> FakeContainer:
                return FakeContainer()

            def column(self) -> FakeContainer:
                return FakeContainer()

            def card(self) -> FakeContainer:
                return FakeContainer()

            def dialog(self) -> FakeDialog:
                return FakeDialog()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def html(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def label(self, text: str) -> FakeContainer:
                del text
                return FakeContainer()

            def button(self, text: str = "", **kwargs: object) -> FakeButton:
                del kwargs
                button = FakeButton(text)
                self.buttons.append(button)
                return button

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(kwargs.get("value"))
                self.inputs.append(control)
                return control

            def select(self, options: object, *, value: object, label: str, **kwargs: object) -> FakeSelect:
                del kwargs
                control = FakeSelect(options, value, label)
                self.selects.append(control)
                return control

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

        service = ModWebService()
        service.set_manager(cast(Any, SimpleNamespace(apps={}, list_known_scopes=lambda: ("factorio", "minecraft"))))
        ui = FakeUi()
        user = ModWebUser(discord_id=42, username="sudo", global_name="Finch", avatar_hash=None)
        cache = object.__new__(config.Name_Cache)
        cache.pointer = Path("discord_names.json")
        cache.by_id = {7: config.UserNames(account="other_user", global_name="Other User")}
        cache.by_alias = {}
        cache.by_platform_id = {}

        with patch.object(config, "Name_Cache", return_value=cache):
            with patch.object(service, "_user_has_level", side_effect=lambda _user, level: level is Power_Level.sudo):
                open_panel = service._build_alias_panel(ui=cast(ModWebUi, cast(object, ui)), user=user)
                open_panel()

        self.assertEqual(len(ui.selects), 1)
        self.assertEqual(ui.selects[0].label, "User")
        self.assertFalse(ui.selects[0].disabled)
        self.assertEqual(ui.selects[0].value, "42")
        self.assertEqual(
            ui.selects[0].options,
            {
                "42": "Finch (42)",
                "7": "Other User (7)",
            },
        )

    def test_app_enable_disable_label_reflects_enabled_state(self) -> None:
        enabled_model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.user,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=cast(Any, SimpleNamespace(configs=())),
            saves=None,
            app_stats=NodeAppRuntimeSummary(
                running=False,
                enabled=True,
                version=None,
                player_count=None,
                player_capacity=None,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
            ),
            app_start_blocked=False,
            settings=None,
        )

        self.assertEqual(ModWebService._app_enable_disable_label(enabled_model), "Disable")

    def test_read_config_content_uses_entry_read_level(self) -> None:
        service = ModWebService()
        model = ModWebBasePageModel(
            node_name="erin",
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            app_color_hex="#22C55E",
            supports_configs=True,
            config_read_level=Power_Level.visitor,
            config_write_level=Power_Level.sudo,
            supports_save_uploads=False,
            supports_save_rename=False,
            save_write_level=Power_Level.user,
            configs=NodeConfigList(
                app_name="minecraft_alpha",
                app_friendly="Minecraft Alpha",
                node="erin",
                configs=(
                    self._config_entry(
                        root_id="server",
                        root_label="Server Configs",
                        relative_path="secret.toml",
                        read_power_level=Power_Level.user,
                    ),
                ),
            ),
            saves=None,
            app_stats=None,
            app_start_blocked=False,
            settings=None,
        )
        user = ModWebUser(discord_id=42, username="visitor", global_name=None, avatar_hash=None)
        service._acl = cast(
            Any, SimpleNamespace(can=lambda user_id, required_level: required_level <= Power_Level.visitor)
        )

        with self.assertRaisesRegex(PermissionError, "User access is required"):
            asyncio.run(service._read_config_content(model=model, config_id="server/secret.toml", user=user))

    def test_async_title_stats_refresher_skips_overlapping_runs(self) -> None:
        async def exercise() -> None:
            refresh_started = asyncio.Event()
            release_refresh = asyncio.Event()
            refresh_calls = 0
            expected_stats = (ModWebTitleStat(label="Status", value="Running", tone="purple"),)
            applied_stats: list[tuple[ModWebTitleStat, ...]] = []

            async def refresh_async_stats() -> tuple[ModWebTitleStat, ...]:
                nonlocal refresh_calls
                refresh_calls += 1
                refresh_started.set()
                await release_refresh.wait()
                return expected_stats

            refresh_async = ModWebService._build_async_title_stats_refresher(
                refresh_async_stats=refresh_async_stats,
                apply_stats=applied_stats.append,
            )

            first_refresh = asyncio.create_task(refresh_async())
            await refresh_started.wait()
            await refresh_async()
            self.assertEqual(refresh_calls, 1)

            release_refresh.set()
            await first_refresh
            self.assertEqual(applied_stats, [expected_stats])

            await refresh_async()
            self.assertEqual(refresh_calls, 2)
            self.assertEqual(applied_stats, [expected_stats, expected_stats])

        asyncio.run(exercise())

    def test_request_path_preserves_query_string(self) -> None:
        request = SimpleNamespace(
            url=SimpleNamespace(path="/mod-web/mods/minecraft_survival", query="tab=mods&view=configs")
        )

        path = ModWebService._request_path(cast(Any, request))

        self.assertEqual(path, "/mod-web/mods/minecraft_survival?tab=mods&view=configs")


if __name__ == "__main__":
    unittest.main()
