# ADR-037 — Bulk transfer reconcile: pair candidates across two accounts in one screen

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-020 (transfers — two linked txns sharing one `transfer_id`); ADR-035 (multi-currency foundation — FX rate lookup + `transfer` parent row); ADR-036 (transfer matching — the single-flow matcher whose scoring + linking primitives this screen reuses)

---

## Context

ADR-036 handles the *single-flow* case: the user marks one transaction as a transfer, the matcher offers existing-other-side candidates, and a single dialog confirms. That covers the moment when the user is already looking at one row.

It doesn't cover the *housekeeping* case: the user has just imported two accounts that have been transferring money back and forth for two years, and wants to reconcile all the un-linked pairs in one pass without scrolling through both registers.

Concretely, the owner's setup workflow is going to involve:

1. Import the UK current account (24 months of data, 18 transfers to/from savings)
2. Import the UK savings account (same 18 transfers, mirror direction)
3. Import the US current account (12 months, 5 transfers to/from US savings)
4. Import the US savings account (same 5 transfers)
5. Import the UK→US international wires (3 of them) on both sides

That's potentially 26 transfer pairs scattered across four accounts. Doing them via the single-flow matcher means opening each pair, picking the destination, clicking through the confirm dialog. Doable, but at scale it's painful and easy to miss a pair.

The reconcile dialog solves this:

- Pick two accounts (or "All accounts" against one)
- See every unmatched candidate pair, side-by-side, sorted by confidence
- Bulk-accept the high-confidence pairs in one click
- Per-pair verbs for the lower-confidence ones
- Cross-currency rates surfaced inline so the user can sanity-check

This *is* the multi-currency power feature. The owner's framing — "very few finance apps offer true multicurrency support" — is most visible here: a single screen that pairs $1,000 USD on 12 March with £785 GBP on 13 March via the spot rate on 12 March is the thing a Banktivity or YNAB user has to do by hand, one row at a time.

The dialog reuses the matcher primitives from ADR-036 wholesale. It is, in essence, a screen-shaped frontend to `find_transfer_candidates` running over every unmatched row on each account.

---

## Options considered

### Scope — two accounts vs N accounts

- *N accounts, one big screen*: a giant table of every unmatched candidate across every account. Powerful, unreadable. Rejected.
- **Two accounts at a time** (chosen): the natural pairing unit. The user picks "Current ↔ Savings" or "USD Checking ↔ UK Current" and works through it. For more pairs the user runs the dialog repeatedly. This matches how the user thinks about transfers — *between* these two specific accounts.
- *Two accounts, with an "Any other account" wildcard on the right*: makes the discovery case easier ("show me every unmatched outflow on Current and any inflow on any other account that matches"). Powerful but a screen-design rabbit hole; deferred.

### Pairing strategy

A unmatched-row on account A could plausibly pair with multiple unmatched-rows on account B (same amount, within window). The dialog needs a *paring* — one A-row to one B-row — not just a list of candidates.

- *Show every candidate edge*: 5 A-rows × 5 same-amount B-rows = 25 rows in the list. Confusing.
- **Greedy pairing by confidence** (chosen): order all A-rows × B-rows pairs by score (using ADR-036's scorer). Walk the list highest-score-first, claiming both sides of each accepted pair. Once a row is in a claimed pair, it's removed from candidacy for any other pair. Result: at most `min(|A_unmatched|, |B_unmatched|)` pairs, each with a unique source/target.
- *Hungarian algorithm / optimal bipartite matching*: optimal but more code than this screen needs and the user-visible result is rarely different from greedy on this signal shape. Skipped.

### Confidence binning

Reuse the score from ADR-036 with the same thresholds — Strong (≥80), Good (60–79), Possible (<60). Each pair gets a coloured chip + a strength label. The "Match all confident" button only matches pairs in the Strong bin by default; a small "Match all Good" link does the wider sweep if the user trusts the data.

### What happens to unpaired rows?

- Rows that have no plausible partner remain in the lists below the paired section in a "Without a match on the other side" panel. The user can:
  - leave them be (they aren't transfers)
  - click "Create new partner" per row, which falls through to the single-flow create-partner path

The dialog never silently creates a partner for the user; the only writes are link-existing or explicit per-row create-new.

### Cross-currency presentation

For a cross-currency pair, the row shows:

- A column: `$1,000.00 USD` on 12 Mar 2026
- B column: `£785.30 GBP` on 13 Mar 2026
- Below: `Implied 1 USD = 0.7853 GBP · spot was 0.7841 (+0.15%) · openexchangerates 2026-03-12`

When the implied rate sits within the FX tolerance (default 1% per ADR-036), the chip is Strong. When it's outside, the pair drops to Possible (so the user gives it a second look) or doesn't pair at all if it's way off.

The rate used at link time is the *implied* rate (back-derived from amounts), with `rate_source='derived'`. The spot rate from the FX table is informational only — it surfaces the deviation but doesn't overwrite the user's true cash movement.

### Bulk write atomicity

All decisions made in the dialog are committed in one SQL transaction via a new `bulk_reconcile_transfers(plan: list[BulkTransferDecision]) -> BulkTransferResult` (the same plan-and-decision dataclasses introduced in ADR-036). If anything fails, nothing is linked. The user can always re-open the dialog if they want to retry.

### Edit / undo

- No undo for the link in v1, same as ADR-036.
- The dialog never deletes; failure mode is "user re-opens, fixes, retries."
- A future Edit Transfer dialog (ADR-020 / ADR-036 backlog) will gain Unlink.

### Performance

For the owner's foreseeable scale (low-thousands of txns, low-dozens of unmatched candidates per account), the dialog can do the matching in Python from a single query that pulls all unmatched rows. SQL indexes already exist for the `WHERE transfer_id IS NULL AND account_id = ?` shape. No new indexes needed.

---

## Decision

### Repository

New method, layered on ADR-036's primitives:

- `find_transfer_pairs(*, account_a_id, account_b_id, window_days=None, fx_tolerance_pct=None) -> list[TransferPair]`
  - Pulls every txn on each account where `transfer_id IS NULL`.
  - Builds the cross-product, scores each potential pair using ADR-036's `score_candidate` helper (extracted from `find_transfer_candidates` so both methods share it).
  - Walks scored list highest-first, claims pairs greedily.
  - Returns ordered pairs (Strong → Good → Possible) plus the unpaired remainders.

```python
@dataclass(frozen=True)
class TransferPair:
    source_txn_id: int
    source_amount: Decimal
    source_currency: str
    source_posted_date: str
    source_payee: str
    target_txn_id: int
    target_amount: Decimal
    target_currency: str
    target_posted_date: str
    target_payee: str
    days_apart: int
    implied_rate: Optional[Decimal]
    spot_rate: Optional[Decimal]
    rate_deviation_pct: Optional[float]
    score: int
    strength: str   # 'Strong' / 'Good' / 'Possible'
```

- `bulk_reconcile_transfers(plan: list[BulkTransferDecision]) -> BulkTransferResult` — already specified in ADR-036; this ADR reuses it unchanged. The reconcile dialog produces the plan from the user's confirmed pair selections.

The greedy pairing logic lives in pure Python in a new helper `mfl_desktop/transfer_reconcile.py` (not on the Repository) so it stays unit-testable without a database. The Repository method calls it with the rows it fetched.

### UI

**Manage → Reconcile Transfers…** (new dialog `mfl_desktop/ui/transfer_reconcile_dialog.py`)

Layout:

```
┌──────────────────────────────────────────────────────────────┐
│ Account A: [Current ▾]     Account B: [Savings ▾]            │
│ Window: ±[3] days   FX tolerance: ±[1.0]% (from Currencies)  │
├──────────────────────────────────────────────────────────────┤
│ PROPOSED PAIRS (12)                                          │
│ ─────────────────────────────────────────────────────────── │
│ ● Strong  -£500.00 1 Jun  ↔  +£500.00 1 Jun        [Match]  │
│ ● Strong  -$1,000  12 Mar ↔  +£785.30 13 Mar       [Match]  │
│           Implied 0.7853 · spot 0.7841 (+0.15%) OXR 12 Mar   │
│ ◐ Good    -£42.50  3 Apr  ↔  +£42.50  6 Apr        [Match]  │
│ ○ Possible -£100   1 May  ↔  +£100    5 May        [Match]  │
│ ...                                                          │
├──────────────────────────────────────────────────────────────┤
│ WITHOUT A MATCH (Current — 3 rows)                           │
│   -£17.99 5 Apr  AMAZON                            [Create new] │
│   ...                                                        │
│ WITHOUT A MATCH (Savings — 1 row)                            │
│   +£2.45 1 Jan   INTEREST                          [Create new] │
├──────────────────────────────────────────────────────────────┤
│ Category for matched: [Transfer: Between own accounts ▾]     │
│ [ Match all Strong ]  [ Match all Good ]                     │
│                                       [ Cancel ]  [ Apply ]  │
└──────────────────────────────────────────────────────────────┘
```

Behaviour:

- **Account combos** populate from `list_accounts()`; selecting the same account on both sides disables Apply.
- **Window / tolerance** default from `setting` (the same tunables ADR-036 introduced) and are editable here too; changes persist back to `setting` on Apply.
- **Per-row Match** toggles a checkbox in front of the row.
- **Match all Strong / Good** select every row in that bin.
- **Category combo** is the seeded Transfer category by default. Changing it changes the category written to *all* matched rows on Apply.
- **Create new** per unpaired-row goes through the standard create-partner path on Apply (the user has to confirm the destination once; the dialog defaults to the *other* account).
- **Apply** builds the `BulkTransferDecision` plan and calls `bulk_reconcile_transfers`. Status-bar toast on success: "Linked 11 transfers, created 1 new partner."

The dialog refreshes its candidate list after Apply — un-matched rows that the user didn't action remain visible so the user can run a second pass with looser thresholds without re-opening.

### Discovery

- **Menu entry**: Manage → Reconcile Transfers… (Ctrl+Shift+R). Placed next to Manage → Schedules / Categories / Payees / Currencies.
- **Suggestion** from the Currencies dialog when a new FX pair has just been refreshed: "You have N unmatched cross-currency rows that might pair now. [Open Reconcile dialog]." (Helpful right after the user backfills historical rates.)

### Files touched

| File | Change |
|---|---|
| `mfl_desktop/transfer_reconcile.py` | New — pure-Python pairing helper |
| `mfl_desktop/db/repository.py` | `find_transfer_pairs`; extract `_score_candidate` from `find_transfer_candidates` so both share it |
| `mfl_desktop/ui/transfer_reconcile_dialog.py` | New — Manage → Reconcile Transfers… |
| `mfl_desktop/ui/register_window.py` | Menu wiring under Manage |
| `mfl_desktop/ui/currencies_dialog.py` | Optional "open Reconcile dialog now" link after a backfill |

---

## Consequences

### Positive

- **The setup workflow has a closing screen.** Import all accounts → run Reconcile Transfers once per pair → done. No row-by-row spelunking.
- **Cross-currency rates are visible and sanity-checkable.** The implied-vs-spot deviation is right there on each row; a user can spot a mis-paired wire instantly because the deviation will be 30%, not 0.3%.
- **Reuses ADR-036 primitives wholesale.** No new scoring logic, no new linking logic, no new write-time invariants — this dialog is a screen-shaped frontend to the matcher we already designed.
- **Greedy pairing is good-enough.** For the data shapes personal finance produces, the result is indistinguishable from optimal bipartite matching; the code is ~30 lines of pure Python.
- **Works for the single-currency case too.** A user with no foreign accounts still benefits — the dialog is the best way to reconcile a year of historical transfers between current and savings imported at the same time.

### Negative / trade-offs

- **Two accounts at a time.** A user with 8 accounts that all interact has to run the dialog several times. Acceptable for v1 — see "Wildcard" item in Out of Scope.
- **No automatic re-pairing after Apply.** If the user matches some pairs and changes a tolerance, the dialog re-computes only on re-open (or after explicit Apply). Less smooth than a live re-pair on tolerance change; deferred.
- **Per-pair category override not in v1.** The single category combo applies to every matched pair in the batch. A user who wants "Mortgage" for the principal transfers and "Between own accounts" for the routine ones would have to run the dialog twice. Tracked as backlog if real use surfaces.
- **Apply is all-or-nothing.** Failure halfway through rolls back everything. Acceptable — the user re-opens, fixes the offending choice, retries.

### Ongoing responsibilities

- **Score thresholds (80 / 60) are shared across ADR-036 and ADR-037.** Any future tuning lives in one constant block in `mfl_desktop/transfer_reconcile.py`.
- **Greedy pairing must remain order-stable.** When two pairs tie on score, ties break by source-txn-id ascending — that keeps the displayed list stable across re-opens.
- **Cross-currency deviation indicator is informational only.** Don't add code that auto-rejects pairs over X% deviation — the user might legitimately have taken a bad rate at a bureau de change. Confidence binning is the right surface; hard rejection is the wrong one.
- **`setting` persistence on Apply.** The window/tolerance fields are persisted via `set_setting` when the user clicks Apply, even if the matching plan is empty. That way the user's tuning sticks for the next pass.

### Out of scope here

- **N-account / wildcard reconcile.** A future "Reconcile against any other account" mode would extend the same dialog with a "(Any other account)" entry in the B combo. Additive — deferred.
- **Auto-categorisation per pair.** A future rules engine (ADR-028 round 3) could pre-select the right transfer category based on payee text on either side. Out of scope here.
- **Per-pair category override.** A small "Per-pair category" column in the table is a one-evening addition if real use surfaces the need.
- **Live re-pair on tolerance change.** Tracked as polish.
