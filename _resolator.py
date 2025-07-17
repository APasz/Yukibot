import logging
from pathlib import Path
import re
from typing import Literal, overload

import hikari

import config

log = logging.getLogger(__name__)


class Resolutator(metaclass=config.Singleton):
    bot: hikari.GatewayBot

    def __init__(self, bot: hikari.GatewayBot | None = None):
        if not bot:
            raise ValueError("bot must be passed")
        self.bot = bot

    @overload
    async def user(
        self, ident: hikari.Snowflakeish, guild_id: hikari.Snowflakeish | None = None, *, silent: Literal[True]
    ) -> hikari.Member | hikari.User | hikari.NotFoundError | None: ...

    @overload
    async def user(
        self, ident: hikari.Snowflakeish, guild_id: hikari.Snowflakeish | None = None, *, silent: Literal[False] = ...
    ) -> hikari.Member | hikari.User | None: ...

    @staticmethod
    def snow_check(
        snow_type: str, snow: hikari.Snowflakeish | list[hikari.Snowflakeish] | dict[object, hikari.Snowflakeish]
    ) -> bool:
        valid_types = (
            hikari.Snowflake,
            hikari.Snowflakeish,
            hikari.PartialUser,
            hikari.PartialChannel,
            hikari.PartialGuild,
            hikari.PartialMessage,
        )
        if not snow:
            log.warning("Invalid; Not Truthy: %s %s[%s]", snow_type, snow, type(snow))
            return False
        if isinstance(snow, valid_types) and not config.SILENT_DEBUG:
            log.debug("Valid; Is Truthy: %s %s[%s]", snow_type, snow, type(snow))
            return True
        if isinstance(snow, dict):
            k = all([isinstance(e, valid_types) for e in snow.values()])
            if not k:
                log.warning("Invalid; dict: %s %s[%s]", snow_type, snow, type(snow))
            return k
        if isinstance(snow, list):
            k = all([isinstance(e, valid_types) for e in snow])
            if not k:
                log.warning("Invalid; list: %s %s[%s]", snow_type, snow, type(snow))
            return k
        log.warning("Invalid; unknown: %s %s[%s]", snow_type, snow, type(snow))
        return False

    @overload
    async def user(
        self, ident: hikari.Snowflakeish, guild_id: hikari.Snowflakeish | None = None, *, silent: bool = True
    ) -> hikari.UndefinedOr[hikari.Member | hikari.User]: ...

    @overload
    async def user(
        self, ident: hikari.Snowflakeish, guild_id: hikari.Snowflakeish | None = None, *, silent: bool = False
    ) -> hikari.Member | hikari.User | None: ...

    async def user(
        self, ident: hikari.Snowflakeish, guild_id: hikari.Snowflakeish | None = None, *, silent: bool = False
    ):
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
                    return hikari.UNDEFINED
                else:
                    raise xcp
            except Exception as xcp:
                log.exception("FETCH; %s: %s @ %s", xcp, ident, guild_id)
        return user

    @overload
    async def channel(
        self, ident: hikari.Snowflakeish
    ) -> (
        hikari.PartialChannel
        | hikari.PermissibleGuildChannel
        | hikari.GuildThreadChannel
        | hikari.DMChannel
        | hikari.GroupDMChannel
        | hikari.GuildTextChannel
        | hikari.GuildVoiceChannel
        | hikari.GuildNewsChannel
        | None
    ): ...

    @overload
    async def channel(
        self, ident: list[hikari.Snowflakeish]
    ) -> (
        list[
            hikari.PartialChannel
            | hikari.PermissibleGuildChannel
            | hikari.GuildThreadChannel
            | hikari.DMChannel
            | hikari.GroupDMChannel
            | hikari.GuildTextChannel
            | hikari.GuildVoiceChannel
            | hikari.GuildNewsChannel
        ]
        | None
    ): ...

    @overload
    async def channel(
        self, ident: dict[object, hikari.Snowflakeish]
    ) -> (
        dict[
            object,
            hikari.PartialChannel
            | hikari.PermissibleGuildChannel
            | hikari.GuildThreadChannel
            | hikari.DMChannel
            | hikari.GroupDMChannel
            | hikari.GuildTextChannel
            | hikari.GuildVoiceChannel
            | hikari.GuildNewsChannel,
        ]
        | None
    ): ...

    async def channel(self, ident):
        """Attempts to resolve ident to a Channel object

        Args;
            ident: ID/s for the channel

        Returns;
            Channel object else None if error
        """
        if not self.snow_check("channel_id", ident):
            return None

        async def get(chan_id):
            chan = self.bot.cache.get_guild_channel(chan_id) or self.bot.cache.get_thread(chan_id)
            if not chan:
                try:
                    if not config.SILENT_DEBUG:
                        log.debug("channel.FETCH: %s", chan_id)
                    chan = await self.bot.rest.fetch_channel(chan_id)
                except Exception as xcp:
                    log.exception("FETCH; %s: %s", xcp, chan_id)
            return chan

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
        if not isinstance(raw, str):
            return raw

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
