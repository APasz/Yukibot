import logging

import lightbulb

from _manager import App_Manager
from _security import Access_Control

log = logging.getLogger(__name__)

group_update = lightbulb.Group("update", "Commands related to app/mod upgate")  # type: ignore


async def ac_app_updates(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values() if a.updater])


@group_update.register
class CMD_UpdateApp(
    lightbulb.SlashCommand,
    name="app",
    description="Performs update on app",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What app to update", autocomplete=ac_app_updates)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, manager: App_Manager):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"update.app; {self.app}: {ctx.user.display_name}")
        app = manager.get(self.app)
        assert app.updater
        old = app.updater.version
        if new := await app.updater.base():
            if new == old:
                await ctx.respond("No new update found")
            else:
                await ctx.respond(f"{f'{app.updater.stringise(old)} > ' if old else ''}{new}")
        else:
            await ctx.respond("err")


# AiviA APasz
