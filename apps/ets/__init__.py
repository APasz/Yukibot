import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from re import Match
from typing import Final, cast

import hikari

import config
from _discord import DC_Bound, DC_Relay
from _security import Power_Level
from apps._app import App
from apps._config import App_Config, AppVersion, Mod_Config, SteamUpdatePreset
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._mod import Mod
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
    IntSettingSpec,
    Setting,
    Setting_Label,
    StringSettingSpec,
)
from apps._steam import SteamGameServerLoginTokenStatus
from apps._tailer import Tailer
from apps._updater import SteamCmd_Update_Manager
from config import Activity_Manager
from relay_notices import (
    PlayerSessionAction,
    RelayNoticeSource,
    render_notice_text,
)

log = logging.getLogger(__name__)

_ETS_DATA_HOME_DIRECTORY_NAME: Final[str] = "home_data"
_ETS_SERVER_HOME_DIRECTORY_NAME: Final[str] = "Euro Truck Simulator 2"
_ETS_SERVER_CONFIG_FILENAME: Final[str] = "server_config.sii"
_ETS_SERVER_LOG_FILENAME: Final[str] = "server.log.txt"
_ETS_SERVER_PACKAGE_CONFIG_FILENAME: Final[str] = "server_packages.sii"
_ETS_SERVER_PACKAGE_DATA_FILENAME: Final[str] = "server_packages.dat"
_ETS_SERVER_PACKAGE_CONFIG_ROOT_ID: Final[str] = "server-packages-config"
_ETS_SERVER_PACKAGE_DATA_ROOT_ID: Final[str] = "server-packages-data"
_ETS_SERVER_PACKAGE_CONFIG_ROOT_LABEL: Final[str] = "Server packages config (.sii)"
_ETS_SERVER_PACKAGE_DATA_ROOT_LABEL: Final[str] = "Server packages data (.dat)"
_ETS_SERVER_LOGON_TOKEN_FIELD: Final[str] = "server_logon_token"
_ETS_INITIAL_CONFIG_WAIT_SECONDS: Final[float] = 20.0
_ETS_INITIAL_CONFIG_POLL_SECONDS: Final[float] = 0.1
_ETS_INITIAL_PROCESS_STOP_WAIT_SECONDS: Final[float] = 5.0
_ETS_SERVER_PORT_FIELDS: Final[tuple[str, str]] = (
    "connection_dedicated_port",
    "query_dedicated_port",
)
_ETS_SHORT_TEXT_MAX_LENGTH: Final[int] = 63
_ETS_WELCOME_MESSAGE_MAX_LENGTH: Final[int] = 127
_ETS_SERVER_PORT_FIELD_PATTERN: Final[str] = "|".join(
    re.escape(field) for field in _ETS_SERVER_PORT_FIELDS
)
_ETS_SERVER_CONFIG_PORT_RE = re.compile(
    rf"^(?P<prefix>\s*(?P<field>{_ETS_SERVER_PORT_FIELD_PATTERN})\s*:\s*)"
    r"(?P<port>\d+)\s*$"
)
_ETS_SERVER_CONFIG_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*)(?P<value>.*)$"
)
_ETS_SERVER_CONFIG_BARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ETS_GAME_VERSION_RE = re.compile(r"\[MP\]\s+Game version:\s*(?P<version>\S+)", re.IGNORECASE)
_ETS_PACKSET_VERSION_RE = re.compile(r"Loaded pack set version\s+(?P<version>\S+)", re.IGNORECASE)

ETS_DEFAULT_CONNECTION_PORT: Final[int] = 27015
# Steam requires GSLTs to be created for the game rather than the dedicated-server app.
STEAM_GAME_APP_ID: Final[int] = 227300
STEAM_APP_ID: Final[int] = 1948160
STEAM_UPDATE_PRESET: Final[SteamUpdatePreset] = SteamUpdatePreset(app_id=STEAM_APP_ID)


@dataclass(frozen=True, slots=True)
class _EtsServerConfigAssignment:
    key: str
    prefix: str
    value_text: str
    suffix: str
    line_ending: str


@dataclass(frozen=True, slots=True)
class _EtsServerConfigAssignmentLocation:
    index: int
    assignment: _EtsServerConfigAssignment


@dataclass(frozen=True, slots=True)
class _EtsServerConfigPortLine:
    index: int
    prefix_end: int
    port_end: int
    line_ending: str


def ets_data_home(directory: Path) -> Path:
    """Return the ETS2 XDG data home isolated inside an app installation."""

    return directory.absolute() / _ETS_DATA_HOME_DIRECTORY_NAME


def ets_server_home(directory: Path) -> Path:
    return ets_data_home(directory) / _ETS_SERVER_HOME_DIRECTORY_NAME


def ets_server_config_path(directory: Path) -> Path:
    return ets_server_home(directory) / _ETS_SERVER_CONFIG_FILENAME


def ets_server_log_path(directory: Path) -> Path:
    return ets_server_home(directory) / _ETS_SERVER_LOG_FILENAME


def ets_server_package_paths(directory: Path) -> tuple[Path, Path]:
    server_home = ets_server_home(directory)
    return (
        server_home / _ETS_SERVER_PACKAGE_CONFIG_FILENAME,
        server_home / _ETS_SERVER_PACKAGE_DATA_FILENAME,
    )


def ets_launch_environment(directory: Path) -> dict[str, str]:
    return {"XDG_DATA_HOME": str(ets_data_home(directory))}


def resolve_ets_connection_port(port: int | None) -> int:
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        raise TypeError("ETS2 connection port must be an integer.")
    resolved_port = ETS_DEFAULT_CONNECTION_PORT if port is None else port
    if not 1 <= resolved_port < 65535:
        raise ValueError("ETS2 connection port must be between 1 and 65534 because its query port is one higher.")
    return resolved_port


def missing_ets_server_package_names(directory: Path) -> tuple[str, ...]:
    return tuple(path.name for path in ets_server_package_paths(directory) if not _is_nonempty_regular_file(path))


def configure_ets_server_ports(*, config_path: Path, connection_port: int | None) -> None:
    """Set the dedicated connection/query port pair in a generated ETS2 config."""

    resolved_connection_port = resolve_ets_connection_port(connection_port)
    port_by_field = {
        "connection_dedicated_port": resolved_connection_port,
        "query_dedicated_port": resolved_connection_port + 1,
    }
    data = config_path.read_text(config.STR_ENCODE)
    lines = data.splitlines(keepends=True)
    matched_lines: dict[str, _EtsServerConfigPortLine] = {}
    in_block_comment = False
    for index, line in enumerate(lines):
        content, line_ending = _split_line_ending(line)
        code_content, in_block_comment = _mask_ets_server_config_comments(
            content,
            in_block_comment=in_block_comment,
        )
        match = _ETS_SERVER_CONFIG_PORT_RE.fullmatch(code_content)
        if match is None:
            continue
        field = match.group("field")
        if field in matched_lines:
            raise ValueError(f"ETS2 server config contains multiple {field} entries: {config_path}")
        matched_lines[field] = _EtsServerConfigPortLine(
            index=index,
            prefix_end=match.end("prefix"),
            port_end=match.end("port"),
            line_ending=line_ending,
        )

    _ensure_ets_server_config_block_comments_closed(
        in_block_comment=in_block_comment,
        config_path=config_path,
    )
    missing_fields = tuple(field for field in _ETS_SERVER_PORT_FIELDS if field not in matched_lines)
    if missing_fields:
        raise ValueError(
            "ETS2 server config is missing dedicated port fields: "
            f"{', '.join(missing_fields)} ({config_path})"
    )

    for field in _ETS_SERVER_PORT_FIELDS:
        matched_line = matched_lines[field]
        content, _ = _split_line_ending(lines[matched_line.index])
        lines[matched_line.index] = (
            f"{content[:matched_line.prefix_end]}{port_by_field[field]}"
            f"{content[matched_line.port_end:]}{matched_line.line_ending}"
        )
    config_path.write_text("".join(lines), config.STR_ENCODE)


async def prepare_ets_server_installation(*, directory: Path, connection_port: int | None) -> None:
    """Initialise a fresh ETS2 dedicated server home and its selected port pair."""

    config_path = ets_server_config_path(directory)
    if config_path.exists() and not config_path.is_file():
        raise ValueError(f"ETS2 server config path is not a file: {config_path}")
    if not config_path.is_file():
        await _create_ets_server_config(directory=directory, config_path=config_path)
    configure_ets_server_ports(config_path=config_path, connection_port=connection_port)


async def _create_ets_server_config(*, directory: Path, config_path: Path) -> None:
    launch_directory = directory / "bin" / "linux_x64"
    launch_script = launch_directory / "server_launch.sh"
    if not launch_script.is_file():
        raise FileNotFoundError(f"ETS2 server launcher is missing after SteamCMD install: {launch_script}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(ets_launch_environment(directory))
    log.info("Creating ETS2 server config: directory=%s data_home=%s", directory, config_path.parent)
    process = await asyncio.create_subprocess_exec(
        "bash",
        launch_script.name,
        cwd=str(launch_directory),
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = asyncio.get_running_loop().time() + _ETS_INITIAL_CONFIG_WAIT_SECONDS
        while not _ets_server_config_has_dedicated_port_fields(config_path):
            if process.returncode is not None:
                await process.wait()
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"ETS2 did not create {config_path.name} within {_ETS_INITIAL_CONFIG_WAIT_SECONDS:.0f}s."
                )
            await asyncio.sleep(_ETS_INITIAL_CONFIG_POLL_SECONDS)
        if not _ets_server_config_has_dedicated_port_fields(config_path):
            raise RuntimeError(
                "ETS2 did not create a usable server configuration during initialisation. "
                "Check the SteamCMD installation and server log."
            )
    finally:
        await _stop_ets_initialisation_process(process)


async def _stop_ets_initialisation_process(process: asyncio.subprocess.Process) -> None:
    _signal_ets_initialisation_process_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=_ETS_INITIAL_PROCESS_STOP_WAIT_SECONDS)
    except TimeoutError:
        _signal_ets_initialisation_process_group(process, signal.SIGKILL)
        await process.wait()


def _signal_ets_initialisation_process_group(
    process: asyncio.subprocess.Process,
    signal_number: signal.Signals,
) -> None:
    # The launcher can exit after spawning the server. Its session can still
    # contain that child, so signal the process group even after the launcher
    # itself has returned.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal_number)


def _split_line_ending(line: str) -> tuple[str, str]:
    content = line.removesuffix("\n").removesuffix("\r")
    return (content, line[len(content) :])


def _parse_ets_server_config_assignment(
    line: str,
    *,
    in_block_comment: bool,
) -> tuple[_EtsServerConfigAssignment | None, bool]:
    content, line_ending = _split_line_ending(line)
    code_content, next_block_comment = _mask_ets_server_config_comments(
        content,
        in_block_comment=in_block_comment,
    )
    match = _ETS_SERVER_CONFIG_ASSIGNMENT_RE.fullmatch(code_content)
    if match is None:
        return None, next_block_comment
    value_text = match.group("value").rstrip()
    value_end = match.start("value") + len(value_text)
    return _EtsServerConfigAssignment(
        key=match.group("key"),
        prefix=content[: match.end("prefix")],
        value_text=value_text,
        suffix=content[value_end:],
        line_ending=line_ending,
    ), next_block_comment


def _mask_ets_server_config_comments(
    content: str,
    *,
    in_block_comment: bool,
) -> tuple[str, bool]:
    """Mask SII comments while preserving character offsets for rewrites."""

    characters = list(content)
    in_string = False
    escaped = False
    index = 0
    while index < len(content):
        character = content[index]
        next_character = content[index + 1] if index + 1 < len(content) else ""
        if in_block_comment:
            characters[index] = " "
            if character == "*" and next_character == "/":
                characters[index + 1] = " "
                index += 2
                in_block_comment = False
                continue
            index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        if character == "#" or (character == "/" and next_character == "/"):
            characters[index:] = " " * (len(content) - index)
            break
        if character == "/" and next_character == "*":
            characters[index] = " "
            characters[index + 1] = " "
            index += 2
            in_block_comment = True
            continue
        index += 1
    return "".join(characters), in_block_comment


def _ensure_ets_server_config_block_comments_closed(
    *,
    in_block_comment: bool,
    config_path: Path,
) -> None:
    if in_block_comment:
        raise ValueError(f"ETS2 server config has an unterminated block comment: {config_path}")


def _parse_ets_server_config_string(raw_value: str) -> str:
    if not raw_value:
        return ""
    if not raw_value.startswith('"'):
        if '"' in raw_value:
            raise ValueError("ETS2 server config text value has an invalid quote.")
        return raw_value
    try:
        parsed_value: object = cast(object, json.loads(raw_value))
    except json.JSONDecodeError as xcp:
        raise ValueError("ETS2 server config quoted text value is invalid.") from xcp
    if not isinstance(parsed_value, str):
        raise ValueError("ETS2 server config text values must be strings.")
    return parsed_value


def _serialise_ets_server_config_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _find_ets_server_config_assignment(
    *,
    lines: list[str],
    key: str,
    config_path: Path,
) -> _EtsServerConfigAssignmentLocation | None:
    matching_assignment: _EtsServerConfigAssignmentLocation | None = None
    key_casefold = key.casefold()
    in_block_comment = False
    for index, line in enumerate(lines):
        assignment, in_block_comment = _parse_ets_server_config_assignment(
            line,
            in_block_comment=in_block_comment,
        )
        if assignment is None or assignment.key.casefold() != key_casefold:
            continue
        if matching_assignment is not None:
            raise ValueError(f"ETS2 server config contains multiple {key} entries: {config_path}")
        matching_assignment = _EtsServerConfigAssignmentLocation(index=index, assignment=assignment)
    _ensure_ets_server_config_block_comments_closed(
        in_block_comment=in_block_comment,
        config_path=config_path,
    )
    return matching_assignment


def _read_ets_server_config_field(*, config_path: Path, key: str) -> str | None:
    lines = config_path.read_text(config.STR_ENCODE).splitlines(keepends=True)
    matching_assignment = _find_ets_server_config_assignment(
        lines=lines,
        key=key,
        config_path=config_path,
    )
    if matching_assignment is None:
        return None
    return matching_assignment.assignment.value_text


def _write_ets_server_config_field(*, config_path: Path, key: str, value_text: str) -> None:
    lines = config_path.read_text(config.STR_ENCODE).splitlines(keepends=True)
    matching_assignment = _find_ets_server_config_assignment(
        lines=lines,
        key=key,
        config_path=config_path,
    )
    if matching_assignment is None:
        raise ValueError(f"ETS2 server config is missing {key}: {config_path}")
    assignment = matching_assignment.assignment
    lines[matching_assignment.index] = (
        f"{assignment.prefix}{value_text}{assignment.suffix}{assignment.line_ending}"
    )
    config_path.write_text("".join(lines), config.STR_ENCODE)


def _ets_server_logon_token_is_configured(raw_value: str | None) -> bool:
    if raw_value is None:
        return False
    value = raw_value.strip()
    if not value:
        return False
    if not value.startswith('"'):
        return True
    try:
        return bool(_parse_ets_server_config_string(value).strip())
    except ValueError:
        # A malformed stored token can still be replaced or cleared through Properties.
        return True


def _serialise_ets_server_logon_token(token: str | None) -> str:
    if token is None:
        return _serialise_ets_server_config_string("")
    if _ETS_SERVER_CONFIG_BARE_TOKEN_RE.fullmatch(token):
        return token
    return _serialise_ets_server_config_string(token)


def _is_nonempty_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _validate_ets_server_package_source(source_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"ETS2 server package upload does not exist: {source_path}")
    if source_path.stat().st_size <= 0:
        raise ValueError("ETS2 server package uploads must not be empty.")


def _ets_server_config_has_dedicated_port_fields(config_path: Path) -> bool:
    try:
        data = config_path.read_text(config.STR_ENCODE)
    except OSError:
        return False
    fields: set[str] = set()
    in_block_comment = False
    for line in data.splitlines():
        code_line, in_block_comment = _mask_ets_server_config_comments(
            line,
            in_block_comment=in_block_comment,
        )
        match = _ETS_SERVER_CONFIG_PORT_RE.fullmatch(code_line)
        if match is not None:
            fields.add(match.group("field"))
    return all(field in fields for field in _ETS_SERVER_PORT_FIELDS)


def _replace_ets_server_package(*, source_path: Path, target: Path) -> None:
    _validate_ets_server_package_source(source_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".upload",
            dir=target.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with source_path.open("rb") as source_file:
                shutil.copyfileobj(source_file, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _candidate_ets_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates = [
        server_log,
        ets_server_log_path(directory),
    ]
    existing: list[Path] = []
    seen: set[Path] = set()
    for pointer in candidates:
        if pointer is None or pointer in seen or not pointer.exists():
            continue
        seen.add(pointer)
        existing.append(pointer)
    return tuple(existing)


def detect_ets_version(*, directory: Path, server_log: Path | None) -> AppVersion | None:
    version: AppVersion | None = None
    for pointer in _candidate_ets_logs(directory=directory, server_log=server_log):
        try:
            for line in pointer.read_text(config.STR_ENCODE, errors="ignore").splitlines():
                if match := _ETS_GAME_VERSION_RE.search(line):
                    return AppVersion(main=match.group("version").strip())
                if version is None and (match := _ETS_PACKSET_VERSION_RE.search(line)):
                    version = AppVersion(main=match.group("version").strip())
        except OSError as xcp:
            log.warning("Failed to inspect ETS log %s: %s", pointer, xcp)
    return version


class Mod_ETS(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class _ETSTextSettingSpec(StringSettingSpec):
    def __init__(
        self,
        *,
        max_length: int,
        allow_blank: bool = False,
        is_sensitive: bool = False,
        do_hide: Power_Level | None = None,
    ) -> None:
        if max_length <= 0:
            raise ValueError("ETS2 text setting maximum length must be positive.")
        super().__init__(allow_blank=allow_blank, is_sensitive=is_sensitive, do_hide=do_hide)
        self._max_length = max_length

    def validate_value(self, value: str) -> None:
        if len(value) > self._max_length:
            raise ValueError(f"must be at most {self._max_length} characters")


class ETS_Settings(App_Settings):
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
        short_text = _ETSTextSettingSpec(max_length=_ETS_SHORT_TEXT_MAX_LENGTH, allow_blank=True)
        welcome_message = _ETSTextSettingSpec(
            max_length=_ETS_WELCOME_MESSAGE_MAX_LENGTH,
            allow_blank=True,
        )
        password = _ETSTextSettingSpec(
            max_length=_ETS_SHORT_TEXT_MAX_LENGTH,
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.user,
        )
        non_negative_integer = IntSettingSpec()
        boolean = BoolSettingSpec()
        options = [
            Setting[str](
                short_text,
                Setting_Label.serv_name,
                "lobby_name",
                [],
                default="",
                desc="Session name, limited to 63 characters.",
            ),
            Setting[str](
                short_text,
                Setting_Label.serv_desc,
                "description",
                [],
                default="",
                paragraph=True,
                desc="Session description, limited to 63 characters.",
            ),
            Setting[str](
                welcome_message,
                Setting_Label.motd,
                "welcome_message",
                [],
                default="",
                paragraph=True,
                desc="Message shown when players join, limited to 127 characters.",
            ),
            Setting[str](
                password,
                Setting_Label.password,
                "password",
                [],
                default="",
                power_level=Power_Level.sudo,
                desc="Optional session password, limited to 63 characters.",
            ),
            Setting[int](
                IntSettingSpec(min_value=1, max_value=8),
                Setting_Label.max_player,
                "max_players",
                [],
                default=8,
                power_level=Power_Level.sudo,
                desc="Maximum players in the session. ETS2 supports up to 8.",
            ),
            Setting[int](
                non_negative_integer,
                "Max Total Vehicles",
                "max_vehicles_total",
                [],
                default=100,
                desc="Maximum total vehicles the session may maintain.",
            ),
            Setting[int](
                non_negative_integer,
                "Max AI Vehicles per Player",
                "max_ai_vehicles_player",
                [],
                default=50,
                desc="Maximum AI vehicles allocated per player.",
            ),
            Setting[int](
                non_negative_integer,
                "Max AI Vehicle Spawns per Player",
                "max_ai_vehicles_player_spawn",
                [],
                default=50,
                desc="Maximum AI vehicles spawned per player.",
            ),
            Setting[bool](
                boolean,
                "Player Damage",
                "player_damage",
                [],
                default=True,
                desc="Allow players to receive damage from other players.",
            ),
            Setting[bool](
                boolean,
                "Traffic",
                "traffic",
                [],
                default=True,
                desc="Enable AI traffic.",
            ),
            Setting[bool](
                boolean,
                "Hide Players in Company Areas",
                "hide_in_company",
                [],
                default=False,
                desc="Hide remote players in company areas.",
            ),
            Setting[bool](
                boolean,
                "Hide Colliding Vehicles",
                "hide_colliding",
                [],
                default=True,
                desc="Hide colliding vehicles after teleporting.",
            ),
            Setting[bool](
                boolean,
                "Force Speed Limiter",
                "force_speed_limiter",
                [],
                default=False,
                desc="Force the speed limiter for all players.",
            ),
            Setting[bool](
                boolean,
                "Optional Mods",
                "mods_optioning",
                [],
                default=False,
                desc="Allow mods marked optional to remain optional.",
            ),
            Setting[bool](
                boolean,
                "No Collision in Service Areas",
                "service_no_collision",
                [],
                default=False,
                desc="Disable collisions in service areas.",
            ),
            Setting[bool](
                boolean,
                "Menu Ghosting",
                "in_menu_ghosting",
                [],
                default=False,
                desc="Disable collisions while a player is paused in a menu.",
            ),
            Setting[bool](
                boolean,
                "Name Tags",
                "name_tags",
                [],
                default=True,
                desc="Show player name tags above vehicles.",
            ),
        ]
        super().__init__(pointer, options, version_getter=version_getter)

    def load(self) -> None:
        data = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        for setting in self.options:
            setting.value = setting.default
        seen_keys: set[str] = set()
        in_block_comment = False
        lines = data.splitlines(keepends=True)
        for line in lines:
            assignment, in_block_comment = _parse_ets_server_config_assignment(
                line,
                in_block_comment=in_block_comment,
            )
            if assignment is None:
                continue
            setting = self._setting_for_exact_key(assignment.key)
            if setting is None:
                continue
            setting_key = setting.key.casefold()
            if setting_key in seen_keys:
                raise ValueError(f"ETS2 server config contains multiple {setting.key} entries: {self.pointer}")
            seen_keys.add(setting_key)
            try:
                value_text = assignment.value_text
                if setting.value_type is str:
                    value_text = _parse_ets_server_config_string(value_text)
                setting.load_value(value_text)
            except (IndexError, ValueError) as xcp:
                if setting.is_sensitive:
                    raise ValueError(f"Invalid stored value for {setting.label}.") from xcp
                raise ValueError(f"Invalid ETS2 server config value for {setting.key}: {xcp}") from xcp
        _ensure_ets_server_config_block_comments_closed(
            in_block_comment=in_block_comment,
            config_path=self.pointer,
        )

    def save(self) -> str:
        data = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        seen_keys: set[str] = set()
        in_block_comment = False
        lines = data.splitlines(keepends=True)
        for index, line in enumerate(lines):
            assignment, in_block_comment = _parse_ets_server_config_assignment(
                line,
                in_block_comment=in_block_comment,
            )
            if assignment is None:
                continue
            setting = self._setting_for_exact_key(assignment.key)
            if setting is None:
                continue
            setting_key = setting.key.casefold()
            if setting_key in seen_keys:
                raise ValueError(f"ETS2 server config contains multiple {setting.key} entries: {self.pointer}")
            seen_keys.add(setting_key)
            value_text = setting.serialise_value()
            if setting.value_type is str:
                value_text = _serialise_ets_server_config_string(value_text)
            lines[index] = f"{assignment.prefix}{value_text}{assignment.suffix}{assignment.line_ending}"
        _ensure_ets_server_config_block_comments_closed(
            in_block_comment=in_block_comment,
            config_path=self.pointer,
        )
        self.pointer.write_text("".join(lines), config.STR_ENCODE)
        return data


class ETS(App[App_Config]):
    _instance: None = None
    chat_relay_outbound = True
    relay_notice_player_session_supported = True
    # server_launch.sh starts the server as a child process.
    uses_process_group_termination = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0x2563EB
        self.proc_name = "eurotrucks2_server"
        self.proc_cmd = [self.proc_name]
        file_settings = ets_server_config_path(cfg.directory)
        self.cmd_start = ["./server_launch.sh"]
        self.cmd_cwd = cfg.directory.absolute() / "bin" / "linux_x64"

        self.process = None
        super().__init__(bot, am, cfg, ETS_Settings(file_settings, version_getter=lambda: cfg.version))
        self.act_err_threshold = 100
        if cfg.steam_update is not None:
            self.updater = SteamCmd_Update_Manager(self)
        self.apply_version(
            detect_ets_version(directory=cfg.directory, server_log=cfg.server_log_file),
            persist=False,
        )

        self._tail: Tailer | None = None
        self._tail_machers: set[Callable[[str], Awaitable[None]]] = set()
        self._matchers = Matchers(self)

    def detect_installed_version(self) -> AppVersion | None:
        return detect_ets_version(directory=self.cfg.directory, server_log=self.cfg.server_log_file)

    def launch_environment(self) -> Mapping[str, str]:
        return ets_launch_environment(self.directory)

    @property
    def steam_game_server_login_token_status(self) -> SteamGameServerLoginTokenStatus:
        token = _read_ets_server_config_field(
            config_path=ets_server_config_path(self.directory),
            key=_ETS_SERVER_LOGON_TOKEN_FIELD,
        )
        return SteamGameServerLoginTokenStatus(
            game_app_id=STEAM_GAME_APP_ID,
            configured=_ets_server_logon_token_is_configured(token),
        )

    def _set_steam_game_server_login_token(self, token: str | None) -> None:
        _write_ets_server_config_field(
            config_path=ets_server_config_path(self.directory),
            key=_ETS_SERVER_LOGON_TOKEN_FIELD,
            value_text=_serialise_ets_server_logon_token(token),
        )

    def _matches_leftover_process(
        self,
        *,
        command_line: tuple[str, ...],
        expected_command_parts: tuple[str, ...],
        process_cwd: str | None,
    ) -> bool:
        if not super()._matches_leftover_process(
            command_line=command_line,
            expected_command_parts=expected_command_parts,
            process_cwd=process_cwd,
        ):
            return False
        if process_cwd is None:
            return False
        try:
            return Path(process_cwd).resolve() == self.resolved_cmd_cwd.resolve()
        except OSError:
            return False

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Config",
                path=ets_server_config_path(self.directory),
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".sii"}),
                # This file contains the write-only Steam login token.
                read_power_level_override=Power_Level.root,
                write_power_level_override=Power_Level.root,
            ),
        )

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        package_config_path, package_data_path = ets_server_package_paths(self.directory)
        return (
            AppSaveRoot(
                id=_ETS_SERVER_PACKAGE_CONFIG_ROOT_ID,
                label=_ETS_SERVER_PACKAGE_CONFIG_ROOT_LABEL,
                path=package_config_path,
                mode=AppSaveRootMode.SELF,
                suffixes=frozenset({".sii"}),
                include_files=True,
                include_directories=False,
            ),
            AppSaveRoot(
                id=_ETS_SERVER_PACKAGE_DATA_ROOT_ID,
                label=_ETS_SERVER_PACKAGE_DATA_ROOT_LABEL,
                path=package_data_path,
                mode=AppSaveRootMode.SELF,
                suffixes=frozenset({".dat"}),
                include_files=True,
                include_directories=False,
            ),
        )

    @property
    def supports_save_uploads(self) -> bool:
        return True

    def resolve_save_upload_root_id(self, upload_name: str) -> str:
        relative_path = normalise_app_save_relative_path(upload_name)
        if Path(relative_path).name != relative_path:
            raise ValueError("ETS2 server package uploads must not include directories.")
        for root in self.save_file_roots:
            if root.resolved_path.name == relative_path:
                return root.id
        raise ValueError(
            "ETS2 server package uploads must be named server_packages.sii or server_packages.dat."
        )

    def _require_server_package_upload_allowed(self) -> None:
        if self.check_running():
            raise ValueError("Stop the ETS2 server before replacing server packages.")

    def _server_package_upload_target(self, *, root_id: str, upload_name: str) -> tuple[AppSaveRoot, Path]:
        root = get_app_save_root(self.save_file_roots, root_id)
        relative_path = normalise_app_save_relative_path(upload_name)
        target = root.resolved_path
        if Path(relative_path).name != relative_path or relative_path != target.name:
            raise ValueError(f"Upload {target.name} to the selected ETS2 server package destination.")
        return root, target

    def validate_save_upload_file(
        self,
        *,
        root_id: str,
        upload_name: str,
        source_path: Path,
    ) -> None:
        self._require_server_package_upload_allowed()
        self._server_package_upload_target(root_id=root_id, upload_name=upload_name)
        _validate_ets_server_package_source(source_path)

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        self._require_server_package_upload_allowed()
        root, target = self._server_package_upload_target(root_id=root_id, upload_name=upload_name)
        _replace_ets_server_package(source_path=source_path, target=target)
        return describe_app_save_path(root=root, path=target, relative_path=target.name)

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        config_path = ets_server_config_path(self.directory)
        if not config_path.is_file():
            raise RuntimeError(
                "ETS2 server config is missing. Reinstall the server or create its server_config.sii before starting."
            )
        missing_packages = missing_ets_server_package_names(self.directory)
        if missing_packages:
            raise RuntimeError(
                "ETS2 requires exported server packages before it can start: "
                f"{', '.join(missing_packages)}. In a matching ETS2 client, run export_server_packages "
                "while a map is loaded, then upload both files in Server Packages."
            )
        await self._std_launch()
        while not self.check_running():
            await asyncio.sleep(1)

        if self.server_log:
            log.debug(f"{self.name} Tailing: server log")
            self._tail = Tailer(self.check_running, self.server_log, self.file_stdout)
        else:
            raise SystemError("No Log to be passed to Tailer")
        await self._tail.start(self._tail_machers)

        self._running = True
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False

        if self._tail:
            await self._tail.stop()

        await self._terminate()
        return True

    async def kill(self) -> bool:
        self._running = False
        if self._tail:
            await self._tail.stop()
        await self._terminate()
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return None


class Matchers:
    def __init__(self, app: ETS):
        self.app = app
        app._tail_machers.add(self.match_version)
        app._tail_machers.add(self.match_transient)

    async def match_version(self, line: str) -> None:
        if match := _ETS_GAME_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)
            return
        if match := _ETS_PACKSET_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)

    async def match_transient(self, line: str):
        match: Match[str] | None = re.search(
            r"\[MP\] (?P<player>\w+) (connected|disconnected),",
            line,
            re.IGNORECASE,
        )
        if match:
            player: str = str(match.group(1))
            action: str = str(match.group(2)).lower()
            if "disconnected" in action:
                if self.app.relay_notice_player_left_enabled is False:
                    return
                notice_action = PlayerSessionAction.LEFT
            else:
                if self.app.relay_notice_player_joined_enabled is False:
                    return
                notice_action = PlayerSessionAction.JOINED
            notice = self.app.player_session_notice(action=notice_action, source=RelayNoticeSource.APP_LOG)
            app_friendly = getattr(self.app, "friendly", self.app.name)
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    render_notice_text(notice, author_name=player, app_name=app_friendly),
                    player or hikari.UNDEFINED,
                    notice=notice,
                )
            )
