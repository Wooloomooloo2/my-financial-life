"""Automatic rotating database snapshots (ADR-057).

The live ``.mfl`` auto-commits every edit (ADR-016), so *durability* is already
covered — there is no unsaved state to lose. What auto-commit does NOT protect
against is a *logical* mistake: a bad import, a botched bulk-edit, a regression.
Auto-commit persists those just as faithfully. This module keeps a rotating set
of timestamped copies of the live database in a ``Snapshots/`` folder beside it,
so the user can roll back to an earlier state via **File ▸ Open**.

Snapshots are taken on launch, on a periodic in-session timer, and on clean
close (the orchestration lives in ``register_window``). Each is a full,
self-contained copy written through SQLite's online backup API
(``Repository.save_copy``) — WAL-safe and atomic. Rotation keeps the most recent
``SNAPSHOT_KEEP`` files.

This module is pure and testable: it decides *whether* to snapshot and *where*,
then prunes. ``now`` is injectable so the timestamp logic is deterministic under
test. Everything here is best-effort — a backup that can't be written must never
break the app or block close, so the public entry points swallow failures.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

SNAPSHOT_DIRNAME = "Snapshots"
SNAPSHOT_KEEP = 10
SNAPSHOT_INTERVAL_MIN = 30
# Timestamp embedded in the filename. Fixed-width fields mean a lexical sort of
# the filenames is also a chronological sort — relied on by existing_snapshots.
_STAMP_FMT = "%Y%m%d-%H%M%S"


def snapshot_dir(db_path: Path | str) -> Path:
    """The ``Snapshots/`` folder beside the live database."""
    return Path(db_path).resolve().parent / SNAPSHOT_DIRNAME


def _snapshot_stem(db_path: Path | str) -> str:
    return Path(db_path).stem


def snapshot_path(db_path: Path | str, now: datetime) -> Path:
    """The snapshot filename for ``db_path`` at ``now`` (no collision check)."""
    stem = _snapshot_stem(db_path)
    return snapshot_dir(db_path) / f"{stem}-{now.strftime(_STAMP_FMT)}.mfl"


def existing_snapshots(db_path: Path | str) -> list[Path]:
    """All snapshot files for this database, oldest first.

    Lexical sort == chronological sort thanks to the fixed-width ``_STAMP_FMT``.
    """
    folder = snapshot_dir(db_path)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{_snapshot_stem(db_path)}-*.mfl"))


def _live_mtime(db_path: Path | str) -> float:
    """Newest mtime across the main database file and its WAL sidecar.

    In WAL mode (``repository.py`` opens with ``journal_mode = WAL``) committed
    writes land in ``<db>-wal`` and the main file's mtime only advances on
    checkpoint. So the WAL's mtime — not the main file's — is the true
    'last changed' signal between checkpoints.
    """
    db_path = Path(db_path)
    newest = db_path.stat().st_mtime if db_path.exists() else 0.0
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        newest = max(newest, wal.stat().st_mtime)
    return newest


def _changed_since_last_snapshot(db_path: Path | str) -> bool:
    """True if the live db has been written since the newest snapshot.

    Avoids byte-identical duplicates — e.g. launching right after a clean close
    that already snapshotted, where nothing has changed in between.
    """
    snaps = existing_snapshots(db_path)
    if not snaps:
        return True
    return _live_mtime(db_path) > snaps[-1].stat().st_mtime


def prune(db_path: Path | str, keep: int = SNAPSHOT_KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots. Returns deleted paths.

    Only ever touches files matching this database's snapshot glob, so it can't
    remove an unrelated file the user dropped in the folder.
    """
    snaps = existing_snapshots(db_path)
    if len(snaps) <= keep:
        return []
    doomed = snaps[: len(snaps) - keep]
    for path in doomed:
        try:
            path.unlink()
        except OSError:
            pass
    return doomed


def maybe_snapshot(
    repo,
    *,
    now: datetime | None = None,
    keep: int = SNAPSHOT_KEEP,
    force: bool = False,
) -> Path | None:
    """Write a rotating snapshot of ``repo``'s live database if it has changed.

    Returns the path written, or ``None`` when skipped (nothing changed since the
    last snapshot, unless ``force``) or on any failure. Best-effort by design:
    callers fire this from the launch path, a timer, and ``closeEvent`` and must
    never see it raise.
    """
    now = now or datetime.now()
    try:
        db_path = Path(repo.db_path)
        if not force and not _changed_since_last_snapshot(db_path):
            return None
        dest = snapshot_path(db_path, now)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Two snapshots within the same second (e.g. a manual Save Copy As right
        # as the timer fires) would collide on the filename — bump with a suffix
        # rather than overwrite an existing snapshot.
        if dest.exists():
            i = 1
            while True:
                alt = dest.with_name(f"{dest.stem}-{i}.mfl")
                if not alt.exists():
                    dest = alt
                    break
                i += 1
        repo.save_copy(dest)
        prune(db_path, keep)
        return dest
    except Exception:
        return None
