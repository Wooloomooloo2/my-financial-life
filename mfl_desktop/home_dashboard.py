"""Home dashboard data assembly (ADR-075, Arc F).

Qt-free. ``gather_home_data`` builds every card's data by calling the existing
shipped compute helpers (net worth via ``compute_account_values`` +
``convert_amount`` per ADR-055, budget via ``compute_matrix``, spending via the
FX-converting payee/category aggregates, holdings via ``compute_holdings_view``,
bills via ``list_scheduled_txns``, recent via ``list_recent_transactions``), so
the dashboard can never disagree with the dedicated screens. Each card is
assembled independently and degrades to empty on any error, so one bad card
never blanks the screen.

The view (``ui/home_view.py``) renders the returned ``HomeData`` into cards.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from mfl_desktop import budget_calc as bc
from mfl_desktop.holdings import compute_holdings_view
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
    security_id: int
    name: str
    symbol: str
    gain: Decimal              # unrealized, display = security's account currency
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
    invest_gains: list[HoldingPerf] = field(default_factory=list)
    invest_losses: list[HoldingPerf] = field(default_factory=list)


@dataclass(frozen=True)
class NetWorthTrend:
    """The Home hero's 12-month net-worth trend (ADR-150). Computed off the fast
    path (in a background thread) because the underlying replay is ~400ms on a
    large file — see ``compute_net_worth_trend``. ``points`` are ``(iso_date,
    net)`` ascending in ``display_ccy``; deltas are ``None`` when undefined."""
    points: list[tuple[str, Decimal]]
    display_ccy: str
    change_month: Optional[Decimal]        # net now − net at the prior month-end
    change_month_pct: Optional[float]      # change_month / |prior month-end net|
    change_year: Optional[Decimal]         # net now − net 12 months ago


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
    top_payees = _safe(_top_payees, repo, month_start, today_iso, display_ccy, top_n, default=[])
    top_categories = _safe(
        _top_categories, repo, month_start, today_iso, display_ccy, top_n, default=[]
    )
    gains, losses = _safe(
        _investment_perf, repo, display_ccy, today_iso, top_n, default=([], []),
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
        invest_gains=gains,
        invest_losses=losses,
    )


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
    # 12 month-ends back, anchored to the first of that month, through today.
    start = today.replace(year=today.year - 1, day=1)
    samples = month_end_samples(start, today)
    hist = gather_net_worth_history(
        repo, sample_dates=samples, display_ccy=ccy,
        family_kinds=_NW_FAMILY_KINDS,
    )
    pts = [(p.date, p.net) for p in hist.points]
    if len(pts) < 2:
        return None
    net_now = pts[-1][1]
    net_prev = pts[-2][1]              # most recent full month-end before today
    net_first = pts[0][1]
    change_month = net_now - net_prev
    change_pct = (
        float(change_month / abs(net_prev)) if net_prev != 0 else None
    )
    return NetWorthTrend(
        points=pts, display_ccy=ccy,
        change_month=change_month, change_month_pct=change_pct,
        change_year=net_now - net_first,
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


def _investment_perf(repo, display_ccy, today_iso, top_n):
    investment = [a for a in repo.list_accounts() if a.family == "investment"]
    if not investment:
        return [], []
    price_map = {
        sid: (p.price, p.price_date) for sid, p in repo.latest_prices().items()
    }
    multipliers = repo.security_multipliers()   # ADR-093: bond/option scaling
    # Aggregate unrealized gain per security across all investment accounts,
    # converting each holding to the display currency first (ADR-055 — a USD
    # holding's gain must not be par-added into a GBP total or mislabelled).
    agg: dict[int, dict] = {}
    for acct in investment:
        txns = repo.list_transactions_for_account(acct.id)
        view = compute_holdings_view(txns, acct.opening_balance, price_map, multipliers)
        for h in view.holdings:
            if not h.priced or h.unrealized_gain is None:
                continue
            gain, _ = repo.convert_amount(
                h.unrealized_gain, from_ccy=acct.currency,
                to_ccy=display_ccy, on_date=today_iso,
            )
            if gain is None:
                continue                         # no FX rate — exclude
            cost, _ = repo.convert_amount(
                h.cost_basis, from_ccy=acct.currency,
                to_ccy=display_ccy, on_date=today_iso,
            )
            slot = agg.setdefault(
                h.security_id,
                {"name": h.name, "symbol": h.symbol,
                 "gain": _ZERO, "cost": _ZERO},
            )
            slot["gain"] += gain
            slot["cost"] += (cost or _ZERO)

    perfs = [
        HoldingPerf(
            security_id=sid, name=v["name"], symbol=v["symbol"], gain=v["gain"],
            pct=(float(v["gain"] / v["cost"]) if v["cost"] else None),
        )
        for sid, v in agg.items()
    ]
    gains = sorted([p for p in perfs if p.gain > 0], key=lambda p: p.gain, reverse=True)
    losses = sorted([p for p in perfs if p.gain < 0], key=lambda p: p.gain)
    return gains[:top_n], losses[:top_n]
