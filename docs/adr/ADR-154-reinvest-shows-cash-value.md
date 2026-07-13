# ADR-154 — A reinvested dividend shows its cash value

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-048 / ADR-093 (the investment dialog, and the tri-field qty ⇄ price ⇄ total solver with its per-instrument price multiplier). ADR-043 (`txn.amount` is the signed cash impact). ADR-089 (a reinvest remembers its category).

## Context

The investment dialog shows a **Total cost** row for a Buy/Sell — the third leg of the tri-field group, so entering any two of quantity / price / total fills the third. A **reinvested dividend** got Quantity and Price and nothing else.

Owner feedback, on editing a real 401(k) row (`23.893205` shares at `25.06`): *"there is no way to see the cash value, which is useful to make sure the other numbers are correct."* The statement quotes the distribution as an **amount** (`$598.76`); the file stores it as shares × price. With no total on screen there was nothing to check the two imported figures against, and no way to go the other way — from the dividend the statement quotes to the share count it bought.

The row genuinely moves no cash (a reinvest buys shares with the distribution, so its signed cash impact is 0, ADR-043) — which is *why* the field was omitted. But "no cash impact" is not the same as "no cash value", and it was the value the owner needed to see.

## Decision

**Show a reinvest the same tri-field total, labelled *Dividend amount*.**

- `_solves_total(kind)` — a new predicate (`buy` / `sell` / `reinvest`) replaces the four scattered `kind in ("buy", "sell")` guards that gate the solver. A reinvest's total row is now visible, live, and solves in both directions: quantity + price fill the dividend amount; **dividend amount + price back out the quantity**, so you can type the figure off the statement and let the dialog compute the shares.
- **No commission leg.** `Total = qty × price × multiplier ± commission` on a trade; a distribution has no fee, so `_solve_trade_field` now zeroes the commission term for anything that isn't a Buy/Sell. That matters because the Commission row is only *hidden* on a reinvest, not cleared — switching Buy → Reinvest with a fee typed would otherwise have folded a phantom `9.99` into the dividend. `_on_action_changed` re-solves on the switch for the same reason.
- **The multiplier still applies** (ADR-093), so a reinvest on a bond or an option values correctly rather than assuming ×1.
- **The stored row is unchanged.** `_compute_amount` still returns `0.00` for a reinvest — the new field is an entry aid and a cross-check, never a cash movement. Nothing downstream of the dialog sees a difference.
- The hint line gains the running `→ value 598.76` a Buy/Sell already showed as `→ cash −598.76`, and the label flips **Total cost: ⇄ Dividend amount:** with the action.

Rejected:

- **Show the value in the hint line only.** Read-only, so it checks the numbers but doesn't let you enter the dividend and get the shares — half the value for the same code. The tri-field group already existed; reusing it was the smaller change.
- **A separate read-only "Cash value" row.** A fourth field duplicating a solver leg the dialog already has, and it would have needed its own recompute path.
- **Store the value in `txn.amount`.** Breaks ADR-043's invariant (cash balance = `SUM(amount)`) — a reinvest would suddenly spend money the account never spent.

## Consequences

- Editing the owner's `PGINX` row now reads `Quantity 23.893205 · Price 25.06 · Dividend amount 598.76` — the imported shares and price are checkable against the statement at a glance, which is what was asked for.
- Entering a reinvest by hand is faster: statement dividend + share price → the fractional share count fills itself, instead of being long-divided by hand.
- Buy/Sell behaviour is untouched (label, commission, accrued interest all as before), and a stored reinvest still moves no cash.
- Covered by `tests/test_investment_reinvest_cash_value.py` (8 tests: the row and its label, both solve directions, the option multiplier, the stale-commission leak, the zero cash impact on save, and the value seeding when re-opening the row). Verified headless with a rendered dialog. Existing dialog tests green.
