# ADR-038 — Banktivity CSV import trusts the amount sign over the Type column

**Date:** 2026-06-07
**Status:** Accepted
**Related:** ADR-021 (Generic CSV column-mapping wizard — different file shape; this ADR is about the Banktivity-detected path only). Supersedes the implicit "Type column is authoritative for Deposit / Withdrawal" rule the v0.1 importer carried forward.

---

## Context

Banktivity's CSV export has three Type values for non-investment rows: `Deposit`, `Withdrawal`, `Transfer`. The v0.1 importer treated all three as direction-encoding names — `Deposit → credit`, `Withdrawal → debit`, `Transfer → debit` — and the Amount column was always read as a magnitude, regardless of sign. Two real Banktivity exports the owner pulled in invalidated that model in opposite ways:

1. **`bedford_house.csv`** (UK property history, 2003–2024). All Withdrawal rows export with explicit negative signs (`-£5,000.00`); the Opening Balance seed row exports as `Type=Transfer` with a positive amount (`£123,000.00`). The "Transfer → debit" shortcut silently inverted the opening balance, producing a `-£123,000` row instead of `+£123,000`.

2. **`ally_savings.csv`** (US savings history, 2013–2026, ~220 rows). All Deposit rows have positive amounts; all the Transfer rows split between positive (inbound) and negative (outbound) signs — exactly matching the sign convention. The file contains **one** Withdrawal row exported with a positive amount (`Withdrawal,Reconciled,10/29/25,Ally,Chase Checking,"$45,000.00"`). The owner identified this as a Banktivity mis-tag: the row is logically a deposit, was entered into Banktivity as `+$45,000`, but ended up tagged Withdrawal. With the v0.1 importer trusting the Type column, the row landed as `-$45,000`, throwing the running balance off by exactly `2 × $45,000 = $90,000`.

The Transfer side was fixed first (see prior amendment to the parser). This ADR generalises the rule across all three Types: the Amount column's sign is authoritative; Type is informational.

The pattern across both files is consistent: **Banktivity exports signed amounts for every Type**. That makes the sign the single source of truth. The v0.1 rule of trusting Type was an oversimplification that happened to be correct for cleanly-tagged Withdrawal rows (where sign and type agreed) and silently wrong for everything else — Opening Balance seeds, mis-tagged rows, and (already fixed) Transfer pairs.

This decision matters now because the owner is about to import 19 years of US checking-account history from Banktivity. Walking through the file row by row to fix Type mis-tags is not viable; the import has to do the right thing on a single pass.

---

## Options considered

### A — Keep trusting Type for Deposit / Withdrawal; force the user to correct each mis-tag in the register after import.

Status-quo for those two Types. Pros: simplest code; doesn't risk silently inverting any row whose sign disagreed with intent. Cons: every mis-tagged row arrives wrong, and on a 19-year file the user can't audit every row. The Bedford House opening-balance case shipped silently inverted; the owner only noticed because the headline balance was negative. On a real working account, a single inverted row throws every downstream running balance off by 2× the row's value and isn't visible at a glance. Rejected.

### B — Trust the sign as the authoritative source of direction; treat Type as informational.

Sign-based direction across all three Types. Matches the empirical evidence — Banktivity exports signed amounts for everything; sign and type agree on cleanly-entered rows; sign reflects user intent on mis-tagged rows.

**Risk analysis** of where this could be wrong: a Banktivity user enters a withdrawal of $50, Banktivity stores it as `-$50` internally and exports `Withdrawal,...,-$50`. Sign and type agree, sign-based rule gets it right. A user enters a deposit of $50 as `+$50`, Banktivity stores `+$50`, exports `Deposit,...,$50`. Sign and type agree, sign-based rule gets it right. A user enters a withdrawal but accidentally selects "Deposit" type before saving — Banktivity stores whatever the user committed; the export reflects the actual stored row. The sign tells the truth either way.

The case that would break sign-based: Banktivity exports a Withdrawal as magnitude-only (no sign) when the user clearly meant a withdrawal. The two real-world files we have show Banktivity doesn't do this — Withdrawals export with negative signs when the user typed them as withdrawals. The ally `$45,000.00` row is the exception that proves the rule: it's positive *because* the user actually meant a deposit. The Type column is the lie there, not the sign.

**Selected**.

### C — Add an import-time toggle "Trust amount sign over Type column", defaulting OFF.

Per-import opt-in. Pros: backwards-compatible; doesn't surprise anyone. Cons: turns a parser bug into a user-facing decision the user shouldn't have to make on every import. The right answer is always the same — trust the sign — so making it a checkbox is just friction. Imports with cleanly-tagged Banktivity data behave identically whether the toggle is ON or OFF (because sign and type agree on those rows); imports with mis-tagged data either need the toggle (and the user has to know to flip it) or just need the parser fixed. Rejected.

### D — Detect Type-vs-sign disagreement at import time and surface a preview dialog for the user to confirm each row.

Most cautious. Pros: silent inversion is impossible. Cons: on a 19-year file with dozens of edge cases, the user is back to row-by-row review. The information the dialog would surface ("Type says X but sign says Y") is exactly what the sign already encodes — the dialog adds a step without adding signal. We don't have a separate truth source to disambiguate. Rejected. The compromise: log the disagreement at INFO level so the warning is in the import log without blocking the flow. A future "review my last import" surface can surface the count in-app.

---

## Decision

**Adopt Option B.** The Banktivity CSV parser's direction-resolution step trusts the amount sign for all three Types (`Deposit`, `Withdrawal`, `Transfer`). The Type column is informational — kept on the row dict for callers that want to surface it (e.g. a future "Type mismatch" report) and logged at INFO when it disagrees with the sign, but it does not drive direction.

Mechanics:

- `_normalise_banktivity_row` reads the inferred direction from `_parse_banktivity_amount` (which returns `("debit", "credit")` based on the sign) and uses that as `tx_type` unconditionally.
- When `row_type.lower() == "deposit"` and the inferred type is `"debit"`, or `row_type.lower() == "withdrawal"` and the inferred type is `"credit"`, a single-line `logger.info` records the mismatch with the row's amount string. Multiple mismatches in one import produce one log line each — no per-row dialog, no summary count surfaced to the UI yet.
- Existing imports already in the database are untouched. Affected rows can be corrected via the inline Amount editor (added earlier today) or by deleting the row and re-importing.

This ADR does not change anything outside the Banktivity-detected path. Generic CSVs (column-mapping wizard) still infer direction from the explicit amount sign (already correct); credit-card-format CSVs still use `debitCreditCode` (already correct).

---

## Consequences

### Positive

- **One pass gets the import right.** No row-by-row reconciliation after a Banktivity import; the sign always tells the truth.
- **Same rule across Types.** `Deposit`, `Withdrawal`, `Transfer` all use the same direction-resolution; no special-casing the parser has to maintain.
- **Aligns with the Transfer fix.** The Transfer-side sign-trust shipped earlier was a partial application of this rule. Generalising removes the inconsistency.

### Negative / trade-offs

- **A genuinely unsigned Banktivity export would mis-import.** If a future Banktivity version exports magnitudes only and relies on the Type column to encode direction, every Withdrawal would import as a credit. The two real-world files we have show this isn't current Banktivity behaviour, but a parser regression-test against a third-party export format is worth setting up if the import surface grows.
- **Type-vs-sign mismatches are logged, not surfaced.** A user who has many mis-tagged rows in their Banktivity data won't see a count in the import-result status bar. The mismatches go through correctly (the sign is right) but the user doesn't know their Banktivity data was inconsistent. A "see import warnings" verb in a future Import History dialog would close that gap; deferred.

### Ongoing responsibilities

- **Any future Banktivity parser change must preserve the sign-is-authoritative rule.** The comment in `_normalise_banktivity_row` explains why and points here; a future contributor who re-introduces a Type-based shortcut for "simplicity" would silently re-introduce the Bedford House and Ally cases.
- **The same rule should apply to any future Banktivity-derived format** (e.g. a Banktivity QIF export). The pattern of "Type is informational, sign is authoritative" is the contract.
