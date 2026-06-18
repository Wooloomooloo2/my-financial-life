# ADR-083 — Report drill-down completion: every report reaches the ledger

**Date:** 2026-06-18
**Status:** Accepted
**Related:** `docs/RELEASE_1.0_BACKLOG.md` workstream **P2** ("make every report clickable & cross-linked"), ADR-034 (the `TransactionsListWindow` drill target + window policy), ADR-066/ADR-068 (Payee / Category-&-Payee drills that already reach it), ADR-064 (Income & Expense), ADR-056 (Sankey), ADR-046 (Investment Returns), ADR-067 (Net Worth donut), ADR-058 (Budget drill-down).

---

## Context

A 1.0-polish audit (launch-plan **P2**) found the reports split between **drill-capable** and **dead-end**:

- **Already drill** → the shared `TransactionsListWindow` (ADR-034): Spending Over Time, Payee, Category & Payee, Account Summary. Budget drills to its own bespoke `BudgetDrillDownWindow` (ADR-058). Home navigates.
- **Dead-ends** (a chart you can look at but not click *through*): **Sankey**, **Income & Expense**, **Investment Returns**, **Net Worth**.

A report that can't reach the transactions behind a number is a worse tool — you see a £2,320 expense month or a SABRA dividend total and can't get to the rows. P2 closes that for the four dead-ends, reusing the existing drill target so the experience is uniform.

`TransactionsListWindow` already filters by **account / category-descendants / payee(-set / null) / period**. The four dead-ends need two new filter dimensions it didn't have:

- **cash-flow kind** (Income & Expense: a bar is "income" or "expense", not a single category), and
- **security** (Investment Returns: a row is one security's activity).

### Owner forks (`AskUserQuestion`, 2026-06-18)
1. **Income & Expense drill** → *that month's income/expense transactions* (one click to the ledger), not an intermediate category breakdown and not only the combined bar.
2. **Net Worth donut** → an outer-ring account slice opens *that account's Account Summary page* (the richer single-account view), **not** the flat register.
3. **Scope** → do all agreed reports in **one arc** (this ADR), rather than shipping incrementally.

---

## Decision

### Two new `TxnListFilter` dimensions + proxy filters (the shared target stays the target)
`TxnListFilter` (in `transactions_list_window.py`) gains:
- `kind` / `kind_label` + factory **`for_kind(...)`** — Income & Expense.
- `security_id` / `security_label` + factory **`for_security(...)`** — Investment Returns.

`DrillDownFilterProxy` gains `set_kind_filter(kind, category_ids)` and `set_security_id(sid)`:
- **kind**: a row passes only when it's non-transfer (`transfer_id IS NULL`), on a category of that kind (the window resolves the kind's category-id set via `Repository.list_categories_flat(kinds=(kind,))`, keeping the proxy repo-free), and its sign matches (`income → amount > 0`, `expense → amount < 0`). This is the **same definition** as `Repository.income_expense_series`, so the drill reconciles with the bar.
- **security**: `row.security_id == sid`.

The kind and security chips are **non-removable** (like the Period chip) — they are the drill's defining dimension; the window's `signature()` includes both so distinct drills are distinct windows (ADR-034 policy). The window construction, period selector, inline-edit delegates, and footer are all unchanged.

### Per-report wiring (each emits a click signal; its window opens the drill)
- **Sankey** (`sankey_chart.py` / `sankey_report_window.py`): `SankeyNode` gains `category_id` (set on real category nodes; `None` for the synthetic *Other* fold and the *Savings/Deficit* balance nodes, which aren't clickable). Chart emits `node_clicked(category_id, label)` on a left-click hit-test → window opens `for_category` (category **and its descendants**, matching the ribbon's rolled-up value) over the report's period + account scope.
- **Income & Expense** (`income_expense_chart.py` / `income_expense_window.py`): the chart's existing `(rect, kind, bucket_index)` hitmap drives a new `segment_clicked(kind, bucket_key)`. A new pure helper **`income_expense.bucket_bounds(key, mode)`** inverts the bucket key to its `(start, end)` calendar span (year/quarter/month exact; week per SQLite's `%W` Monday-first convention). The window resolves it against the last-rendered granularity and opens `for_kind`.
- **Investment Returns** (`investment_returns_window.py`): the chart is **portfolio-level** (a value-over-time composition), so the per-security **table** is the drill source, not the chart. Each row stashes its `security_id` on the first cell (`_SID_ROLE = Qt.UserRole + 1`, off the sort-key role); double-click → `for_security` over the report's resolved window + account scope.
- **Net Worth** (`donut_chart.py` / `net_worth_window.py` / `register_window.py`): `DonutChild` gains `account_id`; the donut's hit tuple carries it (`None` for inner-ring type slices); chart emits `account_clicked(account_id)` for outer-ring slices. The Net Worth window re-emits it as `account_activated(int)`, which `register_window` connects to its canonical **`_open_account_summary`** (single-instance per account) — so a slice opens the same Account Summary window the sidebar would, per owner fork 2.

### Budget — already satisfied (no change)
The budget matrix already double-click-drills an Actual cell into `BudgetDrillDownWindow` — an **editable transactions register** built from the exact txn-id set the cell aggregates (nearest-budgeted-ancestor + transfer-cancellation + the Unbudgeted case). The envelope→ledger gap is already closed. We deliberately **keep the bespoke window** rather than route Budget through the shared `TransactionsListWindow`: the shared window's category/payee/kind/date dimensions can't reproduce the perimeter bucketing, so a switch would *lose* the exact reconciliation that makes the budget drill trustworthy.

### Account scope (uniform across the new drills)
A single selected account drills **per-account**; the all-accounts or a >1 subset case opens the **cross-account** view (`account_id=None`). A subset slightly over-includes — the drill can't represent an account subset — the same documented trade-off as the Payee report (ADR-066).

---

## Consequences

### Positive
- **Every report now reaches the ledger.** Sankey node, income/expense bar, security row, and net-worth account slice are all clickable; the first three land on the shared `TransactionsListWindow` with consistent chips/period/edit behaviour, the fourth on the canonical Account Summary.
- **One drill target, two small new dimensions** — no bespoke per-report transaction views (Budget's pre-existing one excepted, with reason).
- **Reconciliation by construction**: the kind filter mirrors `income_expense_series`'s exact predicate, so an Income/Expense drill sums to the bar.
- **Verified** offscreen on a seeded DB: IE income-bar → only the income row; IE June-expense → the June expense (not May); 2026 expense → both; security drill matches only AAPL (excludes TSLA); Investment-Returns row double-click → that security's row; Net-Worth slice → `account_activated(account_id)`; `bucket_bounds` round-trips month/quarter/year/week; the donut paints the new 8-tuple hits; whole-app import + py_compile clean.

### Negative / trade-offs
- **Split fidelity**: the kind drill matches on a row's own `category_id`, so a *split* transaction whose lines carry the kind's categories (row `category_id` is the parent/None) is under-included — the same split limitation the Spending/Payee drills already carry. Noted, not fixed here.
- **`include_transfers`**: the Income & Expense drill always excludes transfers (the kind filter forces `transfer_id IS NULL`), matching the report's default. A report viewed with `include_transfers=True` won't have its transfer legs appear in the drill. Edge case; deferred.
- **Archived categories**: `list_categories_flat` returns only active categories, so a transaction filed under an *archived* income/expense category is missed by the kind drill. Rare; deferred.
- **Non-removable kind/security chips**: you can't widen a kind/security drill in place (unlike category/payee chips). Deliberate — the dimension is the drill's identity; a generic list is reachable elsewhere.

### Ongoing responsibilities
- A new report that wants to drill emits a click signal carrying the identifying id(s) and opens `TransactionsListWindow` via the matching `TxnListFilter` factory (`for_category` / `for_payee(s)` / `for_kind` / `for_security`) — never a bespoke transactions window unless, like Budget, it needs bucketing the shared filters can't express.
- New `TxnListFilter` dimensions extend `DrillDownFilterProxy.filterAcceptsRow` and `signature()` together.
