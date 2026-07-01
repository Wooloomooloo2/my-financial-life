"""Consistent rounded bar corners across report charts (ADR-128).

Pins the shared helpers in ``chart_helpers``:

- ``bar_corner_radius`` returns the shared constant, clamped for narrow bars;
- ``round_bar_corners`` actually rounds a drawn bar — the extreme corner pixel
  becomes the background while the centre of the top edge stays bar-coloured —
  and does so **at any height**, which is the whole point: a thin stacked cap
  used to fall back to a square top and looked inconsistent from bar to bar.

Pixel-level so it proves the visual result, not just that a code path ran.
Needs PySide6 + offscreen — run with the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_bar_corners.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.ui import chart_helpers as ch

_BG = QColor("#ffffff")
_BAR = QColor("#2563eb")  # blue-600


def _paint(width: int, height: int, bar: QRectF, radius: float, **corners) -> QImage:
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(_BG)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.fillRect(bar, _BAR)
    ch.round_bar_corners(p, bar, radius, _BG, **corners)
    p.end()
    return img


def _is_bg(img: QImage, x: int, y: int) -> bool:
    return QColor(img.pixel(x, y)) == _BG


def _is_bar(img: QImage, x: int, y: int) -> bool:
    return QColor(img.pixel(x, y)) == _BAR


# ── radius helper ────────────────────────────────────────────────────────────


def test_radius_uses_shared_constant_for_wide_bars():
    assert ch.bar_corner_radius(100.0) == ch.BAR_CORNER_RADIUS


def test_radius_clamps_for_narrow_bars():
    # bar_w / 3 wins when it's below the constant.
    assert ch.bar_corner_radius(9.0) == 3.0


def test_radius_never_negative():
    assert ch.bar_corner_radius(0.0) == 0.0


# ── the carve actually rounds ───────────────────────────────────────────────


def test_top_corners_become_background():
    bar = QRectF(10, 10, 60, 80)  # tall bar
    img = _paint(100, 100, bar, 6.0, top=True)
    # Extreme top-left / top-right pixels are carved back to the background…
    assert _is_bg(img, 10, 10)
    assert _is_bg(img, 69, 10)
    # …but the centre of the top edge is untouched (still bar-coloured)…
    assert _is_bar(img, 40, 10)
    # …and well inside the bar is bar-coloured.
    assert _is_bar(img, 40, 50)


def test_bottom_corners_when_requested():
    bar = QRectF(10, 10, 60, 80)
    img = _paint(100, 100, bar, 6.0, top=False, bottom=True)
    assert _is_bg(img, 10, 89)          # bottom-left carved
    assert _is_bg(img, 69, 89)          # bottom-right carved
    assert _is_bar(img, 40, 89)         # bottom-edge centre kept
    assert _is_bar(img, 10, 10)         # top-left NOT carved (bottom-only)


def test_thin_cap_still_rounds():
    # A cap far thinner than the radius — the case that used to render square.
    bar = QRectF(10, 10, 60, 4)
    img = _paint(100, 100, bar, 6.0, top=True)
    assert _is_bg(img, 10, 10)          # corner carved even at height 4
    assert _is_bar(img, 40, 10)         # centre of the (thin) top kept


def test_zero_radius_is_noop():
    bar = QRectF(10, 10, 60, 80)
    img = _paint(100, 100, bar, 0.0, top=True)
    assert _is_bar(img, 10, 10)         # square — nothing carved


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
