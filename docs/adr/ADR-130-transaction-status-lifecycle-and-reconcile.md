# ADR-130 — Transaction status lifecycle (confidence ladder), status-driven reconciliation, and precise OFX matching

**Date:** 2026-07-02
**Status:** Proposed (design + phased plan; implementation to follow in increments)
**Related:** ADR-037 (transfer/reconcile matching). ADR-077 (OFX Direct Connect / import). ADR-036 (inline transfer matching — the ±2-day heuristic reused here). ADR-032 (the SQLite `CHECK`-constraint table-rebuild recipe used by the migration). ADR-051 (`txn_category_line`). ADR-092/109 (file/session). ADR-050 (cross-platform-first — the "some banks offer no download" case this must keep first-class).

## Context

Reconciling the owner's June HSBC statement surfaced a £16.17 (later £13.39) variance that took a line-by-line audit to resolve. The root causes were all **status/selection** problems, not arithmetic:

- Transactions the owner had **not yet seen on the bank** (30-June card purchases, pending) were sitting in a reconcilable state and got ticked into the statement.
- **Duplicate** entries (a day-trip entered on both the 19th and 22nd; a TV-licence and a Pret entered twice) were reconcilable and got ticked.
- The reconcile screen trusts a status + manual ticks, so nothing flagged "this isn't on the bank."

Today's model (audited): statuses are **`Pending / Uncleared / Cleared / Reconciled`** (Title-case), defined as a **copy-pasted tuple in ~8 UI files** (`register_window`, `transaction_dialog`, `split_transaction_dialog`, `investment_transaction_dialog`, `bulk_edit_dialog`, `transactions_list_window`, `delegates`, plus `filter_proxy`/`cli`) and a DB `CHECK(status IN ('Pending','Uncleared','Cleared','Reconciled'))` (migration 0001). Reconciliation **pre-ticks everything in `Cleared`** (`list_cleared_unreconciled_txns` → "Automatically select Cleared Transactions"). OFX import already captures a **FITID** (deduped via the `UNIQUE(account_id, import_hash)` index) and does a **±2-day manual-match** in `import_service`, but the match is "acceptable but not precise" and doesn't drive status.

The owner's real-world workflow:
1. Enter a transaction **when the money is spent** → today `Pending`.
2. When the bank app alerts that it **posted**, mark it → today `Uncleared` (counter-intuitive naming).
3. Periodically **download OFX and import**; entered transactions should **match** and become confirmed, and anything missed should be added — ideally shown explicitly on the import screen.
4. **Reconcile** the statement; everything confirmed within the dates should be on it, then lock.

The current names invert the intuition (`Uncleared` = "I saw it clear") and nothing distinguishes *"I eyeballed it"* from *"the bank's own download confirms it"* — which is exactly the confidence the reconcile step needs.

## Decision

Adopt a **four-state confidence ladder** for `txn.status`, make reconciliation **status-driven** off the bank-confirmed state, and upgrade OFX import into a **precise, reviewable matcher** that advances the status. Statuses become lowercase (matching `statement.status`, already `open`/`reconciled`):

| status | meaning | source of truth |
|---|---|---|
| **pending** | spent, not yet posted at the bank | your entry |
| **cleared** | you saw it post (bank-app alert) | your eyeball |
| **matched** | an OFX download matched it (or added it) | the bank's data |
| **reconciled** | tied to a statement and locked | the statement |

Each step adds corroboration. The rename is mostly mechanical: **`Uncleared → cleared`**, **`Cleared → matched`**, `Pending → pending`, `Reconciled → reconciled`.

### State machine

```
        (spend)             (see it post)          (OFX match)          (close stmt)
  ────► pending ──────────► cleared ─────────────► matched ───────────► reconciled
           │                                    ▲     ▲                     │
           └──────── OFX match a never-eyeballed ┘     │                     │ (reopen)
                                                        │                     ▼
                              OFX adds a missed txn ────┘                 matched
```

- `pending → cleared`: manual ("I saw it post").
- `pending → matched` and `cleared → matched`: an OFX line matches the entry on import.
- *new from bank* (a posted line that matched nothing you entered): inserted directly as **matched**.
- `matched → reconciled`: statement close; **locked** (edits require reopening the statement, ADR existing behaviour).
- A **`cleared` item the next download never confirms stays `cleared`** and is **flagged**, never silently reverted — it's the anomaly signal that catches duplicates/phantom entries (both bugs we hit).

### Sub-decisions

1. **Reconcile eligibility = `matched`, with `cleared` optional (owner's call).** The reconcile candidate set defaults to **`matched`** transactions in range. A per-reconcile **"include cleared (bank-confirmed by eye)"** toggle adds `cleared` items — because **some institutions offer no OFX/download**, so a user may legitimately never reach `matched` and must reconcile off eyeballed `cleared` (ADR-050 first-class-everywhere). When cleared are *excluded*, any `cleared` item inside the statement dates is shown as a **warning list** ("you saw these post but no download confirmed them"). `pending` is never eligible. This alone makes both June bugs impossible.
2. **Keep the spend date; add a `bank_posted_date`.** On match, record the OFX posting date in a new column and use it for reconcile date-ranging (falling back to `posted_date` when absent). The user's spend date stays for analytics; the bank date fixes the boundary/scramble we saw (entries dated 1–3 days before the bank). Rejected: overwriting `posted_date` (destroys the spend-time signal the owner enters deliberately).
3. **Auto-match threshold:** exact **amount** + same **payee** (canonical) + within **≤2 days** → auto-match; anything looser (amount differs, payee differs, or 3–5 days apart) → **needs-review**. The amount-differs case (e.g. entry £8.99 vs bank £8.25) offers **"adopt bank amount"** — precisely the check that would have caught the £13.39.
4. **Unconfirmed `cleared` items are surfaced, never auto-changed** (see state machine).

### Centralisation

Introduce `mfl_desktop/txn_status.py` as the single source: the ordered enum + per-status display metadata (label, register colour/icon, ladder order, `is_locked`). All ~8 duplicated tuples and the `filter_proxy`/`cli` references import from it. Register rendering: **pending = hollow/grey, cleared = amber, matched = blue ✓, reconciled = green 🔒** with a legend + tooltips (the ladder isn't standard-accounting vocabulary, so it must be self-explaining).

## Consequences

- The class of bug we just debugged becomes structurally impossible: `pending` and duplicate/unconfirmed items can't be reconciled, and unconfirmed `cleared` items are flagged rather than silently trusted.
- OFX import becomes the precise, reviewable step the owner wants ("see the matches on the import screen"), with idempotent re-import (existing FITID/`import_hash` unique index) and bank-date alignment.
- A semantic **swap** ships: `Cleared` now means *matched to a download*, and `cleared` means *eyeballed*. Historical data is migrated (`Uncleared→cleared`, `Cleared→matched`); already-`reconciled` (locked) statements are unaffected in meaning. Legend + tooltips mitigate the relearning.
- No-download institutions stay first-class via the optional-cleared reconcile path.
- Removes an 8-way duplicated constant (tech-debt paydown) behind one enum.
- Two migrations (0033 status rename + `CHECK` rebuild via the ADR-032 recipe; later 0034 `bank_posted_date`), and touch points across the register delegate, dialogs, bulk-edit, filter, reconcile queries, and the import service.

## Phased implementation plan

Each phase is independently shippable and testable (Qt-free unit tests where possible + offscreen smokes, per house style).

**Phase 1 — Status model foundation (mechanical, low-risk).**
- New `mfl_desktop/txn_status.py` (ordered enum + display metadata + helpers); replace the ~8 duplicated `STATUSES` tuples and the `filter_proxy`/`cli` references.
- Migration **0033**: rebuild `txn` with `CHECK(status IN ('pending','cleared','matched','reconciled'))` (ADR-032 table-rebuild recipe, preserving indexes incl. `idx_txn_status`), and rewrite data (`Pending→pending`, `Uncleared→cleared`, `Cleared→matched`, `Reconciled→reconciled`). Update every literal-status validation in `repository.py` (four sites) + `csv_parser` status map.
- Register colours/icons + a status legend/tooltips. Behaviour otherwise unchanged.
- Tests: migration up-through-0033 on a copy; status round-trip; a grep-guard test that no literal old-status strings remain outside `txn_status.py`.

**Phase 2 — Reconcile by confidence (no schema change).**
- Reconcile candidate set = `matched` (was `cleared`); rename/repoint `list_cleared_unreconciled_txns`. Add the **"include cleared"** per-reconcile toggle. Surface a **cleared-in-range warning list** when cleared are excluded.
- Tests: candidate-set selection by status; toggle behaviour; a regression building the June scenario (pending + duplicate items must not be auto-selected).

**Phase 3 — Precise OFX matching + bank date (the substantive phase).**
- Migration **0034**: add `txn.bank_posted_date TEXT NULL`.
- Upgrade the `import_service` ±2-day heuristic into a **scored matcher** (amount exact + date window ±5 + canonical-payee fuzzy), producing three buckets. New **import review screen**: **auto-matched**, **needs-review** (incl. amount-differs → *adopt bank amount*), **new-from-bank**; plus an **unmatched-entries** panel ("not seen in this download").
- On accept: `pending`/`cleared → matched`, set `bank_posted_date` from OFX, keep FITID dedup. Reconcile date-ranging prefers `bank_posted_date`.
- Tests: matcher unit tests (exact/near/amount-diff/duplicate/none); idempotent re-import; status transitions; reconcile date-ranging on `bank_posted_date`.

**Sequencing note:** Phases 1–2 already deliver the reconciliation-safety win (the June bugs can't recur) without the import work; Phase 3 delivers the precision the owner asked for. Ship 1 → 2 → 3.
