"""Compact burn-down chart for the budget screen — paintEvent rewrite.

Two line series — Actual cumulative outflow vs. the linear Ideal pacing
line — plus a vertical marker for today. Sits below the summary tiles
on the budget window; not its own report.

Replaces the QtCharts implementation that shipped with ADR-025. The
owner picked the hand-rolled paintEvent variant after the chart-engine
comparison in ADR-026; this is the symmetric port from the Spending Over
Time chart. Same public contract as before — ``set_data(BurnDownData)``
— so ``budget_window.py`` doesn't change.
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
# Actual stays red (intuitive "spend" semantic carried over from ADR-025);
# Ideal grey dashed; Today marker in the app accent so it stands out
# without competing with the data lines.
_COLOR_ACTUAL = "#dc2626"   # red-600
_COLOR_IDEAL  = "#6b7280"   # slate-500
_COLOR_TODAY  = "#2563eb"   # blue-600 — the app accent
_COLOR_AXIS   = "#9ca3af"
_COLOR_GRID   = "#e5e7eb"
_COLOR_LABEL  = "#6b7280"


class BurnDownChart(QWidget):
    """Stateless widget — call ``set_data(burn_down)`` to render.

    Public contract preserved from the previous QtCharts implementation
    so ``budget_window.py`` doesn't need to change.
    """

    # Compact widget — sits between the tiles and the cards on the budget
    # window. Height-bounded; width follows the layout.
    _MARGIN_TOP = 16
    _MARGIN_RIGHT = 14
    _MARGIN_LEFT = 62           # room for "£10,000"
    _AXIS_LABEL_BAND = 18       # x-axis day labels
    _LEGEND_BAND = 22           # small swatches + labels

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

        self._data: Optional[BurnDownData] = None

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(220)
        self.setMaximumHeight(260)

    def set_data(self, data: BurnDownData) -> None:
        """Replace the chart's series + axes from a BurnDownData snapshot."""
        self._data = data
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        data = self._data
        if data is None or not data.x_days:
            self._paint_empty(painter, "No burn-down data")
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
        self._paint_today_marker(painter, chart_rect, data, x_min, x_span)
        self._paint_series(painter, chart_rect, data, ymax, x_min, x_span)
        self._paint_legend(painter, legend_rect)
        self._paint_axis_baseline(painter, chart_rect)

        painter.end()

    # ── geometry / axis ──

    def _compute_rects(self) -> tuple[QRectF, QRectF]:
        w = self.width()
        h = self.height()
        legend_top = h - self._LEGEND_BAND
        chart_bottom = legend_top - self._AXIS_LABEL_BAND
        chart = QRectF(
            self._MARGIN_LEFT,
            self._MARGIN_TOP,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, chart_bottom - self._MARGIN_TOP),
        )
        legend = QRectF(
            self._MARGIN_LEFT,
            legend_top,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            self._LEGEND_BAND,
        )
        return chart, legend

    def _compute_y_axis(self, data: BurnDownData) -> tuple[float, float]:
        max_actual = max((float(v) for v in data.actual), default=0.0)
        vmax = max(float(data.total_planned), max_actual, 1.0)
        ymax, step = nice_ticks(vmax * 1.10, target_count=4)
        return ymax, step

    def _x_to_px(self, x_day: int, chart: QRectF, x_min: int, x_span: int) -> float:
        return chart.left() + ((x_day - x_min) / x_span) * chart.width()

    def _y_to_px(self, y_val: float, chart: QRectF, ymax: float) -> float:
        if ymax <= 0:
            return chart.bottom()
        return chart.bottom() - (y_val / ymax) * chart.height()

    # ── paint sub-routines ──

    def _paint_gridlines(
        self, painter: QPainter, chart: QRectF, ymax: float, step: float
    ) -> None:
        pen = QPen(QColor(_COLOR_GRID))
        pen.setWidth(1)
        painter.setPen(pen)
        n_ticks = int(round(ymax / step)) if step > 0 else 0
        for i in range(n_ticks + 1):
            v = i * step
            y = self._y_to_px(v, chart, ymax)
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))

    def _paint_y_labels(
        self, painter: QPainter, chart: QRectF, ymax: float, step: float
    ) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)
        n_ticks = int(round(ymax / step)) if step > 0 else 0
        for i in range(n_ticks + 1):
            v = i * step
            y = self._y_to_px(v, chart, ymax)
            label = fmt_currency(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                int(chart.left() - tw - 8),
                int(y + fm.ascent() / 2 - 2),
                label,
            )

    def _paint_x_labels(
        self,
        painter: QPainter,
        chart: QRectF,
        data: BurnDownData,
        x_min: int,
        x_span: int,
    ) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)

        # Pick ~6 evenly-spaced day ticks (first + last always shown).
        n = len(data.x_days)
        target = 6
        step = max(1, (n - 1) // target)
        indices = list(range(0, n, step))
        if indices[-1] != n - 1:
            indices.append(n - 1)

        for i in indices:
            day = data.x_days[i]
            x = self._x_to_px(day, chart, x_min, x_span)
            label = str(day)
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                int(x - tw / 2),
                int(chart.bottom() + fm.ascent() + 4),
                label,
            )

    def _paint_today_marker(
        self,
        painter: QPainter,
        chart: QRectF,
        data: BurnDownData,
        x_min: int,
        x_span: int,
    ) -> None:
        if data.today_day < x_min or data.today_day > data.x_days[-1]:
            return
        x = self._x_to_px(data.today_day, chart, x_min, x_span)

        pen = QPen(QColor(_COLOR_TODAY))
        pen.setWidth(1)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        painter.drawLine(int(x), int(chart.top()), int(x), int(chart.bottom()))

        # "Today" pill at the top.
        font = QFont(painter.font())
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = f"Today · day {data.today_day}"
        tw = fm.horizontalAdvance(text) + 12
        th = fm.height() + 2
        # Position pill so it stays inside the chart rect horizontally.
        pill_left = min(chart.right() - tw, max(chart.left(), x - tw / 2))
        pill = QRectF(pill_left, chart.top() - 2, tw, th)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(_COLOR_TODAY)))
        painter.drawRoundedRect(pill, th / 2, th / 2)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            int(pill.left() + 6),
            int(pill.top() + fm.ascent() + 1),
            text,
        )

    def _paint_series(
        self,
        painter: QPainter,
        chart: QRectF,
        data: BurnDownData,
        ymax: float,
        x_min: int,
        x_span: int,
    ) -> None:
        # Ideal — drawn first so Actual sits visually on top.
        self._paint_polyline(
            painter,
            data.x_days,
            data.ideal,
            chart,
            ymax,
            x_min,
            x_span,
            colour=QColor(_COLOR_IDEAL),
            width=2,
            style=Qt.DashLine,
        )
        # Actual.
        self._paint_polyline(
            painter,
            data.x_days,
            data.actual,
            chart,
            ymax,
            x_min,
            x_span,
            colour=QColor(_COLOR_ACTUAL),
            width=2,
            style=Qt.SolidLine,
        )

    def _paint_polyline(
        self,
        painter: QPainter,
        xs: list[int],
        ys,
        chart: QRectF,
        ymax: float,
        x_min: int,
        x_span: int,
        *,
        colour: QColor,
        width: int,
        style: Qt.PenStyle,
    ) -> None:
        if not xs:
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

    def _paint_legend(self, painter: QPainter, legend: QRectF) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        fm = QFontMetrics(font)

        entries = [
            ("Actual", QColor(_COLOR_ACTUAL), Qt.SolidLine),
            ("Ideal",  QColor(_COLOR_IDEAL),  Qt.DashLine),
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

            painter.setPen(QPen(QColor("#374151")))
            painter.drawText(int(x + 24), int(y_text), label)
            x += 24 + fm.horizontalAdvance(label) + 18

    def _paint_axis_baseline(self, painter: QPainter, chart: QRectF) -> None:
        pen = QPen(QColor(_COLOR_AXIS))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(
            int(chart.left()), int(chart.bottom()),
            int(chart.right()), int(chart.bottom()),
        )

    def _paint_empty(self, painter: QPainter, message: str) -> None:
        font = QFont(painter.font())
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(
            int((self.width() - tw) / 2),
            int(self.height() / 2),
            message,
        )
