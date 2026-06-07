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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from mfl_desktop.account_types import AccountTypeSpec, by_key
from mfl_desktop.db.money import decimal_to_pence, pence_to_decimal
from mfl_desktop.db.schema import bootstrap

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


@dataclass(frozen=True)
class Budget:
    """A budget plan. v1 keeps one row per file (see ADR-024); the schema
    supports more so multi-plan can land additively."""
    id: int
    iri: str
    name: str


@dataclass(frozen=True)
class BudgetCategoryRow:
    """One per-category line in a budget, joined with the category's name,
    parent name, and kind so the screen + dialog can render without a
    second pass through the category list. Amount is stored as a positive
    magnitude in pence; signing comes from ``category_kind`` at display time.
    """
    id: int
    budget_id: int
    category_id: int
    category_name: str
    category_parent_name: str   # '' if top-level
    category_kind: str
    amount: Decimal
    cadence: str
    role: str


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


@dataclass(frozen=True)
class LinkExisting:
    """Bulk-edit / reconcile decision: link source to an existing
    candidate row (ADR-036). The candidate's category is rewritten to
    ``category_id`` at link time — that's the whole point."""
    source_txn_id: int
    candidate_txn_id: int
    category_id: int
    rate: Optional[Decimal] = None
    rate_source: Optional[str] = None


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


def new_report_iri() -> str:
    return f"mfl:Report_{uuid.uuid4().hex[:8]}"


def new_report_folder_iri() -> str:
    return f"mfl:ReportFolder_{uuid.uuid4().hex[:8]}"


def new_statement_iri() -> str:
    return f"mfl:Statement_{uuid.uuid4().hex[:8]}"


# ── Repository ──────────────────────────────────────────────────────────────


class Repository:
    def __init__(self, db_path: Path | str) -> None:
        db_path = Path(db_path)
        bootstrap(db_path)
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    @property
    def db_path(self) -> Path:
        return self._db_path

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
        "opening_balance, folder_id"
    )

    def _row_to_account(self, row) -> AccountSummary:
        return AccountSummary(
            id=row["id"], iri=row["iri"], name=row["name"],
            type=row["type"], family=row["family"],
            currency=row["currency"], is_liability=bool(row["is_liability"]),
            opening_balance=pence_to_decimal(row["opening_balance"] or 0),
            folder_id=row["folder_id"],
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

    def list_accounts(self) -> list[AccountSummary]:
        """All non-archived accounts in display order (family, name)."""
        cur = self._conn.execute(
            f"SELECT {self._ACCOUNT_COLS} FROM account "
            "WHERE archived_at IS NULL "
            "ORDER BY family, name"
        )
        return [self._row_to_account(r) for r in cur]

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
    ) -> AccountSummary:
        """Create a new account. `type_key` is the short key from
        account_types.ACCOUNT_TYPES (e.g. 'cash'). Family, is_liability,
        and the IRI class name are derived from the type. Commits on success."""
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
                "(iri, name, type, family, currency, is_liability, opening_balance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    iri, clean_name, spec.storage, spec.family, clean_currency,
                    1 if spec.is_liability else 0,
                    decimal_to_pence(opening_balance),
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
    ) -> AccountSummary:
        """Edit an existing account's name, currency, and opening balance.
        Type / family / is_liability are intentionally not editable here —
        those change the meaning of stored amounts. Commits on success."""
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Account name cannot be empty.")
        clean_currency = (currency or "").strip().upper()
        if not clean_currency:
            raise ValueError("Currency cannot be empty.")
        try:
            self._conn.execute(
                "UPDATE account SET name = ?, currency = ?, opening_balance = ? "
                "WHERE id = ?",
                (
                    clean_name, clean_currency,
                    decimal_to_pence(opening_balance), account_id,
                ),
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

    def compute_account_balances(self) -> dict[int, Decimal]:
        """Per-account balance: opening_balance + sum of txn.amount.

        Returns a dict keyed by account_id. Investment / property accounts
        use the same opening + txns formula for now — once the valuations
        UX ships (backlog) those families switch to latest-valuation.
        """
        cur = self._conn.execute(
            "SELECT a.id, "
            "       a.opening_balance + COALESCE((SELECT SUM(t.amount) "
            "                                     FROM txn t "
            "                                     WHERE t.account_id = a.id), 0) "
            "       AS balance_pence "
            "FROM account a "
            "WHERE a.archived_at IS NULL"
        )
        return {int(r["id"]): pence_to_decimal(r["balance_pence"]) for r in cur}

    def delete_account(self, account_id: int) -> int:
        """Hard-delete an account and everything that references it
        (transactions, import batches, lots, valuations all cascade by FK).
        Returns the count of transactions that were cascaded.

        Schema also reserves `archived_at` for a future soft-delete UX
        (see ADR-011); this method is the destructive variant."""
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
                    "SELECT id, kind FROM category "
                    "WHERE name = ? AND parent_id IS NULL",
                    (name,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT id, kind FROM category "
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
                parent_id = row["id"]
                parent_kind = row["kind"]
        return parent_id

    # ── Category — management (used by the category dialog) ──

    def list_category_tree(self) -> list[CategoryNode]:
        """All non-archived categories as a flat list. The dialog reassembles
        the parent/child structure for display."""
        cur = self._conn.execute(
            "SELECT c.id, c.parent_id, c.name, c.source, c.kind, "
            "       (SELECT COUNT(*) FROM txn t WHERE t.category_id = c.id) AS n "
            "FROM category c "
            "WHERE c.archived_at IS NULL"
        )
        return [
            CategoryNode(
                id=r["id"], parent_id=r["parent_id"], name=r["name"],
                source=r["source"], kind=r["kind"], usage_count=int(r["n"]),
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
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM txn WHERE category_id = ?",
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
        placeholders = ",".join("?" * len(sources))
        try:
            cur = self._conn.execute(
                f"UPDATE txn SET category_id = ? "
                f"WHERE category_id IN ({placeholders})",
                (target_id, *sources),
            )
            moved = cur.rowcount
            self._conn.execute(
                f"DELETE FROM category WHERE id IN ({placeholders})",
                tuple(sources),
            )
            self.commit()
            return moved
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
        try:
            if txn_count > 0:
                self._conn.execute(
                    "UPDATE txn SET category_id = ? WHERE category_id = ?",
                    (UNCATEGORISED_ID, category_id),
                )
            self._conn.execute(
                "DELETE FROM category WHERE id = ?", (category_id,),
            )
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
            "SELECT p.id, p.name, p.canonical_id, "
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
            "  AND t.status != 'Reconciled' "
            "LIMIT 1",
            (account_id, decimal_to_pence(amount), d_minus, d_plus),
        ).fetchone()
        if row is None:
            return None
        return ManualMatch(
            id=row["id"], iri=row["iri"],
            posted_date=row["posted_date"], payee_raw=row["payee_name"],
        )

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
    ) -> int:
        iri = new_transaction_iri()
        cur = self._conn.execute(
            "INSERT INTO txn "
            "(iri, account_id, posted_date, amount, payee_id, category_id, "
            " status, memo, import_hash, import_batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iri, account_id, posted_date, decimal_to_pence(amount),
                payee_id, category_id, status, memo or None,
                import_hash, import_batch_id,
            ),
        )
        return cur.lastrowid

    def merge_into_manual_transaction(
        self,
        manual_id: int,
        import_hash: str,
        memo: Optional[str],
    ) -> None:
        """Add import_hash to an existing manual transaction.

        Memo is only filled in if the existing memo is empty; amount, date,
        category, payee, and status are the user's manually-entered values
        and are not touched.
        """
        self._conn.execute(
            "UPDATE txn SET import_hash = ? WHERE id = ?",
            (import_hash, manual_id),
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

    # ── Register (read + inline edits) ──

    def list_transactions_for_account(
        self, account_id: int,
    ) -> list[TransactionRow]:
        """All transactions for one account, chronologically, with running
        balance. Running balance is seeded from the account's opening_balance
        so the final value matches the account's true balance."""
        opening_row = self._conn.execute(
            "SELECT opening_balance FROM account WHERE id = ?", (account_id,),
        ).fetchone()
        running = pence_to_decimal(
            opening_row["opening_balance"] if opening_row else 0
        )
        cur = self._conn.execute(
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "WHERE t.account_id = ? "
            "ORDER BY t.posted_date ASC, t.id ASC",
            (account_id,),
        )
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
            ))
        return rows

    def list_all_transactions(self) -> list[TransactionRow]:
        """Every transaction across every account, chronologically.

        Running balance is not meaningful across accounts of different types
        and currencies (see project-all-transactions-view in memory) and is
        reported as 0; the UI hides the Balance column in this view.
        """
        cur = self._conn.execute(
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "ORDER BY t.posted_date ASC, t.id ASC"
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

    def spending_aggregates(
        self,
        *,
        date_from: str,
        date_to: str,
        granularity: str,
        account_ids: Optional[list[int]] = None,
        include_uncategorised: bool = True,
        payee_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Outflow spending per (bucket, category_id) over a date range.

        v1 uses a **strict outflow** definition: only transactions whose
        amount is negative on a `kind='expense'` category contribute. This
        keeps the chart's bars unambiguously positive and avoids the
        "Uncategorised has £20k of income misclassified as expense" trap
        (ADR-014 wrong-bucket risk, ADR-018 §spending semantics).

        Refund handling — where a positive amount on an expense category
        reduces the bucket — is deferred to the future Cash Flow report,
        which interprets signed amounts directly.

        ``payee_ids``, when supplied non-empty, narrows the result to
        transactions whose payee is in the set (ADR-039 saved-report
        filter dimension). ``None`` or an empty list means no payee
        narrowing — every payee contributes (including the (No payee)
        rows where ``txn.payee_id`` is NULL).

        Returns a list of dicts: ``{bucket, category_id, spending_pence}``
        — pence are always ≥ 0. Caller rolls category_id up to a "report
        group" id (see `mfl_desktop.reports.category_group_map`) and
        aggregates further.
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

        filter_sql = ""
        if filters:
            filter_sql = " AND " + " AND ".join(filters)

        sql = (
            f"SELECT {bucket_expr} AS bucket, "
            f"       t.category_id AS category_id, "
            f"       SUM(-t.amount) AS spending_pence "
            f"FROM txn t "
            f"JOIN category c ON c.id = t.category_id "
            f"WHERE t.posted_date BETWEEN ? AND ? "
            f"  AND c.kind = 'expense' "
            f"  AND t.amount < 0 "  # strict outflow — see docstring
            f"  {filter_sql} "
            f"GROUP BY bucket, t.category_id "
            f"ORDER BY bucket"
        )
        cur = self._conn.execute(sql, params)
        return [
            {
                "bucket": r["bucket"],
                "category_id": int(r["category_id"]),
                "spending_pence": int(r["spending_pence"]),
            }
            for r in cur
        ]

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
        if status not in ("Pending", "Uncleared", "Cleared", "Reconciled"):
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

    # Sentinel used by bulk_update_transactions to distinguish "don't change
    # this field" from "set this field to None/empty" — passing None for a
    # nullable column like memo means "clear it", so we can't use None as
    # the "leave alone" marker.
    _UNSET = object()

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
                if status not in ("Pending", "Uncleared", "Cleared", "Reconciled"):
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
        true count of rows that will be removed."""
        if not txn_ids:
            return []
        placeholders = ",".join("?" * len(txn_ids))
        cur = self._conn.execute(
            f"SELECT id FROM txn WHERE transfer_id IS NOT NULL AND "
            f"transfer_id IN ("
            f"  SELECT transfer_id FROM txn "
            f"  WHERE id IN ({placeholders}) AND transfer_id IS NOT NULL"
            f")",
            tuple(txn_ids),
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
        status: str = "Pending",
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
        if status not in ("Pending", "Uncleared", "Cleared", "Reconciled"):
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
                if status not in ("Pending", "Uncleared", "Cleared", "Reconciled"):
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

    def _link_transfer_unbatched(
        self,
        *,
        source_txn_id: int,
        candidate_txn_id: int,
        category_id: int,
        rate: Optional[Decimal] = None,
        rate_source: Optional[str] = None,
    ) -> str:
        """Internal helper. Does NOT commit; caller owns the transaction
        boundary. Returns the new transfer IRI."""
        src = self._conn.execute(
            "SELECT id, account_id, amount, transfer_id "
            "FROM txn WHERE id = ?",
            (source_txn_id,),
        ).fetchone()
        cand = self._conn.execute(
            "SELECT id, account_id, amount, transfer_id "
            "FROM txn WHERE id = ?",
            (candidate_txn_id,),
        ).fetchone()
        if src is None:
            raise ValueError(f"No source transaction with id {source_txn_id}")
        if cand is None:
            raise ValueError(
                f"No candidate transaction with id {candidate_txn_id}"
            )
        if src["transfer_id"] is not None:
            raise ValueError(
                f"Source {source_txn_id} is already part of a transfer."
            )
        if cand["transfer_id"] is not None:
            raise ValueError(
                f"Candidate {candidate_txn_id} is already part of a transfer."
            )
        if src["account_id"] == cand["account_id"]:
            raise ValueError(
                "Source and candidate must be on different accounts."
            )
        if (int(src["amount"]) < 0) == (int(cand["amount"]) < 0):
            raise ValueError(
                "Source and candidate amounts must have opposite signs "
                "to form a transfer pair."
            )
        if rate is not None and rate <= 0:
            raise ValueError("rate must be greater than zero.")

        src_ccy = self.get_account_currency(src["account_id"])
        cand_ccy = self.get_account_currency(cand["account_id"])
        src_magnitude = abs(pence_to_decimal(int(src["amount"])))
        cand_magnitude = abs(pence_to_decimal(int(cand["amount"])))

        # Determine from/to: outflow (amount<0) is the source side.
        if int(src["amount"]) < 0:
            from_id = src["account_id"]
            to_id = cand["account_id"]
            from_magnitude = src_magnitude
            to_magnitude = cand_magnitude
        else:
            from_id = cand["account_id"]
            to_id = src["account_id"]
            from_magnitude = cand_magnitude
            to_magnitude = src_magnitude

        if rate is None:
            if src_ccy == cand_ccy:
                resolved_rate = Decimal("1")
                resolved_source = rate_source or "derived"
            elif from_magnitude > 0:
                resolved_rate = to_magnitude / from_magnitude
                resolved_source = rate_source or "derived"
            else:
                resolved_rate = Decimal("1")
                resolved_source = rate_source or "derived"
        else:
            resolved_rate = rate
            resolved_source = rate_source or "manual"

        transfer_iri = new_transfer_iri()
        self._conn.execute(
            "UPDATE txn SET transfer_id = ?, category_id = ? WHERE id = ?",
            (transfer_iri, category_id, source_txn_id),
        )
        self._conn.execute(
            "UPDATE txn SET transfer_id = ?, category_id = ? WHERE id = ?",
            (transfer_iri, category_id, candidate_txn_id),
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

        a_rows = self._conn.execute(
            "SELECT t.id, t.posted_date, t.amount, "
            "       COALESCE(p.name, '') AS payee_name "
            "FROM txn t LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.account_id = ? AND t.transfer_id IS NULL "
            "ORDER BY t.posted_date",
            (account_a_id,),
        ).fetchall()
        b_rows = self._conn.execute(
            "SELECT t.id, t.posted_date, t.amount, "
            "       COALESCE(p.name, '') AS payee_name "
            "FROM txn t LEFT JOIN payee p ON p.id = t.payee_id "
            "WHERE t.account_id = ? AND t.transfer_id IS NULL "
            "ORDER BY t.posted_date",
            (account_b_id,),
        ).fetchall()

        currencies_match = a_acct.currency == b_acct.currency
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
                    source_txn_id=int(src_row["id"]),
                    source_account_id=src_acct.id,
                    source_amount=pence_to_decimal(int(src_row["amount"])),
                    source_currency=src_acct.currency,
                    source_posted_date=src_row["posted_date"],
                    source_payee=src_row["payee_name"],
                    target_txn_id=int(tgt_row["id"]),
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
                ))

        return greedy_pair(
            candidates,
            source_key=lambda p: p.source_txn_id,
            target_key=lambda p: p.target_txn_id,
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
                    status="Pending", memo=sched.memo,
                    posted_date=posted_date, transfer_id=transfer_iri,
                )
                self._insert_transfer_half(
                    account_id=to_id, amount=known_to_magnitude,
                    payee_id=payee_from, category_id=sched.category_id,
                    status="Pending", memo=sched.memo,
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
                    status="Pending",
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

    def auto_post_due(self, through_date: str) -> list[int]:
        """Launch-time sweep: post every ``auto_post=1`` active schedule
        whose ``next_due_date <= through_date``. Catches up multiple
        missed occurrences by looping until next_due_date moves past
        the cutoff. Returns the list of materialised txn ids (source
        side for transfers) in post order.

        Each post is its own atomic transaction; one schedule's failure
        doesn't abort the others. Failures are silently skipped here —
        the caller is the app startup path and shouldn't refuse to
        launch over a single bad schedule. (The dialog's manual Post
        Now flow surfaces errors per-action.)
        """
        posted: list[int] = []
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
                    posted.append(txn_id)
                except Exception:
                    # Skip the rest of this schedule's catch-up — likely
                    # a variable bill or a config issue the user needs to
                    # resolve manually.
                    break
        return posted

    # ── Budgets (ADR-024) ──

    def get_default_budget(self) -> Optional[Budget]:
        """Return the single v1 budget if it exists, or None.
        ``get_or_create_default_budget`` is the constructor side."""
        row = self._conn.execute(
            "SELECT id, iri, name FROM budget ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Budget(id=row["id"], iri=row["iri"], name=row["name"])

    def get_or_create_default_budget(self) -> Budget:
        """Return the file's single budget, creating an empty one on first
        access. Empty = no perimeter, no categories — the screen renders
        an explicit "Set up your budget" state in that case."""
        existing = self.get_default_budget()
        if existing is not None:
            return existing
        iri = new_budget_iri()
        try:
            cur = self._conn.execute(
                "INSERT INTO budget (iri, name) VALUES (?, 'My Budget')",
                (iri,),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return Budget(id=cur.lastrowid, iri=iri, name="My Budget")

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

    def set_budget_accounts(
        self, budget_id: int, account_ids: list[int],
    ) -> None:
        """Replace the budget's perimeter with the given set of account ids.
        Atomic — the old perimeter is dropped and the new one inserted in
        one SQL transaction; failure leaves the previous perimeter intact."""
        unique_ids = list(dict.fromkeys(account_ids))  # preserve order, dedupe
        try:
            self._conn.execute(
                "DELETE FROM budget_account WHERE budget_id = ?",
                (budget_id,),
            )
            if unique_ids:
                self._conn.executemany(
                    "INSERT INTO budget_account (budget_id, account_id) "
                    "VALUES (?, ?)",
                    [(budget_id, aid) for aid in unique_ids],
                )
            self.commit()
        except Exception:
            self.rollback()
            raise

    # ── Budget categories ──

    _BUDGET_CATEGORY_COLS = (
        "bc.id, bc.budget_id, bc.category_id, "
        "c.name AS category_name, "
        "COALESCE(p.name, '') AS category_parent_name, "
        "c.kind AS category_kind, "
        "bc.amount, bc.cadence, bc.role"
    )

    def _row_to_budget_category(self, row) -> BudgetCategoryRow:
        return BudgetCategoryRow(
            id=row["id"], budget_id=row["budget_id"],
            category_id=row["category_id"],
            category_name=row["category_name"],
            category_parent_name=row["category_parent_name"],
            category_kind=row["category_kind"],
            amount=pence_to_decimal(row["amount"]),
            cadence=row["cadence"], role=row["role"],
        )

    def list_budget_categories(
        self, budget_id: int,
    ) -> list[BudgetCategoryRow]:
        """All per-category budget rows for the given budget, sorted by
        role then category name. Used by both the screen and the setup
        dialog."""
        cur = self._conn.execute(
            f"SELECT {self._BUDGET_CATEGORY_COLS} "
            f"FROM budget_category bc "
            f"JOIN      category c ON c.id = bc.category_id "
            f"LEFT JOIN category p ON p.id = c.parent_id "
            f"WHERE bc.budget_id = ? "
            f"ORDER BY bc.role, c.name",
            (budget_id,),
        )
        return [self._row_to_budget_category(r) for r in cur]

    def upsert_budget_category(
        self,
        *,
        budget_id: int,
        category_id: int,
        amount: Decimal,
        cadence: str,
        role: str,
    ) -> int:
        """Insert or update one per-category budget row. ``UNIQUE(budget_id,
        category_id)`` makes the ON CONFLICT path the natural shape.
        Returns the row id (either freshly inserted or the existing one).
        """
        if cadence not in SCHEDULE_CADENCES:
            raise ValueError(
                f"Invalid cadence {cadence!r}; expected one of {SCHEDULE_CADENCES}."
            )
        if role not in BUDGET_ROLES:
            raise ValueError(
                f"Invalid role {role!r}; expected one of {BUDGET_ROLES}."
            )
        if amount < 0:
            raise ValueError("Budget amount cannot be negative.")
        try:
            self._conn.execute(
                "INSERT INTO budget_category "
                "(budget_id, category_id, amount, cadence, role) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(budget_id, category_id) DO UPDATE SET "
                "  amount = excluded.amount, "
                "  cadence = excluded.cadence, "
                "  role = excluded.role",
                (
                    budget_id, category_id, decimal_to_pence(amount),
                    cadence, role,
                ),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise
        row = self._conn.execute(
            "SELECT id FROM budget_category "
            "WHERE budget_id = ? AND category_id = ?",
            (budget_id, category_id),
        ).fetchone()
        return int(row["id"])

    def delete_budget_category(
        self, budget_id: int, category_id: int,
    ) -> None:
        try:
            self._conn.execute(
                "DELETE FROM budget_category "
                "WHERE budget_id = ? AND category_id = ?",
                (budget_id, category_id),
            )
            self.commit()
        except Exception:
            self.rollback()
            raise

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
        sql = (
            "WITH peri AS ("
            "  SELECT account_id FROM budget_account WHERE budget_id = ?"
            ") "
            "SELECT t.id, t.account_id, t.posted_date, t.amount, t.category_id "
            "FROM txn t "
            "WHERE t.account_id IN (SELECT account_id FROM peri) "
            "  AND t.posted_date BETWEEN ? AND ? "
            "  AND ("
            "    t.transfer_id IS NULL "
            "    OR NOT EXISTS ("
            "      SELECT 1 FROM txn t2 "
            "      WHERE t2.transfer_id = t.transfer_id "
            "        AND t2.id != t.id "
            "        AND t2.account_id IN (SELECT account_id FROM peri)"
            "    )"
            "  ) "
            "ORDER BY t.posted_date, t.id"
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
        options that wouldn't match anything in the current view."""
        if account_id is None:
            cur = self._conn.execute(
                "SELECT DISTINCT category_id FROM txn"
            )
        else:
            cur = self._conn.execute(
                "SELECT DISTINCT category_id FROM txn WHERE account_id = ?",
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
    ) -> list[TransactionRow]:
        """Transactions eligible to appear on a reconciliation: every row on
        the account not already Reconciled to a closed statement, regardless
        of date (so old stragglers can still be caught — ADR-040 amendment).
        Rows ticked into a still-open statement are included (their status is
        unchanged until close), so a resumed pass shows them.

        ``include_statement_id`` additionally pulls in rows already reconciled
        to *that* statement, so viewing / reopening a closed statement shows
        its own (Reconciled) rows rather than an empty list. Returned in
        statement order (date asc); ``running_balance`` is 0 (not meaningful
        here)."""
        sid = include_statement_id if include_statement_id is not None else -1
        cur = self._conn.execute(
            "SELECT t.id, t.iri, t.account_id, a.name AS account_name, "
            "       t.posted_date, t.amount, "
            "       t.payee_id, COALESCE(p.name, '') AS payee_name, "
            "       t.category_id, COALESCE(c.name, '') AS category_name, "
            "       t.status, COALESCE(t.memo, '') AS memo, "
            "       t.transfer_id "
            "FROM txn t "
            "JOIN      account a  ON a.id = t.account_id "
            "LEFT JOIN payee p    ON p.id = t.payee_id "
            "LEFT JOIN category c ON c.id = t.category_id "
            "WHERE t.account_id = ? "
            "  AND (t.status != 'Reconciled' OR t.statement_id = ?) "
            "ORDER BY t.posted_date ASC, t.id ASC",
            (account_id, sid),
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

    def list_cleared_unreconciled_txns(self, account_id: int) -> list[int]:
        """Txn ids on the account currently in 'Cleared' status — the
        pre-tick set for "Automatically select Cleared Transactions"."""
        cur = self._conn.execute(
            "SELECT id FROM txn "
            "WHERE account_id = ? AND status = 'Cleared' "
            "ORDER BY posted_date ASC, id ASC",
            (account_id,),
        )
        return [int(r["id"]) for r in cur]

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
        ``status='Reconciled'`` + ``statement_id``, snapshot the residual into
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
                "UPDATE txn SET status = 'Reconciled', statement_id = ? "
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
        ``'Cleared'`` and its ``statement_id`` is cleared; the tick set
        (``statement_txn``) is kept so rows show pre-ticked on resume. The
        statement goes back to ``status='open'`` with ``closing_variance``
        reset and ``reconciled_at`` cleared.

        Note: rows that were Pending/Uncleared at reconcile time come back as
        Cleared — accepted per ADR-040."""
        stmt = self.get_statement(statement_id)
        if stmt is None:
            raise ValueError(f"No statement with id {statement_id}.")
        if stmt.status != "reconciled":
            raise ValueError("Statement is not closed.")
        try:
            self._conn.execute(
                "UPDATE txn SET status = 'Cleared', statement_id = NULL "
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
        Reconciled to it revert to ``'Cleared'``; the statement and its tick
        rows are removed (``statement_txn`` cascades). No-op-safe if already
        gone."""
        try:
            self._conn.execute(
                "UPDATE txn SET status = 'Cleared', statement_id = NULL "
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
        """True if the txn is currently Reconciled to a closed statement —
        the gate for the "change anyway?" confirm on inline edits."""
        row = self._conn.execute(
            "SELECT 1 FROM txn WHERE id = ? AND status = 'Reconciled'",
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
