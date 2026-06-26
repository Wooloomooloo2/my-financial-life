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

# Namespaced so future app-level keys (window geometry, recent-files list, …)
# can live alongside it without collision.
_LAST_DB_KEY = "session/last_db_path"
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


def remember_last_db(path: Path | str) -> None:
    """Record ``path`` as the database to reopen on the next launch.

    Best-effort: a failure to persist must never break opening a file, so any
    error is swallowed. Stores the resolved absolute path so a later launch
    from a different working directory still finds it."""
    try:
        QSettings().setValue(_LAST_DB_KEY, str(Path(path).resolve()))
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
