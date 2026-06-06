# ADR-031 — Hierarchical category picker via full-path labels

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-022 (Register typeahead delegates + inline category create); ADR-018 (Reports framework — checklist surface); ADR-030 (Spending Over Time rollup levels — same disambiguation problem at Leaf); ADR-013 (Category management policy — hierarchy semantics)

---

## Context

Categories are hierarchical (ADR-013), but every picker in the app currently displays them with the label `Name (ImmediateParent)` — e.g. `Tesco (Groceries)`. That hides two things:

1. **The full ancestor chain.** A user typing "Food" into the picker expects to see Food's descendants (`Food → Groceries → Tesco`, `Food → Dining out`) revealed. Today they don't, because the label `Tesco (Groceries)` contains neither the word "Food" nor "Dining out" — the QCompleter's contains-match never fires on an ancestor name.
2. **Same-named categories under different parents.** A user with both a top-level `Home and Garden` (import-created) and a `Household → Home and Garden` (user-created) sees two identical-looking rows in the picker. We hit this concretely in the merge dialog and fixed it there (`_path_for` shows the breadcrumb); the rest of the app still has the problem.

The two issues are the same root cause — the picker label doesn't carry the breadcrumb. Five surfaces use `mfl_desktop/ui/category_picker.py::make_category_picker` and inherit the issue: New Transaction dialog, Bulk Edit dialog, Schedule dialog, Budget Setup dialog, and the register's inline `CategoryTypeaheadDelegate` (ADR-022). The Spending Over Time report's category checklist has the same problem at Leaf rollup (ADR-030), where same-named leaves under different parents would land as indistinguishable rows.

`CategoryChoice` (the dataclass `make_category_picker` consumes) currently carries only the immediate parent's name. To fix the picker we need the full path.

## Options considered

### Genuine tree popup (QTreeView replacing QCompleter)

- **Pros:** Hierarchy is visually rendered as a tree. Expand/collapse browsing for users who don't know what they're looking for. Familiar pattern from file pickers.
- **Cons:** A custom QTreeView popup needs a custom model, custom sizing, custom keyboard nav (Down/Up should still flow row-to-row through the tree, not just inside the focused branch), and styling that doesn't fight Fusion. It's a different visual feel from the payee typeahead (a flat `QCompleter` popup) — inconsistency for the sake of one widget. The contains-match story gets harder: do we hide branches whose names don't match, or grey them out, or expand/collapse the tree on the user's behalf? Each answer has trade-offs and there's no clear right one.
- Rejected.

### Hybrid — flat for typeaheads, tree for checklists

- **Pros:** Each surface gets the widget that fits its task — typeaheads stay flat (matching the payee typeahead), checklists become tree-shaped for browsing.
- **Cons:** Two widget patterns to maintain. The disambiguation gain is the same — same-named leaves are distinguishable in either widget so long as the breadcrumb is in the label.
- Rejected for v1; deferable. If real use of the spending-report checklist surfaces a clear need for browsing-by-hierarchy (vs filtering-by-typing-an-ancestor), we can layer a tree variant of the checklist on top of the proxy model the flat approach will already give us.

### Flat with breadcrumbs (chosen)

The picker label changes from `Tesco (Groceries)` to `Food → Groceries → Tesco`. The QCompleter's existing contains-match-on-text rule does the rest:

- Typing "Food" matches every descendant whose path contains "Food".
- Typing "Tesco" matches the leaf, same as today.
- Same-named leaves under different parents have distinct paths and become visually distinct everywhere a picker is used.

Same separator (` → `) and same root-handling convention as the categories dialog's `_path_for` and the merge dialog's picker (which we just shipped). Visually consistent with the merge-target list users will already have seen.

One label change reaches all five picker surfaces. The spending report's checklist gets the same treatment via a shared `category_path` helper.

## Decision

### Data layer

- Add `path: str` to `CategoryChoice` — the full breadcrumb from root to leaf, joined with ` → `. Top-level categories have `path == name` (no prefix).
- `Repository.list_categories_flat` builds the path via a Python tree walk: one extra SELECT over all non-archived categories (cheap — single table, single column trio) to build a `{id: (parent_id, name)}` map, then walk the parent chain per row. The existing kind-filtered query still drives the returned set; paths are decorated onto each row.
- Sort order changes from `(parent_name, name)` to **lexicographic by path** so DFS-style sibling clustering survives — `Food`, `Food → Dining out`, `Food → Groceries`, `Food → Groceries → Sainsbury's`, then `Income → Salary`, etc.

### Picker layer

- `make_category_picker` uses `c.path` as the display label. Drops the special-case `Name (Parent)` branch — the path covers both cases (top-level rows show `name`, deeper rows show the full chain).
- `selected_category_id` is unchanged — it already matches displayed text to `itemText`, which is now the path. Free-typed text (a brand-new name the user wants to inline-create) still falls through to the ADR-022 confirm-and-create path verbatim, because the inline-create policy keys off "no exact label match," not the label format.
- `CategoryTypeaheadDelegate.setEditorData` keys by `itemData(i) == current_id`, not by label text, so the label-format change is transparent to the delegate.

### Reports layer

- New shared helper `mfl_desktop.reports.category_path(nodes_by_id, cid) -> str` — same algorithm as the categories dialog's `_path_for` (which stays local to that dialog for now; cleanup if/when convenient).
- `SpendingReportWindow._rebuild_categories_list` uses `category_path` so the checklist shows full breadcrumbs. Matters most at Leaf rollup (ADR-030) where same-named leaves under different parents would otherwise be indistinguishable.

### Separator

- ` → ` (U+2192 with single spaces). Same as the merge picker and `_path_for`. No abbreviation, no truncation in the label itself — the QCompleter's popup is wide enough; the line edit elides naturally when collapsed.

## Consequences

### Positive

- Typing any ancestor name reveals all its descendants in every picker. Addresses the explicit user complaint behind the Reports round 2 hierarchical-picker item.
- Same-named categories under different parents become visually distinct everywhere — not just in the merge dialog where we patched it tactically.
- Single repository-level change (`CategoryChoice.path` + `list_categories_flat`) reaches all five combo surfaces and any future picker built via `make_category_picker`.
- The shared `category_path` helper is the answer when a list of `CategoryNode` (rather than `CategoryChoice`) needs path strings. Eligible callers: spending report checklist, future reports, anywhere the categories-dialog `_path_for` logic might be duplicated.
- No new widget, no new popup styling, no break with the payee typeahead's flat pattern.

### Negative / trade-offs

- Deep paths get long. Combos are width-constrained; ellipsing on the line edit happens automatically but the path may be visually truncated when collapsed. Acceptable — the dropdown popup shows full text, and the QCompleter matches against the underlying string regardless of visual truncation.
- One extra SELECT per `list_categories_flat` call. The combo is rebuilt on every cell-editor open (delegate `createEditor`), so this cost lands on every inline category edit. Real-world impact: tens of microseconds at ~150-row category tables; acceptable.
- The flat list now sorts by path rather than `(parent_name, name)`. Behaviourally this is closer to what users expect (siblings cluster, top-levels lead), but the sort key changed and any code that depended on the old order would notice. Grep confirms no callers depend on the sort order — all consumers either iterate to find a match or hand the list straight to `make_category_picker`.

### Ongoing responsibilities

- Any future surface that picks categories should go through `make_category_picker` to inherit path labels for free.
- `category_path` is the canonical helper for path-from-CategoryNode. The categories dialog's `_path_for` will migrate to it when that file is next touched substantively; leaving the duplication for now keeps the diff scoped.
- If the deferred "account folders for categories" idea ever lands (it's been discussed; not on the roadmap), the path traversal logic needs to skip folder nodes the same way `category_root_map` (ADR-030) would. Track both helpers together.
- A future tree-popup variant for the spending report checklist (the "hybrid" option above) can layer on top of the same data — `CategoryChoice.path` and `category_path` give a tree consumer everything it needs. No data-model change required to upgrade.
