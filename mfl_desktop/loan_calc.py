"""Loan amortization — pure Python, no Qt, no SQL (ADR-095).

Mirrors budget_calc / goal_calc: the Repository hands in a loan's terms; this
module produces the amortization **schedule** (one row per monthly payment, each
split into interest + principal) plus summary totals, and the helpers the dialog
needs (the required payment for a term, the per-period split for one balance).

The engine replays the loan from its **current principal** forward, one monthly
payment at a time:

    interest  = balance × monthly_rate
    principal = payment + extra − interest
    balance  -= principal           (final payment trimmed to clear the balance)

It works in float internally — compound-rate conversions need fractional powers —
and quantizes every money figure to 2-dp Decimal on the way out. This is a
*forecast*; the actual posted payments are exact Decimal in the ledger.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Optional

_ZERO = Decimal("0.00")
COMPOUNDING = ("daily", "monthly", "annually")


def _money(x: float) -> Decimal:
    return Decimal(str(round(float(x), 2))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def monthly_rate(annual_rate_pct: float, compounding: str) -> float:
    """Convert an APR (%) to the per-month periodic rate for monthly payments.

    - ``monthly``   — simple APR / 12 (the consumer-loan default);
    - ``annually``  — effective monthly of annual compounding: (1+a)^(1/12) − 1;
    - ``daily``     — effective monthly of daily compounding:
                      (1 + a/365)^(365/12) − 1.
    """
    a = float(annual_rate_pct) / 100.0
    if a == 0:
        return 0.0
    if compounding == "monthly":
        return a / 12.0
    if compounding == "annually":
        return (1.0 + a) ** (1.0 / 12.0) - 1.0
    if compounding == "daily":
        return (1.0 + a / 365.0) ** (365.0 / 12.0) - 1.0
    raise ValueError(f"Unknown compounding {compounding!r}; expected {COMPOUNDING}.")


def required_payment(
    principal: Decimal, annual_rate_pct: float, compounding: str, term_months: int,
) -> Decimal:
    """The level monthly payment that clears ``principal`` over ``term_months``
    — the standard annuity formula P·r / (1 − (1+r)⁻ⁿ). Zero-rate loans split
    evenly. Returns a 2-dp Decimal."""
    if term_months <= 0:
        raise ValueError("Term must be a positive number of months.")
    p = float(principal)
    r = monthly_rate(annual_rate_pct, compounding)
    if r == 0:
        # Even split rounded up so the final (smaller) payment clears it.
        return Decimal(str(p / term_months)).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING,
        )
    pay = p * r / (1.0 - (1.0 + r) ** (-term_months))
    # Round the level payment UP to the cent (as lenders do) so cent-rounding
    # can't leave a residual that needs an extra token payment past the term.
    return Decimal(str(pay)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def split_payment(
    balance: Decimal, annual_rate_pct: float, compounding: str, payment: Decimal,
    extra: Decimal = _ZERO,
) -> tuple[Decimal, Decimal]:
    """Split one ``payment`` (+ ``extra``) against ``balance`` into
    (interest, principal) at the current rate. The principal is trimmed so it
    never exceeds the outstanding balance (the final payment). Used by the
    Repository's split-aware posting and the dialog preview."""
    r = monthly_rate(annual_rate_pct, compounding)
    bal = float(balance)
    interest = bal * r
    principal = float(payment) + float(extra) - interest
    if principal < 0:
        principal = 0.0
    if principal > bal:
        principal = bal
    return _money(interest), _money(principal)


def _add_month(d: date, payment_day: int) -> date:
    """The next monthly payment date on ``payment_day`` (clamped to month len)."""
    y = d.year + (1 if d.month == 12 else 0)
    m = 1 if d.month == 12 else d.month + 1
    day = min(payment_day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _first_payment_date(start_date: str, payment_day: int) -> date:
    """The first scheduled payment on/after ``start_date`` falling on
    ``payment_day``."""
    start = date.fromisoformat(start_date)
    day = min(payment_day, calendar.monthrange(start.year, start.month)[1])
    first = date(start.year, start.month, day)
    if first < start:
        first = _add_month(first, payment_day)
    return first


@dataclass(frozen=True)
class AmortRow:
    number: int          # 1-based payment number
    date: str            # 'YYYY-MM-DD'
    payment: Decimal     # total cash paid this period (incl. extra)
    interest: Decimal
    principal: Decimal
    extra: Decimal
    balance: Decimal     # remaining principal after this payment


@dataclass(frozen=True)
class AmortSchedule:
    rows: list[AmortRow]
    monthly_payment: Decimal   # the scheduled level payment (excl. extra)
    extra_payment: Decimal
    total_interest: Decimal
    total_paid: Decimal
    n_payments: int
    payoff_date: Optional[str]
    negative_amortization: bool   # payment never covers interest → no payoff


def compute_schedule(
    *,
    current_principal: Decimal,
    annual_rate_pct: float,
    compounding: str,
    payment: Decimal,
    start_date: str,
    payment_day: int,
    extra_payment: Decimal = _ZERO,
    max_months: int = 1200,
) -> AmortSchedule:
    """Amortize ``current_principal`` forward at ``payment`` (+ ``extra``) per
    month from ``start_date``. Pure; the rows + totals back the schedule table,
    the balance chart, and the budget pay-down-goal required-monthly.

    If the payment can never cover the interest the schedule stops after one row
    with ``negative_amortization=True`` (the UI warns rather than the engine
    looping to ``max_months``)."""
    r = monthly_rate(annual_rate_pct, compounding)
    bal = float(current_principal)
    pay = float(payment)
    extra = float(extra_payment or _ZERO)
    rows: list[AmortRow] = []
    total_interest = 0.0
    total_paid = 0.0
    neg_amort = False

    if bal <= 0:
        return AmortSchedule(
            rows=[], monthly_payment=_money(pay), extra_payment=_money(extra),
            total_interest=_ZERO, total_paid=_ZERO, n_payments=0,
            payoff_date=None, negative_amortization=False,
        )

    d = _first_payment_date(start_date, payment_day)
    n = 0
    while bal > 0.005 and n < max_months:
        interest = bal * r
        # A payment that doesn't even cover the interest never amortizes.
        if (pay + extra) <= interest + 1e-9 and r > 0:
            neg_amort = True
            rows.append(AmortRow(
                number=n + 1, date=d.isoformat(), payment=_money(pay + extra),
                interest=_money(interest), principal=_ZERO, extra=_money(extra),
                balance=_money(bal),
            ))
            break
        n += 1
        principal = pay + extra - interest
        this_extra = extra
        if principal >= bal:                 # final payment — trim to clear
            principal = bal
            this_extra = max(0.0, min(extra, principal))
            cash = interest + principal
        else:
            cash = pay + extra
        bal -= principal
        total_interest += interest
        total_paid += cash
        rows.append(AmortRow(
            number=n, date=d.isoformat(), payment=_money(cash),
            interest=_money(interest), principal=_money(principal),
            extra=_money(this_extra), balance=_money(max(bal, 0.0)),
        ))
        d = _add_month(d, payment_day)

    return AmortSchedule(
        rows=rows,
        monthly_payment=_money(pay),
        extra_payment=_money(extra),
        total_interest=_money(total_interest),
        total_paid=_money(total_paid),
        n_payments=0 if neg_amort else len(rows),
        payoff_date=(rows[-1].date if rows and not neg_amort else None),
        negative_amortization=neg_amort,
    )
