import asyncio
import json
import logging
import re
from pathlib import Path
import tarfile

import aiohttp
import hikari

from _security import Power_Level
from apps._settings import App_Settings, Setting, Setting_Label
from config import Activity_Manager
from apps._updater import Update_Manager
import config
from _discord import App_Bound, DC_Bound, DC_Relay
from _file import File_Utils
from apps._app import AM_Receiver, App
from apps._config import App_Config, Mod_Config
from apps._mod import Mod
from apps._rcon import RconClient
from apps._tailer import Tailer

log = logging.getLogger(__name__)


class Mod_Factorio(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class Factorio_Settings(App_Settings):
    def __init__(self, pointer: Path) -> None:
        options = [
            Setting(str, Setting_Label.serv_name, "name", []),
            Setting(str, Setting_Label.serv_desc, "description", []),
            Setting(int, Setting_Label.max_player, "max_players", []),
            Setting(
                bool, Setting_Label.visibility, "public", ["visibility"], choices={"Public": "true", "Private": "false"}
            ),
            Setting(str, Setting_Label.password, "game_password", [], power_level=Power_Level.sudo),
        ]
        super().__init__(pointer, options)

    def load(self):
        data = json.loads(self.pointer.read_text(config.STR_ENCODE))
        if not isinstance(data, dict):
            raise ValueError(f"config must be dict not `{type(data)}`")

        for opt in self.options:
            opt.get(data)

    def save(self):
        data = json.loads(self.pointer.read_text(config.STR_ENCODE))
        if not isinstance(data, dict):
            raise ValueError(f"config must be dict not `{type(data)}`")

        for opt in self.options:
            opt.set(data)

        string = json.dumps(data, indent=4)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class Factorio(App):
    _instance = None

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.proc_name = "factorio"
        self.proc_cmd = [self.proc_name, "--start-server"]
        file_settings = cfg.directory.absolute() / "data" / "server-settings.json"
        self.cmd_start = cfg.cmd_start or [
            "bin/x64/factorio",
            "--start-server-load-latest",
            "--server-settings",
            f"{file_settings}",
            "--rcon-port",
            "27015",
            "--rcon-password",
            f"{config.env_req('APP_COMM_PASS')}",
        ]

        self.process = None
        chat_channel = config.env_opt("FACTORIO_CHAT_CHANNEL")
        if chat_channel:
            cfg.chat_channel = chat_channel
        super().__init__(bot, am, cfg, Factorio_Settings(file_settings), Mod_Factorio)
        self.act_err_threshold = 100
        self._lock = self.directory / ".lock"

        self.updater = Factorio_Updater(self, base=config.INDEV)

        self._relay = RconClient(self.check_running, 27015)
        self._tail: Tailer | None = None
        self._tail_machers = set()
        self._players = Players(self)
        self.am_recevier = Receiver(self)
        self._matchers = Matchers(self)

        try:
            settings: dict[str, str | int | bool | list[str] | dict[str, str | int | bool]] = json.loads(
                file_settings.read_text(config.STR_ENCODE)
            )
            if serv_name := settings.get("name"):
                self.cfg.provider_alt_text = str(serv_name)
        except Exception:
            log.exception(f"{__name__} Read Settings")

        log.debug(f"{__name__}.Created")

    async def start(self) -> bool:
        log.info(f"{__name__}.start")

        for item in (self.directory / "saves").iterdir():
            if item.is_dir():
                continue
            if not item.name.endswith("tmp.zip"):
                continue
            File_Utils.remove(item, silent=True, resolve=True)

        wait_count = 10
        while self._lock.exists() and wait_count >= 0:
            wait_count -= 1
            await asyncio.sleep(1)

        await self._std_launch()

        while not self.check_running():
            await asyncio.sleep(1)

        await self._relay.setup()

        if self.server_log and self.server_log.exists():
            File_Utils.link(self.server_log, self.file_stdout.with_name(self.server_log.name))

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
        ok = await self._relay.send("/server-save")
        if not ok:
            if self.process and self.process.stdin:
                log.debug("Falling back to stdin")
                self.process.stdin.write("/server-save")
                self.process.stdin.flush()
        await self._players.stop()
        if self._tail:
            await self._tail.stop()
        await self._relay.teardown()
        await self._terminate()
        await asyncio.sleep(0.5)
        File_Utils.remove(self._lock, silent=True, resolve=True)  # Sometimes it doesn't get removed
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


class Factorio_Updater(Update_Manager):
    def __init__(self, app: Factorio, *, base: bool = False, mods: bool = False) -> None:
        super().__init__(app, base=base, mods=mods)
        log_file = app.directory / "factorio-current.log"
        self.version: None | tuple[int, ...] = None
        if log_file.exists():
            version_re = re.compile(r"Factorio (\d+\.\d+\.\d+)")
            for line in log_file.read_text(config.STR_ENCODE).splitlines():
                if "Factorio" not in line:
                    continue
                if ver := self.extract_version(line, version_re):
                    self.version = ver
                    break
        if self.version:
            log.info(f"Factorio local version: {self.stringise(self.version)}")
        else:
            log.warning(f"Could not determine Factorio version: {log_file}")

    @staticmethod
    def extract_archive(src: Path, dst: Path) -> bool:
        try:
            with tarfile.open(src, mode="r:xz") as archive:
                archive.extractall(path=dst)
            return True
        except Exception as e:
            log.exception(f"Extraction error: {e}")
            return False

    @staticmethod
    async def download(pointer: Path, version: str) -> Path | None:
        url = f"https://www.factorio.com/get-download/{version}/headless/linux64"
        archive_path = pointer / f"factorio-{version}.tar.xz"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        log.warning(f"Download failed: HTTP {resp.status}")
                        return None
                    with open(archive_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)

            return archive_path
        except Exception as xcp:
            log.exception(f"Download error: {xcp}")
            return None

    @staticmethod
    async def fetch_version() -> tuple[int, ...] | None:
        url = "https://factorio.com/api/latest-releases"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        log.warning(f"Failed to fetch latest version: HTTP {response.status}")
                        return None
                    data = await response.json()
                    log.debug(f"{data=}")
                    string = data["stable"]["headless"]
                    return tuple(map(int, string.split(".")))
        except Exception as xcp:
            log.exception(f"Error fetching latest version: {xcp}")
            return None

    async def base(self) -> str | None:
        await super().base()

        latest = await self.fetch_version()
        if not latest:
            return None

        if self.version:
            if latest <= self.version:
                return None
            log.info(f"Latest stable version: {self.stringise(latest)}")

        ver_str = self.stringise(latest)
        pointer = config.DIR_TMP / "factorio"
        pointer = await self.download(pointer, ver_str)
        if not pointer:
            raise FileNotFoundError("The download has gone walkabouts")

    async def mods(self) -> list[str] | None:
        await super().mods()


class Receiver(AM_Receiver):
    def __init__(self, app: Factorio) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        txt = f'/silent-command game.print("{payload.alias}: {payload.content}")'
        await self.app._relay.send(txt)


class Matchers:
    def __init__(self, app: Factorio):
        self.app = app
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_death)

    async def match_chat(self, line: str):
        match = re.search(r"\[CHAT\] (.*?): (.+)", line, re.IGNORECASE)
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Chat: {line=} | {match=}")
        player = None
        if match:
            player = str(match.group(1))
            msg = str(match.group(2))
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player))

    async def match_death(self, line: str):
        match = re.search(r"\[DIED\]\s+(\w+):(\S+)\s+(.+)", line, re.IGNORECASE)
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Death: {line=} | {match=}")
        player = None
        if match:
            mode, player, cause = match.groups()
            log.debug(f"Match_Death: {player=} | {cause=} | {mode=}")
            fmt = {"cause": str(cause).replace("-", " ").title()}
            if cause and mode == "PVE":
                DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.died_pve, player, extra_fmt=fmt))
            elif cause and mode == "PVP":
                DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.died_pvp, player, extra_fmt=fmt))
            elif cause:
                DC_Relay.add(DC_Bound(self.app, cause, player))


class Players:
    def __init__(self, app: "Factorio"):
        self.app = app
        self._players_task: asyncio.Task | None = None
        self._running = False
        self._online: int | None = None
        self._max: int | None = None
        self._players: set[str] = set()

    async def start(self):
        self._online = None
        self._max = None
        self._players = set()
        if self._players_task and not self._players_task.done():
            return
        self._running = True
        self._players_task = asyncio.create_task(self._listplayers())

    async def stop(self):
        self._online = None
        self._max = None
        self._players = set()
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
            await asyncio.sleep(1)
            log.debug(f"Players.PRE {self._online}/{self._max} | {self._players}")
            _max = await self.app._relay.send("/config get max-players")
            if _max:
                self._max = int(_max) or -1
                log.debug(f"Players.{self._max=}")
            string = await self.app._relay.send("/players online")
            if string:

                def find_players(x: str) -> tuple[int, set[str]]:
                    lines = [line.strip() for line in x.split("\n") if line]
                    count = len(lines) - 1
                    players = set(name.rsplit(" ", 1)[0] for name in lines[1:])
                    return count, players

                self._online, players = find_players(string)

                def is_join(new: set) -> tuple[set, set]:
                    join = new.difference(self._players)
                    leave = self._players.difference(new)
                    return join, leave

                joins, leaves = is_join(players)

                for player in leaves:
                    DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.left, player))
                    self._players.discard(player)
                    log.debug(f"Players.discard.{self._players=}")
                for player in joins:
                    DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.join, player))
                    self._players.add(player)
                    log.debug(f"Players.add.{self._players=}")

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


# AiviA APasz
