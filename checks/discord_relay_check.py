from __future__ import annotations

import asyncio
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import hikari
from hikari import messages as hikari_messages

import config
from _discord import (
    App_Bound,
    DC_Bound,
    DC_Relay,
    Message,
    RelayWorkerStatus,
    URLVariant,
    normalise_attachment_relay_name,
)
from _file import File_Utils
from _minecraft_heads import (
    MinecraftDefaultSkin,
    minecraft_avatar_uri,
    minecraft_default_skin_for_dev_bypass_user,
    minecraft_dev_bypass_head_data_uri,
)
from _security import Access_Control, Power_Level
from chat_hub import (
    ChatAttachment,
    ChatAuthor,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpoint,
    ChatEndpointId,
    ChatEndpointKind,
    ChatEvent,
    ChatHub,
    ChatLink,
    ChatLinkVariant,
    ChatMessageReference,
    ChatReferenceKind,
)
from relay_notices import GameDeathKind, GameDeathNotice, PlayerSessionAction, PlayerSessionNotice, RelayNoticeSource


def _make_attachment(*, title: str, filename: str, media_type: str | None) -> hikari.Attachment:
    return cast(
        hikari.Attachment,
        cast(
            object,
            SimpleNamespace(
                title=title,
                filename=filename,
                media_type=media_type,
                app=cast(Any, object()),
                id=hikari.Snowflake(1),
                url="https://example.invalid/file",
                proxy_url="https://example.invalid/file",
                size=1,
                is_ephemeral=False,
            ),
        ),
    )


def _make_textable_channel(
    *,
    channel_id: hikari.Snowflake,
    name: str,
) -> hikari.TextableChannel:
    return hikari.TextableChannel(
        app=cast(Any, object()),
        id=channel_id,
        name=name,
        type=1,
    )


class DiscordRelayAttachmentNameTests(unittest.TestCase):
    def test_attachment_name_prefers_human_title_stem_but_keeps_filename_extension(self) -> None:
        attachment = _make_attachment(title="Sweet Dreams", filename="SPOILER_abc123.png", media_type="image/png")

        result = normalise_attachment_relay_name(attachment)

        self.assertEqual(result, "Sweet Dreams.png")

    def test_attachment_name_sanitises_chatimage_unsafe_characters(self) -> None:
        attachment = _make_attachment(title="jinx, [close up]", filename="jinx-close-up.gif", media_type="image/gif")

        result = normalise_attachment_relay_name(attachment)

        self.assertEqual(result, "jinx close up.gif")

    def test_attachment_name_falls_back_to_media_type_extension_when_needed(self) -> None:
        attachment = _make_attachment(title="cat photo", filename="upload", media_type="image/jpeg")

        result = normalise_attachment_relay_name(attachment)

        self.assertEqual(result, "cat photo.jpg")

    def test_tenor_store_cache_exposes_multiple_gif_variants(self) -> None:
        raw_html = """
        <html>
          <body>
            <script id="store-cache" type="text/x-cache">
              {
                "gifs": {
                  "byId": {
                    "13317275": {
                      "results": [
                        {
                          "media_formats": {
                            "gif": {
                              "url": "https://media1.tenor.com/m/original.gif",
                              "duration": 7.2,
                              "dims": [498, 278],
                              "size": 7847765
                            },
                            "mediumgif": {
                              "url": "https://media1.tenor.com/m/medium.gif",
                              "duration": 7.2,
                              "dims": [498, 278],
                              "size": 7024000
                            },
                            "tinygif": {
                              "url": "https://media.tenor.com/tiny.gif",
                              "duration": 7.2,
                              "dims": [220, 123],
                              "size": 49492
                            },
                            "gifpreview": {
                              "url": "https://media.tenor.com/preview.png",
                              "duration": 0,
                              "dims": [498, 278],
                              "size": 241
                            }
                          }
                        }
                      ]
                    }
                  }
                }
              }
            </script>
          </body>
        </html>
        """

        variants = Message._tenor_gif_variants_from_store_cache(
            raw_html,
            "https://tenor.com/view/anime-nope-tired-sandbox-gif-13317275",
        )

        self.assertEqual([variant.label for variant in variants], ["original", "mediumgif", "tinygif"])
        self.assertEqual(variants[0].size_bytes, 7847765)
        self.assertEqual((variants[2].width, variants[2].height), (220, 123))


class DiscordRelayWebChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_from_dc_bound_preserves_explicit_player_id(self) -> None:
        message = DC_Bound(
            cast(Any, SimpleNamespace(name="minecraft_alpha", scope="minecraft")),
            "Alice joined minecraft_alpha",
            "Alice",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
            player_id=42,
        )

        event = DC_Relay._event_from_dc_bound(message)

        self.assertEqual(event.author.display_name, "Alice")
        self.assertEqual(event.author.id, "42")
        self.assertEqual(event.author.discord_user_id, 42)
        self.assertIsInstance(event.notice, PlayerSessionNotice)
        assert isinstance(event.notice, PlayerSessionNotice)
        self.assertIs(event.notice.action, PlayerSessionAction.JOINED)

    def test_minecraft_dev_bypass_skin_mapping_matches_expected_levels(self) -> None:
        self.assertEqual(
            minecraft_default_skin_for_dev_bypass_user(Access_Control.dev_bypass_user_id(Power_Level.root)),
            MinecraftDefaultSkin.ALEX,
        )
        self.assertEqual(
            minecraft_default_skin_for_dev_bypass_user(Access_Control.dev_bypass_user_id(Power_Level.sudo)),
            MinecraftDefaultSkin.ARI,
        )
        self.assertEqual(
            minecraft_default_skin_for_dev_bypass_user(Access_Control.dev_bypass_user_id(Power_Level.admin)),
            MinecraftDefaultSkin.KAI,
        )
        self.assertEqual(
            minecraft_default_skin_for_dev_bypass_user(Access_Control.dev_bypass_user_id(Power_Level.user)),
            MinecraftDefaultSkin.NOOR,
        )
        self.assertEqual(
            minecraft_default_skin_for_dev_bypass_user(Access_Control.dev_bypass_user_id(Power_Level.visitor)),
            MinecraftDefaultSkin.STEVE,
        )
        self.assertEqual(
            minecraft_default_skin_for_dev_bypass_user(Access_Control.dev_bypass_user_id(Power_Level.guest)),
            MinecraftDefaultSkin.SUNNY,
        )

    async def test_publish_web_chat_delivers_web_session_event(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered

        event = await relay.publish_web_chat(
            room_id="minecraft_alpha",
            session_id="session-1",
            author_display_name="Tester",
            author_id="42",
            discord_user_id=42,
            content="  hello from web  ",
        )

        self.assertEqual(event.room_id, "minecraft_alpha")
        self.assertEqual(event.source.kind, ChatEndpointKind.WEB_SESSION)
        self.assertEqual(event.source.value, "session-1")
        self.assertEqual(event.author.kind, ChatAuthorKind.WEB_USER)
        self.assertEqual(event.author.display_name, "Tester")
        self.assertEqual(event.author.discord_user_id, 42)
        self.assertIsNone(event.author.avatar_uri)
        self.assertEqual(event.content, "hello from web")
        delivered.assert_awaited_once_with(event)

    async def test_publish_web_chat_uses_minecraft_default_head_for_dev_bypass_user(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        cast(Any, relay)._chat_apps = {"minecraft_alpha": SimpleNamespace(scope="minecraft")}
        discord_user_id = Access_Control.dev_bypass_user_id(Power_Level.root)

        event = await relay.publish_web_chat(
            room_id="minecraft_alpha",
            session_id="session-1",
            author_display_name="Tester",
            author_id=str(discord_user_id),
            discord_user_id=discord_user_id,
            content="hello from web",
        )

        self.assertEqual(event.author.avatar_uri, minecraft_dev_bypass_head_data_uri(discord_user_id))
        delivered.assert_awaited_once_with(event)

    async def test_publish_web_chat_uses_minecraft_head_for_known_game_uuid(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                scope="minecraft",
                name_cache=SimpleNamespace(
                    get_game_uuid=Mock(return_value="123e4567-e89b-12d3-a456-426614174000"),
                    get_game_alias=Mock(return_value="AliceGame"),
                ),
            )
        }

        event = await relay.publish_web_chat(
            room_id="minecraft_alpha",
            session_id="session-1",
            author_display_name="Tester",
            author_id="42",
            discord_user_id=42,
            content="hello from web",
        )

        self.assertEqual(event.author.avatar_uri, minecraft_avatar_uri("123e4567-e89b-12d3-a456-426614174000"))
        delivered.assert_awaited_once_with(event)

    async def test_publish_web_chat_uses_minecraft_head_for_known_game_alias_without_uuid(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                scope="minecraft",
                name_cache=SimpleNamespace(
                    get_game_uuid=Mock(return_value=None),
                    get_game_alias=Mock(return_value="AliceGame"),
                ),
            )
        }

        event = await relay.publish_web_chat(
            room_id="minecraft_alpha",
            session_id="session-1",
            author_display_name="Tester",
            author_id="42",
            discord_user_id=42,
            content="hello from web",
        )

        self.assertEqual(event.author.avatar_uri, minecraft_avatar_uri("AliceGame"))
        delivered.assert_awaited_once_with(event)

    async def test_publish_web_chat_does_not_use_default_head_outside_minecraft(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        cast(Any, relay)._chat_apps = {"factorio_lab": SimpleNamespace(scope="factorio")}
        discord_user_id = Access_Control.dev_bypass_user_id(Power_Level.root)

        event = await relay.publish_web_chat(
            room_id="factorio_lab",
            session_id="session-1",
            author_display_name="Tester",
            author_id=str(discord_user_id),
            discord_user_id=discord_user_id,
            content="hello from web",
        )

        self.assertIsNone(event.author.avatar_uri)
        delivered.assert_awaited_once_with(event)

    async def test_publish_chat_event_delivers_existing_event(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        cast(Any, relay)._chat_apps = {}
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Yoko joined minecraft_alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        result = await relay.publish_chat_event(event=event)

        self.assertIs(result, event)
        delivered.assert_awaited_once_with(event, fallback_app=None)

    async def test_publish_web_chat_dispatches_to_configured_relay_loop(self) -> None:
        relay = object.__new__(DC_Relay)
        relay_loop = asyncio.new_event_loop()
        delivered_loops: list[asyncio.AbstractEventLoop] = []

        async def deliver(event: ChatEvent) -> None:
            del event
            delivered_loops.append(asyncio.get_running_loop())

        cast(Any, relay)._relay_loop = relay_loop
        cast(Any, relay)._deliver_chat_event = deliver
        thread = threading.Thread(target=relay_loop.run_forever, daemon=True)
        thread.start()
        try:
            await relay.publish_web_chat(
                room_id="minecraft_alpha",
                session_id="session-1",
                author_display_name="Tester",
                author_id="42",
                discord_user_id=42,
                content="hello from web",
            )
        finally:
            relay_loop.call_soon_threadsafe(relay_loop.stop)
            thread.join(timeout=2)
            relay_loop.close()

        self.assertEqual(delivered_loops, [relay_loop])

    async def test_publish_web_chat_preserves_detected_links(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        with patch.object(
            Message,
            "_resolve_url_metadata",
            new=AsyncMock(return_value=("https://example.invalid/cat.png", "image/png")),
        ):
            event = await relay.publish_web_chat(
                room_id="minecraft_alpha",
                session_id="session-1",
                author_display_name="Tester",
                author_id="42",
                discord_user_id=42,
                content="look https://example.invalid/cat.png",
            )

        self.assertEqual(tuple(link.url for link in event.links), ("https://example.invalid/cat.png",))
        self.assertTrue(event.links[0].is_media)

    async def test_publish_web_chat_logs_tenor_variants(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        cast(Any, relay)._deliver_chat_event = delivered
        tenor_variants = (
            URLVariant(
                key="gif",
                label="original",
                url="https://media1.tenor.com/m/example.gif",
                type="image/gif",
                extension="gif",
                width=498,
                height=278,
                size_bytes=7_847_765,
                duration_seconds=7.2,
            ),
            URLVariant(
                key="tinygif",
                label="tinygif",
                url="https://media.tenor.com/example-tiny.gif",
                type="image/gif",
                extension="gif",
                width=220,
                height=123,
                size_bytes=49_492,
                duration_seconds=7.2,
            ),
        )
        with (
            patch.object(Message, "_resolve_tenor_media_variants", new=AsyncMock(return_value=tenor_variants)),
            patch.object(
                Message,
                "_resolve_url_metadata",
                new=AsyncMock(return_value=("https://media1.tenor.com/m/example.gif", "image/gif")),
            ),
            patch("_discord.tenor_log") as tenor_log_mock,
        ):
            event = await relay.publish_web_chat(
                room_id="minecraft_alpha",
                session_id="session-1",
                author_display_name="Tester",
                author_id="42",
                discord_user_id=42,
                content="look https://tenor.com/view/anime-nope-tired-sandbox-gif-13317275",
            )

        self.assertEqual([variant.label for variant in event.links[0].variants], ["original", "tinygif"])
        tenor_log_mock.assert_called_once()
        self.assertEqual(tenor_log_mock.call_args.args[0], "tenor_link")

    async def test_publish_web_chat_rejects_blank_content(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay)._deliver_chat_event = AsyncMock()

        with self.assertRaises(ValueError):
            await relay.publish_web_chat(
                room_id="minecraft_alpha",
                session_id="session-1",
                author_display_name="Tester",
                author_id="42",
                discord_user_id=42,
                content="  ",
            )

    async def test_publish_web_chat_replies_to_existing_history_event(self) -> None:
        relay = object.__new__(DC_Relay)
        delivered = AsyncMock()
        hub = ChatHub()
        room_id = "minecraft_alpha"
        original = ChatEvent(
            room_id=room_id,
            source=ChatEndpointId.app(room_id),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="hello there",
            id="event-1",
        )
        hub.clear_room(room_id)
        hub.publish(original)
        cast(Any, relay).chat_hub = hub
        cast(Any, relay)._deliver_chat_event = delivered
        cast(Any, relay)._chat_apps = {}

        try:
            event = await relay.publish_web_chat(
                room_id=room_id,
                session_id="session-1",
                author_display_name="Tester",
                author_id="42",
                discord_user_id=42,
                content="replying",
                reply_to_event_id="event-1",
            )
        finally:
            hub.clear_room(room_id)

        self.assertEqual(event.reference_kind, ChatReferenceKind.REPLY)
        self.assertIsNotNone(event.reference)
        assert event.reference is not None
        self.assertEqual(event.reference.author_display_name, "Yoko")
        self.assertEqual(event.reference.content, "hello there")
        delivered.assert_awaited_once_with(event)


class _RecordingReceiver:
    def __init__(self) -> None:
        self.payload: object | None = None

    async def send(self, payload: object) -> None:
        self.payload = payload


class DiscordRelayAppDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_chat_event_to_app_uses_display_name_when_discord_user_has_no_game_alias(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).bot = cast(hikari.GatewayBot, object())
        receiver = _RecordingReceiver()
        app = SimpleNamespace(
            _running=True,
            am_receiver=receiver,
            name_cache=SimpleNamespace(get_game_alias=Mock(return_value=None)),
            scope="minecraft",
        )
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(
                kind=ChatAuthorKind.WEB_USER,
                display_name="Tester",
                discord_user_id=42,
            ),
            content="hello",
        )

        await relay._send_chat_event_to_app(event, cast(Any, app))

        payload = receiver.payload
        self.assertIsNotNone(payload)
        self.assertEqual(getattr(payload, "player"), "Tester")

    async def test_send_chat_event_to_app_keeps_parsed_links_when_event_links_are_empty(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).bot = cast(hikari.GatewayBot, object())
        receiver = _RecordingReceiver()
        app = SimpleNamespace(
            _running=True,
            am_receiver=receiver,
            name_cache=SimpleNamespace(get_game_alias=Mock(return_value=None)),
            scope="minecraft",
        )
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Tester"),
            content="look https://example.invalid/cat.png",
        )
        with patch.object(
            Message,
            "_resolve_url_metadata",
            new=AsyncMock(return_value=("https://example.invalid/cat.png", "image/png")),
        ):
            await relay._send_chat_event_to_app(event, cast(Any, app))

        payload = receiver.payload
        self.assertIsNotNone(payload)
        self.assertEqual([link.url for link in getattr(payload, "urls")], ["https://example.invalid/cat.png"])

    async def test_send_chat_event_to_app_skips_reenrichment_when_event_links_are_present(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).bot = cast(hikari.GatewayBot, object())
        receiver = _RecordingReceiver()
        app = SimpleNamespace(
            _running=True,
            am_receiver=receiver,
            name_cache=SimpleNamespace(get_game_alias=Mock(return_value=None)),
            scope="minecraft",
        )
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Tester"),
            content="look https://example.invalid/cat.png",
            links=(
                ChatLink(
                    url="https://example.invalid/cat.png",
                    is_media=True,
                    extension=".png",
                    variants=(
                        ChatLinkVariant(
                            key="gif",
                            label="original",
                            url="https://media1.tenor.com/m/example.gif",
                            media_type="image/gif",
                            extension="gif",
                        ),
                    ),
                ),
            ),
        )

        with patch.object(Message, "find_urls", new=AsyncMock(return_value=set())) as enrich_mock:
            await relay._send_chat_event_to_app(event, cast(Any, app))

        payload = receiver.payload
        self.assertIsNotNone(payload)
        self.assertEqual([link.url for link in getattr(payload, "urls")], ["https://example.invalid/cat.png"])
        self.assertEqual([variant.label for variant in next(iter(getattr(payload, "urls"))).variants], ["original"])
        enrich_mock.assert_not_awaited()

    async def test_send_chat_event_to_app_renders_typed_notice_content(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).bot = cast(hikari.GatewayBot, object())
        receiver = _RecordingReceiver()
        app = SimpleNamespace(
            _running=True,
            am_receiver=receiver,
            friendly="Minecraft Alpha",
            name_cache=SimpleNamespace(get_game_alias=Mock(return_value=None)),
            scope="minecraft",
        )
        notice = PlayerSessionNotice(action=PlayerSessionAction.JOINED, source=RelayNoticeSource.WEB)
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(kind=ChatAuthorKind.WEB_USER, display_name="Tester"),
            content="placeholder",
            notice=notice,
        )

        await relay._send_chat_event_to_app(event, cast(Any, app))

        payload = receiver.payload
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.content, "Tester joined Minecraft Alpha")
        self.assertIs(payload.notice, notice)


class _NamesStub:
    def __init__(
        self,
        *,
        parsed_text: str | None = None,
        parsed_mentions: set[int] | None = None,
        relay_display_name: str | None = None,
        discord_fallback_name: str | None = None,
    ) -> None:
        self.names: list[object] = []
        self.parse_mention_calls: list[tuple[str, str | None]] = []
        self._parsed_text = parsed_text
        self._parsed_mentions = set() if parsed_mentions is None else set(parsed_mentions)
        self._relay_display_name = relay_display_name
        self._discord_fallback_name = discord_fallback_name

    def parse_mentions(self, text: str, *, scope: str | None = None) -> tuple[str, set[int]]:
        self.parse_mention_calls.append((text, scope))
        return self._parsed_text or text, set(self._parsed_mentions)

    def set_names(self, user: object) -> None:
        self.names.append(user)

    def cached_display_name(
        self,
        user_id: object,
        fallback: str,
        *,
        preferred_guild_id: object | None = None,
    ) -> str:
        del user_id, preferred_guild_id
        return fallback

    def relay_display_name(
        self,
        user_id: object,
        fallback: str,
        *,
        scope: str | None = None,
        preferred_guild_id: object | None = None,
    ) -> str:
        del user_id, scope, preferred_guild_id
        return self._relay_display_name or fallback

    def discord_fallback_name(
        self,
        user_id: object,
        fallback: str,
        *,
        scope: str | None = None,
        fallback_display_name: str | None = None,
    ) -> str:
        del user_id, scope
        return self._discord_fallback_name or fallback_display_name or fallback


class _RelayAppStub:
    def __init__(self, *, name: str, chat_channel_source_value: str) -> None:
        self.name: str = name
        self.chat_channel_source: SimpleNamespace = SimpleNamespace(value=chat_channel_source_value)


class DiscordRelayDiscordEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_from_app_bound_prefers_scoped_relay_display_name(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub(relay_display_name="AliceGame")
        channel = hikari.TextableChannel(
            app=cast(Any, object()),
            id=hikari.Snowflake(321),
            name="relay-chat",
            type=1,
        )
        message = App_Bound(channel, "hello", 42, source_guild_id=hikari.Snowflake(100))
        app = SimpleNamespace(name="minecraft_alpha", scope="minecraft")

        event = relay._event_from_app_bound(message, cast(Any, app))

        self.assertEqual(event.author.display_name, "AliceGame")

    async def test_event_from_app_bound_uses_minecraft_head_for_known_discord_user(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub(relay_display_name="AliceGame")
        channel = hikari.TextableChannel(
            app=cast(Any, object()),
            id=hikari.Snowflake(321),
            name="relay-chat",
            type=1,
        )
        message = App_Bound(channel, "hello", 42, source_guild_id=hikari.Snowflake(100))
        app = SimpleNamespace(
            name="minecraft_alpha",
            scope="minecraft",
            name_cache=SimpleNamespace(
                get_game_uuid=Mock(return_value="123e4567-e89b-12d3-a456-426614174000"),
                get_game_alias=Mock(return_value="AliceGame"),
            ),
        )

        event = relay._event_from_app_bound(message, cast(Any, app))

        self.assertEqual(event.author.avatar_uri, minecraft_avatar_uri("123e4567-e89b-12d3-a456-426614174000"))

    async def test_deliver_chat_event_fans_out_targets_concurrently(self) -> None:
        relay = object.__new__(DC_Relay)
        discord_endpoint = ChatEndpoint(ChatEndpointId.discord_channel("111"), "Discord 111")
        app_endpoint = ChatEndpoint(ChatEndpointId.app("minecraft_alpha"), "Minecraft Alpha")
        relay.chat_hub = cast(Any, SimpleNamespace(publish=Mock(return_value=(discord_endpoint, app_endpoint))))
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", _running=True, am_receiver=AsyncMock()),
        }
        cast(Any, relay)._active_discord_text_routes = AsyncMock(
            return_value=(SimpleNamespace(channel_id=hikari.Snowflake(111), guild_id=hikari.Snowflake(10)),)
        )
        gate = asyncio.Event()
        delivery_order: list[str] = []

        async def send_discord(event: ChatEvent, channel_id: hikari.Snowflakeish) -> None:
            del event, channel_id
            await asyncio.wait_for(gate.wait(), timeout=0.1)
            delivery_order.append("discord")

        async def send_app(event: ChatEvent, app: object) -> None:
            del event, app
            delivery_order.append("app")
            gate.set()

        cast(Any, relay)._send_chat_event_to_discord = send_discord
        cast(Any, relay)._send_chat_event_to_app = send_app
        cast(Any, relay)._send_chat_event_to_discord_tts = AsyncMock()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="hello",
        )

        await asyncio.wait_for(relay._deliver_chat_event(event), timeout=0.2)

        self.assertEqual(delivery_order, ["app", "discord"])

    async def test_deliver_chat_event_keeps_other_targets_running_when_one_target_fails(self) -> None:
        relay = object.__new__(DC_Relay)
        discord_endpoint = ChatEndpoint(ChatEndpointId.discord_channel("111"), "Discord 111")
        app_endpoint = ChatEndpoint(ChatEndpointId.app("minecraft_alpha"), "Minecraft Alpha")
        relay.chat_hub = cast(Any, SimpleNamespace(publish=Mock(return_value=(discord_endpoint, app_endpoint))))
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(name="minecraft_alpha", _running=True, am_receiver=AsyncMock()),
        }
        cast(Any, relay)._active_discord_text_routes = AsyncMock(
            return_value=(SimpleNamespace(channel_id=hikari.Snowflake(111), guild_id=hikari.Snowflake(10)),)
        )
        delivered_targets: list[str] = []

        async def send_discord(event: ChatEvent, channel_id: hikari.Snowflakeish) -> None:
            del event, channel_id
            raise RuntimeError("discord send failed")

        async def send_app(event: ChatEvent, app: object) -> None:
            del event, app
            delivered_targets.append("app")

        cast(Any, relay)._send_chat_event_to_discord = send_discord
        cast(Any, relay)._send_chat_event_to_app = send_app
        cast(Any, relay)._send_chat_event_to_discord_tts = AsyncMock()
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="hello",
        )

        with patch("_discord.log.exception") as exception_mock:
            await relay._deliver_chat_event(event)

        self.assertEqual(delivered_targets, ["app"])
        self.assertEqual(exception_mock.call_args.args[0], "Chat bridge delivery failed: room=%s event=%s target=%s")
        self.assertEqual(exception_mock.call_args.args[1], "minecraft_alpha")
        self.assertEqual(exception_mock.call_args.args[2], event.id)
        self.assertEqual(exception_mock.call_args.args[3], "discord_channel:111")

    async def test_discord_text_mentions_author_when_user_is_in_target_guild(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub()
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value="AliceGame")),
            ),
        }
        membership = AsyncMock(return_value=True)
        cast(Any, relay)._chat_author_is_member_of_guild = membership
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alice", discord_user_id=42),
            content="hello",
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=hikari.Snowflake(123))

        self.assertEqual(text, "<<@42>> hello")
        self.assertEqual(mentions, set())
        membership.assert_awaited_once_with(42, hikari.Snowflake(123))

    async def test_discord_text_uses_game_alias_when_author_is_not_in_target_guild(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub()
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value="AliceGame")),
            ),
        }
        cast(Any, relay)._chat_author_is_member_of_guild = AsyncMock(return_value=False)
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alice", discord_user_id=42),
            content="hello",
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=hikari.Snowflake(123))

        self.assertEqual(text, "<AliceGame> hello")
        self.assertEqual(mentions, set())

    async def test_discord_text_uses_username_without_alias_or_membership(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub(discord_fallback_name="nameA")
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value=None)),
            ),
        }
        cast(Any, relay)._chat_author_is_member_of_guild = AsyncMock(return_value=False)
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Web Alice", discord_user_id=42),
            content="hello",
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=hikari.Snowflake(123))

        self.assertEqual(text, "<nameA> hello")
        self.assertEqual(mentions, set())

    async def test_discord_text_preserves_body_mentions_without_allowing_author_prefix_ping(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub(parsed_text="hello <@77>", parsed_mentions={77})
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value="AliceGame")),
            ),
        }
        cast(Any, relay)._chat_author_is_member_of_guild = AsyncMock(return_value=True)
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alice", discord_user_id=42),
            content="hello @bob",
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=hikari.Snowflake(123))

        self.assertEqual(text, "<<@42>> hello <@77>")
        self.assertEqual(mentions, {77})
        self.assertEqual(cast(Any, relay).names.parse_mention_calls, [("hello @bob", "minecraft")])

    async def test_discord_text_uses_system_plate_for_system_author(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub()
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value=None)),
            ),
        }
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.SYSTEM, "System"),
            content="hello",
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=hikari.Snowflake(123))

        self.assertEqual(text, "<<SYSTEM>> hello")
        self.assertEqual(mentions, set())

    async def test_discord_text_includes_reply_prefix_when_reference_is_present(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub()
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value="AliceGame")),
            ),
        }
        cast(Any, relay)._chat_author_is_member_of_guild = AsyncMock(return_value=False)
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Web Alice", discord_user_id=42),
            content="hello",
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Yoko", content="earlier"),
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=hikari.Snowflake(123))

        self.assertEqual(text, "<AliceGame> reply to <Yoko>; hello")
        self.assertEqual(mentions, set())

    async def test_discord_text_skips_reply_prefix_when_native_reply_is_used(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).names = _NamesStub()
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(
                friendly="Minecraft Alpha",
                scope="minecraft",
                name_cache=SimpleNamespace(get_game_alias=Mock(return_value="AliceGame")),
            ),
        }
        cast(Any, relay)._chat_author_is_member_of_guild = AsyncMock(return_value=False)
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Web Alice", discord_user_id=42),
            content="hello",
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Yoko", content="earlier"),
        )

        text, mentions = await relay._discord_text_for_event(
            event,
            guild_id=hikari.Snowflake(123),
            include_reference_prefix=False,
        )

        self.assertEqual(text, "<AliceGame> hello")
        self.assertEqual(mentions, set())

    def test_embedify_event_keeps_explicit_embed_without_generating_media_embeds(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="look",
            embed=ChatEmbed(title="Relay", description="Forwarded", color=0x336699),
            links=(ChatLink(url="https://example.invalid/cat.gif", is_media=True, extension=".gif"),),
            attachments=(ChatAttachment(uri="https://example.invalid/cat.png", name="cat.png"),),
        )

        embeds = DC_Relay._embedify_event(event)

        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "Relay")
        self.assertEqual(embeds[0].description, "Forwarded")

    def test_embedify_event_preserves_explicit_embed_title_and_description(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Advancement: Stone Age",
            embed=ChatEmbed(title="Advancement", description="Stone Age", color=0x336699),
        )

        embeds = DC_Relay._embedify_event(
            event,
            app=cast(Any, SimpleNamespace(friendly="Minecraft Alpha")),
        )

        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "Advancement")
        self.assertEqual(embeds[0].description, "Stone Age")
        self.assertEqual(embeds[0].color, 0x336699)

    async def test_discord_text_for_explicit_player_embed_keeps_player_plate(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(friendly="Minecraft Alpha", manage_embed_color=0x22C55E),
        }

        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Advancement: Stone Age",
            embed=ChatEmbed(title="Advancement", description="Stone Age", color=0x336699),
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=None)

        self.assertEqual(text, "<Yoko>")
        self.assertEqual(mentions, set())

    async def test_discord_text_and_embeds_synthesise_join_embed_for_typed_notice(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(friendly="Minecraft Alpha", manage_embed_color=0x22C55E),
        }

        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=None)
        embeds = DC_Relay._embedify_event(event, app=cast(Any, relay)._chat_apps["minecraft_alpha"])

        self.assertEqual(text, "")
        self.assertEqual(mentions, set())
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "Minecraft Alpha")
        self.assertEqual(embeds[0].description, "Joined Yoko")
        self.assertEqual(embeds[0].color, 0x22C55E)

    async def test_discord_text_and_embeds_synthesise_death_embed_without_duplicate_player_name(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(friendly="Minecraft Alpha", manage_embed_color=0x22C55E),
        }

        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Yoko died to Skeleton",
            notice=GameDeathNotice(
                death_kind=GameDeathKind.PVE,
                detail_text="died to Skeleton",
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        text, mentions = await relay._discord_text_for_event(event, guild_id=None)
        embeds = DC_Relay._embedify_event(event, app=cast(Any, relay)._chat_apps["minecraft_alpha"])

        self.assertEqual(text, "<Yoko>")
        self.assertEqual(mentions, set())
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "Minecraft Alpha")
        self.assertEqual(embeds[0].description, "Died to Skeleton")
        self.assertEqual(embeds[0].color, 0x22C55E)

    def test_embedify_event_synthesises_pvp_death_embed_without_player_name(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Yoko killed by another player",
            notice=GameDeathNotice(
                death_kind=GameDeathKind.PVP,
                source=RelayNoticeSource.APP_LOG,
            ),
        )

        embeds = DC_Relay._embedify_event(
            event,
            app=cast(Any, SimpleNamespace(friendly="Minecraft Alpha", manage_embed_color=0x22C55E)),
        )

        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "Minecraft Alpha")
        self.assertEqual(embeds[0].description, "Killed by another player")
        self.assertEqual(embeds[0].color, 0x22C55E)

    async def test_chat_author_membership_skips_local_dev_users(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.reso = cast(Any, SimpleNamespace(user=AsyncMock()))

        is_member = await relay._chat_author_is_member_of_guild(
            Access_Control.dev_bypass_user_id(Power_Level.root),
            hikari.Snowflake(123),
        )

        self.assertFalse(is_member)
        relay.reso.user.assert_not_awaited()

    async def test_active_discord_text_routes_keep_one_channel_per_guild_and_max_two(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).resolve_channel = AsyncMock()
        first_channel_id = hikari.Snowflake(111)
        second_channel_id = hikari.Snowflake(222)
        third_channel_id = hikari.Snowflake(333)
        relay._channel_objects = cast(
            Any,
            {
                first_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(10)),
                second_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(10)),
                third_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(20)),
            },
        )

        routes = await relay._active_discord_text_routes((first_channel_id, second_channel_id, third_channel_id))

        self.assertEqual(tuple(route.channel_id for route in routes), (first_channel_id, third_channel_id))

    async def test_active_discord_text_routes_skip_source_guild_for_discord_origin(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).resolve_channel = AsyncMock()
        same_guild_channel_id = hikari.Snowflake(111)
        other_guild_channel_id = hikari.Snowflake(222)
        relay._channel_objects = cast(
            Any,
            {
                same_guild_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(10)),
                other_guild_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(20)),
            },
        )

        routes = await relay._active_discord_text_routes(
            (same_guild_channel_id, other_guild_channel_id),
            source_guild_id=hikari.Snowflake(10),
        )

        self.assertEqual(tuple(route.channel_id for route in routes), (other_guild_channel_id,))

    async def test_active_discord_text_routes_skip_unavailable_channels(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).resolve_channel = AsyncMock(return_value=None)
        relay._channel_objects = cast(Any, {})

        routes = await relay._active_discord_text_routes((hikari.Snowflake(111),))

        self.assertEqual(routes, ())

    async def test_discord_tts_targets_are_per_active_guild(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay)._voice_tts = object()
        cast(Any, relay).resolve_channel = AsyncMock()
        first_channel_id = hikari.Snowflake(111)
        second_channel_id = hikari.Snowflake(222)
        third_channel_id = hikari.Snowflake(333)
        relay._channel_objects = cast(
            Any,
            {
                first_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(10)),
                second_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(10)),
                third_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(20)),
            },
        )
        app = SimpleNamespace(chat_channels=(first_channel_id, second_channel_id, third_channel_id))

        targets = await relay._discord_tts_targets_for_app(cast(Any, app))

        self.assertEqual(
            tuple(target.id for target in targets),
            (
                ChatEndpointId.discord_tts(hikari.Snowflake(10), first_channel_id),
                ChatEndpointId.discord_tts(hikari.Snowflake(20), third_channel_id),
            ),
        )

    async def test_send_chat_event_to_discord_skips_unavailable_channel_without_raising(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay).resolve_channel = AsyncMock(return_value=None)
        relay._channel_objects = cast(Any, {})
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Web Alice", discord_user_id=42),
            content="hello",
        )

        with patch("_discord.log.warning") as warning_mock:
            await relay._send_chat_event_to_discord(event, hikari.Snowflake(111))

        warning_mock.assert_called_once()

    async def test_send_chat_event_to_discord_uses_native_reply_for_original_discord_message(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._channel_objects = {}
        relay.chat_hub = ChatHub()
        cast(Any, relay)._chat_apps = {}
        cast(Any, relay)._discord_text_for_event = AsyncMock(return_value=("hello", set()))
        cast(Any, relay).resolve_channel = AsyncMock()
        channel_id = hikari.Snowflake(111)
        channel = SimpleNamespace(
            id=channel_id,
            guild_id=hikari.Snowflake(10),
            send=AsyncMock(return_value=SimpleNamespace(id=hikari.Snowflake(901))),
        )
        relay._channel_objects[channel_id] = cast(Any, channel)
        room_id = "minecraft_reply_original"
        target_event = ChatEvent(
            room_id=room_id,
            source=ChatEndpointId.discord_channel(channel_id),
            author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Yoko", discord_user_id=42),
            content="earlier",
            id="target-event",
            source_channel_id=int(channel_id),
            source_message_id=555,
        )
        event = ChatEvent(
            room_id=room_id,
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="replying",
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Yoko", content="earlier", event_id="target-event"),
        )

        relay.chat_hub.clear_room(room_id)
        try:
            relay.chat_hub.publish(target_event)
            await relay._send_chat_event_to_discord(event, channel_id)
        finally:
            relay.chat_hub.clear_room(room_id)

        cast(Any, relay)._discord_text_for_event.assert_awaited_once_with(
            event,
            guild_id=hikari.Snowflake(10),
            include_reference_prefix=False,
        )
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.kwargs["reply"], hikari.Snowflake(555))
        self.assertFalse(channel.send.await_args.kwargs["mentions_reply"])

    async def test_send_chat_event_to_discord_uses_native_reply_for_tracked_relay_message(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._channel_objects = {}
        relay.chat_hub = ChatHub()
        cast(Any, relay)._chat_apps = {}
        cast(Any, relay)._discord_text_for_event = AsyncMock(return_value=("hello", set()))
        cast(Any, relay).resolve_channel = AsyncMock()
        channel_id = hikari.Snowflake(111)
        channel = SimpleNamespace(
            id=channel_id,
            guild_id=hikari.Snowflake(10),
            send=AsyncMock(return_value=SimpleNamespace(id=hikari.Snowflake(902))),
        )
        relay._channel_objects[channel_id] = cast(Any, channel)
        room_id = "minecraft_reply_tracked"
        target_event = ChatEvent(
            room_id=room_id,
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="from game",
            id="target-event",
        )
        event = ChatEvent(
            room_id=room_id,
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="replying",
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Yoko", content="from game", event_id="target-event"),
        )

        relay._record_discord_relay_message(channel_id=channel_id, message_id=hikari.Snowflake(777), event=target_event)
        await relay._send_chat_event_to_discord(event, channel_id)

        cast(Any, relay)._discord_text_for_event.assert_awaited_once_with(
            event,
            guild_id=hikari.Snowflake(10),
            include_reference_prefix=False,
        )
        self.assertEqual(channel.send.await_args.kwargs["reply"], hikari.Snowflake(777))

    def test_chat_reference_from_discord_message_uses_tracked_relay_reference_for_relay_bot_message(self) -> None:
        relay = object.__new__(DC_Relay)
        cast(Any, relay)._chat_apps = {}
        source_event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="from game",
            id="target-event",
        )

        relay._record_discord_relay_message(channel_id=hikari.Snowflake(111), message_id=hikari.Snowflake(777), event=source_event)
        message = cast(
            Any,
            SimpleNamespace(
                channel_id=hikari.Snowflake(111),
                referenced_message=SimpleNamespace(
                    id=hikari.Snowflake(777),
                    author=SimpleNamespace(id=hikari.Snowflake(999), username="Yuki", global_name="Yuki"),
                    content="<Yoko> from game",
                    attachments=(),
                ),
            ),
        )

        reference = relay._chat_reference_from_discord_message(message, guild_id=hikari.Snowflake(10))

        self.assertEqual(
            reference, ChatMessageReference(author_display_name="Yoko", content="from game", event_id="target-event")
        )

    def test_forwarded_snapshot_content_uses_snapshot_body_and_extras(self) -> None:
        message = cast(
            hikari.Message,
            cast(
                Any,
                SimpleNamespace(
                    message_snapshots=(
                        SimpleNamespace(
                            content="forwarded body",
                            attachments=(object(),),
                            stickers=(object(), object()),
                        ),
                    )
                ),
            ),
        )

        rendered = DC_Relay._forwarded_snapshot_content(message)

        self.assertEqual(rendered, "forwarded body, attachment, 2 stickers")


class DiscordRelaySeenMessageTests(unittest.TestCase):
    def test_seen_message_ids_are_bounded(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        relay._MAX_SEEN_MESSAGE_IDS = 3

        self.assertTrue(relay._remember_message_id(hikari.Snowflake(1)))
        self.assertTrue(relay._remember_message_id(hikari.Snowflake(2)))
        self.assertTrue(relay._remember_message_id(hikari.Snowflake(3)))
        self.assertTrue(relay._remember_message_id(hikari.Snowflake(4)))

        self.assertEqual(relay.seen_messages_id, {hikari.Snowflake(2), hikari.Snowflake(3), hikari.Snowflake(4)})
        self.assertFalse(relay._remember_message_id(hikari.Snowflake(4)))

    def test_message_author_is_bot_detects_message_author(self) -> None:
        ctx = SimpleNamespace(
            author=None,
            message=SimpleNamespace(author=SimpleNamespace(is_bot=True)),
        )

        self.assertTrue(DC_Relay._message_author_is_bot(cast(Any, ctx)))

    def test_bot_ids_from_snapshots_ignores_oauth_only_bots(self) -> None:
        relay_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(id="100000000000000001", label="Yuki"),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="yuki",
                    public_base_url="https://yuki.example",
                    node_api_base_url="https://yuki.example/api/node",
                )
            ),
        )
        oauth_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(id="100000000000000002", label="OAuth Only"),
            features=config.BotMetadataFeatures(oauth=config.PersistedOAuthLinks(guild="https://example.invalid")),
        )

        bot_ids = DC_Relay._bot_ids_from_snapshots(
            {
                relay_snapshot.profile.id: relay_snapshot,
                oauth_snapshot.profile.id: oauth_snapshot,
            }
        )

        self.assertEqual(bot_ids, {100000000000000001})

    def test_register_app_channel_logs_only_new_registration(self) -> None:
        channel_id = hikari.Snowflake(101)
        app = Mock()
        app.name = "minecraft_alpha"
        DC_Relay._chat_channels.clear()

        try:
            with patch("_discord.log.info") as info_mock:
                DC_Relay.register_app_channel(channel_id, cast(Any, app))
                DC_Relay.register_app_channel(channel_id, cast(Any, app))
        finally:
            DC_Relay._chat_channels.clear()

        info_mock.assert_called_once()

    def test_log_chat_relay_summary_reports_owner_and_default_pickup(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, SimpleNamespace(get_me=Mock(return_value=SimpleNamespace(id=hikari.Snowflake(1)))))
        cast(Any, relay)._known_relay_bot_ids = Mock(return_value=frozenset({1, 2}))
        cast(Any, relay)._known_relay_bot_labels = Mock(return_value={1: "yuki", 2: "erin"})
        cast(Any, relay)._relay_channel_owner_bot_id = Mock(return_value=1)
        first_app = _RelayAppStub(name="minecraft_alpha", chat_channel_source_value="default")
        second_app = _RelayAppStub(name="minecraft_beta", chat_channel_source_value="default")
        third_app = _RelayAppStub(name="minecraft_gamma", chat_channel_source_value="instance")
        DC_Relay._chat_channels.clear()
        DC_Relay._chat_channels[hikari.Snowflake(101)] = {
            cast(Any, second_app),
            cast(Any, third_app),
            cast(Any, first_app),
        }

        try:
            with patch("_discord.log.info") as info_mock:
                relay.log_chat_relay_summary()
        finally:
            DC_Relay._chat_channels.clear()

        info_mock.assert_called_once()
        args = info_mock.call_args.args
        self.assertEqual(args[0], "Relay channel summary: channel=%s owner=%s owned_by_this_bot=%s default_pickup=%s apps=%s")
        self.assertEqual(args[1], 101)
        self.assertEqual(args[2], "yuki")
        self.assertTrue(args[3])
        self.assertEqual(args[4], "minecraft_alpha")
        self.assertEqual(args[5], "minecraft_alpha,minecraft_beta,minecraft_gamma")


class DiscordRelayQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_task_continues_after_send_dc_failure(self) -> None:
        relay = object.__new__(DC_Relay)
        first_message = cast(
            Any,
            SimpleNamespace(app=SimpleNamespace(name="minecraft_alpha"), player="Yoko", content="bad"),
        )
        second_message = cast(
            Any,
            SimpleNamespace(app=SimpleNamespace(name="minecraft_alpha"), player="Erin", content="good"),
        )
        relay.queue = deque((first_message, second_message))
        cast(Any, relay)._send_dc = AsyncMock(side_effect=(RuntimeError("boom"), None))

        with (
            self.assertRaises(asyncio.CancelledError),
            patch("_discord.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
            patch("_discord.log.exception") as exception_mock,
        ):
            await relay._queue_task()

        self.assertEqual(cast(Any, relay)._send_dc.await_count, 2)
        exception_mock.assert_called_once_with(
            "App -> Discord relay worker dropped message: app=%s player=%r content=%r",
            "minecraft_alpha",
            "Yoko",
            "bad",
        )

    async def test_queue_worker_done_schedules_restart_after_unexpected_exception(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._queue_worker_should_run = True
        relay._queue_worker_status = RelayWorkerStatus.RUNNING
        cast(Any, relay)._schedule_queue_worker_restart = Mock()

        async def boom() -> None:
            raise RuntimeError("boom")

        task = asyncio.create_task(boom())
        with self.assertRaises(RuntimeError):
            await task
        relay._read_task = task

        with patch("_discord.log.exception") as exception_mock:
            relay._handle_queue_worker_done(task)

        self.assertIsNone(relay._read_task)
        self.assertEqual(relay._queue_worker_status, RelayWorkerStatus.FAILED)
        cast(Any, relay)._schedule_queue_worker_restart.assert_called_once_with()
        exception_mock.assert_called_once()

    async def test_close_stops_worker_without_scheduling_restart(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._queue_worker_should_run = True
        relay._queue_worker_status = RelayWorkerStatus.RUNNING
        relay._queue_worker_restart_task = asyncio.create_task(asyncio.sleep(60))
        relay._read_task = asyncio.create_task(asyncio.sleep(60))

        await relay.close()

        self.assertFalse(relay._queue_worker_should_run)
        self.assertIsNone(relay._queue_worker_restart_task)
        self.assertIsNone(relay._read_task)
        self.assertEqual(relay._queue_worker_status, RelayWorkerStatus.STOPPED)


class DiscordRelayInboundMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_dc_message_ignores_bot_authored_messages(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        channel_id = hikari.Snowflake(101)
        app = Mock()
        app.name = "minecraft_alpha"
        DC_Relay._chat_channels = {channel_id: {cast(Any, app)}}
        cast(Any, relay).resolve_channel = AsyncMock()

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=True),
            channel_id=channel_id,
            message_id=hikari.Snowflake(99),
            message=SimpleNamespace(author=SimpleNamespace(is_bot=True)),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        cast(Any, relay).resolve_channel.assert_not_awaited()
        self.assertEqual(relay.seen_messages_id, set())

    async def test_on_dc_message_sends_default_pickup_to_local_app_when_channel_is_not_owner(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=False)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        cast(Any, relay)._record_chat_event = Mock()
        cast(Any, relay)._send_chat_event_to_app = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        app = Mock()
        app.name = "minecraft_alpha"
        app.chat_channel_source = SimpleNamespace(value="default")
        DC_Relay._chat_channels = {channel_id: {cast(Any, app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        cast(Any, relay)._is_active_app_chat_channel.assert_awaited_once_with(app, channel_id)
        cast(Any, relay)._deliver_chat_event.assert_not_awaited()
        cast(Any, relay)._record_chat_event.assert_called_once()
        event = cast(Any, relay)._record_chat_event.call_args.args[0]
        self.assertEqual(event.room_id, "minecraft_alpha")
        cast(Any, relay)._send_chat_event_to_app.assert_awaited_once_with(event, app)

    async def test_on_dc_message_owner_processes_default_channel(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=True)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        app = Mock()
        app.name = "minecraft_alpha"
        app.chat_channel_source = SimpleNamespace(value="default")
        app._running = False
        app.am_receiver = None
        DC_Relay._chat_channels = {channel_id: {cast(Any, app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        cast(Any, relay)._is_active_app_chat_channel.assert_awaited_once_with(app, channel_id)
        cast(Any, relay)._deliver_chat_event.assert_awaited_once()
        event = cast(Any, relay)._deliver_chat_event.await_args.args[0]
        self.assertEqual(event.room_id, "minecraft_alpha")

    async def test_on_dc_message_forward_uses_message_snapshot_when_wrapper_has_no_content(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=True)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        app = Mock()
        app.name = "minecraft_alpha"
        app.chat_channel_source = SimpleNamespace(value="default")
        app._running = False
        app.am_receiver = None
        DC_Relay._chat_channels = {channel_id: {cast(Any, app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                message_snapshots=(
                    SimpleNamespace(
                        content="forwarded body",
                        attachments=(object(),),
                        stickers=(),
                    ),
                ),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                referenced_message=None,
                message_reference=SimpleNamespace(type=hikari_messages.MessageReferenceType.FORWARD),
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        cast(Any, relay)._deliver_chat_event.assert_awaited_once()
        event = cast(Any, relay)._deliver_chat_event.await_args.args[0]
        self.assertEqual(event.content, "forwarded body, attachment")
        self.assertEqual(event.reference_kind, ChatReferenceKind.FORWARD)

    async def test_on_dc_message_uses_one_pickup_app_for_shared_default_channel(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=True)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        cast(Any, relay)._record_chat_event = Mock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        first_app = Mock()
        first_app.name = "minecraft_alpha"
        first_app.chat_channel_source = SimpleNamespace(value="default")
        first_app._running = False
        first_app.am_receiver = None
        second_app = Mock()
        second_app.name = "minecraft_beta"
        second_app.chat_channel_source = SimpleNamespace(value="default")
        second_app._running = False
        second_app.am_receiver = None
        DC_Relay._chat_channels = {channel_id: {cast(Any, second_app), cast(Any, first_app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        self.assertEqual(cast(Any, relay)._is_active_app_chat_channel.await_count, 2)
        cast(Any, relay)._deliver_chat_event.assert_awaited_once()
        event = cast(Any, relay)._deliver_chat_event.await_args.args[0]
        self.assertEqual(event.room_id, "minecraft_alpha")
        cast(Any, relay)._record_chat_event.assert_called_once()
        recorded_event = cast(Any, relay)._record_chat_event.call_args.args[0]
        self.assertEqual(recorded_event.room_id, "minecraft_beta")

    async def test_on_dc_message_non_owner_sends_all_running_shared_default_apps_to_game(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=False)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        cast(Any, relay)._record_chat_event = Mock()
        cast(Any, relay)._send_chat_event_to_app = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        first_app = Mock()
        first_app.name = "minecraft_alpha"
        first_app.chat_channel_source = SimpleNamespace(value="default")
        first_app._running = True
        first_app.am_receiver = object()
        second_app = Mock()
        second_app.name = "minecraft_beta"
        second_app.chat_channel_source = SimpleNamespace(value="default")
        second_app._running = True
        second_app.am_receiver = object()
        DC_Relay._chat_channels = {channel_id: {cast(Any, second_app), cast(Any, first_app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        cast(Any, relay)._deliver_chat_event.assert_not_awaited()
        self.assertEqual(cast(Any, relay)._record_chat_event.call_count, 2)
        self.assertEqual(cast(Any, relay)._send_chat_event_to_app.await_count, 2)
        sent_calls = cast(Any, relay)._send_chat_event_to_app.await_args_list
        self.assertEqual([call.args[0].room_id for call in sent_calls], ["minecraft_alpha", "minecraft_beta"])
        self.assertEqual([call.args[1] for call in sent_calls], [first_app, second_app])

    async def test_on_dc_message_owner_sends_non_selected_running_shared_default_app_to_game(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=True)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        cast(Any, relay)._record_chat_event = Mock()
        cast(Any, relay)._send_chat_event_to_app = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        first_app = Mock()
        first_app.name = "minecraft_alpha"
        first_app.chat_channel_source = SimpleNamespace(value="default")
        first_app._running = False
        first_app.am_receiver = None
        second_app = Mock()
        second_app.name = "sevendays_alpha"
        second_app.chat_channel_source = SimpleNamespace(value="default")
        second_app._running = True
        second_app.am_receiver = object()
        DC_Relay._chat_channels = {channel_id: {cast(Any, second_app), cast(Any, first_app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        cast(Any, relay)._deliver_chat_event.assert_awaited_once()
        delivered_event = cast(Any, relay)._deliver_chat_event.await_args.args[0]
        self.assertEqual(delivered_event.room_id, "minecraft_alpha")
        cast(Any, relay)._record_chat_event.assert_called_once()
        recorded_event = cast(Any, relay)._record_chat_event.call_args.args[0]
        self.assertEqual(recorded_event.room_id, "sevendays_alpha")
        cast(Any, relay)._send_chat_event_to_app.assert_awaited_once_with(recorded_event, second_app)

    async def test_on_dc_message_materialises_downloads_only_for_delivery_branch(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=False)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        cast(Any, relay)._record_chat_event = Mock()
        cast(Any, relay)._send_chat_event_to_app = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        first_app = Mock()
        first_app.name = "minecraft_alpha"
        first_app.chat_channel_source = SimpleNamespace(value="default")
        second_app = Mock()
        second_app.name = "minecraft_beta"
        second_app.chat_channel_source = SimpleNamespace(value="default")
        DC_Relay._chat_channels = {channel_id: {cast(Any, second_app), cast(Any, first_app)}}
        attachment = cast(
            hikari.Attachment,
            cast(
                object,
                SimpleNamespace(
                    filename="cat.png",
                    title="cat",
                    media_type="image/png",
                    url="https://cdn.example.invalid/cat.png",
                ),
            ),
        )

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(attachment,),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            with patch.object(File_Utils, "download_temp", new=AsyncMock(return_value=Path("/tmp/cat.png"))) as download_mock:
                await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        self.assertEqual(download_mock.await_count, 1)
        recorded_events = [call.args[0] for call in cast(Any, relay)._record_chat_event.call_args_list]
        self.assertEqual([event.room_id for event in recorded_events], ["minecraft_alpha", "minecraft_beta"])
        self.assertEqual(recorded_events[0].attachments[0].uri, "/tmp/cat.png")
        self.assertEqual(recorded_events[1].attachments[0].uri, "https://cdn.example.invalid/cat.png")
        sent_event, sent_app = cast(Any, relay)._send_chat_event_to_app.await_args.args
        self.assertEqual(sent_event.attachments[0].uri, "/tmp/cat.png")
        self.assertIs(sent_app, first_app)

    async def test_on_dc_message_keeps_partial_attachment_downloads_and_appends_failure_notice(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=False)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        cast(Any, relay)._deliver_chat_event = AsyncMock()
        cast(Any, relay)._record_chat_event = Mock()
        cast(Any, relay)._send_chat_event_to_app = AsyncMock()
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        app = Mock()
        app.name = "minecraft_alpha"
        app.chat_channel_source = SimpleNamespace(value="default")
        DC_Relay._chat_channels = {channel_id: {cast(Any, app)}}
        first_attachment = cast(
            hikari.Attachment,
            cast(
                object,
                SimpleNamespace(
                    filename="cat.png",
                    title="cat",
                    media_type="image/png",
                    url="https://cdn.example.invalid/cat.png",
                ),
            ),
        )
        second_attachment = cast(
            hikari.Attachment,
            cast(
                object,
                SimpleNamespace(
                    filename="dog.png",
                    title="dog",
                    media_type="image/png",
                    url="https://cdn.example.invalid/dog.png",
                ),
            ),
        )

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(first_attachment, second_attachment),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            with patch.object(
                File_Utils,
                "download_temp",
                new=AsyncMock(side_effect=(Path("/tmp/cat.png"), RuntimeError("boom"))),
            ):
                await relay.on_dc_message(cast(Any, ctx))
        finally:
            DC_Relay._chat_channels.clear()

        sent_event, sent_app = cast(Any, relay)._send_chat_event_to_app.await_args.args
        self.assertEqual(sent_event.content, "hello [1 attachment failed to download]")
        self.assertEqual(len(sent_event.attachments), 1)
        self.assertEqual(sent_event.attachments[0].uri, "/tmp/cat.png")
        self.assertIs(sent_app, app)

    async def test_on_dc_message_records_history_for_all_applicable_web_chat_rooms(self) -> None:
        relay = object.__new__(DC_Relay)
        relay.bot = cast(Any, object())
        relay._channel_objects = {}
        relay.chat_hub = ChatHub()
        relay.seen_messages_id = set()
        relay.seen_messages_order = deque()
        setattr(cast(Any, relay), "names", _NamesStub())
        cast(Any, relay)._chat_author_color = AsyncMock(return_value=None)
        cast(Any, relay)._owns_shared_relay_channel = Mock(return_value=True)
        cast(Any, relay)._is_active_app_chat_channel = AsyncMock(return_value=True)
        source_channel = _make_textable_channel(channel_id=hikari.Snowflake(101), name="relay-a")
        cast(Any, relay).resolve_channel = AsyncMock(return_value=source_channel)
        channel_id = hikari.Snowflake(101)
        first_app = Mock()
        first_app.name = "discord_history_alpha"
        first_app.chat_channel_source = SimpleNamespace(value="default")
        first_app._running = False
        first_app.am_receiver = None
        second_app = Mock()
        second_app.name = "discord_history_beta"
        second_app.chat_channel_source = SimpleNamespace(value="default")
        second_app._running = False
        second_app.am_receiver = None
        relay.chat_hub.clear_room(first_app.name)
        relay.chat_hub.clear_room(second_app.name)
        DC_Relay._chat_channels = {channel_id: {cast(Any, second_app), cast(Any, first_app)}}

        ctx = SimpleNamespace(
            is_human=True,
            author=SimpleNamespace(is_bot=False),
            channel_id=channel_id,
            content="hello",
            message_id=hikari.Snowflake(99),
            guild_id=hikari.Snowflake(1),
            author_id=hikari.Snowflake(456),
            message=SimpleNamespace(
                author=SimpleNamespace(is_bot=False),
                type=hikari.MessageType.DEFAULT,
                attachments=(),
                get_member_mentions=Mock(return_value=hikari.UNDEFINED),
                user_mentions=hikari.UNDEFINED,
                message_reference=None,
            ),
        )

        try:
            await relay.on_dc_message(cast(Any, ctx))
            first_history = relay.chat_hub.history(first_app.name)
            second_history = relay.chat_hub.history(second_app.name)
        finally:
            DC_Relay._chat_channels.clear()
            relay.chat_hub.clear_room(first_app.name)
            relay.chat_hub.clear_room(second_app.name)

        self.assertEqual(tuple(event.content for event in first_history), ("hello",))
        self.assertEqual(tuple(event.content for event in second_history), ("hello",))


class DiscordRelayAuthorColorTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_author_color_uses_highest_coloured_role_and_caches_for_guild_user(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._author_color_cache = {}
        relay.reso = cast(Any, SimpleNamespace(user=AsyncMock()))
        lower_coloured_role = SimpleNamespace(color=hikari.Color(0x336699), position=1)
        top_uncoloured_role = SimpleNamespace(color=hikari.Color(0), position=2)
        member = cast(
            hikari.Member,
            cast(
                object,
                SimpleNamespace(
                    get_roles=Mock(return_value=(lower_coloured_role, top_uncoloured_role)),
                    get_top_role=Mock(return_value=top_uncoloured_role),
                ),
            ),
        )

        first = await relay._chat_author_color(
            discord_user_id=42,
            guild_id=hikari.Snowflake(123),
            member=member,
        )
        second = await relay._chat_author_color(
            discord_user_id=42,
            guild_id=hikari.Snowflake(123),
        )

        self.assertEqual(first, "#336699")
        self.assertEqual(second, "#336699")
        relay.reso.user.assert_not_awaited()

    async def test_chat_author_color_ignores_uncoloured_top_role(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._author_color_cache = {}
        role = SimpleNamespace(color=hikari.Color(0), position=1)
        member = cast(
            hikari.Member,
            cast(
                object,
                SimpleNamespace(
                    get_roles=Mock(return_value=(role,)),
                    get_top_role=Mock(return_value=role),
                ),
            ),
        )

        color = await relay._chat_author_color(
            discord_user_id=42,
            guild_id=hikari.Snowflake(123),
            member=member,
        )

        self.assertIsNone(color)

    async def test_chat_author_color_skips_local_dev_users(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._author_color_cache = {}
        relay.reso = cast(Any, SimpleNamespace(user=AsyncMock()))

        color = await relay._chat_author_color(
            discord_user_id=Access_Control.dev_bypass_user_id(Power_Level.root),
            guild_id=hikari.Snowflake(123),
        )

        self.assertIsNone(color)
        relay.reso.user.assert_not_awaited()

    async def test_chat_author_color_caches_discord_lookup_failures(self) -> None:
        relay = object.__new__(DC_Relay)
        relay._author_color_cache = {}
        relay.reso = cast(Any, SimpleNamespace(user=AsyncMock(side_effect=RuntimeError("not found"))))

        first = await relay._chat_author_color(discord_user_id=42, guild_id=hikari.Snowflake(123))
        second = await relay._chat_author_color(discord_user_id=42, guild_id=hikari.Snowflake(123))

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(relay.reso.user.await_count, 1)

    async def test_chat_author_color_for_room_uses_later_guild_when_first_has_no_color(self) -> None:
        relay = object.__new__(DC_Relay)
        first_channel_id = hikari.Snowflake(111)
        second_channel_id = hikari.Snowflake(222)
        cast(Any, relay)._chat_apps = {
            "minecraft_alpha": SimpleNamespace(chat_channels=(first_channel_id, second_channel_id)),
        }
        relay._channel_objects = cast(
            Any,
            {
                first_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(333)),
                second_channel_id: SimpleNamespace(guild_id=hikari.Snowflake(444)),
            },
        )
        chat_author_color = AsyncMock(side_effect=(None, "#445566"))
        cast(Any, relay)._chat_author_color = chat_author_color

        color, guild_id = await relay._chat_author_color_for_room(
            room_id="minecraft_alpha",
            discord_user_id=42,
        )

        self.assertEqual(color, "#445566")
        self.assertEqual(guild_id, 444)
        self.assertEqual(chat_author_color.await_count, 2)


if __name__ == "__main__":
    unittest.main()
