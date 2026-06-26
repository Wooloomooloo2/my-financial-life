# ADR-113 — Income & Expense report: top-level breakdown donut

**Date:** 2026-06-26
**Status:** Accepted
**Related:** ADR-064 (the Income & Expense report this extends — its right-hand summary panel gains the donut). ADR-067 (narrows the ADR-018 no-pies rule: a donut is allowed for a *point-in-time composition of a single positive whole* — exactly the case here). ADR-018 (no-pies rule for time-series / cross-series comparison — untouched; the time-series stays a bar+line chart). ADR-056 (Sankey — the category-kind income/expense definition + display-currency pass this reuses via `sankey_category_totals`). ADR-055 (display-currency policy — exclude no-rate, never par-add). ADR-051 (`txn_category_line` split-unrolled view the totals aggregate over).

## Context

The owner asked for a composition view in the otherwise-empty bottom-right
corner of the Income & Expense report: a pie showing the breakdown of income,
with a toggle to switch to the breakdown of expense, over the report's period
— **top-level categories only**.

A raw pie would collide with the long-standing no-pies rule (ADR-018), but
ADR-067 already carved out the exact shape we need: a **donut** is sanctioned
for a *point-in-time composition of a positive whole*. An income (or expense)
breakdown over a fixed period is precisely that — a single non-negative total
sliced by category — and the report already owns a hand-rolled, reusable
`DonutChart` (ADR-067, Net Worth). So this is the blessed donut case, not a
re-litigation of no-pies: the time-series half of the report stays a bar+line
chart.

## Decision

- **Data — reuse the Sankey aggregate.** The donut is fed by
  `Repository.sankey_category_totals` (the same category-kind cash-flow
  convention as the headline figures: income = inflows on income-kind
  categories, expense = outflows on expense-kind categories, transfers
  excluded), scoped to the report's resolved date bounds, account filter,
  category filter, and display currency. No new query path; no schema change.
- **Roll up to top level.** New pure helper
  `mfl_desktop.reports.income_expense.compose_top_level(leaf_pence, parent_of,
  name_of, *, top_n=8)` walks each leaf category id up the parent map to its
  top-level ancestor, accumulates pence there, and returns
  `CompositionSlice` rows (label / Decimal major-unit value / top-level
  category id) sorted largest-first. Everything past `top_n` folds into a
  single `None`-id **"Other"** slice so the donut never grows a tail of
  slivers; zero/negative totals are dropped. Pure, Qt-free, dates/IDs injected
  — verifiable offscreen like its sibling functions.
- **UI — bottom of the summary panel.** `IncomeExpenseWindow` gains a
  **BREAKDOWN** section pinned to the bottom of the right-hand panel (below an
  existing stretch): an **Income / Expense** segmented toggle (the same
  pill-button style as the Account Summary period selector) over a `DonutChart`.
  Both sides are computed once per refresh and cached
  (`self._comp_slices`), so the toggle re-renders instantly without a
  re-query.
- **Flat single ring + legend, not a centre total.** The slices are already
  the leaves (top-level categories), so the `DonutChart` sunburst's second
  ring would just repeat them — `set_data(..., two_ring=False)` (new flag,
  default keeps Net Worth's two-ring behaviour) draws each segment as one
  annulus slice. The centre is left empty: the headline `Income:` / `Expense:`
  figures sit directly above in the same panel, so a centre total is
  redundant (and was getting elided to `£193,…` in the narrow hole anyway).
  Underneath the donut a compact **legend** lists each slice — colour swatch ·
  category name · whole-pound amount — rebuilt per render; each slice is a
  `GROUP_PALETTE` colour (`chart_helpers.colour_for`) and the donut keeps its
  hover tooltip (amount + share). Empty range / no-data shows the donut's own
  empty state and an empty legend.
- **View layer only.** No migration, no repository change, no new dependency.

## Consequences

- The corner that was blank now carries the composition the bar chart can't
  show, and the report answers both "how did cash-flow trend?" (bars+line) and
  "what made it up?" (donut) without leaving the window.
- Slices are clickable-looking only via hover for now — no per-slice drill-down
  (the bars already drill to transactions per ADR-083). A category drill from a
  slice is a natural follow-up if asked.
- The donut total can differ slightly from the headline when transfers are
  *included* (`include_transfers=True`): `sankey_category_totals` always
  excludes transfer-kind lines and has no include-transfers knob, whereas the
  headline can count transfer-id legs filed under income/expense categories.
  Acceptable for a composition view; revisit if the discrepancy ever surfaces.
- "Top level only" is literal: a file whose income all hangs under one
  top-level `Income` node shows a single slice (e.g. the public demo). The
  owner's real tree has many top-level categories (Bills, Personal, Fees, …),
  where the breakdown is rich.
