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

**Part B — `make_date_edit` adoption (REMAINING, next increment):**
- [ ] Date *format* is already uniform ISO (P1a done). Remaining: replace the ~14 ad-hoc `QDateEdit()` sites with `make_date_edit(...)` for consistency + to fix the no-popup `schedule_dialog`. Display-only (value read via `.date()`), no persistence impact.
- [ ] Adopt `make_period_combo` in the filter dialogs to retire the last `_PERIOD_LABELS` references.

### P2 — Make every report clickable & cross-linked ⭐ (owner explicitly asked)
**Problem (audited):** 5 surfaces drill, 4 are dead-ends. Drill-capable: Spending, Payee, Category & Payee, Account Summary (all → shared `TransactionsListWindow`), plus Home (navigation) and Budget (→ `BudgetDrillDownWindow`). **Dead-ends: Net Worth, Income & Expense, Sankey, Investment Returns.**

**Tasks (all reuse the existing `TransactionsListWindow` drill target):**
- [ ] **Sankey** — click an income/expense node → transactions for that category + period (`sankey_chart.py` needs a `node_clicked` signal + hit-test). _High value; Sankey is a headline report._
- [ ] **Income & Expense** — click a month bar → that bucket's transactions (`income_expense_chart.py` needs `segment_clicked`, mirror Spending).
- [ ] **Investment Returns** — click a security row → that security's buys/sells/dividends over the period.
- [ ] **Net Worth** — click an outer-ring account slice → that account's register (or per-account Summary). _Lower priority; confirm it doesn't muddy the "at a glance" purpose._
- [ ] **Budget drill-down → transactions** — add a "see transactions" link from `BudgetDrillDownWindow` to close the envelope→ledger gap.
- [ ] Audit that **every** drill opens the *same* target with consistent breadcrumb behaviour (one `TransactionsListWindow`, not bespoke variants).

### P3 — Remove overlapping functionality ⭐ (owner explicitly asked)
**Candidates (audited):**
- [ ] **Schedules has 4 entry points** (Manage ▸ Schedules, register filter-bar button, Home "Bills due" card, right-click "Create Schedule From Transaction"). Keep the filter-bar button + the right-click seed path; demote or drop the redundant menu item. Decide the canonical path.
- [ ] **Six report filter dialogs are ~40% copy-paste.** Extract a `ReportFilterDialogBase` (period + custom range + account/category/payee checklists); each report adds only its specials (granularity, rollup, top-N, transfers toggle). ~600 LOC removed, UX unified. _Also the natural home for the P1 shared period widget._
- [ ] **Two transfer-matching UIs** (inline ADR-036 confirm/picker vs Manage ▸ Reconcile Transfers ADR-037) — unify the strength-chip/candidate-table presentation so they look identical even if entry points differ.
- [ ] **`TransactionsListWindow` is only reachable via drill** — confirm that's intentional (it is a good detail view); no menu entry needed, but make sure the breadcrumb "back" is consistent.

### P4 — Visual & interaction polish for a paying audience
- [ ] Consistent dialog sizing, button order (platform-native: macOS vs Windows), default-button + Esc behaviour across all dialogs.
- [ ] Empty-state / first-run polish (the Home dashboard cards already self-hide; verify a brand-new seeded DB looks intentional, not empty/broken).
- [ ] Dark-mode pass over any surface added since ADR-076 (the new commission/total-cost investment fields, Bank Feeds dialog).
- [ ] Spacing/typography scale (the deferred Arc B round) — at least a consistent scale, not pixel-perfect.
- [ ] Iconography + app icon (needed anyway for stores).

### P5 — First-run onboarding & help
- [ ] A short first-run flow: create/seed file, pick base currency, optional "import your first statement" nudge.
- [ ] In-app **Help / Getting Started** (links to the website docs) + an **About** box (version, license, links, attributions).
- [ ] Crash-safety review: confirm the ADR-057 snapshot/checkpoint story holds under the packaged (sandboxed?) file locations — see K-workstream sandbox note.

### P6 — Release engineering hygiene
- [ ] Pin dependencies; produce a reproducible build env per OS.
- [ ] Version string + build metadata surfaced in About and in crash reports.
- [ ] Basic automated test pass green on both OSes before each packaged build (the offscreen Qt smoke pattern already exists).
- [ ] Decide a crash/error-reporting approach (local log + "export diagnostics" button is the privacy-friendly minimum; Sentry-style is heavier and has its own privacy implications).

---

## 2. Workstream K — Packaging, signing & store readiness

### ✅ DECISION K0 (LOCKED) — Direct signed+notarised downloads for 1.0; App Stores deferred to 1.1+
There are **two different things** the owner referred to as "notarised … so it's seen as legit and safe," and they are NOT the same effort:

| Path | Trust signal | Effort | Tax/payments | Sandbox? |
|---|---|---|---|---|
| **Direct download, signed + notarised** (macOS: Developer ID + notarised DMG; Windows: signed installer / signed MSIX) | Passes Gatekeeper / SmartScreen — *already* "legit and safe", no store needed | **Low–medium** | You handle (use a Merchant-of-Record — see C2) | **No** (full file access) |
| **Mac App Store + Microsoft Store** | Store badge + auto-update + store handles payment/tax | **High** (sandboxing, review, store accounts, entitlements) | Store handles (15–30% cut) | **Yes on MAS** — conflicts with our file model |

**The sandbox tension is real and specific to our app:** MFL opens *arbitrary* `.mfl` files anywhere, writes `Snapshots/` and `Library/` folders *beside* the live file, and has the ADR-050 cwd bridge. The Mac App Store sandbox forbids that without **security-scoped bookmarks** and rework of the snapshot/library location model. Direct-notarised has none of those constraints.

**Decision (locked 2026-06-16):** **Ship 1.0 as direct signed+notarised downloads** (fast, full trust, no sandbox rework); **App Stores are a 1.1/1.2 channel** once revenue justifies the sandbox work. This is the single biggest scope lever in the whole plan, and it removes the K3 sandbox work from the 1.0 critical path entirely.

### K1 — macOS direct (Developer ID)
- [ ] Apple Developer Program enrolment (org, once the LLC exists — see B1). **$99/yr.**
- [ ] PyInstaller (or briefcase) build → `.app` → Developer ID signing → **notarisation** → stapled DMG (closes ADR-050 Tier-3).
- [ ] Hardened runtime + entitlements; verify FX/price network calls and file access work notarised.
- [ ] Sparkle (or equivalent) auto-update feed hosted on the website.

### K2 — Windows direct
- [ ] **Code-signing certificate** — ⚠ note: OV certs now effectively require hardware tokens/cloud HSM; EV smooths SmartScreen. Budget for this (~$200–400/yr via a CA, or use Azure Trusted Signing if eligible).
- [ ] PyInstaller build → signed installer (Inno Setup / MSIX) → SmartScreen reputation seasoning.
- [ ] Auto-update mechanism (e.g. WinSparkle / MSIX app installer feed).

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
- [ ] **Preserve the IRI boundary** — no regressions to the `mrl:` account/person namespace (it is the join key MRL matches on). Add a guard/test so a future refactor can't silently change the prefix.
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
- [ ] Changelog / release notes page (feeds the auto-updater too).
- [ ] Status/known-issues page (optional but cheap).

---

## 7. Workstream C — Commerce (pricing, payments, listings)

### C1 — ✅ DECISION (LOCKED): one-time perpetual purchase, everything included
**Decided 2026-06-16 — Option A.** Single one-time price (~£25–45 / $30–50), **everything unlocked** (file import + OFX + Enable Banking + SimpleFIN + Plaid + all reports/investments). Major versions (2.0) are a new paid upgrade. Rationale: MFL is **local-first with BYO feed credentials → no ongoing server cost to amortise**, so a subscription is hard to justify and invites churn; "everything included" is the simplest thing to message and to license. _This supersedes the earlier "willing-to-pay unlocks SimpleFIN/Plaid" note — all providers are now in the box; the user still supplies their own provider key/cost under BYO._

Remaining sub-decisions (engineering/marketing, not blocking the model):
- [ ] **Licensing mechanism — recommend an OFFLINE license key** (signed key the app validates locally; no account server). Fits local-first + privacy and needs no backend. Build a small `licensing.py` + an "Enter license" / "Buy" flow + About-box license state.
- [ ] **Trial:** recommend a time-limited full-feature trial (converts well for utilities) → unlocked by a purchased key.
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
- [ ] **P1** one date-format + one period helper; no duplicated preset/label code.
- [ ] **P2** every report drills to the shared transactions view; no dead-end charts.
- [ ] **P3** the four overlap candidates resolved (schedules entry points, filter-dialog base class, transfer-match unification, drill consistency).
- [ ] **P4/P5** UI polished, dark-mode-complete, first-run + Help + About present.
- [ ] **K1/K2** signed + notarised macOS DMG and signed Windows installer that pass Gatekeeper/SmartScreen, with working auto-update.
- [ ] **B1/B2** company formed, Privacy Policy + ToS published at stable URLs.
- [ ] **W1/W2** website live with showcase, download, buy, privacy, terms, support.
- [ ] **C1/C2** pricing decided, a real purchase → license-key → install flow works end-to-end (via MoR).
- [ ] **M1** IRI boundary guard test green; privacy policy discloses the MFL→MRL hand-off; website tells the bundle story.
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
