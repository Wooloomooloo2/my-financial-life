# ADR-057 — Automatic rotating database snapshots + clean-close checkpoint

**Date:** 2026-06-12
**Status:** Accepted
**Related:** ADR-016 (file save / open model — auto-commit + Save Copy As; this extends it), ADR-009 (SQLite storage engine — WAL journalling), ADR-050 (cross-platform rule set — paths, no OS-shell coupling), ADR-035 (`setting` key/value table — considered for config and rejected for now).

---

## Context

The owner asked to "do save on close, and a true auto-save." That backlog item was written **2026-06-08**, *before* ADR-050 Tier-2 (2026-06-12) moved the default database off the working directory onto the OS-standard per-user location and made the default file itself a `.mfl`. That amendment quietly satisfied the literal headline ask:

- Every Repository write already **auto-commits** to disk (ADR-016) — there is no in-memory pending state, so there is nothing to "save back on close."
- The default file is now itself the live `.mfl`, and **File ▸ Open** works directly against a chosen `.mfl`. The old "work on `mfl_dev.db`, snapshot to `.mfl` by hand" split is gone.

So "write the open `.mfl` on close" is **already true**. Re-examining what the owner actually wants from data-safety surfaced two real gaps that auto-commit does *not* close:

1. **The `.mfl` is not self-contained on quit.** We open in **WAL mode** (`repository.py`: `PRAGMA journal_mode = WAL`, `synchronous = NORMAL`) and **never close the connection or checkpoint** on exit (`main()` simply returns; there was no `closeEvent`). Committed-but-uncheckpointed writes therefore sit in the `MyFinancialLife.mfl-wal` sidecar. If the user copies, emails, or backs up *just the `.mfl`* — exactly what a single-file app invites — those recent edits are silently absent from the copy. (Crash safety itself is fine: WAL + `synchronous=NORMAL` never corrupts and recovers committed frames on the next open. This is purely about the single file being whole.)

2. **No protection against *logical* mistakes.** Auto-commit faithfully persists a bad import or a botched bulk-edit too. ADR-016's *original* motivation — *"my data as of last Saturday"* — wants **timestamped backups you can roll back to**, which the manual-only `Save Copy As` doesn't provide for a user who forgets to take one before a risky operation.

This ADR closes both. It is the "future auto-snapshot scheduler" that ADR-016 §Ongoing-responsibilities explicitly reserved against `Repository.save_copy`.

## Options considered

### What "save on close" should mean (gap 1)

- **Checkpoint the WAL + close the connection on quit (chosen).** A `RegisterWindow.closeEvent` runs `PRAGMA wal_checkpoint(TRUNCATE)` then `conn.close()`. Folds the sidecar back into the main file so the single `.mfl` is self-contained on every clean exit. Cheap, obviously correct, no schema or model change.
- *Switch to rollback-journal / `synchronous=FULL`.* Rejected — throws away WAL's concurrency and crash characteristics to solve a problem a checkpoint-on-close solves directly.
- *Do nothing (rely on next-open WAL recovery).* Rejected — recovery only helps the app re-opening *the same file in place*; it does nothing for a user who copies the bare `.mfl` between sessions.

### Backup safety net (gap 2) — owner picked "snapshots + in-session timer"

The owner was offered checkpoint-only, rotating snapshots, or rotating snapshots **plus** an in-session timer, and chose the **most coverage** option. So:

- **Rotating timestamped snapshots into a `Snapshots/` folder beside the live database**, taken on **launch**, on a **periodic in-session timer** (`SNAPSHOT_INTERVAL_MIN = 30`), and on **clean close**. Rotation keeps the most recent `SNAPSHOT_KEEP = 10`. Restore is just **File ▸ Open** on the snapshot — no bespoke restore UI needed, because every snapshot is a complete, openable database (the migration runner upgrades an older one on open, per ADR-016).

  - **Location:** `Snapshots/` directly beside the live file (`<db>.parent / "Snapshots"`). For the default install that is `~/Library/Application Support/MFL/Snapshots/` (macOS) / `%APPDATA%\MFL\Snapshots\` (Windows) / `~/.local/share/MFL/Snapshots/` (Linux) — one `pathlib` derivation, no platform branch (ADR-050 rules a/i).
  - **Mechanism:** `Repository.save_copy` (SQLite online backup API) — atomic, WAL-safe, already battle-tested by `Save Copy As`. No new copy path to get wrong.
  - **Naming:** `{stem}-YYYYMMDD-HHMMSS.mfl`. Fixed-width fields ⇒ a lexical filename sort *is* a chronological sort, which both rotation and "newest snapshot" rely on. A same-second collision (e.g. a manual `Save Copy As` as the timer fires) gets a `-N` suffix rather than overwriting.

- *Snapshot inside the live file (a `snapshot` table / VACUUM INTO the same db).* Rejected — a backup that lives in the file it protects dies with that file. Separate files are the point.
- *Store keep-count / interval in the `setting` table (ADR-035).* Deferred — they are module constants (`snapshots.py`) for now; promoting them to a Preferences dialog is a clean follow-up when Preferences lands. No schema churn until there's UI to change them.

### Avoiding duplicate / wasteful snapshots

A naive "snapshot on launch + every 30 min + on close" churns byte-identical copies (launch right after a clean close that already snapshotted; close right after a timer tick with no edits between). Chosen guard: **`maybe_snapshot` writes only if the live database has changed since the newest existing snapshot.** "Changed" is `max(mtime(.mfl), mtime(.mfl-wal)) > mtime(newest snapshot)` — the **WAL sidecar's** mtime is the real signal, because in WAL mode the main file's mtime only advances on checkpoint, so committed-but-uncheckpointed edits would otherwise look like "no change." This makes all three triggers idempotent: the timer and close are no-ops when nothing happened, and the launch snapshot is skipped when it would duplicate the prior session's close snapshot. The launch snapshot, when it *does* fire, is valuable precisely because it captures the opened state **before** this session's edits — a clean rollback point.

## Decision

- **`mfl_desktop/snapshots.py`** — pure, testable snapshot logic. `maybe_snapshot(repo, *, now=None, keep=10, force=False)` decides whether to snapshot (change-detection unless `force`), writes via `repo.save_copy` into `snapshot_dir(db_path)`, then `prune`s to `keep`. `now` is injectable for deterministic tests. **Best-effort:** every public entry swallows exceptions and returns `None` — a backup must never break the app or block close. `prune` only ever deletes files matching this database's snapshot glob, so an unrelated file dropped in the folder is safe.
- **`Repository.checkpoint()`** — `PRAGMA wal_checkpoint(TRUNCATE)`, error-swallowing (a busy checkpoint is harmless; frames stay in the WAL and recover on next open).
- **`RegisterWindow`** orchestrates lifecycle (the only place that knows about launch / timer / close):
  - `__init__`: one `maybe_snapshot` (launch), then a `QTimer` at `SNAPSHOT_INTERVAL_MIN` minutes → `_take_snapshot` (status-bar "Backed up to …" only when one is actually written).
  - `closeEvent`: stop the timer → final `maybe_snapshot` → `repo.checkpoint()` → `repo.close()` → `super().closeEvent`.
  - `_swap_repository` (**File ▸ Open**): the file being *left* gets the same treatment before `old_repo.close()` — final snapshot + checkpoint — so swapping files leaves the previous one whole and backed up. `_take_snapshot` reads `self._repo` fresh, so the running timer automatically follows the newly-opened file.
- **Config**: `SNAPSHOT_DIRNAME = "Snapshots"`, `SNAPSHOT_KEEP = 10`, `SNAPSHOT_INTERVAL_MIN = 30` — module constants in `snapshots.py`.

Nothing about the no-Save model, `Save Copy As`, or `Open` changes. This is purely additive: automatic backups behind the scenes plus a clean-close checkpoint.

## Consequences

### Positive
- The single `.mfl` is **self-contained after every clean quit** — copying/backing up that one file is now lossless.
- **Automatic rollback points** against logical mistakes, with zero user effort and no new verb to learn. A bad import is recovered by opening the pre-import snapshot.
- Restore reuses the existing **File ▸ Open** path (snapshots are ordinary, schema-upgradeable databases) — no restore UI to build or maintain.
- Reuses `save_copy` and the existing WAL setup; no schema migration, no new dependency, fully cross-platform via `pathlib` + `QStandardPaths` (ADR-050).

### Negative / trade-offs
- The `Snapshots/` folder grows to ~`SNAPSHOT_KEEP` copies of the database (bounded by rotation, but each is a full copy — for a large DB that is real disk). Tunable only by editing the constant until a Preferences UI exists.
- Snapshots are **point-in-time, not continuous** — an edit made between the last snapshot and an un-clean kill (process killed, power loss) is in the live file's WAL (recovered on next open) but not in any snapshot. That is the intended boundary: snapshots guard against *logical* mistakes; WAL guards against *crashes*.
- A `--db` user pointing at a read-only or odd location may have an un-writable `Snapshots/` dir — handled by the best-effort swallow (no backup, no error), but such a user gets no rollback points. Acceptable for the dev/power-user `--db` path.

### Ongoing responsibilities
- `maybe_snapshot` / `checkpoint` must stay best-effort — any future caller (e.g. a "snapshot now" menu verb, or snapshotting before a destructive bulk operation) must not let a backup failure surface as a hard error.
- If snapshot retention/interval ever need to be user-visible, promote the three constants to `setting` rows (ADR-035) behind a Preferences dialog — the constants are named and centralised for exactly this.
- A future "Restore from snapshot…" convenience verb (a picker over `existing_snapshots`, then `_swap_repository`) is a clean follow-up, but **File ▸ Open** already covers it.
