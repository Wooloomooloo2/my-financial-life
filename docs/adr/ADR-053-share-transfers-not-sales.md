# ADR-053 — In-kind share transfers (ShrsIn / ShrsOut) are not sales

**Date:** 2026-06-10
**Status:** Accepted
**Related:** ADR-043 (action→cash-sign mapping; ShrsIn/ShrsOut carry $0 cash), ADR-044 (FIFO holdings engine + the `basis_incomplete` flag + the deferred "whole-account XIn/XOut transfers don't move lots — round 4" note), ADR-046 (Investment Returns report — the screen where the bug surfaced), ADR-052 (merge securities — the prerequisite for transfers whose legs were recorded under two records).

---

## Context

The owner moved a brokerage position from Morgan Stanley to E\*Trade as an **in-kind transfer** — the shares moved custodian, nothing was sold. In QIF/Banktivity terms that's a `ShrsOut` from the MS account and a matching `ShrsIn` into the E\*Trade account, each carrying a share quantity and **$0 cash**.

The Investment Returns report showed TSBIX with a **−$52,865.64 realized loss** the owner never incurred (a giant red spike on the chart). Root cause: the holdings engine put `shrsout` in `SHARE_OUT_ACTIONS` alongside `sell`/`sellx`, so a `ShrsOut` was processed as a **disposal**:

```
realized = proceeds − cost_removed = $0 (no cash on a transfer) − (full cost basis) = a large loss
```

The matching `ShrsIn` (no price on a transfer-in) re-added the shares at **zero cost basis**, so the receiving account then showed a phantom *unrealized gain*. Net: a transfer — which by definition changes neither realized gain nor total return — was booked as a big realized loss plus an offsetting unrealized gain.

Two complications surfaced in the real data:

1. **The report computes per account.** `ShrsOut` (MS) and `ShrsIn` (E\*Trade) land in separate `compute_returns` calls, so any basis carry-over has to span accounts.
2. **The funds were renamed on the move.** `security.name` is unique and the importer keys on it (ADR-043), so the `ShrsOut` was recorded under the *old* name (one `security` record) and the `ShrsIn` under the *new* name (a different record): e.g. "CRA Qualified Investment" → "CCM Community Impact Bond" (CRANX); two "PIMCO Enhanced Short Maturity" records both ticker EMNT; "iShares MSCI KLD 400" → DSI. So the two legs of one transfer sit on **different `security` ids**.

## Options considered

**(A) Status quo — `ShrsOut` is a sale.** Rejected: books phantom realized losses on every in-kind transfer (the bug). The owner's whole portfolio carried **−$265k** of such phantom realized losses.

**(B) Skip `ShrsIn`/`ShrsOut` entirely, like the deferred `XIn`/`XOut` handling (ADR-044).** Rejected: those actions carry real share quantities and *must* move the position — skipping them would leave shares in the wrong account (a sold-out MS position would still show shares; the E\*Trade position would be missing them).

**(C) Treat `ShrsIn`/`ShrsOut` as transfers: remove/add shares with no realized gain, and carry cost basis across the matched legs.** Chosen. A `ShrsOut` parks its FIFO lots in a per-security "transfer pen"; a later matching `ShrsIn` pulls its basis from the pen. A clean out→in transfer nets to zero realized and preserves cost basis. This is the lightweight, same-currency, single-replay form of the deferred "transfer-linking" work — it does not need the `transfer`-parent plumbing or cross-currency rates.

**(D) Full transfer-linking (round 4): explicitly pair the two legs via a `transfer_id`, across currencies, persisted.** Deferred. Option (C) covers the real case (same-currency in-kind moves) without the schema/UX weight; cross-currency in-kind transfers remain out of scope.

## Decision

- **`qif_actions.py`** — add `SHARE_TRANSFER_ACTIONS = {"shrsin", "shrsout"}` + `is_share_transfer()`. These stay within the share-in/share-out sets (a `ShrsIn` still adds shares, a `ShrsOut` still removes them) — the new predicate only changes *how the basis/realization* is handled.
- **`holdings.py`** — two shared helpers, `_transfer_out` (pop FIFO lots into a per-security pen, basis preserved) and `_transfer_in` (pull carried-basis lots from the pen), used by all three engines:
  - `compute_holdings_view`: `ShrsOut` → pen, **zero realized**; `ShrsIn` → pen first, then explicit price / unknown basis for any remainder.
  - `compute_returns`: same, so the returns report's realized/unrealized split is correct.
  - `compute_value_history`: same, so the Overview cost-basis line doesn't drop on a transfer.
  A `ShrsOut` with no matching `ShrsIn` in the replay leaves its lots parked (shares removed, **no phantom loss**) and flags `basis_incomplete`.
- **`investment_returns_window.py`** — group the selected accounts **by currency** and run one `compute_returns` per currency group (was one per account). Pooling same-currency accounts is what lets a transfer between two *accounts* net (both legs in one replay). Conversion stays correct because a rate depends only on the currency, not the account.
- **Dependency on ADR-052:** a transfer whose legs were recorded under **two `security` records** (renamed fund) only nets once those records are **merged** into one — then both legs share a `security_id` and the pen matches them. The engine fix and the merge verb are complementary: the fix removes the phantom losses unconditionally; the merge is required for cross-record transfers to also preserve basis.

## Consequences

- **Matched same-currency transfers net correctly:** no phantom realized gain/loss, cost basis carried across. TSBIX (both legs already on one record) is fully fixed — **total return unchanged at $19,865.56**, but realized **−$52,865 → +$11,593** and unrealized **−$64,492 → +$33** (the total-return invariance is the proof the change is conservative: a transfer can't change total return, only its split).
- **Cross-record transfers need a merge first.** Until the renamed-fund duplicates are merged (ADR-052), those funds show the `ShrsOut` shares vanishing from the old record and the `ShrsIn` shares entering the new record at zero basis (a phantom *unrealized gain*, flagged `basis_incomplete`). After merging the pair the transfer nets — verified on EMNT (held cost $2,109 → $59,474; unrealized $57,750 → $385) and CRANX (held cost $1,630 → $51,532; unrealized $50,518 → $616).
- **The Stock Record was already correct** for matched transfers because it computes over `list_transactions_for_security` (all accounts for one security) in a single call, so the pen matches both legs.
- **Non-transfer securities are untouched** — the code path is identical when `is_share_transfer` is False, so every buy/sell/dividend security behaves exactly as before (verified: a whole-portfolio run changes only by the removed transfer distortions).
- **Still out of scope:** cross-currency in-kind transfers, and persisting the leg-to-leg link (round-4 transfer-linking). An unmatched `ShrsOut` (shares genuinely leaving the tracked world) books no gain/loss rather than a modelled disposal — honest given we have no proceeds, and surfaced via `basis_incomplete`.
- **Verified** headless on a WAL-consistent snapshot of the live DB: per-security TSBIX figures above; whole-portfolio total return shifts only by the removed phantom losses; EMNT/CRANX basis carries across after merging their pairs; offscreen the report window renders all three.
