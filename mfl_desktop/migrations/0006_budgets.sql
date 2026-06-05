-- 0006_budgets.sql — second half of the budget arc (round B; ADR-024).
--
-- Three tables: `budget` (one plan per file in v1; multi-plan is deferred),
-- `budget_account` (M:N — which accounts make up this budget's perimeter),
-- and `budget_category` (per-category target amount + cadence + role).
--
-- Perimeter rule (ADR-024 §transfers): an in-perimeter transfer to an
-- in-perimeter account cancels out. A transfer where one side is in-
-- perimeter and the other side isn't counts as a normal outflow/inflow
-- depending on direction. The computation lives in the budget module
-- (mfl_desktop/budget_calc.py) — not enforced at the schema layer.
--
-- Amount sign: stored as a positive magnitude in pence; the category's
-- `kind` (income / expense / transfer) tells the computation whether to
-- treat it as inflow or outflow. Mixing a signed convention here would
-- duplicate information already on the category and let the two drift.
--
-- Role: each budgeted category is tagged `bills`, `saving`, or
-- `discretionary` so the Simplifi-style "income after bills & saving"
-- decomposition can split the tiles. Role lives on `budget_category`,
-- not on `category` directly, because the same category might be a
-- bill in one budget and tracked differently in another (multi-budget
-- future) — and even with one budget per file v1, semantically the
-- role is "how this budget treats this category", not an intrinsic
-- property of the category.

PRAGMA foreign_keys = ON;

CREATE TABLE budget (
    id          INTEGER PRIMARY KEY,
    iri         TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL DEFAULT 'My Budget',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE budget_account (
    budget_id    INTEGER NOT NULL REFERENCES budget(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    PRIMARY KEY (budget_id, account_id)
);

CREATE TABLE budget_category (
    id              INTEGER PRIMARY KEY,
    budget_id       INTEGER NOT NULL REFERENCES budget(id) ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    amount          INTEGER NOT NULL CHECK(amount >= 0),
    cadence         TEXT NOT NULL CHECK(cadence IN ('weekly','biweekly','monthly','quarterly','annual')),
    role            TEXT NOT NULL DEFAULT 'discretionary' CHECK(role IN ('bills','saving','discretionary')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (budget_id, category_id)
);

CREATE INDEX idx_budget_category_budget ON budget_category(budget_id);
CREATE INDEX idx_budget_category_category ON budget_category(category_id);
