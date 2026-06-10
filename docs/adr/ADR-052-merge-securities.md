# ADR-052 — Merge securities

**Date:** 2026-06-10
**Status:** Accepted
**Related:** ADR-043 (the `security` master — `name` is `UNIQUE` and the importer keys on it, which is what produces duplicates), ADR-044 (`security_price` table + FIFO holdings that a merge consolidates), ADR-047 (price-source precedence `manual > tiingo > transaction` + Stock Record screen, where the merge verb lives), ADR-012-amend/ADR-029 (`merge_payees`) and ADR-013 (`merge_categories`) — the established merge pattern this mirrors.

---

## Context

`security.name` is `UNIQUE` and the QIF/Banktivity importer keys securities on the name (`get_or_create_security`). So when a fund is **renamed** — or simply exported under two spellings across statements — it lands as **two `security` records that share one ticker**. The owner hit this with TSBIX:

| id | name | ticker | txns | prices |
|----|------|--------|------|--------|
| 47 | Nuveen Core Impact Bond R6 | TSBIX | 23 | 219 tiingo |
| 65 | TIAA CREF CORE IMPACT BD INST | TSBIX | 131 | 59 transaction |

The position is split across both records: holdings, cost basis, and the Investment Returns report each see two half-positions instead of one. The fund's *current* name and Tiingo price history live on id 47; the bulk of the *transaction history* lives on id 65. Neither record alone is right.

There was no way to combine them. A "merge securities" verb has been on the backlog since ADR-044 ("a merge-securities verb is still unbuilt … 56 untickered holdings may hide some"). The app already has the exact shape for this in two other places — `merge_payees` and `merge_categories` (re-point every reference to a survivor, delete the source, atomically) — so this is filling a known gap with an established pattern, not new architecture.

Only two tables reference a security: `txn.security_id` (FK `ON DELETE SET NULL`) and `security_price.security_id` (FK `ON DELETE CASCADE`, PK `(security_id, price_date)`). The `lot` table keys on `symbol`, not `security_id`, so it is untouched. That small surface is why no schema change is needed.

---

## Options considered

**Where the merge happens**

**(A) A standalone Merge dialog reached from the Securities list (multi-select two rows).** Rejected for v1: the owner asked for it "in the stock record," and the Stock Record is already the per-security home where you'd notice a duplicate (its price history and transactions are right there). A list-level multi-select merge can be added later; it would call the same Repository method.

**(B) "Merge…" button on the Stock Record.** Chosen. You open the record that looks wrong, pick the other record, and merge. The dialog stays a thin wrapper over the Repository verb.

**Which record survives**

**(C) Always keep the record the Stock Record was opened from.** Predictable, but in the TSBIX case the owner might open *either* duplicate, and the "better" record (real ticker + 219 Tiingo prices) isn't necessarily the one they clicked. Forcing them to open the right one first is a footgun.

**(D) Default the survivor to the better-data record, but let the user flip it (radio).** Chosen (owner picked this via AskUserQuestion). The default survivor is scored: prefer a real ticker, then more *real* (manual/tiingo) prices, then more total prices, then more transactions. A side-by-side comparison (ticker / #txns / #prices / latest price) and a live "N transactions and M prices move to «survivor»; «other» is deleted" line make the consequence explicit before the user commits. The radio means the heuristic never traps them.

**How colliding prices reconcile**

When both records hold a price on the *same date*, the survivor can keep only one (PK `(security_id, price_date)`). **(E) Pick by ADR-047 source precedence** — manual > tiingo > transaction; a tie keeps the survivor's existing row. Chosen because it's the same rule every other price write already obeys, so a merge can't downgrade a hand-typed or provider price to a trade-derived one. The comparison is rank-based (`manual`=3 / `tiingo`=2 / `transaction`=1), so the result doesn't depend on the order rows are merged.

---

## Decision

New `Repository.merge_securities(source_ids, target_id) -> int`, beside `update_security` and mirroring `merge_payees`/`merge_categories`:

1. Drop `target_id` from `source_ids` defensively; no-op on an empty source list; raise `ValueError` if the target doesn't exist.
2. `UPDATE txn SET security_id = target WHERE security_id IN (sources)` — preserves the transactions (a bare delete would `SET NULL` them per the FK).
3. Move prices via `INSERT … SELECT … FROM security_price WHERE security_id IN (sources) ON CONFLICT(security_id, price_date) DO UPDATE …` whose `WHERE` keeps the row only when the incoming source out-ranks the existing one (manual=3 > tiingo=2 > transaction=1). A plain `UPDATE` would violate the PK on any shared date.
4. `DELETE FROM security_price WHERE security_id IN (sources)` (the copies that lost a collision, and the source side of every moved row).
5. `DELETE FROM security WHERE id IN (sources)`.
6. Commit (steps 2–5 are one transaction; rollback on any error). Then, **outside** the transaction, `seed_prices_from_transactions([target])` so a trade moved onto an *untickered* survivor still seeds a transaction price — a no-op when the survivor carries a ticker.

UI: a **"Merge…"** button opens `mfl_desktop/ui/merge_securities_dialog.py` from two entry points — the Stock Record header (the per-security home, option B) **and** the Manage ▸ Securities list (added after first use showed the list is where you actually notice duplicates side-by-side — it pre-loads the selected row as the security in hand and reloads the list afterwards, dropping the absorbed row). It lists every *other* security (same-ticker matches surfaced first and pre-selected), shows the comparison + survivor radio + live confirmation, and on accept calls `merge_securities` and exposes `survivor_id`/`absorbed_id`/`moved_count`. The Stock Record then **closes** if the record in view was the one absorbed (it no longer exists, so the parent Securities dialog refreshes and drops the duplicate) or **reloads** if it was the survivor.

No migration — pure data operation + UI.

---

## Consequences

- **Duplicate positions can be healed.** After merging TSBIX, holdings/cost-basis and the Investment Returns report see one consolidated FIFO stream (the engines replay by date, so combined history computes correctly).
- **Irreversible, and the dialog says so.** Like `merge_payees`/`merge_categories` there is no undo; the confirmation spells out exactly what moves and what is deleted, and the survivor is a deliberate choice.
- **Price provenance is never downgraded.** The precedence guard means a manual or Tiingo price always wins a same-date collision over a trade-derived one.
- **Known limitation:** a saved Investment Returns report whose filter pins the *absorbed* security's id keeps that stale id (it simply matches nothing after the merge) — re-pick the security in the report filter. Re-pointing filter JSON on merge was judged over-engineering for v1.
- **Verified** headless on a consistent snapshot of the live DB (SQLite backup API, since the live file is WAL-mode): merging id 65 → id 47 moved 131 transactions (survivor 23 → 154), deleted the source, left zero dangling references, preserved the union of price dates, dropped the one transaction price that collided with a Tiingo date (precedence), and produced no PK collisions. An offscreen Qt smoke (PySide6 6.11.1) confirmed the same-ticker pre-selection, the best-data default survivor, the comparison grid, the live confirmation text on flipping the survivor, and the Stock Record's wired Merge button.
