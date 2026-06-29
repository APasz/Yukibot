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
from logging import Logger
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, overload
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import hikari
import psutil
import requests
from hikari.snowflakes import Snowflake
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource

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
from restart_targets import RestartTarget

NAME: str = "Yukibot"
UPLOAD_CLEAR_HOURS: int = 36
DISCORD_UPLOAD_LIMIT_MIB: int = 10
DISCORD_UPLOAD_LIMIT: int = DISCORD_UPLOAD_LIMIT_MIB * 1024 * 1024
log: Logger = logging.getLogger(__name__)
ASYNCIO_ISCOROUTINEFUNCTION_DEPRECATION = (
    "'asyncio.iscoroutinefunction' is deprecated and slated for removal in Python 3.16; "
    "use inspect.iscoroutinefunction() instead"
)
_IGNORED_PYTHON_WARNING_MESSAGE_SNIPPETS: tuple[str, ...] = (
    ASYNCIO_ISCOROUTINEFUNCTION_DEPRECATION,
    "websockets.legacy is deprecated; see",
    "websockets.server.WebSocketServerProtocol is deprecated",
    "remove second argument of ws_handler",
)
LOGGER_TRAFFIC: str = "traffic"
LOGGER_TTS: str = "tts"
LOGGER_AUDIT: str = "audit"
LOGGER_TENOR: str = "tenor"
DEFAULT_DATA_AUTHORITY_BIND_HOST: str = "127.0.0.1"
DEFAULT_DATA_AUTHORITY_BIND_PORT: int = 8081

warnings.filterwarnings(
    action="ignore",
    message=r"'asyncio\.iscoroutinefunction' is deprecated and slated for removal in Python 3\.16; use inspect\.iscoroutinefunction\(\) instead",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    action="ignore",
    message=r"websockets\.legacy is deprecated; see .*",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    action="ignore",
    message=r"websockets\.server\.WebSocketServerProtocol is deprecated",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    action="ignore",
    message=r"remove second argument of ws_handler",
    category=DeprecationWarning,
)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    bot_profile: str | None = None
    indev: str | None = None
    bypass_web_auth: str | None = None
    data_authority_host: str | None = None
    data_authority_token: str | None = None
    data_authority_timeout_seconds: str | None = None
    data_authority_cache_dir: str | None = None
    data_authority_port: str | None = None
    data_authority_bind_host: str | None = None
    data_authority_bind_port: str | None = None
    dir_app: str | None = None
    discord_guild: str | None = None
    started_channel: str | None = None
    voice_channel: str | None = None
    tts_channel: str | None = None
    voice_targets: str | None = None
    tts_engine: str | None = None
    tts_voice: str | None = None
    tts_variant: str | None = None
    tts_piper_model: str | None = None
    tts_piper_config: str | None = None
    tts_piper_data_dir: str | None = None
    music_ytdlp_cookie_file: str | None = None
    music_ytdlp_youtube_extractor_args: str | None = None
    public_base_url: str | None = None
    node_name: str | None = None
    node_api_token_secret: str | None = None
    node_api_bind_host: str | None = None
    node_api_port: str | None = None
    node_api_public_base_url: str | None = None
    mod_web_bind_host: str | None = None
    mod_web_port: str | None = None
    mod_web_public_base_url: str | None = None
    mod_web_discord_client_id: str | None = None
    mod_web_discord_client_secret: str | None = None
    mod_web_auth_redirect_url: str | None = None
    mod_web_session_cache_dir: str | None = None
    mod_web_build_sha: str | None = None
    dir_tmp: str | None = None
    dir_opt: str | None = None
    exg_token: str | None = None
    bot_token: str | None = None
    modrinth_api_key: str | None = None
    curseforge_api_key: str | None = None
    nexusmods_api_key: str | None = None
    wube_api_key: str | None = None
    modio_api_key: str | None = None
    modio_user_id: str | None = None
    steam_web_api_key: str | None = None
    app_comm_pass: str | None = None


class SuppressKnownWarningsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "py.warnings":
            return True
        message = record.getMessage()
        return not any(snippet in message for snippet in _IGNORED_PYTHON_WARNING_MESSAGE_SNIPPETS)

class AppScopes(enum.StrEnum):
    minecraft = "minecraft"
    sevendays = "sevendays"
    beammp = "beammp"
    ets = "ets"
    factorio = "factorio"
    satisfactory = "satisfactory"

    @property
    def display_name(self) -> str:
        match self:
            case AppScopes.minecraft:
                return "Minecraft"
            case AppScopes.sevendays:
                return "7 Days to Die"
            case AppScopes.beammp:
                return "BeamMP"
            case AppScopes.ets:
                return "Euro Truck Simulator 2"
            case AppScopes.factorio:
                return "Factorio"
            case AppScopes.satisfactory:
                return "Satisfactory"


class ID_Platforms(enum.StrEnum):
    steam = "Steam"
    minecraft = "Minecraft"
    microsoft = "Microsoft"
    epic_games = "Epic Games"


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


def _load_env_settings() -> EnvSettings:
    return EnvSettings()


def _load_dotenv_env_vars() -> dict[str, str]:
    source: DotEnvSettingsSource = DotEnvSettingsSource(
        EnvSettings,
        env_file=EnvSettings.model_config.get("env_file"),
        env_file_encoding=EnvSettings.model_config.get("env_file_encoding"),
        env_ignore_empty=EnvSettings.model_config.get("env_ignore_empty"),
    )
    raw_env_vars: Mapping[str, str | None] = source._load_env_vars()
    return {key.casefold(): value for key, value in raw_env_vars.items() if value is not None}


_env_settings: EnvSettings = _load_env_settings()
_dotenv_env_vars: dict[str, str] = _load_dotenv_env_vars()


def _refresh_env_state() -> None:
    global _env_settings, _dotenv_env_vars
    _env_settings = _load_env_settings()
    _dotenv_env_vars = _load_dotenv_env_vars()


def _require_loaded_setting(value: str | None, *, var_name: str) -> str:
    if value is None:
        raise ValueError(f"{var_name} must be set")
    return value


def _lookup_env_value(var: str) -> str | None:
    env: str | None = os.getenv(var)
    if env is not None:
        stripped_env: str = env.strip()
        return stripped_env or None
    dotenv_env: str | None = _dotenv_env_vars.get(var.casefold())
    if dotenv_env is None:
        return None
    stripped_dotenv: str = dotenv_env.strip()
    return stripped_dotenv or None


def env_req(var: str, force_reload: bool = False) -> str:
    if force_reload:
        _refresh_env_state()
    env = _lookup_env_value(var)
    if not env:
        raise ValueError(f"{var} must be set")
    return env


def env_opt(var: str) -> str | None:
    return _lookup_env_value(var)


class VoiceTargetTtsChannelRole(enum.StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"

    @property
    def label(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class VoiceTargetConfig:
    guild_id: hikari.Snowflake
    voice_channel: hikari.Snowflake
    primary_tts_channel: hikari.Snowflake
    primary_tts_listen_enabled: bool = True
    secondary_tts_channel: hikari.Snowflake | None = None
    secondary_tts_listen_enabled: bool = False
    relay_tts_enabled: bool = False

    @property
    def tts_channel(self) -> hikari.Snowflake:
        return self.primary_tts_channel

    @property
    def tts_channels(self) -> tuple[hikari.Snowflake, ...]:
        if self.secondary_tts_channel is None or self.secondary_tts_channel == self.primary_tts_channel:
            return (self.primary_tts_channel,)
        return (self.primary_tts_channel, self.secondary_tts_channel)

    @property
    def listened_tts_channels(self) -> tuple[hikari.Snowflake, ...]:
        channels: list[hikari.Snowflake] = []
        if self.primary_tts_listen_enabled:
            channels.append(self.primary_tts_channel)
        if self.secondary_tts_channel is not None and self.secondary_tts_listen_enabled:
            channels.append(self.secondary_tts_channel)
        return tuple(channels)

    def tts_channel_for_role(self, role: VoiceTargetTtsChannelRole) -> hikari.Snowflake | None:
        if role is VoiceTargetTtsChannelRole.PRIMARY:
            return self.primary_tts_channel
        return self.secondary_tts_channel

    def tts_channel_listen_enabled(self, role: VoiceTargetTtsChannelRole) -> bool:
        if role is VoiceTargetTtsChannelRole.PRIMARY:
            return self.primary_tts_listen_enabled
        return self.secondary_tts_channel is not None and self.secondary_tts_listen_enabled

    def has_tts_channel(self, channel_id: hikari.Snowflakeish) -> bool:
        channel = hikari.Snowflake(channel_id)
        return channel in self.tts_channels

    def has_listening_tts_channel(self, channel_id: hikari.Snowflakeish) -> bool:
        channel = hikari.Snowflake(channel_id)
        return channel in self.listened_tts_channels


class PersistedVoiceTarget(BaseModel):
    voice_channel: int
    primary_tts_channel: int
    primary_tts_listen_enabled: bool = True
    secondary_tts_channel: int | None = None
    secondary_tts_listen_enabled: bool = False
    relay_tts_enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_tts_channel(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value

        payload = dict(value)
        if payload.get("primary_tts_channel") is None and payload.get("tts_channel") is not None:
            payload["primary_tts_channel"] = payload["tts_channel"]
        if "primary_tts_listen_enabled" not in payload:
            payload["primary_tts_listen_enabled"] = True
        if "secondary_tts_listen_enabled" not in payload:
            payload["secondary_tts_listen_enabled"] = payload.get("secondary_tts_channel") is not None
        return payload


def normalise_absolute_path_text(value: str, *, source: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{source} must not be empty.")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(f"{source} must be an absolute path.")
    return str(path)


class PersistedDiskPreferences(BaseModel):
    activity_mounts: list[str] | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    primary_mount: str | None = None

    @field_validator("activity_mounts")
    @classmethod
    def _validate_activity_mounts(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None

        seen: set[str] = set()
        normalised: list[str] = []
        for mountpoint in value:
            mount_text = normalise_absolute_path_text(str(mountpoint), source="disk_preferences.activity_mounts")
            if mount_text in seen:
                continue
            seen.add(mount_text)
            normalised.append(mount_text)
        return normalised

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        normalised: dict[str, str] = {}
        for mountpoint, label in value.items():
            mount_text = normalise_absolute_path_text(str(mountpoint), source="disk_preferences.labels")
            label_text = str(label).strip()
            if not label_text:
                raise ValueError("disk_preferences.labels values must not be empty.")
            normalised[mount_text] = label_text
        return normalised

    @field_validator("primary_mount")
    @classmethod
    def _validate_primary_mount(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalise_absolute_path_text(value, source="disk_preferences.primary_mount")


class PersistedRestartSchedule(BaseModel):
    enabled: bool = False
    hour: int = 0
    minute: int = 0
    last_triggered_at: datetime | None = None

    @field_validator("hour")
    @classmethod
    def _validate_hour(cls, value: int) -> int:
        if value < 0 or value > 23:
            raise ValueError("maintenance restart hour must be between 0 and 23.")
        return value

    @field_validator("minute")
    @classmethod
    def _validate_minute(cls, value: int) -> int:
        if value < 0 or value > 59:
            raise ValueError("maintenance restart minute must be between 0 and 59.")
        return value


class PersistedRestartWarning(BaseModel):
    lead_minutes: int = 15

    @field_validator("lead_minutes")
    @classmethod
    def _validate_lead_minutes(cls, value: int) -> int:
        if value == 0:
            return value
        if value < 5 or value > 180:
            raise ValueError("maintenance restart warning minutes must be 0 or between 5 and 180.")
        return value


class PersistedMaintenanceSettings(BaseModel):
    restart_schedules: dict[RestartTarget, PersistedRestartSchedule] = Field(default_factory=dict)
    restart_warning: PersistedRestartWarning = Field(default_factory=PersistedRestartWarning)

    def schedule_for(self, target: RestartTarget) -> PersistedRestartSchedule:
        return self.restart_schedules.get(target, PersistedRestartSchedule())


class PersistedRestartState(BaseModel):
    auto_start_apps: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_auto_start_app(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "auto_start_apps" in payload:
            return payload
        legacy_auto_start_app = payload.pop("auto_start_app", None)
        if legacy_auto_start_app is not None:
            payload["auto_start_apps"] = [legacy_auto_start_app]
        return payload

    @field_validator("auto_start_apps", mode="before")
    @classmethod
    def _validate_auto_start_apps(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            text = value.strip()
            return (text,) if text else ()
        if not isinstance(value, tuple | list | set | frozenset):
            raise TypeError("auto_start_apps must be a sequence of app names")
        app_names: list[str] = []
        seen_names: set[str] = set()
        for raw_name in value:
            text = str(raw_name).strip()
            if not text or text in seen_names:
                continue
            seen_names.add(text)
            app_names.append(text)
        return tuple(app_names)


def normalise_discord_id_text(value: object, *, source: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{source} must not be empty.")
    try:
        return str(int(hikari.Snowflake(text)))
    except ValueError as xcp:
        raise ValueError(f"{source} must be a Discord snowflake.") from xcp


class OAuthInstallType(enum.StrEnum):
    GUILD = "guild"
    USER = "user"

    @property
    def integration_type(self) -> str:
        if self is OAuthInstallType.GUILD:
            return "0"
        return "1"

    @property
    def scopes(self) -> str:
        if self is OAuthInstallType.GUILD:
            return "applications.commands bot"
        return "applications.commands"


class PersistedOAuthLinks(BaseModel):
    guild: str | None = None
    user: str | None = None

    @field_validator("guild", "user")
    @classmethod
    def _validate_optional_url(cls, value: str | None) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        if not text:
            raise ValueError("OAuth URLs must not be empty.")

        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("OAuth URLs must use http or https.")
        if not parsed.netloc:
            raise ValueError("OAuth URLs must include a host.")
        return text

    def configured_url(self, install_type: OAuthInstallType) -> str | None:
        if install_type is OAuthInstallType.GUILD:
            return self.guild
        return self.user

    def supports(self, install_type: OAuthInstallType) -> bool:
        return install_type.value in self.model_fields_set

    def supported_install_types(self) -> tuple[OAuthInstallType, ...]:
        return tuple(install_type for install_type in OAuthInstallType if self.supports(install_type))

    def serializable(self) -> dict[str, str | None]:
        payload: dict[str, str | None] = {}
        for install_type in self.supported_install_types():
            payload[install_type.value] = self.configured_url(install_type)
        return payload

    @model_serializer(mode="plain")
    def _serialize(self) -> dict[str, str | None]:
        return self.serializable()


class BotMetadataProfile(BaseModel):
    id: str
    label: str | None = None
    bot_profile: BotProfileName | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return normalise_discord_id_text(value, source="bot metadata profile id")

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("bot metadata profile label must not be empty.")
        return text


class BotMetadataModWeb(BaseModel):
    node_name: str
    public_base_url: str
    node_api_base_url: str

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("node_name", "public_base_url", "node_api_base_url")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("bot metadata mod web fields must not be empty.")
        return text


class BotMetadataPresentation(BaseModel):
    avatar_uri: str | None = None
    accent_color_hex: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("avatar_uri")
    @classmethod
    def _validate_avatar_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("bot metadata avatar URI must not be empty.")
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("bot metadata avatar URI must use http or https.")
        if not parsed.netloc:
            raise ValueError("bot metadata avatar URI must include a host.")
        return text

    @field_validator("accent_color_hex")
    @classmethod
    def _validate_accent_color_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            raise ValueError("bot metadata accent color must not be empty.")
        if re.fullmatch(r"#[0-9a-fA-F]{6}", text) is None:
            raise ValueError("bot metadata accent color must be a hex color in #rrggbb format.")
        return text.casefold()


class BotMetadataFeatures(BaseModel):
    oauth: PersistedOAuthLinks | None = None
    mod_web: BotMetadataModWeb | None = None
    presentation: BotMetadataPresentation | None = None


class BotMetadataSnapshot(BaseModel):
    profile: BotMetadataProfile
    features: BotMetadataFeatures = Field(default_factory=BotMetadataFeatures)


def normalise_google_font_source_url(raw: object) -> str:
    if not isinstance(raw, str):
        raise TypeError("Google font source URL must be a string.")
    text = raw.strip()
    if not text:
        raise ValueError("Google font source URL must not be empty.")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Google font source URLs must use http or https.")
    if not parsed.netloc:
        raise ValueError("Google font source URLs must include a host.")
    host = parsed.netloc.casefold()
    if host in {"fonts.google.com", "www.fonts.google.com"}:
        specimen_prefix = "/specimen/"
        if not parsed.path.startswith(specimen_prefix):
            raise ValueError("Google Fonts URLs must point to a specimen page.")
        family_name = unquote(parsed.path[len(specimen_prefix) :]).replace("+", " ").strip().strip("/")
        if not family_name:
            raise ValueError("Google Fonts specimen URLs must include a font family.")
        return urlunsplit(
            SplitResult(
                scheme="https",
                netloc="fonts.googleapis.com",
                path="/css2",
                query=urlencode({"family": family_name, "display": "swap"}),
                fragment="",
            )
        )
    if host != "fonts.googleapis.com":
        raise ValueError("Unsupported font source host.")
    if parsed.path not in {"/css", "/css2"}:
        raise ValueError("Google Fonts API URLs must use /css or /css2.")
    query_pairs = tuple((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=False) if key)
    family_values = tuple(value for key, value in query_pairs if key == "family" and value.strip())
    if len(family_values) != 1:
        raise ValueError("Google Fonts API URLs must specify exactly one non-empty family.")
    normalized_query_pairs: list[tuple[str, str]] = []
    for key, value in query_pairs:
        if key == "display":
            continue
        normalized_query_pairs.append((key, value))
    normalized_query_pairs.append(("display", "swap"))
    return urlunsplit(
        SplitResult(
            scheme="https",
            netloc="fonts.googleapis.com",
            path=parsed.path,
            query=urlencode(normalized_query_pairs, doseq=True),
            fragment="",
        )
    )


def normalise_google_font_source_urls(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    raw_items: Iterable[object]
    if isinstance(raw, str):
        raw_items = tuple(line for line in raw.splitlines() if line.strip())
    elif isinstance(raw, Iterable):
        raw_items = raw
    else:
        raise TypeError("Google font source URLs must be a string or sequence of strings.")
    normalised_urls: list[str] = []
    seen_urls: set[str] = set()
    for item in raw_items:
        normalized_url = normalise_google_font_source_url(item)
        if normalized_url.casefold() in seen_urls:
            continue
        seen_urls.add(normalized_url.casefold())
        normalised_urls.append(normalized_url)
    return tuple(normalised_urls)


class NodeFontSourceSettings(BaseModel):
    google_font_urls: tuple[str, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(extra="forbid")

    @field_validator("google_font_urls", mode="before")
    @classmethod
    def _validate_google_font_urls(cls, raw: object) -> tuple[str, ...]:
        return normalise_google_font_source_urls(raw)


class DiscordActivityField(enum.StrEnum):
    RAM = "ram"
    CPU = "cpu"
    PLAYERS = "players"
    APP = "app"
    DISK_ALERT = "disk_alert"

    @property
    def label(self) -> str:
        labels: dict["DiscordActivityField", str] = {
            DiscordActivityField.RAM: "RAM",
            DiscordActivityField.CPU: "CPU",
            DiscordActivityField.PLAYERS: "Players",
            DiscordActivityField.APP: "App",
            DiscordActivityField.DISK_ALERT: "Disk Alert",
        }
        return labels[self]


_DEFAULT_DISCORD_ACTIVITY_FIELDS: tuple[DiscordActivityField, ...] = (
    DiscordActivityField.RAM,
    DiscordActivityField.CPU,
    DiscordActivityField.PLAYERS,
    DiscordActivityField.APP,
    DiscordActivityField.DISK_ALERT,
)


def parse_discord_activity_fields(
    raw_value: str | Iterable[str | DiscordActivityField],
    *,
    source: str,
) -> tuple[DiscordActivityField, ...]:
    raw_items: Iterable[str | DiscordActivityField]
    if isinstance(raw_value, str):
        raw_items = tuple(item.strip() for item in raw_value.split(","))
    else:
        raw_items = raw_value

    parsed_fields: list[DiscordActivityField] = []
    seen_fields: set[DiscordActivityField] = set()
    for raw_item in raw_items:
        if isinstance(raw_item, DiscordActivityField):
            field = raw_item
        else:
            item_text = str(raw_item).strip()
            if not item_text:
                continue
            try:
                field = DiscordActivityField(item_text)
            except ValueError as xcp:
                raise ValueError(
                    f"{source} contains an unknown activity field {item_text!r}. "
                    "Valid fields: ram, cpu, players, app, disk_alert."
                ) from xcp
        if field in seen_fields:
            raise ValueError(f"{source} must not contain duplicate activity fields.")
        seen_fields.add(field)
        parsed_fields.append(field)
    return tuple(parsed_fields)


def format_discord_activity_fields(fields: Iterable[DiscordActivityField]) -> str:
    return ", ".join(field.value for field in fields)


class DiscordActivitySettings(BaseModel):
    fallback_text: str = NAME
    prefix: str = ""
    separator: str = " | "
    suffix: str = ""
    refresh_interval_seconds: int = 3
    units_per_app: int = 2
    alt_text_percentage: int = 50
    fields: tuple[DiscordActivityField, ...] = _DEFAULT_DISCORD_ACTIVITY_FIELDS

    model_config = ConfigDict(extra="forbid")

    @field_validator("fallback_text")
    @classmethod
    def _validate_fallback_text(cls, value: str) -> str:
        text = str(value).strip()
        if len(text) > 80:
            raise ValueError("discord activity fallback_text must not exceed 80 characters.")
        return text

    @field_validator("prefix", "suffix")
    @classmethod
    def _validate_affix_text(cls, value: str) -> str:
        text = str(value)
        if len(text) > 40:
            raise ValueError("discord activity prefix/suffix must not exceed 40 characters.")
        return text

    @field_validator("separator")
    @classmethod
    def _validate_separator_text(cls, value: str) -> str:
        text = str(value)
        if not text:
            raise ValueError("discord activity separator must not be empty.")
        if len(text) > 16:
            raise ValueError("discord activity separator must not exceed 16 characters.")
        return text

    @field_validator("refresh_interval_seconds")
    @classmethod
    def _validate_refresh_interval_seconds(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("discord activity refresh_interval_seconds must be an integer.")
        if value < 1 or value > 60:
            raise ValueError("discord activity refresh_interval_seconds must be between 1 and 60.")
        return value

    @field_validator("units_per_app")
    @classmethod
    def _validate_units_per_app(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("discord activity units_per_app must be an integer.")
        if value < 1 or value > 20:
            raise ValueError("discord activity units_per_app must be between 1 and 20.")
        return value

    @field_validator("alt_text_percentage")
    @classmethod
    def _validate_alt_text_percentage(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("discord activity alt_text_percentage must be an integer.")
        if value < 0 or value > 100:
            raise ValueError("discord activity alt_text_percentage must be between 0 and 100.")
        return value

    @field_validator("fields", mode="before")
    @classmethod
    def _validate_fields(
        cls,
        value: tuple[DiscordActivityField, ...] | list[DiscordActivityField] | str | None,
    ) -> tuple[DiscordActivityField, ...]:
        if value is None:
            return ()
        return parse_discord_activity_fields(value, source="discord activity fields")

    @model_validator(mode="after")
    def _validate_non_empty_output(self) -> "DiscordActivitySettings":
        if self.fields:
            return self
        if self.fallback_text:
            return self
        raise ValueError("discord activity settings require at least one field or fallback_text.")


class DiscordSettings(BaseModel):
    activity: DiscordActivitySettings = Field(default_factory=DiscordActivitySettings)

    model_config = ConfigDict(extra="forbid")


def build_discord_oauth_url(bot_id: hikari.Snowflakeish | int | str, *, install_type: OAuthInstallType) -> str:
    return urlunsplit(
        (
            "https",
            "discord.com",
            "/oauth2/authorize",
            urlencode(
                {
                    "client_id": str(int(hikari.Snowflake(bot_id))),
                    "integration_type": install_type.integration_type,
                    "scope": install_type.scopes,
                }
            ),
            "",
        )
    )


def normalise_oauth_links(
    links: PersistedOAuthLinks,
    *,
    supported_install_types: Iterable[OAuthInstallType],
) -> PersistedOAuthLinks:
    payload: dict[str, str | None] = {}
    for install_type in supported_install_types:
        payload[install_type.value] = links.configured_url(install_type)
    return PersistedOAuthLinks(**payload)


def supported_oauth_install_types(application: object) -> frozenset[OAuthInstallType]:
    raw_config = getattr(application, "integration_types_config", None)
    if isinstance(raw_config, Mapping):
        supported: set[OAuthInstallType] = set()
        for raw_key in raw_config:
            key_text = str(raw_key)
            if isinstance(raw_key, int):
                key_number = str(raw_key)
            elif isinstance(raw_key, enum.Enum) and isinstance(raw_key.value, int):
                key_number = str(raw_key.value)
            else:
                key_number = None
            if key_text in {"GUILD_INSTALL", OAuthInstallType.GUILD.integration_type} or (
                key_number == OAuthInstallType.GUILD.integration_type
            ):
                supported.add(OAuthInstallType.GUILD)
            elif key_text in {"USER_INSTALL", OAuthInstallType.USER.integration_type} or (
                key_number == OAuthInstallType.USER.integration_type
            ):
                supported.add(OAuthInstallType.USER)
        if supported:
            log.debug(
                "Resolved supported OAuth install types from application config: raw_keys=%s resolved=%s",
                [str(key) for key in raw_config],
                [install_type.value for install_type in sorted(supported, key=lambda item: item.integration_type)],
            )
            return frozenset(supported)

    # Discord defaults newly unmanaged apps to guild installs when no explicit config is surfaced.
    log.debug(
        "Falling back to guild-only OAuth install support; application integration_types_config=%r",
        raw_config,
    )
    return frozenset({OAuthInstallType.GUILD})


class BotConfiguration(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    disk_preferences: PersistedDiskPreferences = Field(default_factory=PersistedDiskPreferences)
    discord_settings: DiscordSettings = Field(default_factory=DiscordSettings)
    maintenance: PersistedMaintenanceSettings = Field(default_factory=PersistedMaintenanceSettings)
    node_capacity: NodeCapacityProfile = Field(default_factory=lambda: default_node_capacity_profile())
    node_font_sources: NodeFontSourceSettings = Field(default_factory=NodeFontSourceSettings)
    restart_state: PersistedRestartState = Field(default_factory=PersistedRestartState)
    steamcmd_path: str = "steamcmd"
    voice_targets: dict[str, PersistedVoiceTarget] = Field(default_factory=dict)
    oauth: PersistedOAuthLinks = Field(default_factory=PersistedOAuthLinks, alias="OAuth")
    known_bots: dict[str, BotMetadataSnapshot] = Field(default_factory=dict, alias="KnownBots")

    @field_validator("oauth")
    @classmethod
    def _validate_oauth(cls, value: PersistedOAuthLinks) -> PersistedOAuthLinks:
        return value

    @field_validator("known_bots")
    @classmethod
    def _validate_known_bots(cls, value: dict[str, BotMetadataSnapshot]) -> dict[str, BotMetadataSnapshot]:
        normalised: dict[str, BotMetadataSnapshot] = {}
        for bot_id, snapshot in value.items():
            bot_id_text = normalise_discord_id_text(bot_id, source="KnownBots key")
            if snapshot.profile.id != bot_id_text:
                raise ValueError("KnownBots keys must match snapshot.profile.id.")
            normalised[bot_id_text] = snapshot
        return normalised

    @field_validator("steamcmd_path", mode="before")
    @classmethod
    def _validate_steamcmd_path(cls, value: object) -> str:
        if value is None:
            return "steamcmd"
        text = str(value).strip()
        if not text:
            return "steamcmd"
        return text


def steamcmd_command_prefix(command_path: str) -> tuple[str, ...]:
    stripped = command_path.strip()
    if not stripped:
        raise ValueError("SteamCMD path must not be empty.")
    if stripped.casefold().endswith(".sh"):
        return ("bash", stripped)
    return (stripped,)


def load_bot_configuration(path: Path) -> BotConfiguration:
    if not path.exists():
        return BotConfiguration()

    raw = json.loads(path.read_text(STR_ENCODE))
    loaded = BotConfiguration.model_validate(raw)
    if isinstance(raw, Mapping) and (
        "node_capacity" not in raw or "discord_settings" not in raw or "node_font_sources" not in raw
    ):
        save_bot_configuration(path, loaded)
    return loaded


def save_bot_configuration(path: Path, bot_config: BotConfiguration) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bot_config.model_dump(mode="json", by_alias=True), sort_keys=True, indent=4),
        STR_ENCODE,
    )


def upsert_known_bot_snapshot(path: Path, snapshot: BotMetadataSnapshot) -> BotConfiguration:
    bot_config = BotConfiguration()
    if path.exists():
        bot_config = load_bot_configuration(path)
    bot_config.known_bots[snapshot.profile.id] = snapshot
    save_bot_configuration(path, bot_config)
    return bot_config


def sync_local_oauth_configuration(
    path: Path,
    *,
    supported_install_types: Iterable[OAuthInstallType],
) -> BotConfiguration:
    bot_config = BotConfiguration()
    if path.exists():
        bot_config = load_bot_configuration(path)

    normalised_oauth = normalise_oauth_links(
        bot_config.oauth,
        supported_install_types=supported_install_types,
    )
    if bot_config.oauth.serializable() != normalised_oauth.serializable():
        log.info(
            "Normalised OAuth config at %s: before=%s after=%s",
            path,
            bot_config.oauth.serializable(),
            normalised_oauth.serializable(),
        )
        bot_config.oauth = normalised_oauth
        save_bot_configuration(path, bot_config)
    return bot_config


def build_local_bot_metadata_snapshot(
    *,
    bot_id: hikari.Snowflakeish | int | str,
    label: str,
    bot_profile: BotProfileName,
    oauth: PersistedOAuthLinks,
    mod_web: BotMetadataModWeb | None = None,
    presentation: BotMetadataPresentation | None = None,
) -> BotMetadataSnapshot:
    return BotMetadataSnapshot(
        profile=BotMetadataProfile(
            id=normalise_discord_id_text(bot_id, source="local bot metadata id"),
            label=label,
            bot_profile=bot_profile,
        ),
        features=BotMetadataFeatures(oauth=oauth, mod_web=mod_web, presentation=presentation),
    )


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
        primary_tts_channel = value.get("primary_tts_channel", value.get("tts_channel"))
        secondary_tts_channel = value.get("secondary_tts_channel")
        primary_tts_listen_enabled = value.get("primary_tts_listen_enabled", True)
        secondary_tts_listen_enabled = value.get(
            "secondary_tts_listen_enabled",
            secondary_tts_channel is not None,
        )
        if voice_channel is None or primary_tts_channel is None:
            raise ValueError(f"{source}[{guild_key!r}] must include both 'voice_channel' and 'primary_tts_channel'.")
        if not isinstance(primary_tts_listen_enabled, bool):
            raise ValueError(f"{source}[{guild_key!r}].primary_tts_listen_enabled must be a boolean.")
        if not isinstance(secondary_tts_listen_enabled, bool):
            raise ValueError(f"{source}[{guild_key!r}].secondary_tts_listen_enabled must be a boolean.")
        relay_tts_enabled = value.get("relay_tts_enabled", False)
        if not isinstance(relay_tts_enabled, bool):
            raise ValueError(f"{source}[{guild_key!r}].relay_tts_enabled must be a boolean.")

        try:
            secondary_channel = (
                hikari.Snowflake(str(secondary_tts_channel).strip()) if secondary_tts_channel is not None else None
            )
            targets[guild_id] = VoiceTargetConfig(
                guild_id=guild_id,
                voice_channel=hikari.Snowflake(str(voice_channel).strip()),
                primary_tts_channel=hikari.Snowflake(str(primary_tts_channel).strip()),
                primary_tts_listen_enabled=primary_tts_listen_enabled,
                secondary_tts_channel=secondary_channel,
                secondary_tts_listen_enabled=secondary_tts_listen_enabled if secondary_channel is not None else False,
                relay_tts_enabled=relay_tts_enabled,
            )
        except ValueError as xcp:
            raise ValueError(f"{source}[{guild_key!r}] contains an invalid channel id.") from xcp
        if (
            targets[guild_id].secondary_tts_channel is not None
            and targets[guild_id].secondary_tts_channel == targets[guild_id].primary_tts_channel
        ):
            raise ValueError(f"{source}[{guild_key!r}].secondary_tts_channel must differ from primary_tts_channel.")

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
    PORTAL = enum.auto()


class DataAuthorityMode(enum.StrEnum):
    LOCAL = enum.auto()
    REMOTE = enum.auto()


BotMetadataProfile.model_rebuild()
BotMetadataSnapshot.model_rebuild()


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
class ModWebServerConfig:
    node_name: str
    host: str
    port: int
    public_base_url: str
    node_api_base_url: str
    token_secret: str | None


@dataclass(frozen=True, slots=True)
class NodeApiServerConfig:
    host: str
    port: int
    public_base_url: str
    node_api_base_url: str


@dataclass(frozen=True, slots=True)
class ModWebAuthConfig:
    discord_client_id: str | None
    discord_client_secret: str | None
    redirect_url: str
    bypass_enabled: bool = False
    session_cache_directory: Path | None = None

    @property
    def enabled(self) -> bool:
        return self.bypass_enabled or (self.discord_client_id is not None and self.discord_client_secret is not None)


_RAM_POINT_BYTES: int = 500 * 1024 * 1024


class ResourcePointSet(BaseModel):
    cpu_points: int = 0
    ram_points: int = 0

    model_config = ConfigDict(extra="forbid")

    @field_validator("cpu_points", "ram_points")
    @classmethod
    def _validate_point_value(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("resource points must be integers.")
        if value < 0:
            raise ValueError("resource points must not be negative.")
        return value


class NodeCapacityProfile(BaseModel):
    cpu_points_total: int
    ram_points_total: int
    cpu_points_reserved: int = 0
    ram_points_reserved: int = 0

    model_config = ConfigDict(extra="forbid")

    @field_validator("cpu_points_total", "ram_points_total", "cpu_points_reserved", "ram_points_reserved")
    @classmethod
    def _validate_capacity_value(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("node capacity values must be integers.")
        if value < 0:
            raise ValueError("node capacity values must not be negative.")
        return value

    @model_validator(mode="after")
    def _validate_reserved_capacity(self) -> "NodeCapacityProfile":
        if self.cpu_points_reserved > self.cpu_points_total:
            raise ValueError("cpu_points_reserved must not exceed cpu_points_total.")
        if self.ram_points_reserved > self.ram_points_total:
            raise ValueError("ram_points_reserved must not exceed ram_points_total.")
        return self

    @property
    def cpu_points_available(self) -> int:
        return self.cpu_points_total - self.cpu_points_reserved

    @property
    def ram_points_available(self) -> int:
        return self.ram_points_total - self.ram_points_reserved


def voice_capacity_reserve_enabled(profile: "BotProfileConfig") -> bool:
    return profile.has_service(BotService.MUSIC) or profile.has_service(BotService.VOICE_TTS)


def default_node_capacity_profile(*, profile: "BotProfileConfig | None" = None) -> NodeCapacityProfile:
    resolved_profile = ACTIVE_BOT_PROFILE if profile is None else profile
    cpu_core_count = psutil.cpu_count(logical=True) or psutil.cpu_count(logical=False) or 1
    ram_total_bytes = psutil.virtual_memory().total
    cpu_points_total = max(1, cpu_core_count)
    ram_points_total = max(1, int(ram_total_bytes // _RAM_POINT_BYTES))
    voice_enabled = voice_capacity_reserve_enabled(resolved_profile)
    cpu_points_reserved = min(cpu_points_total, 2 + (1 if voice_enabled else 0))
    ram_points_reserved = min(ram_points_total, 2 + (2 if voice_enabled else 0))
    return NodeCapacityProfile(
        cpu_points_total=cpu_points_total,
        ram_points_total=ram_points_total,
        cpu_points_reserved=cpu_points_reserved,
        ram_points_reserved=ram_points_reserved,
    )


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
                primary_tts_channel=hikari.Snowflake(legacy_tts_channel),
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
    BotProfileName.PORTAL: BotProfileConfig(
        name=BotProfileName.PORTAL,
        command_groups=(),
        services=frozenset(),
    ),
}


def _parse_bot_profile(raw: str | None) -> BotProfileName:
    if not raw:
        raise ValueError("BOT_PROFILE must be set")

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


def _parse_env_flag(raw: str | None, *, var_name: str) -> bool:
    if raw is None:
        return False
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{var_name} must be a boolean flag such as true/false or 1/0")


def _default_port_for_scheme(scheme: HttpScheme) -> int:
    if scheme == "http":
        return 80
    return 443


def _format_http_netloc(*, host: str, scheme: HttpScheme, port: int) -> str:
    if port == _default_port_for_scheme(scheme):
        return host
    return f"{host}:{port}"


def _parsed_http_scheme(parsed: SplitResult) -> HttpScheme:
    return "http" if parsed.scheme == "http" else "https"


def _default_public_http_scheme() -> HttpScheme:
    return "http" if INDEV else "https"


def _require_secure_public_http_scheme(scheme: HttpScheme, *, var_name: str) -> None:
    if scheme == "http" and not INDEV:
        raise ValueError(f"{var_name} must use https outside INDEV.")


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


def _binding_hosts_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    wildcard_hosts = {"0.0.0.0", "::"}
    return left in wildcard_hosts or right in wildcard_hosts


ACTIVE_BOT_PROFILE = BOT_PROFILES[_parse_bot_profile(_env_settings.bot_profile)]
DATA_AUTHORITY_MODE = _data_authority_mode(ACTIVE_BOT_PROFILE)
INDEV = bool(_env_settings.indev)
BYPASS_WEB_AUTH = INDEV and _parse_env_flag(_env_settings.bypass_web_auth, var_name="BYPASS_WEB_AUTH")
DATA_AUTHORITY_HOST = _env_settings.data_authority_host
DATA_AUTHORITY_TOKEN = _env_settings.data_authority_token
DATA_AUTHORITY_TIMEOUT_SECONDS = _parse_timeout_seconds(_env_settings.data_authority_timeout_seconds, default=2.0)
DATA_AUTHORITY_CACHE_DIR = Path(_env_settings.data_authority_cache_dir or ".cache/authority")
DATA_AUTHORITY_PORT = _parse_optional_port(_env_settings.data_authority_port, var_name="DATA_AUTHORITY_PORT")
DATA_AUTHORITY_BIND_HOST = _env_settings.data_authority_bind_host
DATA_AUTHORITY_BIND_PORT = _parse_optional_port(
    _env_settings.data_authority_bind_port,
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
    if resource is AuthorityResource.NAMES:
        filename = "discord_names.json"
    elif resource is AuthorityResource.USERS:
        filename = "users.json"
    else:
        filename = "bot_registry.json"
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


def fetch_remote_bot_registry() -> dict[str, BotMetadataSnapshot]:
    payload = fetch_remote_resource(AuthorityResource.BOTS)
    return {bot_id: BotMetadataSnapshot.model_validate(snapshot) for bot_id, snapshot in payload.items()}


def sync_remote_bot_metadata(snapshot: BotMetadataSnapshot) -> BotMetadataSnapshot:
    client = authority_client()
    if client is None:
        raise RuntimeError("Remote authority client is not configured")
    response = client.post_json(
        "/authority/bots/sync",
        {"data": snapshot.model_dump(mode="json")},
    )
    return BotMetadataSnapshot.model_validate(response_data(response))


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


APP_PATH = Path(_require_loaded_setting(_env_settings.dir_app, var_name="DIR_APP"))
DISCORD_GUILD = hikari.Snowflake(_require_loaded_setting(_env_settings.discord_guild, var_name="DISCORD_GUILD"))
STARTED_CHANNEL = hikari.Snowflake(_env_settings.started_channel) if _env_settings.started_channel else None
VOICE_CHANNEL = hikari.Snowflake(_env_settings.voice_channel) if _env_settings.voice_channel else None
TTS_CHANNEL = hikari.Snowflake(_env_settings.tts_channel) if _env_settings.tts_channel else None
VOICE_TARGETS = _parse_voice_targets(
    _env_settings.voice_targets,
    default_guild_id=DISCORD_GUILD,
    legacy_voice_channel=VOICE_CHANNEL,
    legacy_tts_channel=TTS_CHANNEL,
)

TTS_ENGINE = (_env_settings.tts_engine or "auto").lower()
TTS_VOICE = _env_settings.tts_voice or "en-gb-x-rp"
TTS_VARIANT = _env_settings.tts_variant
TTS_PIPER_MODEL = _env_settings.tts_piper_model
TTS_PIPER_CONFIG = _env_settings.tts_piper_config
TTS_PIPER_DATA_DIR = _env_settings.tts_piper_data_dir
MUSIC_YTDLP_COOKIE_FILE = (
    Path(_env_settings.music_ytdlp_cookie_file).expanduser() if _env_settings.music_ytdlp_cookie_file else None
)
MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS = _env_settings.music_ytdlp_youtube_extractor_args


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
        return f"{_default_public_http_scheme()}://{public_ip()}"

    parsed = _parse_http_reference(
        raw,
        var_name="PUBLIC_BASE_URL",
        default_scheme=_default_public_http_scheme(),
        allow_path=True,
    )
    scheme = _parsed_http_scheme(parsed)
    _require_secure_public_http_scheme(scheme, var_name="PUBLIC_BASE_URL")
    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            _normalise_public_base_path(parsed.path),
            "",
            "",
        )
    )


def resolve_public_uploads_base_url(public_base_url: str) -> str:
    parsed = _parse_http_reference(
        public_base_url, var_name="PUBLIC_BASE_URL", default_scheme="https", allow_path=False
    )
    return urlunsplit((parsed.scheme, parsed.netloc, "/uploads/", "", ""))


def resolve_mod_web_public_base_url(
    raw: str | None,
    *,
    public_base_url: str,
) -> str:
    reference = raw if raw is not None else public_base_url
    source_name = "MOD_WEB_PUBLIC_BASE_URL" if raw is not None else "PUBLIC_BASE_URL"
    default_scheme = _default_public_http_scheme() if "://" not in reference else "https"
    parsed = _parse_http_reference(reference, var_name=source_name, default_scheme=default_scheme, allow_path=False)
    host = parsed.hostname
    if host is None:
        raise ValueError(f"{source_name} must include a host.")
    scheme = _parsed_http_scheme(parsed)
    _require_secure_public_http_scheme(scheme, var_name=source_name)
    return urlunsplit(
        (
            scheme,
            _format_http_netloc(host=host, scheme=scheme, port=parsed.port or _default_port_for_scheme(scheme)),
            "",
            "",
            "",
        )
    )


def resolve_node_api_public_base_url(
    raw: str | None,
    *,
    mod_web_public_base_url: str,
) -> str:
    reference = raw if raw is not None else mod_web_public_base_url
    source_name = "NODE_API_PUBLIC_BASE_URL" if raw is not None else "MOD_WEB_PUBLIC_BASE_URL"
    default_scheme = _default_public_http_scheme() if "://" not in reference else "https"
    parsed = _parse_http_reference(reference, var_name=source_name, default_scheme=default_scheme, allow_path=False)
    host = parsed.hostname
    if host is None:
        raise ValueError(f"{source_name} must include a host.")
    scheme = _parsed_http_scheme(parsed)
    _require_secure_public_http_scheme(scheme, var_name=source_name)
    return urlunsplit(
        (
            scheme,
            _format_http_netloc(host=host, scheme=scheme, port=parsed.port or _default_port_for_scheme(scheme)),
            "",
            "",
            "",
        )
    )


def resolve_node_api_base_url(public_base_url: str, *, source_name: str = "MOD_WEB_PUBLIC_BASE_URL") -> str:
    parsed = _parse_http_reference(
        public_base_url,
        var_name=source_name,
        default_scheme="https",
        allow_path=False,
    )
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/node", "", ""))


def resolve_mod_web_auth_redirect_url(raw: str | None, *, mod_web_public_base_url: str) -> str:
    if raw is not None:
        parsed = _parse_http_reference(
            raw,
            var_name="MOD_WEB_AUTH_REDIRECT_URL",
            default_scheme=_default_public_http_scheme(),
            allow_path=True,
        )
        scheme = _parsed_http_scheme(parsed)
        _require_secure_public_http_scheme(scheme, var_name="MOD_WEB_AUTH_REDIRECT_URL")
        if parsed.path in {"", "/"}:
            raise ValueError("MOD_WEB_AUTH_REDIRECT_URL must include the Discord OAuth callback path.")
        return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))

    parsed = _parse_http_reference(
        mod_web_public_base_url,
        var_name="MOD_WEB_PUBLIC_BASE_URL",
        default_scheme="https",
        allow_path=False,
    )
    return urlunsplit((parsed.scheme, parsed.netloc, "/auth/discord/callback", "", ""))


def parse_mod_web_build_sha(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", value):
        raise ValueError("MOD_WEB_BUILD_SHA must be a 7-40 character hexadecimal Git commit SHA.")
    return value


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
    allow_insecure_remote: bool = False,
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
    if mode is DataAuthorityMode.REMOTE and scheme != "https" and not allow_insecure_remote:
        raise ValueError("Remote authority endpoints must use https.")
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

    bind_host = _parse_bind_host(raw_host) or DEFAULT_DATA_AUTHORITY_BIND_HOST
    bind_port = raw_port or DEFAULT_DATA_AUTHORITY_BIND_PORT
    return AuthorityServerBinding(host=bind_host, port=bind_port)


RAW_PUBLIC_BASE_URL = _env_settings.public_base_url
PUBLIC_IP = public_ip()
PUBLIC_ADDR = resolve_public_addr(RAW_PUBLIC_BASE_URL, public_ip=PUBLIC_IP)
PUBLIC_BASE_URL = resolve_public_base_url(RAW_PUBLIC_BASE_URL)
PUBLIC_UPLOADS_BASE_URL = resolve_public_uploads_base_url(PUBLIC_BASE_URL)
NODE_NAME = _env_settings.node_name or ACTIVE_BOT_PROFILE.name.value
NODE_API_TOKEN_SECRET = _env_settings.node_api_token_secret or DATA_AUTHORITY_TOKEN
NODE_API_BIND_HOST = _parse_bind_host(_env_settings.node_api_bind_host)
NODE_API_PORT = _parse_optional_port(_env_settings.node_api_port, var_name="NODE_API_PORT")
MOD_WEB_BIND_HOST = _parse_bind_host(_env_settings.mod_web_bind_host) or "0.0.0.0"
MOD_WEB_PORT = _parse_optional_port(_env_settings.mod_web_port, var_name="MOD_WEB_PORT") or 3180
MOD_WEB_PUBLIC_BASE_URL = resolve_mod_web_public_base_url(
    _env_settings.mod_web_public_base_url,
    public_base_url=PUBLIC_BASE_URL,
)
NODE_API_PUBLIC_BASE_URL = resolve_node_api_public_base_url(
    _env_settings.node_api_public_base_url,
    mod_web_public_base_url=MOD_WEB_PUBLIC_BASE_URL,
)
PUBLISHED_NODE_API_BASE_URL = (
    resolve_node_api_base_url(NODE_API_PUBLIC_BASE_URL, source_name="NODE_API_PUBLIC_BASE_URL")
    if NODE_API_PORT is not None
    else resolve_node_api_base_url(MOD_WEB_PUBLIC_BASE_URL)
)
MOD_WEB_SERVER = ModWebServerConfig(
    node_name=NODE_NAME,
    host=MOD_WEB_BIND_HOST,
    port=MOD_WEB_PORT,
    public_base_url=MOD_WEB_PUBLIC_BASE_URL,
    node_api_base_url=PUBLISHED_NODE_API_BASE_URL,
    token_secret=NODE_API_TOKEN_SECRET,
)
NODE_API_SERVER = (
    NodeApiServerConfig(
        host=NODE_API_BIND_HOST or MOD_WEB_BIND_HOST,
        port=NODE_API_PORT,
        public_base_url=NODE_API_PUBLIC_BASE_URL,
        node_api_base_url=PUBLISHED_NODE_API_BASE_URL,
    )
    if NODE_API_PORT is not None
    else None
)
if (
    NODE_API_SERVER is not None
    and NODE_API_SERVER.port == MOD_WEB_PORT
    and _binding_hosts_overlap(NODE_API_SERVER.host, MOD_WEB_BIND_HOST)
):
    raise ValueError("NODE_API_PORT must differ from MOD_WEB_PORT when using a dedicated node API server.")
MOD_WEB_AUTH = ModWebAuthConfig(
    discord_client_id=_env_settings.mod_web_discord_client_id,
    discord_client_secret=_env_settings.mod_web_discord_client_secret,
    redirect_url=resolve_mod_web_auth_redirect_url(
        _env_settings.mod_web_auth_redirect_url,
        mod_web_public_base_url=MOD_WEB_PUBLIC_BASE_URL,
    ),
    bypass_enabled=BYPASS_WEB_AUTH,
    session_cache_directory=Path(_env_settings.mod_web_session_cache_dir or ".cache/mod_web_sessions"),
)
MOD_WEB_BUILD_SHA = parse_mod_web_build_sha(_env_settings.mod_web_build_sha)
DATA_AUTHORITY_ENDPOINT = resolve_data_authority_endpoint(
    DATA_AUTHORITY_HOST,
    DATA_AUTHORITY_PORT,
    mode=DATA_AUTHORITY_MODE,
    public_base_url=PUBLIC_BASE_URL,
    raw_public_base_url=RAW_PUBLIC_BASE_URL,
    allow_insecure_remote=INDEV,
)
DATA_AUTHORITY_SERVER_BINDING = resolve_data_authority_server_binding(
    DATA_AUTHORITY_BIND_HOST,
    DATA_AUTHORITY_BIND_PORT,
    endpoint=DATA_AUTHORITY_ENDPOINT,
)
DATA_AUTHORITY_SERVER_ENABLED = DATA_AUTHORITY_MODE is DataAuthorityMode.LOCAL and DATA_AUTHORITY_TOKEN is not None
DIR_LOG = Path("logs")
DIR_TMP = Path(_require_loaded_setting(_env_settings.dir_tmp, var_name="DIR_TMP"))
"/tmp/yukibot"
DIR_OPT = Path(_require_loaded_setting(_env_settings.dir_opt, var_name="DIR_OPT"))  # nginx setup only opt/bot
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
            "json_line": {
                "format": "%(message)s",
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
            "traffic_file": {
                "class": "logging.FileHandler",
                "filename": str(DIR_LOG / "Traffic.log"),
                "mode": "w",
                "formatter": "standard",
                "encoding": STR_ENCODE,
                "filters": ["suppress_known_warnings"],
            },
            "tts_file": {
                "class": "logging.FileHandler",
                "filename": str(DIR_LOG / "TTS.log"),
                "mode": "w",
                "formatter": "standard",
                "encoding": STR_ENCODE,
                "filters": ["suppress_known_warnings"],
            },
            "audit_file": {
                "class": "logging.FileHandler",
                "filename": str(DIR_LOG / "Audit.log"),
                "mode": "w",
                "formatter": "standard",
                "encoding": STR_ENCODE,
                "filters": ["suppress_known_warnings"],
            },
            "tenor_file": {
                "class": "logging.FileHandler",
                "filename": str(DIR_LOG / "Tenor.jsonl"),
                "mode": "w",
                "formatter": "json_line",
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
            LOGGER_TRAFFIC: {
                "level": root_lvl,
                "handlers": ["traffic_file"],
                "propagate": False,
            },
            "aiohttp.access": {
                "level": root_lvl,
                "handlers": ["traffic_file"],
                "propagate": False,
            },
            LOGGER_TTS: {
                "level": root_lvl,
                "handlers": ["tts_file"],
                "propagate": False,
            },
            LOGGER_AUDIT: {
                "level": root_lvl,
                "handlers": ["audit_file"],
                "propagate": False,
            },
            LOGGER_TENOR: {
                "level": root_lvl,
                "handlers": ["tenor_file"],
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
    FILE_USERS.write_text(json.dumps({"sudo": [], "user": [], "visitor": []}, indent=4), STR_ENCODE)


GUESTS_ALLOWED = False
"If unrecognised users should be allowed to use use the unrestricted commands"


EXR_TOK = _env_settings.exg_token


class Singleton(type):
    """Singleton for singles, singlings, singlers, singletones, singlators, singlatees, and singlated..."""

    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class DisplayNameCategory(enum.StrEnum):
    DISCORD = "discord"
    WEB = "web"


class DisplayNameOverrides(BaseModel):
    discord: str | None = None
    web: str | None = None

    def get_for_category(self, category: DisplayNameCategory) -> str | None:
        if category is DisplayNameCategory.DISCORD:
            return self.discord
        if category is DisplayNameCategory.WEB:
            return self.web
        raise ValueError(f"Unsupported display override category `{category}`.")

    def set_for_category(self, category: DisplayNameCategory, value: str | None) -> None:
        if category is DisplayNameCategory.DISCORD:
            self.discord = value
            return
        if category is DisplayNameCategory.WEB:
            self.web = value
            return
        raise ValueError(f"Unsupported display override category `{category}`.")


class UserNames(BaseModel):
    account: str | None = None
    global_name: str | None = None
    avatar_hash: str | None = None
    names: set[str] = Field(default_factory=set)
    nicknames: set[str] = Field(default_factory=set)
    games: dict[str, tuple[str | None, str | None]] = Field(default_factory=dict)
    platform_ids: dict[str, str] = Field(default_factory=dict)
    guild_names: dict[int, str] = Field(default_factory=dict)
    display_overrides: DisplayNameOverrides = Field(default_factory=DisplayNameOverrides)
    is_manual: bool = False


class NameResolutionStatus(enum.StrEnum):
    UNIQUE = enum.auto()
    AMBIGUOUS = enum.auto()
    NOT_FOUND = enum.auto()


@dataclass(frozen=True, slots=True)
class NameResolutionResult:
    status: NameResolutionStatus
    user_id: int | None = None
    candidate_ids: tuple[int, ...] = ()


class PersonaSurface(enum.StrEnum):
    APP = "app"
    DISCORD = "discord"
    WEB = "web"


@dataclass(frozen=True, slots=True)
class Persona:
    discord_id: hikari.Snowflake | None
    discord_username: str | None
    discord_global: str | None
    discord_nicks: dict[hikari.Snowflake, str]
    scopes: dict[str, str]
    web_override: str | None
    discord_override: str | None
    aliases: tuple[str, ...]


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

    @classmethod
    def _persona_from_entry(cls, user_id: int, user: UserNames) -> Persona:
        scope_aliases = {
            scope.casefold(): alias
            for scope, alias_data in user.games.items()
            if (alias := cls._normalised_optional_text(alias_data[0], label=f"{scope} alias")) is not None
        }
        return Persona(
            discord_id=hikari.Snowflake(user_id),
            discord_username=user.account,
            discord_global=user.global_name,
            discord_nicks={
                hikari.Snowflake(guild_id): nickname
                for guild_id, nickname in user.guild_names.items()
                if nickname
            },
            scopes=scope_aliases,
            web_override=user.display_overrides.web,
            discord_override=user.display_overrides.discord,
            aliases=tuple(alias for alias in cls._sorted_name_values(user.nicknames) if alias),
        )

    def persona(self, user_id: int) -> Persona | None:
        user = self.by_id.get(user_id)
        if user is None:
            return None
        return self._persona_from_entry(user_id, user)

    def _normalise_user(self, user: UserNames) -> bool:
        before = user.model_dump(mode="json")
        user.avatar_hash = self._normalised_optional_text(user.avatar_hash, label="Discord avatar hash")
        user.guild_names = {int(guild_id): name for guild_id, name in user.guild_names.items() if name}
        for category in DisplayNameCategory:
            normalised_override = self._normalised_optional_text(
                user.display_overrides.get_for_category(category),
                label=f"{category.value} display override",
            )
            user.display_overrides.set_for_category(category, normalised_override)
        self._sync_known_names(user)
        return user.model_dump(mode="json") != before

    @staticmethod
    def _normalised_optional_text(value: object | None, *, label: str) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if "\n" in text or "\r" in text:
            raise ValueError(f"{label} must be a single line")
        return text

    @staticmethod
    def _normalised_alias_scope(scope: object) -> str:
        text = str(scope).strip().lower()
        if not text:
            raise ValueError("game alias scope must not be empty")
        return text

    @classmethod
    def _normalised_alias_mapping(cls, game_aliases: Mapping[str, object | None] | None) -> dict[str, str | None]:
        if game_aliases is None:
            return {}
        aliases: dict[str, str | None] = {}
        for scope, alias in game_aliases.items():
            scope_key = cls._normalised_alias_scope(scope)
            aliases[scope_key] = cls._normalised_optional_text(alias, label=f"{scope_key} alias")
        return aliases

    @staticmethod
    def _normalised_display_name_category(category: object) -> DisplayNameCategory:
        if isinstance(category, DisplayNameCategory):
            return category
        text = str(category).strip().lower()
        if not text:
            raise ValueError("display override category must not be empty")
        try:
            return DisplayNameCategory(text)
        except ValueError as xcp:
            raise ValueError(f"Unsupported display override category `{text}`.") from xcp

    @staticmethod
    def _alias_conflict_error(alias: str, *, scope: str | None = None) -> ValueError:
        if scope is None:
            return ValueError(f"General alias `{alias}` is already used by another user.")
        return ValueError(f"{scope.title()} alias `{alias}` is already used by another user.")

    @staticmethod
    def _platform_id_conflict_error(platform: str, platform_id: str) -> ValueError:
        label = "Steam ID" if platform == "steam" else f"{platform.title()} ID"
        return ValueError(f"{label} `{platform_id}` is already linked to another user.")

    @staticmethod
    def _game_uuid_conflict_error(scope: str, game_uuid: str) -> ValueError:
        return ValueError(f"{scope.title()} UUID `{game_uuid}` is already used by another user.")

    def _assert_unique_general_alias(self, user_id: int, alias: str) -> None:
        conflicting_ids = self.by_alias.get(alias.lower(), set()) - {user_id}
        if conflicting_ids:
            raise self._alias_conflict_error(alias)

    def _assert_unique_game_alias(self, user_id: int, scope: str, alias: str) -> None:
        normalised_scope = self._normalised_alias_scope(scope)
        conflicting_ids = self._resolve_game_alias_ids(alias, normalised_scope) - {user_id}
        if conflicting_ids:
            raise self._alias_conflict_error(alias, scope=normalised_scope)

    def _assert_unique_platform_id(self, user_id: int, platform: str, platform_id: str) -> None:
        conflicting_user_id = self.by_platform_id.get(platform, {}).get(platform_id)
        if conflicting_user_id is not None and conflicting_user_id != user_id:
            raise self._platform_id_conflict_error(platform, platform_id)

    def _assert_unique_game_uuid(self, user_id: int, scope: str, game_uuid: str) -> None:
        conflicting_ids = self._resolve_game_uuid_ids(game_uuid, scope) - {user_id}
        if conflicting_ids:
            raise self._game_uuid_conflict_error(scope, game_uuid)

    @classmethod
    def _normalised_game_uuid(cls, scope: object, game_uuid: object | None) -> str | None:
        scope_key = cls._normalised_alias_scope(scope)
        value = cls._normalised_optional_text(game_uuid, label=f"{scope_key} uuid")
        if value is None:
            return None
        if scope_key != "minecraft":
            return value
        compact_uuid = value.replace("-", "").lower()
        if re.fullmatch(r"[0-9a-f]{32}", compact_uuid) is None:
            raise ValueError("minecraft uuid must be 32 hex characters or 36 characters with hyphens")
        return (
            f"{compact_uuid[:8]}-{compact_uuid[8:12]}-{compact_uuid[12:16]}-{compact_uuid[16:20]}-{compact_uuid[20:]}"
        )

    @staticmethod
    def _set_names_payload(
        user_id: int,
        user: UserNames,
        *,
        guild_id: hikari.Snowflakeish | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "user_id": user_id,
            "account": user.account,
            "global_name": user.global_name,
            "avatar_hash": user.avatar_hash,
        }
        if guild_id is not None:
            scoped_guild_id = int(guild_id)
            payload["guild_id"] = scoped_guild_id
            payload["guild_name"] = user.guild_names.get(scoped_guild_id)
        return payload

    def _persist_identity_change(
        self,
        user_id: int,
        user: UserNames,
        *,
        guild_id: hikari.Snowflakeish | None = None,
    ) -> None:
        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(
            NameMutationKind.SET_NAMES,
            **self._set_names_payload(user_id, user, guild_id=guild_id),
        )

    def _apply_discord_identity(self, user: UserNames, discord_user: hikari.User | hikari.Member) -> bool:
        before = user.model_dump(mode="json")
        user.account = discord_user.username
        user.global_name = discord_user.global_name
        user.avatar_hash = self._normalised_optional_text(
            discord_user.avatar_hash,
            label="Discord avatar hash",
        )
        if isinstance(discord_user, hikari.Member):
            guild_id = int(discord_user.guild_id)
            if discord_user.nickname:
                user.guild_names[guild_id] = discord_user.nickname
            else:
                user.guild_names.pop(guild_id, None)
        self._sync_known_names(user)
        return user.model_dump(mode="json") != before

    def sync_members(self, members: Iterable[hikari.Member]) -> int:
        changed_members: list[hikari.Member] = []
        for member in members:
            user_id = int(member.id)
            user = self.by_id.setdefault(user_id, UserNames())
            if self._apply_discord_identity(user, member):
                changed_members.append(member)

        if not changed_members:
            return 0

        self._rebuild_aliases()
        self._dump()
        for member in changed_members:
            user_id = int(member.id)
            self._queue_remote_mutation(
                NameMutationKind.SET_NAMES,
                **self._set_names_payload(user_id, self.by_id[user_id], guild_id=member.guild_id),
            )
        return len(changed_members)

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

        self._persist_identity_change(user_id, user, guild_id=guild_id)
        return True

    def upsert_manual_user(
        self,
        user_id: int,
        *,
        display_name: object | None = None,
        account: object | None = None,
        nicknames: Iterable[object] = (),
        game_aliases: Mapping[str, object | None] | None = None,
    ) -> bool:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        nickname_values = tuple(nicknames)
        alias_updates = self._normalised_alias_mapping(game_aliases)
        normalised_nicknames: list[str] = []
        for nickname in nickname_values:
            alias = self._normalised_optional_text(nickname, label="nickname")
            if alias is not None:
                self._assert_unique_general_alias(user_id, alias)
                normalised_nicknames.append(alias)
        for scope, alias in alias_updates.items():
            if alias is not None:
                self._assert_unique_game_alias(user_id, scope, alias)

        user = self.by_id.setdefault(user_id, UserNames())
        before = user.model_dump(mode="json")
        user.is_manual = True

        account_name = self._normalised_optional_text(account, label="account")
        display = self._normalised_optional_text(display_name, label="display_name")
        if account_name is not None:
            user.account = account_name
        if display is not None:
            user.global_name = display

        for alias in normalised_nicknames:
            user.nicknames.add(alias)

        for scope, alias in alias_updates.items():
            if alias is None:
                user.games.pop(scope, None)
            else:
                user.games[scope] = (alias, user.games.get(scope, (None, None))[1])

        self._sync_known_names(user)
        if user.model_dump(mode="json") == before:
            return False

        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(
            NameMutationKind.UPSERT_MANUAL_USER,
            user_id=user_id,
            display_name=display,
            account=account_name,
            nicknames=normalised_nicknames,
            game_aliases=alias_updates,
        )
        return True

    def is_manual_user(self, user_id: int) -> bool:
        user = self.by_id.get(user_id)
        return user.is_manual if user is not None else False

    def get_display_override(self, user_id: int, category: DisplayNameCategory | str) -> str | None:
        user = self.by_id.get(user_id)
        if user is None:
            return None
        category_key = self._normalised_display_name_category(category)
        return user.display_overrides.get_for_category(category_key)

    def set_display_override(
        self, user_id: int, category: DisplayNameCategory | str, display_name: object | None
    ) -> bool:
        category_key = self._normalised_display_name_category(category)
        value = self._normalised_optional_text(display_name, label=f"{category_key.value} display override")
        user = self.by_id.setdefault(user_id, UserNames())
        before = user.model_dump(mode="json")
        user.display_overrides.set_for_category(category_key, value)
        if user.model_dump(mode="json") == before:
            return False

        self._dump()
        self._queue_remote_mutation(
            NameMutationKind.SET_DISPLAY_OVERRIDE,
            user_id=user_id,
            category=category_key.value,
            display_name=value,
        )
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
            name = self._normalised_optional_text(event.get("name"), label="nickname")
            if name is None:
                raise ValueError("add_name mutation nickname must not be empty")
            is_name = bool(event.get("is_name", True))
            if not is_name:
                self._assert_unique_general_alias(raw_user_id, name)
                user.nicknames.add(name)
        elif kind is NameMutationKind.CLEAN_NAMES:
            names = event.get("names")
            if not isinstance(names, list):
                raise ValueError("clean_names mutation requires names list")
            allowed_names = {str(name) for name in names if str(name)}
            user.global_name = user.global_name if user.global_name in allowed_names else None
            user.guild_names = {guild_id: name for guild_id, name in user.guild_names.items() if name in allowed_names}
            self._sync_known_names(user)
        elif kind is NameMutationKind.SET_DISPLAY_OVERRIDE:
            category = self._normalised_display_name_category(event.get("category"))
            display_name = self._normalised_optional_text(
                event.get("display_name"),
                label=f"{category.value} display override",
            )
            user.display_overrides.set_for_category(category, display_name)
        elif kind is NameMutationKind.REMOVE_GAME_ALIAS:
            user.games.pop(str(event["scope"]).lower(), None)
        elif kind is NameMutationKind.REMOVE_NAME:
            user.nicknames.discard(str(event["name"]))
        elif kind is NameMutationKind.SET_GAME_ALIAS:
            scope = self._normalised_alias_scope(event.get("scope"))
            alias = self._normalised_optional_text(event.get("alias"), label=f"{scope} alias")
            if alias is None:
                raise ValueError("set_game_alias mutation alias must not be empty")
            self._assert_unique_game_alias(raw_user_id, scope, alias)
            user.games[scope] = (alias, user.games.get(scope, (None, None))[1])
        elif kind is NameMutationKind.SET_GAME_UUID:
            scope = self._normalised_alias_scope(event.get("scope"))
            uuid = self._normalised_game_uuid(scope, event.get("uuid"))
            if uuid is not None:
                self._assert_unique_game_uuid(raw_user_id, scope, uuid)
            name, _ = user.games.get(scope, (None, None))
            if name is None and uuid is None:
                user.games.pop(scope, None)
            else:
                user.games[scope] = (name, uuid)
        elif kind is NameMutationKind.SET_NAMES:
            account_in_event = "account" in event
            global_name_in_event = "global_name" in event
            avatar_hash_in_event = "avatar_hash" in event
            raw_account = event.get("account")
            if account_in_event:
                user.account = str(raw_account) if raw_account is not None else None
            raw_global_name = event.get("global_name")
            if global_name_in_event:
                user.global_name = str(raw_global_name) if raw_global_name is not None else None
            if avatar_hash_in_event:
                user.avatar_hash = self._normalised_optional_text(
                    event.get("avatar_hash"),
                    label="Discord avatar hash",
                )
            raw_guild_id = event.get("guild_id")
            raw_guild_names = event.get("guild_names")
            if raw_guild_id is not None and raw_guild_names is not None:
                raise ValueError("set_names mutation can't include both guild_id and guild_names")
            if raw_guild_names is not None:
                if not isinstance(raw_guild_names, dict):
                    raise ValueError("set_names mutation guild_names must be an object")
                user.guild_names = {int(guild_id): str(name) for guild_id, name in raw_guild_names.items() if str(name)}
            elif raw_guild_id is not None:
                if not isinstance(raw_guild_id, int):
                    raise ValueError("set_names mutation guild_id must be an integer")
                raw_guild_name = event.get("guild_name")
                if raw_guild_name is None:
                    user.guild_names.pop(raw_guild_id, None)
                else:
                    guild_name = str(raw_guild_name)
                    if guild_name:
                        user.guild_names[raw_guild_id] = guild_name
                    else:
                        user.guild_names.pop(raw_guild_id, None)
            elif "guild_name" in event:
                raise ValueError("set_names mutation guild_name requires guild_id")
            legacy_names = event.get("names")
            if legacy_names is not None and not isinstance(legacy_names, list):
                raise ValueError("set_names mutation names must be a list when provided")
            if account_in_event or global_name_in_event or raw_guild_names is not None or raw_guild_id is not None:
                self._sync_known_names(user)
            elif legacy_names is not None:
                allowed_names = {str(name) for name in legacy_names if str(name)}
                user.global_name = user.global_name if user.global_name in allowed_names else None
                user.guild_names = {
                    guild_id: name for guild_id, name in user.guild_names.items() if name in allowed_names
                }
                self._sync_known_names(user)
        elif kind is NameMutationKind.SET_PLATFORM_ID:
            platform: str = self._norm_platform_key(event.get("platform"))
            platform_id: object | None = event.get("platform_id")
            value: str | None = (
                self._norm_steam_id(platform_id) if platform == "steam" else self._norm_platform_id(platform_id)
            )
            if value is None:
                user.platform_ids.pop(platform, None)
            else:
                self._assert_unique_platform_id(raw_user_id, platform, value)
                user.platform_ids[platform] = value
        elif kind is NameMutationKind.UPSERT_MANUAL_USER:
            user.is_manual = True
            account: str | None = self._normalised_optional_text(event.get("account"), label="account")
            display_name: str | None = self._normalised_optional_text(event.get("display_name"), label="display_name")
            if account is not None:
                user.account = account
            if display_name is not None:
                user.global_name = display_name

            raw_nicknames: object = event.get("nicknames", ())
            if not isinstance(raw_nicknames, list):
                raise ValueError("upsert_manual_user mutation nicknames must be a list")
            for raw_nickname in raw_nicknames:
                nickname = self._normalised_optional_text(raw_nickname, label="nickname")
                if nickname is not None:
                    self._assert_unique_general_alias(raw_user_id, nickname)
                    user.nicknames.add(nickname)

            raw_game_aliases: object = event.get("game_aliases", {})
            if not isinstance(raw_game_aliases, dict):
                raise ValueError("upsert_manual_user mutation game_aliases must be an object")
            for scope, alias in self._normalised_alias_mapping(raw_game_aliases).items():
                if alias is None:
                    user.games.pop(scope, None)
                else:
                    self._assert_unique_game_alias(raw_user_id, scope, alias)
                    user.games[scope] = (alias, user.games.get(scope, (None, None))[1])
            self._sync_known_names(user)

        changed: bool = user.model_dump(mode="json") != before
        if changed:
            self._rebuild_aliases()
            self._dump()
        return changed

    def add_name(self, user_id: int, name: str, is_name: bool = True):
        if is_name:
            raise ValueError("Known names are derived from Discord identity and cannot be added directly.")
        alias: str | None = self._normalised_optional_text(name, label="general alias")
        if alias is None:
            raise ValueError("general alias must not be empty")
        self._assert_unique_general_alias(user_id, alias)
        user: UserNames = self.by_id.setdefault(user_id, UserNames())
        if alias not in user.nicknames:
            user.nicknames.add(alias)
            self._rebuild_aliases()
            self._dump()
            self._queue_remote_mutation(NameMutationKind.ADD_NAME, user_id=user_id, name=alias, is_name=False)

    def set_names(self, user: hikari.User | hikari.Member) -> None:
        if not user:
            return  # pyright: ignore[reportUnreachable]
        user_id: int = int(user.id)
        userName: UserNames = self.by_id.setdefault(user_id, UserNames())
        if not self._apply_discord_identity(userName, user):
            return

        guild_id: Snowflake | None = user.guild_id if isinstance(user, hikari.Member) else None
        self._persist_identity_change(user_id, userName, guild_id=guild_id)

    def remove_game_alias(self, user_id: int, scope: str) -> None:
        user: UserNames | None = self.by_id.get(user_id)
        if not user:
            return
        user.games.pop(scope.lower(), None)
        self._dump()
        self._queue_remote_mutation(NameMutationKind.REMOVE_GAME_ALIAS, user_id=user_id, scope=scope)

    def remove_name(self, user_id: int, name: str) -> None:
        user: UserNames | None = self.by_id.get(user_id)
        if not user:
            return
        user.nicknames.discard(name)
        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(NameMutationKind.REMOVE_NAME, user_id=user_id, name=name)

    def set_game_alias(self, user_id: int, scope: str, alias: str) -> None:
        normalised_scope: str = self._normalised_alias_scope(scope)
        normalised_alias: str | None = self._normalised_optional_text(alias, label=f"{normalised_scope} alias")
        if normalised_alias is None:
            raise ValueError("game alias must not be empty")
        self._assert_unique_game_alias(user_id, normalised_scope, normalised_alias)
        user: UserNames = self.by_id.setdefault(user_id, UserNames())
        user.games[normalised_scope] = (normalised_alias, user.games.get(normalised_scope, (None, None))[1])
        self._dump()
        self._queue_remote_mutation(
            NameMutationKind.SET_GAME_ALIAS,
            user_id=user_id,
            scope=normalised_scope,
            alias=normalised_alias,
        )

    @staticmethod
    def _norm_platform_key(platform: object | None) -> str:
        value: str = str(platform).strip().lower() if platform is not None else ""
        if not value:
            raise ValueError("platform can't be empty")
        return value

    @staticmethod
    def _norm_platform_id(platform_id: object | None) -> str | None:
        if platform_id is None:
            return None
        value: str = str(platform_id).strip()
        if not value:
            return None
        return value

    @staticmethod
    def _norm_steam_id(steam_id: object | None) -> str | None:
        if steam_id is None:
            return None
        value: str = str(steam_id).strip()
        if not value:
            return None
        if not value.isdigit():
            raise ValueError("steam_id must be numeric")
        return value

    def set_platform_id(self, user_id: int, platform: object, platform_id: object | None) -> bool:
        platform_key = self._norm_platform_key(platform)
        if platform_key == "steam":
            value = self._norm_steam_id(platform_id)
            current = self._norm_steam_id(self.by_id.setdefault(user_id, UserNames()).platform_ids.get(platform_key))
        else:
            value = self._norm_platform_id(platform_id)
            current = self._norm_platform_id(self.by_id.setdefault(user_id, UserNames()).platform_ids.get(platform_key))
        user: UserNames = self.by_id.setdefault(user_id, UserNames())
        if current == value:
            return False

        if value is None:
            user.platform_ids.pop(platform_key, None)
        else:
            self._assert_unique_platform_id(user_id, platform_key, value)
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

        user: UserNames | None = self.by_id.get(user_id)
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
            platform_key: str = self._norm_platform_key(platform)
        except ValueError:
            return None
        resolved_platform_id: str | None
        if platform_key == "steam":
            try:
                resolved_platform_id = self._norm_steam_id(platform_id)
            except ValueError:
                return None
        else:
            resolved_platform_id = self._norm_platform_id(platform_id)
        if not resolved_platform_id:
            return None
        return self.by_platform_id.get(platform_key, {}).get(resolved_platform_id)

    def list_platform_ids(self, user_id: int) -> dict[str, str]:
        user: UserNames | None = self.by_id.get(user_id)
        if not user:
            return {}

        out: dict[str, str] = {}
        for platform, raw_id in user.platform_ids.items():
            try:
                platform_key: str = self._norm_platform_key(platform)
            except ValueError:
                continue
            resolved_platform_id: str | None
            if platform_key == "steam":
                try:
                    resolved_platform_id = self._norm_steam_id(raw_id)
                except ValueError:
                    continue
            else:
                resolved_platform_id = self._norm_platform_id(raw_id)
            if resolved_platform_id:
                out[platform_key] = resolved_platform_id

        return dict[str, str](sorted(out.items()))

    def set_game_uuid(self, user_id: int, scope: str, uuid: object | None) -> bool:
        scope_key = self._normalised_alias_scope(scope)
        value = self._normalised_game_uuid(scope_key, uuid)
        existing: tuple[str | None, str | None] = self.by_id.get(user_id, UserNames()).games.get(
            scope_key, (None, None)
        )
        if existing[1] == value:
            return False
        if value is not None:
            self._assert_unique_game_uuid(user_id, scope_key, value)
        user: UserNames = self.by_id.setdefault(user_id, UserNames())
        name, _ = user.games.get(scope_key, (None, None))
        if name is None and value is None:
            user.games.pop(scope_key, None)
        else:
            user.games[scope_key] = (name, value)
        self._dump()
        self._queue_remote_mutation(NameMutationKind.SET_GAME_UUID, user_id=user_id, scope=scope_key, uuid=value)
        return True

    def set_game_profile(self, user_id: int, scope: object, alias: object, uuid: object | None = None) -> bool:
        scope_key = self._normalised_alias_scope(scope)
        alias_value = self._normalised_optional_text(alias, label=f"{scope_key} alias")
        if alias_value is None:
            raise ValueError("game alias must not be empty")
        uuid_value = self._normalised_game_uuid(scope_key, uuid)
        self._assert_unique_game_alias(user_id, scope_key, alias_value)
        if uuid_value is not None:
            self._assert_unique_game_uuid(user_id, scope_key, uuid_value)
        user: UserNames = self.by_id.setdefault(user_id, UserNames())
        current_value = user.games.get(scope_key)
        next_value = (alias_value, uuid_value)
        if current_value == next_value:
            return False
        user.games[scope_key] = next_value
        self._dump()
        self._queue_remote_mutation(
            NameMutationKind.SET_GAME_ALIAS,
            user_id=user_id,
            scope=scope_key,
            alias=alias_value,
        )
        self._queue_remote_mutation(
            NameMutationKind.SET_GAME_UUID,
            user_id=user_id,
            scope=scope_key,
            uuid=uuid_value,
        )
        return True

    def get_game_alias(self, user_id: int, scope: str) -> str | None:
        user: UserNames | None = self.by_id.get(user_id)
        if not user:
            return None
        alias_data: tuple[str | None, str | None] | None = user.games.get(scope.lower())
        return alias_data[0] if alias_data else None

    def get_game_uuid(self, user_id: int, scope: str) -> str | None:
        user: UserNames | None = self.by_id.get(user_id)
        if not user:
            return None
        alias_data: tuple[str | None, str | None] | None = user.games.get(scope.lower())
        return alias_data[1] if alias_data else None

    def resolve_game_alias_to_id(self, alias: str, scope: str) -> int | None:
        scope_key = self._normalised_alias_scope(scope)
        alias_value = self._normalised_optional_text(alias, label=f"{scope_key} alias")
        if alias_value is None:
            return None
        result = self._resolve_candidate_result(
            self._resolve_game_alias_ids(alias_value, scope_key),
            alias_value,
            prefer_global_name=False,
        )
        return result.user_id

    def relay_mention_name(
        self,
        user_id: int,
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
        default: str = "user",
    ) -> str:
        del preferred_guild_id
        persona = self.persona(user_id)
        if persona is None:
            return default
        resolved = self._resolve_surface_name(
            persona,
            surface=PersonaSurface.APP,
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
            default=default,
        )
        return resolved if resolved is not None else default

    @overload
    def relay_display_name(
        self,
        user_id: int,
        default: None,
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
    ) -> str | None: ...

    @overload
    def relay_display_name(
        self,
        user_id: int,
        default: str = "Unknown",
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
    ) -> str: ...

    def relay_display_name(
        self,
        user_id: int,
        default: str | None = "Unknown",
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
    ) -> str | None:
        del preferred_guild_id
        persona = self.persona(user_id)
        if persona is None:
            return default
        return self._resolve_surface_name(
            persona,
            surface=PersonaSurface.APP,
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
            default=default,
        )

    @overload
    def web_display_name(
        self,
        user_id: int,
        default: None,
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
    ) -> str | None: ...

    @overload
    def web_display_name(
        self,
        user_id: int,
        default: str = "Unknown",
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
    ) -> str: ...

    def web_display_name(
        self,
        user_id: int,
        default: str | None = "Unknown",
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
    ) -> str | None:
        persona = self.persona(user_id)
        if persona is None:
            return default
        return self._resolve_surface_name(
            persona,
            surface=PersonaSurface.WEB,
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
            default=default,
        )

    def discord_avatar_hash(self, user_id: int) -> str | None:
        user = self.by_id.get(user_id)
        return user.avatar_hash if user is not None else None

    def web_mention_name(
        self,
        user_id: int,
        /,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        default: str = "user",
    ) -> str:
        persona = self.persona(user_id)
        if persona is None:
            return default
        resolved = self._resolve_surface_name(
            persona,
            surface=PersonaSurface.WEB,
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
            default=default,
        )
        return resolved if resolved is not None else default

    @overload
    def discord_display_name(
        self,
        user_id: int,
        default: None,
        /,
        *,
        fallback_display_name: str | None = None,
    ) -> str | None: ...

    @overload
    def discord_display_name(
        self,
        user_id: int,
        default: str = "user",
        /,
        *,
        fallback_display_name: str | None = None,
    ) -> str: ...

    def discord_display_name(
        self,
        user_id: int,
        default: str | None = "user",
        /,
        *,
        fallback_display_name: str | None = None,
    ) -> str | None:
        persona = self.persona(user_id)
        if persona is None:
            return fallback_display_name or default
        return self._resolve_surface_name(
            persona,
            surface=PersonaSurface.DISCORD,
            default=default,
            fallback_display_name=fallback_display_name,
        )

    @overload
    def discord_fallback_name(
        self,
        user_id: int,
        default: None,
        /,
        *,
        scope: str | None = None,
        fallback_display_name: str | None = None,
    ) -> str | None: ...

    @overload
    def discord_fallback_name(
        self,
        user_id: int,
        default: str = "user",
        /,
        *,
        scope: str | None = None,
        fallback_display_name: str | None = None,
    ) -> str: ...

    def discord_fallback_name(
        self,
        user_id: int,
        default: str | None = "user",
        /,
        *,
        scope: str | None = None,
        fallback_display_name: str | None = None,
    ) -> str | None:
        del scope
        fallback_name = self._normalised_optional_text(
            fallback_display_name,
            label="discord fallback display name",
        )
        return self.discord_display_name(
            user_id,
            default,
            fallback_display_name=fallback_name,
        )

    @staticmethod
    def _sorted_name_values(values: set[str]) -> list[str]:
        return sorted(values, key=str.casefold)

    @classmethod
    def _normalised_scope_candidates(
        cls,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
    ) -> tuple[str, ...]:
        ordered: list[str] = []

        def add(raw_value: object | None) -> None:
            if raw_value is None:
                return
            value = str(raw_value).strip().lower()
            if value and value not in ordered:
                ordered.append(value)

        add(scope)
        add(preferred_platform)
        for platform in platforms:
            add(platform)
        return tuple(ordered)

    @staticmethod
    def discord_identity_label(global_name: str | None, username: str | None) -> str | None:
        if global_name and username and global_name.casefold() != username.casefold():
            return f"{global_name} [{username}]"
        return global_name or username

    @classmethod
    def _persona_scope_alias(
        cls,
        persona: Persona,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
    ) -> str | None:
        for candidate in cls._normalised_scope_candidates(
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
        ):
            alias = persona.scopes.get(candidate)
            if alias:
                return alias
        return None

    @classmethod
    def _resolve_surface_name(
        cls,
        persona: Persona,
        *,
        surface: PersonaSurface,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        default: str | None = None,
        fallback_display_name: str | None = None,
    ) -> str | None:
        scope_alias = cls._persona_scope_alias(
            persona,
            scope=scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
        )
        discord_identity = cls.discord_identity_label(persona.discord_global, persona.discord_username)
        manual_alias = persona.aliases[0] if persona.aliases else None

        if surface is PersonaSurface.APP:
            return (
                scope_alias
                or persona.web_override
                or persona.discord_global
                or persona.discord_username
                or manual_alias
                or default
            )

        if surface is PersonaSurface.WEB:
            return (
                persona.web_override
                or scope_alias
                or discord_identity
                or manual_alias
                or fallback_display_name
                or default
            )

        if surface is PersonaSurface.DISCORD:
            return discord_identity or persona.web_override or manual_alias or fallback_display_name or default

        raise ValueError(f"Unsupported persona surface `{surface}`.")

    @staticmethod
    def _preferred_guild_display_name(
        entry: UserNames,
        preferred_guild_id: hikari.Snowflakeish | None = None,
    ) -> str | None:
        if preferred_guild_id is not None:
            preferred_name: str | None = entry.guild_names.get(int(preferred_guild_id))
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
        category: DisplayNameCategory | None = None,
    ) -> str | None: ...

    @overload
    def cached_display_name(
        self,
        user_id: int,
        default: str = "Unknown",
        /,
        *,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
        category: DisplayNameCategory | None = None,
    ) -> str: ...

    def cached_display_name(
        self,
        user_id: int,
        default: str | None = "Unknown",
        /,
        *,
        preferred_guild_id: hikari.Snowflakeish | None = DISCORD_GUILD,
        category: DisplayNameCategory | None = None,
    ) -> str | None:
        user: UserNames | None = self.by_id.get(user_id)
        if user is None:
            return default

        if category is not None and (override := user.display_overrides.get_for_category(category)):
            return override
        del preferred_guild_id
        persona = self._persona_from_entry(user_id, user)
        if category is DisplayNameCategory.WEB:
            return self._resolve_surface_name(persona, surface=PersonaSurface.WEB, default=default)
        return self._resolve_surface_name(persona, surface=PersonaSurface.DISCORD, default=default)

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
            return NameResolutionResult(
                NameResolutionStatus.AMBIGUOUS, candidate_ids=tuple[int, ...](sorted(candidate_ids))
            )

        matching_global_names: set[int] = {
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
        return NameResolutionResult(
            NameResolutionStatus.AMBIGUOUS, candidate_ids=tuple[int, ...](sorted(candidate_ids))
        )

    def resolve_name(
        self,
        name: str,
        scope: str | None = None,
        *,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        prefer_global_name: bool = False,
    ) -> NameResolutionResult:
        if scope:
            candidate_ids: set[int] = self._resolve_game_alias_ids(name, scope)
            result: NameResolutionResult = self._resolve_candidate_result(
                candidate_ids, name, prefer_global_name=prefer_global_name
            )
            if result.status is not NameResolutionStatus.NOT_FOUND:
                return result
        for platform_scope in self._normalised_scope_candidates(
            platforms=platforms,
            preferred_platform=preferred_platform,
        ):
            candidate_ids = self._resolve_game_alias_ids(name, platform_scope)
            result = self._resolve_candidate_result(
                candidate_ids,
                name,
                prefer_global_name=prefer_global_name,
            )
            if result.status is not NameResolutionStatus.NOT_FOUND:
                return result
        alias_result = self._resolve_candidate_result(
            self.by_alias.get(name.lower(), set()),
            name,
            prefer_global_name=prefer_global_name,
        )
        if alias_result.status is not NameResolutionStatus.NOT_FOUND:
            return alias_result
        if name.isnumeric():
            if (ident := int(name)) in self.by_id:
                return NameResolutionResult(NameResolutionStatus.UNIQUE, ident)
        return alias_result

    def resolve_to_id(
        self,
        name: str,
        scope: str | None = None,
        *,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
        prefer_global_name: bool = False,
    ) -> int | None:
        return self.resolve_name(
            name,
            scope,
            platforms=platforms,
            preferred_platform=preferred_platform,
            prefer_global_name=prefer_global_name,
        ).user_id

    def _resolve_game_alias_ids(self, alias: str, scope: str | None) -> set[int]:
        matching_ids: set[int] = set()
        alias_key: str = alias.lower()
        scope_key: str | None = scope.lower() if scope else None
        for uid, entry in self.by_id.items():
            if scope_key is None:
                if any(alias_key in (name.lower() for name in data if name) for data in entry.games.values()):
                    matching_ids.add(uid)
            else:
                data: tuple[str | None, str | None] | None = entry.games.get(scope_key)
                if data and alias_key in (name.lower() for name in data if name):
                    matching_ids.add(uid)
        return matching_ids

    def _resolve_game_uuid_ids(self, game_uuid: str, scope: str | None) -> set[int]:
        matching_ids: set[int] = set()
        uuid_key = game_uuid.casefold()
        scope_key: str | None = scope.lower() if scope else None
        for uid, entry in self.by_id.items():
            if scope_key is None:
                if any(uuid_key == stored_uuid.casefold() for _, stored_uuid in entry.games.values() if stored_uuid):
                    matching_ids.add(uid)
            else:
                data: tuple[str | None, str | None] | None = entry.games.get(scope_key)
                if data is not None and data[1] is not None and data[1].casefold() == uuid_key:
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

    def clean(self, user_id: int, current_names: list[str]) -> None:
        user: UserNames | None = self.by_id.get(user_id)
        if not user:
            return
        allowed_names: set[str] = set[str](current_names)
        user.global_name = user.global_name if user.global_name in allowed_names else None
        user.guild_names = {guild_id: name for guild_id, name in user.guild_names.items() if name in allowed_names}
        self._sync_known_names(user)
        self._rebuild_aliases()
        self._dump()
        self._queue_remote_mutation(NameMutationKind.CLEAN_NAMES, user_id=user_id, names=sorted(user.names))

    def _rebuild_aliases(self) -> None:
        self.by_alias.clear()
        self.by_platform_id.clear()
        for uid, entry in self.by_id.items():
            self._sync_known_names(entry)
            for name in entry.names | entry.nicknames:
                self.by_alias.setdefault(name.lower(), set()).add(uid)
            for override in (entry.display_overrides.discord, entry.display_overrides.web):
                if override:
                    self.by_alias.setdefault(override.lower(), set()).add(uid)
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

    def parse_mentions(
        self,
        text: str,
        replace: bool = True,
        *,
        scope: str | None = None,
        platforms: Iterable[object] = (),
        preferred_platform: object | None = None,
    ) -> tuple[str, set[int]]:
        """
        Parse @name mentions in the input text.

        Returns:
            - Modified string (if replace=True), original string otherwise
            - Set of resolved user IDs
        """
        mentions: set[int] = set[int]()

        def repl(match) -> str | Any:
            name = match.group(1)
            uid: int | None = self.resolve_to_id(
                name,
                scope=scope,
                platforms=platforms,
                preferred_platform=preferred_platform,
            )
            if uid:
                mentions.add(uid)
                return f"<@{uid}>" if replace else match.group(0)
            return match.group(0)

        updated: str = re.sub(r"@([\w#-]+)", repl, text)
        return updated, mentions


AC_XCP = LookupError("Invalid input. Please use the autocomplete to select")
"convience var for xcp to raise when using autocomplete options"


class Activity_Provider(Protocol):
    silent: bool = SILENT_DEBUG
    """Whether to log"""
    prio: int = 50
    "0 = RAM | 2 = CPU | 4 = Player | 6 = Process | 10-79 = whatever | 80 >= Alerts"
    activity_field: DiscordActivityField | None = None
    activity_scope_name: str | None = None

    async def get(self) -> str | None:
        return None


class Activity_Manager(Protocol):
    providers: dict[type[Activity_Provider], Activity_Provider]
    """Whether to log"""
    last_update: datetime | None
    state: str | None
    activity_settings: DiscordActivitySettings

    def register(self, provider: Activity_Provider) -> None:
        return

    def deregister(self, provider: Activity_Provider) -> None:
        return

    def set_activity_settings(self, settings: DiscordActivitySettings) -> None:
        return

    def set_rotation_target_name_provider(self, provider: Callable[[], str | None] | None) -> None:
        return

    def current_rotation_target_name(self) -> str | None:
        return None

    async def refresh(self) -> None:
        return

    def current_rotation_slot(self, app_count: int) -> tuple[int, bool]:
        return (0, False)


IS_RESTARTING = False
IS_SHUTTINGDOWN = False

# AiviA APasz
