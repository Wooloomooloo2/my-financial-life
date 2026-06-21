"""Combo chart for the per-account summary screen (ADR-033 + ADR-034).

Per-bucket income bars above the zero baseline and per-bucket spending
bars below it, with a balance polyline overlaid in the app accent. Same
paintEvent + ``chart_helpers`` recipe as the Spending Over Time chart
(ADR-026) and the budget burn-down (ADR-025) — modern flat look, soft
gridlines, hover tooltip, rounded corners on the outer ends of the bars.

**Dual y-axis (ADR-034).** Bars scale against an independent left axis
(``nice_ticks`` over the combined income / spending range). The balance
polyline scales against an independent right axis (``nice_ticks`` over
the combined positive / negative balance range). Each axis owns its
own zero — typically at different y-pixels — which is what lets the
credit-card-paydown view render correctly (balance line in the lower
band, bars at their natural scale in the upper band) without squishing
either series. Right-axis labels render in blue-600 (the balance line's
colour) so the eye associates them with the line.
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

from mfl_desktop.account_summary import BalanceFlowSeries
from mfl_desktop.ui.chart_helpers import fmt_currency, nice_ticks
import mfl_desktop.ui.chart_helpers as _ch
from mfl_desktop.ui.ui_fonts import set_pt


# Series colours — local to this chart, not the GROUP_PALETTE. Picked
# from the same Tailwind v3 ramp as the rest of the palette (ADR-026).
_COLOR_INCOME   = "#10b981"   # emerald-500 — positive flow
_COLOR_SPENDING = "#ef4444"   # red-500 — negative flow


class _AxisRange:
    """Independent positive / negative reach for one y-axis.

    ``top`` and ``bottom`` are positive magnitudes; ``step`` is the
    ``nice_ticks`` interval used for both sides. Empty side: 0.0.
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
        """Build a range that fits both sides with `nice_ticks` plus
        10% headroom. ``negative_max`` is a positive magnitude (caller
        already abs'd it)."""
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


class BalanceFlowChart(QWidget):
    """Stateless widget — call :meth:`set_data` to render.

    The window does the SQL roll-up via :mod:`mfl_desktop.account_summary`
    and hands the resulting :class:`BalanceFlowSeries` in. Empty state
    is signalled with :meth:`show_empty`.
    """

    _MARGIN_TOP    = 20
    _MARGIN_RIGHT  = 72        # room for right-axis "£10,000" labels (ADR-034)
    _MARGIN_LEFT   = 72        # room for left-axis "£10,000" labels
    _AXIS_LABEL_BAND = 22      # x-axis labels
    _LEGEND_BAND   = 26        # legend strip
    _BAR_SLOT_FILL = 0.60      # bar takes 60% of bucket slot
    _BAR_RADIUS_MAX = 5.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(260)

        self._data: Optional[BalanceFlowSeries] = None
        self._empty_message: Optional[str] = None

        # (rect, kind, bucket_index) where kind ∈ {"income", "spending"}.
        # Populated each paintEvent for hover hit-testing.
        self._hitmap: list[tuple[QRectF, str, int]] = []

    # ── public interface ──

    def set_data(self, data: BalanceFlowSeries) -> None:
        self._data = data
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._data = None
        self._empty_message = message
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

        if self._empty_message is not None:
            self._paint_empty(painter, self._empty_message)
            painter.end()
            return

        data = self._data
        if data is None or not data.buckets:
            self._paint_empty(painter, "No data for this period")
            painter.end()
            return

        chart_rect, legend_rect = self._compute_rects()
        bars_axis, line_axis = self._compute_axes(data)

        # Gridlines use the bars axis so the income / spending grid stays
        # readable. The balance line is then overlaid against its own
        # scale — the labels in the right margin tell the user that.
        self._paint_gridlines(painter, chart_rect, bars_axis)
        self._paint_left_labels(painter, chart_rect, bars_axis)
        self._paint_right_labels(painter, chart_rect, line_axis)
        self._paint_x_labels(painter, chart_rect, data)
        self._paint_bars(painter, chart_rect, data, bars_axis)
        self._paint_zero_baseline(painter, chart_rect, bars_axis)
        self._paint_balance_line(painter, chart_rect, data, line_axis)
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

    def _compute_axes(
        self, data: BalanceFlowSeries,
    ) -> tuple[_AxisRange, _AxisRange]:
        """Independent left / right axes (ADR-034). Bars own the left
        scale, balance line owns the right."""
        max_income = max(
            (float(b.income) for b in data.buckets), default=0.0,
        )
        max_spending = max(
            (float(b.spending) for b in data.buckets), default=0.0,
        )
        bars_axis = _AxisRange.fit(max_income, max_spending)

        balances = [float(data.opening_balance)] + [
            float(b.closing_balance) for b in data.buckets
        ]
        max_positive_balance = max((v for v in balances if v > 0), default=0.0)
        max_negative_balance = max((-v for v in balances if v < 0), default=0.0)
        line_axis = _AxisRange.fit(max_positive_balance, max_negative_balance)

        # If the line axis collapsed (balance never moves off zero), give
        # it a token range so the line still has a place to render.
        if line_axis.is_empty():
            line_axis = _AxisRange(100.0, 0.0, 20.0)
        return bars_axis, line_axis

    # ── paint sub-routines ──

    def _paint_gridlines(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        if axis.step <= 0:
            return
        pen = QPen(QColor(QColor(_ch.chart_grid())))
        pen.setWidth(1)
        painter.setPen(pen)
        # Positive side — skip zero (the explicit baseline owns it).
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

    def _paint_left_labels(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        if axis.step <= 0:
            return
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(QColor(_ch.chart_axis_ink()))))
        fm = QFontMetrics(font)

        def draw(value: float) -> None:
            y = axis.y_to_px(value, chart)
            label = fmt_currency(abs(value))
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

    def _paint_right_labels(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        """Right-axis labels for the balance line — coloured blue-600 to
        match the line so the eye associates them (ADR-034)."""
        if axis.step <= 0:
            return
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_accent())))
        fm = QFontMetrics(font)

        def draw(value: float) -> None:
            y = axis.y_to_px(value, chart)
            label = fmt_currency(abs(value))
            if value < 0:
                label = "-" + label
            painter.drawText(
                int(chart.right() + 8),
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

    def _paint_x_labels(
        self, painter: QPainter, chart: QRectF, data: BalanceFlowSeries,
    ) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(QColor(_ch.chart_axis_ink()))))
        fm = QFontMetrics(font)

        n = len(data.buckets)
        if n == 0:
            return
        slot_w = chart.width() / n

        sample = 1
        if slot_w < 60:
            sample = max(2, int(math.ceil(60 / slot_w)))

        for i, b in enumerate(data.buckets):
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
        self,
        painter: QPainter,
        chart: QRectF,
        data: BalanceFlowSeries,
        axis: _AxisRange,
    ) -> None:
        self._hitmap.clear()
        n = len(data.buckets)
        if n == 0:
            return

        slot_w = chart.width() / n
        bar_w = slot_w * self._BAR_SLOT_FILL
        radius = min(self._BAR_RADIUS_MAX, bar_w / 3)
        zero_y = axis.zero_y(chart)

        income_color = QColor(_COLOR_INCOME)
        spending_color = QColor(_COLOR_SPENDING)

        painter.setPen(Qt.NoPen)
        for i, b in enumerate(data.buckets):
            x_left = chart.left() + (i + 0.5) * slot_w - bar_w / 2
            # Income bar — grows up from zero, rounded TOP corners.
            if b.income > 0 and axis.top > 0:
                y_top = axis.y_to_px(float(b.income), chart)
                rect = QRectF(x_left, y_top, bar_w, zero_y - y_top)
                self._draw_rounded_rect(
                    painter, rect, income_color, radius,
                    round_top=True, round_bottom=False,
                )
                self._hitmap.append((rect, "income", i))
            # Spending bar — grows down from zero, rounded BOTTOM corners.
            if b.spending > 0 and axis.bottom > 0:
                y_bottom = axis.y_to_px(-float(b.spending), chart)
                rect = QRectF(x_left, zero_y, bar_w, y_bottom - zero_y)
                self._draw_rounded_rect(
                    painter, rect, spending_color, radius,
                    round_top=False, round_bottom=True,
                )
                self._hitmap.append((rect, "spending", i))

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
        """Draw ``rect`` filled with ``colour``, rounding only the
        requested corners. Very short bars (< radius * 1.4) fall back to
        a plain rect so the rounded corners don't collapse on top of each
        other."""
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
            painter.setBrush(QBrush(colour))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, radius, radius)
            return
        painter.fillPath(path, colour)

    def _paint_zero_baseline(
        self, painter: QPainter, chart: QRectF, axis: _AxisRange,
    ) -> None:
        """Bar-axis zero only (ADR-034) — that's the line bars cross
        between income and spending. The balance line uses its own scale
        and doesn't get a separate baseline drawn."""
        pen = QPen(QColor(QColor(_ch.chart_axis_ink())))
        pen.setWidth(1)
        painter.setPen(pen)
        y = axis.zero_y(chart)
        painter.drawLine(
            int(chart.left()), int(y),
            int(chart.right()), int(y),
        )

    def _paint_balance_line(
        self,
        painter: QPainter,
        chart: QRectF,
        data: BalanceFlowSeries,
        axis: _AxisRange,
    ) -> None:
        n = len(data.buckets)
        if n == 0:
            return
        slot_w = chart.width() / n
        points: list[QPointF] = []
        # Leading point: opening balance at the LEFT edge of bucket 0.
        points.append(QPointF(
            chart.left(),
            axis.y_to_px(float(data.opening_balance), chart),
        ))
        # One point per bucket end (RIGHT edge of that bucket).
        for i, b in enumerate(data.buckets):
            x = chart.left() + (i + 1) * slot_w
            y = axis.y_to_px(float(b.closing_balance), chart)
            points.append(QPointF(x, y))

        pen = QPen(QColor(_ch.chart_accent()))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        for i in range(1, len(points)):
            painter.drawLine(points[i - 1], points[i])

        # Tiny accent dots at each bucket-end point so the eye locks onto
        # the trajectory; skipped at <40px slot width to avoid clutter on
        # long-period zooms.
        if slot_w >= 40:
            painter.setBrush(QBrush(QColor(_ch.chart_accent())))
            painter.setPen(Qt.NoPen)
            for p in points[1:]:
                painter.drawEllipse(p, 2.5, 2.5)

    def _paint_legend(self, painter: QPainter, legend: QRectF) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        fm = QFontMetrics(font)

        x = legend.left()
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_swatch = legend.top() + (legend.height() - 10) / 2

        # Income chip.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(_COLOR_INCOME)))
        painter.drawRoundedRect(QRectF(x, y_swatch, 10, 10), 2, 2)
        painter.setPen(QPen(QColor(_ch.chart_ink())))
        painter.drawText(int(x + 16), int(y_text), "Income (left axis)")
        x += 16 + fm.horizontalAdvance("Income (left axis)") + 18

        # Spending chip.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(_COLOR_SPENDING)))
        painter.drawRoundedRect(QRectF(x, y_swatch, 10, 10), 2, 2)
        painter.setPen(QPen(QColor(_ch.chart_ink())))
        painter.drawText(int(x + 16), int(y_text), "Spending (left axis)")
        x += 16 + fm.horizontalAdvance("Spending (left axis)") + 18

        # Balance — line swatch (longer rectangle so it reads as a line).
        line_pen = QPen(QColor(_ch.chart_accent()))
        line_pen.setWidth(2)
        painter.setPen(line_pen)
        painter.drawLine(
            int(x), int(legend.top() + legend.height() / 2),
            int(x + 18), int(legend.top() + legend.height() / 2),
        )
        painter.setPen(QPen(QColor(_ch.chart_ink())))
        painter.drawText(int(x + 24), int(y_text), "Balance (right axis)")

    def _paint_axis_baseline(self, painter: QPainter, chart: QRectF) -> None:
        pen = QPen(QColor(QColor(_ch.chart_faint())))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(
            int(chart.left()), int(chart.bottom()),
            int(chart.right()), int(chart.bottom()),
        )

    def _paint_empty(self, painter: QPainter, message: str) -> None:
        font = QFont(painter.font())
        set_pt(font, 11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(QColor(_ch.chart_axis_ink()))))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(
            int((self.width() - tw) / 2),
            int(self.height() / 2),
            message,
        )

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        if self._data is None:
            super().mouseMoveEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, kind, idx in self._hitmap:
            if rect.contains(pos):
                bucket = self._data.buckets[idx]
                if kind == "income":
                    value = bucket.income
                    line1 = f"Income · {bucket.label}"
                else:
                    value = bucket.spending
                    line1 = f"Spending · {bucket.label}"
                text = (
                    f"{line1}\n"
                    f"{fmt_currency(float(value), 2)}\n"
                    f"Balance end of bucket: "
                    f"{fmt_currency(float(bucket.closing_balance), 2)}"
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
