"""Holdings & cost-basis engine — pure Python, no Qt, no SQL (ADR-044).

Mirrors account_summary.py: the Repository hands in a list of TransactionRow
(the investment-account register, already carrying action / security_id /
quantity / price / signed cash amount) plus the account's opening balance and a
latest-price map; this module replays the transactions to produce per-security
holdings with FIFO cost basis, market value, and unrealized + realized gain.

FIFO (the owner's chosen basis method, ADR-044): each share-in pushes a lot
``(qty, unit_cost)``; each share-out consumes lots oldest-first, accruing
realized gain = proceeds − matched lot cost. Computed fresh on every call from
the transactions — NOT persisted to the `lot` table (that stays reserved for
when manual basis overrides / specific-ID sales arrive), so there is no
source-of-truth to keep in sync.

Scope notes (round 2):
- Whole-account transfers (XIn / XOut) do NOT move share lots here — that is
  the round-4 transfer-linking concern. (This also sidesteps a known QIF quirk
  in the owner's data: a transfer-out mislabelled as ``XIn``.)
- Stock-split ratio application is deferred — splits are skipped with a logged
  note (the only split in the owner's data is a malformed empty row anyway).
- Shares transferred in (``ShrsIn``) often carry no price, so their lot has no
  known basis; the holding is flagged ``basis_incomplete`` and its cost/gain
  are approximate.
"""
from __future__ import annotations

import bisect
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from mfl_desktop.db.repository import TransactionRow
from mfl_desktop.import_engine.qif_actions import (
    is_share_in, is_share_out, is_split,
)

logger = logging.getLogger(__name__)

_EPS = 1e-9  # share-quantity tolerance (REAL rounding)

# Cash-funded buys: the lot's cost is the true net cash that left the account
# (txn.amount, which includes commission per ADR-043). Other share-ins
# (ReinvDiv / ShrsIn) have no separate cash leg, so their cost is price × qty.
_CASH_BUY_ACTIONS = {"buy", "buyx", "cvrshrt"}


def _to_money(x: float) -> Decimal:
    """Round a float currency amount to 2dp Decimal for display/aggregation."""
    return Decimal(str(round(x, 2)))


@dataclass
class _Lot:
    qty: float
    unit_cost: float          # per-share cost basis (may be 0 when unknown)
    known_basis: bool         # False for transferred-in shares with no price


@dataclass(frozen=True)
class Holding:
    security_id: int
    name: str
    symbol: str
    shares: float
    cost_basis: Decimal                 # Σ open-lot qty × unit_cost
    avg_unit_cost: Optional[float]      # cost_basis / shares (None if shares ~0)
    last_price: Optional[float]
    last_price_date: Optional[str]
    market_value: Optional[Decimal]     # None when unpriced
    unrealized_gain: Optional[Decimal]  # None when unpriced
    unrealized_pct: Optional[float]
    realized_gain: Decimal              # lifetime, this security
    basis_incomplete: bool
    priced: bool


@dataclass(frozen=True)
class HoldingsView:
    holdings: list[Holding] = field(default_factory=list)  # open positions, sorted
    cash_balance: Decimal = Decimal("0.00")
    holdings_market_value: Decimal = Decimal("0.00")       # priced positions only
    account_value: Decimal = Decimal("0.00")               # cash + priced market value
    total_cost_basis: Decimal = Decimal("0.00")            # open positions
    total_unrealized_gain: Decimal = Decimal("0.00")       # priced positions only
    total_realized_gain: Decimal = Decimal("0.00")         # all securities, lifetime
    unpriced_count: int = 0                                # open positions with no price


def compute_holdings_view(
    txns: list[TransactionRow],
    opening_balance: Decimal,
    latest_prices: dict[int, tuple[float, str]],
) -> HoldingsView:
    """Replay an investment account's transactions into a HoldingsView.

    ``latest_prices`` maps security_id → (price, as_of_date 'YYYY-MM-DD').
    Securities absent from the map are 'unpriced' (market value shown as —).
    """
    # FIFO lot queues + realized-gain accumulators, keyed by security_id.
    lots: dict[int, deque[_Lot]] = {}
    realized: dict[int, float] = {}
    incomplete: dict[int, bool] = {}
    meta: dict[int, tuple[str, str]] = {}   # security_id → (name, symbol)
    cash = Decimal(opening_balance)

    # Chronological replay; list_transactions_for_account already returns
    # (posted_date, id) ascending, but sort defensively so the engine is order
    # independent of its caller.
    for t in sorted(txns, key=lambda r: (r.posted_date, r.id)):
        cash += t.amount
        if t.security_id is None or t.action is None:
            continue
        sid = t.security_id
        meta.setdefault(sid, (t.security_name or "", t.security_symbol or ""))
        lots.setdefault(sid, deque())
        realized.setdefault(sid, 0.0)
        incomplete.setdefault(sid, False)

        qty = float(t.quantity) if t.quantity is not None else 0.0

        if is_share_in(t.action):
            if qty <= _EPS:
                continue
            known = True
            if t.action.strip().lower() in _CASH_BUY_ACTIONS and t.amount != 0:
                lot_cost = float(abs(t.amount))        # true net cash (incl. commission)
            elif t.price is not None:
                lot_cost = float(t.price) * qty         # reinvest / shares-in with a price
            else:
                lot_cost = 0.0                           # transferred-in, basis unknown
                known = False
                incomplete[sid] = True
            lots[sid].append(_Lot(qty=qty, unit_cost=lot_cost / qty, known_basis=known))

        elif is_share_out(t.action):
            if qty <= _EPS:
                continue
            proceeds = float(abs(t.amount))
            remaining = qty
            cost_removed = 0.0
            queue = lots[sid]
            while remaining > _EPS and queue:
                lot = queue[0]
                take = min(remaining, lot.qty)
                cost_removed += take * lot.unit_cost
                lot.qty -= take
                remaining -= take
                if lot.qty <= _EPS:
                    queue.popleft()
            realized[sid] += proceeds - cost_removed
            if remaining > _EPS:
                # Sold more than we have basis for (shares predating the import,
                # or a missing transfer-in). Realized gain is approximate.
                incomplete[sid] = True
                logger.info(
                    "Holdings: oversold %.4f shares of security %d beyond known "
                    "lots — realized gain approximate.", remaining, sid,
                )

        elif is_split(t.action):
            logger.info(
                "Holdings: stock split on security %d not applied (ratio "
                "handling deferred, ADR-044). Verify this holding manually.", sid,
            )
            incomplete[sid] = True
        # XIn / XOut and cash-only actions (Div / CGShort / …) don't move lots.

    holdings: list[Holding] = []
    holdings_mv = Decimal("0.00")
    total_cost = Decimal("0.00")
    total_unrealized = Decimal("0.00")
    unpriced = 0

    for sid, queue in lots.items():
        name, symbol = meta.get(sid, ("", ""))
        shares = sum(lot.qty for lot in queue)
        cost_f = sum(lot.qty * lot.unit_cost for lot in queue)
        cost_basis = _to_money(cost_f)
        realized_gain = _to_money(realized.get(sid, 0.0))

        if shares <= _EPS:
            # Fully closed position — no open holding row, but its realized
            # gain still counts toward the account total.
            continue

        avg_cost = cost_f / shares if shares > _EPS else None
        price_entry = latest_prices.get(sid)
        if price_entry is not None:
            price, price_date = price_entry
            market_value = _to_money(shares * price)
            unrealized = market_value - cost_basis
            pct = float(unrealized / cost_basis * 100) if cost_basis != 0 else None
            holdings_mv += market_value
            total_cost += cost_basis
            total_unrealized += unrealized
            priced = True
        else:
            price = price_date = None
            market_value = unrealized = None
            pct = None
            total_cost += cost_basis
            unpriced += 1
            priced = False

        holdings.append(Holding(
            security_id=sid, name=name, symbol=symbol, shares=shares,
            cost_basis=cost_basis, avg_unit_cost=avg_cost,
            last_price=price, last_price_date=price_date,
            market_value=market_value, unrealized_gain=unrealized,
            unrealized_pct=pct, realized_gain=realized_gain,
            basis_incomplete=incomplete.get(sid, False), priced=priced,
        ))

    # Lifetime realized gain across every security (open and fully-closed).
    total_realized = _to_money(sum(realized.values()))

    # Priced first (by market value desc), then unpriced (by cost desc), then name.
    holdings.sort(key=lambda h: (
        0 if h.priced else 1,
        -(float(h.market_value) if h.market_value is not None else 0.0),
        -float(h.cost_basis),
        h.name.lower(),
    ))

    cash_balance = cash.quantize(Decimal("0.01"))
    account_value = cash_balance + holdings_mv
    return HoldingsView(
        holdings=holdings,
        cash_balance=cash_balance,
        holdings_market_value=holdings_mv,
        account_value=account_value,
        total_cost_basis=total_cost,
        total_unrealized_gain=total_unrealized,
        total_realized_gain=total_realized,
        unpriced_count=unpriced,
    )


# ── Valuation over time (ADR-045) ──────────────────────────────────────────


@dataclass(frozen=True)
class ValuePoint:
    """One sample on the value-over-time chart. ``invested_cost`` is the FIFO
    cost basis of the holdings held on ``date`` (exact, price-free);
    ``market_value`` is Σ shares × nearest-prior price, falling back to a
    holding's cost when no price is on file by that date — ``fully_priced`` is
    False whenever any held security used that fallback."""
    date: str                # 'YYYY-MM-DD'
    invested_cost: Decimal
    market_value: Decimal
    fully_priced: bool


def _lot_cost(action: str, amount: Decimal, price: Optional[float], qty: float) -> tuple[float, bool]:
    """Per-lot total cost + whether the basis is known. Matches the rule in
    compute_holdings_view: cash-funded buys use the true net cash; reinvests /
    transfers-in use price × qty; an unknown price means basis 0 / unknown."""
    if action.strip().lower() in _CASH_BUY_ACTIONS and amount != 0:
        return float(abs(amount)), True
    if price is not None:
        return float(price) * qty, True
    return 0.0, False


def compute_value_history(
    txns: list[TransactionRow],
    sample_dates: list,
    price_series_by_security: dict[int, list[tuple[str, float]]],
) -> list[ValuePoint]:
    """Replay the account's investment transactions, snapshotting cost basis +
    market value at each ``sample_dates`` entry (date or 'YYYY-MM-DD' string).

    ``price_series_by_security`` maps security_id → ascending ``(date, price)``
    pairs (e.g. Repository.price_series). Nearest-prior price per sample date is
    an in-memory bisect, so this is a single O(txns + securities×samples) pass.
    """
    samples = sorted({
        d.isoformat() if isinstance(d, date) else str(d) for d in sample_dates
    })
    if not samples:
        return []

    # Per-security ascending date arrays for bisect (ISO sorts chronologically).
    series_dates = {
        sid: [d for d, _ in ser] for sid, ser in price_series_by_security.items()
    }

    def nearest_price(sid: int, on_date: str) -> Optional[float]:
        dates = series_dates.get(sid)
        if not dates:
            return None
        i = bisect.bisect_right(dates, on_date) - 1
        if i < 0:
            return None
        return price_series_by_security[sid][i][1]

    lots: dict[int, deque[_Lot]] = {}
    ordered = sorted(txns, key=lambda r: (r.posted_date, r.id))
    ti = 0
    points: list[ValuePoint] = []

    for sample in samples:
        while ti < len(ordered) and ordered[ti].posted_date <= sample:
            t = ordered[ti]
            ti += 1
            if t.security_id is None or t.action is None:
                continue
            qty = float(t.quantity) if t.quantity is not None else 0.0
            lots.setdefault(t.security_id, deque())
            if is_share_in(t.action) and qty > _EPS:
                cost, known = _lot_cost(t.action, t.amount, t.price, qty)
                lots[t.security_id].append(
                    _Lot(qty=qty, unit_cost=cost / qty, known_basis=known)
                )
            elif is_share_out(t.action) and qty > _EPS:
                remaining = qty
                queue = lots[t.security_id]
                while remaining > _EPS and queue:
                    lot = queue[0]
                    take = min(remaining, lot.qty)
                    lot.qty -= take
                    remaining -= take
                    if lot.qty <= _EPS:
                        queue.popleft()
            # splits (deferred) and XIn/XOut don't move lots here — same as
            # compute_holdings_view.

        invested = 0.0
        market = 0.0
        fully = True
        for sid, queue in lots.items():
            shares = sum(lot.qty for lot in queue)
            if shares <= _EPS:
                continue
            cost = sum(lot.qty * lot.unit_cost for lot in queue)
            invested += cost
            price = nearest_price(sid, sample)
            if price is not None:
                market += shares * price
            else:
                market += cost
                fully = False
        points.append(ValuePoint(
            date=sample,
            invested_cost=_to_money(invested),
            market_value=_to_money(market),
            fully_priced=fully,
        ))
    return points
