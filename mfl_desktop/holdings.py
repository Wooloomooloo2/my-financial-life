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

In-kind share transfers (``ShrsIn`` / ``ShrsOut``, ADR-053): a ShrsOut is a
custodian move, not a sale, so it removes shares with **zero realized gain**
(treating its $0 cash as proceeds would book the whole cost basis as a phantom
loss) and parks the popped lots in a per-security "transfer pen"; a later
matching ``ShrsIn`` pulls its cost basis from that pen, so an out→in transfer
nets to no realized impact and preserves cost basis across accounts. A ShrsIn
with no matching prior ShrsOut (e.g. shares that entered the tracked history
already held elsewhere) still has unknown basis unless the row carries a price —
its lot is cost-0 and the holding is flagged ``basis_incomplete``.
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
    is_income, is_reinvest, is_share_in, is_share_out, is_share_transfer,
    is_split,
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


def xirr(flows: list[tuple[str, float]]) -> Optional[float]:
    """Money-weighted annualized return (XIRR) for irregular dated cash flows —
    the ADR-046 IRR companion. ``flows`` is ``(iso_date, amount)`` where amount
    is signed from the investor's perspective: contributions / buys / the
    opening market value are **negative** (money put to work), and returns /
    sells / dividends / the terminal market value are **positive** (money the
    portfolio gave back).

    Returns the annual rate ``r`` solving ``Σ aᵢ / (1+r)^((dᵢ−d₀)/365) = 0``, or
    ``None`` when the return is undefined — fewer than two flows, or no sign
    change (e.g. only contributions, or only one dated point). Solved by
    bisection over a wide bracket, which is robust where Newton can diverge on
    the steep NPV curve near ``r = −1``; a single sign change (the normal
    invest-then-realize shape) guarantees a unique interior root.
    """
    if len(flows) < 2:
        return None
    amounts = [a for _, a in flows]
    if not (any(a > _EPS for a in amounts) and any(a < -_EPS for a in amounts)):
        return None  # need both an outflow and an inflow, else no root
    d0 = min(date.fromisoformat(d) for d, _ in flows)
    times = [(date.fromisoformat(d) - d0).days / 365.0 for d, _ in flows]
    if all(t == times[0] for t in times):
        return None  # every flow on one date → no time spread to solve over

    def npv(rate: float) -> float:
        base = 1.0 + rate
        return sum(a / base ** t for a, t in zip(amounts, times))

    lo, hi = -0.999999, 1000.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0.0) == (f_hi > 0.0):
        return None  # no sign change in the bracket → no trustworthy real root
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7 or (hi - lo) < 1e-12:
            return mid
        if (f_mid > 0.0) == (f_lo > 0.0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return (lo + hi) / 2.0


@dataclass
class _Lot:
    qty: float
    unit_cost: float          # per-share cost basis (may be 0 when unknown)
    known_basis: bool         # False for transferred-in shares with no price


def _transfer_out(queue: "deque[_Lot]", qty: float, pen: "deque[_Lot]") -> float:
    """Pop FIFO lots totalling up to ``qty`` from ``queue`` and append them to a
    per-security transfer ``pen`` (basis preserved) — a ShrsOut moves shares
    without selling them (ADR-053). Returns the quantity that couldn't be
    satisfied from known lots (transferred out beyond what we have basis for)."""
    remaining = qty
    while remaining > _EPS and queue:
        lot = queue[0]
        take = min(remaining, lot.qty)
        pen.append(_Lot(qty=take, unit_cost=lot.unit_cost, known_basis=lot.known_basis))
        lot.qty -= take
        remaining -= take
        if lot.qty <= _EPS:
            queue.popleft()
    return remaining


def _peek_fifo_cost(queue: "deque[_Lot]", qty: float) -> float:
    """Cost basis of the first ``qty`` shares of a FIFO queue WITHOUT mutating
    it. Used to value an in-kind transfer leg for the money-weighted return
    (ADR-046 IRR) when no market price is on file for the transfer date."""
    remaining = qty
    cost = 0.0
    for lot in queue:
        if remaining <= _EPS:
            break
        take = min(remaining, lot.qty)
        cost += take * lot.unit_cost
        remaining -= take
    return cost


def _apply_split(queue: "deque[_Lot]", ratio: float) -> None:
    """Apply a stock split to a FIFO queue (ADR-054): multiply every open lot's
    share count by ``ratio`` and divide its per-share cost by ``ratio``, so the
    total cost basis is unchanged and the market value stays continuous across
    the split (the post-split shares meet the post-split price). ``ratio`` is
    new shares per old — 5 for a 5-for-1 split, 0.1 for a reverse 1-for-10. A
    non-positive or ~1.0 ratio is a no-op."""
    if ratio <= 0 or abs(ratio - 1.0) <= _EPS:
        return
    for lot in queue:
        lot.qty *= ratio
        lot.unit_cost /= ratio


def _transfer_in(queue: "deque[_Lot]", qty: float, pen: "deque[_Lot]") -> float:
    """Satisfy up to ``qty`` of an incoming ShrsIn from the transfer ``pen``,
    carrying each matched lot's cost basis onto ``queue`` (ADR-053). Returns the
    quantity still unmatched — shares with no prior ShrsOut to carry basis from,
    handled by the caller (explicit price, else unknown basis)."""
    remaining = qty
    while remaining > _EPS and pen:
        src = pen[0]
        take = min(remaining, src.qty)
        queue.append(_Lot(qty=take, unit_cost=src.unit_cost, known_basis=src.known_basis))
        src.qty -= take
        remaining -= take
        if src.qty <= _EPS:
            pen.popleft()
    return remaining


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
    multipliers: Optional[dict[int, float]] = None,
) -> HoldingsView:
    """Replay an investment account's transactions into a HoldingsView.

    ``latest_prices`` maps security_id → (price, as_of_date 'YYYY-MM-DD').
    Securities absent from the map are 'unpriced' (market value shown as —).

    ``multipliers`` maps security_id → price multiplier (ADR-093): every
    ``shares × price`` value site is scaled by it, so a bond (face/100) or an
    option (contract_size) values correctly while a stock (the default 1.0,
    used for any security absent from the map) is unchanged.
    """
    mults = multipliers or {}
    # FIFO lot queues + realized-gain accumulators, keyed by security_id.
    lots: dict[int, deque[_Lot]] = {}
    pending: dict[int, deque[_Lot]] = {}    # transfer pen: ShrsOut → matching ShrsIn
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
            remaining_in = qty
            if is_share_transfer(t.action):
                # Transfer in: carry FIFO cost basis from a matching ShrsOut
                # earlier in the replay rather than treating the shares as free.
                remaining_in = _transfer_in(
                    lots[sid], qty, pending.setdefault(sid, deque()),
                )
                if remaining_in <= _EPS:
                    continue
                # Any shares beyond a matching transfer-out fall through to the
                # explicit-price / unknown-basis handling for the remainder.
            known = True
            accrued = (
                float(t.accrued_interest) if t.accrued_interest is not None else 0.0
            )
            if t.action.strip().lower() in _CASH_BUY_ACTIONS and t.amount != 0:
                # True net cash (incl. commission) less prepaid accrued interest,
                # which is reclaimed at the first coupon and isn't cost (ADR-093).
                lot_cost = float(abs(t.amount)) - accrued
            elif t.price is not None:
                # reinvest / shares-in with a price — × multiplier (ADR-093).
                lot_cost = float(t.price) * remaining_in * mults.get(sid, 1.0)
            else:
                lot_cost = 0.0                           # transferred-in, basis unknown
                known = False
                incomplete[sid] = True
            lots[sid].append(
                _Lot(qty=remaining_in, unit_cost=lot_cost / remaining_in, known_basis=known)
            )

        elif is_share_out(t.action):
            if qty <= _EPS:
                continue
            if is_share_transfer(t.action):
                # Transfer out: remove shares but DON'T realize a gain/loss — a
                # custodian move, not a sale. Park the popped lots so a matching
                # ShrsIn can carry their basis (ADR-053).
                unmatched = _transfer_out(
                    lots[sid], qty, pending.setdefault(sid, deque()),
                )
                if unmatched > _EPS:
                    incomplete[sid] = True
                continue
            # Accrued interest received on a bond sale is interest, not capital
            # proceeds — exclude it from the realized-gain calc (ADR-093), the
            # mirror of excluding it from a buy's basis.
            sell_accrued = (
                float(t.accrued_interest) if t.accrued_interest is not None else 0.0
            )
            proceeds = float(abs(t.amount)) - sell_accrued
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
            ratio = float(t.quantity) if t.quantity else 0.0
            if ratio > 0:
                _apply_split(lots[sid], ratio)        # ADR-054
            else:
                incomplete[sid] = True
                logger.info(
                    "Holdings: stock split on security %d has no ratio "
                    "(quantity) — skipped; verify this holding.", sid,
                )
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
            # Value scales by the security's multiplier (bond %-of-par / option
            # contract size); the displayed last_price stays the raw quote.
            market_value = _to_money(shares * price * mults.get(sid, 1.0))
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


def _lot_cost(
    action: str, amount: Decimal, price: Optional[float], qty: float,
    mult: float = 1.0, accrued: float = 0.0,
) -> tuple[float, bool]:
    """Per-lot total cost + whether the basis is known. Matches the rule in
    compute_holdings_view: cash-funded buys use the true net cash (less any
    accrued interest, which is prepaid coupon, not cost — ADR-093); reinvests /
    transfers-in use price × qty × multiplier (the multiplier scales a bond's
    %-of-par or an option's contract size, ADR-093); an unknown price means
    basis 0 / unknown."""
    if action.strip().lower() in _CASH_BUY_ACTIONS and amount != 0:
        return float(abs(amount)) - accrued, True
    if price is not None:
        return float(price) * qty * mult, True
    return 0.0, False


def compute_value_history(
    txns: list[TransactionRow],
    sample_dates: list,
    price_series_by_security: dict[int, list[tuple[str, float]]],
    multipliers: Optional[dict[int, float]] = None,
) -> list[ValuePoint]:
    """Replay the account's investment transactions, snapshotting cost basis +
    market value at each ``sample_dates`` entry (date or 'YYYY-MM-DD' string).

    ``price_series_by_security`` maps security_id → ascending ``(date, price)``
    pairs (e.g. Repository.price_series). Nearest-prior price per sample date is
    an in-memory bisect, so this is a single O(txns + securities×samples) pass.

    ``multipliers`` (ADR-093) scales each ``shares × price`` value by the
    security's bond/option multiplier; default 1.0 per security.
    """
    mults = multipliers or {}
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
    pending: dict[int, deque[_Lot]] = {}    # transfer pen (ADR-053)
    ordered = sorted(txns, key=lambda r: (r.posted_date, r.id))
    ti = 0
    points: list[ValuePoint] = []

    for sample in samples:
        while ti < len(ordered) and ordered[ti].posted_date <= sample:
            t = ordered[ti]
            ti += 1
            if t.security_id is None or t.action is None:
                continue
            sid = t.security_id
            qty = float(t.quantity) if t.quantity is not None else 0.0
            lots.setdefault(sid, deque())
            if is_share_in(t.action) and qty > _EPS:
                remaining_in = qty
                if is_share_transfer(t.action):
                    # Transfer in: carry basis from a matching ShrsOut so the
                    # cost line doesn't drop on a custodian move (ADR-053).
                    remaining_in = _transfer_in(
                        lots[sid], qty, pending.setdefault(sid, deque()),
                    )
                    if remaining_in <= _EPS:
                        continue
                accrued = (
                    float(t.accrued_interest)
                    if t.accrued_interest is not None else 0.0
                )
                cost, known = _lot_cost(
                    t.action, t.amount, t.price, remaining_in,
                    mults.get(sid, 1.0), accrued,
                )
                lots[sid].append(
                    _Lot(qty=remaining_in, unit_cost=cost / remaining_in, known_basis=known)
                )
            elif is_share_out(t.action) and qty > _EPS:
                if is_share_transfer(t.action):
                    _transfer_out(lots[sid], qty, pending.setdefault(sid, deque()))
                    continue
                remaining = qty
                queue = lots[sid]
                while remaining > _EPS and queue:
                    lot = queue[0]
                    take = min(remaining, lot.qty)
                    lot.qty -= take
                    remaining -= take
                    if lot.qty <= _EPS:
                        queue.popleft()
            elif is_split(t.action):
                _apply_split(lots[sid], float(t.quantity) if t.quantity else 0.0)
            # XIn/XOut don't move lots here — same as compute_holdings_view.

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
                market += shares * price * mults.get(sid, 1.0)
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


# ── Total return over time (ADR-046) ────────────────────────────────────────


@dataclass(frozen=True)
class ReturnPoint:
    """One sample on the returns chart, in the account's native currency.

    ``unrealized = market_value - cost_basis`` is the lifetime appreciation of
    the shares held on ``date`` (a snapshot — not period-scoped). ``realized_cum``
    and ``dividends_cum`` accumulate *only* flows dated on/after the window start
    (ADR-046 — a sale or distribution before the window contributes nothing), so
    both reset to zero at the window's left edge. ``fully_priced`` is False when
    any held security fell back to cost for lack of a price by ``date``."""
    date: str
    cost_basis: Decimal
    market_value: Decimal
    unrealized: Decimal
    realized_cum: Decimal
    dividends_cum: Decimal
    fully_priced: bool


@dataclass(frozen=True)
class SecurityReturn:
    """End-of-window total-return breakdown for one security (ADR-046).

    ``realized_window`` / ``dividends_window`` count only flows within the
    selected window; ``unrealized`` is the lifetime appreciation of the shares
    still held at the window end (``None`` when the position is unpriced, or
    ``shares == 0`` for a position fully exited *within* the window — which
    still carries its windowed realized gain / dividends). ``total_return``
    sums the three components, treating an unknown unrealized as zero."""
    security_id: int
    symbol: str
    name: str
    shares: float
    cost_basis: Decimal                  # cost of shares STILL HELD at window end
    cost_basis_sold: Decimal             # cost of shares SOLD within the window
    market_value: Optional[Decimal]
    unrealized: Optional[Decimal]
    realized_window: Decimal
    dividends_window: Decimal
    total_return: Decimal
    priced: bool
    # Per-security money-weighted-return (IRR) inputs, native currency (ADR-046
    # companion). Same shape as the portfolio-level fields on ReturnsResult: the
    # caller brackets ``cash_flows`` with −opening_market_value / +terminal_
    # market_value (converted) and calls xirr() once per security.
    cash_flows: list = field(default_factory=list)   # list[tuple[str, Decimal]]
    opening_market_value: Decimal = Decimal("0.00")
    terminal_market_value: Decimal = Decimal("0.00")
    irr_fully_priced: bool = True


@dataclass(frozen=True)
class ReturnsResult:
    """Portfolio total-return view for one account over a window (ADR-046).
    Portfolio totals are the end-of-window state; market value / unrealized
    count priced positions only (unpriced contribute nothing — matching
    compute_holdings_view), while the chart ``points`` use a cost fallback so
    the value line never collapses (flagged via ``fully_priced``)."""
    points: list[ReturnPoint] = field(default_factory=list)
    by_security: list[SecurityReturn] = field(default_factory=list)
    cost_basis: Decimal = Decimal("0.00")           # held at window end
    cost_basis_sold: Decimal = Decimal("0.00")      # sold within the window
    market_value: Decimal = Decimal("0.00")
    unrealized: Decimal = Decimal("0.00")
    realized_window: Decimal = Decimal("0.00")
    dividends_window: Decimal = Decimal("0.00")
    total_return: Decimal = Decimal("0.00")
    fully_priced: bool = True
    unpriced_count: int = 0
    # Money-weighted-return (IRR) inputs, native currency (ADR-046 companion).
    # ``cash_flows`` are the dated external flows WITHIN the window (buys/sells/
    # distributions, signed from the investor's view: outflows negative); the
    # caller brackets them with −opening_market_value at the window start and
    # +terminal_market_value at the window end (converting per currency) before
    # calling xirr(). ``irr_fully_priced`` is False when any bookend or transfer
    # leg fell back to cost for lack of a price.
    cash_flows: list = field(default_factory=list)   # list[tuple[str, Decimal]]
    opening_market_value: Decimal = Decimal("0.00")
    terminal_market_value: Decimal = Decimal("0.00")
    irr_fully_priced: bool = True


def _transfer_books_irr_flow(t: "TransactionRow") -> bool:
    """Whether an in-kind share transfer (ShrsIn / ShrsOut) counts as a
    market-value cash flow for the money-weighted return (ADR-046 amendment 3).

    Only a **linked** transfer does — one that shares a ``transfer_id`` with its
    counterpart leg, i.e. a genuine custodian move whose value entered or left
    the measured accounts. A **bare** ShrsIn/ShrsOut (no ``transfer_id``, no
    counterpart) is an import artifact — an opening-balance seed, a correction,
    or a corporate action such as a stock split recorded as a share deposit
    (the ~2,000 Banktivity bare pseudo-transfers). Booking one at market value
    injects a phantom contribution/withdrawal that can swing an IRR wildly
    (e.g. a split-as-ShrsIn dragging a +41 %% total-return holding to a negative
    IRR), so bare transfers move shares but contribute **no** IRR flow. Matched
    bare pairs already netted to zero (equal +mv / −mv on the same date), so
    suppressing both legs leaves that net unchanged; the only behaviour change
    is a single-sided bare transfer, which is far likelier an artifact than a
    real external flow."""
    tid = getattr(t, "transfer_id", None)
    return tid is not None and str(tid).strip() != ""


def compute_returns(
    txns: list[TransactionRow],
    sample_dates: list,
    price_series_by_security: dict[int, list[tuple[str, float]]],
    window_start: str,
    security_ids: Optional[set[int]] = None,
    multipliers: Optional[dict[int, float]] = None,
) -> ReturnsResult:
    """Replay one investment account's transactions into a total-return view.

    Produces the per-sample chart series (``points``), an end-of-window
    per-security breakdown (``by_security``), and portfolio totals.

    The FIFO replay processes the *entire* transaction history so cost basis
    and open shares are always correct, but realized gains and dividend/income
    only count toward the window accumulators when the originating transaction
    is dated on/after ``window_start`` (ADR-046 — period-scoped flows).

    ``sample_dates`` are date/ISO points within the window (e.g. month-ends);
    ``price_series_by_security`` maps security_id → ascending ``(date, price)``
    pairs (Repository.price_series); ``security_ids`` (``None`` = all) restricts
    the view to a subset of securities. ``multipliers`` (ADR-093) scales each
    ``shares × price`` / ``qty × price`` market-value site by the security's
    bond/option multiplier (default 1.0). Currency-agnostic — the caller
    converts when aggregating accounts of differing currencies.
    """
    mults = multipliers or {}
    samples = sorted({
        d.isoformat() if isinstance(d, date) else str(d) for d in sample_dates
    })
    if not samples:
        return ReturnsResult()

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

    def included(sid: int) -> bool:
        return security_ids is None or sid in security_ids

    lots: dict[int, deque[_Lot]] = {}
    pending: dict[int, deque[_Lot]] = {}   # transfer pen: ShrsOut → matching ShrsIn
    realized_win: dict[int, float] = {}
    dividends_win: dict[int, float] = {}
    cost_sold_win: dict[int, float] = {}   # cost basis removed by in-window sells
    meta: dict[int, tuple[str, str]] = {}

    # Money-weighted-return (IRR) state, native currency (ADR-046 companion).
    # Tracked both combined (portfolio IRR) and per-security (per-row IRR).
    cash_flows: list[tuple[str, Decimal]] = []
    cash_flows_by_sec: dict[int, list[tuple[str, Decimal]]] = {}
    opening_taken = False
    opening_mv = 0.0
    opening_by_sec: dict[int, float] = {}
    irr_fully = True
    irr_incomplete_sec: dict[int, bool] = {}   # transfer/bookend cost fallback per sid

    def add_flow(sid_: int, dt: str, amt: Decimal) -> None:
        cash_flows.append((dt, amt))
        cash_flows_by_sec.setdefault(sid_, []).append((dt, amt))

    def snapshot_mv(on_date: str) -> tuple[float, bool, dict[int, float]]:
        """Market value of all currently-held lots on ``on_date`` (nearest-prior
        price, cost fallback when unpriced). Returns
        ``(total_mv, fully_priced, mv_by_security)``; a per-security cost
        fallback also flags that security's IRR incomplete."""
        total = 0.0
        fully = True
        by_sec: dict[int, float] = {}
        for sid_, queue_ in lots.items():
            shares_ = sum(lot.qty for lot in queue_)
            if shares_ <= _EPS:
                continue
            price_ = nearest_price(sid_, on_date)
            if price_ is not None:
                mv_ = shares_ * price_ * mults.get(sid_, 1.0)
            else:
                mv_ = sum(lot.qty * lot.unit_cost for lot in queue_)
                fully = False
                irr_incomplete_sec[sid_] = True
            by_sec[sid_] = mv_
            total += mv_
        return total, fully, by_sec

    ordered = sorted(txns, key=lambda r: (r.posted_date, r.id))
    ti = 0
    points: list[ReturnPoint] = []

    for sample in samples:
        while ti < len(ordered) and ordered[ti].posted_date <= sample:
            t = ordered[ti]
            ti += 1
            # Opening market value = the portfolio value just before the first
            # transaction dated on/after the window start. The (posted_date, id)
            # sort means every pre-window txn precedes every in-window one, so
            # ``lots`` here reflects exactly the holdings carried INTO the window
            # (a buy ON the start date is an in-window flow, not opening capital).
            if not opening_taken and t.posted_date >= window_start:
                opening_mv, of, opening_by_sec = snapshot_mv(window_start)
                irr_fully = irr_fully and of
                opening_taken = True
            if t.security_id is None or t.action is None:
                continue
            sid = t.security_id
            if not included(sid):
                continue
            meta.setdefault(sid, (t.security_name or "", t.security_symbol or ""))
            lots.setdefault(sid, deque())
            realized_win.setdefault(sid, 0.0)
            dividends_win.setdefault(sid, 0.0)
            cost_sold_win.setdefault(sid, 0.0)
            in_window = t.posted_date >= window_start
            qty = float(t.quantity) if t.quantity is not None else 0.0

            if is_share_in(t.action):
                if qty > _EPS:
                    remaining_in = qty
                    if is_share_transfer(t.action):
                        # Transfer in: carry FIFO cost basis from a matching
                        # ShrsOut so the round-trip preserves basis and books no
                        # phantom gain/loss (ADR-053). For the money-weighted
                        # return a LINKED transfer is a contribution of value AT
                        # MARKET on the transfer date — a negative flow for the
                        # whole leg (the matched basis source is irrelevant to
                        # the IRR). A bare/unlinked ShrsIn is an artifact, not a
                        # real contribution, so it books no IRR flow (ADR-046
                        # amendment 3 — see _transfer_books_irr_flow).
                        if in_window and _transfer_books_irr_flow(t):
                            price = nearest_price(sid, t.posted_date)
                            if price is not None:
                                mv = _to_money(qty * price * mults.get(sid, 1.0))
                            else:
                                mv = _to_money(_peek_fifo_cost(
                                    pending.setdefault(sid, deque()), qty))
                                irr_fully = False
                                irr_incomplete_sec[sid] = True
                            add_flow(sid, t.posted_date, -mv)
                        remaining_in = _transfer_in(
                            lots[sid], qty, pending.setdefault(sid, deque()),
                        )
                        if remaining_in <= _EPS:
                            continue
                    accrued = (
                        float(t.accrued_interest)
                        if t.accrued_interest is not None else 0.0
                    )
                    cost, known = _lot_cost(
                        t.action, t.amount, t.price, remaining_in,
                        mults.get(sid, 1.0), accrued,
                    )
                    lots[sid].append(
                        _Lot(qty=remaining_in, unit_cost=cost / remaining_in, known_basis=known)
                    )
                    if in_window and not is_share_transfer(t.action):
                        if is_reinvest(t.action):
                            # Reinvested distribution: income = the reinvested
                            # value (no separate cash leg, so use the lot cost /
                            # amount). It's internal cash, so it's NOT an
                            # external flow for the IRR (it nets to zero).
                            reinv = float(abs(t.amount)) if t.amount != 0 else cost
                            dividends_win[sid] += reinv
                        else:
                            # Cash-funded buy: txn.amount is the signed cash
                            # impact (negative — money deployed). An IRR outflow.
                            add_flow(sid, t.posted_date, t.amount)
            elif is_share_out(t.action):
                if qty > _EPS:
                    if is_share_transfer(t.action):
                        # Transfer out: remove shares, NO realized gain/loss —
                        # park the lots for the matching ShrsIn (ADR-053). For
                        # the IRR a LINKED transfer is a withdrawal of value at
                        # market (a matching in-portfolio ShrsIn cancels it for a
                        # whole-portfolio view; for a single account it correctly
                        # leaves). A bare/unlinked ShrsOut is an artifact and
                        # books no IRR flow (ADR-046 amendment 3).
                        if in_window and _transfer_books_irr_flow(t):
                            price = nearest_price(sid, t.posted_date)
                            if price is not None:
                                mv = _to_money(qty * price * mults.get(sid, 1.0))
                            else:
                                mv = _to_money(_peek_fifo_cost(lots[sid], qty))
                                irr_fully = False
                                irr_incomplete_sec[sid] = True
                            add_flow(sid, t.posted_date, mv)
                        _transfer_out(lots[sid], qty, pending.setdefault(sid, deque()))
                        continue
                    # Accrued interest received on a bond sale is interest, not
                    # capital proceeds (ADR-093) — exclude from realized gain.
                    sell_accrued = (
                        float(t.accrued_interest)
                        if t.accrued_interest is not None else 0.0
                    )
                    proceeds = float(abs(t.amount)) - sell_accrued
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
                    if in_window:
                        realized_win[sid] += proceeds - cost_removed
                        cost_sold_win[sid] += cost_removed
                        # Sale proceeds: txn.amount is positive — an IRR inflow.
                        add_flow(sid, t.posted_date, t.amount)
            elif is_income(t.action):
                if in_window:
                    dividends_win[sid] += float(t.amount)
                    # Cash distribution received: a positive IRR inflow.
                    add_flow(sid, t.posted_date, t.amount)
            elif is_split(t.action):
                _apply_split(lots[sid], float(t.quantity) if t.quantity else 0.0)
            # XIn/XOut: no lot move, no income (round-4 transfer-linking).

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
                market += shares * price * mults.get(sid, 1.0)
            else:
                market += cost
                fully = False
        points.append(ReturnPoint(
            date=sample,
            cost_basis=_to_money(invested),
            market_value=_to_money(market),
            unrealized=_to_money(market - invested),
            realized_cum=_to_money(sum(realized_win.values())),
            dividends_cum=_to_money(sum(dividends_win.values())),
            fully_priced=fully,
        ))

    # Opening bookend fallback (must precede the per-security breakdown, which
    # reads opening_by_sec): if no transaction fell on/after the window start the
    # snapshot trigger never fired; ``lots`` is then unchanged across the window,
    # so valuing it at the window start gives the correct opening capital.
    if not opening_taken:
        opening_mv, of, opening_by_sec = snapshot_mv(window_start)
        irr_fully = irr_fully and of

    # ── end-of-window per-security breakdown + portfolio totals ──
    last_sample = samples[-1]
    by_security: list[SecurityReturn] = []
    tot_cost = Decimal("0.00")
    tot_cost_sold = Decimal("0.00")
    tot_mv = Decimal("0.00")
    tot_unreal = Decimal("0.00")
    tot_realized = Decimal("0.00")
    tot_div = Decimal("0.00")
    unpriced = 0
    end_fully = True

    all_sids = set(lots) | set(realized_win) | set(dividends_win)
    for sid in all_sids:
        queue = lots.get(sid, deque())
        shares = sum(lot.qty for lot in queue)
        realized_w = _to_money(realized_win.get(sid, 0.0))
        dividends_w = _to_money(dividends_win.get(sid, 0.0))
        cost_sold_w = _to_money(cost_sold_win.get(sid, 0.0))
        held = shares > _EPS

        if not held and realized_w == 0 and dividends_w == 0 and cost_sold_w == 0:
            # Fully exited before the window with no in-window flows — skip.
            continue

        name, symbol = meta.get(sid, ("", ""))
        market_value: Optional[Decimal] = None
        unrealized: Optional[Decimal] = None
        priced = False
        terminal_sec = Decimal("0.00")    # IRR terminal flow (cost fallback)
        if held:
            cost_basis = _to_money(sum(lot.qty * lot.unit_cost for lot in queue))
            tot_cost += cost_basis
            price = nearest_price(sid, last_sample)
            if price is not None:
                market_value = _to_money(shares * price * mults.get(sid, 1.0))
                unrealized = market_value - cost_basis
                priced = True
                tot_mv += market_value
                tot_unreal += unrealized
                terminal_sec = market_value
            else:
                unpriced += 1
                end_fully = False
                terminal_sec = cost_basis            # fallback → IRR approximate
                irr_incomplete_sec[sid] = True
        else:
            cost_basis = Decimal("0.00")

        unreal_for_total = unrealized if unrealized is not None else Decimal("0.00")
        total_return = unreal_for_total + realized_w + dividends_w
        tot_realized += realized_w
        tot_div += dividends_w
        tot_cost_sold += cost_sold_w
        by_security.append(SecurityReturn(
            security_id=sid, symbol=symbol, name=name, shares=shares,
            cost_basis=cost_basis, cost_basis_sold=cost_sold_w,
            market_value=market_value,
            unrealized=unrealized, realized_window=realized_w,
            dividends_window=dividends_w, total_return=total_return,
            priced=priced,
            cash_flows=cash_flows_by_sec.get(sid, []),
            opening_market_value=_to_money(opening_by_sec.get(sid, 0.0)),
            terminal_market_value=terminal_sec,
            irr_fully_priced=not irr_incomplete_sec.get(sid, False),
        ))

    by_security.sort(key=lambda s: (
        0 if s.shares > _EPS else 1,
        -float(s.total_return),
        s.name.lower(),
    ))

    # ── portfolio IRR bookends ──
    # Terminal value at the window end (cost fallback already applied by the
    # last sample's point) — the final positive flow for the IRR.
    terminal_mv = points[-1].market_value if points else Decimal("0.00")
    irr_fully = irr_fully and (points[-1].fully_priced if points else True)

    return ReturnsResult(
        points=points,
        by_security=by_security,
        cost_basis=tot_cost,
        cost_basis_sold=tot_cost_sold,
        market_value=tot_mv,
        unrealized=tot_unreal,
        realized_window=tot_realized,
        dividends_window=tot_div,
        total_return=tot_unreal + tot_realized + tot_div,
        fully_priced=end_fully,
        unpriced_count=unpriced,
        cash_flows=cash_flows,
        opening_market_value=_to_money(opening_mv),
        terminal_market_value=terminal_mv,
        irr_fully_priced=irr_fully,
    )
