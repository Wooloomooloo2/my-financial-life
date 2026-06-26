# ADR-110 — Reinvested dividends render as their own series in the Income report

**Date:** 2026-06-26
**Status:** Implemented (2026-06-26).
**Builds on:** ADR-088 (Income Over Time report), ADR-089 (reinvested dividends folded into income, valued qty×price), ADR-086 (investment rows carry a ledger category).

---

## Context

The Income Over Time report has a toggle that folds reinvested-dividend (DRIP)
distributions into income. DRIPs pay no cash — they buy more shares — so they're
invisible to a cash-based income report unless opted in; the toggle values each
at quantity × price and adds it.

The owner reported it as broken: filtering the report to a category (e.g.
"Interest Inc") and enabling the toggle changed nothing. Investigation showed the
behaviour was technically correct but deeply confusing:

- Reinvested dividends are **tagged with a cash income category** (the owner's
  data: all 143 DRIPs under "Dividend Income"). The toggle added them **into that
  category's bar**, so they were invisible as a distinct quantity, and a category
  filter that excluded "Dividend Income" (e.g. "Interest Inc") dropped them
  entirely — the toggle silently did nothing.
- The label "Include reinvested dividends" read as "include dividends," reinforcing
  the confusion.

Owner's call: the toggle should **show reinvested dividends as their own legend
series**, not merge them into another category's bar.

## Decision

1. **Rename** the toggle to **"Show Reinvested Dividends"** (filter dialog),
   with a tooltip explaining it surfaces a distinct series independent of the
   category filter.
2. **Render reinvested dividends as a dedicated series.** The repository tags the
   DRIP rows (`income_aggregates` → `_reinvested_income_rows` adds
   `"reinvested": True`). The report routes those rows to a synthetic group —
   `REINVESTED_GROUP_ID = -100` (negative so it never collides with a real
   category id or `UNCATEGORISED_ID = 1`) labelled **"Reinvested Dividends"** —
   instead of their tagged category's bucket. So cash "Dividend Income" and
   reinvested distributions now show as **separate bars / legend entries**.
3. **Independent of the category filter.** The synthetic series isn't a real
   category and isn't in the category picker, so the picker doesn't gate it — the
   "Show Reinvested Dividends" checkbox is its sole visibility control. It still
   honours the account / date / payee filters (applied in SQL). This means the
   toggle always produces a visible effect when DRIPs exist in range, fixing the
   "does nothing" surprise. The synthetic group is non-drillable (guarded in
   `_on_segment_clicked`, like the Uncategorised sentinel).

### Why independent rather than category-scoped
Keeping it tied to its tagged category would reproduce the original confusion
(no effect when filtered away from "Dividend Income"). A dedicated, always-on
series matches the literal label and the owner's request, and reads cleanly:
selecting "Dividend Income" now shows cash dividends and reinvested dividends as
two distinct bars rather than one merged total.

## Consequences

- Income totals are unchanged; the reinvested portion is just **split out** of its
  cash category into its own series (owner data: Dividend Income £128k → £60k cash
  + £68k Reinvested Dividends).
- Spending report unaffected (no reinvested concept; rows never carry the tag, and
  `dict.get` on the absent key is a no-op).
- Colour is positional (the existing `colour_for(index)` palette), so the series
  picks up a distinct colour with a matching legend chip automatically.

## Verification

- `python -m compileall mfl_desktop` clean.
- `tests/test_income_reinvested_series.py` (offscreen Qt, against the public demo):
  series present when on, absent when off, present even under a non-dividend
  category filter, synthetic id negative.
- Verified end-to-end against the owner's real file (read-only copy): all-income,
  Interest-Inc-filtered, and Dividend-Income-filtered views all render the
  "Reinvested Dividends" series at £68,068.54 distinctly from cash income.
