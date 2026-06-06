"""Pure-Python scoring + greedy-pairing helpers for transfer matching.

Shared by:

- ``Repository.find_transfer_candidates`` (ADR-036 single-flow matcher)
- ``Repository.find_transfer_pairs`` (ADR-037 reconcile dialog)

No database access here — the Repository methods do the fetch and pass
already-shaped values into ``score_candidate``. Keeps the scoring logic
unit-testable without a SQLite fixture and makes future tuning a
one-place change.
"""
from __future__ import annotations

from typing import Callable, Iterable, TypeVar


# Score → strength thresholds. Used by the matcher's confirm/picker
# dialogs (ADR-036) and the reconcile screen's chip rendering (ADR-037).
STRONG_THRESHOLD = 80
GOOD_THRESHOLD = 60


# Payee tokens shorter than this aren't useful for overlap detection.
_MIN_TOKEN_LEN = 3

# Words that appear in machine-generated transfer payee strings and
# shouldn't bias the score (every "Transfer to / Transfer from" pair
# would otherwise be falsely boosted by their own labels).
_PAYEE_STOPWORDS: frozenset[str] = frozenset({
    "transfer", "transfers", "from", "to", "the", "and", "for", "with",
    "via", "ref", "payment", "deposit", "withdrawal", "txn",
    "into", "onto", "out",
})


def _payee_tokens(name: str) -> set[str]:
    """Lowercase, length-filtered, stopword-stripped token set."""
    if not name:
        return set()
    cleaned = (
        name.lower()
        .replace(",", " ")
        .replace(":", " ")
        .replace("/", " ")
        .replace("-", " ")
    )
    return {
        tok for tok in cleaned.split()
        if len(tok) >= _MIN_TOKEN_LEN and tok not in _PAYEE_STOPWORDS
    }


def score_candidate(
    *,
    days_apart: int,
    amount_mismatch_pct: float,
    currencies_match: bool,
    src_payee: str = "",
    tgt_payee: str = "",
) -> int:
    """Score a candidate / pair on a roughly-0-to-110 scale; higher is
    better. The matcher uses the score only for ordering and bin labels —
    never for auto-decisions.

    Weights:

    - Base ``100``.
    - Subtract ``5 * abs(days_apart)`` — same-day is best, edges of the
      window penalised.
    - Subtract ``50 * (amount_mismatch_pct / 100)`` — 1% mismatch costs
      0.5; 5% costs 2.5; matcher upstream discards anything wider than
      its tolerance.
    - Subtract ``20`` if currencies differ — cross-currency starts at 80
      ("Strong") in the best case, falls to "Good" or "Possible" as the
      other penalties stack.
    - Add ``10`` if the two payee strings share at least one non-stopword
      token of length ≥ 3 (after lowercasing and basic punctuation
      cleanup) — a small nudge when both sides clearly reference the same
      entity (e.g. "ACME PAYROLL" / "Acme Inc").
    """
    score = 100
    score -= 5 * abs(int(days_apart))
    bounded = max(0.0, min(float(amount_mismatch_pct), 100.0))
    score -= int(50.0 * bounded / 100.0)
    if not currencies_match:
        score -= 20
    if _payee_tokens(src_payee) & _payee_tokens(tgt_payee):
        score += 10
    return max(0, score)


def strength_for_score(score: int) -> str:
    """Bin a score into the chip label used in the picker + reconcile."""
    if score >= STRONG_THRESHOLD:
        return "Strong"
    if score >= GOOD_THRESHOLD:
        return "Good"
    return "Possible"


T = TypeVar("T")


def greedy_pair(
    candidates: Iterable[T],
    *,
    source_key: Callable[[T], int],
    target_key: Callable[[T], int],
    score_key: Callable[[T], int] = lambda c: getattr(c, "score", 0),
) -> list[T]:
    """Walk ``candidates`` highest-score-first, claim each source / target
    id at most once. Returns the kept candidates in walk order.

    Stable: ties broken by ``score`` desc, then ``source_key`` asc, then
    ``target_key`` asc — same input yields the same output across runs.

    Used by the reconcile dialog (ADR-037) to turn the cross-product of
    A's unmatched rows × B's unmatched rows into a one-to-one pairing.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (-score_key(c), source_key(c), target_key(c)),
    )
    claimed_src: set[int] = set()
    claimed_tgt: set[int] = set()
    paired: list[T] = []
    for c in ordered:
        s = source_key(c)
        t = target_key(c)
        if s in claimed_src or t in claimed_tgt:
            continue
        claimed_src.add(s)
        claimed_tgt.add(t)
        paired.append(c)
    return paired
