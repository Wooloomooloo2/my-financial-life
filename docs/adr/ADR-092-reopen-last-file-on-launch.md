# ADR-092 — Reopen the last-used file on launch

**Date:** 2026-06-20
**Status:** Accepted
**Related:** ADR-050 (cross-platform file locations), ADR-016 (`.mfl` save format / auto-commit), ADR-057 (sidecars beside the file). Owner-reported annoyance.

---

## Context

Quitting and relaunching the app always reopened the **default** file in the default location, never the file the user had actually been working in. You'd `File ▸ Open` your real `.mfl`, work in it, quit — and the next launch dropped you back on the per-user default `MyFinancialLife.mfl` as if the open never happened.

The launch resolution (`__main__.main`) had no memory of it. With no `--db` flag it picked, in order: a legacy dev DB in the working directory (`mfl_dev.mfl` / `mfl_dev.db`), else the OS-standard appdata default. The file a user opened at runtime via `File ▸ Open` (`_swap_repository`) was never recorded anywhere, so it couldn't influence the next launch.

The natural place to store "which file" — the per-file `setting` table — is the wrong one: it lives **inside** each `.mfl`, and we need the path *before* any file is open (chicken-and-egg). This is genuinely app-level, cross-file state.

---

## Decision

**Persist the most-recently-opened database in `QSettings` and prefer it on launch.**

`QSettings` is the OS-standard per-user store (plist on macOS, registry on Windows, ini under `~/.config` on Linux — one mechanism, no platform branch, per ADR-050 rule 9). A no-arg `QSettings()` keys off the `QApplication`'s organisation + application names, so a new `mfl_desktop/app_session.py` exposes two tiny helpers (`remember_last_db`, `last_db_path`) with no constants to thread around. `main` now sets `app.setOrganizationName(APP_NAME)` alongside the existing application name.

**The remembered file is recorded whenever a file becomes the live file:**

- at launch, after the opened repo is in hand (`main`), and
- on `File ▸ Open` (`_swap_repository`), pointing at the newly-adopted file.

A loaded dataset / snapshot (`_load_dataset`) deliberately keeps the same working file, so its path is already what's remembered — no change there.

**Launch precedence becomes** (highest first):

1. `--db` — explicit caller intent (unchanged).
2. **last-opened** — the file open at last quit, *if it still exists* (`last_db_path()` returns `None` for a moved/deleted/other-machine path, so we fall through).
3. legacy cwd dev DB — `mfl_dev.mfl` / `mfl_dev.db` (dev convenience for a checked-out repo).
4. appdata default — the OS-standard per-user file, seeded with a starter account if empty.

Last-opened sits **above** the cwd dev convenience so "reopen what I was working on" wins, while a fresh checkout with nothing recorded still gets the dev file (and that first launch records it, so it stays consistent). A remembered file that exists but won't open (corrupt / not an MFL DB) falls back to the default rather than stranding the launch on a dead file.

---

## Consequences

- Quit-and-relaunch reopens the file you were in — the reported annoyance is gone.
- The fix is `QSettings`-only: no schema change, no migration, nothing written into any `.mfl`.
- `app_session.py` is a small, dependency-light home for future app-level state (window geometry, a recent-files list) without reopening this decision.
- One behavioural note for developers: once you've opened any file, that file (not the cwd `mfl_dev.mfl`) reopens by default; pass `--db` to override, or open `mfl_dev.mfl` again to make it the remembered file.

### Rejected alternatives

- **Store the path in the per-file `setting` table** — can't be read before a file is open; wrong layer.
- **A plain dotfile / JSON next to the binary** — re-implements what `QSettings` already does per-OS, and a packaged app's install dir isn't user-writable.
- **A full recent-files (MRU) menu now** — more than the ask; `app_session` leaves room to add it later.
- **Record only on quit (`closeEvent`)** — recording at the moment a file becomes live is simpler and survives a hard crash that skips `closeEvent`.

---

## Verification

Offscreen (isolated `QSettings` ini): round-trip of `remember_last_db` / `last_db_path`; a relative path is stored absolute. Offscreen Qt: constructing `RegisterWindow` on file A then driving `_swap_repository` to file B updates the remembered file to B, and importing `__main__` (which pulls in `register_window` + `app_session`) raises no circular import.

---

## Amendment — 2026-06-21 (transient-absence robustness)

**Reported:** after working in an **iCloud-Drive** `.mfl` and quitting, the next launch opened the default `mfl_dev.mfl` instead of the remembered file. The recorded pointer was correct, but the launch resolver had two destructive interactions with files that can be *temporarily* absent (iCloud/Dropbox offload, removable/network drives):

1. **`last_db_path()` filtered on `exists()`**, returning `None` for a not-yet-materialised iCloud file — indistinguishable from "no pointer set" — so the resolver fell through to the default.
2. The launch then **re-recorded the fallback default**, *clobbering* the pointer. So a single transient absence permanently lost the user's last file, even after iCloud rehydrated it. (Compounded by `Repository()` bootstrapping a fresh DB at any missing path, which would silently *recreate* a remembered file that was genuinely gone.)

**Fix:** `last_db_path()` now returns the recorded path **without an existence check** — "set but not here right now" is distinct from "unset". The resolver does the existence check itself and tracks a `fell_back_from_remembered` flag: when a remembered pointer exists but the file can't be used this launch (absent, or present-but-won't-open), it opens the default **for this session only and does not overwrite the pointer**. The next launch retries once the file is back. A genuinely-corrupt remembered file still falls back the same way (pointer kept, user re-points via File ▸ Open). Existence is checked before handing the path to `Repository`, so a missing file is never recreated.

**Verified** offscreen: a recorded iCloud-style file, then "evicted" (deleted) → launch opens the legacy default, `fell_back` true, **pointer preserved** (not clobbered); file restored → next launch reopens it; a missing remembered file is never recreated by the resolver.
