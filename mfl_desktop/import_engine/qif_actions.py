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


def _norm(action: str | None) -> str:
    return (action or "").strip().lower()


def is_share_in(action: str | None) -> bool:
    return _norm(action) in SHARE_IN_ACTIONS


def is_share_out(action: str | None) -> bool:
    return _norm(action) in SHARE_OUT_ACTIONS


def is_split(action: str | None) -> bool:
    return _norm(action) in SPLIT_ACTIONS


def affects_shares(action: str | None) -> bool:
    """True if the action changes the share position of its security
    (a buy, sell, transfer in/out, reinvest, or split)."""
    a = _norm(action)
    return a in SHARE_IN_ACTIONS or a in SHARE_OUT_ACTIONS or a in SPLIT_ACTIONS
