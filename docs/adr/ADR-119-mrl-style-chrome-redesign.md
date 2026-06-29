# ADR-119 — MRL-style chrome redesign

**Date:** 2026-06-28 (report-window page headers retrofitted 2026-06-29)
**Status:** Accepted (all arcs shipped; merged to master)
**Related:** ADR-026 / ADR-076 (Fusion + QPalette + token-driven global QSS — the design system this builds on). ADR-100 (brand teal `accent` + `brand_gold`). ADR-101 / ADR-117 (brand marks in persistent UI — sidebar header + publisher logo). ADR-102 (type scale). ADR-075 (Home dashboard + card system). ADR-116 (quick-action toolbar — folded into the new app header here). ADR-039 (sidebar accounts/reports tree — kept as primary nav).

## Context

The owner compared MFL side-by-side with its sister app **My Retirement Life (MRL)** and asked: *"How do we get MFL looking as sleek and professional as MRL?"*

The gap is **not** the design *system* — MFL already has semantic light/dark tokens (`ui/tokens.py`), a global QSS layer (`ui/theme.py`), the brand teal/gold, and a `homeCard` card style. The gap is **structural polish** that MRL applies on top:

1. **Density** — MFL rows are tight (22 px register, packed sidebar); MRL breathes (~40 px, 12 px padding). Density is the strongest "feels older" signal.
2. **Sidebar as a navigation panel** — MRL has a branded header *with subtitle*, grouped muted-caps sections, comfortable padding, a soft selection treatment, and a "PUBLISHED BY" footer. MFL's sidebar is a bare tree with a flat highlight and the publisher mark stranded in the status bar.
3. **Page headers** — every MRL screen opens with a title + grey subtitle; MFL jumps into content.
4. **Button hierarchy** — MRL primary actions are filled pills, secondary are ghost/outline; MFL renders every button the same outlined style.
5. **Top chrome** — MRL has no OS menu bar, just a clean nav + page header; MFL stacks a native `QMenuBar` *and* a `QToolBar`, which reads as classic-desktop.

A framing correction recorded for posterity: the owner's first MFL screenshot was the **register** (a data grid — correctly dense, like Quicken/Banktivity), while MRL's was its **dashboard**. The like-for-like target is MRL's dashboard vs MFL's **Home**. The register stays a grid; it is refined, not turned into a dashboard.

### Decisions taken with the owner (two forks)

- **Ambition:** full MRL-style redesign (drop the OS menu bar; MRL-style left nav; page-header bar; card dashboards; primary/secondary button system) — *not* a targeted polish pass.
- **Where the ~40 menu commands go once the OS menu bar is gone:** keep the **exact same menus, actions, and shortcuts**, but render them as flat custom-styled dropdown buttons in a custom app header. Nothing is moved, renamed, or lost; per-account / per-transaction verbs also remain in their existing context menus. (Rejected: reorganising commands into fewer destination surfaces — biggest change, most screens to build, highest risk of disrupting muscle memory; and a hybrid "More ⋯" overflow — loses discoverability of the folded commands.)

This is done on branch `redesign/mrl-style-chrome` (not master) because of its size — an explicit exception to the usual commit-on-master workflow.

## Decision

A token-first, component-driven uplift delivered in arcs, each verified offscreen and screenshotted before the next:

**Arc 1 — design system + sidebar + button hierarchy.** New semantic tokens for the chrome surfaces; QSS for a `mflPrimary`/`mflGhost` button system (dynamic property, so any button opts in without a subclass), a flush bordered sidebar, refined section headers and selection, denser-but-airier rhythm, and page-header typography. Sidebar gains a brand **subtitle** and the Garelochsoft mark moves from the status bar to a proper sidebar **footer** ("PUBLISHED BY"). The register's "New Transaction" becomes the filled primary.

**Arc 2 — app header replaces the menu bar + toolbar.** A new `ui/app_header.py` renders the eight menus as flat `QToolButton`s with `InstantPopup` (reusing the existing `QMenu`s built from the existing `QAction`s — same shortcuts), plus a right-hand utility cluster (the ADR-116 Update Prices/Rates/All actions as a split control, the dark-mode toggle, an account chip). A new `ui/page_header.py` carries a contextual title + subtitle + a primary-action slot. `RegisterWindow` stops calling `menuBar()`/`addToolBar` and feeds these widgets instead.

**Arc 3 — Home + report screens.** Home grows the MRL hero card (accent left-border, big number), a stat-card row, and section cards with header + action link; the report windows adopt the same page-header + card language.

**Principles:** all colour via `tokens` (light/dark parity preserved — every new token's light value is chosen first, dark second); no new runtime dependencies; pure-QSS and standard widgets only; every existing call site, signal, and shortcut preserved.

## Consequences

- The everyday surfaces (sidebar, header, Home) read as a modern flat app rather than a stock-Fusion desktop tool, matching MRL — while the register keeps the dense grid that a register should have.
- No commands, shortcuts, or context menus are lost: the native menu bar is replaced 1:1 by styled header dropdowns.
- View/QSS layer only — no schema change, no migration, no repository change.
- Larger surface area than a normal change, hence the branch + multi-arc sequencing with an offscreen render check and screenshot at each arc boundary.
- Light/dark parity is held by construction (new tokens carry both values; the global re-style/`notifier.changed` path is unchanged).

## Addenda (follow-ups during the build)

- **Account-holder chip + profile editor.** The header avatar derives its initials from the file's `person.name`. It reads from the *current* repo and is refreshed on every repo swap (`_adopt_repository` — covers File ▸ Open and Data Library loads — and after first-run), so it never shows a previously-open file's holder. There was no way to set the name (first-run only sets the *account* name, and the seed person is "Me"), so clicking the chip now opens a small `ProfileDialog` (live initials preview) that writes `person.name` via the new `Repository.set_person_name`.
- **Use-after-free fix on Home cards.** The clickable `_Card`/`_Row`/`_AccordionHeader` emitted `clicked` (whose slot can rebuild Home and delete the widget) *before* calling `super().mousePressEvent`, crashing on the freed C++ object once Home became reachable from both the sidebar and the new header Home button. Reordered to run base handling first and emit last.
- **Report-window page-header retrofit (2026-06-29).** Closes Arc 3's deferred report-window piece. `ui/page_header.py` gains an opt-in bottom hairline (`show_rule=`, default off so the register's header is unchanged) and a leading slot (`add_leading`, for a drill-down Back button left of the title). Every report window drops its hand-built `top_bar` (bold `_name_label`) + `top_rule` and adopts a `PageHeader`: **title = report name** (`Untitled` when bare; folder-prefixed name + `*` dirty mark when saved), **subtitle = report type** ("Spending Over Time", "Cash Flow", …). The existing verbs move into the header's action slot with the button hierarchy applied — **`Filter…` is the filled primary**, `Save`/`Save As…`/`Back` are ghost — and the per-report inline controls (Display-in / Group-by combos, Show-closed toggle) ride alongside. Retrofitted: Spending + Income (shared base), Income & Expense, Payee Spending, Category & Payee, Investment Returns, Cash Flow/Sankey (header above its separate controls row, so no `show_rule`), Investment Income, and Net Worth (static title, no save). Each `_update_name_label` now drives `set_heading()`; window titles unchanged. View/QSS layer only — no schema, repo, or compute change. Verified offscreen on `mfl_public.mfl`: all nine windows construct, bare/saved/dirty titles resolve, the `Filter…` primary carries the variant, dark-mode toggle re-formats the new border rule, and the two report-logic test scripts stay green.
