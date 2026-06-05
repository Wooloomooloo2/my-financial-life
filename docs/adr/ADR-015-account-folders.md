# ADR-015 — Account folders in the sidebar

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design) — `account` table extended here

---

## Context

The owner is moving from Banktivity and wants the sidebar UX they're used to: accounts grouped into named, collapsible folders, with each row showing a balance, folders showing the sum of their members when collapsed. As the number of accounts grows (everyday spending account, joint account, three savings goals, two credit cards, a property valuation, a brokerage…) a flat sidebar list stops being useful — the owner ends up scanning a wall of names to find the relevant one. Folders restore the ability to group by purpose ("Personal", "Joint", "Long-term") rather than by family ("cash", "credit", "investment").

Three independent decisions follow:

1. **How are folders modelled in the schema?** One option is a generic tree (`parent_id` self-reference on `account_folder` so folders can nest). The simpler option is a flat list — every folder is at the sidebar root, and every account is either in one folder or at the sidebar root.
2. **What is the folder's contract in the sidebar?** Is a folder a selectable node that *also* drives the register view (Banktivity's "show all transactions in folder" model)? Or is it purely a grouping widget that only expands/collapses?
3. **How are multi-currency folders summed?** Adding a £-denominated current account and a $-denominated travel card under one folder makes "sum in pence" semantically incorrect.

## Options considered

### Folder hierarchy — flat (chosen) vs nested

- *Flat*: one level. Every folder at sidebar root. Every account either at root or inside one folder.
  - Pros: simplest schema (`account_folder` doesn't self-reference), simplest sidebar walk (one level of children under each folder), no cycle-prevention needed, no UX question about "drop folder under folder vs at root".
  - Cons: a user with many accounts may eventually want sub-folders ("Joint → Savings", "Joint → Holiday fund"). Future work.
- *Nested*: `account_folder.parent_id` self-references.
  - Pros: matches the category model's depth.
  - Cons: complexity not required by the current use case; cycle prevention; sidebar walk becomes recursive; UI of moving a folder under another folder is a bigger feature surface.

Chosen: **flat**. If nesting is wanted later, add a `parent_id` column in a follow-up migration (additive) and walk the tree in the sidebar — the existing flat code is still correct as a one-level special case.

### Folder selection contract — display-only (chosen) vs selectable view mode

- *Display-only*: folders show their name and a sum, expand/collapse on row click, but are not selectable. The register view only changes when the user clicks an account (or "All transactions").
  - Pros: no new view mode; the sidebar's "what's currently shown" contract is unchanged from the old flat list (account ↔ register, "All" ↔ aggregate); minimal UI for v1.
  - Cons: a user used to Banktivity's "click folder → see all transactions in folder" gets less than they're used to.
- *Folder = view mode*: clicking a folder shows all transactions across its members. Adds a third view mode alongside single-account and all-transactions.
  - Pros: full Banktivity-style.
  - Cons: needs a new model layout (Account column shown like all-transactions; running balance still not meaningful), a new Repository method (`list_transactions_for_folder`), and new code paths in the import / new-transaction guards (which folder do you import into? none — guard remains "single account selected only"). Larger surface, more bugs.

Chosen: **display-only** for v1. The folder-as-view-mode feature is recorded in the backlog and can be added later as a clean extension of the existing all-transactions code path.

### Multi-currency folder sums — naive (chosen) vs per-currency

- *Naive*: sum all member balances together regardless of currency. The number shown next to a folder containing £ and $ accounts is the sum of the raw amounts.
  - Pros: trivial to compute; matches what the existing all-transactions view does (which also doesn't convert currencies).
  - Cons: arithmetically meaningless when currencies differ.
- *Per-currency*: show e.g. `£1,234.50  ·  $620.00` in the folder's balance cell.
  - Pros: arithmetically honest.
  - Cons: column widths blow out; layout becomes awkward; in practice most users keep one currency for the foreseeable future.

Chosen: **naive sum** for v1. Documented as a known limitation. Once the owner actually has multi-currency accounts grouped together, we revisit (probably per-currency rows or conversion to a base currency).

### Account reordering inside a folder — deferred

Per the owner's steer, reordering accounts within a folder is deferred. v1 sorts accounts inside each folder (and at root) by `(family, name)`, matching the old flat-list ordering. When implemented later, an `account.sort_order` column added by a new migration covers it.

## Decision

**Schema** (migration `0003_account_folders.sql`):

- New table `account_folder (id PK, name TEXT, sort_order INTEGER, archived_at TEXT)`.
- New column `account.folder_id INTEGER REFERENCES account_folder(id) ON DELETE SET NULL`. A null `folder_id` means "at the sidebar root".

**Sidebar**: `QListWidget` is replaced with a two-column `QTreeWidget` (Name | Balance, header hidden). Top item is the existing "All transactions" row. Below it: folders in `sort_order`, then root accounts. Folder rows show the sum of their members' balances; account rows show their own balance.

**Folder operations** exposed in v1:

| Verb | Where | Behaviour |
|---|---|---|
| New Folder | Account menu / sidebar context menu | Created at the end of the existing folder list (max sort_order + 1). |
| Rename Folder | Folder context menu | Free-text rename. No uniqueness constraint on folder names — two folders may share a name without error. |
| Delete Folder | Folder context menu | Folder row removed. Accounts inside fall to the sidebar root via FK `ON DELETE SET NULL`. No accounts or transactions are lost. |
| Move Up / Down | Folder context menu | Swaps `sort_order` with the immediate neighbour. No-op at the edges. |
| Move Account → Folder | Account context menu ▸ Move to Folder | Submenu lists existing folders, plus "(No folder)" to move out, plus "New Folder…" shortcut to create one and assign in the same step. |

**Balance computation** is opening_balance + sum(txn.amount) for every non-archived account, returned in one query by `Repository.compute_account_balances`. The sidebar joins by account id. Folder sums are computed in the sidebar code, not in SQL — keeping the SQL one-trick and letting future per-currency rendering happen at the same place that decides display format.

**Sidebar reload** runs after any operation that changes balances (transaction add/delete/import) and after any folder or account CRUD. Folder expansion state is reset on reload — acceptable for v1; preserving expansion is a small follow-up.

## Consequences

### Positive
- Accounts grouped by purpose, not just by family — matches the owner's Banktivity habit.
- Balance visible at a glance for each account *and* for each folder, with no extra clicks.
- Deleting a folder is non-destructive (accounts fall to root) so the user can experiment with grouping without fear of losing data.
- The Repository's `set_account_folder` is the single mutation path for membership — easy to extend later (drag-and-drop calls into the same method).

### Negative / trade-offs
- Folders are not selectable, so clicking a folder name doesn't show its aggregate transactions yet. Tracked in the backlog.
- Multi-currency folder sums are arithmetically naive. Tracked in this ADR as a known limitation.
- Folder names are not unique. Two folders both called "Personal" can coexist — visible to the user, easy to rename if they care.
- Folder expansion state isn't preserved across a sidebar reload (currently every reload re-expands all). Small follow-up.

### Ongoing responsibilities
- Any new code path that changes `account.folder_id` must go through `Repository.set_account_folder` so the contract (commit on success, atomic) is preserved.
- When account reordering is added (deferred), the migration adds `account.sort_order INTEGER NOT NULL DEFAULT 0`, and the sidebar's `accounts_by_folder` sort key becomes `(sort_order, family, name)` rather than `(family, name)` — additive change.
- When the folder-as-view-mode feature is added, the sidebar's `selection_changed` signal needs to grow a third payload kind (folder id) and the register window needs a `_show_folder(folder_id)` view mode mirroring `_show_account` / `_show_all_transactions`. The current sidebar already tags folder rows with a `KIND_ROLE` so the signal change is mechanical.
