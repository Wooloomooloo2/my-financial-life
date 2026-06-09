-- 0015_security_price_transaction_source.sql — ADR-047, investment pricing.
--
-- Adds 'transaction' to the security_price.source CHECK so a price observed
-- from a Buy/Sell of an UNTICKERED security (its per-share trade price on the
-- trade date) can be stored — the semi-automated way to build price history
-- for the 56 holdings that carry no ticker and so can't be fetched from Tiingo.
--
-- SQLite can't ALTER a CHECK in place, so we recreate the table with the
-- widened list — the same approach used for account.type (0008) and
-- report.type (0014). security_price references security(id); nothing
-- references security_price, so foreign_keys=OFF during the swap is enough and
-- the outgoing FK resolves to the recreated table after the rename.
--
-- Source precedence (enforced in Repository, not the schema): manual > tiingo >
-- transaction. A manually-typed price is never auto-overwritten; a Tiingo fetch
-- overwrites tiingo/transaction rows but not manual; a transaction-derived
-- price only ever overwrites a prior transaction-derived row.

PRAGMA foreign_keys = OFF;

CREATE TABLE security_price_new (
    security_id INTEGER NOT NULL REFERENCES security(id) ON DELETE CASCADE,
    price_date  TEXT NOT NULL,                  -- 'YYYY-MM-DD'
    price       REAL NOT NULL CHECK(price >= 0),
    currency    TEXT,                            -- trading currency (USD here); informational
    source      TEXT NOT NULL DEFAULT 'manual'
                CHECK(source IN ('manual', 'tiingo', 'transaction')),
    PRIMARY KEY (security_id, price_date)
);

INSERT INTO security_price_new (security_id, price_date, price, currency, source)
SELECT security_id, price_date, price, currency, source FROM security_price;

DROP TABLE security_price;
ALTER TABLE security_price_new RENAME TO security_price;

-- Recreate the (security_id, price_date DESC) index lost with the old table.
CREATE INDEX idx_security_price_latest
    ON security_price(security_id, price_date DESC);

PRAGMA foreign_keys = ON;
