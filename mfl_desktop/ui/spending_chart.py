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

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import colour_for, fmt_currency, nice_ticks
import mfl_desktop.ui.chart_helpers as _ch
from mfl_desktop.ui.ui_fonts import set_pt


class SpendingChart(QWidget):
    """Stacked-bar chart with average line and legend.

    The Spending window does the SQL roll-up and hands structured data in
    via :meth:`render`. Empty state is signalled with :meth:`show_empty`.

    Emits :py:attr:`segment_clicked(group_id, bucket)` when the user
    clicks a stack segment — the report window uses this to push a
    drill-down onto its filter stack (ADR-039 follow-up). ``group_id``
    is the category id of the rolled-up bucket (the same id the report
    window already groups on); ``bucket`` is the time-bucket key the
    segment belongs to, so a future drill could also narrow by period.
    """

    segment_clicked = Signal(int, str)
    # Double-click on a segment → open its transactions directly (ADR-114).
    # Kept distinct from the single-click drill so a double-click doesn't
    # first drill (re-laying-out the chart) under its own second click.
    segment_double_clicked = Signal(int, str)

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
        # ADR-159: the report's display currency. The chart formats money in four
        # places (axis, average pill, bar totals, tooltip) and used to hard-code
        # the fmt_currency default of "£" in all of them.
        self._symbol = "£"
        self._empty_message: Optional[str] = None
        # When False, the bottom-of-chart legend strip is skipped and
        # the legend band isn't reserved (the chart fills the space).
        # The window owns an external vertical legend instead — see
        # SpendingReportWindow's summary panel.
        self._show_legend = True

        # Updated each paintEvent — list of (rect, group_index, bucket, value)
        # for hover hit-testing.
        self._segment_hitmap: list[tuple[QRectF, int, str, float]] = []

        # Updated each paintEvent — (x_center, top_y, total_pounds) per bar, so
        # the stack totals can be printed above them (ADR-157). Collected while
        # the bars are laid out rather than recomputed, since _paint_bars
        # already sums each stack and tracks its top edge.
        self._bar_totals: list[tuple[float, float, float]] = []

        # Single-vs-double click disambiguation (ADR-114). A single click
        # drills; a double click opens transactions. The single-click action
        # is deferred by the double-click interval so the first press of a
        # double-click doesn't drill (and re-lay-out the chart) before the
        # second click is recognised — which is what made a double-click land
        # on the wrong bar.
        self._pending_single: Optional[tuple[int, str]] = None
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._emit_pending_single)

    # ── public interface ──

    def render(
        self,
        *,
        buckets: list[str],
        groups: list[tuple[int, str]],
        spending: dict[tuple[int, str], int],
        avg_pounds: float,
        currency_symbol: str = "£",
    ) -> None:
        self._buckets = buckets
        self._groups = groups
        self._spending = spending
        self._avg_pounds = avg_pounds
        self._symbol = currency_symbol or "£"
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._buckets = []
        self._groups = []
        self._spending = {}
        self._empty_message = message
        self.update()

    def set_show_legend(self, on: bool) -> None:
        """Toggle the bottom-of-chart legend strip. When off, the legend
        band is reclaimed for the chart and the caller is expected to
        render its own legend somewhere else (e.g. a side panel)."""
        if self._show_legend == on:
            return
        self._show_legend = on
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

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
        # ADR-157: the stack totals are laid out first but drawn last. Laying
        # them out first lets the average pill dodge them; drawing them last
        # keeps the dashed average line from striking through the text.
        totals = self._layout_bar_totals(chart_rect)
        self._paint_average(painter, chart_rect, ymax,
                            avoid=[r for r, _t in totals])
        self._paint_bar_totals(painter, totals)
        if self._show_legend:
            self._paint_legend(painter, legend_rect)
        self._paint_axis_baseline(painter, chart_rect)

        painter.end()

    def _compute_rects(self) -> tuple[QRectF, QRectF]:
        w = self.width()
        h = self.height()
        legend_band = self._LEGEND_BAND if self._show_legend else 0
        legend_top = h - legend_band
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
            legend_band,
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
        pen = QPen(QColor(_ch.chart_grid()))
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
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)

        n_ticks = int(round(ymax / step)) if step > 0 else 0
        for i in range(n_ticks + 1):
            v = i * step
            y = chart.bottom() - (v / ymax) * chart.height()
            label = fmt_currency(v, symbol=self._symbol)
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                int(chart.left() - tw - 8),
                int(y + fm.ascent() / 2 - 2),
                label,
            )

    def _paint_x_labels(self, painter: QPainter, chart: QRectF) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
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
        self._bar_totals.clear()
        n = len(self._buckets)
        if n == 0 or ymax <= 0:
            return

        slot_w = chart.width() / n
        bar_w = slot_w * self._BAR_SLOT_FILL
        radius = _ch.bar_corner_radius(bar_w)  # shared, consistent (ADR-128)

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
            bar_top_y = chart.bottom()
            for seg_pos, (group_index, pounds) in enumerate(seg_values):
                seg_bottom = chart.bottom() - (running / ymax) * chart.height()
                seg_top = chart.bottom() - ((running + pounds) / ymax) * chart.height()
                rect = QRectF(x_left, seg_top, bar_w, seg_bottom - seg_top)

                colour = colour_for(group_index)
                is_top = seg_pos == top_segment_index_in_list
                self._draw_bar_segment(painter, rect, colour, is_top)
                if is_top:
                    bar_top_y = seg_top

                self._segment_hitmap.append((rect, group_index, bucket, pounds))
                running += pounds

            # Round the whole bar's top corners once — so a thin top segment
            # rounds exactly like a tall one, consistently across every bar and
            # report (ADR-128). Carve the full bar rect, not the top segment.
            bar_rect = QRectF(x_left, bar_top_y, bar_w, chart.bottom() - bar_top_y)
            _ch.round_bar_corners(
                painter, bar_rect, radius, QColor(_ch.chart_surface()),
            )

            # `running` is now the whole stack's total (ADR-157).
            self._bar_totals.append((x_left + bar_w / 2, bar_top_y, running))

    def _draw_bar_segment(
        self,
        painter: QPainter,
        rect: QRectF,
        colour: QColor,
        is_top: bool,
    ) -> None:
        # Every segment is a plain rect; the bar's top corners are rounded once
        # by the caller (ADR-128), so no per-segment rounding is needed.
        painter.fillRect(rect, colour)

        # 1px separator between stacked segments (the plot background colour).
        if not is_top:
            sep_pen = QPen(QColor(_ch.chart_surface()))
            sep_pen.setWidth(1)
            painter.setPen(sep_pen)
            painter.drawLine(
                int(rect.left()), int(rect.top()),
                int(rect.right()), int(rect.top()),
            )
            painter.setPen(Qt.NoPen)

    def _paint_average(
        self,
        painter: QPainter,
        chart: QRectF,
        ymax: float,
        avoid: Optional[list[QRectF]] = None,
    ) -> None:
        if ymax <= 0 or self._avg_pounds <= 0:
            return
        y = chart.bottom() - (self._avg_pounds / ymax) * chart.height()

        pen = QPen(QColor(_ch.chart_ink()))
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

        # Pill label at the right end.
        font = QFont(painter.font())
        set_pt(font, 9)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = f"Avg {fmt_currency(self._avg_pounds, symbol=self._symbol)}"
        tw = fm.horizontalAdvance(text) + 12
        th = fm.height() + 4
        pill = QRectF(
            chart.right() - tw,
            y - th - 4,
            tw,
            th,
        )
        # ADR-157: the pill sits above the line at the right edge — exactly where
        # the last bar's total label lands when that bar happens to be near the
        # average (which it often is; the last bucket is usually a part-period).
        # The total is data and the pill is annotation, so the pill moves: drop
        # it below the line. Its dark fill keeps it legible over a bar.
        if avoid and any(pill.intersects(r) for r in avoid):
            below = QRectF(pill)
            below.moveTop(y + 4)
            if not any(below.intersects(r) for r in avoid):
                pill = below
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(_ch.chart_tooltip_bg())))
        painter.drawRoundedRect(pill, th / 2, th / 2)
        painter.setPen(QPen(QColor(_ch.chart_tooltip_ink())))
        painter.drawText(
            int(pill.left() + 6),
            int(pill.top() + fm.ascent() + 2),
            text,
        )

    def _totals_font(self) -> QFont:
        font = QFont(self.font())
        set_pt(font, 9)
        font.setBold(True)
        return font

    def _layout_bar_totals(self, chart: QRectF) -> list[tuple[QRectF, str]]:
        """Where each stack's total goes — geometry only, no painting (ADR-157).

        A stacked bar's headline quantity is its total, and it was the one
        number the chart never showed. The summary panel gives a grand total and
        an average; hovering a segment gives that one segment. The per-bucket
        total — what the bar actually *is* — could only be had by eyeballing
        gridlines that may be £20,000 apart, or adding four tooltips up by hand.

        Returns ``[]`` — suppressing the labels **wholesale** — when the widest
        one wouldn't fit its slot. Granularity goes down to daily, so a wide span
        can produce hundreds of buckets whose labels would collide into mush; the
        tooltip still has the numbers there. All-or-nothing rather than per-bar,
        so the chart never looks like it labelled an arbitrary subset.
        """
        if not self._bar_totals or not self._buckets:
            return []

        fm = QFontMetrics(self._totals_font())
        labels = [fmt_currency(total, symbol=self._symbol)
                  for _x, _top, total in self._bar_totals]
        widest = max(fm.horizontalAdvance(t) for t in labels)
        slot_w = chart.width() / len(self._buckets)
        if slot_w < widest + 8:      # +8: minimum gutter between neighbours
            return []

        out: list[tuple[QRectF, str]] = []
        for (x_center, top_y, _total), text in zip(self._bar_totals, labels):
            tw = fm.horizontalAdvance(text)
            # The y-axis reserves 12% headroom above the tallest bar (see
            # _compute_y_axis) and there's a top margin above that, so this
            # normally has room. Clamp anyway: one dominant bucket could still
            # push its bar close enough to the top to clip the text.
            baseline = max(top_y - 6, fm.ascent() + 2)
            rect = QRectF(
                x_center - tw / 2, baseline - fm.ascent(), tw, fm.height(),
            )
            out.append((rect, text))
        return out

    def _paint_bar_totals(
        self, painter: QPainter, totals: list[tuple[QRectF, str]],
    ) -> None:
        if not totals:
            return
        fm = QFontMetrics(self._totals_font())
        painter.setFont(self._totals_font())
        surface = QColor(_ch.chart_surface())
        ink = QPen(QColor(_ch.chart_ink()))
        for rect, text in totals:
            # Knock the background out behind the text first. Drawing the labels
            # last already puts them *over* the gridlines and the dashed average
            # line, but a dashed line still shows through the gaps between glyph
            # strokes and reads as a strike-through — which is exactly what
            # happened to a bar whose top landed just under the average.
            # A label never overlaps a neighbouring bar (it is centred on its own
            # bar and constrained to its slot), so the surface colour is always
            # the correct thing to clear to.
            painter.setPen(Qt.NoPen)
            painter.fillRect(rect.adjusted(-3, -1, 3, 1), surface)
            painter.setPen(ink)
            painter.drawText(int(rect.left()), int(rect.top() + fm.ascent()), text)

    def _paint_legend(self, painter: QPainter, legend: QRectF) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
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

            painter.setPen(QPen(QColor(_ch.chart_ink())))
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
        pen = QPen(QColor(_ch.chart_faint()))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(
            int(chart.left()), int(chart.bottom()),
            int(chart.right()), int(chart.bottom()),
        )

    def _paint_empty(self, painter: QPainter) -> None:
        message = self._empty_message or ""
        font = QFont(painter.font())
        set_pt(font, 11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(
            int((self.width() - tw) / 2),
            int(self.height() / 2),
            message,
        )

    # ── hover tooltip / click drill-down ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, group_index, bucket, pounds in self._segment_hitmap:
            if rect.contains(pos):
                name = self._groups[group_index][1] if group_index < len(self._groups) else ""
                text = (
                    f"{name}\n{bucket}\n"
                    f"{fmt_currency(pounds, 2, symbol=self._symbol)}"
                )
                QToolTip.showText(
                    self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                    text,
                    self,
                )
                self.setCursor(QCursor(Qt.PointingHandCursor))
                return
        QToolTip.hideText()
        self.unsetCursor()
        super().mouseMoveEvent(event)

    def _segment_at(self, pos) -> Optional[tuple[int, str]]:
        """The ``(group_id, bucket)`` of the stack segment under ``pos``, or
        None. Shared by hover, single-click and double-click so all three
        resolve the same hit."""
        for rect, group_index, bucket, _pounds in self._segment_hitmap:
            if rect.contains(pos) and 0 <= group_index < len(self._groups):
                return self._groups[group_index][0], bucket
        return None

    def mousePressEvent(self, event) -> None:  # noqa: D401 — Qt override
        """Left-click on a stack segment *drills* (via
        :py:attr:`segment_clicked`) — but the emit is deferred by the double-
        click interval so a double-click (which opens transactions) isn't
        preceded by a drill that re-lays-out the chart. Other buttons fall
        through to the base class."""
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        hit = self._segment_at(pos)
        if hit is None:
            super().mousePressEvent(event)
            return
        self._pending_single = hit
        self._click_timer.start(QApplication.doubleClickInterval())

    def _emit_pending_single(self) -> None:
        if self._pending_single is None:
            return
        group_id, bucket = self._pending_single
        self._pending_single = None
        self.segment_clicked.emit(int(group_id), bucket)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: D401 — Qt override
        """Left double-click on a stack segment fires
        :py:attr:`segment_double_clicked` (open transactions) and cancels the
        pending single-click drill. The hitmap is still the pre-drill layout
        here because the single-click action was deferred, so the resolved
        segment is the one the user actually clicked."""
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        self._click_timer.stop()
        self._pending_single = None
        pos = event.position() if hasattr(event, "position") else event.posF()
        hit = self._segment_at(pos)
        if hit is None:
            super().mouseDoubleClickEvent(event)
            return
        group_id, bucket = hit
        self.segment_double_clicked.emit(int(group_id), bucket)

    def leaveEvent(self, event) -> None:  # noqa: D401 — Qt override
        QToolTip.hideText()
        self.unsetCursor()
        super().leaveEvent(event)
