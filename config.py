import enum
import json
import logging
import logging.config
import os
import re
import sys
from dataclasses import dataclass
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
log = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class VoiceTargetConfig:
    guild_id: hikari.Snowflake
    voice_channel: hikari.Snowflake
    tts_channel: hikari.Snowflake


def _parse_optional_snowflake(var: str) -> hikari.Snowflakeish | None:
    value = env_opt(var)
    if not value:
        return None
    return hikari.Snowflake(value)


def _parse_voice_targets(
    raw: str | None,
    *,
    default_guild_id: hikari.Snowflake,
    legacy_voice_channel: hikari.Snowflakeish | None,
    legacy_tts_channel: hikari.Snowflakeish | None,
) -> dict[hikari.Snowflake, VoiceTargetConfig]:
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError as xcp:
            raise ValueError("VOICE_TARGETS must be valid JSON.") from xcp

        if not isinstance(payload, dict):
            raise ValueError("VOICE_TARGETS must be a JSON object keyed by guild id.")

        targets: dict[hikari.Snowflake, VoiceTargetConfig] = {}
        for guild_key, value in payload.items():
            try:
                guild_id = hikari.Snowflake(str(guild_key).strip())
            except ValueError as xcp:
                raise ValueError(f"VOICE_TARGETS has invalid guild id: {guild_key!r}") from xcp

            if not isinstance(value, dict):
                raise ValueError(f"VOICE_TARGETS[{guild_key!r}] must be an object.")

            voice_channel = value.get("voice_channel")
            tts_channel = value.get("tts_channel")
            if voice_channel is None or tts_channel is None:
                raise ValueError(f"VOICE_TARGETS[{guild_key!r}] must include both 'voice_channel' and 'tts_channel'.")

            try:
                targets[guild_id] = VoiceTargetConfig(
                    guild_id=guild_id,
                    voice_channel=hikari.Snowflake(str(voice_channel).strip()),
                    tts_channel=hikari.Snowflake(str(tts_channel).strip()),
                )
            except ValueError as xcp:
                raise ValueError(f"VOICE_TARGETS[{guild_key!r}] contains an invalid channel id.") from xcp

        return targets

    if legacy_voice_channel and legacy_tts_channel:
        return {
            default_guild_id: VoiceTargetConfig(
                guild_id=default_guild_id,
                voice_channel=hikari.Snowflake(legacy_voice_channel),
                tts_channel=hikari.Snowflake(legacy_tts_channel),
            )
        }

    return {}


APP_PATH = Path(env_req("DIR_APP"))
DISCORD_GUILD = hikari.Snowflake(env_req("DISCORD_GUILD"))
STARTED_CHANNEL = _parse_optional_snowflake("STARTED_CHANNEL")
VOICE_CHANNEL = _parse_optional_snowflake("VOICE_CHANNEL")
TTS_CHANNEL = _parse_optional_snowflake("TTS_CHANNEL")
VOICE_TARGETS = _parse_voice_targets(
    env_opt("VOICE_TARGETS"),
    default_guild_id=DISCORD_GUILD,
    legacy_voice_channel=VOICE_CHANNEL,
    legacy_tts_channel=TTS_CHANNEL,
)

TTS_ENGINE = (env_opt("TTS_ENGINE") or "auto").lower()
TTS_VOICE = env_opt("TTS_VOICE") or "en-gb-x-rp"
TTS_VARIANT = env_opt("TTS_VARIANT")
TTS_PIPER_MODEL = env_opt("TTS_PIPER_MODEL")
TTS_PIPER_CONFIG = env_opt("TTS_PIPER_CONFIG")
TTS_PIPER_DATA_DIR = env_opt("TTS_PIPER_DATA_DIR")
MUSIC_YTDLP_COOKIE_FILE = Path(value).expanduser() if (value := env_opt("MUSIC_YTDLP_COOKIE_FILE")) else None
MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS = env_opt("MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS")

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
def public_ip(url: str = PUBLIC_IP_ADDR) -> str:
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
    except requests.RequestException as xcp:
        fallback = "127.0.0.1"
        log.warning(f"Public IP lookup failed via {url!r}: {type(xcp).__name__}: {xcp}; using {fallback}")
        return fallback

    return response.text.strip()


PUBLIC_URL_BASE = env_opt("PUBLIC_URL_BASE") or f"http://{public_ip()}/uploads/"
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
CLEAR_CMDS = False


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
    platform_ids: dict[str, str] = Field(default_factory=dict)


class Name_Cache(metaclass=Singleton):
    def __init__(self):
        self.pointer = DISCORD_NAMES
        self.by_id: dict[int, UserNames] = {}
        self.by_alias: dict[str, int] = {}
        self.by_platform_id: dict[str, dict[str, int]] = {}
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
            return  # pyright: ignore[reportUnreachable]
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

    @staticmethod
    def _norm_platform_key(platform: object | None) -> str:
        value = str(platform).strip().lower() if platform is not None else ""
        if not value:
            raise ValueError("platform can't be empty")
        return value

    @staticmethod
    def _norm_platform_id(platform_id: object | None) -> str | None:
        if platform_id is None:
            return None
        value = str(platform_id).strip()
        if not value:
            return None
        return value

    @staticmethod
    def _norm_steam_id(steam_id: object | None) -> str | None:
        if steam_id is None:
            return None
        value = str(steam_id).strip()
        if not value:
            return None
        if not value.isdigit():
            raise ValueError("steam_id must be numeric")
        return value

    def set_platform_id(self, user_id: int, platform: object, platform_id: object | None) -> bool:
        platform_key = self._norm_platform_key(platform)
        if platform_key == "steam":
            value = self._norm_steam_id(platform_id)
        else:
            value = self._norm_platform_id(platform_id)
        user = self.by_id.setdefault(user_id, UserNames())
        current = self._norm_platform_id(user.platform_ids.get(platform_key))
        if current == value:
            return False

        if value is None:
            user.platform_ids.pop(platform_key, None)
        else:
            user.platform_ids[platform_key] = value

        self._rebuild_aliases()
        self._dump()
        return True

    def get_platform_id(self, user_id: int, platform: object) -> str | None:
        try:
            platform_key = self._norm_platform_key(platform)
        except ValueError:
            return None

        user = self.by_id.get(user_id)
        if not user:
            return None

        value = self._norm_platform_id(user.platform_ids.get(platform_key))
        if value is not None and platform_key == "steam":
            try:
                value = self._norm_steam_id(value)
            except ValueError:
                value = None
        return value

    def resolve_platform_to_id(self, platform: object, platform_id: object | None) -> int | None:
        try:
            platform_key = self._norm_platform_key(platform)
        except ValueError:
            return None
        if platform_key == "steam":
            try:
                value = self._norm_steam_id(platform_id)
            except ValueError:
                return None
        else:
            value = self._norm_platform_id(platform_id)
        if not value:
            return None
        return self.by_platform_id.get(platform_key, {}).get(value)

    def list_platform_ids(self, user_id: int) -> dict[str, str]:
        user = self.by_id.get(user_id)
        if not user:
            return {}

        out: dict[str, str] = {}
        for platform, raw_id in user.platform_ids.items():
            try:
                platform_key = self._norm_platform_key(platform)
            except ValueError:
                continue
            if platform_key == "steam":
                try:
                    value = self._norm_steam_id(raw_id)
                except ValueError:
                    continue
            else:
                value = self._norm_platform_id(raw_id)
            if value:
                out[platform_key] = value

        return dict(sorted(out.items()))

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
        self.by_platform_id.clear()
        for uid, entry in self.by_id.items():
            for name in entry.names | entry.nicknames:
                self.by_alias[name.lower()] = uid
            for platform, raw_id in entry.platform_ids.items():
                try:
                    platform_key = self._norm_platform_key(platform)
                except ValueError:
                    continue
                if platform_key == "steam":
                    try:
                        value = self._norm_steam_id(raw_id)
                    except ValueError:
                        continue
                else:
                    value = self._norm_platform_id(raw_id)
                if value:
                    self.by_platform_id.setdefault(platform_key, {})[value] = uid

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


INDEV = bool(env_opt("INDEV"))

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
