from __future__ import annotations

import logging
from enum import StrEnum

import hikari
import lightbulb

import _sys
import config
from _discord import Distils
from _manager import App_Manager, ac_app_logs
from _security import Access_Control
from cmd_music import MusicService
from cmd_voice import VoiceTTSService

log = logging.getLogger(__name__)

group_ops = lightbulb.Group("ops", "Operational commands")  # type: ignore


class RestartTarget(StrEnum):
    BOT = "bot"
    VOICE = "voice"
    SYSTEM = "system"


RESTART_TARGET_PERMISSIONS: dict[RestartTarget, Access_Control.LvL] = {
    RestartTarget.VOICE: Access_Control.LvL.admin,
    RestartTarget.BOT: Access_Control.LvL.sudo,
    RestartTarget.SYSTEM: Access_Control.LvL.sudo,
}


def available_restart_targets(
    profile: config.BotProfileConfig = config.ACTIVE_BOT_PROFILE,
) -> tuple[RestartTarget, ...]:
    return tuple(
        target
        for target in RestartTarget
        if target is not RestartTarget.VOICE or profile.has_service(config.BotService.VOICE_TTS)
    )


def restart_target_choices(profile: config.BotProfileConfig = config.ACTIVE_BOT_PROFILE) -> list[lightbulb.Choice]:
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
) -> None:
    if restart_type is RestartTarget.VOICE:
        raise ValueError("Voice restart requires the voice service to be enabled")

    await acl.perm_check(ctx.user.id, restart_required_level(restart_type))
    await ctx.defer()
    log.critical(f"Ops.Restart; target={restart_type.value} silent={silent}: {ctx.user.display_name}")
    await _sys.restart(ctx, bot, manager, restart_type.value, silent)


async def reset_voice_runtime(ctx: lightbulb.Context) -> None:
    async with ctx.client.di.enter_context(lightbulb.di.Contexts.DEFAULT) as di:
        voice_tts = await di.get(VoiceTTSService)
        try:
            music = await di.get(MusicService)
        except Exception:
            music = None

    music_guild_ids = music.active_guild_ids() if music else []
    music_result = await music.reset_runtime() if music else None
    voice_result = await voice_tts.reset_runtime(extra_guild_ids=music_guild_ids)
    lines = ["Voice layer reset complete."]
    if music_result:
        lines.extend(
            [
                f"music sessions dropped: `{music_result.session_count}`",
                f"music queued tracks cleared: `{music_result.track_count}`",
                f"managed music sources cleaned: `{music_result.managed_source_count}`",
            ]
        )
    lines.extend(
        [
            f"TTS outstanding jobs cleared: `{voice_result.outstanding_job_count}`",
            f"voice connections reset: `{voice_result.active_connection_count}`",
            f"voice guilds targeted: `{voice_result.targeted_guild_count}`",
            f"TTS connect backoffs cleared: `{voice_result.backoff_count}`",
            f"TTS worker running: `{'yes' if voice_result.worker_restarted else 'no'}`",
        ]
    )
    await ctx.respond("\n".join(lines))


@group_ops.register
class CMD_OpsLog(
    lightbulb.SlashCommand,
    name="logs",
    description="Retrieve log for app/system",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What to get logs for", autocomplete=ac_app_logs)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, distils: Distils, manager: App_Manager):
        await respond_logs(ctx, self.app, acl, distils, manager)


@group_ops.register
class CMD_OpsRestart(
    lightbulb.SlashCommand,
    name="restart",
    description="Restart the bot or host system",
    hooks=[lightbulb.prefab.sliding_window(60, 1, "global")],
):
    target = lightbulb.string(
        "target",
        "What to restart",
        choices=restart_target_choices(),
        default=RestartTarget.BOT.value,
    )
    silent = lightbulb.boolean("silent", "Suppress shutdown/startup messages", default=False)

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
            log.critical(f"Ops.Restart; target={restart_type.value} silent={self.silent}: {ctx.user.display_name}")
            await reset_voice_runtime(ctx)
            return
        await restart_host_or_bot(ctx, acl, bot, manager, restart_type, self.silent)
