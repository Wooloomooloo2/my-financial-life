"""Spending Over Time — the first report (ADR-018 / ADR-030 / ADR-039).

Non-modal QMainWindow with a top bar (back / report name / filter / save
verbs), the chart on the left, and a right-side summary panel showing
the period bounds, the filter summary, and the total / average for the
current view.

Filters live in a modal :class:`SpendingFilterDialog` opened by the top-
bar Filter button (ADR-039 follow-up — the always-visible left panel
that round 1 shipped was too dense). Each filter checklist is search-
able with select-all / deselect-all verbs.

Drill-down: clicking a bar segment narrows the filter to that group's
descendant categories and descends the rollup one notch (top → group,
group → leaf, leaf stays). A Back button on the top bar pops the drill
stack and restores the prior filter snapshot.

Pies are deliberately absent (the owner's standing rule). Income and
transfer transactions are excluded by definition — this is a *spending*
report (ADR-014 sign convention).

Renderer is the hand-rolled :class:`SpendingChart` paintEvent widget,
per ADR-026.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_summary import period_bounds, PERIOD_LABELS as _PERIOD_LABELS
from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.reports import (
    category_group_map, category_path, category_root_map,
)
from mfl_desktop.reports.filters import (
    SpendingOverTimeFilters, TYPE_SPENDING_OVER_TIME,
)
from mfl_desktop.ui.chart_helpers import colour_for, legend_chip
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog
from mfl_desktop.ui.spending_chart import SpendingChart
from mfl_desktop.ui.spending_filter_dialog import (
    SpendingFilterDialog, UNCATEGORISED_ID,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.report_save import resolve_save_as
from dataclasses import replace

# Granularity dataclass keys → SQL bucket keys (the SQL side speaks
# "week" / "month" / ...; the dataclass speaks "weekly" / "monthly").
_GRANULARITY_TO_SQL: dict[str, str] = {
    "weekly":    "week",
    "monthly":   "month",
    "quarterly": "quarter",
    "annually":  "year",
}
_GRANULARITY_AVG_WORD: dict[str, str] = {
    "week": "week", "month": "month", "quarter": "quarter", "year": "year",
}

# Period labels reuse account_summary.PERIOD_LABELS (ADR-082, single source).

# Rollup descent ladder used by drill-down. Clicking a top-level segment
# (rollup=top) descends to group; clicking inside a group view descends
# to leaf; leaf stays at leaf and just narrows the category set.
_ROLLUP_DESCENT: dict[str, str] = {
    "top": "group",
    "group": "leaf",
    "leaf": "leaf",
}


def _auto_granularity_for(span_days: int) -> str:
    """Resolve granularity='auto' against a date-span size — mirrors the
    account-summary screen but with no daily bucket (the spending chart's
    stack bars get unreadable at daily granularity)."""
    if span_days <= 90:
        return "week"
    if span_days <= 730:
        return "month"
    if span_days <= 2200:
        return "quarter"
    return "year"


class SpendingReportWindow(QMainWindow):
    """Spending Over Time window — bare or saved-loaded.

    Construct via :py:meth:`open_bare` for an unattached window (the
    Reports menu entry-point) or :py:meth:`load_from_id` for a saved
    report instance (a sidebar click). The two flows are structurally
    identical; only the initial state and the ``_report_id`` differ.
    """

    reports_changed = Signal()

    def __init__(
        self,
        repo: Repository,
        *,
        report: Optional[ReportRow] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._report_id: Optional[int] = report.id if report is not None else None
        self._loaded_name: Optional[str] = report.name if report is not None else None
        self._loaded_folder_id: Optional[int] = (
            report.folder_id if report is not None else None
        )
        self._dirty: bool = False

        # Drill-down: stack of prior filter snapshots. Empty == top-level.
        self._drill_stack: list[SpendingOverTimeFilters] = []

        self.resize(1240, 740)

        # ── reference data ──
        self._all_accounts = repo.list_accounts()
        self._all_categories = repo.list_category_tree()
        self._categories_by_id = {c.id: c for c in self._all_categories}
        self._all_canonical_payees = repo.list_canonical_payees()
        self._rollup_maps: dict[str, dict[int, int]] = {
            "top":   category_root_map(self._all_categories),
            "group": category_group_map(self._all_categories),
            "leaf":  {c.id: c.id for c in self._all_categories},
        }

        # Active filters — either the loaded saved filters, or defaults.
        self._current_filters: SpendingOverTimeFilters = (
            SpendingOverTimeFilters.from_json(report.filters_json)
            if report is not None
            else SpendingOverTimeFilters.default()
        )

        # ── top bar ──
        self._back_button = QPushButton("← Back")
        self._back_button.clicked.connect(self._on_back)
        self._back_button.setVisible(False)

        self._name_label = QLabel()
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")

        self._filter_button = QPushButton("Filter…")
        self._filter_button.clicked.connect(self._on_open_filter)

        self._save_button = QPushButton("Save")
        self._save_button.clicked.connect(self._on_save)
        self._save_as_button = QPushButton("Save As…")
        self._save_as_button.clicked.connect(self._on_save_as)

        top_bar = QWidget()
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(10, 8, 10, 8)
        top_bar_layout.setSpacing(8)
        top_bar_layout.addWidget(self._back_button)
        top_bar_layout.addWidget(self._name_label, stretch=1)
        top_bar_layout.addWidget(self._filter_button)
        top_bar_layout.addWidget(self._save_button)
        top_bar_layout.addWidget(self._save_as_button)

        top_rule = QFrame()
        top_rule.setFrameShape(QFrame.HLine)
        top_rule.setFrameShadow(QFrame.Sunken)
        tokens.themed(top_rule, "color: {border};")

        # ── chart + right summary panel ──
        self._chart = SpendingChart()
        self._chart.segment_clicked.connect(self._on_segment_clicked)
        # The chart's own legend strip elides categories when they
        # don't all fit horizontally. The right summary panel renders
        # them vertically instead (one chip per group, scrollable).
        self._chart.set_show_legend(False)

        self._summary_panel = self._build_summary_panel()

        self._body_splitter = QSplitter(Qt.Horizontal)
        self._body_splitter.addWidget(self._chart)
        self._body_splitter.addWidget(self._summary_panel)
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)
        _bs = self._current_filters.body_split
        self._body_splitter.setSizes(list(_bs) if _bs else [960, 280])
        self._body_splitter.splitterMoved.connect(lambda *_: self._mark_dirty())
        body_splitter = self._body_splitter

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(top_rule)
        central_layout.addWidget(body_splitter, stretch=1)
        self.setCentralWidget(central)

        self._update_name_label()
        self._update_save_buttons()
        self._refresh()

    # ── constructors ──

    @classmethod
    def open_bare(cls, repo: Repository, parent=None) -> "SpendingReportWindow":
        return cls(repo, report=None, parent=parent)

    @classmethod
    def load_from_id(
        cls, repo: Repository, report_id: int, parent=None,
    ) -> Optional["SpendingReportWindow"]:
        report = repo.get_report(report_id)
        if report is None or report.type != TYPE_SPENDING_OVER_TIME:
            return None
        return cls(repo, report=report, parent=parent)

    # ── right-side summary panel ──

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.NoFrame)
        tokens.themed(panel, "QFrame { background: {canvas}; border-left: 1px solid {border}; }QLabel { background: transparent; }")
        panel.setMinimumWidth(240)

        self._period_value = QLabel()
        self._period_value.setWordWrap(True)
        tokens.themed(self._period_value, "color: {text};")

        self._granularity_value = QLabel()
        tokens.themed(self._granularity_value, "color: {text};")

        self._rollup_value = QLabel()
        tokens.themed(self._rollup_value, "color: {text};")

        self._filters_value = QLabel()
        self._filters_value.setWordWrap(True)
        tokens.themed(self._filters_value, "color: {muted_strong};")

        self._total_value = QLabel()
        tokens.themed(self._total_value, "color: {text}; font-size: 22px; font-weight: bold;")
        self._average_value = QLabel()
        tokens.themed(self._average_value, "color: {muted_strong};")
        self._buckets_value = QLabel()
        tokens.themed(self._buckets_value, "color: {muted_strong}; font-style: italic;")

        # Vertical categories legend — scrollable so long lists don't
        # blow out the panel height. Rebuilt on every render to match
        # the chart's current groups (palette comes from chart_helpers
        # so the colours line up exactly with the bar segments).
        self._categories_container = QWidget()
        self._categories_container.setStyleSheet("background: transparent;")
        self._categories_layout = QVBoxLayout(self._categories_container)
        self._categories_layout.setContentsMargins(0, 0, 0, 0)
        self._categories_layout.setSpacing(4)
        self._categories_layout.addStretch(1)

        self._categories_scroll = QScrollArea()
        self._categories_scroll.setWidget(self._categories_container)
        self._categories_scroll.setWidgetResizable(True)
        self._categories_scroll.setFrameShape(QFrame.NoFrame)
        self._categories_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._categories_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff,
        )

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(self._mini_section_title("Period"))
        layout.addWidget(self._period_value)
        layout.addWidget(self._granularity_value)
        layout.addWidget(self._rollup_value)

        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Filters"))
        layout.addWidget(self._filters_value)

        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Summary"))
        layout.addWidget(self._total_value)
        layout.addWidget(self._average_value)
        layout.addWidget(self._buckets_value)

        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Categories"))
        # Stretch on the scroll area so the legend takes whatever
        # remaining vertical space the panel has, scrolling internally.
        layout.addWidget(self._categories_scroll, stretch=1)
        return panel

    def _update_categories_legend(
        self, groups: list[tuple[int, str]],
    ) -> None:
        """Rebuild the vertical legend in the summary panel to match
        the chart's current groups. Colour indices use the same
        palette as the bar segments (see :func:`colour_for`)."""
        # Drop existing chips (everything except the trailing stretch
        # item at index count-1).
        while self._categories_layout.count() > 1:
            item = self._categories_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        insert_at = 0
        for idx, (_gid, name) in enumerate(groups):
            chip = legend_chip(name, colour_for(idx))
            self._categories_layout.insertWidget(insert_at, chip)
            insert_at += 1

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        return lab

    # ── refresh / render ──

    def _refresh(self) -> None:
        filters = self._current_filters
        d_from, d_to = self._resolve_date_bounds(filters)

        sql_granularity = (
            _auto_granularity_for((d_to - d_from).days)
            if filters.granularity == "auto"
            else _GRANULARITY_TO_SQL.get(filters.granularity, "month")
        )

        account_ids = list(filters.account_ids) or [a.id for a in self._all_accounts]
        if not account_ids:
            self._show_empty("Select at least one account.")
            return

        expanded_payee_ids: Optional[list[int]]
        if filters.payee_ids:
            expanded_payee_ids = self._repo.expand_canonical_payee_ids(
                list(filters.payee_ids),
            )
            if not expanded_payee_ids:
                self._show_empty("No transactions match the selected payees.")
                return
        else:
            expanded_payee_ids = None

        rows = self._repo.spending_aggregates(
            date_from=d_from.isoformat(),
            date_to=d_to.isoformat(),
            granularity=sql_granularity,
            account_ids=account_ids,
            include_uncategorised=filters.include_uncategorised,
            payee_ids=expanded_payee_ids,
        )

        rollup_map = self._rollup_maps[filters.rollup_level]
        checked_bucket_ids: Optional[set[int]] = (
            set(filters.category_ids) if filters.category_ids else None
        )
        spending: dict[tuple[int, str], int] = {}
        for r in rows:
            cid = r["category_id"]
            bid = rollup_map.get(cid, cid)
            if (
                checked_bucket_ids is not None
                and bid != UNCATEGORISED_ID
                and bid not in checked_bucket_ids
            ):
                continue
            key = (bid, r["bucket"])
            spending[key] = spending.get(key, 0) + r["spending_pence"]

        self._render(spending, sql_granularity, d_from, d_to, filters)

    def _render(
        self,
        spending: dict[tuple[int, str], int],
        granularity: str,
        d_from: date,
        d_to: date,
        filters: SpendingOverTimeFilters,
    ) -> None:
        buckets = sorted({key[1] for key in spending.keys()})
        if not buckets:
            self._show_empty("No spending in the selected range / filters.")
            return

        group_totals: dict[int, int] = {}
        for (gid, _), val in spending.items():
            group_totals[gid] = group_totals.get(gid, 0) + val
        groups_sorted_ids = sorted(
            group_totals.keys(), key=lambda g: -group_totals[g],
        )
        groups: list[tuple[int, str]] = [
            (
                gid,
                self._categories_by_id[gid].name
                if gid in self._categories_by_id else f"id={gid}",
            )
            for gid in groups_sorted_ids
        ]

        total_pence = sum(spending.values())
        avg_pence = total_pence / len(buckets)
        avg_pounds = avg_pence / 100.0

        self._chart.render(
            buckets=buckets,
            groups=groups,
            spending=spending,
            avg_pounds=avg_pounds,
        )

        self._update_categories_legend(groups)

        self._update_summary_panel(
            filters=filters,
            d_from=d_from,
            d_to=d_to,
            granularity=granularity,
            total_pence=total_pence,
            avg_pounds=avg_pounds,
            bucket_count=len(buckets),
        )

    def _show_empty(self, message: str) -> None:
        self._chart.show_empty(message)
        self._update_categories_legend([])
        self._update_summary_panel(
            filters=self._current_filters,
            d_from=None, d_to=None, granularity=None,
            total_pence=None, avg_pounds=None, bucket_count=0,
            note=message,
        )

    def _update_summary_panel(
        self,
        *,
        filters: SpendingOverTimeFilters,
        d_from: Optional[date],
        d_to: Optional[date],
        granularity: Optional[str],
        total_pence: Optional[int],
        avg_pounds: Optional[float],
        bucket_count: int,
        note: Optional[str] = None,
    ) -> None:
        period_label = _PERIOD_LABELS.get(filters.period_key, filters.period_key)
        if d_from is not None and d_to is not None:
            self._period_value.setText(
                f"{period_label}\n{d_from.isoformat()} → {d_to.isoformat()}"
            )
        else:
            self._period_value.setText(period_label)

        if granularity is not None:
            self._granularity_value.setText(
                f"Granularity: {filters.granularity}"
                + ("" if filters.granularity != "auto" else f" → {granularity}")
            )
        else:
            self._granularity_value.setText(f"Granularity: {filters.granularity}")

        self._rollup_value.setText(f"Rollup: {filters.rollup_level}")

        # Filter summary lines.
        bits: list[str] = []
        bits.append(self._filter_line(
            "Accounts", filters.account_ids, len(self._all_accounts),
        ))
        bits.append(self._filter_line(
            "Categories",
            filters.category_ids,
            self._distinct_bucket_count(filters.rollup_level),
        ))
        bits.append(self._filter_line(
            "Payees", filters.payee_ids, len(self._all_canonical_payees),
        ))
        if not filters.include_uncategorised:
            bits.append("Excluding Uncategorised")
        if self._drill_stack:
            bits.append(f"Drilled in {len(self._drill_stack)} level(s)")
        self._filters_value.setText("\n".join(bits))

        if total_pence is None:
            self._total_value.setText("—")
            self._average_value.setText(note or "")
            self._buckets_value.setText("")
        else:
            total_pounds = total_pence / 100.0
            gran_word = _GRANULARITY_AVG_WORD.get(granularity or "month", "month")
            bucket_word = gran_word if bucket_count == 1 else f"{gran_word}s"
            self._total_value.setText(f"Total: £{total_pounds:,.2f}")
            self._average_value.setText(
                f"Average: £{(avg_pounds or 0):,.2f} / {gran_word}"
            )
            self._buckets_value.setText(f"{bucket_count} {bucket_word}")

    @staticmethod
    def _filter_line(label: str, selected: tuple, total: int) -> str:
        if not selected:
            return f"{label}: all"
        return f"{label}: {len(selected)} of {total}"

    def _distinct_bucket_count(self, rollup: str) -> int:
        rollup_map = self._rollup_maps.get(rollup, {})
        return len({
            rollup_map[c.id] for c in self._all_categories
            if c.kind == "expense" and c.id in rollup_map
            and rollup_map[c.id] != UNCATEGORISED_ID
        })

    def _resolve_date_bounds(
        self, filters: SpendingOverTimeFilters,
    ) -> tuple[date, date]:
        today = date.today()
        if filters.period_key == "custom":
            try:
                if filters.custom_start and filters.custom_end:
                    return (
                        date.fromisoformat(filters.custom_start),
                        date.fromisoformat(filters.custom_end),
                    )
            except ValueError:
                pass
            # Defensive fallback if the saved blob had a malformed custom
            # range (shouldn't happen — dialog validates — but better than
            # crashing the window on open).
            return period_bounds("quarter", today)
        try:
            return period_bounds(filters.period_key, today)
        except ValueError:
            return period_bounds("quarter", today)

    # ── filter dialog ──

    def _on_open_filter(self) -> None:
        dialog = SpendingFilterDialog(
            self._repo,
            current=self._current_filters,
            accounts=self._all_accounts,
            categories=self._all_categories,
            canonical_payees=self._all_canonical_payees,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        new_filters = dialog.values()
        if new_filters is None or new_filters == self._current_filters:
            return
        # Editing filters clears the drill stack — once the user changes
        # the underlying filters, "back" no longer has a coherent prior
        # state to restore to. Matches Banktivity's drill semantics.
        self._drill_stack = []
        self._back_button.setVisible(False)
        self._current_filters = new_filters
        self._mark_dirty()
        self._refresh()

    # ── drill-down ──

    def _on_segment_clicked(self, group_id: int, _bucket: str) -> None:
        """Push current filters onto the drill stack, then narrow the
        category filter to the clicked group's descendants and descend
        the rollup level one notch (so the chart re-renders with the
        clicked group's children as the new stack)."""
        # Clicking the Uncategorised sentinel has no children to drill
        # into — ignore the click so we don't push a pointless snapshot.
        if group_id == UNCATEGORISED_ID:
            return
        descendants = self._repo.category_descendants(group_id)
        if not descendants:
            return
        next_rollup = _ROLLUP_DESCENT[self._current_filters.rollup_level]
        # Filter the descendants down to the bucket-id set that the next
        # rollup level produces — otherwise an inner leaf wouldn't match
        # because the chart aggregates by the rolled-up bucket id.
        next_rollup_map = self._rollup_maps[next_rollup]
        next_bucket_ids = {
            next_rollup_map.get(cid, cid)
            for cid in descendants
            if cid in self._categories_by_id
            and self._categories_by_id[cid].kind == "expense"
        }
        next_bucket_ids.discard(UNCATEGORISED_ID)
        if not next_bucket_ids:
            return

        self._drill_stack.append(self._current_filters)
        self._current_filters = self._with_updates(
            self._current_filters,
            rollup_level=next_rollup,
            category_ids=tuple(sorted(next_bucket_ids)),
        )
        self._back_button.setVisible(True)
        # Drill-down is a view-only change — it doesn't mark the saved
        # report dirty (the user isn't editing the persisted filters).
        self._refresh()

    def _on_back(self) -> None:
        if not self._drill_stack:
            self._back_button.setVisible(False)
            return
        self._current_filters = self._drill_stack.pop()
        if not self._drill_stack:
            self._back_button.setVisible(False)
        self._refresh()

    @staticmethod
    def _with_updates(
        base: SpendingOverTimeFilters, **changes,
    ) -> SpendingOverTimeFilters:
        return SpendingOverTimeFilters(
            period_key=changes.get("period_key", base.period_key),
            custom_start=changes.get("custom_start", base.custom_start),
            custom_end=changes.get("custom_end", base.custom_end),
            granularity=changes.get("granularity", base.granularity),
            rollup_level=changes.get("rollup_level", base.rollup_level),
            category_ids=changes.get("category_ids", base.category_ids),
            include_uncategorised=changes.get(
                "include_uncategorised", base.include_uncategorised,
            ),
            payee_ids=changes.get("payee_ids", base.payee_ids),
            account_ids=changes.get("account_ids", base.account_ids),
            include_transfers=changes.get(
                "include_transfers", base.include_transfers,
            ),
        )

    # ── save / save-as / dirty state ──

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_save_buttons()

    def _persisted_filters(self) -> SpendingOverTimeFilters:
        """The filters to persist on Save: the *base* of the drill stack
        if the user has drilled in, otherwise the active filters. Drill-
        downs are view-only; the saved report keeps the user's chosen
        top-level filters."""
        base = self._drill_stack[0] if self._drill_stack else self._current_filters
        # Fold the live splitter size in so a tuned layout is saved (ADR-076).
        return replace(base, body_split=tuple(self._body_splitter.sizes()))

    def _on_save(self) -> None:
        if self._report_id is None:
            self._on_save_as()
            return
        filters = self._persisted_filters()
        try:
            row = self._repo.update_report(
                self._report_id,
                filters_json=filters.to_json(),
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save report",
                f"The report was not saved:\n\n{e}",
            )
            return
        self._loaded_name = row.name
        self._loaded_folder_id = row.folder_id
        self._dirty = False
        self._update_name_label()
        self._update_save_buttons()
        self.reports_changed.emit()

    def _on_save_as(self) -> None:
        filters = self._persisted_filters()
        dialog = SaveReportAsDialog(
            self._repo,
            initial_name=self._loaded_name,
            initial_folder_id=self._loaded_folder_id,
            title=(
                "Save As…" if self._report_id is not None else "Save report"
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        choice = dialog.values()
        if choice is None:
            return
        try:
            row = resolve_save_as(
                self, self._repo, self._report_id, TYPE_SPENDING_OVER_TIME,
                choice.name, choice.folder_id, filters.to_json(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save report", str(e))
            return
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save report",
                f"The report was not saved:\n\n{e}",
            )
            return
        if row is None:
            return
        self._report_id = row.id
        self._loaded_name = row.name
        self._loaded_folder_id = row.folder_id
        self._dirty = False
        self._update_name_label()
        self._update_save_buttons()
        self.reports_changed.emit()

    def _update_name_label(self) -> None:
        if self._loaded_name is None:
            self._name_label.setText("Untitled Spending Over Time")
            tokens.themed(self._name_label, "color: {muted}; font-style: italic; font-weight: bold; padding: 4px 8px;")
            self.setWindowTitle("Spending Over Time — Untitled")
            return
        prefix = ""
        if self._loaded_folder_id is not None:
            for f in self._repo.list_report_folders():
                if f.id == self._loaded_folder_id:
                    prefix = f"{f.name} / "
                    break
        dirty_mark = "*" if self._dirty else ""
        self._name_label.setText(f"{prefix}{self._loaded_name}{dirty_mark}")
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")
        self.setWindowTitle(
            f"Spending Over Time — {prefix}{self._loaded_name}{dirty_mark}"
        )

    def _update_save_buttons(self) -> None:
        # Bare windows expose a single Save As… verb; the redundant
        # standalone button hides so only one Save As… is visible.
        if self._report_id is None:
            self._save_button.setText("Save As…")
            self._save_button.setEnabled(True)
            self._save_as_button.setVisible(False)
        else:
            self._save_button.setText("Save")
            self._save_button.setEnabled(self._dirty)
            self._save_as_button.setVisible(True)
        self._update_name_label()

    # ── close prompt ──

    def closeEvent(self, event) -> None:
        if self._report_id is not None and self._dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved changes",
                f"‘{self._loaded_name}’ has unsaved changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Save:
                self._on_save()
        super().closeEvent(event)
