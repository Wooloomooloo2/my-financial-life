"""Repository — the only layer that touches SQL.

Hides the SQLite schema from service and UI code, and converts between
Decimal (interface) and INTEGER pence (storage) for currency amounts.

Transactional boundaries are the caller's responsibility: a service that
performs multiple writes wraps them in a try/except and calls commit() on
success or rollback() on failure.
"""
from __future__ import annotations

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


@dataclass(frozen=True)
class PayeeRow:
    """A payee with the number of transactions currently referring to it."""
    id: int
    name: str
    usage_count: int


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


# ── Identifier helpers (ADR-006) ────────────────────────────────────────────


def new_transaction_iri() -> str:
    return f"mfl:Transaction_{uuid.uuid4().hex[:8]}"


def new_import_batch_iri() -> str:
    return f"mfl:ImportBatch_{uuid.uuid4().hex[:8]}"


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

    def list_payees_with_usage(self) -> list[PayeeRow]:
        """All payees with their current transaction count, sorted by name.
        Includes payees with zero transactions (manually added, or left over
        after deletions)."""
        cur = self._conn.execute(
            "SELECT p.id, p.name, "
            "       (SELECT COUNT(*) FROM txn t WHERE t.payee_id = p.id) AS n "
            "FROM payee p "
            "WHERE p.archived_at IS NULL "
            "ORDER BY p.name COLLATE NOCASE"
        )
        return [
            PayeeRow(id=r["id"], name=r["name"], usage_count=int(r["n"]))
            for r in cur
        ]

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

        Single SQL transaction: either all sources are merged and removed
        or nothing changes."""
        sources = [sid for sid in source_ids if sid != target_id]
        if not sources:
            return 0
        placeholders = ",".join("?" * len(sources))
        try:
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
            "       t.status, COALESCE(t.memo, '') AS memo "
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
            "       t.status, COALESCE(t.memo, '') AS memo "
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
            )
            for r in cur
        ]

    def list_categories_flat(self) -> list[CategoryChoice]:
        """Return all active categories with their immediate parent name for
        disambiguation. Sorted by parent then name. The parent_name is the
        immediate parent only — for deep nesting the full path is not shown."""
        cur = self._conn.execute(
            "SELECT c.id, c.name, c.source, COALESCE(p.name, '') AS parent_name "
            "FROM category c "
            "LEFT JOIN category p ON p.id = c.parent_id "
            "WHERE c.archived_at IS NULL "
            "ORDER BY COALESCE(p.name, ''), c.name"
        )
        return [
            CategoryChoice(
                id=r["id"], name=r["name"],
                parent_name=r["parent_name"], source=r["source"],
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
        """Delete one or more transactions by id. Returns the rows-affected
        count. Commits on success; rolls back on error."""
        if not txn_ids:
            return 0
        placeholders = ",".join("?" * len(txn_ids))
        try:
            cur = self._conn.execute(
                f"DELETE FROM txn WHERE id IN ({placeholders})",
                tuple(txn_ids),
            )
            self.commit()
            return cur.rowcount
        except Exception:
            self.rollback()
            raise
