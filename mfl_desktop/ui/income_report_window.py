"""Income Over Time — the income-side mirror of Spending Over Time (ADR-088).

Identical report to :class:`SpendingReportWindow` — same stacked-bar chart,
period / granularity / rollup / drill-down, filter dialog, and save/load — but
it sums **income** (strict inflows on ``kind='income'`` categories) instead of
spending. All the machinery lives in ``SpendingReportWindow``; this subclass
just selects the income variant via the ``_DIRECTION`` class attribute, which
the base reads for its kind, repository aggregate, saved-report type, and the
on-screen wording.
"""
from __future__ import annotations

from mfl_desktop.ui.spending_report_window import (
    SpendingReportWindow, _INCOME_DIRECTION,
)


class IncomeReportWindow(SpendingReportWindow):
    """Income Over Time window — bare or saved-loaded.

    Construct via the inherited :py:meth:`SpendingReportWindow.open_bare`
    (Reports menu) or :py:meth:`SpendingReportWindow.load_from_id` (a saved-
    report sidebar click). Both honour ``_DIRECTION`` — ``load_from_id``
    only accepts a saved report of the income type."""

    _DIRECTION = _INCOME_DIRECTION
