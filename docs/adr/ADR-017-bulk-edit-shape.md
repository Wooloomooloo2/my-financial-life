# ADR-017 — Bulk edit shape: modal dialog with per-field checkboxes

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design); ADR-012 / ADR-013 (Payee / category management policies — bulk edit reuses their pre-existing repository methods rather than going around them)

---

## Context

The register supports inline single-row editing today: double-click a cell, type or pick a value, commit. After imports — where dozens of similar transactions land at once and need re-categorising, a payee normalising, or a batch confirmation as `Cleared` — repeating that single-row interaction is the slow path. Bulk edit closes that gap.

Three UX shapes were considered. The decision drives where the code lives and how the user thinks about the operation.

## Options considered

### Option 1 — Modal dialog with per-field checkboxes (chosen)

Select N rows in the table, trigger Bulk Edit (Ctrl+E or context menu), a modal dialog opens with one row per editable field. Each row has a leading checkbox plus the field's editor (line edit / combo). Only checked fields are applied; unchecked fields are left alone. Empty Payee or Memo with the checkbox ticked clears the field on every selected row.

- Pros: composes naturally — *"set category AND status, leave payee and memo alone"* is one click per intent. Familiar UX (the same pattern is in countless mail clients and DAWs for "edit selection properties"). The dialog is one new file and the repository change is one atomic method.
- Cons: takes an extra click compared with single-key shortcuts for the common "just change status to Cleared" case. Mitigated by making the dialog small and keyboard-friendly (Tab through checkboxes, Enter to Apply).

### Option 2 — Persistent bulk bar above/below the table

When 2+ rows are selected, a bulk bar slides into view containing the same field editors plus an Apply button. The bar disappears when selection drops below 2.

- Pros: lower friction for the common case — no dialog to dismiss; the user sees the fields without an explicit action.
- Cons: eats permanent screen real estate when not in use (slides in/out is its own polish problem). Mixes "currently editable" UI with "what's selected" UI in the same visual region, which is hard to make non-jarring in Qt. Bigger surface for race conditions (what if the user clears selection while the bar has unapplied input?).

### Option 3 — Per-field actions on the context menu

Right-click selection → "Set Category…", "Set Status…", "Set Payee…", "Set Memo…". Each opens a small dedicated dialog for that one field.

- Pros: very discoverable for a single-field change.
- Cons: two simultaneous changes (category + status) need two prompts and two reloads; the "atomic across N rows" feel is lost. Also duplicates the inline editing path semantically for the single-field case.

## Decision

**Modal dialog with per-field checkboxes** (Option 1). The dialog's fields:

| Field | Editor | Default when checked | Clear-on-empty? |
|---|---|---|---|
| Payee | QLineEdit | empty | yes — empty text clears `payee_id` to NULL |
| Category | QComboBox of `CategoryChoice` | Uncategorised | no — categories are NOT NULL; the user must pick |
| Status | QComboBox of the four enum values | Cleared | no — status is NOT NULL |
| Memo | QLineEdit | empty | yes — empty text clears `memo` to NULL |

**Apply** validates that at least one box is ticked (and that any checked Category has a real id), then closes the dialog with the values dict ready to ``**``-expand into `Repository.bulk_update_transactions`.

**Repository method** is one call: `bulk_update_transactions(txn_ids, *, payee_name=_UNSET, category_id=_UNSET, status=_UNSET, memo=_UNSET)`. A module-level `_UNSET` sentinel distinguishes "don't change this column" from "set this column to None/empty" — `None` couldn't be the sentinel because `None` is the meaningful "clear" value for nullable columns. All field updates run inside one SQL transaction; a failure mid-way rolls back the lot, so the user never sees a half-applied bulk edit.

**Triggers**:
- **Transaction menu → Bulk Edit Selected… (Ctrl+E)** — primary entry, discoverable via menu.
- **Right-click on a multi-row selection → "Bulk Edit N Transactions…"** — appears in the table context menu only when ≥2 rows are selected.

**Enable rules**: bulk edit requires ≥2 rows. Single-row edits use the inline cell editor that's already there — exposing the bulk dialog for one row would just be a noisier path to the same outcome.

**Post-apply**: the model reloads (`_model.reload`) so the table shows the new values. Sidebar balances are *not* refreshed because no bulk-edited field changes amounts — payee/category/status/memo edits are display-only from the balance perspective. (Skipping that refresh keeps the post-apply latency tighter on large selections.)

## Consequences

### Positive
- A single click per *intent*: change one field on N rows or change three fields on N rows, same dialog, same Apply.
- Atomic: either every field on every row updates, or nothing does. No "half the transactions got the new category" failure mode.
- Reuses the existing `get_or_create_payee` path so bulk-setting a payee that didn't exist creates it once, same as inline editing would.
- The empty-clears rule for Payee and Memo gives the user a fast way to clean up imported memos in bulk without typing a sentinel value.

### Negative / trade-offs
- The "all-checked-equal-to-empty" semantics for clearing requires a hint label in the dialog. A non-technical user could otherwise wonder whether ticking Payee + leaving the field empty means "clear it" or "I forgot to type it". The hint is shown unconditionally so the rule is always visible.
- Mass undo isn't available. If the user bulk-changes 200 transactions and immediately regrets it, the only recovery is to bulk-change them back (or restore from a `.mfl` snapshot per ADR-016).

### Ongoing responsibilities
- Any new editable column on `txn` (e.g. a future Tag field) gets a new row in the dialog with the same checkbox-plus-editor pattern, plus a new optional kwarg on `bulk_update_transactions`. The pattern doesn't change.
- If a future feature (typeahead delegates per the polish backlog) ships first for inline editing, bulk edit should adopt the same widgets in its corresponding rows so the user gets consistent behaviour — typing a category in the bulk dialog should suggest from the same list as the inline editor.
- A future "Undo last bulk edit" feature would need a per-transaction snapshot of pre-change values; not built in v1.
