from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
import logging
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
import re

import aiohttp
import hikari
import lightbulb

from TenorGrabber import tenorgrabber

from _resolator import Resolutator

import config
from _file import File_Utils
from _utils import Utilities
from config import Name_Cache, Singleton

from typing import Protocol, TYPE_CHECKING
import emoji

if TYPE_CHECKING:
    from apps._app import App


log = logging.getLogger(__name__)

DISCORD_EMOJI_REGEX = re.compile(r"<a?:(\w+):\d+>")


class AM_Receiver(Protocol):
    async def send(self, payload: App_Bound): ...


class Distils:
    file = File_Utils()
    util = Utilities()

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
    ):
        if not paths:
            raise ValueError("paths list must not be empty")

        base_name = f"{app_name}_{display_name}" if app_name else display_name
        zip_name = base_name + ".zip" if not base_name.endswith(".zip") else ""

        if force_zip:
            paths = [await cls.file.compress(paths, zip_name)]

        # Forced direct download override
        if force_download:
            log.info("Force download mode enabled")
            await cls.direct(ctx, paths, base_name)
            return

        # Try direct send first
        if len(paths) <= 10:
            try:
                total_size = sum(cls.file.pointer_size(p) for p in paths)
                if total_size < config.DISCORD_UPLOAD_LIMIT:
                    await ctx.respond(f"Here ya go, `{base_name}`", attachments=[hikari.File(str(p)) for p in paths])
                    return
            except Exception:
                log.warning("Failed size pre-check, continuing anyway")

        # Try compressed all-in-one

        zip_path = await cls.file.compress(paths, zip_name)
        try:
            if cls.file.pointer_size(zip_path) < config.DISCORD_UPLOAD_LIMIT:
                await ctx.respond(f"Your file sweets, `{base_name}`", attachment=hikari.File(str(zip_path)))
                return
        except hikari.HTTPResponseError as xcp:
            xcp.code
            log.warning(f"Zipped-all upload failed: {xcp}")
        except Exception:
            log.exception("Compression or zipped upload failed")

        # Final fallback: direct download
        await cls.direct(ctx, [zip_path], base_name)

    @classmethod
    async def direct(cls, ctx: lightbulb.Context, paths: Collection[Path], base_name: str):
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
        size = sum([File_Utils.pointer_size(s) for s in files])
        msg = f"`{base_name}` {Utilities.humanise_bytes(size)} expires {expire}\n" + "\n".join(links)
        await ctx.respond(msg)

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
    async def ac_focused_static(ctx: lightbulb.AutocompleteContext, to_send: Collection[str]):
        if not isinstance(ctx.focused.value, str):
            raise ValueError(f"String go with strings, not {type(ctx.focused.value)}")
        foc_val = ctx.focused.value.lower()
        await ctx.respond([hikari.impl.AutocompleteChoiceBuilder(e, e) for e in to_send if foc_val in e.lower()][:25])

    @staticmethod
    async def ac_focused_mutate(
        ctx: lightbulb.AutocompleteContext,
        to_send: dict[str, object],
        caller: Callable[[str, object], tuple[str, str | int | float]],
    ):
        if not isinstance(ctx.focused.value, str):
            raise ValueError(f"String go with strings, not {type(ctx.focused.value)}")
        foc_val = ctx.focused.value.lower()
        acb = hikari.impl.AutocompleteChoiceBuilder
        await ctx.respond([acb(*caller(k, v)) for k, v in to_send.items() if foc_val in k.lower()][:25])


class Generics(Enum):
    join = "{player} joined {app}"
    left = "{player} left {app}"
    died_pve = "{player} died to {cause}"
    died_pvp = "{player} killed by {cause}"


@dataclass(slots=True, frozen=True)
class Fileish:
    uri: str
    name: str


@dataclass(slots=True)
class URLish:
    url: str
    label: str | None = None
    type: str | None = None
    is_media: bool = False
    extension: str | None = None
    orig_url: str | None = None

    def __hash__(self) -> int:
        return hash(self.url)


class Message:
    generics = Generics
    app: "App"
    _string: str
    is_generic: bool
    player: str | int | hikari.UndefinedType
    urls: set["URLish"]
    files: set["Fileish"]
    enrich_task: asyncio.Task | None
    extra_fmt: dict[str, str]

    _md_link_re = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|www\.[^\s)]+)\)")
    _url_re = re.compile(r"\b(https?://[^\s<>()\[\]]+|www\.[^\s<>()\[\]]+)")
    _media_exts = {
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

    __slots__ = ("app", "_string", "is_generic", "player", "urls", "files", "enrich_task", "extra_fmt")

    def __init__(
        self,
        content: str | Generics,
        player: str | int | hikari.UndefinedType,
        files: Sequence[Fileish] | None,
        enrich: bool = True,
        extra_fmt: dict[str, str] | None = None,
    ) -> None:
        if isinstance(content, str):
            self._string = content
            self.is_generic = False
        elif isinstance(content, Generics):
            self._string = content.value
            self.is_generic = True
        else:
            raise ValueError(f"Content must be str, not {type(content)}")

        self.player = player
        if not isinstance(player, (str, int, hikari.UndefinedType, hikari.Snowflake)):
            raise ValueError(f"Player must be str | int | UNDEFINED, not {type(player)}")

        self.urls: set[URLish] = set()
        self.files: set[Fileish] = {f for f in files if isinstance(f, Fileish)} if files else set()

        if enrich:
            self.enrich_task = asyncio.create_task(self.find_urls())
        else:
            self.enrich_task = None

        self.extra_fmt = extra_fmt or {}

    @staticmethod
    def demojise_discord(text: str) -> str:
        return DISCORD_EMOJI_REGEX.sub(r":\1:", text)

    @property
    def content(self) -> str:
        return self.demojise_discord(self._string)

    @property
    def content_demojised(self) -> str:
        return emoji.demojize(self.content)

    async def find_urls(self):
        if self.is_generic:
            self.urls = set()
        else:
            self.urls = await self._enrich_links(self._match_urls(self._string))
        return self.urls

    def _match_urls(self, text: str) -> dict[str, str | None]:
        urls = {v: k for k, v in self._md_link_re.findall(text)}
        urls.update({k: None for k in self._url_re.findall(text) if k not in urls})

        return urls

    async def _resolve_url_metadata(self, session: aiohttp.ClientSession, url: str) -> tuple[str, str | None]:
        try:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return str(resp.url), resp.headers.get("Content-Type", "").lower()
        except Exception:
            return url, None

    async def _enrich_links(self, links: dict[str, str | None]) -> set[URLish]:
        enriched = set()

        async with aiohttp.ClientSession() as session:

            async def enrich_one(url: str, label: str | None):
                tenor = None
                if "tenor.com" in url:
                    tenor = tenorgrabber.getgiflink(url)

                final_url, content_type = await self._resolve_url_metadata(session, tenor or url)
                is_media = any(
                    final_url.lower().endswith(ext)
                    or (content_type and content_type.startswith(("image/", "video/", "audio/")))
                    for ext in self._media_exts
                )
                extension = None
                if is_media:
                    if ext := final_url.split(".")[-1]:
                        if (ext := ext.lower()) in self._media_exts:
                            extension = ext

                urlish = URLish(final_url, label, content_type, is_media, extension, orig_url=url)
                log.debug(f"{urlish=}")
                log.debug(f"{tenor=}")
                enriched.add(urlish)

            await asyncio.gather(*(enrich_one(url, label) for url, label in links.items()))

        return enriched


class DC_Bound(Message):
    __slots__ = ("app", "_string", "is_generic", "player", "player_id", "urls", "files", "enrich_task", "extra_fmt")
    player_id: int | None

    def __init__(
        self,
        app: "App",
        content: str | Generics,
        player: str | int | hikari.UndefinedType,
        files: Sequence[Fileish] | None = None,
        extra_fmt: dict[str, str] | None = None,
    ) -> None:
        super().__init__(content, player, files, extra_fmt=extra_fmt)
        self.app = app
        self.player_id = Name_Cache().resolve_to_id(str(player), app.scope)

        log.debug(f"Create DC_Message: {player} @ {self.app.name}")

    def __repr__(self) -> str:
        return f"<DC: {self.app.name} with {len(self.content)} chars / {len(self.urls)} URLs / {len(self.files)} files for {self.app.chat_channel}>"


class App_Bound(Message):
    __slots__ = ("app", "chan", "_string", "is_generic", "player", "urls", "files", "enrich_task", "extra_fmt")

    def __init__(
        self,
        chan: hikari.TextableChannel,
        content: str | Generics,
        player: str | int | hikari.UndefinedType,
        files: Sequence[Fileish] | None = None,
        extra_fmt: dict[str, str] | None = None,
    ) -> None:
        super().__init__(content, player, files, extra_fmt=extra_fmt)
        self.chan = chan
        log.debug(f"Create App_Message: {player} from {chan.name or chan.id}")

    @property
    def alias(self) -> str:
        if isinstance(self.player, int):
            if player := self.app.name_cache.get_game_alias(self.player, self.app.scope):
                return player
        elif self.player:
            return self.player
        return "UNDEFINED"

    def __repr__(self) -> str:
        return f"<APP: {self.player} with {len(self.content)} chars / {len(self.urls)} URLs / {len(self.files)} files>"


class DC_Relay(metaclass=Singleton):
    queue: deque[DC_Bound] = deque()
    _channel_objects: dict[hikari.Snowflakeish, hikari.TextableChannel] = {}
    _chat_channels: dict[hikari.Snowflakeish, set["App"]] = {}
    _special_channels: dict[hikari.Snowflakeish, set[tuple[str, Callable]]] = {}
    "channel: Apps"
    names = Name_Cache()

    def __init__(self, bot: hikari.GatewayBot) -> None:
        self.bot = bot
        self.reso = Resolutator()

    async def setup(self):
        self._read_task = asyncio.create_task(self._queue_task())

    @classmethod
    def add(cls, x: DC_Bound, /):
        cls.queue.append(x)

    @classmethod
    def register_app_channel(cls, channel_id: hikari.Snowflakeish, app: "App"):
        log.info(f"DC.Register App: {app.name} @ {channel_id=}")
        cls._chat_channels.setdefault(channel_id, set()).add(app)

    async def resolve_channel(self, channel_id: hikari.Snowflakeish) -> hikari.TextableChannel | None:
        chan = self._channel_objects.get(channel_id)
        cache = bool(chan)
        log.debug(f"{cache=} | {self._channel_objects=}")
        if not cache and not (chan := await self.reso.channel(channel_id)):
            cache = False
            return None
        if not cache and isinstance(chan, hikari.TextableChannel):
            self._channel_objects[channel_id] = chan
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
            await self._send_dc(mess)

    @classmethod
    def playerplate(cls, mess: DC_Bound) -> str:
        if not mess.player:
            return "UNDEFINED"
        if mess.player_id or isinstance(mess.player, int):
            return f"<<@{mess.player_id or mess.player}>>"
        player = cls.names.resolve_to_id(mess.player, mess.app.scope)
        if player:
            return f"<<@{player}>>"
        return f"<{mess.player}>"

    @staticmethod
    def embedify(mess: DC_Bound) -> list[hikari.Embed]:
        embs = []
        for link in mess.urls:
            if not link.is_media:
                continue
            emb = hikari.Embed(title=link.label)
            emb.set_image(link.url)
            embs.append(emb)
        for file in mess.files:
            emb = hikari.Embed(title=file.name)
            emb.set_image(file.uri)
            embs.append(emb)
        return embs

    async def _send_dc(self, message: DC_Bound | Message):
        if not isinstance(message, DC_Bound):
            raise ValueError(f"Invalid DC_Message: {message}")
        log.info(f"App -> DC: {message} | {message.content}")

        chan = None
        if chan_id := message.app.chat_channel:
            if not (chan := self._channel_objects.get(chan_id)):
                chan = await self.resolve_channel(message.app.chat_channel)
        if not chan:
            log.error(f"Can't find channel to send message to\n{message}")
            raise LookupError("Can't find channel to send message to")

        player_plate = self.playerplate(message)
        if message.is_generic:
            fmt_map = {"player": player_plate, "app": message.app.friendly} | message.extra_fmt
            text = message.content.format_map(fmt_map)
            mentions = None
        else:
            parsed_content, mentions = self.names.parse_mentions(message.content)
            text = f"{player_plate} {parsed_content}"
            if message.player_id:
                mentions.discard(message.player_id)

        if message.enrich_task:
            await message.enrich_task
        try:
            mess = await chan.send(
                text,
                user_mentions=list(mentions) if mentions else hikari.UNDEFINED,
                attachments=[hikari.File(f.uri, f.name) for f in message.files],
            )
            log.debug(f"DC.Send -> {mess.channel_id}")
        except Exception:
            log.exception(f"DC.Send: -/> {message}")

    seen_messages_id: set[hikari.Snowflake] = set()

    async def on_dcdm_message(self, ctx: hikari.MessageCreateEvent):
        await self.on_dc_message(ctx)

    async def on_gddm_message(self, ctx: hikari.GuildMessageCreateEvent):
        await self.on_dc_message(ctx)

    async def on_dc_message(self, ctx: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if not ctx.is_human:
            return

        if ctx.channel_id not in self._chat_channels:
            return

        if content := ctx.content:
            content = content.strip()
            if content.startswith(config.CHAT_IGNORE):
                return

        if ctx.message_id in self.seen_messages_id:
            log.warning(f"Dupe Message: {ctx.message_id}")
            return
        else:
            self.seen_messages_id.add(ctx.message_id)

        chan = None
        if isinstance(ctx, hikari.GuildMessageCreateEvent):
            chan = ctx.get_channel()
        if not chan:
            chan = await self.resolve_channel((ctx.channel_id))

        shushPylance = hikari.TextableChannel(app=self.bot, id=hikari.Snowflake(0), name="UNKNOWN", type=1)
        files: list[Fileish] = []
        for attach in ctx.message.attachments:
            pointer = await File_Utils.download_temp(attach)
            files.append(Fileish(str(pointer), attach.title or attach.filename))

        message = App_Bound(chan or shushPylance, content or "<NO.MSG>", ctx.author_id, files=files)

        if message.content == "<NO.MSG>":
            if files:
                message._string = ""
            if message.urls:
                message._string = "<URL>"

        if message.enrich_task:
            await message.enrich_task
        for app in self._chat_channels[ctx.channel_id]:
            log.debug(f"{app} | {app._running} | {bool(app.am_receiver)}")
            if app._running and app.am_receiver:
                message.app = app
                await app.am_receiver.send(message)
        for app_name, send_func in self._special_channels[ctx.channel_id]:
            log.debug(f"{app_name} | {send_func} | {bool(send_func)}")
            await send_func(message)


# AiviA APasz
