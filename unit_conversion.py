"""Typed, local conversions for commonly used measurement units."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final


class UnitCategory(StrEnum):
    """A group of units that share one physical dimension."""

    LENGTH = "length"
    AREA = "area"
    VOLUME = "volume"
    MASS = "mass"
    TEMPERATURE = "temperature"
    SPEED = "speed"
    TIME = "time"
    PRESSURE = "pressure"
    ENERGY = "energy"
    POWER = "power"
    DATA = "data"
    ANGLE = "angle"
    FREQUENCY = "frequency"

    @property
    def display_name(self) -> str:
        return _UNIT_CATEGORY_DISPLAY_NAMES[self]


class UnitSystem(StrEnum):
    """The convention a unit belongs to, for concise user-facing labels."""

    SI = "si"
    METRIC = "metric"
    US_CUSTOMARY = "us_customary"
    IMPERIAL = "imperial"
    US_SURVEY = "us_survey"
    INTERNATIONAL = "international"
    ASTRONOMICAL = "astronomical"
    SCIENTIFIC = "scientific"
    COMPUTING = "computing"
    COMMON = "common"
    TROY = "troy"

    @property
    def display_name(self) -> str:
        return _UNIT_SYSTEM_DISPLAY_NAMES[self]


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    """A unit expressed as an affine transformation to its category base unit.

    Base values are calculated as ``(value + value_offset) * numerator /
    denominator + base_offset``. Integer ratios preserve exact definitions such
    as 12 inches in a foot without requiring binary floating point.
    """

    category: UnitCategory
    code: str
    name: str
    symbol: str
    system: UnitSystem
    numerator: int
    denominator: int = 1
    value_offset: Decimal = Decimal()
    base_offset: Decimal = Decimal()

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("Unit codes cannot be empty.")
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError(f"{self.name} must have a positive conversion ratio.")
        if not self.value_offset.is_finite() or not self.base_offset.is_finite():
            raise ValueError(f"{self.name} must have finite conversion offsets.")

    def to_base(self, value: Decimal) -> Decimal:
        """Convert a finite value to this category's base unit."""
        _require_finite_decimal(value, field="Value")
        return ((value + self.value_offset) * self.numerator / self.denominator) + self.base_offset

    def from_base(self, base_value: Decimal) -> Decimal:
        """Convert a finite base-unit value to this unit."""
        _require_finite_decimal(base_value, field="Base value")
        return ((base_value - self.base_offset) * self.denominator / self.numerator) - self.value_offset

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.symbol})"


@dataclass(frozen=True, slots=True)
class UnitConversion:
    """A conversion between two compatible unit definitions."""

    amount: Decimal
    source: UnitDefinition
    target: UnitDefinition
    converted_amount: Decimal

    def __post_init__(self) -> None:
        _require_finite_decimal(self.amount, field="Amount")
        _require_finite_decimal(self.converted_amount, field="Converted amount")
        if self.source.category is not self.target.category:
            raise ValueError("Units must belong to the same category.")


_UNIT_CATEGORY_DISPLAY_NAMES: Final[dict[UnitCategory, str]] = {
    UnitCategory.LENGTH: "Length",
    UnitCategory.AREA: "Area",
    UnitCategory.VOLUME: "Volume",
    UnitCategory.MASS: "Mass",
    UnitCategory.TEMPERATURE: "Temperature",
    UnitCategory.SPEED: "Speed",
    UnitCategory.TIME: "Time",
    UnitCategory.PRESSURE: "Pressure",
    UnitCategory.ENERGY: "Energy",
    UnitCategory.POWER: "Power",
    UnitCategory.DATA: "Data",
    UnitCategory.ANGLE: "Angle",
    UnitCategory.FREQUENCY: "Frequency",
}
_UNIT_SYSTEM_DISPLAY_NAMES: Final[dict[UnitSystem, str]] = {
    UnitSystem.SI: "SI",
    UnitSystem.METRIC: "Metric",
    UnitSystem.US_CUSTOMARY: "US customary",
    UnitSystem.IMPERIAL: "UK imperial",
    UnitSystem.US_SURVEY: "US survey",
    UnitSystem.INTERNATIONAL: "International",
    UnitSystem.ASTRONOMICAL: "Astronomical",
    UnitSystem.SCIENTIFIC: "Scientific",
    UnitSystem.COMPUTING: "Computing",
    UnitSystem.COMMON: "Common",
    UnitSystem.TROY: "Troy",
}


def _unit(
    category: UnitCategory,
    code: str,
    name: str,
    symbol: str,
    system: UnitSystem,
    numerator: int,
    denominator: int = 1,
    *,
    value_offset: str = "0",
    base_offset: str = "0",
) -> UnitDefinition:
    return UnitDefinition(
        category=category,
        code=code,
        name=name,
        symbol=symbol,
        system=system,
        numerator=numerator,
        denominator=denominator,
        value_offset=Decimal(value_offset),
        base_offset=Decimal(base_offset),
    )


def _decimal_unit(
    category: UnitCategory,
    code: str,
    name: str,
    symbol: str,
    system: UnitSystem,
    scale: str,
    *,
    value_offset: str = "0",
    base_offset: str = "0",
) -> UnitDefinition:
    """Build a unit from a human-readable, exact decimal scale."""
    scale_decimal = Decimal(scale)
    if not scale_decimal.is_finite() or scale_decimal <= 0:
        raise ValueError(f"{name} must have a positive finite conversion scale.")
    sign, digits, exponent = scale_decimal.as_tuple()
    if sign:
        raise ValueError(f"{name} must have a positive conversion scale.")
    if not isinstance(exponent, int):
        raise ValueError(f"{name} must have a finite conversion scale.")
    numerator = int("".join(str(digit) for digit in digits))
    denominator = 1
    if exponent < 0:
        denominator = 10 ** -exponent
    elif exponent > 0:
        numerator *= 10**exponent
    return _unit(
        category,
        code,
        name,
        symbol,
        system,
        numerator,
        denominator,
        value_offset=value_offset,
        base_offset=base_offset,
    )


_PI_NUMERATOR: Final[int] = 314_159_265_358_979_323_846
_PI_DENOMINATOR: Final[int] = 10**20


# Each category is ordered from small/common SI units through customary and
# specialist units, which is also the order used by the dashboard table.
UNIT_DEFINITIONS: Final[tuple[UnitDefinition, ...]] = (
    # Length: metre
    _unit(UnitCategory.LENGTH, "nm", "Nanometre", "nm", UnitSystem.SI, 1, 1_000_000_000),
    _unit(UnitCategory.LENGTH, "um", "Micrometre", "µm", UnitSystem.SI, 1, 1_000_000),
    _unit(UnitCategory.LENGTH, "mm", "Millimetre", "mm", UnitSystem.SI, 1, 1_000),
    _unit(UnitCategory.LENGTH, "cm", "Centimetre", "cm", UnitSystem.SI, 1, 100),
    _unit(UnitCategory.LENGTH, "m", "Metre", "m", UnitSystem.SI, 1),
    _unit(UnitCategory.LENGTH, "km", "Kilometre", "km", UnitSystem.SI, 1_000),
    _decimal_unit(UnitCategory.LENGTH, "in", "Inch", "in", UnitSystem.US_CUSTOMARY, "0.0254"),
    _decimal_unit(UnitCategory.LENGTH, "ft", "Foot", "ft", UnitSystem.US_CUSTOMARY, "0.3048"),
    _unit(UnitCategory.LENGTH, "us_survey_ft", "US survey foot", "ft (US)", UnitSystem.US_SURVEY, 1_200, 3_937),
    _decimal_unit(UnitCategory.LENGTH, "yd", "Yard", "yd", UnitSystem.US_CUSTOMARY, "0.9144"),
    _decimal_unit(UnitCategory.LENGTH, "mi", "Mile", "mi", UnitSystem.US_CUSTOMARY, "1609.344"),
    _unit(UnitCategory.LENGTH, "nmi", "Nautical mile", "nmi", UnitSystem.INTERNATIONAL, 1_852),
    _unit(UnitCategory.LENGTH, "au", "Astronomical unit", "au", UnitSystem.ASTRONOMICAL, 149_597_870_700),
    _unit(UnitCategory.LENGTH, "ly", "Light-year", "ly", UnitSystem.ASTRONOMICAL, 9_460_730_472_580_800),
    _unit(
        UnitCategory.LENGTH,
        "pc",
        "Parsec",
        "pc",
        UnitSystem.ASTRONOMICAL,
        30_856_775_814_913_673,
        1,
    ),
    # Area: square metre
    _unit(UnitCategory.AREA, "mm2", "Square millimetre", "mm²", UnitSystem.SI, 1, 1_000_000),
    _unit(UnitCategory.AREA, "cm2", "Square centimetre", "cm²", UnitSystem.SI, 1, 10_000),
    _unit(UnitCategory.AREA, "m2", "Square metre", "m²", UnitSystem.SI, 1),
    _unit(UnitCategory.AREA, "ha", "Hectare", "ha", UnitSystem.METRIC, 10_000),
    _unit(UnitCategory.AREA, "km2", "Square kilometre", "km²", UnitSystem.SI, 1_000_000),
    _decimal_unit(UnitCategory.AREA, "in2", "Square inch", "in²", UnitSystem.US_CUSTOMARY, "0.00064516"),
    _decimal_unit(UnitCategory.AREA, "ft2", "Square foot", "ft²", UnitSystem.US_CUSTOMARY, "0.09290304"),
    _decimal_unit(UnitCategory.AREA, "yd2", "Square yard", "yd²", UnitSystem.US_CUSTOMARY, "0.83612736"),
    _decimal_unit(UnitCategory.AREA, "acre", "Acre", "ac", UnitSystem.US_CUSTOMARY, "4046.8564224"),
    _decimal_unit(UnitCategory.AREA, "mi2", "Square mile", "mi²", UnitSystem.US_CUSTOMARY, "2589988.110336"),
    # Volume: litre
    _unit(UnitCategory.VOLUME, "ml", "Millilitre", "mL", UnitSystem.SI, 1, 1_000),
    _unit(UnitCategory.VOLUME, "l", "Litre", "L", UnitSystem.SI, 1),
    _unit(UnitCategory.VOLUME, "m3", "Cubic metre", "m³", UnitSystem.SI, 1_000),
    _decimal_unit(UnitCategory.VOLUME, "us_tsp", "US teaspoon", "tsp", UnitSystem.US_CUSTOMARY, "0.00492892159375"),
    _decimal_unit(UnitCategory.VOLUME, "us_tbsp", "US tablespoon", "tbsp", UnitSystem.US_CUSTOMARY, "0.01478676478125"),
    _decimal_unit(UnitCategory.VOLUME, "us_floz", "US fluid ounce", "fl oz", UnitSystem.US_CUSTOMARY, "0.0295735295625"),
    _decimal_unit(UnitCategory.VOLUME, "us_cup", "US cup", "cup", UnitSystem.US_CUSTOMARY, "0.2365882365"),
    _decimal_unit(UnitCategory.VOLUME, "us_pt", "US pint", "pt", UnitSystem.US_CUSTOMARY, "0.473176473"),
    _decimal_unit(UnitCategory.VOLUME, "us_qt", "US quart", "qt", UnitSystem.US_CUSTOMARY, "0.946352946"),
    _decimal_unit(UnitCategory.VOLUME, "us_gal", "US gallon", "gal", UnitSystem.US_CUSTOMARY, "3.785411784"),
    _decimal_unit(UnitCategory.VOLUME, "uk_floz", "UK fluid ounce", "fl oz", UnitSystem.IMPERIAL, "0.0284130625"),
    _decimal_unit(UnitCategory.VOLUME, "uk_pt", "UK pint", "pt", UnitSystem.IMPERIAL, "0.56826125"),
    _decimal_unit(UnitCategory.VOLUME, "uk_qt", "UK quart", "qt", UnitSystem.IMPERIAL, "1.1365225"),
    _decimal_unit(UnitCategory.VOLUME, "uk_gal", "UK gallon", "gal", UnitSystem.IMPERIAL, "4.54609"),
    # Mass: gram
    _unit(UnitCategory.MASS, "ug", "Microgram", "µg", UnitSystem.SI, 1, 1_000_000),
    _unit(UnitCategory.MASS, "mg", "Milligram", "mg", UnitSystem.SI, 1, 1_000),
    _unit(UnitCategory.MASS, "g", "Gram", "g", UnitSystem.SI, 1),
    _unit(UnitCategory.MASS, "kg", "Kilogram", "kg", UnitSystem.SI, 1_000),
    _unit(UnitCategory.MASS, "t", "Metric tonne", "t", UnitSystem.METRIC, 1_000_000),
    _decimal_unit(UnitCategory.MASS, "gr", "Grain", "gr", UnitSystem.US_CUSTOMARY, "0.06479891"),
    _decimal_unit(UnitCategory.MASS, "oz", "Ounce", "oz", UnitSystem.US_CUSTOMARY, "28.349523125"),
    _decimal_unit(UnitCategory.MASS, "lb", "Pound", "lb", UnitSystem.US_CUSTOMARY, "453.59237"),
    _decimal_unit(UnitCategory.MASS, "st", "Stone", "st", UnitSystem.IMPERIAL, "6350.29318"),
    _decimal_unit(UnitCategory.MASS, "short_ton", "US short ton", "short ton", UnitSystem.US_CUSTOMARY, "907184.74"),
    _decimal_unit(UnitCategory.MASS, "long_ton", "UK long ton", "long ton", UnitSystem.IMPERIAL, "1016046.9088"),
    _decimal_unit(UnitCategory.MASS, "troy_oz", "Troy ounce", "ozt", UnitSystem.TROY, "31.1034768"),
    # Temperature: kelvin
    _unit(UnitCategory.TEMPERATURE, "k", "Kelvin", "K", UnitSystem.SI, 1),
    _unit(UnitCategory.TEMPERATURE, "c", "Celsius", "°C", UnitSystem.METRIC, 1, base_offset="273.15"),
    _unit(
        UnitCategory.TEMPERATURE,
        "f",
        "Fahrenheit",
        "°F",
        UnitSystem.US_CUSTOMARY,
        5,
        9,
        value_offset="-32",
        base_offset="273.15",
    ),
    _unit(UnitCategory.TEMPERATURE, "r", "Rankine", "°R", UnitSystem.US_CUSTOMARY, 5, 9),
    # Speed: metre per second
    _unit(UnitCategory.SPEED, "m_s", "Metres per second", "m/s", UnitSystem.SI, 1),
    _unit(UnitCategory.SPEED, "km_h", "Kilometres per hour", "km/h", UnitSystem.METRIC, 5, 18),
    _unit(UnitCategory.SPEED, "ft_s", "Feet per second", "ft/s", UnitSystem.US_CUSTOMARY, 381, 1_250),
    _unit(UnitCategory.SPEED, "mph", "Miles per hour", "mph", UnitSystem.US_CUSTOMARY, 1_397, 3_125),
    _unit(UnitCategory.SPEED, "knot", "Knot", "kn", UnitSystem.INTERNATIONAL, 463, 900),
    # Time: second
    _unit(UnitCategory.TIME, "ns", "Nanosecond", "ns", UnitSystem.SI, 1, 1_000_000_000),
    _unit(UnitCategory.TIME, "us", "Microsecond", "µs", UnitSystem.SI, 1, 1_000_000),
    _unit(UnitCategory.TIME, "ms", "Millisecond", "ms", UnitSystem.SI, 1, 1_000),
    _unit(UnitCategory.TIME, "s", "Second", "s", UnitSystem.SI, 1),
    _unit(UnitCategory.TIME, "min", "Minute", "min", UnitSystem.COMMON, 60),
    _unit(UnitCategory.TIME, "h", "Hour", "h", UnitSystem.COMMON, 3_600),
    _unit(UnitCategory.TIME, "day", "Day", "d", UnitSystem.COMMON, 86_400),
    _unit(UnitCategory.TIME, "week", "Week", "wk", UnitSystem.COMMON, 604_800),
    _unit(UnitCategory.TIME, "julian_year", "Julian year", "a", UnitSystem.ASTRONOMICAL, 31_557_600),
    # Pressure: pascal
    _unit(UnitCategory.PRESSURE, "pa", "Pascal", "Pa", UnitSystem.SI, 1),
    _unit(UnitCategory.PRESSURE, "kpa", "Kilopascal", "kPa", UnitSystem.SI, 1_000),
    _unit(UnitCategory.PRESSURE, "mpa", "Megapascal", "MPa", UnitSystem.SI, 1_000_000),
    _unit(UnitCategory.PRESSURE, "bar", "Bar", "bar", UnitSystem.METRIC, 100_000),
    _unit(UnitCategory.PRESSURE, "atm", "Standard atmosphere", "atm", UnitSystem.INTERNATIONAL, 101_325),
    _unit(UnitCategory.PRESSURE, "torr", "Torr", "Torr", UnitSystem.SCIENTIFIC, 101_325, 760),
    _decimal_unit(UnitCategory.PRESSURE, "mmhg", "Millimetre of mercury", "mmHg", UnitSystem.SCIENTIFIC, "133.322387415"),
    _decimal_unit(UnitCategory.PRESSURE, "psi", "Pounds per square inch", "psi", UnitSystem.US_CUSTOMARY, "6894.757293168"),
    # Energy: joule
    _unit(UnitCategory.ENERGY, "j", "Joule", "J", UnitSystem.SI, 1),
    _unit(UnitCategory.ENERGY, "kj", "Kilojoule", "kJ", UnitSystem.SI, 1_000),
    _unit(UnitCategory.ENERGY, "mj", "Megajoule", "MJ", UnitSystem.SI, 1_000_000),
    _unit(UnitCategory.ENERGY, "wh", "Watt-hour", "Wh", UnitSystem.COMMON, 3_600),
    _unit(UnitCategory.ENERGY, "kwh", "Kilowatt-hour", "kWh", UnitSystem.COMMON, 3_600_000),
    _unit(UnitCategory.ENERGY, "cal", "Thermochemical calorie", "cal", UnitSystem.SCIENTIFIC, 4_184, 1_000),
    _unit(UnitCategory.ENERGY, "kcal", "Kilocalorie", "kcal", UnitSystem.SCIENTIFIC, 4_184),
    _decimal_unit(UnitCategory.ENERGY, "btu", "BTU (IT)", "BTU", UnitSystem.US_CUSTOMARY, "1055.05585262"),
    _decimal_unit(UnitCategory.ENERGY, "ft_lbf", "Foot-pound force", "ft·lbf", UnitSystem.US_CUSTOMARY, "1.3558179483314"),
    _decimal_unit(UnitCategory.ENERGY, "ev", "Electronvolt", "eV", UnitSystem.SCIENTIFIC, "0.0000000000000000001602176634"),
    # Power: watt
    _unit(UnitCategory.POWER, "w", "Watt", "W", UnitSystem.SI, 1),
    _unit(UnitCategory.POWER, "kw", "Kilowatt", "kW", UnitSystem.SI, 1_000),
    _unit(UnitCategory.POWER, "mw", "Megawatt", "MW", UnitSystem.SI, 1_000_000),
    _decimal_unit(UnitCategory.POWER, "hp", "Mechanical horsepower", "hp", UnitSystem.US_CUSTOMARY, "745.69987158227022"),
    _decimal_unit(UnitCategory.POWER, "ps", "Metric horsepower", "PS", UnitSystem.METRIC, "735.49875"),
    _decimal_unit(UnitCategory.POWER, "btu_h", "BTU per hour", "BTU/h", UnitSystem.US_CUSTOMARY, "0.2930710701722222"),
    # Data: byte
    _unit(UnitCategory.DATA, "bit", "Bit", "bit", UnitSystem.COMPUTING, 1, 8),
    _unit(UnitCategory.DATA, "byte", "Byte", "B", UnitSystem.COMPUTING, 1),
    _unit(UnitCategory.DATA, "kb", "Kilobyte", "kB", UnitSystem.SI, 1_000),
    _unit(UnitCategory.DATA, "mb", "Megabyte", "MB", UnitSystem.SI, 1_000_000),
    _unit(UnitCategory.DATA, "gb", "Gigabyte", "GB", UnitSystem.SI, 1_000_000_000),
    _unit(UnitCategory.DATA, "tb", "Terabyte", "TB", UnitSystem.SI, 1_000_000_000_000),
    _unit(UnitCategory.DATA, "kib", "Kibibyte", "KiB", UnitSystem.COMPUTING, 1_024),
    _unit(UnitCategory.DATA, "mib", "Mebibyte", "MiB", UnitSystem.COMPUTING, 1_048_576),
    _unit(UnitCategory.DATA, "gib", "Gibibyte", "GiB", UnitSystem.COMPUTING, 1_073_741_824),
    _unit(UnitCategory.DATA, "tib", "Tebibyte", "TiB", UnitSystem.COMPUTING, 1_099_511_627_776),
    # Angle: radian (pi is the fixed CODATA precision used for display conversions)
    _unit(UnitCategory.ANGLE, "rad", "Radian", "rad", UnitSystem.SI, 1),
    _unit(UnitCategory.ANGLE, "mrad", "Milliradian", "mrad", UnitSystem.SI, 1, 1_000),
    _unit(UnitCategory.ANGLE, "deg", "Degree", "°", UnitSystem.COMMON, _PI_NUMERATOR, 180 * _PI_DENOMINATOR),
    _unit(UnitCategory.ANGLE, "arcmin", "Arcminute", "′", UnitSystem.COMMON, _PI_NUMERATOR, 10_800 * _PI_DENOMINATOR),
    _unit(UnitCategory.ANGLE, "arcsec", "Arcsecond", "″", UnitSystem.COMMON, _PI_NUMERATOR, 648_000 * _PI_DENOMINATOR),
    _unit(UnitCategory.ANGLE, "turn", "Turn", "turn", UnitSystem.COMMON, 2 * _PI_NUMERATOR, _PI_DENOMINATOR),
    _unit(UnitCategory.ANGLE, "grad", "Gradian", "gon", UnitSystem.METRIC, _PI_NUMERATOR, 200 * _PI_DENOMINATOR),
    # Frequency: hertz
    _unit(UnitCategory.FREQUENCY, "hz", "Hertz", "Hz", UnitSystem.SI, 1),
    _unit(UnitCategory.FREQUENCY, "khz", "Kilohertz", "kHz", UnitSystem.SI, 1_000),
    _unit(UnitCategory.FREQUENCY, "mhz", "Megahertz", "MHz", UnitSystem.SI, 1_000_000),
    _unit(UnitCategory.FREQUENCY, "ghz", "Gigahertz", "GHz", UnitSystem.SI, 1_000_000_000),
    _unit(UnitCategory.FREQUENCY, "rpm", "Revolutions per minute", "rpm", UnitSystem.COMMON, 1, 60),
    _unit(UnitCategory.FREQUENCY, "bpm", "Beats per minute", "bpm", UnitSystem.COMMON, 1, 60),
)

_UNITS_BY_CATEGORY: Final[dict[UnitCategory, tuple[UnitDefinition, ...]]] = {
    category: tuple(unit for unit in UNIT_DEFINITIONS if unit.category is category) for category in UnitCategory
}
_UNITS_BY_CATEGORY_AND_CODE: Final[dict[UnitCategory, dict[str, UnitDefinition]]] = {
    category: {unit.code: unit for unit in units} for category, units in _UNITS_BY_CATEGORY.items()
}
_UNIT_SYSTEM_DISPLAY_ORDER: Final[dict[UnitSystem, int]] = {
    system: position for position, system in enumerate(UnitSystem)
}
_DISPLAY_UNITS_BY_CATEGORY: Final[dict[UnitCategory, tuple[UnitDefinition, ...]]] = {
    category: tuple(
        sorted(
            units,
            key=lambda unit: (_UNIT_SYSTEM_DISPLAY_ORDER[unit.system], unit.name.casefold()),
        )
    )
    for category, units in _UNITS_BY_CATEGORY.items()
}


def unit_categories() -> tuple[UnitCategory, ...]:
    """Return unit categories in dashboard display order."""
    return tuple(UnitCategory)


def units_for_category(category: UnitCategory) -> tuple[UnitDefinition, ...]:
    """Return all conversion units for one category."""
    return _UNITS_BY_CATEGORY[category]


def display_units_for_category(category: UnitCategory) -> tuple[UnitDefinition, ...]:
    """Return units grouped by system and alphabetised within each group."""
    return _DISPLAY_UNITS_BY_CATEGORY[category]


def unit_definition(*, category: UnitCategory, code: str) -> UnitDefinition:
    """Resolve a configured unit code, raising a helpful error for bad input."""
    try:
        return _UNITS_BY_CATEGORY_AND_CODE[category][code]
    except KeyError as xcp:
        raise ValueError(f"Unknown {category.display_name.lower()} unit {code!r}.") from xcp


def convert_unit_amount(*, amount: Decimal | float | int, source: UnitDefinition, target: UnitDefinition) -> UnitConversion:
    """Convert a finite measurement between two units in the same category."""
    decimal_amount = _coerce_decimal(amount, field="Amount")
    if source.category is not target.category:
        raise ValueError("Units must belong to the same category.")
    converted_amount = target.from_base(source.to_base(decimal_amount))
    return UnitConversion(
        amount=decimal_amount,
        source=source,
        target=target,
        converted_amount=converted_amount,
    )


def convert_unit_category(*, amount: Decimal | float | int, source: UnitDefinition) -> tuple[UnitConversion, ...]:
    """Convert a value to every configured unit compatible with ``source``."""
    return tuple(
        convert_unit_amount(amount=amount, source=source, target=target)
        for target in units_for_category(source.category)
    )


def parse_unit_amount(value: str) -> Decimal:
    """Parse a finite decimal amount, accepting grouping separators and exponent notation."""
    if not isinstance(value, str):
        raise TypeError("Amount must be text.")
    normalised_value = value.strip().replace(",", "").replace("_", "")
    if not normalised_value:
        raise ValueError("Enter an amount.")
    try:
        decimal_value = Decimal(normalised_value)
    except InvalidOperation as xcp:
        raise ValueError("Enter a valid decimal amount.") from xcp
    return _require_finite_decimal(decimal_value, field="Amount")


def format_unit_amount(value: Decimal | float | int, *, significant_figures: int = 8) -> str:
    """Format a finite amount clearly without presenting spurious precision."""
    decimal_value = _coerce_decimal(value, field="Amount")
    if significant_figures < 1:
        raise ValueError("Significant figures must be positive.")
    if decimal_value.is_zero():
        return "0"
    adjusted = decimal_value.copy_abs().adjusted()
    if -6 <= adjusted < significant_figures:
        decimal_places = max(significant_figures - adjusted - 1, 0)
        rendered = f"{decimal_value:,.{decimal_places}f}"
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    rendered = f"{decimal_value:.{significant_figures - 1}E}"
    mantissa, exponent = rendered.split("E")
    return f"{mantissa.rstrip('0').rstrip('.')}e{int(exponent):+d}"


def _coerce_decimal(value: Decimal | float | int, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, float, int)):
        raise TypeError(f"{field} must be a number.")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as xcp:
        raise ValueError(f"{field} must be finite.") from xcp
    return _require_finite_decimal(decimal_value, field=field)


def _require_finite_decimal(value: Decimal, *, field: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field} must be finite.")
    return value
