# ADR-124 — Budget annual matrix: drop the inline rollover annotation (background + tooltip instead)

**Date:** 2026-06-30
**Status:** Accepted
**Related:** ADR-058 (the budget arc — the 12-month editable matrix). ADR-025 (rollover policy / carry-forward). ADR-076 (semantic light/dark tokens — `rollover_bg`). The budget monthly progress view (R3) which renders the same carry differently and is deliberately left unchanged.

## Context

In the **annual** budget view (the `QTableView` matrix in `budget_window.py`), each month column is pinned to a fixed **86 px** (`setColumnWidth(c, 86)`). A normal Budget cell shows a figure like `1,100.00`, which fits. But a Budget cell that carries in an unspent **rollover** rendered the figure *plus* an inline annotation — `1,100.00  (+205.00)` — which is far wider than 86 px. Qt's table elides overflow on the right, so the `(+205.00)` rollover part was clipped to `1,100.00  (+2…` (or worse), and on a wide window the figure itself could be eaten. The owner reported that the rolled-over amount on the annual view "is cut off so it's not visible" (screenshot, 2026-06-30).

The cell already had two *other* signals for the rollover that were **not** truncated:
- a yellow **`_rollover_bg()`** cell background (`tokens.c("rollover_bg")`), and
- a reconciliation **tooltip** ("Budgeted 1,100.00 + 205.00 rolled over = 1,305.00 available this month").

Plus the line's label column carries a **↻ glyph** for any rollover-enabled line. So the inline annotation was the *only* one of four cues that didn't fit — and it was the one breaking the layout.

## Decision

**Remove the inline `(+carry)` annotation from the annual matrix's `DisplayRole`.** The Budget cell now shows just the figure (`_fmt(value)`), which fits the existing 86 px column with no clipping. The rollover is still communicated by the **yellow background + the tooltip + the ↻ label glyph**, which together carry the signal without overflowing the cell.

The `carry` value is still computed in `data()` — it now drives only the `BackgroundRole` (yellow) and `ToolTipRole` (the reconciliation line); it no longer feeds `DisplayRole`.

Chosen by the owner over two alternatives that were offered:
- **Auto-fit only the rollover columns** (widen the few month columns that contain a carry to fit `(+205.00)`, leave the rest at 86 px). Rejected: introduces ragged column widths and horizontal scrolling on a view whose whole point is to see all 12 months at once.
- **Compact carry in a smaller muted font via a paint delegate** (draw a small `(+205)` inside the 86 px cell on the same line). Rejected: more code (a custom `QStyledItemDelegate`), and a tiny superscript-style figure is hard to read in a dense grid anyway.

The owner picked the cleanest option: the dense annual grid relies on colour + hover for the carry detail, and keeps every budget figure fully legible.

## Consequences

- The annual matrix no longer clips any cell; budget figures are always fully visible. The exact carried amount moves one hover away (the tooltip), and the *presence* of a carry stays obvious at a glance (yellow fill + ↻).
- **Scope is the annual matrix only.** The budget **monthly progress view** (`budget_monthly_view.py`) shows the carry inline as `{actual} / {available}  (+205.00)` in a flexible-width label that grows to fit — it was never truncated, so it is left exactly as-is. The two views now differ in carry presentation (grid: background+tooltip; monthly: inline), which matches their different densities: the grid has no room, the monthly row does.
- View layer only — no schema, migration, repository, or `budget_calc` change. `available = alloc + carry` reconciliation is unchanged; Budget / Actual / Diff still line up (Diff already uses `available`, not the raw allocation).
- No column-width change was needed: dropping the annotation is what lets the existing 86 px columns hold the figure.
- Verified offscreen on `mfl_public.mfl` (190 rollover cells) and `mfl_dev.mfl` (54): every carry cell now renders the bare figure with no inline parenthesis, while still returning the `_rollover_bg()` background and a non-empty reconciliation tooltip.
