# ADR-112 — Imports stop recreating curated categories (import-map + match-only)

**Date:** 2026-06-26
**Status:** Implemented (2026-06-26).
**Relates to:** ADR-013/014 (category tree + kinds), ADR-028 (payee alias model —
the template), ADR-070 (archive), ADR-111 (QIF cash import — the trigger).

---

## Context

The owner curates the category tree as they import from Banktivity — merging
duplicates, reparenting, deleting noise. Importing **REI Master Card** (ADR-111,
~1,200 rows) then **recreated a batch of previously merged / reparented / deleted
categories**: `Bills:Utilities:Cable and Internet` reappeared beside the curated
`Bills:Cable and Internet`, a deleted `Fees:Charges` came back, and so on.

Root cause: the importer resolves each transaction's **source category path**
(e.g. `"Bills:Utilities:Cable and Internet"`) through
`find_or_create_category_path`, which *creates* any path it can't find. Once the
user has merged that path away, the source string no longer matches — so the
next import forks a fresh duplicate. Every curation decision is silently undone.

`source='import'` can't distinguish junk from curated: on the owner's real file
179 of 186 categories are `source='import'` (everything came from Banktivity), so
"only delete import-sourced categories" is no filter at all.

## Decision

Two complementary mechanisms, modelled on the payee canonical/alias map (ADR-028):

### 1. A persistent category import-map (migration 0032)

`category_import_map(source_path TEXT PRIMARY KEY, target_category_id INTEGER
REFERENCES category(id) ON DELETE CASCADE)`. The key is the **normalised** source
path (segments trimmed + lower-cased, ':'-joined), so casing/whitespace variants
collapse to one entry.

The map is **auto-recorded by the curation verbs** — the user never hand-authors
it:

- **merge** records each source category's full path → the merge target (and
  repoints any mapping that targeted a source, so chains hold);
- **delete** records the deleted path → **Needs Review** (and repoints mappings
  that targeted the deleted category there too);
- **reparent** records the *old* path of the moved category **and every
  descendant** → their unchanged ids (a move rewrites the whole subtree's paths).

All recording happens inside the verb's existing transaction, so the map change
is atomic with the structural change.

At import, `_resolve_category_id` now resolves in priority order:
1. empty path → Uncategorised;
2. **an explicit mapping wins** — this is how a curated decision sticks;
3. an existing path is used as-is (find-only; never creates a duplicate);
4. otherwise create (legacy behaviour) — unless match-only is on (below).

### 2. Match-only mode + a "Needs Review" holding category

A per-file setting `import_match_only_categories` (**off by default** — a fresh
file must still build its tree from the first import). When **on**, step 4 above
parks the unmatched path in a seeded **"Needs Review"** category (migration 0032,
id recorded in `setting.needs_review_category_id`) instead of creating it — so a
genuinely-new import is visible for triage rather than quietly forking the tree.
"Needs Review" was chosen over Uncategorised so these don't hide among the
ordinary uncategorised backlog.

### UI

The Manage ▸ Categories dialog gains a **"Match imports only"** checkbox (writes
the setting) and an **"Import Mappings…"** button opening a read-mostly
`ImportMappingsDialog` that lists every redirect and lets the user **Forget** any
that are wrong.

## Consequences

- A re-import of a curated path now lands where the user put it. Future imports
  of merged/deleted/moved names follow the recorded redirect instead of
  recreating the category.
- Match-only gives the careful user a hard guarantee: imports never again invent
  a category — anything unrecognised waits in Needs Review.
- The map is normalised and case-folded, so Banktivity's inconsistent casing
  doesn't fork entries.
- **Triage for the damage already done:** the 6 categories the REI import forked
  are merged back by hand in Manage ▸ Categories *after* this ships — and because
  merge now records mappings, doing the merges also inoculates against the next
  import. (Targets: `Bills:Utilities:Cable and Internet`→`Bills:Cable and
  Internet`; `Bills:Utilities:Mobile Phone`→`Bills:Phone`;
  `Personal:Education:Tuition`/`:Books`→`Personal:Education`;
  `Fees:Charges`→owner's choice; `Cash`→`Personal:Cash`; then delete the emptied
  `Bills:Utilities` and `Fees` parents.)

## Verification

- `python -m compileall mfl_desktop` clean.
- `tests/test_category_import_map.py` (Qt-free) 11/11 — normalisation, path
  round-trip, the seeded Needs Review row, find-only never creates, merge/delete/
  reparent each record the right mapping and a re-import follows it, match-only
  routing on/off, an explicit mapping overriding an existing same-named path, and
  blank → Uncategorised.
- `tests/test_category_map_dialogs_smoke.py` (offscreen Qt) 4/4 — the match-only
  checkbox round-trips the setting both ways and the mappings dialog lists/forgets.
- Demo file (older schema) upgrades through migration 0032 and gains a Needs
  Review category; `test_iri_boundary` (seed) and `test_qif_cash` still green.
