# ADR-109 — Cloud-safe data file: never-silent-swap launch, visible working file, configurable backups

**Date:** 2026-06-26
**Status:** Implemented (2026-06-26).
**Supersedes:** the ADR-092 *fall-back-from-remembered* amendment (the silent fallback is the bug this fixes).
**Builds on:** ADR-016 (auto-commit, `.mfl` save format), ADR-057 (rotating snapshots), ADR-059 (data library / saved datasets), ADR-060 (GFS retention policy), ADR-092 (reopen last file via `app_session`), ADR-050 (cross-platform, no platform branches outside one helper).

---

## Context

The owner launched the app one morning and a day of edits was "missing": the app had **opened a different file**, and Manage Data showed neither the current file nor any snapshots. Root cause, confirmed on disk:

- The real working file lived in a **cloud-synced folder** — iCloud Drive (`…/com~apple~CloudDocs/Banktivity Exports/mfl_dev_windows3.mfl`). The `session/last_db_path` pointer correctly pointed at it.
- The launch resolver gated the pointer behind `Path.exists()`. When a cloud provider **evicts** a file (its bytes leave the disk, leaving a placeholder), `exists()` reads False — so the resolver **silently fell through** to a stale `mfl_dev.mfl` in the repo or the hidden app-data file. The Snapshots / Saved-Datasets lists then showed folders beside *that* file, so they looked empty.

This is not a personal edge case. Cloud-synced data folders are the norm: iCloud Drive backs macOS Documents/Desktop by default; OneDrive does the same on Windows, often silently. So the fix is general and provider-agnostic.

Four product decisions (owner-confirmed):

1. **Never silently swap files.** A configured main file that's temporarily unreadable (cloud-evicted, drive offline) is made available or recovered *explicitly* — never replaced behind the user's back.
2. **First-run default = a visible `~/Documents/My Financial Life/` folder**, not the hidden app-data location.
3. **Snapshots folder is user-configurable**, always named **`MFL Snapshots`**.
4. The **working file is visible** in Manage Data (a pinned, non-deletable row) and its snapshots appear. Plus: **auto-save on exit** hardened, and a **Locations** settings surface.

## Decision

### Authoritative main-file pointer + never-silent-swap launch
`session/last_db_path` stays the single source of truth for "the file I'm editing" (already updated on File ▸ Open / relocate). Resolution moves to a new, unit-testable **`mfl_desktop/launch.py`** (`resolve_database`), precedence:

1. `--db PATH` — explicit; used if present, else CLI hint + exit 1.
2. **pointer set** — loop: `cloud.ensure_available(pointer)` (waits out / triggers a cloud download) **and** the file opens → use it. Otherwise show the recovery dialog (**Retry / Open a different file / Start a new file / close=quit**). Never fall through to a different file.
3. **no pointer** — a legacy cwd dev file (`mfl_dev.mfl`/`.db`) if present, else
4. the first-run default in `~/Documents/My Financial Life/` (seeded).

Every resolved path is now an explicit choice, so `__main__` records it unconditionally — the whole `fell_back_from_remembered` dance is deleted.

### Provider-agnostic availability — `mfl_desktop/cloud.py`
`is_available` (a real 1-byte read, stronger than `exists()`), `icloud_placeholder` (the `.{name}.icloud` eviction marker), `request_download` (best-effort hydration: `brctl download` on macOS, else a read to wake OneDrive/Dropbox placeholders), and `ensure_available` (bounded poll on a daemon thread, `pump`ing `app.processEvents` so the splash keeps painting). The **only** platform branch in the change lives in `request_download`; the recovery dialog + Retry is the bulletproof core, the auto-trigger merely a convenience.

### Configurable snapshots location, default local
The **retention policy** (GFS knobs) stays **per-file** in the `setting` table (portable across a Mac↔PC synced `.mfl`). The **folder location** becomes **app-level** (`locations/snapshots_root`, default `QStandardPaths.AppDataLocation`) — a filesystem path is a per-machine storage decision, and snapshots are multi-MB full-DB copies that would bloat a cloud folder. The folder is renamed `Snapshots` → **`MFL Snapshots`**; `existing_snapshots`/`prune` **union-read** the legacy beside-the-file `Snapshots/` so no upgrader's history is orphaned (new captures write only to the new location).

### Visible working file + Locations UI
`data_library.current_file()` surfaces the live file as a `kind="current"` row, pinned bold at the top of Saved datasets and non-deletable/-renamable/-loadable-onto-itself. A new **Locations…** button opens `LocationsDialog` (main-file: *Open existing…* / *Move to folder…*; snapshots: *Change…* parent / *Reveal*), which emits intent signals the window acts on (`_on_open_existing_main`, `_relocate_main_file` via `save_copy`+verify+swap+delete — never a cross-device `os.rename`, `_set_snapshots_root`).

### Auto-save on exit
Already auto-commit + `checkpoint` + final snapshot in `closeEvent`; factored into an idempotent `_flush_and_close` and also wired to `app.aboutToQuit`, so Cmd/Ctrl-Q (which bypasses `closeEvent`) still folds the WAL into the `.mfl`.

## Consequences

- The owner's real iCloud file is reopened reliably; an evicted-at-launch file now waits/downloads and, if still unreachable, prompts instead of opening the wrong file.
- New users get a discoverable file in Documents; everyone can see and relocate their working file and backups.
- **Dev workflow change:** with an authoritative pointer, a checked-out repo no longer auto-opens `mfl_dev.mfl` once a pointer exists — use `--db mfl_dev.mfl`. Intentional (the same silent divergence the fix removes).
- **Out of scope:** true multi-writer safety for two machines editing one cloud file concurrently (SQLite WAL isn't a networked store — providers make conflict copies; "Open a different file" is the escape hatch). `checkpoint(TRUNCATE)` on close + moving snapshots off the cloud folder shrink the WAL/sidecar sync window.

## Verification

- `python -m compileall mfl_desktop` (clean).
- Qt-free unit tests on base `python3`: `tests/test_cloud.py` (7), `tests/test_snapshots_location.py` (4), `tests/test_data_library_current.py` (2).
- PySide6 tests under miniforge: `tests/test_launch_resolution.py` (9 — pointer-available, evicted→retry→materialise, →open-other, →new-file-seed, dialog-closed→quit, no-pointer→first-run, legacy-only-when-pointer-absent, `--db` present/missing) and `tests/test_locations_dialogs_smoke.py` (4, offscreen — pinned current row + disabled verbs, Locations signals, recovery choices).
- IRI guard still 6/6.
