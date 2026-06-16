"""Shared date / period widget factories (ADR-082).

One place to build a ``QDateEdit`` and a period-preset ``QComboBox`` so every
dialog and window looks and behaves the same: a calendar popup, a single
**ISO** display format (``yyyy-MM-dd`` — decision P1a, revised 2026-06-16 from
the original ``d MMM yyyy`` guess to the owner-preferred ISO), and a consistent
way to populate a preset combo from :mod:`mfl_desktop.periods`.

Before this, ~14 ``QDateEdit`` sites each set their own ``setDisplayFormat`` —
mostly ISO ``yyyy-MM-dd`` for data-entry forms, ``d MMM yyyy`` for a couple of
human dialogs, and one (the schedule dialog) with *no* calendar popup. The
stored value is always read via ``QDateEdit.date()`` (a ``QDate``, format-
independent), so unifying the *display* format never touches persistence.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QComboBox, QDateEdit

from mfl_desktop import periods

# The one user-facing display format (P1a, revised to ISO 2026-06-16). The
# human format is kept available for any field that ever wants it.
ISO_DATE_FORMAT = "yyyy-MM-dd"
HUMAN_DATE_FORMAT = "d MMM yyyy"


def make_date_edit(
    initial: Optional[QDate] = None,
    *,
    calendar: bool = True,
    maximum_today: bool = False,
    display_format: str = ISO_DATE_FORMAT,
) -> QDateEdit:
    """Build a consistent ``QDateEdit``: calendar popup on, ISO display format
    (``yyyy-MM-dd``), today by default. ``maximum_today=True`` clamps the upper
    bound to today (for "as of"/historical fields that can't be in the future)."""
    edit = QDateEdit()
    edit.setCalendarPopup(calendar)
    edit.setDisplayFormat(display_format)
    if maximum_today:
        edit.setMaximumDate(QDate.currentDate())
    edit.setDate(initial if initial is not None else QDate.currentDate())
    return edit


def make_period_combo(
    keys: tuple[str, ...], *, current: Optional[str] = None,
) -> QComboBox:
    """Build a period-preset ``QComboBox`` from a :mod:`mfl_desktop.periods`
    preset set — label as text, key as ``itemData`` — optionally selecting
    ``current``. Kills the per-window ``_PERIOD_LABELS`` copies."""
    combo = QComboBox()
    for label, key in periods.options_for(keys):
        combo.addItem(label, key)
    if current is not None:
        i = combo.findData(current)
        if i >= 0:
            combo.setCurrentIndex(i)
    return combo
