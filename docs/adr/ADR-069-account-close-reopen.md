# ADR-069 — Account lifecycle: close / reopen (soft-close)

**Date:** 2026-06-14
**Status:** Accepted
**Implements:** the ADR-011 reserved `archived_at` soft-delete column (account half).
**Related:** ADR-015 (sidebar account folders + sections — the Closed group lives here). ADR-055 (Net Worth display-currency conversion — the "Show closed" toggle threads through unchanged). ADR-044 (`compute_account_values` market value). ADR-070 (the category half of the same lifecycle arc).

---

## Context

Arc C of the owner's 8-arc plan: account/category lifecycle. Accounts accumulate over a financial life — a savings account is emptied and closed, a credit card is cancelled, an old current account is superseded. Today the only way to remove one from the app is the **destructive** `delete_account`, which cascades away every transaction and all import history. That's wrong for a *closed* account: its history is real and should still count in the flow reports; the owner just doesn't want it cluttering the sidebar or distorting Net Worth.

The schema already reserved the answer: `account.archived_at TEXT` (ADR-011), and every list query (`list_accounts`, `compute_account_balances`, `list_folders`, `list_distinct_currencies`, …) already filters `WHERE archived_at IS NULL`. The column was never *set* by any code path. So this ADR is almost entirely UX + a verb to set it — no migration.

The owner settled the two product questions up front (via `AskUserQuestion`):

1. **One lifecycle concept, not two.** A single **Close** verb — not separate Close vs. Hide. A closed account leaves the active sidebar, drops out of Net Worth and account pickers, but its past transactions still flow into the transaction-driven reports.
2. **Excluded by default, with a "Show closed" toggle.** Closed balances are out of the Net Worth headline by default, but a checkbox re-includes them. The sidebar's collapsible "Closed accounts" group is the equivalent toggle there.

---

## Decision

### Repository (`db/repository.py`)
- `AccountSummary` gains `archived_at: Optional[str]` plus an `is_closed` property; `_ACCOUNT_COLS` / `_row_to_account` carry it.
- `list_accounts(include_closed=False)`, `list_investment_accounts(include_closed=False)`, `compute_account_balances(include_closed=False)`, `compute_account_values(include_closed=False)` — default behaviour is **unchanged** (open accounts only); passing `True` drops the `archived_at IS NULL` filter. Only two callers pass `True`: the sidebar build and Net Worth's toggle.
- New **`close_account(id)`** sets `archived_at = datetime('now')` (idempotent — a re-close keeps the original timestamp), and **`reopen_account(id)`** clears it. Both return the refreshed `AccountSummary`. `delete_account` stays as the destructive variant.

### Sidebar (`ui/sidebar.py`)
- The register window now feeds the sidebar `list_accounts(include_closed=True)` + `compute_account_values(include_closed=True)`; the sidebar **partitions** open vs. closed itself.
- Open accounts render exactly as before (folders + roots). Closed accounts are pulled out of the folder layout into one **collapsible "Closed accounts (N)" group** at the bottom of the ACCOUNTS section, **collapsed by default**, with muted (slate-400) leaves. Closed leaves stay **selectable** (you can open a closed account's register) and carry a `CLOSED_ROLE` marker so the context menu can offer the right verbs. `select_account_by_iri` descends into the group (and expands it) so a closed account can still be programmatically selected.

### Register window (`ui/register_window.py`)
- Account menu gains **Close Account…** (above Delete — the gentle, common verb). It's enabled only for a selected *open* account.
- Sidebar right-click on an **open** account: …Edit / Move / **Close Account…** / Delete. On a **closed** account: Summary / **Reopen Account** / Delete (Edit/Move/Close don't apply while archived).
- `_on_close_account` confirms ("moves to Closed accounts, excluded from Net Worth, history kept, reopen any time"), closes, and falls back to All-transactions. `_on_reopen_account(iri)` reopens and re-selects.

### Net Worth (`ui/net_worth_window.py`)
- A **"Show closed accounts"** checkbox in the top bar. `_refresh` threads its state into `list_accounts` + `compute_account_values`, so every total, donut, and column re-includes closed balances when ticked. Closed leaves get a " (closed)" suffix so a surfaced balance isn't mistaken for a live one. Verified on the live snapshot: headline £2,589,980 (excl.) ↔ £2,656,421 (incl.).

**No migration** — `archived_at` already exists (ADR-011) and the read queries already honour it.

---

## Options considered

- **Close-only (chosen)** vs. Close + a separate Hide. Two states double the surface to document and test for a marginal flexibility the owner didn't want. (Owner pick.)
- **Excluded-with-toggle (chosen)** vs. always-excluded. The toggle honours the arc's "report toggles" intent and the rare account that's closed with a residual balance; cost is one checkbox + a sidebar group. (Owner pick.)
- **Flow reports keep closed history automatically (chosen, by construction).** The spending / income & expense / Sankey / payee / category reports aggregate the `txn`/`txn_category_line` tables directly and never gate on `account.archived_at`, so a closed account's history flows in with no extra work — exactly the desired "close keeps history" semantics. Report *account-pickers* (which use `list_accounts`) simply stop offering the closed account; its history is still in the unfiltered totals.
- **Soft-close via `archived_at` (chosen)** vs. a new `account.status` enum. The reserved column + the already-present read filters made soft-close essentially free; an enum would have meant a migration and rewriting every query.
- **Collapsible "Closed accounts" group (chosen)** vs. a persisted "show closed" setting + a global toggle. The group *is* the toggle (collapsed = hidden), is self-documenting, needs no setting, and keeps closed accounts one click from reopen.

---

## Consequences

### Positive
- Non-destructive cleanup: the owner can tidy the sidebar and Net Worth without losing a closed account's history, and reopen is one click.
- Near-zero blast radius: default `list_accounts()` behaviour is byte-identical, so every existing caller (transaction/transfer pickers, budgets, all reports) is unchanged; only the sidebar + Net Worth opt into `include_closed`.
- No migration, no schema change.

### Negative / trade-offs
- Report account-pickers don't list closed accounts, so you can't *filter a report to* a single closed account (its history still appears in the unfiltered totals). If that bites, a per-report "include closed" toggle is additive.
- Closing doesn't auto-zero a non-empty account; a closed account with a residual balance simply disappears from Net Worth until "Show closed" is ticked (intended).
- A closed account is still openable in the register (by design — to view history), so Edit/Delete/Reconcile remain reachable on it via the Account menu; only Close is gated. Acceptable.

### Ongoing responsibilities
- Any new query that should respect closing must filter `archived_at IS NULL` (or take `include_closed`) — follow the existing pattern.
- The category half (ADR-070) mirrors this with `archive_category` / `unarchive_category` and a "Show archived" toggle.
