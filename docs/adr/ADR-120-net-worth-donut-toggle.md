# ADR-120 — Net Worth: one big donut with an Assets / Debts toggle

**Date:** 2026-06-29
**Status:** Accepted
**Related:** ADR-067 (the two-ring Net Worth donut — the no-pies exception). ADR-083 (outer-ring slice → Account Summary drill-down). ADR-055 (FX-convert before summation). ADR-119 (MRL-style chrome — the page-header retrofit that immediately preceded this).

## Context

The Net Worth window's centre (summary) panel showed **two** donuts: a large Assets donut and, in a row below beside the legend, a small **fixed 190×190** Debts donut. Each donut renders the side's total in its hollow centre. At 190 px the centre hole is only ~70 px across, so a real debt total ("£208,619" in the owner's file) elided to nothing useful — the number didn't fit. Reported by the owner.

The debt total is *already* shown twice elsewhere (the right-hand **Debts** column header, and now the donut's own caption), so the cramped centre number was both unreadable and redundant.

## Decision

Replace the big-assets / small-debts pair with **one big donut** in the centre panel plus an **Assets | Debts segmented toggle** above it (offered to and chosen by the owner over the simpler "just drop the debts centre number" option, because it gives both sides a full-size, readable view).

- The donut fills the panel's vertical space (≈460 px wide in a maximised window) — the centre total now fits with room to spare.
- The toggle is the same pill-button pattern as the Income & Expense composition toggle (ADR-113): two checkable `QPushButton`s in an exclusive `QButtonGroup`, themed via `tokens.themed` (checked = brand `accent` fill), so it tracks light/dark.
- Per-refresh the window computes **both** sides' donut segments + totals once and **caches** them (`_side_segments` / `_side_total` / `_family_totals`); toggling calls `_render_active_side()` which redraws from the cache — no recompute, no re-query.
- The **legend** below the donut now shows only the **active** side's family colour key (the toggle already names the side, so the old ASSETS / DEBTS section headings were dropped).
- An all-asset file falls back to Assets and **disables** the Debts toggle button (`_side_segments["debt"]` empty).
- Drill-down is preserved: the single donut's `account_clicked` → `account_activated` works for whichever side is shown (ADR-083).

## Consequences

- The debts composition is now a first-class, full-size view instead of a thumbnail; both totals are legible.
- You see one side at a time (the trade for the size). The right-hand Assets and Debts columns still show both sides' trees simultaneously, so nothing is hidden overall.
- View layer only — no schema, migration, repository, or compute change. `_compute_type_totals` / `_donut_segments` / FX handling (ADR-055) are untouched; only the centre panel's widget tree and render path changed. The now-unused second `DonutChart` and the `_heading_row` legend helper were removed.
- Verified offscreen on `mfl_public.mfl`: toggle swaps donut data + legend (Assets 5 segs/£490k, Debts 2 segs/£209k) and restores from cache; the full-size donut paints with the centre total intact; the Debts button disables when there are no debts; dark-mode toggle restyles the pill buttons; the show-closed refresh drives the new single donut.
