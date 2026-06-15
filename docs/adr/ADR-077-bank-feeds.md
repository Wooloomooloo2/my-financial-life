# ADR-077 — Bank feeds (Arc H): pluggable providers, GoCardless first

**Date:** 2026-06-15
**Status:** Accepted (framework) — **provider pivot, see Amendment 2026-06-15.** GoCardless is no longer the first provider; the pluggable framework + import reuse stand. Round 1 GoCardless code is kept (works for existing GoCardless customers) but is not the path forward.
**Related:** ADR-021 (CSV import), the OFX/QFX import engine, ADR-035 (`setting` table for API keys), ADR-050 (cross-platform, local-first), ADR-016/057 (the `.mfl` is the dataset). Owner-chosen direction.

---

## Context

The last of the eight arcs, and the one the owner calls make-or-break: pulling transactions automatically instead of exporting + importing files by hand.

There is **no free, universal, private bank-feed API**, and the owner's accounts span **UK** (HSBC, Smile, YBS, Capital One UK…) *and* **US** (Ally, Chase, Discover, eTrade, Morgan Stanley…), which no single free mechanism covers. The landscape:

- **OFX Direct Connect** — free, fully local; reuses the OFX engine; covers many US banks, few UK. Per-bank setup.
- **GoCardless "Bank Account Data"** (Open Banking) — **free** tier; UK + EU; OAuth-style bank consent; the user supplies their own free key; consents expire (≈90 days). **No US.**
- **SimpleFIN** — ~$15/yr (user-paid), US-strong, weak UK.
- **Plaid / TrueLayer** — broadest UK+US, best UX, but paid + approval, data via aggregator. The per-connection cost can't be borne centrally for a freely-shared app.

Two constraints fall out:
1. **A freely-shared app can't bear central per-user cost** → whatever the provider, **each user supplies their own credentials/token** → a **pluggable provider framework**, not a hard-wired one.
2. **It must reuse the existing import pipeline** — a feed only needs to produce transactions; staging, FITID/hash dedup, the manual-match heuristic, the review step, and commit are all already built (`ImportService._classify_and_stage` + `commit_import`).

Owner decisions (`AskUserQuestion`): **GoCardless Open Banking first** (free, covers their UK banks); refresh is a **manual "Update accounts" action** to start (downloaded transactions land in the existing review/dedup before commit), with background/scheduled refresh deferred.

---

## Decision

### A pluggable feed-provider framework (`mfl_desktop/feeds/`)

A provider is a small, **Qt-free** object exposing: `list_institutions(country)`, `start_link(institution_id, redirect)` → a hosted consent `link` + an opaque `requisition` id, `link_status(requisition_id)` → pending/linked + the provider account ids, and `fetch_transactions(external_account_id)` → raw provider rows. New providers (OFX Direct Connect for the US, SimpleFIN) implement the same surface later without touching the UI or the import pipeline.

### GoCardless provider (round 1)

`mfl_desktop/feeds/gocardless.py` — a stdlib-`urllib` client (no Qt, no new dependency), base `https://bankaccountdata.gocardless.com/api/v2/`:

- **Token**: `POST /token/new/` `{secret_id, secret_key}` → `access` (24 h) + `refresh` (30 d). Fetched per session; cached in memory.
- **Institutions**: `GET /institutions/?country=GB`.
- **Consent**: `POST /requisitions/` `{redirect, institution_id, reference}` → `{id, link}`. The desktop has no web server, so the flow is: open `link` in the system browser; the user authenticates with their bank; the app **polls** `GET /requisitions/{id}/` until `status == "LN"` (linked) and reads its `accounts`. The `redirect` is a harmless localhost URL — completion is detected by polling, not by catching the redirect (no firewall-prompting local server needed).
- **Data**: `GET /accounts/{id}/transactions/` (`booked` + `pending`), `/balances/`, `/details/`.

### Credentials & links live in the `.mfl` (each user their own)

The GoCardless `secret_id` / `secret_key` persist in the `setting` table (ADR-035), with the same "stored inside this file" disclaimer the OXR/Tiingo keys carry. **Migration 0026** adds a `feed_account` table mapping an MFL account to a provider account:

```
feed_account(id, account_id→account UNIQUE, provider, external_account_id,
             requisition_id, institution_id, institution_name,
             status, last_synced_at, created_at;  UNIQUE(provider, external_account_id))
```

One feed per MFL account in v1; the schema allows many providers.

### Import reuse — feeds are just another source

`gocardless`/`normalize.py` maps a provider transaction to the **exact raw-txn dict** the parsers emit: `date` (bookingDate), `amount` (abs of `transactionAmount.amount`), `tx_type` (`debit`/`credit` from the sign), `payee_raw` (creditor/debtor name or remittance info), `memo` (remittance info), and `fitid = transactionId`. New `ImportService.stage_feed(account_iri, raw_txns)` runs these through `_classify_and_stage` → the **same** dedup (`fitid`→`import_hash`, so re-fetching the overlapping window is idempotent), manual-match heuristic, review, and `commit_import`. Zero new import/dedup logic.

### Refresh model (round 1: manual)

A **Manage ▸ Bank Feeds…** screen connects a bank and links its accounts; an **Update accounts** action fetches each linked account's recent transactions, stages them, and drops the user into the existing import-review dialog before committing. GoCardless's free tier rate-limits per-account data calls (a few/day), which manual refresh respects naturally; background/scheduled refresh is a later round.

---

## Consequences

- A genuine, free bank feed for the owner's UK accounts, with the privacy/local ethos intact (their own key, data in their own `.mfl`).
- The import pipeline, dedup, and review are untouched — a feed can't produce a duplicate the file path wouldn't, and the user still reviews before committing.
- The provider framework means US coverage (OFX Direct Connect, SimpleFIN) is an additive round, not a rewrite.

### Security / privacy

- Credentials sit in the `.mfl` (per ADR-035) — fine for a single-user local file; an OS-keychain option is the existing deferred follow-up. The disclaimer is shown at entry.
- Consents are read-only (GoCardless Bank Account Data is data-only — no payment scope).
- Tokens are kept in memory only; only the long-lived `secret_*` and the requisition/account ids persist.

### Phasing

- **H1 (this round):** provider framework + GoCardless client + `feed_account` schema + normalize + `stage_feed` + offscreen tests. (Connectivity is verifiable headlessly with a real secret before any UI is built — proving the make-or-break pipe works.)
- **H2:** the Manage ▸ Bank Feeds… UI — connect a bank (browser consent + poll), link provider accounts ↔ MFL accounts, and **Update accounts** → review → commit; show balances + last-synced.
- **H3:** re-consent handling (≈90-day expiry), richer error/empty states, scheduled/background refresh, and a second provider (OFX Direct Connect / SimpleFIN) for the US accounts.

### Rejected (for now)

- **Plaid/TrueLayer** — cost + central billing incompatible with a freely-shared app.
- **Catching the OAuth redirect with a bundled local web server** — polling the requisition status is simpler and avoids firewall prompts.
- **Auto-committing fetched transactions** — they go through the same human review as file imports; silent posting of a bad fetch is exactly what the review step prevents.
- **Storing the access/refresh tokens on disk** — short-lived; re-minted from the persisted secret each session.

---

## Amendment (2026-06-15) — GoCardless onboarding closed; pivot to frictionless import + OFX Direct Connect

The headless `feeds-check` probe (built precisely to de-risk this before any UI) surfaced the make-or-break fact immediately: **GoCardless has disabled new Bank Account Data signups and is no longer onboarding customers** (existing customers keep access). The wider 2026 reality, confirmed by research: **there is no free Open-Banking data API available to a UK individual** — Plaid / TrueLayer / Tink cover the UK but only via business onboarding + custom-negotiated pricing, untenable for a freely-shared app; the modern self-hosted apps (Actual Budget) offer only SimpleFIN (US/Canada) or GoCardless (Europe, now closed). So the **UK side cannot have a free auto-feed**; the **US side still can**.

The pluggable framework, `feed_account` schema, `stage_feed`, dedup reuse, and `normalize` pattern are all unaffected — only the *provider* changes. Owner decision (`AskUserQuestion`) on the new direction: **two free tracks**:

1. **Frictionless file import (all banks, incl. UK)** — the realistic win for the UK accounts. Make the export→import loop near-instant: remember each account's import source (folder/format), a one-click "import my latest download" that auto-picks the newest recognised file and routes it straight to the existing review/dedup, and **saved CSV mapping profiles** (the ADR-021 follow-up) so repeat generic-CSV imports skip the column wizard. Free, works for every institution.
   - **Slice 1 shipped (2026-06-15):** per-account **import-folder memory** (the folder you last imported from for an account is remembered in `setting` as `import_dir:{id}` and the Import… picker reopens there) + **Import Latest** (File ▸ Import Latest / Ctrl+Shift+I): grabs the newest `.ofx/.qfx/.qif/.csv` from that folder (Downloads fallback), confirms the filename, and runs it through the existing parse → (map) → dedup → commit.
   - **Slice 2 shipped (2026-06-15): saved CSV mapping profiles.** Migration 0027 `csv_import_mapping` keyed by a normalised **header signature** (`csv_parser.header_signature`). The first generic-CSV import maps columns in the wizard; `apply_mapping_and_stage` saves the `CsvColumnMapping` JSON under the signature. The next import of the same export layout looks it up in `parse_and_stage`, auto-applies it, and returns `"preview"` (the wizard is skipped) — a renamed-column export gets a new signature and re-maps; a stale/incompatible saved mapping falls back to the wizard. Account-agnostic (one profile serves every account importing that bank's format). UI unchanged — driven entirely by the `"preview"` vs `"map"` step the service returns.
2. **OFX Direct Connect (free US auto-feed)** — a real feed provider on the ADR-077 framework: HTTP-POST OFX requests to a bank's OFX server with the user's own credentials (URL/ORG/FID/user/pass), parsing the response with the existing OFX engine. No third party, no cost; covers US banks that still support it (coverage has thinned — verify per-bank at ofxhome.com).
   - **Round 2 shipped (2026-06-15): the config UI.** **Manage ▸ Bank Feeds…** (`ui/ofx_feeds_dialog.py`): `OfxConnectionDialog` adds/edits one connection — bank server (URL/ORG/FID from ofxhome.com), online-banking credentials (password-masked, with the "stored inside this .mfl" disclaimer), the account at the bank (number + type; routing id required for bank types, broker id for investment), and an Advanced group defaulting to the Quicken identity (QWIN/2700/102). A **Test connection** button runs a no-commit fetch and reports the count (or the FI's error). `OfxFeedsDialog` lists the linked feeds (account, institution, last-updated, status) with Add/Edit/Remove and **Update selected / Update all** — which fetch → `stage_feed` → the existing dedup/match → `commit_import`, stamp `last_synced_at`, and report a per-account summary; the register + sidebar refresh if anything imported. Persistence (`feeds/ofx_store.py`, Qt-free): the connection config is a JSON blob in `setting` under `ofx_config:{account_id}` (a stable `client_uid` is generated once and kept), and the `feed_account` row (`provider='ofx_direct'`) is the link marker. Next round: re-fetch error/empty states polish and scheduled/background refresh.
   - **Round 1 shipped (2026-06-15): the provider core + headless probe.** `feeds/ofx_direct.py`: `OfxServer` (url/org/fid + app id/version/OFX version + optional stable `client_uid`), `OfxAccountSpec` (acct id/type + routing/broker id), and `OfxDirectClient`. The OFX protocol (signon, NEWFILEUID, STMTRQ/CCSTMTRQ/INVSTMTRQ grouping, SGML headers) is handled by **`ofxtools.Client`** — already a dependency via the file parser — rather than hand-rolled SGML; the default app identity is **QWIN/2700** (Quicken), which most FIs require. The bank's OFX response *is* the same document the file importer reads, so `fetch_transactions` runs it straight through the existing **`ofx_parser.parse_ofx`** → raw-txn dicts → `ImportService.stage_feed` → FITID dedup / review / commit — **zero** feed-specific parsing or dedup. A failed signon (e.g. bad password) comes back as a valid OFX doc whose `STATUS` carries the error, so `ofx_status()` extracts the FI's own `CODE`/`SEVERITY`/`MESSAGE` and surfaces it as `OfxDirectError` instead of an opaque "no statements". The `client_factory` is injectable and `fetch_ofx(dryrun=True)` returns the request without sending — both make it offscreen-testable. A CLI probe **`ofx-check`** (mirroring `feeds-check`) verifies a real bank end-to-end — connectivity, auth, and that transactions come back — *before* any UI, with `--raw` to dump the request body. Next round: the config UI (store `OfxServer`/`OfxAccountSpec` + credentials in `setting`, link to a `feed_account` with `provider='ofx_direct'`, and an **Update accounts** action → review → commit).

**GoCardless** stays in the tree as a working provider for anyone who already has access, but is not built upon further. **Plaid/TrueLayer (paid UK)** remain rejected for a freely-shared app. Build order: **frictionless import first** (immediate value to every account, lowest risk), then **OFX Direct Connect**.

---

## Amendment 2 (2026-06-15) — correction: a free UK auto-feed *does* exist (Enable Banking); BYO-credentials providers fit a one-time-purchase app

The Amendment-1 conclusion that **"there is no free Open-Banking data API available to a UK individual"** was **wrong** — it generalised from GoCardless's closure without checking the successors. Research (2026 indie-developer landscape) corrects it:

- **Enable Banking** is the practical GoCardless replacement for the UK/EU: **self-serve signup** (no business registration), a **free "Restricted Production" tier for connecting your own accounts**, covering **UK + EU** (~2,700 banks, 30 countries) **including HSBC UK**. This is exactly the gap GoCardless left, still free, and it covers the owner's actual bank. Auth differs from GoCardless: an **RS256-signed JWT** (header `{typ:JWT, alg:RS256, kid:application_id}`, claims `iss:"enablebanking.com"`, `aud:"api.enablebanking.com"`, `iat`, `exp` ≤ 24 h), signed with the application's RSA private key → a new **`cryptography`** dependency (ships macOS + Windows wheels, so ADR-050 holds). Flow: `POST /auth` (aspsp + access window + redirect + state) → browser bank consent → `POST /sessions {code}` → `GET /accounts/{uid}/transactions`. Base `https://api.enablebanking.com`.

**The business-model insight that reframes everything (owner):** "a freely-shared app can't bear central per-user cost" never ruled out paid providers — only the *app developer* paying. Every provider here is **bring-your-own-credentials**: the *user* signs up and (if applicable) pays the aggregator directly, exactly as the app already stores the user's own OXR / Tiingo / GoCardless keys (ADR-035). So a **one-time App Store purchase + Patreon** is fully compatible, and **"willing to pay" genuinely unlocks options** that were dismissed in Amendment 1:

- **Enable Banking** — free for your own UK/EU accounts (the owner's HSBC case).
- **SimpleFIN Bridge** — ~$15/yr, user-paid, self-serve, **US** banks (read-only; the simplest possible client — claim URL → access URL → one `GET /accounts` returns accounts + transactions).
- **Plaid** — as of **2026-04-15** a free **Trial** tier (real production data, **10** Items, US/Canada) + pay-as-you-go for hobbyists; broadest US coverage but the heaviest integration (OAuth/Link).
- **Teller.io** — 100 free live connections, US-only (noted, not selected).

Also corrected: **HSBC "Direct Access 2" / Automated File Delivery is an HSBCnet (commercial) product**, not available on personal banking; and an individual **cannot call HSBC's Open-Banking API directly** (production access requires being an FCA-authorised TPP) — you reach it through an authorised aggregator like Enable Banking. So for personal HSBC UK the only routes are **file download** (Track 1) or **Enable Banking** (this amendment).

**Owner decision (`AskUserQuestion`):** build **three** more providers on the (unchanged) framework — **Enable Banking** (UK/EU, incl. HSBC), **SimpleFIN** (US), **Plaid** (US/CA) — all BYO-credentials. Build order: **Enable Banking first** (solves the owner's HSBC need, mirrors the GoCardless consent→fetch→normalize client), then **SimpleFIN** (simplest), then **Plaid** (heaviest). Each ships the same way as the GoCardless/OFX cores: a Qt-free client + a `normalize_*` into the existing `stage_feed` path + a headless probe + offscreen tests, with UI wiring after.

**All three provider cores shipped (2026-06-15)** — Qt-free clients, `normalize_*` into the existing `stage_feed` dedup/commit, headless `*-check` probes, offscreen-tested (request building, normalize, idempotent re-fetch):
- **Enable Banking** (`feeds/enablebanking.py`, `cli enablebanking-check`) — RS256-JWT auth (new `cryptography` dep); `list_aspsps` → `start_authorization` (browser consent) → `create_session(code)` → `fetch_transactions` (continuation_key paging). `normalize_enablebanking`: booked-only, magnitude + CRDT/DBIT → signed, counterparty payee, `entry_reference` → fitid.
- **SimpleFIN** (`feeds/simplefin.py`, `cli simplefin-check`) — `claim_access_url(setup_token)` → durable access URL; one `GET /accounts` returns accounts + inline transactions; embedded creds sent as Basic auth (kept out of the URL). `normalize_simplefin`: signed amount, epoch `posted` → date.
- **Plaid** (`feeds/plaid.py`, `cli plaid-check`) — `client_id`+`secret` in each body; `create_link_token` → `exchange_public_token` → `accounts_get` + incremental `sync_transactions` (cursor paging, persists `next_cursor`). `normalize_plaid`: **inverted sign** (positive = debit), pending dropped, per-account filter (an Item spans accounts).

**Unified Bank Feeds UI shipped (2026-06-15).** `feeds/sync.py` (Qt-free) holds the per-provider credential storage (`setting` keys) and a single `fetch_raw_for_feed(repo, feed)` that dispatches on `feed.provider` → builds the right client → returns raw-txn dicts (advancing + persisting Plaid's cursor). `ui/bank_feeds_dialog.py` `BankFeedsDialog` replaces the OFX-only dialog: one table across **all** providers (account · provider · institution · last-updated · status), with **Add** → `ProviderPickerDialog` → a provider-specific connect flow, **Remove**, and **Update selected / Update all** → `fetch_raw_for_feed` → the shared `stage_feed` → dedup → `commit_import`. Connect flows: OFX reuses `OfxConnectionDialog`; SimpleFIN pastes a setup token (claimed once); Enable Banking collects the app id + private key, picks a bank, opens the consent URL in the browser and takes the pasted redirect URL back (`code` extracted — no local web server), then `create_session`; Plaid collects client id/secret/env, opens Hosted Link, and on "finished" polls `get_link_public_token` → `exchange_public_token`. All end in a shared `AccountLinkDialog` mapping remote accounts → MFL accounts. The two browser-consent round-trips can't be exercised offscreen (no real bank/Plaid), but every network call goes through the same clients the headless `*-check` probes verify; the engine, dialog construction, the SimpleFIN add flow, and Update are offscreen-tested. Next round: consent-expiry/re-auth states (Enable Banking ~90-180d) and scheduled/background refresh.

## Verification

Round 1 (offscreen, no live network): the normalizer maps GoCardless booked/pending rows to the parser raw-txn shape (sign→tx_type, transactionId→fitid, names/remittance→payee/memo); `stage_feed` runs them through `_classify_and_stage` and `commit_import` with correct dedup on re-fetch (idempotent) and manual-match behaviour; `feed_account` CRUD round-trips and cascades on account delete; the GoCardless client builds correct requests (URLs, headers, bodies) against a stubbed transport. Live bank connectivity is verified by the owner against their own GoCardless secret (a headless check precedes the UI).
