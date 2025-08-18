import asyncio
import inspect
import logging
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta
import traceback

import hikari
import lightbulb
import uvloop

import _sys
import config
from _activity import Activity_Manager, Provider_CPU, Provider_DISK, Provider_RAM
from _discord import DC_Relay, Distils, Resolutator
from _file import File_Utils
from _manager import App_Manager, Provider_Player, Provider_Process
from _sys import Stats_System
from _utils import File_Cleaner, Utilities
from _security import Access_Control
from cmd_alias import group_alias
from cmd_app import group_app
from cmd_misc import group_misc
from cmd_mod import group_mod
from cmd_saves import group_saves  # noqa: F401
from cmd_settings import group_settings
from cmd_update import group_update
from config import Activity_Provider, Name_Cache

log = logging.getLogger("system")


asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


activities: list[type[Activity_Provider]] = [
    Provider_RAM,
    Provider_CPU,
    Provider_Player,
    Provider_Process,
    Provider_DISK,
]
start_time = datetime.now()


def main():
    log.info(f"Running {os.getpid()}")

    bot = hikari.GatewayBot(
        token=config.env_req("BOT_TOKEN"),
        intents=hikari.Intents.ALL_UNPRIVILEGED | hikari.Intents.ALL_MESSAGES | hikari.Intents.MESSAGE_CONTENT,
    )
    app_manager = App_Manager()
    name_cache = Name_Cache()

    if deg := config.env_opt("DISCORD_DEV_GUILD"):
        client: lightbulb.Client = lightbulb.client_from_app(bot, default_enabled_guilds=[hikari.Snowflake(deg)])
    else:
        client: lightbulb.Client = lightbulb.client_from_app(bot)

    utilities = Utilities()
    resolutator = Resolutator(bot)
    registry = client.di.registry_for(lightbulb.di.Contexts.DEFAULT)
    acl = Access_Control()
    registry.register_value(Access_Control, acl)
    registry.register_value(hikari.GatewayBot, bot)
    registry.register_value(lightbulb.Client, client)
    registry.register_value(App_Manager, app_manager)
    registry.register_value(Distils, Distils())
    registry.register_value(Resolutator, resolutator)
    dc_relay = DC_Relay(bot)
    registry.register_value(DC_Relay, dc_relay)
    registry.register_value(Utilities, utilities)
    registry.register_value(File_Utils, File_Utils())
    registry.register_value(Name_Cache, name_cache)
    registry.register_value(Stats_System, Stats_System())
    registry.register_value(File_Cleaner, File_Cleaner())

    client.register(group_app)
    client.register(group_alias)
    client.register(group_mod)
    client.register(group_misc)
    # client.register(group_saves)
    client.register(group_settings)
    client.register(group_update)

    @client.error_handler
    async def error_handler(epf: lightbulb.exceptions.ExecutionPipelineFailedException, ctx: lightbulb.Context) -> bool:
        log.warning(
            f"Command Error: {f'{ctx.command_data.parent.name}.' if ctx.command_data.parent else ''}{ctx.command_data.name} | {epf.causes}"
        )

        simple_errors = []

        def fmt(xcp: Exception) -> str | Exception:
            if isinstance(xcp, lightbulb.prefab.OnCooldown):
                simple_errors.append(xcp)
                rd = utilities.create_rdelta(xcp.remaining)
                return utilities.format_rdelta(rd)
            return xcp

        causes = [f"{type(c).__name__}: {fmt(c)}" for c in epf.causes]
        await ctx.respond(f"my sweets {'an error' if len(causes) == 1 else 'errors'} occurred\n{'\n'.join(causes)}")

        for xcp in epf.causes:
            if xcp not in simple_errors:
                log.exception(f"EH.XCP: {xcp}\n{traceback.format_exc()}")

        return True

    starting_xcp: list[str] = []

    @bot.listen(hikari.StartingEvent)
    async def on_starting(event: hikari.StartingEvent):
        log.info("Starting")
        try:
            await client.start()
            am = await di_inject_providers()
            await app_manager.post_init(bot, am)

            await dc_relay.setup()
            bot.subscribe(hikari.MessageCreateEvent, dc_relay.on_dcdm_message)  # type: ignore
            bot.subscribe(hikari.GuildMessageCreateEvent, dc_relay.on_gddm_message)  # type: ignore
        except Exception as xcp:
            starting_xcp.append(str(xcp))
            raise xcp

    async def di_inject_providers() -> Activity_Manager:
        async with client.di.enter_context(lightbulb.di.Contexts.DEFAULT) as ctx:
            acts = []
            for provider in activities:
                anno = None
                try:
                    sig = inspect.signature(provider.__init__)
                    kwargs = {}
                    for param in sig.parameters.values():
                        if param.name == "self":
                            continue
                        if param.default != param.empty:
                            continue
                        if param.annotation == param.empty:
                            raise TypeError(f"{provider.__name__}.__init__ missing type annotation for '{param.name}'")
                        anno = param.annotation
                        kwargs[param.name] = await ctx.get(anno)
                        acts.append(provider(**kwargs))
                except Exception as xcp:
                    log.exception(f"DI-Inject; {provider}.{anno}")
                    starting_xcp.append(str(xcp))

            am = Activity_Manager(bot, acts)
            ctx.add_value(Activity_Manager, am)
            return am

    @client.task(lightbulb.uniformtrigger(1, wait_first=False), max_failures=100)
    async def task_sys_stats(stats: Stats_System):
        stats.update()

    @client.task(lightbulb.uniformtrigger(3), max_failures=25)
    async def task_activity(actor: Activity_Manager | None):
        if not actor:
            return
        await actor.update()

    @client.task(lightbulb.uniformtrigger(hours=1, wait_first=False), max_failures=100)
    async def task_clear_uploads(cleaner: File_Cleaner, stats: Stats_System):
        for folder, td in cleaner.folders_to_clear.items():
            if not config.SILENT_DEBUG:
                log.debug(f"Clearing {folder}")
            if config.IS_DEBUG and stats.disk.percent > 90:
                log.info("Clearing immediately as disk > 90%")
                td = timedelta(seconds=1)
            cleaner.clear(set(folder.iterdir()), td)

    auto_app = None  # noqa: F841

    @bot.listen(hikari.StartedEvent)
    async def on_started(event: hikari.StartedEvent):
        log.info("Started")
        # await client.sync_application_commands()

        global auto_app
        auto_app = None

        try:
            for arg in sys.argv:
                if arg.startswith("app="):
                    auto_app = app_manager.get(arg.split("=", 1)[1])
                    break
        except Exception as xcp:
            starting_xcp.append(str(xcp))
            raise xcp

        if config.STARTED_CHANNEL:
            txt = ["Started: DEBUG" if config.IS_DEBUG else "Started"]
            if auto_app:
                txt.append(f"\tAuto-Launching: {auto_app.friendly}")
            txt.extend(starting_xcp)
            flags = hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
            try:
                await bot.rest.create_message(config.STARTED_CHANNEL, "\n".join(txt), flags=flags)
            except Exception:
                log.exception("STARTED MESSAGE")

        rmid_file = Path("restart_message_id")
        if rmid_file.exists():
            chan_id, mess_id = rmid_file.read_text().strip().split(":")
            rmid_file.unlink()
            mess = await resolutator.message(int(mess_id), int(chan_id))
            if mess:
                await mess.edit(f"{mess.content or ''} ...Done! :D")

    @bot.listen(hikari.StartedEvent)
    async def after_started(event: hikari.StartedEvent):
        global auto_app
        if auto_app:
            log.info(f"Auto-Launching: {auto_app.friendly}")
            await asyncio.sleep(7)
            try:
                await app_manager.launch(auto_app)
            except Exception as xcp:
                log.exception(f"AUTO_LAUNCH: {auto_app}")
                if config.STARTED_CHANNEL:
                    flags = hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
                    await bot.rest.create_message(config.STARTED_CHANNEL, f"Error: {xcp}", flags=flags)

    @bot.listen(hikari.StoppingEvent)
    async def on_stopping(event: hikari.StoppingEvent):
        log.info("Ending")
        print("Ending")
        await app_manager.end()
        if not config.STARTED_CHANNEL:
            return
        rd = utilities.create_rdelta(start_time, datetime.now())
        txt = f"Shutting Down; uptime: {utilities.format_rdelta(rd)}"
        flags = hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
        try:
            await bot.rest.create_message(config.STARTED_CHANNEL, txt, flags=flags)
        except Exception:
            log.exception("STOPPED MESSAGE")

    @bot.listen(hikari.GuildAvailableEvent)
    async def _on_guild(event: hikari.GuildAvailableEvent):
        guild = event.get_guild()
        if guild:
            if not config.IS_DEBUG:
                appli = await bot.rest.fetch_application()
                cmds = await event.app.rest.fetch_application_commands(appli, event.guild.id)
                if cmds:
                    log.info(
                        f"Clearing Existing app_cmds @ {event.guild.name} | {event.guild.id}\n{[c.name for c in cmds]}"
                    )
                    await event.app.rest.set_application_commands(appli, [], event.guild.id)
            chans = guild.get_channels()
            text_chans: dict[hikari.Snowflakeish, hikari.TextableChannel] = {
                k: v for k, v in chans.items() if isinstance(v, hikari.TextableChannel)
            }
            dc_relay._channel_objects.update(text_chans)
            # log.debug(f"{text_chans=}")

    @bot.listen(hikari.MessageCreateEvent)
    async def _add_names(event: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if hasattr(event, "memeber"):
            name_cache.set_names(event.member or event.author)  # type: ignore
        else:
            name_cache.set_names(event.author)

    @bot.listen(hikari.MessageCreateEvent)
    async def _failsafe_restart(event: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if not event.content:
            return
        if event.author.is_bot:
            return
        if "restart_system" not in event.content or "restart_bot" not in event.content:
            return
        if acl.perm_check(event.author_id, acl.LvL.sudo):
            await _sys.restart(
                event.message, bot, app_manager, "system" if "restart_system" in event.content else "bot"
            )

    bot.run()


if __name__ == "__main__":
    main()

# AiviA APasz
