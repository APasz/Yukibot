import asyncio
import importlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import hikari
import lightbulb


from _discord import DC_Relay
import config
from apps._app import App, App_Config
from config import Activity_Manager, Activity_Provider

log = logging.getLogger(__name__)


class App_Manager(metaclass=config.Singleton):
    activity_manager: "Activity_Manager | None" = None

    def __init__(self):
        self.current: str | None = None
        self.apps: dict[str, App] = {}
        self._lookup: dict[str, str] = {}

    async def post_init(self, bot: hikari.GatewayBot, activity_manager: "Activity_Manager"):
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
        config.ENABLED_FILE.parent.mkdir(exist_ok=True, parents=True)
        return config.ENABLED_FILE.write_text(
            json.dumps({app.name: app.cfg.enabled for app in self.apps.values()}, indent=4), config.STR_ENCODE
        )

    async def load_apps(self, bot: hikari.GatewayBot):
        apps: dict[str, App] = {}
        base_path = Path("apps")

        for entry in base_path.iterdir():
            entry = entry.resolve()
            if not entry.is_dir() or entry.name.startswith("_"):
                continue

            instances_path = entry / "instances.json"
            if not instances_path.exists():
                continue

            module = importlib.import_module(f"apps.{entry.name}")
            cls = next(
                obj
                for obj in vars(module).values()
                if isinstance(obj, type) and issubclass(obj, App) and obj is not App
            )

            raw: dict[str, dict[str, Any]] = json.loads(instances_path.read_text(config.STR_ENCODE))
            for instance_name, raw_cfg in raw.items():
                try:
                    instance_name = f"{entry.name}_{instance_name}"
                    raw_cfg.setdefault("scope", entry.name)
                    raw_cfg.setdefault("apps_dir", entry)
                    chat_chan = config.env_opt(f"{entry.name.upper()}_CHAT_CHANNEL")
                    if not chat_chan:
                        chat_chan = config.env_opt("GAME_CHAT_CHANNEL")
                    if not chat_chan:
                        chat_chan = str(raw_cfg.get("chat_channel"))
                    raw_cfg["chat_channel"] = chat_chan
                    cfg = App_Config(name=instance_name, **raw_cfg)
                    if not cfg.enabled:
                        continue
                    if not self.activity_manager:
                        raise SystemError("Activity_Manager not setup")
                    app = cls(bot, self.activity_manager, cfg)
                    apps[instance_name] = app
                    log.info(f"Loaded: {instance_name}")
                except Exception:
                    log.exception(f"Instantiate {instance_name}")

        await asyncio.gather(*(app.post_init() for app in apps.values()))
        for app in apps.values():
            if app.chat_channel:
                DC_Relay.register_app_channel(app.chat_channel, app)

        self.apps = apps
        self.dump_enabled()

        def permitate(trans: str, base: str, /):
            if trans:
                self._lookup[trans] = base
                self._lookup[trans.lower()] = base
                self._lookup[trans.upper()] = base
                self._lookup[trans.title()] = base
                self._lookup[trans.capitalize()] = base
                self._lookup[trans.casefold()] = base
                self._lookup[trans.swapcase()] = base

        for name, app in self.apps.items():
            permitate(app.name, name)
            permitate(app.proc_name, name)
            permitate(app.directory.name, name)
            if app.friendly:
                permitate(app.friendly, name)

    async def launch(self, name: str | App):
        if isinstance(name, App):
            app = name
        else:
            app = self.get(name)
        name = app.name
        await self.end()
        if not app.cfg.enabled:
            raise LookupError("App Not Enabled")
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

    def get(self, name: str) -> App:
        if app_name := self._lookup.get(name):
            return self.apps[app_name]
        raise ValueError(f"No such app: {name}")

    @property
    def get_current(self) -> App | None:
        return self.apps.get(self.current) if self.current else None


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
                if app.cfg.provider_alt_text:
                    if self._counter == 3:
                        self._counter = 0
                        return f"<{app.cfg.provider_alt_text}>"
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
