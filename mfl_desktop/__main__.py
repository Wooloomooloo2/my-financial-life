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

from PySide6.QtCore import QThreadPool, QRunnable

from mfl_desktop.db.repository import Repository
from mfl_desktop.fx import refresh_latest_into
from mfl_desktop.prices import (
    backfill_missing_history_into,
    refresh_latest_prices_into,
)
from mfl_desktop.ui.register_window import RegisterWindow
from mfl_desktop.ui.theme import apply_theme


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


DEFAULT_DB = Path("mfl_dev.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfl_desktop")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--account-iri",
                        help="Open this account (default: first in DB)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(
            f"Database not found at {args.db}.\n"
            "Create one with: python -m mfl_desktop.cli init",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    apply_theme(app)
    repo = Repository(args.db)

    account_iri = args.account_iri
    if account_iri is None:
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
        account_iri = row["iri"]

    win = RegisterWindow(repo, account_iri)
    win.show()

    # Background launch refresh of FX rates (ADR-035). No-op when no API
    # key is set, when the last refresh was less than 24h ago, or when
    # there are no non-USD accounts to fetch rates for.
    QThreadPool.globalInstance().start(_FxRefreshRunnable(args.db))

    # Background launch refresh of security prices (ADR-044). No-op when no
    # Tiingo key is set, when the last refresh was < 24h ago, or when no
    # securities carry a ticker symbol.
    QThreadPool.globalInstance().start(_PriceRefreshRunnable(args.db))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
