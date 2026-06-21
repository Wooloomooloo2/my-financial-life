-- 0029_bonds_and_options.sql — bonds & options as first-class securities (ADR-093).
--
-- The investment engine (ADR-043/044) models every instrument as a plain
-- equity: quantity (shares) × price (per share), market value = Σ shares ×
-- latest price, cost basis = the net cash. That breaks for the two classes the
-- owner actually trades:
--
--   * BONDS quote as a PERCENT OF PAR and trade in par multiples — 45 bonds of
--     £1,000 par at 99.618 cost 45 × 1,000 × 99.618% = £44,828.10, not
--     45 × 99.618. They carry a coupon, a maturity, and accrued interest paid
--     to the seller (part of the cash, NOT part of cost basis).
--   * OPTIONS trade in contracts of a multiplier (conventionally 100) priced as
--     a premium per share — 1 contract at 1.50 costs 1 × 100 × 1.50 = £150.
--
-- The fix expresses both with a single per-security PRICE MULTIPLIER (cash value
-- of one unit at price = 1: stock → 1, bond → face/100, option → contract_size)
-- so the engine's `shares × price` math is preserved by passing × multiplier at
-- the value sites. Descriptive metadata (coupon, maturity, strike, expiry, …)
-- rides alongside for display and future coupon scheduling.
--
-- Every column is nullable or defaulted, so each existing security reads back as
-- a plain stock (instrument_type 'stock', price_multiplier 1.0, accrued NULL)
-- and every existing query is byte-for-byte unchanged.

PRAGMA foreign_keys = ON;

-- ── security: instrument class + per-class metadata ──────────────────────────

-- Structured discriminator. The existing free-text `type` (QIF `T`, e.g.
-- 'Stock') stays as informational text; this is the value-math switch.
ALTER TABLE security ADD COLUMN instrument_type TEXT NOT NULL DEFAULT 'stock'
    CHECK (instrument_type IN ('stock', 'bond', 'option'));

-- The value-math source of truth: cash value of one unit at price = 1.
--   stock  → 1.0
--   bond   → face_value / 100   (price is a % of par)
--   option → contract_size      (price is a premium per share)
ALTER TABLE security ADD COLUMN price_multiplier REAL NOT NULL DEFAULT 1.0;

-- Bond metadata.
ALTER TABLE security ADD COLUMN face_value    REAL;   -- par per unit (1000)
ALTER TABLE security ADD COLUMN coupon_rate   REAL;   -- annual coupon %, e.g. 4.0
ALTER TABLE security ADD COLUMN maturity_date TEXT;   -- redemption 'YYYY-MM-DD'
ALTER TABLE security ADD COLUMN cusip         TEXT;   -- CUSIP / ISIN identity

-- Option metadata.
ALTER TABLE security ADD COLUMN underlying_symbol TEXT;  -- e.g. 'AAPL'
ALTER TABLE security ADD COLUMN strike            REAL;  -- strike price
ALTER TABLE security ADD COLUMN expiry_date       TEXT;  -- 'YYYY-MM-DD'
ALTER TABLE security ADD COLUMN option_type       TEXT
    CHECK (option_type IN ('call', 'put') OR option_type IS NULL);
ALTER TABLE security ADD COLUMN contract_size     REAL;  -- shares per contract

-- ── txn: accrued interest paid at a bond purchase ────────────────────────────
--
-- Pence of accrued interest handed to the seller for the period since the last
-- coupon. It is part of the CASH paid (so txn.amount, the signed cash impact,
-- includes it and `cash balance = SUM(amount)` still holds) but NOT part of the
-- bond's cost basis — the holdings engine subtracts it back out
-- (basis = abs(amount) − accrued_interest). NULL on every non-bond row.
ALTER TABLE txn ADD COLUMN accrued_interest INTEGER;
