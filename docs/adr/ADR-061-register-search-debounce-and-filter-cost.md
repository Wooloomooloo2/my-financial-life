# ADR-061 — Register search performance (debounce + cached haystack + coalesced status)

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-041 (register date-window filter — its *Negative* section predicted "`All` is still slow on huge accounts… a deeper fix … is a separate optimisation if All-view performance becomes a real complaint"; this is that follow-on, scoped to *search* rather than *sort*). ADR-010 (Repository/model/proxy layering). ADR-051 (split rows — the proxy still surfaces a "—Split—" parent when filtered by a line category, unchanged here).

---

## Context

The owner reported that opening **Chase Checking** with **Show: All**, then typing in the register Search box, froze the screen "as soon as I type a letter."

Profiling the real `mfl_dev.db` (heaviest account = Amex Blue, **13,550 transactions** on the All window) located three costs, all run **synchronously on the UI thread for every keystroke**:

| Per-keystroke cost (13,550-row All view) | Time |
|---|---|
| `filterAcceptsRow` over every row — rebuilding a 7-field haystack + two `f"{x:.2f}"` formats + `join` + `lower` per row | ~15 ms |
| `_update_status` walk (one full pass: `mapToSource` + `Decimal` add per visible row) | ~25 ms |
| …and `_update_status` fired **2–3× per keystroke** (it was wired to `rowsRemoved` + `rowsInserted` + `layoutChanged`, all emitted by one filter invalidation) | ×2–3 |
| Debounce | none — every keystroke ran the whole thing immediately |

So a single keystroke cost ≈ 15 ms (filter) + ~50–75 ms (status ×2–3) ≈ **90 ms**, with **no debounce** — and a fast typist stacked these synchronous passes back-to-back, which reads as a frozen screen. Backspacing (broadening the filter) additionally pays the Python `lessThan` re-sort that ADR-041 already flagged, since broadening re-inserts rows into sorted order.

This is the search-time analogue of ADR-041's sort problem. ADR-041 made the *load* and *sort* of the **All** window opt-in via windowing, but search over an already-loaded All view was left untouched — and that is exactly the scenario the owner hit.

---

## Options considered

### A — Debounce the search box only

A timer so the filter runs once after typing settles, not per keystroke. Pros: small, removes the per-keystroke stacking that causes the *frozen-while-typing* feel. Cons: the one settled pass is still ~90 ms on a 13.5k view (filter rebuild + triple status walk), so a noticeable hitch remains after each pause; doesn't fix the underlying per-row work. **Necessary but not sufficient — kept, combined with B + C.**

### B — Precompute the search haystack once per row (cached "blob")

Build each row's lowercased searchable string once at load and reuse it on every keystroke, so `filterAcceptsRow` is a single substring test. Pros: turns the filter pass from ~15 ms to ~2 ms (measured, ~7×); the string-building was pure per-keystroke waste since the source fields don't change between keystrokes. Cons: a parallel cache (`_search_blobs`) must stay index-aligned with `_rows` — refreshed on load and on inline edit. The two write sites (`reload`, `setData`) are the only places `_rows` is mutated, so the surface is small and auditable. **Selected.**

### C — Coalesce the status-bar refresh

Route the proxy's `rowsRemoved`/`rowsInserted`/`layoutChanged` signals through a zero-delay single-shot timer so the 2–3 emissions from one filter invalidation collapse into **one** `_update_status` walk per event-loop turn. Pros: cuts the status cost by 2–3×; restart-on-emit is the standard Qt coalescing idiom. Cons: status text updates one event-loop turn late (imperceptible). **Selected.**

### D — Move filtering / the visible-net into SQL (server-side search)

Push the search predicate (and the net sum) into the Repository. Pros: scales past in-memory limits. Cons: large change to the model/proxy contract; the live data (13.5k rows) filters in ~2 ms once cached, so this is unwarranted now. **Rejected as premature** — revisit only if accounts reach hundreds of thousands of rows.

### E — Disable `dynamicSortFilter` to avoid re-sort on filter

Stop the proxy re-sorting when broadening the filter. Rejected: forward-typing (the reported freeze) only *removes* rows and pays no sort cost, so this targets the wrong case; and turning it off risks the view not reflecting edits. The Python-`lessThan` sort cost on broadening is ADR-041's already-documented inherent cost, out of scope here.

---

## Decision

Combine A + B + C — all view/model-layer only. **No migration, no schema change, no Repository change.**

**Debounce (A).** The Search `QLineEdit`'s `textChanged` no longer calls `proxy.set_search` directly. It stashes the text in `self._search_pending` and (re)starts a single-shot `QTimer` (200 ms); the timeout applies the pending text to the proxy once. A burst of keystrokes therefore yields one filter pass.

**Cached haystack (B).** `TransactionTableModel` holds `self._search_blobs: list[str]`, index-parallel with `self._rows`, built by `_build_search_blob(row)` (the exact former inline logic: payee, memo, posted_date, security symbol + name, and both `f"{amount:.2f}"` / `f"{abs(amount):.2f}"` comma-free forms, joined and lowercased). Built in `reload()`; the single mutated row is refreshed in `setData`. `search_blob_at(source_row)` exposes it. `TransactionFilterProxy.filterAcceptsRow` replaces its per-row haystack construction with `self._search not in model.search_blob_at(source_row)`. The comma-stripping needle contract (`set_search` strips commas; the formats are comma-free) is preserved — verified byte-identical to the old logic across all 13,550 rows.

**Coalesced status (C).** A zero-delay single-shot `self._status_timer` drives `_update_status`; the four proxy signals connect to `_schedule_status_update` (which restarts the timer). Direct `_update_status()` calls on view swap are unchanged (immediate).

---

## Consequences

### Positive

- **The freeze is gone.** Typing applies no filter until ~200 ms after the last keystroke; the settled pass is ~2 ms filter + one ~25 ms status walk instead of ~90 ms per keystroke ×(however fast you type).
- **The filter pass is ~7× cheaper** (15 ms → 2 ms over 13.5k rows) and the status walk runs once per pass instead of 2–3×, so even the settled hitch is small.
- **Search correctness is unchanged** — same fields, same comma-insensitive amount matching, same split-line surfacing.

### Negative / trade-offs

- **~200 ms latency between typing and results** by design. Tunable via the timer interval; 200 ms tested as the sweet spot between responsiveness and not filtering mid-word.
- **A second parallel cache to keep in sync.** `_search_blobs` must track `_rows`. Mitigated: only `reload()` and `setData` write `_rows`, and both update the blob; a desync would surface as stale search results, not a crash.
- **Backspacing still pays the Python-`lessThan` re-sort** on huge All views (ADR-041's inherent cost). Not addressed here; a C++-side sort key remains the deeper fix if it ever becomes a complaint.

### Ongoing responsibilities

- **Any new write to `TransactionTableModel._rows` must refresh `_search_blobs` at the same index** (or rebuild it), or search goes stale for that row. The two current sites are the contract.
- **Any new register feed/field that should be searchable goes into `_build_search_blob`**, not into `filterAcceptsRow` — the proxy no longer assembles the haystack.
- The visible-net walk in `_update_status` stays O(visible). If it ever needs to scale further, fold the net into the proxy (option D's neighbourhood).

---

## Amendment (2026-06-14) — category names are now in the search haystack; Category filter combo removed

With the per-row haystack already cached, **`category_name` was added to `_build_search_blob`**, so the general Search box now matches a transaction's category too (typing "groceries" filters the register). That made the dedicated **Category filter combo redundant, so it was removed** from the register filter bar (and `_populate_category_combo` + its per-view rebuild calls deleted). The proxy keeps its `set_category_id()` capability — it's simply no longer driven from the register bar — so the ADR-051 split-line surfacing path stays intact for any future caller. `Repository.distinct_category_ids_for_account` is now unused by the register but left in place. Split rows still search on their own `category_name` (the "—Split—" parent's stored category); searching by a *split line's* category name is not covered (the row carries line *ids*, not names) — acceptable for v1, revisit if it comes up.
