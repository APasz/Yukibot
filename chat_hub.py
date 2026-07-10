from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from config import Singleton
from relay_notices import (
    RelayNotice,
    relay_notice_from_mapping,
    relay_notice_to_mapping,
    render_notice_text,
)

log = logging.getLogger(__name__)

_DEFAULT_HISTORY_LIMIT = 250
DEFAULT_CHAT_AUTHOR_COLOR_HEX = "#e4e4e7"


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid.")
    return value


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is invalid.")
    return value


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid.")
    return value


def _optional_text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} is invalid.")
    text = value.strip()
    return text or None


class ChatEndpointKind(StrEnum):
    APP = "app"
    DISCORD_CHANNEL = "discord_channel"
    DISCORD_TTS = "discord_tts"
    WEB_SESSION = "web_session"
    SYSTEM = "system"


class ChatAuthorKind(StrEnum):
    DISCORD_USER = "discord_user"
    GAME_PLAYER = "game_player"
    WEB_USER = "web_user"
    SYSTEM = "system"


class ChatReferenceKind(StrEnum):
    NONE = "none"
    REPLY = "reply"
    FORWARD = "forward"


class ChatMediaProvider(StrEnum):
    DIRECT = "direct"
    TENOR = "tenor"
    GIPHY = "giphy"
    KLIPY = "klipy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChatEndpointId:
    kind: ChatEndpointKind
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("Chat endpoint value must not be empty.")

    @classmethod
    def app(cls, app_name: str) -> ChatEndpointId:
        return cls(ChatEndpointKind.APP, app_name)

    @classmethod
    def discord_channel(cls, channel_id: object) -> ChatEndpointId:
        return cls(ChatEndpointKind.DISCORD_CHANNEL, str(channel_id))

    @classmethod
    def discord_tts(cls, guild_id: object, channel_id: object) -> ChatEndpointId:
        return cls(ChatEndpointKind.DISCORD_TTS, f"{guild_id}:{channel_id}")

    @classmethod
    def web_session(cls, session_id: str) -> ChatEndpointId:
        return cls(ChatEndpointKind.WEB_SESSION, session_id)

    @property
    def stable_key(self) -> str:
        return f"{self.kind.value}:{self.value}"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatEndpointId":
        try:
            kind = ChatEndpointKind(_required_string(payload, "kind"))
        except ValueError as xcp:
            raise ValueError("kind is invalid.") from xcp
        return cls(kind=kind, value=_required_string(payload, "value"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ChatEndpoint:
    id: ChatEndpointId
    label: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatEndpoint":
        raw_id = payload.get("id")
        if not isinstance(raw_id, Mapping):
            raise ValueError("id is invalid.")
        return cls(
            id=ChatEndpointId.from_mapping(raw_id),
            label=_optional_string(payload, "label"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id.to_mapping(),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class ChatRoomUpdate:
    room_id: str
    event: ChatEvent | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.room_id.strip():
            raise ValueError("Chat room update room id must not be empty.")
        if self.event is not None and self.event.room_id.casefold() != self.room_id.casefold():
            raise ValueError("Chat room update event room id is invalid.")
        if self.revision < 0:
            raise ValueError("Chat room update revision must not be negative.")


@dataclass(frozen=True, slots=True)
class ChatAuthor:
    kind: ChatAuthorKind
    display_name: str
    id: str | None = None
    discord_user_id: int | None = None
    color_hex: str | None = None
    avatar_uri: str | None = None

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("Chat author display name must not be empty.")
        if self.color_hex is not None:
            color = self.color_hex.strip()
            if not color.startswith("#") or len(color) != 7:
                raise ValueError("Chat author color must be a #RRGGBB value.")
            try:
                int(color[1:], 16)
            except ValueError as xcp:
                raise ValueError("Chat author color must be a #RRGGBB value.") from xcp
        if self.avatar_uri is not None and not self.avatar_uri.strip():
            raise ValueError("Chat author avatar URI must not be empty.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatAuthor":
        try:
            kind = ChatAuthorKind(_required_string(payload, "kind"))
        except ValueError as xcp:
            raise ValueError("kind is invalid.") from xcp
        raw_discord_user_id = payload.get("discord_user_id")
        if raw_discord_user_id is not None and (
            isinstance(raw_discord_user_id, bool) or not isinstance(raw_discord_user_id, int)
        ):
            raise ValueError("discord_user_id is invalid.")
        return cls(
            kind=kind,
            display_name=_required_string(payload, "display_name"),
            id=_optional_string(payload, "id"),
            discord_user_id=raw_discord_user_id,
            color_hex=_optional_string(payload, "color_hex"),
            avatar_uri=_optional_string(payload, "avatar_uri"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "display_name": self.display_name,
            "id": self.id,
            "discord_user_id": self.discord_user_id,
            "color_hex": self.color_hex,
            "avatar_uri": self.avatar_uri,
        }


@dataclass(frozen=True, slots=True)
class ChatAttachment:
    uri: str
    name: str
    source_url: str | None = None

    def __post_init__(self) -> None:
        if not self.uri.strip():
            raise ValueError("Chat attachment URI must not be empty.")
        if not self.name.strip():
            raise ValueError("Chat attachment name must not be empty.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatAttachment":
        return cls(
            uri=_required_string(payload, "uri"),
            name=_required_string(payload, "name"),
            source_url=_optional_string(payload, "source_url"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "name": self.name,
            "source_url": self.source_url,
        }


@dataclass(frozen=True, slots=True)
class ChatLinkVariant:
    key: str
    label: str
    url: str
    media_type: str | None = None
    extension: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Chat link variant key must not be empty.")
        if not self.label.strip():
            raise ValueError("Chat link variant label must not be empty.")
        if not self.url.strip():
            raise ValueError("Chat link variant URL must not be empty.")
        object.__setattr__(self, "media_type", _optional_text(self.media_type, "media_type"))
        for field_name, value in (
            ("width", self.width),
            ("height", self.height),
            ("size_bytes", self.size_bytes),
        ):
            if value is not None and value < 0:
                raise ValueError(f"Chat link variant {field_name} must not be negative.")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("Chat link variant duration must not be negative.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatLinkVariant":
        def _optional_non_negative_int(key: str) -> int | None:
            raw_value = payload.get(key)
            if raw_value is None:
                return None
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise ValueError(f"{key} is invalid.")
            if raw_value < 0:
                raise ValueError(f"{key} is invalid.")
            return raw_value

        raw_duration = payload.get("duration_seconds")
        duration_seconds: float | None
        if raw_duration is None:
            duration_seconds = None
        elif isinstance(raw_duration, bool) or not isinstance(raw_duration, int | float) or raw_duration < 0:
            raise ValueError("duration_seconds is invalid.")
        else:
            duration_seconds = float(raw_duration)

        return cls(
            key=_required_string(payload, "key"),
            label=_required_string(payload, "label"),
            url=_required_string(payload, "url"),
            media_type=_optional_text(payload.get("media_type"), "media_type"),
            extension=_optional_string(payload, "extension"),
            width=_optional_non_negative_int("width"),
            height=_optional_non_negative_int("height"),
            size_bytes=_optional_non_negative_int("size_bytes"),
            duration_seconds=duration_seconds,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "url": self.url,
            "media_type": self.media_type,
            "extension": self.extension,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class ChatLink:
    url: str
    label: str | None = None
    media_type: str | None = None
    is_media: bool = False
    extension: str | None = None
    original_url: str | None = None
    provider: ChatMediaProvider = ChatMediaProvider.UNKNOWN
    variants: tuple[ChatLinkVariant, ...] = ()

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("Chat link URL must not be empty.")
        object.__setattr__(self, "media_type", _optional_text(self.media_type, "media_type"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatLink":
        raw_is_media = payload.get("is_media", False)
        if not isinstance(raw_is_media, bool):
            raise ValueError("is_media is invalid.")
        provider_value = payload.get("provider", ChatMediaProvider.UNKNOWN.value)
        if not isinstance(provider_value, str):
            raise ValueError("provider is invalid.")
        try:
            provider = ChatMediaProvider(provider_value)
        except ValueError as xcp:
            raise ValueError("provider is invalid.") from xcp
        raw_variants = payload.get("variants", ())
        if not isinstance(raw_variants, list):
            raise ValueError("variants is invalid.")
        variants: list[ChatLinkVariant] = []
        for raw_variant in raw_variants:
            if not isinstance(raw_variant, Mapping):
                raise ValueError("variants is invalid.")
            variants.append(ChatLinkVariant.from_mapping(raw_variant))
        return cls(
            url=_required_string(payload, "url"),
            label=_optional_string(payload, "label"),
            media_type=_optional_text(payload.get("media_type"), "media_type"),
            is_media=raw_is_media,
            extension=_optional_string(payload, "extension"),
            original_url=_optional_string(payload, "original_url"),
            provider=provider,
            variants=tuple(variants),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "url": self.url,
            "label": self.label,
            "media_type": self.media_type,
            "is_media": self.is_media,
            "extension": self.extension,
            "original_url": self.original_url,
            "provider": self.provider.value,
            "variants": [variant.to_mapping() for variant in self.variants],
        }


@dataclass(frozen=True, slots=True)
class ChatEmbed:
    title: str
    description: str
    color: int

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Chat embed title must not be empty.")
        if not self.description.strip():
            raise ValueError("Chat embed description must not be empty.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatEmbed":
        color = payload.get("color")
        if isinstance(color, bool) or not isinstance(color, int):
            raise ValueError("color is invalid.")
        return cls(
            title=_required_string(payload, "title"),
            description=_required_string(payload, "description"),
            color=color,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "title": self.title,
            "description": self.description,
            "color": self.color,
        }


@dataclass(frozen=True, slots=True)
class ChatMessageReference:
    author_display_name: str
    content: str
    event_id: str | None = None
    discord_user_id: int | None = None

    def __post_init__(self) -> None:
        if not self.author_display_name.strip():
            raise ValueError("Chat reference author display name must not be empty.")
        if not self.content.strip():
            raise ValueError("Chat reference content must not be empty.")
        if self.event_id is not None and not self.event_id.strip():
            raise ValueError("Chat reference event id must not be empty.")
        if self.discord_user_id is not None and (
            isinstance(self.discord_user_id, bool) or not isinstance(self.discord_user_id, int)
        ):
            raise ValueError("Chat reference discord user id must be an integer.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatMessageReference":
        raw_discord_user_id = payload.get("discord_user_id")
        if raw_discord_user_id is not None and (
            isinstance(raw_discord_user_id, bool) or not isinstance(raw_discord_user_id, int)
        ):
            raise ValueError("discord_user_id is invalid.")
        return cls(
            author_display_name=_required_string(payload, "author_display_name"),
            content=_required_text(payload, "content"),
            event_id=_optional_string(payload, "event_id"),
            discord_user_id=raw_discord_user_id,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "author_display_name": self.author_display_name,
            "content": self.content,
            "event_id": self.event_id,
            "discord_user_id": self.discord_user_id,
        }


@dataclass(frozen=True, slots=True)
class ChatEvent:
    room_id: str
    source: ChatEndpointId
    author: ChatAuthor
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    attachments: tuple[ChatAttachment, ...] = ()
    links: tuple[ChatLink, ...] = ()
    reference_kind: ChatReferenceKind = ChatReferenceKind.NONE
    reference: ChatMessageReference | None = None
    notice: RelayNotice | None = None
    embed: ChatEmbed | None = None
    source_guild_id: int | None = None
    source_guild_name: str | None = None
    source_channel_id: int | None = None
    source_message_id: int | None = None
    source_label: str | None = None

    def __post_init__(self) -> None:
        if not self.room_id.strip():
            raise ValueError("Chat room id must not be empty.")
        if self.reference is not None and self.reference_kind is ChatReferenceKind.NONE:
            raise ValueError("Chat reference kind must not be none when a reference is present.")

    def resolved_notice(self) -> RelayNotice | None:
        return self.notice

    def render_content(self, *, player_name: str | None = None, app_name: str | None = None) -> str:
        resolved_player_name = player_name or self.author.display_name
        resolved_app_name = app_name or self.room_id
        notice = self.resolved_notice()
        if notice is not None:
            return render_notice_text(notice, author_name=resolved_player_name, app_name=resolved_app_name)
        return self.content

    def to_reference(self, *, player_name: str | None = None, app_name: str | None = None) -> ChatMessageReference:
        preview_content = self.embed.description.strip() if self.embed is not None else ""
        if not preview_content:
            preview_content = self.render_content(player_name=player_name, app_name=app_name).strip()
        if not preview_content:
            if self.attachments or self.links:
                preview_content = "Sent media"
            else:
                preview_content = "Sent a message"
        return ChatMessageReference(
            author_display_name=self.author.display_name,
            content=preview_content,
            event_id=self.id,
            discord_user_id=self.author.discord_user_id,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "ChatEvent":
        raw_source = payload.get("source")
        if not isinstance(raw_source, Mapping):
            raise ValueError("source is invalid.")
        raw_author = payload.get("author")
        if not isinstance(raw_author, Mapping):
            raise ValueError("author is invalid.")
        raw_attachments = payload.get("attachments", ())
        if not isinstance(raw_attachments, Sequence) or isinstance(raw_attachments, (str, bytes)):
            raise ValueError("attachments are invalid.")
        raw_links = payload.get("links", ())
        if not isinstance(raw_links, Sequence) or isinstance(raw_links, (str, bytes)):
            raise ValueError("links are invalid.")
        reference_kind_value = payload.get("reference_kind", ChatReferenceKind.NONE.value)
        if not isinstance(reference_kind_value, str):
            raise ValueError("reference_kind is invalid.")
        try:
            reference_kind = ChatReferenceKind(reference_kind_value)
        except ValueError as xcp:
            raise ValueError("reference_kind is invalid.") from xcp
        raw_notice = payload.get("notice")
        if raw_notice is not None and not isinstance(raw_notice, Mapping):
            raise ValueError("notice is invalid.")
        if "is_template" in payload or "template_values" in payload:
            raise ValueError("Legacy template chat event payloads are no longer supported.")
        raw_created_at = payload.get("created_at", time.time())
        if isinstance(raw_created_at, bool) or not isinstance(raw_created_at, (int, float)):
            raise ValueError("created_at is invalid.")
        raw_source_guild_id = payload.get("source_guild_id")
        if raw_source_guild_id is not None and (
            isinstance(raw_source_guild_id, bool) or not isinstance(raw_source_guild_id, int)
        ):
            raise ValueError("source_guild_id is invalid.")
        raw_source_channel_id = payload.get("source_channel_id")
        if raw_source_channel_id is not None and (
            isinstance(raw_source_channel_id, bool) or not isinstance(raw_source_channel_id, int)
        ):
            raise ValueError("source_channel_id is invalid.")
        raw_source_message_id = payload.get("source_message_id")
        if raw_source_message_id is not None and (
            isinstance(raw_source_message_id, bool) or not isinstance(raw_source_message_id, int)
        ):
            raise ValueError("source_message_id is invalid.")
        raw_embed = payload.get("embed")
        if raw_embed is not None and not isinstance(raw_embed, Mapping):
            raise ValueError("embed is invalid.")
        raw_reference = payload.get("reference")
        if raw_reference is not None and not isinstance(raw_reference, Mapping):
            raise ValueError("reference is invalid.")

        attachments: list[ChatAttachment] = []
        for raw_attachment in raw_attachments:
            if not isinstance(raw_attachment, Mapping):
                raise ValueError("attachments are invalid.")
            attachments.append(ChatAttachment.from_mapping(raw_attachment))

        links: list[ChatLink] = []
        for raw_link in raw_links:
            if not isinstance(raw_link, Mapping):
                raise ValueError("links are invalid.")
            links.append(ChatLink.from_mapping(raw_link))

        event_id = payload.get("id", "")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("id is invalid.")

        return cls(
            room_id=_required_string(payload, "room_id"),
            source=ChatEndpointId.from_mapping(raw_source),
            author=ChatAuthor.from_mapping(raw_author),
            content=_required_text(payload, "content"),
            id=event_id,
            created_at=float(raw_created_at),
            attachments=tuple(attachments),
            links=tuple(links),
            reference_kind=reference_kind,
            reference=ChatMessageReference.from_mapping(raw_reference) if raw_reference is not None else None,
            notice=relay_notice_from_mapping(raw_notice) if raw_notice is not None else None,
            embed=ChatEmbed.from_mapping(raw_embed) if raw_embed is not None else None,
            source_guild_id=raw_source_guild_id,
            source_guild_name=_optional_string(payload, "source_guild_name"),
            source_channel_id=raw_source_channel_id,
            source_message_id=raw_source_message_id,
            source_label=_optional_string(payload, "source_label"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "source": self.source.to_mapping(),
            "author": self.author.to_mapping(),
            "content": self.content,
            "id": self.id,
            "created_at": self.created_at,
            "attachments": [attachment.to_mapping() for attachment in self.attachments],
            "links": [link.to_mapping() for link in self.links],
            "reference_kind": self.reference_kind.value,
            "reference": self.reference.to_mapping() if self.reference is not None else None,
            "notice": relay_notice_to_mapping(self.notice) if self.notice is not None else None,
            "embed": self.embed.to_mapping() if self.embed is not None else None,
            "source_guild_id": self.source_guild_id,
            "source_guild_name": self.source_guild_name,
            "source_channel_id": self.source_channel_id,
            "source_message_id": self.source_message_id,
            "source_label": self.source_label,
        }


class ChatHub(metaclass=Singleton):
    def __init__(self, *, history_limit: int = _DEFAULT_HISTORY_LIMIT) -> None:
        if history_limit <= 0:
            raise ValueError("Chat history limit must be greater than zero.")
        self._history_limit = history_limit
        self._room_endpoints: dict[str, dict[str, ChatEndpoint]] = {}
        self._endpoint_rooms: dict[ChatEndpointId, set[str]] = {}
        self._history: dict[str, deque[ChatEvent]] = {}
        self._discord_source_events: dict[str, dict[tuple[int, int], ChatEvent]] = {}
        self._room_revisions: dict[str, int] = {}
        self._room_subscribers: dict[str, dict[str, Callable[[ChatRoomUpdate], None]]] = {}
        self._lock = threading.RLock()

    def bind(self, room_id: str, endpoint: ChatEndpoint) -> None:
        if not room_id.strip():
            raise ValueError("Chat room id must not be empty.")
        with self._lock:
            room = self._room_endpoints.setdefault(room_id, {})
            room[endpoint.id.stable_key] = endpoint
            self._endpoint_rooms.setdefault(endpoint.id, set()).add(room_id)
            self._increment_room_revision(room_id)
        log.debug("Chat endpoint bound: room=%s endpoint=%s", room_id, endpoint.id.stable_key)
        self._notify_room_subscribers(room_id)

    def bind_many(self, room_id: str, endpoints: Iterable[ChatEndpoint]) -> None:
        for endpoint in endpoints:
            self.bind(room_id, endpoint)

    def clear_room(self, room_id: str) -> None:
        with self._lock:
            endpoints = self._room_endpoints.pop(room_id, {})
            history = self._history.pop(room_id, ())
            self._discord_source_events.pop(room_id, None)
            self._increment_room_revision(room_id)
            for endpoint in endpoints.values():
                rooms = self._endpoint_rooms.get(endpoint.id)
                if rooms is None:
                    continue
                rooms.discard(room_id)
                if not rooms:
                    self._endpoint_rooms.pop(endpoint.id, None)
        log.debug(
            "Chat room bindings cleared: room=%s endpoints=%s history=%s",
            room_id,
            len(endpoints),
            len(tuple(history)),
        )
        self._notify_room_subscribers(room_id)

    def bound_room_ids(self) -> tuple[str, ...]:
        with self._lock:
            room_ids = tuple(sorted(self._room_endpoints, key=str.casefold))
        return room_ids

    def rooms_for_endpoint(self, endpoint_id: ChatEndpointId) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._endpoint_rooms.get(endpoint_id, ()), key=str.casefold))

    def endpoints_for_room(
        self,
        room_id: str,
        *,
        exclude: ChatEndpointId | None = None,
    ) -> tuple[ChatEndpoint, ...]:
        with self._lock:
            endpoints = self._room_endpoints.get(room_id, {})
            values = tuple(endpoints.values())
        if exclude is None:
            return values
        return tuple(endpoint for endpoint in values if endpoint.id != exclude)

    def publish(self, event: ChatEvent) -> tuple[ChatEndpoint, ...]:
        with self._lock:
            history = self._history.setdefault(event.room_id, deque(maxlen=self._history_limit))
            evicted_event = history[0] if len(history) == self._history_limit else None
            history.append(event)
            if evicted_event is not None:
                self._discard_discord_source_event(evicted_event)
            self._index_discord_source_event(event)
            self._increment_room_revision(event.room_id)
            targets = self.endpoints_for_room(event.room_id, exclude=event.source)
        log.debug(
            "Chat event published: room=%s source=%s targets=%s content_len=%s",
            event.room_id,
            event.source.stable_key,
            len(targets),
            len(event.content),
        )
        self._notify_room_subscribers(event.room_id, event=event)
        return targets

    def subscribe(self, room_id: str, callback: Callable[[ChatRoomUpdate], None]) -> str:
        if not room_id.strip():
            raise ValueError("Chat room id must not be empty.")
        subscription_id = uuid.uuid4().hex
        with self._lock:
            self._room_subscribers.setdefault(room_id, {})[subscription_id] = callback
        return subscription_id

    def unsubscribe(self, room_id: str, subscription_id: str) -> None:
        if not room_id.strip():
            raise ValueError("Chat room id must not be empty.")
        with self._lock:
            subscribers = self._room_subscribers.get(room_id)
            if subscribers is None:
                return
            subscribers.pop(subscription_id, None)
            if not subscribers:
                self._room_subscribers.pop(room_id, None)

    def _notify_room_subscribers(self, room_id: str, *, event: ChatEvent | None = None) -> None:
        with self._lock:
            callbacks = tuple(self._room_subscribers.get(room_id, {}).values())
            revision = self._room_revisions.get(room_id, 0)
        if not callbacks:
            return
        update = ChatRoomUpdate(room_id=room_id, event=event, revision=revision)
        for callback in callbacks:
            try:
                callback(update)
            except Exception:
                log.exception("Chat room subscriber callback failed: room=%s", room_id)

    def room_revision(self, room_id: str) -> int:
        with self._lock:
            return self._room_revisions.get(room_id, 0)

    def _increment_room_revision(self, room_id: str) -> int:
        revision = self._room_revisions.get(room_id, 0) + 1
        self._room_revisions[room_id] = revision
        return revision

    @staticmethod
    def _discord_source_key(event: ChatEvent) -> tuple[int, int] | None:
        if event.source.kind is not ChatEndpointKind.DISCORD_CHANNEL:
            return None
        if event.source_channel_id is None or event.source_message_id is None:
            return None
        return event.source_channel_id, event.source_message_id

    def _index_discord_source_event(self, event: ChatEvent) -> None:
        key = self._discord_source_key(event)
        if key is None:
            return
        self._discord_source_events.setdefault(event.room_id, {})[key] = event

    def _discard_discord_source_event(self, event: ChatEvent) -> None:
        key = self._discord_source_key(event)
        if key is None:
            return
        room_events = self._discord_source_events.get(event.room_id)
        if room_events is None:
            return
        if room_events.get(key) is event:
            room_events.pop(key, None)
        if not room_events:
            self._discord_source_events.pop(event.room_id, None)

    def history(self, room_id: str, *, limit: int | None = None) -> tuple[ChatEvent, ...]:
        with self._lock:
            events: Sequence[ChatEvent] = tuple(self._history.get(room_id, ()))
        if limit is None:
            return tuple(events)
        if limit <= 0:
            return ()
        return tuple(events[-limit:])

    def event(self, room_id: str, event_id: str) -> ChatEvent | None:
        with self._lock:
            events = self._history.get(room_id, ())
            for event in reversed(events):
                if event.id == event_id:
                    return event
        return None

    def discord_source_event(self, room_id: str, *, channel_id: int, message_id: int) -> ChatEvent | None:
        with self._lock:
            return self._discord_source_events.get(room_id, {}).get((channel_id, message_id))
