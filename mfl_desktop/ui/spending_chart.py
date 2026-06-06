"""Hand-rolled stacked-bar chart for the Spending Over Time report (ADR-026).

Design notes:
- Modern flat look: solid fills, 1px white separator between segments to
  define stacks visually, rounded top on the topmost segment.
- Soft horizontal gridlines at "nice" tick values.
- Average line is dashed at the average y, label hugs the right side.
- Legend across the bottom strip, dropping overflow when the swatches
  don't fit (owner can widen the window; wrap-to-second-row is a future
  iteration).
- Hover shows a tooltip per stack segment with the group name + value.

This replaces the QtCharts + pyqtgraph + custom three-engine comparison
that shipped under ADR-026's first pass. The owner picked the paintEvent
variant; the other two are gone.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import colour_for, fmt_currency, nice_ticks


class SpendingChart(QWidget):
    """Stacked-bar chart with average line and legend.

    The Spending window does the SQL roll-up and hands structured data in
    via :meth:`render`. Empty state is signalled with :meth:`show_empty`.
    """

    # Layout constants — tuned for readability at 1240x740 (the window's
    # default size). Recalculated each paintEvent so the chart adapts to
    # any window size.
    _MARGIN_TOP = 24
    _MARGIN_RIGHT = 20
    _MARGIN_LEFT = 72          # room for "£10,000"
    _AXIS_LABEL_BAND = 24      # bottom band for the x-axis labels
    _LEGEND_BAND = 36          # bottom band for the legend chips
    _BAR_SLOT_FILL = 0.62      # bar takes 62% of its bucket slot

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

        self._buckets: list[str] = []
        self._groups: list[tuple[int, str]] = []
        self._spending: dict[tuple[int, str], int] = {}
        self._avg_pounds = 0.0
        self._empty_message: Optional[str] = None

        # Updated each paintEvent — list of (rect, group_index, bucket, value)
        # for hover hit-testing.
        self._segment_hitmap: list[tuple[QRectF, int, str, float]] = []

    # ── public interface ──

    def render(
        self,
        *,
        buckets: list[str],
        groups: list[tuple[int, str]],
        spending: dict[tuple[int, str], int],
        avg_pounds: float,
    ) -> None:
        self._buckets = buckets
        self._groups = groups
        self._spending = spending
        self._avg_pounds = avg_pounds
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._buckets = []
        self._groups = []
        self._spending = {}
        self._empty_message = message
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        if self._empty_message is not None:
            self._paint_empty(painter)
            painter.end()
            return

        if not self._buckets or not self._groups:
            painter.end()
            return

        chart_rect, legend_rect = self._compute_rects()
        ymax, ystep = self._compute_y_axis()

        self._paint_gridlines(painter, chart_rect, ymax, ystep)
        self._paint_y_labels(painter, chart_rect, ymax, ystep)
        self._paint_x_labels(painter, chart_rect)
        self._paint_bars(painter, chart_rect, ymax)
        self._paint_average(painter, chart_rect, ymax)
        self._paint_legend(painter, legend_rect)
        self._paint_axis_baseline(painter, chart_rect)

        painter.end()

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

    def _compute_y_axis(self) -> tuple[float, float]:
        bucket_totals = [
            sum(self._spending.get((gid, b), 0) for gid, _ in self._groups) / 100.0
            for b in self._buckets
        ]
        vmax = max(bucket_totals) if bucket_totals else 100.0
        ymax, step = nice_ticks(vmax * 1.12)
        return ymax, step

    def _paint_gridlines(
        self, painter: QPainter, chart: QRectF, ymax: float, step: float
    ) -> None:
        pen = QPen(QColor("#e5e7eb"))
        pen.setWidth(1)
        painter.setPen(pen)

        n_ticks = int(round(ymax / step)) if step > 0 else 0
        for i in range(n_ticks + 1):
            v = i * step
            y = chart.bottom() - (v / ymax) * chart.height()
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))

    def _paint_y_labels(
        self, painter: QPainter, chart: QRectF, ymax: float, step: float
    ) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#6b7280")))
        fm = QFontMetrics(font)

        n_ticks = int(round(ymax / step)) if step > 0 else 0
        for i in range(n_ticks + 1):
            v = i * step
            y = chart.bottom() - (v / ymax) * chart.height()
            label = fmt_currency(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                int(chart.left() - tw - 8),
                int(y + fm.ascent() / 2 - 2),
                label,
            )

    def _paint_x_labels(self, painter: QPainter, chart: QRectF) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#6b7280")))
        fm = QFontMetrics(font)

        n = len(self._buckets)
        if n == 0:
            return
        slot_w = chart.width() / n

        # If labels would overlap, sample every Nth.
        sample = 1
        if slot_w < 60:
            sample = max(2, int(math.ceil(60 / slot_w)))

        for i, bucket in enumerate(self._buckets):
            if i % sample != 0 and i != n - 1:
                continue
            x_center = chart.left() + (i + 0.5) * slot_w
            tw = fm.horizontalAdvance(bucket)
            painter.drawText(
                int(x_center - tw / 2),
                int(chart.bottom() + fm.ascent() + 6),
                bucket,
            )

    def _paint_bars(
        self, painter: QPainter, chart: QRectF, ymax: float
    ) -> None:
        self._segment_hitmap.clear()
        n = len(self._buckets)
        if n == 0 or ymax <= 0:
            return

        slot_w = chart.width() / n
        bar_w = slot_w * self._BAR_SLOT_FILL
        radius = min(6.0, bar_w / 4)

        painter.setPen(Qt.NoPen)
        for i, bucket in enumerate(self._buckets):
            x_left = chart.left() + (i + 0.5) * slot_w - bar_w / 2

            # Compute non-zero segments first so the topmost is identifiable.
            seg_values: list[tuple[int, float]] = []  # (group_index, pounds)
            for idx, (gid, _name) in enumerate(self._groups):
                pence = self._spending.get((gid, bucket), 0)
                if pence > 0:
                    seg_values.append((idx, pence / 100.0))

            if not seg_values:
                continue

            running = 0.0
            top_segment_index_in_list = len(seg_values) - 1
            for seg_pos, (group_index, pounds) in enumerate(seg_values):
                seg_bottom = chart.bottom() - (running / ymax) * chart.height()
                seg_top = chart.bottom() - ((running + pounds) / ymax) * chart.height()
                rect = QRectF(x_left, seg_top, bar_w, seg_bottom - seg_top)

                colour = colour_for(group_index)
                is_top = seg_pos == top_segment_index_in_list
                self._draw_bar_segment(painter, rect, colour, is_top, radius)

                self._segment_hitmap.append((rect, group_index, bucket, pounds))
                running += pounds

    def _draw_bar_segment(
        self,
        painter: QPainter,
        rect: QRectF,
        colour: QColor,
        is_top: bool,
        radius: float,
    ) -> None:
        if is_top and rect.height() > radius * 1.4:
            # Rounded top corners only on the topmost segment.
            path = QPainterPath()
            path.moveTo(rect.left(), rect.bottom())
            path.lineTo(rect.left(), rect.top() + radius)
            path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
            path.lineTo(rect.right() - radius, rect.top())
            path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
            path.lineTo(rect.right(), rect.bottom())
            path.closeSubpath()
            painter.fillPath(path, colour)
        else:
            painter.fillRect(rect, colour)

        # 1px white separator between stacked segments.
        if not is_top:
            sep_pen = QPen(QColor("#ffffff"))
            sep_pen.setWidth(1)
            painter.setPen(sep_pen)
            painter.drawLine(
                int(rect.left()), int(rect.top()),
                int(rect.right()), int(rect.top()),
            )
            painter.setPen(Qt.NoPen)

    def _paint_average(
        self, painter: QPainter, chart: QRectF, ymax: float
    ) -> None:
        if ymax <= 0 or self._avg_pounds <= 0:
            return
        y = chart.bottom() - (self._avg_pounds / ymax) * chart.height()

        pen = QPen(QColor("#374151"))
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

        # Pill label at the right end.
        font = QFont(painter.font())
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = f"Avg {fmt_currency(self._avg_pounds)}"
        tw = fm.horizontalAdvance(text) + 12
        th = fm.height() + 4
        pill = QRectF(
            chart.right() - tw,
            y - th - 4,
            tw,
            th,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#1f2937")))
        painter.drawRoundedRect(pill, th / 2, th / 2)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            int(pill.left() + 6),
            int(pill.top() + fm.ascent() + 2),
            text,
        )

    def _paint_legend(self, painter: QPainter, legend: QRectF) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        fm = QFontMetrics(font)

        x = legend.left()
        y = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        swatch_size = 10
        gap_after_swatch = 6
        gap_between_items = 18

        painter.setPen(Qt.NoPen)
        for idx, (_gid, name) in enumerate(self._groups):
            sw_rect = QRectF(
                x,
                legend.top() + (legend.height() - swatch_size) / 2,
                swatch_size,
                swatch_size,
            )
            painter.setBrush(QBrush(colour_for(idx)))
            painter.drawRoundedRect(sw_rect, 2, 2)

            painter.setPen(QPen(QColor("#374151")))
            tw = fm.horizontalAdvance(name)
            painter.drawText(
                int(x + swatch_size + gap_after_swatch),
                int(y),
                name,
            )
            painter.setPen(Qt.NoPen)

            x += swatch_size + gap_after_swatch + tw + gap_between_items
            if x > legend.right() - 60:
                # Out of room — drop the remaining labels. Owner can widen
                # the window, or we'll later iterate on wrap-to-second-row.
                break

    def _paint_axis_baseline(self, painter: QPainter, chart: QRectF) -> None:
        pen = QPen(QColor("#9ca3af"))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(
            int(chart.left()), int(chart.bottom()),
            int(chart.right()), int(chart.bottom()),
        )

    def _paint_empty(self, painter: QPainter) -> None:
        message = self._empty_message or ""
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#6b7280")))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(
            int((self.width() - tw) / 2),
            int(self.height() / 2),
            message,
        )

    # ── hover tooltip ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, group_index, bucket, pounds in self._segment_hitmap:
            if rect.contains(pos):
                name = self._groups[group_index][1] if group_index < len(self._groups) else ""
                text = f"{name}\n{bucket}\n{fmt_currency(pounds, 2)}"
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
