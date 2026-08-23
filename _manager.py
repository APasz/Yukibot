import asyncio
import enum
import importlib
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import hikari
import lightbulb

import config
from _async_utils import run_blocking
from _discord import App_Bound, DC_Bound, DC_Relay, RelayEmbedPayload
from _utils import format_player_capacity
from apps._app import App, AppRuntimeFaultKind
from apps._config import (
    App_Config,
    AppVersion,
    RelayChannelSource,
    SteamUpdateBranch,
    SteamUpdateConfig,
    normalise_activity_provider_ids,
    normalise_app_title_font,
    normalise_optional_channel_id,
    normalise_optional_channel_ids,
    normalise_optional_friendly_name,
    normalise_optional_text,
)
from apps._steam import (
    cached_steam_update_branches,
    merge_steam_update_branches,
    steam_update_preset_for_scope,
)
from chat_hub import ChatEndpoint, ChatEndpointId, ChatHub
from config import Activity_Manager, Activity_Provider
from relay_notices import (
    AppLifecycleNotice,
    AppLifecycleState,
    RelayNotice,
    RelayNoticeSeverity,
    RelayNoticeSource,
    notice_embed_spec,
)

log = logging.getLogger(__name__)

type JsonObject = dict[str, object]
type JsonMapping = Mapping[str, object]
type ManagedApp = App[App_Config]
type ManagedAppType = type[ManagedApp]


class AppStartBlockerKind(enum.StrEnum):
    ALREADY_RUNNING = "already_running"
    SAME_SCOPE = "same_scope"
    CPU_POINTS = "cpu_points"
    RAM_POINTS = "ram_points"


@dataclass(frozen=True, slots=True)
class AppStartBlocker:
    kind: AppStartBlockerKind
    message: str
    blocking_app_name: str | None = None
    blocking_app_friendly: str | None = None
    required_points: int | None = None
    available_points: int | None = None


@dataclass(frozen=True, slots=True)
class AppInstanceCreateRequest:
    scope: str
    instance_key: str
    friendly_name: str
    subfolder: str
    port: int | None = None
    server_log_file: str | None = None
    admin_password: str | None = None
    steam_branch: str | None = None
    initial_version: AppVersion | None = None


class AppInstallInput(enum.StrEnum):
    """A typed app-specific value requested by an installation recipe."""

    ADMIN_PASSWORD = "admin_password"

    @property
    def label(self) -> str:
        if self is AppInstallInput.ADMIN_PASSWORD:
            return "Admin password"
        raise ValueError(f"Unsupported app installation input: {self}")

    @property
    def help_text(self) -> str:
        if self is AppInstallInput.ADMIN_PASSWORD:
            return "For a new Satisfactory server, use this password when claiming it in the game client."
        raise ValueError(f"Unsupported app installation input: {self}")

    @property
    def is_secret(self) -> bool:
        return self is AppInstallInput.ADMIN_PASSWORD


@dataclass(frozen=True, slots=True)
class AppSteamInstallRecipe:
    """A supported SteamCMD-backed app installation target."""

    scope: str
    label: str
    default_port: int | None
    steam_update: SteamUpdateConfig
    inputs: tuple[AppInstallInput, ...] = ()


@dataclass(frozen=True, slots=True)
class AppInstanceCreationPlan:
    """Validated immutable inputs for a new managed app instance."""

    scope: str
    instance_key: str
    friendly_name: str
    subfolder: Path
    directory: Path
    server_log_file: str | None
    admin_password: str | None
    steam_branch: str | None
    scope_path: Path
    instances_path: Path


@dataclass(frozen=True, slots=True)
class AppDetailsUpdate:
    friendly_name: str
    notes: str | None
    lifecycle_notice_started: bool
    lifecycle_notice_stopped: bool
    lifecycle_notice_crashed: bool
    running_cpu_points: int
    running_ram_points: int
    startup_cpu_points: int | None
    startup_ram_points: int | None
    relay_notice_player_session: bool | None = None
    relay_notice_player_death: bool | None = None
    relay_notice_progress: bool | None = None
    relay_advancements_enabled: bool | None = None
    factorio_chat_relay_use_shout: bool | None = None
    rcon_requires_online_players: bool | None = None
    disabled_activity_provider_ids: tuple[str, ...] | None = None
    title_font_preset: str | None = None
    steam_update_enabled: bool | None = None
    steam_update_selected_branch: str | None = None


@dataclass(frozen=True, slots=True)
class AppInstanceTemplate:
    label: str | None = None
    install_inputs: tuple[AppInstallInput, ...] = ()
    mods_dir: str | None = None
    client_mods_dir: str | None = None
    client_overrides_dir: str | None = None
    server_log_file: str | None = None
    join_port: int | None = None
    api_host: str | None = None
    api_port: int | None = None
    steam_update: SteamUpdateConfig | None = None
    steam_update_factory: Callable[[], SteamUpdateConfig] | None = None

    def __post_init__(self) -> None:
        if self.steam_update is not None and self.steam_update_factory is not None:
            raise ValueError("App instance templates may define either a Steam config or a Steam config factory.")

    def resolved_steam_update(self) -> SteamUpdateConfig | None:
        """Resolve this template's Steam configuration on demand."""

        if self.steam_update is not None:
            return self.steam_update
        if self.steam_update_factory is None:
            return None
        return self.steam_update_factory()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.mods_dir is not None:
            payload["mods_dir"] = self.mods_dir
        if self.client_mods_dir is not None:
            payload["client_mods_dir"] = self.client_mods_dir
        if self.client_overrides_dir is not None:
            payload["client_overrides_dir"] = self.client_overrides_dir
        if self.server_log_file is not None:
            payload["server_log_file"] = self.server_log_file
        if self.join_port is not None:
            payload["join_port"] = self.join_port
        if self.api_host is not None:
            payload["api_host"] = self.api_host
        if self.api_port is not None:
            payload["api_port"] = self.api_port
        steam_update = self.resolved_steam_update()
        if steam_update is not None:
            payload["steam_update"] = steam_update.model_dump(mode="json", exclude_none=True)
        return payload


@dataclass(frozen=True, slots=True)
class StartupDisabledAppNotice:
    app_name: str
    reason: str

    def format_line(self) -> str:
        return f"Auto-disabled: {self.app_name} ({self.reason})"


def _required_scope_steam_update_template(scope: str) -> SteamUpdateConfig:
    preset = steam_update_preset_for_scope(scope)
    if preset is None:
        raise ValueError(f"Steam update preset is not defined for scope {scope!r}.")
    return preset.build_config()


_SCOPE_INSTANCE_TEMPLATES: dict[str, AppInstanceTemplate] = {
    "beammp": AppInstanceTemplate(
        mods_dir="{WD}/Resources/Client",
        client_mods_dir="{WD}/Resources/Client",
        client_overrides_dir="{WD}/client-overrides",
        server_log_file="{WD}/Server.log",
        join_port=30814,
    ),
    "ets": AppInstanceTemplate(
        server_log_file="{WD}/home_data/Euro Truck Simulator 2/server.log.txt",
        join_port=27015,
    ),
    "factorio": AppInstanceTemplate(
        mods_dir="{WD}/mods",
        client_mods_dir="{WD}/mods",
        client_overrides_dir="{WD}/client-overrides",
        server_log_file="{WD}/factorio-current.log",
        join_port=34197,
    ),
    "minecraft": AppInstanceTemplate(
        mods_dir="{WD}/mods",
        client_mods_dir="{WD}/mods",
        client_overrides_dir="{WD}/client-overrides",
        join_port=25565,
    ),
    "satisfactory": AppInstanceTemplate(
        label="Satisfactory",
        install_inputs=(AppInstallInput.ADMIN_PASSWORD,),
        join_port=7777,
        api_host="127.0.0.1",
        steam_update_factory=lambda: _required_scope_steam_update_template("satisfactory"),
    ),
    "sevendays": AppInstanceTemplate(
        label="7 Days to Die",
        mods_dir="{WD}/Mods",
        client_mods_dir="{WD}/Mods",
        client_overrides_dir="{WD}/client-overrides",
        server_log_file="{WD}/server_stdout.log",
        join_port=26900,
        steam_update_factory=lambda: _required_scope_steam_update_template("sevendays"),
    ),
}


def _validate_required_friendly_name(raw: object) -> str:
    friendly_name = normalise_optional_friendly_name(raw)
    if friendly_name is None:
        raise ValueError("Friendly name must not be empty.")
    return friendly_name


def app_scope_from_name(app_name: str) -> str | None:
    scope, separator, _instance_key = app_name.partition("_")
    if not separator or not scope.strip():
        return None
    return scope


def format_enabled_app_dump(apps: Sequence[ManagedApp]) -> str:
    ordered_apps = sorted(apps, key=lambda app: app.name.casefold())
    return "\n".join(f"{app.name}: {app.cfg.enabled_txt}" for app in ordered_apps) + "\n"


class App_Manager(metaclass=config.Singleton):
    _BOT_CONFIGURATION_PATH = config.BOT_CONFIGURATION_PATH
    activity_manager: "Activity_Manager | None" = None
    bot: hikari.GatewayBot | None = None

    def __init__(self):
        self.apps: dict[str, ManagedApp] = {}
        self._lookup: dict[str, str] = {}
        self._managed_shutdown_names: set[str] = set()
        self._pending_start_names: set[str] = set()
        self.default_chat_channels: tuple[hikari.Snowflake, ...] = ()
        self.default_chat_channel: hikari.Snowflake | None = None
        self.default_chat_channel_source = RelayChannelSource.NONE
        self.startup_disabled_instances: list[StartupDisabledAppNotice] = []
        self._update_task: asyncio.Task[None] | None = None
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH

    def _managed_shutdown_name_keys(self) -> set[str]:
        try:
            return self._managed_shutdown_names
        except AttributeError:
            self._managed_shutdown_names = set()
            return self._managed_shutdown_names

    def _pending_start_name_keys(self) -> set[str]:
        try:
            return self._pending_start_names
        except AttributeError:
            self._pending_start_names = set()
            return self._pending_start_names

    async def post_init(self, bot: hikari.GatewayBot, activity_manager: "Activity_Manager"):
        self.bot = bot
        self.activity_manager = activity_manager
        self.activity_manager.set_activity_settings(self.discord_settings().activity)
        self.activity_manager.set_rotation_target_name_provider(self._current_activity_target_name)
        await self.load_apps(bot)
        self._update_task = asyncio.create_task(self.monitor_apps())

    def running_apps(self) -> tuple[ManagedApp, ...]:
        return tuple(
            sorted(
                (app for app in self._apps_mapping().values() if app.check_running()),
                key=lambda item: item.name.casefold(),
            )
        )

    def running_app_names(self) -> tuple[str, ...]:
        return tuple(app.name for app in self.running_apps())

    def activity_rotation_target(self) -> tuple[ManagedApp, bool] | None:
        running_apps = self.running_apps()
        if not running_apps:
            return None
        activity_manager = self.activity_manager
        if activity_manager is None:
            return (running_apps[0], False)
        app_index, show_alt_text = activity_manager.current_rotation_slot(len(running_apps))
        return (running_apps[app_index], show_alt_text)

    def _current_activity_target_name(self) -> str | None:
        target = self.activity_rotation_target()
        if target is None:
            return None
        return target[0].name

    def starting_apps(self) -> tuple[ManagedApp, ...]:
        pending_names = self._pending_start_name_keys()
        return tuple(
            sorted(
                (app for app in self._apps_mapping().values() if app.name.casefold() in pending_names),
                key=lambda item: item.name.casefold(),
            )
        )

    def _start_admission_apps(self, *, exclude_name: str | None = None) -> tuple[ManagedApp, ...]:
        pending_names = self._pending_start_name_keys()
        excluded_key = exclude_name.casefold() if isinstance(exclude_name, str) else None
        return tuple(
            sorted(
                (
                    app
                    for app in self._apps_mapping().values()
                    if (excluded_key is None or app.name.casefold() != excluded_key)
                    and (app.check_running() or app.name.casefold() in pending_names)
                ),
                key=lambda item: item.name.casefold(),
            )
        )

    def _apps_mapping(self) -> dict[str, ManagedApp]:
        try:
            return self.apps
        except AttributeError:
            self.apps = {}
            return self.apps

    def running_scope_conflict(self, app: ManagedApp) -> ManagedApp | None:
        for active_app in self._start_admission_apps(exclude_name=app.name):
            if active_app.scope == app.scope:
                return active_app
        return None

    @staticmethod
    def _app_running_points(app: ManagedApp) -> config.ResourcePointSet:
        return app.cfg.resource_points.running

    @staticmethod
    def _app_startup_points(app: ManagedApp) -> config.ResourcePointSet:
        return app.cfg.resource_points.startup_points

    def _active_resource_point_usage(self, *, exclude_name: str | None = None) -> config.ResourcePointSet:
        pending_names = self._pending_start_name_keys()
        cpu_points = 0
        ram_points = 0
        for app in self._start_admission_apps(exclude_name=exclude_name):
            points = self._app_startup_points(app) if app.name.casefold() in pending_names else self._app_running_points(app)
            cpu_points += points.cpu_points
            ram_points += points.ram_points
        return config.ResourcePointSet(cpu_points=cpu_points, ram_points=ram_points)

    def active_resource_point_usage(self) -> config.ResourcePointSet:
        return self._active_resource_point_usage()

    def app_installer_settings(self) -> config.AppInstallerSettings:
        return self._load_bot_configuration().app_installer

    def node_capacity(self) -> config.NodeCapacityProfile:
        return self._load_bot_configuration().node_capacity

    def discord_settings(self) -> config.DiscordSettings:
        return self._load_bot_configuration().discord_settings

    def capacity_conflict(self, app: ManagedApp) -> AppStartBlocker | None:
        capacity = self.node_capacity()
        active_points = self._active_resource_point_usage(exclude_name=app.name)
        required_points = self._app_startup_points(app)
        available_cpu_points = capacity.cpu_points_available - active_points.cpu_points
        if required_points.cpu_points > available_cpu_points:
            return AppStartBlocker(
                kind=AppStartBlockerKind.CPU_POINTS,
                message=(
                    f"Cannot start {app.friendly}; node `{config.NODE_NAME}` has insufficient CPU points "
                    f"(required {required_points.cpu_points}, available {max(0, available_cpu_points)})."
                ),
                required_points=required_points.cpu_points,
                available_points=max(0, available_cpu_points),
            )
        available_ram_points = capacity.ram_points_available - active_points.ram_points
        if required_points.ram_points > available_ram_points:
            return AppStartBlocker(
                kind=AppStartBlockerKind.RAM_POINTS,
                message=(
                    f"Cannot start {app.friendly}; node `{config.NODE_NAME}` has insufficient RAM points "
                    f"(required {required_points.ram_points}, available {max(0, available_ram_points)})."
                ),
                required_points=required_points.ram_points,
                available_points=max(0, available_ram_points),
            )
        return None

    def start_blocker(
        self,
        app: ManagedApp,
        *,
        include_current_activity: bool = True,
    ) -> AppStartBlocker | None:
        pending_names = self._pending_start_name_keys()
        if include_current_activity and (app.check_running() or app.name.casefold() in pending_names):
            return AppStartBlocker(
                kind=AppStartBlockerKind.ALREADY_RUNNING,
                message=f"{app.friendly} is already running.",
            )
        if blocked_by := self.running_scope_conflict(app):
            return AppStartBlocker(
                kind=AppStartBlockerKind.SAME_SCOPE,
                message=(
                    f"Cannot start {app.friendly}; {blocked_by.friendly} is already running for scope `{app.scope}`."
                ),
                blocking_app_name=blocked_by.name,
                blocking_app_friendly=blocked_by.friendly,
            )
        return self.capacity_conflict(app)

    @staticmethod
    def _should_monitor_app(app: ManagedApp) -> bool:
        return app.lifecycle_started_at is not None or app.process is not None or app.is_started

    async def monitor_apps(self):
        while True:
            for app in tuple(self.apps.values()):
                if not self._should_monitor_app(app):
                    continue
                if not app.check_running():
                    await self._handle_inactive_app(app)
            await asyncio.sleep(1)

    async def _handle_inactive_app(self, app: ManagedApp) -> None:
        started_at = app.lifecycle_started_at
        uptime = datetime.now(timezone.utc) - started_at if started_at is not None else None
        was_manager_initiated_shutdown = app.name.casefold() in self._managed_shutdown_name_keys()
        try:
            await app.handle_unexpected_stop()
        except Exception:
            log.exception("Failed to finalise inactive app: %s", app.name)
        runtime_fault = app.runtime_fault
        if runtime_fault is not None and runtime_fault.kind is AppRuntimeFaultKind.CRASH:
            self._notify_app_crash(app, summary=runtime_fault.summary, uptime=uptime)
        elif started_at is not None and not was_manager_initiated_shutdown:
            self._notify_app_lifecycle(app, started=False, uptime=uptime)
        app.lifecycle_started_at = None

    def dump_enabled(self) -> int:
        config.ENABLED_DUMP_FILE.parent.mkdir(exist_ok=True, parents=True)
        return config.ENABLED_DUMP_FILE.write_text(
            format_enabled_app_dump(tuple(self.apps.values())),
            config.STR_ENCODE,
        )

    async def load_apps(self, bot: hikari.GatewayBot):
        apps: dict[str, ManagedApp] = {}
        base_path = Path("apps")
        self.startup_disabled_instances = []
        for app in self.apps.values():
            DC_Relay.unregister_app(app)
        self._lookup = {}
        self._refresh_default_chat_channel()

        for entry in base_path.iterdir():
            entry = entry.resolve()
            if not entry.is_dir() or entry.name.startswith("_"):
                continue

            instances_path = entry / "instances.json"
            if not instances_path.exists():
                continue

            raw = self._read_json_object(instances_path)
            app_cls, cfg_cls = self._load_scope_types(entry.name)
            for instance_name, raw_cfg in raw.items():
                try:
                    instance_payload = self._as_json_mapping(raw_cfg)
                    if instance_payload is None:
                        raise ValueError(f"{instances_path} instance {instance_name!r} must be a JSON object")
                    cfg = self._build_app_config(
                        scope=entry.name,
                        scope_path=entry,
                        cfg_cls=cfg_cls,
                        instance_key=instance_name,
                        raw_cfg=instance_payload,
                    )
                except Exception:
                    log.exception(f"Validate {entry.name}_{instance_name}")
                    continue

                if not cfg.directory.exists():
                    self._disable_missing_instance(
                        instances_path=instances_path,
                        raw=raw,
                        cfg=cfg,
                        reason=f"directory missing: {cfg.directory}",
                    )
                    continue

                try:
                    app = self._instantiate_app(
                        bot=bot,
                        app_cls=app_cls,
                        cfg=cfg,
                    )
                    self._sync_app_instance_config(app)
                    apps[app.name] = app
                    log.info(f"Loaded: {app.name}")
                except FileNotFoundError as xcp:
                    self._disable_missing_instance(
                        instances_path=instances_path,
                        raw=raw,
                        cfg=cfg,
                        reason=str(xcp),
                    )
                except Exception:
                    log.exception(f"Instantiate {instance_name}")

        await asyncio.gather(*(app.post_init() for app in apps.values()))
        for app in apps.values():
            DC_Relay.bind_app_channel(app)

        self.apps = apps
        self.dump_enabled()
        for name, app in self.apps.items():
            self._register_lookup_aliases(name, app)
        if self.startup_disabled_instances:
            log.warning(
                "Auto-disabled %s app instance(s) during startup: %s",
                len(self.startup_disabled_instances),
                "; ".join(notice.format_line() for notice in self.startup_disabled_instances),
            )

    def startup_disabled_notice_lines(self) -> tuple[str, ...]:
        return tuple(notice.format_line() for notice in self.startup_disabled_instances)

    async def launch(self, name: str | ManagedApp):
        if isinstance(name, App):
            app = name
        else:
            app = self.get(name)
        if not app.cfg.enabled:
            raise LookupError("App Not Enabled")
        if blocker := self.start_blocker(app):
            raise RuntimeError(blocker.message)
        if app.settings:
            app.settings.app.save()
        pending_names = self._pending_start_name_keys()
        pending_names.add(app.name.casefold())
        try:
            await app.start()
            app.lifecycle_started_at = datetime.now(timezone.utc)
            app.verify_published_client_pack()
            self._notify_app_lifecycle(app, started=True)
        except Exception:
            runtime_fault = app.runtime_fault
            if not app.check_running() or runtime_fault is not None:
                await self._handle_inactive_app(app)
            raise
        finally:
            pending_names.discard(app.name.casefold())

    async def _shutdown_apps(
        self,
        *,
        name: str | None,
        action_label: str,
        action_present_participle: str,
        shutdown: Callable[[ManagedApp], Awaitable[bool | None]],
        allow_named_target_when_inactive: bool = False,
    ) -> set[str]:
        async def timed_shutdown(app: ManagedApp, *, allow_inactive_target: bool = False):
            if not app.check_running():
                if not allow_inactive_target:
                    log.info(f"{app.name} not running; skipping.")
                    return (app.name, 0.0, "Skipped", "Not running", None)
            t0 = time.perf_counter()
            started_at = app.lifecycle_started_at
            managed_shutdown_names = self._managed_shutdown_name_keys()
            managed_shutdown_names.add(app.name.casefold())
            try:
                result = await shutdown(app) or None
                elapsed = time.perf_counter() - t0
                uptime = None
                if started_at is not None:
                    uptime = datetime.now(timezone.utc) - started_at
                app.lifecycle_started_at = None
                return (app.name, elapsed, "Success", result, uptime)
            except Exception as xcp:
                elapsed = time.perf_counter() - t0
                log.exception(f"Failed to {action_label} {app.name}: {xcp}")
                return (app.name, elapsed, "Error", str(xcp), None)
            finally:
                managed_shutdown_names.discard(app.name.casefold())

        if name:
            try:
                app = self.get(name)
            except ValueError:
                log.warning(f"Tried to {action_label} unknown app: {name}")
                raise ProcessLookupError("Unknown App")
            result = await timed_shutdown(app, allow_inactive_target=allow_named_target_when_inactive)
            status = f"{result[0]}: {result[2]} in {result[1]:.2f}s"
            log.info(status)
            if result[2] == "Success":
                self._notify_app_lifecycle(app, started=False, uptime=result[4])
            return {
                result[0].title(),
            }

        log.info(f"{action_present_participle} all apps...")
        running_apps = self.running_apps()
        if not running_apps:
            return set()
        results = await asyncio.gather(*(timed_shutdown(app) for app in running_apps))

        names: set[str] = set()
        for app_name, secs, status, _, uptime in results:
            if status != "Skipped":
                names.add(app_name)
                log.info(f" - {app_name}: {status} in {secs:.2f}s")
            if status == "Success":
                app = self.apps[app_name]
                self._notify_app_lifecycle(app, started=False, uptime=uptime)

        log.info(f"All apps finished {action_label}.")
        return names

    async def end(self, name: str | None = None) -> set[str]:
        async def _stop(app: App) -> bool | None:
            return await app.stop()

        return await self._shutdown_apps(
            name=name,
            action_label="stop",
            action_present_participle="Stopping",
            shutdown=_stop,
        )

    async def kill(self, name: str | None = None) -> set[str]:
        async def _kill(app: App) -> bool | None:
            return await app.kill()

        return await self._shutdown_apps(
            name=name,
            action_label="kill",
            action_present_participle="Killing",
            shutdown=_kill,
            allow_named_target_when_inactive=True,
        )

    def _notify_app_lifecycle(
        self,
        app: ManagedApp,
        *,
        started: bool,
        uptime: timedelta | None = None,
    ) -> None:
        if not self._app_can_emit_lifecycle_notice(app):
            return
        if started and not app.cfg.lifecycle_notice_started:
            return
        if not started and not app.cfg.lifecycle_notice_stopped:
            return
        uptime_seconds = None if uptime is None else max(0, round(uptime.total_seconds()))
        notice = AppLifecycleNotice(
            state=AppLifecycleState.STARTED if started else AppLifecycleState.STOPPED,
            source=RelayNoticeSource.APP_MANAGER,
            join_address=app.cfg.join_display_address if started else None,
            detail_lines=app.lifecycle_relay_description_lines(started=started, uptime=uptime),
            uptime_seconds=uptime_seconds,
        )
        embed_spec = notice_embed_spec(notice, app_name=app.friendly, author_name="System")
        relay_embed = (
            None
            if embed_spec is None
            else RelayEmbedPayload(
                title=embed_spec.title,
                description=embed_spec.description,
                color=app.manage_embed_color,
            )
        )
        DC_Relay.add(
            DC_Bound(
                app,
                "Started" if started else "Stopped",
                "System",
                relay_embed=relay_embed,
                notice=notice,
            )
        )

    def _notify_app_crash(
        self,
        app: ManagedApp,
        *,
        summary: str | None,
        uptime: timedelta | None = None,
    ) -> None:
        if not self._app_can_emit_lifecycle_notice(app):
            return
        if not app.cfg.lifecycle_notice_crashed:
            return
        uptime_seconds = None if uptime is None else max(0, round(uptime.total_seconds()))
        notice = AppLifecycleNotice(
            state=AppLifecycleState.CRASHED,
            source=RelayNoticeSource.APP_MANAGER,
            severity=RelayNoticeSeverity.ERROR,
            uptime_seconds=uptime_seconds,
            summary=summary,
        )
        embed_spec = notice_embed_spec(notice, app_name=app.friendly, author_name="System")
        relay_embed = (
            None
            if embed_spec is None
            else RelayEmbedPayload(
                title=embed_spec.title,
                description=embed_spec.description,
                color=app.manage_embed_color,
            )
        )
        DC_Relay.add(
            DC_Bound(
                app,
                "Crashed",
                "System",
                relay_embed=relay_embed,
                notice=notice,
            )
        )

    @staticmethod
    def _app_can_emit_lifecycle_notice(app: ManagedApp) -> bool:
        return bool(app.chat_channel or app.chat_channels or app.supports_chat_relay)

    async def notify_running_app_relays(
        self,
        content: str,
        *,
        player: str = "System",
        notice: RelayNotice | None = None,
    ) -> int:
        if self.bot is None:
            log.warning("Skipping app relay notice because the manager bot is unavailable.")
            return 0

        system_channel = hikari.TextableChannel(app=self.bot, id=hikari.Snowflake(0), name="SYSTEM", type=1)
        sent_count = 0
        for app in sorted(self.apps.values(), key=lambda item: item.name.casefold()):
            if not app.supports_relay_system_notices or not app._running or app.am_receiver is None:
                continue
            relay_message = App_Bound(system_channel, content, player, notice=notice)
            relay_message.app = app
            try:
                await app.am_receiver.send(relay_message)
            except Exception:
                log.exception("Failed to send relay system notice to %s", app.name)
                continue
            sent_count += 1
        return sent_count

    def toggle(self, name: str, state: bool):
        name = name.lower()
        app = self.get(name)
        app.cfg.enabled = state
        self._set_instance_enabled(
            instances_path=app.file_instances,
            instance_key=app.cfg.instance_key,
            enabled=state,
        )
        self.dump_enabled()

    def update_app_details(self, name: str | ManagedApp, details: AppDetailsUpdate) -> str:
        app = name if isinstance(name, App) else self.get(name)
        previous_friendly_name: str = app.friendly
        previous_steam_update = app.cfg.steam_update
        previous_relay_advancements = app.relay_advancements_enabled
        previous_player_session_notice = app.relay_notice_player_session_enabled
        previous_player_death_notice = app.relay_notice_player_death_enabled
        previous_progress_notice = app.relay_notice_progress_enabled
        previous_factorio_chat_relay_use_shout = getattr(app.cfg, "factorio_chat_relay_use_shout", True)
        previous_rcon_requires_online_players = app.rcon_requires_online_players_enabled
        previous_disabled_activity_provider_ids = app.disabled_activity_provider_ids
        next_friendly_name = _validate_required_friendly_name(details.friendly_name)
        next_title_font_preset = (
            app.cfg.title_font_preset
            if details.title_font_preset is None
            else normalise_app_title_font(details.title_font_preset)
        )
        next_notes: str | None = normalise_optional_text(details.notes)
        next_steam_update = self._resolve_next_steam_update_config(app=app, details=details)
        next_relay_advancements = self._resolve_next_relay_advancements_enabled(app=app, details=details)
        next_player_session_notice = self._resolve_next_relay_notice_player_session(app=app, details=details)
        next_player_death_notice = self._resolve_next_relay_notice_player_death(app=app, details=details)
        next_progress_notice = self._resolve_next_relay_notice_progress(app=app, details=details)
        next_factorio_chat_relay_use_shout = self._resolve_next_factorio_chat_relay_use_shout(app=app, details=details)
        next_rcon_requires_online_players = self._resolve_next_rcon_requires_online_players(app=app, details=details)
        next_disabled_activity_provider_ids = self._resolve_next_disabled_activity_provider_ids(app=app, details=details)
        self._validate_steam_update_change_allowed(
            app=app,
            previous_steam_update=previous_steam_update,
            next_steam_update=next_steam_update,
        )
        running_points = config.ResourcePointSet(
            cpu_points=details.running_cpu_points,
            ram_points=details.running_ram_points,
        )
        startup_points: config.ResourcePointSet | None
        startup_cpu_points = (
            running_points.cpu_points if details.startup_cpu_points is None else details.startup_cpu_points
        )
        startup_ram_points = (
            running_points.ram_points if details.startup_ram_points is None else details.startup_ram_points
        )
        if (
            details.startup_cpu_points is None
            and details.startup_ram_points is None
        ) or (
            startup_cpu_points == running_points.cpu_points
            and startup_ram_points == running_points.ram_points
        ):
            startup_points = None
        else:
            startup_points = config.ResourcePointSet(
                cpu_points=startup_cpu_points,
                ram_points=startup_ram_points,
            )
        if (
            previous_friendly_name == next_friendly_name
            and app.cfg.title_font_preset == next_title_font_preset
            and app.cfg.notes == next_notes
            and previous_steam_update == next_steam_update
            and previous_relay_advancements == next_relay_advancements
            and previous_player_session_notice == next_player_session_notice
            and previous_player_death_notice == next_player_death_notice
            and previous_progress_notice == next_progress_notice
            and previous_factorio_chat_relay_use_shout == next_factorio_chat_relay_use_shout
            and previous_rcon_requires_online_players == next_rcon_requires_online_players
            and previous_disabled_activity_provider_ids == next_disabled_activity_provider_ids
        ):
            if (
                app.cfg.lifecycle_notice_started is details.lifecycle_notice_started
                and app.cfg.lifecycle_notice_stopped is details.lifecycle_notice_stopped
                and app.cfg.lifecycle_notice_crashed is details.lifecycle_notice_crashed
                and app.cfg.resource_points.running == running_points
                and app.cfg.resource_points.startup == startup_points
            ):
                return app.friendly
        lookup_conflict = self._friendly_lookup_conflict(app=app, friendly_name=next_friendly_name)
        if lookup_conflict is not None:
            raise ValueError(f"Friendly name conflicts with existing app alias: {lookup_conflict}.")
        instances_path = app.file_instances
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(app.cfg.instance_key),
            instances_path=instances_path,
            instance_key=app.cfg.instance_key,
        )
        next_payload = dict(instance_payload)
        next_payload["friendly_name"] = next_friendly_name
        next_payload["title_font_preset"] = next_title_font_preset
        next_payload["notes"] = next_notes
        next_payload["lifecycle_notice_started"] = details.lifecycle_notice_started
        next_payload["lifecycle_notice_stopped"] = details.lifecycle_notice_stopped
        next_payload["lifecycle_notice_crashed"] = details.lifecycle_notice_crashed
        if next_player_session_notice is not None:
            next_payload["relay_notice_player_session"] = next_player_session_notice
        next_payload.pop("relay_notice_player_joined", None)
        next_payload.pop("relay_notice_player_left", None)
        if next_player_death_notice is not None:
            next_payload["relay_notice_player_death"] = next_player_death_notice
        if next_progress_notice is not None:
            next_payload["relay_notice_progress"] = next_progress_notice
        if next_relay_advancements is not None:
            next_payload["relay_advancements"] = next_relay_advancements
        if app.scope == "factorio":
            next_payload["factorio_chat_relay_use_shout"] = next_factorio_chat_relay_use_shout
        if next_rcon_requires_online_players is not None:
            if next_rcon_requires_online_players == app.rcon_requires_online_players_default:
                next_payload.pop("rcon_requires_online_players", None)
            else:
                next_payload["rcon_requires_online_players"] = next_rcon_requires_online_players
        else:
            next_payload.pop("rcon_requires_online_players", None)
        if next_disabled_activity_provider_ids:
            next_payload["disabled_activity_provider_ids"] = list(next_disabled_activity_provider_ids)
        else:
            next_payload.pop("disabled_activity_provider_ids", None)
        next_resource_points_payload: dict[str, object] = {"running": running_points.model_dump(mode="json")}
        if startup_points is not None:
            next_resource_points_payload["startup"] = startup_points.model_dump(mode="json")
        next_payload["resource_points"] = next_resource_points_payload
        if next_steam_update is None:
            next_payload.pop("steam_update", None)
        else:
            next_payload["steam_update"] = next_steam_update.model_dump(mode="json", exclude_none=True)
        raw[app.cfg.instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        app.cfg.friendly_name = next_friendly_name
        app.cfg.title_font_preset = next_title_font_preset
        app.cfg.notes = next_notes
        app.cfg.lifecycle_notice_started = details.lifecycle_notice_started
        app.cfg.lifecycle_notice_stopped = details.lifecycle_notice_stopped
        app.cfg.lifecycle_notice_crashed = details.lifecycle_notice_crashed
        app.cfg.factorio_chat_relay_use_shout = next_factorio_chat_relay_use_shout
        app.cfg.resource_points.running = running_points
        app.cfg.resource_points.startup = startup_points
        app.cfg.steam_update = next_steam_update
        if next_rcon_requires_online_players is not None:
            app.apply_rcon_requires_online_players_enabled(next_rcon_requires_online_players)
        app.apply_disabled_activity_provider_ids(next_disabled_activity_provider_ids)
        if next_player_session_notice is not None:
            app.apply_relay_notice_player_session_enabled(next_player_session_notice)
        if next_player_death_notice is not None:
            app.apply_relay_notice_player_death_enabled(next_player_death_notice)
        if next_progress_notice is not None:
            app.apply_relay_notice_progress_enabled(next_progress_notice)
        if next_relay_advancements is not None:
            app.apply_relay_advancements_enabled(next_relay_advancements)
        app.friendly = next_friendly_name
        self._sync_app_steam_updater(
            app=app,
            previous_steam_update=previous_steam_update,
            next_steam_update=next_steam_update,
        )
        self._replace_friendly_lookup_aliases(app=app, previous_friendly_name=previous_friendly_name)
        ChatHub().bind(app.name, ChatEndpoint(ChatEndpointId.app(app.name), next_friendly_name))
        log.info(
            "Updated app details: app=%s friendly_name=%s title_font_preset=%s",
            app.name,
            next_friendly_name,
            next_title_font_preset,
        )
        return next_friendly_name

    @staticmethod
    def _resolve_next_disabled_activity_provider_ids(*, app: ManagedApp, details: AppDetailsUpdate) -> tuple[str, ...]:
        if details.disabled_activity_provider_ids is None:
            return app.disabled_activity_provider_ids
        requested_provider_ids = normalise_activity_provider_ids(details.disabled_activity_provider_ids)
        known_provider_ids = {entry.provider_id.casefold(): entry.provider_id for entry in app.activity_provider_entries}
        unknown_provider_ids = [
            provider_id for provider_id in requested_provider_ids if provider_id.casefold() not in known_provider_ids
        ]
        if unknown_provider_ids:
            raise ValueError(f"Unknown app activity providers: {', '.join(unknown_provider_ids)}.")
        return tuple(known_provider_ids[provider_id.casefold()] for provider_id in requested_provider_ids)

    @staticmethod
    def _resolve_next_steam_update_config(*, app: ManagedApp, details: AppDetailsUpdate) -> SteamUpdateConfig | None:
        current_steam_update = app.cfg.steam_update
        if details.steam_update_enabled is None:
            return current_steam_update
        if not details.steam_update_enabled:
            return None
        selected_branch = details.steam_update_selected_branch
        if current_steam_update is not None:
            if selected_branch is None:
                return current_steam_update
            return current_steam_update.with_selected_branch(selected_branch, add_if_missing=True)
        preset = steam_update_preset_for_scope(app.scope)
        if preset is None:
            raise ValueError(f"{app.friendly} does not support Steam update configuration.")
        return preset.build_config(selected_branch=selected_branch)

    @staticmethod
    def _resolve_next_relay_advancements_enabled(*, app: ManagedApp, details: AppDetailsUpdate) -> bool | None:
        current_relay_advancements = app.relay_advancements_enabled
        if details.relay_advancements_enabled is None:
            return current_relay_advancements
        if not app.supports_relay_advancements:
            raise ValueError(f"{app.friendly} does not support {app.relay_advancement_term.lower()} relay.")
        return details.relay_advancements_enabled

    @staticmethod
    def _resolve_next_relay_notice_player_session(*, app: ManagedApp, details: AppDetailsUpdate) -> bool | None:
        current_notice = app.relay_notice_player_session_enabled
        if details.relay_notice_player_session is None:
            return current_notice
        if current_notice is None:
            raise ValueError(f"{app.friendly} does not support player session notices.")
        return details.relay_notice_player_session

    @staticmethod
    def _resolve_next_relay_notice_player_death(*, app: ManagedApp, details: AppDetailsUpdate) -> bool | None:
        current_notice = app.relay_notice_player_death_enabled
        if details.relay_notice_player_death is None:
            return current_notice
        if current_notice is None:
            raise ValueError(f"{app.friendly} does not support death notices.")
        return details.relay_notice_player_death

    @staticmethod
    def _resolve_next_relay_notice_progress(*, app: ManagedApp, details: AppDetailsUpdate) -> bool | None:
        current_notice = app.relay_notice_progress_enabled
        if details.relay_notice_progress is None:
            return current_notice
        if current_notice is None:
            raise ValueError(f"{app.friendly} does not support {app.relay_progress_notice_term.lower()} notices.")
        return details.relay_notice_progress

    @staticmethod
    def _resolve_next_factorio_chat_relay_use_shout(*, app: ManagedApp, details: AppDetailsUpdate) -> bool:
        current_value = getattr(app.cfg, "factorio_chat_relay_use_shout", True)
        if details.factorio_chat_relay_use_shout is None:
            return current_value
        if app.scope != "factorio":
            raise ValueError(f"{app.friendly} does not support Factorio chat relay routing.")
        return details.factorio_chat_relay_use_shout

    @staticmethod
    def _resolve_next_rcon_requires_online_players(*, app: ManagedApp, details: AppDetailsUpdate) -> bool | None:
        current_value = app.rcon_requires_online_players_enabled
        if details.rcon_requires_online_players is None:
            return current_value
        if current_value is None:
            raise ValueError(f"{app.friendly} does not support RCON command gating.")
        return details.rcon_requires_online_players

    @staticmethod
    def _steam_update_runtime_rebuild_required(
        *,
        previous_steam_update: SteamUpdateConfig | None,
        next_steam_update: SteamUpdateConfig | None,
    ) -> bool:
        if previous_steam_update is None or next_steam_update is None:
            return previous_steam_update != next_steam_update
        return (
            previous_steam_update.app_id != next_steam_update.app_id
            or previous_steam_update.steamcmd_executable != next_steam_update.steamcmd_executable
            or previous_steam_update.login != next_steam_update.login
            or previous_steam_update.branches != next_steam_update.branches
        )

    @staticmethod
    def _validate_steam_update_change_allowed(
        *,
        app: ManagedApp,
        previous_steam_update: SteamUpdateConfig | None,
        next_steam_update: SteamUpdateConfig | None,
    ) -> None:
        if app.updater is None:
            return
        current_status = app.updater.status()
        if current_status is None or not current_status.running:
            return
        if previous_steam_update != next_steam_update:
            raise ValueError("Cannot change Steam update configuration while an update is running.")

    def _sync_app_steam_updater(
        self,
        *,
        app: ManagedApp,
        previous_steam_update: SteamUpdateConfig | None,
        next_steam_update: SteamUpdateConfig | None,
    ) -> None:
        if steam_update_preset_for_scope(app.scope) is None:
            return
        if next_steam_update is None:
            app.updater = None
            return
        if app.updater is not None and not self._steam_update_runtime_rebuild_required(
            previous_steam_update=previous_steam_update,
            next_steam_update=next_steam_update,
        ):
            return
        from apps._updater import SteamCmd_Update_Manager

        app.updater = SteamCmd_Update_Manager(app)

    def set_app_friendly_name(self, name: str | ManagedApp, friendly_name: str) -> str:
        app = name if isinstance(name, App) else self.get(name)
        return self.update_app_details(
            app,
            AppDetailsUpdate(
                friendly_name=friendly_name,
                title_font_preset=app.cfg.title_font_preset,
                notes=app.cfg.notes,
                lifecycle_notice_started=app.cfg.lifecycle_notice_started,
                lifecycle_notice_stopped=app.cfg.lifecycle_notice_stopped,
                lifecycle_notice_crashed=app.cfg.lifecycle_notice_crashed,
                running_cpu_points=app.cfg.resource_points.running.cpu_points,
                running_ram_points=app.cfg.resource_points.running.ram_points,
                startup_cpu_points=(
                    None if app.cfg.resource_points.startup is None else app.cfg.resource_points.startup.cpu_points
                ),
                startup_ram_points=(
                    None if app.cfg.resource_points.startup is None else app.cfg.resource_points.startup.ram_points
                ),
            ),
        )

    def set_node_capacity(self, capacity: config.NodeCapacityProfile) -> config.NodeCapacityProfile:
        bot_config = self._load_bot_configuration()
        if bot_config.node_capacity == capacity:
            return capacity
        bot_config = bot_config.model_copy(update={"node_capacity": capacity})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return capacity

    def set_app_installer_settings(self, settings: config.AppInstallerSettings) -> config.AppInstallerSettings:
        bot_config = self._load_bot_configuration()
        if bot_config.app_installer == settings:
            return settings
        bot_config = bot_config.model_copy(update={"app_installer": settings})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return settings

    def node_font_sources(self) -> config.NodeFontSourceSettings:
        return self._load_bot_configuration().node_font_sources

    def set_node_font_sources(self, settings: config.NodeFontSourceSettings) -> config.NodeFontSourceSettings:
        bot_config = self._load_bot_configuration()
        if bot_config.node_font_sources == settings:
            return settings
        bot_config = bot_config.model_copy(update={"node_font_sources": settings})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return settings

    def set_discord_settings(self, settings: config.DiscordSettings) -> config.DiscordSettings:
        config.save_discord_settings(self._bot_configuration_path, settings)
        if self.activity_manager is not None:
            self.activity_manager.set_activity_settings(settings.activity)
        return settings

    def clear_app_chat_channel(self, name: str | ManagedApp) -> None:
        self.set_app_chat_channel(name, None)

    def set_app_chat_channel(self, name: str | ManagedApp, channel_id: hikari.Snowflakeish | None) -> None:
        channel_text = normalise_optional_channel_id(channel_id)
        self.set_app_chat_channels(name, (channel_text,) if channel_text is not None else ())

    def set_app_chat_channels(self, name: str | ManagedApp, channel_ids: Sequence[hikari.Snowflakeish | str]) -> None:
        app = name if isinstance(name, App) else self.get(name)
        if not app.supports_chat_relay:
            if not channel_ids:
                self._purge_app_chat_channel_override(app)
                self._clear_app_relay_state(app)
                DC_Relay.bind_app_channel(app)
                return
            raise ValueError(f"{app.friendly} does not support chat relay.")
        instances_path = app.file_instances
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(app.cfg.instance_key),
            instances_path=instances_path,
            instance_key=app.cfg.instance_key,
        )
        next_payload = dict(instance_payload)
        channel_texts = normalise_optional_channel_ids(tuple(channel_ids))
        self._validate_app_chat_channel_overrides(app, channel_texts)
        self._set_chat_channel_payload(next_payload, channel_texts)
        raw[app.cfg.instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        self._apply_relay_channel(app)

    def set_app_relay_advancements_enabled(self, name: str | ManagedApp, enabled: bool) -> None:
        app = name if isinstance(name, App) else self.get(name)
        if not app.supports_relay_advancements:
            raise ValueError(f"{app.friendly} does not support {app.relay_advancement_term.lower()} relay.")
        instances_path = app.file_instances
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(app.cfg.instance_key),
            instances_path=instances_path,
            instance_key=app.cfg.instance_key,
        )
        next_payload = dict(instance_payload)
        next_payload["relay_advancements"] = enabled
        raw[app.cfg.instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        app.apply_relay_advancements_enabled(enabled)

    def clear_default_chat_channel(self) -> None:
        self.set_default_chat_channel(None)

    def set_default_chat_channel(self, channel_id: hikari.Snowflakeish | None) -> None:
        channel_text = normalise_optional_channel_id(channel_id)
        self.set_default_chat_channels((channel_text,) if channel_text is not None else ())

    def set_default_chat_channels(self, channel_ids: Sequence[hikari.Snowflakeish | str]) -> None:
        defaults_path = self._default_config_path
        raw = self._read_json_object(defaults_path)
        channel_texts = normalise_optional_channel_ids(tuple(channel_ids))
        raw.pop("default_chat_channel", None)
        if not channel_texts:
            raw.pop("default_chat_channels", None)
        else:
            raw["default_chat_channels"] = list(channel_texts)
        self._write_json_object(defaults_path, raw)
        self._refresh_default_chat_channel()
        for app in self.apps.values():
            self._apply_relay_channel(app)

    def get(self, name: str) -> ManagedApp:
        if app_name := self._lookup.get(name):
            return self.apps[app_name]
        raise ValueError(f"No such app: {name}")

    def list_create_scopes(self) -> tuple[str, ...]:
        scopes: list[str] = []
        for entry in Path("apps").iterdir():
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            if not self._scope_supports_create(entry):
                continue
            scopes.append(entry.name)
        return tuple(sorted(scopes, key=str.casefold))

    def list_known_scopes(self) -> tuple[str, ...]:
        scopes: set[str] = {app.scope.strip().lower() for app in self.apps.values() if app.scope.strip()}
        scopes.update(scope.strip().lower() for scope in self.list_create_scopes() if scope.strip())
        return tuple(sorted(scopes, key=str.casefold))

    def list_steam_install_recipes(self) -> tuple[AppSteamInstallRecipe, ...]:
        """Return the node's supported SteamCMD installation recipes."""

        creatable_scopes = frozenset(self.list_create_scopes())
        recipes: list[AppSteamInstallRecipe] = []
        for scope, template in _SCOPE_INSTANCE_TEMPLATES.items():
            if scope not in creatable_scopes:
                continue
            steam_update = template.resolved_steam_update()
            if steam_update is None:
                continue
            scope_path = Path("apps") / scope
            instances_path = scope_path / "instances.json"
            instance_payload = self._read_json_object(instances_path)
            _, template_payload = self._resolve_instance_template(
                scope=scope,
                scope_path=scope_path,
                payload=instance_payload,
            )
            recipes.append(
                AppSteamInstallRecipe(
                    scope=scope,
                    label=template.label or scope.replace("_", " ").title(),
                    default_port=template.join_port,
                    steam_update=self._steam_install_config_from_template(
                        scope=scope,
                        default_config=steam_update,
                        template_payload=template_payload,
                    ),
                    inputs=template.install_inputs,
                )
            )
        return tuple(sorted(recipes, key=lambda recipe: recipe.label.casefold()))

    def prepare_instance_creation(self, request: AppInstanceCreateRequest) -> AppInstanceCreationPlan:
        """Validate a new instance before files are provisioned for it."""

        scope = self._validate_scope_name(request.scope)
        instance_key = self._validate_instance_key(request.instance_key)
        friendly_name = _validate_required_friendly_name(request.friendly_name)
        subfolder = self._validate_subfolder(request.subfolder)
        self._validate_optional_port(request.port)
        server_log_file = self._validate_optional_config_path(
            request.server_log_file,
            label="Server log file",
        )
        admin_password = None
        if scope == "satisfactory":
            admin_password = self._validate_required_config_text(
                request.admin_password,
                label="Admin password",
            )

        scope_path = Path("apps") / scope
        if not scope_path.is_dir():
            raise ValueError(f"Unknown app scope: {scope}")

        instances_path = scope_path / "instances.json"
        raw = self._read_json_object(instances_path)
        if instance_key in raw:
            raise ValueError(f"Instance key `{instance_key}` already exists for scope `{scope}`.")
        self._resolve_instance_template(scope=scope, scope_path=scope_path, payload=raw)

        steam_branch = self._validate_steam_install_branch(scope=scope, branch_id=request.steam_branch)
        return AppInstanceCreationPlan(
            scope=scope,
            instance_key=instance_key,
            friendly_name=friendly_name,
            subfolder=subfolder,
            directory=(config.APP_PATH / subfolder).resolve(),
            server_log_file=server_log_file,
            admin_password=admin_password,
            steam_branch=steam_branch,
            scope_path=scope_path,
            instances_path=instances_path,
        )

    def create_instance(self, request: AppInstanceCreateRequest) -> str:
        plan = self.prepare_instance_creation(request)
        raw = self._read_json_object(plan.instances_path)
        if plan.instance_key in raw:
            raise ValueError(f"Instance key `{plan.instance_key}` already exists for scope `{plan.scope}`.")

        template_key, template_payload = self._resolve_instance_template(
            scope=plan.scope,
            scope_path=plan.scope_path,
            payload=raw,
        )
        next_payload = dict(template_payload)
        next_payload["friendly_name"] = plan.friendly_name
        next_payload["directory"] = f"{{APPS}}/{plan.subfolder.as_posix()}"
        join_port = request.port
        if join_port is None:
            builtin_template = _SCOPE_INSTANCE_TEMPLATES.get(plan.scope)
            if builtin_template is not None:
                join_port = builtin_template.join_port
        if join_port is not None:
            next_payload.pop("port", None)
            next_payload["join_port"] = self._validate_optional_port(join_port)
        if plan.server_log_file is not None:
            next_payload["server_log_file"] = plan.server_log_file
        if plan.admin_password is not None:
            next_payload["admin_password"] = plan.admin_password
        if request.initial_version is not None:
            next_payload["version"] = request.initial_version.model_dump(mode="json", exclude_none=True)
        if plan.steam_branch is not None:
            steam_recipe = self._steam_install_recipe_for_scope(plan.scope)
            if steam_recipe is None:
                raise ValueError(f"SteamCMD installation is not supported for scope `{plan.scope}`.")
            selected_steam_update = steam_recipe.steam_update.with_selected_branch(
                plan.steam_branch,
                add_if_missing=True,
            )
            next_payload["steam_update"] = selected_steam_update.model_dump(mode="json", exclude_none=True)

        raw[plan.instance_key] = next_payload
        self._write_json_object(plan.instances_path, raw)
        instance_name = f"{plan.scope}_{plan.instance_key}"
        log.info(f"Created app instance `{instance_name}` from template `{plan.scope}:{template_key}`")
        return instance_name

    async def load_instance(self, *, scope: str, instance_key: str) -> ManagedApp:
        """Load one newly provisioned instance without rebuilding every running app."""

        bot = self.bot
        if bot is None:
            raise RuntimeError("App manager bot is not available.")
        resolved_scope = self._validate_scope_name(scope)
        resolved_instance_key = self._validate_instance_key(instance_key)
        scope_path = Path("apps") / resolved_scope
        if not scope_path.is_dir():
            raise ValueError(f"Unknown app scope: {resolved_scope}")
        instances_path = scope_path / "instances.json"
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(resolved_instance_key),
            instances_path=instances_path,
            instance_key=resolved_instance_key,
        )
        app_name = f"{resolved_scope}_{resolved_instance_key}"
        if app_name in self.apps:
            raise ValueError(f"App instance `{app_name}` is already loaded.")
        app_cls, cfg_cls = self._load_scope_types(resolved_scope)
        cfg = self._build_app_config(
            scope=resolved_scope,
            scope_path=scope_path,
            cfg_cls=cfg_cls,
            instance_key=resolved_instance_key,
            raw_cfg=instance_payload,
        )
        if not cfg.directory.exists():
            raise FileNotFoundError(f"App directory is missing: {cfg.directory}")

        app = self._instantiate_app(bot=bot, app_cls=app_cls, cfg=cfg)
        try:
            self._sync_app_instance_config(app)
            await app.post_init()
        except (asyncio.CancelledError, Exception):
            DC_Relay.unregister_app(app)
            raise
        DC_Relay.bind_app_channel(app)
        self.apps[app.name] = app
        self._register_lookup_aliases(app.name, app)
        self.dump_enabled()
        log.info("Loaded provisioned app instance: %s", app.name)
        return app

    def discard_unloaded_instance(self, *, scope: str, instance_key: str) -> None:
        """Remove a just-created instance configuration before it has been loaded."""

        resolved_scope = self._validate_scope_name(scope)
        resolved_instance_key = self._validate_instance_key(instance_key)
        app_name = f"{resolved_scope}_{resolved_instance_key}"
        if app_name in self.apps:
            raise ValueError(f"Loaded app instance `{app_name}` cannot be discarded.")
        instances_path = Path("apps") / resolved_scope / "instances.json"
        raw = self._read_json_object(instances_path)
        if resolved_instance_key not in raw:
            return
        del raw[resolved_instance_key]
        self._write_json_object(instances_path, raw)
        log.info("Discarded unregistered app instance: %s", app_name)

    async def delete_instance(self, name: str | ManagedApp) -> None:
        """Permanently remove a managed app's installation and instance configuration."""

        app = name if isinstance(name, App) else self.get(name)
        apps = self._apps_mapping()
        if apps.get(app.name) is not app:
            raise ValueError(f"App instance `{app.name}` is no longer managed.")
        if app.name.casefold() in self._pending_start_name_keys():
            raise RuntimeError(f"Cannot delete {app.friendly} while it is starting.")

        directory = self._instance_deletion_directory(app)
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"Cannot delete {app.friendly}; its installation path is not a directory.")

        if app.check_running():
            await self.end(app.name)
            if app.check_running():
                raise RuntimeError(f"Cannot delete {app.friendly}; it did not stop cleanly.")

        instances_path = app.file_instances
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(app.cfg.instance_key),
            instances_path=instances_path,
            instance_key=app.cfg.instance_key,
        )
        del raw[app.cfg.instance_key]
        self._write_json_object(instances_path, raw)
        apps.pop(app.name)
        deletion_cancelled = False
        try:
            if directory.exists():
                deletion_cancelled = await self._remove_directory_with_deferred_cancellation(directory)
        except Exception:
            apps[app.name] = app
            raw[app.cfg.instance_key] = instance_payload
            try:
                self._write_json_object(instances_path, raw)
            except Exception:
                log.exception("Failed to restore instance configuration after deletion failure: %s", app.name)
            raise

        app.deregister_activity_providers()
        DC_Relay.unregister_app(app)
        app.set_instance_config_change_handler(None)
        self._unregister_lookup_aliases(app)
        self._managed_shutdown_name_keys().discard(app.name.casefold())
        self._pending_start_name_keys().discard(app.name.casefold())
        self._remove_restart_auto_start_app(app.name)
        self.dump_enabled()
        log.info("Deleted app instance: %s", app.name)
        if deletion_cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _remove_directory_with_deferred_cancellation(directory: Path) -> bool:
        """Finish a destructive removal before honouring task cancellation."""

        removal_task = asyncio.create_task(run_blocking(shutil.rmtree, directory))
        cancellation_requested = False
        while not removal_task.done():
            try:
                await asyncio.shield(removal_task)
            except asyncio.CancelledError:
                cancellation_requested = True
        removal_task.result()
        return cancellation_requested

    def set_restart_auto_start_apps(self, apps: Sequence[str | ManagedApp] | None) -> tuple[str, ...]:
        resolved_names: list[str] = []
        seen_names: set[str] = set()
        for raw_app in apps or ():
            if isinstance(raw_app, App):
                app_name = raw_app.name
            else:
                app_name = self.get(raw_app).name
            if app_name in seen_names:
                continue
            seen_names.add(app_name)
            resolved_names.append(app_name)
        resolved_name_tuple = tuple(resolved_names)

        bot_config = self._load_bot_configuration()
        if bot_config.restart_state.auto_start_apps == resolved_name_tuple:
            return resolved_name_tuple

        bot_config.restart_state = bot_config.restart_state.model_copy(update={"auto_start_apps": resolved_name_tuple})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return resolved_name_tuple

    def _remove_restart_auto_start_app(self, app_name: str) -> None:
        bot_config = self._load_bot_configuration()
        app_name_key = app_name.casefold()
        next_auto_start_apps = tuple(
            configured_name
            for configured_name in bot_config.restart_state.auto_start_apps
            if configured_name.casefold() != app_name_key
        )
        if next_auto_start_apps == bot_config.restart_state.auto_start_apps:
            return
        bot_config.restart_state = bot_config.restart_state.model_copy(
            update={"auto_start_apps": next_auto_start_apps}
        )
        config.save_bot_configuration(self._bot_configuration_path, bot_config)

    def set_running_restart_auto_start_apps(self) -> tuple[str, ...]:
        return self.set_restart_auto_start_apps(self.running_apps())

    def consume_restart_auto_start_apps(self) -> tuple[str, ...]:
        bot_config = self._load_bot_configuration()
        auto_start_apps = bot_config.restart_state.auto_start_apps
        if not auto_start_apps:
            return ()
        bot_config.restart_state = bot_config.restart_state.model_copy(update={"auto_start_apps": ()})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return auto_start_apps

    @property
    def _default_config_path(self) -> Path:
        return Path("configuration.json")

    def _refresh_default_chat_channel(self) -> None:
        raw = self._read_json_object(self._default_config_path)
        channel_texts = normalise_optional_channel_ids(raw.get("default_chat_channels"))
        if not channel_texts:
            legacy_channel_text = normalise_optional_channel_id(raw.get("default_chat_channel"))
            channel_texts = (legacy_channel_text,) if legacy_channel_text is not None else ()
        if channel_texts:
            self.default_chat_channels = tuple(hikari.Snowflake(channel_id) for channel_id in channel_texts)
            self.default_chat_channel = self.default_chat_channels[0]
            self.default_chat_channel_source = RelayChannelSource.DEFAULT
            return
        self.default_chat_channels = ()
        self.default_chat_channel = None
        self.default_chat_channel_source = RelayChannelSource.NONE

    def _resolve_relay_channels(
        self,
        *,
        instance_chat_channel: object,
        instance_chat_channels: object = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...], RelayChannelSource]:
        override_channels = normalise_optional_channel_ids(instance_chat_channels)
        if not override_channels:
            legacy_override_channel = normalise_optional_channel_id(instance_chat_channel)
            override_channels = (legacy_override_channel,) if legacy_override_channel is not None else ()
        if override_channels:
            return override_channels, override_channels, RelayChannelSource.INSTANCE

        default_chat_channels, default_chat_channel, default_source = self._default_chat_channel_state()
        if default_chat_channels:
            return tuple(str(channel_id) for channel_id in default_chat_channels), (), default_source
        if default_chat_channel is not None:
            return (str(default_chat_channel),), (), default_source
        return (), (), RelayChannelSource.NONE

    @staticmethod
    def _set_chat_channel_payload(payload: dict[str, object], channel_texts: Sequence[str]) -> None:
        if not channel_texts:
            payload.pop("chat_channel", None)
            payload.pop("chat_channels", None)
        elif len(channel_texts) == 1:
            payload["chat_channel"] = channel_texts[0]
            payload.pop("chat_channels", None)
        else:
            payload["chat_channels"] = list(channel_texts)
            payload.pop("chat_channel", None)

    def _default_chat_channel_texts(self) -> frozenset[str]:
        default_chat_channels, default_chat_channel, _default_source = self._default_chat_channel_state()
        if default_chat_channels:
            return frozenset(str(hikari.Snowflake(channel_id)) for channel_id in default_chat_channels)
        if default_chat_channel is not None:
            return frozenset({str(hikari.Snowflake(default_chat_channel))})
        return frozenset()

    def _default_chat_channel_state(
        self,
    ) -> tuple[tuple[hikari.Snowflake, ...], hikari.Snowflake | None, RelayChannelSource]:
        try:
            default_chat_channels = self.default_chat_channels
        except AttributeError:
            default_chat_channels = ()
            self.default_chat_channels = default_chat_channels
        try:
            default_chat_channel = self.default_chat_channel
        except AttributeError:
            default_chat_channel = None
            self.default_chat_channel = default_chat_channel
        try:
            default_source = self.default_chat_channel_source
        except AttributeError:
            default_source = RelayChannelSource.NONE
            self.default_chat_channel_source = default_source
        return default_chat_channels, default_chat_channel, default_source

    @staticmethod
    def _app_override_channel_texts(instance_payload: Mapping[str, object]) -> tuple[str, ...]:
        override_channels = normalise_optional_channel_ids(instance_payload.get("chat_channels"))
        if override_channels:
            return override_channels
        legacy_override_channel = normalise_optional_channel_id(instance_payload.get("chat_channel"))
        if legacy_override_channel is None:
            return ()
        return (legacy_override_channel,)

    def _validate_app_chat_channel_overrides(self, app: ManagedApp, channel_texts: Sequence[str]) -> None:
        default_channel_texts = self._default_chat_channel_texts()
        conflicts = tuple(channel_id for channel_id in channel_texts if channel_id in default_channel_texts)
        if not conflicts:
            return
        conflict_text = ", ".join(conflicts)
        raise ValueError(f"{app.friendly} relay override conflicts with default relay channel(s): {conflict_text}.")

    def _remove_app_chat_channel_conflicts(
        self,
        app: ManagedApp,
        default_channel_texts: frozenset[str],
        *,
        instances_path: Path | None = None,
        raw: dict[str, object] | None = None,
        instance_payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        next_instances_path = instances_path or app.file_instances
        next_raw = raw if raw is not None else self._read_json_object(next_instances_path)
        next_instance_payload = (
            instance_payload
            if instance_payload is not None
            else self._require_instance_payload(
                next_raw.get(app.cfg.instance_key),
                instances_path=next_instances_path,
                instance_key=app.cfg.instance_key,
            )
        )

        override_channels = self._app_override_channel_texts(next_instance_payload)
        if not override_channels:
            return next_instance_payload

        retained_channels = tuple(channel_id for channel_id in override_channels if channel_id not in default_channel_texts)
        if retained_channels == override_channels:
            return next_instance_payload

        next_payload = dict(next_instance_payload)
        self._set_chat_channel_payload(next_payload, retained_channels)
        next_raw[app.cfg.instance_key] = next_payload
        self._write_json_object(next_instances_path, next_raw)
        removed_channels = tuple(channel_id for channel_id in override_channels if channel_id in default_channel_texts)
        log.info(
            "Removed app relay override channel conflicts for %s: removed=%s",
            app.name,
            ",".join(removed_channels),
        )
        return next_payload

    def _apply_relay_channel(self, app: ManagedApp) -> None:
        instances_path = app.file_instances
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(app.cfg.instance_key),
            instances_path=instances_path,
            instance_key=app.cfg.instance_key,
        )
        if not app.supports_chat_relay:
            self._purge_app_chat_channel_override(
                app,
                instances_path=instances_path,
                raw=raw,
                instance_payload=instance_payload,
            )
            self._clear_app_relay_state(app)
            chat_channels, _chat_overrides, chat_source = self._resolve_relay_channels(
                instance_chat_channel=None,
                instance_chat_channels=None,
            )
            app.cfg.chat_channels = chat_channels
            app.cfg.chat_channel = chat_channels[0] if chat_channels else None
            app.cfg.chat_channel_source = chat_source
            app.chat_channels = tuple(hikari.Snowflake(channel_id) for channel_id in chat_channels)
            app.chat_channel = app.chat_channels[0] if app.chat_channels else None
            app.chat_channel_source = chat_source
            DC_Relay.bind_app_channel(app)
            return
        default_channel_texts = self._default_chat_channel_texts()
        if default_channel_texts:
            instance_payload = self._remove_app_chat_channel_conflicts(
                app,
                default_channel_texts,
                instances_path=instances_path,
                raw=raw,
                instance_payload=instance_payload,
            )
        chat_channels, chat_overrides, chat_source = self._resolve_relay_channels(
            instance_chat_channel=instance_payload.get("chat_channel"),
            instance_chat_channels=instance_payload.get("chat_channels"),
        )
        app.cfg.chat_channels = chat_channels
        app.cfg.chat_channel = chat_channels[0] if chat_channels else None
        app.cfg.chat_channel_overrides = chat_overrides
        app.cfg.chat_channel_override = chat_overrides[0] if chat_overrides else None
        app.cfg.chat_channel_source = chat_source
        app.chat_channels = tuple(hikari.Snowflake(channel_id) for channel_id in chat_channels)
        app.chat_channel = app.chat_channels[0] if app.chat_channels else None
        app.chat_channel_overrides = tuple(hikari.Snowflake(channel_id) for channel_id in chat_overrides)
        app.chat_channel_override = app.chat_channel_overrides[0] if app.chat_channel_overrides else None
        app.chat_channel_source = chat_source
        DC_Relay.bind_app_channel(app)

    def _clear_app_relay_state(self, app: ManagedApp) -> None:
        app.cfg.chat_channel = None
        app.cfg.chat_channels = ()
        app.cfg.chat_channel_override = None
        app.cfg.chat_channel_overrides = ()
        app.cfg.chat_channel_source = RelayChannelSource.NONE
        app.chat_channel = None
        app.chat_channels = ()
        app.chat_channel_override = None
        app.chat_channel_overrides = ()
        app.chat_channel_source = RelayChannelSource.NONE

    def _purge_app_chat_channel_override(
        self,
        app: ManagedApp,
        *,
        instances_path: Path | None = None,
        raw: dict[str, object] | None = None,
        instance_payload: Mapping[str, object] | None = None,
    ) -> None:
        next_instances_path = instances_path or app.file_instances
        next_raw = raw if raw is not None else self._read_json_object(next_instances_path)
        next_instance_payload = (
            instance_payload
            if instance_payload is not None
            else self._require_instance_payload(
                next_raw.get(app.cfg.instance_key),
                instances_path=next_instances_path,
                instance_key=app.cfg.instance_key,
            )
        )
        if "chat_channel" not in next_instance_payload and "chat_channels" not in next_instance_payload:
            return
        next_payload = dict(next_instance_payload)
        next_payload.pop("chat_channel", None)
        next_payload.pop("chat_channels", None)
        next_raw[app.cfg.instance_key] = next_payload
        self._write_json_object(next_instances_path, next_raw)
        log.info(f"Purged unsupported chat relay override for {app.name}")

    @staticmethod
    def _load_scope_types(scope: str) -> tuple[ManagedAppType, type[App_Config]]:
        module = importlib.import_module(f"apps.{scope}")
        app_cls: ManagedAppType | None = None
        module_members = cast(dict[str, object], vars(module))
        for obj in module_members.values():
            if isinstance(obj, type) and issubclass(obj, App) and obj is not App:
                app_cls = cast(ManagedAppType, obj)
                break
        if app_cls is None:
            raise TypeError(f"apps.{scope} does not export an App subclass")
        cfg_cls = getattr(app_cls, "cfg_cls", App_Config)
        if not isinstance(cfg_cls, type) or not issubclass(cfg_cls, App_Config):
            raise TypeError(f"{app_cls.__name__}.cfg_cls must be an App_Config subclass")
        return (app_cls, cfg_cls)

    def _build_app_config(
        self,
        *,
        scope: str,
        scope_path: Path,
        cfg_cls: type[App_Config],
        instance_key: str,
        raw_cfg: JsonMapping,
    ) -> App_Config:
        next_raw_cfg: JsonObject = dict(raw_cfg)
        app_name = f"{scope}_{instance_key}"
        next_raw_cfg.setdefault("scope", scope)
        next_raw_cfg.setdefault("apps_dir", scope_path)
        next_raw_cfg["instance_key"] = instance_key
        chat_chans, chat_overrides, chat_source = self._resolve_relay_channels(
            instance_chat_channel=next_raw_cfg.get("chat_channel"),
            instance_chat_channels=next_raw_cfg.get("chat_channels"),
        )
        next_raw_cfg["chat_channels"] = chat_chans
        next_raw_cfg["chat_channel"] = chat_chans[0] if chat_chans else None
        next_raw_cfg["chat_channel_overrides"] = chat_overrides
        next_raw_cfg["chat_channel_override"] = chat_overrides[0] if chat_overrides else None
        next_raw_cfg["chat_channel_source"] = chat_source
        return cfg_cls.model_validate({"name": app_name, **next_raw_cfg})

    def _instantiate_app(
        self,
        *,
        bot: hikari.GatewayBot,
        app_cls: ManagedAppType,
        cfg: App_Config,
    ) -> ManagedApp:
        if self.activity_manager is None:
            raise SystemError("Activity_Manager not setup")
        app = app_cls(bot, self.activity_manager, cfg)
        app.set_instance_config_change_handler(self._sync_app_instance_config)
        self._apply_relay_channel(app)
        return app

    def _sync_app_instance_config(self, app: ManagedApp) -> None:
        overrides = dict(app.instance_config_overrides)
        if not overrides:
            return
        instances_path = app.file_instances
        raw = self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            raw.get(app.cfg.instance_key),
            instances_path=instances_path,
            instance_key=app.cfg.instance_key,
        )
        next_payload = dict(instance_payload)
        changed = False
        for key, value in overrides.items():
            if next_payload.get(key) == value:
                continue
            next_payload[key] = value
            changed = True
        if not changed:
            return
        raw[app.cfg.instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        log.info("Persisted instance metadata for %s: %s", app.name, ", ".join(sorted(overrides)))

    def _disable_missing_instance(
        self,
        *,
        instances_path: Path,
        raw: JsonObject,
        cfg: App_Config,
        reason: str,
    ) -> None:
        instance_payload = self._require_instance_payload(
            raw.get(cfg.instance_key),
            instances_path=instances_path,
            instance_key=cfg.instance_key,
        )

        if instance_payload.get("enabled") is False:
            log.info(f"Skipping disabled missing app instance `{cfg.name}`: {reason}")
            return

        self._set_instance_enabled(
            instances_path=instances_path,
            instance_key=cfg.instance_key,
            enabled=False,
            raw=raw,
        )
        self.startup_disabled_instances.append(
            StartupDisabledAppNotice(
                app_name=cfg.name,
                reason=reason,
            )
        )
        log.warning(f"Disabled missing app instance `{cfg.name}`: {reason}")

    def _register_lookup_aliases(self, name: str, app: ManagedApp) -> None:
        self._register_lookup_alias_text(app.name, name)
        self._register_lookup_alias_text(getattr(app, "proc_name", ""), name)
        self._register_lookup_alias_text(app.directory.name, name)
        friendly_name = getattr(app, "friendly", "")
        if friendly_name:
            self._register_lookup_alias_text(friendly_name, name)

    def _unregister_lookup_aliases(self, app: ManagedApp) -> None:
        self._remove_lookup_alias_text(app.name, app.name)
        self._remove_lookup_alias_text(getattr(app, "proc_name", ""), app.name)
        self._remove_lookup_alias_text(app.directory.name, app.name)
        self._remove_lookup_alias_text(getattr(app, "friendly", ""), app.name)

    def _instance_deletion_directory(self, app: ManagedApp) -> Path:
        configured_directory = app.directory
        if configured_directory.is_symlink():
            raise ValueError(f"Cannot delete {app.friendly}; its installation path is a symbolic link.")

        app_root = config.APP_PATH.resolve()
        directory = configured_directory.resolve()
        if directory == app_root:
            raise ValueError(f"Cannot delete {app.friendly}; its installation path is DIR_APP itself.")
        if not directory.is_relative_to(app_root):
            raise ValueError(f"Cannot delete {app.friendly}; its installation path is outside DIR_APP.")

        for other_app in self._apps_mapping().values():
            if other_app is app:
                continue
            other_directory = other_app.directory.resolve()
            if directory.is_relative_to(other_directory) or other_directory.is_relative_to(directory):
                raise ValueError(
                    f"Cannot delete {app.friendly}; its installation path overlaps {other_app.friendly}."
                )
        return directory

    @staticmethod
    def _lookup_alias_variants(text: str) -> tuple[str, ...]:
        stripped_text = text.strip()
        if not stripped_text:
            return ()
        ordered_variants = (
            stripped_text,
            stripped_text.lower(),
            stripped_text.upper(),
            stripped_text.title(),
            stripped_text.capitalize(),
            stripped_text.casefold(),
            stripped_text.swapcase(),
        )
        unique_variants: list[str] = []
        seen_variants: set[str] = set()
        for variant in ordered_variants:
            if variant in seen_variants:
                continue
            seen_variants.add(variant)
            unique_variants.append(variant)
        return tuple(unique_variants)

    def _register_lookup_alias_text(self, text: str, base_name: str) -> None:
        lookup = self._lookup_mapping()
        for alias in self._lookup_alias_variants(text):
            lookup[alias] = base_name

    def _remove_lookup_alias_text(self, text: str, base_name: str) -> None:
        lookup = self._lookup_mapping()
        for alias in self._lookup_alias_variants(text):
            if lookup.get(alias) == base_name:
                lookup.pop(alias, None)

    def _replace_friendly_lookup_aliases(self, *, app: ManagedApp, previous_friendly_name: str) -> None:
        self._remove_lookup_alias_text(previous_friendly_name, app.name)
        self._register_lookup_alias_text(app.friendly, app.name)

    def _friendly_lookup_conflict(self, *, app: ManagedApp, friendly_name: str) -> str | None:
        lookup = self._lookup_mapping()
        for alias in self._lookup_alias_variants(friendly_name):
            resolved_name = lookup.get(alias)
            if resolved_name is None or resolved_name == app.name:
                continue
            return resolved_name
        return None

    def _lookup_mapping(self) -> dict[str, str]:
        try:
            return self._lookup
        except AttributeError:
            self._lookup = {}
            return self._lookup

    @staticmethod
    def _find_instance_template(
        payload: Mapping[str, object],
    ) -> tuple[str, Mapping[str, object]] | None:
        for instance_key, value in payload.items():
            template_payload = App_Manager._as_json_mapping(value)
            if template_payload is not None:
                return (str(instance_key), template_payload)
        return None

    def _load_bot_configuration(self) -> config.BotConfiguration:
        try:
            bot_configuration_path = self._bot_configuration_path
        except AttributeError:
            bot_configuration_path = self._BOT_CONFIGURATION_PATH
            self._bot_configuration_path = bot_configuration_path
        if not bot_configuration_path.exists():
            return config.BotConfiguration()
        return config.load_bot_configuration(bot_configuration_path)

    @classmethod
    def _select_instance_template(
        cls,
        payload: Mapping[str, object],
        *,
        scope: str,
    ) -> tuple[str, Mapping[str, object]]:
        template = cls._find_instance_template(payload)
        if template is not None:
            return template
        raise ValueError(f"Scope `{scope}` does not contain a usable instance template.")

    @staticmethod
    def _builtin_instance_template(scope: str) -> tuple[str, Mapping[str, object]] | None:
        template = _SCOPE_INSTANCE_TEMPLATES.get(scope)
        if template is None:
            return None
        return ("default", template.to_payload())

    def _resolve_instance_template(
        self,
        *,
        scope: str,
        scope_path: Path,
        payload: Mapping[str, object],
    ) -> tuple[str, Mapping[str, object]]:
        template = self._find_instance_template(payload)
        if template is not None:
            return template

        builtin_template = self._builtin_instance_template(scope)
        if builtin_template is not None:
            return builtin_template

        raise ValueError(
            f"Scope `{scope}` does not contain a usable instance template. "
            f"Add `{scope_path / 'instances.json'}` with a template entry first."
        )

    def _scope_supports_create(self, scope_path: Path) -> bool:
        if not (scope_path / "__init__.py").exists():
            return False
        instances_path = scope_path / "instances.json"
        raw = self._read_json_object(instances_path)
        return self._find_instance_template(raw) is not None or self._builtin_instance_template(scope_path.name) is not None

    def _steam_install_recipe_for_scope(self, scope: str) -> AppSteamInstallRecipe | None:
        scope_key = scope.strip().casefold()
        for recipe in self.list_steam_install_recipes():
            if recipe.scope.casefold() == scope_key:
                return recipe
        return None

    @staticmethod
    def _steam_install_config_from_template(
        *,
        scope: str,
        default_config: SteamUpdateConfig,
        template_payload: Mapping[str, object],
    ) -> SteamUpdateConfig:
        configured_payload = template_payload.get("steam_update")
        if configured_payload is None:
            configured_config = default_config.model_copy(deep=True)
        else:
            if not isinstance(configured_payload, Mapping):
                raise ValueError(f"Steam update configuration for scope `{scope}` must be an object.")
            configured_config = SteamUpdateConfig.model_validate(configured_payload)
            if configured_config.app_id != default_config.app_id:
                raise ValueError(f"Steam app ID for scope `{scope}` does not match its installation recipe.")
        discovered_branches = cached_steam_update_branches(default_config.app_id, allow_stale=True) or ()
        return configured_config.model_copy(
            update={"branches": merge_steam_update_branches(discovered_branches, configured_config.branches)}
        )

    def _validate_steam_install_branch(self, *, scope: str, branch_id: str | None) -> str | None:
        if branch_id is None:
            return None
        recipe = self._steam_install_recipe_for_scope(scope)
        if recipe is None:
            raise ValueError(f"SteamCMD installation is not supported for scope `{scope}`.")
        requested_branch = SteamUpdateBranch(branch_id=branch_id)
        try:
            return recipe.steam_update.branch(requested_branch.branch_id).branch_id
        except ValueError:
            return requested_branch.branch_id

    @staticmethod
    def _validate_scope_name(raw: str) -> str:
        scope = raw.strip()
        if not scope:
            raise ValueError("Scope must not be empty.")
        if "/" in scope or "\\" in scope or scope.startswith("."):
            raise ValueError("Scope must be a simple app folder name.")
        return scope

    @staticmethod
    def _validate_instance_key(raw: str) -> str:
        instance_key = raw.strip()
        if not instance_key:
            raise ValueError("Instance key must not be empty.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if any(char not in allowed for char in instance_key):
            raise ValueError("Instance key may only use letters, numbers, `_`, and `-`.")
        return instance_key

    @staticmethod
    def _validate_subfolder(raw: str) -> Path:
        subfolder = Path(raw.strip())
        if not str(subfolder):
            raise ValueError("Subfolder must not be empty.")
        if subfolder.is_absolute():
            raise ValueError("Subfolder must be relative to DIR_APP.")

        parts = subfolder.parts
        if not parts:
            raise ValueError("Subfolder must not be empty.")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Subfolder must stay within DIR_APP.")

        resolved_path = (config.APP_PATH / subfolder).resolve()
        app_root = config.APP_PATH.resolve()
        try:
            resolved_path.relative_to(app_root)
        except ValueError as xcp:
            raise ValueError("Subfolder must stay within DIR_APP.") from xcp
        return subfolder

    @staticmethod
    def _validate_optional_port(raw: object) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError("Port must be an integer.")
        if not 1 <= raw <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        return raw

    @staticmethod
    def _validate_optional_config_path(raw: str | None, *, label: str) -> str | None:
        del label
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            return None
        return text

    @staticmethod
    def _validate_required_config_text(raw: str | None, *, label: str) -> str:
        if raw is None:
            raise ValueError(f"{label} must not be empty.")
        text = raw.strip()
        if not text:
            raise ValueError(f"{label} must not be empty.")
        return text

    @staticmethod
    def _read_json_object(path: Path) -> JsonObject:
        if not path.exists():
            return {}
        raw = path.read_text(config.STR_ENCODE)
        if not raw.strip():
            return {}
        payload = cast(object, json.loads(raw))
        next_payload = App_Manager._as_json_object(payload)
        if next_payload is None:
            raise ValueError(f"{path} must contain a JSON object")
        return next_payload

    @staticmethod
    def _write_json_object(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps(payload, indent=4) + "\n", config.STR_ENCODE)

    @staticmethod
    def _as_json_object(value: object) -> JsonObject | None:
        if not isinstance(value, dict):
            return None
        keys = tuple(cast(dict[object, object], value).keys())
        if not all(isinstance(key, str) for key in keys):
            return None
        return cast(JsonObject, value)

    @staticmethod
    def _as_json_mapping(value: object) -> JsonMapping | None:
        if not isinstance(value, Mapping):
            return None
        keys = tuple(cast(Mapping[object, object], value).keys())
        if not all(isinstance(key, str) for key in keys):
            return None
        return cast(JsonMapping, value)

    @classmethod
    def _require_instance_payload(
        cls,
        value: object,
        *,
        instances_path: Path,
        instance_key: str,
    ) -> JsonMapping:
        instance_payload = cls._as_json_mapping(value)
        if instance_payload is None:
            raise ValueError(f"{instances_path} is missing instance {instance_key!r}")
        return instance_payload

    def _set_instance_enabled(
        self,
        *,
        instances_path: Path,
        instance_key: str,
        enabled: bool,
        raw: JsonObject | None = None,
    ) -> None:
        next_raw = raw if raw is not None else self._read_json_object(instances_path)
        instance_payload = self._require_instance_payload(
            next_raw.get(instance_key),
            instances_path=instances_path,
            instance_key=instance_key,
        )
        next_payload = dict(instance_payload)
        next_payload["enabled"] = enabled
        next_raw[instance_key] = next_payload
        self._write_json_object(instances_path, next_raw)


async def ac_enabled_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values() if a.cfg.enabled])


async def ac_running_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([app.friendly for app in manager.running_apps()])


async def ac_disabled_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values() if not a.cfg.enabled])


async def ac_all_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values()])


async def ac_app_logs(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values() if a.dir_log.exists()] + ["System"])


class Provider_Process(Activity_Provider):
    activity_field = config.DiscordActivityField.APP

    def __init__(self, manager: App_Manager):
        self.manager = manager
        self.prio = 6
        super().__init__()

    async def get(self) -> str | None:
        target = self.manager.activity_rotation_target()
        if target is None:
            if not self.silent:
                log.debug("Provider_Process: not app")
            return None
        app, show_alt_text = target
        if show_alt_text and (name := (app.cfg.provider_alt_text or (app.settings and app.settings.app.server_name))):
            return f"<{name.strip(' \'"_-:;<>')}>"
        return app.friendly


class Provider_Player(Activity_Provider):
    _RECOVERY_INTERVAL = 10
    activity_field = config.DiscordActivityField.PLAYERS

    def __init__(self, manager: App_Manager):
        self.manager = manager
        self.prio = 4
        super().__init__()

    @staticmethod
    def _budget_key() -> str:
        return __name__

    @classmethod
    def _recovery_key(cls) -> str:
        return f"{__name__}:recovery"

    async def get(self) -> str | None:
        target = self.manager.activity_rotation_target()
        if target is None:
            if not self.silent:
                log.debug("Provider_Player: not app")
            return None
        app, _show_alt_text = target
        budget_key = self._budget_key()
        recovery_key = self._recovery_key()
        if not app.is_started:
            if not self.silent:
                log.debug("Provider_Player: %s has not finished startup", app.name)
            return None
        if app.act_err_counts.setdefault(budget_key, app.act_err_threshold) <= 0:
            recovery_remaining = app.act_err_counts.get(recovery_key, self._RECOVERY_INTERVAL)
            if recovery_remaining > 0:
                app.act_err_counts[recovery_key] = recovery_remaining - 1
                if not self.silent:
                    log.debug(
                        "Provider_Player: %s exhausted error budget; retrying in %s activity ticks",
                        app.name,
                        recovery_remaining - 1,
                    )
                return None
            app.act_err_counts[recovery_key] = self._RECOVERY_INTERVAL
            if not self.silent:
                log.debug("Provider_Player: %s probing for recovery after exhausted error budget", app.name)
        if players := await app.player_count():
            app.act_err_counts[budget_key] = app.act_err_threshold
            app.act_err_counts.pop(recovery_key, None)
            player_capacity_text: str | None = format_player_capacity(players[1])
            if player_capacity_text is None:
                raise RuntimeError("Player capacity unexpectedly missing for activity provider.")
            status = f"{players[0]}/{player_capacity_text}"
            if not self.silent:
                log.debug("Provider_Player: %s -> %s", app.name, status)
            return status
        app.act_err_counts[budget_key] -= 1
        if not self.silent:
            log.debug(
                "Provider_Player: %s returned no player count | attempts left %s",
                app.name,
                app.act_err_counts[budget_key],
            )
        return None


# AiviA APasz
