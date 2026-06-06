# ADR-036 — Transfer matching: link an existing other-side transaction instead of always creating a partner

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-020 (transfers — two linked txns sharing one `transfer_id`); ADR-035 (multi-currency foundation — `transfer` parent row with rate + rate_source); ADR-037 (bulk reconcile — the many-pairs-at-once form of this same matcher); ADR-022 (register inline typeahead — same "1 strong candidate → silent commit, otherwise prompt" lineage)

---

## Context

ADR-020 settled the verb shape for transfers: pick a `kind='transfer'` category on any flow (New Transaction, inline edit, Bulk Edit) and a single destination-account prompt creates the partner row. That's correct for the case where the *only* record of the transfer is the user's intent in this moment.

In practice it's not the only case. The owner's setup workflow is:

1. Import the current account → has a -£500 row on 1 June labelled "TRANSFER TO SAVINGS"
2. Import the savings account → has a +£500 row on 2 June labelled "TRANSFER FROM CURRENT"
3. Categorise the current-account side as `Transfer: Between own accounts`
4. The current ADR-020 behaviour creates a *new* +£500 partner on savings, giving the savings account a duplicate row

The savings account now has both the imported +£500 *and* a fresh +£500 partner with the same date — the balance is wrong by £500 and the import-duplicate-detection won't catch it because the partner doesn't have an `import_hash`.

The fix is to look for an existing other-side transaction before creating a new one. If one already exists (within a small date window, with the matching opposite-sign amount, not already part of a transfer), link the two rows under one `transfer_id` instead of creating a third. Otherwise fall through to today's create-partner behaviour.

This is critical for the import-everything-then-categorise workflow that the owner uses and that most personal-finance users default to. The matcher also generalises:

- to **cross-currency** transfers, where "matching amount" means the source's amount converted at the FX rate for that day, within a small tolerance to allow for the exchange-rate fee
- to **bulk edits**, where the user selects 12 imported credit-card payments and wants the matcher to silently link each one to the right outgoing row on the current account
- to **a future import-time suggestion**, where right after the import preview surfaces "looks like a transfer to {Account X}" the user can accept the link without a second round-trip

This ADR is scoped to the *single-flow* matcher and its UI. The bulk matching screen — Manage → Reconcile Transfers… that lets a user reconcile many candidate pairs across two accounts in one go — is its own ADR ([ADR-037](ADR-037-bulk-transfer-reconcile.md)), but reuses the matcher core defined here.

---

## Options considered

### When does the matcher run?

- *Only on a Reconcile menu*: leaves the New / inline / Bulk Edit flows always-create. Rejected — the owner's setup workflow is the *create transfer* moment, not a separate reconcile step.
- **On every flow that today calls `create_transfer` / `convert_to_transfer`** (chosen): the matcher is part of the same destination-prompt step. If a candidate exists, it's offered; if not, today's create-partner behaviour fires.

### What defines a "match"?

| Dimension | Default | Why |
|---|---|---|
| **Other account** | The destination the user picked | Free — already known |
| **Direction** | Opposite-sign of the source row | A -£500 outflow matches a +£500 inflow, never another -£500 |
| **Amount equality** | Exact pence equality for same currency; same-day FX rate ± `transfer_fx_tolerance_pct` for cross-currency | Same-currency settles to the penny; cross-currency picks up bank conversion fees |
| **Date window** | source's posted_date ± `transfer_match_window_days` | Settle latency: ACH 1–3 business days, SWIFT up to 5, internal bank 0 |
| **State** | The candidate's `transfer_id` is NULL (not already part of a transfer) | A row already linked can't be re-linked |

Both window-days and FX tolerance are configurable in `setting` — defaults `3` and `1.0` (one percent) per the owner's decision and a sensible round-trip starting point.

### What does "candidates" look like?

- *Always one row only* — pick the best and silently link: hides the wrong choice when there are two same-amount candidates within the window. Rejected.
- **Zero / one / many** with explicit handling (chosen):
  - **Zero candidates** → fall through to today's `create_transfer` path. Silent commit + status-bar toast "Created transfer to {Account B}" (per [[feedback-no-dialog-for-known-imports]] applied to the no-friction case).
  - **One candidate** → confirm dialog: "Match to this existing transaction in {Account B}? [Match] [Create new] [Cancel]." The owner explicitly asked for confirmation — even one strong match shouldn't be silently linked because the cost of linking the wrong row is real (silently changes that row's category and direction).
  - **Many candidates** → picker dialog: list of rows (date / payee / amount), one selected by default (highest confidence), plus "+ Create new" at the bottom and a Cancel button.

### Confidence scoring (used in multi-candidate ordering and in bulk reconcile per ADR-037)

A simple weighted score, kept pure-Python and easily inspectable:

```
score(candidate) = 100
                 - 5  * abs(days_apart)        # 0 days apart → no penalty
                 - 50 * (amount_mismatch_pct)  # 1% off → -0.5; 5% off → -2.5; ≥10% → discarded upstream
                 + 20 * (currencies_match ? 1 : 0)
                 + 10 * (payee_token_overlap)  # +10 if any non-stopword payee token in common (case-insensitive)
```

The score is *informational ordering* — the matcher never auto-decides; the user picks. Used as the sort key when more than one candidate exists, and surfaced as a "Strong / Good / Possible" chip in the picker.

### What about the source row's *current* fields when matching?

When the matcher links, the existing other-side row keeps its own:

- date (might be ±N days off the source's; we accept that — it's reality)
- payee (often imported with a meaningful "TRANSFER FROM ACME" label)
- memo
- status (Cleared / Uncleared as imported)

…and gains:

- the shared `transfer_id`
- its `category_id` is **rewritten** to the same transfer-kind category the source row was assigned, so reports see a coherent pair. (The user picked the transfer category on the source; honoring it on both halves is what they intended.)

This is a deliberate rewrite. The owner's mental model: "this savings inflow *is* the other half of that current-account outflow." The matcher's job is to express that intent in the data. Renaming the category is the *whole point*.

### What about edge cases?

- **Self-account match.** The matcher discards any candidate where the candidate's account_id equals the source's account_id (defensive — shouldn't happen because the user picked a different destination, but enforce it).
- **Same-amount, same-day, multiple candidates with already-set `transfer_id`.** Filtered out at the query level by `WHERE transfer_id IS NULL`. The picker only ever sees unmatched rows.
- **Cross-currency with no FX rate for that day.** The matcher falls back to nearest-prior rate (per ADR-035) when computing the expected receiving amount; if no rate at all, only same-currency candidates are considered (and a small "no FX rate available for {date}; only same-currency candidates shown" note appears below the picker).
- **A candidate whose category is already a transfer-kind but with `transfer_id` IS NULL** — that's an orphaned transfer half (ADR-020 notes this can happen if the user cancelled the destination prompt). The matcher treats it as a normal candidate; the rewrite of `category_id` is a no-op if it's already the same category.
- **The source itself is already a transfer half.** The matcher is never invoked in that case — the dispatcher in `register_window.py` checks `transfer_id` first and the inline-edit path is a no-op on already-transferred rows. Same as today.

### Bulk Edit interaction

The bulk-edit dispatcher today calls `bulk_set_category_and_convert` which creates one partner per source row. The new behaviour:

1. For each source row, run the matcher against the chosen destination account.
2. Collect the results into three lists: `to_link` (one strong / chosen candidate per source), `to_create` (zero candidates), `ambiguous` (multiple candidates needing a per-row pick).
3. If any rows are in `ambiguous`, open a small "Resolve transfer matches" dialog showing one tab/section per ambiguous row with the standard picker. The user works through them or hits "Create new for all remaining." (Owner asked for confirmation but in bulk — this dialog is the bulk form.)
4. After the user resolves ambiguity, commit everything in one SQL transaction via `bulk_match_or_create_transfers`.

For the all-strong-1-candidate case (typical Banktivity→other-bank reconcile), the user gets one summary dialog: "12 rows: 11 will be matched to existing transactions, 1 will create a new partner. [Confirm] [Review individually]." That keeps the bulk feel without losing visibility.

### Why store the matcher's settings in `setting` rather than per-call kwargs?

Because the same matcher runs in the single-row flow, the bulk-edit flow, and ADR-037's reconcile dialog. One source of truth (`setting.transfer_match_window_days`, `setting.transfer_fx_tolerance_pct`) keeps them coherent. The Currencies dialog (or a future Preferences pane) exposes both. Defaults are sane out of the box per the owner's decision (3 days, 1%).

---

## Decision

### Repository

New methods (all on the existing `Repository` class):

- `find_transfer_candidates(*, source_txn_id, other_account_id, window_days=None, fx_tolerance_pct=None) -> list[TransferCandidate]`
  - Reads the matcher settings from `setting` when kwargs are None.
  - Returns scored, sorted candidates (best first).
  - Filters to `transfer_id IS NULL`, opposite-sign amount, within window, and (for cross-currency) within FX tolerance.
- `link_transfer(*, source_txn_id, candidate_txn_id, category_id, rate=None, rate_source='derived') -> str`
  - Generates a fresh `transfer_id` IRI.
  - Writes it to both rows.
  - Rewrites the candidate's `category_id` to the supplied category.
  - Inserts a `transfer` parent row (per ADR-035) with the determined rate and source.
  - Returns the new transfer IRI. Atomic.
- `bulk_match_or_create_transfers(plan: list[BulkTransferDecision]) -> BulkTransferResult`
  - Single SQL transaction.
  - Each `BulkTransferDecision` is either `LinkExisting(source_id, candidate_id, category_id)` or `CreateNew(source_id, other_account_id, category_id)`.
  - All-or-nothing.

New frozen dataclasses:

```python
@dataclass(frozen=True)
class TransferCandidate:
    txn_id: int
    posted_date: str
    amount: Decimal        # in candidate's account's currency, signed
    payee_name: str
    days_apart: int        # signed: positive = later than source
    amount_mismatch_pct: float
    currencies_match: bool
    score: int             # higher = better; from the scorer in §confidence

@dataclass(frozen=True)
class LinkExisting:
    source_txn_id: int
    candidate_txn_id: int
    category_id: int

@dataclass(frozen=True)
class CreateNew:
    source_txn_id: int
    other_account_id: int
    category_id: int

BulkTransferDecision = LinkExisting | CreateNew

@dataclass(frozen=True)
class BulkTransferResult:
    linked: int
    created: int
    transfer_iris: list[str]
```

Amend existing methods:

- `create_transfer` and `convert_to_transfer` keep their existing signatures and become *the create-new branch*. They don't run the matcher themselves — that's the dispatcher's job. They do, however, write the `transfer` parent row (per ADR-035).
- `bulk_set_category_and_convert` is split into two phases. Phase 1 (category + payee/status/memo updates) is unchanged. Phase 2 used to call `_convert_to_transfer_unbatched` directly; it now defers to the dispatcher which decides per-row via the matcher. This keeps the atomicity guarantee — the new helper `bulk_match_or_create_transfers` runs both branches in one SQL transaction.

### UI

**Single-flow path (`mfl_desktop/ui/register_window.py`)** — the existing `_prompt_destination_account` helper extends:

1. After the destination is chosen, call `repo.find_transfer_candidates(source_txn_id=row.id, other_account_id=dest_id)`.
2. Switch on len(candidates):
   - `0` → today's `convert_to_transfer` path. Status-bar toast "Transferred to {Account B}."
   - `1` → open `TransferMatchConfirmDialog` (new) — one row showing the candidate's date / payee / amount with [Match] / [Create new] / [Cancel] buttons.
   - `≥ 2` → open `TransferMatchPickerDialog` (new) — list of candidates sorted by score, best pre-selected, "+ Create new" at the bottom, [OK] / [Cancel] buttons. Each row shows the strength chip ("Strong" / "Good" / "Possible" derived from score thresholds 80/60).

In the cross-currency case, both dialogs add a one-line "Implied rate: 1 GBP = 1.2734 USD (openexchangerates 2026-06-05)" line under the candidate row(s). Linking writes that rate into the `transfer` parent (per ADR-035).

**Bulk Edit path (`mfl_desktop/ui/bulk_edit_dialog.py`)** — when the user has picked a transfer-kind category and the destination account:

1. Run the matcher for each source row, in-memory.
2. Bucket results into `to_link` / `to_create` / `ambiguous`.
3. Open `BulkTransferReviewDialog` (new) — a single-page summary:
   - "Of N transactions: K will match existing, M will create new, P need a choice."
   - Buttons: [Resolve choices] (only enabled if P > 0), [Confirm], [Cancel].
   - If [Resolve choices] is clicked, open `BulkTransferAmbiguousDialog` (new) — paginated picker, one row at a time, "Apply to all remaining like this" affordance for repetitive cases.

After the user confirms, the dispatcher constructs the `BulkTransferDecision` list and calls `bulk_match_or_create_transfers`. All in one SQL transaction.

**Settings surface** — small "Transfer matching" section in the Manage → Currencies dialog (since both new tunables share that screen's context):

- "Match window: ±[ 3 ] days"
- "Cross-currency tolerance: ±[ 1.0 ] %"

Persisted into `setting`. Defaults seeded in migration 0009 (per ADR-035).

### Files touched

| File | Change |
|---|---|
| `mfl_desktop/db/repository.py` | `find_transfer_candidates`, `link_transfer`, `bulk_match_or_create_transfers`; new dataclasses; amend `create_transfer` / `convert_to_transfer` / `bulk_set_category_and_convert` |
| `mfl_desktop/ui/transfer_match_dialogs.py` | New — `TransferMatchConfirmDialog`, `TransferMatchPickerDialog`, `BulkTransferReviewDialog`, `BulkTransferAmbiguousDialog` |
| `mfl_desktop/ui/register_window.py` | `_prompt_destination_account` and bulk-edit dispatcher route through the matcher |
| `mfl_desktop/ui/bulk_edit_dialog.py` | No interface change; dispatcher consumes new dataclasses |
| `mfl_desktop/ui/currencies_dialog.py` | Add the two "Transfer matching" tunables (small section at the bottom of the Currencies dialog from ADR-035) |

---

## Consequences

### Positive

- **The import-everything-then-categorise workflow stops creating duplicates.** Importing both sides and marking one as transfer now *links* — the user sees a single logical operation instead of three rows.
- **Bulk reconciliation is one action.** Select 12 imported credit-card payments → Bulk Edit → transfer category → destination → "11 match, 1 create new" confirmation → done. The owner's quoted pain point ("It's so annoying doing them one at a time") goes away.
- **Cross-currency just works.** The matcher uses the FX rate from ADR-035's table to decide whether a candidate is in range; the link writes the rate into the `transfer` parent so the historical exchange rate is preserved.
- **The two-row + shared `transfer_id` invariant is unchanged.** ADR-020's data model still holds; `transfer_id` is still the source of truth; partner-aware delete still works.
- **The matcher's defaults are right out of the box.** ±3 days, ±1% — owner-set. Tunable for the edge cases.

### Negative / trade-offs

- **The candidate's category is rewritten.** A user who had categorised the imported inflow as "Salary" before marking the outflow as transfer will see the imported row's category change. That's the matcher's job — but it does mean the user loses a small categorisation if they did it in the wrong order. Mitigation: the confirm/picker dialog shows the candidate's current category so the user sees what's about to be rewritten.
- **No undo for the link in v1.** Unlinking is "delete one half, which deletes both via partner-aware delete." A future Edit Transfer dialog (already on the ADR-020 follow-up list) will gain an Unlink verb that keeps both rows as standalone transactions. Tracked as backlog.
- **Settings live in `setting` — no per-account override.** A bank that genuinely settles in 7 business days would need to bump the window globally. Acceptable v1; per-account override is a small additive change later.
- **Confidence score is heuristic.** It's not a model — it's a sum of weighted differences. Good enough to order candidates; not good enough to auto-decide. That's by design — the user always confirms.

### Ongoing responsibilities

- **The matcher is the single dispatcher.** Any new code path that creates transfers — a future "Match this row to..." right-click verb on the register, an import-time auto-suggestion (ADR-035 backlog), the bulk reconcile dialog (ADR-037) — must go through `find_transfer_candidates` and `link_transfer`, never re-implement matching.
- **Category-rewrite invariant.** Linking always rewrites the candidate's category to the source's. Any future Edit Transfer dialog must preserve that or document why it's deviating.
- **Cross-currency rate provenance.** The `transfer.rate_source` field must reflect *how* the rate was obtained at link time. `fx_rate` for an FX-table lookup; `manual` for a user-typed rate in the dialog; `derived` only when amounts back-derive (same-currency or matcher inferred from candidate's amount).
- **Bulk-edit atomicity.** `bulk_match_or_create_transfers` is the single SQL transaction. Adding new bulk-edit fields means threading them through this method, not adding sequential calls.

### Out of scope here (covered separately)

- Bulk reconcile dialog (Manage → Reconcile Transfers…) for arbitrary candidate pairs across two accounts — **[ADR-037](ADR-037-bulk-transfer-reconcile.md)**.
- Import-time transfer suggestion (post-import preview) — ADR-035 backlog; will land later in its own ADR if it grows beyond a simple "review these candidates" affordance.
- Edit Transfer dialog with Unlink verb — ADR-020 backlog.
