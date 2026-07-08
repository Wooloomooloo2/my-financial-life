# ADR-142 — Investment dialog: remember the cash-dividend category + never default a manual entry to 'matched'

**Date:** 2026-07-08
**Status:** Implemented
**Related:** ADR-089 (reinvested-dividend category memory — the pattern mirrored here). ADR-086 (ledger category on investment cash actions). ADR-130 (transaction status ladder — `matched` = an OFX download matched/added the row).

## Context

Two owner reports about the investment-transaction dialog:

1. **Cash dividends** always default to the seeded **Investment income** category. The owner files dividends under **Dividend Income** and has to re-pick it every single time. Reinvested dividends already remember their category (ADR-089, `reinvest_dividend_category_id`), but cash dividends had no equivalent.
2. The dialog **defaults the status to `matched`**. `matched` means "an OFX download matched/added this" (ADR-130) — it should never be the default for a **manual** entry. The register (non-investment) dialog already defaults to `pending`.

## Decision

**1. Remember the cash-dividend category.** Add a `dividend_category_id` app-setting with `get_dividend_category_id` / `set_dividend_category_id`, mirroring the reinvest pair exactly (including the guard that a stored id no longer pointing at a live `kind='income'` category falls back to no-default rather than mis-filing income). In the dialog, a cash **Dividend** action (`"Div"`) defaults to this remembered category (falling back to Investment income when unset), and **filing a cash dividend under a category writes it back** — so it self-seeds from the owner's first pick and stays put. Interest and cap-gain actions still seed Investment income (only dividends were reported, and the memory is per-purpose like the reinvest one).

**2. Default a manual entry to `pending`.** The investment dialog's create default and its edit-mode fallback both change from `matched` to `pending`, matching the register dialog — a manual entry is never `matched`. Editing an existing row still shows that row's stored status (only the missing-status fallback changed).

## Consequences

- After the first time you file a cash dividend under Dividend Income, every subsequent cash dividend defaults to it — no re-picking. Independent of the reinvest default, so cash and reinvested dividends can differ if wanted.
- Manual investment entries default to `pending`, so they don't masquerade as bank-matched — keeping the confidence ladder (ADR-130) honest.
- No schema change (both are app-settings); no migration. The touched-flag logic is unchanged, so a user's explicit pick still wins over the default.
- `tests/test_investment_dividend_category.py` 4/4 (setting round-trip + re-kinded fallback; manual entry defaults to pending not matched; a cash dividend seeds Investment income when unset and the remembered category once set). Full suite 32/32.
