# ADR-071 — Investment QIF `P` field is a description, not a payee

**Date:** 2026-06-15
**Status:** Accepted
**Amends:** ADR-043 (investment QIF import — round 1, where the `P` field was first copied into `payee_raw`).
**Related:** ADR-028/029 (payee model — canonical/alias; the junk inflated the payee list this arc cleans up). ADR-044 (holdings/securities — the `Y` reference is the real instrument identity). The 2026-06-14 live-DB payee cleanup (150 junk payees deleted), whose root cause this closes.

---

## Context

Investment QIF rows carry a `P` line. In Quicken's own files `P` is a payee, but in the **brokerage exports the owner actually imports** (Banktivity → QIF) it is a free-text **description** of the transaction — e.g.

> `PDIV - SABRA HEALTH CARE REIT INC REC 08/29/25 PAY 08/31/25`

ADR-043's round-1 parser copied `P` straight into `payee_raw`, and the import service mints a payee from `payee_raw` (`get_or_create_payee`). So **every investment import created one payee per distinct dividend/description string**, and **re-importing the same QIF regenerated them** — the payee list filled with hundreds of one-use "DIV - …" strings.

This was the root cause behind the 2026-06-14 cleanup: 150 junk payees were deleted from the live `.mfl` (3,245 → 3,095) using a usage-based predicate (zero non-investment txns). That cleanup was a one-off; the source kept producing more on the next import. This ADR removes the source.

The real "who/what" for an investment row is already captured elsewhere: the **security** comes from the `Y` line (`security_id`, ADR-044), the **action** from `N` (Buy/Sell/Div/…). The `P` description is redundant with those for identity, and useful only as human-readable memo text.

---

## Decision

**For every investment-action row, treat `P` as a description, not a payee.** Two owner forks (`AskUserQuestion`):

1. **Scope — all investment-action rows** (not just dividends). Any row routed through `_normalise_invst_record` (i.e. it has an `N` action) treats `P` as a description. Uniform and principled — brokerage `P` is a description field for *all* action types, so gating only the income/Div rows would leave the same trap on Buy/Sell/Cash.
2. **`P` text fate — fold into the memo** (not dropped). The description is appended to the row's memo so no information is lost; a user who wants a real payee can still add one by hand.

### Implementation (`import_engine/qif_parser.py`, `_normalise_invst_record`)

- The memo is now composed from `P` **and** `M` (de-duped so a `P == M` source doesn't double up), followed by the existing `Transfer to/from [Account]` note for `L`-linked rows. Order: description (`P`) first, then `M` memo, then the transfer note.
- `payee_raw` is set to `""`. The import service's `get_or_create_payee("")` returns `None` (existing behaviour), so **investment rows never mint a payee**.

That is the whole change — **parser-only**. No schema change, no migration, no Repository change, no UI change. The cash CSV/OFX/QIF-bank paths are untouched (they don't go through `_normalise_invst_record`), so ordinary payees still import normally.

### Why re-import dedup is unaffected

Investment rows are deduplicated on a composite hash of **account + date + action + security + quantity + amount** (`compute_investment_hash`) — it never included the payee. So dropping `P` from `payee_raw` does not change any row's `import_hash`: previously-imported rows still match and dedupe on re-import, and the junk is simply never created again.

---

## Consequences

- **The junk source is closed.** Future investment imports (and re-imports) create zero description-payees. The 2026-06-14 cleanup no longer needs repeating.
- **No data loss.** The `P` description survives in the memo, visible on the register row.
- **Existing rows are unaffected** by this change alone. Rows already imported keep whatever payee they currently have (most dividend rows already had their payee set to `NULL` by the cleanup); their memos are not retro-actively rewritten. Re-importing a file *will* refresh nothing (dedup skips them), so old rows keep their old memos — acceptable, since the cleanup already nulled their junk payees.
- **Minor cosmetic overlap** on `Cash`/transfer rows: the source's own `P` wording ("Transfer to checking") and the synthesized canonical note ("Transfer to Checking") can both appear in the memo. Left as-is — the `P` text is the source's wording and the synthesized note is the app's canonical form; de-duplicating across that semantic overlap isn't worth special-casing.

### Rejected alternatives

- **Drop `P` entirely** (owner fork B, not taken) — leanest, matches the post-cleanup state, but loses the description text some users may want.
- **Gate only Div/income actions** (owner fork A, not taken) — narrower, but leaves the same payee-from-description trap on Buy/Sell/Cash rows.
- **A payee-cleanup migration** — orthogonal; the one-off cleanup already ran. This ADR fixes the *source* so a migration is never needed again.
- **Map `P` to the security as payee** — conflates two concepts; the security is already a first-class column (`security_id`), and the payee list should stay reserved for genuine counterparties.

---

## Verification

Offscreen parse of a synthetic investment QIF (Div + Buy + Cash/transfer rows):

- **Div** (`P` only, no `M`): `payee_raw=''`, `memo='DIV - SABRA HEALTH CARE REIT INC REC 08/29/25'`.
- **Buy** (`P='Buy shares'`, `M='noteX'`): `payee_raw=''`, `memo='Buy shares | noteX'` (both folded, deduped).
- **Cash** (`P='Transfer to checking'`, `L[Checking]`): `payee_raw=''`, `memo='Transfer to checking | Transfer to Checking'`.

Confirmed `get_or_create_payee('')` returns `None`, and that `compute_investment_hash` excludes the payee so re-import dedup is byte-identical.
