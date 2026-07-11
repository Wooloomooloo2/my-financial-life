# ADR-150 — Net-worth hero carries a 12-month trend sparkline (computed off-thread)

**Date:** 2026-07-11
**Status:** Implemented (amended 2026-07-11 — see Amendment)
**Related:** ADR-119 (net-worth hero + clickable cards). ADR-075 (Home dashboard, `gather_*` pattern). ADR-121 (net worth over time, computed-not-stored replay). ADR-135 (net-worth history sampling / bars). ADR-055 (convert-before-summing FX). ADR-046 (period-scoped investment total return). ADR-026 (hand-rolled `paintEvent` charts). ADR-035 (background launch refresh via `QThreadPool`). ADR-149 (Home refresh defers widget destruction).

## Context

The Home net-worth hero (ADR-119) states one figure — `£281,642` — top-left of a full-width card and leaves roughly 70% of the card empty. It's the textbook "big number in a void": the number has no direction, no context, no sense of whether you're climbing or sinking. A visual review against the frontend-design guidance flagged it as the single highest-leverage screen to improve — "the hero should be a thesis, not a stat."

The obvious fix is to fill the card with the 12-month net-worth trend we already compute for the Net Worth screen's "Over time" tab (`net_worth_history.gather_net_worth_history`), plus a change indicator (this-month and 12-month deltas).

The catch is cost. That series is **computed-not-stored** (ADR-121): every account's transactions are replayed across each monthly sample date and FX-converted at that date. Measured on the owner's real file (26 accounts, ~35k transactions, 14 monthly samples):

| Work | Time |
|---|---|
| `gather_net_worth_history` (12-month monthly series) | **~400 ms** |
| `gather_home_data` (the rest of the dashboard, today) | ~168 ms |

Home is **not** built once. `RegisterWindow.changeEvent` refreshes it on every window re-activation (ADR-063/075), and it rebuilds after transaction edits. Folding a 400 ms replay into `gather_home_data` would more than triple Home's build time and stall the UI thread for ~0.4 s after every edit and every alt-tab back to the app. On a smaller file it's invisible; on the file the owner actually uses daily it's a regression I'd be shipping.

## Decision

**Keep `gather_home_data` on its fast path; compute the trend in a background thread and fold it into the hero when it arrives** — the number paints instantly, the sparkline fades in a beat later.

Three pieces:

1. **`home_dashboard.compute_net_worth_trend(repo, today, display_ccy=None, *, months=12)`** — a Qt-free helper (the ADR-075 `gather_*` sibling) that samples the last 12 month-ends via `net_worth_history.month_end_samples`, calls the existing `gather_net_worth_history` with the same family→asset/debt map the Net Worth screen uses (`cash/investment/property/vehicle` = asset, `credit/loan` = debt), and returns a small `NetWorthTrend`: the `(date, net)` points plus the this-month and 12-month deltas. Returns `None` when there's < 2 points (a brand-new file), so the hero degrades to number-only. It is **not** part of `HomeData` — the fast path never pays for it.

2. **`ui/net_worth_sparkline.py`** — a hand-rolled `paintEvent` widget (ADR-026): a single net-worth line with a soft accent area-fill and an emphasized endpoint dot. No axes, no legend, no gridlines — the report chart (`NetWorthHistoryChart`) owns that job; the hero wants a quiet trend, not a second report. Reads `tokens` at paint time, so it follows the live light/dark theme like every other chart.

3. **`HomeView` runs the compute off the UI thread** with the ADR-035 pattern: a `QRunnable` on `QThreadPool.globalInstance()` opens its **own** `Repository(db_path)` (a sqlite connection can't cross threads), computes the trend, and emits the result through a main-thread `QObject` signal (auto-queued across threads). The result is **cached against a cheap data signature** — `net_worth | account count | display currency | today` — computed from the already-gathered `HomeData`. While a signature's trend is cached, the hero renders the sparkline synchronously with no thread at all; when the signature changes (an edit that moved net worth, a new day, a currency switch) the cache misses and a fresh compute is kicked off. On arrival the handler calls `refresh()`, which now finds the trend cached for the current signature and paints it. `refresh()`'s ADR-149 deferred-destruction contract makes that re-entrant rebuild safe.

So the sequence on the owner's file: Home paints in ~168 ms with the number; ~400 ms later the sparkline and deltas appear; every subsequent activation with unchanged data paints the sparkline instantly from cache.

Rejected:

- **Compute synchronously in `gather_home_data`.** The measured +400 ms on every activation and every post-edit refresh is the whole problem; this is the thing not to do.
- **Synchronous but signature-cached.** Removes the per-activation cost but still stalls the UI thread ~400 ms on the first Home after launch and after *every* edit that moves net worth — and edits are frequent (the register is the app's main surface). Off-thread costs more code but never hitches.
- **Persist / incrementally maintain a net-worth series** (a materialized table updated on write). The right answer if this becomes a hot path in several places, but it's a storage-model change with its own invalidation burden (every txn edit, FX rate, price, account change dirties it). Not worth it for one card; computed-not-stored + a background thread + a signature cache gets the same felt result with no new persistent state. Revisit if a second consumer wants the same series live.
- **Embed the existing `NetWorthHistoryChart`.** It's a full report chart — axes, y-labels, legend, per-bar net markers, hover tooltips — far too busy inside a hero and sized for a report pane. A dedicated minimal sparkline is less code than configuring that one down.
- **A count-up animation on the number.** Gimmicky, and it fights the "quiet everything around the one signature" discipline. The sparkline's draw-in is the only motion, and it's cheap.

## Amendment (2026-07-11) — 30-day + 12-month deltas

Owner feedback on the first cut: the summary said "this month" while the line spans 12 months — mismatched windows. And the "this-month" figure was really month-to-date (net now vs the last month-end), which drifts with the calendar (1 day's worth on the 1st, a month's worth on the 31st).

The hero now summarises **two fixed rolling windows** that bracket the chart's span: **last 30 days** and **last 12 months**, each on its own line, coloured by direction. The 30-day figure is a true rolling delta — net now vs net exactly 30 days ago — sourced by riding a single extra "30 days ago" sample along in the same `gather_net_worth_history` replay (kept out of `points` so the chart stays a clean monthly cadence; no second replay). The 12-month change moves from a muted footnote to a first-class coloured line with its own percentage; the account count drops to the muted line on its own. `NetWorthTrend.change_month{,_pct}` became `change_30d{,_pct}`, and `change_year_pct` was added.

### Investment Performance card matches the same windows

Same feedback applied to the Investment Performance card, which showed **all-time unrealised** top gainers/losers — a different time basis from the hero. It now leads with the portfolio's **last-30-days** and **last-12-months** performance (the same two lines, reusing the hero's delta formatting), followed by the 12-month **top movers**.

The metric is **true return, not value change**: a window's return excludes contributions, computed per investment account via the existing returns engine (ADR-046) as `terminal_value − opening_value + Σ(in-window cash flows)` — the identity its IRR bracketing already provides — summed across accounts and FX-converted per leg (ADR-055). Using the engine's own `total_return` would have been wrong here: it folds in *lifetime* unrealised appreciation, so the 30-day and 12-month figures would differ only by windowed realised/dividends and both read as "all-time-ish" — defeating the window comparison the owner asked for.

Two consequences of the metric:

- This is heavier than the old all-time snapshot (two windows × a per-account returns replay), so it moves **off the fast path** into the *same* background pass as the trend — one thread, one background `Repository`, both cards cached under one signature and painted together. The old synchronous `_investment_perf` (and its all-time `HomeData.invest_gains/losses`) is removed; `gather_home_data` no longer touches investments at all, so the fast path got a little cheaper too.
- The per-security **percentage** is return-on-opening-value, which is unstable for a position built mostly *within* the window (a tiny opening base makes a real £2.6k→£45k holding read as "+1591 %"). Those percentages are suppressed (|return/opening| > 3.0 → show the money gain only); the money figure is always well-defined and is what the movers are ranked by.

## Consequences

- The hero reads as a thesis: the number, its direction (▲/▼ this month, with %), the 12-month change, and the shape of the year — without the UI thread ever stalling on the replay.
- Home's fast path is unchanged at ~168 ms. The trend is amortized: paid once per data change, then served from cache across activations.
- There is a visible ~400 ms window on first load (and after a net-worth-moving edit) where the hero shows the number without the sparkline. This is deliberate — a beat of "number, then trend" beats a third of a second of frozen window. The number is never wrong or missing in that window.
- The background `Repository` is opened and closed per compute (mirrors the ADR-035 FX/price launch runnables). A stale result that arrives after the signature has moved on is dropped by the signature check, not painted.
- New file / < 2 months of history → `compute_net_worth_trend` returns `None` and the hero is exactly the ADR-119 number-only card. No empty-chart state to design.
- The sparkline is a fourth hand-rolled chart (ADR-026); it shares the theme-at-paint-time convention, so the ADR-076 live toggle repaints it for free.
- Same computed-not-stored source as the Net Worth screen (ADR-121/055), so the hero's trend and the report's "Over time" tab can never disagree.
