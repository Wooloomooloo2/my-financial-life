"""Pure-helper tests for net worth over time (ADR-121).

``cash_balance_at_dates`` and ``month_end_samples`` are pure + Qt-free; they
run on the base interpreter or under pytest:

    python3 tests/test_net_worth_history.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mfl_desktop.net_worth_history import (  # noqa: E402
    cash_balance_at_dates, month_end_samples,
)


@dataclass
class _T:
    posted_date: Optional[str]
    amount: Decimal
    security_id: Optional[int] = None


def test_month_end_samples_spans_start_interior_end() -> None:
    s = month_end_samples(date(2025, 1, 15), date(2025, 4, 10))
    assert s[0] == date(2025, 1, 15)
    assert s[-1] == date(2025, 4, 10)
    # interior month-ends Jan/Feb/Mar (Apr's end is past the window end)
    assert date(2025, 1, 31) in s
    assert date(2025, 2, 28) in s
    assert date(2025, 3, 31) in s


def test_month_end_samples_degenerate() -> None:
    assert month_end_samples(date(2025, 5, 1), date(2025, 5, 1)) == [date(2025, 5, 1)]
    # end before start collapses to the two endpoints
    assert month_end_samples(date(2025, 5, 1), date(2025, 4, 1)) == [
        date(2025, 5, 1), date(2025, 4, 1),
    ]


def test_cash_balance_inclusive_boundary_and_running() -> None:
    txns = [
        _T("2025-01-10", Decimal("100.00")),
        _T("2025-02-15", Decimal("-30.00")),
        _T("2025-03-20", Decimal("50.00")),
        _T(None, Decimal("999.00")),          # undated row ignored
    ]
    samples = ["2025-01-10", "2025-02-01", "2025-02-15", "2025-04-01"]
    bal = cash_balance_at_dates(txns, Decimal("5.00"), samples)
    # opening 5; +100 on the 10th (inclusive); -30 on 2/15 (inclusive); +50 by 4/1
    assert bal == [
        Decimal("105.00"),   # 2025-01-10 includes the +100
        Decimal("105.00"),   # 2025-02-01 nothing new
        Decimal("75.00"),    # 2025-02-15 includes the -30
        Decimal("125.00"),   # 2025-04-01 includes the +50
    ]


def test_cash_balance_empty_txns_is_opening() -> None:
    assert cash_balance_at_dates([], Decimal("42.00"), ["2025-01-01", "2025-06-01"]) == [
        Decimal("42.00"), Decimal("42.00"),
    ]


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
