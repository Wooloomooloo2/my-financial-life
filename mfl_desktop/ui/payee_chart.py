"""Payee report ranked-bar chart (ADR-066 / Arc E, E2).

Horizontal bars, longest at the top — "who I pay the most". Each bar is a
canonical payee's total spend over the period. Same hand-rolled paintEvent
+ ``chart_helpers`` recipe as the other report charts (ADR-026 — flat look,
soft value labels, hover tooltip). No pies (ADR-018).

Colour does not encode rank (a single accent hue), so the bars stay honest
— length alone carries the comparison. Bars are **clickable**: a press
emits :attr:`payee_clicked` so the window can open that payee's
transactions. The window pairs this with a sortable table for the precise
figures.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from mfl_desktop.reports.payee_report import PayeeSpendRow
from mfl_desktop.ui.chart_helpers import fmt_currency

_COLOR_BAR     = "#2563eb"   # blue-600 — app accent
_COLOR_TRACK   = "#f1f5f9"   # slate-100 — faint full-width bar track
_COLOR_NAME    = "#334155"   # slate-700 — payee name labels
_COLOR_VALUE   = "#475569"   # slate-600 — value labels
_COLOR_EMPTY   = "#6b7280"


class PayeeChart(QWidget):
    """Stateless widget — call :meth:`render` to draw, :meth:`show_empty`
    for the no-data state. The window does the SQL roll-up + FX via the
    Repository and the pure ``payee_report`` module.

    Emits :attr:`payee_clicked` ``(payee_id, name)`` when a bar is clicked
    (``payee_id`` is ``None`` for the no-payee group)."""

    payee_clicked = Signal(object, str)

    _MARGIN_TOP    = 12
    _MARGIN_BOTTOM = 12
    _MARGIN_LEFT   = 12
    _MARGIN_RIGHT  = 12
    _NAME_BAND     = 168       # left column for payee names
    _VALUE_BAND    = 96        # right column for value labels
    _ROW_MAX_H     = 34        # cap row height so few payees don't look odd
    _BAR_FILL      = 0.62      # bar height as a fraction of the row slot
    _BAR_RADIUS    = 3.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(220)

        self._rows: list[PayeeSpendRow] = []
        self._symbol: str = "£"
        self._empty_message: Optional[str] = None
        # (rect, row_index) for hover hit-testing.
        self._hitmap: list[tuple[QRectF, int]] = []

    # ── public interface ──

    def render(
        self, *, rows: list[PayeeSpendRow], symbol: str = "£",
    ) -> None:
        self._rows = rows
        self._symbol = symbol or "£"
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._rows = []
        self._empty_message = message
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        if self._empty_message is not None:
            self._paint_empty(painter, self._empty_message)
            painter.end()
            return
        if not self._rows:
            self._paint_empty(painter, "No spending for this period")
            painter.end()
            return

        self._paint_bars(painter)
        painter.end()

    def _paint_bars(self, painter: QPainter) -> None:
        self._hitmap.clear()
        n = len(self._rows)
        w = self.width()
        h = self.height()

        area_top = self._MARGIN_TOP
        area_h = max(1, h - self._MARGIN_TOP - self._MARGIN_BOTTOM)
        slot_h = min(self._ROW_MAX_H, area_h / n)
        bar_h = slot_h * self._BAR_FILL

        bar_left = self._MARGIN_LEFT + self._NAME_BAND
        bar_right_limit = w - self._MARGIN_RIGHT - self._VALUE_BAND
        track_w = max(1.0, bar_right_limit - bar_left)

        max_amount = max((float(r.amount) for r in self._rows), default=0.0)
        if max_amount <= 0:
            self._paint_empty(painter, "No spending for this period")
            return

        name_font = QFont(painter.font())
        name_font.setPointSize(9)
        value_font = QFont(painter.font())
        value_font.setPointSize(9)
        name_fm = QFontMetrics(name_font)
        value_fm = QFontMetrics(value_font)

        for i, row in enumerate(self._rows):
            slot_top = area_top + i * slot_h
            bar_top = slot_top + (slot_h - bar_h) / 2
            colour = QColor(_COLOR_BAR)

            # Faint full-width track so short bars still read as a row.
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(_COLOR_TRACK)))
            painter.drawRoundedRect(
                QRectF(bar_left, bar_top, track_w, bar_h),
                self._BAR_RADIUS, self._BAR_RADIUS,
            )

            # The bar itself.
            length = track_w * (float(row.amount) / max_amount)
            length = max(2.0, length)
            bar_rect = QRectF(bar_left, bar_top, length, bar_h)
            painter.setBrush(QBrush(colour))
            painter.drawRoundedRect(bar_rect, self._BAR_RADIUS, self._BAR_RADIUS)
            self._hitmap.append(
                (QRectF(bar_left, slot_top, track_w, slot_h), i),
            )

            # Payee name — left band, vertically centred, elided to fit.
            painter.setFont(name_font)
            painter.setPen(QPen(QColor(_COLOR_NAME)))
            name = name_fm.elidedText(
                row.name, Qt.ElideRight, self._NAME_BAND - 8,
            )
            y_text = int(slot_top + slot_h / 2 + name_fm.ascent() / 2 - 1)
            painter.drawText(int(self._MARGIN_LEFT), y_text, name)

            # Value — right band, right-aligned.
            painter.setFont(value_font)
            painter.setPen(QPen(QColor(_COLOR_VALUE)))
            value = fmt_currency(float(row.amount), 0, symbol=self._symbol)
            vw = value_fm.horizontalAdvance(value)
            painter.drawText(
                int(w - self._MARGIN_RIGHT - vw), y_text, value,
            )

    def _paint_empty(self, painter: QPainter, message: str) -> None:
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_EMPTY)))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(
            int((self.width() - tw) / 2),
            int(self.height() / 2),
            message,
        )

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        if not self._rows:
            super().mouseMoveEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, idx in self._hitmap:
            if rect.contains(pos):
                row = self._rows[idx]
                txns = f"{row.txn_count} txn" + ("s" if row.txn_count != 1 else "")
                text = (
                    f"{row.name}\n"
                    f"{fmt_currency(float(row.amount), 2, symbol=self._symbol)}"
                    f"  ·  {row.pct * 100:.1f}%\n"
                    f"{txns}\n"
                    f"Click to see transactions"
                )
                QToolTip.showText(
                    self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                    text, self,
                )
                self.setCursor(Qt.PointingHandCursor)
                return
        QToolTip.hideText()
        self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: D401 — Qt override
        if event.button() == Qt.LeftButton and self._rows:
            pos = event.position() if hasattr(event, "position") else event.posF()
            for rect, idx in self._hitmap:
                if rect.contains(pos):
                    row = self._rows[idx]
                    self.payee_clicked.emit(row.payee_id, row.name)
                    return
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401 — Qt override
        QToolTip.hideText()
        self.unsetCursor()
        super().leaveEvent(event)
