# My Financial Life — 1.0 Launch Backlog

**Status:** Draft for owner review — 2026-06-16
**Goal:** Take MFL from "feature-rich personal build" to a **paid, store-distributed, legally-operable product** on macOS + Windows, with automated Bank Feeds (Enable Banking first) switched on once the legal/infrastructure prerequisites exist.

This is the definitive 1.0 plan. It is organised into **seven workstreams** that run partly in parallel, a **critical path**, a **definition of done** (the 1.0 gate), and a short list of **decisions only the owner can make** (flagged ⚠ DECISION).

### Decisions locked (2026-06-16)
- **✅ K0 — Distribution:** **Direct signed+notarised downloads for 1.0**; Mac App Store + Microsoft Store deferred to 1.1/1.2 (no sandbox rework in 1.0).
- **✅ C1 — Pricing:** **One-time perpetual purchase, everything included** (~£25–45); 2.0 is a paid upgrade. Needs an offline license-key mechanism.
- **🕓 F1a — Enable Banking key custody:** **Deferred** — ship the existing **BYO** cores as-is (automated feeds are 1.1 anyway); the hosted-vs-BYO call is made after the B3 compliance read.
- **✅ M — My Retirement Life integration:** **bundled/combined offering**; technical integration is a **1.1 fast-follow** as a **file-based MFL→MRL RDF export**. 1.0 only preserves the IRI boundary, discloses the hand-off in the privacy policy, and markets the bundle. See workstream M.
- **✅ P1a — Date format:** **ISO `yyyy-MM-dd`** for all date input fields (owner correction 2026-06-16 — the original `d MMM yyyy` guess was reversed; the app's input fields are now uniformly ISO). Prose custom-range *summary labels* (`Custom: 1 Jun → 6 Jun`) stay as compact prose.
- **Open B1a — Entity/jurisdiction:** UK Ltd is the default working assumption; confirm with an accountant.

---

## 0. The shape of the problem

The trigger for this plan was looking at Enable Banking and realising the *app-level* integration is the easy part — the hard part is everything a real software business needs around it. Enable Banking **Production / full activation** requires (their words): a verified **contract**, a completed **KYC** process, a **billing account**, an **application description shown to end users**, a **data-protection email**, a **privacy policy URL**, and a **terms-of-service URL**. None of those exist yet.

So the real dependency chain is:

```
Legitimate business + legal docs + hosted website
        │
        ▼
Finished, polished, packaged, signed 1.0 app  ──►  Store presence + payments
        │
        ▼
Enable Banking Production activation (KYC + contract + privacy/ToS URLs)
        │
        ▼
Automated UK/EU bank feeds live for paying users
```

The app itself can ship and sell **before** automated feeds are live — file import + OFX Direct Connect already work. **Automated Enable Banking feeds are a fast-follow (1.1), not a 1.0 blocker.** That keeps the launch from being held hostage by KYC/contract timelines.

---

## 1. Workstream P — Product & engineering polish (finish + tighten 1.0)

The app is feature-complete; 1.0 is about **consistency, removing overlap, and making everything cross-link**. Findings below are grounded in a code audit (file references included).

### P1 — Standardise date & period pickers ⭐ (owner explicitly asked)
**Problem (audited):** the app has **two date-display formats** and **three+ period-preset vocabularies**:
- Display formats split: human dialogs use `d MMM yyyy` (account-summary custom range, register filter popover, goals, reconcile); data-entry forms use ISO `yyyy-MM-dd` (new txn, investment txn, splits, prices, FX, schedules). `schedule_dialog.py` is the lone `QDateEdit` with **no calendar popup**.
- Period presets diverge across windows:
  - Register: `30d / 90d / 6m / 12m / YTD / All`
  - Reports (Spending, Income & Expense, Payee, Category & Payee): `Quarter / 6m / YTD / 1y / 3y / Custom`
  - Investment Returns: `YTD / 1y / 3y / 5y / Max / Custom`
  - Sankey: `YTD / MTD / Last month / Custom`
- Period-bounds logic is duplicated (`register_window._months_before`, `account_summary.period_bounds`, `reports/filters`), and the preset→label dict is copy-pasted into **8 files**.

**Status: part A shipped 2026-06-16 (ADR-082); part B (date-format rollout) remains.**

**Part A — period vocabulary single-sourced (DONE):**
- [x] New Qt-free **`mfl_desktop/periods.py`** = single source of truth: per-context preset sets (register/report/investment/sankey, keys unchanged so saved `filters_json` is stable), one `period_bounds(...)` for every key, one `PERIOD_LABELS` registry, `months_before`/`period_since`/`fmt_date_range`/`period_label`/`labels_for`/`options_for`. _(Named `periods.py` not `ui/date_presets.py` — it's Qt-free compute layer, so dialogs/windows/CLI can all share it.)_
- [x] New Qt **`mfl_desktop/ui/date_widgets.py`** = `make_date_edit(...)` (calendar popup + ISO `yyyy-MM-dd`) + `make_period_combo(...)`.
- [x] **P1a → ISO `yyyy-MM-dd`** (owner correction 2026-06-16; factory default + the 4 remaining human-format input sites flipped to ISO → all date inputs now ISO).
- [x] `account_summary` + `reports/filters` re-export the vocab (zero importer churn); register drops `_months_before`/`_current_since` body; Sankey + Investment windows drop inline `_resolve_bounds`; all 10 `_PERIOD_LABELS` dicts removed.
- [x] Fixed a latent bug: `"6m"` meant 180 days in reports but calendar-6-months in the register → now calendar-accurate everywhere (reports' rolling windows shift 0–3 days, intended).
- [x] Verified: periods unit tests, delegation parity + saved-filter round-trip, app import, offscreen filter-dialog + register checks.

**Part B — `make_date_edit` adoption (DONE 2026-06-18):**
- [x] Replaced 17 ad-hoc `QDateEdit()` sites across 12 files with `make_date_edit(...)` (incl. the no-popup `schedule_dialog`); `maximum_today=True` where a future date was previously forbidden (custom-period, reconcile end-date). Display-only (value read via `.date()`), no persistence impact. Left only `delegates.py` (inline cell editor — needs a `parent` the factory doesn't take; already ISO + popup).
- [x] Adopted `make_period_combo` in all five filter dialogs (spending / income & expense / payee / category & payee / investment returns) and `periods.period_label(...)` in the five report windows → **no runtime `_PERIOD_LABELS` references remain**.
- [x] Verified headless: py_compile clean; 23 touched modules + main app modules import offscreen; all 5 filter dialogs + 7 date dialogs construct against a seeded DB (period combos populate, selection round-trips, edits are ISO + calendar popup).

### P2 — Make every report clickable & cross-linked ⭐ (owner explicitly asked)
**Problem (audited):** 5 surfaces drill, 4 are dead-ends. Drill-capable: Spending, Payee, Category & Payee, Account Summary (all → shared `TransactionsListWindow`), plus Home (navigation) and Budget (→ `BudgetDrillDownWindow`). **Dead-ends: Net Worth, Income & Expense, Sankey, Investment Returns.**

**Status: shipped 2026-06-18 as ADR-083.** All five resolved; two new `TxnListFilter` dimensions (kind, security) reused the shared `TransactionsListWindow`.
- [x] **Sankey** — `SankeyNode.category_id` + `node_clicked` hit-test → `for_category` (+descendants) over the report's period/account scope. _Headline report, done._
- [x] **Income & Expense** — chart `segment_clicked(kind, bucket_key)` + new pure `income_expense.bucket_bounds` → `for_kind` (non-transfer, kind's category set, sign-by-kind — reconciles with the bar).
- [x] **Investment Returns** — chart is portfolio-level, so the per-security **table** drills: sid stashed on the row, double-click → `for_security`.
- [x] **Net Worth** — `DonutChild.account_id` + `account_clicked` → window `account_activated` → `register_window._open_account_summary` (owner fork: **Account Summary page**, not the flat register).
- [x] **Budget drill-down → transactions** — **already satisfied**: the matrix double-click-drills an Actual cell into `BudgetDrillDownWindow` (editable txn-id-set register, reconciles exactly). Deliberately kept over the shared window (perimeter bucketing can't be expressed by the shared filters).
- [x] Drill consistency audited — Sankey/I&E/Returns all open the one `TransactionsListWindow`; Net Worth opens the canonical Account Summary; Budget keeps its precise bespoke register (documented).

### P3 — Remove overlapping functionality ⭐ (owner explicitly asked)
**Status: COMPLETE — P3a (ADR-084, 2026-06-18), P3b (ADR-096, 2026-06-21), P3c confirmed no-change.** Guiding rule established in ADR-084: consolidate divergent *duplicates of the same thing*, never prune distinct *affordances*.
**Candidates (audited):**
- [x] **Six report filter dialogs ~40% copy-paste — DONE (P3a, ADR-084).** Extracted a **toolkit** `ReportFilterDialogBase` (opt-in builders + shared helpers + statics); each dialog keeps its specials (rollup/securities/top-N/category tree). Constructor signatures + saved `filters_json` unchanged; verified headless across all six.
- [x] **Schedules' 4 entry points — AUDITED, KEPT.** Confirmed legitimate affordances, not redundancy: Manage ▸ Schedules / filter-bar button (carries the overdue badge) / Home "Bills due" card are three discovery paths to the same `SchedulesDialog`; the right-click "Create Schedule From Transaction" opens a *different* seeded dialog. No amputation.
- [x] **P3b — Two transfer-matching UIs** (inline ADR-036 confirm/picker vs Manage ▸ Reconcile Transfers ADR-037) — **DONE (ADR-096, 2026-06-21).** Extracted the duplicated `_CHIP_COLOURS` / `_fmt_amount` / strength-chip into shared `mfl_desktop/ui/transfer_chips.py` (`CHIP_COLOURS` / `fmt_amount` / `strength_chip` / `strength_chip_holder`); both dialogs import them aliased to their old private names so call sites are untouched; row layouts stay bespoke. Removed the now-dead locals + unused imports (`QSizePolicy` / `QPalette` / `Decimal` / orphan `_fmt_rate`). Verified offscreen + full app import.
- [x] **`TransactionsListWindow` only reachable via drill — CONFIRMED intentional** (good detail view; nav is breadcrumb-chip removal + period swap, consistent per ADR-083). No change. _(P3c affordance-audit = this + schedules, recorded in ADR-084.)_

### P4 — Visual & interaction polish for a paying audience
**Status: COMPLETE 2026-06-21 — dark-mode pass + button audit (ADR-097), brand re-tone to teal+gold (ADR-100), app icon (ADR-101), typography scale (ADR-102).**
- [x] Consistent dialog sizing, button order (platform-native: macOS vs Windows), default-button + Esc behaviour across all dialogs. **AUDITED → already consistent (ADR-097).** ~40 of ~47 dialogs use `QDialogButtonBox` (native ordering + Esc + role-default for free); the remaining hand-rolled rows are action-toolbar management dialogs (New/Edit/Delete + Close) where a default button would make Enter destructive — correctly left without one. No change required.
- [x] Dark-mode pass over any surface added since ADR-076 — **DONE (ADR-097).** The newly-called-out surfaces (commission/total-cost investment fields, Bank Feeds, loan, bonds/options) were already clean (paintEvent + `chart_helpers`). Swept the same item-brush bug class ADR-076 r3 fixed and closed four stragglers: `net_worth` "no rate" flag, `investment_returns` gain/loss table + Performers labels, `statements` status column + summary, `transfer_match` picker sentinel — all now resolve `tokens.c(...)` live (light values unchanged). No frozen hex inside `themed()` templates.
- [ ] Empty-state / first-run polish (the Home dashboard cards already self-hide; verify a brand-new seeded DB looks intentional, not empty/broken). **→ folded into P5 (onboarding).**
- [x] Spacing/typography scale (the deferred Arc B round) — at least a consistent scale, not pixel-perfect. **DONE (ADR-102).** Defined `mfl_desktop/ui/type_scale.py` (9 named px steps reverse-engineered from existing use) + folded the two off-scale one-offs (14→LEAD 15, 17→SUBTITLE 18). Every `font-size` now on the scale. (A full `fs()` migration + a spacing-token system noted as an optional future round.)
- [x] Iconography + app icon (needed anyway for stores). **DONE (ADR-101).** Extracted the MFL hexagon from the brand artwork → `assets/icons/` (PNG set + macOS `.icns` + Windows `.ico`); `resources.py` (frozen-build-safe) wires it as the runtime window/dock icon; `.icns`/`.ico` ready for the packaging step.

_Brand re-tone (ADR-100): the app accent moved blue-600 → icon teal + a gold brand token, off the app-icon artwork — see the colour-scheme note below / CLAUDE_CONTEXT._

### P5 — First-run onboarding & help
**Status: DONE 2026-06-21 (ADR-098).**
- [x] A short first-run flow: create/seed file, **pick base currency**, optional **"import your first statement" nudge** — `FirstRunDialog` (`first_run_dialog.py`), shown once when `__main__` just seeded a brand-new file. New `Repository.set_base_currency` (writes the `setting` the app reads + `person.base_currency` together); applies the chosen currency + name to the starter account; "Import a statement…" routes to `RegisterWindow.start_first_run_import`. Closes the long-standing GBP-hardcode gap.
- [x] In-app **Help / Getting Started** (links to website docs) + an **About** box — Help ▸ Getting Started + Visit Website open `version.DOCS_URL`/`WEBSITE_URL` (placeholders on the launch domain until W1 ships); About box already shipped (ADR-079).
- [x] Crash-safety review — **confirmed, no change needed for 1.0.** K0 = direct signed+notarised (non-sandboxed), so the ADR-057 Snapshots/Library-beside-the-file + appdata model works as-is; the sandbox conflict is a K3/1.1+ concern (already scoped there).

### P6 — Release engineering hygiene
**Status: dep-pinning + version/build metadata + crash log/diagnostics + test gate done 2026-06-21 (ADR-099); per-OS PyInstaller build env stays with the packaging round (ADR-078 / K1-K2).**
- [~] Pin dependencies; produce a reproducible build env per OS. **Deps pinned (ADR-099):** new `requirements-desktop.txt` pins the *actual* desktop runtime — PySide6 / cryptography / ofxtools (the old root `requirements.txt` is the legacy web-app's deps and didn't even list PySide6). Per-OS PyInstaller specs + full lock are deferred to the packaging round (ADR-078, no build scripts yet).
- [x] Version string + build metadata surfaced in About and in crash reports — **DONE (ADR-099).** `version.build_revision()`/`build_string()` (reads a CI-stamped `_build_info.py`, "source" fallback); About shows a "Build {rev}" line; diagnostics + the crash log header carry the full env string.
- [x] Basic automated test pass green — **DONE (ADR-099).** `tests/` IRI guard green + an import-all/`compileall` smoke (135 modules import clean offscreen) as the cross-module gate. CI matrix on both OSes pends the packaging round.
- [x] Decide a crash/error-reporting approach — **DONE (ADR-099): local-log + Export Diagnostics, no telemetry.** `diagnostics.py` = rotating file log + a last-resort excepthook (logs + a non-fatal "your data is safe / log is here" dialog) + Help ▸ **Export Diagnostics…** (PII-light blob to a user-chosen file). Nothing leaves the device.

### P7 — Post-feature polish (gaps surfaced after the 2026-06-16 plan)
**Why this exists:** P1–P6 closed the polish list as scoped on 2026-06-16. Everything below either (a) is a discoverability/UX gap the original audit didn't name, or (b) covers functionality shipped *after* the plan was written (bonds/options ADR-093, loans ADR-095, budget bills ADR-094, Investment Income report ADR-108, reports-include-closed ADR-115). Tracked here so the manual (W2) has a stable UI to document.
- [x] **Quick-action header / toolbar — DONE (ADR-116).** A persistent top `QToolBar` on the main window: **Home · Update Prices · Update Rates · Update All**. Closes the owner-flagged "Home is easily missed as a sidebar row" (ADR-075) and surfaces price/FX refresh that were three clicks deep in Manage ▸ Securities / Currencies. The update buttons fetch **directly** (no dialog) via the same synchronous force-refresh those dialogs' Refresh-Now buttons use (`prices.refresh_latest_prices_into` / `fx.refresh_latest_into`, `force=True`), refresh sidebar balances after, report counts on the status bar, and route to the relevant dialog when the API key is unset. **Update All** runs prices + rates in one click (the F2 "Update all"), skipping a provider whose key is unset (reported in the status line, no dialog) and surviving a single provider's failure; bank feeds stay in their own consent dialog.
- [x] **Brand chrome in the persistent UI — DONE (ADR-117).** The everyday surfaces were all text; the MFL + Garelochsoft logos only appeared in transient places (About/splash/first-run). Added an **MFL brand header atop the sidebar** (hexagon mark + "My Financial Life" in brand teal) and pinned the **Garelochsoft wordmark to the status bar** (owner pick from four mocked options). Knocked the flat light background out of the supplied logo art → transparent assets (new `tools/make_transparent_logos.py`, new `resources.brand_mark`) so they read cleanly in dark mode; the dock/taskbar icon set is untouched.
- [ ] **Broader iconography pass (UI still text-heavy)** — owner's underlying note was the whole UI leans on text. Candidates beyond logos: icons on the quick-action toolbar actions (currently text-only) and/or menu/sidebar glyphs. Deferred — needs an icon set + a consistency pass; not blocking.
- [ ] **Per-feature in-app help coverage** — the newest features have no Help/Getting-Started entry: bonds & options, loan accounts + amortization, budget bills + burn-down, Investment Income report, reports-include-closed default. Decide what's in-app (P5 Help links) vs. only in the website manual (W2). _Do not write screenshots before the UI is frozen._
- [ ] **Investment Income filter dialog** is its own class, not on the shared `ReportFilterDialogBase` (ADR-084) — a minor P3-style consolidation candidate. Low priority; only if touched anyway.
- [x] **Compact file — reclaim SQLite free space (in the Manage Data / Data Library space) — DONE (ADR-137).** Owner-reported the `.mfl` grew ~13 MB → 20 MB in a week with little new data; the cause was ~6 MB of free pages left by deletes (payee merges, removed goals/accounts) + the migrations that rebuild whole tables (ADR-032 CHECK recipe), which SQLite never returns to the OS with `auto_vacuum` off. Added `Repository.compact()` (commit → WAL checkpoint → `VACUUM` → checkpoint; keeps every row) and a **Compact file…** button in the Data Library dialog that shows the before→after reclaim. Verified: 18.05 → 11.19 MB (−38 %), all rows intact, `integrity_check = ok`.
  - [ ] **Follow-up (optional): auto-offer a compact** when `freelist_count` crosses a threshold (e.g. after a big payee merge or a migration run), gated behind a one-time prompt — so the file self-heals without the owner remembering the button. Deferred deliberately: VACUUM rewrites the whole file + needs temp disk, and the migration-driven slack is largely one-off, so the manual control ships first.
- [ ] **UI-freeze checkpoint before the manual** — once P7's UI items land, declare the UI steady-state so W2's screenshots don't go stale (owner's explicit sequencing: manual is the *last* task).

### Open non-code owner action (carried from `backlog_notes.txt`)
- [ ] **SUSA — 7 phantom shares in MS Access - Mark (£1,090), unresolved (ADR-155).** The 2021-01-05 sale of **56** shares runs against only **49** ever bought, so the holdings engine clamped the oversell to zero and a later plug materialised 7 basis-less shares that still show as a holding. Every other oversell in the file was repaired; this one is too large to be rounding, so it means a **purchase of ~7 shares that was never imported**. Owner checked (2026-07-12) and **can find no such transaction on the statements**, so the gap can't be closed from source documents. Whichever repair we eventually pick trades one inaccuracy for another, and that's the owner's call:
  - *Add the 7 shares* (`tools/repair_share_oversells.py --add-shares "MS Access - Mark:SUSA"`) — the position clears and the sale keeps matching the statement's 56 shares, but the added shares carry **no cost basis**, so SUSA's realised gain is overstated by whatever they cost.
  - *Trim the sale to 49* — the position clears and the gain is exactly right, but the row **no longer matches the statement** (implied price becomes $93.64 against the recorded $81.939).
  - *Leave it* (current state) — the £1,090 phantom stays in the portfolio total, and SUSA keeps showing as held.
  Not blocking 1.0; the app no longer *creates* this (ADR-155's Sell to clear), and the repair tool names it on every run.
- [ ] **Post-ADR-112 category triage** — the pre-ADR-112 REI Master Card import forked 6 curated categories. Redo the merges by hand in Manage ▸ Categories (now also records import mappings, inoculating future imports): `Bills:Utilities:Cable and Internet`→`Bills:Cable and Internet`; `Bills:Utilities:Mobile Phone`→`Bills:Phone`; `Personal:Education:Tuition`/`:Books`→`Personal:Education`; `Fees:Charges`→owner's choice; `Cash`→`Personal:Cash`; then delete the emptied `Bills:Utilities` + `Fees`. Optionally enable "Match imports only".

---

## 1b. Defect register (opened 2026-07-12)

**Why this exists:** there was no bug list. Defects were recorded only as "known limitation" footnotes inside individual ADRs, so nobody could see them together — which is how a *wrong-number* bug (ADR-159) sat in a daily-use report, filed as a cosmetic one. This is now the single place a known defect is written down. Add to it rather than to an ADR footnote.

**Caveat on completeness:** the list below is what is *known*, not the result of an audit. It is biased toward code recently worked on (reports, Home, the DB layer). **Imports, budgets, reconcile, scheduled transactions and bank feeds have not been examined for defects.** An audit of those is unscheduled and worth doing before 1.0.

### Open — correctness

- [ ] **Multi-currency folder sums are a naive sum.** A sidebar folder mixing GBP and USD accounts shows an arithmetically meaningless total (no FX conversion). Known and deliberately deferred when it was written; the owner's file *does* now have both currencies, so this is live. Fix mirrors ADR-159: convert per account currency, or show per-currency subtotals.
- [ ] **Kind-drill misses splits.** The category-kind drill matches a row's own `category_id`, but a split transaction's parent row carries the parent/`NULL` category — so a split whose *lines* carry the kind's categories is skipped. (Recorded in the ADR that introduced the drill.)
- [ ] **Splits import as a single row** — import fidelity loss; the split structure is flattened.
- [ ] **A merged security's saved report silently matches nothing.** An Investment Returns report whose filter pins an *absorbed* security id keeps that stale id after a merge; it matches no rows and reports empty rather than erroring or re-pointing.
- [ ] **Historical net worth understates early points** when FX/price history doesn't reach back far enough — accounts drop out of early buckets. A banner warns, but the series is still wrong at the left edge.
- [ ] **First price backfill exceeds the Tiingo rate limit.** The first catch-up launch wants ~58 requests against a 50/hour cap → 429s partway through.

### Open — performance (from the 2026-07-12 instrumented-launch investigation)

- [ ] **Quitting the app freezes for ~2.4s** — three stalls at shutdown (1204 + 922 + 320 ms). Undiagnosed; the likely suspect is the WAL checkpoint on clean close (ADR-057).
- [ ] **Unexplained ~1,596 ms UI freeze during launch** — caught by a UI-thread watchdog but it landed outside instrumented code, so the cause is unknown.
- [ ] **`AccountSummaryWindow`, `BudgetWindow` and `TransactionsListWindow` reload on every `WindowActivate`** — the same anti-pattern ADR-156/157 fixed for Home, still present on three windows. Alt-tabbing to any of them re-runs a full query.
- [ ] **`list_category_tree()` is a correlated subquery** re-scanning the split-unrolled view once per category (200 passes over 35k rows, 50–75 ms) — and **every report constructor calls it twice**. A single `GROUP BY` + `LEFT JOIN` returns identical results in half the time. ~100 ms off every report open.

### Open — consistency

- [ ] **The currency-symbol map is duplicated** in `home_view` and `sankey_report_window`. Both are correct today; they should collapse onto `chart_helpers.currency_symbol()` (ADR-159) next time either is touched.

### Closed recently

- [x] **Spending / Income Over Time summed currencies 1:1 — DONE (ADR-159).** The aggregates had no currency awareness at all: dollars were added to pounds and the result stamped with a `£`. On the owner's file (25 USD + 13 GBP accounts) 2025 income read 416,906 where the true figure is 325,410 GBP — overstated ~28%. Both reports now convert and gained a "Display in" selector. **Any figure previously read off these two reports should be treated as wrong.**
- [x] **Cash Flow Sankey showed a 98% saving rate — DONE (ADR-158).** The Savings node was divided by *expenditure* (which excludes it) instead of income, contradicting the rail's own "Saving rate: 49.4% of income" on the same screen.
- [x] **Home rebuilt itself 5× at launch — DONE (ADR-156 + ADR-160).** 1,169 ms of redundant synchronous rebuilds including a 797 ms UI freeze, via three unguarded paths (window activation, sidebar navigation, background-card arrival). Now 2 rebuilds / 405 ms.
- [x] **Latent crash: background Home pass emitted from a destroyed QObject — DONE (ADR-156).** Quitting with a pass in flight raised `RuntimeError: Signal source has been deleted` on the worker thread.

---

## 2. Workstream K — Packaging, signing & store readiness

> **⚠ SUPERSEDED — distribution reversed twice since this section was written (2026-06-16). Current plan:**
> - **Windows → Microsoft Store-only** (MSIX, MS-signed, MS = Merchant-of-Record) for 1.0.1 — **ADR-123** (supersedes K0 below). 1.0 stays a local Inno `.exe` for early-access sideload (ADR-122).
> - **macOS → Mac App Store-only** (sandboxed, paid up-front) — **ADR-125** (reverses ADR-123's "macOS out of scope"; implements the macOS leg of K3). The K1 direct Developer-ID DMG (ADR-104) is demoted to a dev-only convenience.
> - **Net effect:** both stores sign + act as Merchant-of-Record; we self-manage no signing certs or cross-border tax. The K0 "direct-first, stores deferred" decision and the K1/K3 framing below are **historical** — read them for the sandbox/effort analysis, not the current plan. The MAS sandbox work K3 worried about is now the **active macOS arc** (ADR-125 A–F, engineering complete + tested via an ad-hoc sandboxed build).
> - **macOS sandbox constraint discovered (2026-06-30, ADR-125 addendum):** a **live SQLite DB cannot run on iCloud Drive** under the App Sandbox (the fileprovider blocks WAL sidecars + file locks; no entitlement fixes it; it's also corruption-risky on any cloud sync even outside the sandbox). The working `.mfl` must be **local**; iCloud is for exported backups only. This **re-opens the file-location UX** (the "first-run choose a folder" step leads users into iCloud) — owner to choose container-default vs a local-only folder picker; a clean "import an existing file" (copy-to-local) flow is also needed. Affects how an existing iCloud-stored `.mfl` migrates into the sandboxed app.

### ✅ DECISION K0 (LOCKED 2026-06-16; SUPERSEDED by ADR-123/125) — Direct signed+notarised downloads for 1.0; App Stores deferred to 1.1+
There are **two different things** the owner referred to as "notarised … so it's seen as legit and safe," and they are NOT the same effort:

| Path | Trust signal | Effort | Tax/payments | Sandbox? |
|---|---|---|---|---|
| **Direct download, signed + notarised** (macOS: Developer ID + notarised DMG; Windows: signed installer / signed MSIX) | Passes Gatekeeper / SmartScreen — *already* "legit and safe", no store needed | **Low–medium** | You handle (use a Merchant-of-Record — see C2) | **No** (full file access) |
| **Mac App Store + Microsoft Store** | Store badge + auto-update + store handles payment/tax | **High** (sandboxing, review, store accounts, entitlements) | Store handles (15–30% cut) | **Yes on MAS** — conflicts with our file model |

**The sandbox tension is real and specific to our app:** MFL opens *arbitrary* `.mfl` files anywhere, writes `Snapshots/` and `Library/` folders *beside* the live file, and has the ADR-050 cwd bridge. The Mac App Store sandbox forbids that without **security-scoped bookmarks** and rework of the snapshot/library location model. Direct-notarised has none of those constraints.

**Decision (locked 2026-06-16):** **Ship 1.0 as direct signed+notarised downloads** (fast, full trust, no sandbox rework); **App Stores are a 1.1/1.2 channel** once revenue justifies the sandbox work. This is the single biggest scope lever in the whole plan, and it removes the K3 sandbox work from the 1.0 critical path entirely.

### K1 — macOS direct (Developer ID)
**Build scaffold DONE 2026-06-21 (ADR-104) — signing/notarisation pending the Apple account.**
- [x] PyInstaller build → `.app` → DMG — **DONE (ADR-104).** `packaging/mfl.spec` + `build_macos.sh` produce a runnable `My Financial Life.app` + `.dmg`; verified the frozen app bundles all 31 migrations + icons and bootstraps a DB. Signing/notarisation are env-gated in the script (one command once the identity exists).
- [ ] Apple Developer Program enrolment (org, once the LLC exists — see B1). **$99/yr.**
- [ ] Developer ID signing → **notarisation** → stapled DMG (script-ready: set `MACOS_SIGN_IDENTITY` + `AC_NOTARY_PROFILE`). Closes ADR-050 Tier-3.
- [ ] Hardened runtime + entitlements; verify FX/price network calls and file access work notarised.
- [ ] Sparkle (or equivalent) auto-update feed hosted on the website.

### K2 — Windows direct
**Build scaffold DONE 2026-06-21 (ADR-104) — signing/installer pending the cert; build unrun locally (no Windows host), exercised by CI.**
- [x] PyInstaller build (folder+exe) — **DONE (ADR-104).** Same `mfl.spec` + `build_windows.ps1`; runs on the CI Windows runner.
- [ ] **Code-signing certificate** — ⚠ OV certs now effectively require hardware tokens/cloud HSM; EV smooths SmartScreen (~$200–400/yr, or Azure Trusted Signing). Script-ready: set `WINDOWS_SIGN_PFX`/`_PASSWORD`.
- [ ] Signed installer (Inno Setup `installer.iss` / MSIX) → SmartScreen reputation seasoning.
- [ ] Auto-update mechanism (e.g. WinSparkle / MSIX app installer feed).

_CI: `.github/workflows/build.yml` builds both OSes (unsigned) on every push after the offscreen smokes, and uploads the artifacts — the reproducible per-OS build env (ADR-104)._

### K3 — App Stores (phase 2, gated on K0)
- [ ] **Mac App Store:** sandbox entitlements, security-scoped bookmarks for user-chosen `.mfl` files, relocate `Snapshots/`/`Library/` to sandbox-legal container paths, App Store Connect listing, review.
- [ ] **Microsoft Store:** MSIX packaging, Partner Center account, store listing, certification.
- [ ] Reconcile in-app purchase/licensing with store rules (stores may *require* their IAP for unlocks — see C1).

---

## 3. Workstream F — Bank Feeds productionisation (Enable Banking first)

Cores already shipped (ADR-077): Enable Banking, SimpleFIN, Plaid, OFX Direct + the unified `BankFeedsDialog`. What's missing is **production enablement** and **robustness**.

### F1 — Enable Banking production prerequisites (mostly business/legal — see B & W)
- [ ] Register Production application in the EB Control Panel (needs the items below).
- [ ] App **description** shown to users at consent; **data-protection email**; **privacy policy URL**; **terms-of-service URL** (all from W/B workstreams).
- [ ] Decide activation route: **Restricted (link own accounts, internal test)** first → then request **Unrestricted (full)** which triggers EB's manual review + **KYC** + **contract** + **billing account**.
- [ ] **🕓 DECISION F1a — DEFERRED (decided 2026-06-16):** ship the existing **BYO** cores as-is for now (each user registers their own EB app + supplies the key; nothing sensitive in the binary). The BYO-vs-hosted call is **made after the B3 compliance read**, and since automated feeds are a 1.1 fast-follow it does not block 1.0. _If hosted is later chosen, it cascades into B (data-controller/KYC/PSD2 scope) and may reopen C1 (subscription) — keep that escape hatch in mind, don't architect against it._

### F2 — Feed robustness for real users (engineering)
- [ ] Consent-expiry / re-auth states (EB consents expire ~90 days) — detect, prompt, re-consent without losing the account link.
- [ ] Scheduled / background refresh (the "automatic downloads" the whole arc is named for) + a manual "Update all".
- [ ] Error surfaces: provider down, rate-limited, partial fetch, revoked consent — all need user-legible messaging.
- [ ] End-to-end test against ≥1 real bank per provider (browser consent round-trips are currently untested offscreen).
- [ ] Move provider credentials/keys out of the `.mfl` file into OS keychain (already a noted backlog item; becomes important once non-technical users hold real bank tokens).

### F3 — The other providers (post-1.0)
- [ ] SimpleFIN (~$15/yr US) and Plaid (US/CA) ship **enabled for all licensees** (C1 = everything included); the user still supplies their own provider key/cost under BYO. Same F2 robustness work applies.

---

## 4. Workstream M — My Retirement Life (MRL) integration & bundling

**Decisions locked 2026-06-16:** MFL and MRL are a **bundled / combined offering**; the technical integration is a **1.1 fast-follow**; its direction is **MFL → MRL, file-based RDF export** over the shared ontology. Today **only IRI compatibility exists** — accounts/person carry `mrl:`-namespaced IRIs (`repository._next_account_iri`); there is **no** RDF/SPARQL/Oxigraph code in MFL. The shared `docs/ontology/mrl-ontology.ttl` is a **contract** between the two apps (ADR-005: do not edit it from MFL). MRL already models the same entities MFL owns the live data for (`mrl:Account`/`CashAccount`/`InvestmentAccount`/`PensionAccount`/`CreditCardAccount`/`PropertyAsset`, `mrl:Person`, `mrl:IncomeSource`, `mrl:Currency`, `mrl:Jurisdiction`) plus retirement concepts MFL doesn't (`mrl:ProjectionSettings`, drawdown/surplus strategies, `mrlx:TaxTreatmentType`, `mrl:LifeEvent`) — so the clean split is **MFL = reality today → MRL = projects it forward through retirement + tax.**

### M1 — 1.0-gated items (small; the heavy lifting is 1.1)
- [x] **Preserve the IRI boundary** — **DONE (ADR-096, 2026-06-21).** `tests/test_iri_boundary.py` (Qt-free, runs on base `python3` or pytest) pins: account/person IRIs stay `mrl:`-namespaced (`mrl:<Class>_<n>`, class-scoped sequential), private entities stay `mfl:`, the `mrl:Person_1` + `mrl:CashAccount_1` seed is pinned at both seed sites, and an account round-trips via `get_account_by_iri`. Negative-checked: it fails on a simulated `mrl:`→`mfl:` regression.
- [ ] **Privacy policy (B2) discloses the planned MFL→MRL local data hand-off** — even though the export *code* ships in 1.1, the data-flow section must name it (and the bundle means the privacy story spans both apps).
- [ ] **Bundle marketing prep (W/C)** — the launch website tells the combined "financial life + retirement" story and commerce supports a bundle price. _This is 1.0 even though the technical exchange is 1.1._

### M2 — 1.1: MFL → MRL file-based RDF export (the core)
- [ ] New **Qt-free `mfl_desktop/export/mrl_rdf.py`** — map Repository data → RDF/Turtle over the shared `mrl:` ontology: accounts → `mrl:Account` subclasses (IRI already matches), person → `mrl:Person`, current balances/valuations, investment holdings, income sources → `mrl:IncomeSource`, net-worth snapshot, currencies → `mrl:Currency`, jurisdiction where known.
- [ ] **File ▸ Export for My Retirement Life…** verb → writes a `.ttl` the user loads into MRL; round-trip verified by loading into MRL's Oxigraph store.
- [ ] **CLI `export-mrl`** verb for headless / round-trip testing (matches the project's offscreen-testable pattern).
- [ ] **Payload decision:** start with a **point-in-time snapshot** (balances/values as of export date) + income sources — what MRL's projections need; defer history/time-series to later.
- [ ] **Ontology-as-contract:** pin the `mrl-ontology.ttl` version MFL exports against; a shared-ontology change is a coordinated change across both apps.
- [x] **ADR-081 drafted** (Proposed, 2026-06-16) — MFL→MRL mechanism + entity mapping + the authority contract + payload phasing. Accept when M2 implementation starts; one open sub-decision (stdlib Turtle writer vs shipping `rdflib`) settled during M2.

### M3 — 1.2+: deeper integration (future, optional)
- [ ] MRL → MFL reference reads (surface a retirement-projection summary, or tax/jurisdiction reference, *inside* MFL), and/or automatic/scheduled export, and/or bidirectional sync. Its own arc; out of near-term scope.

### Risks / dependencies
- **Bundle readiness is an external dependency:** marketing MFL+MRL as a bundle at 1.0 assumes MRL reaches a sellable state on a compatible timeline. **Fallback:** if MRL lags, the 1.0 website launches MFL standalone ("integrates with / MRL coming") and the bundle + cross-sell switch on when MRL ships. Track MRL's own launch readiness.
- The **non-sandboxed direct-distribution choice (K0)** keeps a future *local* MRL↔MFL handoff viable. The export being **file-based, user-initiated** means it survives even the 1.2 App-Store channel (a *live* MRL store read would not — another reason export beats live-read).

---

## 5. Workstream B — Business & legal

### B1 — Form the company ⚠ (owner asked)
- [ ] **⚠ DECISION B1a — entity & jurisdiction.** "LLC" is a US concept; the owner is UK-resident with UK+US accounts and is selling into UK/EU. Likely a **UK Ltd company** (for UK/EU operation, Enable Banking contract, and HMRC) — possibly plus a US LLC only if US-store tax structuring needs it. **This needs an accountant's input**, not just code. Get a UK Ltd + business bank account as the default path.
- [ ] Business bank account (needed for EB billing account, store payouts, MoR payouts).
- [ ] Register for tax as required (VAT threshold awareness; a Merchant-of-Record avoids most cross-border VAT pain — see C2).

### B2 — Legal documents ⚠ (owner asked: privacy policy)
- [ ] **Privacy Policy** conforming to **UK GDPR + EU GDPR** (and a clear statement that financial data is stored **locally on the user's device**, which is a strong privacy selling point). Must cover: what bank-feed data flows through which provider, data-protection contact, retention, user rights. _Strongly recommend a solicitor or a reputable template service review, given it's financial + a regulator-facing URL for Enable Banking._
- [ ] **Terms of Service / EULA** (license grant, disclaimers, "not financial advice," liability limits).
- [ ] **Data Processing / sub-processor disclosure** for the feed providers (EB, Plaid, SimpleFIN, Tiingo, openexchangerates).
- [ ] **Disclose the MFL→MRL data hand-off** (workstream M): the privacy policy must name the user-initiated export of financial data from MFL into My Retirement Life, even though the export code ships in 1.1. With the **bundle**, the privacy story spans both apps — decide one combined policy vs. two linked ones.
- [ ] Cookie/privacy notice for the website (W2).

### B3 — Compliance posture
- [ ] Confirm MFL's role under PSD2/Open Banking: under **BYO-credentials** MFL is likely *not* itself the regulated AISP (Enable Banking is the licensed entity / agent) — **but this must be confirmed**, especially if F1a chooses the hosted model. ⚠ Get a definitive read before turning on Production feeds.
- [ ] KYC pack for Enable Banking (company docs, ID) — assemble once B1 done.

---

## 6. Workstream W — Website & support

### W1 — Marketing / product site ⚠ (owner asked)
- [ ] Domain + hosting (static site is plenty: e.g. a static-site generator on Netlify/Cloudflare Pages/GitHub Pages + a custom domain).
- [ ] Pages: **Home/feature showcase** (screenshots, the multi-platform + local-first + multi-currency + investments story), **Download** (links to signed builds + auto-update feeds), **Pricing/Buy**, **Privacy**, **Terms**, **Support/Contact**, **About**.
- [ ] **Combined MFL + MRL "financial life + retirement" story** (bundle decision, workstream M): present the two apps as one offering — MFL tracks today, MRL projects forward — with a bundle buy path alongside standalone MFL. _Fallback if MRL isn't sellable at 1.0: launch MFL standalone with an "integrates with My Retirement Life (coming)" section, and add the bundle when MRL ships._
- [ ] Screenshots/video from the polished P4 build (do this *after* the UI polish, not before).

### W2 — Support & docs
- [ ] **Support contact** (a real monitored address — also serves as EB's data-protection email if appropriate) + a simple ticket/email flow.
- [ ] **Getting-started docs / FAQ** (import a statement, set up a bank feed, multi-currency, investments). The in-app Help (P5) links here.
- [ ] **Full user manual (web + PDF)** ⭐ (owner asked) — a complete manual covering **every** feature across the app (register, imports, transfers, schedules/bills, budgets, reports incl. Investment Income/Returns + Sankey, net worth, loans + amortization, bonds/options, multi-currency, bank feeds, data/backups), with **screenshots and clear explanations**, published on the website and exportable as a **PDF**. **Sequencing (owner): this is the LAST task** — write it only once the UI is in steady state (after the **P7 UI-freeze checkpoint**), otherwise screenshots and copy go stale immediately. Reuses the same polished build that feeds W1's marketing screenshots.
- [ ] Changelog / release notes page (feeds the auto-updater too).
- [ ] Status/known-issues page (optional but cheap).

---

## 7. Workstream C — Commerce (pricing, payments, listings)

### C1 — ✅ DECISION (LOCKED): one-time perpetual purchase, everything included
**Decided 2026-06-16 — Option A.** Single one-time price (~£25–45 / $30–50), **everything unlocked** (file import + OFX + Enable Banking + SimpleFIN + Plaid + all reports/investments). Major versions (2.0) are a new paid upgrade. Rationale: MFL is **local-first with BYO feed credentials → no ongoing server cost to amortise**, so a subscription is hard to justify and invites churn; "everything included" is the simplest thing to message and to license. _This supersedes the earlier "willing-to-pay unlocks SimpleFIN/Plaid" note — all providers are now in the box; the user still supplies their own provider key/cost under BYO._

Remaining sub-decisions (engineering/marketing, not blocking the model):
- [x] **Licensing mechanism — OFFLINE license key (DONE 2026-06-21, ADR-079 amendment).** Built: Qt-free verify-only `licensing.py` (Ed25519 over `cryptography`, already a dep), `license_service.py` orchestration, `version.py`, About box + Enter-License dialog + Help menu + launch nag, `QSettings`-persisted key/trial (app-level), `tools/license_tool.py` offline signer (private key gitignored) + `cli license-check`. Ships a dev public key to be swapped for production.
- [x] **Trial — DONE 2026-06-21.** 30-day full-feature trial, `QSettings` first-write-wins (can't be reset by reinstall); enforcement deliberately gentle (dismissible nag + title cue, not a hard lockout) per ADR-079. Tighten to a hard gate later via `LicenseStatus.unlocked` if wanted.
- [ ] Regional pricing tiers (the Merchant-of-Record can handle this — see C2).
- [ ] **Bundle SKU (workstream M):** an MFL + MRL combined price alongside standalone MFL (both one-time, everything-included); the MoR handles it as a second product/bundle. Define the discount vs. buying separately. Activates when MRL is sellable (else ship standalone-only and add later).
- [ ] _(1.1+ only)_ Reconcile with store IAP rules when K3 lands (stores may force their IAP for digital unlocks → 15–30% cut). Not a 1.0 concern since stores are deferred.

### C2 — Payments (direct sales)
- [ ] **⚠ Recommend a Merchant-of-Record** (Paddle / Lemon Squeezy / FastSpring) for the website store. They become the seller of record and **handle UK/EU VAT + US sales tax + invoicing** — which is the single biggest tax-compliance relief for a solo founder. Stripe alone leaves you holding all the cross-border tax obligations.
- [ ] License-key delivery + a simple "retrieve my key / re-download" flow.
- [ ] Refund policy (store-aligned + MoR-aligned).

### C3 — Store listings (gated on K3)
- [ ] App Store Connect + Microsoft Partner Center accounts (need the LLC + bank account).
- [ ] Listing copy, keywords, screenshots, category, age rating, privacy "nutrition labels" (Apple) / data declarations (MS).

---

## 8. Workstream Q — QA, beta & launch ops

- [ ] **Private beta** on both OSes (TestFlight is MAS-only; for direct builds, just a signed pre-release channel + a handful of testers).
- [ ] Test matrix: macOS (Intel + Apple Silicon) × Windows 10/11; fresh-install, upgrade-over-old-DB, large real DB (the owner's 26-account / ~35k-txn `.mfl`).
- [ ] Data-migration safety: opening an existing `.mfl` in the packaged app, snapshots/library in the new locations.
- [ ] Launch checklist: website live, buy flow tested end-to-end (incl. a real test purchase + refund), support inbox monitored, privacy/ToS URLs live (also needed for EB), version tags + release notes.
- [ ] Day-2 plan: how updates ship (auto-update feeds), how bugs get triaged.

---

## 9. Critical path & sequencing

**Phase 1 — Finish the product (engineering-led, no external blockers)**
P1 date/period standardisation → P2 report cross-linking → P3 de-overlap → P4 polish → P5 onboarding → P6 release hygiene.
_Output: a 1.0-quality build worth packaging and screenshotting._

**Phase 2 — Make it shippable (parallel with late Phase 1)**
B1 company + business bank → B2 legal docs (incl. **M1** MFL→MRL hand-off disclosure) → W1/W2 website + support (incl. **M1** combined MFL+MRL bundle story) → K0 channel decision → K1/K2 sign+notarise direct builds → C1 pricing + C2 payments (+ **M1** bundle SKU). **M1** also adds the cheap IRI-boundary guard test in Phase 1.
_Output: a legally-sellable, signed, downloadable, paid 1.0 with a website — marketed as the MFL+MRL bundle._

**→ 1.0 LAUNCH** (direct download, paid, file-import + OFX feeds; **no automated EB feeds and no MRL export code yet** — both are 1.1).

**Phase 3 — Turn on automated feeds + MRL export (1.1)**
F1 EB Production (KYC + contract using the now-live privacy/ToS URLs + B3 compliance read) → F2 feed robustness → ship EB auto-feeds. **In parallel: M2** MFL→MRL RDF export (`mrl_rdf.py` + Export verb + CLI + ADR-081), round-tripped into MRL's Oxigraph.

**Phase 4 — Channels & breadth (1.2+)**
K3 App Stores (sandbox work) → C3 store listings → F3 SimpleFIN/Plaid ship enabled → **M3** deeper MRL integration (reference reads / bidirectional).

**Why this order:** the app can *earn* before the slow legal/KYC machinery (B1→B2→F1) finishes; the website's privacy/ToS URLs are a prerequisite for EB Production *anyway*, so building them for launch double-counts; deferring the App Store sandbox rework keeps 1.0 small; and the MRL export is a self-contained file-based add that rides the same 1.1 window as feeds without blocking launch.

---

## 10. Definition of Done — the 1.0 gate

1.0 ships when ALL of:
- [x] **P1** one date-format + one period helper; no duplicated preset/label code. _(Part A ADR-082; Part B `make_date_edit`/`make_period_combo` adoption 2026-06-18.)_
- [x] **P2** every report drills to the shared transactions view; no dead-end charts. _(ADR-083, 2026-06-18 — Net Worth drills to the Account Summary page per owner fork; Budget keeps its precise bespoke register.)_
- [x] **P3** the four overlap candidates resolved (schedules entry points, filter-dialog base class, transfer-match unification, drill consistency). _(P3a ADR-084; P3b ADR-096; schedules + drill-consistency confirmed no-change.)_
- [x] **P4/P5** UI polished, dark-mode-complete, first-run + Help + About present. _(P5 ADR-098; P4 complete — dark-mode/button-audit ADR-097, brand re-tone ADR-100, app icon ADR-101, typography ADR-102.)_
- [ ] **K1/K2** signed + notarised macOS DMG and signed Windows installer that pass Gatekeeper/SmartScreen, with working auto-update.
- [ ] **B1/B2** company formed, Privacy Policy + ToS published at stable URLs.
- [ ] **W1/W2** website live with showcase, download, buy, privacy, terms, support.
- [ ] **C1/C2** pricing decided, a real purchase → license-key → install flow works end-to-end (via MoR).
- [~] **M1** IRI boundary guard test green _(DONE — ADR-096, `tests/test_iri_boundary.py`)_; privacy policy discloses the MFL→MRL hand-off (B2, pending); website tells the bundle story (W1, pending).
- [ ] **Q** test matrix green incl. the live `.mfl`; launch checklist complete.

Explicitly **out of the 1.0 gate** (both 1.1 fast-follows): automated Enable Banking feeds, and the **MFL→MRL RDF export code** (M2) — 1.0 only preserves the boundary, discloses it, and markets the bundle.

---

## 11. Decisions — status

| # | Decision | Outcome | Status |
|---|---|---|---|
| **K0** | Direct-notarised vs App Stores for 1.0 | **Direct first; stores 1.1+** | ✅ Locked 2026-06-16 |
| **C1** | Pricing & licensing model | **One-time perpetual, everything included; paid upgrades** | ✅ Locked 2026-06-16 |
| **F1a** | BYO vs MFL-hosted Enable-Banking registration | **Ship BYO; defer hosted-vs-BYO to after B3** | 🕓 Deferred (post-B3, pre-1.1) |
| **P1a** | One app-wide date display format | **ISO `yyyy-MM-dd`** (input fields) | ✅ Locked 2026-06-16 (reversed from `d MMM yyyy`) |
| **B1a** | Entity type/jurisdiction (UK Ltd vs US LLC vs both) | **UK Ltd** default; confirm with accountant | ⚠ Open (needs accountant) |
| **C1-lic** | License mechanism | **Offline signed key** (recommended) | ⚠ Open (engineering) |
| **M-time** | MRL integration timing | **1.1 fast-follow** (IRI boundary kept in 1.0) | ✅ Locked 2026-06-16 |
| **M-dir** | MRL integration direction | **MFL → MRL, file-based RDF export** | ✅ Locked 2026-06-16 |
| **M-comm** | MRL commercial relationship | **Bundle / combined offering** | ✅ Locked 2026-06-16 |
| **M-pay** | MRL export payload | **Point-in-time snapshot + income sources** (recommended) | ⚠ Open (settle in ADR-081) |

**Follow-on ADRs — all drafted 2026-06-16:**
- **ADR-078** (packaging & distribution — closes ADR-050 Tier-3, amends ADR-003) — ✅ **Accepted** (K0 locked).
- **ADR-079** (licensing & pricing) — ✅ **Accepted** (C1 locked; offline-key mechanism detail finalised in implementation).
- **ADR-080** (Enable Banking production / key custody) — ✅ drafted **Proposed**; custody decision deferred to after the B3 compliance read, accept-decision lands as an ADR-080 amendment.
- **ADR-081** (MFL→MRL data exchange) — ✅ drafted **Proposed**; accept at M2 start (open sub-decision: stdlib Turtle writer vs shipping `rdflib`).

---

## 12. Rough cost checklist (recurring/setup, for budgeting)

- Apple Developer Program **$99/yr**; Windows code-signing cert **~$200–400/yr** (or Azure Trusted Signing).
- Company formation + accountant (UK Ltd: modest setup + annual accounting).
- Domain + static hosting (**low**, ~£0–15/mo).
- Merchant-of-Record: **% of revenue** (no fixed cost — they take ~5%+ but absorb tax compliance).
- Enable Banking Production: **billing per their contract** (BYO-app users may bear their own provider cost depending on F1a).
- Legal review of Privacy/ToS (one-off, recommended).
- Optional: error-reporting service, email/support tooling.
