-- ─────────────────────────────────────────────────────────────────────────────
-- 0022 — Multi-account, proportional goals (ADR-058 R4c)
--
-- R4b shipped goals as one-account-⇄-one-goal (budget_goal.account_id +
-- UNIQUE(budget_id, account_id)), deriving the goal's name, currency, baseline
-- and direction from that single account. R4c generalises:
--
--   • a goal can be supported by MANY accounts (e.g. Retirement = savings +
--     investment + pension), and
--   • one account can be SPLIT across goals (e.g. some % of a savings account
--     funds "Camper van", the rest funds "Retirement").
--
-- So the per-account specifics move out of budget_goal into a new M:N link
-- table budget_goal_account, each link carrying a PERCENTAGE (basis points) of
-- the account's balance plus a baseline captured at link creation. budget_goal
-- keeps only goal-level identity and gains a NAME and a reporting CURRENCY (the
-- contributing accounts may be multi-currency and are converted into it via the
-- ADR-055 FX layer at compute time).
--
-- SQLite can't drop a column / UNIQUE in place, so budget_goal is rebuilt.
-- Nothing has an incoming FK to budget_goal (budget_goal_account is new), so
-- the rename-rebuild is safe with foreign_keys ON. ids/iris are preserved so
-- the new link rows match the migrated goals. The runner manages
-- schema_version and wraps this file in a transaction.
--
--   name          — 'Retirement', 'Camper van' (was implicitly the account name).
--   currency      — the goal's reporting currency; per-account balances convert
--                   into it. Migrated goals keep their single account's currency.
--   kind          — 'savings' (assets → a larger balance) / 'paydown' (a
--                   liability → a smaller debt). The math is identical on signed
--                   balances (goal_calc); kind only drives labelling + which
--                   accounts are eligible.
--   target_amount — SIGNED pence in `currency` (a card you owe £1,800 on is
--                   -180000; a £30,000 savings target is +3000000).
--
--   budget_goal_account.share_bp        — basis points, 0..10000 (= 0..100%) of
--                   the account's balance that counts toward THIS goal. A whole-
--                   account link is 10000; a split savings account might be 3000
--                   to one goal + 7000 to another.
--   budget_goal_account.baseline_balance — the account's FULL native signed
--                   balance at link creation (NOT × share). Progress is measured
--                   from baseline so later activity moves it; storing the full
--                   balance (share applied at compute time) means changing a
--                   share never needs a re-baseline. Goal start =
--                   Σ(baseline_balance × share_bp/10000), converted to currency.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE budget_goal RENAME TO budget_goal_old;

CREATE TABLE budget_goal (
    id            INTEGER PRIMARY KEY,
    iri           TEXT UNIQUE NOT NULL,
    budget_id     INTEGER NOT NULL REFERENCES budget(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'savings'
                    CHECK (kind IN ('paydown', 'savings')),
    currency      TEXT NOT NULL,
    target_amount INTEGER NOT NULL,           -- signed pence in `currency`
    target_date   TEXT NOT NULL,              -- 'YYYY-MM-DD'
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE budget_goal_account (
    id               INTEGER PRIMARY KEY,
    goal_id          INTEGER NOT NULL REFERENCES budget_goal(id) ON DELETE CASCADE,
    account_id       INTEGER NOT NULL REFERENCES account(id)     ON DELETE CASCADE,
    share_bp         INTEGER NOT NULL DEFAULT 10000,   -- basis points, 0..10000
    baseline_balance INTEGER NOT NULL,                 -- signed pence: full balance at link creation
    start_date       TEXT NOT NULL,                    -- 'YYYY-MM-DD' at link creation
    UNIQUE (goal_id, account_id)
);

-- Migrate existing R4b single-account goals: each becomes a named goal in its
-- account's currency + one whole-account (100%) link whose baseline is the old
-- captured start_amount/start_date.
INSERT INTO budget_goal
    (id, iri, budget_id, name, kind, currency, target_amount, target_date, created_at)
SELECT g.id, g.iri, g.budget_id,
       a.name || (CASE g.kind WHEN 'paydown' THEN ' pay-down' ELSE ' savings' END),
       g.kind, a.currency, g.target_amount, g.target_date, g.created_at
FROM budget_goal_old g
JOIN account a ON a.id = g.account_id;

INSERT INTO budget_goal_account
    (goal_id, account_id, share_bp, baseline_balance, start_date)
SELECT g.id, g.account_id, 10000, g.start_amount, g.start_date
FROM budget_goal_old g;

DROP TABLE budget_goal_old;

-- Indexes created after the old table (and its same-named index) are dropped,
-- so the idx_budget_goal_budget name is free to reuse.
CREATE INDEX idx_budget_goal_budget   ON budget_goal(budget_id);
CREATE INDEX idx_budget_goal_acc_goal ON budget_goal_account(goal_id);
