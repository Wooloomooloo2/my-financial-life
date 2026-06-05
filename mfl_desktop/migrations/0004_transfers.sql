-- 0004_transfers.sql — adds transfer linking between two txn rows.
-- See ADR-020. A transfer is one user intent ("move £X from A to B")
-- realised as two txns: a -£X outflow on the source account and a +£X
-- inflow on the destination. Both rows share a `transfer_id` so the UI
-- and reports can treat the pair as one logical operation (e.g. delete
-- one half → delete both; cashflow/spending reports ignore the pair).

PRAGMA foreign_keys = ON;

ALTER TABLE txn ADD COLUMN transfer_id TEXT;

CREATE INDEX idx_txn_transfer ON txn(transfer_id) WHERE transfer_id IS NOT NULL;
