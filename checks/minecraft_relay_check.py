from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import hikari

import config
from _discord import (
    App_Bound,
    DC_Bound,
    DC_Relay,
    Fileish,
    MediaProvider,
    Message,
    RelayEmbedPayload,
    RelayMessageReferenceKind,
    URLish,
    URLVariant,
)
from apps._app import AM_Receiver, AppRuntimeFaultKind
from apps._config import AppVersion
from apps.minecraft import (
    Matchers,
    Minecraft,
    Minecraft_Config,
    MinecraftLoader,
    MinecraftRuntimeInfo,
    MinecraftServerPropertiesSnapshot,
    Players,
    Receiver,
    _detect_minecraft_runtime,
    _minecraft_crash_summary_from_log_line,
    _runtime_info_from_log_line,
)
from chat_hub import ChatEndpoint, ChatEndpointId, ChatEvent, ChatHub
from relay_notices import PlayerSessionAction, PlayerSessionNotice, RelayNoticeSource


class _NamesStub:
    @staticmethod
    def parse_mentions(
        text: str,
        *,
        scope: str | None = None,
        platforms: tuple[object, ...] = (),
        preferred_platform: object | None = None,
    ) -> tuple[str, set[int]]:
        del scope, platforms, preferred_platform
        return text, set()

    @staticmethod
    def cached_display_name(
        user_id: hikari.Snowflakeish | None,
        fallback: str,
        *,
        preferred_guild_id: hikari.Snowflakeish | None = None,
    ) -> str:
        del user_id, preferred_guild_id
        return fallback

    @staticmethod
    def discord_display_name(
        user_id: hikari.Snowflakeish | None,
        fallback: str,
        *,
        fallback_display_name: str | None = None,
    ) -> str:
        del user_id
        return fallback_display_name or fallback

    @staticmethod
    def discord_fallback_name(
        user_id: hikari.Snowflakeish | None,
        fallback: str,
        *,
        scope: str | None = None,
        fallback_display_name: str | None = None,
    ) -> str:
        del user_id, scope
        return fallback_display_name or fallback

    @staticmethod
    def relay_display_name(
        user_id: int | None,
        default: str,
        /,
        *,
        scope: str | None = None,
        platforms: tuple[object, ...] = (),
        preferred_platform: object | None = None,
        preferred_guild_id: hikari.Snowflakeish | None = None,
    ) -> str:
        del user_id, scope, platforms, preferred_platform, preferred_guild_id
        return default


def _minecraft_player_notice_app() -> Minecraft:
    app = cast(Any, object.__new__(Minecraft))
    app.name = "minecraft_demo"
    app.scope = "minecraft"
    app.cfg = SimpleNamespace(relay_notice_player_session=True)
    return app


class _DummyReceiver(AM_Receiver):
    async def send(self, payload: App_Bound) -> None:
        return None


class _StoppedRelayApp:
    name = "minecraft_demo"
    friendly = "Minecraft Demo"
    _running = False

    def __init__(self, receiver: object) -> None:
        self.am_receiver = receiver


class _RelayMock:
    def __init__(self, responses: Sequence[str]) -> None:
        self.send = AsyncMock(side_effect=list(responses))


class _SleepStopper:
    def __init__(self, players: Players, *, stop_after: int) -> None:
        self._players = players
        self._stop_after = stop_after
        self.calls = 0

    async def __call__(self, _: float) -> None:
        self.calls += 1
        if self.calls >= self._stop_after:
            self._players._running = False


class _RelayTTSStub:
    def __init__(self, *, target: config.VoiceTargetConfig) -> None:
        self._target = target
        self.queue_relay_message_mock = AsyncMock(return_value=("Stone Age", 1))

    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None:
        return self._target if hikari.Snowflake(guild_id) == self._target.guild_id else None

    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]:
        return await self.queue_relay_message_mock(guild_id, channel_id, message_id, text, user_id=user_id)


def _make_send_channel(
    *,
    send_result: object,
    channel_id: hikari.Snowflakeish = 1,
    guild_id: hikari.Snowflakeish | None = None,
) -> hikari.TextableChannel:
    return cast(
        hikari.TextableChannel,
        cast(
            object,
            SimpleNamespace(
                id=hikari.Snowflake(channel_id),
                guild_id=hikari.Snowflake(guild_id) if guild_id is not None else None,
                send=AsyncMock(return_value=send_result),
            ),
        ),
    )


def _make_minecraft_cfg(
    *,
    relay_advancements: bool = True,
    version: AppVersion | None = None,
) -> Minecraft_Config:
    return Minecraft.cfg_cls(
        name="minecraft_demo",
        instance_key="alpha",
        directory=Path("."),
        apps_dir=Path("."),
        scope="minecraft",
        relay_advancements=relay_advancements,
        version=version,
    )


def _make_textable_channel() -> hikari.TextableChannel:
    return cast(
        hikari.TextableChannel,
        cast(
            object,
            SimpleNamespace(app=cast(Any, object()), id=hikari.Snowflake(1), name="relay-test", type=1),
        ),
    )


def _make_resolution_message(*, app: object, player: str, status: config.NameResolutionStatus) -> DC_Bound:
    return cast(
        DC_Bound,
        cast(
            object,
            SimpleNamespace(
                player=player,
                player_resolution=config.NameResolutionResult(status),
                app=app,
            ),
        ),
    )


class MinecraftRelayTests(unittest.IsolatedAsyncioTestCase):
    def test_server_properties_snapshot_loads_rcon_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "server.properties"
            pointer.write_text(
                "\n".join(
                    (
                        "enable-rcon=true",
                        "rcon.port=25575",
                        "rcon.password=supersecret",
                        "max-players=12",
                    )
                ),
                encoding="utf-8",
            )

            snapshot = MinecraftServerPropertiesSnapshot.load(pointer)

        self.assertEqual(snapshot.enable_rcon, True)
        self.assertEqual(snapshot.rcon_port, 25575)
        self.assertEqual(snapshot.rcon_password, "supersecret")
        self.assertEqual(snapshot.max_players, 12)

    def test_detect_runtime_from_forge_args_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            args_path = root / "libraries" / "net" / "minecraftforge" / "forge" / "1.20.1-47.4.0" / "unix_args.txt"
            args_path.parent.mkdir(parents=True)
            args_path.write_text(
                "\n".join(
                    (
                        "--launchTarget",
                        "forgeserver",
                        "--fml.forgeVersion",
                        "47.4.0",
                        "--fml.mcVersion",
                        "1.20.1",
                    )
                ),
                encoding="utf-8",
            )

            runtime = _detect_minecraft_runtime(
                directory=root,
                server_log=None,
                cfg=Minecraft.cfg_cls(
                    name="minecraft_alpha",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="minecraft",
                ),
            )

        self.assertEqual(runtime, MinecraftRuntimeInfo("1.20.1", MinecraftLoader.FORGE, "47.4.0"))

    def test_detect_runtime_from_forge_coordinate_when_only_vanilla_log_is_present(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            forge_directory = root / "libraries" / "net" / "minecraftforge" / "forge" / "1.20.1-47.4.10"
            forge_directory.mkdir(parents=True)
            logs_directory = root / "logs"
            logs_directory.mkdir()
            (logs_directory / "latest.log").write_text(
                "[00:00:00] [main/INFO]: Starting minecraft server version 1.20.1",
                encoding="utf-8",
            )

            runtime = _detect_minecraft_runtime(
                directory=root,
                server_log=None,
                cfg=Minecraft.cfg_cls(
                    name="minecraft_alpha",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="minecraft",
                ),
            )

        self.assertEqual(runtime, MinecraftRuntimeInfo("1.20.1", MinecraftLoader.FORGE, "47.4.10"))

    def test_detect_runtime_from_fabric_server_jar_name(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fabric-server-mc.1.20.6-loader.0.16.10-launcher.1.0.1.jar").write_text("", encoding="utf-8")

            runtime = _detect_minecraft_runtime(
                directory=root,
                server_log=None,
                cfg=Minecraft.cfg_cls(
                    name="minecraft_alpha",
                    instance_key="alpha",
                    directory=root,
                    apps_dir=root,
                    scope="minecraft",
                ),
            )

        self.assertEqual(runtime, MinecraftRuntimeInfo("1.20.6", MinecraftLoader.FABRIC, "0.16.10"))

    def test_detect_runtime_from_vanilla_log_line(self) -> None:
        runtime = _runtime_info_from_log_line("[00:00:00] [main/INFO]: Starting minecraft server version 1.20.4")

        self.assertEqual(runtime, MinecraftRuntimeInfo("1.20.4", MinecraftLoader.VANILLA, None))

    def test_detect_runtime_from_modlauncher_forge_log_line(self) -> None:
        runtime = _runtime_info_from_log_line(
            "[03Jun2026 18:08:45.352] [main/INFO] [cpw.mods.modlauncher.Launcher/MODLAUNCHER]: "
            "ModLauncher running: args [--launchTarget, forgeserver, --fml.forgeVersion, 47.4.0, "
            "--fml.mcVersion, 1.20.1, --fml.forgeGroup, net.minecraftforge, --fml.mcpVersion, 20230612.114412, nogui]"
        )

        self.assertEqual(runtime, MinecraftRuntimeInfo("1.20.1", MinecraftLoader.FORGE, "47.4.0"))

    def test_detect_runtime_from_quilt_log_line(self) -> None:
        runtime = _runtime_info_from_log_line(
            "[23:26:57] [main/INFO]: Loading Minecraft 1.20.1 with Quilt Loader 0.26.1-beta.1"
        )

        self.assertEqual(runtime, MinecraftRuntimeInfo("1.20.1", MinecraftLoader.QUILT, "0.26.1-beta.1"))

    def test_detect_crash_summary_from_main_fatal_log_line(self) -> None:
        summary = _minecraft_crash_summary_from_log_line(
            "[04Jun2026 08:29:24.797] [main/ERROR] [net.minecraft.server.Main/FATAL]: "
            "Failed to start the minecraft server"
        )

        self.assertEqual(summary, "Failed to start the minecraft server")

    def test_detect_crash_summary_ignores_intermediate_crash_report_uuid_lines(self) -> None:
        summary = _minecraft_crash_summary_from_log_line(
            "[04Jun2026 08:29:22.884] [main/FATAL] [net.minecraftforge.common.ForgeMod/]: "
            "Preparing crash report with UUID 17451b13-ee0d-4d29-9334-d74e5904f42e"
        )

        self.assertIsNone(summary)

    def test_parse_list_response_handles_names(self) -> None:
        snapshot = Players.parse_list_response("There are 3/20 players online: Alice, Bob, Carol")

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.online, 3)
        self.assertEqual(snapshot.maximum, 20)
        self.assertEqual(snapshot.players, frozenset({"Alice", "Bob", "Carol"}))

    def test_parse_count_response_handles_vanilla_list_output(self) -> None:
        self.assertEqual(
            Players.parse_count_response("There are 0 of a max of 20 players online:"),
            (0, 20),
        )

    def test_parse_count_response_handles_slashed_list_output(self) -> None:
        self.assertEqual(
            Players.parse_count_response("There are 3/20 players online: Alice, Bob, Carol"),
            (3, 20),
        )

    async def test_reconcile_players_self_heals_join_and_leave_state(self) -> None:
        app = _minecraft_player_notice_app()
        players = Players(app)
        players._players = {"Alice", "Bob"}

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await players._reconcile_players({"Bob", "Carol"})

        self.assertEqual(players._players, {"Bob", "Carol"})
        self.assertEqual(add_mock.call_count, 2)
        calls = add_mock.call_args_list
        self.assertEqual(calls[0].args[0].content, "Alice left minecraft_demo")
        self.assertEqual(calls[0].args[0].player, "Alice")
        self.assertEqual(calls[1].args[0].content, "Carol joined minecraft_demo")
        self.assertEqual(calls[1].args[0].player, "Carol")

    async def test_log_join_does_not_duplicate_reconcile_join_notice(self) -> None:
        app = _minecraft_player_notice_app()
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_join("[12:00:00] [Server thread/INFO]: Alice joined the game")
            await app._players._reconcile_players({"Alice"})

        add_mock.assert_called_once()

    async def test_log_leave_does_not_duplicate_reconcile_leave_notice(self) -> None:
        app = _minecraft_player_notice_app()
        app._tail_machers = set()
        app._players = Players(app)
        app._players._players = {"Alice"}
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_left("[12:00:00] [Server thread/INFO]: Alice left the game")
            await app._players._reconcile_players(set())

        add_mock.assert_called_once()

    async def test_note_join_resolves_player_to_discord_mention_when_available(self) -> None:
        app = _minecraft_player_notice_app()
        app.name_cache = SimpleNamespace(
            resolve_name=Mock(return_value=config.NameResolutionResult(config.NameResolutionStatus.UNIQUE, 42))
        )
        players = Players(app)
        app._players = players

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            players.note_join("Alice")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.content, "Alice joined minecraft_demo")
        self.assertEqual(relayed_message.player, "Alice")
        self.assertEqual(relayed_message.player_id, 42)
        app.name_cache.resolve_name.assert_called_once_with("Alice", "minecraft")

    async def test_note_join_omits_client_pack_details_when_published(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.cfg = SimpleNamespace(
            client_pack_published_version="2026-07-04",
            client_pack_content_dirty=True,
            relay_notice_player_session=True,
        )
        app.name_cache = SimpleNamespace(
            resolve_name=Mock(return_value=config.NameResolutionResult(config.NameResolutionStatus.UNIQUE, 42))
        )
        players = Players(app)
        app._players = players

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            players.note_join("Alice")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.content, "Alice joined Minecraft Demo")
        self.assertIsInstance(relayed_message.notice, PlayerSessionNotice)
        assert isinstance(relayed_message.notice, PlayerSessionNotice)
        self.assertIsNone(relayed_message.notice.pack_version)
        self.assertFalse(relayed_message.notice.has_unpublished_pack_changes)

    async def test_note_leave_resolves_player_to_discord_mention_when_available(self) -> None:
        app = _minecraft_player_notice_app()
        app.name_cache = SimpleNamespace(
            resolve_name=Mock(return_value=config.NameResolutionResult(config.NameResolutionStatus.UNIQUE, 42))
        )
        players = Players(app)
        app._players = players
        players._players = {"Alice"}

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            players.note_leave("Alice")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.content, "Alice left minecraft_demo")
        self.assertEqual(relayed_message.player, "Alice")
        self.assertEqual(relayed_message.player_id, 42)
        app.name_cache.resolve_name.assert_called_once_with("Alice", "minecraft")

    def test_lifecycle_relay_description_lines_include_pack_and_squaremap(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.scope = "minecraft"
        app.cfg = SimpleNamespace(
            client_pack_published_version="2026-07-05",
            client_pack_content_dirty=True,
        )

        with patch.object(app, "_squaremap_public_url", return_value="https://maps.example.com") as squaremap_url:
            lines = app.lifecycle_relay_description_lines(started=True)

        squaremap_url.assert_called_once_with()
        self.assertEqual(
            lines,
            (
                "Pack: 2026-07-05 [Unpublished Changes]",
                "[Squaremap](https://maps.example.com)",
            ),
        )

    async def test_listplayers_logs_unrecognised_response_only_once_until_success(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        relay = _RelayMock(["???", "???", "There are 0/20 players online:"])
        app._relay = relay
        players = Players(app)
        players._running = True
        stop_after_three_sleeps = _SleepStopper(players, stop_after=3)

        with (
            patch("apps.minecraft.asyncio.sleep", new=stop_after_three_sleeps),
            patch("apps.minecraft.log.warning") as warning_mock,
        ):
            await players._listplayers()

        warning_mock.assert_called_once()
        self.assertEqual(players._online, 0)
        self.assertEqual(players._max, 20)

    async def test_count_fetches_snapshot_when_cache_is_empty(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._running = True
        app._relay = _RelayMock(["There are 2/20 players online: Alice, Bob"])
        players = Players(app)

        value = await players.count()

        self.assertEqual(value, (2, 20))
        self.assertEqual(players._online, 2)
        self.assertEqual(players._max, 20)

    async def test_match_ready_sets_server_ready_event(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app._tail_machers = set()
        app._server_ready = asyncio.Event()
        matcher = Matchers(app)

        await matcher.match_ready('[12:00:00] [Server thread/INFO]: Done (123.456s)! For help, type "help"')

        self.assertTrue(app._server_ready.is_set())

    async def test_match_chat_ignores_relay_notice_prefix(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_chat("[12:00:00] [Server thread/INFO]: <System> !relay notice: unresolved player")

        add_mock.assert_not_called()

    async def test_match_kubejs_chat_relays_structured_message(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            script_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("// managed", encoding="utf-8")

            app = cast(Any, object.__new__(Minecraft))
            app.cfg = _make_minecraft_cfg()
            app.name = "minecraft_demo"
            app.scope = "minecraft"
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: [SimpleNamespace(name="kubejs-forge-2001.6.5-build.26.jar")]
                if state is not False
                else []
            )
            app._kubejs_event_stream_ready = False
            app._tail_machers = set()
            app._players = Players(app)
            matcher = Matchers(app)

            await matcher.match_kubejs_script_loaded(
                "[07Jun2026 03:51:57.975] [Server thread/INFO] [KubeJS Server/]: "
                "Loaded script server_scripts:yuki_log.js in 0.011 s"
            )

            with patch("apps.minecraft.DC_Relay.add") as add_mock:
                await matcher.match_kubejs_event(
                    '[07Jun2026 03:52:34.516] [Worker-Main-25/INFO] [KubeJS Server/]: '
                    'yuki_log.js#15: [YUKI_MC_EVENT] {"type":"chat","time":1.780768354516E12,'
                    '"player":"APasz","uuid":"3ae72093-e174-439a-a155-7cf6c8651184","message":"woa"}'
                )

            add_mock.assert_called_once()
            relayed_message = add_mock.call_args.args[0]
            self.assertEqual(relayed_message.player, "APasz")
            self.assertEqual(relayed_message.content, "woa")
            self.assertEqual(app._players._player_uuids["apasz"], "3ae72093-e174-439a-a155-7cf6c8651184")

    async def test_match_kubejs_event_ignores_non_script_prefixed_line(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            script_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("// managed", encoding="utf-8")

            app = cast(Any, object.__new__(Minecraft))
            app.cfg = _make_minecraft_cfg()
            app.name = "minecraft_demo"
            app.scope = "minecraft"
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: [SimpleNamespace(name="kubejs-forge-2001.6.5-build.26.jar")]
                if state is not False
                else []
            )
            app._kubejs_event_stream_ready = True
            app._tail_machers = set()
            app._players = Players(app)
            matcher = Matchers(app)

            with patch("apps.minecraft.DC_Relay.add") as add_mock:
                await matcher.match_kubejs_event(
                    '[07Jun2026 03:52:34.516] [Worker-Main-25/INFO] [KubeJS Server/]: '
                    '[YUKI_MC_EVENT] {"type":"chat","time":1.780768354516E12,'
                    '"player":"APasz","uuid":"3ae72093-e174-439a-a155-7cf6c8651184","message":"woa"}'
                )

            add_mock.assert_not_called()

    async def test_match_chat_keeps_vanilla_line_before_kubejs_script_loads(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            script_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("// managed", encoding="utf-8")

            app = cast(Any, object.__new__(Minecraft))
            app.cfg = _make_minecraft_cfg()
            app.name = "minecraft_demo"
            app.scope = "minecraft"
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: [SimpleNamespace(name="kubejs-forge-2001.6.5-build.26.jar")]
                if state is not False
                else []
            )
            app._kubejs_event_stream_ready = False
            app._tail_machers = set()
            app._players = Players(app)
            matcher = Matchers(app)

            with patch("apps.minecraft.DC_Relay.add") as add_mock:
                await matcher.match_chat("[12:00:00] [Server thread/INFO]: <APasz> woa")

            add_mock.assert_called_once()

    async def test_match_chat_ignores_vanilla_line_when_kubejs_script_is_available(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            script_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("// managed", encoding="utf-8")

            app = cast(Any, object.__new__(Minecraft))
            app.cfg = _make_minecraft_cfg()
            app.name = "minecraft_demo"
            app.scope = "minecraft"
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: [SimpleNamespace(name="kubejs-forge-2001.6.5-build.26.jar")]
                if state is not False
                else []
            )
            app._kubejs_event_stream_ready = False
            app._tail_machers = set()
            app._players = Players(app)
            matcher = Matchers(app)

            await matcher.match_kubejs_script_loaded(
                "[07Jun2026 03:51:57.975] [Server thread/INFO] [KubeJS Server/]: "
                "Loaded script server_scripts:yuki_log.js in 0.011 s"
            )

            with patch("apps.minecraft.DC_Relay.add") as add_mock:
                await matcher.match_chat("[12:00:00] [Server thread/INFO]: <APasz> woa")

            add_mock.assert_not_called()

    async def test_match_kubejs_player_death_is_ignored_until_native_death_relay_is_replaced(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            script_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("// managed", encoding="utf-8")

            app = cast(Any, object.__new__(Minecraft))
            app.cfg = _make_minecraft_cfg()
            app.name = "minecraft_demo"
            app.scope = "minecraft"
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: [SimpleNamespace(name="kubejs-forge-2001.6.5-build.26.jar")]
                if state is not False
                else []
            )
            app._kubejs_event_stream_ready = False
            app._tail_machers = set()
            app._players = Players(app)
            matcher = Matchers(app)

            await matcher.match_kubejs_script_loaded(
                "[07Jun2026 03:51:57.975] [Server thread/INFO] [KubeJS Server/]: "
                "Loaded script server_scripts:yuki_log.js in 0.011 s"
            )

            with patch("apps.minecraft.DC_Relay.add") as add_mock:
                await matcher.match_kubejs_event(
                    '[07Jun2026 03:52:47.128] [Server thread/INFO] [KubeJS Server/]: '
                    'yuki_log.js#15: [YUKI_MC_EVENT] {"type":"player_death","time":1.780768367128E12,'
                    '"player":"APasz","uuid":"3ae72093-e174-439a-a155-7cf6c8651184","source":"DamageSource (fall)"}'
                )

            add_mock.assert_not_called()
            self.assertEqual(app._players._player_uuids["apasz"], "3ae72093-e174-439a-a155-7cf6c8651184")

    async def test_match_death_preserves_multiword_create_vehicle_names(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app.name_cache = SimpleNamespace(
            resolve_name=Mock(return_value=config.NameResolutionResult(config.NameResolutionStatus.NOT_FOUND))
        )
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_death("[12:00:00] [Server thread/INFO]: Alice was run over by Ge 6/6 I")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "Alice")
        self.assertEqual(relayed_message.content, "was run over by Ge 6/6 I")
        app.name_cache.resolve_name.assert_not_called()

    async def test_match_death_resolves_decimated_player_kills_to_discord_mentions(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app.name_cache = SimpleNamespace(
            resolve_name=Mock(
                side_effect=lambda player, scope: (
                    config.NameResolutionResult(config.NameResolutionStatus.UNIQUE, 42)
                    if (player, scope) == ("Bob", "minecraft")
                    else config.NameResolutionResult(config.NameResolutionStatus.NOT_FOUND)
                )
            )
        )
        app._tail_machers = set()
        app._players = Players(app)
        app._players.note_uuid("Bob", "123e4567-e89b-12d3-a456-426614174000")
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_death("[12:00:00] [Server thread/INFO]: Alice was decimated by Bob")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "Alice")
        self.assertEqual(relayed_message.content, "was decimated by <@42>")
        app.name_cache.resolve_name.assert_called_once_with("Bob", "minecraft")

    async def test_match_death_relays_supported_modded_death_phrases(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)
        cases = (
            (
                "[12:00:00] [Server thread/INFO]: Alice bled to death",
                "Alice",
                "bled to death",
            ),
            (
                "[12:00:00] [Server thread/INFO]: Alice couldn't breathe anymore",
                "Alice",
                "couldn't breathe anymore",
            ),
            (
                "[12:00:00] [Server thread/INFO]: Alice touched the primary circuit of a running Tesla coil",
                "Alice",
                "touched the primary circuit of a running Tesla coil",
            ),
            (
                "[12:00:00] [Server thread/INFO]: Alice went dancing in the acid rain",
                "Alice",
                "went dancing in the acid rain",
            ),
        )

        for line, expected_player, expected_content in cases:
            with self.subTest(line=line):
                with patch("apps.minecraft.DC_Relay.add") as add_mock:
                    await matcher.match_death(line)

                add_mock.assert_called_once()
                relayed_message = add_mock.call_args.args[0]
                self.assertEqual(relayed_message.player, expected_player)
                self.assertEqual(relayed_message.content, expected_content)

    async def test_match_death_ignores_non_death_mod_warning_lines(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_death(
                "[06Jun2026 07:27:28.067] [Server thread/WARN] [cgm/]: literal{asdmea}(2adf8e74-111d-4867-95c9-ae6e6a454afe) "
                "tried to fire before cooldown finished or server is lagging? Remaining milliseconds: 89"
            )

        add_mock.assert_not_called()

    async def test_match_death_ignores_chat_lines(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_death(
                "[06Jun2026 07:06:39.352] [Server thread/INFO] [net.minecraft.server.MinecraftServer/]: "
                "<Rando> gotta be seiso when collecting"
            )

        add_mock.assert_not_called()

    async def test_match_runtime_updates_version_fields(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app.persist_instance_config_overrides = Mock()  # type: ignore[method-assign]
        matcher = Matchers(app)

        await matcher.match_runtime("[23:26:57] [main/INFO]: Loading Minecraft 1.20.1 with Quilt Loader 0.26.1-beta.1")

        version = app.cfg.version
        assert version is not None
        self.assertEqual(version.main, "1.20.1")
        self.assertEqual(version.loader, "quilt")
        self.assertEqual(version.framework, "0.26.1-beta.1")

    async def test_match_runtime_does_not_downgrade_forge_to_vanilla(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg(version=AppVersion(main="1.20.1", framework="47.4.0", loader="forge"))
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._runtime = MinecraftRuntimeInfo("1.20.1", MinecraftLoader.FORGE, "47.4.0")
        app._tail_machers = set()
        app.persist_instance_config_overrides = Mock()  # type: ignore[method-assign]
        matcher = Matchers(app)

        await matcher.match_runtime("[00:00:00] [main/INFO]: Starting minecraft server version 1.20.1")

        version = app.cfg.version
        assert version is not None
        self.assertEqual(version.main, "1.20.1")
        self.assertEqual(version.loader, "forge")
        self.assertEqual(version.framework, "47.4.0")

    async def test_match_crash_records_runtime_fault_and_prefers_latest_summary(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app.runtime_fault = None
        matcher = Matchers(app)

        await matcher.match_crash(
            "[04Jun2026 08:29:22.884] [main/FATAL] [net.minecraftforge.server.loading.ServerModLoader/]: "
            "Crash report saved to ./crash-reports/crash-2026-06-04_08.29.22-fml.txt"
        )
        await matcher.match_crash(
            "[04Jun2026 08:29:24.797] [main/ERROR] [net.minecraft.server.Main/FATAL]: "
            "Failed to start the minecraft server"
        )

        self.assertIsNotNone(app.runtime_fault)
        assert app.runtime_fault is not None
        self.assertIs(app.runtime_fault.kind, AppRuntimeFaultKind.CRASH)
        self.assertEqual(app.runtime_fault.summary, "Failed to start the minecraft server")

    async def test_match_chat_decodes_chatimage_cicode_with_documented_argument_order(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_chat(
                "[12:00:00] [Server thread/INFO]: <Alice> [[CICode,url=https://cdn.example.com/cat.png,name=Cat]]"
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.content, "[Cat](https://cdn.example.com/cat.png)")
        self.assertEqual(relayed_message.player_avatar_uri, "https://mc-heads.net/avatar/Alice/32")

    async def test_match_uuid_caches_player_uuid_for_future_player_heads(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg()
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        await matcher.match_uuid(
            "[12:00:00] [Server thread/INFO]: UUID of player Alice is 123e4567-e89b-12d3-a456-426614174000"
        )

        self.assertEqual(
            app._players.avatar_uri("Alice"),
            "https://mc-heads.net/avatar/123e4567-e89b-12d3-a456-426614174000/32",
        )

    def test_player_avatar_uri_uses_cached_minecraft_uuid_when_live_uuid_is_unknown(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app.name_cache = SimpleNamespace(
            resolve_game_alias_to_id=Mock(return_value=42),
            get_game_uuid=Mock(return_value="123e4567-e89b-12d3-a456-426614174000"),
        )
        players = Players(app)

        avatar_uri = players.avatar_uri("Alice")

        self.assertEqual(avatar_uri, "https://mc-heads.net/avatar/123e4567-e89b-12d3-a456-426614174000/32")
        app.name_cache.resolve_game_alias_to_id.assert_called_once_with("Alice", "minecraft")
        app.name_cache.get_game_uuid.assert_called_once_with(42, "minecraft")

    def test_player_avatar_uri_prefers_live_uuid_before_name_cache(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app.name_cache = SimpleNamespace(
            resolve_game_alias_to_id=Mock(return_value=42),
            get_game_uuid=Mock(return_value="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        )
        players = Players(app)
        players.note_uuid("Alice", "123e4567-e89b-12d3-a456-426614174000")

        avatar_uri = players.avatar_uri("Alice")

        self.assertEqual(avatar_uri, "https://mc-heads.net/avatar/123e4567-e89b-12d3-a456-426614174000/32")
        app.name_cache.resolve_game_alias_to_id.assert_not_called()
        app.name_cache.get_game_uuid.assert_not_called()

    async def test_match_advancement_relays_modern_advancement_lines(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg(relay_advancements=True)
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app.manage_embed_color = 0x22C55E
        app._tail_machers = set()
        app._players = Players(app)
        app._players.note_uuid("Alice", "123e4567-e89b-12d3-a456-426614174000")
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_advancement(
                "[12:00:00] [Server thread/INFO]: Alice has made the advancement [Stone Age]"
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "Alice")
        self.assertEqual(relayed_message.content, "Advancement: Stone Age")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Advancement")
        self.assertEqual(relayed_message.relay_embed.description, "Stone Age")
        self.assertEqual(relayed_message.relay_embed.color, 0x22C55E)
        self.assertEqual(
            relayed_message.player_avatar_uri,
            "https://mc-heads.net/avatar/123e4567-e89b-12d3-a456-426614174000/32",
        )

    async def test_match_advancement_uses_native_title_when_kubejs_stream_is_available(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            script_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("// managed", encoding="utf-8")

            app = cast(Any, object.__new__(Minecraft))
            app.cfg = _make_minecraft_cfg(relay_advancements=True)
            app.name = "minecraft_demo"
            app.scope = "minecraft"
            app.manage_embed_color = 0x22C55E
            app.directory = directory
            app.mods = SimpleNamespace(
                list_mods=lambda state=None: [SimpleNamespace(name="kubejs-forge-2001.6.5-build.26.jar")]
                if state is not False
                else []
            )
            app._kubejs_event_stream_ready = True
            app._tail_machers = set()
            app._players = Players(app)
            matcher = Matchers(app)

            with patch("apps.minecraft.DC_Relay.add") as add_mock:
                await matcher.match_advancement(
                    "[12:00:00] [Server thread/INFO]: Alice has made the advancement [Diorite Stairs]"
                )

            add_mock.assert_called_once()
            relayed_message = add_mock.call_args.args[0]
            self.assertEqual(relayed_message.content, "Advancement: Diorite Stairs")
            assert relayed_message.relay_embed is not None
            self.assertEqual(relayed_message.relay_embed.description, "Diorite Stairs")

    async def test_match_advancement_normalises_goal_and_challenge_lines_to_advancement_term(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg(relay_advancements=True)
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app.manage_embed_color = 0x22C55E
        app._tail_machers = set()
        app._players = Players(app)
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_advancement(
                "[12:00:00] [Server thread/INFO]: Alice has reached the goal [Into Fire]"
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.content, "Advancement: Into Fire")
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Advancement")
        self.assertEqual(relayed_message.relay_embed.description, "Into Fire")

    async def test_match_advancement_respects_disabled_toggle(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = _make_minecraft_cfg(relay_advancements=False)
        app.name = "minecraft_demo"
        app.scope = "minecraft"
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_advancement(
                "[12:00:00] [Server thread/INFO]: Alice has completed the challenge [Monsters Hunted]"
            )

        add_mock.assert_not_called()

    async def test_receiver_wraps_media_urls_with_chatimage_code(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "look https://cdn.example.com/cat.png",
            "Erin",
        )
        payload.urls = {
            URLish(
                url="https://cdn.example.com/cat.png",
                label="Cat",
                type="image/png",
                is_media=True,
                extension="png",
                orig_url="https://cdn.example.com/cat.png",
            )
        }

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn("<Erin> look [[CICode,url=https://cdn.example.com/cat.png,name=Cat]] ", sent_command)

    async def test_receiver_wraps_discord_cdn_media_urls_with_query_params(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        discord_url = (
            "https://cdn.discordapp.com/attachments/1490793757975642182/1511051134536781914/"
            "Screenshot_2026-06-01_at_09.57.38.png?ex=6a1f0b88&is=6a1dba08&hm=5dce79a889c3d71b6ae8147b46456c94ba3a09522abf84b018099a1e74850587"
        )
        payload = App_Bound(
            _make_textable_channel(),
            f"look {discord_url}",
            "Erin",
            enrich=False,
        )

        with patch.object(Message, "_resolve_url_metadata", new=AsyncMock(return_value=(discord_url, "image/png"))):
            payload.urls = await payload.find_urls()

        self.assertEqual(len(payload.urls), 1)
        self.assertEqual(next(iter(payload.urls)).extension, "png")

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(f"[[CICode,url={discord_url}]]", sent_command)

    async def test_receiver_prefers_signed_original_discord_cdn_url_for_chatimage(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        unsigned_url = "https://cdn.discordapp.com/attachments/1376310685478162562/1492140591507705896/Ame_bounce-3.gif"
        signed_url = (
            "https://cdn.discordapp.com/attachments/1376310685478162562/1492140591507705896/"
            "Ame_bounce-3.gif?ex=6a1f7677&is=6a1e24f7&hm=469fe19e319df17aa0de313b1d915441c900ec8befe807bb5f861eea67ac4c28"
        )
        payload = App_Bound(
            _make_textable_channel(),
            f"look {signed_url}",
            "Erin",
        )
        payload.urls = {
            URLish(
                url=unsigned_url,
                label="Ame bounce",
                type="image/gif",
                is_media=True,
                extension="gif",
                orig_url=signed_url,
            )
        }

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(f"[[CICode,url={signed_url},name=Ame bounce]]", sent_command)
        self.assertNotIn(f"[[CICode,url={unsigned_url},name=Ame bounce]]", sent_command)

    async def test_receiver_wraps_giphy_page_urls_with_preview_chatimage(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "look https://giphy.com/gifs/reaction-mood-OkJat1YNdoD3W",
            "Erin",
        )
        payload.urls = {
            URLish(
                url="https://media1.giphy.com/media/OkJat1YNdoD3W/giphy-preview.gif",
                label="Mood",
                type="image/gif",
                is_media=True,
                extension="gif",
                orig_url="https://giphy.com/gifs/reaction-mood-OkJat1YNdoD3W",
                provider=MediaProvider.GIPHY,
            )
        }

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(
            "[[CICode,url=https://media1.giphy.com/media/OkJat1YNdoD3W/giphy-preview.gif,name=Mood]]",
            sent_command,
        )

    async def test_receiver_wraps_klipy_media_urls_with_chatimage(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "look https://klipy.com/gifs/bluey-dog-1",
            "Erin",
        )
        payload.urls = {
            URLish(
                url="https://static.klipy.com/ii/example/path/bluey.gif",
                label="Bluey Dog Dancing",
                type="image/gif",
                is_media=True,
                extension="gif",
                orig_url="https://klipy.com/gifs/bluey-dog-1",
                provider=MediaProvider.KLIPY,
            )
        }

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(
            "[[CICode,url=https://static.klipy.com/ii/example/path/bluey.gif,name=Bluey Dog Dancing]]",
            sent_command,
        )

    async def test_receiver_wraps_tenor_links_with_multiple_gif_variants(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "look https://tenor.com/view/jinx-meme-approaching-close-coming-gif-27652940",
            "Erin",
        )
        payload.urls = {
            URLish(
                url="https://media1.tenor.com/m/example.gif",
                label="Jinx",
                type="image/gif",
                is_media=True,
                extension="gif",
                orig_url="https://tenor.com/view/jinx-meme-approaching-close-coming-gif-27652940",
                provider=MediaProvider.TENOR,
                variants=(
                    URLVariant(
                        key="gif",
                        label="original",
                        url="https://media1.tenor.com/m/example.gif",
                        type="image/gif",
                        extension="gif",
                    ),
                    URLVariant(
                        key="mediumgif",
                        label="mediumgif",
                        url="https://media1.tenor.com/m/example-medium.gif",
                        type="image/gif",
                        extension="gif",
                    ),
                    URLVariant(
                        key="tinygif",
                        label="tinygif",
                        url="https://media.tenor.com/example-tiny.gif",
                        type="image/gif",
                        extension="gif",
                    ),
                ),
            )
        }

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(
            "[[CICode,url=https://media1.tenor.com/m/example.gif,name=original]]",
            sent_command,
        )
        self.assertIn(
            "[[CICode,url=https://media1.tenor.com/m/example-medium.gif,name=mediumgif]]",
            sent_command,
        )
        self.assertIn(
            "[[CICode,url=https://media.tenor.com/example-tiny.gif,name=tinygif]]",
            sent_command,
        )
        self.assertNotIn("https://tenor.com/view/jinx-meme-approaching-close-coming-gif-27652940", sent_command)

    async def test_receiver_leaves_tenor_links_as_plain_urls_without_multiple_gif_variants(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "look https://tenor.com/view/jinx-meme-approaching-close-coming-gif-27652940",
            "Erin",
        )
        payload.urls = {
            URLish(
                url="https://media1.tenor.com/m/example.gif",
                label="Jinx",
                type="image/gif",
                is_media=True,
                extension="gif",
                orig_url="https://tenor.com/view/jinx-meme-approaching-close-coming-gif-27652940",
                provider=MediaProvider.TENOR,
                variants=(
                    URLVariant(
                        key="gif",
                        label="original",
                        url="https://media1.tenor.com/m/example.gif",
                        type="image/gif",
                        extension="gif",
                    ),
                ),
            )
        }

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(
            "https://tenor.com/view/jinx-meme-approaching-close-coming-gif-27652940",
            sent_command,
        )
        self.assertNotIn("[[CICode,url=https://media1.tenor.com/m/example.gif,name=original]]", sent_command)

    def test_giphy_page_urls_resolve_to_preview_gifs(self) -> None:
        resolved = Message._resolve_giphy_media_url("https://giphy.com/gifs/reaction-mood-OkJat1YNdoD3W")

        self.assertEqual(resolved, "https://media1.giphy.com/media/OkJat1YNdoD3W/giphy-preview.gif")

    def test_extract_klipy_media_url_from_html(self) -> None:
        raw_html = """
        <html>
          <body>
            <meta property="og:image" content="https://static.klipy.com/ii/example/path/fancy.gif" />
          </body>
        </html>
        """

        resolved = Message._extract_klipy_media_url_from_html(raw_html)

        self.assertEqual(resolved, "https://static.klipy.com/ii/example/path/fancy.gif")

    async def test_receiver_exposes_attached_images_via_public_uploads_for_chatimage(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "",
            "Erin",
            [Fileish("/tmp/cat.png", "cat.png")],
        )

        with patch(
            "_discord.Utilities.linkify", return_value=("https://public.example/uploads/cat.png", Path("/tmp/cat.png"))
        ):
            await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn("[[CICode,url=https://public.example/uploads/cat.png,name=cat.png]]", sent_command)
        self.assertNotIn("file:///", sent_command)

    async def test_receiver_prefers_original_discord_attachment_url_for_chatimage(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "",
            "Erin",
            [Fileish("/tmp/cat.png", "cat.png", source_url="https://cdn.discordapp.com/attachments/cat.png")],
        )

        with patch(
            "_discord.Utilities.linkify", return_value=("https://public.example/uploads/cat.png", Path("/tmp/cat.png"))
        ):
            await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn(
            "[[CICode,url=https://cdn.discordapp.com/attachments/cat.png,name=cat.png]]",
            sent_command,
        )
        self.assertNotIn("https://public.example/uploads/cat.png", sent_command)

    async def test_receiver_prefixes_reply_indicator(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "look",
            "Erin",
            reference_kind=RelayMessageReferenceKind.REPLY,
        )

        await receiver.send(payload)

        sent_command = app._relay.send.await_args.args[0]
        self.assertIn("<Erin> reply; look ", sent_command)

    async def test_receiver_resolves_discord_user_mentions_for_target_app(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app._relay = SimpleNamespace(send=AsyncMock())
        app.scope = "minecraft"
        app.name_cache = SimpleNamespace(relay_mention_name=Mock(return_value="AliceGame"))
        receiver = Receiver(app)
        payload = App_Bound(
            _make_textable_channel(),
            "hi <@456>",
            "Erin",
            source_guild_id=hikari.Snowflake(123),
        )

        await receiver.send(payload)

        app.name_cache.relay_mention_name.assert_called_once_with(
            456,
            scope="minecraft",
            platforms=(),
            preferred_platform=None,
            preferred_guild_id=hikari.Snowflake(123),
        )
        sent_command = app._relay.send.await_args.args[0]
        self.assertIn("<Erin> hi @AliceGame ", sent_command)

    def test_minecraft_supports_relay_system_notices(self) -> None:
        app = object.__new__(Minecraft)
        app.am_receiver = _DummyReceiver()
        app.chat_relay_outbound = True

        self.assertTrue(app.supports_relay_system_notices)

    async def test_resolution_failure_does_not_send_notice_to_app(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        DC_Relay._resolution_miss_counts.clear()

        receiver = SimpleNamespace(send=AsyncMock())
        app = SimpleNamespace(
            name="minecraft_demo",
            scope="minecraft",
            supports_relay_system_notices=True,
            _running=True,
            am_receiver=receiver,
            cfg=SimpleNamespace(chat_ignore_symbol="!"),
        )
        message = _make_resolution_message(
            app=app,
            player="Erin",
            status=config.NameResolutionStatus.NOT_FOUND,
        )

        with patch("_discord.log.warning") as warning_mock:
            await relay._notify_resolution_failure(message)
            receiver.send.assert_not_awaited()

            await relay._notify_resolution_failure(message)

        warning_mock.assert_called()
        receiver.send.assert_not_awaited()

    async def test_on_dc_message_ignores_channel_pinned_system_messages(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay.reso = Mock()
        setattr(cast(Any, relay), "names", _NamesStub())
        relay.resolve_channel = AsyncMock()
        relay.seen_messages_id = set()

        receiver = SimpleNamespace(send=AsyncMock())
        app = Mock()
        app._running = True
        app.am_receiver = receiver
        channel_id = hikari.Snowflake(1)
        DC_Relay._chat_channels = {channel_id: {app}}

        ctx = SimpleNamespace(
            is_human=True,
            channel_id=channel_id,
            content=None,
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(123),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                type=hikari.MessageType.CHANNEL_PINNED_MESSAGE,
            ),
        )

        await relay.on_dc_message(cast(Any, ctx))

        receiver.send.assert_not_awaited()
        relay.resolve_channel.assert_not_awaited()
        self.assertEqual(relay.seen_messages_id, set())

    async def test_on_dc_message_keeps_discord_chat_active_when_app_is_stopped(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay.chat_hub = ChatHub()
        relay._channel_objects = {}
        setattr(cast(Any, relay), "names", _NamesStub())
        source_channel = _make_textable_channel()
        relay.resolve_channel = AsyncMock(return_value=source_channel)
        relay.seen_messages_id = set()

        receiver = SimpleNamespace(send=AsyncMock())
        app = _StoppedRelayApp(receiver)
        source_channel_id = hikari.Snowflake(1)
        target_send = AsyncMock(return_value=SimpleNamespace(id=hikari.Snowflake(999)))
        target_channel = cast(
            hikari.TextableChannel,
            cast(
                object,
                SimpleNamespace(
                    id=hikari.Snowflake(2),
                    guild_id=None,
                    send=target_send,
                ),
            ),
        )
        relay._channel_objects[hikari.Snowflake(2)] = target_channel
        DC_Relay._chat_channels = {source_channel_id: {cast(Any, app)}}
        DC_Relay._chat_apps = {app.name: cast(Any, app)}
        relay.chat_hub.clear_room(app.name)
        relay.chat_hub.bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), app.friendly))
        relay.chat_hub.bind(app.name, ChatEndpoint(ChatEndpointId.discord_channel(2), "Discord 2"))

        ctx = SimpleNamespace(
            is_human=True,
            channel_id=source_channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=None,
            author=None,
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                stickers=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        history: tuple[ChatEvent, ...] = ()
        try:
            await relay.on_dc_message(cast(Any, ctx))
            history = relay.chat_hub.history(app.name)
        finally:
            DC_Relay._chat_channels.clear()
            DC_Relay._chat_apps.clear()
            relay.chat_hub.clear_room(app.name)

        receiver.send.assert_not_awaited()
        target_send.assert_awaited_once()
        await_args = target_send.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        self.assertEqual(await_args.kwargs["content"], "<456>\nhello")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].content, "hello")

    async def test_send_dc_includes_relay_embed_payload(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        setattr(relay, "names", _NamesStub())
        relay._channel_objects = {}
        relay.resolve_channel = AsyncMock()
        relay._notify_resolution_failure = AsyncMock()

        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.chat_channel = hikari.Snowflake(1)

        send_mock = AsyncMock(return_value=SimpleNamespace(channel_id=hikari.Snowflake(1)))
        channel = cast(
            hikari.TextableChannel,
            cast(
                object,
                SimpleNamespace(
                    id=hikari.Snowflake(1),
                    guild_id=None,
                    send=send_mock,
                ),
            ),
        )
        relay._channel_objects[app.chat_channel] = channel

        message = DC_Bound(
            app,
            "Advancement: Stone Age",
            "Alice",
            relay_embed=RelayEmbedPayload(
                title="Advancement",
                description="Stone Age",
                color=0x22C55E,
            ),
        )

        await relay._send_dc(message)

        await_args = send_mock.await_args
        if await_args is None:
            raise AssertionError("Expected relay send to be awaited.")
        send_kwargs = await_args.kwargs
        self.assertEqual(send_kwargs["content"], "<Alice>")
        self.assertIn("embeds", send_kwargs)
        embed = send_kwargs["embeds"][0]
        self.assertEqual(embed.title, "Advancement")
        self.assertEqual(embed.description, "Stone Age")
        self.assertEqual(embed.color, 0x22C55E)

    async def test_send_dc_preserves_explicit_system_embed_title_and_description(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        setattr(relay, "names", _NamesStub())
        relay._channel_objects = {}
        relay.resolve_channel = AsyncMock()
        relay._notify_resolution_failure = AsyncMock()

        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.chat_channel = hikari.Snowflake(1)

        send_mock = AsyncMock(return_value=SimpleNamespace(channel_id=hikari.Snowflake(1)))
        channel = cast(
            hikari.TextableChannel,
            cast(
                object,
                SimpleNamespace(
                    id=hikari.Snowflake(1),
                    guild_id=None,
                    send=send_mock,
                ),
            ),
        )
        relay._channel_objects[app.chat_channel] = channel

        message = DC_Bound(
            app,
            "Started",
            "System",
            relay_embed=RelayEmbedPayload(
                title="Minecraft Demo Started",
                description="Join: `play.example.com:25565`\n[Squaremap](https://maps.example.com:8123/?world=minecraft_overworld)",
                color=0x22C55E,
            ),
        )

        await relay._send_dc(message)

        await_args = send_mock.await_args
        if await_args is None:
            raise AssertionError("Expected relay send to be awaited.")
        send_kwargs = await_args.kwargs
        self.assertIs(send_kwargs["content"], hikari.UNDEFINED)
        self.assertIn("embeds", send_kwargs)
        embed = send_kwargs["embeds"][0]
        self.assertEqual(embed.title, "Minecraft Demo Started")
        self.assertEqual(
            embed.description,
            "Join: `play.example.com:25565`\n[Squaremap](https://maps.example.com:8123/?world=minecraft_overworld)",
        )
        self.assertEqual(embed.color, 0x22C55E)

    async def test_send_dc_synthesises_join_embed_for_generic_notices(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        setattr(relay, "names", _NamesStub())
        relay._channel_objects = {}
        relay.resolve_channel = AsyncMock()
        relay._notify_resolution_failure = AsyncMock()

        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.chat_channel = hikari.Snowflake(1)
        app.manage_embed_color = 0x22C55E

        send_mock = AsyncMock(return_value=SimpleNamespace(channel_id=hikari.Snowflake(1)))
        channel = cast(
            hikari.TextableChannel,
            cast(
                object,
                SimpleNamespace(
                    id=hikari.Snowflake(1),
                    guild_id=None,
                    send=send_mock,
                ),
            ),
        )
        relay._channel_objects[app.chat_channel] = channel

        message = DC_Bound(
            app,
            "Alice joined Minecraft Demo",
            "Alice",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        await relay._send_dc(message)

        await_args = send_mock.await_args
        if await_args is None:
            raise AssertionError("Expected relay send to be awaited.")
        send_kwargs = await_args.kwargs
        self.assertIs(send_kwargs["content"], hikari.UNDEFINED)
        self.assertIn("embeds", send_kwargs)
        embed = send_kwargs["embeds"][0]
        self.assertEqual(embed.title, "Minecraft Demo")
        self.assertEqual(embed.description, "Joined Alice")
        self.assertEqual(embed.color, 0x22C55E)

    async def test_send_dc_synthesises_join_embed_without_client_pack_details(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        setattr(relay, "names", _NamesStub())
        relay._channel_objects = {}
        relay.resolve_channel = AsyncMock()
        relay._notify_resolution_failure = AsyncMock()

        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.chat_channel = hikari.Snowflake(1)
        app.manage_embed_color = 0x22C55E

        send_mock = AsyncMock(return_value=SimpleNamespace(channel_id=hikari.Snowflake(1)))
        channel = cast(
            hikari.TextableChannel,
            cast(
                object,
                SimpleNamespace(
                    id=hikari.Snowflake(1),
                    guild_id=None,
                    send=send_mock,
                ),
            ),
        )
        relay._channel_objects[app.chat_channel] = channel

        message = DC_Bound(
            app,
            "Alice joined Minecraft Demo",
            "Alice",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
                pack_version="2026-07-04",
                has_unpublished_pack_changes=True,
            ),
        )

        await relay._send_dc(message)

        await_args = send_mock.await_args
        if await_args is None:
            raise AssertionError("Expected relay send to be awaited.")
        embed = await_args.kwargs["embeds"][0]
        self.assertEqual(embed.description, "Joined Alice")

    async def test_send_dc_queues_relay_tts_when_target_matches_channel(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        setattr(relay, "names", _NamesStub())
        relay._channel_objects = {}
        relay.resolve_channel = AsyncMock()
        relay._notify_resolution_failure = AsyncMock()
        relay_tts = _RelayTTSStub(
            target=config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(123),
                voice_channel=hikari.Snowflake(456),
                primary_tts_channel=hikari.Snowflake(2),
                secondary_tts_channel=hikari.Snowflake(1),
                secondary_tts_listen_enabled=True,
                relay_tts_enabled=True,
            )
        )
        relay._voice_tts = relay_tts

        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.chat_channel = hikari.Snowflake(1)

        channel = _make_send_channel(
            channel_id=1,
            guild_id=123,
            send_result=SimpleNamespace(id=hikari.Snowflake(999), channel_id=hikari.Snowflake(1)),
        )
        relay._channel_objects[app.chat_channel] = channel

        message = DC_Bound(app, "Stone Age", "Alice")
        message.player_id = 42

        await relay._send_dc(message)

        relay_tts.queue_relay_message_mock.assert_awaited_once()
        await_args = relay_tts.queue_relay_message_mock.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        self.assertEqual(await_args.args[:2], (hikari.Snowflake(123), hikari.Snowflake(1)))
        self.assertIsInstance(await_args.args[2], hikari.Snowflake)
        self.assertEqual(await_args.args[3], "Stone Age")
        self.assertEqual(await_args.kwargs, {"user_id": 42})

    async def test_send_dc_logs_relay_tts_skip_when_tts_channel_differs(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        setattr(relay, "names", _NamesStub())
        relay._channel_objects = {}
        relay.resolve_channel = AsyncMock()
        relay._notify_resolution_failure = AsyncMock()
        relay_tts = _RelayTTSStub(
            target=config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(123),
                voice_channel=hikari.Snowflake(456),
                primary_tts_channel=hikari.Snowflake(2),
                relay_tts_enabled=True,
            )
        )
        relay_tts.queue_relay_message_mock.side_effect = RuntimeError(
            "Relay message was not posted in an active TTS channel."
        )
        relay._voice_tts = relay_tts

        app = cast(Any, object.__new__(Minecraft))
        app.name = "minecraft_demo"
        app.friendly = "Minecraft Demo"
        app.scope = "minecraft"
        app.chat_channel = hikari.Snowflake(1)

        channel = _make_send_channel(
            channel_id=1,
            guild_id=123,
            send_result=SimpleNamespace(id=hikari.Snowflake(999), channel_id=hikari.Snowflake(1)),
        )
        relay._channel_objects[app.chat_channel] = channel

        message = DC_Bound(app, "Stone Age", "Alice")
        message.player_id = 42

        with patch("_discord.tts_log.info") as info_mock:
            await relay._send_dc(message)

        relay_tts.queue_relay_message_mock.assert_awaited_once()
        await_args = relay_tts.queue_relay_message_mock.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        self.assertEqual(await_args.args[:2], (hikari.Snowflake(123), hikari.Snowflake(1)))
        self.assertIsInstance(await_args.args[2], hikari.Snowflake)
        self.assertEqual(await_args.args[3], "Stone Age")
        self.assertEqual(await_args.kwargs, {"user_id": 42})
        matching_calls = [
            call.args
            for call in info_mock.call_args_list
            if call.args
            and call.args[0] == "Relay TTS skipped app=%s guild=%s channel=%s player=%r user_id=%s reason=%s"
        ]
        self.assertEqual(len(matching_calls), 1)
        _, app_name, guild_id, channel_id, player, user_id, reason = matching_calls[0]
        self.assertEqual(app_name, "minecraft_demo")
        self.assertEqual(guild_id, 123)
        self.assertEqual(channel_id, 1)
        self.assertEqual(player, "Alice")
        self.assertEqual(user_id, 42)
        self.assertEqual(str(reason), "Relay message was not posted in an active TTS channel.")


if __name__ == "__main__":
    unittest.main()
