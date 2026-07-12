from __future__ import annotations

import logging

import hikari
import lightbulb

import _sys
import config
from _discord import Distils
from _manager import App_Manager, ac_app_logs
from _security import Access_Control
from cmd_music import MusicService
from cmd_voice import VoiceTTSService
from restart_targets import RestartTarget
from restart_state import RestartKind, record_voice_restart

log = logging.getLogger(__name__)

group_ops = lightbulb.Group("ops", "Operational commands")  # type: ignore


RESTART_TARGET_PERMISSIONS: dict[RestartTarget, Access_Control.LvL] = {
    RestartTarget.VOICE: Access_Control.LvL.admin,
    RestartTarget.BOT: Access_Control.LvL.sudo,
    RestartTarget.SYSTEM: Access_Control.LvL.sudo,
    RestartTarget.PORTAL: Access_Control.LvL.sudo,
}


def available_restart_targets(
    profile: config.BotProfileConfig = config.ACTIVE_BOT_PROFILE,
) -> tuple[RestartTarget, ...]:
    return tuple(
        target
        for target in RestartTarget
        if (target is not RestartTarget.VOICE or profile.has_service(config.BotService.VOICE_TTS))
        and (target is not RestartTarget.PORTAL or profile.name is config.BotProfileName.YUKI)
    )


def available_maintenance_restart_targets(
    profile: config.BotProfileConfig = config.ACTIVE_BOT_PROFILE,
) -> tuple[RestartTarget, ...]:
    return available_restart_targets(profile)


def restart_target_choices(profile: config.BotProfileConfig = config.ACTIVE_BOT_PROFILE) -> list[lightbulb.Choice[str]]:
    return [lightbulb.Choice(target.value, target.value) for target in available_restart_targets(profile)]


def restart_required_level(restart_type: RestartTarget) -> Access_Control.LvL:
    return RESTART_TARGET_PERMISSIONS[restart_type]


async def respond_logs(
    ctx: lightbulb.Context,
    app_name: str,
    acl: Access_Control,
    distils: Distils,
    manager: App_Manager,
) -> None:
    await acl.perm_check(ctx.user.id, acl.LvL.user)
    log.info(f"Ops.Log; {app_name}: {ctx.user.display_name}")

    if app_name.lower() == "system":
        target = [(config.DIR_LOG / app_name).with_suffix(".log")]
        name = app_name
    else:
        app = manager.get(app_name)
        target = [app.dir_log]
        name = app.friendly
    await distils.respond_files(ctx, target, display_name="logs", app_name=name)


async def restart_host_or_bot(
    ctx: lightbulb.Context,
    acl: Access_Control,
    bot: hikari.GatewayBot,
    manager: App_Manager,
    restart_type: RestartTarget,
    silent: bool,
    auto_restart_running_app: bool,
) -> None:
    if restart_type not in {RestartTarget.BOT, RestartTarget.SYSTEM}:
        raise ValueError(f"{restart_type.value.title()} restart requires its target-specific handler")

    await acl.perm_check(ctx.user.id, restart_required_level(restart_type))
    auto_start_apps = _sys.configure_restart_auto_start_apps(
        manager,
        enabled=auto_restart_running_app,
    )
    await ctx.defer()
    log.critical(
        "Ops.Restart; target=%s silent=%s auto_restart_running_app=%s auto_start_app=%s: %s",
        restart_type.value,
        silent,
        auto_restart_running_app,
        ",".join(auto_start_apps) if auto_start_apps else "none",
        ctx.user.display_name,
    )
    await _sys.restart(ctx, bot, manager, restart_type.value, silent)


async def reset_voice_runtime_services(
    voice_tts: VoiceTTSService,
    music: MusicService | None,
) -> None:
    music_guild_ids = music.active_guild_ids() if music else []
    if music:
        await music.reset_runtime()
    await voice_tts.reset_runtime(extra_guild_ids=music_guild_ids)


async def restart_portal(
    ctx: lightbulb.Context,
    acl: Access_Control,
    silent: bool,
) -> None:
    if config.ACTIVE_BOT_PROFILE.name is not config.BotProfileName.YUKI:
        raise ValueError("Portal restart is only available from the Yuki profile")
    await acl.perm_check(ctx.user.id, restart_required_level(RestartTarget.PORTAL))
    await ctx.defer()
    log.critical(
        "Ops.Restart; target=%s silent=%s: %s",
        RestartTarget.PORTAL.value,
        silent,
        ctx.user.display_name,
    )
    await _sys.restart_portal(ctx, silent=silent)


async def reset_voice_runtime(ctx: lightbulb.Context) -> None:
    async with ctx.client.di.enter_context(lightbulb.di.Contexts.DEFAULT) as di:
        voice_tts = await di.get(VoiceTTSService)
        try:
            music = await di.get(MusicService)
        except Exception:
            music = None

    await reset_voice_runtime_services(voice_tts, music)
    record_voice_restart(RestartKind.MANUAL_VOICE)
    await ctx.respond("Voice restart complete.")


@group_ops.register
class CMD_OpsLog(
    lightbulb.SlashCommand,
    name="logs",
    description="Retrieve log for app/system",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What to get logs for", autocomplete=ac_app_logs)  # pyright: ignore[reportArgumentType]

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, distils: Distils, manager: App_Manager):
        await respond_logs(ctx, self.app, acl, distils, manager)


@group_ops.register
class CMD_OpsRestart(
    lightbulb.SlashCommand,
    name="restart",
    description="Restart a Yuki service or the host system",
    hooks=[lightbulb.prefab.sliding_window(60, 1, "global")],
):
    target = lightbulb.string(
        "target",
        "What to restart",
        choices=restart_target_choices(),
        default=RestartTarget.BOT.value,
    )
    silent = lightbulb.boolean("silent", "Suppress shutdown/startup messages", default=False)
    auto_restart_running_app = lightbulb.boolean(
        "auto_restart_running_app",
        "Restart the currently running app after the bot comes back up",
        default=False,
    )

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        manager: App_Manager,
    ):
        restart_type = RestartTarget(self.target)
        await acl.perm_check(ctx.user.id, restart_required_level(restart_type))
        if restart_type is RestartTarget.VOICE:
            await ctx.defer()
            log.critical(
                "Ops.Restart; target=%s silent=%s auto_restart_running_app=%s: %s",
                restart_type.value,
                self.silent,
                self.auto_restart_running_app,
                ctx.user.display_name,
            )
            await reset_voice_runtime(ctx)
            return
        if restart_type is RestartTarget.PORTAL:
            await restart_portal(ctx, acl, self.silent)
            return
        await restart_host_or_bot(
            ctx,
            acl,
            bot,
            manager,
            restart_type,
            self.silent,
            self.auto_restart_running_app,
        )
