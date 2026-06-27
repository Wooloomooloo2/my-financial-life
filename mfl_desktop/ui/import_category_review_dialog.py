"""Import-time new-category review (ADR-118).

When a staged import carries category names that aren't in the tree and aren't
already mapped, this dialog lists them before commit and lets the user decide,
per path: **map** it to an existing category, **create** it, or send it to
**Needs Review**. A *map* choice is also remembered (recorded as an import
mapping) so future imports of the same export route there automatically.

Pure view: it takes the planned items + the category list and returns the
decisions; the service applies them. No Repository writes happen here.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import CategoryChoice
from mfl_desktop.ui import tokens
from mfl_desktop.ui.category_picker import make_category_picker

_CREATE = "__create__"
_REVIEW = "__review__"


class ImportCategoryReviewDialog(QDialog):
    """Review the categories an import would create. ``decisions()`` returns
    ``{normalized_path: (kind, payload)}`` for :meth:`ImportService.commit_import`
    where kind is ``"map"`` (payload = category id), ``"create"`` or
    ``"review"`` (payload = ``None``)."""

    def __init__(
        self, items, categories: list[CategoryChoice], *, parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New categories in this import")
        self.setModal(True)
        self.resize(640, 460)
        self._items = list(items)
        self._categories = categories
        self._combos: dict[str, QComboBox] = {}

        intro = QLabel(
            f"This import has {len(self._items)} categor"
            f"{'y' if len(self._items) == 1 else 'ies'} that aren't in your "
            "list yet. For each, choose whether to map it to one you already "
            "have, create it, or send it to Needs Review. Mapped paths are "
            "remembered for future imports."
        )
        intro.setWordWrap(True)

        # Quick-set-all controls.
        all_create = QPushButton("Create all")
        all_create.clicked.connect(lambda: self._set_all(_CREATE))
        all_review = QPushButton("Send all to Needs Review")
        all_review.clicked.connect(lambda: self._set_all(_REVIEW))
        quick = QHBoxLayout()
        quick.addWidget(QLabel("Set all:"))
        quick.addWidget(all_create)
        quick.addWidget(all_review)
        quick.addStretch(1)

        # One row per new category: label + a single decision combo.
        rows_host = QWidget()
        rows = QVBoxLayout(rows_host)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        for item in self._items:
            rows.addLayout(self._build_row(item))
        rows.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(rows_host)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(quick)
        layout.addWidget(scroll, stretch=1)
        layout.addWidget(buttons)

    def _build_row(self, item):
        row = QHBoxLayout()
        label = QLabel(
            f"{item.raw}   ·   {item.txn_count} txn"
            f"{'' if item.txn_count == 1 else 's'}"
        )
        tokens.themed(label, "color: {text};")
        label.setMinimumWidth(220)

        combo = make_category_picker(self._categories)
        # Prepend the two non-category actions; categories follow (map-to).
        combo.insertItem(0, "➕  Create as a new category", _CREATE)
        combo.insertItem(1, "⚑  Send to Needs Review", _REVIEW)
        combo.insertSeparator(2)
        combo.setCurrentIndex(0)        # default: create (no behaviour change)
        self._combos[item.normalized] = combo

        row.addWidget(label, stretch=1)
        row.addWidget(combo, stretch=2)
        return row

    def _set_all(self, sentinel: str) -> None:
        for combo in self._combos.values():
            idx = combo.findData(sentinel)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def decisions(self) -> dict:
        """``{normalized_path: (kind, payload)}`` from the per-row choices.
        A row whose combo text doesn't resolve to a real selection falls back
        to ``create`` (the safe, no-surprise default)."""
        out: dict = {}
        for key, combo in self._combos.items():
            data = combo.currentData()
            # The editable combo can hold free text; only trust an item match.
            if combo.findText(combo.currentText()) < 0:
                data = _CREATE
            if data == _REVIEW:
                out[key] = ("review", None)
            elif isinstance(data, int):
                out[key] = ("map", data)
            else:
                out[key] = ("create", None)
        return out
