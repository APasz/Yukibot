from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

import config


ETHANOL_DENSITY_GRAMS_PER_MILLILITRE = Decimal("0.789")
_EXPRESSION_TOKEN_PATTERN = re.compile(r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+))|(?P<operator>[+*x×]))")


@dataclass(frozen=True, slots=True)
class AlcoholAmountRange:
    """An inclusive alcohol amount interval, expressed in grams or drinks."""

    minimum: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        if self.minimum < 0 or self.maximum < self.minimum:
            raise ValueError("Alcohol amount bounds must be non-negative and ordered.")

    @property
    def is_exact(self) -> bool:
        return self.minimum == self.maximum

    @property
    def exact_value(self) -> Decimal:
        if not self.is_exact:
            raise ValueError("This standard-drink definition has a range, not one exact value.")
        return self.minimum


@dataclass(frozen=True, slots=True)
class StandardDrinkConversion:
    amount: Decimal
    from_unit: str
    to_unit: str
    pure_alcohol_grams: AlcoholAmountRange
    converted_amount: AlcoholAmountRange


@dataclass(frozen=True, slots=True)
class BeverageStandardDrinkEstimate:
    volume_millilitres: Decimal
    alcohol_by_volume_percent: Decimal
    pure_alcohol_grams: Decimal


@dataclass(frozen=True, slots=True)
class StandardDrinkEquivalent:
    unit: str
    definition: config.StandardDrinkDefinition
    amount: AlcoholAmountRange | None


def standard_drink_units(*, include_unavailable: bool = True, exact_only: bool = False) -> tuple[str, ...]:
    """Return configured national definitions in display order."""
    return tuple(
        definition.unit
        for definition in config.STANDARD_DRINK_DEFINITIONS
        if include_unavailable or definition.is_convertible
        if not exact_only or definition.has_exact_grams
    )


def standard_drink_definition(unit: str) -> config.StandardDrinkDefinition:
    try:
        return config.STANDARD_DRINK_DEFINITIONS_BY_UNIT[unit.upper()]
    except KeyError as xcp:
        supported_units = ", ".join(standard_drink_units())
        raise ValueError(f"Unknown standard-drink unit {unit!r}. Supported units: {supported_units}.") from xcp


def standard_drink_conversion(
    *, amount: Decimal | float | int, from_unit: str, to_unit: str
) -> StandardDrinkConversion:
    decimal_amount = _require_non_negative_decimal(amount, field="Standard-drink amount")
    source = _convertible_definition(from_unit)
    destination = _convertible_definition(to_unit)
    source_minimum, source_maximum = _definition_gram_bounds(source)
    destination_minimum, destination_maximum = _definition_gram_bounds(destination)
    pure_alcohol_grams = AlcoholAmountRange(
        minimum=decimal_amount * source_minimum,
        maximum=decimal_amount * source_maximum,
    )
    return StandardDrinkConversion(
        amount=decimal_amount,
        from_unit=source.unit,
        to_unit=destination.unit,
        pure_alcohol_grams=pure_alcohol_grams,
        converted_amount=AlcoholAmountRange(
            minimum=pure_alcohol_grams.minimum / destination_maximum,
            maximum=pure_alcohol_grams.maximum / destination_minimum,
        ),
    )


def beverage_standard_drink_estimate(
    *, volume_millilitres: Decimal | float | int, alcohol_by_volume_percent: Decimal | float | int
) -> BeverageStandardDrinkEstimate:
    volume = _require_non_negative_decimal(volume_millilitres, field="Volume")
    abv = _require_non_negative_decimal(alcohol_by_volume_percent, field="ABV")
    if abv > 100:
        raise ValueError("ABV cannot exceed 100%.")

    pure_alcohol_grams = volume * (abv / 100) * ETHANOL_DENSITY_GRAMS_PER_MILLILITRE
    return BeverageStandardDrinkEstimate(
        volume_millilitres=volume,
        alcohol_by_volume_percent=abv,
        pure_alcohol_grams=pure_alcohol_grams,
    )


def standard_drink_equivalents(
    *, pure_alcohol_grams: Decimal | float | int
) -> tuple[StandardDrinkEquivalent, ...]:
    pure_alcohol = _require_non_negative_decimal(pure_alcohol_grams, field="Pure alcohol")
    equivalents: list[StandardDrinkEquivalent] = []
    for definition in config.STANDARD_DRINK_DEFINITIONS:
        if not definition.is_convertible:
            equivalents.append(StandardDrinkEquivalent(unit=definition.unit, definition=definition, amount=None))
            continue
        minimum_grams, maximum_grams = _definition_gram_bounds(definition)
        equivalents.append(
            StandardDrinkEquivalent(
                unit=definition.unit,
                definition=definition,
                amount=AlcoholAmountRange(
                    minimum=pure_alcohol / maximum_grams,
                    maximum=pure_alcohol / minimum_grams,
                ),
            )
        )
    return tuple(equivalents)


def format_standard_drink_number(value: Decimal | float | int) -> str:
    decimal_value = _require_non_negative_decimal(value, field="Number")
    return f"{decimal_value:,.2f}".rstrip("0").rstrip(".")


def format_standard_drink_range(amount: AlcoholAmountRange | None) -> str:
    if amount is None:
        return "Not nationally defined"
    if amount.is_exact:
        return format_standard_drink_number(amount.minimum)
    return f"{format_standard_drink_number(amount.minimum)}–{format_standard_drink_number(amount.maximum)}"


def format_standard_drink_definition(definition: config.StandardDrinkDefinition) -> str:
    if not definition.is_convertible:
        return "Not nationally defined"
    minimum, maximum = _definition_gram_bounds(definition)
    return format_standard_drink_range(AlcoholAmountRange(minimum=minimum, maximum=maximum)) + " g"


def parse_standard_drink_expression(value: str, *, field: str) -> Decimal:
    """Parse a non-negative decimal expression containing only addition and multiplication."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text.")
    position = 0
    expects_number = True
    product: Decimal | None = None
    total = Decimal()
    while position < len(value):
        token = _EXPRESSION_TOKEN_PATTERN.match(value, position)
        if token is None:
            raise ValueError(f"{field} may only use numbers with +, *, or x.")
        position = token.end()
        number_text = token.group("number")
        operator = token.group("operator")
        if expects_number:
            if number_text is None:
                raise ValueError(f"{field} must start with a number.")
            number = Decimal(number_text)
            product = number if product is None else product * number
            expects_number = False
            continue
        if operator is None:
            raise ValueError(f"{field} must separate numbers with +, *, or x.")
        if operator == "+":
            if product is None:
                raise RuntimeError("Expression parser lost its current product.")
            total += product
            product = None
        expects_number = True
    if expects_number:
        raise ValueError(f"{field} cannot end with an operator.")
    if product is None:
        raise RuntimeError("Expression parser ended without a product.")
    return _require_non_negative_decimal(total + product, field=field)


def _convertible_definition(unit: str) -> config.StandardDrinkDefinition:
    definition = standard_drink_definition(unit)
    if not definition.is_convertible:
        raise ValueError(f"{definition.country.display_name} does not have a nationally defined standard drink.")
    return definition


def _definition_gram_bounds(definition: config.StandardDrinkDefinition) -> tuple[Decimal, Decimal]:
    if definition.minimum_grams is None or definition.maximum_grams is None:
        raise ValueError(f"{definition.country.display_name} does not have a standard-drink gram value.")
    return definition.minimum_grams, definition.maximum_grams


def _require_non_negative_decimal(value: Decimal | float | int, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, float, int)):
        raise TypeError(f"{field} must be a number.")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as xcp:
        raise ValueError(f"{field} must be finite.") from xcp
    if not decimal_value.is_finite():
        raise ValueError(f"{field} must be finite.")
    if decimal_value < 0:
        raise ValueError(f"{field} cannot be negative.")
    return decimal_value
