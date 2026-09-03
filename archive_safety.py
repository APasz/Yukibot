"""Shared validation for ZIP archives received from untrusted users."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath, PureWindowsPath
import zipfile


@dataclass(frozen=True, slots=True)
class ZipArchiveLimits:
    """Bound the work and storage required to extract a ZIP archive."""

    member_count: int = 10_000
    file_count: int = 5_000
    file_bytes: int = 1 * 1024 * 1024 * 1024
    extracted_bytes: int = 4 * 1024 * 1024 * 1024
    compression_ratio: int = 100

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer.")


def validated_archive_member_path(member_name: str, *, archive_label: str) -> PurePosixPath:
    """Return a portable relative archive member path or raise ``ValueError``."""
    path = PurePosixPath(member_name)
    if (
        not member_name
        or "\x00" in member_name
        or "\\" in member_name
        or path.is_absolute()
        or PureWindowsPath(member_name).drive
        or ".." in path.parts
    ):
        raise ValueError(f"{archive_label} member path is invalid: {member_name}")
    return path


def validated_zip_entries(
    archive_path: Path | str,
    *,
    archive_label: str,
    limits: ZipArchiveLimits,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Return safe ZIP members after enforcing traversal and resource limits."""
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"{archive_label} is not a zip archive: {PurePosixPath(archive_path).name}")

    entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    file_count = 0
    extracted_bytes = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member_index, member in enumerate(archive.infolist(), start=1):
            if member_index > limits.member_count:
                raise ValueError(f"{archive_label} exceeds the maximum member count.")
            raw_name = member.filename.rstrip("/")
            if not raw_name:
                continue
            path = validated_archive_member_path(raw_name, archive_label=archive_label)
            if not member.is_dir():
                file_count += 1
                if file_count > limits.file_count:
                    raise ValueError(f"{archive_label} exceeds the maximum file count.")
                if member.file_size > limits.file_bytes:
                    raise ValueError(f"{archive_label} member is too large: {member.filename}")
                extracted_bytes += member.file_size
                if extracted_bytes > limits.extracted_bytes:
                    raise ValueError(f"{archive_label} exceeds the maximum extracted size.")
                if member.file_size > member.compress_size * limits.compression_ratio:
                    raise ValueError(f"{archive_label} member compression ratio is too high: {member.filename}")
            entries.append((member, path))
    return entries
