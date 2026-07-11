# ADR-151 — Import matching tolerates settlement gaps; net-new rows are reviewable

**Date:** 2026-07-11
**Status:** Phase 1 (matching window) implemented; Phase 2 (review UX) planned
**Related:** ADR-085 (cross-source duplicate detection). ADR-130 (amount-mismatch fuzzy tier). ADR-010 (manual-placeholder merge-on-confirm). ADR-036 (transfer matching). ADR-118 (import-time review). Feedback: "dialogs only when there's something to ask" (silent commit for clean known-format imports).

## Context

Owner report, from a real HSBC statement import: obvious duplicates were created and never surfaced for review. Two examples, both with **unique amounts** so the match is unambiguous:

- A **Capital One** payment of **£588.38** imported as a new row (2026-07-06, "CAPITAL ONE" DD) alongside the existing manual transfer leg to Capital One (2026-07-09) — a 3-day gap.
- An **M6 toll** of **£11.60** imported as a new row (2026-07-06) alongside the hand-typed "M6 Toll Lichfield" placeholder (2026-07-03) — a 3-day gap.

The cross-source matcher (ADR-085) only pairs rows whose posted-dates are within **±2 days**. But a posted-vs-cleared drift of 3 days is the *ordinary* case — a charge authorised Friday settles Monday; a bank-holiday Monday settles Tuesday (4 days). At ±2 both matches fell outside the window, so both rows were classified `new`, and — because the review dialog only appears when there's a flagged match to confirm (the "dialogs only when there's something to ask" rule) — they committed **silently** as duplicates. The owner never got the chance to catch them.

(The owner also observed the existing rows were "changed to cleared." They were not: both existing rows have `bank_posted_date` / `import_batch` NULL, and the commit path only ever writes to an existing row when a match is *confirmed*. They were already `cleared`; the defect is purely the missed match leaving two `cleared` rows side by side.)

## Decision

Two phases. **Phase 1 (this change) fixes the matching; Phase 2 makes the net-new visible and manually matchable.**

### Phase 1 — a wider, tiered match window

- **Confident window ±2 → ±4 days** (`dedupe.DEFAULT_WINDOW_DAYS`). Covers Friday→Monday (3) and the bank-holiday Tuesday (4). A match inside this window keeps its existing strength rules (same-day / payee-overlap / manual-placeholder → **strong**, default-skip; otherwise weak) and its existing confirm behaviour.
- **Extended "possible" tier ±10 days** (`dedupe.POSSIBLE_WINDOW_DAYS`, opt-in via `match_duplicates(possible_window_days=…)`). An exact-amount row beyond the confident window pairs **only if its payee also overlaps**, and is **always weak** — surfaced as a *possible* match for review, never a default skip. This catches the 6–7 day straggler without silently dropping a genuine repeat.

The tiers are safe by construction: a match is never auto-merged. A **strong** match defaults to skip/merge but is shown and reversible; a **weak** match defaults to *keep* (added), so the wider windows can only ever *offer* a match — the worst case is a review row the user leaves alone. Multiplicity is still consumed one-for-one by the greedy pairing (ADR-085), so N identical charges with 1 on file still add N−1.

The candidate SQL fetch (`list_dedupe_candidates`) widens to the ±10 tier so the weak pass has rows to consider; `import_service._apply_dedupe` passes both windows through.

### Phase 2 — net-new visibility + "find a match" (planned)

The review dialog today lists only flagged matches. It will also present the **net-new** rows and, per row, a **"Find match…"** action that searches the account's existing transactions and lets the user link/merge a new row to one the matcher didn't flag — folding the choice into the existing confirmed-match commit path (the row becomes a user-accepted match). This is the owner's "opportunity to find a match" and the "clearer about net new" ask, and it makes the importer robust to matches the automatic tiers still miss.

Rejected:

- **Just widen to ±4 with no extended tier.** Fixes the reported cases but re-introduces a hard cliff — a 5-day straggler silently duplicates again. The weak tier degrades gracefully instead of cliff-edging.
- **Widen the confident window much further (e.g. ±14).** More real repeats would default to *skip*, risking a wrongly-dropped genuine second charge. Keeping the far tier **weak** (default keep, payee-gated) is the safe way to reach out in time.
- **Business-day-aware counting.** More correct in principle, but ±4 calendar days already covers the weekend cases the owner hit, and a fixed number is simpler to reason about and test. Revisit if holiday clusters prove it necessary.
- **Always show the review, even for an all-new batch.** Contradicts the standing "silent commit for clean known imports" preference. Phase 2's net-new list appears within the review that already opens when there's a match to confirm — it doesn't force a dialog onto a genuinely clean import.

## Consequences

- The two reported duplicates — and the whole class of Friday→Monday settlement gaps — now surface as **strong** matches, default-ticked to merge/skip, so a normal import + Import click no longer creates them. Verified in `tests/test_import_settlement_window.py` (the toll and the transfer leg both match strong at 3 days; a 7-day payee-overlapping straggler is weak; 12 days or no-payee-overlap stays new; repeats still consume one-for-one).
- Imports will flag **more** possible matches for review than before. That is the point — they were previously silent duplicates. Weak matches default to *keep*, so review stays low-friction.
- No data migration and no change to already-imported rows. The owner's two existing duplicates remain until removed by hand (they declined an automated cleanup); this change only prevents new ones.
- `match_duplicates` gains an optional `possible_window_days`; existing callers/tests that omit it are unchanged (tier off by default).
