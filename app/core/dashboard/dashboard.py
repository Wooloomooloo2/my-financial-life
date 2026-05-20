# ===========================================================================
# app/core/dashboard/dashboard.py
#
# Data layer for the dashboard.
#
# Computes:
#   - Net worth (assets minus liabilities across all accounts)
#   - Period income and expenditure (date-filtered, opening balances excluded)
#   - Spending by category (for the chart)
#   - Recent transactions across all accounts
#   - Account balance summary grouped by family
# ===========================================================================

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from app.data.store import store
from app.core.ontology.namespaces import (
    DATA_GRAPH, ONTOLOGY_GRAPH,
    MRL, MFLX, MFL,
    MRL_ACCOUNT_NAME, MRL_ACCOUNT_CURRENCY, MRL_IS_LIABILITY,
    MFL_TRANSACTION, MFL_ON_ACCOUNT, MFL_AMOUNT,
    MFL_TRANSACTION_TYPE, MFL_PAYEE_RAW, MFL_CATEGORY,
    MFLX_TYPE_CREDIT, MFLX_TYPE_DEBIT,
)
from app.core.transactions.transactions import (
    _load_category_labels,
    _load_category_families,
    _fmt_date,
    INCOME_TOP, EXPENSE_TOP, UNCAT_IRI,
)
from app.core.accounts.accounts import (
    get_all_accounts,
    _get_currency_details,
)
from app.core.accounts.person import PERSON_IRI, get_person

logger = logging.getLogger(__name__)

XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"

# ---------------------------------------------------------------------------
# Timescale definitions
# ---------------------------------------------------------------------------

TIMESCALE_OPTIONS = [
    ("MTD",         "This month"),
    ("LAST_MONTH",  "Last month"),
    ("YTD",         "This year"),
    ("ROLLING_3M",  "3 months"),
    ("ROLLING_6M",  "6 months"),
    ("ROLLING_12M", "12 months"),
]

TIMESCALE_LABELS = {k: v for k, v in TIMESCALE_OPTIONS}

DEFAULT_TIMESCALE = "MTD"


def _subtract_months(d: date, months: int) -> date:
    """Subtract N months from a date, clamping to last day if needed."""
    month = d.month - months
    year  = d.year
    while month <= 0:
        month += 12
        year  -= 1
    last_day = calendar.monthrange(year, month)[1]
    return d.replace(year=year, month=month, day=min(d.day, last_day))


def get_date_range(timescale: str) -> tuple[date, date]:
    """Return (start_date, end_date) for the given timescale code."""
    today = date.today()
    if timescale == "MTD":
        return today.replace(day=1), today
    elif timescale == "LAST_MONTH":
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    elif timescale == "YTD":
        return today.replace(month=1, day=1), today
    elif timescale == "ROLLING_3M":
        return _subtract_months(today, 3), today
    elif timescale == "ROLLING_6M":
        return _subtract_months(today, 6), today
    elif timescale == "ROLLING_12M":
        return _subtract_months(today, 12), today
    else:
        return today.replace(day=1), today   # default MTD


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CategorySpend:
    label:  str
    amount: Decimal
    color:  str   # CSS colour for Chart.js


@dataclass
class RecentTransaction:
    date_display:    str
    payee:           str
    account_name:    str
    category_label:  str
    amount_display:  str
    amount_color:    str


@dataclass
class DashboardData:
    # Net worth
    net_worth:         Decimal
    total_assets:      Decimal
    total_liabilities: Decimal
    # Period metrics
    income:            Decimal
    expenditure:       Decimal
    net_cashflow:      Decimal
    # Timescale
    timescale:         str
    timescale_label:   str
    period_start:      str
    period_end:        str
    # Chart
    category_spending: list[CategorySpend]
    # Recent transactions
    recent:            list[RecentTransaction]
    # Base currency symbol
    currency_symbol:   str = "£"


# ---------------------------------------------------------------------------
# Chart colours — 12 colours cycling for up to 12 categories
# ---------------------------------------------------------------------------

_CHART_COLORS = [
    "#6366f1", "#8b5cf6", "#ec4899", "#f59e0b",
    "#10b981", "#3b82f6", "#ef4444", "#14b8a6",
    "#f97316", "#84cc16", "#06b6d4", "#a855f7",
]


# ---------------------------------------------------------------------------
# Net worth
# ---------------------------------------------------------------------------

def _get_net_worth() -> tuple[Decimal, Decimal, Decimal]:
    """
    Return (net_worth, total_assets, total_liabilities).
    Reuses the balance logic from get_all_accounts().
    """
    grouped    = get_all_accounts()
    assets     = Decimal("0")
    liabilities = Decimal("0")

    for accounts in grouped.values():
        for account in accounts:
            if account.is_liability:
                liabilities += account.balance
            else:
                assets += account.balance

    return assets - liabilities, assets, liabilities


# ---------------------------------------------------------------------------
# Period totals (income and expenditure)
# Opening Balance transactions are excluded to avoid distorting income.
# Transfer transactions are excluded (neither income nor expense).
# ---------------------------------------------------------------------------

def _get_period_total(
    tx_type_iri: str,
    start: date,
    end:   date,
) -> Decimal:
    """Sum transaction amounts for a given type within the date range."""
    sparql = f"""
        SELECT (COALESCE(SUM(?amount), 0) AS ?total)
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL}transactionDate>          ?date ;
                    <{MFL_TRANSACTION_TYPE.value}>  <{tx_type_iri}> ;
                    <{MFL_AMOUNT.value}>             ?amount .
                OPTIONAL {{ ?tx <{MFL_PAYEE_RAW.value}> ?payeeRaw }}
                FILTER(
                    ?date >= "{start.isoformat()}"^^<{XSD_DATE}> &&
                    ?date <= "{end.isoformat()}"^^<{XSD_DATE}> &&
                    (!BOUND(?payeeRaw) || STR(?payeeRaw) != "Opening Balance")
                )
            }}
        }}
    """
    for row in store.query(sparql):
        val = row["total"]
        if val is not None:
            return Decimal(str(val.value))
    return Decimal("0")


# ---------------------------------------------------------------------------
# Category spending (for the chart)
# ---------------------------------------------------------------------------

def _get_category_spending(start: date, end: date) -> list[CategorySpend]:
    """
    Return debit spending grouped by category for the period.
    Excludes Opening Balance and Transfer transactions.
    Uncategorised transactions are grouped under a single bucket.
    """
    sparql = f"""
        SELECT ?category (SUM(?amount) AS ?total)
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL}transactionDate>         ?date ;
                    <{MFL_TRANSACTION_TYPE.value}> <{MFLX_TYPE_DEBIT.value}> ;
                    <{MFL_AMOUNT.value}>            ?amount .
                OPTIONAL {{ ?tx <{MFL_CATEGORY.value}>   ?category }}
                OPTIONAL {{ ?tx <{MFL_PAYEE_RAW.value}>  ?payeeRaw }}
                FILTER(
                    ?date >= "{start.isoformat()}"^^<{XSD_DATE}> &&
                    ?date <= "{end.isoformat()}"^^<{XSD_DATE}> &&
                    (!BOUND(?payeeRaw) || STR(?payeeRaw) != "Opening Balance")
                )
            }}
        }}
        GROUP BY ?category
        ORDER BY DESC(?total)
    """
    cat_labels = _load_category_labels()
    results: list[CategorySpend] = []

    for i, row in enumerate(store.query(sparql)):
        cat_iri = row["category"].value if row["category"] else ""
        total   = Decimal(str(row["total"].value)) if row["total"] else Decimal("0")
        label   = cat_labels.get(cat_iri, "Uncategorised") if cat_iri else "Uncategorised"
        color   = _CHART_COLORS[i % len(_CHART_COLORS)]
        results.append(CategorySpend(label=label, amount=total, color=color))

    return results


# ---------------------------------------------------------------------------
# Recent transactions (across all accounts)
# ---------------------------------------------------------------------------

def _get_recent_transactions(
    currency_symbol: str,
    limit: int = 8,
) -> list[RecentTransaction]:
    sparql = f"""
        SELECT ?tx ?date ?amount ?txType ?payeeRaw ?category ?accountName
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL}transactionDate>          ?date ;
                    <{MFL_AMOUNT.value}>             ?amount ;
                    <{MFL_TRANSACTION_TYPE.value}>   ?txType ;
                    <{MFL_ON_ACCOUNT.value}>         ?account .
                ?account <{MRL_ACCOUNT_NAME.value}> ?accountName .
                OPTIONAL {{ ?tx <{MFL_PAYEE_RAW.value}> ?payeeRaw }}
                OPTIONAL {{ ?tx <{MFL_CATEGORY.value}>  ?category }}
            }}
        }}
        ORDER BY DESC(?date) DESC(STR(?tx))
        LIMIT {limit}
    """
    cat_labels = _load_category_labels()
    recent: list[RecentTransaction] = []

    for row in store.query(sparql):
        date_iso     = row["date"].value
        amount       = Decimal(str(row["amount"].value))
        tx_type      = row["txType"].value.split("#")[-1]
        payee_raw    = row["payeeRaw"].value if row["payeeRaw"] else "—"
        cat_iri      = row["category"].value if row["category"] else ""
        account_name = row["accountName"].value if row["accountName"] else ""
        cat_label    = cat_labels.get(cat_iri, "Uncategorised") if cat_iri else "—"

        is_debit = "Debit" in tx_type
        if is_debit:
            amt_str   = f"−{currency_symbol}{amount:,.2f}"
            amt_color = "text-error"
        else:
            amt_str   = f"{currency_symbol}{amount:,.2f}"
            amt_color = "text-base-content"

        recent.append(RecentTransaction(
            date_display=_fmt_date(date_iso),
            payee=payee_raw,
            account_name=account_name,
            category_label=cat_label,
            amount_display=amt_str,
            amount_color=amt_color,
        ))

    return recent


# ---------------------------------------------------------------------------
# Base currency symbol
# ---------------------------------------------------------------------------

def _get_base_currency_symbol() -> str:
    """Return the symbol for the person's base currency, defaulting to £."""
    person = get_person()
    if not person or not person.base_currency_iri:
        return "£"
    code, symbol = _get_currency_details(person.base_currency_iri)
    return symbol or code or "£"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_dashboard_data(timescale: str = DEFAULT_TIMESCALE) -> DashboardData:
    """
    Compute all data needed to render the dashboard for the given timescale.
    """
    if timescale not in TIMESCALE_LABELS:
        timescale = DEFAULT_TIMESCALE

    start, end = get_date_range(timescale)
    sym        = _get_base_currency_symbol()

    net_worth, assets, liabilities = _get_net_worth()
    income      = _get_period_total(MFLX_TYPE_CREDIT.value, start, end)
    expenditure = _get_period_total(MFLX_TYPE_DEBIT.value,  start, end)
    cat_spend   = _get_category_spending(start, end)
    recent      = _get_recent_transactions(sym)

    return DashboardData(
        net_worth=net_worth,
        total_assets=assets,
        total_liabilities=liabilities,
        income=income,
        expenditure=expenditure,
        net_cashflow=income - expenditure,
        timescale=timescale,
        timescale_label=TIMESCALE_LABELS[timescale],
        period_start=f"{start.day} {start.strftime('%b %Y')}",
        period_end=f"{end.day} {end.strftime('%b %Y')}",
        category_spending=cat_spend,
        recent=recent,
        currency_symbol=sym,
    )
