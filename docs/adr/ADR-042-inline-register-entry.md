# ADR-042 — Inline register entry (quick-entry bar)

**Date:** 2026-06-08
**Status:** Parked (built + trialled 2026-06-08, reverted on owner feedback)
**Related:** ADR-017 (bulk edit — the New Transaction modal this complements), ADR-020 (category-driven transfers — the inline entry reuses the destination prompt for transfer-kind categories), ADR-022 (register typeahead delegates — the bar reuses the same payee completer + category picker), ADR-038 (Banktivity sign-authoritative amounts — the bar uses a single signed amount field), ADR-041 (register date-window — the bar deliberately stays *outside* the model/proxy so windowing is untouched).

---

## Parked — outcome (2026-06-08)

The quick-entry bar was built end-to-end as described below, trialled against real data, and **reverted the same day on owner feedback**. The implementation (`mfl_desktop/ui/quick_entry_bar.py` + the `RegisterWindow` wiring + the `_commit_new_transaction` extraction) was removed; the register is back to modal/Ctrl+N entry. This ADR is kept as a record so a future attempt doesn't repeat the same shape.

**What didn't work (owner's words):** *"a fixed line at the bottom looks out of place, it's there permanently, and it doesn't feel quicker to enter on it."* Three distinct problems:

1. **A permanently-docked footer strip looks out of place** — it reads as chrome, not as part of the ledger, and it isn't aligned to the columns it's meant to mirror (the decoupled-widget choice in §options bought isolation from the model/proxy at the cost of exactly this — the bar can't sit *in* the grid).
2. **Always-on is the wrong default** — the entry affordance shouldn't consume permanent vertical space; it should appear on demand and get out of the way.
3. **It didn't feel faster** — the core promise. Tabbing across a detached strip is not meaningfully quicker than the modal, and the signed-amount field + pick-existing-category constraints removed some of the speed the idea was supposed to add.

**What a future attempt should explore instead** (not decided here — for the next planning round): a true **in-grid append/edit row** so entry happens *in the ledger* and aligns with the columns (Option A below, whose model/proxy-integration cost was the reason it was rejected here — that cost may simply be worth paying); or an **on-demand** inline row (appears on a keypress / "＋" click at the top of the list, commits, and disappears) rather than a permanent footer; and re-examining whether the modal is actually the bottleneck or whether it just needs to open faster / pre-focused. The rest of this ADR is the original (now-superseded) decision, retained verbatim.

---

## Context

Every manual transaction today goes through the `NewTransactionDialog` modal (Transaction ▸ New Transaction… / Ctrl+N). That's a good full-form surface — account picker, direction radios, cross-currency-aware — but it's heavy for the common case: the owner sitting in front of one account, typing a quick run of entries. A modal per row means open → fill → Save → reopen, with the cursor never staying put. The owner asked (backlog 2026-06-08, two paired items) for:

1. **An inline add row** — enter transactions directly in the register, with **Tab advancing field-to-field across the row**, instead of always going through the modal.
2. **New-entry save verbs** — Cancel / Save / **Save and add another** (the default), where "Save and add another" lands the cursor on a fresh entry line ready to type.

The backlog flagged three open questions for this ADR: *where the new row sits*, *how Tab order maps to columns*, and *how a transfer-category selection mid-row is handled vs. the modal's destination prompt*.

---

## Options considered — where the new row sits

### A — In-grid draft row inside the table model

Append one "draft" row to `TransactionTableModel` (a phantom at `rowCount`), edited in place with the existing delegates. This is the truest spreadsheet feel and gets perfect column alignment + horizontal-scroll behaviour for free, because the draft rides inside the table's own viewport.

Rejected for v1. The draft has to stay correct through every consumer of the model and proxy, and those are the most intricate, most-recently-churned parts of the app:

- **Sorting** — the draft must stay visually last regardless of the active sort column/direction. `QSortFilterProxyModel` has no "pin a row"; you'd special-case `lessThan` *and* invert on `sortOrder()`.
- **Filtering** — `filterAcceptsRow` must always accept the draft (else searching/status/category filters hide the row you're typing into), and the **ADR-041 date window** would have to exempt it too.
- **Selection / delete / bulk-edit / status-bar net** — every one of these maps proxy rows → source rows → reads a real `TransactionRow.id`; the draft has no id, so each path needs a skip-the-draft guard.

That's draft-awareness smeared across `register_model.py`, `filter_proxy.py`, and a dozen call sites in `register_window.py`, each a sharp edge, landing one day after ADR-041 reworked exactly this code. High regression risk for a convenience feature.

### B — Column-aligned strip with horizontal-scroll sync

A separate one-row widget pinned below the table, with each editor's width slaved to the matching `QHeaderView` section and its x-offset slaved to the table's horizontal scrollbar, so it tracks the columns pixel-for-pixel.

Rejected for v1. The real register is wider than the viewport on the owner's window (sidebar 360 + columns ~1160 > ~960), so it genuinely scrolls horizontally; keeping a separate widget aligned through section-resize **and** scroll is fragile syncing for a marginal gain. Worth revisiting if "it must look like a real grid row" becomes a stated need.

### C — Decoupled quick-entry bar (chosen)

A self-contained `QuickEntryBar` widget docked below the register table, holding a row of **real native widgets** in column order (Date, Payee, Category, Status, Memo, Amount) plus the three verbs. It is **not part of the model or proxy** — it writes through the Repository and triggers the existing `model.reload()`, exactly like the modal does.

**Selected.** Rationale:

- **Zero coupling to the model/proxy.** Sorting, the ADR-041 window, filtering, selection, the net-of-rows status line, and the transfer/reconcile read paths are untouched — no regression surface.
- **Tab is native.** Real focusable widgets tab in order with no edit-trigger or `currentChanged→edit()` plumbing.
- **It reuses the modal's vocabulary.** The bar produces the same `NewTransactionValues` object the dialog does, so a single shared commit path (below) guarantees identical behaviour, including transfers.

The cost is that the bar echoes column *order* but is not pixel-aligned to the scrollable columns. It's presented as a deliberate, visually-distinct entry strip (top rule, leading "＋" affordance) so it reads as an entry area rather than a not-quite-aligned ghost row. The in-grid draft (Option A) remains the richer end state if the owner wants true in-cell entry later; this ADR records why it isn't the v1.

---

## Decision

Add a docked **quick-entry bar** to the register, visible in **single-account view only**.

**Why single-account only.** The bar entry targets "this account" — there's no account field. The all-transactions view has no single target account and mixes currencies, so it keeps the existing modal (which has the account picker). The bar is hidden in that view; Ctrl+N / the menu still open the modal everywhere.

**Fields & Tab order.** Date → Payee → Category → Status → Memo → Amount → **Save and add another**. Defaults: Date = today, Category = Uncategorised (id 1), Status = Pending, Payee/Memo blank, Amount blank.

- **Payee** — `QLineEdit` with the same contains-match completer over `list_payee_names()` as the inline delegate (ADR-022). Unknown names create a payee silently via `get_or_create_payee`, matching the delegate and the modal.
- **Category** — the shared `make_category_picker` searchable combo (ADR-031 full-path labels). v1 requires picking an **existing** category; inline-create-from-the-bar is a deliberate follow-up (the register's category *cell* still offers the ADR-022 confirm-and-create).
- **Amount** — a single **signed** field (negative = money out), parsed by the register's existing `_parse_amount_input` (strips £/$/€ and commas). This replaces the modal's direction-radios-plus-positive-amount: radios break a linear Tab flow, and ADR-038 already makes the sign authoritative everywhere else. A leading currency label shows the account's symbol as a cue.

**Verbs.**

- **Save and add another** (default; Enter from any text field) — validate, commit, reset the fields to defaults, keep the bar focused, land the cursor on **Payee** (Date is pre-filled to today, the usual case).
- **Save** — validate, commit, reset, return focus to the register grid (the just-added row is visible after reload).
- **Cancel** — clear the fields back to defaults without committing; return focus to the grid.

**Validation** is non-blocking: a missing/zero/unparseable amount or a blank-typed category shows a status-bar message and moves focus to the offending field, leaving the other fields intact so the user fixes one thing rather than re-typing the row. (The modal's blocking `QMessageBox` validation is fine for a one-shot dialog; it's wrong for a rapid-entry bar.)

**Transfer-kind category mid-row.** Resolved by sharing one commit path. `RegisterWindow._on_new_transaction` is refactored: everything after it has the validated `NewTransactionValues` moves into **`_commit_new_transaction(values) -> bool`**. Both the modal and the bar call it. So when the chosen category's kind is `transfer`, the bar pops the **same** `TransferDestinationDialog` and calls `create_transfer` (cross-currency partner-amount included, per the ADR-035 amendment) — identical to "New Transaction". The bar uses the modal/create path, **not** the import matcher (ADR-036): the matcher is for reconciling an *existing* imported row, whereas this is a brand-new entry, exactly like the modal.

**Date window interaction (ADR-041).** The bar's lower-bound-only window means a back-dated entry (date earlier than the active "Show" window's `since`) commits fine but won't appear in the current view. After such a commit the bar surfaces a one-line hint to switch the window to see it. Today-dated entries (the common case) are always inside the window.

**No schema change, no migration.** The bar reuses `insert_transaction`, `get_or_create_payee`, `create_transfer`, and `commit` — the same calls the modal already makes.

---

## Consequences

### Positive

- **Rapid manual entry is one row, no modal.** Type across, Enter, the cursor is back on Payee for the next one. The owner's stated workflow.
- **The "visible New Transaction" discoverability gap (basic-management backlog) is closed as a side effect** — the entry surface is now always on screen in single-account view, not hidden behind a menu/shortcut.
- **One commit path for new transactions.** Modal and bar share `_commit_new_transaction`, so transfer handling, cross-currency partner amounts, error reporting, and the reload/sidebar refresh can never drift between the two surfaces.
- **No regression surface on the model/proxy.** ADR-041 windowing, sort, filter, selection, and the net-of-rows line are untouched because the bar lives outside them.

### Negative / trade-offs

- **Not pixel-aligned to the columns.** The bar echoes column order but doesn't track horizontal scroll (Options A/B). Accepted, and visually framed as a distinct strip so it doesn't read as broken alignment.
- **Single-account only.** All-transactions entry still uses the modal. Reasonable — that view has no single target account — but it's an asymmetry to remember.
- **No inline category-create in the bar (v1).** Typing a brand-new category name in the bar's picker is a "pick again", not a create. The register category *cell* still creates inline (ADR-022); unifying the two is a follow-up.
- **Signed-amount field has a tiny learning curve** vs. explicit direction radios. Mitigated by the currency cue + placeholder; consistent with the rest of the app post-ADR-038.

### Ongoing responsibilities

- **Any new-transaction entry surface must route through `_commit_new_transaction`** so transfer detection and the cross-currency path stay uniform. Bypassing it (a direct `insert_transaction`) silently loses the transfer-category branch.
- **The bar's category list must be refreshed** when categories change (merge/delete/inline-create elsewhere) via its `set_categories` setter, called from the window's existing category-refresh hooks. The payee completer self-refreshes on each `reset()`.

### Follow-ups (not in this ADR)

- Inline category-create inside the bar (mirror the ADR-022 confirm-and-create).
- Optional in-grid draft row (Option A) if true in-cell entry is wanted.
- A bar in the all-transactions view with an account field.
- Memo-history completer on the bar's Memo field (symmetric with the backlog's register memo-typeahead item).
- Keyboard shortcut to focus the bar (e.g. Ctrl+Shift+N); v1 relies on it being always visible.
