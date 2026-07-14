"""Spending / Income Over Time fold their tail into "Other" (ADR-166).

The palette has eight identity colours and does not cycle, so a 9th series
would wear slot 1's teal and read as the *same category* as the largest one.
This was not hypothetical: the demo file has **nine** top-level expense
categories, and the first render after the palette landed showed "Charity and
gifts" in exactly Housing's teal.

The report now keeps the top seven and folds everything else into one "Other"
slice, so no two categories ever share a colour.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.db.repository import Repository
from mfl_desktop.reports.filters import TYPE_SPENDING_OVER_TIME  # noqa: F401
from mfl_desktop.ui.chart_helpers import SERIES_SLOTS, colour_for
from mfl_desktop.ui.spending_report_window import (
    OTHER_GROUP_ID,
    OTHER_GROUP_LABEL,
    SpendingReportWindow,
)
from mfl_desktop.ui.theme import apply_theme

_DEMO = _REPO_ROOT / "mfl_public.mfl"


def _win() -> SpendingReportWindow:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_fold_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)

    # The committed demo nests every expense category under one "Expense" root,
    # so a top-level rollup yields two groups and the fold never fires. Promote
    # them to top level — the same thing the screenshot harness does, and the
    # shape a real file has: nine top-level expense categories, one more than
    # the palette has slots. This is the case the fold exists for.
    import sqlite3
    con = sqlite3.connect(tmp)
    row = con.execute(
        "SELECT id FROM category WHERE name='Expense' AND parent_id IS NULL"
    ).fetchone()
    assert row, "demo file no longer has the 'Expense' root this test relies on"
    con.execute(
        "UPDATE category SET parent_id=NULL WHERE parent_id=? AND kind='expense'",
        (row[0],),
    )
    con.commit()
    con.close()

    apply_theme(_app, "light")
    win = SpendingReportWindow.open_bare(Repository(tmp))
    for _ in range(10):
        _app.processEvents()
    return win


def test_the_fixture_really_has_more_categories_than_slots():
    """Guard the guard: if the demo ever changes shape and yields ≤ 8 groups,
    the fold tests below would pass vacuously."""
    win = _win()
    win._chart  # built
    # Count what the report *would* have shown without folding: the folded run
    # ends in Other, so 8 groups with Other last means the tail was real.
    groups = _groups(win)
    assert len(groups) == SERIES_SLOTS
    assert groups[-1][0] == OTHER_GROUP_ID


def _groups(win) -> list[tuple[int, str]]:
    return list(win._chart._groups)


def test_the_chart_never_shows_more_than_eight_series():
    win = _win()
    assert len(_groups(win)) <= SERIES_SLOTS


def test_no_two_series_share_a_colour():
    """The actual defect: two categories in the same teal."""
    win = _win()
    colours = [colour_for(i).name() for i in range(len(_groups(win)))]
    assert len(colours) == len(set(colours)), f"duplicate series colour: {colours}"


def test_the_tail_is_folded_into_a_named_other_slice():
    # The demo file has nine top-level expense categories, so the fold must fire
    # and it must be *labelled* — a silently-dropped tail would understate the
    # chart's totals.
    win = _win()
    groups = _groups(win)
    labels = [name for _, name in groups]
    assert OTHER_GROUP_LABEL in labels
    assert groups[-1][0] == OTHER_GROUP_ID


def test_other_is_not_drillable():
    """"Other" is not a category — clicking it must not push a drill snapshot
    (the same guard REINVESTED_GROUP_ID already has)."""
    win = _win()
    before = len(win._drill_stack)
    win._on_segment_clicked(OTHER_GROUP_ID, "2026-W20")
    assert len(win._drill_stack) == before
