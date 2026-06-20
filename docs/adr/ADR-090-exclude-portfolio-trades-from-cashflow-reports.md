# ADR-090 — Exclude investment portfolio-move trades from the cashflow reports

**Date:** 2026-06-20
**Status:** Accepted.
**Amends:** ADR-018/030 (Spending Over Time), ADR-064 (Income & Expense), ADR-066 (Payee), ADR-068 (Category & Payee), ADR-056 (Sankey). **Builds on:** ADR-043/044 (investment actions), ADR-014 (category kinds).

---

## Context

The owner noticed investment **trades** polluting the Payee report as a large
**"(No payee)"** bucket. Investigation showed the symptom was the visible tip of
a deeper miscount:

- A **Buy** is `amount < 0` on the **Uncategorised** category, and Uncategorised
  is `kind='expense'` (ADR-014). The strict-outflow rule (`kind='expense' AND
  amount < 0`) therefore counts every buy as **spending**. In the live file
  that's **3,073 buys = £1,852,934** — ≈36% of the raw expense total
  (£5,154,544) — flowing into Spending Over Time, the Payee report, and
  Category & Payee. The Income & Expense and Sankey *expense* sides share the
  same rule and the same leak.
- Trades carry **no payee** (buys/sells/share-moves/splits all have
  `payee_id = NULL`), so in the Payee report they collapse into "(No payee)" —
  the noise the owner saw.

A trade is not spending: it moves cash *within* the portfolio (cash ⇄
securities). Cash distributions (Div / IntInc / cap-gains) and the manual Cash
in/out **are** real flows and must stay. Owner decision (`AskUserQuestion`,
2026-06-20): exclude portfolio-move trades from **all** cashflow reports.

## Decision

A shared `Repository._portfolio_move_exclusion()` returns a SQL clause + params
that drop rows whose transaction action is a **portfolio move** —
`SHARE_IN_ACTIONS ∪ SHARE_OUT_ACTIONS ∪ SPLIT_ACTIONS` (buy/buyx/cvrshrt,
sell/sellx/shtsell, shrsin/shrsout, the reinvests, stksplit) — i.e. exactly
`affects_shares`, reusing the `qif_actions` sets so it can't drift from the
holdings engine / importer. It filters by **txn id** (`t.txn_id NOT IN (SELECT
id FROM txn WHERE lower(action) IN …)`) because the split-unrolled
`txn_category_line` view exposes `txn_id` but not `action`; trades are never
split, so the parent action is authoritative.

Applied to the five cashflow aggregates:

- `spending_aggregates` (Spending Over Time)
- `payee_spending_aggregates` (Payee)
- the Category & Payee aggregate
- `income_expense_series` (Income & Expense)
- `sankey_category_totals` (Sankey)

**Not** applied to `income_aggregates`: trades never reach its income side
(buys/sells aren't `kind='income'`; reinvests are `amount = 0`), and its
*reinvested-dividend* valuation (ADR-089) is a **deliberate** inclusion of
reinvests at qty × price — guarded separately by its own toggle. The two are
consistent: a reinvest counts as *income* (the dividend it represents) but never
as *spending* (it has no cash outflow anyway).

## Consequences

- Spending / Payee / Category & Payee / I&E-expense / Sankey-expense drop the
  trade noise. Measured on the live file: Spending Over Time falls by exactly
  the trade total (£5,154,544 → £3,301,610), and the Payee "(No payee)" bucket
  falls from ~£1.85M to £5,696. Income totals are unchanged.
- **The owner's spending figures will drop** — this is a correction, not a
  regression: buys were never spending.
- Cash dividends/interest/cap-gain distributions and manual Cash in/out are
  untouched (not portfolio moves), so genuine investment cashflow still shows.
- No schema change, no migration, no data mutation — purely a query-time
  exclusion. The per-query cost is one indexed `NOT IN (subquery over txn)`.
- A residual small "(No payee)" can remain for genuinely payee-less *cash*
  expenses; that's correct (they really have no payee) and out of scope here.
