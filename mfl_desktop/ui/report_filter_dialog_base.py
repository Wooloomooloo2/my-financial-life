"""Shared toolkit base for the six report filter dialogs (ADR-084, launch P3a).

A code audit for the 1.0 launch found the report filter dialogs
(`spending` / `income_expense` / `payee` / `category_payee` /
`investment_returns` / `sankey`) were ~40% copy-paste: each re-declared
``_set_combo_to``, ``_initial_custom_dates``, ``_sync_custom_visibility``,
the period-combo + custom-date triad, the granularity option list, the
accounts :class:`CheckListPanel` + all-checked→``[]`` normalisation, and
the button-box / accept scaffold.

This base is a **toolkit, not a rigid layout** (ADR-084 decision): it
offers opt-in builder methods (``_make_period_combo`` /
``_make_custom_dates`` / ``_make_granularity_combo`` /
``_make_transfers_check`` / ``_make_top_n_spin`` / ``_make_accounts_panel``)
plus the shared helpers (``_period_and_custom`` / ``_checked_or_all`` /
``_sync_custom_visibility`` / ``_finalise`` / ``values``). Each subclass
still assembles its own layout and builds its own result object in
``_on_accept`` — so Sankey's category tree, Investment's securities panel,
and Spending's rollup rebuild stay bespoke instead of being forced into a
single fixed layout.

The shared period vocabulary + the date-edit / period-combo factories come
from ADR-082 (`mfl_desktop.periods`, `mfl_desktop.ui.date_widgets`). The
persisted ``filters_json`` keys are untouched by this refactor.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLayout,
    QSpinBox,
    QWidget,
)

from mfl_desktop.account_summary import period_bounds
from mfl_desktop.db.repository import AccountSummary
from mfl_desktop.ui.check_list_panel import CheckListPanel
from mfl_desktop.ui.date_widgets import make_date_edit, make_period_combo

# The one granularity option list (was re-declared in spending +
# income_expense). Label → stored value; "auto" lets the window pick a
# bucket size from the date span (see reports.filters.SPENDING_GRANULARITIES).
GRANULARITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Auto",       "auto"),
    ("Weekly",     "weekly"),
    ("Monthly",    "monthly"),
    ("Quarterly",  "quarterly"),
    ("Annually",   "annually"),
)

# The one "Include transfers" tooltip — was copy-pasted verbatim into the
# income-expense / payee / category-payee dialogs (all share the default-off
# cash-flow semantics + the kind-vs-transfer_id nuance).
TRANSFERS_TOOLTIP = (
    "Transfers between your own accounts are excluded by default.\n"
    "Categories marked 'transfer' are always excluded; this also\n"
    "drops linked transfer pairs filed under other categories."
)


class ReportFilterDialogBase(QDialog):
    """Toolkit base for the report filter dialogs.

    Subclasses call ``super().__init__(parent, title=...)`` then build their
    widgets with the ``_make_*`` helpers, lay them out however they like,
    call :py:meth:`_finalise` with the assembled body, and implement
    :py:meth:`_on_accept` to stash a result in ``self._result``.
    """

    def __init__(self, parent: Optional[QWidget] = None, *, title: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        # Populated by the period/custom-date builders when the subclass
        # opts into a period block. Left as None for period-less dialogs
        # (Sankey) so the shared helpers can guard on presence.
        self._period_combo: Optional[QComboBox] = None
        self._custom_from = None
        self._custom_to = None

        # Built by _make_* on demand; subclasses also add their own.
        self._granularity_combo: Optional[QComboBox] = None
        self._include_transfers_check: Optional[QCheckBox] = None
        self._top_n: Optional[QSpinBox] = None
        self._accounts_panel: Optional[CheckListPanel] = None

        # The subclass's _on_accept stores the chosen filter object here;
        # values() hands it back to the caller. None == cancelled / unset.
        self._result: Any = None

    # ── public API ──

    def values(self) -> Any:
        """The chosen filter object (subclass-specific type), or ``None``
        if the dialog was cancelled."""
        return self._result

    # ── builder methods (opt-in) ──

    def _make_period_combo(
        self, keys: tuple[str, ...], current: str,
    ) -> QComboBox:
        """Build the period-preset combo, store it, and wire it to toggle
        the custom-date edits. Returns the combo for layout."""
        combo = make_period_combo(keys, current=current)
        combo.currentIndexChanged.connect(self._sync_custom_visibility)
        self._period_combo = combo
        return combo

    def _make_custom_dates(
        self,
        period_key: str,
        custom_start: Optional[str],
        custom_end: Optional[str],
    ):
        """Build the From/To custom-range date edits seeded from the current
        period bounds. Returns ``(from_edit, to_edit)`` for layout."""
        cf, ct = self._initial_custom_dates(period_key, custom_start, custom_end)
        self._custom_from = make_date_edit(QDate(cf.year, cf.month, cf.day))
        self._custom_to = make_date_edit(QDate(ct.year, ct.month, ct.day))
        return self._custom_from, self._custom_to

    def _make_granularity_combo(self, current: str) -> QComboBox:
        """Build the granularity combo (auto/weekly/…/annually)."""
        combo = QComboBox()
        for label, value in GRANULARITY_OPTIONS:
            combo.addItem(label, userData=value)
        self._set_combo_to(combo, current)
        self._granularity_combo = combo
        return combo

    def _make_transfers_check(
        self, checked: bool, *, text: str = "Include transfers",
        tooltip: Optional[str] = TRANSFERS_TOOLTIP,
    ) -> QCheckBox:
        """Build the 'Include transfers' checkbox (shared default-off
        cash-flow semantics; the standard tooltip unless overridden)."""
        check = QCheckBox(text)
        check.setChecked(checked)
        if tooltip:
            check.setToolTip(tooltip)
        self._include_transfers_check = check
        return check

    def _make_top_n_spin(
        self,
        value: int,
        *,
        minimum: int = 0,
        maximum: int = 200,
        special_value_text: str = "All",
        tooltip: Optional[str] = None,
    ) -> QSpinBox:
        """Build the top-N spinner (``minimum`` shown as ``special_value_text`` —
        e.g. 0 == 'All')."""
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSpecialValueText(special_value_text)
        spin.setValue(value)
        if tooltip:
            spin.setToolTip(tooltip)
        self._top_n = spin
        return spin

    def _make_accounts_panel(
        self,
        accounts: list[AccountSummary],
        checked_ids: tuple[int, ...],
        *,
        placeholder: str = "Search accounts…",
    ) -> CheckListPanel:
        """Build the Accounts checklist seeded from the saved subset
        (empty tuple == all checked)."""
        panel = CheckListPanel(
            "Accounts",
            [(a.id, a.name) for a in accounts],
            placeholder=placeholder,
        )
        panel.set_checked_ids(checked_ids or None)
        self._accounts_panel = panel
        return panel

    def _finalise(self, body: QLayout | QWidget) -> None:
        """Wrap the assembled body in the standard root layout with an
        OK/Cancel button box, wiring accept → ``_on_accept``."""
        from PySide6.QtWidgets import QVBoxLayout  # local: only needed here

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        if isinstance(body, QWidget):
            root.addWidget(body, stretch=1)
        else:
            root.addLayout(body, stretch=1)
        root.addWidget(buttons)

    # ── shared logic ──

    def _sync_custom_visibility(self) -> None:
        """Enable the custom-date edits only when the 'custom' preset is
        selected. Guarded so period-less subclasses can inherit harmlessly."""
        if self._period_combo is None:
            return
        is_custom = self._period_combo.currentData() == "custom"
        if self._custom_from is not None:
            self._custom_from.setEnabled(is_custom)
        if self._custom_to is not None:
            self._custom_to.setEnabled(is_custom)

    def _period_and_custom(
        self, default_key: str,
    ) -> tuple[str, Optional[str], Optional[str]]:
        """Read the period combo + custom-date edits → ``(period_key,
        custom_start, custom_end)``. Custom dates are only populated for the
        'custom' preset; a from>to fat-finger is swapped silently (the swap
        is what the user wanted — a modal warning every time is worse)."""
        period_key = (
            self._period_combo.currentData() if self._period_combo else None
        ) or default_key
        custom_start: Optional[str] = None
        custom_end: Optional[str] = None
        if period_key == "custom" and self._custom_from and self._custom_to:
            cf = self._custom_from.date()
            ct = self._custom_to.date()
            if cf > ct:
                cf, ct = ct, cf
            custom_start = cf.toString(Qt.ISODate)
            custom_end = ct.toString(Qt.ISODate)
        return period_key, custom_start, custom_end

    @staticmethod
    def _checked_or_all(panel: CheckListPanel) -> list[int]:
        """Return the panel's checked ids, or ``[]`` when everything is
        checked (the saved-filter convention: empty == all)."""
        if panel.is_all_checked():
            return []
        return panel.checked_ids()

    # ── subclass hook ──

    def _on_accept(self) -> None:  # pragma: no cover - overridden
        """Build the result object into ``self._result`` and ``accept()``.
        Subclasses must override."""
        raise NotImplementedError

    # ── shared statics ──

    @staticmethod
    def _set_combo_to(combo: QComboBox, value: str) -> None:
        """Select the item whose ``itemData`` equals ``value``; fall back to
        index 0 when absent."""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    @staticmethod
    def _initial_custom_dates(
        period_key: str,
        custom_start: Optional[str],
        custom_end: Optional[str],
    ) -> tuple[date, date]:
        """Seed the custom-range pickers: the stored custom dates when the
        preset is 'custom', else the current bounds of the selected preset,
        else first-of-month → today as a last resort.

        ``period_bounds`` returns a ``None`` start for the unbounded ``max`` /
        ``all`` presets (e.g. the Investment Returns default) — those get a
        year-to-date seed so the pickers open on a concrete, editable range
        rather than crashing on ``None``."""
        today = date.today()
        if period_key == "custom" and custom_start and custom_end:
            try:
                return (
                    date.fromisoformat(custom_start),
                    date.fromisoformat(custom_end),
                )
            except ValueError:
                pass
        try:
            start, end = period_bounds(period_key, today)
        except ValueError:
            return (today.replace(day=1), today)
        if start is None:
            start = date(today.year, 1, 1)
        return (start, end)
