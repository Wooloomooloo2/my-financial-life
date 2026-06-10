"""Per-type filter dataclasses for saved reports (ADR-039).

Each report ``type`` enum value has a matching frozen dataclass here with
``to_json()`` / ``from_json(s)`` / ``default()`` round-trip helpers. The
``report.filters_json`` column stores the per-type blob; the dataclass
defines the in-code shape.

JSON-shape changes inside a type are migrated by :func:`migrate_filters`
at load time, so they don't require a DB migration — the upgraded blob
is re-saved on the next ``Save``. Add new fields with backward-compatible
defaults wherever possible; if a field has no sensible default, bump the
``_version`` integer and add a branch to :func:`migrate_filters`.

Round 1 (this commit) ships ``SpendingOverTimeFilters`` only. Net Worth,
Income & Expense, and Sankey land in subsequent ADRs with their own
dataclasses here.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Optional

# Discriminator strings used as the ``report.type`` enum values and as the
# keys in ``FILTER_DEFAULTS`` / ``filters_from_json``. Mirrors the CHECK
# constraint in 0010_reports.sql.
TYPE_SPENDING_OVER_TIME = "spending_over_time"
TYPE_NET_WORTH = "net_worth"
TYPE_INCOME_EXPENSE = "income_expense"
TYPE_SANKEY = "sankey"
TYPE_INVESTMENT_RETURNS = "investment_returns"

REPORT_TYPES: tuple[str, ...] = (
    TYPE_SPENDING_OVER_TIME,
    TYPE_NET_WORTH,
    TYPE_INCOME_EXPENSE,
    TYPE_SANKEY,
    TYPE_INVESTMENT_RETURNS,
)

REPORT_TYPE_LABELS: dict[str, str] = {
    TYPE_SPENDING_OVER_TIME: "Spending Over Time",
    TYPE_NET_WORTH:          "Net Worth",
    TYPE_INCOME_EXPENSE:     "Income & Expense",
    TYPE_SANKEY:             "Sankey",
    TYPE_INVESTMENT_RETURNS: "Investment Returns",
}


# ── Spending Over Time ──────────────────────────────────────────────────────

# Period preset keys (mirrors mfl_desktop.account_summary.PERIOD_KEYS so the
# vocabulary is consistent across windows). "custom" is the escape hatch
# that uses custom_start / custom_end.
SPENDING_PERIOD_KEYS: tuple[str, ...] = (
    "quarter", "6m", "ytd", "1y", "3y", "custom",
)

# Granularity values stored in the blob. "auto" picks a sensible bucket
# size based on the date span — the window resolves it before calling the
# Repository. The other four map directly to the SQL bucket modes.
SPENDING_GRANULARITIES: tuple[str, ...] = (
    "auto", "weekly", "monthly", "quarterly", "annually",
)

# Rollup levels per ADR-030.
SPENDING_ROLLUP_LEVELS: tuple[str, ...] = ("top", "group", "leaf")


@dataclass(frozen=True)
class SpendingOverTimeFilters:
    """Persisted filter set for a saved Spending Over Time report.

    Empty tuples for the id-list fields mean "all" rather than "none" —
    this matches the natural UX expectation (no filter = show everything)
    and lets the saved blob stay terse when the user hasn't narrowed
    anything down. ``include_uncategorised`` is the one independent toggle
    on top of ``category_ids`` because the Uncategorised bucket has its
    own checkbox in the report UI (it isn't in the checklist).
    """

    period_key: str = "quarter"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    granularity: str = "auto"
    rollup_level: str = "top"
    category_ids: tuple[int, ...] = field(default_factory=tuple)
    include_uncategorised: bool = True
    payee_ids: tuple[int, ...] = field(default_factory=tuple)
    account_ids: tuple[int, ...] = field(default_factory=tuple)
    include_transfers: bool = False        # ADR-018 strict-outflow default

    # ── round-trip helpers ──

    @classmethod
    def default(cls) -> "SpendingOverTimeFilters":
        return cls()

    def to_json(self) -> str:
        return json.dumps(_asdict_for_json(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "SpendingOverTimeFilters":
        raw = json.loads(blob) if blob else {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"SpendingOverTimeFilters: expected JSON object, got {type(raw).__name__}"
            )
        return _from_dict(cls, raw)


# ── Investment Returns (ADR-046) ────────────────────────────────────────────

# Investment-native period presets. "max" = first transaction → today
# (lifetime), which the spending presets don't offer; "custom" uses
# custom_start / custom_end. Resolved to date bounds by the report window.
INVESTMENT_RETURNS_PERIOD_KEYS: tuple[str, ...] = (
    "ytd", "1y", "3y", "5y", "max", "custom",
)


@dataclass(frozen=True)
class InvestmentReturnsFilters:
    """Persisted filter set for a saved Investment Returns report (ADR-046).

    Empty id-tuples mean "all" (same convention as SpendingOverTimeFilters):
    no account filter = every investment account (the whole portfolio), no
    security filter = every security held in the selected accounts. Realized
    gains and dividends are period-scoped to ``period_key`` by the compute
    engine; unrealized gain is the lifetime gain of currently-held positions.
    """

    period_key: str = "max"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    account_ids: tuple[int, ...] = field(default_factory=tuple)
    security_ids: tuple[int, ...] = field(default_factory=tuple)

    # ── round-trip helpers ──

    @classmethod
    def default(cls) -> "InvestmentReturnsFilters":
        return cls()

    def to_json(self) -> str:
        return json.dumps(_asdict_for_json(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "InvestmentReturnsFilters":
        raw = json.loads(blob) if blob else {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"InvestmentReturnsFilters: expected JSON object, got "
                f"{type(raw).__name__}"
            )
        return _from_dict(cls, raw)


# ── Sankey (ADR-056) ────────────────────────────────────────────────────────

# Income → Total → Expenses flow. Period presets are finance-native and
# distinct from the spending presets: month-to-date and last-month matter for a
# cash-flow view. "custom" uses custom_start / custom_end.
SANKEY_PERIOD_KEYS: tuple[str, ...] = ("ytd", "mtd", "last_month", "custom")
SANKEY_VALUE_MODES: tuple[str, ...] = ("amount", "percent")


@dataclass(frozen=True)
class SankeyFilters:
    """Persisted filter set for a saved Sankey report (ADR-056).

    ``depth`` is how many category levels deep the diagram expands (1 = top
    level only). ``threshold_pct`` folds any node worth less than that % of the
    side's total into a single "Other" node (0 = show everything). ``value_mode``
    toggles labels between absolute amounts and a % of total. Income vs expense
    is read from ``category.kind`` (transfers are excluded).
    """

    period_key: str = "ytd"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    depth: int = 2
    threshold_pct: float = 0.0
    value_mode: str = "amount"

    # ── round-trip helpers ──

    @classmethod
    def default(cls) -> "SankeyFilters":
        return cls()

    def to_json(self) -> str:
        return json.dumps(_asdict_for_json(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "SankeyFilters":
        raw = json.loads(blob) if blob else {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"SankeyFilters: expected JSON object, got {type(raw).__name__}"
            )
        return _from_dict(cls, raw)


# ── Dispatch ────────────────────────────────────────────────────────────────

# Maps the report.type enum value to its filter dataclass. Adding a new
# type later means a new entry here + a new dataclass above.
_FILTER_CLASSES: dict[str, type] = {
    TYPE_SPENDING_OVER_TIME: SpendingOverTimeFilters,
    TYPE_INVESTMENT_RETURNS: InvestmentReturnsFilters,
    TYPE_SANKEY: SankeyFilters,
}


def default_filters(report_type: str):
    """Construct a default filter dataclass for the given report type."""
    cls = _FILTER_CLASSES.get(report_type)
    if cls is None:
        raise ValueError(
            f"No filter dataclass registered for report type {report_type!r}. "
            f"Add it to mfl_desktop/reports/filters.py before adding the "
            f"enum value to migration 0010_reports.sql."
        )
    return cls.default()


def filters_from_json(report_type: str, blob: str):
    """Parse a stored filters_json blob into its type-specific dataclass."""
    cls = _FILTER_CLASSES.get(report_type)
    if cls is None:
        raise ValueError(
            f"No filter dataclass registered for report type {report_type!r}."
        )
    return cls.from_json(blob)


def filters_to_json(filters_obj) -> str:
    """Serialise any filter dataclass back to its JSON blob. The dataclass
    knows its own field set, so this is just a polymorphism shim around
    ``.to_json()``."""
    return filters_obj.to_json()


def migrate_filters(report_type: str, blob: str) -> str:
    """Upgrade a stored filter blob to the current shape for its type.

    Today every type's dataclass tolerates missing fields via defaults, so
    the migration is a parse-and-reserialise round trip — that's enough
    to upgrade old blobs that lack a newly-added field, and to drop fields
    that no longer exist. When a future field needs a non-default
    migration (e.g. renaming, type change) add a branch here keyed on
    ``report_type``.
    """
    try:
        parsed = filters_from_json(report_type, blob)
    except (ValueError, json.JSONDecodeError):
        # Unparseable blob — fall back to defaults rather than losing the
        # row. The next save persists the canonical shape.
        parsed = default_filters(report_type)
    return filters_to_json(parsed)


# ── internal helpers ────────────────────────────────────────────────────────


def _asdict_for_json(obj) -> dict:
    """Convert a frozen filter dataclass to a JSON-friendly dict.

    Tuples become lists (json has no tuples). Everything else passes
    through ``asdict``'s default behaviour.
    """
    raw = asdict(obj)
    for f in fields(obj):
        val = raw.get(f.name)
        if isinstance(val, tuple):
            raw[f.name] = list(val)
    return raw


def _from_dict(cls, raw: dict):
    """Construct a frozen dataclass from a dict, coercing JSON lists back
    to tuples for fields annotated as ``tuple[...]``.

    Unknown keys are ignored (forward-compat); missing keys fall back to
    the dataclass default so old blobs auto-upgrade. Type coercion is
    intentionally minimal — JSON ints stay ints, strings stay strings;
    only the list→tuple step needs special handling because the dataclass
    is frozen and tuple-typed.
    """
    kwargs: dict = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        val = raw[f.name]
        if isinstance(val, list):
            val = tuple(val)
        kwargs[f.name] = val
    # Build via the constructor so frozen-dataclass invariants apply.
    return cls(**kwargs)
