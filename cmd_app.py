import asyncio
import logging

import lightbulb

from _discord import Distils
from _file import File_Utils
from _manager import App_Manager, ac_all_apps, ac_enabled_apps
from _sys import Stats_System
from _utils import Utilities

log = logging.getLogger(__name__)

group_app = lightbulb.Group("app", "App Management")  # type: ignore


class NotEnoughDisk(Exception): ...


async def ac_toggle_apps(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([f"{a.cfg.enabled_txt}: {a.friendly}" for a in manager.apps.values() if a.directory.exists()])


@group_app.register
class CMD_AppEnd(
    lightbulb.SlashCommand,
    name="stop",
    description="Stop app",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "global")],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        await ctx.defer()
        log.info(f"App.End: {ctx.user.display_name}")

        details = await manager.end(manager.current)
        apps: list[str] = []
        for proc in details:
            if proc in manager._lookup:
                apps.append(manager.get(proc).friendly)
            else:
                apps.append(proc)
        await ctx.respond(f"Ended: {', '.join(sorted(apps, key=str.lower))}" if apps else "No apps found running")


@group_app.register
class CMD_AppStart(
    lightbulb.SlashCommand,
    name="start",
    description="Start app",
    hooks=[lightbulb.prefab.sliding_window(30, 1, "global")],
):
    app = lightbulb.string("app", "Which app to start", autocomplete=ac_enabled_apps)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        await ctx.defer()
        log.info(f"App.Start; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        if await manager.end():
            await asyncio.sleep(5)
        else:  # Breather to help ensure resources are released
            await asyncio.sleep(1)
        await manager.launch(app)
        await ctx.respond(f"{app.friendly} Started!")


@group_app.register
class CMD_AppToggle(
    lightbulb.SlashCommand,
    name="toggle",
    description="Toggle App",
    hooks=[lightbulb.prefab.sliding_window(60, 1, "global")],
):
    app = lightbulb.string("app", "Which app to toggle", autocomplete=ac_toggle_apps)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 2)
        log.info(f"App.Toggle; {self.app}: {ctx.user.display_name}")

        tog, name = distils.cat_name(self.app, ({"enabled", "disabled"}, manager.apps.keys()))
        app = manager.get(name)
        state = False if tog == "enabled" else True
        manager.toggle(name, state)
        await ctx.respond(f"{app.friendly}: {'Enabled' if state else 'Disabled'}")


@group_app.register
class CMD_AppDownload(
    lightbulb.SlashCommand,
    name="download",
    description="Download App",
    hooks=[lightbulb.prefab.sliding_window(60, 1, "global")],
):
    app = lightbulb.string("app", "Which app to download", autocomplete=ac_all_apps)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, utils: Utilities, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 2)
        await ctx.defer()
        log.info(f"App.Download; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        size = File_Utils.pointer_size(app.directory)
        pad_size = round(size + (size / 100 * 10))
        space = Stats_System().disk.usage.free
        if space < pad_size:
            raise NotEnoughDisk(f"{utils.humanise_bytes(space)} < {utils.humanise_bytes(pad_size)}")
        await distils.respond_files(ctx, [app.directory], display_name=app.friendly, force_download=True)


# AiviA APasz
