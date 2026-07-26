# ADR-184 — The budget Actual drill-down is an editable register

**Status:** Implemented — 2026-07-26

## Context

Owner-reported: on the budget annual screen, double-clicking an **Actual** cell opens the drill-down (ADR-058) listing the transactions behind that cell — but inside that window you can't edit the transactions, and a split's lines aren't visible.

`BudgetDrillDownWindow`'s own docstring says the rows are "an **editable** register … recategorising a transaction here flows straight back through the Repository", and it attaches the register's inline typeahead delegates (payee / category / status). But it was never finished:

- it **never called `setEditTriggers(...)`**, so the table used Qt's defaults, and
- it had **no `doubleClicked` handler** and didn't import the split/investment dialogs.

That is exactly why the two symptoms appear together. Split (and investment) rows are **non-editable inline by design** — `register_model.flags()` returns no `ItemIsEditable` for any `row.split_count`, because a split's parent total and its per-line categories have to change together to keep the sum invariant, so they are edited only through `SplitTransactionDialog`. The register (`register_window.py`) and the shared report drill-down (`TransactionsListWindow`, ADR-147) both route a **double-click on such a row to its detail dialog** — the affordance that makes a split editable and its lines visible. The budget drill-down never got it, so a split row could be neither edited inline nor opened: uneditable, and its splits invisible.

## Decision

**Give `BudgetDrillDownWindow` the same edit affordance the register and the shared drill-down already have.** No new mechanism — the fix is to finish the wiring the window's docstring already promised:

1. Set the register's edit triggers (`DoubleClicked | SelectedClicked | EditKeyPressed`) so plain cash rows edit inline through the delegates that were already attached.
2. Connect `doubleClicked` → `_on_table_double_clicked`, copied from `TransactionsListWindow` (ADR-147): a **split** row opens `SplitTransactionDialog`; an **investment** row opens `InvestmentTransactionDialog`; the Category cell of an inline-categorisable cash action is left to Qt's inline editor (ADR-086); a plain cash row is a no-op (Qt's own double-click edit trigger handles it).
3. On save, reload the model and re-apply the fixed transaction-id set (the drill is a snapshot of one cell, so the id set never changes), then recompute the footer's count + net as a plain sum of the shown rows' amounts — matching how `_drill` computed the cell net.
4. A reconciled-row guard (`_confirm_reconciled_edit`, ADR-040) before opening either dialog, mirroring the other windows.

## Rejected

- **Make split rows editable inline instead.** Rejected for the reason `register_model.flags()` already records: the parent total and per-line categories must change atomically; the split dialog is the one place that holds that invariant. Diverging here would fork the split-edit model.
- **A shared base class for the two drill-down windows.** Tempting given the copied handler, but `BudgetDrillDownWindow` (a fixed txn-id snapshot) and `TransactionsListWindow` (a live filter over period/category/account) have different lifecycles; folding them now would couple two things that are only superficially similar. Left as a possible later refactor.

## Consequences

- The budget drill-down behaves like the register and the report drill-downs: plain rows edit inline, split/investment rows open their dialogs, splits are visible, and edits flow back through the Repository. The budget matrix picks the change up on its next activation refresh, as its docstring already stated.
- Pure UI wiring — no schema/migration, and the perimeter bucketing that builds the id set is untouched.

## Verification

`tests/test_budget_drilldown_editable.py` (3 tests, offscreen): the edit triggers are set (plain rows editable); a split row's double-click routes to `_open_split_txn_dialog` with the split parent's id (its lines become reachable); a plain cash row's double-click opens no dialog (stays inline). Existing budget + drill-down suites pass unchanged. No schema change.
