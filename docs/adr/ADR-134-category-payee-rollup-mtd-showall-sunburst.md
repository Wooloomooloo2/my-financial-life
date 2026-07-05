# ADR-134 — Category & Payee: rollup level, month-to-date, Show-all, category sunburst

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-068 (the Category & Payee report). ADR-030 (Top/Group/Leaf rollup model). ADR-067 (the two-ring donut / no-pies exception). ADR-082 (period presets, single source). ADR-084 (shared report filter base). ADR-133 (the retired Payee report, same session).

## Context

Owner cleanup asks for Category & Payee (ADR-068), which now carries the by-payee analysis that the retired Payee report used to (ADR-133):

1. **A month-to-date preset** — the report set (`REPORT_PRESETS`) had no "so far this month" option.
2. **A category rollup level when grouping by Category** — "same as the Category Over Time report" (ADR-030's Top / Group / Leaf). The report was hard-wired to the **Group** (budget-line) level.
3. **Show top → allow "Show all"** — the cap lived in the filter dialog as a spinner whose `0 = All` was undiscoverable.
4. **A category distribution pie in the bottom-right, in the same design as Net Worth** — owner picked the **two-ring sunburst** (inner = parent category, outer = children).

## Decision

Turn Category & Payee's top bar into the live "shape" controls (dimension + rollup + show-cap), keep the filter dialog for the "what data" filters, and add a category sunburst to the summary panel.

- **Month-to-date preset.** New `periods.CATEGORY_PAYEE_PRESETS = (mtd, quarter, 6m, ytd, 1y, 3y, custom)` — a dedicated set so the shared `REPORT_PRESETS` (used by Spending/Income/I&E) is untouched. Aliased in `reports/filters.py` as `CATEGORY_PAYEE_PERIOD_KEYS`; the filter dialog uses it. Keys are persisted in `filters_json`, so a dedicated set (not a mutation) protects the other reports and stable round-trips. Default period stays `1y`.

- **Rollup level.** `CategoryPayeeFilters` gains `rollup_level: str = "group"` (**group** = the historical behaviour and default; **top** = root; **leaf** = raw category), reusing the ADR-030 vocabulary. The window pre-builds all three leaf→bucket maps (`category_root_map` / `category_group_map` / identity) and swaps them live. A top-bar **Roll up** combo drives it, shown **only while grouping by Category** (inert for Payee, so it hides). Changing the level resets the drill (the bucket set changed — a level-1 fresh start, mirroring ADR-030). Aggregation and the drill-subset filter both key off the active map.

- **Show top / Show all.** Moved out of the buried filter-dialog spinner onto a discoverable top-bar **Show** combo — `Top 10 / 15 / 25 / 50 / 100 / Show all` (`Show all` stores `top_n = 0`, which `build_report` already treats as "no tail hidden"). A saved non-preset cap is added as a one-off item so it round-trips. The filter dialog no longer carries `top_n` (nor `rollup_level` / `group_by`) — it preserves them via `dataclasses.replace` — so there's one control per concept, no desync.

- **Category sunburst (bottom-right).** The right summary panel gains a **two-ring `DonutChart`** (the ADR-067 sunburst, the owner's pick over a flat ring): **inner ring = budget-line group** (Groceries, Transport…), **outer ring = the leaf categories** within, shaded lighter like the Net Worth donut. It's built from the full cached matrix and is **independent of the primary dimension, rollup, and drill** — a stable "where the money went by category" panel (so it stays meaningful even while grouping by Payee). Group colours come from the shared `chart_helpers` palette (`colour_for`).

Rejected: putting rollup in the filter dialog (Spending puts it there because it rebuilds a category checklist; here it's a live view control, better on the top bar next to Group by); mutating `REPORT_PRESETS` to add `mtd` (would change three other reports); a flat single-ring donut (owner chose the sunburst); making the sunburst follow the rollup level (a fixed group→leaf view reads more stably and needs no per-level rebuild).

## Consequences

- The category dimension can now be read at three depths; the top bar shows Group by / Roll up (category only) / Show; the filter dialog shrinks to Period + Transfers + Accounts.
- `rollup_level` is a new persisted field with a back-compatible default (`group` = prior behaviour), so old saved reports (none exist yet) auto-upgrade via the existing parse-and-reserialise path.
- The bottom-right sunburst gives a Net-Worth-consistent composition view without violating the no-pies rule any more than ADR-067 already does (point-in-time composition of a positive whole).
- No schema change, no migration — a new filter field + view controls.
- Verified headless (offscreen, live `mfl_dev.mfl`): the window builds; Roll up hides under Payee and returns under Category; leaf/top rollup + `Show all` (`top_n=0`) apply and re-render; the sunburst renders (`grab()`); the MTD preset is offered. Full self-running suite green.
