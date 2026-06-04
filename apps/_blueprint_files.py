from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

_BLUEPRINT_FILE_SUFFIXES: Final[frozenset[str]] = frozenset({".sbp", ".sbpcfg"})
_INVALID_BLUEPRINT_NAME_CHARACTERS: Final[frozenset[str]] = frozenset({"'", "<", ">", ":", '"', "|", "?", "*"})


class AppBlueprintFileType(StrEnum):
    MODULE = "module"
    CONFIG = "config"


@dataclass(frozen=True, slots=True)
class AppBlueprintEntry:
    id: str
    label: str
    session_name: str
    relative_path: str
    file_type: AppBlueprintFileType
    size_bytes: int
    modified_at: datetime
    uploaded_by_user_id: int | None = None


def blueprint_file_type_from_name(filename: str) -> AppBlueprintFileType:
    suffix: str = Path(filename).suffix.casefold()
    if suffix == ".sbp":
        return AppBlueprintFileType.MODULE
    if suffix == ".sbpcfg":
        return AppBlueprintFileType.CONFIG
    raise ValueError(f"Unsupported blueprint file type: {filename}")


def validate_blueprint_session_name(raw: str) -> str:
    if not raw.strip():
        raise ValueError("Blueprint session name must not be empty.")
    if raw != raw.strip():
        raise ValueError("Blueprint session names must not start or end with spaces.")
    session_name = PurePosixPath(raw)
    if session_name.is_absolute():
        raise ValueError("Blueprint session name must be relative.")
    if len(session_name.parts) != 1 or any(part in {"", ".", ".."} for part in session_name.parts):
        raise ValueError("Blueprint session name must be a single directory name.")
    return session_name.as_posix()


def validate_blueprint_filename(raw: str) -> str:
    if not raw.strip():
        raise ValueError("Blueprint filename must not be empty.")
    if raw != raw.strip():
        raise ValueError("Blueprint filenames must not start or end with spaces.")
    filename: str = raw
    if filename in {".", ".."} or PurePosixPath(filename).name != filename or "\\" in filename:
        raise ValueError("Blueprint filename must not include directories.")
    file_type = blueprint_file_type_from_name(filename)
    suffix: str = ".sbp" if file_type is AppBlueprintFileType.MODULE else ".sbpcfg"
    blueprint_name: str = filename[: -len(suffix)]
    if not blueprint_name:
        raise ValueError("Blueprint filename must include a name before the extension.")
    if blueprint_name[0] in {" ", "."} or blueprint_name[-1] in {" ", "."}:
        raise ValueError("Blueprint names must not start or end with a space or period.")
    if any(character in _INVALID_BLUEPRINT_NAME_CHARACTERS for character in blueprint_name):
        raise ValueError("Blueprint names contain unsupported characters.")
    return filename


def normalise_blueprint_file_id(raw: str) -> str:
    relative_path = PurePosixPath(raw.strip())
    if relative_path.is_absolute():
        raise ValueError("Blueprint path must be relative.")
    parts = relative_path.parts
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Blueprint file ids must use '<session>/<filename>'.")
    session_name: str = validate_blueprint_session_name(parts[0])
    filename: str = validate_blueprint_filename(parts[1])
    return PurePosixPath(session_name, filename).as_posix()


def resolve_blueprint_file_path(root: Path, file_id: str) -> tuple[Path, str]:
    relative_path: str = normalise_blueprint_file_id(file_id)
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_path).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Blueprint path escapes root: {file_id}")
    return candidate, relative_path


def resolve_blueprint_upload_target(root: Path, *, session_name: str, upload_name: str) -> tuple[Path, str]:
    validated_session_name: str = validate_blueprint_session_name(session_name)
    validated_filename: str = validate_blueprint_filename(upload_name)
    relative_path = PurePosixPath(validated_session_name, validated_filename).as_posix()
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_path).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Blueprint upload path escapes root: {relative_path}")
    return candidate, relative_path


def describe_blueprint_file(
    root: Path,
    *,
    relative_path: str,
    uploaded_by_user_id: int | None,
) -> AppBlueprintEntry:
    path, normalised_relative_path = resolve_blueprint_file_path(root, relative_path)
    stat = path.stat()
    session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    return AppBlueprintEntry(
        id=normalised_relative_path,
        label=filename,
        session_name=session_name,
        relative_path=normalised_relative_path,
        file_type=blueprint_file_type_from_name(filename),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
        uploaded_by_user_id=uploaded_by_user_id,
    )


def list_blueprint_files(
    root: Path,
    *,
    uploaded_by_user_id_by_relative_path: dict[str, int],
) -> tuple[AppBlueprintEntry, ...]:
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return ()

    entries: list[AppBlueprintEntry] = []
    for session_path in sorted(resolved_root.iterdir(), key=lambda path: path.name.casefold()):
        if session_path.name.startswith(".") or not session_path.is_dir():
            continue
        session_name: str = validate_blueprint_session_name(session_path.name)
        for blueprint_path in sorted(session_path.iterdir(), key=lambda path: path.name.casefold()):
            if blueprint_path.name.startswith(".") or not blueprint_path.is_file():
                continue
            if blueprint_path.suffix.casefold() not in _BLUEPRINT_FILE_SUFFIXES:
                continue
            filename: str = validate_blueprint_filename(blueprint_path.name)
            relative_path = PurePosixPath(session_name, filename).as_posix()
            stat = blueprint_path.stat()
            entries.append(
                AppBlueprintEntry(
                    id=relative_path,
                    label=filename,
                    session_name=session_name,
                    relative_path=relative_path,
                    file_type=blueprint_file_type_from_name(filename),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                    uploaded_by_user_id=uploaded_by_user_id_by_relative_path.get(relative_path),
                )
            )
    return tuple(entries)
