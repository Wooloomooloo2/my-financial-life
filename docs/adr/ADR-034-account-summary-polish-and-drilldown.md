# ADR-034 — Per-account summary polish: dual-axis chart, section cards, Top-N drill-down

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-033 (Per-account summary screen — the screen this round polishes), ADR-026 (paintEvent chart kit), ADR-018 (strict-outflow semantics for spending aggregates), ADR-022 (register typeahead delegates — reused in the drill-down window)

---

## Context

ADR-033 shipped the per-account summary screen. After living with it for a turn, the owner flagged three concrete pieces of feedback:

1. **Dual y-axis on the combo chart** — "accounts with a big difference between their balances and average spending in a month need 2 Y axis." The credit-card-paydown case (balance −£2,000 → −£100, monthly flow ~£200) makes the bars look stunted under the single-axis scheme ADR-033 settled on. This was the v2 already flagged at the end of ADR-033; real use confirmed the call inside one session.
2. **Section dividers** — "The different sections could be a bit more clear, so with some kind of border around them with soft curves." The four panels (chart, info, top-payees, top-categories) currently sit on the same flat white background separated only by `QSplitter` handles; nothing visually says "this is a unit."
3. **Drill-down from the Top-N panels** — "clicking Eating out and Coffees should bring up those items in a 'report', which would be likely be a register view with a pre-populated filter." Today the Top-N rows are purely visual.

The owner explicitly asked "how should we design that?" for the drill-down — surfaced one design decision via `AskUserQuestion` (window policy: one window per filter click vs. replace in place vs. tabs) and the owner picked **one window per filter signature**.

## Decisions

### 1. Dual y-axis on `BalanceFlowChart`

Replace the single-axis scheme with two **independent** y-axes — no forced common-zero.

- **Left axis** (`y_to_px_left`): bars only. `nice_ticks` over the combined range `(max_income, max_spending)`. Zero is wherever the math puts it inside the chart rect — typically near the middle.
- **Right axis** (`y_to_px_right`): the balance line only. `nice_ticks` over the combined range `(max_positive_balance, max_negative_balance)`. Zero is at a *different* y-pixel from the left axis's zero whenever the two scales differ.
- Left-axis labels render in slate-500 in the left margin; right-axis labels render in **blue-600** (the balance line's colour) in the right margin so the eye associates them with the line. Labels are `fmt_currency` with leading minus for negatives.
- The zero baseline is drawn for the **left axis only** — that's the line bars cross between income and spending. The balance line uses its own scale; drawing a second zero would be visual noise.
- Both axes use their own `nice_ticks` step independently — gridlines no longer align between sides because they're scaled to different ranges. That trade-off is the cost of honest scales; the gridlines are kept soft (slate-200) so they don't compete with the data lines.

**Considered alternatives** (rejected):

- **Force a common-zero proportion across both axes.** Picks the larger above-zero share and stretches the other axis's smaller side to match. Mathematically works for the credit-card case but breaks for the *opposite* case (cash account with always-positive balance: forcing common-zero pins zero to the bottom and the spending bars draw off the chart). Two independent zeros is the only scheme that doesn't break a real case.
- **Detect at runtime and switch between single and dual.** A heuristic (e.g. "use dual when `max_balance > 4 × max_flow`") would handle most cases automatically; rejected because the heuristic is brittle (which way does it round on borderline data?) and because the owner's feedback was unconditional — they want dual all the time once they've seen the trade-off.
- **Toggle in the chart legend.** Single by default, user clicks "scale independently" to flip. Rejected; the owner asked for the fix, not a toggle to apply it manually each time.

### 2. Section cards on `AccountSummaryWindow`

Wrap each of the four panels in a `QFrame` styled as a card: white background, 1px slate-200 border, 10px corner radius, 14px internal padding. Window background drops from white to slate-50 (`#f8fafc`) so the cards visually float against a neutral canvas.

- Cards have `objectName` set (`"chartCard"`, `"infoCard"`, etc.) and the QSS rule is scoped (`QFrame#chartCard { … }`) so child widgets don't inherit the rounded border / background by accident.
- The chart inside the card still paints its own white fill — same colour as the card, no seam. The card border stays visible because the chart's QPainter doesn't paint outside its own widget rect.
- The title bar (account name + Summary header) gets its own card-less treatment — it's a header, not content.
- `QSplitter` handles between cards stay; the cards add visual separation, the splitters keep the resize affordance.

**Considered alternatives** (rejected):

- **Drop shadows via `QGraphicsDropShadowEffect`.** Visually rich but well-known to cause repaint issues on Qt and slows down resizing. Rejected for cost vs. benefit; flat borders read fine.
- **Different background colour per card type** (chart vs. summary vs. lists). Rejected; the visual unit is the screen, not the panel kind. Same neutral white across all four cards.

### 3. Top-N drill-down — new `TransactionsListWindow`

Click a row in Top Payees or Top Categories → a new non-modal `QMainWindow` opens, showing the same transactions the row aggregated.

**Data shape**: `TopNRow` gains an `entity_id: Optional[int]` field (None for the synthetic `(No payee)` / `(Uncategorised)` buckets). The aggregation module computes it via the `id_of` callable passed into the existing `_top_n` helper.

**Click affordance**: `_TopNList` becomes interactive. `mousePressEvent` hit-tests against per-row rectangles populated each `paintEvent`; emits a `row_clicked(TopNRow)` signal. Hover over a real row sets the cursor to `PointingHandCursor` and tints the row background slate-50; rows with `entity_id is None` aren't clickable (you can't filter to "no payee" meaningfully against a name; the bucket would need its own special-case flag — deferred).

**New window — `mfl_desktop/ui/transactions_list_window.py`**:

- Constructor takes `(repo, filter_spec, parent)`. `filter_spec` is a new frozen dataclass `TxnListFilter(account_id, account_name, category_id, payee_id, payee_name, period_key, label)` carrying everything needed to render the breadcrumbs and apply the filter.
- Window title: `"<filter label> — <period label> — <account name>"`, e.g. `"Eating out — Last 90 days — Joint Current"`.
- Top strip: **breadcrumb chip row** showing each active filter as a small pill. Each chip has an × to remove that filter — removing Account widens to cross-account, removing Period widens to "All time", removing Category/Payee strips the entity filter. Removing the last filter doesn't close the window; the title bar carries the breadcrumb history.
- Below chips: the same six-preset period selector as the summary screen, pre-selected to the calling period. The user can change the period inside the drill-down independently.
- Main: `QTableView` using the existing `TransactionTableModel` (account_id or None for the source). A new `DrillDownFilterProxy` extends `TransactionFilterProxy` with `set_date_range(from_iso, to_iso)`, `set_payee_id(int|None)`, and `set_category_descendant_ids(set[int])` so the category filter is "this category and its descendants" (matches the recursive walk reports already use; clicking "Groceries" surfaces Coffees and Eating out underneath it).
- Inline editing reuses the existing delegates (`PayeeTypeaheadDelegate`, `CategoryTypeaheadDelegate`, `StatusDelegate`); edits hit the same Repository and propagate. When the user closes the drill-down and the summary screen re-activates, ADR-033's `WindowActivate` refresh picks up the changes automatically.
- Footer: `"<N> transactions · Total <signed sum>"` for the filtered set.

**Single-instance per filter signature**: the summary window keeps `_drilldown_wins: dict[FilterKey, TransactionsListWindow]` where `FilterKey` is a small tuple `(account_id, period_key, category_id, payee_id)`. Owner picked **"new window per filter"** in the design AskUserQuestion — that's per *distinct* filter, not per *click*. Clicking the same Top-N row twice raises the existing window; clicking a different row spawns a new one. This avoids dupes while still letting the owner compare Tesco vs. Shell side-by-side.

**Parented to the summary**: drill-down windows are children of their summary window (Qt parent). Closing the summary closes all its drill-downs; this is the same pattern the register window uses for the summary itself.

**Reusability** (not implemented in this round, but the dataclass shape supports it): the same `TransactionsListWindow` should later open from Spending Report bar clicks, Net Worth account rows, the payee/category dialogs' "show transactions" verbs. `TxnListFilter` carries enough state to render any of those entry points. Deferred until each of those screens grows a drill-down; this round just wires the summary screen.

**Considered alternatives** (rejected):

- **Jump to the main register with the filter applied.** Hijacks whatever filter the user already had in the register and forces them to manually restore it. The summary is meant to be a focus/report view; drilling down into a separate window keeps the main register stable.
- **Inline drawer/third pane inside the summary window.** Would conflict with the section-cards feedback in this same ADR — the screen is already four cards on two rows, a fifth pane would crowd it.
- **Read-only "report" view.** The owner used both "report" and "register view" — register-view wins on the editability axis. Spotting a miscategorised txn from a drill-down should let you fix it in place, not bounce back to the main register to do it.
- **Category filter as exact-match only** (not descendants). Rejected because reports already use descendants, and clicking "Groceries" reading only literal-Groceries rows would feel wrong when the user knows Coffees lives under it.

## Consequences

### Good

- Dual axes solve the credit-card-paydown view permanently; bars and balance line both render at their natural scale.
- Section cards make the screen read as four units instead of one busy panel; matches the modern flat aesthetic ADR-026 established.
- Top-N drill-down closes the screen's biggest interactive gap — the breakdowns are no longer purely visual.
- `TransactionsListWindow` is the right shape to reuse from every other report screen later — this round invests once and benefits the broader reports arc.

### Cost

- Dual-axis chart is harder to read at first glance (two scales, no aligned gridlines). Colour-coding the right-axis labels mitigates but doesn't eliminate the learning curve.
- Four cards mean more widget construction per summary window open — negligible at MFL's scale but worth noting if the screen ever lives inside a list.
- The drill-down window-per-filter policy means a curious owner clicking through five categories ends up with five windows to close. Owner picked this trade-off explicitly over the replace-in-place alternative.
- `DrillDownFilterProxy` extends `TransactionFilterProxy`; the surface area of filters grows. Kept as a subclass so the main register's proxy stays simple.

### Follow-ups

- **Reusable drill-down from other screens.** Spending Report bars, Net Worth account rows, payee/category dialogs — each is a one-line `TransactionsListWindow(repo, TxnListFilter(...))` call away once each screen grows the entry point. Not in this round.
- **"No payee" / "Uncategorised" drilldowns.** Rows with `entity_id is None` aren't currently clickable. They could open a drill-down filtered to txns with NULL payee_id / Uncategorised category — small follow-up via a special `payee_is_null=True` flag on `TxnListFilter`.
- **Cross-account drill-down from the summary**. Today the drill-down inherits the summary's account_id. Removing the Account chip widens to cross-account; the breadcrumb-chip × is the natural place for this.
- **Footer total semantics**. v1 shows the signed sum; a richer footer (inflows + outflows + net) is the obvious next step if the owner asks.
