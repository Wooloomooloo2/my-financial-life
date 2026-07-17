"""Budget burn-down for the monthly view (ADR-058 R3, ADR-094, ADR-172).

A hand-rolled paintEvent chart (ADR-026) showing **what is left of this
month's plan**, day by day, for a scope (the whole budget, or one envelope).
It descends: start at the plan, fall as you spend, and the day it reaches zero
is the day you run out.

Until ADR-172 this was a burn-*up* wearing the name — every series climbed
toward a ceiling — and it paced against ``available`` (allocation **plus**
accumulated rollover), which is not a plan you meant to spend. See that ADR.

Three series, all step functions (ADR-094 — spend holds flat then jumps at
each transaction; bills step at their due days rather than sloping):

- **Remaining** — the plan less cumulative outflow, through today. A solid
  line in plain ink: it is a fact, not a judgement, so it carries no colour.
- **Plan** — the pacing line: bills step down at their due days, the
  discretionary remainder spread evenly, reaching zero on the last day.
- **Projected** — the forward projection: unpaid bills step down at their due
  days, plus the discretionary run-rate. Dashed, and **coloured by the
  verdict** — green when it lands above zero, red when it crosses. Every
  scheduled outflow in the budget's perimeter is projected, not only the ones
  linked to an envelope (ADR-173).

The **wedge between Remaining and Plan is the reading** (this idea is lifted
from a burn-down the owner shared): green where you are ahead of plan — more
left than you meant to have — and red where you are behind. The colour is the
comparison, so the chart needs no legend to decode two dashed greys.

Stateless — call ``set_data(BurnDownData)`` to render.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from mfl_desktop.budget_calc import BurnDownData
from mfl_desktop.ui import tokens
from mfl_desktop.ui.chart_helpers import fmt_currency, nice_ticks
import mfl_desktop.ui.chart_helpers as _ch
from mfl_desktop.ui.ui_fonts import set_pt


# Series inks, resolved at paint time so they follow a live theme toggle
# (ADR-076). These were five frozen light-theme hexes — the whole of this
# module's ADR-167 ratchet allowance — including a permanent alarm-red for the
# actual line, which shouted danger at a reader who was comfortably under
# budget. Colour now means something: red is *over*, and only over (ADR-172).
def _ink_remaining() -> QColor:  return QColor(_ch.chart_ink())
def _ink_plan() -> QColor:       return QColor(tokens.c("subtle"))
def _ink_good() -> QColor:       return QColor(tokens.c("positive_strong"))
def _ink_bad() -> QColor:        return QColor(tokens.c("negative_strong"))


def _alpha(colour: QColor, a: int) -> QColor:
    out = QColor(colour)
    out.setAlpha(a)
    return out


class BurnDownChart(QWidget):
    """Stateless widget — call ``set_data(BurnDownData)`` to render."""

    _MARGIN_TOP = 20
    _MARGIN_RIGHT = 14
    _MARGIN_LEFT = 62           # room for "£10,000"
    _AXIS_LABEL_BAND = 18       # x-axis day labels
    _MARGIN_BOTTOM = 8
    # No legend band (ADR-172): the wedge's colour is the comparison, and the
    # three series are direct-labelled at their ends. That is 22px of chart
    # back, and one less decode step.

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

    # ── remaining ──

    def _rem(self, data: BurnDownData, cumulative) -> float:
        """The burn-down value for a cumulative-spend figure.

        ``compute_burndown`` produces **cumulative spend** — that is what the
        bills staircase and the run-rate naturally build, and it stays the one
        representation of the series. 'Remaining' is a pure function of it, so
        it is derived here in the view rather than duplicated in the model.
        """
        return float(data.total_planned) - float(cumulative)

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

        data = self._data
        if data is None or not data.x_days or data.total_planned <= 0:
            self._paint_empty(painter, "Nothing budgeted to burn down")
            painter.end()
            return

        chart = self._compute_rect()
        lo, hi, step = self._compute_y_axis(data)
        x_min = data.x_days[0]
        x_max = data.x_days[-1]
        x_span = max(1, x_max - x_min)
        geo = (chart, lo, hi, x_min, x_span)

        self._paint_gridlines(painter, chart, lo, hi, step)
        self._paint_y_labels(painter, chart, lo, hi, step)
        self._paint_x_labels(painter, chart, data, x_min, x_span)
        self._paint_wedges(painter, data, geo)
        self._paint_zero_line(painter, chart, lo, hi)
        self._paint_today_marker(painter, data, geo)
        self._paint_series(painter, data, geo)
        self._paint_end_labels(painter, data, geo)
        # Last, so it wins any overlap: when the plan has run out, that is the
        # most important thing on the chart.
        self._paint_runs_out(painter, data, geo)
        painter.end()

    def _paint_empty(self, painter: QPainter, msg: str) -> None:
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        font = QFont(painter.font())
        set_pt(font, 10)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, msg)

    # ── geometry / axis ──

    def _compute_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        chart_bottom = h - self._AXIS_LABEL_BAND - self._MARGIN_BOTTOM
        return QRectF(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, chart_bottom - self._MARGIN_TOP),
        )

    def _compute_y_axis(self, data: BurnDownData) -> tuple[float, float, float]:
        """(lo, hi, step). The axis runs from the plan at the top down to zero
        — or **below** zero when spending has overshot it, because an overspend
        is exactly what the reader needs to see and clamping at zero would hide
        the one thing the chart exists to warn about."""
        values = (
            [self._rem(data, v) for v in data.actual]
            + [self._rem(data, v) for v in data.proj]
            + [0.0]
        )
        lo_raw = min(values)
        hi_raw = float(data.total_planned)
        _axis_max, step = nice_ticks(max(hi_raw - lo_raw, 1.0), target_count=4)
        lo = math.floor(lo_raw / step) * step
        hi = math.ceil(hi_raw / step) * step
        if hi <= lo:
            hi = lo + step
        return lo, hi, step

    def _x_to_px(self, x_day, chart, x_min, x_span) -> float:
        return chart.left() + ((x_day - x_min) / x_span) * chart.width()

    def _y_to_px(self, y_val, chart, lo, hi) -> float:
        span = hi - lo
        if span <= 0:
            return chart.bottom()
        return chart.bottom() - ((y_val - lo) / span) * chart.height()

    # ── paint sub-routines ──

    def _paint_gridlines(self, painter, chart, lo, hi, step) -> None:
        pen = QPen(QColor(_ch.chart_grid()))
        pen.setWidth(1)
        painter.setPen(pen)
        for v in self._ticks(lo, hi, step):
            y = self._y_to_px(v, chart, lo, hi)
            painter.drawLine(int(chart.left()), int(y),
                             int(chart.right()), int(y))

    def _ticks(self, lo, hi, step) -> list:
        if step <= 0:
            return [lo]
        out, v, guard = [], lo, 0
        while v <= hi + step / 2 and guard < 64:
            out.append(round(v, 6))
            v += step
            guard += 1
        return out

    def _paint_y_labels(self, painter, chart, lo, hi, step) -> None:
        font = QFont(painter.font())
        set_pt(font, 8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        for v in self._ticks(lo, hi, step):
            y = self._y_to_px(v, chart, lo, hi)
            label = fmt_currency(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(chart.left() - tw - 8),
                             int(y + fm.ascent() / 2 - 2), label)

    def _paint_x_labels(self, painter, chart, data, x_min, x_span) -> None:
        font = QFont(painter.font())
        set_pt(font, 8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
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

    def _paint_zero_line(self, painter, chart, lo, hi) -> None:
        """Zero is the floor — the plan exhausted. Only worth emphasising when
        the axis actually reaches it as an interior line (an overspend); at the
        very bottom it is just the baseline."""
        y = self._y_to_px(0.0, chart, lo, hi)
        pen = QPen(QColor(_ch.chart_axis_ink()))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

    # ── the wedge (the reading) ──

    def _paint_wedges(self, painter, data, geo) -> None:
        """Shade between Remaining and Plan: green where more is left than
        planned, red where less.

        This is the chart's whole comparison, done in colour rather than by
        asking the reader to hold two dashed lines apart. The projection's
        wedge is fainter — it is a forecast, not a fact.
        """
        self._wedge(painter, data, geo, data.actual_x, data.actual, alpha=52)
        self._wedge(painter, data, geo, data.proj_x, data.proj, alpha=26)

    def _wedge(self, painter, data, geo, xs, ys, *, alpha) -> None:
        if len(xs) < 2:
            return
        chart, lo, hi, x_min, x_span = geo
        ideal = {d: v for d, v in zip(data.ideal_x, data.ideal)}
        # Walk the series, splitting into runs of the same sign so each run can
        # be filled with its own colour. A run ends when the series crosses the
        # plan; the crossing itself is approximated at the sample boundary,
        # which at one sample per day is under a pixel of error.
        run: list[tuple[float, float, float]] = []   # (x_px, y_series, y_plan)
        sign: Optional[bool] = None
        for d, v in zip(xs, ys):
            if d not in ideal:
                continue
            rv = self._rem(data, v)
            pv = self._rem(data, ideal[d])
            ahead = rv >= pv
            if sign is None:
                sign = ahead
            if ahead != sign and run:
                self._fill_run(painter, run, sign)
                run = [run[-1]]
                sign = ahead
            run.append((
                self._x_to_px(d, chart, x_min, x_span),
                self._y_to_px(rv, chart, lo, hi),
                self._y_to_px(pv, chart, lo, hi),
            ))
        if run and sign is not None:
            self._fill_run(painter, run, sign, alpha=alpha)

    def _fill_run(self, painter, run, ahead: bool, *, alpha: int = 52) -> None:
        if len(run) < 2:
            return
        colour = _ink_good() if ahead else _ink_bad()
        poly = QPolygonF([QPointF(x, y) for x, y, _p in run])
        for x, _y, p in reversed(run):
            poly.append(QPointF(x, p))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(_alpha(colour, alpha)))
        painter.drawPolygon(poly)

    # ── markers ──

    def _today_pill_rect(self, data, geo) -> Optional[QRectF]:
        """Where the Today pill sits — shared with ``_paint_end_labels``, which
        has to keep out of it. The pill hugs the top of the chart, which is
        exactly where 'Remaining' lands on a scope that has spent nothing yet."""
        chart, _lo, _hi, x_min, x_span = geo
        if data.today_day < x_min or data.today_day > data.x_days[-1]:
            return None
        x = self._x_to_px(data.today_day, chart, x_min, x_span)
        font = QFont(self.font())
        set_pt(font, 8)
        font.setBold(True)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(f"Today · {data.today_day}") + 12
        th = fm.height() + 2
        pill_left = min(chart.right() - tw, max(chart.left(), x - tw / 2))
        return QRectF(pill_left, chart.top() - 4, tw, th)

    def _paint_today_marker(self, painter, data, geo) -> None:
        chart, lo, hi, x_min, x_span = geo
        pill = self._today_pill_rect(data, geo)
        if pill is None:
            return
        x = self._x_to_px(data.today_day, chart, x_min, x_span)
        pen = QPen(QColor(_ch.chart_accent()))
        pen.setWidth(1)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        painter.drawLine(int(x), int(chart.top()), int(x), int(chart.bottom()))

        font = QFont(painter.font())
        set_pt(font, 8)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = f"Today · {data.today_day}"
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(_ch.chart_accent())))
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        # `on_accent`, not a literal white: it *is* white in both themes, so
        # this changes no pixel — but a hex here is indistinguishable from the
        # frozen light-theme ones ADR-167 hunts, and the token says out loud
        # that the choice was deliberate.
        painter.setPen(QPen(QColor(tokens.c("on_accent"))))
        painter.drawText(int(pill.left() + 6),
                         int(pill.top() + fm.ascent() + 1), text)

    def _paint_runs_out(self, painter, data, geo) -> None:
        """Mark the day the plan hits zero — the reading a rising line could
        never give. Nothing to mark when it doesn't."""
        chart, lo, hi, x_min, x_span = geo
        day = data.runs_out_day
        if day is None or day < x_min or day > data.x_days[-1]:
            return
        x = self._x_to_px(day, chart, x_min, x_span)
        y = self._y_to_px(0.0, chart, lo, hi)
        colour = _ink_bad()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawEllipse(QPointF(x, y), 3.5, 3.5)

        font = QFont(painter.font())
        set_pt(font, 8)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = f"Runs out · {day}"
        tw = fm.horizontalAdvance(text)
        tx = min(chart.right() - tw, max(chart.left(), x - tw / 2))
        ty = y - 9
        plate = QRectF(tx - 3, ty - fm.ascent() - 1, tw + 6, fm.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(_alpha(QColor(_ch.chart_surface()), 225)))
        painter.drawRect(plate)
        painter.setPen(QPen(colour))
        painter.drawText(int(tx), int(ty), text)

    # ── series ──

    def _paint_series(self, painter, data, geo) -> None:
        verdict = (
            _ink_bad() if data.runs_out_day is not None else _ink_good()
        )
        self._step_line(painter, data, geo, data.ideal_x, data.ideal,
                        colour=_ink_plan(), width=1, style=Qt.SolidLine)
        self._step_line(painter, data, geo, data.proj_x, data.proj,
                        colour=verdict, width=2, style=Qt.DashLine)
        self._step_line(painter, data, geo, data.actual_x, data.actual,
                        colour=_ink_remaining(), width=3, style=Qt.SolidLine)

    def _step_pts(self, data, geo, xs, ys) -> list:
        """Pixel points tracing a staircase through (xs, ys) in *remaining*
        terms: hold each value flat to the next day, then drop — so the series
        reads as discrete steps rather than a diagonal."""
        chart, lo, hi, x_min, x_span = geo
        if not xs:
            return []
        pts = [(self._x_to_px(xs[0], chart, x_min, x_span),
                self._y_to_px(self._rem(data, ys[0]), chart, lo, hi))]
        for i in range(1, len(xs)):
            x = self._x_to_px(xs[i], chart, x_min, x_span)
            y_prev = self._y_to_px(self._rem(data, ys[i - 1]), chart, lo, hi)
            y = self._y_to_px(self._rem(data, ys[i]), chart, lo, hi)
            pts.append((x, y_prev))   # horizontal hold
            pts.append((x, y))        # vertical drop
        return pts

    def _step_line(self, painter, data, geo, xs, ys,
                   *, colour, width, style) -> None:
        pts = self._step_pts(data, geo, xs, ys)
        if len(pts) < 2:
            return
        pen = QPen(colour)
        pen.setWidth(width)
        pen.setStyle(style)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        for i in range(1, len(pts)):
            painter.drawLine(int(pts[i - 1][0]), int(pts[i - 1][1]),
                             int(pts[i][0]), int(pts[i][1]))

    @staticmethod
    def _hits(tx, ty, tw, fm, rect: QRectF) -> bool:
        """Does a text drawn at (tx, ty) baseline overlap ``rect``?"""
        text_rect = QRectF(tx - 3, ty - fm.ascent() - 1, tw + 6, fm.height())
        return text_rect.intersects(rect)

    def _paint_end_labels(self, painter, data, geo) -> None:
        """Name each line where it ends, instead of in a legend band.

        Three dashed styles and a swatch strip made the reader look away from
        the data to decode it. A label at the line's own end is read in place —
        and the wedge already says which is which.
        """
        chart, lo, hi, x_min, x_span = geo
        font = QFont(painter.font())
        set_pt(font, 8)
        painter.setFont(font)
        fm = QFontMetrics(font)
        verdict = (
            _ink_bad() if data.runs_out_day is not None else _ink_good()
        )
        entries = []
        # 'Remaining' steps aside when the plan has run out: the runs-out
        # marker lands in the same neighbourhood (the line's end, at the zero
        # crossing) and is the more important of the two. The solid line is
        # still identified by elimination — the other two are labelled.
        if data.actual_x and data.runs_out_day is None:
            entries.append((
                "Remaining", data.actual_x[-1], data.actual[-1],
                _ink_remaining(),
            ))
        if data.proj_x:
            entries.append((
                "Projected", data.proj_x[-1], data.proj[-1], verdict,
            ))
        if data.ideal_x:
            entries.append((
                "Plan", data.ideal_x[-1], data.ideal[-1], _ink_plan(),
            ))
        pill = self._today_pill_rect(data, geo)
        placed: list[tuple[float, float]] = []
        for label, day, value, colour in entries:
            x = self._x_to_px(day, chart, x_min, x_span)
            y = self._y_to_px(self._rem(data, value), chart, lo, hi)
            tw = fm.horizontalAdvance(label)
            tx = min(chart.right() - tw - 2, x - tw - 6)
            tx = max(chart.left() + 2, tx)
            ty = y - 6
            # Nudge apart rather than overprint: the projection and the plan
            # both converge on zero at month end, so their labels want the same
            # pixel. Only against labels in the same horizontal neighbourhood —
            # 'Remaining' ends mid-month and never competes with them.
            while any(
                abs(ty - py) < fm.height() and abs(tx - px) < tw + 8
                for px, py in placed
            ):
                ty -= fm.height()
            # ...and duck under the Today pill rather than through it. A scope
            # that has spent nothing keeps Remaining pinned to the top of the
            # chart, which is exactly where the pill lives — so 'Remaining'
            # printed straight over 'Today · 17'. Below the line is free.
            if pill is not None and self._hits(tx, ty, tw, fm, pill):
                ty = y + fm.height() + 2
            placed.append((tx, ty))
            # A surface-coloured plate behind the text: these labels sit at the
            # end of their own line, which means *on* it, and a dashed series
            # running through a word makes both unreadable.
            plate = QRectF(tx - 3, ty - fm.ascent() - 1, tw + 6, fm.height())
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(_alpha(QColor(_ch.chart_surface()), 215)))
            painter.drawRect(plate)
            painter.setPen(QPen(colour))
            painter.drawText(int(tx), int(ty), label)
