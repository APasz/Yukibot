from __future__ import annotations

import unittest

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
)


class ChatHubTests(unittest.TestCase):
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
            content="hello",
        )

        targets = hub.publish(event)

        self.assertEqual(targets, (app_endpoint,))
        self.assertEqual(hub.history(room_id), (event,))

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

    def test_room_subscription_notifies_for_bind_publish_and_clear(self) -> None:
        hub = ChatHub()
        room_id = "minecraft_alpha"
        endpoint = ChatEndpoint(ChatEndpointId.app(room_id), "Minecraft Alpha")
        event = ChatEvent(
            room_id=room_id,
            source=endpoint.id,
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alex"),
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

    def test_author_accepts_hex_colour_for_ui_display(self) -> None:
        author = ChatAuthor(ChatAuthorKind.DISCORD_USER, "Erin", color_hex="#aabbcc")

        self.assertEqual(author.color_hex, "#aabbcc")

    def test_author_accepts_avatar_uri_for_ui_display(self) -> None:
        author = ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alex", avatar_uri="https://mc-heads.net/avatar/Alex/32")

        self.assertEqual(author.avatar_uri, "https://mc-heads.net/avatar/Alex/32")

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
            reference=ChatMessageReference(author_display_name="Alex", content="hello there", event_id="source-1"),
            is_template=True,
            template_values={"player": "Erin"},
            embed=ChatEmbed(title="Relay", description="Forwarded", color=0x336699),
            source_guild_id=789,
            source_channel_id=123,
            source_message_id=456789,
            source_label="Guild",
        )

        restored = ChatEvent.from_mapping(event.to_mapping())

        self.assertEqual(restored, event)

    def test_chat_event_render_content_formats_template_values(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alex"),
            content="{player} joined {app}",
            is_template=True,
        )

        self.assertEqual(event.render_content(app_name="Minecraft Alpha"), "Alex joined Minecraft Alpha")

    def test_chat_event_render_content_returns_raw_non_template_content(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alex"),
            content="hello",
        )

        self.assertEqual(event.render_content(app_name="Minecraft Alpha"), "hello")

    def test_chat_event_to_reference_renders_template_values(self) -> None:
        event = ChatEvent(
            room_id="minecraft_alpha",
            source=ChatEndpointId.app("minecraft_alpha"),
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alex"),
            content="{player} joined {app}",
            is_template=True,
            id="event-1",
        )

        reference = event.to_reference(app_name="Minecraft Alpha")

        self.assertEqual(reference, ChatMessageReference("Alex", "Alex joined Minecraft Alpha", event_id="event-1"))

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
            author=ChatAuthor(ChatAuthorKind.GAME_PLAYER, "Alex"),
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
