import ast
import asyncio
from datetime import datetime
import logging
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import hikari

from config import Activity_Manager
from _discord import App_Bound, DC_Bound, DC_Relay
from _file import File_Utils
from apps._app import AM_Receiver, App
from apps._config import App_Config, Mod_Config
from apps._mod import Mod

from apps._tailer import Tailer
from apps._telnet import TelnetClient

import config


log = logging.getLogger(__name__)


class Mod_7D2D(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_extr(src, atomic)


class SevenDays(App):
    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.proc_name = "7daystodie"
        self.proc_cmd = ["7DaysToDieServer", "-nographics"]
        self.server_settings = cfg.directory.absolute() / "serverconfig.xml"
        self.cmd_start = cfg.cmd_start or ["bash", "startserver.sh", f"-configfile={self.server_settings.name}"]
        self.process = None
        super().__init__(bot, am, cfg, Mod_7D2D)
        self.act_err_threshold = 100

        self._relay = TelnetClient(self.check_running, 8081)
        self._tail: Tailer | None = None
        self._tail_matchers = set()
        self.am_recevier = Receiver(self)
        self._players = Players(self)
        self._activities = Activities(self)
        self._matchers = Matchers(self)

        try:
            server_name = next(
                (
                    p.attrib["value"]
                    for p in ET.parse(self.server_settings).getroot().findall("property")
                    if p.attrib.get("name") == "ServerName"
                ),
                None,
            )

            if server_name:
                self.cfg.provider_alt_text = server_name
        except Exception:
            log.exception(f"{__name__} Read Settings")

        log.debug(f"{__name__}.Created")

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
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

        self._tail = Tailer(lambda: self._relay.connected_event, reader, self.file_stdout)  # type: ignore
        await self._tail.start(self._tail_matchers)
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

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


class Receiver(AM_Receiver):
    def __init__(self, app: SevenDays) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        txt = f'say "{payload.alias}: {payload.content_demojised}"'
        await self.app._relay.send(txt)


class Matchers:
    def __init__(self, app: SevenDays):
        self.app = app
        self._last_telnet = datetime.now()
        app._tail_matchers.add(self.match_transiant)
        app._tail_matchers.add(self.match_chat)

    async def match_transiant(self, line: str):
        match = re.search(r"GMSG: Player '(.+?)' (joined|left) the game", line, re.IGNORECASE)
        if match:
            player = match.group(1)
            action = str(match.group(2)).lower()
            txt = DC_Bound.generics.join if "join" in action else DC_Bound.generics.left

            DC_Relay.add(DC_Bound(self.app, txt, player or hikari.UNDEFINED))

    async def match_chat(self, line: str):
        match = re.search(r"Chat.*?:\s*'(.*?)':\s*(.+)", line, re.IGNORECASE)
        player = None
        if match:
            player = str(match.group(1)).strip("\r\n ")
            msg = str(match.group(2)).strip("\r\n ")
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player or hikari.UNDEFINED))


class Players:
    def __init__(self, app: SevenDays):
        self.app = app
        self._players_task: asyncio.Task | None = None
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
        self._time_task: asyncio.Task | None = None
        self._running = False
        self.providers = [Provider_Time(app)]
        self.tasks = set()

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
        self.stats: dict[str, int | float | str | bool | None] = {}
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
        key, val = stat.split("=")
        if val:
            val = ast.literal_eval(val)
        else:
            val = None

        self.stats[key] = val
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Stats: {key}={self.stats[key]}")


# AiviA APasz
