import calendar
import inspect
import logging
import math
import re
from collections.abc import Callable
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

    @classmethod
    def parse_time(cls, string: str, tz: timezone = timezone.utc) -> datetime | None:
        """
        Parse a timestamp or a human-friendly duration into a UTC datetime.

        Accepted inputs (optional leading + or -):
          1) UNIX epoch seconds: "1641591242", "+1641591242", "-31536000"
             (commas/underscores allowed: "1,641,591,242", "1_641_591_242")
          2) Duration tokens (order-free, case-insensitive):
               y  years, mo months, w weeks, d days, h hours, m minutes, s seconds
             Examples: "2h", "3h45m", "1y4m", "2y3mo5d9m", "10m30s", "1w2d"
          3) Colon durations (no unit letters):
               HH:MM | HH:MM:SS | DD:HH:MM:SS | WW:DD:HH:MM:SS
             Examples: "2:30", "1:02:03", "3:12:00:00", "-2:03:12:00:00"
        """
        if not isinstance(string, str):
            raise ValueError(f"string must be of type str not: {type(string)}")

        s_raw = string.strip()
        # allow visual separators in any form
        string = s_raw.replace(",", "").replace("_", "")
        if not string:
            return None

        # peel an optional leading sign for relative handling

        sign = 1
        if string[0] == "+":
            string = string[1:]
        elif string[0] == "-":
            sign = -1
            string = string[1:]

        now = datetime.now(tz)

        if string.isnumeric():
            return datetime.fromtimestamp(int(string), tz=tz)

        # 2) Colon durations (no letters)
        if ":" in string and not re.search(r"[a-zA-Z]", string):
            parts = string.split(":")
            if not all(p.isdigit() for p in parts):
                log.warning("Invalid colon duration: %s", s_raw)
                return None

            values = list(map(int, parts))
            weeks = days = hours = minutes = seconds = 0

            if len(values) == 2:  # HH:MM
                hours, minutes = values
            elif len(values) == 3:  # HH:MM:SS
                hours, minutes, seconds = values
            elif len(values) == 4:  # DD:HH:MM:SS
                days, hours, minutes, seconds = values
            elif len(values) == 5:  # WW:DD:HH:MM:SS
                weeks, days, hours, minutes, seconds = values
            else:
                log.warning("Unsupported colon format: %s", s_raw)
                return None

            if weeks == days == hours == minutes == seconds == 0:
                log.warning("All zero duration in colon format: %s", s_raw)
                return None

            td = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
            td = td if sign > 0 else -td
            log.debug("Successful parse of %s > %s", s_raw, td)
            return now + td

        # 3) Tokenized durations with units (y, mo, w, d, h, m, s)
        t = re.sub(r"\s+", "", string.lower())
        if not re.fullmatch(r"(?:(?:\d+)(?:y|mo|w|d|h|m|s))+", t):
            log.warning("No matches were found: %s", s_raw)
            return None

        years = months = weeks = days = hours = minutes = seconds = 0
        for m in re.finditer(r"(\d+)(y|mo|w|d|h|m|s)", t):
            val = int(m.group(1))
            unit = m.group(2)
            if unit == "y":
                years += val
            elif unit == "mo":
                months += val
            elif unit == "w":
                weeks += val
            elif unit == "d":
                days += val
            elif unit == "h":
                hours += val
            elif unit == "m":
                minutes += val
            elif unit == "s":
                seconds += val

        if years == months == weeks == days == hours == minutes == seconds == 0:
            log.warning("All components zero: %s", s_raw)
            return None

        dt = cls._add_years_months(now, years=sign * years, months=sign * months)
        td = timedelta(
            weeks=sign * weeks, days=sign * days, hours=sign * hours, minutes=sign * minutes, seconds=sign * seconds
        )

        log.debug(
            "Successful parse of %s > years=%d, months=%d, weeks=%d, td=%s, sign=%s",
            s_raw,
            years,
            months,
            weeks,
            td,
            "+" if sign > 0 else "-",
        )
        return dt + td

    @staticmethod
    def _add_years_months(dt: datetime, *, years: int = 0, months: int = 0) -> datetime:
        """Add years/months with calendar rules; clamp day to end-of-month."""
        if years == 0 and months == 0:
            return dt
        total_months = (dt.year * 12 + (dt.month - 1)) + years * 12 + months
        new_year, new_month0 = divmod(total_months, 12)
        new_month = new_month0 + 1
        last_day = calendar.monthrange(new_year, new_month)[1]
        new_day = min(dt.day, last_day)
        return dt.replace(year=new_year, month=new_month, day=new_day)

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
            return False
        return inspect.isawaitable(result)


# AiviA APasz
