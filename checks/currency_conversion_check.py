from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

import aiohttp

import config
from currency_conversion import CurrencyConverter, CurrencyRateProvider, CurrencyRateSnapshot


def _rate_snapshot(*, as_of: date, fetched_at: datetime) -> CurrencyRateSnapshot:
    return CurrencyRateSnapshot(
        as_of=as_of,
        fetched_at=fetched_at,
        provider=CurrencyRateProvider.ECB,
        euro_rates={
            currency: Decimal("1") if currency is config.Currency.EUR else Decimal("2")
            for currency in config.SUPPORTED_CURRENCY
        },
    )


def _reset_currency_converter() -> None:
    CurrencyConverter._snapshot = None
    CurrencyConverter._snapshot_loaded = False
    CurrencyConverter._snapshot_lock = None
    CurrencyConverter._last_refresh_failure = None
    CurrencyConverter._emergency_cache.clear()
    CurrencyConverter._emergency_calls.clear()


def test_parse_currency_amount_supports_arithmetic_and_percentages() -> None:
    percent_amount = CurrencyConverter.parse_amount("10+20%")
    subtraction_amount = CurrencyConverter.parse_amount("10 - 3")
    plain_amount = CurrencyConverter.parse_amount("1,234.50")

    assert percent_amount is not None
    assert percent_amount.amount == Decimal("12.0")
    assert percent_amount.expression == "10+20%"
    assert subtraction_amount is not None
    assert subtraction_amount.amount == Decimal("7")
    assert subtraction_amount.expression == "10-3"
    assert plain_amount is not None
    assert plain_amount.amount == Decimal("1234.50")
    assert plain_amount.expression is None


def test_parse_currency_amount_rejects_negative_results() -> None:
    assert CurrencyConverter.parse_amount("-5") is None
    assert CurrencyConverter.parse_amount("5 - 10") is None
    assert CurrencyConverter.parse_amount("10 - 200%") is None


def test_rate_snapshot_rejects_invalid_rates() -> None:
    now = datetime(2026, 7, 24, 17, tzinfo=UTC)
    rates = {
        currency: Decimal("1") if currency is config.Currency.EUR else Decimal("2")
        for currency in config.SUPPORTED_CURRENCY
    }
    rates[config.Currency.AUD] = Decimal("0")

    try:
        CurrencyRateSnapshot(
            as_of=now.date(),
            fetched_at=now,
            provider=CurrencyRateProvider.ECB,
            euro_rates=rates,
        )
    except ValueError as xcp:
        assert "invalid AUD rate" in str(xcp)
    else:
        raise AssertionError("Zero currency rates must be rejected.")


def test_rate_snapshot_persists_complete_euro_rates_and_converts_cross_rates() -> None:
    now = datetime(2026, 7, 24, 17, tzinfo=UTC)
    snapshot = _rate_snapshot(as_of=now.date(), fetched_at=now)
    with TemporaryDirectory() as directory:
        snapshot_path = Path(directory) / "currency_rates.json"
        with patch.object(config, "CURRENCY_RATE_SNAPSHOT", snapshot_path):
            _reset_currency_converter()
            CurrencyConverter._save_snapshot(snapshot)
            _reset_currency_converter()
            loaded_snapshot = CurrencyConverter._load_snapshot()

    assert loaded_snapshot == snapshot
    assert loaded_snapshot is not None
    assert loaded_snapshot.convert(
        amount=Decimal("12"), source=config.Currency.AUD, target=config.Currency.USD
    ) == Decimal("12")


def test_currency_batch_reuses_one_snapshot_for_every_supported_target() -> None:
    now = datetime.now(UTC)
    _reset_currency_converter()
    CurrencyConverter._snapshot = _rate_snapshot(as_of=now.date(), fetched_at=now)
    CurrencyConverter._snapshot_loaded = True

    conversion_batch = asyncio.run(
        CurrencyConverter.convert_all_with_ecb_metadata(amount=Decimal("12"), src=config.Currency.AUD)
    )

    assert conversion_batch is not None
    assert conversion_batch.amounts[config.Currency.AUD] == Decimal("12")
    assert conversion_batch.amounts[config.Currency.USD] == Decimal("12")
    assert set(conversion_batch.amounts) == set(config.SUPPORTED_CURRENCY)
    _reset_currency_converter()


def test_rate_snapshot_refreshes_after_the_next_ecb_publication_and_skips_weekends() -> None:
    previous_rate_snapshot = _rate_snapshot(
        as_of=date(2026, 7, 24),
        fetched_at=datetime(2026, 7, 27, 13, tzinfo=UTC),
    )

    before_publication = datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
    after_publication = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)

    assert not CurrencyConverter._should_refresh(snapshot=previous_rate_snapshot, now=before_publication)
    assert CurrencyConverter._should_refresh(snapshot=previous_rate_snapshot, now=after_publication)


def test_ecb_xml_parser_builds_a_complete_snapshot() -> None:
    now = datetime(2026, 7, 24, 17, tzinfo=UTC)
    rates = "".join(
        f'<Cube currency="{currency.name}" rate="2" />'
        for currency in config.SUPPORTED_CURRENCY
        if currency is not config.Currency.EUR
    )
    response_xml = f'<Envelope><Cube time="2026-07-24">{rates}</Cube></Envelope>'

    class FakeResponse:
        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object | None,
        ) -> bool:
            del exc_type, exc, traceback
            return False

        def raise_for_status(self) -> None:
            return None

        async def text(self) -> str:
            return response_xml

    class FakeSession:
        def get(self, url: str) -> FakeResponse:
            assert url.startswith("https://www.ecb.europa.eu/")
            return FakeResponse()

    snapshot = asyncio.run(
        CurrencyConverter._fetch_ecb_snapshot(session=cast(aiohttp.ClientSession, cast(object, FakeSession())), now=now)
    )

    assert snapshot.as_of == date(2026, 7, 24)
    assert snapshot.provider is CurrencyRateProvider.ECB
    assert snapshot.euro_rates[config.Currency.EUR] == Decimal("1")
    assert snapshot.euro_rates[config.Currency.AUD] == Decimal("2")
