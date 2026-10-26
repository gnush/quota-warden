from decimal import Decimal

import pytest

from quota_warden.quantity import QuantityError, add, format_quantity, parse


@pytest.mark.parametrize(
    "value,expected",
    [
        ("500m", Decimal("0.5")),
        ("1.5", Decimal("1.5")),
        ("130Gi", Decimal(130 * 1024**3)),
        ("422Ti", Decimal(422 * 1024**4)),
        ("2k", Decimal(2000)),
        ("0", Decimal(0)),
        (8, Decimal(8)),
        (0, Decimal(0)),
    ],
)
def test_parse(value, expected):
    assert parse(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "-1",
        "-500m",
        -8,
        "abc",
        "",
        "1Xi",
        "4.6E+14",
        "1e3",
        "NaN",
        "Infinity",
        " 1",
        "100n",
        "0.0001",
        1.5,
        True,
        None,
        Decimal("1"),
    ],
)
def test_parse_rejects(value):
    with pytest.raises(QuantityError):
        parse(value)


def test_parse_accepts_exa_suffix_not_exponent():
    assert parse("1E") == Decimal(10**18)


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("1024Gi", "1Ti", "2Ti"),
        ("500m", "1", "1.5"),
        ("500m", "1Gi", "1073741824.5"),
        ("300Gi", "425Ti", "435500Gi"),
    ],
)
def test_add(a, b, expected):
    assert add(parse(a), parse(b)) == parse(expected)


def test_add_rejects_non_decimal():
    with pytest.raises(TypeError):
        add(Decimal(1), 1.5)


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("2.000"), "2"),
        (Decimal("2000"), "2000"),
        (Decimal("4.6E+14"), "460000000000000"),
        (Decimal("1.5"), "1.5"),
        (Decimal("0.500"), "0.5"),
        (Decimal("0"), "0"),
        (Decimal("0E+3"), "0"),
    ],
)
def test_format_quantity_is_plain(value, expected):
    assert format_quantity(value) == expected
    assert "E" not in format_quantity(value)
    assert parse(format_quantity(value)) == value


def test_format_quantity_rejects_float():
    with pytest.raises(TypeError):
        format_quantity(1.5)
