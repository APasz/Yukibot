from __future__ import annotations

import unittest
from decimal import Decimal

import config
from standard_drinks import (
    beverage_standard_drink_estimate,
    format_standard_drink_number,
    format_standard_drink_range,
    parse_standard_drink_expression,
    standard_drink_conversion,
    standard_drink_definition,
    standard_drink_equivalents,
    standard_drink_units,
)


class StandardDrinksCheck(unittest.TestCase):
    def test_standard_drink_conversion_uses_configured_gram_definitions(self) -> None:
        conversion = standard_drink_conversion(amount=1.0, from_unit="AU", to_unit="US")

        self.assertEqual(conversion.pure_alcohol_grams.exact_value, Decimal("10"))
        self.assertEqual(conversion.converted_amount.exact_value, Decimal("10") / Decimal("14"))

    def test_beverage_estimate_includes_each_configured_standard(self) -> None:
        estimate = beverage_standard_drink_estimate(volume_millilitres=375.0, alcohol_by_volume_percent=4.8)
        equivalents = standard_drink_equivalents(pure_alcohol_grams=estimate.pure_alcohol_grams)

        self.assertEqual(estimate.pure_alcohol_grams, Decimal("14.20200"))
        self.assertEqual(tuple(equivalent.unit for equivalent in equivalents), standard_drink_units())
        first_amount = equivalents[0].amount
        self.assertIsNotNone(first_amount)
        assert first_amount is not None
        self.assertEqual(first_amount.exact_value, Decimal("1.42020"))
        self.assertEqual(format_standard_drink_number(Decimal("1.4202")), "1.42")

    def test_requested_countries_have_standard_drink_and_currency_support(self) -> None:
        requested_countries = {
            config.Country.BELGIUM,
            config.Country.DENMARK,
            config.Country.FRANCE,
            config.Country.GERMANY,
            config.Country.IRELAND,
            config.Country.JAPAN,
            config.Country.NETHERLANDS,
            config.Country.NORWAY,
            config.Country.SWEDEN,
        }
        expected_currencies_by_country = {
            config.Country.BELGIUM: config.Currency.EUR,
            config.Country.DENMARK: config.Currency.DKK,
            config.Country.FRANCE: config.Currency.EUR,
            config.Country.GERMANY: config.Currency.EUR,
            config.Country.IRELAND: config.Currency.EUR,
            config.Country.JAPAN: config.Currency.JPY,
            config.Country.NETHERLANDS: config.Currency.EUR,
            config.Country.NORWAY: config.Currency.NOK,
            config.Country.SWEDEN: config.Currency.SEK,
        }

        definitions_by_country = {
            definition.country: definition for definition in config.STANDARD_DRINK_DEFINITIONS
        }
        for country in requested_countries:
            definition = definitions_by_country[country]
            self.assertTrue(definition.source_url.startswith("https://"))
            self.assertTrue(definition.source_name)
        self.assertEqual(
            {country: config.CURRENCY_COUNTRIES[country] for country in requested_countries},
            expected_currencies_by_country,
        )
        for currency in set(expected_currencies_by_country.values()):
            self.assertIs(config.CURRENCY_MAP[currency.name], currency)
        self.assertTrue(requested_countries.issubset(config.supported_conversion_countries()))

    def test_range_and_unavailable_definitions_do_not_claim_false_precision(self) -> None:
        swiss_definition = standard_drink_definition("CH")
        belgian_definition = standard_drink_definition("BE")

        self.assertIs(swiss_definition.kind, config.StandardDrinkDefinitionKind.RANGE)
        self.assertEqual(format_standard_drink_range(standard_drink_conversion(amount=1, from_unit="CH", to_unit="US").converted_amount), "0.71–0.86")
        self.assertIs(belgian_definition.kind, config.StandardDrinkDefinitionKind.UNAVAILABLE)
        with self.assertRaisesRegex(ValueError, "does not have a nationally defined"):
            standard_drink_conversion(amount=1, from_unit="BE", to_unit="US")

    def test_legacy_uk_unit_remains_compatible(self) -> None:
        definition = standard_drink_definition("uk")

        self.assertEqual(definition.unit, "UK")
        self.assertEqual(definition.country, config.Country.UNITED_KINGDOM)
        self.assertIn("UK", standard_drink_units())
        self.assertEqual(config.Country.UNITED_KINGDOM.value, "GB")
        self.assertIs(config.Country("UK"), config.Country.UNITED_KINGDOM)

    def test_country_beverage_defaults_match_configured_local_profiles(self) -> None:
        expected_profiles = {
            config.Country.AUSTRALIA: ("375", "4.9"),
            config.Country.UNITED_KINGDOM: ("568", "4.0"),
            config.Country.SWITZERLAND: ("330", "4.8"),
            config.Country.FINLAND: ("330", "5.5"),
            config.Country.HUNGARY: ("40", "40"),
            config.Country.UNITED_STATES: ("355", "5.0"),
            config.Country.DENMARK: ("330", "4.6"),
            config.Country.FRANCE: ("160", "5.6"),
            config.Country.GERMANY: ("500", "4.9"),
            config.Country.IRELAND: ("568", "4.2"),
            config.Country.JAPAN: ("350", "5.0"),
            config.Country.NETHERLANDS: ("330", "5.0"),
            config.Country.NORWAY: ("40", "41.5"),
            config.Country.SWEDEN: ("40", "40"),
        }

        for country, (volume, abv) in expected_profiles.items():
            profile = config.country_beverage_default(country)
            self.assertEqual(profile.volume_millilitres, Decimal(volume))
            self.assertEqual(profile.alcohol_by_volume_percent, Decimal(abv))

    def test_expression_parser_accepts_addition_and_multiplication(self) -> None:
        self.assertEqual(parse_standard_drink_expression("1 + 2x3", field="Amount"), Decimal("7"))
        self.assertEqual(parse_standard_drink_expression("0.5 * 330", field="Volume"), Decimal("165"))
        with self.assertRaisesRegex(ValueError, "only use numbers"):
            parse_standard_drink_expression("1 / 2", field="Amount")

    def test_rejects_invalid_calculator_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            standard_drink_conversion(amount=-1.0, from_unit="AU", to_unit="US")
        with self.assertRaisesRegex(ValueError, "cannot exceed 100"):
            beverage_standard_drink_estimate(volume_millilitres=375.0, alcohol_by_volume_percent=101.0)
