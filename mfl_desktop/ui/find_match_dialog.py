"""'Find a match' picker for import review (ADR-151 Phase 2).

When the automatic matcher leaves a bank row classified *new*, the user can open
this picker to link it to an existing transaction the matcher didn't flag — a
match beyond the date window, a different amount, or an unfamiliar payee string.
It lists the account's existing transactions (exact-amount first, then nearest
date) with a payee filter, and returns the chosen row's id + whether it's a
manual entry (which decides merge-vs-skip on commit).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.ui.chart_helpers import currency_symbol


class FindMatchDialog(QDialog):
    """Pick an existing transaction to match a still-new import row against."""

    def __init__(
        self, repo, account_id: int, *, new_date: str, new_amount_pence: int,
        new_payee: str, currency: str, parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find a match")
        self.setModal(True)
        self.resize(680, 460)
        self._sym = currency_symbol(currency) if currency else ""
        self._chosen: Optional[tuple[int, bool]] = None
        self._chosen_label = ""
        self._cands = repo.find_match_candidates(
            account_id, new_date, new_amount_pence,
        )
        self._visible: list = []

        head = QLabel(
            f"Matching <b>{new_payee or '(no payee)'}</b> — "
            f"{self._fmt(new_amount_pence)} on {new_date}.<br>"
            "Pick the existing transaction this is the same as. A manual entry "
            "is filled in with the bank's details; an already-imported one is "
            "skipped as a duplicate."
        )
        head.setWordWrap(True)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by payee…")
        self._search.textChanged.connect(self._refilter)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Date", "Payee", "Amount", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.doubleClicked.connect(lambda *_: self._accept_selected())

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._buttons.button(QDialogButtonBox.Ok).setText("Use this match")
        self._buttons.accepted.connect(self._accept_selected)
        self._buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(head)
        root.addWidget(self._search)
        root.addWidget(self._table, stretch=1)
        root.addWidget(self._buttons)

        self._render(self._cands)

    # ── public API ──

    def chosen(self) -> Optional[tuple[int, bool]]:
        """(existing_id, is_manual) of the picked row, or None if cancelled."""
        return self._chosen

    def chosen_label(self) -> str:
        return self._chosen_label

    # ── internals ──

    def _fmt(self, pence: int) -> str:
        val = pence / 100.0
        return f"{'-' if val < 0 else '+'}{self._sym}{abs(val):,.2f}"

    def _render(self, cands: list) -> None:
        self._table.setRowCount(0)
        self._visible = []
        for c in cands:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._visible.append(c)
            self._table.setItem(r, 0, QTableWidgetItem(c.posted_date))
            self._table.setItem(r, 1, QTableWidgetItem(c.payee_name or "(no payee)"))
            amt = QTableWidgetItem(self._fmt(c.amount_pence))
            amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(r, 2, amt)
            status = "manual" if c.is_manual else (c.status or "imported")
            self._table.setItem(r, 3, QTableWidgetItem(status))
        empty = not cands
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(not empty)

    def _refilter(self, text: str) -> None:
        t = text.strip().lower()
        if not t:
            self._render(self._cands)
            return
        self._render([
            c for c in self._cands if t in (c.payee_name or "").lower()
        ])

    def _accept_selected(self) -> None:
        r = self._table.currentRow()
        if r < 0 or r >= len(self._visible):
            return
        c = self._visible[r]
        self._chosen = (c.id, c.is_manual)
        self._chosen_label = f"{c.posted_date}  {c.payee_name or '(no payee)'}"
        self.accept()
