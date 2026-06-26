"""Reinvested dividends render as their own series in the Income report (ADR-110).

The "Show Reinvested Dividends" toggle used to silently fold reinvested (DRIP)
distributions into whatever cash income category they were tagged with (e.g.
Dividend Income), so the user couldn't see them and — if filtered to a different
category — the toggle appeared to do nothing. Now they surface as a dedicated
"Reinvested Dividends" legend series, independent of the category filter.

Runs against the versioned public demo (which carries a couple of ReinvDiv
rows). Needs PySide6 + an offscreen platform — use the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_income_reinvested_series.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication, QLabel

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.theme import apply_theme
from mfl_desktop.ui.income_report_window import IncomeReportWindow
from mfl_desktop.ui.spending_report_window import (
    REINVESTED_GROUP_ID,
    REINVESTED_GROUP_LABEL,
)

_DEMO = _REPO_ROOT / "mfl_public.mfl"


def _repo() -> Repository:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_rein_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    repo = Repository(tmp)
    apply_theme(_app, "light")
    return repo


def _wide_income_window(repo, *, include_reinvested: bool):
    win = IncomeReportWindow.open_bare(repo)
    win._current_filters = replace(
        win._current_filters,
        period_key="custom", custom_start="2020-01-01", custom_end="2026-12-31",
        granularity="year", rollup_level="group",
        include_reinvested_dividends=include_reinvested,
    )
    win._refresh()
    return win


def _legend_labels(win) -> list[str]:
    out = []
    lay = win._categories_layout
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None:
            for lbl in w.findChildren(QLabel):
                if lbl.text():
                    out.append(lbl.text())
    return out


def test_reinvested_series_shows_when_toggle_on():
    repo = _repo()
    try:
        labels = _legend_labels(_wide_income_window(repo, include_reinvested=True))
        assert REINVESTED_GROUP_LABEL in labels, labels
    finally:
        repo.close()


def test_reinvested_series_absent_when_toggle_off():
    repo = _repo()
    try:
        labels = _legend_labels(_wide_income_window(repo, include_reinvested=False))
        assert REINVESTED_GROUP_LABEL not in labels, labels
    finally:
        repo.close()


def test_reinvested_bypasses_category_filter():
    """Filtering to a non-dividend income category still shows the reinvested
    series — the toggle is its own visibility control (ADR-110)."""
    repo = _repo()
    try:
        win = IncomeReportWindow.open_bare(repo)
        # Pick any income category that is NOT where DRIPs are tagged.
        income_cat = repo.connection.execute(
            "SELECT id FROM category WHERE kind='income' AND name LIKE '%Salary%' LIMIT 1"
        ).fetchone()
        if income_cat is None:
            income_cat = repo.connection.execute(
                "SELECT id FROM category WHERE kind='income' LIMIT 1"
            ).fetchone()
        win._current_filters = replace(
            win._current_filters,
            period_key="custom", custom_start="2020-01-01", custom_end="2026-12-31",
            granularity="year", rollup_level="group",
            include_reinvested_dividends=True, category_ids=(int(income_cat[0]),),
        )
        win._refresh()
        assert REINVESTED_GROUP_LABEL in _legend_labels(win)
    finally:
        repo.close()


def test_synthetic_group_id_is_negative():
    # Must never collide with a real category id (positive) or Uncategorised (1).
    assert REINVESTED_GROUP_ID < 0


# ── bare-script runner ──────────────────────────────────────────────────────

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
