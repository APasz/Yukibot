import ast
import asyncio
import hashlib
import json
import logging
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit

import hikari

import config
from _async_utils import run_blocking
from _discord import (
    App_Bound,
    DC_Bound,
    DC_Relay,
    OutboundRelayFormatter,
    RelayOutboundFormatOptions,
    render_plain_reference_prefix,
)
from _file import File_Utils
from _security import Power_Level
from apps._app import AM_Receiver, App, AppActivityProvider, AppActivityProviderMetadata
from apps._config import (
    App_Config,
    AppVersion,
    KnownModPageProvider,
    Mod_Config,
    ModPageLink,
    ModPlacement,
    ModType,
    known_mod_page_provider_for_url,
)
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult, ConsoleResponseSource
from apps._mod import Mod
from apps._save_files import (
    AppSaveEntry,
    AppSaveRoot,
    AppSaveRootMode,
    describe_app_save_path,
    get_app_save_root,
)
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    DraftSettingValue,
    IntSettingSpec,
    Setting,
    Setting_Label,
    StringSettingSpec,
)
from apps._tailer import Tailer
from apps._telnet import TelnetClient
from apps._updater import SteamCmd_Update_Manager
from config import Activity_Manager
from relay_notices import (
    GameDeathKind,
    GameDeathNotice,
    PlayerSessionAction,
    PlayerSessionNotice,
    RelayNoticeSource,
    render_notice_text,
)

log = logging.getLogger(__name__)

type GameStatValue = int | float | str | bool | None
type SevenDaysRuntimeLogSignature = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class SevenDaysSaveArchiveInspection:
    content_prefix: tuple[str, ...] | None
    game_world: str | None
    game_name: str | None
    generated_world_prefix: tuple[str, ...] | None
    generated_world: str | None
    file_count: int

    @property
    def missing_game_world(self) -> bool:
        return self.game_world is None

    @property
    def missing_game_name(self) -> bool:
        return self.game_name is None

    @property
    def includes_generated_world(self) -> bool:
        return self.generated_world_prefix is not None

    @property
    def includes_save(self) -> bool:
        return self.content_prefix is not None


@dataclass(frozen=True, slots=True)
class SevenDaysWorldSelection:
    game_world: str | None
    game_name: str | None

    @property
    def requires_fresh_save_name(self) -> bool:
        return self.game_name is None


class SevenDaysUploadKind(StrEnum):
    WORLD = "world"
    SAVE = "save"


@dataclass(frozen=True, slots=True)
class SevenDaysUploadTarget:
    kind: SevenDaysUploadKind
    root: AppSaveRoot
    save_root: AppSaveRoot | None = None

_SEVENDAYS_NEXUSMODS_GAME_DOMAIN = "7daystodie"
_SEVENDAYS_VERSION_RE = re.compile(
    r"Version:\s*V\s*(?P<version>\d+(?:\.\d+)*(?:\s*\([^)]+\)|\s*[bB]\d+)?)",
    re.IGNORECASE,
)
_SEVENDAYS_GAME_VERSION_RE = re.compile(
    r"GamePref\.GameVersion\s*=\s*V\s*(?P<version>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_SEVENDAYS_NEW_SAVE_ROOT_PREFIX = "new-save:"
_SEVENDAYS_NEW_WORLD_ROOT_PREFIX = "new-world:"
_SEVENDAYS_GAME_WORLD_SETTING_KEY = "GameWorld"
_SEVENDAYS_GAME_NAME_SETTING_KEY = "GameName"
_SEVENDAYS_NEW_RWG_WORLD_SELECTION = "new-rwg"
_SEVENDAYS_EXISTING_SAVE_SELECTION_PREFIX = "save:"
_SEVENDAYS_GENERATED_WORLD_SELECTION_PREFIX = "world:"
_SEVENDAYS_SAVE_ROOT_FILE_MARKERS: frozenset[str] = frozenset(
    {
        "blocklimits.dat",
        "blockmappings.nim",
        "decoration.7dt",
        "drones.dat",
        "gameoptions.sdf",
        "itemmappings.nim",
        "main.ttw",
        "multiblocks.7dt",
        "players.xml",
        "power.dat",
        "turrets.dat",
        "vehicles.dat",
    }
)
_SEVENDAYS_SAVE_ROOT_DIRECTORY_MARKERS: frozenset[str] = frozenset(
    {
        "configsdump",
        "dynamicmeshes",
        "player",
        "region",
    }
)
_SEVENDAYS_GENERATED_WORLD_FILE_MARKERS: frozenset[str] = frozenset(
    {
        "generationinfo.txt",
        "map_info.xml",
    }
)
_SEVENDAYS_VERSION_BUILD_RE = re.compile(
    r"(?P<main>\d+(?:\.\d+)*)(?:\s*\((?P<parenthesized>[^)]+)\)|\s*(?P<suffix>[bB]\d+))",
    re.IGNORECASE,
)
_SEVENDAYS_READY_RE = re.compile(r"\bStartAsServer\b")
_SEVENDAYS_TELNET_STARTUP_ERROR_RE = re.compile(r"\bError in Telnet\.ctor\b", re.IGNORECASE)
_SEVENDAYS_TRANSIENT_RE = re.compile(r"GMSG: Player '(.+?)' (joined|left) the game", re.IGNORECASE)
_SEVENDAYS_DEATH_RE = re.compile(r"GMSG: Player '(?P<player>.+?)' died\b", re.IGNORECASE)
_SEVENDAYS_CHAT_RE = re.compile(r"Chat.*?:\s*'(.*?)':\s*(.+)", re.IGNORECASE)
_SEVENDAYS_SANDBOX_CODE_RE = re.compile(r"\bSandbox Code:\s*(?P<code>\S+)\s*$", re.IGNORECASE)
_SEVENDAYS_SANDBOX_OPTIONS_RE = re.compile(r"\bSandbox Options:\s*$", re.IGNORECASE)
_SEVENDAYS_SANDBOX_SECTION_RE = re.compile(r"\*\*\*\s*(?P<section>[^*]+?)\s*\*\*\*")
_SEVENDAYS_SANDBOX_OPTION_RE = re.compile(
    r"\bOption\s+(?P<key>[^:]+):\s*"
    r"(?P<value_index>-?\d+)/(?P<value_label>.*?)\s+"
    r"\(default:\s*(?P<default_index>-?\d+)/(?P<default_label>.*?)\)\s*$",
    re.IGNORECASE,
)
_SEVENDAYS_RUNTIME_LOG_DISCOVERY_TIMEOUT_SECONDS = 10.0
_SEVENDAYS_RUNTIME_LOG_DISCOVERY_POLL_SECONDS = 0.25
_SEVENDAYS_MANAGED_USERDATA_FOLDER = "userdata"
_SEVENDAYS_YUKIBOT_DATA_RELATIVE_PATH = Path(".yukibot")
_SEVENDAYS_SANDBOX_OPTIONS_FILE_NAME = "sandbox_options.json"
_SEVENDAYS_SANDBOX_OPTIONS_SCHEMA_VERSION = 1
_SEVENDAYS_SANDBOX_OPTIONS_MIN_VERSION = AppVersion(main="3.0", build=259)
_SEVENDAYS_STARTUP_SANDBOX_OPTIONS_DELAY_SECONDS = 5.0
_SEVENDAYS_STARTUP_SANDBOX_OPTIONS_MAX_ATTEMPTS = 6
_SEVENDAYS_DEFAULT_TELNET_PORT = 8081


def _required_mapping_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_mapping_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() or None


def _required_mapping_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _sevendays_version_supports_sandbox_options(app_version: AppVersion | None) -> bool:
    return app_version is not None and app_version.is_at_least(_SEVENDAYS_SANDBOX_OPTIONS_MIN_VERSION)


def _json_object(raw_value: object, *, label: str) -> dict[str, object]:
    if not isinstance(raw_value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return raw_value


@dataclass(frozen=True, slots=True)
class SevenDaysSandboxOption:
    section: str
    key: str
    value_index: int
    value_label: str
    default_index: int
    default_label: str

    def __post_init__(self) -> None:
        for field_name in ("section", "key", "value_label", "default_label"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Sandbox option {field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "SevenDaysSandboxOption":
        return cls(
            section=_required_mapping_text(payload, "section"),
            key=_required_mapping_text(payload, "key"),
            value_index=_required_mapping_int(payload, "value_index"),
            value_label=_required_mapping_text(payload, "value_label"),
            default_index=_required_mapping_int(payload, "default_index"),
            default_label=_required_mapping_text(payload, "default_label"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "section": self.section,
            "key": self.key,
            "value_index": self.value_index,
            "value_label": self.value_label,
            "default_index": self.default_index,
            "default_label": self.default_label,
        }


@dataclass(frozen=True, slots=True)
class SevenDaysSandboxOptionsSnapshot:
    generated_at: str
    options: tuple[SevenDaysSandboxOption, ...]
    sandbox_code: str | None = None
    app_version: str | None = None
    schema_version: int = _SEVENDAYS_SANDBOX_OPTIONS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SEVENDAYS_SANDBOX_OPTIONS_SCHEMA_VERSION:
            raise ValueError("Unsupported 7D2D sandbox options schema version")
        if not self.generated_at.strip():
            raise ValueError("Sandbox options snapshot requires a generated timestamp")
        object.__setattr__(self, "generated_at", self.generated_at.strip())
        object.__setattr__(self, "sandbox_code", self.sandbox_code.strip() if self.sandbox_code else None)
        object.__setattr__(self, "app_version", self.app_version.strip() if self.app_version else None)
        if not self.options:
            raise ValueError("Sandbox options snapshot requires at least one option")

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "SevenDaysSandboxOptionsSnapshot":
        schema_version = _required_mapping_int(payload, "schema_version")
        raw_options = payload.get("options")
        if isinstance(raw_options, str) or not isinstance(raw_options, list | tuple):
            raise ValueError("Sandbox options snapshot options must be a sequence")
        options: list[SevenDaysSandboxOption] = []
        for raw_option in raw_options:
            options.append(SevenDaysSandboxOption.from_mapping(_json_object(raw_option, label="Sandbox option")))
        return cls(
            schema_version=schema_version,
            generated_at=_required_mapping_text(payload, "generated_at"),
            sandbox_code=_optional_mapping_text(payload, "sandbox_code"),
            app_version=_optional_mapping_text(payload, "app_version"),
            options=tuple(options),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sandbox_code": self.sandbox_code,
            "app_version": self.app_version,
            "options": [option.to_mapping() for option in self.options],
        }


def _timestamped_sevendays_output_logs(*, directory: Path) -> tuple[Path, ...]:
    log_directories = (directory, directory / "7DaysToDieServer_Data")
    candidates = sorted(
        (pointer for log_dir in log_directories if log_dir.is_dir() for pointer in log_dir.glob("output_log__*.txt")),
        key=lambda pointer: (pointer.stat().st_mtime, pointer.name),
        reverse=True,
    )
    return tuple(candidates)


def _latest_sevendays_output_logs(*, directory: Path, limit: int = 3) -> tuple[Path, ...]:
    if limit < 1:
        return ()
    return tuple(_timestamped_sevendays_output_logs(directory=directory)[:limit])


def _preferred_sevendays_runtime_log(
    *,
    directory: Path,
    server_log: Path | None,
    previous_timestamped_logs: Collection[Path] | None = None,
) -> Path | None:
    timestamped_logs: tuple[Path, ...] = _timestamped_sevendays_output_logs(directory=directory)
    if previous_timestamped_logs is not None:
        previous_log_set = frozenset(previous_timestamped_logs)
        for pointer in timestamped_logs:
            if pointer not in previous_log_set:
                return pointer

    explicit_candidates: tuple[Path | None, ...] = (
        server_log,
        directory / "server_stdout.log",
    )
    for pointer in explicit_candidates:
        if pointer is not None and pointer.exists():
            return pointer

    for pointer in timestamped_logs:
        return pointer

    legacy_output_log = directory / "7DaysToDieServer_Data" / "output_log.txt"
    if legacy_output_log.exists():
        return legacy_output_log
    return None


def _sevendays_runtime_log_signature(pointer: Path) -> SevenDaysRuntimeLogSignature:
    stat = pointer.stat()
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _snapshot_sevendays_runtime_logs(
    *,
    directory: Path,
    server_log: Path | None,
) -> dict[Path, SevenDaysRuntimeLogSignature]:
    snapshot: dict[Path, SevenDaysRuntimeLogSignature] = {}
    for pointer in _candidate_sevendays_logs(directory=directory, server_log=server_log):
        try:
            snapshot[pointer] = _sevendays_runtime_log_signature(pointer)
        except FileNotFoundError:
            continue
    return snapshot


async def _discover_sevendays_runtime_log(
    *,
    directory: Path,
    server_log: Path | None,
    previous_timestamped_logs: Collection[Path] | None = None,
    previous_log_signatures: Mapping[Path, SevenDaysRuntimeLogSignature] | None = None,
    check_running: Callable[[], bool],
    timeout_seconds: float = _SEVENDAYS_RUNTIME_LOG_DISCOVERY_TIMEOUT_SECONDS,
    poll_seconds: float = _SEVENDAYS_RUNTIME_LOG_DISCOVERY_POLL_SECONDS,
) -> Path | None:
    baseline = dict(previous_log_signatures or {})
    if previous_timestamped_logs is not None:
        for pointer in previous_timestamped_logs:
            try:
                baseline.setdefault(pointer, _sevendays_runtime_log_signature(pointer))
            except FileNotFoundError:
                continue

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if baseline:
            for runtime_log in _candidate_sevendays_logs(directory=directory, server_log=server_log):
                try:
                    current_signature = _sevendays_runtime_log_signature(runtime_log)
                except FileNotFoundError:
                    continue
                if baseline.get(runtime_log) != current_signature:
                    return runtime_log
        else:
            if runtime_log := _preferred_sevendays_runtime_log(
                directory=directory,
                server_log=server_log,
                previous_timestamped_logs=previous_timestamped_logs,
            ):
                return runtime_log
        if not check_running() or asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(poll_seconds)


def _candidate_sevendays_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates: list[Path | None] = [
        *_latest_sevendays_output_logs(directory=directory),
        server_log,
        directory / "server_stdout.log",
        directory / "7DaysToDieServer_Data" / "output_log.txt",
    ]
    existing: list[Path] = []
    seen: set[Path] = set()
    for pointer in candidates:
        if pointer is None or pointer in seen or not pointer.exists():
            continue
        seen.add(pointer)
        existing.append(pointer)
    return tuple(existing)


def _sevendays_telnet_port(pointer: Path) -> int:
    enabled_value = _read_serverconfig_value(pointer, "TelnetEnabled")
    if enabled_value is not None and enabled_value.casefold() not in {"true", "false"}:
        raise ValueError(f"Invalid 7D2D TelnetEnabled value: {enabled_value!r}")
    if enabled_value is not None and enabled_value.casefold() == "false":
        raise ValueError("7D2D Telnet must be enabled for server management")

    raw_port = _read_serverconfig_value(pointer, "TelnetPort")
    if raw_port is None:
        return _SEVENDAYS_DEFAULT_TELNET_PORT
    if not raw_port.isdigit():
        raise ValueError(f"Invalid 7D2D TelnetPort value: {raw_port!r}")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError(f"7D2D TelnetPort must be between 1 and 65535, got {port}")
    return port


def _app_version_from_sevendays_text(raw_version: str) -> AppVersion:
    if match := _SEVENDAYS_VERSION_BUILD_RE.fullmatch(raw_version.strip()):
        raw_build = match.group("suffix") or match.group("parenthesized")
        if raw_build is not None and (build_match := re.fullmatch(r"[bB](?P<build>\d+)", raw_build.strip())):
            return AppVersion(main=match.group("main"), build=int(build_match.group("build")))
    return AppVersion(main=raw_version)


def detect_sevendays_version(*, directory: Path, server_log: Path | None) -> AppVersion | None:
    version: AppVersion | None = None
    for pointer in _candidate_sevendays_logs(directory=directory, server_log=server_log):
        try:
            for line in pointer.read_text(config.STR_ENCODE, errors="ignore").splitlines():
                if match := _SEVENDAYS_VERSION_RE.search(line):
                    return _app_version_from_sevendays_text(match.group("version").strip())
                if version is None and (match := _SEVENDAYS_GAME_VERSION_RE.search(line)):
                    version = AppVersion(main=match.group("version").strip())
        except OSError as xcp:
            log.warning("Failed to inspect Seven Days log %s: %s", pointer, xcp)
    return version


def _read_modinfo_value(pointer: Path, field_name: str) -> str | None:
    root = ET.fromstring(pointer.read_text(config.STR_ENCODE))
    for node in root.iter():
        if node.tag.casefold() != field_name.casefold():
            continue
        raw_value = node.attrib.get("value")
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        return value or None
    return None


def _mod_page_from_sevendays_website(raw_website: str | None) -> ModPageLink | None:
    if raw_website is None:
        return None
    provider = known_mod_page_provider_for_url(raw_website)
    if provider is None or provider not in {
        KnownModPageProvider.NEXUSMODS,
        KnownModPageProvider.SEVEN_DAYS_TO_DIE_MODS,
    }:
        return None

    parsed = urlsplit(raw_website)
    path_segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if provider is KnownModPageProvider.NEXUSMODS:
        hostname = parsed.hostname.casefold() if parsed.hostname is not None else ""
        if hostname in {"nexusmods.com", "www.nexusmods.com"}:
            valid_project_path = (
                len(path_segments) >= 3
                and path_segments[0].casefold() == _SEVENDAYS_NEXUSMODS_GAME_DOMAIN
                and path_segments[1].casefold() == "mods"
                and path_segments[2].isdecimal()
            )
        else:
            valid_project_path = (
                hostname == f"{_SEVENDAYS_NEXUSMODS_GAME_DOMAIN}.nexusmods.com"
                and len(path_segments) >= 2
                and path_segments[0].casefold() == "mods"
                and path_segments[1].isdecimal()
        )
        if not valid_project_path:
            return None
    elif len(path_segments) < 2 or path_segments[0].casefold() != "mods":
        return None

    return ModPageLink(name=provider.value, url=raw_website)


def _read_serverconfig_value(pointer: Path, property_name: str) -> str | None:
    root = ET.parse(pointer).getroot()
    for node in root.iter("property"):
        if node.attrib.get("name") != property_name:
            continue
        raw_value = node.attrib.get("value")
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        return value or None
    return None


def _ensure_serverconfig_userdata_redirect(pointer: Path) -> None:
    tree = ET.parse(pointer)
    root = tree.getroot()
    last_known_index: int | None = None
    for index, node in enumerate(root.findall("property")):
        property_name = node.attrib.get("name")
        if property_name == "UserDataFolder":
            node.attrib["value"] = _SEVENDAYS_MANAGED_USERDATA_FOLDER
            tree.write(pointer, encoding=config.STR_ENCODE)
            return
        if property_name == "AdminFileName":
            last_known_index = index

    redirect_node = ET.Element(
        "property",
        {
            "name": "UserDataFolder",
            "value": _SEVENDAYS_MANAGED_USERDATA_FOLDER,
        },
    )
    insert_index = (last_known_index + 1) if last_known_index is not None else len(root)
    root.insert(insert_index, redirect_node)
    tree.write(pointer, encoding=config.STR_ENCODE)


def parse_gamestat_value(raw_value: str) -> GameStatValue:
    """Parse a 7D2D GameStat value, preserving bare identifiers as strings."""
    if raw_value == "":
        return None

    try:
        parsed_value = ast.literal_eval(raw_value)
    except SyntaxError, TypeError, ValueError:
        return raw_value

    if isinstance(parsed_value, bool | int | float | str):
        return parsed_value

    raise ValueError(f"Unsupported GameStat value: {raw_value!r}")


def _is_non_negative_int(raw_value: str) -> bool:
    return raw_value.isdigit()


def _is_non_empty_text(raw_value: str) -> bool:
    return bool(raw_value.strip())


def _normalise_sevendays_save_segment(raw_value: str, *, label: str) -> str:
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{label} is required.")
    path = PurePosixPath(value)
    if path.is_absolute() or path.parts != (value,) or value in {".", ".."} or "\x00" in value:
        raise ValueError(f"{label} must be a single directory name.")
    if value.startswith("."):
        raise ValueError(f"{label} must not start with a dot.")
    return value


def _is_valid_sevendays_save_segment(raw_value: str) -> bool:
    try:
        _normalise_sevendays_save_segment(raw_value, label="Save name")
    except ValueError:
        return False
    return True


def _sevendays_existing_save_selection(*, game_world: str, game_name: str) -> str:
    normalised_world = _normalise_sevendays_save_segment(game_world, label="Game world")
    normalised_name = _normalise_sevendays_save_segment(game_name, label="Save name")
    return (
        f"{_SEVENDAYS_EXISTING_SAVE_SELECTION_PREFIX}"
        f"{quote(normalised_world, safe='')}/{quote(normalised_name, safe='')}"
    )


def _sevendays_fresh_generated_world_selection(*, game_world: str) -> str:
    normalised_world = _normalise_sevendays_save_segment(game_world, label="Game world")
    return f"{_SEVENDAYS_GENERATED_WORLD_SELECTION_PREFIX}{quote(normalised_world, safe='')}"


def _sevendays_save_target_from_selection(selection: str) -> SevenDaysWorldSelection:
    if selection == _SEVENDAYS_NEW_RWG_WORLD_SELECTION:
        return SevenDaysWorldSelection(game_world=None, game_name=None)
    encoded_world = selection.removeprefix(_SEVENDAYS_GENERATED_WORLD_SELECTION_PREFIX)
    if encoded_world != selection:
        return SevenDaysWorldSelection(
            game_world=_normalise_sevendays_save_segment(unquote(encoded_world), label="Game world"),
            game_name=None,
        )
    encoded_target = selection.removeprefix(_SEVENDAYS_EXISTING_SAVE_SELECTION_PREFIX)
    if encoded_target == selection:
        raise ValueError("7 Days to Die world save selection is invalid.")
    encoded_world, separator, encoded_name = encoded_target.partition("/")
    if not separator:
        raise ValueError("7 Days to Die world save selection is invalid.")
    return SevenDaysWorldSelection(
        game_world=_normalise_sevendays_save_segment(unquote(encoded_world), label="Game world"),
        game_name=_normalise_sevendays_save_segment(unquote(encoded_name), label="Save name"),
    )


def inspect_sevendays_save_archive(archive_path: Path) -> SevenDaysSaveArchiveInspection:
    entries = _sevendays_save_archive_entries(archive_path)
    file_paths = [path for member, path in entries if not member.is_dir()]
    if not file_paths:
        raise ValueError("7 Days to Die save archive does not contain any files.")

    marker_prefixes: set[tuple[str, ...]] = set()
    generated_world_prefixes: set[tuple[str, ...]] = set()
    for path in file_paths:
        marker_prefix = _sevendays_save_marker_prefix(path)
        if marker_prefix is not None:
            marker_prefixes.add(marker_prefix)
        generated_world_prefix = _sevendays_generated_world_prefix(path)
        if generated_world_prefix is not None:
            generated_world_prefixes.add(generated_world_prefix)
    # Generated worlds also contain region files, which look like save markers.
    # A marker rooted at the generated-world directory is terrain, not a save.
    marker_prefixes.difference_update(generated_world_prefixes)
    if len(marker_prefixes) > 1:
        labels = ", ".join(sorted(PurePosixPath(*prefix).as_posix() or "." for prefix in marker_prefixes))
        raise ValueError(f"7 Days to Die save archive contains multiple save roots: {labels}")
    if len(generated_world_prefixes) > 1:
        labels = ", ".join(
            sorted(PurePosixPath(*prefix).as_posix() for prefix in generated_world_prefixes)
        )
        raise ValueError(f"7 Days to Die save archive contains multiple generated worlds: {labels}")
    if not marker_prefixes and not generated_world_prefixes:
        raise ValueError("7 Days to Die save archive does not contain recognizable save or generated-world files.")

    content_prefix = next(iter(marker_prefixes), None)
    generated_world_prefix = next(iter(generated_world_prefixes), None)
    allowed_prefixes = tuple(
        prefix for prefix in (content_prefix, generated_world_prefix) if prefix is not None
    )
    for path in file_paths:
        if _sevendays_archive_path_is_ignorable(path):
            continue
        if not any(path.parts[: len(prefix)] == prefix for prefix in allowed_prefixes):
            raise ValueError(f"7 Days to Die save archive contains files outside the save root: {path.as_posix()}")

    game_world, game_name = _sevendays_archive_save_target(content_prefix) if content_prefix is not None else (None, None)
    generated_world = generated_world_prefix[-1] if generated_world_prefix is not None else None
    if game_world is not None and generated_world is not None and game_world != generated_world:
        raise ValueError(
            "7 Days to Die save archive has different save and generated-world names: "
            f"{game_world!r} and {generated_world!r}."
        )
    return SevenDaysSaveArchiveInspection(
        content_prefix=content_prefix,
        game_world=game_world or generated_world,
        game_name=game_name,
        generated_world_prefix=generated_world_prefix,
        generated_world=generated_world,
        file_count=len(file_paths),
    )


def extract_sevendays_save_archive(
    *,
    archive_path: Path,
    destination: Path | None = None,
    generated_world_destination: Path | None = None,
    inspection: SevenDaysSaveArchiveInspection | None = None,
) -> SevenDaysSaveArchiveInspection:
    inspection = inspection or inspect_sevendays_save_archive(archive_path)
    if inspection.content_prefix is not None and destination is None:
        raise ValueError("7 Days to Die save archive requires a save destination.")
    if inspection.generated_world_prefix is not None and generated_world_destination is None:
        raise ValueError("7 Days to Die world archive requires a generated-world destination.")
    entries = _sevendays_save_archive_entries(archive_path)
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
    if generated_world_destination is not None:
        generated_world_destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member, path in entries:
            if _sevendays_archive_path_is_ignorable(path):
                continue
            extraction_destination = destination
            content_prefix = inspection.content_prefix
            if (
                inspection.generated_world_prefix is not None
                and path.parts[: len(inspection.generated_world_prefix)] == inspection.generated_world_prefix
            ):
                if generated_world_destination is None:
                    raise RuntimeError("Generated-world destination unexpectedly missing.")
                extraction_destination = generated_world_destination
                content_prefix = inspection.generated_world_prefix
            if extraction_destination is None or content_prefix is None:
                raise RuntimeError("Save destination unexpectedly missing.")
            relative_path = _strip_sevendays_archive_content_prefix(path, content_prefix)
            if relative_path is None:
                continue
            target = extraction_destination.joinpath(*relative_path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    return inspection


def _sevendays_save_archive_entries(archive_path: Path) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"7 Days to Die save upload is not a zip archive: {archive_path.name}")

    entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            raw_name = member.filename.strip("/")
            if not raw_name:
                continue
            path = PurePosixPath(raw_name)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError(f"7 Days to Die save archive member path is invalid: {member.filename}")
            entries.append((member, path))
    return entries


def _sevendays_save_marker_prefix(path: PurePosixPath) -> tuple[str, ...] | None:
    parts = path.parts
    if not parts:
        return None
    if parts[-1].casefold() in _SEVENDAYS_SAVE_ROOT_FILE_MARKERS:
        return parts[:-1]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in _SEVENDAYS_SAVE_ROOT_DIRECTORY_MARKERS:
            return parts[:index]
    return None


def _sevendays_generated_world_prefix(path: PurePosixPath) -> tuple[str, ...] | None:
    if path.parts[-1].casefold() not in _SEVENDAYS_GENERATED_WORLD_FILE_MARKERS:
        return None
    for index, part in enumerate(path.parts[:-1]):
        if part.casefold() == "generatedworlds":
            if index + 1 < len(path.parts) - 1:
                return path.parts[: index + 2]
            return None
        if part.casefold() == "saves":
            return None
    if len(path.parts) >= 2:
        return path.parts[:-1]
    return None


def _sevendays_archive_save_target(content_prefix: tuple[str, ...]) -> tuple[str | None, str | None]:
    for index, part in enumerate(content_prefix[:-2]):
        if part.casefold() == "saves":
            return (content_prefix[index + 1], content_prefix[index + 2])
    if len(content_prefix) >= 2:
        return (content_prefix[-2], content_prefix[-1])
    if len(content_prefix) == 1:
        return (None, content_prefix[0])
    return (None, None)


def _sevendays_archive_path_is_ignorable(path: PurePosixPath) -> bool:
    return any(part.startswith(".") or part == "__MACOSX" for part in path.parts)


def _strip_sevendays_archive_content_prefix(
    path: PurePosixPath,
    content_prefix: tuple[str, ...],
) -> PurePosixPath | None:
    parts = path.parts
    if parts[: len(content_prefix)] != content_prefix:
        return None
    parts = parts[len(content_prefix) :]
    if not parts:
        return None
    return PurePosixPath(*parts)


_GAME_DIFFICULTY_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Scavenger"),
    ChoiceOption("1", "Adventurer"),
    ChoiceOption("2", "Nomad"),
    ChoiceOption("3", "Warrior"),
    ChoiceOption("4", "Survivalist"),
    ChoiceOption("5", "Insane"),
)

_DEATH_PENALTY_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Nothing"),
    ChoiceOption("1", "Xp Penalty"),
    ChoiceOption("2", "Injured"),
    ChoiceOption("3", "Permanent Death"),
)

_DROP_ON_DEATH_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Nothing"),
    ChoiceOption("1", "Everything"),
    ChoiceOption("2", "Toolbelt"),
    ChoiceOption("3", "Backpack"),
    ChoiceOption("4", "Delete All"),
)

_DROP_ON_QUIT_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Nothing"),
    ChoiceOption("1", "Everything"),
    ChoiceOption("2", "Toolbelt"),
    ChoiceOption("3", "Backpack"),
)

_CAMERA_RESTRICTION_CHOICES = ChoiceSpec(
    ChoiceOption("0", "First Or Third Person"),
    ChoiceOption("1", "First Person Only"),
    ChoiceOption("2", "Third Person Only"),
)

_ENEMY_DIFFICULTY_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Normal"),
    ChoiceOption("1", "Feral"),
)

_ZOMBIE_FERAL_SENSE_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Off"),
    ChoiceOption("1", "Day"),
    ChoiceOption("2", "Night"),
    ChoiceOption("3", "All"),
)

_PLAYER_KILLING_MODE_CHOICES = ChoiceSpec(
    ChoiceOption("0", "No Killing"),
    ChoiceOption("1", "Kill Allies Only"),
    ChoiceOption("2", "Kill Strangers Only"),
    ChoiceOption("3", "Kill Everyone"),
)

_ALLOW_SPAWN_NEAR_FRIEND_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Disabled"),
    ChoiceOption("1", "Always"),
    ChoiceOption("2", "Forest Only"),
)

_LAND_CLAIM_DECAY_MODE_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Slow (Linear)"),
    ChoiceOption("1", "Fast (Exponential)"),
    ChoiceOption("2", "None Until Expired"),
)

_AI_SMELL_MODE_CHOICES = ChoiceSpec(
    ChoiceOption("0", "Off"),
    ChoiceOption("1", "Walk"),
    ChoiceOption("2", "Jog"),
    ChoiceOption("3", "Run"),
    ChoiceOption("4", "Sprint"),
    ChoiceOption("5", "Nightmare"),
)

_SETTIME_CHOICES = ChoiceSpec(
    ChoiceOption("day", "Day"),
    ChoiceOption("night", "Night"),
    strict=False,
)

_SERVER_REGION_CHOICES = ChoiceSpec(
    ChoiceOption("NorthAmericaEast", "N.America East"),
    ChoiceOption("NorthAmericaWest", "N.America West"),
    ChoiceOption("CentralAmerica", "Central America"),
    ChoiceOption("SouthAmerica", "South America"),
    ChoiceOption("Europe", "Europe"),
    ChoiceOption("Russia", "Russia"),
    ChoiceOption("Asia", "Asia"),
    ChoiceOption("MiddleEast", "Middle East"),
    ChoiceOption("Africa", "Africa"),
    ChoiceOption("Oceania", "Oceania"),
)

_SERVER_VISIBILITY_CHOICES = ChoiceSpec(
    ChoiceOption("2", "Public"),
    ChoiceOption("1", "Friends"),
    ChoiceOption("0", "Hidden"),
)

_WORLD_GEN_SIZE_CHOICES = ChoiceSpec(
    ChoiceOption("6144", "Small"),
    ChoiceOption("8192", "Medium"),
    ChoiceOption("10240", "Large"),
)

_TRADER_BIOME_CHOICES = ChoiceSpec(
    ChoiceOption("forest", "Forest"),
    ChoiceOption("burntforest", "Burnt Forest"),
    ChoiceOption("desert", "Desert"),
    ChoiceOption("snow", "Snow"),
    ChoiceOption("wasteland", "Wasteland"),
)


@dataclass(frozen=True, slots=True)
class TraderBiomeDefinition:
    key: str
    label: str
    partial_name: str
    default_biome: str


_TRADER_BIOME_DEFINITIONS: tuple[TraderBiomeDefinition, ...] = (
    TraderBiomeDefinition(
        key="TraderRektBiome",
        label="Trader Rekt Biome",
        partial_name="trader_rekt",
        default_biome="forest",
    ),
    TraderBiomeDefinition(
        key="TraderJenBiome",
        label="Trader Jen Biome",
        partial_name="trader_jen",
        default_biome="burntforest",
    ),
    TraderBiomeDefinition(
        key="TraderBobBiome",
        label="Trader Bob Biome",
        partial_name="trader_bob",
        default_biome="desert",
    ),
    TraderBiomeDefinition(
        key="TraderHughBiome",
        label="Trader Hugh Biome",
        partial_name="trader_hugh",
        default_biome="snow",
    ),
    TraderBiomeDefinition(
        key="TraderJoelBiome",
        label="Trader Joel Biome",
        partial_name="trader_joel",
        default_biome="wasteland",
    ),
)
_TRADER_BIOME_KEYS: frozenset[str] = frozenset(definition.key for definition in _TRADER_BIOME_DEFINITIONS)
_TRADER_BIOME_VALUES: frozenset[str] = _TRADER_BIOME_CHOICES.raw_values()


def _quote_console_argument(raw_value: str) -> str:
    cleaned_value = " ".join(raw_value.strip().splitlines()).replace('"', "'")
    if cleaned_value == "":
        raise ValueError("Console argument cannot be empty")
    return f'"{cleaned_value}"'


@dataclass(frozen=True, slots=True)
class SevenDaysAdminAddRequest:
    subject: str
    permission_level: int


def parse_admin_add_value(raw_value: str) -> SevenDaysAdminAddRequest:
    stripped_value = raw_value.strip()
    if stripped_value == "":
        raise ValueError("Admin add requires a player or platform identifier and permission level")

    subject_part: str
    level_part: str
    if "|" in stripped_value:
        subject_part, level_part = stripped_value.rsplit("|", 1)
    else:
        subject_part, separator, level_part = stripped_value.rpartition(" ")
        if separator == "":
            raise ValueError("Admin add requires `<player or id> <permission level>`")

    subject = subject_part.strip().strip('"')
    level_text = level_part.strip()
    if subject == "":
        raise ValueError("Admin add subject cannot be empty")
    if not _is_non_negative_int(level_text):
        raise ValueError("Admin add permission level must be a whole number between 0 and 1000")

    permission_level = int(level_text)
    if permission_level < 0 or permission_level > 1000:
        raise ValueError("Admin add permission level must be between 0 and 1000")

    return SevenDaysAdminAddRequest(subject=subject, permission_level=permission_level)


async def _send_console_command(
    app: "SevenDays",
    command: str,
    *,
    success_text: str,
    response_text: str | None = None,
) -> ConsoleActionResult:
    was_sent = await app._relay.send(command)
    if not was_sent:
        raise RuntimeError(f"Failed to send console command: {command}")
    return ConsoleActionResult(summary=success_text, text=response_text, source=ConsoleResponseSource.TELNET)


async def _console_saveworld(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    return await _send_console_command(app, "saveworld", success_text=f"{app.friendly}: world save requested.")


async def _request_graceful_shutdown(app: "SevenDays") -> tuple[bool, bool]:
    save_sent = bool(await app._relay.send("saveworld"))
    if save_sent:
        await asyncio.sleep(0.1)
    shutdown_sent = bool(await app._relay.send("shutdown"))
    return (save_sent, shutdown_sent)


async def _console_shutdown(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    save_sent, shutdown_sent = await _request_graceful_shutdown(app)
    if not save_sent:
        raise RuntimeError("Failed to send console command: saveworld")
    if not shutdown_sent:
        raise RuntimeError("Failed to send console command: shutdown")
    return ConsoleActionResult(
        summary=f"{app.friendly}: world save and shutdown requested.",
        source=ConsoleResponseSource.TELNET,
    )


async def _console_settime(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    target = cast(str, value)
    return await _send_console_command(app, f"settime {target}", success_text=f"{app.friendly}: time command sent.")


async def _console_raw_command(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    command = cast(str, value)
    return await _send_console_command(
        app,
        command,
        success_text=f"{app.friendly}: console command sent.",
    )


async def _console_getsandboxoptions(app_obj: object, value: object | None) -> ConsoleActionResult:
    del value
    app = cast(SevenDays, app_obj)
    if not await app.request_sandbox_options():
        raise RuntimeError("Failed to send console command: getsandboxoptions")
    return ConsoleActionResult(
        summary=f"{app.friendly}: sandbox options requested.",
        text="Sandbox options are written to the 7D2D stdout feed.",
        source=ConsoleResponseSource.TELNET,
    )


async def _console_say(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    message = cast(str, value)
    command = f"say {_quote_console_argument(message)}"
    return await _send_console_command(app, command, success_text=f"{app.friendly}: broadcast sent.")


async def _console_kick(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    subject = cast(str, value)
    command = f"kick {_quote_console_argument(subject)}"
    return await _send_console_command(app, command, success_text=f"{app.friendly}: kick requested for `{subject}`.")


async def _console_admin_add(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    request = cast(SevenDaysAdminAddRequest, value)
    command = f"admin add {_quote_console_argument(request.subject)} {request.permission_level}"
    return await _send_console_command(
        app,
        command,
        success_text=f"{app.friendly}: admin add requested for `{request.subject}` at level `{request.permission_level}`.",
    )


async def _console_admin_remove(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    subject = cast(str, value)
    command = f"admin remove {_quote_console_argument(subject)}"
    return await _send_console_command(
        app,
        command,
        success_text=f"{app.friendly}: admin removal requested for `{subject}`.",
    )


_SEVENDAYS_MESSAGE_PARAMETER = ConsoleActionParameter[str](
    key="message",
    label="Message",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Broadcast message to send with `say`.",
    max_length=200,
    multiline=True,
)
_SEVENDAYS_SETTIME_PARAMETER = ConsoleActionParameter[str](
    key="time",
    label="Time",
    value_type=str,
    choices=_SETTIME_CHOICES,
    validator=_is_non_empty_text,
    desc="Use `day`, `night`, `1300`, or `<day> <hour> <minute>` like `6 15 0`.",
    max_length=32,
)
_SEVENDAYS_RAW_COMMAND_PARAMETER = ConsoleActionParameter[str](
    key="command",
    label="Command",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Raw 7D2D console command without a leading slash.",
    max_length=500,
    multiline=True,
)
_SEVENDAYS_PLAYER_PARAMETER = ConsoleActionParameter[str](
    key="player",
    label="Player Or Id",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Player name, entity id, or platform/Steam id.",
    max_length=120,
)
_SEVENDAYS_ADMIN_ADD_PARAMETER = ConsoleActionParameter[SevenDaysAdminAddRequest](
    key="admin_add",
    label="Player Or Id + Level",
    value_type=parse_admin_add_value,
    desc="Enter `<player or id> | <permission level>`. A plain trailing level like `Alice 0` also works.",
    max_length=160,
)

_SEVENDAYS_CONSOLE_ACTIONS: tuple[ConsoleAction, ...] = (
    ConsoleAction(
        key="saveworld",
        label="Save World",
        description="Flush world state to disk.",
        power_level=Power_Level.user,
        execute=_console_saveworld,
    ),
    ConsoleAction(
        key="say",
        label="Say",
        description="Broadcast a message to all players.",
        power_level=Power_Level.user,
        execute=_console_say,
        parameter=_SEVENDAYS_MESSAGE_PARAMETER,
    ),
    ConsoleAction(
        key="settime",
        label="Set Time",
        description="Set the world time to day, night, or a specific value.",
        power_level=Power_Level.sudo,
        execute=_console_settime,
        parameter=_SEVENDAYS_SETTIME_PARAMETER,
    ),
    ConsoleAction(
        key="raw_command",
        label="Run Command",
        description="Send a raw command to the 7D2D console.",
        power_level=Power_Level.sudo,
        execute=_console_raw_command,
        parameter=_SEVENDAYS_RAW_COMMAND_PARAMETER,
    ),
    ConsoleAction(
        key="getsandboxoptions",
        label="Get Sandbox Options",
        description="Request the sandbox option dump added in 7D2D 3.0 b259.",
        power_level=Power_Level.user,
        execute=_console_getsandboxoptions,
        min_app_version=AppVersion(main="3.0", build=259),
    ),
    ConsoleAction(
        key="kick",
        label="Kick Player",
        description="Disconnect a player from the server.",
        power_level=Power_Level.sudo,
        execute=_console_kick,
        parameter=_SEVENDAYS_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="admin_add",
        label="Admin Add",
        description="Grant admin access to a player or platform identifier.",
        power_level=Power_Level.sudo,
        execute=_console_admin_add,
        parameter=_SEVENDAYS_ADMIN_ADD_PARAMETER,
    ),
    ConsoleAction(
        key="admin_remove",
        label="Admin Remove",
        description="Remove admin access from a player or platform identifier.",
        power_level=Power_Level.sudo,
        execute=_console_admin_remove,
        parameter=_SEVENDAYS_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="shutdown",
        label="Shutdown",
        description="Save the world, then gracefully stop the 7D2D server.",
        power_level=Power_Level.sudo,
        execute=_console_shutdown,
    ),
)


_SEVENDAYS_BUILTIN_MOD_NAMES = frozenset(
    {
        "0_TFP_Harmony",
        "TFP_CommandExtensions",
        "TFP_MapRendering",
        "TFP_WebServer",
        "Xample_MarkersMod",
    }
)
_SEVENDAYS_MOD_INFO_FILENAMES: Mapping[ModPlacement, str] = {
    ModPlacement.SERVER_ENABLED: "ModInfo.xml",
    ModPlacement.SERVER_DISABLED: "ModInfo.xml.disabled",
    ModPlacement.CLIENT_ONLY: "ModInfo.xml.client",
}


class Mod_7D2D(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    @staticmethod
    def _existing_mod_info_placements(mod_directory: Path) -> tuple[ModPlacement, ...]:
        return tuple(
            placement
            for placement, filename in _SEVENDAYS_MOD_INFO_FILENAMES.items()
            if (mod_directory / filename).exists()
        )

    @classmethod
    def iter_candidates(cls, folder: Path) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for pointer in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if not pointer.is_dir():
                continue
            if cls._existing_mod_info_placements(pointer):
                candidates.append(pointer)
        return tuple(candidates)

    @classmethod
    def config_from_candidate(
        cls,
        candidate: Path,
        modcf_cls: type[Mod_Config],
        *,
        folder: Path,
    ) -> Mod_Config | None:
        placements = cls._existing_mod_info_placements(candidate)
        if len(placements) != 1:
            raise RuntimeError(f"7D2D mod has conflicting ModInfo.xml placements: {candidate}")
        placement = placements[0]
        return modcf_cls(
            name=candidate.name,
            directory=folder,
            placement=placement,
            mod_type=ModType.CLIENT if placement is ModPlacement.CLIENT_ONLY else ModType.REGULAR,
        )

    @property
    def mod_info_enabled_path(self) -> Path:
        return self.path_for_placement(ModPlacement.SERVER_ENABLED)

    @property
    def mod_info_disabled_path(self) -> Path:
        return self.path_for_placement(ModPlacement.SERVER_DISABLED)

    @property
    def mod_info_client_path(self) -> Path:
        return self.path_for_placement(ModPlacement.CLIENT_ONLY)

    def path_for_placement(self, placement: ModPlacement) -> Path:
        return self.enabled_path / _SEVENDAYS_MOD_INFO_FILENAMES[placement]

    @property
    def path(self) -> Path:
        return self.enabled_path

    @property
    def storage_path(self) -> Path:
        """The whole mod directory remains in place when ModInfo.xml is disabled."""
        return self.enabled_path

    def download_archive_path_rewrites(self) -> tuple[tuple[PurePosixPath, PurePosixPath], ...]:
        if self.cfg.placement is ModPlacement.SERVER_ENABLED:
            return ()
        return (
            (
                PurePosixPath(_SEVENDAYS_MOD_INFO_FILENAMES[self.cfg.placement]),
                PurePosixPath(_SEVENDAYS_MOD_INFO_FILENAMES[ModPlacement.SERVER_ENABLED]),
            ),
        )

    def default_mod_type(self) -> ModType:
        if self.name in _SEVENDAYS_BUILTIN_MOD_NAMES:
            return ModType.BUILTIN
        return ModType.REGULAR

    def exists(self) -> bool:
        return self.enabled_path.is_dir() and bool(
            self._existing_mod_info_placements(self.enabled_path)
        )

    def sync_enabled_state(self) -> None:
        placements = self._existing_mod_info_placements(self.enabled_path)
        if len(placements) > 1:
            raise RuntimeError(f"7D2D mod has files in multiple placements: {self.name}")
        if placements:
            self.cfg.set_placement(placements[0])
        else:
            self.cfg.set_placement(self.cfg.placement)

    def _current_mod_info_path(self) -> Path | None:
        self.sync_enabled_state()
        if self.mod_info_enabled_path.exists():
            return self.mod_info_enabled_path
        if self.mod_info_disabled_path.exists():
            return self.mod_info_disabled_path
        if self.mod_info_client_path.exists():
            return self.mod_info_client_path
        return None

    def _mod_info_value(self, field_name: str) -> str | None:
        pointer = self._current_mod_info_path()
        return None if pointer is None else _read_modinfo_value(pointer, field_name)

    def detect_version(self) -> str | None:
        return self._mod_info_value("Version")

    def detect_friendly(self) -> str | None:
        return self._mod_info_value("DisplayName")

    def detect_description(self) -> str | None:
        return self._mod_info_value("Description")

    def native_metadata_id(self) -> str | None:
        return self._mod_info_value("Name")

    def metadata_fallback_id(self) -> str:
        return (self.native_metadata_id() or self.name).casefold()

    def detect_mod_page(self) -> ModPageLink | None:
        return _mod_page_from_sevendays_website(self._mod_info_value("Website"))

    def sync_metadata(self) -> None:
        super().sync_metadata()
        detected_page = self.detect_mod_page()
        if detected_page is None:
            return
        detected_provider = known_mod_page_provider_for_url(detected_page.url)
        if any(
            known_mod_page_provider_for_url(existing_page.url) is detected_provider
            for existing_page in self.cfg.mod_pages
        ):
            return
        self.cfg.mod_pages = (*self.cfg.mod_pages, detected_page)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_extr(src, atomic)

    async def _enable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await run_blocking(File_Utils.move, self.mod_info_disabled_path, self.mod_info_enabled_path)
        self.cfg.set_placement(ModPlacement.SERVER_ENABLED)
        return self.enabled_path

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await run_blocking(File_Utils.move, self.mod_info_enabled_path, self.mod_info_disabled_path)
        self.cfg.set_placement(ModPlacement.SERVER_DISABLED)
        return self.enabled_path


class SevenDays_Settings(App_Settings):
    def __init__(
        self,
        pointer: Path,
        *,
        rwgmixer_pointer: Path | None = None,
        version_getter: Callable[[], AppVersion | None] | None = None,
    ) -> None:
        _ensure_serverconfig_userdata_redirect(pointer)
        self.rwgmixer_pointer = rwgmixer_pointer or pointer.parent / "Data" / "Config" / "rwgmixer.xml"
        options = [
            Setting[str](
                StringSettingSpec(),
                Setting_Label.serv_name,
                "ServerName",
                [],
                default="My Game Host",
                desc="Name shown in the server browser.",
            ),
            Setting[str](
                StringSettingSpec(),
                Setting_Label.serv_desc,
                "ServerDescription",
                [],
                default="A 7 Days to Die server",
                desc="Description shown in the server browser.",
                paragraph=True,
            ),
            Setting[str](
                StringSettingSpec(
                    allow_blank=True,
                    is_sensitive=True,
                    do_hide=Power_Level.user,
                ),
                Setting_Label.password,
                "ServerPassword",
                [],
                default="",
                power_level=Power_Level.sudo,
                desc="Password required to join. Leave blank for no password.",
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Easy Anti-Cheat",
                "EACEnabled",
                [],
                default=True,
                power_level=Power_Level.sudo,
                desc="Require Easy Anti-Cheat for connecting clients.",
            ),
            Setting[str](
                StringSettingSpec(_SERVER_REGION_CHOICES),
                "Server Region",
                "Region",
                [],
                default="NorthAmericaEast",
                desc="Server browser region.",
            ),
            Setting[int](
                IntSettingSpec(_SERVER_VISIBILITY_CHOICES),
                Setting_Label.visibility,
                "ServerVisibility",
                [],
                default=2,
                desc="How the server appears in the browser.",
            ),
            Setting[int](
                IntSettingSpec(max_value=2048),
                "World Transfer Speed (KiB/s)",
                "ServerMaxWorldTransferSpeedKiBs",
                [],
                default=512,
                power_level=Power_Level.sudo,
                desc="Caps first-time world downloads to about 1300 KiB/s per client.",
            ),
            Setting[int](
                IntSettingSpec(min_value=1),
                Setting_Label.max_player,
                "ServerMaxPlayerCount",
                [],
                default=8,
                desc="Maximum concurrent players.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Reserved Slots",
                "ServerReservedSlots",
                [],
                default=0,
                power_level=Power_Level.sudo,
                desc="Slots reserved for players with the required permission level.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Admin Slots",
                "ServerAdminSlots",
                [],
                default=0,
                power_level=Power_Level.sudo,
                desc="Extra admin-only slots available after normal slots are full.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Player Safe Zone Level",
                "PlayerSafeZoneLevel",
                [],
                default=5,
                desc="Spawn protection applies while the player is at or below this level.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Player Safe Zone Hours",
                "PlayerSafeZoneHours",
                [],
                default=5,
                desc="In-world hours that spawn protection remains active.",
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Build Create",
                "BuildCreate",
                [],
                default=False,
                power_level=Power_Level.sudo,
                desc="Enable creative build mode cheats.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Bedroll Dead Zone Size",
                "BedrollDeadZoneSize",
                [],
                default=15,
                desc="Radius of the no-spawn zone around a bedroll.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Bedroll Expiry Time",
                "BedrollExpiryTime",
                [],
                default=45,
                desc="Real-world days a bedroll remains active after the owner was last online.",
            ),
            Setting[int](
                IntSettingSpec(_ALLOW_SPAWN_NEAR_FRIEND_CHOICES),
                "Allow Spawn Near Friend",
                "AllowSpawnNearFriend",
                [],
                default=2,
                desc="Control whether first-time joins may spawn near online friends.",
                min_app_version=AppVersion(main="2.0"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Max Spawned Zombies",
                "MaxSpawnedZombies",
                [],
                default=64,
                desc="World-wide zombie population cap.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Max Spawned Animals",
                "MaxSpawnedAnimals",
                [],
                default=50,
                desc="World-wide wildlife population cap.",
            ),
            Setting[int](
                IntSettingSpec(min_value=6, max_value=12),
                "Server Max Allowed View Distance",
                "ServerMaxAllowedViewDistance",
                [],
                default=12,
                desc="Highest client view distance the server allows.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Count",
                "LandClaimCount",
                [],
                default=5,
                desc="Maximum active land claims per player.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Size",
                "LandClaimSize",
                [],
                default=41,
                desc="Protected keystone area size in blocks.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Dead Zone",
                "LandClaimDeadZone",
                [],
                default=30,
                desc="Minimum block distance between unrelated land claims.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Expiry Time",
                "LandClaimExpiryTime",
                [],
                default=7,
                desc="Real-world offline days before land claims expire.",
            ),
            Setting[int](
                IntSettingSpec(_LAND_CLAIM_DECAY_MODE_CHOICES),
                "Land Claim Decay Mode",
                "LandClaimDecayMode",
                [],
                default=0,
                desc="Choose how protection changes while claim owners are offline.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Online Durability Modifier",
                "LandClaimOnlineDurabilityModifier",
                [],
                default=4,
                desc="Protected block durability multiplier while the owner is online. `0` is infinite.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Offline Durability Modifier",
                "LandClaimOfflineDurabilityModifier",
                [],
                default=4,
                desc="Protected block durability multiplier while the owner is offline. `0` is infinite.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Land Claim Offline Delay",
                "LandClaimOfflineDelay",
                [],
                default=0,
                desc="Minutes after logout before offline land-claim durability applies.",
            ),
            Setting[str](
                StringSettingSpec(),
                "Active World Save",
                "GameWorld",
                [],
                default=_sevendays_existing_save_selection(game_world="Navezgane", game_name="MyGame"),
                power_level=Power_Level.sudo,
                desc="Choose an existing world/save pair, or choose Generate Fresh Random World below.",
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                "World Gen Seed",
                "WorldGenSeed",
                [],
                default="MyGame",
                power_level=Power_Level.sudo,
                desc="Seed used only when generating a fresh random world.",
            ),
            Setting[int](
                IntSettingSpec(_WORLD_GEN_SIZE_CHOICES),
                "World Gen Size",
                "WorldGenSize",
                [],
                default=6144,
                power_level=Power_Level.sudo,
                desc="Supported RWG world size preset.",
            ),
            Setting[str](
                StringSettingSpec(raw_validator=_is_valid_sevendays_save_segment),
                "Fresh Save Name",
                "GameName",
                [],
                default="MyGame",
                power_level=Power_Level.sudo,
                desc="Used only with Generate Fresh Random World. Existing world/save selections set this automatically.",
            ),
            *[
                Setting[str](
                    StringSettingSpec(_TRADER_BIOME_CHOICES),
                    definition.label,
                    definition.key,
                    [],
                    default=definition.default_biome,
                    desc="RWG unique biome assigned to this trader.",
                    power_level=Power_Level.sudo,
                )
                for definition in _TRADER_BIOME_DEFINITIONS
            ],
            Setting[int](
                IntSettingSpec(_GAME_DIFFICULTY_CHOICES),
                Setting_Label.difficulty,
                "GameDifficulty",
                [],
                default=1,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[str](
                StringSettingSpec(raw_validator=_is_non_empty_text),
                "Sandbox Code",
                "SandboxCode",
                [],
                default="AAAJABJACJADJARFBNC",
                desc="Encoded sandbox options copied from the 7D2D new game screen.",
                min_app_version=AppVersion(main="3.0"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Block Damage Player",
                "BlockDamagePlayer",
                [],
                default=100,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Block Damage Ai",
                "BlockDamageAI",
                [],
                default=100,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Block Damage Ai Blood Moon",
                "BlockDamageAIBM",
                [],
                default=100,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Xp Multiplier",
                "XPMultiplier",
                [],
                default=100,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Day Night Length",
                "DayNightLength",
                [],
                default=60,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Day Light Length",
                "DayLightLength",
                [],
                default=18,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Biome Progression",
                "BiomeProgression",
                [],
                default=True,
                min_app_version=AppVersion(main="2.0"),
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Webdashboard",
                "WebDashboardEnabled",
                [],
                default=False,
                power_level=Power_Level.sudo,
                desc="Enable the built-in web dashboard.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Storm Frequency",
                "StormFreq",
                [],
                default=100,
                min_app_version=AppVersion(main="2.0"),
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_DEATH_PENALTY_CHOICES),
                "Death Penalty",
                "DeathPenalty",
                [],
                default=1,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_DROP_ON_DEATH_CHOICES),
                "Drop On Death",
                "DropOnDeath",
                [],
                default=1,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_DROP_ON_QUIT_CHOICES),
                "Drop On Quit",
                "DropOnQuit",
                [],
                default=0,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_CAMERA_RESTRICTION_CHOICES),
                "Camera Restriction",
                "CameraRestrictionMode",
                [],
                default=0,
                desc="Allow both camera modes or restrict players to one.",
                min_app_version=AppVersion(main="2.0"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Jar Refund",
                "JarRefund",
                [],
                default=60,
                min_app_version=AppVersion(main="2.0"),
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_ENEMY_DIFFICULTY_CHOICES),
                "Enemy Difficulty",
                "EnemyDifficulty",
                [],
                default=0,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_ZOMBIE_FERAL_SENSE_CHOICES),
                "Zombie Feral Sense",
                "ZombieFeralSense",
                [],
                default=0,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Move",
                "ZombieMove",
                [],
                default=0,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Move Night",
                "ZombieMoveNight",
                [],
                default=3,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Feral Move",
                "ZombieFeralMove",
                [],
                default=3,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Blood Moon Move",
                "ZombieBMMove",
                [],
                default=3,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(_AI_SMELL_MODE_CHOICES),
                "Ai Smell Mode",
                "AISmellMode",
                [],
                default=3,
                min_app_version=AppVersion(main="2.0"),
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Blood Moon Frequency",
                "BloodMoonFrequency",
                [],
                default=7,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Blood Moon Range",
                "BloodMoonRange",
                [],
                default=0,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(allow_negative=True),
                "Blood Moon Warning",
                "BloodMoonWarning",
                [],
                default=8,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Loot Abundance",
                "LootAbundance",
                [],
                default=100,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(allow_negative=True),
                "Loot Respawn Days",
                "LootRespawnDays",
                [],
                default=7,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Air Drop Frequency",
                "AirDropFrequency",
                [],
                default=72,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Air Drop Marker",
                "AirDropMarker",
                [],
                default=True,
                max_app_version=AppVersion(main="2.6"),
            ),
            Setting[int](
                IntSettingSpec(),
                "Party Shared Kill Range",
                "PartySharedKillRange",
                [],
                default=100,
                desc="Distance for party shared kill XP and quest credit.",
            ),
            Setting[int](
                IntSettingSpec(_PLAYER_KILLING_MODE_CHOICES),
                "Player Killing Mode",
                "PlayerKillingMode",
                [],
                default=3,
                desc="Controls who players can damage in PvP.",
            ),
            Setting[int](
                IntSettingSpec(),
                "Quest Progression Daily Limit",
                "QuestProgressionDailyLimit",
                [],
                default=4,
                max_app_version=AppVersion(main="2.6"),
            ),
        ]
        super().__init__(pointer, options, version_getter=version_getter)

    @property
    def options(self) -> list[Setting[Any]]:
        self._refresh_save_setting_choices()
        return super().options

    def _refresh_save_setting_choices(self, *, current_target: tuple[str, str] | None = None) -> None:
        world_setting = self._setting_for_key(_SEVENDAYS_GAME_WORLD_SETTING_KEY)
        save_targets = self._discovered_save_targets()
        if current_target is None:
            current_target = self._current_save_target()
        selection_targets = self._unique_save_targets(*save_targets, current_target)
        choices = [
            ChoiceOption(_SEVENDAYS_NEW_RWG_WORLD_SELECTION, "Generate Fresh Random World"),
            *(
                ChoiceOption(
                    _sevendays_fresh_generated_world_selection(game_world=game_world),
                    f"{game_world} / Fresh Characters",
                )
                for game_world in self._discovered_generated_world_names()
            ),
            *(
                ChoiceOption(
                    _sevendays_existing_save_selection(game_world=game_world, game_name=game_name),
                    f"{game_world} / {game_name}",
                )
                for game_world, game_name in selection_targets
            ),
        ]
        if not isinstance(world_setting.spec, StringSettingSpec):
            raise TypeError(f"7D2D world save setting {world_setting.key!r} must be a string setting.")
        world_setting.spec.choice_spec = ChoiceSpec(*choices)

    @staticmethod
    def _unique_save_targets(*values: tuple[str, str] | None) -> tuple[tuple[str, str], ...]:
        choices: list[tuple[str, str]] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            game_world, game_name = value
            try:
                choice = (
                    _normalise_sevendays_save_segment(game_world, label="Game world"),
                    _normalise_sevendays_save_segment(game_name, label="Save name"),
                )
            except ValueError:
                continue
            key = f"{choice[0].casefold()}\x00{choice[1].casefold()}"
            if key in seen:
                continue
            choices.append(choice)
            seen.add(key)
        return tuple(choices)

    def _current_save_target(self) -> tuple[str, str] | None:
        world_setting = self._setting_for_key(_SEVENDAYS_GAME_WORLD_SETTING_KEY)
        name_setting = self._setting_for_key(_SEVENDAYS_GAME_NAME_SETTING_KEY)
        if not isinstance(world_setting.value, str) or not isinstance(name_setting.value, str):
            return None
        try:
            selection = _sevendays_save_target_from_selection(world_setting.value)
            if selection.game_world is None or selection.game_name is None:
                return None
            return (selection.game_world, selection.game_name)
        except ValueError:
            try:
                return (
                    _normalise_sevendays_save_segment(world_setting.value, label="Game world"),
                    _normalise_sevendays_save_segment(name_setting.value, label="Save name"),
                )
            except ValueError:
                return None

    def _settings_userdata_root_path(self) -> Path | None:
        raw_value = _read_serverconfig_value(self.pointer, "UserDataFolder")
        if raw_value is None:
            return None
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = (self.pointer.parent / candidate).resolve()
        return candidate

    def _discovered_save_targets(self) -> tuple[tuple[str, str], ...]:
        saves_root = self._settings_userdata_root_path()
        if saves_root is None:
            return ()
        saves_root = saves_root / "Saves"
        if not saves_root.is_dir():
            return ()
        save_targets: list[tuple[str, str]] = []
        world_directories = sorted(
            (path for path in saves_root.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.name.casefold(),
        )
        for world_directory in world_directories:
            save_directories = sorted(
                (path for path in world_directory.iterdir() if path.is_dir() and not path.name.startswith(".")),
                key=lambda path: path.name.casefold(),
            )
            save_targets.extend((world_directory.name, save_directory.name) for save_directory in save_directories)
        return tuple(save_targets)

    def _discovered_generated_world_names(self) -> tuple[str, ...]:
        userdata_root = self._settings_userdata_root_path()
        if userdata_root is None:
            return ()
        generated_worlds_root = userdata_root / "GeneratedWorlds"
        if not generated_worlds_root.is_dir():
            return ()
        return tuple(
            path.name
            for path in sorted(
                (path for path in generated_worlds_root.iterdir() if path.is_dir() and not path.name.startswith(".")),
                key=lambda path: path.name.casefold(),
            )
        )

    def _rwgmixer_tree(self) -> ET.ElementTree[ET.Element[str]]:
        if not self.rwgmixer_pointer.exists():
            raise FileNotFoundError(f"7D2D rwgmixer.xml missing: {self.rwgmixer_pointer}")
        return cast("ET.ElementTree[ET.Element[str]]", ET.parse(self.rwgmixer_pointer))

    @staticmethod
    def _find_trader_adjustments(root: ET.Element) -> dict[str, ET.Element]:
        adjustments: dict[str, ET.Element] = {}
        expected_partial_names = {definition.partial_name for definition in _TRADER_BIOME_DEFINITIONS}
        for element in root.findall(".//prefab_spawn_adjust"):
            partial_name = element.attrib.get("partial_name")
            if partial_name not in expected_partial_names:
                continue
            if partial_name in adjustments:
                raise ValueError(f"Duplicate trader rwgmixer entry for {partial_name}")
            adjustments[partial_name] = element
        return adjustments

    def _setting_for_key(self, key: str) -> Setting[Any]:
        setting = self.get_setting(key)
        if setting is None:
            raise ValueError(f"Missing expected 7D2D setting: {key}")
        return setting

    def _effective_setting_value(self, key: str, drafts: dict[str, DraftSettingValue]) -> object:
        setting = self._setting_for_key(key)
        draft_value = drafts.get(key, hikari.UNDEFINED)
        if isinstance(draft_value, hikari.UndefinedType):
            return setting.value
        return draft_value

    def _validate_trader_biome_assignments(self) -> None:
        seen_biomes: dict[str, str] = {}
        for definition in _TRADER_BIOME_DEFINITIONS:
            setting = self._setting_for_key(definition.key)
            if not isinstance(setting.value, str):
                raise ValueError(f"{setting.label} must be a string biome value.")
            biome_value = setting.value
            if biome_value not in _TRADER_BIOME_VALUES:
                raise ValueError(f"{setting.label} has unsupported biome {biome_value!r}.")
            previous_label = seen_biomes.get(biome_value)
            if previous_label is not None:
                raise ValueError(
                    f"Trader biome assignments overlap: {previous_label} and {setting.label} both use {biome_value}."
                )
            seen_biomes[biome_value] = setting.label
        if seen_biomes.keys() != _TRADER_BIOME_VALUES:
            missing_biomes = sorted(_TRADER_BIOME_VALUES - seen_biomes.keys())
            raise ValueError(
                f"Trader biome assignments must cover each biome exactly once. Missing: {', '.join(missing_biomes)}"
            )

    def apply_draft_update(
        self,
        *,
        setting: Setting[Any],
        value: object,
        drafts: dict[str, DraftSettingValue],
    ) -> None:
        if setting.key == _SEVENDAYS_GAME_WORLD_SETTING_KEY:
            if not isinstance(value, str):
                raise TypeError("7D2D world save selection must be a string.")
            selected_target = _sevendays_save_target_from_selection(value)
            super().apply_draft_update(setting=setting, value=value, drafts=drafts)
            name_setting = self._setting_for_key(_SEVENDAYS_GAME_NAME_SETTING_KEY)
            if selected_target.game_name is not None:
                super().apply_draft_update(setting=name_setting, value=selected_target.game_name, drafts=drafts)
            return
        if setting.key == _SEVENDAYS_GAME_NAME_SETTING_KEY:
            world_selection = self._effective_setting_value(_SEVENDAYS_GAME_WORLD_SETTING_KEY, drafts)
            if not isinstance(world_selection, str):
                raise TypeError("7D2D world save selection must be a string.")
            if not _sevendays_save_target_from_selection(world_selection).requires_fresh_save_name:
                raise ValueError("Choose a fresh-world option before changing the fresh save name.")
            super().apply_draft_update(setting=setting, value=value, drafts=drafts)
            return
        if setting.key not in _TRADER_BIOME_KEYS:
            super().apply_draft_update(setting=setting, value=value, drafts=drafts)
            return
        if not isinstance(value, str):
            raise TypeError(f"Trader biome setting {setting.key!r} must resolve to a string value.")

        current_value = self._effective_setting_value(setting.key, drafts)
        if not isinstance(current_value, str):
            raise TypeError(f"Trader biome setting {setting.key!r} has non-string current value.")

        swap_definition: TraderBiomeDefinition | None = None
        for definition in _TRADER_BIOME_DEFINITIONS:
            if definition.key == setting.key:
                continue
            if self._effective_setting_value(definition.key, drafts) == value:
                swap_definition = definition
                break

        super().apply_draft_update(setting=setting, value=value, drafts=drafts)
        if swap_definition is None or value == current_value:
            return

        swap_setting = self._setting_for_key(swap_definition.key)
        super().apply_draft_update(setting=swap_setting, value=current_value, drafts=drafts)

    def load(self):
        data = ET.parse(self.pointer).getroot().findall("property")
        if not isinstance(data, list):
            raise ValueError(f"config must be list not `{type(data)}`")

        stored_world: str | None = None
        stored_name: str | None = None
        for element in data:
            property_name = element.attrib.get("name")
            raw_value = element.attrib.get("value")
            if property_name == _SEVENDAYS_GAME_WORLD_SETTING_KEY:
                stored_world = raw_value
                continue
            if property_name == _SEVENDAYS_GAME_NAME_SETTING_KEY:
                stored_name = raw_value
                continue
            for opt in self.options:
                if property_name == opt.key:
                    if raw_value is None:
                        raise ValueError(f"7D2D setting {opt.key!r} is missing its value.")
                    try:
                        opt.load_value(raw_value)
                    except ValueError:
                        if raw_value.strip():
                            raise
                        opt.value = opt.default
                        log.warning(
                            "7D2D stored setting was blank; keeping default: key=%s label=%s default=%s",
                            opt.key,
                            opt.label,
                            opt.serialise_value(),
                        )

        world_setting = self._setting_for_key(_SEVENDAYS_GAME_WORLD_SETTING_KEY)
        name_setting = self._setting_for_key(_SEVENDAYS_GAME_NAME_SETTING_KEY)
        game_world = _normalise_sevendays_save_segment(stored_world or "Navezgane", label="Game world")
        game_name = _normalise_sevendays_save_segment(stored_name or "MyGame", label="Save name")
        name_setting.load_value(game_name)
        configured_target = (game_world, game_name)
        saved_targets = self._discovered_save_targets()
        if configured_target in saved_targets:
            selection = _sevendays_existing_save_selection(game_world=game_world, game_name=game_name)
        elif game_world == "RWG":
            selection = _SEVENDAYS_NEW_RWG_WORLD_SELECTION
        elif game_world in self._discovered_generated_world_names():
            selection = _sevendays_fresh_generated_world_selection(game_world=game_world)
        else:
            selection = _sevendays_existing_save_selection(game_world=game_world, game_name=game_name)
        self._refresh_save_setting_choices(current_target=configured_target)
        world_setting.load_value(selection)

        rwg_root = self._rwgmixer_tree().getroot()
        adjustments = self._find_trader_adjustments(rwg_root)
        for definition in _TRADER_BIOME_DEFINITIONS:
            element = adjustments.get(definition.partial_name)
            if element is None:
                raise ValueError(f"rwgmixer.xml is missing prefab_spawn_adjust for {definition.partial_name}")
            biome_value = element.attrib.get("biomeTags")
            if biome_value is None:
                raise ValueError(f"rwgmixer.xml prefab_spawn_adjust for {definition.partial_name} is missing biomeTags")
            self._setting_for_key(definition.key).load_value(biome_value)
        self._validate_trader_biome_assignments()

    def save(self):
        self._validate_trader_biome_assignments()
        _ensure_serverconfig_userdata_redirect(self.pointer)

        world_setting = self._setting_for_key(_SEVENDAYS_GAME_WORLD_SETTING_KEY)
        name_setting = self._setting_for_key(_SEVENDAYS_GAME_NAME_SETTING_KEY)
        if not isinstance(world_setting.value, str) or not isinstance(name_setting.value, str):
            raise TypeError("7D2D world save settings must be strings.")
        selected_target = _sevendays_save_target_from_selection(world_setting.value)
        if selected_target.game_world is None:
            game_world = "RWG"
            game_name = _normalise_sevendays_save_segment(name_setting.value, label="New save name")
        elif selected_target.game_name is None:
            game_world = selected_target.game_world
            game_name = _normalise_sevendays_save_segment(name_setting.value, label="Fresh save name")
        else:
            game_world = selected_target.game_world
            game_name = selected_target.game_name
            name_setting.value = game_name
        server_world_values = {
            _SEVENDAYS_GAME_WORLD_SETTING_KEY: game_world,
            _SEVENDAYS_GAME_NAME_SETTING_KEY: game_name,
        }

        tree = ET.parse(self.pointer)
        root = tree.getroot()
        data = root.findall("property")
        if not isinstance(data, list):
            raise ValueError(f"config must be list not `{type(data)}`")

        for element in data:
            for opt in self.options:
                if element.attrib.get("name") == opt.key:
                    element.attrib["value"] = server_world_values.get(opt.key, opt.serialise_value())

        tree.write(self.pointer, encoding=config.STR_ENCODE)

        rwg_tree = self._rwgmixer_tree()
        rwg_root = rwg_tree.getroot()
        adjustments = self._find_trader_adjustments(rwg_root)
        for definition in _TRADER_BIOME_DEFINITIONS:
            setting = self._setting_for_key(definition.key)
            if not isinstance(setting.value, str):
                raise ValueError(f"{setting.label} must serialise to a string biome value.")
            element = adjustments.get(definition.partial_name)
            if element is None:
                raise ValueError(f"rwgmixer.xml is missing prefab_spawn_adjust for {definition.partial_name}")
            element.attrib["biomeTags"] = setting.value
        rwg_tree.write(self.rwgmixer_pointer, encoding=config.STR_ENCODE)
        return data


class SevenDays(App[App_Config]):
    chat_relay_outbound = True
    relay_notice_player_session_supported = True
    relay_notice_player_death_supported = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0xB91C1C
        self.proc_name = "7DaysToDie"
        self.proc_cmd = ["7DaysToDieServer", "-nographics"]

        self.process = None
        file_settings = cfg.directory.absolute() / "serverconfig.xml"
        self.cmd_start = ["bash", "startserver.sh", f"-configfile={file_settings.name}"]
        super().__init__(bot, am, cfg, SevenDays_Settings(file_settings, version_getter=lambda: cfg.version), Mod_7D2D)
        self.act_err_threshold = 100
        if cfg.steam_update is not None:
            self.updater = SteamCmd_Update_Manager(self)
        self.apply_version(
            detect_sevendays_version(directory=cfg.directory, server_log=cfg.server_log_file),
            persist=False,
        )

        self._telnet_port = _sevendays_telnet_port(file_settings)
        self._relay = TelnetClient(self.check_running, self._telnet_port)
        self._tail: Tailer | None = None
        self._tail_matchers: set[Callable[[str], Awaitable[None]]] = set()
        self._server_ready = asyncio.Event()
        self._startup_sandbox_options_task: asyncio.Task[None] | None = None
        self._telnet_startup_error: str | None = None
        self.am_receiver = Receiver(self)
        self._players = Players(self)
        self._activities = Activities(self)
        self._matchers = Matchers(self)

        log.debug(f"{__name__}.Created")

    def detect_installed_version(self) -> AppVersion | None:
        return detect_sevendays_version(directory=self.cfg.directory, server_log=self.cfg.server_log_file)

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return self.available_console_actions(_SEVENDAYS_CONSOLE_ACTIONS)

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Config",
                path=self.directory / "serverconfig.xml",
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset({".xml"}),
            ),
            AppConfigFileRoot(
                id="rwg-mixer",
                label="RWG Mixer",
                path=self.directory / "Data" / "Config" / "rwgmixer.xml",
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset({".xml"}),
            ),
        )

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        saves_root = self._save_container_path()
        if saves_root is None:
            return ()

        roots_by_path: dict[Path, AppSaveRoot] = {}
        for save_path in self._discovered_save_directory_paths():
            root = self._save_root_for_path(saves_root=saves_root, save_path=save_path)
            roots_by_path[root.path] = root

        configured_save_path = self._save_directory_path()
        if configured_save_path is not None and configured_save_path not in roots_by_path:
            root = self._save_root_for_path(saves_root=saves_root, save_path=configured_save_path)
            roots_by_path[root.path] = root

        generated_worlds_root = self._generated_worlds_container_path()
        if generated_worlds_root is not None:
            for generated_world_path in self._discovered_generated_world_directory_paths():
                root = self._generated_world_root_for_path(
                    generated_worlds_root=generated_worlds_root,
                    generated_world_path=generated_world_path,
                )
                roots_by_path[root.path] = root

        return tuple(sorted(roots_by_path.values(), key=self._save_root_sort_key))

    @property
    def supports_save_uploads(self) -> bool:
        return self._save_container_path() is not None

    @property
    def supports_save_delete(self) -> bool:
        return bool(self.save_file_roots)

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        if self.check_running():
            raise ValueError("Stop the server before uploading 7 Days to Die saves.")
        serverconfig_path = self.directory / "serverconfig.xml"
        if _read_serverconfig_value(serverconfig_path, "UserDataFolder") is None:
            _ensure_serverconfig_userdata_redirect(serverconfig_path)
        target = self._save_upload_target(root_id)
        root = target.root
        if Path(upload_name).suffix.casefold() != ".zip":
            raise ValueError("7 Days to Die save uploads must be .zip archives.")
        inspection = inspect_sevendays_save_archive(source_path)
        if target.kind is SevenDaysUploadKind.WORLD:
            if not inspection.includes_generated_world:
                raise ValueError("World uploads must contain generated-world files.")
            if inspection.includes_save and target.save_root is None:
                raise ValueError("World-and-save uploads require a save name.")
        elif inspection.includes_generated_world or not inspection.includes_save:
            raise ValueError("Save uploads must contain save files only, without a generated world.")
        if target.kind is SevenDaysUploadKind.SAVE and inspection.game_world not in {None, root.label}:
            raise ValueError(
                "Save world name does not match the selected world: "
                f"{inspection.game_world!r} and {root.label!r}."
            )
        save_root = target.save_root or (root if target.kind is SevenDaysUploadKind.SAVE else None)
        destination = save_root.resolved_path if save_root is not None else None
        if destination is not None and destination.exists():
            if save_root is None:
                raise RuntimeError("Save upload target unexpectedly missing.")
            raise FileExistsError(f"7 Days to Die save already exists: {save_root.label} / {save_root.path.name}")
        generated_world_destination: Path | None = None
        if inspection.generated_world is not None:
            target_world_name = root.path.name if target.kind is SevenDaysUploadKind.WORLD else root.label
            if inspection.generated_world != target_world_name:
                raise ValueError(
                    "Generated world name does not match the selected save world: "
                    f"{inspection.generated_world!r} and {target_world_name!r}."
                )
            generated_world_destination = self._generated_world_directory_path(target_world_name)
            if generated_world_destination.exists():
                raise FileExistsError(f"7 Days to Die generated world already exists: {root.label}")
        temp_parent = (destination or root.resolved_path).parent
        temp_parent.mkdir(parents=True, exist_ok=True)
        if generated_world_destination is not None:
            generated_world_destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as temp_dir:
            extracted_path = Path(temp_dir) / destination.name if inspection.includes_save and destination is not None else None
            extracted_generated_world_path = Path(temp_dir) / "GeneratedWorld"
            extract_sevendays_save_archive(
                archive_path=source_path,
                destination=extracted_path,
                generated_world_destination=(
                    extracted_generated_world_path if generated_world_destination is not None else None
                ),
                inspection=inspection,
            )
            if extracted_path is not None and destination is not None:
                File_Utils.remove(destination, silent=True, resolve=False)
                File_Utils.move(extracted_path, destination, overwrite=False)
            if generated_world_destination is not None:
                File_Utils.remove(generated_world_destination, silent=True, resolve=False)
                File_Utils.move(extracted_generated_world_path, generated_world_destination, overwrite=False)
        if not inspection.includes_save:
            if generated_world_destination is None:
                raise RuntimeError("Generated-world upload did not provide a generated world.")
            return describe_app_save_path(
                root=root,
                path=generated_world_destination,
                relative_path=generated_world_destination.name,
            )
        if save_root is None or destination is None:
            raise RuntimeError("World-and-save upload did not provide a save target.")
        return describe_app_save_path(root=save_root, path=destination, relative_path=destination.name)

    async def download_save_archive(self, file_id: str) -> tuple[str, Path] | None:
        save_path = self.resolve_save_file(file_id)
        root_id, _separator, _relative_path = file_id.partition("/")
        save_root = get_app_save_root(self.save_file_roots, root_id)
        if save_root.id.startswith("world-"):
            return None
        generated_world_path = self._generated_world_directory_path(save_root.label)
        if not generated_world_path.is_dir():
            return None
        userdata_root = self._userdata_root_path()
        if userdata_root is None:
            raise ValueError("7 Days to Die save download requires a configured UserDataFolder.")
        archive_name = f"{self.name}_{save_root.label}_{save_path.name}.zip"
        archive_path = await File_Utils.compress(
            (save_path, generated_world_path),
            archive_name,
            arc_base=userdata_root,
        )
        return (archive_path.name, archive_path)

    @staticmethod
    def new_save_upload_root_id(*, game_world: str, game_name: str) -> str:
        normalised_world = _normalise_sevendays_save_segment(game_world, label="Game world")
        normalised_name = _normalise_sevendays_save_segment(game_name, label="Save name")
        return (
            f"{_SEVENDAYS_NEW_SAVE_ROOT_PREFIX}"
            f"{quote(normalised_world, safe='')}/{quote(normalised_name, safe='')}"
        )

    @staticmethod
    def new_world_upload_root_id(*, game_world: str, game_name: str | None = None) -> str:
        normalised_world = _normalise_sevendays_save_segment(game_world, label="Game world")
        if game_name is None:
            return f"{_SEVENDAYS_NEW_WORLD_ROOT_PREFIX}{quote(normalised_world, safe='')}"
        normalised_name = _normalise_sevendays_save_segment(game_name, label="Save name")
        return (
            f"{_SEVENDAYS_NEW_WORLD_ROOT_PREFIX}"
            f"{quote(normalised_world, safe='')}/{quote(normalised_name, safe='')}"
        )

    def _save_upload_target(self, root_id: str) -> SevenDaysUploadTarget:
        if root_id.startswith(_SEVENDAYS_NEW_SAVE_ROOT_PREFIX):
            return SevenDaysUploadTarget(SevenDaysUploadKind.SAVE, self._new_save_upload_target(root_id))
        if root_id.startswith(_SEVENDAYS_NEW_WORLD_ROOT_PREFIX):
            return self._new_world_upload_target(root_id)
        raise ValueError("7 Days to Die uploads must import a new world or save; delete an old one first.")

    def _new_world_upload_target(self, root_id: str) -> SevenDaysUploadTarget:
        generated_worlds_root = self._generated_worlds_container_path()
        if generated_worlds_root is None:
            raise ValueError("7 Days to Die world uploads require a configured UserDataFolder.")
        encoded_target = root_id.removeprefix(_SEVENDAYS_NEW_WORLD_ROOT_PREFIX)
        encoded_world, separator, encoded_name = encoded_target.partition("/")
        game_world = _normalise_sevendays_save_segment(
            unquote(encoded_world),
            label="Game world",
        )
        world_root = self._generated_world_root_for_path(
            generated_worlds_root=generated_worlds_root,
            generated_world_path=generated_worlds_root / game_world,
        )
        if not separator:
            return SevenDaysUploadTarget(SevenDaysUploadKind.WORLD, world_root)
        game_name = _normalise_sevendays_save_segment(unquote(encoded_name), label="Save name")
        saves_root = self._save_container_path()
        if saves_root is None:
            raise ValueError("7 Days to Die save uploads require a configured UserDataFolder.")
        return SevenDaysUploadTarget(
            SevenDaysUploadKind.WORLD,
            world_root,
            self._save_root_for_path(saves_root=saves_root, save_path=saves_root / game_world / game_name),
        )

    def _new_save_upload_target(self, root_id: str) -> AppSaveRoot:
        saves_root = self._save_container_path()
        if saves_root is None:
            raise ValueError("7 Days to Die save uploads require a configured UserDataFolder.")
        encoded_target = root_id.removeprefix(_SEVENDAYS_NEW_SAVE_ROOT_PREFIX)
        encoded_world, separator, encoded_name = encoded_target.partition("/")
        if not separator:
            raise ValueError("New 7 Days to Die save target is invalid.")
        game_world = _normalise_sevendays_save_segment(unquote(encoded_world), label="Game world")
        game_name = _normalise_sevendays_save_segment(unquote(encoded_name), label="Save name")
        return self._save_root_for_path(saves_root=saves_root, save_path=saves_root / game_world / game_name)

    def delete_save_file(self, *, file_id: str) -> AppSaveEntry:
        if self.check_running():
            raise ValueError("Stop the server before deleting 7 Days to Die saves.")
        try:
            current_save = next(save for save in self.list_save_files() if save.id == file_id)
        except StopIteration as xcp:
            raise FileNotFoundError(f"Unknown save file: {file_id}") from xcp
        save_path = self.resolve_save_file(file_id)
        root_id, _separator, _relative_path = file_id.partition("/")
        root = get_app_save_root(self.save_file_roots, root_id)
        if root.id.startswith("world-"):
            saves_root = self._save_container_path()
            associated_saves = saves_root / save_path.name if saves_root is not None else None
            if associated_saves is not None and associated_saves.is_dir() and any(associated_saves.iterdir()):
                raise ValueError(
                    f"Delete the saves for generated world {save_path.name!r} before deleting its terrain."
                )
        File_Utils.remove(save_path, silent=False, resolve=False)
        if root.id.startswith("save-"):
            try:
                save_path.parent.rmdir()
            except OSError:
                pass
        return current_save

    def _save_directory_path(self) -> Path | None:
        saves_root = self._save_container_path()
        if saves_root is None:
            return None
        game_world = self._serverconfig_setting_value("GameWorld")
        game_name = self._serverconfig_setting_value("GameName")
        if game_world is None or game_name is None:
            return None
        return saves_root / game_world / game_name

    def _save_container_path(self) -> Path | None:
        userdata_root = self._userdata_root_path()
        if userdata_root is None:
            return None
        return userdata_root / "Saves"

    def _generated_worlds_container_path(self) -> Path | None:
        userdata_root = self._userdata_root_path()
        if userdata_root is None:
            return None
        return userdata_root / "GeneratedWorlds"

    def _generated_world_directory_path(self, game_world: str) -> Path:
        world_name = _normalise_sevendays_save_segment(game_world, label="Game world")
        generated_worlds_root = self._generated_worlds_container_path()
        if generated_worlds_root is None:
            raise ValueError("7 Days to Die generated worlds require a configured UserDataFolder.")
        return generated_worlds_root / world_name

    def _discovered_save_directory_paths(self) -> tuple[Path, ...]:
        saves_root = self._save_container_path()
        if saves_root is None or not saves_root.is_dir():
            return ()
        discovered: list[Path] = []
        world_directories = sorted(
            (path for path in saves_root.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.name.casefold(),
        )
        for world_directory in world_directories:
            save_directories = sorted(
                (path for path in world_directory.iterdir() if path.is_dir() and not path.name.startswith(".")),
                key=lambda path: path.name.casefold(),
            )
            discovered.extend(save_directories)
        return tuple(discovered)

    def _discovered_generated_world_directory_paths(self) -> tuple[Path, ...]:
        generated_worlds_root = self._generated_worlds_container_path()
        if generated_worlds_root is None or not generated_worlds_root.is_dir():
            return ()
        return tuple(
            sorted(
                (path for path in generated_worlds_root.iterdir() if path.is_dir() and not path.name.startswith(".")),
                key=lambda path: path.name.casefold(),
            )
        )

    def _save_root_for_path(self, *, saves_root: Path, save_path: Path) -> AppSaveRoot:
        relative_path = PurePosixPath(save_path.resolve().relative_to(saves_root.resolve()).as_posix())
        if len(relative_path.parts) < 2:
            raise ValueError(f"7 Days to Die save path must be under a world directory: {save_path}")
        return AppSaveRoot(
            id=self._save_root_id(relative_path),
            label=relative_path.parts[0],
            path=save_path,
            mode=AppSaveRootMode.SELF,
            include_files=False,
            include_directories=True,
        )

    def _generated_world_root_for_path(
        self,
        *,
        generated_worlds_root: Path,
        generated_world_path: Path,
    ) -> AppSaveRoot:
        relative_path = PurePosixPath(
            generated_world_path.resolve().relative_to(generated_worlds_root.resolve()).as_posix()
        )
        if len(relative_path.parts) != 1:
            raise ValueError(f"7 Days to Die generated world path is invalid: {generated_world_path}")
        return AppSaveRoot(
            id=self._generated_world_root_id(relative_path),
            label=f"World: {relative_path.name}",
            path=generated_world_path,
            mode=AppSaveRootMode.SELF,
            include_files=False,
            include_directories=True,
        )

    @staticmethod
    def _save_root_id(relative_path: PurePosixPath) -> str:
        digest = hashlib.sha1(relative_path.as_posix().encode(config.STR_ENCODE), usedforsecurity=False).hexdigest()
        return f"save-{digest[:12]}"

    @staticmethod
    def _generated_world_root_id(relative_path: PurePosixPath) -> str:
        digest = hashlib.sha1(relative_path.as_posix().encode(config.STR_ENCODE), usedforsecurity=False).hexdigest()
        return f"world-{digest[:12]}"

    @staticmethod
    def _save_root_sort_key(root: AppSaveRoot) -> tuple[str, str, str]:
        return (
            root.label.casefold(),
            root.path.name.casefold(),
            root.path.as_posix().casefold(),
        )

    def _userdata_root_path(self) -> Path | None:
        raw_value = _read_serverconfig_value(self.directory / "serverconfig.xml", "UserDataFolder")
        candidate = Path(raw_value or _SEVENDAYS_MANAGED_USERDATA_FOLDER).expanduser()
        if not candidate.is_absolute():
            candidate = (self.directory / candidate).resolve()
        return candidate

    def _serverconfig_setting_value(self, key: str) -> str | None:
        raw_value = _read_serverconfig_value(self.directory / "serverconfig.xml", key)
        if raw_value is None:
            return None
        return raw_value.strip() or None

    async def _configure_telnet_client(self) -> None:
        telnet_port = _sevendays_telnet_port(self.directory / "serverconfig.xml")
        if telnet_port == self._telnet_port:
            return
        await self._relay.teardown()
        self._telnet_port = telnet_port
        self._relay = TelnetClient(self.check_running, telnet_port)

    async def _cleanup_runtime_component(
        self,
        label: str,
        operation: Callable[[], Awaitable[object]],
    ) -> None:
        try:
            await operation()
        except Exception:
            log.exception("Failed to clean up %s for %s", label, self.name)

    async def _cleanup_runtime_components(self) -> None:
        startup_sandbox_options_task = getattr(self, "_startup_sandbox_options_task", None)
        self._startup_sandbox_options_task = None
        if startup_sandbox_options_task is not None:
            startup_sandbox_options_task.cancel()
            try:
                await startup_sandbox_options_task
            except asyncio.CancelledError:
                pass
        await self._cleanup_runtime_component("player polling", self._players.stop)
        await self._cleanup_runtime_component("activity polling", self._activities.stop)
        if self._tail is not None:
            await self._cleanup_runtime_component("log tailer", self._tail.stop)
        await self._cleanup_runtime_component("Telnet", self._relay.teardown)

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        serverconfig_path = self.directory / "serverconfig.xml"
        if serverconfig_path.is_file():
            _ensure_serverconfig_userdata_redirect(serverconfig_path)
        self._server_ready.clear()
        self._telnet_startup_error = None
        await self._configure_telnet_client()
        previous_log_signatures = _snapshot_sevendays_runtime_logs(
            directory=self.directory,
            server_log=self.server_log,
        )
        await self._std_launch()

        try:
            while not self.check_running():
                log.debug(f"Waiting for {self.name}.check_running...")
                await asyncio.sleep(5)

            log.debug(f"{self.name}.running...")
            runtime_log = await _discover_sevendays_runtime_log(
                directory=self.directory,
                server_log=self.server_log,
                previous_log_signatures=previous_log_signatures,
                check_running=self.check_running,
            )
            if runtime_log is not None:
                File_Utils.link(runtime_log, self.file_stdout.with_name(runtime_log.name))
                self._tail = Tailer(self.check_running, runtime_log, self.file_stdout)
                await self._tail.start(self._tail_matchers)
                await self.wait_for_ready_event(
                    self._server_ready,
                    timeout_seconds=900.0,
                    ready_label="server readiness",
                )
                if self._telnet_startup_error is not None:
                    raise RuntimeError(f"7D2D Telnet failed to start: {self._telnet_startup_error}")
                await self._relay.setup()
            else:
                reader = await self._relay.setup()
                self._tail = Tailer(lambda: self._relay.connected_event, reader, self.file_stdout)
                await self._tail.start(self._tail_matchers)
                await self.wait_for_ready_event(
                    self._server_ready,
                    timeout_seconds=900.0,
                    ready_label="server readiness",
                )
            await self._players.start()
            await self._activities.start()
            self._running = True
            self._schedule_startup_sandbox_options_request()
            return True
        except Exception:
            self._running = False
            await self._cleanup_runtime_components()
            await self._terminate()
            raise

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        try:
            save_sent, shutdown_sent = await _request_graceful_shutdown(self)
            if not save_sent:
                log.warning("Could not request a world save before stopping %s", self.name)
            if not shutdown_sent:
                log.warning("Could not request graceful shutdown for %s; terminating the process", self.name)
        except Exception:
            log.exception("Failed to send graceful shutdown commands for %s", self.name)
        finally:
            await self._cleanup_runtime_components()
            await self._terminate()
        return True

    async def kill(self) -> bool:
        self._running = False
        await self._cleanup_runtime_components()
        await self._terminate()
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()

    @property
    def sandbox_options_path(self) -> Path:
        return self.directory / _SEVENDAYS_YUKIBOT_DATA_RELATIVE_PATH / _SEVENDAYS_SANDBOX_OPTIONS_FILE_NAME

    @property
    def sandbox_options_file_exists(self) -> bool:
        return self.sandbox_options_path.is_file()

    @property
    def supports_sevendays_sandbox_options(self) -> bool:
        return _sevendays_version_supports_sandbox_options(self.cfg.version)

    async def request_sandbox_options(self) -> bool:
        cfg = getattr(self, "cfg", None)
        app_version = getattr(cfg, "version", None)
        if not _sevendays_version_supports_sandbox_options(app_version):
            return False
        return bool(await self._relay.send("getsandboxoptions"))

    def _schedule_startup_sandbox_options_request(self) -> None:
        existing_task = getattr(self, "_startup_sandbox_options_task", None)
        if existing_task is not None and not existing_task.done():
            return
        self._startup_sandbox_options_task = asyncio.create_task(
            self._request_startup_sandbox_options(),
            name=f"{self.name}-startup-sandbox-options",
        )

    async def _request_startup_sandbox_options(
        self,
        *,
        delay_seconds: float = _SEVENDAYS_STARTUP_SANDBOX_OPTIONS_DELAY_SECONDS,
        max_attempts: int = _SEVENDAYS_STARTUP_SANDBOX_OPTIONS_MAX_ATTEMPTS,
    ) -> None:
        try:
            for attempt_index in range(max_attempts):
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                was_sent = await self.request_sandbox_options()
                if was_sent:
                    return
                if self.cfg.version is not None and not self.supports_sevendays_sandbox_options:
                    return
                if attempt_index + 1 >= max_attempts:
                    break
            if self.supports_sevendays_sandbox_options:
                log.warning("%s failed to request 7D2D sandbox options during startup", self.name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s failed while requesting 7D2D sandbox options during startup", self.name)
        finally:
            if getattr(self, "_startup_sandbox_options_task", None) is asyncio.current_task():
                self._startup_sandbox_options_task = None

    def load_sandbox_options_snapshot(self) -> SevenDaysSandboxOptionsSnapshot:
        pointer = self.sandbox_options_path
        try:
            raw_payload: object = json.loads(pointer.read_text(config.STR_ENCODE))
        except json.JSONDecodeError as xcp:
            raise ValueError(f"Invalid 7D2D sandbox options JSON at {pointer}: {xcp}") from xcp
        except OSError as xcp:
            raise ValueError(f"Unable to read 7D2D sandbox options at {pointer}: {xcp}") from xcp
        return SevenDaysSandboxOptionsSnapshot.from_mapping(
            _json_object(raw_payload, label="7D2D sandbox options snapshot")
        )

    def save_sandbox_options_snapshot(self, snapshot: SevenDaysSandboxOptionsSnapshot) -> None:
        pointer = self.sandbox_options_path
        try:
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps(snapshot.to_mapping(), indent=4, sort_keys=True) + "\n", config.STR_ENCODE)
        except OSError as xcp:
            raise ValueError(f"Unable to write 7D2D sandbox options at {pointer}: {xcp}") from xcp


class Receiver(AM_Receiver):
    def __init__(self, app: SevenDays) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        base_content = (
            payload.content_demojised_for_app(self.app)
            if hasattr(payload, "content_demojised_for_app")
            else payload.content_demojised
        )
        content = OutboundRelayFormatter.format_payload(
            payload,
            RelayOutboundFormatOptions(
                base_content=base_content,
                reference_renderer=render_plain_reference_prefix,
            ),
        )
        txt = f"say {_quote_console_argument(f'{payload.alias}: {content}')}"
        await self.app._relay.send(txt)


class Matchers:
    def __init__(self, app: SevenDays) -> None:
        self.app: SevenDays = app
        self._last_telnet: datetime = datetime.now()
        self._sandbox_options_active = False
        self._sandbox_code: str | None = None
        self._sandbox_section: str | None = None
        self._sandbox_options: list[SevenDaysSandboxOption] = []
        app._tail_matchers.add(self.match_version)
        app._tail_matchers.add(self.match_telnet_startup_error)
        app._tail_matchers.add(self.match_ready)
        app._tail_matchers.add(self.match_transiant)
        app._tail_matchers.add(self.match_chat)
        app._tail_matchers.add(self.match_death)
        app._tail_matchers.add(self.match_sandbox_options)

    async def match_version(self, line: str) -> None:
        if match := _SEVENDAYS_VERSION_RE.search(line):
            self.app.apply_version(_app_version_from_sevendays_text(match.group("version").strip()), persist=True)
            return
        if match := _SEVENDAYS_GAME_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)

    async def match_ready(self, line: str) -> None:
        if _SEVENDAYS_READY_RE.search(line):
            if not self.app._server_ready.is_set():
                log.info("%s matched 7D2D ready line: %s", self.app.name, line)
                self.app._server_ready.set()

    async def match_telnet_startup_error(self, line: str) -> None:
        if _SEVENDAYS_TELNET_STARTUP_ERROR_RE.search(line):
            self.app._telnet_startup_error = line

    async def match_sandbox_options(self, line: str) -> None:
        if match := _SEVENDAYS_SANDBOX_CODE_RE.search(line):
            self._sandbox_options_active = False
            self._sandbox_code = match.group("code").strip()
            self._sandbox_section = None
            self._sandbox_options = []
            return

        if _SEVENDAYS_SANDBOX_OPTIONS_RE.search(line):
            self._sandbox_options_active = True
            if self._sandbox_code is None:
                self._sandbox_options = []
            self._sandbox_section = None
            return

        if not self._sandbox_options_active:
            return

        if section_match := _SEVENDAYS_SANDBOX_SECTION_RE.search(line):
            self._sandbox_section = section_match.group("section").strip().title()
            return

        if option_match := _SEVENDAYS_SANDBOX_OPTION_RE.search(line):
            option = SevenDaysSandboxOption(
                section=self._sandbox_section or "Uncategorised",
                key=option_match.group("key"),
                value_index=int(option_match.group("value_index")),
                value_label=option_match.group("value_label"),
                default_index=int(option_match.group("default_index")),
                default_label=option_match.group("default_label"),
            )
            self._sandbox_options.append(option)
            snapshot = SevenDaysSandboxOptionsSnapshot(
                generated_at=datetime.now().isoformat(timespec="seconds"),
                sandbox_code=self._sandbox_code,
                app_version=self.app.cfg.version.display_value if self.app.cfg.version is not None else None,
                options=tuple(self._sandbox_options),
            )
            self.app.save_sandbox_options_snapshot(snapshot)

    async def match_transiant(self, line: str) -> None:
        if match := _SEVENDAYS_TRANSIENT_RE.search(line):
            player = match.group(1)
            action = str(match.group(2)).lower()
            if "join" in action:
                if self.app.relay_notice_player_joined_enabled is False:
                    return
                notice_action = PlayerSessionAction.JOINED
            else:
                if self.app.relay_notice_player_left_enabled is False:
                    return
                notice_action = PlayerSessionAction.LEFT
            notice: PlayerSessionNotice = self.app.player_session_notice(
                action=notice_action,
                source=RelayNoticeSource.APP_LOG,
            )
            app_friendly = getattr(self.app, "friendly", self.app.name)
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    render_notice_text(notice, author_name=player, app_name=app_friendly),
                    player or hikari.UNDEFINED,
                    notice=notice,
                )
            )

    async def match_chat(self, line: str) -> None:
        player = None
        if match := _SEVENDAYS_CHAT_RE.search(line):
            player = str(match.group(1)).strip("\r\n ")
            msg = str(match.group(2)).strip("\r\n ")
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player or hikari.UNDEFINED))

    async def match_death(self, line: str) -> None:
        if self.app.relay_notice_player_death_enabled is False:
            return
        if match := _SEVENDAYS_DEATH_RE.search(line):
            player = match.group("player")
            notice: GameDeathNotice = GameDeathNotice(
                death_kind=GameDeathKind.UNKNOWN,
                detail_text="died",
                source=RelayNoticeSource.APP_LOG,
            )
            app_friendly = getattr(self.app, "friendly", self.app.name)
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    render_notice_text(notice, author_name=player, app_name=app_friendly),
                    player or hikari.UNDEFINED,
                    notice=notice,
                )
            )


class Players:
    def __init__(self, app: SevenDays):
        self.app = app
        self._players_task: asyncio.Task[None] | None = None
        self._running = False
        self._online: int | None = None
        self._max: int | None = None
        app._tail_matchers.add(self.match_players)

    async def start(self):
        self._online = None
        self._max = None
        if self._players_task and not self._players_task.done():
            return
        self._running = True
        self._players_task = asyncio.create_task(self._listplayers())

    async def stop(self):
        self._online = None
        self._max = None
        self._running = False
        if self._players_task:
            self._players_task.cancel()
            try:
                await self._players_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("7D2D player polling task failed before shutdown for %s", self.app.name)
            finally:
                self._players_task = None

    async def match_players(self, line: str):
        current = maximum = None
        if "Total of" in line:
            current = self.extract_num(line)
            if current is not None:
                self._online = current
        elif "Max players" in line:
            maximum = self.extract_num(line)
            if maximum is not None:
                self._max = maximum
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Players: {current}/{maximum}")

    async def _listplayers(self):
        while self._running:
            if self._max is None and self.app._tail:
                log_lines = self.app._tail.specific_lines(0, 500)
                for line in log_lines:
                    if "Max players" in line:
                        log.debug("Found Max Players through log")
                        await self.match_players(line)
            await asyncio.sleep(5)
            await self.app._relay.send("listplayers")

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


class Activities:
    def __init__(self, app: SevenDays):
        self.app = app
        self._time_task: asyncio.Task[None] | None = None
        self._running = False
        self.providers: list[AppActivityProvider[SevenDays]] = [Provider_Time(app)]
        self.app.set_activity_providers(self.providers)
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self):
        self.tasks = {task for task in self.tasks if not task.done()}
        if self.tasks:
            return
        self._running = True
        self.app.register_enabled_activity_providers()
        for prov in self.providers:
            self.tasks.update(asyncio.create_task(func()) for func in prov.task_funcs)

    async def stop(self):
        self._running = False
        self.app.deregister_activity_providers()
        tasks = tuple(self.tasks)
        self.tasks.clear()
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("7D2D activity task failed before shutdown for %s", self.app.name)


class Provider_Time(AppActivityProvider[SevenDays]):
    metadata = AppActivityProviderMetadata(provider_id="time", label="Game Time")

    def __init__(self, app: SevenDays):
        super().__init__(app)
        self._time = None
        self._count = 0
        self.stats: dict[str, GameStatValue] = {}
        app._tail_matchers.add(self.match_time)
        app._tail_matchers.add(self.match_stats)
        self.task_funcs = (self._get_time, self._getgamestats)

    async def get(self) -> str | None:
        if not self._time:
            return None
        day = self._time[0]
        hour = self._time[1]
        zhm = self.stats.get("ZombieHordeMeter")
        # 75% sure ZHM represents the setting which controls the day being coloured red in game on horde day
        if zhm:
            bmd = self.stats.get("BloodMoonDay")
            bmw = self.stats.get("BloodMoonWarning")
            if isinstance(bmd, int) and isinstance(bmw, int):
                if day == bmd and hour >= bmw:
                    return f"!D{day}/H{hour}"
        return f"D{day}/H{hour:02d}"

    async def _get_time(self):
        while True:
            await asyncio.sleep(5)
            await self.app._relay.send("gettime")

    async def match_time(self, line: str):
        if not line.startswith("Day"):
            return
        day, time = line.split(",")
        day = day.split(" ")[-1].strip()
        hour, minute = time.strip().split(":")
        self._time = (int(day), int(hour), int(minute))
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Time: {self._time}")

    async def _getgamestats(self):
        while True:
            await asyncio.sleep(60)
            await self.app._relay.send("getgamestat")

    async def match_stats(self, line: str):
        if not line.startswith("GameStat"):
            return
        stat = line.split(".", 1)[-1].replace(" ", "")
        key, raw_value = stat.split("=", 1)
        val = parse_gamestat_value(raw_value)

        self.stats[key] = val
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Stats: {key}={self.stats[key]}")


# AiviA APasz
