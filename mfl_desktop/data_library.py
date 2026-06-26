"""Test-data library — named save/load of whole ``.mfl`` datasets (ADR-059).

``File ▸ Save Copy As…`` / ``File ▸ Open…`` (the ADR-016 live file plus ADR-057
snapshots) already give blind-file-picker save and open. This module backs a
*visual* library screen for juggling whole datasets — the workflow the owner
needs while developing against many test fixtures (an empty file, a
multi-currency file, a card-payoff scenario, …).

Two ideas, both at-rest file operations with no Qt and no live connection, so
they stay pure and testable:

- **Saved datasets** live in a ``Library/`` folder beside the live database. A
  save is an atomic backup copy (via the live ``Repository.save_copy``); the
  resulting library entry is an ordinary, schema-upgradeable ``.mfl``.
- **Loading a working copy** clones a library entry (or an ADR-057 snapshot)
  *onto the live working file* so the saved original stays pristine — load a
  baseline, edit it, reload it clean, and the library copy never changed. The
  clone opens the source **read-only** (so a load can never migrate or mutate
  the pristine copy) and writes through a temp file + atomic replace, so a
  failure mid-clone leaves the working file intact rather than half-written.

``Library/`` sits beside the live db, parallel to ADR-057's ``Snapshots/``, so
both follow the live file across a load.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mfl_desktop import snapshots

LIBRARY_DIRNAME = "Library"


@dataclass(frozen=True)
class DataFile:
    """One row in the library screen — a saved dataset or a snapshot."""

    path: Path
    name: str            # display name (library: user stem; snapshot: stem)
    saved_at: datetime   # from the file's mtime
    size: int            # bytes
    kind: str            # "current" | "saved" | "snapshot"


def library_dir(db_path: Path | str) -> Path:
    """The ``Library/`` folder beside the live database."""
    return Path(db_path).resolve().parent / LIBRARY_DIRNAME


def sanitize_name(name: str) -> str:
    """Reduce a user-typed dataset name to a safe filename stem.

    Strips path separators and characters illegal on Windows/macOS, collapses
    runs of whitespace, and trims. Returns ``""`` when nothing usable remains
    (the caller rejects an empty result rather than writing a nameless file).
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned


def library_path(db_path: Path | str, name: str) -> Path:
    """Destination ``.mfl`` path in the library for a (sanitized) dataset name."""
    return library_dir(db_path) / f"{sanitize_name(name)}.mfl"


def _mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.fromtimestamp(0)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def current_file(db_path: Path | str) -> DataFile:
    """The live working file itself, as a distinguished ``kind="current"`` row.

    Surfaced at the top of the Saved-datasets list (ADR-109) so the user can
    always *see* which file they're actually editing — the No.1 source of the
    "which file is this?" confusion was that the working file appeared nowhere in
    the library. It is not a saved copy and must never be deletable/renamable;
    the dialog enforces that off the ``kind``."""
    p = Path(db_path).resolve()
    return DataFile(p, p.stem, _mtime(p), _size(p), "current")


def list_saved(db_path: Path | str) -> list[DataFile]:
    """Saved datasets in the library, newest first."""
    folder = library_dir(db_path)
    if not folder.is_dir():
        return []
    files = [
        DataFile(p, p.stem, _mtime(p), _size(p), "saved")
        for p in folder.glob("*.mfl")
    ]
    return sorted(files, key=lambda f: f.saved_at, reverse=True)


def list_snapshots(db_path: Path | str) -> list[DataFile]:
    """ADR-057 snapshots beside the live db, newest first."""
    files = [
        DataFile(p, p.stem, _mtime(p), _size(p), "snapshot")
        for p in snapshots.existing_snapshots(db_path)
    ]
    return sorted(files, key=lambda f: f.saved_at, reverse=True)


def _wal(p: Path) -> Path:
    return p.with_name(p.name + "-wal")


def _shm(p: Path) -> Path:
    return p.with_name(p.name + "-shm")


def delete_file(path: Path | str) -> None:
    """Delete a saved file and any stray WAL/SHM sidecars.

    Sidecar removal is best-effort, but the main ``unlink`` is allowed to raise
    so the caller can report a failure the user should know about.
    """
    path = Path(path)
    for sidecar in (_wal(path), _shm(path)):
        try:
            sidecar.unlink()
        except OSError:
            pass
    path.unlink()


def rename_saved(path: Path | str, new_name: str) -> Path:
    """Rename a saved dataset, returning the new path.

    Raises ``ValueError`` if ``new_name`` sanitizes to empty, ``FileExistsError``
    if the target name is already taken.
    """
    path = Path(path)
    stem = sanitize_name(new_name)
    if not stem:
        raise ValueError("empty name")
    dest = path.with_name(f"{stem}.mfl")
    if dest == path:
        return path
    if dest.exists():
        raise FileExistsError(dest)
    path.rename(dest)
    return dest


def clone_database(src: Path | str, dest: Path | str) -> None:
    """Clone ``src`` onto ``dest`` as a self-contained ``.mfl``, leaving ``src``
    untouched.

    Used to load a saved dataset/snapshot onto the live working file. The source
    is opened **read-only** (``mode=ro``) so loading never migrates or mutates
    the pristine library copy. The clone is written to a temp file via SQLite's
    online backup API, then atomically ``os.replace``d over ``dest`` — so a
    failure mid-clone leaves the existing working file intact rather than
    half-written. Any stale WAL/SHM sidecars beside ``dest`` are removed first so
    the reopened file can't recover frames left by the previous occupant.
    """
    src, dest = Path(src).resolve(), Path(dest)
    tmp = dest.with_name(dest.name + ".loading.tmp")
    for stray in (tmp, _wal(tmp), _shm(tmp)):
        try:
            stray.unlink()
        except OSError:
            pass
    tmp.parent.mkdir(parents=True, exist_ok=True)
    # `file:…?mode=ro` URI; as_uri() handles spaces/percent-encoding for us.
    ro = sqlite3.connect(f"{src.as_uri()}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(tmp)
        try:
            ro.backup(out)
        finally:
            out.close()
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        ro.close()
    # The temp clone is complete and closed; swap it in. Drop the destination's
    # own sidecars first so the reopened db starts from this file alone.
    for sidecar in (_wal(dest), _shm(dest)):
        try:
            sidecar.unlink()
        except OSError:
            pass
    os.replace(tmp, dest)
