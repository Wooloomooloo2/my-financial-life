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

# Period preset key sets are owned by mfl_desktop.periods (ADR-082, single
# source of truth). Re-aliased to the historical names here so the report
# dialogs/windows that import them keep working unchanged.
from mfl_desktop.periods import (  # noqa: F401
    REPORT_PRESETS as SPENDING_PERIOD_KEYS,
    INVESTMENT_PRESETS as INVESTMENT_RETURNS_PERIOD_KEYS,
    SANKEY_PRESETS as SANKEY_PERIOD_KEYS,
)

# Discriminator strings used as the ``report.type`` enum values and as the
# keys in ``FILTER_DEFAULTS`` / ``filters_from_json``. Mirrors the CHECK
# constraint in 0010_reports.sql.
TYPE_SPENDING_OVER_TIME = "spending_over_time"
TYPE_NET_WORTH = "net_worth"
TYPE_INCOME_EXPENSE = "income_expense"
TYPE_SANKEY = "sankey"
TYPE_INVESTMENT_RETURNS = "investment_returns"
TYPE_PAYEE = "payee"
TYPE_CATEGORY_PAYEE = "category_payee"

REPORT_TYPES: tuple[str, ...] = (
    TYPE_SPENDING_OVER_TIME,
    TYPE_NET_WORTH,
    TYPE_INCOME_EXPENSE,
    TYPE_SANKEY,
    TYPE_INVESTMENT_RETURNS,
    TYPE_PAYEE,
    TYPE_CATEGORY_PAYEE,
)

REPORT_TYPE_LABELS: dict[str, str] = {
    TYPE_SPENDING_OVER_TIME: "Spending Over Time",
    TYPE_NET_WORTH:          "Net Worth",
    TYPE_INCOME_EXPENSE:     "Income & Expense",
    TYPE_SANKEY:             "Sankey",
    TYPE_INVESTMENT_RETURNS: "Investment Returns",
    TYPE_PAYEE:              "Payee",
    TYPE_CATEGORY_PAYEE:     "Category & Payee",
}


# ── Spending Over Time ──────────────────────────────────────────────────────

# SPENDING_PERIOD_KEYS is imported at the top from mfl_desktop.periods
# (= REPORT_PRESETS). "custom" is the escape hatch using custom_start/custom_end.

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

    # Saved splitter sizes (ADR-076): chart-over-table + content-vs-summary.
    chart_split: tuple[int, ...] = field(default_factory=tuple)
    body_split: tuple[int, ...] = field(default_factory=tuple)
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


# ── Income & Expense (ADR-064 / Arc E, E1) ──────────────────────────────────

# Reuses the Spending period + granularity vocabulary so the report family
# stays consistent (SPENDING_PERIOD_KEYS / SPENDING_GRANULARITIES above).
# The display currency is NOT persisted here — like Net Worth (ADR-055) and
# Sankey (ADR-056) it's a view preference re-resolved each time the report
# opens.


@dataclass(frozen=True)
class IncomeExpenseFilters:
    """Persisted filter set for a saved Income & Expense report.

    Income vs expense is decided by **category kind** (not raw sign) — the
    aggregation is fixed in SQL, so there's no kind toggle here. Empty
    ``account_ids`` means "all accounts" (same convention as the other
    report filters). Default period is the trailing 12 months — the
    natural cash-flow horizon (and matching the register's 12-month
    default from Arc A / A1).
    """

    period_key: str = "1y"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    granularity: str = "auto"
    account_ids: tuple[int, ...] = field(default_factory=tuple)
    # Transfers between the owner's own accounts are neither income nor
    # expense. ``kind='transfer'`` categories are always excluded by the
    # kind rule; this additionally drops anything carrying a ``transfer_id``
    # (a linked transfer pair) regardless of the category it was filed
    # under. Default False = exclude (the cash-flow-correct default).
    include_transfers: bool = False

    # Saved splitter sizes (ADR-076): chart-over-table + content-vs-summary.
    chart_split: tuple[int, ...] = field(default_factory=tuple)
    body_split: tuple[int, ...] = field(default_factory=tuple)
    # ── round-trip helpers ──

    @classmethod
    def default(cls) -> "IncomeExpenseFilters":
        return cls()

    def to_json(self) -> str:
        return json.dumps(_asdict_for_json(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "IncomeExpenseFilters":
        raw = json.loads(blob) if blob else {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"IncomeExpenseFilters: expected JSON object, got "
                f"{type(raw).__name__}"
            )
        return _from_dict(cls, raw)


# ── Investment Returns (ADR-046) ────────────────────────────────────────────

# INVESTMENT_RETURNS_PERIOD_KEYS is imported at the top from mfl_desktop.periods
# (= INVESTMENT_PRESETS). "max" = first transaction → today (lifetime), which the
# spending presets don't offer; "custom" uses custom_start / custom_end.


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

    # Saved splitter sizes (ADR-076): chart-over-table + content-vs-summary.
    chart_split: tuple[int, ...] = field(default_factory=tuple)
    body_split: tuple[int, ...] = field(default_factory=tuple)
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

# Income → Total → Expenses flow. SANKEY_PERIOD_KEYS is imported at the top from
# mfl_desktop.periods (= SANKEY_PRESETS) — finance-native and distinct from the
# spending presets: month-to-date and last-month matter for a cash-flow view.
SANKEY_VALUE_MODES: tuple[str, ...] = ("amount", "percent")


@dataclass(frozen=True)
class SankeyFilters:
    """Persisted filter set for a saved Sankey report (ADR-056).

    ``depth`` is how many category levels deep the diagram expands (1 = top
    level only). ``threshold_pct`` folds any node worth less than that % of the
    side's total into a single "Other" node (0 = show everything). ``value_mode``
    toggles labels between absolute amounts and a % of total. Income vs expense
    is read from ``category.kind`` (transfers are excluded).

    ``account_ids`` / ``category_ids`` narrow which transactions feed the
    diagram. Empty tuples mean "all" (the same convention as the other report
    filters) — no narrowing, and the saved blob stays terse. ``category_ids``
    holds the leaf/own categories whose transactions count; the report's roll-up
    naturally excludes any descendant left out of the set.
    """

    period_key: str = "ytd"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    depth: int = 2
    threshold_pct: float = 0.0
    value_mode: str = "amount"
    account_ids: tuple[int, ...] = field(default_factory=tuple)
    category_ids: tuple[int, ...] = field(default_factory=tuple)

    # Saved splitter sizes (ADR-076): chart-over-table + content-vs-summary.
    chart_split: tuple[int, ...] = field(default_factory=tuple)
    body_split: tuple[int, ...] = field(default_factory=tuple)
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


# ── Payee (ADR-066 / Arc E, E2) ─────────────────────────────────────────────

# Default count of payees to show before the long tail folds into a single
# "Other" row. 0 means "show every payee, no fold". Reuses the spending
# period vocabulary (SPENDING_PERIOD_KEYS) so the report family stays
# consistent. There is no granularity here — the Payee report is a ranked
# snapshot over the whole period, not a time-bucketed series.
PAYEE_DEFAULT_TOP_N = 15


@dataclass(frozen=True)
class PayeeReportFilters:
    """Persisted filter set for a saved Payee report.

    The report ranks **spending** (strict outflow — ``kind='expense'`` and
    ``amount < 0``, the same definition as Spending Over Time) per payee,
    rolling aliases up to their canonical payee (ADR-028/029). Empty
    ``account_ids`` means "all accounts" (the shared report-filter
    convention). ``top_n`` caps how many payees the chart/table show before
    the remainder collapses into a single "Other" row (0 = show all).

    Like the other reports, transfers between the owner's own accounts are
    neither spending nor income; ``kind='transfer'`` categories are always
    excluded by the kind rule, and ``include_transfers=False`` (the default)
    additionally drops anything carrying a ``transfer_id`` (a linked
    transfer leg). The display currency is a top-bar view preference, not
    persisted here (matching Net Worth / Sankey / Income & Expense).
    """

    period_key: str = "1y"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    account_ids: tuple[int, ...] = field(default_factory=tuple)
    top_n: int = PAYEE_DEFAULT_TOP_N
    include_transfers: bool = False
    # Saved splitter sizes (ADR-076 follow-up): chart-over-table and
    # content-vs-summary. Empty = use the window's defaults.
    chart_split: tuple[int, ...] = field(default_factory=tuple)
    body_split: tuple[int, ...] = field(default_factory=tuple)

    # ── round-trip helpers ──

    @classmethod
    def default(cls) -> "PayeeReportFilters":
        return cls()

    def to_json(self) -> str:
        return json.dumps(_asdict_for_json(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "PayeeReportFilters":
        raw = json.loads(blob) if blob else {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"PayeeReportFilters: expected JSON object, got "
                f"{type(raw).__name__}"
            )
        return _from_dict(cls, raw)


# ── Category & Payee (ADR-068 / Arc E, E3) ──────────────────────────────────

# The two-level drill report's primary dimension. The report ranks one
# dimension at level 1 and drills into the other at level 2. Reuses the
# spending period vocabulary + the payee top-N default.
CATEGORY_PAYEE_GROUP_BY: tuple[str, ...] = ("category", "payee")


@dataclass(frozen=True)
class CategoryPayeeFilters:
    """Persisted filter set for a saved Category & Payee report.

    The report ranks **spending** (strict outflow — same definition as
    Spending Over Time / Payee) cross-cut by category and payee. ``group_by``
    is the *primary* dimension shown at level 1 (the other is the level-2
    drill); it's a saved preference but a toggle flips it live. The category
    dimension is the **budget-line level** (``category_group_map`` — Groceries,
    Transport…), and payees roll up to their canonical (ADR-028/029).

    Empty ``account_ids`` means all accounts. ``top_n`` caps rows per level
    (0 = all; tail omitted with a hidden-count note, like the Payee report).
    Transfers excluded by default with ``include_transfers``. The display
    currency and the live drill state are view-only, not persisted.
    """

    period_key: str = "1y"
    custom_start: Optional[str] = None     # ISO date, only when period_key == "custom"
    custom_end:   Optional[str] = None
    account_ids: tuple[int, ...] = field(default_factory=tuple)
    group_by: str = "category"             # primary dimension at level 1
    top_n: int = 15
    include_transfers: bool = False

    # Saved splitter sizes (ADR-076): chart-over-table + content-vs-summary.
    chart_split: tuple[int, ...] = field(default_factory=tuple)
    body_split: tuple[int, ...] = field(default_factory=tuple)
    # ── round-trip helpers ──

    @classmethod
    def default(cls) -> "CategoryPayeeFilters":
        return cls()

    def to_json(self) -> str:
        return json.dumps(_asdict_for_json(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "CategoryPayeeFilters":
        raw = json.loads(blob) if blob else {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"CategoryPayeeFilters: expected JSON object, got "
                f"{type(raw).__name__}"
            )
        return _from_dict(cls, raw)


# ── Dispatch ────────────────────────────────────────────────────────────────

# Maps the report.type enum value to its filter dataclass. Adding a new
# type later means a new entry here + a new dataclass above.
_FILTER_CLASSES: dict[str, type] = {
    TYPE_SPENDING_OVER_TIME: SpendingOverTimeFilters,
    TYPE_INCOME_EXPENSE: IncomeExpenseFilters,
    TYPE_INVESTMENT_RETURNS: InvestmentReturnsFilters,
    TYPE_SANKEY: SankeyFilters,
    TYPE_PAYEE: PayeeReportFilters,
    TYPE_CATEGORY_PAYEE: CategoryPayeeFilters,
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
