"""Dialogs that surface the transfer matcher (ADR-036) in the register flow.

When the user marks an existing row as a transfer (inline category edit
or bulk edit) and picks the destination account, the dispatcher in
``register_window.py`` runs ``Repository.find_transfer_candidates``. If
candidates exist, one of these dialogs offers them; the user either
links to an existing row or falls through to today's create-partner
behaviour.

``TransferMatchConfirmDialog`` — one candidate, [Match] / [Create new] / [Cancel].
``TransferMatchPickerDialog`` — many candidates, ranked list + [Create new] / [OK] / [Cancel].

Both return the chosen ``TransferCandidate`` via ``result_candidate()``,
or ``None`` when the user explicitly chose Create new (a recognised
acceptance, distinct from cancel which surfaces as ``QDialog.Rejected``).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dataclasses import dataclass

from mfl_desktop.db.repository import (
    BulkTransferDecision,
    CreateNew,
    LinkExisting,
    TransferCandidate,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui import type_scale
from mfl_desktop.ui.transfer_chips import fmt_amount as _fmt_amount, strength_chip as _strength_chip


def _source_summary_label(
    *, source_account: str, source_amount: Decimal, source_currency: str,
    source_date: str, source_payee: str,
) -> QLabel:
    """Header line shown above the candidate list — reminds the user what
    they're matching from."""
    amt = _fmt_amount(source_amount, source_currency)
    payee = source_payee or "—"
    text = (
        f"<b>From {source_account}</b> · {source_date} · "
        f"<span style='color:#0F172A'>{amt}</span> · {payee}"
    )
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.RichText)
    tokens.themed(lbl, "QLabel { color: {muted_strong}; }")
    return lbl


class TransferMatchConfirmDialog(QDialog):
    """One-candidate confirm dialog (ADR-036).

    Three outcomes:
    - **Match**: dialog accepted, ``result_candidate()`` returns the candidate.
    - **Create new**: dialog accepted, ``result_candidate()`` returns ``None``.
    - **Cancel**: dialog rejected; caller bails out.
    """

    def __init__(
        self,
        *,
        candidate: TransferCandidate,
        source_account: str,
        source_amount: Decimal,
        source_currency: str,
        source_date: str,
        source_payee: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Match to existing transaction?")
        self.setMinimumWidth(520)
        self._candidate = candidate
        self._chose_create_new = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            f"Found one possible match in <b>{candidate.account_name}</b>. "
            f"Link the two transactions as a transfer, or create a new "
            f"partner row instead?"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(_source_summary_label(
            source_account=source_account,
            source_amount=source_amount,
            source_currency=source_currency,
            source_date=source_date,
            source_payee=source_payee,
        ))

        # Candidate card — bordered frame mirroring the section-card style
        # introduced by ADR-034.
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        tokens.themed(card, "QFrame { background: white; border: 1px solid {border}; border-radius: 10px; padding: 10px; }")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        row1 = QHBoxLayout()
        amt_lbl = QLabel(
            f"<b>{_fmt_amount(candidate.amount, candidate.account_currency)}</b> "
            f"· {candidate.posted_date}"
        )
        amt_lbl.setTextFormat(Qt.RichText)
        amt_lbl.setStyleSheet(f"QLabel {{ {type_scale.fs(type_scale.LEAD)}; }}")
        row1.addWidget(amt_lbl, 1)
        row1.addWidget(_strength_chip(candidate.strength))
        card_layout.addLayout(row1)

        meta = []
        payee = candidate.payee_name or "(no payee)"
        meta.append(payee)
        days_word = "same day" if candidate.days_apart == 0 else (
            f"{abs(candidate.days_apart)} day"
            f"{'s' if abs(candidate.days_apart) != 1 else ''} "
            f"{'later' if candidate.days_apart > 0 else 'earlier'}"
        )
        meta.append(days_word)
        if not candidate.currencies_match:
            # Cross-currency: surface the implied rate so the user can
            # sanity-check at a glance.
            if candidate.expected_amount and candidate.expected_amount > 0:
                meta.append(
                    f"expected ≈ "
                    f"{_fmt_amount(candidate.expected_amount, candidate.account_currency)}"
                )
        meta_lbl = QLabel(" · ".join(meta))
        tokens.themed(meta_lbl, "QLabel { color: {muted_strong}; font-size: 12px; }")
        card_layout.addWidget(meta_lbl)
        layout.addWidget(card)

        # Buttons — explicit three-way verbs rather than the standard
        # OK/Cancel pair, because "Create new" is a real acceptance.
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        create_btn = QPushButton("Create new partner")
        create_btn.clicked.connect(self._accept_as_create_new)
        match_btn = QPushButton("Match")
        match_btn.setDefault(True)
        match_btn.setAutoDefault(True)
        match_btn.clicked.connect(self.accept)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(create_btn)
        button_row.addWidget(match_btn)
        layout.addLayout(button_row)

    def _accept_as_create_new(self) -> None:
        self._chose_create_new = True
        self.accept()

    def result_candidate(self) -> Optional[TransferCandidate]:
        """Returns the chosen candidate, or ``None`` if the user picked
        "Create new partner". Only meaningful when ``exec()`` returned
        ``QDialog.Accepted`` — call sites check that first."""
        if self._chose_create_new:
            return None
        return self._candidate


class TransferMatchPickerDialog(QDialog):
    """Multi-candidate picker (ADR-036).

    Lists candidates ranked best-first. The last row is always "Create
    new partner instead" so picking it is one click. Strength chips give
    the user a one-glance signal of which row the matcher thinks is best.
    """

    def __init__(
        self,
        *,
        candidates: list[TransferCandidate],
        source_account: str,
        source_amount: Decimal,
        source_currency: str,
        source_date: str,
        source_payee: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick matching transaction")
        self.setMinimumWidth(680)
        self.setMinimumHeight(420)
        self._candidates = candidates

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        other_acct = candidates[0].account_name if candidates else "the other account"
        intro = QLabel(
            f"Found {len(candidates)} possible match"
            f"{'es' if len(candidates) != 1 else ''} in "
            f"<b>{other_acct}</b>. Pick one to link, or create a new "
            f"partner row instead."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(_source_summary_label(
            source_account=source_account,
            source_amount=source_amount,
            source_currency=source_currency,
            source_date=source_date,
            source_payee=source_payee,
        ))

        # The table: one row per candidate + one trailing "Create new" row.
        # We use a QTableWidget rather than a QTableView+model because the
        # data is small, the rows are visually rich (strength chip in a
        # cell), and the dialog is throwaway each open.
        self._table = QTableWidget(len(candidates) + 1, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["Date", "Payee", "Amount", "Δ days", "Strength"]
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        for r, c in enumerate(candidates):
            self._table.setItem(r, 0, QTableWidgetItem(c.posted_date))
            self._table.setItem(r, 1, QTableWidgetItem(c.payee_name or "(no payee)"))
            amt_item = QTableWidgetItem(
                _fmt_amount(c.amount, c.account_currency)
            )
            amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(r, 2, amt_item)
            days_item = QTableWidgetItem(
                "0" if c.days_apart == 0
                else f"{c.days_apart:+d}"
            )
            days_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(r, 3, days_item)
            # Strength chip lives inside a widget cell so we get colour.
            chip_holder = QWidget()
            chip_layout = QHBoxLayout(chip_holder)
            chip_layout.setContentsMargins(4, 2, 4, 2)
            chip_layout.addWidget(_strength_chip(c.strength))
            chip_layout.addStretch(1)
            self._table.setCellWidget(r, 4, chip_holder)

        # "Create new" sentinel row.
        last = len(candidates)
        create_item = QTableWidgetItem("Create new partner instead")
        f = create_item.font()
        f.setItalic(True)
        create_item.setFont(f)
        create_item.setForeground(QColor(tokens.c("accent")))
        self._table.setItem(last, 0, create_item)
        for col in range(1, 5):
            empty = QTableWidgetItem("")
            self._table.setItem(last, col, empty)
        # Span the create-new row across the table for visual clarity.
        self._table.setSpan(last, 0, 1, 5)

        # Best candidate pre-selected; double-click accepts.
        self._table.selectRow(0)
        self._table.doubleClicked.connect(lambda _ix: self._accept_if_valid())

        layout.addWidget(self._table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        self.accept()

    def result_candidate(self) -> Optional[TransferCandidate]:
        """The selected candidate, or ``None`` when the user picked the
        "Create new partner" sentinel row. Only meaningful after
        ``exec() == QDialog.Accepted``."""
        row = self._table.currentRow()
        if row < 0 or row >= len(self._candidates):
            return None
        return self._candidates[row]


# ── Bulk review (ADR-036 bulk path) ─────────────────────────────────────────


@dataclass(frozen=True)
class BulkRowAnalysis:
    """One source row's matcher result, fed into ``BulkTransferReviewDialog``.

    The dialog shows a per-row picker only when ``candidates`` has more
    than one entry (the ambiguous case); otherwise the default decision
    is unambiguous and the row renders as a single summary line.

    ``dest_currency`` is the destination account's currency.
    ``fx_prefill_amount`` is the pre-computed magnitude (positive,
    Decimal) for the partner side per ADR-035's nearest-rate lookup;
    ``None`` when no rate exists. Both feed the cross-currency
    destination-amount column (ADR-035 amendment 2026-06-07). For
    same-currency rows ``dest_currency == source_currency`` and the
    column stays blank for that row.
    """
    source_txn_id: int
    source_account_id: int
    source_account_name: str
    source_amount: Decimal
    source_currency: str
    source_date: str
    source_payee: str
    candidates: list[TransferCandidate]
    dest_currency: str = ""
    fx_prefill_amount: Optional[Decimal] = None


class BulkTransferReviewDialog(QDialog):
    """Summary + per-row resolution screen for the bulk-edit transfer
    dispatcher (ADR-036).

    Each source row falls into one of three buckets:

    - **Will link** — the matcher found exactly one candidate; the row
      shows a single-line summary "→ {date} · {payee} · {amount}".
    - **Will create new** — no candidates; row shows "→ Create new partner
      in {dest}".
    - **Pick one** — multiple candidates; row shows a combo box of the
      candidates plus a "Create new partner" entry. The top-scoring
      candidate is pre-selected so the dialog has a sensible default and
      Confirm is always enabled.

    Calling ``values()`` after Accepted returns the list of
    ``BulkTransferDecision`` ready for ``Repository.bulk_match_or_create_transfers``.
    """

    def __init__(
        self,
        *,
        analyses: list[BulkRowAnalysis],
        other_account_id: int,
        other_account_name: str,
        category_id: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review transfer matches")
        self.setMinimumWidth(720)
        self.setMinimumHeight(520)

        self._analyses = analyses
        self._other_account_id = other_account_id
        self._other_account_name = other_account_name
        self._category_id = category_id
        # row index → QComboBox (None for non-ambiguous rows)
        self._row_combos: dict[int, Optional[object]] = {}
        # row index → QLineEdit for the destination-amount input (only
        # populated on cross-currency rows; ADR-035 amendment).
        self._row_amount_fields: dict[int, Optional[QLineEdit]] = {}

        # Whether any row crosses currencies — drives the extra
        # "Dest amount" column.
        self._has_cross_currency = any(
            a.dest_currency and a.dest_currency != a.source_currency
            for a in analyses
        )

        # Bucket counts
        will_link = sum(1 for a in analyses if len(a.candidates) == 1)
        will_create = sum(1 for a in analyses if len(a.candidates) == 0)
        ambiguous = sum(1 for a in analyses if len(a.candidates) >= 2)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        summary_html = (
            f"<b>{len(analyses)}</b> transaction"
            f"{'s' if len(analyses) != 1 else ''} to convert to transfers "
            f"with <b>{other_account_name}</b>:"
            f"<br>• <span style='color:{tokens.c('positive')}'>{will_link}</span> match an existing transaction"
            f"<br>• <span style='color:{tokens.c('accent')}'>{will_create}</span> create a new partner"
        )
        if ambiguous:
            summary_html += (
                f"<br>• <span style='color:{tokens.c('caution')}'>{ambiguous}</span> need a choice"
            )
        summary = QLabel(summary_html)
        summary.setTextFormat(Qt.RichText)
        summary.setWordWrap(True)
        outer.addWidget(summary)

        # Table of source rows with per-row decision in the last column.
        # 5 cols normally (Date / Amount / Payee / → / Match to); an
        # extra "Dest amount" column appears when any row crosses
        # currencies (ADR-035 amendment 2026-06-07).
        col_count = 6 if self._has_cross_currency else 5
        self._table = QTableWidget(len(analyses), col_count, self)
        headers = ["Date", "Amount", "Payee", "→", "Match to"]
        if self._has_cross_currency:
            headers.append("Dest amount")
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        if self._has_cross_currency:
            header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        for i, a in enumerate(analyses):
            self._table.setItem(i, 0, QTableWidgetItem(a.source_date))
            amt_item = QTableWidgetItem(
                _fmt_amount(a.source_amount, a.source_currency)
            )
            amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 1, amt_item)
            self._table.setItem(
                i, 2, QTableWidgetItem(a.source_payee or "(no payee)"),
            )
            arrow = QTableWidgetItem("→")
            arrow.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(i, 3, arrow)
            self._populate_decision_cell(i, a)
            if self._has_cross_currency:
                self._populate_dest_amount_cell(i, a)

        self._table.resizeRowsToContents()
        outer.addWidget(self._table, 1)

        # Hint line — only when ambiguity needs the user's attention.
        if ambiguous:
            hint = QLabel(
                "Rows with a picker below are ambiguous — the matcher "
                "found multiple candidates. Defaults are the highest-"
                "scoring option; change them as needed."
            )
            hint.setWordWrap(True)
            tokens.themed(hint, "QLabel { color: {muted}; font-size: 11px; }")
            outer.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _populate_dest_amount_cell(
        self, row: int, a: BulkRowAnalysis,
    ) -> None:
        """Render col 5 — the destination-amount input column. Only
        cross-currency rows get an editable input; same-currency rows
        show a dash for symmetry (no input needed, the repository's
        same-currency early-exit picks the source magnitude). The input
        is pre-filled from ``fx_prefill_amount`` when available."""
        if not a.dest_currency or a.dest_currency == a.source_currency:
            dash = QTableWidgetItem("—")
            dash.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 5, dash)
            self._row_amount_fields[row] = None
            return
        edit = QLineEdit()
        validator = QDoubleValidator(0.0, 1_000_000_000.0, 4, edit)
        validator.setNotation(QDoubleValidator.StandardNotation)
        edit.setValidator(validator)
        edit.setPlaceholderText(a.dest_currency)
        edit.setAlignment(Qt.AlignRight)
        if a.fx_prefill_amount is not None:
            edit.setText(f"{a.fx_prefill_amount:.2f}")
            edit.setToolTip(
                f"Pre-filled from stored {a.source_currency} → "
                f"{a.dest_currency} rate. Edit if your statement shows "
                f"a different amount."
            )
        else:
            edit.setToolTip(
                f"No stored {a.source_currency} → {a.dest_currency} "
                f"rate. Enter the {a.dest_currency} amount that hit "
                f"the destination account."
            )
        self._table.setCellWidget(row, 5, edit)
        self._row_amount_fields[row] = edit

    def _populate_decision_cell(self, row: int, a: BulkRowAnalysis) -> None:
        if len(a.candidates) == 0:
            label = QLabel(f"Create new partner in {self._other_account_name}")
            tokens.themed(label, "QLabel { color: {accent}; padding: 2px 6px; }")
            self._table.setCellWidget(row, 4, label)
            self._row_combos[row] = None
            return
        if len(a.candidates) == 1:
            c = a.candidates[0]
            label = QLabel(
                f"{c.posted_date} · {c.payee_name or '(no payee)'} · "
                f"{_fmt_amount(c.amount, c.account_currency)}"
            )
            tokens.themed(label, "QLabel { color: {positive}; padding: 2px 6px; }")
            self._table.setCellWidget(row, 4, label)
            self._row_combos[row] = None
            return
        # Ambiguous → combo
        from PySide6.QtWidgets import QComboBox  # local import to keep top tidy
        combo = QComboBox()
        for c in a.candidates:
            label = (
                f"{c.posted_date} · {c.payee_name or '(no payee)'} · "
                f"{_fmt_amount(c.amount, c.account_currency)} "
                f"[{c.strength}]"
            )
            combo.addItem(label, userData=c.txn_id)
        combo.addItem(
            f"Create new partner in {self._other_account_name}",
            userData=None,
        )
        combo.setCurrentIndex(0)  # top-scoring candidate
        self._table.setCellWidget(row, 4, combo)
        self._row_combos[row] = combo

    def _row_to_amount(self, row: int, a: BulkRowAnalysis) -> Optional[Decimal]:
        """Read the cross-currency destination-amount input for a row,
        or ``None`` if the row is same-currency / has no input. Falls
        back to the FX pre-fill if the user blanked the field; surfaces
        the user's typed value otherwise. Bad text → falls back to
        pre-fill too (the validator should have caught it, but be
        defensive at submit time)."""
        if not a.dest_currency or a.dest_currency == a.source_currency:
            return None
        field = self._row_amount_fields.get(row)
        if field is None:
            return a.fx_prefill_amount
        text = field.text().strip()
        if not text:
            return a.fx_prefill_amount
        try:
            value = Decimal(text)
        except InvalidOperation:
            return a.fx_prefill_amount
        if value <= 0:
            return a.fx_prefill_amount
        return value

    def values(self) -> list[BulkTransferDecision]:
        """Build the decision plan in input order. For non-ambiguous rows
        the default applies; for ambiguous rows the combo's current
        selection feeds the LinkExisting / CreateNew choice. Cross-
        currency CreateNew rows additionally carry the user-entered (or
        FX-pre-filled) ``to_amount`` per ADR-035 amendment 2026-06-07."""
        decisions: list[BulkTransferDecision] = []
        for i, a in enumerate(self._analyses):
            combo = self._row_combos.get(i)
            if combo is None:
                if len(a.candidates) == 1:
                    c = a.candidates[0]
                    decisions.append(LinkExisting(
                        source_txn_id=a.source_txn_id,
                        candidate_txn_id=c.txn_id,
                        category_id=self._category_id,
                    ))
                else:
                    decisions.append(CreateNew(
                        source_txn_id=a.source_txn_id,
                        other_account_id=self._other_account_id,
                        category_id=self._category_id,
                        to_amount=self._row_to_amount(i, a),
                    ))
            else:
                chosen_id = combo.currentData()
                if chosen_id is None:
                    decisions.append(CreateNew(
                        source_txn_id=a.source_txn_id,
                        other_account_id=self._other_account_id,
                        category_id=self._category_id,
                        to_amount=self._row_to_amount(i, a),
                    ))
                else:
                    decisions.append(LinkExisting(
                        source_txn_id=a.source_txn_id,
                        candidate_txn_id=int(chosen_id),
                        category_id=self._category_id,
                    ))
        return decisions
