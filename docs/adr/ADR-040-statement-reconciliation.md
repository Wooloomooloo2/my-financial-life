# ADR-040 — Statement reconciliation: per-account flow, ending-balance match, statement persistence

**Date:** 2026-06-07
**Status:** Accepted
**Related:** ADR-010 (Transactional schema — `txn.status` already includes "Reconciled" as the terminal state; this ADR is what finally drives a txn into that state); ADR-033 (Per-account summary — the "NO STATEMENTS · RECONCILE ›" placeholder row already reserved the entry point); ADR-020 (Transfers — partner-aware behaviour applies to reconciliation: ticking one half of a transfer doesn't auto-tick the partner, since each side reconciles against its *own* account's statement on its own date).

---

## Context

The `Reconciled` status has lived on `txn` since ADR-010 but nothing in the app actually sets it. The owner runs a workflow analogous to Banktivity's: pick a closing statement date and ending balance, walk through the account's uncleared / cleared rows ticking off matches against the paper statement, watch the variance close to zero, then mark the matched set as `Reconciled`. That ritual is what gives a personal-finance app its *correctness anchor* — without it, an inadvertently-duplicated import or a typo'd amount drifts silently for months until the balance gets weirdly off.

ADR-033 reserved the entry-point placeholder ("NO STATEMENTS · RECONCILE ›") on the per-account summary screen specifically so this ADR could land without churning the layout. Today's per-account summary clicks the placeholder and gets an info-only "feature not yet wired" dialog; the swap is small.

This ADR locks the **single-round** shape of reconciliation. Unlike the saved-reports arc, reconciliation doesn't naturally subdivide — there's one core flow (open statement, tick rows, close), with a small follow-up surface (statement history). It ships as one coherent unit.

The decision matters now because the user is about to do a large amount of data entry (the 19-year Chase history, more US accounts coming). Reconciling early — even before all accounts are imported — gives the owner confidence that what's in MFL matches the bank's truth, and any silent drift (mis-tagged Withdrawals like the ones ADR-038 catches) gets surfaced at statement-close time.

---

## Options considered

### UI shape

- *Inline tick-column in the existing register* — add a "Reconciling" checkbox column that appears when a reconciliation is in progress; the user ticks rows in place and a sidebar shows the running variance. Pros: no context switch; the user reconciles in the surface they already know. Cons: the register's sort / filter / scroll state interferes with a sequential tick-down flow; the sidebar real estate is small; concurrent edits (categorise + tick) muddle the verb. The user's mental model is *walk the statement*, which is sequential and focused — the register's free-form interaction is the wrong shape.
- *Side panel docked to the register* — a non-modal panel on the right showing the statement state. Pros: register stays visible. Cons: layout cost (the register's columns get crammed); the panel has to handle its own sort / filter to not include already-ticked rows; the visual coupling between register row state and panel state is fiddly to keep in sync.
- **Modal dialog dedicated to one reconciliation pass** (chosen): opens from the per-account summary's RECONCILE row. Header: account + statement date + ending balance. Body: scrollable table of uncleared / cleared rows (`Reconciled` rows excluded by default — they're a prior statement's truth). Footer: cleared total, variance, status pill, action buttons. The dialog *is* the reconciliation surface; the register continues to operate normally underneath. Closing the dialog without committing returns the user to the summary.

This matches Banktivity's pattern (a dedicated Reconcile sheet) and produces a focused, sequential experience. The trade-off — the register isn't visible during reconcile — is acceptable; the dialog table shows everything the user needs to identify a row (date, payee, category, amount), and if they need to investigate a specific row they can dismiss the dialog and the partial state persists (see Resume below).

### Handling mismatches

- *Refuse to close on non-zero variance* — force the user to find every discrepancy before closing. Pros: pure correctness. Cons: real-world bank statements can be off by a cent due to rounding or by a small amount due to currency-conversion timing; making the user hunt that down before *any* closure is friction.
- *Silently close with whatever variance* — easy. Cons: the whole point of reconciliation is that variance == 0 at close. Silent acceptance defeats the value.
- **Allow close with explicit confirmation + an optional adjustment txn** (chosen): if the user clicks "Reconcile" with variance != 0, a confirm dialog explains the variance and offers two actions:
  - "Add adjustment" — creates a txn dated to the statement date, category = *Adjustment* (system category seeded by this migration), amount = the variance with whichever sign closes it, status = `Reconciled`. The statement row records the adjustment txn id. Future imports will dedup against this row via the standard composite-hash path (it's a manually-entered txn, no `fitid`, no import hash — so it can't collide with anything from imports anyway).
  - "Close with variance" — statement row stores the variance in a `closing_variance_pence` column. `status` becomes `reconciled_with_variance`. Surfaced on the per-account summary's statement-history list. Allows the user to come back later and add an adjustment if the source of the variance turns up.

The adjustment txn is the right shape for the most common cause (a fee or interest line the bank didn't itemise); the close-with-variance escape is the right shape for genuinely-unknown drift that doesn't deserve a phantom category yet.

### Partial / resumable

- *Always-full passes only* — closing the dialog without explicit Reconcile abandons every tick. Cons: a real reconciliation pass on a busy account can take 15-20 minutes; mid-pass interruption (phone call, child) shouldn't force a restart.
- **Open-statement resume** (chosen): the dialog has three save-states:
  - **Cancel** — closes the dialog, discards any unsaved ticks (no DB writes). Confirm dialog when there *are* unsaved ticks.
  - **Save & Close** — persists the current statement row with `status = 'open'`, stores the set of ticked txn ids via `statement_txn(statement_id, txn_id)` join rows. The summary screen surfaces the open statement on next visit; clicking it reopens the dialog with the ticks pre-loaded.
  - **Reconcile** — the full close: marks every ticked row's `status = 'Reconciled'`, sets `statement.status = 'reconciled'` (or `'reconciled_with_variance'` per the mismatch flow), inserts the adjustment txn if asked, and records `reconciled_at`.

Open statements stay editable. A user with a half-finished pass can come back to it next week. Only one open statement per account at a time — opening a second statement requires the first to be closed or cancelled. Surfaced as a small "Resume reconciliation from 2026-05-31" banner on the summary screen.

### Treatment of already-`Reconciled` rows post-import

- *Re-classify them like any other potential-match* — current ImportService behaviour. Cons: imports can silently move a Reconciled row's status back to Uncleared or change its amount, breaking the historical reconciliation.
- **Reconciled rows are import-immutable** (chosen): the import service's potential-match path skips rows where the existing row's `status = 'Reconciled'`. The composite-hash dedup still applies (duplicate row is dropped). A *manual* edit on a Reconciled row in the register is allowed but pops a "this row is reconciled to a statement dated 2025-12-31 — change anyway?" confirm. Amount edit on a Reconciled row gets the same confirm (the existing inline amount editor we just shipped is the surface).

This preserves the historical statement as ground-truth without making the system impossible to correct when a real error is found.

### Transfer-pair behaviour

- *Tick one half → auto-tick the partner* — convenient. Cons: the partner sits on a *different* account, often with a *different* statement date; ticking it against THIS statement is wrong if the user is reconciling Chase against the 2026-05-31 Chase statement and the HSBC partner row hasn't appeared on HSBC's 2026-06-15 statement yet.
- **Each half reconciles independently against its own account's statement** (chosen): the dialog only shows rows on the focus account. The partner row is visible in its own account's reconciliation. The pair is conceptually linked (via `transfer_id`); the reconciliation state is per-row.

### Cross-currency rows

The dialog displays amounts in the focus account's currency (which is `txn.amount` for that row — the stored truth). No conversion. The ending balance the user enters is in the focus account's currency. Variance computed in the same currency. This matches what the bank statement actually looks like; nothing to convert.

### Multi-statement history

The per-account summary screen's reserved RECONCILE row swaps to a small statement list once the first statement is closed. Each row: statement date, ending balance, status pill (Reconciled / Reconciled with variance / Open). Click to view a closed statement (read-only display of the rows that were ticked), or to resume an open one.

Read-only view of past statements: the dialog opens with the table pre-populated and ticks locked, header showing the ending balance, footer showing the cleared total and (any) variance. A "Reopen for editing" verb is available — it sets the statement back to `status='open'` and clears the `Reconciled` status on every linked txn (back to whatever they were before — see below for the prior-status problem).

### What to do about prior status on reopen

- *Track prior status per (statement, txn)* — `statement_txn` gets a `prior_status` column. On reopen, every txn reverts to its `prior_status`. Pros: lossless. Cons: a row that was `Cleared` at reconcile, *then* downgraded to `Uncleared` by some other code path while the statement was closed, would jump back to `Cleared` on reopen. Edge case but real.
- **Reopen reverts to `Cleared`** (chosen): on reopen, every linked txn's status is set to `Cleared` (not the literal prior status). This is correct for the vast majority of reconciled rows (they were Cleared before being reconciled); the rare case where a row was Pending or Uncleared at reconcile time is acceptable to lose — the user can re-tick. The simplicity of the model wins.

### Naming / verb cleanup

- "Reconcile" is the verb on the placeholder row + the dialog action button + the menu entry (**Account → Reconcile…**, Ctrl+Shift+R is taken by Manage → Reconcile Transfers, so use **Ctrl+Shift+B** for *Balance* / *Reconcile*… actually that clashes with **Ctrl+B** = Budget. Pick **Ctrl+Alt+R** — uncommon, mnemonic).
- The statement-history row shows the statement date as the label; opening it opens the dialog.

---

## Decision

### Schema (migration 0011_reconciliation.sql)

```sql
CREATE TABLE statement (
    id                     INTEGER PRIMARY KEY,
    iri                    TEXT NOT NULL UNIQUE,
    account_id             INTEGER NOT NULL
                           REFERENCES account(id) ON DELETE CASCADE,
    statement_date         TEXT NOT NULL,
    ending_balance_pence   INTEGER NOT NULL,
    status                 TEXT NOT NULL
                           CHECK (status IN (
                               'open',
                               'reconciled',
                               'reconciled_with_variance'
                           )),
    closing_variance_pence INTEGER NOT NULL DEFAULT 0,
    adjustment_txn_id      INTEGER REFERENCES txn(id) ON DELETE SET NULL,
    notes                  TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    reconciled_at          TEXT,
    UNIQUE(account_id, statement_date)
);

CREATE TABLE statement_txn (
    statement_id  INTEGER NOT NULL
                  REFERENCES statement(id) ON DELETE CASCADE,
    txn_id        INTEGER NOT NULL
                  REFERENCES txn(id) ON DELETE CASCADE,
    PRIMARY KEY (statement_id, txn_id)
);

ALTER TABLE txn ADD COLUMN statement_id INTEGER
    REFERENCES statement(id) ON DELETE SET NULL;

CREATE INDEX idx_txn_statement ON txn(statement_id);

-- Seed the Adjustment category if absent. kind='expense' is the safe default;
-- the user can change it to Other if they want a dedicated Adjustment kind
-- (we don't add a 'adjustment' kind to the enum here — too narrow).
INSERT OR IGNORE INTO category (parent_id, name, source, kind)
VALUES (NULL, 'Adjustment', 'system', 'expense');
```

`UNIQUE(account_id, statement_date)` enforces one statement per account per date — a user reconciling the same statement twice gets blocked at the schema layer.

`statement_txn` is a join table rather than relying on `txn.statement_id` alone because it cleanly supports the "open statement" state where ticks aren't yet committed to the txn rows. When `statement.status` transitions to `reconciled` / `reconciled_with_variance`, the close path stamps `txn.statement_id` AND `txn.status='Reconciled'` for every joined row in one transaction; the join table is the audit of which rows belonged to that statement.

### Repository surface

- `create_statement(*, account_id, statement_date, ending_balance) -> StatementRow` — creates with `status='open'`. Raises if an open statement already exists on the account.
- `get_open_statement(account_id) -> Optional[StatementRow]`
- `list_statements_for_account(account_id) -> list[StatementRow]` — ordered newest first.
- `get_statement(statement_id) -> Optional[StatementRow]`
- `list_reconcilable_txns(account_id, *, exclude_reconciled=True) -> list[TransactionRow]` — what the dialog shows; excludes rows already linked to a *different* closed statement; includes rows linked to the currently-open statement so resume works.
- `set_statement_ticks(statement_id, txn_ids) -> None` — replaces the `statement_txn` rows for an open statement. Idempotent; called on every dialog Save & Close.
- `close_statement(statement_id, *, adjustment: Optional[AdjustmentSpec], notes: Optional[str]) -> StatementRow` — atomic: writes the adjustment txn if any, sets `statement.status` / `statement.adjustment_txn_id` / `statement.closing_variance_pence` / `statement.reconciled_at`, stamps `txn.statement_id` + `txn.status='Reconciled'` for every ticked row. Raises if the statement is already closed.
- `reopen_statement(statement_id) -> StatementRow` — atomic: every linked txn's `status` resets to `'Cleared'`, `txn.statement_id` cleared, statement back to `'open'`, `adjustment_txn_id` set to NULL (and the adjustment txn deleted if it exists — the user can re-add on next close). `reconciled_at` cleared. `closing_variance_pence` reset to 0.
- `cancel_open_statement(statement_id) -> None` — deletes the statement + its `statement_txn` rows. No txn touched.

`AdjustmentSpec` is a frozen dataclass: `(amount_pence: int, category_id: int, payee_name: str, memo: str)`. The dialog builds it from the confirm-on-close UI.

### Dialog (`mfl_desktop/ui/reconcile_dialog.py`)

**Header**
- Account name + currency.
- Statement date (`QDateEdit`, defaults to last-statement-date + 30 days, or to today if no prior statement).
- Ending balance (`QLineEdit` with right-aligned formatting, currency-symbol-stripped on parse).
- "Notes" (single-line, optional).

**Body (scrollable table)**
- Columns: Tick / Date / Payee / Category / Amount.
- One row per reconcilable txn, sorted by date ascending (statement order).
- Tick column is the only editable column. Double-clicking a row toggles its tick.
- Already-reconciled rows are hidden by default (toggle via a small "Show closed-statement rows" checkbox in the footer; surfaced for the rare case where the user needs to spot a row they already reconciled to a *different* statement — those rows are read-only).
- Cross-currency note: amount displayed in the account's currency, no conversion.

**Footer**
- Cleared total (sum of ticked rows + the account's opening balance + any prior `Reconciled` rows' amounts).
- Variance (= ending balance - cleared total). Coloured: green if 0, amber if non-zero, red if > $100 absolute (heuristic visual cue).
- Status pill: `Open · X of Y ticked`.
- Buttons: `Cancel`, `Save & Close`, `Reconcile` (right-aligned; primary).

**Close flow**

1. User clicks `Reconcile`.
2. If variance == 0: confirm "Mark X transactions as reconciled?" → on yes, call `close_statement(adjustment=None)`. Done.
3. If variance != 0: dialog shows the variance + two verbs:
   - **Add adjustment** — opens a small form (category picker default=Adjustment, payee defaults to "Statement Adjustment", memo blank, amount = variance pre-filled). On submit, builds `AdjustmentSpec` and calls `close_statement(adjustment=spec)`.
   - **Close with variance** — `close_statement(adjustment=None)` with the `closing_variance_pence` set to the residual; statement gets `status='reconciled_with_variance'`.
   - **Back** — returns to the main dialog without closing.

**Save & Close** calls `set_statement_ticks(...)` and dismisses without changing statuses.

**Cancel** prompts if there are unsaved ticks; otherwise dismisses.

### Per-account summary integration

The summary screen's RECONCILE row updates its label based on the account's statement state:

- No statements: "NO STATEMENTS · RECONCILE ›" (unchanged from today's reserved placeholder).
- Open statement exists: "RESUME RECONCILE · Statement of YYYY-MM-DD ›".
- Closed statement(s) exist: "STATEMENTS · Last reconciled YYYY-MM-DD ›" + a small `(N)` count chip.

Clicking the row:

- No statements → opens the Reconcile dialog with a fresh statement.
- Open statement → resumes that statement (Reconcile dialog opens with ticks pre-loaded).
- Closed statement → opens a small statement-history list popup; rows clickable to view in read-only mode (which is the same dialog with the table locked + a "Reopen for editing" verb).

### Menu wiring

**Account → Reconcile…** (Ctrl+Alt+R) opens the dialog for the currently-selected account. Disabled in All-transactions mode (same gate as the per-account summary screen).

### Files touched

| File | Change |
|---|---|
| `mfl_desktop/migrations/0011_reconciliation.sql` | New — schema + Adjustment category seed |
| `mfl_desktop/db/repository.py` | StatementRow dataclass + the CRUD methods listed above |
| `mfl_desktop/ui/reconcile_dialog.py` | New — the dialog described above |
| `mfl_desktop/ui/account_summary_window.py` | RECONCILE row label & handler switch on statement state; statement-history popup |
| `mfl_desktop/ui/register_window.py` | Account → Reconcile menu entry + shortcut; the inline-edit confirm when touching a Reconciled row |
| `mfl_desktop/import_engine/import_service.py` | Skip Reconciled rows in the potential-match path; surface count in import-result message |
| `CLAUDE_CONTEXT.md` | Status line + reconciliation entry in the basic-management round + ADR table |

---

## Consequences

### Positive

- **Closes the correctness loop.** A reconciled account is provably-aligned to the bank statement at the time of closure; future drift is detectable.
- **Reconciled rows become import-stable.** A repeated import (Banktivity re-export, etc.) won't silently re-classify a row whose state has been verified against a statement.
- **The per-account summary screen finally honours its `RECONCILE ›` placeholder** — owners get the affordance the ADR-033 layout promised.
- **Resume support means real reconciliations work.** A multi-day pass through a 19-year backlog is realistic.

### Negative / trade-offs

- **Reopen-loses-prior-status.** A reopened statement reverts every txn to `Cleared`; rows that were `Pending` or `Uncleared` at reconcile time lose that distinction. Acceptable for v1; tracked as a follow-up if real use surfaces a case where it matters.
- **The Adjustment category is a system seed.** Renaming or deleting it via the Categories dialog breaks the close-with-adjustment default. The Repository's category-delete already cascades to Uncategorised for orphan txns; the seed gets the same protection as Income / Expense / Transfer / Uncategorised (delete blocked).
- **One open statement per account.** A user can't run two parallel reconciliations on the same account against two date windows. Real workflow doesn't need this; if it does, the `UNIQUE(account_id, statement_date)` constraint already allows multiple closed statements at different dates — only the *open* state is single.
- **Cross-currency accounts reconcile in their native currency.** An account whose statements come in from the bank in the account's currency works directly; an account where the statement comes in a different currency (rare, edge case for an FX-traded account) is out of scope here.
- **Statement-row history can grow long.** A 19-year-history user reconciling monthly accumulates 200+ statements per account; the history popup needs to paginate or scroll. Initial implementation: simple scrolling list; pagination if it ever feels slow.

### Ongoing responsibilities

- **The Reconciled-row inline-edit confirm must apply everywhere a row's amount, payee, category, or status can change.** That's the existing inline amount editor + the existing payee / category / status delegates + bulk edit. A single helper `Repository.is_reconciled(txn_id) -> bool` plus a window-level confirm wrapper that all the inline call sites use.
- **The Adjustment category is permanent.** It joins the system-seed list (Income, Expense, Transfer, Uncategorised) — its `source='system'` row protects it from deletion. Future migrations that re-shape the category table preserve it.
- **`txn.statement_id` is a soft pointer.** ON DELETE SET NULL means a deleted statement leaves orphaned `Reconciled` txn rows pointing at NULL. The reopen path handles this; a direct `DELETE FROM statement` (e.g. by a user manually editing the DB) wouldn't, and Reconciled-but-unlinked rows would still display as Reconciled until the next status edit. Acceptable; the Repository's `cancel_open_statement` is the proper verb.
- **The Sankey / Net Worth / Income & Expense reports (ADR-039) should treat `Reconciled` as a UI badge, not a filter dimension.** Reports filter on category / payee / account / date — never on reconciliation status. The reconciliation surface is the per-account summary; reports stay agnostic.

### Out of scope here

- **Auto-detect probable statement boundaries.** A future "open a recent OFX statement file → auto-pre-tick rows that match the OFX FITIDs" verb is on the multi-currency / import follow-up list. Not in this ADR.
- **Multi-account "household reconciliation"** — a single Reconcile flow against multiple accounts at once (e.g. for a joint account where both sides see the same statement). Out of scope; the per-account flow is enough.
- **Reconciliation reports** (e.g. "show me the variance history across my accounts"). Out of scope; can be added as a saved-report type later under ADR-039's planning umbrella.
- **Read-only sharing of a reconciled statement** (export to PDF / CSV) — out of scope; tracked as a polish item.
- **Bank-feed integration that auto-reconciles** — out of scope; MFL doesn't do live bank feeds.

---

## Amendment — 2026-06-07 (Banktivity-aligned UI + simplified close model)

**Status:** Accepted. Supersedes the conflicting parts of the original decision below; everything not contradicted here still stands. Migration 0011 has **not** shipped yet, so the schema is revised *in place* rather than via a follow-on ALTER.

The owner supplied the concrete Banktivity reconciliation UX (three reference screenshots: Statements history list, the dates/balances entry screen, and the check-off screen with a live "Missing" counter) plus answers to three design forks. This amendment locks the as-to-be-built shape.

### What changed and why

**1. A statement is a date *range* with both a starting and an ending balance.**
The original schema stored a single `statement_date` + `ending_balance_pence`. Banktivity's entry screen collects **Starting Date / Ending Date** and **Starting Balance / Ending Balance**, showing the **Change in balance** (= ending − starting) as a derived figure. The starting balance is **auto-populated from the previous reconciliation's ending balance** (`get_last_statement_ending`), and is editable for the first-ever statement. This makes the period self-describing and lets the history list show START/END per row.

**2. "Missing" replaces "Variance," and is the live correctness signal.**
On the check-off screen, **Missing = (ending − starting) − (net of ticked rows)**. (Algebraically identical to the old `ending − cleared_total`, but framed against the *change in balance* the way the screenshot presents it.) When Missing hits £0.00 the statement ties out. Displayed top-right with a green check at zero, amber/red otherwise. The check-off list also shows running **WITHDRAWALS / CHECKS / DEPOSITS** subtotals of the ticked set, and amounts render in **two columns (Withdrawal | Deposit)** rather than one signed column, matching the screenshot.

**3. Auto-select cleared transactions.**
The entry screen carries an **"Automatically select [Cleared Transactions]"** control. On entering the check-off page, all currently-`Cleared`, not-yet-reconciled rows are **pre-ticked** (`list_cleared_unreconciled_txns`). The user then adjusts. (The dropdown reserves room for future modes — "None", "All" — but round 1 ships only "Cleared Transactions" and "None".)

**4. The check-off list shows ALL unreconciled rows, any date** (fork answer #3). Old stragglers from before the statement window are still tickable; the date range drives the auto-select default and the header display, not a hard filter on the list.

**5. Single Save button; closing with a non-zero Missing is allowed and *flagged*, not blocked** (fork answers #1 + #2). This is the biggest simplification:
- **Save** is the only primary action. If Missing = £0.00 → the statement closes cleanly (`status='reconciled'`, every ticked row → `Reconciled`). If Missing ≠ £0.00 → it **still closes**, but `closing_variance_pence` records the residual and the history row is flagged (see below). No separate "Reconcile" button.
- **Add Transaction** is available *while reconciling* — it opens the existing New-Transaction dialog pinned to this account; the new row appears in the list (auto-ticked). This is how a user fixes a known-missing line (a bank fee, an un-entered cheque) without leaving the screen, and it **replaces ADR-040's auto "Add adjustment" mechanism entirely**.
- **Resume** is preserved without a second primary button: **Cancel** with unsaved ticks offers *Discard* vs *Save & finish later* (`status='open'`). Open statements surface as "in progress" on the history list. Only one open statement per account (unchanged).

  Consequently the **auto-adjustment txn is dropped**: the `adjustment_txn_id` column, the `AdjustmentSpec` dataclass, the close-time adjustment form, and the seeded system **Adjustment category are all removed** from the design. A user who wants an adjustment line just uses Add Transaction and categorises it themselves.

**6. "Out of balance" is a computed history state, not a stored status** (closes the owner's requirement: *"if the amount is changed on a reconciled transaction, the statement history will show it as out of balance"*). The `reconciled_with_variance` enum value is dropped; `status` is just `'open' | 'reconciled'`. The history list derives each closed statement's tie-out **live**: `residual = (ending − starting) − (current net of linked rows)`. If `residual == 0` → green "Reconciled"; if `residual != 0` → amber **"Out of balance · £X"** (this fires both for statements deliberately closed with a Missing amount *and* for ones that drifted later because a reconciled row's amount was edited or a linked row was deleted). Computing it live means every edit path is covered for free — inline amount edit, bulk edit, delete — with no need to hook each one. `closing_variance_pence` is retained only as the at-close snapshot.

**7. Three entry points, all opening the Statements history surface:**
- a visible **Reconcile** button on the register pane (new — the register had no toolbar; this introduces a small action button alongside it),
- the per-account summary's reserved **RECONCILE ›** row,
- **Account → Reconcile…** (Ctrl+Alt+R, unchanged).

  All three open the **Statements history window** for the current account (account-mode only; disabled in all-transactions mode). From there **"Make a new statement"** launches the two-page wizard; clicking a statement opens it (read-only when reconciled, with **Reopen for editing**); **Edit…** re-opens the dates/balances page; **Delete…** removes the statement and reverts its linked rows to `Cleared`.

### Revised schema (migration 0011_reconciliation.sql)

```sql
CREATE TABLE statement (
    id                     INTEGER PRIMARY KEY,
    iri                    TEXT NOT NULL UNIQUE,
    account_id             INTEGER NOT NULL
                           REFERENCES account(id) ON DELETE CASCADE,
    start_date             TEXT NOT NULL,
    end_date               TEXT NOT NULL,
    starting_balance_pence INTEGER NOT NULL,
    ending_balance_pence   INTEGER NOT NULL,
    status                 TEXT NOT NULL
                           CHECK (status IN ('open', 'reconciled')),
    closing_variance_pence INTEGER NOT NULL DEFAULT 0,  -- at-close snapshot of Missing
    notes                  TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    reconciled_at          TEXT,
    UNIQUE(account_id, end_date)
);

CREATE TABLE statement_txn (
    statement_id  INTEGER NOT NULL REFERENCES statement(id) ON DELETE CASCADE,
    txn_id        INTEGER NOT NULL REFERENCES txn(id)        ON DELETE CASCADE,
    PRIMARY KEY (statement_id, txn_id)
);

ALTER TABLE txn ADD COLUMN statement_id INTEGER
    REFERENCES statement(id) ON DELETE SET NULL;

CREATE INDEX idx_txn_statement ON txn(statement_id);
```

Dropped vs the original 0011 sketch: `statement_date` (→ `start_date` + `end_date`), the `reconciled_with_variance` status, `adjustment_txn_id`, and the `INSERT … Adjustment category` seed. Added: `start_date`, `starting_balance_pence`.

### Revised repository surface

Changed/added relative to the original list:
- `create_statement(*, account_id, start_date, end_date, starting_balance, ending_balance) -> StatementRow` — status `'open'`; raises if an open statement already exists.
- `get_last_statement_ending(account_id) -> Optional[int]` — most recent reconciled statement's ending balance, to auto-fill the next starting balance (falls back to the account's current recorded balance, else 0).
- `list_cleared_unreconciled_txns(account_id) -> list[int]` — txn ids to pre-tick for "Automatically select Cleared Transactions".
- `list_reconcilable_txns(account_id) -> list[TransactionRow]` — all not-yet-reconciled rows + any linked to the currently-open statement (so resume works); any date.
- `update_statement(statement_id, *, start_date, end_date, starting_balance, ending_balance) -> StatementRow` — the Edit… verb (open statements, or a reconciled one being corrected).
- `delete_statement(statement_id) -> None` — the Delete… verb; reverts every linked row to `Cleared`, removes the statement + its join rows.
- `statement_residual(statement_id) -> int` — `(ending − starting) − current_net_of_linked`; 0 ⇒ balanced. Drives the history out-of-balance display.
- `close_statement(statement_id, *, notes=None) -> StatementRow` — stamps ticked rows `Reconciled` + `statement_id`, sets `status='reconciled'`, stores `closing_variance_pence = statement_residual(...)` and `reconciled_at`. No adjustment parameter.
- `reopen_statement`, `get_open_statement`, `get_statement`, `list_statements_for_account`, `set_statement_ticks`, `cancel_open_statement`, `is_reconciled(txn_id)` — unchanged from the original ADR (minus the adjustment handling in reopen).

### Revised files-touched

| File | Change |
|---|---|
| `mfl_desktop/migrations/0011_reconciliation.sql` | New — revised schema above (no Adjustment seed) |
| `mfl_desktop/db/repository.py` | `StatementRow` dataclass + the methods above |
| `mfl_desktop/ui/reconcile_wizard.py` | New — two-page wizard: (1) dates + balances + auto-select, (2) check-off with Missing/subtotals/Withdrawal+Deposit columns/Add Transaction/Save |
| `mfl_desktop/ui/statements_window.py` | New — per-account Statements history (Make new / Edit… / Delete… / status icon / START+END / count); opens the wizard |
| `mfl_desktop/ui/register_window.py` | Reconcile button on the register pane + Account → Reconcile… (Ctrl+Alt+R) + the reconciled-row edit-warning helper |
| `mfl_desktop/ui/account_summary_window.py` | RECONCILE row opens the Statements window |
| `mfl_desktop/import_engine/import_service.py` | Skip `Reconciled` rows in the potential-match path |
| `CLAUDE_CONTEXT.md` | Status line + ADR-table note that 0011/ADR-040 shipped with this amendment |

### Carried-over decisions unchanged by this amendment

Transfer pairs reconcile independently per side; cross-currency accounts reconcile in native currency (no conversion); reconciled rows are import-immutable and editable only via a confirm; reopen reverts linked rows to `Cleared` (lossy on prior Pending/Uncleared, accepted); one open statement per account.
