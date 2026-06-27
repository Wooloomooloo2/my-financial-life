# ADR-116 — Quick-action toolbar (Home · Update Prices · Update Rates)

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
(`Qt.ToolButtonTextOnly` — there are no per-action icons), with three actions:

- **Home** — `select_home()` on the sidebar + `_show_home()`, mirroring the
  launch landing path.
- **Update Prices** — fetches the latest security prices **directly**, no
  dialog: the same synchronous, `force=True` path the Securities dialog's
  Refresh-Now uses (`prices.refresh_latest_prices_into`), under a wait cursor,
  then `_refresh_sidebar_balances()` so changed market values show at once.
- **Update Rates** — the FX equivalent (`fx.refresh_latest_into`,
  `force=True`), then refresh sidebar balances so converted figures update.

Both update handlers:

- **Route to the relevant dialog when the API key is unset** (Tiingo /
  openexchangerates) rather than silently no-op'ing — an info box explains where
  to add the key, then opens *Manage ▸ Securities / Currencies*.
- Report the outcome on the **status bar** (`"N prices refreshed"`) — a
  transient, non-modal confirmation, matching the existing "Transaction added"
  idiom — and surface a `QMessageBox.warning` only when the `RefreshResult`
  carries errors (e.g. a fund ticker Tiingo doesn't cover, or a 429 back-off).

Synchronous-with-wait-cursor (not threaded) is deliberate and consistent with
the two dialogs' own Refresh buttons: it's one call per ticker / one FX call, a
few seconds for a personal portfolio, and the underlying refreshers already
catch network/rate-limit errors into `RefreshResult.errors` rather than raising.

## Consequences

- The three most-used navigations/refreshes are now one click from anywhere in
  the main window; Home is no longer hidden in the sidebar.
- No new refresh logic — the toolbar reuses the exact functions the
  Securities/Currencies dialogs call, so behaviour (24h skip bypassed by
  `force=True`, rate-limit back-off, per-symbol error collection) is identical
  and stays single-sourced. Those dialogs remain the place for API keys, manual
  prices, and history backfill.
- View layer only; no migration, no schema change.
- The toolbar gives the P7 round a home for further quick actions if wanted
  (e.g. an "Update all" that chains prices + rates + bank feeds, per the
  backlog's F2 note) — out of scope here.
