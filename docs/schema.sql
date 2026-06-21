-- My Financial Life — reference SQLite schema (v0.2 rewrite)
--
-- This file is the implementation of ADR-010. It is the source of truth for
-- the initial schema and will be copied into mfl_desktop/migrations/0001_initial.sql
-- when the rewrite begins.
--
-- Conventions:
--   * INTEGER id PRIMARY KEY on every table — used for all FK joins.
--   * TEXT iri UNIQUE NOT NULL on every entity that needs cross-app identity (ADR-006).
--   * Currency amounts in INTEGER minor units (pence). REAL only for non-currency quantities.
--   * Dates ISO 8601 in TEXT. Timestamps with seconds.
--   * Repository sets PRAGMA foreign_keys = ON, journal_mode = WAL, synchronous = NORMAL.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- Schema versioning
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Person — the single profile (one row in v1)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE person (
    id            INTEGER PRIMARY KEY,
    iri           TEXT UNIQUE NOT NULL,    -- 'mrl:Person_1'
    name          TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'GBP'
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Account
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE account (
    id               INTEGER PRIMARY KEY,
    iri              TEXT UNIQUE NOT NULL,   -- e.g. 'mrl:CashAccount_1'
    name             TEXT NOT NULL,
    type             TEXT NOT NULL,          -- 'cash_std' | 'savings_std' | 'credit_std' | 'investment_std' | 'property_std' | 'vehicle_std' | 'loan_std'
    family           TEXT NOT NULL,          -- 'cash' | 'credit' | 'investment' | 'property' | 'vehicle' | 'loan' (derived from type, denormalised for fast filtering)
    currency         TEXT NOT NULL,
    is_liability     INTEGER NOT NULL DEFAULT 0 CHECK(is_liability IN (0,1)),
    opening_balance  INTEGER NOT NULL DEFAULT 0,   -- pence
    opened_on        TEXT,                          -- ISO 8601 date
    archived_at      TEXT,                          -- ISO 8601 timestamp; NULL = active
    -- credit_limit (ADR-058 R4a) + the loan terms table (ADR-095) are added by
    -- later migrations; loan_std is an amortizing-loan liability.
    CHECK (type IN ('cash_std','savings_std','credit_std','investment_std','property_std','vehicle_std','loan_std'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Category — hierarchical, dual-sourced (ADR-010 §4)
--
-- 'Uncategorised' is the one row that the Repository refuses to delete.
-- When any other category is deleted, the Repository re-points its referencing
-- transactions to Uncategorised, then deletes the row.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE category (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES category(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK(source IN ('system','user','import')),
    archived_at TEXT,
    UNIQUE (parent_id, name)
);

CREATE INDEX idx_category_parent ON category(parent_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Payee
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE payee (
    id                  INTEGER PRIMARY KEY,
    name                TEXT UNIQUE NOT NULL,
    default_category_id INTEGER REFERENCES category(id) ON DELETE SET NULL,
    archived_at         TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Import batch — provenance for imported transactions
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE import_batch (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,             -- 'mfl:ImportBatch_<uuid8>'
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    source_format   TEXT NOT NULL,                    -- 'ofx' | 'qfx' | 'csv-banktivity' | 'csv-creditcard' | 'csv-generic' | 'qif'
    source_filename TEXT,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
    new_count       INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    matched_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_import_batch_account ON import_batch(account_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Transaction (named `txn` to avoid the SQL reserved word)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE txn (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,           -- 'mfl:Transaction_<uuid8>'
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    posted_date     TEXT NOT NULL,                  -- ISO 8601 date
    amount          INTEGER NOT NULL,               -- pence; positive=credit, negative=debit
    payee_id        INTEGER REFERENCES payee(id) ON DELETE SET NULL,
    category_id     INTEGER NOT NULL REFERENCES category(id),  -- Uncategorised by default; deletion re-pointed in code
    status          TEXT NOT NULL CHECK(status IN ('Pending','Uncleared','Cleared','Reconciled')),
    memo            TEXT,
    import_hash     TEXT,                           -- OFX FITID or composite MD5; NULL for manual entries
    import_batch_id INTEGER REFERENCES import_batch(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_txn_account_date ON txn(account_id, posted_date);
CREATE INDEX idx_txn_category     ON txn(category_id);
CREATE INDEX idx_txn_payee        ON txn(payee_id);
CREATE INDEX idx_txn_status       ON txn(status);

-- Partial unique index: an import_hash may only appear once per account.
-- NULL hashes (manual entries) are unconstrained.
CREATE UNIQUE INDEX idx_txn_import_hash
    ON txn(account_id, import_hash)
    WHERE import_hash IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Lot — per-lot holdings for investment accounts
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE lot (
    id          INTEGER PRIMARY KEY,
    iri         TEXT UNIQUE NOT NULL,               -- 'mfl:Lot_<uuid8>'
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    open_date   TEXT NOT NULL,                      -- ISO 8601 date
    quantity    REAL NOT NULL,                      -- units; not currency
    unit_cost   REAL NOT NULL,                      -- price per unit at acquisition; not currency
    close_date  TEXT,                               -- NULL = open lot
    notes       TEXT
);

CREATE INDEX idx_lot_account_symbol ON lot(account_id, symbol);
CREATE INDEX idx_lot_open           ON lot(account_id, close_date) WHERE close_date IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Valuation — mark-to-market for investment and property accounts
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE valuation (
    id         INTEGER PRIMARY KEY,
    iri        TEXT UNIQUE NOT NULL,                -- 'mfl:Valuation_<uuid8>'
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    valued_on  TEXT NOT NULL,                       -- ISO 8601 date
    value      INTEGER NOT NULL,                    -- pence
    notes      TEXT
);

CREATE INDEX idx_valuation_account_date ON valuation(account_id, valued_on);

-- ─────────────────────────────────────────────────────────────────────────────
-- Rule — payee/memo pattern → category and/or payee assignment (v0.2)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE rule (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,           -- 'mfl:Rule_<uuid8>'
    pattern         TEXT NOT NULL,
    pattern_kind    TEXT NOT NULL CHECK(pattern_kind IN ('substring','regex')),
    match_field     TEXT NOT NULL CHECK(match_field IN ('payee_raw','memo')),
    set_category_id INTEGER REFERENCES category(id) ON DELETE CASCADE,
    set_payee_id    INTEGER REFERENCES payee(id) ON DELETE CASCADE,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- At least one assignment column must be non-NULL
    CHECK (set_category_id IS NOT NULL OR set_payee_id IS NOT NULL)
);

CREATE INDEX idx_rule_priority ON rule(priority);

-- ─────────────────────────────────────────────────────────────────────────────
-- Seed data
--   * Uncategorised — the reserved deletion-sink row, id will be referenced
--     by the Repository's CategoryGuard.
--   * Income / Expense top-levels with v0.1 subcategories — system defaults,
--     deletable like any other category.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO category (id, parent_id, name, source) VALUES
    (1, NULL, 'Uncategorised', 'system'),
    (2, NULL, 'Income',        'system'),
    (3, NULL, 'Expense',       'system');

INSERT INTO category (parent_id, name, source) VALUES
    -- Income children
    (2, 'Benefits / state payments',  'system'),
    (2, 'Freelance / self-employment','system'),
    (2, 'Investment income',          'system'),
    (2, 'Other income',               'system'),
    (2, 'Rental income',              'system'),
    (2, 'Salary',                     'system'),
    -- Expense children
    (3, 'Charity and gifts',          'system'),
    (3, 'Childcare',                  'system'),
    (3, 'Dining out',                 'system'),
    (3, 'Education',                  'system'),
    (3, 'Groceries',                  'system'),
    (3, 'Healthcare',                 'system'),
    (3, 'Holidays and travel',        'system'),
    (3, 'Housing',                    'system'),
    (3, 'Insurance',                  'system'),
    (3, 'Other expense',              'system'),
    (3, 'Savings and investments',    'system'),
    (3, 'Shopping',                   'system'),
    (3, 'Subscriptions',              'system'),
    (3, 'Transport',                  'system'),
    (3, 'Utilities',                  'system');

INSERT INTO schema_version (version) VALUES (1);
