from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import signal
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Self, TypeVar
from urllib.parse import urlsplit

import hikari
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from satisfactory_api_client import AsyncSatisfactoryAPI
from satisfactory_api_client.data.minimum_privilege_level import MinimumPrivilegeLevel
from satisfactory_api_client.data.response import Response as SatisfactoryAPIResponse
from satisfactory_api_client.data.server_options import ServerOptions

import config
from _file import File_Utils
from apps._app import App
from apps._config import App_Config, resolve_config_path
from apps._settings import App_Settings, Setting
from apps._tailer import Tailer
from config import Activity_Manager

log = logging.getLogger(__name__)

_DEFAULT_API_PORT = 7777
_API_READY_RETRIES = 15
_API_READY_SLEEP_SECONDS = 2.0
_PLAYER_POLL_SECONDS = 15.0
_STOP_WAIT_SECONDS = 15.0
_NETWORK_QUALITY_CHOICES: dict[str, str] = {
    "Low": str(0),
    "Medium": str(1),
    "High": str(2),
    "Ultra": str(3),
}
_NETWORK_QUALITY_VALUES = frozenset(_NETWORK_QUALITY_CHOICES.values())


class SatisfactoryNetworkQuality(enum.IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    ULTRA = 3


def _normalise_api_address(raw: str) -> str:
    address = raw.strip().rstrip("/")
    if address.startswith("https://"):
        address = address.removeprefix("https://")
    elif address.startswith("http://"):
        address = address.removeprefix("http://")

    if address.startswith("["):
        has_port = "]:" in address
    else:
        has_port = address.count(":") == 1

    if not has_port:
        address = f"{address}:{_DEFAULT_API_PORT}"
    return address


def _parse_api_endpoint(address: str) -> tuple[str, int]:
    split = urlsplit(f"https://{_normalise_api_address(address)}")
    host = split.hostname
    try:
        port = split.port
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

    text = str(raw).strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    try:
        numeric = float(text)
    except ValueError as xcp:
        raise ValueError(f"{label} must be numeric") from xcp
    if not numeric.is_integer():
        raise ValueError(f"{label} must be an integer value")
    return int(numeric)


class Satisfactory_Config(App_Config):
    api_host: str | None = "127.0.0.1"
    api_token: str | None = None
    admin_password: str | None = None
    verify_ssl_chain_path: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_address(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        address = payload.get("address")
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

    @field_validator("api_token", "admin_password", mode="before")
    def blank_string_to_none(cls, raw: object) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

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
        state_payload = payload.get("serverGameState")
        if state_payload is None:
            state_payload = payload.get("ServerGameState")
        if not isinstance(state_payload, Mapping):
            raise ValueError("server game state payload must be a mapping")
        return cls.model_validate(state_payload)


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
        value = _coerce_integral_value(raw, label="autosave interval")
        if value < 0:
            raise ValueError("autosave interval must not be negative")
        return value

    @field_validator("network_quality", mode="before")
    def normalise_network_quality(cls, raw: object) -> SatisfactoryNetworkQuality | None:
        if raw is None:
            return None
        quality = _coerce_integral_value(raw, label="network quality")
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
        ssl_context = ssl.create_default_context()
        ssl_context.load_verify_locations(str(chain_path))
        self.cert_path = str(chain_path)
        self._ssl_context = ssl_context


class SatisfactoryBridge:
    def __init__(self, cfg: SatisfactoryBridgeConfig) -> None:
        self._cfg = cfg
        self._api: SatisfactoryAPIClient | None = None
        self._lock = asyncio.Lock()

    async def _build_api(self) -> SatisfactoryAPIClient:
        if self._cfg.token is None:
            api = SatisfactoryAPIClient(
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
        payload = (await self._call("query_server_state")).data
        if not isinstance(payload, Mapping):
            raise ValueError("query_server_state payload must be a mapping")
        return SatisfactoryServerState.from_api_payload(payload)

    async def read_settings(self) -> SatisfactorySettingsSnapshot:
        state = await self.query_server_state()
        payload = (await self._call("get_server_options")).data
        if not isinstance(payload, Mapping):
            raise ValueError("get_server_options payload must be a mapping")
        return SatisfactorySettingsSnapshot.from_api_payloads(state, payload)

    async def apply_settings(self, settings: SatisfactorySettingsSnapshot) -> None:
        if settings.auto_load_session_name is not None:
            await self._call("set_auto_load_session_name", settings.auto_load_session_name)
        server_options = settings.to_sdk_server_options()
        if server_options.to_dict():
            await self._call("apply_server_options", server_options)

    async def save_game(self, save_name: str) -> None:
        await self._call("save_game", save_name)

    async def shutdown(self) -> None:
        await self._call("shutdown")


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
    ) -> None:
        self._bridge = bridge
        self._is_running = is_running
        self._apply_task: asyncio.Task[None] | None = None

        self._auto_load_session_name = Setting(
            str,
            "Auto Load Session",
            "auto_load_session_name",
            [],
            desc="Session name that should load automatically when the server starts.",
        )
        self._auto_pause = Setting(
            bool,
            "Auto Pause",
            "FG.DSAutoPause",
            [],
            desc="Pause the simulation when no players are connected.",
        )
        self._auto_save_on_disconnect = Setting(
            bool,
            "Auto Save On Disconnect",
            "FG.DSAutoSaveOnDisconnect",
            [],
            desc="Save automatically when a player disconnects.",
        )
        self._autosave_interval_seconds = Setting(
            int,
            "Autosave Interval (s)",
            "FG.AutosaveInterval",
            [],
            validator=str.isdigit,
            desc="Seconds between automatic saves.",
        )
        self._send_gameplay_data = Setting(
            bool,
            "Send Gameplay Data",
            "FG.SendGameplayData",
            [],
            desc="Allow gameplay telemetry to be sent.",
        )
        self._network_quality = Setting(
            int,
            "Network Quality",
            "FG.NetworkQuality",
            [],
            choices=_NETWORK_QUALITY_CHOICES,
            validator=lambda raw: raw in _NETWORK_QUALITY_VALUES,
            desc="Higher values can improve network responsiveness at the cost of server performance.",
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
    def _optional_setting_value(setting: Setting, expected_type: type[SettingValueT]) -> SettingValueT | None:
        if isinstance(setting.value, hikari.UndefinedType):
            return None
        if expected_type is int and isinstance(setting.value, bool):
            raise TypeError(f"{setting.key} must be {expected_type.__name__}")
        if not isinstance(setting.value, expected_type):
            raise TypeError(f"{setting.key} must be {expected_type.__name__}")
        return setting.value

    @staticmethod
    def _assign_setting(setting: Setting, value: object | None) -> None:
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

    def save(self) -> dict[str, object]:
        payload = self._write_snapshot(self._snapshot_from_settings())
        if self._is_running():
            self._schedule_apply()
        return payload

    def _schedule_apply(self) -> None:
        try:
            loop = asyncio.get_running_loop()
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
        snapshot = await self._bridge.read_settings()
        self._apply_snapshot(snapshot)
        self._write_snapshot(snapshot)
        return snapshot

    async def apply_current_values(self) -> bool:
        if not self._is_running():
            return False
        snapshot = self._snapshot_from_settings()
        await self._bridge.apply_settings(snapshot)
        await self.refresh_from_server()
        return True


class SatisfactoryPlayers:
    def __init__(self, app: Satisfactory) -> None:
        self.app = app
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
                state = await self.app._bridge.query_server_state()
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
        state = self._state
        if state is None:
            return None
        if state.num_connected_players is None or state.player_limit is None:
            return None
        return (state.num_connected_players, state.player_limit)


class Satisfactory(App):
    cfg_cls = Satisfactory_Config

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: Satisfactory_Config):
        self.manage_embed_color = 0xF59E0B
        self.proc_name = "FactoryServer-Linux-Shipping"
        self.proc_cmd = [self.proc_name]
        self.cmd_start = cfg.cmd_start or ["bash", "FactoryServer.sh"]
        self.process = None

        host = cfg.effective_api_host
        port = cfg.effective_api_port
        if host is None or port is None:
            raise ValueError("Satisfactory requires an API host and port.")
        bridge_cfg = SatisfactoryBridgeConfig(
            host=host,
            port=port,
            token=cfg.api_token,
            password=cfg.admin_password,
            verify_ssl_chain_path=cfg.verify_ssl_chain_path,
        )
        self._bridge = SatisfactoryBridge(bridge_cfg)

        settings_cache = config.DIR_LOG / cfg.name / "satisfactory-settings.json"
        settings_cache.parent.mkdir(parents=True, exist_ok=True)
        if not settings_cache.exists():
            settings_cache.write_text("{}", config.STR_ENCODE)
        self._settings = SatisfactorySettings(settings_cache, self._bridge, lambda: self._running)

        super().__init__(bot, am, cfg, self._settings)
        self.act_err_threshold = 50

        self._tail: Tailer | None = None
        self._tail_matchers: set[Callable[[str], object]] = set()
        self._players = SatisfactoryPlayers(self)

    async def _warm_bridge(self) -> bool:
        for attempt in range(_API_READY_RETRIES):
            try:
                state = await self._bridge.query_server_state()
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
        alt_text = state.active_session_name or state.auto_load_session_name
        if alt_text:
            self.cfg.provider_alt_text = alt_text

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
        bridge_ready = await self._warm_bridge()
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
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self.check_running() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        return not self.check_running()

    def _build_stop_save_name(self) -> str | None:
        state = self._players.state
        if state is None or not state.is_game_running or not state.active_session_name:
            return None
        safe_session = re.sub(r"[^A-Za-z0-9._-]+", "-", state.active_session_name).strip("-")
        safe_session = safe_session or "satisfactory"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
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

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


# AiviA APasz
