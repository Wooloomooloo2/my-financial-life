# ADR-012 — Payee name-management policy: rename collision rejects, merge is the explicit reassign verb

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design) — defines `payee` with `UNIQUE(name)` and `txn.payee_id ON DELETE SET NULL`

---

## Context

The desktop app's first payee-management UI exposes four operations: add a payee, rename a payee, merge several payees into one, and delete payees. Two policy questions affect user-visible behaviour and are worth recording explicitly:

1. **What happens when a rename produces a name that already exists?** The `payee.name` column is `UNIQUE` (ADR-010), so the database cannot store two rows with the same name. The application has to either (a) refuse the rename and tell the user, or (b) auto-merge — re-point the renamed row's transactions onto the existing row and delete the renamed row.
2. **What happens to transactions when a payee is deleted?** The schema's `txn.payee_id` FK is `ON DELETE SET NULL`, so the database will preserve the transaction rows and clear their `payee_id`. The UI has to make this consequence visible to the user before they confirm.

Both questions concern *user trust* more than implementation. Bulk operations that silently re-arrange historical data — especially via what looks like a single-row edit — are a class of footgun this app is meant to avoid for a non-technical audience.

## Options considered

### Option 1 — Rename collision **rejects**; Merge is the only verb that reassigns transactions (chosen)

A rename that would collide with another existing payee fails with a clear error: *"Another payee named 'Tesco' already exists — use Merge to combine them instead."* No data moves. If the user actually wanted to combine the two payees they select both, click Merge, choose a target, and get an explicit confirmation dialog ("Merge 2 payees into 'Tesco'? Their transactions will be reassigned and the merged-from payees will be deleted.") before anything changes.

Merge is the single, deliberate verb for reassigning transactions across payee identities. Rename is single-row, non-destructive, and visually obvious. Each verb does exactly what it says.

### Option 2 — Rename collision auto-merges

Rejected. A user typing a small typo correction (`Tesco Express` → `Tesco`) does not expect their action to reassign hundreds of transactions and delete a row. Auto-merge on rename collapses two distinct verbs into one and removes the explicit confirmation step from the destructive path.

### Option 3 — Rename collision prompts for choice

Rejected. Adds a confirmation dialog to every rename to handle an uncommon case; the same dialog is better placed on the Merge verb where the user has already declared destructive intent.

## Decision

**Rename collisions reject with an error directing the user to Merge.** `Repository.rename_payee` is implemented to check for a colliding row before the UPDATE and raises `ValueError` if one is found; the dialog catches and displays the message.

**Merge is the explicit destructive verb.** The user selects 2+ payees and chooses a target — either one of the selected payees, or a brand-new name they type at the point of merging when none of the existing names is the right canonical form (e.g. merging `Tescos` / `Tesco's` / `TESCO` into a fresh `Tesco`). Typing a name that matches an existing payee **outside the current selection** is rejected, so a single merge can never silently pull in payees the user hasn't chosen. The user confirms a dialog that names the target and states the reassignment count, and only then are transactions re-pointed and the source payees deleted. `Repository.merge_payees` does this in a single SQL transaction so the operation is either fully applied or fully rolled back; if the brand-new target row was created before confirmation and the user cancels, it is deleted.

**Delete preserves transactions and clears their payee.** The schema's `ON DELETE SET NULL` rule on `txn.payee_id` does the work. The delete confirmation dialog explicitly tells the user *"Any transactions using this payee will keep their other fields and show a blank payee."* so the consequence is not hidden behind a generic "delete" verb.

## Consequences

### Positive
- Three distinct verbs map to three distinct user intents. No verb does anything destructive without an explicit confirmation that names the affected rows.
- The data layer's invariants (uniqueness on payee name, cascade behaviour on payee delete) are surfaced to the user with language they can act on, not hidden behind retry loops or silent merges.
- The same shape is reusable for category management (the upcoming work), which has the same rename / merge / delete trio.

### Negative / trade-offs
- A user who really wants the "rename onto an existing name" shortcut has to do two clicks (select both, click Merge) instead of one (rename to the existing name). This is a deliberate friction — the destination is the same; the path is just slower.

### Ongoing responsibilities
- The Merge target is either one of the selected payees or a brand-new name typed at the picker. The picker must reject any typed name that matches an existing payee outside the current selection, so a single Merge can never silently pull in payees the user has not chosen. This keeps Merge a confined operation that only touches the user's current selection (plus, at the user's explicit request, a fresh target).
- If a brand-new target payee is created during the picker, and the user then cancels the confirmation, the new payee row must be deleted. Otherwise a cancelled merge would leave an orphan payee with no transactions.
- The Delete confirmation must always mention that transactions are preserved and rendered with a blank payee, so the FK rule is never a surprise.
- When category management is built (ADR-013, planned), it should follow the same policy by default: rename collisions reject; merge is the explicit reassign verb; merging into a brand-new name is allowed when none of the existing options is the right canonical form. Categories add a hierarchy dimension (reparent), but the rename / merge / delete trio is the same shape.
