"""Bounded, disk-backed preparation of single mods and multi-mod archives."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import lzma
from pathlib import Path, PurePosixPath
import stat
from threading import Event
from typing import BinaryIO
import zipfile
import zlib

import py7zr
from py7zr.exceptions import ArchiveError, PasswordRequired

from _file import require_free_disk_space
from archive_safety import ZipArchiveLimits, validated_archive_member_path, validated_zip_entries
from . import MOD_INFO_MAX_BYTES, _read_modinfo_value

_ZIP_ENCRYPTED_FLAG = 0x1


@dataclass(frozen=True, slots=True)
class PreparedModComponent:
    name: str
    directory: Path
    size_bytes: int
    native_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedModArchive:
    components: tuple[PreparedModComponent, ...]
    extracted_bytes: int
    file_count: int
    member_count: int


@dataclass(frozen=True, slots=True)
class _ArchiveMember:
    name: str
    path: PurePosixPath
    size: int
    is_directory: bool


def _component_roots(members: tuple[_ArchiveMember, ...], upload_name: str) -> dict[PurePosixPath, str]:
    roots = {member.path.parent for member in members if not member.is_directory and member.path.name == "ModInfo.xml"}
    if not roots:
        raise ValueError("The archive does not contain a 7D2D mod with ModInfo.xml.")
    result: dict[PurePosixPath, str] = {}
    names: set[str] = set()
    for root in sorted(roots):
        if any(parent in roots for parent in root.parents):
            raise ValueError("The archive contains nested mod roots.")
        name = root.name if root.parts else Path(upload_name).stem
        if not name or name.casefold().endswith((".disabled", ".client")) or name.startswith("."):
            raise ValueError(f"Invalid mod component name: {name}")
        validated_archive_member_path(name, archive_label="Mod component")
        if name.casefold() in names:
            raise ValueError(f"The archive contains duplicate mod components: {name}")
        names.add(name.casefold())
        result[root] = name
    return result


def _validate_members(members: tuple[_ArchiveMember, ...], limits: ZipArchiveLimits) -> None:
    if len(members) > limits.member_count:
        raise ValueError("Mod archive exceeds the maximum member count.")
    files = [member for member in members if not member.is_directory]
    if len(files) > limits.file_count or sum(member.size for member in files) > limits.extracted_bytes:
        raise ValueError("Mod archive exceeds its file-count or extracted-size limit.")
    paths: dict[str, tuple[PurePosixPath, bool]] = {}
    seen: set[str] = set()
    for member in members:
        if len(member.path.parts) > 64:
            raise ValueError("Mod archive paths are nested too deeply.")
        if member.path.name == "ModInfo.xml" and member.size > MOD_INFO_MAX_BYTES:
            raise ValueError("7D2D mod metadata exceeds the 1 MiB size limit.")
        if member.size > limits.file_bytes:
            raise ValueError(f"Mod archive member is too large: {member.name}")
        key = member.path.as_posix().casefold()
        if key in seen:
            raise ValueError(f"Mod archive contains duplicate paths: {member.name}")
        seen.add(key)
        for index in range(1, len(member.path.parts) + 1):
            path = PurePosixPath(*member.path.parts[:index])
            is_directory = index < len(member.path.parts) or member.is_directory
            previous = paths.setdefault(path.as_posix().casefold(), (path, is_directory))
            if previous != (path, is_directory):
                raise ValueError(f"Mod archive contains conflicting paths: {member.name}")


def _component_root(path: PurePosixPath, roots: Mapping[PurePosixPath, str]) -> PurePosixPath | None:
    return next((parent for parent in path.parents if parent in roots), None)


def _validated_layout(members: tuple[_ArchiveMember, ...], upload_name: str) -> dict[PurePosixPath, str]:
    roots = _component_roots(members, upload_name)
    for member in members:
        if not member.is_directory and _component_root(member.path, roots) is None:
            if member.path.suffix.casefold() not in {".txt", ".md", ".pdf", ".png", ".jpg"} and member.path.name not in {"LICENSE", ".gitattributes", ".gitignore"}:
                raise ValueError(f"The bundle contains files outside its mod folders: {member.name}")
    return roots


def prepare_mod_archive(
    stream: BinaryIO,
    upload_name: str,
    destination: Path,
    *,
    limits: ZipArchiveLimits,
    cancelled: Event,
    validate_root: Callable[[Path], None],
) -> PreparedModArchive:
    """Extract an already-spooled request directly, without copying its archive."""
    try:
        return _prepare_mod_archive(stream, upload_name, destination, limits=limits, cancelled=cancelled, validate_root=validate_root)
    except (zipfile.BadZipFile, ArchiveError, PasswordRequired, EOFError, NotImplementedError, lzma.LZMAError, zlib.error) as xcp:
        raise ValueError("The mod archive is damaged, encrypted, or uses an unsupported compression format.") from xcp


def _prepare_mod_archive(
    stream: BinaryIO, upload_name: str, destination: Path, *, limits: ZipArchiveLimits,
    cancelled: Event, validate_root: Callable[[Path], None],
) -> PreparedModArchive:
    stream.seek(0)
    if zipfile.is_zipfile(stream):
        stream.seek(0)
        entries = validated_zip_entries(
            stream, archive_label="Mod archive", limits=limits, allow_windows_separators=True,
        )
        if any(info.flag_bits & _ZIP_ENCRYPTED_FLAG for info, _path in entries):
            raise ValueError("Mod archives must not be encrypted.")
        members = tuple(
            _ArchiveMember(info.filename, path, info.file_size, info.filename.endswith(("/", "\\")))
            for info, path in entries
        )
        if any(stat.S_ISLNK(info.external_attr >> 16) for info, _path in entries):
            raise ValueError("Mod archives must not contain symbolic links.")
        _validate_members(members, limits)
        roots = _validated_layout(members, upload_name)
        require_free_disk_space(destination, sum(member.size for member in members))
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            for member in members:
                if cancelled.is_set():
                    raise InterruptedError("Mod upload cancelled.")
                target = destination.joinpath(*member.path.parts)
                if member.is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member.name) as source, target.open("xb") as output:
                    while chunk := source.read(1024**2):
                        if cancelled.is_set():
                            raise InterruptedError("Mod upload cancelled.")
                        output.write(chunk)
    elif Path(upload_name).suffix.casefold() == ".7z":
        stream.seek(0, 2)
        archive_bytes = stream.tell()
        stream.seek(0)
        with py7zr.SevenZipFile(stream, max_extract_size=limits.extracted_bytes) as archive:
            infos = archive.list()
            if any(not (info.is_file or info.is_directory) for info in infos):
                raise ValueError("Mod archives must contain only regular files and directories.")
            members = tuple(
                _ArchiveMember(
                    info.filename,
                    validated_archive_member_path(info.filename, archive_label="Mod archive"),
                    info.uncompressed, info.is_directory,
                )
                for info in infos
            )
            _validate_members(members, limits)
            roots = _validated_layout(members, upload_name)
            if sum(member.size for member in members) > archive_bytes * limits.compression_ratio:
                raise ValueError("Mod archive compression ratio is too high.")
            require_free_disk_space(destination, sum(member.size for member in members))
            if cancelled.is_set():
                raise InterruptedError("Mod upload cancelled.")
            archive.extractall(path=destination)
    else:
        raise ValueError("7D2D mod uploads must be ZIP or 7z archives.")

    components: list[PreparedModComponent] = []
    sizes = dict.fromkeys(roots, 0)
    for member in members:
        if not member.is_directory and (root := _component_root(member.path, roots)) is not None:
            sizes[root] += member.size
    for root, name in roots.items():
        directory = destination.joinpath(*root.parts)
        validate_root(directory)
        components.append(PreparedModComponent(
            name=name, directory=directory, size_bytes=sizes[root],
            native_id=_read_modinfo_value(directory / "ModInfo.xml", "Name"),
        ))
    if cancelled.is_set():
        raise InterruptedError("Mod upload cancelled.")
    return PreparedModArchive(
        components=tuple(components), extracted_bytes=sum(member.size for member in members if not member.is_directory),
        file_count=sum(not member.is_directory for member in members), member_count=len(members),
    )
