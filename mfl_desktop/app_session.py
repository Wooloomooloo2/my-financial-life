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

from PySide6.QtCore import QSettings

# Namespaced so future app-level keys (window geometry, recent-files list, …)
# can live alongside it without collision.
_LAST_DB_KEY = "session/last_db_path"
# Licensing is app-level, not per-file (ADR-079): one purchased key + one trial
# clock cover every `.mfl` the user opens, so they live here, not in the
# per-file `setting` table.
_LICENSE_KEY = "license/key"
_TRIAL_START_KEY = "license/trial_start"


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
    """The most-recently-opened database, or ``None`` if nothing is recorded
    or the recorded file no longer exists (moved / deleted / different
    machine). Callers fall through to their normal default in that case."""
    try:
        raw = QSettings().value(_LAST_DB_KEY)
    except Exception:
        return None
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.exists() else None


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
