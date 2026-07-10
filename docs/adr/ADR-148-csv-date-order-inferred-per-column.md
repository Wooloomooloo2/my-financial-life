# ADR-148 — Generic-CSV date order is inferred per column, not per row

**Date:** 2026-07-10
**Status:** Implemented
**Related:** ADR-021 (generic CSV column-mapping wizard + saved mappings). ADR-036/037 (transfer matching; Reconcile Transfers ±window). ADR-130 (`bank_posted_date`). ADR-050 (cross-platform: UK *and* US files are both first-class).

## Context

Owner report: a **$12,000 transfer out of MS Joint Brokerage to eTrade on 2021-05-12** never appeared in Manage ▸ Reconcile Transfers, even though the matching eTrade inflow was present.

The eTrade side (QIF) was dated `2021-05-12`. The MS Joint Brokerage side (generic CSV) was stored as **`2021-12-05`** — day and month transposed. 207 days apart, so `find_transfer_pairs` discarded the candidate on its `abs(days_apart) > window_days` guard before scoring. Nothing surfaced; nothing warned.

The cause was `_parse_generic_date`, which tried a fixed list of `strptime` patterns and returned on the first that fit:

```python
formats = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", ...]
```

`%d/%m/%Y` precedes `%m/%d/%Y`, so a US `MM/DD/YYYY` export parses **day-first whenever the day is 12 or less** and the row silently lands on the wrong date. Rows with a day of 13+ fail DMY and fall through to MDY, landing correctly. The corruption is therefore *partial and interleaved* — roughly the 39% of rows whose day ≤ 12 — which is precisely why it survived the import preview: the five preview rows looked plausible, and the account's running balance was unaffected because only dates moved.

The row is the wrong unit of decision. `05/12/2021` alone is genuinely undecidable; the **column** almost never is. A single `05/14/2021` anywhere in the file proves the format is month-first.

Three accounts were imported through the generic path (`import_batch.source_format = 'csv-generic'`), all with `date_format: "auto"`:

| Batch | Account | Source | Order | Wrong rows |
|---|---|---|---|---|
| 34 | (Closed) Amex Gold | `Amex Gold.csv` | `M/D/YYYY` | **597 / 2667** |
| 43 | Standard Life Pension | `K8416250000-plan-statement.csv` | `DD/MM/YYYY` | 0 / 7 |
| 44 | MS Joint Brokerage | `MS Joint Brokerage.csv` | `M/D/YYYY` | **16 / 151** |

Standard Life escaped only because it is a UK file, which is what the day-first bias assumed. The Banktivity (`_parse_banktivity_date`, unconditionally month/day/year), QIF, and OFX paths were never affected — this is a generic-CSV-only defect.

## Decision

**Read the whole date column before parsing any row of it.**

`infer_day_first(samples) -> bool | None` scans a column: a field > 12 in the **first** position can only be a day (⇒ day-first); in the **second** position can only be a month (⇒ month-first). ISO `%Y-%m-%d` values and unparseable cells cast no vote. It returns `None` when the column never disambiguates (every field ≤ 12) *or* contradicts itself, which are both cases where the file genuinely cannot tell us.

`make_generic_date_parser(samples)` binds that decision into a `Callable[[str], str]` and logs which order it inferred. On `None` it falls back to **day-first** — the historic behaviour, and the right bias for a UK-origin app — and logs a warning pointing at the mapping dialog's explicit date-format combo.

`_parse_generic_date` keeps a `day_first: bool = True` keyword so a single cell can still be parsed in isolation, but both import call sites (`parse_with_mapping`, `_parse_generic`) now materialise their rows and build a column-bound parser first. The mapping dialog's after-mapping preview builds its parser from the **entire** file, not the five rows on screen, so what the user previews is what the import commits.

Rejected:

- **Reordering the format list to put `%m/%d/%Y` first.** Symmetrically wrong — it silently corrupts every UK export instead of every US one. There is no correct global default; that's the whole point.
- **`dateutil` with `dayfirst=`.** Same problem wearing a library. It still wants the answer this ADR exists to compute, and adds a dependency.
- **Defaulting `date_format` to an explicit pattern in the wizard.** Pushes a question onto the user that the file itself answers 999 times in 1000, and the one time it can't, they now get a warning.
- **Warning on ambiguity instead of guessing.** Sound in principle, but an all-ambiguous column is rare enough (it needs *every* row to have day ≤ 12) that a modal on every import would be noise. The log line + explicit override covers it.

## Data repair

The bug is exactly invertible from the stored value, with no reference to the source file: a flipped row has `stored_day = original_month ≤ 12`, and a correctly-parsed row has `stored_day = original_day ≥ 13`. So `stored_day ≤ 12` ⟺ *flipped*, and swapping day↔month on precisely those rows restores the original.

Verified before writing, on the live DB: re-parsing both source CSVs with the buggy format list reproduced the stored dates **exactly** (multiset-equal, 2667/2667 and 151/151), and applying the swap rule reproduced the correct MDY parse **exactly**. Independently, MS Joint Brokerage's import order (`txn.id`) is monotonic in date with 6 violations as-is and **0** after the swap — the source file was date-sorted, and only the true dates make it so.

Applied to `posted_date` and `bank_posted_date` for `import_batch_id IN (34, 44)` in one transaction, after a `.backup` snapshot (`mfl_dev_windows5.pre-datefix-20260710-103718.mfl`). Batch 43 was left alone. The 613 corrected rows own no splits and no statement links, and none of the 16 MS Joint Brokerage rows carried a `transfer_id`, so nothing downstream was anchored to the wrong dates.

Post-fix, `find_transfer_pairs(38, 2)` returns 4 **Strong** pairs — the $12,000 transfer plus three sub-dollar E*TRADE deposits the date skew had also been hiding.

## Consequences

- US and UK generic CSVs both import correctly without the user knowing what `strptime` is. A file with any day-13+ row is settled by the file; only an all-ambiguous column falls back to the day-first default, and it says so in the log.
- Both import paths now read the CSV into a list before parsing (one extra pass, and the rows are held in memory). These are user-selected files, not streams; the largest here is 13,550 rows.
- The mapping dialog's preview and the committed import can no longer disagree about dates — previously they could, since the preview inferred from 5 rows and the import from none.
- The explicit `date_format` combo remains the escape hatch, and still wins over inference. When an explicit pattern fails on a given row, the fallback is now the *inferred* parser rather than a fresh coin-flip.
- **Any generic CSV imported before this fix may carry transposed dates.** The `csv-generic` batches are enumerable via `import_batch`, and the `stored_day ≤ 12` rule identifies the affected rows exactly, so the repair above is repeatable if another such account turns up.
- `tests/test_csv_date_order.py` 15/15 (inference: month-first, day-first, undecidable, self-contradictory, ISO-casts-no-vote, dashed separators, junk rows; cell parsing honours the flag; column-bound parser; day-first fallback; end-to-end US keeps 2021-05-12, UK keeps day-first, one unambiguous row rescues the column, explicit format still overrides). Full suite 38/38.
