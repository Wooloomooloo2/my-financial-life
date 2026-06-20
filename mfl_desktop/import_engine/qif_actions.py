# Investment action classification — the single source of truth shared by the
# QIF parser (which turns each action into a cash sign at import time) and the
# holdings engine (which turns each action into a share-direction at compute
# time). Both must agree on what "Buy" / "ReinvDiv" / "ShrsIn" / … mean, so the
# sets live here rather than being duplicated. ADR-043 (import) + ADR-044
# (holdings).
#
# All comparisons are done on action.strip().lower().

from __future__ import annotations

# Actions whose security leg ADDS shares (cost out / no net cash for reinvests).
SHARE_IN_ACTIONS = {"buy", "buyx", "shrsin", "reinvdiv", "reinvlg", "reinvsh",
                    "reinvint", "reinvmd", "cvrshrt"}

# Actions whose security leg REMOVES shares.
SHARE_OUT_ACTIONS = {"sell", "sellx", "shrsout", "shtsell"}

# In-kind share TRANSFERS between accounts (no cash, no sale). These are a
# subset of the share-in/out sets — a ShrsIn still adds shares and a ShrsOut
# still removes them — but the holdings engine must NOT treat a ShrsOut as a
# realizing disposal (proceeds are $0, so it would book the whole cost basis as
# a phantom loss) and should carry the cost basis across to the matching ShrsIn
# rather than re-acquiring the shares for free. ADR-053.
SHARE_TRANSFER_ACTIONS = {"shrsin", "shrsout"}

# Cash distributions / income received into the account (positive cash in).
CASH_IN_ACTIONS = {"div", "divx", "intinc", "intincx", "miscinc", "miscincx",
                   "cglong", "cglongx", "cgshort", "cgshortx", "cgmid",
                   "cgmidx", "rtrncap", "rtrncapx"}

# Actions that move shares (or nothing) but no cash — share transfers, the
# reinvest leg (dividend already counted as cash in via the paired Div row, or
# netted), stock splits, and reminders.
ZERO_CASH_ACTIONS = {"shrsin", "shrsout", "reinvdiv", "reinvlg", "reinvsh",
                     "reinvint", "reinvmd", "stksplit", "stocksplit",
                     "reminder"}

# Stock-split actions — quantity is a ratio/added-share count, handled
# specially by the holdings engine.
SPLIT_ACTIONS = {"stksplit", "stocksplit"}

# Reinvested-distribution actions: a share-in whose cost basis IS the
# distribution being reinvested (zero net cash). Economically this is
# dividend/income, captured by the returns report (ADR-046) as income =
# price × qty, since these rows carry no separate cash leg.
REINVEST_ACTIONS = {"reinvdiv", "reinvlg", "reinvsh", "reinvint", "reinvmd"}

# Cash EXPENSE actions — fees / margin interest paid out of the account
# (negative cash). The counterpart to CASH_IN_ACTIONS. ADR-086.
CASH_EXPENSE_ACTIONS = {"miscexp", "miscexpx", "margint", "margintx"}


def _norm(action: str | None) -> str:
    return (action or "").strip().lower()


def is_share_in(action: str | None) -> bool:
    return _norm(action) in SHARE_IN_ACTIONS


def is_share_out(action: str | None) -> bool:
    return _norm(action) in SHARE_OUT_ACTIONS


def is_split(action: str | None) -> bool:
    return _norm(action) in SPLIT_ACTIONS


def is_share_transfer(action: str | None) -> bool:
    """True for an in-kind share transfer between accounts (ShrsIn / ShrsOut).
    The holdings engine (ADR-053) treats these as custodian moves — no realized
    gain on the way out, cost basis carried to the matching leg — not sales."""
    return _norm(action) in SHARE_TRANSFER_ACTIONS


def is_income(action: str | None) -> bool:
    """True if the action is a cash distribution / income received into the
    account (dividend, interest, cap-gain distribution, return of capital).
    Used by the returns report (ADR-046) to bucket dividend/income flows.
    Note: a *reinvested* dividend (``ReinvDiv`` etc.) is a share-in with zero
    cash, so it is NOT in this set — the returns engine handles the reinvest
    leg's income value separately (price × qty) to avoid double-counting."""
    return _norm(action) in CASH_IN_ACTIONS


def is_reinvest(action: str | None) -> bool:
    """True if the action reinvests a distribution into new shares (zero net
    cash; the reinvested distribution is both income and cost basis)."""
    return _norm(action) in REINVEST_ACTIONS


def is_categorisable(action: str | None) -> bool:
    """True if a user may assign a ledger category to this investment action:
    the genuine cash income/expense flows — distributions (``CASH_IN_ACTIONS``),
    fees/margin interest (``CASH_EXPENSE_ACTIONS``), the manual ``Cash`` in/out
    (all ADR-086) — **and reinvested distributions** (``REINVEST_ACTIONS``,
    ADR-089). A reinvest carries **zero cash**, so categorising it can never
    inject anything into the strict-cashflow reports (they sum signed amount,
    which is 0 here); it only lets the owner tag a DRIP as e.g. *Dividend
    Income* so the Income Over Time report can surface its share-valued income
    (qty × price) when its "include reinvested dividends" toggle is on.

    Still **False** for portfolio moves (buy/sell/share-transfer/split) —
    categorising those would inject their trade amounts into the cashflow
    reports."""
    a = _norm(action)
    return (
        a in CASH_IN_ACTIONS
        or a in CASH_EXPENSE_ACTIONS
        or a in REINVEST_ACTIONS
        or a == "cash"
    )


def affects_shares(action: str | None) -> bool:
    """True if the action changes the share position of its security
    (a buy, sell, transfer in/out, reinvest, or split)."""
    a = _norm(action)
    return a in SHARE_IN_ACTIONS or a in SHARE_OUT_ACTIONS or a in SPLIT_ACTIONS
