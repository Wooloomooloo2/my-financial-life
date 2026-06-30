"""Cross-file application session state (ADR-092).

The per-file ``setting`` table lives *inside* each ``.mfl``, so it can't
answer "which file should I open?" — that has to be known before any file is
opened. This module persists that kind of app-level, cross-file state in
``QSettings`` (the OS-standard per-user store: a plist on macOS, the registry
on Windows, an ini under ``~/.config`` on Linux — one mechanism, no platform
branch, per ADR-050 rule 9).

Currently just the most-recently-opened database, so quitting and relaunching
reopens the file you were working in rather than the default. ``QSettings()``
with no arguments picks up the organisation + application names set on the
``QApplication`` at startup, so callers need no constants.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, QStandardPaths

from mfl_desktop import sandbox

# Namespaced so future app-level keys (window geometry, recent-files list, …)
# can live alongside it without collision.
_LAST_DB_KEY = "session/last_db_path"
# A security-scoped bookmark of the same file (ADR-125), stored *alongside* the
# plain path above and written only in the macOS App Sandbox build. Inside the
# sandbox a bare path is unreachable on a later launch — only a bookmark can
# re-grant access — but the plain path stays the primary pointer for every
# other build, so the resolver's logic (and its tests) are unchanged: this key
# only adds the sandbox the access a path alone can't give (see
# ``begin_main_file_access``).
_LAST_DB_BOOKMARK_KEY = "session/last_db_bookmark"
# Licensing is app-level, not per-file (ADR-079): one purchased key + one trial
# clock cover every `.mfl` the user opens, so they live here, not in the
# per-file `setting` table.
_LICENSE_KEY = "license/key"
_TRIAL_START_KEY = "license/trial_start"
# Where the rotating snapshot backups live (ADR-109). This is the *parent* of
# the ``MFL Snapshots/`` folder. It is app-level, not per-file: a filesystem
# path is a per-machine storage decision, and storing it inside the `.mfl`
# would break the moment that `.mfl` is the very thing synced between a Mac
# (``/Users/…``) and a PC (``C:\\Users\\…``). The retention *policy* (the GFS
# knobs) stays per-file — see ``snapshots.py``.
_SNAPSHOTS_ROOT_KEY = "locations/snapshots_root"
# Where the data-library ``MFL Library/`` folder lives (ADR-125). Like the
# snapshots root it is an app-level, per-machine path that defaults to the OS
# app-data location — under the macOS App Sandbox that resolves to the app
# container, so saved datasets never require write access to a folder *beside* a
# user-chosen ``.mfl`` (which a file-scoped bookmark can't grant). Kept a
# separate key from the snapshots root so the two can diverge if a relocation UI
# is later added (today only the getter exists; the default covers every build).
_LIBRARY_ROOT_KEY = "locations/library_root"


def remember_last_db(path: Path | str) -> None:
    """Record ``path`` as the database to reopen on the next launch.

    Best-effort: a failure to persist must never break opening a file, so any
    error is swallowed. Stores the resolved absolute path so a later launch
    from a different working directory still finds it.

    In the macOS App Sandbox build (ADR-125) it *additionally* mints a
    security-scoped bookmark for the file and stores it under
    ``_LAST_DB_BOOKMARK_KEY`` — that's the only thing that re-grants access to a
    user-chosen file on a later launch inside the sandbox. We have access to
    ``path`` at every call site (it was just opened / created / picked via the
    powerbox), so the bookmark can always be created here. Outside the sandbox
    no bookmark is written (and any stale one is cleared); the plain path above
    remains the sole mechanism."""
    try:
        s = QSettings()
        s.setValue(_LAST_DB_KEY, str(Path(path).resolve()))
        if sandbox.is_sandboxed():
            blob = sandbox.create_security_scoped_bookmark(path)
            if blob:
                s.setValue(_LAST_DB_BOOKMARK_KEY, blob)
            else:
                s.remove(_LAST_DB_BOOKMARK_KEY)
        else:
            s.remove(_LAST_DB_BOOKMARK_KEY)
    except Exception:
        pass


def last_db_path() -> Optional[Path]:
    """The most-recently-opened database path as recorded, or ``None`` if
    nothing is recorded.

    Deliberately does **not** check existence (ADR-092 amendment): a recorded
    file can be *temporarily* absent — an iCloud/Dropbox file not yet
    materialised, a removable/network drive not mounted — and the launch
    resolver must distinguish "no pointer set" from "pointer set but the file
    isn't here right now" so a transient absence never causes the pointer to be
    silently overwritten with a fallback default. The caller decides what to do
    when the path doesn't currently exist."""
    try:
        raw = QSettings().value(_LAST_DB_KEY)
    except Exception:
        return None
    if not raw:
        return None
    return Path(str(raw))


def begin_main_file_access() -> Optional["sandbox.ResolvedLocation"]:
    """Resolve the remembered file's security-scoped bookmark and **start**
    access to it (ADR-125), returning the live :class:`sandbox.ResolvedLocation`.

    The caller must keep the returned object referenced for as long as the
    database stays open — security-scoped access ends when it is released
    (``__main__`` holds it on the stack across ``app.exec()``, so it lasts the
    whole session). Returns ``None`` when there's no bookmark (every non-sandbox
    build, and a first launch), or when the bookmark can't be resolved (the file
    was deleted / its volume is offline) — in which case the resolver falls back
    to its plain-path flow (``last_db_path`` → cloud probe → recovery dialog),
    so a moved-away file still escalates to recovery rather than a silent swap.

    ``ResolvedLocation.path`` is authoritative when present: a bookmark tracks a
    file across renames/moves, so it can legitimately differ from the (now
    stale) plain path stored beside it."""
    try:
        raw = QSettings().value(_LAST_DB_BOOKMARK_KEY)
    except Exception:
        return None
    if not raw:
        return None
    loc = sandbox.resolve_security_scoped_bookmark(str(raw))
    if loc is None:
        return None
    loc.start()
    return loc


# ── Snapshot location (ADR-109) ─────────────────────────────────────────────

def snapshots_root() -> Path:
    """The parent folder under which the ``MFL Snapshots/`` backup folder lives.

    Defaults to the OS-standard per-user application-data location (local, never
    cloud-synced) — snapshots are multi-MB full-database copies, and keeping many
    of them beside a cloud-synced ``.mfl`` would bloat the user's cloud storage
    and sync bandwidth. The user can point it anywhere via Manage Data ▸
    Locations (e.g. an external drive, or alongside the file for cross-machine
    recovery)."""
    try:
        raw = QSettings().value(_SNAPSHOTS_ROOT_KEY)
    except Exception:
        raw = None
    if raw:
        return Path(str(raw))
    return Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))


def set_snapshots_root(path: Path | str) -> None:
    """Persist the snapshots parent folder (ADR-109). Best-effort: a failure to
    persist must never break the app. Stores the resolved absolute path."""
    try:
        QSettings().setValue(_SNAPSHOTS_ROOT_KEY, str(Path(path).resolve()))
    except Exception:
        pass


def library_root() -> Path:
    """The parent folder under which the data-library ``MFL Library/`` folder
    lives (ADR-125).

    Defaults to the OS-standard per-user application-data location — the same
    default as :func:`snapshots_root`, and under the macOS App Sandbox the app
    container, so the data library never needs to write beside a user-chosen
    file. Reads the ``locations/library_root`` override if one is ever set
    (there is no relocation UI today; the default serves every build)."""
    try:
        raw = QSettings().value(_LIBRARY_ROOT_KEY)
    except Exception:
        raw = None
    if raw:
        return Path(str(raw))
    return Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))


# ── Licensing (ADR-079) ────────────────────────────────────────────────────

def get_license_key() -> Optional[str]:
    """The installed license key string, or ``None`` if unlicensed."""
    try:
        raw = QSettings().value(_LICENSE_KEY)
    except Exception:
        return None
    return str(raw) if raw else None


def set_license_key(key: Optional[str]) -> None:
    """Persist (or clear, with ``None``) the installed license key."""
    try:
        s = QSettings()
        if key:
            s.setValue(_LICENSE_KEY, key)
        else:
            s.remove(_LICENSE_KEY)
    except Exception:
        pass


def get_trial_start() -> Optional[str]:
    """The recorded ISO date the free trial began, or ``None`` if not yet
    started on this machine."""
    try:
        raw = QSettings().value(_TRIAL_START_KEY)
    except Exception:
        return None
    return str(raw) if raw else None


def set_trial_start(iso_date: str) -> None:
    """Record the trial start date (ISO). Best-effort; first-write-wins is the
    caller's responsibility."""
    try:
        QSettings().setValue(_TRIAL_START_KEY, iso_date)
    except Exception:
        pass
