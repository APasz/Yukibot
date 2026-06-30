from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import logging
import re
import signal
import ssl
from asyncio.events import AbstractEventLoop
from asyncio.locks import Lock
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from re import Pattern
from ssl import SSLContext
from threading import Lock as ThreadLock
from typing import Any, Protocol, Self, TypeVar, cast
from urllib.parse import SplitResult, urlsplit

import aiohttp
import hikari
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from satisfactory_api_client import AsyncSatisfactoryAPI
from satisfactory_api_client.data.advanced_game_settings import AdvancedGameSettings
from satisfactory_api_client.data.minimum_privilege_level import MinimumPrivilegeLevel
from satisfactory_api_client.data.response import Response as SatisfactoryAPIResponse
from satisfactory_api_client.data.server_options import ServerOptions
from satisfactory_api_client.exceptions import APIError

import config
from _discord import DC_Bound, DC_Relay
from _file import File_Utils
from _security import Power_Level
from apps._app import App, AppActivityProvider, AppActivityProviderMetadata
from apps._blueprint_files import (
    AppBlueprintEntry,
    AppBlueprintFileType,
    BlueprintUploadPair,
    blueprint_file_type_from_name,
    describe_blueprint,
    find_matching_blueprint_config_relative_path,
    find_matching_blueprint_module_relative_path,
    list_blueprint_files,
    normalise_blueprint_file_id,
    normalise_existing_blueprint_filename,
    resolve_blueprint_file_path,
    resolve_blueprint_upload_target,
    validate_blueprint_session_name,
    validate_blueprint_upload_pair,
)
from apps._config import App_Config, AppVersion, resolve_config_path
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult, ConsoleResponseSource
from apps._save_files import AppSaveEntry, AppSaveEntryKind, AppSaveRoot, AppSaveRootMode
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Settings_Manager,
    StringSettingSpec,
)
from apps._tailer import Tailer
from apps._updater import SteamCmd_Update_Manager
from config import Activity_Manager
from relay_notices import PlayerSessionAction, PlayerSessionNotice, RelayNoticeSource, render_notice_text

log: Logger = logging.getLogger(__name__)

_DEFAULT_API_PORT: int = 7777
_API_READY_RETRIES: int = 15
_API_READY_SLEEP_SECONDS: float = 2.0
_PLAYER_POLL_SECONDS: float = 15.0
_STOP_WAIT_SECONDS: float = 15.0
_NETWORK_QUALITY_CHOICES: tuple[ChoiceOption, ...] = (
    ChoiceOption("0", "Low"),
    ChoiceOption("1", "Medium"),
    ChoiceOption("2", "High"),
    ChoiceOption("3", "Ultra"),
)
_NETWORK_QUALITY_CHOICE_SPEC: ChoiceSpec = ChoiceSpec(*_NETWORK_QUALITY_CHOICES)
_NETWORK_QUALITY_VALUES: frozenset[str] = frozenset(opt.value for opt in _NETWORK_QUALITY_CHOICES)
_SATISFACTORY_SCHEMATIC_NAMES: Mapping[str, str] = {
    "Schematic_1-1": "Base Building",
    "Schematic_1-2": "Logistics",
    "Schematic_1-3": "Field Research",
    "Schematic_2-1": "Part Assembly",
    "Schematic_2-2": "Obstacle Clearing",
    "Schematic_2-3": "Jump Pads",
    "Schematic_2-5": "Resource Sink Bonus Program",
    "Schematic_3-1": "Coal Power",
    "Schematic_3-2": "Logistics Mk.2",
    "Schematic_3-3": "Vehicular Transport",
    "Schematic_3-4": "Basic Steel Production",
    "Schematic_4-1": "Advanced Steel Production",
    "Schematic_4-2": "Enhanced Asset Security",
    "Schematic_4-3": "Expanded Power Infrastructure",
    "Schematic_4-4": "Hypertubes",
    "Schematic_4-5": "FICSIT Blueprints",
    "Schematic_5-1": "Oil Processing",
    "Schematic_5-2": "Industrial Manufacturing",
    "Schematic_5-3": "Logistics Mk.3",
    "Schematic_5-4": "Fluid Packaging",
    "Schematic_5-5": "Petroleum Power",
    "Schematic_6-1": "Logistics Mk.4",
    "Schematic_6-2": "Jetpack",
    "Schematic_6-3": "Monorail Train Technology",
    "Schematic_6-5": "Pipeline Engineering Mk.2",
    "Schematic_6-6": "FICSIT Blueprints Mk.2",
    "Schematic_6-7": "Railway Signalling",
    "Schematic_7-1": "Bauxite Refinement",
    "Schematic_7-2": "Logistics Mk.5",
    "Schematic_7-3": "Hazmat Suit",
    "Schematic_7-4": "Aeronautical Engineering",
    "Schematic_7-5": "Control System Development",
    "Schematic_8-1": "Nuclear Power",
    "Schematic_8-2": "Advanced Aluminum Production",
    "Schematic_8-3": "Hoverpack",
    "Schematic_8-4": "Leading-edge Production",
    "Schematic_8-5": "Particle Enrichment",
    "Schematic_9-1": "Matter Conversion",
    "Schematic_9-2": "Quantum Encoding",
    "Schematic_9-3": "FICSIT Blueprints Mk.3",
    "Schematic_9-4": "Spatial Energy Regulation",
    "Schematic_9-5": "Peak Efficiency",
}
_SATISFACTORY_BUILD_RE: Pattern[str] = re.compile(
    r"LogInit:\s+Build:\s+\+\+FactoryGame\+rel-main-(?P<version>\d+\.\d+\.\d+)-CL-(?P<build>\d+)",
    re.IGNORECASE,
)
_SATISFACTORY_LOGIN_RE: Pattern[str] = re.compile(
    r"LogNet:\s+Login request: .*?\?Name=(?P<player>[^?\s]+)\s+userId:\s+(?P<identity>.+?)\s+platform:",
    re.IGNORECASE,
)
_SATISFACTORY_JOIN_SUCCEEDED_RE: Pattern[str] = re.compile(
    r"LogNet:\s+Join succeeded:\s+(?P<player>\S+)",
    re.IGNORECASE,
)
_SATISFACTORY_CONNECTION_CLOSE_RE: Pattern[str] = re.compile(
    r"LogNet:\s+UNetConnection::Close:\s+.*?UniqueId:\s+(?P<identity>.+?)(?:,\s+Channels:|$)",
    re.IGNORECASE,
)
_SATISFACTORY_CONNECTION_REMOVED_RE: Pattern[str] = re.compile(
    r"LogNet:\s+UNetDriver::RemoveClientConnection\s+-\s+Removed address .*?UniqueId:\s+(?P<identity>.+)$",
    re.IGNORECASE,
)
_SATISFACTORY_FOREIGN_ID_RE: Pattern[str] = re.compile(r"RepData=\[(?P<foreign_id>[^\]]+)\]", re.IGNORECASE)
_SATISFACTORY_SAVE_DATETIME_RE: Pattern[str] = re.compile(
    r"^(?P<year>\d{4})\.(?P<month>\d{2})\.(?P<day>\d{2})-"
    r"(?P<hour>\d{2})\.(?P<minute>\d{2})\.(?P<second>\d{2})$"
)
_SML_UPLUGIN_FILES: tuple[Path, Path] = (
    Path("FactoryGame") / "Mods" / "SML" / "SML.uplugin",
    Path("Mods") / "SML" / "SML.uplugin",
)
_BLUEPRINT_ROOT: Path = Path("~/.config/Epic/FactoryGame/Saved/SaveGames/blueprints").expanduser()
_SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME = "Shared"
_SATISFACTORY_BLUEPRINT_STORAGE_SUFFIX = "-shared"
_SATISFACTORY_SAVE_ROOT_ID = "saves"
_SATISFACTORY_SAVE_ROOT_LABEL = "Server Saves"


class SatisfactoryNetworkQuality(enum.IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    ULTRA = 3


@dataclass(frozen=True, slots=True)
class SatisfactoryPlayerIdentity:
    player_name: str
    session_key: str


def _normalise_satisfactory_player_name(raw: str) -> str:
    player_name: str = raw.strip()
    if not player_name:
        raise ValueError("Satisfactory player name must not be empty.")
    return player_name


def _satisfactory_player_key(player_name: str) -> str:
    return _normalise_satisfactory_player_name(player_name).casefold()


def _satisfactory_session_key(identity_text: str) -> str:
    raw_identity: str = identity_text.strip()
    if not raw_identity:
        raise ValueError("Satisfactory player identity must not be empty.")
    if foreign_id_match := _SATISFACTORY_FOREIGN_ID_RE.search(raw_identity):
        foreign_id: str = foreign_id_match.group("foreign_id").strip()
        if foreign_id:
            return foreign_id.casefold()
    return raw_identity.casefold()


def _validated_blueprint_session_name_or_none(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not raw.strip():
        return None
    try:
        return validate_blueprint_session_name(raw)
    except ValueError:
        log.warning("Ignoring invalid Satisfactory blueprint session name: %r", raw)
        return None


def _candidate_satisfactory_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates: list[Path | None] = [
        server_log,
        directory / "FactoryGame" / "Saved" / "Logs" / "FactoryGame.log",
    ]
    log_dir: Path = directory / "FactoryGame" / "Saved" / "Logs"
    if log_dir.is_dir():
        backups: list[Path] = sorted(log_dir.glob("FactoryGame-backup-*.log"), reverse=True)
        candidates.extend(backups[:3])

    existing: list[Path] = []
    seen: set[Path] = set[Path]()
    for pointer in candidates:
        if pointer is None or pointer in seen or not pointer.exists():
            continue
        seen.add(pointer)
        existing.append(pointer)
    return tuple[Path, ...](existing)


def _app_version_from_satisfactory_build_match(
    match: re.Match[str],
    *,
    framework: str | None = None,
    loader: str | None = None,
    current: AppVersion | None = None,
) -> AppVersion:
    return AppVersion(
        main=match.group("version").strip(),
        build=int(match.group("build")),
        framework=framework if framework is not None else current.framework if current is not None else None,
        loader=loader if loader is not None else current.loader if current is not None else None,
    )


def detect_satisfactory_version(*, directory: Path, server_log: Path | None) -> AppVersion | None:
    game_version: str | None = None
    game_build: int | None = None
    for pointer in _candidate_satisfactory_logs(directory=directory, server_log=server_log):
        try:
            for line in pointer.read_text(config.STR_ENCODE, errors="ignore").splitlines():
                if match := _SATISFACTORY_BUILD_RE.search(line):
                    game_version = match.group("version").strip()
                    game_build = int(match.group("build"))
                    break
        except OSError as xcp:
            log.warning("Failed to inspect Satisfactory log %s: %s", pointer, xcp)
        if game_version is not None:
            break
    if game_version is None:
        return None

    for relative_pointer in _SML_UPLUGIN_FILES:
        pointer: Path = directory / relative_pointer
        if not pointer.exists():
            continue
        try:
            payload = json.loads(pointer.read_text(config.STR_ENCODE))
        except (OSError, json.JSONDecodeError) as xcp:
            log.warning("Failed to inspect SML plugin manifest %s: %s", pointer, xcp)
            continue
        if not isinstance(payload, dict):
            raise ValueError(f"SML plugin manifest must be a JSON object: {pointer}")
        framework_raw: object | None = payload.get("VersionName") or payload.get("SemVersion")
        framework: str | None = str(framework_raw).strip() if framework_raw is not None else None
        return AppVersion(main=game_version, build=game_build, framework=framework or None, loader="sml")

    return AppVersion(main=game_version, build=game_build)


def _normalise_api_address(raw: str) -> str:
    normalised_address: str = raw.strip().rstrip("/")
    if normalised_address.startswith("https://"):
        normalised_address = normalised_address.removeprefix("https://")
    elif normalised_address.startswith("http://"):
        normalised_address = normalised_address.removeprefix("http://")

    if normalised_address.startswith("["):
        has_port: bool = "]:" in normalised_address
    else:
        has_port = normalised_address.count(":") == 1

    if not has_port:
        normalised_address = f"{normalised_address}:{_DEFAULT_API_PORT}"
    return normalised_address


def _parse_api_endpoint(address: str) -> tuple[str, int]:
    split: SplitResult = urlsplit(f"https://{_normalise_api_address(address)}")
    host: str | None = split.hostname
    try:
        port: int | None = split.port
    except ValueError as xcp:
        raise ValueError("address must include a valid numeric port") from xcp
    if host is None or port is None:
        raise ValueError("address must include a host and port")
    return (host, port)


def _coerce_integral_value(raw: object, *, label: str) -> int:
    if raw is None:
        raise ValueError(f"{label} must not be empty")
    if isinstance(raw, bool):
        raise ValueError(f"{label} must not be boolean")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise ValueError(f"{label} must be an integer value")

    text: str = str(raw).strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    try:
        numeric: float = float(text)
    except ValueError as xcp:
        raise ValueError(f"{label} must be numeric") from xcp
    if not numeric.is_integer():
        raise ValueError(f"{label} must be an integer value")
    return int(numeric)


def _string_object_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        result[key] = item
    return result


def _is_non_empty_text(value: str) -> bool:
    return bool(value.strip())


def _satisfactory_api_response_text(payload: object) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        stripped_payload: str = payload.strip()
        return stripped_payload or None
    if isinstance(payload, Mapping):
        return json.dumps(dict(payload), ensure_ascii=True, sort_keys=True)
    if isinstance(payload, list):
        return json.dumps(payload, ensure_ascii=True)
    if isinstance(payload, tuple):
        return json.dumps(list(payload), ensure_ascii=True)
    return str(payload)


class SatisfactoryBlueprintOwnershipEntry(BaseModel):
    uploaded_by_user_id: int


class SatisfactoryBlueprintOwnershipIndex(BaseModel):
    version: int = 1
    files: dict[str, SatisfactoryBlueprintOwnershipEntry] = Field(default_factory=dict)


class SatisfactoryBlueprintOwnershipStore:
    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._lock: ThreadLock = ThreadLock()

    def uploaded_by_user_id_by_relative_path(self) -> dict[str, int]:
        with self._lock:
            index = self._load_index()
            return {
                relative_path: entry.uploaded_by_user_id
                for relative_path, entry in index.files.items()
            }

    def record_upload(self, *, relative_path: str, actor_user_id: int) -> None:
        self.record_upload_batch(relative_paths=(relative_path,), actor_user_id=actor_user_id)

    def record_upload_batch(self, *, relative_paths: Sequence[str], actor_user_id: int) -> None:
        if not relative_paths:
            return
        with self._lock:
            index = self._load_index()
            ownership_entry = SatisfactoryBlueprintOwnershipEntry(uploaded_by_user_id=actor_user_id)
            for relative_path in relative_paths:
                index.files[relative_path] = ownership_entry
            self._save_index(index)

    def clear(self, *, relative_path: str) -> None:
        with self._lock:
            index = self._load_index()
            if relative_path in index.files:
                index.files.pop(relative_path)
                self._save_index(index)

    def replace_all(self, *, uploaded_by_user_id_by_relative_path: Mapping[str, int]) -> None:
        with self._lock:
            self._save_index(
                SatisfactoryBlueprintOwnershipIndex(
                    files={
                        relative_path: SatisfactoryBlueprintOwnershipEntry(uploaded_by_user_id=actor_user_id)
                        for relative_path, actor_user_id in uploaded_by_user_id_by_relative_path.items()
                    }
                )
            )

    def migrate_legacy_relative_paths(self, *, legacy_to_shared_relative_path: Mapping[str, str]) -> None:
        if not legacy_to_shared_relative_path:
            return
        with self._lock:
            index = self._load_index()
            files = dict(index.files)
            for legacy_relative_path, shared_relative_path in legacy_to_shared_relative_path.items():
                legacy_owner = files.pop(legacy_relative_path, None)
                if legacy_owner is None:
                    continue
                shared_owner = files.get(shared_relative_path)
                if shared_owner is None:
                    files[shared_relative_path] = legacy_owner
                    continue
                if shared_owner.uploaded_by_user_id != legacy_owner.uploaded_by_user_id:
                    raise ValueError(
                        f"Conflicting shared blueprint ownership for {shared_relative_path}: "
                        f"{shared_owner.uploaded_by_user_id} != {legacy_owner.uploaded_by_user_id}"
                    )
            self._save_index(SatisfactoryBlueprintOwnershipIndex(files=files))

    def _load_index(self) -> SatisfactoryBlueprintOwnershipIndex:
        if not self._path.exists():
            return SatisfactoryBlueprintOwnershipIndex()
        try:
            payload = json.loads(self._path.read_text(config.STR_ENCODE))
            return SatisfactoryBlueprintOwnershipIndex.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as xcp:
            log.warning("Resetting invalid Satisfactory blueprint ownership index %s: %s", self._path, xcp)
            return SatisfactoryBlueprintOwnershipIndex()

    def _save_index(self, index: SatisfactoryBlueprintOwnershipIndex) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(f"{self._path.name}.tmp")
        try:
            temp_path.write_text(
                json.dumps(index.model_dump(mode="json"), indent=4, sort_keys=True),
                config.STR_ENCODE,
            )
            temp_path.replace(self._path)
        except Exception:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise


class Satisfactory_Config(App_Config):
    api_host: str | None = "127.0.0.1"
    api_token: str | None = None
    admin_password: str
    verify_ssl_chain_path: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_address(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload: dict[str, object] = _string_object_mapping(raw, label="Satisfactory config")
        address: object | None = payload.get("address")
        if address is None:
            return payload
        if "api_host" in payload or "api_port" in payload:
            return payload
        if not isinstance(address, str):
            raise TypeError("address must be a string")
        host, port = _parse_api_endpoint(address)
        payload["api_host"] = host
        payload["api_port"] = port
        return payload

    @field_validator("api_token", mode="before")
    def blank_string_to_none(cls, raw: object) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    @field_validator("admin_password", mode="before")
    def validate_admin_password(cls, raw: object) -> str:
        if raw is None:
            raise ValueError("admin_password must not be empty")
        text: str = str(raw).strip()
        if not text:
            raise ValueError("admin_password must not be empty")
        return text

    @field_validator("verify_ssl_chain_path", mode="before")
    def resolve_verify_ssl_chain_path(cls, raw: str | Path | None, info) -> Path | None:
        return resolve_config_path(raw, directory=info.data.get("directory", ""))


class SatisfactoryServerState(BaseModel):
    active_session_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("activeSessionName", "ActiveSessionName"),
    )
    auto_load_session_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("autoLoadSessionName", "AutoLoadSessionName"),
    )
    num_connected_players: int | None = Field(
        default=None,
        validation_alias=AliasChoices("numConnectedPlayers", "NumConnectedPlayers"),
    )
    player_limit: int | None = Field(
        default=None,
        validation_alias=AliasChoices("playerLimit", "PlayerLimit"),
    )
    tech_tier: int | None = Field(
        default=None,
        validation_alias=AliasChoices("techTier", "TechTier"),
    )
    active_schematic: str | None = Field(
        default=None,
        validation_alias=AliasChoices("activeSchematic", "ActiveSchematic"),
    )
    is_game_running: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("isGameRunning", "IsGameRunning"),
    )
    total_game_duration: int | None = Field(
        default=None,
        validation_alias=AliasChoices("totalGameDuration", "TotalGameDuration"),
    )
    average_tick_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("averageTickRate", "AverageTickRate"),
    )

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("tech_tier", mode="before")
    def normalise_tech_tier(cls, raw: object) -> int | None:
        if raw is None:
            return None
        value: int = _coerce_integral_value(raw, label="tech tier")
        if value < 0:
            raise ValueError("tech tier must not be negative")
        return value

    @field_validator("total_game_duration", mode="before")
    def normalise_total_game_duration(cls, raw: object) -> int | None:
        if raw is None:
            return None
        value: int = _coerce_integral_value(raw, label="total game duration")
        if value < 0:
            raise ValueError("total game duration must not be negative")
        return value

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, object]) -> Self:
        state_payload: object | None = payload.get("serverGameState")
        if state_payload is None:
            state_payload = payload.get("ServerGameState")
        if not isinstance(state_payload, Mapping):
            raise ValueError("server game state payload must be a mapping")
        return cls.model_validate(_string_object_mapping(state_payload, label="server game state payload"))


class SatisfactoryServerOptionsSnapshot(BaseModel):
    auto_pause: bool | None = Field(default=None, validation_alias=AliasChoices("FG.DSAutoPause", "DSAutoPause"))
    auto_save_on_disconnect: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.DSAutoSaveOnDisconnect", "DSAutoSaveOnDisconnect"),
    )
    autosave_interval_seconds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.AutosaveInterval", "AutosaveInterval"),
    )
    send_gameplay_data: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.SendGameplayData", "SendGameplayData"),
    )
    network_quality: SatisfactoryNetworkQuality | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.NetworkQuality", "NetworkQuality"),
    )

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("autosave_interval_seconds", mode="before")
    def normalise_autosave_interval_seconds(cls, raw: object) -> int | None:
        if raw is None:
            return None
        value: int = _coerce_integral_value(raw, label="autosave interval")
        if value < 0:
            raise ValueError("autosave interval must not be negative")
        return value

    @field_validator("network_quality", mode="before")
    def normalise_network_quality(cls, raw: object) -> SatisfactoryNetworkQuality | None:
        if raw is None:
            return None
        quality: int = _coerce_integral_value(raw, label="network quality")
        return SatisfactoryNetworkQuality(quality)

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, object]) -> Self:
        server_options = payload.get("serverOptions")
        if server_options is None:
            server_options = payload.get("ServerOptions")
        pending_options = payload.get("pendingServerOptions")
        if pending_options is None:
            pending_options = payload.get("PendingServerOptions")
        if server_options is not None and not isinstance(server_options, Mapping):
            raise ValueError("server options payload must be a mapping")
        if pending_options is not None and not isinstance(pending_options, Mapping):
            raise ValueError("pending server options payload must be a mapping")
        effective_options = (
            dict(_string_object_mapping(server_options, label="server options payload"))
            if isinstance(server_options, Mapping)
            else {}
        )
        if isinstance(pending_options, Mapping):
            effective_options.update(_string_object_mapping(pending_options, label="pending server options payload"))
        return cls.model_validate(effective_options)

    def to_sdk_server_options(self) -> ServerOptions:
        return ServerOptions(
            DSAutoPause=self.auto_pause,
            DSAutoSaveOnDisconnect=self.auto_save_on_disconnect,
            AutosaveInterval=(
                None if self.autosave_interval_seconds is None else float(self.autosave_interval_seconds)
            ),
            SendGameplayData=self.send_gameplay_data,
            NetworkQuality=None if self.network_quality is None else self.network_quality.value,
        )


class SatisfactoryAdvancedGameSettingsSnapshot(BaseModel):
    creative_mode_enabled: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("creativeModeEnabled", "CreativeModeEnabled"),
    )
    no_power: bool | None = Field(default=None, validation_alias=AliasChoices("FG.NoPower", "NoPower"))
    disable_arachnid_creatures: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.DisableArachnidCreatures", "DisableArachnidCreatures"),
    )
    no_unlock_cost: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.NoUnlockCost", "NoUnlockCost"),
    )
    set_game_phase: int | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.SetGamePhase", "SetGamePhase"),
    )
    give_all_tiers: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.GiveAllTiers", "GiveAllTiers"),
    )
    unlock_all_research_schematics: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.UnlockAllResearchSchematics", "UnlockAllResearchSchematics"),
    )
    unlock_instant_alt_recipes: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.UnlockInstantAltRecipes", "UnlockInstantAltRecipes"),
    )
    unlock_all_resource_sink_schematics: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "FG.UnlockAllResourceSinkSchematics",
            "UnlockAllResourceSinkSchematics",
        ),
    )
    give_items: str | None = Field(default=None, validation_alias=AliasChoices("FG.GiveItems", "GiveItems"))
    no_build_cost: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("FG.NoBuildCost", "NoBuildCost"),
    )
    god_mode: bool | None = Field(default=None, validation_alias=AliasChoices("FG.GodMode", "GodMode"))
    flight_mode: bool | None = Field(default=None, validation_alias=AliasChoices("FG.FlightMode", "FlightMode"))

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("set_game_phase", mode="before")
    def normalise_set_game_phase(cls, raw: object) -> int | None:
        if raw is None:
            return None
        value: int = _coerce_integral_value(raw, label="advanced game phase")
        if value < 0:
            raise ValueError("advanced game phase must not be negative")
        return value

    @field_validator("give_items", mode="before")
    def normalise_give_items(cls, raw: object) -> str | None:
        if raw is None:
            return None
        text: str = str(raw).strip()
        return text or None

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, object]) -> Self:
        advanced_game_settings = payload.get("advancedGameSettings")
        if advanced_game_settings is None:
            advanced_game_settings = payload.get("AdvancedGameSettings")
        if advanced_game_settings is not None and not isinstance(advanced_game_settings, Mapping):
            raise ValueError("advanced game settings payload must be a mapping")
        effective_options = (
            dict(_string_object_mapping(advanced_game_settings, label="advanced game settings payload"))
            if isinstance(advanced_game_settings, Mapping)
            else {}
        )
        creative_mode_enabled: object | None = payload.get("creativeModeEnabled")
        if creative_mode_enabled is None:
            creative_mode_enabled = payload.get("CreativeModeEnabled")
        effective_options["creative_mode_enabled"] = creative_mode_enabled
        return cls.model_validate(effective_options)

    def to_sdk_advanced_game_settings(self) -> AdvancedGameSettings:
        return AdvancedGameSettings(
            NoPower=self.no_power,
            DisableArachnidCreatures=self.disable_arachnid_creatures,
            NoUnlockCost=self.no_unlock_cost,
            SetGamePhase=self.set_game_phase,
            GiveAllTiers=self.give_all_tiers,
            UnlockAllResearchSchematics=self.unlock_all_research_schematics,
            UnlockInstantAltRecipes=self.unlock_instant_alt_recipes,
            UnlockAllResourceSinkSchematics=self.unlock_all_resource_sink_schematics,
            GiveItems=self.give_items,
            NoBuildCost=self.no_build_cost,
            GodMode=self.god_mode,
            FlightMode=self.flight_mode,
        )


class SatisfactorySettingsSnapshot(BaseModel):
    auto_load_session_name: str | None = None
    server_options: SatisfactoryServerOptionsSnapshot = Field(default_factory=SatisfactoryServerOptionsSnapshot)
    advanced_game_settings: SatisfactoryAdvancedGameSettingsSnapshot = Field(
        default_factory=SatisfactoryAdvancedGameSettingsSnapshot
    )

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_flat_payload(cls, raw: object) -> object:
        if not isinstance(raw, Mapping):
            return raw
        payload = _string_object_mapping(raw, label="satisfactory settings snapshot")
        if "server_options" in payload or "advanced_game_settings" in payload:
            return payload

        server_options_keys = (
            "auto_pause",
            "FG.DSAutoPause",
            "auto_save_on_disconnect",
            "FG.DSAutoSaveOnDisconnect",
            "autosave_interval_seconds",
            "FG.AutosaveInterval",
            "send_gameplay_data",
            "FG.SendGameplayData",
            "network_quality",
            "FG.NetworkQuality",
        )
        advanced_game_settings_keys = (
            "creative_mode_enabled",
            "CreativeModeEnabled",
            "creativeModeEnabled",
            "no_power",
            "FG.NoPower",
            "disable_arachnid_creatures",
            "FG.DisableArachnidCreatures",
            "no_unlock_cost",
            "FG.NoUnlockCost",
            "set_game_phase",
            "FG.SetGamePhase",
            "give_all_tiers",
            "FG.GiveAllTiers",
            "unlock_all_research_schematics",
            "FG.UnlockAllResearchSchematics",
            "unlock_instant_alt_recipes",
            "FG.UnlockInstantAltRecipes",
            "unlock_all_resource_sink_schematics",
            "FG.UnlockAllResourceSinkSchematics",
            "give_items",
            "FG.GiveItems",
            "no_build_cost",
            "FG.NoBuildCost",
            "god_mode",
            "FG.GodMode",
            "flight_mode",
            "FG.FlightMode",
        )

        server_options = {key: payload[key] for key in server_options_keys if key in payload}
        advanced_game_settings = {key: payload[key] for key in advanced_game_settings_keys if key in payload}

        if not server_options and not advanced_game_settings:
            return payload

        migrated_payload: dict[str, object] = {
            key: value
            for key, value in payload.items()
            if key not in {*server_options_keys, *advanced_game_settings_keys}
        }
        migrated_payload["server_options"] = server_options
        migrated_payload["advanced_game_settings"] = advanced_game_settings
        return migrated_payload

    @classmethod
    def from_api_payloads(
        cls,
        state: SatisfactoryServerState,
        server_options_payload: Mapping[str, object],
        advanced_game_settings_payload: Mapping[str, object],
    ) -> Self:
        return cls(
            auto_load_session_name=state.auto_load_session_name,
            server_options=SatisfactoryServerOptionsSnapshot.from_api_payload(server_options_payload),
            advanced_game_settings=SatisfactoryAdvancedGameSettingsSnapshot.from_api_payload(
                advanced_game_settings_payload
            ),
        )


class SatisfactorySaveHeader(BaseModel):
    save_name: str = Field(validation_alias=AliasChoices("saveName", "SaveName"))
    session_name: str = Field(validation_alias=AliasChoices("sessionName", "SessionName"))
    save_date_time: datetime = Field(validation_alias=AliasChoices("saveDateTime", "SaveDateTime"))

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("save_name", "session_name", mode="before")
    def normalise_required_text(cls, raw: object) -> str:
        text: str = str(raw).strip()
        if not text:
            raise ValueError("save metadata text field must not be empty")
        return text

    @field_validator("save_date_time", mode="before")
    def normalise_save_date_time(cls, raw: object) -> datetime | object:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)

        text: str = str(raw).strip()
        if not text:
            raise ValueError("save date time must not be empty")

        match: re.Match[str] | None = _SATISFACTORY_SAVE_DATETIME_RE.fullmatch(text)
        if match is None:
            return raw

        try:
            return datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
                int(match.group("second")),
                tzinfo=timezone.utc,
            )
        except ValueError as xcp:
            raise ValueError("save date time must be a valid Satisfactory timestamp") from xcp

    def to_app_save_entry(self) -> AppSaveEntry:
        relative_path = f"{self.session_name}/{self.save_name}"
        return AppSaveEntry(
            id=f"{_SATISFACTORY_SAVE_ROOT_ID}/{relative_path}",
            label=self.save_name,
            relative_path=relative_path,
            root_id=_SATISFACTORY_SAVE_ROOT_ID,
            root_label=_SATISFACTORY_SAVE_ROOT_LABEL,
            kind=AppSaveEntryKind.FILE,
            size_bytes=0,
            modified_at=self.save_date_time,
        )


class SatisfactorySessionSaveSnapshot(BaseModel):
    session_name: str = Field(validation_alias=AliasChoices("sessionName", "SessionName"))
    save_headers: tuple[SatisfactorySaveHeader, ...] = Field(
        validation_alias=AliasChoices("saveHeaders", "SaveHeaders")
    )

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("session_name", mode="before")
    def normalise_session_name(cls, raw: object) -> str:
        text: str = str(raw).strip()
        if not text:
            raise ValueError("session name must not be empty")
        return text

    @field_validator("save_headers", mode="before")
    def normalise_save_headers(
        cls, raw: object
    ) -> tuple[Mapping[str, object] | SatisfactorySaveHeader, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise TypeError("save headers payload must be a sequence")
        headers: list[Mapping[str, object] | SatisfactorySaveHeader] = []
        for item in cast(Sequence[object], raw):
            if isinstance(item, SatisfactorySaveHeader):
                headers.append(item)
                continue
            if not isinstance(item, Mapping):
                raise TypeError("save header payload must be a mapping")
            headers.append(_string_object_mapping(item, label="save header payload"))
        return tuple(headers)


class SatisfactorySessionEnumerationSnapshot(BaseModel):
    sessions: tuple[SatisfactorySessionSaveSnapshot, ...]
    current_session_index: int | None = Field(
        default=None,
        validation_alias=AliasChoices("currentSessionIndex", "CurrentSessionIndex"),
    )

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("sessions", mode="before")
    def normalise_sessions(
        cls, raw: object
    ) -> tuple[Mapping[str, object] | SatisfactorySessionSaveSnapshot, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise TypeError("sessions payload must be a sequence")
        sessions: list[Mapping[str, object] | SatisfactorySessionSaveSnapshot] = []
        for item in cast(Sequence[object], raw):
            if isinstance(item, SatisfactorySessionSaveSnapshot):
                sessions.append(item)
                continue
            if not isinstance(item, Mapping):
                raise TypeError("session payload must be a mapping")
            sessions.append(_string_object_mapping(item, label="session payload"))
        return tuple(sessions)

    @field_validator("current_session_index", mode="before")
    def normalise_current_session_index(cls, raw: object) -> int | None:
        if raw is None:
            return None
        value: int = _coerce_integral_value(raw, label="current session index")
        if value < 0:
            raise ValueError("current session index must not be negative")
        return value

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls.model_validate(_string_object_mapping(payload, label="enumerate sessions payload"))

    def save_entries(self) -> tuple[AppSaveEntry, ...]:
        entries: list[AppSaveEntry] = []
        for session in self.sessions:
            entries.extend(save_header.to_app_save_entry() for save_header in session.save_headers)
        return tuple(sorted(entries, key=lambda entry: entry.relative_path.casefold()))

    def save_header_by_id(self) -> dict[str, SatisfactorySaveHeader]:
        return {
            save_header.to_app_save_entry().id: save_header
            for session in self.sessions
            for save_header in session.save_headers
        }


@dataclass(frozen=True, slots=True)
class SatisfactoryBridgeConfig:
    host: str
    port: int
    token: str | None = None
    password: str | None = None
    verify_ssl_chain_path: Path | None = None


class SatisfactoryAPIClient(AsyncSatisfactoryAPI):
    def use_verify_ssl_chain(self, chain_path: Path) -> None:
        ssl_context: SSLContext = ssl.create_default_context()
        ssl_context.load_verify_locations(str(chain_path))
        self.cert_path = str(chain_path)
        self._ssl_context = ssl_context

    async def upload_save_game_file(
        self,
        *,
        save_name: str,
        source_path: Path,
        load_save_game: bool = False,
        enable_advanced_game_settings: bool = False,
    ) -> SatisfactoryAPIResponse:
        url = f"https://{self.host}:{self.port}/api/v1"
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        envelope = {
            "function": "UploadSaveGame",
            "data": {
                "SaveName": save_name,
                "LoadSaveGame": load_save_game,
                "EnableAdvancedGameSettings": enable_advanced_game_settings,
            },
        }
        form = aiohttp.FormData()
        form.add_field("_charset_", "utf-8")
        form.add_field("data", json.dumps(envelope), content_type="application/json")
        form.add_field(
            "saveGameFile",
            source_path.read_bytes(),
            filename=source_path.name,
            content_type="application/octet-stream",
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form, headers=headers, ssl=self._get_ssl()) as response:
                if response.status not in (200, 201, 202, 204):
                    error_data = await response.json(content_type=None)
                    raise APIError(
                        error_code=str(error_data.get("errorCode", "upload_failed")),
                        message=str(error_data.get("errorMessage", "Save upload failed")),
                    )
                if response.status == 204:
                    return SatisfactoryAPIResponse(success=True, data={})

                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    result = await response.json(content_type=None)
                    if result.get("errorCode"):
                        raise APIError(
                            error_code=str(result.get("errorCode")),
                            message=str(result.get("errorMessage", "Save upload failed")),
                        )
                    return SatisfactoryAPIResponse(success=True, data=result.get("data"))
                if content_type == "application/octet-stream":
                    return SatisfactoryAPIResponse(success=True, data=await response.read())
                return SatisfactoryAPIResponse(success=True, data=await response.read())


class SatisfactoryBridge:
    def __init__(self, cfg: SatisfactoryBridgeConfig) -> None:
        self._cfg: SatisfactoryBridgeConfig = cfg
        self._api: SatisfactoryAPIClient | None = None
        self._lock: Lock = asyncio.Lock()

    async def _build_api(self) -> SatisfactoryAPIClient:
        if self._cfg.token is None:
            api: SatisfactoryAPIClient = SatisfactoryAPIClient(
                host=self._cfg.host,
                port=self._cfg.port,
            )
        else:
            api = SatisfactoryAPIClient(
                host=self._cfg.host,
                port=self._cfg.port,
                auth_token=self._cfg.token,
            )
        if self._cfg.verify_ssl_chain_path is not None:
            api.use_verify_ssl_chain(self._cfg.verify_ssl_chain_path)
        if self._cfg.token is not None:
            await api.verify_authentication_token()
        elif self._cfg.password is not None:
            await api.password_login(MinimumPrivilegeLevel.ADMINISTRATOR, self._cfg.password)
        return api

    async def _client(self) -> SatisfactoryAPIClient:
        async with self._lock:
            if self._api is None:
                self._api = await self._build_api()
            return self._api

    async def _call(self, method_name: str, *args: object) -> SatisfactoryAPIResponse:
        method = getattr(await self._client(), method_name)
        response = await method(*args)
        if not isinstance(response, SatisfactoryAPIResponse):
            raise TypeError(f"{method_name} returned {type(response).__name__}, expected Response")
        return response

    async def query_server_state(self) -> SatisfactoryServerState:
        payload: object = (await self._call("query_server_state")).data
        return SatisfactoryServerState.from_api_payload(
            _string_object_mapping(payload, label="query_server_state payload")
        )

    async def read_server_options(self) -> SatisfactoryServerOptionsSnapshot:
        payload: object = (await self._call("get_server_options")).data
        return SatisfactoryServerOptionsSnapshot.from_api_payload(
            _string_object_mapping(payload, label="get_server_options payload"),
        )

    async def apply_server_options(
        self,
        settings: SatisfactoryServerOptionsSnapshot,
        *,
        auto_load_session_name: str | None = None,
    ) -> None:
        if auto_load_session_name is not None:
            await self._call("set_auto_load_session_name", auto_load_session_name)
        server_options: ServerOptions = settings.to_sdk_server_options()
        if server_options.to_dict():
            await self._call("apply_server_options", server_options)

    async def read_advanced_game_settings(self) -> SatisfactoryAdvancedGameSettingsSnapshot:
        payload: object = (await self._call("get_advanced_game_settings")).data
        return SatisfactoryAdvancedGameSettingsSnapshot.from_api_payload(
            _string_object_mapping(payload, label="get_advanced_game_settings payload")
        )

    async def apply_advanced_game_settings(self, settings: SatisfactoryAdvancedGameSettingsSnapshot) -> None:
        advanced_game_settings: AdvancedGameSettings = settings.to_sdk_advanced_game_settings()
        if advanced_game_settings.to_dict():
            await self._call("apply_advanced_game_settings", advanced_game_settings)

    async def read_settings(self) -> SatisfactorySettingsSnapshot:
        state: SatisfactoryServerState = await self.query_server_state()
        server_options: SatisfactoryServerOptionsSnapshot = await self.read_server_options()
        advanced_game_settings: SatisfactoryAdvancedGameSettingsSnapshot = await self.read_advanced_game_settings()
        return SatisfactorySettingsSnapshot(
            auto_load_session_name=state.auto_load_session_name,
            server_options=server_options,
            advanced_game_settings=advanced_game_settings,
        )

    async def apply_settings(self, settings: SatisfactorySettingsSnapshot) -> None:
        await self.apply_server_options(
            settings.server_options,
            auto_load_session_name=settings.auto_load_session_name,
        )
        await self.apply_advanced_game_settings(settings.advanced_game_settings)

    async def enumerate_sessions(self) -> SatisfactorySessionEnumerationSnapshot:
        payload: object = (await self._call("enumerate_sessions")).data
        return SatisfactorySessionEnumerationSnapshot.from_api_payload(
            _string_object_mapping(payload, label="enumerate_sessions payload")
        )

    async def save_game(self, save_name: str) -> str | None:
        payload: object = (await self._call("save_game", save_name)).data
        return _satisfactory_api_response_text(payload)

    async def delete_save_file(self, save_name: str) -> str | None:
        payload: object = (await self._call("delete_save_file", save_name)).data
        return _satisfactory_api_response_text(payload)

    async def download_save_game(self, save_name: str) -> bytes:
        payload: object = (await self._call("download_save_game", save_name)).data
        if not isinstance(payload, bytes):
            raise TypeError(f"download_save_game returned {type(payload).__name__}, expected bytes")
        return payload

    async def upload_save_game(self, *, save_name: str, source_path: Path) -> str | None:
        payload: object = (
            await (await self._client()).upload_save_game_file(save_name=save_name, source_path=source_path)
        ).data
        return _satisfactory_api_response_text(payload)

    async def shutdown(self) -> str | None:
        payload: object = (await self._call("shutdown")).data
        return _satisfactory_api_response_text(payload)

    async def run_command(self, command: str) -> str | None:
        payload: object = (await self._call("run_command", command)).data
        return _satisfactory_api_response_text(payload)

    def update_password(self, password: str) -> None:
        self._cfg = SatisfactoryBridgeConfig(
            host=self._cfg.host,
            port=self._cfg.port,
            token=self._cfg.token,
            password=password,
            verify_ssl_chain_path=self._cfg.verify_ssl_chain_path,
        )


class SatisfactorySettingsBridge(Protocol):
    async def read_settings(self) -> SatisfactorySettingsSnapshot: ...

    async def apply_settings(self, settings: SatisfactorySettingsSnapshot) -> None: ...


SettingValueT = TypeVar("SettingValueT")
_SATISFACTORY_ADVANCED_GAME_BOOL_SETTINGS: tuple[tuple[str, str, str, str], ...] = (
    ("no_power", "FG.NoPower", "No Power", "Disable all power requirements for the loaded save."),
    (
        "disable_arachnid_creatures",
        "FG.DisableArachnidCreatures",
        "Disable Arachnid Creatures",
        "Disable arachnid creatures for the loaded save.",
    ),
    ("no_unlock_cost", "FG.NoUnlockCost", "No Unlock Cost", "Remove unlock costs for milestones and research."),
    ("give_all_tiers", "FG.GiveAllTiers", "Give All Tiers", "Unlock all HUB tiers for the loaded save."),
    (
        "unlock_all_research_schematics",
        "FG.UnlockAllResearchSchematics",
        "Unlock All Research Schematics",
        "Unlock all MAM research schematics.",
    ),
    (
        "unlock_instant_alt_recipes",
        "FG.UnlockInstantAltRecipes",
        "Unlock Instant Alt Recipes",
        "Unlock alternate recipes immediately.",
    ),
    (
        "unlock_all_resource_sink_schematics",
        "FG.UnlockAllResourceSinkSchematics",
        "Unlock All Resource Sink Schematics",
        "Unlock all AWESOME Shop schematics.",
    ),
    ("no_build_cost", "FG.NoBuildCost", "No Build Cost", "Remove build costs for construction."),
    ("god_mode", "FG.GodMode", "God Mode", "Prevent player damage."),
    ("flight_mode", "FG.FlightMode", "Flight Mode", "Allow free flight for players."),
)


class SatisfactorySettings(App_Settings):
    """Caches desired Satisfactory API settings locally and applies them when the server is reachable."""

    def __init__(
        self,
        pointer: Path,
        bridge: SatisfactorySettingsBridge,
        is_running: Callable[[], bool],
        cfg: Satisfactory_Config,
        instances_path: Path,
        *,
        version_getter: Callable[[], AppVersion | None] | None = None,
    ) -> None:
        self._bridge: SatisfactorySettingsBridge = bridge
        self._is_running: Callable[[], bool] = is_running
        self._cfg: Satisfactory_Config = cfg
        self._instances_path: Path = instances_path
        self._apply_task: asyncio.Task[None] | None = None
        self._creative_mode_enabled: bool | None = None

        self._auto_load_session_name: Setting[str] = Setting[str](
            StringSettingSpec(allow_blank=True),
            "Auto Load Session",
            "auto_load_session_name",
            [],
            default="",
            desc="Session name that should load automatically when the server starts.",
            power_level=Power_Level.sudo,
        )
        self._auto_pause: Setting[bool] = Setting[bool](
            BoolSettingSpec(),
            "Auto Pause",
            "FG.DSAutoPause",
            [],
            default=False,
            desc="Pause the simulation when no players are connected.",
            power_level=Power_Level.sudo,
        )
        self._auto_save_on_disconnect: Setting[bool] = Setting[bool](
            BoolSettingSpec(),
            "Auto Save On Disconnect",
            "FG.DSAutoSaveOnDisconnect",
            [],
            default=True,
            desc="Save automatically when a player disconnects.",
            power_level=Power_Level.sudo,
        )
        self._autosave_interval_seconds: Setting[int] = Setting[int](
            IntSettingSpec(),
            "Autosave Interval (s)",
            "FG.AutosaveInterval",
            [],
            default=300,
            desc="Seconds between automatic saves.",
        )
        self._send_gameplay_data: Setting[bool] = Setting[bool](
            BoolSettingSpec(),
            "Send Gameplay Data",
            "FG.SendGameplayData",
            [],
            default=True,
            desc="Allow gameplay telemetry to be sent.",
            power_level=Power_Level.sudo,
        )
        self._network_quality: Setting[int] = Setting[int](
            IntSettingSpec(
                _NETWORK_QUALITY_CHOICE_SPEC,
                raw_validator=lambda raw: raw in _NETWORK_QUALITY_VALUES,
            ),
            "Network Quality",
            "FG.NetworkQuality",
            [],
            default=SatisfactoryNetworkQuality.MEDIUM.value,
            desc="Higher values can improve network responsiveness at the cost of server performance.",
        )
        self._advanced_game_bool_settings: dict[str, Setting[bool]] = {}
        for field_name, key, label, desc in _SATISFACTORY_ADVANCED_GAME_BOOL_SETTINGS:
            self._advanced_game_bool_settings[field_name] = Setting[bool](
                BoolSettingSpec(),
                label,
                key,
                [],
                default=False,
                desc=desc,
                power_level=Power_Level.sudo,
            )
        self._set_game_phase: Setting[int] = Setting[int](
            IntSettingSpec(min_value=0),
            "Set Game Phase",
            "FG.SetGamePhase",
            [],
            default=0,
            desc="Force the active project assembly phase for the loaded save.",
            power_level=Power_Level.sudo,
        )
        self._give_items: Setting[str] = Setting[str](
            StringSettingSpec(allow_blank=True),
            "Give Items",
            "FG.GiveItems",
            [],
            default="",
            desc="Item grant payload forwarded to Satisfactory advanced game settings.",
            power_level=Power_Level.sudo,
        )
        self._admin_password: Setting[str] = Setting[str](
            StringSettingSpec(
                allow_blank=True,
                is_sensitive=True,
                do_hide=Power_Level.sudo,
            ),
            "Admin Password",
            "admin_password",
            [],
            default="",
            desc="Local API login password stored in the instance config.",
            power_level=Power_Level.sudo,
        )
        super().__init__(
            pointer,
            [
                self._auto_load_session_name,
                self._auto_pause,
                self._auto_save_on_disconnect,
                self._autosave_interval_seconds,
                self._send_gameplay_data,
                self._network_quality,
                *self._advanced_game_bool_settings.values(),
                self._set_game_phase,
                self._give_items,
                self._admin_password,
            ],
            version_getter=version_getter,
        )

    def _snapshot_from_settings(self) -> SatisfactorySettingsSnapshot:
        return SatisfactorySettingsSnapshot(
            auto_load_session_name=self._optional_setting_value(self._auto_load_session_name, str),
            server_options=SatisfactoryServerOptionsSnapshot(
                auto_pause=self._optional_setting_value(self._auto_pause, bool),
                auto_save_on_disconnect=self._optional_setting_value(self._auto_save_on_disconnect, bool),
                autosave_interval_seconds=self._optional_setting_value(self._autosave_interval_seconds, int),
                send_gameplay_data=self._optional_setting_value(self._send_gameplay_data, bool),
                network_quality=(
                    None
                    if (quality := self._optional_setting_value(self._network_quality, int)) is None
                    else SatisfactoryNetworkQuality(quality)
                ),
            ),
            advanced_game_settings=SatisfactoryAdvancedGameSettingsSnapshot.model_validate(
                {
                    "creative_mode_enabled": self._creative_mode_enabled,
                    **{
                        field_name: self._optional_setting_value(setting, bool)
                        for field_name, setting in self._advanced_game_bool_settings.items()
                    },
                    "set_game_phase": self._optional_setting_value(self._set_game_phase, int),
                    "give_items": self._optional_setting_value(self._give_items, str),
                }
            ),
        )

    @staticmethod
    def _optional_setting_value(setting: Setting[Any], expected_type: type[SettingValueT]) -> SettingValueT | None:
        if isinstance(setting.value, hikari.UndefinedType):
            return None
        if expected_type is int and isinstance(setting.value, bool):
            raise TypeError(f"{setting.key} must be {expected_type.__name__}")
        if not isinstance(setting.value, expected_type):
            raise TypeError(f"{setting.key} must be {expected_type.__name__}")
        return setting.value

    @staticmethod
    def _assign_setting(setting: Setting[Any], value: object | None) -> None:
        setting.value = hikari.UNDEFINED if value is None else value

    def _apply_snapshot(self, snapshot: SatisfactorySettingsSnapshot) -> None:
        self._assign_setting(self._auto_load_session_name, snapshot.auto_load_session_name)
        self._assign_setting(self._auto_pause, snapshot.server_options.auto_pause)
        self._assign_setting(self._auto_save_on_disconnect, snapshot.server_options.auto_save_on_disconnect)
        self._assign_setting(self._autosave_interval_seconds, snapshot.server_options.autosave_interval_seconds)
        self._assign_setting(self._send_gameplay_data, snapshot.server_options.send_gameplay_data)
        self._assign_setting(
            self._network_quality,
            None if snapshot.server_options.network_quality is None else snapshot.server_options.network_quality.value,
        )
        for field_name, setting in self._advanced_game_bool_settings.items():
            self._assign_setting(setting, getattr(snapshot.advanced_game_settings, field_name))
        self._assign_setting(self._set_game_phase, snapshot.advanced_game_settings.set_game_phase)
        self._assign_setting(self._give_items, snapshot.advanced_game_settings.give_items)
        self._creative_mode_enabled = snapshot.advanced_game_settings.creative_mode_enabled

    def _apply_local_settings(self) -> None:
        self._assign_setting(self._admin_password, self._cfg.admin_password)

    def _write_snapshot(self, snapshot: SatisfactorySettingsSnapshot) -> dict[str, object]:
        payload = snapshot.model_dump(mode="json", exclude_none=True)
        self.pointer.write_text(json.dumps(payload, indent=4), config.STR_ENCODE)
        return payload

    def load(self) -> None:
        raw = self.pointer.read_text(config.STR_ENCODE).strip()
        if not raw:
            snapshot = SatisfactorySettingsSnapshot()
        else:
            snapshot = SatisfactorySettingsSnapshot.model_validate_json(raw)
        self._apply_snapshot(snapshot)
        self._apply_local_settings()

    def save(self) -> dict[str, object]:
        payload: dict[str, object] = self._write_snapshot(self._snapshot_from_settings())
        self._persist_admin_password()
        if self._is_running():
            self._schedule_apply()
        return payload

    def _persist_admin_password(self) -> None:
        password: str | None = self._optional_setting_value(self._admin_password, str)
        if password is None:
            raise ValueError("admin_password must not be empty")
        if password == self._cfg.admin_password:
            return

        self._cfg.admin_password = password
        if isinstance(self._bridge, SatisfactoryBridge):
            self._bridge.update_password(password)

        raw = _string_object_mapping(
            json.loads(self._instances_path.read_text(config.STR_ENCODE)),
            label=str(self._instances_path),
        )
        instance_payload: object | None = raw.get(self._cfg.instance_key)
        if not isinstance(instance_payload, Mapping):
            raise ValueError(f"{self._instances_path} is missing instance {self._cfg.instance_key!r}")
        next_payload: dict[str, object] = _string_object_mapping(instance_payload, label="instance payload")
        next_payload["admin_password"] = password
        raw[self._cfg.instance_key] = next_payload
        self._instances_path.write_text(json.dumps(raw, indent=4) + "\n", config.STR_ENCODE)

    def _schedule_apply(self) -> None:
        try:
            loop: AbstractEventLoop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("No running event loop; Satisfactory settings will apply on next start.")
            return
        if self._apply_task and not self._apply_task.done():
            self._apply_task.cancel()
        self._apply_task = loop.create_task(self._apply_and_log())

    async def _apply_and_log(self) -> None:
        try:
            await self.apply_current_values()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to apply Satisfactory settings")

    async def refresh_from_server(self) -> SatisfactorySettingsSnapshot:
        snapshot: SatisfactorySettingsSnapshot = await self._call_live_bridge(
            self._bridge.read_settings,
            unavailable_detail=f"{self._cfg.friendly_name} API is unavailable.",
        )
        self._apply_snapshot(snapshot)
        self._write_snapshot(snapshot)
        self._apply_local_settings()
        return snapshot

    async def apply_current_values(self) -> bool:
        if not self._is_running():
            return False
        snapshot: SatisfactorySettingsSnapshot = self._snapshot_from_settings()
        await self._call_live_bridge(
            lambda: self._bridge.apply_settings(snapshot),
            unavailable_detail=f"{self._cfg.friendly_name} API is unavailable.",
        )
        await self.refresh_from_server()
        return True

    async def _call_live_bridge(
        self,
        operation: Callable[[], Awaitable[SettingValueT]],
        *,
        unavailable_detail: str,
    ) -> SettingValueT:
        if not self._is_running():
            raise RuntimeError(f"{self._cfg.friendly_name} is not running.")
        try:
            return await operation()
        except (aiohttp.ClientError, OSError, TimeoutError) as xcp:
            raise RuntimeError(unavailable_detail) from xcp


def _require_satisfactory_app(app_obj: object) -> "Satisfactory":
    return app_obj if isinstance(app_obj, Satisfactory) else cast(Satisfactory, app_obj)


_SATISFACTORY_RAW_COMMAND_PARAMETER: ConsoleActionParameter[str] = ConsoleActionParameter[str](
    key="command",
    label="Command",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Raw Satisfactory server command sent through the HTTPS API.",
    max_length=500,
    multiline=True,
)


async def _console_raw_command(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Satisfactory = _require_satisfactory_app(app_obj)
    command: str = cast(str, value)
    response_text: str | None = await app.run_console_command(command)
    return ConsoleActionResult(
        summary=f"{app.friendly}: API command sent.",
        text=response_text,
        source=ConsoleResponseSource.API,
    )


async def _console_save_game(app_obj: object, value: object | None) -> ConsoleActionResult:
    del value
    app: Satisfactory = _require_satisfactory_app(app_obj)
    save_name, response_text = await app.request_manual_save()
    return ConsoleActionResult(
        summary=f"{app.friendly}: save requested as `{save_name}`.",
        text=response_text,
        source=ConsoleResponseSource.API,
    )


async def _console_shutdown(app_obj: object, value: object | None) -> ConsoleActionResult:
    del value
    app: Satisfactory = _require_satisfactory_app(app_obj)
    response_text: str | None = await app.request_api_shutdown()
    return ConsoleActionResult(
        summary=f"{app.friendly}: shutdown requested via API.",
        text=response_text,
        source=ConsoleResponseSource.API,
    )


_SATISFACTORY_CONSOLE_ACTIONS: tuple[ConsoleAction, ...] = (
    ConsoleAction(
        key="save_game",
        label="Save Game",
        description="Create a timestamped save through the Satisfactory HTTPS API.",
        power_level=Power_Level.user,
        execute=_console_save_game,
    ),
    ConsoleAction(
        key="run_command",
        label="Run Command",
        description="Send a raw command through the Satisfactory HTTPS API.",
        power_level=Power_Level.sudo,
        execute=_console_raw_command,
        parameter=_SATISFACTORY_RAW_COMMAND_PARAMETER,
    ),
    ConsoleAction(
        key="shutdown",
        label="Shutdown",
        description="Gracefully stop the Satisfactory server through the HTTPS API.",
        power_level=Power_Level.sudo,
        execute=_console_shutdown,
    ),
)


def _normalise_active_schematic_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    text: str = raw.strip().strip("\"'")
    if not text:
        return None
    text = text.rsplit("/", 1)[-1]
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    schematic_id: str = text.removesuffix("_C")
    if schematic_name := _SATISFACTORY_SCHEMATIC_NAMES.get(schematic_id):
        return schematic_name
    return schematic_id.replace("_", " ").strip() or None


class Provider_SatisfactoryDay(AppActivityProvider["Satisfactory"]):
    metadata = AppActivityProviderMetadata(provider_id="day", label="Day Counter")

    async def get(self) -> str | None:
        state: SatisfactoryServerState | None = self.app._players.state
        if state is None or state.total_game_duration is None:
            return None
        return f"D{state.total_game_duration // 86400}"


class Provider_SatisfactoryStage(AppActivityProvider["Satisfactory"]):
    metadata = AppActivityProviderMetadata(provider_id="stage", label="Stage")

    async def get(self) -> str | None:
        state: SatisfactoryServerState | None = self.app._players.state
        if state is None:
            return None

        status_parts: list[str] = []
        if state.tech_tier is not None:
            status_parts.append(f"T{state.tech_tier}")
        if active_schematic := _normalise_active_schematic_label(state.active_schematic):
            status_parts.append(active_schematic)
        if not status_parts:
            return None
        return ": ".join(status_parts)


def _build_satisfactory_activity_providers(app: "Satisfactory") -> tuple[AppActivityProvider["Satisfactory"], ...]:
    return (
        Provider_SatisfactoryDay(app),
        Provider_SatisfactoryStage(app),
    )


class SatisfactoryPlayers:
    def __init__(self, app: Satisfactory) -> None:
        self.app: Satisfactory = app
        self._players_task: asyncio.Task[None] | None = None
        self._running = False
        self._state: SatisfactoryServerState | None = None
        self._poll_failed = False

    @property
    def state(self) -> SatisfactoryServerState | None:
        return self._state

    def set_state(self, state: SatisfactoryServerState) -> None:
        self._state = state
        self.app._sync_provider_text(state)

    async def start(self) -> None:
        self._running = True
        if self._players_task and not self._players_task.done():
            return
        self._players_task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        self._running = False
        if self._players_task is None:
            return
        self._players_task.cancel()
        try:
            await self._players_task
        except asyncio.CancelledError:
            pass
        self._players_task = None

    async def _poll(self) -> None:
        while self._running:
            try:
                state: SatisfactoryServerState = await self.app._bridge.query_server_state()
            except asyncio.CancelledError:
                raise
            except Exception as xcp:
                if not self._poll_failed:
                    log.warning(f"{self.app.friendly} player poll failed: {xcp}")
                else:
                    log.debug(f"{self.app.friendly} player poll still failing: {xcp}")
                self._poll_failed = True
            else:
                if self._poll_failed:
                    log.info(f"{self.app.friendly} player poll recovered.")
                self._poll_failed = False
                self.set_state(state)
            await asyncio.sleep(_PLAYER_POLL_SECONDS)

    async def count(self) -> tuple[int, int] | None:
        state: SatisfactoryServerState | None = self._state
        if state is None:
            return None
        if state.num_connected_players is None or state.player_limit is None:
            return None
        return (state.num_connected_players, state.player_limit)


class SatisfactoryPlayerSessionMatcher:
    def __init__(self, app: "Satisfactory") -> None:
        self.app: Satisfactory = app
        self._pending_by_player: dict[str, deque[SatisfactoryPlayerIdentity]] = defaultdict(deque)
        self._active_by_session: dict[str, SatisfactoryPlayerIdentity] = {}

    def reset(self) -> None:
        self._pending_by_player.clear()
        self._active_by_session.clear()

    async def match(self, line: str) -> None:
        if identity := self._match_login(line):
            self._note_login(identity)
            return
        if player_name := self._match_join(line):
            self._note_join(player_name)
            return
        if session_key := self._match_leave(line):
            self._note_leave(session_key)

    @staticmethod
    def _match_login(line: str) -> SatisfactoryPlayerIdentity | None:
        if match := _SATISFACTORY_LOGIN_RE.search(line):
            return SatisfactoryPlayerIdentity(
                player_name=_normalise_satisfactory_player_name(match.group("player")),
                session_key=_satisfactory_session_key(match.group("identity")),
            )
        return None

    @staticmethod
    def _match_join(line: str) -> str | None:
        if match := _SATISFACTORY_JOIN_SUCCEEDED_RE.search(line):
            return _normalise_satisfactory_player_name(match.group("player"))
        return None

    @staticmethod
    def _match_leave(line: str) -> str | None:
        close_match: re.Match[str] | None = _SATISFACTORY_CONNECTION_CLOSE_RE.search(line)
        if close_match is None:
            close_match = _SATISFACTORY_CONNECTION_REMOVED_RE.search(line)
        if close_match is None:
            return None
        return _satisfactory_session_key(close_match.group("identity"))

    def _note_login(self, identity: SatisfactoryPlayerIdentity) -> None:
        session_key: str = identity.session_key
        if session_key in self._active_by_session:
            return
        pending_logins = self._pending_by_player[_satisfactory_player_key(identity.player_name)]
        if any(candidate.session_key == session_key for candidate in pending_logins):
            return
        pending_logins.append(identity)

    def _note_join(self, player_name: str) -> None:
        player_key: str = _satisfactory_player_key(player_name)
        pending_logins = self._pending_by_player.get(player_key)
        if not pending_logins:
            return
        identity: SatisfactoryPlayerIdentity = pending_logins.popleft()
        if not pending_logins:
            self._pending_by_player.pop(player_key, None)
        if identity.session_key in self._active_by_session:
            return
        self._active_by_session[identity.session_key] = identity
        self._emit_notice(player_name=identity.player_name, action=PlayerSessionAction.JOINED)

    def _note_leave(self, session_key: str) -> None:
        identity: SatisfactoryPlayerIdentity | None = self._active_by_session.pop(session_key, None)
        if identity is None:
            return
        self._remove_pending_session(session_key=session_key, player_name=identity.player_name)
        self._emit_notice(player_name=identity.player_name, action=PlayerSessionAction.LEFT)

    def _remove_pending_session(self, *, session_key: str, player_name: str) -> None:
        player_key: str = _satisfactory_player_key(player_name)
        pending_logins = self._pending_by_player.get(player_key)
        if not pending_logins:
            return
        remaining_logins = deque(
            candidate for candidate in pending_logins if candidate.session_key != session_key
        )
        if remaining_logins:
            self._pending_by_player[player_key] = remaining_logins
            return
        self._pending_by_player.pop(player_key, None)

    def _emit_notice(self, *, player_name: str, action: PlayerSessionAction) -> None:
        if action is PlayerSessionAction.JOINED:
            if self.app.relay_notice_player_joined_enabled is False:
                return
        elif action is PlayerSessionAction.LEFT and self.app.relay_notice_player_left_enabled is False:
            return
        notice = PlayerSessionNotice(action=action, source=RelayNoticeSource.APP_LOG)
        app_friendly: str = getattr(self.app, "friendly", self.app.name)
        DC_Relay.add(
            DC_Bound(
                self.app,
                render_notice_text(notice, author_name=player_name, app_name=app_friendly),
                player_name,
                notice=notice,
            )
        )


class Satisfactory(App[Satisfactory_Config]):
    cfg_cls: type[Satisfactory_Config] = Satisfactory_Config
    relay_notice_player_session_supported = True
    name_platforms: tuple[str, ...] = ("steam", "egs")
    preferred_name_platform: str | None = "steam"

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: Satisfactory_Config):
        self.manage_embed_color = 0xF59E0B
        self.proc_name = "FactoryServer-Linux-Shipping"
        self.proc_cmd = [self.proc_name]
        self.cmd_start = cfg.cmd_start or ["bash", "FactoryServer.sh"]
        self.process = None

        host: str | None = cfg.effective_api_host
        port: int | None = cfg.effective_api_port
        if host is None or port is None:
            raise ValueError("Satisfactory requires an API host and port.")
        bridge_cfg: SatisfactoryBridgeConfig = SatisfactoryBridgeConfig(
            host=host,
            port=port,
            token=cfg.api_token,
            password=cfg.admin_password,
            verify_ssl_chain_path=cfg.verify_ssl_chain_path,
        )
        self._bridge: SatisfactoryBridge = SatisfactoryBridge(bridge_cfg)

        settings_cache = config.DIR_LOG / cfg.name / "satisfactory-settings.json"
        settings_cache.parent.mkdir(parents=True, exist_ok=True)
        if not settings_cache.exists():
            settings_cache.write_text("{}", config.STR_ENCODE)
        super().__init__(bot, am, cfg)
        if cfg.steam_update is not None:
            self.updater = SteamCmd_Update_Manager(self)
        self._blueprint_ownership_store = SatisfactoryBlueprintOwnershipStore(
            self.dir_log / "satisfactory-blueprints.json"
        )
        self._settings: SatisfactorySettings = SatisfactorySettings(
            settings_cache,
            self._bridge,
            lambda: self._running,
            cfg,
            self.file_instances,
            version_getter=lambda: cfg.version,
        )
        self.settings = Settings_Manager(cfg, self._settings)
        self.act_err_threshold = 50
        self.apply_version(
            detect_satisfactory_version(directory=cfg.directory, server_log=cfg.server_log_file),
            persist=False,
        )

        self._tail: Tailer | None = None
        self._tail_matchers: set[Callable[[str], Awaitable[None]]] = set()
        self._tail_matchers.add(self._match_version)
        self._player_session_matcher: SatisfactoryPlayerSessionMatcher = SatisfactoryPlayerSessionMatcher(self)
        self._tail_matchers.add(self._player_session_matcher.match)
        self._players: SatisfactoryPlayers = SatisfactoryPlayers(self)
        self._save_index: dict[str, SatisfactorySaveHeader] = {}
        self.set_activity_providers(_build_satisfactory_activity_providers(self))

    def detect_installed_version(self) -> AppVersion | None:
        return detect_satisfactory_version(directory=self.cfg.directory, server_log=self.cfg.server_log_file)

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return self.available_console_actions(_SATISFACTORY_CONSOLE_ACTIONS)

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        return (
            AppSaveRoot(
                id=_SATISFACTORY_SAVE_ROOT_ID,
                label=_SATISFACTORY_SAVE_ROOT_LABEL,
                path=self.cfg.directory / "FactoryGame" / "Saved" / "SaveGames",
                mode=AppSaveRootMode.CHILDREN,
                recursive=True,
                suffixes=frozenset({".sav"}),
                include_files=True,
                include_directories=False,
            ),
        )

    @property
    def supports_save_uploads(self) -> bool:
        return True

    @property
    def supports_save_delete(self) -> bool:
        return True

    @property
    def supports_blueprints(self) -> bool:
        return True

    async def run_console_command(self, command: str) -> str | None:
        return await self._call_live_api(
            lambda: self._bridge.run_command(command),
            unavailable_detail=f"{self.friendly} API is unavailable.",
        )

    async def request_manual_save(self) -> tuple[str, str | None]:
        self._require_live_runtime_api()
        save_name: str | None = self._build_session_save_name(reason="manual")
        if save_name is None:
            raise RuntimeError(f"{self.friendly} does not have an active game session to save.")
        response_text = await self._call_live_api(
            lambda: self._bridge.save_game(save_name),
            unavailable_detail=f"{self.friendly} API is unavailable.",
        )
        return (save_name, response_text)

    async def request_api_shutdown(self) -> str | None:
        return await self._call_live_api(
            self._bridge.shutdown,
            unavailable_detail=f"{self.friendly} API is unavailable.",
        )

    async def list_save_files_async(self) -> tuple[AppSaveEntry, ...]:
        if not self.check_running():
            self._save_index = {}
            return ()
        try:
            enumeration = await self._enumerate_live_save_sessions()
        except RuntimeError as xcp:
            self._save_index = {}
            log.warning("%s", xcp)
            return ()
        self._save_index = enumeration.save_header_by_id()
        return enumeration.save_entries()

    async def download_save_content(self, file_id: str) -> tuple[str, bytes] | None:
        self._require_live_save_api()
        save_header = await self._save_header_for_id(file_id)
        content = await self._call_live_api(
            lambda: self._bridge.download_save_game(save_header.save_name),
            unavailable_detail=f"{self.friendly} save API is unavailable.",
        )
        return (save_header.save_name, content)

    async def upload_save_file_async(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        self._require_live_save_api()
        if root_id != _SATISFACTORY_SAVE_ROOT_ID:
            raise ValueError(f"Unknown save root: {root_id}")
        if "/" in upload_name or "\\" in upload_name or Path(upload_name).name != upload_name:
            raise ValueError("Satisfactory save upload filename must not include path separators.")
        if Path(upload_name).suffix.casefold() != ".sav":
            raise ValueError("Satisfactory save uploads must use a .sav filename.")
        await self._call_live_api(
            lambda: self._bridge.upload_save_game(save_name=upload_name, source_path=source_path),
            unavailable_detail=f"{self.friendly} save API is unavailable.",
        )
        save_header = await self._find_save_header_by_name(upload_name)
        if save_header is None:
            raise FileNotFoundError(f"Uploaded save is not yet visible through the Satisfactory API: {upload_name}")
        return save_header.to_app_save_entry()

    async def delete_save_file_async(self, *, file_id: str) -> AppSaveEntry:
        self._require_live_save_api()
        save_header = await self._save_header_for_id(file_id)
        await self._call_live_api(
            lambda: self._bridge.delete_save_file(save_header.save_name),
            unavailable_detail=f"{self.friendly} save API is unavailable.",
        )
        self._save_index.pop(file_id, None)
        return save_header.to_app_save_entry()

    @property
    def default_blueprint_session_name(self) -> str | None:
        return _SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME

    async def _save_header_for_id(self, file_id: str) -> SatisfactorySaveHeader:
        if save_header := self._save_index.get(file_id):
            return save_header
        enumeration = await self._enumerate_live_save_sessions()
        self._save_index = enumeration.save_header_by_id()
        if save_header := self._save_index.get(file_id):
            return save_header
        raise FileNotFoundError(f"Unknown save file: {file_id}")

    async def _find_save_header_by_name(self, save_name: str) -> SatisfactorySaveHeader | None:
        for save_header in (await self._enumerate_live_save_sessions()).save_header_by_id().values():
            if save_header.save_name == save_name:
                self._save_index[save_header.to_app_save_entry().id] = save_header
                return save_header
        return None

    def _require_live_save_api(self) -> None:
        self._require_live_runtime_api()

    def _require_live_runtime_api(self) -> None:
        if not self.check_running():
            raise RuntimeError(f"{self.friendly} is not running.")

    async def _enumerate_live_save_sessions(self) -> SatisfactorySessionEnumerationSnapshot:
        enumeration = await self._call_live_api(
            self._bridge.enumerate_sessions,
            unavailable_detail=f"{self.friendly} save API is unavailable.",
        )
        with contextlib.suppress(Exception):
            for session in enumeration.sessions:
                self._ensure_shared_blueprint_session_link(session.session_name)
        return enumeration

    async def _call_live_api(
        self,
        operation: Callable[[], Awaitable[SettingValueT]],
        *,
        unavailable_detail: str,
    ) -> SettingValueT:
        self._require_live_runtime_api()
        try:
            return await operation()
        except (aiohttp.ClientError, OSError, TimeoutError) as xcp:
            raise RuntimeError(unavailable_detail) from xcp

    def _blueprint_root_path(self) -> Path:
        override = getattr(self, "_blueprint_root_override", None)
        if isinstance(override, Path):
            return override
        return _BLUEPRINT_ROOT

    def list_blueprint_files(self) -> tuple[AppBlueprintEntry, ...]:
        self._prepare_shared_blueprint_layout()
        return list_blueprint_files(
            self._blueprint_storage_root_path(),
            uploaded_by_user_id_by_relative_path=self._blueprint_ownership_store.uploaded_by_user_id_by_relative_path(),
        )

    @staticmethod
    def _require_blueprint_delete_permission(
        *,
        relative_path: str,
        uploaded_by_user_id_by_relative_path: dict[str, int],
        actor_user_id: int,
        actor_is_sudo: bool,
    ) -> int | None:
        uploaded_by_user_id: int | None = uploaded_by_user_id_by_relative_path.get(relative_path)
        if uploaded_by_user_id is not None and uploaded_by_user_id != actor_user_id and not actor_is_sudo:
            raise PermissionError("Only the uploader or a sudo user can delete this blueprint file.")
        if uploaded_by_user_id is None and not actor_is_sudo:
            raise PermissionError("Only a sudo user can delete blueprint files with no recorded uploader.")
        return uploaded_by_user_id

    def upload_blueprint_file(
        self,
        *,
        session_name: str,
        upload_name: str,
        source_path: Path,
        actor_user_id: int,
        config_upload_name: str | None = None,
        config_source_path: Path | None = None,
    ) -> AppBlueprintEntry:
        if (config_upload_name is None) != (config_source_path is None):
            raise ValueError("Blueprint config upload requires both a filename and source path.")
        self._prepare_shared_blueprint_layout()
        self._ensure_shared_blueprint_session_link(session_name)
        root = self._blueprint_storage_root_path()
        upload_pair: BlueprintUploadPair = validate_blueprint_upload_pair(
            module_filename=upload_name,
            config_filename=config_upload_name,
        )
        module_destination, module_relative_path = resolve_blueprint_upload_target(
            root,
            session_name=_SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME,
            upload_name=upload_pair.module_filename,
        )
        config_target: tuple[Path, str] | None = None
        if upload_pair.config_filename is not None:
            config_destination, config_relative_path = resolve_blueprint_upload_target(
                root,
                session_name=_SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME,
                upload_name=upload_pair.config_filename,
            )
            config_target = (config_destination, config_relative_path)
        if module_destination.exists():
            raise FileExistsError(f"Blueprint file already exists: {module_relative_path}")
        if config_target is not None and config_target[0].exists():
            raise FileExistsError(f"Blueprint config file already exists: {config_target[1]}")

        module_destination.parent.mkdir(parents=True, exist_ok=True)
        cleanup_paths: list[Path] = [module_destination]
        if config_target is not None:
            cleanup_paths.append(config_target[0])
        relative_paths_to_record: tuple[str, ...] = (
            (module_relative_path,) if config_target is None else (module_relative_path, config_target[1])
        )
        try:
            File_Utils.copy(source_path, module_destination, overwrite=False)
            if config_target is not None:
                if config_source_path is None:
                    raise ValueError("Blueprint config upload requires a source path.")
                File_Utils.copy(config_source_path, config_target[0], overwrite=False)
            self._blueprint_ownership_store.record_upload_batch(
                relative_paths=relative_paths_to_record,
                actor_user_id=actor_user_id,
            )
        except Exception:
            for cleanup_path in reversed(cleanup_paths):
                cleanup_path.unlink(missing_ok=True)
            self._cleanup_empty_blueprint_directory(module_destination.parent)
            raise
        return describe_blueprint(
            root,
            relative_path=module_relative_path,
            uploaded_by_user_id_by_relative_path=self._blueprint_ownership_store.uploaded_by_user_id_by_relative_path(),
        )

    def delete_blueprint_file(
        self,
        *,
        file_id: str,
        actor_user_id: int,
        actor_is_sudo: bool,
    ) -> AppBlueprintEntry:
        self._prepare_shared_blueprint_layout()
        root = self._blueprint_storage_root_path()
        canonical_file_id: str = self._canonical_blueprint_file_id(file_id)
        blueprint_path, relative_path = resolve_blueprint_file_path(root, canonical_file_id)
        if not blueprint_path.exists():
            raise FileNotFoundError(f"Blueprint file does not exist: {relative_path}")
        ownership_index = self._blueprint_ownership_store.uploaded_by_user_id_by_relative_path()
        file_type = blueprint_file_type_from_name(blueprint_path.name)
        self._require_blueprint_delete_permission(
            relative_path=relative_path,
            uploaded_by_user_id_by_relative_path=ownership_index,
            actor_user_id=actor_user_id,
            actor_is_sudo=actor_is_sudo,
        )

        if file_type is AppBlueprintFileType.CONFIG:
            module_relative_path = find_matching_blueprint_module_relative_path(root, relative_path)
            if module_relative_path is None:
                raise FileNotFoundError(f"Blueprint module does not exist for config file: {relative_path}")
            module_path, _ = resolve_blueprint_file_path(root, module_relative_path)
            if not module_path.exists():
                raise FileNotFoundError(f"Blueprint module does not exist for config file: {relative_path}")
            blueprint_path.unlink()
            self._blueprint_ownership_store.clear(relative_path=relative_path)
            self._cleanup_empty_blueprint_directory(blueprint_path.parent)
            return describe_blueprint(
                root,
                relative_path=module_relative_path,
                uploaded_by_user_id_by_relative_path=self._blueprint_ownership_store.uploaded_by_user_id_by_relative_path(),
            )

        deleted_entry = describe_blueprint(
            root,
            relative_path=relative_path,
            uploaded_by_user_id_by_relative_path=ownership_index,
        )
        config_relative_path = find_matching_blueprint_config_relative_path(root, relative_path)
        config_path: Path | None = None
        if config_relative_path is not None:
            config_path, _ = resolve_blueprint_file_path(root, config_relative_path)
            self._require_blueprint_delete_permission(
                relative_path=config_relative_path,
                uploaded_by_user_id_by_relative_path=ownership_index,
                actor_user_id=actor_user_id,
                actor_is_sudo=actor_is_sudo,
            )
        blueprint_path.unlink()
        self._blueprint_ownership_store.clear(relative_path=relative_path)
        if config_path is not None and config_path.exists() and config_relative_path is not None:
            config_path.unlink()
            self._blueprint_ownership_store.clear(relative_path=config_relative_path)
        self._cleanup_empty_blueprint_directory(blueprint_path.parent)
        return deleted_entry

    def _blueprint_storage_root_path(self) -> Path:
        override = getattr(self, "_blueprint_shared_root_override", None)
        if isinstance(override, Path):
            return override
        root = self._blueprint_root_path()
        return root.with_name(f"{root.name}{_SATISFACTORY_BLUEPRINT_STORAGE_SUFFIX}")

    def _shared_blueprint_session_path(self) -> Path:
        return self._blueprint_storage_root_path() / _SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME

    def _cleanup_empty_blueprint_directory(self, directory: Path) -> None:
        if directory.resolve() == self._shared_blueprint_session_path().resolve():
            return
        with contextlib.suppress(OSError):
            directory.rmdir()

    def _canonical_blueprint_file_id(self, file_id: str) -> str:
        normalised_file_id: str = normalise_blueprint_file_id(file_id)
        _session_name, filename = normalised_file_id.split("/", maxsplit=1)
        return str(Path(_SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME) / filename).replace("\\", "/")

    def _prepare_shared_blueprint_layout(self) -> None:
        mount_root = self._blueprint_root_path()
        mount_root.mkdir(parents=True, exist_ok=True)
        shared_session_path = self._shared_blueprint_session_path()
        shared_session_path.mkdir(parents=True, exist_ok=True)
        migrated_relative_paths = self._migrate_legacy_blueprint_directories(
            mount_root=mount_root,
            shared_session_path=shared_session_path,
        )
        self._blueprint_ownership_store.migrate_legacy_relative_paths(
            legacy_to_shared_relative_path=migrated_relative_paths,
        )
        for known_session_name in self._known_blueprint_session_names():
            self._ensure_shared_blueprint_session_link(known_session_name)

    def _migrate_legacy_blueprint_directories(
        self,
        *,
        mount_root: Path,
        shared_session_path: Path,
    ) -> dict[str, str]:
        migrated_relative_paths: dict[str, str] = {}
        for session_path in sorted(mount_root.iterdir(), key=lambda path: path.name.casefold()):
            if session_path.name.startswith("."):
                continue
            if session_path.is_symlink():
                self._validate_shared_blueprint_session_link(
                    session_path=session_path,
                    shared_session_path=shared_session_path,
                )
                continue
            if not session_path.is_dir():
                raise ValueError(f"Unsupported Satisfactory blueprint path: {session_path}")
            session_name = validate_blueprint_session_name(session_path.name)
            for child_path in sorted(session_path.iterdir(), key=lambda path: path.name.casefold()):
                if child_path.name.startswith("."):
                    continue
                if not child_path.is_file():
                    raise ValueError(f"Unsupported Satisfactory blueprint entry: {child_path}")
                filename = normalise_existing_blueprint_filename(child_path.name)
                target_path = shared_session_path / filename
                self._merge_blueprint_file(source_path=child_path, target_path=target_path, session_name=session_name)
                legacy_relative_path = f"{session_name}/{filename}"
                shared_relative_path = f"{_SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME}/{filename}"
                migrated_relative_paths[legacy_relative_path] = shared_relative_path
            with contextlib.suppress(OSError):
                session_path.rmdir()
            if session_path.exists():
                raise ValueError(f"Satisfactory blueprint session directory is not empty: {session_path}")
            session_path.symlink_to(shared_session_path, target_is_directory=True)
        return migrated_relative_paths

    @staticmethod
    def _merge_blueprint_file(*, source_path: Path, target_path: Path, session_name: str) -> None:
        if not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.replace(target_path)
            return
        if source_path.stat().st_size != target_path.stat().st_size or source_path.read_bytes() != target_path.read_bytes():
            raise FileExistsError(
                f"Conflicting shared blueprint file for {target_path.name} while migrating session {session_name}."
            )
        source_path.unlink()

    def _known_blueprint_session_names(self) -> tuple[str, ...]:
        names: list[str] = []
        players = getattr(self, "_players", None)
        state: SatisfactoryServerState | None = getattr(players, "state", None)
        if state is not None:
            if active_session_name := _validated_blueprint_session_name_or_none(state.active_session_name):
                names.append(active_session_name)
            if auto_load_session_name := _validated_blueprint_session_name_or_none(state.auto_load_session_name):
                names.append(auto_load_session_name)
        settings = getattr(self, "_settings", None)
        if isinstance(settings, App_Settings):
            setting = settings.get_setting("auto_load_session_name")
            if setting is not None and isinstance(setting.value, str):
                if configured_session_name := _validated_blueprint_session_name_or_none(setting.value):
                    names.append(configured_session_name)
        return tuple(dict.fromkeys(names))

    def _ensure_shared_blueprint_session_link(self, session_name: str) -> None:
        validated_session_name = validate_blueprint_session_name(session_name)
        if validated_session_name == _SATISFACTORY_BLUEPRINT_SHARED_SESSION_NAME:
            return
        mount_root = self._blueprint_root_path()
        shared_session_path = self._shared_blueprint_session_path()
        session_path = mount_root / validated_session_name
        if session_path.exists() or session_path.is_symlink():
            self._validate_shared_blueprint_session_link(
                session_path=session_path,
                shared_session_path=shared_session_path,
            )
            return
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.symlink_to(shared_session_path, target_is_directory=True)

    @staticmethod
    def _validate_shared_blueprint_session_link(*, session_path: Path, shared_session_path: Path) -> None:
        if not session_path.is_symlink():
            raise ValueError(f"Satisfactory blueprint session path must be a symlink: {session_path}")
        resolved_target = session_path.resolve()
        if resolved_target != shared_session_path.resolve():
            raise ValueError(
                f"Satisfactory blueprint session link points to an unexpected target: "
                f"{session_path} -> {resolved_target}"
            )

    async def _warm_bridge(self) -> None:
        for attempt in range(_API_READY_RETRIES):
            try:
                state: SatisfactoryServerState = await self._bridge.query_server_state()
            except Exception as xcp:
                if attempt == _API_READY_RETRIES - 1:
                    raise TimeoutError(f"{self.friendly} API did not become ready after startup.") from xcp
                await asyncio.sleep(_API_READY_SLEEP_SECONDS)
            else:
                self._players.set_state(state)
                return

    def _sync_provider_text(self, state: SatisfactoryServerState) -> None:
        alt_text: str | None = state.active_session_name or state.auto_load_session_name
        if alt_text:
            self.cfg.provider_alt_text = alt_text
        with contextlib.suppress(Exception):
            for session_name in filter(None, (state.active_session_name, state.auto_load_session_name)):
                self._ensure_shared_blueprint_session_link(cast(str, session_name))

    async def _match_version(self, line: str) -> None:
        if match := _SATISFACTORY_BUILD_RE.search(line):
            current_version = self.cfg.version
            self.apply_version(
                _app_version_from_satisfactory_build_match(match, current=current_version),
                persist=True,
            )

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        self._player_session_matcher.reset()
        await self._std_launch()
        while not self.check_running():
            await asyncio.sleep(1)

        if self.server_log and self.server_log.exists():
            File_Utils.link(self.server_log, self.file_stdout.with_name(self.server_log.name))

        if self.process and self.process.stdout:
            self._tail = Tailer(self.check_running, self.process.stdout, self.file_stdout)
        elif self.server_log:
            self._tail = Tailer(self.check_running, self.server_log, self.file_stdout)
        else:
            raise SystemError("No log source available for Satisfactory tailing")
        await self._tail.start(self._tail_matchers)

        self._running = True
        await self._warm_bridge()
        try:
            await self._settings.apply_current_values()
        except Exception as xcp:
            log.warning(f"{self.friendly} pending settings were not applied: {xcp}")
        try:
            await self._settings.refresh_from_server()
        except Exception as xcp:
            log.warning(f"{self.friendly} settings refresh failed after startup: {xcp}")

        await self._players.start()
        self.register_enabled_activity_providers()
        return True

    async def _wait_for_exit(self, timeout_seconds: float) -> bool:
        deadline: float = asyncio.get_running_loop().time() + timeout_seconds
        while self.check_running() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        return not self.check_running()

    def _build_session_save_name(self, *, reason: str) -> str | None:
        state: SatisfactoryServerState | None = self._players.state
        if state is None or not state.is_game_running or not state.active_session_name:
            return None
        safe_session = re.sub(r"[^A-Za-z0-9._-]+", "-", state.active_session_name).strip("-")
        safe_session = safe_session or "satisfactory"
        timestamp: str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{safe_session}-{reason}-{timestamp}"

    def _build_stop_save_name(self) -> str | None:
        return self._build_session_save_name(reason="stop")

    async def _graceful_shutdown(self) -> bool:
        try:
            if save_name := self._build_stop_save_name():
                await self._bridge.save_game(save_name)
            await self._bridge.shutdown()
            return True
        except Exception as xcp:
            log.warning(f"{self.friendly} API shutdown failed, falling back to SIGINT: {xcp}")

        if self.process is None:
            return False
        try:
            self.process.send_signal(signal.SIGINT)
        except Exception as xcp:
            log.warning(f"{self.friendly} SIGINT fallback failed: {xcp}")
            return False
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._players.stop()
        self.deregister_activity_providers()

        graceful_stop_started = await self._graceful_shutdown()
        if graceful_stop_started:
            await self._wait_for_exit(_STOP_WAIT_SECONDS)

        if self._tail:
            await self._tail.stop()

        if self.check_running():
            await self._terminate()
        else:
            self.process = None
        self._player_session_matcher.reset()
        return True

    async def kill(self) -> bool:
        log.info(f"{__name__}.kill")
        self._running = False
        await self._players.stop()
        self.deregister_activity_providers()
        if self._tail:
            await self._tail.stop()
        await self._terminate()
        self._player_session_matcher.reset()
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


# AiviA APasz
