-- Initial schema for My Financial Life desktop (per ADR-010).
-- The migration runner records the applied version in schema_version;
-- this file deliberately does not manage that table.

PRAGMA foreign_keys = ON;

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
    iri              TEXT UNIQUE NOT NULL,
    name             TEXT NOT NULL,
    type             TEXT NOT NULL,
    family           TEXT NOT NULL,
    currency         TEXT NOT NULL,
    is_liability     INTEGER NOT NULL DEFAULT 0 CHECK(is_liability IN (0,1)),
    opening_balance  INTEGER NOT NULL DEFAULT 0,
    opened_on        TEXT,
    archived_at      TEXT,
    CHECK (type IN ('cash_std','savings_std','credit_std','investment_std','property_std'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Category — hierarchical, dual-sourced. 'Uncategorised' (id=1) is the one
-- non-deletable row; the Repository refuses to delete it and re-points
-- referencing transactions to it when other categories are deleted.
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
-- Import batch
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE import_batch (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    source_format   TEXT NOT NULL,
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
    iri             TEXT UNIQUE NOT NULL,
    account_id      INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    posted_date     TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    payee_id        INTEGER REFERENCES payee(id) ON DELETE SET NULL,
    category_id     INTEGER NOT NULL REFERENCES category(id),
    status          TEXT NOT NULL CHECK(status IN ('Pending','Uncleared','Cleared','Reconciled')),
    memo            TEXT,
    import_hash     TEXT,
    import_batch_id INTEGER REFERENCES import_batch(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_txn_account_date ON txn(account_id, posted_date);
CREATE INDEX idx_txn_category     ON txn(category_id);
CREATE INDEX idx_txn_payee        ON txn(payee_id);
CREATE INDEX idx_txn_status       ON txn(status);

CREATE UNIQUE INDEX idx_txn_import_hash
    ON txn(account_id, import_hash)
    WHERE import_hash IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Lot (investment per-lot cost basis)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE lot (
    id          INTEGER PRIMARY KEY,
    iri         TEXT UNIQUE NOT NULL,
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    open_date   TEXT NOT NULL,
    quantity    REAL NOT NULL,
    unit_cost   REAL NOT NULL,
    close_date  TEXT,
    notes       TEXT
);

CREATE INDEX idx_lot_account_symbol ON lot(account_id, symbol);
CREATE INDEX idx_lot_open           ON lot(account_id, close_date) WHERE close_date IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Valuation (mark-to-market for investment / property)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE valuation (
    id         INTEGER PRIMARY KEY,
    iri        TEXT UNIQUE NOT NULL,
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    valued_on  TEXT NOT NULL,
    value      INTEGER NOT NULL,
    notes      TEXT
);

CREATE INDEX idx_valuation_account_date ON valuation(account_id, valued_on);

-- ─────────────────────────────────────────────────────────────────────────────
-- Rule (auto-categorisation, v0.2 backlog item but schema reserved now)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE rule (
    id              INTEGER PRIMARY KEY,
    iri             TEXT UNIQUE NOT NULL,
    pattern         TEXT NOT NULL,
    pattern_kind    TEXT NOT NULL CHECK(pattern_kind IN ('substring','regex')),
    match_field     TEXT NOT NULL CHECK(match_field IN ('payee_raw','memo')),
    set_category_id INTEGER REFERENCES category(id) ON DELETE CASCADE,
    set_payee_id    INTEGER REFERENCES payee(id) ON DELETE CASCADE,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (set_category_id IS NOT NULL OR set_payee_id IS NOT NULL)
);

CREATE INDEX idx_rule_priority ON rule(priority);

-- ─────────────────────────────────────────────────────────────────────────────
-- Seed: the reserved Uncategorised row (id=1) + system default categories
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO category (id, parent_id, name, source) VALUES
    (1, NULL, 'Uncategorised', 'system'),
    (2, NULL, 'Income',        'system'),
    (3, NULL, 'Expense',       'system');

INSERT INTO category (parent_id, name, source) VALUES
    (2, 'Benefits / state payments',  'system'),
    (2, 'Freelance / self-employment','system'),
    (2, 'Investment income',          'system'),
    (2, 'Other income',               'system'),
    (2, 'Rental income',              'system'),
    (2, 'Salary',                     'system'),
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
