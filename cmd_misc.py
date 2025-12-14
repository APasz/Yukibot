from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import time
from typing import Any

import aiohttp
import hikari
import lightbulb

import _errors
import _sys
import config
from _discord import Distils
from _manager import App_Manager, ac_app_logs
from _security import Access_Control
from _utils import Utilities

log = logging.getLogger(__name__)

group_misc = lightbulb.Group("misc", "Misc comands")  # type: ignore


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
        choices=[lightbulb.Choice(c.name, c.name) for c in config.SUPPORTED_CURRENCY],
        default=None,
    )

    _quote_cache: dict[tuple[config.Currency, config.Currency], tuple[Decimal, float]] = {}
    "(src,dst): (quote, time) "

    @classmethod
    async def convert(cls, amount: Decimal, src: config.Currency, dst: config.Currency) -> Decimal | None:
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
                params = {
                    "amount": str(amount),  # ensure serialisable
                    "from": src.name,
                    "to": dst.name,
                    "access_key": config.EXR_TOK,
                }
                async with session.get(config.EXCHANGE_RATE_ADDR, params=params) as resp:
                    data = await resp.json()

            if not data.get("success"):
                err = data.get("error", {})
                log.warning(f"Currency API failed: code={err.get('code')} | type={err.get('type')}")
                return None

            rate = None
            if isinstance(data.get("info"), dict):
                r = data["info"].get("rate")
                if r is None:
                    r = data["info"].get("quote")
                if r is not None:
                    rate = Decimal(str(r))

            if "result" in data and data["result"] is not None:
                result = Decimal(str(data["result"]))
                if rate is None and amount != 0:
                    rate = result / amount
            elif rate is not None:
                result = amount * rate
            else:
                log.warning("Currency API: neither info.rate/quote nor result found")
                return None

            if rate is not None:
                cls._quote_cache[key] = (rate, now)
            return result

        except Exception as xcp:
            log.exception(f"Currency.convert failed: {xcp}")
            return None

    @staticmethod
    def _parse_decimal(num_str: str) -> Decimal | None:
        if not num_str:
            return None

        digits = "".join(ch for ch in num_str if ch.isdigit() or ch in ".,")
        if not digits:
            return None

        if "." in digits and "," in digits:
            digits = digits.replace(",", "")
        elif "," in digits:
            digits = digits.replace(".", "").replace(",", ".")
        else:
            pass

        try:
            return Decimal(digits)
        except InvalidOperation:
            return None

    @classmethod
    def number(cls, string: str) -> tuple[Decimal | None, str | None]:
        amount = cls._parse_decimal(string.replace(" ", "").replace("_", "").replace("+", "").replace("-", ""))
        if amount is None:
            return None, None

        token = "".join(ch for ch in string if not (ch.isdigit() or ch in ".,"))
        if token:
            print(f" Number: {amount=} | {token=!r}")
            return amount, token.upper().strip()
        return None, None

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        await ctx.defer()
        log.info(f"Misc.Currency: {ctx.user.display_name} | Cache={self._quote_cache}")

        amount, token = self.number(self.value)
        if not amount:
            raise _errors.Missing("Number")
        if not token:
            raise _errors.Unparseable(f"Input: {self.value}")
        if not (src := config.CURRENCY_MAP.get(token, None)):
            raise _errors.Unsupported(f"Currency: {token}")
        if self.to:
            targets = [config.Currency[self.to.upper()]]
        else:
            targets = [c for c in config.SUPPORTED_CURRENCY if c != src]

        conversions = {}
        for target in targets:
            result = await self.convert(amount, src, target)

            if result is None:
                log.warning(f"Retrying conversion {src}->{target} after failure")
                await asyncio.sleep(1.5)
                result = await self.convert(amount, src, target)

            conversions[target] = result
            await asyncio.sleep(0.3)

        def _fmt(v: Decimal | None) -> str:
            return f"{v:,.3f}" if isinstance(v, (Decimal, float, int)) else "**error**"

        lines = [f"{t.name}: {_fmt(v)}" for t, v in conversions.items()]

        await ctx.respond(
            f"**{amount:,.3f} {src.name.upper()}** converts to:\n" + "\n".join(sorted(lines, key=str.upper))
        )


@group_misc.register
class CMD_MiscLog(
    lightbulb.SlashCommand,
    name="logs",
    description="Retrieve log for app/system",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "user")],
):
    app = lightbulb.string("app", "What to get logs for", autocomplete=ac_app_logs)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, distils: Distils, manager: App_Manager):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
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
    sys = lightbulb.boolean("sys", "System", default=False)
    silent = lightbulb.boolean("silent", "Suppress shutdown/startup messages", default=False)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, bot: hikari.GatewayBot, manager: App_Manager):
        await acl.perm_check(ctx.user.id, acl.LvL.sudo)
        await ctx.defer()
        log.critical(f"Misc.Restart; sys={self.sys}: {ctx.user.display_name}")
        restart_type = "system" if self.sys else "bot"
        await _sys.restart(ctx, bot, manager, restart_type, self.silent)


@group_misc.register
class CMD_STDDrink(
    lightbulb.SlashCommand,
    name="standard_drink",
    description="Convert between standard drinks",
):
    value = lightbulb.number("value", "Value")
    from_unit = lightbulb.string(
        "from",
        "unit to convert from",
        choices=lightbulb.utils.to_choices([str(s) for s in config.STD_DRINK_GRAMS.keys()]),
    )
    to_unit = lightbulb.string(
        "to",
        "unit to convert to",
        choices=lightbulb.utils.to_choices([str(s) for s in config.STD_DRINK_GRAMS.keys()]),
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        log.info(f"Misc.STDDrink; value={self.value} {self.from_unit} > {self.to_unit}: {ctx.user.display_name}")

        try:
            from_grams = config.STD_DRINK_GRAMS[self.from_unit]
            to_grams = config.STD_DRINK_GRAMS[self.to_unit]
        except KeyError as e:
            raise ValueError(f"Unknown unit: {e.args[0]}")

        grams = self.value * from_grams
        result = round(grams / to_grams, 2)

        await ctx.respond(f"{self.from_unit} {self.value} converts to {self.to_unit} {result}")


MAX_INLINE_LEN = 1800
MAX_ITEMS = 10_000
EMBED_MAX_FIELDS = 25
EMBED_MAX_TOTAL = 6000
EMBEDS_MAX_PER_MESSAGE = 10
RANLIST_STORE_ROOT = Path("./data/ranlists")
RANLIST_STORE_ROOT.mkdir(parents=True, exist_ok=True)


def _store_path_for_user(user_id: hikari.Snowflakeish) -> Path:
    return RANLIST_STORE_ROOT / f"user_{user_id}.json"


def _store_path_for_guild(guild_id: hikari.Snowflakeish) -> Path:
    return RANLIST_STORE_ROOT / f"guild_{guild_id}.json"


async def _load_store_path(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    def _read():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    return await asyncio.to_thread(_read)


async def _save_store_path(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write():
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        tmp.replace(path)

    await asyncio.to_thread(_write)


async def _load_user_store(user_id: hikari.Snowflakeish) -> dict[str, Any]:
    return await _load_store_path(_store_path_for_user(user_id))


async def _save_user_store(user_id: hikari.Snowflakeish, data: dict[str, Any]) -> None:
    await _save_store_path(_store_path_for_user(user_id), data)


async def _load_guild_store(guild_id: hikari.Snowflakeish) -> dict[str, Any]:
    return await _load_store_path(_store_path_for_guild(guild_id))


async def _save_guild_store(guild_id: hikari.Snowflakeish, data: dict[str, Any]) -> None:
    await _save_store_path(_store_path_for_guild(guild_id), data)


@dataclass(frozen=True)
class Item:
    # 'key' is the stable dedupe key (string). For strings it's the cleaned text.
    # For JSON objects/arrays it's a canonical JSON dump.
    key: str
    # 'payload' is what we’ll emit at the end (string or JSON value).
    payload: Any
    weight: float = 1.0
    # 'is_json' flags whether payload is non-string JSON (affects output format).
    is_json: bool = False


def _serialise_items(items: list[Item]) -> list[dict[str, Any]]:
    # Keep everything we need to reconstruct Items exactly.
    out = []
    for it in items:
        out.append(
            {
                "key": it.key,
                "payload": it.payload,
                "weight": it.weight,
                "is_json": it.is_json,
            }
        )
    return out


def _deserialise_items(raw: list[dict[str, Any]]) -> list[Item]:
    out: list[Item] = []
    for d in raw:
        out.append(
            Item(
                key=d["key"],
                payload=d["payload"],
                weight=float(d.get("weight", 1.0)),
                is_json=bool(d.get("is_json", False)),
            )
        )
    return out


def _canon_json_key(obj: Any) -> str:
    # Stable, whitespace-free canonicalisation for dedupe and ordering keys
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _split_inline_list(s: str) -> list[str]:
    s = s.strip()
    if not s:
        return []
    if "\n" in s or "\r" in s:
        parts = s.splitlines()
    else:
        if any(sep in s for sep in (",", ";", "|")):
            for sep in (",", ";", "|"):
                s = s.replace(sep, "\n")
            parts = s.splitlines()
        else:
            parts = s.split()
    return [p.strip() for p in parts if p.strip()]


def _items_from_txt(data: str) -> list[Item]:
    names = _split_inline_list(data)
    return [Item(key=n, payload=n, weight=1.0, is_json=False) for n in names]


def _items_from_json_any(data: str) -> list[Item]:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e

    items: list[Item] = []

    # Case 1: list of strings
    if isinstance(obj, list) and all(isinstance(x, str) for x in obj):
        for x in obj:
            n = x.strip()
            if n:
                items.append(Item(key=n, payload=n, weight=1.0, is_json=False))
        return items

    # Case 2: dict of {name: weight}
    if isinstance(obj, dict):
        for k, v in obj.items():
            name = str(k).strip()
            if not name:
                continue
            try:
                w = float(v)
            except Exception:
                raise ValueError(f"Weight for '{k}' must be numeric, got {type(v).__name__}")
            if not math.isfinite(w) or w <= 0:
                raise ValueError(f"Weight for '{k}' must be a positive finite number")
            items.append(Item(key=name, payload=name, weight=w, is_json=False))
        return items

    # Case 3: list of dicts
    if isinstance(obj, list) and all(isinstance(x, dict) for x in obj):
        # Try weighted variants first
        ok = True
        tmp: list[Item] = []
        for el in obj:
            if "item" in el:
                name = str(el["item"]).strip()
                if not name:
                    continue
                w = float(el.get("weight", 1.0))
                if not math.isfinite(w) or w <= 0:
                    raise ValueError(f"Weight for '{name}' must be a positive finite number")
                tmp.append(Item(key=name, payload=name, weight=w, is_json=False))
            elif len(el) == 1:
                ((k, v),) = el.items()
                name = str(k).strip()
                if not name:
                    continue
                w = float(v)
                if not math.isfinite(w) or w <= 0:
                    raise ValueError(f"Weight for '{name}' must be a positive finite number")
                tmp.append(Item(key=name, payload=name, weight=w, is_json=False))
            else:
                ok = False
                break

        if ok and tmp:
            return tmp

        # Otherwise: treat each dict as an opaque JSON item with uniform weight
        items = [Item(key=_canon_json_key(el), payload=el, weight=1.0, is_json=True) for el in obj]
        return items

    # Case 4: list with mixed JSON types (strings/dicts/arrays)
    if isinstance(obj, list):
        items = []
        for el in obj:
            if isinstance(el, str):
                n = el.strip()
                if n:
                    items.append(Item(key=n, payload=n, weight=1.0, is_json=False))
            elif isinstance(el, (dict, list)):
                items.append(Item(key=_canon_json_key(el), payload=el, weight=1.0, is_json=True))
            else:
                raise ValueError(f"Unsupported JSON item type in list: {type(el).__name__}")
        if items:
            return items

    raise ValueError(
        "Unsupported JSON shape. Use list[str], dict[str, number], list[{'item','weight'}], or list[dict]."
    )


def _weighted_shuffle(items: Sequence[Item]) -> list[Item]:
    if all(abs(it.weight - 1.0) < 1e-12 for it in items):
        out = list(items)
        random.shuffle(out)
        return out

    keyed = []
    for it in items:
        w = float(it.weight)
        if not math.isfinite(w) or w <= 0:
            raise ValueError(f"Invalid weight for item with key '{it.key}': {w}")
        u = max(random.random(), 1e-12)
        key = -math.log(u) / w
        keyed.append((key, it))
    keyed.sort(key=lambda kv: kv[0])
    return [it for _, it in keyed]


def _dedupe(items: Iterable[Item]) -> list[Item]:
    seen: set[str] = set()
    out: list[Item] = []
    for it in items:
        if it.key in seen:
            continue
        seen.add(it.key)
        out.append(it)
    return out


def _is_wagon_list(items: list[Item]) -> bool:
    # All payloads must be dicts with exactly these keys
    for it in items:
        if not isinstance(it.payload, dict):
            return False
        d = it.payload
        if set(d.keys()) != {"ID", "Class", "Operator"}:
            return False
        # quick sanity types
        if not all(isinstance(d[k], str) and d[k].strip() for k in ("ID", "Class", "Operator")):
            return False
    return True


def _dedupe_wagons_by_id(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    out: list[Item] = []
    for it in items:
        wid = it.payload["ID"].strip()
        if wid in seen:
            continue
        seen.add(wid)
        out.append(it)
    return out


def _build_wagon_embeds(items: list[Item]) -> list[hikari.Embed]:
    """
    Build paginated embeds. Each item becomes one field:
    Name: "ID"
    Value: "Class
            Operator"
    """
    embeds: list[hikari.Embed] = []
    # chunk into groups of up to 25
    for i in range(0, len(items), EMBED_MAX_FIELDS):
        chunk = items[i : i + EMBED_MAX_FIELDS]

        emb = hikari.Embed(
            title="Randomised Wagons",
        )
        total_chars = len(emb.title or "") + len(emb.description or "")

        for it in chunk:
            title = f"{it.payload['ID']}"
            value = f"Class: {it.payload['Class']}\nOperator: {it.payload['Operator']}"
            # keep field sizes conservative
            title = title[:256]
            value = value[:1024]
            # rough guard against 6k cap
            if total_chars + len(title) + len(value) + 20 >= EMBED_MAX_TOTAL:
                break
            emb.add_field(name=title, value=value, inline=False)
            total_chars += len(title) + len(value) + 20

        embeds.append(emb)
        if len(embeds) >= EMBEDS_MAX_PER_MESSAGE:
            # We won’t explode the message with more; caller should fallback to file beyond this.
            break

    return embeds


def _format_output(items: Sequence[Item]) -> tuple[str | None, bytes | None, str | None, list[hikari.Embed] | None]:
    """
    Extended: now returns optional embeds too
    Preference order:
        1) If wagon-list shape and fits, return embeds
        2) Else if any JSON items, return JSON blob
        3) Else text
    """
    # 1) Special-case wagons
    if items and all(it.is_json for it in items) and _is_wagon_list(list(items)):
        wagons = _dedupe_wagons_by_id(list(items))
        # If too many to fit in 10 embeds * 25 fields, just bail to JSON file
        if len(wagons) <= EMBEDS_MAX_PER_MESSAGE * EMBED_MAX_FIELDS:
            embeds = _build_wagon_embeds(wagons)
            # If somehow produced zero fields (shouldn’t), fall through to file
            if embeds and any(e.fields for e in embeds):
                return None, None, None, embeds
        # fallback to JSON file below

    # 2) JSON output if any non-string JSON payloads
    if any(it.is_json for it in items):
        payloads = [it.payload for it in items]
        data = json.dumps(payloads, ensure_ascii=False, indent=4).encode("utf-8")
        return None, data, "application/json", None

    # 3) Plain text
    text = "\n".join(str(it.payload) for it in items)
    return text, None, None, None


async def _save_set(ctx: lightbulb.Context, name: str, items: list[Item]) -> None:
    uid = ctx.user.id
    gid = ctx.guild_id
    if gid:  # guild context: write guild + link in user
        gstore = await _load_guild_store(gid)
        gstore[name] = {
            "items": _serialise_items(items),
            "meta": {
                "author_id": str(uid),
                "author_tag": ctx.user.username,
                "saved_at": int(time()),
                "count": len(items),
            },
        }
        await _save_guild_store(gid, gstore)

        ustore = await _load_user_store(uid)
        guild = ctx.interaction.get_guild()
        ustore[name] = {
            "type": "link",
            "target": {"guild_id": hikari.Snowflake(gid), "guild_name": guild.name if guild else "", "name": name},
            "meta": {"created_by": hikari.Snowflake(uid), "created_at": int(time())},
        }
        await _save_user_store(uid, ustore)
        return

    # DM: user store only, unless name is a link
    ustore = await _load_user_store(uid)
    existing = ustore.get(name)
    if existing and existing.get("type") == "link":
        tgt = existing["target"]
        gstore = await _load_guild_store(hikari.Snowflake(tgt["guild_id"]))
        gstore[tgt["name"]] = {
            "items": _serialise_items(items),
            "meta": {
                "author_id": hikari.Snowflake(uid),
                "author_tag": ctx.user.username,
                "saved_at": int(time()),
                "count": len(items),
            },
        }
        await _save_guild_store(hikari.Snowflake(tgt["guild_id"]), gstore)
    else:
        ustore[name] = {
            "type": "local",
            "items": _serialise_items(items),
            "meta": {
                "author_id": hikari.Snowflake(uid),
                "author_tag": ctx.user.username,
                "saved_at": int(time()),
                "count": len(items),
            },
        }
        await _save_user_store(uid, ustore)


def _encode_selector(scope: str, name: str) -> str:
    return f"{'g' if scope == 'guild' else 'u'}:{name}"


def _decode_selector(s: str) -> tuple[str, str] | None:
    # Returns ("guild"|"user", name) or None if it's a legacy unscoped value.
    if not isinstance(s, str):
        return None
    if len(s) > 2 and s[1] == ":" and s[0] in ("g", "u"):
        return ("guild" if s[0] == "g" else "user", s[2:])
    return None


async def _ac_pairs_for_dm(user_id: hikari.Snowflakeish) -> dict[str, object]:
    u = await _load_user_store(user_id)
    pairs: dict[str, object] = {}

    for n, entry in sorted(u.items()):
        et = entry.get("type", "local")
        if et == "local":
            # purely private
            label = f"🔒 • {n}"
            pairs[label] = _encode_selector("user", n)
        elif et == "link":
            tgt = entry.get("target", {})
            tgt_gid = tgt.get("guild_id")
            tgt_gname = tgt.get("guild_name", tgt_gid)
            # call it public link so user knows it’s backed by a guild
            label = f"🔗 • {n} [{tgt_gname}]"
            pairs[label] = _encode_selector("user", n)
        else:
            continue

    return pairs


# 👥
# 🔗
# 🔒


async def _ac_pairs_for_guild(user_id: hikari.Snowflakeish, guild_id: hikari.Snowflakeish) -> dict[str, object]:
    uid = int(user_id)
    gid = int(guild_id)

    u = await _load_user_store(uid)
    g = await _load_guild_store(gid)

    guild_names = set(g.keys())

    # Partition user entries into locals vs links (and note links targeting this guild+name)
    user_local: set[str] = set()
    user_link_same_guild: set[str] = set()
    user_link_other: dict[str, tuple[int, str]] = {}  # name -> (target_gid, target_name)

    for n, entry in u.items():
        et = entry.get("type", "local")
        if et == "local":
            user_local.add(n)
        elif et == "link":
            tgt = entry.get("target", {})
            tgt_gid = int(tgt.get("guild_id", 0))
            tgt_name = str(tgt.get("name", ""))
            if tgt_gid == gid and tgt_name == n:
                user_link_same_guild.add(n)
            else:
                user_link_other[n] = (tgt_gid, tgt_name)
        else:
            # Unknown types are ignored on purpose
            continue

    pairs: dict[str, object] = {}

    # 1) Add guild entries
    for n in sorted(guild_names):
        pairs[n] = _encode_selector("guild", n)

    # 2) Add user locals where there is NO guild entry with same name -> show as plain (no lock)
    for n in sorted(user_local - guild_names):
        pairs[n] = _encode_selector("user", n)

    # 3) Real conflicts: guild name AND user-local with same name -> show both, distinguished
    for n in sorted(user_local & guild_names):
        pairs[f"👥 • {n}"] = _encode_selector("guild", n)
        pairs[f"🔒 • {n}"] = _encode_selector("user", n)

    # 4) User links to OTHER guilds: they don’t conflict with this guild’s name space, so show plainly.
    #    If they collide with an existing plain label, prefix to avoid label collision.
    for n, (tgid, tname) in sorted(user_link_other.items()):
        label = n
        if label in pairs:  # avoid label overwrite
            label = f"{n} (linked)"
        pairs[label] = _encode_selector("user", n)

    # 5) IGNORE user_link_same_guild completely (that’s the confusing duplicate)
    return pairs


async def ac_use_saved(ctx: lightbulb.AutocompleteContext):
    uid = ctx.interaction.user.id
    gid = ctx.interaction.guild_id

    pairs = await _ac_pairs_for_guild(uid, gid) if gid else await _ac_pairs_for_dm(uid)

    def caller(label: str, token: object) -> tuple[str, str]:
        return (label, str(token))

    await Distils.ac_focused_mutate(ctx, pairs, caller)


async def _resolve_saved(ctx: lightbulb.Context, value: str) -> list[Item]:
    uid = int(ctx.user.id)
    gid = ctx.guild_id
    parsed = _decode_selector(value)

    if parsed:
        scope, name = parsed
        if scope == "guild":
            if not gid:
                raise KeyError("Guild-scoped list selected outside a guild")
            gstore = await _load_guild_store(int(gid))
            entry = gstore.get(name)
            if not entry:
                raise KeyError(f"No guild list named '{name}'")
            return _deserialise_items(entry["items"])
        else:
            ustore = await _load_user_store(uid)
            entry = ustore.get(name)
            if not entry:
                raise KeyError(f"No private list named '{name}'")
            etype = entry.get("type", "local")
            if etype == "local":
                return _deserialise_items(entry["items"])
            if etype == "link":
                tgt_gid = int(entry["target"]["guild_id"])
                gstore = await _load_guild_store(tgt_gid)
                gentry = gstore.get(entry["target"]["name"])
                if not gentry:
                    raise KeyError(f"Linked guild set '{entry['target']['name']}' no longer exists")
                return _deserialise_items(gentry["items"])
            raise ValueError(f"Unknown entry type for '{name}': {etype}")

    # Legacy path: unscoped string. Prefer guild if present, else user.
    if gid:
        gstore = await _load_guild_store(int(gid))
        if value in gstore:
            return _deserialise_items(gstore[value]["items"])
    ustore = await _load_user_store(uid)
    entry = ustore.get(value)
    if not entry:
        raise KeyError(f"No saved list named '{value}'")
    if entry.get("type", "local") == "local":
        return _deserialise_items(entry["items"])
    tgt_gid = int(entry["target"]["guild_id"])
    gstore = await _load_guild_store(tgt_gid)
    gentry = gstore.get(entry["target"]["name"])
    if not gentry:
        raise KeyError(f"Linked guild set '{entry['target']['name']}' no longer exists")
    return _deserialise_items(gentry["items"])


@group_misc.register
class CMD_RanList(
    lightbulb.SlashCommand,
    name="randomise_list",
    description="Randomise a list (no duplicates). TXT/JSON supported, with optional weights",
):
    list_str = lightbulb.string(
        "list",
        "Inline list. Newlines preferred; commas/semicolons/pipes also work",
        default=None,
    )
    list_file = lightbulb.attachment(
        "list_file",
        "TXT (newline-separated) or JSON (list[str], {name:weight}, list[{'item','weight'}], list[dict])",
        default=None,
    )
    use_saved = lightbulb.string(
        "use_saved",
        "Use a previously saved list by name",
        default=None,
        autocomplete=ac_use_saved,
    )
    save_as = lightbulb.string(
        "save_as",
        "After parsing/deduping, save this list under a name",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        log.info(
            "Misc.RanList; list_str=%r file=%r use_saved=%r (parsed=%r) save_as=%r user=%s",
            self.list_str,
            getattr(self.list_file, "filename", None),
            self.use_saved,
            _decode_selector(self.use_saved) if self.use_saved else None,
            self.save_as,
            ctx.user.display_name,
        )

        items: list[Item] = []

        # 1) If use_saved provided, load that and skip new input

        if self.use_saved:
            try:
                items = await _resolve_saved(ctx, self.use_saved)
            except KeyError as e:
                await ctx.respond(str(e), flags=hikari.MessageFlag.EPHEMERAL)
                return
            except Exception as e:
                await ctx.respond(f"Failed to load saved list: {e}", flags=hikari.MessageFlag.EPHEMERAL)
                return
        else:
            # 2) Else parse incoming text/file as before
            if self.list_str:
                items.extend(_items_from_txt(self.list_str))

            if self.list_file:
                try:
                    raw = await self.list_file.read()
                except Exception as e:
                    await ctx.respond(f"Could not read attachment: {e}", flags=hikari.MessageFlag.EPHEMERAL)
                    return

                data = raw.decode("utf-8", errors="replace")
                ext = (self.list_file.extension or "").lower()

                try:
                    if ext == "txt" or not ext:
                        items.extend(_items_from_txt(data))
                    elif ext == "json":
                        items.extend(_items_from_json_any(data))
                    else:
                        await ctx.respond(
                            f"Unsupported file extension: .{ext}. Use .txt or .json",
                            flags=hikari.MessageFlag.EPHEMERAL,
                        )
                        return
                except ValueError as ve:
                    await ctx.respond(f"Input error: {ve}", flags=hikari.MessageFlag.EPHEMERAL)
                    return

        # Dedupe
        if items:
            # Special-case wagon format still handled by _is_wagon_list if you kept that code.
            items = _dedupe(items)

        if not items:
            await ctx.respond("No items found.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        if len(items) > MAX_ITEMS:
            await ctx.respond(
                f"Too many items ({len(items)}). Hard cap is {MAX_ITEMS}.", flags=hikari.MessageFlag.EPHEMERAL
            )
            return

        # 3) Optional save
        if self.save_as:
            name = self.save_as.strip()
            if not name:
                await ctx.respond("Save name cannot be empty.", flags=hikari.MessageFlag.EPHEMERAL)
                return
            await _save_set(ctx, name, items)

        # 4) Shuffle and respond (uses the embed/JSON/text logic you already have)
        try:
            shuffled = _weighted_shuffle(items)
        except ValueError as ve:
            await ctx.respond(f"Weight error: {ve}", flags=hikari.MessageFlag.EPHEMERAL)
            return

        text, blob, mime, embeds = _format_output(shuffled)
        preface = f"Randomised {len(shuffled)} unique item(s):"

        if embeds is not None and len(embeds) <= EMBEDS_MAX_PER_MESSAGE:
            await ctx.respond(preface, embeds=embeds)
            return

        if blob is not None:
            fname = "randomised_list.json" if mime == "application/json" else "randomised_list.txt"
            file = hikari.Bytes(io.BytesIO(blob), fname)
            await ctx.respond(preface, attachment=file)
            return

        out_text = text or ""
        if len(preface) + 1 + len(out_text) <= MAX_INLINE_LEN:
            await ctx.respond(f"{preface}\n```\n{out_text}\n```")
        else:
            bio = io.BytesIO(out_text.encode("utf-8"))
            file = hikari.Bytes(bio, "randomised_list.txt")
            await ctx.respond(preface, attachment=file)


@group_misc.register
class CMD_TimeFormat(
    lightbulb.SlashCommand,
    name="time",
    description="Generate a timestamp label",
):
    formats = {
        "Short Time": "<t:{}:t>",
        "Long Time": "<t:{}:T>",
        "Short Date": "<t:{}:d>",
        "Long Date": "<t:{}:D>",
        "Long Date / Short Time": "<t:{}:f>",
        "Full Date / Short Time": "<t:{}:F>",
        "Short Date / Short Time": "<t:{}:s>",
        "Short Date / Medium Time": "<t:{}:S>",
        "Relative Time": "<t:{}:R>",
    }
    # Y=year, MO=month, W=week, D=day, H=hour, MI=minute, S=second
    rounds = ["Y", "MO", "W", "D", "H", "MI", "S"]

    time = lightbulb.string(
        "time",
        "2h, 3h45m, 1y4m, 2y3mo5d9m, 1w2d, 2:30 | 1:02:03, 2:03:12:00:00 | Epoch seconds. Supports + - , _",
        min_length=1,
        max_length=32,  # give room for '2:03:12:00:00' etc.
    )
    output = lightbulb.string(
        "format",
        "Which format to use",
        choices=[lightbulb.Choice(name, val) for name, val in formats.items()],
        default="Short Date/Time",
    )
    rounding = lightbulb.string(
        "round",
        "Round to closest Year, Month, Week, Day, Hour, Minute, or Second",
        choices=lightbulb.utils.to_choices(rounds),
        default="S",
    )

    @staticmethod
    def _start_of_next_month(dt):
        first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # jump to next month by going to day 28 and adding 4 days
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        return first, nxt

    @staticmethod
    def _start_of_next_year(dt):
        first = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = first.replace(year=first.year + 1)
        return first, nxt

    @classmethod
    def _round_wallclock(cls, dt: datetime, unit: str) -> datetime:
        """Round aware dt to nearest unit in its own timezone."""
        # Work in the local tz for human-wallclock rounding, then return with same tzinfo.
        tz = dt.tzinfo or timezone.utc
        local = dt.astimezone(tz)

        if unit == "S":
            # nearest second
            if local.microsecond >= 500_000:
                local = local + timedelta(seconds=1)
            return local.replace(microsecond=0)

        if unit == "MI":
            base = local.replace(second=0, microsecond=0)
            if local.second >= 30:
                base += timedelta(minutes=1)
            return base

        if unit == "H":
            base = local.replace(minute=0, second=0, microsecond=0)
            # nearest hour
            if (local.minute, local.second, local.microsecond) >= (30, 0, 0):
                base += timedelta(hours=1)
            return base

        if unit == "D":
            base = local.replace(hour=0, minute=0, second=0, microsecond=0)
            # nearest day (noon cutoff)
            if (local.hour, local.minute, local.second, local.microsecond) >= (12, 0, 0, 0):
                base += timedelta(days=1)
            return base

        if unit == "W":
            # ISO week: Monday=0
            dow = local.weekday()
            start = (local - timedelta(days=dow)).replace(hour=0, minute=0, second=0, microsecond=0)
            next_start = start + timedelta(days=7)
            mid = start + timedelta(days=3, hours=12)  # halfway point
            return next_start if local >= mid else start

        if unit == "MO":
            start, next_start = cls._start_of_next_month(local)
            mid = start + (next_start - start) / 2
            return next_start if local >= mid else start

        if unit == "Y":
            start, next_start = cls._start_of_next_year(local)
            mid = start + (next_start - start) / 2
            return next_start if local >= mid else start

        # fallback: no rounding
        return local

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, utils: Utilities):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        log.info(
            "Misc.TimeFormat; time=%s round=%s fmt=%s user=%s",
            self.time,
            self.rounding,
            self.output,
            ctx.user.display_name,
        )

        # Use the same tz you wired into parse_time; swap tz if you track per-user prefs.
        tz = timezone.utc

        ts = utils.parse_time(self.time, tz=tz)
        if not ts:
            raise ValueError("Unknown time input")

        rounded = self._round_wallclock(ts, self.rounding)
        rounded_utc = rounded.astimezone(timezone.utc)
        epoch = int(rounded_utc.timestamp())
        txt = self.formats[self.output].format(epoch)
        await ctx.respond(txt)


# AiviA APasz
