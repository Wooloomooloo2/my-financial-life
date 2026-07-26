# ADR-182 — Reconciling pending transactions within the statement dates

**Date:** 2026-07-22
**Status:** Implemented
**Related:** ADR-130 (the confidence ladder and status-driven reconciliation — this ADR relaxes its "pending is never eligible" default, deliberately). ADR-040 (the statement-reconciliation flow and wizard). ADR-050 (cross-platform-first — the no-download / hand-kept account this must keep first-class).

## Context

Owner-reported: *"when reconciling accounts, there has to be a way of including pending transactions that are within the date range."*

ADR-130 made reconciliation status-driven and, in sub-decision 1, ruled *"pending is never eligible."* That was the right fix for the bug it addressed — the June HSBC variance, where not-yet-at-the-bank 30-June card purchases got ticked onto a statement. `list_reconcilable_txns` enforces it: `matched` always eligible, `cleared` optional via a toggle, `pending` excluded outright.

But that default strands a whole workflow. **Manual entry defaults to `pending`** (`transaction_dialog.py`), and a user who keeps an account by hand — entering transactions but not importing OFX and not manually bumping each row to `cleared` — leaves *everything* in `pending` forever. For them the reconcile screen is permanently empty, and the existing "Include cleared" escape hatch doesn't help: their rows are `pending`, not `cleared`. ADR-130 optimised for the download-driven ladder and made the hand-kept case unreconcilable.

The owner's framing carries the fix: *"within the date range."* The danger ADR-130 guarded against was **future** pending (next month's not-yet-real spending) becoming reconcilable. Bounding pending eligibility to the statement dates removes exactly that danger while opening the case that matters.

## Decision

**Add an opt-in "Include pending transactions within the statement dates" toggle, off by default, that makes pending rows eligible only when they fall inside the statement period.** (Owner chose this direct approach over a "bump pending → cleared first" alternative.)

- **Query (`list_reconcilable_txns`).** New `include_pending: bool = False` and `period: Optional[tuple[str, str]]`. A pending row is eligible only when `include_pending` **and** `period` is given **and** `COALESCE(bank_posted_date, posted_date) BETWEEN period`. Without a `period` no pending row is ever eligible, whatever the flag — the ADR-130 default is preserved as the zero-argument behaviour. This is the one status whose eligibility is **date-bounded**; `matched`/`cleared` stay any-date (old stragglers, ADR-040). A pending row has no bank date, so it ranges on the user's `posted_date`.
- **Wizard.** A second checkbox on the balances page beside "Include cleared", feeding the gate and the auto-select preset (pending joins `matched`/`cleared` in the in-period pre-tick), re-gating live on the check-off page, and disabled in read-only view. A symmetric **warning** counts in-period pending rows the gate is excluding (`count_pending_in_period`) — this is the *discovery* path: a hand-kept account, whose rows are all pending, would otherwise see an empty screen with no hint the option exists.
- **Status on close is unchanged.** A ticked pending row goes `pending → reconciled` at `close_statement` like any other ticked row — reconciling *is* confirmation. It skips the `cleared`/`matched` rungs, which is correct: those rungs record corroboration sources (eyeball, download) that a hand-kept row never had.

## Rejected

- **Bump-first (`pending → cleared`, then reconcile via the existing path).** More faithful to ADR-130's ladder, but it edits transactions *outside* the reconcile as a side effect and adds a step. The owner asked to include pending directly.
- **Making pending any-date eligible, like matched/cleared.** This reintroduces the exact June bug (future pending becoming reconcilable). The date bound is the whole safety argument; without it the feature is unsafe.
- **On by default.** Would silently undo ADR-130 for every account, including the download-driven ones it correctly protects. Off-by-default keeps the safe default and makes including pending a deliberate, per-reconcile choice.
- **Persisting the toggle on the statement.** Like "Include cleared" (ADR-130) it's a transient per-reconcile view choice, not statement data; it re-resolves to off each open.

## Consequences

- **A hand-kept account can be reconciled.** Pending rows dated inside the statement period can be ticked; on close they become `reconciled`. The warning surfaces the option precisely to the users who need it.
- **The ADR-130 safety holds where it mattered.** Future/out-of-period pending stays un-reconcilable (the June bug can't recur), and the default candidate set is byte-for-byte what ADR-130 shipped — the new path is reachable only through an explicit opt-in *and* a date bound. Cleared gating and the cleared warning are untouched.
- **`pending → reconciled` is now a reachable transition**, skipping the intermediate rungs by design. `reopen_statement` / `delete_statement` revert reconciled rows to `matched` (ADR-130's existing behaviour), so a reopened hand-entered row lands at `matched`, not back at `pending` — a one-way ratchet already accepted in ADR-130 and unchanged here.
- No schema change; the confidence ladder and `bank_posted_date` are reused as-is.

Verified end-to-end by driving the wizard offscreen: with the toggle off, an in-June pending row is excluded and the warning names it; toggling on surfaces it while a July pending row stays out (the date bound); ticking it plus a matched row and saving closes the statement and marks **both** `reconciled`, leaving the July pending row `pending`. `tests/test_reconcile_confidence.py` grew from 4 to 9 (include-pending-in-period adds only pending; requires a period; date-bounded excludes out-of-period even with the flag; pending+cleared together; `count_pending_in_period`). Full suite 428 passed, 0 failed.
