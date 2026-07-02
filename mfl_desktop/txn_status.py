"""Transaction status — the single source of truth for the confidence ladder
(ADR-130).

A transaction climbs a monotonic ladder of increasing corroboration:

    pending    – entered when the money is spent; not yet seen at the bank
    cleared    – you saw it post (bank-app alert); your eyeball only
    matched    – a downloaded/OFX bank record matched (or added) it; bank-confirmed
    reconciled – tied to a statement and locked

Stored **lowercase** in ``txn.status`` (migration 0033), matching
``statement.status`` (``open`` / ``reconciled``). Every UI list / dialog /
delegate imports :data:`STATUSES` and the display metadata from here instead of
re-declaring the tuple — it used to be copy-pasted in ~8 modules.

The stored value is the lowercase key; the user-facing text is the ``label``.
Combos must show labels but read/write keys (see :class:`StatusDelegate`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PENDING = "pending"
CLEARED = "cleared"
MATCHED = "matched"
RECONCILED = "reconciled"

# Ladder order — also the combo / cycle order (low → high confidence).
STATUSES: tuple[str, ...] = (PENDING, CLEARED, MATCHED, RECONCILED)


@dataclass(frozen=True)
class StatusMeta:
    key: str
    label: str      # user-facing, Title-case
    blurb: str      # tooltip / legend one-liner
    token: str      # design-token name for the register swatch/chip colour
    locked: bool    # reconciled → edits require reopening the statement (ADR-040)


_META: dict[str, StatusMeta] = {
    PENDING: StatusMeta(
        PENDING, "Pending",
        "Entered when spent — not yet seen at the bank.",
        "muted", False,
    ),
    CLEARED: StatusMeta(
        CLEARED, "Cleared",
        "You saw it post at the bank (not yet download-confirmed).",
        "warning", False,
    ),
    MATCHED: StatusMeta(
        MATCHED, "Matched",
        "A downloaded bank record matched it — bank-confirmed.",
        "accent", False,
    ),
    RECONCILED: StatusMeta(
        RECONCILED, "Reconciled",
        "Tied to a statement and locked.",
        "positive", True,
    ),
}


def is_valid(status: str) -> bool:
    """True if ``status`` is one of the four ladder keys."""
    return status in _META


def meta(status: str) -> Optional[StatusMeta]:
    return _META.get(status)


def label(status: str) -> str:
    """User-facing text for a stored status key (falls back to the key)."""
    m = _META.get(status)
    return m.label if m else status


def key_for_label(text: str) -> Optional[str]:
    """Reverse a user-facing label back to its stored key (case-insensitive)."""
    t = (text or "").strip().lower()
    for m in _META.values():
        if m.label.lower() == t or m.key == t:
            return m.key
    return None


def is_locked(status: str) -> bool:
    """True for ``reconciled`` — its rows are statement-locked (ADR-040)."""
    m = _META.get(status)
    return bool(m and m.locked)


def labels() -> list[str]:
    """Ladder-ordered labels, for combo boxes."""
    return [_META[k].label for k in STATUSES]
