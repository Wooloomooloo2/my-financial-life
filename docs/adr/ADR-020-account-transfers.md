# ADR-020 — Account transfers: category-driven, two linked transactions sharing one `transfer_id`

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-006 (Instance IRI naming); ADR-010 (Transactional schema design — `txn` table); ADR-014 (Category kind — `transfer` is one of the three kinds, with its own seeded root); ADR-018 (Spending Over Time — already excludes `kind='transfer'`); ADR-019 (Net Worth — already self-cancels for transfers)

---

## Context

The user moves money between their own accounts often — paying off a credit card from the current account, moving leftover salary to savings, contributing to an investment account, paying a mortgage. Each is one user intent ("move £X from A to B"), but it shows up on **two account statements**: an outflow on the source and a matching inflow on the destination. To keep balances correct *and* to keep reports honest (the same £X shouldn't appear as both income and spending), the data model has to represent this pair as a pair.

Two design dimensions follow:

1. **How is a transfer modelled in the schema?** Single row vs two linked rows vs two un-linked rows.
2. **What is the user-facing verb to create one?** A dedicated "New Transfer" menu item vs the category drives it.

The verb question matters because it determines where transfers fit in the existing UX. The user's instinct, after seeing the first cut with a dedicated dialog: "I would rather select 'transfer' as a category on any transaction and then have it prompt me for the account. Same functionality for bulk edits." That makes the **category** the trigger — picking a `kind='transfer'` category on a transaction prompts for the destination account, then creates the partner row.

This is a meaningfully better UX:

- **One creation flow.** New Transaction → pick a transfer category → prompted for the other side. No second menu entry.
- **Imported transactions become transfers via bulk edit.** Select 12 imported credit-card payments, bulk-edit category to "Credit Card Payment", system prompts once for the destination account, all 12 get partner rows. Same flow as any other bulk recategorisation.
- **Inline category edits work too.** Change one cell from "Groceries" to a transfer category, prompted for the destination, partner appears.
- **Category subdivisions become useful first-class.** Users can define "Credit Card Payment", "Pension Contribution", "Contribute to Savings", "Mortgage Payment" etc. under the seeded Transfer root and reports group by them naturally.

## Options considered

### Modelling — one row with two account FKs / two rows with a shared link / two rows with no link

- *One row, two account FKs* (`source_account_id`, `dest_account_id`): schema is honest about the one-intent-one-row mapping. But every existing query that does `WHERE account_id = ?` (the register, the sidebar balance, all reports) suddenly needs `OR account_id = ?` plus a sign flip on the amount depending on which side it's looking at. Most importantly, the existing one-account-per-txn invariant is the basis for `txn.account_id INTEGER NOT NULL` and every index built on it. Rejected — too invasive.
- *Two rows, no link*: a transfer is just two manually-entered transactions that happen to match. Easy to model; impossible to enforce. The user can delete one half and leave the other dangling; reports can't reliably exclude transfers because they have no way to know which expense/income rows are halves. Rejected.
- **Two rows with a shared link** (chosen): each half is a normal `txn` row — source has the outflow, destination has the inflow — and a `transfer_id TEXT` column on `txn` holds the same IRI on both. Every existing query keeps working unchanged. The link is queryable for any code path that wants the pair (delete, future edit-sync, future cashflow report).

### Verb — dedicated New Transfer / category-driven (chosen)

- *Dedicated New Transfer*: a separate menu item + dialog. Clear, but the dialog's fields (date, amount, category, memo) overlap heavily with New Transaction, the user has to know to use the right one, and the bulk-edit case (converting many existing rows) requires a second mechanism entirely.
- **Category-driven** (chosen): one creation flow. Picking a `kind='transfer'` category on any verb — New Transaction, inline category edit, bulk edit — triggers the same destination prompt and the same `create_transfer` / `convert_to_transfer` plumbing. Verbs are about *which rows* you're acting on; the *kind of action* is the category. Maps cleanly onto how the user already thinks about categories.

### Link identifier — sequential integer / UUID / IRI

- *Sequential integer (linking via own id)*: chicken-and-egg at insert time.
- *Plain UUID*: enough to identify; less self-documenting.
- **IRI `mfl:Transfer_<uuid8>`** (chosen): matches ADR-006's instance naming pattern; doubles as a future RDF identifier for the MRL bridge.

### Identifying transfer rows in the model

- *Mark by category alone* (every transfer uses a `kind='transfer'` category): rejected as the *only* mechanism — a user could later change the category to expense and the model would silently lose track of the pair. The `transfer_id` column is the source of truth for "this row is part of a transfer"; the category kind is the *semantic* tag for reports.
- *Both* (chosen): the dialog flow requires a transfer-kind category to trigger creation, and the `transfer_id` column records the pairing for delete + future sync.

### Payee convention on transfer rows

- *Leave NULL*: register shows blank payee for both halves. Hard to tell at a glance what the row means.
- **Convention-based** (chosen):
  - `create_transfer` (fresh transfer, both halves new) sets both halves' payees to `"Transfer to {dest}"` / `"Transfer from {src}"`.
  - `convert_to_transfer` (existing row becomes the source half) leaves the source's existing payee alone — an import-derived payee like "ACME LTD" is meaningful and shouldn't be clobbered; only the new partner row gets the `"Transfer from {src}"` convention payee.

### Delete semantics — independent halves / partner-aware

- *Independent*: deleting one half leaves the other orphaned. Rejected — defeats the link.
- **Partner-aware** (chosen): `Repository.delete_transactions` expands the id list to include any transfer partners before issuing the DELETE. The confirmation prompt names the partner count so the user isn't surprised by sibling rows vanishing.

### Edit semantics — independent / synced

- *Synced*: editing amount/date on one half writes the change to the other inside the same transaction. Correct in principle, requires a dispatch hook on every edit verb (inline cell edit, bulk edit, etc.) — more surface area than v1 needs.
- **Independent for v1, sync deferred** (chosen): each half is editable as a normal row. Inline category edits to a non-transfer kind on a transfer half don't auto-delete the partner — accepted rough edge. The natural fix is a dedicated edit-transfer dialog in a future revision.

### Import detection — out of scope for v1

OFX and bank-CSV imports don't tag transfers; detecting them means matching debit/credit pairs across accounts within a few days. Real but its own feature surface. v1 workaround: bulk-edit imported credit-card payments (etc.) to a transfer-kind category, one destination prompt, done.

## Decision

**Schema** (migration `0004_transfers.sql`):

- Add `txn.transfer_id TEXT` — nullable, no foreign key (the id space is conceptual, not relational).
- Add a partial index `idx_txn_transfer ON txn(transfer_id) WHERE transfer_id IS NOT NULL` — partner lookups are the only place this is hit and most rows won't have a transfer_id.

**Repository** (`mfl_desktop/db/repository.py`):

- `new_transfer_iri()` — `mfl:Transfer_<uuid8>` per ADR-006.
- `create_transfer(*, from_account_id, to_account_id, posted_date, amount, category_id, memo='', status='Pending')` — for the *fresh* transfer case (New Transaction with a transfer category). Inserts both halves with signed amounts (-amount on source, +amount on destination), generated payees, and a fresh shared `transfer_id`. Atomic.
- `convert_to_transfer(*, txn_id, other_account_id)` — for the *existing-row-becomes-source* case (inline category edit, bulk edit, single-row conversion). Validates the row isn't already a transfer + accounts differ; generates a shared `transfer_id`, sets it on the existing row, inserts a partner with the **opposite-sign amount** so the direction follows the source's amount sign naturally; the partner inherits date / category / status / memo from the source. Atomic.
- `bulk_convert_to_transfers(txn_ids, other_account_id)` — same as convert_to_transfer in a loop, all in one SQL transaction. Each pair gets its own `transfer_id`.
- `bulk_set_category_and_convert(txn_ids, *, category_id, other_account_id, payee_name=…, status=…, memo=…)` — combined bulk-update + transfer-convert for the bulk-edit dispatcher: updates category (so the new partner inherits the new one) and any other ticked fields, *then* converts each row, all in one SQL transaction. Atomic — all-or-nothing.
- `expand_transfer_partners(txn_ids)` — given a list of txn ids, return the same set plus the partner of any row whose `transfer_id` is not null.
- `delete_transactions(txn_ids)` — calls `expand_transfer_partners` before the DELETE so partner halves go with the user's selection.
- `list_categories_flat(kinds=('transfer',))` — narrows the category list when only transfer categories are needed (e.g. a future dedicated transfer picker; the current UX doesn't need it because the user picks from the full list and the dispatcher detects the kind).
- `get_default_transfer_category_id()` — convenience for any UI that wants to pre-select the seeded "Transfer" root.

**TransactionRow** gains `transfer_id: Optional[str]` so the register window can distinguish "could become a transfer" (transfer_id is None) from "already a transfer" (transfer_id is not None) when deciding whether to prompt.

**UI** (`mfl_desktop/ui/register_window.py`):

The user-facing verbs stay the same — **New Transaction (Ctrl+N)**, **inline cell edit**, **Bulk Edit (Ctrl+E)**. Each one's category-set path now branches:

- **New Transaction**: if the chosen category is `kind='transfer'`, after the dialog accepts, the window prompts for the destination account, infers source vs destination from the signed amount (negative ⇒ this account is source, positive ⇒ this account is destination), and calls `create_transfer`. Otherwise the existing `insert_transaction` path runs.
- **Inline cell edit on the category column**: the register window connects to the model's `dataChanged` signal. If the changed column is the category column AND the row's `transfer_id` is None AND the new category's kind is `transfer`, the window prompts for the destination and calls `convert_to_transfer`. If the user cancels the prompt, the row is left with a transfer-kind category and no partner — a recoverable state.
- **Bulk Edit**: if the dialog returns a transfer-kind `category_id` change, the window collects the source accounts of the selection, prompts once for the destination (excluding any source), and calls `bulk_set_category_and_convert` with the destination plus any other ticked fields. Otherwise the existing `bulk_update_transactions` path runs.

The shared `_prompt_destination_account(exclude_account_ids, title, message)` helper provides a `QInputDialog`-based account picker that excludes the source side(s).

**No dedicated "New Transfer" menu item or shortcut** — the category is the verb's modifier.

## Consequences

### Positive
- **One creation flow.** New, inline, and bulk all go through the same prompt → convert pattern. No second mental model for transfers.
- **Bulk-converting imported payments is one action**: select N imported credit-card payments → Ctrl+E → tick Category, pick "Credit Card Payment" → one destination prompt → done. Used to require N round-trips through a dedicated dialog.
- **Net Worth stays correct**: a transfer cancels itself out (+£X on dest, -£X on source, zero net), so the report needs no special handling.
- **Spending Over Time stays correct**: `kind='transfer'` rows are excluded by ADR-018's filter; no code change.
- **Deleting one half always removes both** via `expand_transfer_partners`. The data layer enforces the invariant.
- **Each half is a normal `txn` row** — fits the existing register, filter, sort, search, model, and proxy code unchanged.

### Negative / trade-offs
- **No edit sync in v1.** Changing the amount on one half without the other unbalances the pair. The fix is to delete and re-create. Documented; sync is a future revision (probably via a dedicated edit-transfer dialog rather than per-cell sync, with the inline cell edit redirecting to it).
- **No auto un-transfer.** If a user changes a transfer half's category to a non-transfer kind, the row keeps its `transfer_id` and the partner stays — the model drifts. Accepted rough edge; partner cleanup is manual. The UI could surface this by greying out the inline category combo for transfer halves; not in v1.
- **Cancellation leaves an orphan transfer-kind row.** If the user picks a transfer category and cancels the destination prompt, the row has a transfer category but no partner. The Spending Over Time chart still excludes it (kind filter), so the data-layer impact is minor; the user can re-edit to fix. Accepted as an acceptable v1 rough edge in exchange for not silently reverting a change the user explicitly made.
- **Import detection is manual.** Tracked as a future feature.

### Ongoing responsibilities
- The `transfer_id` column is the single source of truth for pairing. Any new write path (e.g. a future "split transaction" feature) must preserve `transfer_id` on each half — clobbering it would orphan the pair without surfacing the breakage.
- The bulk-edit dispatcher special-cases transfer-kind category changes. Any new field added to bulk edit (e.g. a future "tag" field) needs to thread through `bulk_set_category_and_convert` too so the transfer path stays in sync.
- When edit-sync ships, the natural shape is a dedicated edit-transfer dialog that loads both halves, lets the user change date/amount/category once, and writes the change to both rows in one transaction. Inline cell edits on a transfer half should redirect to that dialog or warn — keeps the rule that both halves agree.
- A future Cash Flow report can use `transfer_id` as the grouping primitive — display transfers as a separate "moved £X" row distinct from spending and income — without further schema change.
