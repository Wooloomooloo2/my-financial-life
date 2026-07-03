# ADR-131 — Sidebar balance: Today vs Projected toggle

**Date:** 2026-07-03
**Status:** Implemented
**Related:** ADR-044 (`compute_account_values` — the sidebar/Net Worth value). ADR-069 (closed accounts). ADR-055 (folder roll-up FX). ADR-092 (app-level `QSettings` prefs). ADR-130 (`bank_posted_date` — a related date-vs-status distinction, though this is purely date-based). ADR-113/120 (the segmented-pill toggle pattern reused here).

## Context

Owner report: the left-panel account balances sum **every** transaction in the ledger, including **future-dated ("forwarded")** ones — a post-dated bill, or a transaction entered ahead of when it posts. So the headline balance is a *projection*, not the money actually in the account today. The owner wanted an option to see **today's** balance vs the **projected** balance.

`compute_account_values` (→ `compute_account_balances`) summed `opening_balance + Σ txn.amount` with no date bound; `balance_as_of(account_id, date)` already existed for a single account (used by reconciliation), so the "today" building block was there — just not exposed to the sidebar.

## Decision

Add a **Today | Projected** segmented toggle at the top of the sidebar (in column 1 of the **ACCOUNTS** header row), switching every account balance at once. Owner choices (2026-07-03):

- **Toggle, not both-at-once.** One figure at a time (a small pill), rather than showing today's-with-projected-secondary. Matches the app's existing pill toggles and keeps the balance column uncluttered.
- **Default = Today.** The sidebar defaults to the *actual* balance as of today (`posted_date <= today`), excluding future-dated rows — what the owner asked to see. The choice is remembered app-level in `QSettings` (`sidebar/balance_mode`).

Mechanics:
- `compute_account_balances` and `compute_account_values` gain an `as_of_date` parameter. When set, the cash sum filters `posted_date <= as_of_date`; for investment accounts the holdings set is likewise filtered to that date (future trades don't count either). Prices are always the latest on file — only the *transaction set* is dated, so "today's balance" means "today's cash + current holdings value".
- The `Sidebar` owns the mode (loaded via a `saved_balance_mode()` static helper so the register window can compute the initial balances before the widget exists), renders the pill, persists on change, and emits `balance_mode_changed`. The register window recomputes balances (`today → date.today()`, `projected → None`) and reloads on that signal, and threads the mode through both reload paths via a `_sidebar_balances()` helper.
- The toggle sits in the balance column; a `minimumSectionSize` bump guarantees the column is wide enough for the pill even when every balance is short (`ResizeToContents` ignores cell widgets).

Rejected: showing both figures always (busier, and the difference only matters when future-dated rows exist); a View-menu toggle (less discoverable than an inline pill); overwriting the ledger sum globally (the register's running-balance column stays a true ledger — ADR-044 — and Net Worth is unchanged for now).

## Consequences

- The sidebar defaults to the balance you actually have today; one click shows the projection including forwarded rows. Folder roll-ups and closed-account rows inherit the mode (the whole `balances` dict is computed with the chosen `as_of`).
- Repository, sidebar, and register-window are touched; no schema change, no migration — purely a date filter on existing sums plus a UI toggle.
- Net Worth and the register running balance are intentionally **not** changed (they answer different questions); a future ADR could extend the same as-of option to Net Worth if wanted.
- `tests/test_sidebar_balance_mode.py` 3/3 (today excludes a future row / projected includes it; `compute_account_values` honours `as_of`; the toggle defaults to Today, persists, emits, and is a no-op on the active mode) — the toggle test manages its own `QSettings` key so it neither depends on nor pollutes the machine's saved choice. Verified visually (pill fits with both wide and tiny balances, light + dark). Full suite 27/27.
