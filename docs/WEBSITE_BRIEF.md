# My Financial Life — Website Content Brief

**Purpose:** a self-contained brief for building the marketing + docs website (workstream W1/W2), to be handed to a separate website project. Everything the site needs is here — you do **not** need the app's source or its ADRs. Distilled from the 1.0 launch backlog and the product decisions as of 2026-06-22.

**Status of the product:** the desktop app (macOS + Windows) is feature-complete and brand-finished; it builds to signed-ready installers. The website is the remaining launch blocker (its Privacy + Terms URLs are *also* a hard prerequisite for turning on automated bank feeds later — see §8).

---

## 1. What the product is (positioning)

**My Financial Life (MFL)** is a **local-first desktop personal-finance app** for macOS and Windows. It tracks your whole financial picture — bank/credit/cash accounts, investments (stocks, funds, bonds, options), budgets, and net worth — with all data stored **on your own device**, never on a server.

**One-paragraph pitch:**
> See your whole financial life in one place — accounts, investments, and budgets — without handing your data to anyone. My Financial Life runs on your Mac or PC, stores everything locally, imports your statements, tracks every holding and currency, and shows you where your money goes. Buy it once; it's yours.

**Primary tagline (used in-app, keep consistent):**
> Your whole financial life — accounts, investments and budgets — private and on your own device.

**Sister product:** **My Retirement Life (MRL)** projects your finances *forward* through retirement and tax. MFL = your money today; MRL = your money's future. They're sold as a bundle (see §9).

---

## 2. Audience

- Individuals and households who want a **serious, private** finance tool and dislike cloud services that monetise their data.
- People moving off discontinued/declining desktop finance apps (Banktivity, Quicken, Microsoft Money émigrés) — the app deliberately speaks their vocabulary.
- Multi-currency / cross-border people (e.g. UK + US accounts) and DIY investors who want real portfolio tracking, not just spending categories.
- Technically comfortable but **not** developers — the buyer is a careful, privacy-minded saver/investor.

Tone: trustworthy, calm, precise, a little premium. Not breathless startup hype. "Quietly powerful." Privacy is a *feature*, stated plainly, not fear-mongered.

---

## 3. Hard constraints — required routes

The shipping app **hardcodes these URLs**; the site MUST serve them (don't rename):

| Route | Purpose | Linked from |
|---|---|---|
| `https://myfinancial.life` | Home / marketing | Help ▸ Visit Website |
| `https://myfinancial.life/buy` | Purchase / pricing | Help ▸ Buy, About box, License dialog |
| `https://myfinancial.life/docs/getting-started` | Getting-started docs | Help ▸ Getting Started |

Also expected by the launch checklist (and by Enable Banking, §8): `/privacy`, `/terms`, `/download`, `/support`. Keep URLs stable once published.

---

## 4. Site map & per-page content

Static site is plenty. Suggested pages:

1. **Home (`/`)** — hero (tagline + screenshot + Download/Buy CTAs), the local-first/privacy story, a feature showcase (§6 grouped), the multi-platform + multi-currency + investments highlights, a short "MFL + MRL" bundle teaser, footer with all links.
2. **Features (`/features`)** *(optional; can fold into Home)* — the full grouped feature list with screenshots.
3. **Download (`/download`)** — signed macOS `.dmg` + Windows installer links, system requirements (macOS 11+, Windows 10/11), "what's new"/changelog link. Until builds are signed, this can say "coming at launch."
4. **Pricing / Buy (`/buy`)** — the one-time price, what's included (everything), the free trial, the bundle option, the Buy button (Merchant-of-Record checkout — see §7). Refund policy link.
5. **Privacy (`/privacy`)** — see §8 (load-bearing; needs legal review).
6. **Terms (`/terms`)** — EULA / ToS (see §8).
7. **Support (`/support`)** — a monitored contact address + FAQ + link to docs. (This address can double as Enable Banking's data-protection contact.)
8. **Docs / Getting Started (`/docs/...`)** — onboarding guide: install, create your file, pick base currency, import your first statement, set up a budget, add investments, multi-currency. Markdown-driven. The in-app "Getting Started" link points at `/docs/getting-started`.
9. **About (`/about`)** *(optional)* — the story, the privacy ethos, who's behind it.

---

## 5. The roadmap framing — don't overpromise

The app **ships and sells now** with **manual data in**: **file import (OFX / QFX / QIF / CSV)** and **OFX Direct Connect** both work today. **Automated bank feeds** (Enable Banking, SimpleFIN, Plaid) and the **MFL→MRL export** are a **1.1 fast-follow**, not in the 1.0 box.

So the site must be careful:
- ✅ Say: "Import statements (OFX/QFX/QIF/CSV) and connect via OFX Direct." 
- 🟡 For automated feeds, say **"coming"** / "automated bank feeds (UK/EU + US) are on the way" — do **not** imply they're live at launch.
- 🟡 For the MRL bundle: if MRL isn't sellable at launch, present MFL standalone with "integrates with My Retirement Life (coming)" and switch on the bundle when MRL ships.

App Store / Microsoft Store versions are a later channel (1.1+); 1.0 is **direct download only**.

---

## 6. Messaging kit — value props & features

**Three pillars (lead with these):**
1. **Private by design.** Your data lives on your device, full stop. No account to create, no cloud sync, no data harvesting. (When bank feeds arrive, *you* hold the provider keys — credentials never route through us.)
2. **Your whole financial life.** Accounts, investments, budgets, and net worth in one app — not just spending categories.
3. **Buy it once.** One-time purchase, everything included, no subscription. Major upgrades are optional and paid; your version keeps working forever.

**Feature list (grouped — use for the showcase):**

- **Accounts & ledger** — current/savings/credit/cash + investment/property/vehicle/loan accounts; a fast multi-account register with an all-transactions view; transfers; reconciliation against statements; payee management with aliases.
- **Import & feeds** — OFX / QFX / QIF / CSV import with a smart mapping wizard for unknown formats; OFX Direct Connect; count-aware de-duplication so re-imports don't double up. *(Automated bank feeds: coming.)*
- **Investments** — real holdings tracking with cost basis, unrealised/realised gains, dividends (incl. DRIP), and returns (incl. IRR); stocks, funds, **bonds, and options**; automatic price history (via your own data provider key); per-security performance.
- **Budgets** — a genuine envelope + zero-sum hybrid: a 12-month editable matrix, per-month allocations, rollover, bills tied to scheduled transactions, a stepped burn-down chart, and savings/pay-down goals that can span multiple accounts.
- **Reports** — Spending over time, Income over time, Income & Expense, Net Worth, Category & Payee, Sankey cash-flow, Investment Returns — all click-through to the underlying transactions.
- **Multi-currency** — true multi-currency accounts and holdings, daily FX rates, manual rates, cross-currency transfers that record the real rate.
- **Loans** — amortising loan accounts with a payment schedule and pay-down goals.
- **Safety** — continuous auto-save, automatic timestamped snapshots (grandfather-father-son retention), a one-click data library for fixtures/backups, light + dark themes.

**Micro-copy / proof points:**
- "No account. No cloud. No catch."
- "Everything's saved automatically — and snapshotted, so you can always roll back."
- "Stocks, funds, bonds, options — with real cost basis and returns, not guesses."
- "One price. Every feature. Yours to keep."

---

## 7. Pricing & licensing (exact model — keep precise)

- **One-time perpetual purchase**, target **~£25–45 / $30–50** (owner sets the final number). **Everything is included** — all reports, investments, multi-currency, and (when live) all feed providers. No subscription, no feature tiers, no add-ons.
- **Major versions (2.0) are a separate paid upgrade**; a 1.x licence keeps working on every 1.x release forever.
- **Free trial:** a **30-day, full-feature** trial — the whole app is unlocked during it; buying a key removes the gentle expiry nag. (The trial is enforced in-app; the site just needs to say "try free for 30 days.")
- **Licence delivery:** an offline signed licence key emailed after purchase; the buyer pastes it into the app (Help ▸ Enter License). The site's Buy flow must deliver the key and offer a "retrieve my key / re-download" path.
- **Payments:** use a **Merchant-of-Record** (Paddle / Lemon Squeezy / FastSpring) so VAT/sales-tax is handled for you — the Buy button is their checkout, and they email the key. (Stripe alone would leave you holding cross-border tax — avoid for the storefront.)
- **Bundle SKU:** an MFL + MRL combined price alongside standalone MFL (both one-time, everything-included). Define the bundle discount when MRL is sellable.

---

## 8. Privacy & legal (load-bearing — needs a solicitor/template review)

These pages are not optional polish: **stable Privacy + Terms URLs are a prerequisite for activating automated bank feeds** (Enable Banking Production requires a privacy-policy URL, a terms URL, and a data-protection email). Treat them as launch-critical.

**Privacy Policy must cover:**
- The headline: **financial data is stored locally on the user's device** — a genuine privacy selling point, stated plainly.
- What the website itself collects (analytics/cookies, if any — keep minimal).
- The bank-feed data flow: when automated feeds are on, which provider (Enable Banking / Plaid / SimpleFIN) the data passes through, under a **bring-your-own-credentials** model (the user holds the keys; MFL is not the data controller for that flow — but get this confirmed in the legal review).
- Third-party services the app can talk to with the user's keys: the bank-feed provider, **openexchangerates** (FX), **Tiingo** (prices).
- A **data-protection contact email** (can be the support address).
- The **MFL→MRL hand-off:** the user-initiated export of their financial data from MFL into My Retirement Life (the export code ships in 1.1, but the *policy* must name the data flow now). With the bundle, decide one combined policy vs. two linked ones.
- UK GDPR + EU GDPR alignment; retention; user rights.

**Terms / EULA must cover:** licence grant (one-time, perpetual, per the model above), "not financial advice," disclaimers, liability limits, refund policy.

**Jurisdiction:** the working assumption is a **UK Ltd** company (owner is UK-resident, selling UK/EU) — confirm with an accountant; this affects the legal entity named in the docs.

> ⚠️ Use real legal review (solicitor or a reputable template service) for Privacy + Terms — it's financial software and these are regulator-facing URLs. The website author should leave clearly-marked placeholders, not invent legal text.

---

## 9. The MFL + MRL bundle story

- **My Retirement Life (MRL)** is the sister app: it takes your current financial reality and **projects it forward through retirement, drawdown, and tax**. The clean split for messaging: **MFL = your money today → MRL = your money's future.**
- They're marketed as a **combined offering / bundle** (one-time, everything-included, with a bundle discount vs. buying separately).
- The technical integration (a user-initiated, file-based export from MFL into MRL) is a **1.1 fast-follow** — so at launch the site tells the *combined story* and offers the bundle, but should not claim live two-way sync.
- **Fallback:** if MRL isn't sellable at launch, ship the MFL site standalone with an "integrates with My Retirement Life (coming)" section, and switch the bundle on when MRL ships.

---

## 10. Brand kit

**Logo / icon:** a hexagonal badge — a gold isometric "M" with a coin and an up-arrow on a deep petrol-teal field. The icon files are in the app repo at **`assets/icons/`** (`mfl_icon_1024.png` master + 16–512 sizes, plus `mfl.icns` / `mfl.ico`). **Copy these into the website project** for the logo/favicon. (MRL's badge is the matching one with a gold "R", a sunset, and a sailboat — for the bundle section.)

**Colour palette** (the app's actual tokens — match these so the site and app feel like one product):

| Role | Light | Dark | Notes |
|---|---|---|---|
| Brand accent (teal) | `#1f6e78` | `#39a0aa` | primary buttons, links, highlights |
| Accent hover | `#185860` | `#2f8893` | |
| Brand gold | `#c9a23a` | `#dcbb55` | sparingly — accents/rules, **not** body text on white (low contrast) |
| Canvas / background | `#f8fafc` | `#0f172a` | |
| Surface / cards | `#ffffff` | `#1e293b` | |
| Text | `#0f172a` | `#f1f5f9` | |
| Heading | `#334155` | `#e2e8f0` | |
| Muted text | `#64748b` | `#94a3b8` | |
| Border | `#e2e8f0` | `#334155` | |
| Positive (gains) | `#16a34a` | `#22c55e` | green = money up |
| Negative (loss) | `#dc2626` | `#f87171` | red = money down |

The sunset orange (~`#e08a3c`) belongs to **MRL**, not MFL — only use it in the bundle/MRL context.

**Typography feel:** clean, modern sans-serif (the app uses the system UI font — Inter, system-ui, or similar is a good web match). Calm, generous spacing. Big legible numbers when showing figures. Avoid playful/rounded display fonts; this is a serious finance product.

**Voice:** plain, confident, specific. Short sentences. Lead with the benefit, back it with the concrete feature. Privacy stated as fact, not as a scare.

---

## 11. Screenshots to capture (from the polished app)

The owner can supply these from the brand-finished build (light theme, the public demo dataset "Jordan Avery"). Suggested shots:
- **Home dashboard** (net worth + accounts + budget + recent + investment performance) — the hero shot.
- **Budget** — the 12-month matrix and/or the monthly envelope view with the burn-down.
- **A report** — Spending over time or the Sankey cash-flow (visually striking).
- **Investments** — holdings with gains + the returns chart.
- **Account register** — the multi-account ledger.
- A **dark-mode** shot for variety.
Plus the **app icon** at large size for the hero/footer.

> The website author should treat screenshots as owner-supplied assets; don't fabricate UI mockups that differ from the real app.

---

## 12. Tech & hosting recommendation

- **Static site** — no backend needed. A generator like **Astro** (great for marketing + markdown docs), **Eleventy**, or **Hugo**; plain HTML is fine for something this small. Markdown for the `/docs` pages.
- **Host:** Netlify, Cloudflare Pages, or GitHub Pages, with the **`myfinancial.life`** custom domain and auto-deploy on push.
- **Commerce:** embed the Merchant-of-Record checkout (Paddle/Lemon Squeezy/FastSpring) on `/buy` — usually a hosted checkout link or a small JS overlay; no server code.
- **Analytics:** if any, prefer privacy-friendly (Plausible/Fathom/none) to stay consistent with the product's ethos — and disclose it in `/privacy`.
- **Separate repo** from the app (different stack, independent deploy/cadence, the site can be public while the app repo stays private).

---

## 13. SEO / meta basics

- Title/description per page; OpenGraph + Twitter cards using the app icon + a hero screenshot.
- Keywords lean: "private personal finance app," "local-first budgeting," "Mac and Windows finance app," "investment + budget tracker," "Quicken/Banktivity alternative."
- Favicon from `assets/icons/`. Sitemap + robots.txt.

---

## 14. What the owner still must supply (not the website author's call)

- The **final price** and the **bundle discount**.
- The **domain** (`myfinancial.life`) DNS pointed at the host.
- The **Merchant-of-Record account** + the checkout link/keys for `/buy`.
- **Legal-reviewed** Privacy + Terms text (the site ships placeholders until then).
- The **support email** address.
- **Screenshots** from the polished build (or ask — they can be generated).
- The **company entity** details (UK Ltd, once formed) named in the legal docs.
- Whether MRL is **sellable at launch** (drives bundle-vs-standalone framing).

---

*End of brief. Anything the website author needs beyond this — exact feature wording, more screenshots, a copy pass — can come from the app repo's `CLAUDE_CONTEXT.md` and `docs/RELEASE_1.0_BACKLOG.md`, but this document is intended to be sufficient on its own.*
