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

from mfl_desktop import launch
from mfl_desktop.app_session import remember_last_db
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.file_recovery_dialog import FileRecoveryDialog
from mfl_desktop.fx import refresh_latest_into
from mfl_desktop.prices import (
    backfill_missing_history_into,
    refresh_latest_prices_into,
)
from mfl_desktop.ui.register_window import RegisterWindow
from mfl_desktop.ui.theme import apply_theme, SETTING_KEY as THEME_SETTING_KEY
from mfl_desktop.version import __version__


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
# Database resolution (which file to open, legacy candidates, the first-run
# default, and the never-silently-swap cloud-recovery loop) lives in
# ``mfl_desktop.launch`` (ADR-109) so it's unit-testable without a window.


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
    app.setApplicationVersion(__version__)  # ADR-079: surfaced in About + diagnostics

    # ADR-101: the brand app icon (window / taskbar / dock). The packaged
    # bundle icon (.icns/.ico) is set by the build step; this is the runtime
    # window icon. No-op if the asset is missing.
    from mfl_desktop import resources
    app.setWindowIcon(resources.app_icon())

    # ADR-103: branded splash, shown before the slow launch work (DB open +
    # migrations + window build) and closed when the main window appears.
    from mfl_desktop.ui.splash import make_splash
    splash = make_splash()
    splash.show()
    app.processEvents()  # paint it now, before we block on the DB

    # ADR-099: local rotating log + last-resort crash handler. Set up now the
    # app name (hence the log dir) is known, before any window or DB work, so
    # an early failure is still captured. No telemetry — the log is local only.
    from mfl_desktop import diagnostics
    diagnostics.setup_logging()
    diagnostics.install_excepthook()

    # Resolve which database to open (ADR-109; supersedes the ADR-092 fall-back).
    # The resolver never silently swaps to a different file: a configured main
    # file that's temporarily unreadable (cloud-evicted, drive offline) is waited
    # out / re-downloaded, then escalated to an explicit recovery dialog shown
    # over the splash. ``pump`` keeps the splash painting during a cloud download.
    res = launch.resolve_database(
        args, pump=app.processEvents, dialog_factory=FileRecoveryDialog,
    )
    if res.exit_code is not None:
        return res.exit_code
    db_path = res.db_path
    seed_if_empty = res.seed_if_empty

    # Every resolved path is now an explicit choice (the pointer, --db, the
    # first-run default, or a file the user picked in recovery), so we always
    # record it as the file to reopen next launch. File ▸ Open / Locations update
    # it at runtime. Repository() bootstraps the schema + mkdirs the parent.
    repo = Repository(db_path)
    remember_last_db(db_path)

    # ADR-076: apply the persisted light/dark theme now the DB is open (before
    # any window is shown, so there's no flash). Default light.
    apply_theme(app, repo.get_setting(THEME_SETTING_KEY, "light") or "light")

    account_iri = args.account_iri
    just_seeded = False
    if account_iri is None:
        row = repo.connection.execute(
            "SELECT iri FROM account ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None and seed_if_empty:
            _seed_starter_db(repo)
            just_seeded = True
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
    splash.finish(win)  # ADR-103: close the splash once the window is up
    # ADR-109: guarantee the WAL is folded into the .mfl on *any* exit, not just
    # a window close — Cmd/Ctrl-Q routes through aboutToQuit, not closeEvent.
    # Idempotent with closeEvent (see RegisterWindow._flush_and_close).
    app.aboutToQuit.connect(win.on_about_to_quit)

    # First-run onboarding (ADR-098): only when we just seeded a brand-new
    # file this launch. Lets the user pick a base currency + name the first
    # account, and optionally jump straight into importing a statement.
    if just_seeded:
        from mfl_desktop.ui.first_run_dialog import FirstRunDialog
        welcome = FirstRunDialog(repo, parent=win)
        welcome.exec()
        win.refresh_after_first_run()
        if welcome.wants_import():
            iri = welcome.starter_account_iri()
            if iri is not None:
                win.start_first_run_import(iri)

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
