-- 0002_category_kind.sql — adds the per-category kind column and the Transfer seed.
-- See ADR-014 for the rationale (each category carries one of income/expense/transfer,
-- which reports use to interpret signed amounts: a positive amount on an expense
-- category is a refund, a negative amount on income is a correction, etc.).

PRAGMA foreign_keys = ON;

-- 1. Add the kind column. NOT NULL with a default so the ALTER succeeds against
--    existing rows; the backfill below replaces the default with the right value
--    derived from each row's root ancestor.

ALTER TABLE category
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'expense'
        CHECK (kind IN ('income', 'expense', 'transfer'));

-- 2. Backfill. The seed in 0001 created Uncategorised (id=1), Income (id=2),
--    Expense (id=3) as top-level rows; every other seeded category sits under
--    Income or Expense. Recursive CTE finds each category's root, and we map
--    Income's root → income, everything else → expense (Uncategorised's own
--    kind is expense per ADR-014 — non-technical user defaulting).

WITH RECURSIVE root_of(id, root_id) AS (
    SELECT id, id FROM category WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, r.root_id
      FROM category c
      JOIN root_of r ON c.parent_id = r.id
)
UPDATE category
SET kind = CASE
    WHEN (SELECT root_id FROM root_of WHERE root_of.id = category.id) = 2 THEN 'income'
    ELSE 'expense'
END;

-- 3. Seed Transfer as the third reserved top-level system category.
--    No subcategories seeded — the user adds the ones that match how they
--    move money between their own accounts (e.g. "Between own accounts",
--    "Mortgage principal", etc.).

INSERT INTO category (parent_id, name, source, kind) VALUES
    (NULL, 'Transfer', 'system', 'transfer');
