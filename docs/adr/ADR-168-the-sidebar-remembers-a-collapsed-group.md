# ADR-168 — The sidebar remembers a collapsed group

**Date:** 2026-07-16
**Status:** Implemented
**Related:** ADR-015 (account folders — this closes its "expansion isn't preserved" backlog item). ADR-092 (the per-file `setting` table, and reopen-last-file-on-launch). ADR-131 (the sidebar's balance-mode preference, the pattern this follows and deliberately departs from). ADR-075 (the Home view's `set_repo`, mirrored here).

## Context

A user reported that collapsing a group in the left panel — a folder, a section, the Closed-accounts group — never sticks. Collapse one to make room, navigate away and back (or quit and relaunch), and it is expanded again.

This is the exact limitation ADR-015 recorded and deferred:

> Folder expansion state isn't preserved across a sidebar reload (currently every reload re-expands all). Small follow-up.

The mechanism is that the sidebar is a single `QTreeWidget` whose groups are `QTreeWidgetItem`s, and expand/collapse state lives **only** on those transient items. `_populate` rebuilds them from scratch — hard-coded to `setExpanded(True)` (and `False` for Closed accounts) — and `reload` runs after almost anything: a transaction add/delete, an import, a report create, navigating between accounts. Every one of those throws the user's choice away. `reload` already round-trips the *selection* across a rebuild; expansion got no such treatment, and even if it had, a round-trip only survives a reload, not a restart.

## Decision

**Persist each group's expansion, per file, and apply it on every build.**

Three decisions carry the weight:

**1. It lives in the file's `setting` table, not app-level `QSettings`.** The balance-mode toggle (ADR-131) is an app-level preference, and following that pattern here would have been the obvious move — but it is wrong for this data. A group's stable key is its folder's DB id, and **folder id 3 in one `.mfl` is a different folder from folder id 3 in another**. An app-level key would bleed one file's collapsed groups onto every other file the user opens. Expansion is per-file state, so it belongs in the per-file store (ADR-092), keyed `sidebar/group_expansion`, and it rides along in backups and snapshots like any other setting.

**2. The value is a `{group_key: expanded}` map of only the groups the user has actually toggled — not a set of collapsed ids.** This is what lets the per-kind *defaults* survive. Folders and section headers default to expanded; the Closed-accounts group defaults to **collapsed** (ADR-069). A bare "set of collapsed keys" cannot represent "the user deliberately *expanded* Closed accounts" without inverting that default. Storing an explicit expanded-bool only for touched groups means `_expanded_for(item, default=…)` reads the saved choice when present and falls back to the caller's per-kind default otherwise — a fresh file behaves exactly as before. Keys are `folder:{id}` / `report_folder:{id}` (the two id spaces overlap, so the kind prefix disambiguates) and the bare kind for the singleton rows (`section_accounts`, `section_reports`, `closed_group`).

**3. The write is driven by `itemExpanded` / `itemCollapsed`, not the click handler.** `_on_item_clicked` only fires for a click on the row *body*. A user collapses a group at least three ways — the row, the disclosure triangle, the keyboard — and only the tree's own expand/collapse signals see all of them. They are connected *after* the initial `_populate` (so building the tree doesn't trip them) and `reload` already wraps its rebuild in `blockSignals(True)` (so a programmatic `setExpanded` during a rebuild doesn't write back). The result: only a genuine user toggle reaches `_remember_group_expansion`, which is best-effort — a failed write must never break navigation.

A file switch reuses the same `Sidebar` instance via `reload`, so the newly-adopted file's remembered state has to take over. The sidebar gained a `set_repo`, called from `_adopt_repository` before the reload, mirroring the Home view's. This also fixes a **latent, pre-existing bug**: the sidebar held a repo reference for its mixed-currency folder roll-ups and that reference was never refreshed on a file switch — so after opening a second file the sidebar's currency conversion was reading the *first* file's settings. One method closes both.

## Rejected

- **App-level `QSettings`, matching the balance-mode toggle.** The tempting consistency, and the one real trap here: folder ids are per-file, so a shared key cross-contaminates files. Per-file storage is the whole point. (Balance mode is legitimately app-level — it is a display preference with no per-file referent — so the two settings correctly live in different stores.)
- **A collapsed-id set instead of an expanded-bool map.** Simpler to write, but it cannot encode "expanded against the default", which is exactly the Closed-accounts case. It would silently flip that group's default the first time anything else was saved.
- **Round-tripping expansion across `reload` the way selection already is, and stopping there.** Half a fix. It would survive a reload but not a relaunch — and "even close and reopen the app" was explicitly part of the report.
- **Hooking `_on_item_clicked` only.** Misses the disclosure-triangle and keyboard toggles — the state would persist for some collapse gestures and not others, which is worse than not persisting at all because it is unpredictable.
- **Pruning stale keys for deleted folders on every write.** A deleted folder leaves an orphaned `folder:{id}` entry in the map. It is inert — no row will ever match it — and pruning would mean walking the live tree on every toggle for no user-visible gain. Left as harmless drift.

## Consequences

- Closes the ADR-015 backlog item, and goes past it: the choice now survives not just a reload but a relaunch, and is correctly scoped per file.
- Fixes the latent file-switch staleness in the sidebar's repo reference (mixed-currency folder totals were resolving their display currency against the previously-open file). `set_repo` re-resolves the display currency and reloads the expansion map together.
- Orphaned keys for deleted folders accumulate in the map. Inert; not pruned (see Rejected).
- The map is written on every toggle — a single indexed upsert into the file's own `setting` table, on a user action that happens at human speed. No hot-path cost; `reload` and `_populate` only *read* the already-in-memory map.

`tests/test_sidebar_group_expansion.py` 6/6: collapse survives a reload; survives a close/reopen (restart); reopening a group clears the memory; state is isolated between two files; a `set_repo` file-switch adopts the new file's state and routes writes to the right file; and a repo-less sidebar (the test construction) no-ops rather than raising. The existing `test_sidebar_balance_mode.py` still passes 3/3. Full suite unchanged bar one pre-existing, unrelated failure (`test_drilldown_account_subset::test_split_row_double_click_opens_split_dialog`, failing on the untouched tree). No schema change — the `setting` table already exists (ADR-092).
