# ADR-022 — Register typeahead delegates + inline category create

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design — `category.source`); ADR-012 (Payee management — free-text payee create on commit); ADR-013 (Category management policy — `source='user'` / `source='import'` / `source='system'`); ADR-014 (Category kind — `income` / `expense` / `transfer`); ADR-017 (Editor delegate pattern in the register). See also the durable feedback rule "no dialog for known-format imports" (we use a dialog here precisely because there *is* something to ask).

---

## Context

The register's editable cells (Payee, Category, Status) use Qt item delegates. After the basic-management round (2026-06-05) and reports round 1, real use has surfaced two friction points on cells whose value space is large:

- **Payee.** Inline edit today is a bare `QLineEdit`. With ~1,300 transactions loaded the owner already has dozens of payees and no in-cell suggestion of what's already in the list — typos like `Tesco`/`Tesco's`/`TESCO` accumulate, and the only fix is the Manage ▸ Payees merge dialog after the fact.
- **Category.** Inline edit is a non-editable `QComboBox` (`CategoryDelegate`) that lists every active category. With the seeded taxonomy plus auto-created import categories the list is already over 60 entries; the combo is a long scroll. The dialog flows (`NewTransactionDialog`, `BulkEditDialog`) already use `make_category_picker` — an editable combo with a contains-match `QCompleter` — and the in-cell experience should match.

The cluster of three improvements called out in the register-UX backlog (CLAUDE_CONTEXT.md and `project_register_ux_backlog`) is:

1. Payee autocomplete on edit.
2. Category autocomplete on edit.
3. Inline category creation when the typed name doesn't exist.

(4) bulk-edit is already shipped in the basic-management round, so it's out of scope here.

The three items above are tightly coupled — they all live behind the same typeahead delegate shape, and (3) only makes sense once (2) gives the user a fast way to discover that the name they want *doesn't* already exist. So they ship together.

The interesting design choices are around (3): inline category creation. A category isn't just a string — it has a `parent_id`, a `kind` (income / expense / transfer per ADR-014), and a `source` (system / user / import per ADR-013). The cell-edit context only carries a name. The decision is how to fill in the rest sensibly without dragging the user into a full create-dialog mid-cell-edit.

## Options considered

### Editor widget for category — reuse `make_category_picker` (chosen)

- *Custom widget*: build a new typeahead widget specifically for the cell delegate. Maximum control over keyboard handling, but two divergent code paths for "pick a category" — one in dialogs, one in the register — which is exactly the kind of inconsistency the user feels first.
- **Reuse `make_category_picker`** (chosen): the same helper that builds the dialog combos builds the cell editor. Identical filter mode (`MatchContains`), case sensitivity, popup behaviour, label format (`Name (Parent)` for sub-categories, leaf name for top-level). One contract to maintain; one keyboard model for the user to learn.
  - The delegate adds the lifecycle plumbing on top: `setEditorData` selects the current category and pre-selects-all the editor text; `setModelData` writes the chosen category id back through the model.

### Choice list — snapshot at construction / fresh per `createEditor` (chosen)

- *Snapshot at construction*: the delegate takes a `list[CategoryChoice]` once and reuses it for every edit. Cheap, but requires the window to rebind the delegate every time the category set changes (after an import, after a category-management dialog, after an inline create). That rebind in turn risks closing a live editor — exactly the bug the inline-create path would trigger if it called `_refresh_categories_view` (which both reloads the model AND rebinds the delegate) before its own `setModelData` call has landed.
- **Fresh per `createEditor`** (chosen): the delegate holds the `Repository` and calls `list_categories_flat()` each time an editor is opened. A single `SELECT … LEFT JOIN` against an unindexed read of a small table — the cost is irrelevant at this scale and the contract is much simpler. The window never has to rebind the category delegate; an inline-created category is automatically present the next time the delegate opens an editor.
  - Symmetric with `PayeeTypeaheadDelegate`, which reads `list_payee_names()` fresh in the same way for the same reasons.

### Payee delegate — snapshot list / repository-backed (chosen)

- *Snapshot list*: cache payee names on the window, pass to the delegate. Same staleness problem as categories — every inline payee edit creates new payee rows, and the snapshot would need to be refreshed after every commit.
- **Repository-backed** (chosen): `PayeeTypeaheadDelegate(repo)` reads `list_payee_names()` fresh on `createEditor`. New `Repository` method (no usage-count subquery, just a sorted-by-name SELECT) keeps the call cheap. The window doesn't have to track payee mutations at all.

### Inline category create — how to handle unknown text (chosen: confirm-and-create as expense at top level)

Four points on the spectrum:

- *No inline create*: typing a name that isn't in the list leaves the cell unchanged and forces the user into Manage ▸ Categories. Safest, but defeats the point of typeahead — every novel category becomes a four-step detour. Rejected.
- *Silent create on commit*: any unknown text immediately becomes a new category, with status-bar confirmation only. Lowest-friction, but typos in the typed text (e.g. `Greoceries` instead of the existing `Groceries`) become permanent ghost categories with one transaction attached, and the only fix is the Manage ▸ Categories merge dialog after the fact. The same hazard the payee column already exposes — but payees have a forgiving v0.1 merge story and categories are much more user-facing because reports build on top of them. Rejected.
- **Single confirm dialog then create** (chosen): "No category named `X` exists. Create it as a new top-level expense category?" / Yes (default) / No. Default-to-Yes so the keyboard fast path is one Enter press; default-to-No would be hostile to the legitimate inline-create case. The dialog gives the user a chance to spot a typo before it gets persisted. On Yes, the category is created and the cell updates; on No, the cell is left unchanged and the editor closes.
- *Full create dialog with parent + kind picker*: mid-cell-edit, pop the same dialog the Manage ▸ Categories ▸ New flow uses, with parent picker, kind selector, and name field. Too heavy for the common case where the user just wants a fresh top-level bucket. Defers the user away from the register, which is exactly where they're trying to work.

The chosen approach assumes that **most** inline creates are flat top-level expense categories, and the user can re-parent / re-kind / merge later via the existing Manage ▸ Categories dialog. That matches the import-created category pattern (which has the same defaults — see `find_or_create_category_path`'s `default_root_kind='expense'`). Status-bar confirmation includes `(expense, top-level)` so the user knows where to find it.

### Defaults for inline-created categories — fixed at expense / top-level / user (chosen)

Given the confirm dialog, the create call uses `parent_id=None`, `kind='expense'`, `source='user'`. The reasoning:

- **Top-level (`parent_id=None`).** The cell-edit context has no parent — the user typed one word. Guessing a parent ("you typed 'Groceries' — did you mean it under 'Food'?") would be magical and wrong as often as right. Top-level is the only un-magical choice; the user can re-parent in two clicks from Manage ▸ Categories.
- **`kind='expense'`.** The register is overwhelmingly used for expense entry; income lines are a small minority and transfer lines are explicit (ADR-020). Defaulting to expense aligns with the import path's behaviour and with reports' strict-outflow semantics (ADR-018). A user creating an income category inline is rare enough that requiring them to fix the kind from Manage ▸ Categories is acceptable. If real use turns this assumption around — i.e. wrong-kind inline creates become common — the right v2 fix is to extend the confirm dialog with a tiny kind radio, not to add a separate full-create path.
- **`source='user'`.** Matches the existing dialog-driven create path. Distinguishes from the `import`-sourced auto-creates so the category-management UI can show provenance.

### Confirm dialog — modal `QMessageBox` / inline status-bar prompt / non-blocking toast (chosen: modal)

- *Inline status-bar prompt with Y/N keys*: novel, requires a custom widget, and steals keyboard focus from the cell editor in a way that interacts poorly with the editor's Tab/Enter handling. Rejected as over-engineered.
- *Non-blocking toast "category created — undo?"*: requires an undo stack the rest of the app doesn't have. Rejected.
- **Modal `QMessageBox.question`** (chosen): standard Qt shape, two buttons, default Yes (so Enter is the fast path). Two extra keystrokes compared to silent-create, and the user pays them only on a brand-new category — every existing category goes through the popup path with zero added friction.

### Where does the inline-create logic live? — delegate / window callback (chosen)

The delegate's `setModelData` only sees text and the index. To decide whether to create, it needs to:

- Show a Qt dialog parented to the main window (not the cell editor — that would close when the dialog opens).
- Mutate the repository.
- Refresh the cached `self._categories` list and the filter-bar combo on the main window.
- Write a status-bar message.

All of those live on `RegisterWindow`, not the delegate. So the delegate takes a `Callable[[str], Optional[int]]` constructor parameter — `on_create_category` — and the window passes `self._on_create_category_inline`. The delegate stays UI-only; the window owns the policy. Same shape as the existing `_prompt_destination_account` / `_on_model_data_changed` factoring for transfers (ADR-020).

### Refresh scope after inline create — `_refresh_categories_view` / new `_reload_category_cache` (chosen)

The existing `_refresh_categories_view` does three things: reload the cached category list, rebuild the filter-bar combo, AND reload the model (because category merges/deletes can re-point `txn.category_id` to Uncategorised). Calling it from inside a delegate's `setModelData` would reset the model mid-edit, invalidating the `QModelIndex` the delegate is about to write into.

The inline-create case only needs the cache + the filter combo refreshed. So the window gains **`_reload_category_cache()`** — the lighter half of `_refresh_categories_view`, safe to call from within a delegate commit. The existing `_refresh_categories_view` is unchanged and still used by the import path and the categories-management dialog, where a model reload is correct.

## Decision

### Repository layer

`Repository` gains one read-only method:

- **`list_payee_names() -> list[str]`** — sorted, NOCASE collation, excludes archived payees. Used by `PayeeTypeaheadDelegate`.

No schema change. No new migration. `create_category` already supports `parent_id=None, kind='expense', source='user'` so the inline-create path uses it as-is.

### Delegate layer — `mfl_desktop/ui/delegates.py`

`CategoryDelegate` is replaced by:

- **`PayeeTypeaheadDelegate(repo: Repository, parent=None)`** — editor is a `QLineEdit` wrapped with a `QCompleter` over `repo.list_payee_names()` (PopupCompletion, MatchContains, CaseInsensitive). Commits raw stripped text; the model's existing `update_transaction_payee` path resolves it via `get_or_create_payee`, preserving the v0.1 free-text-create-on-commit behaviour.
- **`CategoryTypeaheadDelegate(repo: Repository, on_create_category: Callable[[str], Optional[int]], parent=None)`** — editor is the `make_category_picker` combo, fetched fresh from `repo.list_categories_flat()` on every `createEditor`. `setModelData` calls `selected_category_id(editor)` first; if the typed text matches an existing label exactly the id is committed straight through. Otherwise the delegate calls `on_create_category(text)` and commits whatever id comes back. If the callback returns `None` (user cancelled or create failed) the cell is left unchanged.

`StatusDelegate` is unchanged.

### Window layer — `mfl_desktop/ui/register_window.py`

- `_set_model` attaches `PayeeTypeaheadDelegate(self._repo, …)` to the `payee_name` column in both view modes (single-account and all-transactions).
- The category column gets `CategoryTypeaheadDelegate(self._repo, self._on_create_category_inline, …)` instead of `CategoryDelegate(self._categories, …)`.
- New private method **`_on_create_category_inline(name: str) -> Optional[int]`** — shows the confirm dialog, calls `repo.create_category(name, parent_id=None, kind='expense', source='user')`, calls `_reload_category_cache()`, writes the status-bar message, returns the new id. Catches `ValueError` from the create call (sibling-name collision in race scenarios) and surfaces it via `QMessageBox.warning`.
- New private method **`_reload_category_cache()`** — refreshes `self._categories` and rebuilds the filter-bar combo only. Safe to call from inside a delegate's `setModelData`.
- `_refresh_categories_view` continues to do the full refresh (cache + filter combo + model reload + delegate rebind) and is still called by the import and categories-management paths.

No changes to the model, the proxy, the sidebar, or the dialogs.

## Consequences

### Positive

- **Payee typing instantly suggests existing names.** Typos that produce duplicates (`Tesco's` vs `Tesco`) become rare because the completer surfaces the existing entry before the user finishes typing.
- **Category cell editing matches the dialog experience exactly.** Same widget helper, same keyboard model, same filter behaviour — the register and the New Transaction / Bulk Edit flows feel like the same app.
- **New categories can be created without leaving the register.** A confirm dialog is the only interruption; status-bar feedback shows the new category's defaults so the user knows what's been persisted.
- **No model reload during inline create.** The split between `_reload_category_cache` (lightweight, safe inside delegate commit) and `_refresh_categories_view` (heavy, used by mutation-from-outside flows) keeps the commit path predictable.
- **Repository-backed delegates remove the window's responsibility to track every payee/category mutation.** A fresh query per editor open replaces a brittle cached-list-plus-rebind dance, and the new query is cheap.
- **Symmetry between payee and category delegates** — both take the repo and read fresh on open — makes the pattern easy to extend in future (e.g. a memo-history typeahead would slot in the same way).

### Negative / trade-offs

- **Every inline create defaults to top-level expense / source=user.** A user creating an income or transfer category inline gets it wrong and has to fix it from Manage ▸ Categories. The friction is bounded (the wrong-kind category still records the transaction; only reports interpret the kind), and the assumption — that the overwhelming majority of inline creates are flat top-level expense categories — matches how the import path treats unknown categories. If real use proves this wrong, a tiny kind radio added to the confirm dialog is the natural v2.
- **Confirm dialog on unknown text means a focus-loss commit can pop a dialog when the user clicks away.** Standard Qt commit-on-focusout pattern; the No button cancels and the cell is left unchanged. Acceptable, called out so it isn't surprising.
- **`list_categories_flat` is called once per editor open.** With several hundred categories the query is still microseconds, but the cost grows linearly. If the category set ever reached the thousands, caching with mtime-based invalidation would be a sensible follow-up; nothing to do now.
- **`PayeeTypeaheadDelegate` reads all payee names on every open.** Same shape, same trade-off. With ~1,300 transactions the payee count is in the low hundreds; not a concern at MFL's scale.

### Ongoing responsibilities

- The `make_category_picker` helper now backs three surfaces (two dialogs and the register cell). Any change to its keyboard model or popup behaviour affects all three — a feature, but worth being explicit about. New surfaces should reuse the helper rather than rolling their own.
- The inline-create default of `kind='expense'` aligns with `find_or_create_category_path`'s `default_root_kind`. If the import-path default changes (e.g. ADR-014 evolves to a smarter inference), this ADR should be revisited so the two paths stay aligned.
- The confirm dialog wording mentions Manage ▸ Categories as the place to re-parent / change kind. Renaming or removing that menu entry would orphan the prompt; keep them in sync.
