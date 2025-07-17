import logging

import lightbulb

from _discord import Distils
from _manager import App_Manager

log = logging.getLogger(__name__)

group_saves = lightbulb.Group("saves", "Commands related to app saves")  # type: ignore


async def ac_app_configs(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values() if a.saves])


@group_saves.register
class CMD_SavesDownload(
    lightbulb.SlashCommand,
    name="download",
    description="Retrieve save for app",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What to get save for", autocomplete=ac_app_configs)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        log.info(f"Save.Download; {self.app}: {ctx.user.display_name}")
        raise NotImplementedError


# AiviA APasz
