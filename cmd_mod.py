import asyncio
import logging
from typing import Type, TypeVar
from pathlib import Path

import hikari
import lightbulb

from _discord import Distils
from _file import File_Utils
from _manager import App_Manager
from _utils import Utilities

log = logging.getLogger(__name__)

group_mod = lightbulb.Group("mod", "Mod Management")  # type: ignore


async def ac_mod_apps(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    await ctx.respond([a.friendly for a in manager.apps.values() if a.mods])


async def ac_all_mods(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    app = ctx.get_option("app")
    if not app or (app and not app.value):
        return []
    if not isinstance(app.value, str):
        raise ValueError(f"app must be str not {type(app.value)}")
    app = manager.get(app.value)
    if not app.mods:
        return []

    await Distils.ac_focused_mutate(
        ctx,
        {m.name: m.cfg.enabled for m in app.mods.list_mods()},
        lambda k, v: f"{'Enabled' if v else 'Disabled'}: {k}",
    )


async def ac_enabled(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    app = ctx.get_option("app")
    if not app or (app and not app.value):
        return []
    if not isinstance(app.value, str):
        raise ValueError(f"app must be str not {type(app.value)}")
    app = manager.get(app.value)
    if not app.mods:
        return []
    await Distils.ac_focused_static(ctx, [m.name for m in app.mods.list_mods() if m.cfg.enabled])


async def ac_disabled(ctx: lightbulb.AutocompleteContext, manager: App_Manager):
    app = ctx.get_option("app")
    if not app or (app and not app.value):
        return []
    if not isinstance(app.value, str):
        raise ValueError(f"app must be str not {type(app.value)}")
    app = manager.get(app.value)
    if not app.mods:
        return []
    await Distils.ac_focused_static(ctx, [m.name for m in app.mods.list_mods() if not m.cfg.enabled])


T = TypeVar("T", str, hikari.Attachment)


async def file_ops(
    target: object,
    arg: str = "mod",
    limit: int = 10,
    anno: Type[T] = str,
) -> set[T]:
    return {value for i in range(limit) if isinstance((value := getattr(target, f"{arg}{i}", None)), anno)}


@group_mod.register
class CMD_ModList(
    lightbulb.SlashCommand,
    name="list",
    description="List all mods",
    hooks=[lightbulb.prefab.sliding_window(1, 1, "global")],
):
    app = lightbulb.string("app", "Which app to to list mods for", autocomplete=ac_mod_apps)  # type: ignore
    state = lightbulb.boolean("state", "Show only Enabled=True, Disabled=False, All=Unset", default=None)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, utils: Utilities, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 0)
        log.info(f"Modding.List; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        mm = app.has_mod_manager

        mods = mm.list_names(self.state)

        log.debug(f"ModList: {mods}")
        if not mods:
            raise FileNotFoundError(f"No Mods for {app.friendly}")
        await ctx.respond(f"Modlist for {app.friendly}")

        chunks: list[str] = utils.chunket("\n".join(mods), 1950, "\n")

        for chunk in chunks:
            await ctx.respond(chunk)


@group_mod.register
class CMD_ModAdd(
    lightbulb.SlashCommand,
    name="add",
    description="Add mod",
    hooks=[lightbulb.prefab.sliding_window(5, 1, "global")],
):
    app = lightbulb.string("app", "Which app to start", autocomplete=ac_mod_apps)  # type: ignore
    mod0 = lightbulb.attachment("mod0", "File mod to add to app")
    mod1 = lightbulb.attachment("mod1", "File mod to add to app", default=None)
    mod2 = lightbulb.attachment("mod2", "File mod to add to app", default=None)
    mod3 = lightbulb.attachment("mod3", "File mod to add to app", default=None)
    mod4 = lightbulb.attachment("mod4", "File mod to add to app", default=None)
    mod5 = lightbulb.attachment("mod5", "File mod to add to app", default=None)
    mod6 = lightbulb.attachment("mod6", "File mod to add to app", default=None)
    mod7 = lightbulb.attachment("mod7", "File mod to add to app", default=None)
    mod8 = lightbulb.attachment("mod8", "File mod to add to app", default=None)
    mod9 = lightbulb.attachment("mod9", "File mod to add to app", default=None)
    atomic = lightbulb.boolean("atomic", "delete existing beforehand | default=True", default=True)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        await ctx.defer()
        log.info(f"Modding.Add; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        mm = app.has_mod_manager
        files: set[hikari.Attachment] = await file_ops(self, anno=hikari.Attachment)

        paths = await asyncio.gather(*(File_Utils.download_temp(file) for file in files))

        try:
            for path in paths:
                modcfg = mm.modcf_cls(name=path.name, directory=mm.folder)
                mod = mm.mod_cls(modcfg)
                await mod.install(path, self.atomic)
        except Exception:
            log.exception("Mod.Install")
            raise

        await ctx.respond(f"Installed {len(paths)} mods to {app.friendly}", attachments=list(files))


@group_mod.register
class CMD_ModRemove(
    lightbulb.SlashCommand,
    name="remove",
    description="Remove mod",
    hooks=[lightbulb.prefab.sliding_window(5, 1, "global")],
):
    app = lightbulb.string("app", "Which app to remove mods from", autocomplete=ac_mod_apps)  # type: ignore
    mod0 = lightbulb.string("mod0", "Mod to remove", autocomplete=ac_all_mods)  # type: ignore
    mod1 = lightbulb.string("mod1", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod2 = lightbulb.string("mod2", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod3 = lightbulb.string("mod3", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod4 = lightbulb.string("mod4", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod5 = lightbulb.string("mod5", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod6 = lightbulb.string("mod6", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod7 = lightbulb.string("mod7", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod8 = lightbulb.string("mod8", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore
    mod9 = lightbulb.string("mod9", "Mod to remove", autocomplete=ac_all_mods, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        await ctx.defer()
        log.info(f"Modding.Remove; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        mm = app.has_mod_manager
        val2 = mm.index.keys()
        files: set[str] = {distils.cat_name(i, (None, val2))[1] for i in await file_ops(self)}
        log.debug(f"{files=}")
        removed = []
        rm_errors = []
        for file in files:
            try:
                mod = mm.get(file)
                if mod.is_coremod(True):
                    await distils.perm_check(ctx.user.id, 2)
                if await mod.uninstall():
                    removed.append(mod.friendly)
            except Exception as xcp:
                log.exception("Mod.Remove")
                rm_errors.append(f"{xcp}: {file}" if file not in str(xcp) else str(xcp))
        log.debug(f"{len(removed)}: {removed} | {len(rm_errors)}: {rm_errors}")
        mod_txt = "\n".join([f"\t`{f}`" for f in removed if f])
        if removed and rm_errors:
            mod_txt += "\n\n"
        mod_txt += "\n".join([f"\t`{f}`" for f in rm_errors if f])

        await ctx.respond(f"Removed {len(removed)} mods from {app.friendly}\n{mod_txt}")


@group_mod.register
class CMD_ModDown(
    lightbulb.SlashCommand,
    name="download",
    description="Download mod",
    hooks=[lightbulb.prefab.sliding_window(5, 2, "global")],
):
    app = lightbulb.string("app", "App to download mod from", autocomplete=ac_mod_apps)  # type: ignore
    mod0 = lightbulb.string("mod0", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod1 = lightbulb.string("mod1", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod2 = lightbulb.string("mod2", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod3 = lightbulb.string("mod3", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod4 = lightbulb.string("mod4", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod5 = lightbulb.string("mod5", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod6 = lightbulb.string("mod6", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod7 = lightbulb.string("mod7", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod8 = lightbulb.string("mod8", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore
    mod9 = lightbulb.string("mod9", "Mod to download or all if not pass", autocomplete=ac_enabled, default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 0)
        await ctx.defer()
        log.info(f"Modding.Download; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        mm = app.has_mod_manager

        paths: list[Path] = [mm.folder / f for f in await file_ops(self) if f]
        if not paths:
            paths = [p.path for p in mm.index.values() if p.path.exists()]
        if not paths:
            raise FileNotFoundError(f"No Mods for {app.friendly}")
        direct = True if len(paths) > 10 else False

        log.debug(f"Sending Mods; {direct=}: {paths}")
        await distils.respond_files(ctx, paths, app_name=app.friendly, force_download=direct)


@group_mod.register
class CMD_ModToggle(
    lightbulb.SlashCommand,
    name="toggle",
    description="toggle mod",
    hooks=[lightbulb.prefab.sliding_window(5, 2, "global")],
):
    app = lightbulb.string("app", "App to toggle mod for", autocomplete=ac_mod_apps)  # type: ignore
    mod = lightbulb.string("mod", "Mod to toggle", autocomplete=ac_all_mods)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 0)
        await ctx.defer()
        log.info(f"Modding.Toggle; {self.app} | {self.mod}: {ctx.user.display_name}")

        app = manager.get(self.app)
        mm = app.has_mod_manager

        tog, name = distils.cat_name(self.mod, ({"enabled", "disabled"}, mm.index.keys()))
        mod = mm.get(name)
        await mod.toggle(False if tog == "enabled" else True)
        await mm.save_mods()
        await ctx.respond(f"{app.friendly} mod `{name.title()}`: {'Disabled' if tog == 'enabled' else 'Enabled'}")


@group_mod.register
class CMD_ModRefresh(
    lightbulb.SlashCommand,
    name="refresh",
    description="Refreash mod indexi",
    hooks=[lightbulb.prefab.sliding_window(5, 2, "global")],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        await ctx.defer()
        log.info(f"Modding.Refresh: {ctx.user.display_name}")

        refreshed: list[str] = []
        for app in manager.apps.values():
            if not app.mods:
                continue
            await app.mods.reload_mods()
            refreshed.append(app.friendly)

        await ctx.respond(f"Mod indexes refreshed for;\n\t{'\t\n'.join(refreshed)}")


@group_mod.register
class CMD_ModCoremod(
    lightbulb.SlashCommand,
    name="coremod",
    description="Toggle mod as core mod",
    hooks=[lightbulb.prefab.sliding_window(5, 2, "global")],
):
    app = lightbulb.string("app", "App to toggle coremod for", autocomplete=ac_mod_apps)  # type: ignore
    mod = lightbulb.string("mod", "Mod to coremod toggle", autocomplete=ac_all_mods)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 2)
        await ctx.defer()
        log.info(f"Modding.Coremod: {ctx.user.display_name}")

        refreshed: list[str] = []
        for app in manager.apps.values():
            if not app.mods:
                continue
            await app.mods.reload_mods()
            refreshed.append(app.friendly)

        await ctx.respond(f"Mod indexes refreshed for;\n\t{'\t\n'.join(refreshed)}")


# AiviA APasz
