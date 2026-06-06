from __future__ import annotations

from collections.abc import Sequence
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
class AppBlueprintFileEntry:
    id: str
    label: str
    relative_path: str
    size_bytes: int
    modified_at: datetime
    uploaded_by_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class AppBlueprintEntry:
    id: str
    label: str
    session_name: str
    relative_path: str
    size_bytes: int
    modified_at: datetime
    uploaded_by_user_id: int | None = None
    config_file: AppBlueprintFileEntry | None = None


@dataclass(frozen=True, slots=True)
class BlueprintUploadPair:
    module_filename: str
    config_filename: str | None = None


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


def blueprint_name_stem(filename: str) -> str:
    file_type = blueprint_file_type_from_name(filename)
    suffix: str = ".sbp" if file_type is AppBlueprintFileType.MODULE else ".sbpcfg"
    return filename[: -len(suffix)]


def validate_blueprint_module_filename(raw: str) -> str:
    filename: str = validate_blueprint_filename(raw)
    if blueprint_file_type_from_name(filename) is not AppBlueprintFileType.MODULE:
        raise ValueError("Blueprint upload requires a .sbp module file.")
    return filename


def validate_blueprint_config_filename(raw: str) -> str:
    filename: str = validate_blueprint_filename(raw)
    if blueprint_file_type_from_name(filename) is not AppBlueprintFileType.CONFIG:
        raise ValueError("Blueprint config files must use the .sbpcfg extension.")
    return filename


def validate_blueprint_upload_pair(
    *,
    module_filename: str,
    config_filename: str | None = None,
) -> BlueprintUploadPair:
    validated_module: str = validate_blueprint_module_filename(module_filename)
    validated_config: str | None = None
    if config_filename is not None:
        validated_config = validate_blueprint_config_filename(config_filename)
        if blueprint_name_stem(validated_config) != blueprint_name_stem(validated_module):
            raise ValueError("Blueprint config filename must match the blueprint module filename.")
    return BlueprintUploadPair(
        module_filename=validated_module,
        config_filename=validated_config,
    )


def classify_blueprint_upload_filenames(filenames: Sequence[str]) -> BlueprintUploadPair:
    if not filenames:
        raise ValueError("Blueprint upload requires a .sbp blueprint file.")
    if len(filenames) > 2:
        raise ValueError("Blueprint upload accepts one .sbp file and one optional matching .sbpcfg file.")

    module_filename: str | None = None
    config_filename: str | None = None
    for raw_filename in filenames:
        filename: str = validate_blueprint_filename(raw_filename)
        file_type = blueprint_file_type_from_name(filename)
        if file_type is AppBlueprintFileType.MODULE:
            if module_filename is not None:
                raise ValueError("Blueprint upload accepts only one .sbp module file.")
            module_filename = filename
            continue
        if config_filename is not None:
            raise ValueError("Blueprint upload accepts only one .sbpcfg config file.")
        config_filename = filename

    if module_filename is None:
        raise ValueError("Blueprint upload requires a .sbp blueprint file.")
    return validate_blueprint_upload_pair(
        module_filename=module_filename,
        config_filename=config_filename,
    )


def normalise_existing_blueprint_session_name(raw: str) -> str:
    if not raw:
        raise ValueError("Blueprint session name must not be empty.")
    session_name = PurePosixPath(raw)
    if session_name.is_absolute():
        raise ValueError("Blueprint session name must be relative.")
    if len(session_name.parts) != 1 or any(part in {"", ".", ".."} for part in session_name.parts):
        raise ValueError("Blueprint session name must be a single directory name.")
    return session_name.as_posix()


def normalise_existing_blueprint_filename(raw: str) -> str:
    if not raw:
        raise ValueError("Blueprint filename must not be empty.")
    if raw in {".", ".."} or PurePosixPath(raw).name != raw or "\\" in raw:
        raise ValueError("Blueprint filename must not include directories.")
    blueprint_file_type_from_name(raw)
    return raw


def normalise_existing_blueprint_module_filename(raw: str) -> str:
    filename: str = normalise_existing_blueprint_filename(raw)
    if blueprint_file_type_from_name(filename) is not AppBlueprintFileType.MODULE:
        raise ValueError("Blueprint file must use the .sbp extension.")
    return filename


def normalise_existing_blueprint_config_filename(raw: str) -> str:
    filename: str = normalise_existing_blueprint_filename(raw)
    if blueprint_file_type_from_name(filename) is not AppBlueprintFileType.CONFIG:
        raise ValueError("Blueprint config file must use the .sbpcfg extension.")
    return filename


def normalise_blueprint_file_id(raw: str) -> str:
    if not raw:
        raise ValueError("Blueprint file id must not be empty.")
    relative_path = PurePosixPath(raw)
    if relative_path.is_absolute():
        raise ValueError("Blueprint path must be relative.")
    parts = relative_path.parts
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Blueprint file ids must use '<session>/<filename>'.")
    session_name: str = normalise_existing_blueprint_session_name(parts[0])
    filename: str = normalise_existing_blueprint_filename(parts[1])
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
) -> AppBlueprintFileEntry:
    path, normalised_relative_path = resolve_blueprint_file_path(root, relative_path)
    stat = path.stat()
    _session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    return AppBlueprintFileEntry(
        id=normalised_relative_path,
        label=filename,
        relative_path=normalised_relative_path,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
        uploaded_by_user_id=uploaded_by_user_id,
    )


def matching_blueprint_config_relative_path(module_relative_path: str) -> str:
    normalised_relative_path: str = normalise_blueprint_file_id(module_relative_path)
    session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    validated_module_filename: str = validate_blueprint_module_filename(filename)
    config_filename: str = f"{blueprint_name_stem(validated_module_filename)}.sbpcfg"
    return PurePosixPath(session_name, config_filename).as_posix()


def matching_blueprint_module_relative_path(config_relative_path: str) -> str:
    normalised_relative_path: str = normalise_blueprint_file_id(config_relative_path)
    session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    validated_config_filename: str = validate_blueprint_config_filename(filename)
    module_filename: str = f"{blueprint_name_stem(validated_config_filename)}.sbp"
    return PurePosixPath(session_name, module_filename).as_posix()


def find_matching_blueprint_config_relative_path(root: Path, module_relative_path: str) -> str | None:
    normalised_relative_path: str = normalise_blueprint_file_id(module_relative_path)
    session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    module_stem: str = blueprint_name_stem(normalise_existing_blueprint_module_filename(filename))
    session_path = root.resolve() / session_name
    if not session_path.is_dir():
        return None
    for sibling_path in sorted(session_path.iterdir(), key=lambda path: path.name.casefold()):
        if sibling_path.name.startswith(".") or not sibling_path.is_file():
            continue
        if sibling_path.suffix.casefold() not in _BLUEPRINT_FILE_SUFFIXES:
            continue
        sibling_name: str = normalise_existing_blueprint_filename(sibling_path.name)
        if blueprint_file_type_from_name(sibling_name) is not AppBlueprintFileType.CONFIG:
            continue
        if blueprint_name_stem(sibling_name) == module_stem:
            return PurePosixPath(session_name, sibling_name).as_posix()
    return None


def find_matching_blueprint_module_relative_path(root: Path, config_relative_path: str) -> str | None:
    normalised_relative_path: str = normalise_blueprint_file_id(config_relative_path)
    session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    config_stem: str = blueprint_name_stem(normalise_existing_blueprint_config_filename(filename))
    session_path = root.resolve() / session_name
    if not session_path.is_dir():
        return None
    for sibling_path in sorted(session_path.iterdir(), key=lambda path: path.name.casefold()):
        if sibling_path.name.startswith(".") or not sibling_path.is_file():
            continue
        if sibling_path.suffix.casefold() not in _BLUEPRINT_FILE_SUFFIXES:
            continue
        sibling_name: str = normalise_existing_blueprint_filename(sibling_path.name)
        if blueprint_file_type_from_name(sibling_name) is not AppBlueprintFileType.MODULE:
            continue
        if blueprint_name_stem(sibling_name) == config_stem:
            return PurePosixPath(session_name, sibling_name).as_posix()
    return None


def describe_blueprint(
    root: Path,
    *,
    relative_path: str,
    uploaded_by_user_id_by_relative_path: dict[str, int],
) -> AppBlueprintEntry:
    path, normalised_relative_path = resolve_blueprint_file_path(root, relative_path)
    stat = path.stat()
    session_name, filename = normalised_relative_path.split("/", maxsplit=1)
    validated_module_filename: str = normalise_existing_blueprint_module_filename(filename)
    config_file: AppBlueprintFileEntry | None = None
    config_relative_path = find_matching_blueprint_config_relative_path(root, normalised_relative_path)
    if config_relative_path is not None:
        config_file = describe_blueprint_file(
            root,
            relative_path=config_relative_path,
            uploaded_by_user_id=uploaded_by_user_id_by_relative_path.get(config_relative_path),
        )
    return AppBlueprintEntry(
        id=normalised_relative_path,
        label=validated_module_filename,
        session_name=session_name,
        relative_path=normalised_relative_path,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime),
        uploaded_by_user_id=uploaded_by_user_id_by_relative_path.get(normalised_relative_path),
        config_file=config_file,
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
        session_name: str = normalise_existing_blueprint_session_name(session_path.name)
        for blueprint_path in sorted(session_path.iterdir(), key=lambda path: path.name.casefold()):
            if blueprint_path.name.startswith(".") or not blueprint_path.is_file():
                continue
            if blueprint_path.suffix.casefold() not in _BLUEPRINT_FILE_SUFFIXES:
                continue
            filename: str = normalise_existing_blueprint_filename(blueprint_path.name)
            if blueprint_file_type_from_name(filename) is not AppBlueprintFileType.MODULE:
                continue
            relative_path = PurePosixPath(session_name, filename).as_posix()
            entries.append(
                describe_blueprint(
                    root,
                    relative_path=relative_path,
                    uploaded_by_user_id_by_relative_path=uploaded_by_user_id_by_relative_path,
                )
            )
    return tuple(entries)
