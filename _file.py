import enum
import importlib
import importlib.util
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from logging import Logger
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType
from typing import cast

import aiofiles
import hikari

import config
from _async_utils import run_blocking
from archive_safety import extract_rar_entries, open_rar_archive, validated_archive_member_path, validated_rar_entries

log: Logger = logging.getLogger(__name__)
MOD_UPLOAD_STAGING_PREFIX = ".yukibot-mod-upload-"
_DISK_SPACE_RESERVE_BYTES = 256 * 1024**2


class InsufficientDiskSpaceError(ValueError):
    """The target filesystem cannot hold a transfer and its working files."""


def require_free_disk_space(directory: Path, required_bytes: int) -> None:
    """Keep working room beyond a transfer's declared storage requirement."""
    if required_bytes < 0:
        raise ValueError("Required disk space cannot be negative.")
    if shutil.disk_usage(directory).free < required_bytes + _DISK_SPACE_RESERVE_BYTES:
        raise InsufficientDiskSpaceError("Insufficient free disk space for the upload and its temporary files.")


def _stage_file_bytes(pointer: Path, contents: bytes) -> Path:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{pointer.name}-", dir=pointer.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            if pointer.exists():
                temporary.chmod(stat.S_IMODE(pointer.stat().st_mode))
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(pointer: Path, contents: bytes) -> None:
    """Replace a small file after staging its contents, preserving its permissions."""
    temporary = _stage_file_bytes(pointer, contents)
    try:
        temporary.replace(pointer)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_files(contents: Mapping[Path, bytes]) -> None:
    """Stage related small files and roll back replacements if a later replacement fails."""
    destinations: dict[Path, bytes] = {}
    for pointer, data in contents.items():
        destination = pointer.resolve()
        if destination in destinations:
            raise ValueError(f"Multiple writes target the same file: {pointer}")
        destinations[destination] = data
    if len(destinations) == 1:
        pointer, data = next(iter(destinations.items()))
        atomic_write_bytes(pointer, data)
        return

    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        for pointer, data in destinations.items():
            backups[pointer] = _stage_file_bytes(pointer, pointer.read_bytes()) if pointer.exists() else None
            staged[pointer] = _stage_file_bytes(pointer, data)
        failed_pointer: Path | None = None
        try:
            for pointer, temporary in staged.items():
                failed_pointer = pointer
                temporary.replace(pointer)
                replaced.append(pointer)
        except BaseException as error:
            errors: list[BaseException] = [error]
            for pointer in reversed(replaced):
                try:
                    backup = backups[pointer]
                    if backup is None:
                        pointer.unlink()
                    else:
                        backup.replace(pointer)
                except OSError as rollback_error:
                    backup = backups.pop(pointer)
                    if backup is not None:
                        rollback_error.add_note(f"Original contents retained at {backup}.")
                    log.error("File rollback failed: file=%s recovery_copy=%s", pointer, backup)
                    errors.append(rollback_error)
            if len(errors) > 1:
                raise BaseExceptionGroup("File replacement and rollback failed", errors) from None
            if replaced:
                log.warning("File update rolled back: restored_files=%s failed_file=%s", len(replaced), failed_pointer)
            raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class ArchiveKind(enum.StrEnum):
    ZIP = "zip"
    SEVEN_ZIP = "7z"
    RAR = "rar"


class File_Utils:
    @staticmethod
    def _detect_archive_kind(archive_path: Path) -> ArchiveKind:
        if zipfile.is_zipfile(archive_path):
            return ArchiveKind.ZIP
        for kind in (ArchiveKind.SEVEN_ZIP, ArchiveKind.RAR):
            if archive_path.suffix.casefold() == f".{kind.value}":
                return kind
        raise ValueError(f"Unsupported archive type: {archive_path.name}")

    @staticmethod
    def _normalise_archive_member_path(member_name: str) -> Path:
        """Interpret Windows ZIP separators before applying path safety checks."""
        path = validated_archive_member_path(member_name.replace("\\", "/"), archive_label="Archive")
        return Path(*path.parts)

    @classmethod
    def _extract_zip_archive(cls, archive_path: Path, staging_dir: Path) -> None:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in archive.infolist():
                member_name: str = member.filename
                if not member_name:
                    continue
                relative_path: Path = cls._normalise_archive_member_path(member_name)
                target_path: Path = staging_dir / relative_path
                try:
                    if member_name.endswith(("/", "\\")):
                        target_path.mkdir(parents=True, exist_ok=True)
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source, target_path.open("xb") as target:
                        shutil.copyfileobj(source, target)
                except (FileExistsError, NotADirectoryError, IsADirectoryError) as xcp:
                    raise ValueError(f"Archive members have conflicting paths: {relative_path}") from xcp

    @staticmethod
    def _extract_7z_archive(archive_path: Path, staging_dir: Path) -> None:
        if importlib.util.find_spec("py7zr") is not None:
            py7zr_module: ModuleType = importlib.import_module("py7zr")
            seven_zip_file_cls = getattr(py7zr_module, "SevenZipFile")
            with seven_zip_file_cls(archive_path, mode="r") as archive:
                archive.extractall(path=staging_dir)
            return

        for executable_name in ("7zz", "7z", "7za"):
            executable: str | None = shutil.which(executable_name)
            if executable is None:
                continue
            result: CompletedProcess[str] = subprocess.run(
                [executable, "x", "-y", f"-o{staging_dir}", str(archive_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail: str = (
                    result.stderr.strip() or result.stdout.strip() or f"{executable_name} exited {result.returncode}"
                )
                raise RuntimeError(f"7z extraction failed: {detail}")
            return

        raise ValueError("7z extraction requires py7zr or a 7z-compatible executable.")

    @staticmethod
    def _extract_rar_archive(archive_path: Path, staging_dir: Path) -> None:
        limits = config.NODE_API_MOD_ARCHIVE_LIMITS
        with open_rar_archive(archive_path, archive_label="Archive", limits=limits) as archive:
            entries = validated_rar_entries(archive, archive_label="Archive", limits=limits)
            require_free_disk_space(staging_dir, sum(member.file_size for member, _path in entries))
            extract_rar_entries(archive, entries, staging_dir)

    @classmethod
    def _extract_archive_to_directory(
        cls,
        archive_path: Path,
        staging_dir: Path,
        archive_kind: ArchiveKind,
    ) -> None:
        match archive_kind:
            case ArchiveKind.ZIP:
                cls._extract_zip_archive(archive_path, staging_dir)
            case ArchiveKind.SEVEN_ZIP:
                cls._extract_7z_archive(archive_path, staging_dir)
            case ArchiveKind.RAR:
                cls._extract_rar_archive(archive_path, staging_dir)

    @staticmethod
    def _resolved_archive_root(staging_dir: Path) -> Path:
        entries = tuple(staging_dir.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return staging_dir

    @staticmethod
    def _symlink_target(src: Path, dst: Path) -> Path:
        if not src.is_absolute():
            return src

        try:
            return Path(os.path.relpath(src, start=dst.parent))
        except ValueError:
            return src

    @staticmethod
    def append_num(pointer: Path) -> Path:
        for i in range(1, 100):
            candidate: Path = pointer.with_stem(f"{pointer.stem}_{i}")
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise RuntimeError("Too many conflicting zip names")

    @staticmethod
    def remove(target: Path, *, silent: bool = False, resolve: bool = False) -> bool:
        log.debug(f"File.Remove; S={int(silent)} R={int(resolve)}: {target=}")
        try:
            path: Path = target.resolve() if resolve else target
        except FileNotFoundError:
            if silent:
                return True
            raise FileNotFoundError(f"remove.resolve.missing.{target=}")

        if not path.exists():
            if silent:
                return True
            raise FileNotFoundError(f"remove.missing.{path=}")

        try:
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path)
        except Exception:
            log.exception(f"removal failed: {path}")
            if not silent:
                raise
            return False

        return not path.exists()

    @classmethod
    def link(cls, src: Path, dst: Path, overwrite: bool | None = True) -> Path:
        try:
            if overwrite:
                cls.remove(dst, silent=True)
            elif overwrite is None and dst.exists():
                dst = cls.append_num(dst)
            elif dst.exists():
                raise FileExistsError(f"Can't link to existing {dst=}")
            pointer: Path = dst
            pointer.symlink_to(cls._symlink_target(src, pointer), src.is_dir())
        except Exception:
            log.exception(f"link failed: {overwrite=}\n{src}\n{dst}")
            raise
        return src

    @classmethod
    def move(cls, src: Path, target: Path, overwrite: bool | None = True) -> Path:
        try:
            if overwrite:
                cls.remove(target, silent=True)
            elif overwrite is None:
                target = cls.append_num(target)
            elif target.exists():
                raise FileExistsError(f"Can't move to existing {target=}")
            shutil.move(str(src), str(target))
        except Exception:
            log.exception(f"move failed: {overwrite=}\n{src}\n{target}")
            raise
        return target

    @classmethod
    def copy(cls, src: Path, target: Path, overwrite: bool | None = True) -> Path:
        try:
            if overwrite:
                cls.remove(target, silent=True)
            elif overwrite is None:
                target = cls.append_num(target)
            elif target.exists():
                raise FileExistsError(f"Can't copy to existing {target=}")
            shutil.copy(str(src), str(target))
        except Exception:
            log.exception(f"move failed: {overwrite=}\n{src}\n{target}")
            raise
        return target

    @classmethod
    def extract(
        cls,
        src_file: Path,
        dst_dir: Path,
        overwrite: bool | None = True,
        *,
        validate_root: Callable[[Path], None] | None = None,
    ) -> Path:
        """Stage and validate an archive before replacing its destination."""
        try:
            archive_path = Path(src_file)
            archive_kind = cls._detect_archive_kind(archive_path)
            dst_dir.mkdir(parents=True, exist_ok=True)
            extract_base = dst_dir / archive_path.stem
            if extract_base.exists() or extract_base.is_symlink():
                if overwrite is None:
                    extract_base = cls.append_num(extract_base)
                elif not overwrite:
                    raise FileExistsError(f"Can't extract to existing {extract_base=}")

            with tempfile.TemporaryDirectory(prefix="yukibot-extract-", dir=dst_dir) as temp_dir:
                staging_dir = Path(temp_dir) / "contents"
                staging_dir.mkdir()
                cls._extract_archive_to_directory(archive_path, staging_dir, archive_kind)
                resolved_root = cls._resolved_archive_root(staging_dir)
                if validate_root is not None:
                    validate_root(resolved_root)
                previous = Path(temp_dir) / "previous"
                destination_exists = extract_base.exists() or extract_base.is_symlink()
                if overwrite and destination_exists:
                    extract_base.rename(previous)
                elif destination_exists:
                    raise FileExistsError(f"Can't extract to existing {extract_base=}")
                try:
                    resolved_root.rename(extract_base)
                except BaseException:
                    if previous.exists() or previous.is_symlink():
                        previous.rename(extract_base)
                    raise

        except Exception:
            log.exception(f"extraction failed: {overwrite=}\n{src_file}\n{dst_dir}")
            raise

        return extract_base

    @classmethod
    def compress_file(cls, file: Path, zip_path: Path):
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file, arcname=file.name)

    @classmethod
    def compress_files(cls, files: Collection[Path], zip_path: Path, arc_base: Path | None = None):
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file in files:
                arcname = file.relative_to(arc_base) if arc_base else file.name
                zipf.write(file, arcname)

    @classmethod
    def compress_dir(cls, directory: Path, zip_path: Path, arc_base: Path | None = None):
        base = arc_base or directory
        seen: set[tuple[int, int]] = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in directory.walk(follow_symlinks=True):
                root = Path(root)
                stat = root.stat()
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                for dir_ in dirs:
                    dir_path = root / dir_
                    arcname = dir_path.relative_to(base)
                    zipf.writestr(str(arcname) + "/", "")

                for file in files:
                    full_path = root / file
                    arcname = full_path.relative_to(base)
                    zipf.write(full_path, arcname)

    @classmethod
    def compress_dirs(cls, dirs: Collection[Path], zip_path: Path, arc_base: Path | None = None):
        seen: set[tuple[int, int]] = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for directory in dirs:
                if not directory.is_dir():
                    continue
                base = arc_base or directory
                for root, subdirs, files in directory.walk(follow_symlinks=True):
                    root = Path(root)
                    stat = root.stat()
                    key = (stat.st_dev, stat.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    for subdir in subdirs:
                        dir_path = root / subdir
                        arcname = dir_path.relative_to(base)
                        zipf.writestr(str(arcname) + "/", "")

                    for file in files:
                        full_path = root / file
                        arcname = full_path.relative_to(base)
                        zipf.write(full_path, arcname)

    @classmethod
    def compress_paths(cls, paths: Collection[Path], zip_path: Path, arc_base: Path | None = None):
        seen_dirs: set[tuple[int, int]] = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for path in paths:
                if path.is_file():
                    arcname = path.relative_to(arc_base) if arc_base else path.name
                    zipf.write(path, arcname)
                    continue

                if not path.is_dir():
                    raise ValueError(f"Unsupported path type: {path}")

                base = arc_base or path
                for root, subdirs, files in path.walk(follow_symlinks=True):
                    root = Path(root)
                    stat = root.stat()
                    key = (stat.st_dev, stat.st_ino)
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                    for subdir in subdirs:
                        dir_path = root / subdir
                        arcname = dir_path.relative_to(base)
                        zipf.writestr(str(arcname) + "/", "")

                    for file in files:
                        full_path = root / file
                        arcname = full_path.relative_to(base)
                        zipf.write(full_path, arcname)

    @classmethod
    async def compress(
        cls,
        target: Path | Collection[Path],
        zip_name: str,
        overwrite: bool | None = True,
        arc_base: Path | None = None,
    ) -> Path:
        zip_path = config.DIR_ZIPS / zip_name
        if zip_path.exists():
            if overwrite:
                zip_path.unlink()
            elif overwrite is None:
                zip_path = cls.append_num(zip_path)
            else:
                raise FileExistsError(f"{zip_path=} exists and overwrite=False")

        if not zip_path.suffix == ".zip":
            zip_path = zip_path.with_suffix(".zip")

        if isinstance(target, Path):
            cls.ensure_valid_path(target)
            if target.is_file():
                await run_blocking(cls.compress_file, target, zip_path)
            elif target.is_dir():
                await run_blocking(cls.compress_dir, target, zip_path, arc_base)
            else:
                raise ValueError(f"Unsupported path type: {target}")
        else:
            paths = list(target)
            for p in paths:
                cls.ensure_valid_path(p)
            if all(p.is_file() for p in paths):
                await run_blocking(cls.compress_files, paths, zip_path, arc_base)

            elif all(p.is_dir() for p in paths):
                await run_blocking(cls.compress_dirs, paths, zip_path, arc_base)
            else:
                await run_blocking(cls.compress_paths, paths, zip_path, arc_base)

        return zip_path

    @staticmethod
    def ensure_valid_path(path: Path):
        if path.is_symlink() and not path.exists():
            raise FileNotFoundError(f"Broken symlink: {path}")

    @staticmethod
    def pointer_size(pointer: Path) -> int:
        total = 0

        try:
            if pointer.is_symlink():
                # Follow symlinks for files, not dirs
                try:
                    resolved = pointer.resolve(strict=True)
                except FileNotFoundError:
                    return 0
                if resolved.is_file():
                    return resolved.stat().st_size
                elif resolved.is_dir():
                    # Don't recurse into symlinked dirs
                    return 0
            elif pointer.is_file():
                return pointer.stat(follow_symlinks=False).st_size
            elif pointer.is_dir():
                for entry in os.scandir(pointer):
                    try:
                        sub = Path(entry.path)
                        total += File_Utils.pointer_size(sub)
                    except FileNotFoundError:
                        continue
        except Exception as xcp:
            log.exception(f"pointer_size failed on {pointer}: {xcp}")
        return total

    @staticmethod
    async def download_temp(attachment: hikari.Attachment) -> Path:
        suffix = Path(getattr(attachment, "filename", "")).suffix
        with tempfile.NamedTemporaryFile(
            prefix="yukibot-discord-attachment-",
            suffix=suffix,
            dir=config.DIR_TMP,
            delete=False,
        ) as handle:
            path = Path(handle.name)

        try:
            async with aiofiles.open(path, "wb") as f:
                async with attachment.stream() as stream:
                    byte_stream = cast(AsyncIterator[bytes], cast(object, stream))
                    async for chunk_bytes in byte_stream:
                        await f.write(chunk_bytes)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    @staticmethod
    def file_set(target: Path, resolve: bool | None = False) -> set[Path]:
        if resolve not in (True, False, None):
            raise ValueError("resolve must be bool or None")  # pyright: ignore[reportUnreachable]

        try:
            entries = list(target.iterdir())
        except Exception:
            log.exception(f"Failed to list directory: {target}")
            return set()

        if resolve is True:
            files = {p.resolve() for p in entries}
        elif resolve is False:
            files = set(entries)
        else:  # resolve is None
            files = {p.resolve() for p in entries} | set(entries)

        log.info(f"filelist @ {target} [{resolve=}] -> {files}")
        return files


# AiviA APasz
