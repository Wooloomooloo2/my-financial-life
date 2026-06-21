"""Portfolio value-over-time chart for the investment dashboard (ADR-045,
paintEvent per ADR-026 / [[feedback-chart-engine-preference]]).

Two lines across time:
- **Invested** (cost basis of holdings held on each date — exact, price-free)
- **Market value** (Σ shares × nearest-prior historical price)

The area between them is filled green where value ≥ invested (gain) and red
where value < invested (loss), per segment, so a portfolio that dips below
cost reads at a glance. Consumes the `ValuePoint`s from
`holdings.compute_value_history`; currency-aware (the account may be USD).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
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
from mfl_desktop.ui.ui_fonts import set_pt

_COLOR_INVESTED = QColor("#2563eb")   # blue-600
_COLOR_GAIN_FILL = QColor(22, 163, 74, 40)   # green, translucent
_COLOR_LOSS_FILL = QColor(220, 38, 38, 40)   # red, translucent


class ValueHistoryChart(QWidget):
    """Invested vs market-value line chart over time."""

    _MARGIN_TOP = 24
    _MARGIN_RIGHT = 20
    _MARGIN_LEFT = 84
    _AXIS_LABEL_BAND = 24
    _LEGEND_BAND = 30

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._points: list = []          # list[ValuePoint]
        self._symbol = "$"
        self._any_fallback = False
        self._empty_message: Optional[str] = None
        # (x_px, point_index) for nearest-x hover.
        self._x_positions: list[tuple[float, int]] = []

    # ── public ──

    def render(self, points: list, currency_symbol: str = "$",
               any_fallback: bool = False) -> None:
        self._points = points
        self._symbol = currency_symbol or ""
        self._any_fallback = any_fallback
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._points = []
        self._empty_message = message
        self.update()

    # ── formatting ──

    def _fmt(self, amount: float, decimals: int = 0) -> str:
        sym = self._symbol
        sign = "-" if amount < 0 else ""
        a = abs(amount)
        return f"{sign}{sym}{a:,.{decimals}f}" if sym else f"{sign}{a:,.{decimals}f}"

    @staticmethod
    def _month_label(iso: str) -> str:
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

        if self._empty_message is not None or len(self._points) < 2:
            self._paint_empty(
                painter,
                self._empty_message or "Not enough history to chart yet.",
            )
            painter.end()
            return

        chart, legend = self._compute_rects()
        vmax = max(
            max(float(p.invested_cost), float(p.market_value)) for p in self._points
        )
        ymax, ystep = nice_ticks(vmax * 1.1)
        self._paint_gridlines(painter, chart, ymax, ystep)
        self._paint_y_labels(painter, chart, ymax, ystep)
        self._paint_x_labels(painter, chart)
        self._paint_fill_and_lines(painter, chart, ymax)
        self._paint_axis_baseline(painter, chart)
        self._paint_legend(painter, legend)
        painter.end()

    def _compute_rects(self) -> tuple[QRectF, QRectF]:
        w, h = self.width(), self.height()
        legend_top = h - self._LEGEND_BAND
        chart_bottom = legend_top - self._AXIS_LABEL_BAND
        chart = QRectF(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, chart_bottom - self._MARGIN_TOP),
        )
        legend = QRectF(self._MARGIN_LEFT, legend_top,
                        max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
                        self._LEGEND_BAND)
        return chart, legend

    def _x_for(self, i: int, chart: QRectF) -> float:
        n = len(self._points)
        if n <= 1:
            return chart.left()
        return chart.left() + (i / (n - 1)) * chart.width()

    def _y_for(self, v: float, ymax: float, chart: QRectF) -> float:
        return chart.bottom() - (v / ymax) * chart.height() if ymax > 0 else chart.bottom()

    def _paint_gridlines(self, painter, chart, ymax, step) -> None:
        pen = QPen(QColor(_ch.chart_grid()))
        pen.setWidth(1)
        painter.setPen(pen)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            y = chart.bottom() - (i * step / ymax) * chart.height()
            painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

    def _paint_y_labels(self, painter, chart, ymax, step) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            v = i * step
            y = chart.bottom() - (v / ymax) * chart.height()
            label = self._fmt(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(chart.left() - tw - 8),
                             int(y + fm.ascent() / 2 - 2), label)

    def _paint_x_labels(self, painter, chart) -> None:
        font = QFont(painter.font())
        set_pt(font, 8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        n = len(self._points)
        if n == 0:
            return
        # Aim for ~8 labels.
        sample = max(1, n // 8)
        for i, p in enumerate(self._points):
            if i % sample != 0 and i != n - 1:
                continue
            x = self._x_for(i, chart)
            text = self._month_label(p.date)
            tw = fm.horizontalAdvance(text)
            painter.drawText(int(x - tw / 2),
                             int(chart.bottom() + fm.ascent() + 6), text)

    def _paint_fill_and_lines(self, painter, chart, ymax) -> None:
        self._x_positions = []
        invested_pts: list[QPointF] = []
        value_pts: list[QPointF] = []
        for i, p in enumerate(self._points):
            x = self._x_for(i, chart)
            self._x_positions.append((x, i))
            invested_pts.append(QPointF(x, self._y_for(float(p.invested_cost), ymax, chart)))
            value_pts.append(QPointF(x, self._y_for(float(p.market_value), ymax, chart)))

        # Per-segment fill between the two lines, coloured by gain/loss.
        painter.setPen(Qt.NoPen)
        for i in range(len(self._points) - 1):
            inv0, inv1 = float(self._points[i].invested_cost), float(self._points[i + 1].invested_cost)
            val0, val1 = float(self._points[i].market_value), float(self._points[i + 1].market_value)
            poly = QPolygonF([invested_pts[i], value_pts[i],
                              value_pts[i + 1], invested_pts[i + 1]])
            gain = (val0 - inv0) + (val1 - inv1)
            painter.setBrush(QBrush(_COLOR_GAIN_FILL if gain >= 0 else _COLOR_LOSS_FILL))
            painter.drawPolygon(poly)

        self._draw_polyline(painter, invested_pts, _COLOR_INVESTED)
        self._draw_polyline(painter, value_pts, QColor(_ch.chart_axis_ink()))

    def _draw_polyline(self, painter, pts: list[QPointF], colour: QColor) -> None:
        pen = QPen(colour)
        pen.setWidth(2)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolyline(QPolygonF(pts))

    def _paint_axis_baseline(self, painter, chart) -> None:
        pen = QPen(QColor(_ch.chart_faint()))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(chart.bottom()),
                         int(chart.right()), int(chart.bottom()))

    def _paint_legend(self, painter, legend) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        fm = QFontMetrics(font)
        items = [("Invested", _COLOR_INVESTED), ("Market value", QColor(_ch.chart_axis_ink()))]
        x = legend.left()
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_line = legend.top() + legend.height() / 2
        for name, colour in items:
            pen = QPen(colour)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawLine(int(x), int(y_line), int(x + 16), int(y_line))
            painter.setPen(QPen(QColor(_ch.chart_ink())))
            painter.drawText(int(x + 22), int(y_text), name)
            x += 22 + fm.horizontalAdvance(name) + 22
        if self._any_fallback:
            painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
            note = "· early periods use cost where prices are unavailable"
            painter.drawText(int(x), int(y_text), note)

    def _paint_empty(self, painter, message: str) -> None:
        font = QFont(painter.font())
        set_pt(font, 11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
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
        p = self._points[idx]
        inv, val = float(p.invested_cost), float(p.market_value)
        gain = val - inv
        sign = "+" if gain >= 0 else "-"
        pct = (gain / inv * 100) if inv else 0.0
        text = (
            f"{self._month_label(p.date)}\n"
            f"Invested {self._fmt(inv)}\n"
            f"Value {self._fmt(val)}\n"
            f"{'Gain' if gain >= 0 else 'Loss'} {sign}{self._fmt(abs(gain))} "
            f"({sign}{abs(pct):.1f}%)"
        )
        QToolTip.showText(
            self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))), text, self,
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401
        QToolTip.hideText()
        super().leaveEvent(event)
