"""Per-security price-over-time mini chart for the Stock Record screen
(ADR-047; paintEvent per ADR-026 / [[feedback-chart-engine-preference]]).

A single line of a security's stored price points over time — whatever their
source (Tiingo history, manual entry, or a price seeded from an untickered
holding's own trades). Deliberately small and dependency-free: it reuses the
shared ``nice_ticks`` Y-axis heuristic and mirrors ``value_history_chart``'s
structure (index-proportional X, hover tooltip, empty-state message) so it
looks consistent with the other charts.

Prices are per-share quotes (often USD), not pence — the chart is
currency-neutral and takes an optional symbol prefix (default ``$``).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import nice_ticks
import mfl_desktop.ui.chart_helpers as _ch

_COLOR_LINE = QColor("#2563eb")    # blue-600
_COLOR_GRID = QColor(_ch.chart_grid())
_COLOR_AXIS = QColor(_ch.chart_faint())
_COLOR_LABEL = QColor(_ch.chart_axis_ink())
_COLOR_DOT = QColor("#2563eb")


class PriceHistoryChart(QWidget):
    """Single-line price chart over time. Feed it ``[(iso_date, price)]``
    ascending via ``render``."""

    _MARGIN_TOP = 18
    _MARGIN_RIGHT = 16
    _MARGIN_LEFT = 72
    _AXIS_LABEL_BAND = 22

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(160)
        self._points: list[tuple[str, float]] = []
        self._symbol = "$"
        self._empty_message: Optional[str] = None
        self._x_positions: list[tuple[float, int]] = []

    # ── public ──

    def render(self, points: list[tuple[str, float]], currency_symbol: str = "$") -> None:
        """``points`` = ascending ``(iso_date, price)`` pairs."""
        self._points = list(points)
        self._symbol = currency_symbol or ""
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._points = []
        self._empty_message = message
        self.update()

    # ── formatting ──

    def _fmt(self, price: float, decimals: int = 2) -> str:
        sym = self._symbol
        return f"{sym}{price:,.{decimals}f}" if sym else f"{price:,.{decimals}f}"

    @staticmethod
    def _date_label(iso: str) -> str:
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            return iso
        return f"{d.strftime('%b')} {d.strftime('%y')}"

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

        if self._empty_message is not None or len(self._points) < 1:
            self._paint_empty(
                painter, self._empty_message or "No price history yet.",
            )
            painter.end()
            return

        if len(self._points) == 1:
            # A single point can't draw a line — show it as a labelled dot.
            self._paint_single(painter)
            painter.end()
            return

        chart = self._compute_rect()
        prices = [p for _, p in self._points]
        vmax = max(prices)
        ymax, ystep = nice_ticks(vmax * 1.1)
        self._paint_gridlines(painter, chart, ymax, ystep)
        self._paint_y_labels(painter, chart, ymax, ystep)
        self._paint_x_labels(painter, chart)
        self._paint_line(painter, chart, ymax)
        self._paint_axis_baseline(painter, chart)
        painter.end()

    def _compute_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        chart_bottom = h - self._AXIS_LABEL_BAND
        return QRectF(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, chart_bottom - self._MARGIN_TOP),
        )

    def _x_for(self, i: int, chart: QRectF) -> float:
        n = len(self._points)
        if n <= 1:
            return chart.left()
        return chart.left() + (i / (n - 1)) * chart.width()

    def _y_for(self, v: float, ymax: float, chart: QRectF) -> float:
        return chart.bottom() - (v / ymax) * chart.height() if ymax > 0 else chart.bottom()

    def _paint_gridlines(self, painter, chart, ymax, step) -> None:
        pen = QPen(_COLOR_GRID)
        pen.setWidth(1)
        painter.setPen(pen)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            y = chart.bottom() - (i * step / ymax) * chart.height()
            painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

    def _paint_y_labels(self, painter, chart, ymax, step) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(_COLOR_LABEL))
        fm = QFontMetrics(font)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            v = i * step
            y = chart.bottom() - (v / ymax) * chart.height()
            label = self._fmt(v, decimals=0 if step >= 1 else 2)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(chart.left() - tw - 8),
                             int(y + fm.ascent() / 2 - 2), label)

    def _paint_x_labels(self, painter, chart) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(_COLOR_LABEL))
        fm = QFontMetrics(font)
        n = len(self._points)
        if n == 0:
            return
        sample = max(1, n // 8)
        for i, (iso, _) in enumerate(self._points):
            if i % sample != 0 and i != n - 1:
                continue
            x = self._x_for(i, chart)
            text = self._date_label(iso)
            tw = fm.horizontalAdvance(text)
            painter.drawText(int(x - tw / 2),
                             int(chart.bottom() + fm.ascent() + 6), text)

    def _paint_line(self, painter, chart, ymax) -> None:
        self._x_positions = []
        pts: list[QPointF] = []
        for i, (_, price) in enumerate(self._points):
            x = self._x_for(i, chart)
            self._x_positions.append((x, i))
            pts.append(QPointF(x, self._y_for(float(price), ymax, chart)))
        pen = QPen(_COLOR_LINE)
        pen.setWidth(2)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolyline(QPolygonF(pts))

    def _paint_axis_baseline(self, painter, chart) -> None:
        pen = QPen(_COLOR_AXIS)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(chart.bottom()),
                         int(chart.right()), int(chart.bottom()))

    def _paint_single(self, painter) -> None:
        iso, price = self._points[0]
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        fm = QFontMetrics(font)
        cx, cy = self.width() / 2, self.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(_COLOR_DOT)
        painter.drawEllipse(QPointF(cx, cy), 4, 4)
        text = f"{self._date_label(iso)}  {self._fmt(price)}"
        tw = fm.horizontalAdvance(text)
        painter.setPen(QPen(_COLOR_LABEL))
        painter.drawText(int(cx - tw / 2), int(cy - 12), text)

    def _paint_empty(self, painter, message: str) -> None:
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(_COLOR_LABEL))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(int((self.width() - tw) / 2), int(self.height() / 2), message)

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401
        if not self._x_positions:
            super().mouseMoveEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        nearest = min(self._x_positions, key=lambda xp: abs(xp[0] - pos.x()))
        idx = nearest[1]
        iso, price = self._points[idx]
        text = f"{self._date_label(iso)}\n{self._fmt(price)}"
        QToolTip.showText(
            self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))), text, self,
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401
        QToolTip.hideText()
        super().leaveEvent(event)
