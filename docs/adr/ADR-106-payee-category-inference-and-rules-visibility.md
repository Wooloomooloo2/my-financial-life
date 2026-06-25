# ADR-106 — Payee→category: history-inferred pre-fill + memories surfaced in Rules

**Status:** Accepted
**Date:** 2026-06-25

## Context

Two owner reports from real use of the live ledger:

1. Typing **"Scot Rail"** in the manual New Transaction dialog left the category
   **Uncategorised**, even though the owner has many ScotRail transactions
   categorised as *Commuting*.
2. The **Manage ▸ Rules…** screen was **empty**, despite payees that clearly
   auto-categorise.

Investigation of `mfl_dev.mfl` showed neither is a code defect — they expose a
design gap:

- There are **two independent** auto-categorisation mechanisms. (a) The
  **pattern rules** (`rule` table) the Rules dialog manages — the owner has
  **0** of these. (b) The **per-payee remembered category**
  (`payee.default_category_id`, ADR-072) — the owner has **19** (of 3,135
  payees). Auto-categorisation in practice runs almost entirely off (b), but
  the Rules screen only shows (a), so it looked empty/broken.
- The per-payee memory is **sparse** — it's only written when the user accepts
  the inline "remember this?" prompt (ADR-072) or sets it in the Payees dialog.
  Import source-categories and bulk edits never record it.
- The owner's payees are **fragmented**: ScotRail exists as `ScotRail`
  (memory = Commuting), `ScotRail London`, `First ScotRail Ltd`, `SCOTRAIL`,
  and `Scot Rail` — all separate, un-merged records. The owner typed
  `Scot Rail` (its own record, no memory), while only `ScotRail` carried one.
  The name lookup is also exact + case-sensitive.

Owner decisions (`AskUserQuestion`): (1) infer the category from the payee's
**own history** when no explicit memory exists; (2) **fold the remembered
categories into the Rules screen** so they're visible/manageable; (3) defer
merging the duplicate payee records.

## Decision

### 1. History-inferred category pre-fill (manual dialog only)

New read-only `Repository.most_common_category_for_payee(payee_id)`: the
most-frequent **non-Uncategorised, non-transfer** category across the payee
**and its aliases** (`expand_canonical_payee_ids`), ties broken by most recent
`posted_date`. `RegisterWindow._payee_default_category_for_name` — the callback
the New Transaction dialog uses to pre-fill — now returns the explicit
remembered category if set, **else** this inferred one, else None.

Scope is deliberately the **New Transaction dialog pre-fill** only. It does not
write a memory (the explicit default stays the source of truth), and it does
not change the inline register edit or the import categoriser — those keep the
existing explicit-only behaviour, so no silent bulk reclassification happens
from this change. Transfer-kind categories are excluded so a pre-fill never
drops in a category that needs a transfer partner.

### 2. Remembered categories shown in the Rules dialog

New `Repository.list_payee_default_categories()` → `(payee_id, name,
category_id)` for every payee with a memory. The Rules dialog gains a
**"Remembered payee categories"** section (mirroring its existing read-only
aliases section) — a selectable table of Payee → Auto-category with **Edit
category…** (reuses the Payees-dialog picker) and **Forget** (clears the
memory via `set_payee_default_category(pid, None)`). `rules_changed` fires after
either, so the register reloads. This makes "the whole automation picture in one
place" — the dialog's original stated goal — actually true.

## Alternatives considered

- **Make the prefill case-insensitive / fuzzy-match payee names.** Wouldn't
  help: `Scot Rail` and `ScotRail` are genuinely different records, not a case
  difference. The real fix for fragmentation is merging (deferred by the owner).
- **Auto-record a memory whenever any category is set** (import / bulk / manual)
  so the explicit store self-populates. Offered; owner chose history-inference
  instead. Inference needs no migration, reflects *all* prior categorisation
  immediately, and never silently writes data. Left as a future option.
- **Extend history-inference to the inline register edit and the importer.**
  Rejected for this round — it would silently reclassify on every import/edit;
  the owner reported the manual dialog specifically. The repo method is reusable
  if we later choose to.
- **A standalone "Payee categories" management screen.** Owner preferred folding
  into the existing Rules dialog over a new top-level screen.

## Consequences

- Typing a payee you've categorised before pre-fills its usual category without
  any setup — `ScotRail London` (41/41 Commuting) just works.
- The Rules screen now reflects the automation that's actually happening; the
  "empty even though payees auto-categorise" confusion is resolved.
- The two mechanisms (pattern rules vs per-payee memory) remain distinct but are
  now both visible in one dialog.
- Payee fragmentation is unaddressed by design this round; a merge/cleanup pass
  is the natural follow-up.
- No schema change, no migration, no new dependency.
