-- 0017_split_transactions.sql — split transactions (ADR-051).
--
-- A "split" is one transaction (one payee / date / account / total) whose total
-- is broken into several category lines. Banktivity, Quicken, and QIF all model
-- this; until now the Banktivity CSV importer threw the detail away — it
-- stringified the sub-lines into the memo and filed the whole transaction under
-- Uncategorised, so reports/budgets/Top-Categories mis-attributed it.
--
-- DESIGN — the parent `txn` row keeps the full signed `amount`; the lines live
-- in a child `txn_split` table. This is deliberate: every money-movement query
-- reads SUM(txn.amount) (running balance, compute_account_balances,
-- balance_as_of, statement_residual, holdings, net worth) and NONE of them
-- change. A split is invisible to the money layer and reconciles against the
-- bank statement as one row at its full total. Only CATEGORY attribution has to
-- unroll a split into its lines (Spending report, budget actuals, Top
-- Categories) — and the `txn_category_line` view below is the single place that
-- "unroll" is defined.
--
-- INVARIANT: SUM(txn_split.amount WHERE txn_id = X) == txn.amount.
-- A txn "is a split" iff it has at least one txn_split row; its own
-- `txn.category_id` then stays at the Uncategorised sink (id = 1) and is not
-- meaningful. Line amounts are SIGNED pence (same convention as txn.amount), so
-- a -£120 groceries line + a +£20 cashback line net to the -£100 total.
--
-- Scope (v1): cash / bank / credit accounts only. Investment rows (ADR-048
-- action/security/qty/price) get no splits. A split is never a transfer
-- (mutually exclusive — enforced in the UI).

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- txn_split — one row per category line of a split transaction.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE txn_split (
    id          INTEGER PRIMARY KEY,
    txn_id      INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category(id),  -- mirrors txn.category_id: no ON DELETE action;
                                                           -- the category-delete/merge path repoints to the
                                                           -- Uncategorised sink, same as txn rows.
    memo        TEXT,                                      -- per-line note (QIF `E`, Banktivity sub-row); nullable
    amount      INTEGER NOT NULL,                          -- SIGNED pence, same convention as txn.amount
    sort_order  INTEGER NOT NULL DEFAULT 0                 -- preserves the line order the user/import entered
);

CREATE INDEX idx_txn_split_txn ON txn_split(txn_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- txn_category_line — the canonical "category attribution" source.
--
-- A non-split txn maps to itself (its own category_id + amount). A split txn
-- explodes into one row per line (the line's category_id + the line's amount).
-- Category-attributing queries (spending_aggregates, list_perimeter_txns) read
-- FROM this view instead of FROM txn, so a split's spend lands on its line
-- categories. The view exposes the same column names a plain `txn` scan used, so
-- those rewrites are a one-line FROM swap.
--
-- NOTE: `amount` here is the per-line amount, NOT the parent total. Never sum
-- this view for an account balance — that is what the base `txn` table is for.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE VIEW txn_category_line AS
  SELECT t.id          AS txn_id,
         t.account_id  AS account_id,
         t.posted_date AS posted_date,
         t.amount      AS amount,
         t.category_id AS category_id,
         t.payee_id    AS payee_id,
         t.transfer_id AS transfer_id,
         t.status      AS status
    FROM txn t
   WHERE NOT EXISTS (SELECT 1 FROM txn_split s WHERE s.txn_id = t.id)
  UNION ALL
  SELECT t.id          AS txn_id,
         t.account_id  AS account_id,
         t.posted_date AS posted_date,
         s.amount      AS amount,
         s.category_id AS category_id,
         t.payee_id    AS payee_id,
         t.transfer_id AS transfer_id,
         t.status      AS status
    FROM txn t
    JOIN txn_split s ON s.txn_id = t.id;
