from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
import shutil
import zipfile

import config
from archive_safety import validated_zip_entries


class AppSaveRootMode(StrEnum):
    SELF = "self"
    CHILDREN = "children"


class AppSaveEntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class AppSaveRoot:
    id: str
    label: str
    path: Path
    mode: AppSaveRootMode
    recursive: bool = False
    suffixes: frozenset[str] = frozenset()
    include_files: bool = True
    include_directories: bool = True

    def __post_init__(self) -> None:
        if not self.id or "/" in self.id or self.id in {".", ".."}:
            raise ValueError(f"Invalid save root id: {self.id!r}")
        if not self.label.strip():
            raise ValueError("Save root label must not be empty.")

    @property
    def resolved_path(self) -> Path:
        return self.path.resolve()


@dataclass(frozen=True, slots=True)
class AppSaveEntry:
    id: str
    label: str
    relative_path: str
    root_id: str
    root_label: str
    kind: AppSaveEntryKind
    size_bytes: int
    modified_at: datetime


def get_app_save_root(roots: tuple[AppSaveRoot, ...], root_id: str) -> AppSaveRoot:
    return _root_by_id(roots, root_id)


def normalise_app_save_relative_path(raw: str) -> str:
    return _normalise_relative_path(raw)


def describe_app_save_path(*, root: AppSaveRoot, path: Path, relative_path: str) -> AppSaveEntry:
    return _save_metadata(root=root, path=path, relative_path=relative_path)


def list_app_save_files(roots: tuple[AppSaveRoot, ...]) -> tuple[AppSaveEntry, ...]:
    files: list[AppSaveEntry] = []
    for root in roots:
        root_path = root.resolved_path
        if root.mode is AppSaveRootMode.SELF:
            if not root_path.exists():
                continue
            if not _root_allows_path(root=root, path=root_path):
                continue
            files.append(_save_metadata(root=root, path=root_path, relative_path=root_path.name))
            continue

        if root_path.is_file():
            if _root_allows_path(root=root, path=root_path):
                files.append(_save_metadata(root=root, path=root_path, relative_path=root_path.name))
            continue
        if not root_path.is_dir():
            continue
        iterator = root_path.rglob("*") if root.recursive else root_path.glob("*")
        for path in sorted(iterator, key=lambda item: item.relative_to(root_path).as_posix().casefold()):
            if path.name.startswith("."):
                continue
            if not _root_allows_path(root=root, path=path):
                continue
            if not path.resolve().is_relative_to(root_path):
                continue
            files.append(_save_metadata(root=root, path=path, relative_path=path.relative_to(root_path).as_posix()))
    return tuple(sorted(files, key=lambda item: (item.root_label.casefold(), item.relative_path.casefold())))


def resolve_app_save_path(roots: tuple[AppSaveRoot, ...], file_id: str) -> tuple[AppSaveRoot, Path, str]:
    root_id, separator, raw_relative_path = file_id.partition("/")
    if not root_id or not separator or not raw_relative_path:
        raise ValueError("Save file id must use '<root>/<relative-path>'.")

    root = _root_by_id(roots, root_id)
    relative_path = _normalise_relative_path(raw_relative_path)
    root_path = root.resolved_path

    if root.mode is AppSaveRootMode.SELF:
        if relative_path != root_path.name:
            raise ValueError(f"Save file is not in root {root.id}: {file_id}")
        return root, root_path, relative_path

    if root_path.is_file():
        if relative_path != root_path.name:
            raise ValueError(f"Save file is not in root {root.id}: {file_id}")
        return root, root_path, relative_path

    candidate = (root_path / relative_path).resolve()
    if not candidate.is_relative_to(root_path):
        raise ValueError(f"Save file escapes root {root.id}: {file_id}")
    if not _root_allows_path(root=root, path=candidate):
        raise ValueError(f"Save file type is not allowed: {file_id}")
    return root, candidate, relative_path


def resolve_app_save_target(
    roots: tuple[AppSaveRoot, ...],
    *,
    root_id: str,
    relative_path: str,
) -> tuple[AppSaveRoot, Path, str]:
    root = _root_by_id(roots, root_id)
    normalised_relative_path = _normalise_relative_path(relative_path)
    root_path = root.resolved_path

    if root.mode is AppSaveRootMode.SELF:
        if normalised_relative_path != root_path.name:
            raise ValueError(f"Save target must match root {root.id}: {relative_path}")
        return root, root_path, normalised_relative_path

    if root_path.is_file():
        if normalised_relative_path != root_path.name:
            raise ValueError(f"Save target must match root {root.id}: {relative_path}")
        return root, root_path, normalised_relative_path

    candidate = (root_path / normalised_relative_path).resolve()
    if not candidate.is_relative_to(root_path):
        raise ValueError(f"Save target escapes root {root.id}: {relative_path}")
    if not _root_allows_path(root=root, path=candidate):
        raise ValueError(f"Save target type is not allowed: {relative_path}")
    return root, candidate, normalised_relative_path


def replace_directory_from_zip(*, archive_path: Path, destination: Path) -> None:
    extracted_entries = validated_zip_entries(
        archive_path,
        archive_label="Save upload",
        limits=config.NODE_API_ARCHIVE_LIMITS,
    )

    file_entries = [path for member, path in extracted_entries if not member.is_dir()]
    if not file_entries:
        raise ValueError("Save archive does not contain any files.")
    prefix_parts = _common_archive_prefix(file_entries)

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member, normalised_path in extracted_entries:
            relative_path = _strip_archive_prefix(normalised_path, prefix_parts)
            if relative_path is None:
                continue
            target = destination.joinpath(*relative_path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _root_by_id(roots: tuple[AppSaveRoot, ...], root_id: str) -> AppSaveRoot:
    for root in roots:
        if root.id == root_id:
            return root
    raise ValueError(f"Unknown save root: {root_id}")


def _normalise_relative_path(raw: str) -> str:
    relative = PurePosixPath(raw)
    if relative.is_absolute():
        raise ValueError("Save file path must be relative.")
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Save file path contains an invalid segment.")
    return relative.as_posix()


def _common_archive_prefix(paths: list[PurePosixPath]) -> tuple[str, ...]:
    first_parts = paths[0].parts
    if len(first_parts) < 2:
        return ()
    prefix: list[str] = [first_parts[0]]
    for path in paths[1:]:
        if len(path.parts) < 2 or path.parts[0] != prefix[0]:
            return ()
    return tuple(prefix)


def _strip_archive_prefix(path: PurePosixPath, prefix_parts: tuple[str, ...]) -> PurePosixPath | None:
    parts = path.parts
    if prefix_parts:
        if parts[: len(prefix_parts)] != prefix_parts:
            raise ValueError(f"Save archive member does not match the expected prefix: {path.as_posix()}")
        parts = parts[len(prefix_parts) :]
    if not parts:
        return None
    return PurePosixPath(*parts)


def _root_allows_path(*, root: AppSaveRoot, path: Path) -> bool:
    if path.is_file():
        if not root.include_files:
            return False
        if root.suffixes and path.suffix.casefold() not in root.suffixes:
            return False
        return True
    if path.is_dir():
        return root.include_directories
    return False


def _save_metadata(*, root: AppSaveRoot, path: Path, relative_path: str) -> AppSaveEntry:
    stat = path.stat()
    return AppSaveEntry(
        id=f"{root.id}/{relative_path}",
        label=Path(relative_path).name,
        relative_path=relative_path,
        root_id=root.id,
        root_label=root.label,
        kind=AppSaveEntryKind.DIRECTORY if path.is_dir() else AppSaveEntryKind.FILE,
        size_bytes=0 if path.is_dir() else stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
    )
