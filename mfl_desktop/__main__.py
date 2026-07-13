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

import shiboken6
from PySide6.QtWidgets import QApplication

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from mfl_desktop import launch, sandbox
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


class _LaunchRefreshSignals(QObject):
    """Announces that a launch refresh actually wrote something (ADR-156)."""
    wrote = Signal()


class _LaunchRefreshRunnable(QRunnable):
    """Shared plumbing for the two background launch refreshes.

    ADR-156: each writes through its **own** sqlite connection, which the main
    thread's ``data_generation`` cannot reliably see (``total_changes`` is
    per-connection). Before ADR-156 that didn't matter — the window rebuilt Home
    from scratch on the next activation regardless, so the new prices appeared by
    luck. Now that derived values are cached, a writer on another connection has
    to say so, or the user sees pre-refresh figures until their next edit.

    So each subclass reports whether its pass changed any rows, and only then is
    the main thread asked to invalidate and redraw. The emit is guarded because
    the app can be torn down while a launch refresh is still in flight."""

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self.signals = _LaunchRefreshSignals()

    def _work(self, bg: Repository) -> None:
        raise NotImplementedError

    def run(self) -> None:
        # A dedicated Repository connection — the main-thread Repository's
        # sqlite3 connection isn't safe to share across threads, and the launch
        # refresh is its own atomic unit.
        wrote = False
        try:
            bg = Repository(self._db_path)
            try:
                before = bg.total_writes
                self._work(bg)
                wrote = bg.total_writes != before
            finally:
                bg.close()
        except Exception:
            # Swallow — the user can always hit Refresh Now manually.
            return
        if not wrote:
            return
        try:
            if shiboken6.isValid(self.signals):
                self.signals.wrote.emit()
        except RuntimeError:
            pass   # app torn down mid-refresh; nobody left to tell


class _FxRefreshRunnable(_LaunchRefreshRunnable):
    """Background launch refresh (ADR-035). Once-per-day at most; silent
    on failure so a missing API key or a flaky network never blocks the
    launch path."""

    def _work(self, bg: Repository) -> None:
        refresh_latest_into(bg)


class _PriceRefreshRunnable(_LaunchRefreshRunnable):
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

    def _work(self, bg: Repository) -> None:
        bg.seed_prices_from_transactions()
        backfill_missing_history_into(bg)
        refresh_latest_prices_into(bg)


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


def _open_repository_with_fallback(db_path: Path, first_run_default: bool):
    """Open (creating if needed) the resolved database, falling back to the app
    container if a sandbox first-run location the user picked can't be written
    (ADR-125).

    A directory chosen via the powerbox should be writable, but if the grant
    doesn't extend to creating the file (or SQLite's WAL sidecars), creating the
    Repository raises ``sqlite3.OperationalError: unable to open database file``.
    Rather than crash, log the path, warn the user, and use the always-writable
    container default so the app still launches."""
    import logging
    log = logging.getLogger("mfl")
    log.info(
        "Opening database at %s (first_run_default=%s sandboxed=%s)",
        db_path, first_run_default, sandbox.is_sandboxed(),
    )
    try:
        return Repository(db_path)
    except Exception as e:
        fallback = launch.first_run_default_path()
        if Path(db_path) == fallback:
            raise
        log.warning("Could not open DB at %s (%s) — falling back to %s",
                    db_path, e, fallback)
        from PySide6.QtWidgets import QMessageBox
        from mfl_desktop.ui.splash import dismiss_active_splash
        dismiss_active_splash()  # ADR-132: don't let the splash hide this warning
        QMessageBox.warning(
            None,
            "Couldn't use that location",
            "My Financial Life couldn't create your data file in the folder you "
            "chose, so it's using its private app folder for now. You can move "
            "it later from Manage Data ▸ Locations.",
        )
        return Repository(fallback)


def _prompt_first_run_location(default_path: Path) -> Path:
    """Sandbox first run (ADR-125): ask the user which folder to keep their new
    ``.mfl`` in, so it lives somewhere visible and backup-able rather than the
    hidden sandbox container.

    Shown over the splash (no parent). The folder dialog is the macOS powerbox,
    so picking a folder grants this session read/write to it — enough to create
    the file there; ``remember_last_db`` then mints the security-scoped bookmark
    that reopens it next launch. Cancelling falls back to ``default_path`` (the
    container default), so a hesitant user still gets a working app."""
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from mfl_desktop.ui.splash import dismiss_active_splash

    dismiss_active_splash()  # ADR-132: the splash would otherwise hide these prompts
    QMessageBox.information(
        None,
        "Choose where to keep your data",
        "Pick a folder for your My Financial Life data file — somewhere you can "
        "find and back up, like your Documents or an iCloud Drive folder.\n\n"
        "You can move it later from Manage Data.",
    )
    chosen = QFileDialog.getExistingDirectory(
        None,
        "Choose a folder for your data file",
        str(Path.home() / "Documents"),
    )
    import logging
    logging.getLogger("mfl").info("First-run folder picker returned: %r", chosen)
    if not chosen:
        return default_path
    return Path(chosen) / launch.DEFAULT_DB_FILENAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfl_desktop")
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite database path (default: the per-user "
                             "application-data location, or ./mfl_dev.mfl / "
                             "./mfl_dev.db when that legacy dev file is present)")
    parser.add_argument("--account-iri",
                        help="Open this account (default: first in DB)")
    args = parser.parse_args(argv)

    # ADR-126: point OpenSSL at certifi's CA bundle before any HTTPS call.
    # In the frozen macOS bundle the default trust store does not resolve, so
    # without this every Tiingo/FX/feed refresh fails cert verification
    # ("unable to get local issuer certificate"). No-op in dev / on Windows.
    from mfl_desktop.net_certs import ensure_ca_bundle
    ensure_ca_bundle()

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
    #
    # ADR-132: the always-on-top splash hides a no-parent modal dialog behind it
    # on Windows, so the recovery prompt was invisible and the app looked frozen.
    # Drop the splash the instant we need to block on that dialog.
    def _recovery_dialog(path, reason):
        from mfl_desktop.ui.splash import dismiss_active_splash
        dismiss_active_splash()
        return FileRecoveryDialog(path, reason)

    res = launch.resolve_database(
        args, pump=app.processEvents, dialog_factory=_recovery_dialog,
    )
    if res.exit_code is not None:
        return res.exit_code
    db_path = res.db_path
    seed_if_empty = res.seed_if_empty

    # Sandbox first run (ADR-125): before creating the brand-new file, let the
    # user place it in a real, visible folder (held thereafter via a security-
    # scoped bookmark) instead of the hidden sandbox container. Only for the
    # unattended first-run default, and only when sandboxed — the dev / direct
    # build keeps the ADR-109 ~/Documents default with no extra prompt.
    if res.first_run_default and sandbox.is_sandboxed():
        db_path = _prompt_first_run_location(db_path)

    # Every resolved path is now an explicit choice (the pointer, --db, the
    # first-run default, or a file the user picked in recovery), so we always
    # record it as the file to reopen next launch. File ▸ Open / Locations update
    # it at runtime. Repository() bootstraps the schema + mkdirs the parent.
    repo = _open_repository_with_fallback(db_path, res.first_run_default)
    db_path = repo.db_path  # may differ if a sandbox location fell back
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
    #
    # Background launch refresh of security prices (ADR-044). No-op when no
    # Tiingo key is set, when the last refresh was < 24h ago, or when no
    # securities carry a ticker symbol.
    #
    # ADR-156: both write on their own connection, so each tells the window when
    # it actually wrote — otherwise the cached account values would keep showing
    # pre-refresh prices. Queued (worker → main thread), and only when there is
    # something to show, so a no-op launch costs nothing.
    for runnable in (_FxRefreshRunnable(db_path), _PriceRefreshRunnable(db_path)):
        runnable.signals.wrote.connect(win.on_background_data_written)
        QThreadPool.globalInstance().start(runnable)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
