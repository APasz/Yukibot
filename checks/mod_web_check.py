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
from unittest.mock import AsyncMock, Mock, call, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import requests
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from nicegui.elements.link import Link
from yarl import URL

import config
from _minecraft_heads import minecraft_dev_bypass_head_data_uri
from _security import Access_Control, Power_Level
from apps._app import AppRuntimeFault, AppRuntimeFaultKind, ChatRelaySupport
from apps._config import ModType
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
from config import BotConfiguration, BotMetadataSnapshot, ModWebServerConfig
from mod_web_auth import ModWebUser
from node_api import (
    ConsoleResponseSource,
    NodeAppEntry,
    NodeAppMutationAction,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeBlueprintEntry,
    NodeBlueprintList,
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
    NodeModEntry,
    NodeModList,
    NodeModSummary,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveRootEntry,
    NodeSettingChoice,
    NodeSettingEntry,
    NodeSettingList,
    NodeStateStreamEvent,
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
from web_dash.home import _ModWebNodeLatencyBadgeSpec
from web_dash.nicegui_protocols import ModWebUi
from web_dash.service import ModWebService
from web_dash.types import (
    ModDownloadKind,
    ModWebAppLink,
    ModWebAppSectionKind,
    ModWebAppTabContext,
    ModWebAppTabDefinition,
    ModWebAppTabVisibilityRule,
    ModWebBasePageModel,
    ModWebConfigEditorShape,
    ModWebHomeNodeSummary,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebOverviewPageModel,
    ModWebPageModel,
    ModWebSearchOption,
    ModWebSettingControlKind,
    ModWebTitleStat,
    _ModWebAppCardBadgeSpec,
    _ModWebBadgeSpec,
    _ModWebChatComposeRequest,
    _ModWebChatPanelConfig,
    _ModWebChatPanelSignal,
    _ModWebChatSurfaceConfig,
    _ModWebFakeChatMessageMode,
    _ModWebFakeChatPreviewState,
    _ModWebLinkSpec,
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
    def __init__(self) -> None:
        self.delete_handlers: list[Callable[..., object]] = []

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


class ModWebTests(unittest.TestCase):
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
    def _mod_list(*, app_name: str = "minecraft_alpha") -> NodeModList:
        return NodeModList(
            app_name=app_name,
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
        )

    @staticmethod
    def _mod_entry(
        *,
        name: str,
        friendly: str | None = None,
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
    ) -> NodeModEntry:
        return NodeModEntry(
            name=name,
            friendly=friendly or name,
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
        )

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

    def test_build_home_node_stat_specs_groups_metrics_per_node(self) -> None:
        node_stats = ModWebService._build_home_node_stat_specs(
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
                ("RAM", "memory", "44% · 8.0GiB / 16.0GiB", "purple"),
                ("Storage", "storage", "55% · 100.0GiB / 200.0GiB", "purple"),
                ("Uptime", "schedule", "2h 0m | 1d 0h 0m", "black"),
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
        self.assertEqual(node_stats[1].metrics[2].value, "99% · 1.0GiB / 78.6GiB")
        self.assertEqual(node_stats[1].metrics[2].tone, "red")
        self.assertEqual(node_stats[1].metrics[3].value, "<1m")

    def test_node_display_subtitle_omits_case_only_duplicates(self) -> None:
        subtitle: str | None = ModWebService._node_display_subtitle(label="Erin", node_name="erin")

        self.assertIsNone(subtitle)

    def test_node_display_subtitle_keeps_distinct_node_name(self) -> None:
        subtitle: str | None = ModWebService._node_display_subtitle(label="Production", node_name="erin-prod")

        self.assertEqual(subtitle, "erin-prod")

    def test_app_start_blocked_remote_uses_app_name_identity(self) -> None:
        self.assertTrue(
            ModWebService._app_start_blocked_remote(
                app_name="minecraft_alpha",
                app_stats=None,
                running_app_ids=("factorio_lab",),
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
                running_app_ids=("factorio_lab",),
            )
        )
        self.assertFalse(
            ModWebService._app_start_blocked_remote(
                app_name="minecraft_alpha",
                app_stats=None,
                running_app_ids=("minecraft_alpha",),
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

    def test_home_node_latency_badges_javascript_embeds_probe_urls(self) -> None:
        script = ModWebService._home_node_latency_badges_javascript(
            (
                _ModWebNodeLatencyBadgeSpec(
                    badge_id=101,
                    node_label="Yuki",
                    fallback_text="Yuki: Alive",
                    probe_url="/api/node/ping",
                ),
                _ModWebNodeLatencyBadgeSpec(
                    badge_id=202,
                    node_label="Erin",
                    fallback_text="Erin: Down",
                    probe_url=None,
                ),
            )
        )

        self.assertIn('"badge_id":101', script)
        self.assertIn('"node_label":"Yuki"', script)
        self.assertIn('"fallback_text":"Yuki: Alive"', script)
        self.assertIn('"probe_url":"/api/node/ping"', script)
        self.assertIn('"probe_url":null', script)
        self.assertIn("modWebHomeNodeLatency", script)
        self.assertIn("_mod_web_latency_probe", script)
        self.assertIn("${spec.node_label}: ${latency}", script)
        self.assertIn("lastTextByBadgeId", script)
        self.assertIn("if (controllerState.inFlight)", script)

    def test_node_capability_badges_use_updated_wording(self) -> None:
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
                _ModWebBadgeSpec(text="2 apps", tone="black"),
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
        )
        render_badge.assert_not_called()
        attach_text_tooltip.assert_called_once_with(
            ui=ui,
            target=badge_element,
            text="Simulate this node going down.",
        )

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

    def test_node_badge_style_colours_badge_surface_and_text(self) -> None:
        style: str = ModWebService._node_badge_style("#dc6b0f")

        self.assertEqual(style, "border-color: #dc6b0f !important;")
        self.assertNotIn("background:", style)

    def test_probe_node_status_treats_http_response_as_alive(self) -> None:
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        response: SimpleNamespace = SimpleNamespace(status_code=401)

        with patch("web_dash.page_handlers.requests.get", return_value=response):
            status: ModWebNodeStatus = ModWebService()._probe_node_status(node)

        self.assertEqual(status, ModWebNodeStatus(node=node, alive=True, detail="HTTP 401"))

    def test_probe_node_status_uses_presence_timeout(self) -> None:
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )
        response: SimpleNamespace = SimpleNamespace(status_code=200)

        with patch("web_dash.page_handlers.requests.get", return_value=response) as get_request:
            ModWebService()._probe_node_status(node)

        get_request.assert_called_once_with("https://erin.example/api/node/ping", timeout=(2.0, 4.0))

    def test_probe_node_status_marks_request_failure_down(self) -> None:
        node: ModWebNodeLink = ModWebNodeLink(
            node_name="erin",
            label="Erin",
            url="/mod-web/nodes/erin",
            api_base_url="https://erin.example/api/node",
            api_url="/api/node-proxy/erin/apps",
            is_current=False,
        )

        with patch("web_dash.page_handlers.requests.get", side_effect=requests.RequestException("timeout")):
            status: ModWebNodeStatus = ModWebService()._probe_node_status(node)

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
            patch.object(ModWebService, "_probe_node_status") as probe_node_status,
        ):
            statuses = service._login_node_statuses(simulated_down_node_names=("erin",))

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
        probe_node_status.assert_not_called()

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

    def test_format_uptime_seconds_compacts_duration(self) -> None:
        self.assertEqual(ModWebService._format_uptime_seconds(59), "<1m")
        self.assertEqual(ModWebService._format_uptime_seconds(3661), "1h 1m")
        self.assertEqual(ModWebService._format_uptime_seconds(90061), "1d 1h 1m")

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
        self.assertEqual(links[0].url, "/")
        self.assertEqual(links[0].latency_probe_url, "/api/node/ping")
        self.assertEqual(links[1].url, "/mod-web/nodes/erin")
        self.assertEqual(links[1].api_base_url, "http://erin.example:3180/api/node")
        self.assertEqual(links[1].api_url, "/api/node-proxy/erin/apps")
        self.assertEqual(links[1].latency_probe_url, "http://erin.example:3180/api/node/ping")

    def test_app_links_include_dedicated_chat_link_for_local_chat_relay_apps(self) -> None:
        service: ModWebService = ModWebService()
        app: SimpleNamespace = SimpleNamespace(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            cfg=SimpleNamespace(enabled=True),
            manage_embed_color=0x22C55E,
            public_map_url=None,
            mods=None,
            supports_config_files=False,
            supports_save_files=False,
            supports_save_uploads=False,
            supports_save_rename=False,
            supports_settings=False,
            supports_chat_relay=True,
            lowest_config_file_read_level=Power_Level.user,
            config_file_write_level=Power_Level.admin,
            save_file_write_level=Power_Level.sudo,
            check_running=Mock(return_value=False),
        )
        service.set_manager(_manager_stub(apps={"minecraft_alpha": app}))
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        links: tuple[ModWebAppLink, ...] = asyncio.run(service._app_links(user))

        self.assertEqual(len(links), 1)
        self.assertTrue(links[0].enabled)
        self.assertEqual(links[0].color_hex, "#22C55E")
        self.assertTrue(links[0].supports_chat)
        self.assertEqual(links[0].chat_url, "/mod-web/chat/minecraft_alpha")
        self.assertIsNone(links[0].player_count)

    def test_app_links_include_player_count_for_running_local_apps(self) -> None:
        service: ModWebService = ModWebService()
        app: SimpleNamespace = SimpleNamespace(
            name="minecraft_alpha",
            friendly="Minecraft Alpha",
            cfg=SimpleNamespace(enabled=True),
            manage_embed_color=0x22C55E,
            public_map_url=None,
            mods=None,
            supports_config_files=False,
            supports_save_files=False,
            supports_save_uploads=False,
            supports_save_rename=False,
            supports_settings=False,
            supports_chat_relay=True,
            lowest_config_file_read_level=Power_Level.user,
            config_file_write_level=Power_Level.admin,
            save_file_write_level=Power_Level.sudo,
            check_running=Mock(return_value=True),
            player_count=AsyncMock(return_value=(4, 12)),
        )
        service.set_manager(_manager_stub(apps={"minecraft_alpha": app}))
        user: ModWebUser = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        links: tuple[ModWebAppLink, ...] = asyncio.run(service._app_links(user))

        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].player_count, 4)
        self.assertEqual(links[0].player_capacity, 12)

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

        with patch.object(ModWebService, "_remote_apps_async", AsyncMock(return_value=(entry,))):
            links: tuple[ModWebAppLink, ...] = asyncio.run(service._remote_app_links(node, user))

        self.assertEqual(len(links), 1)
        self.assertFalse(links[0].enabled)
        self.assertEqual(links[0].color_hex, "#DC2626")
        self.assertTrue(links[0].supports_console_actions)
        self.assertTrue(links[0].supports_chat)
        self.assertEqual(links[0].chat_url, "/mod-web/nodes/erin/chat/minecraft_alpha")
        self.assertIsNone(links[0].player_count)

    def test_remote_apps_uses_presence_timeout(self) -> None:
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

        with patch.object(ModWebService, "_remote_json", return_value={"apps": []}) as remote_json:
            self.assertEqual(service._remote_apps(node, user), ())

        remote_json.assert_called_once_with(
            node=node,
            app_name=None,
            path="/apps",
            scopes=(NodeApiScope.APPS_READ,),
            user=user,
            timeout=(2.0, 4.0),
        )

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
            patch.object(ModWebService, "_app_links", new=AsyncMock(return_value=())),
            patch.object(ModWebService, "_remote_app_links", new=AsyncMock(side_effect=remote_failure)),
        ):
            sections = asyncio.run(service._home_app_sections(user))

        self.assertEqual(len(sections), 2)
        self.assertIsNone(sections[0].error)
        self.assertEqual(
            sections[1].error,
            "This node is unreachable right now. It may be offline or still waking up.",
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
            patch.object(ModWebService, "_app_links", new=AsyncMock(return_value=())),
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

            def classes(self, value: str) -> "FakeContainer":
                self.class_value = value
                return self

            def style(self, value: str) -> "FakeContainer":
                self.style_value = value
                return self

            def on(self, event_name: str, handler: object) -> "FakeContainer":
                del handler
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

            def card(self) -> FakeContainer:
                card = FakeContainer(kind="card", ui=self)
                self.cards.append(card)
                self.elements.append(card)
                return card

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

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
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)

        with patch.object(ModWebService, "_render_app_card_content") as render_app_card_content:
            service._render_home_page_sections(
                ui=cast(ModWebUi, cast(object, ui)),
                sections=(section,),
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
        self.assertEqual(render_app_card_content.call_count, 2)

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
            ["black", "black", "black", "black", "purple", "purple"],
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
        self.assertEqual([badge.tone for badge in badges], ["black"])

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

        classes = ModWebService._app_card_link_classes(app)

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

        classes = ModWebService._app_card_link_classes(app)

        self.assertIn("mod-app-card-running", classes)
        self.assertNotIn("mod-app-card-starting", classes)
        self.assertNotIn("mod-app-card-stopping", classes)

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

        classes = ModWebService._app_card_link_classes(app)

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

            def classes(self, value: str | None = None, *, replace: str | None = None) -> "FakeLabel":
                self.class_value = replace if replace is not None else value
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

            def column(self) -> FakeContainer:
                return FakeContainer()

            def row(self) -> FakeContainer:
                return FakeContainer()

            def label(self, text: str) -> FakeLabel:
                self.label_texts.append(text)
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def tooltip(self) -> FakeTooltip:
                return FakeTooltip()

            def html(self, content: str) -> FakeHtml:
                return FakeHtml(content)

        service = ModWebService()
        ui = FakeUi()
        hero_card = FakeCard()
        static_badges = (_ModWebBadgeSpec(text="4 Mods", tone="black"),)
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
        )

        apply_runtime = service._render_live_app_hero_runtime(
            ui=cast(ModWebUi, cast(object, ui)),
            hero_card=cast(Any, hero_card),
            title="Minecraft Alpha",
            static_badges=static_badges,
            initial_app_stats=initial_stats,
        )

        self.assertEqual(hero_card.replaced_classes, "mod-card mod-card-hero w-full mod-app-hero-running")
        self.assertEqual(ui.label_texts[:3], ["Minecraft Alpha", "Status", "Running"])
        self.assertEqual(
            ui.label_texts,
            ["Minecraft Alpha", "Status", "Running", "Unsupported", "1.21.1", "Unavailable", "", "4 Mods"],
        )

        apply_runtime(updated_stats)

        self.assertEqual(hero_card.replaced_classes, "mod-card mod-card-hero w-full mod-app-hero-starting")
        self.assertEqual(
            ui.label_texts,
            ["Minecraft Alpha", "Status", "Running", "Unsupported", "1.21.1", "Unavailable", "", "4 Mods"],
        )
        self.assertEqual(ui.labels[2].text, "Starting")

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
            "/mod-web/mods/minecraft_alpha?view=compact&tab=saves&dev_api=1",
            tab_id="configs",
        )

        self.assertEqual(updated_url, "/mod-web/mods/minecraft_alpha?view=compact&dev_api=1&tab=configs")

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

    def test_simulated_down_node_names_ignore_current_and_unknown_nodes(self) -> None:
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

        self.assertEqual(simulated_down_node_names, ("erin", "kousei"))

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

    def test_fake_chat_preview_app_options_include_chat_apps_in_friendly_order(self) -> None:
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

        options = service._fake_chat_preview_app_options()

        self.assertEqual(
            options,
            {
                "Alpha (alpha)": "alpha",
                "beta (beta)": "beta",
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

    def test_dev_error_preview_actions_list_expected_preview_routes(self) -> None:
        actions = ModWebService._dev_error_preview_actions()

        self.assertEqual(
            actions,
            (
                _ModWebLinkSpec(label="Access Denied", url="/mod-web/dev/error/access-denied"),
                _ModWebLinkSpec(label="Sign-in Unavailable", url="/mod-web/dev/error/sign-in-unavailable"),
                _ModWebLinkSpec(label="Page Unavailable", url="/mod-web/dev/error/page-unavailable"),
                _ModWebLinkSpec(label="Chat Unavailable", url="/mod-web/dev/error/chat-unavailable"),
                _ModWebLinkSpec(label="Node Unavailable", url="/mod-web/dev/error/node-unavailable"),
                _ModWebLinkSpec(label="Remote JSON Invalid", url="/mod-web/dev/error/remote-json-invalid"),
                _ModWebLinkSpec(label="Remote Timeout", url="/mod-web/dev/error/remote-timeout"),
                _ModWebLinkSpec(label="Remote Rejected", url="/mod-web/dev/error/remote-rejected"),
                _ModWebLinkSpec(label="Framework 404", url="/mod-web/dev/error/framework-404"),
                _ModWebLinkSpec(label="Framework 500", url="/mod-web/dev/error/framework-500"),
                _ModWebLinkSpec(label="NiceGUI Exception", url="/mod-web/dev/error/nicegui-exception"),
                _ModWebLinkSpec(label="Refresh Shutdown", url="/mod-web/dev/error/refresh-shutdown"),
                _ModWebLinkSpec(label="Config Fail Toasts", url="/mod-web/dev/error/config-failure"),
                _ModWebLinkSpec(label="Chat Stream WS", url="/mod-web/dev/error/chat-stream-websocket"),
            ),
        )

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
        self.assertFalse(
            ModWebService._should_render_framework_error_page(
                method="GET",
                path="/api/node/apps",
                accept_header="text/html,application/xhtml+xml",
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
        relay_mention_name = Mock(side_effect=lambda user_id, **_: f"user-{user_id}")

        with patch(
            "web_dash.chat.config.Name_Cache",
            return_value=SimpleNamespace(relay_mention_name=relay_mention_name),
        ):
            resolved = service._resolve_chat_markup_mentions(
                "hi <@42> and @43",
                room_id="minecraft_alpha",
                preferred_guild_id=99,
            )

        self.assertEqual(resolved, "hi @user-42 and @user-43")
        self.assertEqual(
            relay_mention_name.call_args_list,
            [
                call(42, scope="minecraft", preferred_guild_id=99, default="42"),
                call(43, scope="minecraft", preferred_guild_id=99, default="43"),
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
        relay_display_name = Mock(return_value="Yoko")

        with patch(
            "web_dash.chat.config.Name_Cache",
            return_value=SimpleNamespace(relay_display_name=relay_display_name),
        ):
            resolved = service._chat_event_author_display_name(event)

        self.assertEqual(resolved, "Yoko")
        relay_display_name.assert_called_once_with(42, "42", scope="minecraft", preferred_guild_id=99)

    def test_chat_reference_label_resolves_raw_discord_mentions(self) -> None:
        service = ModWebService()
        service.set_manager(
            _manager_stub(
                apps={
                    "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", scope="minecraft"),
                }
            )
        )
        relay_mention_name = Mock(return_value="Yoko")

        with patch(
            "web_dash.chat.config.Name_Cache",
            return_value=SimpleNamespace(relay_mention_name=relay_mention_name),
        ):
            label = service._chat_reference_label(
                ChatReferenceKind.REPLY,
                ChatMessageReference("<@42>", "Joined the game"),
                room_id="minecraft_alpha",
                preferred_guild_id=99,
            )

        self.assertEqual(label, "Replying to Yoko")
        relay_mention_name.assert_called_once_with(42, scope="minecraft", preferred_guild_id=99, default="42")

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
        self.assertIn('<img class="mod-chat-media-image"', markup)
        self.assertIn('src="https://example.invalid/cat.gif"', markup)
        self.assertIn('href="https://example.invalid/cat.gif"', markup)

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

        self.assertEqual(ModWebService._chat_app_status_badge(running_stats), ("Running", "purple"))
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
            ModWebService._chat_player_count_badge(empty_stats),
            _ModWebBadgeSpec(text="0 / 20", tone="grey"),
        )
        self.assertIsNone(ModWebService._chat_player_count_badge(missing_stats))
        self.assertIsNone(ModWebService._chat_player_count_badge(None))

    def test_player_count_tooltip_html_lists_connected_players(self) -> None:
        service = ModWebService()

        self.assertEqual(
            service._player_count_tooltip_html(
                player_count=3,
                player_capacity=20,
                connected_player_names=("Yoko", "Bea", "Casey"),
            ),
            "Yoko<br>Bea<br>Casey",
        )
        self.assertIsNone(
            service._player_count_tooltip_html(
                player_count=3,
                player_capacity=20,
                connected_player_names=(),
            )
        )
        self.assertIsNone(
            service._player_count_tooltip_html(
                player_count=3,
                player_capacity=None,
                connected_player_names=("Yoko",),
            )
        )

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
            room_callback(object())
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

    def test_local_chat_panel_config_send_message_uses_scoped_relay_display_name(self) -> None:
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
            return_value=SimpleNamespace(relay_display_name=Mock(return_value="AliceGame")),
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
            room_callback(object())
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
                _ModWebBadgeSpec(text="2 blocked", tone="warn"),
                _ModWebBadgeSpec(text="4 downloadable", tone="purple"),
                _ModWebBadgeSpec(text="2 coremods", tone="red"),
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
        self.assertIn("hiddenMessageCount", script)
        self.assertIn("modChatWasPinned", script)
        self.assertIn("modChatHiddenCount", script)
        self.assertIn("autoScrollHiddenMessageLimit = 3", script)
        self.assertIn("shouldAutoScrollAfterRefresh", script)
        self.assertIn("loadedmetadata", script)

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

    def test_render_mods_section_adds_search_box_and_filters_visible_rows(self) -> None:
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

        class FakeButton:
            def __init__(self) -> None:
                self.text: str = ""
                self.enabled: bool = True
                self.class_value: str | None = None

            def classes(self, value: str) -> "FakeButton":
                self.class_value = value
                return self

            def set_text(self, value: str) -> None:
                self.text = value

            def set_enabled(self, enabled: bool) -> None:
                self.enabled = enabled

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
            def __init__(self, *, placeholder: str | None = None) -> None:
                self.placeholder = placeholder
                self.class_value: str | None = None
                self.props_value: str | None = None
                self.handlers: dict[str, Callable[[object], None]] = {}

            def props(self, value: str) -> "FakeInput":
                self.props_value = value
                return self

            def classes(self, value: str) -> "FakeInput":
                self.class_value = value
                return self

            def on(self, event_name: str, handler: Callable[[object], None]) -> "FakeInput":
                self.handlers[event_name] = handler
                return self

        class FakeUpload:
            def classes(self, value: str) -> "FakeUpload":
                del value
                return self

            def disable(self) -> None:
                return None

            def enable(self) -> None:
                return None

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeRefreshable:
            def __init__(self, func: Callable[[str], None]) -> None:
                self._func = func

            def __call__(self, search_query: str) -> None:
                self._func(search_query)

            def refresh(self, search_query: str) -> None:
                self._func(search_query)

        class FakeUi:
            def __init__(self) -> None:
                self.labels: list[FakeLabel] = []
                self.inputs: list[FakeInput] = []
                self.navigate = SimpleNamespace(reload=lambda: None)

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

            def upload(self, *args: object, **kwargs: object) -> FakeUpload:
                del args, kwargs
                return FakeUpload()

            def button(self, *args: object, **kwargs: object) -> FakeButton:
                del args, kwargs
                return FakeButton()

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(placeholder=cast(str | None, kwargs.get("placeholder")))
                self.inputs.append(control)
                return control

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
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
                    total_count=2,
                    enabled_count=2,
                    disabled_count=0,
                    coremod_count=0,
                    downloadable_count=2,
                    non_downloadable_count=0,
                ),
                mods=(
                    self._mod_entry(name="alpha-fabric.jar", friendly="Alpha Fabric"),
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
        )
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
        ui = FakeUi()
        rendered_mod_names: list[str] = []

        with patch.object(
            ModWebService,
            "_render_mod_download_row",
            side_effect=lambda **kwargs: rendered_mod_names.append(kwargs["entry"].name) or None,
        ):
            service._render_mods_section(ui=cast(ModWebUi, cast(object, ui)), model=model, user=user)

            self.assertEqual([control.placeholder for control in ui.inputs], ["Search mods"])
            self.assertEqual(
                ui.inputs[0].class_value,
                "mod-config-search mod-settings-search mod-mods-toolbar-search",
            )
            self.assertEqual(rendered_mod_names, ["alpha-fabric.jar", "beta-forge.jar"])

            search_handler = ui.inputs[0].handlers["update:model-value"]
            search_handler(SimpleNamespace(args="beta"))
            self.assertEqual(rendered_mod_names, ["alpha-fabric.jar", "beta-forge.jar", "beta-forge.jar"])

            search_handler(SimpleNamespace(args="missing"))

        self.assertIn("No mods match that search.", [label.text for label in ui.labels])

    def test_render_saves_editor_uses_settings_search_styling(self) -> None:
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
            def __init__(self, *, placeholder: str | None = None) -> None:
                self.placeholder = placeholder
                self.class_value: str | None = None
                self.handlers: dict[str, Callable[[object], None]] = {}

            def props(self, value: str) -> "FakeInput":
                del value
                return self

            def classes(self, value: str) -> "FakeInput":
                self.class_value = value
                return self

            def on(self, event_name: str, handler: Callable[[object], None]) -> "FakeInput":
                self.handlers[event_name] = handler
                return self

        class FakeUpload:
            def classes(self, value: str) -> "FakeUpload":
                del value
                return self

        class FakeButton:
            def classes(self, value: str) -> "FakeButton":
                del value
                return self

        class FakeDialog(FakeContainer):
            def open(self) -> None:
                return None

            def close(self) -> None:
                return None

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
                self.navigate = SimpleNamespace(reload=lambda: None)

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
                del args, kwargs
                return FakeUpload()

            def button(self, *args: object, **kwargs: object) -> FakeButton:
                del args, kwargs
                return FakeButton()

            def label(self, text: str) -> FakeLabel:
                label = FakeLabel(text)
                self.labels.append(label)
                return label

            def input(self, *args: object, **kwargs: object) -> FakeInput:
                del args
                control = FakeInput(placeholder=cast(str | None, kwargs.get("placeholder")))
                self.inputs.append(control)
                return control

            def notify(self, message: str, *, type: str | None = None) -> None:
                del message, type
                return None

        service = ModWebService()
        model = cast(
            ModWebPageModel,
            cast(
                object,
                SimpleNamespace(
                app_friendly="Factorio",
                supports_save_uploads=False,
                supports_save_rename=False,
                save_write_level=Power_Level.user,
                saves=NodeSaveList(
                    app_name="factorio",
                    app_friendly="Factorio",
                    node="yuki",
                    roots=(NodeSaveRootEntry(id="worlds", label="Worlds"),),
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
            self.assertEqual(rendered_save_names, ["alpha.zip", "beta.zip"])

            search_handler = ui.inputs[0].handlers["update:model-value"]
            search_handler(SimpleNamespace(args="beta"))
            self.assertEqual(rendered_save_names, ["alpha.zip", "beta.zip", "beta.zip"])

            search_handler(SimpleNamespace(args="missing"))

        self.assertIn("No saves match that search.", [label.text for label in ui.labels])

    def test_remote_node_ui_redirects_to_yuki_portal_node_page(self) -> None:
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
        self.assertEqual(response.headers["location"], "https://wakusei.apasz.com/mod-web/nodes/erin")

    def test_remote_node_app_page_redirects_to_yuki_portal_app_page(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        request = SimpleNamespace(
            method="GET", url=SimpleNamespace(path="/mod-web/mods/minecraft_survival", query="tab=mods")
        )

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_yuki_portal_base_url", return_value="https://wakusei.apasz.com"),
        ):
            response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(
            response.headers["location"],
            "https://wakusei.apasz.com/mod-web/nodes/erin/mods/minecraft_survival?tab=mods",
        )

    def test_remote_node_chat_page_redirects_to_yuki_portal_app_chat_page(self) -> None:
        server = replace(config.MOD_WEB_SERVER, node_name="erin")
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/mod-web/chat/minecraft_survival", query=""))

        with (
            patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE),
            patch.object(config, "MOD_WEB_SERVER", server),
            patch.object(ModWebService, "_yuki_portal_base_url", return_value="https://wakusei.apasz.com"),
        ):
            response = ModWebService()._remote_portal_redirect(cast(Any, request))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(
            response.headers["location"],
            "https://wakusei.apasz.com/mod-web/nodes/erin/chat/minecraft_survival",
        )

    def test_remote_node_api_is_not_redirected(self) -> None:
        request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/node/apps", query=""))

        with patch.object(config, "DATA_AUTHORITY_MODE", config.DataAuthorityMode.REMOTE):
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
            [(badge.text, badge.tone) for badge in details.badges],
            [
                ("Game <-> Chat", "grey"),
                ("1.20.4", "black"),
                ("12.0GiB", "grey"),
            ],
        )
        self.assertEqual(details.player_count_badge, _ModWebBadgeSpec(text="3 / 20", tone="purple"))
        self.assertEqual(details.status_text, "Running")
        self.assertEqual(details.status_tone, "purple")

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

    def test_builtin_mod_detection_uses_block_reason(self) -> None:
        builtin_entry = SimpleNamespace(mod_type=ModType.BUILTIN)
        normal_entry = SimpleNamespace(mod_type=ModType.SERVER_ONLY)

        self.assertTrue(ModWebService._is_builtin_mod(cast(Any, builtin_entry)))
        self.assertFalse(ModWebService._is_builtin_mod(cast(Any, normal_entry)))

    def test_selection_toggle_label_switches_between_select_all_and_clear(self) -> None:
        self.assertEqual(ModWebService._selection_toggle_label(selected_count=0), "Select All")
        self.assertEqual(ModWebService._selection_toggle_label(selected_count=2), "Clear")

    def test_mods_card_description_reflects_downloadable_inventory(self) -> None:
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
            "No mods are currently indexed for this app.",
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
            "Browse the indexed mod inventory and inspect file details.",
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
            "Browse the indexed mod inventory, inspect details, and download available files.",
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
            )
        )

        self.assertEqual(
            badges,
            (
                _ModWebBadgeSpec(text="4 mods", tone="black"),
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

    def test_hero_card_style_uses_app_color_when_available(self) -> None:
        self.assertEqual(
            ModWebService._hero_card_style("#22C55E"),
            "--mod-hero-border: #22C55E; --mod-hero-border-fade: var(--mod-border);",
        )
        self.assertEqual(ModWebService._hero_card_style(None), "")

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

        self.assertNotIn("https://unpkg.com", assets_html)
        self.assertIn("Leaflet", assets_html)

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

    def test_additional_app_tabs_can_be_conditionally_enabled_for_detail_pages(self) -> None:
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
                        visibility_rule=ModWebAppTabVisibilityRule.all_of(
                            ModWebAppTabVisibilityRule.min_app_version("1.20.1"),
                            ModWebAppTabVisibilityRule.has_mod("squaremap"),
                            ModWebAppTabVisibilityRule.setting_enabled("squaremap_enabled"),
                        ),
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
        settings = NodeSettingList(
            app_name="minecraft_alpha",
            app_friendly="Minecraft Alpha",
            node="yuki",
            editable_count=1,
            restricted_count=0,
            has_pending_changes=False,
            pending_change_count=0,
            required_save_level_name=Power_Level.user.name,
            required_reload_level_name=Power_Level.user.name,
            settings=(
                self._setting_entry(
                    key="squaremap_enabled",
                    label="Squaremap Enabled",
                    type_name="bool",
                    value_text="true",
                    current_input_value="true",
                ),
            ),
        )
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
            app_stats=NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version="1.20.4",
                player_count=0,
                player_capacity=20,
                relay_support=ChatRelaySupport.BIDIRECTIONAL,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.NONE,
            ),
            app_start_blocked=False,
            settings=settings,
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
                    downloadable_count=0,
                    non_downloadable_count=1,
                ),
                mods=(cast(Any, SimpleNamespace(name="squaremap")),),
                app_stats=None,
            ),
            download_all_url="/mods/download",
            download_enabled_url="/mods/download?enabled_only=true",
            mod_download_urls={},
        )

        tabs = service._page_tabs(model)

        self.assertEqual([tab.tab_id for tab in tabs], ["mods", "settings", "map"])
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
        class FakeContainer:
            class_value: str | None = None
            added_style: str | None = None
            removed_style: str | None = None

            def classes(self, value: str) -> "FakeContainer":
                self.class_value = value
                return self

            def style(self, *, add: str | None = None, remove: str | None = None) -> "FakeContainer":
                self.added_style = add
                self.removed_style = remove
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

            def row(self) -> FakeContainer:
                return FakeContainer()

            def element(self, tag: str) -> FakeContainer:
                del tag
                return FakeContainer()

            def tabs(self, *, value: str, on_change: object) -> FakeContainer:
                del value, on_change
                return FakeContainer()

            def tab(self, tab_id: str, *, label: str) -> object:
                del tab_id, label
                return object()

            def tab_panels(self, tabs: object, *, value: str, animated: bool) -> FakeContainer:
                del tabs, value, animated
                return FakeContainer()

            def tab_panel(self, tab: object) -> FakeContainer:
                del tab
                return FakeContainer()

        service = ModWebService()
        user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
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
            app_stats=None,
            popout_url="/mod-web/chat/minecraft_alpha",
            map_url="https://example.invalid/squaremap/?world=minecraft_overworld",
        )
        ui = FakeUi()
        tabs = service._page_tabs(model)

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
            )

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
        render_page_section.assert_called_once()

    def test_additional_app_tabs_stay_hidden_when_conditions_are_not_met(self) -> None:
        class HiddenTabService(ModWebService):
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
                        visibility_rule=ModWebAppTabVisibilityRule.all_of(
                            ModWebAppTabVisibilityRule.min_app_version("1.20.1"),
                            ModWebAppTabVisibilityRule.has_mod("squaremap"),
                            ModWebAppTabVisibilityRule.setting_enabled("squaremap_enabled"),
                        ),
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

        service = HiddenTabService()
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
            app_stats=NodeAppRuntimeSummary(
                running=True,
                enabled=True,
                version="1.20.0",
                player_count=0,
                player_capacity=20,
                relay_support=ChatRelaySupport.NONE,
                storage_percent=None,
                storage_free_bytes=None,
                storage_total_bytes=None,
                transition_state=NodeAppTransitionState.NONE,
            ),
            app_start_blocked=False,
            settings=None,
            console_actions=None,
        )

        self.assertEqual(service._page_tabs(model), ())

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

    def test_app_action_pending_feedback_messages_cover_start_stop_and_kill(self) -> None:
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.START), "Starting...")
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.STOP), "Stopping...")
        self.assertEqual(ModWebService._app_action_pending_label(NodeAppMutationAction.KILL), "Killing...")
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
