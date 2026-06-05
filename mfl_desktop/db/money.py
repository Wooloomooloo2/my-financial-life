"""Currency conversion between Decimal (interface) and pence (storage).

Per ADR-010, currency amounts are stored as INTEGER minor units. These two
helpers are the only place that conversion happens; service and UI code
work in Decimal.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

_PENCE_PER_UNIT = Decimal("100")
_TWO_PLACES = Decimal("0.01")


def decimal_to_pence(value: Decimal) -> int:
    """Round to 2 decimal places (banker's rounding) and scale to pence."""
    rounded = value.quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)
    return int(rounded * _PENCE_PER_UNIT)


def pence_to_decimal(pence: int) -> Decimal:
    """Scale pence back to a 2-decimal-place Decimal."""
    return (Decimal(pence) / _PENCE_PER_UNIT).quantize(_TWO_PLACES)
