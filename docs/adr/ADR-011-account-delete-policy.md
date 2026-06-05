# ADR-011 — Account delete policy: hard delete now, soft archive reserved

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design) — schema reserves `account.archived_at` for a future soft-delete UX

---

## Context

The desktop app exposes basic account management as part of the v0.1 desktop milestone (create / edit / delete). The schema defined in ADR-010 supports two distinct ways to remove an account from the active set:

1. **Hard delete** — `DELETE FROM account WHERE id = ?`. Foreign-key cascades (`ON DELETE CASCADE`) automatically remove the account's transactions, import batches, lots, and valuations. The row is gone; there is no recovery short of restoring from a SQLite file backup.
2. **Soft delete (archive)** — set `account.archived_at = datetime('now')`. The row stays in the database; `Repository.list_accounts()` already filters `WHERE archived_at IS NULL`, so an archived account silently disappears from the sidebar but its transactions remain intact and the row could be unarchived later.

Both are first-class possibilities in the schema. The decision is which to ship in the first version of the account-management UI.

The owner shares the packaged app with non-technical friends and family. Accidentally deleting an account with hundreds or thousands of imported transactions is a meaningful failure mode for that audience. At the same time, the explicit user request for this milestone was "delete account", not "hide account" — and reserving a UI slot for an Archive operation that the user did not ask for would expand scope.

## Options considered

### Option 1 — Hard delete only, with a strong confirmation dialog (chosen)

The Account menu offers a single "Delete Account…" action. The confirmation dialog states the operation is permanent and shows the count of transactions that will be cascaded:

> Delete account 'Joint Current'?
>
> This will also permanently delete 1,247 transactions and any associated import history. This cannot be undone.

Selecting Yes calls `Repository.delete_account`, which runs `DELETE FROM account` and lets FK cascades handle the dependent rows.

This matches the user's stated request, keeps the menu simple, and uses the SQL-level cascade rather than introducing a hand-rolled "archive" flow that the user may not want.

### Option 2 — Soft archive only

Reject. The user asked for "delete", and a Hide-only UX leaves the database accumulating accounts the user thought were gone, which is its own confusion.

### Option 3 — Both Archive and Delete in the first version

Reject for now. Adds a second menu item, a sidebar filter for "show archived", and an unarchive flow — all before the user has asked for any of it. Better to ship the requested verb and add Archive as a deliberate addition once the user has a concrete need for it (e.g. closing a credit-card account whose history should still appear in historical reports).

## Decision

The desktop app's account-management UI exposes **hard delete only**, with a confirmation dialog that names the account, states the deletion count, and warns that it cannot be undone. `Repository.delete_account` performs an unconditional `DELETE FROM account`; cascading is handled by the schema-level FK constraints from ADR-010.

The schema-level `archived_at` column is **retained as reserved** for a future Archive UX. `Repository.list_accounts` continues to filter it out, so adding an archive flow later requires no schema migration — only a Repository method and a sidebar filter.

## Consequences

### Positive
- One menu item, one confirmation, one verb — matches the user's stated intent and a non-technical user's mental model of "delete".
- Cascading is enforced by SQL, so an account can never end up half-deleted (orphaned transactions, dangling import batches, etc.).
- The Archive door stays open: schema, list-accounts filter, and a future `archive_account` / `unarchive_account` repository pair can be added without touching the storage layer.

### Negative / trade-offs
- A confirmed delete cannot be undone from inside the app. Recovery requires restoring a SQLite file backup.
- Use cases that genuinely want "hide but preserve history" (a closed credit card whose old transactions should still appear in long-range reports) are not supported until the Archive UX is built.

### Ongoing responsibilities
- The confirmation dialog must always show the transaction count before deleting an account that has any. The Repository surfaces this via `count_account_transactions`; the UI must call it before showing the prompt.
- When (not if) Archive is added, the menu becomes "Archive Account" + "Delete Account…" with distinct verbs, distinct icons, and the Delete confirmation kept strong. The presence of `archived_at` in the schema means this is an additive change, not a migration.
