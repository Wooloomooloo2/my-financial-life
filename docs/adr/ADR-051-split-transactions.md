# ADR-051 — Split transactions

**Date:** 2026-06-10
**Status:** Accepted
**Related:** ADR-010 (signed `txn.amount` in pence; the Uncategorised sink at id 1), ADR-014 (category `kind` — split lines exclude transfer-kind categories), ADR-018/030 (Spending Over Time — a category consumer that must unroll), ADR-024 (budget perimeter/actuals — another category consumer), ADR-033/034 (per-account summary Top Categories — the third consumer), ADR-038 (Banktivity sign-over-Type — the importer this extends), ADR-040 (statement reconciliation — unaffected because the parent keeps the total), ADR-048 (investment dialog — the self-persisting dialog pattern reused here; investment rows get no splits), ADR-020/036 (transfers — explicitly mutually exclusive with splits in v1).

---

## Context

The owner's Banktivity data contains **split transactions**: one payee / date / total whose total is divided across several categories (a £80 shop = £55 Groceries + £18 Household + £7 Cashback-style refund). Banktivity exports a split as a parent CSV row with `Category/Account = "(split)"` and the total, followed by sub-rows (empty Type/Status/Date/Payee) each carrying a category and a **signed** amount.

The importer threw that detail away. `csv_parser._collapse_banktivity_splits` flattened the sub-rows into the memo (`"Split: Groceries (-£55) | …"`) and filed the parent under Uncategorised. So every split landed as one uncategorised row, and the Spending report, budget actuals, and Top-Categories all mis-attributed it. There was also no way to *create* or *edit* a split in the register.

The whole app is built on **one row → one category → one amount**: the register is a flat `QTableView`, `txn.category_id` is `NOT NULL`, and a dozen queries read `txn.category_id`/`txn.amount` per row. Splits break that assumption, so the design question was where to absorb the break with the least blast radius.

The owner confirmed scope up front (via AskUserQuestion): **cash / bank / credit accounts only** (investment rows keep the ADR-048 dialog, no per-line splits); a split shows as a **single register row** reading **"—Split—"** with a dialog editor (not inline child rows — that churn is what ADR-042 warned against); creatable **at import, in New Transaction, and on edit**.

---

## Options considered

**(A) Child `txn` rows linked by a parent id.** Each line is a `txn` row; the parent holds the total. Rejected: every `SUM(txn.amount)` query (balance, `balance_as_of`, reconciliation residual, holdings, net worth, sidebar) would double-count parent + children, so *all* of them would need a "exclude parents or children" clause — a huge, error-prone blast radius on the most-load-bearing queries.

**(B) Splits as JSON on the txn.** A `splits` JSON column. Rejected: not queryable — the category consumers (SQL `GROUP BY category_id`) couldn't unroll it without per-row JSON parsing, and it duplicates amounts off the relational model.

**(C) Separate `txn_split` child table; the parent keeps the full signed total.** Chosen. The money layer reads the parent total and is **completely untouched**; only **category attribution** unrolls a split into its lines. The break is absorbed in exactly three queries, behind one SQL view.

The key realisation: a split changes *category attribution*, not *money movement*. The parent transaction still moved £80 against the account on one date — reconciliation, balances, and net worth only care about that. So keep the £80 on the parent and put the category breakdown in a child table.

---

## Decision

### Schema — migration `0017_split_transactions.sql`
```sql
CREATE TABLE txn_split (
    id          INTEGER PRIMARY KEY,
    txn_id      INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category(id),   -- mirrors txn.category_id: no ON DELETE action
    memo        TEXT,
    amount      INTEGER NOT NULL,                           -- SIGNED pence, same convention as txn.amount
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_txn_split_txn ON txn_split(txn_id);
```
- **Invariant:** `SUM(txn_split.amount WHERE txn_id = X) == txn.amount`, enforced at every write (`Repository._replace_split_lines` raises on mismatch — integer pence, so the check is exact, no float tolerance). A txn **is a split** iff it has ≥1 `txn_split` row; its own `txn.category_id` then stays at the Uncategorised sink (id 1) and is not meaningful.
- Line amounts are **signed**, so a −120.00 groceries line + a +20.00 cashback line net to the −100.00 parent total.
- `category_id` mirrors `txn.category_id` exactly — `NOT NULL`, **no `ON DELETE`** action. The category delete/merge path (`Repository.delete_category` / `merge_categories`) now repoints `txn_split.category_id` to the sink/target alongside `txn`, or the FK would block the delete.
- `ON DELETE CASCADE` on `txn_id` means deleting a split parent drops its lines automatically — `delete_transactions` needs no change.

### One unrolling view — `txn_category_line`
```sql
CREATE VIEW txn_category_line AS
  SELECT t.id AS txn_id, t.account_id, t.posted_date, t.amount, t.category_id,
         t.payee_id, t.transfer_id, t.status
    FROM txn t WHERE NOT EXISTS (SELECT 1 FROM txn_split s WHERE s.txn_id = t.id)
  UNION ALL
  SELECT t.id, t.account_id, t.posted_date, s.amount, s.category_id,
         t.payee_id, t.transfer_id, t.status
    FROM txn t JOIN txn_split s ON s.txn_id = t.id;
```
A non-split txn maps to itself; a split explodes into one row per line (the line's category + the line's amount). It exposes the same column names a plain `txn` scan used, so the category-attribution queries are a one-line `FROM` swap:
- **`spending_aggregates`** (Spending Over Time) — `FROM txn t` → `FROM txn_category_line t`.
- **`list_perimeter_txns`** (budget actuals) — `FROM txn_category_line t`, selecting `t.txn_id AS id`. **The intra-perimeter transfer-cancellation `NOT EXISTS (… t2 …)` subquery stays on the base `txn` table** — transfer pairing is a parent-row concept. `budget_calc.compute_budget_view` is unchanged: a split parent now arrives as N per-line rows, each bucketed under its own budgeted ancestor.

`account_summary.top_categories` (the third consumer) is pure-Python over an already-loaded `TransactionRow` list, so it unrolls **in memory** rather than taking a second DB round-trip: the window fetches `Repository.split_lines_for_txns(split_ids)` for the visible split parents and `top_categories` attributes each line's negative amount to its category instead of the parent's Uncategorised bucket. **`top_payees` is unaffected** — one payee per parent.

> Everything else (balance, `balance_as_of`, `statement_residual`, holdings, net worth, the register's running-balance column, the CSV dedup hash) reads the parent total and is untouched. This is the whole point of keeping the total on the parent.

### Repository (`db/repository.py`)
`TransactionRow` gains `split_count` (a cheap `LEFT JOIN (… COUNT(*) … GROUP BY txn_id)` in `list_transactions_for_account` / `list_all_transactions`) so the register can render and gate split rows without a per-row query. New methods, all asserting the sum invariant: `insert_split_transaction` (parent at Uncategorised + lines, no-commit, mirrors `insert_transaction`), `update_split_transaction` (header + replace lines, commits), `convert_plain_to_split`, `convert_split_to_plain`, `split_lines_for_txn(s)`. A new `SplitLine` dataclass carries a line back to the UI.

### Register (`register_model` / `register_window`)
- A split row's Category cell renders **"—Split—"**; the **whole split row is non-editable inline** (like an investment row), so Qt's double-click edit-trigger never fights the dialog and the parent total + line categories always change together. A defensive guard in `_apply_edit` rejects any programmatic edit of a split row.
- **Double-click** a split row → `SplitTransactionDialog` (edit mode), in both single-account and All-transactions views (the account is resolved from the row). Reconciled split rows get the same "change anyway?" confirm (`_confirm_reconciled_edit`) before the dialog opens.
- **Bulk Edit** (ADR-017): setting a category on a selection containing split parents prompts *"convert to a single category and discard split lines?"*, then routes those rows through `convert_split_to_plain`; transfer-kind categories are refused on split rows. Payee/status/memo bulk edits apply to split parents unchanged (parent-level fields).

### Split dialog — `ui/split_transaction_dialog.py` (`SplitTransactionDialog`)
Mirrors the ADR-048 self-persisting dialog. Header: account (context) · Date · Payee · Status · Memo · **Total** (signed). A lines table of **(Category · Memo · signed Amount)** with Add/Remove, and a live **"Assigned X · Unassigned Y"** indicator; **Save enables only when Unassigned reaches 0 and ≥1 line**. The per-line category picker reuses `make_category_picker` **filtered to exclude transfer-kind categories** (a split is never a transfer). Edit mode seeds from `split_lines_for_txn`; create mode (from New Transaction) seeds the header + a single Uncategorised line holding the entered total. The dialog persists via `insert_/update_split_transaction` and `accept()`s; the caller reloads.

### New Transaction (`transaction_dialog`)
A **"Split…"** button validates the header + amount (category not required) and hands them to `SplitTransactionDialog` (the entered amount becomes the split total). The Save path is unchanged.

### Import (`csv_parser` / `import_service`)
The parser→service normalised dict gains an optional **`splits`** key (`[{category_raw, memo, amount(signed)}]`); only the Banktivity path populates it. `_collapse_banktivity_splits` now keeps the parent and attaches the structured sub-rows; `_build_banktivity_splits` turns them into signed lines and, when they don't sum to the parent total (a Banktivity quirk), **appends an Uncategorised "Auto-balanced import remainder" line rather than rejecting the row** (integer-exact, no tolerance). `ClassifiedTransaction` carries `splits`; the **dedup hash is unchanged** (parent-level account|date|total|payee). The commit loop calls `insert_split_transaction` when a row has splits, else the existing single insert. First-import "Cleared" default and Banktivity per-row status stay parent-level.

---

## Consequences

- **Money layer is provably untouched.** Verified headless on a copy of the live DB: converting a plain txn to an equal-total split leaves `compute_account_balances` and `statement_residual` byte-identical; the running balance is unaffected; a split reconciles as one statement line at its full total.
- **Category attribution is correct in one place.** The `txn_category_line` view is the single definition of "what does a split spend on"; the Spending report and budget read it, the summary screen unrolls in memory. Verified: a −120 grocery line shows £120 under Groceries while the +20 cashback line is dropped by the strict-outflow `amount < 0` filter (correct refund-style behaviour).
- **Splits ⊥ transfers (v1).** A split is never a transfer and a split line never targets an account. The line picker hides transfer-kind categories; bulk-edit refuses transfer categories on splits. A combined "transfer + expense in one entry" is two transactions. Investment accounts get no splits.
- **Split-line categories are visible everywhere category usage is derived.** A split line's category lives in `txn_split.category_id`, so the "which categories are used / how often" queries were updated to read the `txn_category_line` view rather than `txn` directly: `list_category_tree` (the category manager's usage count), `count_category_transactions` (delete/merge confirmation), and `distinct_category_ids_for_account` (the register's filter combo). Because the view emits a split's lines (not its Uncategorised parent), a split parent correctly does **not** inflate the Uncategorised count, and a category used only on split lines now shows its true usage and is offered as a register filter. `TransactionRow` carries `split_category_ids` (one extra `GROUP_CONCAT` in the list queries) so the register filter proxy surfaces a "—Split—" parent when the user filters by any of its line categories. The **Spending report** already attributed correctly (its aggregation reads the view) and lists every expense category in its filter regardless of usage, so it needed no change.
- **Import never loses a transaction.** An unbalanced or malformed Banktivity split is auto-balanced (remainder line) and logged, not dropped — consistent with the ADR-038 "trust the data, surface the oddity" stance.
- **Deferred:** QIF split import (`S`/`E`/`$` lines in `!Type:Bank`/`!Type:CCard` sections — currently skipped) is a cheap later extension of the same `splits` dict contract; the OFX/QFX/generic-CSV paths simply never emit `splits`. Per-line transfers and investment splits remain out of scope.
