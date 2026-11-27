"""Shared validation and bounded extraction of untrusted archives."""

from __future__ import annotations

from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager, ExitStack, nullcontext
from dataclasses import dataclass, fields
import os
from ntpath import isreserved
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import subprocess
import tempfile
from threading import Event, Thread
from typing import BinaryIO, Protocol, cast
import zipfile

import rarfile

_ARCHIVE_COPY_CHUNK_BYTES = 1024**2
_ARCHIVE_MAX_PATH_DEPTH = 64
_ARCHIVE_CANCEL_POLL_SECONDS = 0.1


class _RarHeader(Protocol):
    type: int
    flags: int
    data_offset: int
    add_size: int


class RarMember(_RarHeader, Protocol):
    """Typed boundary for rarfile's unannotated member metadata."""

    filename: str
    file_size: int
    compress_size: int
    compress_type: int
    host_os: int
    mode: int
    file_redir: tuple[int, int, str] | None

    def is_symlink(self) -> bool: ...
    def is_file(self) -> bool: ...
    def is_dir(self) -> bool: ...


class RarArchive(Protocol):
    """The RAR reader operations used by the shared extraction code."""

    filename: str

    def infolist(self) -> Sequence[RarMember]: ...
    def open(self, member: RarMember) -> BinaryIO: ...
    def is_solid(self) -> bool: ...


class _RarTool(Protocol):
    def open_cmdline(self, pwd: None, rarfn: str) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class ZipArchiveLimits:
    """Bound the work and storage required to extract an archive."""

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
        or not path.parts
        or "\x00" in member_name
        or "\\" in member_name
        or path.is_absolute()
        or PureWindowsPath(member_name).drive
        or ".." in path.parts
    ):
        raise ValueError(f"{archive_label} member path is invalid: {member_name}")
    if isreserved(member_name):
        raise ValueError(f"{archive_label} member path is not supported on Windows: {member_name}")
    return path


class ArchiveMemberLayoutValidator:
    """Incrementally reject duplicate, conflicting, and nonportable member paths."""

    def __init__(self, *, archive_label: str) -> None:
        self._archive_label = archive_label
        self._paths: dict[str, tuple[str, bool]] = {}
        self._seen: set[str] = set()

    def add(self, member_path: PurePosixPath, *, is_directory: bool) -> None:
        rendered = member_path.as_posix()
        validated_archive_member_path(rendered, archive_label=self._archive_label)
        if len(member_path.parts) > _ARCHIVE_MAX_PATH_DEPTH:
            raise ValueError(f"{self._archive_label} paths are nested too deeply.")
        key = rendered.casefold()
        if key in self._seen:
            raise ValueError(f"{self._archive_label} contains duplicate paths: {member_path}")
        self._seen.add(key)
        prefix = ""
        for index, part in enumerate(member_path.parts, start=1):
            prefix = f"{prefix}/{part}" if prefix else part
            prefix_is_directory = index < len(member_path.parts) or is_directory
            prefix_key = prefix.casefold()
            previous = self._paths.get(prefix_key)
            if previous is None:
                self._paths[prefix_key] = (prefix, prefix_is_directory)
            elif previous != (prefix, prefix_is_directory):
                raise ValueError(f"{self._archive_label} contains conflicting paths: {member_path}")


def validate_archive_member_layout(
    members: Iterable[tuple[PurePosixPath, bool]], *, archive_label: str,
) -> None:
    """Reject duplicate, conflicting, case-ambiguous, and deeply nested paths."""
    validator = ArchiveMemberLayoutValidator(archive_label=archive_label)
    for member_path, is_directory in members:
        validator.add(member_path, is_directory=is_directory)


def check_archive_cancelled(cancelled: Event | None) -> None:
    """Stop an archive operation when its owning task has been cancelled."""
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("Archive operation cancelled.")


def validated_zip_entries(
    archive_path: Path | str | BinaryIO,
    *,
    archive_label: str,
    limits: ZipArchiveLimits,
    allow_windows_separators: bool = False,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Return safe ZIP members after enforcing traversal and resource limits."""
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"{archive_label} is not a zip archive.")

    entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    file_count = 0
    extracted_bytes = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member_index, member in enumerate(archive.infolist(), start=1):
            if member_index > limits.member_count:
                raise ValueError(f"{archive_label} exceeds the maximum member count.")
            member_name = member.filename.replace("\\", "/") if allow_windows_separators else member.filename
            raw_name = member_name.rstrip("/")
            if not raw_name:
                continue
            path = validated_archive_member_path(raw_name, archive_label=archive_label)
            if not member_name.endswith("/"):
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


@contextmanager
def open_rar_archive(
    source: Path | BinaryIO,
    *,
    archive_label: str,
    limits: ZipArchiveLimits,
    temporary_directory: Path | None = None,
    cancelled: Event | None = None,
) -> Generator[RarArchive]:
    """Give RAR backends a reusable path, without recopying a spool per member."""
    check_archive_cancelled(cancelled)
    member_count = 0

    def validate_header(member: _RarHeader) -> None:
        nonlocal member_count
        check_archive_cancelled(cancelled)
        if member.data_offset + member.add_size > archive_bytes:
            raise ValueError(f"{archive_label} contains truncated member data.")
        if (
            member.type == rarfile.RAR_BLOCK_MAIN and member.flags & rarfile.RAR_MAIN_VOLUME
            or member.type == rarfile.RAR_BLOCK_FILE and member.flags & (rarfile.RAR_FILE_SPLIT_BEFORE | rarfile.RAR_FILE_SPLIT_AFTER)
            or member.type == rarfile.RAR_BLOCK_ENDARC and member.flags & rarfile.RAR_ENDARC_NEXT_VOLUME
        ):
            raise ValueError(f"{archive_label} must be a single-volume RAR archive.")
        if member.type == rarfile.RAR_BLOCK_FILE:
            member_count += 1
            if member_count > limits.member_count:
                raise ValueError(f"{archive_label} exceeds the maximum member count.")
            if isinstance(member, rarfile.Rar5FileInfo) and cast(int, cast(object, member.file_flags)) & rarfile.RAR5_FILE_FLAG_UNKNOWN_SIZE:
                raise ValueError(f"{archive_label} members must declare their extracted sizes.")

    try:
        with ExitStack() as stack:
            if isinstance(source, Path):
                archive_path = source
            else:
                source.flush()
                # Linux request spools are unnamed files; the backend can reopen them
                # through the owning process without making another full-size copy.
                try:
                    archive_path = Path(f"/proc/{os.getpid()}/fd/{source.fileno()}")
                except OSError:
                    archive_path = None
                if archive_path is None or not archive_path.is_file():
                    temporary = stack.enter_context(tempfile.NamedTemporaryFile(
                        prefix="yukibot-rar-", suffix=".rar", dir=temporary_directory, delete_on_close=False,
                    ))
                    source.seek(0)
                    while chunk := source.read(_ARCHIVE_COPY_CHUNK_BYTES):
                        check_archive_cancelled(cancelled)
                        temporary.write(chunk)
                    temporary.close()
                    archive_path = Path(temporary.name)
            archive_bytes = archive_path.stat().st_size
            archive = stack.enter_context(rarfile.RarFile(
                archive_path, errors="strict", part_only=True, info_callback=validate_header,
            ))
            if archive.needs_password():
                raise ValueError(f"{archive_label} must not be encrypted.")
            yield cast(RarArchive, cast(object, archive))
    except rarfile.RarCannotExec as xcp:
        raise ValueError("RAR extraction requires an installed RAR backend, preferably unrar.") from xcp
    except rarfile.Error as xcp:
        raise ValueError(f"{archive_label} is damaged, encrypted, or uses an unsupported RAR compression format.") from xcp


def validated_rar_entries(
    archive: RarArchive,
    *,
    archive_label: str,
    limits: ZipArchiveLimits,
) -> list[tuple[RarMember, PurePosixPath]]:
    """Reject links, encryption, unsafe paths, and oversized RAR members."""
    entries: list[tuple[RarMember, PurePosixPath]] = []
    extracted_bytes = 0
    compressed_bytes = 0
    file_count = 0
    solid = archive.is_solid()
    for member in archive.infolist():
        if (
            member.is_symlink() or member.file_redir is not None
            or not (member.is_file() or member.is_dir())
            or member.host_os == rarfile.RAR_OS_UNIX and stat.S_IFMT(member.mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
        ):
            raise ValueError(f"{archive_label} must contain only regular files and directories.")
        path = validated_archive_member_path(member.filename.replace("\\", "/").rstrip("/"), archive_label=archive_label)
        if not member.is_dir():
            file_count += 1
            if file_count > limits.file_count:
                raise ValueError(f"{archive_label} exceeds the maximum file count.")
            if member.file_size > limits.file_bytes:
                raise ValueError(f"{archive_label} member is too large: {member.filename}")
            extracted_bytes += member.file_size
            compressed_bytes += member.compress_size
            if extracted_bytes > limits.extracted_bytes:
                raise ValueError(f"{archive_label} exceeds the maximum extracted size.")
            if not solid and member.file_size > member.compress_size * limits.compression_ratio:
                raise ValueError(f"{archive_label} member compression ratio is too high: {member.filename}")
        entries.append((member, path))
    if extracted_bytes > compressed_bytes * limits.compression_ratio:
        raise ValueError(f"{archive_label} compression ratio is too high.")
    validate_archive_member_layout(((path, member.is_dir()) for member, path in entries), archive_label=archive_label)
    return entries


def extract_rar_entries(
    archive: RarArchive,
    entries: Sequence[tuple[RarMember, PurePosixPath]],
    destination: Path,
    *,
    cancelled: Event | None = None,
) -> None:
    """Write validated members ourselves so the backend cannot create links or paths."""
    check_archive_cancelled(cancelled)
    if any(member.compress_type != rarfile.RAR_M0 for member, _path in entries):
        tool = cast(_RarTool, cast(object, rarfile.tool_setup()))
        command = tool.open_cmdline(None, archive.filename)
        if command[0] == rarfile.UNRAR_TOOL:
            command = [*command[:2], "-cfg-", *command[2:]]
        elif command[0] == rarfile.BSDTAR_TOOL:
            # rarfile puts the option terminator where bsdtar expects -f's value.
            command.remove("--")
        if command[0] in (rarfile.UNRAR_TOOL, rarfile.BSDTAR_TOOL):
            # A solid archive shares a dictionary across files. Decode it once;
            # opening every member separately repeatedly decodes earlier members.
            with _rar_decoder_stream(command, cancelled=cancelled) as stream:
                _write_rar_entries(archive, entries, destination, stream=stream, cancelled=cancelled)
            return
    _write_rar_entries(archive, entries, destination, cancelled=cancelled)


def _cancel_rar_decoder(process: subprocess.Popen[bytes], cancelled: Event, finished: Event) -> None:
    while not finished.wait(_ARCHIVE_CANCEL_POLL_SECONDS):
        if cancelled.is_set():
            if process.poll() is None:
                process.kill()
            return


@contextmanager
def _rar_decoder_stream(command: list[str], *, cancelled: Event | None) -> Generator[BinaryIO]:
    """Check decoder completion and reap it on errors or cancellation."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as xcp:
        raise ValueError("Unable to start the RAR decoder; check the installed backend.") from xcp
    with process:
        assert process.stdout is not None
        stream = cast(BinaryIO, process.stdout)
        finished = Event()
        watcher = None
        if cancelled is not None:
            watcher = Thread(target=_cancel_rar_decoder, args=(process, cancelled, finished), name="rar-cancellation", daemon=True)
            watcher.start()
        try:
            yield stream
            extra_data = stream.read(1)
            check_archive_cancelled(cancelled)
            if extra_data:
                raise ValueError("RAR decoder produced data beyond the declared member sizes.")
            return_code = process.wait()
            check_archive_cancelled(cancelled)
            if return_code != 0:
                raise ValueError(f"RAR archive is damaged or uses unsupported compression (decoder exited {return_code}).")
        finally:
            finished.set()
            if process.poll() is None:
                process.kill()
            if watcher is not None:
                watcher.join()


def _write_rar_entries(
    archive: RarArchive,
    entries: Sequence[tuple[RarMember, PurePosixPath]],
    destination: Path,
    *,
    stream: BinaryIO | None = None,
    cancelled: Event | None,
) -> None:
    for member, path in entries:
        check_archive_cancelled(cancelled)
        target = destination.joinpath(*path.parts)
        try:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with (nullcontext(stream) if stream is not None else archive.open(member)) as source, target.open("xb") as output:
                remaining_bytes = member.file_size
                while remaining_bytes:
                    check_archive_cancelled(cancelled)
                    chunk = source.read(min(_ARCHIVE_COPY_CHUNK_BYTES, remaining_bytes))
                    check_archive_cancelled(cancelled)
                    if not chunk:
                        raise ValueError(f"RAR member size changed while extracting: {member.filename}")
                    output.write(chunk)
                    remaining_bytes -= len(chunk)
        except (FileExistsError, NotADirectoryError, IsADirectoryError) as xcp:
            raise ValueError(f"Archive members have conflicting paths: {path}") from xcp
