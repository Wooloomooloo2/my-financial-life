# ADR-086 — User-editable categories on investment transactions (income/expense actions)

**Date:** 2026-06-19
**Status:** Accepted.
**Amends:** ADR-043 (investment import/columns), ADR-048 (investment transaction dialog). **Builds on:** ADR-014 (category kinds), ADR-064/056/058 (kind-based report/budget aggregation).

---

## Context

Investment-account transactions already store a `category_id`, but it is **auto-derived and locked**: the `InvestmentTransactionDialog` assigns `Div`/`ReinvDiv`/interest/cap-gain actions to *"Income:Investment income"* and everything else to *Uncategorised*, with no picker and no Category column in the investment register. So the owner can't, for example, file an account fee as an expense or split investment income into more specific categories — and those flows are invisible to the Income & Expense / Sankey / budget views, which aggregate by **category kind**.

Owner decisions (`AskUserQuestion`, 2026-06-19):
- **Goal — reports & budget:** categorised investment income/fees should flow into the cashflow reports via their category kind.
- **Scope — cash income/expense actions only:** dividends, interest, cap-gain distributions, the manual *Cash in/out*, and imported fee/margin-interest actions are categorisable. **Buy / Sell / share-transfer / stock-split stay portfolio moves (Uncategorised)** — categorising those would inject their (large) trade amounts into spending/income reports.
- **Editing — dialog field + inline column:** a category picker in the dialog **and** an inline-editable Category column in the investment register.

## Why kind-gating, not account-gating

The reports already do the right thing: income = `amount>0` on `kind='income'`, expense = `amount<0` on `kind='expense'`, transfers excluded by kind. A dividend filed as *Investment income* (income kind, positive cash) is already counted; an account fee filed as an expense will now be counted; a Buy left Uncategorised contributes nothing. So **no report/budget code changes** — restricting *which actions* can be categorised is the entire safety mechanism.

---

## Decision

### Categorisable actions (`qif_actions.is_categorisable`)
A new pure helper marks the **cash income/expense** actions: `CASH_IN_ACTIONS` (div/intinc/cap-gain/return-of-capital + `x` variants), a new `CASH_EXPENSE_ACTIONS` (`miscexp`, `margint` + variants), and the manual `Cash` action. **Excludes** buy/sell/share-transfer/split — and reinvested distributions (`ReinvDiv`…), which are zero-cash share-ins whose income the returns report already books at `price×qty`; they keep their auto *Investment income* category, non-editable.

### Dialog (`InvestmentTransactionDialog`)
A **Category** picker (`make_category_picker`) is shown only for the income/cash action kinds. On create it defaults income actions to *Investment income* and the manual Cash action to Uncategorised; on edit it pre-selects the row's stored category. On save the chosen category is written for categorisable actions; reinvests keep *Investment income*; all other actions stay Uncategorised (unchanged).

### Inline register column
`COLUMNS_INVEST` gains a **Category** column (between Memo and Amount). It is inline-editable **only for categorisable rows** — `TransactionTableModel.flags()` and `setData()` both gate the investment Category cell on `is_categorisable(row.action)` (belt-and-suspenders: flags stops the editor opening, setData rejects a programmatic write). The existing `CategoryTypeaheadDelegate` auto-attaches by column name, so the in-cell typeahead + inline-create match the cash register. Buy/Sell/share rows show their (Uncategorised) category read-only and still open the dialog on double-click; the Category column's double-click is excluded from that dialog-open so it edits inline instead.

---

## Consequences

### Positive
- Investment **income and fees become first-class** in Income & Expense, Sankey, and budgets — via the kind rule already in place, no aggregation changes.
- One categorisation model across cash and investment accounts (same picker, same inline delegate, same memory hooks).
- Portfolio trades stay out of the cashflow reports by construction — the scope gate, not the user's vigilance, prevents distortion.

### Negative / trade-offs
- The manual *Cash in/out* action is genuinely ambiguous (a contribution is transfer-like, a fee is an expense); it is categorisable and the user picks the right kind (including a transfer category, which routes through the existing ADR-020 destination prompt). Documented, not auto-classified.
- Per-row editability makes the investment Category column behave differently from its neighbours (dialog-edited) — intentional, and the only inline-editable investment cell.
- Reinvested distributions remain non-editable *Investment income*; revisit if a user needs to re-file them.

### Ongoing responsibilities
- New investment actions are classified in `qif_actions` (one place); `is_categorisable` decides editability + the dialog picker's visibility.
- The kind contract is the safety boundary — never let a portfolio action become categorisable without re-checking the report impact.
