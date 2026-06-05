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
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")

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

    def get_account_by_iri(self, iri: str) -> Optional[AccountSummary]:
        row = self._conn.execute(
            "SELECT id, iri, name, type, family, currency, is_liability "
            "FROM account WHERE iri = ?",
            (iri,),
        ).fetchone()
        if row is None:
            return None
        return AccountSummary(
            id=row["id"], iri=row["iri"], name=row["name"],
            type=row["type"], family=row["family"],
            currency=row["currency"], is_liability=bool(row["is_liability"]),
        )

    def list_accounts(self) -> list[AccountSummary]:
        """All non-archived accounts in display order (family, name)."""
        cur = self._conn.execute(
            "SELECT id, iri, name, type, family, currency, is_liability "
            "FROM account "
            "WHERE archived_at IS NULL "
            "ORDER BY family, name"
        )
        return [
            AccountSummary(
                id=r["id"], iri=r["iri"], name=r["name"],
                type=r["type"], family=r["family"],
                currency=r["currency"], is_liability=bool(r["is_liability"]),
            )
            for r in cur
        ]

    def account_has_transactions(self, account_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM txn WHERE account_id = ? LIMIT 1", (account_id,),
        ).fetchone()
        return row is not None

    # ── Category ──

    def uncategorised_id(self) -> int:
        return UNCATEGORISED_ID

    def find_or_create_category_path(
        self, segments: list[str], source: str = "import",
    ) -> int:
        """Walk or create a category path (root → leaf), return leaf id.

        Each segment is a name; intermediate nodes are created with the
        given source if missing. Whitespace-only segments are ignored.
        Returns Uncategorised id if segments is empty after cleanup.
        """
        clean = [s.strip() for s in segments if s and s.strip()]
        if not clean:
            return UNCATEGORISED_ID
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
                cur = self._conn.execute(
                    "INSERT INTO category (parent_id, name, source) VALUES (?, ?, ?)",
                    (parent_id, name, source),
                )
                parent_id = cur.lastrowid
            else:
                parent_id = row["id"]
        return parent_id

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
        """All transactions for one account, chronologically, with running balance."""
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
        running = Decimal("0.00")
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
