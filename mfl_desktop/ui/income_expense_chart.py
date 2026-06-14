"""Income & Expense combo chart (ADR-064 / Arc E, E1).

Per-bucket **income bars above** the zero baseline and **expense bars
below** it, with a **net (income − expense) line** overlaid and optional
**average reference lines** for income and expense. Same paintEvent +
``chart_helpers`` recipe as the per-account ``BalanceFlowChart``
(ADR-033/034) and the Spending chart (ADR-026) — modern flat look, soft
gridlines, hover tooltip, rounded outer bar corners. No pies (ADR-018).

Single shared y-axis: income, expense and the net line are all in the
same currency at comparable magnitudes (``|net| ≤ max(income, expense)``),
so one ``nice_ticks`` scale fits them all — simpler than the dual axis
``BalanceFlowChart`` needs for its balance line.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from mfl_desktop.reports.income_expense import IEBucket
from mfl_desktop.ui.chart_helpers import fmt_currency, nice_ticks

_COLOR_INCOME  = "#10b981"   # emerald-500 — income (matches BalanceFlowChart)
_COLOR_EXPENSE = "#ef4444"   # red-500 — expense
_COLOR_NET     = "#2563eb"   # blue-600 — app accent, net line
_COLOR_ZERO    = "#6b7280"   # slate-500 — zero baseline
_COLOR_GRID    = "#e5e7eb"
_COLOR_AXIS    = "#9ca3af"
_COLOR_LABEL   = "#6b7280"


class _AxisRange:
    """Positive (up) / negative (down) reach for the single y-axis.

    ``top`` / ``bottom`` are positive magnitudes; ``step`` is the
    ``nice_ticks`` interval shared by both sides.
    """

    __slots__ = ("top", "bottom", "step")

    def __init__(self, top: float, bottom: float, step: float) -> None:
        self.top = top
        self.bottom = bottom
        self.step = step

    def is_empty(self) -> bool:
        return self.top <= 0 and self.bottom <= 0

    @classmethod
    def fit(cls, positive_max: float, negative_max: float) -> "_AxisRange":
        if positive_max <= 0 and negative_max <= 0:
            return cls(0.0, 0.0, 0.0)
        top, top_step = (
            nice_ticks(positive_max * 1.10) if positive_max > 0 else (0.0, 0.0)
        )
        bottom, bottom_step = (
            nice_ticks(negative_max * 1.10) if negative_max > 0 else (0.0, 0.0)
        )
        step = max(top_step, bottom_step, 1.0)
        if top > 0 and step > 0:
            top = math.ceil(top / step) * step
        if bottom > 0 and step > 0:
            bottom = math.ceil(bottom / step) * step
        return cls(top, bottom, step)

    def zero_y(self, chart: QRectF) -> float:
        total = self.top + self.bottom
        if total <= 0:
            return chart.bottom()
        return chart.top() + (self.top / total) * chart.height()

    def y_to_px(self, value: float, chart: QRectF) -> float:
        zero_y = self.zero_y(chart)
        if value >= 0:
            if self.top <= 0:
                return zero_y
            return zero_y - (value / self.top) * (zero_y - chart.top())
        if self.bottom <= 0:
            return zero_y
        return zero_y + ((-value) / self.bottom) * (chart.bottom() - zero_y)


class IncomeExpenseChart(QWidget):
    """Stateless widget — call :meth:`render` to draw, :meth:`show_empty`
    for the no-data state. The window does the SQL roll-up + FX via the
    Repository and the pure ``income_expense`` module."""

    _MARGIN_TOP    = 20
    _MARGIN_RIGHT  = 16
    _MARGIN_LEFT   = 78        # room for "£10,000" axis labels
    _AXIS_LABEL_BAND = 22      # x-axis labels
    _LEGEND_BAND   = 26
    _BAR_SLOT_FILL = 0.60
    _BAR_RADIUS_MAX = 5.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(280)

        self._buckets: list[IEBucket] = []
        self._avg_income: float = 0.0
        self._avg_expense: float = 0.0
        self._symbol: str = "£"
        self._empty_message: Optional[str] = None

        # (rect, kind, bucket_index) for hover hit-testing; kind ∈
        # {"income", "expense"}.
        self._hitmap: list[tuple[QRectF, str, int]] = []

    # ── public interface ──

    def render(
        self,
        *,
        buckets: list[IEBucket],
        avg_income: float,
        avg_expense: float,
        symbol: str = "£",
    ) -> None:
        self._buckets = buckets
        self._avg_income = avg_income
        self._avg_expense = avg_expense
        self._symbol = symbol or "£"
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._buckets = []
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
        if not self._buckets:
            self._paint_empty(painter, "No data for this period")
            painter.end()
            return

        chart_rect, legend_rect = self._compute_rects()
        axis = self._compute_axis()

        self._paint_gridlines(painter, chart_rect, axis)
        self._paint_axis_labels(painter, chart_rect, axis)
        self._paint_x_labels(painter, chart_rect)
        self._paint_bars(painter, chart_rect, axis)
        self._paint_zero_baseline(painter, chart_rect, axis)
        self._paint_average_lines(painter, chart_rect, axis)
        self._paint_net_line(painter, chart_rect, axis)
        self._paint_legend(painter, legend_rect)
        self._paint_axis_baseline(painter, chart_rect)
        painter.end()

    # ── geometry ──

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

    def _compute_axis(self) -> _AxisRange:
        max_income = max((float(b.income) for b in self._buckets), default=0.0)
        max_expense = max((float(b.expense) for b in self._buckets), default=0.0)
        axis = _AxisRange.fit(max_income, max_expense)
        if axis.is_empty():
            axis = _AxisRange(100.0, 0.0, 20.0)
        return axis

    # ── paint sub-routines ──

    def _paint_gridlines(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        if axis.step <= 0:
            return
        pen = QPen(QColor(_COLOR_GRID))
        pen.setWidth(1)
        painter.setPen(pen)
        n_top = int(round(axis.top / axis.step))
        for i in range(1, n_top + 1):
            y = axis.y_to_px(i * axis.step, chart)
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))
        n_bot = int(round(axis.bottom / axis.step))
        for i in range(1, n_bot + 1):
            y = axis.y_to_px(-i * axis.step, chart)
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))

    def _paint_axis_labels(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        if axis.step <= 0:
            return
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)

        def draw(value: float) -> None:
            y = axis.y_to_px(value, chart)
            label = fmt_currency(abs(value), symbol=self._symbol)
            if value < 0:
                label = "-" + label
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                int(chart.left() - tw - 8),
                int(y + fm.ascent() / 2 - 2),
                label,
            )

        draw(0.0)
        n_top = int(round(axis.top / axis.step))
        for i in range(1, n_top + 1):
            draw(i * axis.step)
        n_bot = int(round(axis.bottom / axis.step))
        for i in range(1, n_bot + 1):
            draw(-i * axis.step)

    def _paint_x_labels(self, painter: QPainter, chart: QRectF) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)

        n = len(self._buckets)
        if n == 0:
            return
        slot_w = chart.width() / n
        sample = 1
        if slot_w < 56:
            sample = max(2, int(math.ceil(56 / slot_w)))
        for i, b in enumerate(self._buckets):
            if i % sample != 0 and i != n - 1:
                continue
            x_center = chart.left() + (i + 0.5) * slot_w
            tw = fm.horizontalAdvance(b.label)
            painter.drawText(
                int(x_center - tw / 2),
                int(chart.bottom() + fm.ascent() + 6),
                b.label,
            )

    def _paint_bars(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        self._hitmap.clear()
        n = len(self._buckets)
        if n == 0:
            return
        slot_w = chart.width() / n
        bar_w = slot_w * self._BAR_SLOT_FILL
        radius = min(self._BAR_RADIUS_MAX, bar_w / 3)
        zero_y = axis.zero_y(chart)

        income_color = QColor(_COLOR_INCOME)
        expense_color = QColor(_COLOR_EXPENSE)
        painter.setPen(Qt.NoPen)
        for i, b in enumerate(self._buckets):
            # Income and expense share one column centred on the slot — the
            # expense bar sits directly beneath the income bar, split at the
            # zero baseline (like BalanceFlowChart), rather than offset
            # side-by-side.
            x_left = chart.left() + (i + 0.5) * slot_w - bar_w / 2
            # Income bar — grows up from zero, rounded top corners.
            if b.income > 0 and axis.top > 0:
                y_top = axis.y_to_px(float(b.income), chart)
                rect = QRectF(x_left, y_top, bar_w, zero_y - y_top)
                self._draw_rounded_rect(
                    painter, rect, income_color, radius,
                    round_top=True, round_bottom=False,
                )
                self._hitmap.append((rect, "income", i))
            # Expense bar — grows down from zero, same column, rounded bottom.
            if b.expense > 0 and axis.bottom > 0:
                y_bottom = axis.y_to_px(-float(b.expense), chart)
                rect = QRectF(x_left, zero_y, bar_w, y_bottom - zero_y)
                self._draw_rounded_rect(
                    painter, rect, expense_color, radius,
                    round_top=False, round_bottom=True,
                )
                self._hitmap.append((rect, "expense", i))

    @staticmethod
    def _draw_rounded_rect(
        painter: QPainter,
        rect: QRectF,
        colour: QColor,
        radius: float,
        *,
        round_top: bool,
        round_bottom: bool,
    ) -> None:
        if rect.height() < radius * 1.4 or not (round_top or round_bottom):
            painter.fillRect(rect, colour)
            return
        path = QPainterPath()
        if round_top and not round_bottom:
            path.moveTo(rect.left(), rect.bottom())
            path.lineTo(rect.left(), rect.top() + radius)
            path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
            path.lineTo(rect.right() - radius, rect.top())
            path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
            path.lineTo(rect.right(), rect.bottom())
            path.closeSubpath()
        elif round_bottom and not round_top:
            path.moveTo(rect.left(), rect.top())
            path.lineTo(rect.right(), rect.top())
            path.lineTo(rect.right(), rect.bottom() - radius)
            path.quadTo(rect.right(), rect.bottom(),
                        rect.right() - radius, rect.bottom())
            path.lineTo(rect.left() + radius, rect.bottom())
            path.quadTo(rect.left(), rect.bottom(),
                        rect.left(), rect.bottom() - radius)
            path.closeSubpath()
        else:
            painter.fillRect(rect, colour)
            return
        painter.fillPath(path, colour)

    def _paint_zero_baseline(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        pen = QPen(QColor(_COLOR_ZERO))
        pen.setWidth(1)
        painter.setPen(pen)
        y = axis.zero_y(chart)
        painter.drawLine(int(chart.left()), int(y),
                         int(chart.right()), int(y))

    def _paint_average_lines(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        """Dashed horizontal reference lines at the mean income (up) and
        mean expense (down) per bucket."""
        for value, colour in (
            (self._avg_income, _COLOR_INCOME),
            (-self._avg_expense, _COLOR_EXPENSE),
        ):
            if value == 0:
                continue
            pen = QPen(QColor(colour))
            pen.setWidth(1)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            y = axis.y_to_px(value, chart)
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))

    def _paint_net_line(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        n = len(self._buckets)
        if n == 0:
            return
        slot_w = chart.width() / n
        points: list[QPointF] = []
        for i, b in enumerate(self._buckets):
            x = chart.left() + (i + 0.5) * slot_w
            y = axis.y_to_px(float(b.net), chart)
            points.append(QPointF(x, y))

        pen = QPen(QColor(_COLOR_NET))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        for i in range(1, len(points)):
            painter.drawLine(points[i - 1], points[i])

        if slot_w >= 40:
            painter.setBrush(QBrush(QColor(_COLOR_NET)))
            painter.setPen(Qt.NoPen)
            for p in points:
                painter.drawEllipse(p, 2.5, 2.5)

    def _paint_legend(self, painter: QPainter, legend: QRectF) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        fm = QFontMetrics(font)

        x = legend.left()
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_swatch = legend.top() + (legend.height() - 10) / 2

        for colour, label in (
            (_COLOR_INCOME, "Income"),
            (_COLOR_EXPENSE, "Expense"),
        ):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(colour)))
            painter.drawRoundedRect(QRectF(x, y_swatch, 10, 10), 2, 2)
            painter.setPen(QPen(QColor("#374151")))
            painter.drawText(int(x + 16), int(y_text), label)
            x += 16 + fm.horizontalAdvance(label) + 18

        # Net — line swatch.
        line_pen = QPen(QColor(_COLOR_NET))
        line_pen.setWidth(2)
        painter.setPen(line_pen)
        painter.drawLine(
            int(x), int(legend.top() + legend.height() / 2),
            int(x + 18), int(legend.top() + legend.height() / 2),
        )
        painter.setPen(QPen(QColor("#374151")))
        painter.drawText(int(x + 24), int(y_text), "Net (income − expense)")

    def _paint_axis_baseline(self, painter: QPainter, chart: QRectF) -> None:
        pen = QPen(QColor(_COLOR_AXIS))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(chart.bottom()),
                         int(chart.right()), int(chart.bottom()))

    def _paint_empty(self, painter: QPainter, message: str) -> None:
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_COLOR_LABEL)))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(
            int((self.width() - tw) / 2),
            int(self.height() / 2),
            message,
        )

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        if not self._buckets:
            super().mouseMoveEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, kind, idx in self._hitmap:
            if rect.contains(pos):
                bucket = self._buckets[idx]
                if kind == "income":
                    value = bucket.income
                    head = f"Income · {bucket.label}"
                else:
                    value = bucket.expense
                    head = f"Expense · {bucket.label}"
                text = (
                    f"{head}\n"
                    f"{fmt_currency(float(value), 2, symbol=self._symbol)}\n"
                    f"Net: {fmt_currency(float(bucket.net), 2, symbol=self._symbol)}"
                )
                QToolTip.showText(
                    self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                    text,
                    self,
                )
                return
        QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401 — Qt override
        QToolTip.hideText()
        super().leaveEvent(event)
