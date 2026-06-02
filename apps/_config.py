from __future__ import annotations

import enum
import logging
import re
from datetime import datetime
from pathlib import Path

import hikari
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import config
from _security import Access_Control, Power_Level
from _resolator import Resolutator

log = logging.getLogger(__name__)


def resolve_config_path(raw: str | Path | None, *, directory: Path | str | None) -> Path | None:
    if raw is None:
        return None
    if isinstance(raw, Path):
        return raw
    if not raw.strip():
        return None

    resolved = Resolutator.path_tokens(raw, {"WD": directory or ""})
    return Path(resolved)


def normalise_optional_channel_id(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return str(hikari.Snowflake(text))


def normalise_optional_channel_ids(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str | int | hikari.Snowflake):
        channel_id = normalise_optional_channel_id(raw)
        return (channel_id,) if channel_id is not None else ()
    if not isinstance(raw, list | tuple | set | frozenset):
        raise TypeError("chat channels must be a Discord snowflake or a sequence of Discord snowflakes")

    channel_ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        channel_id = normalise_optional_channel_id(item)
        if channel_id is None or channel_id in seen:
            continue
        channel_ids.append(channel_id)
        seen.add(channel_id)
    return tuple(channel_ids)


def normalise_optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def normalise_optional_power_level(raw: object) -> Power_Level | None:
    if raw is None:
        return None
    if isinstance(raw, Power_Level):
        return raw
    if isinstance(raw, bool):
        raise TypeError("power level must not be a boolean")
    if isinstance(raw, int | str):
        parsed = Access_Control.parse_level(raw)
        if parsed is None:
            raise ValueError(f"invalid power level {raw!r}")
        return parsed
    raise TypeError("power level must be a string or integer")


class RelayChannelSource(enum.StrEnum):
    NONE = "none"
    DEFAULT = "default"
    INSTANCE = "instance"


class ModDownloadBlockReason(enum.StrEnum):
    SERVER_ONLY = "server_only"
    BUILTIN = "builtin"
    ARTIFACT = "artifact"
    OTHER = "other"

    @property
    def label(self) -> str:
        match self:
            case ModDownloadBlockReason.SERVER_ONLY:
                return "Server only"
            case ModDownloadBlockReason.BUILTIN:
                return "Built-in"
            case ModDownloadBlockReason.ARTIFACT:
                return "Artifact"
            case ModDownloadBlockReason.OTHER:
                return "Not downloadable"


class ModType(enum.StrEnum):
    REGULAR = "regular"
    COREMOD = "coremod"
    BUILTIN = "builtin"
    SERVER_ONLY = "server_only"
    CLIENT = "client"

    @property
    def label(self) -> str:
        match self:
            case ModType.REGULAR:
                return "Regular"
            case ModType.COREMOD:
                return "Coremod"
            case ModType.BUILTIN:
                return "Built-in"
            case ModType.SERVER_ONLY:
                return "Server only"
            case ModType.CLIENT:
                return "Client"


_VERSION_LOADER_RE = re.compile(r"[a-z0-9_]+")


def normalise_version_loader(raw: object) -> str | None:
    text = normalise_optional_text(raw)
    if text is None:
        return None
    normalised = text.casefold().replace("-", "_").replace(" ", "_")
    if _VERSION_LOADER_RE.fullmatch(normalised) is None:
        raise ValueError(f"invalid version loader {text!r}")
    return normalised


class AppVersion(BaseModel):
    main: str
    framework: str | None = None
    loader: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @property
    def display_value(self) -> str:
        if self.loader is not None and self.framework is not None:
            return f"{self.main} [{self.loader} {self.framework}]"
        if self.loader is not None:
            return f"{self.main} [{self.loader}]"
        if self.framework is not None:
            return f"{self.main} [{self.framework}]"
        return self.main

    @field_validator("main", "framework", mode="before")
    @classmethod
    def validate_text_fields(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @field_validator("loader", mode="before")
    @classmethod
    def validate_loader(cls, raw: object) -> str | None:
        return normalise_version_loader(raw)


def normalise_app_version(raw: object) -> AppVersion | None:
    if raw is None:
        return None
    if isinstance(raw, AppVersion):
        return raw
    if isinstance(raw, str):
        main = normalise_optional_text(raw)
        if main is None:
            return None
        return AppVersion(main=main)
    if isinstance(raw, dict):
        return AppVersion.model_validate(raw)
    raise TypeError("version must be a string or version object")


class App_Config(BaseModel):
    name: str
    instance_key: str
    friendly_name: str | None = None
    directory: Path
    apps_dir: Path
    mods_dir: Path | None = None
    settings_pointer: Path | None = None
    server_log_file: Path | None = None
    join_host: str = config.PUBLIC_ADDR
    join_port: int | None = None
    api_host: str | None = None
    api_port: int | None = None
    scope: str
    chat_channel: str | None = None
    chat_channels: tuple[str, ...] = Field(default_factory=tuple)
    chat_channel_override: str | None = None
    chat_channel_overrides: tuple[str, ...] = Field(default_factory=tuple)
    chat_channel_source: RelayChannelSource = RelayChannelSource.NONE
    chat_ignore_symbol: str = config.CHAT_IGNORE
    enabled: bool = True
    cmd_start: list[str] = Field(default_factory=list)
    provider_alt_text: str | None = None
    version: AppVersion | None = None
    config_file_read_level_override: Power_Level | None = None
    config_file_write_level_override: Power_Level | None = None
    save_file_write_level_override: Power_Level | None = None

    @property
    def enabled_txt(self) -> str:
        return "Enabled" if self.enabled else "Disabled"

    @property
    def join_address(self) -> str | None:
        host = self.join_host.strip()
        if not host:
            return None
        return _format_host_port(host=host, port=self.join_port)

    @property
    def join_direct_ip_address(self) -> str | None:
        host = self.join_host.strip()
        public_addr = config.PUBLIC_ADDR.strip()
        public_ip = config.PUBLIC_IP.strip()
        if not host or not public_ip:
            return None
        if host.casefold() != public_addr.casefold():
            return None
        if host.casefold() == public_ip.casefold():
            return None
        return _format_host_port(host=public_ip, port=self.join_port)

    @property
    def join_display_address(self) -> str | None:
        address = self.join_address
        if address is None:
            return None
        direct_ip_address = self.join_direct_ip_address
        if direct_ip_address is None:
            return address
        return f"{address} [{direct_ip_address}]"

    @property
    def effective_api_host(self) -> str | None:
        api_host = self.api_host
        if api_host is not None and api_host.strip():
            return api_host.strip()
        join_host = self.join_host.strip()
        return join_host or None

    @property
    def effective_api_port(self) -> int | None:
        return self.api_port if self.api_port is not None else self.join_port

    model_config = ConfigDict(arbitrary_types_allowed=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_endpoint_fields(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        if "join_port" not in payload and "port" in payload:
            payload["join_port"] = payload["port"]
        return payload

    @field_validator("directory", "mods_dir", "settings_pointer", "server_log_file", mode="before")
    def resolve_dir(cls, raw: str | Path | None, info):
        return resolve_config_path(raw, directory=info.data.get("directory", ""))

    @field_validator("join_host", mode="before")
    def validate_join_host(cls, raw: object) -> str:
        if raw is None:
            return config.PUBLIC_ADDR
        text = str(raw).strip()
        return text or config.PUBLIC_ADDR

    @field_validator("api_host", mode="before")
    def validate_api_host(cls, raw: object) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    @field_validator("chat_channel", "chat_channel_override", mode="before")
    def validate_chat_channel(cls, raw: object) -> str | None:
        return normalise_optional_channel_id(raw)

    @field_validator("chat_channels", "chat_channel_overrides", mode="before")
    def validate_chat_channels(cls, raw: object) -> tuple[str, ...]:
        return normalise_optional_channel_ids(raw)

    @field_validator("version", mode="before")
    def validate_version(cls, raw: object) -> AppVersion | None:
        return normalise_app_version(raw)

    @field_validator(
        "config_file_read_level_override",
        "config_file_write_level_override",
        "save_file_write_level_override",
        mode="before",
    )
    def validate_power_level_override(cls, raw: object) -> Power_Level | None:
        return normalise_optional_power_level(raw)

    @field_validator("join_port", "api_port", mode="before")
    def validate_port(cls, raw: object) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise TypeError("port must be an integer")
        if isinstance(raw, int):
            port = raw
        elif isinstance(raw, str):
            value = raw.strip()
            if not value:
                return None
            if not value.isdecimal():
                raise TypeError("port must be an integer")
            port = int(value)
        else:
            raise TypeError("port must be an integer")
        if port <= 0 or port > 65535:
            raise ValueError("port must be between 1 and 65535")
        return port


def _format_host_port(*, host: str, port: int | None) -> str:
    if port is None:
        return host
    if ":" not in host:
        return f"{host}:{port}"
    if host.startswith("[") and host.endswith("]"):
        return f"{host}:{port}"
    return f"[{host}]:{port}"


class Mod_Config(BaseModel):
    name: str
    directory: Path
    added: datetime = Field(default_factory=datetime.now)
    enabled: bool = True
    version: str | None = None
    origin: str = "manual"
    mod_type: ModType = ModType.REGULAR
    download_block_reason: ModDownloadBlockReason | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_mod_fields(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        if "mod_type" not in payload:
            legacy_coremod = payload.get("coremod")
            block_reason = payload.get("download_block_reason")
            if legacy_coremod is True:
                payload["mod_type"] = ModType.COREMOD
            elif block_reason in (ModDownloadBlockReason.BUILTIN, ModDownloadBlockReason.BUILTIN.value):
                payload["mod_type"] = ModType.BUILTIN
            else:
                payload["mod_type"] = ModType.REGULAR
        return payload

    @property
    def coremod(self) -> bool:
        return self.mod_type is ModType.COREMOD


# AiviA APasz
