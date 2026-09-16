"""Garry's Mod dedicated-server integration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import IO, Any, Final, cast

import hikari

import config
from _async_utils import run_blocking
from _security import Power_Level
from apps._app import App, AppPortClaim, NetworkProtocol
from apps._config import App_Config, AppVersion, SteamUpdatePreset
from apps._config_files import AppConfigFileContent, AppConfigFileKind, AppConfigFileRoot
from apps._settings import App_Settings, IntSettingSpec, Setting, Setting_Label, StringSettingSpec
from apps._steam import SteamGameServerLoginTokenStatus, normalise_steam_game_server_login_token
from apps._updater import SteamCmd_Update_Manager
from config import Activity_Manager

log = logging.getLogger(__name__)

GMOD_DEFAULT_PORT: Final[int] = 27015
GMOD_DEFAULT_GAMEMODE: Final[str] = "sandbox"
GMOD_DEFAULT_STARTUP_MAP: Final[str] = "gm_construct"
GMOD_DEFAULT_MAX_PLAYERS: Final[int] = 16
GMOD_MANAGE_EMBED_COLOR: Final[int] = 0x1194F0
STEAM_GAME_APP_ID: Final[int] = 4000
STEAM_APP_ID: Final[int] = 4020
STEAM_UPDATE_PRESET: Final[SteamUpdatePreset] = SteamUpdatePreset(app_id=STEAM_APP_ID)

_GMOD_MANAGED_DIRECTORY_NAME: Final[str] = ".yukibot"
_GMOD_SETTINGS_FILENAME: Final[str] = "gmod-settings.json"
_GMOD_GSLT_FILENAME: Final[str] = "steam-game-server-login-token"
_GMOD_SERVER_CONFIG_FILENAME: Final[str] = "server.cfg"
_GMOD_LAUNCH_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
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
_GMOD_LEGACY_PLACEHOLDER_VERSION: Final[str] = "0.0"
_REDACTED_SECRET: Final[str] = "[REDACTED]"
_GMOD_LEGACY_STEAM_ACCOUNT_WARNING: Final[str] = (
    "server.cfg contains legacy/manual sv_setsteamaccount configuration. Remove it and use "
    "Yukibot's dedicated Steam Game Server Login Token (GSLT) setting instead."
)


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


def gmod_start_command(
    *,
    port: int | None,
    max_players: int,
    gamemode: str,
    startup_map: str,
    game_server_login_token: str,
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
    return [
        "./srcds_run",
        "-game",
        "garrysmod",
        "+port",
        str(resolved_port),
        "+maxplayers",
        str(max_players),
        "+gamemode",
        resolved_gamemode,
        "+map",
        resolved_map,
        "+sv_setsteamaccount",
        token,
    ]


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
                },
                indent=4,
            )
            + "\n",
            config.STR_ENCODE,
        )
        created.append(settings)
    return tuple(created)


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


class Gmod_Settings(App_Settings):
    """Yukibot-owned startup settings that are applied as Source launch arguments."""

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

    def _string_setting_value(self, key: str) -> str:
        setting = self.get_setting(key)
        if setting is None or not isinstance(setting.value, str):
            raise TypeError(f"Garry's Mod {key} setting is unavailable.")
        return setting.value


def _server_config_contains_steam_account_command(content: str) -> bool:
    return any(_GMOD_STEAM_ACCOUNT_COMMAND_RE.search(line) is not None for line in content.splitlines())


def _redact_server_config_steam_account_command(content: str) -> str:
    """Redact GSLT values without interpreting the rest of a Source config line."""

    def redact_value(match: re.Match[str]) -> str:
        redacted_value = (
            f'"{_REDACTED_SECRET}"' if match.group("quoted") is not None else _REDACTED_SECRET
        )
        return f"{match.group('command')}{match.group('spacing')}{redacted_value}"

    return "".join(
        _GMOD_STEAM_ACCOUNT_VALUE_RE.sub(redact_value, line) for line in content.splitlines(keepends=True)
    )


async def prepare_gmod_server_installation(
    *,
    directory: Path,
    game_server_login_token: str | None,
) -> None:
    """Prepare a freshly downloaded GMod server before it is registered."""

    launcher = directory / "srcds_run"
    if not launcher.is_file():
        raise FileNotFoundError(f"Garry's Mod launcher is missing after SteamCMD install: {launcher}")
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

        return ["./srcds_run", "-game", "garrysmod"]

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = GMOD_MANAGE_EMBED_COLOR
        self.proc_name = "srcds_linux"
        self.proc_cmd = [self.proc_name, "-game", "garrysmod"]
        ensure_gmod_managed_files(cfg.directory)
        # App initialisation logs this value. The real command is only assembled
        # at launch, after the private token has been loaded.
        self.cmd_start = self._base_launch_command()
        self.process = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._launch_token: str | None = None
        super().__init__(bot, am, cfg, Gmod_Settings(gmod_settings_path(cfg.directory)))
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

    def _launch_command(self, token: str) -> list[str]:
        settings = self.settings
        if settings is None or not isinstance(settings.app, Gmod_Settings):
            raise RuntimeError("Garry's Mod launch settings are unavailable.")
        return gmod_start_command(
            port=self.cfg.join_port,
            max_players=settings.app.max_players,
            gamemode=settings.app.gamemode,
            startup_map=settings.app.startup_map,
            game_server_login_token=token,
        )

    def _clear_launch_token(self) -> None:
        """Remove the raw token from transient command state after a launch attempt."""

        self.cmd_start = self._base_launch_command()
        self._launch_token = None

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

    async def start(self) -> bool:
        token = _read_gmod_game_server_login_token(self.directory)
        if token is None:
            raise ValueError("Configure a Steam Game Server Login Token before starting Garry's Mod.")
        try:
            token = normalise_steam_game_server_login_token(token)
        except (TypeError, ValueError) as xcp:
            raise ValueError("The configured Steam Game Server Login Token is invalid.") from xcp

        self._launch_token = token
        self.cmd_start = self._launch_command(token)
        try:
            await self._std_launch()
        except Exception:
            try:
                await self._drain_stderr_task()
            except Exception as xcp:
                log.warning("%s stderr reader failed during launch: error_type=%s", self.name, type(xcp).__name__)
            finally:
                self._clear_launch_token()
            raise RuntimeError("Garry's Mod could not be launched.") from None
        process = self.process
        if process is None or process.stdout is None or not self.check_running():
            try:
                await self._drain_stderr_task()
            finally:
                self._clear_launch_token()
            raise RuntimeError("Garry's Mod exited before startup completed.")
        self._stdout_task = asyncio.create_task(
            self._tee(
                process.stdout,
                self.file_stdout,
                "STDOUT",
                redaction_token=token,
            )
        )
        self._running = True
        return True

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
