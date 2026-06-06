import ast
import asyncio
import logging
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

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
from apps._app import AM_Receiver, App
from apps._config import App_Config, AppVersion, Mod_Config, ModType
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult
from apps._mod import Mod
from apps._save_files import AppSaveEntry, AppSaveEntryKind, AppSaveRoot, AppSaveRootMode, replace_directory_from_zip
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Setting_Label,
    StringSettingSpec,
)
from apps._tailer import Tailer
from apps._telnet import TelnetClient
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
    r"Version:\s*V\s*(?P<version>\d+(?:\.\d+)*(?:\s*\([^)]+\))?)",
    re.IGNORECASE,
)
_SEVENDAYS_GAME_VERSION_RE = re.compile(
    r"GamePref\.GameVersion\s*=\s*V\s*(?P<version>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
_SEVENDAYS_READY_RE = re.compile(r"\bStartAsServer\b")
_SEVENDAYS_TRANSIENT_RE = re.compile(r"GMSG: Player '(.+?)' (joined|left) the game", re.IGNORECASE)
_SEVENDAYS_DEATH_RE = re.compile(r"GMSG: Player '(?P<player>.+?)' died\b", re.IGNORECASE)
_SEVENDAYS_CHAT_RE = re.compile(r"Chat.*?:\s*'(.*?)':\s*(.+)", re.IGNORECASE)


def _candidate_sevendays_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates = [
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


def detect_sevendays_version(*, directory: Path, server_log: Path | None) -> AppVersion | None:
    version: AppVersion | None = None
    for pointer in _candidate_sevendays_logs(directory=directory, server_log=server_log):
        try:
            for line in pointer.read_text(config.STR_ENCODE, errors="ignore").splitlines():
                if match := _SEVENDAYS_VERSION_RE.search(line):
                    raw_version = match.group("version").strip()
                    if build_match := re.fullmatch(r"(?P<base>\d+(?:\.\d+)*)\s*\((?P<extra>[^)]+)\)", raw_version):
                        return AppVersion(
                            main=f"{build_match.group('base')}{build_match.group('extra').strip()}",
                        )
                    return AppVersion(main=raw_version)
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
    def __init__(self, pointer: Path) -> None:
        options = [
            Setting[str](
                StringSettingSpec(),
                Setting_Label.serv_name,
                "ServerName",
                [],
                default="My Game Host",
            ),
            Setting[str](
                StringSettingSpec(),
                Setting_Label.serv_desc,
                "ServerDescription",
                [],
                default="A 7 Days to Die server",
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
            ),
            Setting[str](
                StringSettingSpec(
                    ChoiceSpec(
                        ChoiceOption("NorthAmericaEast", "N.America East"),
                        ChoiceOption("NorthAmericaWest", "N.America West"),
                        ChoiceOption("CentralAmerica", "C.America"),
                        ChoiceOption("SouthAmerica", "S.America"),
                        ChoiceOption("Europe", "Europe"),
                        ChoiceOption("Russia", "Russia"),
                        ChoiceOption("Asia", "Asia"),
                        ChoiceOption("MiddleEast", "Middle East"),
                        ChoiceOption("Africa", "Africa"),
                        ChoiceOption("Oceania", "Oceania"),
                    )
                ),
                "Server Region",
                "Region",
                [],
                default="NorthAmericaEast",
            ),
            Setting[int](
                IntSettingSpec(
                    ChoiceSpec(
                        ChoiceOption("2", "Public"),
                        ChoiceOption("1", "Friends"),
                        ChoiceOption("0", "Private"),
                    )
                ),
                Setting_Label.visibility,
                "ServerVisibility",
                [],
                default=2,
            ),
            Setting[int](
                IntSettingSpec(max_value=5120),
                "World Transfer Speed (KiB/s)",
                "ServerMaxWorldTransferSpeedKiBs",
                [],
                default=512,
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(max_value=8),
                Setting_Label.max_player,
                "ServerMaxPlayerCount",
                [],
                default=8,
            ),
            Setting[int](
                IntSettingSpec(max_value=4),
                "Reserved Slots",
                "ServerReservedSlots",
                [],
                default=0,
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                "Admin Slots",
                "ServerAdminSlots",
                [],
                default=0,
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                "Game World",
                "GameWorld",
                [],
                default="Navezgane",
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                "World Gen Seed",
                "WorldGenSeed",
                [],
                default="MyGame",
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                "World Gen Size",
                "WorldGenSize",
                [],
                default=6144,
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                "Game Name",
                "GameName",
                [],
                default="MyGame",
            ),
            Setting[int](
                IntSettingSpec(_GAME_DIFFICULTY_CHOICES),
                Setting_Label.difficulty,
                "GameDifficulty",
                [],
                default=1,
            ),
            Setting[int](
                IntSettingSpec(),
                "Block Damage Player",
                "BlockDamagePlayer",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(),
                "Block Damage Ai",
                "BlockDamageAI",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(),
                "Block Damage Ai Blood Moon",
                "BlockDamageAIBM",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(),
                "Xp Multiplier",
                "XPMultiplier",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(),
                "Day Night Length",
                "DayNightLength",
                [],
                default=60,
            ),
            Setting[int](
                IntSettingSpec(),
                "Day Light Length",
                "DayLightLength",
                [],
                default=18,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Biome Progression",
                "BiomeProgression",
                [],
                default=True,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Webdashboard",
                "WebDashboardEnabled",
                [],
                default=False,
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                "Storm Frequency",
                "StormFreq",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(_DEATH_PENALTY_CHOICES),
                "Death Penalty",
                "DeathPenalty",
                [],
                default=1,
            ),
            Setting[int](
                IntSettingSpec(_DROP_ON_DEATH_CHOICES),
                "Drop On Death",
                "DropOnDeath",
                [],
                default=1,
            ),
            Setting[int](
                IntSettingSpec(_DROP_ON_QUIT_CHOICES),
                "Drop On Quit",
                "DropOnQuit",
                [],
                default=0,
            ),
            Setting[int](
                IntSettingSpec(_CAMERA_RESTRICTION_CHOICES),
                "Camera Restriction",
                "CameraRestrictionMode",
                [],
                default=0,
            ),
            Setting[int](
                IntSettingSpec(),
                "Jar Refund",
                "JarRefund",
                [],
                default=60,
            ),
            Setting[int](
                IntSettingSpec(_ENEMY_DIFFICULTY_CHOICES),
                "Enemy Difficulty",
                "EnemyDifficulty",
                [],
                default=0,
            ),
            Setting[int](
                IntSettingSpec(_ZOMBIE_FERAL_SENSE_CHOICES),
                "Zombie Feral Sense",
                "ZombieFeralSense",
                [],
                default=0,
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Move",
                "ZombieMove",
                [],
                default=0,
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Move Night",
                "ZombieMoveNight",
                [],
                default=3,
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Feral Move",
                "ZombieFeralMove",
                [],
                default=3,
            ),
            Setting[int](
                IntSettingSpec(),
                "Zombie Blood Moon Move",
                "ZombieBMMove",
                [],
                default=3,
            ),
            Setting[int](
                IntSettingSpec(_AI_SMELL_MODE_CHOICES),
                "Ai Smell Mode",
                "AISmellMode",
                [],
                default=3,
            ),
            Setting[int](
                IntSettingSpec(),
                "Blood Moon Frequency",
                "BloodMoonFrequency",
                [],
                default=7,
            ),
            Setting[int](
                IntSettingSpec(),
                "Blood Moon Range",
                "BloodMoonRange",
                [],
                default=0,
            ),
            Setting[int](
                IntSettingSpec(allow_negative=True),
                "Blood Moon Warning",
                "BloodMoonWarning",
                [],
                default=8,
            ),
            Setting[int](
                IntSettingSpec(),
                "Loot Abundance",
                "LootAbundance",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(allow_negative=True),
                "Loot Respawn Days",
                "LootRespawnDays",
                [],
                default=7,
            ),
            Setting[int](
                IntSettingSpec(),
                "Air Drop Frequency",
                "AirDropFrequency",
                [],
                default=72,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Air Drop Marker",
                "AirDropMarker",
                [],
                default=True,
            ),
            Setting[int](
                IntSettingSpec(),
                "Party Shared Kill Range",
                "PartySharedKillRange",
                [],
                default=100,
            ),
            Setting[int](
                IntSettingSpec(_PLAYER_KILLING_MODE_CHOICES),
                "Player Killing Mode",
                "PlayerKillingMode",
                [],
                default=3,
            ),
            Setting[int](
                IntSettingSpec(),
                "Quest Progression Daily Limit",
                "QuestProgressionDailyLimit",
                [],
                default=4,
            ),
        ]
        super().__init__(pointer, options)

    def load(self):
        data = ET.parse(self.pointer).getroot().findall("property")
        if not isinstance(data, list):
            raise ValueError(f"config must be list not `{type(data)}`")

        for element in data:
            for opt in self.options:
                if element.attrib.get("name") == opt.key:
                    opt.update(element.attrib["value"])

    def save(self):
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
        return data


class SevenDays(App[App_Config]):
    chat_relay_outbound = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0xB91C1C
        self.proc_name = "7DaysToDie"
        self.proc_cmd = ["7DaysToDieServer", "-nographics"]

        self.process = None
        file_settings = cfg.directory.absolute() / "serverconfig.xml"
        self.cmd_start = cfg.cmd_start or ["bash", "startserver.sh", f"-configfile={file_settings.name}"]
        super().__init__(bot, am, cfg, SevenDays_Settings(file_settings), Mod_7D2D)
        self.act_err_threshold = 100
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

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return _SEVENDAYS_CONSOLE_ACTIONS

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
        )

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        save_path = self._save_directory_path()
        if save_path is None:
            return ()
        return (
            AppSaveRoot(
                id="world",
                label="Current Save",
                path=save_path,
                mode=AppSaveRootMode.SELF,
                include_files=False,
                include_directories=True,
            ),
        )

    @property
    def supports_save_uploads(self) -> bool:
        return self._save_directory_path() is not None

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        if root_id != "world":
            raise ValueError(f"Unknown save root: {root_id}")
        if Path(upload_name).suffix.casefold() != ".zip":
            raise ValueError("7 Days to Die save uploads must be .zip archives.")
        destination = self._require_save_directory_path()
        temp_parent = destination.parent
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as temp_dir:
            extracted_path = Path(temp_dir) / destination.name
            replace_directory_from_zip(archive_path=source_path, destination=extracted_path)
            File_Utils.remove(destination, silent=True, resolve=False)
            File_Utils.move(extracted_path, destination, overwrite=False)
        stat = destination.stat()
        return AppSaveEntry(
            id=f"world/{destination.name}",
            label=destination.name,
            relative_path=destination.name,
            root_id="world",
            root_label="Current Save",
            kind=AppSaveEntryKind.DIRECTORY,
            size_bytes=0,
            modified_at=datetime.fromtimestamp(stat.st_mtime),
        )

    def _require_save_directory_path(self) -> Path:
        save_path = self._save_directory_path()
        if save_path is None:
            raise ValueError("7 Days to Die save support requires the UserDataFolder server setting to be configured.")
        return save_path

    def _save_directory_path(self) -> Path | None:
        userdata_root = self._userdata_root_path()
        if userdata_root is None:
            return None
        game_world = self._serverconfig_setting_value("GameWorld")
        game_name = self._serverconfig_setting_value("GameName")
        if game_world is None or game_name is None:
            return None
        return userdata_root / "Saves" / game_world / game_name

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
        await self._std_launch()

        if self.server_log and self.server_log.exists():
            File_Utils.link(self.server_log, self.file_stdout.with_name(self.server_log.name))

        while not self.check_running():
            log.debug(f"Waiting for {self.name}.check_running...")
            await asyncio.sleep(5)

        log.debug(f"{self.name}.running...")
        reader = await self._relay.setup()

        count = 0
        while count < 25 and (not self.process or (self.process and not self.process.stdout)):
            log.debug(f"Waiting for {self.name}.process... proc_stdout={self.process.stdout if self.process else None}")
            await asyncio.sleep(1)
            count += 1

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
    def __init__(self, app: SevenDays):
        self.app = app
        self._last_telnet = datetime.now()
        app._tail_matchers.add(self.match_version)
        app._tail_matchers.add(self.match_ready)
        app._tail_matchers.add(self.match_transiant)
        app._tail_matchers.add(self.match_chat)
        app._tail_matchers.add(self.match_death)

    async def match_version(self, line: str) -> None:
        if match := _SEVENDAYS_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)
            return
        if match := _SEVENDAYS_GAME_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)

    async def match_ready(self, line: str) -> None:
        if _SEVENDAYS_READY_RE.search(line):
            if not self.app._server_ready.is_set():
                log.info("%s matched 7D2D ready line: %s", self.app.name, line)
                self.app._server_ready.set()

    async def match_transiant(self, line: str):
        if match := _SEVENDAYS_TRANSIENT_RE.search(line):
            player = match.group(1)
            action = str(match.group(2)).lower()
            notice = PlayerSessionNotice(
                action=PlayerSessionAction.JOINED if "join" in action else PlayerSessionAction.LEFT,
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

    async def match_chat(self, line: str):
        player = None
        if match := _SEVENDAYS_CHAT_RE.search(line):
            player = str(match.group(1)).strip("\r\n ")
            msg = str(match.group(2)).strip("\r\n ")
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player or hikari.UNDEFINED))

    async def match_death(self, line: str) -> None:
        if match := _SEVENDAYS_DEATH_RE.search(line):
            player = match.group("player")
            notice = GameDeathNotice(
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
        self.providers = [Provider_Time(app)]
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self):
        if self._time_task and not self._time_task.done():
            return
        self._running = True
        for prov in self.providers:
            self.app.activity_manager.register(prov)
            self.tasks.union([asyncio.create_task(func()) for func in prov.task_funcs])

    async def stop(self):
        self._running = False
        for prov in self.providers:
            self.app.activity_manager.deregister(prov)
        for task in self.tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class Provider_Time(config.Activity_Provider):
    def __init__(self, app: SevenDays):
        self.app = app
        self._time = None
        self._count = 0
        self.stats: dict[str, GameStatValue] = {}
        app._tail_matchers.add(self.match_time)
        app._tail_matchers.add(self.match_stats)
        self.task_funcs = [self._get_time, self._getgamestats]
        super().__init__()

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
