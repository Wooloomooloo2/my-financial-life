# ADR-026 — Visual style baseline (Fusion + custom palette) and chart-engine comparison

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-008 (PySide6 — desktop UI framework); ADR-018 (Reports framework + Spending Over Time chart); ADR-025 (Budget visualisations — same QtCharts complaint surfaced on the burn-down, now also moved to paintEvent here).

---

## Context

After ADR-025 shipped the third budget round, the owner flagged that the app reads "a bit 1990's Microsoft". Two distinct causes:

1. **App chrome** — on Windows, QWidgets render through the `windows11` native platform style. It looks dated relative to modern desktop apps (VS Code, Linear, Notion, even Spotify). The native style also ignores most QPalette overrides, so we can't tune it without rewriting widgets.
2. **Charts** — the polish backlog already flagged "Spending Over Time chart visuals" as 1990's: QtCharts ships with very flat defaults, default fonts/sizes, and limited room for typographic polish. The same complaint applies to the burn-down chart from ADR-025.

ADR-008 settled on PySide6 and is not in question. The question is what we do **inside** PySide6 to bring the visual baseline up to modern desktop standards without leaving Qt for Electron / Tauri / Flutter (each of which would discard the layered architecture in CLAUDE_CONTEXT and require a rewrite in JS or Dart).

This ADR records two decisions:

- The **adopted** visual style baseline.
- The **comparison harness** for the chart-engine choice — a runtime toggle between QtCharts, pyqtgraph, and a custom paintEvent widget, on the Spending Over Time report.

## Options considered

### App chrome — native Windows / Fusion + palette / qt-material / hand-rolled QSS (chosen: Fusion + palette + minimal QSS, leaving room to layer further)

- *Native `windows11` style*: ships out of the box, looks like Windows. Owner's complaint. The native styles also ignore most QPalette settings and produce inconsistent metrics between Windows / macOS / Linux when we ship to the owner's eventual non-Windows family members.
- **Fusion + custom QPalette + minimal QSS** (chosen): Fusion is Qt's cross-platform style — same metrics on every platform, responds well to palette overrides, and accepts QSS without fighting it. The MFL palette uses Tailwind v3 slate neutrals + `blue-600` accent (`#2563eb`), so the same vocabulary works for hand-rolled per-window CSS in feature dialogs. A short QSS block rounds inputs/buttons, tunes the table header, and brings the menubar/tooltip in line with the palette. Centralised in `mfl_desktop/ui/theme.py::apply_theme(app)`; called once in `__main__`.
- *qt-material / PyQtDarkTheme / BreezeStyleSheets*: third-party stylesheets that adopt Material or Fluent. Bigger visual jump, but each opinionated about typography and spacing in a way that constrains what we can do in feature windows. Reserved as the next step if Fusion + palette doesn't go far enough.
- *Hand-rolled QSS for the entire app*: most distinctive, most maintenance burden. Easier to layer on top of Fusion later than to rip out an opinionated theme.

The decision orders these in a no-regret way: Fusion + palette is the cheapest jump and leaves all later options open. If the owner concludes after living with it that we want more (Material-ish density, Fluent-ish hover states), we can adopt one of the third-party themes or hand-roll on top without undoing this work.

### Chart engine for Spending Over Time — QtCharts / pyqtgraph / custom paintEvent (chosen: comparison harness, winner TBD)

- *QtCharts* (status quo): ships with PySide6, no extra dependency, easy stacked-bar API. The complaint is purely visual — limited control over typography, axis-label rendering, and bar polish (no rounded corners; the default theme reads flat-but-not-modern).
- *pyqtgraph*: mature scientific-viz library, used widely in Python desktop scientific apps. Light-mode-configurable (`background=white, foreground=slate`), good API for stacked bars via `BarGraphItem`, currency tick labels via `tickStrings` override. Adds one dependency.
- *Custom paintEvent widget*: hand-rolled `QWidget.paintEvent` with `QPainter`. Maximum control — rounded top corners on the topmost stack segment, white 1px separators between segments for definition, dashed average line with a pill label, soft horizontal gridlines, hover tooltips via `mouseMoveEvent` hit testing. No new dependency. More code to own; the burn-down would need its own paintEvent variant if we go this route.

**Initial decision** (2026-06-06, morning): ship all three behind a runtime "Engine" combo in the Spending window's controls panel so the owner could compare side-by-side with real data (~1,300 transactions over six months).

**Comparison outcome** (2026-06-06, afternoon): owner picked the **custom paintEvent** variant. Quoted reaction: "paintevent looks the best and is still ultra-fast." QtCharts and pyqtgraph branches removed; the same paintEvent approach was ported to the burn-down chart at the same time. ADR moved to **Accepted**.

Why paintEvent won:
- Modern flat look (rounded top corners on the topmost stack segment, 1px white separators between segments, soft gridlines, pill-shaped dashed average label) is much closer to current desktop-app expectations than either QtCharts' default theme or pyqtgraph's scientific-viz defaults.
- Full control over typography (Segoe UI inheritance from the app QSS), currency formatting on the Y axis, and hover behaviour.
- No extra dependency (pyqtgraph + numpy avoided in the PyInstaller bundle).
- Fast — the existing ~1,300 transaction dataset paints well under a frame; the paintEvent recomputes layout each repaint without measurable cost.

Cost being taken on:
- A few hundred lines of `paintEvent` per chart shape we add. Mitigated by sharing axes/ticks/format helpers in `mfl_desktop/ui/chart_helpers.py`, so a third chart shape later (e.g. Budget vs Actual time-series) reuses the same primitives.
- Any new chart type is a hand-roll rather than a one-line library call. Acceptable given how few chart types this app needs.

### Where the chart-engine selection lives — toolbar / dropdown / menu (chosen: combo in controls panel)

- *Top-level menu* (Reports → Engine →): formal but hides what's meant to be a quick A/B switch. Wrong tool for the comparison phase.
- *Toolbar combo*: discoverable but adds a toolbar to a window that doesn't have one.
- **Inline combo in the controls panel** (chosen): sits with the other filter controls, visible by default, dropped in next to Granularity. After the comparison ends and a winner is picked, the combo (and the two losing branches) are deleted.

### Where the shared chart-view interface lives — inside spending_report_window.py / new module (chosen: new `spending_chart_views.py`)

- *Inside the window file*: keeps things in one place. The window already does its own SQL and roll-up; adding ~600 lines of three chart implementations would balloon the file past readable.
- **New `mfl_desktop/ui/spending_chart_views.py`** (chosen during comparison): the three engines shared a `SpendingChartView(QWidget)` base with `render(buckets, groups, spending, avg_pounds)` + `show_empty(message)`. The window does the roll-up and hands structured data in.

**After the comparison** the module collapsed to:
- `mfl_desktop/ui/chart_helpers.py` — shared palette, `nice_ticks`, `fmt_currency`, `legend_chip`. Used by both the spending chart and the burn-down chart.
- `mfl_desktop/ui/spending_chart.py` — the (sole) `SpendingChart` paintEvent widget. Replaces `spending_chart_views.py`.
- `mfl_desktop/ui/burn_down_chart.py` — rewritten as a `BurnDownChart` paintEvent widget. External contract (`set_data(BurnDownData)`) preserved, so `budget_window.py` doesn't change.

## Decision

- Adopt **Fusion + custom QPalette + minimal QSS** as the app's visual baseline. Centralised in `mfl_desktop/ui/theme.py::apply_theme(app)`. Applied once from `__main__.py`.
- The MFL palette is a Tailwind v3 slate + `blue-600` scheme; the named constants (`COLOR_BG_WINDOW`, `COLOR_ACCENT`, etc.) in `theme.py` become the colour vocabulary for any subsequent hand-rolled QSS.
- Adopt the **custom paintEvent** chart approach for both the Spending Over Time chart and the burn-down chart. QtCharts and pyqtgraph rejected after the side-by-side comparison.
- `pyqtgraph` removed from `mfl_desktop/requirements.txt`. No new dependency taken on.
- Chart code organised as: `chart_helpers.py` (shared palette + tick / currency / legend helpers), `spending_chart.py` (the spending chart widget), `burn_down_chart.py` (the burn-down widget — preserves the existing `set_data(BurnDownData)` contract). Both windows that consume charts (`spending_report_window.py`, `budget_window.py`) instantiate the widget directly; the runtime engine combo introduced for the comparison is gone.

## Consequences

**Immediate.** The app reads less Windows-native, more modern. Per-window setStyleSheet usage already in `budget_window.py` and `net_worth_window.py` continues to work over the Fusion baseline (their hex colours were already light-on-light and remain legible).

**Both charts now hand-rolled.** Spending Over Time and the budget burn-down both go through `paintEvent`. They share `chart_helpers.py` so palette, tick rounding, currency formatting, and legend chips stay consistent. Any new chart shape (e.g. Budget vs Actual time series) follows the same template — copy from one of the two existing widgets, hook into the shared helpers.

**No new chart-library dep.** `pyqtgraph` and numpy are not in the bundle. Long-term PyInstaller size stays small per ADR-008.

**Styling discipline.** Per-window `setStyleSheet` usage should pull from the named constants in `theme.py` going forward, not invent new hex colours. The chart series colours (`_COLOR_ACTUAL`, `_COLOR_IDEAL`, `_COLOR_TODAY` in `burn_down_chart.py`) and the stack palette (`GROUP_PALETTE` in `chart_helpers.py`) are the only places hex strings should live for chart use. The existing windows that pre-date this ADR don't need a rewrite — they already use compatible greys and accent — but new windows should import from `theme`.

**New chart shapes cost more.** Adding a third chart shape is a hand-roll, not a one-line library call. Acceptable given how few chart types this app needs (the inventory after ADR-025 is: stacked-bar Spending, line burn-down, proportional bar — and the proportional bar is already hand-rolled in `proportional_bar.py`). If we ever needed something exotic (heatmap, scatter, area), pyqtgraph or QtCharts could be reintroduced for that one chart without disturbing the rest.

**Not solved.** Native font selection on Windows: the QSS specifies `"Segoe UI"` as the primary font, which is the right default for Win10/Win11. On a packaged Linux/macOS build the fallback chain takes over (`-apple-system`, `Inter`, `Helvetica Neue`). If a non-Windows family member runs the app and the chrome reads off, a per-platform font override in `apply_theme` is the right escape hatch.

**Reversible (style).** A single line (`apply_theme(app)`) and a single import. Reverting the Fusion baseline goes back to the native Windows look in one commit.

**Less reversible (chart engine).** Going back to QtCharts would mean rewriting both chart widgets from `paintEvent` against the QChart API. Doable but more than a one-line revert. We accept that asymmetry — the comparison ran on real data and the answer was clear.
