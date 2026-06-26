# ADR-111 — QIF cash-ledger import (Bank / Credit-Card / Cash / Oth)

**Date:** 2026-06-26
**Status:** Implemented (2026-06-26).
**Amends:** ADR-043 (QIF parser — investment round 1).

---

## Context

The owner imported a Banktivity QIF export of a credit card ("REI Master Card",
~1,200 transactions). The import surfaced **0 transactions**:

```
QIF parse: 0 transactions, 92 securities, 243 categories
(account='REI Master Card', investment=False)
```

The QIF parser (ADR-043) was built for **investment** exports only: it dispatched
`!Account`, `!Type:Cat`, `!Type:Invst`, and `!Type:Security`, and explicitly
**skipped** `!Type:Bank` / `!Type:CCard` (the source comment said dispatch "can be
added later"). The file's 1,202 transactions lived in a `!Type:CCard` section, so
every one was dropped — while the categories and securities sections still parsed,
making the failure look partial and confusing.

## Decision

Parse QIF cash-ledger sections into the **same cash dict the import service
already consumes from CSV/OFX** — no downstream changes needed.

- `_section_for_header` maps `!Type:Bank`, `!Type:CCard`, `!Type:Cash`, and
  `!Type:Oth A|L` to a new `"cash"` section (and counts it as a recognised
  section so a header-only file isn't rejected).
- `_normalise_cash_record` reads `D` (date), `T`/`U` (signed amount), `C`
  (cleared), `P` (payee), `M` (memo), `N` (cheque/reference — folded into memo),
  and `L` (category, or `L[Account]` → transfer noted in memo, mirroring the
  investment path). Sign: negative `T` → `debit` (cash out), positive → `credit`.
- Splits (`S`/`E`/`$`) are **not exploded** into child rows this round; the record
  imports at its total `T`, falling back to the first split category so a split
  row isn't silently uncategorised.

Cash rows carry no `action` key, so the service's `is_investment =
any(raw.get("action") …)` correctly treats the file as cash and routes it through
the existing dedup (date|amount|payee hash), payee/category resolution, and
review/commit path.

## Consequences

- Bank, credit-card, and cash QIF exports now import. Verified end-to-end on the
  owner's file: **1,202 transactions committed**, balance sums to £0.00 (a
  paid-off card over 10 years), 292 payees + 44 categories resolved, and a
  re-import correctly dedupes to **0 new** rows.
- Investment QIF parsing is unchanged (regression-tested).
- Known limitation (documented): split transactions import as a single row at the
  total amount rather than as child splits — a later round can explode them.

## Verification

- `python -m compileall mfl_desktop` clean.
- `tests/test_qif_cash.py` (Qt-free, base interpreter) 6/6 — CCard parsing, debit/
  credit signs, fields + cleared status, bank transfer + cheque number, split
  fallback, and an investment-section regression.
- End-to-end staging + commit + re-import dedup verified on the real file.
