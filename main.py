import asyncio
import inspect
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import hikari
import hikariwave
import lightbulb

import _sys
import config
from _activity import Activity_Manager, Provider_CPU, Provider_DISK, Provider_RAM
from _authority_server import AuthorityServer
from _discord import DC_Relay, Distils, Resolutator
from _file import File_Utils
from _manager import App_Manager, Provider_Player, Provider_Process
from _security import Access_Control
from _sys import Stats_System
from _utils import File_Cleaner, Utilities
from cmd_alias import AliasEditorService, CMD_Alias
from cmd_app import AppManageService, group_app
from cmd_dashboard import CMD_Dashboard, DashboardEditorService
from cmd_misc import group_misc
from cmd_music import MusicService, group_music
from cmd_online import CMD_Online, OnlineEditorService
from cmd_ops import group_ops
from cmd_saves import group_saves  # noqa: F401
from cmd_voice import VoiceAdminEditorService, VoiceSettingsEditorService, VoiceTTSService, group_voice
from config import Activity_Provider, Name_Cache
from online import Online_Tracker

log = logging.getLogger("system")

if os.name != "nt":
    try:
        import uvloop
    except ImportError:
        log.info("uvloop not available; using default asyncio loop")
    else:
        uvloop.install()

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
    profile = config.ACTIVE_BOT_PROFILE
    log.info(
        "Bot profile: "
        f"{profile.name.value} | "
        f"commands={','.join(group.value for group in profile.command_groups)} | "
        f"services={','.join(sorted(service.value for service in profile.services))}"
    )
    log.info(
        "Public address: "
        f"addr={config.PUBLIC_ADDR} | "
        f"ip={config.PUBLIC_IP} | "
        f"base={config.PUBLIC_BASE_URL} | "
        f"uploads={config.PUBLIC_UPLOADS_BASE_URL}"
    )
    log.info(
        "Authority config: "
        f"mode={config.DATA_AUTHORITY_MODE.value} | "
        f"endpoint={config.DATA_AUTHORITY_ENDPOINT.base_url if config.DATA_AUTHORITY_ENDPOINT else 'None'} | "
        f"bind={f'{config.DATA_AUTHORITY_SERVER_BINDING.host}:{config.DATA_AUTHORITY_SERVER_BINDING.port}' if config.DATA_AUTHORITY_SERVER_BINDING else 'None'} | "
        f"token={'set' if config.DATA_AUTHORITY_TOKEN else 'missing'}"
    )

    bot = hikari.GatewayBot(
        token=config.env_req("BOT_TOKEN"),
        intents=hikari.Intents.ALL_UNPRIVILEGED | hikari.Intents.ALL_PRIVILEGED,
    )
    app_manager = App_Manager()
    name_cache = Name_Cache()
    alias_editor = AliasEditorService()
    app_editor = AppManageService()
    dashboard_editor = DashboardEditorService()
    online_editor = OnlineEditorService()
    voice_admin_editor = VoiceAdminEditorService()
    voice_editor = VoiceSettingsEditorService()
    online_tracker = Online_Tracker()
    stats = Stats_System()
    client: lightbulb.Client

    if deg := config.env_opt("INDEV"):
        log.info(f"DEG|DEV: {deg}")
        client = lightbulb.client_from_app(
            bot,
        )
    else:
        client = lightbulb.client_from_app(bot)

    utilities = Utilities()
    resolutator = Resolutator(bot)
    authority_server = AuthorityServer(name_cache)
    registry = client.di.registry_for(lightbulb.di.Contexts.DEFAULT)
    acl = Access_Control()
    registry.register_value(Access_Control, acl)
    registry.register_value(hikari.GatewayBot, bot)
    registry.register_value(lightbulb.Client, client)
    registry.register_value(App_Manager, app_manager)
    registry.register_value(Distils, Distils())
    registry.register_value(Resolutator, resolutator)
    dc_relay = DC_Relay(bot)
    voice_client: hikariwave.VoiceClient | None = None
    voice_tts: VoiceTTSService | None = None
    music: MusicService | None = None
    if profile.has_service(config.BotService.MUSIC) or profile.has_service(config.BotService.VOICE_TTS):
        voice_client = hikariwave.VoiceClient(bot)
    if profile.has_service(config.BotService.MUSIC):
        if not voice_client:
            raise RuntimeError("Music service requires a voice client")
        music = MusicService(bot, voice_client)
        registry.register_value(MusicService, music)
    if profile.has_service(config.BotService.VOICE_TTS):
        if not voice_client:
            raise RuntimeError("Voice TTS service requires a voice client")
        voice_tts = VoiceTTSService(bot, voice_client)
        registry.register_value(VoiceTTSService, voice_tts)
    if music and voice_tts:
        voice_tts.set_music_active_channel_provider(music.active_channel_id)
        voice_tts.set_music_duck_handler(music.duck_tts_playback)
    registry.register_value(DC_Relay, dc_relay)
    registry.register_value(Utilities, utilities)
    registry.register_value(File_Utils, File_Utils())
    registry.register_value(Name_Cache, name_cache)
    registry.register_value(AliasEditorService, alias_editor)
    registry.register_value(AppManageService, app_editor)
    registry.register_value(DashboardEditorService, dashboard_editor)
    registry.register_value(OnlineEditorService, online_editor)
    registry.register_value(VoiceAdminEditorService, voice_admin_editor)
    registry.register_value(VoiceSettingsEditorService, voice_editor)
    registry.register_value(Online_Tracker, online_tracker)
    registry.register_value(Stats_System, stats)
    registry.register_value(File_Cleaner, File_Cleaner())

    command_groups: dict[config.CommandGroup, lightbulb.Group | type[lightbulb.SlashCommand]] = {
        config.CommandGroup.APP: group_app,
        config.CommandGroup.ALIAS: CMD_Alias,
        config.CommandGroup.DASHBOARD: CMD_Dashboard,
        config.CommandGroup.MISC: group_misc,
        config.CommandGroup.OPS: group_ops,
        config.CommandGroup.ONLINE: CMD_Online,
        # config.CommandGroup.SAVES: group_saves,
        config.CommandGroup.MUSIC: group_music,
        config.CommandGroup.VOICE: group_voice,
    }
    for group_id in profile.command_groups:
        client.register(command_groups[group_id])

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

            if profile.has_service(config.BotService.GAME_RELAY):
                await dc_relay.setup()
                bot.subscribe(hikari.MessageCreateEvent, dc_relay.on_dcdm_message)  # type: ignore
                bot.subscribe(hikari.GuildMessageCreateEvent, dc_relay.on_gddm_message)  # type: ignore
            if music:
                await music.setup(client)
                bot.subscribe(hikari.VoiceStateUpdateEvent, music.on_voice_state_update)  # type: ignore
                bot.subscribe(hikariwave.AudioBeginEvent, music.on_audio_begin)  # type: ignore
                bot.subscribe(hikariwave.AudioEndEvent, music.on_audio_end)  # type: ignore
            if voice_tts:
                await voice_tts.setup(client)
                bot.subscribe(hikari.GuildMessageCreateEvent, voice_tts.on_message)  # type: ignore
                bot.subscribe(hikari.VoiceStateUpdateEvent, voice_tts.on_voice_state_update)  # type: ignore
            await authority_server.start()
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
        if not profile.has_service(config.BotService.ACTIVITY):
            return
        if not actor:
            return
        await actor.update()

    @client.task(lightbulb.uniformtrigger(hours=1, wait_first=False), max_failures=100)
    async def task_clear_uploads(cleaner: File_Cleaner, stats: Stats_System):
        if not profile.has_service(config.BotService.FILE_CLEANER):
            return
        for folder, td in cleaner.folders_to_clear.items():
            if not config.SILENT_DEBUG:
                log.debug(f"Clearing {folder}")
            if config.IS_DEBUG and stats.disk.percent > 90:
                log.info("Clearing immediately as disk > 90%")
                td = timedelta(seconds=1)
            cleaner.clear(set(folder.iterdir()), td)

    @client.task(lightbulb.uniformtrigger(minutes=5, wait_first=False), max_failures=100)
    async def task_online_drink_reminders(tracker: Online_Tracker):
        if not profile.has_service(config.BotService.ONLINE_TRACKING):
            return
        await tracker.send_drink_reminders(bot)

    @client.task(lightbulb.uniformtrigger(minutes=1, wait_first=False), max_failures=100)
    async def task_authority_refresh(acl: Access_Control, names: Name_Cache):
        if config.DATA_AUTHORITY_MODE is not config.DataAuthorityMode.REMOTE:
            return
        await asyncio.to_thread(names.flush_pending_mutations)
        await asyncio.to_thread(acl.reload)
        await asyncio.to_thread(names.refresh_from_authority)

    auto_app = None  # noqa: F841

    @bot.listen(hikari.StartedEvent)
    async def on_started(event: hikari.StartedEvent):
        log.info("Started")
        # await client.sync_application_commands()
        online_tracker.set_ready_delay(8)
        synced_name_count = await asyncio.to_thread(name_cache.sync_cached_members, bot.cache)
        if synced_name_count:
            log.info(f"Synced {synced_name_count} cached member identities on startup")

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

        silent = Path("silent_restart")
        if config.STARTED_CHANNEL and not silent.exists():
            txt = ["Started: DEBUG" if config.IS_DEBUG else "Started"]
            if auto_app:
                txt.append(f"\tAuto-Launching: {auto_app.friendly}")
            txt.extend(starting_xcp)
            flags = hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
            try:
                await bot.rest.create_message(config.STARTED_CHANNEL, "\n".join(txt), flags=flags)
            except Exception:
                log.exception("STARTED MESSAGE")
        silent.unlink(missing_ok=True)

        rmid_file = Path("restart_message_id")
        if rmid_file.exists():
            chan_id, mess_id = rmid_file.read_text().strip().split(":")
            rmid_file.unlink()
            mess = await resolutator.message(int(mess_id), int(chan_id))
            if mess:
                await mess.edit(f"{mess.content or ''} ...Done! :D")

        # await se_app.setup()

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
        if voice_tts:
            await voice_tts.close()
        if music:
            await music.close()
        if voice_client:
            await voice_client.close()
        await authority_server.stop()
        await app_manager.end()
        is_silent_restart = config.IS_RESTARTING and Path("silent_restart").exists()
        if not config.STARTED_CHANNEL or is_silent_restart:
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
        if guild := event.get_guild():
            synced_name_count = await asyncio.to_thread(name_cache.sync_members, event.members.values())
            if synced_name_count:
                log.info(f"Synced {synced_name_count} member identities for guild {event.guild_id}")
            if config.CLEAR_CMDS:
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
            if voice_tts:
                await voice_tts.on_guild_available(guild, client)
            # log.debug(f"{text_chans=}")

    @bot.listen(hikari.GuildJoinEvent)
    async def _on_guild_join(event: hikari.GuildJoinEvent):
        synced_name_count = await asyncio.to_thread(name_cache.sync_members, event.members.values())
        if synced_name_count:
            log.info(f"Synced {synced_name_count} member identities for joined guild {event.guild_id}")

    @bot.listen(hikari.MemberCreateEvent)
    async def _on_member_create(event: hikari.MemberCreateEvent):
        await asyncio.to_thread(name_cache.set_names, event.member)

    @bot.listen(hikari.MemberUpdateEvent)
    async def _on_member_update(event: hikari.MemberUpdateEvent):
        await asyncio.to_thread(name_cache.set_names, event.member)

    @bot.listen(hikari.MemberDeleteEvent)
    async def _on_member_delete(event: hikari.MemberDeleteEvent):
        await asyncio.to_thread(name_cache.remove_guild_name, int(event.user.id), event.guild_id)

    @bot.listen(hikari.InteractionCreateEvent)
    async def _route_alias_editor(event: hikari.InteractionCreateEvent):
        interaction = event.interaction
        interaction_user = getattr(interaction, "user", None)
        if interaction_user is not None and not interaction_user.is_bot:
            await asyncio.to_thread(name_cache.set_names, getattr(interaction, "member", None) or interaction_user)
        if isinstance(interaction, hikari.ComponentInteraction):
            handled = await alias_editor.route_component(
                interaction,
                acl=acl,
                names_cache=name_cache,
                manager=app_manager,
            )
            if handled:
                return
            handled = await app_editor.route_component(
                interaction,
                acl=acl,
                manager=app_manager,
            )
            if handled:
                return
            handled = await dashboard_editor.route_component(
                interaction,
                acl=acl,
                bot=bot,
                manager=app_manager,
                names_cache=name_cache,
                stats=stats,
                tracker=online_tracker,
            )
            if handled:
                return
            handled = await online_editor.route_component(
                interaction,
                acl=acl,
                tracker=online_tracker,
                names_cache=name_cache,
                bot=bot,
            )
            if handled:
                return
            if voice_tts:
                handled = await voice_admin_editor.route_component(
                    interaction,
                    acl=acl,
                    voice_tts=voice_tts,
                )
                if handled:
                    return
                handled = await voice_editor.route_component(
                    interaction,
                    acl=acl,
                    voice_tts=voice_tts,
                )
                if handled:
                    return
        if isinstance(interaction, hikari.ModalInteraction):
            handled = await alias_editor.route_modal(
                interaction,
                acl=acl,
                names_cache=name_cache,
                manager=app_manager,
            )
            if handled:
                return
            handled = await app_editor.route_modal(
                interaction,
                acl=acl,
                manager=app_manager,
            )
            if handled:
                return
            handled = await online_editor.route_modal(
                interaction,
                acl=acl,
                tracker=online_tracker,
                names_cache=name_cache,
                bot=bot,
            )
            if handled:
                return
            if voice_tts:
                handled = await voice_admin_editor.route_modal(
                    interaction,
                    acl=acl,
                    voice_tts=voice_tts,
                )
                if handled:
                    return
                await voice_editor.route_modal(
                    interaction,
                    acl=acl,
                    voice_tts=voice_tts,
                )

    @bot.listen(hikari.MessageCreateEvent)
    async def _route_app_editor_uploads(event: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if event.author.is_bot:
            return
        await app_editor.route_message(
            event.message,
            bot=bot,
            acl=acl,
            manager=app_manager,
        )

    @bot.listen(hikari.MessageCreateEvent)
    async def _add_names(event: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if hasattr(event, "member"):
            name_cache.set_names(event.member or event.author)  # type: ignore
        else:
            name_cache.set_names(event.author)

    @bot.listen(hikari.PresenceUpdateEvent)
    async def _on_presence_update(event: hikari.PresenceUpdateEvent):
        if not profile.has_service(config.BotService.ONLINE_TRACKING):
            return
        await online_tracker.on_presence_update(event, bot, name_cache)

    @bot.listen(hikari.MessageCreateEvent)
    async def _failsafe_restart(event: hikari.MessageCreateEvent | hikari.GuildMessageCreateEvent):
        if not event.content:
            return
        if event.author.is_bot:
            return
        if "restart_system" not in event.content or "restart_bot" not in event.content:
            return
        if acl.perm_check(event.author_id, acl.LvL.sudo):
            await event.message.respond("Yes sir 🫡")
            await _sys.restart(
                event.message, bot, app_manager, "system" if "restart_system" in event.content else "bot"
            )

    bot.run()


if __name__ == "__main__":
    main()

# AiviA APasz
