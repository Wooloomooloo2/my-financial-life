# ADR-138 — Budget funding model (balances vs income) + drop credit-limit-as-funds

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-058 (budget model — perimeter accounts, the D2 available pool, `budget_account.contribution`). ADR-055 (FX conversion of the pool). ADR-032 (the SQLite `CHECK`-rebuild recipe used by the migration). ADR-136 (home budget card — reads the same budget matrix, unaffected).

## Context

Owner feedback on budget setup:

1. A new budget's **available pool** is always seeded from the perimeter accounts' **balances**. The owner wants to choose, when setting up a budget, between:
   - **Account balances to start** — use the current balances (today's behaviour), and
   - **Income only** — fund the budget purely from *new money* (income) flowing into the selected accounts, ignoring the starting balances.
   *(A future feature — deciding what to do with excess balances, e.g. sweep to savings or a budget category — is explicitly out of scope here.)*
2. A credit card's contribution mode `'available_credit'` = **`credit_limit + balance`** (the card's spendable headroom) counted the **credit limit as funds**. The owner: *"that's not good practice."*

`compute_perimeter_pool` (ADR-058 R4a) summed each perimeter account's `contribution` — `'balance'`, `'available_credit'`, or `'excluded'`.

## Decision

**1. Per-budget funding model.** Add `budget.funding_mode` — `'balances'` (default, unchanged) or `'income'` — set on the budget-setup dialog (a radio pair at the top of the Accounts tab, "Fund this budget from: ● Account balances to start / ○ Income only"). `compute_perimeter_pool` branches on it:

- **`'balances'`** — the sum of each non-excluded perimeter account's **signed balance**, FX-converted (unchanged, minus the dropped credit mode).
- **`'income'`** — only `kind='income'` transactions into the non-excluded perimeter accounts over the **whole budget period** (`start_month` → its last month, including future-dated income — the owner's choice), FX-converted. Starting balances are ignored; this is the "give every new pound a job" basis.

The `pool` figure the whole matrix reads (D2 → "Unallocated = pool − assigned") is unchanged in shape; only its basis differs. `'excluded'` accounts stay out of the pool in both modes but remain in the perimeter for actuals.

**2. Drop `'available_credit'`.** A credit card's limit is no longer treated as funds. The `'available_credit'` contribution is removed: the setup dialog offers only **Balance / Excluded**; a credit card in `'balance'` mode contributes its **signed balance**, so its **debt reduces the pool** (owner's pick over "exclude the card entirely") rather than its limit inflating it. Migration 0035 rewrites existing `'available_credit'` rows to `'balance'` and tightens the `budget_account.contribution` CHECK to `('balance','excluded')` (ADR-032 rebuild recipe). The `account_dialog` credit-limit help no longer claims a budget counts available credit.

Rejected: a global (per-file) funding setting — it's a per-budget property (you might run a balances budget and an income budget); a card contributing £0 to the pool — the owner wants the debt reflected; keeping `'available_credit'` as an opt-in — the owner considers counting a limit as funds simply wrong.

## Consequences

- Budgets can be funded from income only, so a fresh-start / zero-based budget no longer inherits whatever happened to be in the accounts. `funding_mode` rides with the budget (create, duplicate, and a `set_budget_funding_mode` setter).
- Credit cards can no longer inflate the pool with their limit; their debt now reduces it, which is the conservative, correct behaviour. Existing budgets that used `'available_credit'` migrate to `'balance'` — their pool drops by the card's former headroom-vs-debt swing, which is the intended correction (they were over-stated).
- Schema: migration **0035** adds `budget.funding_mode` (CHECK) and rebuilds `budget_account` to drop the value. `compute_perimeter_pool`'s signature is unchanged, so every caller (budget window, home card) is unaffected.
- The **excess-balance workflow** (sweep to savings / a category) is deliberately deferred to a later ADR — this change only picks the funding basis.
- `tests/test_budget_funding_mode.py` 6/6 (funding_mode default/persist/create; `available_credit` rejected; balances-mode card debt reduces the pool — £3,500 cash − £318 card = £3,182, not limit-based; excluded card → cash only; income mode counts only in-period income, ignores balances + out-of-period income). Full suite 28/28; setup dialog verified visually.
