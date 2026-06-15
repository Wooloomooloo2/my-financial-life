"""Pure auto-categorisation rule engine (ADR-073, Arc G round 2).

Dependency-free by design — it takes duck-typed rule objects carrying the six
columns (`pattern`, `pattern_kind`, `match_field`, `set_payee_id`,
`set_category_id`, `priority`), so the Repository's ``RuleRow`` is compatible
without this module importing the Repository (mirrors ``budget_calc`` /
``goal_calc``). The Repository and the import service depend on *this*; this
depends on nothing in the app.

A rule matches a transaction's raw payee text or memo with one of four
matcher kinds and, on a match, contributes a payee and/or a category. The
matchers and fields are spelled out as constants so the UI and the engine
can never drift.
"""
from __future__ import annotations

from typing import Optional

# Matcher kinds (rule.pattern_kind) — friendly labels for the UI in the dict.
MATCHER_KINDS: dict[str, str] = {
    "contains": "contains",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "is_exactly": "is exactly",
}

# Fields a rule can match against (rule.match_field) → UI label.
MATCH_FIELDS: dict[str, str] = {
    "payee_raw": "Payee text",
    "memo": "Memo",
}


def rule_matches(rule, payee_text: str, memo: str) -> bool:
    """True when ``rule`` matches the given payee text / memo.

    Matching is case-insensitive and trims surrounding whitespace on both
    sides. An empty pattern never matches (defensive — the UI rejects it).
    """
    field = payee_text if rule.match_field == "payee_raw" else memo
    hay = (field or "").strip().lower()
    needle = (rule.pattern or "").strip().lower()
    if not needle:
        return False
    kind = rule.pattern_kind
    if kind == "contains":
        return needle in hay
    if kind == "starts_with":
        return hay.startswith(needle)
    if kind == "ends_with":
        return hay.endswith(needle)
    if kind == "is_exactly":
        return hay == needle
    return False


def apply_rules(
    rules, payee_text: str, memo: str,
) -> tuple[Optional[int], Optional[int]]:
    """Resolve ``(set_payee_id, set_category_id)`` for one transaction.

    Rules are evaluated in **priority order** (ascending = highest priority
    first; ties broken by id for stability). Each output field is filled from
    the **first matching rule that sets it** — payee and category are resolved
    independently, so a payee-only rule and a category-only rule compose.
    Returns ``(None, None)`` when nothing matches.
    """
    payee_id: Optional[int] = None
    category_id: Optional[int] = None
    for rule in sorted(rules, key=lambda r: (r.priority, r.id)):
        if not rule_matches(rule, payee_text, memo):
            continue
        if payee_id is None and rule.set_payee_id is not None:
            payee_id = rule.set_payee_id
        if category_id is None and rule.set_category_id is not None:
            category_id = rule.set_category_id
        if payee_id is not None and category_id is not None:
            break
    return payee_id, category_id
