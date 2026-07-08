# ADR-143 — Reports resolve archived categories (no more "id=N" rows)

**Date:** 2026-07-08
**Status:** Implemented
**Related:** ADR-070 (`list_category_tree(include_archived=…)` — archived categories). ADR-134 (Category & Payee rollup levels). ADR-068 (Category & Payee report). ADR-056/064 (Sankey / Income & Expense composition).

## Context

Owner report: the Category & Payee report showed a top row labelled **"id=168"** (£14,815) instead of a category name. Category 168 = **"Legal and Closing Costs"** — an *archived* (soft-deleted 2026-06-26) expense category whose transactions still reference it (10 whole txns + 1 split line, −£72.6k). Archiving a category does **not** reassign its historical transactions (ADR-070), so a report over that history still meets category 168.

The report windows built their category **name / rollup maps** from `repo.list_category_tree()` — which excludes archived categories by default. So an id referenced by transactions but missing from the map fell through the label helper's `f"id={id}"` fallback and never rolled up to its parent. This affected **all four** category-aware report windows (Category & Payee, Spending Over Time, Income & Expense composition, Sankey).

## Decision

Build each report's **display name / rollup maps** from `list_category_tree(include_archived=True)`, so a since-archived category resolves to its name and rolls up to its (live) parent group. The **filter picker** keeps the live-only list — you filter by categories you still use, but historical rows under an archived category are still named correctly.

- **Category & Payee** — its `_all_categories` was display-only (the filter dialog builds its own picker), so it simply switches to the archived-inclusive tree.
- **Spending Over Time / Income & Expense / Sankey** — these pass their category list to the filter dialog, so they keep `_all_categories` (live-only) for the picker and add a separate archived-inclusive tree (`_display_categories` / `display_nodes`) for the `id→node`, `parent→children`, and rollup maps.

The change is purely additive to the maps (archived ids gain entries; live ids are unchanged), so existing behaviour for live categories is untouched. No schema change.

Rejected: reassigning an archived category's transactions to Uncategorised on archive (destroys history, and Uncategorised is worse than the real name); showing archived categories in the filter pickers too (clutters the picker with dead categories — you generally don't want to *filter* new reports by them, only to *read* old rows correctly).

## Consequences

- Historical rows under archived categories now show their real name and roll up correctly in every report — "id=168" becomes "Legal and Closing Costs". This is common after an import-driven category cleanup, where categories get archived while their transactions remain.
- Filter pickers are unchanged (live categories only), so no new clutter.
- `tests/test_report_archived_category.py` 2/2 (archived category excluded from the live tree but present with `include_archived`; the archived-inclusive maps resolve its name and roll it up to a real named bucket at both group and top level, landing on the live parent). Full suite 33/33.
