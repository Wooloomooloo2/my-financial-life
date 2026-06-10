# ADR-056 — Sankey report (income → total → expenses)

**Date:** 2026-06-10
**Status:** Accepted
**Related:** ADR-018 (reports framework, no-pies rule), ADR-026 (paintEvent charts, no QtCharts), ADR-039 (saved-reports framework — `report.type` enum already reserved `sankey`; this lands it), ADR-014 (category `kind` — the income/expense/transfer classification this relies on), ADR-051 (`txn_category_line` split-unrolling view), ADR-030/031 (category rollup helpers).

---

## Context

The owner's favourite finance view: a Sankey where income sources on the left merge into a central **Total**, which fans out to expense categories (nested by hierarchy) plus a **Savings** node. Requested controls: depth (top-level vs more levels), a threshold that hides small slices into "Other", an amounts/% toggle, a summary (income / expenditure / saved / saving %), and timeframes YTD / MTD / Last month / Custom.

`report.type='sankey'` was reserved in ADR-039 but never built (the rendering was the open question). This ADR builds it on the existing saved-report plumbing.

**A data reality shaped the design.** The owner's categories are Banktivity-imported: **all 206 are tagged `kind='expense'` or `transfer` — zero `income`** — and many inter-account transfers were recorded as categories named after the destination account ("Smile Savings GBP", "Chase Checking"). So a Sankey keyed on `kind` shows an empty income side today, and one keyed on transaction *sign* shows £96k of savings-transfers as the top "income source". The owner chose to **fix category kinds manually** (which also unblocks the Budget, ADR-024, that depends on the same field). So the report is built **correct on `kind`** and comes alive as the owner reclassifies — rather than hard-coding around the bad data.

## Decisions

- **Classify by `category.kind`, not sign.** Income = inflows on `kind='income'` categories; expense = outflows on `kind='expense'`; `kind='transfer'` excluded entirely (internal moves are neither). Sign-based was rejected: it can't tell a salary from a savings-transfer, which is exactly the owner's noise. New `Repository.sankey_category_totals(date_from, date_to)` returns `{income, expense}` per-category pence over the period, reading the `txn_category_line` view so splits land on each line's category.
- **Nested classic Sankey** (owner's pick over flat-at-depth). Rendered as a **tree partition**: every node has exactly one parent on its side, so ordering each column by its spine-ward neighbour means ribbons never cross — no crossing-minimisation needed. The central **spine height = max(income, expense)** so both sides fill the canvas at one scale; the shorter side is balanced by a **Savings** node (income > expense) or a **Deficit** node (expense > income). New `mfl_desktop/ui/sankey_chart.py` is a paintEvent widget (ADR-026): cubic-Bézier ribbons (thickness ∝ value), thin node boxes, outer-side labels, hover tooltips.
- **Threshold = % of the side's total** (owner's pick over % of parent): any rolled-up node below the threshold folds into one "Other" sibling at its level. `depth` = how many category levels to expand (roots inherit their colour downward).
- **Amounts / % toggle** changes labels only. **Summary panel**: income, expenditure, amount saved (income − expense), saving % of income.
- **Inline controls, not a modal filter** — a divergence from the Spending report (ADR-039). The owner wants to flip timeframe / levels / amounts-vs-% quickly, so timeframe, levels, "hide below %", and the amounts/% switch live on a control strip; the state still persists as a `SankeyFilters` row (Save / Save As).
- **Timeframes** YTD / MTD / Last month / Custom (finance-native cash-flow presets, distinct from the spending presets). `SankeyFilters` registered in `reports/filters.py`; Sankey made selectable in `NewReportDialog`; dispatched in `register_window` (`_open_bare_report` / `_open_saved_report`) + a Reports-menu entry.

## Consequences

- **The income side stays empty until the owner sets income kinds.** The chart shows a guiding note ("set category kinds in Manage ▸ Categories"). The Change-Kind verb cascades to descendants (ADR-014), so marking a parent income is enough — but the *leaf* categories that actually carry the txns must end up `income` for the inflow to count (the rollup sums same-kind descendants only). Verified by reclassifying a few real income leaves: income £64,150 (Mark Gross Income £51,954, Rental £7,078, Interest £5,117) renders against £64,949 of expense with the right depth/threshold behaviour.
- **Mixed subtrees (payroll).** A Banktivity payroll subtree mixes gross income with tax deductions; once the owner marks the income leaves income and leaves tax leaves expense, the gross shows on the left and taxes become their own top-of-subtree expense roots — the standard gross-vs-net Sankey shape.
- **Multi-currency** is summed as bare pence (no conversion), matching the Spending report; a display-currency pass (like ADR-055 did for Net Worth) is a noted follow-up.
- **Verified** offscreen on the live DB: `SankeyFilters` JSON round-trips; the aggregate scopes by period and excludes transfers; depth 1→3 expands nested columns (19→27 painted nodes) and the threshold folds small slices into "Other"; Savings/Deficit balance the shorter side; amounts and percent modes both paint without error; the report is selectable in New Report and opens bare + saved.
