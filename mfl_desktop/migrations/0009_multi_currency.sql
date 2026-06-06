-- 0009_multi_currency.sql — multi-currency foundation (ADR-035).
--
-- Three new tables, no changes to existing schema:
--
--   setting   — file-level key/value pairs (API key, last refresh time,
--               transfer-matching tunables, future prefs)
--   fx_rate   — exchange rates by (date, base, quote) with provider
--               provenance. Daily granularity. USD-base from
--               openexchangerates.org; bilateral pairs allowed for
--               manual entries.
--   transfer  — parent row per transfer (matches txn.transfer_id) holding
--               the exchange rate that was used at posting time and its
--               provenance. Same-currency transfers populate rate=1.0
--               with source='derived'. The two txn amounts on either
--               side remain the truth-of-money; transfer.rate is the
--               truth-of-intent.
--
-- account.currency and person.base_currency already exist (added in 0001)
-- — no ALTER needed. txn does NOT gain a currency column; a txn's currency
-- is its account's currency (single source of truth).

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- setting — flat key/value, namespaced by prefix (oxr_*, transfer_match_*).
-- Complex objects do NOT belong here; if a setting grows beyond a scalar
-- it gets its own table (see ADR-035 §setting discipline).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- fx_rate — one row per (date, base, quote). Provider rows (source =
-- 'openexchangerates') all have base='USD' under the free-tier constraint.
-- Manual entries can be in either direction. 'derived' is reserved for
-- back-derived rates (e.g. computed cross-rates that were materialised
-- into the table — not used today; the cross-rate path computes on the fly).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE fx_rate (
    date   TEXT NOT NULL,
    base   TEXT NOT NULL,
    quote  TEXT NOT NULL,
    rate   REAL NOT NULL CHECK(rate > 0),
    source TEXT NOT NULL DEFAULT 'manual'
           CHECK(source IN ('manual','openexchangerates','derived')),
    PRIMARY KEY (date, base, quote)
);

-- Lookup index for nearest-prior fallback: WHERE base=? AND quote=? AND
-- date <= ? ORDER BY date DESC LIMIT 1.
CREATE INDEX idx_fx_rate_pair_date
    ON fx_rate(base, quote, date DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- transfer — parent row for each transfer pair. PRIMARY KEY iri matches
-- txn.transfer_id (TEXT, set by ADR-020 migration 0004). No FK to txn —
-- the id space is conceptual, same pattern txn.transfer_id already uses.
--
-- rate convention: to_amount_magnitude = from_amount_magnitude * rate.
-- (For same-currency transfers, rate = 1.0.) The actual signed amounts
-- live on the two txn rows; this row stores what rate was intended.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE transfer (
    iri              TEXT PRIMARY KEY,
    from_account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    to_account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    rate             REAL NOT NULL DEFAULT 1.0 CHECK(rate > 0),
    rate_source      TEXT NOT NULL DEFAULT 'derived'
                     CHECK(rate_source IN ('derived','manual','fx_rate')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_transfer_from ON transfer(from_account_id);
CREATE INDEX idx_transfer_to   ON transfer(to_account_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Backfill: every existing transfer pair gets a transfer row with
-- rate=1.0, source='derived'. All pre-0009 transfers are same-currency
-- (no FX wiring existed before this migration), so rate=1.0 is correct
-- by construction. The join picks the outflow side (amount < 0) as the
-- "from" and the inflow side (amount > 0) as the "to" — matches the
-- ADR-020 convention.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO transfer (iri, from_account_id, to_account_id, rate, rate_source)
SELECT
    out_t.transfer_id            AS iri,
    out_t.account_id             AS from_account_id,
    in_t.account_id              AS to_account_id,
    1.0                          AS rate,
    'derived'                    AS rate_source
FROM      txn out_t
JOIN      txn in_t   ON in_t.transfer_id = out_t.transfer_id
                    AND in_t.id != out_t.id
                    AND in_t.amount > 0
WHERE out_t.amount < 0
  AND out_t.transfer_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Seed defaults for the settings the new code reads.
-- transfer_match_window_days: ±N day window for transfer-matching (ADR-036).
-- transfer_fx_tolerance_pct:  cross-currency rate-deviation tolerance (ADR-036).
-- oxr_api_key:                openexchangerates.org API key (user enters).
-- oxr_last_refresh_at:        ISO timestamp of the last successful rate fetch.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO setting (key, value) VALUES
    ('transfer_match_window_days', '3'),
    ('transfer_fx_tolerance_pct',  '1.0'),
    ('oxr_api_key',                ''),
    ('oxr_last_refresh_at',        '');
