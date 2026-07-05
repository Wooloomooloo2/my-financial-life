# ADR-139 — Reconcile transfers that live inside split lines

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-037 (two-account transfer reconcile — `find_transfer_pairs`). ADR-036 (`bulk_match_or_create_transfers` / `LinkExisting`). ADR-051 (split transactions — `txn_split`, `_make_split_line_transfer`). ADR-095 (loan payments already create principal-transfer + interest splits — the same shape this now *matches*).

## Context

The owner imported a **Coop mortgage** and tried to reconcile transfers against **Smile Current**, from which the payments were made. Very few matched. The reason: each payment is a **split** — a principal amount (the transfer to the mortgage) and interest (a separate expense) on one row. Example: on 2012-09-15 a **£700** payment split into **£460.26 principal** + **£239.74 interest**; the mortgage shows a **£460.26** credit.

`find_transfer_pairs` (ADR-037) only ever fetched **whole `txn` rows**, so the source side's amount was the **£700 parent total**, which never matches the £460.26 mortgage credit. The transfer the user wants to reconcile is the **£460.26 split line**, not the whole payment.

The data model already supports split-line transfers: `_make_split_line_transfer` (ADR-051) stamps `txn_split.transfer_id` and a counterpart `txn` sharing that iri (this is exactly how loan payments book principal, ADR-095). The gap was purely in the **matcher** (it didn't offer split lines) and the **link write-path** (it only stamped whole `txn` rows).

## Decision

Teach the reconcile-transfers engine to treat **split lines as first-class transfer candidates**.

- **Candidate pool** — a new `_transfer_candidate_rows(account_id, include_splits)` returns, per account, every unlinked whole non-split txn **plus** each unlinked `txn_split` line (its own signed amount, the parent's date + payee, the line memo). Split **parents** are excluded from the whole-txn set — only their lines compete, at the right granularity. Split lines join the pool **only when the two accounts share a currency** (a split-line transfer is modelled same-currency, rate = 1); cross-currency pairs keep the whole-txn-only behaviour.
- **Pairing** — unchanged scoring. Each candidate is keyed for the greedy claim by `(parent_txn_id, split_id or 0)`, so two lines of one split (and any whole candidates) never collide. In the owner's case the £460.26 principal line matches the £460.26 credit (Strong); the £239.74 interest line finds no counterpart and is left alone.
- **`TransferPair`** gains `source_split_id`/`source_split_memo` (+ target equivalents); `*_txn_id` then names the split's **parent**. **`LinkExisting`** gains `source_split_id`/`candidate_split_id`.
- **Write-path** — `_link_transfer_unbatched` is generalised: a `_read_transfer_side` helper reads either a `txn` or a `txn_split` line, and the transfer_id + category are stamped on whichever table each side is. The result is byte-identical to what `_make_split_line_transfer` produces (split line + counterpart share the iri; a `transfer` parent row records from/to/rate=1; the split parent's `transfer_id` stays NULL), so the register and split editor render it with no further change — the principal line shows **"Transfer to Coop Mortgage"**.
- **Dialog** — the reconcile table tags a split side inline ("· split: Principal") with a tooltip, and threads the split ids into the apply plan.

Rejected: matching the split **parent total** (semantically wrong — a split's money went to several places; the app never puts a transfer on a split parent); supporting cross-currency split-line transfers now (the split-transfer model is same-currency only — out of scope); a separate "split reconcile" screen (the existing two-account dialog now just works).

## Consequences

- Mortgage/loan-style payments entered (or imported) as principal-plus-interest splits reconcile against the other account for the **principal line**, which was previously impossible — the owner's Coop ↔ Smile case now matches.
- No schema change or migration — `txn_split.transfer_id` already existed; this only fills the matcher + link paths. `find_transfer_pairs`' candidate set is larger (every unlinked split line), still bounded by the same date-window / amount filters.
- Linking never creates or deletes a row (both halves pre-exist), so **balances are unchanged**; it only stamps `transfer_id` + category and inserts the `transfer` parent.
- `tests/test_transfer_reconcile_splits.py` 3/3 (the £460.26 principal line becomes a Strong candidate while the interest line doesn't; linking stamps `txn_split` + the counterpart with a shared iri, writes the `transfer` parent, and the split editor shows "Transfer to Coop"; a linked line drops from a second pass). Full suite 29/29; reconcile dialog screenshotted showing the split-tagged pair.
