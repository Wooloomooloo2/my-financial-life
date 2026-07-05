# ADR-133 — Retire the standalone Payee report

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-066 (the Payee report, now retired). ADR-068 (Category & Payee — the surviving home for by-payee spending). ADR-039 (saved reports / New Report dialog). ADR-075 (home dashboard — the Top Payees card).

## Context

Owner report during a reports cleanup: the standalone **Payee** report (ADR-066) "has no functionality beyond the Category & Payee report." That's accurate — Category & Payee (ADR-068) has a **Group by: Payee** toggle that ranks spending per canonical payee with the exact same chart (`PayeeChart`), ranking (`payee_report.build_report`), drill-to-transactions, currency handling, and filters. The two windows are ~90% duplicated; the only thing Payee does that Category & Payee didn't was *be the default view*, which the toggle covers.

I confirmed on disk that **no saved reports of type `payee` exist** in the live `mfl_dev.mfl` (or its `prepayeecleanup` backup) — so retiring the report needs no saved-report migration.

## Decision

Delete the standalone Payee **window** and its **filter dialog**; route every entry point to Category & Payee.

- **Deleted:** `mfl_desktop/ui/payee_report_window.py`, `mfl_desktop/ui/payee_filter_dialog.py`.
- **Kept (shared, not Payee-specific):** `reports/payee_report.py` (`build_report`) and `ui/payee_chart.py` (`PayeeChart`) — both are used by the Category & Payee window; deleting them would break it.
- **`register_window`:** dropped the `PayeeReportWindow` import, the **Reports ▸ Payee…** menu action, `_on_payee_report`, and the `TYPE_PAYEE` branches in `_open_bare_report` / `_open_saved_report`. The home dashboard's `payee_report_requested` (the **Top Payees** card, ADR-075) now opens **Category & Payee** instead — a payee-capable report one toggle away from the by-payee view.
- **`new_report_dialog`:** removed `TYPE_PAYEE` from the offered types, so no *new* Payee reports can be created.
- **`reports/filters.py`:** `TYPE_PAYEE` + `PayeeReportFilters` are **left registered** (harmless): the type still round-trips through `filters_from_json` and stays in the `report` CHECK constraint, so a stray saved row from another file parses rather than erroring. No migration, no constraint rebuild.

Rejected: a full type removal (rebuild the `report` CHECK constraint + migrate rows) — pure churn with zero saved rows and a live cross-file format to keep stable; keeping the Payee window as dead code — the whole point was to cut the duplication.

## Consequences

- One fewer near-duplicate report to maintain; by-payee analysis lives in Category & Payee (which ADR-134 enriches the same session).
- The Reports menu and New Report dialog are shorter; the Top Payees home card still lands somewhere sensible.
- A hypothetical saved `payee` report from an external file won't open (falls to the "not openable" path) but also won't crash — an acceptable edge given none exist.
- Verified headless: `register_window` + `home_view` import clean, no dangling `TYPE_PAYEE` / `PayeeReportWindow` references, `new_report_dialog` no longer lists `payee`, full self-running test suite green.
