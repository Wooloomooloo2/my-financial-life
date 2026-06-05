"""Report-side helpers — pure-Python utilities that the report windows
use to roll Repository data up to the shape they display.

Keeps the Repository focused on SQL and lets the same query feed several
different aggregation rules (per-bucket totals, per-bucket-and-group
stacks, top-N summaries…) without a second round trip.
"""
from __future__ import annotations

from typing import Optional

from mfl_desktop.db.repository import CategoryNode


def category_group_map(nodes: list[CategoryNode]) -> dict[int, int]:
    """For each category id, return the report-group id.

    The "report group" is the user's mental high-level bucket:

    - A top-level category (parent_id is None) is its own group — covers
      both the seeded roots (Income / Expense / Transfer / Uncategorised)
      and any user/import-created top-level rows.
    - A sub-category's group is the **deepest ancestor that is a direct
      child of a root** — i.e. the second level of the tree. So
      ``Expense → Groceries → Tesco`` rolls up to ``Groceries``, and
      ``Expense → Auto → Petrol`` rolls up to ``Auto``.

    This matches how Banktivity-style users read their spending: by the
    natural "budget line" (Groceries, Auto, Housing…), not by the leaf
    they happened to use for an individual transaction.
    """
    by_id = {c.id: c for c in nodes}
    result: dict[int, int] = {}
    for c in nodes:
        result[c.id] = _group_for(c, by_id)
    return result


def _group_for(
    node: CategoryNode, by_id: dict[int, CategoryNode],
) -> int:
    """Walk up the tree until we find a node whose parent is a root (or
    None). That node is the report group."""
    current = node
    # Guard against degenerate input (shouldn't happen given ADR-013's
    # cycle prevention but defensive).
    seen: set[int] = set()
    while current.parent_id is not None and current.id not in seen:
        seen.add(current.id)
        parent = by_id.get(current.parent_id)
        if parent is None or parent.parent_id is None:
            # `parent` is a root (or missing) — `current` is the group.
            break
        current = parent
    return current.id


def group_label(
    group_id: int, nodes_by_id: dict[int, CategoryNode],
) -> str:
    """Display name for a group id — just the category's own name. Used
    in the chart legend and the filter list."""
    node = nodes_by_id.get(group_id)
    return node.name if node else f"id={group_id}"
