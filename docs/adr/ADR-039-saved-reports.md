# ADR-039 — Saved reports: schema, sidebar section, and per-type filter persistence

**Date:** 2026-06-07
**Status:** Accepted (round 1 shipped 2026-06-07)
**Related:** ADR-018 (Reports framework + Spending Over Time — first type to gain persistence); ADR-019 (Net Worth — second target); ADR-015 (Account folders — the sidebar-folder pattern this ADR reuses for a new section); ADR-030 (Spending rollup levels — one of the persisted filter dimensions); ADR-033 (Per-account summary — separate per-account window, not modelled as a saved report — accounts already are the entity here).

---

## Context

Reports today are stateless surfaces. The user opens Reports → Spending Over Time, configures a date range / category filter / rollup level inline, and the result evaporates when the window closes. For a personal-finance app run weekly, the same five-or-six configurations get rebuilt repeatedly. Banktivity solves this by treating each saved report as a first-class object in the left-hand sidebar, alongside accounts, organised into folders. The owner has explicitly asked for the same shape and named the first four report types they want as targets: Spending Over Time, Net Worth, Income & Expense, Sankey.

This ADR is the *planning* document for the saved-reports arc. It locks:

- The storage model (one `report` table per report instance, with per-type JSON filters; a `report_folder` table mirroring the existing account-folder pattern).
- The sidebar layout shape (new "Reports" section below the existing "Accounts" section, separated by a visible rule).
- The save / load / edit / delete vocabulary on the report window itself.
- The Reports menu in the menu bar stays as the "open a bare report" verb list — saved-with-state instances live in the sidebar.

It does *not* lock the per-type filter schemas beyond Spending Over Time (round 1). Each subsequent type lands as its own round, with its filter set chosen against the actual report needs at that point — ADR-040 et seq. land per type. The data model is deliberately type-stable: adding Net Worth or Sankey persistence is additive (new `type` enum value + new JSON schema) without migration.

This decision matters now because the alternative — fork the sidebar widget per new section type, or stuff report state into per-window QSettings — both produce technical debt that compounds with every new report. The save-shape is a small additive schema move; the sidebar shape is a UI-tree refactor that's easier to do once for two sections than twice (once for accounts, again for reports). The owner's stated direction ("we're going to clean up the sidebar a lot") makes settling the section pattern early the right call.

---

## Options considered

### Storage shape

- *Per-type tables* — `spending_report`, `net_worth_report`, `income_expense_report`, `sankey_report`, each with its own columns. Pros: typed columns; easy queries like "all reports filtering on category X". Cons: schema duplication for the shared bits (name, folder_id, iri, created_at), and a migration every time a new type lands or a filter dimension is added. Cross-type listing in the sidebar needs a UNION query. Rejected — premature normalisation for an entity whose shared shape is "name + type + filters".
- *Single table with per-column filters covering every report type's union* — wide table with nullable columns. Cons: a few-dozen-column table whose columns only make sense for one type each; ugly. Rejected.
- **Single table with a JSON blob for filters** (chosen): `report(id, iri, name, type, folder_id, filters_json, created_at)`. `type` is an enum (`spending_over_time`, `net_worth`, `income_expense`, `sankey`); `filters_json` is `TEXT` parsed per-type at read time. Pros: schema doesn't churn when filter set changes within a type; new types are additive (one new enum value, no migration); cross-type sidebar listing is one SELECT. Cons: the per-type filter schema lives in code, not in the DB — refactors that change filter shape need a migration of the JSON blobs. Mitigated by a small `mfl_desktop/reports/filters.py` module that owns the per-type schema + a `migrate()` function for blob-shape changes when they happen.

### Folder model

- *Reuse `account_folder`* — store reports + accounts in the same folder tree. Cons: weird semantics (an account folder named "Investments" containing both an account *and* a report?), and the folder UI gets a "type" column. Rejected — folders are namespaced by the section they belong to.
- **Per-section folder table** (chosen): new `report_folder(id, name, sort_order, created_at)` mirroring `account_folder`. Same CRUD verbs (create / rename / move up-down / delete-with-reparent). Trade-off: small amount of duplicated repository code; acceptable for two sections; revisit if a *third* section type appears with the same shape.

### Sidebar layout

- *Tabs at the top of the sidebar (Accounts / Reports)* — only one section visible at a time. Rejected — the owner wants Banktivity's both-visible layout so totals + saved-report names live in peripheral vision simultaneously.
- *Reports in the menu bar only* — current shape. Rejected explicitly by the owner.
- **Vertical sections separated by a visible rule** (chosen): Accounts on top (existing tree + "All transactions" entry), horizontal separator line, Reports section underneath (Reports folders + report rows). Both sections scroll together if the combined height exceeds the sidebar; both support context-menu CRUD. This shape extends — future sections (saved budgets, schedules-as-list, watchlists) drop in below Reports with their own separator.

### Save / Save As semantics on the report window

- *Single Save button that always creates a new row* — append-only. Pros: simple. Cons: clutters the sidebar with near-duplicate reports.
- *Auto-save on every filter change* — every tweak persists. Pros: no "did I save?" friction. Cons: accidental edits stick; can't sketch a variant without committing.
- **Save + Save As** (chosen): Save updates the current report's `filters_json` and `name` if changed. Save As prompts for a new name + folder and creates a fresh row. The bare-window flow (Reports menu → "Spending Over Time") starts unattached and the Save button reads "Save As…" until the report has been named; once saved/loaded, the button reads "Save" and a "Save As…" option lives next to it.

### How does the report window know which saved-state it's in?

- *Pass the entire filters_json* — opaque to the caller. Cons: the window can't update the source row.
- **Pass the report id (or None for a bare open)** (chosen): the window holds `self._report_id: Optional[int]`. The top of the window shows the report's name (and folder breadcrumb if non-root) when set; "Untitled Spending Over Time" when None. Save uses the id to overwrite; Save As writes a new row and updates `self._report_id` to that new id.

### Sidebar reuse vs. rewrite

- *Add a second `QTreeWidget` below the existing one* — two trees, one per section. Cons: separate selection models; clicking the Reports tree doesn't deselect an account; layout is two scrollables stacked. Rejected — feels wrong even at this small scope.
- **One `QTreeWidget` with two top-level groups** (chosen): the sidebar widget gets two top-level "group nodes" (Accounts, Reports), each non-expandable / unselectable, separated visually by a thin slate-200 rule (rendered via the group node's paint or via spacing). Folders + leaf rows live underneath their group node. Selection is unified — clicking a report deselects any account, and vice versa. The existing `AccountSidebar` becomes `Sidebar` and grows the second section behind a feature flag for the initial implementation so the refactor can land without touching the register window's selection plumbing.

### Reports menu in the menu bar — keep or remove?

- *Remove* — saved reports in sidebar; no top-level Reports menu. Cons: "I just want to look at spending right now" loses the muscle-memory verb. Saved-state-required-for-everything is friction.
- **Keep, narrowed scope** (chosen): the Reports menu lists report *types* (Spending Over Time, Net Worth, Income & Expense, Sankey). Clicking opens the bare report window unattached to a saved row. Saving from there creates a row in the sidebar. The dichotomy: menu = verbs (no-state-required), sidebar = entities (saved-with-state).

---

## Decision

### Schema (migration 0010_reports.sql)

```sql
CREATE TABLE report_folder (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE report (
    id          INTEGER PRIMARY KEY,
    iri         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL
                CHECK (type IN (
                    'spending_over_time',
                    'net_worth',
                    'income_expense',
                    'sankey'
                )),
    folder_id   INTEGER REFERENCES report_folder(id) ON DELETE SET NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, folder_id)
);

CREATE INDEX idx_report_folder ON report(folder_id);
```

IRIs follow the existing convention (`mfl:Report_<uuid8>`, `mfl:ReportFolder_<uuid8>`). Name uniqueness is per-folder, matching the account / account-folder pattern.

### Per-type filter schemas (Round 1: Spending Over Time)

Spending Over Time's filter shape lives in `mfl_desktop/reports/filters.py::SpendingOverTimeFilters`. Frozen dataclass:

```python
@dataclass(frozen=True)
class SpendingOverTimeFilters:
    period_key: str                     # "quarter" | "6m" | "ytd" | "1y" | "3y" | "custom"
    custom_start: Optional[str] = None  # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    granularity: str = "auto"           # "auto" | "weekly" | "monthly" | "quarterly" | "annually"
    rollup_level: str = "top"           # ADR-030: "top" | "group" | "leaf"
    category_ids: tuple[int, ...] = ()  # empty == all (incl. uncategorised by default)
    include_uncategorised: bool = True
    payee_ids: tuple[int, ...] = ()     # empty == all
    account_ids: tuple[int, ...] = ()   # empty == all in-scope accounts
    include_transfers: bool = False     # ADR-018 strict-outflow default stays off
```

The other three types' filter schemas land in their respective round ADRs. The module's contract: each type has a `<Type>Filters` frozen dataclass + `to_json()` / `from_json(s)` round-trip + `default()` constructor. The sidebar / Save / open paths read and write through these.

### Repository surface (additions)

- `list_reports(folder_id: Optional[int] = ...) -> list[ReportRow]` — when `folder_id` is the special sentinel `UNSET`, returns all reports across folders for the sidebar build.
- `list_report_folders() -> list[ReportFolderRow]`
- `get_report(report_id) -> Optional[ReportRow]`
- `create_report(name, type_key, folder_id, filters_json) -> ReportRow`
- `update_report(report_id, *, name=UNSET, folder_id=UNSET, filters_json=UNSET) -> ReportRow`
- `delete_report(report_id) -> bool`
- `create_report_folder(name) -> ReportFolderRow`
- `rename_report_folder`, `move_report_folder`, `delete_report_folder` — mirror the existing account-folder methods, including delete-reparents-children-to-root semantics.

`ReportRow` is a frozen dataclass: `id, iri, name, type, folder_id, folder_name, filters_json, created_at`. The `folder_name` is denormalised on the row for sidebar rendering.

### Sidebar restructure

- The existing `AccountSidebar` class is renamed to `Sidebar` and grows two top-level group rows: "Accounts" and "Reports", each rendered as a non-selectable, non-expandable header row (boldface, slate-700, with a thin slate-200 underline). Folders + leaves live underneath their group, indented one level less than today so the visual hierarchy makes sense.
- Selection signal carries a new discriminator: `selection_changed(kind, payload)` where `kind` is `"all_transactions" | "account" | "report"` and `payload` is the account IRI / report id depending. Existing single-arg `selection_changed(account_iri)` is replaced; one breaking change for one caller.
- Context-menu CRUD verbs split per section:
  - Accounts section: existing (New Account, Edit, Delete, Move to Folder, New Folder).
  - Reports section: New Report…, Edit Report (= rename / move folder), Delete Report, Move to Folder, New Folder.
- "All transactions" stays inside the Accounts section as its top entry (preserves the current shape).

### Report-window UX

The Spending Over Time window gains a top bar with:

- The current saved name on the left (italic, slate-500 "Untitled Spending Over Time" when unsaved). Single-click renames inline (only meaningful for saved reports; for unsaved ones the rename happens via Save As).
- "Save" button (disabled when no changes since last save / load; reads "Save As…" when the report is unsaved).
- "Save As…" button (always enabled; opens a small dialog: name + folder picker + Save).
- Filter controls underneath (existing controls, expanded per the filter schema above).

Save flow:

1. Window holds `_report_id: Optional[int]` and `_dirty: bool`.
2. Any filter change sets `_dirty = True`.
3. Save (with `_report_id` set) calls `update_report` and clears dirty.
4. Save As opens the dialog → `create_report` → updates `_report_id` and clears dirty.
5. Closing a dirty window prompts "Unsaved changes — save / discard / cancel" (only when `_report_id is not None`; an unsaved bare window discards silently).

Open flow:

1. Sidebar click on a report → emits `selection_changed("report", report_id)`.
2. Register window dispatches: if the window for that report-type isn't open, open one; pass the report's filters in.
3. If a window is already open with a *different* saved report, prompt "Save changes?" then load the new one.
4. Multiple windows of the same type with different saved reports open simultaneously is allowed — owner can do side-by-side comparisons (similar to drill-down windows).

### Reports menu (menu bar) — unchanged scope

Reports → Spending Over Time / Net Worth / Income & Expense / Sankey opens the bare window for that type with default filters and `_report_id = None`. Same window class as a saved-report open; just different entry parameters.

### Files touched (round 1)

| File | Change |
|---|---|
| `mfl_desktop/migrations/0010_reports.sql` | New — schema |
| `mfl_desktop/db/repository.py` | ReportRow / ReportFolderRow dataclasses; CRUD methods |
| `mfl_desktop/reports/filters.py` | New module — per-type filter dataclasses, round-trip helpers |
| `mfl_desktop/ui/sidebar.py` | Restructure into two sections (Accounts / Reports) with group rows |
| `mfl_desktop/ui/register_window.py` | Selection-signal handler updated; Reports menu wires saved-vs-bare open; sidebar context menu split per section |
| `mfl_desktop/ui/spending_report_window.py` | Top bar with name + Save / Save As; filter controls expanded to match schema; load-from-id path |
| `mfl_desktop/ui/new_report_dialog.py` | New — small dialog: pick type → "Save As" flow on first save |
| `mfl_desktop/ui/save_report_as_dialog.py` | New — name + folder picker |

---

## Consequences

### Positive

- **No retrofit later.** Adding Net Worth / Income & Expense / Sankey persistence is one new enum value + one new filter dataclass each; no schema change, no sidebar work.
- **Sidebar shape scales.** Future sections (saved budgets, schedules-as-list, watchlists) drop in below Reports with their own separator and folder table. The widget pattern is settled.
- **Reports menu retains its muscle-memory role.** A user who just wants "show me spending right now" still has a one-click entry; saved reports are an *additional* affordance, not a replacement.
- **Per-type filters can grow without migration.** Adding a "exclude_payees" filter to Spending Over Time after launch is a code change (the dataclass + the window), not a DB migration.

### Negative / trade-offs

- **Filter JSON is opaque to SQL.** "Find all reports that filter on category X" can't be a SQL query — it has to be a Python iterate-and-parse. Acceptable: this query isn't a near-term need; if it becomes one, a denormalised `report_filter_index` table with `(report_id, dimension, value)` rows can be added without changing the primary table.
- **Two folder tables (account_folder + report_folder) is a small duplication.** Same CRUD shape, same context-menu wiring. Per the planning, we don't unify yet; a third section type with the same pattern is the prompt to revisit.
- **One breaking-shape change on the sidebar `selection_changed` signal.** One caller (register window) gets updated in lockstep. No external consumers.
- **`spending_report_window.py` gains a top bar that needs to play well with the existing window state.** Mitigated by keeping the bar purely an overlay above the existing controls — no layout disruption to the chart / filter area.

### Ongoing responsibilities

- **Every new report type added to the `type` enum must land with its filter dataclass in `mfl_desktop/reports/filters.py` and a `default()` for the bare-open flow.** Adding the enum value without the dataclass would crash the sidebar render for that row.
- **Filter-shape changes within a type need a JSON migration step in `filters.py`.** A small helper module `migrate_report_filters(report_row)` lives in the same file; on load, if the stored JSON predates the current shape, it's upgraded in-place and re-saved.
- **Sidebar group rows are visually authoritative.** Adding a new section later means a new group row + separator and the existing two sections re-balance — don't shoehorn a third section into the Reports group.

### Out of scope here (covered separately)

- The per-type filter schemas for Net Worth, Income & Expense, and Sankey — those reports may not even have all four filter dimensions Spending Over Time wants. Each gets its own round ADR.
- **Sankey** as a chart type — the rendering itself (proper Sankey requires more layout work than the existing `paintEvent` patterns) is an open design item. Round-3 ADR.
- **Drag-and-drop in the sidebar** — context-menu Move to Folder ships in round 1; drag is a future polish ADR.
- **Bare-open saved-state interaction** — when a user opens "Spending Over Time" from the menu, configures filters, and then *also* opens a saved Spending Over Time report, they get two windows. Folding the menu-opened bare window into "becomes the saved report once saved" is the obvious behaviour and lands here; no separate "promote to saved" verb needed.
- **Read-only / share / export of a saved report** — a future "Copy filters to clipboard" / "Export as CSV" arc lives separately.
- **Per-report theme overrides** — a saved report whose chart should be a different colour scheme: not in scope.

### Rounds (planning)

- **Round 1** — Schema, sidebar restructure, Spending Over Time as the first persisted type, the Save / Save As / Open / Edit / Delete vocabulary, the Reports menu narrowing. Shipped 2026-06-07 — implementation matches the design above with three small additions: (a) `report_folder` carries `iri` and `archived_at` columns so it mirrors `account_folder`'s shape (the iri convention `mfl:ReportFolder_<uuid8>` was already specified in the ADR text); (b) `Repository.expand_canonical_payee_ids` was added so the new payee filter expands a canonical selection to include its alias rows — bridges the ADR-029 round-1 gap where `txn.payee_id` still points at aliases; (c) `spending_aggregates` gained a `payee_ids` kwarg to honour the new filter dimension. Bare-window vs. saved-report singleton policy: bare windows are one-per-type (Reports menu), saved-report windows are one-per-report-id (sidebar click) — matches the account-summary drill-down pattern and side-steps the contradiction in §"How does the report window know which saved-state it's in".

  **Round-1 polish (same session, 2026-06-07)** after first-use feedback: (a) the always-visible left filter panel moved to a modal `SpendingFilterDialog` opened by a Filter… button on the top bar — too dense permanently visible, and each checklist needed Select all / Deselect all / search anyway (now in the new reusable `CheckListPanel` widget); (b) total + average + filter summary moved to a right-side summary panel so the chart owns most of the window; (c) clicking a bar segment now drills into that category (rollup descends one notch, category filter narrows to descendants), with a Back button on the top bar to pop the drill stack — drill state is view-only and doesn't dirty the saved report's persisted filters; (d) bare windows hide the redundant standalone "Save As…" button (the primary button already says "Save As…" when there's nothing to overwrite); (e) right-clicking in the Reports section's empty space now offers Reports verbs (a new `Sidebar.section_at_y` helper picks the section by y-coordinate so empty-area clicks reach the right menu).
- **Round 2** — Net Worth as a persisted type. Its filter schema chosen at the time. ADR-041 (or whatever number is next).
- **Round 3** — Income & Expense + Sankey. Sankey gets its own rendering-engine sub-decision.
- **Round 4** — Polish: drag-to-folder, export, share, per-report colour overrides if asked.

The arc is open-ended; the round-1 shape doesn't force the later rounds to ship in any particular order. The owner can interleave with other arcs (reconciliation, payee round 2, etc.) as priorities shift.
