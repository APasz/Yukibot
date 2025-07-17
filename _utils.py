from collections.abc import Callable
import inspect
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, overload

from dateutil.relativedelta import relativedelta

import config
from _file import File_Utils

log = logging.getLogger(__name__)


class File_Cleaner(metaclass=config.Singleton):
    def __init__(self):
        self.folders_to_clear: dict[Path, timedelta] = {
            config.DIR_UPLOAD: config.UPLOAD_CLEAR_TIME,
            config.DIR_ZIPS: config.UPLOAD_CLEAR_TIME * 1.2,
            config.DIR_TMP: config.UPLOAD_CLEAR_TIME * 1.2,
        }
        self.files_to_clear: dict[Path, timedelta] = {}
        self.symfiles_to_clear: dict[Path, timedelta] = {}

    @staticmethod
    def clear(paths: Path | set[Path], threshold: timedelta = timedelta(seconds=1)) -> set[Path]:
        removed: set[Path] = set()
        now = datetime.now()

        if isinstance(paths, Path):
            if not paths.is_dir():
                raise SystemError(f"Single Path object must be directory: {paths}")
            if File_Utils.remove(paths, silent=True, resolve=False):
                removed.add(paths)
            return removed

        invalid = {p for p in paths if not (p.is_file() or p.is_symlink())}
        if invalid:
            raise SystemError(f"All paths must be files/symlinks: {invalid}")

        for path in paths:
            if not path.exists():
                removed.add(path)
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if now - mtime > threshold:
                log.warning(f"File removed: {path}")
                if File_Utils.remove(path, silent=True, resolve=False):
                    removed.add(path)

        return paths - removed


class Utilities:
    "Collection of various functions that do little things"

    MAGNITUDES = "BKMGTPEZY"

    @staticmethod
    def bytes_magnitude(byte_num: int, use_iec: bool, magnitude: str, precision: int = 3) -> float:
        """Does the math of turning a number of bytes or bits into the appropriate number for the given magnitude

        Args;
            byte_num: Number of bytes or bits
            magnitude: Notation to use ('B', 'K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
            use_iec: Whether to use powers of 1024 (IEC) or 1000 (SI)
            is_bit: Whether byte_num represents bits instead

        Returns;
            float: Resulting number
        """
        unit = 1024 if use_iec else 1000
        if magnitude.upper() not in Utilities.MAGNITUDES:
            raise ValueError(f"Invalid magnitude: {magnitude}")

        return round(byte_num / math.pow(unit, Utilities.MAGNITUDES.index(magnitude.upper())), precision)

    @staticmethod
    def find_magnitude(byte_num: int, use_iec: bool = True) -> str:
        """Finds appropriate magnitude based on byte_num

        Args;
            byte_num: Number of bytes or bits
            use_iec: Whether to use powers of 1024 (IEC) or 1000 (SI)

        Returns;
            str: Notation corresponding to the magnitude of byte_num
        """
        unit = 1024 if use_iec else 1000
        for i, magnitude in enumerate(Utilities.MAGNITUDES):
            if byte_num < (unit ** (i + 1)):
                return magnitude
        return "Y"

    @staticmethod
    def humanise_bytes(
        byte_num: int,
        /,
        is_bit: bool = False,
        convert: bool = False,
        use_iec: bool = True,
        magnitude: str | None = None,
        precision: int = 2,
    ) -> str:
        """Return string with appropriate notation for a number of bytes

        Args;
            byte_num: Number of bytes or bits
            is_bit: Whether byte_num represents bits instead of bytes
            convert: Whether to convert byte_num between bits and bytes
            use_iec: Whether to use powers of 1024 (IEC) or 1000 (SI)
            magnitude: Specific notation to use
            precision: Decimal precision of the result

        Raises;
            ValueError: If byte_num is not an int or magnitude is invalid

        Returns;
            str: Formatted string with the appropriate notation
        """
        if isinstance(byte_num, float):
            byte_num = round(byte_num)
        elif isinstance(byte_num, str):
            byte_num = int(byte_num)
        elif not isinstance(byte_num, int):
            raise ValueError(f"byte_num must be an int, got {type(byte_num)}")
        if convert:
            if is_bit:
                byte_num = round((byte_num / 8))
                is_bit = False
            else:
                byte_num *= 8
                is_bit = True

        if magnitude is None:
            magnitude = Utilities.find_magnitude(byte_num, use_iec)
        elif not isinstance(magnitude, str):
            raise ValueError(f"Magnitude must be a str, got {type(magnitude)}")
        elif magnitude.upper() not in Utilities.MAGNITUDES:
            raise ValueError(f"Unrecognized magnitude: {magnitude}")

        size = Utilities.bytes_magnitude(byte_num, use_iec, magnitude, precision)
        if precision == 0:
            size = int(size)
        magnitude = magnitude.upper() if magnitude != "B" else ""
        power = "i" if use_iec and magnitude else ""
        unit = "b" if is_bit else "B"

        return f"{size}{magnitude}{power}{unit}"

    @staticmethod
    def parse_time(string: str) -> datetime | None:
        """Parse timestamp or human time into datetime object

        Args;
            string: Timestamp (1641591242) or human-readable (2h, 3h45m, 20m)

        Returns;
            datetime object or None if there was error or no match
        """
        if not isinstance(string, str):
            raise ValueError(f"string must be of type str not: {type(string)}")

        if string.isnumeric():
            if len(string) > 11:
                log.warning("string unreasonably long: %s", string)
                return None
            return datetime.fromtimestamp(int(string), timezone.utc)

        pattern = r"((?P<hours>\d+)h)?((?P<minutes>\d+)m)?"
        match = re.fullmatch(pattern, string.strip())
        if not match:
            log.warning("No matches were found: %s", string)
            return None

        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)

        if hours == 0 and minutes == 0:
            log.warning("hours and minutes 0")
            return None

        td = timedelta(hours=hours, minutes=minutes)
        log.debug("Successful parse of %s > %s", string, td)
        return datetime.now(timezone.utc) + td

    @staticmethod
    def format_rdelta(delta: relativedelta) -> str:
        parts = []
        if delta.years:
            parts.append(f"{delta.years}y")
        if delta.months:
            parts.append(f"{delta.months}mo")
        if delta.days:
            parts.append(f"{delta.days}d")
        if delta.hours:
            parts.append(f"{delta.hours}h")
        if delta.minutes:
            parts.append(f"{delta.minutes}m")
        if delta.seconds:
            parts.append(f"{delta.seconds}s")
        return " ".join(parts) or "0s"

    @overload
    @staticmethod
    def create_rdelta(start: datetime, end: datetime) -> relativedelta: ...

    @overload
    @staticmethod
    def create_rdelta(total_seconds: float | int, /) -> relativedelta: ...

    @staticmethod
    def create_rdelta(start: datetime | float | int, end: datetime | None = None) -> relativedelta:
        if isinstance(start, (float, int)):
            return relativedelta(seconds=int(start))
        elif isinstance(start, datetime) and isinstance(end, datetime):
            return relativedelta(end, start)
        raise ValueError(f"Unsupported types: {start=}:{type(start)} | {end=}:{type(end)}")

    @staticmethod
    def chunket(text: str, length: int, separator: str | None = None) -> list[str]:
        """Splits a string into chunks of at most `length` characters,
        optionally preferring to split at the last occurrence of `separator`.

        Args;
            text: The string to split.
            length: Maximum length of each chunk.
            separator: Optional character to prefer as a split point.

        Returns;
            List of string chunks.
        """
        chunks = []
        i = 0
        while i < len(text):
            end = i + length
            chunk = text[i:end]
            if separator and separator in chunk and end < len(text):
                sep_pos = chunk.rfind(separator)
                if sep_pos > 0:
                    end = i + sep_pos + 1  # include separator
                    chunk = text[i:end]
            chunks.append(chunk)
            i = end
        return chunks

    @staticmethod
    def nice_time(delta: timedelta | None = None, date: datetime | None = None, fmt: str = "f") -> str:
        if not delta:
            delta = timedelta(seconds=0)
        if not date:
            date = datetime.now(timezone.utc)
        return f"<t:{int((date + delta).timestamp())}:{fmt}>"

    @staticmethod
    def linkify(target: Path) -> tuple[str, Path]:
        up_target = config.DIR_UPLOAD / target.name
        up_target = File_Utils.link(target, up_target, overwrite=None)
        return (config.PUBLIC_URL_BASE + target.name, up_target)

    @staticmethod
    def is_awaitable(func: Callable[[], Any]) -> bool:
        try:
            result = func()
        except Exception:
            return False  # Or True, or re-raise, depending on how tolerant you want to be
        return inspect.isawaitable(result)


# AiviA APasz
