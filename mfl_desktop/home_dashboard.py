"""Home dashboard data assembly (ADR-075, Arc F).

Qt-free. ``gather_home_data`` builds every card's data by calling the existing
shipped compute helpers (net worth via ``compute_account_values`` +
``convert_amount`` per ADR-055, budget via ``compute_matrix``, spending via the
FX-converting payee/category aggregates, bills via ``list_scheduled_txns``,
recent via ``list_recent_transactions``), so the dashboard can never disagree
with the dedicated screens. Each card is assembled independently and degrades to
empty on any error, so one bad card never blanks the screen.

Two cards are too heavy for that synchronous fast path — the net-worth trend and
investment performance both replay transaction history (ADR-150). They live in
``compute_net_worth_trend`` / ``compute_investment_performance`` and are run off
the UI thread by the view, not carried on ``HomeData``.

The view (``ui/home_view.py``) renders the returned ``HomeData`` into cards.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from mfl_desktop import budget_calc as bc
from mfl_desktop.holdings import compute_returns
from mfl_desktop.net_worth_history import gather_net_worth_history, month_end_samples
from mfl_desktop.reports.payee_report import build_report

logger = logging.getLogger(__name__)

_ZERO = Decimal("0.00")

# Family → asset/debt for the net-worth trend, single-sourced with the Net Worth
# screen's `_FAMILY_VIEW` (ADR-121). Kept here (not imported from the UI) so this
# module stays Qt-free.
_NW_FAMILY_KINDS = {
    "cash": "asset", "investment": "asset", "property": "asset",
    "vehicle": "asset", "credit": "debt", "loan": "debt",
}

_FAMILY_LABELS = {
    "cash": "Cash",
    "credit": "Credit cards",
    "investment": "Investments",
    "property": "Property",
    "vehicle": "Vehicles",
    "loan": "Loans",
}
_FAMILY_ORDER = ("cash", "credit", "investment", "property", "vehicle", "loan")


@dataclass(frozen=True)
class AccountLine:
    account_id: int
    iri: str
    name: str
    currency: str
    value: Optional[Decimal]   # in the display currency, or None if no FX rate


@dataclass(frozen=True)
class AccountGroup:
    family: str
    label: str
    subtotal: Decimal          # sum of convertible values, display currency
    accounts: list[AccountLine]


@dataclass(frozen=True)
class BudgetCard:
    name: str
    currency: str
    month_label: str           # e.g. "June 2026"
    # `planned` = THIS month's expense allocation (the monthly plan), so it
    # matches what the Budget page calls "assigned" rather than the envelope
    # `available` — which balloons with accumulated rollover and read as a huge
    # monthly budget (ADR-136, superseding ADR-087's use of `available` here).
    planned: Decimal
    spent: Decimal
    rollover: Decimal          # carried-over budget available on top this month


@dataclass(frozen=True)
class BillLine:
    label: str
    amount: Decimal            # signed (estimate)
    due_date: str              # 'YYYY-MM-DD'
    days_until: int
    overdue: bool


@dataclass(frozen=True)
class RecentTxn:
    txn_id: int
    account_id: int
    account_iri: str
    account_name: str
    posted_date: str
    payee: str
    category: str
    amount: Decimal


@dataclass(frozen=True)
class SpendRow:
    label: str
    amount: Decimal            # positive magnitude, display currency
    entity_id: Optional[int]   # payee_id / category_id (None for the no-* bucket)


@dataclass(frozen=True)
class HoldingPerf:
    """A per-security performance row. ``gain`` is a windowed true return in the
    display currency (ADR-150 amendment); ``pct`` is that return over the
    position's value at the window start."""
    security_id: int
    name: str
    symbol: str
    gain: Decimal
    pct: Optional[float]


@dataclass(frozen=True)
class HomeData:
    display_ccy: str
    net_worth: Decimal
    net_worth_excluded: int            # accounts with a value but no FX rate
    account_groups: list[AccountGroup] = field(default_factory=list)
    budget: Optional[BudgetCard] = None
    bills: list[BillLine] = field(default_factory=list)
    bills_overdue: int = 0
    recent: list[RecentTxn] = field(default_factory=list)
    top_payees: list[SpendRow] = field(default_factory=list)
    top_categories: list[SpendRow] = field(default_factory=list)
    # What period the two spending cards actually cover — "This month" normally,
    # or a named month ("June 2026") when the current one has no spending and
    # they fell back to the last one that did (ADR-163). The cards title
    # themselves from this, so the number on screen always says what it is.
    spend_period_label: str = "This month"
    # Investment performance is period-scoped and heavy, so it is computed off
    # the fast path in a background thread — see ``compute_investment_performance``
    # (ADR-150 amendment), not carried on HomeData.


@dataclass(frozen=True)
class NetWorthTrend:
    """The Home hero's 12-month net-worth trend (ADR-150). Computed off the fast
    path (in a background thread) because the underlying replay is ~400ms on a
    large file — see ``compute_net_worth_trend``. ``points`` are ``(iso_date,
    net)`` ascending in ``display_ccy`` (the monthly series the chart draws);
    the deltas summarise two rolling windows over that same span, ``None`` when
    undefined."""
    points: list[tuple[str, Decimal]]
    display_ccy: str
    change_30d: Optional[Decimal]          # net now − net 30 days ago
    change_30d_pct: Optional[float]        # change_30d / |net 30 days ago|
    change_year: Optional[Decimal]         # net now − net 12 months ago
    change_year_pct: Optional[float]       # change_year / |net 12 months ago|


@dataclass(frozen=True)
class InvestmentPerf:
    """Home's investment-performance card (ADR-150 amendment). Two portfolio
    windows matching the net-worth hero — last 30 days and last 12 months — as
    *true return* (excludes contributions), plus the 12-month top movers.
    Computed off the fast path in the same background thread as the trend."""
    display_ccy: str
    return_30d: Optional[Decimal]
    pct_30d: Optional[float]
    return_12m: Optional[Decimal]
    pct_12m: Optional[float]
    gainers: list[HoldingPerf] = field(default_factory=list)   # 12-month, best first
    losers: list[HoldingPerf] = field(default_factory=list)    # 12-month, worst first


def _display_currency(repo) -> str:
    """The dashboard's display currency: the base-currency setting, else a
    GBP/first-account fallback (mirrors ADR-055's intent loosely)."""
    base = repo.get_setting("base_currency")
    if base:
        return base
    accounts = repo.list_accounts()
    if any(a.currency == "GBP" for a in accounts):
        return "GBP"
    return accounts[0].currency if accounts else "GBP"


def _month_label(today: date) -> str:
    return today.strftime("%B %Y")


def gather_home_data(repo, today: date, *, recent_n: int = 8, top_n: int = 5) -> HomeData:
    """Assemble the whole dashboard. Resilient: each card's failure is logged
    and degrades to empty rather than raising."""
    display_ccy = _display_currency(repo)
    today_iso = today.isoformat()
    month_start = today.replace(day=1).isoformat()

    net_worth, excluded, groups = _net_worth_and_accounts(repo, display_ccy, today_iso)

    data = HomeData(
        display_ccy=display_ccy,
        net_worth=net_worth,
        net_worth_excluded=excluded,
        account_groups=groups,
    )

    # Each remaining card is optional/heavier — isolate failures.
    budget = _safe(_budget_card, repo, today, display_ccy, default=None)
    bills, overdue = _safe(_bills, repo, today, default=([], 0))
    recent = _safe(_recent, repo, recent_n, default=[])

    spend_from, spend_to, spend_label = _safe(
        _spend_window, repo, today,
        default=(month_start, today_iso, "This month"),
    )
    top_payees = _safe(_top_payees, repo, spend_from, spend_to, display_ccy, top_n, default=[])
    top_categories = _safe(
        _top_categories, repo, spend_from, spend_to, display_ccy, top_n, default=[]
    )

    return HomeData(
        display_ccy=display_ccy,
        net_worth=net_worth,
        net_worth_excluded=excluded,
        account_groups=groups,
        budget=budget,
        bills=bills,
        bills_overdue=overdue,
        recent=recent,
        top_payees=top_payees,
        top_categories=top_categories,
        spend_period_label=spend_label,
    )


def _spend_window(repo, today: date) -> tuple[str, str, str]:
    """The period the Top Payees / Top Categories cards cover (ADR-163).

    Normally the current calendar month. But the month-to-date window is empty
    on the 1st of a month, and stays empty on any file whose data stops earlier
    — and the dashboard's answer to that was two cards reading "No spending yet
    this month" with nothing under them, which reads as a broken app rather than
    as a quiet month.

    So: if the current month has no spending, fall back to **the last month that
    does**, and say which month that is. Falling back silently would be worse
    than the empty card — a figure labelled "this month" that is really March's
    is a lie, and the label is what makes this honest rather than merely full.
    """
    month_start = today.replace(day=1)
    this_month = today.strftime("%Y-%m")

    latest = repo.latest_spending_month(not_after=this_month)
    if latest is None or latest == this_month:
        # Either there is spending this month, or the file has none anywhere —
        # in which case an empty "This month" card is the honest answer.
        return month_start.isoformat(), today.isoformat(), "This month"

    year, month = int(latest[:4]), int(latest[5:7])
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start.strftime("%B %Y")


def compute_net_worth_trend(
    repo, today: date, display_ccy: Optional[str] = None, *, months: int = 12,
) -> Optional[NetWorthTrend]:
    """The last-``months`` net-worth trend for the Home hero (ADR-150).

    Qt-free, and deliberately kept **out** of ``gather_home_data``: the
    underlying ``gather_net_worth_history`` replay is ~400ms on a large file, so
    the view runs this in a background thread and folds the result into the hero
    when it lands. Returns ``None`` when there isn't enough history (< 2 samples)
    to draw a line — the hero then stays the ADR-119 number-only card."""
    ccy = display_ccy or _display_currency(repo)
    # 12 month-ends back, anchored to the first of that month, through today —
    # the series the chart draws. A rolling "30 days ago" point rides along in
    # the same replay purely to source the 30-day delta (it's kept out of the
    # chart so the line stays a clean monthly cadence).
    start = today.replace(year=today.year - 1, day=1)
    monthly = month_end_samples(start, today)
    d30 = today - timedelta(days=30)
    hist = gather_net_worth_history(
        repo, sample_dates=sorted(set(monthly) | {d30}), display_ccy=ccy,
        family_kinds=_NW_FAMILY_KINDS,
    )
    net_by_date = {p.date: p.net for p in hist.points}
    points = [
        (d.isoformat(), net_by_date[d.isoformat()])
        for d in monthly if d.isoformat() in net_by_date
    ]
    if len(points) < 2:
        return None
    net_now = net_by_date.get(today.isoformat(), points[-1][1])
    net_first = points[0][1]
    net_30 = net_by_date.get(d30.isoformat())

    change_30d = (net_now - net_30) if net_30 is not None else None
    change_30d_pct = (
        float(change_30d / abs(net_30))
        if (net_30 is not None and net_30 != 0) else None
    )
    change_year = net_now - net_first
    change_year_pct = (
        float(change_year / abs(net_first)) if net_first != 0 else None
    )
    return NetWorthTrend(
        points=points, display_ccy=ccy,
        change_30d=change_30d, change_30d_pct=change_30d_pct,
        change_year=change_year, change_year_pct=change_year_pct,
    )


def compute_investment_performance(
    repo, today: date, display_ccy: Optional[str] = None, *, top_n: int = 3,
) -> Optional["InvestmentPerf"]:
    """Investment performance over the hero's two windows — last 30 days and
    last 12 months — as *true return* (ADR-150 amendment), plus the 12-month top
    movers. Qt-free and kept OFF the fast path: it runs ``compute_returns``
    (ADR-046) per investment account per window, so the view computes it in the
    same background thread as the net-worth trend.

    A window's true return excludes contributions: for each account it is
    ``terminal_value − opening_value + Σ(in-window cash flows)`` — the identity
    the returns engine's IRR bracketing is built on — summed across accounts and
    FX-converted into ``display_ccy`` (each leg at its own date, ADR-055).
    Returns ``None`` when there are no priced investment positions."""
    ccy = display_ccy or _display_currency(repo)
    investment = [a for a in repo.list_accounts() if a.family == "investment"]
    if not investment:
        return None
    multipliers = repo.security_multipliers()
    today_iso = today.isoformat()

    def conv(amount: Decimal, from_ccy: str, on_date: str) -> Decimal:
        if amount is None:
            return _ZERO
        if from_ccy == ccy:
            return amount
        c, _fb = repo.convert_amount(
            amount, from_ccy=from_ccy, to_ccy=ccy, on_date=on_date,
        )
        return c if c is not None else _ZERO   # unconvertible → excluded

    # Each account's history is gathered once and replayed per window.
    acct_data = []
    for acct in investment:
        txns = repo.list_transactions_for_account(acct.id)
        sec_ids = {t.security_id for t in txns if t.security_id is not None}
        if not sec_ids:
            continue
        pser = {
            sid: [(p.price_date, p.price) for p in repo.price_series(sid)]
            for sid in sec_ids
        }
        acct_data.append((acct, txns, sec_ids, pser))
    if not acct_data:
        return None

    def window(start: date):
        ws = start.isoformat()
        samples = month_end_samples(start, today)
        tot_return = _ZERO
        tot_open = _ZERO
        by_sec: dict[int, dict] = {}

        def _flows(cash_flows, from_ccy) -> Decimal:
            return sum((conv(a, from_ccy, d) for d, a in cash_flows), _ZERO)

        for acct, txns, sec_ids, pser in acct_data:
            res = compute_returns(txns, samples, pser, ws, sec_ids, multipliers)
            open_mv = conv(res.opening_market_value, acct.currency, ws)
            term_mv = conv(res.terminal_market_value, acct.currency, today_iso)
            tot_return += term_mv - open_mv + _flows(res.cash_flows, acct.currency)
            tot_open += open_mv
            for s in res.by_security:
                s_open = conv(s.opening_market_value, acct.currency, ws)
                s_term = conv(s.terminal_market_value, acct.currency, today_iso)
                slot = by_sec.setdefault(
                    s.security_id,
                    {"name": s.name, "symbol": s.symbol,
                     "return": _ZERO, "open": _ZERO},
                )
                slot["return"] += s_term - s_open + _flows(s.cash_flows, acct.currency)
                slot["open"] += s_open
        pct = float(tot_return / tot_open) if tot_open > 0 else None
        return tot_return, pct, by_sec

    try:
        d12 = today.replace(year=today.year - 1)
    except ValueError:                       # today is 29 Feb
        d12 = today.replace(year=today.year - 1, day=28)

    ret30, pct30, _ = window(today - timedelta(days=30))
    ret12, pct12, by_sec12 = window(d12)

    def _mover_pct(ret: Decimal, opening: Decimal) -> Optional[float]:
        """Return-on-opening-value, or ``None`` when that base is degenerate. A
        position built mostly *within* the window has a tiny opening value, so
        return/opening explodes (a real £2.6k→£45k holding reads as +1591 %); we
        show its money gain without a misleading percentage."""
        if opening <= 0:
            return None
        p = float(ret / opening)
        return p if abs(p) <= 3.0 else None

    movers = [
        HoldingPerf(
            security_id=sid, name=v["name"], symbol=v["symbol"], gain=v["return"],
            pct=_mover_pct(v["return"], v["open"]),
        )
        for sid, v in by_sec12.items()
    ]
    gainers = sorted(
        [m for m in movers if m.gain > 0], key=lambda m: m.gain, reverse=True,
    )[:top_n]
    losers = sorted([m for m in movers if m.gain < 0], key=lambda m: m.gain)[:2]

    if not gainers and not losers and ret30 == 0 and ret12 == 0:
        return None
    return InvestmentPerf(
        display_ccy=ccy, return_30d=ret30, pct_30d=pct30,
        return_12m=ret12, pct_12m=pct12, gainers=gainers, losers=losers,
    )


def _safe(fn, *args, default):
    try:
        return fn(*args)
    except Exception:
        logger.exception("Home dashboard card %s failed", getattr(fn, "__name__", fn))
        return default


def _net_worth_and_accounts(repo, display_ccy, today_iso):
    values = repo.compute_account_values()      # native per account
    accounts = repo.list_accounts()
    total = _ZERO
    excluded = 0
    by_family: dict[str, list[AccountLine]] = {}
    subtotals: dict[str, Decimal] = {}
    for a in accounts:
        native = values.get(a.id, _ZERO)
        conv, _fallback = repo.convert_amount(
            native, from_ccy=a.currency, to_ccy=display_ccy, on_date=today_iso,
        )
        if conv is None:
            if native != 0:
                excluded += 1
            line_val: Optional[Decimal] = None
        else:
            total += conv
            line_val = conv
            subtotals[a.family] = subtotals.get(a.family, _ZERO) + conv
        by_family.setdefault(a.family, []).append(
            AccountLine(
                account_id=a.id, iri=a.iri, name=a.name,
                currency=a.currency, value=line_val,
            )
        )

    groups: list[AccountGroup] = []
    seen = set()
    for fam in (*_FAMILY_ORDER, *sorted(by_family)):
        if fam in seen or fam not in by_family:
            continue
        seen.add(fam)
        lines = sorted(by_family[fam], key=lambda l: l.name.lower())
        groups.append(
            AccountGroup(
                family=fam,
                label=_FAMILY_LABELS.get(fam, fam.title()),
                subtotal=subtotals.get(fam, _ZERO),
                accounts=lines,
            )
        )
    return total, excluded, groups


def _budget_card(repo, today, display_ccy) -> Optional[BudgetCard]:
    budgets = repo.list_budgets()
    if not budgets:
        return None
    budget = budgets[0]                          # most recent / default
    months = budget.months()
    today_month = today.strftime("%Y-%m")
    if today_month not in months:
        return None                              # budget doesn't cover this month
    ccy = budget.currency or display_ccy
    lines = repo.list_budget_lines(budget.id)
    allocations = repo.list_budget_allocations(budget.id)
    ptxns = repo.list_perimeter_txns(
        budget.id, months[0] + "-01", months[-1] + "-31",
    )
    pool, excluded = repo.compute_perimeter_pool(
        budget.id, display_ccy=ccy, on_date=today.isoformat(),
    )
    matrix = bc.compute_matrix(
        budget=budget, lines=lines, allocations=allocations,
        perimeter_txns=ptxns, parent_map=repo.category_parent_map(),
        kind_map=repo.category_kind_map(), pool=pool,
        excluded_accounts=excluded, display_ccy=ccy, today_month=today_month,
    )
    idx = months.index(today_month)
    expenses = next((s for s in matrix.sections if s.kind == "expense"), None)
    if expenses is None or idx >= len(expenses.subtotal):
        return None
    cell = expenses.subtotal[idx]
    # `planned` = this month's expense allocation (the monthly plan); the
    # accumulated rollover (`available − allocation`) is surfaced separately as
    # `rollover` so the card reads "spent of this month's budget (+ £N carried
    # over)" instead of "spent of a giant available" (ADR-136).
    rollover = cell.available - cell.allocation
    return BudgetCard(
        name=budget.name, currency=ccy, month_label=_month_label(today),
        planned=cell.allocation, spent=cell.actual,
        rollover=rollover if rollover > 0 else Decimal("0.00"),
    )


def _bills(repo, today, *, horizon=6):
    scheds = repo.list_scheduled_txns()
    rows: list[BillLine] = []
    for s in scheds:
        try:
            days = (date.fromisoformat(s.next_due_date) - today).days
        except ValueError:
            continue
        label = (
            s.payee_name or s.category_name
            or s.transfer_to_account_name or "Scheduled"
        )
        rows.append(
            BillLine(
                label=label, amount=s.estimated_amount,
                due_date=s.next_due_date, days_until=days, overdue=days < 0,
            )
        )
    rows.sort(key=lambda b: b.due_date)
    overdue = sum(1 for b in rows if b.overdue)
    return rows[:horizon], overdue


def _recent(repo, recent_n):
    rows = repo.list_recent_transactions(limit=recent_n)
    accts = {a.id: a for a in repo.list_accounts(include_closed=True)}
    out: list[RecentTxn] = []
    for t in rows:
        a = accts.get(t.account_id)
        out.append(
            RecentTxn(
                txn_id=t.id, account_id=t.account_id,
                account_iri=a.iri if a else "",
                account_name=t.account_name,
                posted_date=t.posted_date,
                payee=t.payee_name or "",
                category=(
                    "— Split —" if t.split_count else (t.category_name or "")
                ),
                amount=t.amount,
            )
        )
    return out


def _top_payees(repo, date_from, date_to, display_ccy, top_n):
    raw = repo.payee_spending_aggregates(
        date_from=date_from, date_to=date_to, display_currency=display_ccy,
    )
    result = build_report(raw["payees"], top_n=top_n)
    return [
        SpendRow(label=r.name, amount=r.amount, entity_id=r.payee_id)
        for r in result.rows
    ]


def _top_categories(repo, date_from, date_to, display_ccy, top_n):
    raw = repo.category_payee_matrix(
        date_from=date_from, date_to=date_to, display_currency=display_ccy,
    )
    by_cat: dict[int, int] = {}
    for cell in raw["cells"]:
        cid = cell["category_id"]
        by_cat[cid] = by_cat.get(cid, 0) + cell["spending_pence"]
    names = {c.id: (c.name or c.path) for c in repo.list_categories_flat()}
    ranked = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [
        SpendRow(
            label=names.get(cid, f"#{cid}"),
            amount=(Decimal(pence) / 100).quantize(_ZERO),
            entity_id=cid,
        )
        for cid, pence in ranked
    ]
