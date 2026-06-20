# ADR-088 — Income Over Time report + category filter on Income & Expense

**Date:** 2026-06-20
**Status:** Accepted.
**Builds on:** ADR-018 / ADR-030 / ADR-039 (Spending Over Time + saved-report framework), ADR-014 (category kinds), ADR-064 (Income & Expense report), ADR-084 (shared report-filter base). **Amends:** ADR-064 (adds a category filter dimension).

---

## Context

Two requests:

1. **Income Over Time** — a report identical to *Spending Over Time* but
   focused on income. Spending Over Time is the time-bucketed stacked-bar
   report with period / granularity / rollup / drill-down, a filter dialog,
   and saved-report persistence. The owner wanted the same thing for the
   income side.
2. **Category filter on Income & Expense** — the cash-flow report (ADR-064)
   could be narrowed by account and transfers but not by category.

The Spending report counts **strict outflow** (`kind='expense'` AND
`amount < 0`, ADR-018). The income mirror is the symmetric **strict inflow**
(`kind='income'` AND `amount > 0`). Everything else — the chart, the rollup
maps, the drill-down, the filter checklists, the save/load flow — is
kind-agnostic.

## Decision

### 1. Income Over Time — one window, parameterised by direction

Rather than duplicate ~1,400 lines across three files, the
`SpendingReportWindow` machinery is generalised over a small frozen
`_Direction` descriptor capturing the only things that differ between the
variants:

- `kind` — the category kind that counts (`expense` / `income`)
- `type_key` / `type_label` — saved-report discriminator + on-screen name
- `noun` — for the empty-state line ("No income in…")
- `filters_cls` — `SpendingOverTimeFilters` / `IncomeOverTimeFilters`
- `aggregate_method` / `value_key` — the Repository method + result key

The expense report uses the default `_DIRECTION`; `IncomeReportWindow` is a
**thin subclass** that sets `_DIRECTION = _INCOME_DIRECTION` and inherits
everything else. The shared `open_bare` / `load_from_id` classmethods read
`cls._DIRECTION`, so `load_from_id` only accepts a saved report of its own
type. `_with_updates` switched from an explicit constructor to
`dataclasses.replace`, which preserves the concrete filter subclass.

New pieces:
- **`IncomeOverTimeFilters`** — a frozen subclass of `SpendingOverTimeFilters`
  with the identical field shape; distinct only in type so the saved-report
  dispatch (`_FILTER_CLASSES`, the window loader) tells them apart.
- **`Repository.income_aggregates`** — mirror of `spending_aggregates`:
  `kind='income' AND amount > 0`, `SUM(t.amount)`, reading the split-unrolled
  `txn_category_line` view (ADR-051), returning
  `{bucket, category_id, income_pence}`.
- **`SpendingFilterDialog`** gained a `kind` (+ `title`) parameter: the
  category checklist filters by that kind, and the **Include Uncategorised**
  toggle is hidden for income — the reserved Uncategorised category (id=1) is
  `kind='expense'` (ADR-014 / 0002), so it can never appear in an income
  aggregation. The field stays on the dataclass for shape symmetry but is
  inert for income.
- Wiring: a new report type `income_over_time` (migration **0028** widens the
  `report.type` CHECK, following the 0014/0023/0024 table-recreate pattern), a
  Reports-menu entry, the New Report catalog, and the bare/saved dispatch in
  `register_window`.

Why a subclass over a `kind='income'` flag on the expense report: the report
**type** must be distinct anyway (separate saved reports, separate sidebar
rows, separate CHECK value). Given that, a one-line subclass keyed on
`_DIRECTION` is the least-surface way to get a second type with zero logic
duplication.

### 2. Category filter on Income & Expense

- `IncomeExpenseFilters` gains `category_ids: tuple[int, ...]` (empty = all,
  the shared convention).
- The filter dialog gains a **Categories** checklist over the income + expense
  categories (transfers excluded — never income or expense), full breadcrumb
  labels (ADR-031).
- `income_expense_series` gains a `category_ids` parameter adding a
  `t.category_id IN (…)` clause. The **kind rule still decides** income vs
  expense; the category filter only narrows *which* categories feed the totals.
- The window **expands each picked category to its subtree**
  (`category_descendants`) before querying, so selecting a parent (e.g.
  "Expense") naturally pulls in its children. The summary panel shows a
  "Categories: N of M" line.

## Consequences

- A future third over-time variant (e.g. transfers) is another `_Direction`
  + thin subclass, no further machinery.
- The two over-time reports cannot drift: they share one window, chart, filter
  dialog, and drill-down. The expense report's behaviour is unchanged
  (verified: identical spending totals and an always-visible Uncategorised
  toggle).
- Saved Spending reports are unaffected — the new type and field are additive,
  and old blobs round-trip through the same defaults-tolerant loader.
- Verified headless against a copy of the live `mfl_dev.mfl`: `income_aggregates`
  returns real income series, the Income window renders, the I&E category
  filter narrows the expense total, and the Uncategorised toggle hides for
  income only.
