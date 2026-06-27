# ADR-116 — Quick-action toolbar (Home · Update Prices · Update Rates · Update All)

**Date:** 2026-06-27
**Status:** Accepted
**Related:** ADR-075 (Home dashboard as a sidebar landing page — the row this toolbar makes reachable in one click). ADR-044 / ADR-049 (Tiingo price refresh — `prices.refresh_latest_prices_into`). ADR-035 / ADR-065 (openexchangerates FX refresh — `fx.refresh_latest_into`). ADR-100/101/102 (P4 brand/icon/type polish — this is the P7 post-feature polish round).

## Context

Three things the owner uses often were buried:

- **Home** is only reachable as a row in the left sidebar (ADR-075). The owner
  flagged it as "easily missed."
- **Update Prices** lived three clicks deep in *Manage ▸ Securities… ▸ Refresh
  Now*.
- **Update Rates** (FX) lived three clicks deep in *Manage ▸ Currencies… ▸
  Refresh Now*.

The main window had **no toolbar at all** — every verb was in the menu bar or
the sidebar. The 2026-06-16 launch backlog's polish workstreams (P1–P6) didn't
name a primary toolbar; this is the first item of a new **P7 post-feature
polish** round.

## Decision

Add a persistent, non-movable `QToolBar` at the top of `RegisterWindow`
(`_build_toolbar`, called right after `_build_menus`), text-only
(`Qt.ToolButtonTextOnly` — there are no per-action icons), with four actions:

- **Home** — `select_home()` on the sidebar + `_show_home()`, mirroring the
  launch landing path.
- **Update Prices** — fetches the latest security prices **directly**, no
  dialog: the same synchronous, `force=True` path the Securities dialog's
  Refresh-Now uses (`prices.refresh_latest_prices_into`), under a wait cursor,
  then `_refresh_sidebar_balances()` so changed market values show at once.
- **Update Rates** — the FX equivalent (`fx.refresh_latest_into`,
  `force=True`), then refresh sidebar balances so converted figures update.
- **Update All** — runs prices + rates in one click (the backlog's F2 "Update
  all"). The shared refresh logic is factored into `_refresh_prices` /
  `_refresh_rates` cores that each catch their own exceptions into the returned
  error list, so one failing provider doesn't abort the other. Each provider
  runs only if its API key is set; a missing key is reported as **skipped** in
  the status line (e.g. `Updated 5 rates · skipped prices (no Tiingo key)`)
  rather than popping a dialog — one click must not spawn two modal asks.
  **Bank feeds are deliberately excluded**: they need interactive consent and
  keep their own *Manage ▸ Bank Feeds* dialog.

The single-action update handlers (used by Update Prices / Update Rates):

- **Route to the relevant dialog when the API key is unset** (Tiingo /
  openexchangerates) rather than silently no-op'ing — an info box explains where
  to add the key, then opens *Manage ▸ Securities / Currencies*.
- Report the outcome on the **status bar** (`"Updated N prices"`) — a transient,
  non-modal confirmation, matching the existing "Transaction added" idiom — and
  surface a `QMessageBox.warning` only when the refresh returns errors (e.g. a
  fund ticker Tiingo doesn't cover, or a 429 back-off).

The refresh runs **off the UI thread** (owner feedback: the first cut froze the
window during the fetch). A `QRunnable` on the global `QThreadPool` opens its own
`Repository` connection (sqlite3 connections aren't thread-safe; WAL lets the
worker write while the main connection reads) and hands the result back via a
`QObject`-carried signal — the same pattern `__main__`'s launch-time price/FX
refresh already uses. While it runs, the three Update actions are disabled (one
refresh at a time) and the status bar shows "Updating…". The completion signal
is connected to a **bound method of the window** (a main-thread `QObject`), never
a bare lambda — an unbound functor has no thread affinity, so the slot could run
on the worker thread and touch widgets.

**Refreshing the result must not move the user.** The first cut called
`_refresh_sidebar_balances()`, which re-selects an account — and on Home (no
account selected) falls through to selecting the *first* account, yanking the
user off the dashboard. The handler now calls `_refresh_after_market_update()`:
reload the sidebar **preserving selection**, and — because the sidebar's reload
has no "home" restore case (it falls back to All transactions) — re-assert Home
and refresh the dashboard when Home is the visible page. An account/register
view stays put.

**The toolbar is restorable.** Qt's built-in toolbar context menu can hide it,
and `toggleViewAction()` is the only way back — it's added to the **View** menu
("Quick Actions Toolbar"), checked-state synced to visibility.

## Consequences

- The three most-used navigations/refreshes are now one click from anywhere in
  the main window; Home is no longer hidden in the sidebar.
- No new refresh logic — the toolbar reuses the exact functions the
  Securities/Currencies dialogs call, so behaviour (24h skip bypassed by
  `force=True`, rate-limit back-off, per-symbol error collection) is identical
  and stays single-sourced. Those dialogs remain the place for API keys, manual
  prices, and history backfill.
- View layer only; no migration, no schema change.
- Update All covers the backlog's F2 "Update all" for the two non-interactive
  data sources (prices + rates). Folding in bank feeds — which need consent —
  stays with the Bank Feeds dialog; a future quick action could trigger an
  OFX-Direct-only background feed refresh if wanted.
