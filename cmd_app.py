from __future__ import annotations

import lightbulb
from lightbulb.commands.groups import Group

import _errors
import cmd_app_console as _console
import cmd_app_manage as _manage
from _manager import App_Manager, ac_all_apps, ac_enabled_apps, ac_running_apps
from _security import Access_Control
from apps._app import App
from cmd_app_console import AppConsoleService, ac_console_apps
from cmd_app_manage import AppManageService, _app_started_response_text, log
from web_dash.links import current_node_app_url as _current_node_app_url


def __getattr__(name: str) -> object:
    if hasattr(_manage, name):
        return getattr(_manage, name)
    if hasattr(_console, name):
        return getattr(_console, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def current_node_app_url(app_name: str) -> str:
    return _current_node_app_url(app_name)


group_app: Group = lightbulb.Group("app", "App Management")  # type: ignore[reportAssignmentType]


@group_app.register
class CMD_AppStop(
    lightbulb.SlashCommand,
    name="stop",
    description="Stop a running app",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "global")],
):
    app = lightbulb.string("app", "Which running app to stop", autocomplete=ac_running_apps)  # pyright: ignore[reportAssignmentType, reportArgumentType]

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, manager: App_Manager) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await ctx.defer()
        log.info(f"App.Stop; {self.app}: {ctx.user.display_name}")

        details = await manager.end(self.app)
        apps: list[str] = []
        for proc in details:
            try:
                apps.append(manager.get(proc).friendly)
            except ValueError:
                apps.append(proc)
        await ctx.respond(f"Ended: {', '.join(sorted(apps, key=str.lower))}" if apps else "No apps found running")


@group_app.register
class CMD_AppStart(
    lightbulb.SlashCommand,
    name="start",
    description="Start an enabled app",
    hooks=[lightbulb.prefab.sliding_window(30, 1, "global")],
):
    app = lightbulb.string("app", "Which app to start", autocomplete=ac_enabled_apps)  # pyright: ignore[reportAssignmentType, reportArgumentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        app_editor: AppManageService,
        manager: App_Manager,
    ) -> None:
        del app_editor
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await ctx.defer()
        log.info(f"App.Start; {self.app}: {ctx.user.display_name}")

        app: App = manager.get(self.app)
        if blocker := manager.start_blocker(app):
            await ctx.respond(blocker.message)
            return
        await manager.launch(app)
        await ctx.respond(_app_started_response_text(app))


@group_app.register
class CMD_AppManage(
    lightbulb.SlashCommand,
    name="manage",
    description="Open the app manager",
    hooks=[lightbulb.prefab.sliding_window(30, 1, "global")],
):
    app = lightbulb.string("app", "App to manage", autocomplete=ac_all_apps, default=None)  # pyright: ignore[reportAssignmentType, reportArgumentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        app_editor: AppManageService,
        manager: App_Manager,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"App.Manage; {self.app or '<landing>'}: {ctx.user.display_name}")
        if self.app is None:
            await app_editor.open_editor(ctx=ctx, acl=acl, manager=manager)
            return
        app = manager.get(self.app)
        await app_editor.open_editor(ctx=ctx, acl=acl, manager=manager, initial_app=app)


@group_app.register
class CMD_AppConsole(
    lightbulb.SlashCommand,
    name="console",
    description="Open curated console actions for an app",
):
    app = lightbulb.string("app", "App to control", autocomplete=ac_console_apps)  # pyright: ignore[reportAssignmentType, reportArgumentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        console_editor: AppConsoleService,
        manager: App_Manager,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"App.Console; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        if not app.supports_console_actions:
            raise _errors.UnsupportedConsole(f"{app.friendly} does not support console actions")
        await console_editor.open_editor(ctx=ctx, acl=acl, manager=manager, app=app)
