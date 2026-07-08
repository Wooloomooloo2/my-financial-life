# ADR-147 — Report drill-down honours a multi-account subset scope (and stays editable)

**Date:** 2026-07-08
**Status:** Implemented
**Related:** ADR-083 (report → transactions drill-down, `TxnListFilter` / `TransactionsListWindow`). ADR-146 (Cash Flow / Sankey report, the report that surfaced the bug). ADR-051 (split transactions + `txn_category_line` split-unroll — split lines carry the parent txn's `account_id`). ADR-048 (investment transaction dialog). ADR-086 (investment Category cell inline-editable). ADR-034 §3 (single-window-per-filter registry keyed on `TxnListFilter.signature`).

## Context

A report can be scoped to a **subset** of accounts — e.g. a "rental" Cash Flow (Sankey) report over two rental accounts, deliberately excluding a personal American Express card. The report's aggregate is correctly account-filtered: `sankey_category_totals` (like every report query) applies `t.account_id IN (…)`, so the diagram's "Interest Exp" slice sums only the rental accounts.

But **clicking** that slice leaked. The drill-down window (`TransactionsListWindow`) only ever carried a *single* `account_id`, or `None` for cross-account — it had no notion of "this specific set of accounts". So `SankeyReportWindow._on_node_clicked` collapsed any multi-account scope to `account_id=None`:

```python
if len(acc_ids) == 1:
    account_id = acc_ids[0]        # per-account drill
else:
    account_id, account_name = None, ""   # 0 OR a subset → ALL accounts
```

With the rental report (two accounts) the `else` branch fired, and the "Interest Exp" drill opened a list of **every** account's interest — including the American Express interest the report itself excluded. The number in the diagram and the rows behind it disagreed.

This was latent in **every** report that shares the drill-down (Payee/Category, Income & Expense, Spending Over Time, Investment Returns, Investment Income): each used the identical `len(acc_ids) == 1 else None` collapse, so any subset-of-accounts scope drilled cross-account. The Sankey/rental case is just where the owner hit it.

A second, related gap: once the drill-down opened, the rental "Interest Exp" rows were **splits** (ADR-051), and split rows are non-editable inline. Unlike the register, the drill-down wired **no double-click → detail dialog**, so a split (or investment) row in a drill-down could only be viewed, never edited — the owner had to hunt it down in the account register to change it.

## Decision

**1. Thread the report's full account selection into the drill-down and filter to it**, instead of collapsing a subset to cross-account.

- **`TxnListFilter`** gains `account_ids: tuple[int, ...] = ()` (empty == no subset) and `account_ids_label: str = ""` (the chip caption, e.g. "3 accounts"). Both are optional with defaults, so every existing caller and saved-window signature is unchanged. `account_ids` joins `signature()` so a subset scope opens its own drill-down window, distinct from the cross-account one. All the `for_*` factory methods accept the two fields.
- **`DrillDownFilterProxy`** gains `set_account_ids(set|None)` and a `filterAcceptsRow` clause rejecting any row whose `account_id` isn't in the set. `TransactionRow` already carries `account_id`, so this is a pure row-predicate — no new query.
- **Model loading**: a subset loads the model **cross-account** (`account_id=None` → `list_all_transactions`) and the proxy narrows it. This reuses the existing all-transactions layout, so the drill naturally shows the **Account** column — a bonus for a multi-account view, letting the owner see which rental account each line belongs to. (A single account keeps the per-account model + running-balance column, unchanged.)
- **Chip**: the subset renders as a removable chip mirroring the single-account chip. Its × (`_on_remove_account_subset`) clears the proxy filter to widen back to every account — no model rebuild needed, since it's already cross-account.
- **Shared helper** `drilldown_account_scope(account_ids, name_for)` resolves a report's selection into `(account_id, account_name, account_ids, account_ids_label)` — one account → per-account; several → subset + "{n} accounts"; none → cross-account. **All six drill callers** (Sankey, Spending, Income & Expense, Category/Payee, Investment Returns, Investment Income) route through it, replacing the duplicated collapse. Account Summary is intrinsically single-account (`self._account.id`) and is untouched.

**2. Make dialog-edited rows editable from the drill-down.** `TransactionsListWindow` now connects `doubleClicked` → `_on_table_double_clicked`, mirroring the register: a split row opens `SplitTransactionDialog`, an investment row opens `InvestmentTransactionDialog`, and a plain cash row falls through to Qt's inline editor (no-op here). The account is resolved from the row (`seed.account_id`), so it works in single-account, cross-account, and subset views alike; a reconciled row gets the same "change anyway?" confirm as an inline edit; on save the model reloads and the filter/footer refresh. Inline editing (payee/category/status/memo/amount/date) already worked cross-account — `setData` keys off `row.id`, independent of account scope — so no change was needed there.

Rejected: loading a bespoke multi-account model query (the cross-account model + a proxy predicate is simpler and reuses the Account-column layout); showing individual account chips (one "{n} accounts" chip is compact and the Account column already identifies each row); silently keeping cross-account behaviour (the whole point is that the drill must reconcile with the account-filtered total); leaving split/investment rows read-only in the drill-down (the owner expects to edit what they drilled into, and the register already had the dialogs).

## Consequences

- Clicking "Interest Exp" (or any node/bar/row) on a report scoped to a subset of accounts now lists **only** those accounts' transactions — the out-of-scope American Express interest is gone, and the list total reconciles with the report figure. This holds across the Cash Flow, Spending, Income & Expense, Category/Payee, and both Investment reports.
- Double-clicking a split or investment row in any drill-down opens its edit dialog, so a drilled transaction is editable in place instead of only in the account register.
- A subset drill shows the cross-account column layout (Account column, no running balance) — correct for a multi-account view, and consistent with removing the single-account chip.
- No schema change or migration. `tests/test_drilldown_account_subset.py` 9/9 (subset excludes the out-of-scope account; no-subset stays cross-account; clearing the subset widens back; `for_category` threads `account_ids` and distinguishes the signature; the single-account path keeps an empty subset; the scope helper's one/several/none cases; a split row's double-click routes to the split dialog). `test_sankey_transfers.py` 7/7, `test_drilldown_investment_columns.py` 4/4, and the Income & Expense / Investment report suites unaffected.
