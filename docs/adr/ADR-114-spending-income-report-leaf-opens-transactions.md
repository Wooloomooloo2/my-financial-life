# ADR-114 — Spending / Income Over Time: a leaf click opens transactions

**Date:** 2026-06-26
**Status:** Accepted
**Related:** ADR-018 / ADR-030 / ADR-088 (Spending Over Time + its Income mirror — the report this fixes). ADR-083 (drill-a-bar-to-its-transactions, the `TransactionsListWindow` + `TxnListFilter` pattern this reuses, as the Income & Expense and Payee reports already do). ADR-034 §3 (`DrillDownFilterProxy` — the category-and-descendants + period scoping the opened window applies).

## Context

Clicking a bar segment in **Spending Over Time** / **Income Over Time**
descends the category rollup one notch: top → group → leaf. The descent
ladder (`_ROLLUP_DESCENT`) maps **`leaf → leaf`**, so once the view is at the
leaf level a further click just re-narrows the category filter to the same leaf
and pushes another (near-identical) snapshot onto the drill stack. The bar
looks the same, nothing new is revealed, and **the user never reaches the
actual transactions** — they reported it as "it just infinitely drills in a
level without showing the actual transactions." Every other bar-chart report
(Income & Expense, Payee, Net Worth, Investment Returns) drills a terminal
element to a `TransactionsListWindow`; this one had no such exit.

## Decision

In `SpendingReportWindow._on_segment_clicked`, when the clicked segment can't
be broken down any further, **open its transactions instead of re-drilling**.
"Can't be broken down" = the view is already at the `leaf` rollup, **or** the
clicked category has no children (`Repository.category_has_children` is False —
a leaf reached at the top/group level, e.g. a childless top-level category).

New `_open_transactions(category_id)` builds a `TxnListFilter.for_category`
(category-and-descendants) scoped to the report's **resolved date bounds**
(passed as a `custom` period so the list matches exactly what the bars summed)
and the report's account scope (a single selected account → per-account;
all / a subset → the cross-account view, mirroring the Income & Expense and
Payee drills), then shows a `WA_DeleteOnClose` `TransactionsListWindow`.

The Uncategorised and Reinvested-Dividends sentinels are still ignored (no real
category id to scope to); the multi-level descent for categories that *do* have
children is unchanged, as is Back / the drill stack.

## Consequences

- The report is now consistent with the rest of the suite: drill the categories
  down, and the terminal click lands you on the transactions — no dead-end loop.
- A click at the `leaf` rollup always opens transactions, so the (rare) case of
  a tree deeper than three levels shows the leaf-bucket's whole subtree rather
  than drilling further; acceptable and matches the "and descendants" scoping.
- Scope is the **whole report period**, not the clicked time bucket — the
  existing category drill already ignored the bucket, so this keeps the
  behaviour the user's filters describe. Scoping a leaf click to just the
  clicked bucket's span is a possible future refinement.
- Inherited by `IncomeReportWindow` for free (it only overrides `_DIRECTION`).
  View layer only; no migration, no schema change.
