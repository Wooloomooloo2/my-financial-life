"""Manage → Reconcile Transfers… dialog (ADR-037).

Picks two accounts, runs ``Repository.find_transfer_pairs`` (greedy
bipartite match using the same scorer as the single-flow matcher), and
shows the proposed pairs in a table the user can check/uncheck before
applying. Cross-currency rows surface the implied-vs-spot deviation
inline so the user can sanity-check at a glance.

All writes go through ``Repository.bulk_match_or_create_transfers`` in
one SQL transaction; cancelling the dialog touches nothing.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import (
    LinkExisting,
    Repository,
    TransferPair,
)
from mfl_desktop.ui.category_picker import make_category_picker
from mfl_desktop.ui import tokens
from mfl_desktop.ui.transfer_chips import (
    fmt_amount as _fmt_amount,
    strength_chip_holder as _strength_chip_widget,
)


def _payee_item(
    payee: str, split_id: Optional[int], split_memo: Optional[str],
) -> QTableWidgetItem:
    """Payee cell for a pair side. A split-line side is flagged inline with a
    'split' tag + its line memo, so the user sees the match is a line inside a
    larger split rather than a whole transaction (ADR-139)."""
    text = payee or "(no payee)"
    item = QTableWidgetItem(text)
    if split_id is not None:
        tag = f"split: {split_memo}" if split_memo else "split line"
        item.setText(f"{text}  ·  {tag}")
        item.setToolTip(
            "This side is one line of a split transaction; only that line "
            "is linked as the transfer."
        )
    return item


class TransferReconcileDialog(QDialog):
    """Pick two accounts → see proposed pairs → check / uncheck → apply.

    The dialog is modal; on Apply it commits via the Repository's bulk
    helper in one SQL transaction. After Apply succeeds, the dialog
    re-runs the pair search so the user can do a second pass with
    different settings (e.g. widen tolerance) without re-opening.
    """

    def __init__(
        self,
        repo: Repository,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Reconcile transfers")
        self.setMinimumWidth(760)
        self.setMinimumHeight(560)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # ── Account pickers ───────────────────────────────────────────────
        self._accounts = self._repo.list_accounts()
        self._account_a = QComboBox()
        self._account_b = QComboBox()
        for acct in self._accounts:
            label = f"{acct.name}  ·  {acct.currency}"
            self._account_a.addItem(label, userData=acct.id)
            self._account_b.addItem(label, userData=acct.id)
        if len(self._accounts) >= 2:
            self._account_b.setCurrentIndex(1)
        self._account_a.currentIndexChanged.connect(self._on_accounts_changed)
        self._account_b.currentIndexChanged.connect(self._on_accounts_changed)

        acct_row = QHBoxLayout()
        acct_row.addWidget(QLabel("Account A:"))
        acct_row.addWidget(self._account_a, 1)
        acct_row.addSpacing(12)
        acct_row.addWidget(QLabel("Account B:"))
        acct_row.addWidget(self._account_b, 1)
        outer.addLayout(acct_row)

        # ── Tunables read-only summary (live from setting table) ──────────
        win_days = self._repo.get_setting("transfer_match_window_days") or "3"
        fx_tol = self._repo.get_setting("transfer_fx_tolerance_pct") or "1.0"
        tunables = QLabel(
            f"Window: ±{win_days} days · FX tolerance: ±{fx_tol}% "
            f"<span style='color:{tokens.c('muted')}'>"
            f"(change in Manage ▸ Currencies)</span>"
        )
        tunables.setTextFormat(Qt.RichText)
        tokens.themed(tunables, "QLabel { color: {muted_strong}; font-size: 11px; }")
        outer.addWidget(tunables)

        # ── Proposed pairs table ──────────────────────────────────────────
        pairs_box = QGroupBox("Proposed pairs")
        pairs_layout = QVBoxLayout(pairs_box)

        self._table = QTableWidget(0, 8, self)
        # Columns: ✓ / strength / source date / source amount / source payee /
        #          target date / target amount / target payee
        self._table.setHorizontalHeaderLabels([
            "✓", "Strength", "A date", "A amount", "A payee",
            "B date", "B amount", "B payee",
        ])
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        for col, mode in enumerate([
            QHeaderView.ResizeToContents,    # ✓
            QHeaderView.ResizeToContents,    # strength
            QHeaderView.ResizeToContents,    # A date
            QHeaderView.ResizeToContents,    # A amount
            QHeaderView.Stretch,             # A payee
            QHeaderView.ResizeToContents,    # B date
            QHeaderView.ResizeToContents,    # B amount
            QHeaderView.Stretch,             # B payee
        ]):
            header.setSectionResizeMode(col, mode)
        pairs_layout.addWidget(self._table)

        # Strength shortcut buttons.
        shortcut_row = QHBoxLayout()
        self._match_strong_btn = QPushButton("Check all Strong")
        self._match_strong_btn.clicked.connect(
            lambda: self._check_by_strength({"Strong"})
        )
        self._match_good_btn = QPushButton("Check all Strong + Good")
        self._match_good_btn.clicked.connect(
            lambda: self._check_by_strength({"Strong", "Good"})
        )
        self._uncheck_btn = QPushButton("Uncheck all")
        self._uncheck_btn.clicked.connect(lambda: self._check_by_strength(set()))
        shortcut_row.addWidget(self._match_strong_btn)
        shortcut_row.addWidget(self._match_good_btn)
        shortcut_row.addWidget(self._uncheck_btn)
        shortcut_row.addStretch(1)
        pairs_layout.addLayout(shortcut_row)

        outer.addWidget(pairs_box, 1)

        # ── Category for matched rows ─────────────────────────────────────
        # Restrict the picker to transfer-kind categories — picking an
        # expense category here would make every linked pair non-self-
        # cancelling in the spending report (ADR-018).
        all_cats = self._repo.list_categories_flat()
        transfer_cats = [c for c in all_cats if c.kind == "transfer"]
        default_tcat = self._repo.get_default_transfer_category_id()
        self._category_picker = make_category_picker(
            transfer_cats, default_id=default_tcat,
        )
        cat_form = QFormLayout()
        cat_form.addRow(
            "Category for matched pairs:", self._category_picker,
        )
        outer.addLayout(cat_form)

        # ── Buttons ───────────────────────────────────────────────────────
        self._status_label = QLabel("")
        tokens.themed(self._status_label, "QLabel { color: {muted_strong}; font-size: 11px; }")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Apply | QDialogButtonBox.Close
        )
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self._status_label, 1)
        bottom.addWidget(buttons)
        outer.addLayout(bottom)

        self._pairs: list[TransferPair] = []
        self._reload_pairs()

    # ── data refreshers ─────────────────────────────────────────────────

    def _on_accounts_changed(self) -> None:
        # Don't trigger on programmatic init noise; just always reload.
        self._reload_pairs()

    def _reload_pairs(self) -> None:
        a_id = self._account_a.currentData()
        b_id = self._account_b.currentData()
        self._pairs = []
        self._table.setRowCount(0)
        if a_id is None or b_id is None:
            self._status_label.setText("Pick both accounts.")
            return
        if a_id == b_id:
            self._status_label.setText(
                "Pick two different accounts to compare."
            )
            return
        try:
            pairs = self._repo.find_transfer_pairs(
                account_a_id=a_id, account_b_id=b_id,
            )
        except Exception as e:
            self._status_label.setText(f"Error: {e}")
            return
        self._pairs = pairs
        self._populate_table()
        # Default: check every Strong pair so the common case is one click.
        self._check_by_strength({"Strong"})
        n_strong = sum(1 for p in pairs if p.strength == "Strong")
        n_good = sum(1 for p in pairs if p.strength == "Good")
        n_poss = sum(1 for p in pairs if p.strength == "Possible")
        self._status_label.setText(
            f"{len(pairs)} pair{'s' if len(pairs) != 1 else ''} found · "
            f"{n_strong} Strong / {n_good} Good / {n_poss} Possible"
        )

    def _populate_table(self) -> None:
        self._table.setRowCount(len(self._pairs))
        for i, p in enumerate(self._pairs):
            chk = QCheckBox()
            chk_holder = QWidget()
            chk_layout = QHBoxLayout(chk_holder)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.addWidget(chk)
            self._table.setCellWidget(i, 0, chk_holder)

            self._table.setCellWidget(i, 1, _strength_chip_widget(p.strength))

            self._table.setItem(i, 2, QTableWidgetItem(p.source_posted_date))
            amt_a = QTableWidgetItem(
                _fmt_amount(p.source_amount, p.source_currency),
            )
            amt_a.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 3, amt_a)
            self._table.setItem(i, 4, _payee_item(
                p.source_payee, p.source_split_id, p.source_split_memo,
            ))

            self._table.setItem(i, 5, QTableWidgetItem(p.target_posted_date))
            amt_b = QTableWidgetItem(
                _fmt_amount(p.target_amount, p.target_currency),
            )
            amt_b.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 6, amt_b)
            self._table.setItem(i, 7, _payee_item(
                p.target_payee, p.target_split_id, p.target_split_memo,
            ))

            # Cross-currency: tint the payee cell with a rate note tooltip
            # so the user can hover for the spot-vs-implied detail.
            if (
                p.source_currency != p.target_currency
                and p.implied_rate is not None
                and p.spot_rate is not None
            ):
                tip = (
                    f"Implied {p.implied_rate:.4f}  ·  "
                    f"Spot {p.spot_rate:.4f}  ·  "
                    f"Δ {p.rate_deviation_pct:+.2f}%"
                )
                for col in (4, 7):
                    item = self._table.item(i, col)
                    if item:
                        item.setToolTip(tip)

    def _check_by_strength(self, strengths: set[str]) -> None:
        """Set the checkbox state of every row whose pair.strength is in
        ``strengths``. Empty set un-checks everything (Uncheck All)."""
        for i, p in enumerate(self._pairs):
            holder = self._table.cellWidget(i, 0)
            if holder is None:
                continue
            chk = holder.findChild(QCheckBox)
            if chk is None:
                continue
            chk.setChecked(p.strength in strengths)

    def _checked_pairs(self) -> list[TransferPair]:
        out: list[TransferPair] = []
        for i, p in enumerate(self._pairs):
            holder = self._table.cellWidget(i, 0)
            if holder is None:
                continue
            chk = holder.findChild(QCheckBox)
            if chk is not None and chk.isChecked():
                out.append(p)
        return out

    # ── apply ────────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        checked = self._checked_pairs()
        if not checked:
            QMessageBox.information(
                self, "Nothing checked",
                "Tick the pairs you want to link, then Apply.",
            )
            return
        category_id = self._category_picker.currentData()
        if category_id is None:
            QMessageBox.warning(
                self, "Pick a category",
                "Choose the transfer category to apply to matched pairs.",
            )
            return
        category_id = int(category_id)
        plan = [
            LinkExisting(
                source_txn_id=p.source_txn_id,
                candidate_txn_id=p.target_txn_id,
                category_id=category_id,
                source_split_id=p.source_split_id,      # ADR-139
                candidate_split_id=p.target_split_id,
            )
            for p in checked
        ]
        try:
            result = self._repo.bulk_match_or_create_transfers(plan)
        except Exception as e:
            QMessageBox.critical(
                self, "Reconcile failed",
                f"The change was not applied:\n\n{e}",
            )
            return
        # Re-fetch so the matched rows drop from the list and the user can
        # do another pass with different settings if they want.
        self._reload_pairs()
        msg = (
            f"Linked {result.linked} pair{'s' if result.linked != 1 else ''}."
        )
        self._status_label.setText(msg)
