"""Merge securities dialog (ADR-052).

Two records can describe the same instrument — ``security.name`` is unique and
the importer keys on it, so a fund that arrives under two spellings (e.g.
"TIAA CREF CORE IMPACT BD INST" and "Nuveen Core Impact Bond R6", both ticker
TSBIX) becomes two records that split the position. This dialog collapses them:
all transactions and stored prices move to the record you keep, and the other
is deleted.

Opened from the Stock Record's "Merge…" button with the security in view. The
user picks the other record (same-ticker matches surfaced first), sees a
side-by-side comparison, and chooses which record survives — defaulting to the
one with the better data (a real ticker, then more price history, then more
transactions). The repoint + delete is atomic in ``Repository.merge_securities``;
price collisions on a shared date keep the higher-precedence source
(manual > tiingo > transaction, ADR-047).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository, SecurityRow

_SYMBOL = "$"


def _fmt_price(value) -> str:
    if value is None:
        return "—"
    s = f"{_SYMBOL}{float(value):,.4f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


@dataclass(frozen=True)
class _SecStats:
    """Cheap per-security facts used for the comparison and the best-data
    default. ``real_prices`` counts manual/tiingo points (the ones that out-rank
    a trade-derived price) so a record's price provenance is visible."""
    txns: int
    prices: int
    real_prices: int
    latest_label: str
    has_symbol: bool

    @property
    def score(self) -> tuple:
        """Higher sorts first as the default survivor: prefer a real ticker,
        then more real (manual/tiingo) prices, then more total prices, then
        more transactions."""
        return (self.has_symbol, self.real_prices, self.prices, self.txns)


class MergeSecuritiesDialog(QDialog):
    """Pick a second record, choose the survivor, merge. On accept the merge
    has already been applied; the caller reads ``survivor_id`` / ``absorbed_id``
    / ``moved_count`` to decide whether to reload or close."""

    def __init__(
        self, repo: Repository, current: SecurityRow,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._current = current
        self.survivor_id: Optional[int] = None
        self.absorbed_id: Optional[int] = None
        self.moved_count: int = 0

        self.setWindowTitle("Merge securities")
        self.setMinimumWidth(560)

        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        intro = QLabel(
            "Combine two records for the same instrument. Every transaction "
            "and stored price moves to the record you keep; the other record "
            "is deleted. This can't be undone."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #475569; }")
        outer.addWidget(intro)

        # ── pick the other record ──
        form = QFormLayout()
        self._other_combo = QComboBox()
        self._other_combo.setEditable(True)
        self._other_combo.setInsertPolicy(QComboBox.NoInsert)
        self._other_combo.completer().setCompletionMode(
            self._other_combo.completer().CompletionMode.PopupCompletion
        )
        self._other_combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self._populate_others()
        self._other_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_comparison()
        )
        form.addRow(f"Merge {current.name!r} with:", self._other_combo)
        outer.addLayout(form)

        # ── side-by-side comparison ──
        cmp_box = QGroupBox("Compare")
        self._grid = QGridLayout(cmp_box)
        self._grid.setColumnStretch(1, 1)
        self._grid.setColumnStretch(2, 1)
        outer.addWidget(cmp_box)

        # ── survivor choice ──
        keep_box = QGroupBox("Keep which record?")
        keep_v = QVBoxLayout(keep_box)
        self._keep_group = QButtonGroup(self)
        self._keep_current = QRadioButton()
        self._keep_other = QRadioButton()
        self._keep_group.addButton(self._keep_current, 0)
        self._keep_group.addButton(self._keep_other, 1)
        self._keep_group.idToggled.connect(lambda _i, _on: self._refresh_confirm())
        keep_v.addWidget(self._keep_current)
        keep_v.addWidget(self._keep_other)
        outer.addWidget(keep_box)

        self._confirm = QLabel("")
        self._confirm.setWordWrap(True)
        self._confirm.setStyleSheet("QLabel { color: #b45309; }")
        outer.addWidget(self._confirm)

        # ── buttons ──
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_btn = buttons.button(QDialogButtonBox.Ok)
        self._ok_btn.setText("Merge")
        self._ok_btn.setDefault(True)
        buttons.accepted.connect(self._on_merge)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._refresh_comparison()

    # ── construction helpers ──

    def _populate_others(self) -> None:
        """List every other security, with any record that shares this one's
        ticker surfaced to the top (the likely duplicates)."""
        cur_symbol = (self._current.symbol or "").strip().casefold()
        others = [s for s in self._repo.list_securities() if s.id != self._current.id]

        def sort_key(s: SecurityRow):
            same_ticker = (
                bool(cur_symbol)
                and (s.symbol or "").strip().casefold() == cur_symbol
            )
            return (0 if same_ticker else 1, s.name.casefold())

        others.sort(key=sort_key)
        for s in others:
            label = f"{s.symbol} — {s.name}" if (s.symbol or "").strip() else s.name
            self._other_combo.addItem(label, s.id)
        # Pre-select the first same-ticker match when there is one.
        if others and cur_symbol and (others[0].symbol or "").strip().casefold() == cur_symbol:
            self._other_combo.setCurrentIndex(0)

    def _selected_other(self) -> Optional[SecurityRow]:
        sid = self._other_combo.currentData()
        if sid is None:
            return None
        for s in self._repo.list_securities():
            if s.id == sid:
                return s
        return None

    def _stats(self, security: SecurityRow) -> _SecStats:
        txns = self._repo.list_transactions_for_security(security.id)
        series = self._repo.price_series(security.id)
        real = sum(1 for p in series if p.source in ("manual", "tiingo"))
        latest = self._repo.latest_price_for_security(security.id)
        latest_label = (
            f"{_fmt_price(latest.price)} ({latest.price_date}, {latest.source})"
            if latest is not None else "—"
        )
        return _SecStats(
            txns=len(txns), prices=len(series), real_prices=real,
            latest_label=latest_label,
            has_symbol=bool((security.symbol or "").strip()),
        )

    # ── refresh ──

    def _refresh_comparison(self) -> None:
        other = self._selected_other()
        # Clear the grid.
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if other is None:
            self._ok_btn.setEnabled(False)
            self._keep_current.setText(self._current.name)
            self._keep_other.setText("(pick a record above)")
            self._keep_other.setEnabled(False)
            self._refresh_confirm()
            return

        self._ok_btn.setEnabled(True)
        self._keep_other.setEnabled(True)
        cur_stats = self._stats(self._current)
        oth_stats = self._stats(other)

        def cap(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setStyleSheet("QLabel { color: #64748B; }")
            return lab

        def head(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setStyleSheet("QLabel { font-weight: 600; }")
            lab.setWordWrap(True)
            return lab

        self._grid.addWidget(QLabel(""), 0, 0)
        self._grid.addWidget(head(self._current.name), 0, 1)
        self._grid.addWidget(head(other.name), 0, 2)
        rows = [
            ("Ticker", self._current.symbol or "—", other.symbol or "—"),
            ("Transactions", str(cur_stats.txns), str(oth_stats.txns)),
            ("Stored prices", str(cur_stats.prices), str(oth_stats.prices)),
            ("Latest price", cur_stats.latest_label, oth_stats.latest_label),
        ]
        for r, (label, cur_v, oth_v) in enumerate(rows, start=1):
            self._grid.addWidget(cap(label), r, 0)
            cv = QLabel(cur_v)
            cv.setWordWrap(True)
            ov = QLabel(oth_v)
            ov.setWordWrap(True)
            self._grid.addWidget(cv, r, 1)
            self._grid.addWidget(ov, r, 2)

        # Default survivor = the record with the better data.
        self._keep_current.setText(f"Keep {self._current.name}")
        self._keep_other.setText(f"Keep {other.name}")
        keep_current_default = cur_stats.score >= oth_stats.score
        self._keep_group.blockSignals(True)
        self._keep_current.setChecked(keep_current_default)
        self._keep_other.setChecked(not keep_current_default)
        self._keep_group.blockSignals(False)
        self._refresh_confirm()

    def _refresh_confirm(self) -> None:
        other = self._selected_other()
        if other is None:
            self._confirm.setText("")
            return
        if self._keep_current.isChecked():
            survivor, absorbed = self._current, other
        else:
            survivor, absorbed = other, self._current
        moved = self._repo.list_transactions_for_security(absorbed.id)
        prices = self._repo.price_series(absorbed.id)
        self._confirm.setText(
            f"{len(moved)} transaction{'s' if len(moved) != 1 else ''} and "
            f"{len(prices)} price{'s' if len(prices) != 1 else ''} will move to "
            f"{survivor.name!r}. {absorbed.name!r} will be deleted. "
            f"This can't be undone."
        )

    # ── action ──

    def _on_merge(self) -> None:
        other = self._selected_other()
        if other is None:
            QMessageBox.information(self, "Merge securities", "Pick a record to merge with.")
            return
        if self._keep_current.isChecked():
            survivor, absorbed = self._current, other
        else:
            survivor, absorbed = other, self._current
        if QMessageBox.question(
            self, "Merge securities",
            f"Merge {absorbed.name!r} into {survivor.name!r}?\n\n"
            f"All transactions and prices move to {survivor.name!r} and "
            f"{absorbed.name!r} is deleted. This can't be undone.",
        ) != QMessageBox.Yes:
            return
        try:
            moved = self._repo.merge_securities([absorbed.id], survivor.id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Merge securities", f"Could not merge:\n\n{e}")
            return
        self.survivor_id = survivor.id
        self.absorbed_id = absorbed.id
        self.moved_count = moved
        self.accept()
