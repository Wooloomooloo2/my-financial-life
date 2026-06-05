# ADR-016 — File save / open model: auto-commit + Save Copy As snapshots

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-009 (Storage engine — SQLite)

---

## Context

The owner wants to take backups as the data set grows and as new features land — a way to keep "this is my data as of last Saturday" available, so that experimental changes (a new categorisation rule, a botched bulk edit, a feature regression) don't risk destroying months of imported transactions.

The data lives in a single SQLite file with WAL journalling. Every Repository method commits as part of its own atomic transaction (`self.commit()` after the SQL completes, `self.rollback()` on exception). There is no pending in-memory state — what's on disk after a successful method call *is* the truth.

This is unlike a Word/Excel document model where the working file diverges from disk and a "Save" verb writes accumulated edits back. Three options for how to expose this to the user follow.

## Options considered

### Option 1 — Auto-commit + Save Copy As + Open (chosen)

No "Save" menu item. Two file verbs:

- **File → Save Copy As…** — atomic SQLite backup of the current database to a chosen path. Uses `sqlite3.Connection.backup()`, which is online (no need to checkpoint WAL first), atomic (the destination is either the full snapshot or absent), and doesn't interrupt the working session. The working file is untouched.
- **File → Open…** — switch the app to working on a chosen database file. The current Repository is closed; a new Repository is opened against the chosen file (which runs the migration runner, so opening an older backup auto-upgrades it to the current schema); sidebar, category cache, filter combo, and register model are all rebuilt against the new repo.

The window title shows the current filename: *"My Financial Life — Personal.mfl — Current Account · GBP"*. The user always knows which file they're editing.

Strengths: matches how the data actually works (every edit is already persisted). No risk of losing data because the user forgot to save. The snapshot verb has a name that says exactly what it does.

### Option 2 — Add a no-op "Save" alongside Save Copy As

Keep a "Save" menu item for muscle memory (Ctrl+S), even though every commit already hits disk. Save would be cosmetic — flash "Saved" in the status bar and return.

Rejected. A menu item that does nothing visible is worse than no item at all — the user learns to ignore it, and when they actually need a snapshot they reach for Save instead of Save Copy As and lose the backup they wanted to keep.

### Option 3 — Document-style: an explicit working file with dirty tracking

Treat the database as a document — track "dirty" state in memory, only commit to disk on Save. A "Save" verb writes pending edits, "Save As" writes them to a new path and becomes the new working file, "Open" loads a different file (with an "unsaved changes" prompt).

Rejected. Requires holding edits in memory and re-architecting the Repository (every write currently commits). Buys nothing for the user — autosave is universally preferable for financial data, where losing a half-hour of categorisations to a crash is a real cost.

### File extension — `.mfl` (chosen, with `.db` accepted)

`.mfl` is a clear marker that the file belongs to this app and not to some other SQLite-using tool the user might also run. The dialog filter accepts both `.mfl` and `.db` so existing `mfl_dev.db` users aren't locked out. Save Copy As defaults to `.mfl` when the user doesn't type an extension.

## Decision

**Two file verbs**: Open… (Ctrl+O) and Save Copy As… (Ctrl+Shift+S). No plain Save.

**Repository owns the path**: `Repository.db_path` is a public property; the window title reads it on every update. `Repository.save_copy(dest_path)` wraps `sqlite3.Connection.backup()` — atomic, online, WAL-safe.

**Open replaces the working repo**: `RegisterWindow._swap_repository(new_repo)` closes the old repo, replaces `self._repo` and `self._service`, refreshes the cached category list, rebuilds the sidebar (which fires selection_changed and drives the register model rebuild via the existing code path), and updates the window title. If the file can't be opened (corrupt, wrong format), the swap aborts before the old repo is closed and the user keeps the previous file.

**Window title** format: `My Financial Life — {filename} — {view info}`. The filename is always visible so users can't accidentally edit the wrong file when juggling backups.

**File extension**: `.mfl` preferred; `.db` accepted in the Open dialog; Save Copy As defaults to `.mfl`.

## Consequences

### Positive
- One-click snapshot. Save Copy As is a *named, intentional verb* — the user always knows what it does. No ambiguity with a no-op Save.
- Opening an older backup auto-upgrades its schema via the migration runner, so backups taken before a future schema change still load.
- Zero risk of data loss from forgetting to save. Every commit is on disk before the next user action.
- The Repository's `db_path` property makes the working file visible to any other UI piece that wants it (recent files, status bar, eventual auto-snapshot scheduler).

### Negative / trade-offs
- A user used to Ctrl+S = "save my work" will reach for it and find nothing happens. The Ctrl+Shift+S binding for Save Copy As is the closest alternative; muscle memory recovers fast once the model is explained.
- No "unsaved changes" prompt on Open or Quit, because there are no unsaved changes. If a user opens a different file mid-task, the previous file stays exactly as they left it. This is correct behaviour but takes a moment to internalise.

### Ongoing responsibilities
- `_swap_repository` is the single point where the working repo changes. Any future UI piece that caches Repository data (delegates, models, filter combos) must be invalidated and rebuilt there — the same way `_refresh_categories_view` is reused for both import and category management.
- Recent files / last-opened-file persistence is a small follow-up. QSettings on the standard organisation/application name will land in `%APPDATA%` on Windows, `~/.config` on Linux, `~/Library/Preferences` on macOS — see ADR-004 for the cross-platform contract.
- A future auto-snapshot scheduler (e.g. nightly copy of the working DB to a "Snapshots" folder) plugs into `Repository.save_copy` directly without any UI change.
