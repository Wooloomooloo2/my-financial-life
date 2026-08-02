# ADR-186 — A skipped duplicate still re-confirms the row it duplicates

**Date:** 2026-08-02
**Status:** Implemented
**Related:** ADR-130 (the confidence ladder — `pending` → `cleared` → `matched` → `reconciled` — and `bank_posted_date`; this ADR closes the one path that could put a row *down* the ladder with no way back up). ADR-085 (count-aware cross-source duplicate matching — the fuzzy pass this exact-hash fast path short-circuits). ADR-010 (merge-into-manual-placeholder, the sibling path that already does this). ADR-151 (settlement-gap window).

## Context

Owner-reported: *"why did my latest import fail to match so many items in the HSBC account between the start of July and 11th July?"*

Forensics on the live file. The 2 August import of `TransactionHistory (4).ofx` (1–31 July, batch 50) split cleanly on the date the *previous* import had covered:

| Date range | duplicate (skipped) | matched | new |
|---|---|---|---|
| 1–10 Jul | **63** | 4 | 1 |
| 13–31 Jul | 0 | 92 | 3 |

The 1–10 July rows had already been imported on 11 July (`TransactionHistory (3).ofx`, batch 45). HSBC reissues the same FITID for a given transaction, so every one of them hit the exact-`import_hash` fast path in `_classify_and_stage` and was staged `duplicate`. On commit, `duplicate` was `skipped += 1; continue` — the incoming copy dropped, the existing row never touched.

Separately, and this is what made it visible: 60 of those rows had been knocked from `matched` back to `cleared` (status column only — no other field on the row changed; the register's inline status edit and the bulk-edit dialog are the only writers). The two rows in that window that the import *did* re-match — the M6 Toll charge and the Capital One transfer, both reached via the fuzzy pass because their hash was absent — are the only two still sitting at `matched`. So the register showed a wall of `cleared` across the first ten days of July, immediately below a fully `matched` rest-of-month.

The downgrade itself is a user action and out of scope. What this ADR fixes is that **it was unrepairable**: every subsequent download of an overlapping period carries the same FITID, is classified `duplicate`, and is skipped. There was no import that could put those rows back. The only remedies were another manual bulk edit or deleting the rows and re-importing — both worse than the problem.

That is also inconsistent with the sibling path. When a download meets a *hand-entered* row, `merge_into_manual_transaction` treats it as the bank confirming the transaction and advances `pending`/`cleared` → `matched` (ADR-130). An exact-hash duplicate is a **stronger** confirmation — same account, same FITID, no fuzzy inference at all — and was the one confirmation we threw away.

## Decision

**On commit, a `duplicate` row re-confirms the transaction it duplicates instead of walking past it.** The incoming copy is still skipped — no row is created, `duplicate_count` is unchanged — but the row carrying that `import_hash` is carried back up the ladder.

- **`Repository.reconfirm_by_import_hash(account_id, import_hash, bank_posted_date)`.** Looks up the row by `(account_id, import_hash)` and applies exactly two changes: `status = CASE WHEN status IN ('pending','cleared') THEN 'matched' ELSE status END`, and `bank_posted_date = COALESCE(bank_posted_date, ?)`. Returns `True` only when the status actually advanced. No commit — the caller owns the transaction.
- **Nothing else moves.** Amount, payee, category, memo, and the user's spend date are untouched, so a re-import can never overwrite an edit made since the first one. This is deliberately narrower than `merge_into_manual_transaction`, which also stamps the hash and may fill an empty memo: on this path the row was already stamped by the original import, so those questions are settled.
- **`reconciled` is locked**, as everywhere else — a closed statement is not disturbed by a re-download. `matched` is already at the rung and only gains a missing bank date.
- **An existing `bank_posted_date` wins.** The duplicate carries the same date by construction, so first-write-wins is the conservative choice (`merge_into_manual_transaction` has the opposite `COALESCE` order because there the incoming date is the *only* one).
- **Reported as `ImportResult.refreshed`** — a subset of `skipped`, never an extra row. The register status bar and the bank-feeds summary add a "(N re-confirmed)" clause only when N > 0; the CLI prints it always. `import_batch.duplicate_count` keeps its existing meaning.

## Rejected

- **Leaving it and telling the owner to bulk-edit back.** The asymmetry stays: an import can move a row down (indirectly, by never restoring it) but never up. A bank feed re-delivering the same FITIDs every sync hits this on every account, not just after a manual edit.
- **Widening the duplicate check so re-downloads become fuzzy matches instead.** Would surface 63 rows in the review dialog for the owner to tick, to reach a conclusion the exact hash already proves. It also reintroduces multiplicity risk (ADR-085) that the hash path is precisely designed to avoid.
- **Reusing `merge_into_manual_transaction` directly.** The SQL is nearly right, but it re-stamps the hash it already matched on and can rewrite the memo — writes with no justification on this path. A narrow method states the intent and bounds the blast radius.
- **Counting re-confirmations as `matched`.** `matched` means "the review dialog's match was accepted and merged." Conflating the two would make the import summary and `import_batch.matched_count` mean two different things.
- **Advancing `reconciled` rows, or letting a later bank date overwrite an earlier one.** Both edit settled data on a path the user never reviews.

## Consequences

- **Re-importing an overlapping period is now self-healing.** Verified against a copy of the live file: re-running `TransactionHistory (4).ofx` over the current database reports `skipped=161, refreshed=63` and moves exactly 63 rows `cleared` → `matched`, leaving the 6 `reconciled` rows and the 8 never-downloaded rows alone. That is the owner's reported window, repaired by the import that previously ignored it.
- **Overlapping downloads become useful rather than inert.** Grabbing a wider date range than needed now tops up `bank_posted_date` on rows that predate ADR-130 and lifts anything sitting below `matched`, instead of being a no-op.
- **Bank feeds benefit most.** A feed re-delivers the same FITIDs every sync, so its "N skipped" line was pure noise; the re-confirmed count is now the informative part of it.
- **Reconciliation gets more reliable.** `matched` is always eligible while `cleared` needs the opt-in toggle (ADR-130/ADR-182), so a downgraded row silently dropped out of the default candidate set. It comes back on the next import.
- No schema change and no migration; the ladder, `bank_posted_date`, and the batch counters are reused as-is.

`tests/test_import_duplicate_reconfirm.py` adds 7 (ladder advance for each of the four statuses; bank date filled vs. preserved; no other column touched; account-scoped; unknown hash a no-op; the reported downgrade-then-repair case end to end; a settled re-import reporting `refreshed=0`). Full suite 461 passed, 0 failed — run file-by-file because `tests/test_budget_drilldown_editable.py` triggers a pre-existing Qt garbage-collection access violation at interpreter shutdown, reproduced unchanged on clean `master`. `RegisterWindow` and `BankFeedsDialog` constructed offscreen to check the reworded summaries.
