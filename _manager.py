import asyncio
import importlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import hikari
import lightbulb

import config
from _discord import App_Bound, DC_Bound, DC_Relay
from _relay_embeds import build_app_lifecycle_embed
from apps._app import App
from apps._config import App_Config, RelayChannelSource, normalise_optional_channel_id, normalise_optional_channel_ids
from config import Activity_Manager, Activity_Provider

log = logging.getLogger(__name__)

type JsonObject = dict[str, object]
type JsonMapping = Mapping[str, object]
type ManagedApp = App[App_Config]
type ManagedAppType = type[ManagedApp]


@dataclass(frozen=True, slots=True)
class AppInstanceCreateRequest:
    scope: str
    instance_key: str
    friendly_name: str
    subfolder: str
    port: int | None = None
    server_log_file: str | None = None
    admin_password: str | None = None


@dataclass(frozen=True, slots=True)
class AppInstanceTemplate:
    mods_dir: str | None = None
    server_log_file: str | None = None
    join_port: int | None = None
    api_host: str | None = None
    api_port: int | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.mods_dir is not None:
            payload["mods_dir"] = self.mods_dir
        if self.server_log_file is not None:
            payload["server_log_file"] = self.server_log_file
        if self.join_port is not None:
            payload["join_port"] = self.join_port
        if self.api_host is not None:
            payload["api_host"] = self.api_host
        if self.api_port is not None:
            payload["api_port"] = self.api_port
        return payload


@dataclass(frozen=True, slots=True)
class StartupDisabledAppNotice:
    app_name: str
    reason: str

    def format_line(self) -> str:
        return f"Auto-disabled: {self.app_name} ({self.reason})"


_SCOPE_INSTANCE_TEMPLATES: dict[str, AppInstanceTemplate] = {
    "beammp": AppInstanceTemplate(
        mods_dir="{WD}/Resources/Client",
        server_log_file="{WD}/Server.log",
        join_port=30814,
    ),
    "ets": AppInstanceTemplate(
        server_log_file="{WD}/home_data/Euro Truck Simulator 2/server.log.txt",
        join_port=27015,
    ),
    "factorio": AppInstanceTemplate(
        mods_dir="{WD}/mods",
        server_log_file="{WD}/factorio-current.log",
        join_port=34197,
    ),
    "minecraft": AppInstanceTemplate(
        mods_dir="{WD}/mods",
        join_port=25565,
    ),
    "satisfactory": AppInstanceTemplate(
        join_port=7777,
        api_host="127.0.0.1",
    ),
    "sevendays": AppInstanceTemplate(
        mods_dir="{WD}/Mods",
        server_log_file="{WD}/server_stdout.log",
        join_port=26900,
    ),
}


def format_enabled_app_dump(apps: Sequence[ManagedApp]) -> str:
    ordered_apps = sorted(apps, key=lambda app: app.name.casefold())
    return "\n".join(f"{app.name}: {app.cfg.enabled_txt}" for app in ordered_apps) + "\n"


class App_Manager(metaclass=config.Singleton):
    _BOT_CONFIGURATION_PATH = Path("configuration.json")
    activity_manager: "Activity_Manager | None" = None
    bot: hikari.GatewayBot | None = None

    def __init__(self):
        self.current: str | None = None
        self.apps: dict[str, ManagedApp] = {}
        self._lookup: dict[str, str] = {}
        self.default_chat_channels: tuple[hikari.Snowflake, ...] = ()
        self.default_chat_channel: hikari.Snowflake | None = None
        self.default_chat_channel_source = RelayChannelSource.NONE
        self.startup_disabled_instances: list[StartupDisabledAppNotice] = []
        self._update_task: asyncio.Task[None] | None = None
        self._bot_configuration_path = self._BOT_CONFIGURATION_PATH

    async def post_init(self, bot: hikari.GatewayBot, activity_manager: "Activity_Manager"):
        self.bot = bot
        self.activity_manager = activity_manager
        await self.load_apps(bot)
        self._update_task = asyncio.create_task(self.update_current())

    async def update_current(self):
        while True:
            if app := self.get_current:
                if not app.check_running():
                    self.current = None
            await asyncio.sleep(1)

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
        name = app.name
        if not app.cfg.enabled:
            raise LookupError("App Not Enabled")
        await self.end()
        if app.settings:
            app.settings.app.save()
        self.current = name
        try:
            await app.start()
            app.lifecycle_started_at = datetime.now(timezone.utc)
            self._notify_app_lifecycle(app, started=True)
        except Exception:
            if self.current == name:
                self.current = None
            raise

    async def _shutdown_apps(
        self,
        *,
        name: str | None,
        action_label: str,
        action_present_participle: str,
        shutdown: Callable[[ManagedApp], Awaitable[bool | None]],
        allow_current_target_when_inactive: bool = False,
    ) -> set[str]:
        async def timed_shutdown(app: ManagedApp):
            is_current_target = self.current == app.name
            if not app.check_running():
                if not allow_current_target_when_inactive or not is_current_target:
                    log.info(f"{app.name} not running; skipping.")
                    return (app.name, 0.0, "Skipped", "Not running", None)
            t0 = time.perf_counter()
            started_at = app.lifecycle_started_at
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

        if name:
            try:
                app = self.get(name)
            except ValueError:
                log.warning(f"Tried to {action_label} unknown app: {name}")
                raise ProcessLookupError("Unknown App")
            result = await timed_shutdown(app)
            status = f"{result[0]}: {result[2]} in {result[1]:.2f}s"
            log.info(status)
            if result[2] == "Success":
                if self.current == app.name:
                    self.current = None
                self._notify_app_lifecycle(app, started=False, uptime=result[4])
            return {
                result[0].title(),
            }

        if app := self.get_current:
            log.info(f"{action_present_participle} current app: {self.current}")
            result = await timed_shutdown(app)
            log.info(f"{result[0]}: {result[2]} in {result[1]:.2f}s")
            self.current = None
            if result[2] == "Success":
                self._notify_app_lifecycle(app, started=False, uptime=result[4])
            return {
                result[0].title(),
            }

        log.info(f"{action_present_participle} all apps...")
        results = await asyncio.gather(*(timed_shutdown(app) for app in self.apps.values()))

        names: set[str] = set()
        for app_name, secs, status, _, uptime in results:
            if status != "Skipped":
                names.add(app_name)
                log.info(f" - {app_name}: {status} in {secs:.2f}s")
            if status == "Success":
                app = self.apps[app_name]
                self._notify_app_lifecycle(app, started=False, uptime=uptime)

        log.info(f"All apps finished {action_label}.")
        self.current = None
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
            allow_current_target_when_inactive=True,
        )

    def _notify_app_lifecycle(
        self,
        app: ManagedApp,
        *,
        started: bool,
        uptime: timedelta | None = None,
    ) -> None:
        if app.chat_channel is None:
            return
        relay_embed = build_app_lifecycle_embed(app, started=started, uptime=uptime)
        content = "Started" if started else "Stopped"
        DC_Relay.add(DC_Bound(app, content, "System", relay_embed=relay_embed))

    async def notify_running_app_relays(self, content: str, *, player: str = "System") -> int:
        if self.bot is None:
            log.warning("Skipping app relay notice because the manager bot is unavailable.")
            return 0

        system_channel = hikari.TextableChannel(app=self.bot, id=hikari.Snowflake(0), name="SYSTEM", type=1)
        sent_count = 0
        for app in sorted(self.apps.values(), key=lambda item: item.name.casefold()):
            if not app.supports_relay_system_notices or not app._running or app.am_receiver is None:
                continue
            notice = App_Bound(system_channel, content, player)
            notice.app = app
            try:
                await app.am_receiver.send(notice)
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

    def create_instance(self, request: AppInstanceCreateRequest) -> str:
        scope = self._validate_scope_name(request.scope)
        instance_key = self._validate_instance_key(request.instance_key)
        friendly_name = request.friendly_name.strip()
        if not friendly_name:
            raise ValueError("Friendly name must not be empty.")
        subfolder = self._validate_subfolder(request.subfolder)
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

        template_key, template_payload = self._resolve_instance_template(
            scope=scope,
            scope_path=scope_path,
            payload=raw,
        )
        next_payload = dict(template_payload)
        next_payload["friendly_name"] = friendly_name
        next_payload["directory"] = f"{{APPS}}/{subfolder.as_posix()}"
        if request.port is not None:
            next_payload.pop("port", None)
            next_payload["join_port"] = request.port
        if server_log_file is not None:
            next_payload["server_log_file"] = server_log_file
        if admin_password is not None:
            next_payload["admin_password"] = admin_password

        raw[instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        instance_name = f"{scope}_{instance_key}"
        log.info(f"Created app instance `{instance_name}` from template `{scope}:{template_key}`")
        return instance_name

    @property
    def get_current(self) -> ManagedApp | None:
        return self.apps.get(self.current) if self.current else None

    def set_restart_auto_start_app(self, name: str | ManagedApp | None) -> str | None:
        resolved_name: str | None
        if name is None:
            resolved_name = None
        elif isinstance(name, App):
            resolved_name = name.name
        else:
            resolved_name = self.get(name).name

        bot_config = self._load_bot_configuration()
        if bot_config.restart_state.auto_start_app == resolved_name:
            return resolved_name

        bot_config.restart_state = bot_config.restart_state.model_copy(update={"auto_start_app": resolved_name})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return resolved_name

    def set_current_restart_auto_start_app(self) -> str | None:
        return self.set_restart_auto_start_app(self.get_current)

    def consume_restart_auto_start_app(self) -> str | None:
        bot_config = self._load_bot_configuration()
        auto_start_app = bot_config.restart_state.auto_start_app
        if auto_start_app is None:
            return None
        bot_config.restart_state = bot_config.restart_state.model_copy(update={"auto_start_app": None})
        config.save_bot_configuration(self._bot_configuration_path, bot_config)
        return auto_start_app

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

        default_chat_channels = cast(tuple[hikari.Snowflake, ...], getattr(self, "default_chat_channels", ()))
        if default_chat_channels:
            return tuple(str(channel_id) for channel_id in default_chat_channels), (), self.default_chat_channel_source
        if self.default_chat_channel is not None:
            return (str(self.default_chat_channel),), (), self.default_chat_channel_source
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
        channels = cast(tuple[hikari.Snowflake, ...], getattr(self, "default_chat_channels", ()))
        if channels:
            return frozenset(str(hikari.Snowflake(channel_id)) for channel_id in channels)
        channel = cast(hikari.Snowflake | None, getattr(self, "default_chat_channel", None))
        if channel is not None:
            return frozenset({str(hikari.Snowflake(channel))})
        return frozenset()

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
        def permitate(trans: str, base: str, /):
            if trans:
                self._lookup[trans] = base
                self._lookup[trans.lower()] = base
                self._lookup[trans.upper()] = base
                self._lookup[trans.title()] = base
                self._lookup[trans.capitalize()] = base
                self._lookup[trans.casefold()] = base
                self._lookup[trans.swapcase()] = base

        permitate(app.name, name)
        permitate(app.proc_name, name)
        permitate(app.directory.name, name)
        if app.friendly:
            permitate(app.friendly, name)

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
        if not self._bot_configuration_path.exists():
            return config.BotConfiguration()
        return config.load_bot_configuration(self._bot_configuration_path)

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


async def ac_disabled_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values() if not a.cfg.enabled])


async def ac_all_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values()])


async def ac_app_logs(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([a.friendly for a in manager.apps.values() if a.dir_log.exists()] + ["System"])


class Provider_Process(Activity_Provider):
    def __init__(self, manager: App_Manager):
        self.manager = manager
        self.prio = 6
        self._counter = 0
        super().__init__()

    async def get(self) -> str | None:
        if not self.silent:
            log.debug(f"Provider_Process: {self.manager.current}")
        if app := self.manager.get_current:
            if app.check_running():
                if name := (app.cfg.provider_alt_text or (app.settings and app.settings.app.server_name)):
                    if self._counter == 3:
                        self._counter = 0
                        return f"<{name.strip(' \'"_-:;<>')}>"
                    self._counter += 1
                return app.friendly
            elif not self.silent:
                log.debug("Provider_Process: not running")
        elif not self.silent:
            log.debug("Provider_Process: not app")
        return None


class Provider_Player(Activity_Provider):
    _RECOVERY_INTERVAL = 10

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
        if app := self.manager.get_current:
            if not app.check_running():
                if not self.silent:
                    log.debug("Provider_Player: %s is not running", app.name)
                return None
            if not app.is_started:
                if not self.silent:
                    log.debug("Provider_Player: %s has not finished startup", app.name)
                return None
            budget_key = self._budget_key()
            recovery_key = self._recovery_key()
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
                status = f"{players[0]}/{players[1]}"
                if not self.silent:
                    log.debug("Provider_Player: %s -> %s", app.name, status)
                return status
            else:
                app.act_err_counts[budget_key] -= 1
                if not self.silent:
                    log.debug(
                        "Provider_Player: %s returned no player count | attempts left %s",
                        app.name,
                        app.act_err_counts[budget_key],
                    )
        elif not self.silent:
            log.debug("Provider_Player: not app")
        return None


# AiviA APasz
