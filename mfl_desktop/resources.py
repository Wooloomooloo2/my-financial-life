"""Runtime resource resolution (ADR-101).

Locates bundled assets — currently the app icon — in both a plain source
checkout and a frozen PyInstaller build. PyInstaller unpacks bundled data
to ``sys._MEIPASS``; a source run reads from the repo root. The icon source
of truth lives in ``assets/icons/`` at the repo root (the same files the
packaging step feeds to PyInstaller as the bundle icon).
"""
from __future__ import annotations

import sys
from pathlib import Path


def _root() -> Path:
    """The base directory assets are resolved against — the PyInstaller
    unpack dir when frozen, else the repository root (parent of this
    package)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def asset_path(*parts: str) -> Path:
    """Path to a bundled asset under ``assets/`` (e.g.
    ``asset_path("icons", "mfl_icon_256.png")``)."""
    return _root() / "assets" / Path(*parts)


def app_icon():
    """A multi-resolution ``QIcon`` for the window / taskbar / dock, built
    from the PNG size set so it stays crisp at any display size. Returns an
    empty ``QIcon`` if the files are missing (never raises — a missing icon
    must not stop the app launching). Imported lazily so this module stays
    Qt-free for non-GUI callers."""
    from PySide6.QtGui import QIcon
    icon = QIcon()
    any_added = False
    for size in (16, 32, 64, 128, 256, 512, 1024):
        p = asset_path("icons", f"mfl_icon_{size}.png")
        if p.exists():
            icon.addFile(str(p))
            any_added = True
    if not any_added:
        # Fall back to the master if the size set isn't present.
        master = asset_path("icons", "mfl_icon_1024.png")
        if master.exists():
            icon.addFile(str(master))
    return icon


def app_pixmap(size: int = 64):
    """A crisp square ``QPixmap`` of the app icon at ``size`` px — for showing
    the icon *inside* the UI (About box, first-run welcome, splash). Picks the
    best source from the multi-resolution icon. May be a null pixmap if the
    asset is missing (callers should guard with ``isNull()``)."""
    from PySide6.QtCore import QSize
    return app_icon().pixmap(QSize(size, size))


def brand_mark(size: int = 28):
    """A transparent-background MFL hexagon mark at ``size`` px, for the brand
    chrome *inside* the UI (sidebar header, About). Distinct from ``app_pixmap``
    — which derives from the dock/taskbar icon set whose PNGs carry a flat light
    background that would show as a box on dark surfaces. Falls back to
    ``app_pixmap`` if the dedicated transparent asset is missing."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap
    p = asset_path("icons", "mfl_mark.png")
    if not p.exists():
        return app_pixmap(size)
    pm = QPixmap(str(p))
    if pm.isNull():
        return app_pixmap(size)
    return pm.scaled(
        size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
    )


def company_logo(height: int = 32):
    """The Garelochsoft company wordmark scaled to ``height`` px (aspect kept),
    for the publisher attribution in the About box. Garelochsoft publishes My
    Financial Life. Returns a null pixmap if the asset is missing."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap
    p = asset_path("icons", "garelochsoft_logo.png")
    if not p.exists():
        return QPixmap()
    pm = QPixmap(str(p))
    if pm.isNull():
        return pm
    return pm.scaledToHeight(height, Qt.SmoothTransformation)
