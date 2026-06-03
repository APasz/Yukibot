from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import shlex
import tempfile
from asyncio.locks import Event
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit, urlunsplit

import hikari
from pydantic import field_validator

import config
from _discord import (
    App_Bound,
    DC_Bound,
    DC_Relay,
    Fileish,
    MediaProvider,
    OutboundRelayFormatter,
    RelayEmbedPayload,
    RelayOutboundFormatOptions,
    URLVariant,
    URLish,
    render_plain_reference_prefix,
)
from _file import File_Utils
from _relay_embeds import build_app_relay_embed
from _security import Power_Level
from apps._app import AM_Receiver, App, RelayAdvancementTerms
from apps._config import App_Config, AppVersion, Mod_Config, ModDownloadBlockReason, ModType, normalise_app_version
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult, ConsoleResponseSource
from apps._mod import Mod, humanise_mod_identifier
from apps._rcon import RconClient
from apps._save_files import (
    AppSaveEntry,
    AppSaveRoot,
    AppSaveRootMode,
    describe_app_save_path,
    get_app_save_root,
    replace_directory_from_zip,
)
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

log = logging.getLogger(__name__)


CHATIMAGE_IMAGE_FORMATS = frozenset({"png", "jpg", "jpeg", "jfif", "gif", "ico", "bmp"})
_CICODE_ARGUMENT_KEYS = ("url", "name", "nsfw", "pre", "suf")
_PLAYER_NAME_PATTERN = r"[A-Za-z0-9_]{1,16}"

JOIN_RE = re.compile(r"\[.*?\]:\s+[<('\"]*([^>'\"\)\(\s]+)[>'\"\)\(]* joined the game", re.IGNORECASE)
LEAVE_RE = re.compile(r"\[.*?\]:\s+[<('\"]*([^>'\"\)\(\s]+)[>'\"\)\(]* left the game", re.IGNORECASE)
MINECRAFT_DEATH_CAUSE_PHRASE_OPENERS: tuple[str, ...] = (
    "accidentally walked into a Fire Jet",
    "became an ice cube",
    "bit down on a gumball",
    "bled to death",
    "could not let go of their tool",
    "couldn't breathe anymore",
    "didn't duck and cover",
    "discovered intentional game design",
    "extracted too much",
    "failed to hack a Security Station",
    "had their ears melted",
    "has become one with the smeltery",
    "impaled on a spike",
    "lost their breath",
    "met death, the destroyer of worlds",
    "self destructed",
    "stepped on something unreasonably painful",
    "stood too close to a Carminite Reactor",
    "succumbed to radiation poisoning",
    "took a fatal steam bath",
    "touched the primary circuit of a running Tesla coil",
    "was ran over by",
    "was run over by",
    "wasn't careful enough around live wiring",
    "went dancing in the acid rain",
)
MINECRAFT_DEATH_CAUSE_WORD_OPENERS: tuple[str, ...] = (
    "drowned",
    "was",
    "fell",
    "died",
    "tried",
    "blew",
    "hit",
    "walked",
    "got",
    "froze",
    "burned",
    "exploded",
    "starved",
    "suffocated",
    "struck",
    "shot",
    "slain",
)


def _compile_minecraft_death_re() -> re.Pattern[str]:
    phrase_pattern = "|".join(re.escape(opener) for opener in MINECRAFT_DEATH_CAUSE_PHRASE_OPENERS)
    word_pattern = "|".join(re.escape(opener) for opener in MINECRAFT_DEATH_CAUSE_WORD_OPENERS)
    cause_pattern = rf"(?:(?:{phrase_pattern}).*|(?:{word_pattern}).+)"
    return re.compile(rf"\[.*?\]: (?P<player>\S+)\s+(?P<cause>{cause_pattern})", re.IGNORECASE)


_MINECRAFT_DEATH_PLAYER_REFERENCE_SUFFIX_PATTERN = (
    r"(?=$|\s+using\b|\s+with\b|\s+\[|\s+\(|\s+while\b|\s+whilst\b|\s+trying\b|[.,!?])"
)
MINECRAFT_DEATH_PLAYER_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?P<prefix>\bby\s+)(?P<player>{_PLAYER_NAME_PATTERN})"
        rf"{_MINECRAFT_DEATH_PLAYER_REFERENCE_SUFFIX_PATTERN}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>\b(?:while|whilst)\s+fighting\s+)(?P<player>{_PLAYER_NAME_PATTERN})"
        rf"{_MINECRAFT_DEATH_PLAYER_REFERENCE_SUFFIX_PATTERN}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>\btrying\s+to\s+escape\s+)(?P<player>{_PLAYER_NAME_PATTERN})"
        rf"{_MINECRAFT_DEATH_PLAYER_REFERENCE_SUFFIX_PATTERN}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>\btrying\s+to\s+hurt\s+)(?P<player>{_PLAYER_NAME_PATTERN})"
        rf"{_MINECRAFT_DEATH_PLAYER_REFERENCE_SUFFIX_PATTERN}",
        re.IGNORECASE,
    ),
)

DEATH_RE = _compile_minecraft_death_re()
CHAT_RE = re.compile(r"\[.*?\]: <([^>]+)>\s+(.*)")
UUID_RE = re.compile(r"UUID of player (?P<name>\w+) is (?P<uuid>[0-9a-fA-F-]{36})", re.IGNORECASE)
CICODE_RE = re.compile(r"\[\[CICode,(?P<body>.*?)\]\]", re.IGNORECASE)
ADVANCEMENT_RE = re.compile(
    r"\[.*?\]: (?P<player>\S+)\s+(?P<kind>has made the advancement|has reached the goal|has completed the challenge|has just earned the achievement)\s+\[(?P<title>[^\]]+)\]",
    re.IGNORECASE,
)
READY_RE = re.compile(r'Done \([^)]+\)! For help, type "help"', re.IGNORECASE)
PLAYER_LIST_COUNT_RE = re.compile(
    r"There are (?P<online>\d+)\s+(?:of|/)\s+(?:a max of\s+)?(?P<max>\d+)\s+players\s+online",
    re.IGNORECASE,
)
PLAYER_LIST_FALLBACK_RE = re.compile(r"(?P<online>\d+)\D+(?P<max>\d+)")
_PLAYER_NAME_RE = re.compile(_PLAYER_NAME_PATTERN)
_MC_HEADS_AVATAR_URL_TEMPLATE = "https://mc-heads.net/avatar/{identifier}/32"
DEFAULT_MINECRAFT_RCON_PORT = 25575
GAMEMODE_CHOICES = ChoiceSpec(
    ChoiceOption("survival"),
    ChoiceOption("creative"),
    ChoiceOption("adventure"),
    ChoiceOption("spectator"),
)
DIFFICULTY_CHOICES = ChoiceSpec(
    ChoiceOption("peaceful", "Peaceful"),
    ChoiceOption("easy", "Easy"),
    ChoiceOption("normal", "Normal"),
    ChoiceOption("hard", "Hard"),
)
WEATHER_CHOICES = ChoiceSpec(
    ChoiceOption("clear"),
    ChoiceOption("rain"),
    ChoiceOption("thunder"),
)
TIME_CHOICES = ChoiceSpec(
    ChoiceOption("day", "Day"),
    ChoiceOption("noon", "Noon"),
    ChoiceOption("night", "Night"),
    ChoiceOption("midnight", "Midnight"),
)
_MINECRAFT_VERSION_TEXT_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9_.-]+)?")
_FORGE_LOG_RUNTIME_RE = re.compile(
    r"Forge mod loading, version (?P<loader>\S+), for MC (?P<mc>\S+)",
    re.IGNORECASE,
)
_NEOFORGE_LOG_RUNTIME_RE = re.compile(
    r"NeoForge mod loading, version (?P<loader>\S+), for MC (?P<mc>\S+)",
    re.IGNORECASE,
)
_FABRIC_LOG_RUNTIME_RE = re.compile(
    r"Loading Minecraft (?P<mc>\S+) with Fabric Loader (?P<loader>\S+)",
    re.IGNORECASE,
)
_QUILT_LOG_RUNTIME_RE = re.compile(
    r"Loading Minecraft (?P<mc>\S+) with Quilt Loader (?P<loader>\S+)",
    re.IGNORECASE,
)
_VANILLA_LOG_RUNTIME_RE = re.compile(
    r"Starting minecraft server version (?P<mc>\S+)",
    re.IGNORECASE,
)
_MODLAUNCHER_ARGS_RUNTIME_RE = re.compile(
    r"ModLauncher running: args \[(?P<body>.*)\]",
    re.IGNORECASE,
)
_FABRIC_SERVER_JAR_RE = re.compile(
    r"fabric-server-mc\.(?P<mc>[^-]+)-loader\.(?P<loader>[^-]+)-launcher\.(?P<launcher>.+)\.jar",
    re.IGNORECASE,
)
_MINECRAFT_SERVER_JAR_RE = re.compile(
    r"(?:minecraft_server[.-]|server[.-])(?P<mc>\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9_.-]+)?)\.jar",
    re.IGNORECASE,
)
_MINECRAFT_MOD_VERSION_RE_PATTERNS = (
    re.compile(
        r"[-_](?P<version>v?\d+(?:\.\d+)+)(?:\+(?:mc)?\d+(?:\.\d+)+)?(?:\+(?:forge|fabric|quilt|neoforge))?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"[-_](?P<version>v?\d+(?:\.\d+)+)[-_](?:forge|fabric|quilt|neoforge)$",
        re.IGNORECASE,
    ),
    re.compile(r"[-_](?P<version>v?\d+(?:\.\d+)+)$", re.IGNORECASE),
)
_MINECRAFT_MOD_LOADER_TOKENS = frozenset({"forge", "fabric", "quilt", "neoforge"})
_SQUAREMAP_MOD_BASE_NAME = "squaremap"
_SQUAREMAP_PUBLIC_PATH = "/squaremap/"
_SQUAREMAP_WORLD_NAME = "minecraft_overworld"


class MinecraftLoader(enum.StrEnum):
    VANILLA = "vanilla"
    FORGE = "forge"
    NEOFORGE = "neoforge"
    FABRIC = "fabric"
    QUILT = "quilt"
    LEGACY_FABRIC = "legacy_fabric"

    @property
    def display_name(self) -> str:
        if self is MinecraftLoader.NEOFORGE:
            return "NeoForge"
        if self is MinecraftLoader.LEGACY_FABRIC:
            return "Legacy Fabric"
        return self.title()


@dataclass(frozen=True, slots=True)
class MinecraftRuntimeInfo:
    minecraft_version: str | None = None
    loader: MinecraftLoader | None = None
    loader_version: str | None = None

    @property
    def version(self) -> AppVersion | None:
        if self.minecraft_version is None:
            return None
        return AppVersion(
            main=self.minecraft_version,
            framework=self.loader_version,
            loader=self.loader.value if self.loader is not None else None,
        )

    @property
    def loader_display_value(self) -> str | None:
        if self.loader is None:
            return None
        if self.loader_version:
            return f"{self.loader.display_name} {self.loader_version}"
        return self.loader.display_name


def _normalise_optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _normalise_minecraft_version(raw: object) -> str | None:
    text = _normalise_optional_text(raw)
    if text is None:
        return None
    if _MINECRAFT_VERSION_TEXT_RE.fullmatch(text) is None:
        raise ValueError(f"invalid Minecraft version {text!r}")
    return text


def _overlay_runtime_info(
    base: MinecraftRuntimeInfo | None, incoming: MinecraftRuntimeInfo | None
) -> MinecraftRuntimeInfo | None:
    if incoming is None:
        return base
    if base is None:
        return incoming
    return MinecraftRuntimeInfo(
        minecraft_version=incoming.minecraft_version or base.minecraft_version,
        loader=_overlay_runtime_loader(base.loader, incoming.loader),
        loader_version=incoming.loader_version or base.loader_version,
    )


def _fill_runtime_info(
    base: MinecraftRuntimeInfo | None, fallback: MinecraftRuntimeInfo | None
) -> MinecraftRuntimeInfo | None:
    if fallback is None:
        return base
    if base is None:
        return fallback
    return MinecraftRuntimeInfo(
        minecraft_version=base.minecraft_version or fallback.minecraft_version,
        loader=base.loader or fallback.loader,
        loader_version=base.loader_version or fallback.loader_version,
    )


def _overlay_runtime_loader(
    base: MinecraftLoader | None, incoming: MinecraftLoader | None
) -> MinecraftLoader | None:
    if incoming is None:
        return base
    if base is None:
        return incoming
    if incoming is MinecraftLoader.VANILLA and base is not MinecraftLoader.VANILLA:
        return base
    return incoming


def _runtime_info_from_config(cfg: "Minecraft_Config") -> MinecraftRuntimeInfo | None:
    version = cfg.version
    if version is None:
        return None
    loader: MinecraftLoader | None = None
    if version.loader is not None:
        loader = MinecraftLoader(version.loader)
    runtime = MinecraftRuntimeInfo(
        minecraft_version=version.main,
        loader=loader,
        loader_version=version.framework,
    )
    if runtime.minecraft_version is None and runtime.loader is None and runtime.loader_version is None:
        return None
    return runtime


def _parse_launcher_arg_values(pointer: Path) -> dict[str, str]:
    raw = pointer.read_text(config.STR_ENCODE)
    tokens = shlex.split(raw)
    values: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        if index + 1 >= len(tokens):
            break
        values[token] = tokens[index + 1]
        index += 2
    return values


def _parse_forge_like_runtime(pointer: Path, *, loader: MinecraftLoader) -> MinecraftRuntimeInfo | None:
    values = _parse_launcher_arg_values(pointer)
    minecraft_version = values.get("--fml.mcVersion")
    loader_version = (
        values.get("--fml.forgeVersion") or values.get("--fml.neoforgeVersion") or values.get("--fml.neoForgeVersion")
    )
    coordinate_runtime = _runtime_info_from_forge_like_coordinate(pointer.parent.name, loader=loader)
    if coordinate_runtime is not None:
        minecraft_version = minecraft_version or coordinate_runtime.minecraft_version
        loader_version = loader_version or coordinate_runtime.loader_version
    runtime = MinecraftRuntimeInfo(
        minecraft_version=_normalise_minecraft_version(minecraft_version),
        loader=loader,
        loader_version=_normalise_optional_text(loader_version),
    )
    if runtime.minecraft_version is None and runtime.loader_version is None:
        return None
    return runtime


def _runtime_info_from_modlauncher_args_line(line: str) -> MinecraftRuntimeInfo | None:
    match = _MODLAUNCHER_ARGS_RUNTIME_RE.search(line)
    if match is None:
        return None
    tokens = [token.strip() for token in match.group("body").split(",")]
    values: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--") or index + 1 >= len(tokens):
            index += 1
            continue
        values[token] = tokens[index + 1]
        index += 2
    loader: MinecraftLoader | None = None
    if values.get("--fml.forgeVersion") is not None or values.get("--launchTarget") == "forgeserver":
        loader = MinecraftLoader.FORGE
    elif (
        values.get("--fml.neoforgeVersion") is not None
        or values.get("--fml.neoForgeVersion") is not None
        or values.get("--launchTarget") == "neoforgeserver"
    ):
        loader = MinecraftLoader.NEOFORGE
    if loader is None:
        return None
    runtime = MinecraftRuntimeInfo(
        minecraft_version=_normalise_minecraft_version(values.get("--fml.mcVersion")),
        loader=loader,
        loader_version=_normalise_optional_text(
            values.get("--fml.forgeVersion")
            or values.get("--fml.neoforgeVersion")
            or values.get("--fml.neoForgeVersion")
        ),
    )
    if runtime.minecraft_version is None and runtime.loader_version is None:
        return None
    return runtime


def _runtime_info_from_forge_like_coordinate(
    coordinate: str, *, loader: MinecraftLoader
) -> MinecraftRuntimeInfo | None:
    coordinate_parts = coordinate.split("-", 1)
    if len(coordinate_parts) != 2:
        return None
    runtime = MinecraftRuntimeInfo(
        minecraft_version=_normalise_minecraft_version(coordinate_parts[0]),
        loader=loader,
        loader_version=_normalise_optional_text(coordinate_parts[1]),
    )
    if runtime.minecraft_version is None and runtime.loader_version is None:
        return None
    return runtime


def _detect_forge_like_runtime(
    directory: Path, *, glob_pattern: str, loader: MinecraftLoader
) -> MinecraftRuntimeInfo | None:
    for coordinate_directory in sorted(directory.glob(glob_pattern)):
        if not coordinate_directory.is_dir():
            continue
        for pointer in sorted(coordinate_directory.glob("*_args.txt")):
            runtime = _parse_forge_like_runtime(pointer, loader=loader)
            if runtime is not None:
                return runtime
        runtime = _runtime_info_from_forge_like_coordinate(coordinate_directory.name, loader=loader)
        if runtime is not None:
            return runtime
    return None


def _detect_forge_runtime(directory: Path) -> MinecraftRuntimeInfo | None:
    return _detect_forge_like_runtime(
        directory,
        glob_pattern="libraries/net/minecraftforge/forge/*",
        loader=MinecraftLoader.FORGE,
    )


def _detect_neoforge_runtime(directory: Path) -> MinecraftRuntimeInfo | None:
    return _detect_forge_like_runtime(
        directory,
        glob_pattern="libraries/net/neoforged/neoforge/*",
        loader=MinecraftLoader.NEOFORGE,
    )


def _detect_fabric_runtime(directory: Path) -> MinecraftRuntimeInfo | None:
    for pointer in sorted(directory.glob("fabric-server-mc.*-loader.*-launcher.*.jar")):
        match = _FABRIC_SERVER_JAR_RE.fullmatch(pointer.name)
        if match is None:
            continue
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.FABRIC,
            loader_version=_normalise_optional_text(match.group("loader")),
        )
    if (directory / "fabric-server-launch.jar").exists():
        return MinecraftRuntimeInfo(loader=MinecraftLoader.FABRIC)
    if (directory / "legacy-fabric-server-launch.jar").exists():
        return MinecraftRuntimeInfo(loader=MinecraftLoader.LEGACY_FABRIC)
    return None


def _detect_quilt_runtime(directory: Path) -> MinecraftRuntimeInfo | None:
    loader_version: str | None = None
    for pointer in sorted(directory.glob("libraries/org/quiltmc/quilt-loader/*")):
        if pointer.is_dir():
            loader_version = pointer.name
            break
    if (directory / "quilt-server-launch.jar").exists():
        return MinecraftRuntimeInfo(loader=MinecraftLoader.QUILT, loader_version=loader_version)
    return None


def _detect_vanilla_runtime(directory: Path) -> MinecraftRuntimeInfo | None:
    for pointer in sorted(directory.glob("*.jar")):
        match = _MINECRAFT_SERVER_JAR_RE.fullmatch(pointer.name)
        if match is None:
            continue
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.VANILLA,
        )
    return None


def _candidate_runtime_logs(*, directory: Path, server_log: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if server_log is not None:
        candidates.append(server_log)
    candidates.append(directory / "logs" / "latest.log")
    unique: list[Path] = []
    seen: set[Path] = set()
    for pointer in candidates:
        if pointer in seen or not pointer.exists():
            continue
        seen.add(pointer)
        unique.append(pointer)
    return tuple(unique)


def _runtime_info_from_log_line(line: str) -> MinecraftRuntimeInfo | None:
    if runtime := _runtime_info_from_modlauncher_args_line(line):
        return runtime
    if match := _FORGE_LOG_RUNTIME_RE.search(line):
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.FORGE,
            loader_version=_normalise_optional_text(match.group("loader")),
        )
    if match := _NEOFORGE_LOG_RUNTIME_RE.search(line):
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.NEOFORGE,
            loader_version=_normalise_optional_text(match.group("loader")),
        )
    if match := _FABRIC_LOG_RUNTIME_RE.search(line):
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.FABRIC,
            loader_version=_normalise_optional_text(match.group("loader")),
        )
    if match := _QUILT_LOG_RUNTIME_RE.search(line):
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.QUILT,
            loader_version=_normalise_optional_text(match.group("loader")),
        )
    if match := _VANILLA_LOG_RUNTIME_RE.search(line):
        return MinecraftRuntimeInfo(
            minecraft_version=_normalise_minecraft_version(match.group("mc")),
            loader=MinecraftLoader.VANILLA,
        )
    return None


def _detect_runtime_from_logs(*, directory: Path, server_log: Path | None) -> MinecraftRuntimeInfo | None:
    runtime: MinecraftRuntimeInfo | None = None
    for pointer in _candidate_runtime_logs(directory=directory, server_log=server_log):
        try:
            for line in pointer.read_text(config.STR_ENCODE, errors="ignore").splitlines():
                runtime = _overlay_runtime_info(runtime, _runtime_info_from_log_line(line))
        except OSError as xcp:
            log.warning("Failed to inspect Minecraft log %s: %s", pointer, xcp)
    return runtime


def _detect_minecraft_runtime(
    *, directory: Path, server_log: Path | None, cfg: "Minecraft_Config"
) -> MinecraftRuntimeInfo | None:
    detected_runtime: MinecraftRuntimeInfo | None = None
    for detector in (
        _detect_forge_runtime,
        _detect_neoforge_runtime,
        _detect_fabric_runtime,
        _detect_quilt_runtime,
        _detect_vanilla_runtime,
    ):
        detected_runtime = _fill_runtime_info(detected_runtime, detector(directory))
    detected_runtime = _fill_runtime_info(
        detected_runtime,
        _detect_runtime_from_logs(directory=directory, server_log=server_log),
    )
    return _fill_runtime_info(detected_runtime, _runtime_info_from_config(cfg))


def _build_cicode_argument_pattern(key: str) -> re.Pattern[str]:
    other_keys = "|".join(_CICODE_ARGUMENT_KEYS)
    return re.compile(rf"(?:^|,){key}=(?P<value>.*?)(?=,(?:{other_keys})=|$)", re.IGNORECASE)


CICODE_URL_RE = _build_cicode_argument_pattern("url")
CICODE_NAME_RE = _build_cicode_argument_pattern("name")


def _is_non_empty_text(text: str) -> bool:
    return bool(text.strip())


def _is_player_name(text: str) -> bool:
    return _PLAYER_NAME_RE.fullmatch(text.strip()) is not None


def _resolve_minecraft_player_mention(player: str, *, app: "Minecraft") -> str | None:
    players = getattr(app, "_players", None)
    has_seen = getattr(players, "has_seen", None)
    if not callable(has_seen) or not has_seen(player):
        return None

    name_cache = getattr(app, "name_cache", None)
    resolve_name = getattr(name_cache, "resolve_name", None)
    if not callable(resolve_name):
        return None

    scope = getattr(app, "scope", None)
    resolution = cast(
        config.NameResolutionResult,
        resolve_name(player, scope if isinstance(scope, str) else None),
    )
    if resolution.status is not config.NameResolutionStatus.UNIQUE or resolution.user_id is None:
        return None
    return f"<@{resolution.user_id}>"


def _resolve_minecraft_death_mentions(cause: str, *, app: "Minecraft") -> str:
    def replace_reference(match: re.Match[str]) -> str:
        mention = _resolve_minecraft_player_mention(match.group("player"), app=app)
        if mention is None:
            return match.group(0)
        return f"{match.group('prefix')}{mention}"

    resolved_cause = cause
    for pattern in MINECRAFT_DEATH_PLAYER_REFERENCE_PATTERNS:
        resolved_cause = pattern.sub(replace_reference, resolved_cause)
    return resolved_cause


def _normalise_extension(value: str | None) -> str | None:
    if value is None:
        return None
    normalised = value.strip().lower().removeprefix(".")
    if not normalised:
        return None
    return normalised


def _supports_chatimage_extension(extension: str | None) -> bool:
    return _normalise_extension(extension) in CHATIMAGE_IMAGE_FORMATS


def _mc_heads_avatar_uri(identifier: str) -> str:
    return _MC_HEADS_AVATAR_URL_TEMPLATE.format(identifier=quote(identifier, safe=""))


def _build_chatimage_code(url: str, *, name: str | None = None) -> str:
    if name:
        return f"[[CICode,url={url},name={name}]]"
    return f"[[CICode,url={url}]]"


def _is_discord_cdn_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").casefold()
    return host in {"cdn.discordapp.com", "media.discordapp.net"}


def _is_signed_discord_cdn_url(url: str | None) -> bool:
    if not _is_discord_cdn_url(url):
        return False
    parsed_query = urlparse(url).query
    query_text = parsed_query.decode() if isinstance(parsed_query, bytes) else parsed_query
    query = parse_qs(query_text, keep_blank_values=False)
    return all(key in query and any(value.strip() for value in query[key]) for key in ("ex", "is", "hm"))


def _preferred_minecraft_link_url(link: URLish) -> str:
    for candidate in (link.url, link.orig_url):
        if candidate is not None and _is_signed_discord_cdn_url(candidate):
            return candidate
    return link.url


def _render_chatimage_variants(variants: tuple[URLVariant, ...]) -> str | None:
    rendered_variants = [
        _build_chatimage_code(variant.url, name=variant.label)
        for variant in variants
        if _supports_chatimage_extension(variant.extension)
    ]
    if len(rendered_variants) < 2:
        return None
    return " ".join(rendered_variants)


def _render_minecraft_link(link: URLish) -> str | None:
    if not link.is_media:
        return None
    if link.provider is MediaProvider.TENOR and link.orig_url:
        rendered_variants = _render_chatimage_variants(link.variants)
        if rendered_variants is not None:
            return rendered_variants
        return link.orig_url
    preferred_url = _preferred_minecraft_link_url(link)
    if _supports_chatimage_extension(link.extension):
        return _build_chatimage_code(preferred_url, name=link.label)
    return preferred_url


def _render_minecraft_file(file: Fileish, public_url: str) -> str | None:
    if _supports_chatimage_extension(Path(file.name).suffix):
        image_url = file.source_url or public_url
        return _build_chatimage_code(image_url, name=file.name)
    return public_url


@dataclass(frozen=True, slots=True)
class ChatImageCode:
    url: str
    name: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "ChatImageCode | None":
        url_match = CICODE_URL_RE.search(raw)
        if url_match is None:
            return None
        name_match = CICODE_NAME_RE.search(raw)
        url = url_match.group("value").strip()
        name = name_match.group("value").strip() if name_match is not None else None
        return cls(url=url, name=name or None)

    def to_markdown(self) -> str:
        if self.name:
            return f"[{self.name}]({self.url})"
        return self.url


def _validate_advancement_kind(kind: str) -> None:
    normalised = kind.casefold().strip()
    if normalised in {
        "has made the advancement",
        "has reached the goal",
        "has completed the challenge",
        "has just earned the achievement",
    }:
        return
    raise ValueError(f"unsupported advancement kind {kind!r}")


def _parse_optional_server_bool(raw: str | None, *, key: str) -> bool | None:
    if raw is None:
        return None
    value = raw.strip().casefold()
    if not value:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{key} must be `true` or `false`, got {raw!r}")


def _parse_optional_server_int(raw: str | None, *, key: str) -> int | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if not value.isdecimal():
        raise ValueError(f"{key} must be an integer, got {raw!r}")
    return int(value)


def _load_server_properties_map(pointer: Path) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in pointer.read_text(config.STR_ENCODE).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        properties[key.strip()] = value.strip()
    return properties


def _describe_secret_state(raw: str | None) -> str:
    if raw is None:
        return "missing"
    value = raw.strip()
    if not value:
        return "empty"
    return f"set(len={len(value)})"


def _describe_password_match(server_password: str | None) -> str:
    env_password = config.env_opt("APP_COMM_PASS") or config.env_req("APP_COMM_PASS")
    normalised_env_password = env_password.strip("'").strip('"').strip(" ")
    if server_password is None:
        return "missing-in-server-properties"
    if not server_password.strip():
        return "empty-in-server-properties"
    return "match" if server_password == normalised_env_password else "mismatch"


@dataclass(frozen=True, slots=True)
class MinecraftServerPropertiesSnapshot:
    enable_rcon: bool | None
    rcon_port: int | None
    rcon_password: str | None
    max_players: int | None

    @classmethod
    def load(cls, pointer: Path) -> "MinecraftServerPropertiesSnapshot":
        properties = _load_server_properties_map(pointer)
        return cls(
            enable_rcon=_parse_optional_server_bool(properties.get("enable-rcon"), key="enable-rcon"),
            rcon_port=_parse_optional_server_int(properties.get("rcon.port"), key="rcon.port"),
            rcon_password=properties.get("rcon.password"),
            max_players=_parse_optional_server_int(properties.get("max-players"), key="max-players"),
        )


def _load_server_properties_snapshot(pointer: Path) -> MinecraftServerPropertiesSnapshot | None:
    if not pointer.exists():
        return None
    try:
        return MinecraftServerPropertiesSnapshot.load(pointer)
    except (OSError, ValueError) as xcp:
        log.warning("Failed to parse Minecraft server properties at %s: %s", pointer, xcp)
        return None


def _normalise_minecraft_mod_version_token(raw: str) -> tuple[str, bool] | None:
    text = raw.strip()
    if not text:
        return None
    is_mc_token = text.casefold().startswith("mc")
    if is_mc_token:
        text = text[2:]
    text = text.removeprefix("v").removeprefix("V")
    if re.fullmatch(r"\d+(?:\.\d+)+", text) is None:
        return None
    return (text, is_mc_token)


def _choose_loader_adjacent_version(before_raw: str | None, after_raw: str | None) -> str | None:
    before = _normalise_minecraft_mod_version_token(before_raw or "")
    after = _normalise_minecraft_mod_version_token(after_raw or "")
    if before is None and after is None:
        return None
    if before is None:
        return None if after is None or after[1] else after[0]
    if after is None:
        return None if before[1] else before[0]
    if before[1] and not after[1]:
        return after[0]
    if after[1] and not before[1]:
        return before[0]

    before_text, _ = before
    after_text, _ = after
    before_segments = before_text.split(".")
    after_segments = after_text.split(".")
    if before_text.startswith("1.") != after_text.startswith("1."):
        return after_text if before_text.startswith("1.") else before_text
    if len(before_segments) != len(after_segments):
        return before_text if len(before_segments) > len(after_segments) else after_text
    before_tuple = tuple(int(part) for part in before_segments)
    after_tuple = tuple(int(part) for part in after_segments)
    if before_tuple != after_tuple:
        return before_text if before_tuple > after_tuple else after_text
    return after_text


def _detect_minecraft_mod_version(name: str) -> str | None:
    stem = Path(name).stem
    tokens = stem.split("-")
    for index, token in enumerate(tokens):
        if token.casefold() not in _MINECRAFT_MOD_LOADER_TOKENS:
            continue
        after = _normalise_minecraft_mod_version_token(tokens[index + 1] if index + 1 < len(tokens) else "")
        after_next = _normalise_minecraft_mod_version_token(tokens[index + 2] if index + 2 < len(tokens) else "")
        if after is not None and after_next is not None:
            return after_next[0]
        version = _choose_loader_adjacent_version(
            tokens[index - 1] if index > 0 else None,
            tokens[index + 1] if index + 1 < len(tokens) else None,
        )
        if version is not None:
            return version
    for pattern in _MINECRAFT_MOD_VERSION_RE_PATTERNS:
        if match := pattern.search(stem):
            return match.group("version").removeprefix("v")
    return None


def _detect_minecraft_mod_base_name(name: str) -> str:
    stem = Path(name).stem
    patterns = (
        re.compile(
            r"^(?P<base>.+)-(?P<version>v?\d+(?:\.\d+)+)(?:\+(?:mc)?\d+(?:\.\d+)+)?\+(?:forge|fabric|quilt|neoforge)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?P<base>.+)-(?P<version>v?\d+(?:\.\d+)+)-(?:forge|fabric|quilt|neoforge)-\d+(?:\.\d+)+$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?P<base>.+)-(?:forge|fabric|quilt|neoforge)-(?:mc)?\d+(?:\.\d+)+-(?P<version>v?\d+(?:\.\d+)+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?P<base>.+)-(?P<version>v?\d+(?:\.\d+)+)-(?:forge|fabric|quilt|neoforge)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?P<base>.+)-\d+(?:\.\d+)+-(?P<version>v?\d+(?:\.\d+)+)$",
            re.IGNORECASE,
        ),
        re.compile(r"^(?P<base>.+)-(?P<version>v?\d+(?:\.\d+)+)$", re.IGNORECASE),
    )
    for pattern in patterns:
        if match := pattern.fullmatch(stem):
            return match.group("base")
    return stem


def _detect_minecraft_mod_friendly(name: str) -> str:
    base_name = _detect_minecraft_mod_base_name(name)
    return humanise_mod_identifier(base_name, split_single_camel=True)


def _is_squaremap_mod_name(name: str) -> bool:
    return _detect_minecraft_mod_base_name(name).casefold() == _SQUAREMAP_MOD_BASE_NAME


def _build_squaremap_public_url(*, public_base_url: str, world: str) -> str:
    parsed = urlsplit(public_base_url)
    if not parsed.scheme or parsed.hostname is None:
        raise ValueError("PUBLIC_BASE_URL must include a scheme and host.")
    return urlunsplit((parsed.scheme, parsed.netloc, _SQUAREMAP_PUBLIC_PATH, urlencode({"world": world}), ""))


class Mod_MC(Mod):
    def __init__(self, cfg: Mod_Config):
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)

    def sync_metadata(self) -> None:
        super().sync_metadata()
        if _is_squaremap_mod_name(self.name):
            self.cfg.mod_type = ModType.SERVER_ONLY
            if self.cfg.download_block_reason is None:
                self.cfg.download_block_reason = ModDownloadBlockReason.SERVER_ONLY

    def default_mod_type(self) -> ModType:
        if _is_squaremap_mod_name(self.name):
            return ModType.SERVER_ONLY
        return super().default_mod_type()

    def detect_version(self) -> str | None:
        return _detect_minecraft_mod_version(self.name)

    def detect_friendly(self) -> str | None:
        return _detect_minecraft_mod_friendly(self.name)


@dataclass(frozen=True, slots=True)
class PlayerListSnapshot:
    online: int
    maximum: int
    players: frozenset[str]


async def _run_console_command(app: "Minecraft", command: str, *, success_text: str) -> ConsoleActionResult:
    response = await app._relay.send(command)
    if response:
        return ConsoleActionResult(
            summary=success_text,
            text=response,
            source=ConsoleResponseSource.RCON,
        )
    return ConsoleActionResult(summary=success_text)


async def _console_save_all(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    return await _run_console_command(app, "save-all", success_text=f"{app.friendly}: world save requested.")


async def _console_stop_server(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    return await _run_console_command(app, "stop", success_text=f"{app.friendly}: stop requested.")


async def _console_say(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    message: str = cast(str, value)
    return await _run_console_command(app, f"say {message}", success_text=f"{app.friendly}: broadcast sent.")


async def _console_op(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    player: str = cast(str, value)
    return await _run_console_command(app, f"op {player}", success_text=f"{app.friendly}: op requested for `{player}`.")


async def _console_deop(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    player: str = cast(str, value)
    return await _run_console_command(
        app, f"deop {player}", success_text=f"{app.friendly}: deop requested for `{player}`."
    )


async def _console_whitelist_add(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    player: str = cast(str, value)
    return await _run_console_command(
        app,
        f"whitelist add {player}",
        success_text=f"{app.friendly}: whitelist add requested for `{player}`.",
    )


async def _console_whitelist_remove(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    player: str = cast(str, value)
    return await _run_console_command(
        app,
        f"whitelist remove {player}",
        success_text=f"{app.friendly}: whitelist removal requested for `{player}`.",
    )


async def _console_kick(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    player: str = cast(str, value)
    return await _run_console_command(
        app, f"kick {player}", success_text=f"{app.friendly}: kick requested for `{player}`."
    )


async def _console_weather(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    weather: str = cast(str, value)
    return await _run_console_command(
        app, f"weather {weather}", success_text=f"{app.friendly}: weather set to `{weather}`."
    )


async def _console_time_set(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    target: str = cast(str, value)
    return await _run_console_command(
        app, f"time set {target}", success_text=f"{app.friendly}: time set to `{target}`."
    )


async def _console_raw_command(app_obj: object, value: object | None) -> ConsoleActionResult:
    app: Minecraft = cast(Minecraft, app_obj)
    command: str = cast(str, value)
    return await _run_console_command(
        app,
        command,
        success_text=f"{app.friendly}: console command sent.",
    )


_PLAYER_PARAMETER: ConsoleActionParameter[str] = ConsoleActionParameter[str](
    key="player",
    label="Player",
    value_type=str,
    validator=_is_player_name,
    desc="Minecraft username (letters, numbers, underscore; up to 16 characters).",
    max_length=16,
)
_MESSAGE_PARAMETER: ConsoleActionParameter[str] = ConsoleActionParameter[str](
    key="message",
    label="Message",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Broadcast message to send with `say`.",
    max_length=200,
    multiline=True,
)
_WEATHER_PARAMETER: ConsoleActionParameter[str] = ConsoleActionParameter[str](
    key="weather",
    label="Weather",
    value_type=str,
    choices=WEATHER_CHOICES,
    desc="Server weather target.",
)
_TIME_PARAMETER: ConsoleActionParameter[str] = ConsoleActionParameter[str](
    key="time",
    label="Time",
    value_type=str,
    choices=TIME_CHOICES,
    desc="Preset time of day.",
)
_RAW_COMMAND_PARAMETER: ConsoleActionParameter[str] = ConsoleActionParameter[str](
    key="command",
    label="Command",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Raw Minecraft console command without a leading slash.",
    max_length=500,
    multiline=True,
)
_MINECRAFT_CONSOLE_ACTIONS: tuple[ConsoleAction, ...] = (
    ConsoleAction(
        key="save_all",
        label="Save All",
        description="Flush world state to disk.",
        power_level=Power_Level.user,
        execute=_console_save_all,
    ),
    ConsoleAction(
        key="say",
        label="Say",
        description="Broadcast a message to all players.",
        power_level=Power_Level.user,
        execute=_console_say,
        parameter=_MESSAGE_PARAMETER,
    ),
    ConsoleAction(
        key="weather",
        label="Weather",
        description="Set the server weather.",
        power_level=Power_Level.user,
        execute=_console_weather,
        parameter=_WEATHER_PARAMETER,
    ),
    ConsoleAction(
        key="time_set",
        label="Time Set",
        description="Set the world time to a preset.",
        power_level=Power_Level.user,
        execute=_console_time_set,
        parameter=_TIME_PARAMETER,
    ),
    ConsoleAction(
        key="raw_command",
        label="Run Command",
        description="Send a raw command to the Minecraft console.",
        power_level=Power_Level.sudo,
        execute=_console_raw_command,
        parameter=_RAW_COMMAND_PARAMETER,
    ),
    ConsoleAction(
        key="op",
        label="Op Player",
        description="Grant operator access to a player.",
        power_level=Power_Level.sudo,
        execute=_console_op,
        parameter=_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="deop",
        label="Deop Player",
        description="Remove operator access from a player.",
        power_level=Power_Level.sudo,
        execute=_console_deop,
        parameter=_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="whitelist_add",
        label="Whitelist Add",
        description="Add a player to the whitelist.",
        power_level=Power_Level.sudo,
        execute=_console_whitelist_add,
        parameter=_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="whitelist_remove",
        label="Whitelist Remove",
        description="Remove a player from the whitelist.",
        power_level=Power_Level.sudo,
        execute=_console_whitelist_remove,
        parameter=_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="kick",
        label="Kick Player",
        description="Disconnect a player from the server.",
        power_level=Power_Level.sudo,
        execute=_console_kick,
        parameter=_PLAYER_PARAMETER,
    ),
    ConsoleAction(
        key="stop_server",
        label="Stop Server",
        description="Stop the server process through RCON.",
        power_level=Power_Level.sudo,
        execute=_console_stop_server,
    ),
)


class Minecraft_Settings(App_Settings):
    def __init__(self, pointer: Path) -> None:
        options = [
            Setting[int](
                IntSettingSpec(),
                Setting_Label.max_player,
                "max-players",
                [],
                default=20,
                power_level=Power_Level.sudo,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Allow Flight",
                "allow-flight",
                [],
                default=False,
                power_level=Power_Level.sudo,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Command Blocks",
                "enable-command-block",
                [],
                default=False,
                power_level=Power_Level.sudo,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Force Gamemode",
                "force-gamemode",
                [],
                default=False,
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(GAMEMODE_CHOICES),
                "Gamemode",
                "gamemode",
                [],
                default="survival",
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(),
                "Level Name",
                "level-name",
                [],
                default="world",
                desc="Changing this switches the world folder.",
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                "Level Seed",
                "level-seed",
                [],
                default="",
                desc="Only used when generating a new world.",
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(allow_blank=True),
                Setting_Label.motd,
                "motd",
                [],
                default="A Minecraft Server",
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Whitelist",
                "white-list",
                [],
                default=False,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Enforce Whitelist",
                "enforce-whitelist",
                [],
                default=False,
            ),
            Setting[bool](
                BoolSettingSpec(),
                "Pvp",
                "pvp",
                [],
                default=True,
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                "Spawn Protection",
                "spawn-protection",
                [],
                default=16,
                power_level=Power_Level.sudo,
            ),
            Setting[int](
                IntSettingSpec(),
                "View Distance",
                "view-distance",
                [],
                default=10,
                power_level=Power_Level.sudo,
            ),
            Setting[str](
                StringSettingSpec(DIFFICULTY_CHOICES),
                Setting_Label.difficulty,
                "difficulty",
                [],
                default="easy",
            ),
        ]
        super().__init__(pointer, options)

    def load(self) -> None:
        data: str = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines: list[str] = data.split("\n")
        for line in lines:
            for opt in self.options:
                if line.startswith(opt.key):
                    arg, val = [x.strip() for x in line.split("=", 1)]
                    opt.update(val)

    def save(self) -> str:
        data: str = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines: list[str] = data.split("\n")
        for idx, line in enumerate[str](lines):
            for opt in self.options:
                if line.startswith(opt.key):
                    arg, val = [x.strip() for x in line.split("=", 1)]
                    lines[idx] = f"{arg}={opt.serialise_value()}"

        string: str = "\n".join(lines)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


class Minecraft_Config(App_Config):
    relay_advancements: bool = True

    @field_validator("version", mode="before")
    def validate_version(cls, raw: object) -> AppVersion | None:
        version: AppVersion | None = normalise_app_version(raw)
        if version is None:
            return None
        return cls._normalise_runtime_version(version)

    @classmethod
    def _normalise_runtime_version(cls, version: AppVersion) -> AppVersion:
        main: str | None = _normalise_minecraft_version(version.main)
        if main is None:
            raise ValueError("Minecraft version must define a main version.")
        loader: str | None = version.loader
        if loader is not None:
            loader = MinecraftLoader(loader).value
        return AppVersion(
            main=main,
            framework=_normalise_optional_text(version.framework),
            loader=loader,
        )


class Minecraft(App[Minecraft_Config]):
    cfg_cls: type[Minecraft_Config] = Minecraft_Config
    chat_relay_outbound = True
    relay_advancement_terms = RelayAdvancementTerms("Advancement", "Advancements")

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: Minecraft_Config):
        self.manage_embed_color = 0x22C55E
        self.proc_name = "java"
        self.proc_cmd = [self.proc_name, "nogui"]
        file_settings: Path = cfg.directory.absolute() / "server.properties"
        self.cmd_start = cfg.cmd_start or ["bash", "run.sh"]
        self.process = None
        super().__init__(bot, am, cfg, Minecraft_Settings(file_settings), Mod_MC)

        self._file_settings: Path = file_settings
        self._server_properties: MinecraftServerPropertiesSnapshot | None = _load_server_properties_snapshot(
            file_settings
        )
        self._rcon_host: str = cfg.api_host or "localhost"
        self._rcon_port: int = cfg.api_port or (
            self._server_properties.rcon_port
            if self._server_properties and self._server_properties.rcon_port is not None
            else DEFAULT_MINECRAFT_RCON_PORT
        )
        self._relay: RconClient = RconClient(
            self.check_running,
            self._rcon_port,
            host=self._rcon_host,
            label=f"minecraft:{cfg.name}",
        )
        self._tail: Tailer | None = None
        self._tail_machers = set()
        self._server_ready: Event = asyncio.Event()
        self._players: Players = Players(self)
        self.am_receiver = Receiver(self)
        self._activities: Activities = Activities(self)
        self._matchers: Matchers = Matchers(self)
        self._runtime: MinecraftRuntimeInfo | None = None
        self._apply_runtime_snapshot(
            _detect_minecraft_runtime(
                directory=cfg.directory,
                server_log=cfg.server_log_file,
                cfg=cfg,
            ),
            persist=False,
        )

        log.debug(f"{__name__}.Created")

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return _MINECRAFT_CONSOLE_ACTIONS

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Properties",
                path=self._file_settings,
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".properties"}),
            ),
            AppConfigFileRoot(
                id="mod-configs",
                label="Mod Configs",
                path=self.directory / "config",
                kind=AppConfigFileKind.MOD,
                read_power_level_override=Power_Level.visitor,
            ),
            AppConfigFileRoot(
                id="server-config",
                label="Server Config",
                path=self._world_directory_path() / "serverconfig",
                kind=AppConfigFileKind.GAME,
            ),
        )

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        return (
            AppSaveRoot(
                id="world",
                label="Current World",
                path=self._world_directory_path(),
                mode=AppSaveRootMode.SELF,
                include_files=False,
                include_directories=True,
            ),
        )

    @property
    def supports_save_uploads(self) -> bool:
        return True

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        root = get_app_save_root(self.save_file_roots, root_id)
        if Path(upload_name).suffix.casefold() != ".zip":
            raise ValueError("Minecraft save uploads must be .zip archives.")
        destination = root.resolved_path
        temp_parent = destination.parent
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as temp_dir:
            extracted_path = Path(temp_dir) / destination.name
            replace_directory_from_zip(archive_path=source_path, destination=extracted_path)
            File_Utils.remove(destination, silent=True, resolve=False)
            File_Utils.move(extracted_path, destination, overwrite=False)
        return describe_app_save_path(root=root, path=destination, relative_path=destination.name)

    def _world_directory_name(self) -> str:
        root_name = "world"
        settings_manager = getattr(self, "settings", None)
        if settings_manager is None:
            return root_name
        level_name_setting = settings_manager.app.get_setting("level-name")
        if level_name_setting is None:
            return root_name
        if not isinstance(level_name_setting.value, str):
            return root_name
        resolved_name = level_name_setting.value.strip()
        return resolved_name or root_name

    def _world_directory_path(self) -> Path:
        return self.directory / self._world_directory_name()

    @property
    def relay_advancements_enabled(self) -> bool | None:
        return self.cfg.relay_advancements

    @property
    def manager_status_lines(self) -> tuple[str, ...]:
        runtime = getattr(self, "_runtime", None)
        lines = [
            f"scope: {self.scope}",
            f"version: {runtime.minecraft_version if runtime and runtime.minecraft_version else 'none'}",
        ]
        if runtime and runtime.loader_display_value is not None:
            lines.append(f"loader: {runtime.loader_display_value}")
        return tuple(lines)

    def lifecycle_relay_description_lines(
        self,
        *,
        started: bool,
        uptime: timedelta | None = None,
    ) -> tuple[str, ...]:
        del uptime
        if not started:
            return ()
        squaremap_url = self._squaremap_public_url()
        if squaremap_url is None:
            return ()
        return (f"[Squaremap]({squaremap_url})",)

    @property
    def public_map_url(self) -> str | None:
        return self._squaremap_public_url()

    def apply_relay_advancements_enabled(self, enabled: bool) -> None:
        self.cfg.relay_advancements = enabled

    def _squaremap_public_url(self) -> str | None:
        if not self._has_squaremap_mod():
            return None
        try:
            return _build_squaremap_public_url(
                public_base_url=config.PUBLIC_BASE_URL,
                world=_SQUAREMAP_WORLD_NAME,
            )
        except ValueError as xcp:
            log.warning("Failed to build squaremap public URL for %s: %s", self.name, xcp)
            return None

    def _has_squaremap_mod(self) -> bool:
        if self.mods is None:
            return False
        return any(_is_squaremap_mod_name(mod.name) for mod in self.mods.list_mods())

    def _apply_runtime_snapshot(self, runtime: MinecraftRuntimeInfo | None, *, persist: bool) -> bool:
        current_runtime = getattr(self, "_runtime", None)
        next_runtime = _overlay_runtime_info(current_runtime, runtime)
        if next_runtime == current_runtime or next_runtime is None:
            return False
        self._runtime = next_runtime
        self.cfg.version = next_runtime.version
        if persist:
            self.persist_instance_config_overrides()
        return True

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        self._server_ready.clear()
        log.info(
            "Minecraft config for %s: server_properties=%s exists=%s rcon_host=%s rcon_port=%s file_enable_rcon=%s file_rcon_port=%s file_max_players=%s file_password_state=%s password_match=%s",
            self.name,
            self._file_settings,
            self._file_settings.exists(),
            self._rcon_host,
            self._rcon_port,
            self._server_properties.enable_rcon if self._server_properties else None,
            self._server_properties.rcon_port if self._server_properties else None,
            self._server_properties.max_players if self._server_properties else None,
            _describe_secret_state(self._server_properties.rcon_password if self._server_properties else None),
            _describe_password_match(self._server_properties.rcon_password if self._server_properties else None),
        )
        if self._server_properties and self._server_properties.enable_rcon is False:
            log.warning(
                "%s has enable-rcon=false in %s; player count will not work until RCON is enabled.",
                self.name,
                self._file_settings,
            )
        if (
            self._server_properties
            and self._server_properties.rcon_port is not None
            and self._server_properties.rcon_port != self._rcon_port
        ):
            log.warning(
                "%s RCON port mismatch: configured_port=%s server_properties_port=%s",
                self.name,
                self._rcon_port,
                self._server_properties.rcon_port,
            )
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
        await self.wait_for_ready_event(
            self._server_ready,
            timeout_seconds=900.0,
            ready_label="server readiness",
        )
        await self._relay.setup()

        await self._players.start()
        await self._activities.start()
        self._running = True
        return True

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._players.stop()
        await self._activities.stop()
        if not self._relay.is_connected:
            log.warning("%s shutdown is skipping graceful RCON stop because the relay is not connected.", self.name)
            await self._terminate()
            return True
        try:
            await self._relay.send("save-all")
            await asyncio.sleep(0.2)
            await self._relay.send("stop")
        except RuntimeError as xcp:
            log.warning("%s shutdown fell back to terminate because RCON was unavailable: %s", self.name, xcp)
            await self._terminate()
            return True
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

    async def kill(self) -> bool:
        self._running = False
        await self._players.stop()
        await self._activities.stop()
        await self._terminate()
        return True

    async def player_count(self):
        return await self._players.count()


class Receiver(AM_Receiver):
    def __init__(self, app: Minecraft) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        base_content = payload.content_for_app(self.app) if hasattr(payload, "content_for_app") else payload.content
        content = OutboundRelayFormatter.format_payload(
            payload,
            RelayOutboundFormatOptions(
                base_content=base_content,
                link_renderer=_render_minecraft_link,
                file_renderer=_render_minecraft_file,
                reference_renderer=render_plain_reference_prefix,
            ),
        )
        log.debug("Receiver.formatted_content=%r | %s", content, payload)

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
        app._tail_machers.add(self.match_runtime)
        app._tail_machers.add(self.match_ready)
        app._tail_machers.add(self.match_uuid)
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_advancement)
        app._tail_machers.add(self.match_death)
        app._tail_machers.add(self.match_join)
        app._tail_machers.add(self.match_left)

    @staticmethod
    def _deCICodeify(match: re.Match[str]) -> str:
        parsed = ChatImageCode.parse(match.group("body"))
        if parsed is None:
            return match.group(0)
        return parsed.to_markdown()

    async def match_runtime(self, line: str) -> None:
        runtime = _runtime_info_from_log_line(line)
        if runtime is None:
            return
        if self.app._apply_runtime_snapshot(runtime, persist=True):
            loader_value = None
            if self.app.cfg.version is not None:
                loader_value = self.app.cfg.version.loader
                if self.app.cfg.version.framework is not None and loader_value is not None:
                    loader_value = f"{loader_value} {self.app.cfg.version.framework}"
            log.info(
                "%s detected Minecraft runtime: version=%s loader=%s",
                self.app.name,
                self.app.cfg.version.main if self.app.cfg.version is not None else None,
                loader_value,
            )

    async def match_chat(self, line: str):
        if match := CHAT_RE.match(line):
            player, content = match.groups()
            if content and "CICode" in content:
                content = CICODE_RE.sub(self._deCICodeify, content).strip()
            if content and not content.startswith(self.app.cfg.chat_ignore_symbol):
                DC_Relay.add(
                    DC_Bound(
                        self.app,
                        content,
                        player,
                        player_avatar_uri=self._player_avatar_uri(player),
                    )
                )

    async def match_advancement(self, line: str):
        if not self.app.cfg.relay_advancements:
            return
        if match := ADVANCEMENT_RE.match(line):
            player = match.group("player")
            _validate_advancement_kind(match.group("kind"))
            advancement_type = self.app.relay_advancement_term
            advancement_title = match.group("title").strip()
            relay_embed: RelayEmbedPayload = build_app_relay_embed(
                self.app,
                title=advancement_type,
                description=advancement_title,
            )
            content = f"{advancement_type}: {advancement_title}"
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    content,
                    player,
                    relay_embed=relay_embed,
                    player_avatar_uri=self._player_avatar_uri(player),
                )
            )

    async def match_death(self, line: str):
        if match := DEATH_RE.match(line):
            player, content = match.groups()
            content = _resolve_minecraft_death_mentions(content, app=self.app)
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    content,
                    player,
                    player_avatar_uri=self._player_avatar_uri(player),
                )
            )

    async def match_uuid(self, line: str) -> None:
        if match := UUID_RE.search(line):
            players = getattr(self.app, "_players", None)
            note_uuid = getattr(players, "note_uuid", None)
            if callable(note_uuid):
                note_uuid(match.group("name"), match.group("uuid"))

    async def match_join(self, line: str):
        if match := JOIN_RE.match(line):
            player = match.group(1)
            self.app._players.note_join(player)

    async def match_left(self, line: str):
        if match := LEAVE_RE.match(line):
            player = match.group(1)
            self.app._players.note_leave(player)

    async def match_ready(self, line: str):
        if READY_RE.search(line):
            if not self.app._server_ready.is_set():
                log.info("%s matched Minecraft ready line: %s", self.app.name, line)
                self.app._server_ready.set()

    def _player_avatar_uri(self, player: str) -> str | None:
        players = getattr(self.app, "_players", None)
        avatar_uri = getattr(players, "avatar_uri", None)
        if not callable(avatar_uri):
            return None
        return cast(str | None, avatar_uri(player))


class Players:
    def __init__(self, app: Minecraft):
        self.app = app
        self._players_task: asyncio.Task[None] | None = None
        self._running = False
        self._online: int | None = None
        self._max: int | None = None
        self._players: set[str] = set()
        self._player_uuids: dict[str, str] = {}
        self._logged_empty_response = False
        self._logged_unrecognised_response = False

    async def start(self):
        self._online = None
        self._max = None
        self._players = set()
        self._player_uuids = {}
        self._logged_empty_response = False
        self._logged_unrecognised_response = False
        if self._players_task and not self._players_task.done():
            return
        self._running = True
        self._players_task = asyncio.create_task(self._listplayers())

    async def stop(self):
        self._online = None
        self._max = None
        self._players = set()
        self._player_uuids = {}
        self._logged_empty_response = False
        self._logged_unrecognised_response = False
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
            snapshot = await self._fetch_snapshot()
            if snapshot is None:
                continue
            if not config.SILENT_DEBUG:
                log.debug("List Return: %s", snapshot)
            self._apply_snapshot(snapshot)
            await self._reconcile_players(set(snapshot.players))

    async def count(self) -> tuple[int, int] | None:
        if not config.SILENT_DEBUG:
            log.debug(f"Player.count={self._online}/{self._max}")
        if self._online is not None and self._max is not None:
            return (self._online, self._max)
        if not self.app.is_started:
            if not config.SILENT_DEBUG:
                log.debug("%s player count requested before startup completed.", self.app.name)
            return None
        snapshot = await self._fetch_snapshot()
        if snapshot is None:
            return None
        self._apply_snapshot(snapshot)
        return (snapshot.online, snapshot.maximum)

    @staticmethod
    def parse_count_response(text: str) -> tuple[int, int] | None:
        snapshot = Players.parse_list_response(text)
        if snapshot is None:
            return None
        return (snapshot.online, snapshot.maximum)

    @staticmethod
    def parse_list_response(text: str) -> PlayerListSnapshot | None:
        summary = text.split(":", 1)[0].strip()
        if match := PLAYER_LIST_COUNT_RE.search(summary):
            online = int(match.group("online"))
            maximum = int(match.group("max"))
        elif match := PLAYER_LIST_FALLBACK_RE.search(summary):
            online = int(match.group("online"))
            maximum = int(match.group("max"))
        else:
            return None

        player_text = text.split(":", 1)[1].strip() if ":" in text else ""
        players = frozenset(player.strip() for player in player_text.split(",") if player.strip())
        return PlayerListSnapshot(
            online=online,
            maximum=maximum,
            players=players,
        )

    async def _fetch_snapshot(self) -> PlayerListSnapshot | None:
        try:
            response = await self.app._relay.send("list")
        except Exception as xcp:
            log.warning("%s player list query failed: %s", self.app.name, xcp)
            return None
        if not response:
            if not self._logged_empty_response:
                log.warning("%s returned an empty response to the Minecraft `list` command.", self.app.name)
                self._logged_empty_response = True
            if not config.SILENT_DEBUG:
                log.debug("%s returned an empty response to the Minecraft `list` command.", self.app.name)
            return None
        if not config.SILENT_DEBUG:
            log.debug("%s raw Minecraft `list` response: %r", self.app.name, response)
        snapshot = self.parse_list_response(response)
        if snapshot is None and not self._logged_unrecognised_response:
            log.warning("%s returned an unrecognised Minecraft player list response: %r", self.app.name, response)
            self._logged_unrecognised_response = True
        elif snapshot is not None and not config.SILENT_DEBUG:
            log.debug(
                "%s parsed Minecraft player snapshot: online=%s maximum=%s players=%s",
                self.app.name,
                snapshot.online,
                snapshot.maximum,
                sorted(snapshot.players),
            )
        return snapshot

    def _apply_snapshot(self, snapshot: PlayerListSnapshot) -> None:
        self._online = snapshot.online
        self._max = snapshot.maximum
        self._logged_empty_response = False
        self._logged_unrecognised_response = False

    async def _reconcile_players(self, players: set[str]) -> None:
        joins = players.difference(self._players)
        leaves = self._players.difference(players)

        for player in sorted(leaves):
            self.note_leave(player)
        for player in sorted(joins):
            self.note_join(player)

    def note_join(self, player: str) -> None:
        if player in self._players:
            return
        self._players.add(player)
        relay_player = _resolve_minecraft_player_mention(player, app=self.app) or player
        DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.join, relay_player, player_avatar_uri=self.avatar_uri(player)))

    def note_leave(self, player: str) -> None:
        if player not in self._players:
            return
        relay_player = _resolve_minecraft_player_mention(player, app=self.app) or player
        self._players.discard(player)
        DC_Relay.add(DC_Bound(self.app, DC_Bound.generics.left, relay_player, player_avatar_uri=self.avatar_uri(player)))

    @staticmethod
    def _player_key(player: str) -> str:
        return player.strip().casefold()

    def note_uuid(self, player: str, player_uuid: str) -> None:
        normalised_player = player.strip()
        normalised_uuid = player_uuid.strip().lower()
        if not normalised_player or not normalised_uuid:
            return
        self._player_uuids[self._player_key(normalised_player)] = normalised_uuid

    def has_seen(self, player: str) -> bool:
        player_key = self._player_key(player)
        if not player_key:
            return False
        if any(self._player_key(seen_player) == player_key for seen_player in self._players):
            return True
        return player_key in self._player_uuids

    def _cached_profile_uuid(self, player: str) -> str | None:
        name_cache = getattr(self.app, "name_cache", None)
        resolve_game_alias_to_id = getattr(name_cache, "resolve_game_alias_to_id", None)
        get_game_uuid = getattr(name_cache, "get_game_uuid", None)
        scope = getattr(self.app, "scope", None)
        if not callable(resolve_game_alias_to_id) or not callable(get_game_uuid) or not isinstance(scope, str):
            return None
        user_id = cast(int | None, resolve_game_alias_to_id(player, scope))
        if user_id is None:
            return None
        return cast(str | None, get_game_uuid(user_id, scope))

    def avatar_uri(self, player: str) -> str | None:
        normalised_player = player.strip()
        if not normalised_player or normalised_player.casefold() == "system":
            return None
        identifier = self._player_uuids.get(self._player_key(normalised_player))
        if identifier is None:
            identifier = self._cached_profile_uuid(normalised_player)
        if identifier is None:
            if not _is_player_name(normalised_player):
                return None
            identifier = normalised_player
        return _mc_heads_avatar_uri(identifier)


class Activities:
    def __init__(self, app: Minecraft):
        self.app = app
        self._time_task: asyncio.Task[None] | None = None
        self._running = False
        self.providers = [Provider_Day(app)]
        self.tasks: set[asyncio.Task[None]] = set()

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
