# ADR-095 — Loan accounts and amortization

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-010 (schema), ADR-008/032 (account types & families), ADR-019/055/067 (Net Worth), ADR-020/051 (transfers & splits), ADR-023 (scheduled transactions), ADR-058 R4b/R4c (pay-down & savings goals), ADR-094 (budget bills), ADR-075 (Home dashboard). Owner-requested flagship for the 1.0 ship.

---

## Context

MFL models cash, credit, investment, property and vehicle accounts, but has no concept of an **amortizing loan** — a mortgage, car loan, or personal loan where a fixed periodic payment splits into a shrinking *interest* part and a growing *principal* part, and the balance follows a known schedule. A credit card (the only existing liability) is a revolving balance with no term, payment formula, or principal/interest split, so it can't stand in.

The owner wants loan accounts capturing, at minimum: original amount; principal already paid; interest rate + type (APR applied daily / monthly / annually, entered or calculated); minimum payment (entered or calculated); optional extra payments; a choice to track **principal + interest** or **the whole amount**; for the split mode, a paying account and whether interest is booked **in the loan account** (preferred) or **on the paying account as a split**; and an **amortization schedule as a table and a chart**. Loans must appear on the Home page, be **added to the budget automatically** (principal/interest split computed for you), and count in **Net Worth**. A *"what-if" extra-payment scenario* is explicitly a **future** feature.

---

## Decision

A loan is a **new account family** plus a 1:1 **terms table**, a **pure amortization engine**, a **schedule window** (table + chart), and a **split-aware payment** primitive that reuses the existing transfer/split machinery. Budget and Net Worth integration reuse existing concepts (pay-down goals; the liability family).

### Schema (migration 0031)

- **`loan_std` account type**, family `loan`, **liability** (`account_types.py` + the `account.type` CHECK widened via the ADR-032 table-recreate recipe). A loan's balance is the usual `SUM(txn.amount)` — negative = owed — so Net Worth, the sidebar, and balances work with no special-casing beyond the family being a liability.
- **`loan` table** (1:1 with the account):

| Column | Meaning |
|---|---|
| `account_id` PK | the loan account |
| `original_amount` | original principal, pence |
| `principal_paid` | principal repaid before tracking began, pence (so **current principal = original − principal_paid**) |
| `interest_rate` | annual rate %, e.g. 5.5 |
| `compounding` | `daily` / `monthly` / `annually` (how the APR → a monthly periodic rate) |
| `term_months` | loan term (drives the calculated payment); nullable |
| `payment` | the scheduled periodic payment, pence; nullable ⇒ calculated from term |
| `extra_payment` | optional extra per period, pence; 0/NULL = none |
| `start_date` | first-payment / origination date |
| `payment_day` | day-of-month the payment falls (1–31) |
| `track_mode` | `split` (principal+interest) / `whole` |
| `interest_source` | `loan` (interest booked in the loan account — preferred) / `payment` (interest as a split on the paying account) |
| `payment_account_id` | where the cash comes from (split mode) |
| `interest_category_id` | the interest-expense category |
| `goal_id` | the auto-created budget pay-down goal, if any |

### Pure amortization engine (`loan_calc.py`)

Mirrors `budget_calc` / `goal_calc` — no Qt, no SQL. `compute_schedule(...)` replays the loan from its **current principal** forward, one monthly payment at a time, each `interest = balance × monthly_rate`, `principal = payment + extra − interest`, until the balance hits zero (the final payment trimmed). Returns `AmortRow`s (number, date, payment, interest, principal, extra, balance) plus totals (total interest, total paid, payoff date, n payments). `required_payment(...)` is the standard annuity formula `P·r / (1 − (1+r)⁻ⁿ)` so a blank payment is **calculated** from the term; `monthly_rate(apr, compounding)` converts the APR (monthly = apr/12; annually = effective monthly of annual compounding; daily = effective monthly of daily compounding). Negative amortization (payment ≤ interest) is detected and flagged rather than looping. The engine works in float internally (compound-rate powers) and quantizes each row to 2-dp `Decimal` — it's a forecast, not ledger truth.

### Split-aware payment (`Repository.post_loan_payment`)

Recording a loan payment computes the current split from the live balance + terms, then writes through the **existing** primitives:

- **`whole`** — one transfer of the full payment from the paying account into the loan account (balance moves toward zero by the full payment; no interest line).
- **`split` + `interest_source='loan'`** (preferred) — a transfer of the full payment into the loan account, **plus an interest-expense txn in the loan account** (category = interest), so the loan balance nets down by *principal* and the interest is visible on the loan.
- **`split` + `interest_source='payment'`** — a **split transaction on the paying account** (ADR-051): a transfer line for the principal (into the loan account) + an interest-expense line — so the interest sits on the paying account.

The interest amount is dynamic (it shrinks as the balance falls), computed at post time from the live balance — never a fixed stored split.

### Surfaces

- **Loan dialog** (create/edit) captures every field; the payment and rate can each be **typed or calculated** (a "Calculate from term" affordance), and the split-mode rows (paying account, interest source, interest category) reveal only in `split` mode.
- **The amortization schedule lives on the Account Summary screen.** The summary window already swaps its layout by family (cash → cash-flow chart + Top-N; investment → holdings dashboard); a loan now gets a dedicated **`LoanScheduleWidget`** (`loan_schedule_view.py`) — a summary line (balance owed, payment, payoff date, remaining interest), a **paintEvent declining-balance chart**, the full **schedule table** (date · payment · interest · principal · balance), and a **Record payment** action that posts the next split. A loan deliberately has **no Top-Payees / Top-Categories panels** — it has no payees or spending categories to rank. Reached the way every account summary is: double-click the sidebar row, **Account ▸ Summary…**, or the sidebar context menu; **New Loan…** opens it on the new account.
- **Net Worth** — loans are liabilities by family; they flow into the Debts side with no new code beyond the family.
- **Home dashboard** — loans surface in the accounts-by-family card and the net-worth card (reusing ADR-075 compute).
- **Budget** — on loan creation the app **offers to track the payoff** as an ADR-058 R4b **pay-down goal** (the loan account at 100%, target 0 by the payoff date), so the principal commitment shows in the budget's Goals section with its required monthly; the interest flows through as an expense when payments post. (Consistent with ADR-094's "ask before mutating the budget".)

### Scope boundaries (Round 1 = the owner's "full feature", minus what-if)

Shipped: account + terms + dialog, amortization table + chart, split-aware **Record payment**, Net Worth + Home, budget pay-down-goal integration. **Deferred:** the **what-if** extra-payment scenario (explicitly future); **auto-posting** the recurring payment with its dynamic split (Round 1 records payments on demand — the amortization table is the forward plan; a scheduled auto-split post is a follow-on that extends ADR-023's post path); per-day-count exact daily interest (Round 1 uses an effective-monthly rate); offers/property-style mark-to-market.

---

## Consequences

- Loans are first-class: they amortize, show a schedule + chart, post payments that correctly split principal vs interest, count as debt in Net Worth, and surface on Home and in the budget.
- New `loan` family + `loan_std` type, `loan` terms table (migration 0031), pure `loan_calc.py`, `Repository` loan CRUD + `post_loan_payment`, a loan dialog, and a `LoanScheduleWidget` embedded in the Account Summary. No change to existing accounts.
- The budget pay-down goal reuses ADR-058 R4b wholesale — no new budget object — and the payment split reuses ADR-020 transfers + ADR-051 splits, so the integrations rest on proven code.

### Rejected alternatives

- **Model a loan as a credit-card account.** No term, payment formula, or principal/interest split — it can't produce a schedule or a correct payment.
- **Store the principal/interest split per period in a table.** The split is a deterministic function of the live balance + terms; storing it invites drift and breaks the moment an extra/early payment changes the trajectory. Compute it.
- **A bespoke loan-payment budget object** (a "loan envelope" with its own P/I rows). A pay-down goal already expresses "reduce this liability to zero by a date" and is rendered in the matrix; reusing it avoids a parallel concept.
- **Auto-post the recurring split payment in Round 1.** The dynamic split on the ADR-023 auto-post sweep is the fiddly part; recording on demand is correct and shippable now, with auto-post a clean follow-on.
- **Ship what-if scenarios now.** Explicitly future per the owner; the engine is built so a scenario just re-runs `compute_schedule` with an altered extra payment.
