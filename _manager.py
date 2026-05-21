import asyncio
import importlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hikari
import lightbulb

import config
from _discord import DC_Relay
from apps._app import App, App_Config
from apps._config import RelayChannelSource, normalise_optional_channel_id
from config import Activity_Manager, Activity_Provider

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppInstanceCreateRequest:
    scope: str
    instance_key: str
    friendly_name: str
    subfolder: str
    port: int | None = None
    server_log_file: str | None = None


def format_enabled_app_dump(apps: Sequence[App]) -> str:
    ordered_apps = sorted(apps, key=lambda app: app.name.casefold())
    return "\n".join(f"{app.name}: {app.cfg.enabled_txt}" for app in ordered_apps) + "\n"


class App_Manager(metaclass=config.Singleton):
    activity_manager: "Activity_Manager | None" = None
    bot: hikari.GatewayBot | None = None

    def __init__(self):
        self.current: str | None = None
        self.apps: dict[str, App] = {}
        self._lookup: dict[str, str] = {}
        self.default_chat_channel: hikari.Snowflake | None = None
        self.default_chat_channel_source = RelayChannelSource.NONE

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
        apps: dict[str, App] = {}
        base_path = Path("apps")
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

            raw: dict[str, dict[str, Any]] = json.loads(instances_path.read_text(config.STR_ENCODE))
            app_cls, cfg_cls = self._load_scope_types(entry.name)
            for instance_name, raw_cfg in raw.items():
                try:
                    app = self._instantiate_app(
                        bot=bot,
                        scope=entry.name,
                        scope_path=entry,
                        app_cls=app_cls,
                        cfg_cls=cfg_cls,
                        instance_key=instance_name,
                        raw_cfg=raw_cfg,
                    )
                    apps[instance_name] = app
                    log.info(f"Loaded: {instance_name}")
                except Exception:
                    log.exception(f"Instantiate {instance_name}")

        await asyncio.gather(*(app.post_init() for app in apps.values()))
        for app in apps.values():
            DC_Relay.bind_app_channel(app)

        self.apps = apps
        self.dump_enabled()
        for name, app in self.apps.items():
            self._register_lookup_aliases(name, app)

    async def launch(self, name: str | App):
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
        await app.start()
        self.current = name

    async def end(self, name: str | None = None) -> set[str]:
        async def timed_stop(app: App):
            if not app.check_running():
                log.info(f"{app.name} not running; skipping.")
                return (app.name, 0.0, "Skipped", "Not running")

            t0 = time.perf_counter()
            try:
                result = await app.stop() or None
                elapsed = time.perf_counter() - t0
                return (app.name, elapsed, "Success", result)
            except Exception as xcp:
                elapsed = time.perf_counter() - t0
                log.exception(f"Failed to stop {app.name}: {xcp}")
                return (app.name, elapsed, "Error", str(xcp))

        if name:
            name = name.lower()
            if name not in self.apps:
                log.warning(f"Tried to end unknown app: {name}")
                raise ProcessLookupError("Unknown App")
            result = await timed_stop(self.apps[name])
            status = f"{result[0]}: {result[2]} in {result[1]:.2f}s"
            log.info(status)
            return {
                name.title(),
            }

        if app := self.get_current:
            log.info(f"Ending current app: {self.current}")
            result = await timed_stop(app)
            log.info(f"{result[0]}: {result[2]} in {result[1]:.2f}s")
            self.current = None
            return {
                result[0].title(),
            }

        log.info("Ending all apps...")
        results = await asyncio.gather(*(timed_stop(app) for app in self.apps.values()))

        names: set[str] = set()
        for name, secs, status, detail in results:
            if status != "Skipped":
                names.add(name)
                log.info(f" - {name}: {status} in {secs:.2f}s")

        log.info("All apps shut down.")
        self.current = None
        return names

    def toggle(self, name: str, state: bool):
        name = name.lower()
        app = self.get(name)
        app.cfg.enabled = state
        self.dump_enabled()

    def clear_app_chat_channel(self, name: str | App) -> None:
        self.set_app_chat_channel(name, None)

    def set_app_chat_channel(self, name: str | App, channel_id: hikari.Snowflakeish | None) -> None:
        app = name if isinstance(name, App) else self.get(name)
        if not app.supports_chat_relay:
            if channel_id is None:
                self._purge_app_chat_channel_override(app)
                self._clear_app_relay_state(app)
                DC_Relay.bind_app_channel(app)
                return
            raise ValueError(f"{app.friendly} does not support chat relay.")
        instances_path = app.cfg.apps_dir / "instances.json"
        raw = self._read_json_object(instances_path)
        instance_payload = raw.get(app.cfg.instance_key)
        if not isinstance(instance_payload, Mapping):
            raise ValueError(f"{instances_path} is missing instance {app.cfg.instance_key!r}")
        next_payload = dict(instance_payload)
        channel_text = normalise_optional_channel_id(channel_id)
        if channel_text is None:
            next_payload.pop("chat_channel", None)
        else:
            next_payload["chat_channel"] = channel_text
        raw[app.cfg.instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        self._apply_relay_channel(app)

    def clear_default_chat_channel(self) -> None:
        self.set_default_chat_channel(None)

    def set_default_chat_channel(self, channel_id: hikari.Snowflakeish | None) -> None:
        defaults_path = self._default_config_path
        raw = self._read_json_object(defaults_path)
        channel_text = normalise_optional_channel_id(channel_id)
        if channel_text is None:
            raw.pop("default_chat_channel", None)
        else:
            raw["default_chat_channel"] = channel_text
        self._write_json_object(defaults_path, raw)
        self._refresh_default_chat_channel()
        for app in self.apps.values():
            self._apply_relay_channel(app)

    def get(self, name: str) -> App:
        if app_name := self._lookup.get(name):
            return self.apps[app_name]
        raise ValueError(f"No such app: {name}")

    def list_create_scopes(self) -> tuple[str, ...]:
        scopes: list[str] = []
        for entry in Path("apps").iterdir():
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            if not (entry / "instances.json").exists():
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

        scope_path = Path("apps") / scope
        if not scope_path.is_dir():
            raise ValueError(f"Unknown app scope: {scope}")

        instances_path = scope_path / "instances.json"
        raw = self._read_json_object(instances_path)
        if instance_key in raw:
            raise ValueError(f"Instance key `{instance_key}` already exists for scope `{scope}`.")

        template_key, template_payload = self._select_instance_template(raw, scope=scope)
        next_payload = dict(template_payload)
        next_payload["friendly_name"] = friendly_name
        next_payload["directory"] = f"{{APPS}}/{subfolder.as_posix()}"
        if request.port is not None:
            next_payload.pop("port", None)
            next_payload["join_port"] = request.port
        if server_log_file is not None:
            next_payload["server_log_file"] = server_log_file

        raw[instance_key] = next_payload
        self._write_json_object(instances_path, raw)
        instance_name = f"{scope}_{instance_key}"
        log.info(f"Created app instance `{instance_name}` from template `{scope}:{template_key}`")
        return instance_name

    @property
    def get_current(self) -> App | None:
        return self.apps.get(self.current) if self.current else None

    @property
    def _default_config_path(self) -> Path:
        return Path("configuration.json")

    def _refresh_default_chat_channel(self) -> None:
        raw = self._read_json_object(self._default_config_path)
        channel_text = normalise_optional_channel_id(raw.get("default_chat_channel"))
        if channel_text is not None:
            self.default_chat_channel = hikari.Snowflake(channel_text)
            self.default_chat_channel_source = RelayChannelSource.DEFAULT
            return
        self.default_chat_channel = None
        self.default_chat_channel_source = RelayChannelSource.NONE

    def _resolve_relay_channel(
        self,
        *,
        instance_chat_channel: object,
    ) -> tuple[str | None, str | None, RelayChannelSource]:
        override_channel = normalise_optional_channel_id(instance_chat_channel)
        if override_channel is not None:
            return override_channel, override_channel, RelayChannelSource.INSTANCE

        if self.default_chat_channel is not None:
            return str(self.default_chat_channel), None, self.default_chat_channel_source
        return None, None, RelayChannelSource.NONE

    def _apply_relay_channel(self, app: App) -> None:
        instances_path = app.cfg.apps_dir / "instances.json"
        raw = self._read_json_object(instances_path)
        instance_payload = raw.get(app.cfg.instance_key)
        if not isinstance(instance_payload, Mapping):
            raise ValueError(f"{instances_path} is missing instance {app.cfg.instance_key!r}")
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
        chat_channel, chat_override, chat_source = self._resolve_relay_channel(
            instance_chat_channel=instance_payload.get("chat_channel"),
        )
        app.cfg.chat_channel = chat_channel
        app.cfg.chat_channel_override = chat_override
        app.cfg.chat_channel_source = chat_source
        app.chat_channel = hikari.Snowflake(chat_channel) if chat_channel else None
        app.chat_channel_override = hikari.Snowflake(chat_override) if chat_override else None
        app.chat_channel_source = chat_source
        DC_Relay.bind_app_channel(app)

    def _clear_app_relay_state(self, app: App) -> None:
        app.cfg.chat_channel = None
        app.cfg.chat_channel_override = None
        app.cfg.chat_channel_source = RelayChannelSource.NONE
        app.chat_channel = None
        app.chat_channel_override = None
        app.chat_channel_source = RelayChannelSource.NONE

    def _purge_app_chat_channel_override(
        self,
        app: App,
        *,
        instances_path: Path | None = None,
        raw: dict[str, object] | None = None,
        instance_payload: Mapping[str, object] | None = None,
    ) -> None:
        next_instances_path = instances_path or (app.cfg.apps_dir / "instances.json")
        next_raw = raw if raw is not None else self._read_json_object(next_instances_path)
        next_instance_payload = instance_payload if instance_payload is not None else next_raw.get(app.cfg.instance_key)
        if not isinstance(next_instance_payload, Mapping):
            raise ValueError(f"{next_instances_path} is missing instance {app.cfg.instance_key!r}")
        if "chat_channel" not in next_instance_payload:
            return
        next_payload = dict(next_instance_payload)
        next_payload.pop("chat_channel", None)
        next_raw[app.cfg.instance_key] = next_payload
        self._write_json_object(next_instances_path, next_raw)
        log.info(f"Purged unsupported chat relay override for {app.name}")

    @staticmethod
    def _load_scope_types(scope: str) -> tuple[type[App], type[App_Config]]:
        module = importlib.import_module(f"apps.{scope}")
        app_cls = next(
            obj for obj in vars(module).values() if isinstance(obj, type) and issubclass(obj, App) and obj is not App
        )
        cfg_cls = getattr(app_cls, "cfg_cls", App_Config)
        if not isinstance(cfg_cls, type) or not issubclass(cfg_cls, App_Config):
            raise TypeError(f"{app_cls.__name__}.cfg_cls must be an App_Config subclass")
        return (app_cls, cfg_cls)

    def _instantiate_app(
        self,
        *,
        bot: hikari.GatewayBot,
        scope: str,
        scope_path: Path,
        app_cls: type[App],
        cfg_cls: type[App_Config],
        instance_key: str,
        raw_cfg: Mapping[str, Any],
    ) -> App:
        next_raw_cfg: dict[str, Any] = dict(raw_cfg)
        app_name = f"{scope}_{instance_key}"
        next_raw_cfg.setdefault("scope", scope)
        next_raw_cfg.setdefault("apps_dir", scope_path)
        next_raw_cfg["instance_key"] = instance_key
        chat_chan, chat_override, chat_source = self._resolve_relay_channel(
            instance_chat_channel=next_raw_cfg.get("chat_channel"),
        )
        next_raw_cfg["chat_channel"] = chat_chan
        next_raw_cfg["chat_channel_override"] = chat_override
        next_raw_cfg["chat_channel_source"] = chat_source
        if self.activity_manager is None:
            raise SystemError("Activity_Manager not setup")
        cfg = cfg_cls.model_validate({"name": app_name, **next_raw_cfg})
        app = app_cls(bot, self.activity_manager, cfg)
        self._apply_relay_channel(app)
        return app

    def _register_lookup_aliases(self, name: str, app: App) -> None:
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
    def _select_instance_template(
        payload: Mapping[str, object],
        *,
        scope: str,
    ) -> tuple[str, Mapping[str, object]]:
        for instance_key, value in payload.items():
            if isinstance(value, Mapping):
                return (str(instance_key), value)
        raise ValueError(f"Scope `{scope}` does not contain a usable instance template.")

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
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            return None
        return text

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        raw = path.read_text(config.STR_ENCODE)
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return dict(payload)

    @staticmethod
    def _write_json_object(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps(payload, indent=4) + "\n", config.STR_ENCODE)


async def ac_enabled_apps(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values() if a.cfg.enabled])


async def ac_disabled_apps(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values() if not a.cfg.enabled])


async def ac_all_apps(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values()])


async def ac_app_logs(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
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
            if app.check_running:
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
    def __init__(self, manager: App_Manager):
        self.manager = manager
        self.prio = 4
        super().__init__()

    async def get(self) -> str | None:
        if app := self.manager.get_current:
            if not app.check_running:
                return None
            if app.act_err_counts.setdefault(__name__, app.act_err_threshold) <= 0:
                return None
            if players := await app.player_count():
                return f"{players[0]}/{players[1]}"
            else:
                app.act_err_counts[__name__] -= 1
                if not self.silent:
                    log.debug(f"Provider_Player: not players | attempts left {app.act_err_counts[__name__]}")
        elif not self.silent:
            log.debug("Provider_Player: not app")
        return None


# AiviA APasz
