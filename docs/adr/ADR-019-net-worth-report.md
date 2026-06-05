# ADR-019 — Net Worth report

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design — `account.family`, `account.is_liability`, `valuation` table); ADR-018 (Reports framework — non-modal window, single-instance, opened from the Reports menu)

---

## Context

After the Spending Over Time chart, the owner asked for a Net Worth view modelled on Pocketsmith's Assets / Net Worth / Debts layout. Three columns: a summary panel on the left with the big total and a proportional split; an Assets column listing accounts grouped by family; a Debts column doing the mirror. A "+ Asset" / "+ Debt" button at the bottom of each column adds a new account inline.

Three decisions follow:

1. **Visualisation of the split.** Pocketsmith uses a pie chart. The owner's standing rule (per ADR-018 §pie charts) is no pies — they "rarely convey accurate data". A substitute that conveys the same proportional information is needed.
2. **What balances do investment / property accounts show?** The schema's `valuation` table is reserved for mark-to-market entries (latest valuation = balance for those families). It isn't wired to any UI yet, so investment / property accounts have no valuations recorded today.
3. **How is the family grouping expressed?** The schema's `account.family` column is the natural axis (cash / credit / investment / property). The display labels and colours can be derived from a small static table.

## Options considered

### Visualisation — pie / horizontal proportional bar / treemap / list only

- *Pie*: rejected — owner rule. Pie segments are also hard to compare at a glance once there are more than three of them.
- *Horizontal proportional bar* (chosen): single bar, segments coloured by family, widths proportional to amount. Easy to read; preserves the "compare two segments by length" affordance that pies lack; degrades gracefully on small widths.
- *Treemap*: more compact for many families but harder to label and visually noisier. Not warranted for the four-or-fewer families in v1.
- *List only*: clean but loses the visual sense of the split. The owner's reference (Pocketsmith) explicitly *had* a visual, so dropping it would miss the brief.

### Investment / property balances — opening + sum(txns) / latest valuation / hybrid

- *Latest valuation*: correct semantically (a house isn't worth `opening_balance + sum(txns)`). Requires the `valuation` table to be populated, which means a Valuations UI we don't have.
- *Opening balance + sum(txns)* (chosen for v1): the same formula every other account family uses today. Works for any account that has an opening balance set when created; gives the user something visible immediately. Will be wrong-by-design for investment accounts where the cash basis ≠ market value, but the user can put a reasonable round-number opening balance per holding to approximate. Tracked as a follow-up: a Valuations UI (or even a simple "Update balance to £X" verb on an account) closes the gap.

A hybrid — use latest valuation if one exists, fall back to opening + txns — is the obvious eventual target, but with zero valuations in the schema today the fall-back is the only thing in use. We'll add the latest-valuation branch when the Valuations UI ships.

### Family display — driven by `account.family` / `account.type` / hand-curated

- *`account.family`* (chosen): four buckets (cash / credit / investment / property) are exactly the level of detail Net Worth wants. Aligns with the existing `account_types.py` mapping.
- *`account.type`*: too fine — Current Account and Savings would appear as separate Net Worth buckets even though both are "Cash & Bank" to a user.
- *Hand-curated buckets*: lets us split "Cash & Bank" into "Bank" and "Cash" the way Pocketsmith does, but adds a parallel categorisation the schema doesn't model. Skipped in v1; revisit if users actually want it.

### Adding accounts — separate Asset / Debt dialogs / one combined dialog

The screenshot has separate "+ Asset" and "+ Debt" buttons. Two implementation options:

- *Separate dialogs* pre-selecting the type by intent: clicking "+ Asset" pre-selects "Current Account"; "+ Debt" pre-selects "Credit Card".
- *Same dialog* both buttons open, no preset (chosen for v1): the existing `AccountDialog` already has a Type combo. Pre-selecting would only save one click and would surprise a user who wants e.g. to add an "Investment" via the "+ Asset" button. Keeping one entry point keeps the schema-of-truth (account types in `account_types.py`) the single dropdown.

## Decision

**`NetWorthWindow`** is a non-modal `QMainWindow` opened from **Reports → Net Worth…**. Single-instance via a `_net_worth_win` attribute on `RegisterWindow`, matching the spending report pattern.

**Three columns** in a `QSplitter`:

| Column | Content |
|---|---|
| Summary | Title ("Net Worth"), big total (green when positive, red when negative), `ProportionalBar` showing asset family widths, colour-coded legend listing each family's amount (assets section then debts section). |
| Assets | Header "Assets" + total in green, "WHAT I OWN" subhead, a `QTreeWidget` (group rows by family, child rows per individual account, expanded by default), and a "+ Asset" button at the bottom. |
| Debts | Mirror of Assets in red. Liability balances (negative in the database) are displayed as positive numbers (£429 owed rather than -£429). |

**Family → view** mapping is a small static table at the top of `net_worth_window.py`:

```python
_FAMILY_VIEW = [
    ("investment", "Investments",   QColor("#2563eb"), "asset"),
    ("property",   "Property",      QColor("#14b8a6"), "asset"),
    ("cash",       "Cash & Bank",   QColor("#22c55e"), "asset"),
    ("credit",     "Credit Cards",  QColor("#ec4899"), "debt"),
]
```

Order in the list = display order. Adding a new family means adding a row here; the rest of the report adapts.

**`ProportionalBar`** (`mfl_desktop/ui/proportional_bar.py`) is a small `QWidget` that paints horizontal segments proportional to a list of `BarSegment(label, amount, color)`. Zero-amount segments are skipped; the bar carries a subtle border and a grey background so empty regions look intentional rather than broken.

**Balances** come from `Repository.compute_account_balances` (existing helper). Asset total = sum of non-liability family balances; debt total = sum of negated liability balances; net worth = asset total − debt total.

**Add Account** ("+ Asset" / "+ Debt") opens the existing `AccountDialog` with no preset, calls `Repository.create_account`, refreshes the report, and asks the parent `RegisterWindow` to reload its sidebar so the new account appears there too.

## Consequences

### Positive
- Net Worth at a glance — total, proportional split, per-account drill-down — in one window.
- Pie-chart-free per the owner's rule, with a horizontal proportional bar that's easier to read at a glance and degrades gracefully on small widths.
- Adding an asset or a debt is one click, opens the same dialog as everywhere else, immediately reflects in the report and in the register's sidebar.
- The static family-view table is the single point of truth for adding new families later (vehicles, gold, etc.).

### Negative / trade-offs
- Investment / property balances will be wrong for users who don't enter a meaningful opening balance — until a Valuations UI ships, the report can only reflect what's in `opening_balance + sum(txns)`. Documented; the user expectation is that the v1 number is a starting approximation.
- The "+ Asset" and "+ Debt" buttons open the same dialog, so the user has to pick the right type in the Type combo. Pre-selecting felt more surprising than helpful.
- No multi-currency conversion. Cash account in USD and house in GBP sum naively in £ today. The same naive-sum limitation as ADR-015 — revisit alongside that ADR when the user actually has multi-currency holdings.
- No trend over time. A net-worth-over-time line chart is the natural next iteration; not in v1.

### Ongoing responsibilities
- When the Valuations UI ships, `compute_account_balances` (or a new dedicated `net_worth_balances`) should branch on family: cash / credit keep the txn-sum formula; investment / property use the latest valuation if one exists, fall back to the txn-sum otherwise. The Net Worth report calls into that helper without changing its own code.
- When a new account family is added (new value in `account.family`), the `_FAMILY_VIEW` table must be extended — otherwise its accounts silently disappear from the report. The CHECK constraint on `account.type` is a backstop against typos, but `family` is freeform; consider a CHECK on family too if we ever ship a new family.
- A future "Net Worth Over Time" line chart can sit next to "Net Worth" in the Reports menu; the daily balance series is computable from `opening_balance + cumulative sum of txn.amount up to each date` — same Repository, one new query.
