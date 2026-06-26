"""Database resolution for launch (ADR-109).

Decides *which* ``.mfl`` the app opens, and — crucially — never silently opens a
different file when the user's configured main file is temporarily unreadable
(the cloud-eviction bug ADR-109 fixes). Kept out of ``__main__`` so the
resolution algorithm is small and unit-testable without spinning up a real
``QApplication``/window.

The authoritative "main file" is the most-recently-opened path
(``app_session.last_db_path``) — it already means "reopen the file I was last
editing" and is updated whenever the user opens or relocates a file. Precedence:

1. ``--db PATH``      — explicit caller intent (dev / scripted).
2. main-file pointer  — if set: make it available (waiting out a cloud download),
                        else show the recovery dialog. **Never** fall through to a
                        different file behind the user's back.
3. legacy cwd db      — only when *no* pointer is set (fresh checkout dev convenience).
4. first-run default  — a fresh file in a visible ``~/Documents/My Financial Life``.

Module-level imports are real (the runtime always has PySide6); tests monkeypatch
the referenced names (``last_db_path``, ``cloud``, ``Repository``,
``first_run_default_path``) on this module.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from mfl_desktop import cloud
from mfl_desktop.app_session import last_db_path, remember_last_db
from mfl_desktop.db.repository import Repository

# Historical cwd dev databases (canonical .mfl first, then the older .db), used
# only when no main-file pointer is set — see ADR-016/050. Moved here from
# ``__main__`` so resolution is self-contained.
LEGACY_DB_CANDIDATES = [Path("mfl_dev.mfl"), Path("mfl_dev.db")]
DEFAULT_DB_FILENAME = "MyFinancialLife.mfl"


def first_run_default_path() -> Path:
    """Where a brand-new user's file is created (ADR-109).

    A *visible* ``My Financial Life`` folder under the OS Documents location
    (``~/Documents`` on macOS, ``Documents`` on Windows) — discoverable and
    user-controlled, unlike the hidden app-data folder the app used to default
    to. ``QStandardPaths.DocumentsLocation`` keeps it cross-platform with no
    branch."""
    from PySide6.QtCore import QStandardPaths

    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return Path(docs) / "My Financial Life" / DEFAULT_DB_FILENAME


@dataclass(frozen=True)
class Resolution:
    """The outcome of :func:`resolve_database`.

    ``exit_code`` is non-None only when the process should stop without opening a
    window (``--db`` missing → 1; user closed the recovery dialog → 0); in that
    case ``db_path`` is None. Otherwise ``db_path`` is the file to open and
    ``seed_if_empty`` says whether to seed a starter account into an empty one."""

    db_path: Optional[Path] = None
    seed_if_empty: bool = False
    exit_code: Optional[int] = None


def _opens(path: Path) -> bool:
    """True if ``path`` opens as a real database. Opened read-and-closed purely
    to verify — only ever called after :func:`cloud.is_available`, so it never
    creates a missing file."""
    try:
        Repository(path).close()
        return True
    except Exception:
        return False


def resolve_database(
    args,
    *,
    pump: Optional[Callable[[], None]] = None,
    dialog_factory: Callable[[Path, str], "object"],
) -> Resolution:
    """Resolve which database to open (ADR-109). See module docstring.

    ``pump`` is forwarded to :func:`cloud.ensure_available` so a splash keeps
    painting while a cloud file downloads. ``dialog_factory(path, reason)`` builds
    the recovery dialog (injected so this stays testable); the returned object
    must expose ``run() -> RecoveryChoice``."""
    # 1. Explicit --db: use it, but don't silently create a missing one.
    if getattr(args, "db", None) is not None:
        db_path = Path(args.db)
        if not db_path.exists():
            print(
                f"Database not found at {db_path}.\n"
                "Create one with: python -m mfl_desktop.cli init",
                file=sys.stderr,
            )
            return Resolution(exit_code=1)
        return Resolution(db_path=db_path)

    pointer = last_db_path()

    # 3 & 4. No pointer → fresh checkout dev file, else a new first-run file.
    if pointer is None:
        legacy = next((p for p in LEGACY_DB_CANDIDATES if p.exists()), None)
        if legacy is not None:
            return Resolution(db_path=legacy)
        return Resolution(db_path=first_run_default_path(), seed_if_empty=True)

    # 2. Pointer set → make it available or recover explicitly. Never swap silently.
    while True:
        if cloud.ensure_available(pointer, pump=pump) and _opens(pointer):
            return Resolution(db_path=pointer)
        reason = "unreadable" if cloud.is_available(pointer) else "unavailable"
        choice = dialog_factory(pointer, reason).run()
        if choice.retry:
            continue
        if choice.open_other and choice.path is not None:
            remember_last_db(choice.path)
            return Resolution(db_path=Path(choice.path))
        if choice.new_file and choice.path is not None:
            remember_last_db(choice.path)
            return Resolution(db_path=Path(choice.path), seed_if_empty=True)
        # Dialog closed with no choice → the user wants to quit.
        return Resolution(exit_code=0)
