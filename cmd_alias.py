import logging

import hikari
import lightbulb
from hikari import Embed

from _security import Access_Control
from _discord import Distils, Resolutator
from _manager import App_Manager
from config import Name_Cache

log = logging.getLogger(__name__)

group_alias = lightbulb.Group("alias", "Alias Management")  # type: ignore


async def ac_app_scopes(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond(list({a.scope.title() for a in manager.apps.values()}))


async def ac_all_names(ctx: lightbulb.AutocompleteContext, namesCache: Name_Cache):
    user_id = ctx.interaction.user.id

    names = namesCache.by_id.get(user_id)
    if not names:
        await ctx.respond([])
        return
    await ctx.respond(
        [f"General: {n}" for n in names.names | names.nicknames] + [f"{g.title()}: {n}" for g, n in names.games.items()]
    )


async def ac_all_ids(ctx: lightbulb.AutocompleteContext, namesCache: Name_Cache):
    await ctx.respond([namesCache.by_id[i].account for i in namesCache.by_id.keys()])


@group_alias.register
class CMD_AliasSet(
    lightbulb.SlashCommand,
    name="set",
    description="Set App Specific Alias",
):
    app = lightbulb.string("app", "Which app to set alias for", autocomplete=ac_app_scopes)  # type: ignore
    alias = lightbulb.string("alias", "Your new alias")
    user = lightbulb.string("user", "Other user", autocomplete=ac_all_ids, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, namesCache: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        user_id = ctx.user.id
        if self.user:
            await acl.perm_check(ctx.user.id, acl.LvL.sudo)
            user_id = namesCache.resolve_to_id(self.user)
            if not user_id:
                raise KeyError("User Not Found")
        log.info(f"Alias.Set; {self.alias} @ {self.app}: {ctx.user.display_name} > {self.user}")

        namesCache.set_game_alias(user_id, self.app, self.alias)
        await ctx.respond(
            f"{namesCache.by_id[user_id].account if self.user else ''} Alias set for {self.app.title()} to {self.alias}"
        )


@group_alias.register
class CMD_AliasAdd(
    lightbulb.SlashCommand,
    name="add",
    description="Add General Alias",
):
    alias = lightbulb.string("alias", "Alias to add")
    user = lightbulb.string("user", "Other user", autocomplete=ac_all_ids, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, namesCache: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        user_id = ctx.user.id
        if self.user:
            await acl.perm_check(ctx.user.id, acl.LvL.sudo)
            user_id = namesCache.resolve_to_id(self.user)
            if not user_id:
                raise KeyError("User Not Found")
        log.info(f"Alias.Add; {self.alias}: {ctx.user.display_name} > {self.user}")

        namesCache.add_name(user_id, self.alias, False)
        await ctx.respond(
            f"{self.alias} nickname added {f'for {namesCache.by_id[user_id].account}' if self.user else ''}"
        )


@group_alias.register
class CMD_AliasRemove(
    lightbulb.SlashCommand,
    name="remove",
    description="Remove Alias",
):
    alias = lightbulb.string("alias", "Alias to remove", autocomplete=ac_all_names)  # type: ignore
    user = lightbulb.string("user", "Other user", autocomplete=ac_all_ids, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, distils: Distils, namesCache: Name_Cache):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        user_id = ctx.user.id
        if self.user:
            await acl.perm_check(ctx.user.id, acl.LvL.sudo)
            user_id = namesCache.resolve_to_id(self.user)
            if not user_id:
                raise KeyError("User Not Found")
        log.info(f"Alias.Remove; {self.alias}: {ctx.user.display_name} > {self.user}")

        cat, name = distils.cat_name(self.alias)
        cat = cat.lower()

        if cat == "general":
            namesCache.remove_name(user_id, name)
        else:
            namesCache.remove_game_alias(user_id, cat)
        await ctx.respond(
            f"{name} removed from {cat.title()} {f'for {namesCache.by_id[user_id].account}' if self.user else ''}"
        )


@group_alias.register
class CMD_AliasList(
    lightbulb.SlashCommand,
    name="list",
    description="List All Aliases",
):
    user = lightbulb.string("user", "Other user", autocomplete=ac_all_ids, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, namesCache: Name_Cache, reso: Resolutator):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        log.info(f"Alias.List: {ctx.user.display_name} > {self.user}")

        user_id = namesCache.resolve_to_id(self.user) if self.user else ctx.user.id
        if not user_id:
            raise KeyError("Alias Lookup Failed")
        is_self = user_id == ctx.user.id

        def get_colour(usr: hikari.User | hikari.Member) -> hikari.Color | None:
            if isinstance(usr, hikari.Member):
                for role in usr.get_roles():
                    if role.colour:
                        return role.colour
            return usr.accent_colour

        if is_self:
            usr = ctx.member or ctx.user
            name = "Your"
        else:
            usr = await reso.user(user_id, ctx.guild_id)
            if not usr:
                raise ValueError("User Not Found")
            name = usr.display_name

        colour = get_colour(usr)

        names = namesCache.by_id.get(user_id)
        if not names:
            raise KeyError("UserID Not Found in Name_Cache")

        embed = Embed(title=f"{name} Aliases" if name else "Your Aliases", description="", color=colour or 0xB00F0F)
        embed.add_field(name="General", value="\n".join(sorted(names.names | names.nicknames)), inline=False)
        for g, n in names.games.items():
            embed.add_field(name=g.title(), value=n[0], inline=False)
        await ctx.respond(embed=embed)
        return


# AiviA APasz
