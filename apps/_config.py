from __future__ import annotations

import enum
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import hikari
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import config
from _resolator import Resolutator
from _security import Access_Control, Power_Level

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


APP_FRIENDLY_NAME_MAX_LENGTH = 80


def normalise_optional_friendly_name(raw: object) -> str | None:
    text = normalise_optional_text(raw)
    if text is None:
        return None
    if len(text) > APP_FRIENDLY_NAME_MAX_LENGTH:
        raise ValueError(f"Friendly name must be {APP_FRIENDLY_NAME_MAX_LENGTH} characters or fewer.")
    return text


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


class AppTitleFont(enum.StrEnum):
    AUTO = "auto"
    DEFAULT = "default"
    ARIAL = "arial"
    ARIAL_NARROW = "arial_narrow"
    AVENIR_NEXT = "avenir_next"
    BAHNSCHRIFT = "bahnschrift"
    CALIBRI = "calibri"
    CONSOLAS = "consolas"
    COURIER_NEW = "courier_new"
    FIRA_SANS = "fira_sans"
    FRANKLIN_GOTHIC_MEDIUM = "franklin_gothic_medium"
    GEORGIA = "georgia"
    GILL_SANS = "gill_sans"
    HELVETICA_NEUE = "helvetica_neue"
    IMPACT = "impact"
    INTER = "inter"
    JETBRAINS_MONO = "jetbrains_mono"
    LATO = "lato"
    LUCIDA_SANS = "lucida_sans"
    MERRIWEATHER = "merriweather"
    MINECRAFT_TEN = "minecraft_ten"
    OPEN_SANS = "open_sans"
    PALATINO_LINOTYPE = "palatino_linotype"
    PLAYFAIR_DISPLAY = "playfair_display"
    POPPINS = "poppins"
    TITILLIUM_WEB = "titillium_web"
    RAJDHANI = "rajdhani"
    ROBOTO = "roboto"
    SEGOE_UI = "segoe_ui"
    SOURCE_SANS_3 = "source_sans_3"
    TAHOMA = "tahoma"
    TREBUCHET_MS = "trebuchet_ms"
    VERDANA = "verdana"
    BEBAS_NEUE = "bebas_neue"
    MONTSERRAT = "montserrat"
    OSWALD = "oswald"

    @property
    def label(self) -> str:
        return _APP_TITLE_FONT_DEFINITIONS[self].label

    @property
    def css_font_family(self) -> str | None:
        return _APP_TITLE_FONT_DEFINITIONS[self].css_font_family

    def resolved(self, *, scope: str | None) -> "AppTitleFont":
        if self is not AppTitleFont.AUTO:
            return self
        scope_key = (scope or "").strip().casefold()
        return _APP_TITLE_FONT_AUTO_BY_SCOPE.get(scope_key, AppTitleFont.DEFAULT)


@dataclass(frozen=True, slots=True)
class _AppTitleFontDefinition:
    label: str
    css_font_family: str | None


@dataclass(frozen=True, slots=True)
class ResolvedAppTitleFont:
    value: str
    label: str
    css_font_family: str | None
    is_builtin: bool


_APP_TITLE_FONT_DEFINITIONS: dict[AppTitleFont, _AppTitleFontDefinition] = {
    AppTitleFont.AUTO: _AppTitleFontDefinition(label="Auto (by game)", css_font_family=None),
    AppTitleFont.DEFAULT: _AppTitleFontDefinition(label="Default", css_font_family=None),
    AppTitleFont.ARIAL: _AppTitleFontDefinition(label="Arial", css_font_family='"Arial", "Helvetica Neue", Helvetica, sans-serif'),
    AppTitleFont.ARIAL_NARROW: _AppTitleFontDefinition(
        label="Arial Narrow",
        css_font_family='"Arial Narrow", "Bahnschrift SemiCondensed", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.AVENIR_NEXT: _AppTitleFontDefinition(
        label="Avenir Next",
        css_font_family='"Avenir Next", Avenir, "Helvetica Neue", Helvetica, Arial, sans-serif',
    ),
    AppTitleFont.BAHNSCHRIFT: _AppTitleFontDefinition(
        label="Bahnschrift",
        css_font_family='"Bahnschrift", "Arial Narrow", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.BEBAS_NEUE: _AppTitleFontDefinition(
        label="Bebas Neue",
        css_font_family='"Bebas Neue", Impact, "Arial Narrow Bold", sans-serif',
    ),
    AppTitleFont.CALIBRI: _AppTitleFontDefinition(
        label="Calibri",
        css_font_family='Calibri, "Segoe UI", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.CONSOLAS: _AppTitleFontDefinition(
        label="Consolas",
        css_font_family='Consolas, "Lucida Console", "Courier New", monospace',
    ),
    AppTitleFont.COURIER_NEW: _AppTitleFontDefinition(
        label="Courier New",
        css_font_family='"Courier New", Courier, monospace',
    ),
    AppTitleFont.FIRA_SANS: _AppTitleFontDefinition(
        label="Fira Sans",
        css_font_family='"Fira Sans", "Segoe UI", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.FRANKLIN_GOTHIC_MEDIUM: _AppTitleFontDefinition(
        label="Franklin Gothic Medium",
        css_font_family='"Franklin Gothic Medium", "Arial Narrow", Arial, sans-serif',
    ),
    AppTitleFont.GEORGIA: _AppTitleFontDefinition(
        label="Georgia",
        css_font_family='Georgia, "Times New Roman", Times, serif',
    ),
    AppTitleFont.GILL_SANS: _AppTitleFontDefinition(
        label="Gill Sans",
        css_font_family='"Gill Sans", "Gill Sans MT", Calibri, sans-serif',
    ),
    AppTitleFont.HELVETICA_NEUE: _AppTitleFontDefinition(
        label="Helvetica Neue",
        css_font_family='"Helvetica Neue", Helvetica, Arial, sans-serif',
    ),
    AppTitleFont.IMPACT: _AppTitleFontDefinition(
        label="Impact",
        css_font_family='Impact, Haettenschweiler, "Arial Narrow Bold", sans-serif',
    ),
    AppTitleFont.INTER: _AppTitleFontDefinition(
        label="Inter",
        css_font_family='Inter, "Segoe UI", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.JETBRAINS_MONO: _AppTitleFontDefinition(
        label="JetBrains Mono",
        css_font_family='"JetBrains Mono", "IBM Plex Mono", "Fira Code", monospace',
    ),
    AppTitleFont.LATO: _AppTitleFontDefinition(
        label="Lato",
        css_font_family='Lato, "Helvetica Neue", Helvetica, Arial, sans-serif',
    ),
    AppTitleFont.LUCIDA_SANS: _AppTitleFontDefinition(
        label="Lucida Sans",
        css_font_family='"Lucida Sans", "Lucida Sans Unicode", "Lucida Grande", sans-serif',
    ),
    AppTitleFont.MERRIWEATHER: _AppTitleFontDefinition(
        label="Merriweather",
        css_font_family='Merriweather, Georgia, "Times New Roman", serif',
    ),
    AppTitleFont.MINECRAFT_TEN: _AppTitleFontDefinition(
        label="Minecraft Ten",
        css_font_family='"Minecraft Ten", "Minecrafter", "Press Start 2P", "VT323", monospace',
    ),
    AppTitleFont.MONTSERRAT: _AppTitleFontDefinition(
        label="Montserrat",
        css_font_family='"Montserrat", "Avenir Next Condensed", "Arial Narrow", sans-serif',
    ),
    AppTitleFont.OPEN_SANS: _AppTitleFontDefinition(
        label="Open Sans",
        css_font_family='"Open Sans", "Segoe UI", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.OSWALD: _AppTitleFontDefinition(
        label="Oswald",
        css_font_family='"Oswald", "Arial Narrow", sans-serif',
    ),
    AppTitleFont.PALATINO_LINOTYPE: _AppTitleFontDefinition(
        label="Palatino Linotype",
        css_font_family='"Palatino Linotype", Palatino, "Book Antiqua", serif',
    ),
    AppTitleFont.PLAYFAIR_DISPLAY: _AppTitleFontDefinition(
        label="Playfair Display",
        css_font_family='"Playfair Display", Georgia, "Times New Roman", serif',
    ),
    AppTitleFont.POPPINS: _AppTitleFontDefinition(
        label="Poppins",
        css_font_family='Poppins, "Avenir Next", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.RAJDHANI: _AppTitleFontDefinition(
        label="Rajdhani",
        css_font_family='"Rajdhani", Eurostile, Bahnschrift, sans-serif',
    ),
    AppTitleFont.ROBOTO: _AppTitleFontDefinition(
        label="Roboto",
        css_font_family='Roboto, "Helvetica Neue", Helvetica, Arial, sans-serif',
    ),
    AppTitleFont.SEGOE_UI: _AppTitleFontDefinition(
        label="Segoe UI",
        css_font_family='"Segoe UI", Tahoma, Geneva, Verdana, sans-serif',
    ),
    AppTitleFont.SOURCE_SANS_3: _AppTitleFontDefinition(
        label="Source Sans 3",
        css_font_family='"Source Sans 3", "Segoe UI", "Helvetica Neue", Arial, sans-serif',
    ),
    AppTitleFont.TAHOMA: _AppTitleFontDefinition(
        label="Tahoma",
        css_font_family='Tahoma, Verdana, "Segoe UI", sans-serif',
    ),
    AppTitleFont.TITILLIUM_WEB: _AppTitleFontDefinition(
        label="Titillium Web",
        css_font_family='"Titillium Web", "Bahnschrift SemiCondensed", "Arial Narrow", sans-serif',
    ),
    AppTitleFont.TREBUCHET_MS: _AppTitleFontDefinition(
        label="Trebuchet MS",
        css_font_family='"Trebuchet MS", "Lucida Sans Unicode", "Lucida Grande", sans-serif',
    ),
    AppTitleFont.VERDANA: _AppTitleFontDefinition(
        label="Verdana",
        css_font_family='Verdana, Geneva, Tahoma, sans-serif',
    ),
}

_APP_TITLE_FONT_AUTO_BY_SCOPE: dict[str, AppTitleFont] = {
    "minecraft": AppTitleFont.MINECRAFT_TEN,
    "factorio": AppTitleFont.TITILLIUM_WEB,
    "satisfactory": AppTitleFont.RAJDHANI,
    "sevendays": AppTitleFont.BEBAS_NEUE,
    "beammp": AppTitleFont.MONTSERRAT,
    "ets": AppTitleFont.OSWALD,
}

_APP_TITLE_FONT_BY_VALUE: dict[str, AppTitleFont] = {font.value: font for font in AppTitleFont}


def normalise_app_title_font(raw: object) -> str:
    if isinstance(raw, AppTitleFont):
        return raw.value
    if not isinstance(raw, str):
        raise TypeError("title font must be a string")
    value = raw.strip()
    if not value:
        raise ValueError("title font must not be empty")
    return value


def resolve_app_title_font(*, value: str, scope: str | None) -> ResolvedAppTitleFont:
    normalised_value = normalise_app_title_font(value)
    builtin_font = _APP_TITLE_FONT_BY_VALUE.get(normalised_value)
    if builtin_font is not None:
        resolved_builtin = builtin_font.resolved(scope=scope)
        return ResolvedAppTitleFont(
            value=normalised_value,
            label=_APP_TITLE_FONT_DEFINITIONS[resolved_builtin].label if builtin_font is AppTitleFont.AUTO else builtin_font.label,
            css_font_family=resolved_builtin.css_font_family,
            is_builtin=True,
        )
    return ResolvedAppTitleFont(
        value=normalised_value,
        label=normalised_value,
        css_font_family=_css_font_family_literal(normalised_value),
        is_builtin=False,
    )


def app_title_font_options(*, custom_font_families: Iterable[str] = (), selected_value: str | None = None) -> dict[str, str]:
    options: dict[str, str] = {font.value: font.label for font in AppTitleFont}
    known_labels = {font.label.casefold() for font in AppTitleFont}
    extra_families: set[str] = set()
    for family_name in custom_font_families:
        if not isinstance(family_name, str):
            continue
        cleaned_name = family_name.strip()
        if not cleaned_name or cleaned_name.casefold() in known_labels:
            continue
        extra_families.add(cleaned_name)
    if selected_value is not None:
        cleaned_selected_value = normalise_app_title_font(selected_value)
        if cleaned_selected_value not in options:
            extra_families.add(cleaned_selected_value)
    for family_name in sorted(extra_families, key=str.casefold):
        options[family_name] = family_name
    return options


def app_title_font_default_label(*, scope: str | None) -> str:
    return resolve_app_title_font(value=AppTitleFont.AUTO.value, scope=scope).label


def _css_font_family_literal(family_name: str) -> str:
    escaped_family_name = family_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped_family_name}"'


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


class AppResourcePointProfile(BaseModel):
    running: config.ResourcePointSet = Field(default_factory=config.ResourcePointSet)
    startup: config.ResourcePointSet | None = None

    model_config = ConfigDict(extra="forbid")

    @property
    def startup_points(self) -> config.ResourcePointSet:
        return self.startup or self.running


class App_Config(BaseModel):
    name: str
    instance_key: str
    friendly_name: str | None = None
    title_font_preset: str = AppTitleFont.AUTO.value
    notes: str | None = None
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
    lifecycle_notice_started: bool = True
    lifecycle_notice_stopped: bool = True
    lifecycle_notice_crashed: bool = True
    cmd_start: list[str] = Field(default_factory=list)
    provider_alt_text: str | None = None
    version: AppVersion | None = None
    resource_points: AppResourcePointProfile = Field(default_factory=AppResourcePointProfile)
    config_file_read_level_override: Power_Level | None = None
    config_file_write_level_override: Power_Level | None = None
    save_file_write_level_override: Power_Level | None = None

    @field_validator("friendly_name", mode="before")
    @classmethod
    def validate_friendly_name(cls, raw: object) -> str | None:
        return normalise_optional_friendly_name(raw)

    @field_validator("notes", mode="before")
    @classmethod
    def validate_optional_text_fields(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @field_validator("title_font_preset", mode="before")
    @classmethod
    def validate_title_font_preset(cls, raw: object) -> str:
        return normalise_app_title_font(raw)

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
