"""Shared parsing and resilient daily rate lookup for currency conversions."""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as element_tree
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from zoneinfo import ZoneInfo

import aiohttp

import config
from _authority import read_json_object, write_json_object

log = logging.getLogger(__name__)
_ECB_DAILY_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_FRANKFURTER_LATEST_RATES_URL = "https://api.frankfurter.dev/v1/latest"
_ECB_TIME_ZONE = ZoneInfo("Europe/Brussels")
_ECB_PUBLICATION_TIME = time(hour=16, minute=10)
_MAX_SNAPSHOT_AGE = timedelta(days=30)
_REFRESH_FAILURE_RETRY_INTERVAL = timedelta(hours=1)
_EMERGENCY_CACHE_INTERVAL = timedelta(hours=24)
_EMERGENCY_DAILY_CALL_LIMIT = 2


class CurrencyRateProvider(StrEnum):
    ECB = "ECB"
    FRANKFURTER = "Frankfurter"
    EXCHANGE_RATE_HOST = "exchangerate.host"


@dataclass(frozen=True, slots=True)
class CurrencyAmount:
    """A parsed currency amount and the optional expression used to calculate it."""

    amount: Decimal
    expression: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite() or self.amount < 0:
            raise ValueError("Currency amounts must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class CurrencyRateSnapshot:
    """One complete set of EUR-relative rates from a daily reference-rate source."""

    as_of: date
    fetched_at: datetime
    provider: CurrencyRateProvider
    euro_rates: Mapping[config.Currency, Decimal]

    def __post_init__(self) -> None:
        missing = set(config.SUPPORTED_CURRENCY).difference(self.euro_rates)
        if missing:
            missing_names = ", ".join(sorted(currency.name for currency in missing))
            raise ValueError(f"Currency rate snapshot is missing: {missing_names}")
        if self.euro_rates[config.Currency.EUR] != Decimal("1"):
            raise ValueError("Currency rate snapshots must define EUR as exactly 1.")
        for currency, rate in self.euro_rates.items():
            if not isinstance(currency, config.Currency):
                raise ValueError("Currency rate snapshots must use configured currency keys.")
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
                raise ValueError(f"Currency rate snapshot has an invalid {currency.name} rate.")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("Currency rate snapshot fetch times must include a time zone.")

    def convert(self, *, amount: Decimal, source: config.Currency, target: config.Currency) -> Decimal:
        return amount * self.euro_rates[target] / self.euro_rates[source]

    def age(self, *, now: datetime) -> timedelta:
        return max(now.date() - self.as_of, timedelta())

    def to_mapping(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "provider": self.provider.value,
            "euro_rates": {currency.name: str(rate) for currency, rate in self.euro_rates.items()},
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CurrencyRateSnapshot":
        raw_rates = value.get("euro_rates")
        if not isinstance(raw_rates, Mapping):
            raise ValueError("Currency rate snapshot has no rate mapping.")
        euro_rates: dict[config.Currency, Decimal] = {}
        for raw_currency, raw_rate in raw_rates.items():
            if not isinstance(raw_currency, str):
                raise ValueError("Currency rate snapshot contains a non-text currency code.")
            euro_rates[config.Currency[raw_currency]] = Decimal(str(raw_rate))
        raw_as_of = value.get("as_of")
        raw_fetched_at = value.get("fetched_at")
        raw_provider = value.get("provider")
        if not isinstance(raw_as_of, str) or not isinstance(raw_fetched_at, str) or not isinstance(raw_provider, str):
            raise ValueError("Currency rate snapshot has invalid metadata.")
        return cls(
            as_of=date.fromisoformat(raw_as_of),
            fetched_at=datetime.fromisoformat(raw_fetched_at),
            provider=CurrencyRateProvider(raw_provider),
            euro_rates=euro_rates,
        )


@dataclass(frozen=True, slots=True)
class CurrencyConversionResult:
    """A conversion result with the source metadata needed to present its freshness."""

    amount: Decimal
    provider: CurrencyRateProvider
    as_of: date | None
    age: timedelta | None

    @property
    def is_stale(self) -> bool:
        return self.age is not None and self.age >= timedelta(days=7)


@dataclass(frozen=True, slots=True)
class CurrencyConversionBatch:
    """All supported conversion amounts calculated from one daily rate snapshot."""

    amounts: Mapping[config.Currency, Decimal]
    provider: CurrencyRateProvider
    as_of: date
    age: timedelta

    @property
    def is_stale(self) -> bool:
        return self.age >= timedelta(days=7)


class CurrencyConverter:
    """Parse currency amounts and convert them from one persisted daily rate sheet."""

    _snapshot: CurrencyRateSnapshot | None = None
    _snapshot_loaded = False
    _snapshot_lock: asyncio.Lock | None = None
    _last_refresh_failure: datetime | None = None
    _emergency_cache: dict[tuple[config.Currency, config.Currency], tuple[CurrencyConversionResult, datetime]] = {}
    _emergency_calls: dict[date, int] = {}
    _PERCENT_EXPR = re.compile(
        r"""
        ^\s*
        (?P<base>[0-9][0-9.,]*)
        \s*(?P<op>[+-])\s*
        (?P<pct>[0-9][0-9.,]*)
        \s*%
        \s*$
        """,
        re.VERBOSE,
    )
    _SIMPLE_EXPR = re.compile(
        r"""
        ^\s*
        (?P<a>[0-9][0-9.,]*)
        \s*(?P<op>[+-])\s*
        (?P<b>[0-9][0-9.,]*)
        \s*$
        """,
        re.VERBOSE,
    )

    @classmethod
    async def convert(cls, amount: Decimal, src: config.Currency, dst: config.Currency) -> Decimal | None:
        """Convert an amount while retaining the existing command-facing return type."""
        conversion = await cls.convert_with_metadata(amount=amount, src=src, dst=dst)
        return None if conversion is None else conversion.amount

    @classmethod
    async def convert_with_metadata(
        cls, *, amount: Decimal, src: config.Currency, dst: config.Currency
    ) -> CurrencyConversionResult | None:
        """Convert an amount and return the rate source and age for display."""
        if src is dst:
            return CurrencyConversionResult(
                amount=amount,
                provider=CurrencyRateProvider.ECB,
                as_of=None,
                age=None,
            )
        now = datetime.now(UTC)
        snapshot = await cls._rate_snapshot(now=now)
        if snapshot is not None:
            return CurrencyConversionResult(
                amount=snapshot.convert(amount=amount, source=src, target=dst),
                provider=snapshot.provider,
                as_of=snapshot.as_of,
                age=snapshot.age(now=now),
            )
        return await cls._convert_with_exchangerate_host(amount=amount, src=src, dst=dst, now=now)

    @classmethod
    async def convert_all_with_metadata(
        cls, *, amount: Decimal, src: config.Currency
    ) -> CurrencyConversionBatch | None:
        """Calculate every supported target from one shared reference-rate snapshot."""
        now = datetime.now(UTC)
        snapshot = await cls._rate_snapshot(now=now)
        return None if snapshot is None else cls._batch_from_snapshot(snapshot=snapshot, amount=amount, src=src, now=now)

    @classmethod
    async def convert_all_with_ecb_metadata(
        cls, *, amount: Decimal, src: config.Currency
    ) -> CurrencyConversionBatch | None:
        """Calculate every supported target from the ECB reference-rate sheet only."""
        now = datetime.now(UTC)
        snapshot = await cls._ecb_rate_snapshot(now=now)
        return None if snapshot is None else cls._batch_from_snapshot(snapshot=snapshot, amount=amount, src=src, now=now)

    @classmethod
    def cached_conversion_batch(cls, *, amount: Decimal, src: config.Currency) -> CurrencyConversionBatch | None:
        """Return the persisted rate sheet without making a network request."""
        now = datetime.now(UTC)
        snapshot = cls._load_snapshot()
        if snapshot is None or snapshot.age(now=now) > _MAX_SNAPSHOT_AGE:
            return None
        return cls._batch_from_snapshot(snapshot=snapshot, amount=amount, src=src, now=now)

    @classmethod
    def cached_ecb_conversion_batch(cls, *, amount: Decimal, src: config.Currency) -> CurrencyConversionBatch | None:
        """Return any persisted ECB sheet without requesting a network refresh."""
        now = datetime.now(UTC)
        snapshot = cls._load_snapshot()
        if snapshot is None or snapshot.provider is not CurrencyRateProvider.ECB:
            return None
        return cls._batch_from_snapshot(snapshot=snapshot, amount=amount, src=src, now=now)

    @classmethod
    async def fetch_with_metadata(
        cls, *, amount: Decimal, src: config.Currency, dst: config.Currency
    ) -> CurrencyConversionResult | None:
        """Use the quota-limited emergency provider for an explicitly requested pair."""
        if src is dst:
            return CurrencyConversionResult(
                amount=amount,
                provider=CurrencyRateProvider.EXCHANGE_RATE_HOST,
                as_of=None,
                age=None,
            )
        return await cls._convert_with_exchangerate_host(amount=amount, src=src, dst=dst, now=datetime.now(UTC))

    @staticmethod
    def _batch_from_snapshot(
        *, snapshot: CurrencyRateSnapshot, amount: Decimal, src: config.Currency, now: datetime
    ) -> CurrencyConversionBatch:
        return CurrencyConversionBatch(
            amounts={
                target: snapshot.convert(amount=amount, source=src, target=target)
                for target in config.SUPPORTED_CURRENCY
            },
            provider=snapshot.provider,
            as_of=snapshot.as_of,
            age=snapshot.age(now=now),
        )

    @classmethod
    async def _rate_snapshot(cls, *, now: datetime) -> CurrencyRateSnapshot | None:
        snapshot = cls._load_snapshot()
        if not cls._should_refresh(snapshot=snapshot, now=now):
            return snapshot
        if (
            cls._last_refresh_failure is not None
            and cls._last_refresh_failure + _REFRESH_FAILURE_RETRY_INTERVAL > now
        ):
            return snapshot if snapshot is not None and snapshot.age(now=now) <= _MAX_SNAPSHOT_AGE else None
        lock = cls._snapshot_lock
        if lock is None:
            lock = asyncio.Lock()
            cls._snapshot_lock = lock
        async with lock:
            snapshot = cls._load_snapshot()
            if not cls._should_refresh(snapshot=snapshot, now=now):
                return snapshot
            refreshed_snapshot = await cls._fetch_snapshot(now=now)
            if refreshed_snapshot is not None:
                cls._last_refresh_failure = None
                cls._save_snapshot(refreshed_snapshot)
                return refreshed_snapshot
            cls._last_refresh_failure = now
            if snapshot is not None and snapshot.age(now=now) <= _MAX_SNAPSHOT_AGE:
                log.warning(
                    "Currency rate refresh failed; using %s rates from %s.",
                    snapshot.provider.value,
                    snapshot.as_of.isoformat(),
                )
                return snapshot
        return None

    @classmethod
    async def _ecb_rate_snapshot(cls, *, now: datetime) -> CurrencyRateSnapshot | None:
        snapshot = cls._load_snapshot()
        if snapshot is not None and snapshot.provider is CurrencyRateProvider.ECB and not cls._should_refresh(
            snapshot=snapshot, now=now
        ):
            return snapshot
        if (
            cls._last_refresh_failure is not None
            and cls._last_refresh_failure + _REFRESH_FAILURE_RETRY_INTERVAL > now
        ):
            return snapshot if snapshot is not None and snapshot.provider is CurrencyRateProvider.ECB else None
        lock = cls._snapshot_lock
        if lock is None:
            lock = asyncio.Lock()
            cls._snapshot_lock = lock
        async with lock:
            snapshot = cls._load_snapshot()
            if snapshot is not None and snapshot.provider is CurrencyRateProvider.ECB and not cls._should_refresh(
                snapshot=snapshot, now=now
            ):
                return snapshot
            try:
                async with aiohttp.ClientSession() as session:
                    refreshed_snapshot = await cls._fetch_ecb_snapshot(session=session, now=now)
            except (aiohttp.ClientError, InvalidOperation, ValueError, element_tree.ParseError) as xcp:
                cls._last_refresh_failure = now
                log.warning("ECB currency rate provider failed: %s", xcp)
                return snapshot if snapshot is not None and snapshot.provider is CurrencyRateProvider.ECB else None
            cls._last_refresh_failure = None
            cls._save_snapshot(refreshed_snapshot)
            return refreshed_snapshot

    @classmethod
    def _load_snapshot(cls) -> CurrencyRateSnapshot | None:
        if cls._snapshot_loaded:
            return cls._snapshot
        cls._snapshot_loaded = True
        try:
            cls._snapshot = CurrencyRateSnapshot.from_mapping(read_json_object(config.CURRENCY_RATE_SNAPSHOT))
        except FileNotFoundError:
            return None
        except (InvalidOperation, KeyError, OSError, TypeError, ValueError) as xcp:
            log.warning("Ignoring invalid currency rate snapshot %s: %s", config.CURRENCY_RATE_SNAPSHOT, xcp)
        return cls._snapshot

    @classmethod
    def _save_snapshot(cls, snapshot: CurrencyRateSnapshot) -> None:
        cls._snapshot = snapshot
        cls._snapshot_loaded = True
        try:
            write_json_object(config.CURRENCY_RATE_SNAPSHOT, snapshot.to_mapping())
        except OSError:
            log.exception("Could not persist currency rate snapshot to %s", config.CURRENCY_RATE_SNAPSHOT)

    @staticmethod
    def _latest_expected_rate_date(*, now: datetime) -> date:
        local_time = now.astimezone(_ECB_TIME_ZONE)
        expected = local_time.date()
        if local_time.timetz().replace(tzinfo=None) < _ECB_PUBLICATION_TIME:
            expected -= timedelta(days=1)
        while expected.weekday() >= 5:
            expected -= timedelta(days=1)
        return expected

    @classmethod
    def _should_refresh(cls, *, snapshot: CurrencyRateSnapshot | None, now: datetime) -> bool:
        if snapshot is None:
            return True
        expected_date = cls._latest_expected_rate_date(now=now)
        if snapshot.as_of >= expected_date:
            return False
        fetched_at = snapshot.fetched_at.astimezone(_ECB_TIME_ZONE)
        if fetched_at.date() > expected_date:
            return False
        if fetched_at.date() == expected_date and fetched_at.timetz().replace(tzinfo=None) >= _ECB_PUBLICATION_TIME:
            return False
        return True

    @classmethod
    async def _fetch_snapshot(cls, *, now: datetime) -> CurrencyRateSnapshot | None:
        async with aiohttp.ClientSession() as session:
            for provider, loader in (
                (CurrencyRateProvider.ECB, cls._fetch_ecb_snapshot),
                (CurrencyRateProvider.FRANKFURTER, cls._fetch_frankfurter_snapshot),
            ):
                try:
                    return await loader(session=session, now=now)
                except (aiohttp.ClientError, InvalidOperation, ValueError, element_tree.ParseError) as xcp:
                    log.warning("Currency rate provider %s failed: %s", provider.value, xcp)
        return None

    @classmethod
    async def _fetch_ecb_snapshot(cls, *, session: aiohttp.ClientSession, now: datetime) -> CurrencyRateSnapshot:
        async with session.get(_ECB_DAILY_RATES_URL) as response:
            response.raise_for_status()
            content = await response.text()
        root = element_tree.fromstring(content)
        as_of: date | None = None
        euro_rates: dict[config.Currency, Decimal] = {config.Currency.EUR: Decimal("1")}
        for element in root.iter():
            raw_date = element.attrib.get("time")
            if raw_date is not None:
                as_of = date.fromisoformat(raw_date)
            raw_currency = element.attrib.get("currency")
            raw_rate = element.attrib.get("rate")
            if raw_currency is None or raw_rate is None:
                continue
            currency = config.Currency.__members__.get(raw_currency)
            if currency is not None:
                euro_rates[currency] = Decimal(raw_rate)
        if as_of is None:
            raise ValueError("ECB rate feed did not include a reference date.")
        return CurrencyRateSnapshot(
            as_of=as_of,
            fetched_at=now,
            provider=CurrencyRateProvider.ECB,
            euro_rates=euro_rates,
        )

    @classmethod
    async def _fetch_frankfurter_snapshot(
        cls, *, session: aiohttp.ClientSession, now: datetime
    ) -> CurrencyRateSnapshot:
        symbols = ",".join(currency.name for currency in config.SUPPORTED_CURRENCY if currency is not config.Currency.EUR)
        async with session.get(_FRANKFURTER_LATEST_RATES_URL, params={"symbols": symbols}) as response:
            response.raise_for_status()
            data = await response.json()
        if not isinstance(data, dict):
            raise ValueError("Frankfurter rate feed did not return an object.")
        raw_as_of = data.get("date")
        raw_rates = data.get("rates")
        if not isinstance(raw_as_of, str) or not isinstance(raw_rates, dict):
            raise ValueError("Frankfurter rate feed has invalid data.")
        euro_rates: dict[config.Currency, Decimal] = {config.Currency.EUR: Decimal("1")}
        for raw_currency, raw_rate in raw_rates.items():
            if not isinstance(raw_currency, str):
                raise ValueError("Frankfurter rate feed contains an invalid currency code.")
            currency = config.Currency.__members__.get(raw_currency)
            if currency is not None:
                euro_rates[currency] = Decimal(str(raw_rate))
        return CurrencyRateSnapshot(
            as_of=date.fromisoformat(raw_as_of),
            fetched_at=now,
            provider=CurrencyRateProvider.FRANKFURTER,
            euro_rates=euro_rates,
        )

    @classmethod
    async def _convert_with_exchangerate_host(
        cls, *, amount: Decimal, src: config.Currency, dst: config.Currency, now: datetime
    ) -> CurrencyConversionResult | None:
        cache_key = (src, dst)
        cached = cls._emergency_cache.get(cache_key)
        if cached is not None:
            cached_result, cached_at = cached
            if cached_at + _EMERGENCY_CACHE_INTERVAL > now:
                return CurrencyConversionResult(
                    amount=amount * cached_result.amount,
                    provider=cached_result.provider,
                    as_of=cached_result.as_of,
                    age=cached_result.age,
                )
            del cls._emergency_cache[cache_key]
        if not config.EXR_TOK:
            return None
        if now.date() not in cls._emergency_calls:
            cls._emergency_calls.clear()
            cls._emergency_calls[now.date()] = 0
        calls_today = cls._emergency_calls[now.date()]
        if calls_today >= _EMERGENCY_DAILY_CALL_LIMIT:
            log.warning("Currency emergency-provider daily call limit reached.")
            return None
        cls._emergency_calls[now.date()] = calls_today + 1
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "amount": str(amount),
                    "from": src.name,
                    "to": dst.name,
                    "access_key": config.EXR_TOK,
                }
                async with session.get(config.EXCHANGE_RATE_ADDR, params=params) as response:
                    response.raise_for_status()
                    data = await response.json()
            if not isinstance(data, dict) or not data.get("success") or data.get("result") is None:
                return None
            raw_as_of = data.get("date")
            as_of = date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None
            result = CurrencyConversionResult(
                amount=Decimal(str(data["result"])),
                provider=CurrencyRateProvider.EXCHANGE_RATE_HOST,
                as_of=as_of,
                age=None if as_of is None else max(now.date() - as_of, timedelta()),
            )
            if amount != 0:
                cls._emergency_cache[cache_key] = (
                    CurrencyConversionResult(
                        amount=result.amount / amount,
                        provider=result.provider,
                        as_of=result.as_of,
                        age=result.age,
                    ),
                    now,
                )
            return result
        except (aiohttp.ClientError, InvalidOperation, ValueError):
            log.exception("Emergency currency conversion failed")
            return None

    @staticmethod
    def _parse_decimal(value: str) -> Decimal | None:
        digits = "".join(character for character in value if character.isdigit() or character in ".,")
        if not digits:
            return None
        if "." in digits and "," in digits:
            digits = digits.replace(",", "")
        elif "," in digits:
            digits = digits.replace(".", "").replace(",", ".")
        try:
            return Decimal(digits)
        except InvalidOperation:
            return None

    @classmethod
    def parse_amount(cls, value: str) -> CurrencyAmount | None:
        """Parse a numeric amount with optional addition, subtraction, or percentage adjustment."""
        expression = value.strip()
        if not expression or expression.startswith("-"):
            return None

        match = cls._PERCENT_EXPR.match(expression)
        if match is not None:
            base = cls._parse_decimal(match.group("base"))
            percentage = cls._parse_decimal(match.group("pct"))
            if base is None or percentage is None:
                return None
            multiplier = Decimal("1") + (percentage / Decimal("100")) * (
                Decimal("1") if match.group("op") == "+" else Decimal("-1")
            )
            amount = base * multiplier
            if amount < 0:
                return None
            return CurrencyAmount(
                amount=amount,
                expression=f"{match.group('base')}{match.group('op')}{match.group('pct')}%",
            )

        match = cls._SIMPLE_EXPR.match(expression)
        if match is not None:
            first = cls._parse_decimal(match.group("a"))
            second = cls._parse_decimal(match.group("b"))
            if first is None or second is None:
                return None
            amount = first + second if match.group("op") == "+" else first - second
            if amount < 0:
                return None
            return CurrencyAmount(
                amount=amount,
                expression=f"{match.group('a')}{match.group('op')}{match.group('b')}",
            )

        amount = cls._parse_decimal(expression.replace(" ", "").replace("_", ""))
        return None if amount is None or amount < 0 else CurrencyAmount(amount=amount, expression=None)
