import asyncio
import logging
from pathlib import Path
import re
import tomllib
import zipfile
import tomli_w

import hikari

from _discord import AM_Receiver, App_Bound, DC_Bound, DC_Relay
from apps._settings import App_Settings, Setting, Setting_Label
from apps._tailer import Tailer
from config import Activity_Manager
import config
from apps._app import App
from apps._config import App_Config, Mod_Config
from apps._mod import Mod

log = logging.getLogger(__name__)


class Mod_BeamMP(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class BeamMP_Settings(App_Settings):
    def __init__(self, pointer: Path) -> None:
        builtin_maps = {
            "levels/gridmap_v2/info.json",
            "levels/johnson_valley/info.json",
            "levels/automation_test_track/info.json",
            "levels/east_coast_usa/info.json",
            "levels/hirochi_raceway/info.json",
            "levels/driver_training/info.json",
            "levels/west_coast_usa/info.json",
            "levels/utah/info.json",
            "levels/smallgrid/info.json",
            "levels/derby/info.json",
            "levels/small_island/info.json",
            "levels/industrial/info.json",
            "levels/jungle_rock_island/info.json",
            "levels/italy/info.json",
        }

        def normalize_member(p: str) -> str | None:
            p = p.lstrip("/").lower()
            if not (p.startswith("levels/") and p.endswith("/info.json")):
                return None
            return p

        def find_levels(levels: set[str] | None = None) -> list[str]:
            found: set[str] = set(levels or set())
            mods_dir = pointer.parent / "Resources" / "Client"
            if not mods_dir.is_dir():
                return sorted(found)

            for file in mods_dir.glob("*.zip"):
                try:
                    with zipfile.ZipFile(file, "r") as zf:
                        for name in zf.namelist():
                            norm = normalize_member(name)
                            if norm:
                                found.add(norm)
                except zipfile.BadZipFile:
                    log.error(f"BadZip @ {file}")
                    continue
            return sorted(found)

        def extract_map_name(name: str) -> str | None:
            name = name.strip().lstrip("/").lower()
            if not (name.startswith("levels/") and name.endswith("/info.json")):
                return None
            core = name.removeprefix("levels/").removesuffix("/info.json")

            acronyms = {"usa", "us", "jp", "au", "uk", "eu", "cn", "ru", "kr", "fr", "it", "de"}
            words = core.split("_")
            pretty_words = [w.upper() if w in acronyms else w.title() for w in words]
            return " ".join(pretty_words)

        all_levels = find_levels(builtin_maps)

        map_choices = {name: f"/{lvl}" for lvl in all_levels if (name := extract_map_name(lvl))}

        options = [
            Setting(str, Setting_Label.serv_name, "Name", ["General"]),
            Setting(str, Setting_Label.serv_desc, "Description", ["General"]),
            Setting(int, Setting_Label.max_player, "MaxPlayers", ["General"]),
            Setting(int, "Max Cars", "MaxCars", ["General"]),
            Setting(
                bool,
                Setting_Label.visibility,
                "Private",
                ["General"],
                choices={"Public": "false", "Private": "true"},
            ),
            Setting(bool, "Allow Guests", "AllowGuests", ["General"]),
            Setting(str, Setting_Label.map_name, "Map", ["General"], choices=map_choices),
        ]
        super().__init__(pointer, options)

    def load(self):
        data = tomllib.loads(self.pointer.read_text(config.STR_ENCODE))
        if not isinstance(data, dict):
            raise ValueError(f"config must be dict not `{type(data)}`")

        for opt in self.options:
            opt.get(data)

    def save(self):
        data = tomllib.loads(self.pointer.read_text(config.STR_ENCODE))
        if not isinstance(data, dict):
            raise ValueError(f"config must be dict not `{type(data)}`")

        for opt in self.options:
            opt.set(data)

        string = tomli_w.dumps(data)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class BeamMP(App):
    _instance = None

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.process = None
        self.proc_name = "BeamMP-Server"
        self.proc_cmd = ["script", "-qfc", self.proc_name, "/dev/null"]
        self.cmd_start = cfg.cmd_start or [
            "./BeamMP-Server",
        ]
        file_settings = cfg.directory.absolute() / "ServerConfig.toml"
        super().__init__(bot, am, cfg, BeamMP_Settings(file_settings), Mod_BeamMP)
        self.act_err_threshold = 100

        self.cur_player: int = 0

        self._tail: Tailer | None = None
        self._tail_machers = set()
        self.am_recevier = Receiver(self)
        self._players = Players(self)
        self._matchers = Matchers(self)

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        await self._std_launch()
        while not self.check_running():
            await asyncio.sleep(1)

        if self.process and self.process.stdout:
            log.debug(f"{self.name} Tailing: Process")
            self._tail = Tailer(self.check_running, self.process.stdout, self.file_stdout)
        elif self.server_log:
            log.debug(f"{self.name} Tailing: server log")
            self._tail = Tailer(self.check_running, self.server_log, self.file_stdout)
        else:
            raise SystemError("No Log to be passed to Tailer")
        await self._tail.start(self._tail_machers)

        await self._players.start()

        self._running = True
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False

        await self._players.stop()

        if self.process and self.process.stdin:
            self.process.stdin.write("exit\n")
            self.process.stdin.flush()

        if self._tail:
            await self._tail.stop()
        await self._terminate()
        return True

    async def player_count(self):
        return await self._players.count()


class Matchers:
    def __init__(self, app: BeamMP):
        self.app = app
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_transient)
        app._tail_machers.add(self.match_player_count)

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
        match = re.search(
            r"\[.*?\] \[INFO\] ([^\s]+) (is now synced!|Connection Terminated)",
            line,
            re.IGNORECASE,
        )
        if match:
            player = match.group(1)
            action = match.group(2).lower()
            txt = DC_Bound.generics.join if "synced" in action else DC_Bound.generics.left

            DC_Relay.add(DC_Bound(self.app, txt, player or hikari.UNDEFINED))

    async def match_player_count(self, line: str):
        match = re.search(r"Total Players:\s+(\d+)", line)
        log.debug(
            f"MATCH_PLAYER: {self.app.cur_player=} | {self.app.settings.app.max_player if self.app.settings else None} | {match=}"
        )
        if match:
            self.app.cur_player = int(match.group(1))
        return None


class Receiver(AM_Receiver):
    def __init__(self, app: BeamMP) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        if not config.SILENT_DEBUG:
            log.debug(f"Saying from {payload.alias}: {payload.content}")
        if self.app.process and self.app.process.stdin:
            self.app.process.stdin.write(f"say {payload.alias}: {payload.content}\n")
            self.app.process.stdin.flush()
        else:
            log.error("Unable to say")


class Players:
    def __init__(self, app: "BeamMP"):
        self.app = app
        self._players_task: asyncio.Task | None = None
        self._running = False
        self._max: int | None = self.app.settings.app.max_player if self.app.settings else None
        self._online: int | None = None

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

    async def _listplayers(self):
        while self._running:
            await asyncio.sleep(4)
            if self.app.process and self.app.process.stdin:
                self.app.process.stdin.write("status\n")
                self.app.process.stdin.flush()
            await asyncio.sleep(1)
            if isinstance(self.app.cur_player, int):
                self._online = self.app.cur_player

    async def count(self) -> tuple[int, int] | None:
        if self.app.settings:
            self._max = self.app.settings.app.max_player
        if not config.SILENT_DEBUG:
            log.debug(f"Player.count={self._online}/{self._max}")
        if self._online is not None and self._max is not None:
            return (self._online, self._max)
        return None


# AiviA APasz
