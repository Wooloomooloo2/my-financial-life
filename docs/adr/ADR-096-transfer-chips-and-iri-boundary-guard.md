# ADR-096 — Shared transfer-strength chips (P3b) + an MFL↔MRL IRI-boundary guard test (M1)

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-036 (transfer matching — inline confirm/picker), ADR-037 (Reconcile Transfers dialog), ADR-026 (theme palette), ADR-084 (de-overlap rule: consolidate duplicates, keep distinct affordances), ADR-005 / ADR-006 (MRL ontology contract + cross-app IRIs), `RELEASE_1.0_BACKLOG.md` items **P3b** and **M1**.

---

## Context

Two loose ends from the 1.0 backlog's engineering-led Phase 1, bundled into one commit because each is small and self-contained.

**P3b — the last de-overlap candidate.** The two transfer-matching surfaces each carried a private copy of the same three helpers:

- `_CHIP_COLOURS` — the Strong/Good/Possible → pill-colour map (blue-600 / amber-500 / slate-500, from ADR-026's palette).
- `_fmt_amount(value, currency)` — the `£500.00` / `$1,000.00` / `EUR 12.34` formatter.
- a strength-chip builder — `transfer_match_dialogs._strength_chip` (a `QLabel` pill) and `transfer_reconcile_dialog._strength_chip_widget` (the same pill wrapped in a centred holder for table cells).

Byte-for-byte duplicates across `transfer_match_dialogs.py` and `transfer_reconcile_dialog.py`. Nothing forced them to stay in sync — add a currency symbol or retune a strength colour on one surface and the other silently drifts. ADR-084 established the rule for exactly this: **consolidate divergent duplicates of the same thing; never prune distinct affordances.** The two dialogs' *row layouts* are genuinely different (a confirm card vs. a one-candidate combo vs. an A/B reconcile grid) and stay bespoke; only the shared primitives move.

**M1 — guard the IRI boundary.** Accounts and the Person carry **`mrl:`-namespaced** IRIs (`mrl:CashAccount_1`, `mrl:Person_1`); everything MFL owns privately (transactions, transfers, schedules, budgets, reports, securities, …) uses **`mfl:`**. That `mrl:` prefix is the *join key* My Retirement Life matches MFL's exported entities on (workstream M; the file-based RDF export ships in 1.1, but 1.0 must "preserve the IRI boundary … add a guard/test so a future refactor can't silently change the prefix"). Today the boundary is enforced only by convention in three places — `Repository._next_account_iri` (mints `mrl:<class>_<n>`) and the two first-file seed sites (`__main__._seed_starter_db`, `cli.cmd_init`, both inlining `mrl:Person_1` + `mrl:CashAccount_1`). A rename there would break MRL's match with no failing test to catch it.

---

## Decision

**(1) Extract `mfl_desktop/ui/transfer_chips.py` (P3b).** One Qt module holds the shared primitives:

- `CHIP_COLOURS` — the strength→colour map (light hex values unchanged).
- `fmt_amount(value, currency)` — the amount formatter (identical output to both prior copies).
- `strength_chip(strength) -> QLabel` — the bare pill (used inline by the confirm dialog).
- `strength_chip_holder(strength) -> QWidget` — the pill in a centred, left-hugging holder (used by the picker and reconcile table cells via `setCellWidget`).

Both dialogs import these (aliased to their old private names — `from … import fmt_amount as _fmt_amount, strength_chip as _strength_chip` — so the existing call sites are untouched and the diff stays minimal). The now-dead local definitions and their now-unused imports (`QSizePolicy`, `QPalette`, `Decimal`, the orphaned `_fmt_rate`) are removed.

The chip colours stay **literal hex**, not theme tokens: the pill is a coloured background with white text, readable in both light and dark themes by construction (ADR-076) — it never needs to invert, so threading it through `tokens.c(...)` would add machinery for no visible change.

**(2) Add `tests/test_iri_boundary.py` (M1).** A Qt-free guard (runs on the base interpreter and under pytest) asserting the boundary from both directions:

- every account family mints `mrl:<ClassName>_<n>`, with the class segment equal to the type's declared `class_name`, and **never** `mfl:`;
- per-class numbering increments and is class-scoped;
- `_next_account_iri` always returns the `mrl:` prefix;
- an account is queryable by its IRI (`get_account_by_iri` round-trips — the exact lookup MRL performs);
- the MFL-private minters (`new_transaction_iri`, `new_transfer_iri`, …) all stay in `mfl:` and never leak into `mrl:`;
- the first-file seed (`mrl:Person_1` + `mrl:CashAccount_1`) is pinned at **both** seed sites — checked at the source-text level so the test needs no Qt (the GUI `__main__` imports PySide6 at load).

This is the project's first file under `tests/`. It carries a bare-script runner (`python3 tests/test_iri_boundary.py` prints PASS/FAIL per case) so it fits the project's "verified offscreen, no pytest installed" reality while still being a discoverable `test_*` module for a future pytest run.

---

## Alternatives considered

- **Leave the chips duplicated.** Rejected — it's the explicit open P3b item, and ADR-084 already committed the project to killing same-thing duplicates.
- **Also unify the two dialogs' row layouts.** Rejected — they're distinct affordances (single-confirm vs. ranked picker vs. A/B reconcile grid), and ADR-084's rule is to keep those. Only the leaf primitives are shared.
- **Make the chip colours theme tokens.** Rejected as needless — white-on-colour pills already read in both themes; no dark value differs.
- **Guard the IRI boundary by importing and running the seed functions.** Rejected for the seed check — `__main__`/`cli` pull in PySide6, which would force the guard onto the Qt interpreter. A source-text assertion keeps it on base `python3` and still fails loudly if either seed site flips the prefix. The minting path *is* exercised live (it's Qt-free via `Repository`).

---

## Consequences

- The two transfer surfaces can no longer drift on chip colour or amount formatting — both read one module. A future third surface gets the same primitives for free.
- The MRL join key is now regression-protected: any refactor that flips the account/person prefix, breaks the `mrl:<Class>_<n>` shape, or lets a private entity into the `mrl:` space fails the guard. Verified the guard catches a simulated `mrl:`→`mfl:` regression (it isn't vacuously passing).
- Closes **P3b** (the last de-overlap candidate; only P3c's affordance-audit remained and was already confirmed no-change in ADR-084) and the test half of **M1**. P3 is now complete.

---

## Verification

- `py_compile` clean on the three touched UI files + the new module.
- Offscreen Qt: `transfer_chips` primitives (amount formatting incl. fallback, both chip builders); both inline dialogs (`TransferMatchConfirmDialog`, `TransferMatchPickerDialog`, `BulkTransferReviewDialog`) construct and render their chips; `TransferReconcileDialog` builds against the real public-demo Repository; full app import OK; alias wiring asserted (`md._fmt_amount is transfer_chips.fmt_amount`, etc.).
- Base `python3`: `tests/test_iri_boundary.py` → 6/6 pass; negative check confirms it fails on a simulated prefix regression.
