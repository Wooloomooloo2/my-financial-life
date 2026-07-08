# ADR-144 — Investment reports: name / symbol cell links to the Stock Record

**Date:** 2026-07-08
**Status:** Implemented
**Related:** ADR-047 (Stock Record screen — per-security detail). ADR-083 (double-click a security row → its transactions over the report period). ADR-109 follow-up (security-aware drill-down columns).

## Context

The Stock Record screen (ADR-047, reached from Manage ▸ Securities) is the home for a single security: identity, price history + mini chart, current position, and every Buy/Sell/Div across accounts. Until now the only way in was Manage ▸ Securities — double-clicking a row there. But the two investment reports (Investment Returns, Investment Income) are exactly where the owner is *looking at a security by name* and wants its detail — and had no link to it.

Both report tables already wire `cellDoubleClicked` → `_on_security_row_activated`, which opens the transactions drill-down for the period (ADR-083). That drill-down is useful and shouldn't be lost. Both tables share the same first two columns: **Symbol** (col 0) and **Security** (col 1), with the row's security id stashed on the first cell (`_SID_ROLE`).

## Decision

Make the double-click **column-aware** in both report windows:

- Double-click the **Symbol (col 0)** or **Security (col 1)** cell → open that security's **Stock Record** (`StockRecordDialog`, modal, parented to the report).
- Double-click **any other (numeric) cell** → keep the existing transactions drill-down (ADR-083), unchanged.

`_on_security_row_activated(row, col)` now only routes on `col`; the two destinations are separate helpers (`_open_stock_record_for_row`, `_open_transactions_for_row`). The Stock Record helper reads the row's `_SID_ROLE`, resolves a `SecurityRow` via `repo.get_security(sid)` (the same call Manage ▸ Securities uses), and `.exec()`s the dialog — mirroring `securities_dialog._open_record_for_row`.

Rejected: replacing the transactions drill-down entirely (the period-scoped transaction list is a distinct, useful view); a right-click context menu (heavier, less discoverable than "double-click the name"); making the name/symbol cells look like hyperlinks (the table is a plain `QTableWidget` with alternating rows — a link style would fight the report's visual language, and double-click matches the Securities-dialog affordance the owner already knows).

## Consequences

- From either investment report, double-clicking a security's name or symbol jumps straight to its Stock Record — set a ticker, hand-price, or read the position without going back through Manage ▸ Securities.
- The numeric-cell → transactions drill-down (ADR-083) is untouched; the two affordances now coexist on one table, split by column.
- No schema change; both windows already carried the security id per row.
- `tests/test_investment_report_stock_record_link.py` 2/2 (Symbol/Security cells route to the Stock Record; every numeric column keeps the transactions drill-down — a regression guard for a future column-layout change silently redirecting the name/symbol cells). Existing `tests/test_drilldown_investment_columns.py` 4/4 unaffected.
