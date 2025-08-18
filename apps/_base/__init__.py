import asyncio
import logging
from pathlib import Path

import hikari

from apps._settings import App_Settings, Setting
from config import Activity_Manager
from apps._app import App
from apps._config import App_Config, Mod_Config
from apps._mod import Mod
import config

log = logging.getLogger(__name__)


class Mod_Base(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class Base_Settings(App_Settings):
    def __init__(self, pointer: Path) -> None:
        options = [Setting(str, "", "", [])]
        super().__init__(pointer, options)

    def load(self):
        return None

    def save(self):
        return {}


class Base(App):
    _instance = None

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.proc_name = ""
        self.proc_cmd = [""]
        file_settings = cfg.directory.absolute() / "server.properties"
        self.cmd_start = cfg.cmd_start or [
            "",
        ]

        self.process = None
        chat_channel = config.env_opt("BASE_CHAT_CHANNEL")
        if chat_channel:
            cfg.chat_channel = chat_channel
        super().__init__(bot, am, cfg, Base_Settings(Path()), Mod_Base)
        self.act_err_threshold = 100

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        await self._std_launch()
        while not self.check_running():
            await asyncio.sleep(1)
        self._running = True
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._terminate()
        return True

    async def player_count(self):
        return None  # await self._players.count()


# AiviA APasz
