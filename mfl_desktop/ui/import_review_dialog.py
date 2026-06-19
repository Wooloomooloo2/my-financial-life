"""Import duplicate-review dialog (ADR-085).

Shown **only when** the cross-source duplicate pass flags one or more incoming
rows as likely already present (a clean import still commits silently — the
no-dialog-for-known-imports rule holds when there's nothing to ask). Lists each
flagged row beside the existing transaction it matched, with a strength chip and
an "Already present?" toggle.

Defaults (ADR-085, owner-locked): **strong** matches (same-day, or a manual
placeholder, or a payee-token overlap) are pre-ticked; **weak** matches (exact
amount within the window but a different date and no payee overlap) are left
unticked so the user opts in. The consume-once pairing upstream guarantees the
*counts* are right — if you spent £8.90 three times and only one is already in,
exactly one row is proposed for skipping.

On confirm a ticked row is either **merged** into a hand-typed placeholder
(manual target) or **skipped** as a duplicate (already-imported target); an
unticked row is **added**. ``accepted_fitids()`` returns the ticked rows for
``ImportService.commit_import``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.ui import tokens

if TYPE_CHECKING:
    from mfl_desktop.import_engine.import_service import (
        ClassifiedTransaction, PendingImport,
    )

_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}


def _sym(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get((currency or "").upper(), (currency or "") + " ")


class ImportReviewDialog(QDialog):
    """Modal review of flagged duplicates. Accept commits the user's choices."""

    # Strength → chip styling (light-mode anchored; tokens keep dark legible).
    _CHIP = {
        "strong": ("Strong", "{positive_strong}"),
        "weak":   ("Possible", "{caution}"),
    }

    def __init__(self, pending: "PendingImport", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Review import — {pending.account_name}")
        self.setModal(True)
        self.resize(860, 540)

        self._currency = pending.currency
        self._matches: list["ClassifiedTransaction"] = [
            tx for tx in pending.transactions if tx.status == "potential_match"
        ]
        n_new = sum(1 for tx in pending.transactions if tx.status == "new")
        total = len(self._matches) + n_new

        header = QLabel(
            f"<b>{len(self._matches)}</b> of {total} transactions look like "
            f"they're already in <b>{pending.account_name}</b>. "
            "Ticked rows are skipped (or merged into a manual entry); "
            "unticked rows are added.<br>"
            "Repeats are counted — if you really did spend the same amount "
            "more than once, only the copies already on file are proposed for "
            "skipping."
        )
        header.setWordWrap(True)

        self._table = QTableWidget(len(self._matches), 6)
        self._table.setHorizontalHeaderLabels([
            "Already\npresent?", "Importing", "Amount",
            "Matches existing", "Match", "On confirm",
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        for i, tx in enumerate(self._matches):
            self._populate_row(i, tx)

        # ── bulk actions ──
        skip_all = QPushButton("Skip all")
        keep_all = QPushButton("Keep all")
        reset = QPushButton("Reset to suggested")
        skip_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        keep_all.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        reset.clicked.connect(self._reset_to_suggested)
        bulk = QHBoxLayout()
        bulk.setContentsMargins(0, 0, 0, 0)
        bulk.addWidget(skip_all)
        bulk.addWidget(keep_all)
        bulk.addWidget(reset)
        bulk.addStretch(1)
        bulk_w = QWidget()
        bulk_w.setLayout(bulk)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(header)
        root.addWidget(bulk_w)
        root.addWidget(self._table, stretch=1)
        root.addWidget(buttons)

    # ── public API ──

    def accepted_fitids(self) -> set[str]:
        """Fitids of the rows ticked 'already present' (merge or skip)."""
        out: set[str] = set()
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item is not None and item.checkState() == Qt.Checked:
                out.add(str(item.data(Qt.UserRole)))
        return out

    # ── internals ──

    def _populate_row(self, i: int, tx: "ClassifiedTransaction") -> None:
        # col 0 — checkable; default ticked for strong matches.
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk.setCheckState(
            Qt.Checked if tx.match_strength == "strong" else Qt.Unchecked
        )
        chk.setData(Qt.UserRole, tx.fitid)
        self._table.setItem(i, 0, chk)

        self._table.setItem(
            i, 1, QTableWidgetItem(f"{tx.date_iso}   {tx.payee_raw}"),
        )
        amt = QTableWidgetItem(self._fmt_amount(tx))
        amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(i, 2, amt)
        self._table.setItem(
            i, 3, QTableWidgetItem(f"{tx.match_txn_date}   {tx.match_txn_payee}"),
        )
        self._table.setCellWidget(i, 4, self._chip(tx.match_strength))
        action = "Merge" if tx.match_is_manual else "Skip"
        self._table.setItem(i, 5, QTableWidgetItem(action))

    def _fmt_amount(self, tx: "ClassifiedTransaction") -> str:
        sym = _sym(self._currency)
        sign = "-" if tx.tx_type == "debit" else "+"
        return f"{sign}{sym}{abs(float(tx.amount)):,.2f}"

    def _chip(self, strength: str) -> QWidget:
        label, colour_token = self._CHIP.get(strength, self._CHIP["weak"])
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(4, 2, 4, 2)
        chip = QLabel(label)
        chip.setAlignment(Qt.AlignCenter)
        tokens.themed(
            chip,
            "color: white; background: " + colour_token + "; "
            "border-radius: 8px; padding: 2px 8px; "
            "font-weight: 600; font-size: 11px;",
        )
        lay.addWidget(chip)
        lay.addStretch(1)
        return holder

    def _set_all(self, state: Qt.CheckState) -> None:
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item is not None:
                item.setCheckState(state)

    def _reset_to_suggested(self) -> None:
        for i, tx in enumerate(self._matches):
            item = self._table.item(i, 0)
            if item is not None:
                item.setCheckState(
                    Qt.Checked if tx.match_strength == "strong" else Qt.Unchecked
                )
