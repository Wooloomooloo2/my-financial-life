-- 0018_split_line_transfers.sql — transfer categories on split lines
-- (ADR-051 amendment, 2026-06-10).
--
-- ADR-051 made "a split is never a transfer" a hard invariant: split lines were
-- pure CATEGORY attribution, and the per-line category picker excluded
-- transfer-kind categories. The owner wants a split line to be able to use a
-- transfer category and pick the account it transfers to — e.g. a £100 payment
-- split into −£70 Groceries + −£30 "Transfer to Savings".
--
-- DESIGN — a transfer line spawns a REAL partner `txn` in the destination
-- account (a `txn_split` row alone moves no money out of the source account), so
-- the destination balance is correct. The split's parent `txn` still carries the
-- full signed total, so the SOURCE balance and every money-layer query are
-- untouched, exactly as in ADR-051. The link is per-LINE, not per-parent: a new
-- nullable `txn_split.transfer_id` mirrors `txn.transfer_id` and is shared by the
-- line and its partner txn; a `transfer` parent row records direction + rate.
--
-- Scope (v1): same-currency transfers only (rate = 1.0, 'derived'). The
-- destination account's currency must match the split's account; cross-currency
-- split-line transfers are deferred (the FX layer is separately incomplete).
--
-- The single place "what a split spends on" is defined is the `txn_category_line`
-- view. Until now its split-line branch sourced `transfer_id` from the PARENT
-- (always NULL for a split). It now sources it from the LINE, so the budget
-- perimeter's transfer-cancellation (`NOT EXISTS … t2 …` in list_perimeter_txns)
-- sees a split-line transfer's partner and cancels/counts it correctly, with no
-- change to that query. Spending aggregates filter `category.kind = 'expense'`,
-- so transfer-kind lines drop out of spend with no change there either.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- txn_split.transfer_id — the shared id linking a transfer LINE to its partner
-- `txn` in the destination account. NULL for an ordinary category line. No FK
-- (same conceptual-id convention as txn.transfer_id, migrations 0004/0009).
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE txn_split ADD COLUMN transfer_id TEXT;

CREATE INDEX idx_txn_split_transfer
    ON txn_split(transfer_id) WHERE transfer_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Recreate txn_category_line so a SPLIT line exposes its OWN transfer_id
-- (`s.transfer_id`) rather than the parent's. A non-split txn maps to itself and
-- still exposes `t.transfer_id`. Every other column is unchanged from 0017, so
-- the FROM-swap callers (spending_aggregates, list_perimeter_txns,
-- distinct_category_ids_for_account, the usage-count queries) need no edits.
-- ─────────────────────────────────────────────────────────────────────────────

DROP VIEW txn_category_line;

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
         s.transfer_id AS transfer_id,   -- line's own transfer link (ADR-051 amendment)
         t.status      AS status
    FROM txn t
    JOIN txn_split s ON s.txn_id = t.id;
