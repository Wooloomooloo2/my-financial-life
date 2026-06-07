-- 0011_reconciliation.sql — statement reconciliation (ADR-040 + 2026-06-07
-- Banktivity-aligned amendment).
--
-- A "statement" is a per-account reconciliation period: a start/end date and
-- the bank's starting + ending balances. The user ticks off the transactions
-- that appear on the paper statement until "Missing" — the difference between
-- the change-in-balance and the net of the ticked rows — reaches zero, then
-- saves to mark those rows Reconciled.
--
-- Two new tables + one column on txn:
--
--   statement      — one reconciliation period per account. `status` is just
--                    'open' (in progress / resumable) or 'reconciled' (closed).
--                    There is deliberately no 'reconciled_with_variance' state:
--                    a statement that closed with a residual, OR one that later
--                    drifted because a reconciled row's amount was edited, is
--                    detected *live* by recomputing the residual against the
--                    current linked rows (see Repository.statement_residual).
--                    `closing_variance_pence` is only the at-close snapshot of
--                    that residual, kept for reference.
--   statement_txn  — join table: which txns belong to a statement. This is the
--                    authoritative "ticked" record and exists for both open
--                    (ticks-in-progress) and closed statements. At close, the
--                    same rows also get txn.status='Reconciled' + txn.statement_id.
--
--   txn.statement_id — soft pointer to the statement a row was reconciled to.
--                    ON DELETE SET NULL: deleting a statement leaves the rows in
--                    place (the Repository's delete/reopen verbs revert status).
--
-- No Adjustment-category seed and no adjustment_txn_id column: the amendment
-- replaced the auto-adjustment mechanism with an "Add Transaction while
-- reconciling" affordance, so a missing line is just a normal user txn.
--
-- IRIs follow the ADR-006 convention (mfl:Statement_<uuid8>).

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- statement — one reconciliation period per account.
-- UNIQUE(account_id, end_date): one statement per account per closing date.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE statement (
    id                     INTEGER PRIMARY KEY,
    iri                    TEXT NOT NULL UNIQUE,
    account_id             INTEGER NOT NULL
                           REFERENCES account(id) ON DELETE CASCADE,
    start_date             TEXT NOT NULL,
    end_date               TEXT NOT NULL,
    starting_balance_pence INTEGER NOT NULL,
    ending_balance_pence   INTEGER NOT NULL,
    status                 TEXT NOT NULL
                           CHECK (status IN ('open', 'reconciled')),
    closing_variance_pence INTEGER NOT NULL DEFAULT 0,  -- at-close snapshot of Missing
    notes                  TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    reconciled_at          TEXT,
    UNIQUE(account_id, end_date)
);

CREATE INDEX idx_statement_account ON statement(account_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- statement_txn — the ticked rows belonging to a statement (open or closed).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE statement_txn (
    statement_id  INTEGER NOT NULL
                  REFERENCES statement(id) ON DELETE CASCADE,
    txn_id        INTEGER NOT NULL
                  REFERENCES txn(id) ON DELETE CASCADE,
    PRIMARY KEY (statement_id, txn_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- txn.statement_id — soft pointer set at close; cleared on reopen/delete.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE txn ADD COLUMN statement_id INTEGER
    REFERENCES statement(id) ON DELETE SET NULL;

CREATE INDEX idx_txn_statement ON txn(statement_id);
