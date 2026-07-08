# ADR-145 — Bare share-transfers don't distort the money-weighted return

**Date:** 2026-07-08
**Status:** Implemented
**Related:** ADR-046 (Investment Returns report — total return + money-weighted IRR; this is amendment 3). ADR-053 (in-kind share transfers ShrsIn/ShrsOut are not sales — basis carry). ADR-054 (stock splits adjust the FIFO lots). Transfer-taxonomy cleanup (the ~2,000 Banktivity bare pseudo-transfers).

## Context

Owner report: in Investment Returns, **SCHD showed a −2.2% IRR while its total return was +41% (+$13,528)** — a stark contradiction against DIVO's sane +15% IRR over the same period.

Root cause is a data artifact meeting an over-eager engine rule:

- SCHD did a **3-for-1 split on 2024-10-11** (price history drops $84.62 → $28.54). The split was never recorded as a `StkSplit`; instead the extra shares were trued-up much later as a **bare share deposit** — `ShrsIn 594.825` on 2025-10-31, amount 0, no `transfer_id`, no matching `ShrsOut` anywhere. (594.825 ≈ 2× the ~291 pre-split shares — one of the bare Banktivity pseudo-transfers.)
- The returns engine (`compute_returns`) booked **every** in-window ShrsIn/ShrsOut as a market-value IRR cash flow (ADR-053's transfer legs count as value entering/leaving the measured accounts). So the split-as-deposit injected a phantom **≈ 594.825 × ~$27 ≈ $16,000 "contribution"** on 2025-10-31 — roughly half the cost basis, dropped in ~8 months before the window end, earning nothing after. The money-weighted return went negative. Total return (FIFO basis; the ShrsIn adds $0 cost) didn't use that figure, which is why the two disagreed so wildly.

Inspection showed **all 78 share-transfers in the data are bare** (no `transfer_id`, all amount 0); there are zero linked ones. So bare transfers are the norm here, and they are exactly the rows that shouldn't be read as real capital flows.

## Decision

A share-transfer leg books a market-value IRR cash flow **only when it is linked** — shares a `transfer_id` with its counterpart leg (`holdings._transfer_books_irr_flow`). A **bare/unlinked** ShrsIn/ShrsOut moves shares (basis carry / unknown-basis lot, unchanged) but contributes **no** IRR flow.

Rationale: a linked transfer is a genuine custodian move whose value entered or left the measured accounts — a real flow for a per-account or subset IRR. A bare ShrsIn/ShrsOut has no counterpart; it's an opening-balance seed, a correction, or a corporate action recorded as a deposit. Counting it at market injects a phantom contribution/withdrawal.

The change is **net-safe** for existing behaviour:

- **Matched bare pairs** (both legs in the computed set) already netted to zero — equal +mv / −mv on the same date — so suppressing both legs leaves that net unchanged.
- **True artifacts** (no counterpart, e.g. the split-as-ShrsIn) were the distortion → now correct.
- The only behaviour change is a **single-sided bare transfer** (one leg in the set), which is far likelier an import artifact than a real external flow.

**Companion data fix (SCHD).** The engine guard corrects the *number*, but the split still belongs in the record. So SCHD is also cleaned up at the data layer: insert a `StkSplit` (ratio 3) on 2024-10-11 (triples the 291.276 pre-split shares in place, basis unchanged, no flow) and shrink the bare `ShrsIn` from 594.825 to the **12.273 residual** (594.825 − 2×291.276) so the current share balance (1296.597) is preserved exactly. The 12.273 residual is of unknown origin and left as a small bare ShrsIn rather than inventing a source.

Rejected: dropping the SCHD ShrsIn entirely (would change the current balance by 12.273 shares and risk disagreeing with the broker statement); modelling the split via `price_multiplier` (ADR-093) — the stored prices are already split-adjusted, so a `StkSplit` on the lots is the right model; keying the guard on "amount == 0" instead of linkage (a genuine cashless transfer also has amount 0 — linkage is the real signal).

## Consequences

- SCHD's IRR returns to a sane, positive value consistent with its +41% total return (verified on the real data through `compute_returns`: **−3.4% → +10.3%/yr**; the bare ShrsIn drops out of the flow list). Every holding and the portfolio total are hardened against the same artifact — the fix is dataset-wide, not SCHD-specific.
- Total return, cost basis, market value, unrealized, and realized are all **unchanged** — the guard touches only IRR cash flows.
- Once genuine cross-account transfers get linked (`transfer_id`) as part of the transfer-taxonomy cleanup, their IRR flows come back automatically; until then, bare transfers simply don't distort the money-weighted return.
- `tests/test_returns_transfer_irr_guard.py` 3/3 (bare ShrsIn books no flow → clean ~44%/yr; a linked ShrsIn still books its market-value contribution; a bare ShrsOut books no flow and no realized gain). No schema change (the guard reads the existing `transfer_id`).
