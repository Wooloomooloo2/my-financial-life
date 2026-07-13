# ADR-160 — Home stops rebuilding itself on navigation and on background-card arrival

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-156 (the activation-refresh guard + `data_generation` — this finishes the job it started). ADR-075 (Home as stack page 0). ADR-150 (heavy Home cards computed off-thread). ADR-149 (Home refresh defers widget destruction).

## Context

ADR-156 fixed the freeze when *closing a report* by guarding `RegisterWindow.changeEvent` — the activation path — with `HomeView.refresh_if_stale()`. It claimed that was the whole of the problem. It was not.

Instrumenting a **real launch against the owner's live file** (a watchdog on the UI thread plus timers on the hot paths) showed Home rebuilding itself **five times in the first four seconds**, before the user had touched anything:

```
[2.12s]  206.5 ms  HomeView.refresh  (FULL REBUILD)
[2.27s]  142.4 ms  HomeView.refresh  (FULL REBUILD)
[3.09s]  425.8 ms  HomeView.refresh  (FULL REBUILD)
[3.31s]  217.2 ms  HomeView.refresh  (FULL REBUILD)   ### UI FROZE 797 ms
[3.74s]  179.6 ms  HomeView.refresh  (FULL REBUILD)
```

1,169ms of synchronous rebuild, including a **797ms UI freeze**. None of it went through the path ADR-156 guarded. Two separate callers were still rebuilding unconditionally:

**`RegisterWindow._show_home()`** called `refresh()` outright. It sits on the sidebar's *selection* path (`_on_sidebar_change` → `_show_home`), and the sidebar's selection fires more than once during startup — and again every time the user returns to Home, whether or not anything changed.

**`HomeView._on_bg_ready()`** called `refresh()` — a **full** rebuild, re-running the entire query pass — purely to fold the off-thread net-worth trend and investment-performance cards (ADR-150) into a dashboard *it had itself drawn moments earlier*. `gather_home_data` is ~85% of a refresh (480ms of 553ms measured against the live file), so the whole re-query was waste: the data provably had not moved, only the presentation had.

The second one is the more interesting mistake, because it is self-inflicted: the off-thread computation added in ADR-150 to *avoid* blocking the UI was paying for itself with a second full synchronous rebuild on arrival.

## Decision

**Two changes, one principle: never rebuild what the user cannot tell apart from what is already on screen, and never re-query data you already hold.**

### 1. Navigation checks the freshness token

`_show_home()` calls `refresh_if_stale()` — the same guard ADR-156 put on the activation path. Returning to Home on unchanged data is now free. A real edit still bumps `data_generation` and still redraws, so the ADR-075 guarantee is untouched.

### 2. The background cards redraw without re-querying

`HomeView.refresh(reuse_data=True)` skips `gather_home_data` and rebuilds the widgets from the `HomeData` of the last render. `_on_bg_ready` is the only caller, because it is the only place that *knows* the data hasn't moved — it is decorating a dashboard it just drew.

It is not a blind shortcut. The cached data is stamped with the freshness token it was gathered for, and `reuse_data` **re-gathers anyway** if that token no longer matches — so a write that landed while the worker was out (a background price refresh, an edit in another window) is picked up rather than papered over. `set_repo` (File ▸ Open) drops the cache outright: that `HomeData` belongs to the old file.

### Rejected

- **Update only the hero card in `_on_bg_ready`.** The tempting surgical fix, and wrong: the background payload feeds *both* the hero's sparkline **and** an investment card inside the grid (`_build_cards(data, invest)`). Replacing both means replacing most of the widget tree anyway — for no saving over the widget rebuild, and with real ADR-149 use-after-free risk, since Home rebuilds can run inside a card's own mouse event. Reusing the data gets ~85% of the win with none of the danger.
- **Debounce `_show_home`.** Hides repeated rebuilds behind a timer instead of not doing them. The rebuilds are redundant, not merely bunched.
- **Cache inside `gather_home_data`.** Would help every caller, but it is a pure function over the repo and the right place for the memo is the generation token that already exists (ADR-156), not a second caching layer with its own invalidation rules to get wrong.
- **Suppress the ADR-150 background pass when Home isn't visible.** Treats a symptom of the churn rather than the churn, and Home is the launch page anyway.

## Consequences

- Measured on the live file, startup goes from **5 rebuilds / 1,169ms** to **2 rebuilds / 405ms** — one full gather (305ms) plus one widget-only redraw (100ms). The 797ms UI freeze is gone.
- Returning to Home from a register, repeatedly, now costs nothing.
- The background-cards arrival costs a widget rebuild instead of a full query pass — roughly 100ms instead of 400ms, and it can no longer show stale data, because a moved token forces the re-gather.
- Together with ADR-156, **all three** paths that rebuild Home (activation, navigation, background-card arrival) are now guarded. That is believed to be all of them; `refresh()` remains unconditional for the callers that genuinely need it (first-run, market-data update, file swap), each of which follows a known write.
- 6 new tests (`tests/test_home_rebuild_churn.py`); 4 fail against the pre-ADR code. They count calls to `gather_home_data` rather than timing anything, so they pin the *behaviour* (did it re-query?) and won't turn flaky on a slow machine. Full suite 269/269. No schema change.
- **Still open** from the same investigation, not addressed here: quitting the app freezes for ~2.4s (three stalls at shutdown — likely the WAL checkpoint, ADR-057); an unexplained 1,596ms stall during launch that landed outside instrumented code; and `AccountSummaryWindow` / `BudgetWindow` / `TransactionsListWindow` still reload on every `WindowActivate` — the same anti-pattern, on three more windows.
