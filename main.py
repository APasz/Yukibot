import asyncio
import inspect
import logging
import os
import signal
import traceback
from collections.abc import Callable
from concurrent.futures import Future as ConcurrentFuture
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import Logger
from pathlib import Path
from typing import Any, Protocol

from startup_logging import install_startup_exception_logger

install_startup_exception_logger()

import hikari
import hikariwave
import lightbulb

import _sys
import config
from _activity import Activity_Manager, Provider_CPU, Provider_DISK, Provider_RAM
from _async_utils import run_blocking
from _authority_server import AuthorityServer
from _discord import DC_Relay, Distils, cached_member_role_color, color_int_to_hex
from _file import File_Utils
from _manager import App_Manager, AppStartBlocker, AppStartBlockerKind, ManagedApp, Provider_Player, Provider_Process
from _resolator import Resolutator
from _security import Access_Control
from _sys import Stats_System
from _utils import File_Cleaner, Utilities
from apps._app import App
from cmd_alias import AliasEditorService, CMD_Alias
from cmd_app import group_app
from cmd_app_console import AppConsoleService
from cmd_app_manage import AppManageService
from cmd_dashboard import CMD_Dashboard, DashboardEditorService
from cmd_misc import group_misc
from cmd_music import MusicService, group_music
from cmd_online import CMD_Online, OnlineEditorService
from cmd_ops import available_maintenance_restart_targets, group_ops, reset_voice_runtime_services
from cmd_voice import VoiceAdminEditorService, VoiceSettingsEditorService, VoiceTTSService, group_voice
from config import Activity_Provider, Name_Cache, NodeCapacityProfile
from font_assets import font_assets
from maintenance import MaintenanceService
from mirror_models import MirrorAutoSyncOutcome, MirrorAutoSyncResult
from mirror_service import MirrorService
from node_api_system import NodeSystemAction
from node_api_relay import RemoteRelayTTSForwarder
from node_api_http import NodeApiHttpService
from online import Online_Tracker
from relay_notices import (
    BotLifecycleNotice,
    BotLifecycleStage,
    RelayNotice,
    RelayNoticeSeverity,
    RelayNoticeSource,
    render_system_notice_text,
)
from restart_state import (
    RestartKind,
    mark_pending_process_restart_if_missing,
    record_process_start,
    record_voice_restart,
)
from restart_targets import RestartTarget
from web_dash.service import ModWebService

log: Logger = logging.getLogger("system")

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
start_time: datetime = datetime.now()
_RESTART_AUTO_LAUNCH_DELAY_SECONDS: float = 0.0
_PORTAL_AUTHORITY_REFRESH_INTERVAL_SECONDS: float = 60.0
_PORTAL_MAINTENANCE_REFRESH_INTERVAL_SECONDS: float = 60.0
_PORTAL_MIRROR_SYNC_PACE_SECONDS: float = 15.0


@dataclass(frozen=True, slots=True)
class RestartAutoLaunchSelection:
    apps: tuple[App, ...] = ()
    error_lines: tuple[str, ...] = ()

    @property
    def started_notice_lines(self) -> tuple[str, ...]:
        return tuple[str, ...](f"\tAuto-Launch Scheduled: {app.friendly}" for app in self.apps)


def _consume_restart_auto_launch_selection(app_manager: App_Manager) -> RestartAutoLaunchSelection:
    selected_apps: list[App] = []
    error_lines: list[str] = []
    try:
        auto_start_app_names: tuple[str, ...] = app_manager.consume_restart_auto_start_apps()
    except Exception as xcp:
        return RestartAutoLaunchSelection(error_lines=(str(xcp),))
    for auto_start_app_name in auto_start_app_names:
        try:
            selected_apps.append(app_manager.get(auto_start_app_name))
        except Exception as xcp:
            error_lines.append(str(xcp))
    return RestartAutoLaunchSelection(apps=tuple[App[Any], ...](selected_apps), error_lines=tuple(error_lines))


def _restart_auto_launch_sort_key(app_manager: App_Manager, app: App) -> tuple[int, str]:
    blocker: AppStartBlocker | None = app_manager.start_blocker(app)
    if blocker is None:
        return (0, app.friendly.casefold())
    if blocker.kind is AppStartBlockerKind.ALREADY_RUNNING:
        return (2, app.friendly.casefold())
    return (1, app.friendly.casefold())


def _restart_auto_launch_priority(app: App) -> tuple[int, int, int, int, str]:
    startup_points = app.cfg.resource_points.startup_points
    running_points = app.cfg.resource_points.running
    startup_cpu_delta = startup_points.cpu_points - running_points.cpu_points
    startup_ram_delta = startup_points.ram_points - running_points.ram_points
    return (
        -startup_points.cpu_points,
        -startup_points.ram_points,
        -startup_cpu_delta,
        -startup_ram_delta,
        app.friendly.casefold(),
    )


def _restart_auto_launch_fits_after_ready(
    app_manager: App_Manager,
    candidate: App,
    *,
    active_apps: tuple[App, ...],
) -> bool:
    if any(active_app.scope == candidate.scope for active_app in active_apps):
        return False

    capacity: NodeCapacityProfile = app_manager.node_capacity()
    active_cpu_points: int = sum(active_app.cfg.resource_points.running.cpu_points for active_app in active_apps)
    active_ram_points: int = sum(active_app.cfg.resource_points.running.ram_points for active_app in active_apps)
    startup_points = candidate.cfg.resource_points.startup_points
    return (
        active_cpu_points + startup_points.cpu_points <= capacity.cpu_points_available
        and active_ram_points + startup_points.ram_points <= capacity.ram_points_available
    )


def _plan_restart_auto_launch_sequence(app_manager: App_Manager, auto_apps: tuple[App, ...]) -> tuple[App, ...]:
    queued_apps: tuple[App[Any], ...] = tuple[App[Any], ...](app for app in auto_apps if not app.check_running())
    if not queued_apps:
        return ()

    active_apps: tuple[ManagedApp, ...] = app_manager.running_apps()

    def _search(active: tuple[App, ...], remaining: tuple[App, ...]) -> tuple[App, ...]:
        best_sequence: tuple[App, ...] = ()
        for candidate in sorted(remaining, key=_restart_auto_launch_priority):
            if not _restart_auto_launch_fits_after_ready(app_manager, candidate, active_apps=active):
                continue
            next_remaining = tuple(app for app in remaining if app is not candidate)
            sequence = (candidate, *_search((*active, candidate), next_remaining))
            if len(sequence) > len(best_sequence):
                best_sequence = sequence
        return best_sequence

    return _search(active_apps, queued_apps)


async def _launch_restart_auto_apps(
    app_manager: App_Manager,
    auto_apps: tuple[App, ...],
    *,
    delay_seconds: float = _RESTART_AUTO_LAUNCH_DELAY_SECONDS,
) -> None:
    if not auto_apps:
        return
    auto_app_names = ", ".join(app.friendly for app in auto_apps)
    if delay_seconds > 0:
        log.info("Auto-launch scheduled for %s in %.1fs", auto_app_names, delay_seconds)
        await asyncio.sleep(delay_seconds)
    else:
        log.info("Auto-launch starting for %s", auto_app_names)

    remaining_apps: list[App] = list(auto_apps)
    error_lines: list[str] = []
    while remaining_apps:
        already_running_apps = tuple(app for app in remaining_apps if app.check_running())
        for already_running_app in already_running_apps:
            log.info("Skipping auto-launch for %s because it is already running.", already_running_app.friendly)
            remaining_apps.remove(already_running_app)
        if not remaining_apps:
            break

        planned_sequence = _plan_restart_auto_launch_sequence(app_manager, tuple(remaining_apps))
        if planned_sequence:
            auto_app = planned_sequence[0]
            remaining_apps.remove(auto_app)
        else:
            remaining_apps.sort(key=lambda app: _restart_auto_launch_sort_key(app_manager, app))
            auto_app = remaining_apps.pop(0)
        blocker = app_manager.start_blocker(auto_app)
        if blocker is not None:
            if blocker.kind is AppStartBlockerKind.ALREADY_RUNNING:
                log.info("Skipping auto-launch for %s because it is already running.", auto_app.friendly)
                continue
            error_lines.append(blocker.message)
            log.warning("Skipping auto-launch for %s because startup is blocked: %s", auto_app.friendly, blocker.message)
            continue
        log.info("Auto-launching: %s", auto_app.friendly)
        try:
            await app_manager.launch(auto_app)
        except Exception as xcp:
            error_lines.append(str(xcp))
            log.exception("Auto-launch failed for %s", auto_app.friendly)
            continue
        log.info("Auto-launched: %s", auto_app.friendly)

    if error_lines:
        raise RuntimeError("\n".join(error_lines))


def _build_startup_notice(
    *,
    auto_launch: RestartAutoLaunchSelection,
    startup_disabled_lines: tuple[str, ...],
    error_lines: tuple[str, ...],
) -> BotLifecycleNotice:
    combined_error_lines = (*error_lines, *auto_launch.error_lines)
    severity = RelayNoticeSeverity.WARNING if startup_disabled_lines or combined_error_lines else RelayNoticeSeverity.INFO
    return BotLifecycleNotice(
        stage=BotLifecycleStage.STARTED,
        source=RelayNoticeSource.BOT,
        severity=severity,
        debug_mode=config.IS_DEBUG,
        auto_launch_app_names=tuple(app.friendly for app in auto_launch.apps),
        startup_disabled_lines=startup_disabled_lines,
        error_lines=combined_error_lines,
    )


def _build_shutdown_notice(*, started_at: datetime, now: datetime) -> BotLifecycleNotice:
    uptime_seconds = max(0, int((now - started_at).total_seconds()))
    return BotLifecycleNotice(
        stage=BotLifecycleStage.STOPPING,
        source=RelayNoticeSource.BOT,
        uptime_seconds=uptime_seconds,
    )


def _build_bot_error_notice(error_text: str) -> BotLifecycleNotice:
    summary = error_text.strip()
    if not summary:
        raise ValueError("Bot lifecycle error notice text must not be blank.")
    return BotLifecycleNotice(
        stage=BotLifecycleStage.ERROR,
        source=RelayNoticeSource.BOT,
        severity=RelayNoticeSeverity.ERROR,
        summary=summary,
    )


async def _post_started_channel_notice(
    bot: hikari.GatewayBot,
    notice: RelayNotice,
    *,
    error_context: str,
) -> None:
    if not config.STARTED_CHANNEL:
        return
    flags = hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
    try:
        await bot.rest.create_message(
            config.STARTED_CHANNEL,
            render_system_notice_text(notice),
            flags=flags,
        )
    except Exception:
        log.exception(error_context)


class _CleanupDisk(Protocol):
    @property
    def percent(self) -> int: ...

    @property
    def mountpoint_text(self) -> str: ...


class _CleanupStats(Protocol):
    def disk_for_path(self, path: Path) -> _CleanupDisk | None: ...


class _ManagedFileCleaner(Protocol):
    @property
    def folders_to_clear(self) -> dict[Path, timedelta]: ...

    def clear(self, paths: set[Path], threshold: timedelta | None = None) -> set[Path]: ...


def _clear_managed_files_once(
    cleaner: _ManagedFileCleaner,
    stats: _CleanupStats,
    *,
    profile: config.BotProfileConfig,
) -> None:
    if not profile.has_service(config.BotService.FILE_CLEANER):
        return

    for folder, threshold in cleaner.folders_to_clear.items():
        if not config.SILENT_DEBUG:
            log.debug("Clearing %s", folder)
        disk = stats.disk_for_path(folder)
        effective_threshold = threshold
        if config.IS_DEBUG and disk is not None and disk.percent > 90:
            log.info("Clearing immediately as disk > 90%% for %s", disk.mountpoint_text)
            effective_threshold = timedelta(seconds=1)
        cleaner.clear(set(folder.iterdir()), effective_threshold)


async def _run_portal() -> None:
    log.info("Starting portal profile")
    record_process_start(start_time)
    log.info(
        "Portal authority config: mode=%s endpoint=%s token=%s",
        config.DATA_AUTHORITY_MODE.value,
        config.DATA_AUTHORITY_ENDPOINT.base_url if config.DATA_AUTHORITY_ENDPOINT is not None else "None",
        "set" if config.DATA_AUTHORITY_TOKEN else "missing",
    )
    acl = Access_Control()
    mod_web = ModWebService()
    maintenance = MaintenanceService()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    restart_requested = False

    def _request_stop() -> None:
        if stop_event.is_set():
            return
        log.info("Stopping portal profile")
        mod_web.begin_shutdown()
        stop_event.set()

    def _request_restart(restart_kind: RestartKind = RestartKind.MANUAL_BOT) -> None:
        nonlocal restart_requested
        if stop_event.is_set():
            return
        mark_pending_process_restart_if_missing(restart_kind)
        restart_requested = True
        config.IS_RESTARTING = True
        log.critical("Restarting portal profile")
        mod_web.begin_shutdown()
        stop_event.set()

    def _schedule_restart() -> None:
        loop.call_soon_threadsafe(_request_restart)

    def _schedule_system_action(action: NodeSystemAction, auto_restart_running_apps: bool, silent: bool) -> None:
        del auto_restart_running_apps, silent
        if action is not NodeSystemAction.RESTART_PROCESS:
            raise ValueError(f"Portal does not support {action.value}.")
        _schedule_restart()

    mod_web.set_system_action_handler(_schedule_system_action)
    mod_web.set_maintenance_service(maintenance, (RestartTarget.BOT,))
    await mod_web.start(acl=acl)

    refresh_task: asyncio.Task[None] | None = None
    if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.REMOTE:
        refresh_task = asyncio.create_task(_portal_authority_refresh_loop(acl=acl, stop_event=stop_event))
    mirror_sync_task: asyncio.Task[None] | None = None
    if config.mirror_hosting_enabled():
        mirrors = mod_web.mirror_service
        if mirrors is None:
            raise RuntimeError("Portal mirror service was not initialised.")
        mirror_sync_task = asyncio.create_task(
            _portal_mirror_sync_loop(mirrors=mirrors, stop_event=stop_event),
            name="portal-mirror-sync",
        )
    maintenance_task = asyncio.create_task(
        _portal_maintenance_restart_loop(
            maintenance=maintenance,
            stop_event=stop_event,
            on_restart=_request_restart,
        )
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, _request_stop)

    try:
        await stop_event.wait()
    finally:
        for task in (refresh_task, mirror_sync_task, maintenance_task):
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    if restart_requested:
        raise SystemExit(1)


async def _refresh_portal_remote_state(acl: Access_Control) -> None:
    if config.DATA_AUTHORITY_MODE is not config.DataAuthorityMode.REMOTE:
        return
    await run_blocking(acl.reload)
    try:
        await run_blocking(config.fetch_remote_bot_registry)
    except Exception as xcp:
        log.warning("Portal bot registry refresh failed; keeping cached node metadata: %s", xcp)


async def _portal_authority_refresh_loop(
    *,
    acl: Access_Control,
    stop_event: asyncio.Event,
    interval_seconds: float = _PORTAL_AUTHORITY_REFRESH_INTERVAL_SECONDS,
) -> None:
    while not stop_event.is_set():
        await _refresh_portal_remote_state(acl)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def _portal_maintenance_restart_loop(
    *,
    maintenance: MaintenanceService,
    stop_event: asyncio.Event,
    on_restart: Callable[[RestartKind], None],
    interval_seconds: float = _PORTAL_MAINTENANCE_REFRESH_INTERVAL_SECONDS,
) -> None:
    available_targets = (RestartTarget.BOT,)
    while not stop_event.is_set():
        maintenance.reload()
        now = datetime.now().astimezone()
        due_targets = maintenance.due_restart_targets(now=now, available_targets=available_targets)
        if due_targets:
            maintenance.mark_triggered(due_targets, triggered_at=now)
            log.critical(
                "Portal maintenance restart: due=%s",
                ",".join(target.value for target in due_targets),
            )
            on_restart(RestartKind.SCHEDULED_BOT)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def _portal_mirror_sync_loop(
    *,
    mirrors: MirrorService,
    stop_event: asyncio.Event,
    interval_seconds: float = _PORTAL_MIRROR_SYNC_PACE_SECONDS,
) -> None:
    """Run one due Git mirror check per paced Portal scheduler tick."""

    while not stop_event.is_set():
        try:
            result: MirrorAutoSyncResult | None = await run_blocking(mirrors.sync_next_due_git_project)
        except Exception:
            log.exception("Portal automatic mirror check crashed")
        else:
            if result is not None:
                detail = result.project.status_detail or "No status detail."
                if result.outcome is MirrorAutoSyncOutcome.FAILED:
                    log.warning("Portal automatic mirror check failed: project=%s detail=%s", result.project_id, detail)
                else:
                    log.info(
                        "Portal automatic mirror check: project=%s outcome=%s detail=%s",
                        result.project_id,
                        result.outcome.value,
                        detail,
                    )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


def main():
    config.IS_SHUTTINGDOWN = False
    log.info(f"Running {os.getpid()}")
    profile = config.ACTIVE_BOT_PROFILE
    if profile.name is config.BotProfileName.PORTAL:
        asyncio.run(_run_portal())
        return
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
    app_console = AppConsoleService()
    dashboard_editor = DashboardEditorService()
    online_editor = OnlineEditorService()
    voice_admin_editor = VoiceAdminEditorService()
    voice_editor = VoiceSettingsEditorService()
    online_tracker = Online_Tracker()
    stats = Stats_System()
    file_cleaner = File_Cleaner()
    maintenance = MaintenanceService()
    client: lightbulb.Client

    client = lightbulb.client_from_app(bot)

    utilities = Utilities()
    resolutator = Resolutator(bot)
    authority_server = AuthorityServer(name_cache)
    node_api_server = NodeApiHttpService()
    bot_runtime_loop: asyncio.AbstractEventLoop | None = None

    def _schedule_node_system_action(
        action: NodeSystemAction,
        auto_restart_running_apps: bool,
        silent: bool,
    ) -> None:
        runtime_loop = bot_runtime_loop
        if runtime_loop is None:
            raise RuntimeError("Bot runtime loop is not available for node system actions.")

        async def _execute_node_system_action() -> None:
            auto_start_apps = _sys.configure_restart_auto_start_apps(
                app_manager,
                enabled=auto_restart_running_apps,
            )
            restart_type = "system" if action is NodeSystemAction.REBOOT_HOST else "bot"
            log.critical(
                "Executing web dashboard node system action: action=%s auto_restart_running_apps=%s "
                "auto_start_apps=%s",
                action.value,
                auto_restart_running_apps,
                ",".join(auto_start_apps) if auto_start_apps else "none",
            )
            await _sys.scheduled_restart(
                bot=bot,
                manager=app_manager,
                restart_type=restart_type,
                reason=f"Web dashboard requested {action.value}.",
                message_channel_id=None,
                suppress_notifications=True,
                scheduled=False,
                silent=silent,
            )

        future = asyncio.run_coroutine_threadsafe(_execute_node_system_action(), runtime_loop)

        def _log_system_action_failure(completed: ConcurrentFuture[None]) -> None:
            try:
                completed.result()
            except SystemExit:
                return
            except BaseException:
                log.exception("Web dashboard node system action failed: action=%s", action.value)

        future.add_done_callback(_log_system_action_failure)

    node_api_server.set_system_action_handler(_schedule_node_system_action)
    node_api_server.set_maintenance_service(
        maintenance,
        available_maintenance_restart_targets(profile),
    )
    registry = client.di.registry_for(lightbulb.di.Contexts.DEFAULT)
    acl = Access_Control()
    registry.register_value(Access_Control, acl)
    registry.register_value(hikari.GatewayBot, bot)
    registry.register_value(lightbulb.Client, client)
    registry.register_value(App_Manager, app_manager)
    registry.register_value(Distils, Distils())
    registry.register_value(Resolutator, resolutator)
    dc_relay = DC_Relay(bot)
    if profile.has_service(config.BotService.GAME_RELAY):
        node_api_server.set_chat_relay_service(dc_relay)
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
        dc_relay.set_voice_tts_service(voice_tts)
        app_editor.set_voice_target_service(voice_tts)
        node_api_server.set_relay_tts_service(voice_tts)
    elif profile.has_service(config.BotService.GAME_RELAY):
        dc_relay.set_voice_tts_service(RemoteRelayTTSForwarder())
    if music and voice_tts:
        voice_tts.set_music_active_channel_provider(music.active_channel_id)
        voice_tts.set_music_duck_handler(music.duck_tts_playback)
    registry.register_value(DC_Relay, dc_relay)
    registry.register_value(Utilities, utilities)
    registry.register_value(File_Utils, File_Utils())
    registry.register_value(Name_Cache, name_cache)
    registry.register_value(AliasEditorService, alias_editor)
    registry.register_value(AppManageService, app_editor)
    registry.register_value(AppConsoleService, app_console)
    registry.register_value(DashboardEditorService, dashboard_editor)
    registry.register_value(OnlineEditorService, online_editor)
    registry.register_value(VoiceAdminEditorService, voice_admin_editor)
    registry.register_value(VoiceSettingsEditorService, voice_editor)
    registry.register_value(Online_Tracker, online_tracker)
    registry.register_value(Stats_System, stats)
    registry.register_value(MaintenanceService, maintenance)
    registry.register_value(File_Cleaner, file_cleaner)

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
        nonlocal bot_runtime_loop
        bot_runtime_loop = asyncio.get_running_loop()
        log.info("Starting")
        try:
            await client.start()
            _clear_managed_files_once(file_cleaner, stats, profile=profile)
            am = await di_inject_providers()
            await app_manager.post_init(bot, am)
            font_assets.schedule_startup_refresh(google_font_urls=app_manager.node_font_sources().google_font_urls)
            if profile.has_service(config.BotService.GAME_RELAY):
                dc_relay.set_event_loop()
            await node_api_server.start(app_manager, acl=acl)

            if profile.has_service(config.BotService.GAME_RELAY):
                log.info("Starting Discord relay service")
                await dc_relay.setup()
                bot.subscribe(hikari.MessageCreateEvent, dc_relay.on_dcdm_message)  # type: ignore
                bot.subscribe(hikari.GuildMessageCreateEvent, dc_relay.on_gddm_message)  # type: ignore
                log.info("Discord relay service ready")
            if music:
                log.info("Starting music service")
                await music.setup(client)
                bot.subscribe(hikari.VoiceStateUpdateEvent, music.on_voice_state_update)  # type: ignore
                bot.subscribe(hikariwave.AudioBeginEvent, music.on_audio_begin)  # type: ignore
                bot.subscribe(hikariwave.AudioEndEvent, music.on_audio_end)  # type: ignore
                log.info("Music service ready")
            if voice_tts:
                log.info("Starting voice TTS service")
                await voice_tts.setup(client)
                bot.subscribe(hikari.GuildMessageCreateEvent, voice_tts.on_message)  # type: ignore
                bot.subscribe(hikari.VoiceStateUpdateEvent, voice_tts.on_voice_state_update)  # type: ignore
                log.info("Voice TTS service ready")
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

    @client.task(lightbulb.uniformtrigger(1, wait_first=False), max_failures=25)
    async def task_activity(actor: Activity_Manager | None):
        if not profile.has_service(config.BotService.ACTIVITY):
            return
        if not actor:
            return
        await actor.update()

    @client.task(lightbulb.uniformtrigger(hours=1), max_failures=100)
    async def task_clear_uploads(cleaner: File_Cleaner, stats: Stats_System):
        _clear_managed_files_once(cleaner, stats, profile=profile)

    @client.task(lightbulb.uniformtrigger(minutes=5, wait_first=False), max_failures=100)
    async def task_online_drink_reminders(tracker: Online_Tracker):
        if not profile.has_service(config.BotService.ONLINE_TRACKING):
            return
        await tracker.send_drink_reminders(bot)

    async def sync_bot_metadata(bot: hikari.GatewayBot, *, initial: bool) -> None:
        try:
            application = await bot.rest.fetch_application()
            supported_install_types = config.supported_oauth_install_types(application)
            install_types_text = ",".join(
                install_type.value
                for install_type in sorted(supported_install_types, key=lambda item: item.integration_type)
            )
            if initial:
                log.info(
                    "Initial OAuth install types for %s: %s",
                    config.ACTIVE_BOT_PROFILE.name.value,
                    install_types_text,
                )
            elif not config.SILENT_DEBUG:
                log.debug(
                    "OAuth install types for %s: %s",
                    config.ACTIVE_BOT_PROFILE.name.value,
                    install_types_text,
                )
            bot_config = await run_blocking(
                config.sync_local_oauth_configuration,
                Path("configuration.json"),
                supported_install_types=supported_install_types,
            )
        except Exception as xcp:
            prefix = "Initial bot OAuth support refresh" if initial else "Bot OAuth support refresh"
            log.warning(f"{prefix} failed; using current local configuration: {xcp}")
            try:
                bot_config = config.load_bot_configuration(Path("configuration.json"))
            except (OSError, ValueError) as configuration_xcp:
                log.warning(
                    "%s could not read the local configuration; metadata sync will retry: %s",
                    prefix,
                    configuration_xcp,
                )
                return

        me = bot.get_me()
        if me is None:
            if initial:
                log.info("Deferring initial bot metadata sync until the current bot user is available")
            else:
                log.warning("Skipping bot metadata sync because the current bot user is unavailable")
            return

        display_avatar_url = getattr(me, "display_avatar_url", None)
        avatar_uri = str(display_avatar_url) if display_avatar_url is not None else None
        accent_color_hex = color_int_to_hex(
            cached_member_role_color(bot, guild_id=config.DISCORD_GUILD, user_id=me.id)
        )
        presentation = (
            config.BotMetadataPresentation(
                avatar_uri=avatar_uri,
                accent_color_hex=accent_color_hex,
            )
            if avatar_uri is not None or accent_color_hex is not None
            else None
        )
        snapshot = config.build_local_bot_metadata_snapshot(
            bot_id=me.id,
            label=me.display_name or me.username,
            bot_profile=profile.name,
            oauth=bot_config.oauth,
            mod_web=config.BotMetadataModWeb(
                node_name=config.MOD_WEB_SERVER.node_name,
                public_base_url=config.MOD_WEB_SERVER.public_base_url,
                node_api_base_url=config.MOD_WEB_SERVER.node_api_base_url,
            ),
            presentation=presentation,
        )
        if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.LOCAL:
            try:
                await run_blocking(config.upsert_known_bot_snapshot, Path("configuration.json"), snapshot)
            except Exception as xcp:
                log.warning(f"Local bot metadata persist failed; node switcher may miss this node: {xcp}")
            return

        try:
            await run_blocking(config.fetch_remote_bot_registry)
        except Exception as xcp:
            log.warning(f"Bot registry refresh failed; node switcher may use stale cache: {xcp}")
        try:
            await run_blocking(config.sync_remote_bot_metadata, snapshot)
        except Exception as xcp:
            log.warning(f"Bot metadata sync failed; keeping local snapshot only: {xcp}")

    @client.task(lightbulb.uniformtrigger(minutes=1, wait_first=False), max_failures=100)
    async def task_authority_refresh(acl: Access_Control, names: Name_Cache, bot: hikari.GatewayBot):
        if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.REMOTE:
            await run_blocking(names.flush_pending_mutations)
            await run_blocking(acl.reload)
            await run_blocking(names.refresh_from_authority)
        await sync_bot_metadata(bot, initial=False)

    @client.task(lightbulb.uniformtrigger(minutes=1, wait_first=False), max_failures=100)
    async def task_maintenance_restarts(maintenance: MaintenanceService, manager: App_Manager):
        maintenance.reload()
        available_targets = available_maintenance_restart_targets(profile)
        now = datetime.now().astimezone()
        due_warnings = maintenance.due_restart_warnings(now=now, available_targets=available_targets)
        for warning in due_warnings:
            warning_notice = maintenance.build_restart_warning_notice(warning)
            warning_text = render_system_notice_text(warning_notice)
            sent_count = await manager.notify_running_app_relays(
                warning_text,
                notice=warning_notice,
            )
            auto_start_apps: tuple[str, ...] = ()
            tts_notice_count = 0
            if warning.lead_minutes == 1:
                auto_start_apps = manager.set_running_restart_auto_start_apps()
                if voice_tts is not None:
                    tts_notice_count = await voice_tts.notify_connected_tts_channels(warning_text)
            log.info(
                "Maintenance.Warning; effective=%s due=%s lead=%sm slot=%s relays=%s auto_start=%s tts=%s",
                warning.effective_target.value,
                ",".join(target.value for target in warning.matched_targets),
                warning.lead_minutes,
                warning.scheduled_for.isoformat(),
                sent_count,
                ",".join(auto_start_apps) if auto_start_apps else "none",
                tts_notice_count,
            )

        due_targets = maintenance.due_restart_targets(now=now, available_targets=available_targets)
        if not due_targets:
            return

        effective_target = maintenance.due_restart_target(now=now, available_targets=due_targets)
        if effective_target is None:
            return

        scheduled_for = maintenance.next_restart_at(effective_target, now=now) or now.replace(second=0, microsecond=0)
        maintenance.mark_triggered(due_targets, triggered_at=now)
        schedule = maintenance.schedule_for(effective_target)
        due_names = ", ".join(target.value for target in due_targets)
        log.critical(
            "Maintenance.Restart; effective=%s due=%s interval=%sm slot=%s",
            effective_target.value,
            due_names,
            schedule.interval_minutes,
            scheduled_for.isoformat(),
        )

        if effective_target is RestartTarget.VOICE:
            if voice_tts is None:
                log.warning("Skipping scheduled voice restart because the voice service is unavailable")
                return
            await reset_voice_runtime_services(voice_tts, music)
            record_voice_restart(RestartKind.SCHEDULED_VOICE, restarted_at=now)
            completion_notice = maintenance.build_restart_completed_notice(
                effective_target=effective_target,
                matched_targets=due_targets,
                scheduled_for=scheduled_for,
            )
            await _post_started_channel_notice(bot, completion_notice, error_context="MAINTENANCE COMPLETE MESSAGE")
            return

        restart_notice = maintenance.build_restart_executing_notice(
            effective_target=effective_target,
            matched_targets=due_targets,
            scheduled_for=scheduled_for,
        )
        await _sys.scheduled_restart(
            bot=bot,
            manager=manager,
            restart_type=effective_target.value,
            reason=render_system_notice_text(restart_notice),
            message_channel_id=config.STARTED_CHANNEL,
            suppress_notifications=True,
        )

    @bot.listen(hikari.StartedEvent)
    async def on_started(event: hikari.StartedEvent):
        log.info("Started")
        record_process_start(start_time)
        # await client.sync_application_commands()
        await sync_bot_metadata(bot, initial=True)
        if profile.has_service(config.BotService.GAME_RELAY):
            dc_relay.log_chat_relay_summary()
        online_tracker.set_ready_delay(8)
        hydrated_presence_count = online_tracker.hydrate_cached_presences(bot)
        if hydrated_presence_count:
            log.info("Hydrated %s tracked presence snapshot(s) from cache on startup", hydrated_presence_count)
        synced_name_count = await run_blocking(name_cache.sync_cached_members, bot.cache)
        if synced_name_count:
            log.info(f"Synced {synced_name_count} cached member identities on startup")

        auto_launch = _consume_restart_auto_launch_selection(app_manager)
        silent = Path("silent_restart")
        if config.STARTED_CHANNEL and not silent.exists():
            startup_notice = _build_startup_notice(
                auto_launch=auto_launch,
                startup_disabled_lines=app_manager.startup_disabled_notice_lines(),
                error_lines=tuple(starting_xcp),
            )
            await _post_started_channel_notice(bot, startup_notice, error_context="STARTED MESSAGE")
        silent.unlink(missing_ok=True)

        rmid_file = Path("restart_message_id")
        if rmid_file.exists():
            chan_id, mess_id = rmid_file.read_text().strip().split(":")
            rmid_file.unlink()
            mess = await resolutator.message(int(mess_id), int(chan_id))
            if mess:
                await mess.edit(f"{mess.content or ''} ...Done! :D")

        if auto_launch.apps:
            try:
                await _launch_restart_auto_apps(app_manager, auto_launch.apps)
            except Exception as xcp:
                log.exception("AUTO_LAUNCH: %s", ", ".join(app.name for app in auto_launch.apps))
                error_notice = _build_bot_error_notice(str(xcp))
                await _post_started_channel_notice(bot, error_notice, error_context="AUTO LAUNCH ERROR MESSAGE")

        # await se_app.setup()

    @bot.listen(hikari.StoppingEvent)
    async def on_stopping(event: hikari.StoppingEvent):
        log.info("Ending")
        print("Ending")
        config.IS_SHUTTINGDOWN = True
        if voice_tts:
            await voice_tts.close()
        if music:
            await music.close()
        if voice_client:
            await voice_client.close()
        await node_api_server.stop()
        await authority_server.stop()
        await app_manager.end()
        if profile.has_service(config.BotService.GAME_RELAY):
            await dc_relay.close()
        is_silent_restart = config.IS_RESTARTING and Path("silent_restart").exists()
        if not config.STARTED_CHANNEL or is_silent_restart:
            return
        shutdown_notice = _build_shutdown_notice(started_at=start_time, now=datetime.now())
        await _post_started_channel_notice(bot, shutdown_notice, error_context="STOPPED MESSAGE")

    @bot.listen(hikari.GuildAvailableEvent)
    async def _on_guild(event: hikari.GuildAvailableEvent):
        if guild := event.get_guild():
            synced_name_count = await run_blocking(name_cache.sync_members, event.members.values())
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
        synced_name_count = await run_blocking(name_cache.sync_members, event.members.values())
        if synced_name_count:
            log.info(f"Synced {synced_name_count} member identities for joined guild {event.guild_id}")

    @bot.listen(hikari.MemberCreateEvent)
    async def _on_member_create(event: hikari.MemberCreateEvent):
        await run_blocking(name_cache.set_names, event.member)

    @bot.listen(hikari.MemberUpdateEvent)
    async def _on_member_update(event: hikari.MemberUpdateEvent):
        await run_blocking(name_cache.set_names, event.member)

    @bot.listen(hikari.MemberDeleteEvent)
    async def _on_member_delete(event: hikari.MemberDeleteEvent):
        await run_blocking(name_cache.remove_guild_name, int(event.user.id), event.guild_id)

    @bot.listen(hikari.InteractionCreateEvent)
    async def _route_alias_editor(event: hikari.InteractionCreateEvent):
        interaction = event.interaction
        interaction_user = getattr(interaction, "user", None)
        if interaction_user is not None and not interaction_user.is_bot:
            await run_blocking(name_cache.set_names, getattr(interaction, "member", None) or interaction_user)
        if isinstance(interaction, hikari.ComponentInteraction):
            handled = await alias_editor.route_component(
                interaction,
                acl=acl,
                names_cache=name_cache,
            )
            if handled:
                return
            handled = await app_editor.route_component(
                interaction,
                bot=bot,
                acl=acl,
                manager=app_manager,
            )
            if handled:
                return
            handled = await app_console.route_component(
                interaction,
                bot=bot,
                acl=acl,
                manager=app_manager,
            )
            if handled:
                return
            handled = await dashboard_editor.route_component(
                interaction,
                acl=acl,
                bot=bot,
                maintenance=maintenance,
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
            )
            if handled:
                return
            handled = await app_editor.route_modal(
                interaction,
                bot=bot,
                acl=acl,
                manager=app_manager,
            )
            if handled:
                return
            handled = await app_console.route_modal(
                interaction,
                bot=bot,
                acl=acl,
                manager=app_manager,
            )
            if handled:
                return
            handled = await dashboard_editor.route_modal(
                interaction,
                acl=acl,
                bot=bot,
                maintenance=maintenance,
                manager=app_manager,
                names_cache=name_cache,
                stats=stats,
                tracker=online_tracker,
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
        if isinstance(event, hikari.GuildMessageCreateEvent):
            name_cache.set_names(event.member or event.author)
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
        if await acl.perm_check(event.author_id, acl.LvL.sudo):
            await event.message.respond("Yes sir 🫡")
            await _sys.restart(
                event.message, bot, app_manager, "system" if "restart_system" in event.content else "bot"
            )

    bot.run()


if __name__ == "__main__":
    main()

# AiviA APasz
