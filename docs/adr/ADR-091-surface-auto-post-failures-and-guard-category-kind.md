# ADR-091 — Surface auto-post failures, and guard category kind changes that orphan schedules

**Date:** 2026-06-20
**Status:** Accepted
**Related:** ADR-074 (capture a scheduled transfer's destination at post time), ADR-023 (scheduled transactions), ADR-020 (category-driven transfers), ADR-014 (category kind changes). Owner-reported bug.

---

## Context

A fixed-amount expense schedule (Polestar 2 → "Asset Depreciation", −£450/mo, auto-post on) was shown overdue in the Schedules dialog, several occurrences behind, despite the app having been launched dozens of times since the due date — and it never auto-posted.

Tracing it: the launch sweep `Repository.auto_post_due` ran every launch and tried to post the schedule every time, but `post_scheduled_txn` raised **"Transfer schedule is missing a destination account."** and the sweep swallowed it:

```python
try:
    txn_id = self.post_scheduled_txn(sid)
    ...
except Exception:
    break   # silently skip the rest of this schedule's catch-up
```

The schedule was transfer-kind with `transfer_to_account_id = NULL` — the exact state ADR-074 documents — because its category, "Asset Depreciation", had been **switched from expense to transfer kind after the schedule was created** (`change_category_kind`). ADR-074 fixed the *manual* Post Now path (prompt for a destination, persist it) but explicitly left the auto-post sweep swallowing failures. So the only visible symptom was a schedule that looked permanently due and never posted, with no error, no log, no count — invisible launch after launch.

Two gaps, then:

1. **The sweep hides per-schedule failures.** "Refusing to abort the whole launch over one bad schedule" is right; *dropping the failure on the floor* is not. The user has no way to learn a schedule is broken.
2. **Changing a category to transfer-kind silently orphans dependent schedules.** `create_scheduled_txn` rejects transfer-kind-without-destination at setup, but `change_category_kind` applies the kind flip with no check on schedules already pointing at the category — manufacturing the unpostable state ADR-074 then has to repair.

---

## Decision

**(1) Surface auto-post failures instead of swallowing them.**

`auto_post_due` now returns an `AutoPostResult` — `posted: list[int]` **and** `failures: list[AutoPostFailure]` (each carrying `schedule_id`, a human label `"Account — Category (Payee)"`, and the exception message) — instead of a bare `list[int]`. Per-schedule failures are still caught (one bad schedule never aborts the launch or the other schedules' catch-up), but they're recorded, not dropped.

`RegisterWindow._run_auto_post_sweep` shows the posted count as before (quiet on a clean run), and now, when there are failures, raises a `QMessageBox.warning` listing each failed schedule and its reason and pointing the user at Schedules → Post Now (where ADR-074's destination-capture fix lives). A clean launch stays silent.

**(2) Guard category-kind changes that would orphan a schedule.**

`change_category_kind`, when the new kind is `transfer`, first finds every **active** schedule using the category (or any descendant) with `transfer_to_account_id IS NULL`. If any exist it **refuses** the change with a `ValueError` naming the offenders (the Categories dialog already surfaces this as a warning), telling the user to set a destination on each (or pick a non-transfer kind) first. No partial application — the kind flip and the orphaning are prevented together.

Refusing (rather than auto-repairing) is deliberate: a destination account is a user choice the system can't infer, exactly as ADR-074 concluded for the post-time prompt.

---

## Consequences

- The reported symptom — a schedule silently never auto-posting, launch after launch — is now impossible to miss: the sweep tells the user which schedules failed and why.
- The most common *cause* of that state (a post-hoc category-kind change) is blocked at source, so the orphaned-schedule case becomes rare rather than routine.
- ADR-074's manual repair path is unchanged and remains the fix for any schedule that still ends up destination-less (e.g. seeded that way): the warning now routes the user straight to it.
- Return-type change to `auto_post_due` (`list[int]` → `AutoPostResult`); the only caller (`_run_auto_post_sweep`) is updated. No schema change, no migration.

### Rejected alternatives

- **Keep swallowing, just log to a file** — the audience doesn't read logs; an invisible failure is the whole bug. A visible warning is the point.
- **Auto-repair the schedule when posting fails** (e.g. pick any account) — guesses a destination the user must choose; ADR-074 already rejected post-without-persist for the same reason.
- **Auto-clear the destination requirement / post it as a plain expense** — silently reinterprets a transfer as spending; corrupts reports.
- **Block the kind change only in the dialog** — the Repository is the integrity boundary (CLI/scripting call it too); the guard belongs there, with the dialog merely surfacing it.

---

## Verification

Offscreen against copies of the live file: `auto_post_due` on a file whose "Asset Depreciation" category is transfer-kind and whose schedule lacks a destination returns `posted=[]` and a `failures` entry naming the schedule with reason "Transfer schedule is missing a destination account." (previously: silent empty result). `change_category_kind(90, "transfer")` on a file where that category is still expense and a schedule uses it without a destination raises a `ValueError` naming the schedule; changing an unrelated category with no dependent schedules to transfer-kind still succeeds.

## Data note

Existing `.mfl` files that already hit this (their "Asset Depreciation" category was flipped to `transfer`) are repaired by setting the category back to its correct kind — `UPDATE category SET kind='expense' WHERE id=<id>` — or by giving each affected schedule a destination via Schedules → Post Now (ADR-074). The live `mfl_dev.mfl` already had the category back at `expense`; the stale `mfl_dev.db` / `mfl_dev_windows.mfl` copies still carry the `transfer` value.
