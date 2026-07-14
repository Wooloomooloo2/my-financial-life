"""A report leads with what it *is*, not with the word "Untitled" (ADR-164).

An unsaved report used to put **"Untitled"** in the largest text on the screen,
with the report's actual identity ("Spending Over Time") as the small grey
subtitle underneath. Five report windows each had their own copy of that
string-building; ``report_heading`` is now the single definition.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.ui.page_header import report_heading


def test_an_unsaved_report_leads_with_its_type():
    title, subtitle, window_title = report_heading("Spending Over Time", None)
    assert title == "Spending Over Time"     # not "Untitled"
    assert subtitle == "Unsaved report"      # the unsaved-ness is the quiet part
    assert window_title == "Spending Over Time — Unsaved"


def test_the_word_untitled_is_gone():
    for label in ("Cash Flow", "Income & Expense", "Investment Returns"):
        title, subtitle, window_title = report_heading(label, None)
        assert "Untitled" not in (title, subtitle, window_title)
        assert title == label


def test_a_saved_report_leads_with_its_name():
    # The name is the identity the user chose — it outranks the type, which
    # drops to the subtitle.
    title, subtitle, window_title = report_heading("Cash Flow", "Retirement plan")
    assert title == "Retirement plan"
    assert subtitle == "Cash Flow"
    assert window_title == "Cash Flow — Retirement plan"


def test_a_dirty_saved_report_is_marked():
    title, _, window_title = report_heading("Cash Flow", "Plan", dirty=True)
    assert title == "Plan*"
    assert window_title == "Cash Flow — Plan*"


def test_a_foldered_report_is_prefixed_with_its_folder():
    title, _, _ = report_heading(
        "Cash Flow", "Plan", folder_name="Tax year 25/26",
    )
    assert title == "Tax year 25/26 / Plan"


def test_folder_and_dirty_compose():
    title, _, window_title = report_heading(
        "Cash Flow", "Plan", folder_name="Archive", dirty=True,
    )
    assert title == "Archive / Plan*"
    assert window_title == "Cash Flow — Archive / Plan*"


def test_an_unsaved_report_ignores_a_stray_folder_name():
    # An unsaved report is in no folder; a folder name must not leak into the
    # title and imply it has been filed somewhere.
    title, subtitle, _ = report_heading(
        "Cash Flow", None, folder_name="Archive", dirty=True,
    )
    assert title == "Cash Flow"
    assert subtitle == "Unsaved report"
