from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import signal
import ssl
from asyncio.events import AbstractEventLoop
from asyncio.locks import Lock
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from re import Pattern
from ssl import SSLContext
from typing import Any, Protocol, Self, TypeVar
from urllib.parse import SplitResult, urlsplit

import hikari
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from satisfactory_api_client import AsyncSatisfactoryAPI
from satisfactory_api_client.data.minimum_privilege_level import MinimumPrivilegeLevel
from satisfactory_api_client.data.response import Response as SatisfactoryAPIResponse
from satisfactory_api_client.data.server_options import ServerOptions

import config
from _file import File_Utils
from _security import Power_Level
from apps._app import App
from apps._config import App_Config, AppVersion, resolve_config_path
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
from config import Activity_Manager

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
_SATISFACTORY_BUILD_RE: Pattern[str] = re.compile(
    r"LogInit:\s+Build:\s+\+\+FactoryGame\+rel-main-(?P<version>\d+\.\d+\.\d+)-CL-(?P<build>\d+)",
    re.IGNORECASE,
)
_SML_UPLUGIN_FILES: tuple[Path, Path] = (
    Path("FactoryGame") / "Mods" / "SML" / "SML.uplugin",
    Path("Mods") / "SML" / "SML.uplugin",
)


class SatisfactoryNetworkQuality(enum.IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    ULTRA = 3


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


def detect_satisfactory_version(*, directory: Path, server_log: Path | None) -> AppVersion | None:
    game_version: str | None = None
    for pointer in _candidate_satisfactory_logs(directory=directory, server_log=server_log):
        try:
            for line in pointer.read_text(config.STR_ENCODE, errors="ignore").splitlines():
                if match := _SATISFACTORY_BUILD_RE.search(line):
                    game_version = match.group("version").strip()
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
        return AppVersion(main=game_version, framework=framework or None, loader="sml")

    return AppVersion(main=game_version)


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
    is_game_running: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("isGameRunning", "IsGameRunning"),
    )
    average_tick_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("averageTickRate", "AverageTickRate"),
    )

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, object]) -> Self:
        state_payload: object | None = payload.get("serverGameState")
        if state_payload is None:
            state_payload = payload.get("ServerGameState")
        if not isinstance(state_payload, Mapping):
            raise ValueError("server game state payload must be a mapping")
        return cls.model_validate(_string_object_mapping(state_payload, label="server game state payload"))


class SatisfactorySettingsSnapshot(BaseModel):
    auto_load_session_name: str | None = None
    auto_pause: bool | None = Field(default=None, validation_alias="FG.DSAutoPause")
    auto_save_on_disconnect: bool | None = Field(default=None, validation_alias="FG.DSAutoSaveOnDisconnect")
    autosave_interval_seconds: int | None = Field(default=None, validation_alias="FG.AutosaveInterval")
    send_gameplay_data: bool | None = Field(default=None, validation_alias="FG.SendGameplayData")
    network_quality: SatisfactoryNetworkQuality | None = Field(default=None, validation_alias="FG.NetworkQuality")

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
    def from_api_payloads(cls, state: SatisfactoryServerState, payload: Mapping[str, object]) -> Self:
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
        effective_options = dict(server_options) if isinstance(server_options, Mapping) else {}
        if isinstance(pending_options, Mapping):
            effective_options.update(pending_options)
        effective_options["auto_load_session_name"] = state.auto_load_session_name
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

    async def read_settings(self) -> SatisfactorySettingsSnapshot:
        state: SatisfactoryServerState = await self.query_server_state()
        payload: object = (await self._call("get_server_options")).data
        return SatisfactorySettingsSnapshot.from_api_payloads(
            state,
            _string_object_mapping(payload, label="get_server_options payload"),
        )

    async def apply_settings(self, settings: SatisfactorySettingsSnapshot) -> None:
        if settings.auto_load_session_name is not None:
            await self._call("set_auto_load_session_name", settings.auto_load_session_name)
        server_options: ServerOptions = settings.to_sdk_server_options()
        if server_options.to_dict():
            await self._call("apply_server_options", server_options)

    async def save_game(self, save_name: str) -> None:
        await self._call("save_game", save_name)

    async def shutdown(self) -> None:
        await self._call("shutdown")

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


class SatisfactorySettings(App_Settings):
    """Caches desired settings locally and applies them via the HTTPS API when reachable."""

    def __init__(
        self,
        pointer: Path,
        bridge: SatisfactorySettingsBridge,
        is_running: Callable[[], bool],
        cfg: Satisfactory_Config,
        instances_path: Path,
    ) -> None:
        self._bridge: SatisfactorySettingsBridge = bridge
        self._is_running: Callable[[], bool] = is_running
        self._cfg: Satisfactory_Config = cfg
        self._instances_path: Path = instances_path
        self._apply_task: asyncio.Task[None] | None = None

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
                self._admin_password,
            ],
        )

    def _snapshot_from_settings(self) -> SatisfactorySettingsSnapshot:
        return SatisfactorySettingsSnapshot(
            auto_load_session_name=self._optional_setting_value(self._auto_load_session_name, str),
            auto_pause=self._optional_setting_value(self._auto_pause, bool),
            auto_save_on_disconnect=self._optional_setting_value(self._auto_save_on_disconnect, bool),
            autosave_interval_seconds=self._optional_setting_value(self._autosave_interval_seconds, int),
            send_gameplay_data=self._optional_setting_value(self._send_gameplay_data, bool),
            network_quality=(
                None
                if (quality := self._optional_setting_value(self._network_quality, int)) is None
                else SatisfactoryNetworkQuality(quality)
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
        self._assign_setting(self._auto_pause, snapshot.auto_pause)
        self._assign_setting(self._auto_save_on_disconnect, snapshot.auto_save_on_disconnect)
        self._assign_setting(self._autosave_interval_seconds, snapshot.autosave_interval_seconds)
        self._assign_setting(self._send_gameplay_data, snapshot.send_gameplay_data)
        self._assign_setting(
            self._network_quality,
            None if snapshot.network_quality is None else snapshot.network_quality.value,
        )

    def _apply_local_settings(self) -> None:
        self._assign_setting(self._admin_password, self._cfg.admin_password)

    def _write_snapshot(self, snapshot: SatisfactorySettingsSnapshot) -> dict[str, object]:
        payload = snapshot.model_dump(mode="json")
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
        snapshot: SatisfactorySettingsSnapshot = await self._bridge.read_settings()
        self._apply_snapshot(snapshot)
        self._write_snapshot(snapshot)
        self._apply_local_settings()
        return snapshot

    async def apply_current_values(self) -> bool:
        if not self._is_running():
            return False
        snapshot: SatisfactorySettingsSnapshot = self._snapshot_from_settings()
        await self._bridge.apply_settings(snapshot)
        await self.refresh_from_server()
        return True


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


class Satisfactory(App[Satisfactory_Config]):
    cfg_cls: type[Satisfactory_Config] = Satisfactory_Config

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
        self._settings: SatisfactorySettings = SatisfactorySettings(
            settings_cache,
            self._bridge,
            lambda: self._running,
            cfg,
            self.file_instances,
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
        self._players: SatisfactoryPlayers = SatisfactoryPlayers(self)

    async def _warm_bridge(self) -> bool:
        for attempt in range(_API_READY_RETRIES):
            try:
                state: SatisfactoryServerState = await self._bridge.query_server_state()
            except Exception as xcp:
                if attempt == _API_READY_RETRIES - 1:
                    log.warning(f"{self.friendly} API was not ready after startup: {xcp}")
                    return False
                await asyncio.sleep(_API_READY_SLEEP_SECONDS)
            else:
                self._players.set_state(state)
                return True
        return False

    def _sync_provider_text(self, state: SatisfactoryServerState) -> None:
        alt_text: str | None = state.active_session_name or state.auto_load_session_name
        if alt_text:
            self.cfg.provider_alt_text = alt_text

    async def _match_version(self, line: str) -> None:
        if match := _SATISFACTORY_BUILD_RE.search(line):
            self.apply_version(match.group("version"), persist=True)

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
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
        bridge_ready: bool = await self._warm_bridge()
        if bridge_ready:
            try:
                await self._settings.apply_current_values()
            except Exception as xcp:
                log.warning(f"{self.friendly} pending settings were not applied: {xcp}")
            try:
                await self._settings.refresh_from_server()
            except Exception as xcp:
                log.warning(f"{self.friendly} settings refresh failed after startup: {xcp}")

        await self._players.start()
        return True

    async def _wait_for_exit(self, timeout_seconds: float) -> bool:
        deadline: float = asyncio.get_running_loop().time() + timeout_seconds
        while self.check_running() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        return not self.check_running()

    def _build_stop_save_name(self) -> str | None:
        state: SatisfactoryServerState | None = self._players.state
        if state is None or not state.is_game_running or not state.active_session_name:
            return None
        safe_session = re.sub(r"[^A-Za-z0-9._-]+", "-", state.active_session_name).strip("-")
        safe_session = safe_session or "satisfactory"
        timestamp: str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{safe_session}-stop-{timestamp}"

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

        graceful_stop_started = await self._graceful_shutdown()
        if graceful_stop_started:
            await self._wait_for_exit(_STOP_WAIT_SECONDS)

        if self._tail:
            await self._tail.stop()

        if self.check_running():
            await self._terminate()
        else:
            self.process = None
        return True

    async def kill(self) -> bool:
        log.info(f"{__name__}.kill")
        self._running = False
        await self._players.stop()
        if self._tail:
            await self._tail.stop()
        await self._terminate()
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


# AiviA APasz
