"""Kubernetes quantities as exact Decimals."""

import re
from decimal import Decimal

from kubernetes.utils.quantity import parse_quantity

# Stricter than parse_quantity, which also takes floats, exponents, signs, NaN
# and Infinity.
_SHAPE = re.compile(r"(\d+(\.\d*)?|\.\d+)(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE])?")


class QuantityError(ValueError):
    """A source value the merge must drop."""


def parse(value: str | int) -> Decimal:
    # int is legal: CRD values are x-kubernetes-int-or-string. bool is an int.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise QuantityError(f"{value!r}: expected a string or int, got {type(value).__name__}")
    if isinstance(value, int):
        value = str(value)
    if value.startswith("-"):
        raise QuantityError(f"{value!r}: negative")
    if not _SHAPE.fullmatch(value):
        raise QuantityError(f"{value!r}: not a plain quantity")
    result = parse_quantity(value)
    # the API server would round this up to 1m
    if (result * 1000) % 1:
        raise QuantityError(f"{value!r}: finer than 1m")
    return result


def add(a: Decimal, b: Decimal) -> Decimal:
    if not (isinstance(a, Decimal) and isinstance(b, Decimal)):
        raise TypeError(f"add() takes Decimals, got {type(a).__name__}, {type(b).__name__}")
    return a + b


def format_quantity(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise TypeError(f"format_quantity() takes a Decimal, got {type(value).__name__}")
    # normalize() strips 2.000 to 2; 'f' undoes the exponent normalize() may add
    return format(value.normalize(), "f")
