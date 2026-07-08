"""Bare share-transfers don't distort the money-weighted return (ADR-046 amend 3).

A bare ShrsIn/ShrsOut (no ``transfer_id``, no counterpart leg) is an import
artifact — an opening-balance seed, a correction, or a corporate action such as
a stock split recorded as a share deposit. The returns engine used to book every
in-window transfer at market value as an IRR cash flow, so a split-as-ShrsIn
injected a large phantom contribution that could drag a strongly positive
holding to a *negative* IRR (the real SCHD case: +41% total return, −2.2% IRR).

The guard (``holdings._transfer_books_irr_flow``): only a LINKED transfer (shares
a ``transfer_id`` with its partner) books a market-value IRR flow; a bare
transfer moves shares but contributes no flow. This exercises the real engine
(``compute_returns``) on synthetic rows, then brackets the per-security flows the
same way the report window does before calling ``xirr``.

Pure-Python (no Qt / DB); run under any python3 with the repo on the path:

    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_returns_transfer_irr_guard.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import TransactionRow
from mfl_desktop.holdings import compute_returns, xirr

WINDOW_START, END = "2020-01-01", "2023-01-01"
SAMPLES = [WINDOW_START, END]
# Flat $10 through the buy + transfer, $15 at the window end.
SERIES = {1: [("2020-01-01", 10.0), ("2022-01-01", 10.0), ("2023-01-01", 15.0)]}

_seq = iter(range(1, 10_000))


def _row(posted_date, action, *, amount="0", qty=None, price=None,
         transfer_id=None) -> TransactionRow:
    i = next(_seq)
    return TransactionRow(
        id=i, iri=f"mfl:T{i}", account_id=1, account_name="Broker",
        posted_date=posted_date, amount=Decimal(amount), payee_id=None,
        payee_name="", category_id=1, category_name="Uncategorised",
        status="cleared", memo="", running_balance=Decimal("0"),
        transfer_id=transfer_id, action=action, security_id=1,
        security_name="AAA", security_symbol="AAA", quantity=qty, price=price,
    )


def _sec(txns):
    res = compute_returns(txns, SAMPLES, SERIES, WINDOW_START, security_ids={1})
    return next(s for s in res.by_security if s.security_id == 1)


def _irr(sec) -> float:
    flows = []
    if sec.opening_market_value != 0:
        flows.append((WINDOW_START, -float(sec.opening_market_value)))
    flows.extend((d, float(a)) for d, a in sec.cash_flows)
    if sec.terminal_market_value != 0:
        flows.append((END, float(sec.terminal_market_value)))
    return xirr(flows)


def test_bare_shrsin_books_no_irr_flow():
    # Buy 100 @ $10, then 100 shares appear as a bare ShrsIn (split-as-deposit).
    txns = [
        _row("2020-01-01", "Buy", amount="-1000", qty=100.0, price=10.0),
        _row("2022-01-01", "ShrsIn", amount="0", qty=100.0),   # bare: no transfer_id
    ]
    sec = _sec(txns)
    # Only the buy is an external flow — the bare deposit is not counted.
    assert sec.cash_flows == [("2020-01-01", Decimal("-1000.00"))], sec.cash_flows
    assert abs(sec.shares - 200.0) < 1e-6
    assert sec.terminal_market_value == Decimal("3000.00")
    # $1000 in (2020) → $3000 value (2023): ~44%/yr, cleanly positive.
    irr = _irr(sec)
    assert 0.40 < irr < 0.48, irr


def test_linked_transfer_still_books_a_flow():
    # Same, but the ShrsIn is LINKED to a partner leg — a genuine custodian move.
    txns = [
        _row("2020-01-01", "Buy", amount="-1000", qty=100.0, price=10.0),
        _row("2022-01-01", "ShrsIn", amount="0", qty=100.0, transfer_id="pair-1"),
    ]
    sec = _sec(txns)
    # The linked leg books a market-value contribution (−100 × $10) in 2022.
    assert ("2022-01-01", Decimal("-1000.00")) in sec.cash_flows, sec.cash_flows
    assert len(sec.cash_flows) == 2
    # Booking $1000 of "fresh capital" in 2022 pulls the money-weighted return
    # below the bare-artifact case.
    assert _irr(sec) < 0.40


def test_bare_shrsout_books_no_irr_flow():
    # Buy 200 @ $10, then 100 shares leave as a bare ShrsOut (no counterpart).
    txns = [
        _row("2020-01-01", "Buy", amount="-2000", qty=200.0, price=10.0),
        _row("2022-01-01", "ShrsOut", amount="0", qty=100.0),   # bare
    ]
    sec = _sec(txns)
    assert sec.cash_flows == [("2020-01-01", Decimal("-2000.00"))], sec.cash_flows
    assert abs(sec.shares - 100.0) < 1e-6           # 100 left, no realized gain
    assert sec.realized_window == Decimal("0.00")


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
