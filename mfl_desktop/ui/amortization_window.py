"""Amortization schedule window for a loan account (ADR-095).

Opened from a loan account (sidebar context menu / Account menu). Shows the
loan's forward amortization — a summary (current balance, monthly payment,
payoff date, total remaining interest), a **balance-decline chart**, and the
full **schedule table** (payment · interest · principal · balance per month).
A **Record payment** button posts the next split through
``Repository.post_loan_payment``; **Edit loan…** reopens the loan dialog.

Single instance per loan account, keyed in the register window. paintEvent
chart per the ADR-026 chart-engine preference.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui import tokens
import mfl_desktop.ui.chart_helpers as _ch
from mfl_desktop.ui.chart_helpers import fmt_currency, nice_ticks
from mfl_desktop.ui.ui_fonts import set_pt

_CCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


class _BalanceChart(QWidget):
    """Declining-balance curve over the life of the loan (paintEvent)."""

    _ML, _MR, _MT, _MB = 64, 14, 16, 24

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list = []
        self.setMinimumHeight(200)
        self.setMaximumHeight(260)

    def set_rows(self, rows) -> None:
        self._rows = rows or []
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(_ch.chart_surface()))
        rows = self._rows
        if len(rows) < 2:
            p.setPen(QPen(QColor(_ch.chart_axis_ink())))
            p.drawText(self.rect(), Qt.AlignCenter, "No schedule to chart")
            p.end()
            return
        w, h = self.width(), self.height()
        rect = QRectF(self._ML, self._MT, max(1, w - self._ML - self._MR),
                      max(1, h - self._MT - self._MB))
        ymax, ystep = nice_ticks(max(float(r.balance) for r in rows) * 1.08, 4)
        n = len(rows)

        def x(i): return rect.left() + (i / max(1, n - 1)) * rect.width()
        def y(v): return rect.bottom() - (v / ymax) * rect.height() if ymax else rect.bottom()

        # gridlines + y labels
        p.setFont(self._small(p))
        steps = int(round(ymax / ystep)) if ystep > 0 else 0
        fm = QFontMetrics(p.font())
        for k in range(steps + 1):
            v = k * ystep
            yy = y(v)
            p.setPen(QPen(QColor(_ch.chart_grid())))
            p.drawLine(int(rect.left()), int(yy), int(rect.right()), int(yy))
            p.setPen(QPen(QColor(_ch.chart_axis_ink())))
            lbl = fmt_currency(v)
            p.drawText(int(rect.left() - fm.horizontalAdvance(lbl) - 8),
                       int(yy + fm.ascent() / 2 - 2), lbl)

        # filled balance area + line
        pts = [QPointF(x(i), y(float(r.balance))) for i, r in enumerate(rows)]
        poly = QPolygonF(pts)
        poly.append(QPointF(rect.right(), rect.bottom()))
        poly.append(QPointF(rect.left(), rect.bottom()))
        fill = QColor("#a855f7"); fill.setAlpha(40)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(fill)); p.drawPolygon(poly)
        pen = QPen(QColor("#a855f7")); pen.setWidth(2)
        p.setPen(pen)
        for i in range(1, len(pts)):
            p.drawLine(pts[i - 1], pts[i])

        # x labels (first / mid / last year)
        p.setPen(QPen(QColor(_ch.chart_axis_ink())))
        for i in (0, n // 2, n - 1):
            yr = rows[i].date[:4]
            xx = x(i)
            p.drawText(int(xx - fm.horizontalAdvance(yr) / 2),
                       int(rect.bottom() + fm.ascent() + 4), yr)
        # baseline
        p.setPen(QPen(QColor(_ch.chart_axis_ink())))
        p.drawLine(int(rect.left()), int(rect.bottom()),
                   int(rect.right()), int(rect.bottom()))
        p.end()

    def _small(self, p):
        f = QFont(p.font()); set_pt(f, 8); return f


class AmortizationWindow(QMainWindow):
    """Per-loan amortization schedule + chart + record-payment."""

    def __init__(self, repo: Repository, account_id: int, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._account_id = account_id
        self.setMinimumSize(820, 600)

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        f = QFont(self._summary.font()); f.setBold(True); self._summary.setFont(f)
        v.addWidget(self._summary)

        self._warn = QLabel("")
        self._warn.setWordWrap(True)
        tokens.themed(self._warn, "QLabel { color: {warning}; }")
        v.addWidget(self._warn)

        self._chart = _BalanceChart()
        v.addWidget(self._chart)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["#", "Date", "Payment", "Interest", "Principal", "Balance"]
        )
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for c in (2, 3, 4, 5):
            hdr.setSectionResizeMode(c, QHeaderView.Stretch)
        v.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        self._record_btn = QPushButton("Record payment")
        self._record_btn.clicked.connect(self._on_record)
        btn_row.addWidget(self._record_btn)
        edit_btn = QPushButton("Edit loan…")
        edit_btn.clicked.connect(self._on_edit)
        btn_row.addWidget(edit_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self._reload()

    # ── data ──

    def _reload(self) -> None:
        loan = self._repo.get_loan(self._account_id)
        acct = next(
            (a for a in self._repo.list_accounts(include_closed=True)
             if a.id == self._account_id), None,
        )
        if loan is None or acct is None:
            self.setWindowTitle("Amortization")
            return
        self.setWindowTitle(f"Amortization — {acct.name}")
        sym = _CCY_SYMBOLS.get(acct.currency, acct.currency + " ")
        balance = self._repo.loan_current_balance(self._account_id)
        payment = self._repo.effective_payment(loan)
        sched = self._repo.loan_schedule(self._account_id)
        self._chart.set_rows(sched.rows)

        if sched.negative_amortization:
            self._warn.setText(
                f"⚠ The payment of {sym}{payment:,.2f} doesn't cover the monthly "
                f"interest — the balance won't reduce. Increase the payment in "
                f"Edit loan…"
            )
        else:
            self._warn.setText("")

        self._summary.setText(
            f"Balance owed {sym}{balance:,.2f}   ·   payment {sym}{payment:,.2f}/mo"
            + (f" (+{sym}{loan.extra_payment:,.2f} extra)"
               if loan.extra_payment else "")
            + (f"   ·   paid off {sched.payoff_date}   ·   "
               f"remaining interest {sym}{sched.total_interest:,.2f}"
               if sched.payoff_date else "")
        )
        self._record_btn.setEnabled(
            balance > 0 and loan.payment_account_id is not None
            and not sched.negative_amortization
        )

        rows = sched.rows
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._cell(i, 0, str(r.number), Qt.AlignCenter)
            self._cell(i, 1, r.date, Qt.AlignCenter)
            self._cell(i, 2, f"{sym}{r.payment:,.2f}", Qt.AlignRight | Qt.AlignVCenter)
            self._cell(i, 3, f"{sym}{r.interest:,.2f}", Qt.AlignRight | Qt.AlignVCenter)
            self._cell(i, 4, f"{sym}{r.principal:,.2f}", Qt.AlignRight | Qt.AlignVCenter)
            self._cell(i, 5, f"{sym}{r.balance:,.2f}", Qt.AlignRight | Qt.AlignVCenter)

    def _cell(self, r, c, text, align) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(align)
        self._table.setItem(r, c, item)

    # ── actions ──

    def _on_record(self) -> None:
        loan = self._repo.get_loan(self._account_id)
        if loan is None:
            return
        if QMessageBox.question(
            self, "Record payment",
            "Record this month's loan payment? It will post the principal and "
            "interest split for the current balance.",
        ) != QMessageBox.Yes:
            return
        try:
            self._repo.post_loan_payment(
                account_id=self._account_id,
                posted_date=__import__("datetime").date.today().isoformat(),
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Record payment", str(e))
            return
        self._reload()

    def _on_edit(self) -> None:
        from mfl_desktop.ui.loan_dialog import LoanDialog
        dlg = LoanDialog(self._repo, account_id=self._account_id, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._reload()
