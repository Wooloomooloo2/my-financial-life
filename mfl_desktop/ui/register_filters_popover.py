"""Register "Filters ▾" popover — date-range and amount-range filters (ADR-062).

A small non-modal panel dropped under the filter bar's Filters button. Each of
the four bounds (date From, date To, amount Min, amount Max) is gated by its own
checkbox, so an unchecked end means "unbounded there" — you can filter on just a
lower date, just a maximum amount, etc. Amounts are **signed** (a Max of −500
isolates large outflows). The panel applies live: any change emits
``filters_changed(date_from, date_to, amount_min, amount_max)`` with ISO-date
strings / ``Decimal`` values (or ``None`` for an unchecked/empty bound).

It's a plain non-modal ``QDialog`` (not a ``Qt::Popup`` / ``QMenu``) on purpose
— the date fields use calendar popups, and nesting those inside a popup-flagged
container makes the container self-dismiss. The owning window positions it under
the button and toggles it; the active-filter indicator lives on the button.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_AMOUNT_LIMIT = 1_000_000_000.0  # generous signed bound for the spin boxes


class RegisterFiltersPopover(QDialog):
    filters_changed = Signal(object, object, object, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filters")
        # A normal non-modal tool window: stays open while the register is
        # edited, doesn't grab app focus, and lets the date calendar popups
        # behave. The owner closes it via the button toggle or Close.
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setModal(False)

        today = QDate.currentDate()

        # ── date range ──
        self._from_check = QCheckBox("From:")
        self._from_edit = QDateEdit(today)
        self._from_edit.setCalendarPopup(True)
        self._from_edit.setDisplayFormat("d MMM yyyy")
        self._from_edit.setEnabled(False)

        self._to_check = QCheckBox("To:")
        self._to_edit = QDateEdit(today)
        self._to_edit.setCalendarPopup(True)
        self._to_edit.setDisplayFormat("d MMM yyyy")
        self._to_edit.setEnabled(False)

        # ── amount range (signed) ──
        self._min_check = QCheckBox("Min:")
        self._min_spin = self._make_amount_spin()
        self._min_spin.setEnabled(False)

        self._max_check = QCheckBox("Max:")
        self._max_spin = self._make_amount_spin()
        self._max_spin.setEnabled(False)

        hint = QLabel("Amounts are signed — e.g. Max −500 finds outflows over 500.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b; font-size: 11px;")

        clear_btn = QPushButton("Clear filters")
        clear_btn.clicked.connect(self.clear)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.hide)

        # ── layout ──
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("Date range"), 0, 0, 1, 2)
        grid.addWidget(self._from_check, 1, 0)
        grid.addWidget(self._from_edit, 1, 1)
        grid.addWidget(self._to_check, 2, 0)
        grid.addWidget(self._to_edit, 2, 1)
        grid.addWidget(QLabel("Amount range"), 3, 0, 1, 2)
        grid.addWidget(self._min_check, 4, 0)
        grid.addWidget(self._min_spin, 4, 1)
        grid.addWidget(self._max_check, 5, 0)
        grid.addWidget(self._max_spin, 5, 1)

        buttons = QHBoxLayout()
        buttons.addWidget(clear_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        layout.addLayout(grid)
        layout.addWidget(hint)
        layout.addLayout(buttons)

        # ── wiring: every change re-emits the current filter state ──
        self._from_check.toggled.connect(self._from_edit.setEnabled)
        self._to_check.toggled.connect(self._to_edit.setEnabled)
        self._min_check.toggled.connect(self._min_spin.setEnabled)
        self._max_check.toggled.connect(self._max_spin.setEnabled)
        for w in (self._from_check, self._to_check,
                  self._min_check, self._max_check):
            w.toggled.connect(self._emit)
        self._from_edit.dateChanged.connect(self._emit)
        self._to_edit.dateChanged.connect(self._emit)
        self._min_spin.valueChanged.connect(self._emit)
        self._max_spin.valueChanged.connect(self._emit)

    @staticmethod
    def _make_amount_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(-_AMOUNT_LIMIT, _AMOUNT_LIMIT)
        spin.setGroupSeparatorShown(True)
        spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        return spin

    # ── public API ──

    def is_active(self) -> bool:
        return any(c.isChecked() for c in (
            self._from_check, self._to_check,
            self._min_check, self._max_check,
        ))

    def current_values(self):
        date_from = (
            self._from_edit.date().toString("yyyy-MM-dd")
            if self._from_check.isChecked() else None
        )
        date_to = (
            self._to_edit.date().toString("yyyy-MM-dd")
            if self._to_check.isChecked() else None
        )
        amt_min = (
            Decimal(str(self._min_spin.value()))
            if self._min_check.isChecked() else None
        )
        amt_max = (
            Decimal(str(self._max_spin.value()))
            if self._max_check.isChecked() else None
        )
        return date_from, date_to, amt_min, amt_max

    def clear(self) -> None:
        for c in (self._from_check, self._to_check,
                  self._min_check, self._max_check):
            c.setChecked(False)
        # _emit fires via the toggled connections; one explicit emit covers the
        # already-unchecked case (no toggle signal) too.
        self._emit()

    def popup_under(self, anchor: QWidget) -> None:
        """Show positioned just below the anchor widget (the Filters button)."""
        below = anchor.mapToGlobal(anchor.rect().bottomLeft())
        self.adjustSize()
        self.move(below)
        self.show()
        self.raise_()
        self.activateWindow()

    # ── internals ──

    def _emit(self, *args) -> None:
        self.filters_changed.emit(*self.current_values())
