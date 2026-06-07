# CLAUDE_CONTEXT.md
# My Financial Life — Developer Context for AI Assistance

This file gives a new Claude session full context to continue development
without needing the original conversation transcript.

**Last updated:** 2026-06-07 — most recent session closed the cross-currency entry gap (ADR-035 amendment 2026-06-07): new `TransferDestinationDialog` collects the partner-side amount at the moment of creation so the user is never blocked on a stored FX rate. Wired into the New Transaction transfer flow, the inline category-edit flow, the bulk-edit transfer review (new "Dest amount" column with FX pre-fill), and Post Now on cross-currency transfer schedules. `post_scheduled_txn` gains a `to_amount` kwarg + derived-rate branch. The earlier 2026-06-06 work shipped the multi-currency foundation (ADR-035), the transfer matcher (ADR-036), and the bulk reconcile screen (ADR-037). New visible surfaces: **Manage → Currencies…** (API key, refresh now, manual rates, matcher tunables), inline + bulk transfer matching dialogs replacing the always-create-partner behaviour, and **Manage → Reconcile Transfers…** (Ctrl+Shift+R) for pairing many candidates across two accounts at once. New modules: `mfl_desktop/fx.py` (openexchangerates.org client + launch-refresh helpers) and `mfl_desktop/transfer_reconcile.py` (pure-Python scorer + greedy pairer shared by single-flow matcher and reconcile dialog). Earlier-in-day work also covered: visual baseline + paintEvent charts (ADR-026), Create Schedule From Transaction (ADR-027), Payee aliases round 1 (ADR-029 + ADR-012 amendment), Spending Over Time rollup levels (ADR-030), hierarchical category picker (ADR-031), Vehicle account type (ADR-032), Per-account summary screen + polish + drill-down (ADR-033 / ADR-034 + period-preset amendment).

---

## Status at a glance

- **v0.1 shipped** as a local web app (FastAPI + HTMX + Oxigraph). MVP complete, owner-only. Now in maintenance mode.
- **2026-06-05 pivot:** MFL is being rebuilt as a **native desktop application** (PySide6 + SQLite) for Windows-first distribution. See [ADR-008](docs/adr/ADR-008-desktop-ui-framework.md), [ADR-009](docs/adr/ADR-009-storage-engine-for-ledger-data.md), and [ADR-010](docs/adr/ADR-010-transactional-schema-design.md).
- **Desktop app under `mfl_desktop/` is the live target.** Multi-account register with an All-transactions cross-account view, OFX/QFX/CSV import working end-to-end through the Qt UI, layered architecture (UI → proxy → model → Repository → SQLite). Owner has loaded six months of real data into it (~1,300 transactions) and confirmed the feel.
- **Basic management round complete (2026-06-05):** new + delete + bulk-edit transaction (Ctrl+E modal with per-field checkboxes); account CRUD (create / edit / delete) with opening balance; payee + category management dialogs with rename / merge / delete (cross-merge/-kind-merge rejected explicitly); category `kind` (income/expense/transfer) with cascade on reparent and direct Change Kind verb; Banktivity-style account folders in the sidebar with balance roll-up; File → Save Copy As… / Open… for `.mfl` snapshots; register search now covers payee/memo/amount/date and is comma-insensitive; category combos in dialogs are searchable typeaheads.
- **Reports round 1 (2026-06-05):** Reports → Spending Over Time (stacked bar by top-level expense category group, granularity weekly/monthly/quarterly/annually, date range, account/category/Uncategorised filters, average line, strict-outflow semantics per ADR-018); Reports → Net Worth (Pocketsmith-style three-column layout, big total + horizontal proportional bar + colour-coded legend / Assets / Debts, grouped by account type with per-account drill-down, +Asset / +Debt buttons opening the existing AccountDialog).
- **Transfers (2026-06-05):** category-driven (ADR-020). No dedicated New Transfer verb — picking a `kind='transfer'` category on any flow (New Transaction, inline cell edit, Bulk Edit) prompts for the destination account and creates a partner row sharing one `transfer_id`. Direction inferred from source amount sign. Delete is partner-aware. Migration 0004.
- **Generic CSV mapping wizard (2026-06-05):** unknown-format CSVs (Pocketsmith, etc.) now open a `CsvMappingDialog` — file-preview at top, mapping form in the middle, live after-mapping preview at the bottom (ADR-021). Smart defaults pre-fill all five fields from the existing alias lists; user just confirms for conventional layouts. No schema change. Known formats still commit silently per the no-dialog-for-known-imports rule. Saved mapping profiles (auto-skip the dialog for repeat imports) are explicitly deferred to a future ADR. Shipped alongside a fix to `_classify_and_stage` so within-batch composite-hash collisions (two CSV rows with the same date/amount/empty-payee) get a deterministic `:N` suffix instead of blowing up the `UNIQUE(account_id, import_hash)` constraint at commit.
- **Register typeahead delegates (2026-06-05):** Payee and Category inline editors are now repository-backed typeaheads matching the dialog flows (ADR-022). `PayeeTypeaheadDelegate` wraps `QLineEdit` + `QCompleter` over `Repository.list_payee_names()`; `CategoryTypeaheadDelegate` reuses the same `make_category_picker` helper as the dialog combos and reads `list_categories_flat()` fresh per editor open (no delegate rebind needed). Typing an unknown category name in the Category cell pops a single Yes/No confirm; on Yes the category is created top-level with `kind='expense'`, `source='user'` (aligned with the import path's default) and committed into the cell. Lightweight `_reload_category_cache()` on the window refreshes the cached choice list + filter combo without resetting the model, so the inline-create path is safe to call from inside a delegate `setModelData`.
- **Scheduled transactions / bills (2026-06-05):** First half of the budget arc (ADR-023). New `scheduled_txn` table (migration 0005) stores templates: cadence (weekly/biweekly/monthly/quarterly/annual), anchor date, next-due date, optional end date, signed estimated amount, `variable` flag (prompt at post), `auto_post` flag. Posting materialises one `txn` (or a transfer pair via `create_transfer` when category kind = transfer) and advances next-due via anchor-based math (Jan 31 monthly → Feb 28 → Mar 31). Manual post by default; launch-time `auto_post_due(today)` sweep catches up auto-posters loop-style (a user gone two months gets every missed occurrence). New dialogs: `ScheduleDialog` (single-record edit, transfer destination revealed when category kind = transfer) and `SchedulesDialog` (Manage → Schedules… — table view + New/Edit/Post Now/Delete). No txn → schedule back-link in v1.
- **Budget core (2026-06-05):** Second half of the budget arc (ADR-024). New `budget` / `budget_account` (M:N perimeter) / `budget_category` (target + cadence + role) tables (migration 0006). One budget per file in v1; schema supports many. Perimeter rule: transfers between two in-perimeter accounts cancel out; transfers crossing the perimeter count. Per-category amount stored positive; direction inferred from `category.kind`. Role on `budget_category` (bills / saving / discretionary). Actuals bucket against the **nearest budgeted ancestor** of each txn's category; un-budgeted-chain txns fall into "Other". Pure-Python `mfl_desktop/budget_calc.py` does pro-rating (`365.25/12` etc., identity for matching cadence on its calendar period) and the four Simplifi tiles (Income after bills & saving / Planned spending / Other spending / Available). **Transfer-kind budget categories are skipped in the tile math** (post-ship amendment to ADR-024). Cash-on-hand badge in the header is the separate reality-check. New screens: `BudgetSetupDialog` (two-tab modal — Accounts + Categories) and `BudgetWindow` (non-modal `QMainWindow` under a new top-level **Budget** menu, Ctrl+B, refreshes on `WindowActivate`). No rollover.
- **Budget visualisations (2026-06-06):** Round C of the budget arc (ADR-025). Closes the four ADR-024 deferrals. **Full-cadence-period subtitle** on non-monthly cards (e.g. £1,800 annual Holidays card now shows "£147.84/mo" + a second line "£1,800 annually · this year: £450 of £1,800"). **Burn-down chart** under the tile strip — QtCharts line chart with Actual cumulative outflow vs. linear Ideal pacing + vertical today marker. **Proportional summary bar** (reusing `ProportionalBar` from ADR-019) showing Bills / Saving / Planned / Other / Available proportions with colour-keyed legend — owner's no-pies rule (ADR-018) preserved. **Scheduled-but-not-posted projection** per card: "+£X expected" badge surfacing un-posted scheduled outflows whose `next_due_date` falls inside the screen period. All computation in `mfl_desktop/budget_calc.py` as optional kwargs to `compute_budget_view`; pure-function contract preserved. New widget `mfl_desktop/ui/burn_down_chart.py`. Cadence-period containment uses the global anchor rule (Monday weeks, calendar quarters/years; bi-weekly = 14-day window ending today pending per-schedule anchors).
- **Budget arc state (2026-06-06):** All three rounds shipped end-to-end, but the screen is **rough** — owner flagged it as needing further polish iterations after real use. Likely targets for the next pass: card-layout density (current vertical stack of header + cash badge + 4 tiles + bar + chart + cards eats ~480px before any card is visible), ~~visual tone of the QtCharts default styling on the burn-down~~ (closed 2026-06-06 by ADR-026 — burn-down is now a hand-rolled paintEvent widget matching the spending chart), labelling clarity on the tile colours, and the "Other" bucket behaviour when the user hasn't set up budget categories yet. Don't treat any specific bit of the screen as final — every part is fair game when polish work resumes.
- **Visual baseline + paintEvent charts (2026-06-06):** ADR-026 settled the "1990's Microsoft" complaint. Fusion style + a custom Tailwind v3 slate / `blue-600` QPalette + a minimal QSS layer are applied once from `__main__` via `mfl_desktop/ui/theme.py::apply_theme(app)`. Both charts (Spending Over Time, budget burn-down) hand-rolled as `paintEvent` `QWidget`s after a side-by-side comparison vs. QtCharts and pyqtgraph — owner picked paintEvent for the modern flat look (rounded top corners, soft gridlines, pill-shaped dashed average label, hover tooltip) plus full typography control plus zero new dependency. Shared chart primitives in `mfl_desktop/ui/chart_helpers.py` (palette, `nice_ticks`, `fmt_currency`, `legend_chip`); spending and burn-down both consume them. pyqtgraph removed from requirements; QtCharts no longer imported by either chart widget. Memory: future chart shapes default to paintEvent ([[feedback-chart-engine-preference]]).
- **Period presets revised + Custom range (2026-06-06):** ADR-033 amendment after the polish round shipped. New `PERIOD_KEYS` are `("quarter", "6m", "ytd", "1y", "3y", "custom")` with labels Last Quarter / Last 6 months / Year to date / Last 12 months / Last 3 years / Custom — finance-native vocabulary matching the Banktivity inspiration. "Last Quarter" is rolling 90 days (consistent with the other rolling presets). The old "all" preset is retired in favour of explicit Custom. New `mfl_desktop/ui/custom_period_dialog.py` is a small modal with From/To `QDateEdit` calendar pickers, From ≤ To validation, both clamped to today; defaults seeded from the previously-displayed range so Custom is "edit where you are". Shared helpers `fmt_date_range` and `period_display_label` in `mfl_desktop/account_summary.py` render "Custom: 1 Jun → 6 Jun" (same year) or "30 Dec 2025 → 6 Jun 2026" (otherwise). `TxnListFilter` carries `custom_start` / `custom_end` so a drill-down opened from a Custom-mode summary inherits the same range; the filter's `signature()` includes the bounds so distinct custom ranges open as distinct windows. Drill-down period chip is now non-removable (period is always set; the user changes it via the button row, not by removing it). Default period stays a rolling 90 days under the new "quarter" key, so the chart looks identical on first open.
- **Per-account summary polish — dual-axis chart, section cards, Top-N drill-down (2026-06-06):** ADR-034 closes three pieces of real-use feedback after ADR-033 shipped, in one polish round. **Dual y-axis** on `BalanceFlowChart` — independent left (bars) and right (balance line) scales, each with its own `nice_ticks`; right-axis labels coloured blue-600 to match the line. Bars own the zero baseline; the line doesn't. Closes the credit-card-paydown case where the balance line was dwarfing the bars. **Section cards** around the four panels (chart, info, top-payees, top-categories): `QFrame` with rounded 10px border + 1px slate-200 outline + white background on a slate-50 window canvas. Splitter handles widened to 12px so the cards have a visible gutter. **Top-N drill-down** — clicking "Eating out" or "Tesco" in a Top-N panel opens a new `TransactionsListWindow` (`mfl_desktop/ui/transactions_list_window.py`) showing the underlying transactions as an editable register view with a breadcrumb chip strip (Account / Period / Category / Payee, each removable via ×) and the same six-preset period selector as the summary. New `DrillDownFilterProxy` subclasses `TransactionFilterProxy` with date-range + payee-id + category-descendants filters. New `TxnListFilter` frozen dataclass + `for_payee` / `for_category` factory constructors carry the spec. Category filter is "this and descendants" (uses `Repository.category_descendants`). Same inline delegates as the main register so edits propagate through the Repository. **`TopNRow` gains an `entity_id`** so the drill-down can filter on ids rather than labels — `_top_n_by_id` aggregates by id with a synthetic `None` bucket for `(No payee)` (non-clickable in v1). **`_TopNList` becomes interactive**: hover tints the row slate-100, cursor switches to PointingHand on clickable rows, press emits `row_clicked(TopNRow)`. **Window policy**: one drill-down per distinct `(account_id, period_key, category_id, payee_id)` signature — same row clicked twice raises the existing window, different row spawns a new one (owner picked this over replace-in-place via `AskUserQuestion`). Drill-downs parented to the summary so closing the summary closes them all. Closes the dual-axis, section-borders, and drill-down follow-ups originally flagged on ADR-033.
- **Per-account summary screen (2026-06-06):** ADR-033 lands the per-account focus view — the missing other-axis next to the cross-account register, the budget window, and Net Worth. New non-modal `QMainWindow` (`mfl_desktop/ui/account_summary_window.py`) opened from sidebar context menu (Account Summary… first item), sidebar double-click on an account row, and **Account → Summary…** (Ctrl+I). Single-instance per account — `RegisterWindow._account_summary_wins: dict[int, AccountSummaryWindow]` keyed on `account.id`; repeat opens raise the existing window. Top of screen: a combo chart — income bars above the zero baseline, spending bars below, balance polyline overlaid in blue-600 — driven by the owner's framing ("would be good for current accounts and also quite a nice view for a credit card to see the balance coming down if you're trying to pay off a debt"). Period selector below: Last 30 days / Last 90 days / YTD / Last 12 months / Last 5 years / All time, default 90d, granularity auto-picked (daily ≤45d, weekly ≤120d, monthly ≤800d, quarterly ≤2200d, yearly beyond). Right column: Summary (recorded balance + scheduled-count line), Additional Info (Uncleared `(N)` count + amount in red, Cleared Balance), Upcoming (next 5 schedules touching the focus account within 30 days — picks up either `account_id` match or transfer-destination match, flipping sign for transfer-in), plus a placeholder "NO STATEMENTS · RECONCILE ›" row that opens an info dialog (handler swap when the reconciliation arc lands; layout untouched). Bottom row: TOP PAYEES + TOP CATEGORIES — strict-outflow lists with proportional bar fills, period-scoped. Non-cash families (investment / property / vehicle) render an amber banner above the chart: "Balance reflects recorded transactions; valuations not yet wired." until the valuation pipeline arrives. Chart, info panel, top-payees, and top-categories sit in nested `QSplitter`s so any of the four panes can be resized. New paintEvent widget `mfl_desktop/ui/balance_flow_chart.py` reusing `chart_helpers`; new pure-Python `mfl_desktop/account_summary.py` mirroring `budget_calc.py` (compute_balance_flow_series / compute_period_summary / compute_status_breakdown / top_payees / top_categories / upcoming_scheduled). No schema change. Refreshes on `WindowActivate`. Smoke-tested with synthetic data — period math balances (opening + inflows − outflows = closing), cleared + uncleared = recorded, strict-outflow proportions sum to 100%.
- **Create Schedule From Transaction (2026-06-06):** ADR-027 adds a right-click verb on the register that seeds the `ScheduleDialog` from a transaction (account / payee / category / amount / memo / transfer-destination if applicable). The dialog field renamed to **"Next occurrence:"** in both seeded and from-scratch flows. The handler steps `txn.posted_date` forward by the default cadence (`monthly`) until the result is strictly after today, passes that as `seed.anchor_date`; on save anchor = next_due = that future date. New frozen `ScheduleSeed` dataclass + `seed=` kwarg on `ScheduleDialog`; new `Repository.get_transfer_partner_account_id`. No schema change.
- **Payee aliases round 1 (2026-06-06):** First round of the three-round payee arc planned in ADR-028. Migration 0007 adds `payee.canonical_id` (self-ref, `ON DELETE SET NULL`) + partial index. `Repository` gains `set_alias_of` / `promote_to_canonical` / `list_canonical_payees` / `list_aliases_of`; `list_payee_names` filtered to canonicals only (typeahead + bulk-edit completer now suggest preferred labels only); `list_payees_with_usage` rewritten with rolled-up counts on canonicals; `merge_payees` patched to re-point sources' aliases onto the target before deletion. `PayeeRow` extended (`canonical_id`, `canonical_name`, `direct_usage_count`). Payees dialog gets 3 columns (Name / Alias of / Used in), indented aliases, and two new verbs (**Make &Alias of…**, **&Promote to Canonical**). `ADR-012` amended with the canonical/alias model and the three-shape verb table (Merge / Alias / Delete). **Round 2** (import-time alias lookup) and **round 3** (rules engine) deferred per ADR-028 plan. Round 1 deliberately doesn't roll up the register's per-row display to the canonical — round 2 sets `txn.payee_id` to the canonical at import time, which avoids a silent rewrite-on-no-change pitfall in the inline typeahead.
- **Multi-currency foundation (2026-06-06):** ADR-035 lays the schema + data layer for true multi-currency support. Migration 0009 adds three tables — `setting` (flat key/value store for the openexchangerates API key, last-refresh timestamp, and matcher tunables), `fx_rate` (date/base/quote/rate/source with daily granularity), and `transfer` (parent row per pair, keyed on the existing `txn.transfer_id` IRI, carrying the exchange rate that was used at posting time + its provenance: `derived` / `manual` / `fx_rate`). Existing same-currency transfers are back-filled at rate=1.0. `Repository.get_fx_rate_nearest` walks a six-step lookup chain: exact bilateral → exact inverse → exact USD-pivot → nearest-prior bilateral → nearest-prior inverse → nearest-prior USD-pivot, so any `(date, base, quote)` ask resolves cleanly off a USD-base provider feed plus arbitrary bilateral manual entries. `Repository.convert_amount` is the single conversion path; same-currency early-exits cheaply. **Transfer plumbing is now currency-aware end-to-end** — `create_transfer`, `convert_to_transfer`, and `post_scheduled_txn` each take optional `to_amount` / `rate` / `rate_source` kwargs (two-of-three rule: any one resolves the others, missing both triggers an FX-table lookup with nearest-prior fallback). The destination row's stored amount is whatever really hit that account's statement; the parent `transfer.rate` row is the truth-of-intent. **txn does NOT gain a currency column** — a txn's currency is its account's currency, single source of truth. `account.currency` and `person.base_currency` already existed in 0001 so no ALTER was needed on existing tables.
- **openexchangerates.org integration + Currencies dialog (2026-06-06):** new `mfl_desktop/fx.py` is a pure-Python urllib client (no Qt imports — CLI-compatible). Endpoints used: `/latest.json` and `/historical/<date>.json`, both with the free-tier USD-base constraint. `refresh_latest_into(repo, force=False)` skips when last refresh < 24h ago (configurable via `LAUNCH_REFRESH_INTERVAL_HOURS`) and when no non-USD accounts exist — single-currency users never burn API budget. **Background launch refresh** wired in `__main__.py` via `QThreadPool.globalInstance().start(_FxRefreshRunnable(args.db))` with its own Repository connection (sqlite3 cross-thread safety). New **Manage → Currencies…** dialog (`mfl_desktop/ui/currencies_dialog.py`) holds the API key field (with "stored inside this file" disclaimer per ADR-035), Refresh Now button (synchronous, wait-cursor), latest-rates table (one row per `(base, quote)` pair we've stored, showing the most recent date+rate), "Add manual:" row at the bottom for ad-hoc rate entry (covers the OXR-not-set case and weekend gaps), and the two matcher tunables from ADR-036 (`transfer_match_window_days` default 3, `transfer_fx_tolerance_pct` default 1.0). API key persists to `setting.oxr_api_key` on Save; tunables persist to their own setting keys. `backfill_historical()` exists in `fx.py` for explicit date-range backfills but isn't wired into the dialog yet.
- **Transfer matching (2026-06-06):** ADR-036 replaces ADR-020's always-create-partner behaviour with a matcher that finds an existing other-side row first. `Repository.find_transfer_candidates` filters by opposite sign, `transfer_id IS NULL`, posted_date within `±transfer_match_window_days`, and (for cross-currency) amount within `transfer_fx_tolerance_pct × 5` of `source_magnitude × spot_rate`. Returns scored candidates. `Repository.link_transfer` writes a fresh `transfer_id` IRI on both rows, **rewrites both rows' `category_id`** to the source's transfer category (the rewrite is the whole point per ADR-036 §what-about-the-source-row), and inserts a `transfer` parent row with rate back-derived from the two amounts. `mfl_desktop/transfer_reconcile.py` holds the pure-Python `score_candidate` (weights: 100 base, −5×|days_apart|, −50×amount_mismatch_pct/100, −20 if currencies differ, +10 on payee-token overlap; stopwords filter out "transfer/to/from/payment" etc.) and the `greedy_pair` bipartite helper. Strength bins: ≥80 Strong (blue), ≥60 Good (amber), else Possible (slate). **UI:** new `transfer_match_dialogs.py` holds `TransferMatchConfirmDialog` (one candidate; [Match] / [Create new partner] / [Cancel] verbs) and `TransferMatchPickerDialog` (many candidates; ranked table with strength chips + trailing "Create new" sentinel row). Both wired into `register_window._on_model_data_changed` via the new `_offer_transfer_match` helper — the inline-edit flow now routes through them before falling back to `convert_to_transfer`. Matcher errors (e.g. missing FX rate) fail soft: status-bar message + fallback to create-new. **Bulk-edit dispatcher** in `_on_bulk_edit` was rewritten in two phases: phase 1 (`bulk_update_transactions`) applies category + payee/status/memo; phase 2 runs the matcher per row, opens a `BulkTransferReviewDialog` (summary line + per-row decision cell — fixed label for unambiguous rows, inline combo box for the rows with multiple candidates, top-score pre-selected), and writes via `bulk_match_or_create_transfers` (single SQL transaction). New `BulkRowAnalysis` dataclass + `LinkExisting` / `CreateNew` decision types are the Repository's `BulkTransferDecision` union.
- **Cross-currency entry collects partner amount (2026-06-07):** ADR-035 amendment closes the gap between ADR-035 §UI and the as-built behaviour. New `mfl_desktop/ui/transfer_destination_dialog.py` (`TransferDestinationDialog`) surfaces a single combined picker: account combo at the top, cross-currency block (revealed only when the chosen other-account differs in currency) with editable amount field + live "Implied rate: 1 USD = 0.7900 GBP" line + hint about pre-fill source. Pre-fill comes from `Repository.get_fx_rate_nearest`; when no rate is on file the field is blank and the user types the magnitude that actually hit the destination. Returns `TransferDestinationChoice(account_id, other_amount)` — sign-neutral, framed as "this account / other account." Wired into the **New Transaction** transfer flow (translates `other_amount` to `amount` / `to_amount` per direction), the **inline category-edit** flow (passes `to_amount` to `convert_to_transfer`; the matcher still runs first per ADR-036), the **bulk-edit transfer review** (new "Dest amount" column with per-row FX pre-fill — `BulkRowAnalysis` gains `dest_currency` + `fx_prefill_amount`, the dialog only renders the extra column when any row crosses currencies), and **Post Now on cross-currency transfer schedules** (via a new `locked_account_id` kwarg that pins the combo to `transfer_to_account_id`). `Repository.post_scheduled_txn` gains a `to_amount` kwarg + a `derived`-rate branch that bypasses the FX-table lookup when supplied; `auto_post_due` is unchanged (no user prompt path). All four user-facing entry surfaces now stamp `rate_source='derived'` when the user types a partner amount, preserving ADR-035's truth-of-intent contract (`fx_rate` source reserved for stored-rate lookups, `manual` reserved for user-typed rates which the txn-entry UI still doesn't expose).
- **Bulk transfer reconcile (2026-06-06):** ADR-037 ships **Manage → Reconcile Transfers… (Ctrl+Shift+R)** for housekeeping after importing both sides of many transfer pairs separately. `Repository.find_transfer_pairs(account_a_id, account_b_id, window_days, fx_tolerance_pct)` builds the cross-product of A's unmatched rows × B's unmatched rows, scores via the shared `score_candidate`, then runs `greedy_pair` (highest-score-first; each source/target row claimed at most once; ties broken by score desc then id asc for stable ordering). Returns ranked `TransferPair` rows (Strong → Good → Possible). New `mfl_desktop/ui/transfer_reconcile_dialog.py` has account A + B combos at top, a read-only tunable summary line ("Window: ±3 days · FX tolerance: ±1.0% (change in Manage ▸ Currencies)"), and a checkbox table of proposed pairs with strength chip + side-by-side view (A date/amount/payee | B date/amount/payee). Cross-currency rows surface a tooltip on the payee cells: "Implied 1.2739 · Spot 1.2700 · Δ +0.31%". Three shortcut buttons — Check all Strong / Check all Strong + Good / Uncheck all. Transfer-only category picker via the existing `make_category_picker`, defaulting to the seeded Transfer root. Apply writes through `bulk_match_or_create_transfers` and re-runs the search so matched rows drop out — the user can do a second pass with different settings without re-opening. Two-accounts-at-a-time is deliberate per ADR-037 — a future N-account wildcard mode is on the backlog.
- **Original PySide6 prototype kept at `prototype_register/`** as a reference for the data-grid pattern; it's not the main app.
- **Sister app MRL stays RDF-based.** MFL ↔ MRL integration is now at the data-exchange boundary, not shared storage.

---

## Owner profile

Mark Hall (`mark.a.hall@gmail.com`). Sole developer, not a professional developer, comfortable reading and editing Python. Runs the app locally; copies code into files manually. Moving personally from macOS to Windows — hence Windows-first distribution. Will share the packaged app with non-technical friends/family.

**Durable working preferences:**
- Deliver **complete files**, not diffs or snippets. After full-file replacements, verify imports were preserved.
- **Every significant architectural decision is recorded as an ADR** under `docs/adr/`, with rationale for the alternatives rejected — even decisions the owner has explicitly delegated.

---

## Target architecture (post-pivot, in progress)

**Stack:** Python 3.13 + **PySide6 (Qt for Python)** + **SQLite**, with DuckDB attachment as a future option for analytical queries.

**Distribution:** Single-file Windows `.exe` via PyInstaller. macOS and Linux follow once Windows is stable.

**Layering** — mirrored in the prototype, will carry forward into the real app:

```
QMainWindow                ← top-level windows + navigation
    │
    ▼
QSortFilterProxyModel      ← filter / sort
    │
    ▼
TransactionTableModel      ← QAbstractTableModel contract
    │
    ▼
Repository                 ← only layer that touches SQL
    │
    ▼
SQLite
```

The Repository contract isolates the rest of the codebase from the storage decision. Future DuckDB analytics attachment slots in at the Repository level without disturbing UI or service code.

---

## Legacy architecture (v0.1, maintenance only)

**Stack:** FastAPI + Jinja2 + HTMX + Tailwind/DaisyUI + Chart.js + pyoxigraph 0.5.8.

**Run:** `python main.py` from the project root → `http://127.0.0.1:8000`.

**Location:** `main.py` + everything under `app/`. Jinja templates under `app/templates/`. TTL ontology under `docs/ontology/`.

**Status:** No new features. Bug fixes only if the owner needs them while the rewrite is in flight. The import engine (`app/core/import_engine/`) will be **lifted** onto the new repository — preserved, not rewritten.

---

## What survives the rewrite

| Element | Status in rewrite |
|---|---|
| CSV / OFX / QFX parsers (`app/core/import_engine/`) | Preserved; retargeted at the SQLite repository |
| Duplicate detection (OFX FITID hash, CSV composite MD5 hash) | Preserved |
| Account types and families (cash / credit / investment / property) | Preserved |
| Transaction status enum (Pending / Uncleared / Cleared / Reconciled) | Preserved |
| Category taxonomy | **Changed** — now hierarchical and dual-sourced (see below) |
| MRL-compatible IRIs as identifiers | Preserved — stored as opaque text columns in SQLite |
| MFL ontology TTL (`mfl-ontology.ttl`) | Becomes a reference document; no longer a runtime artefact |
| HTMX templates / Jinja layouts | Discarded — replaced by Qt widgets |
| Pagination logic | Discarded — `QTableView` virtualises natively |
| SPARQL data-access code | Discarded for MFL (retained inside MRL only) |

---

## Domain reference (still authoritative)

### Account types and families

| Key | Label | Family | Liability | Balance from |
|---|---|---|---|---|
| `cash_std` | Current account | cash | No | Transactions SUM |
| `savings_std` | Savings account | cash | No | Transactions SUM |
| `credit_std` | Credit card | credit | Yes | Transactions SUM |
| `investment_std` | Investment account | investment | No | Latest valuation |
| `property_std` | Property | property | No | Latest valuation |
| `vehicle_std` | Vehicle | vehicle | No | Latest valuation (planned) |

### Transaction statuses

| Status | Usage |
|---|---|
| Pending | Manual entry, not yet on statement |
| Uncleared | Imported, needs review |
| Cleared | Verified correct |
| Reconciled | Matched to closing balance |

Import default behaviour: first import on an empty account → `Cleared` (treated as historical load); subsequent imports → `Uncleared` (review). Banktivity CSV imports always honour the per-transaction status from the file.

### Categories (revised model)

Categories form a **tree** with three sources:

1. **System defaults** — seeded from the v0.1 taxonomy (Income / Expense top-level, with the v0.1 subcategories). Not deletable; archivable/hidden TBD.
2. **User-created** — the user can add categories anywhere in the tree at any time.
3. **Import-created** — when an import file carries a category MFL doesn't recognise, the category is auto-created. For source formats with hierarchical paths (Banktivity uses `Parent:Child`), the path is parsed and intermediate nodes are created as needed.

Import lookup is by **full path**, not just leaf name — `Auto:Gas` and a top-level `Gas` are distinct categories. Reports/filters use SQLite `WITH RECURSIVE` to walk the tree when "this category and descendants" is requested. Open design questions tracked in memory (`project-categories-design`) and will be settled in the schema ADR.

### Identifier conventions

Carried forward from ADR-006:

- **Accounts and Person:** `mrl:ClassName_N` (integer suffix), MRL-compatible — e.g. `mrl:CashAccount_1`, `mrl:Person_1`.
- **Transactions, ImportBatches, ValuationEvents:** `mfl:ClassName_<uuid8>` — e.g. `mfl:Transaction_a3f7c901`.

In SQLite, these are stored as `TEXT` columns on the relevant rows (e.g. `account.iri`, `txn.iri`). They remain meaningful for any future RDF export and for MRL integration; internally the database uses INTEGER PKs for joins.

### Import workflow (preserved)

```
Upload (OFX / QFX / CSV)
    ↓
parse_and_stage() → returns (token, "preview" | "map")
    ↓ if "map"
Column mapping UI            ← for unknown CSV layouts
    ↓
apply_mapping_and_stage()
    ↓
Preview                      ← new / duplicate / potential-match classification
    ↓
commit_import()
    ↓
Result summary
```

**Duplicate detection:**
- OFX: bank FITID stored as `import_hash`.
- CSV: `MD5(account_iri + "|" + date + "|" + amount + "|" + payee_raw)[:12]`.

**Potential match:** manual entry on same account, same amount, same direction, date ±2 days. Default action is merge; merge preserves user data and adds the import hash to the manual entry.

### CSV format detection (preserved)

`_detect_format(lines)` returns `"banktivity"`, `"creditcard"`, or `"generic"`.

- **Banktivity:** Row 1 = account name (≤2 commas), row 2 has Type/Status/Date/Payee headers. Per-transaction status honoured. Amounts have a currency symbol (`£` / `$` / `€`) and commas. Date format M/D/YY. Split transactions collapsed to parent total. Direction is taken from the amount sign for all three Types (Deposit / Withdrawal / Transfer) per **ADR-038** — Banktivity exports signed amounts across the board, so the sign is the single source of truth. The Type column is informational only; mismatches (a `Withdrawal` exported with a positive amount because the user mis-tagged it) are logged at INFO and the sign wins. **Categories use `:` as the hierarchy separator** and will be created if unknown.
- **Credit card:** headers contain `debitCreditCode` or `merchant.name`. Date is ISO 8601 with time. Amount is always positive; direction comes from `debitCreditCode`.
- **Generic:** falls through to the column-mapping UI.

---

## Sister app — My Retirement Life (MRL)

MRL remains RDF/Oxigraph-based — its workload (tax law across jurisdictions, heterogeneous evolving relationships) is what RDF was designed for. Integration with MFL now happens at the **data-exchange boundary**, not shared storage:

- **Identifier compatibility** — MFL stores MRL-style IRIs as opaque text on rows that need to refer to MRL entities.
- **Reference reads** — MFL can read MRL's Oxigraph store directly for reference data (tax rates, jurisdictions).
- **Data exchange** — MFL can emit and consume RDF when full data exchange is needed.

The original "shared database with MRL" goal from ADR-001 is no longer the strategy at the storage layer. See ADR-009.

---

## Repository structure

```
C:\Users\hallm\Documents\GitHub\my-financial-life\
├── main.py                          # v0.1 entrypoint (legacy)
├── requirements.txt                 # v0.1 deps (legacy)
├── CLAUDE_CONTEXT.md                # this file
├── docs/
│   ├── adr/                         # ADR-001 through ADR-010 + README
│   ├── schema.sql                   # ADR-010 reference schema (copy lives in mfl_desktop/migrations/)
│   └── ontology/
│       ├── mrl-ontology.ttl         # shared with MRL — do not edit from MFL
│       └── mfl-ontology.ttl         # MFL-specific, now a reference document
├── app/                             # v0.1 web app (legacy — maintenance only)
│   ├── api/                         # FastAPI routes
│   ├── core/
│   │   ├── accounts/
│   │   ├── dashboard/
│   │   ├── import_engine/           # SUPERSEDED — port lives at mfl_desktop/import_engine/
│   │   ├── ontology/
│   │   ├── transactions/
│   │   └── templates.py
│   ├── data/                        # Oxigraph singleton + loader
│   └── templates/                   # Jinja/HTMX (discarded in rewrite)
├── mfl_desktop/                     # Native desktop app — the live target
│   ├── __main__.py                  # `python -m mfl_desktop` launches the GUI
│   ├── cli.py                       # init / import / list / categories / add-account
│   ├── requirements.txt             # PySide6 + ofxtools
│   ├── README.md
│   ├── db/
│   │   ├── money.py                 # Decimal ↔ INTEGER pence
│   │   ├── repository.py            # Repository — the ONLY layer that touches SQL
│   │   └── schema.py                # Migration runner (manages schema_version)
│   ├── migrations/
│   │   ├── 0001_initial.sql         # ADR-010 schema + seeded categories
│   │   ├── 0002_category_kind.sql   # ADR-014: kind column + Transfer seed
│   │   ├── 0003_account_folders.sql # ADR-015: account_folder + account.folder_id
│   │   ├── 0004_transfers.sql       # ADR-020: txn.transfer_id + partial index
│   │   ├── 0005_scheduled_txn.sql   # ADR-023: scheduled_txn template + due-date indexes
│   │   ├── 0006_budgets.sql         # ADR-024: budget + budget_account + budget_category
│   │   ├── 0007_payee_canonical.sql # ADR-029: payee.canonical_id + partial index
│   │   ├── 0008_vehicle_account_type.sql # ADR-032: widen account.type CHECK to include vehicle_std
│   │   └── 0009_multi_currency.sql  # ADR-035: setting + fx_rate + transfer parent tables + backfill
│   ├── import_engine/               # Lifted from app/core/import_engine/
│   │   ├── ofx_parser.py            # OFX/QFX — verbatim from v0.1
│   │   ├── csv_parser.py            # Banktivity / credit-card / generic CSV (syntax bug fixed)
│   │   └── import_service.py        # Stage + classify + commit, rewritten against Repository
│   ├── account_summary.py           # ADR-033: pure-Python per-account aggregations (mirror of budget_calc.py)
│   ├── fx.py                        # ADR-035: openexchangerates.org client + refresh helpers (no Qt deps)
│   ├── transfer_reconcile.py        # ADR-036/037: pure-Python score_candidate + greedy_pair helpers
│   └── ui/
│       ├── register_window.py       # QMainWindow with sidebar + register
│       ├── register_model.py        # QAbstractTableModel — single-account + all-transactions modes
│       ├── filter_proxy.py          # Sort / filter on underlying values
│       ├── delegates.py             # Payee + Category typeahead delegates + Status combo (ADR-022)
│       ├── category_picker.py       # Shared editable-combo helper for category fields
│       ├── csv_mapping_dialog.py    # ADR-021: column-mapping wizard for unknown CSV formats
│       ├── schedule_dialog.py       # ADR-023: single-record schedule create/edit
│       ├── schedules_dialog.py     # ADR-023: schedule list + CRUD + Post Now
│       ├── budget_setup_dialog.py   # ADR-024: perimeter + per-category setup
│       ├── budget_window.py         # ADR-024/025: budget screen (tiles + bar + chart + cards)
│       ├── burn_down_chart.py       # ADR-025: cumulative outflow vs ideal pacing
│       ├── balance_flow_chart.py    # ADR-033/034: combo chart (income/spending bars + balance line; dual y-axis)
│       ├── account_summary_window.py # ADR-033/034: per-account focus screen (card layout + Top-N drill-down)
│       ├── transactions_list_window.py # ADR-034: drill-down register view with breadcrumb chips
│       ├── custom_period_dialog.py  # ADR-033 amendment: From/To date picker for "Custom" period
│       ├── currencies_dialog.py     # ADR-035: Manage → Currencies… (API key + refresh + manual rates + tunables)
│       ├── transfer_match_dialogs.py # ADR-036: confirm + picker + bulk review dialogs
│       ├── transfer_reconcile_dialog.py # ADR-037: Manage → Reconcile Transfers… (Ctrl+Shift+R)
│       └── sidebar.py               # Account list with "All transactions" entry
└── prototype_register/              # Original PySide6 prototype — kept for reference
    ├── README.md
    ├── requirements.txt
    ├── seed.py                      # builds prototype.db (10k synthetic txns)
    └── register_proto.py            # single-window register demo
```

---

## Backlog

Captured during the rewrite as features are deferred for later turns.

### Register UX

Items (1)–(3) of the original cluster shipped under ADR-022 (typeahead delegates + inline category create). Item (4) bulk-edit shipped in the basic-management round under ADR-017. Follow-ups noted in ADR-022's consequences section:

- **Kind-aware inline create.** Today's confirm dialog fixes `kind='expense'` for the new category. If real use shows the owner regularly wanting income or transfer-kind categories created inline, the right v2 is a tiny kind radio inside the confirm dialog — not a separate full-create dialog path.
- **Memo history typeahead.** The memo cell is still a bare `QLineEdit`. A repository-backed completer over distinct memo strings would slot in symmetrically with the payee delegate.

### Polish backlog from 2026-06-05 basic-management round

- **Visible "New Transaction" button.** Today the only entry points are the Transaction menu and Ctrl+N. A toolbar / register-pane button would be more discoverable. Owner asked for this during step 1; deferred to a later UI polish pass.
- **Unlock kind combo on New sub-category.** In the New Category dialog, when a parent is chosen the kind combo is locked to the parent's kind. For mixed-kind structures (e.g. Paycheck with Gross Pay = income and Taxes = expense beneath it), creating a different-kind child is currently two steps (create as inherited kind, then Change Kind). Editing this to leave the kind combo *unlocked* (just defaulting to parent's kind) is the obvious fix when real-world use confirms the need.
- ~~**Spending Over Time chart visuals.**~~ Closed by ADR-026 (paintEvent rewrite — modern flat look, rounded top corners, soft gridlines, hover tooltips, pill-shaped average label). Surviving sub-items (cleaner axis labels like `Jan 2026`, optional numbers-on-bars toggle, Save Chart As Image) carried into the new "Reports — round 2 follow-ups" section below.

### Budget arc — follow-ups after round C (2026-06-06)

The three-round arc (ADR-023 / 024 / 025) is complete. Items deliberately left for later polish or to feed real-use feedback:

- **Per-card sparkline** of historical actuals over the last N periods — a thin chart inside each card showing the trend, scoped to the card's own cadence period. Deferred from round C to keep card-layout simple while owner lives with the screen.
- **Reports → Budget vs Actual** time-series window — a separate report showing planned vs actual per category over a date range. Bigger window of its own; defer until use surfaces a clear need beyond the current per-month view.
- **Overdue-schedules surface on the budget window** — round C deliberately omits overdue schedules (next_due_date before period start) from the per-card "expected" badge. A small "X overdue schedules — review in Manage ▸ Schedules" indicator on the budget header would close the visibility gap.
- **Per-schedule (and per-budget_category) cadence anchor** — the global Monday-weeks / calendar-quarters rule from ADR-023 holds for both schedules and budget cards. A user whose paycheck is a Friday-paid biweekly schedule would benefit from a per-row anchor override; needs UI in the schedule and budget setup dialogs and a small schema migration to add the anchor column on budget_category (it's already on scheduled_txn).
- **Rollover** of unspent surplus / deficit between periods — a per-`budget_category` rollover flag. Additive when needed.
- **Multi-budget per file** — schema already supports it; UI surfaces a single default budget today.
- **Stepped burn-down ideal** that drops at known scheduled-txn dates rather than a single straight line. Adds nuance without new data.
- **Budget palette extraction** — round C inlines the segment / chip colours in `budget_window.py` and the chart series colours in `burn_down_chart.py`; consolidate if a wider palette unification ever lands.

### Payee arc — round 1 shipped (2026-06-06)

Three-round arc planned in **ADR-028** (Proposed). **Round 1 shipped 2026-06-06** as **ADR-029** (Accepted) + an amendment to **ADR-012**.

- **Round 1 (shipped)**: migration 0007 adds `payee.canonical_id` + partial index. `Repository` gains `set_alias_of`, `promote_to_canonical`, `list_canonical_payees`, `list_aliases_of`; `list_payees_with_usage` rewritten with canonical rollup; `list_payee_names` filtered to canonicals only (typeahead + bulk-edit completer); `merge_payees` re-points sources' aliases onto the target before deletion. `PayeeRow` extended with `canonical_id` / `canonical_name` / `direct_usage_count`. PayeesDialog gets 3 columns (Name / Alias of / Used in), indented aliases, and two new verbs (Make Alias of…, Promote to Canonical). Sort-on-column-0 disabled so canonical→aliases grouping survives. **Deferred deliberately**: register inline display doesn't roll up to canonical (round 2's import-engine work is the natural place — silent rewrite-on-no-change pitfall via the inline typeahead if done at display layer).
- **Round 2** (own arc, after Reports round 2): import-time alias lookup. Exact match at minimum, pattern / fuzzy as follow-ups inside the arc.
- **Round 3** (own arc, after round 2): rules engine — the long-deferred `rule` table finally wired up. Matchers + setters + priority + Manage → Rules dialog.

**Decisions still ahead** (per-round ADRs to settle): round-2 match strategies; round-3 rule priority semantics + retroactive application; (lower priority) per-row register display rollup; (lower priority) a "Show aliases" toggle in the Payees dialog.

**Round 3 scope sharpening (added 2026-06-06)** — owner sketched the round-3 UI: a dedicated screen that shows **both** aliases (the round-1 manual mappings) and rules side-by-side, with create/delete affordances. Rule creation: matcher type (`contains` / `starts-with` / `ends-with` / `is-exactly`) + string + target canonical payee. Aliases are essentially the `is-exactly` case already, so round 3 should consider unifying the two — either by treating the round-1 `payee.canonical_id` rows as an implicit `is-exactly` rule in the unified view, or by migrating them into a single `payee_rule` (or similar) table with a `match_kind` column. Decision deferred to round-3 planning ADR; this note exists so the round-3 author starts with the unified-screen intent in hand rather than discovering it mid-build. Owner's wording: *"a dedicated screen to be able to see all of the aliases, and be able to create or delete rules. You'd be allowed to say 'if contains xxxx string, then xxxx payee' or 'starts with, ends with, is exactly' etc."*

### Reports — round 2 follow-ups (2026-06-06)

Owner saw the potential of the paintEvent approach (ADR-026) and flagged that reports has a lot more work to do — what shipped under ADR-018 is the floor, not the ceiling. Quoted reactions on the paintEvent landing: "Resizing is cleaner, the default hover over feels a bit more natural." Concrete items raised:

- ~~**Default to top-level category rollup on Spending Over Time.**~~ Shipped 2026-06-06 as **ADR-030**: new `Rollup:` combo on the report panel with three positions (Top level / Group / Leaf), default flipped from Group → Top level. New `mfl_desktop/reports.py::category_root_map` helper mirrors `category_group_map`; Leaf mode is the identity case. Categories checklist rebuilds on rollup change, all checked, Uncategorised's separate toggle preserved across all three modes.
- ~~**Hierarchical category pickers (charts, budget, transactions).**~~ Shipped 2026-06-06 as **ADR-031** — flat-with-breadcrumbs approach. `CategoryChoice` gained a `path` field; `Repository.list_categories_flat` builds it via a two-pass walk and sorts the result by path so siblings cluster under their parent. `make_category_picker` uses `c.path` as the display label (5 combo surfaces inherit the fix: transaction dialog, bulk edit, schedule, budget setup, register inline typeahead). New shared `mfl_desktop/reports.py::category_path(nodes_by_id, cid)` helper feeds the spending report's checklist so its labels show full paths too (matters most at Leaf rollup per ADR-030). Tree-popup variant rejected for v1 — flat list matches the payee typeahead pattern and stays consistent with the recently-shipped merge picker.
- **Broader reports arc.** v0.1 had a dashboard and several views that haven't been ported yet. Owner wants to come back to reports as a thread now that the rendering ceiling is clear — likely a multi-round arc like the budget one. Open questions for the arc planning: which reports to ship, in what order, against which user task (cash flow, income vs spending, category trends, account balance history, net-worth trajectory). Treat as an open thread, not a single ADR.

### Account workflows (2026-06-06)

Owner-raised additions on the same turn as ADR-032 (Vehicle account type). The per-account summary shipped 2026-06-06 (ADR-033); statement reconciliation remains an open arc.

- **Statement reconciliation per account.** Banktivity-style reconcile flow: pick a closing statement date + ending balance, walk through the account's uncleared/cleared transactions ticking off matches, surface running variance against the target, mark the matched set as `Reconciled` (the status already exists per ADR-010). Needs an ADR — UI shape (modal wizard vs side-panel), how mismatches are handled (allow a balancing adjustment txn? force the user to find the error?), how partial reconciliations are saved/resumed, and what happens to imports that arrive after a statement is reconciled. Touches the register (Reconciled status display already exists), the import pipeline (don't re-classify a Reconciled row), and probably gets its own menu under **Account → Reconcile…** with the account picker if more than one is open. **Entry point already reserved** by the per-account summary screen's "NO STATEMENTS · RECONCILE ›" placeholder row — when the arc ships, the handler is swapped to open the reconcile dialog and the layout doesn't churn.
- ~~**Per-account summary screen.**~~ Shipped 2026-06-06 as **ADR-033** + **ADR-034** (polish + drill-down). Combo chart (income/spending bars + balance line) over a six-preset period selector, right-column Summary / Additional Info / Upcoming / Reconcile placeholder, bottom-row Top Payees + Top Categories — now in card containers, with dual y-axis on the chart and clickable Top-N rows that open a `TransactionsListWindow` drill-down filtered to the clicked entity. **Follow-ups still open**: reuse `TransactionsListWindow` from other screens (Spending Report bar clicks, Net Worth account rows, payee/category dialogs' "show transactions" verbs — same dataclass, just wire each entry point); inflow Top-10 (symmetric to Top Payees but for incoming flow); cleared-only balance line variant (original ADR-033 backlog mentioned "cleared-vs-running variants"); per-account sparkline in the sidebar; saved period preference per account; custom date range; richer drill-down footer (inflow / outflow / net rather than just signed sum); drill-down for the `(No payee)` / `(Uncategorised)` synthetic buckets; reconciliation arc closes the placeholder.

### Multi-currency + transfer arc — follow-ups after 2026-06-06

The three-ADR arc (ADR-035 / 036 / 037) is shipped end-to-end. Open threads:

- **Report currency selector + conversion.** Spending Over Time, Net Worth, Budget tiles, and any cross-account aggregation in `TransactionsListWindow` need a display-currency combo with `Repository.convert_amount` plumbed in. ADR-035 §UI specifies the surface; implementation deferred until after the user has lived with the matcher + currencies dialog. Net Worth's bare pence sum is the most acute case — once USD accounts exist, the headline number is silently wrong without conversion.
- **Import-time transfer suggestion.** After commit, surface candidates the matcher found between the just-imported account and every other account ("Looks like a transfer to Joint Savings; review these N rows?"). One-click bulk accept lands in the existing `TransferReconcileDialog` flow. Deferred until after the report selector since the user's stated workflow is to import → reconcile manually.
- **`backfill_historical` wired in the Currencies dialog.** The function exists in `mfl_desktop/fx.py`; the dialog has the Add manual rate row but not the Backfill historical button. A small "Backfill USD→GBP from 2024-01-01 to 2026-06-06" verb with the API-call-cost confirmation per ADR-035 §guard-rails would close the historical-rate gap for users importing years of foreign data.
- **Edit Transfer dialog with Unlink verb.** ADR-020 backlog item; now also the natural place to surface `update_transfer_rate` for the cross-currency case (e.g. user wants to override the FX-table rate that was used at link time). Inline category edits on a transfer half should redirect to it.
- **Per-budget currency override.** ADR-035 §budget left this deferred. A budget whose perimeter spans accounts of different currencies currently denominates tile totals in `person.base_currency`; users with two home currencies (rare but real) would benefit from picking the budget's own display currency.
- **OS-keychain API key storage.** Today the openexchangerates API key lives in the `setting` table — i.e. inside the `.mfl` file. The Currencies dialog warns about this. If real key-sharing (or multi-file portability) becomes a concern, a follow-up ADR can move it to the Windows Credential Manager / macOS Keychain.
- **Per-txn currency for manual foreign-currency entries.** Today a txn's currency is its account's currency. A user who wants to record a USD-denominated charge on a GBP credit card before the card converts has no first-class shape — they enter it at the converted GBP amount with a memo note. If this comes up, ADR-035 §rejected has the design space documented.
- **N-account reconcile wildcard.** ADR-037 deliberately picked two-accounts-at-a-time. A future "(Any other account)" mode in the B combo would let users do one pass for the discovery case ("which outflows on Current look like inflows anywhere?"). Additive to the existing dialog.
- **Per-pair category override in reconcile.** ADR-037 ships one category for the whole batch. A small per-pair category combo column is a one-evening addition if real use surfaces the need (e.g. mortgage transfers vs. between-own-accounts on the same A↔B pair).
- **Live re-pair on tolerance change.** Today the reconcile dialog only re-computes pairs on Apply or after the user re-opens. A reactive update when the user tweaks `transfer_match_window_days` or `transfer_fx_tolerance_pct` would feel snappier.
- **Stepped burn-down ideal.** Existing budget arc backlog item — drops at scheduled-txn dates instead of straight-line. Independent of multi-currency.

### Other deferred items

- **Saved CSV mapping profiles.** Follow-up to ADR-021: persist the mapping the user just used (keyed by a normalised header signature) so the next Pocketsmith (or other unknown-format) import skips the wizard and commits silently. Needs its own ADR — header-signature scheme, conflict handling when export columns are renamed, profile-management UI (edit/rename/delete).
- **QIF parser.** v0.2 high-priority; lift the QIF format alongside the existing OFX/CSV parsers.
- ~~**Categorisation rules engine.**~~ Absorbed into round 3 of the payee-aliases-and-rules arc — see ADR-028 (planning) and the "Payee arc" section below.
- **Per-lot IRR / ROI.** Schema is in place (`lot`, `valuation`); no computation yet.
- **Category management UI.** Re-parent, rename, archive categories — needed to manage what import-created and to undo path-conflict separations after the fact.
- **Dashboard.** v0.1 had it; needs porting to Qt — charts per ADR-026 (paintEvent), reusing `mfl_desktop/ui/chart_helpers.py`.
- **Account / settings management UI.** Add / edit / archive accounts, set base currency.
- **Valuation pipeline for non-cash accounts.** `valuation` table exists since ADR-010 but isn't wired. Vehicles (ADR-032), property, and investment accounts would all benefit. Mark-to-market source per family type (KBB/Autotrader for vehicles, land-registry/Zillow for property, broker feed for investments).
- **Packaging.** Single-file `.exe` via PyInstaller per ADR-008.

---

## Architecture Decision Records

| ADR | Topic | Status |
|---|---|---|
| ADR-001 | Backend language and triple store | Partially superseded by ADR-009 |
| ADR-002 | Frontend stack (HTMX) | **Superseded by ADR-008** |
| ADR-003 | Packaging (PyInstaller / AppImage) | Applies (Windows-first prioritised) |
| ADR-004 | Cross-platform portability | Applies |
| ADR-005 | Ontology strategy (MRL dependency) | Applies (reference-only for MFL) |
| ADR-006 | Instance IRI naming | Applies (IRIs become text keys in SQLite) |
| ADR-007 | Data access patterns (quad vs SPARQL) | Legacy-code only |
| ADR-008 | Desktop UI framework — PySide6 | **Accepted 2026-06-05** |
| ADR-009 | Storage engine — SQLite | **Accepted 2026-06-05** |
| ADR-010 | Transactional schema design | **Accepted 2026-06-05** |
| ADR-011 | Account delete policy — hard delete now, archive reserved | **Accepted 2026-06-05** |
| ADR-012 | Payee name-management policy | **Accepted 2026-06-05** |
| ADR-013 | Category management policy | **Accepted 2026-06-05** |
| ADR-014 | Category kind (income/expense/transfer) | **Accepted 2026-06-05** |
| ADR-015 | Account folders in the sidebar | **Accepted 2026-06-05** |
| ADR-016 | File save / open model — auto-commit + Save Copy As snapshots | **Accepted 2026-06-05** |
| ADR-017 | Bulk edit shape — modal dialog with per-field checkboxes | **Accepted 2026-06-05** |
| ADR-018 | Reports framework + first chart — Spending Over Time | **Accepted 2026-06-05** |
| ADR-019 | Net Worth report — three-column Assets / Net Worth / Debts | **Accepted 2026-06-05** |
| ADR-020 | Account transfers — category-driven, two linked txns sharing one transfer_id | **Accepted 2026-06-05** |
| ADR-021 | Generic CSV column-mapping wizard | **Accepted 2026-06-05** |
| ADR-022 | Register typeahead delegates + inline category create | **Accepted 2026-06-05** |
| ADR-023 | Scheduled transactions (bills, recurring income, recurring transfers) | **Accepted 2026-06-05** |
| ADR-024 | Budget core — perimeter + per-category targets + screen | **Accepted 2026-06-05** |
| ADR-025 | Budget visualisations — burn-down + summary bar + cadence subtitles + scheduled projection | **Accepted 2026-06-06** |
| ADR-026 | Visual style baseline (Fusion + custom palette) and chart-engine paintEvent | **Accepted 2026-06-06** |
| ADR-027 | Create Schedule From Transaction — right-click verb on the register | **Accepted 2026-06-06** |
| ADR-028 | Payee aliases, canonical labels, and the auto-categorisation arc (planning) | **Proposed 2026-06-06** |
| ADR-029 | Payee aliases round 1 — data model + manual alias UI | **Accepted 2026-06-06** |
| ADR-012 (amend) | Canonical / alias model + Merge/Alias/Delete verb table | **Accepted 2026-06-06** |
| ADR-030 | Spending Over Time rollup levels — Top / Group / Leaf | **Accepted 2026-06-06** |
| ADR-031 | Hierarchical category picker via full-path labels | **Accepted 2026-06-06** |
| ADR-032 | Vehicle account type and the `vehicle` family | **Accepted 2026-06-06** |
| ADR-033 | Per-account summary screen — combo chart, period selector, top-N breakdowns | **Accepted 2026-06-06** |
| ADR-034 | Per-account summary polish — dual-axis chart, section cards, Top-N drill-down | **Accepted 2026-06-06** |
| ADR-033 (amend) | Period preset set updated; Custom range added | **Accepted 2026-06-06** |
| ADR-035 | Multi-currency foundation — fx_rate, transfer parent, settings, openexchangerates integration | **Accepted 2026-06-06** |
| ADR-036 | Transfer matching — link existing other-side row instead of always creating a partner | **Accepted 2026-06-06** |
| ADR-037 | Bulk transfer reconcile — Manage → Reconcile Transfers… dialog | **Accepted 2026-06-06** |
| ADR-035 (amend) | Cross-currency entry collects partner amount at the point of creation | **Accepted 2026-06-07** |
| ADR-038 | Banktivity CSV import trusts the amount sign over the Type column | **Accepted 2026-06-07** |

Full index and summaries: [`docs/adr/README.md`](docs/adr/README.md).

---

## Known pitfalls

Most legacy-specific pitfalls only matter while maintaining the v0.1 web app; the cross-cutting ones carry into the rewrite.

**Carry forward:**
1. **Windows date formatting** — `%-d` doesn't work. Use `f"{d.day} {d.strftime('%b %Y')}"`.
2. **Full file replacements lose manually-added code** — verify imports survive after any full Write. The `import hashlib` / `compute_hash` pair was lost more than once in v0.1; don't repeat the mistake on the rewrite.
3. **IRI namespace discipline** — Transactions are MFL namespace, accounts/person are MRL. Carrying this into SQLite as stored strings means the mistake travels silently if you generate the wrong prefix on insert.
4. **CSV import hashes can collide within one batch.** The composite `date|amount|payee_raw` hash isn't unique across a single file — two coffees on the same day at the same price, or any rows with an unmapped payee column, will collide. `_classify_and_stage` resolves this with a deterministic `:N` suffix; any future rewrite of the staging path must preserve this or the `UNIQUE(account_id, import_hash)` constraint will fire at commit.
5. **Transfer parent row must always be inserted alongside both half-rows.** Per ADR-035, every transfer pair has three writes — two `txn` rows + one `transfer` row — and they must travel together in one SQL transaction. `_insert_transfer_parent` is the single entry point; `create_transfer`, `_convert_to_transfer_unbatched`, `_link_transfer_unbatched`, and the transfer branch of `post_scheduled_txn` all call it. Any future code path that writes a transfer (e.g. a CSV import that detects pairs at staging time, or a split-transaction feature that creates a transfer leg) must call `_insert_transfer_parent` too, or reports that read `transfer.rate` will silently miss the rate-of-record. The existing tables have no FK from `txn.transfer_id` to `transfer.iri` — by design, matching ADR-020's "id space is conceptual" — so SQLite won't catch the omission.
6. **FX rate lookup direction matters.** `Repository.get_fx_rate_nearest(date, base, quote)` walks six steps including inverse and USD-pivot fallbacks because openexchangerates' free tier stores everything `USD → X`. Asking for `X → USD` without the inverse fallback returned no rate before this was fixed — the symptom was a fully cross-currency matcher silently returning zero candidates. Any future provider integration that bypasses `get_fx_rate_nearest` (e.g. a direct ECB feed that stores `EUR → X`) needs to either populate both directions or extend the lookup chain.

**Legacy-only:**
4. **pyoxigraph ASK returns bool** — `bool(store.query("ASK {...}"))`.
5. **`@app.on_event` deprecated in FastAPI** — use lifespan context manager.
6. **Starlette 1.0 TemplateResponse** — `request` is first positional arg, not in context dict.
7. **mfl-ontology.ttl must not be empty** — was created as 0-byte placeholder originally.

---

## Development workflow

### Run the desktop app (the live target)
```powershell
# from the project root, with an activated venv
pip install -r mfl_desktop\requirements.txt

# one-time: create the database and seed a first account
python -m mfl_desktop.cli init

# optional: add more accounts
python -m mfl_desktop.cli add-account "Joint Savings" --type savings
python -m mfl_desktop.cli add-account "Amex Gold"     --type credit

# launch the GUI
python -m mfl_desktop
```

### CLI smoke tests
```powershell
python -m mfl_desktop.cli import path\to\statement.qfx
python -m mfl_desktop.cli list --limit 30
python -m mfl_desktop.cli categories
```

### Run the v0.1 web app (legacy, still works on Oxigraph)
```powershell
python main.py
# open http://127.0.0.1:8000
```

### Run the original PySide6 prototype (reference only)
```powershell
cd prototype_register
.\.venv\Scripts\Activate.ps1
python register_proto.py
```

### Starting a new session
1. Read this file.
2. Skim [`docs/adr/README.md`](docs/adr/README.md) for decision context.
3. Check memory under `~/.claude/projects/.../memory/` for owner preferences and project state.
4. Confirm where work is in flight before editing.
5. Deliver complete files; verify imports survive.
6. New architectural decisions need a new ADR (even if owner says "your call").
