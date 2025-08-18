import logging

import lightbulb

from _discord import Distils
from _manager import App_Manager
from _security import Access_Control

log = logging.getLogger(__name__)

group_settings = lightbulb.Group("settings", "Commands related to app settings")  # type: ignore


async def ac_app_configs(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values() if a.settings])


async def ac_configs(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    app = ctx.get_option("app")
    to_send = []
    if app and app.value and isinstance(app.value, str):
        app = manager.get(app.value)
        if app.settings:
            to_send = app.settings.app.friendly_options
    await Distils.ac_focused_static(ctx, to_send)


async def ac_value(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    app = ctx.get_option("app")
    setting = ctx.get_option("setting")
    ctx.focused
    if (
        app
        and app.value
        and isinstance(app.value, str)
        and setting
        and setting.value
        and isinstance(setting.value, str)
    ):
        app = manager.get(app.value)
        to_send = []
        if cf := app.settings.app.get_setting(setting.value) if app.settings else None:
            if isinstance(cf.choices, list):
                to_send = cf.choices
            elif isinstance(cf.choices, dict):
                to_send = list(cf.choices.keys())
        await Distils.ac_focused_static(ctx, to_send)


@group_settings.register
class CMD_Set(
    lightbulb.SlashCommand,
    name="set",
    description="Set setting for app",
    hooks=[lightbulb.prefab.sliding_window(5, 1, "user")],
):
    app = lightbulb.string("app", "What to get settings for", autocomplete=ac_app_configs)  # type: ignore
    setting = lightbulb.string("setting", "Which setting to change/retrieve", autocomplete=ac_configs)  # type: ignore
    value = lightbulb.string(
        "value",
        "New Value | If unset the current value is returned",
        autocomplete=ac_value,  # type: ignore
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, manager: App_Manager):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"Settings.Set; {self.app}: {ctx.user.display_name}")
        app = manager.get(self.app)
        setting = app.settings.app.get_setting(self.setting) if app.settings else None

        if not setting:
            raise ModuleNotFoundError(f"{self.setting} Not Found for {self.app}")
        await acl.perm_check(ctx.user.id, setting.power_level)

        if self.value:
            if isinstance(setting.choices, dict):
                setting.update(setting.choices.get(self.value, self.value))
            else:
                setting.update(self.value)
            await ctx.respond(
                f"{app.friendly} `{setting.label}` updated: {setting.value}\n-# Settings not saved until app is launched or save cmd is run"
            )
        else:
            await ctx.respond(f"{app.friendly} `{setting.label}` currently: {setting.value}")


@group_settings.register
class CMD_Save(
    lightbulb.SlashCommand,
    name="save",
    description="Save setting for app without launching",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What to save settings for", autocomplete=ac_app_configs)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, manager: App_Manager):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"Settings.Save; {self.app}: {ctx.user.display_name}")
        app = manager.get(self.app)
        if app.settings:
            app.settings.app.save()
            await ctx.respond("Settings saved")
        else:
            raise ModuleNotFoundError


# AiviA APasz
