# ADR-141 — Rename a saved report

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-039 (saved-report framework — the sidebar rows + single-instance windows). ADR-084 (report filter dialogs). Mirrors the existing folder-rename verb (`rename_report_folder`).

## Context

Owner report: there was no way to change a saved report's name — the only option was to delete and recreate it (losing the report, its folder placement, and filters). The sidebar's report context menu had Open / Move to Folder / Delete, but no Rename. The repository already had `update_report(name=…)` (used by the report windows' Save path); it just wasn't reachable as a rename verb.

## Decision

Add **"Rename Report…"** to the sidebar report context menu (right after Open), mirroring the existing "Rename Folder…". The handler `_on_rename_report` prompts with the current name (`QInputDialog`), calls `update_report(report_id, name=…)`, and refreshes the sidebar preserving selection. If the report is currently open, its window title is updated live (all report windows share the `_loaded_name` / `_update_name_label` pattern, so a duck-typed refresh keeps the open title in sync without reopening).

Validation is entirely the existing `update_report`: a blank name raises `ValueError`; a clash **within a folder** hits the `UNIQUE(name, folder_id)` constraint and surfaces as a "Could not rename" warning. Top-level (no-folder) names may repeat — SQLite treats a NULL `folder_id` as distinct in the unique index, and `create_report` already behaves the same way, so rename is consistent with create rather than adding a new rule.

No new repository method, no schema change — this is a UI verb over existing plumbing.

## Consequences

- Reports can be renamed in place, keeping their id, folder, filters, and any open window. The report windows' own Save re-reads `row.name`, so a sidebar rename never gets reverted by a later save.
- Consistent with the folder-rename UX and the create-report name rules.
- `tests/test_report_rename.py` 4/4 (rename updates the name; type + filters preserved; blank rejected; in-folder clash rejected). Full suite 31/31.
