"""Entry point for the desktop application.

Usage:
    python -m mfl_desktop [--db PATH] [--account-iri IRI]

If the database doesn't exist, points the user at the CLI to create it.
If no account is specified, opens the register on the first account in the DB.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from PySide6.QtCore import QThreadPool, QRunnable, QStandardPaths

from mfl_desktop.app_session import last_db_path, remember_last_db
from mfl_desktop.db.repository import Repository
from mfl_desktop.fx import refresh_latest_into
from mfl_desktop.prices import (
    backfill_missing_history_into,
    refresh_latest_prices_into,
)
from mfl_desktop.ui.register_window import RegisterWindow
from mfl_desktop.ui.theme import apply_theme, SETTING_KEY as THEME_SETTING_KEY


class _FxRefreshRunnable(QRunnable):
    """Background launch refresh (ADR-035). Once-per-day at most; silent
    on failure so a missing API key or a flaky network never blocks the
    launch path."""

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    def run(self) -> None:
        # Use a dedicated Repository connection — the main-thread
        # Repository's sqlite3 connection isn't safe to share across
        # threads, and the launch refresh is its own atomic unit.
        try:
            bg = Repository(self._db_path)
            try:
                refresh_latest_into(bg)
            finally:
                bg.close()
        except Exception:
            # Swallow — the user can always hit Refresh Now manually.
            pass


class _PriceRefreshRunnable(QRunnable):
    """Background launch refresh of security prices (ADR-044/047). Mirrors the
    FX runnable: own Repository connection, silent on failure (missing Tiingo
    key / flaky network never blocks launch).

    Three steps, cheapest first (ADR-047):
      1. seed_prices_from_transactions — instant, no network, no key needed;
         prices the untickered majority from their own trades (and catches up
         the whole history on first launch).
      2. backfill_missing_history_into — auto-fetch full history for any
         tickered security that doesn't have it yet (newly tickered/imported);
         self-limiting, so it's not a per-launch re-fetch of everything.
      3. refresh_latest_prices_into — today's close (own 24h throttle).
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    def run(self) -> None:
        try:
            bg = Repository(self._db_path)
            try:
                bg.seed_prices_from_transactions()
                backfill_missing_history_into(bg)
                refresh_latest_prices_into(bg)
            finally:
                bg.close()
        except Exception:
            pass


APP_NAME = "MFL"
# Historical cwd dev databases, preferred in this order: the canonical .mfl
# (ADR-016's save format) first, then the older .db. So a repo checked out on
# any machine opens the live working file with no --db flag. (ADR-050 Tier-2
# amended 2026-06-14: was .db-only, which silently diverged from a .mfl carried
# across machines — the Windows checkout opened a stale mfl_dev.db while the
# real data lived in mfl_dev.mfl.)
LEGACY_DB_CANDIDATES = [Path("mfl_dev.mfl"), Path("mfl_dev.db")]
DEFAULT_DB_FILENAME = "MyFinancialLife.mfl"


def _appdata_db_path() -> Path:
    """The OS-standard default database location (ADR-050 rule 2).

    Resolves to ``~/Library/Application Support/MFL`` on macOS,
    ``%APPDATA%\\MFL`` on Windows, ``~/.local/share/MFL`` on Linux — one
    ``QStandardPaths`` call, no platform branch (rule 9). Requires a
    QApplication whose applicationName is set (``APP_NAME`` drives the
    trailing ``MFL`` folder)."""
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return Path(base) / DEFAULT_DB_FILENAME


def _seed_starter_db(repo: Repository) -> None:
    """Seed the first person + cash account into a freshly-bootstrapped DB.

    Mirrors ``mfl_desktop.cli.cmd_init`` so a packaged user with no CLI gets a
    working, empty register on first launch instead of the "run the CLI" error.
    Consistent with ADR-016's auto-commit model: the new file is valid the
    moment it is written — there is no separate "Save"."""
    repo.connection.execute(
        "INSERT INTO person (iri, name, base_currency) VALUES (?, ?, ?)",
        ("mrl:Person_1", "Me", "GBP"),
    )
    repo.connection.execute(
        "INSERT INTO account (iri, name, type, family, currency) "
        "VALUES (?, ?, ?, ?, ?)",
        ("mrl:CashAccount_1", "Current account", "cash_std", "cash", "GBP"),
    )
    repo.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfl_desktop")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite database path (default: the per-user "
                             "application-data location, or ./mfl_dev.mfl / "
                             "./mfl_dev.db when that legacy dev file is present)")
    parser.add_argument("--account-iri",
                        help="Open this account (default: first in DB)")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv)
    # Set before any QStandardPaths lookup — it is what makes AppDataLocation
    # resolve to the trailing "MFL" folder (ADR-050 rule 2). The same two names
    # are what a no-arg QSettings() keys off for app-level state (ADR-092).
    app.setOrganizationName(APP_NAME)
    app.setApplicationName(APP_NAME)

    # Resolve which database to open (ADR-050 rule 2 + ADR-016 + ADR-092).
    # Precedence, highest first:
    #   1. --db          explicit caller intent
    #   2. last-opened   the file open at last quit (ADR-092), if it still exists
    #   3. legacy cwd db dev convenience for a checked-out repo (mfl_dev.mfl/.db)
    #   4. appdata default  the OS-standard per-user file (seeded if empty)
    seed_if_empty = False
    remembered = None if args.db is not None else last_db_path()
    legacy_db = next((p for p in LEGACY_DB_CANDIDATES if p.exists()), None)
    if args.db is not None:
        # Explicit path: the caller asked for a specific file — don't silently
        # create it; point at the CLI if it's missing (unchanged behaviour).
        db_path = args.db
        if not db_path.exists():
            print(
                f"Database not found at {db_path}.\n"
                "Create one with: python -m mfl_desktop.cli init",
                file=sys.stderr,
            )
            return 1
    elif remembered is not None:
        # Reopen the file the user was working in when they last quit (ADR-092).
        # last_db_path() already confirmed it exists; if it turns out to be
        # unreadable we fall back to the default below.
        db_path = remembered
    elif legacy_db is not None:
        # Dev convenience: a checked-out repo with the historical working DB in
        # cwd keeps launching against it with no --db flag. The canonical .mfl
        # wins over a legacy .db when both are present (ADR-016/050).
        db_path = legacy_db
    else:
        # Default: the OS-standard per-user location. Repository() bootstraps
        # the schema and mkdirs the parent on first run; we own this file, so
        # an empty one gets seeded with a starter account below.
        db_path = _appdata_db_path()
        seed_if_empty = True

    try:
        repo = Repository(db_path)
    except Exception as e:
        if remembered is not None and db_path == remembered:
            # The remembered file exists but won't open (corrupt / not an MFL
            # file). Don't strand the user on a dead launch — fall back to the
            # normal default and let them File ▸ Open the right one.
            db_path = legacy_db if legacy_db is not None else _appdata_db_path()
            seed_if_empty = legacy_db is None
            repo = Repository(db_path)
        else:
            raise

    # Persist the file we actually opened so the next launch reopens it
    # (ADR-092). File ▸ Open updates this again at runtime.
    remember_last_db(db_path)

    # ADR-076: apply the persisted light/dark theme now the DB is open (before
    # any window is shown, so there's no flash). Default light.
    apply_theme(app, repo.get_setting(THEME_SETTING_KEY, "light") or "light")

    account_iri = args.account_iri
    if account_iri is None:
        row = repo.connection.execute(
            "SELECT iri FROM account ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None and seed_if_empty:
            _seed_starter_db(repo)
            row = repo.connection.execute(
                "SELECT iri FROM account ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            print(
                "No accounts in database.\n"
                "Initialise with: python -m mfl_desktop.cli init",
                file=sys.stderr,
            )
            return 1
        # ADR-075: the existence/seed check above stays, but we deliberately
        # leave account_iri as None so the window opens on the Home dashboard
        # (the default landing view) rather than the first account's register.
        # An explicit --account-iri on the CLI still deep-links to that account.

    win = RegisterWindow(repo, account_iri)
    win.show()

    # Background launch refresh of FX rates (ADR-035). No-op when no API
    # key is set, when the last refresh was less than 24h ago, or when
    # there are no non-USD accounts to fetch rates for.
    QThreadPool.globalInstance().start(_FxRefreshRunnable(db_path))

    # Background launch refresh of security prices (ADR-044). No-op when no
    # Tiingo key is set, when the last refresh was < 24h ago, or when no
    # securities carry a ticker symbol.
    QThreadPool.globalInstance().start(_PriceRefreshRunnable(db_path))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
