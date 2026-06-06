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
        """Return all active categories with their immediate parent name for
        disambiguation. Sorted by parent then name. The parent_name is the
        immediate parent only — for deep nesting the full path is not shown.

        `kinds`, when provided, narrows the result to categories of those
        kinds (e.g. `('transfer',)` for the transfer-target picker)."""
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
            f"WHERE c.archived_at IS NULL{kind_clause} "
            "ORDER BY COALESCE(p.name, ''), c.name",
            params,
        )
        return [
            CategoryChoice(
                id=r["id"], name=r["name"],
                parent_name=r["parent_name"], source=r["source"],
                kind=r["kind"],
            )
            for r in cur
        ]

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
        amount: Decimal,        # positive magnitude
        category_id: int,
        memo: str = "",
        status: str = "Pending",
    ) -> tuple[int, int]:
        """Create a transfer between two accounts as two linked txns.

        Both rows share one `transfer_id` IRI so the delete path (and any
        future report logic) can treat them as a single operation. The
        source's payee is "Transfer to <dest>"; the destination's is
        "Transfer from <source>" — that gives the register a self-
        documenting display without a special UI marker.

        Validation:
        - source and destination must differ;
        - amount must be > 0;
        - status must be a valid txn.status enum value.

        Atomic — either both rows are inserted or neither is. Returns the
        ``(source_txn_id, destination_txn_id)`` pair.
        """
        if from_account_id == to_account_id:
            raise ValueError("Source and destination accounts must differ.")
        if amount <= 0:
            raise ValueError("Transfer amount must be greater than zero.")
        if status not in ("Pending", "Uncleared", "Cleared", "Reconciled"):
            raise ValueError(f"Invalid status: {status!r}")

        # Look up account names for the payee labels.
        rows = self._conn.execute(
            "SELECT id, name FROM account WHERE id IN (?, ?)",
            (from_account_id, to_account_id),
        ).fetchall()
        names = {r["id"]: r["name"] for r in rows}
        if from_account_id not in names or to_account_id not in names:
            raise ValueError("Unknown account id passed to create_transfer.")
        from_name = names[from_account_id]
        to_name = names[to_account_id]

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
                amount=amount,
                payee_id=payee_from,
                category_id=category_id,
                status=status,
                memo=memo,
                posted_date=posted_date,
                transfer_id=transfer_iri,
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
    ) -> int:
        """Pair an existing txn with a new partner row to form a transfer.

        Generates one shared `transfer_id`, applies it to the existing txn,
        and inserts a partner on `other_account_id` with the opposite-sign
        amount. The partner's payee reads "Transfer from <source>"; the
        existing txn's payee is left alone so any meaningful import-derived
        payee survives. Atomic — either both rows have the transfer_id and
        the partner exists or nothing changed. Returns the partner txn id.
        """
        try:
            partner_id = self._convert_to_transfer_unbatched(
                txn_id, other_account_id,
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
        """Convert each of `txn_ids` into a transfer paired with a fresh
        partner on `other_account_id`. Each pair gets its own
        `transfer_id` — they're independent transfers, just submitted as
        one batch. All-or-nothing: any failure rolls back every change.
        Returns the partner ids in the order they were created."""
        if not txn_ids:
            return []
        try:
            partner_ids = [
                self._convert_to_transfer_unbatched(tid, other_account_id)
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
                self._convert_to_transfer_unbatched(tid, other_account_id)
                for tid in txn_ids
            ]
            self.commit()
            return partner_ids
        except Exception:
            self.rollback()
            raise

    def _convert_to_transfer_unbatched(
        self, txn_id: int, other_account_id: int,
    ) -> int:
        """Internal helper used by both single and bulk convert. Does NOT
        commit — caller is responsible for the transaction boundary."""
        row = self._conn.execute(
            "SELECT t.id, t.account_id, t.amount, t.posted_date, "
            "       t.category_id, t.status, t.memo, t.transfer_id, "
            "       a.name AS account_name "
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
        partner_amount_pence = -int(row["amount"])
        cur = self._conn.execute(
            "INSERT INTO txn "
            "(iri, account_id, posted_date, amount, payee_id, category_id, "
            " status, memo, import_hash, import_batch_id, transfer_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                partner_iri, other_account_id, row["posted_date"],
                partner_amount_pence, partner_payee_id, row["category_id"],
                row["status"], row["memo"], transfer_iri,
            ),
        )
        return cur.lastrowid

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
                if amount < 0:
                    from_id = sched.account_id
                    to_id = sched.transfer_to_account_id
                else:
                    from_id = sched.transfer_to_account_id
                    to_id = sched.account_id
                # create_transfer commits internally; replicate its work
                # inline so we can include the schedule update in the same
                # transaction.
                transfer_iri = new_transfer_iri()
                from_name = self._conn.execute(
                    "SELECT name FROM account WHERE id = ?", (from_id,),
                ).fetchone()["name"]
                to_name = self._conn.execute(
                    "SELECT name FROM account WHERE id = ?", (to_id,),
                ).fetchone()["name"]
                payee_to = self.get_or_create_payee(f"Transfer to {to_name}")
                payee_from = self.get_or_create_payee(
                    f"Transfer from {from_name}"
                )
                magnitude = abs(amount)
                source_id = self._insert_transfer_half(
                    account_id=from_id, amount=-magnitude,
                    payee_id=payee_to, category_id=sched.category_id,
                    status="Pending", memo=sched.memo,
                    posted_date=posted_date, transfer_id=transfer_iri,
                )
                self._insert_transfer_half(
                    account_id=to_id, amount=magnitude,
                    payee_id=payee_from, category_id=sched.category_id,
                    status="Pending", memo=sched.memo,
                    posted_date=posted_date, transfer_id=transfer_iri,
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
