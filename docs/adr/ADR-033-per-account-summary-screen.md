# ADR-033 — Per-account summary screen

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-018 (Reports framework + Spending Over Time chart — sets the strict-outflow rule and the paintEvent precedent), ADR-019 (Net Worth report — three-column screen layout the owner already lives with; reuse of `ProportionalBar`), ADR-023 (Scheduled transactions — provides the data feed for the Upcoming block), ADR-026 (Visual baseline + paintEvent charts — shared chart kit), ADR-030 (Rollup levels — strict-outflow semantics for the top-N breakdowns)

---

## Context

Every other screen the owner has built (the register, the budget window, Net Worth, Spending Over Time) starts from "what's happening across everything?" The thing missing is the other-axis view — *focus on one account*: how's its balance trending, what's flowing in and out, who am I paying, what categories does that fall into, and what's coming up. The legacy v0.1 web app had a similar landing per account; the owner flagged a "good-looking" per-account summary on the same turn as ADR-032 (Vehicle account type) and pointed at a Banktivity screen for inspiration. Backlog item is the "Per-account summary screen" line under "Account workflows".

Design questions:

1. **What chart goes at the top?** The original backlog wording was "historical balance chart (line, with optional cleared-vs-running variants)". The owner walked that back in this round: they want **bars above and below the zero baseline for income and spending plus a line tracking the balance**, on the same widget. Owner's framing: "would be good for current accounts and also quite a nice view for a credit card to see the balance coming down if you're trying to pay off a debt." This is a combo chart, not a pure line, and not the monthly-net bars the screenshot literally shows either.
2. **Balance series semantics — cleared vs. running vs. both?** The original backlog noted this. The combo-chart variant settles it implicitly: one balance line, using the signed running balance (opening + cumulative `txn.amount`). Cleared-only is a different question and is left for a future iteration.
3. **Non-cash families.** Investment, property, and vehicle accounts compute "balance" the same way today — opening + sum of txns — but the real number for those families lives in the `valuation` table (ADR-010), which isn't wired yet (also flagged on ADR-032). The summary screen has to render something that doesn't lie.
4. **Sparse data.** A two-week-old account with three txns shouldn't render a noisy multi-year chart.
5. **Entry points + window lifecycle.** Backlog says "Opens from the sidebar context menu or double-click on an account row, in a new non-modal `QMainWindow` — single-instance per account so opening twice raises the existing window." Settle the keying (account.iri vs account.id) and the menu path.
6. **The screenshot has the RECONCILE row circled in red.** Statement reconciliation is its own backlog item with its own ADR ahead of it. The summary screen has to decide whether to surface that entry point now (placeholder) or wait.

## Options considered

### Chart shape

- **Line of daily running balance** — the original backlog phrasing. Cleanest single-purpose chart; loses the income/spending context the owner now wants in the same widget.
  - Rejected. Doesn't deliver the "how am I paying down this credit card" story the owner specifically asked for.
- **Bars of monthly net change (Banktivity-screenshot-literal)** — one bar per month, height = net cash flow that month. Visually closest to the screenshot.
  - Rejected. Conflates inflows and outflows into a single number — answers "did I net-save this month" but not "how much did I bring in vs spend." Owner's framing explicitly wants both directions visible.
- **Bars of end-of-month closing balance** — bars whose heights are balance snapshots. Possibly what Banktivity is drawing.
  - Rejected. Bars-of-balance is an unusual choice and loses the flow story.
- **Combo: income bars above zero + spending bars below zero + balance line overlay (chosen)** — one bar pair per bucket (income above the zero baseline, spending below), with a polyline tracking the signed running balance over the same buckets. Matches the owner's exact wording. Reads as cash-flow at the bucket scale + balance trajectory simultaneously.

### Balance-line scale vs. bar scale

The chosen combo plots two value families (cash flow and balance) on a single y-axis. They may differ by an order of magnitude (a cash account with a £5,000 balance and £1,000 monthly flow; a credit card at -£200 balance with £150 monthly flow).

- **Single y-axis, both series share it (chosen for v1)** — picks `nice_ticks` from the combined range. Simple, reads correctly when the two scales are comparable (the common case in the owner's data).
- **Dual y-axes** (left for bars, right for the line) — solves the scale mismatch. Adds visual complexity, and `paintEvent` is happy to do it but the legend / hover code roughly doubles.
  - Deferred. If real use surfaces a chart where the bars look stunted because the line dominates the axis (or vice versa), this is the right v2 move; until then, single-axis keeps the widget simple and the code reviewable. Documented as the obvious follow-up.

### Period selector

- **YTD / Current toggle only** — what the Banktivity screenshot shows. Cheap; doesn't satisfy the backlog's "30 days / quarter / year / YTD / custom" list.
  - Rejected. Owner asked for the richer range in the backlog.
- **Six fixed presets (chosen): Last 30 days / Last 90 days / YTD / Last 12 months / Last 5 years / All time.** Each preset implies a granularity (see next). No custom range picker in v1 — covers the 95% case; custom range is a small follow-up if it surfaces.

### Bucket granularity

Auto-picked from the selected period so the chart never shows more than ~60 buckets or fewer than ~5:

| Period span | Granularity | Typical buckets |
|---|---|---|
| ≤ 45 days | daily | up to 45 |
| ≤ 120 days | weekly | up to ~17 |
| ≤ 800 days (~26 mo) | monthly | up to ~26 |
| ≤ 2200 days (~6 y) | quarterly | up to ~24 |
| > 2200 days | yearly | adaptive |

The thresholds are deliberately loose — they only need to keep bar widths sensible at the window's default size.

### Balance semantics for non-cash families

- **Hide the chart entirely for investment / property / vehicle accounts** — safest but punitive: the screen becomes mostly empty for users tracking those accounts.
  - Rejected. The chart still says something useful (the balance line shows how recorded txns moved the asset's basis even before valuations land).
- **Render the chart with a banner note (chosen)** — for investment / property / vehicle accounts, the balance line is the same `opening + cumulative txn.amount` formula, and the screen carries a one-line "Balance reflects recorded transactions; valuations not yet wired." note above the chart. When the valuation pipeline lands (its own ADR), the note disappears and the line becomes the mark-to-market series. The note is a deliberate honesty signal, not error-handling.

### Sparse data

- Below ~30 days of recorded history, the chart still renders but a `Not enough history yet` note sits below it. Buckets show whatever's there; the line starts at the account's opening balance. No special-casing — the chart engine handles a single bucket fine.

### Top-10 panels

- **Strict outflow** for both Top Payees and Top Categories (matches ADR-018/030's spending semantics). Inflows are not displayed in v1 — the chart already surfaces income proportionally, and Banktivity's screen doesn't list top inflow sources either.
- **Inflow toggle** considered, deferred. If the owner wants "top payers" on a salary account, the same widgets render that data with a sign flip — small additive change.
- **Transfers** are included in the totals. The screen is a single-account picture, so a transfer leaving the account is real outflow for *this* account regardless of where it lands. (This is different from the budget perimeter rule in ADR-024, which is correct in its own context — the budget is about a *set* of accounts, the summary is about *one*.)

### Reconcile entry point

- **Drop it for now** — clean v1, no placeholder buttons. Re-add when the reconciliation arc lands.
  - Rejected. The owner circled this row in red on the inspiration screenshot, and the explicit ask was to leave the entry point in the layout so the screen doesn't need re-layout when reconciliation arrives.
- **Placeholder button (chosen)** — render the "NO STATEMENTS · RECONCILE ›" row in the right column. Click → info dialog: "Statement reconciliation is coming soon — tracked in the backlog." When the reconciliation flow lands (its own ADR), the handler is swapped to open the reconcile dialog; the layout doesn't move.
- **Pre-build the collapsible Statements section** — speculative scaffolding for an un-shipped feature.
  - Rejected. Too much guessing about the future shape of reconciliation.

### Entry points + lifecycle

- **Double-click on an account row in the sidebar** + **right-click → Account Summary…** in the sidebar context menu + **Account → Summary…** in the menu bar (active for whichever account is currently selected). All three open the same window; opening an account that already has a summary window raises and focuses it.
- **Single-instance keying — by `account.id`.** IRIs are also unique but `account.id` is the local integer PK and is already the key for `_account_summary_wins`-style dicts in the existing windows.

### Module shape

Mirror the budget arc's split: a pure-Python aggregation module + paintEvent chart widget + thin window.

- `mfl_desktop/account_summary.py` — pure functions over the `TransactionRow` / `ScheduledTxnRow` records the Repository already returns. No Qt imports. Mirrors `mfl_desktop/budget_calc.py` in shape, so it's familiar.
- `mfl_desktop/ui/balance_flow_chart.py` — combo paintEvent widget. Same conventions as the Spending and Burn-down charts (chart_helpers, white background, soft gridlines, rounded bar tops, hover tooltip).
- `mfl_desktop/ui/account_summary_window.py` — the `QMainWindow`. Wires the period selector → aggregations → widgets and refreshes on `WindowActivate`.

## Decision

### 1. New module — `mfl_desktop/account_summary.py`

Pure-Python aggregations, no Qt imports.

- `pick_granularity(period_days: int) -> str` — table above.
- `bucket_label(date_str: str, granularity: str) -> str` — human label per bucket.
- `compute_balance_flow_series(...) -> BalanceFlowSeries` — returns ordered `BalanceFlowBucket` list with `income`, `spending`, `closing_balance` per bucket. Closing balance is the signed running balance at the END of the bucket; the chart uses an extra leftmost point (period-start balance) so the line starts at the right place.
- `compute_period_summary(...) -> PeriodSummary` — opening / inflows / outflows / closing for the whole period.
- `compute_status_breakdown(...) -> StatusBreakdown` — recorded balance, cleared balance, uncleared count and amount. `Pending` and `Uncleared` are grouped together as "uncleared" (matches the user-facing language); `Cleared` and `Reconciled` count as cleared.
- `top_payees(period_txns, n=10) -> list[TopNRow]` — strict outflow, grouped by payee_name. Empty payee renders as `(No payee)`.
- `top_categories(period_txns, n=10) -> list[TopNRow]` — strict outflow, grouped by category_name. Uses the category's name as it appears on the txn row (round 1 of ADR-029 deliberately leaves register display un-rolled; round 2 will canonicalise at import time and downstream rollups inherit the fix).
- `upcoming_scheduled(schedules, account_id, today, horizon_days=30, n=5) -> list[UpcomingScheduled]` — filters schedules where `account_id` matches OR the schedule transfers TO this account, sorts by next_due_date, returns the next N within `horizon_days`.

### 2. New widget — `mfl_desktop/ui/balance_flow_chart.py`

Hand-rolled paintEvent widget per ADR-026, sharing `chart_helpers`.

- Bars centred in each bucket slot: income above the zero baseline, spending below. Bars share the same x-axis but different colours: emerald-500 for income, red-500 for spending.
- The zero baseline is drawn explicitly (1px slate-400) because the bars cross it.
- Balance polyline drawn last (over the bars), in blue-600 (the app accent). One point per bucket *plus* a leading point at period-start (so the line begins at the opening balance and walks through every bucket end).
- Single y-axis. `nice_ticks` over the combined range (max income, max spending taken as negative, max/min balance). Labels are `fmt_currency`.
- X-axis labels are bucket labels at evenly-spaced indices (same heuristic as the burn-down chart — about six labels).
- Hover tooltip per bar: bucket label + income/spending value + bucket-end balance.
- Legend strip below: Income (emerald), Spending (red), Balance (blue line).
- Empty state and sparse-data note delegated to the surrounding window — the chart only renders what it's given.

### 3. New window — `mfl_desktop/ui/account_summary_window.py`

`QMainWindow`, non-modal, sized 1240×740 (same default as the existing report windows). Title `"<Account Name> · Summary"`.

Layout:

```
┌──────────────────────────────────────────────────────────────────────┐
│ <Account Name>                                                       │
│                                                                       │
│ ┌──── chart panel ─────────────┐  ┌──── info panel ─────────────────┐│
│ │ ACCOUNT BALANCE              │  │ SUMMARY                          ││
│ │  [BalanceFlowChart]          │  │   Recorded Balance      £x,xxx   ││
│ │ [30d][90d][YTD][1y][5y][All] │  │   N scheduled txns (or "none")   ││
│ │                              │  │                                  ││
│ │ REPORT: <period label>       │  │ ADDITIONAL INFO                  ││
│ │   Opening balance     £x,xxx │  │   Uncleared (N)       £x,xxx     ││
│ │   Inflows             £x,xxx │  │   Cleared Balance     £x,xxx     ││
│ │   Outflows         −£x,xxx   │  │                                  ││
│ │   Closing balance     £x,xxx │  │ UPCOMING                         ││
│ │                              │  │   <next N scheduled, 30 days>    ││
│ │                              │  │                                  ││
│ │                              │  │ [○ NO STATEMENTS · RECONCILE ›]  ││
│ └──────────────────────────────┘  └──────────────────────────────────┘│
│ ┌──── top-10 payees ───────────┐  ┌──── top-10 categories ──────────┐│
│ │ TOP PAYEES (period)          │  │ TOP CATEGORIES (period)          ││
│ │   Tesco       ▓▓▓░░  £812    │  │   Groceries   ▓▓▓░░    £1,043    ││
│ │   ...                        │  │   ...                            ││
│ └──────────────────────────────┘  └──────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────┘
```

Top row uses a `QSplitter(Qt.Horizontal)` so the owner can re-balance the columns; bottom row is a second splitter for the same reason. Vertically these two rows sit in a `QSplitter(Qt.Vertical)` so the user can give more room to either the chart strip or the breakdowns. Initial sizes mirror Net Worth's split.

Refresh policy: `eventFilter` on `WindowActivate`, same pattern as `BudgetWindow`. Each refresh pulls `list_transactions_for_account`, opening balance, scheduled txns due through `today + 30`, and runs the aggregations.

### 4. Entry points (in `mfl_desktop/ui/register_window.py`)

- Sidebar context menu — add `Account Summary…` as the first item in the account-row context menu (above New Account / Edit / Move to Folder / Delete).
- Sidebar double-click — connect `AccountSidebar.itemDoubleClicked` to a handler that opens the summary for the clicked account row. Folder rows and the "All transactions" row remain non-summary on double-click.
- Account menu — add `Account → Summary…` (Ctrl+Shift+A is taken by Manage → Accounts; use `Ctrl+I` for "info" — picked because it's not bound elsewhere and Banktivity-like apps use `Cmd+I` for the same shape).
- Single-instance keying — `self._account_summary_wins: dict[int, AccountSummaryWindow]` keyed on `account.id`. Opening an account that already has a window raises and focuses it; closing the window removes the entry.

### 5. Reconcile placeholder

Right-column footer renders a row matching the Banktivity look: a small status badge ("NO STATEMENTS" in slate text) and a "RECONCILE ›" button. Click → `QMessageBox.information(self, "Reconciliation", "Statement reconciliation is coming in a future release.")`. When the reconciliation arc lands, the handler is swapped to open the reconcile dialog; layout untouched.

## Consequences

### Good

- Per-account focus arrives without any schema change — every data feed already exists on the Repository.
- Visual style stays consistent with the rest of the app (paintEvent + `chart_helpers`); zero new dependencies.
- The combo chart answers two questions at once (flow + balance trajectory) without resorting to two side-by-side widgets.
- Reconcile placeholder anchors the future reconciliation UI in the layout so that ADR doesn't churn this screen when it lands.
- Pure-Python aggregation module is unit-testable in isolation and mirrors the budget arc's split, so the codebase stays predictable.

### Cost

- Single y-axis for the combo chart can look squished when the balance scale dwarfs the flow scale (or vice versa). Documented as the obvious v2 (dual axes) — easy follow-up if real use surfaces it.
- The "Balance reflects recorded transactions; valuations not yet wired." note will sit on every investment / property / vehicle summary until the valuation pipeline lands. It's a temporary honesty signal, but it's screen real-estate for now.
- Adding a sixth entry point shape (per-account summary) to the sidebar context menu makes that menu denser. Acceptable; the alphabetical-ish ordering keeps it readable.

### Follow-ups (not in this ADR)

- **Dual-axis combo chart.** Switch the balance line to a right axis if real use shows the bars getting stunted.
- **Inflow Top-10.** Symmetric panel for top payers; currently only outflows are listed.
- **Cleared-only balance line.** Original backlog mentioned "cleared-vs-running variants" — add as a small chip-toggle above the chart if the owner asks for it after living with the screen.
- **Per-account sparkline in the sidebar.** Once this screen exists, the per-account row could carry a tiny sparkline of the same balance series — cheap with the aggregation module already built.
- **Saved period preference per account.** Today, opening a summary always lands on the default period; persisting a per-account preference (last 90 days etc.) is a small additive change.
- **Reconciliation arc.** The placeholder button gets a real target. Will arrive under its own ADR.
- **Custom date range** on the period selector if real use shows the six presets are insufficient.

---

## Amendment 2026-06-06 — Period preset set updated; Custom range added

After living with the screen for a session, the owner asked for a different set of period presets and surfaced the Custom-range follow-up earlier than the original ADR anticipated.

### What changed

- **`PERIOD_KEYS`** swapped from `("30d", "90d", "ytd", "1y", "5y", "all")` to `("quarter", "6m", "ytd", "1y", "3y", "custom")`.
- **`PERIOD_LABELS`** updated to **Last Quarter / Last 6 months / Year to date / Last 12 months / Last 3 years / Custom**. The Banktivity inspiration screenshot used calendar-quarter wording; the swap aligns the rest of the row with the same finance-native vocabulary.
- **`period_bounds`** semantics:
  - `quarter` → rolling 90 days (consistent with "Last 6 months" and "Last 12 months" being rolling, not calendar-period).
  - `6m` → rolling 180 days.
  - `ytd`, `1y` — unchanged.
  - `3y` → rolling 3 × 365 days.
  - `custom` — raises `ValueError`; the calling window supplies its own bounds. The previous "all" key is retired; if the owner needs an effectively-unbounded view, Custom covers it explicitly.
- **`_DEFAULT_PERIOD` → `"quarter"`** on both `AccountSummaryWindow` and the drill-down (`TransactionsListWindow`). The previous default was `"90d"`; `quarter` carries the same 90-day rolling behaviour under the new vocabulary, so existing users see no surprise change in the chart on first open.
- **New `mfl_desktop/ui/custom_period_dialog.py`** — small modal `QDialog` with two `QDateEdit` calendar pickers (From / To), constrained to From ≤ To and both ≤ today. Defaults to the period the user was already showing — opening Custom while on "Last 12 months" pre-fills the dialog with that range, so the owner can tweak rather than restart. Cancel restores the previously-checked preset button.
- **Custom state on the windows**: `_custom_start`, `_custom_end`, `_previous_period`. New shared helpers in `mfl_desktop/account_summary.py`: `fmt_date_range(start, end)` ("1 Jun → 6 Jun" same year, "30 Dec 2025 → 6 Jun 2026" otherwise — avoids `%-d`) and `period_display_label(key, custom_start, custom_end)` ("Custom: 1 Jun → 6 Jun" when on Custom, the preset label otherwise). Used in the REPORT header on the summary screen and in the period chip on the drill-down window.
- **Drill-down inheritance**: `TxnListFilter` gained `custom_start: Optional[date]` and `custom_end: Optional[date]` so a drill-down opened from a summary screen on Custom inherits the same custom window. `TxnListFilter.signature()` includes the custom bounds so distinct custom ranges open as distinct drill-down windows (single-instance still holds per ADR-034 §3).
- **Drill-down period chip is now non-removable** — the period is always set (the user changes it via the button row, not by removing the chip). The previous `_on_remove_period` handler was deleted; the chip-builder gained an `on_remove=None` mode that omits the × button.

### Why this is an amendment, not a new ADR

The structural decision (six preset buttons, auto-granularity per span, period selector below the chart) is unchanged. Only the specific presets and the addition of the Custom escape hatch are different — and the Custom range was already flagged as a follow-up in this ADR's original Follow-ups list. The amendment keeps the change discoverable in the ADR that introduced the period selector in the first place.

### Considered alternatives

- **Keep "all" as an internal sentinel for "no period filter"** so the drill-down's period chip × could mean "show every txn". Rejected; complicates `period_bounds`' contract for a use case Custom covers explicitly (and arguably more honestly — Custom shows the picked bounds in the chip; "no filter" leaves the user guessing what's being shown).
- **Calendar-quarter semantics for "Last Quarter"** (i.e. show Q1 while we're in Q2). Rejected for consistency with "Last 6 months" / "Last 12 months" / "Last 3 years" all being rolling. If real use surfaces a need for calendar-quarter, an additional preset can land additively.
- **Make the drill-down period chip removable** and map × to a Custom dialog with a wide default. Rejected; non-removable is simpler and the Custom button is one click away.
