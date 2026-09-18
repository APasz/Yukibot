"""Garry's Mod dedicated-server integration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import socket
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum, auto
from pathlib import Path
from typing import IO, Any, Final, cast

import hikari

import config
from _async_utils import run_blocking
from _security import Power_Level
from apps._app import App, AppPortClaim, AppRuntimeFault, AppRuntimeFaultCode, AppRuntimeFaultKind, NetworkProtocol
from apps._config import App_Config, AppVersion, SteamUpdatePreset
from apps._config_files import AppConfigFileContent, AppConfigFileKind, AppConfigFileRoot
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    IntSettingSpec,
    Setting,
    Setting_Label,
    SettingSpec,
    StringSettingSpec,
)
from apps._steam import SteamGameServerLoginTokenStatus, normalise_steam_game_server_login_token
from apps._updater import SteamCmd_Update_Manager
from config import Activity_Manager
from .workshop import GmodWorkshopSource

log = logging.getLogger(__name__)

GMOD_DEFAULT_PORT: Final[int] = 27015
GMOD_DEFAULT_GAMEMODE: Final[str] = "sandbox"
GMOD_DEFAULT_STARTUP_MAP: Final[str] = "gm_construct"
GMOD_DEFAULT_MAX_PLAYERS: Final[int] = 16
GMOD_DEFAULT_WORKSHOP_AUTO_UPDATE: Final[bool] = True
GMOD_DEFAULT_INSTALL_SUBFOLDER: Final[str] = "garrymod"
GMOD_MANAGE_EMBED_COLOR: Final[int] = 0x1194F0
STEAM_GAME_APP_ID: Final[int] = 4000
STEAM_APP_ID: Final[int] = 4020
GMOD_X64_STEAM_BRANCH: Final[str] = "x86-64"
STEAM_UPDATE_PRESET: Final[SteamUpdatePreset] = SteamUpdatePreset(
    app_id=STEAM_APP_ID,
    default_selected_branch=GMOD_X64_STEAM_BRANCH,
    required_selected_branch=GMOD_X64_STEAM_BRANCH,
)

_GMOD_MANAGED_DIRECTORY_NAME: Final[str] = ".yukibot"
_GMOD_SETTINGS_FILENAME: Final[str] = "gmod-settings.json"
_GMOD_GSLT_FILENAME: Final[str] = "steam-game-server-login-token"
_GMOD_SERVER_CONFIG_FILENAME: Final[str] = "server.cfg"
_GMOD_WORKSHOP_ADDON_DIRECTORY_NAME: Final[str] = "yukibot-workshop"
_GMOD_WORKSHOP_MANIFEST_FILENAME: Final[str] = "yukibot_workshop_downloads.lua"
_GMOD_X64_LAUNCHER_NAME: Final[str] = "srcds_run_x64"
_GMOD_X64_LAUNCH_COMMAND: Final[str] = f"./{_GMOD_X64_LAUNCHER_NAME}"
_GMOD_NO_RESTART_ARGUMENT: Final[str] = "-norestart"
_GMOD_CONSOLE_ARGUMENT: Final[str] = "-console"
_GMOD_BIND_ADDRESS: Final[str] = "0.0.0.0"
_GMOD_IP_ARGUMENT: Final[str] = "-ip"
_GMOD_PORT_ARGUMENT: Final[str] = "-port"
_GMOD_X64_BINARY_RELATIVE_PATH: Final[Path] = Path("bin") / "linux64" / "srcds"
_GMOD_X64_PROCESS_NAME: Final[str] = _GMOD_X64_BINARY_RELATIVE_PATH.name
_GMOD_LAUNCH_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_GMOD_WORKSHOP_ITEM_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[1-9][0-9]{0,19}$")
_GMOD_WORKSHOP_ITEM_ID_LIST_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[\s,]+")
_GMOD_MAX_WORKSHOP_ITEM_ID: Final[int] = (1 << 64) - 1
_GMOD_WORKSHOP_COLLECTION_ID_LABEL: Final[str] = "Workshop collection ID"
_GMOD_CLIENT_CONTENT_WORKSHOP_ITEM_LABEL: Final[str] = "client content Workshop ID"
_GMOD_CLIENT_CONTENT_WORKSHOP_LIST_LABEL: Final[str] = "client content Workshop IDs"
_GMOD_STEAM_ACCOUNT_COMMAND_RE: Final[re.Pattern[str]] = re.compile(
    r"\bsv_setsteamaccount(?:\s|$)",
    re.IGNORECASE,
)
_GMOD_STEAM_ACCOUNT_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<command>\bsv_setsteamaccount\b)
    (?P<spacing>[ \t]+)
    (?:
        (?P<quoted>"(?:\\[^\r\n]|[^"\\\r\n])*")(?=[ \t;\r\n]|$)
        # A malformed quoted value has no safe suffix boundary, so redact through EOL.
        | (?P<unterminated_quote>"[^\r\n]*)
        | (?P<unquoted>(?!//)[^\s;]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_GMOD_GSLT_REJECTED_RE: Final[re.Pattern[str]] = re.compile(r"\bGSL token expired\b", re.IGNORECASE)
_GMOD_CONSOLE_ANSI_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_GMOD_CONSOLE_STATUS_HOSTNAME_RE: Final[re.Pattern[str]] = re.compile(r"^\s*hostname\s*:", re.IGNORECASE)
_GMOD_CONSOLE_STATUS_UDP_ENDPOINT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*udp\s*/\s*ip\s*:", re.IGNORECASE)
_GMOD_CONSOLE_STATUS_MAP_RE: Final[re.Pattern[str]] = re.compile(r"^\s*map\s*:", re.IGNORECASE)
_GMOD_CONSOLE_STATUS_PLAYERS_RE: Final[re.Pattern[str]] = re.compile(r"^\s*players\s*:", re.IGNORECASE)
_GMOD_SOURCE_QUERY_HOST: Final[str] = "127.0.0.1"
_GMOD_SOURCE_INFO_REQUEST: Final[bytes] = b"\xff\xff\xff\xffTSource Engine Query\x00"
_GMOD_SOURCE_RESPONSE_HEADER: Final[bytes] = b"\xff\xff\xff\xff"
_GMOD_SOURCE_INFO_RESPONSE_TYPE: Final[bytes] = b"I"
_GMOD_SOURCE_CHALLENGE_RESPONSE_TYPE: Final[bytes] = b"A"
_GMOD_SOURCE_INFO_RESPONSE_PREFIX: Final[bytes] = _GMOD_SOURCE_RESPONSE_HEADER + _GMOD_SOURCE_INFO_RESPONSE_TYPE
_GMOD_SOURCE_CHALLENGE_RESPONSE_PREFIX: Final[bytes] = (
    _GMOD_SOURCE_RESPONSE_HEADER + _GMOD_SOURCE_CHALLENGE_RESPONSE_TYPE
)
_GMOD_SOURCE_CHALLENGE_SIZE: Final[int] = 4
_GMOD_SOURCE_QUERY_RESPONSE_SIZE: Final[int] = 4_096
_GMOD_SERVER_INFO_QUERY_TIMEOUT_SECONDS: Final[float] = 2.0
_GMOD_STARTUP_READY_TIMEOUT_SECONDS: Final[float] = 900.0
_GMOD_STARTUP_READY_PROBE_INTERVAL_SECONDS: Final[float] = 1.0
_GMOD_STARTUP_STATUS_PROBE_INITIAL_DELAY_SECONDS: Final[float] = 15.0
_GMOD_STARTUP_STATUS_PROBE_INTERVAL_SECONDS: Final[float] = 10.0
_GMOD_CONSOLE_STATUS_COMMAND: Final[str] = "status\n"
_GMOD_LEGACY_PLACEHOLDER_VERSION: Final[str] = "0.0"
_REDACTED_SECRET: Final[str] = "[REDACTED]"
_GMOD_LEGACY_STEAM_ACCOUNT_WARNING: Final[str] = (
    "server.cfg contains legacy/manual sv_setsteamaccount configuration. Remove it and use "
    "Yukibot's dedicated Steam Game Server Login Token (GSLT) setting instead."
)
_GMOD_GSLT_REJECTED_SUMMARY: Final[str] = "Steam rejected the configured Game Server Login Token."
_GMOD_GSLT_REJECTED_REMEDIATION: Final[str] = (
    f"Create a fresh GSLT for Garry's Mod (App ID {STEAM_GAME_APP_ID}), replace it in Properties, then restart."
)


class _GmodStartupStatusPhase(Enum):
    """Ordered fields expected from the dedicated-server ``status`` command."""

    WAITING_FOR_HOSTNAME = auto()
    WAITING_FOR_UDP_ENDPOINT = auto()
    WAITING_FOR_MAP = auto()
    WAITING_FOR_PLAYERS = auto()
    READY = auto()


def gmod_server_config_path(directory: Path) -> Path:
    """Return Garry's Mod's normal server configuration path."""

    return directory.absolute() / "garrysmod" / "cfg" / _GMOD_SERVER_CONFIG_FILENAME


def gmod_managed_directory(directory: Path) -> Path:
    """Return Yukibot's private per-instance Garry's Mod state directory."""

    return directory.absolute() / _GMOD_MANAGED_DIRECTORY_NAME


def gmod_settings_path(directory: Path) -> Path:
    """Return the persisted Yukibot launch-settings path for an instance."""

    return gmod_managed_directory(directory) / _GMOD_SETTINGS_FILENAME


def gmod_game_server_login_token_path(directory: Path) -> Path:
    """Return the private, write-only Steam Game Server Login Token path."""

    return gmod_managed_directory(directory) / _GMOD_GSLT_FILENAME


def gmod_workshop_manifest_path(directory: Path) -> Path:
    """Return Yukibot's server-only Workshop download manifest path."""

    return (
        directory.absolute()
        / "garrysmod"
        / "addons"
        / _GMOD_WORKSHOP_ADDON_DIRECTORY_NAME
        / "lua"
        / "autorun"
        / "server"
        / _GMOD_WORKSHOP_MANIFEST_FILENAME
    )


def _require_gmod_x64_installation(directory: Path) -> None:
    """Validate the required 64-bit runtime or explain how to repair the install."""

    launcher = directory / _GMOD_X64_LAUNCHER_NAME
    if not launcher.is_file():
        raise FileNotFoundError(
            f"Garry's Mod 64-bit launcher is missing: {launcher}. "
            f"Update the server on Steam branch {GMOD_X64_STEAM_BRANCH!r}."
        )
    binary = directory / _GMOD_X64_BINARY_RELATIVE_PATH
    if not binary.is_file():
        raise FileNotFoundError(
            f"Garry's Mod 64-bit engine binary is missing: {binary}. "
            f"Update the server on Steam branch {GMOD_X64_STEAM_BRANCH!r}."
        )


def resolve_gmod_game_port(port: int | None) -> int:
    """Resolve one configured Garry's Mod game port."""

    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        raise TypeError("Garry's Mod game port must be an integer.")
    resolved_port = GMOD_DEFAULT_PORT if port is None else port
    if not 1 <= resolved_port <= 65535:
        raise ValueError("Garry's Mod game port must be between 1 and 65535.")
    return resolved_port


def _normalise_gmod_launch_name(raw_value: object, *, label: str) -> str:
    if not isinstance(raw_value, str):
        raise TypeError(f"Garry's Mod {label} must be text.")
    value = raw_value.strip()
    if _GMOD_LAUNCH_NAME_RE.fullmatch(value) is None:
        raise ValueError(f"Garry's Mod {label} must use letters, numbers, dots, underscores, or hyphens.")
    return value


def _normalise_gmod_workshop_item_id(raw_value: object, *, label: str) -> str:
    """Validate one opaque Steam Workshop identifier without treating it as Lua."""

    if not isinstance(raw_value, str):
        raise TypeError(f"Garry's Mod {label} must be text.")
    value = raw_value.strip()
    if _GMOD_WORKSHOP_ITEM_ID_RE.fullmatch(value) is None or int(value) > _GMOD_MAX_WORKSHOP_ITEM_ID:
        raise ValueError(f"Garry's Mod {label} must be a non-zero decimal Steam Workshop ID.")
    return value


def _normalise_gmod_workshop_item_ids(
    raw_values: Sequence[object],
    *,
    item_label: str,
    list_label: str,
    deduplicate: bool = False,
) -> tuple[str, ...]:
    """Validate one ordered Workshop ID list, optionally preserving first occurrences."""

    if isinstance(raw_values, (str, bytes)):
        raise TypeError(f"Garry's Mod {list_label} must be a list.")
    item_ids = tuple(
        _normalise_gmod_workshop_item_id(raw_value, label=item_label) for raw_value in raw_values
    )
    if len(set(item_ids)) != len(item_ids):
        if deduplicate:
            return tuple(dict.fromkeys(item_ids))
        raise ValueError(f"Garry's Mod {list_label} must not contain duplicates.")
    return item_ids


def gmod_start_command(
    *,
    port: int | None,
    max_players: int,
    gamemode: str,
    startup_map: str,
    game_server_login_token: str,
    workshop_collection_id: str | None = None,
    workshop_auto_update: bool = GMOD_DEFAULT_WORKSHOP_AUTO_UPDATE,
) -> list[str]:
    """Build the Linux dedicated-server command from persisted launch values."""

    if isinstance(max_players, bool) or not isinstance(max_players, int):
        raise TypeError("Garry's Mod max players must be an integer.")
    if max_players < 1:
        raise ValueError("Garry's Mod max players must be at least 1.")
    resolved_port = resolve_gmod_game_port(port)
    resolved_gamemode = _normalise_gmod_launch_name(gamemode, label="gamemode")
    resolved_map = _normalise_gmod_launch_name(startup_map, label="startup map")
    token = normalise_steam_game_server_login_token(game_server_login_token)
    if not isinstance(workshop_auto_update, bool):
        raise TypeError("Garry's Mod Workshop auto-update setting must be a bool.")
    collection_id = (
        None
        if workshop_collection_id is None
        else _normalise_gmod_workshop_item_id(workshop_collection_id, label=_GMOD_WORKSHOP_COLLECTION_ID_LABEL)
    )
    command = [
        _GMOD_X64_LAUNCH_COMMAND,
        _GMOD_NO_RESTART_ARGUMENT,
        _GMOD_CONSOLE_ARGUMENT,
        "-game",
        "garrysmod",
        _GMOD_IP_ARGUMENT,
        _GMOD_BIND_ADDRESS,
        _GMOD_PORT_ARGUMENT,
        str(resolved_port),
        "+maxplayers",
        str(max_players),
    ]
    if collection_id is not None:
        command.extend(
            (
                "+host_workshop_collection",
                collection_id,
                "+host_workshop_autoupdate",
                "1" if workshop_auto_update else "0",
            )
        )
    command.extend(
        (
            "+gamemode",
            resolved_gamemode,
            "+map",
            resolved_map,
            "+sv_setsteamaccount",
            token,
        )
    )
    return command


def ensure_gmod_managed_files(directory: Path) -> tuple[Path, ...]:
    """Create missing non-secret Garry's Mod configuration files without overwriting game files."""

    created: list[Path] = []
    server_config = gmod_server_config_path(directory)
    if not server_config.exists():
        server_config.parent.mkdir(parents=True, exist_ok=True)
        server_config.write_text("// Garry's Mod server configuration.\n", config.STR_ENCODE)
        created.append(server_config)

    managed_directory = gmod_managed_directory(directory)
    managed_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(managed_directory, 0o700)
    settings = gmod_settings_path(directory)
    if not settings.exists():
        settings.write_text(
            json.dumps(
                {
                    "startup_map": GMOD_DEFAULT_STARTUP_MAP,
                    "gamemode": GMOD_DEFAULT_GAMEMODE,
                    "max_players": GMOD_DEFAULT_MAX_PLAYERS,
                    "workshop_collection_id": "",
                    "workshop_auto_update": GMOD_DEFAULT_WORKSHOP_AUTO_UPDATE,
                    "client_content_workshop_ids": [],
                },
                indent=4,
            )
            + "\n",
            config.STR_ENCODE,
        )
        created.append(settings)
    return tuple(created)


def render_gmod_workshop_manifest(workshop_item_ids: tuple[str, ...]) -> str:
    """Render the server-only Lua manifest for explicitly configured client content."""

    normalised_ids = _normalise_gmod_workshop_item_ids(
        workshop_item_ids,
        item_label=_GMOD_CLIENT_CONTENT_WORKSHOP_ITEM_LABEL,
        list_label=_GMOD_CLIENT_CONTENT_WORKSHOP_LIST_LABEL,
    )
    lines = [
        "-- Generated by Yukibot. Configure client content Workshop IDs in Yukibot; do not edit this file.",
        "if not SERVER then return end",
        "",
        *(f'resource.AddWorkshop("{workshop_item_id}")' for workshop_item_id in normalised_ids),
    ]
    return "\n".join(lines) + "\n"


def sync_gmod_workshop_manifest(directory: Path, workshop_item_ids: tuple[str, ...]) -> Path:
    """Synchronise Yukibot's owned Workshop download manifest for one instance."""

    manifest_path = gmod_workshop_manifest_path(directory)
    content = render_gmod_workshop_manifest(workshop_item_ids)
    try:
        previous_content = manifest_path.read_text(config.STR_ENCODE)
    except FileNotFoundError:
        previous_content = None
    if previous_content != content:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(content, config.STR_ENCODE)
    return manifest_path


async def _receive_gmod_source_query_response(
    sock: socket.socket,
    *,
    deadline: float,
) -> bytes | None:
    """Receive one local Source query packet before an absolute loop-time deadline."""

    loop = asyncio.get_running_loop()
    remaining_seconds = deadline - loop.time()
    if remaining_seconds <= 0:
        return None
    try:
        response = await asyncio.wait_for(
            loop.sock_recv(sock, _GMOD_SOURCE_QUERY_RESPONSE_SIZE),
            timeout=remaining_seconds,
        )
    except (OSError, TimeoutError):
        return None
    return response


def _is_gmod_source_info_response(response: bytes) -> bool:
    return len(response) > len(_GMOD_SOURCE_INFO_RESPONSE_PREFIX) and response.startswith(
        _GMOD_SOURCE_INFO_RESPONSE_PREFIX
    )


def _advance_gmod_startup_status_phase(
    phase: _GmodStartupStatusPhase,
    line: str,
) -> _GmodStartupStatusPhase:
    """Advance a parsed dedicated-server status response by one console line."""

    cleaned_line = _GMOD_CONSOLE_ANSI_ESCAPE_RE.sub("", line)
    if _GMOD_CONSOLE_STATUS_HOSTNAME_RE.match(cleaned_line) is not None:
        return _GmodStartupStatusPhase.WAITING_FOR_UDP_ENDPOINT
    if (
        phase is _GmodStartupStatusPhase.WAITING_FOR_UDP_ENDPOINT
        and _GMOD_CONSOLE_STATUS_UDP_ENDPOINT_RE.match(cleaned_line) is not None
    ):
        return _GmodStartupStatusPhase.WAITING_FOR_MAP
    if (
        phase is _GmodStartupStatusPhase.WAITING_FOR_MAP
        and _GMOD_CONSOLE_STATUS_MAP_RE.match(cleaned_line) is not None
    ):
        return _GmodStartupStatusPhase.WAITING_FOR_PLAYERS
    if (
        phase is _GmodStartupStatusPhase.WAITING_FOR_PLAYERS
        and _GMOD_CONSOLE_STATUS_PLAYERS_RE.match(cleaned_line) is not None
    ):
        return _GmodStartupStatusPhase.READY
    return phase


def _write_gmod_console_status(stream: IO[str]) -> None:
    """Request the current Source dedicated-server status through a trusted command."""

    stream.write(_GMOD_CONSOLE_STATUS_COMMAND)
    stream.flush()


async def gmod_server_info_responds(
    *,
    port: int,
    timeout_seconds: float | int = _GMOD_SERVER_INFO_QUERY_TIMEOUT_SECONDS,
) -> bool:
    """Return whether the local server answers a Source A2S_INFO query within the timeout."""

    resolved_port = resolve_gmod_game_port(port)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("Garry's Mod server-info query timeout must be a number.")
    try:
        resolved_timeout_seconds = float(timeout_seconds)
    except OverflowError as xcp:
        raise ValueError("Garry's Mod server-info query timeout must be a positive finite number.") from xcp
    if not math.isfinite(resolved_timeout_seconds) or resolved_timeout_seconds <= 0:
        raise ValueError("Garry's Mod server-info query timeout must be a positive finite number.")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + resolved_timeout_seconds
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            await loop.sock_connect(sock, (_GMOD_SOURCE_QUERY_HOST, resolved_port))
            await loop.sock_sendall(sock, _GMOD_SOURCE_INFO_REQUEST)
            response = await _receive_gmod_source_query_response(sock, deadline=deadline)
            if response is None:
                return False
            if _is_gmod_source_info_response(response):
                return True
            if not response.startswith(_GMOD_SOURCE_CHALLENGE_RESPONSE_PREFIX):
                return False
            challenge_start = len(_GMOD_SOURCE_CHALLENGE_RESPONSE_PREFIX)
            challenge_end = challenge_start + _GMOD_SOURCE_CHALLENGE_SIZE
            if len(response) < challenge_end:
                return False
            await loop.sock_sendall(sock, _GMOD_SOURCE_INFO_REQUEST + response[challenge_start:challenge_end])
            response = await _receive_gmod_source_query_response(sock, deadline=deadline)
            return response is not None and _is_gmod_source_info_response(response)
    except OSError:
        return False


def _read_gmod_game_server_login_token(directory: Path) -> str | None:
    """Read an instance's private Steam token only for local launch handling."""

    token_path = gmod_game_server_login_token_path(directory)
    try:
        raw_value = token_path.read_text(config.STR_ENCODE)
    except FileNotFoundError:
        return None
    token = raw_value.strip()
    return token or None


def write_gmod_game_server_login_token(*, directory: Path, token: str | None) -> None:
    """Atomically set or clear a private Steam Game Server Login Token."""

    token_path = gmod_game_server_login_token_path(directory)
    if token is None:
        token_path.unlink(missing_ok=True)
        return

    normalised_token = normalise_steam_game_server_login_token(token)
    token_directory = token_path.parent
    token_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(token_directory, 0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=config.STR_ENCODE,
            prefix=f".{token_path.name}.",
            suffix=".tmp",
            dir=token_directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(f"{normalised_token}\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(token_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def redact_gmod_game_server_login_token(text: str, *, token: str | None) -> str:
    """Redact one known GSLT from output that may be surfaced by Yukibot."""

    if token is None or not token:
        return text
    return text.replace(token, _REDACTED_SECRET)


def _load_gmod_settings_payload(pointer: Path) -> dict[str, Any]:
    try:
        raw_payload = json.loads(pointer.read_text(config.STR_ENCODE))
    except json.JSONDecodeError as xcp:
        raise ValueError("Garry's Mod launch settings must be valid JSON.") from xcp
    if not isinstance(raw_payload, dict) or not all(isinstance(key, str) for key in raw_payload):
        raise ValueError("Garry's Mod launch settings must be a JSON object.")
    return cast(dict[str, Any], raw_payload)


def _gmod_version_needs_manifest_refresh(version: AppVersion | None) -> bool:
    """Return whether a stored version lacks a reliable GMod version identity."""

    return version is None or version.main is None or version.main == _GMOD_LEGACY_PLACEHOLDER_VERSION


class _GmodWorkshopCollectionSettingSpec(StringSettingSpec):
    """Persist one optional, validated Workshop collection identifier."""

    def __init__(self) -> None:
        super().__init__(allow_blank=True)

    def parse(self, raw_value: str) -> str:
        if not raw_value.strip():
            return ""
        return _normalise_gmod_workshop_item_id(raw_value, label=_GMOD_WORKSHOP_COLLECTION_ID_LABEL)


class _GmodWorkshopItemListSettingSpec(SettingSpec[tuple[str, ...]]):
    """Parse a user-editable list of Workshop IDs into safe Lua data values."""

    def __init__(self) -> None:
        super().__init__(tuple)

    def parse(self, raw_value: str) -> tuple[str, ...]:
        value = raw_value.strip()
        if not value:
            return ()
        return _normalise_gmod_workshop_item_ids(
            _GMOD_WORKSHOP_ITEM_ID_LIST_SEPARATOR_RE.split(value),
            item_label=_GMOD_CLIENT_CONTENT_WORKSHOP_ITEM_LABEL,
            list_label=_GMOD_CLIENT_CONTENT_WORKSHOP_LIST_LABEL,
        )

    def serialise_value(self, value: object) -> str:
        if isinstance(value, hikari.UndefinedType):
            return ""
        if not isinstance(value, (list, tuple)):
            raise TypeError("Client content Workshop IDs must be a list or tuple.")
        item_ids = _normalise_gmod_workshop_item_ids(
            value,
            item_label=_GMOD_CLIENT_CONTENT_WORKSHOP_ITEM_LABEL,
            list_label=_GMOD_CLIENT_CONTENT_WORKSHOP_LIST_LABEL,
        )
        return ", ".join(item_ids)

    def display_value(self, value: tuple[str, ...] | hikari.UndefinedType) -> str:
        serialised_value = self.serialise_value(value)
        return serialised_value or "None"


class Gmod_Settings(App_Settings):
    """Yukibot-owned startup and Workshop content-delivery settings."""

    def __init__(self, pointer: Path) -> None:
        launch_name = StringSettingSpec(raw_validator=lambda value: _GMOD_LAUNCH_NAME_RE.fullmatch(value) is not None)
        super().__init__(
            pointer,
            [
                Setting[str](
                    launch_name,
                    Setting_Label.map_name,
                    "startup_map",
                    (),
                    default=GMOD_DEFAULT_STARTUP_MAP,
                    power_level=Power_Level.sudo,
                    desc="Map to load when the server next starts.",
                ),
                Setting[str](
                    launch_name,
                    "Gamemode",
                    "gamemode",
                    (),
                    default=GMOD_DEFAULT_GAMEMODE,
                    power_level=Power_Level.sudo,
                    desc="Gamemode folder to load when the server next starts.",
                ),
                Setting[int](
                    IntSettingSpec(min_value=1),
                    Setting_Label.max_player,
                    "max_players",
                    (),
                    default=GMOD_DEFAULT_MAX_PLAYERS,
                    power_level=Power_Level.sudo,
                    desc="Maximum player slots when the server next starts.",
                ),
                Setting[str](
                    _GmodWorkshopCollectionSettingSpec(),
                    "Workshop Collection",
                    "workshop_collection_id",
                    (),
                    default="",
                    power_level=Power_Level.sudo,
                    show_in_settings=False,
                    desc="Public or unlisted Workshop collection to mount on the next start; leave blank to disable.",
                ),
                Setting[bool](
                    BoolSettingSpec(),
                    "Workshop Auto-update",
                    "workshop_auto_update",
                    (),
                    default=GMOD_DEFAULT_WORKSHOP_AUTO_UPDATE,
                    power_level=Power_Level.sudo,
                    show_in_settings=False,
                    desc="Update the configured Workshop collection when the server next starts.",
                ),
                Setting[tuple[str, ...]](
                    _GmodWorkshopItemListSettingSpec(),
                    "Client Content Workshop IDs",
                    "client_content_workshop_ids",
                    (),
                    default=(),
                    power_level=Power_Level.sudo,
                    show_in_settings=False,
                    desc=(
                        "Comma- or whitespace-separated Workshop item IDs to require clients to download on the "
                        "next server start; do not include Lua-only addons."
                    ),
                ),
            ],
        )

    def load(self) -> None:
        payload = _load_gmod_settings_payload(self.pointer)
        for setting in self.options:
            setting.get(payload)

    def save(self) -> dict[str, Any]:
        payload = _load_gmod_settings_payload(self.pointer)
        for setting in self.options:
            setting.set(payload)
        self.pointer.write_text(json.dumps(payload, indent=4) + "\n", config.STR_ENCODE)
        return payload

    @property
    def startup_map(self) -> str:
        return self._string_setting_value("startup_map")

    @property
    def gamemode(self) -> str:
        return self._string_setting_value("gamemode")

    @property
    def max_players(self) -> int:
        setting = self.get_setting("max_players")
        if setting is None or not isinstance(setting.value, int):
            raise TypeError("Garry's Mod max players setting is unavailable.")
        return setting.value

    @property
    def workshop_collection_id(self) -> str | None:
        return self._string_setting_value("workshop_collection_id") or None

    @property
    def workshop_auto_update(self) -> bool:
        setting = self.get_setting("workshop_auto_update")
        if setting is None or not isinstance(setting.value, bool):
            raise TypeError("Garry's Mod Workshop auto-update setting is unavailable.")
        return setting.value

    @property
    def client_content_workshop_ids(self) -> tuple[str, ...]:
        setting = self.get_setting("client_content_workshop_ids")
        if setting is None:
            raise TypeError("Garry's Mod client content Workshop IDs setting is unavailable.")
        value = cast(object, setting.value)
        if not isinstance(value, tuple) or not all(isinstance(workshop_item_id, str) for workshop_item_id in value):
            raise TypeError("Garry's Mod client content Workshop IDs setting is unavailable.")
        return cast(tuple[str, ...], value)

    def _string_setting_value(self, key: str) -> str:
        setting = self.get_setting(key)
        if setting is None or not isinstance(setting.value, str):
            raise TypeError(f"Garry's Mod {key} setting is unavailable.")
        return setting.value


def _source_config_active_content(line: str) -> str:
    """Return one Source config line before an unquoted ``//`` comment."""

    in_quotes = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_quotes and character == "\\":
            escaped = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and character == "/" and line.startswith("//", index):
            return line[:index]
    return line


def _server_config_contains_steam_account_command(content: str) -> bool:
    return any(
        _GMOD_STEAM_ACCOUNT_COMMAND_RE.search(_source_config_active_content(line)) is not None
        for line in content.splitlines()
    )


def _redact_server_config_steam_account_command(content: str) -> str:
    """Redact GSLT values without interpreting the rest of a Source config line."""

    def redact_value(match: re.Match[str]) -> str:
        redacted_value = (
            f'"{_REDACTED_SECRET}"' if match.group("quoted") is not None else _REDACTED_SECRET
        )
        return f"{match.group('command')}{match.group('spacing')}{redacted_value}"

    def redact_line(line: str) -> str:
        active_content = _source_config_active_content(line)
        return _GMOD_STEAM_ACCOUNT_VALUE_RE.sub(redact_value, active_content) + line[len(active_content) :]

    return "".join(redact_line(line) for line in content.splitlines(keepends=True))


async def prepare_gmod_server_installation(
    *,
    directory: Path,
    game_server_login_token: str | None,
) -> None:
    """Prepare a freshly downloaded GMod server before it is registered."""

    _require_gmod_x64_installation(directory)
    if game_server_login_token is None:
        raise ValueError("Garry's Mod requires a Steam Game Server Login Token.")
    ensure_gmod_managed_files(directory)
    write_gmod_game_server_login_token(directory=directory, token=game_server_login_token)


class Gmod(App[App_Config]):
    """A Linux Garry's Mod dedicated server managed through SteamCMD."""

    uses_process_group_termination = True

    @staticmethod
    def _base_launch_command() -> list[str]:
        """Return the non-secret portion safe to retain outside an active launch."""

        return [
            _GMOD_X64_LAUNCH_COMMAND,
            _GMOD_NO_RESTART_ARGUMENT,
            _GMOD_CONSOLE_ARGUMENT,
            "-game",
            "garrysmod",
        ]

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = GMOD_MANAGE_EMBED_COLOR
        if cfg.steam_update is not None:
            cfg.steam_update = STEAM_UPDATE_PRESET.normalise_config(cfg.steam_update)
        self.proc_name = _GMOD_X64_PROCESS_NAME
        self.proc_cmd = [self.proc_name, "-game", "garrysmod"]
        ensure_gmod_managed_files(cfg.directory)
        # App initialisation logs this value. The real command is only assembled
        # at launch, after the private token has been loaded.
        self.cmd_start = self._base_launch_command()
        self.process = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stdout_capture_started: bool = False
        self._launch_token: str | None = None
        self._startup_status_phase = _GmodStartupStatusPhase.WAITING_FOR_HOSTNAME
        self._startup_status_ready_event: asyncio.Event | None = None
        super().__init__(bot, am, cfg, Gmod_Settings(gmod_settings_path(cfg.directory)))
        self._workshop_source = GmodWorkshopSource(settings=self._require_settings)
        self.add_mod_source(self._workshop_source)
        if cfg.steam_update is not None:
            self.updater = SteamCmd_Update_Manager(self)
            if _gmod_version_needs_manifest_refresh(cfg.version):
                self.apply_version(self.detect_installed_version(), persist=False)

    @property
    def version_display(self) -> str:
        """Avoid presenting the former installer placeholder as a GMod release."""

        version = self.cfg.version
        if version is not None and version.main == _GMOD_LEGACY_PLACEHOLDER_VERSION:
            steam_display = version.steam_display_value
            if version.steam_build is not None and steam_display is not None:
                return steam_display
            return "none"
        return super().version_display

    def detect_installed_version(self) -> AppVersion | None:
        """Report the Steam build recorded for this dedicated-server installation."""

        updater = self.updater
        if not isinstance(updater, SteamCmd_Update_Manager):
            return None
        return updater.installed_manifest_version()

    @property
    def listening_port_claims(self) -> tuple[AppPortClaim, ...]:
        return (
            AppPortClaim(
                protocol=NetworkProtocol.UDP,
                port=resolve_gmod_game_port(self.cfg.join_port),
                purpose="game server",
            ),
        )

    @property
    def steam_game_server_login_token_status(self) -> SteamGameServerLoginTokenStatus:
        return SteamGameServerLoginTokenStatus(
            game_app_id=STEAM_GAME_APP_ID,
            configured=_read_gmod_game_server_login_token(self.directory) is not None,
        )

    def _set_steam_game_server_login_token(self, token: str | None) -> None:
        write_gmod_game_server_login_token(directory=self.directory, token=token)

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Config",
                path=gmod_server_config_path(self.directory),
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset({".cfg"}),
            ),
        )

    def read_config_file(self, file_id: str) -> AppConfigFileContent:
        config_content = super().read_config_file(file_id)
        has_legacy_steam_account_command = _server_config_contains_steam_account_command(config_content.content)
        return replace(
            config_content,
            content=_redact_server_config_steam_account_command(config_content.content),
            warning=_GMOD_LEGACY_STEAM_ACCOUNT_WARNING if has_legacy_steam_account_command else None,
        )

    def write_config_file(self, file_id: str, content: str) -> AppConfigFileContent:
        if _server_config_contains_steam_account_command(content):
            raise ValueError("sv_setsteamaccount is managed through the Steam Game Server Login Token setting.")
        return super().write_config_file(file_id, content)

    def _require_settings(self) -> Gmod_Settings:
        """Return this instance's typed settings or fail before launch work begins."""

        settings = self.settings
        if settings is None or not isinstance(settings.app, Gmod_Settings):
            raise RuntimeError("Garry's Mod launch settings are unavailable.")
        return settings.app

    @property
    def workshop_source(self) -> GmodWorkshopSource:
        """Return the read-only Workshop source owned by this GMod instance."""

        return self._workshop_source

    def update_workshop_collection(self, collection_id: str | None) -> None:
        """Persist one validated collection ID and invalidate its source snapshot."""

        self._update_workshop_setting(
            key="workshop_collection_id",
            value="" if collection_id is None else collection_id,
            discard_source_snapshot=True,
        )

    def update_workshop_auto_update(self, enabled: bool) -> None:
        """Persist the next-start collection auto-update setting."""

        if not isinstance(enabled, bool):
            raise TypeError("Workshop auto-update enabled state must be a bool.")
        self._update_workshop_setting(
            key="workshop_auto_update",
            value="true" if enabled else "false",
            discard_source_snapshot=False,
        )

    def update_client_content_workshop_ids(self, item_ids: Sequence[str]) -> Path:
        """Persist explicit resource.AddWorkshop IDs and synchronise their manifest."""

        deduplicated_item_ids = _normalise_gmod_workshop_item_ids(
            item_ids,
            item_label=_GMOD_CLIENT_CONTENT_WORKSHOP_ITEM_LABEL,
            list_label=_GMOD_CLIENT_CONTENT_WORKSHOP_LIST_LABEL,
            deduplicate=True,
        )
        self._update_workshop_setting(
            key="client_content_workshop_ids",
            value=", ".join(deduplicated_item_ids),
            discard_source_snapshot=True,
        )
        return self._sync_workshop_manifest()

    def _update_workshop_setting(
        self,
        *,
        key: str,
        value: str,
        discard_source_snapshot: bool,
    ) -> None:
        settings = self._require_settings()
        setting = settings.get_setting(key)
        if setting is None:
            raise RuntimeError(f"Garry's Mod Workshop setting {key!r} is unavailable.")
        setting.update(value)
        settings.save()
        self.has_mod_catalog.invalidate_source(
            self._workshop_source.kind,
            discard_snapshot=discard_source_snapshot,
        )

    def _launch_command(self, token: str) -> list[str]:
        settings = self._require_settings()
        return gmod_start_command(
            port=self.cfg.join_port,
            max_players=settings.max_players,
            gamemode=settings.gamemode,
            startup_map=settings.startup_map,
            game_server_login_token=token,
            workshop_collection_id=settings.workshop_collection_id,
            workshop_auto_update=settings.workshop_auto_update,
        )

    def _sync_workshop_manifest(self) -> Path:
        """Write the server-only content manifest from the current saved settings."""

        return sync_gmod_workshop_manifest(self.directory, self._require_settings().client_content_workshop_ids)

    def _clear_launch_token(self) -> None:
        """Remove the raw token from transient command state after a launch attempt."""

        self.cmd_start = self._base_launch_command()
        self._launch_token = None
        self._startup_status_phase = _GmodStartupStatusPhase.WAITING_FOR_HOSTNAME
        self._startup_status_ready_event = None

    def _reset_startup_status_probe(self) -> None:
        """Prepare a fresh, local console-status readiness probe for one launch."""

        self._startup_status_phase = _GmodStartupStatusPhase.WAITING_FOR_HOSTNAME
        self._startup_status_ready_event = asyncio.Event()

    def log_launch_context(self) -> None:
        command = [
            redact_gmod_game_server_login_token(argument, token=self._launch_token) for argument in self.cmd_start
        ]
        log.info(
            "Launch config for %s: scope=%s directory=%s cwd=%s cmd_start=%s join_host=%s join_port=%s",
            self.name,
            self.scope,
            self.directory,
            self.resolved_cmd_cwd,
            command,
            self.cfg.join_host,
            self.cfg.join_port,
        )

    def _observe_startup_console_line(self, line: str) -> None:
        """Record one captured console line for the active status readiness probe."""

        ready_event = self._startup_status_ready_event
        if ready_event is None or ready_event.is_set():
            return
        self._startup_status_phase = _advance_gmod_startup_status_phase(self._startup_status_phase, line)
        if self._startup_status_phase is _GmodStartupStatusPhase.READY:
            ready_event.set()

    async def _request_startup_console_status(self) -> bool:
        """Ask the local dedicated-server console for a complete status response."""

        process = self.process
        if process is None or process.stdin is None or not self.check_running():
            return False
        try:
            await run_blocking(_write_gmod_console_status, process.stdin)
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True

    async def _tee(
        self,
        stream: IO[str] | None,
        dest: Path,
        label: str,
        *,
        redaction_token: str | None = None,
    ) -> None:
        if stream is None:
            return
        token = self._launch_token if redaction_token is None else redaction_token
        with dest.open("w", encoding=config.STR_ENCODE) as output:
            while line := await run_blocking(stream.readline):
                redacted_line = redact_gmod_game_server_login_token(line, token=token)
                output.write(redacted_line)
                output.flush()
                self._observe_startup_console_line(redacted_line)
                if not config.SILENT_DEBUG:
                    log.debug("%s: %s", label, redacted_line.strip())

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
            return Path(process_cwd).resolve() == self.directory.resolve()
        except OSError:
            return False

    async def handle_unexpected_stop(self) -> None:
        try:
            await super().handle_unexpected_stop()
        finally:
            try:
                await self._drain_stdout_task()
            finally:
                self._clear_launch_token()

    def diagnose_unexpected_stop(self) -> AppRuntimeFault | None:
        """Recognise safe, actionable GMod fatal errors after stdout has drained."""

        if not self._stdout_capture_started:
            return None
        stdout_tail = self.read_stdout_tail()
        if not any(_GMOD_GSLT_REJECTED_RE.search(line) is not None for line in stdout_tail.lines):
            return None
        return AppRuntimeFault(
            kind=AppRuntimeFaultKind.CRASH,
            code=AppRuntimeFaultCode.GMOD_STEAM_GAME_SERVER_LOGIN_TOKEN_REJECTED,
            summary=_GMOD_GSLT_REJECTED_SUMMARY,
            remediation=_GMOD_GSLT_REJECTED_REMEDIATION,
        )

    def _start_stdout_capture(self, stream: IO[str], *, redaction_token: str) -> None:
        self._stdout_capture_started = True
        self._stdout_task = asyncio.create_task(
            self._tee(
                stream,
                self.file_stdout,
                "STDOUT",
                redaction_token=redaction_token,
            )
        )

    async def _cleanup_failed_startup(self, *, terminate_process: bool) -> None:
        """Dispose of launch state when this start attempt cannot become ready."""

        self._running = False
        if terminate_process and self.check_running():
            await self._terminate_runtime()
            return
        try:
            await self.handle_unexpected_stop()
        finally:
            try:
                await self._drain_stderr_task()
            except Exception as xcp:
                log.warning("%s stderr reader failed during startup cleanup: error_type=%s", self.name, type(xcp).__name__)
            finally:
                self._clear_launch_token()

    async def start(self) -> bool:
        self._stdout_capture_started = False
        self.clear_runtime_fault()
        _require_gmod_x64_installation(self.directory)
        token = _read_gmod_game_server_login_token(self.directory)
        if token is None:
            raise ValueError("Configure a Steam Game Server Login Token before starting Garry's Mod.")
        try:
            token = normalise_steam_game_server_login_token(token)
        except (TypeError, ValueError) as xcp:
            raise ValueError("The configured Steam Game Server Login Token is invalid.") from xcp

        self._reset_startup_status_probe()
        try:
            self._sync_workshop_manifest()
            self._launch_token = token
            self.cmd_start = self._launch_command(token)
        except Exception:
            self._clear_launch_token()
            raise
        try:
            await self._std_launch()
        except asyncio.CancelledError:
            await self._cleanup_failed_startup(terminate_process=True)
            raise
        except Exception:
            await self._cleanup_failed_startup(terminate_process=True)
            raise RuntimeError("Garry's Mod could not be launched.") from None
        process = self.process
        if process is None or process.stdout is None or not self.check_running():
            if process is not None and process.stdout is not None:
                self._start_stdout_capture(process.stdout, redaction_token=token)
            await self._cleanup_failed_startup(terminate_process=True)
            raise RuntimeError("Garry's Mod exited before startup completed.")
        self._start_stdout_capture(process.stdout, redaction_token=token)
        try:
            await self._wait_for_startup_ready()
        except (Exception, asyncio.CancelledError):
            await self._cleanup_failed_startup(terminate_process=True)
            raise
        self._running = True
        return True

    async def _wait_for_startup_ready(self) -> None:
        """Wait for a Source query or console status, as playable GMod servers may decline A2S."""

        status_ready_event = self._startup_status_ready_event
        if status_ready_event is None:
            raise RuntimeError("Garry's Mod startup status probe was not initialised.")
        game_port = resolve_gmod_game_port(self.cfg.join_port)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _GMOD_STARTUP_READY_TIMEOUT_SECONDS
        next_status_probe_at = loop.time() + _GMOD_STARTUP_STATUS_PROBE_INITIAL_DELAY_SECONDS
        while True:
            if not self.check_running():
                raise RuntimeError("Garry's Mod stopped before its readiness checks completed.")
            if status_ready_event.is_set():
                log.info("%s completed a local dedicated-server console status check.", self.name)
                return
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                raise TimeoutError(
                    f"Garry's Mod did not answer a server-info query or complete a local status command within "
                    f"{_GMOD_STARTUP_READY_TIMEOUT_SECONDS:.0f}s."
                )
            if await gmod_server_info_responds(
                port=game_port,
                timeout_seconds=min(_GMOD_SERVER_INFO_QUERY_TIMEOUT_SECONDS, remaining_seconds),
            ):
                log.info("%s answered a local Source server-info query.", self.name)
                return
            if status_ready_event.is_set():
                log.info("%s completed a local dedicated-server console status check.", self.name)
                return
            now = loop.time()
            if now >= next_status_probe_at:
                if await self._request_startup_console_status():
                    log.debug("Requested local dedicated-server status for %s startup readiness.", self.name)
                next_status_probe_at = now + _GMOD_STARTUP_STATUS_PROBE_INTERVAL_SECONDS
            remaining_seconds = deadline - loop.time()
            if remaining_seconds > 0:
                until_next_status_probe = max(0.0, next_status_probe_at - loop.time())
                await asyncio.sleep(
                    min(
                        _GMOD_STARTUP_READY_PROBE_INTERVAL_SECONDS,
                        remaining_seconds,
                        until_next_status_probe,
                    )
                )

    async def stop(self) -> bool:
        return await self._terminate_runtime()

    async def kill(self) -> bool:
        return await self._terminate_runtime()

    async def _terminate_runtime(self) -> bool:
        """Terminate the process and dispose of transient launch secrets."""

        self._running = False
        try:
            await self._terminate()
        finally:
            try:
                await self._drain_stdout_task()
            finally:
                self._clear_launch_token()
        return True

    async def _drain_stdout_task(self, timeout_seconds: float = 1.0) -> None:
        task = self._stdout_task
        if task is None or task.done():
            return

        current_loop = asyncio.get_running_loop()
        task_loop = task.get_loop()
        if task_loop is not current_loop:
            deadline = current_loop.time() + timeout_seconds
            while not task.done():
                if current_loop.time() >= deadline:
                    log.warning("%s stdout reader did not finish in time; cancelling it.", self.name)
                    if not task_loop.is_closed():
                        task_loop.call_soon_threadsafe(task.cancel)
                    return
                await asyncio.sleep(0.05)
            return

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        except asyncio.TimeoutError:
            log.warning("%s stdout reader did not finish in time; cancelling it.", self.name)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except Exception as xcp:
            log.warning("%s stdout reader failed: error_type=%s", self.name, type(xcp).__name__)


# Accept the product's conventional capitalisation for direct imports as well.
GMod = Gmod
GMod_Settings = Gmod_Settings
