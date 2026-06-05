-- 0005_scheduled_txn.sql — adds scheduled transactions (round A of the
-- budget arc; see ADR-023).
--
-- A scheduled transaction is a *template* — every <cadence> on/around
-- <next_due_date> a real `txn` is expected. "Posting" materialises the
-- template into a `txn` (or a transfer pair if the category is
-- kind='transfer') and advances next_due_date by one cadence step.
--
-- Sign convention matches `txn.amount`: estimated_amount is signed, so a
-- +£3,000 schedule on an income-kind category is a salary; a -£15.99
-- on an expense-kind category is a subscription. Variable amounts (e.g.
-- utility bills) store the estimate here and prompt the user for the
-- actual at post time — the schedule is the template, the materialised
-- txn is the truth.
--
-- transfer_to_account_id is required when the category's kind='transfer'
-- (app-level check; kind lives in a different table so the DB only
-- enforces FK validity). For non-transfer categories it stays NULL.
--
-- end_date is optional. When the post path advances next_due_date past
-- end_date, the schedule is archived (archived_at set, not deleted) so
-- historical materialised txns retain a recoverable link to the source.

PRAGMA foreign_keys = ON;

CREATE TABLE scheduled_txn (
    id                      INTEGER PRIMARY KEY,
    iri                     TEXT UNIQUE NOT NULL,
    account_id              INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    payee_id                INTEGER REFERENCES payee(id) ON DELETE SET NULL,
    category_id             INTEGER NOT NULL REFERENCES category(id),
    transfer_to_account_id  INTEGER REFERENCES account(id) ON DELETE SET NULL,
    estimated_amount        INTEGER NOT NULL,
    variable                INTEGER NOT NULL DEFAULT 0 CHECK(variable IN (0,1)),
    memo                    TEXT,
    cadence                 TEXT NOT NULL CHECK(cadence IN ('weekly','biweekly','monthly','quarterly','annual')),
    anchor_date             TEXT NOT NULL,
    next_due_date           TEXT NOT NULL,
    end_date                TEXT,
    auto_post               INTEGER NOT NULL DEFAULT 0 CHECK(auto_post IN (0,1)),
    notes                   TEXT,
    archived_at             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Launch sweep uses (next_due_date, auto_post=1, archived_at IS NULL); the
-- partial index keeps it tiny — only active, auto-posting schedules.
CREATE INDEX idx_scheduled_txn_due_auto
    ON scheduled_txn(next_due_date)
    WHERE archived_at IS NULL AND auto_post = 1;

-- The Schedules dialog and budget round B query by next_due_date across
-- all active schedules (auto and manual) when computing planned spending,
-- so a broader covering index too.
CREATE INDEX idx_scheduled_txn_due
    ON scheduled_txn(next_due_date)
    WHERE archived_at IS NULL;
