"""Database bootstrap and migration runner.

Applies any migration files under mfl_desktop/migrations/ that haven't
already been recorded in schema_version. Migrations are SQL files named
NNNN_<short_description>.sql and applied in lexical order.

The runner manages schema_version; migration files do not.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _migrations_dir() -> Path:
    """Locate the bundled migration SQL files (ADR-104).

    In a source checkout they sit beside the package (``mfl_desktop/
    migrations``). In a frozen PyInstaller build the spec bundles them to
    ``<_MEIPASS>/mfl_desktop/migrations`` — resolve against ``sys._MEIPASS``
    there so the packaged app can bootstrap a database (without this the
    frozen app finds no migrations and every DB stays empty)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "mfl_desktop" / "migrations"
    return Path(__file__).parent.parent / "migrations"


MIGRATIONS_DIR = _migrations_dir()


def bootstrap(db_path: Path) -> None:
    """Apply any pending migrations to the SQLite database at db_path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_schema_version_table(conn)
        applied = _applied_versions(conn)
        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(path.stem.split("_", 1)[0])
            if version in applied:
                continue
            logger.info(f"Applying migration {path.name}")
            sql = path.read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,),
            )
            conn.commit()
    finally:
        conn.close()


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    cur = conn.execute("SELECT version FROM schema_version")
    return {row[0] for row in cur}
