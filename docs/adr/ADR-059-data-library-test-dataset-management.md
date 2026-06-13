# ADR-059 — Data Library: visual save / load of whole datasets

**Date:** 2026-06-13
**Status:** Accepted
**Related:** ADR-016 (file save / open model — auto-commit + Save Copy As + Open; this builds on it), ADR-057 (automatic rotating snapshots — the `Snapshots/` folder this screen also lists; ADR-057 §Ongoing-responsibilities explicitly reserved this "Restore from snapshot…" follow-up), ADR-009 (SQLite storage engine — WAL + the online backup API), ADR-050 (cross-platform rule set — `pathlib` paths, no OS-shell coupling).

---

## Context

`File ▸ Save Copy As…` and `File ▸ Open…` (ADR-016) plus the automatic `Snapshots/` folder (ADR-057) already give save, open, and rollback. But both verbs are **blind file-pickers**: to switch between datasets the owner navigates an OS dialog, types or hunts for a filename, and gets no view of what's been saved. While building the budget redesign (ADR-058) against many fixtures — an empty file, a multi-currency file, a card-payoff scenario — the owner asked for "a way to open a file as well… a small screen showing saved data and the ability to load and save."

The need is a **visual library of whole datasets** for juggling test data, not a new persistence model. Each `.mfl` is already a complete, self-contained, schema-upgradeable database (ADR-016 / ADR-057), which makes a shelf-of-datasets the natural unit.

Two owner-resolved forks shaped it (via `AskUserQuestion`):

1. **What the screen lists** → *named saves **and** snapshots.* So the screen doubles as the "Restore from snapshot…" UI that ADR-057 deferred.
2. **What "Load" does to a dataset** → *load a **fresh working copy**.* Loading copies the dataset onto the live working file so the **saved original stays pristine** — load a baseline, mess it up, reload it clean, and the library copy never changed. This is the decisive difference from `File ▸ Open`, which makes the picked file *itself* live (every edit mutates it).

## Options considered

### Storage layout

- **A `Library/` folder beside the live db, parallel to ADR-057's `Snapshots/` (chosen).** Named saves are ordinary `.mfl` files in `<db>.parent / "Library"`. One `pathlib` derivation, no platform branch (ADR-050), and it travels with the live file across a load just like `Snapshots/` does. Snapshots keep their existing home and naming; the screen simply lists both folders.
- *A registry/manifest file mapping names → paths.* Rejected — a folder of `name.mfl` files **is** the manifest; the filename is the name. No second source of truth to keep in sync.
- *Store datasets inside one container db.* Rejected for the same reason ADR-057 rejected in-file snapshots — a dataset that lives in the file it's meant to be independent of isn't independent.

### Load semantics (the load-bearing fork)

- **Clone the source onto the working file; reopen (chosen).** Tear down the current working file (snapshot + checkpoint + close, exactly as ADR-057's `_swap_repository` already does), clone the chosen dataset/snapshot **onto** the working-file path, reopen, rebuild the UI. The saved original is never opened for writing, so it can't be mutated or migrated. The working file keeps a single stable path; the *loaded dataset's name* (not the bench filename) shows in the title bar so the user always knows which fixture they're in.
- *Make the chosen file live, like `File ▸ Open`.* Rejected for this verb — it mutates the saved dataset as you work, which defeats "reload a clean baseline." `File ▸ Open` still exists for the make-it-live case; the two verbs are deliberately distinct.
- *Load into a per-dataset working file named after the dataset.* Rejected — multiplies working files and fragments where the `Snapshots/` folder attaches. One stable bench file is simpler and keeps snapshots in one place.

### Clone safety

The source must never be harmed and the working file must never be left half-written:

- **Read-only source + temp-file + atomic replace (chosen).** `clone_database` opens the source with a `file:…?mode=ro` URI (so a load can never migrate or mutate the pristine copy — opening a `.mfl` via `Repository()` would run the migration runner and *write* to it), writes the clone to a `*.loading.tmp` via SQLite's online backup API, then `os.replace`s it over the working file. A failure mid-clone leaves the existing working file intact. The working file's own WAL/SHM sidecars are dropped first so the reopened db can't recover frames from the previous occupant.
- *Plain `shutil.copyfile`.* Rejected — wouldn't capture a source that happened to carry an uncheckpointed `-wal` sidecar, and gives no read-only guarantee. The backup API is already the project's WAL-safe copy path (`Repository.save_copy`).

### Where the logic lives

- **A Qt-free `mfl_desktop/data_library.py` (chosen)** holds every at-rest file operation — list / save-path / clone / rename / delete / sanitize — so it's pure and testable, mirroring `snapshots.py` and `budget_calc.py`. The dialog (`ui/data_library_dialog.py`) is a thin view; the window (`register_window.py`) owns the one thing the dialog can't — replacing the live working file.

## Decision

- **`mfl_desktop/data_library.py`** (Qt-free):
  - `library_dir(db_path)` → `<db>.parent / "Library"`; `library_path(db_path, name)`; `sanitize_name` (strips path-illegal chars, collapses whitespace, empty ⇒ rejected by callers).
  - `list_saved(db_path)` / `list_snapshots(db_path)` → `list[DataFile]` (path, display name, `saved_at` mtime, size, `kind`), newest first. Snapshots delegate to `snapshots.existing_snapshots`.
  - `clone_database(src, dest)` — read-only source, temp-and-atomic-replace, sidecar cleanup (see *Clone safety*).
  - `rename_saved(path, new_name)` (collision → `FileExistsError`, empty → `ValueError`); `delete_file(path)` (best-effort sidecar removal; main `unlink` raises so the caller can report).
- **`mfl_desktop/ui/data_library_dialog.py`** — modal `DataLibraryDialog`. Two tabs: **Saved datasets** (Load… / Save current as… / Rename… / Delete) and **Snapshots** (Load a copy… / Delete), each a non-editable single-select table (Name · Saved · Size). *Save current as…* prompts a name, confirms an overwrite, and writes via the live `Repository.save_copy` (the working file is untouched, so the dialog stays open). Loading is the one thing the dialog can't do itself — it emits `load_requested(Path)` after a confirm and closes (its repo handle is about to be torn down); the window does the swap.
- **`RegisterWindow`**:
  - `_swap_repository` refactored into **`_adopt_repository`** (bring a repo up: service, category cache, sidebar, combo, title, auto-post sweep) + **`_teardown_repository`** (the ADR-057 snapshot + checkpoint + close). `File ▸ Open` is adopt-new-then-teardown-old (recoverable); load is teardown-old-first (the file has to be closed before it can be overwritten).
  - **`_load_dataset(source)`** — teardown the working file, `clone_database(source, bench)`, reopen + adopt; on clone failure, reopen the (intact) bench and report. Sets `_loaded_dataset = source.stem`, shown in the title in place of the bench filename.
  - **File ▸ Manage Data…** (`Ctrl+Shift+D`) opens the dialog.
- **Config:** `LIBRARY_DIRNAME = "Library"` — module constant in `data_library.py`.

Nothing about the no-Save model, `Save Copy As`, `Open`, or snapshots changes. This is additive: a visual front end over existing copy/open/snapshot mechanics, plus the one new behaviour of loading a *working copy* rather than the file itself.

## Consequences

### Positive
- A real library for juggling test data: see every saved dataset and snapshot, load any with two clicks, save the current state under a name — no OS file dialog, no filename hunting.
- **Loading never mutates a saved dataset** (read-only clone), so a baseline fixture is reusable indefinitely — the core ask.
- Fulfils ADR-057's deferred "Restore from snapshot…" verb: snapshots are loadable from the same screen.
- Pure, testable file layer (`data_library.py`); no schema migration, no new dependency; fully cross-platform via `pathlib` (ADR-050). Reuses `save_copy` and the WAL backup path.

### Negative / trade-offs
- The `Library/` folder is full copies of the database — bounded only by the user (no rotation, unlike `Snapshots/`). Deleting old datasets is a manual verb in the screen.
- "Load a working copy" means edits made to the working file since the last *Save current as…* exist only in the auto-snapshot taken at teardown — not in any named dataset. The confirm dialog states this; the ADR-057 snapshot is the safety net.
- A `--db` user pointing at a read-only location has an un-writable `Library/` — save/clone surface the error rather than silently no-op (unlike snapshots, which are best-effort background work). Acceptable: these are explicit user verbs that should report failure.

### Ongoing responsibilities
- `clone_database` must keep its atomic temp-and-replace + read-only-source contract — any future caller (e.g. "duplicate dataset", or loading before a destructive operation) depends on the working file surviving a failed clone and the source surviving a load.
- If `Library/` retention ever needs bounding, it's a rotation/prune follow-up mirroring `snapshots.prune` — but named saves are deliberately user-owned, so this stays manual unless the owner asks.
