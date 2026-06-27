# ADR-118 — Import-time category review, transfer references, and import undo

**Date:** 2026-06-27
**Status:** Accepted
**Related:** ADR-112 (category import map + match-only — the curation-preserving layer this builds on). ADR-111 (QIF cash import — already handled `[Account]` transfer refs; the generic-CSV path did not). ADR-037 (Reconcile Transfers — where the noted transfers get paired). ADR-085 (cross-source duplicate review — the sibling pre-commit dialog). ADR-010/021 (import flow).

## Context

Importing the owner's **Amex Gold** export (a generic CSV, 2,667 rows) created
categories that shouldn't exist — the second time an import silently forked the
tree (REI was the first, ADR-112). Inspecting the live file showed two distinct
causes:

1. **A bug.** Banktivity writes transfers in the category column as a bracketed
   account name in the last path segment — `[Chase Checking]` or grouped
   `Transfer:[Chase Checking]`. The **QIF** importer recognises this (ADR-111);
   the **generic-CSV** path did not, so `[Chase Checking]` was created as a
   category and 123 transactions were filed under it. The per-payee
   auto-categorisation (ADR-072) then even re-categorised some of them as spend.

2. **Working-as-designed, but wrong default.** ADR-112's map only re-routes
   categories the user previously *merged/deleted/reparented*; a brand-new name
   (`Household:General Items`, `Gift Received`) is still **created** because
   match-only defaults off. Right for a first import, wrong for an established
   tree — and not what the owner expected.

The owner chose: an **import-time review** of would-be-new categories, with
transfer references auto-handled; and **undo + re-import** to clean up the run
that already happened.

## Decision

**1. Transfer references are never categories (central, all formats).**
`import_service._transfer_ref_account` detects a bracketed **last** segment
(`[Account]` or `…:[Account]`). Such a value resolves to Uncategorised, gets a
`Transfer to/from {account}` memo note (mirroring the QIF path), and is excluded
from the auto-categorisation fill — so it stays an unclassified transfer for
*Reconcile Transfers* (ADR-037) to pair, never a spend, never a bogus category.

**2. Import-time new-category review.** Before commit, `plan_new_categories`
returns the distinct source categories the import would create (excluding empty,
transfer refs, mapped paths, and existing paths). If any, the new
`ImportCategoryReviewDialog` lists them and the user picks, per path: **map** to
an existing category, **create** it, or send to **Needs Review**. A *map* choice
is also recorded as an import mapping (ADR-112), so the next import of that path
follows automatically. `commit_import` takes a `category_decisions` dict and
applies it ahead of the create-or-match-only logic. No new categories ⇒ no
dialog (silent commit, unchanged for clean imports).

**3. Undo an import — with empty-category cleanup.** New
`Repository.delete_import_batch` deletes exactly the transactions a batch created
(splits/reconcile rows cascade), then the batch row (txns first —
`import_batch_id` is `ON DELETE SET NULL`). It returns the categories *that batch
populated* which are now empty + import-sourced; the **File ▸ Undo Import…**
dialog offers to delete them via `delete_empty_import_categories` — a **direct**
delete that records **no** Needs-Review mapping (unlike the ADR-112
`delete_category`), so a re-import re-offers them in the review dialog instead of
silently re-using leftovers. The empty check is conservative (no txns, split
lines, children, schedules, or budget lines reference it). Categories an
*earlier*, already-undone import left orphaned aren't attributable to this batch
and so aren't offered — those are a manual delete.

## Consequences

- Imports no longer silently fork the category tree: a genuinely-new category is
  a deliberate choice, and a Banktivity transfer reference is recognised as a
  transfer on every format, not invented as a category. Match-only (ADR-112)
  remains available; the review dialog is the lighter-touch default that also
  catches near-duplicates by letting the user map.
- The default per-row choice is **Create**, so a user who just clicks *Import*
  gets the old behaviour — the dialog informs, it doesn't force a workflow.
- Undo is txn-scoped: rows merged into a pre-existing manual transaction
  (ADR-010) aren't batch-created and aren't removed; imported cash rows are never
  transfers, so there are no partner rows. Categories a prior import created are
  not deleted by undo — re-importing cleanly may mean removing now-empty
  categories by hand (documented for the owner's Amex cleanup).
- Service/UI/repo layer only; no migration, no schema change. Verified end-to-end
  on a copy of the live file + the real `Amex Gold.csv`: 2,667 imported, the 123
  `[Chase Checking]` rows Uncategorised + noted, zero bracket categories, only
  `Household:General Items` + `Gift Received` flagged new. Qt-free
  `tests/test_import_category_review.py` 6/6; offscreen dialog smokes;
  ADR-112 map tests still 11/11.
