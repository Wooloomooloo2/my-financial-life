# ADR-137 — Compact the database to reclaim free pages (VACUUM)

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-059 (Data Library dialog — where the action lives). ADR-057/060 (snapshots + WAL checkpoint on close). ADR-032 (the CHECK-constraint table-rebuild recipe that generates much of the slack). ADR-085 (import dedup — delete/re-insert churn).

## Context

Owner report: the `.mfl` jumped from ~13 MB to over 20 MB in a week without much new data. Diagnosis on the dev file (same pattern: 12.45 MB on 14 Jun → 18.05 MB now):

- **Row counts barely moved** — and net *fell*: payees −110 (the June merge cleanup), budget goals removed, categories −5, versus only ~58 new transactions and some price/FX rows.
- **`PRAGMA freelist_count` = 1621 pages = 6.33 MB of free space** sitting unused inside the file, with **`auto_vacuum = 0`** (SQLite's default).

So the growth is **dead space, not data**. SQLite with auto-vacuum off never returns freed pages to the OS — every delete (payee merges, removed accounts/goals) and every whole-table rebuild (the schema migrations that copy a table to widen a CHECK constraint — 12 migrations ran between `schema_version` 22→34, several rebuilding the 35 k-row `txn` / 11 k-row `txn_split` tables) leaves pages on the freelist, and the file only ever grows. A one-off `VACUUM` reclaims them: on the dev file it took **18.05 MB → 11.19 MB (−38 %)** — smaller than the June backup, consistent with the lower row count.

The app had **no way to reclaim this** — `save_copy` (backup API) and `checkpoint` (WAL→main) exist, but neither compacts, and nothing runs `VACUUM`.

## Decision

Add a manual **Compact file…** action to the Data Library dialog (Manage Data), backed by a new `Repository.compact()`.

- `Repository.compact() -> (size_before, size_after)`: commits any pending transaction, folds the WAL in (`wal_checkpoint(TRUNCATE)`), runs **`VACUUM`**, commits, checkpoints again, and returns the on-disk sizes. VACUUM can't run inside a transaction, hence the explicit commit first; the double checkpoint makes the measured size the real single-file size. It keeps every row — only free pages go.
- The Data Library dialog gains a **Compact file…** button (ActionRole, beside Locations…) that shows the current size, confirms ("keeps every bit of your data"), runs `compact()`, and reports the reclaim (`before → after`, or "already compact"). The dialog then refreshes so the pinned *current* row shows the new size.

**Manual, not automatic** (for now): VACUUM rewrites the whole file (seconds on a 20 MB file, longer as data grows) and needs free disk for a temp copy, so a surprise pause on every close is worse than an explicit, occasional button. The migration-driven slack is largely one-off; ordinary use churns slowly.

Rejected: switching to `auto_vacuum = INCREMENTAL` (itself requires a full VACUUM to take effect on an existing file, and incremental vacuum needs periodic `PRAGMA incremental_vacuum` calls — same manual trigger, more moving parts); VACUUM-on-close (unpredictable shutdown stalls, and Ctrl-Q bypasses `closeEvent`); auto-VACUUM when the freelist crosses a threshold (reasonable future enhancement — noted below — but starts with the explicit control so the behaviour is understood first).

## Consequences

- Users can reclaim bloat on demand from Manage Data; the owner's file will drop back to ~11–12 MB with all data intact.
- No schema change, no migration; `VACUUM` is atomic (temp-file rebuild) and the `integrity_check` on the compacted file is clean.
- The action operates on the single shared `Repository` connection (the app's one-connection model), committing first so no open transaction blocks the VACUUM.
- Possible follow-up: offer an automatic compact when `freelist_count` is large (e.g. after a big migration or a bulk payee merge), gated behind a one-time prompt.
- Verified headless: `compact()` on a copy of the live file reclaimed 6.86 MB (18.05 → 11.19 MB) with all 75,667 rows across 32 tables unchanged and `PRAGMA integrity_check = ok`; the Data Library dialog builds with the new button.
