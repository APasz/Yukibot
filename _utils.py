import calendar
import inspect
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from functools import cache
from pathlib import Path
from typing import Any, overload
from urllib.parse import quote
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones

from dateutil.relativedelta import relativedelta

import config
from _file import File_Utils

log = logging.getLogger(__name__)
_UNLIMITED_PLAYER_CAPACITY_SENTINEL: int = -1
_UNLIMITED_PLAYER_CAPACITY_TEXT: str = "∞"


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
    def clear(paths: Path | set[Path], threshold: timedelta | None = None) -> set[Path]:
        threshold = threshold or timedelta(seconds=1)
        removed: set[Path] = set()
        now = datetime.now()

        if isinstance(paths, Path):
            if not paths.is_dir():
                raise SystemError(f"Single Path object must be directory: {paths}")
            if File_Utils.remove(paths, silent=True, resolve=False):
                removed.add(paths)
            return removed

        invalid = {p for p in paths if not File_Cleaner._is_clearable_path(p)}
        if invalid:
            raise SystemError(f"All paths must be files, directories, or symlinks: {invalid}")

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

    @staticmethod
    def _is_clearable_path(path: Path) -> bool:
        return not path.exists() or path.is_file() or path.is_dir() or path.is_symlink()


def format_player_capacity(player_capacity: int | None) -> str | None:
    if player_capacity is None:
        return None
    if player_capacity == _UNLIMITED_PLAYER_CAPACITY_SENTINEL:
        return _UNLIMITED_PLAYER_CAPACITY_TEXT
    return str(player_capacity)


@dataclass(frozen=True, slots=True)
class TimezoneSelectionOption:
    """One timezone choice rendered by the timestamp picker."""

    value: str
    timezone_code: str
    offset_text: str
    location_text: str | None


@dataclass(frozen=True, slots=True)
class _TimezoneSelectionCatalogueEntry:
    """Static timezone metadata used to build a current picker option."""

    value: str
    location_text: str | None
    location_search_text: str
    country_codes: frozenset[str]


class Utilities:
    "Collection of various functions that do little things"

    MAGNITUDES = "BKMGTPEZY"
    DISCORD_TIMESTAMP_FORMATS: tuple[tuple[str, str], ...] = (
        ("Short Time", "<t:{}:t>"),
        ("Long Time", "<t:{}:T>"),
        ("Short Date", "<t:{}:d>"),
        ("Long Date", "<t:{}:D>"),
        ("Long Date / Short Time", "<t:{}:f>"),
        ("Full Date / Short Time", "<t:{}:F>"),
        ("Short Date / Short Time", "<t:{}:s>"),
        ("Short Date / Medium Time", "<t:{}:S>"),
        ("Relative Time", "<t:{}:R>"),
    )
    DISCORD_TIMESTAMP_STYLE_REPRESENTATIONS: dict[str, str] = {
        "t": "HH:MM",
        "T": "HH:MM:SS",
        "d": "DD/MM/YYYY",
        "D": "Mon DD YYYY",
        "f": "Mon DD YYYY HH:MM",
        "F": "Day Mon DD YYYY HH:MM",
        "s": "YYYY-MM-DD HH:MM",
        "S": "YYYY-MM-DD HH:MM:SS",
        "R": "in 2 hours",
    }
    TIMESTAMP_ROUNDING_UNITS: tuple[str, ...] = ("Y", "MO", "W", "D", "H", "MI", "S")
    _TZ_OFFSET_RE = re.compile(r"^(?:(?:UTC|GMT)\s*)?(?P<sign>[+-])(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?$", re.IGNORECASE)
    _CLOCK_RE = re.compile(
        r"^(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?(?P<tz>Z|[+-]\d{1,2}:?\d{2})?$",
        re.IGNORECASE,
    )
    _ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    _DMY_DATE_RE = re.compile(r"^(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{2}|\d{4})$")
    _DMY_DATETIME_RE = re.compile(
        (
            r"^(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{2}|\d{4})\s+"
            r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?(?P<tz>Z|[+-]\d{1,2}:?\d{2})?$"
        ),
        re.IGNORECASE,
    )
    _IANA_ZONES = available_timezones()
    _TZ_ALIASES = {
        "MELBOURNE": "Australia/Melbourne",
        "LONDON": "Europe/London",
        "ZURICH": "Europe/Zurich",
        "HELSINKI": "Europe/Helsinki",
    }

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
            raise ValueError(f"byte_num must be an int, got {type(byte_num)}")  # pyright: ignore[reportUnreachable]
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
            raise ValueError(f"Magnitude must be a str, got {type(magnitude)}")  # pyright: ignore[reportUnreachable]
        elif magnitude.upper() not in Utilities.MAGNITUDES:
            raise ValueError(f"Unrecognised magnitude: {magnitude}")

        size = Utilities.bytes_magnitude(byte_num, use_iec, magnitude, precision)
        if precision == 0:
            size = int(size)
        magnitude = magnitude.upper() if magnitude != "B" else ""
        power = "i" if use_iec and magnitude else ""
        unit = "b" if is_bit else "B"

        return f"{size}{magnitude}{power}{unit}"

    @classmethod
    def parse_time(cls, string: str, tz: tzinfo = timezone.utc) -> datetime | None:
        """
        Parse a timestamp, absolute datetime, or a human-friendly duration into a tz-aware datetime.

        Accepted inputs (optional leading + or -):
          0) Absolute datetime:
               - ISO date/time: "2026-02-06T14:30:00+10:00", "2026-02-06 14:30", "2026-02-06T14:30Z"
               - D/M/Y date/time: "07/02/26 01:00", "07/02/2026 01:00:30+11:00"
               - "[zone] [time] [date]": "UTC+10:00 14:30 2026-02-06"
               - "[time-with-tz] [date]": "14:30+10:00 2026-02-06"
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
            raise ValueError(f"string must be of type str not: {type(string)}")  # pyright: ignore[reportUnreachable]

        s_raw = string.strip()
        if not s_raw:
            return None

        # Exact forms are checked first to avoid clashing with relative syntax.
        if exact := cls.parse_exact_time(s_raw, tz=tz):
            return exact

        return cls.parse_relative_time(s_raw, tz=tz)

    @classmethod
    def parse_exact_time(cls, value: str, tz: tzinfo = timezone.utc) -> datetime | None:
        """Parse an epoch or an absolute date/time, excluding relative durations."""
        if not isinstance(value, str):
            raise ValueError(f"value must be of type str not: {type(value)}")  # pyright: ignore[reportUnreachable]

        s_raw = value.strip()
        if not s_raw:
            return None

        if absolute := cls.parse_absolute_time(s_raw, tz=tz):
            return absolute

        normalized = s_raw.replace(",", "").replace("_", "")
        if normalized.lstrip("+-").isnumeric():
            try:
                return datetime.fromtimestamp(int(normalized), tz=tz)
            except (OverflowError, OSError, ValueError):
                return None

        return None

    @classmethod
    def parse_relative_time(cls, value: str, tz: tzinfo = timezone.utc) -> datetime | None:
        """Parse a duration relative to now, excluding epoch and absolute inputs."""
        if not isinstance(value, str):
            raise ValueError(f"value must be of type str not: {type(value)}")  # pyright: ignore[reportUnreachable]

        s_raw = value.strip()
        if not s_raw:
            return None

        # allow visual separators in any form
        string = s_raw.replace(",", "").replace("_", "")

        # peel an optional leading sign for relative handling

        sign = 1
        if string[0] == "+":
            string = string[1:]
        elif string[0] == "-":
            sign = -1
            string = string[1:]

        now = datetime.now(tz)

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

        # 3) Tokenised durations with units (y, mo, w, d, h, m, s)
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
    def _start_of_next_month(dt: datetime) -> tuple[datetime, datetime]:
        first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        return first, next_month

    @staticmethod
    def _start_of_next_year(dt: datetime) -> tuple[datetime, datetime]:
        first = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return first, first.replace(year=first.year + 1)

    @classmethod
    def round_wallclock(cls, dt: datetime, unit: str) -> datetime:
        """Round an aware datetime to the nearest unit in its own timezone."""
        if unit not in cls.TIMESTAMP_ROUNDING_UNITS:
            raise ValueError(f"Unknown timestamp rounding unit: {unit}")
        timezone_info = dt.tzinfo or timezone.utc
        local = dt.astimezone(timezone_info)

        if unit == "S":
            if local.microsecond >= 500_000:
                local += timedelta(seconds=1)
            return local.replace(microsecond=0)
        if unit == "MI":
            rounded = local.replace(second=0, microsecond=0)
            return rounded + timedelta(minutes=1) if local.second >= 30 else rounded
        if unit == "H":
            rounded = local.replace(minute=0, second=0, microsecond=0)
            return rounded + timedelta(hours=1) if (local.minute, local.second, local.microsecond) >= (30, 0, 0) else rounded
        if unit == "D":
            rounded = local.replace(hour=0, minute=0, second=0, microsecond=0)
            return rounded + timedelta(days=1) if (local.hour, local.minute, local.second, local.microsecond) >= (12, 0, 0, 0) else rounded
        if unit == "W":
            start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            return start + timedelta(days=7) if local >= start + timedelta(days=3, hours=12) else start
        if unit == "MO":
            start, next_month = cls._start_of_next_month(local)
            return next_month if local >= start + (next_month - start) / 2 else start
        start, next_year = cls._start_of_next_year(local)
        return next_year if local >= start + (next_year - start) / 2 else start

    @classmethod
    def parse_timezone(cls, value: str) -> tzinfo | None:
        if not isinstance(value, str):
            return None  # pyright: ignore[reportUnreachable]

        raw = value.strip()
        if not raw:
            return None
        up = raw.upper()

        if up in {"UTC", "Z", "GMT"}:
            return timezone.utc

        # Common city aliases / friendly labels.
        if up in cls._TZ_ALIASES:
            try:
                return ZoneInfo(cls._TZ_ALIASES[up])
            except ZoneInfoNotFoundError:
                pass

        m = cls._TZ_OFFSET_RE.fullmatch(up)
        if m:
            sign = -1 if m.group("sign") == "-" else 1
            hours = int(m.group("h"))
            minutes = int(m.group("m") or "0")

            if minutes >= 60:
                return None
            if sign < 0 and (hours > 12 or (hours == 12 and minutes > 0)):
                return None
            if sign > 0 and (hours > 14 or (hours == 14 and minutes > 0)):
                return None

            return timezone(sign * timedelta(hours=hours, minutes=minutes))

        # Try exact IANA zone.
        try:
            return ZoneInfo(raw)
        except (ValueError, ZoneInfoNotFoundError):
            pass

        # If a city-like token was provided (e.g. "Melbourne"), match zone suffix.
        city_token = re.sub(r"[\s\-]+", "_", raw).casefold()
        if "/" not in city_token:
            matches = sorted(z for z in cls._IANA_ZONES if z.rsplit("/", 1)[-1].casefold() == city_token)
            if matches:
                try:
                    return ZoneInfo(matches[0])
                except ZoneInfoNotFoundError:
                    return None

        return None

    @staticmethod
    def _timezone_offset_label(total_minutes: int) -> str:
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    @classmethod
    @cache
    def _timezone_selection_catalog(cls) -> tuple[_TimezoneSelectionCatalogueEntry, ...]:
        zone_records: dict[str, tuple[frozenset[str], str]] = {}
        for timezone_path in TZPATH:
            zone_tab_path = Path(timezone_path) / "zone.tab"
            if not zone_tab_path.is_file():
                continue
            for raw_line in zone_tab_path.read_text(encoding="utf-8").splitlines():
                if not raw_line or raw_line.startswith("#"):
                    continue
                fields = raw_line.split("\t", maxsplit=3)
                if len(fields) < 3:
                    continue
                zone_name = fields[2]
                if zone_name not in cls._IANA_ZONES:
                    continue
                country_codes = frozenset(fields[0].split(","))
                location_description = fields[3] if len(fields) == 4 else ""
                zone_records.setdefault(zone_name, (country_codes, location_description))
            break
        if not zone_records:
            zone_records = {zone_name: (frozenset(), "") for zone_name in cls._IANA_ZONES}

        entries: list[_TimezoneSelectionCatalogueEntry] = [
            _TimezoneSelectionCatalogueEntry(
                value="UTC",
                location_text=None,
                location_search_text="",
                country_codes=frozenset(),
            )
        ]
        for total_minutes in range(-(12 * 60), (14 * 60) + 1, 15):
            if total_minutes == 0:
                continue
            value = cls._timezone_offset_label(total_minutes)
            entries.append(
                _TimezoneSelectionCatalogueEntry(
                    value=value,
                    location_text=None,
                    location_search_text="",
                    country_codes=frozenset(),
                )
        )
        for zone_name, (country_codes, location_description) in sorted(zone_records.items()):
            location_text = f"{zone_name}{f' · {location_description}' if location_description else ''}"
            entries.append(
                _TimezoneSelectionCatalogueEntry(
                    value=zone_name,
                    location_text=location_text,
                    location_search_text=" ".join(
                        (zone_name.rsplit("/", maxsplit=1)[-1].replace("_", " "), location_description)
                    ).casefold(),
                    country_codes=country_codes,
                )
            )
        return tuple(entries)

    @staticmethod
    def _supported_timezone_country_codes() -> frozenset[str]:
        return frozenset(country.value for country in config.supported_conversion_countries())

    @classmethod
    def _timezone_selection_option(
        cls,
        *,
        entry: _TimezoneSelectionCatalogueEntry,
        now: datetime,
    ) -> TimezoneSelectionOption | None:
        if entry.value == "UTC":
            return TimezoneSelectionOption(
                value="UTC",
                timezone_code="UTC",
                offset_text="Universal",
                location_text=None,
            )
        if entry.value.startswith("UTC"):
            return TimezoneSelectionOption(
                value=entry.value,
                timezone_code=entry.value,
                offset_text="Fixed offset",
                location_text=None,
            )
        try:
            zone = ZoneInfo(entry.value)
        except (ValueError, ZoneInfoNotFoundError):
            return None
        current_time = now.astimezone(zone)
        offset = current_time.utcoffset() or timedelta()
        return TimezoneSelectionOption(
            value=entry.value,
            timezone_code=current_time.tzname() or entry.value,
            offset_text=cls._timezone_offset_label(int(offset.total_seconds() // 60)).removeprefix("UTC"),
            location_text=entry.location_text,
        )

    @classmethod
    def timezone_selection_options(cls, query: str | None = None) -> tuple[TimezoneSelectionOption, ...]:
        """Return unique, context-sensitive timezone choices for the timestamp picker."""
        normalized_query = "" if query is None else query.strip().casefold()
        entries = cls._timezone_selection_catalog()
        if not normalized_query:
            supported_country_codes = cls._supported_timezone_country_codes()
            candidate_entries = (
                entry
                for entry in entries
                if entry.value == "UTC" or entry.country_codes.intersection(supported_country_codes)
            )
        else:
            candidate_entries = entries
        now = datetime.now(timezone.utc)
        option_entries = tuple(
            (entry, option)
            for entry in candidate_entries
            if (option := cls._timezone_selection_option(entry=entry, now=now)) is not None
        )
        if not normalized_query:
            matches = tuple(option for _, option in option_entries)
        elif normalized_query.isalpha() and len(normalized_query) < 3:
            matches = tuple(
                option
                for _, option in option_entries
                if option.timezone_code.casefold().startswith(normalized_query)
            )
        elif normalized_query.isalpha():
            matches = tuple(
                option
                for entry, option in option_entries
                if normalized_query in option.timezone_code.casefold()
                or normalized_query in entry.value.casefold()
                or normalized_query in entry.location_search_text
            )
        else:
            parsed_timezone = cls.parse_timezone(normalized_query)
            offset = None if parsed_timezone is None else parsed_timezone.utcoffset(None)
            if offset is None:
                matches = tuple(
                    option
                    for entry, option in option_entries
                    if normalized_query in entry.value.casefold()
                    or normalized_query in option.timezone_code.casefold()
                )
            else:
                offset_value = cls._timezone_offset_label(int(offset.total_seconds() // 60))
                matches = tuple(option for _, option in option_entries if option.value == offset_value)
        include_locations = normalized_query.isalpha() and len(normalized_query) >= 3
        if include_locations:
            return matches
        deduplicated: dict[tuple[str, str], TimezoneSelectionOption] = {}
        preferred_timezone_names = frozenset(cls._TZ_ALIASES.values())
        for entry in matches:
            key = (entry.timezone_code, entry.offset_text)
            option = TimezoneSelectionOption(
                value=entry.value,
                timezone_code=entry.timezone_code,
                offset_text=entry.offset_text,
                location_text=None,
            )
            existing = deduplicated.get(key)
            if existing is None or (
                entry.value in preferred_timezone_names and existing.value not in preferred_timezone_names
            ):
                deduplicated[key] = option
        return tuple(deduplicated.values())

    @classmethod
    def normalise_timezone_name(cls, value: str) -> str | None:
        """Validate a timezone input and return its preferred display/catalogue key."""
        raw = value.strip()
        parsed_timezone = cls.parse_timezone(raw)
        if parsed_timezone is None:
            return None
        if raw.upper() in {"UTC", "Z", "GMT"}:
            return "UTC"
        offset = parsed_timezone.utcoffset(None)
        if offset is not None:
            total_minutes = int(offset.total_seconds() // 60)
            return "UTC" if total_minutes == 0 else cls._timezone_offset_label(total_minutes)
        normalized_raw = raw.casefold()
        catalogue = cls._timezone_selection_catalog()
        exact_match = next((entry.value for entry in catalogue if entry.value.casefold() == normalized_raw), None)
        if exact_match is not None:
            return exact_match
        city_token = re.sub(r"[\s\-]+", "_", raw).casefold()
        city_match = next(
            (
                entry.value
                for entry in catalogue
                if entry.value.rsplit("/", maxsplit=1)[-1].casefold() == city_token
            ),
            None,
        )
        return city_match or raw

    @classmethod
    def _parse_clock_token(
        cls,
        token: str,
        *,
        default_tz: tzinfo,
        require_tz: bool = False,
    ) -> tuple[int, int, int, tzinfo] | None:
        m = cls._CLOCK_RE.fullmatch(token.strip())
        if not m:
            return None

        h = int(m.group("h"))
        mi = int(m.group("m"))
        sec = int(m.group("s") or "0")
        if h >= 24 or mi >= 60 or sec >= 60:
            return None

        tz_token = m.group("tz")
        if require_tz and not tz_token:
            return None

        if not tz_token:
            tz_info = default_tz
        elif tz_token.upper() == "Z":
            tz_info = timezone.utc
        else:
            tz_info = cls.parse_timezone(tz_token)
            if tz_info is None:
                return None

        return (h, mi, sec, tz_info)

    @classmethod
    def parse_absolute_time(cls, value: str, tz: tzinfo = timezone.utc) -> datetime | None:
        s = value.strip()
        if not s:
            return None

        # DD/MM/YY HH:MM[:SS][tz] or DD/MM/YYYY HH:MM[:SS][tz]
        dmy_dt = cls._DMY_DATETIME_RE.fullmatch(s)
        if dmy_dt:
            day = int(dmy_dt.group("d"))
            month = int(dmy_dt.group("m"))
            year_raw = dmy_dt.group("y")
            year = 2000 + int(year_raw) if len(year_raw) == 2 else int(year_raw)
            hour = int(dmy_dt.group("h"))
            minute = int(dmy_dt.group("mi"))
            second = int(dmy_dt.group("s") or "0")

            if hour >= 24 or minute >= 60 or second >= 60:
                return None

            tz_token = dmy_dt.group("tz")
            if not tz_token:
                tz_info = tz
            elif tz_token.upper() == "Z":
                tz_info = timezone.utc
            else:
                tz_info = cls.parse_timezone(tz_token)
                if tz_info is None:
                    return None

            try:
                return datetime(year, month, day, hour, minute, second, tzinfo=tz_info)
            except ValueError:
                return None

        # DD/MM/YY or DD/MM/YYYY (defaults to midnight in supplied tz)
        dmy = cls._DMY_DATE_RE.fullmatch(s)
        if dmy:
            day = int(dmy.group("d"))
            month = int(dmy.group("m"))
            year_raw = dmy.group("y")
            year = 2000 + int(year_raw) if len(year_raw) == 2 else int(year_raw)
            try:
                return datetime(year, month, day, 0, 0, 0, tzinfo=tz)
            except ValueError:
                return None

        parts = s.split()

        # [zone] [time] [date]
        if len(parts) == 3 and cls._ISO_DATE_RE.fullmatch(parts[2]):
            zone = cls.parse_timezone(parts[0])
            if zone:
                clock = cls._parse_clock_token(parts[1], default_tz=zone)
                if clock:
                    h, mi, sec, tz_info = clock
                    try:
                        date_part = datetime.fromisoformat(parts[2])
                    except ValueError:
                        return None
                    return datetime(
                        date_part.year,
                        date_part.month,
                        date_part.day,
                        h,
                        mi,
                        sec,
                        tzinfo=tz_info,
                    )

        # [time-with-tz] [date]
        if len(parts) == 2 and cls._ISO_DATE_RE.fullmatch(parts[1]):
            clock = cls._parse_clock_token(parts[0], default_tz=tz, require_tz=True)
            if clock:
                h, mi, sec, tz_info = clock
                try:
                    date_part = datetime.fromisoformat(parts[1])
                except ValueError:
                    return None
                return datetime(
                    date_part.year,
                    date_part.month,
                    date_part.day,
                    h,
                    mi,
                    sec,
                    tzinfo=tz_info,
                )

        # ISO datetime/date. If tz missing, apply the supplied default tz.
        iso = s
        if iso.endswith(("Z", "z")):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=tz)

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
        return (config.PUBLIC_UPLOADS_BASE_URL + quote(target.name), up_target)

    @staticmethod
    def is_awaitable(func: Callable[[], Any]) -> bool:
        try:
            result = func()
        except Exception:
            return False
        return inspect.isawaitable(result)


# AiviA APasz
