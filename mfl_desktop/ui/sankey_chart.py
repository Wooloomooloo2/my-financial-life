"""Sankey chart — income → total → expenses flow (ADR-056).

A hand-rolled paintEvent widget (per the ADR-026 no-QtCharts rule). The window
hands in two trees of ``SankeyNode`` — income roots (left of the spine) and
expense roots (right) — already rolled up, depth-limited, and threshold-folded.
This widget does the geometry: assign each node a column by its distance from
the central "Total" spine, pack each column vertically (height ∝ value), and
draw a curved ribbon from every node to its parent (thickness ∝ value). Labels
sit on the outer side of each node; hover shows the exact amount + share.

Layout is a tree partition, not a general Sankey: every node has exactly one
parent on its side, so ribbons don't cross when each column is ordered by its
parent. The spine height is ``max(total_income, total_expense)`` so both sides
fill the canvas at one scale; the shorter side is balanced by a Savings node
(income > expense) or a Deficit node (expense > income).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import fmt_currency
import mfl_desktop.ui.chart_helpers as _ch


@dataclass
class SankeyNode:
    """One box in the diagram. ``value`` is rolled-up pounds. ``children`` are
    the next level down (further from the spine). Layout fields (prefixed ``_``)
    are filled in by the chart."""
    label: str
    value: float
    color: QColor
    children: list["SankeyNode"] = field(default_factory=list)
    is_other: bool = False
    is_balance: bool = False          # a Savings / Deficit balancing node
    _rect: Optional[QRectF] = None
    _col: int = 0
    _parent: Optional["SankeyNode"] = None


_NODE_W = 16.0
_MIN_LINK_W = 70.0      # smallest horizontal span of a ribbon between columns
_MAX_LINK_W = 340.0     # cap so a few-column diagram doesn't get absurdly long
_PAD = 6.0              # vertical gap between sibling boxes
_MARGIN_TOP = 28.0
_MARGIN_BOTTOM = 16.0
_MIN_LABEL_H = 11.0     # don't label boxes thinner than this (use the tooltip)

_INK = "#1f2937"
_MUTED = "#6b7280"


class SankeyChart(QWidget):
    """Renders the income/expense flow. Call ``render(...)`` to (re)draw."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(360)
        self._income: list[SankeyNode] = []
        self._expense: list[SankeyNode] = []
        self._total_income = 0.0
        self._total_expense = 0.0
        self._value_mode = "amount"
        self._symbol = "£"
        self._empty_message = "No data for this period."
        self._spine: Optional[SankeyNode] = None
        self._hitmap: list[tuple[QRectF, SankeyNode, float]] = []

    # ── data in ──

    def render(
        self, *,
        income: list[SankeyNode],
        expense: list[SankeyNode],
        total_income: float,
        total_expense: float,
        value_mode: str = "amount",
        currency_symbol: str = "£",
    ) -> None:
        self._income = income
        self._expense = expense
        self._total_income = total_income
        self._total_expense = total_expense
        self._value_mode = value_mode
        self._symbol = currency_symbol
        self.update()

    def show_empty(self, message: str) -> None:
        self._income = []
        self._expense = []
        self._empty_message = message
        self.update()

    # ── label helpers ──

    def _amount_label(self, value: float, side_total: float) -> str:
        if self._value_mode == "percent":
            pct = (value / side_total * 100.0) if side_total else 0.0
            return f"{pct:.0f}%"
        return fmt_currency(value, symbol=self._symbol)

    # ── layout ──

    def _assign_columns(self) -> tuple[int, int]:
        """Walk both trees, set ``_col`` / ``_parent`` on every node, and return
        (deepest income column magnitude, deepest expense column)."""
        max_in = 0
        max_out = 0

        def walk(node: SankeyNode, col: int, parent: Optional[SankeyNode]) -> None:
            nonlocal max_in, max_out
            node._col = col
            node._parent = parent
            if col < 0:
                max_in = max(max_in, -col)
            elif col > 0:
                max_out = max(max_out, col)
            step = -1 if col <= 0 else 1
            # roots: income go left (-1), expense go right (+1)
            for ch in node.children:
                walk(ch, col + step, node)

        for root in self._income:
            walk(root, -1, self._spine)
        for root in self._expense:
            walk(root, 1, self._spine)
        return max_in, max_out

    def _columns(self, min_col: int, max_col: int) -> dict[int, list[SankeyNode]]:
        """Bucket every node by column, preserving parent grouping order so a
        column reads in the same vertical order as the column nearer the spine
        — which is what keeps the ribbons from crossing."""
        cols: dict[int, list[SankeyNode]] = {c: [] for c in range(min_col, max_col + 1)}
        cols[0] = [self._spine] if self._spine else []
        # Level 1 are the roots (the spine is synthetic and has no children list);
        # deeper columns walk parent.children, ordered by the spine-ward column
        # so the ribbons don't cross.
        if -1 in cols:
            cols[-1] = list(self._income)
        if 1 in cols:
            cols[1] = list(self._expense)
        for c in range(-2, min_col - 1, -1):
            for parent in cols[c + 1]:
                cols[c].extend(parent.children)
        for c in range(2, max_col + 1):
            for parent in cols[c - 1]:
                cols[c].extend(parent.children)
        return cols

    def _layout(self, w: float, h: float) -> Optional[dict[int, list[SankeyNode]]]:
        spine_value = max(self._total_income, self._total_expense)
        if spine_value <= 0:
            return None
        self._spine = SankeyNode(
            label="Total income", value=spine_value, color=QColor(_ch.chart_ink()),
        )
        max_in, max_out = self._assign_columns()
        cols = self._columns(-max_in, max_out)

        draw_h = h - _MARGIN_TOP - _MARGIN_BOTTOM
        # Reserve room for the most-padded column so nothing overflows.
        max_pad = max(
            ((len(nodes) - 1) * _PAD for nodes in cols.values() if nodes),
            default=0.0,
        )
        scale = (draw_h - max_pad) / spine_value
        if scale <= 0:
            scale = draw_h / spine_value

        # Horizontal placement: spread the columns to fill the available width
        # rather than centring a fixed-width band (which left wide empty margins
        # when there were only a handful of columns). Reserve a label gutter on
        # each outer side for the category labels that sit beyond the end
        # columns, then divide the rest among the inter-column links.
        n_slots = max_out + max_in + 1
        gutter = min(max(w * 0.16, 140.0), 300.0)
        band = max(w - 2.0 * gutter, n_slots * _NODE_W + (n_slots - 1) * _MIN_LINK_W)
        if n_slots > 1:
            link_w = (band - n_slots * _NODE_W) / (n_slots - 1)
            link_w = max(_MIN_LINK_W, min(link_w, _MAX_LINK_W))
        else:
            link_w = 0.0
        total_w = n_slots * _NODE_W + (n_slots - 1) * link_w
        # Centre what we laid out — when the per-link cap bites on a wide window
        # with few columns, total_w < band, so re-centre across the full width.
        start_x = max((w - total_w) / 2.0, 8.0)

        def x_of(col: int) -> float:
            return start_x + (col + max_in) * (_NODE_W + link_w)

        for col, nodes in cols.items():
            if not nodes:
                continue
            block = sum(n.value for n in nodes) * scale + (len(nodes) - 1) * _PAD
            y = _MARGIN_TOP + (draw_h - block) / 2.0
            x = x_of(col)
            for n in nodes:
                node_h = max(n.value * scale, 1.0)
                n._rect = QRectF(x, y, _NODE_W, node_h)
                y += node_h + _PAD
        return cols

    # ── paint ──

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("white"))

        self._hitmap = []
        if not self._income and not self._expense:
            self._paint_empty(painter)
            painter.end()
            return

        cols = self._layout(float(self.width()), float(self.height()))
        if cols is None:
            self._paint_empty(painter)
            painter.end()
            return

        # Ribbons first (under the boxes).
        for col, nodes in cols.items():
            if col == 0:
                continue
            self._paint_parent_ribbons(painter, nodes)

        # Boxes + labels.
        for col, nodes in cols.items():
            side_total = self._total_income if col < 0 else self._total_expense
            if col == 0:
                side_total = max(self._total_income, self._total_expense)
            for n in nodes:
                self._paint_node(painter, n, col, side_total)

        # Spine caption.
        if self._spine and self._spine._rect is not None:
            self._paint_spine_caption(painter, self._spine)
        painter.end()

    def _paint_parent_ribbons(self, painter: QPainter, nodes: list[SankeyNode]) -> None:
        """Draw each node's ribbon back to its parent. The parent's facing edge
        is partitioned among its children (contiguous, in column order); the
        child end spans the child's full height."""
        # Group children by parent to allocate contiguous source bands.
        offsets: dict[int, float] = {}
        for n in nodes:
            parent = n._parent
            if parent is None or parent._rect is None or n._rect is None:
                continue
            child = n._rect
            income_side = n._col < 0
            # Parent facing edge x, child facing edge x.
            if income_side:
                px = parent._rect.left()
                cx = child.right()
            else:
                px = parent._rect.right()
                cx = child.left()
            # The spine is parent to BOTH sides; its left edge (income) and
            # right edge (expense) must partition independently.
            okey = (id(parent), income_side)
            off = offsets.get(okey, parent._rect.top())
            src_top = off
            src_bot = off + child.height()
            offsets[okey] = src_bot
            dst_top = child.top()
            dst_bot = child.bottom()

            path = QPainterPath()
            xc = (px + cx) / 2.0
            path.moveTo(px, src_top)
            path.cubicTo(xc, src_top, xc, dst_top, cx, dst_top)
            path.lineTo(cx, dst_bot)
            path.cubicTo(xc, dst_bot, xc, src_bot, px, src_bot)
            path.closeSubpath()
            colour = QColor(n.color)
            colour.setAlpha(115)
            painter.fillPath(path, colour)

    def _paint_node(
        self, painter: QPainter, n: SankeyNode, col: int, side_total: float,
    ) -> None:
        if n._rect is None:
            return
        painter.fillRect(n._rect, n.color)
        self._hitmap.append((QRectF(n._rect), n, side_total))
        if n._rect.height() < _MIN_LABEL_H:
            return
        amount = self._amount_label(n.value, side_total)
        text = f"{n.label}  {amount}" if col != 0 else n.label
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_INK)))
        cy = n._rect.center().y()
        if col < 0:                      # income — label to the left
            r = QRectF(n._rect.left() - 360, cy - 9, 354, 18)
            painter.drawText(r, Qt.AlignRight | Qt.AlignVCenter, text)
        elif col > 0:                    # expense — label to the right
            r = QRectF(n._rect.right() + 6, cy - 9, 360, 18)
            painter.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, text)

    def _paint_spine_caption(self, painter: QPainter, spine: SankeyNode) -> None:
        rect = spine._rect
        font = QFont(painter.font())
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_INK)))
        cap = "Total income" if self._total_income >= self._total_expense else "Total"
        amount = fmt_currency(
            max(self._total_income, self._total_expense), symbol=self._symbol,
        )
        r = QRectF(rect.center().x() - 80, rect.top() - 22, 160, 18)
        painter.drawText(r, Qt.AlignHCenter | Qt.AlignBottom, f"{cap}  {amount}")

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor(_MUTED)))
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, self._empty_message)

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:
        pos = event.position() if hasattr(event, "position") else QPointF(event.pos())
        for rect, node, side_total in self._hitmap:
            if rect.contains(pos):
                pct = (node.value / side_total * 100.0) if side_total else 0.0
                QToolTip.showText(
                    event.globalPosition().toPoint()
                    if hasattr(event, "globalPosition") else event.globalPos(),
                    f"{node.label}\n{fmt_currency(node.value, symbol=self._symbol)}"
                    f"  ·  {pct:.1f}%",
                    self,
                )
                return
        QToolTip.hideText()

    def leaveEvent(self, _event) -> None:
        QToolTip.hideText()
