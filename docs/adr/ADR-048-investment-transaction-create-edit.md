# ADR-048 — Investment transaction create + edit (dialog)

**Date:** 2026-06-09
**Status:** Accepted
**Related:** ADR-043 (investment `txn` columns + `qif_actions` action classification + the signed-cash-impact convention this upholds), ADR-047 (transaction-derived prices — a manual trade reseeds them), ADR-044/046 (FIFO holdings + returns engines that consume `action`/`quantity`/`price`), ADR-020/035 (cash transfers — explicitly *not* handled here for investment XIn/XOut), ADR-022 (the inline-delegate editing model this deliberately bypasses for investment rows), ADR-042 (parked inline-entry attempt — the lesson that a dialog is the safer surface when many fields interact).

---

## Context

Round 1 (ADR-043) imported investment rows (Buy/Sell/Div/…) into an interleaved register but left them **read-only**: `COLUMNS_INVEST` marked Date/Action/Symbol/Security/Qty/Price/Amount non-editable (only Status/Memo were inline-editable), and **New Transaction** always opened the cash-shaped `NewTransactionDialog`. So on an E\*Trade account, double-clicking a field did nothing and there was no way to hand-enter or correct a trade — the owner hit both while loading real data.

Editing investment fields is not like editing a cash cell: changing qty, price, action, or commission must re-derive the **signed cash impact** (`txn.amount`) and the FIFO consequences, and the cross-field rules differ by action. Inline per-cell delegates would have to re-run that derivation on every cell commit, with many ways to leave a row inconsistent. **Owner chose (via AskUserQuestion) a single dialog for both create and edit** over inline grid editing — one place that owns all the cross-field rules.

(Setting a missing **ticker** is a *security-master* edit, handled by the ADR-047 Stock Record screen — not this dialog. This dialog edits a transaction's trade data.)

---

## Decision

### One dialog — `ui/investment_transaction_dialog.py` (`InvestmentTransactionDialog`)
Opened in **create** mode by New Transaction when the focused account is an investment account, and in **edit** mode by **double-clicking an investment row**. It writes through the Repository itself and calls `accept()`; the caller (`register_window`) just reloads the model + sidebar balances.

**Investment-shaped fields (not the cash form).** The form is **Account (context) · Date · Action · Symbol · Security · Qty · Price · Status · Memo** — Payee, Commission, and a standing cash-amount box are gone (owner: the first cut "still had cash fields"). A curated action combo (Buy / Sell / Div / ReinvDiv / IntInc / CGLong / CGShort / ShrsIn / ShrsOut / Cash — canonical QIF strings matching the `qif_actions` sets) **shows or hides** rows so only the relevant fields appear, and decides the cash amount:
- **Buy / Sell** — Qty + Price; signed cash impact **computed silently** (`∓ qty·price`). No commission field (the owner's E\*Trade rows are commission-free — verified `qty·price` equals the imported amounts); when *editing* a trade whose Qty + Price are unchanged, the seed's stored amount is preserved so a re-save never drifts off the imported figure.
- **ReinvDiv** — Qty + Price; cash impact **0** (the reinvested value is income, counted by the returns report from price·qty per ADR-046).
- **ShrsIn / ShrsOut** — Qty (+ optional Price for basis); cash impact **0**.
- **Div / IntInc / CGLong / CGShort** — an **Amount** field replaces Qty/Price (positive cash in); routed to the system **Income:Investment income** category (`qif_parser._INCOME_CATEGORY`).
- **Cash** — an Amount field only (signed); no security.

**Symbol drives Security.** Symbol sits *above* Security. Typing a ticker resolves the security on field-exit: first to an existing security carrying that symbol (selects it), else — when online with a Tiingo key — to a **`prices.lookup_symbol_name` → Tiingo metadata** call that fills the security name (a new security, created on save with that symbol); offline/unknown falls back to manual name entry. Picking an existing Security fills Symbol the other way. Because the ticker lives on the *security*, saving persists a changed symbol via `Repository.update_security` (or `get_or_create_security` for a new one) — applying to all that security's rows and re-enabling Tiingo, exactly like the ADR-047 Stock Record edit. With `securities_missing_history` ignoring transaction-derived prices, tickering here triggers a full Tiingo backfill next launch.

The stored `txn.amount` is always the **signed cash impact** (ADR-043 invariant). Non-income/cash actions resolve to **Uncategorised**; category isn't exposed (investment rows are almost always Uncategorised or Investment-income). After a save the dialog calls `seed_prices_from_transactions([security_id])` so a manual trade on an **untickered** security feeds its price history (ADR-047) — a no-op for tickered ones.

### Repository / prices
`update_investment_transaction(...)` — one write of every editable field (the per-field updaters can't change action/security/qty/price). `insert_transaction` covers create. `prices.TiingoClient.fetch_metadata(symbol)` + `prices.lookup_symbol_name(repo, symbol)` provide the symbol→name lookup (best-effort: `None` when no key/offline/unknown).

### Register wiring (`register_window` / `register_model`)
- `COLUMNS_INVEST` Status/Memo flipped to **non-editable**, so the whole investment row is dialog-edited and Qt's double-click edit-trigger never opens an inline editor on these rows.
- `self._table.doubleClicked` → `_on_table_double_clicked`: for an investment account it maps the proxy index to the source row and opens the dialog in edit mode (no-op for cash accounts — their cells stay inline-editable).
- `_on_new_transaction` branches to the investment dialog when the focused account's family is `investment`.
- **Bulk Edit** gains a **Symbol** field too (`BulkEditDialog` takes an optional `security_context`): when every selected row is the *same* security, the dialog offers a ticker checkbox prefilled with that security's current symbol. `_on_bulk_edit` computes the single-security context, pops `symbol` from the change set (it isn't a txn column), and applies it via `update_security`. Shown only for a single-security selection — a ticker can't sensibly apply to a mixed set.

---

## Consequences

### Positive
- **Verified headless on a copy of the live DB:** the action combo shows/hides the right rows (Buy → Symbol/Security/Qty/Price, amount hidden; Div → Amount, Qty/Price hidden; Cash → Amount only); New → Buy 10 @ 12.5 of an untickered security computes −125.00 and seeds a `transaction` price of 12.5; typing an existing ticker ("DIVO") resolves to its security; editing Tesla's first buy with Qty/Price unchanged preserves the stored amount (−6849.80). All modules compile and construct offscreen.
- Investment rows are now first-class: hand-enter a trade not in any import, or correct an imported one, with the cash impact and FIFO staying correct because the derivation lives in one place.

### Negative / trade-offs
- **Inline editing is gone for investment rows** (Status/Memo included) — by design; everything routes through the dialog. If quick status-only edits prove painful, a future inline status delegate could return for investment rows.
- **Transfer actions (XIn/XOut) and stock splits are not offered** — transfer-linking is a later round (ADR-036 reuse) and split-ratio handling is the ADR-044 deferral. An imported row carrying such an action still opens in edit mode (its action is added to the combo so the edit is faithful) but the curated list doesn't offer them for new entry.
- **No delete-from-dialog and no category picker** in v1; delete stays the existing register/bulk path, category is auto-resolved.
- **No commission field / no manual cash-amount override for trades.** Trade cash = `qty·price` (exact for the owner's commission-free data). A trade with a separate fee, or whose statement cash differs from `qty·price`, can't be hand-entered to the penny — the edit-preservation rule covers *imported* rows, but a fresh manual trade with a fee would be off by the fee. Add a commission/override field back if a fee-charging brokerage appears.
- **Symbol lookup is a blocking Tiingo call on field-exit** (with a wait cursor); offline/no-key returns instantly (key checked first), so it only stalls when actually reaching the network.

### Ongoing responsibilities
- Any new action added to the curated list must exist in the `qif_actions` classification sets, or the holdings/returns engines won't account for it.
- The signed-cash-impact convention (ADR-043) is the contract `update_investment_transaction` and the dialog's amount logic both depend on — keep amount derivation in step with `qif_parser._cash_impact` if either changes.
