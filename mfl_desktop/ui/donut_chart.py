"""Two-ring donut (sunburst) chart — Net Worth composition (ADR-067).

Inner ring = account type, outer ring = the individual accounts within each
type. A deliberate, owner-approved **exception to the ADR-018 "no pies"
rule** (see ADR-067): for a point-in-time *composition of a positive whole*
(an asset or debt side of the balance sheet) a two-level donut reads far
better than a stacked bar, and the no-pies rule was aimed at time-series
charts. Negative values can't be a slice, so the caller passes one donut
for assets and another for debts (each over positive magnitudes).

Hand-rolled ``paintEvent`` like every other chart here (ADR-026): flat
fills, thin white separators, a hollow centre showing the side's total, and
a hover tooltip with each slice's amount and share. Angles are measured
**clockwise from 12 o'clock** internally and converted to Qt's
(CCW-from-3-o'clock, 1/16°) convention only at ``drawPie``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import fmt_currency
import mfl_desktop.ui.chart_helpers as _ch
from mfl_desktop.ui.ui_fonts import set_pt


@dataclass(frozen=True)
class DonutChild:
    """An outer-ring slice — one individual account."""
    label: str
    value: float            # >= 0, in the display currency's major units
    color: QColor
    # The account this slice represents (ADR-083 drill-down → its Account
    # Summary). None keeps the slice non-clickable (e.g. a synthetic child).
    account_id: Optional[int] = None


@dataclass(frozen=True)
class DonutSegment:
    """An inner-ring slice — one account type — with its accounts as the
    outer-ring children nested within its angular span."""
    label: str
    value: float            # >= 0; should equal sum(child.value)
    color: QColor
    children: tuple[DonutChild, ...] = field(default_factory=tuple)
    # A drill id for the inner slice itself (ADR-152). None keeps the inner ring
    # non-clickable (the Net Worth sunburst — only its account children drill).
    segment_id: Optional[int] = None


class DonutChart(QWidget):
    """Stateless widget — call :meth:`set_data` to draw, :meth:`show_empty`
    for the no-data state."""

    # Left-click on a slice that carries a drill id → that id. Outer-ring slices
    # carry their DonutChild.account_id (ADR-083, Net Worth → Account Summary);
    # inner-ring slices carry their DonutSegment.segment_id when set (ADR-152,
    # the category sunburst → transactions). Slices with no id aren't clickable.
    account_clicked = Signal(int)

    # Ring geometry as fractions of the outer radius.
    _R_HOLE = 0.40             # centre hole
    _R_MID  = 0.68             # inner/outer ring boundary

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(170, 170)

        self._segments: list[DonutSegment] = []
        self._center_label: str = ""
        self._center_sub: str = ""
        self._symbol: str = "£"
        self._two_ring: bool = True
        self._empty_message: Optional[str] = None
        # (a_start, a_span, r_in, r_out, label, value, pct, account_id) — a in
        # degrees clockwise from 12 o'clock; account_id is None for inner-ring
        # (type) slices, the account id for outer-ring (account) slices.
        self._hits: list[
            tuple[float, float, float, float, str, float, float, Optional[int]]
        ] = []

    # ── public interface ──

    def set_data(
        self,
        *,
        segments: list[DonutSegment],
        center_label: str = "",
        center_sub: str = "",
        symbol: str = "£",
        two_ring: bool = True,
    ) -> None:
        """Draw the donut. ``two_ring`` (default) nests each segment's
        ``children`` as an outer ring (the Net Worth sunburst, ADR-067);
        pass ``two_ring=False`` for a flat single-ring donut where each
        segment is one annulus slice and ``children`` are ignored — used by
        the Income & Expense breakdown, where the slices are already the
        leaves and a second ring would just repeat them (ADR-113)."""
        self._segments = list(segments)
        self._center_label = center_label
        self._center_sub = center_sub
        self._symbol = symbol or "£"
        self._two_ring = two_ring
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._segments = []
        self._empty_message = message
        self.update()

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(_ch.chart_surface()))

        total = sum(max(0.0, s.value) for s in self._segments)
        if self._empty_message is not None or not self._segments or total <= 0:
            self._paint_empty(p, self._empty_message or "No data")
            p.end()
            return

        self._hits = []
        side = min(self.width(), self.height()) - 12
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r_out = side / 2.0
        r_mid = r_out * self._R_MID
        r_hole = r_out * self._R_HOLE
        outer_rect = QRectF(cx - r_out, cy - r_out, 2 * r_out, 2 * r_out)
        mid_rect = QRectF(cx - r_mid, cy - r_mid, 2 * r_mid, 2 * r_mid)
        hole_rect = QRectF(cx - r_hole, cy - r_hole, 2 * r_hole, 2 * r_hole)

        sep = QPen(QColor(_ch.chart_surface()))
        sep.setWidth(1)

        a = 0.0  # degrees clockwise from 12 o'clock
        for seg in self._segments:
            seg_val = max(0.0, seg.value)
            if seg_val <= 0:
                continue
            seg_span = seg_val / total * 360.0
            seg_start = a

            if not self._two_ring:
                # Flat single ring: one annulus slice spanning hole → r_out.
                self._draw_pie(p, outer_rect, seg_start, seg_span, seg.color, sep)
                self._hits.append(
                    (seg_start, seg_span, r_hole, r_out, seg.label, seg_val,
                     seg_val / total, None)
                )
                a += seg_span
                continue

            # Outer ring — the accounts, tiling the segment's span.
            child_total = sum(max(0.0, c.value) for c in seg.children)
            if seg.children and child_total > 0:
                ca = seg_start
                for c in seg.children:
                    cval = max(0.0, c.value)
                    if cval <= 0:
                        continue
                    cspan = cval / child_total * seg_span
                    self._draw_pie(p, outer_rect, ca, cspan, c.color, sep)
                    self._hits.append(
                        (ca, cspan, r_mid, r_out, c.label, cval, cval / total,
                         c.account_id)
                    )
                    ca += cspan
            else:
                self._draw_pie(
                    p, outer_rect, seg_start, seg_span,
                    seg.color.lighter(118), sep,
                )

            # Inner ring — the type. Drawn after, at the smaller radius, so it
            # overpaints the inner portion of the outer pies (annulus effect).
            self._draw_pie(p, mid_rect, seg_start, seg_span, seg.color, sep)
            self._hits.append(
                (seg_start, seg_span, r_hole, r_mid, seg.label, seg_val,
                 seg_val / total, seg.segment_id)
            )
            a += seg_span

        # Punch the centre hole.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_ch.chart_surface()))
        p.drawEllipse(hole_rect)
        self._paint_center(p, cx, cy, r_hole)
        p.end()

    def _draw_pie(
        self, p: QPainter, rect: QRectF,
        a_start: float, a_span: float, color: QColor, sep_pen: QPen,
    ) -> None:
        """Draw a pie wedge. ``a_start`` / ``a_span`` are degrees clockwise
        from 12 o'clock; Qt wants 1/16° CCW from 3 o'clock."""
        qt_start = int(round((90.0 - a_start) * 16))
        qt_span = int(round(-a_span * 16))
        if qt_span == 0:
            return
        p.setPen(sep_pen)
        p.setBrush(color)
        p.drawPie(rect, qt_start, qt_span)

    def _paint_center(
        self, p: QPainter, cx: float, cy: float, r_hole: float,
    ) -> None:
        max_w = int(2 * r_hole - 10)
        if self._center_label:
            f = QFont(p.font())
            set_pt(f, 9)
            p.setFont(f)
            p.setPen(QPen(QColor(_ch.chart_axis_ink())))
            fm = QFontMetrics(f)
            text = fm.elidedText(self._center_label, Qt.ElideRight, max_w)
            p.drawText(int(cx - fm.horizontalAdvance(text) / 2),
                       int(cy - 4), text)
        if self._center_sub:
            f = QFont(p.font())
            set_pt(f, 12)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QPen(QColor(_ch.chart_ink())))
            fm = QFontMetrics(f)
            text = fm.elidedText(self._center_sub, Qt.ElideRight, max_w)
            p.drawText(int(cx - fm.horizontalAdvance(text) / 2),
                       int(cy + fm.ascent()), text)

    def _paint_empty(self, p: QPainter, message: str) -> None:
        f = QFont(p.font())
        set_pt(f, 10)
        p.setFont(f)
        p.setPen(QPen(QColor(_ch.chart_faint())))
        fm = QFontMetrics(f)
        p.drawText(
            int((self.width() - fm.horizontalAdvance(message)) / 2),
            int(self.height() / 2),
            message,
        )

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        if not self._hits:
            super().mouseMoveEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        dx = pos.x() - cx
        dy = pos.y() - cy
        r = math.hypot(dx, dy)
        # Angle clockwise from 12 o'clock: 0 at top, 90 to the right.
        a = math.degrees(math.atan2(dx, -dy)) % 360.0
        for a_start, a_span, r_in, r_out, label, value, pct, account_id in self._hits:
            if not (r_in <= r <= r_out):
                continue
            rel = (a - a_start) % 360.0
            if rel <= a_span:
                text = (
                    f"{label}\n"
                    f"{fmt_currency(value, 2, symbol=self._symbol)}"
                    f"  ·  {pct * 100:.1f}%"
                )
                QToolTip.showText(
                    self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                    text, self,
                )
                if account_id is not None:
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    self.unsetCursor()
                return
        self.unsetCursor()
        QToolTip.hideText()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: D401 — Qt override
        if event.button() != Qt.LeftButton or not self._hits:
            super().mousePressEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r = math.hypot(pos.x() - cx, pos.y() - cy)
        a = math.degrees(math.atan2(pos.x() - cx, -(pos.y() - cy))) % 360.0
        for a_start, a_span, r_in, r_out, _label, _value, _pct, account_id in self._hits:
            if not (r_in <= r <= r_out) or account_id is None:
                continue
            if (a - a_start) % 360.0 <= a_span:
                self.account_clicked.emit(account_id)
                return
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401 — Qt override
        self.unsetCursor()
        QToolTip.hideText()
        super().leaveEvent(event)
