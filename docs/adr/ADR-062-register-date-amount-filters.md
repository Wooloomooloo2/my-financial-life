# ADR-062 — Register date-range and amount-range filters (Filters popover)

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-041 (register date *window* — the performance load bound; this layers an explicit range on top and can widen that load). ADR-061 (the proxy filter path + cached search; date/amount filtering rides the same in-memory proxy). ADR-033 amendment (`CustomPeriodDialog` — the From/To date-picker pattern this mirrors). Part of the "Arc A — register tidy-up" cluster (A5).

---

## Context

The register had three filters — free-text Search, the "Show" date *window* (ADR-041, a load-performance lower bound), and Status. The owner wanted two more, both expressed as ranges: **a date From/To** and **an amount Min/Max** ("show me what I spent between these dates", "find the transactions over £500"). The filter bar was already near capacity (Search / Show / Status + the new ＋New Transaction and Reconcile buttons from A3).

Three independent UX questions, all put to the owner via `AskUserQuestion`:

1. **Date range vs the existing Show presets** → **separate From/To fields** (not folded into the Show combo as a "Custom range…" entry). The owner wants an explicit range that reads as its own thing, distinct from the quick-window shortcuts.
2. **Amount semantics** → **signed** (Min/Max apply to the signed amount, so `Max = −500` isolates large *outflows*), not magnitude. More precise for direction-specific queries.
3. **Placement** → a **"Filters ▾" popover** button (not a permanent second row, not inline on the bar), with the date + amount fields and a **Clear filters** button inside, and an **active-filter dot** on the button when anything is set.

---

## Decision

Add a **"Filters ▾" button** to the register filter bar. It opens a small non-modal popover panel (`mfl_desktop/ui/register_filters_popover.py`) holding:

- **Date From / To** — two `QDateEdit`s with calendar popups, each independently *enabled by a checkbox* (an unchecked end = unbounded on that side, so you can say "everything after 1 Jan" with no upper bound). Inclusive both ends.
- **Amount Min / Max** — two `QDoubleSpinBox`es, each gated by its own checkbox, applied to the **signed** amount.
- **Clear filters** — resets all four to off.

The panel applies **live** (each change emits a `filters_changed` signal carrying the four optional values); the register re-filters immediately. The button shows a trailing "●" when any of the four is active.

**Where the filtering happens.** Date and amount are **proxy-level** filters in `TransactionFilterProxy` (new `set_date_range(from, to)` / `set_amount_range(min, max)` + checks in `filterAcceptsRow`), exactly like Search — they run in-memory over the already-loaded rows, which ADR-061 made cheap. Dates compare lexicographically on the stored `'YYYY-MM-DD'` strings (no parsing); amounts compare on the `Decimal`. **No Repository or schema change, no model change beyond what already exists.**

**The one coupling — From can widen the load.** The "Show" preset (ADR-041) still governs how many rows are *loaded*. A From date earlier than the loaded window would otherwise match nothing (those rows aren't in memory). So the register's load bound becomes `_effective_since() = min(preset_since, filter_from)` (with `All`/`None` still meaning load-everything): setting From to a date before the current window **auto-widens the load** so the range is reachable, while To and amount stay pure in-memory filters (rows newer than To, or outside the amount band, are already loaded — just hidden). Clearing From reverts the load to the preset.

---

## Options considered

- **Date range as separate fields (chosen)** vs. a "Custom range…" entry in the Show combo. The combo-entry option reuses `CustomPeriodDialog` and keeps a single date control, but the owner preferred an explicit, always-discoverable From/To.
- **Signed amount (chosen)** vs. magnitude. Magnitude ("size between £100–£500, either direction") is more intuitive for the common case, but the owner wanted direction-specific power (large outflows) and accepted the slightly less obvious `Max = −500` idiom.
- **Popover (chosen)** vs. a permanent second filter row vs. inline. The popover keeps the bar clean and scales to future filters; the cost is one click to reveal, mitigated by the active-filter dot.
- **Proxy-level filtering (chosen)** vs. pushing date/amount into SQL. Pushing date into SQL (a real `until` bound) was considered, but the rows in any sane window are already loaded and ADR-061 made in-memory filtering ~2 ms, so a Repository change buys nothing here. From-widening reuses the existing `set_since` reload; it is the only load-affecting piece.
- **Live-apply popover (chosen)** vs. an OK/Cancel modal. Live matches the rest of the filter bar (Search/Status apply as you go) and the owner's preview showed a live panel.

---

## Consequences

### Positive
- Two new, frequently-wanted query dimensions with no schema/Repository change and negligible runtime cost.
- The bar stays uncluttered; the popover is the natural home for any further filters (e.g. a later "direction" or "cleared-only" toggle).
- From "just works" across windows — you never get a silent empty result because the data wasn't loaded.

### Negative / trade-offs
- **Signed amount is less obvious** than magnitude — surfacing large outflows means a negative Max. The field labels/placeholder hint at it; revisit with a magnitude+direction control if it trips the owner up.
- **From-widening blurs the Show/From boundary** a little (a From earlier than the preset effectively overrides the load lower bound). Documented; it's the least-surprising behaviour given the alternative is empty results.
- **A very old From on a huge account reloads a large window** (the ADR-041 sort cost returns for that load). Acceptable — it's an explicit, deliberate action, same as picking "All".

### Ongoing responsibilities
- **Any new register load path must use `_effective_since()`**, not `_current_since()` directly, or the From-widening silently stops working.
- **New proxy filters go in `filterAcceptsRow` with their own early-out**, and their setters must `invalidateRowsFilter()` — consistent with Search/Status/date/amount.
