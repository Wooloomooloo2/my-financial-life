"""Net worth over time (ADR-121).

A time series of total assets, debts, and net worth in a chosen display
currency. Computed-not-stored like everything else here: at each sample date
every account is valued — **cash balance** (a transaction replay, ``opening +
Σ amount ≤ date``, inclusive per ADR-040) **plus holdings market value**
(``holdings.compute_value_history``; zero for accounts with no security txns,
so one path values every family) — then FX-converted to the display currency
*at that date* (ADR-055: convert before summing, exclude an account at any date
it can't convert rather than par-adding) and bucketed into asset / debt
families.

Pure of Qt. ``gather_net_worth_history`` is repo-coupled but UI-free (the
ADR-075 ``gather_*`` pattern); the family→asset/debt classification is passed
in by the caller (the Net Worth window's ``_FAMILY_VIEW``) so it stays
single-sourced and this module imports no UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from mfl_desktop.holdings import compute_value_history


@dataclass(frozen=True)
class NetWorthPoint:
    """One sample on the net-worth-over-time series. Family maps hold the
    display-currency value contributed by each family on ``date`` — assets as
    their signed value, debts as a positive magnitude owed."""
    date: str                            # 'YYYY-MM-DD'
    family_assets: dict[str, Decimal]
    family_debts: dict[str, Decimal]
    asset_total: Decimal
    debt_total: Decimal
    net: Decimal


@dataclass(frozen=True)
class NetWorthHistory:
    points: list[NetWorthPoint]
    display_ccy: str
    excluded_any: bool      # ≥1 account dropped at ≥1 date (no rate that far back)
    fallback_used: bool     # ≥1 converted value used a nearest-prior FX rate


def month_end_samples(start: date, end: date) -> list[date]:
    """``[start]`` + every month-end strictly between + ``[end]``. Mirrors the
    Returns report's sampling so the two investment views line up."""
    if end <= start:
        return [start] if end == start else [start, end]
    out: list[date] = [start]
    y, m = start.year, start.month
    while True:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        eom = date(ny, nm, 1) - timedelta(days=1)
        if eom >= end:
            break
        if eom > start:
            out.append(eom)
        y, m = ny, nm
    out.append(end)
    return out


def cash_balance_at_dates(
    txns, opening_balance: Decimal, samples_iso: list[str],
) -> list[Decimal]:
    """Recorded cash balance at the end of each sample date: ``opening +
    Σ(amount)`` for txns posted on or before that date. ``samples_iso`` must be
    ascending. Single pass — walk txns in date order, advancing a running total
    as each sample boundary is crossed (the multi-date generalisation of
    ``Repository.balance_as_of``; inclusive ``<= date`` per ADR-040)."""
    dated = sorted(
        ((t.posted_date, t.amount) for t in txns if t.posted_date),
        key=lambda x: x[0],
    )
    out: list[Decimal] = []
    running = Decimal(opening_balance)
    i, n = 0, len(dated)
    for s in samples_iso:
        while i < n and dated[i][0] <= s:
            running += Decimal(dated[i][1])
            i += 1
        out.append(running)
    return out


def gather_net_worth_history(
    repo,
    *,
    sample_dates: list,
    display_ccy: str,
    family_kinds: dict[str, str],
    include_closed: bool = False,
) -> NetWorthHistory:
    """Build the net-worth-over-time series. ``family_kinds`` maps each account
    family to ``"asset"`` or ``"debt"`` (families absent from the map are
    skipped, mirroring the point-in-time screen). ``sample_dates`` are date
    objects or ISO strings; the result's points are in ascending date order."""
    samples_iso = sorted({
        d.isoformat() if isinstance(d, date) else str(d) for d in sample_dates
    })
    n = len(samples_iso)
    fam_assets: list[dict[str, Decimal]] = [{} for _ in range(n)]
    fam_debts: list[dict[str, Decimal]] = [{} for _ in range(n)]
    excluded_any = False
    fallback_used = False

    accounts = repo.list_accounts(include_closed=include_closed)
    multipliers = repo.security_multipliers()   # ADR-093 bond/option scaling

    # Memoise the FX *rate* per (from-currency, date) so the 6-step lookup runs
    # once per pair, not once per account. Same-currency short-circuits.
    rate_cache: dict[tuple[str, str], tuple[Optional[Decimal], bool]] = {}

    def rate_for(ccy: str, on_date: str) -> tuple[Optional[Decimal], bool]:
        if ccy == display_ccy:
            return Decimal(1), False
        key = (ccy, on_date)
        if key not in rate_cache:
            r, _, fb = repo.get_fx_rate_nearest(on_date, ccy, display_ccy)
            rate_cache[key] = (r, fb)
        return rate_cache[key]

    for acct in accounts:
        kind = family_kinds.get(acct.family)
        if kind is None:
            continue
        txns = repo.list_transactions_for_account(acct.id)
        cash = cash_balance_at_dates(txns, acct.opening_balance, samples_iso)

        sec_ids = {t.security_id for t in txns if t.security_id is not None}
        holdings_by_date: dict[str, Decimal] = {}
        if sec_ids:
            pser = {
                sid: [(p.price_date, p.price) for p in repo.price_series(sid)]
                for sid in sec_ids
            }
            for vp in compute_value_history(txns, samples_iso, pser, multipliers):
                holdings_by_date[vp.date] = vp.market_value

        for i, s in enumerate(samples_iso):
            native = cash[i] + holdings_by_date.get(s, Decimal(0))
            rate, fb = rate_for(acct.currency, s)
            if rate is None:
                excluded_any = True
                continue
            fallback_used = fallback_used or fb
            conv = native * rate
            if kind == "asset":
                fam_assets[i][acct.family] = (
                    fam_assets[i].get(acct.family, Decimal(0)) + conv
                )
            else:
                # Liabilities are stored negative; show debt as positive owed.
                fam_debts[i][acct.family] = (
                    fam_debts[i].get(acct.family, Decimal(0)) - conv
                )

    points: list[NetWorthPoint] = []
    for i, s in enumerate(samples_iso):
        a_total = sum(fam_assets[i].values(), Decimal(0))
        d_total = sum(fam_debts[i].values(), Decimal(0))
        points.append(NetWorthPoint(
            date=s, family_assets=fam_assets[i], family_debts=fam_debts[i],
            asset_total=a_total, debt_total=d_total, net=a_total - d_total,
        ))
    return NetWorthHistory(
        points=points, display_ccy=display_ccy,
        excluded_any=excluded_any, fallback_used=fallback_used,
    )
