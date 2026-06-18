import asyncio
import enum
import importlib
import importlib.util
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import AsyncIterator, Collection
from pathlib import Path, PurePosixPath
from typing import cast

import aiofiles
import hikari

import config

log = logging.getLogger(__name__)


class ArchiveKind(enum.StrEnum):
    ZIP = "zip"
    SEVEN_ZIP = "7z"


class File_Utils:
    @staticmethod
    def _detect_archive_kind(archive_path: Path) -> ArchiveKind:
        if zipfile.is_zipfile(archive_path):
            return ArchiveKind.ZIP
        if archive_path.suffix.casefold() == ".7z":
            return ArchiveKind.SEVEN_ZIP
        raise ValueError(f"Unsupported archive type: {archive_path.name}")

    @staticmethod
    def _normalise_archive_member_path(member_name: str) -> Path:
        resolved = PurePosixPath(member_name)
        if not member_name or resolved.is_absolute() or ".." in resolved.parts:
            raise ValueError(f"Archive member path is invalid: {member_name}")
        return Path(*resolved.parts)

    @classmethod
    def _extract_zip_archive(cls, archive_path: Path, staging_dir: Path) -> None:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in archive.infolist():
                member_name = member.filename
                if not member_name:
                    continue
                relative_path = cls._normalise_archive_member_path(member_name)
                target_path = staging_dir / relative_path
                if member_name.endswith("/"):
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, open(target_path, "wb") as target:
                    target.write(source.read())

    @staticmethod
    def _extract_7z_archive(archive_path: Path, staging_dir: Path) -> None:
        if importlib.util.find_spec("py7zr") is not None:
            py7zr_module = importlib.import_module("py7zr")
            seven_zip_file_cls = getattr(py7zr_module, "SevenZipFile")
            with seven_zip_file_cls(archive_path, mode="r") as archive:
                archive.extractall(path=staging_dir)
            return

        for executable_name in ("7zz", "7z", "7za"):
            executable = shutil.which(executable_name)
            if executable is None:
                continue
            result = subprocess.run(
                [executable, "x", "-y", f"-o{staging_dir}", str(archive_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or f"{executable_name} exited {result.returncode}"
                raise RuntimeError(f"7z extraction failed: {detail}")
            return

        raise ValueError("7z extraction requires py7zr or a 7z-compatible executable.")

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
            candidate = pointer.with_stem(f"{pointer.stem}_{i}")
            if not candidate.exists():
                return candidate
        raise RuntimeError("Too many conflicting zip names")

    @staticmethod
    def remove(target: Path, *, silent: bool = False, resolve: bool = False) -> bool:
        log.debug(f"File.Remove; S={int(silent)} R={int(resolve)}: {target=}")
        try:
            path = target.resolve() if resolve else target
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
            pointer = dst
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
    def extract(cls, src_file: Path, dst_dir: Path, overwrite: bool | None = True) -> Path:
        try:
            archive_path = Path(src_file)
            archive_kind = cls._detect_archive_kind(archive_path)
            dst_dir.mkdir(parents=True, exist_ok=True)
            extract_base = dst_dir / archive_path.stem
            if overwrite:
                cls.remove(extract_base, silent=True)
            elif overwrite is None and extract_base.exists():
                extract_base = cls.append_num(extract_base)
            elif extract_base.exists():
                raise FileExistsError(f"Can't extract to existing {extract_base=}")

            extract_base.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="yukibot-extract-") as temp_dir:
                staging_dir = Path(temp_dir)
                cls._extract_archive_to_directory(archive_path, staging_dir, archive_kind)
                resolved_root = cls._resolved_archive_root(staging_dir)
                for entry in resolved_root.iterdir():
                    target = extract_base / entry.name
                    if target.exists():
                        raise FileExistsError(f"Can't move to existing {target=}")
                    shutil.move(str(entry), str(target))

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
                await asyncio.to_thread(cls.compress_file, target, zip_path)
            elif target.is_dir():
                await asyncio.to_thread(cls.compress_dir, target, zip_path, arc_base)
            else:
                raise ValueError(f"Unsupported path type: {target}")
        else:
            paths = list(target)
            for p in paths:
                cls.ensure_valid_path(p)
            if all(p.is_file() for p in paths):
                await asyncio.to_thread(cls.compress_files, paths, zip_path, arc_base)

            elif all(p.is_dir() for p in paths):
                await asyncio.to_thread(cls.compress_dirs, paths, zip_path, arc_base)
            else:
                await asyncio.to_thread(cls.compress_paths, paths, zip_path, arc_base)

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
