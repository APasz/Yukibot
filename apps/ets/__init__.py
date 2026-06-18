import asyncio
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from re import Match

import hikari

import config
from _discord import DC_Bound, DC_Relay
from _security import Power_Level
from apps._app import App
from apps._config import App_Config, AppVersion, Mod_Config
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._mod import Mod
from apps._settings import App_Settings, IntSettingSpec, Setting, Setting_Label, StringSettingSpec
from apps._tailer import Tailer
from config import Activity_Manager
from relay_notices import (
    PlayerSessionAction,
    PlayerSessionNotice,
    RelayNoticeSource,
    render_notice_text,
)

log = logging.getLogger(__name__)

_ETS_GAME_VERSION_RE = re.compile(r"\[MP\]\s+Game version:\s*(?P<version>\S+)", re.IGNORECASE)
_ETS_PACKSET_VERSION_RE = re.compile(r"Loaded pack set version\s+(?P<version>\S+)", re.IGNORECASE)


def _candidate_ets_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates = [
        server_log,
        directory / "home_data" / "Euro Truck Simulator 2" / "server.log.txt",
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


class ETS_Settings(App_Settings):
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
        options = [
            Setting[str](
                StringSettingSpec(allow_blank=True),
                Setting_Label.serv_name,
                "lobby_name",
                [],
                default="",
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                Setting_Label.serv_desc,
                "description",
                [],
                default="",
                paragraph=True,
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                Setting_Label.motd,
                "welcome_message",
                [],
                default="",
                paragraph=True,
            ),
            Setting[str](
                StringSettingSpec(
                    allow_blank=True,
                    is_sensitive=True,
                    do_hide=Power_Level.user,
                ),
                Setting_Label.password,
                "password",
                [],
                default="",
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                Setting_Label.max_player,
                "max_players",
                [],
                default=8,
                power_level=Power_Level.sudo,
            ),
        ]
        super().__init__(pointer, options, version_getter=version_getter)

    def load(self):
        data = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines = data.split("\n")
        for line in lines:
            for opt in self.options:
                if line.strip().startswith(opt.key):
                    arg, val = [x.strip() for x in line.split(":", 1)]
                    opt.load_value(val)

    def save(self):
        data = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines = data.split("\n")
        for idx, line in enumerate(lines):
            for opt in self.options:
                if line.strip().startswith(opt.key):
                    arg, val = [x.strip() for x in line.split(":", 1)]
                    lines[idx] = f" {arg}: {opt.serialise_value()}"

        string = "\n".join(lines)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class ETS(App):
    _instance: None = None
    chat_relay_outbound = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0x2563EB
        self.proc_name = "eurotrucks2_server"
        self.proc_cmd = [self.proc_name]
        file_settings: Path = cfg.directory.absolute() / "home_data" / "Euro Truck Simulator 2" / "server_config.sii"
        self.cmd_start = cfg.cmd_start or ["./server_launch.sh"]
        self.cmd_cwd = cfg.directory.absolute() / "bin" / "linux_x64"

        self.process = None
        super().__init__(bot, am, cfg, ETS_Settings(file_settings, version_getter=lambda: cfg.version))
        self.act_err_threshold = 100
        self.apply_version(
            detect_ets_version(directory=cfg.directory, server_log=cfg.server_log_file),
            persist=False,
        )

        self._tail: Tailer | None = None
        self._tail_machers = set()
        # self.am_recevier = Receiver(self)
        # self._players = Players(self)
        self._matchers = Matchers(self)

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Config",
                path=self.directory / "home_data" / "Euro Truck Simulator 2" / "server_config.sii",
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".sii"}),
            ),
        )

        self.shell = True

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
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

        subprocess.run(["pkill", "-f", self.proc_name])
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

    async def player_count(self):
        return None  # await self._players.count()


# 00:10:13.294 : [MP] APasz connected, client_id = 10
# 00:10:13.294 : [MP] [Chat] APasz connected
# 00:12:11.720 : [MP] APasz disconnected, client_id = 10


class Matchers:
    def __init__(self, app: ETS):
        self.app = app
        app._tail_machers.add(self.match_version)
        # app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_transient)

    async def match_version(self, line: str) -> None:
        if match := _ETS_GAME_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)
            return
        if match := _ETS_PACKSET_VERSION_RE.search(line):
            self.app.apply_version(match.group("version"), persist=True)

    async def match_chat(self, line: str):
        match = re.search(r"\[.*?\] \[CHAT\] \(\d+\) <([^>]+)> +(.+)", line, re.IGNORECASE)
        player = None
        if match:
            player = str(match.group(1))
            msg = str(match.group(2))
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player))

    async def match_transient(self, line: str):
        match: Match[str] | None = re.search(
            r"\[MP\] (?P<player>\w+) (connected|disconnected),",
            line,
            re.IGNORECASE,
        )
        if match:
            player: str = str(match.group(1))
            action: str = str(match.group(2)).lower()
            notice = PlayerSessionNotice(
                action=PlayerSessionAction.LEFT if "disconnected" in action else PlayerSessionAction.JOINED,
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


# AiviA APasz
