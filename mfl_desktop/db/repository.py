"""Repository — the only layer that touches SQL.

Hides the SQLite schema from service and UI code, and converts between
Decimal (interface) and INTEGER pence (storage) for currency amounts.

Transactional boundaries are the caller's responsibility: a service that
performs multiple writes wraps them in a try/except and calls commit() on
success or rollback() on failure.
"""
from __future__ import annotations

import calendar
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Iterable, Optional, Union

from mfl_desktop import txn_status
from mfl_desktop.account_types import AccountTypeSpec, by_key
from mfl_desktop.db.money import decimal_to_pence, pence_to_decimal
from mfl_desktop.db.schema import bootstrap
from mfl_desktop.rules_engine import rule_matches

# The one non-deletable category (ADR-010 §4). Seeded as id=1 in
# 0001_initial.sql; serves as the deletion sink for every other category.
UNCATEGORISED_ID = 1


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountSummary:
    id: int
    iri: str
    name: str
    type: str
    family: str
    currency: str
    is_liability: bool
    opening_balance: Decimal = Decimal("0.00")
    folder_id: Optional[int] = None
    credit_limit: Optional[Decimal] = None   # credit cards only (ADR-058 R4a)
    archived_at: Optional[str] = None        # set = account is closed (ADR-069)

    @property
    def is_closed(self) -> bool:
        """A closed (archived) account — kept for history but out of the
        sidebar's active list, Net Worth, and report pickers (ADR-069)."""
        return self.archived_at is not None


@dataclass(frozen=True)
class FolderSummary:
    """A sidebar account-folder row. `account_count` is the number of
    non-archived accounts that currently belong to this folder."""
    id: int
    name: str
    sort_order: int
    account_count: int


@dataclass(frozen=True)
class ManualMatch:
    """A manually-entered transaction matching a candidate import row."""
    id: int
    iri: str
    posted_date: str
    payee_raw: str


@dataclass(frozen=True)
class DedupeExisting:
    """An existing transaction offered as a cross-source duplicate target
    (ADR-085). ``is_manual`` (no import_hash) decides the resolution on
    confirm: merge into the placeholder vs skip the incoming row."""
    id: int
    posted_date: str
    amount_pence: int
    payee_name: str
    import_hash: Optional[str]
    is_manual: bool


@dataclass(frozen=True)
class MatchCandidate:
    """An existing transaction offered as a manual match target in the import
    review's 'Find a match' picker (ADR-151 Phase 2). Carries ``status`` so the
    picker can show it, and ``is_manual`` so confirm routes merge-vs-skip."""
    id: int
    posted_date: str
    amount_pence: int
    payee_name: str
    status: str
    is_manual: bool


@dataclass(frozen=True)
class TransactionRow:
    """A transaction joined with its payee + category + account names.

    Amount is Decimal (signed; negative = debit). Running balance is computed
    in date order at load time and is only meaningful in single-account view
    — `list_all_transactions` returns rows with running_balance = 0.

    `transfer_id`, when present, indicates this row is one half of a
    transfer (per ADR-020) — the partner shares the same value. Used by
    the register window to distinguish "already a transfer" from "could
    become a transfer" when the user picks a transfer-kind category.
    """
    id: int
    iri: str
    account_id: int
    account_name: str
    posted_date: str
    amount: Decimal
    payee_id: Optional[int]
    payee_name: str
    category_id: int
    category_name: str
    status: str
    memo: str
    running_balance: Decimal
    transfer_id: Optional[str] = None
    # `split_count` > 0 marks this row as a split transaction (ADR-051): the
    # parent keeps the full signed `amount`, while its category lines live in
    # `txn_split`. The register renders the Category cell as "—Split—" and makes
    # the row dialog-only when this is non-zero. `split_category_ids` is the set
    # of categories the lines use, so the register's category filter can surface
    # a split parent when the user filters by one of its line categories.
    split_count: int = 0
    split_category_ids: frozenset = frozenset()
    # Investment fields (ADR-043). `action` is None on an ordinary cash row;
    # set ('Buy'/'Sell'/'Div'/…) on an investment-account row. The rest are
    # populated only when relevant to the action (quantity/price for trades,
    # security for anything touching an instrument).
    action: Optional[str] = None
    security_id: Optional[int] = None
    security_name: str = ""
    security_symbol: str = ""
    quantity: Optional[float] = None
    price: Optional[float] = None
    # Brokerage fee (ADR-012/0012 `txn.commission`, stored as pence). Signed
    # `amount` already includes it (cost basis uses abs(amount), holdings.py);
    # surfaced here so the edit dialog can show + preserve it. Decimal or None.
    commission: Optional[Decimal] = None
    # Accrued interest paid at a bond purchase (ADR-093, `txn.accrued_interest`,
    # pence). Part of the cash (so `amount` includes it), but NOT part of cost
    # basis — the holdings engine subtracts it back out. None on every non-bond
    # row. Decimal or None.
    accrued_interest: Optional[Decimal] = None


@dataclass(frozen=True)
class SplitLine:
    """One category line of a split transaction (ADR-051). ``amount`` is a
    signed Decimal (same convention as ``TransactionRow.amount``); the lines of
    a split sum to the parent's total. ``id`` is the ``txn_split`` row id when
    read back, or ``None`` for a line being passed into a create/update.

    ``category_kind`` is the line category's kind ('income' / 'expense' /
    'transfer'); ``transfer_to_account_id`` is set when the line is a transfer
    (ADR-051 amendment) — the destination account the line moves money to,
    derived from the partner ``txn`` that shares the line's ``transfer_id``.
    Both default to the non-transfer case for lines being passed *into* a
    create/update (the dialog supplies the destination separately)."""
    category_id: int
    category_name: str
    memo: str
    amount: Decimal
    id: Optional[int] = None
    category_kind: str = "expense"
    transfer_to_account_id: Optional[int] = None


# A split line being passed *into* a create/update. Three-tuple
# (category_id, memo, amount) is a plain category line; the four-tuple form adds
# a transfer destination account (ADR-051 amendment) — when present, the line is
# a transfer and spawns a partner txn there. The 3-tuple form keeps the import
# path (Banktivity splits, never transfers) working unchanged.
SplitLineInput = Union[
    "tuple[int, Optional[str], Decimal]",
    "tuple[int, Optional[str], Decimal, Optional[int]]",
]


@dataclass(frozen=True)
class SecurityRow:
    """A row in the securities master (ADR-043). Referenced by `name`
    (the QIF `Y` field); `symbol` is the optional ticker (often blank in
    Banktivity exports).

    `earliest_txn_date` (ADR-049 amendment) is the buffered date of this
    security's first transaction — the floor a history backfill should fetch
    from, so we don't pull (and store) decades of prices from before the owner
    ever held it. ``None`` when the row was built by a caller that doesn't need
    it (only the pricing queries populate it)."""
    id: int
    iri: str
    name: str
    symbol: str
    type: str
    earliest_txn_date: Optional[str] = None
    # Instrument class + per-class metadata (ADR-093). Default to a plain equity
    # so every pre-093 construction site reads back exactly as before. The
    # value-math source of truth is ``price_multiplier`` (cash value of one unit
    # at price = 1: stock 1.0; bond face/100; option contract_size); the
    # descriptive fields drive it at entry time and feed display + future coupon
    # scheduling. Only the dialog-feeding queries (``list_securities`` /
    # ``list_securities_for_accounts`` / ``get_security``) populate them; the
    # Tiingo pricing queries leave them at the defaults (they don't need them).
    instrument_type: str = "stock"
    price_multiplier: float = 1.0
    face_value: Optional[float] = None
    coupon_rate: Optional[float] = None
    maturity_date: Optional[str] = None
    cusip: Optional[str] = None
    underlying_symbol: Optional[str] = None
    strike: Optional[float] = None
    expiry_date: Optional[str] = None
    option_type: Optional[str] = None
    contract_size: Optional[float] = None


@dataclass(frozen=True)
class PriceRow:
    """A security price point (ADR-044). `price` is a per-share quote (REAL),
    not pence — consistent with txn.price / lot.unit_cost."""
    security_id: int
    price_date: str
    price: float
    currency: str
    source: str


@dataclass(frozen=True)
class PayeeRow:
    """A payee with its current usage count and canonical/alias state.

    ADR-028 / ADR-029: a payee row is either *canonical* (``canonical_id``
    is ``None``) or an *alias* of another payee (``canonical_id`` set).
    Aliases route through their canonical for display and reporting but
    keep their own historical txn rows pointing at the alias's id.

    - ``usage_count``: for a canonical, this is the **rolled-up** count
      (own direct txns + every alias's direct txns). For an alias, just
      its own direct count. The Payees dialog uses this directly; callers
      that need the bare direct count can read ``direct_usage_count``.
    """
    id: int
    name: str
    usage_count: int
    canonical_id: Optional[int] = None
    canonical_name: Optional[str] = None
    direct_usage_count: int = 0
    # ADR-072: the payee's remembered auto-category. Stored on the canonical
    # row only (aliases route through their canonical), so this is populated
    # for canonicals and left None on aliases.
    default_category_id: Optional[int] = None


@dataclass(frozen=True)
class RuleRow:
    """One auto-categorisation rule (ADR-073). Matches a transaction's raw
    payee text or memo and sets a payee and/or a category at import.

    The display fields (``set_payee_name`` / ``set_category_path``) are
    resolved by the Repository for the management screen; the rest are the
    raw `rule` columns and are also what the pure ``rules_engine`` reads
    (duck-typed)."""
    id: int
    pattern: str
    pattern_kind: str          # contains | starts_with | ends_with | is_exactly
    match_field: str           # payee_raw | memo
    set_payee_id: Optional[int]
    set_category_id: Optional[int]
    priority: int
    set_payee_name: Optional[str] = None
    set_category_path: Optional[str] = None


CATEGORY_KINDS: tuple[str, ...] = ("income", "expense", "transfer")


@dataclass(frozen=True)
class CategoryNode:
    """One row from the category tree, used by the management UI.

    `usage_count` is the *direct* count of transactions referencing this
    category id; descendants' transactions are not aggregated here.

    `kind` is one of CATEGORY_KINDS (per ADR-014); reports interpret signed
    amounts against this — e.g. a positive amount on an expense category is
    treated as a refund.
    """
    id: int
    parent_id: Optional[int]
    name: str
    source: str
    kind: str
    usage_count: int
    archived: bool = False   # True = archived/hidden (ADR-070)


@dataclass(frozen=True)
class CategoryChoice:
    """A category as offered in the register's filter and edit combos."""
    id: int
    name: str
    parent_name: str   # '' if top-level — used to disambiguate sibling-name collisions
    source: str
    kind: str = "expense"
    path: str = ""     # full breadcrumb 'Root → Mid → Leaf'; '' falls back to `name` at display time


SCHEDULE_CADENCES: tuple[str, ...] = (
    "weekly", "biweekly", "monthly", "quarterly", "annual",
)


BUDGET_ROLES: tuple[str, ...] = ("bills", "saving", "discretionary")


BUDGET_ROLLOVER: tuple[str, ...] = ("none", "accumulate")


def budget_months(start_month: str, length: int) -> list[str]:
    """The list of 'YYYY-MM' months a budget spans, from ``start_month`` for
    ``length`` months (ADR-058). Pure helper — lives here (not budget_calc) so
    the Repository can use it without importing budget_calc (which imports the
    Repository)."""
    year = int(start_month[:4])
    month = int(start_month[5:7])
    out: list[str] = []
    for _ in range(max(0, length)):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


@dataclass(frozen=True)
class Budget:
    """A budget plan (ADR-058). Multiple per file; each carries a period
    (``start_month`` 'YYYY-MM' + ``length_months``, default 12 = Jan–Dec), an
    optional display ``currency`` (None = the file's base currency), and a
    ``funding_mode`` (ADR-138): ``'balances'`` seeds the pool from the perimeter
    accounts' balances (default); ``'income'`` seeds it from income into those
    accounts over the budget period."""
    id: int
    iri: str
    name: str
    start_month: str
    length_months: int
    currency: Optional[str]
    funding_mode: str = "balances"

    def months(self) -> list[str]:
        return budget_months(self.start_month, self.length_months)


@dataclass(frozen=True)
class BudgetLine:
    """One envelope in a budget (ADR-058) — a budgeted category with its role
    and auto-rollover policy. The per-month amounts live in
    ``budget_allocation``; actuals + carry are computed, never stored. Joined
    with the category's name / parent / kind so the matrix renders in one pass.
    """
    id: int
    budget_id: int
    category_id: int
    category_name: str
    category_parent_name: str   # '' if top-level
    category_kind: str          # income / expense / transfer
    role: str                   # bills / saving / discretionary
    rollover: str               # none / accumulate
    sort_order: int
    # ADR-094: when set, this envelope is a *bill* backed by a scheduled_txn —
    # the schedule owns the date/cadence/amount/account, and the burn-down
    # projects its occurrences (flattening once amount-matched as paid). NULL =
    # an ordinary envelope.
    scheduled_txn_id: Optional[int] = None


@dataclass(frozen=True)
class BudgetAllocation:
    """One editable matrix cell — the budgeted amount for a line in a month
    (ADR-058). Positive magnitude; sign at display time from ``category.kind``.
    """
    budget_line_id: int
    month: str                  # 'YYYY-MM'
    amount: Decimal


@dataclass(frozen=True)
class GoalAccountLink:
    """One account's contribution to a goal (ADR-058 R4c). ``share_bp`` is basis
    points (0..10000 = 0..100%) of the account's balance that counts toward the
    goal; ``baseline_balance`` is the account's **full** signed native balance at
    link creation — the share is applied at compute time, so changing a share
    never needs a re-baseline. ``start_date`` is when this link was created."""
    account_id: int
    share_bp: int
    baseline_balance: Decimal   # signed native balance at link creation
    start_date: str             # 'YYYY-MM-DD'


@dataclass(frozen=True)
class BudgetGoal:
    """A savings or pay-down goal in a budget (ADR-058 R4b/R4c). A goal spans one
    or more accounts (``accounts``), each contributing ``share_bp`` of its
    balance; the goal rolls them up — converting each to ``currency`` via the FX
    layer (ADR-055) — toward ``target_amount`` by ``target_date``.
    ``target_amount`` is a **signed** balance in ``currency`` (a card you owe
    £1,800 on is -1800.00; a £30,000 savings target is +30000.00). Per-account
    baselines live on the links; the required-monthly and progress figures are
    computed live in :mod:`mfl_desktop.goal_calc`, never stored."""
    id: int
    iri: str
    budget_id: int
    name: str
    kind: str                   # 'paydown' | 'savings'
    currency: str               # the goal's reporting currency
    target_amount: Decimal      # signed target balance in `currency`
    target_date: str            # 'YYYY-MM-DD'
    accounts: tuple["GoalAccountLink", ...] = ()


@dataclass(frozen=True)
class GoalAggregate:
    """A goal's account links rolled up + converted to its currency (ADR-058
    R4c). ``start`` / ``current`` are signed Decimals in the goal currency;
    ``excluded`` names accounts that couldn't be converted (no FX rate — excluded
    per ADR-055, never par-added)."""
    start: Decimal
    current: Decimal
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class PerimeterTxn:
    """A transaction inside a budget's perimeter window, after the intra-
    perimeter-transfer cancellation rule (ADR-024 §transfers). Used by
    ``budget_calc`` to bucket actuals against budgeted categories."""
    id: int
    account_id: int
    posted_date: str
    amount: Decimal      # signed
    category_id: int


@dataclass(frozen=True)
class ScheduledTxnRow:
    """A scheduled transaction joined with its account, payee, category, and
    optional transfer-destination names. Used by the Schedules dialog list view
    and (in round B) by the budget screen to project planned spending.

    `estimated_amount` is signed (matches `txn.amount`); `variable=1` means
    the schedule is a placeholder amount and the post path prompts for the
    real number. `category_kind` is denormalised onto the row so the dialog
    can branch (transfer-kind schedules need a destination account) without
    re-querying.
    """
    id: int
    iri: str
    account_id: int
    account_name: str
    payee_id: Optional[int]
    payee_name: str
    category_id: int
    category_name: str
    category_kind: str
    transfer_to_account_id: Optional[int]
    transfer_to_account_name: str   # '' when not a transfer
    estimated_amount: Decimal
    variable: bool
    memo: str
    cadence: str
    anchor_date: str
    next_due_date: str
    end_date: Optional[str]
    auto_post: bool
    notes: str
    archived_at: Optional[str]


@dataclass(frozen=True)
class AutoPostFailure:
    """One schedule the launch sweep tried to auto-post but couldn't (ADR-091).

    ``label`` is a human-readable identifier for the Schedules list
    ("Polestar 2 — Asset Depreciation (Myself)"); ``reason`` is the
    exception message from ``post_scheduled_txn`` (e.g. a destination-less
    transfer schedule, a missing FX rate). Carried out of ``auto_post_due``
    so the caller can tell the user instead of the failure vanishing.
    """
    schedule_id: int
    label: str
    reason: str


@dataclass(frozen=True)
class AutoPostResult:
    """Outcome of a launch-time auto-post sweep (ADR-091).

    ``posted`` is the txn ids materialised (source-side for transfers);
    ``failures`` is the schedules that raised and were skipped. Before
    ADR-091 the sweep returned only the posted ids and silently dropped
    failures, so a permanently-broken schedule (e.g. transfer-kind with no
    destination) looked like nothing was due, launch after launch.
    """
    posted: list[int] = field(default_factory=list)
    failures: list[AutoPostFailure] = field(default_factory=list)


# Provenance of a transfer's exchange rate (per ADR-035). 'derived' = back-
# derived from the two txn amounts (or rate=1.0 for same-currency).
# 'manual' = the user typed it. 'fx_rate' = looked up from the fx_rate table
# at posting time.
TRANSFER_RATE_SOURCES: tuple[str, ...] = ("derived", "manual", "fx_rate")


@dataclass(frozen=True)
class TransferRow:
    """The parent row for one transfer pair (ADR-035).

    ``iri`` matches the shared ``txn.transfer_id`` on the two half-rows.
    ``rate`` is the quote-per-base used at posting time — i.e.
    ``to_amount_magnitude = from_amount_magnitude * rate``. Same-currency
    transfers carry ``rate=Decimal('1')`` with ``rate_source='derived'``.
    The two txn amounts on either side remain the truth-of-money; this
    row is the truth-of-intent (what rate was used).
    """
    iri: str
    from_account_id: int
    to_account_id: int
    rate: Decimal
    rate_source: str
    created_at: str


# Strength bins used by both the single-flow matcher (ADR-036) and the
# reconcile dialog (ADR-037). Score thresholds live in transfer_reconcile.
TRANSFER_STRENGTHS: tuple[str, ...] = ("Strong", "Good", "Possible")


@dataclass(frozen=True)
class TransferCandidate:
    """A potential other-side txn that the matcher considered (ADR-036).

    Returned by ``Repository.find_transfer_candidates`` for the single-
    flow path (one source row, multiple maybe-partners). Score is
    informational ordering — the matcher never auto-decides; the user
    confirms via the picker / confirm dialog.

    ``amount`` is signed in the candidate's account currency.
    ``expected_amount`` is the magnitude (always positive) the matcher
    expected to find on the candidate side given the source's amount and
    the FX rate for the date (or |source.amount| for same-currency).
    """
    txn_id: int
    account_id: int
    account_name: str
    account_currency: str
    posted_date: str
    amount: Decimal
    payee_name: str
    category_id: int
    days_apart: int             # signed: positive = later than source
    amount_mismatch_pct: float
    currencies_match: bool
    expected_amount: Decimal
    score: int
    strength: str


@dataclass(frozen=True)
class TransferPair:
    """One greedy-matched pair across two accounts (ADR-037).

    Returned by ``Repository.find_transfer_pairs`` for the reconcile
    dialog. ``source_*`` is always the outflow side (negative-amount txn
    on its account); ``target_*`` is always the inflow side. ``days_apart``
    is the unsigned magnitude.

    ``implied_rate`` is the rate back-derived from the actual two amounts
    (``|target_amount| / |source_amount|``); ``spot_rate`` is what the FX
    table said for that day (or 1.0 for same-currency). The deviation
    surfaces in the dialog as an at-a-glance sanity check.
    """
    source_txn_id: int
    source_account_id: int
    source_amount: Decimal
    source_currency: str
    source_posted_date: str
    source_payee: str
    target_txn_id: int
    target_account_id: int
    target_amount: Decimal
    target_currency: str
    target_posted_date: str
    target_payee: str
    days_apart: int
    implied_rate: Optional[Decimal]
    spot_rate: Optional[Decimal]
    rate_deviation_pct: Optional[float]
    score: int
    strength: str
    # ADR-139: when a side is a *split line* rather than a whole txn, its
    # ``txn_split`` id is here and ``*_txn_id`` is the split's PARENT txn id.
    # ``*_split_memo`` is the line memo, for display. None = whole txn.
    source_split_id: Optional[int] = None
    source_split_memo: Optional[str] = None
    target_split_id: Optional[int] = None
    target_split_memo: Optional[str] = None


@dataclass(frozen=True)
class LinkExisting:
    """Bulk-edit / reconcile decision: link source to an existing
    candidate row (ADR-036). The candidate's category is rewritten to
    ``category_id`` at link time — that's the whole point.

    ADR-139: a side may be a split *line*; give its ``txn_split`` id in
    ``source_split_id`` / ``candidate_split_id`` (the ``*_txn_id`` then names
    the split's parent txn). None = that side is a whole txn."""
    source_txn_id: int
    candidate_txn_id: int
    category_id: int
    rate: Optional[Decimal] = None
    rate_source: Optional[str] = None
    source_split_id: Optional[int] = None
    candidate_split_id: Optional[int] = None


@dataclass(frozen=True)
class CreateNew:
    """Bulk-edit / reconcile decision: manufacture a fresh partner row
    on ``other_account_id`` (ADR-036). ``to_amount`` / ``rate`` /
    ``rate_source`` follow the same two-of-three rule as
    ``Repository.create_transfer``."""
    source_txn_id: int
    other_account_id: int
    category_id: int
    to_amount: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    rate_source: Optional[str] = None


# Union of the two decision shapes; consumed by
# ``Repository.bulk_match_or_create_transfers``.
BulkTransferDecision = LinkExisting | CreateNew


@dataclass(frozen=True)
class BulkTransferResult:
    """Summary of a bulk transfer operation. ``linked`` counts existing-
    row matches; ``created`` counts manufactured partner rows. The IRIs
    are returned in plan order so the caller can correlate to source rows
    if it needs to follow up (e.g. select-and-scroll-to in the register)."""
    linked: int
    created: int
    transfer_iris: list[str]


# ── Saved reports (ADR-039) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ReportFolderRow:
    """A sidebar Reports-section folder. Mirrors :class:`FolderSummary`'s
    shape — flat list, sort_order for explicit ordering, archived rows
    excluded from listings."""
    id: int
    iri: str
    name: str
    sort_order: int
    report_count: int


@dataclass(frozen=True)
class ReportRow:
    """A saved report instance (ADR-039).

    ``type`` discriminates the per-type filter schema in
    :mod:`mfl_desktop.reports.filters` (the ``filters_json`` blob is
    opaque to SQL — parsed at read time). ``folder_name`` is denormalised
    onto the row for sidebar rendering; ``None`` when the report sits at
    the Reports-section root.
    """
    id: int
    iri: str
    name: str
    type: str
    folder_id: Optional[int]
    folder_name: Optional[str]
    filters_json: str
    created_at: str


@dataclass(frozen=True)
class FeedAccount:
    """A bank-feed link: an MFL account ↔ a provider account (ADR-077).

    Provider credentials live in the ``setting`` table, not here; access
    tokens are never persisted. ``status`` tracks consent health
    ('linked' / 'expired' / 'error')."""
    id: int
    account_id: int
    provider: str
    external_account_id: str
    requisition_id: Optional[str]
    institution_id: Optional[str]
    institution_name: Optional[str]
    status: str
    last_synced_at: Optional[str]


@dataclass(frozen=True)
class StatementRow:
    """One reconciliation period for an account (ADR-040).

    ``status`` is ``'open'`` (in progress, resumable) or ``'reconciled'``
    (closed). There is no separate "with variance" status — a statement
    that closed with a residual, or one that drifted afterwards because a
    reconciled row's amount was edited or deleted, is detected live via
    ``residual``:

    - ``residual`` = (``ending_balance`` − ``starting_balance``) − net of the
      currently-linked (ticked) rows. This is the "Missing" figure on the
      check-off screen and the out-of-balance signal on the history list.
    - ``closing_variance`` is only the snapshot of ``residual`` taken at the
      moment of close, kept for reference.

    ``txn_count`` and ``residual`` are computed in the listing queries, so
    they reflect the current state of the linked transactions.
    """
    id: int
    iri: str
    account_id: int
    start_date: str
    end_date: str
    starting_balance: Decimal
    ending_balance: Decimal
    status: str
    closing_variance: Decimal
    notes: Optional[str]
    created_at: str
    reconciled_at: Optional[str]
    txn_count: int = 0
    residual: Decimal = Decimal("0.00")

    @property
    def change_in_balance(self) -> Decimal:
        return self.ending_balance - self.starting_balance

    @property
    def is_balanced(self) -> bool:
        """True when the linked rows tie the statement out (residual == 0)."""
        return self.residual == Decimal("0.00")

    @property
    def is_out_of_balance(self) -> bool:
        """A closed statement whose linked rows no longer tie out — shown as
        'Out of balance' on the history list."""
        return self.status == "reconciled" and not self.is_balanced


# ── Identifier helpers (ADR-006) ────────────────────────────────────────────


def _parse_split_cids(value: Optional[str]) -> frozenset:
    """Parse a GROUP_CONCAT(category_id) string ('2,3,7') into a frozenset of
    ints (ADR-051). Empty/None → empty set (the common no-splits case)."""
    if not value:
        return frozenset()
    return frozenset(int(x) for x in value.split(",") if x)


def new_transaction_iri() -> str:
    return f"mfl:Transaction_{uuid.uuid4().hex[:8]}"


def new_import_batch_iri() -> str:
    return f"mfl:ImportBatch_{uuid.uuid4().hex[:8]}"


def new_transfer_iri() -> str:
    return f"mfl:Transfer_{uuid.uuid4().hex[:8]}"


def new_scheduled_txn_iri() -> str:
    return f"mfl:Scheduled_{uuid.uuid4().hex[:8]}"


def new_budget_iri() -> str:
    return f"mfl:Budget_{uuid.uuid4().hex[:8]}"


def new_goal_iri() -> str:
    return f"mfl:Goal_{uuid.uuid4().hex[:8]}"


def new_report_iri() -> str:
    return f"mfl:Report_{uuid.uuid4().hex[:8]}"


def new_report_folder_iri() -> str:
    return f"mfl:ReportFolder_{uuid.uuid4().hex[:8]}"


def new_statement_iri() -> str:
    return f"mfl:Statement_{uuid.uuid4().hex[:8]}"


def new_security_iri() -> str:
    return f"mfl:Security_{uuid.uuid4().hex[:8]}"


def new_rule_iri() -> str:
    return f"mfl:Rule_{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Loan:
    """An amortizing loan's terms (ADR-095), 1:1 with its account. Money fields
    are Decimal (converted from pence). ``current_principal`` =
    ``original_amount − principal_paid`` — the balance the amortization schedule
    is projected from. ``payment`` is None when it should be calculated from the
    term."""
    account_id: int
    original_amount: Decimal
    principal_paid: Decimal
    interest_rate: float          # annual %, e.g. 5.5
    compounding: str              # daily / monthly / annually
    term_months: Optional[int]
    payment: Optional[Decimal]    # None = calculate from term
    extra_payment: Decimal
    start_date: str               # 'YYYY-MM-DD'
    payment_day: int
    track_mode: str               # split / whole
    interest_source: str          # loan / payment
    payment_account_id: Optional[int]
    interest_category_id: Optional[int]
    goal_id: Optional[int]

    @property
    def current_principal(self) -> Decimal:
        return self.original_amount - self.principal_paid


# ── Repository ──────────────────────────────────────────────────────────────


class Repository:
    # Sentinel for "argument not supplied" on partial-update methods, where
    # None is itself a meaningful value (e.g. clearing a credit limit). Defined
    # at the top of the class so it is in scope for every method's default args.
    _UNSET = object()

    def __init__(self, db_path: Path | str) -> None:
        db_path = Path(db_path)
        bootstrap(db_path)
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        # ADR-153: generation-keyed memo for compute_account_values, the single
        # most expensive derivation in the app and the one the UI recomputes
        # most often. See data_generation() for how staleness is detected.
        self._ext_gen = 0
        self._acct_values_gen: Optional[tuple] = None
        self._acct_values_cache: dict[tuple, dict[int, Decimal]] = {}

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── cache invalidation (ADR-153) ─────────────────────────────────────────

    def data_generation(self) -> tuple:
        """A cheap token that changes whenever the data behind a derived value
        may have moved. Callers use it to skip recomputing — or re-rendering —
        against a database that hasn't changed.

        It combines three sources, because no single one sees every writer:

        * ``total_changes`` — rows written through *this* connection. A plain
          attribute (~90ns, no SQL). Covers every edit made through the UI.
        * ``PRAGMA data_version`` — commits made by another *process* (~12µs).
        * ``_ext_gen`` — bumped by hand via :meth:`note_external_change` for the
          background threads that write through their own ``Repository`` on
          their own connection (the launch and toolbar price/FX refreshes,
          ADR-035/044/116).

        That third source is the one that matters, and it is not redundant.
        ``data_version`` is *documented* to observe other connections' commits,
        and in one probe it did observe a sibling connection in our own process —
        but in another it did not, and I could not make the difference
        deterministic. So the contract is explicit notification; ``data_version``
        is a cheap backstop, not the guarantee. **Anything that writes through
        its own connection must call** :meth:`note_external_change` **on the main
        thread when it lands**, or callers will hold stale derived values.
        """
        return (
            self._conn.total_changes,
            self._conn.execute("PRAGMA data_version").fetchone()[0],
            self._ext_gen,
        )

    def note_external_change(self) -> None:
        """Declare that a background thread wrote through its own connection, so
        anything keyed on :meth:`data_generation` must recompute. Call on the
        main thread when the worker's result lands (ADR-153)."""
        self._ext_gen += 1

    @property
    def total_writes(self) -> int:
        """Rows written through this connection since it was opened. Lets a
        background worker tell whether its pass actually changed anything, so it
        only asks the main thread to invalidate when there is something to see
        (ADR-153)."""
        return self._conn.total_changes

    def save_copy(self, dest_path: Path | str) -> None:
        """Atomic online backup of the current database to dest_path.

        Uses SQLite's built-in backup API which is WAL-safe (no need to
        checkpoint first) and atomic — the destination file is either the
        full snapshot or absent. Overwrites the destination if it exists.
        Working DB is untouched."""
        dest_path = Path(dest_path)
        dest = sqlite3.connect(dest_path)
        try:
            self._conn.backup(dest)
        finally:
            dest.close()

    def checkpoint(self) -> None:
        """Fold the WAL back into the main database file (TRUNCATE mode).

        In WAL mode (see ``__init__``) committed writes accumulate in the
        ``<db>-wal`` sidecar until a checkpoint. Running this on clean close
        makes the single ``.mfl`` file self-contained, so copying or backing up
        just that one file never loses recent edits (ADR-057). Best-effort: a
        busy checkpoint is harmless — the frames stay in the WAL and are
        recovered automatically on the next open."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def compact(self) -> tuple[int, int]:
        """Reclaim unused space by rewriting the database file (SQLite ``VACUUM``).

        Deletes and whole-table rebuilds — merging payees, removing accounts,
        the schema migrations that copy a table to change a CHECK constraint
        (the ADR-032 recipe) — leave **free pages** in the file. With
        ``auto_vacuum`` off (the default) those pages are never returned to the
        OS, so the ``.mfl`` only ever grows even when the row count falls.
        ``VACUUM`` copies the live data into a fresh, tightly-packed file and
        frees the slack back to disk. It keeps every row — only dead space goes.

        Commits any pending work and folds the WAL in first (and again after) so
        nothing is lost. Returns ``(size_before, size_after)`` in bytes."""
        before = self._db_path.stat().st_size if self._db_path.exists() else 0
        # VACUUM cannot run inside a transaction — clear any pending one, then
        # fold the WAL back so the size we measure is the real on-disk file.
        self._conn.commit()
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.execute("VACUUM")
        self._conn.commit()
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        after = self._db_path.stat().st_size if self._db_path.exists() else 0
        return before, after

    def is_open(self) -> bool:
        """True if the underlying connection is still usable. Cheap probe used
        by long-lived UI (e.g. the budget window's activate-refresh) to avoid
        operating on a connection that has been closed — e.g. during app
        shutdown, when the owning window's closeEvent has already run
        ``close()`` but a queued event still fires a refresh."""
        try:
            self._conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    # Raw connection — for the smoke-test CLI and the schema/repo internals.
    # UI/service code should use the typed methods below.
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # ── Account ──

    _ACCOUNT_COLS = (
        "id, iri, name, type, family, currency, is_liability, "
        "opening_balance, folder_id, credit_limit, archived_at"
    )

    def _row_to_account(self, row) -> AccountSummary:
        return AccountSummary(
            id=row["id"], iri=row["iri"], name=row["name"],
            type=row["type"], family=row["family"],
            currency=row["currency"], is_liability=bool(row["is_liability"]),
            opening_balance=pence_to_decimal(row["opening_balance"] or 0),
            folder_id=row["folder_id"],
            credit_limit=(
                pence_to_decimal(row["credit_limit"])
                if row["credit_limit"] is not None else None
            ),
            archived_at=row["archived_at"],
        )

    def get_account_by_iri(self, iri: str) -> Optional[AccountSummary]:
        row = self._conn.execute(
            f"SELECT {self._ACCOUNT_COLS} FROM account WHERE iri = ?",
            (iri,),
        ).fetchone()
        return self._row_to_account(row) if row is not None else None

    def get_account_by_id(self, account_id: int) -> Optional[AccountSummary]:
        row = self._conn.execute(
            f"SELECT {self._ACCOUNT_COLS} FROM account WHERE id = ?",
            (account_id,),
        ).fetchone()
        return self._row_to_account(row) if row is not None else None

    def list_accounts(self, include_closed: bool = False) -> list[AccountSummary]:
        """Accounts in display order (family, name).

        By default only *open* accounts are returned (``archived_at IS NULL``)
        — the single source of truth for the sidebar's active list, every
        transaction/transfer picker, and every report's account filter. Pass
        ``include_closed=True`` to also return closed (archived) accounts; the
        only callers that do are the sidebar (which renders them in a separate
        'Closed accounts' group) and Net Worth's 'Show closed' toggle (ADR-069).
        """
        where = "" if include_closed else "WHERE archived_at IS NULL "
        cur = self._conn.execute(
            f"SELECT {self._ACCOUNT_COLS} FROM account "
            f"{where}"
            "ORDER BY family, name"
        )
        return [self._row_to_account(r) for r in cur]

    def list_investment_accounts(
        self, include_closed: bool = False,
    ) -> list[AccountSummary]:
        """Investment-family accounts in display order. Feeds the Investment
        Returns report's account filter (ADR-046). Closed accounts are
        excluded unless ``include_closed=True`` (ADR-069)."""
        return [
            a for a in self.list_accounts(include_closed=include_closed)
            if a.family == "investment"
        ]

    def account_has_transactions(self, account_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM txn WHERE account_id = ? LIMIT 1", (account_id,),
        ).fetchone()
        return row is not None

    def count_account_transactions(self, account_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM txn WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _next_account_iri(self, class_name: str) -> str:
        """Generate the next sequential IRI for the given MRL class — finds
        the largest existing integer suffix on accounts of that class and
        increments. Scoped per class so deletions in one class don't shift
        another class's numbering."""
        prefix = f"mrl:{class_name}_"
        max_n = 0
        for row in self._conn.execute(
            "SELECT iri FROM account WHERE iri LIKE ?", (f"{prefix}%",),
        ):
            try:
                max_n = max(max_n, int(row["iri"][len(prefix):]))
            except ValueError:
                pass
        return f"{prefix}{max_n + 1}"

    def create_account(
        self,
        *,
        name: str,
        type_key: str,
        currency: str,
        opening_balance: Decimal = Decimal("0.00"),
        credit_limit: Optional[Decimal] = None,
    ) -> AccountSummary:
        """Create a new account. `type_key` is the short key from
        account_types.ACCOUNT_TYPES (e.g. 'cash'). Family, is_liability,
        and the IRI class name are derived from the type. ``credit_limit``
        (credit cards only, ADR-058 R4a) is optional. Commits on success."""
        spec: AccountTypeSpec = by_key(type_key)
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Account name cannot be empty.")
        clean_currency = (currency or "").strip().upper()
        if not clean_currency:
            raise ValueError("Currency cannot be empty.")
        iri = self._next_account_iri(spec.class_name)
        try:
            cur = self._conn.execute(
                "INSERT INTO account "
                "(iri, name, type, family, currency, is_liability, "
                " opening_balance, credit_limit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iri, clean_name, spec.storage, spec.family, clean_currency,
                    1 if spec.is_liability else 0,
                    decimal_to_pence(opening_balance),
                    decimal_to_pence(credit_limit)
                    if credit_limit is not None else None,
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        new_id = cur.lastrowid
        # Re-read so the returned AccountSummary uses the same conversion path
        # as list_accounts (single source of truth for pence→Decimal).
        acct = self.get_account_by_id(new_id)
        assert acct is not None
        return acct

    def update_account(
        self,
        account_id: int,
        *,
        name: str,
        currency: str,
        opening_balance: Decimal,
        credit_limit=_UNSET,
    ) -> AccountSummary:
        """Edit an existing account's name, currency, and opening balance.
        Type / family / is_liability are intentionally not editable here —
        those change the meaning of stored amounts. ``credit_limit`` follows
        the _UNSET sentinel — pass a Decimal to set it, ``None`` to clear it,
        or omit it to leave the stored value untouched (ADR-058 R4a). Commits
        on success."""
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Account name cannot be empty.")
        clean_currency = (currency or "").strip().upper()
        if not clean_currency:
            raise ValueError("Currency cannot be empty.")
        sets = ["name = ?", "currency = ?", "opening_balance = ?"]
        params: list = [
            clean_name, clean_currency, decimal_to_pence(opening_balance),
        ]
        if credit_limit is not self._UNSET:
            sets.append("credit_limit = ?")
            params.append(
                decimal_to_pence(credit_limit)
                if credit_limit is not None else None
            )
        params.append(account_id)
        try:
            self._conn.execute(
                f"UPDATE account SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        acct = self.get_account_by_id(account_id)
        if acct is None:
            raise ValueError(f"No account with id {account_id}")
        return acct

    # ── Account folders (sidebar grouping, ADR-015) ──

    def list_folders(self) -> list[FolderSummary]:
        """All non-archived folders in sidebar order (sort_order, then name
        as a stable tiebreaker)."""
        cur = self._conn.execute(
            "SELECT f.id, f.name, f.sort_order, "
            "       (SELECT COUNT(*) FROM account a "
            "        WHERE a.folder_id = f.id AND a.archived_at IS NULL) AS n "
            "FROM account_folder f "
            "WHERE f.archived_at IS NULL "
            "ORDER BY f.sort_order, f.name"
        )
        return [
            FolderSummary(
                id=r["id"], name=r["name"],
                sort_order=int(r["sort_order"]), account_count=int(r["n"]),
            )
            for r in cur
        ]

    def create_folder(self, name: str) -> FolderSummary:
        """Create a folder. Appended at the end of the existing folder list
        (sort_order = current max + 1) so reorders are explicit."""
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Folder name cannot be empty.")
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM account_folder "
            "WHERE archived_at IS NULL"
        ).fetchone()
        next_order = int(row["m"]) + 1
        try:
            cur = self._conn.execute(
                "INSERT INTO account_folder (name, sort_order) VALUES (?, ?)",
                (clean, next_order),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return FolderSummary(
            id=cur.lastrowid, name=clean, sort_order=next_order,
            account_count=0,
        )

    def rename_folder(self, folder_id: int, new_name: str) -> None:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Folder name cannot be empty.")
        try:
            self._conn.execute(
                "UPDATE account_folder SET name = ? WHERE id = ?",
                (clean, folder_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def delete_folder(self, folder_id: int) -> None:
        """Delete a folder. Accounts belonging to it fall to the sidebar
        root because of the FK rule (ON DELETE SET NULL) — no account or
        transaction data is lost."""
        try:
            self._conn.execute(
                "DELETE FROM account_folder WHERE id = ?", (folder_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_account_folder(
        self, account_id: int, folder_id: Optional[int],
    ) -> None:
        """Move an account in or out of a folder. Passing None moves it
        back to the sidebar root."""
        try:
            self._conn.execute(
                "UPDATE account SET folder_id = ? WHERE id = ?",
                (folder_id, account_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def move_folder(self, folder_id: int, direction: int) -> None:
        """Swap this folder's sort_order with its immediate neighbour in
        the given direction (-1 = up, +1 = down). No-op if there is no
        neighbour on that side."""
        if direction not in (-1, 1):
            raise ValueError(f"Invalid move direction: {direction}")
        row = self._conn.execute(
            "SELECT sort_order FROM account_folder WHERE id = ?",
            (folder_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No folder with id {folder_id}")
        current = int(row["sort_order"])
        if direction == -1:
            neighbour = self._conn.execute(
                "SELECT id, sort_order FROM account_folder "
                "WHERE sort_order < ? AND archived_at IS NULL "
                "ORDER BY sort_order DESC LIMIT 1",
                (current,),
            ).fetchone()
        else:
            neighbour = self._conn.execute(
                "SELECT id, sort_order FROM account_folder "
                "WHERE sort_order > ? AND archived_at IS NULL "
                "ORDER BY sort_order ASC LIMIT 1",
                (current,),
            ).fetchone()
        if neighbour is None:
            return
        try:
            # Swap. A two-step UPDATE through a sentinel keeps a hypothetical
            # UNIQUE(sort_order) future constraint working; today sort_order
            # is unconstrained but the pattern is cheap.
            self._conn.execute(
                "UPDATE account_folder SET sort_order = -1 WHERE id = ?",
                (folder_id,),
            )
            self._conn.execute(
                "UPDATE account_folder SET sort_order = ? WHERE id = ?",
                (current, neighbour["id"]),
            )
            self._conn.execute(
                "UPDATE account_folder SET sort_order = ? WHERE id = ?",
                (int(neighbour["sort_order"]), folder_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def compute_account_balances(
        self, include_closed: bool = False, as_of_date: Optional[str] = None,
    ) -> dict[int, Decimal]:
        """Per-account balance: opening_balance + sum of txn.amount.

        Returns a dict keyed by account_id. Investment / property accounts
        use the same opening + txns formula for now — once the valuations
        UX ships (backlog) those families switch to latest-valuation.

        ``as_of_date`` (inclusive ``'YYYY-MM-DD'``), when given, counts only
        transactions ``posted_date <= as_of_date`` — i.e. **today's** balance,
        excluding future-dated ("forwarded") rows. ``None`` sums the whole
        ledger (the **projected** balance) — ADR-131.

        Closed accounts are omitted unless ``include_closed=True`` (ADR-069),
        matching :pymeth:`list_accounts`.
        """
        where = "" if include_closed else "WHERE a.archived_at IS NULL"
        date_clause = " AND t.posted_date <= ?" if as_of_date else ""
        params = (as_of_date,) if as_of_date else ()
        cur = self._conn.execute(
            "SELECT a.id, "
            "       a.opening_balance + COALESCE((SELECT SUM(t.amount) "
            "                                     FROM txn t "
            "                                     WHERE t.account_id = a.id"
            f"{date_clause}), 0) "
            "       AS balance_pence "
            "FROM account a "
            f"{where}",
            params,
        )
        return {int(r["id"]): pence_to_decimal(r["balance_pence"]) for r in cur}

    def balance_as_of(self, account_id: int, as_of_date: str) -> Decimal:
        """Recorded cash balance for one account at the *end of* ``as_of_date``
        (an inclusive ``'YYYY-MM-DD'`` bound):
        ``opening_balance + SUM(amount) WHERE posted_date <= as_of_date``.

        Same shape as :pymeth:`compute_account_balances` (opening + Σ amount)
        and the running-balance seed in :pymeth:`list_transactions_for_account`
        — but inclusive of the boundary day, because a statement's ending
        balance counts every transaction dated on or before the closing date
        (the windowing seed uses ``< since``; this uses ``<= as_of_date``).
        Pure cash ledger, no market-value/valuation adjustment — consistent
        with reconciliation operating on the transaction ledger (ADR-040)."""
        opening_row = self._conn.execute(
            "SELECT opening_balance FROM account WHERE id = ?", (account_id,),
        ).fetchone()
        opening = pence_to_decimal(
            opening_row["opening_balance"] if opening_row else 0
        )
        sum_row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM txn "
            "WHERE account_id = ? AND posted_date <= ?",
            (account_id, as_of_date),
        ).fetchone()
        return opening + pence_to_decimal(sum_row["s"])

    def earliest_posted_date(self) -> Optional[str]:
        """The earliest ``posted_date`` ('YYYY-MM-DD') across all transactions,
        or None if the ledger is empty. The floor for an "All" net-worth-over-
        time range (ADR-121)."""
        row = self._conn.execute(
            "SELECT MIN(posted_date) AS d FROM txn WHERE posted_date IS NOT NULL"
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def compute_account_values(
        self, include_closed: bool = False, as_of_date: Optional[str] = None,
    ) -> dict[int, Decimal]:
        """Per-account *market value* (ADR-044) — the figure Net Worth and the
        sidebar should show. For investment accounts this is
        ``cash + Σ(open-lot shares × latest price)``; unpriced holdings
        contribute nothing, so an account with no prices on file falls back to
        its cash balance. Every other family is the cash balance unchanged
        (``compute_account_balances``). The register's running-balance column is
        a transaction ledger and is deliberately NOT affected by this.

        ``as_of_date`` (ADR-131) restricts both the cash sum and the investment
        holdings to transactions ``posted_date <= as_of_date`` — today's balance
        vs the projected (whole-ledger) balance. Prices are always the latest on
        file; only the transaction set is dated.

        Closed accounts are omitted unless ``include_closed=True`` (ADR-069).

        Memoised against :meth:`data_generation` (ADR-153). This is the app's
        most expensive derivation — it replays every investment account's whole
        ledger through the FIFO holdings engine — and the UI calls it on every
        sidebar reload and every Home refresh, almost always for data that has
        not moved. The cache is keyed on the arguments as well as the
        generation, since ``as_of_date`` changes the answer."""
        key = (include_closed, as_of_date)
        gen = self.data_generation()
        if gen == self._acct_values_gen:
            hit = self._acct_values_cache.get(key)
            if hit is not None:
                # Copy: callers treat the result as theirs to mutate, and Decimal
                # values are immutable, so a shallow copy fully isolates them.
                return dict(hit)
        else:
            self._acct_values_cache.clear()
            self._acct_values_gen = gen
        values = self._compute_account_values_uncached(include_closed, as_of_date)
        self._acct_values_cache[key] = values
        return dict(values)

    def _compute_account_values_uncached(
        self, include_closed: bool, as_of_date: Optional[str],
    ) -> dict[int, Decimal]:
        balances = self.compute_account_balances(
            include_closed=include_closed, as_of_date=as_of_date,
        )
        investment = [
            a for a in self.list_accounts(include_closed=include_closed)
            if a.family == "investment"
        ]
        if not investment:
            return balances
        # Lazy import: holdings.py imports TransactionRow from this module, so a
        # top-level import here would be circular.
        from mfl_desktop.holdings import compute_holdings_view
        price_map = {
            sid: (p.price, p.price_date)
            for sid, p in self.latest_prices().items()
        }
        multipliers = self.security_multipliers()   # ADR-093: bond/option scaling
        values = dict(balances)
        for acct in investment:
            txns = self.list_transactions_for_account(acct.id)
            if as_of_date:
                txns = [t for t in txns if t.posted_date <= as_of_date]
            view = compute_holdings_view(
                txns, acct.opening_balance, price_map, multipliers,
            )
            values[acct.id] = view.account_value
        return values

    # ── Loans (ADR-095) ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_loan(r) -> Loan:
        return Loan(
            account_id=r["account_id"],
            original_amount=pence_to_decimal(r["original_amount"]),
            principal_paid=pence_to_decimal(r["principal_paid"]),
            interest_rate=float(r["interest_rate"]),
            compounding=r["compounding"],
            term_months=r["term_months"],
            payment=(pence_to_decimal(r["payment"])
                     if r["payment"] is not None else None),
            extra_payment=pence_to_decimal(r["extra_payment"]),
            start_date=r["start_date"], payment_day=int(r["payment_day"]),
            track_mode=r["track_mode"], interest_source=r["interest_source"],
            payment_account_id=r["payment_account_id"],
            interest_category_id=r["interest_category_id"],
            goal_id=r["goal_id"],
        )

    def get_loan(self, account_id: int) -> Optional[Loan]:
        """The loan terms for an account, or None if it isn't a loan."""
        r = self._conn.execute(
            "SELECT * FROM loan WHERE account_id = ?", (account_id,),
        ).fetchone()
        return self._row_to_loan(r) if r is not None else None

    def list_loans(self) -> list[Loan]:
        """Every loan's terms (one per loan account)."""
        return [
            self._row_to_loan(r)
            for r in self._conn.execute("SELECT * FROM loan")
        ]

    def loan_current_balance(self, account_id: int) -> Decimal:
        """The principal still owed, as a **positive** magnitude (the loan
        account's balance is negative; this flips it). The amortization schedule
        and payment posting project forward from this live figure."""
        bal = self.compute_account_balances(include_closed=True).get(
            account_id, Decimal("0.00"),
        )
        return -bal if bal < 0 else Decimal("0.00")

    def effective_payment(self, loan: Loan) -> Decimal:
        """The level monthly payment: the stored one, else calculated from the
        **original** amount over the term (the contractual payment)."""
        if loan.payment is not None:
            return loan.payment
        if loan.term_months:
            from mfl_desktop.loan_calc import required_payment
            return required_payment(
                loan.original_amount, loan.interest_rate, loan.compounding,
                loan.term_months,
            )
        return Decimal("0.00")

    def loan_schedule(self, account_id: int):
        """The amortization schedule from the loan's **live** balance forward
        (ADR-095). Returns a ``loan_calc.AmortSchedule`` (lazy import — loan_calc
        is pure and Qt-free, but importing at module top isn't needed)."""
        from mfl_desktop.loan_calc import compute_schedule
        loan = self.get_loan(account_id)
        if loan is None:
            raise ValueError(f"Account {account_id} is not a loan.")
        return compute_schedule(
            current_principal=self.loan_current_balance(account_id),
            annual_rate_pct=loan.interest_rate, compounding=loan.compounding,
            payment=self.effective_payment(loan),
            start_date=loan.start_date, payment_day=loan.payment_day,
            extra_payment=loan.extra_payment,
        )

    def create_loan_account(
        self, *,
        name: str,
        currency: str,
        original_amount: Decimal,
        interest_rate: float,
        start_date: str,
        principal_paid: Decimal = Decimal("0.00"),
        compounding: str = "monthly",
        term_months: Optional[int] = None,
        payment: Optional[Decimal] = None,
        extra_payment: Decimal = Decimal("0.00"),
        payment_day: int = 1,
        track_mode: str = "split",
        interest_source: str = "loan",
        payment_account_id: Optional[int] = None,
        interest_category_id: Optional[int] = None,
    ) -> int:
        """Create a loan account + its terms (ADR-095). The account's opening
        balance is set to the **current principal owed** (original − already
        paid) as a negative liability, so its balance reads as the debt. Returns
        the new account id. Commits."""
        current = original_amount - principal_paid
        acct = self.create_account(
            name=name, type_key="loan", currency=currency,
            opening_balance=-current,
        )
        try:
            self._conn.execute(
                "INSERT INTO loan (account_id, original_amount, principal_paid, "
                "  interest_rate, compounding, term_months, payment, "
                "  extra_payment, start_date, payment_day, track_mode, "
                "  interest_source, payment_account_id, interest_category_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    acct.id, decimal_to_pence(original_amount),
                    decimal_to_pence(principal_paid), float(interest_rate),
                    compounding, term_months,
                    decimal_to_pence(payment) if payment is not None else None,
                    decimal_to_pence(extra_payment), start_date, int(payment_day),
                    track_mode, interest_source, payment_account_id,
                    interest_category_id,
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return acct.id

    def update_loan(self, account_id: int, **fields) -> None:
        """Update a loan's terms. Accepts any subset of the column names
        (Decimal money fields are converted to pence). Commits."""
        if not fields:
            return
        money_cols = {"original_amount", "principal_paid", "payment",
                      "extra_payment"}
        sets: list[str] = []
        params: list = []
        for col, value in fields.items():
            sets.append(f"{col} = ?")
            if col in money_cols and value is not None:
                params.append(decimal_to_pence(value))
            else:
                params.append(value)
        params.append(account_id)
        try:
            self._conn.execute(
                f"UPDATE loan SET {', '.join(sets)} WHERE account_id = ?", params,
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def post_loan_payment(
        self, *,
        account_id: int,
        posted_date: str,
        amount: Optional[Decimal] = None,
        extra: Optional[Decimal] = None,
        status: str = "matched",
    ) -> None:
        """Record one loan payment, split into principal + interest per the
        loan's track mode (ADR-095), through the existing transfer/split paths.

        ``amount`` defaults to the loan's effective level payment; ``extra`` to
        its stored extra. The interest is computed from the **live** balance, so
        it shrinks as the loan is paid down. Commits (or rolls back)."""
        loan = self.get_loan(account_id)
        if loan is None:
            raise ValueError(f"Account {account_id} is not a loan.")
        bal = self.loan_current_balance(account_id)
        if bal <= 0:
            raise ValueError("This loan is already paid off.")
        pay = amount if amount is not None else self.effective_payment(loan)
        extra = extra if extra is not None else loan.extra_payment
        if pay + (extra or Decimal("0.00")) <= 0:
            raise ValueError("Enter a payment amount (no payment is set on this loan).")

        from mfl_desktop.loan_calc import split_payment
        interest, principal = split_payment(
            bal, loan.interest_rate, loan.compounding, pay, extra or Decimal("0.00"),
        )
        total_cash = interest + principal     # what actually leaves the cash account

        if loan.payment_account_id is None:
            raise ValueError(
                "Set a paying account on this loan before recording a payment."
            )
        try:
            if loan.track_mode == "whole":
                # No split — the whole payment reduces the loan balance.
                self.create_transfer(
                    from_account_id=loan.payment_account_id,
                    to_account_id=account_id, posted_date=posted_date,
                    amount=total_cash,
                    category_id=self.get_default_transfer_category_id(),
                    status=status, memo="Loan payment",
                )
            elif loan.interest_source == "loan":
                # Full payment into the loan account, then interest booked there.
                self.create_transfer(
                    from_account_id=loan.payment_account_id,
                    to_account_id=account_id, posted_date=posted_date,
                    amount=total_cash,
                    category_id=self.get_default_transfer_category_id(),
                    status=status, memo="Loan payment",
                )
                if interest > 0:
                    cat = (loan.interest_category_id
                           or self._loan_interest_category_id())
                    self.insert_transaction(
                        account_id=account_id, posted_date=posted_date,
                        amount=-interest, payee_id=None, category_id=cat,
                        status=status, memo="Loan interest",
                        import_hash=None, import_batch_id=None,
                    )
            else:  # split, interest paid from the paying account
                cat = loan.interest_category_id or self._loan_interest_category_id()
                xfer_cat = self.get_default_transfer_category_id()
                lines = [
                    (xfer_cat, "Principal", -principal, account_id),
                ]
                if interest > 0:
                    lines.append((cat, "Interest", -interest))
                self.insert_split_transaction(
                    account_id=loan.payment_account_id, posted_date=posted_date,
                    payee_id=None, status=status, memo="Loan payment",
                    total_amount=-total_cash, lines=lines,
                    import_hash=None, import_batch_id=None,
                )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def _loan_interest_category_id(self) -> int:
        """The default interest-expense category (seeded under Expenses),
        created on demand."""
        return self.find_or_create_category_path(
            ["Interest", "Loan interest"], source="user",
        )

    def create_loan_paydown_goal(self, account_id: int, budget_id: int) -> int:
        """Create a pay-down goal that tracks this loan to zero by its payoff
        date (ADR-095, reusing ADR-058 R4b) and link it on the loan. The loan
        account is the goal's sole account at 100%. Returns the goal id."""
        loan = self.get_loan(account_id)
        if loan is None:
            raise ValueError(f"Account {account_id} is not a loan.")
        acct = next(
            (a for a in self.list_accounts(include_closed=True) if a.id == account_id),
            None,
        )
        sched = self.loan_schedule(account_id)
        target_date = sched.payoff_date or loan.start_date
        goal_id = self.add_budget_goal(
            budget_id=budget_id,
            name=f"Pay off {acct.name if acct else 'loan'}",
            kind="paydown",
            currency=acct.currency if acct else "GBP",
            target_amount=Decimal("0.00"),     # fully paid
            target_date=target_date,
            accounts=[(account_id, 10000)],    # 100%
            today=date.today().isoformat(),
        )
        self.update_loan(account_id, goal_id=goal_id)
        return goal_id

    def close_account(self, account_id: int) -> AccountSummary:
        """Soft-close an account: set ``archived_at`` to now (ADR-069).

        Non-destructive — the account, its transactions, and all history are
        kept. A closed account drops out of :pymeth:`list_accounts` (and so
        the sidebar's active list, Net Worth, and every report's account
        picker) but its past transactions still flow into the transaction-
        driven reports (Spending / Income & Expense / Sankey / Payee). The
        ADR-011 ``archived_at`` column is the store; this is the gentle
        counterpart to the destructive :pymeth:`delete_account`. Idempotent —
        re-closing an already-closed account leaves the original timestamp."""
        try:
            self._conn.execute(
                "UPDATE account SET archived_at = datetime('now') "
                "WHERE id = ? AND archived_at IS NULL",
                (account_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        acct = self.get_account_by_id(account_id)
        if acct is None:
            raise ValueError(f"No account with id {account_id}")
        return acct

    def reopen_account(self, account_id: int) -> AccountSummary:
        """Reverse :pymeth:`close_account` — clear ``archived_at`` so the
        account rejoins the active list everywhere (ADR-069)."""
        try:
            self._conn.execute(
                "UPDATE account SET archived_at = NULL WHERE id = ?",
                (account_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        acct = self.get_account_by_id(account_id)
        if acct is None:
            raise ValueError(f"No account with id {account_id}")
        return acct

    def delete_account(self, account_id: int) -> int:
        """Hard-delete an account and everything that references it
        (transactions, import batches, lots, valuations all cascade by FK).
        Returns the count of transactions that were cascaded.

        This is the destructive variant — see :pymeth:`close_account` for the
        non-destructive soft-close that keeps history (ADR-069)."""
        txn_count = self.count_account_transactions(account_id)
        try:
            self._conn.execute("DELETE FROM account WHERE id = ?", (account_id,))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return txn_count

    # ── Category ──

    def uncategorised_id(self) -> int:
        return UNCATEGORISED_ID

    _REINVEST_DIVIDEND_CATEGORY_SETTING = "reinvest_dividend_category_id"

    def get_reinvest_dividend_category_id(self) -> Optional[int]:
        """The category that **reinvested distributions** (DRIP) default to on
        import and in the investment dialog (ADR-089). It's the owner's choice
        — e.g. *Dividend Income* — seeded by filing a reinvest under a category
        in the dialog (which writes it via
        :meth:`set_reinvest_dividend_category_id`).

        Returns the configured category id, or ``None`` when unset or when the
        stored id no longer points at a live ``kind='income'`` category (so a
        deleted / re-kinded category silently falls back to no-default rather
        than mis-filing income)."""
        raw = self.get_setting(self._REINVEST_DIVIDEND_CATEGORY_SETTING)
        if not raw:
            return None
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return None
        row = self._conn.execute(
            "SELECT 1 FROM category "
            "WHERE id = ? AND kind = 'income' AND archived_at IS NULL",
            (cid,),
        ).fetchone()
        return cid if row is not None else None

    def set_reinvest_dividend_category_id(
        self, category_id: Optional[int],
    ) -> None:
        """Persist the default reinvested-dividend category (ADR-089). Pass
        ``None`` to clear it."""
        self.set_setting(
            self._REINVEST_DIVIDEND_CATEGORY_SETTING,
            str(category_id) if category_id is not None else "",
        )

    _DIVIDEND_CATEGORY_SETTING = "dividend_category_id"

    def get_dividend_category_id(self) -> Optional[int]:
        """The category a **cash dividend** defaults to in the investment dialog
        (ADR-142) — the owner's remembered choice (e.g. *Dividend Income*)
        instead of the generic seeded *Investment income*. Seeded by filing a
        cash dividend under a category (via :meth:`set_dividend_category_id`).

        Returns ``None`` when unset or when the stored id no longer points at a
        live ``kind='income'`` category (a deleted / re-kinded category falls
        back to no-default rather than mis-filing income)."""
        raw = self.get_setting(self._DIVIDEND_CATEGORY_SETTING)
        if not raw:
            return None
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return None
        row = self._conn.execute(
            "SELECT 1 FROM category "
            "WHERE id = ? AND kind = 'income' AND archived_at IS NULL",
            (cid,),
        ).fetchone()
        return cid if row is not None else None

    def set_dividend_category_id(self, category_id: Optional[int]) -> None:
        """Persist the default cash-dividend category (ADR-142). Pass ``None``
        to clear it."""
        self.set_setting(
            self._DIVIDEND_CATEGORY_SETTING,
            str(category_id) if category_id is not None else "",
        )

    def find_or_create_category_path(
        self, segments: list[str], source: str = "import",
        default_root_kind: str = "expense",
    ) -> int:
        """Walk or create a category path (root → leaf), return leaf id.

        Each segment is a name; intermediate nodes are created with the
        given source if missing. Whitespace-only segments are ignored.
        Returns Uncategorised id if segments is empty after cleanup.

        New top-level categories are created with `default_root_kind`
        (per ADR-014 the import default is 'expense' — most imported lines
        are spend, and the user can reclassify in the category manager).
        New sub-categories inherit their parent's kind.
        """
        clean = [s.strip() for s in segments if s and s.strip()]
        if not clean:
            return UNCATEGORISED_ID
        parent_id: Optional[int] = None
        parent_kind: Optional[str] = None
        for name in clean:
            if parent_id is None:
                row = self._conn.execute(
                    "SELECT id, kind, archived_at FROM category "
                    "WHERE name = ? AND parent_id IS NULL",
                    (name,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT id, kind, archived_at FROM category "
                    "WHERE name = ? AND parent_id = ?",
                    (name, parent_id),
                ).fetchone()
            if row is None:
                kind = parent_kind if parent_kind is not None else default_root_kind
                cur = self._conn.execute(
                    "INSERT INTO category (parent_id, name, source, kind) "
                    "VALUES (?, ?, ?, ?)",
                    (parent_id, name, source, kind),
                )
                parent_id = cur.lastrowid
                parent_kind = kind
            else:
                # An import re-using a path that the user had archived means
                # the category is in use again (ADR-070). The UNIQUE
                # (parent_id, name) constraint would block a fresh sibling, so
                # resurrect the existing row rather than land transactions on an
                # invisible (archived) category. Walking root→leaf this
                # restores the whole matched path.
                if row["archived_at"] is not None:
                    self._conn.execute(
                        "UPDATE category SET archived_at = NULL WHERE id = ?",
                        (row["id"],),
                    )
                parent_id = row["id"]
                parent_kind = row["kind"]
        return parent_id

    def find_category_path(self, segments: list[str]) -> Optional[int]:
        """Find an existing category path (root → leaf), return the leaf id, or
        ``None`` if any segment is missing. The find-only twin of
        :meth:`find_or_create_category_path` — it never inserts, so an importer
        can ask "does the user already have this category?" without forking a
        duplicate (ADR-112). Archived rows count: a hidden category is still a
        real one the user chose to keep, not an absence."""
        clean = [s.strip() for s in segments if s and s.strip()]
        if not clean:
            return None
        parent_id: Optional[int] = None
        for name in clean:
            if parent_id is None:
                row = self._conn.execute(
                    "SELECT id FROM category WHERE name = ? AND parent_id IS NULL",
                    (name,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT id FROM category WHERE name = ? AND parent_id = ?",
                    (name, parent_id),
                ).fetchone()
            if row is None:
                return None
            parent_id = row["id"]
        return parent_id

    # ── Category import-map + match-only (ADR-112) ──
    #
    # An importer resolves each transaction's *source* category path (e.g.
    # "Bills:Utilities:Cable") against the tree. By default it creates anything
    # it can't find — which silently recreates categories the user has merged,
    # reparented, or deleted. Two countermeasures:
    #   • a persistent map (source path → my category), auto-recorded whenever
    #     the user merges/deletes/reparents, consulted at import before any
    #     create — so a curated decision sticks across re-imports; and
    #   • a match-only mode that routes anything still unmatched to "Needs
    #     Review" instead of creating it, so new imports never quietly fork the
    #     tree again.

    _IMPORT_MATCH_ONLY_SETTING = "import_match_only_categories"
    _NEEDS_REVIEW_CATEGORY_SETTING = "needs_review_category_id"

    @staticmethod
    def normalize_category_path(raw) -> str:
        """The canonical map key for a category path: each segment trimmed and
        lower-cased, blanks dropped, re-joined with ':'. Accepts a raw
        "A:B:C" string or a list of segments. Case- and whitespace-folding
        means "Bills : Utilities" and "bills:utilities" map to one key."""
        segments = raw.split(":") if isinstance(raw, str) else list(raw)
        clean = [str(s).strip().lower() for s in segments if s and str(s).strip()]
        return ":".join(clean)

    def _category_path_string(self, category_id: int) -> str:
        """The normalised source-path key for an existing category id — its full
        root→leaf path folded by :meth:`normalize_category_path`. '' for an
        unknown id. Used to record what a just-merged/deleted/moved category
        *was* called, so a later import of that name is rerouted not recreated."""
        names: list[str] = []
        cid: Optional[int] = category_id
        seen: set[int] = set()
        while cid is not None and cid not in seen:
            seen.add(cid)
            row = self._conn.execute(
                "SELECT name, parent_id FROM category WHERE id = ?", (cid,),
            ).fetchone()
            if row is None:
                break
            names.append(row["name"])
            cid = row["parent_id"]
        names.reverse()
        return self.normalize_category_path(names)

    def category_display_path(self, category_id: int) -> str:
        """The full root→leaf path of a category in its original casing, joined
        with ' : ' for display (e.g. "Bills : Cable and Internet"). '' for an
        unknown id. Used by the import-mappings management dialog."""
        names: list[str] = []
        cid: Optional[int] = category_id
        seen: set[int] = set()
        while cid is not None and cid not in seen:
            seen.add(cid)
            row = self._conn.execute(
                "SELECT name, parent_id FROM category WHERE id = ?", (cid,),
            ).fetchone()
            if row is None:
                break
            names.append(row["name"])
            cid = row["parent_id"]
        names.reverse()
        return " : ".join(names)

    def import_match_only_categories(self) -> bool:
        """Whether imports route unmatched categories to Needs Review instead of
        creating them (ADR-112). Off by default — a fresh file should still
        build its tree from the first import; the curating user turns it on."""
        return self.get_setting(self._IMPORT_MATCH_ONLY_SETTING) == "1"

    def set_import_match_only_categories(self, on: bool) -> None:
        self.set_setting(self._IMPORT_MATCH_ONLY_SETTING, "1" if on else "0")

    def needs_review_category_id(self) -> int:
        """The "Needs Review" holding category (migration 0032), where match-only
        imports park unmatched categories. Resolves via the stored setting, then
        a name lookup, then Uncategorised — so an import never fails for want of
        it even if the setting row was lost."""
        raw = self.get_setting(self._NEEDS_REVIEW_CATEGORY_SETTING)
        if raw:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                cid = None
            if cid is not None and self._conn.execute(
                "SELECT 1 FROM category WHERE id = ?", (cid,),
            ).fetchone() is not None:
                return cid
        row = self._conn.execute(
            "SELECT id FROM category WHERE name = 'Needs Review' "
            "AND parent_id IS NULL AND source = 'system'",
        ).fetchone()
        return row["id"] if row is not None else UNCATEGORISED_ID

    def get_category_import_mapping(self, source_path) -> Optional[int]:
        """The mapped target category id for a source path, or ``None`` if there
        is no mapping (ADR-112). Accepts a raw string or segment list."""
        key = self.normalize_category_path(source_path)
        if not key:
            return None
        row = self._conn.execute(
            "SELECT target_category_id FROM category_import_map "
            "WHERE source_path = ?",
            (key,),
        ).fetchone()
        return row["target_category_id"] if row is not None else None

    def set_category_import_mapping(
        self, source_path, target_category_id: int,
    ) -> None:
        """Record/replace a source-path → target-category mapping (ADR-112)."""
        key = self.normalize_category_path(source_path)
        if not key:
            return
        try:
            self._conn.execute(
                "INSERT INTO category_import_map (source_path, target_category_id) "
                "VALUES (?, ?) ON CONFLICT(source_path) DO UPDATE SET "
                "target_category_id = excluded.target_category_id",
                (key, target_category_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def delete_category_import_mapping(self, source_path) -> None:
        """Remove a mapping so its source path resolves normally again."""
        key = self.normalize_category_path(source_path)
        if not key:
            return
        try:
            self._conn.execute(
                "DELETE FROM category_import_map WHERE source_path = ?", (key,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def list_category_import_map(self) -> list[tuple[str, int, str]]:
        """Every mapping as ``(source_path, target_category_id, target_label)``,
        ordered by source path, for the management dialog. ``target_label`` is
        the target's display path (empty if the target was deleted out from
        under the row, though the FK cascade normally prevents that)."""
        rows = self._conn.execute(
            "SELECT source_path, target_category_id FROM category_import_map "
            "ORDER BY source_path",
        ).fetchall()
        return [
            (r["source_path"], r["target_category_id"],
             self.category_display_path(r["target_category_id"]))
            for r in rows
        ]

    def _record_category_import_mapping(
        self, source_path_key: str, target_category_id: int,
    ) -> None:
        """Insert/replace a mapping **without committing** — for use inside the
        merge/delete/reparent transactions so the record lands atomically with
        the structural change."""
        if not source_path_key:
            return
        self._conn.execute(
            "INSERT INTO category_import_map (source_path, target_category_id) "
            "VALUES (?, ?) ON CONFLICT(source_path) DO UPDATE SET "
            "target_category_id = excluded.target_category_id",
            (source_path_key, target_category_id),
        )

    # ── Category — management (used by the category dialog) ──

    def list_category_tree(
        self, include_archived: bool = False,
    ) -> list[CategoryNode]:
        """Categories as a flat list; the dialog reassembles the parent/child
        structure for display. Archived categories are excluded unless
        ``include_archived=True`` (ADR-070) — the only caller that asks is the
        Manage ▸ Categories dialog with its 'Show archived' toggle on."""
        # Usage counts a category's appearances in the split-unrolled view
        # (ADR-051): a split line counts under its own category, and a split
        # parent does NOT count under Uncategorised (the view emits its lines,
        # not the parent row). For a no-splits ledger this equals COUNT over
        # `txn`.
        where = "" if include_archived else "WHERE c.archived_at IS NULL"
        cur = self._conn.execute(
            "SELECT c.id, c.parent_id, c.name, c.source, c.kind, "
            "       c.archived_at, "
            "       (SELECT COUNT(*) FROM txn_category_line t "
            "        WHERE t.category_id = c.id) AS n "
            "FROM category c "
            f"{where}"
        )
        return [
            CategoryNode(
                id=r["id"], parent_id=r["parent_id"], name=r["name"],
                source=r["source"], kind=r["kind"], usage_count=int(r["n"]),
                archived=r["archived_at"] is not None,
            )
            for r in cur
        ]

    def get_category_kind(self, category_id: int) -> Optional[str]:
        """Returns the kind of a category, or None if no such id."""
        row = self._conn.execute(
            "SELECT kind FROM category WHERE id = ?", (category_id,),
        ).fetchone()
        return row["kind"] if row is not None else None

    def category_descendants(self, category_id: int) -> set[int]:
        """All ids in the subtree rooted at `category_id`, including itself.
        Uses SQLite's WITH RECURSIVE (per ADR-010 §4)."""
        cur = self._conn.execute(
            "WITH RECURSIVE d(id) AS ("
            "  SELECT id FROM category WHERE id = ? "
            "  UNION ALL "
            "  SELECT c.id FROM category c JOIN d ON c.parent_id = d.id"
            ") SELECT id FROM d",
            (category_id,),
        )
        return {r["id"] for r in cur}

    def category_has_children(self, category_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM category WHERE parent_id = ? LIMIT 1",
            (category_id,),
        ).fetchone()
        return row is not None

    def count_category_transactions(self, category_id: int) -> int:
        # Count via the split-unrolled view (ADR-051) so split lines on this
        # category are included — these are the rows the delete/merge path
        # repoints to the Uncategorised sink.
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM txn_category_line WHERE category_id = ?",
            (category_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def find_top_level_category_id_by_name(self, name: str) -> Optional[int]:
        """Used by the merge-target picker to detect collisions when the user
        types a brand-new top-level name — see ADR-013."""
        clean = (name or "").strip()
        if not clean:
            return None
        row = self._conn.execute(
            "SELECT id FROM category WHERE name = ? AND parent_id IS NULL",
            (clean,),
        ).fetchone()
        return row["id"] if row is not None else None

    def _sibling_exists(
        self, name: str, parent_id: Optional[int], exclude_id: Optional[int],
    ) -> bool:
        """True when another category with `name` lives under `parent_id`.
        `exclude_id` skips a specific row — used by rename/reparent to ignore
        the node being moved."""
        if parent_id is None:
            row = self._conn.execute(
                "SELECT id FROM category "
                "WHERE name = ? AND parent_id IS NULL "
                "  AND (? IS NULL OR id != ?)",
                (name, exclude_id, exclude_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id FROM category "
                "WHERE name = ? AND parent_id = ? "
                "  AND (? IS NULL OR id != ?)",
                (name, parent_id, exclude_id, exclude_id),
            ).fetchone()
        return row is not None

    def create_category(
        self, name: str, parent_id: Optional[int], kind: str,
        source: str = "user",
    ) -> int:
        """Insert a new category. The caller passes `kind` — for top-level
        creates the dialog asks the user; for sub-categories the dialog
        passes the parent's kind. Storing the kind on each row keeps
        report logic out of the recursive-walk path."""
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Category name cannot be empty.")
        if kind not in CATEGORY_KINDS:
            raise ValueError(
                f"Invalid kind {kind!r}; expected one of {CATEGORY_KINDS}."
            )
        if self._sibling_exists(clean, parent_id, exclude_id=None):
            raise ValueError(
                f"A category named {clean!r} already exists at that level."
            )
        try:
            cur = self._conn.execute(
                "INSERT INTO category (parent_id, name, source, kind) "
                "VALUES (?, ?, ?, ?)",
                (parent_id, clean, source, kind),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return cur.lastrowid

    def rename_category(self, category_id: int, new_name: str) -> None:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Category name cannot be empty.")
        row = self._conn.execute(
            "SELECT parent_id FROM category WHERE id = ?", (category_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No category with id {category_id}")
        if self._sibling_exists(clean, row["parent_id"], exclude_id=category_id):
            raise ValueError(
                f"A category named {clean!r} already exists at the same "
                f"level — use Merge to combine them instead."
            )
        try:
            self._conn.execute(
                "UPDATE category SET name = ? WHERE id = ?",
                (clean, category_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def reparent_category(
        self, category_id: int, new_parent_id: Optional[int],
        new_kind: Optional[str] = None,
    ) -> None:
        """Move a category to a new parent. If `new_kind` is given, also
        update kind on this category AND all of its descendants — used
        when the dialog has confirmed a cross-kind reparent with the user.
        Otherwise kind is left untouched (intra-kind reparent)."""
        if new_parent_id == category_id:
            raise ValueError("A category cannot be its own parent.")
        if new_parent_id is not None:
            if new_parent_id in self.category_descendants(category_id):
                raise ValueError(
                    "The chosen parent is inside this category's own "
                    "subtree — that would create a cycle."
                )
        row = self._conn.execute(
            "SELECT name FROM category WHERE id = ?", (category_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No category with id {category_id}")
        if self._sibling_exists(
            row["name"], new_parent_id, exclude_id=category_id,
        ):
            raise ValueError(
                f"A category named {row['name']!r} already exists under the "
                f"chosen parent. Rename one first, or merge them."
            )
        if new_kind is not None and new_kind not in CATEGORY_KINDS:
            raise ValueError(
                f"Invalid kind {new_kind!r}; expected one of {CATEGORY_KINDS}."
            )
        # Moving a category rewrites the path of it *and* every descendant (their
        # shared prefix changes). Capture each old path → its (unchanged) id
        # before the move, so a later import using the old name reroutes to the
        # same category instead of recreating the old branch (ADR-112).
        old_paths = {
            cid: self._category_path_string(cid)
            for cid in self.category_descendants(category_id)
        }
        try:
            self._conn.execute(
                "UPDATE category SET parent_id = ? WHERE id = ?",
                (new_parent_id, category_id),
            )
            if new_kind is not None:
                descendants = self.category_descendants(category_id)
                placeholders = ",".join("?" * len(descendants))
                self._conn.execute(
                    f"UPDATE category SET kind = ? "
                    f"WHERE id IN ({placeholders})",
                    (new_kind, *descendants),
                )
            for cid, key in old_paths.items():
                # Skip a no-op where the old path equals the category's new path
                # (only possible at the root level with no actual move).
                if key and key != self._category_path_string(cid):
                    self._record_category_import_mapping(key, cid)
            self.commit()
        except Exception:
            self.rollback()
            raise

    def change_category_kind(
        self, category_id: int, new_kind: str,
    ) -> None:
        """Set kind on this category and cascade to every descendant.

        Used for the direct Change Kind verb on top-level categories. For
        sub-categories the right verb is reparent under a different-kind
        root (see ADR-014) so kind and parent stay in sync; the dialog
        enforces that distinction. The Repository, however, does not —
        it just applies the change, so any future caller (CLI, scripting)
        must take responsibility for the structural choice."""
        if new_kind not in CATEGORY_KINDS:
            raise ValueError(
                f"Invalid kind {new_kind!r}; expected one of {CATEGORY_KINDS}."
            )
        if self._conn.execute(
            "SELECT 1 FROM category WHERE id = ?", (category_id,),
        ).fetchone() is None:
            raise ValueError(f"No category with id {category_id}")
        descendants = self.category_descendants(category_id)
        if not descendants:
            return
        placeholders = ",".join("?" * len(descendants))
        # ADR-091: switching a category to transfer-kind would orphan any
        # active schedule that uses it without a destination account — the
        # auto-post sweep would then silently skip it forever (the very bug
        # ADR-074 traced to a post-hoc kind change). Refuse the change and
        # name the offenders so the user fixes the destination (or the kind)
        # first, rather than discovering broken schedules much later.
        if new_kind == "transfer":
            orphaned = self._conn.execute(
                f"SELECT s.id, a.name AS acct, "
                f"       COALESCE(p.name, '') AS payee, c.name AS cat "
                f"FROM scheduled_txn s "
                f"JOIN      account  a ON a.id = s.account_id "
                f"LEFT JOIN payee    p ON p.id = s.payee_id "
                f"JOIN      category c ON c.id = s.category_id "
                f"WHERE s.archived_at IS NULL "
                f"  AND s.transfer_to_account_id IS NULL "
                f"  AND s.category_id IN ({placeholders}) "
                f"ORDER BY a.name, c.name",
                tuple(descendants),
            ).fetchall()
            if orphaned:
                labels = ", ".join(
                    f"{r['acct']} — {r['cat']}"
                    + (f" ({r['payee']})" if r["payee"] else "")
                    for r in orphaned[:3]
                )
                extra = (
                    "" if len(orphaned) <= 3
                    else f" (+{len(orphaned) - 3} more)"
                )
                one = len(orphaned) == 1
                raise ValueError(
                    f"{len(orphaned)} active schedule"
                    f"{'' if one else 's'} use{'s' if one else ''} this "
                    f"category with no destination account: {labels}{extra}. A "
                    f"transfer-kind schedule needs a destination, so they "
                    f"would silently stop auto-posting. Set a destination on "
                    f"each (open Schedules), or pick a non-transfer kind."
                )
        try:
            self._conn.execute(
                f"UPDATE category SET kind = ? WHERE id IN ({placeholders})",
                (new_kind, *descendants),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def merge_categories(
        self, source_ids: list[int], target_id: int,
    ) -> int:
        """Re-point every transaction on the source categories to the target,
        then delete the source rows. Atomic — either everything moves or
        nothing does.

        Rejects sources with subcategories (to avoid the cascading sibling-
        name collision morass on children — see ADR-013) and rejects
        cross-kind merges (ADR-014: the user has to convert kinds explicitly
        via reparent first; otherwise a merge would silently change how the
        merged-from transactions are interpreted in reports)."""
        sources = [sid for sid in source_ids if sid != target_id]
        if not sources:
            return 0
        target_kind = self.get_category_kind(target_id)
        if target_kind is None:
            raise ValueError(f"No category with id {target_id}")
        for sid in sources:
            if self.category_has_children(sid):
                row = self._conn.execute(
                    "SELECT name FROM category WHERE id = ?", (sid,),
                ).fetchone()
                label = row["name"] if row else f"id={sid}"
                raise ValueError(
                    f"Category {label!r} has subcategories — reparent or "
                    f"merge them first, then try again."
                )
            sk = self.get_category_kind(sid)
            if sk != target_kind:
                row = self._conn.execute(
                    "SELECT name FROM category WHERE id = ?", (sid,),
                ).fetchone()
                label = row["name"] if row else f"id={sid}"
                raise ValueError(
                    f"Category {label!r} is a {sk} category but the target "
                    f"is a {target_kind} category. Convert the source's "
                    f"kind first by reparenting it under the right root, "
                    f"or pick a different target."
                )
        # Capture each source's full path *before* it's deleted, so a later
        # import of that name reroutes to the target instead of recreating the
        # category (ADR-112). Merge rejects sources with children, so a source's
        # own path is the only one to record.
        source_paths = {sid: self._category_path_string(sid) for sid in sources}
        placeholders = ",".join("?" * len(sources))
        try:
            cur = self._conn.execute(
                f"UPDATE txn SET category_id = ? "
                f"WHERE category_id IN ({placeholders})",
                (target_id, *sources),
            )
            moved = cur.rowcount
            # Split lines reference categories too (ADR-051); re-point them onto
            # the target as well, otherwise the FK on txn_split.category_id would
            # block the DELETE below (it has no ON DELETE action, by design).
            self._conn.execute(
                f"UPDATE txn_split SET category_id = ? "
                f"WHERE category_id IN ({placeholders})",
                (target_id, *sources),
            )
            # Any existing mapping that targeted a source would cascade-delete
            # with it; repoint those onto the merge target so the chain holds.
            self._conn.execute(
                f"UPDATE category_import_map SET target_category_id = ? "
                f"WHERE target_category_id IN ({placeholders})",
                (target_id, *sources),
            )
            self._conn.execute(
                f"DELETE FROM category WHERE id IN ({placeholders})",
                tuple(sources),
            )
            for sid in sources:
                self._record_category_import_mapping(source_paths[sid], target_id)
            self.commit()
            return moved
        except Exception:
            self.rollback()
            raise

    def _category_ancestors(self, category_id: int) -> set[int]:
        """All ids on the path from `category_id` up to the root (inclusive).
        Mirror of :pymeth:`category_descendants` walking the other way."""
        cur = self._conn.execute(
            "WITH RECURSIVE a(id, parent_id) AS ("
            "  SELECT id, parent_id FROM category WHERE id = ? "
            "  UNION ALL "
            "  SELECT c.id, c.parent_id FROM category c JOIN a ON c.id = a.parent_id"
            ") SELECT id FROM a",
            (category_id,),
        )
        return {r["id"] for r in cur}

    def archive_category(self, category_id: int) -> int:
        """Soft-archive (hide) a category and its whole subtree (ADR-070).

        Non-destructive — the rows, their kind/parent links, and every
        transaction's ``category_id`` are untouched, so the archived
        category's history still aggregates in the flow reports. An archived
        category just drops out of the pickers, the dialog's default view, and
        the budget setup. Cascades to descendants so a whole branch hides in
        one action (and the tree never holds an active child under an archived
        parent). Rejects the reserved Uncategorised row. Returns the number of
        categories archived. Idempotent — already-archived rows keep their
        original timestamp."""
        if category_id == UNCATEGORISED_ID:
            raise ValueError(
                "The Uncategorised category is the reserved fallback and "
                "cannot be archived."
            )
        ids = self.category_descendants(category_id)
        placeholders = ",".join("?" * len(ids))
        try:
            cur = self._conn.execute(
                f"UPDATE category SET archived_at = datetime('now') "
                f"WHERE id IN ({placeholders}) AND archived_at IS NULL",
                tuple(ids),
            )
            self.commit()
            return cur.rowcount
        except Exception:
            self.rollback()
            raise

    def unarchive_category(self, category_id: int) -> int:
        """Reverse :pymeth:`archive_category` (ADR-070) — clear ``archived_at``
        on the category, its whole subtree, *and* every ancestor up to the
        root, so the restored category is always reachable in the tree even if
        it sat inside a previously-archived branch. Returns the number of
        categories restored."""
        ids = (
            self.category_descendants(category_id)
            | self._category_ancestors(category_id)
        )
        placeholders = ",".join("?" * len(ids))
        try:
            cur = self._conn.execute(
                f"UPDATE category SET archived_at = NULL "
                f"WHERE id IN ({placeholders}) AND archived_at IS NOT NULL",
                tuple(ids),
            )
            self.commit()
            return cur.rowcount
        except Exception:
            self.rollback()
            raise

    def delete_category(self, category_id: int) -> int:
        """Delete a category. Rejects the reserved Uncategorised row and any
        category that still has subcategories (force reparent first).
        Transactions that referenced this category are reassigned to
        Uncategorised before the row is removed. Returns the count of
        reassigned transactions."""
        if category_id == UNCATEGORISED_ID:
            raise ValueError(
                "The Uncategorised category is the reserved fallback and "
                "cannot be deleted."
            )
        if self.category_has_children(category_id):
            raise ValueError(
                "This category has subcategories. Reparent or delete them "
                "first."
            )
        txn_count = self.count_category_transactions(category_id)
        # Record the deleted path → Needs Review before the row goes, so a later
        # import of that name parks for triage instead of recreating it (ADR-112).
        deleted_path = self._category_path_string(category_id)
        review_id = self.needs_review_category_id()
        try:
            if txn_count > 0:
                self._conn.execute(
                    "UPDATE txn SET category_id = ? WHERE category_id = ?",
                    (UNCATEGORISED_ID, category_id),
                )
            # Split lines reference categories too (ADR-051); reassign any to
            # Uncategorised before the row is removed, or the txn_split FK (no
            # ON DELETE action, by design) would block the DELETE.
            self._conn.execute(
                "UPDATE txn_split SET category_id = ? WHERE category_id = ?",
                (UNCATEGORISED_ID, category_id),
            )
            # Mappings pointing at this category would cascade-delete with it;
            # repoint them to Needs Review (matching where its path now routes),
            # but never let Needs Review map to itself.
            if review_id != category_id:
                self._conn.execute(
                    "UPDATE category_import_map SET target_category_id = ? "
                    "WHERE target_category_id = ?",
                    (review_id, category_id),
                )
            self._conn.execute(
                "DELETE FROM category WHERE id = ?", (category_id,),
            )
            if review_id != category_id:
                self._record_category_import_mapping(deleted_path, review_id)
            self.commit()
            return txn_count
        except Exception:
            self.rollback()
            raise

    # ── Payee ──

    def get_or_create_payee(self, name: str) -> Optional[int]:
        name = (name or "").strip()
        if not name:
            return None
        row = self._conn.execute(
            "SELECT id FROM payee WHERE name = ?", (name,),
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = self._conn.execute(
            "INSERT INTO payee (name) VALUES (?)", (name,),
        )
        return cur.lastrowid

    # ── Securities (ADR-043) ──

    # Instrument-metadata columns set on a security create/edit (ADR-093). The
    # value-math column ``price_multiplier`` plus the descriptive per-class
    # fields. ``instrument_type`` is handled separately (it has a NOT NULL
    # default, so it's only written when supplied).
    _INSTRUMENT_META_COLS = (
        "price_multiplier", "face_value", "coupon_rate", "maturity_date",
        "cusip", "underlying_symbol", "strike", "expiry_date", "option_type",
        "contract_size",
    )

    def get_or_create_security(
        self, name: str, symbol: str = "", type_: str = "",
        *,
        instrument_type: Optional[str] = None,
        price_multiplier: Optional[float] = None,
        face_value: Optional[float] = None,
        coupon_rate: Optional[float] = None,
        maturity_date: Optional[str] = None,
        cusip: Optional[str] = None,
        underlying_symbol: Optional[str] = None,
        strike: Optional[float] = None,
        expiry_date: Optional[str] = None,
        option_type: Optional[str] = None,
        contract_size: Optional[float] = None,
    ) -> Optional[int]:
        """Upsert a security by its (unique) name — the QIF `Y` reference.

        Returns the security id, or None if `name` is blank (a cash-only
        action with no instrument). When the security already exists, a
        symbol/type supplied here backfills any blank columns (so a later
        import that carries the ticker can fill in one mastered earlier
        without it) but never overwrites a value already on file.

        The instrument-class kwargs (ADR-093) are applied **on create only**;
        editing an existing security's class/metadata goes through
        ``update_security`` (the dialog has the id by then). Omitted (None) ⇒
        the column keeps its schema default (stock / 1.0 / NULL).
        """
        clean = (name or "").strip()
        if not clean:
            return None
        clean_symbol = (symbol or "").strip()
        clean_type = (type_ or "").strip()
        # Some QIF exports put the security's full name in the `S` (symbol)
        # field rather than a ticker. That's not a real ticker, so treat a
        # symbol that just repeats the name as blank (the price-feed key in a
        # later round needs a real ticker, and the register's Symbol column
        # should stay empty rather than echo the name).
        if clean_symbol.casefold() == clean.casefold():
            clean_symbol = ""
        row = self._conn.execute(
            "SELECT id, symbol, type FROM security WHERE name = ?", (clean,),
        ).fetchone()
        if row is not None:
            if clean_symbol and not (row["symbol"] or "").strip():
                self._conn.execute(
                    "UPDATE security SET symbol = ? WHERE id = ?",
                    (clean_symbol, row["id"]),
                )
            if clean_type and not (row["type"] or "").strip():
                self._conn.execute(
                    "UPDATE security SET type = ? WHERE id = ?",
                    (clean_type, row["id"]),
                )
            return row["id"]
        # Base columns + any instrument-class metadata supplied on create.
        cols = ["iri", "name", "symbol", "type"]
        vals: list = [new_security_iri(), clean, clean_symbol or None,
                      clean_type or None]
        if instrument_type is not None:
            cols.append("instrument_type")
            vals.append(instrument_type)
        meta = {
            "price_multiplier": price_multiplier, "face_value": face_value,
            "coupon_rate": coupon_rate, "maturity_date": maturity_date,
            "cusip": cusip, "underlying_symbol": underlying_symbol,
            "strike": strike, "expiry_date": expiry_date,
            "option_type": option_type, "contract_size": contract_size,
        }
        for col in self._INSTRUMENT_META_COLS:
            if meta[col] is not None:
                cols.append(col)
                vals.append(meta[col])
        placeholders = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT INTO security ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        return cur.lastrowid

    # The full security column list (ADR-093) — instrument class + metadata, for
    # the queries that feed the transaction/stock-record dialogs. The Tiingo
    # pricing queries select a narrower set and leave the metadata at defaults.
    _SECURITY_COLS = (
        "id, iri, name, COALESCE(symbol, '') AS symbol, "
        "COALESCE(type, '') AS type, "
        "instrument_type, price_multiplier, face_value, coupon_rate, "
        "maturity_date, cusip, underlying_symbol, strike, expiry_date, "
        "option_type, contract_size"
    )

    @staticmethod
    def _security_row(r) -> SecurityRow:
        """Build a fully-populated SecurityRow from a row selected with
        ``_SECURITY_COLS`` (ADR-093)."""
        return SecurityRow(
            id=r["id"], iri=r["iri"], name=r["name"],
            symbol=r["symbol"], type=r["type"],
            instrument_type=r["instrument_type"] or "stock",
            price_multiplier=(
                r["price_multiplier"] if r["price_multiplier"] is not None else 1.0
            ),
            face_value=r["face_value"], coupon_rate=r["coupon_rate"],
            maturity_date=r["maturity_date"], cusip=r["cusip"],
            underlying_symbol=r["underlying_symbol"], strike=r["strike"],
            expiry_date=r["expiry_date"], option_type=r["option_type"],
            contract_size=r["contract_size"],
        )

    def list_securities(self) -> list[SecurityRow]:
        """Every non-archived security, sorted by name."""
        cur = self._conn.execute(
            f"SELECT {self._SECURITY_COLS} "
            "FROM security WHERE archived_at IS NULL "
            "ORDER BY name COLLATE NOCASE"
        )
        return [self._security_row(r) for r in cur]

    def get_security(self, security_id: int) -> Optional[SecurityRow]:
        """One fully-populated security by id (ADR-093), or None if missing."""
        r = self._conn.execute(
            f"SELECT {self._SECURITY_COLS} FROM security WHERE id = ?",
            (security_id,),
        ).fetchone()
        return self._security_row(r) if r is not None else None

    def security_multipliers(self) -> dict[int, float]:
        """security_id → price_multiplier for every security (ADR-093). The
        holdings engine multiplies each ``shares × price`` value site by this so
        a bond (face/100) or option (contract_size) values correctly; a stock is
        1.0. Securities absent from the map default to 1.0 in the engine."""
        cur = self._conn.execute(
            "SELECT id, price_multiplier FROM security"
        )
        return {
            r["id"]: (r["price_multiplier"] if r["price_multiplier"] is not None else 1.0)
            for r in cur
        }

    def list_securities_with_symbol(self) -> list[SecurityRow]:
        """Non-archived securities that carry a ticker symbol — the ones a
        price provider (Tiingo) can look up. Securities with no symbol are
        manual-price only (ADR-044)."""
        return [s for s in self.list_securities() if (s.symbol or "").strip()]

    def list_securities_for_accounts(
        self, account_ids: list[int],
    ) -> list[SecurityRow]:
        """Distinct securities referenced by investment transactions in the
        given accounts, sorted by name. Feeds the Investment Returns report's
        security filter (ADR-046) so it lists only securities actually held in
        the selected accounts. Empty ``account_ids`` returns []."""
        if not account_ids:
            return []
        placeholders = ",".join("?" for _ in account_ids)
        cur = self._conn.execute(
            "SELECT DISTINCT "
            "  s.id, s.iri, s.name, COALESCE(s.symbol, '') AS symbol, "
            "  COALESCE(s.type, '') AS type, "
            "  s.instrument_type, s.price_multiplier, s.face_value, "
            "  s.coupon_rate, s.maturity_date, s.cusip, s.underlying_symbol, "
            "  s.strike, s.expiry_date, s.option_type, s.contract_size "
            "FROM security s "
            "JOIN txn t ON t.security_id = s.id "
            f"WHERE t.account_id IN ({placeholders}) "
            "  AND s.archived_at IS NULL "
            "ORDER BY s.name COLLATE NOCASE",
            list(account_ids),
        )
        return [self._security_row(r) for r in cur]

    def update_security(
        self, security_id: int, *,
        name: Optional[str] = None,
        symbol: Optional[str] = None,
        type_: Optional[str] = None,
        instrument_type: Optional[str] = None,
        price_multiplier=_UNSET,
        face_value=_UNSET,
        coupon_rate=_UNSET,
        maturity_date=_UNSET,
        cusip=_UNSET,
        underlying_symbol=_UNSET,
        strike=_UNSET,
        expiry_date=_UNSET,
        option_type=_UNSET,
        contract_size=_UNSET,
    ) -> None:
        """Edit a security's master fields (ADR-047 Stock Record, ADR-093
        instrument class/metadata).

        For ``name`` / ``symbol`` / ``type_`` / ``instrument_type``: ``None``
        means 'leave unchanged'; pass an empty string for ``symbol`` / ``type_``
        to clear it to NULL. For the instrument-metadata kwargs (ADR-093) the
        convention is the class-level ``_UNSET`` sentinel — omit to leave the
        column untouched, pass ``None`` to clear it, a value to set it (so a
        bond→stock switch can null the bond columns out). Setting a symbol on a
        previously untickered security re-enables Tiingo for it. Raises
        ``ValueError`` on a blank name or a name that collides with another
        security. Commits.
        """
        sets: list[str] = []
        params: list = []
        if name is not None:
            clean = name.strip()
            if not clean:
                raise ValueError("Security name can't be blank.")
            dup = self._conn.execute(
                "SELECT id FROM security WHERE name = ? AND id != ?",
                (clean, security_id),
            ).fetchone()
            if dup is not None:
                raise ValueError(
                    f"Another security is already named {clean!r}."
                )
            sets.append("name = ?")
            params.append(clean)
        if symbol is not None:
            sets.append("symbol = ?")
            params.append(symbol.strip() or None)
        if type_ is not None:
            sets.append("type = ?")
            params.append(type_.strip() or None)
        if instrument_type is not None:
            sets.append("instrument_type = ?")
            params.append(instrument_type)
        # Instrument metadata (ADR-093): _UNSET = leave, None = clear, else set.
        meta = {
            "price_multiplier": price_multiplier, "face_value": face_value,
            "coupon_rate": coupon_rate, "maturity_date": maturity_date,
            "cusip": cusip, "underlying_symbol": underlying_symbol,
            "strike": strike, "expiry_date": expiry_date,
            "option_type": option_type, "contract_size": contract_size,
        }
        for col in self._INSTRUMENT_META_COLS:
            value = meta[col]
            if value is not self._UNSET:
                sets.append(f"{col} = ?")
                # price_multiplier is NOT NULL — a cleared (None) multiplier
                # falls back to the stock default of 1.0.
                if col == "price_multiplier" and value is None:
                    value = 1.0
                params.append(value)
        if not sets:
            return
        params.append(security_id)
        self._conn.execute(
            f"UPDATE security SET {', '.join(sets)} WHERE id = ?", params,
        )
        self.commit()

    def merge_securities(self, source_ids: list[int], target_id: int) -> int:
        """Merge one or more securities into ``target_id`` (ADR-052).

        Every transaction and stored price on a source security is re-pointed
        to the target, then the source ``security`` rows are deleted. Use this
        to collapse duplicate records that describe the same instrument — e.g.
        a fund imported under two names ("TIAA CREF CORE IMPACT BD INST" and
        "Nuveen Core Impact Bond R6", both ticker TSBIX) because
        ``security.name`` is unique and the importer keys on it.

        Price collisions are resolved by the ADR-047 source precedence
        (manual > tiingo > transaction): when a source and the target both
        hold a price on the same date, the higher-precedence source wins and a
        tie keeps the target's existing row. The comparison is rank-based, so
        the outcome doesn't depend on the order rows are merged in.

        ``target_id`` is excluded from ``source_ids`` defensively. Returns the
        count of transactions re-pointed. The repoint + delete run in a single
        SQL transaction — either the whole merge lands or nothing changes.
        """
        sources = [sid for sid in source_ids if sid != target_id]
        if not sources:
            return 0
        if self._conn.execute(
            "SELECT 1 FROM security WHERE id = ?", (target_id,),
        ).fetchone() is None:
            raise ValueError(f"No security with id {target_id}")
        placeholders = ",".join("?" * len(sources))
        try:
            cur = self._conn.execute(
                f"UPDATE txn SET security_id = ? "
                f"WHERE security_id IN ({placeholders})",
                (target_id, *sources),
            )
            moved = cur.rowcount
            # Move the sources' prices onto the target. security_price's PK is
            # (security_id, price_date), so a plain UPDATE would collide on any
            # date both records hold — instead INSERT…SELECT and let the
            # conflict clause keep the higher-precedence price.
            self._conn.execute(
                f"INSERT INTO security_price "
                f"(security_id, price_date, price, currency, source) "
                f"SELECT ?, price_date, price, currency, source "
                f"FROM security_price WHERE security_id IN ({placeholders}) "
                f"ON CONFLICT(security_id, price_date) DO UPDATE SET "
                f"  price = excluded.price, currency = excluded.currency, "
                f"  source = excluded.source "
                f"WHERE (CASE excluded.source WHEN 'manual' THEN 3 "
                f"         WHEN 'tiingo' THEN 2 ELSE 1 END) "
                f"    > (CASE security_price.source WHEN 'manual' THEN 3 "
                f"         WHEN 'tiingo' THEN 2 ELSE 1 END)",
                (target_id, *sources),
            )
            # Drop the now-copied source price rows (the winners are on target).
            self._conn.execute(
                f"DELETE FROM security_price WHERE security_id IN ({placeholders})",
                tuple(sources),
            )
            self._conn.execute(
                f"DELETE FROM security WHERE id IN ({placeholders})",
                tuple(sources),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        # A trade moved onto an untickered survivor can now seed a transaction
        # price; seeding skips tickered securities, so this is a no-op when the
        # survivor carries a symbol. Its own commit, outside the merge txn.
        self.seed_prices_from_transactions(security_ids=[target_id])
        return moved

    # ── Security prices (ADR-044) ──

    # Price-source precedence (ADR-047): manual > tiingo > transaction. The
    # guard is the `WHERE` appended to an upsert's DO UPDATE so a write of a
    # given source never overwrites a price from a higher-priority source on the
    # same date. A manual entry (explicit user action) overwrites anything.
    _PRICE_OVERWRITE_GUARD = {
        "manual": "",
        "tiingo": " WHERE security_price.source != 'manual'",
        "transaction": " WHERE security_price.source NOT IN ('manual', 'tiingo')",
    }

    def upsert_security_price(
        self, *,
        security_id: int,
        price_date: str,
        price: float,
        source: str = "manual",
        currency: Optional[str] = None,
    ) -> None:
        """Insert or replace one security's price on a date. Mirrors
        upsert_fx_rate. Commits immediately.

        Honours the source-precedence rule (ADR-047): a ``tiingo`` write won't
        clobber a ``manual`` price, and a ``transaction``-derived write won't
        clobber a ``manual`` or ``tiingo`` price, on the same date. ``manual``
        (the Stock Record / Securities-dialog entry path) always wins."""
        guard = self._PRICE_OVERWRITE_GUARD.get(source, "")
        self._conn.execute(
            "INSERT INTO security_price "
            "(security_id, price_date, price, currency, source) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(security_id, price_date) DO UPDATE SET "
            "  price = excluded.price, currency = excluded.currency, "
            "  source = excluded.source" + guard,
            (security_id, price_date, float(price), currency, source),
        )
        self.commit()

    def latest_prices(self) -> dict[int, PriceRow]:
        """Most recent price point per security (ADR-044), keyed by
        security_id. Uses the (security_id, price_date DESC) index."""
        cur = self._conn.execute(
            "SELECT sp.security_id, sp.price_date, sp.price, "
            "       COALESCE(sp.currency, '') AS currency, sp.source "
            "FROM security_price sp "
            "JOIN (SELECT security_id, MAX(price_date) AS md "
            "      FROM security_price GROUP BY security_id) latest "
            "  ON latest.security_id = sp.security_id "
            " AND latest.md = sp.price_date"
        )
        return {
            r["security_id"]: PriceRow(
                security_id=r["security_id"], price_date=r["price_date"],
                price=r["price"], currency=r["currency"], source=r["source"],
            )
            for r in cur
        }

    def latest_price_for_security(self, security_id: int) -> Optional[PriceRow]:
        row = self._conn.execute(
            "SELECT security_id, price_date, price, "
            "       COALESCE(currency, '') AS currency, source "
            "FROM security_price WHERE security_id = ? "
            "ORDER BY price_date DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        if row is None:
            return None
        return PriceRow(
            security_id=row["security_id"], price_date=row["price_date"],
            price=row["price"], currency=row["currency"], source=row["source"],
        )

    def bulk_upsert_security_prices(
        self, rows: list[tuple[int, str, float, str]],
    ) -> None:
        """Upsert many (security_id, price_date, price, source) rows in one
        transaction — the historical-backfill path (ADR-045), which would
        otherwise pay a commit per day.

        This is the Tiingo backfill path; per the source-precedence rule
        (ADR-047) the conflict clause leaves a ``manual`` price on a given date
        untouched (a user-typed price out-ranks a provider fetch), but does
        overwrite ``tiingo`` and ``transaction`` rows."""
        if not rows:
            return
        self._conn.executemany(
            "INSERT INTO security_price "
            "(security_id, price_date, price, currency, source) "
            "VALUES (?, ?, ?, NULL, ?) "
            "ON CONFLICT(security_id, price_date) DO UPDATE SET "
            "  price = excluded.price, source = excluded.source "
            "  WHERE security_price.source != 'manual'",
            [(sid, d, float(p), src) for sid, d, p, src in rows],
        )
        self.commit()

    def price_series(self, security_id: int) -> list[PriceRow]:
        """Every stored price for a security, ascending by date (ADR-045).
        Loaded once per security so a valuation pass can do in-memory
        nearest-prior lookups rather than a query per sample date."""
        cur = self._conn.execute(
            "SELECT security_id, price_date, price, "
            "       COALESCE(currency, '') AS currency, source "
            "FROM security_price WHERE security_id = ? "
            "ORDER BY price_date ASC",
            (security_id,),
        )
        return [
            PriceRow(
                security_id=r["security_id"], price_date=r["price_date"],
                price=r["price"], currency=r["currency"], source=r["source"],
            )
            for r in cur
        ]

    def get_security_price_nearest(
        self, security_id: int, on_date: str,
    ) -> Optional[PriceRow]:
        """Nearest price on or before ``on_date`` (mirrors get_fx_rate_nearest's
        nearest-prior step). Used for point valuations."""
        row = self._conn.execute(
            "SELECT security_id, price_date, price, "
            "       COALESCE(currency, '') AS currency, source "
            "FROM security_price WHERE security_id = ? AND price_date <= ? "
            "ORDER BY price_date DESC LIMIT 1",
            (security_id, on_date),
        ).fetchone()
        if row is None:
            return None
        return PriceRow(
            security_id=row["security_id"], price_date=row["price_date"],
            price=row["price"], currency=row["currency"], source=row["source"],
        )

    def delete_security_price(self, security_id: int, price_date: str) -> None:
        """Remove one stored price point (ADR-047, Stock Record screen). No-op
        if absent. Commits."""
        self._conn.execute(
            "DELETE FROM security_price WHERE security_id = ? AND price_date = ?",
            (security_id, price_date),
        )
        self.commit()

    def securities_missing_history(
        self, min_points: int = 2, *,
        cooldown_days: int = 30, as_of: Optional[date] = None,
    ) -> list[SecurityRow]:
        """Tickered, non-archived securities with fewer than ``min_points``
        *real* stored price rows — i.e. never properly backfilled. Feeds the
        launch-time auto-backfill (ADR-047) so a newly tickered or freshly
        imported security gets its full Tiingo history without anyone clicking
        'Backfill history'.

        Only ``manual`` / ``tiingo`` rows count as "history"; ``transaction``-
        derived prices (the sparse per-trade points seeded for untickered
        holdings) do NOT — so when a previously untickered security is given a
        ticker, its handful of trade-derived prices don't mask it from the
        backfill, and the real end-of-day series is fetched on the next launch.

        Two request-waste filters (ADR-049):
        - **Orphan skip** — only securities referenced by at least one ``txn``
          are returned. Securities imported via a QIF ``!Type:Security`` block
          for accounts not yet migrated carry no transactions, contribute to no
          holding/value/return, and must not be fetched.
        - **Give-up cooldown** — a security whose last Tiingo fetch failed
          ("not covered") within ``cooldown_days`` is skipped, so an uncovered
          ticker isn't re-fetched on every launch. After the window it's retried
          automatically (a ticker Tiingo starts covering heals itself)."""
        ref = as_of or date.today()
        cutoff = (ref - timedelta(days=cooldown_days)).isoformat()
        cur = self._conn.execute(
            "SELECT s.id, s.iri, s.name, "
            "       COALESCE(s.symbol, '') AS symbol, "
            "       COALESCE(s.type, '') AS type, "
            # Buffered first-transaction date — the backfill floor (ADR-049
            # amendment). 7 days before the first trade so a nearest-prior
            # price lookup around the buy date still resolves.
            "       (SELECT date(MIN(t.posted_date), '-7 days') "
            "          FROM txn t WHERE t.security_id = s.id) AS earliest_txn "
            "FROM security s "
            "LEFT JOIN security_price sp ON sp.security_id = s.id "
            "WHERE s.archived_at IS NULL AND COALESCE(s.symbol, '') != '' "
            "  AND EXISTS (SELECT 1 FROM txn t WHERE t.security_id = s.id) "
            "  AND (s.price_fetch_failed_at IS NULL "
            "       OR s.price_fetch_failed_at < ?) "
            "GROUP BY s.id "
            "HAVING SUM(CASE WHEN sp.source IN ('manual', 'tiingo') "
            "                THEN 1 ELSE 0 END) < ? "
            "ORDER BY s.name COLLATE NOCASE",
            (cutoff, min_points),
        )
        return [
            SecurityRow(
                id=r["id"], iri=r["iri"], name=r["name"],
                symbol=r["symbol"], type=r["type"],
                earliest_txn_date=r["earliest_txn"],
            )
            for r in cur
        ]

    def securities_to_price(
        self, *, cooldown_days: int = 30, as_of: Optional[date] = None,
    ) -> list[SecurityRow]:
        """Tickered, non-archived securities worth fetching prices for (ADR-049)
        — the latest-refresh and full-backfill input. Same two waste filters as
        ``securities_missing_history``: must have at least one ``txn`` (skip
        orphan securities from un-migrated accounts) and must not be inside the
        give-up cooldown after an uncovered-ticker failure. Unlike
        ``securities_missing_history`` it does NOT care how many prices are
        already stored — the latest-close sweep refreshes every held security
        once a day.

        ``list_securities_with_symbol`` keeps its broader "every tickered
        security" meaning for non-pricing callers; this is the pricing-only
        view."""
        ref = as_of or date.today()
        cutoff = (ref - timedelta(days=cooldown_days)).isoformat()
        cur = self._conn.execute(
            "SELECT s.id, s.iri, s.name, "
            "       COALESCE(s.symbol, '') AS symbol, "
            "       COALESCE(s.type, '') AS type, "
            # Buffered first-transaction date — the backfill floor (ADR-049
            # amendment); see securities_missing_history for the rationale.
            "       (SELECT date(MIN(t.posted_date), '-7 days') "
            "          FROM txn t WHERE t.security_id = s.id) AS earliest_txn "
            "FROM security s "
            "WHERE s.archived_at IS NULL AND COALESCE(s.symbol, '') != '' "
            "  AND EXISTS (SELECT 1 FROM txn t WHERE t.security_id = s.id) "
            "  AND (s.price_fetch_failed_at IS NULL "
            "       OR s.price_fetch_failed_at < ?) "
            "ORDER BY s.name COLLATE NOCASE",
            (cutoff,),
        )
        return [
            SecurityRow(
                id=r["id"], iri=r["iri"], name=r["name"],
                symbol=r["symbol"], type=r["type"],
                earliest_txn_date=r["earliest_txn"],
            )
            for r in cur
        ]

    def securities_with_incomplete_history(
        self, *, min_points: int = 2, min_span_days: int = 30,
        staleness_days: int = 7, cooldown_days: int = 30,
        as_of: Optional[date] = None,
    ) -> list[SecurityRow]:
        """Tickered, transacted, non-given-up securities whose stored real
        (``manual`` / ``tiingo``) history is missing, only a sliver, or stale —
        i.e. the ones a full backfill still has work to do for. Feeds the manual
        'Backfill history' button so a re-click only spends Tiingo requests on
        securities that need them; a complete, up-to-date series costs zero
        (ADR-049 follow-up — the lighter cousin of a request-budget tracker).

        A security is returned when ANY of:

        - **too few points** — fewer than ``min_points`` real prices (never
          properly backfilled);
        - **sliver of history** — its real prices span fewer than
          ``min_span_days`` days *despite* being owned longer than that. This
          catches the latest-close stragglers left by the ADR-049 backfill bug
          (a handful of recent closes accumulated by the daily sweep, clearing
          ``min_points`` but only days long) WITHOUT flagging a fund whose long
          recent series simply can't reach an old purchase date because Tiingo
          lacks the early data — re-fetching that is futile, so it's left alone;
        - **stale** — its latest real price is older than ``staleness_days``
          (the user returning after an absence; a re-fetch fills the gap). In
          normal use the daily latest-close sweep keeps this fresh, so this arm
          stays quiet and a re-click is still free.

        Same orphan-skip + give-up-cooldown waste filters as
        ``securities_missing_history``."""
        ref = as_of or date.today()
        cutoff = (ref - timedelta(days=cooldown_days)).isoformat()
        stale_before = (ref - timedelta(days=staleness_days)).isoformat()
        today = ref.isoformat()
        real = "CASE WHEN sp.source IN ('manual', 'tiingo') THEN 1 ELSE 0 END"
        real_date = (
            "CASE WHEN sp.source IN ('manual', 'tiingo') "
            "THEN sp.price_date END"
        )
        cur = self._conn.execute(
            "SELECT s.id, s.iri, s.name, "
            "       COALESCE(s.symbol, '') AS symbol, "
            "       COALESCE(s.type, '') AS type, "
            "       (SELECT date(MIN(t.posted_date), '-7 days') "
            "          FROM txn t WHERE t.security_id = s.id) AS earliest_txn "
            "FROM security s "
            "LEFT JOIN security_price sp ON sp.security_id = s.id "
            "WHERE s.archived_at IS NULL AND COALESCE(s.symbol, '') != '' "
            "  AND EXISTS (SELECT 1 FROM txn t WHERE t.security_id = s.id) "
            "  AND (s.price_fetch_failed_at IS NULL "
            "       OR s.price_fetch_failed_at < ?) "
            "GROUP BY s.id "
            f"HAVING SUM({real}) < ? "
            f"    OR MIN({real_date}) IS NULL "
            # sliver: stored span shorter than min_span_days while owned longer
            f"    OR ( julianday(MAX({real_date})) - julianday(MIN({real_date})) "
            "           < ? "
            "         AND julianday(?) - julianday(earliest_txn) >= ? ) "
            f"    OR MAX({real_date}) < ? "
            "ORDER BY s.name COLLATE NOCASE",
            (cutoff, min_points, min_span_days, today, min_span_days,
             stale_before),
        )
        return [
            SecurityRow(
                id=r["id"], iri=r["iri"], name=r["name"],
                symbol=r["symbol"], type=r["type"],
                earliest_txn_date=r["earliest_txn"],
            )
            for r in cur
        ]

    def earliest_transaction_date(
        self, security_id: int, *, buffer_days: int = 7,
    ) -> Optional[str]:
        """Buffered date ('YYYY-MM-DD') of a security's first transaction — the
        floor for a single-security history backfill (ADR-049 amendment, the
        Stock Record 'Fetch from Tiingo' path). ``buffer_days`` before the first
        trade so a nearest-prior price lookup around the buy date resolves.
        Returns ``None`` when the security has no transactions (caller then
        falls back to the far-past default)."""
        row = self._conn.execute(
            "SELECT date(MIN(posted_date), ?) AS d "
            "FROM txn WHERE security_id = ?",
            (f"-{int(buffer_days)} days", security_id),
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def securities_currently_held(self, *, eps: float = 1e-6) -> set[int]:
        """Security ids with a net open share position (> 0) across all accounts
        — backs the 'Show only held securities' filter on the Securities dialog.

        Net shares = Σ share-in quantities − Σ share-out quantities, classified
        with the shared ``qif_actions`` predicates (the same sets the holdings
        engine and the QIF importer use) so this can't drift from them. Excludes
        fully-sold positions (net ~0) and never-held orphan securities. Stock
        splits aren't applied — consistent with ``compute_holdings_view`` — so a
        post-split holding is still correctly reported as held (sign, not
        magnitude, is what matters here)."""
        from mfl_desktop.import_engine.qif_actions import (
            is_share_in, is_share_out,
        )
        net: dict[int, float] = {}
        for r in self._conn.execute(
            "SELECT security_id, action, quantity FROM txn "
            "WHERE security_id IS NOT NULL AND action IS NOT NULL "
            "  AND quantity IS NOT NULL"
        ):
            qty = float(r["quantity"] or 0.0)
            if qty <= 0:
                continue
            sid = r["security_id"]
            if is_share_in(r["action"]):
                net[sid] = net.get(sid, 0.0) + qty
            elif is_share_out(r["action"]):
                net[sid] = net.get(sid, 0.0) - qty
        return {sid for sid, q in net.items() if q > eps}

    def mark_security_price_unavailable(
        self, security_id: int, *, when: Optional[str] = None,
    ) -> None:
        """Record that Tiingo couldn't serve this security's history (ADR-049) —
        an HTTP 404 / unknown ticker or a successful-but-empty series. Stamps
        ``security.price_fetch_failed_at`` so the launch fetch paths skip it for
        the cooldown window. ``when`` is an ISO datetime (the fetch path passes
        its own UTC 'now'); defaults to today's date. Commits."""
        ts = when or date.today().isoformat()
        self._conn.execute(
            "UPDATE security SET price_fetch_failed_at = ? WHERE id = ?",
            (ts, security_id),
        )
        self.commit()

    def clear_security_price_unavailable(self, security_id: int) -> None:
        """Clear the give-up flag after a successful fetch (ADR-049). No-op when
        already clear (avoids a needless write). Commits."""
        self._conn.execute(
            "UPDATE security SET price_fetch_failed_at = NULL "
            "WHERE id = ? AND price_fetch_failed_at IS NOT NULL",
            (security_id,),
        )
        self.commit()

    def seed_prices_from_transactions(
        self, *, security_ids: Optional[list[int]] = None,
    ) -> int:
        """Seed ``security_price`` from the per-share price on investment trades
        of UNTICKERED securities (ADR-047).

        A Buy/Sell/ReinvDiv of a security with no ticker carries its own
        per-share price (``txn.price``) — the only price signal available for
        the holdings Tiingo can't fetch. For each such row we record
        ``(security_id, posted_date, price)`` with ``source='transaction'``.
        ``price IS NOT NULL`` naturally selects trades/reinvests and skips cash
        ``Div``/``Cash`` rows (which carry no price). Restricted to securities
        whose symbol is blank, so a tickered security's clean end-of-day Tiingo
        series is never polluted with an intraday execution price.

        Honours source precedence (manual > tiingo > transaction): the upsert
        only overwrites an existing ``transaction``-derived row, never a manual
        or Tiingo price on the same date. Idempotent — safe to run on every
        import and at launch. ``security_ids`` restricts the sweep to the
        just-imported securities (``None`` = all). Returns the affected row
        count. Commits.
        """
        sql = (
            "INSERT INTO security_price "
            "(security_id, price_date, price, currency, source) "
            "SELECT t.security_id, t.posted_date, t.price, NULL, 'transaction' "
            "FROM txn t "
            "JOIN security s ON s.id = t.security_id "
            "WHERE t.price IS NOT NULL AND t.price > 0 "
            "  AND COALESCE(s.symbol, '') = '' "
            "  AND s.archived_at IS NULL "
        )
        params: list = []
        if security_ids:
            placeholders = ",".join("?" for _ in security_ids)
            sql += f"  AND t.security_id IN ({placeholders}) "
            params.extend(security_ids)
        # When one security has several priced trades on the SAME day, the PK
        # collapses them; the last visited row wins. We don't ORDER BY (SQLite
        # forbids it on the SELECT feeding an upsert) — same-day, same-security
        # prices differ only intraday, which is below the resolution we store.
        sql += (
            "ON CONFLICT(security_id, price_date) DO UPDATE SET "
            "  price = excluded.price, source = 'transaction' "
            "  WHERE security_price.source = 'transaction'"
        )
        cur = self._conn.execute(sql, params)
        self.commit()
        return cur.rowcount

    def find_payee_id_by_name(self, name: str) -> Optional[int]:
        """Case-sensitive lookup. Returns None if no payee has that name."""
        clean = (name or "").strip()
        if not clean:
            return None
        row = self._conn.execute(
            "SELECT id FROM payee WHERE name = ?", (clean,),
        ).fetchone()
        return row["id"] if row is not None else None

    def list_payee_names(self) -> list[str]:
        """Canonical-payee names only, sorted case-insensitively. Feeds the
        register's payee typeahead delegate + the Bulk Edit dialog's
        completer. Per ADR-028 / ADR-029 round 1, aliases are deliberately
        hidden — the user is suggesting the preferred label, not the raw
        historical strings."""
        cur = self._conn.execute(
            "SELECT name FROM payee "
            "WHERE archived_at IS NULL AND canonical_id IS NULL "
            "ORDER BY name COLLATE NOCASE"
        )
        return [r["name"] for r in cur]

    def list_canonical_payees(self) -> list[tuple[int, str]]:
        """All canonical payees as ``(id, name)`` tuples, sorted. Used by
        the "Make alias of…" picker so the user can choose the target."""
        cur = self._conn.execute(
            "SELECT id, name FROM payee "
            "WHERE archived_at IS NULL AND canonical_id IS NULL "
            "ORDER BY name COLLATE NOCASE"
        )
        return [(r["id"], r["name"]) for r in cur]

    def expand_canonical_payee_ids(self, ids: list[int]) -> list[int]:
        """Return the input canonical payee ids plus the ids of every
        payee whose ``canonical_id`` is in the input.

        Used by report filters (ADR-039) to make "filter to canonical
        Tesco" automatically include historical alias rows. Round 1 of
        ADR-029 left ``txn.payee_id`` pointing at the raw alias; round 2
        will normalise to the canonical at import time, but the existing
        ledger still has alias-pointing txns until that lands. Empty
        input returns an empty list."""
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        cur = self._conn.execute(
            f"SELECT id FROM payee "
            f"WHERE id IN ({ph}) OR canonical_id IN ({ph})",
            [*ids, *ids],
        )
        return [int(r["id"]) for r in cur]

    def list_aliases_of(self, canonical_id: int) -> list[tuple[int, str]]:
        """Every alias pointing at ``canonical_id``, as ``(id, name)`` tuples."""
        cur = self._conn.execute(
            "SELECT id, name FROM payee "
            "WHERE canonical_id = ? AND archived_at IS NULL "
            "ORDER BY name COLLATE NOCASE",
            (canonical_id,),
        )
        return [(r["id"], r["name"]) for r in cur]

    def list_payees_with_usage(self) -> list[PayeeRow]:
        """All payees with usage counts + canonical/alias state, sorted.

        Returns one row per payee. For canonicals, ``usage_count`` is the
        **rolled-up** total (direct txns + every alias's direct txns) so
        the dialog can show "Tesco · 142" even when the txns are split
        across several aliases. For aliases, ``usage_count`` is the
        direct count only. ``direct_usage_count`` is always the direct
        count for callers that need to distinguish.

        Sort: canonicals alphabetically, with each canonical's aliases
        immediately after it (also alphabetical). Standalone canonicals
        (no aliases) and aliases of a standalone canonical interleave
        cleanly. Implementation: pull rows + direct counts in one query,
        do the rollup + sort in Python (payee table is small)."""
        cur = self._conn.execute(
            "SELECT p.id, p.name, p.canonical_id, p.default_category_id, "
            "       c.name AS canonical_name, "
            "       (SELECT COUNT(*) FROM txn t WHERE t.payee_id = p.id) "
            "           AS direct_cnt "
            "FROM      payee p "
            "LEFT JOIN payee c ON c.id = p.canonical_id "
            "WHERE p.archived_at IS NULL"
        )
        raw = list(cur)

        # Roll alias counts up to their canonical.
        rolled_extra: dict[int, int] = {}
        for r in raw:
            if r["canonical_id"] is not None:
                rolled_extra[r["canonical_id"]] = (
                    rolled_extra.get(r["canonical_id"], 0)
                    + int(r["direct_cnt"])
                )

        # Build PayeeRow list with rolled counts.
        by_id: dict[int, PayeeRow] = {}
        for r in raw:
            direct = int(r["direct_cnt"])
            is_canonical = r["canonical_id"] is None
            rolled = (
                direct + rolled_extra.get(r["id"], 0)
                if is_canonical else direct
            )
            by_id[r["id"]] = PayeeRow(
                id=r["id"],
                name=r["name"],
                usage_count=rolled,
                canonical_id=r["canonical_id"],
                canonical_name=r["canonical_name"],
                direct_usage_count=direct,
                default_category_id=r["default_category_id"],
            )

        # Display order: canonical, then its aliases. Group keys are the
        # canonical's name (case-insensitive) so the order matches what
        # the user sees alphabetically.
        canonicals = sorted(
            (p for p in by_id.values() if p.canonical_id is None),
            key=lambda p: p.name.lower(),
        )
        aliases_by_canonical: dict[int, list[PayeeRow]] = {}
        for p in by_id.values():
            if p.canonical_id is not None:
                aliases_by_canonical.setdefault(p.canonical_id, []).append(p)
        for lst in aliases_by_canonical.values():
            lst.sort(key=lambda p: p.name.lower())

        ordered: list[PayeeRow] = []
        for c in canonicals:
            ordered.append(c)
            ordered.extend(aliases_by_canonical.get(c.id, []))
        # Catch any orphan aliases whose canonical was archived (shouldn't
        # happen given delete_payees promotes them, but defensive).
        ordered_ids = {p.id for p in ordered}
        for p in by_id.values():
            if p.id not in ordered_ids:
                ordered.append(p)
        return ordered

    def set_alias_of(self, alias_id: int, canonical_id: int) -> None:
        """Make ``alias_id`` an alias of ``canonical_id``.

        Validates the two-level rule (ADR-028): the target must itself be
        canonical, and the source must not already have aliases of its
        own. ``alias_id == canonical_id`` is rejected. Commits on success.
        """
        if alias_id == canonical_id:
            raise ValueError("A payee can't be an alias of itself.")
        target = self._conn.execute(
            "SELECT canonical_id FROM payee WHERE id = ? AND archived_at IS NULL",
            (canonical_id,),
        ).fetchone()
        if target is None:
            raise ValueError(
                f"No active payee with id {canonical_id} to alias to."
            )
        if target["canonical_id"] is not None:
            raise ValueError(
                "Target is itself an alias — pick its canonical instead "
                "(aliases of aliases aren't allowed)."
            )
        source = self._conn.execute(
            "SELECT canonical_id FROM payee WHERE id = ? AND archived_at IS NULL",
            (alias_id,),
        ).fetchone()
        if source is None:
            raise ValueError(f"No active payee with id {alias_id}.")
        has_children = self._conn.execute(
            "SELECT 1 FROM payee WHERE canonical_id = ? LIMIT 1",
            (alias_id,),
        ).fetchone()
        if has_children is not None:
            raise ValueError(
                "This payee already has its own aliases — promote those "
                "first, or pick a different source."
            )
        try:
            self._conn.execute(
                "UPDATE payee SET canonical_id = ? WHERE id = ?",
                (canonical_id, alias_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def promote_to_canonical(self, payee_id: int) -> None:
        """Drop a payee's canonical link, making it its own canonical
        again. No-op for rows that were already canonical."""
        try:
            self._conn.execute(
                "UPDATE payee SET canonical_id = NULL WHERE id = ?",
                (payee_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Payee→category memory (ADR-072) ──

    def _canonical_id_for(self, payee_id: int) -> int:
        """Resolve a payee id to its canonical (itself, if already canonical).
        The auto-category memory lives on the canonical, so every read/write
        routes through here first."""
        row = self._conn.execute(
            "SELECT canonical_id FROM payee WHERE id = ?", (payee_id,),
        ).fetchone()
        if row is None or row["canonical_id"] is None:
            return payee_id
        return int(row["canonical_id"])

    def set_payee_default_category(
        self, payee_id: int, category_id: Optional[int],
    ) -> None:
        """Remember (or clear) the auto-category for a payee (ADR-072).

        The memory is stored on the **canonical** — if ``payee_id`` is an
        alias, its canonical carries it so every alias of the same merchant
        shares one memory. ``category_id=None`` (or Uncategorised) clears it.
        Commits."""
        canon = self._canonical_id_for(payee_id)
        if category_id == UNCATEGORISED_ID:
            category_id = None
        try:
            self._conn.execute(
                "UPDATE payee SET default_category_id = ? WHERE id = ?",
                (category_id, canon),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def get_payee_default_category(self, payee_id: int) -> Optional[int]:
        """The effective auto-category for a payee, read from its canonical
        (ADR-072). None when no memory is set."""
        canon = self._canonical_id_for(payee_id)
        row = self._conn.execute(
            "SELECT default_category_id FROM payee WHERE id = ?", (canon,),
        ).fetchone()
        if row is None or row["default_category_id"] is None:
            return None
        return int(row["default_category_id"])

    def most_common_category_for_payee(self, payee_id: int) -> Optional[int]:
        """Infer a payee's usual category from its own transaction history
        (ADR-106): the most-frequent non-Uncategorised, non-transfer category
        across the payee and its aliases, ties broken by most recent. Used as
        a fallback for the New Transaction dialog's category pre-fill when the
        payee has no explicit remembered category (ADR-072). Returns None when
        the payee has no categorised history. Read-only — never writes a
        memory; the explicit per-payee default stays the source of truth."""
        ids = self.expand_canonical_payee_ids([payee_id]) or [payee_id]
        ph = ",".join("?" * len(ids))
        row = self._conn.execute(
            f"SELECT t.category_id AS cid, COUNT(*) AS n, "
            f"       MAX(t.posted_date) AS recent "
            f"FROM txn t JOIN category c ON c.id = t.category_id "
            f"WHERE t.payee_id IN ({ph}) "
            f"  AND t.category_id != ? AND c.kind != 'transfer' "
            f"GROUP BY t.category_id "
            f"ORDER BY n DESC, recent DESC LIMIT 1",
            (*ids, UNCATEGORISED_ID),
        ).fetchone()
        return int(row["cid"]) if row is not None else None

    def list_payee_default_categories(self) -> list[tuple[int, str, int]]:
        """``(payee_id, payee_name, category_id)`` for every payee carrying a
        remembered auto-category (ADR-072). Memories live on canonicals, so
        these are the merchants that auto-categorise on import / pre-fill on
        entry. Sorted by payee name; feeds the Rules dialog's memories section
        (ADR-106). The caller resolves the category's display path."""
        cur = self._conn.execute(
            "SELECT id, name, default_category_id FROM payee "
            "WHERE default_category_id IS NOT NULL "
            "ORDER BY name COLLATE NOCASE"
        )
        return [
            (int(r["id"]), r["name"], int(r["default_category_id"]))
            for r in cur
        ]

    def resolve_import_payee(
        self, raw_name: str,
    ) -> tuple[Optional[int], Optional[int]]:
        """Resolve a raw import payee string to ``(payee_id, default_category_id)``
        (ADR-072 / ADR-028 round 2).

        - Empty name → ``(None, None)`` (investment rows carry no payee, ADR-071).
        - Exact name match: if the matched row is an **alias**, the returned
          id is its **canonical** — new ledger rows point at the canonical so
          the register shows the clean name and the alias's history rolls up
          without a read-time ``COALESCE``. Existing alias-pointing rows are
          left alone; this only stops creating new ones.
        - No match: a new canonical payee is created (today's behaviour).

        The second element is the canonical's ``default_category_id`` (the
        memorised auto-category) or None. Does **not** commit — the insert
        stays inside the import service's transaction, like
        ``get_or_create_payee``."""
        name = (raw_name or "").strip()
        if not name:
            return (None, None)
        row = self._conn.execute(
            "SELECT id, canonical_id FROM payee WHERE name = ?", (name,),
        ).fetchone()
        if row is not None:
            payee_id = (
                int(row["canonical_id"])
                if row["canonical_id"] is not None
                else int(row["id"])
            )
        else:
            cur = self._conn.execute(
                "INSERT INTO payee (name) VALUES (?)", (name,),
            )
            payee_id = int(cur.lastrowid)
        default = self._conn.execute(
            "SELECT default_category_id FROM payee WHERE id = ?", (payee_id,),
        ).fetchone()
        default_cat = (
            int(default["default_category_id"])
            if default and default["default_category_id"] is not None
            else None
        )
        return (payee_id, default_cat)

    def count_uncategorised_for_payee(self, payee_id: int) -> int:
        """How many plain transactions for this payee (rolled up over its
        aliases) are still Uncategorised — the candidate set for a retroactive
        auto-category apply (ADR-072). Excludes transfers and split parents
        (whose categories live in ``txn_split``)."""
        ids = self.expand_canonical_payee_ids([self._canonical_id_for(payee_id)])
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM txn "
            f"WHERE payee_id IN ({ph}) "
            f"  AND category_id = ? "
            f"  AND transfer_id IS NULL "
            f"  AND id NOT IN (SELECT txn_id FROM txn_split)",
            [*ids, UNCATEGORISED_ID],
        ).fetchone()
        return int(row["c"])

    def apply_default_category_to_uncategorised(
        self, payee_id: int, category_id: int,
    ) -> int:
        """Set ``category_id`` on every Uncategorised, non-transfer, non-split
        transaction for this payee (rolled up over its aliases). Returns the
        number of rows changed; never overwrites a category already set.
        Commits."""
        ids = self.expand_canonical_payee_ids([self._canonical_id_for(payee_id)])
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        try:
            cur = self._conn.execute(
                f"UPDATE txn SET category_id = ? "
                f"WHERE payee_id IN ({ph}) "
                f"  AND category_id = ? "
                f"  AND transfer_id IS NULL "
                f"  AND id NOT IN (SELECT txn_id FROM txn_split)",
                [category_id, *ids, UNCATEGORISED_ID],
            )
            self.commit()
            return int(cur.rowcount)
        except Exception:
            self.rollback()
            raise

    # ── Auto-categorisation rules (ADR-073) ──

    _RULE_KINDS = ("contains", "starts_with", "ends_with", "is_exactly")
    _RULE_FIELDS = ("payee_raw", "memo")

    def list_rules(self) -> list[RuleRow]:
        """All rules in priority order (ascending = highest priority first),
        with display names resolved for the management screen."""
        cur = self._conn.execute(
            "SELECT r.id, r.pattern, r.pattern_kind, r.match_field, "
            "       r.set_payee_id, r.set_category_id, r.priority, "
            "       p.name AS payee_name "
            "FROM      rule r "
            "LEFT JOIN payee p ON p.id = r.set_payee_id "
            "ORDER BY r.priority, r.id"
        )
        rows = cur.fetchall()
        paths = {c.id: (c.path or c.name) for c in self.list_categories_flat()}
        return [
            RuleRow(
                id=r["id"], pattern=r["pattern"], pattern_kind=r["pattern_kind"],
                match_field=r["match_field"], set_payee_id=r["set_payee_id"],
                set_category_id=r["set_category_id"], priority=r["priority"],
                set_payee_name=r["payee_name"],
                set_category_path=paths.get(r["set_category_id"]),
            )
            for r in rows
        ]

    def create_rule(
        self, *, pattern: str, pattern_kind: str, match_field: str,
        set_payee_id: Optional[int], set_category_id: Optional[int],
        priority: int = 100,
    ) -> int:
        """Insert an auto-categorisation rule. Validates the matcher kind /
        field and that at least one setter is present. Commits; returns the id."""
        self._validate_rule(
            pattern, pattern_kind, match_field, set_payee_id, set_category_id,
        )
        try:
            cur = self._conn.execute(
                "INSERT INTO rule (iri, pattern, pattern_kind, match_field, "
                " set_category_id, set_payee_id, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_rule_iri(), pattern.strip(), pattern_kind, match_field,
                    set_category_id, set_payee_id, priority,
                ),
            )
            self.commit()
            return int(cur.lastrowid)
        except Exception:
            self.rollback()
            raise

    def update_rule(
        self, rule_id: int, *, pattern: str, pattern_kind: str,
        match_field: str, set_payee_id: Optional[int],
        set_category_id: Optional[int], priority: int,
    ) -> None:
        """Update every editable field of a rule. Validates as create_rule."""
        self._validate_rule(
            pattern, pattern_kind, match_field, set_payee_id, set_category_id,
        )
        try:
            self._conn.execute(
                "UPDATE rule SET pattern = ?, pattern_kind = ?, "
                " match_field = ?, set_category_id = ?, set_payee_id = ?, "
                " priority = ? WHERE id = ?",
                (
                    pattern.strip(), pattern_kind, match_field,
                    set_category_id, set_payee_id, priority, rule_id,
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def delete_rule(self, rule_id: int) -> None:
        try:
            self._conn.execute("DELETE FROM rule WHERE id = ?", (rule_id,))
            self.commit()
        except Exception:
            self.rollback()
            raise

    @classmethod
    def _validate_rule(
        cls, pattern, pattern_kind, match_field, set_payee_id, set_category_id,
    ) -> None:
        if not (pattern or "").strip():
            raise ValueError("A rule needs a pattern to match.")
        if pattern_kind not in cls._RULE_KINDS:
            raise ValueError(f"Unknown matcher kind: {pattern_kind!r}")
        if match_field not in cls._RULE_FIELDS:
            raise ValueError(f"Unknown match field: {match_field!r}")
        if set_payee_id is None and set_category_id is None:
            raise ValueError("A rule must set a payee, a category, or both.")

    def _txns_matching_rule(self, rule):
        """Rows (id, category_id, payee_id) for non-transfer, non-split txns
        whose stored payee name / memo matches the rule (ADR-073 retroactive).

        Reuses the pure ``rule_matches`` so import-time and retroactive
        matching agree; the stored payee **name** stands in for the original
        raw import string (it isn't retained), which is what the user sees."""
        cur = self._conn.execute(
            "SELECT t.id, t.category_id, t.payee_id, "
            "       COALESCE(p.name, '') AS pname, "
            "       COALESCE(t.memo, '') AS memo "
            "FROM      txn t "
            "LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.transfer_id IS NULL "
            "  AND t.id NOT IN (SELECT txn_id FROM txn_split)"
        )
        return [r for r in cur.fetchall() if rule_matches(rule, r["pname"], r["memo"])]

    @staticmethod
    def _rule_would_change(rule, row) -> bool:
        if rule.set_category_id is not None and row["category_id"] == UNCATEGORISED_ID:
            return True
        if rule.set_payee_id is not None and row["payee_id"] is None:
            return True
        return False

    def count_txns_matching_rule(self, rule) -> int:
        """How many existing transactions the rule would change — matching
        rows whose target field is still unset (Uncategorised category / NULL
        payee). Never counts rows already set."""
        return sum(
            1 for r in self._txns_matching_rule(rule)
            if self._rule_would_change(rule, r)
        )

    def apply_rule_to_existing(self, rule) -> int:
        """Apply ``rule`` to matching existing transactions, filling only an
        Uncategorised category / NULL payee — never overwriting, never
        touching transfers or split parents. Returns rows changed. Commits."""
        changed = 0
        try:
            for r in self._txns_matching_rule(rule):
                sets: list[str] = []
                params: list = []
                if (
                    rule.set_category_id is not None
                    and r["category_id"] == UNCATEGORISED_ID
                ):
                    sets.append("category_id = ?")
                    params.append(rule.set_category_id)
                if rule.set_payee_id is not None and r["payee_id"] is None:
                    sets.append("payee_id = ?")
                    params.append(rule.set_payee_id)
                if not sets:
                    continue
                params.append(r["id"])
                self._conn.execute(
                    f"UPDATE txn SET {', '.join(sets)} WHERE id = ?", params,
                )
                changed += 1
            self.commit()
            return changed
        except Exception:
            self.rollback()
            raise

    def create_payee(self, name: str) -> int:
        """Insert a new payee. Raises ValueError if the name is blank or
        already in use."""
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Payee name cannot be empty.")
        try:
            cur = self._conn.execute(
                "INSERT INTO payee (name) VALUES (?)", (clean,),
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            self.rollback()
            raise ValueError(
                f"A payee named {clean!r} already exists."
            ) from e
        except Exception:
            self.rollback()
            raise
        return cur.lastrowid

    def rename_payee(self, payee_id: int, new_name: str) -> None:
        """Rename a payee. Raises ValueError on blank input or on a collision
        with another existing payee (use merge_payees in that case)."""
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Payee name cannot be empty.")
        existing = self._conn.execute(
            "SELECT id FROM payee WHERE name = ? AND id != ?",
            (clean, payee_id),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"Another payee named {clean!r} already exists — use Merge "
                f"to combine them instead."
            )
        try:
            self._conn.execute(
                "UPDATE payee SET name = ? WHERE id = ?", (clean, payee_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def merge_payees(self, source_ids: list[int], target_id: int) -> int:
        """Re-point every transaction referencing any payee in source_ids
        to target_id, then delete the source payees. Returns the count of
        transactions reassigned. target_id is excluded from source_ids
        defensively, in case the caller passes it.

        ADR-029 round 1: if any source payee has aliases of its own, those
        aliases are re-pointed onto the target *before* the sources are
        deleted, so the merge doesn't orphan them via the FK's ON DELETE
        SET NULL (which would silently promote them to canonical — wrong
        for a merge, where the user clearly wants them under the target).

        Single SQL transaction: either everything moves and the sources
        are removed, or nothing changes."""
        sources = [sid for sid in source_ids if sid != target_id]
        if not sources:
            return 0
        placeholders = ",".join("?" * len(sources))
        try:
            # Re-point any aliases of the sources onto the target first.
            self._conn.execute(
                f"UPDATE payee SET canonical_id = ? "
                f"WHERE canonical_id IN ({placeholders})",
                (target_id, *sources),
            )
            cur = self._conn.execute(
                f"UPDATE txn SET payee_id = ? "
                f"WHERE payee_id IN ({placeholders})",
                (target_id, *sources),
            )
            moved = cur.rowcount
            self._conn.execute(
                f"DELETE FROM payee WHERE id IN ({placeholders})",
                tuple(sources),
            )
            self.commit()
            return moved
        except Exception:
            self.rollback()
            raise

    def delete_payees(self, payee_ids: list[int]) -> int:
        """Delete one or more payees. Transactions referencing them have
        their payee_id set to NULL (FK ON DELETE SET NULL). Returns the
        count of payees deleted."""
        if not payee_ids:
            return 0
        placeholders = ",".join("?" * len(payee_ids))
        try:
            cur = self._conn.execute(
                f"DELETE FROM payee WHERE id IN ({placeholders})",
                tuple(payee_ids),
            )
            self.commit()
            return cur.rowcount
        except Exception:
            self.rollback()
            raise

    # ── Transaction ──

    def import_hash_exists(self, account_id: int, import_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM txn WHERE account_id = ? AND import_hash = ? LIMIT 1",
            (account_id, import_hash),
        ).fetchone()
        return row is not None

    def find_manual_match(
        self,
        account_id: int,
        posted_date: str,
        amount: Decimal,
        window_days: int = 2,
    ) -> Optional[ManualMatch]:
        """Find a manual (no import_hash) transaction matching the candidate.

        Matches on account, exact amount in pence (sign-preserving for
        direction), and posted date within ±window_days.

        Reconciled rows are excluded (ADR-040): once a row is matched to a
        closed statement, an import must not silently re-classify it as a
        potential match and alter its status/amount.
        """
        try:
            d = date.fromisoformat(posted_date)
        except ValueError:
            return None
        d_minus = (d - timedelta(days=window_days)).isoformat()
        d_plus = (d + timedelta(days=window_days)).isoformat()
        row = self._conn.execute(
            "SELECT t.id, t.iri, t.posted_date, COALESCE(p.name, '') AS payee_name "
            "FROM txn t "
            "LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.account_id = ? "
            "  AND t.amount = ? "
            "  AND t.posted_date BETWEEN ? AND ? "
            "  AND t.import_hash IS NULL "
            "  AND t.status != 'reconciled' "
            "LIMIT 1",
            (account_id, decimal_to_pence(amount), d_minus, d_plus),
        ).fetchone()
        if row is None:
            return None
        return ManualMatch(
            id=row["id"], iri=row["iri"],
            posted_date=row["posted_date"], payee_raw=row["payee_name"],
        )

    def list_dedupe_candidates(
        self, account_id: int, start_date: str, end_date: str,
    ) -> list[DedupeExisting]:
        """Existing transactions in ``[start_date, end_date]`` for an account,
        as cross-source duplicate-match targets (ADR-085).

        Unlike :meth:`find_manual_match` this returns rows of **any** source
        (manual or imported) — the count-aware matcher needs every existing
        copy so multiplicity is respected. Reconciled rows are included (a
        confirmed duplicate against one is only ever *skipped on the incoming
        side*; the reconciled row is never altered). ``import_hash`` is
        returned so the caller can exclude exact-hash targets already claimed
        by the batch's fast path.
        """
        rows = self._conn.execute(
            "SELECT t.id, t.posted_date, t.amount, t.import_hash, "
            "       COALESCE(p.name, '') AS payee_name "
            "FROM txn t "
            "LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.account_id = ? "
            "  AND t.posted_date BETWEEN ? AND ?",
            (account_id, start_date, end_date),
        ).fetchall()
        return [
            DedupeExisting(
                id=r["id"], posted_date=r["posted_date"],
                amount_pence=r["amount"], payee_name=r["payee_name"],
                import_hash=r["import_hash"],
                is_manual=r["import_hash"] is None,
            )
            for r in rows
        ]

    def find_match_candidates(
        self, account_id: int, around_date: str, amount_pence: int,
        *, day_window: int = 60, limit: int = 200,
    ) -> list["MatchCandidate"]:
        """Existing transactions in an account offered as manual match targets
        for a still-new import row (ADR-151 Phase 2, the 'Find a match' picker).

        Spans every exact-amount row (any date) plus everything within
        ``±day_window`` days of ``around_date``, ranked exact-amount first then
        by date proximity — so the picker surfaces both the same-amount charge a
        fortnight off and the nearby rows a payee search can whittle down."""
        rows = self._conn.execute(
            "SELECT t.id, t.posted_date, t.amount, t.status, "
            "       COALESCE(p.name, '') AS payee_name, "
            "       (t.import_hash IS NULL) AS is_manual "
            "FROM txn t "
            "LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.account_id = ? "
            "  AND ( t.amount = ? "
            "        OR ABS(julianday(t.posted_date) - julianday(?)) <= ? ) "
            "ORDER BY (t.amount = ?) DESC, "
            "         ABS(julianday(t.posted_date) - julianday(?)) ASC "
            "LIMIT ?",
            (account_id, amount_pence, around_date, day_window,
             amount_pence, around_date, limit),
        ).fetchall()
        return [
            MatchCandidate(
                id=r["id"], posted_date=r["posted_date"],
                amount_pence=r["amount"], payee_name=r["payee_name"],
                status=r["status"] or "", is_manual=bool(r["is_manual"]),
            )
            for r in rows
        ]

    def insert_transaction(
        self,
        *,
        account_id: int,
        posted_date: str,
        amount: Decimal,
        payee_id: Optional[int],
        category_id: int,
        status: str,
        memo: str,
        import_hash: Optional[str],
        import_batch_id: Optional[int],
        action: Optional[str] = None,
        security_id: Optional[int] = None,
        quantity: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
        commission: Optional[Decimal] = None,
        accrued_interest: Optional[Decimal] = None,
        bank_posted_date: Optional[str] = None,
    ) -> int:
        """Insert a transaction. The investment kwargs (ADR-043) default to
        None so every existing cash caller is unaffected; when supplied,
        `amount` is still the SIGNED CASH IMPACT (Buy negative, Sell/Div
        positive, share-only actions zero) so cash balance = SUM(amount)
        holds. `quantity`/`price` are stored as REAL; `commission` and
        `accrued_interest` (a bond purchase's prepaid coupon, ADR-093) as
        pence. `amount` already includes accrued_interest in the cash; cost
        basis subtracts it back out in holdings.py."""
        iri = new_transaction_iri()
        cur = self._conn.execute(
            "INSERT INTO txn "
            "(iri, account_id, posted_date, amount, payee_id, category_id, "
            " status, memo, import_hash, import_batch_id, "
            " action, security_id, quantity, price, commission, "
            " accrued_interest, bank_posted_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iri, account_id, posted_date, decimal_to_pence(amount),
                payee_id, category_id, status, memo or None,
                import_hash, import_batch_id,
                action or None, security_id,
                float(quantity) if quantity is not None else None,
                float(price) if price is not None else None,
                decimal_to_pence(commission) if commission is not None else None,
                decimal_to_pence(accrued_interest)
                if accrued_interest is not None else None,
                bank_posted_date,
            ),
        )
        return cur.lastrowid

    def update_investment_transaction(
        self, txn_id: int, *,
        posted_date: str,
        amount: Decimal,
        payee_id: Optional[int],
        category_id: int,
        status: str,
        memo: str,
        action: Optional[str],
        security_id: Optional[int],
        quantity: Optional[Decimal],
        price: Optional[Decimal],
        commission: Optional[Decimal],
        accrued_interest: Optional[Decimal] = None,
    ) -> None:
        """Update every editable field of one investment transaction in a single
        write (ADR-048 — the Investment Transaction edit dialog). ``amount`` is
        the SIGNED CASH IMPACT, the same contract as ``insert_transaction`` (Buy
        negative, Sell/Div positive, share-only actions zero), so cash balance =
        SUM(amount) stays correct. ``accrued_interest`` (ADR-093) is a bond
        purchase's prepaid coupon in pence — included in ``amount``'s cash but
        held out of cost basis by the holdings engine. Commits."""
        self._conn.execute(
            "UPDATE txn SET posted_date = ?, amount = ?, payee_id = ?, "
            "  category_id = ?, status = ?, memo = ?, action = ?, "
            "  security_id = ?, quantity = ?, price = ?, commission = ?, "
            "  accrued_interest = ? "
            "WHERE id = ?",
            (
                posted_date, decimal_to_pence(amount), payee_id,
                category_id, status, memo or None, action or None,
                security_id,
                float(quantity) if quantity is not None else None,
                float(price) if price is not None else None,
                decimal_to_pence(commission) if commission is not None else None,
                decimal_to_pence(accrued_interest)
                if accrued_interest is not None else None,
                txn_id,
            ),
        )
        self.commit()

    # ── Split transactions (ADR-051) ──
    #
    # A split's parent `txn` row keeps the full signed total and category_id =
    # Uncategorised(1); the category lines live in `txn_split` and sum to the
    # parent total. Only category-attribution reads unroll (via the
    # `txn_category_line` view); the money layer reads the parent total as-is.

    def _replace_split_lines(
        self, txn_id: int, total_amount: Decimal,
        lines: "list[SplitLineInput]",
    ) -> None:
        """Delete and re-insert the `txn_split` rows for one parent, after
        checking the signed line amounts sum exactly to ``total_amount``.
        Pence are integers, so the check is exact (no float tolerance). Does
        not commit. ``lines`` must be non-empty; each line is
        ``(category_id, memo, amount)`` or — for a transfer line (ADR-051
        amendment) — ``(category_id, memo, amount, transfer_to_account_id)``.

        **Transfer lines.** When a line carries a destination account, the line
        moves money out of the split's account into that account, so a real
        partner ``txn`` is created there (the source balance stays correct via
        the parent total; the destination balance only moves because of the
        partner row). The line and its partner share a fresh ``transfer_id`` and
        a ``transfer`` parent row records direction + rate. Same-currency only
        in v1 (``rate=1.0, rate_source='derived'``).

        Edit is tear-down-and-rebuild: any partner txns the split currently owns
        are deleted first, then recreated from the new line set. (A reconciled
        partner row is therefore rebuilt — accepted v1 limitation.)"""
        if not lines:
            raise ValueError("A split transaction needs at least one line.")
        # Normalise each line to (category_id, memo, pence, dest_account_id|None).
        norm: list[tuple[int, Optional[str], int, Optional[int]]] = []
        for ln in lines:
            cid, memo, amt = ln[0], ln[1], ln[2]
            dest = ln[3] if len(ln) > 3 else None
            norm.append((cid, memo, decimal_to_pence(amt), dest))
        total_pence = decimal_to_pence(total_amount)
        line_sum = sum(p for _, _, p, _ in norm)
        if line_sum != total_pence:
            raise ValueError(
                f"Split lines must sum to the transaction total "
                f"({total_pence} pence); got {line_sum}."
            )

        # Source account (the split's own account) — needed for transfer-line
        # currency checks and the transfer parent's from/to direction. The
        # parent row is already written when we get here (insert/update both
        # touch txn first), so its posted_date is the date the partner inherits.
        prow = self._conn.execute(
            "SELECT account_id, posted_date FROM txn WHERE id = ?", (txn_id,),
        ).fetchone()
        if prow is None:
            raise ValueError(f"No transaction with id {txn_id}")
        source_account_id = int(prow["account_id"])
        source_ccy = self.get_account_currency(source_account_id)
        parent_date = prow["posted_date"]

        # Tear down the partner txns this split currently owns, then its lines.
        for r in self._conn.execute(
            "SELECT transfer_id FROM txn_split "
            "WHERE txn_id = ? AND transfer_id IS NOT NULL",
            (txn_id,),
        ).fetchall():
            self._conn.execute(
                "DELETE FROM txn WHERE transfer_id = ?", (r["transfer_id"],),
            )
        self._conn.execute("DELETE FROM txn_split WHERE txn_id = ?", (txn_id,))

        for order, (cid, memo, pence, dest) in enumerate(norm):
            transfer_iri: Optional[str] = None
            if dest is not None:
                transfer_iri = self._make_split_line_transfer(
                    source_account_id=source_account_id,
                    source_ccy=source_ccy,
                    posted_date=parent_date,
                    category_id=cid,
                    line_pence=pence,
                    dest_account_id=dest,
                    memo=memo,
                )
            self._conn.execute(
                "INSERT INTO txn_split "
                "(txn_id, category_id, memo, amount, sort_order, transfer_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (txn_id, cid, (memo or None), pence, order, transfer_iri),
            )

    def _make_split_line_transfer(
        self, *,
        source_account_id: int,
        source_ccy: Optional[str],
        posted_date: str,
        category_id: int,
        line_pence: int,
        dest_account_id: int,
        memo: Optional[str],
    ) -> str:
        """Create the partner ``txn`` + ``transfer`` parent for one transfer
        split line and return the shared ``transfer_id`` (ADR-051 amendment).

        The partner amount is ``-line_pence`` (same convention as
        ``create_transfer``: the two sides carry opposite signs). Direction
        follows the line sign — a line that takes money *out* of the split
        account (line < 0) is ``from = source, to = dest``; a line that brings
        money *in* (line > 0) is the reverse. The partner inherits the split
        parent's ``posted_date`` so both halves clear on the same day.
        Same-currency only in v1 (rate 1.0 / 'derived')."""
        if line_pence == 0:
            raise ValueError("A transfer split line cannot be zero.")
        if dest_account_id == source_account_id:
            raise ValueError(
                "A transfer split line must transfer to a different account."
            )
        if self.get_category_kind(category_id) != "transfer":
            raise ValueError(
                "A split line with a destination account must use a "
                "transfer-kind category."
            )
        dest_ccy = self.get_account_currency(dest_account_id)
        if dest_ccy is None:
            raise ValueError(f"Unknown destination account {dest_account_id}.")
        if dest_ccy != source_ccy:
            raise ValueError(
                "Split-line transfers must stay within one currency in this "
                f"version (source {source_ccy}, destination {dest_ccy})."
            )

        names = self._account_names((source_account_id,))
        source_name = names.get(source_account_id, "account")
        transfer_iri = new_transfer_iri()
        # Money leaves the source on a negative line → the destination receives;
        # its partner row reads "Transfer from <source>". A positive line is the
        # reverse flow (the partner pays out to the source).
        if line_pence < 0:
            from_id, to_id = source_account_id, dest_account_id
            partner_payee = self.get_or_create_payee(
                f"Transfer from {source_name}"
            )
        else:
            from_id, to_id = dest_account_id, source_account_id
            partner_payee = self.get_or_create_payee(
                f"Transfer to {source_name}"
            )
        self._insert_transfer_half(
            account_id=dest_account_id,
            amount=pence_to_decimal(-line_pence),
            payee_id=partner_payee,
            category_id=category_id,
            status="pending",
            memo=memo or "",
            posted_date=posted_date,
            transfer_id=transfer_iri,
        )
        self._insert_transfer_parent(
            iri=transfer_iri,
            from_account_id=from_id,
            to_account_id=to_id,
            rate=Decimal("1"),
            rate_source="derived",
        )
        return transfer_iri

    def _account_names(self, account_ids: tuple[int, ...]) -> dict[int, str]:
        """Map account id → name for the given ids (one query)."""
        if not account_ids:
            return {}
        ph = ",".join("?" * len(account_ids))
        cur = self._conn.execute(
            f"SELECT id, name FROM account WHERE id IN ({ph})",
            tuple(account_ids),
        )
        return {int(r["id"]): r["name"] for r in cur}

    def insert_split_transaction(
        self, *,
        account_id: int,
        posted_date: str,
        payee_id: Optional[int],
        status: str,
        memo: str,
        total_amount: Decimal,
        lines: list[SplitLineInput],
        import_hash: Optional[str],
        import_batch_id: Optional[int],
        bank_posted_date: Optional[str] = None,
    ) -> int:
        """Insert a split transaction (ADR-051): a parent `txn` row carrying the
        full signed ``total_amount`` (category_id = Uncategorised) plus one
        `txn_split` row per line. Lines must sum to ``total_amount``. Does not
        commit — mirrors ``insert_transaction``; the caller commits."""
        txn_id = self.insert_transaction(
            account_id=account_id, posted_date=posted_date, amount=total_amount,
            payee_id=payee_id, category_id=UNCATEGORISED_ID, status=status,
            memo=memo, import_hash=import_hash, import_batch_id=import_batch_id,
            bank_posted_date=bank_posted_date,
        )
        self._replace_split_lines(txn_id, total_amount, lines)
        return txn_id

    def update_split_transaction(
        self, txn_id: int, *,
        posted_date: str,
        payee_id: Optional[int],
        status: str,
        memo: str,
        total_amount: Decimal,
        lines: list[SplitLineInput],
    ) -> None:
        """Update a split transaction's parent header and replace its lines in
        one write (ADR-051). The parent keeps ``total_amount``; category_id
        stays at Uncategorised. Lines must sum to ``total_amount``. Commits."""
        self._conn.execute(
            "UPDATE txn SET posted_date = ?, amount = ?, payee_id = ?, "
            "  category_id = ?, status = ?, memo = ? WHERE id = ?",
            (
                posted_date, decimal_to_pence(total_amount), payee_id,
                UNCATEGORISED_ID, status, memo or None, txn_id,
            ),
        )
        self._replace_split_lines(txn_id, total_amount, lines)
        self.commit()

    def convert_plain_to_split(
        self, txn_id: int,
        lines: list[SplitLineInput],
    ) -> None:
        """Turn an existing plain transaction into a split (ADR-051): keep its
        amount/payee/date/status, move category_id to Uncategorised, and attach
        the given lines (which must sum to the existing total). Commits."""
        row = self._conn.execute(
            "SELECT amount FROM txn WHERE id = ?", (txn_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No transaction with id {txn_id}")
        total = pence_to_decimal(row["amount"])
        self._conn.execute(
            "UPDATE txn SET category_id = ? WHERE id = ?",
            (UNCATEGORISED_ID, txn_id),
        )
        self._replace_split_lines(txn_id, total, lines)
        self.commit()

    def convert_split_to_plain(self, txn_id: int, category_id: int) -> None:
        """Collapse a split back to a single-category transaction (ADR-051):
        delete its `txn_split` rows and set the parent's category_id. The
        parent's amount is unchanged. Any transfer-line partner txns the split
        owned (ADR-051 amendment) are deleted first so they don't orphan.
        Commits."""
        for r in self._conn.execute(
            "SELECT transfer_id FROM txn_split "
            "WHERE txn_id = ? AND transfer_id IS NOT NULL",
            (txn_id,),
        ).fetchall():
            self._conn.execute(
                "DELETE FROM txn WHERE transfer_id = ?", (r["transfer_id"],),
            )
        self._conn.execute("DELETE FROM txn_split WHERE txn_id = ?", (txn_id,))
        self._conn.execute(
            "UPDATE txn SET category_id = ? WHERE id = ?",
            (category_id, txn_id),
        )
        self.commit()

    def split_lines_for_txns(
        self, txn_ids: list[int],
    ) -> dict[int, list[SplitLine]]:
        """Category lines for several split transactions, grouped by parent id
        and ordered within each parent (ADR-051). Used by the account-summary
        screen to roll split spend up to the right categories."""
        if not txn_ids:
            return {}
        ph = ",".join("?" * len(txn_ids))
        # ``partner`` is the destination half of a transfer LINE (ADR-051
        # amendment): a transfer split line shares its ``transfer_id`` with one
        # real ``txn`` in another account, whose account_id is the destination.
        cur = self._conn.execute(
            f"SELECT s.id, s.txn_id, s.category_id, "
            f"       COALESCE(c.name, '') AS category_name, "
            f"       COALESCE(c.kind, 'expense') AS category_kind, "
            f"       COALESCE(s.memo, '') AS memo, s.amount, "
            f"       partner.account_id AS transfer_to_account_id "
            f"FROM txn_split s "
            f"LEFT JOIN category c ON c.id = s.category_id "
            f"LEFT JOIN txn partner ON partner.transfer_id = s.transfer_id "
            f"WHERE s.txn_id IN ({ph}) "
            f"ORDER BY s.txn_id, s.sort_order, s.id",
            tuple(txn_ids),
        )
        out: dict[int, list[SplitLine]] = {}
        for r in cur:
            dest = r["transfer_to_account_id"]
            out.setdefault(r["txn_id"], []).append(SplitLine(
                category_id=r["category_id"],
                category_name=r["category_name"],
                memo=r["memo"],
                amount=pence_to_decimal(r["amount"]),
                id=r["id"],
                category_kind=r["category_kind"],
                transfer_to_account_id=int(dest) if dest is not None else None,
            ))
        return out

    def split_lines_for_txn(self, txn_id: int) -> list[SplitLine]:
        """The category lines of one split transaction, in entry order
        (ADR-051). Empty list when the transaction has no splits."""
        return self.split_lines_for_txns([txn_id]).get(txn_id, [])

    def merge_into_manual_transaction(
        self,
        manual_id: int,
        import_hash: str,
        memo: Optional[str],
        bank_posted_date: Optional[str] = None,
        new_amount: Optional[Decimal] = None,
    ) -> None:
        """Confirm a download against an existing hand-entered transaction.

        Stamps its ``import_hash``, advances it up the confidence ladder to
        ``matched`` if it's still ``pending``/``cleared`` (ADR-130 — the
        download is the bank confirmation), and records the bank's posting date
        (``bank_posted_date``) so reconciliation can range on it. ``new_amount``
        (ADR-130 Phase 3b "adopt bank amount") overwrites the signed amount when
        the user chose to take the download's figure over a mis-entry. The
        user's spend date, category, and payee are untouched; a ``reconciled``
        (locked) row is left alone (status, amount). Memo fills only if empty.
        """
        new_amount_pence = (
            decimal_to_pence(new_amount) if new_amount is not None else None
        )
        self._conn.execute(
            "UPDATE txn SET import_hash = ?, "
            "  status = CASE WHEN status IN ('pending', 'cleared') "
            "                THEN 'matched' ELSE status END, "
            "  bank_posted_date = COALESCE(?, bank_posted_date), "
            "  amount = CASE WHEN status = 'reconciled' THEN amount "
            "                ELSE COALESCE(?, amount) END "
            "WHERE id = ?",
            (import_hash, bank_posted_date, new_amount_pence, manual_id),
        )
        if memo:
            self._conn.execute(
                "UPDATE txn SET memo = ? "
                "WHERE id = ? AND (memo IS NULL OR memo = '')",
                (memo, manual_id),
            )

    # ── Import batch ──

    def create_import_batch(
        self, account_id: int, source_format: str, source_filename: str,
    ) -> int:
        iri = new_import_batch_iri()
        cur = self._conn.execute(
            "INSERT INTO import_batch "
            "(iri, account_id, source_format, source_filename) "
            "VALUES (?, ?, ?, ?)",
            (iri, account_id, source_format, source_filename),
        )
        return cur.lastrowid

    def finalise_import_batch(
        self,
        batch_id: int,
        new_count: int,
        duplicate_count: int,
        matched_count: int,
    ) -> None:
        self._conn.execute(
            "UPDATE import_batch SET "
            "  new_count = ?, duplicate_count = ?, matched_count = ? "
            "WHERE id = ?",
            (new_count, duplicate_count, matched_count, batch_id),
        )

    def list_import_batches(self, limit: int = 50) -> list[dict]:
        """Recent import batches, newest first, for the Undo-Import picker
        (ADR-118). Each row carries the account name and the originally-recorded
        counts so the user can identify the run they want to reverse."""
        rows = self._conn.execute(
            "SELECT b.id, b.source_format, b.source_filename, b.imported_at, "
            "       b.new_count, b.account_id, a.name AS account_name, "
            "       (SELECT COUNT(*) FROM txn WHERE txn.import_batch_id = b.id) "
            "         AS live_count "
            "FROM import_batch b "
            "LEFT JOIN account a ON a.id = b.account_id "
            "ORDER BY b.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_import_batch(self, batch_id: int) -> dict:
        """Undo an import (ADR-118): delete every transaction that batch created,
        then the batch row. Returns ``{"deleted_txns": int, "empty_categories":
        [(id, display_path), …]}`` — the second list is the categories this
        import populated that are now **empty and import-sourced**, offered to the
        caller for optional cleanup (a re-import re-creates/maps them via the
        review dialog only if they're gone).

        ``txn_split`` lines and reconciliation rows cascade off the ``txn``
        delete (ON DELETE CASCADE). Transactions are deleted **before** the batch
        row because ``txn.import_batch_id`` is ``ON DELETE SET NULL`` — dropping
        the batch first would sever the link and orphan the rows.

        Caveats: rows the import merged into a pre-existing manual placeholder
        (ADR-010) are not batch-created and so are not removed; imported cash
        rows are never transfers, so there are no partner rows."""
        try:
            # Capture the categories this batch touched (direct + split lines)
            # *before* deleting, so we can spot the ones it leaves empty.
            touched: set[int] = set()
            for r in self._conn.execute(
                "SELECT DISTINCT category_id FROM txn "
                "WHERE import_batch_id = ? AND category_id IS NOT NULL",
                (batch_id,),
            ):
                touched.add(r["category_id"])
            for r in self._conn.execute(
                "SELECT DISTINCT s.category_id FROM txn_split s "
                "JOIN txn t ON t.id = s.txn_id "
                "WHERE t.import_batch_id = ? AND s.category_id IS NOT NULL",
                (batch_id,),
            ):
                touched.add(r["category_id"])

            n = self._conn.execute(
                "SELECT COUNT(*) AS c FROM txn WHERE import_batch_id = ?",
                (batch_id,),
            ).fetchone()["c"]
            self._conn.execute(
                "DELETE FROM txn WHERE import_batch_id = ?", (batch_id,),
            )
            self._conn.execute(
                "DELETE FROM import_batch WHERE id = ?", (batch_id,),
            )
            empties = [
                (cid, self.category_display_path(cid))
                for cid in touched
                if self._is_empty_import_category(cid)
            ]
            empties.sort(key=lambda t: t[1])
            self.commit()
            return {"deleted_txns": int(n), "empty_categories": empties}
        except Exception:
            self.rollback()
            raise

    def _is_empty_import_category(self, cid: int) -> bool:
        """True when category ``cid`` is import-created and now carries nothing —
        no direct txns, no split lines, no child categories, and isn't wired into
        a schedule or budget. The conservative test behind Undo-Import's category
        cleanup (ADR-118); deleting such a row can't orphan anything."""
        row = self._conn.execute(
            "SELECT source FROM category WHERE id = ?", (cid,),
        ).fetchone()
        if row is None or row["source"] != "import":
            return False
        checks = (
            ("txn", "category_id"),
            ("txn_split", "category_id"),
            ("category", "parent_id"),
            ("scheduled_txn", "category_id"),
            ("budget_line", "category_id"),
        )
        for table, col in checks:
            hit = self._conn.execute(
                f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (cid,),
            ).fetchone()
            if hit is not None:
                return False
        return True

    def delete_empty_import_categories(self, ids: list[int]) -> int:
        """Delete empty import-created categories outright — **no** Needs-Review
        mapping recorded (unlike :meth:`delete_category`), so a later re-import
        re-offers them in the review dialog (ADR-118). Each id is re-checked for
        emptiness defensively; anything no longer empty is skipped."""
        deleted = 0
        try:
            for cid in ids:
                if self._is_empty_import_category(cid):
                    self._conn.execute(
                        "DELETE FROM category WHERE id = ?", (cid,),
                    )
                    deleted += 1
            self.commit()
            return deleted
        except Exception:
            self.rollback()
            raise

    # ── Register (read + inline edits) ──

    def list_transactions_for_account(
        self, account_id: int, since: str | None = None,
    ) -> list[TransactionRow]:
        """All transactions for one account, chronologically, with running
        balance. Running balance is seeded from the account's opening_balance
        so the final value matches the account's true balance.

        ``since`` (ADR-041) is an inclusive ``'YYYY-MM-DD'`` lower bound: only
        rows with ``posted_date >= since`` are returned, but the running
        balance is still correct because it is seeded with
        ``opening_balance + SUM(amount) WHERE posted_date < since`` — i.e. the
        balance as of the first windowed row, not restarted from zero.
        ``None`` (the default) returns the full history unchanged. The bound
        is lower-only by design, so future-dated rows stay visible. The seed
        query uses ``< since`` and the row select uses ``>= since`` — they must
        share the same bound or the boundary day double-counts or is skipped.
        """
        opening_row = self._conn.execute(
            "SELECT opening_balance FROM account WHERE id = ?", (account_id,),
        ).fetchone()
        running = pence_to_decimal(
            opening_row["opening_balance"] if opening_row else 0
        )
        if since is not None:
            seed_row = self._conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM txn "
                "WHERE account_id = ? AND posted_date < ?",
                (account_id, since),
            ).fetchone()
            running += pence_to_decimal(seed_row["s"])
        sql = (
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id, "
            "       t.action, t.security_id, t.quantity, t.price, t.commission, "
            "       t.accrued_interest, "
            "       COALESCE(s.name, '') AS security_name, "
            "       COALESCE(s.symbol, '') AS security_symbol, "
            "       COALESCE(sp.c, 0) AS split_count, sp.cids AS split_cids "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "LEFT JOIN security s ON s.id = t.security_id "
            "LEFT JOIN (SELECT txn_id, COUNT(*) AS c, "
            "                  GROUP_CONCAT(category_id) AS cids "
            "           FROM txn_split GROUP BY txn_id) sp ON sp.txn_id = t.id "
            "WHERE t.account_id = ? "
        )
        params: list = [account_id]
        if since is not None:
            sql += "  AND t.posted_date >= ? "
            params.append(since)
        sql += "ORDER BY t.posted_date ASC, t.id ASC"
        cur = self._conn.execute(sql, params)
        rows: list[TransactionRow] = []
        for r in cur:
            amt = pence_to_decimal(r["amount"])
            running += amt
            rows.append(TransactionRow(
                id=r["id"], iri=r["iri"],
                account_id=r["account_id"], account_name=r["account_name"],
                posted_date=r["posted_date"], amount=amt,
                payee_id=r["payee_id"], payee_name=r["payee_name"],
                category_id=r["category_id"], category_name=r["category_name"],
                status=r["status"], memo=r["memo"],
                running_balance=running,
                transfer_id=r["transfer_id"],
                split_count=r["split_count"],
                split_category_ids=_parse_split_cids(r["split_cids"]),
                action=r["action"], security_id=r["security_id"],
                security_name=r["security_name"],
                security_symbol=r["security_symbol"],
                quantity=r["quantity"], price=r["price"],
                commission=(
                    pence_to_decimal(r["commission"])
                    if r["commission"] is not None else None
                ),
                accrued_interest=(
                    pence_to_decimal(r["accrued_interest"])
                    if r["accrued_interest"] is not None else None
                ),
            ))
        return rows

    def list_all_transactions(self, since: str | None = None) -> list[TransactionRow]:
        """Every transaction across every account, chronologically.

        Running balance is not meaningful across accounts of different types
        and currencies (see project-all-transactions-view in memory) and is
        reported as 0; the UI hides the Balance column in this view.

        ``since`` (ADR-041) is an inclusive ``'YYYY-MM-DD'`` lower bound; with
        no running balance here there is no seed query — the window is a plain
        ``WHERE posted_date >= since``. ``None`` returns the full history.
        """
        sql = (
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id, "
            "       t.action, t.security_id, t.quantity, t.price, t.commission, "
            "       t.accrued_interest, "
            "       COALESCE(s.name, '') AS security_name, "
            "       COALESCE(s.symbol, '') AS security_symbol, "
            "       COALESCE(sp.c, 0) AS split_count, sp.cids AS split_cids "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "LEFT JOIN security s ON s.id = t.security_id "
            "LEFT JOIN (SELECT txn_id, COUNT(*) AS c, "
            "                  GROUP_CONCAT(category_id) AS cids "
            "           FROM txn_split GROUP BY txn_id) sp ON sp.txn_id = t.id "
        )
        params: list = []
        if since is not None:
            sql += "WHERE t.posted_date >= ? "
            params.append(since)
        sql += "ORDER BY t.posted_date ASC, t.id ASC"
        cur = self._conn.execute(sql, params)
        return [
            TransactionRow(
                id=r["id"], iri=r["iri"],
                account_id=r["account_id"], account_name=r["account_name"],
                posted_date=r["posted_date"], amount=pence_to_decimal(r["amount"]),
                payee_id=r["payee_id"], payee_name=r["payee_name"],
                category_id=r["category_id"], category_name=r["category_name"],
                status=r["status"], memo=r["memo"],
                running_balance=Decimal("0.00"),
                transfer_id=r["transfer_id"],
                split_count=r["split_count"],
                split_category_ids=_parse_split_cids(r["split_cids"]),
                action=r["action"], security_id=r["security_id"],
                security_name=r["security_name"],
                security_symbol=r["security_symbol"],
                quantity=r["quantity"], price=r["price"],
                commission=(
                    pence_to_decimal(r["commission"])
                    if r["commission"] is not None else None
                ),
                accrued_interest=(
                    pence_to_decimal(r["accrued_interest"])
                    if r["accrued_interest"] is not None else None
                ),
            )
            for r in cur
        ]

    def list_recent_transactions(self, limit: int = 10) -> list[TransactionRow]:
        """The most recent ``limit`` transactions across every account,
        newest first (ADR-075, Home dashboard's Recent activity card).

        A bounded ``ORDER BY … DESC LIMIT`` so the dashboard never materialises
        the full ledger (tens of thousands of rows) on every refresh. Mirrors
        ``list_all_transactions``' join/row shape; running balance is not
        meaningful across accounts and is reported as 0.

        Portfolio-move trades (buy/sell/share-move/reinvest/split) are excluded
        (ADR-090, extended to the Home feed): they carry no payee and sit on the
        Uncategorised category, so they'd otherwise fill the Recent activity card
        with "Uncategorised" rows that aren't cash activity and aren't the user's
        to categorise. Cash distributions (Div/IntInc) and the manual Cash in/out
        are *not* moves and stay."""
        from mfl_desktop.import_engine.qif_actions import (
            SHARE_IN_ACTIONS, SHARE_OUT_ACTIONS, SPLIT_ACTIONS,
        )
        moves = sorted(SHARE_IN_ACTIONS | SHARE_OUT_ACTIONS | SPLIT_ACTIONS)
        move_ph = ",".join("?" * len(moves))
        cur = self._conn.execute(
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id, "
            "       t.action, t.security_id, t.quantity, t.price, t.commission, "
            "       t.accrued_interest, "
            "       COALESCE(s.name, '') AS security_name, "
            "       COALESCE(s.symbol, '') AS security_symbol, "
            "       COALESCE(sp.c, 0) AS split_count, sp.cids AS split_cids "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "LEFT JOIN security s ON s.id = t.security_id "
            "LEFT JOIN (SELECT txn_id, COUNT(*) AS c, "
            "                  GROUP_CONCAT(category_id) AS cids "
            "           FROM txn_split GROUP BY txn_id) sp ON sp.txn_id = t.id "
            f"WHERE (t.action IS NULL OR lower(t.action) NOT IN ({move_ph})) "
            "ORDER BY t.posted_date DESC, t.id DESC LIMIT ?",
            (*moves, limit),
        )
        return [
            TransactionRow(
                id=r["id"], iri=r["iri"],
                account_id=r["account_id"], account_name=r["account_name"],
                posted_date=r["posted_date"], amount=pence_to_decimal(r["amount"]),
                payee_id=r["payee_id"], payee_name=r["payee_name"],
                category_id=r["category_id"], category_name=r["category_name"],
                status=r["status"], memo=r["memo"],
                running_balance=Decimal("0.00"),
                transfer_id=r["transfer_id"],
                split_count=r["split_count"],
                split_category_ids=_parse_split_cids(r["split_cids"]),
                action=r["action"], security_id=r["security_id"],
                security_name=r["security_name"],
                security_symbol=r["security_symbol"],
                quantity=r["quantity"], price=r["price"],
                commission=(
                    pence_to_decimal(r["commission"])
                    if r["commission"] is not None else None
                ),
                accrued_interest=(
                    pence_to_decimal(r["accrued_interest"])
                    if r["accrued_interest"] is not None else None
                ),
            )
            for r in cur
        ]

    def list_transactions_for_security(
        self, security_id: int,
    ) -> list[TransactionRow]:
        """Every investment transaction referencing one security, across all
        accounts, chronologically (ADR-047, Stock Record screen). Running
        balance is not meaningful across accounts and is reported as 0. Mirrors
        ``list_all_transactions``' join shape."""
        cur = self._conn.execute(
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id, "
            "       t.action, t.security_id, t.quantity, t.price, t.commission, "
            "       t.accrued_interest, "
            "       COALESCE(s.name, '') AS security_name, "
            "       COALESCE(s.symbol, '') AS security_symbol "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "LEFT JOIN security s ON s.id = t.security_id "
            "WHERE t.security_id = ? "
            "ORDER BY t.posted_date ASC, t.id ASC",
            (security_id,),
        )
        return [
            TransactionRow(
                id=r["id"], iri=r["iri"],
                account_id=r["account_id"], account_name=r["account_name"],
                posted_date=r["posted_date"], amount=pence_to_decimal(r["amount"]),
                payee_id=r["payee_id"], payee_name=r["payee_name"],
                category_id=r["category_id"], category_name=r["category_name"],
                status=r["status"], memo=r["memo"],
                running_balance=Decimal("0.00"),
                transfer_id=r["transfer_id"],
                action=r["action"], security_id=r["security_id"],
                security_name=r["security_name"],
                security_symbol=r["security_symbol"],
                quantity=r["quantity"], price=r["price"],
                commission=(
                    pence_to_decimal(r["commission"])
                    if r["commission"] is not None else None
                ),
                accrued_interest=(
                    pence_to_decimal(r["accrued_interest"])
                    if r["accrued_interest"] is not None else None
                ),
            )
            for r in cur
        ]

    # ── Reports (read-only aggregates) ──

    _BUCKET_EXPR = {
        # Monday-anchored week ('YYYY-Wnn'). SQLite's %W uses Monday as
        # the first day of the week, which is what UK personal-finance
        # reports usually expect.
        "week":    "strftime('%Y-W%W', t.posted_date)",
        "month":   "strftime('%Y-%m', t.posted_date)",
        # 'YYYY-Qn' for chronological sortability.
        "quarter": (
            "strftime('%Y', t.posted_date) || '-Q' "
            "|| ((CAST(strftime('%m', t.posted_date) AS INTEGER) - 1) / 3 + 1)"
        ),
        "year":    "strftime('%Y', t.posted_date)",
    }

    def _to_display_ccy(
        self,
        pence: int,
        ccy: str,
        target_ccy: str,
        on_date: str,
        unconverted: dict[str, int],
    ) -> Optional[int]:
        """Convert ``pence`` from ``ccy`` into ``target_ccy`` at ``on_date``
        (ADR-156). The shared rule behind every multi-currency aggregate.

        Returns ``None`` when there is no rate on file. The caller must then
        DROP the amount and it is recorded in ``unconverted`` — so the report can
        say "£X wasn't converted" rather than silently understating a total,
        which is the whole reason this is a helper and not an inline try/except.

        A no-op (returns ``pence`` unchanged) when no display currency is asked
        for, or when the amount is already in it — so a single-currency file
        never touches the FX tables.
        """
        target_ccy = (target_ccy or "").strip().upper()
        ccy = (ccy or "").strip().upper()
        if not target_ccy or not ccy or ccy == target_ccy:
            return pence
        converted, _fallback = self.convert_amount(
            Decimal(pence), from_ccy=ccy, to_ccy=target_ccy, on_date=on_date,
        )
        if converted is None:
            if pence:
                unconverted[ccy] = unconverted.get(ccy, 0) + abs(pence)
            return None
        return int(converted.to_integral_value(rounding=ROUND_HALF_UP))

    def spending_aggregates(
        self,
        *,
        date_from: str,
        date_to: str,
        granularity: str,
        account_ids: Optional[list[int]] = None,
        include_uncategorised: bool = True,
        payee_ids: Optional[list[int]] = None,
        display_currency: Optional[str] = None,
    ) -> dict:
        """Net spending per (bucket, category_id) over a date range.

        **Net expense** definition (ADR-129): every ``kind='expense'`` line
        contributes its **signed** amount, so a positive amount (a refund /
        reimbursement) *reduces* the category's spend — ``SUM(-amount)`` over
        all of the category's lines in the bucket. A category whose refunds
        meet or exceed its outflows for the bucket nets ``≤ 0`` and is
        **clamped to £0** (dropped): a stacked bar can't draw a negative
        segment, and the returned pence stay unambiguously ``> 0``. This
        matches the Income & Expense / Sankey / Payee reports, which share the
        definition, and reconciles with a category drill-down (net of refunds).
        Income misclassified as an expense category no longer inflates a bar —
        as a positive amount it now *reduces* the (already-clamped) net.

        ``payee_ids``, when supplied non-empty, narrows the result to
        transactions whose payee is in the set (ADR-039 saved-report
        filter dimension). ``None`` or an empty list means no payee
        narrowing — every payee contributes (including the (No payee)
        rows where ``txn.payee_id`` is NULL).

        ``display_currency`` (ADR-156) converts every amount from its **account's**
        currency into the target at ``date_to``, matching the Sankey / Payee /
        Income & Expense reports. Without it this method summed raw minor units
        across accounts — adding dollars to pounds 1:1 — which silently produced a
        meaningless total on any multi-currency file.

        Returns ``{"rows": [{bucket, category_id, spending_pence}, ...],
        "unconverted": {ccy: pence}}`` — pence are always ≥ 0. ``unconverted``
        holds amounts dropped for want of an FX rate, so the report can warn
        rather than quietly understate. Caller rolls category_id up to a "report
        group" id (see `mfl_desktop.reports.category_group_map`) and aggregates
        further.
        """
        if granularity not in self._BUCKET_EXPR:
            raise ValueError(
                f"Unknown granularity {granularity!r}; expected one of "
                f"{tuple(self._BUCKET_EXPR.keys())}"
            )
        bucket_expr = self._BUCKET_EXPR[granularity]

        filters: list[str] = []
        params: list = [date_from, date_to]

        if account_ids is not None:
            if not account_ids:
                # Empty account selection → no rows.
                return []
            ph = ",".join("?" * len(account_ids))
            filters.append(f"t.account_id IN ({ph})")
            params.extend(account_ids)

        if not include_uncategorised:
            filters.append("t.category_id != ?")
            params.append(UNCATEGORISED_ID)

        if payee_ids:
            ph = ",".join("?" * len(payee_ids))
            filters.append(f"t.payee_id IN ({ph})")
            params.extend(payee_ids)

        # ADR-090: portfolio-move trades are not spending — drop them.
        move_clause, move_params = self._portfolio_move_exclusion()
        filters.append(move_clause)
        params.extend(move_params)

        filter_sql = ""
        if filters:
            filter_sql = " AND " + " AND ".join(filters)

        sql = (
            f"SELECT {bucket_expr} AS bucket, "
            f"       t.category_id AS category_id, "
            f"       a.currency AS ccy, "
            f"       SUM(-t.amount) AS spending_pence "
            # Read from the split-unrolled view (ADR-051): a split transaction
            # contributes one row per category line (its line amount + category)
            # rather than its parent total, so spend lands on the right
            # categories. A non-split txn maps to itself, so this is identical
            # to scanning `txn` for the no-splits-in-the-file case.
            f"FROM txn_category_line t "
            f"JOIN category c ON c.id = t.category_id "
            # ADR-156: amounts are in their ACCOUNT's currency, so group by it
            # and convert each group — otherwise dollars and pounds are summed 1:1.
            f"JOIN account a ON a.id = t.account_id "
            f"WHERE t.posted_date BETWEEN ? AND ? "
            f"  AND c.kind = 'expense' "
            # Net expense (ADR-129): all signs — a positive line (refund)
            # reduces the category's spend. Net-negative categories are
            # clamped below.
            f"  {filter_sql} "
            f"GROUP BY bucket, t.category_id, a.currency "
            f"ORDER BY bucket"
        )
        cur = self._conn.execute(sql, params)
        target_ccy = (display_currency or "").strip().upper()
        unconverted: dict[str, int] = {}
        # Convert FIRST, then net, then clamp. Netting per-currency would clamp a
        # currency's refunds away before they could offset that category's spend
        # in another currency (ADR-129 nets across the whole category-bucket).
        totals: dict[tuple[str, int], int] = {}
        for r in cur:
            pence = self._to_display_ccy(
                int(r["spending_pence"]), r["ccy"], target_ccy, date_to,
                unconverted,
            )
            if pence is None:
                continue      # no rate on file — recorded in `unconverted`
            key = (r["bucket"], int(r["category_id"]))
            totals[key] = totals.get(key, 0) + pence

        out: list[dict] = []
        for (bucket, category_id), net in totals.items():
            if net <= 0:
                continue  # net refund/zero → £0 in the stack (ADR-129)
            out.append({
                "bucket": bucket,
                "category_id": category_id,
                "spending_pence": net,
            })
        out.sort(key=lambda r: r["bucket"])
        return {"rows": out, "unconverted": unconverted}

    def income_aggregates(
        self,
        *,
        date_from: str,
        date_to: str,
        granularity: str,
        account_ids: Optional[list[int]] = None,
        include_uncategorised: bool = True,
        payee_ids: Optional[list[int]] = None,
        include_reinvested: bool = False,
        display_currency: Optional[str] = None,
    ) -> dict:
        """Inflow income per (bucket, category_id) over a date range — the
        income-side mirror of :meth:`spending_aggregates` (ADR-088, Income
        Over Time report).

        Uses a **strict inflow** definition: only transactions whose amount is
        positive on a ``kind='income'`` category contribute, so the chart's
        bars are unambiguously positive and a negative correction on an income
        category doesn't flip a bar negative. Note the asymmetry (ADR-129): the
        expense side (:meth:`spending_aggregates`) now *nets* refunds rather
        than counting strict outflows, because reimbursements are an
        expense-side workflow; the income side keeps the strict rule.

        ``include_uncategorised`` is accepted for signature parity with
        ``spending_aggregates`` but is effectively inert here: the reserved
        Uncategorised category (id=1) is ``kind='expense'`` (ADR-014), so it
        never matches the income kind filter regardless of the flag.

        ``include_reinvested`` (ADR-089) folds in **reinvested distributions**
        (DRIP — ``ReinvDiv`` & co.), which carry their dividend as new shares,
        not cash: their ``amount`` is 0, so the strict-inflow query above can't
        see them. When True, a second pass values each reinvest row on a
        ``kind='income'`` category at **quantity × price** (the reinvested
        distribution = the new lot's cost) and adds it under that row's
        category, so a DRIP tagged *Dividend Income* lands alongside the cash
        dividends. Reinvests left Uncategorised (the import default) stay out —
        the owner tags them first (now possible per ADR-089). No double count:
        the cash pass requires ``amount > 0`` and reinvests are always 0.

        ``display_currency`` (ADR-156) converts every amount from its **account's**
        currency at ``date_to`` — see :meth:`spending_aggregates`. Both passes
        convert, so a USD DRIP and a GBP dividend land in the same money.

        Returns ``{"rows": [{bucket, category_id, income_pence}, ...],
        "unconverted": {ccy: pence}}`` — pence are always ≥ 0. Caller rolls
        ``category_id`` up to a report group id and aggregates further (same flow
        as the spending report). Rows are not de-duplicated across the two
        passes; the caller already sums by (bucket, group), so a category can
        legitimately appear twice.
        """
        if granularity not in self._BUCKET_EXPR:
            raise ValueError(
                f"Unknown granularity {granularity!r}; expected one of "
                f"{tuple(self._BUCKET_EXPR.keys())}"
            )
        bucket_expr = self._BUCKET_EXPR[granularity]

        filters: list[str] = []
        params: list = [date_from, date_to]

        if account_ids is not None:
            if not account_ids:
                return []
            ph = ",".join("?" * len(account_ids))
            filters.append(f"t.account_id IN ({ph})")
            params.extend(account_ids)

        if not include_uncategorised:
            filters.append("t.category_id != ?")
            params.append(UNCATEGORISED_ID)

        if payee_ids:
            ph = ",".join("?" * len(payee_ids))
            filters.append(f"t.payee_id IN ({ph})")
            params.extend(payee_ids)

        filter_sql = ""
        if filters:
            filter_sql = " AND " + " AND ".join(filters)

        sql = (
            f"SELECT {bucket_expr} AS bucket, "
            f"       t.category_id AS category_id, "
            f"       a.currency AS ccy, "
            f"       SUM(t.amount) AS income_pence "
            # Read from the split-unrolled view (ADR-051) so a split lands on
            # each line's own category (identical to scanning `txn` when the
            # file has no splits) — see spending_aggregates for the rationale.
            f"FROM txn_category_line t "
            f"JOIN category c ON c.id = t.category_id "
            f"JOIN account a ON a.id = t.account_id "   # ADR-156: per-currency
            f"WHERE t.posted_date BETWEEN ? AND ? "
            f"  AND c.kind = 'income' "
            f"  AND t.amount > 0 "  # strict inflow — see docstring
            f"  {filter_sql} "
            f"GROUP BY bucket, t.category_id, a.currency "
            f"ORDER BY bucket"
        )
        cur = self._conn.execute(sql, params)
        target_ccy = (display_currency or "").strip().upper()
        unconverted: dict[str, int] = {}
        totals: dict[tuple[str, int], int] = {}
        for r in cur:
            pence = self._to_display_ccy(
                int(r["income_pence"]), r["ccy"], target_ccy, date_to, unconverted,
            )
            if pence is None:
                continue
            key = (r["bucket"], int(r["category_id"]))
            totals[key] = totals.get(key, 0) + pence
        out = [
            {"bucket": b, "category_id": cid, "income_pence": p}
            for (b, cid), p in totals.items()
        ]

        if include_reinvested:
            out.extend(
                self._reinvested_income_rows(
                    bucket_expr=bucket_expr,
                    filter_sql=filter_sql,
                    # Same WHERE-clause params as the cash pass: [from, to] +
                    # the account / uncat / payee values appended above.
                    filter_params=params[2:],
                    date_from=date_from,
                    date_to=date_to,
                    target_ccy=target_ccy,
                    unconverted=unconverted,
                )
            )
        out.sort(key=lambda r: r["bucket"])
        return {"rows": out, "unconverted": unconverted}

    def _reinvested_income_rows(
        self,
        *,
        bucket_expr: str,
        filter_sql: str,
        filter_params: list,
        date_from: str,
        date_to: str,
        target_ccy: str = "",
        unconverted: Optional[dict[str, int]] = None,
    ) -> list[dict]:
        """Reinvested-distribution (DRIP) income per (bucket, category_id),
        valued at **quantity × price** (ADR-089). Reads ``txn`` directly — a
        reinvest is never split, so the split-unrolled view adds nothing — and
        counts only rows on a ``kind='income'`` category (Uncategorised DRIPs
        are excluded until the owner tags them). The ``filter_sql`` /
        ``filter_params`` are the caller's account / uncategorised / payee
        clauses, which reference ``t.*`` columns present on ``txn`` too."""
        from mfl_desktop.import_engine.qif_actions import REINVEST_ACTIONS

        actions = sorted(REINVEST_ACTIONS)
        action_ph = ",".join("?" * len(actions))
        sql = (
            f"SELECT {bucket_expr} AS bucket, "
            f"       t.category_id AS category_id, "
            f"       a.currency AS ccy, "
            # `price` is a per-share unit price in **pounds** (a Buy's
            # pence `amount` == quantity × price × 100), so scale to pence.
            f"       CAST(ROUND(SUM(t.quantity * t.price) * 100) AS INTEGER) AS income_pence "
            f"FROM txn t "
            f"JOIN category c ON c.id = t.category_id "
            f"JOIN account a ON a.id = t.account_id "   # ADR-156: per-currency
            f"WHERE t.posted_date BETWEEN ? AND ? "
            f"  AND c.kind = 'income' "
            f"  AND lower(t.action) IN ({action_ph}) "
            f"  AND t.quantity IS NOT NULL AND t.price IS NOT NULL "
            f"  {filter_sql} "
            f"GROUP BY bucket, t.category_id, a.currency "
            f"ORDER BY bucket"
        )
        params = [date_from, date_to] + actions + list(filter_params)
        cur = self._conn.execute(sql, params)
        if unconverted is None:
            unconverted = {}
        out: list[dict] = []
        for r in cur:
            if r["income_pence"] is None:
                continue
            pence = self._to_display_ccy(
                int(r["income_pence"]), r["ccy"], target_ccy, date_to, unconverted,
            )
            if pence is None:
                continue
            out.append({
                "bucket": r["bucket"],
                "category_id": int(r["category_id"]),
                "income_pence": pence,
                # Tag so the report can surface these as their own
                # "Reinvested Dividends" series rather than silently merging
                # them into the cash category they're tagged with (ADR-110).
                "reinvested": True,
            })
        return out

    def _portfolio_move_exclusion(self) -> tuple[str, list]:
        """SQL clause + params that exclude investment **portfolio-move
        trades** (buy / sell / share-in-out / reinvest / split) from the
        cashflow aggregations (ADR-090).

        A trade moves cash *within* the portfolio (cash ⇄ securities), so it is
        neither spending nor income — but a Buy is ``amount < 0`` on the
        Uncategorised (``kind='expense'``) category, so the expense rule
        would otherwise miscount it as spending (and, having no payee, dump it
        into the Payee report's "(No payee)" bucket). Filters by **txn id** —
        the split-unrolled ``txn_category_line`` view exposes ``txn_id`` but not
        ``action`` — and reuses ``affects_shares``' action sets so it can't
        drift from the holdings engine / importer. (Cash distributions —
        Div/IntInc/cap-gains — and the manual Cash in/out are *not* moves: they
        are real flows and stay in the reports.)"""
        from mfl_desktop.import_engine.qif_actions import (
            SHARE_IN_ACTIONS, SHARE_OUT_ACTIONS, SPLIT_ACTIONS,
        )
        moves = sorted(SHARE_IN_ACTIONS | SHARE_OUT_ACTIONS | SPLIT_ACTIONS)
        ph = ",".join("?" * len(moves))
        clause = (
            f"t.txn_id NOT IN (SELECT id FROM txn "
            f"WHERE action IS NOT NULL AND lower(action) IN ({ph}))"
        )
        return clause, moves

    def payee_spending_aggregates(
        self,
        *,
        date_from: str,
        date_to: str,
        account_ids: Optional[Iterable[int]] = None,
        display_currency: Optional[str] = None,
        include_transfers: bool = False,
    ) -> dict:
        """Net spending per **canonical payee** over a date range
        (ADR-066 / Arc E, E2 — Payee report).

        Uses the same **net-expense** definition as
        :meth:`spending_aggregates` (ADR-129): every ``kind='expense'`` line
        contributes its signed amount, so a refund / reimbursement reduces the
        payee's spend; a payee whose refunds meet or exceed its outflows nets
        ``≤ 0`` and is **clamped to £0** (dropped), keeping bars unambiguously
        positive. Reads the split-unrolled ``txn_category_line`` view (ADR-051)
        so a split lands on each line's own category.

        Aliases are rolled up to their canonical payee (ADR-028/029) via
        ``COALESCE(p.canonical_id, p.id)`` — "AMZN" and "Amazon UK" count as
        one payee, labelled with the canonical name. Lines with no payee
        (``txn.payee_id IS NULL``) collapse to a single group with
        ``payee_id = None`` (the caller labels it "(No payee)").

        ``include_transfers`` (default False) additionally drops every line
        carrying a ``transfer_id`` (a linked transfer leg) regardless of the
        category it was filed under — mirrors the Income & Expense report's
        transfer handling.

        ``account_ids`` non-empty restricts to those accounts; ``None`` /
        empty means all. ``display_currency`` converts each per-(payee,
        currency) total into that one currency via the ADR-035 FX layer at
        the period-end date (``date_to``); per ADR-055 a (payee, currency)
        slice with no rate is **excluded** (never par-added) and its
        magnitude collected under ``unconverted``. ``None`` keeps bare
        same-units pence (single-currency case).

        Returns ``{'payees': [{payee_id, name, spending_pence, txn_count}],
        'unconverted': {currency: pence}}`` — pence ≥ 0, one entry per
        canonical payee, unsorted (the pure ``payee_report`` module ranks +
        folds the long tail into "Other").
        """
        clauses = [
            "t.posted_date BETWEEN ? AND ?",
            "c.kind = 'expense'",  # net of refunds (ADR-129) — all signs
        ]
        params: list = [date_from, date_to]
        acc = list(account_ids) if account_ids else []
        if acc:
            clauses.append(f"t.account_id IN ({','.join('?' * len(acc))})")
            params.extend(acc)
        if not include_transfers:
            clauses.append("t.transfer_id IS NULL")
        # ADR-090: portfolio-move trades aren't spending (and have no payee, so
        # they'd otherwise swell the "(No payee)" bucket) — drop them.
        move_clause, move_params = self._portfolio_move_exclusion()
        clauses.append(move_clause)
        params.extend(move_params)

        cur = self._conn.execute(
            "SELECT COALESCE(p.canonical_id, p.id) AS canon_id, "
            "  canon.name AS payee_name, "
            "  a.currency AS ccy, "
            "  SUM(-t.amount) AS spending_pence, "
            "  COUNT(*) AS txn_count "
            "FROM txn_category_line t "
            "JOIN category c ON c.id = t.category_id "
            "JOIN account a ON a.id = t.account_id "
            "LEFT JOIN payee p ON p.id = t.payee_id "
            "LEFT JOIN payee canon ON canon.id = COALESCE(p.canonical_id, p.id) "
            "WHERE " + " AND ".join(clauses) + " "
            "GROUP BY canon_id, a.currency",
            params,
        )
        target_ccy = (display_currency or "").strip().upper()
        payees: dict = {}  # canon_id -> aggregated entry
        unconverted: dict[str, int] = {}
        for r in cur:
            canon_id = r["canon_id"]  # None for the no-payee group
            pence = int(r["spending_pence"])
            count = int(r["txn_count"])
            ccy = (r["ccy"] or "").strip().upper()
            if target_ccy and ccy and ccy != target_ccy:
                converted, _fb = self.convert_amount(
                    Decimal(pence), from_ccy=ccy, to_ccy=target_ccy,
                    on_date=date_to,
                )
                if converted is None:
                    if pence != 0:
                        unconverted[ccy] = unconverted.get(ccy, 0) + abs(pence)
                    continue
                pence = int(converted.to_integral_value(rounding=ROUND_HALF_UP))
            entry = payees.get(canon_id)
            if entry is None:
                entry = {
                    "payee_id": canon_id,
                    "name": r["payee_name"],
                    "spending_pence": 0,
                    "txn_count": 0,
                }
                payees[canon_id] = entry
            entry["spending_pence"] += pence
            entry["txn_count"] += count
        # Clamp net-refund payees to £0 (drop) — a payee whose refunds exceed
        # its outflows nets ≤ 0 and can't be a positive bar (ADR-129).
        kept = [e for e in payees.values() if e["spending_pence"] > 0]
        return {"payees": kept, "unconverted": unconverted}

    def category_payee_matrix(
        self,
        *,
        date_from: str,
        date_to: str,
        account_ids: Optional[Iterable[int]] = None,
        display_currency: Optional[str] = None,
        include_transfers: bool = False,
    ) -> dict:
        """Net spending per **(category, canonical payee)** cell over a
        date range (ADR-068 / Arc E, E3 — Category & Payee report).

        Same **net-expense** definition as :meth:`spending_aggregates` /
        :meth:`payee_spending_aggregates` (ADR-129): every ``kind='expense'``
        line contributes its signed amount (a refund reduces the cell), and a
        cell that nets ``≤ 0`` is **clamped to £0** (dropped). Over the
        split-unrolled ``txn_category_line`` view
        (ADR-051). ``category_id`` is the transaction's **leaf** category —
        the caller rolls it up to the budget-line level
        (``category_group_map``) and pivots either dimension. The payee is
        rolled up to its canonical id (ADR-028/029); ``payee_id`` is ``None``
        for transactions with no payee.

        ``include_transfers`` (default False) additionally drops linked
        transfer legs (``transfer_id``), mirroring the other reports.
        ``account_ids`` non-empty restricts to those accounts. Each
        per-(category, payee, currency) slice is FX-converted to
        ``display_currency`` at ``date_to``; a slice with no rate is excluded
        and its magnitude collected under ``unconverted`` (ADR-055).

        Returns ``{'cells': [{category_id, payee_id, spending_pence,
        txn_count}], 'unconverted': {currency: pence}}`` — one cell per
        (leaf category, canonical payee), pence ≥ 0, unsorted.
        """
        clauses = [
            "t.posted_date BETWEEN ? AND ?",
            "c.kind = 'expense'",  # net of refunds (ADR-129) — all signs
        ]
        params: list = [date_from, date_to]
        acc = list(account_ids) if account_ids else []
        if acc:
            clauses.append(f"t.account_id IN ({','.join('?' * len(acc))})")
            params.extend(acc)
        if not include_transfers:
            clauses.append("t.transfer_id IS NULL")
        # ADR-090: portfolio-move trades aren't spending — drop them.
        move_clause, move_params = self._portfolio_move_exclusion()
        clauses.append(move_clause)
        params.extend(move_params)

        cur = self._conn.execute(
            "SELECT t.category_id AS category_id, "
            "  COALESCE(p.canonical_id, p.id) AS canon_id, "
            "  a.currency AS ccy, "
            "  SUM(-t.amount) AS spending_pence, "
            "  COUNT(*) AS txn_count "
            "FROM txn_category_line t "
            "JOIN category c ON c.id = t.category_id "
            "JOIN account a ON a.id = t.account_id "
            "LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE " + " AND ".join(clauses) + " "
            "GROUP BY t.category_id, canon_id, a.currency",
            params,
        )
        target_ccy = (display_currency or "").strip().upper()
        cells: dict = {}  # (category_id, canon_id) -> aggregated cell
        unconverted: dict[str, int] = {}
        for r in cur:
            pence = int(r["spending_pence"])
            count = int(r["txn_count"])
            ccy = (r["ccy"] or "").strip().upper()
            if target_ccy and ccy and ccy != target_ccy:
                converted, _fb = self.convert_amount(
                    Decimal(pence), from_ccy=ccy, to_ccy=target_ccy,
                    on_date=date_to,
                )
                if converted is None:
                    if pence != 0:
                        unconverted[ccy] = unconverted.get(ccy, 0) + abs(pence)
                    continue
                pence = int(converted.to_integral_value(rounding=ROUND_HALF_UP))
            key = (r["category_id"], r["canon_id"])
            cell = cells.get(key)
            if cell is None:
                cell = {
                    "category_id": r["category_id"],
                    "payee_id": r["canon_id"],
                    "spending_pence": 0,
                    "txn_count": 0,
                }
                cells[key] = cell
            cell["spending_pence"] += pence
            cell["txn_count"] += count
        # Clamp net-refund cells to £0 (drop) — a (category, payee) cell whose
        # refunds exceed its outflows nets ≤ 0 (ADR-129).
        kept = [c for c in cells.values() if c["spending_pence"] > 0]
        return {"cells": kept, "unconverted": unconverted}

    def sankey_category_totals(
        self, *, date_from: str, date_to: str,
        account_ids: Optional[Iterable[int]] = None,
        category_ids: Optional[Iterable[int]] = None,
        display_currency: Optional[str] = None,
        include_transfers: bool = False,
        transfer_category_ids: Optional[Iterable[int]] = None,
    ) -> dict:
        """Period-scoped income and expense totals per category for the Sankey
        report (ADR-056) and the Income & Expense composition donut.

        ADR-140: with ``include_transfers`` on, ``kind='transfer'`` legs fold in
        as directional cash flows (outflow → expense, inflow → income), keyed by
        their own category so they roll up as their own slice;
        ``transfer_category_ids`` (empty == all) narrows which. Both default off,
        so the Sankey report is unchanged.

        Income = inflows (``amount > 0``) on ``kind='income'`` categories;
        expense = **net** of all signed amounts on ``kind='expense'``
        categories (ADR-129: a refund reduces the category; a category that
        nets ≤ 0 is dropped), matching ``spending_aggregates``. Transfers
        (``kind='transfer'``) are excluded entirely — they move money between
        the owner's own accounts and are neither income nor expense. Reads the
        split-unrolled ``txn_category_line`` view (ADR-051), so a split lands on
        each line's own category.

        ``account_ids`` / ``category_ids``, when non-empty, restrict the lines to
        those accounts / categories; ``None`` or empty means "all" (no
        narrowing). The category filter matches the line's own category id, so
        the caller's roll-up reflects only the selected leaves.

        ``display_currency``, when set, converts every category total from its
        account's native currency into that one currency via the ADR-035 FX
        layer, so a multi-currency portfolio sums coherently rather than adding
        bare pence across currencies. Conversion is done per (category,
        currency) bucket at the **period-end** date (``date_to``) — with the
        owner's sparse manual rates ``get_fx_rate_nearest`` resolves the same
        rate at any date, and a period-end snapshot is the natural convention
        for a flow report. Per ADR-055's policy a bucket with no rate to the
        display currency is *excluded* (never folded in at 1:1) and its
        magnitude collected under ``unconverted``. When ``display_currency`` is
        ``None`` the totals are bare same-units pence (legacy behaviour).

        Returns ``{'income': {category_id: pence}, 'expense': {category_id:
        pence}, 'unconverted': {currency: pence}}`` with pence ≥ 0, keyed by the
        leaf category the txn/line carries. The caller rolls these up the
        category tree.
        """
        income: dict[int, int] = {}
        expense: dict[int, int] = {}
        unconverted: dict[str, int] = {}
        tcats = list(transfer_category_ids) if transfer_category_ids else []
        kind_clause = (
            "(c.kind = 'income' AND t.amount > 0) OR c.kind = 'expense'"
        )
        params: list = [date_from, date_to]
        if include_transfers:
            if tcats:
                kind_clause += (
                    f" OR (c.kind = 'transfer' AND t.category_id IN "
                    f"({','.join('?' * len(tcats))}))"
                )
                params.extend(tcats)
            else:
                kind_clause += " OR c.kind = 'transfer'"
        clauses = ["t.posted_date BETWEEN ? AND ?", f"( {kind_clause} )"]
        acc = list(account_ids) if account_ids else []
        if acc:
            clauses.append(f"t.account_id IN ({','.join('?' * len(acc))})")
            params.extend(acc)
        cats = list(category_ids) if category_ids else []
        if cats:
            cat_clause = f"t.category_id IN ({','.join('?' * len(cats))})"
            if include_transfers:
                cat_clause = f"({cat_clause} OR c.kind = 'transfer')"
            clauses.append(cat_clause)
            params.extend(cats)
        # ADR-090: portfolio-move trades are neither income nor expense — drop
        # them so a Buy (amount<0 on Uncategorised/expense) doesn't show up as
        # an expense flow on the Sankey.
        move_clause, move_params = self._portfolio_move_exclusion()
        clauses.append(move_clause)
        params.extend(move_params)
        cur = self._conn.execute(
            "SELECT c.kind AS kind, t.category_id AS cid, a.currency AS ccy, "
            # ADR-140: transfer legs classified by direction; income/expense
            # keep their kind.
            "  CASE WHEN c.kind = 'income'  THEN 'income' "
            "       WHEN c.kind = 'expense' THEN 'expense' "
            "       WHEN t.amount >= 0       THEN 'income' "
            "       ELSE 'expense' END AS flow, "
            "  SUM(CASE WHEN c.kind = 'income'  THEN t.amount "
            "           WHEN c.kind = 'expense' THEN -t.amount "
            "           ELSE ABS(t.amount) END) AS pence "
            "FROM txn_category_line t "
            "JOIN category c ON c.id = t.category_id "
            "JOIN account a ON a.id = t.account_id "
            "WHERE " + " AND ".join(clauses) + " "
            "GROUP BY t.category_id, flow, c.kind, a.currency",
            params,
        )
        target_ccy = (display_currency or "").strip().upper()
        for r in cur:
            cid = int(r["cid"])
            pence = int(r["pence"])
            ccy = (r["ccy"] or "").strip().upper()
            if target_ccy and ccy and ccy != target_ccy:
                converted, _fb = self.convert_amount(
                    Decimal(pence), from_ccy=ccy, to_ccy=target_ccy,
                    on_date=date_to,
                )
                if converted is None:
                    if pence != 0:
                        unconverted[ccy] = unconverted.get(ccy, 0) + abs(pence)
                    continue
                pence = int(converted.to_integral_value(rounding=ROUND_HALF_UP))
            bucket = income if r["flow"] == "income" else expense
            bucket[cid] = bucket.get(cid, 0) + pence
        # Net-refund expense categories clamp to £0 (drop) — ADR-129. Income
        # stays as-is (strict inflow, always ≥ 0). Transfer legs are single-
        # sign per direction, so they're always ≥ 0 and survive (ADR-140).
        expense = {cid: p for cid, p in expense.items() if p > 0}
        return {"income": income, "expense": expense, "unconverted": unconverted}

    def income_expense_series(
        self,
        *,
        date_from: str,
        date_to: str,
        granularity: str,
        account_ids: Optional[Iterable[int]] = None,
        category_ids: Optional[Iterable[int]] = None,
        display_currency: Optional[str] = None,
        include_transfers: bool = False,
        transfer_category_ids: Optional[Iterable[int]] = None,
    ) -> dict:
        """Per-bucket income and expense totals for the Income & Expense
        report (ADR-064 / Arc E, E1).

        ADR-140: when ``include_transfers`` is on, ``kind='transfer'`` legs are
        folded in as **directional cash flows** — an outflow (``amount < 0``)
        counts on the *expense* side, an inflow on the *income* side (unlike
        an expense category, transfers are not netted; each leg counts by its
        own sign). ``transfer_category_ids`` narrows this to specific transfer
        categories (empty == all transfer categories) so, e.g., only a
        'Mortgage Principal' transfer feeds a rental ROI view. The category
        narrowing above never drops these transfer legs.

        Income = inflows (``amount > 0``) on ``kind='income'`` categories;
        expense = **net** of all signed amounts per ``kind='expense'``
        category, each floored at £0 (ADR-129: a refund reduces its category;
        a category netting ≤ 0 contributes nothing) — the same definition as
        the Sankey and Spending reports, so the expense bar reconciles with
        Spending Over Time. ``kind='transfer'`` categories are
        never income or expense, so they're excluded by the kind rule.
        Reads the split-unrolled ``txn_category_line`` view (ADR-051) so a
        split lands on each line's own category/kind.

        ``include_transfers`` (default False) additionally drops every line
        that carries a ``transfer_id`` — i.e. a linked transfer pair leg —
        regardless of the category kind it was filed under, so an inter-
        account move that slipped in under an income/expense category still
        stays out of the cash-flow totals. Pass True to count those legs.
        (A transfer recorded under an income/expense category but *not*
        linked as a pair has no ``transfer_id`` and is indistinguishable
        from a real flow here — the fix for that is to set its category's
        kind to ``transfer`` or reconcile it as a transfer.)

        ``granularity`` is one of ``_BUCKET_EXPR``'s keys (week / month /
        quarter / year); the caller resolves ``'auto'`` to one of those
        first. ``account_ids`` non-empty restricts to those accounts;
        ``None``/empty means all. ``category_ids`` non-empty restricts to
        lines whose own category is in the set (ADR-088 amend) — the caller
        expands a picked parent to its descendants first, so a parent
        selection naturally pulls in its children; ``None``/empty means all
        categories. The kind rule still decides income vs expense, so this
        only narrows *which* income/expense categories feed the totals.

        ``display_currency`` converts every per-(bucket, currency) total
        into that one currency via the ADR-035 FX layer, summed in the
        display currency so a mixed GBP+USD scope is coherent. Conversion
        is per (bucket, currency) at the **period-end** date (``date_to``)
        — consistent with the Sankey report and fine under the owner's
        sparse manual rates (``get_fx_rate_nearest`` resolves the same rate
        at any date). Per ADR-055's policy a bucket with no rate to the
        display currency is **excluded** (never par-added at 1:1) and its
        magnitude collected under ``unconverted``. ``None`` keeps bare
        same-units pence (single-currency case).

        Returns ``{'income': {bucket: pence}, 'expense': {bucket: pence},
        'unconverted': {currency: pence}}`` — pence ≥ 0, keyed by the
        strftime bucket string (matches ``income_expense.enumerate_buckets``).
        """
        if granularity not in self._BUCKET_EXPR:
            raise ValueError(
                f"Unknown granularity {granularity!r}; expected one of "
                f"{tuple(self._BUCKET_EXPR.keys())}"
            )
        bucket_expr = self._BUCKET_EXPR[granularity]

        income: dict[str, int] = {}
        expense: dict[str, int] = {}
        unconverted: dict[str, int] = {}

        # ADR-140: optional transfer-kind branch, narrowed to specific transfer
        # categories (empty == all). Only when include_transfers is on.
        tcats = list(transfer_category_ids) if transfer_category_ids else []
        kind_clause = (
            "(c.kind = 'income' AND t.amount > 0) "
            "OR c.kind = 'expense'"  # expense: net of refunds (ADR-129)
        )
        params: list = [date_from, date_to]
        if include_transfers:
            if tcats:
                kind_clause += (
                    f" OR (c.kind = 'transfer' AND t.category_id IN "
                    f"({','.join('?' * len(tcats))}))"
                )
                params.extend(tcats)
            else:
                kind_clause += " OR c.kind = 'transfer'"
        clauses = ["t.posted_date BETWEEN ? AND ?", f"( {kind_clause} )"]
        acc = list(account_ids) if account_ids else []
        if acc:
            clauses.append(f"t.account_id IN ({','.join('?' * len(acc))})")
            params.extend(acc)
        cats = list(category_ids) if category_ids else []
        if cats:
            # The income/expense category narrowing must not drop the transfer
            # legs we deliberately folded in (they have their own categories).
            cat_clause = f"t.category_id IN ({','.join('?' * len(cats))})"
            if include_transfers:
                cat_clause = f"({cat_clause} OR c.kind = 'transfer')"
            clauses.append(cat_clause)
            params.extend(cats)
        if not include_transfers:
            clauses.append("t.transfer_id IS NULL")
        # ADR-090: portfolio-move trades are neither income nor expense — drop
        # them so buys don't inflate the expense side / savings rate.
        move_clause, move_params = self._portfolio_move_exclusion()
        clauses.append(move_clause)
        params.extend(move_params)

        cur = self._conn.execute(
            # Group by category too (ADR-129) so each expense category's net can
            # be floored at £0 before summing — matching the per-category clamp
            # in spending_aggregates, so the Income & Expense expense bar equals
            # the Spending Over Time total for the same scope.
            f"SELECT {bucket_expr} AS bucket, c.kind AS kind, "
            f"  a.currency AS ccy, "
            # ADR-140: a transfer leg's *flow* is its direction (outflow →
            # expense, inflow → income); income/expense keep their kind.
            f"  CASE WHEN c.kind = 'income'  THEN 'income' "
            f"       WHEN c.kind = 'expense' THEN 'expense' "
            f"       WHEN t.amount >= 0       THEN 'income' "
            f"       ELSE 'expense' END AS flow, "
            f"  SUM(CASE WHEN c.kind = 'income'  THEN t.amount "
            f"           WHEN c.kind = 'expense' THEN -t.amount "
            f"           ELSE ABS(t.amount) END) AS pence "
            f"FROM txn_category_line t "
            f"JOIN category c ON c.id = t.category_id "
            f"JOIN account a ON a.id = t.account_id "
            f"WHERE " + " AND ".join(clauses) + " "
            f"GROUP BY bucket, flow, c.kind, t.category_id, a.currency",
            params,
        )
        target_ccy = (display_currency or "").strip().upper()
        for r in cur:
            bucket = r["bucket"]
            pence = int(r["pence"])
            # Net-refund expense category → £0 (ADR-129); income is strict (>0).
            # Transfer legs are grouped by direction (single-sign), so their
            # magnitude is always ≥ 0 and never floored (ADR-140).
            if r["kind"] == "expense" and pence <= 0:
                continue
            ccy = (r["ccy"] or "").strip().upper()
            if target_ccy and ccy and ccy != target_ccy:
                converted, _fb = self.convert_amount(
                    Decimal(pence), from_ccy=ccy, to_ccy=target_ccy,
                    on_date=date_to,
                )
                if converted is None:
                    if pence != 0:
                        unconverted[ccy] = unconverted.get(ccy, 0) + abs(pence)
                    continue
                pence = int(converted.to_integral_value(rounding=ROUND_HALF_UP))
            dest = income if r["flow"] == "income" else expense
            dest[bucket] = dest.get(bucket, 0) + pence
        return {"income": income, "expense": expense, "unconverted": unconverted}

    def list_categories_flat(
        self, kinds: Optional[tuple[str, ...]] = None,
    ) -> list[CategoryChoice]:
        """Return all active categories with both the immediate parent name
        (for legacy callers) and the full breadcrumb path (ADR-031), used by
        ``make_category_picker`` so typing any ancestor name reveals all its
        descendants in the typeahead.

        `kinds`, when provided, narrows the result to categories of those
        kinds (e.g. `('transfer',)` for the transfer-target picker)."""
        # First pass: read every non-archived category so we can build the
        # full breadcrumb path. Cheap (single SELECT over one table, three
        # columns, no joins) and avoids a recursive CTE.
        path_cur = self._conn.execute(
            "SELECT id, parent_id, name FROM category WHERE archived_at IS NULL"
        )
        nodes: dict[int, tuple[Optional[int], str]] = {
            r["id"]: (r["parent_id"], r["name"]) for r in path_cur
        }
        path_of: dict[int, str] = {}
        for cid in nodes:
            parts: list[str] = []
            current: Optional[int] = cid
            seen: set[int] = set()
            while current is not None and current not in seen:
                seen.add(current)
                node = nodes.get(current)
                if node is None:
                    break
                parent_id, name = node
                parts.append(name)
                current = parent_id
            path_of[cid] = " → ".join(reversed(parts)) if parts else ""

        # Second pass: kind-filtered SELECT, decorated with the path.
        params: list = []
        kind_clause = ""
        if kinds is not None:
            if not kinds:
                return []
            ph = ",".join("?" * len(kinds))
            kind_clause = f" AND c.kind IN ({ph})"
            params.extend(kinds)
        cur = self._conn.execute(
            "SELECT c.id, c.name, c.source, c.kind, "
            "       COALESCE(p.name, '') AS parent_name "
            "FROM category c "
            "LEFT JOIN category p ON p.id = c.parent_id "
            f"WHERE c.archived_at IS NULL{kind_clause}",
            params,
        )
        choices = [
            CategoryChoice(
                id=r["id"], name=r["name"],
                parent_name=r["parent_name"], source=r["source"],
                kind=r["kind"],
                path=path_of.get(r["id"], r["name"]),
            )
            for r in cur
        ]
        # Sort by full path so siblings cluster under their parent and
        # top-levels lead — DFS-style traversal in the dropdown.
        choices.sort(key=lambda c: c.path.lower())
        return choices

    def update_transaction_payee(
        self, txn_id: int, payee_name: str,
    ) -> tuple[Optional[int], str]:
        """Set the payee by name. Returns (payee_id, display_name) for the
        caller to cache; payee_id is None if the cleaned name is empty."""
        payee_id = self.get_or_create_payee(payee_name)
        self._conn.execute(
            "UPDATE txn SET payee_id = ? WHERE id = ?", (payee_id, txn_id),
        )
        self.commit()
        display = (payee_name or "").strip()
        return payee_id, display

    def update_transaction_category(
        self, txn_id: int, category_id: int,
    ) -> str:
        """Set the category. Returns the new category's display name."""
        row = self._conn.execute(
            "SELECT name FROM category WHERE id = ?", (category_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No category with id {category_id}")
        self._conn.execute(
            "UPDATE txn SET category_id = ? WHERE id = ?",
            (category_id, txn_id),
        )
        self.commit()
        return row["name"]

    def update_transaction_status(self, txn_id: int, status: str) -> None:
        if not txn_status.is_valid(status):
            raise ValueError(f"Invalid status: {status!r}")
        self._conn.execute(
            "UPDATE txn SET status = ? WHERE id = ?", (status, txn_id),
        )
        self.commit()

    def update_transaction_memo(self, txn_id: int, memo: str) -> None:
        self._conn.execute(
            "UPDATE txn SET memo = ? WHERE id = ?",
            (memo or None, txn_id),
        )
        self.commit()

    def update_transaction_date(self, txn_id: int, new_date: str) -> str:
        """Set a transaction's posted date ('YYYY-MM-DD'). Returns the stored
        date. Validates the format (ISO date) so a malformed string can't reach
        the column and break date-window / running-balance ordering.

        A transfer pair's two halves are NOT date-synced — the two sides of a
        transfer often clear on different dates (one account posts a day later),
        so each row keeps its own posted date (unlike amount, which must stay in
        sign-locked step)."""
        try:
            normalized = date.fromisoformat(str(new_date).strip()).isoformat()
        except ValueError as e:
            raise ValueError(f"Invalid date: {new_date!r}") from e
        self._conn.execute(
            "UPDATE txn SET posted_date = ? WHERE id = ?", (normalized, txn_id),
        )
        self.commit()
        return normalized

    def update_transaction_amount(
        self, txn_id: int, new_signed_amount: Decimal,
    ) -> Decimal:
        """Update a transaction's signed amount.

        Non-transfer rows: simple update. Sign is taken from the caller's
        value (a deposit corrected from +50 to -50 is allowed).

        Transfer-half rows: preserves the row's existing sign — the
        direction of a transfer is encoded by its source/destination
        accounts on the ``transfer`` parent row, so flipping a half-row's
        sign would un-pair it from the partner. The caller's magnitude
        replaces the existing magnitude. Same-currency transfers also
        sync the partner's magnitude (the two halves must agree).
        Cross-currency transfers leave the partner's amount alone and
        recompute ``transfer.rate`` from the two final magnitudes;
        ``rate_source`` becomes ``'derived'``.

        Atomic. Returns the value actually stored on this row (may
        differ from input on a transfer-sign flip — sign is coerced
        back to the original)."""
        if new_signed_amount == 0:
            raise ValueError("Transaction amount must not be zero.")
        row = self._conn.execute(
            "SELECT id, account_id, amount, transfer_id "
            "FROM txn WHERE id = ?",
            (txn_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No transaction with id {txn_id}")
        existing_pence = int(row["amount"])
        existing_sign = -1 if existing_pence < 0 else 1
        transfer_iri = row["transfer_id"]

        try:
            if transfer_iri is None:
                # Non-transfer: store exactly what the caller asked for.
                self._conn.execute(
                    "UPDATE txn SET amount = ? WHERE id = ?",
                    (decimal_to_pence(new_signed_amount), txn_id),
                )
                self.commit()
                return new_signed_amount

            # Transfer half — preserve this row's sign; treat caller's
            # value as the new magnitude regardless of the sign they
            # typed.
            new_magnitude = abs(new_signed_amount)
            stored_signed = new_magnitude * existing_sign
            self._conn.execute(
                "UPDATE txn SET amount = ? WHERE id = ?",
                (decimal_to_pence(stored_signed), txn_id),
            )
            partner = self._conn.execute(
                "SELECT id, account_id, amount FROM txn "
                "WHERE transfer_id = ? AND id != ?",
                (transfer_iri, txn_id),
            ).fetchone()
            if partner is None:
                # Defensive: half-row with no partner shouldn't happen
                # (delete is partner-aware) but if it does, just commit
                # the single-side change.
                self.commit()
                return stored_signed

            this_ccy = self.get_account_currency(row["account_id"])
            partner_ccy = self.get_account_currency(partner["account_id"])
            partner_existing_pence = int(partner["amount"])
            partner_sign = -1 if partner_existing_pence < 0 else 1

            if this_ccy == partner_ccy:
                # Same currency — partner magnitude must match.
                partner_new_signed = new_magnitude * partner_sign
                self._conn.execute(
                    "UPDATE txn SET amount = ? WHERE id = ?",
                    (decimal_to_pence(partner_new_signed), partner["id"]),
                )
                # Rate stays 1.0 / 'derived' by default; refresh anyway
                # in case a manual override had drifted.
                self._conn.execute(
                    "UPDATE transfer SET rate = 1.0, rate_source = 'derived' "
                    "WHERE iri = ?",
                    (transfer_iri,),
                )
            else:
                # Cross-currency — partner amount unchanged; recompute
                # rate from the two stored magnitudes. transfer.rate
                # convention: to_magnitude = from_magnitude × rate, with
                # from_account_id / to_account_id on the parent row.
                partner_magnitude = abs(pence_to_decimal(partner_existing_pence))
                tparent = self._conn.execute(
                    "SELECT from_account_id, to_account_id "
                    "FROM transfer WHERE iri = ?",
                    (transfer_iri,),
                ).fetchone()
                if tparent is not None:
                    if int(tparent["from_account_id"]) == int(row["account_id"]):
                        from_mag = new_magnitude
                        to_mag = partner_magnitude
                    else:
                        from_mag = partner_magnitude
                        to_mag = new_magnitude
                    if from_mag > 0:
                        new_rate = to_mag / from_mag
                        self._conn.execute(
                            "UPDATE transfer "
                            "SET rate = ?, rate_source = 'derived' "
                            "WHERE iri = ?",
                            (float(new_rate), transfer_iri),
                        )
            self.commit()
            return stored_signed
        except Exception:
            self.rollback()
            raise

    # (``_UNSET`` is defined once at the top of the class — bulk_update_*
    # and update_account share it.)

    def bulk_update_transactions(
        self,
        txn_ids: list[int],
        *,
        payee_name=_UNSET,
        category_id=_UNSET,
        status=_UNSET,
        memo=_UNSET,
    ) -> int:
        """Apply one or more field changes to every txn in `txn_ids`.

        Only fields whose value differs from the sentinel are touched. For
        the nullable columns (`payee_name`, `memo`), an empty / whitespace-
        only string clears the field (NULL). For the non-nullable enums
        (`category_id`, `status`) the value must be valid; status is
        checked here, category_id is enforced by the FK.

        All updates happen in a single SQLite transaction so a failure
        midway through leaves no partial state behind. Returns the number
        of transactions targeted (== len(txn_ids))."""
        if not txn_ids:
            return 0
        placeholders = ",".join("?" * len(txn_ids))
        try:
            if payee_name is not self._UNSET:
                clean = (payee_name or "").strip()
                payee_id = (
                    self.get_or_create_payee(clean) if clean else None
                )
                self._conn.execute(
                    f"UPDATE txn SET payee_id = ? WHERE id IN ({placeholders})",
                    (payee_id, *txn_ids),
                )
            if category_id is not self._UNSET:
                self._conn.execute(
                    f"UPDATE txn SET category_id = ? WHERE id IN ({placeholders})",
                    (int(category_id), *txn_ids),
                )
            if status is not self._UNSET:
                if not txn_status.is_valid(status):
                    raise ValueError(f"Invalid status: {status!r}")
                self._conn.execute(
                    f"UPDATE txn SET status = ? WHERE id IN ({placeholders})",
                    (status, *txn_ids),
                )
            if memo is not self._UNSET:
                # Treat blank as 'clear'; SQLite NULL renders as blank in the
                # register and is the same intent the user expressed.
                value = memo.strip() if isinstance(memo, str) else memo
                self._conn.execute(
                    f"UPDATE txn SET memo = ? WHERE id IN ({placeholders})",
                    (value or None, *txn_ids),
                )
            self.commit()
            return len(txn_ids)
        except Exception:
            self.rollback()
            raise

    def delete_transactions(self, txn_ids: list[int]) -> int:
        """Delete one or more transactions by id, **expanded to include any
        transfer partners** (per ADR-020 — both halves of a transfer are one
        logical operation, so deleting one removes both). Returns the count
        of rows actually deleted. Commits on success; rolls back on error."""
        if not txn_ids:
            return 0
        expanded = self.expand_transfer_partners(txn_ids)
        placeholders = ",".join("?" * len(expanded))
        try:
            cur = self._conn.execute(
                f"DELETE FROM txn WHERE id IN ({placeholders})",
                tuple(expanded),
            )
            # Demote any split line whose transfer partner just went away
            # (ADR-051 amendment) — e.g. the user deleted the destination half
            # of a split-line transfer directly. The split parent survives and
            # stays balanced; the line simply stops being a transfer. (Deleting
            # the split parent itself instead removes its partners via the
            # expansion above, so this only fires for orphaned line refs.)
            self._conn.execute(
                "UPDATE txn_split SET transfer_id = NULL "
                "WHERE transfer_id IS NOT NULL AND transfer_id NOT IN "
                "  (SELECT transfer_id FROM txn WHERE transfer_id IS NOT NULL)"
            )
            self.commit()
            return cur.rowcount
        except Exception:
            self.rollback()
            raise

    def expand_transfer_partners(self, txn_ids: list[int]) -> list[int]:
        """Given a list of txn ids, return the same set plus the partner
        of any id whose row has a transfer_id. Stable iteration order:
        original ids first, then any added partners. Used by both the
        delete path and the UI's confirmation prompt so the user sees the
        true count of rows that will be removed.

        A split parent's own ``txn.transfer_id`` is NULL — its transfer links
        live on its ``txn_split`` lines (ADR-051 amendment) — so the inner set
        also pulls transfer_ids from the parents' split lines, ensuring a
        split's line partners are deleted with it."""
        if not txn_ids:
            return []
        placeholders = ",".join("?" * len(txn_ids))
        cur = self._conn.execute(
            f"SELECT id FROM txn WHERE transfer_id IS NOT NULL AND "
            f"transfer_id IN ("
            f"  SELECT transfer_id FROM txn "
            f"  WHERE id IN ({placeholders}) AND transfer_id IS NOT NULL"
            f"  UNION "
            f"  SELECT transfer_id FROM txn_split "
            f"  WHERE txn_id IN ({placeholders}) AND transfer_id IS NOT NULL"
            f")",
            (*txn_ids, *txn_ids),
        )
        partners = {r["id"] for r in cur}
        # Preserve original ordering; append unseen partners at the end.
        seen = set(txn_ids)
        result = list(txn_ids)
        for pid in partners:
            if pid not in seen:
                seen.add(pid)
                result.append(pid)
        return result

    def get_transfer_partner_account_id(self, txn_id: int) -> Optional[int]:
        """For a transfer-half txn, return the partner row's account_id.

        Returns ``None`` when ``txn_id`` isn't part of a transfer (or has
        no surviving partner — shouldn't happen given ADR-020's two-row
        invariant, but defensive). Used by the "Create Schedule From
        Transaction" verb (ADR-027) to pre-fill the destination account
        when seeding a schedule from a transfer txn.
        """
        row = self._conn.execute(
            "SELECT partner.account_id AS account_id "
            "FROM txn self "
            "JOIN txn partner "
            "  ON partner.transfer_id = self.transfer_id "
            " AND partner.id != self.id "
            "WHERE self.id = ? AND self.transfer_id IS NOT NULL "
            "LIMIT 1",
            (txn_id,),
        ).fetchone()
        return row["account_id"] if row else None

    def get_default_transfer_category_id(self) -> Optional[int]:
        """The seeded top-level 'Transfer' category (added by migration 0002).
        Used as the default in the New Transfer dialog. Falls back to any
        transfer-kind row if the seeded one was renamed/deleted."""
        row = self._conn.execute(
            "SELECT id FROM category "
            "WHERE kind = 'transfer' AND archived_at IS NULL "
            "  AND parent_id IS NULL AND name = 'Transfer' LIMIT 1"
        ).fetchone()
        if row is not None:
            return row["id"]
        row = self._conn.execute(
            "SELECT id FROM category "
            "WHERE kind = 'transfer' AND archived_at IS NULL "
            "ORDER BY parent_id IS NULL DESC, id LIMIT 1"
        ).fetchone()
        return row["id"] if row is not None else None

    def create_transfer(
        self,
        *,
        from_account_id: int,
        to_account_id: int,
        posted_date: str,
        amount: Decimal,                # positive magnitude in from-account currency
        category_id: int,
        memo: str = "",
        status: str = "pending",
        to_amount: Optional[Decimal] = None,    # positive magnitude in to-account currency
        rate: Optional[Decimal] = None,         # quote per base (from → to)
        rate_source: Optional[str] = None,      # 'manual' | 'fx_rate' | 'derived'
    ) -> tuple[int, int]:
        """Create a transfer between two accounts as two linked txns + a
        parent ``transfer`` row.

        Both txns share one ``transfer_id`` IRI; the parent row records the
        exchange rate that was used at posting time so the historical
        intent survives later edits to either half. The source's payee is
        "Transfer to <dest>"; the destination's is "Transfer from <source>"
        — gives the register a self-documenting display without a special
        UI marker.

        **Same-currency** (``from_account.currency == to_account.currency``):
        ``rate=1.0``, ``rate_source='derived'`` are filled in automatically;
        ``to_amount`` defaults to ``amount``. Passing inconsistent values
        is rejected.

        **Cross-currency** (currencies differ): the two-of-three rule on
        ``amount`` / ``to_amount`` / ``rate``:

        - If ``to_amount`` and ``rate`` are both given, they must agree
          (within 0.1%); ``rate_source`` defaults to ``'manual'``.
        - If only ``to_amount`` is given, ``rate`` is back-derived; source
          defaults to ``'derived'``.
        - If only ``rate`` is given, ``to_amount = amount × rate``; source
          defaults to ``'manual'``.
        - If neither is given, the FX rate is looked up from the ``fx_rate``
          table for ``posted_date`` (nearest-prior fallback per ADR-035);
          source defaults to ``'fx_rate'``. Raises ``ValueError`` if no
          rate is available.

        Validation:
        - source and destination must differ;
        - amount must be > 0;
        - status must be a valid txn.status enum value;
        - any supplied rate / to_amount must be > 0.

        Atomic — either both txn halves and the parent row land or nothing
        lands. Returns the ``(source_txn_id, destination_txn_id)`` pair.
        """
        if from_account_id == to_account_id:
            raise ValueError("Source and destination accounts must differ.")
        if amount <= 0:
            raise ValueError("Transfer amount must be greater than zero.")
        if not txn_status.is_valid(status):
            raise ValueError(f"Invalid status: {status!r}")
        if to_amount is not None and to_amount <= 0:
            raise ValueError("to_amount must be greater than zero.")
        if rate is not None and rate <= 0:
            raise ValueError("rate must be greater than zero.")

        # Look up both accounts in one query — used for both name labelling
        # and currency-aware rate resolution.
        rows = self._conn.execute(
            "SELECT id, name, currency FROM account WHERE id IN (?, ?)",
            (from_account_id, to_account_id),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        if from_account_id not in by_id or to_account_id not in by_id:
            raise ValueError("Unknown account id passed to create_transfer.")
        from_name = by_id[from_account_id]["name"]
        to_name = by_id[to_account_id]["name"]
        from_ccy = by_id[from_account_id]["currency"]
        to_ccy = by_id[to_account_id]["currency"]

        # Resolve the rate / to_amount / rate_source triple per the rules above.
        if from_ccy == to_ccy:
            if to_amount is not None and abs(to_amount - amount) > Decimal("0.005"):
                raise ValueError(
                    "to_amount must equal amount for a same-currency transfer."
                )
            if rate is not None and abs(rate - Decimal("1")) > Decimal("0.0001"):
                raise ValueError(
                    "rate must be 1 for a same-currency transfer."
                )
            resolved_to_amount = amount
            resolved_rate = Decimal("1")
            resolved_source = rate_source or "derived"
        else:
            if to_amount is not None and rate is not None:
                implied = amount * rate
                tol = to_amount * Decimal("0.001")
                if abs(implied - to_amount) > tol:
                    raise ValueError(
                        "Supplied rate and to_amount are inconsistent."
                    )
                resolved_to_amount = to_amount
                resolved_rate = rate
                resolved_source = rate_source or "manual"
            elif to_amount is not None:
                resolved_to_amount = to_amount
                resolved_rate = to_amount / amount
                resolved_source = rate_source or "derived"
            elif rate is not None:
                resolved_to_amount = amount * rate
                resolved_rate = rate
                resolved_source = rate_source or "manual"
            else:
                looked, _, _ = self.get_fx_rate_nearest(
                    posted_date, from_ccy, to_ccy,
                )
                if looked is None:
                    raise ValueError(
                        f"No FX rate available for {from_ccy} → {to_ccy} "
                        f"on or before {posted_date}; supply a rate or "
                        f"to_amount, or refresh rates first."
                    )
                resolved_rate = looked
                resolved_to_amount = amount * looked
                resolved_source = rate_source or "fx_rate"

        transfer_iri = new_transfer_iri()
        try:
            payee_to = self.get_or_create_payee(f"Transfer to {to_name}")
            payee_from = self.get_or_create_payee(f"Transfer from {from_name}")
            source_id = self._insert_transfer_half(
                account_id=from_account_id,
                amount=-amount,
                payee_id=payee_to,
                category_id=category_id,
                status=status,
                memo=memo,
                posted_date=posted_date,
                transfer_id=transfer_iri,
            )
            dest_id = self._insert_transfer_half(
                account_id=to_account_id,
                amount=resolved_to_amount,
                payee_id=payee_from,
                category_id=category_id,
                status=status,
                memo=memo,
                posted_date=posted_date,
                transfer_id=transfer_iri,
            )
            self._insert_transfer_parent(
                iri=transfer_iri,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                rate=resolved_rate,
                rate_source=resolved_source,
            )
            self.commit()
            return source_id, dest_id
        except Exception:
            self.rollback()
            raise

    def convert_to_transfer(
        self,
        *,
        txn_id: int,
        other_account_id: int,
        to_amount: Optional[Decimal] = None,
        rate: Optional[Decimal] = None,
        rate_source: Optional[str] = None,
    ) -> int:
        """Pair an existing txn with a new partner row to form a transfer.

        Generates one shared ``transfer_id``, applies it to the existing
        txn, inserts a partner on ``other_account_id`` with the opposite-
        sign amount, and writes the transfer parent row recording the
        exchange rate that was used (ADR-035). Cross-currency: pass
        ``to_amount`` or ``rate`` to override the FX-table lookup. Atomic.
        Returns the partner txn id.
        """
        try:
            partner_id, _ = self._convert_to_transfer_unbatched(
                txn_id, other_account_id,
                to_amount=to_amount,
                rate=rate,
                rate_source=rate_source,
            )
            self.commit()
            return partner_id
        except Exception:
            self.rollback()
            raise

    def bulk_convert_to_transfers(
        self,
        txn_ids: list[int],
        other_account_id: int,
    ) -> list[int]:
        """Convert each of ``txn_ids`` into a transfer paired with a fresh
        partner on ``other_account_id``. Each pair gets its own
        ``transfer_id`` and parent row. All-or-nothing: any failure rolls
        back every change. Returns partner ids in input order.

        Cross-currency rates come from the FX table (per-txn lookup at
        each row's posted_date). For per-row rate overrides use
        ``bulk_match_or_create_transfers`` with explicit ``CreateNew``
        decisions instead.
        """
        if not txn_ids:
            return []
        try:
            partner_ids = [
                self._convert_to_transfer_unbatched(tid, other_account_id)[0]
                for tid in txn_ids
            ]
            self.commit()
            return partner_ids
        except Exception:
            self.rollback()
            raise

    def bulk_set_category_and_convert(
        self,
        txn_ids: list[int],
        *,
        category_id: int,
        other_account_id: int,
        payee_name=None,
        status=None,
        memo=None,
    ) -> list[int]:
        """Atomic combination of `bulk_update_transactions` and
        `bulk_convert_to_transfers` used by the bulk-edit dispatcher when
        the user picks a transfer-kind category. The category is updated
        (so the partner inherits the new one) plus optional other fields,
        then a partner row is created per source txn against
        `other_account_id`. All in one SQL transaction.

        `payee_name` / `status` / `memo` follow the same _UNSET sentinel
        convention as `bulk_update_transactions` — leave them at the
        default to skip that column. Returns the partner ids in input
        order."""
        if not txn_ids:
            return []
        placeholders = ",".join("?" * len(txn_ids))
        try:
            self._conn.execute(
                f"UPDATE txn SET category_id = ? WHERE id IN ({placeholders})",
                (int(category_id), *txn_ids),
            )
            if payee_name is not self._UNSET and payee_name is not None:
                clean = (payee_name or "").strip()
                payee_id = (
                    self.get_or_create_payee(clean) if clean else None
                )
                self._conn.execute(
                    f"UPDATE txn SET payee_id = ? WHERE id IN ({placeholders})",
                    (payee_id, *txn_ids),
                )
            if status is not self._UNSET and status is not None:
                if not txn_status.is_valid(status):
                    raise ValueError(f"Invalid status: {status!r}")
                self._conn.execute(
                    f"UPDATE txn SET status = ? WHERE id IN ({placeholders})",
                    (status, *txn_ids),
                )
            if memo is not self._UNSET and memo is not None:
                value = memo.strip() if isinstance(memo, str) else memo
                self._conn.execute(
                    f"UPDATE txn SET memo = ? WHERE id IN ({placeholders})",
                    (value or None, *txn_ids),
                )
            partner_ids = [
                self._convert_to_transfer_unbatched(tid, other_account_id)[0]
                for tid in txn_ids
            ]
            self.commit()
            return partner_ids
        except Exception:
            self.rollback()
            raise

    def _convert_to_transfer_unbatched(
        self, txn_id: int, other_account_id: int,
        *,
        to_amount: Optional[Decimal] = None,
        rate: Optional[Decimal] = None,
        rate_source: Optional[str] = None,
    ) -> tuple[int, str]:
        """Internal helper used by both single and bulk convert. Does NOT
        commit — caller is responsible for the transaction boundary.

        Cross-currency aware (ADR-035): when the source's account currency
        and ``other_account_id``'s currency differ, the partner's amount
        is computed from ``to_amount`` / ``rate`` (using the same two-of-
        three rule as ``create_transfer``) or looked up from ``fx_rate``.
        The transfer parent row records the rate that was used.

        Returns ``(partner_txn_id, transfer_iri)``.
        """
        row = self._conn.execute(
            "SELECT t.id, t.account_id, t.amount, t.posted_date, "
            "       t.category_id, t.status, t.memo, t.transfer_id, "
            "       a.name AS account_name, a.currency AS account_currency "
            "FROM txn t "
            "JOIN account a ON a.id = t.account_id "
            "WHERE t.id = ?",
            (txn_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No transaction with id {txn_id}")
        if row["transfer_id"] is not None:
            raise ValueError(
                f"Transaction {txn_id} is already part of a transfer."
            )
        if row["account_id"] == other_account_id:
            raise ValueError(
                "Destination account must differ from the transaction's "
                "own account."
            )
        if int(row["amount"]) == 0:
            raise ValueError(
                "Cannot convert a zero-amount transaction to a transfer."
            )
        if to_amount is not None and to_amount <= 0:
            raise ValueError("to_amount must be greater than zero.")
        if rate is not None and rate <= 0:
            raise ValueError("rate must be greater than zero.")

        other_acct = self.get_account_by_id(other_account_id)
        if other_acct is None:
            raise ValueError(
                f"Unknown destination account id {other_account_id}"
            )

        src_amount_pence = int(row["amount"])
        src_magnitude = abs(pence_to_decimal(src_amount_pence))
        src_ccy = row["account_currency"]
        dst_ccy = other_acct.currency

        # Step 1 — resolve the partner side's magnitude and a tentative
        # rate in the caller's convention ("partner per src"). The caller
        # supplies either ``to_amount`` (= partner magnitude), ``rate``
        # (= partner per src), both (must agree), or neither (FX lookup).
        if src_ccy == dst_ccy:
            if to_amount is not None and abs(to_amount - src_magnitude) > Decimal("0.005"):
                raise ValueError(
                    "to_amount must equal the source magnitude for "
                    "same-currency transfers."
                )
            partner_magnitude = src_magnitude
            caller_rate = Decimal("1")
            resolved_source = rate_source or "derived"
        else:
            if to_amount is not None and rate is not None:
                implied = src_magnitude * rate
                tol = to_amount * Decimal("0.001")
                if abs(implied - to_amount) > tol:
                    raise ValueError(
                        "Supplied rate and to_amount are inconsistent."
                    )
                partner_magnitude = to_amount
                caller_rate = rate
                resolved_source = rate_source or "manual"
            elif to_amount is not None:
                partner_magnitude = to_amount
                caller_rate = to_amount / src_magnitude
                resolved_source = rate_source or "derived"
            elif rate is not None:
                partner_magnitude = src_magnitude * rate
                caller_rate = rate
                resolved_source = rate_source or "manual"
            else:
                looked, _, _ = self.get_fx_rate_nearest(
                    row["posted_date"], src_ccy, dst_ccy,
                )
                if looked is None:
                    raise ValueError(
                        f"No FX rate available for {src_ccy} → {dst_ccy} "
                        f"on or before {row['posted_date']}; supply a rate "
                        f"or to_amount, or refresh rates first."
                    )
                partner_magnitude = src_magnitude * looked
                caller_rate = looked
                resolved_source = rate_source or "fx_rate"

        # Step 2 — direction. Partner sign opposes the source's sign
        # (the partner row is the other side of the same money movement);
        # from / to ids reflect actual money flow.
        if src_amount_pence < 0:
            # Outflow: money leaves the source's account, arrives at the
            # partner. from=src, to=partner.
            partner_amount = partner_magnitude
            from_id, to_id = row["account_id"], other_account_id
            from_magnitude, to_magnitude = src_magnitude, partner_magnitude
        else:
            # Inflow: money arrives at the source's account from the
            # partner. from=partner, to=src.
            partner_amount = -partner_magnitude
            from_id, to_id = other_account_id, row["account_id"]
            from_magnitude, to_magnitude = partner_magnitude, src_magnitude

        # Step 3 — convert caller's "partner per src" rate to the
        # transfer table's "to per from" convention. For outflow these
        # match (src=from, partner=to); for inflow they invert. Using
        # the magnitudes is more numerically stable than dividing
        # ``caller_rate``.
        if from_magnitude > 0:
            resolved_rate = to_magnitude / from_magnitude
        else:
            resolved_rate = caller_rate

        transfer_iri = new_transfer_iri()
        self._conn.execute(
            "UPDATE txn SET transfer_id = ? WHERE id = ?",
            (transfer_iri, txn_id),
        )
        # Partner inherits date, category, status, memo from the existing
        # txn so reports treat the pair as one consistent block.
        partner_payee_id = self.get_or_create_payee(
            f"Transfer from {row['account_name']}"
        )
        partner_iri = new_transaction_iri()
        cur = self._conn.execute(
            "INSERT INTO txn "
            "(iri, account_id, posted_date, amount, payee_id, category_id, "
            " status, memo, import_hash, import_batch_id, transfer_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                partner_iri, other_account_id, row["posted_date"],
                decimal_to_pence(partner_amount), partner_payee_id,
                row["category_id"], row["status"], row["memo"], transfer_iri,
            ),
        )
        self._insert_transfer_parent(
            iri=transfer_iri,
            from_account_id=from_id,
            to_account_id=to_id,
            rate=resolved_rate,
            rate_source=resolved_source,
        )
        return cur.lastrowid, transfer_iri

    def _insert_transfer_half(
        self,
        *,
        account_id: int,
        amount: Decimal,
        payee_id: Optional[int],
        category_id: int,
        status: str,
        memo: str,
        posted_date: str,
        transfer_id: str,
    ) -> int:
        iri = new_transaction_iri()
        cur = self._conn.execute(
            "INSERT INTO txn "
            "(iri, account_id, posted_date, amount, payee_id, category_id, "
            " status, memo, import_hash, import_batch_id, transfer_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                iri, account_id, posted_date, decimal_to_pence(amount),
                payee_id, category_id, status, memo or None, transfer_id,
            ),
        )
        return cur.lastrowid

    def _insert_transfer_parent(
        self,
        *,
        iri: str,
        from_account_id: int,
        to_account_id: int,
        rate: Decimal,
        rate_source: str,
    ) -> None:
        """Insert one row into the ``transfer`` parent table (ADR-035).

        Does NOT commit — the caller's transaction boundary holds. Used by
        every code path that creates a transfer pair (create_transfer,
        _convert_to_transfer_unbatched, _link_transfer_unbatched, the
        transfer branch of post_scheduled_txn).
        """
        if rate_source not in TRANSFER_RATE_SOURCES:
            raise ValueError(
                f"Invalid rate_source {rate_source!r}; "
                f"expected one of {TRANSFER_RATE_SOURCES}."
            )
        if rate <= 0:
            raise ValueError("Transfer rate must be greater than zero.")
        self._conn.execute(
            "INSERT INTO transfer "
            "(iri, from_account_id, to_account_id, rate, rate_source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                iri, from_account_id, to_account_id,
                float(rate), rate_source,
            ),
        )

    def get_transfer(self, iri: str) -> Optional[TransferRow]:
        """Look up a transfer parent row by its IRI (the shared ``txn.transfer_id``)."""
        row = self._conn.execute(
            "SELECT iri, from_account_id, to_account_id, rate, rate_source, "
            "       created_at "
            "FROM transfer WHERE iri = ?",
            (iri,),
        ).fetchone()
        if row is None:
            return None
        return TransferRow(
            iri=row["iri"],
            from_account_id=int(row["from_account_id"]),
            to_account_id=int(row["to_account_id"]),
            rate=Decimal(str(row["rate"])),
            rate_source=row["rate_source"],
            created_at=row["created_at"],
        )

    def update_transfer_rate(
        self,
        iri: str,
        *,
        rate: Decimal,
        rate_source: str,
    ) -> None:
        """Override the recorded exchange rate on a transfer parent row.

        Used by a future Edit Transfer dialog and by manual cross-currency
        adjustments. Does NOT rewrite either half-row's amount — those are
        the source of truth for what hit each account's statement. This
        only updates the *intent* metadata on the parent row.
        """
        if rate_source not in TRANSFER_RATE_SOURCES:
            raise ValueError(
                f"Invalid rate_source {rate_source!r}; "
                f"expected one of {TRANSFER_RATE_SOURCES}."
            )
        if rate <= 0:
            raise ValueError("Transfer rate must be greater than zero.")
        try:
            self._conn.execute(
                "UPDATE transfer SET rate = ?, rate_source = ? WHERE iri = ?",
                (float(rate), rate_source, iri),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Settings (key/value, ADR-035) ──────────────────────────────────────

    def get_setting(
        self, key: str, default: Optional[str] = None,
    ) -> Optional[str]:
        """Read a flat key/value from the ``setting`` table.

        Returns the stored string, or ``default`` if the key is absent or
        has an explicit empty value. Callers that want an empty string
        treated as a real value should read the row directly.
        """
        row = self._conn.execute(
            "SELECT value FROM setting WHERE key = ?", (key,),
        ).fetchone()
        if row is None:
            return default
        v = row["value"]
        if v is None or v == "":
            return default
        return v

    def set_setting(self, key: str, value: Optional[str]) -> None:
        """Upsert a flat key/value into the ``setting`` table. Empty value
        stores an empty string (use this to clear without removing the row)."""
        try:
            self._conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value if value is not None else ""),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_base_currency(self, code: str) -> None:
        """Set the file's base (reporting) currency (ADR-098, first-run).

        The app reads the base currency from the ``setting`` table key
        ``base_currency`` (Home dashboard, Net Worth, sidebar, FX refresh),
        with the seed also stamping ``person.base_currency``. This writes
        **both** in one transaction so the setting (what's read) and the
        person row (the seeded MRL-boundary value) stay in agreement.
        ``code`` is uppercased; empty is rejected."""
        clean = (code or "").strip().upper()
        if not clean:
            raise ValueError("Base currency cannot be empty.")
        try:
            self._conn.execute(
                "INSERT INTO setting (key, value) VALUES ('base_currency', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (clean,),
            )
            self._conn.execute("UPDATE person SET base_currency = ?", (clean,))
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_person_name(self, name: str) -> None:
        """Set the account holder's display name (ADR-119).

        Drives the header avatar initials and personalises the app. Trims; an
        empty name is rejected. Targets the primary (lowest-id) person row — the
        app is single-person at the MRL boundary."""
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Name cannot be empty.")
        try:
            self._conn.execute(
                "UPDATE person SET name = ? "
                "WHERE id = (SELECT MIN(id) FROM person)",
                (clean,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Foreign-exchange rates (ADR-035) ───────────────────────────────────

    def upsert_fx_rate(
        self,
        *,
        date: str,
        base: str,
        quote: str,
        rate: Decimal,
        source: str = "manual",
    ) -> None:
        """Insert or update one FX rate row.

        Same-currency upserts are a no-op (the conversion path early-exits
        on equal currencies; storing rate=1 self-pairs would just be noise).
        """
        b = (base or "").strip().upper()
        q = (quote or "").strip().upper()
        if not b or not q:
            raise ValueError("Currency codes cannot be empty.")
        if rate <= 0:
            raise ValueError("Rate must be greater than zero.")
        if source not in ("manual", "openexchangerates", "derived"):
            raise ValueError(f"Invalid rate source {source!r}.")
        if b == q:
            return
        try:
            self._conn.execute(
                "INSERT INTO fx_rate (date, base, quote, rate, source) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(date, base, quote) DO UPDATE SET "
                "  rate = excluded.rate, source = excluded.source",
                (date, b, q, float(rate), source),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def get_fx_rate_on(
        self, on_date: str, base: str, quote: str,
    ) -> Optional[Decimal]:
        """Exact-date bilateral lookup. Returns None if missing."""
        b = base.strip().upper()
        q = quote.strip().upper()
        if b == q:
            return Decimal("1")
        row = self._conn.execute(
            "SELECT rate FROM fx_rate "
            "WHERE date = ? AND base = ? AND quote = ?",
            (on_date, b, q),
        ).fetchone()
        return Decimal(str(row["rate"])) if row is not None else None

    def get_fx_rate_nearest(
        self, on_date: str, base: str, quote: str,
    ) -> tuple[Optional[Decimal], Optional[str], bool]:
        """Walk the six-step lookup chain (ADR-035 §missing-rate policy):

        1. Exact bilateral ``(on_date, base, quote)``.
        2. Exact inverse — we have ``quote → base`` on that date; return
           ``1 / rate`` (covers the common case where every provider rate
           is stored ``USD → X`` and the caller asks for ``X → USD``).
        3. Exact USD-pivot — derives the cross-rate from ``USD → base``
           and ``USD → quote`` on the same date (when neither side is USD).
        4. Nearest-prior bilateral.
        5. Nearest-prior inverse.
        6. Nearest-prior USD-pivot.

        Returns ``(rate, rate_date_used, was_fallback)``. ``rate_date_used``
        is the date of the row(s) that fed the lookup — None when nothing
        was found. ``was_fallback`` is True when the rate came from a
        prior-date row (steps 4-6), False when it came from exact date.
        Same-currency returns ``(Decimal('1'), on_date, False)`` immediately.
        """
        b = base.strip().upper()
        q = quote.strip().upper()
        if b == q:
            return Decimal("1"), on_date, False

        # 1. Exact bilateral.
        row = self._conn.execute(
            "SELECT rate FROM fx_rate "
            "WHERE date = ? AND base = ? AND quote = ?",
            (on_date, b, q),
        ).fetchone()
        if row is not None:
            return Decimal(str(row["rate"])), on_date, False

        # 2. Exact inverse — stored quote → base; return reciprocal.
        row = self._conn.execute(
            "SELECT rate FROM fx_rate "
            "WHERE date = ? AND base = ? AND quote = ?",
            (on_date, q, b),
        ).fetchone()
        if row is not None and float(row["rate"]) > 0:
            return Decimal("1") / Decimal(str(row["rate"])), on_date, False

        # 3. Exact USD-pivot (when neither side is USD).
        if b != "USD" and q != "USD":
            row = self._conn.execute(
                "SELECT b.rate AS brate, q.rate AS qrate "
                "FROM fx_rate b JOIN fx_rate q "
                "  ON q.date = b.date AND q.base = 'USD' AND q.quote = ? "
                "WHERE b.base = 'USD' AND b.quote = ? AND b.date = ?",
                (q, b, on_date),
            ).fetchone()
            if row is not None and row["brate"] and float(row["brate"]) > 0:
                cross = Decimal(str(row["qrate"])) / Decimal(str(row["brate"]))
                return cross, on_date, False

        # 4. Nearest-prior bilateral.
        row = self._conn.execute(
            "SELECT date, rate FROM fx_rate "
            "WHERE base = ? AND quote = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (b, q, on_date),
        ).fetchone()
        if row is not None:
            return Decimal(str(row["rate"])), row["date"], True

        # 5. Nearest-prior inverse.
        row = self._conn.execute(
            "SELECT date, rate FROM fx_rate "
            "WHERE base = ? AND quote = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (q, b, on_date),
        ).fetchone()
        if row is not None and float(row["rate"]) > 0:
            return (
                Decimal("1") / Decimal(str(row["rate"])),
                row["date"],
                True,
            )

        # 6. Nearest-prior USD-pivot — find the latest date where both
        # USD → base and USD → quote exist on or before on_date.
        if b != "USD" and q != "USD":
            row = self._conn.execute(
                "SELECT b.date AS bdate, b.rate AS brate, q.rate AS qrate "
                "FROM fx_rate b JOIN fx_rate q "
                "  ON q.date = b.date AND q.base = 'USD' AND q.quote = ? "
                "WHERE b.base = 'USD' AND b.quote = ? AND b.date <= ? "
                "ORDER BY b.date DESC LIMIT 1",
                (q, b, on_date),
            ).fetchone()
            if row is not None and row["brate"] and float(row["brate"]) > 0:
                cross = Decimal(str(row["qrate"])) / Decimal(str(row["brate"]))
                return cross, row["bdate"], True

        return None, None, True

    def convert_amount(
        self,
        amount: Decimal,
        *,
        from_ccy: str,
        to_ccy: str,
        on_date: str,
    ) -> tuple[Optional[Decimal], bool]:
        """Convert ``amount`` from ``from_ccy`` to ``to_ccy`` on ``on_date``.

        Returns ``(converted_amount, was_fallback)``. Same-currency returns
        ``(amount, False)`` without touching the database. When no rate
        exists at all (not even a prior fallback), returns ``(None, True)``
        — the caller decides whether to surface "rate missing" or treat as
        zero. The converted amount is *not* rounded; callers that need
        currency-precision rounding apply ``Decimal.quantize`` themselves.
        """
        f = from_ccy.strip().upper()
        t = to_ccy.strip().upper()
        if f == t:
            return amount, False
        rate, _, fb = self.get_fx_rate_nearest(on_date, f, t)
        if rate is None:
            return None, True
        return amount * rate, fb

    def list_distinct_currencies(self) -> list[str]:
        """Every currency code in use on a non-archived account, sorted.
        Feeds the report display-currency selector."""
        cur = self._conn.execute(
            "SELECT DISTINCT currency FROM account "
            "WHERE archived_at IS NULL ORDER BY currency"
        )
        return [r["currency"] for r in cur]

    def list_known_rate_pairs(self) -> list[tuple[str, str]]:
        """Every distinct (base, quote) we've ever stored a rate for, sorted."""
        cur = self._conn.execute(
            "SELECT DISTINCT base, quote FROM fx_rate "
            "ORDER BY base, quote"
        )
        return [(r["base"], r["quote"]) for r in cur]

    def get_account_currency(self, account_id: int) -> Optional[str]:
        """Single-column lookup of an account's currency. Returns None if
        the account doesn't exist. Cheap; used heavily by the matcher and
        report conversion paths."""
        row = self._conn.execute(
            "SELECT currency FROM account WHERE id = ?", (account_id,),
        ).fetchone()
        return row["currency"] if row is not None else None

    # ── Transfer matching (ADR-036) and reconcile (ADR-037) ───────────────

    def _matcher_settings(
        self,
        window_days: Optional[int],
        fx_tolerance_pct: Optional[float],
    ) -> tuple[int, float]:
        """Read defaults from ``setting`` when kwargs are None. Centralised
        so single-flow + bulk + reconcile all converge on one source of
        truth for the tunables."""
        if window_days is None:
            v = self.get_setting("transfer_match_window_days")
            window_days = int(v) if v else 3
        if fx_tolerance_pct is None:
            v = self.get_setting("transfer_fx_tolerance_pct")
            fx_tolerance_pct = float(v) if v else 1.0
        return window_days, fx_tolerance_pct

    def find_transfer_candidates(
        self,
        *,
        source_txn_id: int,
        other_account_id: int,
        window_days: Optional[int] = None,
        fx_tolerance_pct: Optional[float] = None,
    ) -> list[TransferCandidate]:
        """Look for an existing other-side row that could pair with the
        source. Used by the single-flow matcher (ADR-036) when the user
        marks a row as transfer and picks a destination account.

        Returns the candidates that pass the hard filters (opposite sign,
        unmatched, within date window, amount within tolerance), sorted
        by score desc. Empty list when nothing matches — caller falls
        through to the create-new path.
        """
        from mfl_desktop.transfer_reconcile import (
            score_candidate, strength_for_score,
        )

        window_days, fx_tolerance_pct = self._matcher_settings(
            window_days, fx_tolerance_pct,
        )

        src = self._conn.execute(
            "SELECT t.id, t.account_id, t.amount, t.posted_date, t.transfer_id, "
            "       COALESCE(p.name, '') AS payee_name "
            "FROM txn t LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.id = ?",
            (source_txn_id,),
        ).fetchone()
        if src is None:
            raise ValueError(f"No transaction with id {source_txn_id}")
        if src["transfer_id"] is not None:
            raise ValueError(
                f"Source transaction {source_txn_id} is already a transfer."
            )
        if src["account_id"] == other_account_id:
            raise ValueError("Other account must differ from source account.")

        src_acct = self.get_account_by_id(src["account_id"])
        other_acct = self.get_account_by_id(other_account_id)
        if src_acct is None or other_acct is None:
            raise ValueError("Unknown account id.")

        src_amount_pence = int(src["amount"])
        if src_amount_pence == 0:
            return []
        src_magnitude = abs(pence_to_decimal(src_amount_pence))
        src_payee = src["payee_name"]
        src_date = date.fromisoformat(src["posted_date"])
        currencies_match = src_acct.currency == other_acct.currency

        # Expected magnitude on the candidate side.
        if currencies_match:
            expected_magnitude = src_magnitude
        else:
            spot, _, _ = self.get_fx_rate_nearest(
                src["posted_date"], src_acct.currency, other_acct.currency,
            )
            if spot is None:
                # Cross-currency with no rate anywhere — can't match.
                return []
            expected_magnitude = src_magnitude * spot

        amount_sign_sql = "t.amount > 0" if src_amount_pence < 0 else "t.amount < 0"
        d_min = (src_date - timedelta(days=window_days)).isoformat()
        d_max = (src_date + timedelta(days=window_days)).isoformat()

        cur = self._conn.execute(
            f"SELECT t.id, t.account_id, t.amount, t.posted_date, "
            f"       t.category_id, "
            f"       COALESCE(p.name, '') AS payee_name "
            f"FROM txn t LEFT JOIN payee p ON p.id = t.payee_id "
            f"WHERE t.account_id = ? AND t.transfer_id IS NULL "
            f"  AND {amount_sign_sql} "
            f"  AND t.posted_date BETWEEN ? AND ?",
            (other_account_id, d_min, d_max),
        )

        candidates: list[TransferCandidate] = []
        for r in cur:
            cand_magnitude = abs(pence_to_decimal(int(r["amount"])))
            if expected_magnitude > 0:
                mismatch_pct = (
                    abs(float(cand_magnitude - expected_magnitude))
                    / float(expected_magnitude) * 100.0
                )
            else:
                mismatch_pct = 0.0
            # Hard filters: same-currency near-exact; cross-currency within
            # fx_tolerance × 5 (anything wider isn't really a transfer).
            if currencies_match and mismatch_pct > 0.01:
                continue
            if not currencies_match and mismatch_pct > fx_tolerance_pct * 5.0:
                continue
            cand_date = date.fromisoformat(r["posted_date"])
            days_apart = (cand_date - src_date).days
            score = score_candidate(
                days_apart=days_apart,
                amount_mismatch_pct=mismatch_pct,
                currencies_match=currencies_match,
                src_payee=src_payee,
                tgt_payee=r["payee_name"],
            )
            candidates.append(TransferCandidate(
                txn_id=int(r["id"]),
                account_id=int(r["account_id"]),
                account_name=other_acct.name,
                account_currency=other_acct.currency,
                posted_date=r["posted_date"],
                amount=pence_to_decimal(int(r["amount"])),
                payee_name=r["payee_name"],
                category_id=int(r["category_id"]),
                days_apart=days_apart,
                amount_mismatch_pct=mismatch_pct,
                currencies_match=currencies_match,
                expected_amount=expected_magnitude,
                score=score,
                strength=strength_for_score(score),
            ))
        candidates.sort(
            key=lambda c: (-c.score, abs(c.days_apart), c.txn_id),
        )
        return candidates

    def link_transfer(
        self,
        *,
        source_txn_id: int,
        candidate_txn_id: int,
        category_id: int,
        rate: Optional[Decimal] = None,
        rate_source: Optional[str] = None,
    ) -> str:
        """Link two existing unmatched rows into one transfer pair (ADR-036).

        Atomic: writes a fresh ``transfer_id`` IRI on both rows, rewrites
        both rows' ``category_id`` to ``category_id`` (the rewrite is the
        whole point — the source's chosen transfer category propagates to
        the other half), and inserts the ``transfer`` parent row.

        When ``rate`` is None: same-currency uses 1.0; cross-currency
        back-derives the rate from the two magnitudes (rate = target /
        source magnitude). ``rate_source`` defaults to ``'derived'``;
        callers passing a user-typed rate should set ``'manual'``.

        Returns the new transfer IRI.
        """
        try:
            iri = self._link_transfer_unbatched(
                source_txn_id=source_txn_id,
                candidate_txn_id=candidate_txn_id,
                category_id=category_id,
                rate=rate,
                rate_source=rate_source,
            )
            self.commit()
            return iri
        except Exception:
            self.rollback()
            raise

    def _read_transfer_side(
        self, txn_id: int, split_id: Optional[int],
    ) -> dict:
        """Read one side of a link — a whole ``txn`` or (ADR-139) a ``txn_split``
        line. Returns ``{table, id, account_id, amount, transfer_id, is_split}``.
        For a split line ``account_id`` is its parent txn's account."""
        if split_id is not None:
            r = self._conn.execute(
                "SELECT ts.id, ts.amount, ts.transfer_id, t.account_id "
                "FROM txn_split ts JOIN txn t ON t.id = ts.txn_id "
                "WHERE ts.id = ?",
                (split_id,),
            ).fetchone()
            if r is None:
                raise ValueError(f"No split line with id {split_id}")
            return {
                "table": "txn_split", "id": int(r["id"]),
                "account_id": int(r["account_id"]), "amount": int(r["amount"]),
                "transfer_id": r["transfer_id"], "is_split": True,
            }
        r = self._conn.execute(
            "SELECT id, account_id, amount, transfer_id FROM txn WHERE id = ?",
            (txn_id,),
        ).fetchone()
        if r is None:
            raise ValueError(f"No transaction with id {txn_id}")
        return {
            "table": "txn", "id": int(r["id"]),
            "account_id": int(r["account_id"]), "amount": int(r["amount"]),
            "transfer_id": r["transfer_id"], "is_split": False,
        }

    def _link_transfer_unbatched(
        self,
        *,
        source_txn_id: int,
        candidate_txn_id: int,
        category_id: int,
        rate: Optional[Decimal] = None,
        rate_source: Optional[str] = None,
        source_split_id: Optional[int] = None,
        candidate_split_id: Optional[int] = None,
    ) -> str:
        """Internal helper. Does NOT commit; caller owns the transaction
        boundary. Returns the new transfer IRI.

        ADR-139: either side may be a *split line* (pass its ``txn_split`` id in
        ``source_split_id`` / ``candidate_split_id``); the transfer_id is then
        stamped on that ``txn_split`` row instead of a ``txn`` row — the same
        shape ``_make_split_line_transfer`` produces, so the register + split
        editor render it identically. Split-line links are same-currency only."""
        src = self._read_transfer_side(source_txn_id, source_split_id)
        cand = self._read_transfer_side(candidate_txn_id, candidate_split_id)
        if src["transfer_id"] is not None:
            raise ValueError("Source is already part of a transfer.")
        if cand["transfer_id"] is not None:
            raise ValueError("Candidate is already part of a transfer.")
        if src["account_id"] == cand["account_id"]:
            raise ValueError(
                "Source and candidate must be on different accounts."
            )
        if (src["amount"] < 0) == (cand["amount"] < 0):
            raise ValueError(
                "Source and candidate amounts must have opposite signs "
                "to form a transfer pair."
            )
        if rate is not None and rate <= 0:
            raise ValueError("rate must be greater than zero.")

        src_ccy = self.get_account_currency(src["account_id"])
        cand_ccy = self.get_account_currency(cand["account_id"])
        if (src["is_split"] or cand["is_split"]) and src_ccy != cand_ccy:
            # A split-line transfer is modelled same-currency only (rate=1);
            # cross-currency split transfers aren't supported.
            raise ValueError(
                "Split-line transfers must be between same-currency accounts."
            )
        src_magnitude = abs(pence_to_decimal(src["amount"]))
        cand_magnitude = abs(pence_to_decimal(cand["amount"]))

        # Determine from/to: outflow (amount<0) is the source side.
        if src["amount"] < 0:
            from_id, to_id = src["account_id"], cand["account_id"]
            from_magnitude, to_magnitude = src_magnitude, cand_magnitude
        else:
            from_id, to_id = cand["account_id"], src["account_id"]
            from_magnitude, to_magnitude = cand_magnitude, src_magnitude

        if rate is None:
            if src_ccy == cand_ccy or from_magnitude <= 0:
                resolved_rate = Decimal("1")
            else:
                resolved_rate = to_magnitude / from_magnitude
            resolved_source = rate_source or "derived"
        else:
            resolved_rate = rate
            resolved_source = rate_source or "manual"

        transfer_iri = new_transfer_iri()
        for side in (src, cand):
            self._conn.execute(
                f"UPDATE {side['table']} SET transfer_id = ?, category_id = ? "
                "WHERE id = ?",
                (transfer_iri, category_id, side["id"]),
            )
        self._insert_transfer_parent(
            iri=transfer_iri,
            from_account_id=from_id,
            to_account_id=to_id,
            rate=resolved_rate,
            rate_source=resolved_source,
        )
        return transfer_iri

    def bulk_match_or_create_transfers(
        self,
        plan: list[BulkTransferDecision],
    ) -> BulkTransferResult:
        """Apply a batch of mixed link / create-new decisions atomically
        (ADR-036). Each entry is a ``LinkExisting`` or ``CreateNew``;
        single SQL transaction; any failure rolls back the whole batch.
        """
        if not plan:
            return BulkTransferResult(
                linked=0, created=0, transfer_iris=[],
            )
        linked = 0
        created = 0
        iris: list[str] = []
        try:
            for decision in plan:
                if isinstance(decision, LinkExisting):
                    iri = self._link_transfer_unbatched(
                        source_txn_id=decision.source_txn_id,
                        candidate_txn_id=decision.candidate_txn_id,
                        category_id=decision.category_id,
                        rate=decision.rate,
                        rate_source=decision.rate_source,
                        source_split_id=decision.source_split_id,
                        candidate_split_id=decision.candidate_split_id,
                    )
                    linked += 1
                    iris.append(iri)
                elif isinstance(decision, CreateNew):
                    # Stamp the chosen category on the source row first;
                    # _convert_to_transfer_unbatched copies the source's
                    # category onto the partner, so this propagates
                    # cleanly. Bulk-edit phase 1 may already have done
                    # this, but being defensive is cheap.
                    self._conn.execute(
                        "UPDATE txn SET category_id = ? WHERE id = ?",
                        (decision.category_id, decision.source_txn_id),
                    )
                    _, iri = self._convert_to_transfer_unbatched(
                        decision.source_txn_id,
                        decision.other_account_id,
                        to_amount=decision.to_amount,
                        rate=decision.rate,
                        rate_source=decision.rate_source,
                    )
                    created += 1
                    iris.append(iri)
                else:
                    raise ValueError(
                        f"Unknown transfer decision type: "
                        f"{type(decision).__name__}"
                    )
            self.commit()
            return BulkTransferResult(
                linked=linked, created=created, transfer_iris=iris,
            )
        except Exception:
            self.rollback()
            raise

    def _transfer_candidate_rows(
        self, account_id: int, *, include_splits: bool,
    ) -> list[dict]:
        """Unmatched transfer candidates on an account (ADR-037/139): every
        whole non-split txn with ``transfer_id IS NULL``, plus — when
        ``include_splits`` — each unlinked ``txn_split`` line of a split txn.

        A split line carries its own signed ``amount`` (so a £460.26 principal
        line inside a £700 payment is matchable), the *parent's* posted_date +
        payee, and its line ``memo``. Split parents themselves are excluded from
        the whole-txn set — only their lines compete."""
        rows: list[dict] = []
        for r in self._conn.execute(
            "SELECT t.id AS txn_id, t.posted_date, t.amount, "
            "       COALESCE(p.name, '') AS payee_name "
            "FROM txn t LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.account_id = ? AND t.transfer_id IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM txn_split ts WHERE ts.txn_id = t.id) "
            "ORDER BY t.posted_date",
            (account_id,),
        ):
            rows.append({
                "txn_id": int(r["txn_id"]), "split_id": None,
                "posted_date": r["posted_date"], "amount": int(r["amount"]),
                "payee_name": r["payee_name"], "memo": "",
            })
        if include_splits:
            for r in self._conn.execute(
                "SELECT ts.id AS split_id, ts.txn_id AS txn_id, t.posted_date, "
                "       ts.amount, COALESCE(p.name, '') AS payee_name, "
                "       COALESCE(ts.memo, '') AS memo "
                "FROM txn_split ts JOIN txn t ON t.id = ts.txn_id "
                "LEFT JOIN payee p ON p.id = t.payee_id "
                "WHERE t.account_id = ? AND ts.transfer_id IS NULL "
                "ORDER BY t.posted_date",
                (account_id,),
            ):
                rows.append({
                    "txn_id": int(r["txn_id"]), "split_id": int(r["split_id"]),
                    "posted_date": r["posted_date"], "amount": int(r["amount"]),
                    "payee_name": r["payee_name"], "memo": r["memo"] or "",
                })
        return rows

    def find_transfer_pairs(
        self,
        *,
        account_a_id: int,
        account_b_id: int,
        window_days: Optional[int] = None,
        fx_tolerance_pct: Optional[float] = None,
    ) -> list[TransferPair]:
        """Pair every unmatched row on ``account_a_id`` with the best
        opposite-sign candidate on ``account_b_id`` (ADR-037).

        Greedy: walk score-desc, each source / target row claimed at
        most once. Returns the resulting pairs (Strong → Good → Possible).
        Anything that didn't pair (no opposite-sign row in window /
        amount tolerance / spot rate available) is filtered out — the
        caller surfaces those separately by re-querying for unmatched
        rows after the dialog applies.
        """
        from mfl_desktop.transfer_reconcile import (
            score_candidate, strength_for_score, greedy_pair,
        )

        if account_a_id == account_b_id:
            raise ValueError("Source and target accounts must differ.")
        window_days, fx_tolerance_pct = self._matcher_settings(
            window_days, fx_tolerance_pct,
        )

        a_acct = self.get_account_by_id(account_a_id)
        b_acct = self.get_account_by_id(account_b_id)
        if a_acct is None or b_acct is None:
            raise ValueError("Unknown account id.")

        currencies_match = a_acct.currency == b_acct.currency
        # ADR-139: split lines join the pool only for same-currency account
        # pairs (a split-line transfer is modelled same-currency, rate=1).
        a_rows = self._transfer_candidate_rows(
            account_a_id, include_splits=currencies_match,
        )
        b_rows = self._transfer_candidate_rows(
            account_b_id, include_splits=currencies_match,
        )
        candidates: list[TransferPair] = []

        for ar in a_rows:
            a_date = date.fromisoformat(ar["posted_date"])
            a_amount = int(ar["amount"])
            if a_amount == 0:
                continue
            for br in b_rows:
                b_amount = int(br["amount"])
                if b_amount == 0:
                    continue
                b_date = date.fromisoformat(br["posted_date"])
                days_apart_signed = (b_date - a_date).days
                if abs(days_apart_signed) > window_days:
                    continue
                if (a_amount < 0) == (b_amount < 0):
                    continue  # same sign, can't be a transfer pair

                # Source = outflow side.
                if a_amount < 0:
                    src_row, tgt_row = ar, br
                    src_acct, tgt_acct = a_acct, b_acct
                else:
                    src_row, tgt_row = br, ar
                    src_acct, tgt_acct = b_acct, a_acct

                src_magnitude = abs(pence_to_decimal(int(src_row["amount"])))
                tgt_magnitude = abs(pence_to_decimal(int(tgt_row["amount"])))
                if src_magnitude <= 0:
                    continue
                implied_rate = tgt_magnitude / src_magnitude

                if currencies_match:
                    spot_rate: Optional[Decimal] = Decimal("1")
                    rate_deviation: Optional[float] = 0.0
                    amount_mismatch_pct = (
                        abs(float(tgt_magnitude - src_magnitude))
                        / float(src_magnitude) * 100.0
                    )
                else:
                    spot, _, _ = self.get_fx_rate_nearest(
                        src_row["posted_date"],
                        src_acct.currency, tgt_acct.currency,
                    )
                    if spot is None:
                        # Without a spot rate we can't reasonably score
                        # a cross-currency pair; skip rather than mislead.
                        continue
                    spot_rate = spot
                    rate_deviation = float(
                        (implied_rate - spot_rate) / spot_rate
                    ) * 100.0
                    expected_tgt = src_magnitude * spot_rate
                    amount_mismatch_pct = (
                        abs(float(tgt_magnitude - expected_tgt))
                        / float(expected_tgt) * 100.0
                    )
                # Hard filters
                if currencies_match and amount_mismatch_pct > 0.01:
                    continue
                if not currencies_match and amount_mismatch_pct > fx_tolerance_pct * 5.0:
                    continue

                src_date = date.fromisoformat(src_row["posted_date"])
                tgt_date = date.fromisoformat(tgt_row["posted_date"])
                days_apart_abs = abs((tgt_date - src_date).days)
                score = score_candidate(
                    days_apart=days_apart_abs,
                    amount_mismatch_pct=amount_mismatch_pct,
                    currencies_match=currencies_match,
                    src_payee=src_row["payee_name"],
                    tgt_payee=tgt_row["payee_name"],
                )
                candidates.append(TransferPair(
                    source_txn_id=int(src_row["txn_id"]),
                    source_account_id=src_acct.id,
                    source_amount=pence_to_decimal(int(src_row["amount"])),
                    source_currency=src_acct.currency,
                    source_posted_date=src_row["posted_date"],
                    source_payee=src_row["payee_name"],
                    target_txn_id=int(tgt_row["txn_id"]),
                    target_account_id=tgt_acct.id,
                    target_amount=pence_to_decimal(int(tgt_row["amount"])),
                    target_currency=tgt_acct.currency,
                    target_posted_date=tgt_row["posted_date"],
                    target_payee=tgt_row["payee_name"],
                    days_apart=days_apart_abs,
                    implied_rate=implied_rate,
                    spot_rate=spot_rate,
                    rate_deviation_pct=rate_deviation,
                    score=score,
                    strength=strength_for_score(score),
                    source_split_id=src_row["split_id"],
                    source_split_memo=(src_row["memo"] or None
                                       if src_row["split_id"] else None),
                    target_split_id=tgt_row["split_id"],
                    target_split_memo=(tgt_row["memo"] or None
                                       if tgt_row["split_id"] else None),
                ))

        # Keys are unique per candidate row — a split line is keyed by its own
        # split id so two lines of one parent (and the parent's other whole
        # candidates) never collide (ADR-139).
        return greedy_pair(
            candidates,
            source_key=lambda p: (p.source_txn_id, p.source_split_id or 0),
            target_key=lambda p: (p.target_txn_id, p.target_split_id or 0),
        )

    # ── Scheduled transactions (ADR-023) ──

    _SCHEDULED_COLS = (
        "s.id, s.iri, s.account_id, a.name AS account_name, "
        "s.payee_id, COALESCE(p.name, '') AS payee_name, "
        "s.category_id, c.name AS category_name, c.kind AS category_kind, "
        "s.transfer_to_account_id, "
        "COALESCE(ta.name, '') AS transfer_to_account_name, "
        "s.estimated_amount, s.variable, "
        "COALESCE(s.memo, '') AS memo, "
        "s.cadence, s.anchor_date, s.next_due_date, s.end_date, "
        "s.auto_post, COALESCE(s.notes, '') AS notes, s.archived_at"
    )

    def _row_to_scheduled(self, row) -> ScheduledTxnRow:
        return ScheduledTxnRow(
            id=row["id"], iri=row["iri"],
            account_id=row["account_id"], account_name=row["account_name"],
            payee_id=row["payee_id"], payee_name=row["payee_name"],
            category_id=row["category_id"],
            category_name=row["category_name"],
            category_kind=row["category_kind"],
            transfer_to_account_id=row["transfer_to_account_id"],
            transfer_to_account_name=row["transfer_to_account_name"],
            estimated_amount=pence_to_decimal(row["estimated_amount"]),
            variable=bool(row["variable"]),
            memo=row["memo"], cadence=row["cadence"],
            anchor_date=row["anchor_date"],
            next_due_date=row["next_due_date"],
            end_date=row["end_date"],
            auto_post=bool(row["auto_post"]),
            notes=row["notes"],
            archived_at=row["archived_at"],
        )

    def list_scheduled_txns(
        self, include_archived: bool = False,
    ) -> list[ScheduledTxnRow]:
        """Active schedules in next-due-date order. Pass ``include_archived``
        to see archives too (used by the management dialog's filter)."""
        where = "" if include_archived else "WHERE s.archived_at IS NULL"
        cur = self._conn.execute(
            f"SELECT {self._SCHEDULED_COLS} "
            f"FROM scheduled_txn s "
            f"JOIN      account  a  ON a.id = s.account_id "
            f"LEFT JOIN payee    p  ON p.id = s.payee_id "
            f"JOIN      category c  ON c.id = s.category_id "
            f"LEFT JOIN account  ta ON ta.id = s.transfer_to_account_id "
            f"{where} "
            f"ORDER BY s.next_due_date ASC, s.id ASC"
        )
        return [self._row_to_scheduled(r) for r in cur]

    def get_scheduled_txn(self, schedule_id: int) -> Optional[ScheduledTxnRow]:
        row = self._conn.execute(
            f"SELECT {self._SCHEDULED_COLS} "
            f"FROM scheduled_txn s "
            f"JOIN      account  a  ON a.id = s.account_id "
            f"LEFT JOIN payee    p  ON p.id = s.payee_id "
            f"JOIN      category c  ON c.id = s.category_id "
            f"LEFT JOIN account  ta ON ta.id = s.transfer_to_account_id "
            f"WHERE s.id = ?",
            (schedule_id,),
        ).fetchone()
        return self._row_to_scheduled(row) if row is not None else None

    def list_schedules_due_through(
        self, through_date: str,
    ) -> list[ScheduledTxnRow]:
        """Every active schedule with ``next_due_date <= through_date``.
        Used by the launch-time auto-post sweep and (in round B) by the
        budget screen's planned-spending projection."""
        cur = self._conn.execute(
            f"SELECT {self._SCHEDULED_COLS} "
            f"FROM scheduled_txn s "
            f"JOIN      account  a  ON a.id = s.account_id "
            f"LEFT JOIN payee    p  ON p.id = s.payee_id "
            f"JOIN      category c  ON c.id = s.category_id "
            f"LEFT JOIN account  ta ON ta.id = s.transfer_to_account_id "
            f"WHERE s.archived_at IS NULL AND s.next_due_date <= ? "
            f"ORDER BY s.next_due_date ASC, s.id ASC",
            (through_date,),
        )
        return [self._row_to_scheduled(r) for r in cur]

    def create_scheduled_txn(
        self,
        *,
        account_id: int,
        payee_name: str,
        category_id: int,
        estimated_amount: Decimal,
        cadence: str,
        anchor_date: str,
        end_date: Optional[str] = None,
        next_due_date: Optional[str] = None,
        transfer_to_account_id: Optional[int] = None,
        variable: bool = False,
        auto_post: bool = False,
        memo: str = "",
        notes: str = "",
    ) -> int:
        """Insert a new schedule. ``next_due_date`` defaults to ``anchor_date``
        (the first occurrence is the anchor); pass an explicit value to
        skip ahead. Validates cadence + transfer-kind / destination-account
        consistency. Commits on success.
        """
        if cadence not in SCHEDULE_CADENCES:
            raise ValueError(
                f"Invalid cadence {cadence!r}; expected one of {SCHEDULE_CADENCES}."
            )
        kind = self.get_category_kind(category_id)
        if kind is None:
            raise ValueError(f"No category with id {category_id}")
        if kind == "transfer":
            if transfer_to_account_id is None:
                raise ValueError(
                    "Transfer-kind categories require a destination account."
                )
            if transfer_to_account_id == account_id:
                raise ValueError(
                    "Destination account must differ from the source."
                )
        else:
            # Stamp out a destination if the caller mistakenly set one — keeps
            # the column meaningful (NULL iff non-transfer).
            transfer_to_account_id = None
        if estimated_amount == 0:
            raise ValueError("Estimated amount cannot be zero.")
        if next_due_date is None:
            next_due_date = anchor_date

        payee_id = self.get_or_create_payee(payee_name)
        iri = new_scheduled_txn_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO scheduled_txn "
                "(iri, account_id, payee_id, category_id, "
                " transfer_to_account_id, estimated_amount, variable, "
                " memo, cadence, anchor_date, next_due_date, end_date, "
                " auto_post, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iri, account_id, payee_id, category_id,
                    transfer_to_account_id,
                    decimal_to_pence(estimated_amount),
                    1 if variable else 0,
                    memo or None, cadence, anchor_date, next_due_date,
                    end_date, 1 if auto_post else 0, notes or None,
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return cur.lastrowid

    def update_scheduled_txn(
        self,
        schedule_id: int,
        *,
        account_id: int,
        payee_name: str,
        category_id: int,
        estimated_amount: Decimal,
        cadence: str,
        anchor_date: str,
        next_due_date: str,
        end_date: Optional[str],
        transfer_to_account_id: Optional[int],
        variable: bool,
        auto_post: bool,
        memo: str,
        notes: str,
    ) -> None:
        """Replace every editable field on the schedule. Validation matches
        ``create_scheduled_txn``. Does not retro-edit any txns that were
        already materialised from prior occurrences — past posts are the
        responsibility of the register, not the schedule.
        """
        if cadence not in SCHEDULE_CADENCES:
            raise ValueError(
                f"Invalid cadence {cadence!r}; expected one of {SCHEDULE_CADENCES}."
            )
        kind = self.get_category_kind(category_id)
        if kind is None:
            raise ValueError(f"No category with id {category_id}")
        if kind == "transfer":
            if transfer_to_account_id is None:
                raise ValueError(
                    "Transfer-kind categories require a destination account."
                )
            if transfer_to_account_id == account_id:
                raise ValueError(
                    "Destination account must differ from the source."
                )
        else:
            transfer_to_account_id = None
        if estimated_amount == 0:
            raise ValueError("Estimated amount cannot be zero.")

        payee_id = self.get_or_create_payee(payee_name)
        try:
            self._conn.execute(
                "UPDATE scheduled_txn SET "
                "  account_id = ?, payee_id = ?, category_id = ?, "
                "  transfer_to_account_id = ?, estimated_amount = ?, "
                "  variable = ?, memo = ?, cadence = ?, anchor_date = ?, "
                "  next_due_date = ?, end_date = ?, auto_post = ?, "
                "  notes = ? "
                "WHERE id = ?",
                (
                    account_id, payee_id, category_id,
                    transfer_to_account_id,
                    decimal_to_pence(estimated_amount),
                    1 if variable else 0,
                    memo or None, cadence, anchor_date, next_due_date,
                    end_date, 1 if auto_post else 0, notes or None,
                    schedule_id,
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_scheduled_transfer_destination(
        self, schedule_id: int, transfer_to_account_id: int,
    ) -> None:
        """Set the destination account on a transfer-kind schedule that has
        none (ADR-074).

        A schedule can end up transfer-kind without a destination — e.g. its
        category was switched to a transfer kind after the schedule was
        created, or it was seeded from a transaction without one. Posting then
        failed with no way to fix it; the Post Now flow now captures the
        destination and persists it here so this and future posts work.
        Validates the destination differs from the schedule's own account.
        Commits."""
        sched = self.get_scheduled_txn(schedule_id)
        if sched is None:
            raise ValueError(f"No schedule with id {schedule_id}")
        if transfer_to_account_id == sched.account_id:
            raise ValueError(
                "The destination account can't be the same as the source."
            )
        try:
            self._conn.execute(
                "UPDATE scheduled_txn SET transfer_to_account_id = ? "
                "WHERE id = ?",
                (transfer_to_account_id, schedule_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def delete_scheduled_txn(self, schedule_id: int) -> None:
        """Hard-delete a schedule. Already-materialised txns are untouched —
        the schedule is a template, the txn is the truth, and the link
        between them is not stored in v1 (see ADR-023 deferrals)."""
        try:
            self._conn.execute(
                "DELETE FROM scheduled_txn WHERE id = ?", (schedule_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    @staticmethod
    def compute_next_due_date(
        anchor_date: str, cadence: str, current_due: str,
    ) -> str:
        """Next occurrence after ``current_due``, anchored at ``anchor_date``.

        For weekly/biweekly the math is current + 7 / + 14 days. For
        monthly / quarterly / annual the next occurrence is anchor-based —
        we add one period's months to the current month and use
        ``min(anchor.day, days_in_target_month)`` so a "31st of every
        month" schedule produces Jan 31 → Feb 28 → Mar 31 (not Mar 28),
        and a "Feb 29" annual schedule produces 2024-02-29 → 2025-02-28
        → 2026-02-28 → 2027-02-28 → 2028-02-29.
        """
        if cadence not in SCHEDULE_CADENCES:
            raise ValueError(
                f"Invalid cadence {cadence!r}; expected one of {SCHEDULE_CADENCES}."
            )
        cur = date.fromisoformat(current_due)
        if cadence == "weekly":
            return (cur + timedelta(days=7)).isoformat()
        if cadence == "biweekly":
            return (cur + timedelta(days=14)).isoformat()
        anchor = date.fromisoformat(anchor_date)
        months_step = {"monthly": 1, "quarterly": 3, "annual": 12}[cadence]
        # Total months from anchor to current, then advance by one step.
        total_months = (
            (cur.year - anchor.year) * 12 + (cur.month - anchor.month)
        )
        next_month_offset = total_months + months_step
        target_year = anchor.year + (anchor.month - 1 + next_month_offset) // 12
        target_month = (anchor.month - 1 + next_month_offset) % 12 + 1
        target_day = min(anchor.day, calendar.monthrange(target_year, target_month)[1])
        return date(target_year, target_month, target_day).isoformat()

    def post_scheduled_txn(
        self,
        schedule_id: int,
        actual_amount: Optional[Decimal] = None,
        to_amount: Optional[Decimal] = None,
    ) -> int:
        """Materialise the next occurrence and advance ``next_due_date``.

        For non-transfer categories: inserts one txn via the same path as
        manual entry (``insert_transaction``). For transfer-kind categories:
        uses ``create_transfer`` with the source/destination derived from
        the schedule's account and ``transfer_to_account_id``, with the
        direction determined by the sign of the estimated amount (negative
        = outflow, the schedule's own account is the source).

        ``actual_amount`` overrides the stored estimate; required for
        variable schedules (the dialog passes the user-entered amount),
        ignored on fixed schedules unless the caller deliberately passes
        one. Sign of the actual amount must match the sign of the estimate
        — a fixed-direction schedule producing an inverted-sign txn is
        almost always a bug.

        ``to_amount`` (ADR-035 amendment 2026-06-07) is the explicit
        magnitude on the **``transfer_to_account_id`` side** in that
        account's currency, regardless of inflow/outflow direction. Lets
        the manual Post Now flow collect the partner-side amount when no
        FX rate is on file, instead of erroring on the lookup. The rate
        is back-derived from the two amounts and recorded as
        ``rate_source='derived'``. Same-currency / non-transfer
        schedules: ignored.

        Atomic: the txn insert / transfer pair plus the schedule update
        run in one SQLite transaction. If the post advances past
        ``end_date`` the schedule is archived in the same transaction.

        Returns the txn id (or the source-half id for transfers).
        """
        sched = self.get_scheduled_txn(schedule_id)
        if sched is None:
            raise ValueError(f"No schedule with id {schedule_id}")
        if sched.archived_at is not None:
            raise ValueError("Schedule is archived; cannot post.")

        amount = (
            sched.estimated_amount if actual_amount is None else actual_amount
        )
        if amount == 0:
            raise ValueError("Cannot post a zero amount.")
        if (amount > 0) != (sched.estimated_amount > 0):
            raise ValueError(
                "Actual amount sign does not match the schedule's direction. "
                f"Expected {'inflow' if sched.estimated_amount > 0 else 'outflow'}."
            )

        posted_date = sched.next_due_date
        try:
            if sched.category_kind == "transfer":
                if sched.transfer_to_account_id is None:
                    raise ValueError(
                        "Transfer schedule is missing a destination account."
                    )
                # Direction: estimated_amount sign tells us which side is the
                # source. Negative → schedule's own account is the source.
                # The magnitude carried by the schedule is in *the schedule's
                # account currency*, which is the source side when the
                # estimate is negative and the destination side when it's
                # positive.
                if amount < 0:
                    from_id = sched.account_id
                    to_id = sched.transfer_to_account_id
                    known_from_magnitude: Optional[Decimal] = abs(amount)
                    known_to_magnitude: Optional[Decimal] = None
                else:
                    from_id = sched.transfer_to_account_id
                    to_id = sched.account_id
                    known_from_magnitude = None
                    known_to_magnitude = abs(amount)

                from_acct_row = self._conn.execute(
                    "SELECT name, currency FROM account WHERE id = ?",
                    (from_id,),
                ).fetchone()
                to_acct_row = self._conn.execute(
                    "SELECT name, currency FROM account WHERE id = ?",
                    (to_id,),
                ).fetchone()
                from_name = from_acct_row["name"]
                to_name = to_acct_row["name"]
                from_ccy = from_acct_row["currency"]
                to_ccy = to_acct_row["currency"]

                if from_ccy == to_ccy:
                    if known_from_magnitude is None:
                        known_from_magnitude = known_to_magnitude
                    if known_to_magnitude is None:
                        known_to_magnitude = known_from_magnitude
                    used_rate = Decimal("1")
                    used_rate_source = "derived"
                elif to_amount is not None:
                    # ADR-035 amendment 2026-06-07: caller supplied the
                    # destination-side magnitude — no FX lookup needed.
                    # In both directions ``to_amount`` is the magnitude on
                    # the to_id side, so it directly fills the unknown
                    # half regardless of sign.
                    if to_amount <= 0:
                        raise ValueError(
                            "to_amount must be greater than zero."
                        )
                    if known_from_magnitude is None:
                        known_from_magnitude = to_amount
                    else:
                        known_to_magnitude = to_amount
                    used_rate = known_to_magnitude / known_from_magnitude
                    used_rate_source = "derived"
                else:
                    looked, _, _ = self.get_fx_rate_nearest(
                        posted_date, from_ccy, to_ccy,
                    )
                    if looked is None:
                        raise ValueError(
                            f"No FX rate available for {from_ccy} → "
                            f"{to_ccy} on or before {posted_date}; refresh "
                            f"rates or edit the schedule."
                        )
                    used_rate = looked
                    used_rate_source = "fx_rate"
                    if known_from_magnitude is None:
                        # Schedule is inflow; we know to_magnitude.
                        known_from_magnitude = known_to_magnitude / looked
                    else:
                        known_to_magnitude = known_from_magnitude * looked

                # create_transfer commits internally; replicate its work
                # inline so we can include the schedule update in the same
                # transaction.
                transfer_iri = new_transfer_iri()
                payee_to = self.get_or_create_payee(f"Transfer to {to_name}")
                payee_from = self.get_or_create_payee(
                    f"Transfer from {from_name}"
                )
                source_id = self._insert_transfer_half(
                    account_id=from_id, amount=-known_from_magnitude,
                    payee_id=payee_to, category_id=sched.category_id,
                    status="pending", memo=sched.memo,
                    posted_date=posted_date, transfer_id=transfer_iri,
                )
                self._insert_transfer_half(
                    account_id=to_id, amount=known_to_magnitude,
                    payee_id=payee_from, category_id=sched.category_id,
                    status="pending", memo=sched.memo,
                    posted_date=posted_date, transfer_id=transfer_iri,
                )
                self._insert_transfer_parent(
                    iri=transfer_iri,
                    from_account_id=from_id,
                    to_account_id=to_id,
                    rate=used_rate,
                    rate_source=used_rate_source,
                )
                inserted_id = source_id
            else:
                inserted_id = self.insert_transaction(
                    account_id=sched.account_id,
                    posted_date=posted_date,
                    amount=amount,
                    payee_id=sched.payee_id,
                    category_id=sched.category_id,
                    status="pending",
                    memo=sched.memo,
                    import_hash=None,
                    import_batch_id=None,
                )

            next_due = self.compute_next_due_date(
                sched.anchor_date, sched.cadence, sched.next_due_date,
            )
            archive = (
                sched.end_date is not None and next_due > sched.end_date
            )
            if archive:
                self._conn.execute(
                    "UPDATE scheduled_txn SET "
                    "  next_due_date = ?, archived_at = datetime('now') "
                    "WHERE id = ?",
                    (next_due, schedule_id),
                )
            else:
                self._conn.execute(
                    "UPDATE scheduled_txn SET next_due_date = ? WHERE id = ?",
                    (next_due, schedule_id),
                )
            self.commit()
            return inserted_id
        except Exception:
            self.rollback()
            raise

    def auto_post_due(self, through_date: str) -> AutoPostResult:
        """Launch-time sweep: post every ``auto_post=1`` active schedule
        whose ``next_due_date <= through_date``. Catches up multiple
        missed occurrences by looping until next_due_date moves past
        the cutoff. Returns an ``AutoPostResult`` carrying the materialised
        txn ids (source side for transfers, in post order) **and** the
        schedules that couldn't post (ADR-091).

        Each post is its own atomic transaction; one schedule's failure
        doesn't abort the others, and the sweep never refuses to launch
        over a single bad schedule. But — unlike before ADR-091 — a
        failure is no longer dropped on the floor: it's recorded in
        ``failures`` so the caller can tell the user. A schedule that's
        permanently broken (e.g. transfer-kind with no destination, per
        ADR-074) was otherwise invisible — it looked like nothing was due,
        launch after launch, while quietly never posting.
        """
        result = AutoPostResult()
        cur = self._conn.execute(
            "SELECT id FROM scheduled_txn "
            "WHERE archived_at IS NULL AND auto_post = 1 "
            "  AND next_due_date <= ? "
            "ORDER BY next_due_date ASC, id ASC",
            (through_date,),
        )
        # Snapshot the candidate ids — each post advances next_due_date
        # so the looping window can shift while we iterate the rowset.
        candidate_ids = [r["id"] for r in cur]
        for sid in candidate_ids:
            while True:
                # Re-read each iteration so the loop terminates correctly
                # when next_due_date moves past through_date or the
                # schedule was archived by hitting end_date.
                sched = self.get_scheduled_txn(sid)
                if sched is None or sched.archived_at is not None:
                    break
                if sched.next_due_date > through_date:
                    break
                try:
                    txn_id = self.post_scheduled_txn(sid)
                    result.posted.append(txn_id)
                except Exception as exc:
                    # Skip the rest of this schedule's catch-up — likely a
                    # variable bill or a config issue the user must resolve
                    # manually (e.g. a transfer schedule missing its
                    # destination). Record it so the caller can surface it
                    # instead of it silently never posting.
                    label = (
                        f"{sched.account_name} — {sched.category_name}"
                        f" ({sched.payee_name})"
                    )
                    result.failures.append(AutoPostFailure(
                        schedule_id=sid, label=label, reason=str(exc),
                    ))
                    break
        return result

    # ── Budgets (ADR-058) ──

    _BUDGET_COLS = (
        "id, iri, name, start_month, length_months, currency, funding_mode"
    )

    def _row_to_budget(self, row) -> Budget:
        return Budget(
            id=row["id"], iri=row["iri"], name=row["name"],
            start_month=row["start_month"],
            length_months=int(row["length_months"]),
            currency=row["currency"],
            funding_mode=row["funding_mode"] or "balances",
        )

    def list_budgets(self) -> list[Budget]:
        """All budgets in the file, newest first (ADR-058 — multi-budget)."""
        cur = self._conn.execute(
            f"SELECT {self._BUDGET_COLS} FROM budget ORDER BY id DESC"
        )
        return [self._row_to_budget(r) for r in cur]

    def get_budget(self, budget_id: int) -> Optional[Budget]:
        row = self._conn.execute(
            f"SELECT {self._BUDGET_COLS} FROM budget WHERE id = ?",
            (budget_id,),
        ).fetchone()
        return self._row_to_budget(row) if row is not None else None

    def create_budget(
        self,
        *,
        name: str,
        start_month: str,
        length_months: int = 12,
        currency: Optional[str] = None,
        funding_mode: str = "balances",
    ) -> Budget:
        """Create a new, empty budget (no perimeter, no lines). ``start_month``
        is 'YYYY-MM'. ``funding_mode`` (ADR-138) is ``'balances'`` or
        ``'income'``."""
        clean = (name or "").strip() or "Budget"
        if funding_mode not in ("balances", "income"):
            raise ValueError(f"Unknown funding_mode: {funding_mode!r}")
        iri = new_budget_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO budget (iri, name, start_month, length_months, "
                "currency, funding_mode) VALUES (?, ?, ?, ?, ?, ?)",
                (iri, clean, start_month, length_months, currency, funding_mode),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return self.get_budget(int(cur.lastrowid))  # type: ignore[return-value]

    def get_or_create_default_budget(self) -> Budget:
        """Return the first budget, creating a default current-year (Jan–Dec)
        one on first access so the screen is always openable (ADR-058)."""
        existing = self._conn.execute(
            f"SELECT {self._BUDGET_COLS} FROM budget ORDER BY id LIMIT 1"
        ).fetchone()
        if existing is not None:
            return self._row_to_budget(existing)
        # strftime gives the file's idea of 'this year'; matches migration 0019.
        row = self._conn.execute(
            "SELECT strftime('%Y-01', 'now') AS m"
        ).fetchone()
        return self.create_budget(name="My Budget", start_month=row["m"])

    def duplicate_budget(self, budget_id: int, new_name: str) -> Budget:
        """Copy a budget — perimeter, lines, and per-month allocations — into a
        new budget (ADR-058 §scenarios). One transaction; the copy is fully
        independent of the source."""
        src = self.get_budget(budget_id)
        if src is None:
            raise ValueError(f"No budget with id {budget_id}.")
        clean = (new_name or "").strip() or f"{src.name} (copy)"
        iri = new_budget_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO budget (iri, name, start_month, length_months, "
                "currency, funding_mode) VALUES (?, ?, ?, ?, ?, ?)",
                (iri, clean, src.start_month, src.length_months, src.currency,
                 src.funding_mode),
            )
            new_id = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO budget_account "
                "(budget_id, account_id, contribution) "
                "SELECT ?, account_id, contribution "
                "FROM budget_account WHERE budget_id = ?",
                (new_id, budget_id),
            )
            # Lines + allocations: copy each line, then its allocations onto the
            # freshly-minted line id (matched back via category_id, which is
            # unique per budget).
            self._conn.execute(
                "INSERT INTO budget_line "
                "(budget_id, category_id, role, rollover, sort_order) "
                "SELECT ?, category_id, role, rollover, sort_order "
                "FROM budget_line WHERE budget_id = ?",
                (new_id, budget_id),
            )
            self._conn.execute(
                "INSERT INTO budget_allocation (budget_line_id, month, amount) "
                "SELECT nl.id, a.month, a.amount "
                "FROM budget_allocation a "
                "JOIN budget_line ol ON ol.id = a.budget_line_id "
                "JOIN budget_line nl ON nl.budget_id = ? "
                "                   AND nl.category_id = ol.category_id "
                "WHERE ol.budget_id = ?",
                (new_id, budget_id),
            )
            # Goals (R4b/R4c): copy each goal onto the new budget with a fresh
            # IRI, then its account links — baseline / share / start_date are
            # preserved so the scenario starts where the source did.
            for g in self.list_budget_goals(budget_id):
                gcur = self._conn.execute(
                    "INSERT INTO budget_goal (iri, budget_id, name, kind, "
                    "currency, target_amount, target_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_goal_iri(), new_id, g.name, g.kind, g.currency,
                     decimal_to_pence(g.target_amount), g.target_date),
                )
                ngid = int(gcur.lastrowid)
                for link in g.accounts:
                    self._conn.execute(
                        "INSERT INTO budget_goal_account (goal_id, account_id, "
                        "share_bp, baseline_balance, start_date) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (ngid, link.account_id, link.share_bp,
                         decimal_to_pence(link.baseline_balance),
                         link.start_date),
                    )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return self.get_budget(new_id)  # type: ignore[return-value]

    def delete_budget(self, budget_id: int) -> None:
        """Delete a budget; perimeter, lines, and allocations cascade."""
        try:
            self._conn.execute("DELETE FROM budget WHERE id = ?", (budget_id,))
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── savings / pay-down goals (ADR-058 R4b/R4c) ──

    def list_budget_goals(self, budget_id: int) -> list[BudgetGoal]:
        """All goals in a budget, oldest first, each with its account links."""
        goal_rows = self._conn.execute(
            "SELECT id, iri, budget_id, name, kind, currency, target_amount, "
            "target_date FROM budget_goal WHERE budget_id = ? ORDER BY id",
            (budget_id,),
        ).fetchall()
        if not goal_rows:
            return []
        links: dict[int, list[GoalAccountLink]] = {}
        for r in self._conn.execute(
            "SELECT ga.goal_id, ga.account_id, ga.share_bp, ga.baseline_balance, "
            "       ga.start_date "
            "FROM budget_goal_account ga "
            "JOIN budget_goal g ON g.id = ga.goal_id "
            "WHERE g.budget_id = ? ORDER BY ga.id",
            (budget_id,),
        ):
            links.setdefault(int(r["goal_id"]), []).append(GoalAccountLink(
                account_id=int(r["account_id"]), share_bp=int(r["share_bp"]),
                baseline_balance=pence_to_decimal(int(r["baseline_balance"])),
                start_date=r["start_date"],
            ))
        return [
            BudgetGoal(
                id=int(r["id"]), iri=r["iri"], budget_id=int(r["budget_id"]),
                name=r["name"], kind=r["kind"], currency=r["currency"],
                target_amount=pence_to_decimal(int(r["target_amount"])),
                target_date=r["target_date"],
                accounts=tuple(links.get(int(r["id"]), [])),
            )
            for r in goal_rows
        ]

    def add_budget_goal(
        self,
        *,
        budget_id: int,
        name: str,
        kind: str,
        currency: str,
        target_amount: Decimal,
        target_date: str,
        accounts: list[tuple[int, int]],   # (account_id, share_bp)
        today: str,
    ) -> int:
        """Create a goal + its account links (ADR-058 R4c). ``accounts`` is
        ``(account_id, share_bp)`` pairs (share_bp = basis points, 10000 = 100%);
        each link captures the account's *current* native market value (cash +
        holdings, ADR-044) as its baseline so progress is measured from
        creation."""
        if not accounts:
            raise ValueError("A goal needs at least one account.")
        balances = self.compute_account_values()   # market value (cash + holdings)
        try:
            cur = self._conn.execute(
                "INSERT INTO budget_goal (iri, budget_id, name, kind, currency, "
                "target_amount, target_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_goal_iri(), budget_id, (name or "").strip(), kind, currency,
                 decimal_to_pence(target_amount), target_date),
            )
            goal_id = int(cur.lastrowid)
            for account_id, share_bp in accounts:
                baseline = balances.get(account_id, Decimal("0.00"))
                self._conn.execute(
                    "INSERT INTO budget_goal_account (goal_id, account_id, "
                    "share_bp, baseline_balance, start_date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (goal_id, account_id, int(share_bp),
                     decimal_to_pence(baseline), today),
                )
            self.commit()
            return goal_id
        except Exception:
            self.rollback()
            raise

    def update_budget_goal(
        self,
        goal_id: int,
        *,
        name: Optional[str] = None,
        target_amount: Optional[Decimal] = None,
        target_date: Optional[str] = None,
        currency: Optional[str] = None,
        accounts: Optional[list[tuple[int, int]]] = None,
        today: Optional[str] = None,
    ) -> None:
        """Edit a goal's meta and/or reconcile its account links (ADR-058 R4c).
        For ``accounts`` (``(account_id, share_bp)`` pairs): a kept account keeps
        its captured baseline (only ``share_bp`` updates); a newly-added account
        captures its baseline *now* (``today`` required); a removed account's link
        is deleted. A kept account's baseline is immutable — re-baselining means
        removing then re-adding it."""
        try:
            sets: list[str] = []
            params: list = []
            if name is not None:
                sets.append("name = ?")
                params.append(name.strip())
            if target_amount is not None:
                sets.append("target_amount = ?")
                params.append(decimal_to_pence(target_amount))
            if target_date is not None:
                sets.append("target_date = ?")
                params.append(target_date)
            if currency is not None:
                sets.append("currency = ?")
                params.append(currency)
            if sets:
                params.append(goal_id)
                self._conn.execute(
                    f"UPDATE budget_goal SET {', '.join(sets)} WHERE id = ?",
                    params,
                )
            if accounts is not None:
                if not accounts:
                    raise ValueError("A goal needs at least one account.")
                if today is None:
                    raise ValueError("today is required to add goal accounts.")
                existing = {
                    int(r["account_id"]): int(r["share_bp"])
                    for r in self._conn.execute(
                        "SELECT account_id, share_bp FROM budget_goal_account "
                        "WHERE goal_id = ?", (goal_id,),
                    )
                }
                wanted = {int(aid): int(bp) for aid, bp in accounts}
                for aid in set(existing) - set(wanted):       # removed
                    self._conn.execute(
                        "DELETE FROM budget_goal_account WHERE goal_id = ? "
                        "AND account_id = ?", (goal_id, aid),
                    )
                balances = None
                for aid, bp in wanted.items():
                    if aid in existing:                       # kept — share only
                        if existing[aid] != bp:
                            self._conn.execute(
                                "UPDATE budget_goal_account SET share_bp = ? "
                                "WHERE goal_id = ? AND account_id = ?",
                                (bp, goal_id, aid),
                            )
                    else:                                     # added — baseline now
                        if balances is None:
                            balances = self.compute_account_values()
                        baseline = balances.get(aid, Decimal("0.00"))
                        self._conn.execute(
                            "INSERT INTO budget_goal_account (goal_id, "
                            "account_id, share_bp, baseline_balance, start_date) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (goal_id, aid, bp, decimal_to_pence(baseline), today),
                        )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def compute_goal_aggregates(
        self, budget_id: int, *, on_date: str,
    ) -> dict[int, GoalAggregate]:
        """Roll each goal's account links up into the goal's currency (ADR-058
        R4c): ``start`` = Σ(baseline_balance × share), ``current`` =
        Σ(current_value × share), each converted from the account's currency to
        the goal currency via the FX layer (ADR-055 — an account with no rate is
        excluded + named, never par-added). Both bookends omit an excluded
        account so they stay consistent.

        ``current`` uses **market value** (``compute_account_values`` — cash +
        Σ(open-lot shares × latest price), ADR-044), NOT just cash: a 401(k) or
        brokerage with no cash but six-figure holdings must count its holdings
        toward a savings goal."""
        goals = self.list_budget_goals(budget_id)
        if not goals:
            return {}
        values = self.compute_account_values()   # market value (cash + holdings)
        accounts = {a.id: a for a in self.list_accounts()}
        cents = Decimal("0.01")
        out: dict[int, GoalAggregate] = {}
        for g in goals:
            start = Decimal("0.00")
            current = Decimal("0.00")
            excluded: list[str] = []
            for link in g.accounts:
                acc = accounts.get(link.account_id)
                if acc is None:
                    continue
                share = Decimal(link.share_bp) / Decimal(10000)
                base_c, _ = self.convert_amount(
                    link.baseline_balance * share,
                    from_ccy=acc.currency, to_ccy=g.currency, on_date=on_date,
                )
                cur_c, _ = self.convert_amount(
                    values.get(link.account_id, Decimal("0.00")) * share,
                    from_ccy=acc.currency, to_ccy=g.currency, on_date=on_date,
                )
                if base_c is None or cur_c is None:
                    excluded.append(acc.name)
                    continue
                start += base_c
                current += cur_c
            out[g.id] = GoalAggregate(
                start=start.quantize(cents, rounding=ROUND_HALF_UP),
                current=current.quantize(cents, rounding=ROUND_HALF_UP),
                excluded=tuple(excluded),
            )
        return out

    def account_inflows_by_month(
        self, account_id: int, start_month: str, end_month: str,
    ) -> dict[str, Decimal]:
        """Sum of positive (inflow) transaction amounts on an account per
        ``'YYYY-MM'`` month, inclusive of the bounds. Used for a goal's
        *actual paid* (R4b): payments onto a card / deposits into a savings
        account land as inflows, while purchases (outflows) are excluded."""
        cur = self._conn.execute(
            "SELECT substr(posted_date, 1, 7) AS m, SUM(amount) AS s "
            "FROM txn WHERE account_id = ? AND amount > 0 "
            "AND substr(posted_date, 1, 7) BETWEEN ? AND ? "
            "GROUP BY m",
            (account_id, start_month, end_month),
        )
        return {r["m"]: pence_to_decimal(int(r["s"])) for r in cur}

    def delete_budget_goal(self, goal_id: int) -> None:
        try:
            self._conn.execute(
                "DELETE FROM budget_goal WHERE id = ?", (goal_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def rename_budget(self, budget_id: int, new_name: str) -> None:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Budget name cannot be empty.")
        try:
            self._conn.execute(
                "UPDATE budget SET name = ? WHERE id = ?",
                (clean, budget_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_budget_period(
        self, budget_id: int, *, start_month: str, length_months: int,
    ) -> None:
        """Set a budget's period (ADR-058). Existing allocations outside the
        new window are left in place (harmless — the matrix only reads months
        in range); months newly in range start empty (= 0)."""
        if length_months < 1:
            raise ValueError("A budget must span at least one month.")
        try:
            self._conn.execute(
                "UPDATE budget SET start_month = ?, length_months = ? "
                "WHERE id = ?",
                (start_month, length_months, budget_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_budget_funding_mode(self, budget_id: int, mode: str) -> None:
        """Set how a budget seeds its available pool (ADR-138): ``'balances'``
        (the perimeter accounts' balances) or ``'income'`` (income into those
        accounts over the budget period)."""
        if mode not in ("balances", "income"):
            raise ValueError(f"Unknown funding_mode: {mode!r}")
        try:
            self._conn.execute(
                "UPDATE budget SET funding_mode = ? WHERE id = ?",
                (mode, budget_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_budget_currency(
        self, budget_id: int, currency: Optional[str],
    ) -> None:
        try:
            self._conn.execute(
                "UPDATE budget SET currency = ? WHERE id = ?",
                (currency, budget_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Budget perimeter (which accounts count) ──

    def list_budget_account_ids(self, budget_id: int) -> list[int]:
        """The set of accounts inside this budget's perimeter, in account-
        display order (family, name) so the dialog renders consistently
        with the sidebar."""
        cur = self._conn.execute(
            "SELECT a.id FROM budget_account ba "
            "JOIN account a ON a.id = ba.account_id "
            "WHERE ba.budget_id = ? AND a.archived_at IS NULL "
            "ORDER BY a.family, a.name",
            (budget_id,),
        )
        return [int(r["id"]) for r in cur]

    def list_budget_account_contributions(
        self, budget_id: int,
    ) -> dict[int, str]:
        """``{account_id: contribution}`` for the budget's perimeter (ADR-058
        R4a). Contribution is ``balance`` (counts toward the pool) or
        ``excluded`` (in the perimeter for actuals only). ``available_credit``
        was dropped in ADR-138 — a card contributes its signed balance."""
        cur = self._conn.execute(
            "SELECT account_id, contribution FROM budget_account "
            "WHERE budget_id = ?",
            (budget_id,),
        )
        return {int(r["account_id"]): r["contribution"] for r in cur}

    def set_budget_accounts(
        self, budget_id: int, accounts: list[tuple[int, str]],
    ) -> None:
        """Replace the budget's perimeter with ``(account_id, contribution)``
        pairs (ADR-058 R4a — the pool-contribution mode rides with membership).
        Atomic — the old perimeter is dropped and the new one inserted in one
        SQL transaction; failure leaves the previous perimeter intact."""
        seen: dict[int, str] = {}
        for aid, mode in accounts:               # preserve order, dedupe
            if aid not in seen:
                if mode not in ("balance", "excluded"):   # ADR-138: no available_credit
                    raise ValueError(f"Invalid contribution {mode!r}.")
                seen[aid] = mode
        try:
            self._conn.execute(
                "DELETE FROM budget_account WHERE budget_id = ?",
                (budget_id,),
            )
            if seen:
                self._conn.executemany(
                    "INSERT INTO budget_account "
                    "(budget_id, account_id, contribution) VALUES (?, ?, ?)",
                    [(budget_id, aid, mode) for aid, mode in seen.items()],
                )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Budget lines (envelopes) + per-month allocations (ADR-058) ──

    _BUDGET_LINE_COLS = (
        "bl.id, bl.budget_id, bl.category_id, "
        "c.name AS category_name, "
        "COALESCE(p.name, '') AS category_parent_name, "
        "c.kind AS category_kind, "
        "bl.role, bl.rollover, bl.sort_order, bl.scheduled_txn_id"
    )

    def _row_to_budget_line(self, row) -> BudgetLine:
        return BudgetLine(
            id=row["id"], budget_id=row["budget_id"],
            category_id=row["category_id"],
            category_name=row["category_name"],
            category_parent_name=row["category_parent_name"],
            category_kind=row["category_kind"],
            role=row["role"], rollover=row["rollover"],
            sort_order=int(row["sort_order"]),
            scheduled_txn_id=row["scheduled_txn_id"],
        )

    def list_budget_lines(self, budget_id: int) -> list[BudgetLine]:
        """All envelope lines in a budget, ordered for the matrix: by kind
        (income → expense → transfer), then sort_order, then name."""
        cur = self._conn.execute(
            f"SELECT {self._BUDGET_LINE_COLS} "
            f"FROM budget_line bl "
            f"JOIN      category c ON c.id = bl.category_id "
            f"LEFT JOIN category p ON p.id = c.parent_id "
            f"WHERE bl.budget_id = ? "
            f"ORDER BY CASE c.kind WHEN 'income' THEN 0 "
            f"                     WHEN 'expense' THEN 1 ELSE 2 END, "
            f"         bl.sort_order, c.name",
            (budget_id,),
        )
        return [self._row_to_budget_line(r) for r in cur]

    def add_budget_line(
        self,
        *,
        budget_id: int,
        category_id: int,
        role: str = "discretionary",
        rollover: Optional[str] = None,
        scheduled_txn_id: Optional[int] = None,
    ) -> int:
        """Add an envelope for ``category_id``. ``rollover`` defaults to
        'accumulate' for expense categories, 'none' otherwise (ADR-058 D3).
        ``scheduled_txn_id`` (ADR-094) marks the line as a bill backed by that
        schedule. Idempotent on UNIQUE(budget_id, category_id) — updates
        role/rollover (and the schedule link when supplied) if the line already
        exists. Returns the line id."""
        if role not in BUDGET_ROLES:
            raise ValueError(f"Invalid role {role!r}; expected {BUDGET_ROLES}.")
        if rollover is None:
            kind_row = self._conn.execute(
                "SELECT kind FROM category WHERE id = ?", (category_id,),
            ).fetchone()
            rollover = (
                "accumulate"
                if kind_row is not None and kind_row["kind"] == "expense"
                else "none"
            )
        if rollover not in BUDGET_ROLLOVER:
            raise ValueError(
                f"Invalid rollover {rollover!r}; expected {BUDGET_ROLLOVER}."
            )
        try:
            self._conn.execute(
                "INSERT INTO budget_line "
                "(budget_id, category_id, role, rollover, scheduled_txn_id) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(budget_id, category_id) DO UPDATE SET "
                "  role = excluded.role, rollover = excluded.rollover, "
                "  scheduled_txn_id = COALESCE(excluded.scheduled_txn_id, "
                "                              budget_line.scheduled_txn_id)",
                (budget_id, category_id, role, rollover, scheduled_txn_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        row = self._conn.execute(
            "SELECT id FROM budget_line WHERE budget_id = ? AND category_id = ?",
            (budget_id, category_id),
        ).fetchone()
        return int(row["id"])

    def set_budget_line_schedule(
        self, line_id: int, scheduled_txn_id: Optional[int],
    ) -> None:
        """Link (or, with None, unlink) a budget line to a scheduled_txn — i.e.
        mark it as a bill or demote it back to a plain envelope (ADR-094)."""
        try:
            self._conn.execute(
                "UPDATE budget_line SET scheduled_txn_id = ? WHERE id = ?",
                (scheduled_txn_id, line_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def list_bill_schedules_for_budget(self, budget_id: int) -> list[dict]:
        """The schedule recurrence behind each bill line in a budget (ADR-094) —
        ``{category_id, cadence, anchor_date, amount, end_date}`` for every line
        with a live (non-archived) linked schedule. ``amount`` is the schedule's
        signed estimated amount. Feeds ``bill_occurrences_in_month`` so the
        burn-down can project + amount-match the bills."""
        cur = self._conn.execute(
            "SELECT bl.category_id, s.cadence, s.anchor_date, "
            "       s.estimated_amount, s.end_date "
            "FROM budget_line bl "
            "JOIN scheduled_txn s ON s.id = bl.scheduled_txn_id "
            "WHERE bl.budget_id = ? AND s.archived_at IS NULL",
            (budget_id,),
        )
        return [
            {
                "category_id": r["category_id"],
                "cadence": r["cadence"],
                "anchor_date": r["anchor_date"],
                "amount": pence_to_decimal(r["estimated_amount"]),
                "end_date": r["end_date"],
            }
            for r in cur
        ]

    def list_schedules_not_in_budget(self, budget_id: int) -> list["ScheduledTxnRow"]:
        """Active expense/transfer schedules whose category isn't yet an
        envelope in this budget (ADR-094) — the candidates for the setup-time
        "add scheduled transactions" picker. Income schedules are excluded
        (bills are outflows)."""
        budgeted = {
            r["category_id"] for r in self._conn.execute(
                "SELECT category_id FROM budget_line WHERE budget_id = ?",
                (budget_id,),
            )
        }
        return [
            s for s in self.list_scheduled_txns(include_archived=False)
            if s.category_kind in ("expense", "transfer")
            and s.category_id not in budgeted
        ]

    def add_bill_line_from_schedule(
        self, *, budget_id: int, schedule_id: int, seed_allocations: bool = True,
    ) -> int:
        """Create (or update) a budget line for a schedule's category and link it
        as a bill (ADR-094). When ``seed_allocations`` is set, each month's
        allocation is seeded from the schedule's expected occurrences that month
        (a monthly bill → its amount; a weekly bill → ~4–5×), so the envelope's
        plan matches the bill out of the box. Returns the line id."""
        sched = self.get_scheduled_txn(schedule_id)
        if sched is None:
            raise ValueError(f"No schedule with id {schedule_id}.")
        role = "bills" if sched.category_kind == "expense" else "discretionary"
        line_id = self.add_budget_line(
            budget_id=budget_id, category_id=sched.category_id,
            role=role, scheduled_txn_id=schedule_id,
        )
        if seed_allocations:
            # Lazy import: budget_calc imports from this module (circular at top).
            from mfl_desktop.budget_calc import (
                BillSchedule, bill_occurrences_in_month,
            )
            budget = self.get_budget(budget_id)
            if budget is not None:
                bs = BillSchedule(
                    category_id=sched.category_id, cadence=sched.cadence,
                    anchor_date=sched.anchor_date,
                    amount=abs(sched.estimated_amount), end_date=sched.end_date,
                )
                for month in budget.months():
                    occ = bill_occurrences_in_month([bs], month)
                    total = sum((o.amount for o in occ), Decimal("0.00"))
                    if total > 0:
                        self.set_line_allocation(
                            line_id, month, total, scope="one",
                        )
        return line_id

    def update_budget_line(
        self,
        line_id: int,
        *,
        role: Optional[str] = None,
        rollover: Optional[str] = None,
    ) -> None:
        """Update an envelope's role and/or rollover policy."""
        sets: list[str] = []
        params: list = []
        if role is not None:
            if role not in BUDGET_ROLES:
                raise ValueError(f"Invalid role {role!r}.")
            sets.append("role = ?")
            params.append(role)
        if rollover is not None:
            if rollover not in BUDGET_ROLLOVER:
                raise ValueError(f"Invalid rollover {rollover!r}.")
            sets.append("rollover = ?")
            params.append(rollover)
        if not sets:
            return
        params.append(line_id)
        try:
            self._conn.execute(
                f"UPDATE budget_line SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def delete_budget_line(self, line_id: int) -> None:
        """Delete an envelope; its allocations cascade."""
        try:
            self._conn.execute(
                "DELETE FROM budget_line WHERE id = ?", (line_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def list_budget_allocations(
        self, budget_id: int,
    ) -> dict[tuple[int, str], Decimal]:
        """All allocations for a budget, keyed by ``(budget_line_id, month)``.
        Absent cells (no row) are simply not present → the caller treats them
        as 0; storage stays sparse."""
        cur = self._conn.execute(
            "SELECT a.budget_line_id, a.month, a.amount "
            "FROM budget_allocation a "
            "JOIN budget_line bl ON bl.id = a.budget_line_id "
            "WHERE bl.budget_id = ?",
            (budget_id,),
        )
        return {
            (int(r["budget_line_id"]), r["month"]): pence_to_decimal(
                int(r["amount"])
            )
            for r in cur
        }

    def set_line_allocation(
        self,
        line_id: int,
        month: str,
        amount: Decimal,
        *,
        scope: str = "one",
    ) -> None:
        """Set a line's budgeted amount for ``month``, with copy-forward
        (ADR-058 D1). ``scope``:

        - ``'one'``     — just this month;
        - ``'forward'`` — this month and every later month in the budget;
        - ``'all'``     — every month in the budget.

        All target months are written in **one transaction** so a partial
        stamp can't leave the matrix half-propagated."""
        if amount < 0:
            raise ValueError("Budget amount cannot be negative.")
        line = self._conn.execute(
            "SELECT budget_id FROM budget_line WHERE id = ?", (line_id,),
        ).fetchone()
        if line is None:
            raise ValueError(f"No budget line with id {line_id}.")
        budget = self.get_budget(int(line["budget_id"]))
        assert budget is not None
        months = budget.months()
        if scope == "one":
            targets = [month]
        elif scope == "forward":
            targets = [m for m in months if m >= month]
        elif scope == "all":
            targets = list(months)
        else:
            raise ValueError(f"Invalid scope {scope!r}.")
        # A copy-forward from a month outside the budget window still writes at
        # least the named month.
        if month not in targets:
            targets.append(month)
        pence = decimal_to_pence(amount)
        try:
            self._conn.executemany(
                "INSERT INTO budget_allocation (budget_line_id, month, amount) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(budget_line_id, month) DO UPDATE SET "
                "  amount = excluded.amount",
                [(line_id, m, pence) for m in targets],
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def historical_monthly_average(
        self,
        *,
        budget_id: int,
        category_id: int,
        months: int = 12,
        as_of: str,
    ) -> Decimal:
        """Suggested monthly allocation for a category (ADR-058 D6): the
        average monthly magnitude of perimeter activity on the category and
        its descendants over the trailing ``months`` ending at ``as_of``.

        Reads the split-unrolled view so split lines count; uses the budget's
        perimeter accounts so the suggestion reflects the accounts in scope.
        Returns a positive Decimal (0 if no history)."""
        if months < 1:
            months = 1
        sub_ids = self.category_descendants(category_id) | {category_id}
        end = date.fromisoformat(as_of)
        # Window start = first day of the month (months-1) before as_of's month.
        y, m = end.year, end.month
        back = months - 1
        m -= back
        while m < 1:
            m += 12
            y -= 1
        start = date(y, m, 1).isoformat()
        placeholders = ",".join("?" for _ in sub_ids)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(ABS(t.amount)), 0) AS total "
            f"FROM txn_category_line t "
            f"JOIN budget_account ba ON ba.account_id = t.account_id "
            f"WHERE ba.budget_id = ? "
            f"  AND t.category_id IN ({placeholders}) "
            f"  AND t.posted_date BETWEEN ? AND ?",
            (budget_id, *sub_ids, start, as_of),
        ).fetchone()
        total = pence_to_decimal(int(row["total"]))
        return (total / Decimal(months)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )

    # ── Budget computation source data ──

    def compute_perimeter_cash_on_hand(
        self, budget_id: int,
    ) -> Decimal:
        """Sum of current balances across the budget's in-perimeter
        accounts. Reality-check number for the header badge; not part of
        the planned-vs-actual tile math.

        Naive cross-currency sum — same caveat as `compute_account_balances`
        (ADR-015): mixed-currency perimeters are simply summed pence-wise.
        Acceptable until multi-currency budgets become a concern."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM("
            "  a.opening_balance + COALESCE("
            "    (SELECT SUM(t.amount) FROM txn t WHERE t.account_id = a.id), 0"
            "  )"
            "), 0) AS total_pence "
            "FROM account a "
            "JOIN budget_account ba ON ba.account_id = a.id "
            "WHERE ba.budget_id = ? AND a.archived_at IS NULL",
            (budget_id,),
        ).fetchone()
        return pence_to_decimal(int(row["total_pence"]))

    def compute_perimeter_pool(
        self,
        budget_id: int,
        *,
        display_ccy: str,
        on_date: str,
    ) -> tuple[Decimal, list[str]]:
        """The budget's available pool (ADR-058 D2 / R4a / ADR-138), converted
        to ``display_ccy`` via the FX layer (ADR-055 — no naive par-add).

        The budget's **funding mode** (ADR-138) sets the basis:

        - ``'balances'`` (default) — the sum of each perimeter account's
          ``contribution``: ``'balance'`` counts its **signed** balance (so a
          credit card's debt *reduces* the pool — the former
          ``'available_credit'``/limit basis was dropped as bad practice),
          ``'excluded'`` counts nothing (still in the perimeter for actuals).
        - ``'income'`` — only income (``kind='income'`` transactions) into the
          non-excluded perimeter accounts over the **budget period** (its
          start_month through its last month, incl. future-dated income), i.e.
          the new money to assign, ignoring any starting balances.

        Returns ``(pool, excluded_names)`` where ``excluded_names`` lists
        accounts whose currency has no FX rate to ``display_ccy`` (a banner); a
        deliberately ``'excluded'`` account is silent."""
        budget = self.get_budget(budget_id)
        pool = Decimal("0.00")
        excluded: list[str] = []

        if budget is not None and budget.funding_mode == "income":
            months = budget.months()
            start, end = f"{months[0]}-01", f"{months[-1]}-31"
            cur = self._conn.execute(
                "SELECT a.id, a.name, a.currency, "
                "  COALESCE(SUM(t.amount), 0) AS in_pence "
                "FROM txn t "
                "JOIN account a  ON a.id = t.account_id "
                "JOIN budget_account ba ON ba.account_id = a.id "
                "     AND ba.budget_id = ? "
                "JOIN category c ON c.id = t.category_id "
                "WHERE a.archived_at IS NULL AND ba.contribution != 'excluded' "
                "  AND c.kind = 'income' "
                "  AND t.posted_date BETWEEN ? AND ? "
                "GROUP BY a.id, a.name, a.currency",
                (budget_id, start, end),
            )
        else:  # 'balances' (default)
            cur = self._conn.execute(
                "SELECT a.id, a.name, a.currency, "
                "  a.opening_balance + COALESCE("
                "    (SELECT SUM(t.amount) FROM txn t WHERE t.account_id = a.id), 0"
                "  ) AS in_pence "
                "FROM account a "
                "JOIN budget_account ba ON ba.account_id = a.id "
                "WHERE ba.budget_id = ? AND a.archived_at IS NULL "
                "  AND ba.contribution != 'excluded' "
                "ORDER BY a.family, a.name",
                (budget_id,),
            )

        for r in cur:
            native = pence_to_decimal(int(r["in_pence"] or 0))
            if native == 0:
                continue
            converted, _fallback = self.convert_amount(
                native, from_ccy=r["currency"], to_ccy=display_ccy,
                on_date=on_date,
            )
            if converted is None:
                excluded.append(f"{r['name']} (no rate to {display_ccy})")
            else:
                pool += converted
        return pool, excluded

    def list_perimeter_txns(
        self,
        budget_id: int,
        period_start: str,
        period_end: str,
    ) -> list[PerimeterTxn]:
        """All transactions inside the budget's perimeter window, filtered
        by the intra-perimeter-transfer cancellation rule:

        - non-transfer rows on perimeter accounts → included;
        - transfer rows where the *partner* account is also in perimeter
          → excluded (both halves cancel inside the perimeter);
        - transfer rows where the partner account is OUTSIDE the perimeter
          → included (this half is real cross-perimeter flow).

        The bucket-by-budgeted-ancestor mapping is left to the computation
        module (`mfl_desktop/budget_calc.py`) — this method is pure data.
        """
        # SQLite parameter substitution doesn't take a tuple for IN clauses;
        # we splice the budget_id into a CTE and let the engine resolve
        # everything. Cheaper than two round-trips when the perimeter has
        # 20+ accounts.
        # Read from the split-unrolled view (ADR-051): a split parent yields one
        # row per category line (line category + line amount), so budget actuals
        # bucket each line under its own budgeted ancestor. A non-split txn maps
        # to itself. A split *line* may now be a transfer (ADR-051 amendment):
        # the view exposes the LINE's own transfer_id, so the cancellation branch
        # below handles it just like a parent-level transfer — an in-perimeter
        # split-line transfer cancels (its partner txn t2 sits in the perimeter),
        # an out-of-perimeter one counts as real cross-perimeter flow. The `t2`
        # subquery stays on base `txn` because the partner is always a real txn
        # row (the source side lives on the split parent, id != t.txn_id).
        sql = (
            "WITH peri AS ("
            "  SELECT account_id FROM budget_account WHERE budget_id = ?"
            ") "
            "SELECT t.txn_id AS id, t.account_id, t.posted_date, t.amount, "
            "       t.category_id "
            "FROM txn_category_line t "
            "WHERE t.account_id IN (SELECT account_id FROM peri) "
            "  AND t.posted_date BETWEEN ? AND ? "
            "  AND ("
            "    t.transfer_id IS NULL "
            "    OR NOT EXISTS ("
            "      SELECT 1 FROM txn t2 "
            "      WHERE t2.transfer_id = t.transfer_id "
            "        AND t2.id != t.txn_id "
            "        AND t2.account_id IN (SELECT account_id FROM peri)"
            "    )"
            "  ) "
            "ORDER BY t.posted_date, t.txn_id"
        )
        cur = self._conn.execute(
            sql, (budget_id, period_start, period_end),
        )
        return [
            PerimeterTxn(
                id=int(r["id"]),
                account_id=int(r["account_id"]),
                posted_date=r["posted_date"],
                amount=pence_to_decimal(int(r["amount"])),
                category_id=int(r["category_id"]),
            )
            for r in cur
        ]

    def distinct_category_ids_for_account(
        self, account_id: Optional[int],
    ) -> set[int]:
        """Category ids actually referenced by txns in a single account
        (when ``account_id`` is set) or across the whole ledger (when
        ``None``). Used by the register's category-filter combo to hide
        options that wouldn't match anything in the current view.

        Reads the split-unrolled view (ADR-051) so a category used only on a
        split line is still offered — the register filter then surfaces the
        '—Split—' parent via the proxy's split-aware match."""
        if account_id is None:
            cur = self._conn.execute(
                "SELECT DISTINCT category_id FROM txn_category_line"
            )
        else:
            cur = self._conn.execute(
                "SELECT DISTINCT category_id FROM txn_category_line "
                "WHERE account_id = ?",
                (account_id,),
            )
        return {int(r["category_id"]) for r in cur}

    def category_parent_map(self) -> dict[int, Optional[int]]:
        """Mapping of category id → parent id, for the whole tree. Used
        by the budget computation to walk up to the nearest budgeted
        ancestor when bucketing actuals — cheaper to compute the chain
        in Python from one snapshot than to do a recursive CTE per
        transaction."""
        cur = self._conn.execute(
            "SELECT id, parent_id FROM category WHERE archived_at IS NULL"
        )
        return {int(r["id"]): r["parent_id"] for r in cur}

    def category_kind_map(self) -> dict[int, str]:
        """Mapping of category id → kind (income/expense/transfer) for the
        whole tree. Used by the budget matrix (ADR-058) to file each
        un-budgeted perimeter txn into the right section's Unbudgeted row."""
        cur = self._conn.execute(
            "SELECT id, kind FROM category WHERE archived_at IS NULL"
        )
        return {int(r["id"]): r["kind"] for r in cur}

    def category_usage_counts(self) -> dict[int, int]:
        """Mapping of category id → number of transactions using it (across the
        whole ledger, reading the split-unrolled view so split lines count).
        Used by the budget setup picker so the user can see, at a glance, which
        categories actually carry activity (ADR-058)."""
        cur = self._conn.execute(
            "SELECT category_id, COUNT(*) AS n FROM txn_category_line "
            "GROUP BY category_id"
        )
        return {int(r["category_id"]): int(r["n"]) for r in cur}

    def category_rollup_usage_counts(self) -> dict[int, int]:
        """Like ``category_usage_counts`` but each category's count includes its
        whole subtree (direct + all descendants). Used by the budget setup
        chooser (ADR-058) so a top-level group reflects its children's activity
        — a parent with all its spend on leaves no longer reads '0 txns'."""
        direct = self.category_usage_counts()
        parent_map = self.category_parent_map()
        out: dict[int, int] = {cid: 0 for cid in parent_map}
        for cid, cnt in direct.items():
            out[cid] = out.get(cid, 0) + cnt
            parent = parent_map.get(cid)
            seen: set[int] = set()
            while parent is not None and parent not in seen:
                out[parent] = out.get(parent, 0) + cnt
                seen.add(parent)
                parent = parent_map.get(parent)
        return out

    def top_level_categories_with_activity(
        self,
        account_ids: list[int],
        *,
        months: int = 12,
        as_of: str,
    ) -> set[int]:
        """Top-level categories (``parent_id IS NULL``) whose subtree has any
        activity in the given accounts over the trailing ``months`` (ADR-058
        prepopulation). Returns the set of top-level ancestor ids — pre-ticked
        in the setup chooser so a fresh budget starts from the user's real
        spending shape. Accounts are passed directly (not via budget_id) so the
        suggestion reflects the perimeter the user is *currently* choosing,
        before Save."""
        if not account_ids:
            return set()
        if months < 1:
            months = 1
        end = date.fromisoformat(as_of)
        y, m = end.year, end.month - (months - 1)
        while m < 1:
            m += 12
            y -= 1
        start = date(y, m, 1).isoformat()
        acc_ph = ",".join("?" for _ in account_ids)
        cur = self._conn.execute(
            f"SELECT DISTINCT category_id FROM txn_category_line "
            f"WHERE account_id IN ({acc_ph}) "
            f"  AND posted_date BETWEEN ? AND ?",
            (*account_ids, start, as_of),
        )
        active = {int(r["category_id"]) for r in cur}
        parent_map = self.category_parent_map()
        tops: set[int] = set()
        for cid in active:
            cur_id: Optional[int] = cid
            seen: set[int] = set()
            while cur_id is not None and cur_id not in seen:
                seen.add(cur_id)
                parent = parent_map.get(cur_id)
                if parent is None:
                    tops.add(cur_id)
                    break
                cur_id = parent
        return tops

    def list_perimeter_schedules_due_through(
        self,
        budget_id: int,
        through_date: str,
    ) -> list[ScheduledTxnRow]:
        """Schedules due on or before ``through_date`` whose source account
        is inside the budget's perimeter. Used by the budget screen to
        surface "still-to-post" planned outflows that haven't been
        materialised yet (auto-post off, or user hasn't launched today)."""
        cur = self._conn.execute(
            f"SELECT {self._SCHEDULED_COLS} "
            f"FROM scheduled_txn s "
            f"JOIN      account  a  ON a.id = s.account_id "
            f"LEFT JOIN payee    p  ON p.id = s.payee_id "
            f"JOIN      category c  ON c.id = s.category_id "
            f"LEFT JOIN account  ta ON ta.id = s.transfer_to_account_id "
            f"JOIN budget_account ba ON ba.account_id = s.account_id "
            f"WHERE s.archived_at IS NULL "
            f"  AND s.next_due_date <= ? "
            f"  AND ba.budget_id = ? "
            f"ORDER BY s.next_due_date ASC, s.id ASC",
            (through_date, budget_id),
        )
        return [self._row_to_scheduled(r) for r in cur]

    # ── Saved reports (ADR-039) ──

    _REPORT_COLS = (
        "r.id, r.iri, r.name, r.type, r.folder_id, "
        "r.filters_json, r.created_at, f.name AS folder_name"
    )

    def _row_to_report(self, row) -> ReportRow:
        return ReportRow(
            id=int(row["id"]),
            iri=row["iri"],
            name=row["name"],
            type=row["type"],
            folder_id=row["folder_id"],
            folder_name=row["folder_name"],
            filters_json=row["filters_json"] or "{}",
            created_at=row["created_at"],
        )

    def list_report_folders(self) -> list[ReportFolderRow]:
        """All non-archived report folders in sidebar order (sort_order,
        then name as a stable tiebreaker). ``report_count`` is the number
        of non-archived reports currently inside the folder."""
        cur = self._conn.execute(
            "SELECT f.id, f.iri, f.name, f.sort_order, "
            "       (SELECT COUNT(*) FROM report r "
            "        WHERE r.folder_id = f.id AND r.archived_at IS NULL) AS n "
            "FROM report_folder f "
            "WHERE f.archived_at IS NULL "
            "ORDER BY f.sort_order, f.name"
        )
        return [
            ReportFolderRow(
                id=int(r["id"]), iri=r["iri"], name=r["name"],
                sort_order=int(r["sort_order"]), report_count=int(r["n"]),
            )
            for r in cur
        ]

    def list_reports(self) -> list[ReportRow]:
        """All non-archived saved reports, joined with their folder name.
        Sorted by folder (root first, then folders in their sort_order)
        then report name — matches the sidebar's natural render order."""
        cur = self._conn.execute(
            f"SELECT {self._REPORT_COLS} "
            f"FROM report r "
            f"LEFT JOIN report_folder f ON f.id = r.folder_id "
            f"WHERE r.archived_at IS NULL "
            f"ORDER BY "
            f"  CASE WHEN r.folder_id IS NULL THEN 0 ELSE 1 END, "
            f"  f.sort_order, "
            f"  r.name COLLATE NOCASE"
        )
        return [self._row_to_report(r) for r in cur]

    def get_report(self, report_id: int) -> Optional[ReportRow]:
        row = self._conn.execute(
            f"SELECT {self._REPORT_COLS} "
            f"FROM report r "
            f"LEFT JOIN report_folder f ON f.id = r.folder_id "
            f"WHERE r.id = ? AND r.archived_at IS NULL",
            (report_id,),
        ).fetchone()
        return self._row_to_report(row) if row is not None else None

    def create_report(
        self,
        *,
        name: str,
        type_key: str,
        folder_id: Optional[int],
        filters_json: str,
    ) -> ReportRow:
        """Insert a new saved report. Raises ``ValueError`` on a blank
        name or a duplicate (name, folder_id) — the UNIQUE constraint in
        0010_reports.sql enforces the latter."""
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Report name cannot be empty.")
        iri = new_report_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO report (iri, name, type, folder_id, filters_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (iri, clean, type_key, folder_id, filters_json or "{}"),
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            self.rollback()
            raise ValueError(
                f"Could not create report {clean!r}: {e}"
            ) from e
        except Exception:
            self.rollback()
            raise
        row = self.get_report(int(cur.lastrowid))
        assert row is not None  # we just inserted it
        return row

    def update_report(
        self,
        report_id: int,
        *,
        name=_UNSET,
        folder_id=_UNSET,
        filters_json=_UNSET,
    ) -> ReportRow:
        """Update one or more fields on a saved report. Unspecified fields
        keep their current value (the sentinel distinguishes "leave alone"
        from "set to None" on the optional folder_id).

        References the class-level ``_UNSET`` sentinel via ``self._UNSET``
        — Python class scope is not visible to nested function bodies, so
        a bare ``_UNSET`` inside the method body would resolve to a
        different (or missing) name and silently treat every default as
        "set to None" (see the bulk_update_transactions pattern above)."""
        sets: list[str] = []
        params: list = []
        if name is not self._UNSET:
            clean = (name or "").strip()
            if not clean:
                raise ValueError("Report name cannot be empty.")
            sets.append("name = ?")
            params.append(clean)
        if folder_id is not self._UNSET:
            sets.append("folder_id = ?")
            params.append(folder_id)
        if filters_json is not self._UNSET:
            sets.append("filters_json = ?")
            params.append(filters_json or "{}")
        if not sets:
            row = self.get_report(report_id)
            if row is None:
                raise ValueError(f"No report with id {report_id}.")
            return row
        params.append(report_id)
        try:
            self._conn.execute(
                f"UPDATE report SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            self.rollback()
            raise ValueError(f"Could not update report: {e}") from e
        except Exception:
            self.rollback()
            raise
        row = self.get_report(report_id)
        if row is None:
            raise ValueError(f"No report with id {report_id}.")
        return row

    def delete_report(self, report_id: int) -> bool:
        """Hard-delete a saved report. Returns True if a row was removed,
        False if the id was already gone."""
        try:
            cur = self._conn.execute(
                "DELETE FROM report WHERE id = ?", (report_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return cur.rowcount > 0

    # ── bank feeds (ADR-077) ──

    _FEED_COLS = (
        "id, account_id, provider, external_account_id, requisition_id, "
        "institution_id, institution_name, status, last_synced_at"
    )

    def _row_to_feed(self, r) -> FeedAccount:
        return FeedAccount(
            id=r["id"], account_id=r["account_id"], provider=r["provider"],
            external_account_id=r["external_account_id"],
            requisition_id=r["requisition_id"],
            institution_id=r["institution_id"],
            institution_name=r["institution_name"],
            status=r["status"], last_synced_at=r["last_synced_at"],
        )

    def list_feed_accounts(self) -> list[FeedAccount]:
        cur = self._conn.execute(
            f"SELECT {self._FEED_COLS} FROM feed_account ORDER BY id"
        )
        return [self._row_to_feed(r) for r in cur]

    def get_feed_account(self, account_id: int) -> Optional[FeedAccount]:
        r = self._conn.execute(
            f"SELECT {self._FEED_COLS} FROM feed_account WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return self._row_to_feed(r) if r is not None else None

    def link_feed_account(
        self, *, account_id: int, provider: str, external_account_id: str,
        requisition_id: Optional[str] = None, institution_id: Optional[str] = None,
        institution_name: Optional[str] = None,
    ) -> FeedAccount:
        """Link (or re-link) an MFL account to a provider account. Upserts on
        the account (one feed per account, v1). Commits."""
        try:
            self._conn.execute(
                "INSERT INTO feed_account "
                "(account_id, provider, external_account_id, requisition_id, "
                " institution_id, institution_name, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'linked') "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "  provider=excluded.provider, "
                "  external_account_id=excluded.external_account_id, "
                "  requisition_id=excluded.requisition_id, "
                "  institution_id=excluded.institution_id, "
                "  institution_name=excluded.institution_name, "
                "  status='linked'",
                (account_id, provider, external_account_id, requisition_id,
                 institution_id, institution_name),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        feed = self.get_feed_account(account_id)
        assert feed is not None
        return feed

    def unlink_feed_account(self, account_id: int) -> None:
        try:
            self._conn.execute(
                "DELETE FROM feed_account WHERE account_id = ?", (account_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def mark_feed_synced(self, account_id: int) -> None:
        """Stamp last_synced_at = now and clear any error/expired status."""
        try:
            self._conn.execute(
                "UPDATE feed_account "
                "SET last_synced_at = datetime('now'), status = 'linked' "
                "WHERE account_id = ?",
                (account_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_feed_status(self, account_id: int, status: str) -> None:
        if status not in ("linked", "expired", "error"):
            raise ValueError(f"Invalid feed status {status!r}")
        try:
            self._conn.execute(
                "UPDATE feed_account SET status = ? WHERE account_id = ?",
                (status, account_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── saved CSV mapping profiles (ADR-021 follow-up) ──

    def get_csv_mapping(self, signature: str) -> Optional[str]:
        """The saved column-mapping JSON for a CSV header signature, or None.
        Touches last_used_at on a hit."""
        row = self._conn.execute(
            "SELECT mapping_json FROM csv_import_mapping WHERE signature = ?",
            (signature,),
        ).fetchone()
        if row is None:
            return None
        try:
            self._conn.execute(
                "UPDATE csv_import_mapping SET last_used_at = datetime('now') "
                "WHERE signature = ?",
                (signature,),
            )
            self.commit()
        except Exception:
            self.rollback()
        return row["mapping_json"]

    def save_csv_mapping(self, signature: str, mapping_json: str) -> None:
        """Persist (or update) the column mapping for a CSV header signature."""
        try:
            self._conn.execute(
                "INSERT INTO csv_import_mapping (signature, mapping_json) "
                "VALUES (?, ?) "
                "ON CONFLICT(signature) DO UPDATE SET "
                "  mapping_json = excluded.mapping_json, "
                "  last_used_at = datetime('now')",
                (signature, mapping_json),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def create_report_folder(self, name: str) -> ReportFolderRow:
        """Create a Reports-section folder. Appended at the end of the
        existing folder list (sort_order = current max + 1), matching the
        account-folder pattern."""
        clean = (name or "").strip()
        if not clean:
            raise ValueError("Folder name cannot be empty.")
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM report_folder "
            "WHERE archived_at IS NULL"
        ).fetchone()
        next_order = int(row["m"]) + 1
        iri = new_report_folder_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO report_folder (iri, name, sort_order) "
                "VALUES (?, ?, ?)",
                (iri, clean, next_order),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return ReportFolderRow(
            id=int(cur.lastrowid), iri=iri, name=clean,
            sort_order=next_order, report_count=0,
        )

    def rename_report_folder(self, folder_id: int, new_name: str) -> None:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("Folder name cannot be empty.")
        try:
            self._conn.execute(
                "UPDATE report_folder SET name = ? WHERE id = ?",
                (clean, folder_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def delete_report_folder(self, folder_id: int) -> None:
        """Delete a Reports-section folder. Reports inside it fall to the
        Reports root via the FK rule (ON DELETE SET NULL) — no report data
        is lost. Mirrors :meth:`delete_folder`."""
        try:
            self._conn.execute(
                "DELETE FROM report_folder WHERE id = ?", (folder_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def set_report_folder(
        self, report_id: int, folder_id: Optional[int],
    ) -> None:
        """Move a report into a folder (``folder_id`` set) or out to the
        Reports root (``folder_id=None``)."""
        try:
            self._conn.execute(
                "UPDATE report SET folder_id = ? WHERE id = ?",
                (folder_id, report_id),
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            self.rollback()
            raise ValueError(
                f"Could not move report: name collides in the target folder ({e})"
            ) from e
        except Exception:
            self.rollback()
            raise

    def move_report_folder(self, folder_id: int, direction: int) -> None:
        """Swap this folder's sort_order with its immediate neighbour
        (direction = -1 up / +1 down). No-op if there is no neighbour."""
        if direction not in (-1, 1):
            raise ValueError(f"Invalid move direction: {direction}")
        row = self._conn.execute(
            "SELECT sort_order FROM report_folder WHERE id = ?",
            (folder_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No report folder with id {folder_id}")
        current = int(row["sort_order"])
        if direction == -1:
            neighbour = self._conn.execute(
                "SELECT id, sort_order FROM report_folder "
                "WHERE sort_order < ? AND archived_at IS NULL "
                "ORDER BY sort_order DESC LIMIT 1",
                (current,),
            ).fetchone()
        else:
            neighbour = self._conn.execute(
                "SELECT id, sort_order FROM report_folder "
                "WHERE sort_order > ? AND archived_at IS NULL "
                "ORDER BY sort_order ASC LIMIT 1",
                (current,),
            ).fetchone()
        if neighbour is None:
            return
        try:
            sentinel = -1
            self._conn.execute(
                "UPDATE report_folder SET sort_order = ? WHERE id = ?",
                (sentinel, folder_id),
            )
            self._conn.execute(
                "UPDATE report_folder SET sort_order = ? WHERE id = ?",
                (current, int(neighbour["id"])),
            )
            self._conn.execute(
                "UPDATE report_folder SET sort_order = ? WHERE id = ?",
                (int(neighbour["sort_order"]), folder_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Statement reconciliation (ADR-040) ──

    # Shared SELECT prefix: each statement row carries its live txn_count and
    # residual_pence, computed from the statement_txn join so they reflect the
    # CURRENT state of the linked rows (this is what makes "out of balance"
    # detection free — an edited/deleted reconciled row changes residual_pence
    # without any per-edit bookkeeping). Callers append a WHERE/ORDER clause.
    _STATEMENT_SELECT = (
        "SELECT s.id, s.iri, s.account_id, s.start_date, s.end_date, "
        "       s.starting_balance_pence, s.ending_balance_pence, "
        "       s.status, s.closing_variance_pence, s.notes, "
        "       s.created_at, s.reconciled_at, "
        "       (SELECT COUNT(*) FROM statement_txn st "
        "        WHERE st.statement_id = s.id) AS txn_count, "
        "       (s.ending_balance_pence - s.starting_balance_pence) "
        "       - COALESCE((SELECT SUM(t.amount) FROM statement_txn st "
        "                   JOIN txn t ON t.id = st.txn_id "
        "                   WHERE st.statement_id = s.id), 0) "
        "       AS residual_pence "
        "FROM statement s "
    )

    def _row_to_statement(self, row) -> StatementRow:
        return StatementRow(
            id=int(row["id"]), iri=row["iri"],
            account_id=int(row["account_id"]),
            start_date=row["start_date"], end_date=row["end_date"],
            starting_balance=pence_to_decimal(row["starting_balance_pence"]),
            ending_balance=pence_to_decimal(row["ending_balance_pence"]),
            status=row["status"],
            closing_variance=pence_to_decimal(row["closing_variance_pence"]),
            notes=row["notes"],
            created_at=row["created_at"],
            reconciled_at=row["reconciled_at"],
            txn_count=int(row["txn_count"]),
            residual=pence_to_decimal(row["residual_pence"]),
        )

    def get_statement(self, statement_id: int) -> Optional[StatementRow]:
        row = self._conn.execute(
            self._STATEMENT_SELECT + "WHERE s.id = ?", (statement_id,),
        ).fetchone()
        return self._row_to_statement(row) if row is not None else None

    def get_open_statement(self, account_id: int) -> Optional[StatementRow]:
        """The account's single open (in-progress) statement, if any."""
        row = self._conn.execute(
            self._STATEMENT_SELECT
            + "WHERE s.account_id = ? AND s.status = 'open' "
            + "ORDER BY s.id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        return self._row_to_statement(row) if row is not None else None

    def list_statements_for_account(
        self, account_id: int,
    ) -> list[StatementRow]:
        """All statements for an account, newest closing date first — the
        history list."""
        cur = self._conn.execute(
            self._STATEMENT_SELECT
            + "WHERE s.account_id = ? "
            + "ORDER BY s.end_date DESC, s.id DESC",
            (account_id,),
        )
        return [self._row_to_statement(r) for r in cur]

    def get_last_statement_ending(self, account_id: int) -> Optional[Decimal]:
        """Ending balance of the most recent *reconciled* statement, used to
        auto-fill the next statement's starting balance. None if the account
        has never been reconciled (the dialog then falls back to the current
        recorded balance, or 0)."""
        row = self._conn.execute(
            "SELECT ending_balance_pence FROM statement "
            "WHERE account_id = ? AND status = 'reconciled' "
            "ORDER BY end_date DESC, id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        return pence_to_decimal(row["ending_balance_pence"]) if row else None

    def list_reconcilable_txns(
        self, account_id: int, *, include_statement_id: Optional[int] = None,
        include_cleared: bool = False,
    ) -> list[TransactionRow]:
        """Transactions eligible to appear on a reconciliation (ADR-130).

        Eligibility follows the confidence ladder: **matched** rows (a download
        confirmed them) are always eligible; **cleared** rows (seen at the bank
        by eye but not download-confirmed) are eligible only when
        ``include_cleared`` — for institutions that offer no download.
        **pending** rows are never eligible. This is what stops a not-yet-at-the-
        bank or duplicate row being ticked onto a statement by accident.

        Rows already ticked into ``include_statement_id`` (an open pass being
        resumed, or a closed statement being viewed) are **always** included
        regardless of status, so their ticks aren't lost. Any date is eligible
        (old stragglers can still be caught — ADR-040).

        The reported ``posted_date`` is the **bank posting date** where a
        download recorded one (``COALESCE(bank_posted_date, posted_date)``,
        ADR-130) so reconciliation ranges and displays against the statement's
        dates rather than the user's spend date. ``running_balance`` is 0."""
        sid = include_statement_id if include_statement_id is not None else -1
        cur = self._conn.execute(
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       COALESCE(t.bank_posted_date, t.posted_date) AS posted_date, "
            "       t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "WHERE t.account_id = ? "
            "  AND ( t.status = 'matched' "
            "        OR (t.status = 'cleared' AND ?) "
            "        OR t.id IN (SELECT txn_id FROM statement_txn "
            "                    WHERE statement_id = ?) ) "
            "ORDER BY COALESCE(t.bank_posted_date, t.posted_date) ASC, t.id ASC",
            (account_id, 1 if include_cleared else 0, sid),
        )
        return [
            TransactionRow(
                id=r["id"], iri=r["iri"],
                account_id=r["account_id"], account_name=r["account_name"],
                posted_date=r["posted_date"],
                amount=pence_to_decimal(r["amount"]),
                payee_id=r["payee_id"], payee_name=r["payee_name"],
                category_id=r["category_id"], category_name=r["category_name"],
                status=r["status"], memo=r["memo"],
                running_balance=Decimal("0.00"),
                transfer_id=r["transfer_id"],
            )
            for r in cur
        ]

    def count_cleared_in_period(
        self, account_id: int, date_from: str, date_to: str,
    ) -> int:
        """How many ``cleared`` rows (seen at the bank, not download-confirmed)
        fall in the statement period — surfaced as a reconcile warning when
        cleared rows are excluded from the candidate set (ADR-130)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM txn "
            "WHERE account_id = ? AND status = 'cleared' "
            "  AND posted_date BETWEEN ? AND ?",
            (account_id, date_from, date_to),
        ).fetchone()
        return int(row[0]) if row else 0

    def get_statement_tick_ids(self, statement_id: int) -> set[int]:
        """The set of txn ids currently ticked into a statement (open or
        closed) — used to pre-check rows when resuming or viewing."""
        cur = self._conn.execute(
            "SELECT txn_id FROM statement_txn WHERE statement_id = ?",
            (statement_id,),
        )
        return {int(r["txn_id"]) for r in cur}

    def _statement_residual_pence(self, statement_id: int) -> int:
        row = self._conn.execute(
            "SELECT (s.ending_balance_pence - s.starting_balance_pence) "
            "       - COALESCE((SELECT SUM(t.amount) FROM statement_txn st "
            "                   JOIN txn t ON t.id = st.txn_id "
            "                   WHERE st.statement_id = s.id), 0) "
            "       AS residual_pence "
            "FROM statement s WHERE s.id = ?",
            (statement_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No statement with id {statement_id}.")
        return int(row["residual_pence"])

    def statement_residual(self, statement_id: int) -> Decimal:
        """The live "Missing" figure: (ending − starting) − net of the ticked
        rows. Zero means the statement balances."""
        return pence_to_decimal(self._statement_residual_pence(statement_id))

    def create_statement(
        self, *,
        account_id: int,
        start_date: str,
        end_date: str,
        starting_balance: Decimal,
        ending_balance: Decimal,
    ) -> StatementRow:
        """Create a new open statement. Raises ``ValueError`` if the date
        range is inverted, if the account already has an open statement (only
        one in-progress reconciliation per account), or if a statement already
        exists on ``end_date`` (UNIQUE(account_id, end_date))."""
        if end_date < start_date:
            raise ValueError("Statement end date is before its start date.")
        existing = self.get_open_statement(account_id)
        if existing is not None:
            raise ValueError(
                "This account already has an open statement "
                f"(ending {existing.end_date}). Finish or delete it first."
            )
        iri = new_statement_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO statement "
                "(iri, account_id, start_date, end_date, "
                " starting_balance_pence, ending_balance_pence, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open')",
                (iri, account_id, start_date, end_date,
                 decimal_to_pence(starting_balance),
                 decimal_to_pence(ending_balance)),
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            self.rollback()
            raise ValueError(
                f"Could not create statement: a statement ending {end_date} "
                f"already exists on this account ({e})"
            ) from e
        except Exception:
            self.rollback()
            raise
        row = self.get_statement(int(cur.lastrowid))
        assert row is not None  # we just inserted it
        return row

    def update_statement(
        self,
        statement_id: int,
        *,
        start_date=_UNSET,
        end_date=_UNSET,
        starting_balance=_UNSET,
        ending_balance=_UNSET,
    ) -> StatementRow:
        """Edit a statement's dates / balances (the history Edit… verb).
        Allowed on open and reconciled statements — editing a closed
        statement's balances simply re-derives its residual (which may flip
        it to / from out-of-balance). Uses ``self._UNSET`` (see
        :meth:`update_report` for why the sentinel must be ``self``-qualified
        inside the body)."""
        sets: list[str] = []
        params: list = []
        if start_date is not self._UNSET:
            sets.append("start_date = ?")
            params.append(start_date)
        if end_date is not self._UNSET:
            sets.append("end_date = ?")
            params.append(end_date)
        if starting_balance is not self._UNSET:
            sets.append("starting_balance_pence = ?")
            params.append(decimal_to_pence(starting_balance))
        if ending_balance is not self._UNSET:
            sets.append("ending_balance_pence = ?")
            params.append(decimal_to_pence(ending_balance))
        if not sets:
            row = self.get_statement(statement_id)
            if row is None:
                raise ValueError(f"No statement with id {statement_id}.")
            return row
        params.append(statement_id)
        try:
            self._conn.execute(
                f"UPDATE statement SET {', '.join(sets)} WHERE id = ?", params,
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            self.rollback()
            raise ValueError(f"Could not update statement: {e}") from e
        except Exception:
            self.rollback()
            raise
        row = self.get_statement(statement_id)
        if row is None:
            raise ValueError(f"No statement with id {statement_id}.")
        return row

    def set_statement_ticks(
        self, statement_id: int, txn_ids: list[int],
    ) -> None:
        """Replace the ticked-row set for an open statement. Idempotent —
        called on every "Save & finish later". Raises if the statement is
        already closed."""
        stmt = self.get_statement(statement_id)
        if stmt is None:
            raise ValueError(f"No statement with id {statement_id}.")
        if stmt.status != "open":
            raise ValueError("Cannot change ticks on a closed statement.")
        try:
            self._conn.execute(
                "DELETE FROM statement_txn WHERE statement_id = ?",
                (statement_id,),
            )
            self._conn.executemany(
                "INSERT INTO statement_txn (statement_id, txn_id) "
                "VALUES (?, ?)",
                [(statement_id, tid) for tid in txn_ids],
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def close_statement(
        self,
        statement_id: int,
        *,
        ticked_ids: Optional[list[int]] = None,
        notes: Optional[str] = None,
    ) -> StatementRow:
        """Close (reconcile) a statement in one atomic step: persist the final
        ticked set (if ``ticked_ids`` is given), stamp every ticked row
        ``status='reconciled'`` + ``statement_id``, snapshot the residual into
        ``closing_variance_pence``, and set ``status='reconciled'`` +
        ``reconciled_at``.

        Closing is allowed even with a non-zero residual (ADR-040 amendment):
        the statement just shows as out of balance on the history list. Raises
        if the statement is already closed."""
        stmt = self.get_statement(statement_id)
        if stmt is None:
            raise ValueError(f"No statement with id {statement_id}.")
        if stmt.status != "open":
            raise ValueError("Statement is already closed.")
        try:
            if ticked_ids is not None:
                self._conn.execute(
                    "DELETE FROM statement_txn WHERE statement_id = ?",
                    (statement_id,),
                )
                self._conn.executemany(
                    "INSERT INTO statement_txn (statement_id, txn_id) "
                    "VALUES (?, ?)",
                    [(statement_id, tid) for tid in ticked_ids],
                )
            # Stamp the linked rows Reconciled and point them at this statement.
            self._conn.execute(
                "UPDATE txn SET status = 'reconciled', statement_id = ? "
                "WHERE id IN (SELECT txn_id FROM statement_txn "
                "             WHERE statement_id = ?)",
                (statement_id, statement_id),
            )
            residual_pence = self._statement_residual_pence(statement_id)
            self._conn.execute(
                "UPDATE statement SET status = 'reconciled', "
                "  closing_variance_pence = ?, notes = ?, "
                "  reconciled_at = datetime('now') "
                "WHERE id = ?",
                (residual_pence, notes, statement_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        row = self.get_statement(statement_id)
        assert row is not None
        return row

    def reopen_statement(self, statement_id: int) -> StatementRow:
        """Reopen a closed statement for editing. Every linked row reverts to
        ``'matched'`` and its ``statement_id`` is cleared; the tick set
        (``statement_txn``) is kept so rows show pre-ticked on resume. The
        statement goes back to ``status='open'`` with ``closing_variance``
        reset and ``reconciled_at`` cleared.

        Note: rows that were pending/cleared at reconcile time come back as
        matched — accepted per ADR-040 (ADR-130 rename)."""
        stmt = self.get_statement(statement_id)
        if stmt is None:
            raise ValueError(f"No statement with id {statement_id}.")
        if stmt.status != "reconciled":
            raise ValueError("Statement is not closed.")
        try:
            self._conn.execute(
                "UPDATE txn SET status = 'matched', statement_id = NULL "
                "WHERE statement_id = ?",
                (statement_id,),
            )
            self._conn.execute(
                "UPDATE statement SET status = 'open', "
                "  closing_variance_pence = 0, reconciled_at = NULL "
                "WHERE id = ?",
                (statement_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        row = self.get_statement(statement_id)
        assert row is not None
        return row

    def delete_statement(self, statement_id: int) -> None:
        """Delete a statement (history Delete… verb). Any rows currently
        Reconciled to it revert to ``'matched'``; the statement and its tick
        rows are removed (``statement_txn`` cascades). No-op-safe if already
        gone."""
        try:
            self._conn.execute(
                "UPDATE txn SET status = 'matched', statement_id = NULL "
                "WHERE statement_id = ?",
                (statement_id,),
            )
            self._conn.execute(
                "DELETE FROM statement WHERE id = ?", (statement_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def cancel_open_statement(self, statement_id: int) -> None:
        """Discard an open statement (Cancel → Discard on a pass started this
        session). Touches no txn — an open statement's ticks never changed any
        row's status. No-op if the statement is already gone; raises if it is
        closed (use :meth:`delete_statement` for that)."""
        stmt = self.get_statement(statement_id)
        if stmt is None:
            return
        if stmt.status != "open":
            raise ValueError("Use delete_statement for a closed statement.")
        try:
            self._conn.execute(
                "DELETE FROM statement WHERE id = ?", (statement_id,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

    def is_reconciled(self, txn_id: int) -> bool:
        """True if the txn is reconciled **to an actual statement** — the gate
        for the "change anyway?" confirm on inline edits / split open.

        Requires a non-null ``statement_id``, not just ``status='reconciled'``:
        the confirm warns that an edit "may put that statement out of balance",
        which is meaningless for a row that carries the Reconciled *status* but
        no statement. Banktivity-migrated data arrives exactly like that
        (status preserved, no statement created — see Known pitfalls §8), so a
        status-only check spuriously blocked editing those rows and their
        splits (ADR-040 amendment, 2026-06-19)."""
        row = self._conn.execute(
            "SELECT 1 FROM txn WHERE id = ? AND status = 'reconciled' "
            "AND statement_id IS NOT NULL",
            (txn_id,),
        ).fetchone()
        return row is not None

    def get_statement_for_txn(self, txn_id: int) -> Optional[StatementRow]:
        """The statement a row is reconciled to (for the edit-warning copy),
        or None if it isn't linked to one."""
        row = self._conn.execute(
            "SELECT statement_id FROM txn WHERE id = ?", (txn_id,),
        ).fetchone()
        if row is None or row["statement_id"] is None:
            return None
        return self.get_statement(int(row["statement_id"]))
