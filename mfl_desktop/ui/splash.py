"""Branded launch splash (ADR-103).

A lightweight ``QSplashScreen`` shown the instant the app starts and closed
when the main window appears. It earns its keep on a large file (migrations
+ window build take a beat) and on a cold start; on a fast launch it flashes
briefly and vanishes.

Always light-branded: the splash is composed *before* the persisted theme is
applied (that needs the DB open), so it uses explicit brand literals rather
than theme tokens — a brief launch screen in brand colours regardless of the
user's light/dark preference.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen

from mfl_desktop import resources
from mfl_desktop.version import APP_NAME

# Brand literals (the icon's palette) — see ADR-100.
_TEAL = "#1f6e78"
_INK_MUTED = "#64748b"
_BORDER = "#e2e8f0"
_SURFACE = "#ffffff"

_W, _H = 440, 280

# The splash created by :func:`make_splash`, tracked module-side so any
# launch-time code path can dismiss it without threading the object through
# (ADR-132). See :func:`dismiss_active_splash`.
_active_splash: QSplashScreen | None = None


def dismiss_active_splash() -> None:
    """Close the launch splash if it's still showing (ADR-132).

    The splash is ``WindowStaysOnTopHint`` so it stays visible through a slow
    launch — but on Windows that topmost band also sits *above* a no-parent
    modal dialog, hiding the recovery prompt / crash box / first-run picker
    behind it so the app looks frozen. Every launch-time dialog calls this
    first; once a dialog is needed we're in an interactive flow, so losing the
    branded splash for the rest of launch is the right trade. Idempotent and
    safe to call when there is no splash (e.g. tests, headless)."""
    global _active_splash
    sp = _active_splash
    _active_splash = None
    if sp is not None:
        sp.close()


def make_splash() -> QSplashScreen:
    """Compose and return the splash. Call ``.show()`` then
    ``app.processEvents()`` to paint it before the slow launch work, and
    ``.finish(main_window)`` once the window is up."""
    pm = QPixmap(_W, _H)
    pm.fill(QColor(_SURFACE))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    p.setPen(QColor(_BORDER))
    p.drawRect(0, 0, _W - 1, _H - 1)

    icon = resources.brand_mark(104)
    if not icon.isNull():
        p.drawPixmap((_W - 104) // 2, 36, icon)

    p.setPen(QColor(_TEAL))
    name_font = QFont()
    name_font.setPixelSize(22)
    name_font.setWeight(QFont.DemiBold)
    p.setFont(name_font)
    p.drawText(QRect(0, 150, _W, 30), Qt.AlignHCenter, APP_NAME)

    p.setPen(QColor(_INK_MUTED))
    sub_font = QFont()
    sub_font.setPixelSize(12)
    p.setFont(sub_font)
    p.drawText(
        QRect(0, 182, _W, 20), Qt.AlignHCenter,
        "private and on your own device",
    )
    p.end()

    splash = QSplashScreen(pm)
    splash.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    splash.showMessage(
        "Loading…", Qt.AlignBottom | Qt.AlignHCenter, QColor(_INK_MUTED),
    )
    global _active_splash
    _active_splash = splash
    return splash
