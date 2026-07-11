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

ADR-151 Phase 2: below the flagged matches, the dialog also lists the **net-new**
rows this import will add, each with a **Find match…** action — a picker over the
account's existing transactions — so the user can hand-link a row the automatic
matcher missed (a match beyond the date window, a different amount, an unfamiliar
payee). ``found_matches()`` returns those choices; the caller reclassifies them
as accepted matches before commit.
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

    def __init__(self, pending: "PendingImport", repo=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Review import — {pending.account_name}")
        self.setModal(True)
        self.resize(880, 600)

        self._currency = pending.currency
        self._repo = repo
        self._account_id = pending.account_id
        self._account_name = pending.account_name
        self._matches: list["ClassifiedTransaction"] = [
            tx for tx in pending.transactions if tx.status == "potential_match"
        ]
        self._new_rows: list["ClassifiedTransaction"] = [
            tx for tx in pending.transactions if tx.status == "new"
        ]
        # fitid -> (existing_id, is_manual, label) for user-found matches (ADR-151
        # Phase 2). These reclassify a 'new' row as an accepted match at commit.
        self._found: dict[str, tuple[int, bool, str]] = {}
        n_new = len(self._new_rows)
        total = len(self._matches) + n_new

        header = QLabel(
            f"<b>{len(self._matches)}</b> of {total} incoming transactions look "
            f"like they're already in <b>{pending.account_name}</b>; the other "
            f"<b>{n_new}</b> will be added. Ticked rows are skipped (or merged "
            "into a manual entry); unticked rows are added.<br>"
            "Repeats are counted — if you really did spend the same amount "
            "more than once, only the copies already on file are proposed for "
            "skipping."
        )
        header.setWordWrap(True)

        self._table = QTableWidget(len(self._matches), 7)
        self._table.setHorizontalHeaderLabels([
            "Already\npresent?", "Importing", "Amount",
            "Matches existing", "Match", "On confirm", "Adopt\nbank amt",
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
        hh.setSectionResizeMode(6, QHeaderView.ResizeToContents)

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
        root.addWidget(self._table, stretch=2)
        new_section = self._build_new_section()
        if new_section is not None:
            root.addWidget(new_section, stretch=1)
        root.addWidget(buttons)

    # ── public API ──

    def found_matches(self) -> dict:
        """fitid -> (existing_id, is_manual) for rows the user matched by hand
        via 'Find match…' (ADR-151 Phase 2). The caller reclassifies these as
        accepted matches before commit."""
        return {fid: (eid, man) for fid, (eid, man, _lbl) in self._found.items()}

    def accepted_fitids(self) -> set[str]:
        """Fitids of the rows ticked 'already present' (merge or skip)."""
        out: set[str] = set()
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item is not None and item.checkState() == Qt.Checked:
                out.add(str(item.data(Qt.UserRole)))
        return out

    def adopted_amount_fitids(self) -> set[str]:
        """Fitids of amount-mismatch rows where the user opted to adopt the
        bank's amount (ADR-130 Phase 3b). Only meaningful for rows also ticked
        'already present' — commit_import applies it on the merge path."""
        out: set[str] = set()
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 6)
            if (
                item is not None
                and bool(item.flags() & Qt.ItemIsUserCheckable)
                and item.checkState() == Qt.Checked
            ):
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
        # For an amount-mismatch match, show what the user typed beside the
        # existing row so the discrepancy is obvious (ADR-130 Phase 3b).
        existing = f"{tx.match_txn_date}   {tx.match_txn_payee}"
        if tx.match_amount_differs and tx.match_existing_amount is not None:
            existing += f"   (yours: {self._fmt_signed(tx.match_existing_amount)})"
        self._table.setItem(i, 3, QTableWidgetItem(existing))
        self._table.setCellWidget(i, 4, self._chip(tx.match_strength))
        action = "Merge" if tx.match_is_manual else "Skip"
        self._table.setItem(i, 5, QTableWidgetItem(action))

        # col 6 — "adopt bank amount" (amount-differs rows only). Default ticked
        # so accepting the match also fixes the mis-entry; blank + disabled for
        # every other row.
        adopt = QTableWidgetItem()
        if tx.match_amount_differs and tx.match_is_manual:
            adopt.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            adopt.setCheckState(Qt.Checked)
            adopt.setData(Qt.UserRole, tx.fitid)
            adopt.setToolTip(
                f"Overwrite your {self._fmt_signed(tx.match_existing_amount)} "
                f"with the bank's {self._fmt_amount(tx)} on import."
            )
        else:
            adopt.setFlags(Qt.NoItemFlags)
        self._table.setItem(i, 6, adopt)

    def _fmt_amount(self, tx: "ClassifiedTransaction") -> str:
        sym = _sym(self._currency)
        sign = "-" if tx.tx_type == "debit" else "+"
        return f"{sign}{sym}{abs(float(tx.amount)):,.2f}"

    def _fmt_signed(self, amount) -> str:
        """Format a signed Decimal amount (negative = money out)."""
        sym = _sym(self._currency)
        val = float(amount or 0)
        return f"{'-' if val < 0 else '+'}{sym}{abs(val):,.2f}"

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

    # ── net-new section + 'find a match' (ADR-151 Phase 2) ──

    def _new_signed_pence(self, tx) -> int:
        mag = int(tx.amount * 100)
        return -mag if tx.tx_type == "debit" else mag

    def _build_new_section(self):
        """The net-new rows this import will add, each with a 'Find match…'
        action to hand-link it to an existing transaction the matcher missed.
        Returns None when there's nothing new or no repo to search."""
        if not self._new_rows or self._repo is None:
            return None
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._new_label = QLabel()
        self._new_label.setWordWrap(True)
        lay.addWidget(self._new_label)

        self._new_table = QTableWidget(len(self._new_rows), 3)
        self._new_table.setHorizontalHeaderLabels(
            ["Adding", "Amount", "Already recorded?"]
        )
        self._new_table.verticalHeader().setVisible(False)
        self._new_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._new_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._new_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for i, tx in enumerate(self._new_rows):
            self._new_table.setItem(
                i, 0, QTableWidgetItem(f"{tx.date_iso}   {tx.payee_raw}"),
            )
            amt = QTableWidgetItem(self._fmt_amount(tx))
            amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._new_table.setItem(i, 1, amt)
            self._new_table.setCellWidget(i, 2, self._new_match_cell(i, tx))
        lay.addWidget(self._new_table)
        self._update_new_label()
        return wrap

    def _update_new_label(self) -> None:
        n = len(self._new_rows)
        matched = len(self._found)
        adding = n - matched
        extra = f" · {matched} matched to an existing entry" if matched else ""
        self._new_label.setText(
            f"<b>{adding}</b> new transaction{'s' if adding != 1 else ''} will be "
            f"added to <b>{self._account_name}</b>{extra}.  "
            "Spot one that's already recorded? Use <i>Find match…</i>."
        )

    def _new_match_cell(self, row: int, tx) -> QWidget:
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        found = self._found.get(tx.fitid)
        lay.addStretch(1)
        if found is None:
            btn = QPushButton("Find match…")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda _=False, t=tx, r=row: self._open_find_match(t, r)
            )
            lay.addWidget(btn)
        else:
            _eid, is_manual, label = found
            verb = "merge into" if is_manual else "skip vs"
            tag = QLabel(f"→ {verb} {label}")
            tokens.themed(tag, "color: {positive_strong}; font-weight: 600;")
            clear = QPushButton("✕")
            clear.setToolTip("Not a match — add as new")
            clear.setCursor(Qt.PointingHandCursor)
            clear.setFixedWidth(28)
            clear.clicked.connect(
                lambda _=False, t=tx, r=row: self._clear_found(t, r)
            )
            lay.addWidget(tag)
            lay.addWidget(clear)
        return holder

    def _open_find_match(self, tx, row: int) -> None:
        from mfl_desktop.ui.find_match_dialog import FindMatchDialog
        dlg = FindMatchDialog(
            self._repo, self._account_id,
            new_date=tx.date_iso, new_amount_pence=self._new_signed_pence(tx),
            new_payee=tx.payee_raw, currency=self._currency, parent=self,
        )
        if dlg.exec() == QDialog.Accepted and dlg.chosen() is not None:
            eid, is_manual = dlg.chosen()
            self._found[tx.fitid] = (eid, is_manual, dlg.chosen_label())
            self._new_table.setCellWidget(row, 2, self._new_match_cell(row, tx))
            self._update_new_label()

    def _clear_found(self, tx, row: int) -> None:
        self._found.pop(tx.fitid, None)
        self._new_table.setCellWidget(row, 2, self._new_match_cell(row, tx))
        self._update_new_label()
