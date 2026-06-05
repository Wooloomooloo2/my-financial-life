# ADR-021 — Generic CSV column-mapping wizard

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design — `txn`, `import_batch`, categories); ADR-013 (Category management — `:`-path resolution into the hierarchy); ADR-017 (Modal-dialog shape, per-field checkbox pattern reused here); ADR-018 (Strict-outflow spending semantics — depends on correct `kind` assignment from import); see also the durable feedback rule "no dialog for known-format imports".

---

## Context

OFX, QFX, Banktivity CSV, and credit-card CSV imports already work end-to-end through `parse_and_stage` → silent commit, per the no-dialog-for-known-imports feedback. The fifth path — `_detect_format` returning `"generic"` — is the open hole. The current GUI shows a `QMessageBox` saying "Column mapping needed… coming in a future update", and the user falls back to re-exporting from another tool. The owner's most-wanted source for this round is **Pocketsmith**, whose CSV export is a perfectly ordinary `Date / Amount / Note / Category / …` layout that isn't currently auto-detected and never will be (it shares no distinguishing fingerprint with the formats `_detect_format` checks for).

The plumbing for user-supplied mappings has been in place since the rewrite started:

- `CsvColumnMapping` (dataclass): `date_col`, `date_format`, `amount_col`, `amount_inverted`, `debit_col`, `credit_col`, `payee_col`, `memo_col`, `category_col`.
- `csv_parser.parse_with_mapping(content, mapping)`: applies the mapping to raw CSV content and produces the same normalised transaction dicts the OFX path emits.
- `ImportService.parse_and_stage` already detects "generic" and stages a `PendingCsvMap` (headers + 5 preview rows + raw bytes) instead of going straight to classify-and-stage.
- `ImportService.apply_mapping_and_stage(token, mapping)` reads the staged bytes back, runs `parse_with_mapping`, then runs the same classify-and-stage path as the known-format imports.

All that is missing is the **dialog that turns user choices into a `CsvColumnMapping`**. This ADR captures the dialog's shape, the smart-defaults strategy that keeps it out of the user's way when the source headers are conventional, and the deliberate exclusion of mapping-profile persistence from this round.

Two design dimensions follow:

1. **How much should the wizard ask?** A multi-step wizard with separate pages for date / amount / payee / etc. would be the classic shape; a single-page modal dialog is the alternative.
2. **Should mappings be saved for re-use?** Pocketsmith CSVs are a recurring source — the user will import them every month. Wizarding through the same 5 combos every time is a friction; auto-detecting "this header signature has been mapped before" and skipping straight to the silent-commit path is a meaningful UX improvement. But it's a new schema surface (mapping table + header-signature scheme) and a new conflict path (what if the user edits a saved mapping mid-flow?) — large enough to be its own ADR.

## Options considered

### Dialog shape — multi-step wizard / single-page modal (chosen)

- *Multi-step `QWizard`*: one page per logical decision (date, amount, payee+memo, category, preview). Walks the user through the fields one at a time; familiar shape. But it inflates a 30-second task into 5 button-clicks even when the smart defaults are right, and the "after-mapping preview" feedback loop wants the date / amount / payee combos on the same screen as the preview table so a user can see why a row didn't parse without paging back and forth. Rejected.
- **Single-page modal `QDialog`** (chosen): one screen with the original-file preview at the top, the mapping form in the middle, and an after-mapping preview at the bottom that refreshes on every combo change. The user sees their choices and the consequences side by side. Matches the project's existing dialog idiom (`BulkEditDialog`, `AccountDialog`, `TransactionDialog`).

### Smart defaults — none / alias-based pre-fill (chosen)

- *Empty combos*: every dropdown starts at `(none)`; the user picks each field. Defensible but unfriendly — a Pocketsmith export with `Date / Amount / Note` headers shouldn't need the user to set three combos to the only sensible value.
- **Alias-based pre-fill** (chosen): reuse the alias lists already in `csv_parser._parse_generic` (`_DATE_ALIASES`, `_AMOUNT_ALIASES`, `_DEBIT_ALIASES`, `_CREDIT_ALIASES`, `_PAYEE_ALIASES`, `_MEMO_ALIASES`) plus a new `_CATEGORY_ALIASES`. On open, scan the file's headers against each alias list and pre-select the match. The user only adjusts what's wrong. For Pocketsmith this means: open the wizard → glance at the after-mapping preview → click Import. Three clicks total instead of eight.
  - Important: this is *only* defaults. `_detect_format` still returns `"generic"` because the file isn't *uniquely* identifiable as a known bank format — we still show the wizard so the user can confirm or override. The defaults aren't allowed to silently commit; they're a starting point.

### Amount style — always single column / debit+credit pair / radio toggle (chosen)

- *Always single signed column*: simplest, fits Pocketsmith. But many UK high-street bank CSVs use a debit column and a credit column instead; rejecting them would force the user to pre-process the file in a spreadsheet, which contradicts the point of the wizard.
- *Auto-detect from headers*: try to pick single-vs-split based on which alias list matched. Brittle — a file with both an `Amount` and `Debit` column (some banks do this redundantly) makes the wrong choice silently.
- **Radio toggle with single as default** (chosen): two radio buttons, *Single signed column* (default) and *Separate debit and credit columns*. The respective combos enable when their radio is selected; the others are disabled (not hidden — keeps the layout stable). Pre-fill picks single when the headers match `_AMOUNT_ALIASES`, otherwise checks for both debit and credit matches and switches to split if it finds them.

### Sign convention — fixed / "invert sign" checkbox (chosen)

The default sign convention in MFL is `positive = credit (inflow), negative = debit (outflow)` — same as Banktivity. Some credit-card statement exports flip this (positive = "purchase" = outflow). A user-facing `Invert sign` checkbox on the single-column path lets the wizard handle both; uses the existing `CsvColumnMapping.amount_inverted` flag the parser already supports.

### Date format — auto only / dropdown of common patterns / auto + custom (chosen)

- *Auto only*: `csv_parser._parse_generic_date` tries seven formats. Good enough for most files. But the failure mode (silent row skip when none match) is opaque, and a UK user with a `13/06/2026` date might see all their rows mysteriously vanish if the auto-fallback happened to match `%m/%d/%Y` first against US dates earlier in the file.
- **`auto` + named common patterns + a custom strptime entry** (chosen): the date-format combo has `auto` selected by default, plus `%Y-%m-%d`, `%d/%m/%Y`, `%m/%d/%Y`, `%d-%m-%Y`, `%Y%m%d` as named pre-sets, plus `(custom…)` which switches the combo into a free-text strptime editor. The after-mapping preview surfaces parse failures by showing `(unparseable)` in the date cell instead of silently dropping the row, so a wrong default is immediately visible.

### Category column — none / mapped through the existing `:` path resolver (chosen)

A category column is **optional** but, when set, runs through `_resolve_category_id` exactly as Banktivity does today: split on `:`, walk/create the hierarchy. Pocketsmith uses single-level categories so the resolver behaves as a find-or-create on the top level; users coming from Banktivity who exported via Pocketsmith first would still get their `Food:Groceries` style paths preserved.

### Live preview — none / manual refresh / live re-render on every change (chosen)

- *None*: user fills in the combos and hits Import. If they got it wrong, the import goes through with wrong data and they have to undo via category-management and bulk-edit. Bad.
- *Manual "Refresh preview" button*: cheap to implement, but the user has to remember to click it. The button itself adds clutter to a busy dialog.
- **Live re-render on every change** (chosen): every `currentTextChanged` / `toggled` signal triggers a re-parse of the staged preview rows through `parse_with_mapping` and updates the after-mapping table. Cost is negligible (5 rows, 5 columns, parsed in microseconds). The user sees the consequence of their selection without taking an extra action. The dialog feels responsive in the way Banktivity's column-mapping import feels responsive.

### Saved mapping profiles — yes / **deferred** (chosen for this ADR)

A real follow-up. The natural shape would be:

- A header-signature key — sorted, lower-cased, comma-joined header names (so `Date, Amount, Note, Category` and `note, amount, date, category` map to the same key).
- An `import_mapping_profile` table storing `(signature, name, mapping_json, created_at, last_used_at)`.
- On `parse_and_stage`, before falling through to the wizard, look up the signature. Hit → apply the saved mapping straight through, commit silently, status-bar result. Miss → wizard. On wizard accept, ask "remember this mapping for this format?" with a default name derived from the filename stem.
- Management UI for editing / deleting saved profiles (new entry under File or Settings).

**Deferred from this round** because:
- The wizard is independently useful on day one; profiles are an optimisation on top.
- Profile editing has UX questions of its own (what if the user wants to remap a column after using a saved profile? do we re-prompt or silently apply? how do we handle a header that's renamed in a new export version?) that deserve their own design pass.
- This ADR explicitly states the deferral so the next ADR can pick it up without re-litigating the shape of the wizard.

## Decision

### Repository / data layer
No schema changes. No new tables. All wiring sits in `import_engine/` and `ui/`.

### Service layer

`ImportService` gains one method:

- **`discard_pending_map(token: str) -> None`** — drops the staged `PendingCsvMap` when the user cancels the wizard. Idempotent (no error if the token is already gone). Called from the dialog's Cancel/Reject path so a cancelled wizard doesn't leak the staged file bytes in memory until process exit.

No changes to `parse_and_stage`, `apply_mapping_and_stage`, `_classify_and_stage`, or `commit_import` — the existing surface is sufficient.

### Parser

`csv_parser.py` gains one constant:

- **`_CATEGORY_ALIASES = ("category", "categories", "tag", "tags")`** — used by the dialog's smart-default scan. The generic parser doesn't currently look for a category column on its own (`_parse_generic` ignores it) and that stays the same — the alias list is purely for the dialog to pre-select the dropdown.

No other parser changes.

### Dialog — `mfl_desktop/ui/csv_mapping_dialog.py` (new)

Single modal `QDialog` (`CsvMappingDialog`) constructed with the staged `PendingCsvMap` plus a parent.

**Top — File preview.** Read-only `QTableWidget`, original headers as column titles, the 5 stored `preview_rows` as the body. Row height fixed so the table is compact (~120px).

**Middle — Mapping form.** `QGridLayout`:

- `Date column:` — `QComboBox`, the file's headers.
- `Date format:` — `QComboBox` editable: `auto` (default), `%Y-%m-%d`, `%d/%m/%Y`, `%m/%d/%Y`, `%d-%m-%Y`, `%Y%m%d`. The user can type a custom strptime pattern; if it doesn't match one of the named entries, the underlying `CsvColumnMapping.date_format` carries the typed string straight to the parser.
- `Amount style:` — two `QRadioButton`s in a `QButtonGroup`:
  - **Single signed column** (default):
    - `Amount column:` — `QComboBox`, the file's headers.
    - `Invert sign (positive = debit):` — `QCheckBox`.
  - **Separate debit and credit columns**:
    - `Debit column:` — `QComboBox`, the file's headers.
    - `Credit column:` — `QComboBox`, the file's headers.
  - The two groups are stacked in the grid; the radio toggle enables one group's combos and disables the other's. Disabled (not hidden) so the layout stays stable.
- `Payee / description:` — `QComboBox` with `(none)` as the first option plus the file's headers.
- `Memo (optional):` — same shape.
- `Category (optional):` — same shape.

**Bottom — After-mapping preview.** Second read-only `QTableWidget` with five columns: `Date`, `Payee`, `Amount`, `Direction`, `Category`. Re-renders on every form change via `_refresh_preview()`:

- Builds a `CsvColumnMapping` from the current widget state.
- Parses the staged `file_bytes` with `parse_with_mapping`.
- Takes the first 5 rows (or fewer if the mapping fails).
- Cells that fail to parse show `(unparseable)` instead of silently disappearing — so a wrong date format is immediately visible.

If the form is incomplete (no date column, no amount source) the after-mapping table shows a single greyed-out informational row instead of attempting a parse.

**Smart defaults.** On construction, iterate the file's headers (lower-cased, stripped) and pre-select the first match against each alias list. If `_DEBIT_ALIASES` and `_CREDIT_ALIASES` both match, the *Separate* radio activates; otherwise *Single*. The user sees the dialog with sensible choices already populated and a live after-mapping preview showing what they'll get.

**Dialog buttons.** Standard `QDialogButtonBox(Ok | Cancel)`.

- **OK** validates: date column set; at least one amount source set (either Amount or both Debit and Credit). If validation fails, the dialog stays open and surfaces the reason via a `QMessageBox`. If validation succeeds, the dialog exposes the built `CsvColumnMapping` as a property and accepts.
- **Cancel** rejects; the caller is responsible for calling `discard_pending_map(token)`.

The dialog **does not** call `apply_mapping_and_stage` or commit anything itself. It is a pure value-producing widget — accepts → caller reads `.mapping`, rejects → caller discards. Keeps the dialog independently testable and matches the value-producing shape of `BulkEditDialog`.

### Caller — `register_window.py`

`_on_import` changes:

1. The `next_step == "map"` branch replaces the "coming soon" `QMessageBox` with:
   - Pull the `PendingCsvMap` via `get_pending_map(token)`.
   - Construct and `exec()` the `CsvMappingDialog`.
   - On reject: call `discard_pending_map(token)`, return silently.
   - On accept: call `apply_mapping_and_stage(token, dialog.mapping)`, receive a new token pointing at a normal `PendingImport`.
2. The post-stage commit logic (auto-accept potential matches, `commit_import`, refresh views, status-bar message) is factored out of `_on_import` into a new private method **`_commit_pending(token: str, file_label: str) -> None`** so both the known-format path and the mapped path call the same code. `file_label` is just the user-visible filename for the status-bar message — keeps the existing message text intact.

No changes to the sidebar, model, or proxy.

## Consequences

### Positive
- **Pocketsmith and any other column-shaped CSV import now works**, end-to-end, without spreadsheet pre-processing.
- **Smart defaults make the common case near-zero-friction** — open the dialog, glance at the after-mapping preview, click Import.
- **Live preview surfaces mapping mistakes immediately** — date-format mismatches and inverted-sign confusions are visible before commit, not after.
- **No schema change**, no migration. The wizard sits entirely above the existing repository contract.
- **`_commit_pending` factoring** removes a soon-to-be three-way duplication (known format, mapped format, future saved-profile format) of the commit-and-refresh code in `_on_import`.
- **`discard_pending_map`** closes a small memory-leak gap that was already latent for any user who started a generic-CSV import and then cancelled.

### Negative / trade-offs
- **Every Pocketsmith import re-walks the wizard.** Smart defaults make it fast, but a returning user still has to click Import once per month per Pocketsmith file. Saved mapping profiles fix this; deferred to a future ADR.
- **No multi-line preview while editing combos.** The after-mapping preview is fixed at 5 rows — same as the file preview. A file whose first 5 rows are unrepresentative (e.g. all credits, no debits) will give a misleading preview. Accepted; the cost of a larger preview is dialog height, and 5 rows is the same window the existing `PendingCsvMap` stores.
- **Date format `auto` can still pick the wrong order** on ambiguous DMY/MDY dates if the first few rows happen to be `<=12/<=12`. Surfaced by the after-mapping preview when an absurd date appears, but not preventable for files that are themselves ambiguous in their first rows. Workaround: the user picks `%d/%m/%Y` or `%m/%d/%Y` explicitly.
- **No category-column merging across the same parse.** If a Pocketsmith export labels two rows `Groceries` and the user has both `Expense:Groceries` and `Auto:Groceries` in their tree, `_resolve_category_id` will silently create a new top-level `Groceries` rather than merge. This is consistent with the import-from-Banktivity behaviour and the same workaround applies (post-import category-merge via the management dialog). Not new; called out so future users know.

### Ongoing responsibilities
- The wizard's smart defaults depend on the alias lists in `csv_parser.py`. When a new common header alias surfaces from a real-world export ("Posting Date", "Booking Date", "Transaction Amount Local"), it gets added to the relevant list — that alone widens the wizard's pre-fill without touching the dialog.
- The dialog uses `parse_with_mapping` for live preview. Any future parser change (e.g. multi-currency awareness, exchange-rate columns) should ensure the preview keeps working — ideally by extending `CsvColumnMapping` rather than branching the parser.
- The deferred saved-mapping-profile feature, when implemented, builds on `_commit_pending` and the existing `apply_mapping_and_stage` plumbing — adding the profile cache between header inspection and dialog instantiation, not by rewriting either. Its ADR should cover header-signature schema, conflict handling on re-export with renamed columns, and profile-management UI.
