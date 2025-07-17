from __future__ import annotations
import asyncio
from datetime import timedelta
import json
import logging
from pathlib import Path
import re

import hikari

from config import Activity_Manager
from _discord import App_Bound, DC_Bound, DC_Relay
from apps._app import AM_Receiver, App
from apps._config import App_Config, Mod_Config
from apps._mod import Mod
from apps._rcon import RconClient
from apps._tailer import Tailer
import config


log = logging.getLogger(__name__)


ci_fmts = {"png", "jpg", "jpeg", "jfif", "gif", "ico", "bmp"}

JOIN_RE = re.compile(r"\[.*?\]:\s+[<('\"]*([^>'\"\)\(\s]+)[>'\"\)\(]* joined the game", re.IGNORECASE)
LEAVE_RE = re.compile(r"\[.*?\]:\s+[<('\"]*([^>'\"\)\(\s]+)[>'\"\)\(]* left the game", re.IGNORECASE)
DEATH_RE = re.compile(
    r"\[.*?\]: (?P<player>\S+)\s+(?P<cause>(?:drowned|was|fell|died|tried|blew|hit|walked|got|froze|burned|exploded|starved|suffocated|struck|shot|slain).+)",
    re.IGNORECASE,
)
CHAT_RE = re.compile(r"\[.*?\]: <([^>]+)>\s+(.*)")
UUID_RE = re.compile(r"UUID of player (?P<name>\w+) is (?P<uuid>[0-9a-fA-F-]{36})", re.IGNORECASE)
CICODE_RE = re.compile(r"\[\[CICode(?:,name=(?P<name>[^\],]+))?,url=(?P<url>https?://[^\s\]]+)\]\]", re.IGNORECASE)


class Mod_MC(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class Minecraft(App):
    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.proc_name = "java"
        self.proc_cmd = ["java", "nogui"]
        self.server_settings = cfg.directory.absolute() / "server.properties"
        self.cmd_start = cfg.cmd_start or ["bash", "run.sh"]
        self.process = None
        super().__init__(bot, am, cfg, Mod_MC)

        self._relay = RconClient(self.check_running, 25576)
        self._tail: Tailer | None = None
        self._tail_machers = set()
        self._players = Players(self)
        self.am_recevier = Receiver(self)
        self._activities = Activities(self)
        self._matchers = Matchers(self)

        log.debug(f"{__name__}.Created")

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        await self._std_launch()

        while not self.check_running():
            await asyncio.sleep(1)

        await self._relay.setup()

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
        await self._activities.start()
        self._running = True
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._players.stop()
        await self._activities.stop()
        await self._relay.send("save-all")
        await asyncio.sleep(0.2)
        await self._relay.send("stop")
        for _ in range(10):
            if not self.process:
                return False
            if self.process and self.process.poll() is not None:
                log.info(f"{self.friendly} stopped gracefully.")
                self.process = None
                return False
            await asyncio.sleep(0.25)
        log.warning(f"{self.friendly} did not shut down in time. Forcing termination.")
        await self._terminate()
        return True

    async def player_count(self):
        return await self._players.count()


class Receiver(AM_Receiver):
    def __init__(self, app: Minecraft) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        def wrap_ci(url: str, name: str | None = None) -> str:
            return f"[[CICode{f',name={name}' if name else ''},url={url}]]"

        urls: dict[str, str | None] = {}
        "orig: wrap"
        for link in payload.urls:
            if not link.is_media:
                continue
            if link.extension in ci_fmts:
                urls[link.orig_url or link.url] = wrap_ci(link.url, link.label)
            else:
                urls[link.orig_url or link.url] = link.url

        for file in payload.files:
            urls[wrap_ci(f"file:///{file.uri}", file.name)] = None

        log.debug(f"Receiver.{urls=} | {payload}")
        content = payload.content
        for old, new in urls.items():
            if new:
                content = content.replace(old, new)
            else:
                content = f"{content} {old}"

        colour = "white"
        json_obj = {
            "text": f"<{payload.alias}> {content} ",
            "color": colour,
        }
        txt = f"tellraw @a {json.dumps(json_obj)}\n"
        await self.app._relay.send(txt)


class Matchers:
    def __init__(self, app: Minecraft):
        self.app = app
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_death)
        app._tail_machers.add(self.match_join)
        app._tail_machers.add(self.match_left)

    @staticmethod
    def _deCICodeify(match: re.Match) -> str:
        name = match.group("name")
        url = match.group("url")
        if name:
            return f"[{name}]({url})"
        elif url:
            return url
        else:
            return ""

    async def match_chat(self, line: str):
        if match := CHAT_RE.match(line):
            player, content = match.groups()
            if content and "CICode" in content:
                content = CICODE_RE.sub(self._deCICodeify, content).strip()

            DC_Relay.add(DC_Bound(self.app, content, player))

    async def match_death(self, line: str):
        if match := DEATH_RE.match(line):
            player, content = match.groups()
            DC_Relay.add(DC_Bound(self.app, content, player))

    async def match_join(self, line: str):
        if match := JOIN_RE.match(line):
            player = match.group(1)
            DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.join, player))

    async def match_left(self, line: str):
        if match := LEAVE_RE.match(line):
            player = match.group(1)
            DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.left, player))


class Players:
    def __init__(self, app: Minecraft):
        self.app = app
        self._players_task: asyncio.Task | None = None
        self._running = False
        self._online: int | None = None
        self._max: int | None = None

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
            string = await self.app._relay.send("list")
            if not config.SILENT_DEBUG:
                log.debug(f"List Return: {string}")
            if string:
                parts = string.split(":", 1)[0].split(" ")
                if parts[2].isnumeric():
                    self._online = int(parts[2])
                if parts[7].isnumeric():
                    self._max = int(parts[7])

    async def count(self) -> tuple[int, int] | None:
        if not config.SILENT_DEBUG:
            log.debug(f"Player.count={self._online}/{self._max}")
        if self._online is not None and self._max is not None:
            return (self._online, self._max)
        return None


class Activities:
    def __init__(self, app: Minecraft):
        self.app = app
        self._time_task: asyncio.Task | None = None
        self._running = False
        self.providers = [Provider_Day(app)]
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
        for task in self.tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class Provider_Day(config.Activity_Provider):
    def __init__(self, app: Minecraft):
        self.app = app
        self._timedelta = None
        self._count = 0
        self.task_funcs = [self._get_time]
        super().__init__()

    async def get(self) -> str | None:
        return f"D{self._timedelta.days}" if self._timedelta else None

    async def _get_time(self):
        while True:
            await asyncio.sleep(10)
            text = await self.app._relay.send("time query gametime")
            if text:
                time = text.split(" ")[-1]
                self._timedelta = timedelta(seconds=int(time))


# AiviA APasz
