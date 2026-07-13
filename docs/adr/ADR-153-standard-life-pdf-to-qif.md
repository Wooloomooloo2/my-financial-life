# ADR-153 — Standard Life pension statements: PDF → QIF, with prices solved from the switches

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-043 (QIF import; investment actions). ADR-044 (holdings engine). ADR-053 (ShrsIn/ShrsOut are custodian moves, not disposals — cost basis carries across). ADR-013/ADR-093 (`security_price`; `manual` is the universal fallback for untickered securities). ADR-148 (the earlier CSV import of this same plan — batch 43).

## Context

Standard Life will not export anything machine-readable for a Group Stakeholder plan. The only artefact is a **PDF transaction statement**, and the owner's covers 15 Apr 2005 → 9 Jul 2026: 7 contributions, 37 "automatic phased switches" within the Lifestyle profile, and a closing position of £9,277.99.

The account already existed and read **£2,263.31** — the 7 contributions, imported earlier from a CSV as **plain cash rows** (ADR-148, batch 43): no fund, no units, no price. So the pension was showing its 2005 contributions and none of 21 years of growth, and there was no path to fix that by hand.

Three things stood in the way of a mechanical conversion:

**1. No prices.** These are an insurer's internal funds. They have no ticker, so Tiingo can never price them (the same wall as ADR-090's bonds), and the statement prints exactly **one** price per fund — on the closing date. An import with no price history values a holding off its last trade, so the account would have been stuck at its 17 Jun 2026 value (£9,251.00) and its chart would have been a flat line.

**2. Units don't conserve.** Replaying the switches naively goes negative: the plan sells 2,202.631 units of `SL Managed P` in Jul 2023 but the 2005 contributions only ever bought 2,012.189. Units *grow* between switches, by an amount the statement never itemises.

**3. One "switch" isn't a switch.** On 29 Jul 2025 both funds are sold and two funds with entirely new names are bought.

## Decision

`tools/standard_life_pdf_to_qif.py` (operator tool, not shipped) converts the PDF to a QIF the existing investment importer already understands, plus a unit-price CSV.

### Prices are recoverable — the switches are a linear system

A Lifestyle switch is not a partial rebalance. It **liquidates the whole plan and rebuys it** in the new target mix, which is why "Amount of switch" tracks the plan value (£6,651.51 in Jul 2023 → £9,251.00 in Jun 2026) rather than some small rebalancing delta. So each switch states the same total twice:

```
sold_a·Pa   + sold_b·Pb   = total      what was sold is the whole plan…
bought_a·Pa + bought_b·Pb = total      …and all of it went straight back in
```

Two equations, two unknowns, solvable on all 37 dates. **This is the only source of unit prices in the document.**

It is independently checkable: solving the closing date yields **123.91p / 116.38p**, and the statement separately publishes **124.0p / 116.4p**. Those published prices are never fed in — they *can't* be, because they're rounded to 0.1p and don't reproduce the stated fund values. The exact price is `value / units`, and that's what the tool uses.

### The missing units are policy credits — book them as ShrsIn

The drift is Standard Life crediting **free units** — the statement's "Total credits" line, £513.08. That is a share-in with no cash leg, so it is emitted as `ShrsIn` and the replay lands on the closing units exactly.

Their **dates are not recoverable**, and this is the one place the conversion is lossy. The statement itemises nothing between 2005 and 2023, yet units grew by ~190 across that window, so those get booked in one lump at the first switch that reveals them. Consequences: unit counts are exact, **cost basis is approximate, and any gain/loss figure on this account is indicative**. Accepted — it's a tax-free wrapper, so nothing downstream depends on the basis being right, and the alternative (invent a schedule) would be fabrication dressed up as precision.

### The rebrand is a re-designation, not a trade

29 Jul 2025 is a **share-class change**: `SL Managed P` → `SLMixAstMgdS7P`, `SL MAMgd2060 P` → `SLMan2060ShS7P`. The same money stays in the same funds under new unit classes. Emitted as `ShrsOut`/`ShrsIn`, which ADR-053 already defines as a custodian move carrying cost basis across.

Recording it as Sell+Buy would have booked a **phantom ~£8,000 realised gain**. It is also the one switch whose prices are *not* recoverable — four unknowns against two equations — and it needs none, because no cash moves.

### Cash: emit only the Buy leg

A pension has no cash sleeve. Default is to emit the `Buy` only, on the assumption the contribution already sits in the account as cash — which it did. Cash therefore lands at exactly **£0.00**. `--contrib-cash` emits a funding `Contrib` row before each `Buy` for an empty account.

## Guard rails

**The tool refuses to write unless the replay reconciles** against the statement's own investment summary, fund by fund, to the unit (exit 1). Everything above is inference; this is the check that makes it trustworthy.

That check has a blind spot, and it needed a second guard. With `--since` (for a follow-up statement that re-lists history already imported), the replay starts holding nothing — and the credit mechanism will cheerfully "credit" the *entire* missing opening position into existence. It still reconciles, because the fabricated units land on the right closing figure. Importing that into an account that already holds them **doubles the position**. So: selling a fund the replay holds none of is a hard error naming the exact `--hold` to pass, not a warning.

The remaining foot-gun is `--contrib-cash` on an account whose cash rows already exist. It doubles the money, and unit reconciliation cannot see it, because it checks units. Documented at the flag and in the module docstring.

Rejected:

- **A CSV price importer in the app.** The right long-term answer, and it belongs on the backlog — but it's a UI + mapping surface for a problem exactly one account has today. `--load-prices` writes `security_price` directly (`source='manual'`, which already wins over `tiingo`/`transaction` per ADR-013).
- **Skipping the switches; just book the closing position.** Two `ShrsIn` rows and done. Loses the price history, so the chart is a flat line, and the register would show a pension that sprang into existence at its current value.
- **Modelling switches as ShrsOut/ShrsIn too** (no cash, no realised gain). Tempting, and it would sidestep the price solve entirely — but then there is *no* price history at all, since the prices only exist as a by-product of the sell/buy legs.
- **Hardcoding the fund names and the rebrand mapping.** They're derived: a rebrand is a switch whose sold and bought fund sets are disjoint, and the sleeves pair positionally down the two columns (count-checked, and a mispairing breaks reconciliation loudly).

## Notes on the PDF itself

Layout-mode extraction is mandatory (`extract_text(extraction_mode="layout")`). The switch tables are two side-by-side columns and plain extraction interleaves them into nonsense.

Within a block, everything keys off the **character column**, never off match order. Both columns usually name the *same* fund (`SL Managed P` | `SL Managed P`), so order tells you nothing — pairing by order silently attributes the buy leg's units to the sell leg. The split column is read from the `Funds switched to` header, because the tables shift horizontally between pages.

## Consequences

- Standard Life Pension reads **£9,277.99** — 2,992.432 units of `SLMixAstMgdS7P` (£3,707.86) + 4,786.346 of `SLMan2060ShS7P` (£5,570.13), cash £0.00, nothing unpriced. Every figure matches the statement's investment summary to the penny.
- 74 unit prices spanning Jul 2023 → Jul 2026, so the funds chart plots the lifestyling period instead of a flat line.
- Applied to the live DB (`mfl_dev_windows6 - Copy.mfl`) after a `.backup` snapshot (`…pre-standard-life-20260711-2215.mfl`): 225 rows imported, 0 skipped, integrity ok, and total `txn` went 39,565 → 39,790 — exactly +225, so nothing outside the account moved.
- **These rows carry no provider IDs.** The importer cannot dedupe them, so re-importing an overlapping period double-counts. Follow-up statements must use `--since` + `--hold`.
- Writing the QIF against the app's real parser caught a defect that would otherwise have shipped: **QIF dates are US `M/D/Y`** (`qif_parser._parse_qif_date`). An initial `D/M/Y` emitter had every row silently dropped — and every day ≤ 12 would have been transposed rather than rejected. Same class of bug as ADR-148, from the opposite direction.
- The tool generalises to any Standard Life plan of this shape; nothing about the owner's fund names or dates is hardcoded. It has been exercised on exactly one statement.
