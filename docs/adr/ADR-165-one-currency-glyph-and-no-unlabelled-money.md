# ADR-165 — One currency glyph, and no unlabelled money

**Date:** 2026-07-13
**Status:** Implemented
**Related:** ADR-159 (`chart_helpers.currency_symbol()` — declared "the single definition of the glyph"; this ADR makes that true). ADR-161/162/163/164 (the same design review). ADR-084 (consolidate divergent duplicates).

## Context

The design review flagged the sidebar for showing `US Brokerage $17,035.62` next to `£19,041.85` "with nothing marking it as a different unit". **That framing was wrong** — the `$` *is* the marker, and the sidebar was doing the right thing for USD. Worth saying plainly, because pulling the thread found something considerably worse.

`Repository`/`chart_helpers` gained `currency_symbol()` in ADR-159, whose docstring says:

> *"One definition, so a report can't disagree with a chart about what a dollar looks like."*

It was not one definition. **Sixteen** modules in `mfl_desktop/ui/` carried their own private copy of the currency table — `_CCY_SYMBOLS`, `_CURRENCY_SYMBOLS`, `_SYM`, `_SYMBOL` — and they did not agree with each other:

- **Most returned an empty string for a currency they didn't know.** So a **CHF, CAD, AUD, SEK…** balance rendered as a bare `1,234.00` — *with no currency marker at all*, in a sidebar column that also holds sterling. That is not a cosmetic defect. It is an amount whose unit the user cannot see, sitting next to amounts in a different unit.
- **Four of them were missing JPY entirely** (`transfer_chips`, `reconcile_wizard`, `statements_window`, `transfer_destination_dialog`), so a yen transfer chip read `JPY 500.00` while the identical amount read `¥500.00` two screens away.
- Only three of the sixteen had the code-prefix fallback that `currency_symbol()` has had all along.

The defect register already carried this as a *consistency* nit — "the currency-symbol map is duplicated in `home_view` and `sankey_report_window`". It was duplicated in **sixteen** modules, and in most of them it was a correctness bug, not a tidiness one. Same shape as ADR-159 itself: a wrong-number bug filed as cosmetics.

## Decision

**`chart_helpers.currency_symbol()` is the only currency table in the app.** All sixteen private copies are deleted; every call site delegates.

The fallback is the whole point: an unknown code returns `"CHF "` (code + space), never `""`. **Money is never printed without its unit.** A row with no currency at all — a mixed-currency folder total — still gets no symbol, because there genuinely isn't one to give; it must *not* be stamped with a `£` it hasn't earned.

The **`REPORTS` sidebar header is hidden when it has nothing under it.** On a file with no saved reports it was drawn anyway, leaving a bare caption dangling at the bottom of the sidebar with nothing beneath it — which reads as a section that failed to load. A report *folder* with no reports still counts as content.

## Rejected

- **Adding the missing currencies to each of the sixteen tables.** Sixteen places to keep in sync forever, and the seventeenth window would copy whichever it found.
- **Making `currency_symbol("")` return `"£"` for callers with no currency.** It already does this for an empty code — a GBP assumption that is fine as a last resort inside a chart, and *not* fine on a sidebar row, where it would silently label an unknown balance as sterling. Call sites guard with `if currency else ""` instead. The GBP default is now the only sharp edge left in this function, and it is deliberate.

## Consequences

- Any currency outside GBP/USD/EUR/JPY now renders as `CHF 1,234.00` across the whole app instead of a bare `1,234.00` in most of it. **This changes what is on screen for anyone holding an account in a fifth currency** — for the better, and it is the point of the change.
- Yen amounts are consistent app-wide for the first time.
- 16 modules lost a constant; one gained authority.
- The `REPORTS` header disappears on a file with no reports. It reappears the moment one is saved.

## Known limitation — this does not fix the folder sums

The sidebar's **folder totals are still a naive sum across currencies** (a folder holding a GBP and a USD account shows an arithmetically meaningless number). That is a *different* bug — it is in the defect register, it is the ADR-159 class (`SUM` without conversion), and it needs FX conversion at the folder level, not a glyph. **Marking the unit correctly on each row does not make the total beneath them mean anything**, and it would be easy to mistake this ADR for having addressed it. It has not.

`tests/test_currency_symbol_single_definition.py` 8/8. The one that matters is a **source scan**: it greps every `ui/*.py` for a currency→glyph dict literal and fails if any module other than `chart_helpers` defines one — so it stops the seventeenth copy. (It earned its keep immediately: it caught four tables that a grep for the two known variable names had missed.) Full suite 331/331. No schema change.
