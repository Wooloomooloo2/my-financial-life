# ADR-185 — The Investment Income report is a saved report

**Date:** 2026-07-30
**Status:** Implemented (2026-07-30)
**Builds on:** ADR-108 (Investment Income view — the live window this promotes), ADR-039 (saved-reports framework: `report` table, `filters_json`, New Report dialog, sidebar Reports section), ADR-046 (Investment Returns — the sibling saved report whose Save / Save As / dirty / close-prompt scaffolding this mirrors), ADR-076 (persisted splitter sizes + `report_save.resolve_save_as`), ADR-164 (`report_heading` / `report_folder_name` — the one header-string builder), ADR-055 (display currency is a view preference, not persisted).

---

## Context

Owner-reported: *"The report 'Investment Income' does not seem to have a Save or Save As button, so filters and date ranges do not persist as with other reports."*

This is exactly as designed — and the design was wrong for how the report is used. ADR-108 shipped Investment Income as a **live analysis window**: no saved `report.type`, no migration, an in-memory `IncomeFilters` that lived only for the session (its docstring said so outright), and a Reports-menu entry that opened a single kept-alive singleton. Every *other* report (Spending Over Time, Income Over Time, Income & Expense, Sankey, Category & Payee, and its closest sibling **Investment Returns**) is a saved report: pick a period / accounts / toggles, **Save** or **Save As…**, and the report reappears in the sidebar with that state. Investment Income was the lone exception, so a carefully-filtered income view (say TTM, one ISA account, reinvested-dividends excluded) evaporated on close.

The report is otherwise structurally identical to Investment Returns — same header, same `compute_returns` engine, same filter dialog base, same period vocabulary — so the gap is purely that it was never wired into the ADR-039 framework.

## Options considered

1. **Add ad-hoc "remember last filters" persistence** (e.g. stash the blob in `QSettings`) **vs. make it a real saved report.** → **Saved report.** A one-off QSettings hack would give *one* remembered state, not the named, foldered, multiple-saved-views model every other report already has; it would diverge from the framework instead of joining it, and the owner's words — *"as with other reports"* — ask for parity, not a private shortcut.
2. **Where the persisted filter dataclass lives.** → **In `mfl_desktop.reports.filters`** as `InvestmentIncomeFilters`, beside the other seven, registered in `_FILTER_CLASSES`. ADR-108's `IncomeFilters` (in the pure-aggregation module `reports.investment_income`) is **removed** — persisted filter shapes belong in `filters.py`, and leaving a second, non-persisted copy in the aggregator would be a trap. The aggregator module goes back to pure functions, matching the holdings engine it mirrors.
3. **Keep the singleton live window alongside the saved one vs. retire it.** → **Retire it.** The Reports-menu entry now calls `_open_bare_report(TYPE_INVESTMENT_INCOME)` like every other report (bare-window singleton via `_bare_report_wins`); the bespoke `_investment_income_win` reference and its custom open handler are gone. One code path, not two.

## Decision

**Promote Investment Income to a first-class saved report type**, mirroring Investment Returns field-for-field on the persistence scaffolding.

- **New type `investment_income`.** Added to `REPORT_TYPES` / `REPORT_TYPE_LABELS` ("Investment Income"), and migration **0036** widens the `report.type` CHECK constraint (the SQLite table-recreate dance the earlier widenings used — 0014 / 0023 / 0024 / 0028).
- **`InvestmentIncomeFilters`** (frozen dataclass in `filters.py`): `period_key="1y"` (TTM, the ADR-108 default), `custom_start` / `custom_end`, `account_ids` (empty == whole portfolio), `include_reinvested=True` (ADR-089), plus the ADR-076 `chart_split` / `body_split` splitter sizes. Standard `default()` / `to_json()` / `from_json()` round-trip; registered in `_FILTER_CLASSES`. The **display currency is not persisted** — a view preference re-resolved to the base currency each open (ADR-055), exactly as Investment Returns treats it.
- **Window scaffolding** copied from `InvestmentReturnsWindow`: a `reports_changed` signal; `report: Optional[ReportRow]` constructor arg with `open_bare` / `load_from_id` classmethods; **Save** + **Save As…** ghost buttons in the header; `_dirty` tracking (a filter edit or a splitter drag marks dirty; a currency change does not); `report_heading` for the title/subtitle/window-title (an unsaved report leads with its type, a saved one with its name); `resolve_save_as` for the Save As target resolution; and the unsaved-changes close prompt. A filter edit carries the splitter sizes across the dialog round-trip (the dialog builds a fresh filters that would otherwise reset them).
- **Dispatch:** `new_report_dialog` lists it as available; `register_window._open_bare_report` and `_open_saved_report` gain `investment_income` branches.

## Consequences

- Investment Income now saves, re-opens from the sidebar, supports multiple named views in folders, and prompts on close with unsaved changes — full parity with the other reports; the owner's filtered income view persists.
- **`IncomeFilters` is gone** (moved/renamed to `InvestmentIncomeFilters` in `filters.py`); the filter dialog and window import the new class, and `reports.investment_income` is pure aggregation again. No external caller referenced the old class.
- Old data files auto-upgrade on open via migration 0036; there is nothing to back-fill (there were never any saved Investment Income reports to migrate). The filter blob rides in `filters_json`, so no further schema change is needed for future fields (they add with defaults, per `filters.py`).
- The bespoke singleton path is retired, so the report obeys the same bare/saved-window bookkeeping as the rest.

## Verification

Headless (offscreen): the `InvestmentIncomeFilters` blob round-trips and resolves through `default_filters` / `filters_from_json`; `create_report(type_key="investment_income", …)` is **accepted by the DB** (proving migration 0036 applied and the CHECK constraint was widened); `load_from_id` restores `period_key` / `include_reinvested` into a constructed window; a bare window shows **Save As…** only while a saved window shows **Save** + **Save As…**; `_filters_to_persist` folds the live splitter sizes into the blob. Existing investment-income tests (income-reinvested series, stock-record link, dividend category, drill-down columns) stay green (14 passed).

No behavioural change to the report's numbers, chart, table, drill-down, or currency handling — this ADR is persistence wiring only.
