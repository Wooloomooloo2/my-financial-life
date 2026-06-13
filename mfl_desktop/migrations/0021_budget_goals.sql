-- ─────────────────────────────────────────────────────────────────────────────
-- 0021 — Pay-down (and future savings) goals (ADR-058 R4b)
--
-- A goal targets one account in a budget to reach a target balance by a target
-- date. The app derives the required monthly contribution and a progress figure
-- from the live balance — nothing about the result is stored; only the intent
-- (target + date) and the baseline captured at creation.
--
--   budget_id / account_id — the goal belongs to a budget (so duplicate-as-
--     scenario branches it, like budget_account / budget_line) and one account.
--     UNIQUE(budget_id, account_id) — at most one goal per account per budget.
--
--   kind — 'paydown' (a liability toward a smaller debt) now; 'savings' (an
--     asset toward a larger balance) is the R4c mirror. The math is identical on
--     signed balances, so the column is here from the start and R4c needs no
--     migration — only a UI pass.
--
--   target_amount / start_amount — SIGNED pence balances (a card you owe £1,800
--     on is -180000). target is the balance to reach (0 = a card fully paid).
--     start is the balance captured when the goal was created — progress is
--     measured from there, so later charges that push the balance back up
--     visibly reduce progress (ADR-058 R4b: balance-based progress).
--
--   target_date / start_date — 'YYYY-MM-DD'. months-left = whole calendar months
--     from today to target_date; required monthly = work-left / months-left.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE budget_goal (
    id            INTEGER PRIMARY KEY,
    iri           TEXT UNIQUE NOT NULL,
    budget_id     INTEGER NOT NULL REFERENCES budget(id)  ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'paydown'
                    CHECK (kind IN ('paydown', 'savings')),
    target_amount INTEGER NOT NULL,           -- signed pence: target balance
    target_date   TEXT NOT NULL,              -- 'YYYY-MM-DD'
    start_amount  INTEGER NOT NULL,           -- signed pence: balance at creation
    start_date    TEXT NOT NULL,              -- 'YYYY-MM-DD' at creation
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (budget_id, account_id)
);

CREATE INDEX idx_budget_goal_budget ON budget_goal(budget_id);
