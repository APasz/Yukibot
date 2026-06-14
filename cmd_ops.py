from __future__ import annotations

import logging
from dataclasses import dataclass

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

log = logging.getLogger(__name__)

group_ops = lightbulb.Group("ops", "Operational commands")  # type: ignore


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
    if restart_type is RestartTarget.VOICE:
        raise ValueError("Voice restart requires the voice service to be enabled")

    await acl.perm_check(ctx.user.id, restart_required_level(restart_type))
    auto_start_apps = (
        manager.set_running_restart_auto_start_apps()
        if auto_restart_running_app
        else manager.set_restart_auto_start_apps(())
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


@dataclass(frozen=True, slots=True)
class VoiceRuntimeResetSummary:
    session_count: int
    track_count: int
    managed_source_count: int
    outstanding_job_count: int
    active_connection_count: int
    targeted_guild_count: int
    backoff_count: int
    worker_restarted: bool


async def reset_voice_runtime_services(
    voice_tts: VoiceTTSService,
    music: MusicService | None,
) -> VoiceRuntimeResetSummary:
    music_guild_ids = music.active_guild_ids() if music else []
    music_result = await music.reset_runtime() if music else None
    voice_result = await voice_tts.reset_runtime(extra_guild_ids=music_guild_ids)
    return VoiceRuntimeResetSummary(
        session_count=music_result.session_count if music_result else 0,
        track_count=music_result.track_count if music_result else 0,
        managed_source_count=music_result.managed_source_count if music_result else 0,
        outstanding_job_count=voice_result.outstanding_job_count,
        active_connection_count=voice_result.active_connection_count,
        targeted_guild_count=voice_result.targeted_guild_count,
        backoff_count=voice_result.backoff_count,
        worker_restarted=voice_result.worker_restarted,
    )


def voice_runtime_reset_lines(summary: VoiceRuntimeResetSummary) -> list[str]:
    lines = ["Voice layer reset complete."]
    if summary.session_count or summary.track_count or summary.managed_source_count:
        lines.extend(
            [
                f"music sessions dropped: `{summary.session_count}`",
                f"music queued tracks cleared: `{summary.track_count}`",
                f"managed music sources cleaned: `{summary.managed_source_count}`",
            ]
        )
    lines.extend(
        [
            f"TTS outstanding jobs cleared: `{summary.outstanding_job_count}`",
            f"voice connections reset: `{summary.active_connection_count}`",
            f"voice guilds targeted: `{summary.targeted_guild_count}`",
            f"TTS connect backoffs cleared: `{summary.backoff_count}`",
            f"TTS worker running: `{'yes' if summary.worker_restarted else 'no'}`",
        ]
    )
    return lines


async def reset_voice_runtime(ctx: lightbulb.Context) -> None:
    async with ctx.client.di.enter_context(lightbulb.di.Contexts.DEFAULT) as di:
        voice_tts = await di.get(VoiceTTSService)
        try:
            music = await di.get(MusicService)
        except Exception:
            music = None

    summary = await reset_voice_runtime_services(voice_tts, music)
    lines = voice_runtime_reset_lines(summary)
    await ctx.respond("\n".join(lines))


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
        await restart_host_or_bot(
            ctx,
            acl,
            bot,
            manager,
            restart_type,
            self.silent,
            self.auto_restart_running_app,
        )
