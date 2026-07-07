import asyncio
import contextlib
import json
import logging
import re
import shutil
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from re import Match, Pattern
from typing import Generic, TypeAlias, TypeVar, cast
from urllib.parse import quote

import aiohttp
import hikari
from modmux import Muxer, parse_url
from modmux.models import ModID, Provider
from modmux.modmux_errors import ModMuxError

import config
from _discord import (
    App_Bound,
    DC_Bound,
    DC_Relay,
    OutboundRelayFormatter,
    RelayEmbedPayload,
    RelayOutboundFormatOptions,
    render_plain_reference_prefix,
)
from _file import File_Utils
from _security import Power_Level, _owner_group
from apps._app import AM_Receiver, App, RelayAdvancementTerms
from apps._config import (
    App_Config,
    AppVersion,
    FactorioUpdateBranch,
    FactorioUpdateConfig,
    KnownModPageProvider,
    Mod_Config,
    ModPageLink,
    known_mod_page_provider_for_url,
)
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._mod import Mod, humanise_mod_identifier
from apps._rcon import RconClient
from apps._save_files import (
    AppSaveEntry,
    AppSaveRoot,
    AppSaveRootMode,
    describe_app_save_path,
    get_app_save_root,
    normalise_app_save_relative_path,
)
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Setting_Label,
    SettingSpec,
    StringSettingSpec,
)
from apps._tailer import Tailer
from apps._updater import (
    AppUpdateBranchState,
    AppUpdateInfo,
    AppUpdateOperationKind,
    AppUpdateOperationResult,
    AppUpdateProviderKind,
    AppUpdateState,
    AppUpdateStatus,
    Update_Manager,
)
from config import Activity_Manager
from relay_notices import (
    GameDeathKind,
    GameDeathNotice,
    GameProgressKind,
    GameProgressNotice,
    PlayerSessionAction,
    RelayNoticeSource,
    notice_embed_spec,
    render_notice_text,
)

log = logging.getLogger(__name__)

_FACTORIO_VERSION_RE: Pattern[str] = re.compile(r"Factorio (?P<version>\d+\.\d+\.\d+)")
_FACTORIO_INFO_JSON_NAME = "info.json"
_FACTORIO_IGNORED_MOD_FILES: frozenset[str] = frozenset({"mod-list.json", "mod-settings.dat"})
_FACTORIO_MOD_VERSION_RE: Pattern[str] = re.compile(
    r"_(?P<version>v?\d+(?:\.\d+)+(?:[-+._][A-Za-z0-9]+)*)$",
    re.IGNORECASE,
)
_FACTORIO_RESEARCH_FINISHED_RE: Pattern[str] = re.compile(r"\[RESEARCH FINISHED\]\s+(?P<research>.+)$", re.IGNORECASE)
_FACTORIO_ERROR_RE: Pattern[str] = re.compile(r"^\s*\d+\.\d+\s+Error\s+(?P<source>\S+:\d+):\s+(?P<message>.+)$")
_FACTORIO_LINE_MATCHER = Callable[[str], Awaitable[None]]
_FactorioSettingValue = TypeVar("_FactorioSettingValue", str, bool, int)
_FACTORIO_UPDATE_BRANCHES: tuple[FactorioUpdateBranch, ...] = (
    FactorioUpdateBranch.STABLE,
    FactorioUpdateBranch.EXPERIMENTAL,
)


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    mapping = cast(Mapping[object, object], value)
    result: dict[str, object] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings.")
        result[key] = item
    return result


def _load_json_object(raw: str | bytes, *, label: str) -> dict[str, object]:
    return _json_object(cast(object, json.loads(raw)), label=label)


def _optional_factorio_metadata_text(
    payload: Mapping[str, object],
    field_name: str,
    *,
    label: str,
) -> str | None:
    raw_value = payload.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError(f"{label} {field_name} must be a string.")
    value = raw_value.strip()
    return value or None


@dataclass(frozen=True, slots=True)
class FactorioModMetadata:
    name: str | None = None
    version: str | None = None
    title: str | None = None
    homepage: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, label: str) -> "FactorioModMetadata":
        return cls(
            name=_optional_factorio_metadata_text(payload, "name", label=label),
            version=_optional_factorio_metadata_text(payload, "version", label=label),
            title=_optional_factorio_metadata_text(payload, "title", label=label),
            homepage=_optional_factorio_metadata_text(payload, "homepage", label=label),
        )


def detect_factorio_version(*, directory: Path) -> AppVersion | None:
    log_file: Path = directory / "factorio-current.log"
    if not log_file.exists():
        return None
    try:
        for line in log_file.read_text(config.STR_ENCODE, errors="ignore").splitlines():
            if match := _FACTORIO_VERSION_RE.search(line):
                return AppVersion(main=match.group("version").strip())
    except OSError as xcp:
        log.warning("Failed to inspect Factorio log %s: %s", log_file, xcp)
    return None


def _parse_factorio_version_text(version_text: str, *, label: str) -> tuple[int, ...]:
    text: str = version_text.strip()
    if not text:
        raise ValueError(f"{label} must not be empty.")
    parts: list[str] = text.split(".")
    if not all(part.isdecimal() for part in parts):
        raise ValueError(f"{label} is invalid: {version_text!r}")
    return tuple[int, ...](map(int, parts))


def _factorio_latest_headless_versions(payload: Mapping[str, object]) -> dict[FactorioUpdateBranch, tuple[int, ...]]:
    versions: dict[FactorioUpdateBranch, tuple[int, ...]] = {}
    for branch in _FACTORIO_UPDATE_BRANCHES:
        branch_payload: dict[str, object] = _json_object(
            payload.get(branch.value), label=f"Factorio latest releases.{branch.value}"
        )
        raw_version: object | None = branch_payload.get("headless")
        if not isinstance(raw_version, str):
            raise ValueError(f"Factorio latest releases {branch.value}.headless version is invalid.")
        versions[branch] = _parse_factorio_version_text(
            raw_version,
            label=f"Factorio latest releases {branch.value}.headless version",
        )
    return versions


def _factorio_download_url(branch: FactorioUpdateBranch) -> str:
    if branch is FactorioUpdateBranch.STABLE:
        return "https://factorio.com/get-download/stable/headless/linux64"
    return "https://factorio.com/get-download/experimental/headless/linux64"


def _normalise_factorio_archive_member_path(member_name: str) -> Path:
    pure_path: PurePosixPath = PurePosixPath(member_name)
    if not member_name or pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError(f"Factorio archive member path is invalid: {member_name}")
    return Path(*pure_path.parts)


def _factorio_download_archive_path(*, tmp_dir: Path, branch: FactorioUpdateBranch, version_text: str) -> Path:
    return tmp_dir / f"factorio-{branch.value}-{version_text}.tar.xz"


def _ensure_factorio_binary_executable(binary_path: Path) -> None:
    if not binary_path.is_file():
        raise FileNotFoundError(f"Factorio binary does not exist: {binary_path}")
    current_mode = binary_path.stat().st_mode
    if current_mode & 0o111:
        return
    binary_path.chmod(current_mode | 0o111)
    log.warning("Restored execute permissions for Factorio binary: %s", binary_path)


@dataclass(frozen=True, slots=True)
class FactorioModListEntry:
    name: str
    enabled: bool

    @classmethod
    def from_mapping(cls, payload: object) -> "FactorioModListEntry":
        entry: dict[str, object] = _json_object(payload, label="Factorio mod-list entry")
        name: object | None = entry.get("name")
        enabled: object | None = entry.get("enabled")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Factorio mod-list entry name is invalid.")
        if not isinstance(enabled, bool):
            raise ValueError("Factorio mod-list entry enabled state is invalid.")
        return cls(name=name.strip(), enabled=enabled)

    def to_mapping(self) -> dict[str, object]:
        return {"name": self.name, "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class FactorioModList:
    mods: tuple[FactorioModListEntry, ...]

    @classmethod
    def load(cls, pointer: Path) -> "FactorioModList":
        if not pointer.exists():
            return cls(mods=())
        payload = _load_json_object(pointer.read_text(config.STR_ENCODE), label="Factorio mod-list")
        raw_mods = payload.get("mods")
        if not isinstance(raw_mods, list):
            raise ValueError("Factorio mod-list mods are invalid.")
        return cls(mods=tuple(FactorioModListEntry.from_mapping(item) for item in cast(list[object], raw_mods)))

    def state_for(self, mod_id: str) -> bool | None:
        for entry in self.mods:
            if entry.name == mod_id:
                return entry.enabled
        return None

    def set_enabled(self, mod_id: str, enabled: bool) -> "FactorioModList":
        updated = list(self.mods)
        for index, entry in enumerate(updated):
            if entry.name != mod_id:
                continue
            updated[index] = FactorioModListEntry(name=mod_id, enabled=enabled)
            break
        else:
            updated.append(FactorioModListEntry(name=mod_id, enabled=enabled))
        return FactorioModList(mods=tuple(updated))

    def remove(self, mod_id: str) -> "FactorioModList":
        return FactorioModList(mods=tuple(entry for entry in self.mods if entry.name != mod_id))

    def save(self, pointer: Path) -> None:
        payload = {"mods": [entry.to_mapping() for entry in self.mods]}
        pointer.write_text(json.dumps(payload, indent=4) + "\n", config.STR_ENCODE)


def _factorio_mod_metadata_from_archive(pointer: Path) -> FactorioModMetadata | None:
    if not pointer.exists():
        return None
    if pointer.is_dir():
        info_pointer = pointer / _FACTORIO_INFO_JSON_NAME
        if not info_pointer.exists():
            return None
        label = f"Factorio info.json {info_pointer}"
        payload = _load_json_object(
            info_pointer.read_text(config.STR_ENCODE),
            label=label,
        )
        return FactorioModMetadata.from_mapping(payload, label=label)

    if pointer.suffix.lower() != ".zip":
        return None

    with zipfile.ZipFile(pointer, "r") as archive:
        for member_name in archive.namelist():
            normalised = member_name.rstrip("/")
            if normalised == _FACTORIO_INFO_JSON_NAME or normalised.endswith(f"/{_FACTORIO_INFO_JSON_NAME}"):
                label = f"Factorio archive info.json {pointer}"
                payload = _load_json_object(
                    archive.read(member_name),
                    label=label,
                )
                return FactorioModMetadata.from_mapping(payload, label=label)
    return None


def _factorio_mod_name_from_path(pointer: Path) -> str:
    metadata = _factorio_mod_metadata_from_archive(pointer)
    if metadata is not None and metadata.name is not None:
        return metadata.name
    stem = pointer.stem if pointer.is_file() else pointer.name
    if "_" not in stem:
        return stem
    return stem.rsplit("_", 1)[0]


def _factorio_mod_version_from_name(name: str) -> str | None:
    match = _FACTORIO_MOD_VERSION_RE.search(Path(name).stem)
    if match is None:
        return None
    return match.group("version").removeprefix("v")


def _factorio_mod_page(raw_url: str | None) -> ModPageLink | None:
    if raw_url is None:
        return None
    mod_id = parse_url(raw_url)
    if mod_id is None or mod_id.provider is not Provider.WUBE:
        return None
    canonical_url = f"https://mods.factorio.com/mod/{quote(mod_id.id, safe='')}"
    return ModPageLink(name=KnownModPageProvider.FACTORIO_MODS.value, url=canonical_url)


async def _find_factorio_mod_page_via_modmux(*, mod_id: str, muxer: Muxer) -> ModPageLink:
    resolved_mod = await muxer.get_mod(
        Provider.WUBE,
        ModID(provider=Provider.WUBE, id=mod_id),
        author_resolution=False,
    )
    resolved_id = resolved_mod.slug or resolved_mod.id.id
    page = _factorio_mod_page(f"https://mods.factorio.com/mod/{resolved_id}")
    if page is None:
        raise ValueError(f"modmux returned an invalid Factorio mod ID: {resolved_id!r}")
    return page


class Mod_Factorio(Mod):
    def __init__(self, cfg: Mod_Config):
        self._detected_metadata = FactorioModMetadata()
        super().__init__(cfg)

    @classmethod
    def iter_candidates(cls, folder: Path) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for pointer in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if pointer.name in _FACTORIO_IGNORED_MOD_FILES:
                continue
            if pointer.is_dir():
                if (pointer / _FACTORIO_INFO_JSON_NAME).exists():
                    candidates.append(pointer)
                continue
            if pointer.is_file() and pointer.suffix.lower() == ".zip":
                candidates.append(pointer)
        return tuple(candidates)

    @property
    def disabled_path(self) -> Path:
        return self.enabled_path

    @property
    def mod_list_path(self) -> Path:
        return self.directory / "mod-list.json"

    @property
    def factorio_mod_id(self) -> str:
        return self._detected_metadata.name or _factorio_mod_name_from_path(self.path)

    def exists(self) -> bool:
        if self.name in _FACTORIO_IGNORED_MOD_FILES:
            return False
        return self.enabled_path.is_dir() or (
            self.enabled_path.is_file() and self.enabled_path.suffix.lower() == ".zip"
        )

    def _load_mod_list(self) -> FactorioModList:
        return FactorioModList.load(self.mod_list_path)

    def _save_mod_list(self, mod_list: FactorioModList) -> None:
        mod_list.save(self.mod_list_path)

    def sync_enabled_state(self) -> None:
        mod_state = self._load_mod_list().state_for(self.factorio_mod_id)
        if mod_state is not None:
            self.cfg.enabled = mod_state

    def detect_version(self) -> str | None:
        return self._detected_metadata.version or _factorio_mod_version_from_name(self.name)

    def detect_friendly(self) -> str | None:
        if self._detected_metadata.title is not None:
            return self._detected_metadata.title
        return humanise_mod_identifier(self.factorio_mod_id, split_single_camel=True)

    def native_metadata_id(self) -> str:
        return self.factorio_mod_id

    def metadata_fallback_id(self) -> str:
        return self.factorio_mod_id.casefold()

    def detect_mod_page(self) -> ModPageLink | None:
        return _factorio_mod_page(self._detected_metadata.homepage)

    def _has_factorio_mod_page(self) -> bool:
        return any(
            known_mod_page_provider_for_url(page.url) is KnownModPageProvider.FACTORIO_MODS
            for page in self.cfg.mod_pages
        )

    def _add_factorio_mod_page(self, page: ModPageLink) -> None:
        if not self._has_factorio_mod_page():
            self.cfg.mod_pages = (*self.cfg.mod_pages, page)

    def sync_metadata(self) -> None:
        self._detected_metadata = _factorio_mod_metadata_from_archive(self.path) or FactorioModMetadata()
        super().sync_metadata()
        detected_page = self.detect_mod_page()
        if detected_page is not None:
            self._add_factorio_mod_page(detected_page)

    @classmethod
    async def sync_external_metadata_batch(cls, mods: Iterable[Mod]) -> None:
        factorio_mods = tuple(
            mod for mod in mods if isinstance(mod, cls) and not mod._has_factorio_mod_page()
        )
        if not factorio_mods:
            return

        async with Muxer() as muxer:
            async def resolve_page(mod: "Mod_Factorio") -> None:
                try:
                    page = await _find_factorio_mod_page_via_modmux(mod_id=mod.factorio_mod_id, muxer=muxer)
                except (ModMuxError, ValueError) as xcp:
                    log.warning("Factorio mod page lookup failed for %s: %s", mod.name, xcp)
                    return
                mod._add_factorio_mod_page(page)

            await asyncio.gather(*(resolve_page(mod) for mod in factorio_mods))

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)
        await asyncio.to_thread(self._set_mod_list_enabled, True)

    async def uninstall(self, override_coremod: bool = False) -> bool:
        removed = await super().uninstall(override_coremod)
        await asyncio.to_thread(self._remove_mod_list_entry)
        return removed

    async def _enable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await asyncio.to_thread(self._set_mod_list_enabled, True)
        self.cfg.enabled = True
        return self.path

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await asyncio.to_thread(self._set_mod_list_enabled, False)
        self.cfg.enabled = False
        return self.path

    def _set_mod_list_enabled(self, enabled: bool) -> None:
        mod_list = self._load_mod_list()
        self._save_mod_list(mod_list.set_enabled(self.factorio_mod_id, enabled))

    def _remove_mod_list_entry(self) -> None:
        mod_list = self._load_mod_list()
        self._save_mod_list(mod_list.remove(self.factorio_mod_id))


@dataclass(frozen=True, slots=True)
class FactorioSettingDefinition(Generic[_FactorioSettingValue]):
    label: str | Setting_Label
    key: str
    path: tuple[str, ...]
    spec: SettingSpec[_FactorioSettingValue]
    default: _FactorioSettingValue
    power_level: Power_Level = Power_Level.admin
    desc: str | None = None
    paragraph: bool = False
    comment_key: str | None = None
    prefer_comment_desc: bool = False

    def create_setting(self) -> Setting[_FactorioSettingValue]:
        return Setting(
            self.spec,
            self.label,
            self.key,
            self.path,
            default=self.default,
            power_level=self.power_level,
            desc=self.desc,
            paragraph=self.paragraph,
        )

    @property
    def comment_lookup_key(self) -> str:
        if self.comment_key is not None:
            return self.comment_key
        return self.path[0] if self.path else self.key


def _normalise_factorio_comment(raw_comment: object) -> str | None:
    if isinstance(raw_comment, str):
        text: str = raw_comment.strip()
        return text or None
    if isinstance(raw_comment, list):
        lines: list[str] = []
        for item in cast(list[object], raw_comment):
            text = str(item).strip()
            if text:
                lines.append(text)
        if lines:
            return " ".join(lines)
    return None


_FACTORIO_PUBLIC_VISIBILITY_CHOICES: ChoiceSpec = ChoiceSpec(
    ChoiceOption("true", "Public"),
    ChoiceOption("false", "Private"),
)
_FactorioSettingDefinitionItem: TypeAlias = (
    FactorioSettingDefinition[str] | FactorioSettingDefinition[bool] | FactorioSettingDefinition[int]
)

_FACTORIO_SETTING_DEFINITIONS: tuple[_FactorioSettingDefinitionItem, ...] = (
    FactorioSettingDefinition[str](
        Setting_Label.serv_name,
        "name",
        (),
        StringSettingSpec(allow_blank=True),
        "",
    ),
    FactorioSettingDefinition[str](
        Setting_Label.serv_desc,
        "description",
        (),
        StringSettingSpec(allow_blank=True),
        "",
        paragraph=True,
    ),
    FactorioSettingDefinition[int](
        Setting_Label.max_player,
        "max_players",
        (),
        IntSettingSpec(min_value=0),
        0,
        power_level=Power_Level.sudo,
    ),
    FactorioSettingDefinition[bool](
        Setting_Label.visibility,
        "public",
        ("visibility",),
        BoolSettingSpec(_FACTORIO_PUBLIC_VISIBILITY_CHOICES),
        False,
        comment_key="visibility",
    ),
    FactorioSettingDefinition[str](
        Setting_Label.password,
        "game_password",
        (),
        StringSettingSpec(
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.user,
        ),
        "",
        power_level=Power_Level.sudo,
    ),
    FactorioSettingDefinition[bool](
        "Require User Verification",
        "require_user_verification",
        (),
        BoolSettingSpec(),
        False,
    ),
    FactorioSettingDefinition[int](
        "Max Upload (KiB/s)",
        "max_upload_in_kilobytes_per_second",
        (),
        IntSettingSpec(),
        0,
    ),
    FactorioSettingDefinition[int](
        "Max Upload Slots",
        "max_upload_slots",
        (),
        IntSettingSpec(),
        5,
    ),
    FactorioSettingDefinition[int](
        "Minimum Latency Ticks",
        "minimum_latency_in_ticks",
        (),
        IntSettingSpec(),
        0,
    ),
    FactorioSettingDefinition[int](
        "Max Heartbeats / Second",
        "max_heartbeats_per_second",
        (),
        IntSettingSpec(
            min_value=6,
            max_value=240,
        ),
        60,
    ),
    FactorioSettingDefinition[int](
        "Autosave Interval",
        "autosave_interval",
        (),
        IntSettingSpec(),
        10,
    ),
    FactorioSettingDefinition[bool](
        "Ignore Returning Player Limit",
        "ignore_player_limit_for_returning_players",
        (),
        BoolSettingSpec(),
        False,
    ),
    FactorioSettingDefinition[int](
        "AFK Autokick Interval",
        "afk_autokick_interval",
        (),
        IntSettingSpec(),
        0,
    ),
    FactorioSettingDefinition[bool](
        "Auto Pause",
        "auto_pause",
        (),
        BoolSettingSpec(),
        True,
    ),
    FactorioSettingDefinition[bool](
        "Pause On Connect",
        "auto_pause_when_players_connect",
        (),
        BoolSettingSpec(),
        False,
    ),
    FactorioSettingDefinition[bool](
        "Admins Pause Only",
        "only_admins_can_pause_the_game",
        (),
        BoolSettingSpec(),
        True,
    ),
    FactorioSettingDefinition[bool](
        "Autosave Server Only",
        "autosave_only_on_server",
        (),
        BoolSettingSpec(),
        True,
    ),
    FactorioSettingDefinition[str](
        "Factorio Username",
        "username",
        (),
        StringSettingSpec(
            allow_blank=True,
            do_hide=Power_Level.admin,
        ),
        "",
        power_level=Power_Level.root,
        desc="Factorio account username used for mod portal authentication.",
        comment_key="credentials",
    ),
    FactorioSettingDefinition[str](
        "Factorio Password",
        "password",
        (),
        StringSettingSpec(
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.root,
        ),
        "",
        power_level=Power_Level.root,
        desc="Factorio account password used for mod portal authentication.",
        comment_key="credentials",
        prefer_comment_desc=True,
    ),
    FactorioSettingDefinition[str](
        "Factorio Token",
        "token",
        (),
        StringSettingSpec(
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.root,
        ),
        "",
        power_level=Power_Level.root,
    ),
    FactorioSettingDefinition[bool](
        "Non Blocking Saving",
        "non_blocking_saving",
        (),
        BoolSettingSpec(),
        False,
        power_level=Power_Level.root,
        desc="On UNIX systems, server will fork itself to create an autosave.",
    ),
)


class Factorio_Settings(App_Settings):
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
        self._definitions: tuple[_FactorioSettingDefinitionItem, ...] = _FACTORIO_SETTING_DEFINITIONS
        super().__init__(
            pointer, [definition.create_setting() for definition in self._definitions], version_getter=version_getter
        )

    def _apply_descriptions(self, data: dict[str, object]) -> None:
        for definition in self._definitions:
            setting: Setting[object] | None = self.get_setting(definition.key)
            if setting is None:
                raise ValueError(f"Missing Factorio setting definition for {definition.key}")
            comment_desc = _normalise_factorio_comment(data.get(f"_comment_{definition.comment_lookup_key}"))
            if definition.prefer_comment_desc and comment_desc is not None:
                setting.desc = comment_desc
                continue
            if definition.desc is not None:
                setting.desc = definition.desc
                continue
            setting.desc = comment_desc

    def load(self) -> None:
        data = _load_json_object(self.pointer.read_text(config.STR_ENCODE), label="Factorio server settings")
        self._apply_descriptions(data)
        for opt in self.options:
            opt.get(data)

    def save(self) -> dict[str, object]:
        data = _load_json_object(self.pointer.read_text(config.STR_ENCODE), label="Factorio server settings")
        for opt in self.options:
            opt.set(data)

        string: str = json.dumps(data, indent=4)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class Factorio(App[App_Config]):
    _instance = None
    chat_relay_outbound = True
    relay_advancement_terms = RelayAdvancementTerms("Research", "Research")
    relay_notice_player_session_supported = True
    relay_notice_player_death_supported = True
    relay_notice_progress_supported = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0xDC6B0F
        self.proc_name = "factorio"
        self.proc_cmd = [self.proc_name, "--start-server"]
        file_settings: Path = cfg.directory.absolute() / "data" / "server-settings.json"
        self.cmd_start = cfg.cmd_start or [
            "bin/x64/factorio",
            "--start-server-load-latest",
            "--server-settings",
            f"{file_settings}",
            "--rcon-port",
            "27015",
            "--rcon-password",
            f"{config.env_req('APP_COMM_PASS')}",
        ]

        self.process = None
        super().__init__(
            bot, am, cfg, Factorio_Settings(file_settings, version_getter=lambda: cfg.version), Mod_Factorio
        )
        self.act_err_threshold = 100
        self._lock: Path = self.directory / ".lock"

        self.updater = Factorio_Updater(self, base=True)
        self.apply_version(detect_factorio_version(directory=cfg.directory), persist=False)

        self._relay: RconClient = RconClient(self.check_running, 27015)
        self._tail: Tailer | None = None
        self._tail_machers: set[_FACTORIO_LINE_MATCHER] = set()
        self._startup_error: str | None = None
        self._players: Players = Players(self)
        self.am_receiver = Receiver(self)
        self._matchers: Matchers = Matchers(self)

        try:
            settings = _load_json_object(
                file_settings.read_text(config.STR_ENCODE),
                label="Factorio server settings",
            )
            serv_name = settings.get("name")
            if serv_name is not None:
                if not isinstance(serv_name, str):
                    raise ValueError("Factorio server setting `name` must be a string.")
                if serv_name.strip():
                    self.cfg.provider_alt_text = serv_name.strip()
        except Exception:
            log.exception(f"{__name__} Read Settings")

        log.debug(f"{__name__}.Created")

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Settings",
                path=self.directory / "data" / "server-settings.json",
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".json"}),
            ),
        )

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        return (
            AppSaveRoot(
                id="saves",
                label="Saves",
                path=self.directory / "saves",
                mode=AppSaveRootMode.CHILDREN,
                recursive=False,
                suffixes=frozenset[str]({".zip"}),
                include_files=True,
                include_directories=False,
            ),
        )

    @property
    def supports_save_uploads(self) -> bool:
        return True

    @property
    def supports_save_rename(self) -> bool:
        return True

    @property
    def supports_save_delete(self) -> bool:
        return True

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        root = get_app_save_root(self.save_file_roots, root_id)
        relative_path = normalise_app_save_relative_path(upload_name)
        if Path(relative_path).name != relative_path:
            raise ValueError("Factorio save upload name must not include directories.")
        if Path(relative_path).suffix.casefold() != ".zip":
            raise ValueError("Factorio save uploads must be .zip files.")
        target_dir = root.resolved_path
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / relative_path
        File_Utils.copy(source_path, target, overwrite=True)
        return describe_app_save_path(root=root, path=target, relative_path=relative_path)

    def relocate_save_file(
        self,
        *,
        save_id: str,
        destination_root_id: str,
        destination_relative_path: str,
    ) -> AppSaveEntry:
        source_root = get_app_save_root(self.save_file_roots, "saves")
        if destination_root_id != source_root.id:
            raise ValueError("Factorio saves can only be renamed within the saves root.")
        relative_path = normalise_app_save_relative_path(destination_relative_path)
        if Path(relative_path).name != relative_path:
            raise ValueError("Factorio save name must not include directories.")
        if Path(relative_path).suffix.casefold() != ".zip":
            raise ValueError("Factorio save names must use the .zip suffix.")
        source = self.resolve_save_file(save_id)
        if not source.exists():
            raise FileNotFoundError(f"Save file does not exist: {Path(save_id).name}")
        target = source_root.resolved_path / relative_path
        if target == source:
            return describe_app_save_path(root=source_root, path=source, relative_path=relative_path)
        if target.exists():
            raise FileExistsError(f"Save file already exists: {relative_path}")
        File_Utils.move(source, target, overwrite=False)
        return describe_app_save_path(root=source_root, path=target, relative_path=relative_path)

    def delete_save_file(self, *, file_id: str) -> AppSaveEntry:
        if self.check_running():
            raise ValueError("Stop the server before deleting saves.")
        source_root = get_app_save_root(self.save_file_roots, "saves")
        try:
            current_save = next(save for save in self.list_save_files() if save.id == file_id)
        except StopIteration as xcp:
            raise FileNotFoundError(f"Unknown save file: {file_id}") from xcp
        save_path = self.resolve_save_file(file_id)
        if not save_path.exists():
            raise FileNotFoundError(f"Save file does not exist: {Path(file_id).name}")
        if save_path.parent.resolve() != source_root.resolved_path:
            raise ValueError("Factorio save deletion only supports files directly inside the saves root.")
        File_Utils.remove(save_path, silent=False, resolve=False)
        return current_save

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        _ensure_factorio_binary_executable(self.directory / "bin" / "x64" / "factorio")
        self._startup_error = None

        for item in (self.directory / "saves").iterdir():
            if item.is_dir():
                continue
            if not item.name.endswith("tmp.zip"):
                continue
            File_Utils.remove(item, silent=True, resolve=True)

        wait_count = 10
        while self._lock.exists() and wait_count >= 0:
            wait_count -= 1
            await asyncio.sleep(1)

        await self._std_launch()

        if self.server_log and self.server_log.exists():
            File_Utils.link(self.server_log, self.file_stdout.with_name(self.server_log.name))

        if self.process and self.process.stdout:
            log.debug(f"{self.name} Tailing: Process")
            self._tail = Tailer(self._startup_tail_ready, self.process.stdout, self.file_stdout)
        elif self.server_log:
            log.debug(f"{self.name} Tailing: server log")
            self._tail = Tailer(self._startup_tail_ready, self.server_log, self.file_stdout)
        else:
            raise SystemError("No Log to be passed to Tailer")
        await self._tail.start(self._tail_machers)

        await self._wait_for_startup_ready()

        await self._players.start()

        self._running = True
        return True

    async def _wait_for_startup_ready(self) -> None:
        setup_task = asyncio.create_task(self._relay.setup())
        try:
            while not setup_task.done():
                if self._startup_error is not None:
                    raise RuntimeError(f"Factorio startup failed: {self._startup_error}")
                if not self.check_running():
                    await self._drain_stderr_task()
                    detail = self._startup_error or "process exited before RCON became available"
                    raise RuntimeError(f"Factorio startup failed: {detail}")
                await asyncio.sleep(0.2)
            await setup_task
        except Exception:
            if not setup_task.done():
                setup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await setup_task
            if self._tail is not None:
                await self._tail.stop()
                self._tail = None
            raise

    def _startup_tail_ready(self) -> bool:
        return self.process is not None

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._players.stop()
        ok: str | None = await self._relay.send("/server-save", reconnect_on_failure=False)
        if not ok:
            if self.process and self.process.stdin:
                log.debug("Falling back to stdin")
                self.process.stdin.write("/server-save")
                self.process.stdin.flush()
        if self._tail:
            await self._tail.stop()
        await self._relay.teardown()
        await self._terminate()
        await asyncio.sleep(0.5)
        File_Utils.remove(self._lock, silent=True, resolve=True)  # Sometimes it doesn't get removed
        return True

    async def kill(self) -> bool:
        self._running = False
        await self._players.stop()
        if self._tail:
            await self._tail.stop()
        await self._relay.teardown()
        await self._terminate()
        await asyncio.sleep(0.5)
        File_Utils.remove(self._lock, silent=True, resolve=True)
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()

    def connected_player_names(self) -> tuple[str, ...]:
        return self._players.connected_player_names()

    def detect_installed_version(self) -> AppVersion | None:
        return detect_factorio_version(directory=self.directory)


class Factorio_Updater(Update_Manager):
    def __init__(self, app: Factorio, *, base: bool = False, mods: bool = False) -> None:
        super().__init__(app, base=base, mods=mods)
        self.version: tuple[int, ...] | None = None
        app_version: AppVersion | None = detect_factorio_version(directory=app.directory)
        if app_version is not None:
            self.version = _parse_factorio_version_text(app_version.main, label="local Factorio version")
        if self.version:
            log.info(f"Factorio local version: {self.stringise(self.version)}")
        else:
            log.warning(f"Could not determine Factorio version: {app.directory / 'factorio-current.log'}")
        self._state_lock = threading.Lock()
        self._status: AppUpdateStatus = AppUpdateStatus(
            state=AppUpdateState.IDLE,
            summary="Ready",
        )
        self._log_tail: deque[str] = deque(maxlen=80)
        self._operation_running: bool = False
        self._active_task: asyncio.Task[AppUpdateOperationResult] | None = None

    @staticmethod
    def _unix_ms_now() -> int:
        return int(time.time() * 1000)

    def info(self) -> AppUpdateInfo:
        update_config = self._factorio_update_config()
        selected_branch = update_config.selected_branch
        installed_branch = update_config.installed_branch
        return AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.FACTORIO,
            provider_label="Factorio.com",
            selected_branch_id=selected_branch.value,
            selected_branch_label=selected_branch.display_label,
            branches=tuple(
                AppUpdateBranchState(
                    branch_id=branch.value,
                    label=branch.display_label,
                    selected=branch is selected_branch,
                )
                for branch in _FACTORIO_UPDATE_BRANCHES
            ),
            supports_verify=True,
            installed_branch_id=installed_branch.value if installed_branch is not None else None,
        )

    def select_branch(self, branch_id: str) -> AppUpdateInfo:
        if self.status().running:
            raise RuntimeError(f"Cannot change the Factorio branch while {self.app.friendly} is updating.")
        update_config = self._factorio_update_config()
        branch = update_config.branch(branch_id)
        if branch is update_config.selected_branch:
            return self.info()
        self.app.cfg.factorio_update = update_config.model_copy(update={"selected_branch": branch})
        self.app.persist_instance_config_overrides()
        log.info("Selected Factorio update branch for %s: %s", self.app.friendly, branch.value)
        return self.info()

    def status(self) -> AppUpdateStatus:
        with self._state_lock:
            return self._status

    async def start_selected_update(self) -> AppUpdateOperationResult:
        return self._start_selected_operation(AppUpdateOperationKind.UPDATE)

    async def start_selected_verify(self) -> AppUpdateOperationResult:
        return self._start_selected_operation(AppUpdateOperationKind.VERIFY)

    def _start_selected_operation(self, kind: AppUpdateOperationKind) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(kind)
        log.info("Starting Factorio %s task: app=%s branch=%s", kind.value, self.app.friendly, branch.value)
        task: asyncio.Task[AppUpdateOperationResult] = asyncio.create_task(
            self._run_started_operation(kind=kind, branch=branch)
        )
        with self._state_lock:
            self._active_task = task
        task.add_done_callback(self._log_background_task_outcome)
        return AppUpdateOperationResult(
            kind=kind,
            message=f"Started {kind.value} for {self.app.friendly} on Factorio branch {branch.display_label}.",
            selected_branch_id=branch.value,
            selected_branch_label=branch.display_label,
        )

    async def update_selected(self) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(AppUpdateOperationKind.UPDATE)
        return await self._run_started_operation(kind=AppUpdateOperationKind.UPDATE, branch=branch)

    async def verify_selected(self) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(AppUpdateOperationKind.VERIFY)
        return await self._run_started_operation(kind=AppUpdateOperationKind.VERIFY, branch=branch)

    async def base(self) -> str | None:
        result = await self.update_selected()
        return result.version_text

    def _begin_selected_operation(self, kind: AppUpdateOperationKind) -> FactorioUpdateBranch:
        if kind not in (AppUpdateOperationKind.UPDATE, AppUpdateOperationKind.VERIFY):
            raise ValueError(f"Unsupported Factorio update operation: {kind.value}")
        if self.app.check_running():
            raise RuntimeError(f"{self.app.friendly} must be stopped before {kind.value}.")
        branch = self._factorio_update_config().selected_branch
        started_at_unix_ms = self._unix_ms_now()
        with self._state_lock:
            if self._operation_running:
                raise RuntimeError(f"{self.app.friendly} already has an update operation in progress.")
            self._operation_running = True
            self._active_task = None
            self._log_tail.clear()
            self._status = AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary=f"Starting {kind.value}...",
                operation_kind=kind,
                progress_percent=0.0,
                started_at_unix_ms=started_at_unix_ms,
            )
        self._append_log(f"Selected branch: {branch.display_label} ({branch.value})")
        return branch

    async def _run_started_operation(
        self,
        *,
        kind: AppUpdateOperationKind,
        branch: FactorioUpdateBranch,
    ) -> AppUpdateOperationResult:
        try:
            self._update_running_status(
                summary=f"Checking latest {branch.display_label.lower()} release...",
                detail="Fetching Factorio release metadata.",
                progress_percent=5.0,
            )
            latest_versions = await self.fetch_latest_versions()
            latest = latest_versions[branch]
            latest_version_text = self.stringise(latest)
            if self.version is not None:
                if latest < self.version:
                    current_version_text = self.stringise(self.version)
                    raise RuntimeError(
                        f"Selected {branch.display_label.lower()} release {latest_version_text} is older than the "
                        f"installed version {current_version_text}; downgrades are not supported."
                    )
                if latest == self.version and kind is AppUpdateOperationKind.UPDATE:
                    message = f"No new {branch.display_label.lower()} update found for {self.app.friendly}."
                    self._finish_operation(
                        state=AppUpdateState.SUCCEEDED,
                        summary=message,
                        detail=f"{self.app.friendly} is already on {latest_version_text}.",
                        progress_percent=100.0,
                    )
                    return AppUpdateOperationResult(
                        kind=kind,
                        message=message,
                        version_text=latest_version_text,
                        selected_branch_id=branch.value,
                        selected_branch_label=branch.display_label,
                    )

            self._append_log(f"Latest {branch.display_label.lower()} version: {latest_version_text}")
            archive_path = await self.download_release(branch=branch, version_text=latest_version_text)
            try:
                self._update_running_status(
                    summary="Installing update...",
                    detail=f"Extracting {archive_path.name}.",
                    progress_percent=80.0,
                )
                await asyncio.to_thread(self._install_release_archive, archive_path)
            finally:
                File_Utils.remove(archive_path, silent=True, resolve=False)

            self.version = latest
            self.app.apply_version(AppVersion(main=latest_version_text), persist=False)
            update_config = self._factorio_update_config().model_copy(update={"installed_branch": branch})
            self.app.cfg.factorio_update = update_config
            self.app.persist_instance_config_overrides()

            if kind is AppUpdateOperationKind.VERIFY:
                message = (
                    f"Verified {self.app.friendly} on Factorio branch {branch.display_label} "
                    f"by reinstalling {latest_version_text}."
                )
            else:
                message = f"Updated {self.app.friendly} on Factorio branch {branch.display_label} to {latest_version_text}."
            self._finish_operation(
                state=AppUpdateState.SUCCEEDED,
                summary=message,
                detail="Factorio release installed successfully.",
                progress_percent=100.0,
            )
            return AppUpdateOperationResult(
                kind=kind,
                message=message,
                version_text=latest_version_text,
                selected_branch_id=branch.value,
                selected_branch_label=branch.display_label,
            )
        except Exception as xcp:
            self._finish_operation(
                state=AppUpdateState.FAILED,
                summary=f"{kind.value.title()} failed for {self.app.friendly}.",
                detail=str(xcp),
            )
            log.warning(
                "Factorio %s failed: app=%s branch=%s error=%s",
                kind.value,
                self.app.friendly,
                branch.value,
                xcp,
            )
            raise
        finally:
            with self._state_lock:
                self._operation_running = False
                self._active_task = None

    async def download_release(self, *, branch: FactorioUpdateBranch, version_text: str) -> Path:
        url = _factorio_download_url(branch)
        archive_path = _factorio_download_archive_path(
            tmp_dir=config.DIR_TMP,
            branch=branch,
            version_text=version_text,
        )
        self._append_log(f"Downloading {url}")
        timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Factorio download failed with HTTP {response.status}.")
                    total_bytes = response.content_length
                    downloaded_bytes = 0
                    with open(archive_path, "wb") as archive_file:
                        async for chunk in response.content.iter_chunked(262_144):
                            archive_file.write(chunk)
                            downloaded_bytes += len(chunk)
                            progress_percent = 40.0
                            detail = f"Downloaded {downloaded_bytes} bytes."
                            if total_bytes is not None and total_bytes > 0:
                                ratio = min(1.0, downloaded_bytes / total_bytes)
                                progress_percent = 15.0 + (ratio * 55.0)
                                detail = f"Downloaded {downloaded_bytes} of {total_bytes} bytes ({ratio * 100.0:.2f}%)."
                            self._update_running_status(
                                summary=f"Downloading {branch.display_label.lower()} release...",
                                detail=detail,
                                progress_percent=progress_percent,
                            )
        except Exception:
            File_Utils.remove(archive_path, silent=True, resolve=False)
            raise
        self._append_log(f"Downloaded archive: {archive_path.name}")
        return archive_path

    @staticmethod
    async def fetch_latest_versions() -> dict[FactorioUpdateBranch, tuple[int, ...]]:
        url = "https://factorio.com/api/latest-releases"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"Failed to fetch Factorio latest versions: HTTP {response.status}.")
                data = _json_object(cast(object, await response.json()), label="Factorio latest releases")
        return _factorio_latest_headless_versions(data)

    @staticmethod
    def _extract_archive_root(archive_path: Path, staging_dir: Path) -> Path:
        with tarfile.open(archive_path, mode="r:xz") as archive:
            for member in archive.getmembers():
                relative_path = _normalise_factorio_archive_member_path(member.name)
                target_path = staging_dir / relative_path
                if member.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    target_path.chmod(member.mode & 0o7777)
                    continue
                if member.issym() or member.islnk():
                    raise ValueError(f"Factorio archive symlinks are not supported: {member.name}")
                if not member.isfile():
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Factorio archive member could not be read: {member.name}")
                with source, open(target_path, "wb") as target:
                    shutil.copyfileobj(source, target)
                target_path.chmod(member.mode & 0o7777)
        extracted_root = staging_dir / "factorio"
        if not extracted_root.is_dir():
            raise FileNotFoundError("Factorio archive did not contain the expected root directory.")
        return extracted_root

    @staticmethod
    def _overlay_directory(source_root: Path, target_root: Path) -> None:
        for source in source_root.iterdir():
            target = target_root / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _install_release_archive(self, archive_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="yukibot-factorio-update-") as temp_dir:
            staging_dir = Path(temp_dir)
            extracted_root = self._extract_archive_root(archive_path, staging_dir)
            self._overlay_directory(extracted_root, self.app.directory)
        _ensure_factorio_binary_executable(self.app.directory / "bin" / "x64" / "factorio")
        self._apply_directory_ownership(self.app.directory)
        self._append_log(f"Installed archive into {self.app.directory}")

    async def mods(self) -> list[str] | None:
        await super().mods()

    def _factorio_update_config(self) -> FactorioUpdateConfig:
        update_config = self.app.cfg.factorio_update
        if update_config is None:
            return FactorioUpdateConfig()
        return update_config

    def _append_log(self, line: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self._state_lock:
            self._log_tail.append(clean_line)
            if self._status.state is AppUpdateState.RUNNING:
                self._status = replace(self._status, log_lines=tuple(self._log_tail))

    def _update_running_status(self, *, summary: str, detail: str | None, progress_percent: float | None) -> None:
        with self._state_lock:
            if self._status.state is not AppUpdateState.RUNNING:
                return
            self._status = replace(
                self._status,
                summary=summary,
                detail=detail,
                progress_percent=progress_percent,
                log_lines=tuple(self._log_tail),
            )

    def _finish_operation(
        self,
        *,
        state: AppUpdateState,
        summary: str,
        detail: str | None,
        progress_percent: float | None = None,
    ) -> None:
        finished_at_unix_ms = self._unix_ms_now()
        with self._state_lock:
            self._status = replace(
                self._status,
                state=state,
                summary=summary,
                detail=detail,
                progress_percent=progress_percent,
                log_lines=tuple(self._log_tail),
                finished_at_unix_ms=finished_at_unix_ms,
            )

    def _log_background_task_outcome(self, task: asyncio.Task[AppUpdateOperationResult]) -> None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            log.info("Factorio update task cancelled: app=%s", self.app.friendly)
        except Exception:
            log.exception("Factorio update task failed in background: app=%s", self.app.friendly)
        else:
            log.info(
                "Factorio update task completed: app=%s branch=%s version=%s",
                self.app.friendly,
                result.selected_branch_id,
                result.version_text,
            )

    @staticmethod
    def _apply_directory_ownership(root: Path) -> None:
        username, group_name = _owner_group()
        for pointer in (root, *root.rglob("*")):
            if pointer.is_symlink():
                continue
            shutil.chown(pointer, user=username, group=group_name)


class Receiver(AM_Receiver):
    def __init__(self, app: Factorio) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        base_content = payload.content_for_app(self.app) if hasattr(payload, "content_for_app") else payload.content
        content = OutboundRelayFormatter.format_payload(
            payload,
            RelayOutboundFormatOptions(
                base_content=base_content,
                reference_renderer=render_plain_reference_prefix,
            ),
        )
        txt: str = f'/silent-command game.print("{payload.alias}: {content}")'
        await self.app._relay.send(txt)


def _render_factorio_research_name(raw_name: str) -> str:
    return humanise_mod_identifier(raw_name.strip(), split_single_camel=True)


class Matchers:
    def __init__(self, app: Factorio):
        self.app = app
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_death)
        app._tail_machers.add(self.match_error)
        app._tail_machers.add(self.match_research)

    async def match_chat(self, line: str):
        match: Match[str] | None = re.search(r"\[CHAT\] (.*?): (.+)", line, re.IGNORECASE)
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Chat: {line=} | {match=}")
        player = None
        if match:
            player = str(match.group(1))
            msg = str(match.group(2))
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player))

    async def match_death(self, line: str):
        if self.app.relay_notice_player_death_enabled is False:
            return
        match: Match[str] | None = re.search(r"\[DIED\]\s+(\w+):(\S+)\s+(.+)", line, re.IGNORECASE)
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Death: {line=} | {match=}")
        player = None
        if match:
            mode, player, cause = match.groups()
            log.debug(f"Match_Death: {player=} | {cause=} | {mode=}")
            detail_text = str(cause).replace("-", " ").title() if cause else "died"
            if mode == "PVE":
                detail_text = f"died to {detail_text}" if cause else "died"
                death_kind = GameDeathKind.PVE
            elif mode == "PVP":
                detail_text = f"killed by {detail_text}" if cause else "killed by another player"
                death_kind = GameDeathKind.PVP
            else:
                death_kind = GameDeathKind.UNKNOWN
            notice = GameDeathNotice(
                death_kind=death_kind,
                detail_text=detail_text,
                source=RelayNoticeSource.APP_LOG,
            )
            app_friendly = getattr(self.app, "friendly", self.app.name)
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    render_notice_text(notice, author_name=player, app_name=app_friendly),
                    player,
                    notice=notice,
                )
            )

    async def match_error(self, line: str) -> None:
        if match := _FACTORIO_ERROR_RE.search(line):
            source = match.group("source").strip()
            message = match.group("message").strip()
            self.app._startup_error = f"{source}: {message}"
            log.warning("Factorio error detected for %s: %s", self.app.name, self.app._startup_error)

    async def match_research(self, line: str) -> None:
        if self.app.relay_notice_progress_enabled is False:
            return
        if match := _FACTORIO_RESEARCH_FINISHED_RE.search(line):
            research_name: str = _render_factorio_research_name(match.group("research"))
            research_label = self.app.relay_advancement_term
            app_friendly = getattr(self.app, "friendly", self.app.name)
            notice = GameProgressNotice(
                progress_kind=GameProgressKind.RESEARCH,
                label=research_label,
                title=research_name,
                source=RelayNoticeSource.APP_LOG,
            )
            embed_spec = notice_embed_spec(notice, app_name=app_friendly, author_name="System")
            relay_embed = (
                None
                if embed_spec is None
                else RelayEmbedPayload(
                    title=embed_spec.title,
                    description=embed_spec.description,
                    color=self.app.manage_embed_color,
                )
            )
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    f"{research_label}: {research_name}",
                    "System",
                    relay_embed=relay_embed,
                    notice=notice,
                )
            )


class Players:
    def __init__(self, app: "Factorio") -> None:
        self.app: Factorio = app
        self._players_task: asyncio.Task[None] | None = None
        self._players_task_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._online: int | None = None
        self._max: int | None = None
        self._players: set[str] = set[str]()

    async def start(self) -> None:
        self._online = None
        self._max = None
        self._players = set[str]()
        if self._players_task and not self._players_task.done():
            return
        self._running = True
        self._players_task_loop = asyncio.get_running_loop()
        self._players_task = asyncio.create_task(self._listplayers())

    async def stop(self) -> None:
        self._online = None
        self._max = None
        self._players = set[str]()
        self._running = False
        if self._players_task:
            task = self._players_task
            self._players_task = None
            task_loop = self._players_task_loop
            self._players_task_loop = None
            if task_loop is None or task.done():
                return
            current_loop = asyncio.get_running_loop()
            if task_loop is not current_loop:
                task_loop.call_soon_threadsafe(task.cancel)
                log.debug("Factorio player poll task cancelled without await because it belongs to a different loop.")
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _listplayers(self) -> None:
        while self._running:
            await asyncio.sleep(1)
            if not self._running or config.IS_SHUTTINGDOWN or not self.app.check_running():
                return
            log.debug(f"Players.PRE {self._online}/{self._max} | {self._players}")
            _max: str | None = await self.app._relay.send("/config get max-players")
            if not self._running:
                return
            if _max:
                self._max = int(_max) or -1
                log.debug(f"Players.{self._max=}")
            string: str | None = await self.app._relay.send("/players online")
            if not self._running:
                return
            if string:

                def find_players(x: str) -> tuple[int, set[str]]:
                    lines: list[str] = [line.strip() for line in x.split("\n") if line]
                    count: int = len(lines) - 1
                    players: set[str] = set[str](name.rsplit(" ", 1)[0] for name in lines[1:])
                    return count, players

                self._online, players = find_players(string)

                def is_join(new: set[str]) -> tuple[set[str], set[str]]:
                    join: set[str] = new.difference(self._players)
                    leave: set[str] = self._players.difference(new)
                    return join, leave

                joins, leaves = is_join(players)

                for player in leaves:
                    if self.app.relay_notice_player_left_enabled is not False:
                        notice = self.app.player_session_notice(
                            action=PlayerSessionAction.LEFT,
                            source=RelayNoticeSource.APP_POLL,
                        )
                        app_friendly = getattr(self.app, "friendly", self.app.name)
                        DC_Relay.add(
                            DC_Bound(
                                self.app,
                                render_notice_text(notice, author_name=player, app_name=app_friendly),
                                player,
                                notice=notice,
                            )
                        )
                    self._players.discard(player)
                    log.debug(f"Players.discard.{self._players=}")
                for player in joins:
                    if self.app.relay_notice_player_joined_enabled is not False:
                        notice = self.app.player_session_notice(
                            action=PlayerSessionAction.JOINED,
                            source=RelayNoticeSource.APP_POLL,
                        )
                        app_friendly = getattr(self.app, "friendly", self.app.name)
                        DC_Relay.add(
                            DC_Bound(
                                self.app,
                                render_notice_text(notice, author_name=player, app_name=app_friendly),
                                player,
                                notice=notice,
                            )
                        )
                    self._players.add(player)
                    log.debug(f"Players.add.{self._players=}")

    @staticmethod
    def extract_num(text: str) -> int | None:
        for part in text.split(" "):
            if part.strip().isnumeric():
                return int(part)
        return None

    async def count(self) -> tuple[int, int] | None:
        if not config.SILENT_DEBUG:
            log.debug(f"Player.count={self._online}/{self._max}")
        if self._online is not None and self._max is not None:
            return (self._online, self._max)
        return None

    def connected_player_names(self) -> tuple[str, ...]:
        return tuple[str, ...](sorted(self._players, key=str.casefold))


# AiviA APasz
