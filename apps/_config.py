from __future__ import annotations

import enum
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from re import Pattern
from string import Formatter
from urllib.parse import urlsplit

import hikari
from modmux.models import Provider
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

import config
from _resolator import Resolutator
from _security import Access_Control, Power_Level

log = logging.getLogger(__name__)

_CLIENT_PACK_VERSION_RE: Pattern[str] = re.compile(r"(?:\d+|\d{4}-\d{2}-\d{2}(?:\.\d+)?)")
CLIENT_PACK_CHANGELOG_MAX_LENGTH = 4000
CLIENT_PACK_METADATA_NAME_MAX_LENGTH = 100
CLIENT_PACK_METADATA_DESCRIPTION_MAX_LENGTH = 1000
CLIENT_PACK_FILENAME_TEMPLATE_MAX_LENGTH = 200
CLIENT_PACK_FILENAME_STEM_MAX_LENGTH = 180
CLIENT_PACK_FILENAME_TEMPLATE_DEFAULT = "{pack_name}-{version}"
CLIENT_PACK_FILENAME_PLACEHOLDERS: tuple[str, ...] = (
    "app_name",
    "pack_name",
    "version",
    "minecraft_version",
    "format",
)


def next_client_pack_version(current_version: str | None, *, published_on: date | None = None) -> str:
    release_date = published_on or date.today()
    date_version = release_date.isoformat()
    if current_version == date_version:
        return f"{date_version}.2"
    prefix = f"{date_version}."
    if current_version is not None and current_version.startswith(prefix):
        sequence_text = current_version.removeprefix(prefix)
        if sequence_text.isdecimal():
            return f"{date_version}.{int(sequence_text) + 1}"
    return date_version


class ModDistributionMode(enum.StrEnum):
    NONE = "none"
    RAW_ENABLED = "raw_enabled"
    SIDE_AWARE_GENERIC_CLIENT_PACK = "side_aware_generic_client_pack"
    MINECRAFT_LAUNCHER_PACK = "minecraft_launcher_pack"
    SERVER_PUSH = "server_push"
    EXTERNAL_MANIFEST = "external_manifest"


@dataclass(frozen=True, slots=True)
class AppModCapabilities:
    mode: ModDistributionMode
    supports_raw_download: bool = False
    supports_client_only: bool = False
    supports_client_pack: bool = False
    supports_launcher_formats: bool = False
    include_client_overrides: bool = False
    launcher_metadata_providers: tuple[Provider, ...] = ()


_DEFAULT_MOD_CAPABILITIES = AppModCapabilities(mode=ModDistributionMode.NONE)
_MOD_CAPABILITIES_BY_SCOPE: dict[str, AppModCapabilities] = {
    "minecraft": AppModCapabilities(
        mode=ModDistributionMode.MINECRAFT_LAUNCHER_PACK,
        supports_raw_download=True,
        supports_client_only=True,
        supports_client_pack=True,
        supports_launcher_formats=True,
        include_client_overrides=True,
        launcher_metadata_providers=(Provider.MODRINTH, Provider.CURSEFORGE),
    ),
    "sevendays": AppModCapabilities(
        mode=ModDistributionMode.SIDE_AWARE_GENERIC_CLIENT_PACK,
        supports_raw_download=True,
        supports_client_only=True,
        supports_client_pack=True,
        include_client_overrides=True,
    ),
    "factorio": AppModCapabilities(
        mode=ModDistributionMode.RAW_ENABLED,
        supports_raw_download=True,
    ),
    "beammp": AppModCapabilities(
        mode=ModDistributionMode.SERVER_PUSH,
        supports_raw_download=True,
    ),
    "ets": _DEFAULT_MOD_CAPABILITIES,
    "satisfactory": _DEFAULT_MOD_CAPABILITIES,
}


def mod_capabilities_for_scope(scope: str | None) -> AppModCapabilities:
    if scope is None:
        return _DEFAULT_MOD_CAPABILITIES
    return _MOD_CAPABILITIES_BY_SCOPE.get(scope.strip().casefold(), _DEFAULT_MOD_CAPABILITIES)


def launcher_provider_label(provider: Provider) -> str:
    match provider:
        case Provider.MODRINTH:
            return "Modrinth"
        case Provider.CURSEFORGE:
            return "CurseForge"
        case _:
            return provider.value.title()


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


def normalise_client_pack_changelog(raw: object, *, required: bool = False) -> str | None:
    text = normalise_optional_text(raw)
    if required and text is None:
        raise ValueError("Client pack publication requires a changelog.")
    if text is not None and len(text) > CLIENT_PACK_CHANGELOG_MAX_LENGTH:
        raise ValueError(
            f"client pack changelog must be at most {CLIENT_PACK_CHANGELOG_MAX_LENGTH} characters"
        )
    return text


def normalise_client_pack_version(raw: object, *, required: bool = False) -> str | None:
    if raw is None or raw == 0:
        if required:
            raise ValueError("client pack release requires a version")
        return None
    if isinstance(raw, bool):
        raise ValueError("client pack version must be a date version")
    normalised = str(raw).strip()
    if not normalised:
        if required:
            raise ValueError("client pack release requires a version")
        return None
    if _CLIENT_PACK_VERSION_RE.fullmatch(normalised) is None:
        raise ValueError("client pack version must be numeric or use YYYY-MM-DD[.N]")
    return normalised


class ClientPackRelease(BaseModel):
    version: str
    changelog: str

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, raw: object) -> str:
        version = normalise_client_pack_version(raw, required=True)
        assert version is not None
        return version

    @field_validator("changelog", mode="before")
    @classmethod
    def validate_changelog(cls, raw: object) -> str:
        changelog = normalise_client_pack_changelog(raw, required=True)
        assert changelog is not None
        return changelog


class ClientPackModSnapshot(BaseModel):
    name: str
    friendly: str
    version: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("name", "friendly", mode="before")
    @classmethod
    def validate_required_text(cls, raw: object, info: ValidationInfo) -> str:
        field_name = info.field_name or "value"
        return _normalise_required_text(raw, field_name=field_name)

    @field_validator("version", mode="before")
    @classmethod
    def validate_optional_version(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)


_CLIENT_PACK_KUBEJS_SCRIPT_ROOTS = frozenset({"server_scripts", "startup_scripts"})


def normalise_client_pack_kubejs_script_path(raw: object) -> str:
    if not isinstance(raw, str):
        raise TypeError("client-pack KubeJS script paths must be strings")
    path = PurePosixPath(raw.strip().replace("\\", "/"))
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] not in _CLIENT_PACK_KUBEJS_SCRIPT_ROOTS
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(
            "client-pack KubeJS script paths must be relative to server_scripts or startup_scripts"
        )
    if path.name.casefold() == "example.js":
        raise ValueError("the built-in KubeJS example.js cannot be included in client packs")
    return path.as_posix()


class ClientPackKubeJsScript(BaseModel):
    relative_path: str
    included: bool = True

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("relative_path", mode="before")
    @classmethod
    def validate_relative_path(cls, raw: object) -> str:
        return normalise_client_pack_kubejs_script_path(raw)


class ClientPackMetadataConfig(BaseModel):
    name: str = Field(min_length=1, max_length=CLIENT_PACK_METADATA_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=CLIENT_PACK_METADATA_DESCRIPTION_MAX_LENGTH)
    filename_template: str = Field(
        default=CLIENT_PACK_FILENAME_TEMPLATE_DEFAULT,
        min_length=1,
        max_length=CLIENT_PACK_FILENAME_TEMPLATE_MAX_LENGTH,
    )
    include_servers_dat: bool = True
    include_options_txt: bool = True

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("filename_template")
    @classmethod
    def validate_filename_template(cls, template: str) -> str:
        if "/" in template or "\\" in template:
            raise ValueError("client-pack filename templates cannot contain path separators")
        if any(character in '<>:"|?*' or ord(character) < 32 for character in template):
            raise ValueError("client-pack filename templates contain invalid filename characters")
        allowed_placeholders = frozenset(CLIENT_PACK_FILENAME_PLACEHOLDERS)
        try:
            fields = tuple(Formatter().parse(template))
        except ValueError as xcp:
            raise ValueError("client-pack filename template contains invalid braces") from xcp
        for _literal, field_name, format_spec, conversion in fields:
            if field_name is None:
                continue
            if field_name not in allowed_placeholders:
                raise ValueError(f"unknown client-pack filename placeholder: {field_name}")
            if format_spec or conversion:
                raise ValueError("client-pack filename placeholders do not support formatting options")
        return template

    def filename_stem(
        self,
        *,
        app_name: str,
        version: str,
        minecraft_version: str,
        format_name: str,
    ) -> str:
        def filename_token(value: str) -> str:
            token = "".join(
                character if character.isalnum() or character in {"-", "_", "."} else "_" for character in value.strip()
            ).strip("._")
            return token or "client-pack"

        stem = self.filename_template.format(
            app_name=filename_token(app_name),
            pack_name=filename_token(self.name),
            version=filename_token(version),
            minecraft_version=filename_token(minecraft_version),
            format=filename_token(format_name),
        ).strip()
        if not stem or stem in {".", ".."}:
            raise ValueError("client-pack filename template produced an empty filename")
        if len(stem) > CLIENT_PACK_FILENAME_STEM_MAX_LENGTH:
            raise ValueError(
                f"client-pack filename stem must be at most {CLIENT_PACK_FILENAME_STEM_MAX_LENGTH} characters"
            )
        return stem


def normalise_activity_provider_ids(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple | set | frozenset):
        raise TypeError("activity provider ids must be a sequence of strings")

    provider_ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        provider_id = normalise_optional_text(item)
        if provider_id is None:
            continue
        provider_key = provider_id.casefold()
        if provider_key in seen:
            continue
        provider_ids.append(provider_id)
        seen.add(provider_key)
    return tuple(provider_ids)


def normalise_optional_build(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise TypeError("build must be an integer")
    if isinstance(raw, int):
        build = raw
    else:
        text = normalise_optional_text(raw)
        if text is None:
            return None
        if not text.isdigit():
            raise ValueError("build must be an integer")
        build = int(text)
    if build < 0:
        raise ValueError("build must be non-negative")
    return build


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


def _normalise_required_text(raw: object, *, field_name: str) -> str:
    text = normalise_optional_text(raw)
    if text is None:
        raise ValueError(f"{field_name} must not be empty")
    return text


class SteamUpdateLogin(BaseModel):
    username: str = "anonymous"
    password: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("username", mode="before")
    @classmethod
    def validate_username(cls, raw: object) -> str:
        return _normalise_required_text(raw, field_name="steam username")

    @field_validator("password", mode="before")
    @classmethod
    def validate_password(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)


class SteamUpdateBranch(BaseModel):
    branch_id: str
    label: str | None = None
    beta_password: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("branch_id", mode="before")
    @classmethod
    def validate_branch_id(cls, raw: object) -> str:
        return _normalise_required_text(raw, field_name="steam branch id")

    @field_validator("label", "beta_password", mode="before")
    @classmethod
    def validate_optional_text(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @property
    def display_label(self) -> str:
        return self.label or self.branch_id


class SteamUpdateConfig(BaseModel):
    app_id: int
    steamcmd_executable: str = "steamcmd"
    login: SteamUpdateLogin = Field(default_factory=SteamUpdateLogin)
    branches: tuple[SteamUpdateBranch, ...] = Field(
        default_factory=lambda: (SteamUpdateBranch(branch_id="public", label="Public"),)
    )
    selected_branch: str = "public"

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("app_id", mode="before")
    @classmethod
    def validate_app_id(cls, raw: object) -> int:
        if isinstance(raw, bool):
            raise TypeError("steam app id must be an integer")
        if isinstance(raw, int):
            app_id = raw
        else:
            text = _normalise_required_text(raw, field_name="steam app id")
            if not text.isdecimal():
                raise ValueError("steam app id must be numeric")
            app_id = int(text)
        if app_id <= 0:
            raise ValueError("steam app id must be positive")
        return app_id

    @field_validator("steamcmd_executable", mode="before")
    @classmethod
    def validate_steamcmd_executable(cls, raw: object) -> str:
        return _normalise_required_text(raw, field_name="steamcmd executable")

    @field_validator("selected_branch", mode="before")
    @classmethod
    def validate_selected_branch(cls, raw: object) -> str:
        return _normalise_required_text(raw, field_name="selected steam branch")

    @model_validator(mode="after")
    def validate_branches(self) -> "SteamUpdateConfig":
        branch_ids: list[str] = []
        seen_branch_keys: set[str] = set()
        for branch in self.branches:
            branch_key = branch.branch_id.casefold()
            if branch_key in seen_branch_keys:
                raise ValueError(f"duplicate steam branch id: {branch.branch_id}")
            seen_branch_keys.add(branch_key)
            branch_ids.append(branch.branch_id)
        if self.selected_branch.casefold() not in seen_branch_keys:
            raise ValueError(
                f"selected steam branch {self.selected_branch!r} must match one of: {', '.join(branch_ids)}"
            )
        return self

    def branch(self, branch_id: str) -> SteamUpdateBranch:
        branch_key = branch_id.strip().casefold()
        for branch in self.branches:
            if branch.branch_id.casefold() == branch_key:
                return branch
        raise ValueError(f"Unknown Steam branch: {branch_id}")

    @property
    def selected_branch_config(self) -> SteamUpdateBranch:
        return self.branch(self.selected_branch)


class FactorioUpdateBranch(enum.StrEnum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"

    @property
    def display_label(self) -> str:
        if self is FactorioUpdateBranch.STABLE:
            return "Stable"
        return "Experimental"


class FactorioUpdateConfig(BaseModel):
    selected_branch: FactorioUpdateBranch = FactorioUpdateBranch.STABLE
    installed_branch: FactorioUpdateBranch | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("selected_branch", "installed_branch", mode="before")
    @classmethod
    def validate_branch(cls, raw: object, info: ValidationInfo) -> FactorioUpdateBranch | None:
        if isinstance(raw, FactorioUpdateBranch):
            return raw
        text = normalise_optional_text(raw)
        if text is None:
            if info.field_name == "installed_branch":
                return None
            raise ValueError("selected Factorio branch must not be empty")
        try:
            return FactorioUpdateBranch(text.casefold())
        except ValueError as xcp:
            raise ValueError(f"unknown Factorio branch: {text}") from xcp

    def branch(self, branch_id: str) -> FactorioUpdateBranch:
        text = _normalise_required_text(branch_id, field_name="Factorio branch id")
        try:
            return FactorioUpdateBranch(text.casefold())
        except ValueError as xcp:
            raise ValueError(f"Unknown Factorio branch: {branch_id}") from xcp

    @property
    def selected_branch_label(self) -> str:
        return self.selected_branch.display_label


@dataclass(frozen=True, slots=True)
class SteamUpdatePreset:
    app_id: int
    branches: tuple[SteamUpdateBranch, ...]
    default_selected_branch: str = "public"

    def build_config(self, *, selected_branch: str | None = None) -> SteamUpdateConfig:
        resolved_selected_branch = (
            self.default_selected_branch
            if selected_branch is None or not selected_branch.strip()
            else selected_branch.strip()
        )
        return SteamUpdateConfig(
            app_id=self.app_id,
            branches=tuple(branch.model_copy(deep=True) for branch in self.branches),
            selected_branch=resolved_selected_branch,
        )


_STEAM_UPDATE_PRESETS: dict[str, SteamUpdatePreset] = {
    "satisfactory": SteamUpdatePreset(
        app_id=1690800,
        branches=(
            SteamUpdateBranch(branch_id="public", label="Stable"),
            SteamUpdateBranch(branch_id="experimental", label="Experimental"),
        ),
        default_selected_branch="public",
    ),
    "sevendays": SteamUpdatePreset(
        app_id=294420,
        branches=(
            SteamUpdateBranch(branch_id="public", label="Stable"),
            SteamUpdateBranch(branch_id="latest_experimental", label="Experimental"),
        ),
        default_selected_branch="latest_experimental",
    ),
}


def steam_update_preset_for_scope(scope: str | None) -> SteamUpdatePreset | None:
    if scope is None:
        return None
    scope_key = scope.strip().casefold()
    if not scope_key:
        return None
    return _STEAM_UPDATE_PRESETS.get(scope_key)


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

class ModSide(enum.StrEnum):
    SERVER = "server"
    CLIENT = "client"
    BOTH = "both"


class ModPlacement(enum.StrEnum):
    SERVER_ENABLED = "server_enabled"
    SERVER_DISABLED = "server_disabled"
    CLIENT_ONLY = "client_only"

    @property
    def enabled(self) -> bool:
        return self is ModPlacement.SERVER_ENABLED

    @property
    def server_loadable(self) -> bool:
        return self is not ModPlacement.CLIENT_ONLY

    @property
    def label(self) -> str:
        match self:
            case ModPlacement.SERVER_ENABLED:
                return "Server enabled"
            case ModPlacement.SERVER_DISABLED:
                return "Server disabled"
            case ModPlacement.CLIENT_ONLY:
                return "Client only"


def is_client_pack_candidate(placement: ModPlacement, side: ModSide) -> bool:
    """Return whether a mod placement may participate in client packs."""
    if placement is ModPlacement.CLIENT_ONLY:
        if side is not ModSide.CLIENT:
            raise ValueError(
                f"Client-only mod placement requires client-side classification, not {side.value!r}"
            )
        return True
    return placement is not ModPlacement.SERVER_DISABLED


class ModType(enum.StrEnum):
    REGULAR = "regular"
    COREMOD = "coremod"
    BUILTIN = "builtin"
    SERVER = "server"
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
            case ModType.SERVER:
                return "Server"
            case ModType.CLIENT:
                return "Client"

    @property
    def side(self) -> ModSide:
        match self:
            case ModType.REGULAR | ModType.COREMOD:
                return ModSide.BOTH
            case ModType.SERVER | ModType.BUILTIN:
                return ModSide.SERVER
            case ModType.CLIENT:
                return ModSide.CLIENT

    @property
    def included_in_client_by_default(self) -> bool:
        return self is not ModType.BUILTIN


class ModMetadataOverrides(BaseModel):
    """Optional operator-supplied mod metadata shown in place of detected values."""

    friendly_name: str | None = None
    version: str | None = None
    origin: str | None = None
    added: datetime | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("friendly_name")
    @classmethod
    def validate_friendly_name(cls, raw: str | None) -> str | None:
        return normalise_optional_friendly_name(raw)

    @field_validator("version", "origin")
    @classmethod
    def normalise_text(cls, raw: str | None) -> str | None:
        return normalise_optional_text(raw)


class KnownModPageProvider(enum.StrEnum):
    MODRINTH = "Modrinth"
    CURSEFORGE = "CurseForge"
    NEXUSMODS = "NexusMods"
    SEVEN_DAYS_TO_DIE_MODS = "7D2Dmods"
    FACTORIO_MODS = "FactorioMods"
    MOD_IO = "mod.io"
    STEAM_WORKSHOP = "Steam Workshop"
    TRANSPORT_FEVER_NET = "TransportFever.net"
    THUNDERSTORE = "Thunderstore"
    PLANET_MINECRAFT = "Planet Minecraft"
    SPIGOT_MC = "SpigotMC"
    HANGAR = "Hangar"
    BUKKIT = "Bukkit"
    DISCORD = "Discord"
    REDDIT = "Reddit"
    YOUTUBE = "YouTube"
    PATREON = "Patreon"
    KO_FI = "Ko-fi"
    GITHUB = "GitHub"
    GITLAB = "GitLab"

    @property
    def domains(self) -> tuple[str, ...]:
        match self:
            case KnownModPageProvider.MODRINTH:
                return ("modrinth.com",)
            case KnownModPageProvider.CURSEFORGE:
                return ("curseforge.com",)
            case KnownModPageProvider.NEXUSMODS:
                return ("nexusmods.com",)
            case KnownModPageProvider.SEVEN_DAYS_TO_DIE_MODS:
                return ("7daystodiemods.com",)
            case KnownModPageProvider.FACTORIO_MODS:
                return ("mods.factorio.com",)
            case KnownModPageProvider.MOD_IO:
                return ("mod.io",)
            case KnownModPageProvider.STEAM_WORKSHOP:
                return ("steamcommunity.com",)
            case KnownModPageProvider.TRANSPORT_FEVER_NET:
                return ("transportfever.net",)
            case KnownModPageProvider.THUNDERSTORE:
                return ("thunderstore.io",)
            case KnownModPageProvider.PLANET_MINECRAFT:
                return ("planetminecraft.com",)
            case KnownModPageProvider.SPIGOT_MC:
                return ("spigotmc.org",)
            case KnownModPageProvider.HANGAR:
                return ("hangar.papermc.io",)
            case KnownModPageProvider.BUKKIT:
                return ("dev.bukkit.org",)
            case KnownModPageProvider.DISCORD:
                return ("discord.com", "discord.gg")
            case KnownModPageProvider.REDDIT:
                return ("reddit.com",)
            case KnownModPageProvider.YOUTUBE:
                return ("youtube.com", "youtu.be")
            case KnownModPageProvider.PATREON:
                return ("patreon.com",)
            case KnownModPageProvider.KO_FI:
                return ("ko-fi.com",)
            case KnownModPageProvider.GITHUB:
                return ("github.com",)
            case KnownModPageProvider.GITLAB:
                return ("gitlab.com",)


def normalise_mod_page_url(raw: object) -> str:
    url = _normalise_required_text(raw, field_name="Mod page URL")
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or parsed.hostname is None:
        raise ValueError("Mod page URLs must be absolute HTTPS URLs")
    return url


def known_mod_page_provider_for_url(raw: object) -> KnownModPageProvider | None:
    try:
        url = normalise_mod_page_url(raw)
    except (TypeError, ValueError):
        return None
    hostname = urlsplit(url).hostname
    assert hostname is not None
    normalised_hostname = hostname.casefold()
    for provider in KnownModPageProvider:
        if any(
            normalised_hostname == domain or normalised_hostname.endswith(f".{domain}")
            for domain in provider.domains
        ):
            return provider
    return None


class ModPageLink(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("url", mode="before")
    @classmethod
    def validate_url(cls, raw: object) -> str:
        return normalise_mod_page_url(raw)


_MOD_PAGE_DISPLAY_ORDER: tuple[KnownModPageProvider, ...] = (
    KnownModPageProvider.MODRINTH,
    KnownModPageProvider.CURSEFORGE,
    KnownModPageProvider.MOD_IO,
    KnownModPageProvider.STEAM_WORKSHOP,
    KnownModPageProvider.NEXUSMODS,
    KnownModPageProvider.TRANSPORT_FEVER_NET,
    KnownModPageProvider.SEVEN_DAYS_TO_DIE_MODS,
)
_MOD_PAGE_DISPLAY_PRIORITY: dict[KnownModPageProvider, int] = {
    provider: priority for priority, provider in enumerate(_MOD_PAGE_DISPLAY_ORDER)
}


def _mod_page_display_priority(page: ModPageLink) -> int:
    provider = known_mod_page_provider_for_url(page.url)
    if provider is None:
        return len(_MOD_PAGE_DISPLAY_ORDER)
    return _MOD_PAGE_DISPLAY_PRIORITY.get(provider, len(_MOD_PAGE_DISPLAY_ORDER))


def mod_pages_in_display_order(mod_pages: Iterable[ModPageLink]) -> tuple[ModPageLink, ...]:
    return tuple(sorted(mod_pages, key=_mod_page_display_priority))


class ModPageMatchConfidence(enum.StrEnum):
    EXACT = "exact"
    STRONG = "strong"
    POSSIBLE = "possible"

    @property
    def label(self) -> str:
        return self.value.title()


class ModPageMatchReason(enum.StrEnum):
    FILE_HASH = "file_hash"
    FILE_FINGERPRINT = "file_fingerprint"
    NAME = "name"
    GAME_VERSION = "game_version"
    LOADER = "loader"

    @property
    def label(self) -> str:
        match self:
            case ModPageMatchReason.FILE_HASH:
                return "file hash"
            case ModPageMatchReason.FILE_FINGERPRINT:
                return "file fingerprint"
            case ModPageMatchReason.NAME:
                return "name"
            case ModPageMatchReason.GAME_VERSION:
                return "game version"
            case ModPageMatchReason.LOADER:
                return "loader"


class ModPageCandidate(BaseModel):
    provider: Provider
    page: ModPageLink
    project_id: str
    title: str
    author: str | None = None
    summary: str | None = None
    game_versions: tuple[str, ...] = ()
    loaders: tuple[str, ...] = ()
    confidence: ModPageMatchConfidence
    match_reasons: tuple[ModPageMatchReason, ...]

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("project_id", "title", mode="before")
    @classmethod
    def validate_required_text(cls, raw: object, info: ValidationInfo) -> str:
        field_name = info.field_name or "value"
        return _normalise_required_text(raw, field_name=field_name.replace("_", " "))

    @field_validator("author", "summary", mode="before")
    @classmethod
    def validate_optional_text(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @model_validator(mode="after")
    def validate_candidate(self) -> ModPageCandidate:
        expected_page_provider = {
            Provider.MODRINTH: KnownModPageProvider.MODRINTH,
            Provider.CURSEFORGE: KnownModPageProvider.CURSEFORGE,
        }.get(self.provider)
        if expected_page_provider is None:
            raise ValueError(f"unsupported mod page candidate provider: {self.provider.value}")
        if known_mod_page_provider_for_url(self.page.url) is not expected_page_provider:
            raise ValueError("mod page candidate URL does not match its provider")
        if not self.match_reasons:
            raise ValueError("mod page candidates require at least one match reason")
        if len(self.match_reasons) != len(set(self.match_reasons)):
            raise ValueError("mod page candidate match reasons must be unique")
        return self

    @property
    def selection_label(self) -> str:
        reasons = ", ".join(reason.label for reason in self.match_reasons)
        author = f" by {self.author}" if self.author is not None else ""
        return f"{self.title}{author} — {self.confidence.label} — matched {reasons}"


class ModPageProviderCandidates(BaseModel):
    provider: Provider
    candidates: tuple[ModPageCandidate, ...] = ()
    error: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @model_validator(mode="after")
    def validate_candidates(self) -> ModPageProviderCandidates:
        if any(candidate.provider is not self.provider for candidate in self.candidates):
            raise ValueError("mod page candidates must match their provider result")
        if self.error is not None and self.candidates:
            raise ValueError("mod page provider results cannot contain candidates and an error")
        return self


class ModPageDiscovery(BaseModel):
    providers: tuple[ModPageProviderCandidates, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_unique_providers(self) -> ModPageDiscovery:
        providers = tuple(result.provider for result in self.providers)
        if len(providers) != len(set(providers)):
            raise ValueError("mod page discovery providers must be unique")
        return self

    @property
    def candidates(self) -> tuple[ModPageCandidate, ...]:
        return tuple(candidate for result in self.providers for candidate in result.candidates)


class ModClassificationOverride(BaseModel):
    """Operator-selected classification that takes precedence over automatic detection."""

    mod_type: ModType
    download_block_reason: ModDownloadBlockReason | None = None


class ModrinthModMetadata(BaseModel):
    page_url: str
    project_id: str
    version_id: str
    download_url: str
    description: str | None = None
    filename: str | None = None
    sha1: str | None = None
    sha512: str | None = None
    size: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("project_id", "version_id", mode="before")
    @classmethod
    def validate_identifier(cls, raw: object) -> str:
        return _normalise_required_text(raw, field_name="Modrinth identifier")

    @field_validator("filename", mode="before")
    @classmethod
    def validate_filename(cls, raw: object) -> str | None:
        if raw is None:
            return None
        filename = _normalise_required_text(raw, field_name="Modrinth filename")
        if PurePosixPath(filename).name != filename or filename in {".", ".."}:
            raise ValueError("Modrinth filename must be a single file name")
        return filename

    @field_validator("description", mode="before")
    @classmethod
    def validate_description(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @field_validator("sha1", "sha512", mode="before")
    @classmethod
    def validate_hash(cls, raw: object, info: ValidationInfo) -> str | None:
        if raw is None:
            return None
        digest = _normalise_required_text(raw, field_name=f"Modrinth {info.field_name}").casefold()
        expected_length = 40 if info.field_name == "sha1" else 128
        if len(digest) != expected_length or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                f"Modrinth {info.field_name} must be a {expected_length}-character hexadecimal digest"
            )
        return digest

    @field_validator("size", mode="before")
    @classmethod
    def validate_size(cls, raw: object) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError("Modrinth file size must be an integer")
        if raw <= 0:
            raise ValueError("Modrinth file size must be positive")
        return raw

    @field_validator("page_url", "download_url", mode="before")
    @classmethod
    def validate_download_url(cls, raw: object) -> str:
        url = _normalise_required_text(raw, field_name="Modrinth URL")
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "https" or parsed.hostname is None:
            raise ValueError("Modrinth URLs must be absolute HTTPS URLs")
        return url


class CurseForgeFileReference(BaseModel):
    project_id: int
    file_id: int

    @field_validator("project_id", "file_id", mode="before")
    @classmethod
    def validate_identifier(cls, raw: object) -> int:
        if isinstance(raw, bool):
            raise TypeError("CurseForge identifiers must be integers")
        if isinstance(raw, int):
            identifier = raw
        elif isinstance(raw, str) and raw.strip().isdecimal():
            identifier = int(raw.strip())
        else:
            raise ValueError("CurseForge identifiers must be integers")
        if identifier <= 0:
            raise ValueError("CurseForge identifiers must be positive")
        return identifier


class CurseForgeModMetadata(CurseForgeFileReference):
    page_url: str | None = None
    description: str | None = None

    @field_validator("page_url", mode="before")
    @classmethod
    def validate_page_url(cls, raw: object) -> str | None:
        if raw is None:
            return None
        url = _normalise_required_text(raw, field_name="CurseForge URL")
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "https" or parsed.hostname is None:
            raise ValueError("CurseForge URL must be an absolute HTTPS URL")
        return url

    @field_validator("description", mode="before")
    @classmethod
    def validate_description(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)


class LauncherProviderUrls(BaseModel):
    modrinth: str | None = None
    curseforge: str | None = None
    curseforge_reference: CurseForgeFileReference | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("modrinth", "curseforge", mode="before")
    @classmethod
    def validate_optional_url(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    def for_provider(self, provider: Provider) -> str | None:
        match provider:
            case Provider.MODRINTH:
                return self.modrinth
            case Provider.CURSEFORGE:
                return self.curseforge
            case _:
                raise ValueError(f"Launcher metadata URLs do not support {provider.value}.")

    def has_provider(self, provider: Provider) -> bool:
        if provider is Provider.CURSEFORGE and self.curseforge_reference is not None:
            return True
        return self.for_provider(provider) is not None

    @model_validator(mode="after")
    def validate_curseforge_source(self) -> LauncherProviderUrls:
        if self.curseforge is not None and self.curseforge_reference is not None:
            raise ValueError("Provide either a CurseForge file page or CurseForge project and file IDs, not both.")
        return self


class LauncherMetadataMatchReason(enum.StrEnum):
    EXPLICIT_FILE_PAGE = "explicit_file_page"
    SHA1 = "sha1"
    FILENAME_AND_SIZE = "filename_and_size"
    FILENAME = "filename"

    @property
    def label(self) -> str:
        match self:
            case LauncherMetadataMatchReason.EXPLICIT_FILE_PAGE:
                return "explicit file page"
            case LauncherMetadataMatchReason.SHA1:
                return "SHA-1"
            case LauncherMetadataMatchReason.FILENAME_AND_SIZE:
                return "filename and size"
            case LauncherMetadataMatchReason.FILENAME:
                return "filename"


class LauncherMetadataReleaseChannel(enum.StrEnum):
    RELEASE = "release"
    BETA = "beta"
    ALPHA = "alpha"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return self.value.title()


class LauncherMetadataCandidate(BaseModel):
    provider: Provider
    project_page_url: str
    file_page_url: str
    version: str
    filename: str
    size: int | None = None
    game_versions: tuple[str, ...] = ()
    loaders: tuple[str, ...] = ()
    release_channel: LauncherMetadataReleaseChannel = LauncherMetadataReleaseChannel.UNKNOWN
    match_reasons: tuple[LauncherMetadataMatchReason, ...]

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("project_page_url", "file_page_url", mode="before")
    @classmethod
    def validate_page_url(cls, raw: object) -> str:
        return normalise_mod_page_url(raw)

    @field_validator("version", "filename", mode="before")
    @classmethod
    def validate_required_text(cls, raw: object, info: ValidationInfo) -> str:
        field_name = info.field_name or "value"
        return _normalise_required_text(raw, field_name=field_name.replace("_", " "))

    @field_validator("size", mode="before")
    @classmethod
    def validate_size(cls, raw: object) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError("launcher metadata candidate size must be an integer")
        if raw <= 0:
            raise ValueError("launcher metadata candidate size must be positive")
        return raw

    @model_validator(mode="after")
    def validate_match_reasons(self) -> LauncherMetadataCandidate:
        if not self.match_reasons:
            raise ValueError("launcher metadata candidates require at least one match reason")
        if len(self.match_reasons) != len(set(self.match_reasons)):
            raise ValueError("launcher metadata candidate match reasons must be unique")
        return self

    @property
    def selection_label(self) -> str:
        reasons = ", ".join(reason.label for reason in self.match_reasons)
        compatibility = ", ".join((*self.game_versions, *self.loaders))
        suffix = f" — {compatibility}" if compatibility else ""
        return f"{self.version} — {self.filename} — matched {reasons}{suffix}"


class LauncherMetadataProviderCandidates(BaseModel):
    provider: Provider
    project_page_url: str
    candidates: tuple[LauncherMetadataCandidate, ...] = ()
    error: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("project_page_url", mode="before")
    @classmethod
    def validate_project_page_url(cls, raw: object) -> str:
        return normalise_mod_page_url(raw)

    @model_validator(mode="after")
    def validate_candidates(self) -> LauncherMetadataProviderCandidates:
        if any(candidate.provider is not self.provider for candidate in self.candidates):
            raise ValueError("launcher metadata candidates must match their provider result")
        if self.error is not None and self.candidates:
            raise ValueError("launcher metadata provider results cannot contain candidates and an error")
        return self


class LauncherMetadataDiscovery(BaseModel):
    providers: tuple[LauncherMetadataProviderCandidates, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_unique_providers(self) -> LauncherMetadataDiscovery:
        providers = tuple(result.provider for result in self.providers)
        if len(providers) != len(set(providers)):
            raise ValueError("launcher metadata discovery providers must be unique")
        return self

    @property
    def candidates(self) -> tuple[LauncherMetadataCandidate, ...]:
        return tuple(candidate for result in self.providers for candidate in result.candidates)


class ModPlatformMetadata(BaseModel):
    modrinth: ModrinthModMetadata | None = None
    curseforge: CurseForgeModMetadata | None = None

    @property
    def description(self) -> str | None:
        if self.modrinth is not None and self.modrinth.description is not None:
            return self.modrinth.description
        if self.curseforge is not None:
            return self.curseforge.description
        return None

    def page_url_for(self, provider: Provider) -> str | None:
        match provider:
            case Provider.MODRINTH:
                return None if self.modrinth is None else self.modrinth.page_url
            case Provider.CURSEFORGE:
                return None if self.curseforge is None else self.curseforge.page_url
            case _:
                raise ValueError(f"Launcher metadata does not support {provider.value}.")


class LauncherMetadataProviderError(BaseModel):
    provider: Provider
    message: str

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("message", mode="before")
    @classmethod
    def validate_message(cls, raw: object) -> str:
        return _normalise_required_text(raw, field_name="launcher metadata provider error")


class LauncherMetadataResolution(BaseModel):
    platforms: ModPlatformMetadata = Field(default_factory=ModPlatformMetadata)
    suggested_mod_type: ModType | None = None
    suggestion_provider: Provider | None = None
    provider_errors: tuple[LauncherMetadataProviderError, ...] = ()

    @model_validator(mode="after")
    def validate_suggestion_provider(self) -> LauncherMetadataResolution:
        if (self.suggested_mod_type is None) != (self.suggestion_provider is None):
            raise ValueError("Launcher metadata suggestions require both a mod type and provider")
        providers = tuple(error.provider for error in self.provider_errors)
        if len(providers) != len(set(providers)):
            raise ValueError("launcher metadata provider errors must have unique providers")
        return self


class BulkLauncherMetadataStatus(enum.StrEnum):
    EXACT = "exact"
    UNMATCHED = "unmatched"

    @property
    def label(self) -> str:
        return self.value.title()


BulkLauncherMetadataProviderError = LauncherMetadataProviderError


class BulkLauncherMetadataEntry(BaseModel):
    mod_name: str
    friendly_name: str
    status: BulkLauncherMetadataStatus
    mod_pages: tuple[ModPageLink, ...] = ()
    platforms: ModPlatformMetadata = Field(default_factory=ModPlatformMetadata)
    suggested_mod_type: ModType | None = None
    matched_providers: tuple[Provider, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("mod_name", "friendly_name", mode="before")
    @classmethod
    def validate_required_text(cls, raw: object, info: ValidationInfo) -> str:
        field_name = info.field_name or "value"
        return _normalise_required_text(raw, field_name=field_name.replace("_", " "))

    @model_validator(mode="after")
    def validate_matches(self) -> BulkLauncherMetadataEntry:
        if len(self.matched_providers) != len(set(self.matched_providers)):
            raise ValueError("bulk launcher metadata matched providers must be unique")
        expected_providers = tuple(
            provider
            for provider in (Provider.MODRINTH, Provider.CURSEFORGE)
            if self.platforms.page_url_for(provider) is not None
        )
        if set(self.matched_providers) != set(expected_providers):
            raise ValueError("bulk launcher metadata providers must match platform metadata")
        if self.status is BulkLauncherMetadataStatus.EXACT and not self.matched_providers:
            raise ValueError("exact bulk launcher metadata entries require a provider match")
        if self.status is BulkLauncherMetadataStatus.UNMATCHED and self.matched_providers:
            raise ValueError("unmatched bulk launcher metadata entries cannot contain provider matches")
        return self


class BulkLauncherMetadataDiscovery(BaseModel):
    entries: tuple[BulkLauncherMetadataEntry, ...] = ()
    provider_errors: tuple[BulkLauncherMetadataProviderError, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_unique_values(self) -> BulkLauncherMetadataDiscovery:
        mod_names = tuple(entry.mod_name for entry in self.entries)
        if len(mod_names) != len(set(mod_names)):
            raise ValueError("bulk launcher metadata entries must have unique mod names")
        providers = tuple(error.provider for error in self.provider_errors)
        if len(providers) != len(set(providers)):
            raise ValueError("bulk launcher metadata provider errors must have unique providers")
        return self

    @property
    def exact_entries(self) -> tuple[BulkLauncherMetadataEntry, ...]:
        return tuple(
            entry for entry in self.entries if entry.status is BulkLauncherMetadataStatus.EXACT
        )


class ClientPackPolicy(enum.StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    ALTERNATIVE = "alternative"

    @property
    def label(self) -> str:
        match self:
            case ClientPackPolicy.REQUIRED:
                return "Required"
            case ClientPackPolicy.OPTIONAL:
                return "Optional"
            case ClientPackPolicy.ALTERNATIVE:
                return "Alternative"


class ClientPackConfig(BaseModel):
    included_in_client: bool = True
    policy: ClientPackPolicy = ClientPackPolicy.REQUIRED
    choice_group: str | None = None
    default_choice: bool = False
    default_selected: bool = False

    model_config = ConfigDict(str_strip_whitespace=True)

    def apply_default_inclusion(self, mod_type: ModType) -> None:
        """Apply the type-derived default without marking it as an operator override."""
        if "included_in_client" not in self.model_fields_set:
            object.__setattr__(
                self,
                "included_in_client",
                mod_type.included_in_client_by_default,
            )

    @field_validator("choice_group", mode="before")
    @classmethod
    def validate_choice_group(cls, value: object) -> object:
        if isinstance(value, str) and any(character.isspace() for character in value):
            raise ValueError("client-pack choice group IDs cannot contain whitespace")
        return value

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_optional_default(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        policy = payload.get("policy", ClientPackPolicy.REQUIRED)
        if policy in (ClientPackPolicy.OPTIONAL, ClientPackPolicy.OPTIONAL.value):
            payload.setdefault("default_selected", True)
        return payload

    @model_validator(mode="after")
    def validate_choice_configuration(self) -> ClientPackConfig:
        if self.policy is ClientPackPolicy.ALTERNATIVE:
            if not self.choice_group:
                raise ValueError("alternative client-pack mods require a choice group")
            if self.default_selected:
                raise ValueError("alternative client-pack mods use default_choice, not default_selected")
            return self
        if self.choice_group is not None:
            raise ValueError("only alternative client-pack mods may have a choice group")
        if self.default_choice:
            raise ValueError("only alternative client-pack mods may be the default choice")
        if self.policy is not ClientPackPolicy.OPTIONAL and self.default_selected:
            raise ValueError("only optional client-pack mods may be selected by default")
        return self


_VERSION_LOADER_RE: Pattern[str] = re.compile(r"[a-z0-9_]+")
_APP_VERSION_MAIN_TOKEN_RE: Pattern[str] = re.compile(r"\d+|[a-z]+", re.IGNORECASE)


def normalise_version_loader(raw: object) -> str | None:
    text: str | None = normalise_optional_text(raw)
    if text is None:
        return None
    normalised: str = text.casefold().replace("-", "_").replace(" ", "_")
    if _VERSION_LOADER_RE.fullmatch(normalised) is None:
        raise ValueError(f"invalid version loader {text!r}")
    return normalised


def _app_version_main_tokens(raw_main: str) -> tuple[int | str, ...]:
    tokens = [
        int(token) if token.isdigit() else token.casefold() for token in _APP_VERSION_MAIN_TOKEN_RE.findall(raw_main)
    ]
    if tokens:
        return tuple(tokens)
    return (raw_main.casefold(),)


def _compare_app_version_main(left_main: str, right_main: str) -> int:
    missing = object()
    left_tokens = _app_version_main_tokens(left_main)
    right_tokens = _app_version_main_tokens(right_main)
    for left_token, right_token in zip_longest(left_tokens, right_tokens, fillvalue=missing):
        if left_token is missing:
            return -1
        if right_token is missing:
            return 1
        if left_token == right_token:
            continue
        if isinstance(left_token, int) and isinstance(right_token, int):
            return -1 if left_token < right_token else 1
        if isinstance(left_token, str) and isinstance(right_token, str):
            return -1 if left_token < right_token else 1
        return -1 if isinstance(left_token, int) else 1
    return 0


class AppVersion(BaseModel):
    main: str
    build: int | None = None
    framework: str | None = None
    loader: str | None = None
    steam_build: int | None = None
    steam_branch: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @property
    def display_value(self) -> str:
        main_value = self.main if self.build is None else f"{self.main}:{self.build}"
        extra_parts: list[str] = []
        if self.loader is not None and self.framework is not None:
            extra_parts.append(f"{self.loader} {self.framework}")
        elif self.loader is not None:
            extra_parts.append(self.loader)
        elif self.framework is not None:
            extra_parts.append(self.framework)
        if self.steam_build is not None or self.steam_branch is not None:
            steam_parts = ["Steam"]
            if self.steam_branch is not None:
                steam_parts.append(self.steam_branch)
            if self.steam_build is not None:
                steam_parts.extend(("build", str(self.steam_build)))
            extra_parts.append(" ".join(steam_parts))
        if not extra_parts:
            return main_value
        return f"{main_value} {' '.join(f'[{part}]' for part in extra_parts)}"

    def compare_main_and_build(self, other: "AppVersion") -> int:
        main_cmp = _compare_app_version_main(self.main, other.main)
        if main_cmp != 0:
            return main_cmp
        left_build = -1 if self.build is None else self.build
        right_build = -1 if other.build is None else other.build
        if left_build == right_build:
            return 0
        return -1 if left_build < right_build else 1

    def is_at_least(self, minimum: "AppVersion") -> bool:
        return self.compare_main_and_build(minimum) >= 0

    def is_at_most(self, maximum: "AppVersion") -> bool:
        return self.compare_main_and_build(maximum) <= 0

    @field_validator("main", "framework", "steam_branch", mode="before")
    @classmethod
    def validate_text_fields(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @field_validator("build", "steam_build", mode="before")
    @classmethod
    def validate_build(cls, raw: object) -> int | None:
        return normalise_optional_build(raw)

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
        main: str | None = normalise_optional_text(raw)
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
    client_mods_dir: Path | None = None
    client_overrides_dir: Path | None = None
    client_pack_current_hash: str | None = None
    client_pack_published_hash: str | None = None
    client_pack_published_version: str | None = None
    client_pack_published_changelog: str | None = None
    client_pack_releases: tuple[ClientPackRelease, ...] = ()
    client_pack_published_mods: tuple[ClientPackModSnapshot, ...] = ()
    client_pack_verified_hash: str | None = None
    client_pack_content_dirty: bool = False
    client_pack_excluded_kubejs_scripts: tuple[str, ...] = ()
    client_pack_metadata: ClientPackMetadataConfig | None = None
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
    relay_notice_player_session: bool = True
    relay_notice_player_death: bool = True
    relay_notice_progress: bool = True
    disabled_activity_provider_ids: tuple[str, ...] = Field(default_factory=tuple)
    cmd_start: list[str] = Field(default_factory=list)
    provider_alt_text: str | None = None
    factorio_chat_relay_use_shout: bool = True
    rcon_requires_online_players: bool | None = None
    version: AppVersion | None = None
    steam_update: SteamUpdateConfig | None = None
    factorio_update: FactorioUpdateConfig | None = None
    resource_points: AppResourcePointProfile = Field(default_factory=AppResourcePointProfile)
    config_file_read_level_override: Power_Level | None = None
    config_file_write_level_override: Power_Level | None = None
    save_file_write_level_override: Power_Level | None = None

    @field_validator("friendly_name", mode="before")
    @classmethod
    def validate_friendly_name(cls, raw: object) -> str | None:
        return normalise_optional_friendly_name(raw)

    @field_validator("client_pack_current_hash", "client_pack_published_hash", "client_pack_verified_hash")
    @classmethod
    def validate_client_pack_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", normalised) is None:
            raise ValueError("client pack hashes must be SHA-256 hex digests")
        return normalised

    @field_validator("client_pack_published_version", mode="before")
    @classmethod
    def validate_client_pack_published_version(cls, value: object) -> str | None:
        return normalise_client_pack_version(value)

    @field_validator("client_pack_releases")
    @classmethod
    def validate_client_pack_releases(
        cls,
        releases: tuple[ClientPackRelease, ...],
    ) -> tuple[ClientPackRelease, ...]:
        versions = [release.version for release in releases]
        if len(versions) != len(set(versions)):
            raise ValueError("client pack release versions must be unique")
        return releases

    @field_validator("client_pack_published_mods")
    @classmethod
    def validate_client_pack_published_mods(
        cls,
        mods: tuple[ClientPackModSnapshot, ...],
    ) -> tuple[ClientPackModSnapshot, ...]:
        names = [mod.name.casefold() for mod in mods]
        if len(names) != len(set(names)):
            raise ValueError("client pack published mods must be unique")
        return tuple(sorted(mods, key=lambda mod: mod.friendly.casefold()))

    @field_validator("client_pack_published_changelog", mode="before")
    @classmethod
    def validate_client_pack_changelog(cls, value: object) -> str | None:
        return normalise_client_pack_changelog(value)

    @field_validator("client_pack_excluded_kubejs_scripts", mode="before")
    @classmethod
    def validate_client_pack_excluded_kubejs_scripts(cls, raw: object) -> tuple[str, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list | tuple | set | frozenset):
            raise TypeError("client_pack_excluded_kubejs_scripts must be a sequence")
        paths = tuple(normalise_client_pack_kubejs_script_path(value) for value in raw)
        if len(paths) != len(set(paths)):
            raise ValueError("client-pack excluded KubeJS script paths must be unique")
        return tuple(sorted(paths, key=str.casefold))

    @field_validator("notes", mode="before")
    @classmethod
    def validate_optional_text_fields(cls, raw: object) -> str | None:
        return normalise_optional_text(raw)

    @field_validator("disabled_activity_provider_ids", mode="before")
    @classmethod
    def validate_disabled_activity_provider_ids(cls, raw: object) -> tuple[str, ...]:
        return normalise_activity_provider_ids(raw)

    @field_validator("title_font_preset", mode="before")
    @classmethod
    def validate_title_font_preset(cls, raw: object) -> str:
        return normalise_app_title_font(raw)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_relay_notice_player_session(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        if "relay_notice_player_session" in raw:
            return raw
        joined = raw.get("relay_notice_player_joined")
        left = raw.get("relay_notice_player_left")
        if isinstance(joined, bool) and isinstance(left, bool):
            raw["relay_notice_player_session"] = joined and left
        elif isinstance(joined, bool):
            raw["relay_notice_player_session"] = joined
        elif isinstance(left, bool):
            raw["relay_notice_player_session"] = left
        return raw

    @property
    def enabled_txt(self) -> str:
        return "Enabled" if self.enabled else "Disabled"

    @property
    def join_address(self) -> str | None:
        host: str = self.join_host.strip()
        if not host:
            return None
        return _format_host_port(host=host, port=self.join_port)

    @property
    def join_direct_ip_address(self) -> str | None:
        host: str = self.join_host.strip()
        public_addr: str = config.PUBLIC_ADDR.strip()
        public_ip: str = config.PUBLIC_IP.strip()
        if not host or not public_ip:
            return None
        if host.casefold() != public_addr.casefold():
            return None
        if host.casefold() == public_ip.casefold():
            return None
        return _format_host_port(host=public_ip, port=self.join_port)

    @property
    def join_display_address(self) -> str | None:
        address: str | None = self.join_address
        if address is None:
            return None
        direct_ip_address: str | None = self.join_direct_ip_address
        if direct_ip_address is None:
            return address
        return f"{address} [{direct_ip_address}]"

    @property
    def effective_api_host(self) -> str | None:
        api_host: str | None = self.api_host
        if api_host is not None and api_host.strip():
            return api_host.strip()
        join_host: str = self.join_host.strip()
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

    @field_validator(
        "directory",
        "mods_dir",
        "client_mods_dir",
        "client_overrides_dir",
        "settings_pointer",
        "server_log_file",
        mode="before",
    )
    def resolve_dir(cls, raw: str | Path | None, info) -> Path | None:
        return resolve_config_path(raw, directory=info.data.get("directory", ""))

    @field_validator("join_host", mode="before")
    def validate_join_host(cls, raw: object) -> str:
        if raw is None:
            return config.PUBLIC_ADDR
        text: str = str(raw).strip()
        return text or config.PUBLIC_ADDR

    @field_validator("api_host", mode="before")
    def validate_api_host(cls, raw: object) -> str | None:
        if raw is None:
            return None
        text: str = str(raw).strip()
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

    @field_validator("steam_update", mode="before")
    def validate_steam_update(cls, raw: object) -> SteamUpdateConfig | None:
        if raw is None:
            return None
        if isinstance(raw, SteamUpdateConfig):
            return raw
        if not isinstance(raw, dict):
            raise TypeError("steam_update must be a mapping")
        return SteamUpdateConfig.model_validate(raw)

    @field_validator("factorio_update", mode="before")
    def validate_factorio_update(cls, raw: object) -> FactorioUpdateConfig | None:
        if raw is None:
            return None
        if isinstance(raw, FactorioUpdateConfig):
            return raw
        if not isinstance(raw, dict):
            raise TypeError("factorio_update must be a mapping")
        return FactorioUpdateConfig.model_validate(raw)

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
            port: int = raw
        elif isinstance(raw, str):
            value: str = raw.strip()
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
    client_path: Path | None = None
    added: datetime = Field(default_factory=datetime.now)
    enabled: bool = True
    placement: ModPlacement = ModPlacement.SERVER_ENABLED
    version: str | None = None
    origin: str = "manual"
    mod_type: ModType = ModType.REGULAR
    download_block_reason: ModDownloadBlockReason | None = None
    classification_override: ModClassificationOverride | None = None
    mod_pages: tuple[ModPageLink, ...] = ()
    metadata_overrides: ModMetadataOverrides = Field(default_factory=ModMetadataOverrides)
    client_pack: ClientPackConfig = Field(default_factory=ClientPackConfig)
    platforms: ModPlatformMetadata = Field(default_factory=ModPlatformMetadata)

    model_config = ConfigDict(arbitrary_types_allowed=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_mod_fields(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        if "placement" not in payload:
            payload["placement"] = (
                ModPlacement.SERVER_DISABLED
                if payload.get("enabled") is False
                else ModPlacement.SERVER_ENABLED
            )
        if "mod_type" not in payload:
            legacy_coremod = payload.get("coremod")
            block_reason = payload.get("download_block_reason")
            if payload["placement"] in (ModPlacement.CLIENT_ONLY, ModPlacement.CLIENT_ONLY.value):
                payload["mod_type"] = ModType.CLIENT
            elif legacy_coremod is True:
                payload["mod_type"] = ModType.COREMOD
            elif block_reason in (ModDownloadBlockReason.BUILTIN, ModDownloadBlockReason.BUILTIN.value):
                payload["mod_type"] = ModType.BUILTIN
            else:
                payload["mod_type"] = ModType.REGULAR
        return payload

    @model_validator(mode="after")
    def sync_legacy_enabled_field(self) -> Mod_Config:
        self.enabled = self.placement.enabled
        mod_type = (
            self.mod_type
            if self.classification_override is None
            else self.classification_override.mod_type
        )
        self.client_pack.apply_default_inclusion(mod_type)
        return self

    def set_placement(self, placement: ModPlacement) -> None:
        self.placement = placement
        self.enabled = placement.enabled

    @property
    def coremod(self) -> bool:
        effective_mod_type = (
            self.mod_type if self.classification_override is None else self.classification_override.mod_type
        )
        return effective_mod_type is ModType.COREMOD

    @field_validator("directory", "client_path", mode="before")
    @classmethod
    def resolve_mod_paths(cls, raw: str | Path | None, info) -> Path | None:
        return resolve_config_path(raw, directory=info.data.get("directory", ""))


# AiviA APasz
