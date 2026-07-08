# ADR-146 — Cash Flow (Sankey) report: fold transfers in, and choose which

**Date:** 2026-07-08
**Status:** Implemented
**Related:** ADR-056 (Sankey / Cash Flow report + `sankey_category_totals`). ADR-140 (Income & Expense — choose which transfers to fold in; the pattern this mirrors). ADR-129 (net-of-refunds expense). ADR-051 (`txn_category_line` split-unroll view). ADR-084 (`ReportFilterDialogBase`, the shared "Include transfers" checkbox).

## Context

The Sankey (Cash Flow) report reads income vs expense from `category.kind` and **excluded transfers entirely** — a transfer between the owner's own accounts is neither income nor expense, so by default it has no place in a cash-flow diagram.

But the owner wants the same **ROI / directional cash-flow** view the Income & Expense report already offers (ADR-140): when the report is scoped to one operating account, a transfer's near leg (e.g. a mortgage-principal payment *out*, or a savings contribution *out*) is a real outflow of that account and should show on the diagram. The counterpart leg lives on the other account, out of scope, so it doesn't double back as phantom income.

`sankey_category_totals` — shared by the Sankey report **and** the Income & Expense composition donut — already grew `include_transfers` / `transfer_category_ids` in ADR-140 (defaulted off, so the Sankey report was untouched). Only the Sankey window never passed them or exposed UI.

## Decision

Give the Sankey report the same **"Include transfers" + which-transfer-categories** control as Income & Expense, wired to the parameters `sankey_category_totals` already supports.

- **Filters**: `SankeyFilters` gains `include_transfers: bool = False` and `transfer_category_ids: tuple[int, ...] = ()`. Both default off, so existing saved reports and the default view are byte-for-byte unchanged; old saved blobs (missing the fields) auto-default off via the generic `_from_dict`.
- **Directional treatment** (as ADR-140): a transfer *outflow* (`amount < 0`) counts on the **expense** side, an *inflow* on the **income** side, keyed by the transfer's own category so it rolls up as its own slice. Transfer legs are single-sign per direction (always a positive magnitude), never hitting the ADR-129 £0 net-refund floor.
- **Node rendering**: transfer categories are `kind='transfer'`, so the existing `_build_side` (which roots + rolls up by a single kind) would leave them in the side *total* but unrendered. `_refresh` now **partitions** each side's aggregate into its own-kind bucket vs the folded-in transfer bucket, builds the income/expense nodes from the first, and — when transfers are on — builds the transfer categories as their own roots (via `_build_side("transfer", …)`) appended to the side their direction landed on. Partitioning by kind prevents both dropping a transfer slice and double-counting a (pathologically) nested one.
- **UI**: `SankeyFilterDialog` gains the shared "Include transfers" checkbox and a third checklist, **"Transfer categories (empty = all)"**, enabled only while the checkbox is ticked; `values()` now returns `(account_ids, category_ids, include_transfers, transfer_category_ids)`. The filter note reads "Filtered: … transfers (N categories)".

Rejected: a by-account transfer selection (the owner picks by *purpose*/category — "Mortgage Principal" — independent of how many accounts are involved, matching ADR-140); a separate "Transfers" band in the diagram (folding them into the existing income/expense flows is what makes the net read as cash flow); changing the default (off stays the cash-flow-correct default and keeps every saved report stable).

## Consequences

- Scoping the Cash Flow report to a rental's operating account and ticking "Mortgage Principal" now shows the principal transfer as its own expense-side slice alongside repairs and interest, with rental income on the left — the property's cash flow, matching the Income & Expense ROI view (both read `sankey_category_totals`).
- A transfer counts on both sides only when both its accounts are in scope; the dialog's tooltip spells this out.
- Minor: with a non-zero "Hide below" threshold set, small slices can fold into an "Other" node separately within the own-kind and transfer groups (a side could show two "Other"s). The threshold defaults to 0 (no fold), so this is an edge case only.
- No schema change or migration. `tests/test_sankey_transfers.py` 7/7 (default excludes; single-account scope shows just the near leg; both-accounts shows both; the picker narrows / empty = all; JSON round-trip + old-blob default off; the dialog panel enables with the checkbox and returns the selection; the window renders the transfer category as an expense node and drops it when toggled off). Neighbouring report suites unaffected.
