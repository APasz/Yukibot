import asyncio
import contextlib
import hashlib
import json
import logging
import re
import signal
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from re import Match, Pattern
from typing import Any, Generic, TypeAlias, TypeVar, cast, overload
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
import hikari
from modmux import Muxer, parse_url
from modmux.models import ModID, Provider
from modmux.modmux_errors import ModMuxError

import config
from _async_utils import run_blocking
from _discord import (
    App_Bound,
    DC_Bound,
    DC_Relay,
    OutboundRelayFormatter,
    RelayEmbedPayload,
    RelayOutboundFormatOptions,
    render_plain_reference_prefix,
)
from _file import File_Utils
from _security import Power_Level, _owner_group
from apps._app import (
    AM_Receiver,
    App,
    AppActivityProvider,
    AppActivityProviderMetadata,
    AppVersionSource,
    RelayAdvancementTerms,
)
from apps._config import (
    App_Config,
    AppVersion,
    FactorioUpdateBranch,
    FactorioUpdateConfig,
    KnownModPageProvider,
    Mod_Config,
    ModPageLink,
    known_mod_page_provider_for_url,
)
from apps._config_files import AppConfigFileKind, AppConfigFileRoot
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult, ConsoleExecutor, ConsoleResponseSource
from apps._mod import Mod, humanise_mod_identifier
from apps._rcon import RconClient
from apps._save_files import (
    AppSaveEntry,
    AppSaveRoot,
    AppSaveRootMode,
    describe_app_save_path,
    get_app_save_root,
    normalise_app_save_relative_path,
)
from apps._settings import (
    App_Settings,
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    ForcedSettingState,
    IntSettingSpec,
    Setting,
    Setting_Label,
    SettingSpec,
    SettingStateForceRule,
    StringSettingSpec,
)
from apps._tailer import Tailer
from apps._updater import (
    AppUpdateBranchState,
    AppUpdateInfo,
    AppUpdateOperationKind,
    AppUpdateOperationResult,
    AppUpdateProviderKind,
    AppUpdateState,
    AppUpdateStatus,
    Update_Manager,
)
from config import Activity_Manager
from relay_notices import (
    GameDeathKind,
    GameDeathNotice,
    GameProgressKind,
    GameProgressNotice,
    PlayerSessionAction,
    RelayNoticeSource,
    notice_embed_spec,
    render_notice_text,
)

log = logging.getLogger(__name__)

_FACTORIO_VERSION_RE: Pattern[str] = re.compile(r"Factorio (?P<version>\d+\.\d+\.\d+)")
_FACTORIO_INFO_JSON_NAME = "info.json"
_FACTORIO_IGNORED_MOD_FILES: frozenset[str] = frozenset({"mod-list.json", "mod-settings.dat"})
_FACTORIO_MOD_VERSION_RE: Pattern[str] = re.compile(
    r"_(?P<version>v?\d+(?:\.\d+)+(?:[-+._][A-Za-z0-9]+)*)$",
    re.IGNORECASE,
)
_FACTORIO_MOD_PORTAL_HOST = "mods.factorio.com"
_FACTORIO_MOD_PORTAL_BASE_URL = f"https://{_FACTORIO_MOD_PORTAL_HOST}"
_FACTORIO_MOD_PORTAL_ID_RE: Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")
_FACTORIO_MOD_DEPENDENCY_RE: Pattern[str] = re.compile(r"^(?P<mod_id>[A-Za-z0-9_-]+)(?=\s|[<>=!~]|$)")
_FACTORIO_CONFIG_FILENAMES: tuple[str, ...] = (
    "server-settings.json",
    "map-settings.json",
    "map-gen-settings.json",
)
_FACTORIO_MOD_SETTINGS_FILENAME = "mod-settings.dat"
_FACTORIO_STOP_SAVE_GRACE_SECONDS = 1.0
_FACTORIO_GRACEFUL_STOP_TIMEOUT_SECONDS = 30.0
_FACTORIO_RESEARCH_FINISHED_RE: Pattern[str] = re.compile(r"\[RESEARCH FINISHED\]\s+(?P<research>.+)$", re.IGNORECASE)
_FACTORIO_YUKI_BRIDGE_EVENT_RE: Pattern[str] = re.compile(r"\[Yuki\]\s+(?P<payload>\{.+\})\s*$")
_FACTORIO_ERROR_RE: Pattern[str] = re.compile(r"^\s*\d+\.\d+\s+Error\s+(?P<source>\S+:\d+):\s+(?P<message>.+)$")
_FACTORIO_MAP_AGE_PART_RE: Pattern[str] = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_FACTORIO_EVOLUTION_FACTOR_RE: Pattern[str] = re.compile(
    r"evolution factor:\s*(?P<value>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_FACTORIO_SURFACE_HEADER_RE: Pattern[str] = re.compile(r"^(?P<surface>[^:]+):\s*$")
_FACTORIO_INLINE_SURFACE_EVOLUTION_RE: Pattern[str] = re.compile(
    r"^(?P<surface>.+?)\s*:\s*evolution factor:\s*(?P<value>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_FACTORIO_COMMAND_FAILURE_RE: Pattern[str] = re.compile(
    r"\b(?:unknown command|cannot execute command|unknown option|failed|error|usage)\b",
    re.IGNORECASE,
)
_FACTORIO_PLAYER_JOIN_EVENT_NAME = "PlayerJoinGame"
_FACTORIO_PLAYER_EVENT_NAME_RE: Pattern[str] = re.compile(
    rf"\b{_FACTORIO_PLAYER_JOIN_EVENT_NAME}\b",
    re.IGNORECASE,
)
_FACTORIO_YUKI_BRIDGE_MOD_ID = "yuki-bridge"
_FACTORIO_YUKI_BRIDGE_EVENTS_PATH = Path("script-output") / "yuki" / "events.ndjson"
_FACTORIO_LINE_MATCHER = Callable[[str], Awaitable[None]]
_FactorioSettingValue = TypeVar("_FactorioSettingValue", str, bool, int)
FactorioRconCommand: TypeAlias = str | dict[str, str]
FactorioRconResponse: TypeAlias = str | dict[str, str | None] | None
_FACTORIO_UPDATE_BRANCHES: tuple[FactorioUpdateBranch, ...] = (
    FactorioUpdateBranch.STABLE,
    FactorioUpdateBranch.EXPERIMENTAL,
)


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    mapping = cast(Mapping[object, object], value)
    result: dict[str, object] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings.")
        result[key] = item
    return result


def _load_json_object(raw: str | bytes, *, label: str) -> dict[str, object]:
    return _json_object(cast(object, json.loads(raw)), label=label)


def _optional_factorio_metadata_text(
    payload: Mapping[str, object],
    field_name: str,
    *,
    label: str,
) -> str | None:
    raw_value = payload.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError(f"{label} {field_name} must be a string.")
    value = raw_value.strip()
    return value or None


@dataclass(frozen=True, slots=True)
class FactorioModMetadata:
    name: str | None = None
    version: str | None = None
    title: str | None = None
    homepage: str | None = None
    description: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, label: str) -> "FactorioModMetadata":
        return cls(
            name=_optional_factorio_metadata_text(payload, "name", label=label),
            version=_optional_factorio_metadata_text(payload, "version", label=label),
            title=_optional_factorio_metadata_text(payload, "title", label=label),
            homepage=_optional_factorio_metadata_text(payload, "homepage", label=label),
            description=_optional_factorio_metadata_text(payload, "description", label=label),
        )


@dataclass(frozen=True, slots=True)
class FactorioModPortalCredentials:
    username: str
    token: str


@dataclass(frozen=True, slots=True)
class FactorioVanillaMod:
    name: str
    title: str
    version: str | None


@dataclass(frozen=True, slots=True)
class FactorioModPortalRelease:
    download_url: str
    file_name: str
    version: str
    sha1: str
    released_at: str | None
    factorio_version: str | None
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactorioModPortalReleaseOption:
    version: str
    file_name: str
    released_at: str | None
    factorio_version: str | None


@dataclass(frozen=True, slots=True)
class FactorioModPortalDownload:
    mod_id: str
    page_url: str
    file_name: str
    version: str
    archive_path: Path


@dataclass(frozen=True, slots=True)
class FactorioModPortalCandidate:
    mod_id: str
    title: str
    page_url: str
    file_name: str
    version: str
    required_by: tuple[str, ...]
    dependency_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactorioModPortalResolution:
    requested_mod_id: str
    candidates: tuple[FactorioModPortalCandidate, ...]


@dataclass(frozen=True, slots=True)
class FactorioMapAge:
    total_seconds: int

    def __post_init__(self) -> None:
        if self.total_seconds < 0:
            raise ValueError("Factorio map age total_seconds must be non-negative.")

    @property
    def days(self) -> int:
        return self.total_seconds // 86_400

    @property
    def hours(self) -> int:
        return (self.total_seconds % 86_400) // 3_600

    def activity_text(self) -> str:
        return f"D{self.days}/H{self.hours:02d}"


@dataclass(frozen=True, slots=True)
class FactorioEvolution:
    factor: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.factor <= 1.0:
            raise ValueError("Factorio evolution factor must be between 0.0 and 1.0.")

    def activity_text(self) -> str:
        percentage = round(self.factor * 100.0, 1)
        if percentage.is_integer():
            return f"{int(percentage)}%"
        return f"{percentage:.1f}%"


@dataclass(frozen=True, slots=True)
class FactorioSurfaceEvolution:
    surface_name: str
    evolution: FactorioEvolution

    def __post_init__(self) -> None:
        if not self.surface_name.strip():
            raise ValueError("Factorio surface evolution surface_name must not be blank.")

    def detail_text(self) -> str:
        return f"{self.surface_name}: {self.evolution.activity_text()}"


@dataclass(frozen=True, slots=True)
class FactorioActivitySnapshot:
    map_age: FactorioMapAge | None = None
    primary_evolution: FactorioEvolution | None = None
    surface_evolutions: tuple[FactorioSurfaceEvolution, ...] = ()


def _is_non_empty_text(text: str) -> bool:
    return bool(text.strip())


def _is_positive_int_text(text: str) -> bool:
    return text.isdigit() and int(text) > 0


def factorio_server_settings_path(directory: Path) -> Path:
    return directory.absolute() / "data" / "server-settings.json"


def factorio_mod_settings_path(directory: Path) -> Path:
    return directory.absolute() / "mods" / _FACTORIO_MOD_SETTINGS_FILENAME


def factorio_vanilla_mods(data_dir: Path) -> Mapping[str, FactorioVanillaMod]:
    if not data_dir.exists():
        return {}
    if not data_dir.is_dir():
        raise ValueError(f"Factorio data path is not a directory: {data_dir}")
    mods: dict[str, FactorioVanillaMod] = {}
    for info_path in sorted(data_dir.glob("*/info.json")):
        payload = _load_json_object(info_path.read_text(config.STR_ENCODE), label=f"{info_path} metadata")
        raw_name = payload.get("name")
        if not isinstance(raw_name, str) or not _FACTORIO_MOD_PORTAL_ID_RE.fullmatch(raw_name.strip()):
            raise ValueError(f"Factorio vanilla mod metadata name is invalid: {info_path}")
        name = raw_name.strip()
        raw_title = payload.get("title")
        title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else name
        raw_version = payload.get("version")
        version = raw_version.strip() if isinstance(raw_version, str) and raw_version.strip() else None
        mods[name] = FactorioVanillaMod(name=name, title=title, version=version)
    return mods


def factorio_config_path(directory: Path, filename: str) -> Path:
    if filename not in _FACTORIO_CONFIG_FILENAMES:
        raise ValueError(f"Unsupported Factorio config filename: {filename}")
    return directory.absolute() / "data" / filename


def _factorio_example_config_path(config_path: Path) -> Path:
    if config_path.suffix != ".json":
        raise ValueError(f"Factorio config file must be JSON: {config_path}")
    return config_path.with_name(f"{config_path.stem}.example{config_path.suffix}")


def ensure_factorio_config_files(directory: Path) -> tuple[Path, ...]:
    copied: list[Path] = []
    for filename in _FACTORIO_CONFIG_FILENAMES:
        config_path = factorio_config_path(directory, filename)
        if config_path.exists():
            continue
        example_path = _factorio_example_config_path(config_path)
        if not example_path.exists():
            continue
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example_path, config_path)
        copied.append(config_path)
        log.info("Created Factorio config from example: %s -> %s", example_path, config_path)
    return tuple(copied)


def factorio_mod_portal_credentials_from_server_settings(settings_path: Path) -> FactorioModPortalCredentials:
    settings = _load_json_object(settings_path.read_text(config.STR_ENCODE), label="Factorio server settings")
    raw_username = settings.get("username")
    raw_token = settings.get("token")
    if not isinstance(raw_username, str) or not isinstance(raw_token, str):
        raise ValueError("Factorio server settings username and token are required to download mods.")
    username = raw_username.strip()
    token = raw_token.strip()
    if not username or not token:
        raise ValueError("Factorio server settings username and token are required to download mods.")
    return FactorioModPortalCredentials(username=username, token=token)


def detect_factorio_version(*, directory: Path) -> AppVersion | None:
    info_path: Path = directory / "data" / "base" / _FACTORIO_INFO_JSON_NAME
    if info_path.exists():
        try:
            payload = _load_json_object(info_path.read_text(config.STR_ENCODE), label=f"{info_path} metadata")
            raw_version = payload.get("version")
            if isinstance(raw_version, str) and raw_version.strip():
                return AppVersion(main=raw_version.strip())
        except (OSError, ValueError) as xcp:
            log.warning("Failed to inspect Factorio base metadata %s: %s", info_path, xcp)
    log_file: Path = directory / "factorio-current.log"
    if not log_file.exists():
        return None
    try:
        for line in log_file.read_text(config.STR_ENCODE, errors="ignore").splitlines():
            if match := _FACTORIO_VERSION_RE.search(line):
                return AppVersion(main=match.group("version").strip())
    except OSError as xcp:
        log.warning("Failed to inspect Factorio log %s: %s", log_file, xcp)
    return None


def _parse_factorio_version_text(version_text: str, *, label: str) -> tuple[int, ...]:
    text: str = version_text.strip()
    if not text:
        raise ValueError(f"{label} must not be empty.")
    parts: list[str] = text.split(".")
    if not all(part.isdecimal() for part in parts):
        raise ValueError(f"{label} is invalid: {version_text!r}")
    return tuple[int, ...](map(int, parts))


def _factorio_latest_headless_versions(payload: Mapping[str, object]) -> dict[FactorioUpdateBranch, tuple[int, ...]]:
    versions: dict[FactorioUpdateBranch, tuple[int, ...]] = {}
    for branch in _FACTORIO_UPDATE_BRANCHES:
        branch_payload: dict[str, object] = _json_object(
            payload.get(branch.value), label=f"Factorio latest releases.{branch.value}"
        )
        raw_version: object | None = branch_payload.get("headless")
        if not isinstance(raw_version, str):
            raise ValueError(f"Factorio latest releases {branch.value}.headless version is invalid.")
        versions[branch] = _parse_factorio_version_text(
            raw_version,
            label=f"Factorio latest releases {branch.value}.headless version",
        )
    return versions


def _factorio_download_url(branch: FactorioUpdateBranch) -> str:
    if branch is FactorioUpdateBranch.STABLE:
        return "https://factorio.com/get-download/stable/headless/linux64"
    return "https://factorio.com/get-download/experimental/headless/linux64"


def parse_factorio_mod_portal_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        raise ValueError("Factorio mod portal URL must not be empty.")
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != _FACTORIO_MOD_PORTAL_HOST:
        raise ValueError("Factorio mod links must use https://mods.factorio.com/mod/{name}.")
    path_parts = tuple(part for part in parsed.path.split("/") if part)
    if len(path_parts) < 2 or path_parts[0] != "mod":
        raise ValueError("Factorio mod links must use https://mods.factorio.com/mod/{name}.")
    mod_id = path_parts[1]
    if not _FACTORIO_MOD_PORTAL_ID_RE.fullmatch(mod_id):
        raise ValueError(f"Factorio mod ID is invalid: {mod_id!r}")
    return mod_id


def _normalise_factorio_mod_portal_path(raw_path: object, *, field_name: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"Factorio mod portal release {field_name} is invalid.")
    path = raw_path.strip()
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        raise ValueError(f"Factorio mod portal release {field_name} must be a relative absolute path.")
    pure_path = PurePosixPath(parsed.path)
    if pure_path.is_absolute() and ".." not in pure_path.parts:
        return path
    raise ValueError(f"Factorio mod portal release {field_name} is invalid.")


def _normalise_factorio_mod_portal_file_name(raw_name: object) -> str:
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("Factorio mod portal release file_name is invalid.")
    file_name = raw_name.strip()
    if PurePosixPath(file_name).name != file_name or file_name in {".", ".."}:
        raise ValueError("Factorio mod portal release file_name must be a single file name.")
    if not file_name.endswith(".zip"):
        raise ValueError("Factorio mod portal release file_name must be a zip archive.")
    return file_name


def _normalise_factorio_mod_portal_sha1(raw_sha1: object) -> str:
    if not isinstance(raw_sha1, str):
        raise ValueError("Factorio mod portal release sha1 is invalid.")
    sha1 = raw_sha1.strip().casefold()
    if len(sha1) != 40 or any(character not in "0123456789abcdef" for character in sha1):
        raise ValueError("Factorio mod portal release sha1 must be a 40-character hexadecimal digest.")
    return sha1


def _factorio_required_dependency_mod_id(raw_dependency: object) -> str | None:
    if not isinstance(raw_dependency, str):
        raise ValueError("Factorio mod portal release dependency is invalid.")
    dependency = raw_dependency.strip()
    if not dependency:
        return None
    if dependency.startswith(("?", "+", "!", "(?)")):
        return None
    if dependency.startswith("~"):
        dependency = dependency.removeprefix("~").strip()
    match = _FACTORIO_MOD_DEPENDENCY_RE.match(dependency)
    if match is None:
        raise ValueError(f"Factorio mod portal release dependency is invalid: {raw_dependency!r}")
    mod_id = match.group("mod_id")
    if mod_id == "base":
        return None
    return mod_id


def _factorio_required_dependencies(info_json: Mapping[str, object]) -> tuple[str, ...]:
    raw_dependencies = info_json.get("dependencies")
    if raw_dependencies is None:
        return ()
    if not isinstance(raw_dependencies, list):
        raise ValueError("Factorio mod portal release dependencies are invalid.")
    dependencies: list[str] = []
    seen: set[str] = set()
    for raw_dependency in raw_dependencies:
        mod_id = _factorio_required_dependency_mod_id(raw_dependency)
        if mod_id is None or mod_id in seen:
            continue
        seen.add(mod_id)
        dependencies.append(mod_id)
    return tuple(dependencies)


def _factorio_mod_portal_release_from_mapping(payload: object) -> FactorioModPortalRelease:
    release = _json_object(payload, label="Factorio mod portal release")
    raw_version = release.get("version")
    if not isinstance(raw_version, str) or not raw_version.strip():
        raise ValueError("Factorio mod portal release version is invalid.")
    info_json = _json_object(release.get("info_json", {}), label="Factorio mod portal release info_json")
    raw_factorio_version = info_json.get("factorio_version")
    if raw_factorio_version is not None and not isinstance(raw_factorio_version, str):
        raise ValueError("Factorio mod portal release info_json factorio_version is invalid.")
    raw_released_at = release.get("released_at")
    if raw_released_at is not None and not isinstance(raw_released_at, str):
        raise ValueError("Factorio mod portal release released_at is invalid.")
    return FactorioModPortalRelease(
        download_url=_normalise_factorio_mod_portal_path(release.get("download_url"), field_name="download_url"),
        file_name=_normalise_factorio_mod_portal_file_name(release.get("file_name")),
        version=raw_version.strip(),
        sha1=_normalise_factorio_mod_portal_sha1(release.get("sha1")),
        released_at=raw_released_at.strip() if isinstance(raw_released_at, str) and raw_released_at.strip() else None,
        factorio_version=(
            raw_factorio_version.strip()
            if isinstance(raw_factorio_version, str) and raw_factorio_version.strip()
            else None
        ),
        dependencies=_factorio_required_dependencies(info_json),
    )


def _factorio_mod_portal_releases(payload: Mapping[str, object]) -> tuple[FactorioModPortalRelease, ...]:
    raw_releases = payload.get("releases")
    if not isinstance(raw_releases, list):
        raise ValueError("Factorio mod portal response releases are invalid.")
    releases = tuple(_factorio_mod_portal_release_from_mapping(item) for item in cast(list[object], raw_releases))
    if not releases:
        raise ValueError("Factorio mod portal response contains no releases.")
    return releases


def _factorio_mod_release_matches_game_version(
    release: FactorioModPortalRelease,
    factorio_version: AppVersion | None,
) -> bool:
    if factorio_version is None or release.factorio_version is None:
        return True
    installed_parts = factorio_version.main.split(".")
    if len(installed_parts) < 2:
        return True
    return release.factorio_version == ".".join(installed_parts[:2])


def _select_factorio_mod_portal_release(
    releases: Iterable[FactorioModPortalRelease],
    *,
    factorio_version: AppVersion | None,
    requested_version: str | None = None,
) -> FactorioModPortalRelease:
    release_list = tuple(releases)
    requested = requested_version.strip() if requested_version is not None else ""
    if requested:
        matching = tuple(release for release in release_list if release.version == requested)
        if not matching:
            raise ValueError(f"Factorio mod portal release version was not found: {requested}.")
        compatible_matching = tuple(
            release for release in matching if _factorio_mod_release_matches_game_version(release, factorio_version)
        )
        if not compatible_matching:
            raise ValueError(
                f"Factorio mod portal release {requested} is not compatible with this Factorio version."
            )
        return max(compatible_matching, key=lambda release: release.released_at or "")
    compatible = tuple(
        release for release in release_list if _factorio_mod_release_matches_game_version(release, factorio_version)
    )
    candidates = compatible or release_list
    return max(candidates, key=lambda release: release.released_at or "")


def _factorio_mod_download_url(release: FactorioModPortalRelease, credentials: FactorioModPortalCredentials) -> str:
    query = urlencode({"username": credentials.username, "token": credentials.token})
    separator = "&" if "?" in release.download_url else "?"
    return f"{_FACTORIO_MOD_PORTAL_BASE_URL}{release.download_url}{separator}{query}"


async def _factorio_mod_portal_metadata(session: aiohttp.ClientSession, mod_id: str) -> Mapping[str, object]:
    metadata_url = f"{_FACTORIO_MOD_PORTAL_BASE_URL}/api/mods/{quote(mod_id, safe='')}/full"
    async with session.get(metadata_url) as response:
        if response.status != 200:
            raise RuntimeError(f"Factorio mod portal metadata fetch failed for {mod_id} with HTTP {response.status}.")
        payload = _json_object(cast(object, await response.json()), label="Factorio mod portal response")
    portal_mod_name = payload.get("name")
    if portal_mod_name != mod_id:
        raise ValueError("Factorio mod portal response did not match the requested mod.")
    return payload


def _factorio_mod_portal_candidate(
    *,
    payload: Mapping[str, object],
    release: FactorioModPortalRelease,
    required_by: Iterable[str],
) -> FactorioModPortalCandidate:
    raw_mod_id = payload.get("name")
    if not isinstance(raw_mod_id, str) or not raw_mod_id.strip():
        raise ValueError("Factorio mod portal response name is invalid.")
    mod_id = raw_mod_id.strip()
    raw_title = payload.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else mod_id
    return FactorioModPortalCandidate(
        mod_id=mod_id,
        title=title,
        page_url=f"{_FACTORIO_MOD_PORTAL_BASE_URL}/mod/{quote(mod_id, safe='')}",
        file_name=release.file_name,
        version=release.version,
        required_by=tuple(required_by),
        dependency_ids=release.dependencies,
    )


async def _resolve_factorio_mod_portal_candidates_with_session(
    *,
    session: aiohttp.ClientSession,
    requested_mod_id: str,
    factorio_version: AppVersion | None,
    requested_mod_version: str | None,
) -> tuple[FactorioModPortalResolution, dict[str, FactorioModPortalRelease]]:
    pending: deque[str] = deque([requested_mod_id])
    seen: set[str] = set()
    ordered_ids: list[str] = []
    payloads: dict[str, Mapping[str, object]] = {}
    releases: dict[str, FactorioModPortalRelease] = {}
    required_by_ids: dict[str, set[str]] = {requested_mod_id: set()}

    while pending:
        mod_id = pending.popleft()
        if mod_id in seen:
            continue
        seen.add(mod_id)
        payload = await _factorio_mod_portal_metadata(session, mod_id)
        release = _select_factorio_mod_portal_release(
            _factorio_mod_portal_releases(payload),
            factorio_version=factorio_version,
            requested_version=requested_mod_version if mod_id == requested_mod_id else None,
        )
        ordered_ids.append(mod_id)
        payloads[mod_id] = payload
        releases[mod_id] = release
        for dependency_id in release.dependencies:
            required_by_ids.setdefault(dependency_id, set()).add(mod_id)
            if dependency_id not in seen:
                pending.append(dependency_id)

    candidates = tuple(
        _factorio_mod_portal_candidate(
            payload=payloads[mod_id],
            release=releases[mod_id],
            required_by=tuple(sorted(required_by_ids.get(mod_id, set()))),
        )
        for mod_id in ordered_ids
    )

    return FactorioModPortalResolution(requested_mod_id=requested_mod_id, candidates=candidates), releases


async def resolve_factorio_mod_portal_candidates(
    *,
    page_url: str,
    factorio_version: AppVersion | None,
    requested_mod_version: str | None = None,
) -> FactorioModPortalResolution:
    mod_id = parse_factorio_mod_portal_url(page_url)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        resolution, _releases = await _resolve_factorio_mod_portal_candidates_with_session(
            session=session,
            requested_mod_id=mod_id,
            factorio_version=factorio_version,
            requested_mod_version=requested_mod_version,
        )
    return resolution


async def list_factorio_mod_portal_release_options(
    *,
    page_url: str,
    factorio_version: AppVersion | None,
) -> tuple[FactorioModPortalReleaseOption, ...]:
    mod_id = parse_factorio_mod_portal_url(page_url)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        payload = await _factorio_mod_portal_metadata(session, mod_id)
    releases = tuple(
        release
        for release in _factorio_mod_portal_releases(payload)
        if _factorio_mod_release_matches_game_version(release, factorio_version)
    )
    sorted_releases = tuple(sorted(releases, key=lambda release: release.released_at or "", reverse=True))
    return tuple(
        FactorioModPortalReleaseOption(
            version=release.version,
            file_name=release.file_name,
            released_at=release.released_at,
            factorio_version=release.factorio_version,
        )
        for release in sorted_releases
    )


def _verify_factorio_mod_download_sha1(archive_path: Path, expected_sha1: str) -> None:
    digest = hashlib.sha1()
    with archive_path.open("rb") as archive_file:
        for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha1 = digest.hexdigest()
    if actual_sha1 != expected_sha1:
        raise ValueError(
            f"Downloaded Factorio mod checksum mismatch for {archive_path.name}: "
            f"expected {expected_sha1}, got {actual_sha1}."
        )


async def download_factorio_mod_from_portal(
    *,
    page_url: str,
    destination_dir: Path,
    factorio_version: AppVersion | None,
    credentials: FactorioModPortalCredentials,
    requested_mod_version: str | None = None,
) -> FactorioModPortalDownload:
    downloads = await download_factorio_mods_from_portal(
        page_url=page_url,
        destination_dir=destination_dir,
        factorio_version=factorio_version,
        credentials=credentials,
        selected_mod_ids=None,
        requested_mod_version=requested_mod_version,
    )
    if len(downloads) != 1:
        raise RuntimeError("Single Factorio mod download unexpectedly resolved multiple archives.")
    return downloads[0]


async def download_factorio_mods_from_portal(
    *,
    page_url: str,
    destination_dir: Path,
    factorio_version: AppVersion | None,
    credentials: FactorioModPortalCredentials,
    selected_mod_ids: Iterable[str] | None,
    requested_mod_version: str | None = None,
) -> tuple[FactorioModPortalDownload, ...]:
    mod_id = parse_factorio_mod_portal_url(page_url)
    selected_ids: set[str] | None = None if selected_mod_ids is None else set(selected_mod_ids)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        resolution, releases = await _resolve_factorio_mod_portal_candidates_with_session(
            session=session,
            requested_mod_id=mod_id,
            factorio_version=factorio_version,
            requested_mod_version=requested_mod_version,
        )
        if selected_ids is None:
            selected_ids = {mod_id}
        if mod_id not in selected_ids:
            raise ValueError("The requested Factorio mod must be included in the selected downloads.")
        available_ids = {candidate.mod_id for candidate in resolution.candidates}
        unknown_ids = selected_ids - available_ids
        if unknown_ids:
            raise ValueError(f"Unknown Factorio mod dependency selection: {', '.join(sorted(unknown_ids))}")

        downloads: list[FactorioModPortalDownload] = []
        for candidate in resolution.candidates:
            if candidate.mod_id not in selected_ids:
                continue
            release = releases[candidate.mod_id]
            archive_path = destination_dir / release.file_name
            try:
                async with session.get(
                    _factorio_mod_download_url(release, credentials), allow_redirects=True
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"Factorio mod download failed for {candidate.mod_id} with HTTP {response.status}."
                        )
                    with archive_path.open("wb") as archive_file:
                        async for chunk in response.content.iter_chunked(262_144):
                            archive_file.write(chunk)
                _verify_factorio_mod_download_sha1(archive_path, release.sha1)
            except Exception:
                File_Utils.remove(archive_path, silent=True, resolve=False)
                raise
            downloads.append(
                FactorioModPortalDownload(
                    mod_id=candidate.mod_id,
                    page_url=candidate.page_url,
                    file_name=release.file_name,
                    version=release.version,
                    archive_path=archive_path,
                )
            )
    return tuple(downloads)


def _normalise_factorio_archive_member_path(member_name: str) -> Path:
    pure_path: PurePosixPath = PurePosixPath(member_name)
    if not member_name or pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError(f"Factorio archive member path is invalid: {member_name}")
    return Path(*pure_path.parts)


def _factorio_download_archive_path(*, tmp_dir: Path, branch: FactorioUpdateBranch, version_text: str) -> Path:
    return tmp_dir / f"factorio-{branch.value}-{version_text}.tar.xz"


def _ensure_factorio_binary_executable(binary_path: Path) -> None:
    if not binary_path.is_file():
        raise FileNotFoundError(f"Factorio binary does not exist: {binary_path}")
    current_mode = binary_path.stat().st_mode
    if current_mode & 0o111:
        return
    binary_path.chmod(current_mode | 0o111)
    log.warning("Restored execute permissions for Factorio binary: %s", binary_path)


@dataclass(frozen=True, slots=True)
class FactorioModListEntry:
    name: str
    enabled: bool

    @classmethod
    def from_mapping(cls, payload: object) -> "FactorioModListEntry":
        entry: dict[str, object] = _json_object(payload, label="Factorio mod-list entry")
        name: object | None = entry.get("name")
        enabled: object | None = entry.get("enabled")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Factorio mod-list entry name is invalid.")
        if not isinstance(enabled, bool):
            raise ValueError("Factorio mod-list entry enabled state is invalid.")
        return cls(name=name.strip(), enabled=enabled)

    def to_mapping(self) -> dict[str, object]:
        return {"name": self.name, "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class FactorioModList:
    mods: tuple[FactorioModListEntry, ...]

    @classmethod
    def load(cls, pointer: Path) -> "FactorioModList":
        if not pointer.exists():
            return cls(mods=())
        payload = _load_json_object(pointer.read_text(config.STR_ENCODE), label="Factorio mod-list")
        raw_mods = payload.get("mods")
        if not isinstance(raw_mods, list):
            raise ValueError("Factorio mod-list mods are invalid.")
        return cls(mods=tuple(FactorioModListEntry.from_mapping(item) for item in cast(list[object], raw_mods)))

    def state_for(self, mod_id: str) -> bool | None:
        for entry in self.mods:
            if entry.name == mod_id:
                return entry.enabled
        return None

    def set_enabled(self, mod_id: str, enabled: bool) -> "FactorioModList":
        updated = list(self.mods)
        for index, entry in enumerate(updated):
            if entry.name != mod_id:
                continue
            updated[index] = FactorioModListEntry(name=mod_id, enabled=enabled)
            break
        else:
            updated.append(FactorioModListEntry(name=mod_id, enabled=enabled))
        return FactorioModList(mods=tuple(updated))

    def remove(self, mod_id: str) -> "FactorioModList":
        return FactorioModList(mods=tuple(entry for entry in self.mods if entry.name != mod_id))

    def save(self, pointer: Path) -> None:
        payload = {"mods": [entry.to_mapping() for entry in self.mods]}
        pointer.write_text(json.dumps(payload, indent=4) + "\n", config.STR_ENCODE)


def _factorio_mod_metadata_from_archive(pointer: Path) -> FactorioModMetadata | None:
    if not pointer.exists():
        return None
    if pointer.is_dir():
        info_pointer = pointer / _FACTORIO_INFO_JSON_NAME
        if not info_pointer.exists():
            return None
        label = f"Factorio info.json {info_pointer}"
        payload = _load_json_object(
            info_pointer.read_text(config.STR_ENCODE),
            label=label,
        )
        return FactorioModMetadata.from_mapping(payload, label=label)

    if pointer.suffix.lower() != ".zip":
        return None

    with zipfile.ZipFile(pointer, "r") as archive:
        for member_name in archive.namelist():
            normalised = member_name.rstrip("/")
            if normalised == _FACTORIO_INFO_JSON_NAME or normalised.endswith(f"/{_FACTORIO_INFO_JSON_NAME}"):
                label = f"Factorio archive info.json {pointer}"
                payload = _load_json_object(
                    archive.read(member_name),
                    label=label,
                )
                return FactorioModMetadata.from_mapping(payload, label=label)
    return None


def _factorio_mod_name_from_path(pointer: Path) -> str:
    metadata = _factorio_mod_metadata_from_archive(pointer)
    if metadata is not None and metadata.name is not None:
        return metadata.name
    stem = pointer.stem if pointer.is_file() else pointer.name
    if "_" not in stem:
        return stem
    return stem.rsplit("_", 1)[0]


def _factorio_mod_version_from_name(name: str) -> str | None:
    match = _FACTORIO_MOD_VERSION_RE.search(Path(name).stem)
    if match is None:
        return None
    return match.group("version").removeprefix("v")


def _factorio_mod_page(raw_url: str | None) -> ModPageLink | None:
    if raw_url is None:
        return None
    mod_id = parse_url(raw_url)
    if mod_id is None or mod_id.provider is not Provider.WUBE:
        return None
    canonical_url = f"https://mods.factorio.com/mod/{quote(mod_id.id, safe='')}"
    return ModPageLink(name=KnownModPageProvider.FACTORIO_MODS.value, url=canonical_url)


async def _find_factorio_mod_page_via_modmux(*, mod_id: str, muxer: Muxer) -> ModPageLink:
    resolved_mod = await muxer.get_mod(
        Provider.WUBE,
        ModID(provider=Provider.WUBE, id=mod_id),
        author_resolution=False,
    )
    resolved_id = resolved_mod.slug or resolved_mod.id.id
    page = _factorio_mod_page(f"https://mods.factorio.com/mod/{resolved_id}")
    if page is None:
        raise ValueError(f"modmux returned an invalid Factorio mod ID: {resolved_id!r}")
    return page


class Mod_Factorio(Mod):
    def __init__(self, cfg: Mod_Config):
        self._detected_metadata = FactorioModMetadata()
        super().__init__(cfg)

    @classmethod
    def iter_candidates(cls, folder: Path) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for pointer in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if pointer.name in _FACTORIO_IGNORED_MOD_FILES:
                continue
            if pointer.is_dir():
                if (pointer / _FACTORIO_INFO_JSON_NAME).exists():
                    candidates.append(pointer)
                continue
            if pointer.is_file() and pointer.suffix.lower() == ".zip":
                candidates.append(pointer)
        return tuple(candidates)

    @property
    def disabled_path(self) -> Path:
        return self.enabled_path

    @property
    def mod_list_path(self) -> Path:
        return self.directory / "mod-list.json"

    @property
    def factorio_mod_id(self) -> str:
        return self._detected_metadata.name or _factorio_mod_name_from_path(self.path)

    def exists(self) -> bool:
        if self.name in _FACTORIO_IGNORED_MOD_FILES:
            return False
        return self.enabled_path.is_dir() or (
            self.enabled_path.is_file() and self.enabled_path.suffix.lower() == ".zip"
        )

    def _load_mod_list(self) -> FactorioModList:
        return FactorioModList.load(self.mod_list_path)

    def _save_mod_list(self, mod_list: FactorioModList) -> None:
        mod_list.save(self.mod_list_path)

    def sync_enabled_state(self) -> None:
        mod_state = self._load_mod_list().state_for(self.factorio_mod_id)
        if mod_state is not None:
            self.cfg.enabled = mod_state

    def detect_version(self) -> str | None:
        return self._detected_metadata.version or _factorio_mod_version_from_name(self.name)

    def detect_friendly(self) -> str | None:
        if self._detected_metadata.title is not None:
            return self._detected_metadata.title
        return humanise_mod_identifier(self.factorio_mod_id, split_single_camel=True)

    def detect_description(self) -> str | None:
        return self._detected_metadata.description

    def native_metadata_id(self) -> str:
        return self.factorio_mod_id

    def metadata_fallback_id(self) -> str:
        return self.factorio_mod_id.casefold()

    def detect_mod_page(self) -> ModPageLink | None:
        return _factorio_mod_page(self._detected_metadata.homepage)

    def _has_factorio_mod_page(self) -> bool:
        return any(
            known_mod_page_provider_for_url(page.url) is KnownModPageProvider.FACTORIO_MODS
            for page in self.cfg.mod_pages
        )

    def _add_factorio_mod_page(self, page: ModPageLink) -> None:
        if not self._has_factorio_mod_page():
            self.cfg.mod_pages = (*self.cfg.mod_pages, page)

    def sync_metadata(self) -> None:
        self._detected_metadata = _factorio_mod_metadata_from_archive(self.path) or FactorioModMetadata()
        super().sync_metadata()
        detected_page = self.detect_mod_page()
        if detected_page is not None:
            self._add_factorio_mod_page(detected_page)

    @classmethod
    async def sync_external_metadata_batch(cls, mods: Iterable[Mod]) -> None:
        factorio_mods = tuple(
            mod for mod in mods if isinstance(mod, cls) and not mod._has_factorio_mod_page()
        )
        if not factorio_mods:
            return

        async with Muxer() as muxer:
            async def resolve_page(mod: "Mod_Factorio") -> None:
                try:
                    page = await _find_factorio_mod_page_via_modmux(mod_id=mod.factorio_mod_id, muxer=muxer)
                except (ModMuxError, ValueError) as xcp:
                    log.warning("Factorio mod page lookup failed for %s: %s", mod.name, xcp)
                    return
                mod._add_factorio_mod_page(page)

            await asyncio.gather(*(resolve_page(mod) for mod in factorio_mods))

    async def install(self, src: Path, atomic: bool = True):
        await self._handle_drop(src, atomic)
        await run_blocking(self._set_mod_list_enabled, True)

    async def uninstall(self, override_coremod: bool = False) -> bool:
        removed = await super().uninstall(override_coremod)
        await run_blocking(self._remove_mod_list_entry)
        return removed

    async def _enable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await run_blocking(self._set_mod_list_enabled, True)
        self.cfg.enabled = True
        return self.path

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        await run_blocking(self._set_mod_list_enabled, False)
        self.cfg.enabled = False
        return self.path

    def _set_mod_list_enabled(self, enabled: bool) -> None:
        mod_list = self._load_mod_list()
        self._save_mod_list(mod_list.set_enabled(self.factorio_mod_id, enabled))

    def _remove_mod_list_entry(self) -> None:
        mod_list = self._load_mod_list()
        self._save_mod_list(mod_list.remove(self.factorio_mod_id))


@dataclass(frozen=True, slots=True)
class FactorioSettingDefinition(Generic[_FactorioSettingValue]):
    label: str | Setting_Label
    key: str
    path: tuple[str, ...]
    spec: SettingSpec[_FactorioSettingValue]
    default: _FactorioSettingValue
    power_level: Power_Level = Power_Level.admin
    desc: str | None = None
    paragraph: bool = False
    comment_key: str | None = None
    prefer_comment_desc: bool = False
    forced_state_rules: tuple[SettingStateForceRule, ...] = ()

    def create_setting(self) -> Setting[_FactorioSettingValue]:
        return Setting(
            self.spec,
            self.label,
            self.key,
            self.path,
            default=self.default,
            power_level=self.power_level,
            desc=self.desc,
            paragraph=self.paragraph,
            forced_state_rules=self.forced_state_rules,
        )

    @property
    def comment_lookup_key(self) -> str:
        if self.comment_key is not None:
            return self.comment_key
        return self.path[0] if self.path else self.key


def _normalise_factorio_comment(raw_comment: object) -> str | None:
    if isinstance(raw_comment, str):
        text: str = raw_comment.strip()
        return text or None
    if isinstance(raw_comment, list):
        lines: list[str] = []
        for item in cast(list[object], raw_comment):
            text = str(item).strip()
            if text:
                lines.append(text)
        if lines:
            return " ".join(lines)
    return None


_FACTORIO_PUBLIC_VISIBILITY_CHOICES: ChoiceSpec = ChoiceSpec(
    ChoiceOption("true", "Public"),
    ChoiceOption("false", "Private"),
)
_FactorioSettingDefinitionItem: TypeAlias = (
    FactorioSettingDefinition[str] | FactorioSettingDefinition[bool] | FactorioSettingDefinition[int]
)

_FACTORIO_SETTING_DEFINITIONS: tuple[_FactorioSettingDefinitionItem, ...] = (
    FactorioSettingDefinition[str](
        Setting_Label.serv_name,
        "name",
        (),
        StringSettingSpec(allow_blank=True),
        "",
    ),
    FactorioSettingDefinition[str](
        Setting_Label.serv_desc,
        "description",
        (),
        StringSettingSpec(allow_blank=True),
        "",
        paragraph=True,
    ),
    FactorioSettingDefinition[int](
        Setting_Label.max_player,
        "max_players",
        (),
        IntSettingSpec(min_value=0),
        0,
        power_level=Power_Level.sudo,
    ),
    FactorioSettingDefinition[bool](
        Setting_Label.visibility,
        "public",
        ("visibility",),
        BoolSettingSpec(_FACTORIO_PUBLIC_VISIBILITY_CHOICES),
        False,
        comment_key="visibility",
        forced_state_rules=(
            SettingStateForceRule(
                True,
                ForcedSettingState("require_user_verification", True),
            ),
        ),
    ),
    FactorioSettingDefinition[str](
        Setting_Label.password,
        "game_password",
        (),
        StringSettingSpec(
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.user,
        ),
        "",
        power_level=Power_Level.sudo,
    ),
    FactorioSettingDefinition[bool](
        "Require User Verification",
        "require_user_verification",
        (),
        BoolSettingSpec(),
        False,
    ),
    FactorioSettingDefinition[int](
        "Max Upload (KiB/s)",
        "max_upload_in_kilobytes_per_second",
        (),
        IntSettingSpec(),
        0,
    ),
    FactorioSettingDefinition[int](
        "Max Upload Slots",
        "max_upload_slots",
        (),
        IntSettingSpec(),
        5,
    ),
    FactorioSettingDefinition[int](
        "Minimum Latency Ticks",
        "minimum_latency_in_ticks",
        (),
        IntSettingSpec(),
        0,
    ),
    FactorioSettingDefinition[int](
        "Max Heartbeats / Second",
        "max_heartbeats_per_second",
        (),
        IntSettingSpec(
            min_value=6,
            max_value=240,
        ),
        60,
    ),
    FactorioSettingDefinition[int](
        "Autosave Interval",
        "autosave_interval",
        (),
        IntSettingSpec(),
        10,
    ),
    FactorioSettingDefinition[bool](
        "Ignore Returning Player Limit",
        "ignore_player_limit_for_returning_players",
        (),
        BoolSettingSpec(),
        False,
    ),
    FactorioSettingDefinition[int](
        "AFK Autokick Interval",
        "afk_autokick_interval",
        (),
        IntSettingSpec(),
        0,
    ),
    FactorioSettingDefinition[bool](
        "Auto Pause",
        "auto_pause",
        (),
        BoolSettingSpec(),
        True,
    ),
    FactorioSettingDefinition[bool](
        "Pause On Connect",
        "auto_pause_when_players_connect",
        (),
        BoolSettingSpec(),
        False,
    ),
    FactorioSettingDefinition[bool](
        "Admins Pause Only",
        "only_admins_can_pause_the_game",
        (),
        BoolSettingSpec(),
        True,
    ),
    FactorioSettingDefinition[bool](
        "Autosave Server Only",
        "autosave_only_on_server",
        (),
        BoolSettingSpec(),
        True,
    ),
    FactorioSettingDefinition[str](
        "Factorio Username",
        "username",
        (),
        StringSettingSpec(
            allow_blank=True,
            do_hide=Power_Level.admin,
        ),
        "",
        power_level=Power_Level.root,
        desc="Factorio account username used for mod portal authentication.",
        comment_key="credentials",
    ),
    FactorioSettingDefinition[str](
        "Factorio Password",
        "password",
        (),
        StringSettingSpec(
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.root,
        ),
        "",
        power_level=Power_Level.root,
        desc="Factorio account password used for mod portal authentication.",
        comment_key="credentials",
        prefer_comment_desc=True,
    ),
    FactorioSettingDefinition[str](
        "Factorio Token",
        "token",
        (),
        StringSettingSpec(
            allow_blank=True,
            is_sensitive=True,
            do_hide=Power_Level.root,
        ),
        "",
        power_level=Power_Level.root,
    ),
    FactorioSettingDefinition[bool](
        "Non Blocking Saving",
        "non_blocking_saving",
        (),
        BoolSettingSpec(),
        False,
        power_level=Power_Level.root,
        desc="On UNIX systems, server will fork itself to create an autosave.",
    ),
)


class Factorio_Settings(App_Settings):
    def __init__(self, pointer: Path, *, version_getter: Callable[[], AppVersion | None] | None = None) -> None:
        self._definitions: tuple[_FactorioSettingDefinitionItem, ...] = _FACTORIO_SETTING_DEFINITIONS
        super().__init__(
            pointer, [definition.create_setting() for definition in self._definitions], version_getter=version_getter
        )

    def _apply_descriptions(self, data: dict[str, object]) -> None:
        for definition in self._definitions:
            setting: Setting[object] | None = self.get_setting(definition.key)
            if setting is None:
                raise ValueError(f"Missing Factorio setting definition for {definition.key}")
            comment_desc = _normalise_factorio_comment(data.get(f"_comment_{definition.comment_lookup_key}"))
            if definition.prefer_comment_desc and comment_desc is not None:
                setting.desc = comment_desc
                continue
            if definition.desc is not None:
                setting.desc = definition.desc
                continue
            setting.desc = comment_desc

    def load(self) -> None:
        data = _load_json_object(self.pointer.read_text(config.STR_ENCODE), label="Factorio server settings")
        self._apply_descriptions(data)
        for opt in self.options:
            opt.get(data)

    def save(self) -> dict[str, object]:
        data = _load_json_object(self.pointer.read_text(config.STR_ENCODE), label="Factorio server settings")
        for opt in self.options:
            opt.set(data)

        string: str = json.dumps(data, indent=4)
        self.pointer.write_text(string, config.STR_ENCODE)
        return data


@dataclass(frozen=True, slots=True)
class FactorioKickRequest:
    player: str
    reason: str


def _parse_factorio_kick_request(value: object) -> FactorioKickRequest:
    raw_value = str(value).strip()
    player, separator, reason = raw_value.partition(" ")
    if not player.strip() or not separator or not reason.strip():
        raise ValueError("Kick requires a player and reason, for example `Alice griefing`.")
    return FactorioKickRequest(player=player.strip(), reason=reason.strip())


async def _run_factorio_console_command(
    app: "Factorio",
    command: str,
    *,
    success_text: str,
) -> ConsoleActionResult:
    response = await app.send_player_gated_rcon(command, check_player_gate=False)
    if response:
        return ConsoleActionResult(
            summary=success_text,
            text=response,
            source=ConsoleResponseSource.RCON,
        )
    return ConsoleActionResult(summary=success_text, source=ConsoleResponseSource.RCON)


def _static_factorio_console_executor(command: str, *, success_text: str) -> ConsoleExecutor:
    async def _execute(app_obj: object, value: object | None) -> ConsoleActionResult:
        del value
        app = cast(Factorio, app_obj)
        return await _run_factorio_console_command(
            app,
            command,
            success_text=f"{app.friendly}: {success_text}",
        )

    return _execute


def _value_factorio_console_executor(
    command_builder: Callable[[object], str],
    success_text_builder: Callable[["Factorio", object], str],
) -> ConsoleExecutor:
    async def _execute(app_obj: object, value: object | None) -> ConsoleActionResult:
        if value is None:
            raise ValueError("Console action value is required.")
        app = cast(Factorio, app_obj)
        return await _run_factorio_console_command(
            app,
            command_builder(value),
            success_text=success_text_builder(app, value),
        )

    return _execute


async def _console_raw_command(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(Factorio, app_obj)
    command = cast(str, value)
    return await _run_factorio_console_command(
        app,
        command,
        success_text=f"{app.friendly}: console command sent.",
    )


async def _console_promote(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(Factorio, app_obj)
    player = cast(str, value)
    return await _run_factorio_console_command(
        app,
        f"/promote {player}",
        success_text=f"{app.friendly}: promotion requested for `{player}`.",
    )


async def _console_demote(app_obj: object, value: object | None) -> ConsoleActionResult:
    app = cast(Factorio, app_obj)
    player = cast(str, value)
    return await _run_factorio_console_command(
        app,
        f"/demote {player}",
        success_text=f"{app.friendly}: demotion requested for `{player}`.",
    )


async def _console_admins(app_obj: object, value: object | None) -> ConsoleActionResult:
    del value
    app = cast(Factorio, app_obj)
    return await _run_factorio_console_command(
        app,
        "/admins",
        success_text=f"{app.friendly}: admin list requested.",
    )


_FACTORIO_PLAYER_PARAMETER = ConsoleActionParameter[str](
    key="player",
    label="Player",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Factorio player name to target.",
    max_length=100,
)
_FACTORIO_RAW_COMMAND_PARAMETER = ConsoleActionParameter[str](
    key="command",
    label="Command",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Raw Factorio console command sent through RCON.",
    max_length=500,
    multiline=True,
)
_FACTORIO_PERF_AVG_FRAMES_PARAMETER = ConsoleActionParameter[int](
    key="frames",
    label="Frames",
    value_type=int,
    validator=_is_positive_int_text,
    desc="Number of ticks/updates used to average performance counters.",
    max_length=6,
)
_FACTORIO_KICK_PARAMETER = ConsoleActionParameter[FactorioKickRequest](
    key="kick",
    label="Player And Reason",
    value_type=_parse_factorio_kick_request,
    validator=_is_non_empty_text,
    desc="Player name followed by kick reason, for example `Alice griefing`.",
    max_length=300,
)
_FACTORIO_MESSAGE_PARAMETER = ConsoleActionParameter[str](
    key="message",
    label="Message",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Message to send to all players.",
    max_length=300,
    multiline=True,
)
_FACTORIO_CHEAT_PARAMETER = ConsoleActionParameter[str](
    key="mode",
    label="Mode",
    value_type=str,
    choices=ChoiceSpec(ChoiceOption("all", "All"), ChoiceOption("off", "Off"), strict=False),
    validator=_is_non_empty_text,
    desc="Cheat mode target: all, off, or a planet/platform name.",
    max_length=100,
)
_FACTORIO_LUA_COMMAND_PARAMETER = ConsoleActionParameter[str](
    key="lua",
    label="Lua Command",
    value_type=str,
    validator=_is_non_empty_text,
    desc="Lua command body to execute.",
    max_length=1000,
    multiline=True,
)
_FACTORIO_CONSOLE_ACTIONS: tuple[ConsoleAction, ...] = (
    ConsoleAction(
        key="admins",
        label="List Admins",
        description="List the current game admins.",
        power_level=Power_Level.user,
        execute=_console_admins,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="seed",
        label="Seed",
        description="Print the starting map seed.",
        power_level=Power_Level.user,
        execute=_static_factorio_console_executor("/seed", success_text="map seed requested."),
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="time",
        label="Map Time",
        description="Print how old the map is.",
        power_level=Power_Level.user,
        execute=_static_factorio_console_executor("/time", success_text="map time requested."),
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="perf_avg_frames",
        label="Perf Avg Frames",
        description="Set the averaging window for performance counters.",
        power_level=Power_Level.user,
        execute=_value_factorio_console_executor(
            lambda value: f"/perf-avg-frames {cast(int, value)}",
            lambda app, value: f"{app.friendly}: performance counter averaging set to {cast(int, value)} frames.",
        ),
        parameter=_FACTORIO_PERF_AVG_FRAMES_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="shout",
        label="Shout",
        description="Send a message to all players, including other forces.",
        power_level=Power_Level.user,
        execute=_value_factorio_console_executor(
            lambda value: f"/shout {cast(str, value)}",
            lambda app, _value: f"{app.friendly}: shout sent.",
        ),
        parameter=_FACTORIO_MESSAGE_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="raw_command",
        label="Run Command",
        description="Send a raw command to the Factorio console.",
        power_level=Power_Level.sudo,
        execute=_console_raw_command,
        parameter=_FACTORIO_RAW_COMMAND_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="server_save",
        label="Server Save",
        description="Save the game on the server.",
        power_level=Power_Level.sudo,
        execute=_static_factorio_console_executor("/server-save", success_text="server save requested."),
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="promote",
        label="Promote Player",
        description="Grant admin access to a player.",
        power_level=Power_Level.sudo,
        execute=_console_promote,
        parameter=_FACTORIO_PLAYER_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="demote",
        label="Demote Player",
        description="Remove admin access from a player.",
        power_level=Power_Level.sudo,
        execute=_console_demote,
        parameter=_FACTORIO_PLAYER_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="kick",
        label="Kick Player",
        description="Kick a player with a reason.",
        power_level=Power_Level.sudo,
        execute=_value_factorio_console_executor(
            lambda value: (
                f"/kick {cast(FactorioKickRequest, value).player} {cast(FactorioKickRequest, value).reason}"
            ),
            lambda app, value: (
                f"{app.friendly}: kick requested for `{cast(FactorioKickRequest, value).player}`."
            ),
        ),
        parameter=_FACTORIO_KICK_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="cheat",
        label="Cheat",
        description="Enable cheat mode/research or disable cheat mode.",
        power_level=Power_Level.sudo,
        execute=_value_factorio_console_executor(
            lambda value: f"/cheat {cast(str, value)}",
            lambda app, value: f"{app.friendly}: cheat command requested for `{cast(str, value)}`.",
        ),
        parameter=_FACTORIO_CHEAT_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="command",
        label="Lua Command",
        description="Execute a Lua command.",
        power_level=Power_Level.sudo,
        execute=_value_factorio_console_executor(
            lambda value: f"/command {cast(str, value)}",
            lambda app, _value: f"{app.friendly}: Lua command requested.",
        ),
        parameter=_FACTORIO_LUA_COMMAND_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
    ConsoleAction(
        key="silent_command",
        label="Silent Lua Command",
        description="Execute a Lua command without printing it to the console.",
        power_level=Power_Level.sudo,
        execute=_value_factorio_console_executor(
            lambda value: f"/silent-command {cast(str, value)}",
            lambda app, _value: f"{app.friendly}: silent Lua command requested.",
        ),
        parameter=_FACTORIO_LUA_COMMAND_PARAMETER,
        transport=ConsoleResponseSource.RCON,
    ),
)


class Factorio(App[App_Config]):
    _instance = None
    chat_relay_outbound = True
    relay_advancement_terms = RelayAdvancementTerms("Research", "Research")
    relay_notice_player_session_supported = True
    relay_notice_player_death_supported = True
    relay_notice_progress_supported = True
    rcon_requires_online_players_default = True

    def __init__(self, bot: hikari.GatewayBot, am: Activity_Manager, cfg: App_Config):
        self.manage_embed_color = 0xDC6B0F
        self.proc_name = "factorio"
        self.proc_cmd = [self.proc_name, "--start-server"]
        ensure_factorio_config_files(cfg.directory)
        file_settings: Path = factorio_server_settings_path(cfg.directory)
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
        super().__init__(
            bot, am, cfg, Factorio_Settings(file_settings, version_getter=lambda: cfg.version), Mod_Factorio
        )
        self.act_err_threshold = 100
        self._lock: Path = self.directory / ".lock"

        self.updater = Factorio_Updater(self, base=True)
        self.apply_version(detect_factorio_version(directory=cfg.directory), persist=False)

        self._relay: RconClient = RconClient(self.check_running, 27015)
        self._tail: Tailer | None = None
        self._tail_machers: set[_FACTORIO_LINE_MATCHER] = set()
        self._bridge_events_tail: Tailer | None = None
        self._bridge_tail_matchers: set[_FACTORIO_LINE_MATCHER] = set()
        self._startup_error: str | None = None
        self._players: Players = Players(self)
        self._activities: FactorioActivities = FactorioActivities(self)
        self.am_receiver = Receiver(self)
        self._matchers: Matchers = Matchers(self)

        try:
            settings = _load_json_object(
                file_settings.read_text(config.STR_ENCODE),
                label="Factorio server settings",
            )
            serv_name = settings.get("name")
            if serv_name is not None:
                if not isinstance(serv_name, str):
                    raise ValueError("Factorio server setting `name` must be a string.")
                if serv_name.strip():
                    self.cfg.provider_alt_text = serv_name.strip()
        except Exception:
            log.exception(f"{__name__} Read Settings")

        log.debug(f"{__name__}.Created")

    async def post_init(self) -> None:
        await super().post_init()
        self._activities.configure_providers()

    @property
    def console_actions(self) -> tuple[ConsoleAction, ...]:
        return self.available_console_actions(_FACTORIO_CONSOLE_ACTIONS)

    async def ensure_console_action_allowed(self, action: ConsoleAction) -> None:
        del action

    @property
    def yuki_bridge_enabled(self) -> bool:
        override = getattr(self, "_factorio_yuki_bridge_enabled", None)
        if isinstance(override, bool):
            return override
        mod_manager = getattr(self, "mods", None)
        if mod_manager is None:
            return False
        for mod in mod_manager.list_mods():
            if not isinstance(mod, Mod_Factorio):
                continue
            if mod.factorio_mod_id.casefold() != _FACTORIO_YUKI_BRIDGE_MOD_ID:
                continue
            return mod.server_loadable and mod.cfg.enabled
        return False

    @property
    def bridge_events_path(self) -> Path:
        return self.directory / _FACTORIO_YUKI_BRIDGE_EVENTS_PATH

    @property
    def bridge_events_tail_active(self) -> bool:
        return getattr(self, "_bridge_events_tail", None) is not None

    @property
    def activity_providers(self) -> tuple[AppActivityProvider[Any], ...]:
        activities = getattr(self, "_activities", None)
        if activities is not None:
            activities.configure_providers()
        return super().activity_providers

    @property
    def relay_notice_player_death_enabled(self) -> bool | None:
        if not self.yuki_bridge_enabled:
            return None
        return super().relay_notice_player_death_enabled

    def apply_relay_notice_player_death_enabled(self, enabled: bool) -> None:
        if not self.yuki_bridge_enabled:
            raise ValueError(f"{self.friendly} requires {_FACTORIO_YUKI_BRIDGE_MOD_ID} for death notices.")
        super().apply_relay_notice_player_death_enabled(enabled)

    @property
    def relay_notice_progress_enabled(self) -> bool | None:
        if not self.yuki_bridge_enabled:
            return None
        return super().relay_notice_progress_enabled

    def apply_relay_notice_progress_enabled(self, enabled: bool) -> None:
        if not self.yuki_bridge_enabled:
            raise ValueError(
                f"{self.friendly} requires {_FACTORIO_YUKI_BRIDGE_MOD_ID} for {self.relay_progress_notice_term.lower()} notices."
            )
        super().apply_relay_notice_progress_enabled(enabled)

    @property
    def config_file_roots(self) -> tuple[AppConfigFileRoot, ...]:
        return (
            AppConfigFileRoot(
                id="server",
                label="Server Settings",
                path=factorio_config_path(self.directory, "server-settings.json"),
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".json"}),
            ),
            AppConfigFileRoot(
                id="map-settings",
                label="Map Settings",
                path=factorio_config_path(self.directory, "map-settings.json"),
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".json"}),
                read_power_level_override=Power_Level.sudo,
                write_power_level_override=Power_Level.sudo,
            ),
            AppConfigFileRoot(
                id="map-gen-settings",
                label="Map Gen Settings",
                path=factorio_config_path(self.directory, "map-gen-settings.json"),
                kind=AppConfigFileKind.GAME,
                recursive=False,
                suffixes=frozenset[str]({".json"}),
                read_power_level_override=Power_Level.sudo,
                write_power_level_override=Power_Level.sudo,
            ),
        )

    @property
    def save_file_roots(self) -> tuple[AppSaveRoot, ...]:
        return (
            AppSaveRoot(
                id="saves",
                label="Saves",
                path=self.directory / "saves",
                mode=AppSaveRootMode.CHILDREN,
                recursive=False,
                suffixes=frozenset[str]({".zip"}),
                include_files=True,
                include_directories=False,
            ),
        )

    @property
    def supports_save_uploads(self) -> bool:
        return True

    @property
    def supports_save_rename(self) -> bool:
        return True

    @property
    def supports_save_delete(self) -> bool:
        return True

    def upload_save_file(self, *, root_id: str, upload_name: str, source_path: Path) -> AppSaveEntry:
        root = get_app_save_root(self.save_file_roots, root_id)
        relative_path = normalise_app_save_relative_path(upload_name)
        if Path(relative_path).name != relative_path:
            raise ValueError("Factorio save upload name must not include directories.")
        if Path(relative_path).suffix.casefold() != ".zip":
            raise ValueError("Factorio save uploads must be .zip files.")
        target_dir = root.resolved_path
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / relative_path
        File_Utils.copy(source_path, target, overwrite=True)
        return describe_app_save_path(root=root, path=target, relative_path=relative_path)

    def relocate_save_file(
        self,
        *,
        save_id: str,
        destination_root_id: str,
        destination_relative_path: str,
    ) -> AppSaveEntry:
        source_root = get_app_save_root(self.save_file_roots, "saves")
        if destination_root_id != source_root.id:
            raise ValueError("Factorio saves can only be renamed within the saves root.")
        relative_path = normalise_app_save_relative_path(destination_relative_path)
        if Path(relative_path).name != relative_path:
            raise ValueError("Factorio save name must not include directories.")
        if Path(relative_path).suffix.casefold() != ".zip":
            raise ValueError("Factorio save names must use the .zip suffix.")
        source = self.resolve_save_file(save_id)
        if not source.exists():
            raise FileNotFoundError(f"Save file does not exist: {Path(save_id).name}")
        target = source_root.resolved_path / relative_path
        if target == source:
            return describe_app_save_path(root=source_root, path=source, relative_path=relative_path)
        if target.exists():
            raise FileExistsError(f"Save file already exists: {relative_path}")
        File_Utils.move(source, target, overwrite=False)
        return describe_app_save_path(root=source_root, path=target, relative_path=relative_path)

    def delete_save_file(self, *, file_id: str) -> AppSaveEntry:
        if self.check_running():
            raise ValueError("Stop the server before deleting saves.")
        source_root = get_app_save_root(self.save_file_roots, "saves")
        try:
            current_save = next(save for save in self.list_save_files() if save.id == file_id)
        except StopIteration as xcp:
            raise FileNotFoundError(f"Unknown save file: {file_id}") from xcp
        save_path = self.resolve_save_file(file_id)
        if not save_path.exists():
            raise FileNotFoundError(f"Save file does not exist: {Path(file_id).name}")
        if save_path.parent.resolve() != source_root.resolved_path:
            raise ValueError("Factorio save deletion only supports files directly inside the saves root.")
        File_Utils.remove(save_path, silent=False, resolve=False)
        return current_save

    async def start(self) -> bool:
        log.info(f"{__name__}.start")
        _ensure_factorio_binary_executable(self.directory / "bin" / "x64" / "factorio")
        self._startup_error = None

        for item in (self.directory / "saves").iterdir():
            if item.is_dir():
                continue
            if not item.name.endswith("tmp.zip"):
                continue
            File_Utils.remove(item, silent=True, resolve=True)

        if self.yuki_bridge_enabled:
            self._reset_bridge_events_file()

        wait_count = 10
        while self._lock.exists() and wait_count >= 0:
            wait_count -= 1
            await asyncio.sleep(1)

        await self._std_launch()

        if self.server_log and self.server_log.exists():
            File_Utils.link(self.server_log, self.file_stdout.with_name(self.server_log.name))

        if self.process and self.process.stdout:
            log.debug(f"{self.name} Tailing: Process")
            self._tail = Tailer(self._startup_tail_ready, self.process.stdout, self.file_stdout)
        elif self.server_log:
            log.debug(f"{self.name} Tailing: server log")
            self._tail = Tailer(self._startup_tail_ready, self.server_log, self.file_stdout)
        else:
            raise SystemError("No Log to be passed to Tailer")
        await self._tail.start(self._tail_machers)
        await self._start_bridge_events_tail()

        await self._wait_for_startup_ready()

        await self._players.start()
        await self._activities.start()

        self._running = True
        return True

    async def _wait_for_startup_ready(self) -> None:
        setup_task = asyncio.create_task(self._relay.setup())
        try:
            while not setup_task.done():
                if self._startup_error is not None:
                    raise RuntimeError(f"Factorio startup failed: {self._startup_error}")
                if not self.check_running():
                    await self._drain_stderr_task()
                    detail = self._startup_error or "process exited before RCON became available"
                    raise RuntimeError(f"Factorio startup failed: {detail}")
                await asyncio.sleep(0.2)
            await setup_task
        except Exception:
            if not setup_task.done():
                setup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await setup_task
            if self._tail is not None:
                await self._tail.stop()
                self._tail = None
            await self._stop_bridge_events_tail()
            with contextlib.suppress(Exception):
                await self._relay.teardown()
            with contextlib.suppress(Exception):
                await self._terminate()
            raise

    def _startup_tail_ready(self) -> bool:
        return self.process is not None

    def _reset_bridge_events_file(self) -> None:
        File_Utils.remove(self.bridge_events_path, silent=True, resolve=False)

    async def _start_bridge_events_tail(self) -> None:
        if not self.yuki_bridge_enabled:
            return
        if self._bridge_events_tail is not None:
            return
        log.debug("%s Tailing: yuki bridge events", self.name)
        self._bridge_events_tail = Tailer(self._startup_tail_ready, self.bridge_events_path)
        await self._bridge_events_tail.start(self._bridge_tail_matchers)

    async def _stop_bridge_events_tail(self) -> None:
        bridge_events_tail = getattr(self, "_bridge_events_tail", None)
        if bridge_events_tail is None:
            return
        await bridge_events_tail.stop()
        self._bridge_events_tail = None

    async def stop(self) -> bool:
        log.info(f"{__name__}.stop")
        self._running = False
        await self._activities.stop()
        await self._players.stop()
        stopped_gracefully = await self._request_graceful_process_stop()
        if not stopped_gracefully:
            save_requested = await self._request_stop_save()
            if save_requested:
                await asyncio.sleep(_FACTORIO_STOP_SAVE_GRACE_SECONDS)
            await self._terminate()
        if self._tail:
            await self._tail.stop()
            self._tail = None
        await self._stop_bridge_events_tail()
        await self._relay.teardown()
        await asyncio.sleep(0.5)
        File_Utils.remove(self._lock, silent=True, resolve=True)  # Sometimes it doesn't get removed
        return True

    async def _request_graceful_process_stop(self) -> bool:
        process = self.process
        if process is None:
            return False
        if process.poll() is not None:
            await self._drain_stderr_task()
            if self.process is process:
                self.process = None
            return True

        log.info(
            "Requesting graceful Factorio shutdown for %s with SIGINT; timeout=%ss",
            self.name,
            _FACTORIO_GRACEFUL_STOP_TIMEOUT_SECONDS,
        )
        try:
            process.send_signal(signal.SIGINT)
            await run_blocking(process.wait, _FACTORIO_GRACEFUL_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log.warning(
                "%s did not stop after SIGINT within %ss.",
                self.name,
                _FACTORIO_GRACEFUL_STOP_TIMEOUT_SECONDS,
            )
            return False
        except ProcessLookupError:
            log.info("%s process exited before SIGINT could be delivered.", self.name)
        except Exception:
            log.exception("Failed to request graceful Factorio shutdown for %s.", self.name)
            return False

        await self._drain_stderr_task()
        if self.process is process:
            self.process = None
        log.info("%s stopped gracefully after SIGINT.", self.name)
        return True

    async def _request_stop_save(self) -> bool:
        save_requested = False
        ok: str | None = None
        if self._relay.is_connected:
            ok = await self.send_player_gated_rcon(
                "/server-save",
                reconnect_on_failure=False,
                check_player_gate=False,
            )
            save_requested = ok is not None
        if not ok:
            if self.process and self.process.stdin:
                log.debug("Falling back to stdin")
                try:
                    self.process.stdin.write("/server-save\n")
                    self.process.stdin.flush()
                    save_requested = True
                except (BrokenPipeError, OSError, ValueError) as xcp:
                    log.warning("Factorio stdin save fallback failed for %s: %s", self.name, xcp)
        return save_requested

    async def kill(self) -> bool:
        self._running = False
        await self._activities.stop()
        await self._players.stop()
        if self._tail:
            await self._tail.stop()
            self._tail = None
        await self._stop_bridge_events_tail()
        await self._relay.teardown()
        await self._terminate()
        await asyncio.sleep(0.5)
        File_Utils.remove(self._lock, silent=True, resolve=True)
        return True

    async def player_count(self) -> tuple[int, int] | None:
        return await self._players.count()

    def connected_player_names(self) -> tuple[str, ...]:
        return self._players.connected_player_names()

    async def _rcon_player_gate_block_reason(self) -> str | None:
        try:
            gate_enabled = self.rcon_requires_online_players_enabled
        except AttributeError:
            gate_enabled = False
        if gate_enabled is not True:
            return None
        player_snapshot = await self.player_count()
        if player_snapshot is None:
            return f"{self.friendly} player count is unavailable, so RCON commands are currently gated."
        online_players, _player_capacity = player_snapshot
        if online_players > 0:
            return None
        return f"{self.friendly} has no players online, so RCON commands are currently gated."

    @overload
    async def send_player_gated_rcon(
        self,
        command: str,
        *,
        reconnect_on_failure: bool = True,
        fail_when_gated: bool = False,
        check_player_gate: bool = True,
    ) -> str | None: ...

    @overload
    async def send_player_gated_rcon(
        self,
        command: dict[str, str],
        *,
        reconnect_on_failure: bool = True,
        fail_when_gated: bool = False,
        check_player_gate: bool = True,
    ) -> dict[str, str | None] | None: ...

    async def send_player_gated_rcon(
        self,
        command: FactorioRconCommand,
        *,
        reconnect_on_failure: bool = True,
        fail_when_gated: bool = False,
        check_player_gate: bool = True,
    ) -> FactorioRconResponse:
        if check_player_gate:
            block_reason = await self._rcon_player_gate_block_reason()
            if block_reason is not None:
                if fail_when_gated:
                    raise RuntimeError(block_reason)
                log.debug("Skipping Factorio RCON command for %s: %s", getattr(self, "name", "unknown"), block_reason)
                return None
        if reconnect_on_failure:
            return await self._relay.send(command)
        return await self._relay.send(command, reconnect_on_failure=False)

    def detect_installed_version(self) -> AppVersion | None:
        return detect_factorio_version(directory=self.directory)

    @property
    def version_source(self) -> AppVersionSource:
        return AppVersionSource.INSTALLED_FILES


class Factorio_Updater(Update_Manager):
    _scope_update_locks: dict[str, threading.Lock] = {}
    _scope_update_locks_guard = threading.Lock()

    def __init__(self, app: Factorio, *, base: bool = False, mods: bool = False) -> None:
        super().__init__(app, base=base, mods=mods)
        self.version: tuple[int, ...] | None = None
        app_version: AppVersion | None = detect_factorio_version(directory=app.directory)
        if app_version is not None:
            self.version = _parse_factorio_version_text(app_version.main, label="local Factorio version")
        if self.version:
            log.info(f"Factorio local version: {self.stringise(self.version)}")
        else:
            log.warning(f"Could not determine Factorio version: {app.directory / 'factorio-current.log'}")
        self._state_lock = threading.Lock()
        self._status: AppUpdateStatus = AppUpdateStatus(
            state=AppUpdateState.IDLE,
            summary="Ready",
        )
        self._log_tail: deque[str] = deque(maxlen=80)
        self._operation_running: bool = False
        self._active_task: asyncio.Task[AppUpdateOperationResult] | None = None
        self._held_scope_update_lock: threading.Lock | None = None

    @staticmethod
    def _unix_ms_now() -> int:
        return int(time.time() * 1000)

    def info(self) -> AppUpdateInfo:
        update_config = self._factorio_update_config()
        selected_branch = update_config.selected_branch
        installed_branch = update_config.installed_branch
        return AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.FACTORIO,
            provider_label="Factorio.com",
            selected_branch_id=selected_branch.value,
            selected_branch_label=selected_branch.display_label,
            branches=tuple(
                AppUpdateBranchState(
                    branch_id=branch.value,
                    label=branch.display_label,
                    selected=branch is selected_branch,
                )
                for branch in _FACTORIO_UPDATE_BRANCHES
            ),
            supports_verify=True,
            installed_branch_id=installed_branch.value if installed_branch is not None else None,
        )

    def select_branch(self, branch_id: str) -> AppUpdateInfo:
        if self.status().running:
            raise RuntimeError(f"Cannot change the Factorio branch while {self.app.friendly} is updating.")
        update_config = self._factorio_update_config()
        branch = update_config.branch(branch_id)
        if branch is update_config.selected_branch:
            return self.info()
        self.app.cfg.factorio_update = update_config.model_copy(update={"selected_branch": branch})
        self.app.persist_instance_config_overrides()
        log.info("Selected Factorio update branch for %s: %s", self.app.friendly, branch.value)
        return self.info()

    def status(self) -> AppUpdateStatus:
        with self._state_lock:
            return self._status

    async def start_selected_update(self) -> AppUpdateOperationResult:
        return self._start_selected_operation(AppUpdateOperationKind.UPDATE)

    async def start_selected_verify(self) -> AppUpdateOperationResult:
        return self._start_selected_operation(AppUpdateOperationKind.VERIFY)

    def _start_selected_operation(self, kind: AppUpdateOperationKind) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(kind)
        log.info("Starting Factorio %s task: app=%s branch=%s", kind.value, self.app.friendly, branch.value)
        task: asyncio.Task[AppUpdateOperationResult] = asyncio.create_task(
            self._run_started_operation(kind=kind, branch=branch)
        )
        with self._state_lock:
            self._active_task = task
        task.add_done_callback(self._log_background_task_outcome)
        return AppUpdateOperationResult(
            kind=kind,
            message=f"Started {kind.value} for {self.app.friendly} on Factorio branch {branch.display_label}.",
            selected_branch_id=branch.value,
            selected_branch_label=branch.display_label,
        )

    async def update_selected(self) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(AppUpdateOperationKind.UPDATE)
        return await self._run_started_operation(kind=AppUpdateOperationKind.UPDATE, branch=branch)

    async def verify_selected(self) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(AppUpdateOperationKind.VERIFY)
        return await self._run_started_operation(kind=AppUpdateOperationKind.VERIFY, branch=branch)

    async def base(self) -> str | None:
        result = await self.update_selected()
        return result.version_text

    def _begin_selected_operation(self, kind: AppUpdateOperationKind) -> FactorioUpdateBranch:
        if kind not in (AppUpdateOperationKind.UPDATE, AppUpdateOperationKind.VERIFY):
            raise ValueError(f"Unsupported Factorio update operation: {kind.value}")
        if self.app.check_running():
            raise RuntimeError(f"{self.app.friendly} must be stopped before {kind.value}.")
        branch = self._factorio_update_config().selected_branch
        started_at_unix_ms = self._unix_ms_now()
        with self._state_lock:
            if self._operation_running:
                raise RuntimeError(f"{self.app.friendly} already has an update operation in progress.")
        scope_update_lock = self._acquire_scope_update_lock(kind)
        with self._state_lock:
            if self._operation_running:
                scope_update_lock.release()
                raise RuntimeError(f"{self.app.friendly} already has an update operation in progress.")
            self._held_scope_update_lock = scope_update_lock
            self._operation_running = True
            self._active_task = None
            self._log_tail.clear()
            self._status = AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary=f"Starting {kind.value}...",
                operation_kind=kind,
                progress_percent=0.0,
                started_at_unix_ms=started_at_unix_ms,
            )
        self._append_log(f"Selected branch: {branch.display_label} ({branch.value})")
        return branch

    def _acquire_scope_update_lock(self, kind: AppUpdateOperationKind) -> threading.Lock:
        scope = str(getattr(self.app, "scope", "")).strip() or "factorio"
        with self._scope_update_locks_guard:
            scope_update_lock = self._scope_update_locks.setdefault(scope, threading.Lock())
        if scope_update_lock.acquire(blocking=False):
            return scope_update_lock
        raise RuntimeError(
            f"Another Factorio {kind.value} operation is already in progress for scope `{scope}`."
        )

    def _release_scope_update_lock(self) -> None:
        with self._state_lock:
            scope_update_lock = self._held_scope_update_lock
            self._held_scope_update_lock = None
        if scope_update_lock is not None:
            scope_update_lock.release()

    async def _run_started_operation(
        self,
        *,
        kind: AppUpdateOperationKind,
        branch: FactorioUpdateBranch,
    ) -> AppUpdateOperationResult:
        try:
            self._update_running_status(
                summary=f"Checking latest {branch.display_label.lower()} release...",
                detail="Fetching Factorio release metadata.",
                progress_percent=5.0,
            )
            latest_versions = await self.fetch_latest_versions()
            latest = latest_versions[branch]
            latest_version_text = self.stringise(latest)
            if self.version is not None:
                if latest < self.version:
                    current_version_text = self.stringise(self.version)
                    raise RuntimeError(
                        f"Selected {branch.display_label.lower()} release {latest_version_text} is older than the "
                        f"installed version {current_version_text}; downgrades are not supported."
                    )
                if latest == self.version and kind is AppUpdateOperationKind.UPDATE:
                    message = f"No new {branch.display_label.lower()} update found for {self.app.friendly}."
                    self._finish_operation(
                        state=AppUpdateState.SUCCEEDED,
                        summary=message,
                        detail=f"{self.app.friendly} is already on {latest_version_text}.",
                        progress_percent=100.0,
                    )
                    return AppUpdateOperationResult(
                        kind=kind,
                        message=message,
                        version_text=latest_version_text,
                        selected_branch_id=branch.value,
                        selected_branch_label=branch.display_label,
                    )

            self._append_log(f"Latest {branch.display_label.lower()} version: {latest_version_text}")
            archive_path = await self.download_release(branch=branch, version_text=latest_version_text)
            try:
                self._update_running_status(
                    summary="Installing update...",
                    detail=f"Extracting {archive_path.name}.",
                    progress_percent=80.0,
                )
                await run_blocking(self._install_release_archive, archive_path)
            finally:
                File_Utils.remove(archive_path, silent=True, resolve=False)

            self.version = latest
            self.app.apply_version(AppVersion(main=latest_version_text), persist=False)
            update_config = self._factorio_update_config().model_copy(update={"installed_branch": branch})
            self.app.cfg.factorio_update = update_config
            self.app.persist_instance_config_overrides()

            if kind is AppUpdateOperationKind.VERIFY:
                message = (
                    f"Verified {self.app.friendly} on Factorio branch {branch.display_label} "
                    f"by reinstalling {latest_version_text}."
                )
            else:
                message = f"Updated {self.app.friendly} on Factorio branch {branch.display_label} to {latest_version_text}."
            self._finish_operation(
                state=AppUpdateState.SUCCEEDED,
                summary=message,
                detail="Factorio release installed successfully.",
                progress_percent=100.0,
            )
            return AppUpdateOperationResult(
                kind=kind,
                message=message,
                version_text=latest_version_text,
                selected_branch_id=branch.value,
                selected_branch_label=branch.display_label,
            )
        except Exception as xcp:
            self._finish_operation(
                state=AppUpdateState.FAILED,
                summary=f"{kind.value.title()} failed for {self.app.friendly}.",
                detail=str(xcp),
            )
            log.warning(
                "Factorio %s failed: app=%s branch=%s error=%s",
                kind.value,
                self.app.friendly,
                branch.value,
                xcp,
            )
            raise
        finally:
            with self._state_lock:
                self._operation_running = False
                self._active_task = None
            self._release_scope_update_lock()

    async def download_release(self, *, branch: FactorioUpdateBranch, version_text: str) -> Path:
        url = _factorio_download_url(branch)
        archive_path = _factorio_download_archive_path(
            tmp_dir=config.DIR_TMP,
            branch=branch,
            version_text=version_text,
        )
        self._append_log(f"Downloading {url}")
        timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Factorio download failed with HTTP {response.status}.")
                    total_bytes = response.content_length
                    downloaded_bytes = 0
                    with open(archive_path, "wb") as archive_file:
                        async for chunk in response.content.iter_chunked(262_144):
                            archive_file.write(chunk)
                            downloaded_bytes += len(chunk)
                            progress_percent = 40.0
                            detail = f"Downloaded {downloaded_bytes} bytes."
                            if total_bytes is not None and total_bytes > 0:
                                ratio = min(1.0, downloaded_bytes / total_bytes)
                                progress_percent = 15.0 + (ratio * 55.0)
                                detail = f"Downloaded {downloaded_bytes} of {total_bytes} bytes ({ratio * 100.0:.2f}%)."
                            self._update_running_status(
                                summary=f"Downloading {branch.display_label.lower()} release...",
                                detail=detail,
                                progress_percent=progress_percent,
                            )
        except Exception:
            File_Utils.remove(archive_path, silent=True, resolve=False)
            raise
        self._append_log(f"Downloaded archive: {archive_path.name}")
        return archive_path

    @staticmethod
    async def fetch_latest_versions() -> dict[FactorioUpdateBranch, tuple[int, ...]]:
        url = "https://factorio.com/api/latest-releases"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"Failed to fetch Factorio latest versions: HTTP {response.status}.")
                data = _json_object(cast(object, await response.json()), label="Factorio latest releases")
        return _factorio_latest_headless_versions(data)

    @staticmethod
    def _extract_archive_root(archive_path: Path, staging_dir: Path) -> Path:
        with tarfile.open(archive_path, mode="r:xz") as archive:
            for member in archive.getmembers():
                relative_path = _normalise_factorio_archive_member_path(member.name)
                target_path = staging_dir / relative_path
                if member.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    target_path.chmod(member.mode & 0o7777)
                    continue
                if member.issym() or member.islnk():
                    raise ValueError(f"Factorio archive symlinks are not supported: {member.name}")
                if not member.isfile():
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Factorio archive member could not be read: {member.name}")
                with source, open(target_path, "wb") as target:
                    shutil.copyfileobj(source, target)
                target_path.chmod(member.mode & 0o7777)
        extracted_root = staging_dir / "factorio"
        if not extracted_root.is_dir():
            raise FileNotFoundError("Factorio archive did not contain the expected root directory.")
        return extracted_root

    @staticmethod
    def _overlay_directory(source_root: Path, target_root: Path) -> None:
        for source in source_root.iterdir():
            target = target_root / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _install_release_archive(self, archive_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="yukibot-factorio-update-") as temp_dir:
            staging_dir = Path(temp_dir)
            extracted_root = self._extract_archive_root(archive_path, staging_dir)
            self._overlay_directory(extracted_root, self.app.directory)
        _ensure_factorio_binary_executable(self.app.directory / "bin" / "x64" / "factorio")
        self._apply_directory_ownership(self.app.directory)
        self._append_log(f"Installed archive into {self.app.directory}")

    async def mods(self) -> list[str] | None:
        await super().mods()

    def _factorio_update_config(self) -> FactorioUpdateConfig:
        update_config = self.app.cfg.factorio_update
        if update_config is None:
            return FactorioUpdateConfig()
        return update_config

    def _append_log(self, line: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self._state_lock:
            self._log_tail.append(clean_line)
            if self._status.state is AppUpdateState.RUNNING:
                self._status = replace(self._status, log_lines=tuple(self._log_tail))

    def _update_running_status(self, *, summary: str, detail: str | None, progress_percent: float | None) -> None:
        with self._state_lock:
            if self._status.state is not AppUpdateState.RUNNING:
                return
            self._status = replace(
                self._status,
                summary=summary,
                detail=detail,
                progress_percent=progress_percent,
                log_lines=tuple(self._log_tail),
            )

    def _finish_operation(
        self,
        *,
        state: AppUpdateState,
        summary: str,
        detail: str | None,
        progress_percent: float | None = None,
    ) -> None:
        finished_at_unix_ms = self._unix_ms_now()
        with self._state_lock:
            self._status = replace(
                self._status,
                state=state,
                summary=summary,
                detail=detail,
                progress_percent=progress_percent,
                log_lines=tuple(self._log_tail),
                finished_at_unix_ms=finished_at_unix_ms,
            )

    def _log_background_task_outcome(self, task: asyncio.Task[AppUpdateOperationResult]) -> None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            log.info("Factorio update task cancelled: app=%s", self.app.friendly)
        except Exception:
            log.exception("Factorio update task failed in background: app=%s", self.app.friendly)
        else:
            log.info(
                "Factorio update task completed: app=%s branch=%s version=%s",
                self.app.friendly,
                result.selected_branch_id,
                result.version_text,
            )

    @staticmethod
    def _apply_directory_ownership(root: Path) -> None:
        username, group_name = _owner_group()
        for pointer in (root, *root.rglob("*")):
            if pointer.is_symlink():
                continue
            shutil.chown(pointer, user=username, group=group_name)


class Receiver(AM_Receiver):
    def __init__(self, app: Factorio) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        base_content = payload.content_for_app(self.app) if hasattr(payload, "content_for_app") else payload.content
        content = OutboundRelayFormatter.format_payload(
            payload,
            RelayOutboundFormatOptions(
                base_content=base_content,
                reference_renderer=render_plain_reference_prefix,
            ),
        )
        message = _format_factorio_console_message(alias=payload.alias, content=content)
        app_config = getattr(self.app, "cfg", None)
        use_shout = getattr(app_config, "factorio_chat_relay_use_shout", True)
        if not use_shout:
            await self.app.send_player_gated_rcon(f"/silent-command game.print({json.dumps(message)})")
            return
        if self.app.yuki_bridge_enabled:
            response = await self.app.send_player_gated_rcon(
                _format_factorio_bridge_say_command(alias=payload.alias, content=content)
            )
            if not _factorio_command_failed(response):
                return
        response = await self.app.send_player_gated_rcon(f"/shout {message}")
        if _factorio_command_failed(response):
            await self.app.send_player_gated_rcon(f"/silent-command game.print({json.dumps(message)})")


def _render_factorio_research_name(raw_name: str) -> str:
    return humanise_mod_identifier(raw_name.strip(), split_single_camel=True)


def _factorio_bridge_research_name(payload: Mapping[str, object]) -> str | None:
    if payload.get("kind") != "research_finished":
        return None
    raw_technology = payload.get("technology")
    if not isinstance(raw_technology, str) or not raw_technology.strip():
        return None
    research_name = _render_factorio_research_name(raw_technology)
    raw_level = payload.get("level")
    if isinstance(raw_level, int) and raw_level > 1 and not research_name.endswith(f" {raw_level}"):
        return f"{research_name} {raw_level}"
    return research_name


def _parse_factorio_bridge_event_payload(line: str, *, include_wrapped_event: bool) -> Mapping[str, object] | None:
    clean_line = line.strip()
    if clean_line.startswith("{"):
        payload_text = clean_line
    elif include_wrapped_event and (match := _FACTORIO_YUKI_BRIDGE_EVENT_RE.search(clean_line)) is not None:
        payload_text = match.group("payload")
    else:
        return None
    try:
        payload = _optional_mapping(json.loads(payload_text))
    except json.JSONDecodeError:
        return None
    return payload


def _line_has_factorio_player_join_event(line: str, *, include_wrapped_event: bool = True) -> bool:
    payload = _parse_factorio_bridge_event_payload(line, include_wrapped_event=include_wrapped_event)
    if payload is not None:
        raw_event_name = payload.get("event") or payload.get("event_name") or payload.get("name") or payload.get(
            "kind"
        )
        return (
            isinstance(raw_event_name, str)
            and raw_event_name.strip().casefold() == _FACTORIO_PLAYER_JOIN_EVENT_NAME.casefold()
        )

    return _FACTORIO_PLAYER_EVENT_NAME_RE.search(line) is not None


def _parse_factorio_bridge_research_name(line: str, *, include_wrapped_event: bool = True) -> str | None:
    payload = _parse_factorio_bridge_event_payload(line, include_wrapped_event=include_wrapped_event)
    return None if payload is None else _factorio_bridge_research_name(payload)


def _parse_factorio_research_name(line: str, *, include_wrapped_bridge_event: bool = True) -> str | None:
    if match := _FACTORIO_RESEARCH_FINISHED_RE.search(line):
        return _render_factorio_research_name(match.group("research"))
    return _parse_factorio_bridge_research_name(line, include_wrapped_event=include_wrapped_bridge_event)


def _parse_factorio_map_age(text: str) -> FactorioMapAge | None:
    total_seconds = 0.0
    found_any = False
    for match in _FACTORIO_MAP_AGE_PART_RE.finditer(text):
        value = float(match.group("value"))
        unit = match.group("unit").casefold()
        if unit.startswith("d"):
            total_seconds += value * 86_400
        elif unit.startswith("h"):
            total_seconds += value * 3_600
        elif unit.startswith("m"):
            total_seconds += value * 60
        else:
            total_seconds += value
        found_any = True
    if not found_any:
        return None
    return FactorioMapAge(total_seconds=int(total_seconds))


def _parse_factorio_evolution(text: str) -> FactorioEvolution | None:
    match = _FACTORIO_EVOLUTION_FACTOR_RE.search(text)
    if match is None:
        return None
    raw_factor = float(match.group("value"))
    factor = raw_factor / 100.0 if raw_factor > 1.0 else raw_factor
    return FactorioEvolution(factor=max(0.0, min(1.0, factor)))


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _normalise_factorio_bridge_evolution(value: object) -> FactorioEvolution | None:
    if not isinstance(value, int | float):
        return None
    return FactorioEvolution(factor=max(0.0, min(1.0, float(value))))


def _factorio_bridge_surface_name(surface_payload: Mapping[str, object]) -> str | None:
    raw_name = surface_payload.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        return humanise_mod_identifier(raw_name.strip(), split_single_camel=True)
    raw_planet = surface_payload.get("planet")
    if isinstance(raw_planet, str) and raw_planet.strip():
        return humanise_mod_identifier(raw_planet.strip(), split_single_camel=True)
    return None


def _factorio_bridge_surface_evolution(entry: object) -> FactorioSurfaceEvolution | None:
    surface_entry = _optional_mapping(entry)
    if surface_entry is None:
        return None
    surface_payload = _optional_mapping(surface_entry.get("surface"))
    evolution_payload = _optional_mapping(surface_entry.get("evolution"))
    if surface_payload is None or evolution_payload is None:
        return None
    surface_name = _factorio_bridge_surface_name(surface_payload)
    if surface_name is None:
        return None
    evolution = _normalise_factorio_bridge_evolution(evolution_payload.get("total"))
    if evolution is None:
        return None
    return FactorioSurfaceEvolution(surface_name, evolution)


def _factorio_bridge_evolution_snapshot(payload: Mapping[str, object]) -> FactorioActivitySnapshot | None:
    if payload is None or payload.get("kind") != "evolution":
        return None
    raw_surfaces = payload.get("surfaces")
    if not isinstance(raw_surfaces, list):
        return None
    surface_evolutions = tuple(
        surface_evolution
        for item in raw_surfaces
        if (surface_evolution := _factorio_bridge_surface_evolution(item)) is not None
    )
    if not surface_evolutions:
        return None
    ordered_evolutions = tuple(sorted(surface_evolutions, key=_surface_evolution_sort_key))
    return FactorioActivitySnapshot(
        primary_evolution=ordered_evolutions[0].evolution,
        surface_evolutions=ordered_evolutions,
    )


def _parse_factorio_bridge_evolution_snapshot(text: str) -> FactorioActivitySnapshot | None:
    try:
        payload = _optional_mapping(json.loads(text))
    except json.JSONDecodeError:
        return None
    if payload is None:
        return None
    return _factorio_bridge_evolution_snapshot(payload)


def _normalise_factorio_surface_name(raw_name: str) -> str:
    return raw_name.strip().strip("\"'[]()")


def _surface_evolution_sort_key(surface_evolution: FactorioSurfaceEvolution) -> tuple[int, str]:
    surface_name = surface_evolution.surface_name.casefold()
    return (0 if surface_name == "nauvis" else 1, surface_name)


def _parse_factorio_surface_evolutions(text: str) -> tuple[FactorioSurfaceEvolution, ...]:
    evolutions_by_surface: dict[str, FactorioSurfaceEvolution] = {}
    pending_surface_name: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        inline_match = _FACTORIO_INLINE_SURFACE_EVOLUTION_RE.match(line)
        if inline_match is not None:
            surface_name = _normalise_factorio_surface_name(inline_match.group("surface"))
            evolution = _parse_factorio_evolution(line)
            if surface_name and evolution is not None:
                evolutions_by_surface[surface_name.casefold()] = FactorioSurfaceEvolution(surface_name, evolution)
            pending_surface_name = None
            continue
        header_match = _FACTORIO_SURFACE_HEADER_RE.match(line)
        if header_match is not None:
            surface_name = _normalise_factorio_surface_name(header_match.group("surface"))
            pending_surface_name = surface_name or None
            continue
        if pending_surface_name is None:
            continue
        evolution = _parse_factorio_evolution(line)
        if evolution is None:
            continue
        evolutions_by_surface[pending_surface_name.casefold()] = FactorioSurfaceEvolution(pending_surface_name, evolution)
        pending_surface_name = None
    return tuple(sorted(evolutions_by_surface.values(), key=_surface_evolution_sort_key))


def _format_factorio_console_message(*, alias: str, content: str) -> str:
    clean_alias = " ".join(alias.strip().splitlines()).replace('"', "'")
    clean_content = " ".join(content.strip().splitlines()).replace('"', "'")
    combined = f"{clean_alias}: {clean_content}".strip()
    if not combined:
        raise ValueError("Factorio outbound relay message must not be empty.")
    return combined


def _format_factorio_bridge_say_command(*, alias: str, content: str) -> str:
    clean_alias = " ".join(alias.strip().splitlines()).replace("|", "/")
    clean_content = " ".join(content.strip().splitlines())
    if not clean_alias or not clean_content:
        raise ValueError("Factorio bridge outbound relay message must not be empty.")
    return f"/yuki say {clean_alias}|{clean_content}"


def _factorio_command_failed(response: str | None) -> bool:
    if response is None:
        return False
    return _FACTORIO_COMMAND_FAILURE_RE.search(response) is not None


class FactorioActivities:
    _POLL_INTERVAL_SECONDS = 15.0

    def __init__(self, app: "Factorio") -> None:
        self.app: Factorio = app
        self.snapshot: FactorioActivitySnapshot = FactorioActivitySnapshot()
        self._map_age_provider = Provider_FactorioMapAge(app)
        self._evolution_provider = Provider_FactorioEvolution(app)
        self.configure_providers()
        self._task: asyncio.Task[None] | None = None

    def configure_providers(self) -> None:
        providers: list[AppActivityProvider["Factorio"]] = [self._map_age_provider]
        if self.app.yuki_bridge_enabled:
            providers.append(self._evolution_provider)
        self.app.set_activity_providers(tuple(providers))

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self.snapshot = FactorioActivitySnapshot()
        self.configure_providers()
        self.app.register_enabled_activity_providers()
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        self.app.deregister_activity_providers()
        self.snapshot = FactorioActivitySnapshot()
        await self.app._cancel_background_task(self._task, label="activity task")
        self._task = None

    def update_evolution_snapshot(self, snapshot: FactorioActivitySnapshot) -> None:
        self.snapshot = FactorioActivitySnapshot(
            map_age=self.snapshot.map_age,
            primary_evolution=snapshot.primary_evolution,
            surface_evolutions=snapshot.surface_evolutions,
        )

    async def _poll(self) -> None:
        while self.app.check_running() and not config.IS_SHUTTINGDOWN:
            self.configure_providers()
            commands: dict[str, str] = {"time": "/time"}
            bridge_enabled = self.app.yuki_bridge_enabled
            if bridge_enabled:
                commands["evolution"] = "/yuki evolution player"
            responses = await self.app.send_player_gated_rcon(commands)
            if isinstance(responses, dict):
                map_age = _parse_factorio_map_age(responses.get("time") or "")
                primary_evolution: FactorioEvolution | None = None
                surface_evolutions: tuple[FactorioSurfaceEvolution, ...] = ()
                if bridge_enabled and self.app.bridge_events_tail_active:
                    primary_evolution = self.snapshot.primary_evolution
                    surface_evolutions = self.snapshot.surface_evolutions
                elif bridge_enabled:
                    evolution_snapshot = _parse_factorio_bridge_evolution_snapshot(responses.get("evolution") or "")
                    if evolution_snapshot is not None:
                        primary_evolution = evolution_snapshot.primary_evolution
                        surface_evolutions = evolution_snapshot.surface_evolutions
                self.snapshot = FactorioActivitySnapshot(
                    map_age=map_age or self.snapshot.map_age,
                    primary_evolution=primary_evolution if bridge_enabled else None,
                    surface_evolutions=surface_evolutions if bridge_enabled else (),
                )
            await asyncio.sleep(self._POLL_INTERVAL_SECONDS)


class Provider_FactorioMapAge(AppActivityProvider["Factorio"]):
    metadata = AppActivityProviderMetadata(provider_id="map_age", label="Map Age")

    async def get(self) -> str | None:
        map_age = self.app._activities.snapshot.map_age
        if map_age is None:
            return None
        return map_age.activity_text()


class Provider_FactorioEvolution(AppActivityProvider["Factorio"]):
    metadata = AppActivityProviderMetadata(provider_id="evolution", label="Evolution")

    async def get(self) -> str | None:
        if not self.app.yuki_bridge_enabled:
            return None
        evolution = self.app._activities.snapshot.primary_evolution
        if evolution is None:
            return None
        return evolution.activity_text()

    async def detail(self) -> str | None:
        if not self.app.yuki_bridge_enabled:
            return None
        surface_evolutions = self.app._activities.snapshot.surface_evolutions
        if not surface_evolutions:
            return None
        return "\n".join(surface_evolution.detail_text() for surface_evolution in surface_evolutions)


class Matchers:
    def __init__(self, app: Factorio):
        self.app = app
        if not hasattr(app, "_bridge_tail_matchers"):
            app._bridge_tail_matchers = set()
        app._tail_machers.add(self.match_chat)
        app._tail_machers.add(self.match_death)
        app._tail_machers.add(self.match_error)
        app._tail_machers.add(self.match_player_session)
        app._tail_machers.add(self.match_research)
        app._bridge_tail_matchers.add(self.match_bridge_event)

    async def match_chat(self, line: str):
        match: Match[str] | None = re.search(r"\[CHAT\] (.*?): (.+)", line, re.IGNORECASE)
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
        if self.app.relay_notice_player_death_enabled is not True:
            return
        match: Match[str] | None = re.search(r"\[DIED\]\s+(\w+):(\S+)\s+(.+)", line, re.IGNORECASE)
        if not config.SILENT_DEBUG:
            log.debug(f"Match_Death: {line=} | {match=}")
        player = None
        if match:
            mode, player, cause = match.groups()
            log.debug(f"Match_Death: {player=} | {cause=} | {mode=}")
            detail_text = str(cause).replace("-", " ").title() if cause else "died"
            if mode == "PVE":
                detail_text = f"died to {detail_text}" if cause else "died"
                death_kind = GameDeathKind.PVE
            elif mode == "PVP":
                detail_text = f"killed by {detail_text}" if cause else "killed by another player"
                death_kind = GameDeathKind.PVP
            else:
                death_kind = GameDeathKind.UNKNOWN
            notice = GameDeathNotice(
                death_kind=death_kind,
                detail_text=detail_text,
                source=RelayNoticeSource.APP_LOG,
            )
            self._relay_death_notice(player=player, notice=notice)

    async def match_error(self, line: str) -> None:
        if match := _FACTORIO_ERROR_RE.search(line):
            source = match.group("source").strip()
            message = match.group("message").strip()
            self.app._startup_error = f"{source}: {message}"
            log.warning("Factorio error detected for %s: %s", self.app.name, self.app._startup_error)

    async def match_player_session(self, line: str) -> None:
        if not _line_has_factorio_player_join_event(line):
            return
        self.app._players.note_player_join_signal()

    async def match_research(self, line: str) -> None:
        if self.app.relay_notice_progress_enabled is not True:
            return
        research_name = _parse_factorio_research_name(
            line,
            include_wrapped_bridge_event=not getattr(self.app, "bridge_events_tail_active", False),
        )
        if research_name is None:
            return
        self._relay_research_notice(research_name)

    async def match_bridge_event(self, line: str) -> None:
        payload = _parse_factorio_bridge_event_payload(line, include_wrapped_event=False)
        if payload is None:
            return
        kind = payload.get("kind")
        if kind == "evolution":
            snapshot = _factorio_bridge_evolution_snapshot(payload)
            if snapshot is not None:
                self.app._activities.update_evolution_snapshot(snapshot)
            return
        if kind == "research_finished":
            await self.match_research(line)
            return
        if kind == "player_died":
            self._match_bridge_death(payload)
            return
        if _line_has_factorio_player_join_event(line, include_wrapped_event=False):
            self.app._players.note_player_join_signal()

    def _match_bridge_death(self, payload: Mapping[str, object]) -> None:
        if self.app.relay_notice_player_death_enabled is not True:
            return
        raw_player = payload.get("player")
        if not isinstance(raw_player, str) or not raw_player.strip():
            return

        cause_payload = _optional_mapping(payload.get("cause"))
        cause_name: str | None = None
        cause_force: str | None = None
        if cause_payload is not None:
            raw_cause_name = cause_payload.get("name")
            if isinstance(raw_cause_name, str) and raw_cause_name.strip():
                cause_name = humanise_mod_identifier(raw_cause_name.strip(), split_single_camel=True)
            raw_cause_force = cause_payload.get("force")
            if isinstance(raw_cause_force, str) and raw_cause_force.strip():
                cause_force = raw_cause_force.strip().casefold()

        if cause_force == "player":
            death_kind = GameDeathKind.PVP
            detail_text = f"killed by {cause_name}" if cause_name else "killed by another player"
        elif cause_name is not None:
            death_kind = GameDeathKind.PVE
            detail_text = f"died to {cause_name}"
        else:
            death_kind = GameDeathKind.UNKNOWN
            detail_text = "died"

        notice = GameDeathNotice(
            death_kind=death_kind,
            detail_text=detail_text,
            source=RelayNoticeSource.APP_LOG,
        )
        self._relay_death_notice(player=raw_player.strip(), notice=notice)

    def _relay_death_notice(self, *, player: str, notice: GameDeathNotice) -> None:
        app_friendly = getattr(self.app, "friendly", self.app.name)
        DC_Relay.add(
            DC_Bound(
                self.app,
                render_notice_text(notice, author_name=player, app_name=app_friendly),
                player,
                notice=notice,
            )
        )

    def _relay_research_notice(self, research_name: str) -> None:
        research_label = self.app.relay_advancement_term
        app_friendly = getattr(self.app, "friendly", self.app.name)
        notice = GameProgressNotice(
            progress_kind=GameProgressKind.RESEARCH,
            label=research_label,
            title=research_name,
            source=RelayNoticeSource.APP_LOG,
        )
        embed_spec = notice_embed_spec(notice, app_name=app_friendly, author_name="System")
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
                f"{research_label}: {research_name}",
                "System",
                relay_embed=relay_embed,
                notice=notice,
            )
        )


class Players:
    _POLL_INTERVAL_SECONDS = 1.0
    _EMPTY_POLLS_BEFORE_IDLE = 3

    def __init__(self, app: "Factorio") -> None:
        self.app: Factorio = app
        self._players_task: asyncio.Task[None] | None = None
        self._players_task_loop: asyncio.AbstractEventLoop | None = None
        self._poll_requested: asyncio.Event | None = None
        self._running = False
        self._online: int | None = None
        self._max: int | None = None
        self._players: set[str] = set[str]()
        self._empty_poll_count = 0

    async def start(self) -> None:
        self._online = 0
        self._max = None
        self._players = set[str]()
        self._empty_poll_count = 0
        if self._players_task and not self._players_task.done():
            return
        self._running = True
        self._poll_requested = asyncio.Event()
        self._players_task_loop = asyncio.get_running_loop()
        self._players_task = asyncio.create_task(self._poll_after_join_signal())

    async def stop(self) -> None:
        self._online = None
        self._max = None
        self._players = set[str]()
        self._empty_poll_count = 0
        self._running = False
        self._poll_requested = None
        if self._players_task:
            task = self._players_task
            self._players_task = None
            task_loop = self._players_task_loop
            self._players_task_loop = None
            if task_loop is None or task.done():
                return
            current_loop = asyncio.get_running_loop()
            if task_loop is not current_loop:
                task_loop.call_soon_threadsafe(task.cancel)
                log.debug("Factorio player poll task cancelled without await because it belongs to a different loop.")
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def note_player_join_signal(self) -> None:
        if not self._running:
            return
        self._online = max(self._online or 0, 1)
        self._empty_poll_count = 0
        if self._poll_requested is not None:
            self._poll_requested.set()

    async def _poll_after_join_signal(self) -> None:
        while self._running:
            poll_requested = self._poll_requested
            if poll_requested is None:
                return
            await poll_requested.wait()
            poll_requested.clear()
            await self._poll_until_idle()

    async def _poll_until_idle(self) -> None:
        while self._running and not config.IS_SHUTTINGDOWN and self.app.check_running():
            online_players = await self._poll_player_snapshot()
            if online_players == 0:
                self._empty_poll_count += 1
            elif online_players is not None:
                self._empty_poll_count = 0

            if self._empty_poll_count >= self._EMPTY_POLLS_BEFORE_IDLE:
                self._players.clear()
                self._online = 0
                self._empty_poll_count = 0
                return

            await asyncio.sleep(self._POLL_INTERVAL_SECONDS)

    async def _poll_player_snapshot(self) -> int | None:
        log.debug(f"Players.PRE {self._online}/{self._max} | {self._players}")
        if self._max is None:
            max_response = await self.app._relay.send("/config get max-players")
            max_players = self.extract_num(max_response) if max_response else None
            if max_players is not None:
                self._max = max_players or -1
                log.debug(f"Players.{self._max=}")

        response = await self.app._relay.send("/players online")
        if not response:
            return None
        players = self._parse_online_players(response)
        self._apply_player_snapshot(players)
        return len(players)

    @staticmethod
    def _parse_online_players(text: str) -> set[str]:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return set[str](name.rsplit(" ", 1)[0] for name in lines[1:])

    def _apply_player_snapshot(self, players: set[str]) -> None:
        joins = players.difference(self._players)
        leaves = self._players.difference(players)

        for player in leaves:
            self.apply_session_event(
                player=player,
                action=PlayerSessionAction.LEFT,
                source=RelayNoticeSource.APP_POLL,
            )
        for player in joins:
            self.apply_session_event(
                player=player,
                action=PlayerSessionAction.JOINED,
                source=RelayNoticeSource.APP_POLL,
            )

        self._players = set(players)
        self._online = len(players)

    def apply_session_event(self, *, player: str, action: PlayerSessionAction, source: RelayNoticeSource) -> None:
        player_name = player.strip()
        if not player_name:
            raise ValueError("Factorio player session event requires a player name.")
        if action is PlayerSessionAction.JOINED:
            if player_name in self._players:
                return
            self._players.add(player_name)
            self._online = len(self._players)
            self._relay_session_notice(player=player_name, action=action, source=source)
            log.debug(f"Players.add.{self._players=}")
            return
        if action is PlayerSessionAction.LEFT:
            if player_name not in self._players:
                return
            self._players.discard(player_name)
            self._online = len(self._players)
            self._relay_session_notice(player=player_name, action=action, source=source)
            log.debug(f"Players.discard.{self._players=}")
            return
        raise ValueError(f"Unsupported Factorio player session action: {action}")

    def _relay_session_notice(self, *, player: str, action: PlayerSessionAction, source: RelayNoticeSource) -> None:
        if action is PlayerSessionAction.JOINED:
            if self.app.relay_notice_player_joined_enabled is False:
                return
        elif action is PlayerSessionAction.LEFT:
            if self.app.relay_notice_player_left_enabled is False:
                return
        else:
            raise ValueError(f"Unsupported Factorio player session action: {action}")
        notice = self.app.player_session_notice(action=action, source=source)
        app_friendly = getattr(self.app, "friendly", self.app.name)
        DC_Relay.add(
            DC_Bound(
                self.app,
                render_notice_text(notice, author_name=player, app_name=app_friendly),
                player,
                notice=notice,
            )
        )

    @staticmethod
    def extract_num(text: str) -> int | None:
        for part in text.split(" "):
            if part.strip().isnumeric():
                return int(part)
        return None

    async def count(self) -> tuple[int, int] | None:
        if not config.SILENT_DEBUG:
            log.debug(f"Player.count={self._online}/{self._max}")
        if self._online is not None:
            return (self._online, self._max or -1)
        return None

    def connected_player_names(self) -> tuple[str, ...]:
        return tuple[str, ...](sorted(self._players, key=str.casefold))


# AiviA APasz
