-- ─────────────────────────────────────────────────────────────────────────────
-- 0020 — Credit cards as a budget pool source (ADR-058 R4a)
--
-- Two additive columns, no data migration beyond column defaults:
--
--   account.credit_limit — the card's limit in pence (NULL = not a card / not
--     set). Available credit is then `credit_limit + balance` (balance is
--     signed; a card you owe £1,800 on with a £5,000 limit has balance −1800,
--     so available = 5000 + (−1800) = 3200).
--
--   budget_account.contribution — how each perimeter account feeds the pool
--     (ADR-058 D2). Membership in budget_account still defines the perimeter
--     for *actuals* (a transaction on the account counts regardless); this
--     column governs only the *pool* figure:
--       'balance'          — the account's balance feeds the pool (default;
--                            preserves every existing budget's behaviour)
--       'available_credit' — the card's available credit feeds the pool
--       'excluded'         — in the perimeter for actuals, but contributes
--                            nothing to the pool
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE account ADD COLUMN credit_limit INTEGER;

ALTER TABLE budget_account ADD COLUMN contribution TEXT NOT NULL DEFAULT 'balance'
    CHECK (contribution IN ('balance', 'available_credit', 'excluded'));
