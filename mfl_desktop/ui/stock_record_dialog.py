"""Stock Record screen — per-security detail view (ADR-047).

A single security's home: its identity (name / ticker / type, all editable),
its price history (view / add / edit / delete + a price-over-time mini chart),
its activity (every Buy/Sell/Div across all accounts), and its current
position (shares / cost basis / market value / gains).

Reached by double-clicking a row in Manage ▸ Securities (or its "Open stock
record" button). Two jobs it unlocks beyond viewing:

  * **Set a missing ticker.** Many holdings imported from QIF carry no symbol
    (e.g. Tesla came in as "Tesla Inc" with a blank ticker). Typing the ticker
    here + "Fetch from Tiingo" re-enables automatic prices for that holding.
  * **Hand-price the untickered.** For holdings with no ticker at all, the
    price-history table is the manual fallback (on top of the prices
    auto-seeded from their own trades, ADR-047).

Modal QDialog — opened on top of the (modal) Securities dialog. Position math
reuses ``holdings.compute_holdings_view`` over just this security's
transactions, so FIFO cost basis / realized gain stay consistent with the
holdings table and the returns report.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository, SecurityRow
from mfl_desktop.holdings import compute_holdings_view
from mfl_desktop.prices import PriceFetchError, backfill_security_history_into
from mfl_desktop.ui.price_history_chart import PriceHistoryChart

# Display currency for quotes / cash on this screen. The owner's portfolio is
# single-currency USD and prices are USD quotes; a per-account currency lookup
# is a later refinement if a multi-currency brokerage appears.
_SYMBOL = "$"


def _fmt_money(value, decimals: int = 2) -> str:
    if value is None:
        return "—"
    v = float(value)
    sign = "-" if v < 0 else ""
    return f"{sign}{_SYMBOL}{abs(v):,.{decimals}f}"


def _fmt_num(value, decimals: int = 4) -> str:
    if value is None:
        return "—"
    s = f"{float(value):,.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


class StockRecordDialog(QDialog):
    """Per-security detail + price management."""

    def __init__(
        self, repo: Repository, security: SecurityRow,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._sid = security.id
        self._security = security
        self.setWindowTitle(f"Stock record — {security.name}")
        self.setMinimumSize(900, 640)

        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        outer.addWidget(self._build_header())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

        self._reload_all()

    # ── construction ──

    def _build_header(self) -> QWidget:
        box = QGroupBox("Security details")
        form = QFormLayout(box)
        self._name_edit = QLineEdit(self._security.name)
        self._symbol_edit = QLineEdit(self._security.symbol or "")
        self._symbol_edit.setPlaceholderText("ticker, e.g. TSLA (blank = untickered)")
        self._symbol_edit.setMaximumWidth(220)
        self._type_edit = QLineEdit(self._security.type or "")
        self._type_edit.setMaximumWidth(220)
        form.addRow("Name:", self._name_edit)
        form.addRow("Ticker symbol:", self._symbol_edit)
        form.addRow("Type:", self._type_edit)

        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("Save details")
        self._save_btn.clicked.connect(self._on_save_details)
        btn_row.addWidget(self._save_btn)
        self._fetch_btn = QPushButton("Fetch from Tiingo")
        self._fetch_btn.setToolTip(
            "Save the ticker and download this security's full price history "
            "from Tiingo (needs a ticker + an API key in Manage ▸ Securities)."
        )
        self._fetch_btn.clicked.connect(self._on_fetch_from_tiingo)
        btn_row.addWidget(self._fetch_btn)
        btn_row.addStretch(1)
        self._header_status = QLabel("")
        self._header_status.setStyleSheet("QLabel { color: #475569; }")
        btn_row.addWidget(self._header_status)
        form.addRow("", self._wrap(btn_row))
        return box

    def _build_left_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        chart_box = QGroupBox("Price history")
        cv = QVBoxLayout(chart_box)
        self._chart = PriceHistoryChart()
        cv.addWidget(self._chart)
        v.addWidget(chart_box, 1)

        prices_box = QGroupBox("Stored prices")
        pv = QVBoxLayout(prices_box)
        self._prices_table = QTableWidget(0, 3)
        self._prices_table.setHorizontalHeaderLabels(["Date", "Price", "Source"])
        self._prices_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._prices_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._prices_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._prices_table.verticalHeader().setVisible(False)
        ph = self._prices_table.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.Stretch)
        ph.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ph.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        pv.addWidget(self._prices_table)

        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("Add / edit:"))
        self._add_date = QDateEdit()
        self._add_date.setCalendarPopup(True)
        self._add_date.setDisplayFormat("yyyy-MM-dd")
        self._add_date.setDate(date.today())
        add_row.addWidget(self._add_date)
        self._add_price = QLineEdit()
        self._add_price.setPlaceholderText("price")
        self._add_price.setMaximumWidth(100)
        add_row.addWidget(self._add_price)
        add_btn = QPushButton("Save price")
        add_btn.clicked.connect(self._on_add_price)
        add_row.addWidget(add_btn)
        del_btn = QPushButton("Delete selected")
        del_btn.clicked.connect(self._on_delete_price)
        add_row.addWidget(del_btn)
        add_row.addStretch(1)
        pv.addLayout(add_row)

        manual_note = QLabel(
            "Manually entered prices always win over downloaded or "
            "trade-derived ones."
        )
        manual_note.setWordWrap(True)
        manual_note.setStyleSheet("QLabel { color: #64748B; font-size: 11px; }")
        pv.addWidget(manual_note)
        v.addWidget(prices_box, 1)
        return pane

    def _build_right_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # Position summary card.
        pos_box = QGroupBox("Current position")
        grid = QGridLayout(pos_box)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self._pos_labels: dict[str, QLabel] = {}
        fields = [
            ("Shares", "shares"), ("Avg cost", "avg_cost"),
            ("Cost basis", "cost_basis"), ("Last price", "last_price"),
            ("Market value", "market_value"), ("Unrealised", "unrealized"),
            ("Realised (lifetime)", "realized"), ("", "spacer"),
        ]
        for i, (label, key) in enumerate(fields):
            if key == "spacer":
                continue
            r, c = divmod(i, 2)
            cap = QLabel(label + ":")
            cap.setStyleSheet("QLabel { color: #64748B; }")
            val = QLabel("—")
            val.setStyleSheet("QLabel { font-weight: 600; }")
            grid.addWidget(cap, r, c * 2)
            grid.addWidget(val, r, c * 2 + 1)
            self._pos_labels[key] = val
        self._pos_note = QLabel("")
        self._pos_note.setWordWrap(True)
        self._pos_note.setStyleSheet("QLabel { color: #b45309; font-size: 11px; }")
        grid.addWidget(self._pos_note, (len(fields) + 1) // 2, 0, 1, 4)
        v.addWidget(pos_box)

        # Transactions table.
        txn_box = QGroupBox("Transactions")
        tv = QVBoxLayout(txn_box)
        self._txn_table = QTableWidget(0, 6)
        self._txn_table.setHorizontalHeaderLabels(
            ["Date", "Account", "Action", "Qty", "Price", "Amount"]
        )
        self._txn_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._txn_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._txn_table.verticalHeader().setVisible(False)
        th = self._txn_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(1, QHeaderView.Stretch)
        th.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        tv.addWidget(self._txn_table)
        v.addWidget(txn_box, 1)
        return pane

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ── data refresh ──

    def _reload_all(self) -> None:
        self._reload_security()
        self._reload_prices()
        self._reload_position()
        self._reload_transactions()
        self._update_fetch_enabled()

    def _reload_security(self) -> None:
        # Re-read the master so the title / fields reflect a saved edit.
        for s in self._repo.list_securities():
            if s.id == self._sid:
                self._security = s
                break
        self.setWindowTitle(f"Stock record — {self._security.name}")

    def _reload_prices(self) -> None:
        series = self._repo.price_series(self._sid)
        self._prices_table.setRowCount(len(series))
        for i, pr in enumerate(series):
            date_item = QTableWidgetItem(pr.price_date)
            self._prices_table.setItem(i, 0, date_item)
            price_item = QTableWidgetItem(_fmt_num(pr.price, 4))
            price_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._prices_table.setItem(i, 1, price_item)
            self._prices_table.setItem(i, 2, QTableWidgetItem(pr.source))
        # Chart wants ascending (date, price); price_series is already ascending.
        chart_points = [(pr.price_date, pr.price) for pr in series]
        if chart_points:
            self._chart.render(chart_points, _SYMBOL)
        else:
            self._chart.show_empty("No price history yet.")

    def _reload_position(self) -> None:
        txns = self._repo.list_transactions_for_security(self._sid)
        pr = self._repo.latest_price_for_security(self._sid)
        latest = {self._sid: (pr.price, pr.price_date)} if pr is not None else {}
        view = compute_holdings_view(txns, Decimal("0"), latest)
        holding = next(
            (h for h in view.holdings if h.security_id == self._sid), None,
        )
        if holding is not None:
            self._pos_labels["shares"].setText(_fmt_num(holding.shares))
            self._pos_labels["avg_cost"].setText(_fmt_money(holding.avg_unit_cost, 4))
            self._pos_labels["cost_basis"].setText(_fmt_money(holding.cost_basis))
            lp = (
                f"{_fmt_money(holding.last_price, 4)}"
                + (f" ({holding.last_price_date})" if holding.last_price_date else "")
                if holding.last_price is not None else "—"
            )
            self._pos_labels["last_price"].setText(lp)
            self._pos_labels["market_value"].setText(_fmt_money(holding.market_value))
            if holding.unrealized_gain is not None:
                pct = (f" ({holding.unrealized_pct:+.1f}%)"
                       if holding.unrealized_pct is not None else "")
                self._pos_labels["unrealized"].setText(
                    _fmt_money(holding.unrealized_gain) + pct
                )
            else:
                self._pos_labels["unrealized"].setText("—")
            self._pos_labels["realized"].setText(_fmt_money(holding.realized_gain))
            self._pos_note.setText(
                "Cost basis is approximate — some shares were transferred in "
                "without a known purchase price." if holding.basis_incomplete else ""
            )
        else:
            # No open position (never held, or fully exited).
            for key in ("shares", "avg_cost", "cost_basis", "last_price",
                        "market_value", "unrealized"):
                self._pos_labels[key].setText("—")
            self._pos_labels["shares"].setText("0")
            self._pos_labels["realized"].setText(
                _fmt_money(view.total_realized_gain)
            )
            self._pos_note.setText(
                "No current position — fully exited. The figure above is the "
                "lifetime realised gain from this security."
                if txns else "No transactions for this security."
            )

    def _reload_transactions(self) -> None:
        txns = self._repo.list_transactions_for_security(self._sid)
        self._txn_table.setRowCount(len(txns))
        for i, t in enumerate(txns):
            self._txn_table.setItem(i, 0, QTableWidgetItem(t.posted_date))
            self._txn_table.setItem(i, 1, QTableWidgetItem(t.account_name))
            self._txn_table.setItem(i, 2, QTableWidgetItem(t.action or ""))
            qty = QTableWidgetItem(_fmt_num(t.quantity))
            qty.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._txn_table.setItem(i, 3, qty)
            price = QTableWidgetItem(_fmt_num(t.price, 4))
            price.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._txn_table.setItem(i, 4, price)
            amt = QTableWidgetItem(_fmt_money(t.amount))
            amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._txn_table.setItem(i, 5, amt)

    def _update_fetch_enabled(self) -> None:
        self._fetch_btn.setEnabled(bool(self._symbol_edit.text().strip()))

    # ── handlers ──

    def _on_save_details(self) -> None:
        try:
            self._repo.update_security(
                self._sid,
                name=self._name_edit.text(),
                symbol=self._symbol_edit.text(),
                type_=self._type_edit.text(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Save details", str(e))
            return
        self._header_status.setText("Saved")
        self._reload_security()
        self._update_fetch_enabled()

    def _on_fetch_from_tiingo(self) -> None:
        symbol = self._symbol_edit.text().strip()
        if not symbol:
            QMessageBox.warning(
                self, "Fetch from Tiingo",
                "Enter a ticker symbol first (e.g. TSLA).",
            )
            return
        # Persist the symbol so the fetch + future auto-refresh use it.
        try:
            self._repo.update_security(self._sid, symbol=symbol)
        except ValueError as e:
            QMessageBox.warning(self, "Fetch from Tiingo", str(e))
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            result = backfill_security_history_into(
                self._repo, security_id=self._sid, symbol=symbol,
            )
        except PriceFetchError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Fetch from Tiingo", str(e))
            return
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Fetch from Tiingo", f"Could not fetch prices:\n\n{e}",
            )
            return
        QApplication.restoreOverrideCursor()
        if result.errors:
            QMessageBox.warning(
                self, "Fetch from Tiingo",
                "Tiingo returned no history for this ticker:\n\n"
                + "\n".join(result.errors),
            )
        else:
            self._header_status.setText(
                f"Fetched {result.new_prices_count:,} prices"
            )
        self._reload_security()
        self._reload_prices()
        self._reload_position()

    def _on_add_price(self) -> None:
        text = self._add_price.text().strip().replace(",", "").lstrip("$£€")
        on_date = self._add_date.date().toString("yyyy-MM-dd")
        try:
            price = float(text)
        except ValueError:
            QMessageBox.warning(
                self, "Save price", f"{text!r} doesn't look like a number.",
            )
            return
        if price < 0:
            QMessageBox.warning(self, "Save price", "Price can't be negative.")
            return
        try:
            self._repo.upsert_security_price(
                security_id=self._sid, price_date=on_date,
                price=price, source="manual",
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Save price", f"Could not save:\n\n{e}")
            return
        self._add_price.clear()
        self._reload_prices()
        self._reload_position()

    def _on_delete_price(self) -> None:
        row = self._prices_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Delete price", "Select a price row to delete.",
            )
            return
        date_item = self._prices_table.item(row, 0)
        if date_item is None:
            return
        price_date = date_item.text()
        if QMessageBox.question(
            self, "Delete price",
            f"Delete the stored price for {price_date}?",
        ) != QMessageBox.Yes:
            return
        self._repo.delete_security_price(self._sid, price_date)
        self._reload_prices()
        self._reload_position()
