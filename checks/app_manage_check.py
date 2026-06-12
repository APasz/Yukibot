from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import hikari
from hikari_ui import EditorCustomIdCodec, InteractionDeferral
from hikari_ui.action import PagedActionCodec

import config
from _discord import App_Bound, DC_Relay
from _editor_session import EditorSessionNamespace
from _manager import App_Manager, AppDetailsUpdate, AppInstanceCreateRequest, AppInstanceTemplate, Provider_Player
from _relay_embeds import build_app_lifecycle_embed
from _security import Power_Level
from apps._app import AM_Receiver, App, AppRuntimeFault, AppRuntimeFaultKind, ChatRelaySupport, RelayAdvancementTerms
from apps._config import (
    APP_FRIENDLY_NAME_MAX_LENGTH,
    App_Config,
    AppVersion,
    Mod_Config,
    ModDownloadBlockReason,
    RelayChannelSource,
    normalise_app_version,
)
from apps._console import (
    ConsoleAction,
    ConsoleActionParameter,
    ConsoleActionResult,
    ConsoleResponseSource,
    execute_console_action,
)
from apps._mod import Mod
from apps._settings import ChoiceOption, ChoiceSpec
from apps.minecraft import Minecraft, Minecraft_Config, MinecraftLoader, MinecraftRuntimeInfo
from apps.sevendays import SevenDays
from chat_hub import ChatEndpoint, ChatEndpointId, ChatEndpointKind, ChatHub
from cmd_app import (
    AppConsoleActionKind,
    AppConsoleService,
    AppConsoleState,
    AppManageActionKind,
    AppManageCapability,
    AppManageMode,
    AppManageService,
    AppManageState,
    EditorStatus,
    _app_capabilities,
    _app_extra_capability_labels,
    _app_relay_lines,
    _app_started_response_text,
    _app_status_lines,
    _console_action_result_status_text,
    _console_action_status_lines_for_view,
    _console_state_from_value,
    _console_state_value,
    _default_relay_lines,
    _state_value,
)
from cmd_dashboard import DashboardEditorService
from relay_notices import MaintenanceNotice, MaintenanceStage, RelayNoticeSeverity, RelayNoticeSource
from restart_targets import RestartTarget


class _RunningProcess:
    def poll(self) -> None:
        return None


class _DummyReceiver(AM_Receiver):
    async def send(self, payload: App_Bound) -> None:
        return None


class _RecordingReceiver(AM_Receiver):
    def __init__(self) -> None:
        self.payloads: list[App_Bound] = []

    async def send(self, payload: App_Bound) -> None:
        self.payloads.append(payload)


class _DummyApp(App[App_Config]):
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


class _TestMod(Mod):
    async def install(self, src: Path, atomic: bool = True) -> None:
        del src, atomic


class _MissingFileApp(_DummyApp):
    def __init__(self, bot: hikari.GatewayBot, am: Any, cfg: App_Config) -> None:  # pyright: ignore[reportMissingSuperCall]
        raise FileNotFoundError("App_Settings file missing")


class _SlowStartApp(_DummyApp):
    start_entered: asyncio.Event | None = None
    release_start: asyncio.Event | None = None

    async def start(self) -> bool:
        if self.start_entered is None or self.release_start is None:
            raise RuntimeError("_SlowStartApp requires start synchronization events")
        self.start_entered.set()
        await self.release_start.wait()
        return True


def _build_dummy_app(
    *,
    chat_relay_outbound: bool = False,
    has_receiver: bool = False,
    join_host: str = "play.example.com",
    join_port: int | None = None,
    version: str | AppVersion | None = None,
) -> _DummyApp:
    resolved_version = normalise_app_version(version)
    app = object.__new__(_DummyApp)
    app.name = "dummy"
    app.friendly = "Dummy"
    app.scope = "dummy"
    app.directory = Path(".")
    app.file_instances = Path("instances.json")
    app.updater = None
    app.mods = None
    app.settings = None
    app.chat_channel = None
    app.chat_channels = ()
    app.chat_channel_override = None
    app.chat_channel_overrides = ()
    app.chat_channel_source = RelayChannelSource.NONE
    app.chat_relay_outbound = chat_relay_outbound
    app.am_receiver = _DummyReceiver() if has_receiver else None
    app.manage_embed_color = 0x96212B
    app.lifecycle_started_at = None
    app.runtime_fault = None
    app.cfg = App_Config(
        name="dummy",
        instance_key="alpha",
        friendly_name="Dummy",
        directory=Path("."),
        apps_dir=Path("."),
        scope="dummy",
        join_host=join_host,
        join_port=join_port,
        version=resolved_version,
    )
    app.file_instances = app.cfg.apps_dir / "instances.json"
    app.process = None
    return app


def _build_minecraft_app(
    *,
    relay_advancements: bool = True,
    minecraft_version: str | None = None,
    minecraft_loader: MinecraftLoader | None = None,
    minecraft_loader_version: str | None = None,
) -> Minecraft:
    app = object.__new__(Minecraft)
    app.name = "minecraft_alpha"
    app.friendly = "Minecraft Alpha"
    app.scope = "minecraft"
    app.directory = Path(".")
    app.file_instances = Path("instances.json")
    app.updater = None
    app.mods = None
    app.settings = None
    app.process = None
    app.chat_channel = None
    app.chat_channels = ()
    app.chat_channel_override = None
    app.chat_channel_overrides = ()
    app.chat_channel_source = RelayChannelSource.NONE
    app.am_receiver = None
    app.runtime_fault = None
    version = (
        AppVersion(
            main=minecraft_version,
            framework=minecraft_loader_version,
            loader=minecraft_loader.value if minecraft_loader is not None else None,
        )
        if minecraft_version is not None
        else None
    )
    app.cfg = Minecraft_Config(
        name=app.name,
        instance_key="alpha",
        friendly_name=app.friendly,
        directory=Path("."),
        apps_dir=Path("."),
        scope="minecraft",
        relay_advancements=relay_advancements,
        version=version,
    )
    app.file_instances = app.cfg.apps_dir / "instances.json"
    app._runtime = MinecraftRuntimeInfo(
        minecraft_version=minecraft_version,
        loader=minecraft_loader,
        loader_version=minecraft_loader_version,
    )
    return app


def _button_states(components: list[hikari.api.MessageActionRowBuilder]) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for row in components:
        for component in row.components:
            dynamic_component = cast(Any, component)
            label = getattr(dynamic_component, "label", None)
            if isinstance(label, str):
                states[label] = bool(getattr(dynamic_component, "is_disabled"))
    return states


def _channel_select_placeholders(components: list[hikari.api.MessageActionRowBuilder]) -> tuple[str, ...]:
    placeholders: list[str] = []
    for row in components:
        for component in row.components:
            placeholder = getattr(cast(Any, component), "placeholder", None)
            if isinstance(placeholder, str):
                placeholders.append(placeholder)
    return tuple(placeholders)


def _build_channel_resolution_bot(channel_guild_ids: dict[int, int]) -> Mock:
    bot = Mock()
    bot.cache.get_thread = Mock(return_value=None)
    bot.rest.fetch_channel = AsyncMock(side_effect=AssertionError("channel fetch should not be required in this test"))

    def _get_guild_channel(channel_id: hikari.Snowflakeish) -> object | None:
        guild_id = channel_guild_ids.get(int(hikari.Snowflake(channel_id)))
        if guild_id is None:
            return None
        return SimpleNamespace(guild_id=hikari.Snowflake(guild_id))

    bot.cache.get_guild_channel = Mock(side_effect=_get_guild_channel)
    return bot


class AppManageTests(unittest.TestCase):
    def test_editor_session_namespace_invalidates_custom_ids_from_previous_startup(self) -> None:
        older = EditorSessionNamespace(token="oldtoken")
        newer = EditorSessionNamespace(token="newtoken")
        old_codec = EditorCustomIdCodec(older.prefix("app-manage:"))
        new_codec = EditorCustomIdCodec(newer.prefix("app-manage:"))

        custom_id = old_codec.build("ta", scope_id=123, user_id=456)

        self.assertIsNone(new_codec.parse(custom_id))

    def test_app_manager_setting_state_custom_id_stays_within_limit(self) -> None:
        codec = EditorCustomIdCodec(EditorSessionNamespace(token="xy").prefix("app-manage:"))
        state = AppManageState(
            mode=AppManageMode.SETTING_CHOICES,
            page=0,
            app_name="minecraft_all_fabric",
            selected_page_slot=0,
            selected_setting_index=24,
        )

        custom_id = codec.build(
            PagedActionCodec(AppManageActionKind).build(
                AppManageActionKind.SAVE_SETTINGS,
                page=0,
                value=_state_value(state),
            ),
            scope_id=1234567890123456789,
            user_id=2234567890123456789,
        )

        self.assertLessEqual(len(custom_id), 100)

    def test_console_state_round_trips(self) -> None:
        state = AppConsoleState(page=3, app_name="minecraft_ermingham", selected_action_index=28)

        encoded = _console_state_value(state)

        self.assertEqual(_console_state_from_value(encoded, 3), state)

    def test_console_state_requires_app_name(self) -> None:
        self.assertIsNone(_console_state_from_value("~1", 0))

    def test_minecraft_exposes_curated_console_actions(self) -> None:
        app = object.__new__(Minecraft)

        action_keys = {action.key for action in app.console_actions}

        self.assertTrue(app.supports_console_actions)
        self.assertIn("raw_command", action_keys)
        self.assertIn("save_all", action_keys)
        self.assertIn("op", action_keys)
        self.assertIn("stop_server", action_keys)

    def test_sevendays_exposes_curated_console_actions(self) -> None:
        app = object.__new__(SevenDays)

        action_keys = {action.key for action in app.console_actions}

        self.assertTrue(app.supports_console_actions)
        self.assertIn("saveworld", action_keys)
        self.assertIn("settime", action_keys)
        self.assertIn("say", action_keys)
        self.assertIn("kick", action_keys)
        self.assertIn("admin_add", action_keys)
        self.assertIn("admin_remove", action_keys)
        self.assertIn("shutdown", action_keys)

    def test_console_action_view_hides_choice_preview_when_select_menu_exists(self) -> None:
        app = object.__new__(Minecraft)
        action = next(action for action in app.console_actions if action.key == "time_set")

        lines = _console_action_status_lines_for_view(action)

        self.assertFalse(any(line.startswith("choices:") for line in lines))
        self.assertFalse(any(line.startswith("input:") for line in lines))

    def test_console_action_result_status_text_includes_response_text(self) -> None:
        status_text = _console_action_result_status_text(
            ConsoleActionResult(
                summary="Minecraft Alpha: broadcast sent.",
                text="[Server] hi",
                source=ConsoleResponseSource.RCON,
            )
        )

        self.assertEqual(status_text, "Minecraft Alpha: broadcast sent.\nResult: [Server] hi")

    def test_console_action_result_status_text_prefixes_unsuccessful_results(self) -> None:
        status_text = _console_action_result_status_text(
            ConsoleActionResult(
                summary="Minecraft Alpha: command rejected.",
                success=False,
            )
        )

        self.assertEqual(status_text, "Error: Minecraft Alpha: command rejected.")

    def test_console_modal_schema_is_derived_from_action_parameter(self) -> None:
        service = AppConsoleService()
        parameter = ConsoleActionParameter[object](
            key="message",
            label="Broadcast Message",
            value_type=str,
            desc="Send text to every player.",
            max_length=64,
            multiline=True,
        )

        modal = service._build_value_modal(parameter)
        schema = modal.schema
        if schema is None:
            raise AssertionError("Expected modal schema")
        field = schema.fields[0]

        self.assertEqual(field.label, "Broadcast Message")
        self.assertEqual(field.max_length, 64)
        self.assertEqual(field.placeholder, "Send text to every player.")
        self.assertEqual(field.style, hikari.TextInputStyle.PARAGRAPH)

    def test_console_parameter_recent_inputs_track_recency(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="player",
            label="Player",
            value_type=str,
        )

        parameter.remember_input("Alice")
        parameter.remember_input("Bob")
        parameter.remember_input("Alice")

        self.assertEqual(parameter.recent_inputs, ("Alice", "Bob"))

    def test_console_parameter_recent_inputs_keep_last_twenty_five_values(self) -> None:
        parameter = ConsoleActionParameter[int](
            key="count",
            label="Count",
            value_type=int,
        )

        for value in range(30):
            parameter.remember_input(str(value))

        self.assertEqual(len(parameter.recent_inputs), 25)
        self.assertEqual(parameter.recent_inputs[0], "29")
        self.assertEqual(parameter.recent_inputs[-1], "5")

    def test_console_parameter_uses_choice_spec_labels_and_values(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="time",
            label="Time",
            value_type=str,
            choices=ChoiceSpec(ChoiceOption("day", "Day"), ChoiceOption("night", "Night")),
        )

        self.assertEqual(parameter.parse("Day"), "day")
        self.assertEqual(parameter.display_value("night"), "Night (night)")

        with self.assertRaisesRegex(ValueError, "must match provided choices"):
            parameter.parse("noon")

    def test_console_parameter_non_strict_choice_spec_allows_custom_values(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="time",
            label="Time",
            value_type=str,
            choices=ChoiceSpec(ChoiceOption("day", "Day"), ChoiceOption("night", "Night"), strict=False),
        )

        self.assertEqual(parameter.parse("Day"), "day")
        self.assertEqual(parameter.parse("1300"), "1300")

    def test_shared_execute_console_action_parses_input_and_tracks_recents(self) -> None:
        parameter = ConsoleActionParameter[str](
            key="player",
            label="Player",
            value_type=str,
        )
        execute = AsyncMock(return_value=ConsoleActionResult(summary="Minecraft Alpha: op requested."))
        action = ConsoleAction(
            key="op",
            label="Op",
            description="Grant operator status.",
            power_level=Power_Level.user,
            execute=execute,
            parameter=parameter,
        )
        app = SimpleNamespace(friendly="Minecraft Alpha")

        result = asyncio.run(
            execute_console_action(app=app, is_running=lambda: True, action=action, raw_value="Alice")
        )

        execute.assert_awaited_once_with(app, "Alice")
        self.assertEqual(parameter.recent_inputs, ("Alice",))
        self.assertEqual(result.summary, "Minecraft Alpha: op requested.")

    def test_console_modal_submit_defers_execute_actions(self) -> None:
        service = AppConsoleService()
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppConsoleActionKind.EXECUTE_ACTION,
                    page=0,
                    value=_console_state_value(
                        AppConsoleState(page=0, app_name="minecraft_alpha", selected_action_index=0)
                    ),
                )
            },
        )()

        deferral = service._defer_modal_submit(cast(Any, req), {})

        self.assertEqual(deferral, InteractionDeferral.update())

    def test_console_close_action_does_not_require_state(self) -> None:
        service = AppConsoleService()
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppConsoleActionKind.CLOSE,
                    page=0,
                )
            },
        )()

        response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        assert response is not None
        self.assertEqual(response.content, "App console closed.")

    def test_toggle_relay_advancements_action_updates_manager(self) -> None:
        service = AppManageService()
        app = _build_minecraft_app(relay_advancements=True)
        manager = Mock()
        manager.get.return_value = app
        manager.set_app_relay_advancements_enabled = Mock()
        manager.default_chat_channel = None
        acl = Mock()
        bot = Mock()
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.TOGGLE_RELAY_ADVANCEMENTS,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_app_relay_advancements_enabled.assert_called_once_with(app, False)
        assert response is not None

    def test_save_relay_channel_action_updates_only_current_guild_channels(self) -> None:
        service = AppManageService()
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        app.chat_channel_overrides = app.chat_channels
        app.chat_channel_override = app.chat_channels[0]
        app.chat_channel_source = RelayChannelSource.INSTANCE
        manager = Mock()
        manager.get.return_value = app
        manager.set_app_chat_channels = Mock()
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 202: 20, 303: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "values": ["303"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_app_chat_channels.assert_called_once_with(
            app,
            (hikari.Snowflake(202), hikari.Snowflake(303)),
        )
        assert response is not None
        self.assertEqual(response.content, "Dummy relay text channel for this guild set to <#303>.")

    def test_save_relay_channel_action_uses_default_without_creating_override(self) -> None:
        service = AppManageService()
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        app.chat_channel = app.chat_channels[0]
        app.chat_channel_overrides = ()
        app.chat_channel_override = None
        app.chat_channel_source = RelayChannelSource.DEFAULT
        manager = Mock()
        manager.get.return_value = app
        manager.set_app_chat_channels = Mock()
        manager.default_chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        manager.default_chat_channel = hikari.Snowflake(101)
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 202: 20})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "values": ["101"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_app_chat_channels.assert_called_once_with(app, ())
        assert response is not None
        self.assertEqual(response.content, "Dummy already uses the default relay channel for this guild.")

    def test_save_relay_channel_action_syncs_existing_voice_target(self) -> None:
        service = AppManageService()
        voice_target_service = Mock()
        current_target = config.VoiceTargetConfig(
            guild_id=hikari.Snowflake(10),
            voice_channel=hikari.Snowflake(404),
            primary_tts_channel=hikari.Snowflake(101),
            secondary_tts_channel=hikari.Snowflake(505),
            secondary_tts_listen_enabled=True,
            relay_tts_enabled=False,
        )
        voice_target_service.voice_target.return_value = current_target
        service.set_voice_target_service(cast(Any, voice_target_service))
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(101),)
        manager = Mock()
        manager.get.return_value = app
        manager.set_app_chat_channels = Mock()
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 303: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "values": ["303"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            asyncio.run(service._on_editor_action(cast(Any, req), {}))

        voice_target_service.set_voice_target_config.assert_called_once_with(
            hikari.Snowflake(10),
            voice_channel=hikari.Snowflake(404),
            primary_tts_channel=hikari.Snowflake(303),
            primary_tts_listen_enabled=True,
            secondary_tts_channel=hikari.Snowflake(505),
            secondary_tts_listen_enabled=True,
            relay_tts_enabled=False,
        )

    def test_save_relay_voice_channel_action_uses_current_guild_text_channel(self) -> None:
        service = AppManageService()
        voice_target_service = Mock()
        voice_target_service.voice_target.return_value = None
        service.set_voice_target_service(cast(Any, voice_target_service))
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(303), hikari.Snowflake(202))
        manager = Mock()
        manager.get.return_value = app
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({202: 20, 303: 10, 404: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_VOICE_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "values": ["404"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        voice_target_service.set_voice_target_config.assert_called_once_with(
            hikari.Snowflake(10),
            voice_channel=hikari.Snowflake(404),
            primary_tts_channel=hikari.Snowflake(303),
            primary_tts_listen_enabled=True,
            secondary_tts_channel=None,
            secondary_tts_listen_enabled=None,
            relay_tts_enabled=True,
        )
        assert response is not None
        self.assertEqual(response.content, "Relay voice channel for this guild set to <#404>.")

    def test_clear_relay_channel_action_removes_only_current_guild_channel(self) -> None:
        service = AppManageService()
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        app.chat_channel_overrides = app.chat_channels
        app.chat_channel_override = app.chat_channels[0]
        app.chat_channel_source = RelayChannelSource.INSTANCE
        manager = Mock()
        manager.get.return_value = app
        manager.set_app_chat_channels = Mock()
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 202: 20})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.CLEAR_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_app_chat_channels.assert_called_once_with(app, (hikari.Snowflake(202),))
        assert response is not None
        self.assertEqual(response.content, "Dummy relay text channel removed for this guild.")

    def test_clear_relay_channel_action_reports_inherited_default_without_override(self) -> None:
        service = AppManageService()
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(101),)
        app.chat_channel = app.chat_channels[0]
        app.chat_channel_overrides = ()
        app.chat_channel_override = None
        app.chat_channel_source = RelayChannelSource.DEFAULT
        manager = Mock()
        manager.get.return_value = app
        manager.set_app_chat_channels = Mock()
        manager.default_chat_channels = (hikari.Snowflake(101),)
        manager.default_chat_channel = hikari.Snowflake(101)
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.CLEAR_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name)),
                ),
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_app_chat_channels.assert_called_once_with(app, ())
        assert response is not None
        self.assertEqual(
            response.content,
            "Dummy has no relay override configured for this guild. The default relay channel still applies here.",
        )

    def test_save_default_relay_channel_action_updates_only_current_guild_channels(self) -> None:
        service = AppManageService()
        manager = Mock()
        manager.default_chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        manager.default_chat_channel = hikari.Snowflake(101)
        manager.set_default_chat_channels = Mock()
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 202: 20, 303: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0)),
                ),
                "values": ["303"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_default_chat_channels.assert_called_once_with(
            (hikari.Snowflake(202), hikari.Snowflake(303)),
        )
        assert response is not None
        self.assertEqual(response.content, "Default relay text channel for this guild set to <#303>.")

    def test_save_default_relay_channel_action_syncs_matching_voice_target(self) -> None:
        service = AppManageService()
        voice_target_service = Mock()
        voice_target_service.voice_target.return_value = config.VoiceTargetConfig(
            guild_id=hikari.Snowflake(10),
            voice_channel=hikari.Snowflake(404),
            primary_tts_channel=hikari.Snowflake(101),
            relay_tts_enabled=True,
        )
        service.set_voice_target_service(cast(Any, voice_target_service))
        manager = Mock()
        manager.default_chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        manager.default_chat_channel = hikari.Snowflake(101)
        manager.set_default_chat_channels = Mock()
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 202: 20, 303: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0)),
                ),
                "values": ["303"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            asyncio.run(service._on_editor_action(cast(Any, req), {}))

        voice_target_service.set_voice_target_config.assert_called_once_with(
            hikari.Snowflake(10),
            voice_channel=hikari.Snowflake(404),
            primary_tts_channel=hikari.Snowflake(303),
            primary_tts_listen_enabled=True,
            secondary_tts_channel=None,
            secondary_tts_listen_enabled=False,
            relay_tts_enabled=True,
        )

    def test_save_default_relay_voice_channel_action_uses_current_guild_text_channel(self) -> None:
        service = AppManageService()
        voice_target_service = Mock()
        voice_target_service.voice_target.return_value = None
        service.set_voice_target_service(cast(Any, voice_target_service))
        manager = Mock()
        manager.default_chat_channels = (hikari.Snowflake(303), hikari.Snowflake(202))
        manager.default_chat_channel = hikari.Snowflake(303)
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({202: 20, 303: 10, 404: 10})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.SAVE_RELAY_VOICE_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0)),
                ),
                "values": ["404"],
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        voice_target_service.set_voice_target_config.assert_called_once_with(
            hikari.Snowflake(10),
            voice_channel=hikari.Snowflake(404),
            primary_tts_channel=hikari.Snowflake(303),
            primary_tts_listen_enabled=True,
            secondary_tts_channel=None,
            secondary_tts_listen_enabled=None,
            relay_tts_enabled=True,
        )
        assert response is not None
        self.assertEqual(response.content, "Default relay voice channel for this guild set to <#404>.")

    def test_clear_default_relay_channel_action_removes_only_current_guild_channel(self) -> None:
        service = AppManageService()
        manager = Mock()
        manager.default_chat_channels = (hikari.Snowflake(101), hikari.Snowflake(202))
        manager.default_chat_channel = hikari.Snowflake(101)
        manager.set_default_chat_channels = Mock()
        acl = Mock()
        acl.can.return_value = True
        bot = _build_channel_resolution_bot({101: 10, 202: 20})
        req = type(
            "_Req",
            (),
            {
                "action": service._action_codec.build(
                    AppManageActionKind.CLEAR_RELAY_CHANNEL,
                    page=0,
                    value=_state_value(AppManageState(mode=AppManageMode.RELAY, page=0)),
                ),
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
                "interaction": Mock(message=None, guild_id=hikari.Snowflake(10)),
            },
        )()

        with (
            patch.object(AppManageService, "_require_manager", return_value=cast(Any, manager)),
            patch.object(AppManageService, "_require_acl", return_value=cast(Any, acl)),
            patch.object(AppManageService, "_require_bot", return_value=cast(Any, bot)),
        ):
            response = asyncio.run(service._on_editor_action(cast(Any, req), {}))

        manager.set_default_chat_channels.assert_called_once_with((hikari.Snowflake(202),))
        assert response is not None
        self.assertEqual(response.content, "Default relay text channel removed for this guild.")

    def test_manager_toggle_updates_instances_json(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Dummy", "directory": "{APPS}/dummy", "enabled": False}}),
                encoding="utf-8",
            )
            app.name = "dummy_alpha"
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name="Dummy",
                directory=temp_path / "dummy",
                apps_dir=temp_path,
                scope="dummy",
                enabled=False,
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"
            manager.apps = {app.name: app}
            manager._lookup = {app.name: app.name, app.name.lower(): app.name}

            with patch.object(config, "ENABLED_DUMP_FILE", temp_path / "enabled_apps.txt"):
                manager.toggle(app.name, True)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertTrue(app.cfg.enabled)
            self.assertTrue(payload["alpha"]["enabled"])

    def test_chat_relay_support_is_inbound_when_receiver_is_present(self) -> None:
        app = _build_dummy_app(has_receiver=True)

        self.assertIs(app.chat_relay_support, ChatRelaySupport.INBOUND)
        self.assertTrue(app.supports_chat_relay)
        self.assertTrue(app.supports_inbound_chat_relay)
        self.assertTrue(app.supports_relay_system_notices)
        self.assertFalse(app.supports_outbound_chat_relay)

    def test_chat_relay_support_is_outbound_when_app_emits_without_receiver(self) -> None:
        app = _build_dummy_app(chat_relay_outbound=True)

        self.assertIs(app.chat_relay_support, ChatRelaySupport.OUTBOUND)
        self.assertTrue(app.supports_chat_relay)
        self.assertFalse(app.supports_inbound_chat_relay)
        self.assertTrue(app.supports_outbound_chat_relay)

    def test_chat_relay_support_is_bidirectional_when_app_supports_both(self) -> None:
        app = _build_dummy_app(chat_relay_outbound=True, has_receiver=True)

        self.assertIs(app.chat_relay_support, ChatRelaySupport.BIDIRECTIONAL)

    def test_minecraft_supports_advancement_relay_toggle(self) -> None:
        app = _build_minecraft_app(relay_advancements=True)

        self.assertTrue(app.supports_relay_advancements)
        self.assertEqual(app.relay_advancements_enabled, True)

    def test_app_status_lines_fall_back_to_version(self) -> None:
        app = _build_dummy_app(version="1.2.3")

        self.assertEqual(
            _app_status_lines(app),
            (
                "scope: dummy",
                "version: 1.2.3",
            ),
        )

    def test_app_capabilities_surface_inbound_chat_label(self) -> None:
        app = _build_dummy_app(has_receiver=True)

        capabilities = _app_capabilities(app)
        labels = _app_extra_capability_labels(app)

        self.assertIn(AppManageCapability.CHAT, capabilities)
        self.assertIn("Chat -> Game", labels)
        self.assertNotIn("Toggle", labels)

    def test_app_relay_lines_display_current_and_default_voice_channels(self) -> None:
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(123), hikari.Snowflake(456))
        app.chat_channel = app.chat_channels[0]
        app.chat_channel_overrides = app.chat_channels
        app.chat_channel_override = app.chat_channel
        manager = Mock()
        manager.bot = _build_channel_resolution_bot({123: 10, 456: 20, 789: 10, 987: 30})
        manager.default_chat_channels = (hikari.Snowflake(789), hikari.Snowflake(987))
        manager.default_chat_channel = hikari.Snowflake(789)
        voice_target_service = Mock()
        voice_target_service.voice_targets.return_value = {
            hikari.Snowflake(10): config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(10),
                voice_channel=hikari.Snowflake(901),
                primary_tts_channel=hikari.Snowflake(123),
                relay_tts_enabled=True,
            ),
            hikari.Snowflake(30): config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(30),
                voice_channel=hikari.Snowflake(902),
                primary_tts_channel=hikari.Snowflake(987),
                relay_tts_enabled=True,
            ),
        }

        lines = _app_relay_lines(
            app,
            manager,
            current_guild_id=hikari.Snowflake(10),
            voice_target_service=cast(Any, voice_target_service),
        )

        self.assertEqual(
            lines,
            (
                "Support: Chat -> Game",
                "Text: <#123> | <#456>",
                "Voice: <#901> | unset",
                "Default: <#789> | unset",
            ),
        )

    def test_default_relay_lines_display_current_and_other_voice_channels(self) -> None:
        manager = Mock()
        manager.bot = _build_channel_resolution_bot({123: 10, 456: 20})
        manager.default_chat_channels = (hikari.Snowflake(123), hikari.Snowflake(456))
        manager.default_chat_channel = hikari.Snowflake(123)
        voice_target_service = Mock()
        voice_target_service.voice_targets.return_value = {
            hikari.Snowflake(10): config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(10),
                voice_channel=hikari.Snowflake(900),
                primary_tts_channel=hikari.Snowflake(123),
                relay_tts_enabled=True,
            ),
            hikari.Snowflake(20): config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(20),
                voice_channel=hikari.Snowflake(901),
                primary_tts_channel=hikari.Snowflake(456),
                relay_tts_enabled=True,
            ),
        }

        lines = _default_relay_lines(
            manager,
            current_guild_id=hikari.Snowflake(10),
            voice_target_service=cast(Any, voice_target_service),
        )

        self.assertEqual(lines, ("Text: <#123> | <#456>", "Voice: <#900> | <#901>"))

    def test_started_message_includes_join_address(self) -> None:
        app = _build_dummy_app(join_port=25565)

        self.assertEqual(_app_started_response_text(app), "Dummy Started!\nJoin: `play.example.com:25565`")

    def test_started_message_includes_public_ip_fallback_for_default_public_addr(self) -> None:
        with (
            patch.object(config, "PUBLIC_ADDR", "wakusei.apasz.com"),
            patch.object(config, "PUBLIC_IP", "203.0.113.10"),
        ):
            app = _build_dummy_app(join_host="wakusei.apasz.com", join_port=25565)
            self.assertEqual(
                _app_started_response_text(app),
                "Dummy Started!\nJoin: `wakusei.apasz.com:25565 [203.0.113.10:25565]`",
            )

    def test_app_lifecycle_embed_includes_join_address_for_start(self) -> None:
        app = _build_dummy_app(join_port=25565)

        embed = build_app_lifecycle_embed(app, started=True)

        self.assertEqual(embed.title, "Dummy Started")
        self.assertEqual(embed.description, "Join: `play.example.com:25565`")
        self.assertEqual(embed.color, app.manage_embed_color)

    def test_app_lifecycle_embed_includes_uptime_for_stop(self) -> None:
        app = _build_dummy_app(join_port=25565)

        embed = build_app_lifecycle_embed(app, started=False, uptime=timedelta(hours=1, minutes=2, seconds=3))

        self.assertEqual(embed.title, "Dummy Ended")
        self.assertEqual(
            embed.description,
            "Uptime: `1h 2m 3s`",
        )

    def test_dashboard_services_include_join_address_for_running_app(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app(join_port=25565)
        app.process = cast(Any, _RunningProcess())
        manager.activity_manager = None
        manager.apps = {app.name: app}
        manager.current = app.name

        lines = DashboardEditorService._service_lines(manager)

        self.assertIn("join address: play.example.com:25565", lines)

    def test_manager_rejects_chat_channel_updates_for_unsupported_apps(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()

        with self.assertRaisesRegex(ValueError, "does not support chat relay"):
            manager.set_app_chat_channel(app, hikari.Snowflake(123))

    def test_bind_app_channel_skips_unsupported_apps(self) -> None:
        app = _build_dummy_app()
        app.chat_channel = hikari.Snowflake(456)
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertNotIn(app.chat_channel, DC_Relay._chat_channels)
        finally:
            DC_Relay._chat_channels.clear()

    def test_bind_app_channel_skips_outbound_only_apps(self) -> None:
        app = _build_dummy_app(chat_relay_outbound=True)
        app.chat_channel = hikari.Snowflake(789)
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertNotIn(app.chat_channel, DC_Relay._chat_channels)
        finally:
            DC_Relay._chat_channels.clear()

    def test_bind_app_channel_registers_inbound_apps(self) -> None:
        app = _build_dummy_app(has_receiver=True)
        app.chat_channel = hikari.Snowflake(789)
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertEqual(DC_Relay._chat_channels[app.chat_channel], {cast(App[App_Config], app)})
        finally:
            DC_Relay._chat_channels.clear()

    def test_bind_app_channel_registers_multiple_discord_endpoints(self) -> None:
        app = _build_dummy_app(has_receiver=True)
        app.chat_channels = (hikari.Snowflake(789), hikari.Snowflake(987))
        app.chat_channel = app.chat_channels[0]
        hub = ChatHub()
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertEqual(DC_Relay._chat_channels[hikari.Snowflake(789)], {cast(App[App_Config], app)})
            self.assertEqual(DC_Relay._chat_channels[hikari.Snowflake(987)], {cast(App[App_Config], app)})
            endpoints = hub.endpoints_for_room(app.name)
            endpoint_kinds = {endpoint.id.kind for endpoint in endpoints}
            discord_channel_ids = {
                endpoint.id.value for endpoint in endpoints if endpoint.id.kind is ChatEndpointKind.DISCORD_CHANNEL
            }
            self.assertIn(ChatEndpointKind.APP, endpoint_kinds)
            self.assertEqual(discord_channel_ids, {"789", "987"})
        finally:
            DC_Relay._chat_channels.clear()
            hub.clear_room(app.name)

    def test_manager_updates_multiple_chat_channels_in_instances_json(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app(has_receiver=True)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Dummy", "directory": "{APPS}/dummy"}}),
                encoding="utf-8",
            )
            app.name = "dummy_alpha"
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name="Dummy",
                directory=temp_path / "dummy",
                apps_dir=temp_path,
                scope="dummy",
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"

            try:
                manager.set_app_chat_channels(app, (hikari.Snowflake(123), hikari.Snowflake(456)))
            finally:
                DC_Relay._chat_channels.clear()
                ChatHub().clear_room(app.name)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["alpha"]["chat_channels"], ["123", "456"])
            self.assertNotIn("chat_channel", payload["alpha"])
            self.assertEqual(app.cfg.chat_channels, ("123", "456"))
            self.assertEqual(app.cfg.chat_channel, "123")
            self.assertEqual(app.chat_channels, (hikari.Snowflake(123), hikari.Snowflake(456)))
            self.assertEqual(app.chat_channel, hikari.Snowflake(123))

    def test_manager_rejects_app_chat_channel_conflicting_with_default_channel(self) -> None:
        manager = object.__new__(App_Manager)
        manager.default_chat_channels = (hikari.Snowflake(123),)
        manager.default_chat_channel = hikari.Snowflake(123)
        manager.default_chat_channel_source = RelayChannelSource.DEFAULT
        app = _build_dummy_app(has_receiver=True)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Dummy", "directory": "{APPS}/dummy"}}),
                encoding="utf-8",
            )
            app.name = "dummy_alpha"
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name="Dummy",
                directory=temp_path / "dummy",
                apps_dir=temp_path,
                scope="dummy",
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"

            with self.assertRaises(ValueError):
                manager.set_app_chat_channels(app, (hikari.Snowflake(123),))

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertNotIn("chat_channel", payload["alpha"])
            self.assertNotIn("chat_channels", payload["alpha"])

    def test_manager_updates_multiple_default_chat_channels_in_configuration_json(self) -> None:
        manager = object.__new__(App_Manager)
        manager.apps = {}
        manager.default_chat_channels = ()
        manager.default_chat_channel = None
        manager.default_chat_channel_source = RelayChannelSource.NONE

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "configuration.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            original_cwd = Path.cwd()

            os.chdir(temp_path)
            try:
                manager.set_default_chat_channels((hikari.Snowflake(123), hikari.Snowflake(456)))
            finally:
                os.chdir(original_cwd)

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["default_chat_channels"], ["123", "456"])
            self.assertNotIn("default_chat_channel", payload)
            self.assertEqual(manager.default_chat_channels, (hikari.Snowflake(123), hikari.Snowflake(456)))
            self.assertEqual(manager.default_chat_channel, hikari.Snowflake(123))

    def test_manager_default_chat_channels_remove_conflicting_app_overrides(self) -> None:
        manager = object.__new__(App_Manager)
        manager.default_chat_channels = ()
        manager.default_chat_channel = None
        manager.default_chat_channel_source = RelayChannelSource.NONE
        app = _build_dummy_app(has_receiver=True)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "configuration.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "friendly_name": "Dummy",
                            "directory": "{APPS}/dummy",
                            "chat_channels": ["123", "456"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            app.name = "dummy_alpha"
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name="Dummy",
                directory=temp_path / "dummy",
                apps_dir=temp_path,
                scope="dummy",
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"
            manager.apps = {app.name: cast(App[App_Config], app)}
            original_cwd = Path.cwd()

            os.chdir(temp_path)
            try:
                manager.set_default_chat_channels((hikari.Snowflake(123), hikari.Snowflake(789)))
            finally:
                os.chdir(original_cwd)
                DC_Relay._chat_channels.clear()
                ChatHub().clear_room(app.name)

            config_payload = json.loads(config_path.read_text(encoding="utf-8"))
            instance_payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(config_payload["default_chat_channels"], ["123", "789"])
            self.assertEqual(instance_payload["alpha"]["chat_channel"], "456")
            self.assertNotIn("chat_channels", instance_payload["alpha"])
            self.assertEqual(app.chat_channels, (hikari.Snowflake(456),))
            self.assertIs(app.chat_channel_source, RelayChannelSource.INSTANCE)

    def test_manager_updates_minecraft_advancement_relay_in_instances_json(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_minecraft_app(relay_advancements=True)

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "friendly_name": "Minecraft Alpha",
                            "directory": "{APPS}/mc",
                            "relay_advancements": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            app.cfg = Minecraft_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=temp_path / "mc",
                apps_dir=temp_path,
                scope="minecraft",
                relay_advancements=True,
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"
            managed_app = cast(App[App_Config], app)
            manager.apps = {app.name: managed_app}
            manager._lookup = {app.name: app.name, app.name.lower(): app.name}

            manager.set_app_relay_advancements_enabled(managed_app, False)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(app.relay_advancements_enabled, False)
            self.assertEqual(payload["alpha"]["relay_advancements"], False)

    def test_manager_persists_generic_version_in_instances_json(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app(version="1.2.3")

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Dummy", "directory": "{APPS}/dummy"}}),
                encoding="utf-8",
            )
            app.cfg = App_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=temp_path / "dummy",
                apps_dir=temp_path,
                scope="dummy",
                version=AppVersion(main="1.2.3"),
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"

            manager._sync_app_instance_config(cast(App[App_Config], app))

            payload = json.loads(instances_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["alpha"]["version"], {"main": "1.2.3"})

    def test_manager_persists_minecraft_runtime_version_in_instances_json(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_minecraft_app(
            minecraft_version="1.20.1",
            minecraft_loader=MinecraftLoader.FORGE,
            minecraft_loader_version="47.4.0",
        )

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Minecraft Alpha", "directory": "{APPS}/mc"}}),
                encoding="utf-8",
            )
            app.cfg = Minecraft_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=temp_path / "mc",
                apps_dir=temp_path,
                scope="minecraft",
                version=AppVersion(main="1.20.1", framework="47.4.0", loader="forge"),
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"
            app._runtime = MinecraftRuntimeInfo("1.20.1", MinecraftLoader.FORGE, "47.4.0")

            manager._sync_app_instance_config(cast(App[App_Config], app))

            payload = json.loads(instances_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["alpha"]["version"], {"main": "1.20.1", "framework": "47.4.0", "loader": "forge"})

    def test_app_status_lines_show_detected_minecraft_runtime(self) -> None:
        app = _build_minecraft_app(
            minecraft_version="1.20.1",
            minecraft_loader=MinecraftLoader.FORGE,
            minecraft_loader_version="47.4.0",
        )

        self.assertEqual(
            _app_status_lines(app),
            (
                "scope: minecraft",
                "version: 1.20.1",
                "loader: Forge 47.4.0",
            ),
        )

    def test_relay_view_exposes_advancement_toggle_for_minecraft(self) -> None:
        service = AppManageService()
        app = _build_minecraft_app(relay_advancements=True)
        manager = Mock()
        manager.get.return_value = app
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True

        _embed, components = service._render_editor(
            actor_user_id=1,
            locale=hikari.Locale.EN_US,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name),
            status="Opened relay manager.",
        )

        buttons = _button_states(components)

        self.assertIn("Disable Advancements", buttons)
        self.assertFalse(buttons["Disable Advancements"])

    def test_landing_view_uses_explicit_error_status_for_title(self) -> None:
        service = AppManageService()
        manager = Mock()
        manager.apps = {}
        manager.default_chat_channels = ()
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True

        embed, _components = service._render_editor(
            actor_user_id=1,
            locale=hikari.Locale.EN_US,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.LANDING, page=0),
            status=EditorStatus(text="Could not load apps.", is_error=True),
        )

        self.assertIsNotNone(embed)
        assert embed is not None
        self.assertEqual(embed.title, "Error | App Manager")

    def test_relay_view_uses_app_specific_advancement_term_labels(self) -> None:
        service = AppManageService()
        app = _build_minecraft_app(relay_advancements=True)
        app.relay_advancement_terms = RelayAdvancementTerms("Research", "Research")
        manager = Mock()
        manager.get.return_value = app
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True

        _embed, components = service._render_editor(
            actor_user_id=1,
            locale=hikari.Locale.EN_US,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name),
            status="Opened relay manager.",
        )

        buttons = _button_states(components)

        self.assertIn("Disable Research", buttons)
        self.assertFalse(buttons["Disable Research"])

    def test_relay_view_uses_single_text_and_voice_selects_for_current_guild(self) -> None:
        service = AppManageService()
        service.set_voice_target_service(cast(Any, Mock()))
        app = _build_dummy_app(has_receiver=True)
        manager = Mock()
        manager.get.return_value = app
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True

        _embed, components = service._render_editor(
            actor_user_id=1,
            locale=hikari.Locale.EN_US,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.RELAY, page=0, app_name=app.name),
            status="Opened relay manager.",
            current_guild_id=hikari.Snowflake(123),
        )

        placeholders = _channel_select_placeholders(components)
        buttons = _button_states(components)

        self.assertIn("Choose relay text channel for Dummy", placeholders)
        self.assertIn("Choose relay voice channel for Dummy", placeholders)
        self.assertIn("Remove This Guild Relay", buttons)

    def test_default_relay_view_uses_single_text_and_voice_selects_for_current_guild(self) -> None:
        service = AppManageService()
        service.set_voice_target_service(cast(Any, Mock()))
        manager = Mock()
        manager.default_chat_channels = ()
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True

        _embed, components = service._render_editor(
            actor_user_id=1,
            locale=hikari.Locale.EN_US,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.RELAY, page=0),
            status="Opened relay manager.",
            current_guild_id=hikari.Snowflake(123),
        )

        placeholders = _channel_select_placeholders(components)
        buttons = _button_states(components)

        self.assertIn("Choose default relay text channel for this server", placeholders)
        self.assertIn("Choose default relay voice channel for this server", placeholders)
        self.assertIn("Remove This Guild Default", buttons)

    def test_manage_lock_reason_is_bypassed_with_active_lock(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        message_id = hikari.Snowflake(123)
        app.check_running = Mock(return_value=True)

        service._touch_app_lock(message_id=message_id, user_id=hikari.Snowflake(456), app_name=app.name)

        self.assertIsNone(service.manage_lock_reason(app))

    def test_start_lock_reason_is_bypassed_while_editor_session_is_active(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        message_id = hikari.Snowflake(123)

        service._touch_app_lock(message_id=message_id, user_id=hikari.Snowflake(456), app_name=app.name)

        self.assertIsNone(service.start_lock_reason(app))

    def test_home_view_keeps_management_buttons_enabled_while_running(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        app.scope = "dummy"
        app.mods = cast(Any, Mock())
        app.settings = cast(Any, Mock())
        app.updater = Mock(version=None)
        app.check_running = Mock(return_value=True)
        manager = Mock()
        manager.get.return_value = app
        manager.default_chat_channel = None
        acl = Mock()
        acl.can.return_value = True

        _embed, components = service._render_editor(
            actor_user_id=1,
            locale=hikari.Locale.EN_US,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.HOME, page=0, app_name=app.name),
            status="Opened Dummy.",
        )

        buttons = _button_states(components)

        self.assertFalse(buttons["Disable App"])
        self.assertFalse(buttons["Download"])
        self.assertFalse(buttons["Update"])
        self.assertFalse(buttons["Manage Mods"])
        self.assertFalse(buttons["Manage Settings"])

    def test_mods_view_exposes_web_button(self) -> None:
        service = AppManageService()
        with TemporaryDirectory() as temp_dir:
            mods_path = Path(temp_dir)
            mod = _TestMod(Mod_Config(name="example.jar", directory=mods_path))
            mod_manager = Mock()
            mod_manager.list_mods.return_value = [mod]

            app = _build_dummy_app()
            app.mods = cast(Any, mod_manager)
            app.scope = "dummy"
            manager = Mock()
            manager.get.return_value = app
            acl = Mock()
            acl.can.return_value = True

            _embed, components = service._render_editor(
                actor_user_id=1,
                locale=hikari.Locale.EN_US,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.MODS, page=0, app_name=app.name),
                status="Opened mods.",
            )

        buttons = _button_states(components)

        self.assertIn("Web", buttons)
        self.assertFalse(buttons["Web"])

    def test_build_mods_view_counts_builtin_mods_as_coremods(self) -> None:
        service = AppManageService()
        with TemporaryDirectory() as temp_dir:
            mods_path = Path(temp_dir)
            builtin_mod = _TestMod(
                Mod_Config(
                    name="builtin.jar",
                    directory=mods_path,
                    download_block_reason=ModDownloadBlockReason.BUILTIN,
                )
            )
            regular_mod = _TestMod(Mod_Config(name="regular.jar", directory=mods_path))
            mod_manager = Mock()
            mod_manager.list_mods.return_value = [builtin_mod, regular_mod]

            app = _build_dummy_app()
            app.mods = cast(Any, mod_manager)

            view = service._build_mods_view(app=app, state=AppManageState(mode=AppManageMode.MODS, page=0))

        self.assertEqual(view.coremod_count, 1)

    def test_mods_view_disables_coremod_toggle_for_builtin_mod(self) -> None:
        service = AppManageService()
        with TemporaryDirectory() as temp_dir:
            mods_path = Path(temp_dir)
            builtin_mod = _TestMod(
                Mod_Config(
                    name="builtin.jar",
                    directory=mods_path,
                    download_block_reason=ModDownloadBlockReason.BUILTIN,
                )
            )
            mod_manager = Mock()
            mod_manager.list_mods.return_value = [builtin_mod]

            app = _build_dummy_app()
            app.mods = cast(Any, mod_manager)
            app.scope = "dummy"
            manager = Mock()
            manager.get.return_value = app
            acl = Mock()
            acl.can.return_value = True

            _embed, components = service._render_editor(
                actor_user_id=1,
                locale=hikari.Locale.EN_US,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.MODS, page=0, app_name=app.name, selected_page_slot=0),
                status="Opened mods.",
            )

        buttons = _button_states(components)
        self.assertTrue(buttons["Set Coremod"])

    def test_handle_mod_web_action_updates_status_with_page_url(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        interaction = Mock()
        interaction.message = None
        interaction.create_initial_response = AsyncMock()
        req = type(
            "_Req",
            (),
            {
                "interaction": interaction,
                "user_id": 123,
                "locale": hikari.Locale.EN_US,
            },
        )()
        manager = Mock()
        acl = Mock()

        service._edit_editor_message = AsyncMock()
        service._mod_web.open_mod_page = AsyncMock(return_value="https://mods.example/mod-web/mods/dummy")

        asyncio.run(
            service._handle_mod_web_action(
                req=cast(Any, req),
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.MODS, page=0, app_name=app.name),
                app=app,
            )
        )

        interaction.create_initial_response.assert_awaited_once_with(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
        service._mod_web.open_mod_page.assert_awaited_once_with(app)
        service._edit_editor_message.assert_awaited_once()
        await_args = service._edit_editor_message.await_args
        assert await_args is not None
        status = await_args.kwargs["status"]
        self.assertIn("Opened mod web page for `Dummy`.", status)
        self.assertIn("https://mods.example/mod-web/mods/dummy", status)

    def test_touch_app_lock_preserves_manager_metadata_across_refreshes(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        message_id = hikari.Snowflake(123)
        channel_id = hikari.Snowflake(789)
        guild_id = hikari.Snowflake(456)
        application_id = hikari.Snowflake(321)
        expires_at = service._now() + timedelta(minutes=5)

        service._touch_app_lock(
            message_id=message_id,
            user_id=hikari.Snowflake(654),
            app_name=app.name,
            channel_id=channel_id,
            guild_id=guild_id,
            application_id=application_id,
            interaction_token="token",
            response_expires_at=expires_at,
        )
        service._touch_app_lock(
            message_id=message_id,
            user_id=hikari.Snowflake(654),
            app_name=app.name,
        )

        lock = service.start_lock(app)

        self.assertIsNotNone(lock)
        assert lock is not None
        self.assertEqual(lock.channel_id, channel_id)
        self.assertEqual(lock.guild_id, guild_id)
        self.assertEqual(lock.application_id, application_id)
        self.assertEqual(lock.interaction_token, "token")
        self.assertEqual(lock.response_expires_at, expires_at)

    def test_build_start_lock_response_includes_manager_and_location(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        message_id = hikari.Snowflake(123)
        user_id = hikari.Snowflake(456)
        channel_id = hikari.Snowflake(789)
        lock_expires_at = service._now() + timedelta(minutes=5)

        service._touch_app_lock(
            message_id=message_id,
            user_id=user_id,
            app_name=app.name,
            channel_id=channel_id,
            application_id=hikari.Snowflake(999),
            interaction_token="token",
            response_expires_at=lock_expires_at,
        )
        lock = service.start_lock(app)
        assert lock is not None

        content, components, is_ephemeral = service.build_start_lock_response(
            actor_user_id=999,
            locale=hikari.Locale.EN_US,
            app=app,
            lock=lock,
        )

        self.assertIn(f"<@{int(user_id)}>", content)
        self.assertIn(f"<#{int(channel_id)}>", content)
        self.assertTrue(is_ephemeral)
        self.assertTrue(components)

    def test_build_start_lock_response_is_regular_when_manager_cannot_be_closed(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        message_id = hikari.Snowflake(123)

        service._touch_app_lock(
            message_id=message_id,
            user_id=hikari.Snowflake(456),
            app_name=app.name,
            channel_id=hikari.Snowflake(789),
        )
        lock = service.start_lock(app)
        assert lock is not None

        _content, _components, is_ephemeral = service.build_start_lock_response(
            actor_user_id=999,
            locale=hikari.Locale.EN_US,
            app=app,
            lock=lock,
        )

        self.assertFalse(is_ephemeral)


class AppManageAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_apps_indexes_current_app_by_full_app_name(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.apps = {}
        manager._lookup = {}
        manager.default_chat_channel = None
        manager.default_chat_channel_source = RelayChannelSource.NONE
        manager.startup_disabled_instances = []
        manager.activity_manager = cast(Any, object())

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "minecraft"
            app_dir = temp_path / "mc_erm"
            scope_path.mkdir()
            app_dir.mkdir()
            (scope_path / "instances.json").write_text(
                json.dumps(
                    {
                        "ermingham": {
                            "friendly_name": "Ermingham",
                            "directory": str(app_dir),
                        }
                    }
                ),
                encoding="utf-8",
            )

            def instantiate_dummy(
                *,
                bot: hikari.GatewayBot,
                app_cls: type[App[App_Config]],
                cfg: App_Config,
            ) -> App[App_Config]:
                app = _build_dummy_app()
                app.name = cfg.name
                app.friendly = cfg.friendly_name or cfg.name
                app.cfg = cfg
                app.file_instances = cfg.apps_dir / "instances.json"
                app.proc_name = "java"
                app.directory = cfg.directory
                return app

            with (
                patch("_manager.Path.iterdir", return_value=iter((scope_path,))),
                patch.object(App_Manager, "_load_scope_types", return_value=(_DummyApp, App_Config)),
                patch.object(App_Manager, "_instantiate_app", side_effect=instantiate_dummy),
                patch.object(DC_Relay, "unregister_app"),
                patch.object(DC_Relay, "bind_app_channel"),
            ):
                await manager.load_apps(cast(Any, object()))

        manager.current = "minecraft_ermingham"

        current = manager.get_current

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.name, "minecraft_ermingham")

    async def test_provider_player_calls_check_running_and_resets_error_budget_on_success(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        app.act_err_threshold = 25
        app.act_err_counts = {"_manager": 3}
        app._running = True
        app.player_count = AsyncMock(return_value=(2, 20))
        check_running = Mock(return_value=True)
        app.check_running = check_running
        manager.current = app.name
        manager.apps = {app.name: app}

        provider = Provider_Player(manager)

        value = await provider.get()

        self.assertEqual(value, "2/20")
        check_running.assert_called_once_with()
        self.assertEqual(app.act_err_counts["_manager"], 25)

    async def test_provider_player_skips_error_budget_until_app_has_started(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        app.act_err_threshold = 25
        app.act_err_counts = {"_manager": 7}
        app._running = False
        app.player_count = AsyncMock(return_value=None)
        app.check_running = Mock(return_value=True)
        manager.current = app.name
        manager.apps = {app.name: app}

        provider = Provider_Player(manager)

        value = await provider.get()

        self.assertIsNone(value)
        app.player_count.assert_not_awaited()
        self.assertEqual(app.act_err_counts["_manager"], 7)

    async def test_provider_player_recovers_after_exhausted_error_budget(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        app.act_err_threshold = 3
        app.act_err_counts = {"_manager": 0, "_manager:recovery": 0}
        app._running = True
        app.player_count = AsyncMock(return_value=(1, 10))
        app.check_running = Mock(return_value=True)
        manager.current = app.name
        manager.apps = {app.name: app}

        provider = Provider_Player(manager)

        value = await provider.get()

        self.assertEqual(value, "1/10")
        app.player_count.assert_awaited_once()
        self.assertEqual(app.act_err_counts["_manager"], 3)
        self.assertNotIn("_manager:recovery", app.act_err_counts)

    async def test_wait_for_ready_event_fails_when_process_stops_before_ready(self) -> None:
        app = object.__new__(_DummyApp)
        app.name = "dummy_alpha"
        app.process = None

        with self.assertRaisesRegex(RuntimeError, "stopped before reporting startup readiness"):
            await app.wait_for_ready_event(
                asyncio.Event(),
                timeout_seconds=1.0,
                ready_label="startup readiness",
            )

    async def test_launch_process_propagates_popen_failure(self) -> None:
        app = _build_dummy_app()
        app.cmd_cwd = None
        app.cmd_start = ["missing-server"]
        app.server_log = None
        app.file_stdout = Path("/tmp/dummy_stdout.log")
        app.file_errout = Path("/tmp/dummy_stderr.log")
        app.shell = False

        with patch("apps._app.subprocess.Popen", side_effect=FileNotFoundError("missing executable")):
            with self.assertRaisesRegex(FileNotFoundError, "missing executable"):
                await app._launch_process()

    async def test_load_apps_disables_instance_when_directory_is_missing(self) -> None:
        manager = object.__new__(App_Manager)
        manager.apps = {}
        manager._lookup = {}
        manager.current = None
        manager.default_chat_channel = None
        manager.default_chat_channel_source = RelayChannelSource.NONE
        manager.activity_manager = cast(Any, object())
        original_cwd = Path.cwd()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            instances_path = scope_path / "instances.json"
            instances_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "friendly_name": "Demo Alpha",
                            "directory": "{APPS}/demo-alpha",
                            "enabled": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            os.chdir(temp_path)
            try:
                with (
                    patch.object(config, "APP_PATH", temp_path / "app_data"),
                    patch.object(App_Manager, "_load_scope_types", return_value=(_DummyApp, App_Config)),
                    patch.object(App_Manager, "dump_enabled", return_value=0),
                ):
                    await manager.load_apps(cast(Any, object()))
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["alpha"]["enabled"])
            self.assertEqual(manager.apps, {})
            self.assertEqual(
                manager.startup_disabled_notice_lines(),
                (f"Auto-disabled: demo_alpha (directory missing: {temp_path / 'app_data' / 'demo-alpha'})",),
            )

    async def test_load_apps_disables_instance_when_required_file_is_missing(self) -> None:
        manager = object.__new__(App_Manager)
        manager.apps = {}
        manager._lookup = {}
        manager.current = None
        manager.default_chat_channel = None
        manager.default_chat_channel_source = RelayChannelSource.NONE
        manager.activity_manager = cast(Any, object())
        original_cwd = Path.cwd()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            app_path = temp_path / "app_data" / "demo-alpha"
            app_path.mkdir(parents=True)
            instances_path = scope_path / "instances.json"
            instances_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "friendly_name": "Demo Alpha",
                            "directory": "{APPS}/demo-alpha",
                            "enabled": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            os.chdir(temp_path)
            try:
                with (
                    patch.object(config, "APP_PATH", temp_path / "app_data"),
                    patch.object(App_Manager, "_load_scope_types", return_value=(_MissingFileApp, App_Config)),
                    patch.object(App_Manager, "dump_enabled", return_value=0),
                ):
                    await manager.load_apps(cast(Any, object()))
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["alpha"]["enabled"])
            self.assertEqual(manager.apps, {})
            self.assertEqual(
                manager.startup_disabled_notice_lines(),
                ("Auto-disabled: demo_alpha (App_Settings file missing)",),
            )

    async def test_end_resolves_current_name_with_mixed_case_instance_key(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        app.name = "satisfactory_Prime"
        app.friendly = "Satisfactory Prime"
        app.cfg = App_Config(
            name=app.name,
            instance_key="Prime",
            friendly_name=app.friendly,
            directory=Path("."),
            apps_dir=Path("."),
            scope="satisfactory",
        )
        app.file_instances = app.cfg.apps_dir / "instances.json"
        manager.apps = {app.name: app}
        manager._lookup = {
            app.name: app.name,
            app.name.lower(): app.name,
            app.friendly: app.name,
            app.friendly.lower(): app.name,
        }
        manager.current = app.name

        stopped = await manager.end(manager.current)

        self.assertEqual(stopped, {"Satisfactory_Prime"})

    async def test_kill_targets_current_app_during_startup_before_process_is_running(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        app.kill = AsyncMock(return_value=True)  # type: ignore[method-assign]
        app.check_running = Mock(return_value=False)  # type: ignore[method-assign]
        manager.apps = {app.name: app}
        manager._lookup = {app.name: app.name, app.name.lower(): app.name}
        manager.current = app.name

        killed = await manager.kill(manager.current)

        self.assertEqual(killed, {"Dummy"})
        app.kill.assert_awaited_once()

    async def test_launch_emits_lifecycle_start_embed_for_app_chat_channel(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = _build_dummy_app(join_port=25565)
        app.chat_channel = hikari.Snowflake(123)

        with patch("_manager.DC_Relay.add") as add_mock:
            await manager.launch(app)

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Started")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Started")
        self.assertEqual(relayed_message.relay_embed.description, "Join: `play.example.com:25565`")
        self.assertIsNotNone(app.lifecycle_started_at)

    async def test_launch_emits_lifecycle_start_embed_for_web_chat_only_relay_app(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = _build_dummy_app(has_receiver=True, join_port=25565)

        with patch("_manager.DC_Relay.add") as add_mock:
            await manager.launch(app)

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Started")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Started")
        self.assertEqual(relayed_message.relay_embed.description, "Join: `play.example.com:25565`")
        self.assertIsNotNone(app.lifecycle_started_at)

    async def test_launch_emits_lifecycle_start_embed_for_default_chat_channels_without_chat_relay(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = _build_dummy_app(join_port=25565)
        app.chat_channels = (hikari.Snowflake(123),)

        with patch("_manager.DC_Relay.add") as add_mock:
            await manager.launch(app)

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Started")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Started")
        self.assertEqual(relayed_message.relay_embed.description, "Join: `play.example.com:25565`")

    async def test_launch_skips_lifecycle_start_embed_when_started_notice_disabled(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = _build_dummy_app(join_port=25565)
        app.chat_channel = hikari.Snowflake(123)
        app.cfg.lifecycle_notice_started = False

        with patch("_manager.DC_Relay.add") as add_mock:
            await manager.launch(app)

        add_mock.assert_not_called()
        self.assertIsNotNone(app.lifecycle_started_at)

    async def test_launch_emits_crash_embed_when_startup_records_runtime_fault(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = _build_dummy_app(join_port=25565)
        app.chat_channel = hikari.Snowflake(123)
        app.check_running = Mock(return_value=False)  # type: ignore[method-assign]
        app.handle_unexpected_stop = AsyncMock(return_value=None)  # type: ignore[method-assign]

        async def _crash_start() -> bool:
            app.runtime_fault = AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH,
                summary="Failed to start the minecraft server",
            )
            raise RuntimeError("dummy stopped before reporting server readiness")

        app.start = _crash_start  # type: ignore[method-assign]

        with patch("_manager.DC_Relay.add") as add_mock:
            with self.assertRaisesRegex(RuntimeError, "stopped before reporting server readiness"):
                await manager.launch(app)

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Crashed")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Crashed")
        self.assertEqual(relayed_message.relay_embed.description, "Failed to start the minecraft server")
        app.handle_unexpected_stop.assert_awaited_once()
        self.assertIsNone(app.lifecycle_started_at)
        self.assertIsNone(manager.current)

    async def test_launch_emits_crash_embed_for_web_chat_only_relay_app(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = _build_dummy_app(has_receiver=True, join_port=25565)
        app.check_running = Mock(return_value=False)  # type: ignore[method-assign]
        app.handle_unexpected_stop = AsyncMock(return_value=None)  # type: ignore[method-assign]

        async def _crash_start() -> bool:
            app.runtime_fault = AppRuntimeFault(
                kind=AppRuntimeFaultKind.CRASH,
                summary="Failed to start the minecraft server",
            )
            raise RuntimeError("dummy stopped before reporting server readiness")

        app.start = _crash_start  # type: ignore[method-assign]

        with patch("_manager.DC_Relay.add") as add_mock:
            with self.assertRaisesRegex(RuntimeError, "stopped before reporting server readiness"):
                await manager.launch(app)

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Crashed")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Crashed")
        self.assertEqual(relayed_message.relay_embed.description, "Failed to start the minecraft server")
        app.handle_unexpected_stop.assert_awaited_once()
        self.assertIsNone(app.lifecycle_started_at)
        self.assertIsNone(manager.current)

    async def test_end_emits_lifecycle_stop_embed_with_uptime(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = "dummy"
        app = _build_dummy_app(join_port=25565)
        app.chat_channel = hikari.Snowflake(123)
        app.process = cast(Any, _RunningProcess())
        app.lifecycle_started_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=2, seconds=3)
        manager.apps = {app.name: app}
        manager._lookup = {app.name: app.name, app.name.lower(): app.name}

        with patch("_manager.DC_Relay.add") as add_mock:
            stopped = await manager.end(manager.current)

        self.assertEqual(stopped, {"Dummy"})
        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Stopped")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Ended")
        self.assertEqual(relayed_message.relay_embed.description, "Uptime: `1h 2m 3s`")
        self.assertIsNone(app.lifecycle_started_at)

    async def test_handle_inactive_app_emits_lifecycle_stop_embed_for_unmanaged_shutdown(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = "dummy"
        app = _build_dummy_app(join_port=25565)
        app.chat_channel = hikari.Snowflake(123)
        app.lifecycle_started_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=2, seconds=3)
        app.handle_unexpected_stop = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with patch("_manager.DC_Relay.add") as add_mock:
            await manager._handle_inactive_app(app)

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Stopped")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Dummy Ended")
        self.assertEqual(relayed_message.relay_embed.description, "Uptime: `1h 2m 3s`")
        app.handle_unexpected_stop.assert_awaited_once()
        self.assertIsNone(app.lifecycle_started_at)
        self.assertIsNone(manager.current)

    async def test_handle_inactive_app_skips_duplicate_stop_embed_for_managed_shutdown(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = "dummy"
        manager._managed_shutdown_names = {"dummy"}
        app = _build_dummy_app(join_port=25565)
        app.chat_channel = hikari.Snowflake(123)
        app.lifecycle_started_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=2, seconds=3)
        app.handle_unexpected_stop = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with patch("_manager.DC_Relay.add") as add_mock:
            await manager._handle_inactive_app(app)

        add_mock.assert_not_called()
        app.handle_unexpected_stop.assert_awaited_once()
        self.assertIsNone(app.lifecycle_started_at)
        self.assertIsNone(manager.current)

    async def test_notify_running_app_relays_targets_only_running_inbound_apps(self) -> None:
        manager = object.__new__(App_Manager)
        manager.bot = cast(Any, object())
        restart_notice = MaintenanceNotice(
            stage=MaintenanceStage.WARNING,
            target=RestartTarget.SYSTEM,
            source=RelayNoticeSource.BOT,
            severity=RelayNoticeSeverity.WARNING,
            lead_minutes=1,
        )
        running_app = _build_dummy_app(has_receiver=True)
        running_app._running = True
        running_receiver = _RecordingReceiver()
        running_app.am_receiver = running_receiver
        stopped_app = _build_dummy_app(has_receiver=True)
        stopped_app.name = "stopped"
        stopped_app.friendly = "Stopped"
        stopped_app._running = False
        stopped_receiver = _RecordingReceiver()
        stopped_app.am_receiver = stopped_receiver
        outbound_only_app = _build_dummy_app(chat_relay_outbound=True, has_receiver=False)
        outbound_only_app.name = "outbound"
        outbound_only_app.friendly = "Outbound"
        outbound_only_app._running = True
        manager.apps = {
            running_app.name: running_app,
            stopped_app.name: stopped_app,
            outbound_only_app.name: outbound_only_app,
        }

        sent_count = await manager.notify_running_app_relays(
            "Scheduled maintenance: restart in 1m.",
            notice=restart_notice,
        )

        self.assertEqual(sent_count, 1)
        self.assertEqual(len(running_receiver.payloads), 1)
        self.assertEqual(running_receiver.payloads[0].content, "Scheduled maintenance: restart in 1m.")
        self.assertEqual(running_receiver.payloads[0].player, "System")
        self.assertIs(running_receiver.payloads[0].notice, restart_notice)
        self.assertEqual(len(stopped_receiver.payloads), 0)

    def test_set_current_restart_auto_start_app_persists_then_consume_clears(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            manager = object.__new__(App_Manager)
            running_app = _build_dummy_app()
            running_app.name = "minecraft_alpha"
            running_app.friendly = "Minecraft Alpha"
            manager.apps = {running_app.name: running_app}
            manager.current = running_app.name
            manager._bot_configuration_path = config_path

            persisted = manager.set_current_restart_auto_start_app()
            loaded = config.load_bot_configuration(config_path)
            consumed = manager.consume_restart_auto_start_app()
            cleared = config.load_bot_configuration(config_path)

        self.assertEqual(persisted, "minecraft_alpha")
        self.assertEqual(loaded.restart_state.auto_start_app, "minecraft_alpha")
        self.assertEqual(consumed, "minecraft_alpha")
        self.assertIsNone(cleared.restart_state.auto_start_app)

    def test_set_current_restart_auto_start_app_clears_stale_value_when_no_app_running(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "configuration.json"
            config.save_bot_configuration(
                config_path,
                config.BotConfiguration(
                    restart_state=config.PersistedRestartState(auto_start_app="minecraft_alpha"),
                ),
            )
            manager = object.__new__(App_Manager)
            manager.apps = {}
            manager.current = None
            manager._bot_configuration_path = config_path

            persisted = manager.set_current_restart_auto_start_app()
            loaded = config.load_bot_configuration(config_path)

        self.assertIsNone(persisted)
        self.assertIsNone(loaded.restart_state.auto_start_app)

    async def test_launch_sets_current_while_start_is_in_progress(self) -> None:
        manager = object.__new__(App_Manager)
        manager.current = None
        manager.end = AsyncMock(return_value=set())
        app = object.__new__(_SlowStartApp)
        app.name = "satisfactory_alpha"
        app.friendly = "Satisfactory"
        app.directory = Path(".")
        app.updater = None
        app.mods = None
        app.settings = None
        app.chat_channel = None
        app.chat_channel_override = None
        app.chat_channel_source = RelayChannelSource.NONE
        app.chat_relay_outbound = False
        app.am_receiver = None
        app.manage_embed_color = 0x96212B
        app.lifecycle_started_at = None
        app.cfg = App_Config(
            name=app.name,
            instance_key="alpha",
            friendly_name=app.friendly,
            directory=Path("."),
            apps_dir=Path("."),
            scope="satisfactory",
        )
        app.file_instances = app.cfg.apps_dir / "instances.json"
        app.process = None
        app.start_entered = asyncio.Event()
        app.release_start = asyncio.Event()

        launch_task = asyncio.create_task(manager.launch(app))
        await asyncio.wait_for(app.start_entered.wait(), timeout=1)

        self.assertEqual(manager.current, app.name)

        app.release_start.set()
        await asyncio.wait_for(launch_task, timeout=1)

    async def test_force_invalidate_lock_closes_manager_when_response_is_still_live(self) -> None:
        service = AppManageService()
        app = _build_dummy_app()
        message_id = hikari.Snowflake(123)
        lock_expires_at = service._now() + timedelta(minutes=5)
        bot = Mock()
        bot.rest = Mock()
        bot.rest.edit_interaction_response = AsyncMock()

        service._touch_app_lock(
            message_id=message_id,
            user_id=hikari.Snowflake(456),
            app_name=app.name,
            channel_id=hikari.Snowflake(789),
            application_id=hikari.Snowflake(999),
            interaction_token="token",
            response_expires_at=lock_expires_at,
        )
        lock = service.start_lock(app)
        assert lock is not None

        closed = await service._force_invalidate_lock(
            bot=bot,
            lock=lock,
            actor_user_id=654,
        )

        self.assertTrue(closed)
        self.assertIsNone(service.start_lock(app))
        bot.rest.edit_interaction_response.assert_awaited_once()

    def test_apply_relay_channel_purges_unsupported_override(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(json.dumps({"alpha": {"chat_channel": "123"}}), encoding="utf-8")
            app.cfg = App_Config(
                name="dummy_alpha",
                instance_key="alpha",
                friendly_name="Dummy",
                directory=temp_path,
                apps_dir=temp_path,
                scope="dummy",
            )
            app.file_instances = app.cfg.apps_dir / "instances.json"
            app.chat_channel = hikari.Snowflake(123)
            app.chat_channel_override = hikari.Snowflake(123)
            app.chat_channel_source = RelayChannelSource.INSTANCE
            DC_Relay._chat_channels.clear()

            try:
                manager._apply_relay_channel(app)
            finally:
                DC_Relay._chat_channels.clear()

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertNotIn("chat_channel", payload["alpha"])
            self.assertIsNone(app.chat_channel)
            self.assertIsNone(app.chat_channel_override)
            self.assertIs(app.chat_channel_source, RelayChannelSource.NONE)

    def test_create_instance_writes_new_entry_from_template(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            instances_path = scope_path / "instances.json"
            instances_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "friendly_name": "Demo Alpha",
                            "directory": "{APPS}/demo-alpha",
                            "server_log_file": "{WD}/Server.log",
                            "port": 12345,
                        }
                    }
                ),
                encoding="utf-8",
            )

            os.chdir(temp_path)
            try:
                instance_name = manager.create_instance(
                    AppInstanceCreateRequest(
                        scope="demo",
                        instance_key="beta",
                        friendly_name="Demo Beta",
                        subfolder="demo-beta",
                        port=23456,
                        server_log_file="{WD}/logs/server.log",
                    )
                )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(instance_name, "demo_beta")
            self.assertEqual(payload["beta"]["friendly_name"], "Demo Beta")
            self.assertEqual(payload["beta"]["directory"], "{APPS}/demo-beta")
            self.assertEqual(payload["beta"]["server_log_file"], "{WD}/logs/server.log")
            self.assertEqual(payload["beta"]["join_port"], 23456)

    def test_create_instance_rejects_subfolder_escape(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            instances_path = scope_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Demo Alpha", "directory": "{APPS}/demo-alpha"}}),
                encoding="utf-8",
            )

            os.chdir(temp_path)
            try:
                with self.assertRaisesRegex(ValueError, "DIR_APP|within DIR_APP|stay within DIR_APP"):
                    manager.create_instance(
                        AppInstanceCreateRequest(
                            scope="demo",
                            instance_key="beta",
                            friendly_name="Demo Beta",
                            subfolder="../escape",
                        )
                    )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(tuple(payload.keys()), ("alpha",))

    def test_list_create_scopes_includes_scope_with_builtin_template_and_no_instances(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            (scope_path / "__init__.py").write_text("", encoding="utf-8")

            os.chdir(temp_path)
            try:
                with patch.dict(
                    "_manager._SCOPE_INSTANCE_TEMPLATES",
                    {"demo": AppInstanceTemplate(mods_dir="{WD}/mods", join_port=25565)},
                ):
                    scopes = manager.list_create_scopes()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(scopes, ("demo",))

    def test_create_instance_writes_new_entry_from_builtin_template(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            (scope_path / "__init__.py").write_text("", encoding="utf-8")
            instances_path = scope_path / "instances.json"

            os.chdir(temp_path)
            try:
                with patch.dict(
                    "_manager._SCOPE_INSTANCE_TEMPLATES",
                    {
                        "demo": AppInstanceTemplate(
                            mods_dir="{WD}/mods",
                            server_log_file="{WD}/Server.log",
                            join_port=25565,
                        )
                    },
                ):
                    instance_name = manager.create_instance(
                        AppInstanceCreateRequest(
                            scope="demo",
                            instance_key="alpha",
                            friendly_name="Demo Alpha",
                            subfolder="demo-alpha",
                        )
                    )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(instance_name, "demo_alpha")
            self.assertEqual(payload["alpha"]["friendly_name"], "Demo Alpha")
            self.assertEqual(payload["alpha"]["directory"], "{APPS}/demo-alpha")
            self.assertEqual(payload["alpha"]["mods_dir"], "{WD}/mods")
            self.assertEqual(payload["alpha"]["server_log_file"], "{WD}/Server.log")
            self.assertEqual(payload["alpha"]["join_port"], 25565)

    def test_set_app_friendly_name_persists_instance_metadata(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Dummy", "directory": "{APPS}/dummy"}}),
                encoding="utf-8",
            )
            app = _build_dummy_app()
            app.file_instances = instances_path
            app.cfg.apps_dir = temp_path
            manager._lookup = {}
            manager._register_lookup_aliases(app.name, app)
            hub = ChatHub()
            hub.clear_room(app.name)
            hub.bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), app.friendly))

            os.chdir(temp_path)
            try:
                updated_friendly_name = manager.set_app_friendly_name(app, "Demo Alpha")
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_friendly_name, "Demo Alpha")
            self.assertEqual(app.friendly, "Demo Alpha")
            self.assertEqual(app.cfg.friendly_name, "Demo Alpha")
            self.assertEqual(payload["alpha"]["friendly_name"], "Demo Alpha")
            self.assertEqual(manager._lookup.get("Demo Alpha"), app.name)
            self.assertEqual(manager._lookup.get("demo alpha"), app.name)
            self.assertIsNone(manager._lookup.get("Dummy"))
            endpoints = hub.endpoints_for_room(app.name)
            self.assertEqual(len(endpoints), 1)
            self.assertEqual(endpoints[0].label, "Demo Alpha")

    def test_set_app_friendly_name_rejects_lookup_collision(self) -> None:
        manager = object.__new__(App_Manager)
        primary_app = _build_dummy_app()
        primary_app.name = "dummy_alpha"
        primary_app.cfg.name = primary_app.name
        primary_app.cfg.instance_key = "alpha"
        primary_app.friendly = "Dummy Alpha"
        primary_app.cfg.friendly_name = primary_app.friendly
        conflicting_app = _build_dummy_app()
        conflicting_app.name = "dummy_beta"
        conflicting_app.cfg.name = conflicting_app.name
        conflicting_app.cfg.instance_key = "beta"
        conflicting_app.friendly = "Dummy Beta"
        conflicting_app.cfg.friendly_name = conflicting_app.friendly
        manager.apps = {primary_app.name: primary_app, conflicting_app.name: conflicting_app}
        manager._lookup = {}
        manager._register_lookup_aliases(primary_app.name, primary_app)
        manager._register_lookup_aliases(conflicting_app.name, conflicting_app)

        with self.assertRaisesRegex(ValueError, "Friendly name conflicts with existing app alias"):
            manager.set_app_friendly_name(primary_app, "Dummy Beta")

    def test_set_app_friendly_name_rejects_overlong_value(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()

        with self.assertRaisesRegex(
            ValueError, f"Friendly name must be {APP_FRIENDLY_NAME_MAX_LENGTH} characters or fewer."
        ):
            manager.set_app_friendly_name(app, "A" * (APP_FRIENDLY_NAME_MAX_LENGTH + 1))

    def test_update_app_details_persists_notes_and_lifecycle_notice_flags(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Dummy", "directory": "{APPS}/dummy"}}),
                encoding="utf-8",
            )
            app = _build_dummy_app()
            app.file_instances = instances_path
            app.cfg.apps_dir = temp_path
            manager._lookup = {}
            manager._register_lookup_aliases(app.name, app)

            os.chdir(temp_path)
            try:
                updated_friendly_name = manager.update_app_details(
                    app,
                    AppDetailsUpdate(
                        friendly_name="Dummy Prime",
                        notes="Main survival shard",
                        lifecycle_notice_started=False,
                        lifecycle_notice_stopped=True,
                        lifecycle_notice_crashed=False,
                    ),
                )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_friendly_name, "Dummy Prime")
            self.assertEqual(app.cfg.notes, "Main survival shard")
            self.assertFalse(app.cfg.lifecycle_notice_started)
            self.assertTrue(app.cfg.lifecycle_notice_stopped)
            self.assertFalse(app.cfg.lifecycle_notice_crashed)
            self.assertEqual(payload["alpha"]["notes"], "Main survival shard")
            self.assertFalse(payload["alpha"]["lifecycle_notice_started"])
            self.assertTrue(payload["alpha"]["lifecycle_notice_stopped"])
            self.assertFalse(payload["alpha"]["lifecycle_notice_crashed"])

    def test_create_instance_requires_admin_password_for_satisfactory(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "satisfactory"
            scope_path.mkdir(parents=True)
            (scope_path / "__init__.py").write_text("", encoding="utf-8")
            instances_path = scope_path / "instances.json"

            os.chdir(temp_path)
            try:
                with patch.dict(
                    "_manager._SCOPE_INSTANCE_TEMPLATES",
                    {
                        "satisfactory": AppInstanceTemplate(
                            join_port=7777,
                            api_host="127.0.0.1",
                        )
                    },
                ):
                    with self.assertRaisesRegex(ValueError, "Admin password must not be empty"):
                        manager.create_instance(
                            AppInstanceCreateRequest(
                                scope="satisfactory",
                                instance_key="alpha",
                                friendly_name="Satisfactory Alpha",
                                subfolder="satisfactory-alpha",
                            )
                        )

                    instance_name = manager.create_instance(
                        AppInstanceCreateRequest(
                            scope="satisfactory",
                            instance_key="alpha",
                            friendly_name="Satisfactory Alpha",
                            subfolder="satisfactory-alpha",
                            admin_password=" secret ",
                        )
                    )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(instance_name, "satisfactory_alpha")
            self.assertEqual(payload["alpha"]["friendly_name"], "Satisfactory Alpha")
            self.assertEqual(payload["alpha"]["directory"], "{APPS}/satisfactory-alpha")
            self.assertEqual(payload["alpha"]["join_port"], 7777)
            self.assertEqual(payload["alpha"]["api_host"], "127.0.0.1")
            self.assertEqual(payload["alpha"]["admin_password"], "secret")


if __name__ == "__main__":
    unittest.main()
