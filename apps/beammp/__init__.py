import asyncio
import logging
import re
import tomllib
import zipfile
from logging import Logger
from pathlib import Path
from re import Match
from typing import Any, Literal

import hikari
import tomli_w

import config
from _discord import (
    AM_Receiver,
    App_Bound,
    DC_Bound,
    DC_Relay,
    Generics,
    OutboundRelayFormatter,
    RelayOutboundFormatOptions,
    render_plain_reference_prefix,
)
from _security import Power_Level
from apps._app import App
from apps._config import App_Config, Mod_Config
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._mod import Mod
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Setting_Label,
    StringSettingSpec,
)
from apps._tailer import Tailer
from config import Activity_Manager

log: Logger = logging.getLogger(__name__)

class Mod_BeamMP(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class BeamMP_Settings(App_Settings):
    def __init__(self, pointer: Path) -> None:
        builtin_maps: set[str] = {
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

        def normalise_member(p: str) -> str | None:
            p = p.lstrip("/").lower()
            if not (p.startswith("levels/") and p.endswith("/info.json")):
                return None
            return p

        def find_levels(levels: set[str] | None = None) -> list[str]:
            found: set[str] = set[str](levels or set[str]())
            mods_dir: Path = pointer.parent / "Resources" / "Client"
            if not mods_dir.is_dir():
                return sorted(found)

            for file in mods_dir.glob("*.zip"):
                try:
                    with zipfile.ZipFile(file, "r") as zf:
                        for name in zf.namelist():
                            norm: str | None = normalise_member(name)
                            if norm:
                                found.add(norm)
                except zipfile.BadZipFile:
                    log.error(f"BadZip @ {file}")
                    continue
            return sorted(found)

        def extract_map_name(name: str) -> str | None:
            mapname: str = name.strip().lstrip("/").lower()
            if not (mapname.startswith("levels/") and mapname.endswith("/info.json")):
                return None
            core: str = mapname.removeprefix("levels/").removesuffix("/info.json")

            acronyms: set[str] = {"usa", "us", "jp", "au", "uk", "eu", "cn", "ru", "kr", "fr", "it", "de"}
            words: list[str] = core.split("_")
            pretty_words: list[str] = [w.upper() if w in acronyms else w.title() for w in words]
            return " ".join(pretty_words)

        all_levels: list[str] = find_levels(builtin_maps)

        map_choices: ChoiceSpec = ChoiceSpec(
            *(ChoiceOption(f"/{lvl}", name) for lvl in all_levels if (name := extract_map_name(lvl)))
        )

        options = [
            Setting[str](
                StringSettingSpec(allow_blank=True),
                Setting_Label.serv_name,
                "Name",
                ["General"],
                default="",
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                Setting_Label.serv_desc,
                "Description",
                ["General"],
                default="",
            ),
            Setting[int](
                IntSettingSpec(),
                Setting_Label.max_player,
                "MaxPlayers",
                ["General"],
                default=8,
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                "Max Cars",
                "MaxCars",
                ["General"],
                default=1,
            ),
            Setting[bool](
                BoolSettingSpec(),
                Setting_Label.visibility,
                "Private",
                ["General"],
                default=True,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Allow Guests",
                "AllowGuests",
                ["General"],
                default=True,
            ),
            Setting[str](
                StringSettingSpec(map_choices),
                Setting_Label.map_name,
                "Map",
                ["General"],
                default="/levels/gridmap_v2/info.json",
                power_level=Power_Level.guest,
            ),
        ]
        super().__init__(pointer, options)

    def load(self) -> None:
        data: dict[str, object] = tomllib.loads(self.pointer.read_text(config.STR_ENCODE))
        for opt in self.options:
            opt.get(data)

    def save(self) -> dict[str, object]:
        data: dict[str, object] = tomllib.loads(self.pointer.read_text(config.STR_ENCODE))
        for opt in self.options:
            opt.set(data)

        string: str = tomli_w.dumps(data)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class BeamMP(App[App_Config]):
    _instance: None = None
    chat_relay_outbound = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0xF97316
        self.process = None
        self.proc_name = "BeamMP-Server"
        self.proc_cmd = ["script", "-qfc", self.proc_name, "/dev/null"]
        self.cmd_start = cfg.cmd_start or [
            "./BeamMP-Server",
        ]
        file_settings: Path = cfg.directory.absolute() / "ServerConfig.toml"
        super().__init__(bot, am, cfg, BeamMP_Settings(file_settings), Mod_BeamMP)
        self.act_err_threshold = 100

        self.cur_player: int = 0

        self._tail: Tailer | None = None
        self._tail_machers: set[Any] = set[Any]()
        self.am_receiver = Receiver(self)
        self._players: Players = Players(self)
        self._matchers: Matchers = Matchers(self)

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Config",
                path=self.directory / "ServerConfig.toml",
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".toml"}),
            ),
        )

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

    async def kill(self) -> bool:
        self._running = False
        await self._players.stop()
        if self._tail:
            await self._tail.stop()
        await self._terminate()
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()


class Matchers:
    def __init__(self, app: BeamMP) -> None:
        self.app = app
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_transient)
        app._tail_machers.add(self.match_player_count)

    async def match_chat(self, line: str) -> None:
        match: Match[str] | None = re.search(r"\[.*?\] \[CHAT\] \(\d+\) <([^>]+)> +(.+)", line, re.IGNORECASE)
        player: str | None = None
        if match:
            player = str(match.group(1))
            msg = str(match.group(2))
            log.debug(f"Match_Chat: {player=} | {msg=}")
            if msg and not msg.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(DC_Bound(self.app, msg, player))

    async def match_transient(self, line: str) -> None:
        match: Match[str] | None = re.search(
            r"\[.*?\] \[INFO\] ([^\s]+) (is now synced!|Connection Terminated)",
            line,
            re.IGNORECASE,
        )
        if match:
            player: str = match.group(1)
            action: str = match.group(2).lower()
            txt: Literal[Generics.join, Generics.left] = (
                DC_Bound.generics.join if "synced" in action else DC_Bound.generics.left
            )

            DC_Relay.add(DC_Bound(self.app, txt, player or hikari.UNDEFINED))

    async def match_player_count(self, line: str):
        match: Match[str] | None = re.search(r"Total Players:\s+(\d+)", line)
        log.debug(
            f"MATCH_PLAYER: {self.app.cur_player=} | {self.app.settings.app.max_player if self.app.settings else None} | {match=}"
        )
        if match:
            self.app.cur_player = int(match.group(1))
        return None


class Receiver(AM_Receiver):
    def __init__(self, app: BeamMP) -> None:
        super().__init__()
        self.app: BeamMP = app

    async def send(self, payload: App_Bound) -> None:
        base_content: str = (
            payload.content_for_app(self.app) if hasattr(payload, "content_for_app") else payload.content
        )
        content: str = OutboundRelayFormatter.format_payload(
            payload,
            RelayOutboundFormatOptions(
                base_content=base_content,
                reference_renderer=render_plain_reference_prefix,
            ),
        )
        if not config.SILENT_DEBUG:
            log.debug(f"Saying from {payload.alias}: {content}")
        if self.app.process and self.app.process.stdin:
            self.app.process.stdin.write(f"say {payload.alias}: {content}\n")
            self.app.process.stdin.flush()
        else:
            log.error("Unable to say")


class Players:
    def __init__(self, app: "BeamMP") -> None:
        self.app: BeamMP = app
        self._players_task: asyncio.Task[None] | None = None
        self._running = False
        self._max: int | None = self.app.settings.app.max_player if self.app.settings else None
        self._online: int | None = None

    async def start(self) -> None:
        self._online = None
        self._max = None
        if self._players_task and not self._players_task.done():
            return
        self._running = True
        self._players_task = asyncio.create_task(self._listplayers())

    async def stop(self) -> None:
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

    async def _listplayers(self) -> None:
        while self._running:
            await asyncio.sleep(4)
            if self.app.process and self.app.process.stdin:
                self.app.process.stdin.write("status\n")
                self.app.process.stdin.flush()
            await asyncio.sleep(1)
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
