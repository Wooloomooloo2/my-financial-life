-- ADR-032: add the 'vehicle_std' account type (family = 'vehicle').
--
-- SQLite can't ALTER a CHECK constraint in place, so we recreate the
-- table with the widened CHECK list. FK references INTO account from
-- txn, lot, valuation, import_batch, scheduled_txn, budget_account, etc.
-- are stored by table name and resolve to the renamed table once the
-- rename completes (foreign_keys=OFF prevents intermediate checks from
-- firing during the swap).

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
    CHECK (type IN (
        'cash_std','savings_std','credit_std','investment_std',
        'property_std','vehicle_std'
    ))
);

INSERT INTO account_new (
    id, iri, name, type, family, currency, is_liability,
    opening_balance, opened_on, archived_at, folder_id
)
SELECT
    id, iri, name, type, family, currency, is_liability,
    opening_balance, opened_on, archived_at, folder_id
FROM account;

DROP TABLE account;
ALTER TABLE account_new RENAME TO account;

-- Recreate the folder index lost with the original table.
CREATE INDEX idx_account_folder ON account(folder_id);

PRAGMA foreign_keys = ON;
