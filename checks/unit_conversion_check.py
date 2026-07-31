from __future__ import annotations

from decimal import Decimal

import pytest

from unit_conversion import (
    UnitCategory,
    convert_unit_amount,
    display_units_for_category,
    format_unit_amount,
    parse_unit_amount,
    unit_categories,
    unit_definition,
    units_for_category,
)


def _unit(category: UnitCategory, code: str):
    return unit_definition(category=category, code=code)


def test_catalogue_includes_a_populated_set_of_metric_and_customary_categories() -> None:
    assert UnitCategory.LENGTH in unit_categories()
    assert UnitCategory.VOLUME in unit_categories()
    assert UnitCategory.ENERGY in unit_categories()
    assert {unit.code for unit in units_for_category(UnitCategory.VOLUME)} >= {
        "l",
        "us_gal",
        "uk_gal",
    }


def test_display_units_are_grouped_by_system_and_alphabetised_within_each_group() -> None:
    units = display_units_for_category(UnitCategory.LENGTH)

    assert [(unit.system.display_name, unit.name) for unit in units[:6]] == [
        ("SI", "Centimetre"),
        ("SI", "Kilometre"),
        ("SI", "Metre"),
        ("SI", "Micrometre"),
        ("SI", "Millimetre"),
        ("SI", "Nanometre"),
    ]
    assert [(unit.system.display_name, unit.name) for unit in units[6:10]] == [
        ("US customary", "Foot"),
        ("US customary", "Inch"),
        ("US customary", "Mile"),
        ("US customary", "Yard"),
    ]


@pytest.mark.parametrize(
    ("category", "amount", "source_code", "target_code", "expected"),
    (
        (UnitCategory.LENGTH, Decimal("1"), "mi", "km", Decimal("1.609344")),
        (UnitCategory.VOLUME, Decimal("1"), "us_gal", "l", Decimal("3.785411784")),
        (UnitCategory.VOLUME, Decimal("1"), "uk_gal", "l", Decimal("4.54609")),
        (UnitCategory.MASS, Decimal("1"), "lb", "g", Decimal("453.59237")),
        (UnitCategory.ENERGY, Decimal("1"), "kwh", "mj", Decimal("3.6")),
        (UnitCategory.DATA, Decimal("1"), "gib", "gb", Decimal("1.073741824")),
    ),
)
def test_linear_conversions_preserve_defined_unit_values(
    category: UnitCategory,
    amount: Decimal,
    source_code: str,
    target_code: str,
    expected: Decimal,
) -> None:
    conversion = convert_unit_amount(
        amount=amount,
        source=_unit(category, source_code),
        target=_unit(category, target_code),
    )

    assert conversion.converted_amount == expected


def test_temperature_conversion_supports_negative_values_and_affine_units() -> None:
    conversion = convert_unit_amount(
        amount=Decimal("-40"),
        source=_unit(UnitCategory.TEMPERATURE, "f"),
        target=_unit(UnitCategory.TEMPERATURE, "c"),
    )

    assert conversion.converted_amount == Decimal("-40.00")


def test_angle_conversion_uses_one_shared_pi_definition() -> None:
    conversion = convert_unit_amount(
        amount=Decimal("1"),
        source=_unit(UnitCategory.ANGLE, "turn"),
        target=_unit(UnitCategory.ANGLE, "deg"),
    )

    assert conversion.converted_amount == Decimal("360")


def test_incompatible_units_are_rejected() -> None:
    with pytest.raises(ValueError, match="same category"):
        convert_unit_amount(
            amount=Decimal("1"),
            source=_unit(UnitCategory.LENGTH, "m"),
            target=_unit(UnitCategory.MASS, "g"),
        )


def test_amount_parsing_and_formatting_are_finite_and_readable() -> None:
    assert parse_unit_amount("-1,024.50") == Decimal("-1024.50")
    assert format_unit_amount(Decimal("1234.56789")) == "1,234.5679"
    assert format_unit_amount(Decimal("1000")) == "1,000"
    assert format_unit_amount(Decimal("0.0000000001234")) == "1.234e-10"
    with pytest.raises(ValueError, match="finite"):
        parse_unit_amount("NaN")
