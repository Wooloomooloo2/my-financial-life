# ADR-115 — Reports include closed accounts by default

**Date:** 2026-06-27
**Status:** Accepted
**Related:** ADR-069 (account close/reopen — `archived_at`, `list_accounts(include_closed=)`, the sidebar "Closed accounts" group and Net Worth's "Show closed" toggle). ADR-084 (shared report filter-dialog base — the one `_make_accounts_panel`). ADR-064 / ADR-018 / ADR-088 / ADR-056 (the report windows touched).

## Context

A closed (archived) account is kept for history but drops out of
`Repository.list_accounts()` by default (ADR-069). Every analysis report
seeded its account list from that default — Spending / Income Over Time,
Income & Expense, Payee, Category·Payee, Sankey, Investment Income, Investment
Returns — and then aggregated over `filters.account_ids or [every loaded
account]`. So a closed account's transactions were **silently excluded from
every report**, even with no account filter set. That breaks long-term trend
analysis: close last year's current account and its years of history vanish
from the very charts meant to show the long run.

## Decision

Reports **include closed accounts by default**, and the account filter can
uncheck them. Each report window now seeds its account list with
`include_closed=True`:

- cash/flow reports — `list_accounts(include_closed=True)` (Spending, Income,
  Income & Expense, Payee, Category·Payee, Sankey);
- investment reports — `list_investment_accounts(include_closed=True)`
  (Investment Income, Investment Returns).

Because the "all accounts" default is *empty* `account_ids` → "every loaded
account", and the loaded set now includes closed accounts, both the aggregation
and the filter checklist pick them up. The saved-filter convention is unchanged
(empty == all; an explicit subset is respected), so existing saved reports with
no account filter automatically gain their closed-account history, and any
report where the user picked specific accounts is untouched.

In the shared filter dialog (`ReportFilterDialogBase._make_accounts_panel`,
used by all six dialogs) a closed account's row is tagged **"(closed)"** so it's
identifiable when the user wants to uncheck it. Unchecking it falls out through
the existing `_checked_or_all` path as an explicit account subset.

This is the report-analysis counterpart to the operational rule: account
pickers that *write* (new transaction, transfer destination, schedules, budget
setup, loans, bank feeds) deliberately keep excluding closed accounts — you
don't post to a closed account. Net Worth keeps its own point-in-time "Show
closed" toggle (ADR-069); this ADR is about flow/trend reports.

## Consequences

- Long-running trends are whole again: a closed account's years of history show
  up in the reports by default, and a drill into a period that only a closed
  account covered resolves its name correctly (the Sankey/per-account name
  lookups also widened to `include_closed=True`).
- The default is more inclusive, so a report can now show more than before for
  files with closed accounts; the user trims via the filter (the "(closed)" tag
  makes that one click). View layer only; no migration, no schema change.
- Not touched: operational write-path account pickers, and Net Worth's
  point-in-time toggle.
