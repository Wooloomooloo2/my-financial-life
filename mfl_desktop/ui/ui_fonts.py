"""Cross-platform font sizing (ADR-076 amendment).

We force the Fusion style on every platform, and a *point* size renders at a
different pixel size per OS because logical DPI differs: 96 on Windows, 72 on
macOS. So the same `setPointSize(9)` that looks right on Windows (~12px) comes
out ~25% smaller on macOS (~9px). Painter labels (charts) and any explicitly
point-sized widget font hit this.

:func:`set_pt` sizes a font to the visual size of ``pt`` points **at 96 DPI**,
consistently across platforms: it pins the equivalent pixel size on macOS and
keeps points elsewhere — so Windows/Linux are byte-for-byte unchanged while
macOS matches them. Use it instead of ``QFont.setPointSize`` for chart and
display fonts.
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QFont

_IS_MAC = sys.platform == "darwin"


def set_pt(font: QFont, pt: float) -> QFont:
    """Size ``font`` to the visual size of ``pt`` points at 96 DPI. Mutates the
    font in place and returns it (so it can be used inline). On macOS this sets
    the equivalent **pixel** size (``pt × 96/72``); elsewhere it sets the point
    size unchanged."""
    if _IS_MAC:
        font.setPixelSize(round(pt * 96 / 72))
    else:
        font.setPointSize(round(pt))
    return font
