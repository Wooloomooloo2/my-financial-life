"""Projected burn-down chart for the budget monthly view (ADR-058 R3).

A hand-rolled paintEvent chart (ADR-026) showing one month's spend
depletion for a scope (the whole budget, or a single category):

- **Actual** — cumulative outflow magnitude through today (solid red).
- **Ideal** — the linear pacing line to the planned total (grey dashed).
- **Projected** — the forward projection that makes this *better than
  Pocketsmith* (principle 12): from today to month-end it extends the
  observed average daily rate, so an overspending scope keeps climbing
  and visibly **crosses the budget early** instead of going flat at today
  (amber dashed).
- A faint horizontal **Budget** reference at ``total_planned`` so the
  crossing is legible, plus a vertical **Today** marker.

Rebuilds the ADR-025/026 ``burn_down_chart.py`` that R1 removed; same
paintEvent idiom as the Spending Over Time chart. Stateless — call
``set_data(BurnDownData)`` to render.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from mfl_desktop.budget_calc import BurnDownData
from mfl_desktop.ui.chart_helpers import fmt_currency, nice_ticks

# Series colours — local to this chart, not the GROUP_PALETTE.
_COLOR_ACTUAL = "#dc2626"   # red-600 — spend
_COLOR_IDEAL = "#6b7280"    # slate-500 — ideal pacing
_COLOR_PROJECT = "#f59e0b"  # amber-500 — forward projection
_COLOR_BUDGET = "#94a3b8"   # slate-400 — budget reference line
_COLOR_TODAY = "#2563eb"    # blue-600 — the app accent
_COLOR_GRID = "#e5e7eb"
_COLOR_LABEL = "#6b7280"


class BurnDownChart(QWidget):
    """Stateless widget — call ``set_data(BurnDownData)`` to render."""

    _MARGIN_TOP = 18
    _MARGIN_RIGHT = 14
    _MARGIN_LEFT = 62           # room for "£10,000"
    _AXIS_LABEL_BAND = 18       # x-axis day labels
    _LEGEND_BAND = 22           # swatches + labels

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._data: Optional[BurnDownData] = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(220)
        self.setMaximumHeight(280)

    def set_data(self, data: Optional[BurnDownData]) -> None:
        self._data = data
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        data = self._data
        if data is None or not data.x_days:
            self._paint_empty(painter, "No spending to chart this month")
            painter.end()
            return

        chart_rect, legend_rect = self._compute_rects()
        ymax, ystep = self._compute_y_axis(data)
        x_min = data.x_days[0]
        x_max = data.x_days[-1]
        x_span = max(1, x_max - x_min)

        self._paint_gridlines(painter, chart_rect, ymax, ystep)
        self._paint_y_labels(painter, chart_rect, ymax, ystep)
        self._paint_x_labels(painter, chart_rect, data, x_min, x_span)
        self._paint_budget_line(painter, chart_rect, data, ymax)
        self._paint_today_marker(painter, chart_rect, data, x_min, x_span)
        self._paint_series(painter, chart_rect, data, ymax, x_min, x_span)
        self._paint_legend(painter, legend_rect)
        self._paint_axis_baseline(painter, chart_rect)
        painter.end()

    def _paint_empty(self, painter: QPainter, msg: str) -> None:
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        font = QFont(painter.font())
        font.setPointSize(10)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, msg)

    # ── geometry / axis ──

    def _compute_rects(self) -> tuple[QRectF, QRectF]:
        w, h = self.width(), self.height()
        legend_top = h - self._LEGEND_BAND
        chart_bottom = legend_top - self._AXIS_LABEL_BAND
        chart = QRectF(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, chart_bottom - self._MARGIN_TOP),
        )
        legend = QRectF(
            self._MARGIN_LEFT, legend_top,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            self._LEGEND_BAND,
        )
        return chart, legend

    def _compute_y_axis(self, data: BurnDownData) -> tuple[float, float]:
        # Include the projection peak so an over-budget line stays on-chart.
        peak = max(
            [float(data.total_planned)]
            + [float(v) for v in data.actual]
            + [float(v) for v in data.proj]
            + [1.0]
        )
        return nice_ticks(peak * 1.10, target_count=4)

    def _x_to_px(self, x_day, chart, x_min, x_span) -> float:
        return chart.left() + ((x_day - x_min) / x_span) * chart.width()

    def _y_to_px(self, y_val, chart, ymax) -> float:
        if ymax <= 0:
            return chart.bottom()
        return chart.bottom() - (y_val / ymax) * chart.height()

    # ── paint sub-routines ──

    def _paint_gridlines(self, painter, chart, ymax, step) -> None:
        pen = QPen(QColor(_COLOR_GRID))
        pen.setWidth(1)
        painter.setPen(pen)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            y = self._y_to_px(i * step, chart, ymax)
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))

    def _paint_y_labels(self, painter, chart, ymax, step) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            v = i * step
            y = self._y_to_px(v, chart, ymax)
            label = fmt_currency(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(chart.left() - tw - 8),
                             int(y + fm.ascent() / 2 - 2), label)

    def _paint_x_labels(self, painter, chart, data, x_min, x_span) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)
        n = len(data.x_days)
        step = max(1, (n - 1) // 6)
        indices = list(range(0, n, step))
        if indices[-1] != n - 1:
            indices.append(n - 1)
        for i in indices:
            day = data.x_days[i]
            x = self._x_to_px(day, chart, x_min, x_span)
            label = str(day)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(x - tw / 2),
                             int(chart.bottom() + fm.ascent() + 4), label)

    def _paint_budget_line(self, painter, chart, data, ymax) -> None:
        if data.total_planned <= 0:
            return
        y = self._y_to_px(float(data.total_planned), chart, ymax)
        pen = QPen(QColor(_COLOR_BUDGET))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_BUDGET)))
        painter.drawText(int(chart.left() + 4), int(y - 3), "Budget")

    def _paint_today_marker(self, painter, chart, data, x_min, x_span) -> None:
        if data.today_day < x_min or data.today_day > data.x_days[-1]:
            return
        x = self._x_to_px(data.today_day, chart, x_min, x_span)
        pen = QPen(QColor(_COLOR_TODAY))
        pen.setWidth(1)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        painter.drawLine(int(x), int(chart.top()), int(x), int(chart.bottom()))

        font = QFont(painter.font())
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = f"Today · {data.today_day}"
        tw = fm.horizontalAdvance(text) + 12
        th = fm.height() + 2
        pill_left = min(chart.right() - tw, max(chart.left(), x - tw / 2))
        pill = QRectF(pill_left, chart.top() - 4, tw, th)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(_COLOR_TODAY)))
        painter.drawRoundedRect(pill, th / 2, th / 2)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(int(pill.left() + 6),
                         int(pill.top() + fm.ascent() + 1), text)

    def _paint_series(self, painter, chart, data, ymax, x_min, x_span) -> None:
        # Ideal first (back), then projection, then actual on top.
        self._polyline(painter, data.ideal_x, data.ideal, chart, ymax,
                       x_min, x_span, colour=QColor(_COLOR_IDEAL),
                       width=2, style=Qt.DashLine)
        self._polyline(painter, data.proj_x, data.proj, chart, ymax,
                       x_min, x_span, colour=QColor(_COLOR_PROJECT),
                       width=2, style=Qt.DashLine)
        self._polyline(painter, data.actual_x, data.actual, chart, ymax,
                       x_min, x_span, colour=QColor(_COLOR_ACTUAL),
                       width=2, style=Qt.SolidLine)

    def _polyline(self, painter, xs, ys, chart, ymax, x_min, x_span,
                  *, colour, width, style) -> None:
        if len(xs) < 2:
            return
        pen = QPen(colour)
        pen.setWidth(width)
        pen.setStyle(style)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        prev_x = self._x_to_px(xs[0], chart, x_min, x_span)
        prev_y = self._y_to_px(float(ys[0]), chart, ymax)
        for i in range(1, len(xs)):
            x = self._x_to_px(xs[i], chart, x_min, x_span)
            y = self._y_to_px(float(ys[i]), chart, ymax)
            painter.drawLine(int(prev_x), int(prev_y), int(x), int(y))
            prev_x, prev_y = x, y

    def _paint_legend(self, painter, legend) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        fm = QFontMetrics(font)
        entries = [
            ("Actual", QColor(_COLOR_ACTUAL), Qt.SolidLine),
            ("Ideal", QColor(_COLOR_IDEAL), Qt.DashLine),
            ("Projected", QColor(_COLOR_PROJECT), Qt.DashLine),
        ]
        x = legend.left()
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_line = legend.top() + legend.height() / 2
        for label, colour, style in entries:
            pen = QPen(colour)
            pen.setWidth(2)
            pen.setStyle(style)
            painter.setPen(pen)
            painter.drawLine(int(x), int(y_line), int(x + 18), int(y_line))
            x += 24
            painter.setPen(QPen(QColor("#374151")))
            painter.drawText(int(x), int(y_text), label)
            x += fm.horizontalAdvance(label) + 18

    def _paint_axis_baseline(self, painter, chart) -> None:
        pen = QPen(QColor(_COLOR_LABEL))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(chart.bottom()),
                         int(chart.right()), int(chart.bottom()))
