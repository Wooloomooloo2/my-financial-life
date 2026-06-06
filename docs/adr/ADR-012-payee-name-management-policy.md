# ADR-012 — Payee name-management policy: rename collision rejects, merge is the explicit reassign verb

**Date:** 2026-06-05 (originally); **amended 2026-06-06** to add the canonical/alias model — see Amendment at the end.
**Status:** Accepted (amended)
**Related:** ADR-010 (Transactional schema design) — defines `payee` with `UNIQUE(name)` and `txn.payee_id ON DELETE SET NULL`; ADR-028 (Payee aliases arc planning); ADR-029 (round 1 implementation that motivated the amendment)

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

---

## Amendment 2026-06-06 — canonical / alias model (ADR-029 round 1)

ADR-029 added a self-referential `payee.canonical_id` column (planning recorded in ADR-028) and two new verbs to the Payees dialog: **Make Alias of…** and **Promote to Canonical**. The original three verbs from this ADR (Rename / Merge / Delete) continue to apply with the policy below.

### New verb policy

- **Make Alias of…** routes one or more sources at a chosen canonical target. Transactions referencing the alias rows are **left in place** — `txn.payee_id` continues to point at the alias row. Display, typeahead, and reports route through the canonical. The user can pick the target from the existing canonicals or type a brand-new name (a new canonical is created and the sources are aliased into it). The two-level invariant is enforced at the Repository: target must be canonical; source must not already have aliases of its own.
- **Promote to Canonical** drops the alias link on one or more rows. They reappear in the typeahead and are treated as distinct payees from their former canonical. No transaction data moves.

### Three distinct destructive shapes

After this amendment, the dialog exposes three semantically different ways to reassign or remove payees. Each maps to a distinct user intent:

| Verb | What happens to transactions | What happens to the source payee row |
|---|---|---|
| **Merge** | Re-pointed onto the target via `UPDATE txn SET payee_id = target` | Source rows are **deleted**. Aliases of sources are re-pointed onto the target (see "merge re-points aliases" below). |
| **Make Alias of** | Left in place, still pointing at the alias row's `id` | Source rows **stay**, with their `canonical_id` set to the target. Typeahead and reports route via the canonical. |
| **Delete** | `payee_id` set to NULL (FK rule) | Source rows **deleted**. Aliases of the deleted canonical auto-promote to canonical (FK rule on `canonical_id`). |

### When to use which

- **Merge** when the typo and the canonical represent the *same* historical reality and consolidating the rows is desirable. Loses the typo row from history but keeps the txns. Right answer for "I had three payees that were all really Tesco, kill them and keep one."
- **Make Alias of** when the typo represents a different bit of historical reality (a particular bank's POS string, an old branding) that's worth keeping as evidence in the database even though the user's preferred label is different. Doesn't move txns. Right answer for "TESC*GROCERIES 0123 LONDON is how the bank labelled my Tesco trips for years — keep the row, route the display through Tesco."
- **Delete** when the row was created in error (e.g. a manual entry typo on a payee with zero txns, or a payee that's no longer relevant and the user is OK with the txns going blank). Loses both the row and the txns' payee linkage.

### Merge re-points aliases of sources onto the target

The original ADR-012 merge transaction was simple: move txns, delete sources. After the canonical/alias amendment, merge has to handle a subtlety — what if one of the source payees has aliases of its own? The FK's `ON DELETE SET NULL` would silently auto-promote those aliases when the source is deleted, which is wrong for a merge: the user clearly wants everything under the target, not detached as freshly-canonical strays.

`Repository.merge_payees` was updated to re-point any aliases of the sources onto the target *before* the source rows are deleted, inside the same SQL transaction. This is the boring obvious correction — without it the merge would silently lose the user's stated intent.

### Rename collision: unchanged

Rename of either a canonical or an alias to a name that already exists on another payee row still rejects. The original ADR-012 policy stands.

### Aliases are filtered from the typeahead

`Repository.list_payee_names()` (which feeds the register's inline payee editor and the Bulk Edit dialog's completer) returns only canonical payees. Aliases are stored in the payee table but invisible to the typeahead — the user is suggesting their preferred label, not the raw historical strings. ADR-026's "fastidious vs not-fastidious user" dichotomy from the ADR-028 planning ADR plays out at the import-engine layer in round 2; round 1's filter is the user-facing piece.

### Two-level invariant is a Repository invariant

SQLite doesn't have a CHECK constraint expressive enough to prevent `payee → canonical → canonical` chains in pure DDL. `Repository.set_alias_of` rejects: targets that are themselves aliases; sources that already have aliases of their own. Aliases of aliases aren't possible by construction; aliases-of-the-thing-I-just-promoted-to-canonical is, but only after the canonical is canonical.
