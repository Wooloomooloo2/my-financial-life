"""Crash logging + exportable diagnostics (ADR-099, P6).

The privacy-friendly minimum the 1.0 backlog asks for: a **local rotating
log file**, a **last-resort crash handler** that records uncaught
exceptions (and tells the user where the log is instead of vanishing), and
an **Export Diagnostics** blob the user can copy/save to attach to a
support email. Nothing leaves the machine unless the user sends it — there
is no telemetry, no network call, no Sentry-style auto-report.

Qt-light: logging + collection are pure Python; only the optional crash
dialog touches Qt, and only if a ``QApplication`` already exists. The log
lives beside the app's data, under the per-user app-data location
(``~/Library/Application Support/MFL/logs`` on macOS, ``%APPDATA%\\MFL\\logs``
on Windows) so it survives across files and works in a packaged build.
"""
from __future__ import annotations

import logging
import logging.handlers
import platform
import sys
from pathlib import Path
from typing import Optional

from mfl_desktop import version

logger = logging.getLogger("mfl")

_LOG_FILENAME = "mfl.log"
_MAX_BYTES = 1_000_000          # ~1 MB per file
_BACKUP_COUNT = 3               # keep mfl.log + .1/.2/.3
_configured = False
_in_excepthook = False          # re-entry guard


def log_dir() -> Path:
    """The directory the log file lives in. Uses Qt's per-user app-data
    location when a QApplication is up (it is, by the time we log), else a
    platform-appropriate fallback so this never throws at import time."""
    try:
        from PySide6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if base:
            return Path(base) / "logs"
    except Exception:
        pass
    # Fallback when Qt isn't available (e.g. early crash / headless).
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MFL" / "logs"
    if sys.platform.startswith("win"):
        import os
        return Path(os.environ.get("APPDATA", Path.home())) / "MFL" / "logs"
    return Path.home() / ".local" / "share" / "MFL" / "logs"


def log_path() -> Path:
    return log_dir() / _LOG_FILENAME


def environment_summary() -> str:
    """One compact, PII-free line describing the running environment —
    reused as the log's startup banner and the diagnostics header."""
    try:
        from PySide6 import __version__ as pyside_ver
        from PySide6.QtCore import qVersion
        qt = f"PySide6 {pyside_ver} / Qt {qVersion()}"
    except Exception:
        qt = "PySide6 (unavailable)"
    return (
        f"{version.APP_NAME} {version.build_string()} · "
        f"Python {platform.python_version()} · {qt} · "
        f"{platform.system()} {platform.release()} ({platform.machine()})"
    )


def setup_logging(level: int = logging.INFO) -> None:
    """Configure file + console logging once. Idempotent — repeated calls
    (e.g. tests) don't stack handlers. A failure to create the log file is
    swallowed: logging must never be the reason the app won't start."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            d / _LOG_FILENAME, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        # No file handler — console only. Don't crash on a read-only home.
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    _configured = True
    logger.info("Started — %s", environment_summary())


def install_excepthook() -> None:
    """Route uncaught exceptions through the log + a best-effort dialog,
    then chain to the previous hook. Keyboard interrupts pass straight
    through. Re-entrant calls (a crash while reporting a crash) are guarded
    so we never loop."""
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        global _in_excepthook
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        if not _in_excepthook:
            _in_excepthook = True
            try:
                _show_crash_dialog(exc_type, exc)
            except Exception:
                pass
            finally:
                _in_excepthook = False
        previous(exc_type, exc, tb)

    sys.excepthook = _hook


def _show_crash_dialog(exc_type, exc) -> None:
    """Tell the user something broke and where the log is — only if a
    QApplication is already running (no GUI bootstrap from a crash)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    if QApplication.instance() is None:
        return
    # ADR-132: a launch-time crash (e.g. the data file failing to open) fires
    # while the always-on-top splash is still up; on Windows that topmost band
    # hides this dialog behind it, so the app just appears to hang. Dismiss the
    # splash and force the box topmost so the error is actually seen.
    try:
        from mfl_desktop.ui.splash import dismiss_active_splash
        dismiss_active_splash()
    except Exception:
        pass
    box = QMessageBox()
    box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle("Unexpected error")
    box.setText(
        "My Financial Life hit an unexpected error. Your data is saved "
        "continuously, so it should be safe — but if anything looks wrong, "
        "you can restore from a snapshot (File ▸ Manage Data…)."
    )
    box.setInformativeText(
        f"Details have been written to the log:\n{log_path()}\n\n"
        f"Help ▸ Export Diagnostics… bundles this for a support email."
    )
    box.setDetailedText(f"{exc_type.__name__}: {exc}")
    box.setStandardButtons(QMessageBox.Ok)
    box.exec()


def _log_tail(max_lines: int = 200) -> str:
    """The last ``max_lines`` of the current log file, or a placeholder."""
    p = log_path()
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return "(no log file yet)"
    tail = lines[-max_lines:]
    return "\n".join(tail) if tail else "(log is empty)"


def collect_diagnostics(repo=None) -> str:
    """A support-ready, PII-light text blob: environment, key paths, and the
    tail of the log. ``repo`` (optional) adds the open file path + a couple
    of harmless counts so a bug report has context without dumping data."""
    parts: list[str] = []
    parts.append("=== My Financial Life — diagnostics ===")
    parts.append(environment_summary())
    parts.append(f"Log file: {log_path()}")
    if repo is not None:
        try:
            parts.append(f"Open database: {repo.db_path}")
        except Exception:
            pass
        try:
            n_acct = len(repo.list_accounts())
            parts.append(f"Accounts: {n_acct}")
        except Exception:
            pass
        try:
            base = repo.get_setting("base_currency") or "(unset → GBP)"
            parts.append(f"Base currency: {base}")
        except Exception:
            pass
    parts.append("")
    parts.append("--- recent log ---")
    parts.append(_log_tail())
    return "\n".join(parts)


def write_diagnostics(dest: Path, repo=None) -> Path:
    """Write the diagnostics blob to ``dest`` and return the path."""
    dest = Path(dest)
    dest.write_text(collect_diagnostics(repo), encoding="utf-8")
    return dest
