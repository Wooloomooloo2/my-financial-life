-- 0030_budget_bill_lines.sql — bills as scheduled-backed budget lines (ADR-094).
--
-- A "bill" is a budget envelope tied to a real scheduled transaction (ADR-023):
-- the schedule owns the date, cadence, amount, and paying account that a
-- category-level budget_line otherwise lacks. The link lets the burn-down
-- project the bill at its due date(s) and FLATTEN once paid (amount-matched
-- against actuals), and lets a weekly/twice-monthly bill contribute the right
-- number of occurrences in a monthly view.
--
-- One nullable column: NULL = an ordinary envelope (every existing row), set =
-- a bill. ON DELETE SET NULL so deleting the schedule quietly demotes the line
-- back to a normal envelope rather than cascading the budget line away.

PRAGMA foreign_keys = ON;

ALTER TABLE budget_line ADD COLUMN scheduled_txn_id INTEGER
    REFERENCES scheduled_txn(id) ON DELETE SET NULL;

CREATE INDEX idx_budget_line_schedule
    ON budget_line(scheduled_txn_id) WHERE scheduled_txn_id IS NOT NULL;
