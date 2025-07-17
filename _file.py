import asyncio
import logging
import os
import shutil
import zipfile
from collections.abc import Collection
from pathlib import Path

import aiofiles
import hikari

import config

log = logging.getLogger(__name__)


class File_Utils:
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
            target = src  # pathlib is pov from symlink
            pointer = dst
            pointer.symlink_to(target.resolve(), pointer.is_dir())
        except Exception:
            log.exception(f"link failed: {overwrite=}\n{src}\n{dst}")
            raise
        return target

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
            zip_path = Path(src_file)
            if not zipfile.is_zipfile(zip_path):
                raise FileNotFoundError("not zipfile")

            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                names = zip_ref.namelist()
                non_dirs = [n for n in names if not n.endswith("/")]
                split_names = [n.split("/", 1) for n in non_dirs if "/" in n]
                top_levels = {parts[0] for parts in split_names}

                if len(top_levels) == 1 and all(n.startswith(f"{list(top_levels)[0]}/") for n in non_dirs):
                    prefix = next(iter(top_levels)) + "/"
                else:
                    prefix = ""

                extract_base = dst_dir / zip_path.stem

                for member in zip_ref.infolist():
                    name = member.filename
                    if not name or name.endswith("/"):
                        continue

                    rel_path = name[len(prefix) :] if prefix and name.startswith(prefix) else name
                    if not rel_path:
                        continue

                    target = extract_base / rel_path

                    if overwrite:
                        cls.remove(target, silent=True)
                    elif overwrite is None:
                        target = cls.append_num(target)
                    elif target.exists():
                        raise FileExistsError(f"Can't move to existing {target=}")

                    target.parent.mkdir(parents=True, exist_ok=True)

                    with zip_ref.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())

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
        seen = set()
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
        seen = set()
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
        elif isinstance(target, Collection):
            paths = list(target)
            for p in paths:
                cls.ensure_valid_path(p)
            if all(p.is_file() for p in paths):
                await asyncio.to_thread(cls.compress_files, paths, zip_path, arc_base)

            elif all(p.is_dir() for p in paths):
                await asyncio.to_thread(cls.compress_dirs, paths, zip_path, arc_base)
            else:
                raise ValueError("Mixed or invalid collection passed to compress()")
        else:
            raise TypeError("target must be a Path or Collection[Path]")

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
                resolved = pointer.resolve(strict=True)
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
        path = config.DIR_TMP / attachment.filename
        async with aiofiles.open(path, "wb") as f:
            async with attachment.stream() as stream:
                async for chunk in stream:
                    await f.write(chunk)
        return path

    @staticmethod
    def file_set(target: Path, resolve: bool | None = False) -> set[Path]:
        if resolve not in (True, False, None):
            raise ValueError("resolve must be bool or None")

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
