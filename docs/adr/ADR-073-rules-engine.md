# ADR-073 — Auto-categorisation rules engine (Arc G round 2)

**Date:** 2026-06-15
**Status:** Accepted
**Implements:** ADR-028 round 3 (the long-deferred `rule` table) + the owner's round-3 sketch of a unified "see aliases, create/delete rules" screen.
**Related:** ADR-072 (round 1 — per-payee category memory + import-time alias resolution; this round runs *after* alias resolution). ADR-029 (aliases). ADR-012/013/014 (payees, categories, kind). ADR-051 (splits). ADR-018 (strict-outflow / categories). The `rule` table reserved in migration 0001.

---

## Context

ADR-072 (round 1) gave a payee a remembered category and applied it at import. That covers the *exact-payee* case. It doesn't cover the owner's pattern case — *"anything whose text contains TFL is TfL / Transport"*, *"anything starting with AMZN is Amazon"* — where the merchant string varies but a substring is stable. The `rule` table has sat unused since migration 0001 for exactly this; ADR-028 planned it as round 3.

The owner sketched the UI: *"a dedicated screen to be able to see all of the aliases, and be able to create or delete rules. You'd be allowed to say 'if contains xxxx string, then xxxx payee' or 'starts with, ends with, is exactly'."* The 0001 `rule` table's `pattern_kind` CHECK only allows `substring`/`regex`, which doesn't match that vocabulary.

ADR-028 (Option C, rejected) and ADR-072 both settled that **aliases and rules stay separate at the data layer** — an alias is an *identity* statement (rolls up history), a rule is an *import-time automation*. This round keeps that split and adds the rule engine alongside; the "unified screen" is a *view*, not a merged model.

Owner forks (`AskUserQuestion`): the screen is a **dedicated Rules screen that also lists aliases read-only**; rules apply retroactively **only when asked** (unset/uncategorised only, never overwrite); a rule can set **payee and/or category**.

---

## Decision

### Matcher vocabulary (migration 0025)

The `rule.pattern_kind` CHECK is recreated as `('contains','starts_with','ends_with','is_exactly')` (the owner's vocabulary), replacing `('substring','regex')`. Same table-recreate recipe as the report-type CHECK widenings (0014/0023/0024): `PRAGMA foreign_keys=OFF`, create `rule_new`, copy (mapping any legacy `substring`/`regex` → `contains`, though the table is empty), drop, rename, recreate `idx_rule_priority`. Everything else on the row is unchanged: `pattern`, `match_field` (`payee_raw`/`memo`), `set_category_id` / `set_payee_id` (both `ON DELETE CASCADE`, at least one required by CHECK), `priority`.

### The engine is pure (`rules_engine.py`)

A dependency-free module — no Repository import (it takes duck-typed rule objects with the six attributes, so `RuleRow` from the Repository is compatible without a circular import, mirroring `budget_calc`/`goal_calc`):

- `rule_matches(rule, payee_text, memo) -> bool` — picks the field by `match_field`, lower-cases both sides, and tests `contains` / `starts_with` / `ends_with` / `is_exactly`. Empty pattern never matches.
- `apply_rules(rules, payee_text, memo) -> (set_payee_id, set_category_id)` — evaluates rules in **priority order (ascending = highest priority first; ties by id)** and fills each field from the **first matching rule that sets it** (field-independent first-win, so a payee-only rule and a category-only rule compose). Constants `MATCHER_KINDS` / `MATCH_FIELDS` live here too.

### Applied at import, after alias resolution (`ImportService.commit_import`)

Rules are loaded once per import (`Repository.list_rules`). For each plain row, after `resolve_import_payee` (ADR-072):

1. `apply_rules(rules, tx.payee_raw, tx.memo)` → `(rule_payee, rule_cat)`.
2. If `rule_payee` is set, it **overrides** the resolved payee (and the effective per-payee default is re-read from the rule's payee).
3. Category precedence when the source left the row **Uncategorised** (and it isn't a split): **rule category → else per-payee default** (ADR-072). A category the file carried always wins over both.

`rule_payee` applies to split parents too (payee is parent-level); `rule_cat` doesn't (a split parent stays Uncategorised, its lines carry categories). The `potential_match` merge path is untouched. Re-import dedup is unaffected (cash hashes on `payee_raw`, not the resolved ids). Manual entry doesn't run pattern rules (there's no raw text — you pick a payee); it uses the per-payee default instead (see below).

### Retroactive application (ask each time)

On creating/editing a rule, the screen offers *"Apply to N matching existing transactions?"*. Matching reuses the **same pure `rule_matches`** against each transaction's **stored payee name** (the original raw import string isn't retained, so the current name is the proxy — a `contains TESCO` rule still matches a row now named "Tesco") and memo. Safe scope, mirroring ADR-072: only a row whose target field is unset is changed — `set_category_id` only fills an **Uncategorised** category; `set_payee_id` only fills a **NULL** payee — never overwriting, and never touching transfers or split parents. `Repository.count_txns_matching_rule` / `apply_rule_to_existing` do the count + update.

### Management screen (`rules_dialog.py` + `rule_edit_dialog.py`)

**Manage ▸ Rules…** opens a non-modal-style dialog with two tables:

- **Rules** (editable): When (`payee text/memo` + matcher + pattern) · Sets payee · Sets category · Priority, with New / Edit / Delete. The edit dialog has match-field + matcher-kind combos, a pattern field, optional payee picker (canonicals) + optional category picker, and a priority spin box; it validates that the pattern is non-empty and at least one of payee/category is set.
- **Aliases** (read-only): the existing `payee.canonical_id` aliases shown as implicit *"is exactly → payee"* rows, with a note that they're managed in **Payees…** — giving the owner the "see all aliases + rules in one place" view without duplicating alias CRUD.

`Repository`: `RuleRow` (with display `set_payee_name` / `set_category_path`), `list_rules`, `create_rule`, `update_rule`, `delete_rule`, `new_rule_iri()`.

### Manual-entry category pre-fill (the deferred G1 nicety)

`NewTransactionDialog` gains an optional `payee_category_lookup` callback; on the payee field's `editingFinished`, if the typed name resolves to a payee with a remembered category and the category combo is still Uncategorised, it pre-fills. Backward-compatible (the param defaults to None → today's behaviour). The register passes a lookup over `find_payee_id_by_name` → `get_payee_default_category`.

---

## Consequences

- The owner's pattern case is covered: `contains TFL → TfL / Transport` auto-applies on import and (when asked) to history.
- **Aliases, per-payee memory, and rules** now form a coherent stack: alias resolves identity → rules apply pattern automation → per-payee default fills any remaining gap. Each stays a distinct, inspectable concept (no ML, ADR-028).
- One migration (0025), one new pure module, additive Repository methods, two new dialogs, one menu item, one optional dialog param.
- `set_category_id`/`set_payee_id` keep `ON DELETE CASCADE`: deleting a category or payee that a rule targets deletes the now-meaningless rule. Acceptable (a dangling rule would silently stop working); surfaced only as the rule disappearing.

### Rejected alternatives

- **Merge aliases into `rule`** (ADR-028 Option C) — conflates identity and automation; would disturb the load-bearing `COALESCE(canonical_id, id)` rollup. Kept separate; unified only in the view.
- **Regex matchers** — power-user feature at odds with the non-technical audience; the four friendly kinds cover the real cases. Easy to add later (it was in the original CHECK).
- **Match rules against the resolved/canonical payee** — that's exactly what the per-payee default category already does; rules match raw text/memo, the one thing the default can't.
- **Rules on manual entry** — no raw text exists at manual entry; the per-payee pre-fill covers that path.
- **Retroactive overwrite** — would clobber deliberate categorisations/payees; unset-only is the safe contract (matches ADR-072).

---

## Verification

Offscreen:

- Pure engine: each matcher kind (contains/starts/ends/exact, case-insensitive); `apply_rules` priority order + field-independent first-win; empty-pattern guard; payee-only + category-only composition.
- Repository: `create/list/update/delete_rule` round-trip with display names; `count_txns_matching_rule` / `apply_rule_to_existing` scope (unset/uncategorised only, no transfers/splits, never overwrite) reusing `rule_matches`.
- End-to-end import: a `contains` rule sets payee + category on a matching row; a category the file carried is kept; precedence rule-cat > per-payee default; split parent gets the rule payee but stays Uncategorised.
- Offscreen Qt: the rule edit dialog validates (empty pattern / no setter rejected) and round-trips values; the Rules dialog lists rules + read-only aliases; the New Transaction dialog pre-fills the category from a remembered payee.
