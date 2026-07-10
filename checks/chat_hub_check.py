from __future__ import annotations

import unittest

import config
from chat_hub import (
    DEFAULT_CHAT_AUTHOR_COLOR_HEX,
    ChatAttachment,
    ChatAuthor,
    ChatAuthorKind,
    ChatEmbed,
    ChatEndpoint,
    ChatEndpointId,
    ChatEvent,
    ChatHub,
    ChatLink,
    ChatLinkVariant,
    ChatMessageReference,
    ChatReferenceKind,
    ChatRoomUpdate,
)
from relay_notices import PlayerSessionAction, PlayerSessionNotice, RelayNoticeSource


class ChatHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._clear_chat_hub()

    def tearDown(self) -> None:
        self._clear_chat_hub()

    @staticmethod
    def _clear_chat_hub() -> None:
        hub = ChatHub()
        for room_id in hub.bound_room_ids():
            hub.clear_room(room_id)

    def test_publish_records_history_and_excludes_source_endpoint(self) -> None:
        hub = ChatHub()
        room_id = "minecraft_alpha"
        app_endpoint = ChatEndpoint(ChatEndpointId.app(room_id), "Minecraft Alpha")
        discord_endpoint = ChatEndpoint(ChatEndpointId.discord_channel("123"), "Discord 123")
        hub.clear_room(room_id)
        hub.bind(room_id, app_endpoint)
        hub.bind(room_id, discord_endpoint)

        event = ChatEvent(
            room_id=room_id,
            source=discord_endpoint.id,
            author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin", id="456", discord_user_id=456),
            content="Erin joined Minecraft Alpha",
        )

        targets = hub.publish(event)

        self.assertEqual(targets, (app_endpoint,))
        self.assertEqual(hub.history(room_id), (event,))

    def test_discord_source_event_index_tracks_publish_and_eviction(self) -> None:
        config.Singleton._instances.pop(ChatHub, None)
        try:
            hub = ChatHub(history_limit=2)
            room_id = "minecraft_alpha"
            endpoint = ChatEndpointId.discord_channel("123")
            first = ChatEvent(
                room_id=room_id,
                source=endpoint,
                author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin"),
                content="first",
                source_channel_id=123,
                source_message_id=1,
            )
            second = ChatEvent(
                room_id=room_id,
                source=endpoint,
                author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin"),
                content="second",
                source_channel_id=123,
                source_message_id=2,
            )
            third = ChatEvent(
                room_id=room_id,
                source=endpoint,
                author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin"),
                content="third",
                source_channel_id=123,
                source_message_id=3,
            )

            hub.publish(first)
            hub.publish(second)
            self.assertIs(hub.discord_source_event(room_id, channel_id=123, message_id=1), first)
            self.assertIs(hub.discord_source_event(room_id, channel_id=123, message_id=2), second)

            hub.publish(third)

            self.assertIsNone(hub.discord_source_event(room_id, channel_id=123, message_id=1))
            self.assertIs(hub.discord_source_event(room_id, channel_id=123, message_id=2), second)
            self.assertIs(hub.discord_source_event(room_id, channel_id=123, message_id=3), third)
        finally:
            config.Singleton._instances.pop(ChatHub, None)

    def test_clear_room_removes_endpoint_room_indexes(self) -> None:
        hub = ChatHub()
        room_id = "factorio_alpha"
        endpoint = ChatEndpoint(ChatEndpointId.discord_channel("123"), "Discord 123")
        hub.clear_room(room_id)
        hub.bind(room_id, endpoint)
        hub.publish(
            ChatEvent(
                room_id=room_id,
                source=endpoint.id,
                author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin"),
                content="hello",
            )
        )

        hub.clear_room(room_id)

        self.assertEqual(hub.endpoints_for_room(room_id), ())
        self.assertEqual(hub.rooms_for_endpoint(endpoint.id), ())
        self.assertEqual(hub.history(room_id), ())

    def test_bound_room_ids_return_sorted_bound_rooms(self) -> None:
        hub = ChatHub()
        first_room_id = "zeta_room"
        second_room_id = "alpha_room"
        endpoint = ChatEndpoint(ChatEndpointId.discord_channel("123"), "Discord 123")
        hub.clear_room(first_room_id)
        hub.clear_room(second_room_id)

        try:
            hub.bind(first_room_id, endpoint)
            hub.bind(second_room_id, endpoint)

            self.assertEqual(hub.bound_room_ids(), (second_room_id, first_room_id))
        finally:
            hub.clear_room(first_room_id)
            hub.clear_room(second_room_id)

    def test_room_subscription_notifies_for_bind_publish_and_clear(self) -> None:
        hub = ChatHub()
        room_id = "minecraft_alpha"
        endpoint = ChatEndpoint(ChatEndpointId.app(room_id), "Minecraft Alpha")
        event = ChatEvent(
            room_id=room_id,
            source=endpoint.id,
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="hello",
        )
        updates: list[str] = []
        subscription_id = hub.subscribe(room_id, lambda update: updates.append(update.room_id))

        try:
            hub.clear_room(room_id)
            updates.clear()
            hub.bind(room_id, endpoint)
            hub.publish(event)
            hub.clear_room(room_id)
        finally:
            hub.unsubscribe(room_id, subscription_id)
            hub.clear_room(room_id)

        self.assertEqual(updates, [room_id, room_id, room_id])

    def test_room_subscription_stops_after_unsubscribe(self) -> None:
        hub = ChatHub()
        room_id = "factorio_alpha"
        endpoint = ChatEndpoint(ChatEndpointId.app(room_id), "Factorio Alpha")
        updates: list[str] = []
        subscription_id = hub.subscribe(room_id, lambda update: updates.append(update.room_id))

        try:
            hub.unsubscribe(room_id, subscription_id)
            hub.bind(room_id, endpoint)
        finally:
            hub.clear_room(room_id)

        self.assertEqual(updates, [])

    def test_room_subscription_includes_published_event_delta(self) -> None:
        hub = ChatHub()
        room_id = "factorio_delta"
        endpoint = ChatEndpoint(ChatEndpointId.app(room_id), "Factorio Delta")
        event = ChatEvent(
            room_id=room_id,
            source=endpoint.id,
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="hello",
        )
        updates: list[ChatRoomUpdate] = []
        hub.clear_room(room_id)
        subscription_id = hub.subscribe(room_id, updates.append)

        try:
            hub.publish(event)
        finally:
            hub.unsubscribe(room_id, subscription_id)
            hub.clear_room(room_id)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].room_id, room_id)
        self.assertEqual(updates[0].event, event)
        self.assertGreater(updates[0].revision, 0)

    def test_room_revision_advances_for_each_visible_change(self) -> None:
        hub = ChatHub()
        room_id = "factorio_revision"
        endpoint = ChatEndpoint(ChatEndpointId.app(room_id), "Factorio Revision")
        hub.clear_room(room_id)
        initial_revision = hub.room_revision(room_id)

        try:
            hub.bind(room_id, endpoint)
            bound_revision = hub.room_revision(room_id)
            hub.publish(
                ChatEvent(
                    room_id=room_id,
                    source=endpoint.id,
                    author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
                    content="hello",
                )
            )
            published_revision = hub.room_revision(room_id)
        finally:
            hub.clear_room(room_id)

        self.assertEqual(bound_revision, initial_revision + 1)
        self.assertEqual(published_revision, bound_revision + 1)

    def test_author_accepts_hex_colour_for_ui_display(self) -> None:
        author = ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin", color_hex="#aabbcc")

        self.assertEqual(author.color_hex, "#aabbcc")

    def test_author_accepts_avatar_uri_for_ui_display(self) -> None:
        author = ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko", avatar_uri="https://mc-heads.net/avatar/Yoko/32")

        self.assertEqual(author.avatar_uri, "https://mc-heads.net/avatar/Yoko/32")

    def test_default_author_colour_is_valid_hex(self) -> None:
        author = ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin", color_hex=DEFAULT_CHAT_AUTHOR_COLOR_HEX)

        self.assertEqual(author.color_hex, DEFAULT_CHAT_AUTHOR_COLOR_HEX)

    def test_discord_tts_endpoint_uses_guild_and_channel_identity(self) -> None:
        endpoint_id = ChatEndpointId.discord_tts("123", "456")

        self.assertEqual(endpoint_id.kind.value, "discord_tts")
        self.assertEqual(endpoint_id.value, "123:456")

    def test_author_rejects_invalid_colour(self) -> None:
        with self.assertRaises(ValueError):
            ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin", color_hex="aabbcc")

    def test_chat_event_round_trips_mapping(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(
                ChatAuthorKind.DISCORD_USER,
                "Erin",
                id="456",
                discord_user_id=456,
                color_hex="#336699",
                avatar_uri="https://mc-heads.net/avatar/456/32",
            ),
            content="hello",
            attachments=(ChatAttachment(uri="https://cdn.example.com/cat.png", name="cat.png"),),
            links=(
                ChatLink(
                    url="https://example.com/cat.png",
                    is_media=True,
                    extension=".png",
                    variants=(
                        ChatLinkVariant(
                            key="gif",
                            label="original",
                            url="https://media1.tenor.com/m/original.gif",
                            media_type="image/gif",
                            extension="gif",
                            width=498,
                            height=278,
                            size_bytes=7_847_765,
                            duration_seconds=7.2,
                        ),
                        ChatLinkVariant(
                            key="tinygif",
                            label="tinygif",
                            url="https://media.tenor.com/tiny.gif",
                            media_type="image/gif",
                            extension="gif",
                            width=220,
                            height=123,
                            size_bytes=49_492,
                            duration_seconds=7.2,
                        ),
                    ),
                ),
            ),
            reference_kind=ChatReferenceKind.REPLY,
            reference=ChatMessageReference(author_display_name="Yoko", content="hello there", event_id="source-1"),
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
                pack_version="2026-07-04",
                has_unpublished_pack_changes=True,
            ),
            embed=ChatEmbed(title="Relay", description="Forwarded", color=0x336699),
            source_guild_id=789,
            source_guild_name="Friends",
            source_channel_id=123,
            source_message_id=456789,
            source_label="Guild",
        )

        restored = ChatEvent.from_mapping(event.to_mapping())

        self.assertEqual(restored, event)

    def test_chat_event_from_mapping_treats_blank_link_media_type_as_missing(self) -> None:
        payload = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.discord_channel("123"),
            author=ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin"),
            content="hello",
            links=(
                ChatLink(
                    url="https://example.com/cat.png",
                    media_type="image/png",
                    is_media=True,
                    variants=(
                        ChatLinkVariant(
                            key="tiny",
                            label="tiny",
                            url="https://example.com/cat-tiny.png",
                            media_type="image/png",
                        ),
                    ),
                ),
            ),
        ).to_mapping()
        raw_links = payload["links"]
        assert isinstance(raw_links, list)
        raw_link = raw_links[0]
        assert isinstance(raw_link, dict)
        raw_link["media_type"] = "   "
        raw_variant = raw_link["variants"][0]
        assert isinstance(raw_variant, dict)
        raw_variant["media_type"] = ""

        restored = ChatEvent.from_mapping(payload)

        self.assertIsNone(restored.links[0].media_type)
        self.assertIsNone(restored.links[0].variants[0].media_type)

    def test_chat_event_render_content_renders_typed_notice(self) -> None:
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

        self.assertEqual(event.render_content(app_name="Minecraft Alpha"), "Yoko joined Minecraft Alpha")

    def test_chat_event_render_content_returns_raw_non_template_content(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="hello",
        )

        self.assertEqual(event.render_content(app_name="Minecraft Alpha"), "hello")

    def test_chat_event_to_reference_renders_typed_notice(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="Yoko joined Minecraft Alpha",
            notice=PlayerSessionNotice(
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_LOG,
            ),
            id="event-1",
        )

        reference = event.to_reference(app_name="Minecraft Alpha")

        self.assertEqual(reference, ChatMessageReference("Yoko", "Yoko joined Minecraft Alpha", event_id="event-1"))

    def test_chat_event_from_mapping_rejects_legacy_template_payload(self) -> None:
        payload = {
            "room_id": "minecraft_alpha",
            "source": ChatEndpointId.app("minecraft_alpha").to_mapping(),
            "author": ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko").to_mapping(),
            "content": "{player} joined {app}",
            "id": "event-1",
            "created_at": 1.0,
            "attachments": [],
            "links": [],
            "reference_kind": ChatReferenceKind.NONE.value,
            "reference": None,
            "is_template": True,
            "notice": None,
            "embed": None,
            "source_guild_id": None,
            "source_channel_id": None,
            "source_message_id": None,
            "source_label": None,
        }

        with self.assertRaisesRegex(ValueError, "Legacy template chat event payloads are no longer supported."):
            ChatEvent.from_mapping(payload)

    def test_chat_event_to_reference_falls_back_to_media_label(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.web_session("session-1"),
            author=ChatAuthor(ChatAuthorKind.WEB_USER, "Tester"),
            content="",
            attachments=(ChatAttachment(uri="https://cdn.example.invalid/cat.png", name="cat.png"),),
        )

        reference = event.to_reference()

        self.assertEqual(reference.content, "Sent media")

    def test_chat_hub_event_returns_matching_history_event(self) -> None:
        hub = ChatHub()
        room_id = "factorio_alpha"
        hub.clear_room(room_id)
        event = ChatEvent(
            room_id=room_id,
            source=ChatEndpointId.app(room_id),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Yoko"),
            content="hello",
            id="event-lookup",
        )

        try:
            hub.publish(event)
            self.assertEqual(hub.event(room_id, "event-lookup"), event)
            self.assertIsNone(hub.event(room_id, "missing"))
        finally:
            hub.clear_room(room_id)


if __name__ == "__main__":
    unittest.main()
