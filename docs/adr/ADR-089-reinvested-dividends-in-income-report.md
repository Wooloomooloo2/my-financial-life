# ADR-089 — Reinvested dividends as income: categorisable DRIPs + a value-at-qty×price toggle

**Date:** 2026-06-20
**Status:** Accepted.
**Amends:** ADR-086 (investment-transaction categories — lifts the reinvest exclusion). **Builds on:** ADR-088 (Income Over Time report), ADR-043/044/046 (investment import / holdings / returns).

---

## Context

The owner's 401(k) records dividends as **ReinvDiv** (DRIP) — a single reinvest
transaction with **no cash leg**, rather than eTrade's two-row *Div (cash in) +
Buy (cash out)*. In the live file that's **143 rows totalling £80,299.13**, held
as `quantity × price`, with `amount = 0`.

These were invisible to the new Income Over Time report for two independent
reasons:

1. **No cash.** The income aggregate sums signed `amount` (`amount > 0`); a
   reinvest's amount is 0, so there is nothing to sum.
2. **Not categorisable.** ADR-086 deliberately excluded reinvests from
   user categorisation ("keeps its auto *Investment income*"), and in practice
   the QIF-imported rows sit at **Uncategorised** (id=1, which is
   `kind='expense'`) — so even the kind filter would drop them.

The owner had already tagged their cash `Div` rows as *Dividend Income* and
wanted the reinvested ones to appear the same way. Chosen approach
(`AskUserQuestion`, 2026-06-20): **categorise + toggle** — keep the single DRIP
row, let it be tagged, and value it at qty × price behind an opt-in toggle.

## Units footnote (the bug this nearly shipped with)

A Buy's pence `amount` equals `quantity × price × 100` (verified across the live
Buy rows, ratio exactly 100) — i.e. **`price` is a per-share unit price in
pounds**, not pence. The reinvest valuation therefore scales `SUM(qty × price)`
by 100 to reach pence. (A throwaway check initially mis-reported the DRIP total
as £802.99; it is £80,299.13.)

## Decision

### 1. Reinvests are categorisable (amends ADR-086)

`qif_actions.is_categorisable` now returns True for `REINVEST_ACTIONS` as well
as the cash income/expense actions. **Safe by construction:** a reinvest is
zero-cash, so its category can never reach the strict-cashflow reports (they sum
`amount`, which is 0) — categorising it only lets the income report value its
share-held dividend. The register's Category cell keys off `is_categorisable`,
so it becomes inline-editable for reinvests automatically (flags + setData);
the investment dialog shows the Category picker for the reinvest kind and
defaults it to *Investment income*. `_kind` now classifies **all** reinvest
variants (`reinvdiv/reinvlg/reinvsh/reinvint/reinvmd` via `is_reinvest`) as the
`reinvest` UI group, fixing the prior `== "reinvdiv"`-only check that treated
the other variants as cash income.

### 2. Income Over Time values reinvested dividends at qty × price (toggle)

`Repository.income_aggregates` gains `include_reinvested`. When True, a second
pass over `txn` (reinvests are never split, so the split-unroll view is
irrelevant) values each reinvest row **on a `kind='income'` category** at
`ROUND(SUM(quantity × price) × 100)` pence, attributed to that row's category —
so a DRIP tagged *Dividend Income* lands in the same bucket as the cash
dividends. No double count: the cash pass requires `amount > 0`; reinvests are
always 0. Reinvests still at Uncategorised are excluded (the kind filter), so
the owner tags them first (now possible).

The toggle is persisted as `IncomeOverTimeFilters.include_reinvested_dividends`
(**default on** — the natural expectation for an income report) and surfaced as
an **"Include reinvested dividends"** checkbox shown only on the income filter
dialog. The field lives on the `IncomeOverTimeFilters` subclass (the Spending
report has no such concept); the shared window/dialog guard on its presence
(`hasattr`) so the spending path is untouched.

### 3. New reinvests get categorised automatically (import + dialog)

So future DRIPs don't recreate the Uncategorised backlog, reinvests auto-tag to
a **configured reinvest-dividend category** (setting
`reinvest_dividend_category_id`, read/validated by
`Repository.get_reinvest_dividend_category_id` — it only returns a live
`kind='income'` category, so a deleted/re-kinded one self-heals to "no
default"):

- **Import** (`import_service`): a reinvest-action row that is *still
  Uncategorised* after the source-category / payee-memory / rule passes falls
  back to the configured category. A rule the owner wrote still wins (this only
  fills what nothing else claimed); reinvests have no payee, so the existing
  passes never touched them anyway. Keyed on the **action**, so it's reliable
  regardless of the DRIP's memo ("Dividend" / "Pass Thru Div" / blank).
- **Dialog**: a reinvest defaults to the configured category (else seeded
  *Investment income*), and **saving a reinvest under a category writes it back
  as the default** — so the setting self-seeds through normal use, with no new
  preferences screen (there is none — settings are a flat key/value table). The
  owner tags one reinvest *Dividend Income* and every future import lands there.

## Scope / consequences

- The **Income Over Time** report counts reinvested dividends by default; the
  **Income & Expense** report is *unchanged* (stays strictly cash, so its
  savings-rate keeps its "cash actually kept" meaning). Adding the same opt-in
  toggle to I&E is a clean follow-up if wanted.
- To make their reinvested dividends show, the owner tags the 143 ReinvDiv rows
  as *Dividend Income* — fastest via a register bulk-edit — then they appear
  automatically (toggle is on by default).
- No migration; no change to the holdings/returns engines (the returns report
  already books reinvest income at price × qty independently — a separate
  report, no interaction).
- Verified headless against a copy of the live `mfl_dev.mfl`: with the DRIPs
  tagged income, the toggle adds exactly **£80,299.13**; the checkbox shows for
  income and hides for expense; the dialog/register expose the category for all
  reinvest variants.
