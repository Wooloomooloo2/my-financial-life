"""Stack totals are printed above the bars (ADR-154).

The stacked-bar chart (Spending Over Time / Income Over Time) showed a grand
total and an average in the summary panel, and a per-segment value on hover, but
never the one number each bar actually represents — its total. Reading it meant
eyeballing gridlines £20,000 apart.

Pinned here:
  * the totals are laid out, one per bar, and equal the sum of the stack;
  * they suppress *wholesale* when they wouldn't fit (granularity goes to daily,
    so a wide span can produce hundreds of bars);
  * the average pill dodges them rather than overlapping — the last bucket is
    usually a part-period and so tends to land near the average, which is exactly
    where the pill lives.

Run headless:

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_spending_chart_bar_totals.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.ui.spending_chart import SpendingChart
from mfl_desktop.ui.theme import apply_theme

_GROUPS = [
    (1, "Rental Income"),
    (2, "Reinvested Dividends"),
    (3, "Dividend Income"),
    (4, "Interest Inc"),
]
_SPLIT = ((1, 0.45), (2, 0.22), (3, 0.23), (4, 0.10))


def _chart(buckets, totals_pounds, avg=0.0, size=(1500, 780)) -> SpendingChart:
    """A painted chart. `totals_pounds` maps bucket -> intended stack total."""
    spending = {}
    for b in buckets:
        pence_total = int(totals_pounds[b] * 100)
        for gid, frac in _SPLIT:
            spending[(gid, b)] = int(pence_total * frac)
    apply_theme(_app, "light")
    c = SpendingChart()
    c.set_show_legend(False)
    c.resize(*size)
    c.render(buckets=buckets, groups=_GROUPS, spending=spending, avg_pounds=avg)
    c.setAttribute(Qt.WA_DontShowOnScreen, True)
    c.show()
    c.grab()          # force a paintEvent so _bar_totals is populated
    return c


def _years():
    base = {2015: 9_000, 2016: 10_000, 2017: 12_000, 2018: 11_000, 2019: 13_000,
            2020: 14_000, 2021: 24_000, 2022: 20_000, 2023: 32_000, 2024: 42_000,
            2025: 73_000, 2026: 25_000}
    buckets = [str(y) for y in sorted(base)]
    return buckets, {str(y): v for y, v in base.items()}


def test_one_total_per_bar():
    buckets, totals = _years()
    c = _chart(buckets, totals)
    assert len(c._bar_totals) == len(buckets)


def test_total_equals_the_sum_of_its_stack():
    """The label must be the stack's total, not the top segment or the average."""
    buckets, totals = _years()
    c = _chart(buckets, totals)
    for bucket, (_x, _top, total) in zip(buckets, c._bar_totals):
        # _SPLIT sums to 1.0, but each segment is int-truncated, so allow a
        # few pence of rounding across the four segments.
        assert abs(total - totals[bucket]) < 1.0, bucket


def test_labels_are_laid_out_when_there_is_room():
    buckets, totals = _years()
    c = _chart(buckets, totals)
    chart_rect, _legend = c._compute_rects()
    laid = c._layout_bar_totals(chart_rect)
    assert len(laid) == len(buckets)
    texts = [t for _r, t in laid]
    assert "£73,000" in texts          # the tall bar is labelled with its total


def test_labels_do_not_overlap_each_other():
    buckets, totals = _years()
    c = _chart(buckets, totals)
    chart_rect, _legend = c._compute_rects()
    rects = [r for r, _t in c._layout_bar_totals(chart_rect)]
    for a, b in zip(rects, rects[1:]):
        assert not a.intersects(b)


def test_labels_suppress_wholesale_when_bars_are_dense():
    """144 monthly buckets: the labels can't fit, so none are drawn — rather than
    an arbitrary labelled subset. The tooltip still carries the numbers."""
    buckets = [f"{y}-{m:02d}" for y in range(2015, 2027) for m in range(1, 13)]
    totals = {b: 2_000.0 for b in buckets}
    c = _chart(buckets, totals)
    chart_rect, _legend = c._compute_rects()
    assert c._layout_bar_totals(chart_rect) == []


def test_labels_suppress_in_a_narrow_window():
    """Same 12 bars, but the window is too narrow for the widest label."""
    buckets, totals = _years()
    c = _chart(buckets, totals, size=(320, 480))
    chart_rect, _legend = c._compute_rects()
    assert c._layout_bar_totals(chart_rect) == []


def test_average_pill_does_not_overlap_a_total_label():
    """The last bucket is typically a part-period, so its bar lands near the
    average — right where the pill sits. The pill must move, not the total."""
    buckets, totals = _years()
    # 2026's total (£25,000) sits essentially on the average line.
    c = _chart(buckets, totals, avg=24_582.79)
    chart_rect, _legend = c._compute_rects()
    laid = c._layout_bar_totals(chart_rect)
    assert laid, "totals should be shown at this size"

    ymax, _step = c._compute_y_axis()
    y = chart_rect.bottom() - (c._avg_pounds / ymax) * chart_rect.height()

    from PySide6.QtGui import QFont, QFontMetrics
    from mfl_desktop.ui.ui_fonts import set_pt
    from mfl_desktop.ui.chart_helpers import fmt_currency
    from PySide6.QtCore import QRectF

    font = QFont(c.font())
    set_pt(font, 9)
    font.setBold(True)
    fm = QFontMetrics(font)
    tw = fm.horizontalAdvance(f"Avg {fmt_currency(c._avg_pounds)}") + 12
    th = fm.height() + 4

    above = QRectF(chart_rect.right() - tw, y - th - 4, tw, th)
    label_rects = [r for r, _t in laid]
    assert any(above.intersects(r) for r in label_rects), (
        "test is not exercising the collision — the pill's default spot is clear"
    )

    below = QRectF(above)
    below.moveTop(y + 4)
    assert not any(below.intersects(r) for r in label_rects), (
        "the fallback position collides too; the pill has nowhere to go"
    )


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
