# ADR-018 — Reports framework + first chart: Spending Over Time

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-008 (UI framework — PySide6); ADR-014 (Category kind — defines spending semantics)

---

## Context

With the basic management round (transactions, accounts, payees, categories, folders, save/open) complete, the next milestone is reports. The owner asked for an explicit first cut: a stacked bar chart of spending over time with a flexible time series (weekly / monthly / quarterly / annually), a date range, account and category filters, an Uncategorised toggle, a period total, and an average — with the average drawn as a line on the chart. **Pie charts are explicitly ruled out** ("I think they rarely convey accurate data") so the chart vocabulary is bars and lines.

Three design questions follow from that brief: where reports live in the UI, what library draws the chart, and how spending is *meant* — the sign convention the bars are computed against.

## Options considered

### Where reports live

- *Embedded in the register window*: a "Reports" tab next to the register table.
  - Pros: single window; reduces task-switching.
  - Cons: the register's sidebar / filter bar / table dominate horizontally; the report needs the same width. Tab-switching loses the "compare report against register" workflow.
- *Separate non-modal window* (chosen): opens from a Reports menu, lives as long as the register, can sit side-by-side with the main window on a multi-monitor setup. The register stays usable while the report is open.
  - Pros: side-by-side workflow; each window is sized for its own job.
  - Cons: one more window to manage. Mitigated by reusing a single instance — opening Spending Over Time twice raises the existing window rather than spawning a duplicate.
- *Modal dialog*: blocks the register while the report is open.
  - Rejected. Reports are a read workflow; you frequently want to look at the report and click back into a transaction to recategorise.

### Chart library

- **QtCharts** (chosen): bundled with PySide6; no new runtime dependency; supports the exact primitives needed (`QStackedBarSeries` with `QBarCategoryAxis`, `QLineSeries` with `QValueAxis`, `QChartView` host) and stays consistent with ADR-008's "everything in Qt" stance.
- *pyqtgraph*: faster for large datasets, less polished defaults. Worth considering if reports grow beyond ~10k bars per chart. For the personal-finance scale (12–60 buckets per chart), QtCharts is comfortable.
- *Matplotlib via QtAgg*: heavyweight (~30 MB), brings a separate styling system, useful for Jupyter-style ad-hoc charts but not for a tight desktop UI. Rejected.

### Spending semantics — strict outflow vs net spending

Per ADR-014, the `category.kind` column tags every category as `income`, `expense`, or `transfer`. Two definitions of "spending" follow:

- *Net spending*: `-SUM(amount)` on every `kind='expense'` row. Negative amounts (the typical expense) contribute positively; positive amounts (refunds) contribute negatively. Matches ADR-014's description of refund handling.
- *Strict outflow* (chosen for v1): `SUM(-amount)` filtered to `amount < 0`. Only outflows count. Refunds appear in cashflow / income views, not here.

The net-spending definition is conceptually clean but produces **negative bar segments** when a group's bucket total is net-negative — which happens in practice today because Uncategorised's `kind='expense'` default (chosen at ADR-014) misclassifies positive amounts (typical: imported income with no category) as refunds. The smoke test on real data showed Uncategorised at `-£20,782.55` for one month for exactly this reason. `QStackedBarSeries` doesn't render negative stack segments cleanly, and even if it did, a chart titled "Spending" with bars pointing down is confusing.

Strict outflow sidesteps the problem: outflows are by definition positive, the chart is unambiguous, and the Uncategorised wrong-bucket risk from ADR-014 doesn't pollute the visual story. Refunds are still meaningful — they just belong in the Cash Flow report that hasn't been built yet, which will use signed amounts directly.

- **kind='income'** and **kind='transfer'** rows are excluded entirely; this is a *spending* report.

### Grouping rule for stacked-bar colours

The owner asked for "different colour for each high-level category" — the natural budget-line dimension (Groceries, Auto, Housing, …). Three options:

- *Top-level only* (e.g. always group as `Expense`): too coarse — one colour for everything is just a non-stacked bar.
- *Leaf-level*: too fine — every payee-specific child category becomes its own stack segment, the legend overflows, and the chart loses readability.
- **Second-level ("direct child of a root")** (chosen): for `Expense → Groceries → Tesco`, the group is `Groceries`. For `Expense → Auto → Petrol`, the group is `Auto`. A user-created top-level category with `kind='expense'` that isn't under `Expense` rolls up to itself. **Uncategorised is its own group** with its own toggle, kept out of the category checklist to make the include/exclude verb clear.

This rule lives in `mfl_desktop/reports.py::category_group_map(nodes)` — a pure-Python walk over the cached tree. SQL stays one-trick (sum per `(bucket, category_id)`) and the same query can drive different aggregation rules later without round-tripping.

### Refresh model

- *Manual Refresh button*: explicit; predictable; adds a click per change.
- *Auto-refresh on every control change* (chosen): the SQL aggregation over ~1,300 rows completes in well under a frame and the chart re-render is fast. The user gets immediate feedback as they slide the date range or toggle a category. A manual Refresh control isn't needed at this scale; we can introduce a debounce later if controls slide rapidly enough to feel laggy.

### Defaults

- Granularity: **Monthly** — most common for personal finance.
- Date range: **last 12 months → today** — fills a monthly chart with a year of context.
- Accounts / category groups: **all checked**.
- Include Uncategorised: **checked** — surfaces the "needs categorising" mass rather than hiding it.

## Decision

**Spending Over Time** ships as the first report:

- **Non-modal `SpendingReportWindow` (`QMainWindow`)**, opened from a new top-level **Reports** menu (entry: *Spending Over Time…*). The register holds one instance; reopening raises the existing window.
- **Chart**: `QStackedBarSeries` on `QBarCategoryAxis × QValueAxis` (£ label format). Bar order matches the bucket order; segment order within each bar matches **largest-total group first** so the legend is stable across views.
- **Average line**: drawn as a `QLineSeries` (dashed, dark-grey, width 2) attached to a *second*, invisible `QValueAxis` on the bottom — this is the only way to share a y-axis between a categorical-x series (the bars) and a numeric-x series (the line) inside one `QChart`.
- **Summary strip** below the chart: `Total: £x   Average: £y / {gran}   (N {gran}s)`. Average matches the line.
- **Repository method** `spending_aggregates(*, date_from, date_to, granularity, account_ids, include_uncategorised)` returns rows of `{bucket, category_id, spending_pence}` grouped by `(bucket, category_id)` filtered to `kind='expense'` and the active account list, with bucket expression chosen per granularity. The category-group roll-up happens in Python.
- **Grouping** uses `mfl_desktop.reports.category_group_map(nodes)`: for each category id, the report-group id is the **deepest ancestor whose parent is a root**, or the category itself if it's already top-level. Pure Python; cached on the report window for the session.
- **Filter widgets**: `QComboBox` (granularity) + two `QDateEdit`s (range) + checkable `QListWidget`s (accounts + category groups) + `QCheckBox` (Include Uncategorised). All wired to `_refresh` — no Refresh button.

## Consequences

### Positive
- A spending bar chart with the asked-for controls in one window, no new dependencies, working against real data.
- The grouping rule scales: future reports (cashflow, net-worth, category breakdowns) can call the same `spending_aggregates` (or its income equivalent) and pick their own roll-up depth.
- The same Repository query can serve weekly, monthly, quarterly, annual aggregates by switching one parameter — no per-granularity special cases in SQL.
- QtCharts inherits PySide6's packaging story (ADR-003) — Windows `.exe` doesn't grow.

### Negative / trade-offs
- Mixing categorical and numeric x-axes for the average line is a Qt-specific workaround. Behaves correctly but is less obvious than a single axis would be; documented in the `_render` method's comment so a future reader doesn't try to simplify the axes and break the line.
- Multi-currency users get a single £-label format. The report is single-currency for now — a multi-currency aggregate would need conversion to a base currency or per-currency facets; out of scope at v1.
- The category checklist lists only **expense groups**. A future "Income Over Time" or "Cashflow" report will need its own filter widgets; the controls panel is a thin layer over the Repository call, so duplication will be small.
- Auto-refresh runs the SQL on every checkbox tick. At ~1.3k transactions it's a non-issue; if the dataset grows materially (10k+ rows × many buckets) we may want to coalesce rapid changes via a 100ms debounce.

### Ongoing responsibilities
- The grouping rule (`category_group_map`) is shared with every future report. Any change in the category tree's "root" definition (e.g. introducing nested folders for categories) needs to update this helper, not each individual report.
- New report windows follow the same shape: non-modal `QMainWindow` parented to the register, single-instance via a `_<name>_win` attribute, opened from the Reports menu. Adding a new report is a new file plus one menu entry plus a Repository method.
- A future *Save Chart As Image…* verb belongs on the report window's File menu (or as a button). `QChartView.grab().save(path)` covers it without touching the data layer.
