# ADR-105 — Backlog usability round: faster manual entry, report focus, editable drill-downs

**Status:** Accepted
**Date:** 2026-06-25

## Context

Four small usability papercuts accumulated in `docs/backlog_notes.txt` from real
use. They're independent but all about friction in everyday flows, so they ship
as one round:

1. **No "Save and new" on the manual New Transaction dialog.** Entering several
   transactions by hand means re-opening the dialog from the menu/toolbar every
   time, re-picking the account.
2. **No payee autocomplete in the New Transaction dialog.** The inline register
   cell has a typeahead (`PayeeTypeaheadDelegate`, ADR-022) but the modal entry
   dialog made you type the full payee name with no completion — inconsistent
   with the cell and with Bulk Edit (which already completes payees).
3. **Spending Over Time report drops behind the register after editing the
   filter.** The report window is a non-modal `QMainWindow` parented to the
   register; opening its modal filter dialog and accepting it returned
   activation to the top-level parent (the register), burying the report the
   user was looking at.
4. **The report drill-down transaction view (`TransactionsListWindow`,
   ADR-034) can't be bulk-edited.** Inline edits work (it shares the register's
   model + delegates), but there was no context menu and no `Ctrl+E`, so the
   selection-based Bulk Edit verb the main register offers was simply absent.

## Decision

### 1. Save & New (transaction_dialog.py + register_window.py)

`NewTransactionDialog` gains a third button, **"Save && New"** (ActionRole),
beside Save and Split…. It validates and accepts exactly like Save but sets a
`save_and_new_requested()` flag. The register's new-transaction handler is
refactored: the per-dialog body moves into `_create_one_transaction(default_id)`
which returns the account id to reuse when Save & New was clicked, or `None` to
stop. `_on_new_transaction` loops on that, so a Save & New keeps the dialog
cycling on the same account; a plain Save, a cancel, an error, or the Split…
branch all stop the loop. Save & New is a no-op shortcut for the Split path
(Split has its own dialog) — it simply doesn't loop there.

### 2. Payee autocomplete (transaction_dialog.py)

The dialog takes an optional `payee_names` list and attaches a `QCompleter`
(`PopupCompletion`, `MatchContains`, case-insensitive) to the payee field —
the same configuration `PayeeTypeaheadDelegate` and `BulkEditDialog` use, so
all three surfaces behave identically. The register passes
`repo.list_payee_names()` (canonical names only, ADR-028).

### 3. Report stays in front (spending_report_window.py)

After the filter dialog returns, `_on_open_filter` calls
`self.raise_(); self.activateWindow()` **unconditionally** (whether or not the
filters changed) before applying any result, so the report the user is editing
stays foreground. Because `IncomeReportWindow` subclasses this window, the
income report inherits the fix. The other report windows
(Income & Expense, Payee, Sankey, Category & Payee, Investment Returns) get the
same one-liner after their filter dialogs for consistency — same window shape,
same latent bug.

### 4. Bulk edit in the drill-down (transactions_list_window.py)

`TransactionsListWindow` gains:

- Explicit edit triggers matching the register
  (`DoubleClicked | SelectedClicked | EditKeyPressed`) so inline editing is as
  responsive as the main grid.
- A reconciled-edit guard wired onto the model (same confirm dialog the
  register uses), so editing a reconciled row in the drill-down warns first.
- A right-click context menu and a `Ctrl+E` shortcut offering **Bulk Edit N
  Transactions…** when ≥2 rows are selected, reusing the existing
  `BulkEditDialog` and `Repository.bulk_update_transactions`.

**Scope limit — transfers and splits stay in the register.** The register's
`_on_bulk_edit` also converts selections to transfers and discards split lines,
machinery that is tightly coupled to the register (destination prompts,
`bulk_match_or_create_transfers`, split conversion). Rather than duplicate or
prematurely extract all of it, the drill-down's bulk edit covers the safe
field set — **payee / category / status / memo** — and **refuses a
transfer-kind category** with a message pointing the user to the main register.
This is the same conservative line ADR-034 drew for the drill-down generally:
match the register's everyday editing, defer the heavyweight transactional
verbs.

## Alternatives considered

- **Extract a shared "transaction table behaviours" mixin** (context menu +
  bulk edit + delete) used by both the register and the drill-down. Cleaner
  long-term, but the register handler carries transfer/split/security branches
  with deep `self._proxy`/`self._model`/`self._categories` coupling; extracting
  it safely is its own refactor round. Recorded here as the future direction if
  a third surface needs the same verbs.
- **Make Save & New reset the existing dialog in place** instead of
  re-opening. Re-opening reuses the register's full commit path (transfer,
  split, payee-default-category) unchanged, so a fresh dialog per record is
  simpler and less bug-prone than teaching the dialog to clear itself.
- **Fix the z-order by reparenting the report window to be top-level
  (no parent).** Rejected — parenting to the register is deliberate (closing
  the register closes its report windows); raising on filter-close is the
  targeted fix.

## Consequences

- Manual multi-entry is materially faster; the entry dialog now matches the
  inline cell and Bulk Edit for payee completion.
- The drill-down is a near-peer of the register for editing, minus transfers
  and splits — a documented, intentional gap, not an oversight.
- No schema change, no migration, no new dependency. View/dialog layer only.
