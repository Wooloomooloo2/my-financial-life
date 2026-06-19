"""Count-aware cross-source duplicate matching for imports (ADR-085).

The exact-``import_hash`` check only catches a re-import of the *same* file —
the same real transaction arriving from a *different* source (OFX FITID vs a
Banktivity composite hash vs a bank feed's transactionId) carries a different
hash and slips through. This module fuzzy-matches an import batch against the
transactions already in the account so those cross-source duplicates can be
surfaced for review.

The load-bearing property is **multiplicity**: people legitimately spend the
same amount at the same payee several times in a few days (£8.90 ScotRail on
Mon/Tue/Thu). An existence test ("is there a match? skip it") would wrongly
drop genuine repeats. So we **pair** import rows to existing rows with each
existing row claimed at most once (``transfer_reconcile.greedy_pair``) — if the
register holds 1 copy and the import has 3, exactly 1 is a duplicate and 2 are
new.

Pure module — no Qt, no SQL. The Repository fetches the existing rows and the
import service shapes the import rows; this just does the matching, so it's
unit-testable without a fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from mfl_desktop.transfer_reconcile import (
    greedy_pair, score_candidate, _payee_tokens,
)

# Default date tolerance for pairing the *same* charge across sources — a
# posted-date can drift a day or two between a bank's OFX and a Banktivity
# export. NOT the window that governs repeats (consumption handles those).
DEFAULT_WINDOW_DAYS = 2


@dataclass(frozen=True)
class ImportRow:
    """One incoming row, as seen by the matcher."""
    index: int                 # position in the staged batch (the claim key)
    date_iso: str
    amount_pence: int          # signed (negative = money out)
    payee_raw: str


@dataclass(frozen=True)
class ExistingRow:
    """One transaction already in the account, as a match target."""
    id: int
    date_iso: str
    amount_pence: int          # signed
    payee_name: str
    is_manual: bool            # import_hash IS NULL (a hand-typed placeholder)


@dataclass(frozen=True)
class DedupeMatch:
    """The outcome for one matched import row."""
    existing_id: int
    is_manual: bool            # manual → confirm == merge; imported → confirm == skip
    strength: str              # 'strong' | 'weak' (drives the default review tick)
    existing_date: str
    existing_payee: str


@dataclass(frozen=True)
class _Pair:
    index: int
    existing_id: int
    score: int
    days_apart: int
    payee_overlap: bool
    is_manual: bool
    existing_date: str
    existing_payee: str


def _days_apart(a_iso: str, b_iso: str) -> Optional[int]:
    try:
        return abs((date.fromisoformat(a_iso) - date.fromisoformat(b_iso)).days)
    except ValueError:
        return None


def match_duplicates(
    import_rows: list[ImportRow],
    existing_rows: list[ExistingRow],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[int, DedupeMatch]:
    """Pair import rows to existing rows (each existing row claimed once).

    Returns ``{import_index: DedupeMatch}`` for the import rows that matched;
    unmatched import rows are absent (they're genuinely new). Candidate pairs
    require an **exact signed-amount** match and a date within ``±window_days``;
    they're ordered same-day / same-payee first so the greedy walk claims the
    most plausible pairings before the window stretches.
    """
    # Bucket existing rows by exact signed amount — the hard filter — so each
    # import row only considers same-amount candidates.
    by_amount: dict[int, list[ExistingRow]] = {}
    for e in existing_rows:
        by_amount.setdefault(e.amount_pence, []).append(e)

    pairs: list[_Pair] = []
    for imp in import_rows:
        for e in by_amount.get(imp.amount_pence, ()):  # exact amount only
            d = _days_apart(imp.date_iso, e.date_iso)
            if d is None or d > window_days:
                continue
            overlap = bool(_payee_tokens(imp.payee_raw) & _payee_tokens(e.payee_name))
            score = score_candidate(
                days_apart=d, amount_mismatch_pct=0.0, currencies_match=True,
                src_payee=imp.payee_raw, tgt_payee=e.payee_name,
            )
            pairs.append(_Pair(
                index=imp.index, existing_id=e.id, score=score, days_apart=d,
                payee_overlap=overlap, is_manual=e.is_manual,
                existing_date=e.date_iso, existing_payee=e.payee_name,
            ))

    kept = greedy_pair(
        pairs,
        source_key=lambda p: p.index,
        target_key=lambda p: p.existing_id,
        score_key=lambda p: p.score,
    )

    out: dict[int, DedupeMatch] = {}
    for p in kept:
        # Cross-source payee text is unreliable, so same-day is the dependable
        # "strong" signal; a manual placeholder is always strong (its match is
        # the established merge-on-confirm behaviour).
        strong = p.is_manual or p.days_apart == 0 or p.payee_overlap
        out[p.index] = DedupeMatch(
            existing_id=p.existing_id, is_manual=p.is_manual,
            strength="strong" if strong else "weak",
            existing_date=p.existing_date, existing_payee=p.existing_payee,
        )
    return out
