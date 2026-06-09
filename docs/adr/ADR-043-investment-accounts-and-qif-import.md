# ADR-043 — Investment accounts & QIF import (arc plan + round 1)

**Date:** 2026-06-08
**Status:** Accepted (round 1 shipped)
**Related:** ADR-010 (transactional schema / Repository contract — this extends `txn` and adds the `security` table; the dormant `lot`/`valuation` tables wait for rounds 2–3), ADR-006 (IRI naming — `mfl:Security_<uuid8>`), ADR-038 (Banktivity trusts the amount sign — the same "the exported number is the truth" principle drives the action→cash-sign mapping here), ADR-020/035/036 (transfers — round 4 will link investment cash transfers through the existing matcher), ADR-014 (category kind — dividends route to the system *Investment income* category).

---

## Context

MFL had no investment support. The `investment_std` account type existed in `account_types.py` and the `lot`/`valuation` tables existed (ADR-010) but were entirely unwired; there was **no `security` table**, the `txn` table was **pure-cash** (amount/payee/category/status — no action, security, quantity, or price), the whole import pipeline (`ClassifiedTransaction`) was cash-shaped, and there was **no QIF parser** (only a README backlog note).

The owner wants real investment data in, starting from a Banktivity QIF export of an E*Trade account (`etrade_mark.qif`): four sections — `!Account` (name, `TInvst`, `B46839.61` cleared balance), `!Type:Cat` (the source category list), `!Type:Invst` (957 transactions), `!Type:Security` (91 securities). Investment actions present: **Buy 429, Div 348, Cash 112, ShrsIn 35, Sell 13, ReinvDiv 10, CGShort 4, CGLong 4, XIn 1, StkSplit 1**.

This is a multi-round arc (like budget and reports). Because the round-1 schema choices lock in data once the owner imports, the data-model fork and scope boundary were settled up front.

### The arc

| Round | Scope |
|---|---|
| **R1 (this ADR)** | QIF import, `security` master, `txn` investment columns, investment register view. Data in + visible + cash balance correct. |
| R2 | Holdings & cost basis — wire `lot`, shares-per-security, average cost, realized gains on sells. |
| R3 | Prices & market value — `valuation` entry/feed, value = cash + Σ(qty×price), unrealized gain, Net Worth integration. |
| R4 | Transfer-linking (reuse ADR-036 matcher for the `L[Account]` cash rows), dividend/income reporting polish, per-currency display. |

---

## Options considered

### Data model — how to store an investment transaction

A Buy/Sell/Div carries an action, a security, a quantity, a price, and a cash impact, none of which `txn` held.

**A — Extend `txn` with nullable investment columns. (Selected.)** One interleaved register per account — buys, sells, dividends, and cash all in one chronological stream, exactly as QIF, Banktivity, and Quicken model it. `txn.amount` stays the **signed cash impact**, so cash balance = `SUM(amount)` is unchanged and the existing register/model/proxy/Repository carry over with extra columns shown only for investment accounts. `action IS NULL` ⇒ an ordinary cash row, so every existing account and the entire cash code path are untouched. Minimal new plumbing.

**B — Separate `investment_txn` table.** Keep `txn` pure-cash; add a parallel table for security actions with its own cash leg. More normalized, but it splits the account into two streams, needs a second model + proxy + import path + a merged view to present one register, and diverges from how the source QIF is shaped. Rejected — the cost is real and the benefit (purity) is cosmetic for a single-user personal-finance ledger.

The owner confirmed A.

### `quantity` / `price` storage type

Stored as **REAL**, matching the existing `lot.quantity` / `lot.unit_cost` choice (ADR-010). Money stays integer pence (`amount`, `commission`). Fractional shares are pervasive in this data (reinvestments down to 0.001 shares), and float display is acceptable for a quantity; exact-decimal share accounting, if ever needed, is a round-2 concern when lots are computed.

### `action` constraint

**Free-text, validated in Python (no SQL CHECK).** QIF exports carry quirks — this file has a malformed empty `StkSplit` and an `XIn` that moves shares rather than cash — and the action vocabulary grows across brokers. `qif_parser` normalises and classifies; an unrecognised action falls back to "treat the signed total as cash" so a real movement is never silently dropped. A CHECK would turn every new broker quirk into a migration.

### Transfers (`L[Chase Checking]` + `$`)

The deposit/withdrawal `Cash` rows name a linked account. **Imported as plain cash rows in round 1**, with the linked account recorded in the memo (`"Transfer from Chase Checking"`); the row keeps `amount = T` so the cash balance stays correct. Real cross-account linking reuses the ADR-036 matcher in round 4 — pulling it into round 1 would couple the import to the matcher and require the other account to already exist. Owner confirmed.

### `!Type:Cat`

**Parsed but not bulk-created in round 1.** Categories already auto-create on demand, and investment rows carry no per-row category — only dividends/cap-gains need one, and those map by action (below). Pre-seeding Banktivity's entire 700-line tree would create a mass of unused categories. Deferred.

### QIF breadth

**Investment-focused, extensible.** The parser handles this file fully and is structured so `!Type:Bank` / `!Type:CCard` dispatch slots into `_section_for_header`/`_dispatch_record` later. Round 1 doesn't need general QIF.

---

## Decision

### Schema — migration `0012_investments.sql`

- New **`security`** table: `id / iri (mfl:Security_<uuid8>) / name UNIQUE / symbol / type / archived_at`. Referenced by **name** (the QIF `Y` field); `symbol` is nullable because Banktivity frequently exports it blank, and serves as the secondary key for a future price feed.
- **`txn`** gains five nullable columns: `action TEXT`, `security_id INTEGER → security`, `quantity REAL`, `price REAL`, `commission INTEGER` (pence), plus a partial index `idx_txn_security`.

### The action → cash-sign mapping (the crux)

`txn.amount` remains the **signed cash impact**, so cash balance = `SUM(amount)` holds across the interleaved stream:

| Action | Cash impact | Shares |
|---|---|---|
| Buy | `−T` (cash out) | + |
| Sell | `+T` (cash in) | − |
| Div / CGShort / CGLong / IntInc / MiscInc | `+T` (distribution in) | — |
| Cash | `T` as-is (already signed: deposit +, withdrawal −) | — |
| ShrsIn / ShrsOut / ReinvDiv / StkSplit | `0` (no cash — a reinvested dividend nets to zero) | ± / — |
| XIn / XOut | `±T` | (XIn may carry shares with T=0) |

`quantity` is the positive magnitude QIF exports; the action carries direction. The parser expresses the cash impact as `(abs(amount), tx_type)` so the import service reuses its existing sign-from-`tx_type`, duplicate-detection, and category-resolution machinery unchanged.

### QIF parser — `mfl_desktop/import_engine/qif_parser.py`

`parse_qif(bytes, filename) → QifFile` (`.account`, `.securities`, `.categories`, `.transactions`, `.is_investment`). Reuses `csv_parser._decode` and `_parse_amount_str`. Date parsing handles QIF US M/D/Y, Banktivity's 2-digit years, and the Quicken apostrophe form. Each transaction is a **superset of the cash dict** the import service already consumes, plus `action / security_name / quantity / price / commission / linked_account`. Dividend-type actions get `category_raw = "Income:Investment income"`; everything else is left to Uncategorised.

### Import service — `import_service.py`

`ClassifiedTransaction` / `PendingImport` grow optional investment fields + a `securities` list + `is_investment`, all defaulting empty (cash path unchanged). `.qif` routes through `qif_parser`; `_classify_and_stage` hashes investment rows on `account_iri|date|action|security|quantity|amount` (the cash hash would collide on the many same-day rows sharing date+amount) and skips the ±2-day manual-match (designed for cash entry). `commit_import` creates the securities master first (building a name→id map; rows referencing an unmastered name create it on the fly), resolves `security_id`, and passes the investment kwargs to the extended `Repository.insert_transaction`.

### Repository

`new_security_iri()`, `SecurityRow`, `get_or_create_security` (upsert by unique name; backfills a blank symbol/type but never overwrites), `list_securities`. `insert_transaction` gains backward-compatible investment kwargs. `TransactionRow` and both `list_transactions_for_account` / `list_all_transactions` gain a `LEFT JOIN security` surfacing `action / security_id / security_name / security_symbol / quantity / price`.

### Register UI

`TransactionTableModel` gains a `COLUMNS_INVEST` layout — `Date / Action / Security / Qty / Price / Status / Memo / Amount / Balance` — selected via an `invest` flag the window sets from `account.family == "investment"`. The security-action columns are **read-only in round 1** (inline qty/price/action editing is R2+), so no new delegates; Status/Memo stay editable via the existing delegates, which attach by column *name*. `register_window` adds `*.qif` to the import filter and passes the flag. The all-transactions view keeps its cash-shaped layout (investment rows show their cash `amount` there).

---

## Consequences

### Positive

- **The owner's E*Trade history imports end-to-end** through the existing silent-commit flow (CLI and GUI). Verified: 957 transactions, 91 securities, cash balance **$46,839.77 vs the QIF's stated $46,839.61 — a 16¢ residual** (sub-cent reinvestment rounding in the source's own balance; the action-class nets are all correctly signed — Buy −296k, Cash +170k, Sell +138k, Div +31k — so a sign error would show as thousands, not cents). Re-import dedups to zero new.
- **One interleaved register**, matching the source and the owner's mental model, with almost no new UI plumbing.
- **The cash code path is untouched** — `action IS NULL` everywhere it already was.

### Negative / trade-offs

- **Net Worth understates an investment account until round 3.** With no market value yet, the account contributes only its **cash balance** to Net Worth and the summary screen (the existing "valuations not yet wired" banner already covers this). The headline is silently low while holdings carry unrealised value — acceptable for round 1, closed by R3.
- **No holdings/cost-basis view yet (R2)** and **transfers are unlinked cash rows (R4)** — both deliberate scope cuts; the data is fully captured (`security_id`, `quantity`, `price`, `commission`, linked-account memo) so later rounds compute from it without re-import.
- **A handful of source quirks ride through as-is:** the malformed empty `StkSplit` imports as a zero-cash marker row (split-ratio handling is R2), and the `XIn` account-out transfer imports as a zero-cash share row. Both are faithful to the file and harmless to the balance.

### Ongoing responsibilities

- **Any new transfer-writing path stays out of the investment import in R1** — round 4 owns linking. Until then investment cash transfers are plain rows.
- **`amount` must remain the signed cash impact for investment rows** — holdings (R2) and market value (R3) both assume cash balance = `SUM(amount)` and derive share positions from `quantity`/`action` separately.
- **The investment dup-hash includes action+security+quantity** — any future per-row edit or re-export that changes those will re-classify a row as new; that's correct (it *is* a different row), but worth remembering when debugging an unexpected "new" on re-import.
