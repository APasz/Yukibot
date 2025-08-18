import asyncio
import logging
from pathlib import Path
import re
import subprocess

import hikari

from _discord import DC_Bound, DC_Relay
from _security import Power_Level
from apps._settings import App_Settings, Setting, Setting_Label
from apps._tailer import Tailer
from config import Activity_Manager
from apps._app import App
from apps._config import App_Config, Mod_Config
from apps._mod import Mod
import config

log = logging.getLogger(__name__)


class Mod_ETS(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class ETS_Settings(App_Settings):
    def __init__(self, pointer: Path) -> None:
        options = [
            Setting(str, Setting_Label.serv_name, "lobby_name", []),
            Setting(str, Setting_Label.serv_desc, "description", []),
            Setting(str, Setting_Label.motd, "welcome_message", []),
            Setting(str, Setting_Label.password, "password", [], power_level=Power_Level.sudo),
            Setting(int, Setting_Label.max_player, "max_players", []),
        ]
        super().__init__(pointer, options)

    def load(self):
        data = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines = data.split("\n")
        for line in lines:
            for opt in self.options:
                if line.strip().startswith(opt.key):
                    arg, val = [x.strip() for x in line.split(":", 1)]
                    opt.update(val)

    def save(self):
        data = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines = data.split("\n")
        for idx, line in enumerate(lines):
            for opt in self.options:
                if line.strip().startswith(opt.key):
                    arg, val = [x.strip() for x in line.split(":", 1)]
                    lines[idx] = f" {arg}: {opt.value}"

        string = "\n".join(lines)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class ETS(App):
    _instance = None

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.proc_name = "eurotrucks2_server"
        self.proc_cmd = [self.proc_name]
        file_settings = cfg.directory.absolute() / "home_data" / "Euro Truck Simulator 2" / "server_config.sii"
        self.cmd_start = cfg.cmd_start or ["./server_launch.sh"]
        self.cmd_cwd = cfg.directory.absolute() / "bin" / "linux_x64"

        self.process = None
        chat_channel = config.env_opt("ETS_CHAT_CHANNEL")
        if chat_channel:
            cfg.chat_channel = chat_channel
        super().__init__(bot, am, cfg, ETS_Settings(file_settings))
        self.act_err_threshold = 100

        self._tail: Tailer | None = None
        self._tail_machers = set()
        # self.am_recevier = Receiver(self)
        # self._players = Players(self)
        self._matchers = Matchers(self)

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

    async def player_count(self):
        return None  # await self._players.count()


# 00:10:13.294 : [MP] APasz connected, client_id = 10
# 00:10:13.294 : [MP] [Chat] APasz connected
# 00:12:11.720 : [MP] APasz disconnected, client_id = 10


class Matchers:
    def __init__(self, app: ETS):
        self.app = app
        # app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_transient)

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
            r"\[MP\] (?P<player>\w+) (connected|disconnected),",
            line,
            re.IGNORECASE,
        )
        if match:
            player = match.group(1)
            action = match.group(2).lower()
            txt = DC_Bound.generics.left if "disconnected" in action else DC_Bound.generics.join

            DC_Relay.add(DC_Bound(self.app, txt, player or hikari.UNDEFINED))


# AiviA APasz
