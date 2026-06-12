-- 0019_budget_redesign.sql — budget redesign (ADR-058).
--
-- Supersedes the ADR-024 budget core. The single amortized
-- `budget_category.amount + cadence` per category can't support editing one
-- month's figure (matrix, principle 10) or rollover (principle 7), so the
-- model pivots to EXPLICIT PER-MONTH ALLOCATIONS:
--
--   budget          gains a period: start_month ('YYYY-MM') + length_months
--                   (default 12 = Jan–Dec) + an optional display currency.
--   budget_line     the envelope — one row per budgeted category in a budget
--                   (category + role + rollover policy). Replaces
--                   budget_category's amount/cadence with a per-month grid.
--   budget_allocation  the editable matrix cell — (budget_line × month →
--                   positive-pence amount; sign comes from category.kind).
--
-- Actuals and rollover are NOT stored — both are computed in budget_calc.py
-- from budget_allocation + perimeter txns + prior-month carry. budget_account
-- (the perimeter M:N) is unchanged.
--
-- This migration extends `budget`, creates the two new tables, migrates the
-- existing budget_category rows into lines + 12 seeded monthly allocations
-- (rough cadence→month conversion; the owner re-tunes in the matrix), then
-- drops budget_category. A fresh database (budget_category empty) just gets
-- the new shape with nothing to migrate.
--
-- The runner manages schema_version and wraps this file in a transaction;
-- DROP TABLE budget_category is safe with foreign_keys ON because nothing
-- references it (budget_account/budget_line FK to budget + account/category).

-- ── budget gains a period (principle 4) ──
ALTER TABLE budget ADD COLUMN start_month   TEXT;     -- 'YYYY-MM'; backfilled below
ALTER TABLE budget ADD COLUMN length_months INTEGER NOT NULL DEFAULT 12;
ALTER TABLE budget ADD COLUMN currency      TEXT;     -- display ccy; NULL = file base

-- Existing budgets default to the current calendar year (Jan–Dec).
UPDATE budget SET start_month = strftime('%Y-01', 'now') WHERE start_month IS NULL;

-- ── the envelope (replaces budget_category) ──
CREATE TABLE budget_line (
    id          INTEGER PRIMARY KEY,
    budget_id   INTEGER NOT NULL REFERENCES budget(id)   ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'discretionary'
                  CHECK(role IN ('bills','saving','discretionary')),
    -- Auto-rollover policy (ADR-058 D3). 'accumulate' carries the running
    -- surplus/deficit forward; 'none' resets each month. The Repository seeds
    -- 'accumulate' for expense lines, 'none' for income/transfer.
    rollover    TEXT NOT NULL DEFAULT 'none'
                  CHECK(rollover IN ('none','accumulate')),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (budget_id, category_id)
);

-- ── the editable matrix cell ──
CREATE TABLE budget_allocation (
    id              INTEGER PRIMARY KEY,
    budget_line_id  INTEGER NOT NULL REFERENCES budget_line(id) ON DELETE CASCADE,
    month           TEXT NOT NULL,                       -- 'YYYY-MM'
    amount          INTEGER NOT NULL CHECK(amount >= 0), -- positive pence
    UNIQUE (budget_line_id, month)
);

CREATE INDEX idx_budget_line_budget     ON budget_line(budget_id);
CREATE INDEX idx_budget_line_category   ON budget_line(category_id);
CREATE INDEX idx_budget_allocation_line ON budget_allocation(budget_line_id);

-- ── migrate existing budget_category rows → lines ──
INSERT INTO budget_line (budget_id, category_id, role, rollover, sort_order)
SELECT bc.budget_id,
       bc.category_id,
       bc.role,
       CASE WHEN c.kind = 'expense' THEN 'accumulate' ELSE 'none' END,
       bc.id
FROM budget_category bc
JOIN category c ON c.id = bc.category_id;

-- ── seed 12 (length_months) monthly allocations per migrated line ──
-- amount = the old amortized cadence amount converted to a per-month figure.
INSERT INTO budget_allocation (budget_line_id, month, amount)
WITH RECURSIVE nums(n) AS (
    SELECT 0
    UNION ALL SELECT n + 1 FROM nums WHERE n < 59
)
SELECT bl.id,
       strftime('%Y-%m', date(b.start_month || '-01', '+' || nums.n || ' months')),
       CAST(ROUND(
           CASE bc.cadence
               WHEN 'monthly'   THEN bc.amount * 1.0
               WHEN 'weekly'    THEN bc.amount * 52.0 / 12.0
               WHEN 'biweekly'  THEN bc.amount * 26.0 / 12.0
               WHEN 'quarterly' THEN bc.amount / 3.0
               WHEN 'annual'    THEN bc.amount / 12.0
           END
       ) AS INTEGER)
FROM budget_line bl
JOIN budget          b  ON b.id = bl.budget_id
JOIN budget_category bc ON bc.budget_id = bl.budget_id
                       AND bc.category_id = bl.category_id
JOIN nums ON nums.n < b.length_months;

DROP TABLE budget_category;
