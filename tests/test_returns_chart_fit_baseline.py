"""The Investment Returns chart can fit its y-axis to the data (ADR-181).

The chart is a stacked *area* composition (cost / gain / realized / dividends)
that was always anchored at zero. Honest for "all time", but on a short window
the whole stack floats near the top of a 0→£2M axis and looks flat — the owner
couldn't see the portfolio's movement. A "Fit axis" toggle (default on) now
rounds the floor down to just below the lowest cost band so the movement fills
the panel, while a Zero setting keeps the true-magnitude composition.

Pinned here:

1. **nice_bounds** brackets an arbitrary range on round numbers with a non-zero
   floor — the helper the fit mode rests on.
2. **_y_range** — zero mode is byte-for-byte the old behaviour (floor 0, extend
   down only for a realized loss); fit mode lifts the floor above zero for a
   high-base short window, reduces to ~zero when the window reaches a near-empty
   portfolio (so "Max" is unchanged), and still reaches a realized loss below 0.
3. **chart_fit** round-trips through the saved filters and an old blob upgrades.
4. **The window wiring** — the toggle updates the filter, the chart and the
   dirty flag; editing filters in the dialog preserves the chart_fit choice.

Run headless:

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_returns_chart_fit_baseline.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.reports.filters import InvestmentReturnsFilters
from mfl_desktop.ui.chart_helpers import nice_bounds
from mfl_desktop.ui.returns_chart import ReturnsChart
from mfl_desktop.ui.theme import apply_theme

_DEMO = _REPO_ROOT / "mfl_public.mfl"


def _pt(cost, mv, realized=0.0, div=0.0):
    """A minimal stand-in for holdings.ReturnPoint — the fields _bounds reads."""
    return SimpleNamespace(
        date="2026-01-01",
        cost_basis=cost, market_value=mv,
        realized_cum=realized, dividends_cum=div,
        unrealized=mv - cost,
    )


def _chart(points, fit):
    c = ReturnsChart()
    c._points = points
    c._fit = fit
    return c


# ── 1. nice_bounds ──────────────────────────────────────────────────────────

def test_nice_bounds_brackets_with_a_nonzero_floor():
    lo, hi, step = nice_bounds(905_000, 1_560_000)
    assert lo <= 905_000 < 1_560_000 <= hi
    assert lo > 0                                   # the whole point: floor isn't 0
    # floor and ceiling land on step boundaries
    assert abs(round(lo / step) * step - lo) < 1e-6
    assert abs(round(hi / step) * step - hi) < 1e-6


def test_nice_bounds_handles_a_negative_floor():
    lo, hi, step = nice_bounds(-5_000, 120_000)
    assert lo <= -5_000 and hi >= 120_000


def test_nice_bounds_degenerate_flat_range_does_not_crash():
    lo, hi, step = nice_bounds(1_000, 1_000)       # vmax == vmin
    assert hi > lo and step > 0


# ── 2. _y_range ─────────────────────────────────────────────────────────────

def test_zero_mode_floor_is_zero():
    pts = [_pt(700_000, 950_000), _pt(900_000, 1_500_000)]
    ymin, ymax, step = _chart(pts, fit=False)._y_range()
    assert ymin == 0.0
    assert ymax >= 1_500_000


def test_fit_mode_lifts_the_floor_on_a_high_base_window():
    # A 12-month-like window: cost never below ~£700k, tops ~£1.5M.
    pts = [_pt(700_000, 950_000), _pt(680_000, 950_000), _pt(900_000, 1_500_000)]
    ymin, ymax, step = _chart(pts, fit=True)._y_range()
    assert ymin > 0.0                               # dead space below reclaimed
    assert ymin < 680_000                           # but the lowest cost stays visible
    assert ymax >= 1_500_000


def test_fit_mode_reduces_to_zero_when_data_reaches_near_empty():
    # An all-time window: the portfolio starts near £0, so fit ≈ zero-anchored.
    pts = [_pt(0.0, 0.0), _pt(50_000, 60_000), _pt(900_000, 1_500_000)]
    ymin, _, _ = _chart(pts, fit=True)._y_range()
    assert ymin == 0.0


def test_fit_mode_still_reaches_a_realized_loss_below_zero():
    # Realized is stacked above the position value, so a stack only dips below
    # zero when the realized loss exceeds what's left — the same excursion
    # zero-mode extends downward for. The fitted floor must include it.
    pts = [_pt(10_000, 12_000), _pt(8_000, 6_000, realized=-25_000)]
    ymin, _, _ = _chart(pts, fit=True)._y_range()
    assert ymin < 0.0                               # floor reaches the -£17k stack foot


def test_set_fit_toggles_and_is_idempotent():
    c = ReturnsChart()
    assert c._fit is True                           # default on
    c.set_fit(False)
    assert c._fit is False
    c.set_fit(False)                                # no-op, must not raise
    assert c._fit is False
    c.set_fit(True)
    assert c._fit is True


def test_a_short_window_paints_without_error_in_both_modes():
    # Regression for the clip + axis-break path: a lifted floor must render.
    pts = [_pt(700_000 + i * 1000, 950_000 + i * 4000) for i in range(14)]
    for fit in (True, False):
        c = _chart(pts, fit=fit)
        c.resize(800, 400)
        c.grab()                                    # forces a full paintEvent


# ── 3. chart_fit round-trip ─────────────────────────────────────────────────

def test_chart_fit_defaults_on():
    assert InvestmentReturnsFilters.default().chart_fit is True


def test_chart_fit_round_trips_through_json():
    f = replace(InvestmentReturnsFilters.default(), chart_fit=False)
    back = InvestmentReturnsFilters.from_json(f.to_json())
    assert back.chart_fit is False


def test_old_saved_blob_without_chart_fit_upgrades_to_on():
    # A report saved before ADR-181 has no chart_fit key.
    legacy = '{"period_key":"1y","account_ids":[],"security_ids":[]}'
    assert InvestmentReturnsFilters.from_json(legacy).chart_fit is True


# ── 4. window wiring ────────────────────────────────────────────────────────

def _window():
    from mfl_desktop.ui.investment_returns_window import InvestmentReturnsWindow
    tmp = Path(tempfile.mkdtemp(prefix="mfl_adr181_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    apply_theme(_app, "light")
    return InvestmentReturnsWindow(Repository(tmp))


def test_toggle_updates_filter_chart_and_dirty():
    win = _window()
    win._dirty = False
    assert win._current_filters.chart_fit is True
    win._fit_button.setChecked(False)               # fires _on_toggle_fit
    assert win._current_filters.chart_fit is False
    assert win._chart._fit is False
    assert win._dirty is True
    win.close()


def test_editing_filters_preserves_the_fit_choice():
    """Turn Fit off, then apply a filter-dialog change: the dialog builds a fresh
    InvestmentReturnsFilters (chart_fit defaulting True), so the window must
    carry the user's choice across it rather than silently re-enabling Fit."""
    import mfl_desktop.ui.investment_returns_window as mod
    win = _window()
    win._fit_button.setChecked(False)               # Fit off
    assert win._current_filters.chart_fit is False

    # Stub the modal dialog: accepted, returning a fresh filters (period changed,
    # chart_fit back at its default True — exactly what the real dialog does).
    class _StubDialog:
        def __init__(self, *a, **k): pass
        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.Accepted
        def values(self):
            return replace(InvestmentReturnsFilters.default(), period_key="3y")

    orig = mod.InvestmentReturnsFilterDialog
    mod.InvestmentReturnsFilterDialog = _StubDialog
    try:
        win._on_open_filter()
    finally:
        mod.InvestmentReturnsFilterDialog = orig

    assert win._current_filters.period_key == "3y"  # the edit applied
    assert win._current_filters.chart_fit is False  # ...and Fit stayed off
    assert win._chart._fit is False
    win.close()


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
