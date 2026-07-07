from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

import config
from _security import Power_Level

DEFAULT_CONFIG_FILE_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".ini",
        ".json",
        ".properties",
        ".snbt",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
MAX_CONFIG_FILE_BYTES = 1024 * 1024


class AppConfigFileKind(StrEnum):
    GAME = "game"
    MOD = "mod"


@dataclass(frozen=True, slots=True)
class AppConfigFileRoot:
    id: str
    label: str
    path: Path
    kind: AppConfigFileKind
    recursive: bool = True
    suffixes: frozenset[str] = DEFAULT_CONFIG_FILE_SUFFIXES
    read_power_level_override: Power_Level | None = None
    write_power_level_override: Power_Level | None = None

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or self.id in {".", ".."}:
            raise ValueError(f"Invalid config root id: {self.id!r}")
        if not self.label.strip():
            raise ValueError("Config root label must not be empty.")

    @property
    def resolved_path(self) -> Path:
        return self.path.resolve()


@dataclass(frozen=True, slots=True)
class AppConfigFile:
    id: str
    label: str
    relative_path: str
    root_id: str
    root_label: str
    kind: AppConfigFileKind
    read_power_level: Power_Level
    write_power_level: Power_Level
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class AppConfigFileContent:
    file: AppConfigFile
    content: str


def effective_config_root_read_level(*, root: AppConfigFileRoot, default: Power_Level) -> Power_Level:
    if root.read_power_level_override is not None:
        return root.read_power_level_override
    return default


def effective_config_root_write_level(*, root: AppConfigFileRoot, default: Power_Level) -> Power_Level:
    if root.write_power_level_override is not None:
        return root.write_power_level_override
    return default


def list_app_config_files(
    roots: tuple[AppConfigFileRoot, ...],
    *,
    default_read_level: Power_Level,
    default_write_level: Power_Level,
) -> tuple[AppConfigFile, ...]:
    files: list[AppConfigFile] = []
    for root in roots:
        root_path = root.resolved_path
        if root_path.is_file():
            files.append(
                _file_metadata(
                    root=root,
                    path=root_path,
                    relative_path=root_path.name,
                    default_read_level=default_read_level,
                    default_write_level=default_write_level,
                )
            )
            continue
        if not root_path.is_dir():
            continue
        iterator = root_path.rglob("*") if root.recursive else root_path.glob("*")
        for path in sorted(iterator, key=lambda item: item.relative_to(root_path).as_posix().casefold()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if root.suffixes and path.suffix.casefold() not in root.suffixes:
                continue
            if not path.resolve().is_relative_to(root_path):
                continue
            files.append(
                _file_metadata(
                    root=root,
                    path=path,
                    relative_path=path.relative_to(root_path).as_posix(),
                    default_read_level=default_read_level,
                    default_write_level=default_write_level,
                )
            )
    return tuple(
        sorted(files, key=lambda item: (item.kind.value, item.root_label.casefold(), item.relative_path.casefold()))
    )


def read_app_config_file(
    roots: tuple[AppConfigFileRoot, ...],
    file_id: str,
    *,
    default_read_level: Power_Level,
    default_write_level: Power_Level,
) -> AppConfigFileContent:
    root, path, relative_path = resolve_app_config_path(roots, file_id)
    _validate_readable_config_file(path)
    try:
        content = path.read_text(config.STR_ENCODE)
    except UnicodeDecodeError as xcp:
        raise ValueError(f"Config file is not valid {config.STR_ENCODE}: {file_id}") from xcp
    return AppConfigFileContent(
        file=_file_metadata(
            root=root,
            path=path,
            relative_path=relative_path,
            default_read_level=default_read_level,
            default_write_level=default_write_level,
        ),
        content=content,
    )


def write_app_config_file(
    roots: tuple[AppConfigFileRoot, ...],
    file_id: str,
    content: str,
    *,
    default_read_level: Power_Level,
    default_write_level: Power_Level,
) -> AppConfigFileContent:
    root, path, relative_path = resolve_app_config_path(roots, file_id)
    _validate_readable_config_file(path)
    encoded = content.encode(config.STR_ENCODE)
    if len(encoded) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(f"Config file content exceeds {MAX_CONFIG_FILE_BYTES} bytes.")
    path.write_text(content, config.STR_ENCODE)
    return AppConfigFileContent(
        file=_file_metadata(
            root=root,
            path=path,
            relative_path=relative_path,
            default_read_level=default_read_level,
            default_write_level=default_write_level,
        ),
        content=content,
    )


def resolve_app_config_path(roots: tuple[AppConfigFileRoot, ...], file_id: str) -> tuple[AppConfigFileRoot, Path, str]:
    root_id, separator, raw_relative_path = file_id.partition("/")
    if not root_id or not separator or not raw_relative_path:
        raise ValueError("Config file id must use '<root>/<relative-path>'.")

    root = _root_by_id(roots, root_id)
    relative_path = _normalise_relative_path(raw_relative_path)
    root_path = root.resolved_path
    if root_path.is_file():
        if relative_path != root_path.name:
            raise ValueError(f"Config file is not in root {root.id}: {file_id}")
        return root, root_path, relative_path

    candidate = (root_path / relative_path).resolve()
    if not candidate.is_relative_to(root_path):
        raise ValueError(f"Config file escapes root {root.id}: {file_id}")
    if root.suffixes and candidate.suffix.casefold() not in root.suffixes:
        raise ValueError(f"Config file suffix is not allowed: {file_id}")
    return root, candidate, relative_path


def resolve_app_config_root(roots: tuple[AppConfigFileRoot, ...], root_id: str) -> AppConfigFileRoot:
    return _root_by_id(roots, root_id)


def _root_by_id(roots: tuple[AppConfigFileRoot, ...], root_id: str) -> AppConfigFileRoot:
    for root in roots:
        if root.id == root_id:
            return root
    raise ValueError(f"Unknown config root: {root_id}")


def _normalise_relative_path(raw: str) -> str:
    relative = PurePosixPath(raw)
    if relative.is_absolute():
        raise ValueError("Config file path must be relative.")
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Config file path contains an invalid segment.")
    return relative.as_posix()


def _validate_readable_config_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Config file does not exist: {path.name}")
    if not path.is_file():
        raise ValueError(f"Config path is not a file: {path.name}")
    if path.stat().st_size > MAX_CONFIG_FILE_BYTES:
        raise ValueError(f"Config file exceeds {MAX_CONFIG_FILE_BYTES} bytes: {path.name}")


def _file_metadata(
    *,
    root: AppConfigFileRoot,
    path: Path,
    relative_path: str,
    default_read_level: Power_Level,
    default_write_level: Power_Level,
) -> AppConfigFile:
    stat = path.stat()
    return AppConfigFile(
        id=f"{root.id}/{relative_path}",
        label=Path(relative_path).name,
        relative_path=relative_path,
        root_id=root.id,
        root_label=root.label,
        kind=root.kind,
        read_power_level=effective_config_root_read_level(root=root, default=default_read_level),
        write_power_level=effective_config_root_write_level(root=root, default=default_write_level),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
    )
