# ADR-093 — Bonds and options as first-class securities

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-043 (investment accounts & QIF — `security` master + `txn` investment columns), ADR-044 (holdings/FIFO + prices + market-value net worth), ADR-046 (Investment Returns report), ADR-047 (transaction-derived prices for untickered holdings), ADR-048 (investment transaction dialog), ADR-053 (in-kind share transfers), ADR-054 (stock splits). Owner-requested for the 1.0 ship. Closes part of the "bond pricing by CUSIP" backlog investigation (2026-06-18).

---

## Context

The investment engine (ADR-043/044) models every instrument as a plain equity: a `security` carries a `name` + optional `symbol`, a trade stores `quantity` (shares) × `price` (per share), and market value is `Σ shares × latest price`. That assumption is wired through three places:

- the **holdings engine** (`holdings.py`) — `market_value = shares × price` in `compute_holdings_view`, `compute_value_history`, and `compute_returns`;
- the **transaction dialog** (`InvestmentTransactionDialog`) — the tri-field solver `Total = quantity × price ± commission`;
- **cost basis** — a cash-funded buy's basis is `abs(txn.amount)` (the true net cash, commission included).

That breaks for the two instrument classes the owner actually trades:

**Bonds** are quoted as a **percent of par** and traded in **par multiples**. The owner's most recent trade: *45 Apple bonds, par £1,000 each, at 99.618 (per 100 of par)*. The real cash is `45 × 1,000 × 99.618% = £44,828.10`, not `45 × 99.618`. A bond also carries a **coupon** (4%), a **redemption/maturity date**, and — the bit most apps forget — **accrued interest** paid to the seller for the period since the last coupon, which is part of the cash you hand over but is **not** part of what you paid for the bond (it is returned to you in the first coupon).

**Options** trade in **contracts**, each representing a **contract multiplier** of the underlying (conventionally 100 shares), priced as a **premium per share**. One contract at a 1.50 premium costs `1 × 100 × 1.50 = £150`, not £1.50. An option also has a **strike**, an **expiry**, and a **call/put** type.

Neither can be auto-priced (the 2026-06-18 investigation: Tiingo is ticker-only equities/ETFs/funds; bonds are untickered, options aren't covered). So both rely on the existing manual / transaction-derived price path (ADR-047) — that part already works; the gap is the **value math, the descriptive metadata, and the cash-vs-basis split**.

The owner asked for **basic recording** of options (the `AskUserQuestion` fork): record buys/sells to open and close, and expiry; **no** automated exercise → underlying shares, and **no** assignment. Bonds are wanted in full (par/multiples, coupon, maturity, commission, accrued interest).

---

## Decision

Make instrument class a property of the **security**, and express the par/contract economics as a single **price multiplier** so the engine's `shares × price` math stays intact.

### Schema (migration 0029)

`security` gains a structured discriminator + per-class metadata (all nullable / defaulted, so every existing row reads back as a plain stock and every existing query is unchanged):

| Column | Meaning |
|---|---|
| `instrument_type TEXT NOT NULL DEFAULT 'stock'` | `'stock'` / `'bond'` / `'option'` (CHECK). The existing free-text `type` (QIF `T`) is left as informational. |
| `price_multiplier REAL NOT NULL DEFAULT 1.0` | **The value-math source of truth.** Cash value of one unit at price = 1. Stock → 1; bond → `face_value / 100` (price is % of par); option → `contract_size`. |
| `face_value REAL` | Bond par per unit (1000). |
| `coupon_rate REAL` | Bond annual coupon %, e.g. 4.0. |
| `maturity_date TEXT` | Bond redemption date `YYYY-MM-DD`. |
| `cusip TEXT` | CUSIP / ISIN identity (closes the missing-identity gap flagged in the bond backlog; nullable, no auto-pricing implied). |
| `underlying_symbol TEXT` | Option underlying ticker. |
| `strike REAL` | Option strike price. |
| `expiry_date TEXT` | Option expiry `YYYY-MM-DD`. |
| `option_type TEXT` | `'call'` / `'put'` (CHECK allows NULL). |
| `contract_size REAL` | Option shares-per-contract (default entry 100). |

`txn` gains one nullable column:

| Column | Meaning |
|---|---|
| `accrued_interest INTEGER` | Pence of accrued interest paid at a bond purchase. Part of cash, **excluded from cost basis**. NULL on every other row. |

`price_multiplier` is deliberately **derived and stored**, not recomputed on the fly: the engine wants one number at the `shares × price` site without branching on instrument type, and the descriptive fields (`face_value` / `contract_size`) drive it at entry time. The dialog keeps them consistent (set face → set multiplier). The redundancy is documented and one-directional.

### Value, cash, and basis

For a trade of `q` units at quoted `p` with multiplier `m`, commission `c`, accrued `a`:

```
trade cost   = q × p × m            (the "principal")
cost basis   = trade cost + c       (commission capitalised, as today)
cash out/in  = cost basis (+ a on a buy)      → txn.amount = ∓ cash
market value = shares × latest_price × m
```

- **Accrued interest is in the cash, not the basis.** `txn.amount` (signed cash impact, so `cash balance = Σ amount` still holds) includes it; the holdings basis subtracts it back out (`basis = abs(amount) − accrued`). This matches a broker confirm: *Principal + Commission = cost; + Accrued = net cash*. Recording accrued as prepaid interest that nets against the first coupon is **deferred** (see Consequences) — for 1.0 it is captured on the trade and kept out of basis, which is the part that has to be right for returns/yield.
- **Stocks are byte-for-byte unchanged:** `m = 1`, `accrued = NULL`, so every formula collapses to the current one.

### Holdings engine (`holdings.py`)

`compute_holdings_view` / `compute_value_history` / `compute_returns` each take a new optional `multipliers: dict[int, float]` (security_id → multiplier, default `{}` → 1.0) and a per-transaction `accrued`:

- every `shares × price` / `qty × price` **market-value** site multiplies by the security's `m`;
- the cash-buy **basis** is `abs(amount) − accrued_interest` (so commission stays in, accrued comes out);
- the priced share-in basis (`price × qty`, used by reinvest / shares-in-with-price) multiplies by `m`;
- the **displayed** `Holding.last_price` stays the raw quote (99.618), not the effective per-unit value — the multiplier is applied only inside value sums.

Explicit `multipliers` parameter (vs folding `m` into the price maps at the Repository boundary) is chosen because the price maps are built by ~6 scattered callers and feed both value **and** the displayed quote; a forgotten call site then shows a bond at `m = 1` (a visible, debuggable under-value) rather than silently corrupting an equity's quote.

### Transaction dialog (`InvestmentTransactionDialog`)

A new **Instrument** combo (Stock / Bond / Option) reveals the relevant metadata rows and switches the math:

- **Bond** — Face value, Coupon %, Maturity, CUSIP, Accrued interest; the Price field is labelled *Price (% of par)*; `m = face / 100`. Total cost (tri-field) is the principal `q × p × m ± commission`; Accrued adds to cash only; the hint shows the all-in cash out.
- **Option** — Underlying, Strike, Expiry, Call/Put, Contract size; Price is *Premium per share*; quantity is **contracts**; `m = contract_size`. Expire-worthless = a **Sell at price 0** (proceeds 0, realised loss = basis); the hint says so when the instrument is an option.
- **Stock** — exactly as today.

The tri-field solver becomes multiplier-aware (`Total = q × p × m ± commission`); `m` is constant once the instrument's face/contract is set. On save the dialog creates/updates the security's `instrument_type` + metadata + `price_multiplier` and stores `accrued_interest` on the trade.

Buy/Sell are **reused** for bonds and options — they are cash trades; the class differences live entirely in the security's multiplier/metadata, so the action-classification sets (`qif_actions`) and the FIFO replay are untouched.

### Surfaces

- **Stock Record dialog** shows the instrument metadata as a read-only summary line (a bond's par/coupon/maturity/CUSIP, an option's underlying/strike/expiry/type/size); the row is hidden for a plain stock. Editing the metadata happens on a trade in the Investment Transaction dialog (every save re-asserts the security's class + metadata via `update_security`). A full inline metadata editor on the Stock Record screen is a small follow-up.
- **Net Worth / Home / per-account summary** already read `compute_account_values` → market value is now multiplier-correct with no change at those call sites beyond passing `multipliers`.

### Scope boundary (basic options)

Long options (buy-to-open → sell-to-close / expire) are fully supported. **Written / short options** (sell-to-open) record their cash correctly but, having no prior lot, leave the holding `basis_incomplete` — the same honest-flag behaviour the engine already uses for oversells. Exercise and assignment are out of scope for 1.0.

---

## Consequences

- Bonds and options value, cost-basis, and report correctly; an equity portfolio is unaffected (multiplier 1, accrued NULL — every formula reduces to the current one).
- The percent-of-par + accrued split means a bond's **return** and **cost basis** are right, not inflated by prepaid interest — the thing most consumer apps get wrong.
- `TransactionRow` gains `accrued_interest`; the investment SELECTs that build it and the insert/update paths carry the new column. The three holdings functions gain optional params (default-compatible).
- **Deferred:** coupon-income auto-scheduling (use `coupon_rate` + `maturity_date` to seed coupon receipts and net accrued interest against the first one) — a natural follow-on once the schedules↔instruments link from the budget-bill work (ADR to come) exists; option exercise/assignment; a bond yield-to-maturity column; auto-pricing (still impossible — manual / transaction-derived only).
- No change to dedup, import, or the cash code path (`instrument_type` defaults to `'stock'`; QIF imports keep minting stocks until a bond/option arrives via a feed that names it — not a current source).

### Rejected alternatives

- **Normalise bonds internally** (store quantity as face units, price as a fraction) so `qty × price` works unmodified — discarded: the owner enters and wants to see *45 bonds*, not *45,000 par units*.
- **Fold the multiplier into the price maps at the Repository boundary** (zero engine change) — discarded: the maps also feed the displayed quote and are built by many callers; a miss silently corrupts an equity's shown price. Explicit engine param fails visibly instead.
- **Capitalise accrued interest into cost basis** (simplest — just add it to cash) — discarded: it overstates basis and understates yield; accrued is returned in the first coupon, it was never part of the price of the bond.
- **A separate `bond` / `option` table** (1:1 with security) — discarded: the metadata is a handful of nullable columns and every read already goes through `security`; a side table doubles the join surface for no integrity win.
- **Dedicated bond/option buy/sell actions** — discarded: a bond purchase is a cash buy; the action sets and FIFO replay shouldn't grow a parallel vocabulary when the only differences are numeric (multiplier) and descriptive (metadata).
- **Full options lifecycle (exercise/assignment) for 1.0** — out of scope per the owner's "basic recording" fork; revisit if option trading becomes frequent.
