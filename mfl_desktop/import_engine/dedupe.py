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

# Confident date tolerance for pairing the *same* charge across sources. A
# posted-date routinely drifts a few days between when a charge is authorised
# and when it settles — the classic "posted Friday, cleared Monday" is already 3
# days, and a bank-holiday Monday pushes it to 4 (ADR-151). Matches inside this
# window default to skip/merge. NOT the window that governs repeats (consumption
# handles those).
DEFAULT_WINDOW_DAYS = 4

# Extended tolerance for a weak *possible* match (ADR-151): an exact-amount row
# whose payee also overlaps but which sits beyond the confident window — a
# settlement straggler. Surfaced for review as 'possible' (never a default
# skip), so a long gap is caught without silently dropping a genuine repeat.
POSSIBLE_WINDOW_DAYS = 10

# Amount-mismatch tier (ADR-130 Phase 3b): after exact-amount pairing, a still-
# unmatched download row can pair with a same-sign existing row whose amount is
# within this fraction AND whose payee overlaps — a likely mis-entry (e.g. an
# £8.99 typed for an £8.25 charge). Conservative + payee-gated + review-only, so
# it can't silently corrupt data. 0.0 disables the tier (default = off, so the
# ADR-085 dedupe behaviour is unchanged unless a caller opts in).
DEFAULT_FUZZY_AMOUNT_PCT = 0.20


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
    # Amount-mismatch tier (ADR-130 Phase 3b): a near-amount match where the
    # download's amount differs from the existing row's — a likely mis-entry.
    # Always a *weak* review item; the UI offers "adopt bank amount".
    amount_differs: bool = False
    existing_amount_pence: int = 0    # the existing row's signed amount


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
    near: bool = False          # matched only via the extended 'possible' window


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
    possible_window_days: int = 0,
    fuzzy_amount_pct: float = 0.0,
) -> dict[int, DedupeMatch]:
    """Pair import rows to existing rows (each existing row claimed once).

    Returns ``{import_index: DedupeMatch}`` for the import rows that matched;
    unmatched import rows are absent (they're genuinely new). Candidate pairs
    require an **exact signed-amount** match and a date within ``±window_days``;
    they're ordered same-day / same-payee first so the greedy walk claims the
    most plausible pairings before the window stretches.

    ``possible_window_days`` > ``window_days`` opts into an **extended tier**
    (ADR-151): an exact-amount row beyond the confident window but within this
    one, *and* whose payee overlaps, pairs as a **weak** match — a settlement
    straggler surfaced for review, never a default skip. 0 disables it.

    ``fuzzy_amount_pct`` > 0 runs a second, conservative **amount-mismatch**
    pass (ADR-130 Phase 3b) over the rows still unmatched: a same-sign existing
    row whose amount is within that fraction *and* whose payee overlaps pairs as
    a **weak** ``amount_differs`` match (the UI offers "adopt bank amount"). Off
    by default, so the exact behaviour is unchanged unless a caller opts in.
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
            if d is None:
                continue
            overlap = bool(_payee_tokens(imp.payee_raw) & _payee_tokens(e.payee_name))
            if d <= window_days:
                near = False
            elif possible_window_days and d <= possible_window_days and overlap:
                near = True                # extended 'possible' tier (weak only)
            else:
                continue
            score = score_candidate(
                days_apart=d, amount_mismatch_pct=0.0, currencies_match=True,
                src_payee=imp.payee_raw, tgt_payee=e.payee_name,
            )
            pairs.append(_Pair(
                index=imp.index, existing_id=e.id, score=score, days_apart=d,
                payee_overlap=overlap, is_manual=e.is_manual,
                existing_date=e.date_iso, existing_payee=e.payee_name, near=near,
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
        # the established merge-on-confirm behaviour). A match found only via the
        # extended window (ADR-151) is always weak — the wider the date gap, the
        # more it needs a human glance before it's skipped.
        strong = (not p.near) and (
            p.is_manual or p.days_apart == 0 or p.payee_overlap
        )
        out[p.index] = DedupeMatch(
            existing_id=p.existing_id, is_manual=p.is_manual,
            strength="strong" if strong else "weak",
            existing_date=p.existing_date, existing_payee=p.existing_payee,
        )

    if fuzzy_amount_pct > 0:
        _match_amount_mismatches(
            import_rows, existing_rows, out, window_days, fuzzy_amount_pct,
        )
    return out


def _match_amount_mismatches(
    import_rows: list[ImportRow],
    existing_rows: list[ExistingRow],
    out: dict[int, DedupeMatch],
    window_days: int,
    fuzzy_amount_pct: float,
) -> None:
    """Second pass (ADR-130): pair still-unmatched download rows to same-sign,
    payee-overlapping existing rows whose amount is within ``fuzzy_amount_pct``
    — a likely mis-entry. Each existing row still claimed at most once. Patches
    ``out`` in place with weak ``amount_differs`` matches."""
    claimed = {m.existing_id for m in out.values()}
    free = [e for e in existing_rows if e.id not in claimed]
    if not free:
        return

    pairs: list[_Pair] = []
    extra: dict[tuple[int, int], int] = {}   # (index, existing_id) -> existing amount
    for imp in import_rows:
        if imp.index in out:                 # already exact-matched
            continue
        for e in free:
            if (imp.amount_pence < 0) != (e.amount_pence < 0):
                continue                     # opposite sign — not the same charge
            diff = abs(imp.amount_pence - e.amount_pence)
            larger = max(abs(imp.amount_pence), abs(e.amount_pence))
            if diff == 0 or larger == 0 or diff / larger > fuzzy_amount_pct:
                continue
            d = _days_apart(imp.date_iso, e.date_iso)
            if d is None or d > window_days:
                continue
            if not (_payee_tokens(imp.payee_raw) & _payee_tokens(e.payee_name)):
                continue                     # payee overlap REQUIRED for fuzzy
            score = score_candidate(
                days_apart=d, amount_mismatch_pct=diff / larger * 100.0,
                currencies_match=True,
                src_payee=imp.payee_raw, tgt_payee=e.payee_name,
            )
            pairs.append(_Pair(
                index=imp.index, existing_id=e.id, score=score, days_apart=d,
                payee_overlap=True, is_manual=e.is_manual,
                existing_date=e.date_iso, existing_payee=e.payee_name,
            ))
            extra[(imp.index, e.id)] = e.amount_pence

    kept = greedy_pair(
        pairs,
        source_key=lambda p: p.index,
        target_key=lambda p: p.existing_id,
        score_key=lambda p: p.score,
    )
    for p in kept:
        out[p.index] = DedupeMatch(
            existing_id=p.existing_id, is_manual=p.is_manual,
            strength="weak",           # always review an amount change
            existing_date=p.existing_date, existing_payee=p.existing_payee,
            amount_differs=True,
            existing_amount_pence=extra[(p.index, p.existing_id)],
        )
