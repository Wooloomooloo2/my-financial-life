-- 0031_loan_accounts.sql — loan accounts & amortization (ADR-095).
--
-- Adds a 'loan_std' account type (family 'loan', a liability) and a 1:1 `loan`
-- terms table. A loan's balance is the usual SUM(txn.amount) (negative = owed),
-- so Net Worth / sidebar / balances need no special-casing beyond the family
-- being a liability — only the account.type CHECK has to learn the new value.
--
-- SQLite can't ALTER a CHECK in place, so we recreate `account` with the
-- widened list (the ADR-032 recipe), preserving every current column incl. the
-- ADR-058-R4a `credit_limit`. foreign_keys=OFF during the swap so FK references
-- into account (by table name) resolve to the renamed table.

PRAGMA foreign_keys = OFF;

CREATE TABLE account_new (
    id               INTEGER PRIMARY KEY,
    iri              TEXT UNIQUE NOT NULL,
    name             TEXT NOT NULL,
    type             TEXT NOT NULL,
    family           TEXT NOT NULL,
    currency         TEXT NOT NULL,
    is_liability     INTEGER NOT NULL DEFAULT 0 CHECK(is_liability IN (0,1)),
    opening_balance  INTEGER NOT NULL DEFAULT 0,
    opened_on        TEXT,
    archived_at      TEXT,
    folder_id        INTEGER REFERENCES account_folder(id) ON DELETE SET NULL,
    credit_limit     INTEGER,
    CHECK (type IN (
        'cash_std','savings_std','credit_std','investment_std',
        'property_std','vehicle_std','loan_std'
    ))
);

INSERT INTO account_new (
    id, iri, name, type, family, currency, is_liability,
    opening_balance, opened_on, archived_at, folder_id, credit_limit
)
SELECT
    id, iri, name, type, family, currency, is_liability,
    opening_balance, opened_on, archived_at, folder_id, credit_limit
FROM account;

DROP TABLE account;
ALTER TABLE account_new RENAME TO account;

CREATE INDEX idx_account_folder ON account(folder_id);

PRAGMA foreign_keys = ON;

-- ── loan terms (1:1 with the account) ───────────────────────────────────────
--
-- current principal = original_amount − principal_paid. The amortization
-- schedule (loan_calc.py) is projected from the current principal forward; the
-- account's transactions track the actual paydown. All money is pence.
CREATE TABLE loan (
    account_id          INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    original_amount     INTEGER NOT NULL,
    principal_paid      INTEGER NOT NULL DEFAULT 0,
    interest_rate       REAL NOT NULL,                 -- annual %, e.g. 5.5
    compounding         TEXT NOT NULL DEFAULT 'monthly'
                          CHECK(compounding IN ('daily','monthly','annually')),
    term_months         INTEGER,                       -- drives a calculated payment
    payment             INTEGER,                       -- pence; NULL = calculated
    extra_payment       INTEGER NOT NULL DEFAULT 0,    -- pence; 0 = none
    start_date          TEXT NOT NULL,                 -- 'YYYY-MM-DD'
    payment_day         INTEGER NOT NULL DEFAULT 1,    -- day-of-month (1..31)
    track_mode          TEXT NOT NULL DEFAULT 'split'
                          CHECK(track_mode IN ('split','whole')),
    interest_source     TEXT NOT NULL DEFAULT 'loan'
                          CHECK(interest_source IN ('loan','payment')),
    payment_account_id  INTEGER REFERENCES account(id) ON DELETE SET NULL,
    interest_category_id INTEGER REFERENCES category(id),
    goal_id             INTEGER REFERENCES budget_goal(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
