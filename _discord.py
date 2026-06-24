from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence, Sized
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, SupportsInt, cast, runtime_checkable
from urllib.parse import urlparse

import aiohttp
import emoji
import hikari
import lightbulb
from hikari import messages as hikari_messages
from hikari.guilds import Member, Role
from hikari.internal import routes
from hikari.users import OwnUser
from pathvalidate import sanitize_filename
from TenorGrabber import tenorgrabber

import config
from _audit import tenor_log
from _authority import AuthorityResource, read_json_object
from _file import File_Utils
from _minecraft_heads import minecraft_avatar_uri, minecraft_dev_bypass_head_data_uri
from _resolator import Resolutator
from _security import Access_Control
from _utils import Utilities
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
    ChatMediaProvider,
    ChatMessageReference,
    ChatReferenceKind,
)
from config import Name_Cache, NameResolutionResult, NameResolutionStatus, Singleton
from relay_notices import (
    RelayNotice,
    notice_embed_spec,
    notice_hides_body_content,
    render_notice_body,
    render_notice_text,
)

if TYPE_CHECKING:
    from apps._app import App


@runtime_checkable
class RelayTTSService(Protocol):
    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None: ...

    async def queue_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
    ) -> tuple[str, int]: ...


@runtime_checkable
class DiscordRelayTTSService(RelayTTSService, Protocol):
    async def queue_discord_relay_message(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        text: str,
        *,
        user_id: hikari.Snowflakeish | None,
        source_app: str,
        player_name: str,
    ) -> tuple[str, int]: ...


log = logging.getLogger(__name__)
tts_log = logging.getLogger(config.LOGGER_TTS)

DISCORD_EMOJI_REGEX = re.compile(r"<a?:(\w+):\d+>")
DISCORD_USER_MENTION_REGEX = re.compile(r"<@!?(\d+)>")
_KLIPY_MEDIA_URL_RE = re.compile(
    r"https://static\.klipy\.com/[^\s\"'<>]+\.(?:gif|png|jpe?g|webp|mp4|webm)", re.IGNORECASE
)
_GIPHY_ID_RE = re.compile(r"^(?P<slug>.+)-(?P<gif_id>[A-Za-z0-9]+)$")
_TENOR_VIEW_POST_ID_RE = re.compile(r"-(?P<post_id>\d+)$")
_TENOR_STORE_CACHE_RE = re.compile(r'<script id="store-cache"[^>]*>(?P<payload>.*?)</script>', re.IGNORECASE | re.DOTALL)
_TENOR_GIF_VARIANT_ORDER = ("gif", "mediumgif", "tinygif", "nanogif")
_ATTACHMENT_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,16}$")
_ATTACHMENT_UNSAFE_NAME_CHARS_RE = re.compile(r"[\r\n\t,\[\]]+")
_ATTACHMENT_MULTI_SPACE_RE = re.compile(r"\s+")
_TENOR_GRABBER_MODULE: ModuleType = tenorgrabber


def color_int_to_hex(color: int | None) -> str | None:
    if color is None or color == 0:
        return None
    return f"#{color:06x}"


def _role_color_value(role: Role | None) -> int | None:
    if role is None:
        return None
    role_color = int(role.color)
    if role_color == 0:
        return None
    return role_color


class _RoleLike(Protocol):
    @property
    def color(self) -> SupportsInt: ...

    @property
    def position(self) -> int: ...


class _RoleBearingMember(Protocol):
    def get_roles(self) -> Sequence[_RoleLike]: ...

    def get_top_role(self) -> _RoleLike | None: ...


def _role_like_color_value(role: _RoleLike | None) -> int | None:
    if role is None:
        return None
    role_color = int(role.color)
    if role_color == 0:
        return None
    return role_color


def _member_roles_by_position(member: _RoleBearingMember) -> tuple[_RoleLike, ...]:
    roles = tuple(member.get_roles())
    if roles:
        return tuple(sorted(roles, key=lambda role: role.position, reverse=True))
    top_role = member.get_top_role()
    return (top_role,) if top_role is not None else ()


def member_role_color(member: Member | _RoleBearingMember) -> int | None:
    for role in _member_roles_by_position(member):
        role_color = _role_like_color_value(role)
        if role_color is not None:
            return role_color
    return None


def cached_member_role_color(
    bot: hikari.GatewayBot,
    *,
    guild_id: hikari.Snowflakeish,
    user_id: hikari.Snowflakeish,
) -> int | None:
    member: Member | None = bot.cache.get_member(guild_id, user_id)
    if member is None:
        return None
    return member_role_color(member)


def cached_top_role_color(
    bot: hikari.GatewayBot,
    *,
    guild_ids: Sequence[hikari.Snowflakeish],
) -> int | None:
    me: OwnUser | None = bot.get_me()
    if me is None:
        return None

    for guild_id in guild_ids:
        if role_color := cached_member_role_color(bot, guild_id=guild_id, user_id=me.id):
            return role_color

    return None


def _normalise_attachment_extension(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if not text.startswith("."):
        text = "." + text
    if _ATTACHMENT_EXTENSION_RE.fullmatch(text) is None:
        return None
    if text == ".jpe":
        return ".jpg"
    return text


def _guess_attachment_extension(media_type: str | None) -> str | None:
    if media_type is None:
        return None
    guessed = mimetypes.guess_extension(media_type, strict=False)
    return _normalise_attachment_extension(guessed)


def _media_extension_from_url(url: str, *, supported_extensions: Collection[str]) -> str | None:
    path = urlparse(url).path
    normalised = _normalise_attachment_extension(Path(path).suffix)
    if normalised is None:
        return None
    extension = normalised.removeprefix(".")
    if extension not in supported_extensions:
        return None
    return extension


def _guess_media_type_from_url(url: str) -> str | None:
    guessed_type, _ = mimetypes.guess_type(url, strict=False)
    if guessed_type is None:
        return None
    return guessed_type.lower()


def _normalise_attachment_stem(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = _ATTACHMENT_UNSAFE_NAME_CHARS_RE.sub(" ", raw.strip())
    text = _ATTACHMENT_MULTI_SPACE_RE.sub(" ", text).strip(" .")
    if not text:
        return None
    sanitised = sanitize_filename(text, platform="universal").strip(" .")
    if not sanitised:
        return None
    return sanitised


def normalise_attachment_relay_name(attachment: hikari.Attachment) -> str:
    title_name = attachment.title.strip() if attachment.title is not None else ""
    filename_name = attachment.filename.strip()
    title_path = Path(title_name) if title_name else None
    filename_path = Path(filename_name)

    extension = (
        _normalise_attachment_extension(filename_path.suffix)
        or _normalise_attachment_extension(title_path.suffix if title_path is not None else None)
        or _guess_attachment_extension(attachment.media_type)
    )
    preferred_stem = _normalise_attachment_stem(title_path.stem if title_path is not None else None)
    fallback_stem = _normalise_attachment_stem(filename_path.stem or filename_path.name)
    stem = preferred_stem or fallback_stem or "attachment"
    if extension is None:
        return stem
    return f"{stem}{extension}"


class AM_Receiver(Protocol):
    async def send(self, payload: App_Bound): ...


class Distils:
    file = File_Utils()
    util = Utilities()

    @classmethod
    async def _deliver_files(
        cls,
        paths: list[Path],
        *,
        base_name: str,
        force_download: bool,
        force_zip: bool,
        send_text: Callable[[str], Awaitable[object]],
        send_many_files: Callable[[str, list[hikari.File]], Awaitable[object]],
        send_single_file: Callable[[str, hikari.File], Awaitable[object]],
    ) -> FileDeliveryMode:
        if not paths:
            raise ValueError("paths list must not be empty")

        zip_name = base_name + ".zip" if not base_name.endswith(".zip") else ""
        delivery_paths = paths
        if force_zip:
            delivery_paths = [await cls.file.compress(paths, zip_name)]

        if force_download:
            await send_text(await cls.build_direct_file_message(delivery_paths, base_name))
            return FileDeliveryMode.DIRECT

        if len(delivery_paths) <= 10:
            try:
                total_size = sum(cls.file.pointer_size(path) for path in delivery_paths)
                if total_size < config.DISCORD_UPLOAD_LIMIT:
                    await send_many_files(
                        f"Here ya go, `{base_name}`",
                        [hikari.File(str(path)) for path in delivery_paths],
                    )
                    return FileDeliveryMode.ATTACHMENTS
            except Exception:
                log.warning("Failed size pre-check, continuing anyway")

        zip_path = await cls.file.compress(delivery_paths, zip_name)
        try:
            if cls.file.pointer_size(zip_path) < config.DISCORD_UPLOAD_LIMIT:
                await send_single_file(f"Your file sweets, `{base_name}`", hikari.File(str(zip_path)))
                return FileDeliveryMode.ZIP
        except hikari.HTTPResponseError as xcp:
            xcp.code
            log.warning(f"Zipped-all upload failed: {xcp}")
        except Exception:
            log.exception("Compression or zipped upload failed")

        await send_text(await cls.build_direct_file_message([zip_path], base_name))
        return FileDeliveryMode.DIRECT

    @classmethod
    async def respond_files(
        cls,
        ctx: lightbulb.Context,
        paths: list[Path],
        *,
        display_name: str = "mods",
        app_name: str | None = None,
        force_download: bool = False,
        force_zip: bool = False,
    ) -> FileDeliveryMode:
        base_name = f"{app_name}_{display_name}" if app_name else display_name
        return await cls._deliver_files(
            paths,
            base_name=base_name,
            force_download=force_download,
            force_zip=force_zip,
            send_text=lambda content: ctx.respond(content),
            send_many_files=lambda content, attachments: ctx.respond(content, attachments=attachments),
            send_single_file=lambda content, attachment: ctx.respond(content, attachment=attachment),
        )

    @classmethod
    async def send_files(
        cls,
        rest: hikari.api.RESTClient,
        channel_id: hikari.Snowflakeish,
        paths: list[Path],
        *,
        display_name: str = "mods",
        app_name: str | None = None,
        force_download: bool = False,
        force_zip: bool = False,
    ) -> FileDeliveryMode:
        base_name = f"{app_name}_{display_name}" if app_name else display_name
        return await cls._deliver_files(
            paths,
            base_name=base_name,
            force_download=force_download,
            force_zip=force_zip,
            send_text=lambda content: rest.create_message(channel_id, content),
            send_many_files=lambda content, attachments: rest.create_message(
                channel_id,
                content,
                attachments=attachments,
            ),
            send_single_file=lambda content, attachment: rest.create_message(
                channel_id,
                content,
                attachment=attachment,
            ),
        )

    @classmethod
    async def direct(cls, ctx: lightbulb.Context, paths: Collection[Path], base_name: str):
        msg = await cls.build_direct_file_message(paths, base_name)
        await ctx.respond(msg)

    @classmethod
    async def build_direct_file_message(cls, paths: Collection[Path], base_name: str) -> str:
        links: list[str] = []
        files: list[Path] = []

        if 1 < len(paths) < 5:
            for path in paths:
                link, pointer = cls.util.linkify(path)
                links.append(link)
                files.append(pointer)
        else:
            archive = await cls.file.compress(paths, base_name)
            link, pointer = cls.util.linkify(archive)
            links.append(link)
            files.append(pointer)

        expire = cls.util.nice_time(config.UPLOAD_CLEAR_TIME)
        size = sum(File_Utils.pointer_size(path) for path in files)
        return f"`{base_name}` {Utilities.humanise_bytes(size)} expires {expire}\n" + "\n".join(links)

    @staticmethod
    def cat_name(
        var: str,
        validator: tuple[Collection[str] | None, Collection[str] | None] = (None, None),
        *,
        lower: bool = True,
    ) -> tuple[str, str]:
        try:
            var1, var2 = [e.strip().lower() if lower else e.strip() for e in var.split(":", 1)]
            val1, val2 = validator
            if not var1:
                raise ValueError("var1 Missing")
            if not var2:
                raise ValueError("var2 Missing")
            if val1:
                if lower:
                    var1 = var1.lower().strip()
                    val1 = {e.lower().strip() for e in val1}
                if var1 not in val1:
                    raise ValueError(f"{var1} not in {val1} | {lower=}")
            if val2:
                if lower:
                    var2 = var2.lower().strip()
                    val2 = {e.lower().strip() for e in val2}
                if var2 not in val2:
                    raise ValueError(f"{var2} not in {val2} | {lower=}")
        except ValueError as xcp:
            log.exception(f"CatName: {var=} against {validator=}\n{xcp}")
            raise config.AC_XCP
        except Exception as xcp:
            log.exception("UserInput")
            raise xcp
        return var1, var2

    @staticmethod
    async def ac_focused_static(ctx: lightbulb.AutocompleteContext[str], to_send: Collection[str]) -> None:
        if not isinstance(ctx.focused.value, str):
            raise ValueError(f"String go with strings, not {type(ctx.focused.value)}")
        foc_val = ctx.focused.value.lower()
        await ctx.respond([hikari.impl.AutocompleteChoiceBuilder(e, e) for e in to_send if foc_val in e.lower()][:25])

    @staticmethod
    async def ac_focused_mutate(
        ctx: lightbulb.AutocompleteContext[str],
        to_send: dict[str, object],
        caller: Callable[[str, object], tuple[str, str | int | float]],
    ):
        if not isinstance(ctx.focused.value, str):
            raise ValueError(f"String go with strings, not {type(ctx.focused.value)}")
        foc_val = ctx.focused.value.lower()
        acb = hikari.impl.AutocompleteChoiceBuilder
        await ctx.respond([acb(*caller(k, v)) for k, v in to_send.items() if foc_val in k.lower()][:25])


class FileDeliveryMode(Enum):
    ATTACHMENTS = "attachments"
    ZIP = "zip"
    DIRECT = "direct"


class RelayWorkerStatus(Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"


class RelayMessageReferenceKind(Enum):
    NONE = "none"
    REPLY = "reply"
    FORWARD = "forward"


class MediaProvider(Enum):
    DIRECT = "direct"
    TENOR = "tenor"
    GIPHY = "giphy"
    KLIPY = "klipy"
    UNKNOWN = "unknown"


class MessageEnrichmentState(Enum):
    DISABLED = "disabled"
    PENDING = "pending"
    COMPLETE = "complete"


@dataclass(slots=True, frozen=True)
class RelayEmbedPayload:
    title: str
    description: str
    color: int


@dataclass(frozen=True, slots=True)
class DiscordTextRoute:
    channel_id: hikari.Snowflake
    guild_id: hikari.Snowflake | None


@dataclass(frozen=True, slots=True)
class DiscordTTSRoute:
    guild_id: hikari.Snowflake
    channel_id: hikari.Snowflake


@dataclass(slots=True, frozen=True)
class Fileish:
    uri: str
    name: str
    source_url: str | None = None


@dataclass(slots=True, frozen=True)
class DiscordAttachmentDownloadBatch:
    files: tuple[Fileish, ...]
    failed_count: int = 0

    def __post_init__(self) -> None:
        if self.failed_count < 0:
            raise ValueError("failed_count must not be negative.")

    @property
    def has_failures(self) -> bool:
        return self.failed_count > 0


@dataclass(slots=True, frozen=True)
class URLVariant:
    key: str
    label: str
    url: str
    type: str | None = None
    extension: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    duration_seconds: float | None = None


@dataclass(slots=True)
class URLish:
    url: str
    label: str | None = None
    type: str | None = None
    is_media: bool = False
    extension: str | None = None
    orig_url: str | None = None
    provider: MediaProvider = MediaProvider.UNKNOWN
    variants: tuple[URLVariant, ...] = ()

    def __hash__(self) -> int:
        return hash(self.url)


class Message:
    app: "App"
    _string: str
    player: str | int | hikari.UndefinedType
    urls: set["URLish"]
    files: set["Fileish"]
    _enrichment_state: MessageEnrichmentState
    relay_embed: RelayEmbedPayload | None
    notice: RelayNotice | None

    _md_link_re: re.Pattern[str] = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|www\.[^\s)]+)\)")
    _url_re: re.Pattern[str] = re.compile(r"\b(https?://[^\s<>()\[\]]+|www\.[^\s<>()\[\]]+)")
    _media_exts: frozenset[str] = frozenset(
        {
            "jpg",
            "jpeg",
            "png",
            "ico",
            "bmp",
            "jfif",
            "gif",
            "webp",
            "mp4",
            "webm",
            "ogg",
            "mp3",
            "wav",
            "flac",
        }
    )

    __slots__ = ("app", "_string", "player", "urls", "files", "_enrichment_state", "relay_embed", "notice")

    def __init__(
        self,
        content: str,
        player: str | int | hikari.UndefinedType,
        files: Sequence[Fileish] | None,
        enrich: bool = True,
        relay_embed: RelayEmbedPayload | None = None,
        notice: RelayNotice | None = None,
    ) -> None:
        self.app = cast("App", object())
        self._string = content
        self.player = player
        self.urls = set()
        self.files = set(files or ())
        self._enrichment_state = MessageEnrichmentState.PENDING if enrich else MessageEnrichmentState.DISABLED

        self.relay_embed = relay_embed
        self.notice = notice

    @staticmethod
    def demojise_discord(text: str) -> str:
        return DISCORD_EMOJI_REGEX.sub(r":\1:", text)

    @property
    def content(self) -> str:
        return self.demojise_discord(self._string)

    @property
    def content_demojised(self) -> str:
        return emoji.demojize(self.content)

    async def find_urls(self) -> set["URLish"]:
        self.urls = await self._enrich_links(self._match_urls(self._string))
        self._enrichment_state = MessageEnrichmentState.COMPLETE
        return self.urls

    async def ensure_enriched(self) -> set["URLish"]:
        if self._enrichment_state is not MessageEnrichmentState.PENDING:
            return self.urls
        return await self.find_urls()

    def _match_urls(self, text: str) -> dict[str, str | None]:
        urls: dict[str, str | None] = {}
        for label, url in cast(Sequence[tuple[str, str]], self._md_link_re.findall(text)):
            urls[url] = label
        for url in cast(Sequence[str], self._url_re.findall(text)):
            urls.setdefault(url, None)

        return urls

    @staticmethod
    def _media_provider_for_url(url: str) -> MediaProvider:
        host = (urlparse(url).hostname or "").casefold()
        if "tenor.com" in host:
            return MediaProvider.TENOR
        if "giphy.com" in host:
            return MediaProvider.GIPHY
        if "klipy.com" in host:
            return MediaProvider.KLIPY
        if "static.klipy.com" in host:
            return MediaProvider.KLIPY
        return MediaProvider.DIRECT if host else MediaProvider.UNKNOWN

    @staticmethod
    def _resolve_tenor_media_url(url: str) -> str | None:
        getgiflink = cast(Callable[[str], object] | None, getattr(_TENOR_GRABBER_MODULE, "getgiflink", None))
        if getgiflink is None:
            return None
        result = getgiflink(url)
        return result if isinstance(result, str) and result else None

    @staticmethod
    def _tenor_view_post_id(url: str) -> str | None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if "tenor.com" not in host:
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return None
        match = _TENOR_VIEW_POST_ID_RE.search(path_parts[-1])
        if match is None:
            return None
        return match.group("post_id")

    @staticmethod
    def _tenor_store_cache_payload(raw_html: str) -> Mapping[str, object] | None:
        match = _TENOR_STORE_CACHE_RE.search(raw_html)
        if match is None:
            return None
        try:
            payload = json.loads(html.unescape(match.group("payload")))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, Mapping):
            return None
        return payload

    @staticmethod
    def _is_tenor_gif_variant_key(key: str) -> bool:
        return key == "gif" or (key.endswith("gif") and key != "gifpreview")

    @classmethod
    def _ordered_tenor_gif_variant_keys(cls, media_formats: Mapping[str, object]) -> tuple[str, ...]:
        preferred_keys = tuple(key for key in _TENOR_GIF_VARIANT_ORDER if isinstance(media_formats.get(key), Mapping))
        extra_keys = tuple(
            sorted(
                key
                for key, value in media_formats.items()
                if isinstance(key, str)
                and cls._is_tenor_gif_variant_key(key)
                and key not in _TENOR_GIF_VARIANT_ORDER
                and isinstance(value, Mapping)
            )
        )
        return preferred_keys + extra_keys

    @classmethod
    def _tenor_gif_variants_from_store_cache(cls, raw_html: str, url: str) -> tuple[URLVariant, ...]:
        post_id = cls._tenor_view_post_id(url)
        if post_id is None:
            return ()
        store_cache = cls._tenor_store_cache_payload(raw_html)
        if store_cache is None:
            return ()
        raw_gifs = store_cache.get("gifs")
        if not isinstance(raw_gifs, Mapping):
            return ()
        raw_by_id = raw_gifs.get("byId")
        if not isinstance(raw_by_id, Mapping):
            return ()
        raw_entry = raw_by_id.get(post_id)
        if not isinstance(raw_entry, Mapping):
            return ()
        raw_results = raw_entry.get("results")
        if not isinstance(raw_results, list):
            return ()
        raw_result = next((item for item in raw_results if isinstance(item, Mapping)), None)
        if raw_result is None:
            return ()
        raw_media_formats = raw_result.get("media_formats")
        if not isinstance(raw_media_formats, Mapping):
            return ()

        media_formats: dict[str, Mapping[str, object]] = {
            key: value
            for key, value in raw_media_formats.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }
        variants: list[URLVariant] = []
        seen_urls: set[str] = set()
        for key in cls._ordered_tenor_gif_variant_keys(media_formats):
            raw_variant = media_formats.get(key)
            if raw_variant is None:
                continue
            variant_url = raw_variant.get("url")
            if not isinstance(variant_url, str) or not variant_url:
                continue
            if variant_url in seen_urls:
                continue
            raw_dims = raw_variant.get("dims")
            width: int | None = None
            height: int | None = None
            if isinstance(raw_dims, list) and len(raw_dims) >= 2:
                raw_width = raw_dims[0]
                raw_height = raw_dims[1]
                if isinstance(raw_width, int) and not isinstance(raw_width, bool):
                    width = raw_width
                if isinstance(raw_height, int) and not isinstance(raw_height, bool):
                    height = raw_height
            raw_size_bytes = raw_variant.get("size")
            size_bytes = raw_size_bytes if isinstance(raw_size_bytes, int) and not isinstance(raw_size_bytes, bool) else None
            raw_duration = raw_variant.get("duration")
            duration_seconds = (
                float(raw_duration)
                if isinstance(raw_duration, int | float) and not isinstance(raw_duration, bool)
                else None
            )
            extension = _media_extension_from_url(variant_url, supported_extensions=cls._media_exts)
            variants.append(
                URLVariant(
                    key=key,
                    label="original" if key == "gif" else key,
                    url=variant_url,
                    type=_guess_media_type_from_url(variant_url),
                    extension=extension,
                    width=width,
                    height=height,
                    size_bytes=size_bytes,
                    duration_seconds=duration_seconds,
                )
            )
            seen_urls.add(variant_url)
        return tuple(variants)

    async def _resolve_tenor_media_variants(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> tuple[URLVariant, ...]:
        raw_html = await self._fetch_page_html(session, url)
        if raw_html is None:
            return ()
        return self._tenor_gif_variants_from_store_cache(raw_html, url)

    @staticmethod
    def _log_tenor_link_metadata(
        *,
        original_url: str,
        resolved_url: str | None,
        final_url: str,
        content_type: str | None,
        variants: Sequence[URLVariant],
    ) -> None:
        tenor_log(
            "tenor_link",
            original_url=original_url,
            post_id=Message._tenor_view_post_id(original_url),
            resolved_url=resolved_url,
            final_url=final_url,
            content_type=content_type,
            variants=[
                {
                    "key": variant.key,
                    "label": variant.label,
                    "url": variant.url,
                    "media_type": variant.type,
                    "extension": variant.extension,
                    "width": variant.width,
                    "height": variant.height,
                    "size_bytes": variant.size_bytes,
                    "duration_seconds": variant.duration_seconds,
                }
                for variant in variants
            ],
        )

    @staticmethod
    def _resolve_giphy_media_url(url: str) -> str | None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if "giphy.com" not in host:
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return None
        last_part = path_parts[-1]
        match = _GIPHY_ID_RE.fullmatch(last_part)
        if match is None:
            return None
        gif_id = match.group("gif_id")
        return f"https://media1.giphy.com/media/{gif_id}/giphy-preview.gif"

    @staticmethod
    def _extract_klipy_media_url_from_html(raw_html: str) -> str | None:
        match = _KLIPY_MEDIA_URL_RE.search(html.unescape(raw_html))
        if match is None:
            return None
        return match.group(0)

    async def _fetch_page_html(self, session: aiohttp.ClientSession, url: str) -> str | None:
        try:
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=8),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/137.0.0.0 Safari/537.36"
                    )
                },
            ) as resp:
                if resp.status >= 400:
                    return None
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" not in content_type:
                    return None
                return await resp.text()
        except Exception:
            return None

    async def _resolve_special_media_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
        provider: MediaProvider,
        *,
        tenor_variants: Sequence[URLVariant] = (),
    ) -> str | None:
        if provider is MediaProvider.TENOR:
            if tenor_variants:
                for variant in tenor_variants:
                    if variant.key == "gif":
                        return variant.url
                return tenor_variants[0].url
            try:
                return await asyncio.to_thread(self._resolve_tenor_media_url, url)
            except Exception:
                return None
        if provider is MediaProvider.GIPHY:
            return self._resolve_giphy_media_url(url)
        if provider is MediaProvider.KLIPY:
            parsed = urlparse(url)
            host = (parsed.hostname or "").casefold()
            if host == "static.klipy.com":
                return url
            raw_html = await self._fetch_page_html(session, url)
            if raw_html is None:
                return None
            return self._extract_klipy_media_url_from_html(raw_html)
        return None

    async def _resolve_url_metadata(self, session: aiohttp.ClientSession, url: str) -> tuple[str, str | None]:
        try:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return str(resp.url), resp.headers.get("Content-Type", "").lower()
        except Exception:
            return url, None

    async def _enrich_links(self, links: dict[str, str | None]) -> set[URLish]:
        enriched: set[URLish] = set()

        async with aiohttp.ClientSession() as session:

            async def enrich_one(url: str, label: str | None) -> None:
                provider = self._media_provider_for_url(url)
                tenor_variants: tuple[URLVariant, ...] = ()
                if provider is MediaProvider.TENOR:
                    tenor_variants = await self._resolve_tenor_media_variants(session, url)
                special_url = await self._resolve_special_media_url(
                    session,
                    url,
                    provider,
                    tenor_variants=tenor_variants,
                )
                final_url, content_type = await self._resolve_url_metadata(session, special_url or url)
                extension = _media_extension_from_url(final_url, supported_extensions=self._media_exts)
                is_media = extension is not None or (
                    content_type is not None and content_type.startswith(("image/", "video/", "audio/"))
                )

                urlish = URLish(
                    final_url,
                    label,
                    content_type,
                    is_media,
                    extension,
                    orig_url=url,
                    provider=provider,
                    variants=tenor_variants,
                )
                log.debug(f"{urlish=}")
                log.debug("special_media_url=%r", special_url)
                if provider is MediaProvider.TENOR:
                    self._log_tenor_link_metadata(
                        original_url=url,
                        resolved_url=special_url,
                        final_url=final_url,
                        content_type=content_type,
                        variants=tenor_variants,
                    )
                enriched.add(urlish)

            await asyncio.gather(*(enrich_one(url, label) for url, label in links.items()))

        return enriched


class DC_Bound(Message):
    __slots__ = (
        "app",
        "_string",
        "player",
        "player_id",
        "player_avatar_uri",
        "player_resolution",
        "urls",
        "files",
        "_enrichment_state",
        "relay_embed",
        "notice",
    )
    player_id: int | None
    player_avatar_uri: str | None
    player_resolution: NameResolutionResult

    def __init__(
        self,
        app: "App",
        content: str,
        player: str | int | hikari.UndefinedType,
        files: Sequence[Fileish] | None = None,
        relay_embed: RelayEmbedPayload | None = None,
        notice: RelayNotice | None = None,
        player_id: int | None = None,
        player_avatar_uri: str | None = None,
    ) -> None:
        super().__init__(content, player, files, relay_embed=relay_embed, notice=notice)
        self.app = app
        if player_id is None:
            self.player_resolution = Name_Cache().resolve_name(
                str(player),
                app.scope,
                platforms=getattr(app, "name_platforms", ()),
                preferred_platform=getattr(app, "preferred_name_platform", None),
            )
            self.player_id = self.player_resolution.user_id
        else:
            if isinstance(player_id, bool):
                raise TypeError("DC_Bound player_id must not be a boolean.")
            self.player_id = player_id
            self.player_resolution = NameResolutionResult(NameResolutionStatus.UNIQUE, player_id)
        if player_avatar_uri is None:
            self.player_avatar_uri = None
        else:
            self.player_avatar_uri = player_avatar_uri.strip() or None

        log.debug(f"Create DC_Message: {player} @ {self.app.name}")

    def __repr__(self) -> str:
        return f"<DC: {self.app.name} with {len(self.content)} chars / {len(self.urls)} URLs / {len(self.files)} files for {self.app.chat_channel}>"


class App_Bound(Message):
    __slots__ = (
        "app",
        "chan",
        "source_guild_id",
        "source_message_id",
        "_string",
        "player",
        "urls",
        "files",
        "_enrichment_state",
        "relay_embed",
        "notice",
        "reference_kind",
        "reference",
    )

    def __init__(
        self,
        chan: hikari.TextableChannel,
        content: str,
        player: str | int | hikari.UndefinedType,
        files: Sequence[Fileish] | None = None,
        *,
        enrich: bool = True,
        reference_kind: RelayMessageReferenceKind = RelayMessageReferenceKind.NONE,
        reference: ChatMessageReference | None = None,
        relay_embed: RelayEmbedPayload | None = None,
        notice: RelayNotice | None = None,
        source_guild_id: hikari.Snowflakeish | None = None,
        source_message_id: hikari.Snowflakeish | None = None,
    ) -> None:
        super().__init__(content, player, files, enrich=enrich, relay_embed=relay_embed, notice=notice)
        self.chan = chan
        self.reference_kind = reference_kind
        self.reference = reference
        self.source_guild_id = hikari.Snowflake(source_guild_id) if source_guild_id is not None else None
        self.source_message_id = hikari.Snowflake(source_message_id) if source_message_id is not None else None
        log.debug(f"Create App_Message: {player} from {chan.name or chan.id}")

    @property
    def alias(self) -> str:
        if isinstance(self.player, int):
            if player := self.app.name_cache.get_game_alias(self.player, self.app.scope):
                return player
        elif self.player:
            return self.player
        return "UNDEFINED"

    def content_for_app(self, app: "App") -> str:
        base_content = self.demojise_discord(self._string)
        platforms = getattr(app, "name_platforms", ())
        preferred_platform = getattr(app, "preferred_name_platform", None)

        def replace_mention(match: re.Match[str]) -> str:
            mentioned_user_id = int(match.group(1))
            resolved_name = app.name_cache.relay_mention_name(
                mentioned_user_id,
                scope=app.scope,
                platforms=platforms,
                preferred_platform=preferred_platform,
                preferred_guild_id=self.source_guild_id,
            )
            return f"@{resolved_name}"

        return DISCORD_USER_MENTION_REGEX.sub(replace_mention, base_content)

    def content_demojised_for_app(self, app: "App") -> str:
        return emoji.demojize(self.content_for_app(app))

    def __repr__(self) -> str:
        return f"<APP: {self.player} with {len(self.content)} chars / {len(self.urls)} URLs / {len(self.files)} files>"


@dataclass(frozen=True, slots=True)
class RelayOutboundFormatOptions:
    base_content: str
    link_renderer: Callable[[URLish], str | None] | None = None
    file_renderer: Callable[[Fileish, str], str | None] | None = None
    reference_renderer: Callable[[RelayMessageReferenceKind], str | None] | None = None


@dataclass(frozen=True, slots=True)
class _DiscordRelayMessageRecord:
    channel_id: int
    message_id: int
    source_event_id: str
    reference: ChatMessageReference


class OutboundRelayFormatter:
    @staticmethod
    def public_file_url(file: Fileish) -> str:
        public_url, _ = Utilities.linkify(Path(file.uri))
        return public_url

    @staticmethod
    def _sorted_urls(urls: Collection[URLish]) -> tuple[URLish, ...]:
        return tuple(sorted(urls, key=lambda link: (link.orig_url or link.url, link.url, link.label or "")))

    @staticmethod
    def _sorted_files(files: Collection[Fileish]) -> tuple[Fileish, ...]:
        return tuple(sorted(files, key=lambda file: (file.name, file.uri)))

    @classmethod
    def format_payload(cls, payload: App_Bound, options: RelayOutboundFormatOptions) -> str:
        content = options.base_content
        if options.link_renderer is not None:
            for link in cls._sorted_urls(payload.urls):
                replacement = options.link_renderer(link)
                if replacement is None:
                    continue
                content = content.replace(link.orig_url or link.url, replacement)

        rendered_files: list[str] = []
        for file in cls._sorted_files(payload.files):
            public_url = cls.public_file_url(file)
            if options.file_renderer is None:
                rendered = public_url
            else:
                rendered = options.file_renderer(file, public_url)
            if rendered is None:
                continue
            value = rendered.strip()
            if value:
                rendered_files.append(value)

        if rendered_files:
            suffix = " ".join(rendered_files)
            content = f"{content} {suffix}" if content else suffix

        if options.reference_renderer is not None:
            reference_kind = getattr(payload, "reference_kind", RelayMessageReferenceKind.NONE)
            prefix = options.reference_renderer(reference_kind)
            if prefix:
                prefix_value = prefix.strip()
                content = f"{prefix_value} {content}" if content else prefix_value
        return content.strip()


def relay_reference_kind_for_message(message: hikari.Message) -> RelayMessageReferenceKind:
    if message.type is hikari.MessageType.REPLY:
        return RelayMessageReferenceKind.REPLY
    reference = message.message_reference
    if reference is not None and getattr(reference, "type", None) is hikari_messages.MessageReferenceType.FORWARD:
        return RelayMessageReferenceKind.FORWARD
    return RelayMessageReferenceKind.NONE


def render_plain_reference_prefix(reference_kind: RelayMessageReferenceKind) -> str | None:
    if reference_kind is RelayMessageReferenceKind.REPLY:
        return "reply;"
    if reference_kind is RelayMessageReferenceKind.FORWARD:
        return "forwarded;"
    return None


def _chat_reference_kind(reference_kind: RelayMessageReferenceKind) -> ChatReferenceKind:
    if reference_kind is RelayMessageReferenceKind.REPLY:
        return ChatReferenceKind.REPLY
    if reference_kind is RelayMessageReferenceKind.FORWARD:
        return ChatReferenceKind.FORWARD
    return ChatReferenceKind.NONE


def _relay_reference_kind(reference_kind: ChatReferenceKind) -> RelayMessageReferenceKind:
    if reference_kind is ChatReferenceKind.REPLY:
        return RelayMessageReferenceKind.REPLY
    if reference_kind is ChatReferenceKind.FORWARD:
        return RelayMessageReferenceKind.FORWARD
    return RelayMessageReferenceKind.NONE


def _chat_media_provider(provider: MediaProvider) -> ChatMediaProvider:
    try:
        return ChatMediaProvider(provider.value)
    except ValueError:
        return ChatMediaProvider.UNKNOWN


def _media_provider(provider: ChatMediaProvider) -> MediaProvider:
    try:
        return MediaProvider(provider.value)
    except ValueError:
        return MediaProvider.UNKNOWN


def _chat_attachment(file: Fileish) -> ChatAttachment:
    return ChatAttachment(uri=file.uri, name=file.name, source_url=file.source_url)


def _fileish(attachment: ChatAttachment) -> Fileish:
    return Fileish(uri=attachment.uri, name=attachment.name, source_url=attachment.source_url)


def _discord_attachment_fileish(attachment: hikari.Attachment) -> Fileish:
    return Fileish(uri=attachment.url, name=normalise_attachment_relay_name(attachment), source_url=attachment.url)


def _chat_link_variant(variant: URLVariant) -> ChatLinkVariant:
    return ChatLinkVariant(
        key=variant.key,
        label=variant.label,
        url=variant.url,
        media_type=variant.type,
        extension=variant.extension,
        width=variant.width,
        height=variant.height,
        size_bytes=variant.size_bytes,
        duration_seconds=variant.duration_seconds,
    )


def _url_variant(variant: ChatLinkVariant) -> URLVariant:
    return URLVariant(
        key=variant.key,
        label=variant.label,
        url=variant.url,
        type=variant.media_type,
        extension=variant.extension,
        width=variant.width,
        height=variant.height,
        size_bytes=variant.size_bytes,
        duration_seconds=variant.duration_seconds,
    )


def _chat_link(link: URLish) -> ChatLink:
    return ChatLink(
        url=link.url,
        label=link.label,
        media_type=link.type,
        is_media=link.is_media,
        extension=link.extension,
        original_url=link.orig_url,
        provider=_chat_media_provider(link.provider),
        variants=tuple(_chat_link_variant(variant) for variant in link.variants),
    )


def _urlish(link: ChatLink) -> URLish:
    return URLish(
        url=link.url,
        label=link.label,
        type=link.media_type,
        is_media=link.is_media,
        extension=link.extension,
        orig_url=link.original_url,
        provider=_media_provider(link.provider),
        variants=tuple(_url_variant(variant) for variant in link.variants),
    )


def _chat_embed(embed: RelayEmbedPayload | None) -> ChatEmbed | None:
    if embed is None:
        return None
    return ChatEmbed(title=embed.title, description=embed.description, color=embed.color)


def _relay_embed(embed: ChatEmbed | None) -> RelayEmbedPayload | None:
    if embed is None:
        return None
    return RelayEmbedPayload(title=embed.title, description=embed.description, color=embed.color)


class DC_Relay(metaclass=Singleton):
    queue: deque[DC_Bound] = deque()
    _channel_objects: dict[hikari.Snowflakeish, hikari.TextableChannel] = {}
    _chat_channels: dict[hikari.Snowflakeish, set["App"]] = {}
    _chat_apps: dict[str, "App"] = {}
    _special_channels: dict[hikari.Snowflakeish, set[tuple[str, Callable[["App_Bound"], Awaitable[None]]]]] = {}
    _resolution_miss_counts: dict[tuple[str, str], int] = {}
    _resolution_notice_at: dict[tuple[str, str, NameResolutionStatus], float] = {}
    _CHAT_AUTHOR_COLOR_CACHE_SECONDS: float = 15 * 60
    _CHANNEL_RESOLUTION_MISS_CACHE_SECONDS: float = 5 * 60
    _RELAY_OWNER_BOT_CACHE_SECONDS: float = 5 * 60
    _MAX_ACTIVE_CHAT_TEXT_CHANNELS: int = 2
    _MAX_TRACKED_DISCORD_CHAT_MESSAGES: int = 5_000
    _QUEUE_WORKER_RESTART_DELAY_SECONDS: float = 1.0
    "channel: Apps"
    names = Name_Cache()

    def __init__(self, bot: hikari.GatewayBot) -> None:
        self.bot = bot
        self.reso = Resolutator()
        self.chat_hub = ChatHub()
        self._voice_tts: RelayTTSService | None = None
        self._relay_loop: asyncio.AbstractEventLoop | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._queue_worker_restart_task: asyncio.Task[None] | None = None
        self._queue_worker_should_run: bool = False
        self._queue_worker_status: RelayWorkerStatus = RelayWorkerStatus.STOPPED
        self._author_color_cache: dict[tuple[int, int], tuple[str | None, float]] = {}
        self._channel_resolution_miss_at: dict[hikari.Snowflake, float] = {}
        self._relay_owner_bot_ids_cache: tuple[float, frozenset[int]] | None = None
        self._discord_relay_message_order: deque[_DiscordRelayMessageRecord] = deque()
        self._discord_relay_reference_by_message: dict[tuple[int, int], ChatMessageReference] = {}
        self._discord_relay_message_id_by_event_channel: dict[tuple[str, int], int] = {}

    def set_event_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._relay_loop = loop or asyncio.get_running_loop()

    async def setup(self):
        self.set_event_loop()
        self._queue_worker_should_run = True
        self._start_queue_worker()

    async def close(self) -> None:
        self._queue_worker_should_run = False

        restart_task = getattr(self, "_queue_worker_restart_task", None)
        if restart_task is not None and not restart_task.done():
            restart_task.cancel()
            try:
                await restart_task
            except asyncio.CancelledError:
                pass
        self._queue_worker_restart_task = None

        read_task = getattr(self, "_read_task", None)
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
        self._read_task = None
        self._queue_worker_status = RelayWorkerStatus.STOPPED

    def set_voice_tts_service(self, voice_tts: RelayTTSService | None) -> None:
        self._voice_tts = voice_tts

    def _start_queue_worker(self) -> None:
        existing_task = getattr(self, "_read_task", None)
        if existing_task is not None and not existing_task.done():
            self._queue_worker_status = RelayWorkerStatus.RUNNING
            return

        loop = self._relay_loop
        if loop is None:
            loop = asyncio.get_running_loop()
            self._relay_loop = loop
        if loop.is_closed():
            raise RuntimeError("Discord relay worker event loop is closed.")

        task = loop.create_task(self._queue_task(), name="discord-relay-queue")
        self._read_task = task
        self._queue_worker_status = RelayWorkerStatus.RUNNING
        task.add_done_callback(self._handle_queue_worker_done)

    async def _restart_queue_worker_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._QUEUE_WORKER_RESTART_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            self._queue_worker_restart_task = None

        if not self._queue_worker_should_run:
            self._queue_worker_status = RelayWorkerStatus.STOPPED
            return

        try:
            self._start_queue_worker()
        except Exception:
            self._queue_worker_status = RelayWorkerStatus.FAILED
            log.exception("Discord relay worker restart failed.")

    def _schedule_queue_worker_restart(self) -> None:
        if not self._queue_worker_should_run:
            self._queue_worker_status = RelayWorkerStatus.STOPPED
            return

        restart_task = getattr(self, "_queue_worker_restart_task", None)
        if restart_task is not None and not restart_task.done():
            return

        loop = self._relay_loop
        if loop is None or loop.is_closed():
            self._queue_worker_status = RelayWorkerStatus.FAILED
            log.error("Discord relay worker restart skipped because the event loop is unavailable.")
            return

        self._queue_worker_status = RelayWorkerStatus.RESTARTING
        self._queue_worker_restart_task = loop.create_task(
            self._restart_queue_worker_after_delay(),
            name="discord-relay-queue-restart",
        )

    def _handle_queue_worker_done(self, task: asyncio.Task[None]) -> None:
        if task is not getattr(self, "_read_task", None):
            return

        self._read_task = None

        if task.cancelled():
            if self._queue_worker_should_run:
                log.warning("Discord relay worker was cancelled unexpectedly; scheduling restart.")
                self._schedule_queue_worker_restart()
            else:
                self._queue_worker_status = RelayWorkerStatus.STOPPED
            return

        exception = task.exception()
        if exception is None:
            if self._queue_worker_should_run:
                log.error("Discord relay worker exited unexpectedly without an exception; scheduling restart.")
                self._schedule_queue_worker_restart()
            else:
                self._queue_worker_status = RelayWorkerStatus.STOPPED
            return

        self._queue_worker_status = RelayWorkerStatus.FAILED
        log.exception(
            "Discord relay worker stopped unexpectedly.",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        self._schedule_queue_worker_restart()

    @classmethod
    def add(cls, x: DC_Bound, /):
        cls.queue.append(x)

    @classmethod
    def register_app_channel(cls, channel_id: hikari.Snowflakeish, app: "App"):
        apps = cls._chat_channels.setdefault(channel_id, set())
        if app in apps:
            return
        apps.add(app)
        log.info(f"DC.Register App: {app.name} @ {channel_id=}")

    @classmethod
    def unregister_app(cls, app: "App") -> None:
        empty_channels: list[hikari.Snowflakeish] = []
        for channel_id, apps in cls._chat_channels.items():
            apps.discard(app)
            if not apps:
                empty_channels.append(channel_id)
        for channel_id in empty_channels:
            cls._chat_channels.pop(channel_id, None)
        cls._chat_apps.pop(app.name, None)
        ChatHub().clear_room(app.name)

    @classmethod
    def bind_app_channel(cls, app: "App") -> None:
        cls.unregister_app(app)
        if not app.supports_chat_relay:
            return

        channels = cls._app_chat_channels(app)
        cls._chat_apps[app.name] = app
        hub = ChatHub()
        if app.supports_inbound_chat_relay:
            hub.bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), app.friendly))
        for channel_id in channels:
            hub.bind(app.name, ChatEndpoint(ChatEndpointId.discord_channel(channel_id), f"Discord {int(channel_id)}"))
            if app.supports_inbound_chat_relay:
                cls.register_app_channel(channel_id, app)

    @classmethod
    def _registered_app_chat_channels(cls, app: "App") -> tuple[hikari.Snowflake, ...]:
        return tuple(hikari.Snowflake(channel_id) for channel_id, apps in cls._chat_channels.items() if app in apps)

    @classmethod
    def _app_chat_channels(cls, app: "App") -> tuple[hikari.Snowflake, ...]:
        channels = cast(tuple[hikari.Snowflakeish, ...], getattr(app, "chat_channels", ()))
        if channels:
            return tuple(hikari.Snowflake(channel_id) for channel_id in channels)
        channel = cast(hikari.Snowflakeish | None, getattr(app, "chat_channel", None))
        if channel is not None:
            return (hikari.Snowflake(channel),)
        return cls._registered_app_chat_channels(app)

    async def _discord_text_route(
        self,
        channel_id: hikari.Snowflakeish,
        *,
        known_guild_id: hikari.Snowflakeish | None = None,
    ) -> DiscordTextRoute | None:
        channel_snowflake = hikari.Snowflake(channel_id)
        if known_guild_id is not None:
            return DiscordTextRoute(channel_id=channel_snowflake, guild_id=hikari.Snowflake(known_guild_id))

        channel = self._channel_objects.get(channel_snowflake) or await self.resolve_channel(channel_snowflake)
        if channel is None:
            return None
        raw_guild_id = cast(hikari.Snowflakeish | None, getattr(channel, "guild_id", None))
        return DiscordTextRoute(
            channel_id=channel_snowflake,
            guild_id=hikari.Snowflake(raw_guild_id) if raw_guild_id is not None else None,
        )

    async def _active_discord_text_routes(
        self,
        channel_ids: Sequence[hikari.Snowflakeish],
        *,
        source_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[DiscordTextRoute, ...]:
        source_guild = int(hikari.Snowflake(source_guild_id)) if source_guild_id is not None else None
        selected: list[DiscordTextRoute] = []
        seen_channels: set[int] = set()
        seen_guilds: set[int] = set()

        for channel_id in channel_ids:
            route = await self._discord_text_route(channel_id)
            if route is None:
                continue
            channel_key = int(route.channel_id)
            if channel_key in seen_channels:
                continue
            if route.guild_id is not None:
                guild_key = int(route.guild_id)
                if guild_key == source_guild:
                    continue
                if guild_key in seen_guilds:
                    continue
                seen_guilds.add(guild_key)
            selected.append(route)
            seen_channels.add(channel_key)
            if len(selected) >= self._MAX_ACTIVE_CHAT_TEXT_CHANNELS:
                break

        return tuple(selected)

    async def _active_app_chat_routes(self, app: "App") -> tuple[DiscordTextRoute, ...]:
        return await self._active_discord_text_routes(self._app_chat_channels(app))

    def _discord_guild_name(self, guild_id: hikari.Snowflakeish | None) -> str | None:
        if guild_id is None:
            return None
        manager = getattr(self, "manager", None)
        bot = getattr(manager, "bot", None) if manager is not None else None
        cache = getattr(bot, "cache", None) if bot is not None else None
        get_guild = getattr(cache, "get_guild", None) if cache is not None else None
        guild = get_guild(int(hikari.Snowflake(guild_id))) if callable(get_guild) else None
        guild_name = getattr(guild, "name", None)
        if isinstance(guild_name, str) and guild_name.strip():
            return guild_name
        return None

    async def _is_active_app_chat_channel(self, app: "App", channel_id: hikari.Snowflakeish) -> bool:
        channel_snowflake = hikari.Snowflake(channel_id)
        return any(route.channel_id == channel_snowflake for route in await self._active_app_chat_routes(app))

    @staticmethod
    def _app_uses_default_relay_channels(app: "App") -> bool:
        source: object = getattr(app, "chat_channel_source", None)
        source_value: object = getattr(source, "value", source)
        return source_value == "default"

    @staticmethod
    def _app_relay_name(app: "App") -> str:
        name = app.name
        if name:
            return name
        return repr(app)

    @staticmethod
    def _app_can_receive_chat(app: "App") -> bool:
        is_running = getattr(app, "_running", False)
        if not isinstance(is_running, bool) or not is_running:
            return False
        return getattr(app, "am_receiver", None) is not None

    @classmethod
    def _default_relay_pickup_app(cls, apps: Collection["App"]) -> "App | None":
        default_apps = tuple(app for app in apps if cls._app_uses_default_relay_channels(app))
        if not default_apps:
            return None
        return min(default_apps, key=cls._app_relay_name)

    @staticmethod
    def _message_author_is_bot(ctx: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent) -> bool:
        message = getattr(ctx, "message", None)
        for author in (getattr(ctx, "author", None), getattr(message, "author", None)):
            is_bot = getattr(author, "is_bot", False)
            if isinstance(is_bot, bool) and is_bot:
                return True
        return False

    def _current_bot_user_id(self) -> int | None:
        get_me = cast(Callable[[], OwnUser | None] | None, getattr(self.bot, "get_me", None))
        if get_me is None:
            return None
        me = get_me()
        if me is None:
            return None
        return int(hikari.Snowflake(me.id))

    @staticmethod
    def _bot_ids_from_snapshots(snapshots: Mapping[str, object]) -> set[int]:
        bot_ids: set[int] = set()
        for raw_snapshot in snapshots.values():
            try:
                snapshot = config.BotMetadataSnapshot.model_validate(raw_snapshot)
                if snapshot.features.mod_web is None:
                    continue
                bot_ids.add(int(hikari.Snowflake(snapshot.profile.id)))
            except Exception:
                continue
        return bot_ids

    @staticmethod
    def _bot_labels_from_snapshots(snapshots: Mapping[str, object]) -> dict[int, str]:
        labels: dict[int, str] = {}
        for raw_snapshot in snapshots.values():
            try:
                snapshot = config.BotMetadataSnapshot.model_validate(raw_snapshot)
                if snapshot.features.mod_web is None:
                    continue
                bot_id = int(hikari.Snowflake(snapshot.profile.id))
                labels[bot_id] = snapshot.features.mod_web.node_name or snapshot.profile.label or str(bot_id)
            except Exception:
                continue
        return labels

    def _known_relay_bot_ids(self) -> frozenset[int]:
        now = time.monotonic()
        cached = cast(tuple[float, frozenset[int]] | None, getattr(self, "_relay_owner_bot_ids_cache", None))
        if cached is not None:
            cached_at, cached_bot_ids = cached
            if now - cached_at < self._RELAY_OWNER_BOT_CACHE_SECONDS:
                return cached_bot_ids

        bot_ids: set[int] = set()
        current_bot_id = self._current_bot_user_id()
        if current_bot_id is not None:
            bot_ids.add(current_bot_id)

        try:
            for snapshot in config.load_bot_configuration(Path("configuration.json")).known_bots.values():
                if snapshot.features.mod_web is not None:
                    bot_ids.add(int(hikari.Snowflake(snapshot.profile.id)))
        except Exception as xcp:
            log.debug("Failed to load local relay bot registry for channel ownership: %s", xcp)

        cache_path = config.authority_cache_path(AuthorityResource.BOTS)
        if cache_path.exists():
            try:
                bot_ids.update(self._bot_ids_from_snapshots(read_json_object(cache_path)))
            except Exception as xcp:
                log.debug("Failed to load cached relay bot registry for channel ownership: %s", xcp)

        resolved = frozenset(bot_ids)
        self._relay_owner_bot_ids_cache = (now, resolved)
        return resolved

    def _known_relay_bot_labels(self) -> dict[int, str]:
        labels: dict[int, str] = {}
        current_bot_id = self._current_bot_user_id()
        if current_bot_id is not None:
            labels[current_bot_id] = config.MOD_WEB_SERVER.node_name

        try:
            labels.update(
                self._bot_labels_from_snapshots(config.load_bot_configuration(Path("configuration.json")).known_bots)
            )
        except Exception as xcp:
            log.debug("Failed to load local relay bot labels for channel ownership: %s", xcp)

        cache_path = config.authority_cache_path(AuthorityResource.BOTS)
        if cache_path.exists():
            try:
                labels.update(self._bot_labels_from_snapshots(read_json_object(cache_path)))
            except Exception as xcp:
                log.debug("Failed to load cached relay bot labels for channel ownership: %s", xcp)
        return labels

    @staticmethod
    def _relay_channel_owner_bot_id(
        channel_id: hikari.Snowflakeish,
        bot_ids: Collection[int],
    ) -> int | None:
        if not bot_ids:
            return None
        channel = str(int(hikari.Snowflake(channel_id)))
        return max(
            bot_ids,
            key=lambda bot_id: hashlib.sha256(f"{channel}:{bot_id}".encode("ascii")).digest(),
        )

    def _owns_shared_relay_channel(self, channel_id: hikari.Snowflakeish) -> bool:
        current_bot_id = self._current_bot_user_id()
        if current_bot_id is None:
            return True
        bot_ids = self._known_relay_bot_ids()
        if len(bot_ids) <= 1:
            return True
        return self._relay_channel_owner_bot_id(channel_id, bot_ids) == current_bot_id

    def log_chat_relay_summary(self) -> None:
        bot_ids = self._known_relay_bot_ids()
        bot_labels = self._known_relay_bot_labels()
        current_bot_id = self._current_bot_user_id()
        for channel_id, apps in sorted(self._chat_channels.items(), key=lambda item: int(hikari.Snowflake(item[0]))):
            channel_snowflake = hikari.Snowflake(channel_id)
            relay_apps = tuple(sorted(apps, key=self._app_relay_name))
            default_pickup_app = self._default_relay_pickup_app(relay_apps)
            owner_id = self._relay_channel_owner_bot_id(channel_snowflake, bot_ids)
            owner_label = bot_labels.get(owner_id, str(owner_id)) if owner_id is not None else "unassigned"
            owned_by_this_bot = owner_id is None or current_bot_id is None or owner_id == current_bot_id
            log.info(
                "Relay channel summary: channel=%s owner=%s owned_by_this_bot=%s default_pickup=%s apps=%s",
                int(channel_snowflake),
                owner_label,
                owned_by_this_bot,
                getattr(default_pickup_app, "name", None),
                ",".join(self._app_relay_name(app) for app in relay_apps),
            )

    async def _discord_tts_targets_for_app(self, app: "App") -> tuple[ChatEndpoint, ...]:
        if getattr(self, "_voice_tts", None) is None:
            return ()

        targets: list[ChatEndpoint] = []
        seen_guilds: set[int] = set()
        for route in await self._active_app_chat_routes(app):
            if route.guild_id is None:
                continue
            guild_key = int(route.guild_id)
            if guild_key in seen_guilds:
                continue
            seen_guilds.add(guild_key)
            endpoint_id = ChatEndpointId.discord_tts(route.guild_id, route.channel_id)
            targets.append(ChatEndpoint(endpoint_id, f"Discord TTS {guild_key}"))
        return tuple(targets)

    @staticmethod
    def _discord_tts_route_for_endpoint(endpoint_id: ChatEndpointId) -> DiscordTTSRoute:
        guild_text, separator, channel_text = endpoint_id.value.partition(":")
        if not separator or not guild_text.strip() or not channel_text.strip():
            raise ValueError(f"Invalid Discord TTS endpoint: {endpoint_id.value!r}")
        return DiscordTTSRoute(guild_id=hikari.Snowflake(guild_text), channel_id=hikari.Snowflake(channel_text))

    @staticmethod
    def _role_color_hex(role: hikari.Role | None) -> str | None:
        return color_int_to_hex(_role_color_value(role))

    def _preferred_chat_author_color(self, discord_user_id: int) -> str | None:
        del discord_user_id
        return None

    @staticmethod
    def _is_local_chat_user(discord_user_id: int) -> bool:
        return Access_Control.dev_bypass_level(discord_user_id) is not None

    async def _chat_author_color(
        self,
        *,
        discord_user_id: int | None,
        guild_id: hikari.Snowflakeish | None,
        member: hikari.Member | None = None,
    ) -> str | None:
        if discord_user_id is None:
            return None
        preferred_color = self._preferred_chat_author_color(discord_user_id)
        if preferred_color is not None:
            return preferred_color
        if guild_id is None:
            return None
        if self._is_local_chat_user(discord_user_id):
            return None

        guild_snowflake = hikari.Snowflake(guild_id)
        cache_key = (discord_user_id, int(guild_snowflake))
        now = time.monotonic()
        author_color_cache = self._chat_author_color_cache_map()
        cached = author_color_cache.get(cache_key)
        if cached is not None and now - cached[1] < self._CHAT_AUTHOR_COLOR_CACHE_SECONDS:
            return cached[0]

        resolved_member = member
        if resolved_member is None:
            try:
                user = await self.reso.user(discord_user_id, guild_snowflake, silent=True)
            except Exception as xcp:
                log.debug(
                    "Chat author role color lookup failed: user=%s guild=%s error=%s",
                    discord_user_id,
                    int(guild_snowflake),
                    xcp,
                )
                user = None
            resolved_member = user if isinstance(user, hikari.Member) else None

        color = color_int_to_hex(member_role_color(resolved_member)) if resolved_member is not None else None
        author_color_cache[cache_key] = (color, now)
        return color

    async def _chat_author_color_for_room(
        self,
        *,
        room_id: str,
        discord_user_id: int | None,
    ) -> tuple[str | None, int | None]:
        if discord_user_id is None:
            return None, None
        app = self._chat_apps.get(room_id)
        if app is None:
            return None, None
        fallback_guild_id: int | None = None
        for route in await self._active_app_chat_routes(app):
            guild_id = route.guild_id
            if guild_id is None:
                continue
            guild_id_int = int(guild_id)
            if fallback_guild_id is None:
                fallback_guild_id = guild_id_int
            color = await self._chat_author_color(discord_user_id=discord_user_id, guild_id=guild_id)
            if color is not None:
                return color, guild_id_int
        return None, fallback_guild_id

    async def resolve_channel(self, channel_id: hikari.Snowflakeish) -> hikari.TextableChannel | None:
        channel_snowflake = hikari.Snowflake(channel_id)
        miss_cache = getattr(self, "_channel_resolution_miss_at", {})
        missed_at = miss_cache.get(channel_snowflake)
        now = time.time()
        if missed_at is not None:
            if now - missed_at < self._CHANNEL_RESOLUTION_MISS_CACHE_SECONDS:
                return None
            miss_cache.pop(channel_snowflake, None)

        chan = self._channel_objects.get(channel_snowflake)
        cache = bool(chan)
        log.debug(f"{cache=} | {self._channel_objects=}")
        if not cache and not (chan := await self.reso.channel(channel_snowflake)):
            miss_cache[channel_snowflake] = now
            return None
        miss_cache.pop(channel_snowflake, None)
        if not cache and isinstance(chan, hikari.TextableChannel):
            self._channel_objects[channel_snowflake] = chan
        if not isinstance(chan, hikari.TextableChannel):
            return None
        return chan

    async def _queue_task(self):
        log.debug("Task Started")
        while True:
            if not self.queue:
                await asyncio.sleep(0.05)
                continue
            if not config.SILENT_DEBUG:
                log.debug(f"DC.Queue: {self.queue}")
            mess = self.queue.popleft()
            try:
                await self._send_dc(mess)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "App -> Discord relay worker dropped message: app=%s player=%r content=%r",
                    getattr(getattr(mess, "app", None), "name", None),
                    getattr(mess, "player", None),
                    getattr(mess, "content", None),
                )

    @classmethod
    def playerplate(cls, mess: DC_Bound) -> str:
        if not mess.player:
            return "UNDEFINED"
        if isinstance(mess.player, str) and mess.player.casefold() == "system":
            return "<<SYSTEM>>"
        if mess.player_id or isinstance(mess.player, int):
            return f"<@{mess.player_id or mess.player}>"
        return f"<{mess.player}>"

    @classmethod
    def _resolution_miss_key(cls, message: DC_Bound) -> tuple[str, str] | None:
        if not isinstance(message.player, str):
            return None
        return (message.app.name, message.player.casefold())

    async def _notify_resolution_failure(self, message: DC_Bound) -> None:
        miss_key = self._resolution_miss_key(message)
        if miss_key is None:
            return
        player_name = message.player
        if not isinstance(player_name, str):
            raise TypeError("Resolution failure notices require a string player name.")
        if message.player_resolution.status is NameResolutionStatus.UNIQUE:
            self._resolution_miss_counts.pop(miss_key, None)
            return
        consecutive_misses = self._resolution_miss_counts.get(miss_key, 0) + 1
        self._resolution_miss_counts[miss_key] = consecutive_misses
        log.warning(
            "Relay name resolution miss app=%s scope=%s player=%r status=%s consecutive=%d candidates=%s",
            message.app.name,
            message.app.scope,
            message.player,
            message.player_resolution.status,
            consecutive_misses,
            list(message.player_resolution.candidate_ids),
        )

    @staticmethod
    def embedify(mess: DC_Bound) -> list[hikari.Embed]:
        embs: list[hikari.Embed] = []
        if mess.relay_embed is not None:
            embs.append(
                hikari.Embed(
                    title=mess.relay_embed.title,
                    description=mess.relay_embed.description,
                    color=mess.relay_embed.color,
                )
            )
        return embs

    @staticmethod
    def _relay_tts_text(message: DC_Bound) -> str:
        notice = message.notice
        if notice is not None:
            player_name = str(message.player) if isinstance(message.player, (str, int, hikari.Snowflake)) else "someone"
            return render_notice_text(notice, author_name=player_name, app_name=message.app.friendly)
        return message.content_demojised

    @staticmethod
    def _synthetic_message_id_for_event(event: ChatEvent) -> hikari.Snowflake:
        digest = hashlib.blake2b(event.id.encode("utf-8"), digest_size=8).digest()
        return hikari.Snowflake(int.from_bytes(digest, "big"))

    def _relay_tts_text_for_event(self, event: ChatEvent, app: "App | None") -> str:
        notice = event.resolved_notice()
        if notice is not None:
            app_friendly = app.friendly if app is not None else event.room_id
            return render_notice_text(notice, author_name=event.author.display_name, app_name=app_friendly)
        return emoji.demojize(Message.demojise_discord(event.content))

    async def _send_chat_event_to_discord_tts(
        self,
        event: ChatEvent,
        route: DiscordTTSRoute,
        *,
        source_message: DC_Bound | None = None,
    ) -> None:
        voice_tts = cast(RelayTTSService | DiscordRelayTTSService | None, getattr(self, "_voice_tts", None))
        if voice_tts is None:
            return

        app = self._chat_apps.get(event.room_id) or (source_message.app if source_message is not None else None)
        app_name = app.name if app is not None else event.room_id
        player_name = str(source_message.player) if source_message is not None else event.author.display_name
        player_id = source_message.player_id if source_message is not None else event.author.discord_user_id
        relay_text = (
            self._relay_tts_text(source_message)
            if source_message is not None
            else self._relay_tts_text_for_event(event, app)
        ).strip()
        if not relay_text:
            tts_log.info(
                "Relay TTS skipped app=%s guild=%s channel=%s player=%r user_id=%s reason=empty_relay_text",
                app_name,
                int(route.guild_id),
                int(route.channel_id),
                player_name,
                player_id,
            )
            return

        message_id = self._synthetic_message_id_for_event(event)
        try:
            if isinstance(voice_tts, DiscordRelayTTSService):
                spoken, queue_size = await voice_tts.queue_discord_relay_message(
                    route.guild_id,
                    route.channel_id,
                    message_id,
                    relay_text,
                    user_id=player_id,
                    source_app=app_name,
                    player_name=player_name,
                )
            else:
                spoken, queue_size = await voice_tts.queue_relay_message(
                    route.guild_id,
                    route.channel_id,
                    message_id,
                    relay_text,
                    user_id=player_id,
                )
        except (RuntimeError, ValueError) as xcp:
            tts_log.info(
                "Relay TTS skipped app=%s guild=%s channel=%s player=%r user_id=%s reason=%s",
                app_name,
                int(route.guild_id),
                int(route.channel_id),
                player_name,
                player_id,
                xcp,
            )
            return

        tts_log.info(
            "Relay TTS queued app=%s guild=%s channel=%s player=%r user_id=%s queue_size=%s spoken=%r",
            app_name,
            int(route.guild_id),
            int(route.channel_id),
            player_name,
            player_id,
            queue_size,
            spoken,
        )

    @classmethod
    def _chat_hub(cls, relay: "DC_Relay") -> ChatHub:
        return getattr(relay, "chat_hub", ChatHub())

    @staticmethod
    def _merge_chat_targets(*groups: Sequence[ChatEndpoint]) -> tuple[ChatEndpoint, ...]:
        merged: list[ChatEndpoint] = []
        seen: set[str] = set()
        for group in groups:
            for endpoint in group:
                key = endpoint.id.stable_key
                if key in seen:
                    continue
                seen.add(key)
                merged.append(endpoint)
        return tuple(merged)

    @classmethod
    def _event_player_name(cls, player: str | int | hikari.UndefinedType) -> str:
        if isinstance(player, (int, hikari.Snowflake)):
            return str(player)
        if isinstance(player, str) and player:
            return player
        return "UNDEFINED"

    @classmethod
    def _event_from_dc_bound(cls, message: DC_Bound, *, author_color_hex: str | None = None) -> ChatEvent:
        player_name = cls._event_player_name(message.player)
        author_kind = ChatAuthorKind.SYSTEM if player_name.casefold() == "system" else ChatAuthorKind.GAME_PLAYER
        return ChatEvent(
            room_id=message.app.name,
            source=ChatEndpointId.app(message.app.name),
            author=ChatAuthor(
                kind=author_kind,
                id=str(message.player_id) if message.player_id is not None else player_name,
                display_name=player_name,
                discord_user_id=message.player_id,
                color_hex=author_color_hex,
                avatar_uri=message.player_avatar_uri,
            ),
            content=message.content,
            attachments=tuple(_chat_attachment(file) for file in OutboundRelayFormatter._sorted_files(message.files)),
            links=tuple(_chat_link(link) for link in OutboundRelayFormatter._sorted_urls(message.urls)),
            notice=message.notice,
            embed=_chat_embed(message.relay_embed),
        )

    def _event_from_app_bound(
        self, message: App_Bound, app: "App", *, author_color_hex: str | None = None
    ) -> ChatEvent:
        author_id = (
            int(hikari.Snowflake(message.player)) if isinstance(message.player, (int, hikari.Snowflake)) else None
        )
        app_scope = getattr(app, "scope", None)
        scoped_app = app_scope if isinstance(app_scope, str) else None
        app_platforms = getattr(app, "name_platforms", ())
        preferred_platform = getattr(app, "preferred_name_platform", None)
        author_display = (
            self.names.relay_display_name(
                author_id,
                str(author_id),
                scope=scoped_app,
                platforms=app_platforms,
                preferred_platform=preferred_platform,
                preferred_guild_id=message.source_guild_id,
            )
            if author_id is not None
            else str(message.player or "UNDEFINED")
        )
        author_avatar_uri = self._minecraft_chat_author_avatar_uri(
            room_id=app.name,
            discord_user_id=author_id,
            app=app,
        )
        source_channel_id = int(message.chan.id) if getattr(message.chan, "id", None) is not None else None
        source_label = message.chan.name if getattr(message.chan, "name", None) else None
        source_guild_name = self._discord_guild_name(message.source_guild_id)
        return ChatEvent(
            room_id=app.name,
            source=ChatEndpointId.discord_channel(source_channel_id or 0),
            author=ChatAuthor(
                kind=ChatAuthorKind.DISCORD_USER if author_id is not None else ChatAuthorKind.SYSTEM,
                id=str(author_id) if author_id is not None else str(message.player),
                display_name=author_display,
                discord_user_id=author_id,
                color_hex=author_color_hex,
                avatar_uri=author_avatar_uri,
            ),
            content=message.content,
            attachments=tuple(_chat_attachment(file) for file in OutboundRelayFormatter._sorted_files(message.files)),
            links=tuple(_chat_link(link) for link in OutboundRelayFormatter._sorted_urls(message.urls)),
            reference_kind=_chat_reference_kind(message.reference_kind),
            reference=message.reference,
            notice=message.notice,
            source_guild_id=int(message.source_guild_id) if message.source_guild_id is not None else None,
            source_guild_name=source_guild_name,
            source_channel_id=source_channel_id,
            source_message_id=int(message.source_message_id) if message.source_message_id is not None else None,
            source_label=source_label,
        )

    def _chat_reference_from_event(self, event: ChatEvent) -> ChatMessageReference:
        app = self._chat_apps.get(event.room_id)
        app_friendly = app.friendly if app is not None else event.room_id
        return event.to_reference(app_name=app_friendly)

    def _discord_relay_message_order_buffer(self) -> deque[_DiscordRelayMessageRecord]:
        raw_order = getattr(self, "_discord_relay_message_order", None)
        if isinstance(raw_order, deque):
            return cast(deque[_DiscordRelayMessageRecord], raw_order)
        record_order: deque[_DiscordRelayMessageRecord] = deque()
        self._discord_relay_message_order = record_order
        return record_order

    def _discord_relay_reference_by_message_map(self) -> dict[tuple[int, int], ChatMessageReference]:
        raw_map = getattr(self, "_discord_relay_reference_by_message", None)
        if isinstance(raw_map, dict):
            return cast(dict[tuple[int, int], ChatMessageReference], raw_map)
        reference_map: dict[tuple[int, int], ChatMessageReference] = {}
        self._discord_relay_reference_by_message = reference_map
        return reference_map

    def _is_tracked_discord_relay_message(
        self,
        *,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
    ) -> bool:
        reference_map = self._discord_relay_reference_by_message_map()
        return (
            int(hikari.Snowflake(channel_id)),
            int(hikari.Snowflake(message_id)),
        ) in reference_map

    def _discord_relay_message_id_by_event_channel_map(self) -> dict[tuple[str, int], int]:
        raw_map = getattr(self, "_discord_relay_message_id_by_event_channel", None)
        if isinstance(raw_map, dict):
            return cast(dict[tuple[str, int], int], raw_map)
        event_map: dict[tuple[str, int], int] = {}
        self._discord_relay_message_id_by_event_channel = event_map
        return event_map

    def _chat_author_color_cache_map(self) -> dict[tuple[int, int], tuple[str | None, float]]:
        raw_cache = getattr(self, "_author_color_cache", None)
        if isinstance(raw_cache, dict):
            return cast(dict[tuple[int, int], tuple[str | None, float]], raw_cache)
        author_color_cache: dict[tuple[int, int], tuple[str | None, float]] = {}
        self._author_color_cache = author_color_cache
        return author_color_cache

    def _discord_relay_reference_for_message(
        self,
        *,
        channel_id: hikari.Snowflakeish | None,
        message_id: hikari.Snowflakeish | None,
    ) -> ChatMessageReference | None:
        if channel_id is None or message_id is None:
            return None
        reference_map = self._discord_relay_reference_by_message_map()
        return reference_map.get((int(hikari.Snowflake(channel_id)), int(hikari.Snowflake(message_id))))

    def _record_discord_relay_message(
        self,
        *,
        channel_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        event: ChatEvent,
    ) -> None:
        record_order = self._discord_relay_message_order_buffer()
        reference_map = self._discord_relay_reference_by_message_map()
        event_map = self._discord_relay_message_id_by_event_channel_map()

        channel_id_int = int(hikari.Snowflake(channel_id))
        message_id_int = int(hikari.Snowflake(message_id))
        record = _DiscordRelayMessageRecord(
            channel_id=channel_id_int,
            message_id=message_id_int,
            source_event_id=event.id,
            reference=self._chat_reference_from_event(event),
        )
        reference_map[(channel_id_int, message_id_int)] = record.reference
        event_map[(event.id, channel_id_int)] = message_id_int
        record_order.append(record)
        while len(record_order) > self._MAX_TRACKED_DISCORD_CHAT_MESSAGES:
            stale = record_order.popleft()
            message_key = (stale.channel_id, stale.message_id)
            if reference_map.get(message_key) == stale.reference:
                reference_map.pop(message_key, None)
            event_key = (stale.source_event_id, stale.channel_id)
            if event_map.get(event_key) == stale.message_id:
                event_map.pop(event_key, None)

    def _discord_message_id_for_event_in_channel(
        self,
        *,
        event_id: str,
        channel_id: hikari.Snowflakeish,
    ) -> int | None:
        event_map = self._discord_relay_message_id_by_event_channel_map()
        return event_map.get((event_id, int(hikari.Snowflake(channel_id))))

    def _discord_reply_message_id_for_event(
        self,
        event: ChatEvent,
        *,
        channel_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        reference = event.reference
        if event.reference_kind is not ChatReferenceKind.REPLY or reference is None or reference.event_id is None:
            return None
        referenced_event = self._chat_hub(self).event(event.room_id, reference.event_id)
        channel_id_int = int(hikari.Snowflake(channel_id))
        if (
            referenced_event is not None
            and referenced_event.source.kind is ChatEndpointKind.DISCORD_CHANNEL
            and referenced_event.source_channel_id == channel_id_int
            and referenced_event.source_message_id is not None
        ):
            return hikari.Snowflake(referenced_event.source_message_id)
        tracked_message_id = self._discord_message_id_for_event_in_channel(
            event_id=reference.event_id,
            channel_id=channel_id,
        )
        if tracked_message_id is None:
            return None
        return hikari.Snowflake(tracked_message_id)

    def _discord_message_author_display_name(
        self,
        message: hikari.Message,
        *,
        guild_id: hikari.Snowflakeish | None,
    ) -> str:
        author = getattr(message, "author", None)
        member = getattr(message, "member", None)
        author_id = getattr(author, "id", None)
        fallback = self.names.discord_identity_label(
            getattr(author, "global_name", None),
            getattr(author, "username", None),
        ) or getattr(author, "display_name", None) or getattr(member, "display_name", None)
        if isinstance(author_id, int | str | hikari.Snowflake):
            del guild_id
            return self.names.discord_display_name(
                int(author_id),
                str(fallback or author_id),
                fallback_display_name=str(fallback or author_id),
            )
        if isinstance(fallback, str) and fallback.strip():
            return fallback
        return "Unknown"

    def _chat_reference_content_for_discord_message(self, message: hikari.Message) -> str:
        raw_content = str(getattr(message, "content", "") or "").strip()
        if raw_content:
            parsed_content, _ = self.names.parse_mentions(raw_content)
            content = Message.demojise_discord(parsed_content).strip()
            if content:
                return content
        attachments = getattr(message, "attachments", ())
        if attachments:
            return "Sent media"
        return "Sent a message"

    @staticmethod
    def _collection_size(value: object | None) -> int:
        if value is None or value is hikari.UNDEFINED:
            return 0
        try:
            return len(cast(Sized, value))
        except TypeError:
            try:
                return len(tuple(cast(Iterable[object], value)))
            except TypeError:
                return 0

    @classmethod
    def _relay_message_extra_text(cls, *, attachment_count: int, sticker_count: int = 0) -> str:
        extras: list[str] = []
        if attachment_count > 0:
            extras.append("attachment" if attachment_count == 1 else f"{attachment_count} attachments")
        if sticker_count > 0:
            extras.append("sticker" if sticker_count == 1 else f"{sticker_count} stickers")
        return ", ".join(extras)

    @classmethod
    def _compose_relay_message_body(
        cls,
        raw_content: str,
        *,
        attachment_count: int,
        sticker_count: int = 0,
        include_extras_with_content: bool = False,
    ) -> str:
        content = Message.demojise_discord(raw_content).strip()
        extras = cls._relay_message_extra_text(attachment_count=attachment_count, sticker_count=sticker_count)
        if content:
            if extras and include_extras_with_content:
                return f"{content}, {extras}"
            return content
        return extras

    @classmethod
    def _forwarded_snapshot_content(cls, message: hikari.Message) -> str:
        snapshots = getattr(message, "message_snapshots", hikari.UNDEFINED)
        if snapshots is None or snapshots is hikari.UNDEFINED:
            return ""
        try:
            snapshot_items = tuple(cast(Iterable[object], snapshots))
        except TypeError:
            return ""

        rendered_snapshots: list[str] = []
        for snapshot in snapshot_items:
            rendered = cls._compose_relay_message_body(
                str(getattr(snapshot, "content", "") or ""),
                attachment_count=cls._collection_size(getattr(snapshot, "attachments", None)),
                sticker_count=cls._collection_size(getattr(snapshot, "stickers", None)),
                include_extras_with_content=True,
            )
            if rendered:
                rendered_snapshots.append(rendered)
        return " ... ".join(rendered_snapshots)

    def _chat_reference_from_discord_message(
        self,
        message: hikari.Message,
        *,
        guild_id: hikari.Snowflakeish | None,
    ) -> ChatMessageReference | None:
        referenced_message = getattr(message, "referenced_message", None)
        if referenced_message is None or referenced_message is hikari.UNDEFINED:
            return None
        resolved_referenced_message = cast(hikari.Message, referenced_message)
        tracked_reference = self._discord_relay_reference_for_message(
            channel_id=message.channel_id,
            message_id=resolved_referenced_message.id,
        )
        if tracked_reference is not None:
            return tracked_reference
        referenced_author = getattr(resolved_referenced_message, "author", None)
        referenced_author_id = getattr(referenced_author, "id", None)
        discord_user_id = (
            int(hikari.Snowflake(referenced_author_id))
            if isinstance(referenced_author_id, int | str | hikari.Snowflake)
            else None
        )
        author_display_name = self._discord_message_author_display_name(resolved_referenced_message, guild_id=guild_id)
        content = self._chat_reference_content_for_discord_message(resolved_referenced_message)
        event_id = str(resolved_referenced_message.id)
        return ChatMessageReference(
            author_display_name=author_display_name,
            content=content,
            event_id=event_id,
            discord_user_id=discord_user_id,
        )

    async def publish_web_chat(
        self,
        *,
        room_id: str,
        session_id: str,
        author_display_name: str,
        author_id: str | None,
        discord_user_id: int | None,
        content: str,
        reply_to_event_id: str | None = None,
    ) -> ChatEvent:
        relay_loop = cast(asyncio.AbstractEventLoop | None, getattr(self, "_relay_loop", None))
        current_loop = asyncio.get_running_loop()
        if relay_loop is not None and relay_loop is not current_loop:
            if relay_loop.is_closed():
                raise RuntimeError("Discord relay event loop is closed.")
            future = asyncio.run_coroutine_threadsafe(
                self._publish_web_chat_on_loop(
                    room_id=room_id,
                    session_id=session_id,
                    author_display_name=author_display_name,
                    author_id=author_id,
                    discord_user_id=discord_user_id,
                    content=content,
                    reply_to_event_id=reply_to_event_id,
                ),
                relay_loop,
            )
            return await asyncio.wrap_future(future)
        return await self._publish_web_chat_on_loop(
            room_id=room_id,
            session_id=session_id,
            author_display_name=author_display_name,
            author_id=author_id,
            discord_user_id=discord_user_id,
            content=content,
            reply_to_event_id=reply_to_event_id,
        )

    async def publish_chat_event(self, *, event: ChatEvent) -> ChatEvent:
        relay_loop = cast(asyncio.AbstractEventLoop | None, getattr(self, "_relay_loop", None))
        current_loop = asyncio.get_running_loop()
        if relay_loop is not None and relay_loop is not current_loop:
            if relay_loop.is_closed():
                raise RuntimeError("Discord relay event loop is closed.")
            future = asyncio.run_coroutine_threadsafe(
                self._publish_chat_event_on_loop(event=event),
                relay_loop,
            )
            return await asyncio.wrap_future(future)
        return await self._publish_chat_event_on_loop(event=event)

    async def _publish_chat_event_on_loop(self, *, event: ChatEvent) -> ChatEvent:
        await self._deliver_chat_event(event, fallback_app=self._chat_apps.get(event.room_id))
        return event

    async def _publish_web_chat_on_loop(
        self,
        *,
        room_id: str,
        session_id: str,
        author_display_name: str,
        author_id: str | None,
        discord_user_id: int | None,
        content: str,
        reply_to_event_id: str | None = None,
    ) -> ChatEvent:
        room = room_id.strip()
        if not room:
            raise ValueError("Web chat room id must not be empty.")
        source_session_id = session_id.strip()
        if not source_session_id:
            raise ValueError("Web chat session id must not be empty.")
        message = content.strip()
        if not message:
            raise ValueError("Web chat content must not be empty.")
        display_name = author_display_name.strip() or "Web User"
        parsed_message = Message(message, display_name, None)
        await parsed_message.ensure_enriched()
        author_color_hex, source_guild_id = await self._chat_author_color_for_room(
            room_id=room,
            discord_user_id=discord_user_id,
        )
        author_avatar_uri = self._web_chat_author_avatar_uri(room_id=room, discord_user_id=discord_user_id)
        reference_kind = ChatReferenceKind.NONE
        reference: ChatMessageReference | None = None
        if reply_to_event_id is not None:
            target_event = self._chat_hub(self).event(room, reply_to_event_id)
            if target_event is None:
                raise ValueError("Web chat reply target is unavailable.")
            reference_kind = ChatReferenceKind.REPLY
            reference = self._chat_reference_from_event(target_event)
        event = ChatEvent(
            room_id=room,
            source=ChatEndpointId.web_session(source_session_id),
            author=ChatAuthor(
                kind=ChatAuthorKind.WEB_USER,
                id=author_id,
                display_name=display_name,
                discord_user_id=discord_user_id,
                color_hex=author_color_hex,
                avatar_uri=author_avatar_uri,
            ),
            content=message,
            links=tuple(_chat_link(link) for link in OutboundRelayFormatter._sorted_urls(parsed_message.urls)),
            reference_kind=reference_kind,
            reference=reference,
            source_guild_id=source_guild_id,
            source_guild_name=self._discord_guild_name(source_guild_id),
            source_label="Web dashboard",
        )
        await self._deliver_chat_event(event)
        return event

    def _web_chat_author_avatar_uri(
        self,
        *,
        room_id: str,
        discord_user_id: int | None,
    ) -> str | None:
        return self._minecraft_chat_author_avatar_uri(room_id=room_id, discord_user_id=discord_user_id)

    def _minecraft_chat_author_avatar_uri(
        self,
        *,
        room_id: str,
        discord_user_id: int | None,
        app: object | None = None,
    ) -> str | None:
        if discord_user_id is None:
            return None
        target_app = app
        if target_app is None:
            chat_apps = cast(Mapping[str, object], getattr(self, "_chat_apps", {}))
            target_app = chat_apps.get(room_id)
        scope = getattr(target_app, "scope", None)
        if scope != "minecraft":
            return None
        name_cache = getattr(target_app, "name_cache", None)
        get_game_uuid = getattr(name_cache, "get_game_uuid", None)
        if callable(get_game_uuid):
            uuid = cast(Callable[[int, str], str | None], get_game_uuid)(discord_user_id, scope)
            if isinstance(uuid, str) and uuid.strip():
                return minecraft_avatar_uri(uuid)
        get_game_alias = getattr(name_cache, "get_game_alias", None)
        if callable(get_game_alias):
            alias = cast(Callable[[int, str], str | None], get_game_alias)(discord_user_id, scope)
            if isinstance(alias, str) and alias.strip():
                return minecraft_avatar_uri(alias)
        return minecraft_dev_bypass_head_data_uri(discord_user_id)

    def _discord_author_fallback_name(self, event: ChatEvent, app: "App | None") -> str:
        if event.author.discord_user_id is not None:
            return self.names.discord_fallback_name(
                event.author.discord_user_id,
                "user",
                fallback_display_name=event.author.display_name,
            )
        return event.author.display_name

    def _app_author_display_name(self, event: ChatEvent, app: "App") -> str:
        discord_user_id = event.author.discord_user_id
        if discord_user_id is None:
            return event.author.display_name
        app_scope = getattr(app, "scope", None)
        scope = app_scope if isinstance(app_scope, str) else None
        app_platforms = getattr(app, "name_platforms", ())
        preferred_platform = getattr(app, "preferred_name_platform", None)
        return self.names.relay_display_name(
            discord_user_id,
            event.author.display_name,
            scope=scope,
            platforms=app_platforms,
            preferred_platform=preferred_platform,
            preferred_guild_id=event.source_guild_id,
        )

    async def _chat_author_is_member_of_guild(
        self,
        discord_user_id: int,
        guild_id: hikari.Snowflakeish | None,
    ) -> bool:
        if guild_id is None:
            return False
        if self._is_local_chat_user(discord_user_id):
            return False
        guild_snowflake = hikari.Snowflake(guild_id)
        try:
            user = await self.reso.user(discord_user_id, guild_snowflake, silent=True)
        except Exception as xcp:
            log.debug(
                "Chat author guild membership lookup failed: user=%s guild=%s error=%s",
                discord_user_id,
                int(guild_snowflake),
                xcp,
            )
            return False
        if not isinstance(user, hikari.Member):
            return False
        self.names.set_names(user)
        return True

    async def _playerplate_for_event(
        self,
        event: ChatEvent,
        *,
        guild_id: hikari.Snowflakeish | None,
        app: "App | None",
    ) -> str:
        if event.author.kind is ChatAuthorKind.SYSTEM:
            return "<<SYSTEM>>"
        discord_user_id = event.author.discord_user_id
        if discord_user_id is not None and await self._chat_author_is_member_of_guild(discord_user_id, guild_id):
            return f"<<@{discord_user_id}>>"
        return f"<{self._discord_author_fallback_name(event, app)}>"

    async def _discord_text_for_event(
        self,
        event: ChatEvent,
        *,
        guild_id: hikari.Snowflakeish | None,
        include_reference_prefix: bool = True,
        app: "App | None" = None,
    ) -> tuple[str, set[int]]:
        app = app or self._chat_apps.get(event.room_id)
        app_friendly = getattr(app, "friendly", event.room_id) if app is not None else event.room_id
        player_plate = await self._playerplate_for_event(event, guild_id=guild_id, app=app)
        notice = event.resolved_notice()
        relay_embed = self._relay_embed_payload_for_event(event, app=app)
        reference_prefix = (
            await self._discord_reference_prefix(event, guild_id=guild_id, app=app) if include_reference_prefix else None
        )
        if notice is not None:
            if relay_embed is not None:
                if notice_hides_body_content(notice):
                    return reference_prefix or "", set()
                return self._discord_text_for_embed_event(
                    event,
                    player_plate=player_plate,
                    reference_prefix=reference_prefix,
                ), set()
            notice_body = render_notice_body(notice, app_name=app_friendly)
            if reference_prefix is not None:
                notice_body = f"{reference_prefix} {notice_body}".strip() if notice_body else reference_prefix
            if event.author.kind is ChatAuthorKind.SYSTEM:
                return notice_body, set()
            return f"{player_plate} {notice_body}".strip(), set()

        app_scope = getattr(app, "scope", None) if app is not None else None
        scope = app_scope if isinstance(app_scope, str) else None
        app_platforms = getattr(app, "name_platforms", ()) if app is not None else ()
        preferred_platform = getattr(app, "preferred_name_platform", None) if app is not None else None
        parsed_content, mentions = self.names.parse_mentions(
            event.content,
            scope=scope,
            platforms=app_platforms,
            preferred_platform=preferred_platform,
        )
        body = parsed_content
        if reference_prefix is not None:
            body = f"{reference_prefix} {body}".strip() if body else reference_prefix
        text = f"{player_plate} {body}".strip()
        if relay_embed is not None:
            text = self._discord_text_for_embed_event(
                event,
                player_plate=player_plate,
                reference_prefix=reference_prefix,
            )
        return text, mentions

    async def _discord_reference_prefix(
        self,
        event: ChatEvent,
        *,
        guild_id: hikari.Snowflakeish | None,
        app: "App | None",
    ) -> str | None:
        if event.reference_kind is ChatReferenceKind.REPLY:
            if event.reference is not None:
                reference_author = await self._discord_reference_author_plate(event.reference, guild_id=guild_id)
                return f"reply to {reference_author};"
            return "reply;"
        if event.reference_kind is ChatReferenceKind.FORWARD:
            if event.reference is not None:
                reference_author = await self._discord_reference_author_plate(event.reference, guild_id=guild_id)
                return f"forwarded from {reference_author};"
            return "forwarded;"
        return None

    async def _discord_reference_author_plate(
        self,
        reference: ChatMessageReference,
        *,
        guild_id: hikari.Snowflakeish | None,
    ) -> str:
        discord_user_id = reference.discord_user_id
        if discord_user_id is None:
            match = DISCORD_USER_MENTION_REGEX.fullmatch(reference.author_display_name.strip())
            if match is not None:
                discord_user_id = int(match.group(1))
        if discord_user_id is not None and await self._chat_author_is_member_of_guild(discord_user_id, guild_id):
            return f"<<@{discord_user_id}>>"
        if discord_user_id is not None:
            resolved_name = self.names.discord_fallback_name(
                discord_user_id,
                "user",
                fallback_display_name=reference.author_display_name,
            )
            return f"<{resolved_name}>"
        return f"<{reference.author_display_name}>"

    @staticmethod
    def _discord_text_for_embed_event(
        event: ChatEvent,
        *,
        player_plate: str,
        reference_prefix: str | None,
    ) -> str:
        if event.author.kind is ChatAuthorKind.SYSTEM:
            return reference_prefix or ""
        if reference_prefix is None:
            return player_plate
        return f"{reference_prefix} {player_plate}".strip()

    @staticmethod
    def _is_discord_attachment_too_large_error(error: hikari.HTTPResponseError) -> bool:
        if isinstance(error, hikari.BadRequestError) and error.code == 40005:
            return True
        return int(error.status) == 413

    @staticmethod
    def _can_forward_source_discord_message(event: ChatEvent) -> bool:
        return (
            event.source.kind is ChatEndpointKind.DISCORD_CHANNEL
            and event.source_channel_id is not None
            and event.source_message_id is not None
        )

    async def _forward_chat_event_to_discord(
        self,
        event: ChatEvent,
        *,
        channel_id: hikari.Snowflakeish,
    ) -> hikari.Message:
        if not self._can_forward_source_discord_message(event):
            raise ValueError("Chat event cannot be forwarded without a source Discord message.")

        channel_snowflake = hikari.Snowflake(channel_id)
        rest = cast(Any, self.bot.rest)
        response_payload = await rest._request(
            routes.POST_CHANNEL_MESSAGES.compile(channel=channel_snowflake),
            json={
                "message_reference": {
                    "type": hikari_messages.MessageReferenceType.FORWARD.value,
                    "message_id": str(event.source_message_id),
                    "channel_id": str(event.source_channel_id),
                }
            },
        )
        if not isinstance(response_payload, Mapping):
            raise TypeError("Discord forward response payload is invalid.")
        return cast(hikari.Message, rest._entity_factory.deserialize_message(response_payload))

    @classmethod
    def _relay_embed_payload_for_event(
        cls,
        event: ChatEvent,
        *,
        app: "App | None",
    ) -> RelayEmbedPayload | None:
        embed_title = getattr(app, "friendly", event.room_id) if app is not None else event.room_id
        if event.embed is not None:
            explicit_title = event.embed.title.strip()
            return RelayEmbedPayload(
                title=explicit_title or embed_title,
                description=event.embed.description.strip(),
                color=event.embed.color,
            )
        notice = event.resolved_notice()
        if notice is not None and app is not None:
            embed_spec = notice_embed_spec(
                notice,
                app_name=embed_title,
                author_name=event.author.display_name,
            )
            if embed_spec is not None:
                return RelayEmbedPayload(
                    title=embed_spec.title,
                    description=embed_spec.description,
                    color=app.manage_embed_color,
                )
            return None
        return None

    @classmethod
    def _embedify_event(cls, event: ChatEvent, *, app: "App | None" = None) -> list[hikari.Embed]:
        embeds: list[hikari.Embed] = []
        payload = cls._relay_embed_payload_for_event(event, app=app)
        if payload is not None:
            embeds.append(
                hikari.Embed(title=payload.title, description=payload.description, color=payload.color)
            )
        return embeds

    async def _send_chat_event_to_discord(
        self,
        event: ChatEvent,
        channel_id: hikari.Snowflakeish,
        *,
        source_app: "App | None" = None,
    ) -> None:
        channel_snowflake = hikari.Snowflake(channel_id)
        channel = self._channel_objects.get(channel_snowflake)
        if channel is None:
            channel = await self.resolve_channel(channel_snowflake)
        if channel is None:
            log.warning(
                "Skipping unavailable Discord chat target: room=%s channel=%s",
                event.room_id,
                int(channel_snowflake),
            )
            return

        reply_message_id = self._discord_reply_message_id_for_event(event, channel_id=channel_snowflake)
        app = source_app or self._chat_apps.get(event.room_id)
        guild_id = cast(hikari.Snowflakeish | None, getattr(channel, "guild_id", None))
        if app is None:
            text, mentions = await self._discord_text_for_event(
                event,
                guild_id=guild_id,
                include_reference_prefix=reply_message_id is None,
            )
        else:
            text, mentions = await self._discord_text_for_event(
                event,
                guild_id=guild_id,
                include_reference_prefix=reply_message_id is None,
                app=app,
            )
        embeds = self._embedify_event(event, app=app)
        try:
            sent_message = await channel.send(
                content=text if text else hikari.UNDEFINED,
                embeds=embeds if embeds else hikari.UNDEFINED,
                reply=reply_message_id if reply_message_id is not None else hikari.UNDEFINED,
                reply_must_exist=False if reply_message_id is not None else hikari.UNDEFINED,
                mentions_reply=False if reply_message_id is not None else hikari.UNDEFINED,
                user_mentions=list(mentions),
                attachments=[hikari.File(attachment.uri, attachment.name) for attachment in event.attachments],
            )
            self._record_discord_relay_message(channel_id=channel_snowflake, message_id=sent_message.id, event=event)
            log.debug(
                "Chat event sent to Discord: room=%s channel=%s message=%s",
                event.room_id,
                int(channel.id),
                sent_message,
            )
        except hikari.HTTPResponseError as xcp:
            if self._is_discord_attachment_too_large_error(xcp) and self._can_forward_source_discord_message(event):
                try:
                    sent_message = await self._forward_chat_event_to_discord(event, channel_id=channel_snowflake)
                except Exception:
                    log.exception(
                        "DC.Send forward fallback failed: room=%s channel=%s event=%s",
                        event.room_id,
                        int(channel_snowflake),
                        event.id,
                    )
                else:
                    self._record_discord_relay_message(
                        channel_id=channel_snowflake,
                        message_id=sent_message.id,
                        event=event,
                    )
                    log.info(
                        "Chat event forwarded to Discord after attachment upload exceeded size limit: room=%s source_channel=%s source_message=%s channel=%s",
                        event.room_id,
                        event.source_channel_id,
                        event.source_message_id,
                        int(channel_snowflake),
                    )
                    return
            log.exception("DC.Send: -/> room=%s channel=%s event=%s", event.room_id, int(channel_snowflake), event.id)
        except Exception:
            log.exception("DC.Send: -/> room=%s channel=%s event=%s", event.room_id, int(channel_snowflake), event.id)

    async def _send_chat_event_to_app(self, event: ChatEvent, app: "App") -> None:
        app_name = getattr(app, "name", event.room_id)
        app_friendly = getattr(app, "friendly", event.room_id)
        if not app._running:
            log.info(
                "Chat -> App skipped: app=%s event=%s source=%s reason=not_running",
                app_name,
                event.id,
                event.source.stable_key,
            )
            return
        if app.am_receiver is None:
            log.warning(
                "Chat -> App skipped: app=%s event=%s source=%s reason=no_receiver",
                app_name,
                event.id,
                event.source.stable_key,
            )
            return

        channel_id = hikari.Snowflake(event.source_channel_id or 0)
        channel_name = event.source_label or "UNKNOWN"
        channel = hikari.TextableChannel(app=self.bot, id=channel_id, name=channel_name, type=1)
        author = self._app_author_display_name(event, app)
        notice = event.resolved_notice()
        rendered_content = event.render_content(player_name=author, app_name=app_friendly)
        payload = App_Bound(
            channel,
            rendered_content,
            author,
            files=[_fileish(attachment) for attachment in event.attachments],
            enrich=not event.links,
            reference_kind=_relay_reference_kind(event.reference_kind),
            relay_embed=_relay_embed(event.embed),
            notice=notice,
            source_guild_id=event.source_guild_id,
        )
        if event.links:
            payload.urls = {_urlish(link) for link in event.links}
        else:
            await payload.ensure_enriched()
        payload.app = app
        log.info(
            "Chat -> App: app=%s event=%s source=%s author=%r chars=%s attachments=%s links=%s",
            app_name,
            event.id,
            event.source.stable_key,
            payload.alias,
            len(event.content),
            len(event.attachments),
            len(event.links),
        )
        try:
            await app.am_receiver.send(payload)
        except Exception:
            log.exception(
                "Chat -> App failed: app=%s event=%s source=%s author=%r",
                app_name,
                event.id,
                event.source.stable_key,
                payload.alias,
            )
            return
        log.debug("Chat -> App sent: app=%s event=%s", app_name, event.id)

    async def _run_isolated_delivery(
        self,
        delivery: Awaitable[None],
        *,
        event: ChatEvent,
        target: ChatEndpointId,
    ) -> None:
        try:
            await delivery
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Chat bridge delivery failed: room=%s event=%s target=%s",
                event.room_id,
                event.id,
                target.stable_key,
            )

    async def _deliver_chat_event(
        self,
        event: ChatEvent,
        *,
        source_message: DC_Bound | None = None,
        fallback_app: "App | None" = None,
    ) -> None:
        targets = self._chat_hub(self).publish(event)
        app = self._chat_apps.get(event.room_id) or getattr(source_message, "app", None)
        if not targets and event.source.kind is ChatEndpointKind.APP:
            if app is not None:
                targets = tuple(
                    ChatEndpoint(ChatEndpointId.discord_channel(route.channel_id), f"Discord {int(route.channel_id)}")
                    for route in await self._active_app_chat_routes(app)
                )
        if event.source.kind is ChatEndpointKind.APP and app is not None:
            targets = self._merge_chat_targets(targets, await self._discord_tts_targets_for_app(app))
        if not targets and fallback_app is not None:
            await self._send_chat_event_to_app(event, fallback_app)
            return

        discord_text_channel_ids = tuple(
            hikari.Snowflake(target.id.value)
            for target in targets
            if target.id.kind is ChatEndpointKind.DISCORD_CHANNEL
        )
        source_guild_id = event.source_guild_id if event.source.kind is ChatEndpointKind.DISCORD_CHANNEL else None
        active_text_routes = await self._active_discord_text_routes(
            discord_text_channel_ids,
            source_guild_id=source_guild_id,
        )
        active_text_channel_ids = {route.channel_id for route in active_text_routes}
        deliveries: list[Awaitable[None]] = []
        for target in targets:
            if target.id.kind is ChatEndpointKind.DISCORD_CHANNEL:
                channel_id = hikari.Snowflake(target.id.value)
                if channel_id in active_text_channel_ids:
                    source_app = getattr(source_message, "app", None)
                    if source_app is None:
                        deliveries.append(
                            self._run_isolated_delivery(
                                self._send_chat_event_to_discord(event, channel_id),
                                event=event,
                                target=target.id,
                            )
                        )
                    else:
                        deliveries.append(
                            self._run_isolated_delivery(
                                self._send_chat_event_to_discord(
                                    event,
                                    channel_id,
                                    source_app=source_app,
                                ),
                                event=event,
                                target=target.id,
                            )
                        )
            elif target.id.kind is ChatEndpointKind.DISCORD_TTS:
                deliveries.append(
                    self._run_isolated_delivery(
                        self._send_chat_event_to_discord_tts(
                            event,
                            self._discord_tts_route_for_endpoint(target.id),
                            source_message=source_message,
                        ),
                        event=event,
                        target=target.id,
                    )
                )
            elif target.id.kind is ChatEndpointKind.APP:
                app = self._chat_apps.get(target.id.value)
                if app is not None:
                    deliveries.append(
                        self._run_isolated_delivery(
                            self._send_chat_event_to_app(event, app),
                            event=event,
                            target=target.id,
                        )
                    )
        if deliveries:
            await asyncio.gather(*deliveries)

    def _record_chat_event(self, event: ChatEvent) -> None:
        self._chat_hub(self).publish(event)

    def _attachment_download_failure_notice(self, failed_count: int) -> str:
        if failed_count <= 0:
            raise ValueError("failed_count must be positive.")
        if failed_count == 1:
            return "[1 attachment failed to download]"
        return f"[{failed_count} attachments failed to download]"

    def _apply_attachment_download_failure_notice(self, message: App_Bound, failed_count: int) -> None:
        if failed_count <= 0:
            return
        notice = self._attachment_download_failure_notice(failed_count)
        base_content = message._string.strip()
        message._string = f"{base_content} {notice}".strip() if base_content else notice

    @staticmethod
    async def _download_discord_message_attachments(
        attachments: Sequence[hikari.Attachment],
    ) -> DiscordAttachmentDownloadBatch:
        download_results = await asyncio.gather(
            *(File_Utils.download_temp(attachment) for attachment in attachments),
            return_exceptions=True,
        )

        downloaded_files: list[Fileish] = []
        failed_count = 0
        for attachment, result in zip(attachments, download_results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                failed_count += 1
                log.warning(
                    "Discord attachment download failed: filename=%s url=%s error=%s",
                    getattr(attachment, "filename", "<unknown>"),
                    getattr(attachment, "url", None),
                    result,
                )
                continue
            downloaded_files.append(
                Fileish(str(result), normalise_attachment_relay_name(attachment), source_url=attachment.url)
            )

        return DiscordAttachmentDownloadBatch(files=tuple(downloaded_files), failed_count=failed_count)

    async def _send_dc(self, message: DC_Bound | Message):
        if not isinstance(message, DC_Bound):
            raise ValueError(f"Invalid DC_Message: {message}")
        log.info(f"App -> DC: {message} | {message.content}")

        await message.ensure_enriched()
        await self._notify_resolution_failure(message)
        author_color_hex, _ = await self._chat_author_color_for_room(
            room_id=message.app.name,
            discord_user_id=message.player_id,
        )
        await self._deliver_chat_event(
            self._event_from_dc_bound(message, author_color_hex=author_color_hex),
            source_message=message,
        )

    _MAX_SEEN_MESSAGE_IDS: int = 5_000
    seen_messages_id: set[hikari.Snowflake] = set()
    seen_messages_order: deque[hikari.Snowflake] = deque()

    def _remember_message_id(self, message_id: hikari.Snowflake) -> bool:
        if message_id in self.seen_messages_id:
            return False
        self.seen_messages_id.add(message_id)
        self.seen_messages_order.append(message_id)
        while len(self.seen_messages_order) > self._MAX_SEEN_MESSAGE_IDS:
            stale_id = self.seen_messages_order.popleft()
            self.seen_messages_id.discard(stale_id)
        return True

    async def on_dcdm_message(self, ctx: hikari.MessageCreateEvent):
        await self.on_dc_message(ctx)

    async def on_gddm_message(self, ctx: hikari.GuildMessageCreateEvent):
        await self.on_dc_message(ctx)

    async def on_dc_message(self, ctx: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if not ctx.is_human:
            return

        if self._message_author_is_bot(ctx):
            return

        if self._is_tracked_discord_relay_message(channel_id=ctx.channel_id, message_id=ctx.message_id):
            log.debug(
                "Ignoring tracked relay echo: channel=%s message=%s",
                int(ctx.channel_id),
                int(ctx.message_id),
            )
            return

        if ctx.channel_id not in self._chat_channels:
            return

        if ctx.message.type is hikari.MessageType.CHANNEL_PINNED_MESSAGE:
            return

        if content := ctx.content:
            content = content.strip()
            if content.startswith(config.CHAT_IGNORE):
                return

        if not self._remember_message_id(ctx.message_id):
            log.warning(f"Dupe Message: {ctx.message_id}")
            return

        chan = None
        if isinstance(ctx, hikari.GuildMessageCreateEvent):
            chan = ctx.get_channel()
        if not chan:
            chan = await self.resolve_channel((ctx.channel_id))
        if chan is not None:
            self._channel_objects[hikari.Snowflake(ctx.channel_id)] = cast(hikari.TextableChannel, chan)

        source_member = ctx.member if isinstance(ctx, hikari.GuildMessageCreateEvent) else None
        if source_member is not None:
            self.names.set_names(source_member)
        elif isinstance(ctx.author, hikari.User):
            self.names.set_names(ctx.author)

        raw_member_mentions = ctx.message.get_member_mentions()
        member_mentions: Mapping[hikari.Snowflake, hikari.Member]
        if raw_member_mentions is hikari.UNDEFINED:
            member_mentions = {}
        else:
            member_mentions = raw_member_mentions
        for member in member_mentions.values():
            self.names.set_names(member)
        raw_user_mentions = ctx.message.user_mentions
        user_mentions: Mapping[hikari.Snowflake, hikari.User]
        if raw_user_mentions is hikari.UNDEFINED:
            user_mentions = {}
        else:
            user_mentions = raw_user_mentions
        for user_id, user in user_mentions.items():
            if user_id not in member_mentions:
                self.names.set_names(user)

        content = str(ctx.content or "").strip()
        if content.startswith(config.CHAT_IGNORE):
            return
        if not content and relay_reference_kind_for_message(ctx.message) is RelayMessageReferenceKind.FORWARD:
            content = self._forwarded_snapshot_content(ctx.message)

        source_guild_id = ctx.guild_id if isinstance(ctx, hikari.GuildMessageCreateEvent) else None
        source_member = ctx.member if isinstance(ctx, hikari.GuildMessageCreateEvent) else None
        author_color_hex = await self._chat_author_color(
            discord_user_id=int(ctx.author_id),
            guild_id=source_guild_id,
            member=source_member,
        )

        shushPylance = hikari.TextableChannel(app=self.bot, id=hikari.Snowflake(0), name="UNKNOWN", type=1)
        remote_files = [_discord_attachment_fileish(attachment) for attachment in ctx.message.attachments]
        downloaded_attachments: DiscordAttachmentDownloadBatch | None = None

        message = App_Bound(
            chan or shushPylance,
            content or "<NO.MSG>",
            ctx.author_id,
            files=remote_files,
            reference_kind=relay_reference_kind_for_message(ctx.message),
            reference=self._chat_reference_from_discord_message(ctx.message, guild_id=source_guild_id),
            source_guild_id=source_guild_id,
            source_message_id=ctx.message_id,
        )

        if message.content == "<NO.MSG>":
            if remote_files:
                message._string = ""
            if message.urls:
                message._string = "<URL>"

        await message.ensure_enriched()
        owns_shared_relay_channel = self._owns_shared_relay_channel(ctx.channel_id)
        relay_apps = tuple(sorted(self._chat_channels[ctx.channel_id], key=self._app_relay_name))
        default_pickup_app = self._default_relay_pickup_app(relay_apps)
        for app in relay_apps:
            app_uses_default_channels = self._app_uses_default_relay_channels(app)
            if not await self._is_active_app_chat_channel(app, ctx.channel_id):
                log.debug("Inactive chat channel ignored: app=%s channel=%s", app.name, int(ctx.channel_id))
                continue
            log.debug(f"{app} | {app._running} | {bool(app.am_receiver)}")
            message.app = app
            should_deliver = True
            if app_uses_default_channels and app is not default_pickup_app:
                can_receive_chat = self._app_can_receive_chat(app)
                should_deliver = False
                log.debug(
                    "Shared default relay channel mirrored to non-selected app room: app=%s selected=%s channel=%s deliver_to_app=%s",
                    app.name,
                    getattr(default_pickup_app, "name", None),
                    int(ctx.channel_id),
                    can_receive_chat,
                )
                if remote_files and can_receive_chat and downloaded_attachments is None:
                    downloaded_attachments = await self._download_discord_message_attachments(ctx.message.attachments)
                    self._apply_attachment_download_failure_notice(message, downloaded_attachments.failed_count)
                if can_receive_chat and downloaded_attachments is not None:
                    message.files = set(downloaded_attachments.files)
                else:
                    message.files = set(remote_files)
                event = self._event_from_app_bound(message, app, author_color_hex=author_color_hex)
                self._record_chat_event(event)
                if can_receive_chat:
                    await self._send_chat_event_to_app(event, app)
                continue
            if app_uses_default_channels and app is default_pickup_app and not owns_shared_relay_channel:
                should_deliver = False
                log.info(
                    "Shared default relay channel recorded with local app pickup only by non-owner: app=%s channel=%s",
                    app.name,
                    int(ctx.channel_id),
                )
                if remote_files and downloaded_attachments is None:
                    downloaded_attachments = await self._download_discord_message_attachments(ctx.message.attachments)
                    self._apply_attachment_download_failure_notice(message, downloaded_attachments.failed_count)
                if downloaded_attachments is not None:
                    message.files = set(downloaded_attachments.files)
                event = self._event_from_app_bound(message, app, author_color_hex=author_color_hex)
                self._record_chat_event(event)
                await self._send_chat_event_to_app(event, app)
                continue
            if not should_deliver:
                message.files = set(remote_files)
                event = self._event_from_app_bound(message, app, author_color_hex=author_color_hex)
                self._record_chat_event(event)
                continue
            if remote_files and downloaded_attachments is None:
                downloaded_attachments = await self._download_discord_message_attachments(ctx.message.attachments)
                self._apply_attachment_download_failure_notice(message, downloaded_attachments.failed_count)
            if downloaded_attachments is not None:
                message.files = set(downloaded_attachments.files)
            else:
                message.files = set(remote_files)
            event = self._event_from_app_bound(message, app, author_color_hex=author_color_hex)
            fallback_app = app if app._running and app.am_receiver is not None else None
            await self._deliver_chat_event(event, fallback_app=fallback_app)
            message.files = set(remote_files)
        for app_name, send_func in self._special_channels.get(ctx.channel_id, ()):
            log.debug(f"{app_name} | {send_func} | {bool(send_func)}")
            if remote_files and downloaded_attachments is None:
                downloaded_attachments = await self._download_discord_message_attachments(ctx.message.attachments)
                self._apply_attachment_download_failure_notice(message, downloaded_attachments.failed_count)
            if downloaded_attachments is not None:
                message.files = set(downloaded_attachments.files)
            await send_func(message)


# AiviA APasz
