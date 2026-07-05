import asyncio
import json
import logging
import re
import tomllib
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from logging import Logger
from pathlib import Path
from re import Match
from typing import Any, cast

import hikari
import tomli_w

import config
from _discord import (
    AM_Receiver,
    App_Bound,
    DC_Bound,
    DC_Relay,
    OutboundRelayFormatter,
    RelayOutboundFormatOptions,
    render_plain_reference_prefix,
)
from _security import Power_Level
from apps._app import App
from apps._config import App_Config, AppVersion, Mod_Config, ModPageLink
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._mod import Mod, humanise_mod_identifier
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
from relay_notices import (
    PlayerSessionAction,
    RelayNoticeSource,
    render_notice_text,
)

log: Logger = logging.getLogger(__name__)

_BEAMMP_METADATA_MAX_BYTES = 1_048_576
_BEAMMP_MOD_VERSION_RE = re.compile(
    r"(?:^|[-_. ])v?(?P<version>\d+(?:\.\d+)+(?:[-+._][A-Za-z0-9]+)*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BeamMpModMetadata:
    native_id: str | None = None
    title: str | None = None
    version: str | None = None
    homepage: str | None = None


def _beammp_metadata_text(payload: Mapping[str, object], *field_names: str) -> str | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text:
            return text
    return None


def _beammp_metadata_version(payload: Mapping[str, object], *field_names: str) -> str | None:
    text = _beammp_metadata_text(payload, *field_names)
    if text is not None:
        return text
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        return str(value)
    return None


def _beammp_metadata_mapping(raw: bytes) -> Mapping[str, object] | None:
    try:
        payload = cast(object, json.loads(raw.decode("utf-8-sig")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    mapping = cast(Mapping[object, object], payload)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)


def _beammp_metadata_from_mapping(payload: Mapping[str, object]) -> BeamMpModMetadata:
    resource_id = payload.get("resource_id")
    native_id = (
        str(resource_id)
        if isinstance(resource_id, int) and not isinstance(resource_id, bool) and resource_id > 0
        else _beammp_metadata_text(payload, "id", "mod_id")
    )
    return BeamMpModMetadata(
        native_id=native_id,
        title=_beammp_metadata_text(payload, "title", "Name", "name"),
        version=_beammp_metadata_version(payload, "version_string", "version", "Version"),
        homepage=_beammp_metadata_text(payload, "homepage", "resource_url", "url"),
    )


def _beammp_mod_metadata(pointer: Path) -> BeamMpModMetadata | None:
    if not pointer.is_file() or pointer.suffix.casefold() != ".zip":
        return None
    try:
        with zipfile.ZipFile(pointer, "r") as archive:
            package_entries: list[zipfile.ZipInfo] = []
            content_entries: list[zipfile.ZipInfo] = []
            for member in archive.infolist():
                if member.is_dir() or member.file_size > _BEAMMP_METADATA_MAX_BYTES:
                    continue
                parts = tuple(part for part in member.filename.replace("\\", "/").split("/") if part)
                folded_parts = tuple(part.casefold() for part in parts)
                if len(parts) >= 2 and folded_parts[0] == "mod_info" and folded_parts[-1] == "info.json":
                    package_entries.append(member)
                elif (
                    len(parts) == 3
                    and folded_parts[0] in {"levels", "vehicles"}
                    and folded_parts[-1] == "info.json"
                ):
                    content_entries.append(member)

            candidates = package_entries or (content_entries if len(content_entries) == 1 else [])
            for member in candidates:
                payload = _beammp_metadata_mapping(archive.read(member))
                if payload is None:
                    continue
                metadata = _beammp_metadata_from_mapping(payload)
                if metadata.native_id is None and len(parts := tuple(
                    part for part in member.filename.replace("\\", "/").split("/") if part
                )) >= 2:
                    metadata = replace(metadata, native_id=parts[1])
                if any((metadata.native_id, metadata.title, metadata.version, metadata.homepage)):
                    return metadata
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return None
    return None


def _beammp_filename_version(name: str) -> str | None:
    match = _BEAMMP_MOD_VERSION_RE.search(Path(name).stem)
    return None if match is None else match.group("version").removeprefix("v")


def _beammp_mod_page(raw_url: str | None) -> ModPageLink | None:
    if raw_url is None:
        return None
    try:
        return ModPageLink(name="Homepage", url=raw_url)
    except ValueError:
        return None


class Mod_BeamMP(Mod):
    def __init__(self, cfg: Mod_Config):
        self._detected_metadata = BeamMpModMetadata()
        super().__init__(cfg)

    def sync_metadata(self) -> None:
        self.sync_enabled_state()
        self._detected_metadata = _beammp_mod_metadata(self.path) or BeamMpModMetadata()
        super().sync_metadata()
        detected_page = _beammp_mod_page(self._detected_metadata.homepage)
        if detected_page is not None and all(page.url != detected_page.url for page in self.cfg.mod_pages):
            self.cfg.mod_pages = (*self.cfg.mod_pages, detected_page)

    def detect_version(self) -> str | None:
        return self._detected_metadata.version or _beammp_filename_version(self.name)

    def detect_friendly(self) -> str | None:
        return self._detected_metadata.title or humanise_mod_identifier(
            Path(self.name).stem,
            split_single_camel=True,
        )

    def native_metadata_id(self) -> str | None:
        return self._detected_metadata.native_id

    def metadata_fallback_id(self) -> str:
        stem = Path(self.name).stem
        match = _BEAMMP_MOD_VERSION_RE.search(stem)
        if match is not None:
            stem = stem[: match.start()].rstrip("-_. ")
        return stem.casefold()

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)


class BeamMP_Settings(App_Settings):
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
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
                paragraph=True,
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
        super().__init__(pointer, options, version_getter=version_getter)

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
    relay_notice_player_session_supported = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0xF97316
        self.process = None
        self.proc_name = "BeamMP-Server"
        self.proc_cmd = ["script", "-qfc", self.proc_name, "/dev/null"]
        self.cmd_start = cfg.cmd_start or [
            "./BeamMP-Server",
        ]
        file_settings: Path = cfg.directory.absolute() / "ServerConfig.toml"
        super().__init__(bot, am, cfg, BeamMP_Settings(file_settings, version_getter=lambda: cfg.version), Mod_BeamMP)
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
            if "synced" in action:
                if self.app.relay_notice_player_joined_enabled is False:
                    return
                notice_action = PlayerSessionAction.JOINED
            else:
                if self.app.relay_notice_player_left_enabled is False:
                    return
                notice_action = PlayerSessionAction.LEFT
            notice = self.app.player_session_notice(action=notice_action, source=RelayNoticeSource.APP_LOG)
            app_friendly = getattr(self.app, "friendly", self.app.name)
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    render_notice_text(notice, author_name=player, app_name=app_friendly),
                    player or hikari.UNDEFINED,
                    notice=notice,
                )
            )

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
