"""Custom date-range picker for the per-account summary screen.

Opened from the "Custom" button in the period selector (ADR-033
amendment 2026-06-06). Two ``QDateEdit`` fields with calendar popups,
constrained so From ≤ To. Defaults to the period the caller was
already showing — opening Custom while on "Last 12 months" pre-fills
the dialog with that range, so the owner can tweak rather than restart.

Returns ``(date_from, date_to)`` on accept via :py:meth:`values`; the
caller decides what to do on Rejected (typically: restore the previous
preset selection without changing the visible range).
"""
from __future__ import annotations
from mfl_desktop.ui import tokens

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)


class CustomPeriodDialog(QDialog):
    """Modal date-range dialog. Construct with the existing bounds so
    Custom opens "where you were"; call :py:meth:`exec` and read
    :py:meth:`values` after Accepted."""

    def __init__(
        self,
        *,
        initial_from: date,
        initial_to: date,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Custom date range")
        self.setModal(True)

        intro = QLabel(
            "Pick the date range you want to see on the summary. "
            "Both ends are inclusive."
        )
        intro.setWordWrap(True)
        tokens.themed(intro, "color: {muted_strong};")

        self._from_edit = QDateEdit(QDate(initial_from.year,
                                          initial_from.month,
                                          initial_from.day))
        self._from_edit.setCalendarPopup(True)
        self._from_edit.setDisplayFormat("yyyy-MM-dd")
        # Keep From ≤ today so the user can't pick a future start; the
        # chart only shows posted txns.
        self._from_edit.setMaximumDate(QDate.currentDate())

        self._to_edit = QDateEdit(QDate(initial_to.year,
                                        initial_to.month,
                                        initial_to.day))
        self._to_edit.setCalendarPopup(True)
        self._to_edit.setDisplayFormat("yyyy-MM-dd")
        self._to_edit.setMaximumDate(QDate.currentDate())

        form = QFormLayout()
        form.addRow("From:", self._from_edit)
        form.addRow("To:",   self._to_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)

    # ── public API ──

    def values(self) -> tuple[date, date]:
        """Reads the dialog state — only call after Accepted."""
        return self._qdate_to_date(self._from_edit.date()), \
               self._qdate_to_date(self._to_edit.date())

    # ── validation ──

    def _on_ok(self) -> None:
        d_from = self._qdate_to_date(self._from_edit.date())
        d_to = self._qdate_to_date(self._to_edit.date())
        if d_from > d_to:
            QMessageBox.warning(
                self, "Invalid range",
                "The From date must be on or before the To date.",
            )
            return
        self.accept()

    @staticmethod
    def _qdate_to_date(qd: QDate) -> date:
        return date(qd.year(), qd.month(), qd.day())
