from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import shlex
import tempfile
import threading
import tomllib
import zipfile
from asyncio.locks import Event
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import TypeAlias, cast
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

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
    URLish,
    URLVariant,
    render_plain_reference_prefix,
)
from _file import File_Utils
from _minecraft_heads import minecraft_avatar_uri
from _security import Power_Level
from apps._app import (
    AM_Receiver,
    App,
    AppActivityProvider,
    AppActivityProviderMetadata,
    AppRuntimeFaultKind,
    RelayAdvancementTerms,
)
from apps._config import (
    App_Config,
    AppVersion,
    Mod_Config,
    ModPageLink,
    ModType,
    known_mod_page_provider_for_url,
    normalise_app_version,
)
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
from relay_notices import (
    GameDeathKind,
    GameDeathNotice,
    GameProgressKind,
    GameProgressNotice,
    PlayerSessionAction,
    PlayerSessionNotice,
    RelayNoticeSource,
    notice_embed_spec,
    render_notice_text,
)

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
    return re.compile(rf"\[.*?\]: (?P<player>{_PLAYER_NAME_PATTERN})\s+(?P<cause>{cause_pattern})", re.IGNORECASE)


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
_MINECRAFT_MOD_COMPATIBILITY_SUFFIX_RE = re.compile(
    r"^(?P<base>.+)-(?P<version>v?\d+(?:\.\d+)+)"
    r"-(?:mc)?\d+(?:\.(?:\d+|x))+"
    r"-[a-z][a-z0-9_]*\d+(?:\.\d+)*\+?$",
    re.IGNORECASE,
)
_MINECRAFT_MOD_LOADER_TOKENS = frozenset({"forge", "fabric", "quilt", "neoforge"})
_MINECRAFT_MOD_METADATA_MAX_BYTES = 1_048_576
_MINECRAFT_FORGE_METADATA_PATHS = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
_MINECRAFT_FABRIC_METADATA_PATH = "fabric.mod.json"
_MINECRAFT_QUILT_METADATA_PATH = "quilt.mod.json"
_MINECRAFT_MANIFEST_PATH = "META-INF/MANIFEST.MF"
_KUBEJS_MOD_BASE_NAME = "kubejs"
_YUKIBOT_DATA_RELATIVE_PATH = Path(".yukibot")
_LEGACY_YUKIBOT_DATA_RELATIVE_PATH = Path("yukibot")
_YUKIBOT_ASSETS_RELATIVE_PATH = _YUKIBOT_DATA_RELATIVE_PATH / "assets"
_LEGACY_YUKIBOT_ASSETS_RELATIVE_PATH = _LEGACY_YUKIBOT_DATA_RELATIVE_PATH / "assets"
_YUKIBOT_REGISTRIES_RELATIVE_PATH = _YUKIBOT_DATA_RELATIVE_PATH / "registries"
_LEGACY_YUKIBOT_REGISTRIES_RELATIVE_PATH = _LEGACY_YUKIBOT_DATA_RELATIVE_PATH / "registries"
_YUKIBOT_ITEM_ICONS_RELATIVE_PATH = _YUKIBOT_ASSETS_RELATIVE_PATH / "item_icons"
_LEGACY_YUKIBOT_ITEM_ICONS_RELATIVE_PATH = _LEGACY_YUKIBOT_ASSETS_RELATIVE_PATH / "item_icons"
_YUKIBOT_RECIPES_FILE_NAME = "recipes.json"
_YUKIBOT_ITEM_REGISTRY_FILE_NAME = "items.json"
_KUBEJS_SERVER_SCRIPTS_RELATIVE_PATH = Path("kubejs/server_scripts")
_KUBEJS_YUKI_LOG_SCRIPT_NAME = "yuki_log.js"
_KUBEJS_YUKI_RECIPES_SCRIPT_NAME = "yuki_recipes.js"
_KUBEJS_YUKI_ITEM_REGISTRY_SCRIPT_NAME = "yuki_item_registry.js"
_KUBEJS_YUKI_LOG_SOURCE_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "minecraft" / "kubejs" / _KUBEJS_YUKI_LOG_SCRIPT_NAME
)
_KUBEJS_YUKI_ITEM_REGISTRY_SOURCE_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "minecraft" / "kubejs" / _KUBEJS_YUKI_ITEM_REGISTRY_SCRIPT_NAME
)
_KUBEJS_YUKI_LOG_FALLBACK_SOURCE = """var PREFIX = '[YUKI_MC_EVENT] '

function emit(type, data) {
    var out = {}

    out.type = String(type)
    out.time = Date.now()

    for (var key in data) {
        if (data.hasOwnProperty(key)) {
            out[key] = data[key]
        }
    }

    console.info(PREFIX + JSON.stringify(out))
}

function playerName(player) {
    if (player.username) return String(player.username)
    if (player.name && player.name.string) return String(player.name.string)
    return String(player)
}

function playerUUID(player) {
    if (player.uuid) return String(player.uuid)
    return ''
}

PlayerEvents.loggedIn(function (event) {
    emit('player_join', {
        player: playerName(event.player),
        uuid: playerUUID(event.player)
    })
})

PlayerEvents.loggedOut(function (event) {
    emit('player_leave', {
        player: playerName(event.player),
        uuid: playerUUID(event.player)
    })
})

PlayerEvents.chat(function (event) {
    emit('chat', {
        player: playerName(event.player),
        uuid: playerUUID(event.player),
        message: String(event.message)
    })
})

EntityEvents.death(function (event) {
    var entity = event.entity

    if (!entity) return
    if (String(entity.type) != 'minecraft:player') return

    emit('player_death', {
        player: playerName(entity),
        uuid: playerUUID(entity),
        source: String(event.source)
    })
})
"""
_KUBEJS_YUKI_ITEM_REGISTRY_FALLBACK_SOURCE = """const YUKIBOT_ITEM_REGISTRY_OUTPUT_PATH = '.yukibot/registries/items.json'
const YUKIBOT_ITEM_REGISTRY_SCHEMA_VERSION = 1
const BuiltInRegistries = Java.loadClass('net.minecraft.core.registries.BuiltInRegistries')

const itemIds = []
const blockItemIds = []

BuiltInRegistries.ITEM.keySet().forEach(id => {
    itemIds.push(String(id))
    if (BuiltInRegistries.BLOCK.containsKey(id)) {
        blockItemIds.push(String(id))
    }
})

itemIds.sort()
blockItemIds.sort()
JsonIO.write(YUKIBOT_ITEM_REGISTRY_OUTPUT_PATH, {
    schema_version: YUKIBOT_ITEM_REGISTRY_SCHEMA_VERSION,
    generated_at_epoch_ms: Date.now(),
    item_ids: itemIds,
    block_item_ids: blockItemIds
})
console.info(`[YUKI_MC_ITEM_REGISTRY] wrote ${itemIds.length} item ids (${blockItemIds.length} blocks) to ${YUKIBOT_ITEM_REGISTRY_OUTPUT_PATH}`)
"""
_KUBEJS_LOADER_TOKENS = frozenset({"forge", "fabric", "quilt", "neoforge"})
_ALMOST_UNIFIED_MOD_BASE_NAME = "almostunified"
_KUBEJS_SCRIPT_LOADED_RE = re.compile(
    r"\[KubeJS Server/\]:\s+Loaded script server_scripts:yuki_log\.js\b",
    re.IGNORECASE,
)
_KUBEJS_EVENT_RE = re.compile(
    r"\[KubeJS Server/\]:\s+yuki_log\.js#\d+:\s+\[YUKI_MC_EVENT\]\s+(?P<payload>\{.*\})\s*$",
    re.IGNORECASE,
)
_SQUAREMAP_MOD_BASE_NAME = "squaremap"
_SQUAREMAP_PUBLIC_PATH = "/squaremap/"
_SQUAREMAP_CONFIG_RELATIVE_PATH = Path("squaremap/config.yml")
_SQUAREMAP_WEB_ROOT_RELATIVE_PATH = Path("squaremap/web")
_SQUAREMAP_WEB_ADDRESS_RE: re.Pattern[str] = re.compile(r"^\s*web-address\s*:\s*(?P<url>.+?)\s*$")
_SQUAREMAP_WORLD_NAME = "minecraft_overworld"
_MINECRAFT_CRASH_SUMMARY_IGNORED_PREFIXES: tuple[str, ...] = ("Preparing crash report with UUID ",)


def _minecraft_crash_summary_from_log_line(line: str) -> str | None:
    if "FATAL" not in line:
        return None
    if "[main/FATAL]" not in line and "[main/ERROR]" not in line:
        return None
    if "]:" not in line:
        return None
    summary = line.rsplit("]:", 1)[1].strip()
    if not summary:
        return None
    if any(summary.startswith(prefix) for prefix in _MINECRAFT_CRASH_SUMMARY_IGNORED_PREFIXES):
        return None
    return summary


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


class MinecraftRecipeKind(enum.StrEnum):
    SHAPELESS = "shapeless"
    SHAPED = "shaped"
    SMELTING = "smelting"
    BLASTING = "blasting"
    SMOKING = "smoking"
    CAMPFIRE_COOKING = "campfire_cooking"
    STONECUTTING = "stonecutting"

    @property
    def kubejs_method(self) -> str:
        if self is MinecraftRecipeKind.CAMPFIRE_COOKING:
            return "campfireCooking"
        return self.value

    @property
    def recipe_type_id(self) -> str:
        if self is MinecraftRecipeKind.SHAPED:
            return "minecraft:crafting_shaped"
        if self is MinecraftRecipeKind.SHAPELESS:
            return "minecraft:crafting_shapeless"
        return f"minecraft:{self.value}"


class MinecraftRecipeUnificationMode(enum.StrEnum):
    DISABLED = "disabled"
    EXPECTED_PRESENT = "expected_present"
    ADDED_LATER = "added_later"


class KubeJsRecipeAddonKind(enum.StrEnum):
    CREATE = "create"
    IMMERSIVE_ENGINEERING = "immersive_engineering"

    @property
    def display_name(self) -> str:
        if self is KubeJsRecipeAddonKind.CREATE:
            return "KubeJS Create"
        if self is KubeJsRecipeAddonKind.IMMERSIVE_ENGINEERING:
            return "KubeJS Immersive Engineering"
        return self.value.replace("_", " ").title()


_KUBEJS_RECIPE_ADDON_BASE_NAMES: Mapping[str, KubeJsRecipeAddonKind] = {
    "kubejs-create": KubeJsRecipeAddonKind.CREATE,
    "kubejs-immersive-engineering": KubeJsRecipeAddonKind.IMMERSIVE_ENGINEERING,
    "kubejs-immersiveengineering": KubeJsRecipeAddonKind.IMMERSIVE_ENGINEERING,
}


@dataclass(frozen=True, slots=True)
class KubeJsRecipeAddonCapability:
    kind: KubeJsRecipeAddonKind
    mod_name: str

    @property
    def display_name(self) -> str:
        return self.kind.display_name


@dataclass(frozen=True, slots=True)
class KubeJsRecipeSupportStatus:
    kubejs_enabled: bool
    script_path: Path
    script_exists: bool
    addons: tuple[KubeJsRecipeAddonCapability, ...] = ()
    unification_mode: MinecraftRecipeUnificationMode = MinecraftRecipeUnificationMode.DISABLED

    @property
    def addon_display_names(self) -> tuple[str, ...]:
        return tuple(addon.display_name for addon in self.addons)


class MinecraftRecipeIngredientKind(enum.StrEnum):
    ITEM = "item"
    TAG = "tag"


_MINECRAFT_RESOURCE_LOCATION_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_MINECRAFT_NAMESPACE_RE = re.compile(r"^[a-z0-9_.-]+$")


def _normalise_minecraft_resource_location(raw: str, *, field_name: str) -> str:
    text = raw.strip().casefold()
    if _MINECRAFT_RESOURCE_LOCATION_RE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a namespaced Minecraft id.")
    _namespace, resource_path = text.split(":", maxsplit=1)
    if any(segment in {"", ".", ".."} for segment in resource_path.split("/")):
        raise ValueError(f"{field_name} contains an invalid resource path.")
    return text


def _normalise_minecraft_namespace(raw: str, *, field_name: str) -> str:
    text = raw.strip().casefold()
    if _MINECRAFT_NAMESPACE_RE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a Minecraft namespace.")
    return text


def _normalise_recipe_count(raw: int, *, field_name: str, maximum: int = 64) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{field_name} must be an integer.")
    if raw < 1 or raw > maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum}.")
    return raw


def _kubejs_json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _write_text_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=config.STR_ENCODE,
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _recipe_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be an object.")
    return raw


def _recipe_sequence(raw: object, *, label: str) -> Sequence[object]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValueError(f"{label} must be a list.")
    return raw


def _required_recipe_string(payload: Mapping[str, object], key: str, *, label: str) -> str:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} {key!r} must be a non-empty string.")
    return raw


def _optional_recipe_string(payload: Mapping[str, object], key: str, *, label: str) -> str | None:
    raw = payload.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} {key!r} must be a non-empty string when provided.")
    return raw


def _optional_recipe_int(payload: Mapping[str, object], key: str, *, label: str) -> int | None:
    raw = payload.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{label} {key!r} must be an integer when provided.")
    return raw


def _optional_recipe_float(payload: Mapping[str, object], key: str, *, label: str) -> float | None:
    raw = payload.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{label} {key!r} must be a number when provided.")
    return float(raw)


@dataclass(frozen=True, slots=True)
class MinecraftRecipeIngredient:
    kind: MinecraftRecipeIngredientKind
    resource_id: str
    count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _normalise_minecraft_resource_location(self.resource_id, field_name="recipe ingredient"),
        )
        object.__setattr__(self, "count", _normalise_recipe_count(self.count, field_name="recipe ingredient count"))

    @classmethod
    def item(cls, item_id: str, *, count: int = 1) -> "MinecraftRecipeIngredient":
        return cls(MinecraftRecipeIngredientKind.ITEM, item_id, count=count)

    @classmethod
    def tag(cls, tag_id: str, *, count: int = 1) -> "MinecraftRecipeIngredient":
        return cls(MinecraftRecipeIngredientKind.TAG, tag_id, count=count)

    @property
    def kubejs_value(self) -> str:
        resource_text = self.resource_id if self.kind is MinecraftRecipeIngredientKind.ITEM else f"#{self.resource_id}"
        if self.count == 1:
            return resource_text
        return f"{self.count}x {resource_text}"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftRecipeIngredient":
        raw_kind = _required_recipe_string(payload, "kind", label="recipe ingredient")
        kind = MinecraftRecipeIngredientKind(raw_kind)
        count = _optional_recipe_int(payload, "count", label="recipe ingredient")
        return cls(
            kind=kind,
            resource_id=_required_recipe_string(payload, "id", label="recipe ingredient"),
            count=1 if count is None else count,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "id": self.resource_id,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class MinecraftRecipeItemStack:
    item_id: str
    count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "item_id",
            _normalise_minecraft_resource_location(self.item_id, field_name="recipe output"),
        )
        object.__setattr__(self, "count", _normalise_recipe_count(self.count, field_name="recipe output count"))

    @property
    def kubejs_value(self) -> str:
        if self.count == 1:
            return self.item_id
        return f"{self.count}x {self.item_id}"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftRecipeItemStack":
        count = _optional_recipe_int(payload, "count", label="recipe output")
        return cls(
            item_id=_required_recipe_string(payload, "item", label="recipe output"),
            count=1 if count is None else count,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "item": self.item_id,
            "count": self.count,
        }


def _normalise_optional_recipe_id(recipe_id: str | None) -> str | None:
    if recipe_id is None:
        return None
    return _normalise_minecraft_resource_location(recipe_id, field_name="recipe id")


def _recipe_expression_with_id(expression: str, recipe_id: str | None) -> str:
    if recipe_id is None:
        return expression
    return f"{expression}.id({_kubejs_json(recipe_id)})"


@dataclass(frozen=True, slots=True)
class MinecraftShapelessRecipe:
    output: MinecraftRecipeItemStack
    ingredients: tuple[MinecraftRecipeIngredient, ...]
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        if not self.ingredients:
            raise ValueError("Shapeless recipes require at least one ingredient.")
        ingredient_slot_count = sum(ingredient.count for ingredient in self.ingredients)
        if ingredient_slot_count > 9:
            raise ValueError("Shapeless recipes can use at most 9 ingredient slots.")
        object.__setattr__(self, "recipe_id", _normalise_optional_recipe_id(self.recipe_id))

    def render_kubejs(self) -> str:
        expression = (
            f"event.shapeless({_kubejs_json(self.output.kubejs_value)}, "
            f"{_kubejs_json([ingredient.kubejs_value for ingredient in self.ingredients])})"
        )
        return _recipe_expression_with_id(expression, self.recipe_id)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftShapelessRecipe":
        raw_ingredients = _recipe_sequence(payload.get("ingredients"), label="shapeless recipe ingredients")
        return cls(
            output=MinecraftRecipeItemStack.from_mapping(_recipe_mapping(payload.get("output"), label="recipe output")),
            ingredients=tuple(
                MinecraftRecipeIngredient.from_mapping(_recipe_mapping(item, label="shapeless recipe ingredient"))
                for item in raw_ingredients
            ),
            recipe_id=_optional_recipe_string(payload, "id", label="shapeless recipe"),
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": MinecraftRecipeKind.SHAPELESS.value,
            "output": self.output.to_mapping(),
            "ingredients": [ingredient.to_mapping() for ingredient in self.ingredients],
        }
        if self.recipe_id is not None:
            payload["id"] = self.recipe_id
        return payload


@dataclass(frozen=True, slots=True)
class MinecraftShapedRecipe:
    output: MinecraftRecipeItemStack
    pattern: tuple[str, ...]
    key: Mapping[str, MinecraftRecipeIngredient]
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.pattern) <= 3:
            raise ValueError("Shaped recipes require 1 to 3 pattern rows.")
        row_widths = {len(row) for row in self.pattern}
        if len(row_widths) != 1:
            raise ValueError("Shaped recipe rows must all have the same width.")
        row_width = row_widths.pop()
        if not 1 <= row_width <= 3:
            raise ValueError("Shaped recipe rows must be 1 to 3 characters wide.")

        used_symbols: set[str] = {symbol for row in self.pattern for symbol in row if symbol != " "}
        if not used_symbols:
            raise ValueError("Shaped recipes require at least one non-empty ingredient slot.")
        key_symbols: set[str] = set()
        for symbol, ingredient in self.key.items():
            if len(symbol) != 1 or symbol == " ":
                raise ValueError("Shaped recipe key symbols must be single non-space characters.")
            if ingredient.count != 1:
                raise ValueError("Shaped recipe key ingredients must have a count of 1.")
            key_symbols.add(symbol)
        missing_symbols = used_symbols - key_symbols
        if missing_symbols:
            raise ValueError(f"Shaped recipe pattern is missing key symbols: {', '.join(sorted(missing_symbols))}")
        unused_symbols = key_symbols - used_symbols
        if unused_symbols:
            raise ValueError(f"Shaped recipe key has unused symbols: {', '.join(sorted(unused_symbols))}")
        object.__setattr__(self, "recipe_id", _normalise_optional_recipe_id(self.recipe_id))

    def render_kubejs(self) -> str:
        key_payload = {symbol: ingredient.kubejs_value for symbol, ingredient in sorted(self.key.items())}
        expression = (
            f"event.shaped({_kubejs_json(self.output.kubejs_value)}, "
            f"{_kubejs_json(list(self.pattern))}, {_kubejs_json(key_payload)})"
        )
        return _recipe_expression_with_id(expression, self.recipe_id)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftShapedRecipe":
        raw_pattern = _recipe_sequence(payload.get("pattern"), label="shaped recipe pattern")
        raw_key = _recipe_mapping(payload.get("key"), label="shaped recipe key")
        pattern: list[str] = []
        for raw_row in raw_pattern:
            if not isinstance(raw_row, str):
                raise ValueError("Shaped recipe pattern rows must be strings.")
            pattern.append(raw_row)
        key: dict[str, MinecraftRecipeIngredient] = {}
        for symbol, raw_ingredient in raw_key.items():
            if not isinstance(symbol, str):
                raise ValueError("Shaped recipe key symbols must be strings.")
            key[symbol] = MinecraftRecipeIngredient.from_mapping(
                _recipe_mapping(raw_ingredient, label="shaped recipe key ingredient")
            )
        return cls(
            output=MinecraftRecipeItemStack.from_mapping(_recipe_mapping(payload.get("output"), label="recipe output")),
            pattern=tuple(pattern),
            key=key,
            recipe_id=_optional_recipe_string(payload, "id", label="shaped recipe"),
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": MinecraftRecipeKind.SHAPED.value,
            "output": self.output.to_mapping(),
            "pattern": list(self.pattern),
            "key": {symbol: ingredient.to_mapping() for symbol, ingredient in sorted(self.key.items())},
        }
        if self.recipe_id is not None:
            payload["id"] = self.recipe_id
        return payload


@dataclass(frozen=True, slots=True)
class MinecraftCookingRecipe:
    kind: MinecraftRecipeKind
    output: MinecraftRecipeItemStack
    ingredient: MinecraftRecipeIngredient
    experience: float | None = None
    cooking_time_ticks: int | None = None
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in (
            MinecraftRecipeKind.SMELTING,
            MinecraftRecipeKind.BLASTING,
            MinecraftRecipeKind.SMOKING,
            MinecraftRecipeKind.CAMPFIRE_COOKING,
        ):
            raise ValueError(f"Unsupported cooking recipe kind: {self.kind.value}")
        if self.ingredient.count != 1:
            raise ValueError("Cooking recipe ingredients must have a count of 1.")
        if self.experience is not None and self.experience < 0:
            raise ValueError("Cooking recipe experience must be non-negative.")
        if self.cooking_time_ticks is not None:
            if isinstance(self.cooking_time_ticks, bool) or not isinstance(self.cooking_time_ticks, int):
                raise TypeError("Cooking time must be an integer tick count.")
            if self.cooking_time_ticks < 0:
                raise ValueError("Cooking time must be non-negative.")
        object.__setattr__(self, "recipe_id", _normalise_optional_recipe_id(self.recipe_id))

    def render_kubejs(self) -> str:
        expression = (
            f"event.{self.kind.kubejs_method}({_kubejs_json(self.output.kubejs_value)}, "
            f"{_kubejs_json(self.ingredient.kubejs_value)})"
        )
        if self.experience is not None:
            expression = f"{expression}.xp({self.experience:g})"
        if self.cooking_time_ticks is not None:
            expression = f"{expression}.cookingTime({self.cooking_time_ticks})"
        return _recipe_expression_with_id(expression, self.recipe_id)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, kind: MinecraftRecipeKind) -> "MinecraftCookingRecipe":
        return cls(
            kind=kind,
            output=MinecraftRecipeItemStack.from_mapping(_recipe_mapping(payload.get("output"), label="recipe output")),
            ingredient=MinecraftRecipeIngredient.from_mapping(
                _recipe_mapping(payload.get("ingredient"), label="cooking recipe ingredient")
            ),
            experience=_optional_recipe_float(payload, "experience", label="cooking recipe"),
            cooking_time_ticks=_optional_recipe_int(payload, "cooking_time_ticks", label="cooking recipe"),
            recipe_id=_optional_recipe_string(payload, "id", label="cooking recipe"),
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "output": self.output.to_mapping(),
            "ingredient": self.ingredient.to_mapping(),
        }
        if self.experience is not None:
            payload["experience"] = self.experience
        if self.cooking_time_ticks is not None:
            payload["cooking_time_ticks"] = self.cooking_time_ticks
        if self.recipe_id is not None:
            payload["id"] = self.recipe_id
        return payload


@dataclass(frozen=True, slots=True)
class MinecraftStonecuttingRecipe:
    output: MinecraftRecipeItemStack
    ingredient: MinecraftRecipeIngredient
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        if self.ingredient.count != 1:
            raise ValueError("Stonecutting recipe ingredients must have a count of 1.")
        object.__setattr__(self, "recipe_id", _normalise_optional_recipe_id(self.recipe_id))

    def render_kubejs(self) -> str:
        expression = (
            f"event.stonecutting({_kubejs_json(self.output.kubejs_value)}, "
            f"{_kubejs_json(self.ingredient.kubejs_value)})"
        )
        return _recipe_expression_with_id(expression, self.recipe_id)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftStonecuttingRecipe":
        return cls(
            output=MinecraftRecipeItemStack.from_mapping(_recipe_mapping(payload.get("output"), label="recipe output")),
            ingredient=MinecraftRecipeIngredient.from_mapping(
                _recipe_mapping(payload.get("ingredient"), label="stonecutting recipe ingredient")
            ),
            recipe_id=_optional_recipe_string(payload, "id", label="stonecutting recipe"),
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": MinecraftRecipeKind.STONECUTTING.value,
            "output": self.output.to_mapping(),
            "ingredient": self.ingredient.to_mapping(),
        }
        if self.recipe_id is not None:
            payload["id"] = self.recipe_id
        return payload


@dataclass(frozen=True, slots=True)
class MinecraftRecipeRemovalFilter:
    recipe_id: str | None = None
    output: MinecraftRecipeIngredient | None = None
    input: MinecraftRecipeIngredient | None = None
    recipe_type: MinecraftRecipeKind | str | None = None
    mod_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipe_id", _normalise_optional_recipe_id(self.recipe_id))
        if self.recipe_type is not None and not isinstance(self.recipe_type, MinecraftRecipeKind):
            object.__setattr__(
                self,
                "recipe_type",
                _normalise_minecraft_resource_location(self.recipe_type, field_name="recipe type"),
            )
        if self.mod_id is not None:
            object.__setattr__(self, "mod_id", _normalise_minecraft_namespace(self.mod_id, field_name="recipe mod id"))
        if self.output is not None and self.output.count != 1:
            raise ValueError("Recipe removal output filters must have a count of 1.")
        if self.input is not None and self.input.count != 1:
            raise ValueError("Recipe removal input filters must have a count of 1.")
        if not any((self.recipe_id, self.output, self.input, self.recipe_type, self.mod_id)):
            raise ValueError("Recipe removal filters require at least one condition.")

    @property
    def kubejs_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.recipe_id is not None:
            payload["id"] = self.recipe_id
        if self.output is not None:
            payload["output"] = self.output.kubejs_value
        if self.input is not None:
            payload["input"] = self.input.kubejs_value
        if self.recipe_type is not None:
            payload["type"] = (
                self.recipe_type.recipe_type_id
                if isinstance(self.recipe_type, MinecraftRecipeKind)
                else _normalise_minecraft_resource_location(self.recipe_type, field_name="recipe type")
            )
        if self.mod_id is not None:
            payload["mod"] = self.mod_id
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftRecipeRemovalFilter":
        raw_recipe_type = _optional_recipe_string(payload, "recipe_type", label="recipe removal filter")
        recipe_type: MinecraftRecipeKind | str | None
        if raw_recipe_type is None:
            recipe_type = None
        else:
            try:
                recipe_type = MinecraftRecipeKind(raw_recipe_type)
            except ValueError:
                recipe_type = raw_recipe_type
        raw_output = payload.get("output")
        raw_input = payload.get("input")
        return cls(
            recipe_id=_optional_recipe_string(payload, "id", label="recipe removal filter"),
            output=None
            if raw_output is None
            else MinecraftRecipeIngredient.from_mapping(_recipe_mapping(raw_output, label="recipe removal output")),
            input=None
            if raw_input is None
            else MinecraftRecipeIngredient.from_mapping(_recipe_mapping(raw_input, label="recipe removal input")),
            recipe_type=recipe_type,
            mod_id=_optional_recipe_string(payload, "mod", label="recipe removal filter"),
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.recipe_id is not None:
            payload["id"] = self.recipe_id
        if self.output is not None:
            payload["output"] = self.output.to_mapping()
        if self.input is not None:
            payload["input"] = self.input.to_mapping()
        if self.recipe_type is not None:
            payload["recipe_type"] = (
                self.recipe_type.value if isinstance(self.recipe_type, MinecraftRecipeKind) else self.recipe_type
            )
        if self.mod_id is not None:
            payload["mod"] = self.mod_id
        return payload


@dataclass(frozen=True, slots=True)
class MinecraftRecipeRemoval:
    filter: MinecraftRecipeRemovalFilter
    directive_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "directive_id", _normalise_optional_recipe_id(self.directive_id))

    def render_kubejs(self) -> str:
        return f"event.remove({_kubejs_json(self.filter.kubejs_payload)})"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftRecipeRemoval":
        return cls(
            filter=MinecraftRecipeRemovalFilter.from_mapping(
                _recipe_mapping(payload.get("filter"), label="recipe removal filter")
            ),
            directive_id=_optional_recipe_string(payload, "id", label="recipe removal directive"),
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": "remove",
            "filter": self.filter.to_mapping(),
        }
        if self.directive_id is not None:
            payload["id"] = self.directive_id
        return payload


MinecraftRecipeMutation: TypeAlias = (
    MinecraftShapedRecipe
    | MinecraftShapelessRecipe
    | MinecraftCookingRecipe
    | MinecraftStonecuttingRecipe
    | MinecraftRecipeRemoval
)

_MINECRAFT_MANAGED_RECIPE_NAMESPACE = "yukibot"


def minecraft_recipe_mutation_id(mutation: MinecraftRecipeMutation) -> str | None:
    if isinstance(mutation, MinecraftRecipeRemoval):
        return mutation.directive_id
    return mutation.recipe_id


def minecraft_recipe_mutation_with_id(
    mutation: MinecraftRecipeMutation,
    recipe_id: str,
) -> MinecraftRecipeMutation:
    if isinstance(mutation, MinecraftRecipeRemoval):
        return replace(mutation, directive_id=recipe_id)
    return replace(mutation, recipe_id=recipe_id)


def _unique_managed_minecraft_recipe_id(base_recipe_id: str, existing_recipe_ids: Collection[str]) -> str:
    normalised_existing_ids = {recipe_id.strip().casefold() for recipe_id in existing_recipe_ids}
    if base_recipe_id not in normalised_existing_ids:
        return base_recipe_id
    suffix = 2
    while f"{base_recipe_id}_{suffix}" in normalised_existing_ids:
        suffix += 1
    return f"{base_recipe_id}_{suffix}"


def generated_minecraft_recipe_id(
    *,
    minecraft_username: str,
    output_item_id: str,
    existing_recipe_ids: Collection[str],
) -> str:
    username = minecraft_username.strip()
    if _PLAYER_NAME_RE.fullmatch(username) is None:
        raise ValueError("A valid linked Minecraft username is required to create recipes.")
    normalised_output_id = _normalise_minecraft_resource_location(output_item_id, field_name="recipe output")
    output_namespace, output_path = normalised_output_id.split(":", maxsplit=1)
    base_recipe_id = (
        f"{_MINECRAFT_MANAGED_RECIPE_NAMESPACE}:{username.casefold()}/{output_namespace}/{output_path}"
    )
    return _unique_managed_minecraft_recipe_id(base_recipe_id, existing_recipe_ids)


def generated_minecraft_recipe_mutation_id(
    *,
    minecraft_username: str,
    mutation: MinecraftRecipeMutation,
    existing_recipe_ids: Collection[str],
) -> str:
    if not isinstance(mutation, MinecraftRecipeRemoval):
        return generated_minecraft_recipe_id(
            minecraft_username=minecraft_username,
            output_item_id=mutation.output.item_id,
            existing_recipe_ids=existing_recipe_ids,
        )
    username = minecraft_username.strip()
    if _PLAYER_NAME_RE.fullmatch(username) is None:
        raise ValueError("A valid linked Minecraft username is required to create recipe removal directives.")
    removal_filter = mutation.filter
    if removal_filter.recipe_id is not None:
        namespace, resource_path = removal_filter.recipe_id.split(":", maxsplit=1)
        descriptor = f"recipe/{namespace}/{resource_path}"
    elif removal_filter.output is not None:
        namespace, resource_path = removal_filter.output.resource_id.split(":", maxsplit=1)
        descriptor = f"output/{removal_filter.output.kind.value}/{namespace}/{resource_path}"
    elif removal_filter.input is not None:
        namespace, resource_path = removal_filter.input.resource_id.split(":", maxsplit=1)
        descriptor = f"input/{removal_filter.input.kind.value}/{namespace}/{resource_path}"
    elif removal_filter.mod_id is not None:
        descriptor = f"mod/{removal_filter.mod_id}"
    elif removal_filter.recipe_type is not None:
        recipe_type_id = (
            removal_filter.recipe_type.recipe_type_id
            if isinstance(removal_filter.recipe_type, MinecraftRecipeKind)
            else removal_filter.recipe_type
        )
        namespace, resource_path = recipe_type_id.split(":", maxsplit=1)
        descriptor = f"type/{namespace}/{resource_path}"
    else:
        raise ValueError("Recipe removal directives require at least one filter.")
    base_recipe_id = f"{_MINECRAFT_MANAGED_RECIPE_NAMESPACE}:{username.casefold()}/remove/{descriptor}"
    return _unique_managed_minecraft_recipe_id(base_recipe_id, existing_recipe_ids)

_MINECRAFT_RECIPE_BOOK_SCHEMA_VERSION = 1
_MINECRAFT_ITEM_REGISTRY_SCHEMA_VERSION = 1


def _minecraft_recipe_mutation_from_mapping(payload: Mapping[str, object]) -> MinecraftRecipeMutation:
    raw_kind = _required_recipe_string(payload, "kind", label="recipe mutation")
    if raw_kind == "remove":
        return MinecraftRecipeRemoval.from_mapping(payload)
    kind = MinecraftRecipeKind(raw_kind)
    if kind is MinecraftRecipeKind.SHAPELESS:
        return MinecraftShapelessRecipe.from_mapping(payload)
    if kind is MinecraftRecipeKind.SHAPED:
        return MinecraftShapedRecipe.from_mapping(payload)
    if kind in (
        MinecraftRecipeKind.SMELTING,
        MinecraftRecipeKind.BLASTING,
        MinecraftRecipeKind.SMOKING,
        MinecraftRecipeKind.CAMPFIRE_COOKING,
    ):
        return MinecraftCookingRecipe.from_mapping(payload, kind=kind)
    if kind is MinecraftRecipeKind.STONECUTTING:
        return MinecraftStonecuttingRecipe.from_mapping(payload)
    raise ValueError(f"Unsupported recipe mutation kind: {raw_kind}")


def _minecraft_recipe_mutation_to_mapping(mutation: MinecraftRecipeMutation) -> dict[str, object]:
    return mutation.to_mapping()


@dataclass(frozen=True, slots=True)
class MinecraftRecipeBook:
    mutations: tuple[MinecraftRecipeMutation, ...] = ()
    schema_version: int = _MINECRAFT_RECIPE_BOOK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _MINECRAFT_RECIPE_BOOK_SCHEMA_VERSION:
            raise ValueError(f"Unsupported Minecraft recipe book schema version: {self.schema_version}")

    @classmethod
    def empty(cls) -> "MinecraftRecipeBook":
        return cls()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftRecipeBook":
        raw_schema_version = payload.get("schema_version")
        if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
            raise ValueError("Minecraft recipe book schema_version must be an integer.")
        raw_mutations = _recipe_sequence(payload.get("mutations"), label="recipe book mutations")
        return cls(
            schema_version=raw_schema_version,
            mutations=tuple(
                _minecraft_recipe_mutation_from_mapping(_recipe_mapping(item, label="recipe mutation"))
                for item in raw_mutations
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mutations": [_minecraft_recipe_mutation_to_mapping(mutation) for mutation in self.mutations],
        }


@dataclass(frozen=True, slots=True)
class MinecraftItemRegistrySnapshot:
    item_ids: tuple[str, ...] = ()
    block_item_ids: tuple[str, ...] = ()
    item_types_classified: bool = False
    generated_at_epoch_ms: int | None = None
    schema_version: int = _MINECRAFT_ITEM_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _MINECRAFT_ITEM_REGISTRY_SCHEMA_VERSION:
            raise ValueError(f"Unsupported Minecraft item registry schema version: {self.schema_version}")
        if not isinstance(self.item_types_classified, bool):
            raise TypeError("Minecraft item registry item_types_classified must be a boolean.")
        if self.block_item_ids and not self.item_types_classified:
            raise ValueError("Minecraft block item IDs require classified item type data.")
        if self.generated_at_epoch_ms is not None:
            if isinstance(self.generated_at_epoch_ms, bool) or not isinstance(self.generated_at_epoch_ms, int):
                raise ValueError("Minecraft item registry generated_at_epoch_ms must be an integer.")
            if self.generated_at_epoch_ms < 0:
                raise ValueError("Minecraft item registry generated_at_epoch_ms must not be negative.")
        normalised_item_ids = tuple(
            sorted(
                {
                    _normalise_minecraft_resource_location(item_id, field_name="minecraft item registry item id")
                    for item_id in self.item_ids
                }
            )
        )
        object.__setattr__(self, "item_ids", normalised_item_ids)
        normalised_block_item_ids = tuple(
            sorted(
                {
                    _normalise_minecraft_resource_location(
                        item_id,
                        field_name="minecraft block item registry item id",
                    )
                    for item_id in self.block_item_ids
                }
            )
        )
        unknown_block_item_ids = set(normalised_block_item_ids) - set(normalised_item_ids)
        if unknown_block_item_ids:
            raise ValueError("Minecraft block item registry IDs must also exist in the item registry.")
        object.__setattr__(self, "block_item_ids", normalised_block_item_ids)

    @classmethod
    def empty(cls) -> "MinecraftItemRegistrySnapshot":
        return cls()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "MinecraftItemRegistrySnapshot":
        raw_schema_version = payload.get("schema_version")
        if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
            raise ValueError("Minecraft item registry schema_version must be an integer.")
        raw_generated_at_epoch_ms = payload.get("generated_at_epoch_ms")
        if raw_generated_at_epoch_ms is None:
            generated_at_epoch_ms: int | None = None
        else:
            if isinstance(raw_generated_at_epoch_ms, bool) or not isinstance(raw_generated_at_epoch_ms, int):
                raise ValueError("Minecraft item registry generated_at_epoch_ms must be an integer.")
            generated_at_epoch_ms = raw_generated_at_epoch_ms
        raw_item_ids = _recipe_sequence(payload.get("item_ids"), label="minecraft item registry item ids")
        item_ids: list[str] = []
        for raw_item_id in raw_item_ids:
            if not isinstance(raw_item_id, str):
                raise ValueError("Minecraft item registry item ids must be strings.")
            item_ids.append(raw_item_id)
        raw_block_item_ids = payload.get("block_item_ids")
        block_item_ids: list[str] = []
        if raw_block_item_ids is not None:
            for raw_block_item_id in _recipe_sequence(
                raw_block_item_ids,
                label="minecraft block item registry item ids",
            ):
                if not isinstance(raw_block_item_id, str):
                    raise ValueError("Minecraft block item registry item ids must be strings.")
                block_item_ids.append(raw_block_item_id)
        return cls(
            schema_version=raw_schema_version,
            generated_at_epoch_ms=generated_at_epoch_ms,
            item_ids=tuple(item_ids),
            block_item_ids=tuple(block_item_ids),
            item_types_classified=raw_block_item_ids is not None,
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "generated_at_epoch_ms": self.generated_at_epoch_ms,
            "item_ids": list(self.item_ids),
        }
        if self.item_types_classified:
            payload["block_item_ids"] = list(self.block_item_ids)
        return payload


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


def _resolve_minecraft_player_user_id(player: str, *, app: "Minecraft") -> int | None:
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
    return resolution.user_id


def _resolve_minecraft_player_mention(player: str, *, app: "Minecraft") -> str | None:
    user_id = _resolve_minecraft_player_user_id(player, app=app)
    if user_id is None:
        return None
    return f"<@{user_id}>"


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
    return minecraft_avatar_uri(identifier)


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


def _minecraft_progress_kind(kind: str) -> GameProgressKind:
    normalised = kind.casefold().strip()
    if normalised == "has made the advancement":
        return GameProgressKind.ADVANCEMENT
    if normalised == "has reached the goal":
        return GameProgressKind.GOAL
    if normalised == "has completed the challenge":
        return GameProgressKind.CHALLENGE
    if normalised == "has just earned the achievement":
        return GameProgressKind.ACHIEVEMENT
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
    if compatibility_match := _MINECRAFT_MOD_COMPATIBILITY_SUFFIX_RE.fullmatch(stem):
        return compatibility_match.group("version").removeprefix("v")
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
    if compatibility_match := _MINECRAFT_MOD_COMPATIBILITY_SUFFIX_RE.fullmatch(stem):
        return compatibility_match.group("base")
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


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)


def _nonempty_metadata_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


@dataclass(frozen=True, slots=True)
class MinecraftModMetadata:
    mod_id: str | None = None
    display_name: str | None = None
    version: str | None = None
    homepage: str | None = None


def _usable_minecraft_mod_version(value: object) -> str | None:
    version = _nonempty_metadata_text(value)
    if version is None or (version.startswith("${") and version.endswith("}")):
        return None
    return version


def _forge_mod_metadata(payload: object) -> MinecraftModMetadata | None:
    metadata = _string_mapping(payload)
    if metadata is None:
        return None
    raw_mods = metadata.get("mods")
    if not isinstance(raw_mods, list):
        return None
    for raw_mod in cast(list[object], raw_mods):
        mod = _string_mapping(raw_mod)
        if mod is not None:
            return MinecraftModMetadata(
                mod_id=_nonempty_metadata_text(mod.get("modId")),
                display_name=_nonempty_metadata_text(mod.get("displayName")),
                version=_usable_minecraft_mod_version(mod.get("version")),
                homepage=_nonempty_metadata_text(mod.get("displayURL")),
            )
    return None


def _fabric_mod_metadata(payload: object) -> MinecraftModMetadata | None:
    metadata = _string_mapping(payload)
    if metadata is None:
        return None
    contact = _string_mapping(metadata.get("contact"))
    return MinecraftModMetadata(
        mod_id=_nonempty_metadata_text(metadata.get("id")),
        display_name=_nonempty_metadata_text(metadata.get("name")),
        version=_usable_minecraft_mod_version(metadata.get("version")),
        homepage=None if contact is None else _nonempty_metadata_text(contact.get("homepage")),
    )


def _quilt_mod_metadata(payload: object) -> MinecraftModMetadata | None:
    metadata = _string_mapping(payload)
    quilt_loader = None if metadata is None else _string_mapping(metadata.get("quilt_loader"))
    quilt_metadata = None if quilt_loader is None else _string_mapping(quilt_loader.get("metadata"))
    if quilt_loader is None:
        return None
    contact = None if quilt_metadata is None else _string_mapping(quilt_metadata.get("contact"))
    return MinecraftModMetadata(
        mod_id=_nonempty_metadata_text(quilt_loader.get("id")),
        display_name=None if quilt_metadata is None else _nonempty_metadata_text(quilt_metadata.get("name")),
        version=_usable_minecraft_mod_version(quilt_loader.get("version")),
        homepage=None if contact is None else _nonempty_metadata_text(contact.get("homepage")),
    )


def _read_minecraft_mod_metadata_entry(archive: zipfile.ZipFile, member_name: str) -> str | None:
    try:
        member = archive.getinfo(member_name)
    except KeyError:
        return None
    if member.is_dir() or member.file_size > _MINECRAFT_MOD_METADATA_MAX_BYTES:
        return None
    try:
        return archive.read(member).decode("utf-8-sig")
    except (OSError, RuntimeError, UnicodeDecodeError, zipfile.BadZipFile):
        return None


def _minecraft_manifest_version(archive: zipfile.ZipFile) -> str | None:
    raw_manifest = _read_minecraft_mod_metadata_entry(archive, _MINECRAFT_MANIFEST_PATH)
    if raw_manifest is None:
        return None
    attributes: dict[str, str] = {}
    current_name: str | None = None
    for line in raw_manifest.splitlines():
        if line.startswith(" ") and current_name is not None:
            attributes[current_name] += line[1:]
            continue
        if ":" not in line:
            current_name = None
            continue
        raw_name, raw_value = line.split(":", 1)
        current_name = raw_name.strip().casefold()
        attributes[current_name] = raw_value.strip()
    return _usable_minecraft_mod_version(attributes.get("implementation-version"))


def _minecraft_metadata_with_manifest_version(
    archive: zipfile.ZipFile,
    metadata: MinecraftModMetadata,
) -> MinecraftModMetadata:
    if metadata.version is not None:
        return metadata
    manifest_version = _minecraft_manifest_version(archive)
    return metadata if manifest_version is None else replace(metadata, version=manifest_version)


def _minecraft_mod_metadata(pointer: Path) -> MinecraftModMetadata | None:
    if not pointer.is_file():
        return None
    try:
        with zipfile.ZipFile(pointer, "r") as archive:
            for member_name in _MINECRAFT_FORGE_METADATA_PATHS:
                raw_metadata = _read_minecraft_mod_metadata_entry(archive, member_name)
                if raw_metadata is None:
                    continue
                try:
                    metadata = _forge_mod_metadata(tomllib.loads(raw_metadata))
                except tomllib.TOMLDecodeError:
                    continue
                if metadata is not None:
                    return _minecraft_metadata_with_manifest_version(archive, metadata)

            raw_fabric_metadata = _read_minecraft_mod_metadata_entry(archive, _MINECRAFT_FABRIC_METADATA_PATH)
            if raw_fabric_metadata is not None:
                try:
                    fabric_payload = cast(object, json.loads(raw_fabric_metadata))
                    metadata = _fabric_mod_metadata(fabric_payload)
                except json.JSONDecodeError:
                    metadata = None
                if metadata is not None:
                    return _minecraft_metadata_with_manifest_version(archive, metadata)

            raw_quilt_metadata = _read_minecraft_mod_metadata_entry(archive, _MINECRAFT_QUILT_METADATA_PATH)
            if raw_quilt_metadata is not None:
                try:
                    quilt_payload = cast(object, json.loads(raw_quilt_metadata))
                    metadata = _quilt_mod_metadata(quilt_payload)
                except json.JSONDecodeError:
                    metadata = None
                if metadata is not None:
                    return _minecraft_metadata_with_manifest_version(archive, metadata)
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _minecraft_mod_page(raw_url: str | None) -> ModPageLink | None:
    if raw_url is None:
        return None
    provider = known_mod_page_provider_for_url(raw_url)
    try:
        return ModPageLink(
            name="Homepage" if provider is None else provider.value,
            url=raw_url,
        )
    except ValueError:
        return None


def _bundled_kubejs_yuki_log_script_source() -> str:
    try:
        return _KUBEJS_YUKI_LOG_SOURCE_PATH.read_text(config.STR_ENCODE)
    except OSError as xcp:
        log.info(
            "Bundled KubeJS relay script unavailable at %s; using embedded fallback: %s",
            _KUBEJS_YUKI_LOG_SOURCE_PATH,
            xcp,
        )
        return _KUBEJS_YUKI_LOG_FALLBACK_SOURCE


def _bundled_kubejs_yuki_item_registry_script_source() -> str:
    try:
        return _KUBEJS_YUKI_ITEM_REGISTRY_SOURCE_PATH.read_text(config.STR_ENCODE)
    except OSError as xcp:
        log.info(
            "Bundled KubeJS item registry script unavailable at %s; using embedded fallback: %s",
            _KUBEJS_YUKI_ITEM_REGISTRY_SOURCE_PATH,
            xcp,
        )
        return _KUBEJS_YUKI_ITEM_REGISTRY_FALLBACK_SOURCE


def _is_kubejs_mod_name(name: str) -> bool:
    base_name = _normalised_minecraft_mod_base_name(name)
    if base_name == _KUBEJS_MOD_BASE_NAME:
        return True
    tokens = tuple(token for token in base_name.split("-") if token)
    return len(tokens) >= 2 and tokens[0] == _KUBEJS_MOD_BASE_NAME and tokens[1] in _KUBEJS_LOADER_TOKENS


def _normalised_minecraft_mod_base_name(name: str) -> str:
    return _detect_minecraft_mod_base_name(name).strip().casefold().replace("_", "-")


def _is_almost_unified_mod_name(name: str) -> bool:
    return _normalised_minecraft_mod_base_name(name) == _ALMOST_UNIFIED_MOD_BASE_NAME


def _detect_kubejs_recipe_addon_kind(name: str) -> KubeJsRecipeAddonKind | None:
    base_name = _normalised_minecraft_mod_base_name(name)
    for addon_base_name, addon_kind in _KUBEJS_RECIPE_ADDON_BASE_NAMES.items():
        if base_name == addon_base_name or base_name.startswith(f"{addon_base_name}-"):
            return addon_kind
    return None


def _managed_kubejs_recipe_script_source(
    status: KubeJsRecipeSupportStatus,
    mutations: tuple[MinecraftRecipeMutation, ...] = (),
) -> str:
    addon_lines = "\n".join(f"// - {addon.display_name}: {addon.mod_name}" for addon in status.addons)
    if not addon_lines:
        addon_lines = "// - none detected"
    mutation_lines = "\n".join(f"  {mutation.render_kubejs()}" for mutation in mutations)
    if not mutation_lines:
        mutation_lines = "  // YukiBot generated recipe mutations will be written here."
    return (
        "// Managed by YukiBot. Edit generated recipes through the Recipes tab.\n"
        "// Manual changes in this file may be overwritten.\n"
        f"// AlmostUnified mode: {status.unification_mode.value}\n"
        "// Detected KubeJS recipe addons:\n"
        f"{addon_lines}\n\n"
        "ServerEvents.recipes(function(event) {\n"
        f"{mutation_lines}\n"
        "})\n"
    )


def _is_squaremap_mod_name(name: str) -> bool:
    return _detect_minecraft_mod_base_name(name).casefold() == _SQUAREMAP_MOD_BASE_NAME


class KubeJsEventType(enum.StrEnum):
    PLAYER_JOIN = "player_join"
    PLAYER_LEAVE = "player_leave"
    CHAT = "chat"
    PLAYER_DEATH = "player_death"


@dataclass(frozen=True, slots=True)
class KubeJsEvent:
    event_type: KubeJsEventType
    player: str
    uuid: str | None = None
    message: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if not _PLAYER_NAME_RE.fullmatch(self.player):
            raise ValueError("KubeJS event player is invalid.")
        if self.uuid is not None and not self.uuid.strip():
            raise ValueError("KubeJS event uuid must not be blank.")
        if self.event_type is KubeJsEventType.CHAT and (self.message is None or not self.message.strip()):
            raise ValueError("KubeJS chat event message must not be blank.")


def _required_kubejs_player(payload: Mapping[str, object]) -> str:
    raw_player = payload.get("player")
    if not isinstance(raw_player, str):
        raise ValueError("KubeJS event player is invalid.")
    player = raw_player.strip()
    if not _PLAYER_NAME_RE.fullmatch(player):
        raise ValueError("KubeJS event player is invalid.")
    return player


def _optional_kubejs_text(payload: Mapping[str, object], key: str) -> str | None:
    raw_value = payload.get(key)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError(f"KubeJS event {key} is invalid.")
    value = raw_value.strip()
    if not value:
        return None
    return value


def _parse_kubejs_event(line: str) -> KubeJsEvent | None:
    match = _KUBEJS_EVENT_RE.search(line)
    if match is None:
        return None
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_type = payload.get("type")
    if not isinstance(raw_type, str):
        return None
    try:
        event_type = KubeJsEventType(raw_type)
        return KubeJsEvent(
            event_type=event_type,
            player=_required_kubejs_player(payload),
            uuid=_optional_kubejs_text(payload, "uuid"),
            message=_optional_kubejs_text(payload, "message"),
            source=_optional_kubejs_text(payload, "source"),
        )
    except ValueError:
        return None


def _build_squaremap_public_url(*, public_base_url: str, world: str) -> str:
    parsed = urlsplit(public_base_url)
    if not parsed.scheme or parsed.hostname is None:
        raise ValueError("PUBLIC_BASE_URL must include a scheme and host.")
    return urlunsplit((parsed.scheme, parsed.netloc, _SQUAREMAP_PUBLIC_PATH, urlencode({"world": world}), ""))


def _load_squaremap_web_address(pointer: Path) -> str | None:
    if not pointer.exists():
        return None
    try:
        lines = pointer.read_text(encoding="utf-8").splitlines()
    except OSError as xcp:
        log.warning("Failed to read Squaremap config at %s: %s", pointer, xcp)
        return None
    for line in lines:
        match = _SQUAREMAP_WEB_ADDRESS_RE.match(line)
        if match is None:
            continue
        candidate = match.group("url").split("#", 1)[0].strip().strip("\"'")
        parsed = urlsplit(candidate)
        if not parsed.scheme or parsed.hostname is None:
            log.warning("Ignoring invalid Squaremap web-address at %s: %s", pointer, candidate)
            return None
        return candidate
    return None


class Mod_MC(Mod):
    def __init__(self, cfg: Mod_Config):
        self._detected_metadata = MinecraftModMetadata()
        super().__init__(cfg)

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)

    def sync_metadata(self) -> None:
        self.sync_enabled_state()
        self._detected_metadata = _minecraft_mod_metadata(self.path) or MinecraftModMetadata()
        super().sync_metadata()
        detected_page = _minecraft_mod_page(self._detected_metadata.homepage)
        if detected_page is not None:
            detected_provider = known_mod_page_provider_for_url(detected_page.url)
            has_page = any(
                existing_page.url == detected_page.url
                or (
                    detected_provider is not None
                    and known_mod_page_provider_for_url(existing_page.url) is detected_provider
                )
                for existing_page in self.cfg.mod_pages
            )
            if not has_page:
                self.cfg.mod_pages = (*self.cfg.mod_pages, detected_page)
        if _is_squaremap_mod_name(self.name) and self.cfg.classification_override is None:
            self.cfg.mod_type = ModType.SERVER

    def default_mod_type(self) -> ModType:
        if _is_squaremap_mod_name(self.name):
            return ModType.SERVER
        return super().default_mod_type()

    def detect_version(self) -> str | None:
        return self._detected_metadata.version or _detect_minecraft_mod_version(self.name)

    def detect_friendly(self) -> str | None:
        return self._detected_metadata.display_name or _detect_minecraft_mod_friendly(self.name)

    def native_metadata_id(self) -> str | None:
        return self._detected_metadata.mod_id

    def metadata_fallback_id(self) -> str:
        return _detect_minecraft_mod_base_name(self.name).casefold()


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
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
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
                paragraph=True,
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
        super().__init__(pointer, options, version_getter=version_getter)

    def load(self) -> None:
        data: str = self.pointer.read_text(config.STR_ENCODE)
        if not data:
            raise ValueError("config must not be empty")

        lines: list[str] = data.split("\n")
        for line in lines:
            for opt in self.options:
                if line.startswith(opt.key):
                    arg, val = [x.strip() for x in line.split("=", 1)]
                    opt.load_value(val)

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
    pack_author: str = "Yukibot"

    @field_validator("pack_author", mode="before")
    @classmethod
    def validate_pack_author(cls, raw: object) -> str:
        author = _normalise_optional_text(raw)
        if author is None:
            raise ValueError("Minecraft pack author must not be empty")
        return author

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
            build=version.build,
            framework=_normalise_optional_text(version.framework),
            loader=loader,
        )


class Minecraft(App[Minecraft_Config]):
    cfg_cls: type[Minecraft_Config] = Minecraft_Config
    chat_relay_outbound = True
    relay_advancement_terms = RelayAdvancementTerms("Advancement", "Advancements")
    relay_notice_player_session_supported = True
    relay_notice_player_death_supported = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: Minecraft_Config):
        self.manage_embed_color = 0x22C55E
        self.proc_name = "java"
        self.proc_cmd = [self.proc_name, "nogui"]
        file_settings: Path = cfg.directory.absolute() / "server.properties"
        self.cmd_start = cfg.cmd_start or ["bash", "run.sh"]
        self.process = None
        super().__init__(bot, am, cfg, Minecraft_Settings(file_settings, version_getter=lambda: cfg.version), Mod_MC)

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
        self._minecraft_item_icon_archive_cache_lock = threading.Lock()
        self._kubejs_event_stream_ready = False
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

    async def post_init(self):
        await super().post_init()
        self._migrate_legacy_yukibot_data()
        self._sync_kubejs_yuki_log_script()
        self._sync_kubejs_recipe_script()
        self._sync_kubejs_item_registry_script()
        await asyncio.to_thread(self._minecraft_item_icon_archive_paths_by_namespace)

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return self.available_console_actions(_MINECRAFT_CONSOLE_ACTIONS)

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

    @property
    def supports_save_delete(self) -> bool:
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

    def delete_save_file(self, *, file_id: str) -> AppSaveEntry:
        if self.check_running():
            raise ValueError("Stop the server before deleting its current world.")
        try:
            current_save = next(save for save in self.list_save_files() if save.id == file_id)
        except StopIteration as xcp:
            raise FileNotFoundError(f"Unknown save file: {file_id}") from xcp
        save_path = self.resolve_save_file(file_id)
        File_Utils.remove(save_path, silent=False, resolve=False)
        return current_save

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

    @property
    def map_proxy_url(self) -> str | None:
        return self._squaremap_proxy_url()

    @property
    def map_proxy_root_path(self) -> Path | None:
        if not self._has_squaremap_mod():
            return None
        root_path = self.directory / _SQUAREMAP_WEB_ROOT_RELATIVE_PATH
        return root_path if root_path.is_dir() else None

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

    def _squaremap_proxy_url(self) -> str | None:
        if not self._has_squaremap_mod():
            return None
        local_web_address = _load_squaremap_web_address(self.directory / _SQUAREMAP_CONFIG_RELATIVE_PATH)
        if local_web_address is not None:
            return local_web_address
        return self._squaremap_public_url()

    def _has_enabled_matching_mod(self, matcher: Callable[[str], bool]) -> bool:
        if self.mods is None:
            return False
        return any(matcher(mod.name) for mod in self.mods.list_mods(True))

    def _has_squaremap_mod(self) -> bool:
        return self._has_enabled_matching_mod(_is_squaremap_mod_name)

    def _has_enabled_kubejs_mod(self) -> bool:
        return self._has_enabled_matching_mod(_is_kubejs_mod_name)

    def _has_enabled_almost_unified_mod(self) -> bool:
        return self._has_enabled_matching_mod(_is_almost_unified_mod_name)

    def _kubejs_yuki_log_path(self) -> Path:
        return self.directory / _KUBEJS_SERVER_SCRIPTS_RELATIVE_PATH / _KUBEJS_YUKI_LOG_SCRIPT_NAME

    def _kubejs_yuki_recipes_path(self) -> Path:
        return self.directory / _KUBEJS_SERVER_SCRIPTS_RELATIVE_PATH / _KUBEJS_YUKI_RECIPES_SCRIPT_NAME

    def _kubejs_yuki_item_registry_script_path(self) -> Path:
        return self.directory / _KUBEJS_SERVER_SCRIPTS_RELATIVE_PATH / _KUBEJS_YUKI_ITEM_REGISTRY_SCRIPT_NAME

    def _yukibot_recipe_book_path(self) -> Path:
        return self.directory / _YUKIBOT_DATA_RELATIVE_PATH / _YUKIBOT_RECIPES_FILE_NAME

    def _legacy_yukibot_recipe_book_path(self) -> Path:
        return self.directory / _LEGACY_YUKIBOT_DATA_RELATIVE_PATH / _YUKIBOT_RECIPES_FILE_NAME

    def _yukibot_item_registry_path(self) -> Path:
        return self.directory / _YUKIBOT_REGISTRIES_RELATIVE_PATH / _YUKIBOT_ITEM_REGISTRY_FILE_NAME

    def _legacy_yukibot_item_registry_path(self) -> Path:
        return self.directory / _LEGACY_YUKIBOT_REGISTRIES_RELATIVE_PATH / _YUKIBOT_ITEM_REGISTRY_FILE_NAME

    def _yukibot_item_icon_directory(self) -> Path:
        return self.directory / _YUKIBOT_ITEM_ICONS_RELATIVE_PATH

    def _legacy_yukibot_item_icon_directory(self) -> Path:
        return self.directory / _LEGACY_YUKIBOT_ITEM_ICONS_RELATIVE_PATH

    def _yukibot_item_icon_path(self, item_id: str) -> Path:
        namespace, resource_path = _normalise_minecraft_resource_location(
            item_id,
            field_name="minecraft item icon id",
        ).split(":", maxsplit=1)
        return self._yukibot_item_icon_directory() / namespace / Path(*resource_path.split("/")).with_suffix(".png")

    def _legacy_yukibot_item_icon_path(self, item_id: str) -> Path:
        namespace, resource_path = _normalise_minecraft_resource_location(
            item_id,
            field_name="minecraft item icon id",
        ).split(":", maxsplit=1)
        return (
            self._legacy_yukibot_item_icon_directory() / namespace / Path(*resource_path.split("/")).with_suffix(".png")
        )

    def _resolve_existing_yukibot_data_path(self, *, current_path: Path, legacy_path: Path) -> Path:
        if current_path.exists():
            return current_path
        if legacy_path.exists():
            return legacy_path
        return current_path

    def _uses_kubejs_event_stream(self) -> bool:
        return (
            self._has_enabled_kubejs_mod()
            and self._kubejs_yuki_log_path().is_file()
            and getattr(self, "_kubejs_event_stream_ready", False)
        )

    def kubejs_recipe_support_status(self) -> KubeJsRecipeSupportStatus:
        script_path = self._kubejs_yuki_recipes_path()
        addons: list[KubeJsRecipeAddonCapability] = []
        seen_addons: set[KubeJsRecipeAddonKind] = set()
        if self.mods is not None:
            for mod in self.mods.list_mods(True):
                addon_kind = _detect_kubejs_recipe_addon_kind(mod.name)
                if addon_kind is None or addon_kind in seen_addons:
                    continue
                addons.append(KubeJsRecipeAddonCapability(kind=addon_kind, mod_name=mod.name))
                seen_addons.add(addon_kind)
        unification_mode = (
            MinecraftRecipeUnificationMode.EXPECTED_PRESENT
            if self._has_enabled_almost_unified_mod()
            else MinecraftRecipeUnificationMode.DISABLED
        )
        return KubeJsRecipeSupportStatus(
            kubejs_enabled=self._has_enabled_kubejs_mod(),
            script_path=script_path,
            script_exists=script_path.is_file(),
            addons=tuple(addons),
            unification_mode=unification_mode,
        )

    def load_kubejs_recipe_book(self) -> MinecraftRecipeBook:
        recipe_book_path = self._resolve_existing_yukibot_data_path(
            current_path=self._yukibot_recipe_book_path(),
            legacy_path=self._legacy_yukibot_recipe_book_path(),
        )
        if not recipe_book_path.exists():
            return MinecraftRecipeBook.empty()
        try:
            raw_payload: object = json.loads(recipe_book_path.read_text(config.STR_ENCODE))
        except json.JSONDecodeError as xcp:
            raise ValueError(f"Invalid Minecraft recipe book JSON at {recipe_book_path}: {xcp}") from xcp
        except OSError as xcp:
            raise ValueError(f"Unable to read Minecraft recipe book at {recipe_book_path}: {xcp}") from xcp
        return MinecraftRecipeBook.from_mapping(_recipe_mapping(raw_payload, label="Minecraft recipe book"))

    def save_kubejs_recipe_book(self, recipe_book: MinecraftRecipeBook) -> None:
        recipe_book_path = self._yukibot_recipe_book_path()
        try:
            _write_text_atomically(
                recipe_book_path,
                json.dumps(recipe_book.to_mapping(), indent=4) + "\n",
            )
        except OSError as xcp:
            raise ValueError(f"Unable to write Minecraft recipe book at {recipe_book_path}: {xcp}") from xcp

    def load_kubejs_item_registry(self) -> MinecraftItemRegistrySnapshot:
        item_registry_path = self._resolve_existing_yukibot_data_path(
            current_path=self._yukibot_item_registry_path(),
            legacy_path=self._legacy_yukibot_item_registry_path(),
        )
        if not item_registry_path.exists():
            return MinecraftItemRegistrySnapshot.empty()
        try:
            raw_payload: object = json.loads(item_registry_path.read_text(config.STR_ENCODE))
        except json.JSONDecodeError as xcp:
            raise ValueError(f"Invalid Minecraft item registry JSON at {item_registry_path}: {xcp}") from xcp
        except OSError as xcp:
            raise ValueError(f"Unable to read Minecraft item registry at {item_registry_path}: {xcp}") from xcp
        return MinecraftItemRegistrySnapshot.from_mapping(_recipe_mapping(raw_payload, label="Minecraft item registry"))

    def resolve_minecraft_item_icon_path(self, item_id: str) -> Path | None:
        normalised_item_id = _normalise_minecraft_resource_location(item_id, field_name="minecraft item icon id")
        current_cache_path = self._yukibot_item_icon_path(normalised_item_id)
        cached_path = self._resolve_existing_yukibot_data_path(
            current_path=current_cache_path,
            legacy_path=self._legacy_yukibot_item_icon_path(normalised_item_id),
        )
        if cached_path.is_file():
            return cached_path
        icon_bytes = self._load_minecraft_item_icon_source_bytes(normalised_item_id)
        if icon_bytes is None:
            return None
        current_cache_path.parent.mkdir(parents=True, exist_ok=True)
        current_cache_path.write_bytes(icon_bytes)
        return current_cache_path

    def _load_minecraft_item_icon_source_bytes(self, item_id: str) -> bytes | None:
        namespace, _separator, _resource_path = item_id.partition(":")
        candidate_asset_paths = self._minecraft_item_icon_candidate_asset_paths(item_id)
        for asset_root in self._minecraft_item_icon_loose_asset_roots():
            for asset_path in candidate_asset_paths:
                source_path = asset_root / Path(asset_path.as_posix())
                if source_path.is_file():
                    return source_path.read_bytes()
        archive_paths_by_namespace = self._minecraft_item_icon_archive_paths_by_namespace()
        for archive_path in archive_paths_by_namespace.get(namespace, ()):
            try:
                with zipfile.ZipFile(archive_path, "r") as archive:
                    for asset_path in candidate_asset_paths:
                        try:
                            with archive.open(asset_path.as_posix(), "r") as asset_file:
                                return asset_file.read()
                        except KeyError:
                            continue
            except OSError, zipfile.BadZipFile:
                continue
        return None

    @staticmethod
    def _minecraft_item_icon_candidate_asset_paths(item_id: str) -> tuple[PurePosixPath, ...]:
        namespace, resource_path = item_id.split(":", maxsplit=1)
        return (
            PurePosixPath("assets") / namespace / "textures" / "item" / f"{resource_path}.png",
            PurePosixPath("assets") / namespace / "textures" / "items" / f"{resource_path}.png",
            PurePosixPath("assets") / namespace / "textures" / "block" / f"{resource_path}.png",
            PurePosixPath("assets") / namespace / "textures" / "blocks" / f"{resource_path}.png",
        )

    def _minecraft_item_icon_loose_asset_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = [self.directory / "kubejs"]
        resourcepacks_path = self.directory / "resourcepacks"
        if resourcepacks_path.is_dir():
            roots.extend(pointer for pointer in sorted(resourcepacks_path.iterdir()) if pointer.is_dir())
        return tuple(root for root in roots if root.exists())

    def _minecraft_item_icon_archive_paths_by_namespace(self) -> dict[str, tuple[Path, ...]]:
        cached_mapping = getattr(self, "_minecraft_item_icon_archive_paths_by_namespace_cache", None)
        if isinstance(cached_mapping, dict):
            return cast(dict[str, tuple[Path, ...]], cached_mapping)
        cache_lock = getattr(self, "_minecraft_item_icon_archive_cache_lock", None)
        if not isinstance(cache_lock, threading.Lock):
            cache_lock = threading.Lock()
            setattr(self, "_minecraft_item_icon_archive_cache_lock", cache_lock)
        with cache_lock:
            cached_mapping = getattr(self, "_minecraft_item_icon_archive_paths_by_namespace_cache", None)
            if isinstance(cached_mapping, dict):
                return cast(dict[str, tuple[Path, ...]], cached_mapping)
            archive_paths_by_namespace: dict[str, list[Path]] = {}
            for archive_path in self._minecraft_item_icon_archive_candidates():
                try:
                    with zipfile.ZipFile(archive_path, "r") as archive:
                        namespaces = {
                            entry_parts[1]
                            for entry in archive.namelist()
                            for entry_parts in [entry.split("/", maxsplit=3)]
                            if len(entry_parts) >= 3 and entry_parts[0] == "assets" and entry_parts[1]
                        }
                except OSError, zipfile.BadZipFile:
                    continue
                for namespace in sorted(namespaces):
                    archive_paths_by_namespace.setdefault(namespace, []).append(archive_path)
            resolved_mapping = {namespace: tuple(paths) for namespace, paths in archive_paths_by_namespace.items()}
            setattr(self, "_minecraft_item_icon_archive_paths_by_namespace_cache", resolved_mapping)
            return resolved_mapping

    def _minecraft_item_icon_archive_candidates(self) -> tuple[Path, ...]:
        candidate_paths: list[Path] = []
        seen_paths: set[Path] = set()
        if self.mods is not None:
            for mod in self.mods.list_mods(True):
                mod_path = mod.path
                if mod_path.suffix.casefold() != ".jar" or not mod_path.is_file() or mod_path in seen_paths:
                    continue
                candidate_paths.append(mod_path)
                seen_paths.add(mod_path)
        resourcepacks_path = self.directory / "resourcepacks"
        if resourcepacks_path.is_dir():
            for archive_path in sorted(resourcepacks_path.iterdir()):
                if archive_path.suffix.casefold() != ".zip" or not archive_path.is_file() or archive_path in seen_paths:
                    continue
                candidate_paths.append(archive_path)
                seen_paths.add(archive_path)
        for archive_path in sorted(self.directory.glob("*.jar")):
            if not archive_path.is_file() or archive_path in seen_paths:
                continue
            candidate_paths.append(archive_path)
            seen_paths.add(archive_path)
        return tuple(candidate_paths)

    def append_kubejs_recipe_mutation(self, mutation: MinecraftRecipeMutation) -> MinecraftRecipeBook:
        recipe_book = self.load_kubejs_recipe_book()
        next_recipe_book = MinecraftRecipeBook(mutations=recipe_book.mutations + (mutation,))
        self._save_and_sync_kubejs_recipe_book(recipe_book, next_recipe_book)
        return next_recipe_book

    def replace_kubejs_recipe_mutation(self, index: int, mutation: MinecraftRecipeMutation) -> MinecraftRecipeBook:
        if index < 0:
            raise ValueError("Minecraft recipe mutation index must not be negative.")
        recipe_book = self.load_kubejs_recipe_book()
        if index >= len(recipe_book.mutations):
            raise IndexError(f"Unknown Minecraft recipe mutation index: {index}")
        next_mutations = list(recipe_book.mutations)
        next_mutations[index] = mutation
        next_recipe_book = MinecraftRecipeBook(mutations=tuple(next_mutations))
        self._save_and_sync_kubejs_recipe_book(recipe_book, next_recipe_book)
        return next_recipe_book

    def remove_kubejs_recipe_mutation(self, index: int) -> MinecraftRecipeBook:
        if index < 0:
            raise ValueError("Minecraft recipe mutation index must not be negative.")
        recipe_book = self.load_kubejs_recipe_book()
        if index >= len(recipe_book.mutations):
            raise IndexError(f"Unknown Minecraft recipe mutation index: {index}")
        next_recipe_book = MinecraftRecipeBook(
            mutations=tuple(
                mutation for mutation_index, mutation in enumerate(recipe_book.mutations) if mutation_index != index
            )
        )
        self._save_and_sync_kubejs_recipe_book(recipe_book, next_recipe_book)
        return next_recipe_book

    def _save_and_sync_kubejs_recipe_book(
        self,
        previous_recipe_book: MinecraftRecipeBook,
        next_recipe_book: MinecraftRecipeBook,
    ) -> None:
        status = self.kubejs_recipe_support_status()
        script_content = (
            _managed_kubejs_recipe_script_source(status, mutations=next_recipe_book.mutations)
            if status.kubejs_enabled
            else None
        )
        self.save_kubejs_recipe_book(next_recipe_book)
        if script_content is None:
            return
        try:
            self._write_kubejs_recipe_script(status.script_path, script_content)
        except Exception as xcp:
            try:
                self.save_kubejs_recipe_book(previous_recipe_book)
            except Exception as rollback_xcp:
                raise RuntimeError(
                    "Minecraft recipe script generation failed and the recipe book rollback also failed: "
                    f"{rollback_xcp}"
                ) from xcp
            raise OSError(f"Minecraft recipe script generation failed; recipe changes were rolled back: {xcp}") from xcp

    def _load_or_create_kubejs_recipe_book(self) -> MinecraftRecipeBook:
        recipe_book_path = self._yukibot_recipe_book_path()
        recipe_book = self.load_kubejs_recipe_book()
        if not recipe_book_path.exists():
            self.save_kubejs_recipe_book(recipe_book)
        return recipe_book

    def _migrate_legacy_yukibot_data(self) -> None:
        migration_pairs = (
            (self._legacy_yukibot_recipe_book_path(), self._yukibot_recipe_book_path()),
            (self._legacy_yukibot_item_registry_path(), self._yukibot_item_registry_path()),
        )
        for legacy_path, current_path in migration_pairs:
            if current_path.exists() or not legacy_path.exists():
                continue
            try:
                current_path.parent.mkdir(parents=True, exist_ok=True)
                current_path.write_text(legacy_path.read_text(config.STR_ENCODE), config.STR_ENCODE)
            except OSError as xcp:
                log.warning(
                    "Failed to migrate legacy YukiBot Minecraft data for %s from %s to %s: %s",
                    self.name,
                    legacy_path,
                    current_path,
                    xcp,
                )
                continue
            log.info("%s migrated legacy YukiBot Minecraft data: %s -> %s", self.name, legacy_path, current_path)

    def _sync_kubejs_yuki_log_script(self) -> bool:
        if not self._has_enabled_kubejs_mod():
            return False
        script_content = _bundled_kubejs_yuki_log_script_source()
        script_path = self._kubejs_yuki_log_path()
        try:
            script_path.parent.mkdir(parents=True, exist_ok=True)
            if script_path.exists() and script_path.read_text(config.STR_ENCODE) == script_content:
                return False
            script_path.write_text(script_content, config.STR_ENCODE)
        except OSError as xcp:
            log.warning("Failed to sync KubeJS relay script for %s at %s: %s", self.name, script_path, xcp)
            return False
        log.info("%s synced KubeJS relay script: %s", self.name, script_path)
        return True

    def _sync_kubejs_recipe_script(self) -> bool:
        status = self.kubejs_recipe_support_status()
        if not status.kubejs_enabled:
            return False
        recipe_book = self._load_or_create_kubejs_recipe_book()
        script_content = _managed_kubejs_recipe_script_source(status, mutations=recipe_book.mutations)
        try:
            changed = self._write_kubejs_recipe_script(status.script_path, script_content)
        except OSError as xcp:
            log.warning("Failed to sync KubeJS recipe script for %s at %s: %s", self.name, status.script_path, xcp)
            return False
        if changed:
            log.info("%s synced KubeJS recipe script: %s", self.name, status.script_path)
        return changed

    @staticmethod
    def _write_kubejs_recipe_script(script_path: Path, script_content: str) -> bool:
        if script_path.exists() and script_path.read_text(config.STR_ENCODE) == script_content:
            return False
        _write_text_atomically(script_path, script_content)
        return True

    def _sync_kubejs_item_registry_script(self) -> bool:
        if not self._has_enabled_kubejs_mod():
            return False
        script_content = _bundled_kubejs_yuki_item_registry_script_source()
        script_path = self._kubejs_yuki_item_registry_script_path()
        item_registry_path = self._yukibot_item_registry_path()
        try:
            script_path.parent.mkdir(parents=True, exist_ok=True)
            item_registry_path.parent.mkdir(parents=True, exist_ok=True)
            if script_path.exists() and script_path.read_text(config.STR_ENCODE) == script_content:
                return False
            script_path.write_text(script_content, config.STR_ENCODE)
        except OSError as xcp:
            log.warning("Failed to sync KubeJS item registry script for %s at %s: %s", self.name, script_path, xcp)
            return False
        log.info("%s synced KubeJS item registry script: %s", self.name, script_path)
        return True

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

    async def _stop_runtime_services(self) -> None:
        self._running = False
        await self._players.stop()
        await self._activities.stop()

    async def _stop_tailer(self) -> None:
        if self._tail is None:
            return
        await self._tail.stop()
        self._tail = None

    async def handle_unexpected_stop(self) -> None:
        await self._stop_runtime_services()
        await self._stop_tailer()
        await super().handle_unexpected_stop()

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        self.clear_runtime_fault()
        self._server_ready.clear()
        self._kubejs_event_stream_ready = False
        self._migrate_legacy_yukibot_data()
        self._sync_kubejs_yuki_log_script()
        self._sync_kubejs_recipe_script()
        self._sync_kubejs_item_registry_script()
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
        self.clear_runtime_fault()
        await self._stop_runtime_services()
        if not self._relay.is_connected:
            log.warning("%s shutdown is skipping graceful RCON stop because the relay is not connected.", self.name)
            await self._terminate()
            await self._stop_tailer()
            return True
        try:
            await self._relay.send("save-all")
            await asyncio.sleep(0.2)
            await self._relay.send("stop")
        except RuntimeError as xcp:
            log.warning("%s shutdown fell back to terminate because RCON was unavailable: %s", self.name, xcp)
            await self._terminate()
            await self._stop_tailer()
            return True
        for _ in range(10):
            if not self.process:
                await self._stop_tailer()
                return False
            if self.process and self.process.poll() is not None:
                log.info(f"{self.friendly} stopped gracefully.")
                self.process = None
                await self._stop_tailer()
                return False
            await asyncio.sleep(0.25)
        log.warning(f"{self.friendly} did not shut down in time. Forcing termination.")
        await self._terminate()
        await self._stop_tailer()
        return True

    async def kill(self) -> bool:
        self.clear_runtime_fault()
        await self._stop_runtime_services()
        await self._terminate()
        await self._stop_tailer()
        return True

    async def player_count(self):
        return await self._players.count()

    def connected_player_names(self) -> tuple[str, ...]:
        return self._players.connected_player_names()


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
        app._tail_machers.add(self.match_crash)
        app._tail_machers.add(self.match_kubejs_script_loaded)
        app._tail_machers.add(self.match_kubejs_event)
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

    async def match_crash(self, line: str) -> None:
        summary = _minecraft_crash_summary_from_log_line(line)
        if summary is None:
            return
        if self.app.record_runtime_fault(kind=AppRuntimeFaultKind.CRASH, summary=summary):
            log.warning("%s detected Minecraft crash signal: %s", self.app.name, summary)

    async def match_kubejs_script_loaded(self, line: str) -> None:
        if _KUBEJS_SCRIPT_LOADED_RE.search(line) is None:
            return
        if not self.app._has_enabled_kubejs_mod():
            return
        self.app._kubejs_event_stream_ready = True

    async def match_kubejs_event(self, line: str) -> None:
        if not self.app._uses_kubejs_event_stream():
            return
        event = _parse_kubejs_event(line)
        if event is None:
            return
        self.app._players.note_uuid(event.player, event.uuid or "")
        if event.event_type is KubeJsEventType.PLAYER_JOIN:
            self.app._players.note_join(event.player, source=RelayNoticeSource.APP_LOG)
            return
        if event.event_type is KubeJsEventType.PLAYER_LEAVE:
            self.app._players.note_leave(event.player, source=RelayNoticeSource.APP_LOG)
            return
        if event.event_type is KubeJsEventType.CHAT:
            assert event.message is not None
            content = event.message
            if "CICode" in content:
                content = CICODE_RE.sub(self._deCICodeify, content).strip()
            if not content or content.startswith(self.app.cfg.chat_ignore_symbol):
                return
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    content,
                    event.player,
                    player_avatar_uri=self._player_avatar_uri(event.player),
                )
            )
            return
        return

    async def match_chat(self, line: str):
        if self.app._uses_kubejs_event_stream():
            return
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
            raw_kind = match.group("kind")
            _validate_advancement_kind(raw_kind)
            advancement_type = self.app.relay_advancement_term
            advancement_title = match.group("title").strip()
            app_friendly = getattr(self.app, "friendly", self.app.name)
            notice = GameProgressNotice(
                progress_kind=_minecraft_progress_kind(raw_kind),
                label=advancement_type,
                title=advancement_title,
                source=RelayNoticeSource.APP_LOG,
            )
            embed_spec = notice_embed_spec(notice, app_name=app_friendly, author_name=player)
            relay_embed = (
                None
                if embed_spec is None
                else RelayEmbedPayload(
                    title=embed_spec.title,
                    description=embed_spec.description,
                    color=self.app.manage_embed_color,
                )
            )
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    f"{advancement_type}: {advancement_title}",
                    player,
                    relay_embed=relay_embed,
                    notice=notice,
                    player_avatar_uri=self._player_avatar_uri(player),
                )
            )

    async def match_death(self, line: str):
        if self.app.relay_notice_player_death_enabled is False:
            return
        if match := DEATH_RE.match(line):
            player, content = match.groups()
            content = _resolve_minecraft_death_mentions(content, app=self.app)
            notice = GameDeathNotice(
                death_kind=GameDeathKind.UNKNOWN,
                detail_text=content,
                source=RelayNoticeSource.APP_LOG,
            )
            DC_Relay.add(
                DC_Bound(
                    self.app,
                    content,
                    player,
                    notice=notice,
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
        if self.app._uses_kubejs_event_stream():
            return
        if match := JOIN_RE.match(line):
            player = match.group(1)
            self.app._players.note_join(player, source=RelayNoticeSource.APP_LOG)

    async def match_left(self, line: str):
        if self.app._uses_kubejs_event_stream():
            return
        if match := LEAVE_RE.match(line):
            player = match.group(1)
            self.app._players.note_leave(player, source=RelayNoticeSource.APP_LOG)

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
        task = self._players_task
        self._players_task = None
        await self.app._cancel_background_task(task, label="player poll task")

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

    def note_join(self, player: str, *, source: RelayNoticeSource = RelayNoticeSource.APP_POLL) -> None:
        if player in self._players:
            return
        self._players.add(player)
        if self.app.relay_notice_player_joined_enabled is False:
            return
        relay_player_id = _resolve_minecraft_player_user_id(player, app=self.app)
        notice = PlayerSessionNotice(action=PlayerSessionAction.JOINED, source=source)
        app_friendly = getattr(self.app, "friendly", self.app.name)
        DC_Relay.add(
            DC_Bound(
                self.app,
                render_notice_text(notice, author_name=player, app_name=app_friendly),
                player,
                notice=notice,
                player_id=relay_player_id,
                player_avatar_uri=self.avatar_uri(player),
            )
        )

    def note_leave(self, player: str, *, source: RelayNoticeSource = RelayNoticeSource.APP_POLL) -> None:
        if player not in self._players:
            return
        relay_player_id = _resolve_minecraft_player_user_id(player, app=self.app)
        self._players.discard(player)
        if self.app.relay_notice_player_left_enabled is False:
            return
        notice = PlayerSessionNotice(action=PlayerSessionAction.LEFT, source=source)
        app_friendly = getattr(self.app, "friendly", self.app.name)
        DC_Relay.add(
            DC_Bound(
                self.app,
                render_notice_text(notice, author_name=player, app_name=app_friendly),
                player,
                notice=notice,
                player_id=relay_player_id,
                player_avatar_uri=self.avatar_uri(player),
            )
        )

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

    def connected_player_names(self) -> tuple[str, ...]:
        return tuple[str, ...](sorted(self._players, key=str.casefold))


class Activities:
    def __init__(self, app: Minecraft):
        self.app = app
        self._time_task: asyncio.Task[None] | None = None
        self._running = False
        self.providers: list[AppActivityProvider[Minecraft]] = [Provider_Day(app)]
        self.app.set_activity_providers(self.providers)
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self):
        self.tasks = {task for task in self.tasks if not task.done()}
        if self.tasks:
            return
        self._running = True
        self.app.register_enabled_activity_providers()
        for prov in self.providers:
            self.tasks.update(asyncio.create_task(func()) for func in prov.task_funcs)

    async def stop(self):
        self._running = False
        self.app.deregister_activity_providers()
        tasks = tuple(self.tasks)
        self.tasks.clear()
        for task in tasks:
            await self.app._cancel_background_task(task, label="activity task")


class Provider_Day(AppActivityProvider[Minecraft]):
    metadata = AppActivityProviderMetadata(provider_id="day", label="Day Counter")

    def __init__(self, app: Minecraft):
        super().__init__(app)
        self._timedelta = None
        self._count = 0
        self.task_funcs = (self._get_time,)

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
