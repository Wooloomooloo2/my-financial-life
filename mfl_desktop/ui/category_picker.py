"""Searchable category combo helper.

Shared by NewTransactionDialog, BulkEditDialog, ScheduleDialog,
BudgetSetupDialog, and the register's CategoryTypeaheadDelegate so every
surface picks categories the same way.

The combo is editable and uses a contains-match QCompleter, so typing
"groc" reduces the dropdown to anything with "groc" in the label.

ADR-031: labels are the **full breadcrumb path** (`Food → Groceries →
Tesco`) instead of `Leaf (ImmediateParent)`, so typing an ancestor name
("Food") reveals all the ancestor's descendants in the typeahead, and
same-named leaves under different parents are visually distinct.

The combo's `userData` is the category id; `userData=None` is used for
the optional "no change" placeholder when one is requested.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCompleter, QComboBox

from mfl_desktop.db.repository import CategoryChoice


def make_category_picker(
    categories: list[CategoryChoice],
    default_id: Optional[int] = None,
) -> QComboBox:
    """Return an editable QComboBox populated with the categories.

    - Labels are the full breadcrumb path (ADR-031). Top-level rows show
      just the name; deeper rows show `Parent → Child → …`.
    - Typing filters the dropdown via QCompleter (MatchContains,
      case-insensitive) — typing any ancestor name reveals descendants.
    - The user can still click to drop the full list. `setInsertPolicy
      (NoInsert)` keeps free-text entries from being added as bogus
      combo rows.
    - `default_id`, if given, pre-selects the matching item.
    """
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    for c in categories:
        # Fall back to `name` if path was empty (defensive — populated by
        # Repository.list_categories_flat in normal flows).
        label = c.path or c.name
        combo.addItem(label, userData=c.id)
    if default_id is not None:
        for i in range(combo.count()):
            if combo.itemData(i) == default_id:
                combo.setCurrentIndex(i)
                break
    completer = combo.completer()
    if completer is not None:
        # PopupCompletion: typing pops a dropdown of matching items rather
        # than splicing the matched suffix into the user's input. The
        # default InlineCompletion + MatchContains combination produces
        # frankenwords like "sub" + "ls and Subscriptions" when the
        # matched text overlaps the typed prefix.
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
    return combo


def selected_category_id(combo: QComboBox) -> Optional[int]:
    """Resolve the user's choice to a category id.

    Returns the id of the item whose displayed label exactly matches the
    combo's current text — that's the path the QCompleter takes when the
    user picks from the dropdown (click or Enter on a highlighted row).
    Returns None when the user typed text that doesn't match any item;
    callers should treat that as "pick again".
    """
    text = combo.currentText().strip()
    if not text:
        return None
    for i in range(combo.count()):
        if combo.itemText(i) == text:
            data = combo.itemData(i)
            return int(data) if data is not None else None
    return None
