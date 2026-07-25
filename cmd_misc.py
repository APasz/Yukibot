from __future__ import annotations

import io
import json
import logging
import math
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Any

import aiohttp
import hikari
import lightbulb

import _errors
import config
from currency_conversion import CurrencyConverter
from standard_drinks import format_standard_drink_range, standard_drink_conversion, standard_drink_units
from _async_utils import run_blocking
from _discord import Distils
from _security import Access_Control
from _utils import Utilities

log = logging.getLogger(__name__)

group_misc = lightbulb.Group("misc", "Misc comands")  # type: ignore


class TZ_LOCS(StrEnum):
    MELBOURNE = "Australia/Melbourne"
    LONDON = "Europe/London"
    ZURICH = "Europe/Zurich"
    HELSINKI = "Europe/Helsinki"


USER_TZ = {
    375547210760454145: TZ_LOCS.MELBOURNE,
    1286408555615883275: TZ_LOCS.LONDON,
    365865021215080448: TZ_LOCS.ZURICH,
    1238890673709781017: TZ_LOCS.HELSINKI,
    966684377985732618: TZ_LOCS.LONDON,
    1340971786942025781: TZ_LOCS.MELBOURNE,
    792857784508219404: TZ_LOCS.MELBOURNE,
    1449629470615928872: TZ_LOCS.MELBOURNE,
}

COMMON_OFFSET_LOCATIONS = {
    "UTC-12:00": "Baker Island",
    "UTC-11:00": "Pago Pago",
    "UTC-10:00": "Honolulu",
    "UTC-09:30": "Marquesas",
    "UTC-09:00": "Anchorage",
    "UTC-08:00": "Los Angeles",
    "UTC-07:00": "Denver",
    "UTC-06:00": "Chicago",
    "UTC-05:00": "New York",
    "UTC-04:00": "Halifax",
    "UTC-03:30": "St Johns",
    "UTC-03:00": "Buenos Aires",
    "UTC-02:00": "South Georgia",
    "UTC-01:00": "Azores",
    "UTC+00:00": "UTC",
    "UTC+01:00": "Berlin",
    "UTC+02:00": "Cairo",
    "UTC+03:00": "Riyadh",
    "UTC+03:30": "Tehran",
    "UTC+04:00": "Dubai",
    "UTC+04:30": "Kabul",
    "UTC+05:00": "Karachi",
    "UTC+05:30": "New Delhi",
    "UTC+05:45": "Kathmandu",
    "UTC+06:00": "Dhaka",
    "UTC+06:30": "Yangon",
    "UTC+07:00": "Bangkok",
    "UTC+08:00": "Singapore",
    "UTC+08:45": "Eucla",
    "UTC+09:00": "Tokyo",
    "UTC+09:30": "Adelaide",
    "UTC+10:00": "Sydney",
    "UTC+10:30": "Lord Howe",
    "UTC+11:00": "Noumea",
    "UTC+12:00": "Auckland",
    "UTC+12:45": "Chatham",
    "UTC+13:00": "Apia",
    "UTC+14:00": "Kiritimati",
}


def _user_tz_name(user_id: hikari.Snowflakeish) -> str | None:
    tz_loc = USER_TZ.get(int(user_id))
    return str(tz_loc) if tz_loc else None


def _tz_offset_label(tz_name: str) -> str:
    tz = Utilities.parse_timezone(tz_name)
    if tz is None:
        return "+00:00"

    offset = datetime.now(tz).utcoffset() or timedelta()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    minutes = abs(total_minutes)
    hours, mins = divmod(minutes, 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def _tz_location_label(tz_name: str) -> str:
    if tz_name.startswith("UTC"):
        return COMMON_OFFSET_LOCATIONS.get(tz_name, "")
    return tz_name.rsplit("/", 1)[-1].replace("_", " ")


def _format_tz_label(tz_name: str) -> str:
    offset = _tz_offset_label(tz_name)
    location = _tz_location_label(tz_name)
    return f"{offset} {location}".strip()


def _build_timezone_offsets() -> list[str]:
    offsets: list[str] = ["UTC"]
    for total_minutes in range(-(12 * 60), (14 * 60) + 1, 15):
        sign = "+" if total_minutes >= 0 else "-"
        minutes = abs(total_minutes)
        hours, mins = divmod(minutes, 60)
        offsets.append(f"UTC{sign}{hours:02d}:{mins:02d}")
    return offsets


TIMEZONE_OFFSETS = _build_timezone_offsets()


async def ac_timezones(ctx: lightbulb.AutocompleteContext[str]) -> None:
    choices: dict[str, object] = {}
    user_tz = _user_tz_name(ctx.interaction.user.id)
    special = sorted({str(tz_loc) for tz_loc in USER_TZ.values()})

    if user_tz and user_tz in special:
        special = [user_tz] + [tz_name for tz_name in special if tz_name != user_tz]

    for tz_name in special:
        choices[_format_tz_label(tz_name)] = tz_name
    for offset in TIMEZONE_OFFSETS:
        choices[_format_tz_label(offset)] = offset

    await Distils.ac_focused_mutate(ctx, choices, lambda k, v: (k, str(v)))


@group_misc.register
class CMD_MiscCurrency(
    lightbulb.SlashCommand,
    name="currency",
    description="Convert currency, e.g. 10aud",
    hooks=[lightbulb.prefab.sliding_window(len(config.SUPPORTED_CURRENCY) * 2, 1, "global")],
):
    value = lightbulb.string("value", "Amount and currency '10AUD' 'AUD 10' '10+20%aud' 'Aud 10-3'")
    to = lightbulb.string(
        "to",
        "Currency to convert to",
        choices=[lightbulb.Choice(c.name, c.name) for c in config.SUPPORTED_CURRENCY],
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        await ctx.defer()
        log.info("Misc.Currency: %s", ctx.user.display_name)

        token = "".join(character for character in self.value.strip() if character.isalpha()).upper()
        parsed_amount = CurrencyConverter.parse_amount(re.sub(r"[A-Za-z]", " ", self.value))
        if parsed_amount is None or parsed_amount.amount == 0:
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
            result = await CurrencyConverter.convert(parsed_amount.amount, src, target)
            conversions[target] = result

        def _fmt(v: Decimal | None) -> str:
            return f"{v:,.3f}" if isinstance(v, (Decimal, float, int)) else "**error**"

        lines = [f"{t.name}: {_fmt(v)}" for t, v in conversions.items()]

        if parsed_amount.expression:
            header = f"**{parsed_amount.amount:,.3f} {src.name.upper()}** ({parsed_amount.expression}) converts to:\n"
        else:
            header = f"**{parsed_amount.amount:,.3f} {src.name.upper()}** converts to:\n"

        await ctx.respond(header + "\n".join(sorted(lines, key=str.upper)))


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
        choices=lightbulb.utils.to_choices(standard_drink_units(include_unavailable=False)),
    )
    to_unit = lightbulb.string(
        "to",
        "unit to convert to",
        choices=lightbulb.utils.to_choices(standard_drink_units(include_unavailable=False)),
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        log.info(f"Misc.STDDrink; value={self.value} {self.from_unit} > {self.to_unit}: {ctx.user.display_name}")

        conversion = standard_drink_conversion(
            amount=self.value,
            from_unit=self.from_unit,
            to_unit=self.to_unit,
        )
        result = format_standard_drink_range(conversion.converted_amount)

        await ctx.respond(f"{self.from_unit} {self.value} converts to {self.to_unit} {result}")

@group_misc.register
class CMD_MiscPFP(
    lightbulb.SlashCommand,
    name="pfp",
    description="Show your profile picture or another user's",
):
    user = lightbulb.user("user", "Optional target user", default=None)  # type: ignore

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        await ctx.defer()
        log.info(f"Misc.PFP: {ctx.user.display_name} > {self.user}")

        target = self.user or ctx.member or ctx.user
        if isinstance(target, hikari.Member):
            avatar_url = target.make_guild_avatar_url()
        else:
            avatar_url = target.make_avatar_url()
        if not avatar_url:
            avatar_url = target.display_avatar_url
        filename = f"avatar_{target.id}.{avatar_url.extension}"

        async with aiohttp.ClientSession() as session:
            async with session.get(str(avatar_url)) as resp:
                resp.raise_for_status()
                avatar_bytes = await resp.read()

        await ctx.respond(target.display_name, attachment=hikari.Bytes(avatar_bytes, filename))


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

    return await run_blocking(_read)


async def _save_store_path(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write():
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        tmp.replace(path)

    await run_blocking(_write)


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
        return None  # pyright: ignore[reportUnreachable]
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


async def ac_use_saved(ctx: lightbulb.AutocompleteContext[str]) -> None:
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
    formats = dict(Utilities.DISCORD_TIMESTAMP_FORMATS)
    rounds = Utilities.TIMESTAMP_ROUNDING_UNITS

    time = lightbulb.string(
        "time",
        "Relative, epoch, ISO, DMY, or zone time+date (e.g. 2h, 07/02/26 01:00)",
        min_length=1,
        max_length=96,
    )
    zone = lightbulb.string(
        "timezone",
        "Timezone for inputs without tz. Supports UTC offsets or IANA names (e.g. Europe/London)",
        autocomplete=ac_timezones,  # type: ignore
        default=None,
    )
    output = lightbulb.string(
        "format",
        "Which format to use",
        choices=[lightbulb.Choice(name, val) for name, val in formats.items()],
        default=formats["Short Date / Short Time"],
    )
    rounding = lightbulb.string(
        "round",
        "Round to closest Year, Month, Week, Day, Hour, Minute, or Second",
        choices=lightbulb.utils.to_choices(rounds),
        default="S",
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, utils: Utilities):
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        log.info(
            "Misc.TimeFormat; time=%s tz=%s round=%s fmt=%s user=%s",
            self.time,
            self.zone,
            self.rounding,
            self.output,
            ctx.user.display_name,
        )

        zone_raw = self.zone or _user_tz_name(ctx.user.id) or "UTC"
        tz = utils.parse_timezone(zone_raw)
        if tz is None:
            raise ValueError(f"Unknown timezone: {zone_raw}")

        ts = utils.parse_time(self.time, tz=tz)
        if ts is None:
            raise ValueError("Unknown time input")

        rounded = utils.round_wallclock(ts, self.rounding)
        rounded_utc = rounded.astimezone(timezone.utc)
        epoch = int(rounded_utc.timestamp())
        txt = self.output.format(epoch)
        await ctx.respond(txt)


# AiviA APasz
