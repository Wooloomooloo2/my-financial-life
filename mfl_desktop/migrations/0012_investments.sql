-- 0012_investments.sql — investment accounts & QIF import, round 1 (ADR-043).
--
-- Adds a securities master and extends `txn` so an investment account's
-- register can hold security actions (Buy / Sell / Div / ReinvDiv / ShrsIn /
-- …) alongside ordinary cash rows in a single interleaved stream — the model
-- QIF, Banktivity, and Quicken all use. The owner-confirmed alternative
-- (a separate investment_txn table) was rejected because it would split the
-- account into two streams and double the register/model/import plumbing.
--
-- Two changes:
--
--   security        — one row per traded instrument. QIF references securities
--                     by NAME (the `Y` field); the ticker `symbol` (`S`) is
--                     frequently blank in Banktivity exports, so `name` is the
--                     unique key and `symbol` is a nullable secondary key for a
--                     future price feed (round 3).
--
--   txn.action      — the QIF investment action (Buy/Sell/Div/Cash/ShrsIn/
--                     ShrsOut/ReinvDiv/CGShort/CGLong/XIn/XOut/StkSplit).
--                     NULL on an ordinary cash transaction, so every existing
--                     row and the entire cash code path are unaffected.
--                     Deliberately NOT a CHECK constraint: QIF exports carry
--                     quirks (a malformed empty StkSplit; an XIn that moves
--                     shares, not cash) and the action set will grow — it is
--                     validated/normalised in Python (qif_parser) instead.
--   txn.security_id — the instrument this action concerns (NULL for pure cash).
--   txn.quantity    — share count as a positive magnitude; the action carries
--                     direction (Buy/ShrsIn/ReinvDiv add, Sell/ShrsOut remove).
--                     REAL to match the existing lot.quantity choice (ADR-010).
--   txn.price       — per-share price (QIF `I`). REAL, matching lot.unit_cost.
--   txn.commission  — fee in pence (QIF `O`). Informational in round 1.
--
-- The existing `txn.amount` (INTEGER pence) remains the SIGNED CASH IMPACT, so
-- cash balance = SUM(amount) is unchanged: Buy -T, Sell/Div/CGShort/CGLong/Cash
-- +T (Cash's T already carries its sign), ShrsIn/ShrsOut/ReinvDiv/StkSplit 0
-- (no cash impact — a reinvested dividend nets to zero), XIn/XOut ±T.
--
-- Round 1 stores the raw transactions + securities only. Cost basis (lot),
-- market value (valuation), and cross-account transfer-linking are deferred to
-- later rounds (see ADR-043). The existing lot/valuation tables (ADR-010) are
-- intentionally left untouched here.
--
-- IRIs follow the ADR-006 convention (mfl:Security_<uuid8>).

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- security — the instrument master. Referenced by name (QIF `Y`).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE security (
    id          INTEGER PRIMARY KEY,
    iri         TEXT UNIQUE NOT NULL,    -- 'mfl:Security_a3f7c901'
    name        TEXT UNIQUE NOT NULL,    -- 'SCHWAB US DIVIDEND EQUITY ETF'
    symbol      TEXT,                    -- 'SCHD' (nullable; QIF `S`, often blank)
    type        TEXT,                    -- QIF `T` ('Stock', etc.)
    archived_at TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- txn investment columns — all nullable; action IS NULL ⇒ ordinary cash row.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE txn ADD COLUMN action      TEXT;
ALTER TABLE txn ADD COLUMN security_id INTEGER REFERENCES security(id) ON DELETE SET NULL;
ALTER TABLE txn ADD COLUMN quantity    REAL;
ALTER TABLE txn ADD COLUMN price       REAL;
ALTER TABLE txn ADD COLUMN commission  INTEGER;

CREATE INDEX idx_txn_security ON txn(security_id) WHERE security_id IS NOT NULL;
