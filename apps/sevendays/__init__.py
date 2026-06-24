import ast
import asyncio
import hashlib
import logging
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import hikari

import config
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
from apps._config import App_Config, AppVersion, Mod_Config, ModType
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult
from apps._mod import Mod
from apps._save_files import (
    AppSaveEntry,
    AppSaveRoot,
    AppSaveRootMode,
    describe_app_save_path,
    get_app_save_root,
    replace_directory_from_zip,
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

_SEVENDAYS_VERSION_RE = re.compile(
    r"Version:\s*V\s*(?P<version>\d+(?:\.\d+)*(?:\s*\([^)]+\)|[bB]\d+)?)",
    re.IGNORECASE,
)
_SEVENDAYS_GAME_VERSION_RE = re.compile(
    r"GamePref\.GameVersion\s*=\s*V\s*(?P<version>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_SEVENDAYS_VERSION_BUILD_RE = re.compile(
    r"(?P<main>\d+(?:\.\d+)*)(?:\s*\((?P<parenthesized>[^)]+)\)|(?P<suffix>[bB]\d+))",
    re.IGNORECASE,
)
_SEVENDAYS_READY_RE = re.compile(r"\bStartAsServer\b")
_SEVENDAYS_TRANSIENT_RE = re.compile(r"GMSG: Player '(.+?)' (joined|left) the game", re.IGNORECASE)
_SEVENDAYS_DEATH_RE = re.compile(r"GMSG: Player '(?P<player>.+?)' died\b", re.IGNORECASE)
_SEVENDAYS_CHAT_RE = re.compile(r"Chat.*?:\s*'(.*?)':\s*(.+)", re.IGNORECASE)
_SEVENDAYS_RUNTIME_LOG_DISCOVERY_TIMEOUT_SECONDS = 10.0
_SEVENDAYS_RUNTIME_LOG_DISCOVERY_POLL_SECONDS = 0.25
_SEVENDAYS_MANAGED_USERDATA_FOLDER = "userdata"


def _timestamped_sevendays_output_logs(*, directory: Path) -> tuple[Path, ...]:
    log_dir = directory / "7DaysToDieServer_Data"
    if not log_dir.is_dir():
        return ()
    candidates = sorted(
        log_dir.glob("output_log__*.txt"),
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
    explicit_candidates: tuple[Path | None, ...] = (
        server_log,
        directory / "server_stdout.log",
    )
    for pointer in explicit_candidates:
        if pointer is not None and pointer.exists():
            return pointer

    timestamped_logs: tuple[Path, ...] = _timestamped_sevendays_output_logs(directory=directory)
    if previous_timestamped_logs is not None:
        previous_log_set = frozenset(previous_timestamped_logs)
        for pointer in timestamped_logs:
            if pointer not in previous_log_set:
                return pointer
    for pointer in timestamped_logs:
        return pointer

    legacy_output_log = directory / "7DaysToDieServer_Data" / "output_log.txt"
    if legacy_output_log.exists():
        return legacy_output_log
    return None


def _stable_sevendays_runtime_log(*, directory: Path, server_log: Path | None) -> Path | None:
    candidates: tuple[Path | None, ...] = (
        server_log,
        directory / "server_stdout.log",
        directory / "7DaysToDieServer_Data" / "output_log.txt",
    )
    for pointer in candidates:
        if pointer is not None and pointer.exists():
            return pointer
    return None


def _launch_created_sevendays_runtime_log(
    *,
    directory: Path,
    previous_timestamped_logs: Collection[Path] | None = None,
) -> Path | None:
    previous_log_set = frozenset(previous_timestamped_logs or ())
    for pointer in _timestamped_sevendays_output_logs(directory=directory):
        if pointer not in previous_log_set:
            return pointer
    return None


async def _discover_sevendays_runtime_log(
    *,
    directory: Path,
    server_log: Path | None,
    previous_timestamped_logs: Collection[Path] | None = None,
    check_running: Callable[[], bool],
    timeout_seconds: float = _SEVENDAYS_RUNTIME_LOG_DISCOVERY_TIMEOUT_SECONDS,
    poll_seconds: float = _SEVENDAYS_RUNTIME_LOG_DISCOVERY_POLL_SECONDS,
) -> Path | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if runtime_log := _stable_sevendays_runtime_log(directory=directory, server_log=server_log):
            return runtime_log
        if runtime_log := _launch_created_sevendays_runtime_log(
            directory=directory,
            previous_timestamped_logs=previous_timestamped_logs,
        ):
            return runtime_log
        if not check_running() or asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(poll_seconds)


def _candidate_sevendays_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates: list[Path | None] = [
        server_log,
        directory / "server_stdout.log",
        *_latest_sevendays_output_logs(directory=directory),
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


async def _send_console_command(app: "SevenDays", command: str, *, success_text: str) -> ConsoleActionResult:
    was_sent = await app._relay.send(command)
    if not was_sent:
        raise RuntimeError(f"Failed to send console command: {command}")
    return ConsoleActionResult(summary=success_text)


async def _console_saveworld(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    return await _send_console_command(app, "saveworld", success_text=f"{app.friendly}: world save requested.")


async def _console_shutdown(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    return await _send_console_command(app, "shutdown", success_text=f"{app.friendly}: shutdown requested.")


async def _console_settime(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(SevenDays, app_obj)
    target = cast(str, value)
    return await _send_console_command(app, f"settime {target}", success_text=f"{app.friendly}: time command sent.")


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
        description="Gracefully stop the 7D2D server.",
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


class Mod_7D2D(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    @classmethod
    def iter_candidates(cls, folder: Path) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for pointer in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if not pointer.is_dir():
                continue
            if (pointer / "ModInfo.xml").exists() or (pointer / "ModInfo.xml.disabled").exists():
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
        enabled = (candidate / "ModInfo.xml").exists() and not (candidate / "ModInfo.xml.disabled").exists()
        return modcf_cls(name=candidate.name, directory=folder, enabled=enabled)

    @property
    def mod_info_enabled_path(self) -> Path:
        return self.enabled_path / "ModInfo.xml"

    @property
    def mod_info_disabled_path(self) -> Path:
        return self.enabled_path / "ModInfo.xml.disabled"

    @property
    def path(self) -> Path:
        return self.enabled_path

    def default_mod_type(self) -> ModType:
        if self.name in _SEVENDAYS_BUILTIN_MOD_NAMES:
            return ModType.BUILTIN
        return ModType.REGULAR

    def exists(self) -> bool:
        return self.enabled_path.is_dir() and (
            self.mod_info_enabled_path.exists() or self.mod_info_disabled_path.exists()
        )

    def sync_enabled_state(self) -> None:
        enabled_exists = self.mod_info_enabled_path.exists()
        disabled_exists = self.mod_info_disabled_path.exists()
        if enabled_exists and not disabled_exists:
            self.cfg.enabled = True
        elif disabled_exists and not enabled_exists:
            self.cfg.enabled = False

    def detect_version(self) -> str | None:
        self.sync_enabled_state()
        if self.mod_info_enabled_path.exists():
            return _read_modinfo_value(self.mod_info_enabled_path, "Version")
        if self.mod_info_disabled_path.exists():
            return _read_modinfo_value(self.mod_info_disabled_path, "Version")
        return None

    def detect_friendly(self) -> str | None:
        self.sync_enabled_state()
        if self.mod_info_enabled_path.exists():
            return _read_modinfo_value(self.mod_info_enabled_path, "DisplayName")
        if self.mod_info_disabled_path.exists():
            return _read_modinfo_value(self.mod_info_disabled_path, "DisplayName")
        return None

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_extr(src, atomic)

    async def _enable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await asyncio.to_thread(File_Utils.move, self.mod_info_disabled_path, self.mod_info_enabled_path)
        self.cfg.enabled = True
        return self.enabled_path

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await asyncio.to_thread(File_Utils.move, self.mod_info_enabled_path, self.mod_info_disabled_path)
        self.cfg.enabled = False
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
                StringSettingSpec(allow_blank=True),
                "Game World",
                "GameWorld",
                [],
                default="Navezgane",
                power_level=Power_Level.sudo,
                desc="Use `RWG` for a generated world or enter an existing world name.",
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                "World Gen Seed",
                "WorldGenSeed",
                [],
                default="MyGame",
                power_level=Power_Level.sudo,
                desc="Seed used when `GameWorld` is `RWG`. Existing generated worlds are reused.",
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
                StringSettingSpec(allow_blank=True),
                "Game Name",
                "GameName",
                [],
                default="MyGame",
                desc="Save name and decoration seed. It does not change the overall RWG layout.",
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

        for element in data:
            for opt in self.options:
                if element.attrib.get("name") == opt.key:
                    opt.load_value(element.attrib["value"])

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

        tree = ET.parse(self.pointer)
        root = tree.getroot()
        data = root.findall("property")
        if not isinstance(data, list):
            raise ValueError(f"config must be list not `{type(data)}`")

        for element in data:
            for opt in self.options:
                if element.attrib.get("name") == opt.key:
                    element.attrib["value"] = opt.serialise_value()

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
        self.cmd_start = cfg.cmd_start or ["bash", "startserver.sh", f"-configfile={file_settings.name}"]
        super().__init__(bot, am, cfg, SevenDays_Settings(file_settings, version_getter=lambda: cfg.version), Mod_7D2D)
        self.act_err_threshold = 100
        if cfg.steam_update is not None:
            self.updater = SteamCmd_Update_Manager(self)
        self.apply_version(
            detect_sevendays_version(directory=cfg.directory, server_log=cfg.server_log_file),
            persist=False,
        )

        self._relay = TelnetClient(self.check_running, 8081)
        self._tail: Tailer | None = None
        self._tail_matchers = set()
        self._server_ready = asyncio.Event()
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

        return tuple(sorted(roots_by_path.values(), key=self._save_root_sort_key))

    @property
    def supports_save_uploads(self) -> bool:
        return bool(self.save_file_roots)

    @property
    def supports_save_delete(self) -> bool:
        return bool(self.save_file_roots)

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        root = get_app_save_root(self.save_file_roots, root_id)
        if Path(upload_name).suffix.casefold() != ".zip":
            raise ValueError("7 Days to Die save uploads must be .zip archives.")
        destination = root.resolved_path
        temp_parent = destination.parent
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as temp_dir:
            extracted_path = Path(temp_dir) / destination.name
            replace_directory_from_zip(archive_path=source_path, destination=extracted_path)
            File_Utils.remove(destination, silent=True, resolve=False)
            File_Utils.move(extracted_path, destination, overwrite=False)
        return describe_app_save_path(root=root, path=destination, relative_path=destination.name)

    def delete_save_file(self, *, file_id: str) -> AppSaveEntry:
        if self.check_running():
            raise ValueError("Stop the server before deleting 7 Days to Die saves.")
        try:
            current_save = next(save for save in self.list_save_files() if save.id == file_id)
        except StopIteration as xcp:
            raise FileNotFoundError(f"Unknown save file: {file_id}") from xcp
        save_path = self.resolve_save_file(file_id)
        File_Utils.remove(save_path, silent=False, resolve=False)
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

    @staticmethod
    def _save_root_id(relative_path: PurePosixPath) -> str:
        digest = hashlib.sha1(relative_path.as_posix().encode(config.STR_ENCODE), usedforsecurity=False).hexdigest()
        return f"save-{digest[:12]}"

    @staticmethod
    def _save_root_sort_key(root: AppSaveRoot) -> tuple[str, str, str]:
        return (
            root.label.casefold(),
            root.path.name.casefold(),
            root.path.as_posix().casefold(),
        )

    def _userdata_root_path(self) -> Path | None:
        raw_value = _read_serverconfig_value(self.directory / "serverconfig.xml", "UserDataFolder")
        if raw_value is None:
            return None
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = (self.directory / candidate).resolve()
        return candidate

    def _serverconfig_setting_value(self, key: str) -> str | None:
        raw_value = _read_serverconfig_value(self.directory / "serverconfig.xml", key)
        if raw_value is None:
            return None
        return raw_value.strip() or None

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        self._server_ready.clear()
        previous_timestamped_logs: frozenset[Path] = frozenset(
            _timestamped_sevendays_output_logs(directory=self.directory)
        )
        await self._std_launch()

        while not self.check_running():
            log.debug(f"Waiting for {self.name}.check_running...")
            await asyncio.sleep(5)

        log.debug(f"{self.name}.running...")
        reader = await self._relay.setup()

        runtime_log = await _discover_sevendays_runtime_log(
            directory=self.directory,
            server_log=self.server_log,
            previous_timestamped_logs=previous_timestamped_logs,
            check_running=self.check_running,
        )
        if runtime_log is not None:
            File_Utils.link(runtime_log, self.file_stdout.with_name(runtime_log.name))
            self._tail = Tailer(self.check_running, runtime_log, self.file_stdout)
        else:
            self._tail = Tailer(lambda: self._relay.connected_event, reader, self.file_stdout)  # type: ignore[arg-type]
        await self._tail.start(self._tail_matchers)
        await self.wait_for_ready_event(
            self._server_ready,
            timeout_seconds=900.0,
            ready_label="server readiness",
        )
        await self._players.start()
        await self._activities.start()
        self._running = True
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._relay.send("saveworld")
        await asyncio.sleep(0.1)
        await self._relay.send("shutdown")
        await self._players.stop()
        await self._activities.stop()
        if self._tail:
            await self._tail.stop()
        if self._relay:
            await self._relay.teardown()
        await self._terminate()
        return True

    async def kill(self) -> bool:
        self._running = False
        await self._players.stop()
        await self._activities.stop()
        if self._tail:
            await self._tail.stop()
        if self._relay:
            await self._relay.teardown()
        await self._terminate()
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


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
        app._tail_matchers.add(self.match_version)
        app._tail_matchers.add(self.match_ready)
        app._tail_matchers.add(self.match_transiant)
        app._tail_matchers.add(self.match_chat)
        app._tail_matchers.add(self.match_death)

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
            notice: PlayerSessionNotice = PlayerSessionNotice(
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
