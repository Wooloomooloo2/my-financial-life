# ADR-085 — Cross-source, count-aware duplicate detection on import

**Date:** 2026-06-19
**Status:** Accepted.
**Amends:** ADR-010 §6 (import classification), the no-dialog-for-known-imports rule (a dialog now appears *only when duplicates are detected*), ADR-077 (bank-feed imports inherit this automatically via `_classify_and_stage`).
**Related:** ADR-036/037 (reuses `transfer_reconcile.greedy_pair` + `score_candidate`); `docs/RELEASE_1.0_BACKLOG.md` workstream **F2** (feed robustness — cross-source dedup is the same problem for feeds vs files).

---

## Context

A real import duplicated ~46 June transactions that were already present. Root cause, confirmed against the live data:

- Duplicate detection keyed on **exact `import_hash`** (`Repository.import_hash_exists`). For OFX/QFX that hash is the bank's **FITID**; for the earlier **Banktivity/CSV migration** it's a **composite `date|amount|payee` hash**; a bank **feed** uses its own `transactionId`. The *same* real transaction arriving from a *different source* therefore carries a *different* hash → no match → duplicate inserted.
- The only fuzzy fallback (`find_manual_match`, ±2 days) was restricted to **manually-typed rows** (`import_hash IS NULL`). Previously-imported originals have a hash, so it skipped them.
- Known-format imports **commit silently** (no review), so the duplicates went straight in with no chance to catch them.

A naive "if a matching (date, amount) row exists, skip the incoming one" is **wrong** and was explicitly rejected by the owner: people legitimately spend the **same amount at the same payee multiple times in a few days** (£8.90 ScotRail Mon/Tue/Thu; £6.80 Caffè Nero 2–3×/week). Existence-based skipping would drop genuine repeats. The check must compare the **number** already present against the **number** on the import and only treat the **excess** as new.

---

## Decision

### Count-aware matching by consume-once pairing (the crux)
Match each import row to a **distinct** existing register row; each existing row can be claimed **at most once**. This is exactly `transfer_reconcile.greedy_pair` (highest-score-first, one claim per source/target id), reused unchanged.

- Register has **1** × £8.90 ScotRail, import has **3** → 1 claims the single existing row → **1 duplicate, 2 new.** ✅
- Register has **3**, import has **3** → all claimed → 3 duplicates.
- Register has **3**, import has **2** → 2 claimed, the 3rd existing copy untouched.

Multiplicity safety comes from **consumption**, not from any threshold — so even three identical strong matches can never skip more rows than actually exist.

### Matching predicate + scoring (`mfl_desktop/import_engine/dedupe.py`, pure)
- **Hard filters:** same account, **exact signed amount** (pence), posted date within **±`window_days` (default 2)**.
- **Score** via `transfer_reconcile.score_candidate` (`amount_mismatch_pct=0`, `currencies_match=True`): same-day scores highest, payee-token overlap adds a nudge — so the greedy walk pairs same-day / same-payee first (this is what stops `Tue ScotRail` from stealing `Tue OtherShop`'s existing row).
- **Strength** drives only the *default* review state: **strong** = same-day **or** payee-token overlap (cross-source payee text is unreliable — `A AND B COUNCIL` vs `Argyle and Bute Coun` share no token — so same-day is the dependable strong signal); **weak** = exact amount within window but different date and no payee overlap.

### Resolution depends on what was matched
The matched existing row is either a **manual placeholder** (`import_hash IS NULL`) or an **already-imported** row:
- **manual target →** confirm = **merge** the incoming hash into the placeholder (existing `merge_into_manual_transaction`, ADR-010 behaviour preserved). Manual matches are always "strong" (default-confirm), matching the old auto-accept.
- **imported target →** confirm = **skip** the incoming row (it's a true cross-source duplicate; the existing row is never touched — so even **Reconciled** originals are safe to match against, and are included as targets per the owner decision).

### Review only when there's something to ask
- **No matches found →** commit silently as today (the no-dialog-for-known-imports rule holds for clean imports).
- **Matches found →** a new `ImportReviewDialog` lists each pair (import row ↔ matched existing row) with a strength chip and an "Already present?" toggle. **Defaults (owner-locked):** strong → ticked (skip/merge); weak → unticked (added, user opts in). Bulk **Skip all / Keep all / Reset to suggested**. The count is shown so "spent 3, 1 already in → skipping 1" is explicit. Nothing is ever discarded without being shown.

### Exact-hash fast path unchanged
Same-file re-import still short-circuits on `import_hash_exists` → `duplicate` (auto-skip, no review). The fuzzy pass excludes existing rows whose hash is in the current batch, so an exact-dup target can't also be fuzzy-claimed.

---

## Consequences

### Positive
- Closes the cross-source duplicate hole for **file re-imports across formats, feed-vs-file, and migration-vs-bank** in one place (`_classify_and_stage`), so feeds (ADR-077) inherit it.
- **Legitimate same-amount repeats are preserved** by construction — the consume-once pairing, not a heuristic, guarantees the count is right.
- The clean-import experience is unchanged (still silent); the dialog appears strictly when there is a decision to make.

### Negative / trade-offs
- Cross-source payee text is too divergent to rely on, so genuine duplicates that fall on **different dates with no payee overlap** surface as *weak* (unticked) — the user must opt in to skipping them. Deliberate: the safe error is "shown but not auto-skipped," never "silently dropped."
- A per-import O(import × existing-in-window) pairing pass. Bounded by the date window and one indexed query; negligible for normal statement sizes.
- Investment rows keep their existing action+security+quantity hash path and are **not** fuzzy-matched in this round (their duplication shape differs; a future round can extend if needed).

### Ongoing responsibilities
- New import sources route through `_classify_and_stage` to inherit dedup — never insert straight to the Repository.
- The strength/scoring lives in the pure `dedupe.py` + `transfer_reconcile` — tune in one place; never reintroduce an existence-only skip.
