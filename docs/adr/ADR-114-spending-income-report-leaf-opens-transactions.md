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
- **Amendment 2 (same day) — double-click opens transactions; single vs double
  disambiguated.** The real report was: double-clicking "Rent for 2023" showed
  the *wrong* transactions (a subset of a different year). Cause: a single
  left-click *drills* (descends the category rollup, re-laying-out the chart),
  and a double-click's **first** press drilled before the **second** click was
  recognised — so the second click landed on a now-different bar (often a
  drilled-into child category), opening a subset. Fix: the chart now defers the
  single-click drill by `QApplication.doubleClickInterval()` and emits a new
  `segment_double_clicked(group_id, bucket)` on a real double-click (timer
  cancelled, so no drill happens). The window opens the **whole clicked
  category (and descendants) for the clicked bucket** on double-click —
  deterministic, no re-layout — while single-click still drills. Cost: a single
  click's drill now lands one double-click-interval later; acceptable for
  reliable double-click semantics, and double-click is the gesture the user
  reached for anyway.
- **Amendment 1 (same day):** scope is the **clicked time bucket**, not the whole
  report period. The first cut opened the full report range, so clicking
  "Rent for 2023" listed Rent for *2007–2025*. The leaf click now resolves its
  bucket key to a date span (`bucket_bounds` from
  `mfl_desktop.reports.income_expense`, which both reports' `_BUCKET_EXPR` feed)
  against the last-render granularity (remembered as `self._last_granularity`),
  **clamped to the report range** so an edge bucket lists only the slice the bar
  actually summed. Unparseable key → falls back to the full report range.
- Inherited by `IncomeReportWindow` for free (it only overrides `_DIRECTION`).
  View layer only; no migration, no schema change.
