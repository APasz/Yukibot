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
    allow_file_creation: bool = False
    allow_file_deletion: bool = False
    protected_relative_paths: frozenset[str] = frozenset()
    write_notice: str | None = None

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or self.id in {".", ".."}:
            raise ValueError(f"Invalid config root id: {self.id!r}")
        if not self.label.strip():
            raise ValueError("Config root label must not be empty.")
        if self.path.exists() and self.path.is_file() and (self.allow_file_creation or self.allow_file_deletion):
            raise ValueError(f"Config root {self.id!r} must be a directory for file management.")
        protected_relative_paths = frozenset(
            _normalise_relative_path(relative_path) for relative_path in self.protected_relative_paths
        )
        if protected_relative_paths != self.protected_relative_paths:
            object.__setattr__(self, "protected_relative_paths", protected_relative_paths)
        if self.write_notice is not None:
            if not isinstance(self.write_notice, str) or not self.write_notice.strip():
                raise ValueError("Config root write notice must be a non-empty string or None.")
            normalised_write_notice = self.write_notice.strip()
            if normalised_write_notice != self.write_notice:
                object.__setattr__(self, "write_notice", normalised_write_notice)

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
    can_write: bool = True
    can_delete: bool = False
    write_notice: str | None = None


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
    if relative_path in root.protected_relative_paths:
        raise ValueError(f"Config file is managed and cannot be modified: {relative_path}")
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


def create_app_config_file(
    roots: tuple[AppConfigFileRoot, ...],
    root_id: str,
    relative_path: str,
    content: str,
    *,
    default_read_level: Power_Level,
    default_write_level: Power_Level,
) -> AppConfigFileContent:
    file_id = f"{root_id}/{relative_path}"
    root, path, normalised_relative_path = resolve_app_config_path(roots, file_id)
    if not root.allow_file_creation:
        raise ValueError(f"Config root does not allow file creation: {root.label}")
    if normalised_relative_path in root.protected_relative_paths:
        raise ValueError(f"Config file is managed and cannot be created: {normalised_relative_path}")
    if path.exists():
        raise ValueError(f"Config file already exists: {normalised_relative_path}")
    encoded = content.encode(config.STR_ENCODE)
    if len(encoded) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(f"Config file content exceeds {MAX_CONFIG_FILE_BYTES} bytes.")

    root_path = root.resolved_path
    if root_path.exists() and not root_path.is_dir():
        raise ValueError(f"Config root is not a directory: {root.label}")
    root_path.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.parent.resolve().is_relative_to(root_path):
        raise ValueError(f"Config file escapes root {root.id}: {file_id}")
    created = False
    try:
        with path.open("x", encoding=config.STR_ENCODE) as handle:
            created = True
            handle.write(content)
    except OSError:
        if created:
            path.unlink(missing_ok=True)
        raise

    return AppConfigFileContent(
        file=_file_metadata(
            root=root,
            path=path,
            relative_path=normalised_relative_path,
            default_read_level=default_read_level,
            default_write_level=default_write_level,
        ),
        content=content,
    )


def delete_app_config_file(
    roots: tuple[AppConfigFileRoot, ...],
    file_id: str,
    *,
    default_read_level: Power_Level,
    default_write_level: Power_Level,
) -> AppConfigFile:
    root, path, relative_path = resolve_app_config_path(roots, file_id)
    _validate_readable_config_file(path)
    if not root.allow_file_deletion:
        raise ValueError(f"Config root does not allow file deletion: {root.label}")
    if relative_path in root.protected_relative_paths:
        raise ValueError(f"Config file is managed and cannot be deleted: {relative_path}")
    deleted_file = _file_metadata(
        root=root,
        path=path,
        relative_path=relative_path,
        default_read_level=default_read_level,
        default_write_level=default_write_level,
    )
    path.unlink()
    return deleted_file


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
    if not root.recursive and len(PurePosixPath(relative_path).parts) != 1:
        raise ValueError(f"Config root does not allow nested files: {file_id}")

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
        can_write=relative_path not in root.protected_relative_paths,
        can_delete=root.allow_file_deletion and relative_path not in root.protected_relative_paths,
        write_notice=root.write_notice,
    )
