import enum
import json
import logging
import logging.config
import os
import re
import sys
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Literal, Protocol, overload
from urllib.parse import SplitResult, urlsplit, urlunsplit

import dotenv
import hikari
import requests
from pydantic import BaseModel, ConfigDict, Field

from _authority import (
    AuthorityClient,
    AuthorityResource,
    NameMutationKind,
    append_pending,
    read_json_object,
    read_pending,
    response_data,
    write_json_object,
)

NAME: str = "Omnibot"
UPLOAD_CLEAR_HOURS: int = 36
DISCORD_UPLOAD_LIMIT: int = 10  # in MiB
log = logging.getLogger(__name__)
ASYNCIO_ISCOROUTINEFUNCTION_DEPRECATION = (
    "'asyncio.iscoroutinefunction' is deprecated and slated for removal in Python 3.16; "
    "use inspect.iscoroutinefunction() instead"
)

warnings.filterwarnings(
    action="ignore",
    message=r"'asyncio\.iscoroutinefunction' is deprecated and slated for removal in Python 3\.16; use inspect\.iscoroutinefunction\(\) instead",
    category=DeprecationWarning,
)


class SuppressKnownWarningsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "py.warnings":
            return True
        return ASYNCIO_ISCOROUTINEFUNCTION_DEPRECATION not in record.getMessage()


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
PUBLIC_IP_SOURCE_URL: str = "https://api.ipify.org"
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


class PersistedVoiceTarget(BaseModel):
    voice_channel: int
    tts_channel: int


class BotConfiguration(BaseModel):
    model_config = ConfigDict(extra="allow")

    voice_targets: dict[str, PersistedVoiceTarget] = Field(default_factory=dict)


def parse_voice_targets_payload(
    payload: object,
    *,
    source: str,
) -> dict[hikari.Snowflake, VoiceTargetConfig]:
    if not isinstance(payload, dict):
        raise ValueError(f"{source} must be a JSON object keyed by guild id.")

    targets: dict[hikari.Snowflake, VoiceTargetConfig] = {}
    for guild_key, value in payload.items():
        try:
            guild_id = hikari.Snowflake(str(guild_key).strip())
        except ValueError as xcp:
            raise ValueError(f"{source} has invalid guild id: {guild_key!r}") from xcp

        if not isinstance(value, dict):
            raise ValueError(f"{source}[{guild_key!r}] must be an object.")

        voice_channel = value.get("voice_channel")
        tts_channel = value.get("tts_channel")
        if voice_channel is None or tts_channel is None:
            raise ValueError(f"{source}[{guild_key!r}] must include both 'voice_channel' and 'tts_channel'.")

        try:
            targets[guild_id] = VoiceTargetConfig(
                guild_id=guild_id,
                voice_channel=hikari.Snowflake(str(voice_channel).strip()),
                tts_channel=hikari.Snowflake(str(tts_channel).strip()),
            )
        except ValueError as xcp:
            raise ValueError(f"{source}[{guild_key!r}] contains an invalid channel id.") from xcp

    return targets


class CommandGroup(enum.StrEnum):
    APP = enum.auto()
    ALIAS = enum.auto()
    DASHBOARD = enum.auto()
    MISC = enum.auto()
    OPS = enum.auto()
    ONLINE = enum.auto()
    UPDATE = enum.auto()
    MUSIC = enum.auto()
    VOICE = enum.auto()


class BotService(enum.StrEnum):
    ACTIVITY = enum.auto()
    FILE_CLEANER = enum.auto()
    GAME_RELAY = enum.auto()
    MUSIC = enum.auto()
    ONLINE_TRACKING = enum.auto()
    VOICE_TTS = enum.auto()


class BotProfileName(enum.StrEnum):
    YUKI = enum.auto()
    ERIN = enum.auto()


class DataAuthorityMode(enum.StrEnum):
    LOCAL = enum.auto()
    REMOTE = enum.auto()


type HttpScheme = Literal["http", "https"]


@dataclass(frozen=True, slots=True)
class AuthorityEndpoint:
    scheme: HttpScheme
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return urlunsplit((self.scheme, f"{self.host}:{self.port}", "", "", ""))


@dataclass(frozen=True, slots=True)
class AuthorityServerBinding:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class BotProfileConfig:
    name: BotProfileName
    command_groups: tuple[CommandGroup, ...]
    services: frozenset[BotService]

    def has_service(self, service: BotService) -> bool:
        return service in self.services


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
        return parse_voice_targets_payload(payload, source="VOICE_TARGETS")

    if legacy_voice_channel and legacy_tts_channel:
        return {
            default_guild_id: VoiceTargetConfig(
                guild_id=default_guild_id,
                voice_channel=hikari.Snowflake(legacy_voice_channel),
                tts_channel=hikari.Snowflake(legacy_tts_channel),
            )
        }

    return {}


ALL_COMMAND_GROUPS: tuple[CommandGroup, ...] = (
    CommandGroup.APP,
    CommandGroup.ALIAS,
    CommandGroup.DASHBOARD,
    CommandGroup.MISC,
    CommandGroup.OPS,
    CommandGroup.ONLINE,
    CommandGroup.MUSIC,
    CommandGroup.VOICE,
)
ALL_BOT_SERVICES: frozenset[BotService] = frozenset(BotService)
BOT_PROFILES: dict[BotProfileName, BotProfileConfig] = {
    BotProfileName.YUKI: BotProfileConfig(
        name=BotProfileName.YUKI,
        command_groups=ALL_COMMAND_GROUPS,
        services=ALL_BOT_SERVICES,
    ),
    BotProfileName.ERIN: BotProfileConfig(
        name=BotProfileName.ERIN,
        command_groups=(
            CommandGroup.APP,
            CommandGroup.DASHBOARD,
            CommandGroup.OPS,
        ),
        services=frozenset(
            {
                BotService.ACTIVITY,
                BotService.FILE_CLEANER,
                BotService.GAME_RELAY,
            }
        ),
    ),
}


def _parse_bot_profile(raw: str | None) -> BotProfileName:
    if not raw:
        return BotProfileName.YUKI

    value = raw.strip().lower()
    try:
        return BotProfileName(value)
    except ValueError as xcp:
        expected = ", ".join(sorted(profile.value for profile in BotProfileName))
        raise ValueError(f"BOT_PROFILE must be one of: {expected}") from xcp


def _data_authority_mode(profile: BotProfileConfig) -> DataAuthorityMode:
    if profile.name is BotProfileName.YUKI:
        return DataAuthorityMode.LOCAL
    return DataAuthorityMode.REMOTE


def _parse_optional_port(raw: str | None, *, var_name: str) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as xcp:
        raise ValueError(f"{var_name} must be an integer") from xcp
    if value < 1 or value > 65535:
        raise ValueError(f"{var_name} must be between 1 and 65535")
    return value


def _parse_timeout_seconds(raw: str | None, *, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as xcp:
        raise ValueError("DATA_AUTHORITY_TIMEOUT_SECONDS must be a number") from xcp
    if value <= 0:
        raise ValueError("DATA_AUTHORITY_TIMEOUT_SECONDS must be greater than 0")
    return value


def _default_port_for_scheme(scheme: HttpScheme) -> int:
    if scheme == "http":
        return 80
    return 443


def _parsed_http_scheme(parsed: SplitResult) -> HttpScheme:
    return "http" if parsed.scheme == "http" else "https"


def _parse_http_reference(
    raw: str,
    *,
    var_name: str,
    default_scheme: HttpScheme,
    allow_path: bool,
) -> SplitResult:
    value = raw.strip()
    if not value:
        raise ValueError(f"{var_name} must not be empty when set.")
    parsed = urlsplit(value if "://" in value else f"{default_scheme}://{value}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{var_name} must use http or https when a scheme is provided.")
    if not parsed.netloc:
        raise ValueError(f"{var_name} must include a host.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{var_name} must not include query strings or fragments.")
    if parsed.username or parsed.password:
        raise ValueError(f"{var_name} must not include user info.")
    if not allow_path and parsed.path not in {"", "/"}:
        raise ValueError(f"{var_name} must not include a path.")
    return parsed


def _default_authority_scheme(public_base_url: str | None) -> HttpScheme:
    if public_base_url is None:
        return "https"
    parsed = _parse_http_reference(
        public_base_url,
        var_name="PUBLIC_BASE_URL",
        default_scheme="https",
        allow_path=False,
    )
    return _parsed_http_scheme(parsed)


def _resolve_authority_reference(
    raw_host: str | None,
    *,
    mode: DataAuthorityMode,
    raw_public_base_url: str | None,
    public_base_url: str,
) -> tuple[str, bool, str] | None:
    if raw_host is not None:
        return (raw_host, False, "DATA_AUTHORITY_HOST")
    if raw_public_base_url is not None:
        return (public_base_url, False, "PUBLIC_BASE_URL")
    if mode is DataAuthorityMode.LOCAL:
        return (public_base_url, False, "PUBLIC_BASE_URL")
    return None


def _parse_bind_host(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise ValueError("DATA_AUTHORITY_BIND_HOST must not be empty when set.")
    if "://" in value or any(char in value for char in "/?#"):
        raise ValueError("DATA_AUTHORITY_BIND_HOST must be a plain host or interface without a scheme or path.")
    return value


ACTIVE_BOT_PROFILE = BOT_PROFILES[_parse_bot_profile(env_opt("BOT_PROFILE"))]
DATA_AUTHORITY_MODE = _data_authority_mode(ACTIVE_BOT_PROFILE)
DATA_AUTHORITY_HOST = env_opt("DATA_AUTHORITY_HOST")
DATA_AUTHORITY_TOKEN = env_opt("DATA_AUTHORITY_TOKEN")
DATA_AUTHORITY_TIMEOUT_SECONDS = _parse_timeout_seconds(env_opt("DATA_AUTHORITY_TIMEOUT_SECONDS"), default=2.0)
DATA_AUTHORITY_CACHE_DIR = Path(env_opt("DATA_AUTHORITY_CACHE_DIR") or ".cache/authority")
DATA_AUTHORITY_PORT = _parse_optional_port(env_opt("DATA_AUTHORITY_PORT"), var_name="DATA_AUTHORITY_PORT")
DATA_AUTHORITY_BIND_HOST = env_opt("DATA_AUTHORITY_BIND_HOST")
DATA_AUTHORITY_BIND_PORT = _parse_optional_port(
    env_opt("DATA_AUTHORITY_BIND_PORT"),
    var_name="DATA_AUTHORITY_BIND_PORT",
)


def authority_client() -> AuthorityClient | None:
    if DATA_AUTHORITY_MODE is not DataAuthorityMode.REMOTE:
        return None
    if DATA_AUTHORITY_ENDPOINT is None or not DATA_AUTHORITY_TOKEN:
        raise ValueError(
            "Remote authority mode requires DATA_AUTHORITY_TOKEN and either DATA_AUTHORITY_HOST "
            "or an explicit PUBLIC_BASE_URL"
        )
    return AuthorityClient(DATA_AUTHORITY_ENDPOINT.base_url, DATA_AUTHORITY_TOKEN, DATA_AUTHORITY_TIMEOUT_SECONDS)


def authority_cache_path(resource: AuthorityResource) -> Path:
    filename = "discord_names.json" if resource is AuthorityResource.NAMES else "users.json"
    return DATA_AUTHORITY_CACHE_DIR / filename


def authority_pending_names_path() -> Path:
    return DATA_AUTHORITY_CACHE_DIR / "discord_names.pending.jsonl"


def fetch_remote_resource(resource: AuthorityResource) -> dict[str, object]:
    client = authority_client()
    if client is None:
        raise RuntimeError("Remote authority client is not configured")
    payload = response_data(client.get_json(f"/authority/{resource.value}"))
    write_json_object(authority_cache_path(resource), payload)
    return payload


def load_authority_json(resource: AuthorityResource, local_path: Path) -> dict[str, object]:
    if DATA_AUTHORITY_MODE is DataAuthorityMode.LOCAL:
        return read_json_object(local_path)

    cache_path = authority_cache_path(resource)
    try:
        return fetch_remote_resource(resource)
    except Exception as xcp:
        if cache_path.exists():
            log.warning(f"Authority {resource.value} refresh failed; using cache {cache_path}: {xcp}")
            return read_json_object(cache_path)
        if local_path.exists():
            log.warning(
                f"Authority {resource.value} refresh failed and no cache was found; "
                f"using local snapshot {local_path}: {xcp}"
            )
            return read_json_object(local_path)
        raise


def save_authority_json(
    resource: AuthorityResource,
    local_path: Path,
    payload: Mapping[str, object],
) -> dict[str, object]:
    serializable_payload = dict(payload)
    if DATA_AUTHORITY_MODE is DataAuthorityMode.LOCAL:
        write_json_object(local_path, serializable_payload)
        return serializable_payload

    client = authority_client()
    if client is None:
        raise RuntimeError("Remote authority client is not configured")

    response = client.post_json(f"/authority/{resource.value}/replace", {"data": serializable_payload})
    data = response_data(response)
    write_json_object(authority_cache_path(resource), data)
    return data


def flush_remote_name_mutations() -> int:
    if DATA_AUTHORITY_MODE is not DataAuthorityMode.REMOTE:
        return 0

    client = authority_client()
    if client is None:
        return 0

    pending_path = authority_pending_names_path()
    remaining: list[dict[str, object]] = []
    sent = 0
    for pending in read_pending(pending_path):
        if not isinstance(pending.get("kind"), str):
            log.warning(f"Skipping invalid pending name mutation without kind: {pending}")
            continue
        try:
            client.post_json("/authority/names/mutate", {"event": pending})
            sent += 1
        except Exception as xcp:
            log.warning(f"Pending name authority merge failed: {xcp}")
            remaining.append(pending)

    if remaining:
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text("\n".join(json.dumps(item, sort_keys=True) for item in remaining) + "\n", STR_ENCODE)
    elif pending_path.exists():
        pending_path.unlink()

    return sent


def queue_remote_name_mutation(event: dict[str, object]) -> None:
    if DATA_AUTHORITY_MODE is not DataAuthorityMode.REMOTE:
        return

    append_pending(authority_pending_names_path(), event)


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
ENABLED_DUMP_FILE = Path("enabled_apps.txt")


@cache
def public_ip(url: str = PUBLIC_IP_SOURCE_URL) -> str:
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
    except requests.RequestException as xcp:
        fallback = "127.0.0.1"
        log.warning(f"Public IP lookup failed via {url!r}: {type(xcp).__name__}: {xcp}; using {fallback}")
        return fallback

    return response.text.strip()


def _normalise_public_base_path(path: str) -> str:
    if not path or path == "/":
        return ""
    raise ValueError(
        "PUBLIC_BASE_URL must not include a path. Set only the public scheme/host, and Yukibot will derive /uploads/."
    )


def resolve_public_base_url(raw: str | None) -> str:
    if raw is None:
        return f"http://{public_ip()}"

    parsed = _parse_http_reference(raw, var_name="PUBLIC_BASE_URL", default_scheme="https", allow_path=True)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            _normalise_public_base_path(parsed.path),
            "",
            "",
        )
    )


def resolve_public_uploads_base_url(public_base_url: str) -> str:
    parsed = _parse_http_reference(public_base_url, var_name="PUBLIC_BASE_URL", default_scheme="https", allow_path=False)
    return urlunsplit((parsed.scheme, parsed.netloc, "/uploads/", "", ""))


def resolve_public_addr(raw: str | None, *, public_ip: str) -> str:
    if raw is None:
        return public_ip

    public_base_url = resolve_public_base_url(raw)
    parsed = _parse_http_reference(
        public_base_url,
        var_name="PUBLIC_BASE_URL",
        default_scheme="https",
        allow_path=False,
    )
    if parsed.hostname is None:
        raise ValueError("PUBLIC_BASE_URL must include a host.")
    return parsed.hostname


@cache
def public_host() -> str:
    return PUBLIC_ADDR


def resolve_data_authority_endpoint(
    raw_host: str | None,
    port: int | None,
    *,
    mode: DataAuthorityMode,
    public_base_url: str,
    raw_public_base_url: str | None = None,
) -> AuthorityEndpoint | None:
    resolved_reference = _resolve_authority_reference(
        raw_host,
        mode=mode,
        raw_public_base_url=raw_public_base_url,
        public_base_url=public_base_url,
    )
    if resolved_reference is None:
        return None

    reference, allow_path, source_name = resolved_reference
    default_scheme = _default_authority_scheme(public_base_url) if "://" not in reference else "https"

    parsed = _parse_http_reference(
        reference,
        var_name=source_name,
        default_scheme=default_scheme,
        allow_path=allow_path,
    )
    host = parsed.hostname
    if host is None:
        raise ValueError(f"{source_name} must include a host.")

    scheme = _parsed_http_scheme(parsed)
    return AuthorityEndpoint(
        scheme=scheme,
        host=host,
        port=port or parsed.port or _default_port_for_scheme(scheme),
    )


def resolve_data_authority_server_binding(
    raw_host: str | None,
    raw_port: int | None,
    *,
    endpoint: AuthorityEndpoint | None,
) -> AuthorityServerBinding | None:
    if endpoint is None:
        return None

    bind_host = _parse_bind_host(raw_host) or endpoint.host
    bind_port = raw_port or endpoint.port
    return AuthorityServerBinding(host=bind_host, port=bind_port)


RAW_PUBLIC_BASE_URL = env_opt("PUBLIC_BASE_URL")
PUBLIC_IP = public_ip()
PUBLIC_ADDR = resolve_public_addr(RAW_PUBLIC_BASE_URL, public_ip=PUBLIC_IP)
PUBLIC_BASE_URL = resolve_public_base_url(RAW_PUBLIC_BASE_URL)
PUBLIC_UPLOADS_BASE_URL = resolve_public_uploads_base_url(PUBLIC_BASE_URL)
DATA_AUTHORITY_ENDPOINT = resolve_data_authority_endpoint(
    DATA_AUTHORITY_HOST,
    DATA_AUTHORITY_PORT,
    mode=DATA_AUTHORITY_MODE,
    public_base_url=PUBLIC_BASE_URL,
    raw_public_base_url=RAW_PUBLIC_BASE_URL,
)
DATA_AUTHORITY_SERVER_BINDING = resolve_data_authority_server_binding(
    DATA_AUTHORITY_BIND_HOST,
    DATA_AUTHORITY_BIND_PORT,
    endpoint=DATA_AUTHORITY_ENDPOINT,
)
DATA_AUTHORITY_SERVER_ENABLED = DATA_AUTHORITY_MODE is DataAuthorityMode.LOCAL and DATA_AUTHORITY_TOKEN is not None
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
        "filters": {
            "suppress_known_warnings": {
                "()": SuppressKnownWarningsFilter,
            },
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": str(DIR_LOG / "System.log"),
                "mode": "w",  # 'a' if you want to append instead
                "formatter": "standard",
                "encoding": STR_ENCODE,
                "filters": ["suppress_known_warnings"],
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "filters": ["suppress_known_warnings"],
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


if DATA_AUTHORITY_MODE is DataAuthorityMode.LOCAL and not FILE_USERS.exists():
    FILE_USERS.write_text(json.dumps({"sudo": [], "user": []}, indent=4), STR_ENCODE)


GUESTS_ALLOWED = True
"If unrecognised users should be allowed to use use the unrestricted commands"


EXR_TOK = env_opt("EXG_TOKEN")


class Singleton(type):
    """Singleton for singles, singlings, singlers, singletones, singlators, singlatees, and singlated..."""

    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class UserNames(BaseModel):
    account: str | None = None
    global_name: str | None = None
    names: set[str] = Field(default_factory=set)
    nicknames: set[str] = Field(default_factory=set)
    games: dict[str, tuple[str, str | None]] = Field(default_factory=dict)
    platform_ids: dict[str, str] = Field(default_factory=dict)
    guild_names: dict[int, str] = Field(default_factory=dict)


class NameResolutionStatus(enum.StrEnum):
    UNIQUE = enum.auto()
    AMBIGUOUS = enum.auto()
    NOT_FOUND = enum.auto()


@dataclass(frozen=True, slots=True)
class NameResolutionResult:
    status: NameResolutionStatus
    user_id: int | None = None
    candidate_ids: tuple[int, ...] = ()


class Name_Cache(metaclass=Singleton):
    def __init__(self):
        self.pointer = DISCORD_NAMES
        if DATA_AUTHORITY_MODE is DataAuthorityMode.REMOTE:
            self.pointer = authority_cache_path(AuthorityResource.NAMES)
        self.by_id: dict[int, UserNames] = {}
        self.by_alias: dict[str, set[int]] = {}
        self.by_platform_id: dict[str, dict[str, int]] = {}
        self._read()

    @staticmethod
    def _entries_from_serialized(raw: dict[str, object]) -> dict[int, UserNames]:
        entries: dict[int, UserNames] = {}
        for uid, entry in raw.items():
            if not isinstance(entry, dict):
                raise TypeError(f"Name cache entry for {uid} must be an object")
            entries[int(uid)] = UserNames(**entry)
        return entries

    def _read(self):
        if DATA_AUTHORITY_MODE is DataAuthorityMode.LOCAL and not self.pointer.exists():
            self._dump()
        needs_migration = False
        try:
            raw = load_authority_json(AuthorityResource.NAMES, DISCORD_NAMES)
            self.by_id = self._entries_from_serialized(raw)
        except json.JSONDecodeError, TypeError, ValueError, OSError:
            if DATA_AUTHORITY_MODE is DataAuthorityMode.REMOTE:
                log.exception(
                    "Name authority unavailable and no valid cache is available; starting name cache degraded"
                )
                self.by_id = {}
                self._rebuild_aliases()
                return
            log.exception("Corrupt name cache, resetting")
            self.by_id = {}
            self._dump()

        for entry in self.by_id.values():
            needs_migration = self._normalise_user(entry) or needs_migration
        self._rebuild_aliases()
        if needs_migration:
            self._dump()

    def serializable(self, user_ids: set[int] | None = None) -> dict[str, object]:
        source = (
            self.by_id.items()
            if user_ids is None
            else ((uid, self.by_id[uid]) for uid in user_ids if uid in self.by_id)
        )
        return {str(uid): entry.model_dump(mode="json", exclude={"names"}) for uid, entry in source}

    def _dump(self):
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        self.pointer.write_text(json.dumps(self.serializable(), sort_keys=True, indent=4), STR_ENCODE)

    def _queue_remote_mutation(self, kind: NameMutationKind, **payload: object) -> None:
        if DATA_AUTHORITY_MODE is not DataAuthorityMode.REMOTE:
            return
        event: dict[str, object] = {"kind": kind.value}
        event.update(payload)
        queue_remote_name_mutation(event)

    def refresh_from_authority(self) -> bool:
        if DATA_AUTHORITY_MODE is not DataAuthorityMode.REMOTE:
            return False
        try:
            raw = fetch_remote_resource(AuthorityResource.NAMES)
            self.by_id = self._entries_from_serialized(raw)
            self._rebuild_aliases()
            return True
        except Exception as xcp:
            log.warning(f"Name authority refresh failed; keeping current cache: {xcp}")
            return False

    def flush_pending_mutations(self) -> int:
        return flush_remote_name_mutations()

    @staticmethod
    def _derived_known_names(user: UserNames) -> set[str]:
        names = {name for name in [user.account, user.global_name] if name}
        names.update(name for name in user.guild_names.values() if name)
        return names

    def _sync_known_names(self, user: UserNames) -> None:
        user.names = self._derived_known_names(user)

    def _normalise_user(self, user: UserNames) -> bool:
        before = user.model_dump(mode="json")
        user.guild_names = {int(guild_id): name for guild_id, name in user.guild_names.items() if name}
        self._sync_known_names(user)
        return user.model_dump(mode="json") != before

    @staticmethod
    def _set_names_payload(user_id: int, user: UserNames) -> dict[str, object]:
        return {
            "user_id": user_id,
            "account": user.account,
            "global_name": user.global_name,
            "guild_names": {str(guild_id): name for guild_id, name in sorted(user.guild_names.items())},
        }

    def _persist_identity_change(self, user_id: int, user: UserNames) -> None:
        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(NameMutationKind.SET_NAMES, **self._set_names_payload(user_id, user))

    def _apply_discord_identity(self, user: UserNames, discord_user: hikari.User | hikari.Member) -> bool:
        before = user.model_dump(mode="json")
        user.account = discord_user.username
        user.global_name = discord_user.global_name
        if isinstance(discord_user, hikari.Member):
            guild_id = int(discord_user.guild_id)
            if discord_user.nickname:
                user.guild_names[guild_id] = discord_user.nickname
            else:
                user.guild_names.pop(guild_id, None)
        self._sync_known_names(user)
        return user.model_dump(mode="json") != before

    def sync_members(self, members: Iterable[hikari.Member]) -> int:
        changed_user_ids: list[int] = []
        for member in members:
            user_id = int(member.id)
            user = self.by_id.setdefault(user_id, UserNames())
            if self._apply_discord_identity(user, member):
                changed_user_ids.append(user_id)

        if not changed_user_ids:
            return 0

        self._rebuild_aliases()
        self._dump()
        for user_id in changed_user_ids:
            self._queue_remote_mutation(NameMutationKind.SET_NAMES, **self._set_names_payload(user_id, self.by_id[user_id]))
        return len(changed_user_ids)

    def sync_cached_members(self, cache: hikari.api.Cache) -> int:
        members: list[hikari.Member] = []
        for guild_members in cache.get_members_view().values():
            members.extend(guild_members.values())
        return self.sync_members(members)

    def remove_guild_name(self, user_id: int, guild_id: hikari.Snowflakeish) -> bool:
        user = self.by_id.get(user_id)
        if user is None:
            return False

        before = user.model_dump(mode="json")
        user.guild_names.pop(int(guild_id), None)
        self._sync_known_names(user)
        if user.model_dump(mode="json") == before:
            return False

        self._persist_identity_change(user_id, user)
        return True

    def apply_mutation_event(self, event: dict[str, object]) -> bool:
        raw_kind = event.get("kind")
        if not isinstance(raw_kind, str):
            raise ValueError("Name mutation event must include string kind")
        kind = NameMutationKind(raw_kind)
        raw_user_id = event.get("user_id")
        if not isinstance(raw_user_id, int):
            raise ValueError("Name mutation event must include integer user_id")
        user = self.by_id.setdefault(raw_user_id, UserNames())

        before = user.model_dump(mode="json")
        if kind is NameMutationKind.ADD_NAME:
            name = str(event["name"])
            is_name = bool(event.get("is_name", True))
            if not is_name:
                user.nicknames.add(name)
        elif kind is NameMutationKind.CLEAN_NAMES:
            names = event.get("names")
            if not isinstance(names, list):
                raise ValueError("clean_names mutation requires names list")
            allowed_names = {str(name) for name in names if str(name)}
            user.global_name = user.global_name if user.global_name in allowed_names else None
            user.guild_names = {guild_id: name for guild_id, name in user.guild_names.items() if name in allowed_names}
            self._sync_known_names(user)
        elif kind is NameMutationKind.REMOVE_GAME_ALIAS:
            user.games.pop(str(event["scope"]).lower(), None)
        elif kind is NameMutationKind.REMOVE_NAME:
            user.nicknames.discard(str(event["name"]))
        elif kind is NameMutationKind.SET_GAME_ALIAS:
            user.games[str(event["scope"]).lower()] = (str(event["alias"]), None)
        elif kind is NameMutationKind.SET_GAME_UUID:
            scope = str(event["scope"]).lower()
            uuid = str(event["uuid"])
            name, _ = user.games.get(scope, (None, None))
            if name:
                user.games[scope] = (name, uuid)
        elif kind is NameMutationKind.SET_NAMES:
            account = event.get("account")
            user.account = str(account) if account is not None else None
            global_name = event.get("global_name")
            if global_name is not None or "global_name" in event:
                user.global_name = str(global_name) if global_name is not None else None
            raw_guild_names = event.get("guild_names")
            if raw_guild_names is not None:
                if not isinstance(raw_guild_names, dict):
                    raise ValueError("set_names mutation guild_names must be an object")
                user.guild_names = {int(guild_id): str(name) for guild_id, name in raw_guild_names.items() if str(name)}
            legacy_names = event.get("names")
            if legacy_names is not None and not isinstance(legacy_names, list):
                raise ValueError("set_names mutation names must be a list when provided")
            if "account" in event or global_name is not None or "global_name" in event or raw_guild_names is not None:
                self._sync_known_names(user)
            elif legacy_names is not None:
                allowed_names = {str(name) for name in legacy_names if str(name)}
                user.global_name = user.global_name if user.global_name in allowed_names else None
                user.guild_names = {guild_id: name for guild_id, name in user.guild_names.items() if name in allowed_names}
                self._sync_known_names(user)
        elif kind is NameMutationKind.SET_PLATFORM_ID:
            platform = self._norm_platform_key(event.get("platform"))
            platform_id = event.get("platform_id")
            value = self._norm_steam_id(platform_id) if platform == "steam" else self._norm_platform_id(platform_id)
            if value is None:
                user.platform_ids.pop(platform, None)
            else:
                user.platform_ids[platform] = value

        changed = user.model_dump(mode="json") != before
        if changed:
            self._rebuild_aliases()
            self._dump()
        return changed

    def add_name(self, user_id: int, name: str, is_name: bool = True):
        if is_name:
            raise ValueError("Known names are derived from Discord identity and cannot be added directly.")
        user = self.by_id.setdefault(user_id, UserNames())
        if name not in user.nicknames:
            user.nicknames.add(name)
            self._rebuild_aliases()
            self._dump()
            self._queue_remote_mutation(NameMutationKind.ADD_NAME, user_id=user_id, name=name, is_name=False)

    def set_names(self, user: hikari.User | hikari.Member):
        if not user:
            return  # pyright: ignore[reportUnreachable]
        user_id = int(user.id)
        userName = self.by_id.setdefault(user_id, UserNames())
        if not self._apply_discord_identity(userName, user):
            return

        self._persist_identity_change(user_id, userName)

    def remove_game_alias(self, user_id: int, scope: str):
        user = self.by_id.get(user_id)
        if not user:
            return
        user.games.pop(scope.lower(), None)
        self._dump()
        self._queue_remote_mutation(NameMutationKind.REMOVE_GAME_ALIAS, user_id=user_id, scope=scope)

    def remove_name(self, user_id: int, name: str):
        user = self.by_id.get(user_id)
        if not user:
            return
        user.nicknames.discard(name)
        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(NameMutationKind.REMOVE_NAME, user_id=user_id, name=name)

    def set_game_alias(self, user_id: int, scope: str, alias: str):
        user = self.by_id.setdefault(user_id, UserNames())
        user.games[scope.lower()] = (alias, None)
        self._dump()
        self._queue_remote_mutation(NameMutationKind.SET_GAME_ALIAS, user_id=user_id, scope=scope, alias=alias)

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
        self._queue_remote_mutation(
            NameMutationKind.SET_PLATFORM_ID,
            user_id=user_id,
            platform=platform_key,
            platform_id=value,
        )
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
            self._queue_remote_mutation(NameMutationKind.SET_GAME_UUID, user_id=user_id, scope=scope, uuid=uuid)

    def get_game_alias(self, user_id: int, scope: str) -> str | None:
        user = self.by_id.get(user_id)
        if not user:
            return None
        alias_data = user.games.get(scope.lower())
        return alias_data[0] if alias_data else user.account

    @staticmethod
    def _sorted_name_values(values: set[str]) -> list[str]:
        return sorted(values, key=str.casefold)

    @staticmethod
    def _preferred_guild_display_name(
        entry: UserNames,
        preferred_guild_id: hikari.Snowflakeish | None = None,
    ) -> str | None:
        if preferred_guild_id is not None:
            preferred_name = entry.guild_names.get(int(preferred_guild_id))
            if preferred_name:
                return preferred_name

        if not entry.guild_names:
            return None

        return sorted(entry.guild_names.values(), key=str.casefold)[0]

    @overload
    def cached_display_name(
        self,
        user_id: int,
        default: None,
        /,
        *,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
    ) -> str | None: ...

    @overload
    def cached_display_name(
        self,
        user_id: int,
        default: str = "Unknown",
        /,
        *,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
    ) -> str: ...

    def cached_display_name(
        self,
        user_id: int,
        default: str | None = "Unknown",
        /,
        *,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
    ) -> str | None:
        user = self.by_id.get(user_id)
        if user is None:
            return default

        if preferred_name := self._preferred_guild_display_name(user, preferred_guild_id):
            return preferred_name
        if user.global_name:
            return user.global_name
        if user.account:
            return user.account
        if user.names:
            return self._sorted_name_values(user.names)[0]
        if user.nicknames:
            return self._sorted_name_values(user.nicknames)[0]
        return default

    def _resolve_candidate_result(
        self,
        candidate_ids: set[int],
        name: str,
        *,
        prefer_global_name: bool,
    ) -> NameResolutionResult:
        if not candidate_ids:
            return NameResolutionResult(NameResolutionStatus.NOT_FOUND)
        if len(candidate_ids) == 1:
            return NameResolutionResult(NameResolutionStatus.UNIQUE, next(iter(candidate_ids)))
        if not prefer_global_name:
            return NameResolutionResult(NameResolutionStatus.AMBIGUOUS, candidate_ids=tuple(sorted(candidate_ids)))

        matching_global_names = {
            user_id
            for user_id in candidate_ids
            if (
                (entry := self.by_id.get(user_id)) is not None
                and entry.global_name is not None
                and entry.global_name.lower() == name.lower()
            )
        }
        if len(matching_global_names) == 1:
            return NameResolutionResult(NameResolutionStatus.UNIQUE, next(iter(matching_global_names)))
        return NameResolutionResult(NameResolutionStatus.AMBIGUOUS, candidate_ids=tuple(sorted(candidate_ids)))

    def resolve_name(
        self,
        name: str,
        scope: str | None = None,
        *,
        prefer_global_name: bool = False,
    ) -> NameResolutionResult:
        if scope:
            candidate_ids = self._resolve_game_alias_ids(name, scope)
            result = self._resolve_candidate_result(candidate_ids, name, prefer_global_name=prefer_global_name)
            if result.status is not NameResolutionStatus.NOT_FOUND:
                return result
        if name.isnumeric():
            if (ident := int(name)) in self.by_id:
                return NameResolutionResult(NameResolutionStatus.UNIQUE, ident)
        return self._resolve_candidate_result(
            self.by_alias.get(name.lower(), set()),
            name,
            prefer_global_name=prefer_global_name,
        )

    def resolve_to_id(self, name: str, scope: str | None = None, *, prefer_global_name: bool = False) -> int | None:
        return self.resolve_name(name, scope, prefer_global_name=prefer_global_name).user_id

    def _resolve_game_alias_ids(self, alias: str, scope: str | None) -> set[int]:
        matching_ids: set[int] = set()
        alias_key = alias.lower()
        scope_key = scope.lower() if scope else None
        for uid, entry in self.by_id.items():
            if scope_key is None:
                if any(alias_key in (name.lower() for name in data if name) for data in entry.games.values()):
                    matching_ids.add(uid)
            else:
                data = entry.games.get(scope_key)
                if data and alias_key in (name.lower() for name in data if name):
                    matching_ids.add(uid)
        return matching_ids

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
        return self.cached_display_name(user_id, default)

    def clean(self, user_id: int, current_names: list[str]):
        user = self.by_id.get(user_id)
        if not user:
            return
        allowed_names = set(current_names)
        user.global_name = user.global_name if user.global_name in allowed_names else None
        user.guild_names = {guild_id: name for guild_id, name in user.guild_names.items() if name in allowed_names}
        self._sync_known_names(user)
        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(NameMutationKind.CLEAN_NAMES, user_id=user_id, names=sorted(user.names))

    def _rebuild_aliases(self):
        self.by_alias.clear()
        self.by_platform_id.clear()
        for uid, entry in self.by_id.items():
            self._sync_known_names(entry)
            for name in entry.names | entry.nicknames:
                self.by_alias.setdefault(name.lower(), set()).add(uid)
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
    last_update: datetime
    state: str | None

    def register(self, provider: Activity_Provider):
        return

    def deregister(self, provider: Activity_Provider):
        return


IS_RESTARTING = False

# AiviA APasz
