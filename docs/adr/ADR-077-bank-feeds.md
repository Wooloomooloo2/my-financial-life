# ADR-077 — Bank feeds (Arc H): pluggable providers, GoCardless first

**Date:** 2026-06-15
**Status:** Accepted — planning + round 1 (foundation). Per-round detail follows as each lands.
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

## Verification

Round 1 (offscreen, no live network): the normalizer maps GoCardless booked/pending rows to the parser raw-txn shape (sign→tx_type, transactionId→fitid, names/remittance→payee/memo); `stage_feed` runs them through `_classify_and_stage` and `commit_import` with correct dedup on re-fetch (idempotent) and manual-match behaviour; `feed_account` CRUD round-trips and cascades on account delete; the GoCardless client builds correct requests (URLs, headers, bodies) against a stubbed transport. Live bank connectivity is verified by the owner against their own GoCardless secret (a headless check precedes the UI).
