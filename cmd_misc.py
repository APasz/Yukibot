import asyncio
import logging
import re
from time import time

import aiohttp
import hikari
import lightbulb

import _sys
import config
from _discord import Distils
from _manager import App_Manager, ac_app_logs

log = logging.getLogger(__name__)

group_misc = lightbulb.Group("misc", "Misc comands")  # type: ignore

CURRENCY_INPUT = r"(?i)([A-Z]{3})\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*([A-Z]{3})"


@group_misc.register
class CMD_MiscCurrency(
    lightbulb.SlashCommand,
    name="currency",
    description="Convert currency, e.g. 10aud",
    hooks=[lightbulb.prefab.sliding_window(len(config.SUPPORTED_CURRENCY) * 2, 1, "global")],
):
    value = lightbulb.string("value", "Amount and currency, e.g. '10AUD' or 'AUD 10'")
    to = lightbulb.string(
        "to",
        "Currency to convert to",
        choices=[lightbulb.Choice(c, c) for c in config.SUPPORTED_CURRENCY],
        default=None,
    )

    _quote_cache: dict[tuple[str, str], tuple[float, float]] = {}
    "(src,dst): (quote, time) "

    @classmethod
    async def convert(cls, amount: float, src: str, dst: str) -> float | None:
        now = time()
        key = (src, dst)

        cached = cls._quote_cache.get(key)

        threshold = 4 * 24 * 60 * 60
        if cached:
            age = now - cached[1]
            if age < threshold:
                return amount * cached[0]
            else:
                del cls._quote_cache[key]

        try:
            async with aiohttp.ClientSession() as session:
                params = {"amount": amount, "from": src, "to": dst, "access_key": config.EXR_TOK}
                async with session.get(config.EXCHANGE_RATE_ADDR, params=params) as resp:
                    data = await resp.json()

                    if not data.get("success"):
                        error = data.get("error", {})
                        code = error.get("code")
                        type = error.get("type")
                        log.warning(f"Currency API failed: {code=} | {type=}")
                        return None

                    quote = data["info"]["quote"]
                    cls._quote_cache[key] = (quote, now)
                    log.debug(f"Quote {src}->{dst} = {quote}")
                    return amount * quote

        except Exception as xcp:
            log.exception(f"Currency.convert failed: {xcp}")
            return None

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils):
        await distils.perm_check(ctx.user.id, 0)
        await ctx.defer()
        log.info(f"Misc.Currency: {ctx.user.display_name} | Cache={self._quote_cache}")

        raw = re.sub(r"\s+", " ", self.value.upper().replace(",", "").strip())
        match = re.fullmatch(CURRENCY_INPUT, raw)
        if not match:
            await ctx.respond("sweetheart ive no idea what you telling me, try eg `10AUD` or `AUD 10`")
            return

        src, amt_str = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
        if src not in config.SUPPORTED_CURRENCY:
            await ctx.respond(f"unsupported: `{src}`\nsupported: {', '.join(config.SUPPORTED_CURRENCY)}")
            return
        src: str

        try:
            amount = float(amt_str)
        except ValueError:
            await ctx.respond(f"darling youve given invalid value `{amt_str}`")
            return

        targets = [self.to] if self.to else [c for c in config.SUPPORTED_CURRENCY if c != src]

        conversions = {}
        for target in targets:
            result = await self.convert(amount, src, target)

            if result is None:
                log.warning(f"Retrying conversion {src}->{target} after failure")
                await asyncio.sleep(1.5)
                result = await self.convert(amount, src, target)

            conversions[target] = result
            await asyncio.sleep(0.3)

        lines = [f"{t}: {f'{v:,.3f}' if isinstance(v, float) else '**error**'}" for t, v in conversions.items()]

        await ctx.respond(f"**{amount:,.3f} {src}** converts to:\n" + "\n".join(sorted(lines, key=str.upper)))


@group_misc.register
class CMD_MiscLog(
    lightbulb.SlashCommand,
    name="logs",
    description="Retrieve log for app/system",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What to get logs for", autocomplete=ac_app_logs)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 1)
        log.info(f"Misc.Log; {self.app}: {ctx.user.display_name}")

        if self.app.lower() == "system":
            target = [(config.DIR_LOG / self.app).with_suffix(".log")]
            name = self.app
        else:
            app = manager.get(self.app)
            target = [app.dir_log]
            name = app.friendly
        await distils.respond_files(ctx, target, display_name="logs", app_name=name)


@group_misc.register
class CMD_MiscRestart(
    lightbulb.SlashCommand,
    name="restart",
    description="Restart Bot aka crash bot",
    hooks=[lightbulb.prefab.sliding_window(60, 1, "global")],
):
    sys = lightbulb.boolean("sys", "sys", default=False)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils, bot: hikari.GatewayBot, manager: App_Manager):
        await distils.perm_check(ctx.user.id, 2)
        await ctx.defer()
        log.critical(f"Misc.Restart; sys={self.sys}: {ctx.user.display_name}")
        restart_type = "system" if self.sys else "bot"
        await _sys.restart(ctx, bot, manager, restart_type)


@group_misc.register
class CMD_STDDrink(
    lightbulb.SlashCommand,
    name="standard_drink",
    description="Convert between standard drinks",
):
    value = lightbulb.integer("value", "Value")
    from_unit = lightbulb.string(
        "from", "unit to convert from", choices=[lightbulb.Choice(p, p) for p in config.STD_DRINK_GRAMS.keys()]
    )
    to_unit = lightbulb.string(
        "to", "unit to convert to", choices=[lightbulb.Choice(p, p) for p in config.STD_DRINK_GRAMS.keys()]
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, distils: Distils):
        await distils.perm_check(ctx.user.id, 0)
        log.info(f"Misc.STDDrink; value={self.value} {self.from_unit} > {self.to_unit}: {ctx.user.display_name}")

        try:
            from_grams = config.STD_DRINK_GRAMS[self.from_unit]
            to_grams = config.STD_DRINK_GRAMS[self.to_unit]
        except KeyError as e:
            raise ValueError(f"Unknown unit: {e.args[0]}")

        grams = self.value * from_grams
        result = round(grams / to_grams, 2)

        await ctx.respond(f"{self.from_unit} {self.value} converts to {self.to_unit} {result}")


# AiviA APasz
