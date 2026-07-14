"""Home's spending cards fall back to the last month with spending (ADR-163).

Top Payees / Top Categories windowed on the current calendar month, so on the
1st — or on any file whose data stops earlier — the dashboard showed two cards
reading "No spending yet this month" with nothing under them. They now fall back
to the last month that *does* have spending, and say which month that is.

Qt-free: this is all `home_dashboard` / `Repository`, no widgets.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository
from mfl_desktop.home_dashboard import _spend_window, gather_home_data

_DEMO = _REPO_ROOT / "mfl_public.mfl"

# The demo file's transactions stop in June 2026.
_LAST_MONTH_WITH_SPENDING = "2026-06"


def _repo() -> Repository:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_spend_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    return Repository(tmp)


def test_latest_spending_month_finds_the_last_month_with_spending():
    repo = _repo()
    assert repo.latest_spending_month(not_after="2026-07") == _LAST_MONTH_WITH_SPENDING


def test_latest_spending_month_respects_the_upper_bound():
    # Asking as-of an earlier month must not return a later one.
    repo = _repo()
    got = repo.latest_spending_month(not_after="2026-03")
    assert got is not None and got <= "2026-03"


def test_current_month_with_spending_is_used_as_is():
    """The normal case must not change: an active file stays on "This month"."""
    repo = _repo()
    date_from, date_to, label = _spend_window(repo, date(2026, 6, 20))
    assert label == "This month"
    assert date_from == "2026-06-01"
    assert date_to == "2026-06-20"          # month-to-date, not the whole month


def test_empty_current_month_falls_back_to_the_last_month_with_spending():
    """The bug: July 2026 has no spending, so both cards were empty."""
    repo = _repo()
    date_from, date_to, label = _spend_window(repo, date(2026, 7, 13))
    assert label == "June 2026"             # named, never silently substituted
    assert date_from == "2026-06-01"
    assert date_to == "2026-06-30"          # the *whole* fallback month


def test_fallback_month_end_is_right_across_a_year_boundary():
    """December → the 31st, not a rollover into month 13."""
    repo = _repo()
    # Nothing in the file after June 2026, so as-of Jan 2027 we fall back.
    _, date_to, label = _spend_window(repo, date(2027, 1, 10))
    assert label == "June 2026" and date_to == "2026-06-30"


def test_the_cards_actually_have_rows_after_the_fallback():
    """The whole point: the dashboard is populated, not two dead cards."""
    repo = _repo()
    data = gather_home_data(repo, date(2026, 7, 13))
    assert data.spend_period_label == "June 2026"
    assert data.top_payees, "Top Payees should not be empty after the fallback"
    assert data.top_categories, "Top Categories should not be empty either"


def test_label_defaults_to_this_month():
    """A file with spending in the current month keeps the plain label, so the
    card doesn't start naming months for no reason."""
    repo = _repo()
    data = gather_home_data(repo, date(2026, 6, 20))
    assert data.spend_period_label == "This month"
