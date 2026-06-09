-- 0013_security_prices.sql — security price store (ADR-044, investment round 2).
--
-- Per-security, date-stamped price points feeding holdings market value and
-- (round 3) value-over-time. Deliberately NOT the account-level `valuation`
-- table (ADR-010): valuations are one mark-to-market number per account;
-- prices are a per-security time series, and an investment account's value is
-- DERIVED (cash + Σ shares × price), not a stored account valuation.
--
-- Shape mirrors `fx_rate` (ADR-035): a composite PK on (id, date) plus a
-- (id, date DESC) index so "latest price" and (round 3) "nearest-prior price"
-- are index-only lookups. `source` distinguishes a manually-typed price from a
-- Tiingo fetch; manual is the universal fallback because 56 of the 91
-- securities in the owner's data carry no ticker and can't be auto-priced.
--
-- `price` is REAL (a per-share quote, like txn.price / lot.unit_cost), not
-- pence — consistent with how shares/prices are stored since ADR-043. Money
-- (cash amounts) stays integer pence elsewhere.

PRAGMA foreign_keys = ON;

CREATE TABLE security_price (
    security_id INTEGER NOT NULL REFERENCES security(id) ON DELETE CASCADE,
    price_date  TEXT NOT NULL,                  -- 'YYYY-MM-DD'
    price       REAL NOT NULL CHECK(price >= 0),
    currency    TEXT,                            -- trading currency (USD here); informational in R2
    source      TEXT NOT NULL DEFAULT 'manual'
                CHECK(source IN ('manual', 'tiingo')),
    PRIMARY KEY (security_id, price_date)
);

CREATE INDEX idx_security_price_latest
    ON security_price(security_id, price_date DESC);
