-- ADR-138: budget funding model + drop the credit-limit-as-funds contribution.
--
-- (1) budget.funding_mode — how the available pool is seeded:
--       'balances' (default, current behaviour): the perimeter accounts'
--                  balances;
--       'income'  : only income into the perimeter accounts over the budget
--                  period ("new money"), ignoring starting balances.
--
-- (2) drop 'available_credit' from budget_account.contribution. Treating a
--     card's credit *limit* as spendable funds is bad practice; a credit card
--     now contributes its (signed) balance like any other account, so its debt
--     reduces the pool rather than its limit inflating it. Existing
--     'available_credit' rows migrate to 'balance'. SQLite can't ALTER a CHECK,
--     so the table is rebuilt (ADR-032 recipe) to drop the value.

ALTER TABLE budget ADD COLUMN funding_mode TEXT NOT NULL DEFAULT 'balances'
    CHECK (funding_mode IN ('balances', 'income'));

PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

CREATE TABLE budget_account_new (
    budget_id    INTEGER NOT NULL REFERENCES budget(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    contribution TEXT NOT NULL DEFAULT 'balance'
        CHECK (contribution IN ('balance', 'excluded')),
    PRIMARY KEY (budget_id, account_id)
);

INSERT INTO budget_account_new (budget_id, account_id, contribution)
SELECT budget_id, account_id,
       CASE contribution WHEN 'available_credit' THEN 'balance'
                         ELSE contribution END
FROM budget_account;

DROP TABLE budget_account;
ALTER TABLE budget_account_new RENAME TO budget_account;

PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
