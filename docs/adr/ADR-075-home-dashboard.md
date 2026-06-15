# ADR-075 — Home / dashboard screen (Arc F)

**Date:** 2026-06-15
**Status:** Accepted
**Related:** the "App shell & navigation" backlog item (landing screen on launch). ADR-015/039/069 (sidebar sections + rows). ADR-055/067 (Net Worth FX + donuts). ADR-058 (budget matrix). ADR-044/046 (holdings, returns). ADR-063 (bills-due cue). ADR-033/034 (per-account summary, Top-N). ADR-066 (payee aggregates).

---

## Context

The app drops you straight into a register on launch. As MFL heads toward being shared with non-technical friends/family (ADR-008/050), the first screen should be an at-a-glance overview, not a ledger. The backlog flagged a landing screen with net worth, upcoming bills, recent activity, and quick links; the owner expanded the wishlist to a full dashboard.

The pieces already exist as reusable compute: `compute_account_values` + `convert_amount` (net worth, ADR-055), `compute_matrix` (budget, ADR-058), `payee_spending_aggregates` + `top_categories` (ADR-066/033), `compute_holdings_view` (ADR-044), `bills_due_summary`/`upcoming_scheduled` + `list_all_transactions` (ADR-063/033). This arc composes them onto one screen; it adds **no new aggregation semantics**, only an assembly + presentation layer.

Owner decisions (`AskUserQuestion`): **Home is the default landing view** on launch; and the cards are **net worth, accounts overview, budget summary (this month), upcoming bills, recent activity, top-5 payees, top-5 categories, and investment performance (top-5 gains / top-5 losses)**.

---

## Decision

### Shell — a stacked right pane + a "Home" sidebar row

The register window's right side becomes a `QStackedWidget` with two pages: the **Home** dashboard and the existing **register/report** panel (filter bar + table). A new **Home** row sits at the very top of the sidebar (above ACCOUNTS), emitting `selection_changed("home", None)`. Selecting Home shows the dashboard; selecting All-transactions / an account / a report shows the register page (the existing `_show_*` methods also flip the stack to the register page). On launch the window selects Home (replacing the previous "open the first account" default). It's a normal navigable view — no splash, no modality, no opt-out needed.

### Cards (round 1)

A vertical `QScrollArea` of cards. Period-scoped cards use the **current calendar month**, labelled "this month"; the investment card is a point-in-time **unrealized** snapshot. A card hides itself when it has nothing to show (no budget, no investments, no schedules), so a new/simple file isn't full of empty boxes.

1. **Net worth** — headline assets − debts in the display currency, FX-converted with ADR-055 rules (no-rate accounts excluded, never par-added; a note when any were). Click → Net Worth window.
2. **Accounts overview** — each account's current value grouped by family (cash / credit / investment / …), with a per-group subtotal. Click an account → its register.
3. **Budget summary (this month)** — the default budget's Expenses **planned vs spent** for the current month (the matrix's Expenses-section subtotal cell), with a small progress bar. Click → Budget window. Hidden when no budget exists.
4. **Upcoming bills** — the next few due/overdue scheduled transactions across all accounts (`upcoming_scheduled` over all schedules), colour-cued like ADR-063. Click → Manage ▸ Schedules.
5. **Recent activity** — the latest N transactions across all accounts. Click a row → that account's register.
6. **Top payees (this month)** — top 5 by strict-outflow spending via `payee_spending_aggregates` + `build_report`. Click → Payee report (or the payee's transactions).
7. **Top categories (this month)** — top 5 by strict outflow via `top_categories`. Click → that category's transactions / Spending report.
8. **Investment performance** — top 5 **unrealized gains** and top 5 **unrealized losses** by security, aggregating `compute_holdings_view` across all investment accounts (priced positions only). Hidden when there are no investment holdings.

### Data assembly — a Qt-free layer

A new `mfl_desktop/home_dashboard.py` exposes a `HomeData` dataclass and `gather_home_data(repo, today, *, recent_n, top_n)` that builds every card's data by calling the existing helpers (and the existing pure functions for budget/holdings). Keeping it Qt-free makes the whole dashboard offscreen-testable without widgets and keeps the FX/period logic out of the view. The view (`mfl_desktop/ui/home_view.py`) is presentation only: it renders `HomeData` into cards and emits navigation signals the register window wires to its existing handlers. Refresh on `WindowActivate` (like the budget/summary screens) so the dashboard reflects edits made elsewhere; best-effort (a closed connection on shutdown bails cleanly, per the ADR-057 pattern).

---

## Consequences

- The first thing a user (especially a shared-with friend) sees is a meaningful overview, not a ledger.
- No schema change, no migration, no new aggregation logic — every number is computed by an already-shipped, already-verified helper, so the dashboard can't disagree with the dedicated screens.
- The stacked-pane refactor is the only structural change to the register window; the register/report page is unchanged, just wrapped.
- Each card is independent and self-hiding, so the screen scales from an empty first-run file to a full multi-account/investment/budget setup.

### Scope / deferred

- **Period choice** is fixed to the current calendar month for the spending cards (labelled). A per-card period selector is a later polish.
- **Investment performance** is unrealized gain/loss (market value − cost basis) at today's prices — not a time-weighted period return (that's the Investment Returns report, ADR-046).
- **Customisation** (reorder/hide cards, drag to rearrange) is out of scope for round 1.
- **Quick-action buttons** (New Transaction / Import) weren't in the owner's final card list, so they're omitted; the menus/shortcuts already cover them.

### Rejected alternatives

- **A separate Home window** — a stacked pane keeps one window, reuses the sidebar for navigation, and stays non-modal.
- **Recomputing aggregates bespoke for the dashboard** — would risk drift from the dedicated screens; reusing the shipped helpers guarantees consistency.
- **Always opening the register** (Home opt-in only) — the owner chose Home as the default landing view.

---

## Verification

Offscreen: `gather_home_data` on a seeded multi-account file returns the right net-worth total (FX-excluded accounts noted), the current-month budget planned/spent, top-5 payees/categories for the month, the latest-N recent rows, upcoming/overdue bills, and per-security top gains/losses aggregated across investment accounts; empty-file and no-budget/no-investment paths degrade to hidden cards. Offscreen Qt: the Home view builds from `HomeData`, the sidebar exposes a Home row emitting `("home", None)`, the register window defaults to the Home page on launch and switches to the register page on account/report selection, and card click signals reach the existing navigation handlers.
