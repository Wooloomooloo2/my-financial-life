# ADR-067 — Net Worth two-ring donut (a scoped exception to "no pies")

**Date:** 2026-06-14
**Status:** Accepted
**Amends:** ADR-018 (the "no pies" rule — narrowed, not overturned; see below).
**Related:** ADR-019 (the original Net Worth report this window descends from). ADR-055 (Net Worth display-currency conversion + exclude-no-rate policy — reused unchanged). ADR-044 (investment market value feeding `compute_account_values`). ADR-026 (the hand-rolled `paintEvent` chart convention this follows).

---

## Context

Arc E round 3 (E4). The Net Worth window (ADR-019 / ADR-055) shows assets vs. debts in three columns, with a left-hand **summary panel** whose composition visual was a horizontal **proportional bar** — chosen specifically because ADR-018 ruled out pie charts ("I think they rarely convey accurate data"). The owner asked to replace that bar with a **two-ring donut**: inner ring = account *type*, outer ring = the *individual accounts* within each type.

This is a direct conflict with ADR-018, so it's an explicit owner decision to **narrow** that rule rather than ignore it. The reasoning that makes the exception coherent rather than a reversal:

- ADR-018's objection is about pies **for proportions that the eye must compare across categories or over time** — the spending report is a time series, where stacked bars + an average line are unambiguous and a pie would be actively worse.
- A net-worth side (assets, or debts) at a single instant is a **part-to-whole composition of one positive total**. That's the one case a donut is genuinely good at, and the two-ring (sunburst) form adds a second level — type → account — that a single bar can't show without becoming a dense stack of tiny segments.

So the rule becomes: **no pies/donuts for time series or cross-series comparison; a donut is allowed for a point-in-time composition of a single positive whole.** The spending, income & expense, payee, Sankey, and investment-returns reports are all unaffected.

A donut also can't represent a **negative** slice, and net worth contains debts (stored negative). Rather than abandon the form or fake it with magnitudes, the owner chose **two donuts**: a larger Assets donut and a smaller Debts donut, each over positive magnitudes (the debts donut flips the stored-negative balances to positive "amount owed").

---

## Decision

Replace the proportional bar in the Net Worth summary panel with **two `DonutChart` widgets** (`mfl_desktop/ui/donut_chart.py`):

- **Assets donut** (primary, takes the panel's vertical space) and **Debts donut** (smaller, fixed ~190 px, shown beside the legend). The Debts donut + its title hide entirely when there are no debts.
- **Inner ring = account type** (`account.type` → its `ACCOUNT_TYPES` label, e.g. *Current account*, *Savings account*, *Investment*); **outer ring = the individual accounts** within each type, tiling their parent type's angular span. Outer slices are **progressively lighter tints** of the type's family colour (`_shade`) so they read as the same type while staying distinguishable. The hollow centre shows the side's total.
- Hover gives a tooltip with each slice's amount and share. Angles are tracked **clockwise from 12 o'clock** and converted to Qt's (CCW-from-3-o'clock, 1/16°) convention only at `drawPie`; the two rings are drawn as full pies with the inner ring overpainting the inner band and a punched centre hole (the annulus trick), with thin white separators.

`DonutChart` is a generic, stateless `paintEvent` widget (`set_data(segments, center_label, center_sub, symbol)` / `show_empty`) taking `DonutSegment` (a type) each holding `DonutChild` outer slices (accounts). The Net Worth window builds the segments in `_donut_segments(type_totals, kind)` from the **already-FX-converted** per-account values, so the existing ADR-055 currency handling carries over verbatim: values are in the chosen display currency, and an account with **no rate is excluded** from the donut (it still appears in the missing-rate banner, never folded in at 1:1). The family-level legend, the Assets/Debts columns, the display-currency selector, and the banner are all unchanged.

---

## Options considered

- **Donut for this view, keep "no pies" everywhere else (chosen)** vs. holding the line with the proportional bar vs. overturning ADR-018 wholesale. The narrowed rule keeps ADR-018's intent (no pies for time series / comparison) while allowing the one shape that suits a point-in-time composition.
- **Two donuts — assets + smaller debts (chosen)** vs. assets-only (debts only in the column) vs. one donut over signed magnitudes. One signed donut is misleading (a big debt would look like a big holding); assets-only drops a visual the owner wanted; two donuts show both compositions honestly. (Owner pick via `AskUserQuestion`.)
- **Inner ring = account type (chosen)** vs. family (the 5 colour groups). Type is finer and is what the owner asked for; family stays as the legend/colour key, and the type colour is inherited from its family so the two stay visually consistent.
- **Reuse the existing standalone Net Worth window (chosen)** vs. a new saved `net_worth` report. The reserved `net_worth` saved type is still unused, but adding a second Net Worth entry point would be confusing; enhancing the existing window needs no migration and keeps one home for net worth. (Owner pick.)
- **Annulus via overpaint + drawPie (chosen)** vs. building `QPainterPath` ring sectors with `arcTo`. The overpaint trick is simpler and the white separators + centre punch give a clean result; path sectors are more code for no visible gain at these sizes.

---

## Consequences

### Positive
- The Net Worth composition now reads at two levels at a glance — which *types* dominate each side, and which *accounts* within them — which the single proportional bar couldn't show.
- Zero change to the money math: values come straight from the ADR-055 converted figures, so multi-currency, exclude-no-rate, and the banner behave exactly as before; only the visual changed.
- `DonutChart` is generic and reusable should another genuine point-in-time composition want one later (within the narrowed rule).

### Negative / trade-offs
- **It is a pie-family chart**, which ADR-018 set out to avoid. The mitigation is the explicit, narrow scope (composition of one positive whole, point-in-time only) — if it ever creeps toward time-series or cross-series use, that's a regression against ADR-018's intent.
- **Two donuts cost space** in the summary panel; the debts donut is deliberately small and hides when empty, but on a very narrow window the panel is busier than the old single bar.
- **Many tiny accounts** make thin outer slices that are hard to hover precisely — acceptable for a personal finance file's account count; a future "fold small accounts" option could help if it bites.
- **The proportional bar is now unused** by Net Worth (`proportional_bar.py` remains in the tree, no longer imported) — left in place rather than deleted, as a still-valid generic widget.

### Ongoing responsibilities
- Keep the donut strictly to point-in-time composition; reach for bars/lines for anything time-series, per the (now narrowed) ADR-018 rule.
- A new account *family* still needs a colour in `_FAMILY_VIEW`; the donut inherits type colours from there, so the two stay in sync automatically.
