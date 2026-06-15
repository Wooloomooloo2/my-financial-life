-- ADR-077: bank feeds (Arc H). Maps an MFL account to a provider account
-- (e.g. a GoCardless Bank Account Data account) so an automatic feed can
-- pull transactions through the existing import pipeline.
--
-- One feed per MFL account in v1 (UNIQUE(account_id)); the schema allows
-- several providers across the file. Provider credentials (the GoCardless
-- secret_id / secret_key) live in the `setting` table (ADR-035), not here.
-- Access/refresh tokens are never persisted — re-minted per session.

CREATE TABLE feed_account (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,            -- 'gocardless'
    external_account_id TEXT NOT NULL,            -- the provider's account id
    requisition_id      TEXT,                     -- the consent/requisition id
    institution_id      TEXT,                     -- the bank's provider id
    institution_name    TEXT,
    status              TEXT NOT NULL DEFAULT 'linked'
                        CHECK(status IN ('linked', 'expired', 'error')),
    last_synced_at      TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id),
    UNIQUE(provider, external_account_id)
);

CREATE INDEX idx_feed_account_account ON feed_account(account_id);
