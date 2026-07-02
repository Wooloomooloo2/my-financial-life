-- ADR-130 Phase 1: rename txn.status to the lowercase confidence ladder and
-- widen the CHECK.
--
--   Pending    -> pending      (spent, not yet at the bank)
--   Uncleared  -> cleared      (you saw it post at the bank)
--   Cleared    -> matched      (a downloaded record matched/added it)
--   Reconciled -> reconciled   (statement-locked)
--
-- SQLite can't ALTER a CHECK in place, so we recreate the table (ADR-032
-- recipe) with the new value list and map the data in the copy. FKs into txn
-- (statement_txn, txn_split) are stored by table name and resolve once the
-- rename completes; foreign_keys=OFF stops intermediate checks firing during
-- the swap. legacy_alter_table=ON stops the RENAME from trying to "fix" (and
-- corrupting) the txn_category_line VIEW that references txn — the view keeps
-- referencing "txn" by name and re-resolves against the recreated table.

PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

CREATE TABLE txn_new (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    posted_date     TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    payee_id        INTEGER REFERENCES payee(id) ON DELETE SET NULL,
    category_id     INTEGER NOT NULL REFERENCES category(id),
    status          TEXT NOT NULL CHECK(status IN ('pending','cleared','matched','reconciled')),
    memo            TEXT,
    import_hash     TEXT,
    import_batch_id INTEGER REFERENCES import_batch(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    transfer_id     TEXT,
    statement_id    INTEGER REFERENCES statement(id) ON DELETE SET NULL,
    action          TEXT,
    security_id     INTEGER REFERENCES security(id) ON DELETE SET NULL,
    quantity        REAL,
    price           REAL,
    commission      INTEGER,
    accrued_interest INTEGER
);

INSERT INTO txn_new (
    id, iri, account_id, posted_date, amount, payee_id, category_id, status,
    memo, import_hash, import_batch_id, created_at, transfer_id, statement_id,
    action, security_id, quantity, price, commission, accrued_interest
)
SELECT
    id, iri, account_id, posted_date, amount, payee_id, category_id,
    CASE status
        WHEN 'Pending'    THEN 'pending'
        WHEN 'Uncleared'  THEN 'cleared'
        WHEN 'Cleared'    THEN 'matched'
        WHEN 'Reconciled' THEN 'reconciled'
        ELSE status
    END,
    memo, import_hash, import_batch_id, created_at, transfer_id, statement_id,
    action, security_id, quantity, price, commission, accrued_interest
FROM txn;

DROP TABLE txn;
ALTER TABLE txn_new RENAME TO txn;

-- Recreate the indexes lost with the original table.
CREATE INDEX idx_txn_account_date ON txn(account_id, posted_date);
CREATE INDEX idx_txn_category     ON txn(category_id);
CREATE INDEX idx_txn_payee        ON txn(payee_id);
CREATE INDEX idx_txn_status       ON txn(status);
CREATE UNIQUE INDEX idx_txn_import_hash
    ON txn(account_id, import_hash)
    WHERE import_hash IS NOT NULL;
CREATE INDEX idx_txn_transfer ON txn(transfer_id) WHERE transfer_id IS NOT NULL;
CREATE INDEX idx_txn_statement ON txn(statement_id);
CREATE INDEX idx_txn_security ON txn(security_id) WHERE security_id IS NOT NULL;

PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
