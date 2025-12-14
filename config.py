import enum
import json
import logging
import logging.config
import os
import re
import sys
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Protocol, overload

import dotenv
import hikari
import requests
from pydantic import BaseModel, Field

NAME: str = "Yukibot"
UPLOAD_CLEAR_HOURS: int = 36
DISCORD_UPLOAD_LIMIT: int = 10  # in MiB


class Currency(enum.StrEnum):
    AUD = enum.auto()
    CHF = enum.auto()
    EUR = enum.auto()
    GBP = enum.auto()
    HUF = enum.auto()
    USD = enum.auto()


SUPPORTED_CURRENCY: dict[Currency, set[str]] = {
    Currency.AUD: {"A$", "$A", "AU$", "$AU", "AUD$", "$AUD", "AUD"},
    Currency.CHF: {"CHF", "SFR", "FR"},
    Currency.EUR: {"€", "EURO", "EUR"},
    Currency.GBP: {"£", "GBP"},
    Currency.HUF: {"Ft", "HUF"},
    Currency.USD: {"US$", "$US", "$USD", "USD$", "$", "USD"},
}

STD_DRINK_GRAMS: dict[str, int] = {"AU": 10, "UK": 8, "CH": 12, "FI": 12, "HU": 17, "US": 14}
PUBLIC_IP_ADDR: str = "https://api.ipify.org"
EXCHANGE_RATE_ADDR: str = "https://api.exchangerate.host/convert"
FILE_USERS: Path = Path("users.json")
DISCORD_NAMES: Path = Path("discord_names.json")
CHAT_IGNORE: str = "!"


# user config end

if os.name == "nt":
    print("Windows not supported!")
    exit(2)


dotenv.load_dotenv()


def env_req(var: str, force_reload: bool = False) -> str:
    if force_reload:
        dotenv.load_dotenv()
    env = os.getenv(var)
    if not env:
        raise ValueError(f"{var} must be set")
    return env.strip()


def env_opt(var: str) -> str | None:
    env = os.getenv(var)
    if not env:
        return None
    return env.strip()


APP_PATH = Path(env_req("DIR_APP"))
DISCORD_GUILD = hikari.Snowflake(env_req("DISCORD_GUILD"))
chan = env_opt("STARTED_CHANNEL")
STARTED_CHANNEL: hikari.Snowflakeish | None
if not chan:
    STARTED_CHANNEL = None
else:
    STARTED_CHANNEL = hikari.Snowflake(chan)

DISCORD_UPLOAD_LIMIT = DISCORD_UPLOAD_LIMIT * 1024 * 1024
"total byte size limit for uploads to discord"


def checksort_currencies(currencies: dict[Currency, set[str]]) -> dict[str, Currency]:
    """Build alias->code map with uppercase normalisation and collision warning."""
    mapping: dict[str, Currency] = {}
    for cur, syms in currencies.items():
        for sym in syms:
            key = sym.strip().upper()
            if key in mapping and mapping[key] != cur:
                print(f"Currency Collision: {sym}@{cur} > {mapping[key]}")
                continue
            mapping[key] = cur
    return mapping


CURRENCY_MAP = checksort_currencies(SUPPORTED_CURRENCY)

UPLOAD_CLEAR_TIME = timedelta(hours=UPLOAD_CLEAR_HOURS)
TENOR_ADDR = "tenor.com/view"
ENABLED_FILE = Path("enabled_apps.json")


@cache
def public_ip(url: str = PUBLIC_IP_ADDR):
    return requests.get(url).text


PUBLIC_URL_BASE = f"http://{public_ip()}/uploads/"
DIR_LOG = Path("logs")
DIR_TMP = Path(env_req("DIR_TMP"))
"/tmp/yukibot"
DIR_OPT = Path(env_req("DIR_OPT"))  # nginx setup only opt/bot
"/opt/yukibot"
DIR_UPLOAD = DIR_OPT / "uploads"
"{opt}/uploads"
DIR_DOWNLOADS = DIR_OPT / "downloads"
"{opt}/downloads"
DIR_ZIPS = DIR_OPT / "zips"
"{opt}/zips"
DIR_CWD = Path().parent


DIR_LOG.mkdir(parents=True, exist_ok=True)
DIR_TMP.mkdir(parents=True, exist_ok=True)
DIR_UPLOAD.mkdir(parents=True, exist_ok=True)
DIR_ZIPS.mkdir(parents=True, exist_ok=True)

STR_ENCODE = "utf-8"

is_debug = "-debug" in sys.argv
is_dc_debug = "-dc-debug" in sys.argv

root_lvl = logging.DEBUG if is_debug else logging.INFO
dc_lvl = logging.DEBUG if is_debug and is_dc_debug else logging.INFO


logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s | %(levelname).1s %(name)-25s - %(message)s",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": str(DIR_LOG / "System.log"),
                "mode": "w",  # 'a' if you want to append instead
                "formatter": "standard",
                "encoding": STR_ENCODE,
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
            },
        },
        "root": {
            "level": root_lvl,
            "handlers": ["file", "console"],
        },
        "loggers": {
            "system": {
                "level": root_lvl,
                "handlers": ["file"],
                "propagate": False,
            },
            "hikari": {
                "level": dc_lvl,
                "handlers": ["file"],
                "propagate": False,
            },
            "lightbulb": {
                "level": dc_lvl,
                "handlers": ["file"],
                "propagate": False,
            },
            "linkd": {
                "level": dc_lvl,
                "handlers": ["file"],
                "propagate": False,
            },
        },
    }
)
log = logging.getLogger("system")
IS_DEBUG = log.getEffectiveLevel() < 20
SILENT_DEBUG = IS_DEBUG and "-silent" in sys.argv
log.info(
    f"Log Level={logging._levelToName[root_lvl]} DCLog={logging._levelToName[dc_lvl]} {SILENT_DEBUG=} | sys.argv={str(sys.argv).strip('[]')}"
)


if not FILE_USERS.exists():
    FILE_USERS.write_text(json.dumps({"sudo": [], "user": []}, indent=4), STR_ENCODE)


GUESTS_ALLOWED = True
"If unrecognised users should be allowed to use use the unrestricted commands"


EXR_TOK = env_req("EXG_TOKEN")


class Singleton(type):
    """Singleton for singles, singlings, singlers, singletones, singlators, singlatees, and singlated..."""

    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class UserNames(BaseModel):
    account: str | None = None
    names: set[str] = Field(default_factory=set)
    nicknames: set[str] = Field(default_factory=set)
    games: dict[str, tuple[str, str | None]] = Field(default_factory=dict)


class Name_Cache(metaclass=Singleton):
    def __init__(self):
        self.pointer = DISCORD_NAMES
        self.by_id: dict[int, UserNames] = {}
        self.by_alias: dict[str, int] = {}
        self._read()

    def _read(self):
        if not self.pointer.exists():
            self._dump()
        try:
            raw = json.loads(self.pointer.read_text(STR_ENCODE))
            self.by_id = {int(uid): UserNames(**entry) for uid, entry in raw.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            log.exception("Corrupt name cache, resetting")
            self.by_id = {}
            self._dump()

        self._rebuild_aliases()

    def _dump(self):
        serializable = {str(uid): entry.model_dump(mode="json") for uid, entry in self.by_id.items()}
        self.pointer.write_text(json.dumps(serializable, sort_keys=True, indent=4), STR_ENCODE)

    def add_name(self, user_id: int, name: str, is_name: bool = True):
        user = self.by_id.setdefault(user_id, UserNames())
        target = user.names if is_name else user.nicknames
        if name not in target:
            target.add(name)
            self._rebuild_aliases()
            self._dump()

    def set_names(self, user: hikari.User | hikari.Member):
        if not user:
            return
        userName = self.by_id.setdefault(user.id, UserNames())
        userName.account = user.username
        userName.names = {
            name
            for name in [
                user.username,
                user.global_name,
                user.nickname if isinstance(user, hikari.Member) else None,
            ]
            if name
        }

        self._rebuild_aliases()
        self._dump()

    def remove_game_alias(self, user_id: int, scope: str):
        user = self.by_id.get(user_id)
        if not user:
            return
        user.games.pop(scope.lower(), None)
        self._dump()

    def remove_name(self, user_id: int, name: str):
        user = self.by_id.get(user_id)
        if not user:
            return
        user.nicknames.discard(name)
        self._rebuild_aliases()
        self._dump()

    def set_game_alias(self, user_id: int, scope: str, alias: str):
        user = self.by_id.setdefault(user_id, UserNames())
        user.games[scope.lower()] = (alias, None)
        self._dump()

    def set_game_uuid(self, user_id: int, scope: str, uuid: str):
        existing = self.by_id.get(user_id, UserNames()).games.get(scope, (None, None))
        if existing and existing[1] and existing[1].lower() == uuid.lower():
            return
        scope = scope.lower()
        user = self.by_id.setdefault(user_id, UserNames())
        name, _ = user.games.get(scope, (None, None))
        if name:
            user.games[scope] = (name, uuid)
            self._dump()

    def get_game_alias(self, user_id: int, scope: str) -> str | None:
        user = self.by_id.get(user_id)
        if not user:
            return None
        alias_data = user.games.get(scope.lower())
        return alias_data[0] if alias_data else user.account

    def resolve_to_id(self, name: str, scope: str | None = None) -> int | None:
        if scope:
            ident = self._resolve_game_alias(name, scope)
            if ident:
                return ident
        if name.isnumeric():
            if (ident := int(name)) in self.by_id:
                return ident
        return self.by_alias.get(name.lower())

    def _resolve_game_alias(self, alias: str, scope: str | None) -> int | None:
        for uid, entry in self.by_id.items():
            if not scope:
                for app in entry.games.keys():
                    if result := self._resolve_game_alias(alias, app):
                        return result
            else:
                data = entry.games.get(scope)
                if data and alias.lower() in (n.lower() for n in data if n):
                    return uid
        return None

    @overload
    async def best_known(
        self,
        user_id: int,
        default: None,
        /,
        scope: str | None = None,
        bot: hikari.GatewayBot | None = None,
    ) -> str | None: ...

    @overload
    async def best_known(
        self,
        user_id: int,
        default: str = "Unknown",
        /,
        scope: str | None = None,
        bot: hikari.GatewayBot | None = None,
    ) -> str: ...

    async def best_known(
        self,
        user_id: int,
        default: str | None = "Unknown",
        /,
        scope: str | None = None,
        bot: hikari.GatewayBot | None = None,
    ) -> str | None:
        if scope and (name := self.get_game_alias(user_id, scope)):
            return name
        if bot:
            if user := bot.cache.get_member(DISCORD_GUILD, user_id):
                self.set_names(user)
                return user.display_name
            if user := bot.cache.get_user(user_id):
                self.set_names(user)
                if user.display_name:
                    return user.display_name
            try:
                if user := await bot.rest.fetch_member(DISCORD_GUILD, user_id):
                    self.set_names(user)
                    return user.display_name
            except hikari.NotFoundError:
                pass
            except Exception as xcp:
                log.warning(f"Member fallback failed for {user_id}: {xcp}")
            try:
                if user := await bot.rest.fetch_user(user_id):
                    self.set_names(user)
                    if user.display_name:
                        return user.display_name
            except hikari.NotFoundError:
                pass
            except Exception as xcp:
                log.warning(f"User Fallback failed for {user_id}: {xcp}")
        user = self.by_id.get(user_id)
        return user.account if user else default

    def clean(self, user_id: int, current_names: list[str]):
        user = self.by_id.get(user_id)
        if not user:
            return
        user.names = set(current_names)
        self._rebuild_aliases()
        self._dump()

    def _rebuild_aliases(self):
        self.by_alias.clear()
        for uid, entry in self.by_id.items():
            for name in entry.names | entry.nicknames:
                self.by_alias[name.lower()] = uid

    def parse_mentions(self, text: str, replace: bool = True) -> tuple[str, set[int]]:
        """
        Parse @name mentions in the input text.

        Returns:
            - Modified string (if replace=True), original string otherwise
            - Set of resolved user IDs
        """
        mentions: set[int] = set()

        def repl(match):
            name = match.group(1)
            uid = self.resolve_to_id(name)
            if uid:
                mentions.add(uid)
                return f"<@{uid}>" if replace else match.group(0)
            return match.group(0)

        updated = re.sub(r"@([\w#-]+)", repl, text)
        return updated, mentions


INDEV = bool(env_opt("DISCORD_DEV_GUILD"))

AC_XCP = LookupError("Invalid input. Please use the autocomplete to select")
"convience var for xcp to raise when using autocomplete options"


class Activity_Provider(Protocol):
    silent: bool = SILENT_DEBUG
    """Whether to log"""
    prio = 50
    "0 = RAM | 2 = CPU | 4 = Player | 6 = Process | 10-79 = whatever | 80 >= Alerts"

    async def get(self) -> str | None:
        return None


class Activity_Manager(Protocol):
    providers: dict[type[Activity_Provider], Activity_Provider]
    """Whether to log"""

    def register(self, provider: Activity_Provider):
        return

    def deregister(self, provider: Activity_Provider):
        return


IS_RESTARTING = False

# AiviA APasz
