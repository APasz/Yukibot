import logging
import re
from pathlib import Path
from typing import TypeAlias, cast, overload

import hikari

import config

log = logging.getLogger(__name__)
_SnowflakeCollection: TypeAlias = list[hikari.Snowflakeish] | dict[object, hikari.Snowflakeish]
_ChannelLookupInput: TypeAlias = hikari.Snowflakeish | _SnowflakeCollection
_MessageLookupInput: TypeAlias = hikari.Snowflakeish | _SnowflakeCollection
_ResolvedChannel: TypeAlias = hikari.PartialChannel | hikari.PermissibleGuildChannel | hikari.GuildThreadChannel


class Resolutator(metaclass=config.Singleton):
    bot: hikari.GatewayBot

    def __init__(self, bot: hikari.GatewayBot | None = None):
        if not bot:
            raise ValueError("bot must be passed")
        self.bot = bot

    @staticmethod
    def snow_check(snow_type: str, snow: object) -> bool:
        valid_types: tuple[type[object], ...] = (
            int,
            hikari.Snowflake,
            hikari.PartialUser,
            hikari.PartialChannel,
            hikari.PartialGuild,
            hikari.PartialMessage,
        )
        if not snow:
            log.warning("Invalid; Not Truthy: %s %s[%s]", snow_type, snow, type(snow))
            return False
        if isinstance(snow, dict):
            snow_map = cast(dict[object, object], snow)
            k = all(isinstance(e, valid_types) for e in snow_map.values())
            if not k:
                log.warning("Invalid; dict: %s %s[%s]", snow_type, snow_map, type(snow_map))
            return k
        if isinstance(snow, list):
            snow_list = cast(list[object], snow)
            k = all(isinstance(e, valid_types) for e in snow_list)
            if not k:
                log.warning("Invalid; list: %s %s[%s]", snow_type, snow_list, type(snow_list))
            return k
        if isinstance(snow, valid_types):
            if not config.SILENT_DEBUG:
                log.debug("Valid; Is Truthy: %s %s[%s]", snow_type, snow, type(snow))
            return True
        log.warning("Invalid; unknown: %s %s[%s]", snow_type, snow, type(snow))
        return False

    async def user(
        self, ident: hikari.Snowflakeish, guild_id: hikari.Snowflakeish | None = None, *, silent: bool = False
    ) -> hikari.Member | hikari.User | None:
        if guild_id:
            user = self.bot.cache.get_member(guild_id, ident)
        else:
            user = self.bot.cache.get_user(ident)
        if not user:
            try:
                log.debug("user.FETCH: %s", ident)
                if guild_id:
                    user = await self.bot.rest.fetch_member(guild_id, ident)
                else:
                    user = await self.bot.rest.fetch_user(ident)
            except hikari.NotFoundError as xcp:
                if guild_id:
                    log.warning("FETCH; %s: Retrying without guild_id", xcp)
                    return await self.user(ident)
                if silent:
                    return None
                else:
                    raise xcp
            except Exception as xcp:
                log.exception("FETCH; %s: %s @ %s", xcp, ident, guild_id)
        return user

    @overload
    async def channel(
        self, ident: hikari.Snowflakeish
    ) -> _ResolvedChannel | None: ...

    @overload
    async def channel(
        self, ident: list[hikari.Snowflakeish]
    ) -> list[_ResolvedChannel | None] | None: ...

    @overload
    async def channel(
        self, ident: dict[object, hikari.Snowflakeish]
    ) -> dict[object, _ResolvedChannel | None] | None: ...

    async def channel(
        self, ident: _ChannelLookupInput
    ) -> _ResolvedChannel | list[_ResolvedChannel | None] | dict[object, _ResolvedChannel | None] | None:
        """Attempts to resolve ident to a Channel object

        Args;
            ident: ID/s for the channel

        Returns;
            Channel object else None if error
        """
        if not self.snow_check("channel_id", ident):
            return None

        async def get(chan_id: hikari.Snowflakeish) -> _ResolvedChannel | None:
            chan = self.bot.cache.get_guild_channel(chan_id) or self.bot.cache.get_thread(chan_id)
            if not chan:
                try:
                    if not config.SILENT_DEBUG:
                        log.debug("channel.FETCH: %s", chan_id)
                    chan = await self.bot.rest.fetch_channel(chan_id)
                except hikari.ForbiddenError as xcp:
                    log.debug("channel.FETCH forbidden: %s error=%s", chan_id, xcp)
                except hikari.NotFoundError as xcp:
                    log.debug("channel.FETCH missing: %s error=%s", chan_id, xcp)
                except Exception as xcp:
                    log.exception("FETCH; %s: %s", xcp, chan_id)
            return chan

        if isinstance(ident, dict):
            return {key: await get(e) for key, e in ident.items()}  # type: ignore
        elif isinstance(ident, list):
            return [await get(e) for e in ident]  # type: ignore
        return await get(ident)

    @overload
    async def message(
        self, ident: hikari.Snowflakeish, chan_ident: hikari.Snowflakeish | None
    ) -> hikari.Message | None: ...

    @overload
    async def message(
        self, ident: list[hikari.Snowflakeish], chan_ident: hikari.Snowflakeish | None
    ) -> list[hikari.Message | None] | None: ...

    @overload
    async def message(
        self, ident: dict[object, hikari.Snowflakeish], chan_ident: hikari.Snowflakeish | None
    ) -> dict[object, hikari.Message | None] | None: ...

    async def message(
        self,
        ident: _MessageLookupInput,
        chan_ident: hikari.Snowflakeish | None = None,
    ) -> hikari.Message | list[hikari.Message | None] | dict[object, hikari.Message | None] | None:
        """Attempts to resolve ident to a Message object

        Args;
            ident: ID/s for the message
            chan_ident: ID for the channel the message resides in, required for fetching when message not in cache

        Returns;
            Message object else None if error
        """
        if not self.snow_check("message_id", ident):
            return None

        async def get(mess_id: hikari.Snowflakeish) -> hikari.Message | None:
            mess = self.bot.cache.get_message(mess_id)
            if not mess and chan_ident:
                try:
                    if not config.SILENT_DEBUG:
                        log.debug("message.FETCH: %s", mess_id)
                    mess = await self.bot.rest.fetch_message(chan_ident, mess_id)
                except Exception as xcp:
                    log.exception("FETCH; %s: %s", xcp, mess_id)
            return mess

        if isinstance(ident, dict):
            return {key: await get(e) for key, e in ident.items()}  # type: ignore
        elif isinstance(ident, list):
            return [await get(e) for e in ident]  # type: ignore
        return await get(ident)

    @staticmethod
    def path_tokens(raw: str, context: dict[str, Path | str] | None = None) -> str:
        """
        Resolves special tokens in a path string, including env vars and custom tokens.
        Supports:
            - {TOKEN}
            - {ENV:VAR_NAME}

        Args;
            raw: The raw string path with tokens
            context: Dictionary of replacement tokens like {"APP": Path(...), "TMP": "/tmp"}

        Returns;
            Resolved string path
        """
        raw = re.sub(r"\{ENV:([\w\d_]+)\}", lambda m: config.env_opt(m.group(1)) or "", raw)

        defaults = {
            "APPS": config.APP_PATH,
            "TMP": config.DIR_TMP,
            "OPT": config.DIR_OPT,
            "HOME": Path.home(),
            "CWD": config.DIR_CWD,
        }

        for key, val in (defaults | (context or {})).items():
            raw = raw.replace(f"{{{key}}}", str(val))

        return raw


# AiviA APasz
